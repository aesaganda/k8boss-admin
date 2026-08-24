"""
CLI pods (§15) — a pod with ``kubectl`` in it, and a shell into that pod.

An operator halfway through an incident wants to run one command this console
does not have a page for: ``kubectl get --raw /metrics``, ``kubectl auth
can-i --list``, an ``oc adm`` subcommand. The alternative is leaving the console
for a laptop with a kubeconfig on it, which is the moment the audit trail stops.

So this module creates a pod whose image carries ``kubectl`` (or ``oc``), and
§7's exec socket opens a shell in it. **It adds no new terminal and no new
websocket**: ``WS /api/ws/pods/{ns}/{name}/exec`` already exists, is already
gated on ``ADMIN_ALLOW_MUTATIONS`` and a preflight of ``create pods/exec``, and
already writes an audit record when a session opens and another when it closes.
A second exec path would be a second place for those to be got right.

**Be exact about what this is.** Everything typed in that shell happens outside
this console's write funnel: no preflight naming the permission, no ``dryRun=All``
projection, no diff on screen, no ``resourceVersion`` check, and no audit row
saying *what changed*. The trail records that a shell was opened on this pod, by
whom, for how long and how many bytes went through it — and nothing about the
`kubectl delete` typed into it. That is the same bargain §7 already strikes for
a shell in any pod, stated again here because this pod is the one whose entire
purpose is running cluster commands.

The design follows from that:

**It has its own gate.** ``ADMIN_CLI_ENABLED`` must be on *as well as*
``ADMIN_ALLOW_MUTATIONS``, the third such pair in this codebase and for the
reason §8 of the safety model gives: a blast radius different enough in kind
that an operator may reasonably want every other write without it. A console
with this off can still scale a Deployment through a diff; it just cannot be
used as a kubectl terminal.

**The ServiceAccount is the whole of the permission story, and Kubernetes gives
us no way to gate it.** kubectl inside the pod authenticates as the pod's
ServiceAccount, so a shell here can do exactly what that account can do — not
what the console can do, and not what the signed-in operator can do. There is no
RBAC verb covering *which* ServiceAccount a pod may bind: a caller holding
``create pods`` in a namespace can bind any account in it, including one far
more privileged than the caller. That means ``ADMIN_CLI_SERVICE_ACCOUNT`` is the
only control over this feature's reach, it is a deployment setting rather than a
per-user one, and it cannot be narrowed by anybody's RBAC afterwards.

The default is therefore ``default`` — the account every namespace has and which
holds no permissions at all. Out of the box kubectl in this pod is refused by the
API server for everything, and a cluster admin has to deliberately bind a Role
before it can do anything. That is a feature that arrives useless rather than a
feature that arrives dangerous.

**The account is named in the diff.** The create goes through :func:`mutate` as
every write here does, so ``before`` is ``None`` and the unified diff is the
whole manifest as an addition — ``serviceAccountName`` included, on screen,
before the operator confirms. That is the disclosure mechanism, and it costs
nothing because the funnel already does it.

**Not privileged, no host namespaces, no host mounts.** Nothing this pod does
needs the machine it lands on; §5.5's node debug pod is the feature for that and
is gated separately. ``allowPrivilegeEscalation: false`` and every capability
dropped, so the pod satisfies PodSecurity ``baseline`` and most of
``restricted``.

**It is reused, and it is not removed automatically.** Opening the CLI when a
pod is already Running reuses it rather than creating a second one, because a
pod per click is how this feature would leak a dozen of them into a namespace in
an afternoon. Nothing deletes it on tab close — a closed tab is not a signal and
a restarted console pod drops whatever would have issued the DELETE — so, like
§5.5, it does the honest thing instead: it can find its own pods again by label,
``ADMIN_CLI_MAX_SECONDS`` bounds how long one runs, and removal is an explicit
action through the same funnel.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any

from app.admin.apply import create_fn, delete_resource
from app.admin.mutate import mutate
from app.audit import recorder
from app.config import settings
from app.errors import (
    AdminError,
    Invalid,
    MutationsDisabled,
    NotFound,
    podsecurity_hint,
)
from app.resources import reader
from app.resources.envelope import envelope
from app.resources.shaping import container_state, get_field

logger = logging.getLogger(__name__)

#: The label every pod this module creates carries, and the only thing it
#: selects on. The same key §5.5 uses — it is this console's component label —
#: with a different value, so listing one never returns the other. A *label*
#: rather than an annotation because it has to be selectable: the console must
#: be able to find the pods it created on a cluster where other tooling also
#: creates pods with tools in them.
COMPONENT_LABEL = "k8boss-admin/component"
COMPONENT_VALUE = "cli"
SELECTOR = f"{COMPONENT_LABEL}={COMPONENT_VALUE}"

#: ``k8boss-cli-<suffix>``. Prefixed rather than generated blind so that an
#: operator running `kubectl get pods` in this namespace can tell at a glance
#: that the pod is this console's terminal and not a workload somebody deployed.
NAME_PREFIX = "k8boss-cli-"

#: The container name, fixed. The UI passes it to §7's exec socket rather than
#: letting the terminal pick, and a one-container pod has nothing to choose
#: between anyway.
CONTAINER_NAME = "cli"

#: Same alphabet as §5.5's and §7.4's: no vowels, so a generated name cannot
#: spell a word, and none of the characters most often misheard when a name is
#: read aloud during an incident.
_SUFFIX_ALPHABET = "bcdfghjkmnpqrstvwxz23456789"
_SUFFIX_LENGTH = 5

_MAX_IMAGE_LENGTH = 512
_IMAGE_FORBIDDEN = re.compile(r"[\s\x00-\x1f]")

#: Upper bound on how many of this console's CLI pods a listing reports. There
#: should be one; a namespace holding more than this has a leak worth seeing in
#: full rather than a page of.
_LIST_LIMIT = 200


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

#: The sentence ``ADMIN_CLI_ENABLED`` being off produces, in one place because
#: both :func:`enabled_state` and :func:`_require_enabled` say it, and an
#: operator reading the disabled button's tooltip and the 403 that follows a
#: forced request should not be given two different explanations of one switch.
_FEATURE_OFF_DETAIL = (
    "CLI pods are disabled on this deployment (ADMIN_CLI_ENABLED is off). This "
    "is a separate gate from ADMIN_ALLOW_MUTATIONS because a shell running "
    "kubectl bypasses every control this console puts in front of a write: no "
    "preflight naming the permission, no dry run, no diff, and no audit record "
    "of what changed."
)


def enabled_state() -> dict[str, Any]:
    """Whether this deployment permits CLI pods, and the sentence why.

    Returned to the caller rather than only enforced, so the UI can disable the
    button *with the reason* (rule 11.4) instead of offering it and producing a
    403. The two gates are reported separately because they send an operator to
    two different lines of the same file, and "writes are off" is a different
    conversation from "writes are on and this one specific thing is not".
    """
    if not settings.admin_allow_mutations:
        return {
            "enabled": False,
            "detail": (
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "creates nothing on a cluster. The pod can still be previewed — "
                "reading what would be created is a read — but a shell could not "
                "be opened in it, because §7 gates exec with the writes for the "
                "same reason: a shell can do everything a write can."
            ),
        }
    if not settings.cli_enabled:
        return {"enabled": False, "detail": _FEATURE_OFF_DETAIL}
    return {"enabled": True, "detail": "CLI pods are enabled on this deployment."}


def _require_enabled(*, dry_run: bool) -> None:
    """Refuse the feature gate before the cluster is touched, naming the setting.

    **This checks ``ADMIN_CLI_ENABLED`` only.** ``ADMIN_ALLOW_MUTATIONS`` is
    :func:`mutate`'s to enforce, and it already refuses a real write on a
    read-only console with this same error class and audits the refusal.
    Checking it here as well would put two denial rows in the trail for one
    attempt and make the count of "who tried" wrong.

    That split also gives the two gates the behaviour each should have. A
    read-only console still **projects** this pod, because inspecting what would
    be created is a read and §1.6 permits it — unlike §5.5, whose projection is
    a working recipe for a privileged pod. The feature gate refuses the dry run
    too: a deployment that switched this off has decided the console is not a
    kubectl terminal, and offering a preview of one is offering the feature.

    ``MutationsDisabled`` rather than ``RBACDenied``: the operator's permissions
    are irrelevant to this refusal, and telling them otherwise sends them to
    argue with a cluster admin about a ClusterRole that is already correct.
    """
    if settings.cli_enabled:
        return

    target = {
        "group": "", "version": "v1", "resource": "pods",
        "namespace": settings.cli_namespace, "name": None,
    }
    error = MutationsDisabled(
        "Starting a CLI session is disabled on this console.",
        detail=_FEATURE_OFF_DETAIL,
        hint=(
            "Set ADMIN_ALLOW_MUTATIONS=true and ADMIN_CLI_ENABLED=true to allow "
            "it. Both are required; the second exists so this one action can be "
            "withheld while every other write stays available."
        ),
        context={**target, "verb": "create"},
    )
    # Audited as a denial, the way `mutate()` audits its own gate refusal.
    # Somebody attempting to start a kubectl terminal on a console where that is
    # switched off is a fact worth keeping: either the deployment is configured
    # wrongly or the caller believes it is not, and both are answered by this row
    # existing. It is also the one record that would otherwise be missing,
    # because the funnel — which audits everything else — is never reached.
    #
    # Never raises: see `app.audit`. A failed INSERT must not turn a refusal into
    # a 500, which would tell the operator the console is broken rather than that
    # the feature is off.
    recorder.record(
        verb="create",
        target=target,
        dry_run=dry_run,
        outcome="denied",
        detail="CLI pod refused (feature disabled)",
        error=f"{error.code}: {error.message}",
    )
    logger.warning("Refused a CLI pod: ADMIN_CLI_ENABLED is false.")
    raise error


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _check_image(image: str | None) -> str:
    """The CLI image, or 422 naming the rule.

    Validated rather than forwarded because it is the one field a caller
    supplies and it lands in a pod spec. The console cannot check the part that
    actually matters — that the image has ``kubectl`` or ``oc`` on its PATH —
    and does not pretend to: an image without them produces a pod that starts
    and a shell that answers "command not found", which is legible. What is not
    legible is a stray newline from a paste turning into an API server
    validation error about a field nobody typed.
    """
    value = (image if image is not None else settings.cli_image or "").strip()
    if not value:
        raise Invalid(
            "A CLI pod needs an image.",
            hint=(
                "Name an image with kubectl (or oc) on its PATH. This console's "
                f"configured default is {settings.cli_image!r}."
            ),
            context={"parameter": "image"},
        )
    if len(value) > _MAX_IMAGE_LENGTH:
        raise Invalid(
            f"That image reference is {len(value)} characters long "
            f"(limit {_MAX_IMAGE_LENGTH}).",
            context={"parameter": "image", "length": len(value),
                     "limit": _MAX_IMAGE_LENGTH},
        )
    if _IMAGE_FORBIDDEN.search(value):
        raise Invalid(
            "An image reference cannot contain whitespace or control characters.",
            detail=f"received {value!r}",
            hint="This is usually a line break picked up by a copy and paste.",
            context={"parameter": "image", "value": value},
        )
    return value


def _pod_name() -> str:
    """``k8boss-cli-<suffix>``.

    Nothing from the request goes into the name — unlike §5.5, which carries the
    node — because a CLI pod is not *about* anything. A random suffix keeps two
    operators starting a session in the same second from colliding on a name.
    """
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
    return f"{NAME_PREFIX}{suffix}"


# --------------------------------------------------------------------------- #
# The pod
# --------------------------------------------------------------------------- #

def build_pod(
    *,
    name: str,
    image: str,
    namespace: str,
    service_account: str,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    """The manifest this module creates. Pure — no I/O, so it can be asserted on.

    What is *absent* here is as deliberate as what is present, and the contrast
    with §5.5's ``build_pod`` is the point: no ``hostPath``, no ``hostPID``, no
    ``hostNetwork``, no ``nodeName``, no toleration. This pod does not care which
    machine it lands on and has no business reading one.

    The fields that are here:

    * ``serviceAccountName`` with ``automountServiceAccountToken: true`` is the
      whole feature — it is what makes ``kubectl`` inside the container able to
      talk to the API server, and it is what decides everything it may do there.
      §5.5 sets the opposite (``false``) for the opposite reason. See the module
      docstring on why RBAC cannot gate which account this is.
    * ``command`` is a shell loop rather than the image's own entrypoint, which
      for a kubectl image *is kubectl* and would exit immediately with a usage
      message. It is written as ``while : ; do sleep 3600; done`` rather than
      ``sleep infinity`` because BusyBox's ``sleep`` — what a small image ships —
      takes a number and rejects ``infinity``, so the pod would CrashLoop on
      exactly the minimal images an operator is most likely to pick.
    * ``stdin``/``tty`` so §7's exec socket has a terminal to attach to. Without
      them the shell opens and every curses program in it renders as noise.
    * ``restartPolicy: Never`` — a terminal pod that crash-loops is litter, and
      the operator wants to see that it exited rather than watch it be replaced.
    * ``allowPrivilegeEscalation: false``, every capability dropped, and the
      runtime's default seccomp profile. Nothing kubectl does needs any of them.
      ``runAsNonRoot`` is deliberately **not** set: it would make an image whose
      user is root fail to start with a kubelet error naming a field the operator
      never chose, and it is the namespace's PodSecurity level — not this
      console — that decides whether that is required. A namespace enforcing
      ``restricted`` refuses this pod at the dry run, carrying admission's own
      message, before anything exists.
    * No ``resources``: an unset request is BestEffort, which the kubelet always
      admits. A request could be refused on a cluster under pressure, and a
      terminal that will not start during an incident is a terminal that is not
      there when it is wanted.
    * ``activeDeadlineSeconds`` (``ADMIN_CLI_MAX_SECONDS``, 0 to disable) bounds
      how long the container runs. It does **not** delete the pod — the kubelet
      stops the container and marks the pod Failed — so it bounds the window in
      which an unattended session is possible and leaves the removal exactly
      where it was.
    """
    # 0 (or None) means unbounded, and the field is then omitted rather than
    # sent as 0 — the API server rejects activeDeadlineSeconds: 0.
    deadline = settings.cli_max_seconds if max_seconds is None else max_seconds

    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {COMPONENT_LABEL: COMPONENT_VALUE},
        },
        "spec": {
            "serviceAccountName": service_account,
            "automountServiceAccountToken": True,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": CONTAINER_NAME,
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", "while :; do sleep 3600; done"],
                    "stdin": True,
                    "tty": True,
                    "terminationMessagePolicy": "File",
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                }
            ],
        },
    }
    if deadline:
        pod["spec"]["activeDeadlineSeconds"] = int(deadline)
    return pod


# --------------------------------------------------------------------------- #
# Reading what is already there
# --------------------------------------------------------------------------- #

def _row(pod: Any) -> dict[str, Any]:
    """One CLI pod, as the UI reads it."""
    statuses = get_field(pod, "status", "containerStatuses", default=[]) or []
    state, reason = container_state(statuses[0] if statuses else None)
    container = (get_field(pod, "spec", "containers", default=[]) or [{}])[0]
    return {
        "name": get_field(pod, "metadata", "name"),
        "namespace": get_field(pod, "metadata", "namespace"),
        "image": get_field(container, "image"),
        # From the spec, because it is the account kubectl in this pod actually
        # authenticates as — which is the only thing that decides what a shell
        # here can do. `null` when the field is absent means the API server
        # defaulted it; the UI says "unknown", not "default", because those are
        # different claims about somebody's permissions.
        "serviceAccount": get_field(pod, "spec", "serviceAccountName"),
        "phase": get_field(pod, "status", "phase"),
        "state": state,
        "reason": reason,
        "node": get_field(pod, "spec", "nodeName"),
        "started_at": (
            get_field(statuses[0] if statuses else None, "state", "running", "startedAt")
            or get_field(statuses[0] if statuses else None, "state", "terminated", "startedAt")
        ),
        "created_at": get_field(pod, "metadata", "creationTimestamp"),
    }


def list_cli_pods() -> dict[str, Any]:
    """``GET /api/cli`` (§15).

    Every CLI pod this console created, plus whether the deployment permits
    creating another and what a new one would be made of.

    ``items: []`` is a real zero — the namespace was listed and holds none. A
    listing that could not happen raises, per §0.1: a CLI panel that showed
    "no session" because it could not read the namespace would have an operator
    starting a second pod beside the one already running.
    """
    namespace = settings.cli_namespace
    listing = reader.list_resource(
        "", "v1", "pods", namespace=namespace, label_selector=SELECTOR, limit=_LIST_LIMIT,
    )
    items = [_row(pod) for pod in listing.get("items") or []]

    state = enabled_state()
    # The continue token is carried through rather than dropped. `envelope()`
    # derives nothing from it, so a listing that stopped at the limit and
    # reported `continue: null` would be saying "these are all of them" about a
    # page — §0.1's corollary applied to pagination.
    result = envelope(items, cont=listing.get("continue"), remaining=listing.get("remaining"))
    # Additive to §1.2. The UI needs all of these: whether it may offer the
    # action, the sentence to put on the disabled control, and — because they are
    # what the operator is actually agreeing to — where the pod appears, what
    # image it runs and which ServiceAccount decides what kubectl in it can do.
    result["enabled"] = state["enabled"]
    result["enabledDetail"] = state["detail"]
    result["namespace"] = namespace
    result["image"] = settings.cli_image
    result["serviceAccount"] = settings.cli_service_account
    result["container"] = CONTAINER_NAME
    return result


# --------------------------------------------------------------------------- #
# The writes
# --------------------------------------------------------------------------- #

def create_cli_pod(
    *, image: str | None = None, dry_run: bool = True,
) -> dict[str, Any]:
    """``POST /api/cli`` (§15).

    Returns the §1.5 mutation response plus the pod, so the caller can open a
    terminal on it without parsing the diff.

    Nothing here reuses an existing pod. Reuse is the caller's decision and the
    UI makes it from :func:`list_cli_pods`, because "there is already a Running
    session, use that one" is an answer an operator should see rather than a
    branch a create endpoint takes silently — a POST that sometimes creates and
    sometimes does not cannot report ``applied`` honestly.
    """
    resolved_image = _check_image(image)

    # Before the cluster is touched. See `_require_enabled` for which of the two
    # gates refuses a projection and why they differ.
    _require_enabled(dry_run=dry_run)

    namespace = settings.cli_namespace
    service_account = settings.cli_service_account
    name = _pod_name()
    body = build_pod(
        name=name, image=resolved_image, namespace=namespace,
        service_account=service_account,
    )

    try:
        result = mutate(
            verb="create",
            group="",
            version="v1",
            plural="pods",
            namespace=namespace,
            name=name,
            dry_run=dry_run,
            apply_fn=create_fn("", "v1", "pods", body, namespace=namespace, name=name),
            before=None,
        # The audit sentence names the ServiceAccount, because that — not the
        # image and not the namespace — is what decides everything a shell in
        # this pod is able to do to the cluster.
            detail=(
                f"CLI pod {name} ({resolved_image}) as ServiceAccount "
                f"{service_account} in {namespace}"
            ),
        )
    except AdminError as e:
        # The funnel has already audited this failure; only the advice changes.
        # Re-raised either way, so a caller sees the same code and status it
        # would have seen without this clause.
        raise podsecurity_hint(
            e, namespace=namespace, setting="ADMIN_CLI_NAMESPACE"
        ) from None

    result["pod"] = name
    result["namespace"] = namespace
    result["image"] = resolved_image
    result["serviceAccount"] = service_account
    result["container"] = CONTAINER_NAME
    return result


def remove_cli_pod(pod: str, *, dry_run: bool = True) -> dict[str, Any]:
    """``DELETE /api/cli/{pod}`` (§15). §1.5 mutation response.

    **This endpoint deletes only pods this console created.** The label is
    checked first, and a pod failing it is a ``404 not_found`` for *this*
    endpoint rather than a delete. Without that check the route would be a
    namespaced pod-delete reachable by anyone who can call it, bypassing the
    resource browser's own confirm dialog. The generic delete in §4 remains the
    way to remove any other pod, gated as it already is.
    """
    namespace = settings.cli_namespace
    context = {
        "group": "", "version": "v1", "resource": "pods",
        "namespace": namespace, "name": pod,
    }

    live = reader.get_resource("", "v1", "pods", pod, namespace=namespace)
    labels = get_field(live, "metadata", "labels", default={}) or {}
    if labels.get(COMPONENT_LABEL) != COMPONENT_VALUE:
        raise NotFound(
            f'Pod "{pod}" is not a CLI pod created by this console.',
            detail=f"It does not carry {COMPONENT_LABEL}={COMPONENT_VALUE}.",
            hint=(
                "This endpoint removes only the CLI pods this console created. "
                "Delete any other pod from the resource browser."
            ),
            context=context,
        )

    # Delegated rather than reimplemented: §4's delete already reads the live
    # object so the diff is `before=live, after=null`, already goes through the
    # funnel, and already refuses on a read-only console. A second delete path
    # would be a second place for those to be got right.
    #
    # `Background` propagation: a pod owns nothing, so there is nothing to
    # cascade to and nothing to wait for.
    return delete_resource("", "v1", "pods", namespace, pod, "Background", dry_run)


__all__ = [
    "COMPONENT_LABEL",
    "COMPONENT_VALUE",
    "CONTAINER_NAME",
    "NAME_PREFIX",
    "SELECTOR",
    "build_pod",
    "create_cli_pod",
    "enabled_state",
    "list_cli_pods",
    "remove_cli_pod",
]
