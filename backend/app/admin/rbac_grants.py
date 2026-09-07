"""
§30 — granting and revoking a role inside one namespace.

**The write is one RoleBinding patch. This module is the sentence in front of
it,** for the reason §20, §24, §25 and §26 exist: the diff of an RBAC change is
a name being added to a list, and what that name can now *do* is in a different
object that nobody opened.

Four things are true about RoleBindings and none of them is visible in the
binding:

**A `roleRef` is a name, not a capability.** `subjects: [alice]` under
`roleRef: ClusterRole/admin` is three words, and `admin` in a namespace includes
`create rolebindings` — so alice can now grant herself, and anyone else,
everything else bindable here. `edit` includes `create pods/exec`, and an exec
session reads every Secret mounted into every pod in the namespace whether or
not the role mentions Secrets. Both are ordinary, both are the reason the grant
was made, and neither is legible from the word on the button. §30 resolves the
role and says what its rules confer, in the dialog, before the write.

**A binding to a role that does not exist is accepted.** The API server does not
validate `roleRef` against anything. The binding grants nothing — until somebody
creates a Role by that name, at which point it silently starts granting, and
anybody holding `create roles` in the namespace can be that somebody. A console
that renders that as an ordinary successful grant has reported a permission
that does not exist yet and will appear without another decision.

**A role this console could not read is unknown, not empty.** §0.1's corollary,
pointed at a security control: a grant whose rules could not be fetched must not
render as a grant of nothing. It gets its own consequence and the operator
acknowledges binding a role whose contents were not established.

**Removing a subject from a binding is not revoking their access.** They may be
named in another binding here, or in a ClusterRoleBinding, which grants
cluster-wide and therefore also here. A console that says "revoked" and leaves
the access in place is the defect standard exactly: an action reported as done
is a claim, and this one would be false. So a revoke's plan lists what *else*
names the subject — and when the cluster-wide listing is refused, that list is
`null` and says so, because "nothing else grants this" is the sentence that must
never be produced by a read that did not happen.

**Scope, deliberately narrow.**

*One binding per call.* If two RoleBindings in the namespace share a `roleRef`,
§30 refuses rather than picking one: "remove alice from `view`" has two possible
meanings there, and silently choosing the first would report a revoke that left
her bound. §4 edits either one by name.

*Patch, never delete.* Revoking the last subject leaves the binding in place
with an empty `subjects` list, which grants nothing. Deleting it instead would
be a second verb whose blast radius — every *other* subject in it — this plan
would then also have to explain, for the sake of tidiness. The plan says the
binding will remain and empty; §4 deletes it.

*`roleRef` is immutable.* The API server rejects a change to it, so "move alice
from `view` to `edit`" is not an edit here: it is a revoke and a grant, two
writes, two diffs, two audit rows. §30 does not hide that behind one button.

*Namespace scope only.* §30 writes RoleBindings. It does not create
ClusterRoleBindings — a cluster-wide grant is not a namespace administrator's
action with a namespace's blast radius, and offering it on a namespace page is
how one gets made by someone who meant the namespace. §4 creates them.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin.apply import MERGE_PATCH, create_fn, patch_fn
from app.admin.mutate import FeatureGate, mutate, read_only_switch
from app.errors import AdminError, Conflict, Invalid, NotFound
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

GROUP, VERSION = "rbac.authorization.k8s.io", "v1"
BINDINGS = "rolebindings"
CLUSTER_BINDINGS = "clusterrolebindings"
ROLES = "roles"
CLUSTER_ROLES = "clusterroles"

#: The three subject kinds a RoleBinding accepts.
SUBJECT_KINDS = ("User", "Group", "ServiceAccount")

#: The two things a namespaced RoleBinding may reference. A `RoleBinding` to a
#: **ClusterRole** is the common case and the one most often misread in both
#: directions: it confers that ClusterRole's rules *inside this namespace only*.
#: It is not a cluster-wide grant, and it is not weaker than the ClusterRole.
ROLE_KINDS = ("Role", "ClusterRole")

OPERATIONS = ("grant", "revoke")

#: How many bindings one listing reads. A page limit is a claim about
#: completeness, so both listings check for the API server's `continue` token
#: rather than assuming one page was all of it: the residual list exists to stop
#: "revoked" being said when it is false, and a limit silently applied would put
#: the sentence straight back — this time in a namespace nobody thought was big.
PAGE = 500

# --------------------------------------------------------------------------- #
# Powers — what a rule set actually confers
# --------------------------------------------------------------------------- #

POWER_FULL_CONTROL = "grant_full_control"
POWER_SECRET_READ = "grant_secret_read"
POWER_POD_EXEC = "grant_pod_exec"
POWER_ESCALATION = "grant_privilege_escalation"
POWER_IMPERSONATE = "grant_impersonate"

#: Resource names that make a rule an impersonation rule.
IMPERSONATION_TARGETS = ("users", "groups", "serviceaccounts")

# --------------------------------------------------------------------------- #
# Consequence codes — the §18 handshake
# --------------------------------------------------------------------------- #

WARN_FULL_CONTROL = "grant_confers_full_control"
WARN_ESCALATION = "grant_confers_privilege_escalation"
WARN_SECRET_ACCESS = "grant_confers_secret_access"
WARN_IMPERSONATION = "grant_confers_impersonation"
WARN_ROLE_UNREADABLE = "grant_role_unreadable"
WARN_ROLE_ABSENT = "grant_role_absent"
WARN_RULES_PENDING = "grant_rules_not_aggregated"
WARN_ACCESS_REMAINS = "revoke_access_remains"
WARN_RESIDUAL_UNKNOWN = "revoke_residual_unknown"


def _invalid(message: str, *, parameter: str, hint: str | None = None) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter})


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """`ADMIN_ALLOW_MUTATIONS` alone, and the dry run is not withheld.

    No switch of its own, for §26's reason: §4 can already write a RoleBinding on
    this deployment, so a flag that disabled only the endpoint which *explains*
    the grant would leave the unexplained one in place. The plan stays available
    on a read-only console because what a role confers is a read, and it is the
    read somebody wants most on the console they cannot write from.
    """
    return FeatureGate(
        feature="granting and revoking a role",
        message="Changing role bindings is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The plan is still available: what a "
                "role confers, and what would still grant access after a revoke, "
                "are reads."
            )),
        ),
        enabled_detail="This deployment permits changing role bindings.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §30's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_subject(raw: Any, *, namespace: str) -> dict[str, str]:
    """One RoleBinding subject, normalised, with the `apiGroup` the API requires.

    ``apiGroup`` is not cosmetic. A ``User`` or ``Group`` subject belongs to the
    RBAC API group and a ``ServiceAccount`` belongs to the core group, whose name
    is the empty string — and the API server rejects a subject that names the
    wrong one. Filling it in from ``kind`` here means no caller can send a
    subject that is *almost* right.

    A ``ServiceAccount`` without a namespace defaults to the namespace being
    granted in, which is what an operator means and what ``oc`` assumes. It is
    filled in rather than left out because a ServiceAccount subject with no
    namespace matches nothing, and a binding that matches nothing is a grant
    reported as made that does not apply to anybody.
    """
    if not isinstance(raw, dict):
        raise _invalid("`subject` must be an object.", parameter="subject")
    kind = str(raw.get("kind") or "").strip()
    if kind not in SUBJECT_KINDS:
        raise _invalid(
            f"{kind!r} is not a RoleBinding subject kind.",
            parameter="subject.kind",
            hint=f"Use one of {', '.join(SUBJECT_KINDS)}.",
        )
    name = str(raw.get("name") or "").strip()
    if not name:
        raise _invalid("`subject` names nobody.", parameter="subject.name")

    subject: dict[str, str] = {"kind": kind, "name": name}
    if kind == "ServiceAccount":
        subject["namespace"] = str(raw.get("namespace") or "").strip() or namespace
        subject["apiGroup"] = ""
    else:
        subject["apiGroup"] = GROUP
    return subject


def validate_role(raw: Any) -> dict[str, str]:
    """The `roleRef` being bound: a kind and a name, both required."""
    if not isinstance(raw, dict):
        raise _invalid("`role` must be an object.", parameter="role")
    kind = str(raw.get("kind") or "").strip()
    if kind not in ROLE_KINDS:
        raise _invalid(
            f"{kind!r} is not a role kind a RoleBinding can reference.",
            parameter="role.kind",
            hint=f"Use one of {', '.join(ROLE_KINDS)}.",
        )
    name = str(raw.get("name") or "").strip()
    if not name:
        raise _invalid("`role` names no role.", parameter="role.name")
    return {"kind": kind, "name": name, "apiGroup": GROUP}


def validate_operation(raw: Any) -> str:
    value = str(raw or "").strip()
    if value not in OPERATIONS:
        raise _invalid(
            f"{value!r} is not an operation.",
            parameter="operation",
            hint=f"Use one of {', '.join(OPERATIONS)}.",
        )
    return value


def validate(payload: dict[str, Any], *, namespace: str) -> dict[str, Any]:
    """``{operation, role, subject}`` — every field checked before any read."""
    return {
        "operation": validate_operation(payload.get("operation")),
        "role": validate_role(payload.get("role")),
        "subject": validate_subject(payload.get("subject"), namespace=namespace),
    }


# --------------------------------------------------------------------------- #
# What the role confers
# --------------------------------------------------------------------------- #

def _values(rule: Any, key: str) -> list[str]:
    return [str(value) for value in (get_field(rule, key, default=[]) or [])]


def _matches(values: list[str], *wanted: str) -> bool:
    """Does this rule field cover any of ``wanted``, wildcard included?"""
    return "*" in values or any(value in wanted for value in values)


def _core_group(rule: Any) -> bool:
    """Is this rule about the core API group? Its name is the empty string."""
    groups = _values(rule, "apiGroups")
    return "*" in groups or "" in groups


def powers_of(rules: list[Any]) -> list[dict[str, str]]:
    """The capabilities in a rule set that are worth a sentence of their own.

    Deliberately five, and deliberately not "every verb this role has". A finding
    that fires on most roles is one nobody reads on the day it matters — §28.6's
    rule, applied here. Each of these is either invisible from the role's name or
    reaches further than the name suggests:

    * **Full control** — a wildcard rule. There is nothing else to say about it.
    * **Reading Secrets** — the direct grant.
    * **Exec into pods** — the *indirect* one, and the reason `edit` is not a
      lesser `admin` where Secrets are concerned. A shell in a pod reads every
      Secret mounted into it and every value in its environment, so a role with
      no rule about Secrets at all still hands them over.
    * **Privilege escalation** — `create` or `update` on `rolebindings` (the
      grantee can grant themselves anything else bindable here), or the `escalate`
      or `bind` verbs, which are the two the API server checks when refusing to
      let somebody hand out more than they hold. Note that this fires on the
      stock `admin` ClusterRole. That is correct and is the point: binding
      `admin` in a namespace delegates the namespace's RBAC too.
    * **Impersonation** — acting as another identity, which makes every other
      limit on this subject advisory.

    A rule with `nonResourceURLs` and no `resources` confers none of these, and
    falls out of every branch below without a special case.
    """
    found: dict[str, str] = {}
    for rule in rules:
        verbs = _values(rule, "verbs")
        resources = _values(rule, "resources")
        groups = _values(rule, "apiGroups")

        if "*" in verbs and "*" in resources and "*" in groups:
            found[POWER_FULL_CONTROL] = (
                "Every verb on every resource in every API group, inside this "
                "namespace."
            )
        if (
            _core_group(rule)
            and _matches(resources, "secrets")
            and _matches(verbs, "get", "list", "watch")
        ):
            found[POWER_SECRET_READ] = (
                "Reads Secrets in this namespace directly."
            )
        if (
            _core_group(rule)
            and _matches(resources, "pods/exec", "pods/attach")
            and _matches(verbs, "create")
        ):
            found[POWER_POD_EXEC] = (
                "Opens a shell in any pod here, which reads every Secret mounted "
                "into it and every value in its environment — whether or not this "
                "role mentions Secrets."
            )
        if _matches(verbs, "escalate"):
            found[POWER_ESCALATION] = (
                "Holds `escalate`, which is the check the API server uses to stop "
                "somebody writing a role granting more than they hold."
            )
        elif _matches(verbs, "bind"):
            found.setdefault(POWER_ESCALATION, (
                "Holds `bind`, which is the check the API server uses to stop "
                "somebody binding a role granting more than they hold."
            ))
        elif (
            _matches(groups, GROUP)
            and _matches(resources, "rolebindings")
            and _matches(verbs, "create", "update", "patch")
        ):
            found.setdefault(POWER_ESCALATION, (
                "Writes RoleBindings in this namespace, so whoever holds it can "
                "grant themselves and anyone else every other role bindable here."
            ))
        if _matches(verbs, "impersonate") and _matches(resources, *IMPERSONATION_TARGETS):
            found[POWER_IMPERSONATE] = (
                "Acts as another user, group or ServiceAccount, which makes every "
                "other limit on this subject advisory."
            )

    order = (
        POWER_FULL_CONTROL, POWER_ESCALATION, POWER_IMPERSONATE,
        POWER_SECRET_READ, POWER_POD_EXEC,
    )
    return [{"code": code, "detail": found[code]} for code in order if code in found]


def role_capability(role: dict[str, str], *, namespace: str) -> dict[str, Any]:
    """What the referenced role is, and what it grants — or that we do not know.

    Three states, kept apart because they send an operator to three different
    places:

    ``present``
        The role was read. ``rules`` is what it holds and ``rule_count`` counts
        them. An explicit ``rules: []`` is a real zero and reads as one.

    ``absent``
        No such role. **The API server accepts this binding anyway**, and it
        grants nothing until a role by that name appears — after which it grants
        whatever that role holds, with nobody making a second decision. Usually
        it is a typo. Occasionally it is a trap somebody set.

    ``unreadable``
        The read failed. ``rules`` is ``None`` and ``rule_count`` is ``None``,
        never ``[]`` and never ``0``: this is §0.1's corollary pointed at a
        security control, and "grants nothing" is the one answer that must not be
        produced by a read that did not happen.

    ``rules`` is also ``None`` for a **present** ClusterRole with an
    ``aggregationRule`` whose controller has not filled it in yet. That role is
    not empty; it is not written yet, and it will grow on its own afterwards.
    ``aggregates`` says which case a null is.
    """
    plural = ROLES if role["kind"] == "Role" else CLUSTER_ROLES
    scope = namespace if role["kind"] == "Role" else None

    try:
        obj = reader.get_resource(GROUP, VERSION, plural, role["name"], namespace=scope)
    except NotFound:
        return {
            "state": "absent", "rules": None, "rule_count": None,
            "aggregates": False, "powers": [],
        }
    except AdminError as error:
        logger.info(
            "§30: could not read %s/%s: %s", role["kind"], role["name"], error,
        )
        return {
            "state": "unreadable", "rules": None, "rule_count": None,
            "aggregates": False, "powers": [],
        }

    raw = get_field(obj, "rules")
    aggregates = get_field(obj, "aggregationRule") is not None
    if not isinstance(raw, list):
        # Absent `rules`, not empty rules. On an aggregated ClusterRole this is
        # the window before the controller writes them; reporting 0 would
        # describe an aggregate that will grant cluster-admin as granting
        # nothing.
        return {
            "state": "present", "rules": None, "rule_count": None,
            "aggregates": aggregates, "powers": [],
        }

    rules = [rule for rule in raw]
    return {
        "state": "present",
        "rules": rules,
        "rule_count": len(rules),
        "aggregates": aggregates,
        "powers": powers_of(rules),
    }


# --------------------------------------------------------------------------- #
# Bindings
# --------------------------------------------------------------------------- #

def _same_role(binding: Any, role: dict[str, str]) -> bool:
    ref = get_field(binding, "roleRef")
    if ref is None:
        return False
    return (
        get_field(ref, "kind") == role["kind"]
        and get_field(ref, "name") == role["name"]
    )


def subject_matches(candidate: Any, subject: dict[str, str]) -> bool:
    """Is this the same subject? Kind, name, and — for ServiceAccounts — namespace.

    ``apiGroup`` is deliberately not compared. It is derived from ``kind`` on the
    way in, and a binding written by hand may spell it out, omit it, or (for a
    ServiceAccount) give the empty string; comparing it would report an identical
    subject as a different one and revoke nothing.
    """
    if get_field(candidate, "kind") != subject["kind"]:
        return False
    if get_field(candidate, "name") != subject["name"]:
        return False
    if subject["kind"] == "ServiceAccount":
        return get_field(candidate, "namespace") == subject["namespace"]
    return True


def binding_subjects(binding: Any) -> list[dict[str, Any]]:
    """The binding's subjects as plain dicts. ``[]`` when the field is absent —
    which for `subjects` is a real empty: a RoleBinding with no subjects is
    legal, grants nothing, and is exactly what a revoke of the last subject
    leaves behind."""
    out: list[dict[str, Any]] = []
    for entry in get_field(binding, "subjects", default=[]) or []:
        row: dict[str, Any] = {
            "kind": get_field(entry, "kind"),
            "name": get_field(entry, "name"),
        }
        namespace = get_field(entry, "namespace")
        if namespace:
            row["namespace"] = namespace
        api_group = get_field(entry, "apiGroup")
        if api_group is not None:
            row["apiGroup"] = api_group
        out.append(row)
    return out


def _binding_summary(binding: Any) -> dict[str, Any]:
    ref = get_field(binding, "roleRef")
    return {
        "name": get_field(binding, "metadata", "name"),
        "namespace": get_field(binding, "metadata", "namespace"),
        "role": (
            {"kind": get_field(ref, "kind"), "name": get_field(ref, "name")}
            if ref is not None else None
        ),
    }


def list_bindings(namespace: str) -> tuple[list[Any], bool]:
    """``(bindings, truncated)`` for the namespace. Primary read — failure raises.

    There is no useful plan for a namespace whose bindings this console could not
    list: every branch below (which binding to patch, whether the subject is
    already there, what else still grants access) is computed from them, and a
    plan built on an empty list would answer all three wrongly at once.

    ``truncated`` is the same statement one page weaker. A `continue` token means
    there are bindings this call did not see, so "no binding here references that
    role" and "nothing else names this subject" are both unproven — and the
    caller refuses rather than writing against a picture it knows is partial.
    """
    envelope = reader.list_resource(GROUP, VERSION, BINDINGS, namespace=namespace, limit=PAGE)
    return list(envelope.get("items") or []), bool(envelope.get("continue"))


def find_binding(bindings: list[Any], role: dict[str, str]) -> tuple[Any | None, list[str]]:
    """``(the one binding for this role, the names of all of them)``.

    More than one is not an error here — it is a fact about the namespace, and
    the caller turns it into a refusal. Sorted by name so that "the first one" is
    the same object on every call rather than whatever the API server listed
    first.
    """
    matches = sorted(
        (b for b in bindings if _same_role(b, role)),
        key=lambda b: str(get_field(b, "metadata", "name") or ""),
    )
    names = [str(get_field(b, "metadata", "name") or "") for b in matches]
    return (matches[0] if matches else None), names


def residual_bindings(
    bindings: list[Any],
    subject: dict[str, str],
    *,
    exclude: str | None,
    unavailable: list[dict[str, Any]],
) -> dict[str, Any]:
    """What would still name this subject after the revoke.

    Two lists, and the second one is why this function exists rather than being
    a filter at the call site. RoleBindings in this namespace are already in
    hand. **ClusterRoleBindings are a second, cluster-scoped read that can be
    refused** — and when it is, the answer is ``null``, not ``[]``. `[]` there
    means "we checked the whole cluster and nothing else grants this", which is
    the single most dangerous sentence this feature could produce: it is the one
    an operator acts on by closing the ticket.
    """
    here = [
        _binding_summary(binding)
        for binding in bindings
        if get_field(binding, "metadata", "name") != exclude
        and any(subject_matches(s, subject) for s in get_field(binding, "subjects", default=[]) or [])
    ]

    cluster: list[dict[str, Any]] | None = None
    truncated = False
    with collect(unavailable, GROUP, CLUSTER_BINDINGS):
        envelope = reader.list_resource(GROUP, VERSION, CLUSTER_BINDINGS, limit=PAGE)
        raw = list(envelope.get("items") or [])
        truncated = bool(envelope.get("continue"))
        cluster = [
            _binding_summary(binding)
            for binding in raw
            if any(
                subject_matches(s, subject)
                for s in get_field(binding, "subjects", default=[]) or []
            )
        ]

    # A truncated listing keeps what it found — that is strictly more useful than
    # discarding it — and flags itself, because `[]` with more pages behind it
    # means "the first 500 did not name them", which is not the sentence the
    # empty list otherwise makes.
    return {
        "namespace_bindings": here,
        "cluster_bindings": cluster,
        "cluster_truncated": truncated,
    }


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def _spell(subject: dict[str, str]) -> str:
    if subject["kind"] == "ServiceAccount":
        return f"ServiceAccount {subject['namespace']}/{subject['name']}"
    return f"{subject['kind']} {subject['name']}"


def consequences_for(
    *,
    operation: str,
    role: dict[str, str],
    subject: dict[str, str],
    capability: dict[str, Any],
    residual: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The §18 handshake: every consequence, each acknowledged by code.

    **The narrower powers are suppressed when the role confers full control.**
    Both are true; one is actionable, and asking somebody to tick "this also
    reads Secrets" under "this grants everything" makes the list longer without
    making the decision better — §28.4's rule about `pdb_never_allows_disruption`
    and `pdb_blocking_now`, applied to the same problem.
    """
    out: list[dict[str, Any]] = []
    who = _spell(subject)
    what = f"{role['kind']} {role['name']}"
    codes = {power["code"] for power in capability["powers"]}

    if operation == "grant":
        if POWER_FULL_CONTROL in codes:
            out.append({
                "code": WARN_FULL_CONTROL,
                "label": f"{what} confers full control of this namespace",
                "consequence": (
                    f"{who} will be able to perform every verb on every resource "
                    "here, including reading every Secret and changing these "
                    "bindings."
                ),
                "mitigation": (
                    "Bind a narrower role, or scope the grant to the resources "
                    "actually needed with a Role of your own."
                ),
            })
        else:
            if POWER_ESCALATION in codes:
                out.append({
                    "code": WARN_ESCALATION,
                    "label": f"{what} lets its holder grant further access",
                    "consequence": (
                        f"{who} will be able to hand out roles in this namespace, "
                        "including to themselves. Revoking this one binding later "
                        "will not undo what they granted in the meantime."
                    ),
                    "mitigation": (
                        "Bind a role without rolebinding write access — `edit` "
                        "rather than `admin` is the usual pair."
                    ),
                })
            if POWER_IMPERSONATE in codes:
                out.append({
                    "code": WARN_IMPERSONATION,
                    "label": f"{what} confers impersonation",
                    "consequence": (
                        f"{who} will be able to act as another identity, so the "
                        "audit trail here and on the cluster records the "
                        "identity they borrowed."
                    ),
                    "mitigation": "Bind a role without the `impersonate` verb.",
                })
            if POWER_SECRET_READ in codes or POWER_POD_EXEC in codes:
                out.append({
                    "code": WARN_SECRET_ACCESS,
                    "label": f"{what} exposes this namespace's Secrets",
                    "consequence": (
                        f"{who} will be able to read the Secrets in this "
                        "namespace"
                        + (
                            " — through `pods/exec`, which reaches them even "
                            "though the role has no rule about Secrets."
                            if POWER_SECRET_READ not in codes else "."
                        )
                    ),
                    "mitigation": (
                        "Bind a role without `get secrets` and without "
                        "`create pods/exec`, and keep the credentials this "
                        "namespace holds in mind when deciding."
                    ),
                })

        if capability["state"] == "unreadable":
            out.append({
                "code": WARN_ROLE_UNREADABLE,
                "label": f"{what} could not be read",
                "consequence": (
                    "This console could not fetch the role, so what it confers "
                    "is unknown. The binding will still be created and will "
                    "grant whatever the role holds."
                ),
                "mitigation": (
                    "Grant this console `get` on the role, or read the role "
                    "yourself before binding it."
                ),
            })
        elif capability["state"] == "absent":
            out.append({
                "code": WARN_ROLE_ABSENT,
                "label": f"{what} does not exist",
                "consequence": (
                    "The API server accepts a binding to a role that is not "
                    f"there. It grants nothing today; the moment a {role['kind']} "
                    f"named {role['name']} is created, this binding starts "
                    "granting whatever it holds, with no further decision."
                ),
                "mitigation": (
                    "Check the spelling and the kind — a Role and a ClusterRole "
                    "of the same name are different objects."
                ),
            })
        elif capability["aggregates"] and capability["rules"] is None:
            out.append({
                "code": WARN_RULES_PENDING,
                "label": f"{what} is an aggregate whose rules are not written yet",
                "consequence": (
                    "Its rules are filled in by the aggregation controller and "
                    "are absent right now, so what this binding confers cannot "
                    "be listed — and it will grow on its own as matching roles "
                    "are labelled."
                ),
                "mitigation": (
                    "Wait for the controller to write the rules and preview "
                    "again, or bind a role that states its own."
                ),
            })

    if operation == "revoke" and residual is not None:
        here = residual["namespace_bindings"]
        cluster = residual["cluster_bindings"]
        if here or cluster:
            names = ", ".join(
                row["name"] for row in (here + (cluster or []))[:5] if row["name"]
            )
            out.append({
                "code": WARN_ACCESS_REMAINS,
                "label": f"{who} is still bound elsewhere",
                "consequence": (
                    f"{len(here)} other binding(s) in this namespace"
                    + (
                        f" and {len(cluster)} ClusterRoleBinding(s)"
                        if cluster else ""
                    )
                    + f" still name them: {names}. This revoke removes one "
                    "binding, not their access."
                ),
                "mitigation": (
                    "Use §23's subject review after this write to ask the API "
                    "server what they can still do, and remove the other "
                    "bindings if that is the intent."
                ),
            })
        if cluster is None or residual["cluster_truncated"]:
            out.append({
                "code": WARN_RESIDUAL_UNKNOWN,
                "label": (
                    "Cluster-wide bindings could not be listed"
                    if cluster is None
                    else "More cluster-wide bindings exist than were read"
                ),
                "consequence": (
                    "A ClusterRoleBinding grants everywhere, including here, so "
                    f"whether {who} keeps access through one is unknown. This is "
                    "not a report that none exists."
                ),
                "mitigation": (
                    "Grant this console `list` on clusterrolebindings, or ask "
                    "the API server directly with §23's subject review."
                ),
            })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Recomputed against the cluster as it is now, so acknowledging a preview of a
    `view` grant cannot carry over to an `admin` one.
    """
    if not consequences:
        return
    accepted = set(acknowledged or [])
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This change has consequences that have not been acknowledged.",
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
# The patch and the body
# --------------------------------------------------------------------------- #

def next_subjects(
    current: list[dict[str, Any]], subject: dict[str, str], *, operation: str
) -> list[dict[str, Any]]:
    """The `subjects` list this write is asking for."""
    if operation == "grant":
        return [*current, dict(subject)]
    return [entry for entry in current if not subject_matches(entry, subject)]


def build_subject_patch(
    subjects: list[dict[str, Any]], *, resource_version: str | None
) -> dict[str, Any]:
    """The merge patch for `subjects`, plus rule 4's version.

    An empty list is sent as ``[]`` and **not** as ``null``, which is the
    opposite of §24's choice for `spec.taints` and is deliberate. `null` there
    renders as the field disappearing, which is what removing every taint is.
    Here the field disappearing and the field being empty are the same grant —
    none — but only one of them shows the operator, in the diff, that the binding
    survives with nobody in it. That is the fact §30 refuses to hide, so it is
    the one the diff has to carry.
    """
    patch: dict[str, Any] = {"subjects": subjects}
    if resource_version:
        patch["metadata"] = {"resourceVersion": resource_version}
    return patch


def build_binding_body(
    namespace: str, role: dict[str, str], subjects: list[dict[str, Any]], *, name: str
) -> dict[str, Any]:
    """A whole RoleBinding, for the grant that has no binding to patch."""
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "roleRef": {"apiGroup": GROUP, "kind": role["kind"], "name": role["name"]},
        "subjects": subjects,
    }


def _require_version(
    name: str, sent: str | None, live: str | None, **extra: Any
) -> None:
    """§0.4, locally, so the operator gets a fresh plan rather than a bare 409.

    Enforced again by the API server, because the patch carries the caller's
    ``resourceVersion``. That second check is the load-bearing one: this one
    loses the race between the read above and the write below, and on a subject
    list that matters — two administrators granting at once, the second patch
    replacing the whole array, and one grant silently gone.
    """
    if not sent or not live or sent == live:
        return
    raise Conflict(
        f"The binding {name} changed while you were reading it.",
        detail=f"You are editing version {sent}; the cluster has {live}.",
        hint="Preview again against what the binding says now.",
        context={
            "group": GROUP, "version": VERSION, "resource": BINDINGS,
            "name": name, "verb": "patch",
            "currentResourceVersion": live,
            **extra,
        },
    )


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def _blocked(message: str, hint: str, **context: Any) -> dict[str, Any]:
    return {"message": message, "hint": hint, "context": context}


def _resolve(namespace: str, request: dict[str, Any]) -> dict[str, Any]:
    """Everything both the plan and the write need, computed once.

    Kept in one function because the write recomputes all of it against the
    cluster as it is *now* rather than trusting the plan the operator was
    looking at — the same discipline §24 and §26 use, and the reason a
    consequence acknowledged against one state cannot be spent on another.
    """
    role, subject, operation = request["role"], request["subject"], request["operation"]
    unavailable: list[dict[str, Any]] = []

    bindings, bindings_truncated = list_bindings(namespace)
    listed, all_names = find_binding(bindings, role)

    # Re-read the one binding this call would write, by name, and only when this
    # call would actually write to it — an ambiguous or truncated listing is
    # refused below, and fetching then spends a round trip on a binding nobody
    # is going to touch.
    #
    # The re-read itself is not an optimisation. The listing is trimmed for list
    # rows (§4 strips `last-applied-configuration` there) and the API server's
    # projection is not, so diffing the listed copy against the projection would
    # render kubectl's annotation as a line this write is adding. It is also the
    # fresher `resourceVersion`, which narrows the window the concurrency check
    # below loses.
    binding = listed
    if listed is not None and len(all_names) == 1 and not bindings_truncated:
        binding = reader.get_resource(
            GROUP, VERSION, BINDINGS,
            str(get_field(listed, "metadata", "name") or ""),
            namespace=namespace,
        )

    capability = role_capability(role, namespace=namespace)

    live_name = str(get_field(binding, "metadata", "name") or "") if binding else None
    already = bool(
        binding is not None
        and any(
            subject_matches(entry, subject)
            for entry in get_field(binding, "subjects", default=[]) or []
        )
    )

    residual = (
        residual_bindings(bindings, subject, exclude=live_name, unavailable=unavailable)
        if operation == "revoke" else None
    )

    blocked: dict[str, Any] | None = None
    if bindings_truncated:
        # Refused rather than warned. Every decision below — which binding to
        # write, whether the subject is already named, what else grants them
        # access — is read off this listing, and each one answered from a first
        # page is a confident answer about bindings nobody looked at.
        blocked = _blocked(
            f"{namespace} has more RoleBindings than this console read in one "
            "page.",
            "Every answer here is computed from that listing, so none of them "
            "can be trusted. Use the YAML editor on the binding you mean.",
            read=len(bindings),
        )
    elif len(all_names) > 1:
        blocked = _blocked(
            f"{len(all_names)} bindings in {namespace} reference "
            f"{role['kind']}/{role['name']}.",
            "Edit the one you mean by name with the YAML editor. Picking one "
            "here would report a change to a binding you did not choose.",
            bindings=all_names,
        )
    elif operation == "grant" and already:
        blocked = _blocked(
            f"{_spell(subject)} is already named in {live_name}.",
            "Nothing needs to happen. To widen their access, grant a different "
            "role — a binding's roleRef cannot be changed.",
            binding=live_name,
        )
    elif operation == "revoke" and binding is None:
        blocked = _blocked(
            f"No binding in {namespace} references {role['kind']}/{role['name']}.",
            "There is nothing here to revoke. The residual list says what else "
            "names this subject.",
        )
    elif operation == "revoke" and not already:
        blocked = _blocked(
            f"{live_name} does not name {_spell(subject)}.",
            "There is nothing to remove from this binding. The residual list "
            "says what else names this subject.",
            binding=live_name,
        )

    # The name a create would take, and the collision that refuses it. §30 does
    # not invent `admin-1`: a console that picks a name the operator never saw
    # has made a decision about an object they will later go looking for.
    create_name = role["name"]
    if operation == "grant" and binding is None and blocked is None:
        taken = next(
            (b for b in bindings if get_field(b, "metadata", "name") == create_name),
            None,
        )
        if taken is not None:
            ref = get_field(taken, "roleRef")
            blocked = _blocked(
                f"A binding named {create_name} already exists here and "
                f"references {get_field(ref, 'kind')}/{get_field(ref, 'name')}.",
                "A roleRef cannot be changed, so this grant needs a binding with "
                "another name. Create it with the YAML editor.",
                binding=create_name,
            )

    current = binding_subjects(binding) if binding is not None else []
    return {
        "operation": operation,
        "role": {"kind": role["kind"], "name": role["name"]},
        "subject": subject,
        "binding": (_binding_summary(binding) if binding is not None else None),
        "createName": create_name if binding is None else None,
        "resourceVersion": (
            str(get_field(binding, "metadata", "resourceVersion"))
            if binding is not None and get_field(binding, "metadata", "resourceVersion")
            else None
        ),
        "capability": capability,
        "currentSubjects": current,
        "requestedSubjects": (
            next_subjects(current, subject, operation=operation)
            if blocked is None else current
        ),
        "residual": residual,
        "blocked": blocked,
        "unavailable": unavailable,
        "_live": binding,
        "_bindings": bindings,
    }


def plan(namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/access/namespaces/{namespace}/grants/plan`` (§30).

    Ungated and unaudited: reads only. It does not dry-run the patch — a dry run
    is a write request the caller has not made yet, and it needs the preflight
    the funnel does.

    A request that cannot proceed comes back ``blocked`` rather than as a `422`,
    the way §20's, §21's and §24's do: this is the screen where the grant is
    *decided*, and answering "there is nothing to revoke" with an error alone
    would withhold the residual list at the moment it is the thing that answers
    "then where does their access come from".
    """
    request = validate(payload, namespace=namespace)
    resolved = _resolve(namespace, request)
    resolved.pop("_live", None)
    resolved.pop("_bindings", None)
    unavailable = resolved.pop("unavailable")
    blocked = resolved["blocked"]

    return {
        "namespace": namespace,
        **resolved,
        "consequences": (
            []
            if blocked
            else consequences_for(
                operation=request["operation"],
                role=request["role"],
                subject=request["subject"],
                capability=resolved["capability"],
                residual=resolved["residual"],
            )
        ),
        "unavailable": unavailable,
        "partial": bool(unavailable),
        "gate": enabled_state(),
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def _detail(namespace: str, resolved: dict[str, Any]) -> str:
    """The audit sentence. It names the role and the powers, because "grant view
    to alice" and "grant admin to alice" are the same shape and not the same
    event, and the row is what somebody reads a month later."""
    role = resolved["role"]
    verb = "grant" if resolved["operation"] == "grant" else "revoke"
    parts = [
        f"{verb} {role['kind']}/{role['name']} "
        f"{'to' if verb == 'grant' else 'from'} {_spell(resolved['subject'])} "
        f"in {namespace}"
    ]
    powers = [power["code"] for power in resolved["capability"]["powers"]]
    if powers and verb == "grant":
        parts.append("confers " + ", ".join(powers))
    if resolved["capability"]["state"] != "present":
        parts.append(f"role {resolved['capability']['state']}")
    return "; ".join(parts)


def apply_grant(
    namespace: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/access/namespaces/{namespace}/grants`` (§30) — one funnel call.

    Order of refusals, each before the cluster is changed: the request is
    validated, the plan is recomputed against the namespace as it is *now*, a
    blocked plan is raised as `422`, the concurrency check runs against the live
    binding, and the acknowledgement check runs over the recomputed consequences.
    Then :func:`mutate`, which gates, preflights, sends the write with
    ``dryRun=All`` when this is a preview, diffs, and audits the outcome.

    **Two verbs, one funnel call.** A grant with no binding to extend is a
    ``create`` of a whole RoleBinding; everything else is a ``patch`` of
    ``subjects``. They are preflighted as what they are — ``create`` and
    ``patch`` name different permissions, and a caller who may patch an existing
    binding but not create one gets the denial that says so.

    **`applied: true` means the binding's subject list is what you sent.** For a
    revoke it does **not** mean the subject can no longer act here: the
    ``residual`` block in this response is the list of what still names them,
    and §23's subject review is how to ask the API server the authoritative
    question afterwards.
    """
    request = validate(payload, namespace=namespace)
    resolved = _resolve(namespace, request)
    live = resolved.pop("_live", None)
    resolved.pop("_bindings", None)
    unavailable = resolved.pop("unavailable")

    if resolved["blocked"]:
        raise Invalid(
            resolved["blocked"]["message"],
            hint=resolved["blocked"]["hint"],
            context={"parameter": "operation", **resolved["blocked"]["context"]},
        )

    sent_version = payload.get("resourceVersion")
    if live is not None:
        _require_version(
            str(get_field(live, "metadata", "name") or ""),
            sent_version,
            resolved["resourceVersion"],
            currentSubjects=resolved["currentSubjects"],
        )

    consequences = consequences_for(
        operation=request["operation"],
        role=request["role"],
        subject=request["subject"],
        capability=resolved["capability"],
        residual=resolved["residual"],
    )
    _require_acknowledgement(consequences, acknowledge_consequences)

    subjects = resolved["requestedSubjects"]
    if live is None:
        name = resolved["createName"]
        apply_fn = create_fn(
            GROUP, VERSION, BINDINGS,
            build_binding_body(namespace, request["role"], subjects, name=name),
            namespace=namespace, name=name,
        )
        verb = "create"
    else:
        name = str(get_field(live, "metadata", "name") or "")
        apply_fn = patch_fn(
            GROUP, VERSION, BINDINGS, name,
            build_subject_patch(
                subjects, resource_version=sent_version or resolved["resourceVersion"],
            ),
            namespace=namespace, content_type=MERGE_PATCH,
        )
        verb = "patch"

    result = mutate(
        verb=verb,
        group=GROUP,
        version=VERSION,
        plural=BINDINGS,
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        gate=_gate(),
        apply_fn=apply_fn,
        before=live,
        detail=_detail(namespace, resolved),
    )
    result["operation"] = resolved["operation"]
    result["role"] = resolved["role"]
    result["subject"] = resolved["subject"]
    result["binding"] = resolved["binding"] or {
        "name": name, "namespace": namespace, "role": resolved["role"],
    }
    result["capability"] = resolved["capability"]
    result["currentSubjects"] = resolved["currentSubjects"]
    result["requestedSubjects"] = subjects
    result["residual"] = resolved["residual"]
    result["consequences"] = consequences
    result["unavailable"] = unavailable
    return result


__all__ = [
    "apply_grant",
    "binding_subjects",
    "build_binding_body",
    "build_subject_patch",
    "consequences_for",
    "enabled_state",
    "find_binding",
    "list_bindings",
    "next_subjects",
    "plan",
    "powers_of",
    "residual_bindings",
    "role_capability",
    "subject_matches",
    "validate",
]
