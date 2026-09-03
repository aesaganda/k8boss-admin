"""
Installing and managing the shipped router (§14).

This is the module that makes k8boss-admin, on request, put a reverse proxy on
somebody's cluster. Read :mod:`app.admin.router_bundle` first for *what* gets
installed and why HAProxy; this module is about *how*, and the how is the part
that keeps the console honest about what it has become.

**There is no controller here.** Install, upgrade and uninstall are each a
sequence of ordinary writes, one per object, each going through
:func:`app.admin.mutate.mutate` — the same gate, preflight, dry run, diff and
audit row as a scale. Nothing in this process watches the router afterwards.
:func:`status` is a live read like every other page in this console. What keeps
the router running is the Kubernetes control plane; what routes traffic is
HAProxy's own in-cluster controller. The console's model of the router is
"whatever the cluster currently says", and it stores nothing.

**The console never adopts an object it did not create.** Every object in the
bundle carries ``app.kubernetes.io/managed-by: k8boss-admin``. An install that
finds an object of the same name *without* that label refuses, names it, and
stops — before writing anything, on a dry run as much as on a real one. A
console that overwrote a ClusterRole or a Namespace somebody else was using,
because the name happened to match, would be the worst kind of confident wrong
answer: the operator asked for an install and got a silent takeover.

**A partial install is reported as a partial install.** Eight objects, eight
writes, and the sixth can fail. There is no rollback: deleting the five that
succeeded would be five more writes the operator did not approve, against
objects that may now be in use. So the response carries a per-object outcome,
``installed`` is false unless every one of them succeeded, and ``failed`` counts
the ones that did not. This is the same shape §5's drain uses, for the same
reason — "installed" over a half-created router is the sentence that leaves
somebody debugging an ingress path that was never finished.

**A dry run says whose diff each object carries.** The API server's
NamespaceLifecycle admission refuses a create into a namespace that does not
exist — ``dryRun=All`` included, with a 404 naming the namespace — so on a fresh
install the four objects inside the router's namespace cannot be projected until
the Namespace is real, and a dry run that tried came back reporting them as
``not_found`` failures on every real cluster. So the Namespace and the three
other cluster-scoped objects are projected by the API server
(``projection: "server"``), and when the Namespace does not exist yet the four
inside it are reported with ``projection: "rendered"``: the bundle's manifest
diffed against nothing, with a preflight of the ``create`` the real install will
need, so a missing grant surfaces before the confirm rather than after the
namespace exists. Once the Namespace exists — a reinstall, an upgrade — every
object is projected by the API server. §17's project uses the same rule.

**Uninstall does not delete the namespace.** It deletes the objects it created
and leaves the Namespace standing, reporting that it did. A namespace can hold
things the console did not put there, and ``kubectl delete namespace`` is not a
recoverable operation. The operator removes it themselves, having looked.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin import apply as apply_service
from app.admin import preflight
from app.admin import router_bundle
from app.admin.diff import build_diff
from app.admin.router_bundle import (
    MANAGED_BY,
    NAME,
    ROUTER_VERSION,
    VERSION_LABEL,
    BundleObject,
    RouterOptions,
)
from app.audit import recorder
from app.config import settings
from app.errors import AdminError, Conflict, MutationsDisabled, NotFound
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: The label whose value marks an object as this console's to manage.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def enabled_state() -> dict[str, Any]:
    """Whether this deployment permits router management, and the sentence why.

    Reported rather than only enforced, so the UI disables the button *with the
    reason* (rule 11.4). The two gates are separate because they send an
    operator to two different lines of the same file.
    """
    if not settings.admin_allow_mutations:
        return {
            "enabled": False,
            "detail": (
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The install plan and its diff are "
                "still available: reading what would be created is a read."
            ),
        }
    if not settings.router_manage_enabled:
        return {
            "enabled": False,
            "detail": (
                "Managing the shipped router is disabled on this deployment "
                "(ADMIN_ROUTER_MANAGE_ENABLED is off). It has its own gate because "
                "installing it creates a cluster-scoped RBAC grant that can read "
                "every Secret in the cluster, and puts a process on the cluster's "
                "ingress path — a larger commitment than the rest of the write "
                "surface, and one a deployment may reasonably want to withhold "
                "while allowing everything else."
            ),
        }
    return {"enabled": True, "detail": "Router management is enabled on this deployment."}


def _require_enabled(action: str, *, dry_run: bool, namespace: str) -> None:
    """Refuse a real router write before the cluster is touched.

    **Dry runs are permitted**, which is a deliberate difference from §5.5's node
    debug pods, where the projection is itself withheld. The difference is what
    the projection *is*. A node debug pod's manifest is a working recipe for a
    privileged pod on a deployment whose operator switched that feature off. The
    router's manifests are a pinned copy of a public upstream bundle; showing
    them to an operator who is deciding whether to turn the gate on is the point,
    not a leak. So the plan and its diff render on a console where this is off,
    and only the confirming call is refused.
    """
    if dry_run:
        return
    state = enabled_state()
    if state["enabled"]:
        return

    target = {
        "group": "apps", "version": "v1", "resource": "deployments",
        "namespace": namespace, "name": NAME,
    }
    error = MutationsDisabled(
        f"Router {action} is disabled on this console.",
        detail=state["detail"],
        hint=(
            "Set ADMIN_ALLOW_MUTATIONS=true and ADMIN_ROUTER_MANAGE_ENABLED=true "
            "to allow it. Both are required; the second exists so this one "
            "feature can be withheld while every other write stays available."
        ),
        context={**target, "verb": "create", "action": action},
    )
    # Audited as a denial, for the same reason the funnel audits its own gate
    # refusal: somebody attempting to install a cluster-wide ingress controller
    # on a console where that is switched off is a fact worth keeping, and the
    # funnel — which audits everything else — is never reached.
    recorder.record(
        verb="create",
        target=target,
        dry_run=dry_run,
        outcome="denied",
        detail=f"router {action} refused (feature disabled)",
        error=f"{error.code}: {error.message}",
    )
    logger.warning(
        "Refused router %s: %s", action,
        "ADMIN_ALLOW_MUTATIONS is false" if not settings.admin_allow_mutations
        else "ADMIN_ROUTER_MANAGE_ENABLED is false",
    )
    raise error


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #

def _managed_by_us(obj: Any) -> bool:
    """Whether this object carries the console's managed-by label."""
    labels = get_field(obj, "metadata", "labels", default={}) or {}
    return labels.get(MANAGED_BY_LABEL) == MANAGED_BY


def _read_live(item: BundleObject) -> dict[str, Any] | None:
    """The object as it exists now, or ``None`` if it does not.

    Only ``NotFound`` becomes ``None``. Every other failure propagates: a
    forbidden read, or an API server that did not answer, must not be turned
    into "it is not there" — that would have the installer create an object that
    already exists, or report a takeover as a fresh install.
    """
    try:
        return reader.get_resource(
            item.group, item.version, item.plural, item.name, namespace=item.namespace,
        )
    except NotFound:
        return None


def _refuse_takeover(
    item: BundleObject, live: dict[str, Any], *, dry_run: bool,
) -> Conflict:
    """409 naming the object the console will not adopt, and an audit row saying so.

    Audited for the same reason the mutations gate audits its own refusal:
    somebody attempting to install a router over an object they do not own is
    exactly the event the trail exists to hold, and the funnel — which records
    everything else — is never reached, because this fires before the first
    ``mutate()``. Recorded on a dry run too, with ``dry_run`` set honestly: the
    attempt happened either way, and a trail that held only the confirmed ones
    could not answer "did anyone try".
    """
    owner = (get_field(live, "metadata", "labels", default={}) or {}).get(
        MANAGED_BY_LABEL
    )
    where = f"{item.namespace}/{item.name}" if item.namespace else item.name
    error = Conflict(
        f"A {item.kind} called {where} already exists and this console did not create it.",
        detail=(
            "It does not carry the label "
            f"{MANAGED_BY_LABEL}={MANAGED_BY}"
            + (f" (it says {owner!r})." if owner else ".")
            + " Overwriting it would take over an object something else is using."
        ),
        hint=(
            "Install into a different namespace, or delete that object yourself "
            "if it is left over from an earlier install."
        ),
        context={
            "group": item.group, "version": item.version, "resource": item.plural,
            "namespace": item.namespace, "name": item.name, "kind": item.kind,
        },
    )
    # Never raises — see `app.audit`. A failed INSERT must not turn a refusal
    # into a 500, which would tell the operator the console is broken rather
    # than that it declined to take over their object.
    recorder.record(
        verb="create",
        target={
            "group": item.group, "version": item.version, "resource": item.plural,
            "namespace": item.namespace, "name": item.name,
        },
        dry_run=dry_run,
        outcome="conflict",
        detail=(
            f"router install refused: {item.kind} {where} is not managed by "
            "this console"
        ),
        error=f"{error.code}: {error.message}",
    )
    return error


def _check_ownership(
    objects: list[BundleObject], *, dry_run: bool,
) -> list[dict[str, Any]]:
    """Read every object first and refuse the whole install if any is not ours.

    All of them, before any of them is written. A per-object check inside the
    apply loop would create the first five objects and *then* discover that the
    sixth belongs to somebody else, leaving a half-install behind for a refusal
    that was knowable up front.
    """
    existing: list[dict[str, Any]] = []
    for item in objects:
        live = _read_live(item)
        if live is not None and not _managed_by_us(live):
            raise _refuse_takeover(item, live, dry_run=dry_run)
        existing.append(
            {
                "kind": item.kind,
                "name": item.name,
                "namespace": item.namespace,
                "exists": live is not None,
                "resourceVersion": (
                    get_field(live, "metadata", "resourceVersion") if live else None
                ),
                "version": (
                    (get_field(live, "metadata", "labels", default={}) or {}).get(
                        VERSION_LABEL
                    )
                    if live else None
                ),
            }
        )
    return existing



#: What the API server says when it refuses a ClusterRole write because the
#: writer does not itself hold the permissions it is trying to grant.
#:
#: Matched as a substring of the API server's own detail, which this codebase
#: normally forbids — ``from_api_exception`` maps by status precisely because
#: reason strings are not stable. The narrowness is what makes it safe here: it
#: is used **only** to replace the ``hint``, never the code, never the status,
#: and a miss degrades to the ordinary rbac_denied hint rather than to a wrong
#: one.
#:
#: Copied verbatim (lowercased) from a live API server, not paraphrased. The
#: first version of this constant read "attempt to grant extra privileges",
#: which no Kubernetes since 1.9 emits — so the marker never matched, the hint
#: never fired, and the operator was sent to grant `create clusterroles`, the
#: one verb the preflight had just confirmed they hold. The test that was
#: supposed to catch that asserted against the same invented string, so it
#: passed. Check any change to this line against `kubectl create --as=<sa>
#: --dry-run=server` output rather than against memory.
_ESCALATION_MARKER = "attempting to grant rbac permissions not currently held"


def _escalation_hint(item: BundleObject, error: AdminError) -> AdminError:
    """Rewrite an rbac_denied hint when RBAC escalation prevention is the cause.

    This is the failure mode preflight cannot see coming, and it is worth the
    special case. Kubernetes refuses to let a subject create a ClusterRole
    granting permissions it does not itself hold — a rule enforced at admission,
    not by RBAC verbs. So ``SelfSubjectAccessReview`` is asked "may I create
    clusterroles?", answers *yes*, the preflight passes, and the create then
    fails 403.

    Left alone, the operator is told they cannot ``create clusterroles`` — a verb
    they demonstrably hold, since the review said so — and goes off to grant it
    again. Naming escalation prevention instead points them at the two grants
    that actually fix it.
    """
    if error.code != "rbac_denied":
        return error
    if item.kind not in {"ClusterRole", "ClusterRoleBinding"}:
        return error
    if _ESCALATION_MARKER not in str(error.detail or "").lower():
        return error

    error.hint = (
        "This is RBAC escalation prevention, not a missing verb: the API server "
        "refuses to let an identity create a role granting permissions it does "
        "not itself hold. The console's ServiceAccount can create ClusterRoles — "
        "the preflight confirmed it — but not one that reads Secrets cluster-wide "
        "unless it either holds that itself or is granted `escalate` on "
        "rbac.authorization.k8s.io/clusterroles (and `bind` for the "
        "ClusterRoleBinding). deploy/rbac.yaml grants both."
    )
    return error


def _immutable_class_hint(item: BundleObject, error: AdminError) -> AdminError:
    """Say how to get past an IngressClass whose ``spec.controller`` cannot change.

    ``spec.controller`` is immutable, so a cluster carrying a router installed
    before the controller string was corrected cannot be upgraded in place: the
    API server refuses the update and the install stops at seven of eight. The
    verbatim detail names the field, which is accurate and not actionable —
    "field is immutable" does not tell an operator that the fix is to delete one
    object and install again, nor what deleting it costs.

    It costs something real, so it is stated rather than glossed: while the class
    is absent, Ingresses naming it are unclaimed. On a cluster whose router was
    never admitting them that changes nothing, which is the situation any cluster
    hitting this message is in — but an operator should hear that from the
    console rather than have to work it out.

    The code stays ``invalid``: the API server refused the object, and that is
    what the frontend branches on.
    """
    if item.kind != "IngressClass" or "immutable" not in str(error.detail or "").lower():
        return error

    error.message = (
        f"The IngressClass {item.name} already exists with a different "
        f"spec.controller, and that field cannot be changed after creation."
    )
    error.hint = (
        f"Delete it and install again: kubectl delete ingressclass {item.name}. "
        "Ingresses naming this class are unclaimed until the install recreates "
        "it — on a router that was not admitting them, that changes nothing."
    )
    return error


def _dependency_hint(item: BundleObject, error: AdminError, *, failed_kinds: set[str]) -> AdminError:
    """Say the ClusterRoleBinding was not created because its ClusterRole was not.

    The API server answers a ClusterRoleBinding create whose ``roleRef`` names a
    missing ClusterRole with a **404 that names the binding** —
    ``clusterrolebindings.rbac.authorization.k8s.io "x" not found`` — whenever
    the caller does not hold ``bind``. That is the API server's own wording and
    ``from_api_exception`` maps it correctly by status; relayed unchanged it
    still reads as "the binding you asked about does not exist", which is wrong
    twice over: nothing was asked to exist, this was a create, and the object
    that is actually missing is the ClusterRole one line above in the same
    report. An operator who believes it goes to inspect a binding that is fine.

    Only the message and hint are rewritten. The code stays ``not_found`` —
    that is what the cluster answered, and inventing a different one here would
    be this module deciding it knows better than the API server about a call it
    did not make.
    """
    if item.kind != "ClusterRoleBinding" or "ClusterRole" not in failed_kinds:
        return error

    error.message = (
        f"Not created: the ClusterRole {item.name} failed earlier in this same "
        f"install, and a ClusterRoleBinding cannot reference a role that does "
        f"not exist."
    )
    error.hint = (
        "This object has no problem of its own — fix the ClusterRole failure "
        "reported above and install again. The 404 names this binding because "
        "that is how the API server words a missing roleRef, not because the "
        "console found this binding missing."
    )
    return error


# --------------------------------------------------------------------------- #
# Applying one object
# --------------------------------------------------------------------------- #

def _apply_object(
    item: BundleObject, live_version: str | None, *, dry_run: bool, action: str,
) -> dict[str, Any]:
    """Create or replace one bundle object, through the generic apply path.

    Delegates to :func:`app.admin.apply.create_from_yaml` and
    :func:`app.admin.apply.update_from_yaml` rather than calling
    :func:`app.admin.mutate.mutate` directly. Those two already carry the
    document/URL agreement check, the namespace resolution, the verb check and
    — for the replace — rule 4's optimistic concurrency, and re-implementing any
    of that here would be a second write path for exactly the objects that most
    need the first one.
    """
    document = reader.to_yaml(item.body)
    detail = (
        f"router {action}: {item.kind} "
        f"{item.namespace + '/' if item.namespace else ''}{item.name} "
        f"({router_bundle.ROUTER_VERSION})"
    )
    if live_version is None:
        return apply_service.create_from_yaml(
            item.group, item.version, item.plural, item.namespace, document, dry_run,
            detail=detail,
        )
    return apply_service.update_from_yaml(
        item.group, item.version, item.plural, item.namespace, item.name,
        document, live_version, dry_run, detail=detail,
    )


def _outcome(
    item: BundleObject, *, verb: str, result: dict[str, Any] | None,
    error: AdminError | None, projection: str | None = None,
    preflight_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One entry in the per-object report.

    ``applied`` is copied from the funnel's own answer and never derived here:
    §1.5 makes it the only evidence a cluster changed, and a second place that
    computed it would eventually compute it differently.

    ``projection`` says whose diff this is: ``server`` when the API server
    projected it, ``rendered`` when it is the bundle's own manifest because the
    namespace did not yet exist to project into, ``None`` when there is no diff
    at all. A UI labels the two differently — a rendered manifest has not been
    through admission.
    """
    if projection is None and result is not None and result.get("diff") is not None:
        projection = "server"
    return {
        "kind": item.kind,
        "name": item.name,
        "namespace": item.namespace,
        "group": item.group,
        "resource": item.plural,
        "verb": verb,
        "applied": bool(result and result.get("applied")),
        "diff": (result or {}).get("diff"),
        "projection": projection,
        "preflight": preflight_result,
        "auditId": (result or {}).get("auditId"),
        "error": None if error is None else {
            "code": error.code, "message": error.message, "detail": error.detail,
            "hint": error.hint,
        },
    }


def _rendered_outcome(item: BundleObject) -> dict[str, Any]:
    """The dry-run report for an object the API server cannot project yet.

    The manifest diffed against nothing — nothing can exist inside a namespace
    that does not — and a preflight of the ``create`` the real install will
    use: the one thing about such an object that *can* be checked before its
    namespace exists, and the one most worth knowing first. No audit row: no
    request reached the cluster for this object.
    """
    check = preflight.check(
        "create", item.group, item.plural, namespace=item.namespace, name=item.name,
    )
    return _outcome(
        item,
        verb="create",
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


# --------------------------------------------------------------------------- #
# Install and upgrade
# --------------------------------------------------------------------------- #

def install(payload: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """§14 ``POST /api/router`` — install or upgrade the shipped router.

    One call covers both. There is no separate upgrade verb because there is no
    difference in what happens: every object is written to the version this
    console ships, and each one is a create if it is absent and a replace if it
    is already ours. A distinct "upgrade" endpoint would be the same eight writes
    behind a name that implied a migration path this has none of.

    **The optimistic concurrency here is weaker than §13's, on purpose and worth
    knowing.** The ``resourceVersion`` a replace carries is the one read by the
    ownership scan moments earlier, not one the operator was shown and approved.
    Rule 4 is satisfied — the API server still enforces it, and the scan-to-apply
    window is closed — but the "fresh diff against the object you were looking
    at" property that §13's editor has is absent, because there is no editor
    here: the content is the bundle, and it is the same bytes whoever approved
    it. §14 says so rather than implying the §13 guarantee.

    Returns the §14 install response: a per-object report, an aggregate
    ``installed``, and the count that did not land. On a dry run into a
    namespace that does not exist yet, the objects inside it carry
    ``projection: "rendered"`` and a preflight rather than a server projection;
    see the module docstring and :func:`_rendered_outcome`.
    """
    options = router_bundle.validate_options(payload)
    _require_enabled("install", dry_run=dry_run, namespace=options.namespace)

    objects = router_bundle.build(options)
    # Before anything is written, and on a dry run too: a takeover the operator
    # would hit at confirm time is a takeover they should have been told about
    # at preview time.
    existing = _check_ownership(objects, dry_run=dry_run)

    results: list[dict[str, Any]] = []
    failed = 0
    # Which kinds have already failed in THIS install, so a later object whose
    # failure is only a consequence of an earlier one can say so instead of
    # reporting a cause it does not have.
    failed_kinds: set[str] = set()
    # Whether the Namespace exists decides what a dry run can show for the
    # objects inside it. See the module docstring: the API server cannot
    # project into a namespace that is not there, and a dry run that asked it
    # to reported four not_found failures on every fresh install.
    namespace_exists = next(
        (bool(prior["exists"]) for item, prior in zip(objects, existing)
         if item.kind == "Namespace"),
        True,
    )
    for item, prior in zip(objects, existing):
        verb = "update" if prior["exists"] else "create"
        if dry_run and item.namespace is not None and not namespace_exists:
            results.append(_rendered_outcome(item))
            continue
        try:
            result = _apply_object(
                item, prior["resourceVersion"], dry_run=dry_run, action="install",
            )
        except AdminError as e:
            failed += 1
            # Preflight cannot see escalation prevention coming; this is where
            # the hint stops sending the operator to grant a verb they hold.
            error = _escalation_hint(item, e)
            error = _dependency_hint(item, error, failed_kinds=failed_kinds)
            error = _immutable_class_hint(item, error)
            failed_kinds.add(item.kind)
            results.append(_outcome(item, verb=verb, result=None, error=error))
            # Keep going. Stopping at the first failure would leave the operator
            # with one error and no idea whether the other seven would also
            # fail — which is the difference between "grant this one verb" and
            # "this whole install is not going to work here".
            logger.warning(
                "Router install: %s %s failed: %s", verb, item.kind, e.message,
            )
            continue
        results.append(_outcome(item, verb=verb, result=result, error=None))

    return {
        "dryRun": dry_run,
        # False whenever anything failed, and false on every dry run. A dry run
        # that reported `installed: true` would be the §1.5 mistake with a
        # cluster's ingress path attached to it.
        "installed": (not dry_run) and failed == 0,
        "failed": failed,
        "version": ROUTER_VERSION,
        "namespace": options.namespace,
        "ingressClassName": options.ingress_class_name,
        "objects": results,
        "serves": _serves(),
        "options": _options_payload(options),
    }


def uninstall(payload: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """§14 ``DELETE /api/router`` — remove the objects this console created.

    Reverse install order, and **the Namespace is left standing**. Deleting a
    namespace deletes everything in it, including whatever else an operator has
    put there since, and it is not recoverable. What is deleted is reported
    object by object; what is retained is reported too, with the reason, so
    "uninstalled" never means more than it did.

    Objects that are not ours are skipped, not deleted — the same rule as
    install, applied in the direction where getting it wrong is worse.
    """
    # Discover where it actually is rather than trusting the caller. A stale UI
    # (or a hand-written request) naming another controller's IngressClass would
    # otherwise reach `_read_live` on that object. The managed-by check below
    # still refuses to delete it — but not asking about it in the first place is
    # better than being saved by the second line of defence.
    discovered: list[dict[str, Any]] = []
    installed_namespace = _discover_namespace(discovered)
    resolved = dict(payload)
    if installed_namespace and not payload.get("namespace"):
        resolved["namespace"] = installed_namespace

    options = router_bundle.validate_options(resolved)
    _require_enabled("uninstall", dry_run=dry_run, namespace=options.namespace)

    objects = [
        item for item in reversed(router_bundle.build(options))
        if item.kind != "Namespace"
    ]

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed = 0
    removed = 0

    for item in objects:
        live = _read_live(item)
        if live is None:
            skipped.append({
                "kind": item.kind, "name": item.name, "namespace": item.namespace,
                "reason": "It is not on the cluster.",
            })
            continue
        if not _managed_by_us(live):
            skipped.append({
                "kind": item.kind, "name": item.name, "namespace": item.namespace,
                "reason": (
                    f"It does not carry {MANAGED_BY_LABEL}={MANAGED_BY}, so this "
                    "console did not create it and will not delete it."
                ),
            })
            continue
        try:
            result = apply_service.delete_resource(
                item.group, item.version, item.plural, item.namespace, item.name,
                "Background", dry_run,
                detail=(
                    f"router uninstall: {item.kind} "
                    f"{item.namespace + '/' if item.namespace else ''}{item.name}"
                ),
            )
        except AdminError as e:
            failed += 1
            results.append(_outcome(item, verb="delete", result=None, error=e))
            continue
        removed += 1
        results.append(_outcome(item, verb="delete", result=result, error=None))

    return {
        "dryRun": dry_run,
        "namespace": options.namespace,
        "namespaceDiscovered": installed_namespace is not None,
        "removed": removed,
        "failed": failed,
        # Never "uninstalled: true" while anything failed, and never on a dry run.
        "uninstalled": (not dry_run) and failed == 0,
        "objects": results,
        "skipped": skipped,
        "retained": [
            {
                "kind": "Namespace",
                "name": options.namespace,
                "reason": (
                    "Deleting a namespace deletes everything in it, including "
                    "anything put there since the router was installed, and it "
                    "cannot be undone. Remove it yourself once you have looked."
                ),
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def _serves() -> list[dict[str, Any]]:
    """What the shipped router does and does not serve, as facts.

    Reported from the console rather than inferred from the cluster, because it
    is a property of the software, not of the installation. It is here so the
    Routes screen can say "the router this console installs will not serve an
    HTTPRoute" *before* an operator writes one and waits for a Gateway that is
    never going to accept it.
    """
    return [
        {
            "backend": "ingress",
            "served": True,
            "detail": (
                "The shipped router is an Ingress controller. Exposures written "
                "as Ingresses with its class are served by it."
            ),
        },
        {
            "backend": "gateway",
            "served": False,
            "detail": (
                "The HAProxy Kubernetes Ingress Controller implements Gateway API "
                "for TCPRoute only — HTTPRoute is not implemented. Enabling the "
                "Gateway API option grants it the permissions and the controller "
                "name, and it still will not accept an HTTPRoute. Serving those "
                "needs a Gateway API implementation such as Envoy Gateway, Istio "
                "or Cilium, which this console does not install."
            ),
        },
        {
            "backend": "openshift",
            "served": False,
            "detail": (
                "Routes are served by OpenShift's own router, which an OpenShift "
                "cluster already runs. Installing this one alongside it would put "
                "two proxies in contention for the same hostnames."
            ),
        },
    ]


def _options_payload(options: RouterOptions) -> dict[str, Any]:
    return {
        "namespace": options.namespace,
        "serviceType": options.service_type,
        "replicas": options.replicas,
        "ingressClassName": options.ingress_class_name,
        "defaultClass": options.default_class,
        "gatewayApi": options.gateway_api,
        "image": options.image,
    }


def _discover_namespace(unavailable: list[dict[str, Any]]) -> str | None:
    """Where the router is installed, read off the cluster rather than stored.

    The ClusterRoleBinding is the one object that both has a fixed cluster-scoped
    name and records the namespace — its subject's. That is what lets this
    console be stateless about its own router: "where did I install it" is a live
    read, not a row in a database that can disagree with the cluster.

    ``None`` means either "not installed" or "we could not look", and the two are
    kept apart by ``unavailable``: a caller that finds ``None`` with an entry in
    the sink must not report "not installed".
    """
    binding: dict[str, Any] | None = None
    with collect(unavailable, "rbac.authorization.k8s.io", "clusterrolebindings"):
        try:
            binding = reader.get_resource(
                "rbac.authorization.k8s.io", "v1", "clusterrolebindings", NAME,
            )
        except NotFound:
            binding = None
    if binding is None:
        return None
    for subject in get_field(binding, "subjects", default=[]) or []:
        namespace = get_field(subject, "namespace")
        if namespace:
            return str(namespace)
    return None


def _deployment_state(
    namespace: str, unavailable: list[dict[str, Any]],
) -> dict[str, Any]:
    """The router Deployment's live state.

    Every count is ``None`` rather than ``0`` when the read failed. A router
    reported as "0 of 2 replicas ready" when in fact nobody could look is the
    §0.1 corollary applied to the one workload whose health decides whether the
    cluster is reachable from outside — and "0 ready" is what sends an operator
    to restart a router that is fine.
    """
    before = len(unavailable)
    deployment: dict[str, Any] | None = None
    with collect(unavailable, "apps", "deployments", namespace=namespace):
        try:
            deployment = reader.get_resource(
                "apps", "v1", "deployments", NAME, namespace=namespace,
            )
        except NotFound:
            deployment = None
    # `present` is a tri-state for the same reason `installed` is. A read that
    # failed and an object that is genuinely absent both leave `deployment` at
    # None, and reporting both as `present: false` would have the UI render
    # "Deployment: absent" from a read nobody could make.
    unreadable = len(unavailable) > before

    if deployment is None:
        return {
            "present": None if unreadable else False,
            "version": None,
            "desiredReplicas": None,
            "readyReplicas": None,
            "image": None,
            # Null, not false. This seeds a form that writes on confirm, so
            # "we could not look" must not arrive there as "it is off".
            "gatewayApi": None,
            "detail": (
                "The router Deployment could not be read, so whether it is there "
                "is unknown — not absent."
                if unreadable else None
            ),
        }

    labels = get_field(deployment, "metadata", "labels", default={}) or {}
    containers = get_field(deployment, "spec", "template", "spec", "containers", default=[]) or []
    generation = get_field(deployment, "metadata", "generation")
    observed = get_field(deployment, "status", "observedGeneration")

    # The §6 staleness rule, applied here too: a controller that has not written
    # a status for the current generation has told us nothing about the current
    # generation. Reporting last generation's ready count beside this
    # generation's spec is how an upgrade looks finished while the old pods are
    # still the only ones running.
    stale = generation is not None and observed is not None and observed < generation
    return {
        "present": True,
        "version": labels.get(VERSION_LABEL),
        "desiredReplicas": get_field(deployment, "spec", "replicas"),
        "readyReplicas": (
            None if stale else get_field(deployment, "status", "readyReplicas", default=0)
        ),
        "image": get_field(containers[0], "image") if containers else None,
        # Which optional features the *installed* router was given, read back off
        # its own arguments rather than remembered. The reinstall form seeds
        # itself from these: without them it offers the bundle's defaults over a
        # router installed with different ones, and an operator taking a version
        # bump silently turns off whatever they had switched on. `defaultClass`
        # is not here because it is already visible as `ingressClass.default`,
        # which is the object that actually carries it.
        "gatewayApi": (
            any(
                str(a).startswith("--gateway-controller-name=")
                for a in (get_field(containers[0], "args", default=[]) or [])
            )
            if containers else None
        ),
        "detail": (
            "The Deployment controller has not yet reported on the current "
            f"generation ({observed} of {generation}), so how many replicas are "
            "ready is not known yet."
            if stale else None
        ),
    }


def _service_state(namespace: str, unavailable: list[dict[str, Any]]) -> dict[str, Any]:
    """The router Service, and the address traffic actually arrives on.

    ``addresses`` empty on a ``LoadBalancer`` Service means the cloud provider
    has not assigned one yet — a real, temporary and common state — and it is
    reported as empty with that sentence rather than as "no address", which
    reads as a failure.
    """
    before = len(unavailable)
    service: dict[str, Any] | None = None
    with collect(unavailable, "", "services", namespace=namespace):
        try:
            service = reader.get_resource("", "v1", "services", NAME, namespace=namespace)
        except NotFound:
            service = None
    unreadable = len(unavailable) > before

    if service is None:
        return {
            "present": None if unreadable else False,
            "type": None,
            # `[]` here is "there is no Service, so it publishes nothing"; on the
            # unreadable branch the `present: None` beside it is what says the
            # empty list is not evidence of anything.
            "addresses": [],
            "nodePorts": [],
            "detail": (
                "The router Service could not be read, so whether it is there is "
                "unknown — not absent."
                if unreadable else None
            ),
        }

    addresses: list[str] = []
    for entry in get_field(service, "status", "loadBalancer", "ingress", default=[]) or []:
        value = get_field(entry, "hostname") or get_field(entry, "ip")
        if value and str(value) not in addresses:
            addresses.append(str(value))

    service_type = get_field(service, "spec", "type")
    node_ports = [
        {"name": get_field(p, "name"), "port": get_field(p, "nodePort")}
        for p in get_field(service, "spec", "ports", default=[]) or []
        if get_field(p, "nodePort")
    ]

    detail = None
    if service_type == "LoadBalancer" and not addresses:
        detail = (
            "The Service is a LoadBalancer and no address has been published for "
            "it yet. On a cluster with no load-balancer provider this stays empty "
            "permanently, and the router is unreachable from outside."
        )
    return {
        "present": True,
        "type": service_type,
        "addresses": addresses,
        "nodePorts": node_ports,
        "detail": detail,
    }


def _ingress_class_state(
    options_class: str, unavailable: list[dict[str, Any]],
) -> dict[str, Any]:
    """The IngressClass, and whether it is the cluster default."""
    before = len(unavailable)
    obj: dict[str, Any] | None = None
    with collect(unavailable, "networking.k8s.io", "ingressclasses"):
        try:
            obj = reader.get_resource(
                "networking.k8s.io", "v1", "ingressclasses", options_class,
            )
        except NotFound:
            obj = None
    unreadable = len(unavailable) > before
    if obj is None:
        return {
            "present": None if unreadable else False,
            "name": options_class,
            "default": None,
            "controller": None,
        }
    annotations = get_field(obj, "metadata", "annotations", default={}) or {}
    return {
        "present": True,
        "name": options_class,
        "default": annotations.get("ingressclass.kubernetes.io/is-default-class") == "true",
        "controller": get_field(obj, "spec", "controller"),
    }



#: Controllers whose upstream project has been retired, keyed on the exact
#: ``spec.controller`` string an IngressClass carries.
#:
#: Exact, never a substring of "nginx". ``kubernetes/ingress-nginx`` (retired
#: March 2026), F5's NGINX Ingress Controller and F5's NGINX Gateway Fabric are
#: three different products, and telling an operator their supported, actively
#: released controller is retired is the confidently-wrong answer this console
#: exists to avoid — pointed, this time, at their whole ingress path.
RETIRED_CONTROLLERS: dict[str, str] = {
    "k8s.io/ingress-nginx": (
        "The kubernetes/ingress-nginx project was retired in March 2026: no "
        "further releases, no bugfixes, and no fixes for security issues found "
        "after that date. Its intended successor, InGate, was retired before it "
        "shipped. This is F5's NGINX Ingress Controller only if spec.controller "
        "says so — it does not here."
    ),
}


def _other_classes(
    unavailable: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Every IngressClass on the cluster, so an install is not offered blind.

    ``None``, never ``[]``, when the listing failed. The two are the answer to
    different questions and the caller renders them differently: an empty list
    is "nothing else is serving Ingresses here, this router will be the only
    one", and that is a sentence worth being sure about before an operator
    installs a second proxy beside a first.
    """
    listing: dict[str, Any] | None = None
    with collect(unavailable, "networking.k8s.io", "ingressclasses"):
        listing = reader.list_resource(
            "networking.k8s.io", "v1", "ingressclasses", limit=200,
        )
    if listing is None:
        return None

    classes: list[dict[str, Any]] = []
    for obj in listing["items"]:
        controller = get_field(obj, "spec", "controller")
        annotations = get_field(obj, "metadata", "annotations", default={}) or {}
        classes.append({
            "name": get_field(obj, "metadata", "name"),
            "controller": controller,
            "default": annotations.get(
                "ingressclass.kubernetes.io/is-default-class"
            ) == "true",
            "managedByUs": _managed_by_us(obj),
            "retired": RETIRED_CONTROLLERS.get(str(controller or "")),
        })
    return classes


def status(namespace: str | None = None) -> dict[str, Any]:
    """§14 ``GET /api/router`` — what is on the cluster right now.

    A live read, every time. Nothing here is cached and nothing is remembered
    between calls: the console's answer to "is the router installed" is whatever
    the cluster says this second, which is the same promise every other page in
    this product makes.

    ``installed`` is a tri-state. ``True`` and ``False`` mean what they say;
    ``None`` means one of the reads failed and the question is open. A console
    that reported ``False`` during an API outage would invite an operator to
    install a second router on top of the one already running.
    """
    unavailable: list[dict[str, Any]] = []
    gate = enabled_state()

    discovered = _discover_namespace(unavailable)
    blind_on_discovery = bool(unavailable)
    target_namespace = namespace or discovered or settings.router_namespace

    deployment = _deployment_state(target_namespace, unavailable)
    service = _service_state(target_namespace, unavailable)
    ingress_class = _ingress_class_state(
        router_bundle.INGRESS_CLASS_NAME, unavailable,
    )
    other_classes = _other_classes(unavailable)

    if unavailable:
        installed: bool | None = None
    elif blind_on_discovery:
        installed = None
    else:
        installed = bool(deployment["present"])

    installed_version = deployment["version"]
    return {
        "enabled": gate["enabled"],
        "enabledDetail": gate["detail"],
        # Tri-state; see the docstring. Never coerced to a boolean.
        "installed": installed,
        "namespace": target_namespace,
        "namespaceDiscovered": discovered is not None,
        "shippedVersion": ROUTER_VERSION,
        "installedVersion": installed_version,
        # True when the cluster is running something this console does not ship —
        # newer *or* older. The UI needs the difference to avoid calling a
        # downgrade an upgrade.
        "versionMatches": (
            None if installed_version is None else installed_version == ROUTER_VERSION
        ),
        # "The installed version differs from the one this console ships" —
        # which is all this console can honestly assert. It is NOT "a newer
        # version of HAProxy exists": that would be a claim about a third party's
        # release history, made from a string baked into this repo, and it would
        # be wrong in both directions — stale after upstream releases, and
        # backwards when a cluster runs something newer than this console knows.
        #
        # Null, not false, when the installed version could not be read: "no
        # upgrade available" is a claim an unreadable Deployment does not
        # support.
        "upgradeAvailable": (
            None if installed_version is None
            else installed_version != ROUTER_VERSION
        ),
        "deployment": deployment,
        "service": service,
        "ingressClass": ingress_class,
        # Null when the listing failed, never []. "Nothing else is serving
        # Ingresses here" is what decides whether installing this router puts a
        # second proxy beside a first, and it is not a claim an unreadable
        # listing supports.
        "otherClasses": other_classes,
        "serves": _serves(),
        "image": router_bundle.ROUTER_IMAGE,
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


def plan(payload: dict[str, Any]) -> dict[str, Any]:
    """§14 ``POST /api/router/plan`` — the manifests, without touching the cluster.

    Pure and ungated. What this console would install is not a secret — it is a
    pinned copy of a public upstream bundle — and an operator deciding whether to
    turn ``ADMIN_ROUTER_MANAGE_ENABLED`` on has to be able to read it first.
    Nothing here is preflighted or audited, because nothing happens.
    """
    options = router_bundle.validate_options(payload)
    objects = router_bundle.build(options)
    return {
        "version": ROUTER_VERSION,
        "image": options.image,
        "options": _options_payload(options),
        "serves": _serves(),
        "objects": [
            {
                "kind": item.kind,
                "name": item.name,
                "namespace": item.namespace,
                "group": item.group,
                "resource": item.plural,
                "yaml": reader.to_yaml(item.body),
            }
            for item in objects
        ],
    }


__all__ = [
    "MANAGED_BY_LABEL",
    "RETIRED_CONTROLLERS",
    "enabled_state",
    "install",
    "plan",
    "status",
    "uninstall",
]
