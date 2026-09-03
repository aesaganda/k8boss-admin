"""
Creating a project (§17): a namespace and the objects that govern it, as one
planned, acknowledged, per-object-reported sequence of ordinary writes.

OpenShift's ``oc new-project`` does not create a namespace. It instantiates a
*project request template*: a Namespace, a ResourceQuota, a LimitRange, a
RoleBinding handing the requester ``admin``, and — on most production
clusters — a NetworkPolicy or two. Vanilla Kubernetes has every one of those
objects and no act that produces them together, so a team gets a bare
namespace and the quota arrives a week later, after the incident. This module
is that act, and it is built out of things this console already does:

* **Five objects, five passes through the funnel.** Each is an ordinary create
  through :func:`app.admin.apply.create_from_yaml`, so each carries its own
  preflight, its own dry run, its own diff and its own audit row. There is no
  template stored anywhere and nothing reconciles afterwards; the defaults live
  in the request the operator sends, and what keeps the objects there is the
  cluster. ``docs/adr-0006-projects.md`` records why this is five writes and
  not a template engine.

* **The console never adopts a namespace.** A project is created into a
  namespace that does not exist. One that does — whoever created it, whatever
  it holds — is refused with a 409 naming it, before any object is written and
  on a dry run too, and the refusal is audited. This is stricter than §14's
  managed-by check on purpose: a namespace is somebody's, and there is no label
  that makes taking one over acceptable.

* **A partial create is reported as one.** ``created`` is true only when every
  object landed on a real write; ``failed`` and ``skipped`` count the rest; the
  per-object report names each failure with the grant it needed. Nothing is
  rolled back — deleting a namespace the operator just asked for is not a
  correction anybody wants made on their behalf.

* **The namespaced objects cannot be projected by the API server until the
  Namespace exists, and the dry run says so rather than pretending.** Admission
  refuses a create into a namespace that is not there — dry run included, with
  a 404 naming the namespace. So on a dry run the Namespace is projected by the
  API server like any other write, and the four objects inside it are reported
  with ``projection: "rendered"``: the console's own manifest, diffed against
  nothing, with a preflight of the verb the real write will need so a missing
  grant surfaces before the confirm rather than after the namespace exists. On
  the real write every object is projected and diffed by the API server, and
  the response says which kind of diff each one carries. §14's router install
  follows the same rule.

* **Every consequence is acknowledged by name.** The same handshake §13 and
  §16 use, for the same reason: a quota with no LimitRange behind it refuses
  every pod that does not spell out its own requests, and an operator who did
  not read that sentence finds it out from a Deployment that never gets a pod.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.admin import apply as apply_service
from app.admin import preflight
from app.admin.diff import build_diff
from app.audit import recorder
from app.config import settings
from app.errors import AdminError, Conflict, Invalid, MutationsDisabled, NotFound
from app.k8s.quantities import parse_quantity
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import (
    POD_SECURITY_LEVELS,
    POD_SECURITY_MODES,
    POD_SECURITY_PREFIX,
    get_field,
)
from app.services.projects import DESCRIPTION_ANNOTATION, DISPLAY_NAME_ANNOTATION

logger = logging.getLogger(__name__)

#: RFC 1123 DNS label — what the API server accepts for a namespace name.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

#: A Pod Security version label: ``latest`` or a minor version like ``v1.31``.
_PSA_VERSION = re.compile(r"^(latest|v1\.\d{1,3})$")

#: Prefixes the two platforms reserve for their own namespaces. Not refused —
#: an administrator may have a reason — but named as a consequence, because a
#: namespace called ``kube-payments`` is one every future reader assumes is the
#: control plane's.
RESERVED_PREFIXES = ("kube-", "openshift-")

#: The three built-in aggregated ClusterRoles a project admin can be handed.
PROJECT_ROLES = ("admin", "edit", "view")

SUBJECT_KINDS = ("User", "Group", "ServiceAccount")
LIMIT_TYPES = ("Container", "Pod", "PersistentVolumeClaim")

#: Quota keys that make the API server refuse a pod which does not state its
#: own requests or limits for that resource. A quota over any of these needs a
#: LimitRange supplying defaults, or every unadorned pod is refused at admission.
_COMPUTE_QUOTA_KEYS = {
    "requests.cpu": ("cpu", "defaultRequest"),
    "requests.memory": ("memory", "defaultRequest"),
    "limits.cpu": ("cpu", "default"),
    "limits.memory": ("memory", "default"),
    "cpu": ("cpu", "defaultRequest"),
    "memory": ("memory", "defaultRequest"),
}

#: Fixed object names. Fixed rather than derived from the project name so a
#: project created here and one created by hand from the same recipe collide
#: on the object rather than silently coexisting as two quotas.
QUOTA_NAME = "project-quota"
LIMIT_RANGE_NAME = "project-limits"
ISOLATION_POLICY_NAME = "allow-same-namespace"

#: Consequence codes. A closed set: the frontend renders one checkbox per code
#: and the write refuses unless every code present is acknowledged by name, so
#: a new code is a contract change rather than a new string.
WARN_NAMESPACE_UNKNOWN = "namespace_unknown"
WARN_RESERVED_PREFIX = "reserved_prefix"
WARN_PSA_NOT_ENFORCED = "psa_not_enforced"
WARN_PSA_ENFORCED = "psa_enforced"
WARN_NO_QUOTA = "no_quota"
WARN_QUOTA_NEEDS_DEFAULTS = "quota_needs_defaults"
WARN_NO_ADMIN = "no_admin"
WARN_INGRESS_ISOLATED = "ingress_isolated"

#: What the API server says when it refuses a RoleBinding because the caller
#: does not itself hold what the role grants and holds no ``bind``. Matched as
#: a substring of the detail, exactly as §14's router does and with the same
#: justification: it only ever rewrites the *hint*, never the code or the
#: status, and a miss degrades to the ordinary rbac_denied hint.
_ESCALATION_MARKER = "attempting to grant rbac permissions not currently held"


@dataclass(frozen=True)
class ProjectObject:
    """One object a project creates, and where it goes."""

    kind: str
    group: str
    version: str
    plural: str
    namespace: str | None
    name: str
    body: dict[str, Any]


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def enabled_state() -> dict[str, Any]:
    """Whether this deployment can create a project, and the sentence why not.

    One gate, not two: unlike §14 and §16 there is no feature switch of its
    own, because nothing here is a larger commitment than the §4 YAML editor
    already offers. Every object is one the editor could create; this endpoint
    adds the plan, the checks and the per-object report in front of them.
    """
    if not settings.admin_allow_mutations:
        return {
            "enabled": False,
            "detail": (
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so "
                "it writes nothing to a cluster. The plan, the projected Namespace "
                "and the rendered objects are still available: reading what "
                "would be created is a read."
            ),
        }
    return {"enabled": True, "detail": "This deployment permits creating projects."}


def _require_enabled(*, dry_run: bool, name: str) -> None:
    """Refuse a real write before the cluster is touched. Dry runs pass through."""
    if dry_run:
        return
    state = enabled_state()
    if state["enabled"]:
        return
    target = {"group": "", "version": "v1", "resource": "namespaces", "namespace": None, "name": name}
    error = MutationsDisabled(
        "Creating a project is disabled on this console.",
        detail=state["detail"],
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        context={**target, "verb": "create"},
    )
    # Written directly because the funnel — which audits everything else — is
    # never reached. A refusal that left no row would be a hole in the trail at
    # exactly the moment somebody asks who tried.
    recorder.record(
        verb="create",
        target=target,
        dry_run=dry_run,
        outcome="denied",
        detail=f"project {name}: create refused",
        error=f"{error.code}: {error.message}",
    )
    logger.warning("project create refused: %s (name=%s)", state["detail"], name)
    raise error


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


def _quantity_map(value: Any, *, parameter: str) -> dict[str, str]:
    """Validate a ``{resource: quantity}`` map and return it with wire strings.

    Every value must parse under the Kubernetes quantity grammar and be
    non-negative. Checked here rather than left to the API server because the
    API server's message names a field path inside an object the operator never
    wrote by hand, and this one names the form field.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _invalid(f"`{parameter}` must be a map of resource name to quantity.", parameter=parameter)
    out: dict[str, str] = {}
    for key, raw in value.items():
        resource = str(key or "").strip()
        if not resource or len(resource) > 253:
            raise _invalid(f"`{parameter}` has an empty or overlong resource name.", parameter=parameter)
        if raw is None or str(raw).strip() == "":
            # An empty value is a field the operator left blank, not a zero.
            continue
        text = str(raw).strip()
        parsed = parse_quantity(text)
        if parsed is None or parsed < 0:
            raise _invalid(
                f"{text!r} is not a Kubernetes quantity for {resource} in `{parameter}`.",
                parameter=parameter,
                hint="Use the API server's grammar: 500m, 2, 1Gi, 256Mi, 10.",
                resource=resource,
                value=text,
            )
        out[resource] = text
    return out


def _pod_security(raw: Any) -> dict[str, str | None]:
    """Validate the three Pod Security modes and their optional versions."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _invalid("`podSecurity` must be an object.", parameter="podSecurity")
    out: dict[str, str | None] = {}
    for mode in POD_SECURITY_MODES:
        level = raw.get(mode)
        level = str(level).strip() if level is not None and str(level).strip() else None
        if level is not None and level not in POD_SECURITY_LEVELS:
            raise _invalid(
                f"{level!r} is not a Pod Security level.",
                parameter=f"podSecurity.{mode}",
                hint=f"Use one of {', '.join(POD_SECURITY_LEVELS)}.",
            )
        version = raw.get(mode + "Version")
        version = str(version).strip() if version is not None and str(version).strip() else None
        if version is not None and not _PSA_VERSION.match(version):
            raise _invalid(
                f"{version!r} is not a Pod Security version.",
                parameter=f"podSecurity.{mode}Version",
                hint="Use `latest` or a minor version such as `v1.31`.",
            )
        if version is not None and level is None:
            raise _invalid(
                f"`podSecurity.{mode}Version` is set but `podSecurity.{mode}` is not.",
                parameter=f"podSecurity.{mode}Version",
                hint="A version label without its level label is ignored by admission.",
            )
        out[mode] = level
        out[mode + "Version"] = version
    return out


def _limits(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("`limits` must be a list of LimitRange items.", parameter="limits")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _invalid(f"`limits[{index}]` must be an object.", parameter=f"limits[{index}]")
        kind = str(item.get("type") or "").strip()
        if kind not in LIMIT_TYPES:
            raise _invalid(
                f"{kind!r} is not a LimitRange item type.",
                parameter=f"limits[{index}].type",
                hint=f"Use one of {', '.join(LIMIT_TYPES)}.",
            )
        entry = {"type": kind}
        for field in ("max", "min", "default", "defaultRequest", "maxLimitRequestRatio"):
            values = _quantity_map(item.get(field), parameter=f"limits[{index}].{field}")
            if values:
                entry[field] = values
        if len(entry) == 1:
            # A type with nothing under it is a LimitRange item that constrains
            # nothing, and the API server accepts it. Refused here because an
            # operator who submitted it almost certainly left the fields blank
            # by mistake, and the resulting object would look like a limit.
            raise _invalid(
                f"`limits[{index}]` sets no max, min, default, defaultRequest or maxLimitRequestRatio.",
                parameter=f"limits[{index}]",
            )
        out.append(entry)
    return out


def _subjects(raw: Any, *, project: str) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("`admins` must be a list of subjects.", parameter="admins")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _invalid(f"`admins[{index}]` must be an object.", parameter=f"admins[{index}]")
        kind = str(item.get("kind") or "").strip()
        if kind not in SUBJECT_KINDS:
            raise _invalid(
                f"{kind!r} is not a RoleBinding subject kind.",
                parameter=f"admins[{index}].kind",
                hint=f"Use one of {', '.join(SUBJECT_KINDS)}.",
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise _invalid(f"`admins[{index}]` names no subject.", parameter=f"admins[{index}].name")
        entry = {"kind": kind, "name": name}
        if kind == "ServiceAccount":
            # A ServiceAccount subject is namespaced. Defaulting to the project
            # itself is what `oc new-project` does for the requester's SA and is
            # the only namespace guaranteed to exist once the create finishes.
            entry["namespace"] = str(item.get("namespace") or "").strip() or project
        elif item.get("namespace"):
            raise _invalid(
                f"A {kind} subject carries no namespace.",
                parameter=f"admins[{index}].namespace",
            )
        out.append(entry)
    return out


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Second validation layer, behind the pydantic body — as §16 does, and why."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise _invalid("No project name was given.", parameter="name")
    if not _DNS_LABEL.match(name):
        raise _invalid(
            f"{name!r} is not a valid namespace name.",
            parameter="name",
            hint=(
                "Lower-case letters, digits and hyphens, starting and ending with a "
                "letter or digit, at most 63 characters."
            ),
        )

    role = str(payload.get("adminRole") or "admin").strip()
    if role not in PROJECT_ROLES:
        raise _invalid(
            f"{role!r} is not a project role.",
            parameter="adminRole",
            hint=f"Use one of {', '.join(PROJECT_ROLES)} — the built-in aggregated ClusterRoles.",
        )

    display_name = str(payload.get("displayName") or "").strip() or None
    description = str(payload.get("description") or "").strip() or None

    return {
        "name": name,
        "displayName": display_name,
        "description": description,
        "podSecurity": _pod_security(payload.get("podSecurity")),
        "quota": _quantity_map(payload.get("quota"), parameter="quota"),
        "limits": _limits(payload.get("limits")),
        "admins": _subjects(payload.get("admins"), project=name),
        "adminRole": role,
        "isolateIngress": bool(payload.get("isolateIngress", False)),
    }


# --------------------------------------------------------------------------- #
# The objects
# --------------------------------------------------------------------------- #

def build_objects(request: dict[str, Any]) -> list[ProjectObject]:
    """The objects a project is, in the order they are written.

    The Namespace first, because everything else lives in it. Nothing is added
    that the caller did not ask for: no managed-by label (the console does not
    manage a namespace after creating it — see the module docstring), no
    annotations of this console's own. The two OpenShift annotations are the
    caller's display name and description, and only appear when given.
    """
    name = request["name"]
    labels = {}
    psa = request["podSecurity"]
    for mode in POD_SECURITY_MODES:
        if psa.get(mode):
            labels[POD_SECURITY_PREFIX + mode] = psa[mode]
            if psa.get(mode + "Version"):
                labels[POD_SECURITY_PREFIX + mode + "-version"] = psa[mode + "Version"]
    annotations = {}
    if request["displayName"]:
        annotations[DISPLAY_NAME_ANNOTATION] = request["displayName"]
    if request["description"]:
        annotations[DESCRIPTION_ANNOTATION] = request["description"]

    metadata: dict[str, Any] = {"name": name}
    if labels:
        metadata["labels"] = labels
    if annotations:
        metadata["annotations"] = annotations

    objects = [
        ProjectObject(
            kind="Namespace", group="", version="v1", plural="namespaces",
            namespace=None, name=name,
            body={"apiVersion": "v1", "kind": "Namespace", "metadata": metadata},
        )
    ]

    if request["quota"]:
        objects.append(ProjectObject(
            kind="ResourceQuota", group="", version="v1", plural="resourcequotas",
            namespace=name, name=QUOTA_NAME,
            body={
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": QUOTA_NAME, "namespace": name},
                "spec": {"hard": dict(request["quota"])},
            },
        ))

    if request["limits"]:
        objects.append(ProjectObject(
            kind="LimitRange", group="", version="v1", plural="limitranges",
            namespace=name, name=LIMIT_RANGE_NAME,
            body={
                "apiVersion": "v1",
                "kind": "LimitRange",
                "metadata": {"name": LIMIT_RANGE_NAME, "namespace": name},
                "spec": {"limits": [dict(item) for item in request["limits"]]},
            },
        ))

    if request["admins"]:
        subjects = []
        for subject in request["admins"]:
            entry: dict[str, Any] = {"kind": subject["kind"], "name": subject["name"]}
            if subject["kind"] == "ServiceAccount":
                entry["namespace"] = subject["namespace"]
            else:
                # User and Group subjects belong to the RBAC API group; a
                # ServiceAccount's apiGroup is the empty string and is omitted.
                entry["apiGroup"] = "rbac.authorization.k8s.io"
            subjects.append(entry)
        role = request["adminRole"]
        objects.append(ProjectObject(
            kind="RoleBinding", group="rbac.authorization.k8s.io", version="v1",
            plural="rolebindings", namespace=name, name=role,
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": role, "namespace": name},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": role,
                },
                "subjects": subjects,
            },
        ))

    if request["isolateIngress"]:
        objects.append(ProjectObject(
            kind="NetworkPolicy", group="networking.k8s.io", version="v1",
            plural="networkpolicies", namespace=name, name=ISOLATION_POLICY_NAME,
            body={
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": ISOLATION_POLICY_NAME, "namespace": name},
                "spec": {
                    # Every pod, ingress only, admitted from pods in this same
                    # namespace and from nowhere else. This is the shape
                    # OpenShift's project template ships under the same name.
                    "podSelector": {},
                    "policyTypes": ["Ingress"],
                    "ingress": [{"from": [{"podSelector": {}}]}],
                },
            },
        ))

    return objects


def _object_payload(item: ProjectObject) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "name": item.name,
        "namespace": item.namespace,
        "group": item.group,
        "version": item.version,
        "resource": item.plural,
        "yaml": reader.to_yaml(item.body),
    }


# --------------------------------------------------------------------------- #
# The target namespace
# --------------------------------------------------------------------------- #

def _read_namespace(name: str) -> dict[str, Any] | None:
    """The namespace as it exists now, or ``None`` if it does not.

    Only ``NotFound`` becomes ``None``. A forbidden read or an API server that
    did not answer propagates: turning either into "it is not there" is what
    would have this module create a project over somebody's namespace.
    """
    try:
        return reader.get_resource("", "v1", "namespaces", name, namespace=None)
    except NotFound:
        return None


def _refuse_existing(name: str, live: dict[str, Any], *, dry_run: bool) -> Conflict:
    """409 naming the namespace the console will not adopt, audited.

    Audited for §14's reason: this fires before the first ``mutate()``, so the
    funnel never records it, and "did anyone try to create a project over
    kube-system" is exactly the question the trail exists to answer.
    """
    phase = get_field(live, "status", "phase")
    error = Conflict(
        f"A namespace called {name} already exists.",
        detail=(
            f"It is {phase or 'in an unknown phase'} and was not created by this "
            "request. A project is created into a namespace that does not exist; "
            "this console does not add quotas, limits or bindings to one that "
            "does, because it cannot know what else is in there or who it belongs to."
        ),
        hint=(
            "Choose another name, or add the objects to the existing namespace "
            "one at a time through the YAML editor, where each is its own diff."
        ),
        context={
            "group": "", "version": "v1", "resource": "namespaces",
            "namespace": None, "name": name,
        },
    )
    recorder.record(
        verb="create",
        target={"group": "", "version": "v1", "resource": "namespaces", "namespace": None, "name": name},
        dry_run=dry_run,
        outcome="conflict",
        detail=f"project {name}: refused, the namespace already exists",
        error=f"{error.code}: {error.message}",
    )
    return error


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def consequences_for(request: dict[str, Any], *, exists: bool | None) -> list[dict[str, Any]]:
    """What the operator is consenting to, each as a code they must name.

    Every entry is knowable from the request and the one namespace read, and
    every one describes something that goes wrong *silently* otherwise — which
    is the bar for putting a checkbox in front of somebody.
    """
    out: list[dict[str, Any]] = []
    name = request["name"]

    if exists is None:
        out.append({
            "code": WARN_NAMESPACE_UNKNOWN,
            "label": "Whether this namespace already exists could not be checked",
            "consequence": (
                "The namespace read did not answer. The write re-reads before "
                "creating anything and refuses if the namespace is there, so "
                "nothing is taken over — but this plan cannot say which outcome "
                "to expect."
            ),
            "mitigation": "Retry when the API server is answering.",
        })

    if name.startswith(RESERVED_PREFIXES):
        out.append({
            "code": WARN_RESERVED_PREFIX,
            "label": f"{name} uses a prefix reserved for the platform",
            "consequence": (
                "kube- and openshift- are the prefixes the control plane and "
                "OpenShift use for their own namespaces. Nothing refuses the "
                "name, and every tool and person that reads it afterwards will "
                "assume it is the platform's."
            ),
            "mitigation": "Choose a name without that prefix.",
        })

    psa = request["podSecurity"]
    if psa.get("enforce") is None:
        out.append({
            "code": WARN_PSA_NOT_ENFORCED,
            "label": "No Pod Security level is enforced",
            "consequence": (
                "Without an enforce label, whatever the cluster's Pod Security "
                "admission default is applies to this namespace — and that "
                "default is read from a file on the API server which no API "
                "serves, so this console cannot tell you what it is. On a "
                "cluster with no default configured, every pod is admitted, "
                "privileged ones included."
            ),
            "mitigation": (
                "Enforce baseline or restricted, which is what an OpenShift "
                "project gets through its security context constraints."
            ),
        })
    elif psa["enforce"] in ("baseline", "restricted"):
        level = psa["enforce"]
        detail = (
            "restricted refuses any pod that runs as root, allows privilege "
            "escalation, keeps Linux capabilities, or does not set a seccomp "
            "profile — which includes many upstream images as shipped."
            if level == "restricted"
            else "baseline refuses privileged containers, host namespaces, "
            "hostPath volumes and hostPorts."
        )
        out.append({
            "code": WARN_PSA_ENFORCED,
            "label": f"Pods that do not meet the {level} level are refused",
            "consequence": (
                f"{detail} A Deployment whose template violates it is accepted, "
                "and then its ReplicaSet fails to create a single pod; the only "
                "evidence is an event on the ReplicaSet, and the workload sits at "
                "0 of N with no pod to look at."
            ),
            "mitigation": (
                "Set warn and audit to the same level so violations are "
                "reported on the write, and check the Events page for "
                "FailedCreate after deploying."
            ),
        })

    quota = request["quota"]
    if not quota:
        out.append({
            "code": WARN_NO_QUOTA,
            "label": "Nothing bounds what this project can consume",
            "consequence": (
                "With no ResourceQuota, one Deployment in this namespace can "
                "request every core and every byte the cluster has, and the "
                "scheduler will let it. This is what vanilla Kubernetes gives "
                "a namespace by default; a project without a quota is the "
                "thing this feature exists to avoid."
            ),
            "mitigation": "Set at least requests.cpu, requests.memory and pods.",
        })
    else:
        defaults = _limit_defaults(request["limits"])
        uncovered = sorted({
            key for key in quota
            if key in _COMPUTE_QUOTA_KEYS
            and _COMPUTE_QUOTA_KEYS[key][0] not in defaults[_COMPUTE_QUOTA_KEYS[key][1]]
        })
        if uncovered:
            out.append({
                "code": WARN_QUOTA_NEEDS_DEFAULTS,
                "label": "The quota will refuse pods that do not state their own resources",
                "consequence": (
                    f"A ResourceQuota over {', '.join(uncovered)} makes the API "
                    "server refuse every pod that does not set a request or limit "
                    "for that resource itself — `must specify "
                    f"{uncovered[0]}` — and no Container LimitRange here supplies "
                    "a default for it. Most manifests do not set these, so most "
                    "workloads deployed into this project will be accepted as a "
                    "Deployment and never get a pod."
                ),
                "mitigation": (
                    "Add a Container LimitRange item with default and "
                    "defaultRequest for cpu and memory, which is what an "
                    "OpenShift project template pairs with its quota."
                ),
            })

    if not request["admins"]:
        out.append({
            "code": WARN_NO_ADMIN,
            "label": "Nobody is bound to this project",
            "consequence": (
                "No RoleBinding is created, so only identities holding a "
                "cluster-wide binding can do anything in this namespace. The "
                "team it is for cannot deploy into it until somebody binds them."
            ),
            "mitigation": "Name a User, Group or ServiceAccount as its admin.",
        })

    if request["isolateIngress"]:
        out.append({
            "code": WARN_INGRESS_ISOLATED,
            "label": "Traffic from other namespaces is refused, the router's included",
            "consequence": (
                "The allow-same-namespace policy selects every pod and admits "
                "ingress only from pods in this namespace. An Ingress or Route "
                "to a Service here will not be served until a second policy "
                "admits the ingress controller's namespace, and monitoring "
                "scrapes from another namespace stop too. Whether any of this "
                "is enforced at all is decided by the cluster's CNI plugin, "
                "which no API this console can reach reports on."
            ),
            "mitigation": (
                "Add a policy admitting the router's namespace after creating "
                "the project, from the Network Policies tab."
            ),
        })

    return out


def _limit_defaults(limits: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Which resources the Container items default, by field."""
    out: dict[str, set[str]] = {"default": set(), "defaultRequest": set()}
    for item in limits:
        if item.get("type") != "Container":
            continue
        for field in out:
            out[field].update((item.get(field) or {}).keys())
    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name."""
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This project has consequences that have not been acknowledged.",
        detail="; ".join(f"{entry['code']}: {entry['label']}" for entry in missing),
        hint=(
            "Re-send with acknowledgeConsequences naming each of "
            + ", ".join(entry["code"] for entry in missing)
            + "."
        ),
        context={
            "parameter": "acknowledgeConsequences",
            "unacknowledged": [entry["code"] for entry in missing],
        },
    )


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan(payload: dict[str, Any]) -> dict[str, Any]:
    """§17 ``POST /api/projects/plan`` — the objects, and what creating them means.

    Ungated and unaudited: everything it does is a read. One namespace read
    decides ``target.exists``; a failure of that read is collected into
    ``unavailable[]`` and becomes ``exists: null`` plus a consequence, rather
    than failing the plan — the rendered objects are still worth reading.
    """
    request = validate_request(payload)
    unavailable: list[dict[str, Any]] = []
    objects = build_objects(request)

    exists: bool | None = None
    live: dict[str, Any] | None = None
    with collect(unavailable, "", "namespaces"):
        live = _read_namespace(request["name"])
        exists = live is not None

    consequences = consequences_for(request, exists=exists)
    gate = enabled_state()
    return {
        "name": request["name"],
        "target": {
            "exists": exists,
            "phase": get_field(live, "status", "phase") if live else None,
            "detail": (
                "The namespace read did not answer, so whether it exists is unknown."
                if exists is None
                else f"{request['name']} already exists; the create will be refused."
                if exists
                else f"{request['name']} does not exist and can be created.",
            ),
        },
        "objects": [_object_payload(item) for item in objects],
        "consequences": consequences,
        "enabled": gate["enabled"],
        "enabledDetail": gate["detail"],
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def _escalation_hint(item: ProjectObject, error: AdminError) -> AdminError:
    """Name RBAC escalation prevention when it, not a verb, refused the binding.

    The same failure §14 explains for ClusterRoles: preflight says the console
    may create RoleBindings, and the API server then refuses one that binds a
    role granting more than the console itself holds — unless it holds ``bind``
    on that ClusterRole. Only the hint changes.
    """
    if error.code != "rbac_denied" or item.kind != "RoleBinding":
        return error
    if _ESCALATION_MARKER not in str(error.detail or "").lower():
        return error
    error.hint = (
        "This is RBAC escalation prevention, not a missing verb: the API server "
        "refuses to let an identity bind a role granting permissions it does not "
        "itself hold. Grant the console's ServiceAccount `bind` on "
        f"rbac.authorization.k8s.io/clusterroles for `{item.name}`, or the "
        "permissions that ClusterRole aggregates. deploy/rbac.yaml's writer role "
        "carries `bind`."
    )
    return error


def _outcome(
    item: ProjectObject,
    *,
    result: dict[str, Any] | None,
    error: AdminError | None,
    projection: str | None,
    preflight_result: dict[str, Any] | None = None,
    skipped: str | None = None,
) -> dict[str, Any]:
    """One entry in the per-object report.

    ``applied`` is copied from the funnel's own answer and never derived here,
    for §14's reason. ``projection`` says whose diff this is: ``server`` when
    the API server projected it, ``rendered`` when it is this console's own
    manifest because the namespace did not yet exist to project into, ``None``
    when there is no diff at all.
    """
    return {
        "kind": item.kind,
        "name": item.name,
        "namespace": item.namespace,
        "group": item.group,
        "resource": item.plural,
        "verb": "create",
        "applied": bool(result and result.get("applied")),
        "diff": (result or {}).get("diff"),
        "projection": projection,
        "preflight": preflight_result,
        "auditId": (result or {}).get("auditId"),
        "skipped": skipped,
        "error": None if error is None else {
            "code": error.code, "message": error.message, "detail": error.detail,
            "hint": error.hint,
        },
    }


def _apply(item: ProjectObject, *, dry_run: bool, project: str) -> dict[str, Any]:
    """Create one object through the generic apply path, and so the funnel."""
    where = f"{item.namespace}/{item.name}" if item.namespace else item.name
    return apply_service.create_from_yaml(
        item.group, item.version, item.plural, item.namespace,
        reader.to_yaml(item.body), dry_run,
        detail=f"project {project}: create {item.kind} {where}",
    )


def _rendered(item: ProjectObject) -> dict[str, Any]:
    """The dry-run report for an object the API server cannot project yet.

    The manifest diffed against nothing, and a preflight of the verb the real
    write will use — the one thing about this object that *can* be checked
    before its namespace exists, and the one most worth knowing first.
    """
    check = preflight.check(
        "create", item.group, item.plural, namespace=item.namespace, name=item.name,
    )
    return _outcome(
        item,
        result={"applied": False, "diff": build_diff(None, item.body), "auditId": None},
        error=None,
        projection="rendered",
        preflight_result={
            "allowed": check.get("allowed"),
            "reason": check.get("reason"),
            "evaluationError": check.get("evaluationError"),
            "hint": check.get("hint"),
        },
    )


def create(
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """§17 ``POST /api/projects`` — create the namespace and what governs it.

    The plan is recomputed here rather than trusted from the caller, as §16
    does: acknowledgements are checked against the consequences of *this*
    request, not the one the operator read a minute ago.

    Order of refusals, each before anything is written: the mutations gate
    (real writes only), request validation, the namespace read — which must
    answer, because "unknown" is not a state a create may proceed from — the
    takeover refusal, and the acknowledgement check. Only then the writes.

    On a real write, a Namespace that failed to be created stops the sequence:
    the four objects inside it are reported ``skipped`` with the reason rather
    than attempted, because each would fail with a 404 naming the namespace and
    leave four audit rows saying nothing the first one did not.
    """
    request = validate_request(payload)
    name = request["name"]
    _require_enabled(dry_run=dry_run, name=name)

    objects = build_objects(request)

    # No `collect` here: a namespace read that did not answer must stop the
    # write, not become "it is not there".
    live = _read_namespace(name)
    if live is not None:
        raise _refuse_existing(name, live, dry_run=dry_run)

    consequences = consequences_for(request, exists=False)
    _require_acknowledgement(consequences, acknowledge_consequences)

    results: list[dict[str, Any]] = []
    failed = 0
    skipped = 0
    namespace_failed = False

    for item in objects:
        if item.kind != "Namespace" and dry_run:
            # The API server cannot project into a namespace that does not
            # exist, and on a dry run it never will. Rendered, and said so.
            results.append(_rendered(item))
            continue
        if item.kind != "Namespace" and namespace_failed:
            skipped += 1
            results.append(_outcome(
                item, result=None, error=None, projection=None,
                skipped=(
                    f"Not attempted: the Namespace {name} was not created, and a "
                    f"{item.kind} cannot be created into a namespace that does not exist."
                ),
            ))
            continue
        try:
            result = _apply(item, dry_run=dry_run, project=name)
        except AdminError as e:
            failed += 1
            error = _escalation_hint(item, e)
            if item.kind == "Namespace":
                namespace_failed = True
            results.append(_outcome(item, result=None, error=error, projection=None))
            logger.warning("project %s: create %s failed: %s", name, item.kind, e.message)
            continue
        results.append(_outcome(item, result=result, error=None, projection="server"))

    return {
        "dryRun": dry_run,
        # False whenever anything failed or was skipped, and false on every dry
        # run. A dry run that reported `created: true` would be §1.5's mistake
        # with a team's namespace attached to it.
        "created": (not dry_run) and failed == 0 and skipped == 0,
        "failed": failed,
        "skipped": skipped,
        "name": name,
        "objects": results,
        "consequences": consequences,
    }


__all__ = [
    "ISOLATION_POLICY_NAME",
    "LIMIT_RANGE_NAME",
    "PROJECT_ROLES",
    "QUOTA_NAME",
    "RESERVED_PREFIXES",
    "build_objects",
    "consequences_for",
    "create",
    "enabled_state",
    "plan",
    "validate_request",
]
