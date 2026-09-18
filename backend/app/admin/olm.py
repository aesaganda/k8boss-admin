"""
Installing Operator Lifecycle Manager (§33).

This is the module that lets k8boss-admin put a cluster's *operator control
plane* on it. Read :mod:`app.admin.olm_bundle` first for what gets installed and
where the bytes come from, and `docs/adr-0008-shipped-olm.md` for why a second
shipped bundle exists at all after ADR-0004 said there would never be one. This
module is about how, and the how is what keeps the console honest about the size
of what it just did.

**There is no controller here.** Install is a sequence of ordinary writes, one
per object, each through :func:`app.admin.mutate.mutate` — the same gate,
preflight, dry run, diff and audit row as a scale. Twenty-six objects, twenty-six
preflights, twenty-six diffs, twenty-six audit rows. Nothing in this process
watches OLM afterwards; :func:`status` is a live read like every other page.
What keeps OLM running is the Kubernetes control plane, and what installs
operators is OLM.

## The one wait, and why it is not a reconcile loop

Between the two phases the install waits for the eight CustomResourceDefinitions
to report ``Established``. That is the only place in this codebase that waits on
a cluster, and it is worth being precise about what it is: **one bounded wait
inside one request**, the same thing ``kubectl wait --for=condition=Established``
does, bounded by ``ADMIN_OLM_ESTABLISH_TIMEOUT_SECONDS`` and then abandoned. It
holds no state, survives nothing, retries nothing, and corrects no drift. If the
wait expires the install stops and says so — it does **not** write phase two,
because phase two into a cluster whose CRDs are not established is eighteen
``no matches for kind`` failures that describe the wait's timeout and name
neither it nor anything an operator can act on.

## `installed` is not `working`, and this module never conflates them

The strongest thing :func:`install` can honestly return is that twenty-six
objects were accepted by the API server. It is not that OLM works. Two steps
happen afterwards that belong to OLM and not to this console:

* The two Deployments have to schedule and become Ready.
* The ``packageserver`` ClusterServiceVersion has to be reconciled by the
  olm-operator, which registers ``v1.packages.operators.coreos.com`` as an
  aggregated APIService. Until that succeeds, §16's portal reads exactly the
  empty state it read before the install — the one that says this cluster does
  not serve PackageManifests — and it will be *right*.

So :func:`install` returns ``installed`` for "every object was accepted" and a
separate ``ready`` block that is always ``null`` on the install response, with
the sentence saying where to look. :func:`status` is the endpoint that answers
whether OLM is actually up, because that answer is a live read and cannot be
known at the moment the last object is created. A response that said
``installed: true`` and let a UI render "Operator Lifecycle Manager installed" is
this project's defect standard aimed at the thing the console just did.

## The console never adopts an OLM it did not install

Every object carries ``app.kubernetes.io/managed-by: k8boss-admin``, and an
install that finds *any* of the twenty-six already present without that label
refuses — before writing anything, on a dry run as much as on a real one, and
naming every conflicting object rather than the first. Naming all of them is a
deliberate difference from §14's router, and the reason is the shape of the
failure here: a cluster that already runs OLM collides on most of the twenty-six
at once, and reporting them one per attempt would have an operator rerun the
install a dozen times to learn a fact that was knowable on the first read. What
the refusal is protecting against is not subtle — writing OLM 0.35.0's
Deployments over a running OLM 0.30 would restart a cluster's entire operator
control plane, and every operator OLM manages with it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.admin import apply as apply_service
from app.admin import olm_bundle
from app.admin import preflight
from app.admin.diff import build_diff
from app.admin.mutate import FeatureGate, Switch, read_only_switch, require_open
from app.admin.olm_bundle import (
    MANAGED_BY,
    MANAGED_BY_LABEL,
    OLM_VERSION,
    PHASE_CORE,
    PHASE_CRDS,
    BundleObject,
    OLMOptions,
)
from app.audit import recorder
from app.config import settings
from app.errors import AdminError, Conflict, Invalid, NotFound, Unsupported
from app.k8s.client import get_clients
from app.resources import catalog as catalog_module
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import get_field
from app.services import portal as portal_service

logger = logging.getLogger(__name__)

#: Consequence codes. Closed set, like §16's: the frontend renders one checkbox
#: per entry and :func:`install` refuses a real write unless every code present
#: is acknowledged by name, so adding one is a contract change rather than a new
#: string.
#:
#: Two of the three are unconditional, which §16's are not. That is not an
#: oversight — they are what installing OLM *is*, rather than findings about a
#: particular cluster, and the one an operator most needs to have read is the one
#: that is always there.
WARN_CLUSTER_ADMIN_GRANT = "cluster_admin_grant"
WARN_CRD_OWNERSHIP = "crd_ownership"
WARN_COMMUNITY_CATALOG = "community_catalog"

#: How often the establishment wait re-reads. Not configurable: the timeout is
#: the knob that matters, and a poll interval an operator could set to 0.01 is a
#: way to hammer an API server from a settings file.
_POLL_INTERVAL_SECONDS = 1.0


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate(action: str) -> FeatureGate:
    """The two switches in front of an OLM install, in the funnel's terms.

    **Neither withholds the dry run**, for the same reason §14's and §16's do
    not: what the projection shows is a pinned copy of a public upstream release
    that anyone can download, and an operator deciding whether to set
    ``ADMIN_OLM_INSTALL_ENABLED`` has to be able to read the ClusterRole first.
    Withholding it would mean the decision to grant ``*`` on ``*`` gets made
    without the object in front of the person making it — the opposite of what
    the gate is for. §5.5's node debug pods withhold their projection because the
    projection is itself a working recipe for a privileged pod; nothing here is
    secret.
    """
    return FeatureGate(
        feature=f"OLM {action}",
        message="Installing Operator Lifecycle Manager is switched off on this console.",
        hint=(
            "Set ADMIN_ALLOW_MUTATIONS=true and ADMIN_OLM_INSTALL_ENABLED=true. "
            "Both are required, and the second is separate from "
            "ADMIN_PORTAL_INSTALL_ENABLED on purpose: subscribing writes one "
            "object into an API the cluster already serves, and this creates the "
            "API. The plan and its diff are readable without either."
        ),
        switches=(
            read_only_switch(detail=(
                "This deployment is read-only: ADMIN_ALLOW_MUTATIONS is off, so "
                "no write reaches any cluster from here. The install plan, the "
                "manifests and their diff are still available: reading what "
                "would be created is a read."
            )),
            Switch(
                "ADMIN_OLM_INSTALL_ENABLED", settings.olm_install_enabled,
                detail=(
                    "Installing Operator Lifecycle Manager is switched off on "
                    "this deployment (ADMIN_OLM_INSTALL_ENABLED is not set). It "
                    "has its own gate because an install creates eight "
                    "cluster-scoped CustomResourceDefinitions and a ClusterRole "
                    "granting every verb on every resource in the cluster, "
                    "including escalate and bind — the widest grant this console "
                    "can create. Browsing the portal and reading what an install "
                    "would create are unaffected."
                ),
            ),
        ),
        enabled_detail=(
            "This deployment permits installing Operator Lifecycle Manager."
        ),
    )


def enabled_state() -> dict[str, Any]:
    """Whether this deployment permits installing OLM, and the sentence why.

    Reported rather than only enforced, so the portal's empty state disables the
    button *with the reason* (rule 11.4) instead of offering it and answering
    with a 403.
    """
    return _gate("install").state()


def _require_open(action: str, *, dry_run: bool) -> None:
    """Refuse a real install before anything is read or built.

    Step one of the funnel, hoisted. The row is written against the olm-operator
    Deployment — the object the bundle exists to create — because an install
    that waited for the first :func:`mutate` would produce twenty-six denial
    rows and twenty-six failed objects for one attempt.
    """
    require_open(
        _gate(action),
        verb="create", group="apps", version="v1", plural="deployments",
        namespace=olm_bundle.OLM_NAMESPACE, name=olm_bundle.OLM_DEPLOYMENTS[0],
        dry_run=dry_run,
        detail=f"OLM {action} refused (switched off)",
        context={"action": action, "version": OLM_VERSION},
    )


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def _consequences(options: OLMOptions) -> list[dict[str, Any]]:
    """What an operator is consenting to, in the words the refusal will use.

    Each entry is acknowledged by code before a real install proceeds. The
    wording is deliberately not softened: ``*`` on ``*`` with ``escalate`` is
    what upstream's manifest says, and paraphrasing it as "broad permissions"
    would be this console making a grant sound smaller than the object in the
    diff two lines below.
    """
    entries: list[dict[str, Any]] = [
        {
            "code": WARN_CLUSTER_ADMIN_GRANT,
            "label": "This grants OLM full control of the cluster",
            "consequence": (
                "The ClusterRole system:controller:operator-lifecycle-manager "
                "grants watch, list, get, create, update, patch, delete, "
                "deletecollection, escalate and bind on every resource in every "
                "API group, plus every verb on all non-resource URLs. That is "
                "not a summary — it is apiGroups: ['*'], resources: ['*'] "
                "verbatim from upstream's manifest — and it is bound to the "
                "olm-operator-serviceaccount in the olm namespace. `escalate` "
                "means OLM can grant permissions nobody has given it."
            ),
            "mitigation": (
                "There is no narrower version of this to choose. It is inherent "
                "to OLM rather than a choice made here: OLM installs operators "
                "that ask for arbitrary permissions, so it has to hold them. "
                "Read the ClusterRole in the objects below before confirming — "
                "it is the widest grant this console can create anywhere, wider "
                "than §14's router. If that is more than this cluster should "
                "hand to a controller, the answer is not to install OLM."
            ),
        },
        {
            "code": WARN_CRD_OWNERSHIP,
            "label": "Eight CustomResourceDefinitions become part of the cluster's API",
            "consequence": (
                "Subscriptions, ClusterServiceVersions, InstallPlans, "
                "CatalogSources, OperatorGroups, Operators, OperatorConditions "
                "and OLMConfigs are added cluster-wide. Removing them later is "
                "not the reverse of this install: deleting a CRD deletes every "
                "custom resource made from it, across every namespace, with no "
                "further confirmation."
            ),
            "mitigation": (
                "This console does not offer that removal — §33 installs and "
                "does not uninstall — so removing OLM later is a deliberate "
                "sequence of deletions through the resource browser, each with "
                "its own diff."
            ),
        },
    ]
    if options.community_catalog:
        entries.append({
            "code": WARN_COMMUNITY_CATALOG,
            "label": "The cluster will pull and trust a community catalog",
            "consequence": (
                f"The optional CatalogSource pulls {olm_bundle.COMMUNITY_CATALOG_IMAGE} "
                "— an unpinned tag, re-polled hourly — and every package it "
                "lists becomes installable from this console's portal. Nobody "
                "here reviews that catalog's contents, and they can change "
                "between two page loads without anything in this cluster "
                "changing."
            ),
            "mitigation": (
                "Leave it off. An OLM with no CatalogSource is a working OLM "
                "with an empty catalog, and you can then add a CatalogSource "
                "you have chosen — a private mirror, or the same one pinned to "
                "a digest."
            ),
        })
    return entries


def _notes() -> list[dict[str, Any]]:
    """Facts about this install that are not consent decisions.

    Separate from :func:`_consequences` on purpose. A checkbox means "I accept
    this outcome"; none of these is an outcome the operator can accept or
    decline, and mixing them in would train people to tick four boxes to get
    past two that mattered.

    The shape differs too, and deliberately: a consequence carries
    ``consequence``/``mitigation``, which is what ``ConsequenceChecklist``
    renders beside a checkbox, and a note carries a plain ``detail``. A note that
    arrived in the consequence shape would be one refactor away from being
    dropped into that component and acquiring a checkbox nobody meant it to have.
    """
    return [
        {
            "code": "installed_is_not_running",
            "label": "A successful install is not a running OLM",
            "detail": (
                "A successful install means twenty-six objects were accepted by "
                "the API server. It does not mean OLM is running: the two "
                "Deployments still have to become Ready, and the packageserver "
                "ClusterServiceVersion still has to be reconciled by OLM itself "
                "before packages.operators.coreos.com exists. Until that "
                "happens the portal reads the same empty state it reads now, "
                "and it is not wrong to. Reload the portal to see it change."
            ),
        },
        {
            "code": "preflight_cannot_see_escalation",
            "label": "The preflight cannot tell you whether the ClusterRole write will be accepted",
            "detail": (
                "The SelfSubjectAccessReview in front of the ClusterRole write "
                "will answer yes whether or not the write can succeed. "
                "Kubernetes refuses to let an identity create a role granting "
                "permissions it does not itself hold, and that is enforced at "
                "admission rather than by verb matching, so no review can "
                "report it. If the install fails there, the error says so "
                "rather than sending you to grant a verb the review just "
                "confirmed you have."
            ),
        },
        {
            "code": "no_uninstall_and_no_upgrade",
            "label": "There is no uninstall here, and no upgrade",
            "detail": (
                f"This console installs OLM {OLM_VERSION} and does nothing else "
                "with it. There is no uninstall — removing OLM means deleting "
                "its CRDs, which deletes every operator's Subscription and "
                "ClusterServiceVersion with them — and no upgrade: a later OLM "
                "is installed with kubectl from upstream's release, the same "
                "way this one could have been. The console reports which "
                "version is running and whether it matches the one shipped "
                "here, and stops there."
            ),
        },
        {
            "code": "network_policies",
            "label": "Upstream ships five NetworkPolicies with this",
            "detail": (
                "Upstream's manifest includes five NetworkPolicies: a "
                "default-deny in the olm namespace with three narrow allowances "
                "for OLM's own components, and an allow-all in the operators "
                "namespace. They constrain OLM's namespace, not yours, and they "
                "do nothing at all unless the cluster's CNI plugin enforces "
                "NetworkPolicy."
            ),
        },
    ]


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a real install whose consequences the caller has not accepted by name.

    Named rather than boolean, copied from §16 and §13 for the reason given
    there: a caller that acknowledged one list and then turned the community
    catalog on has to acknowledge the new one. Extra tokens are accepted;
    missing ones are not.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "Installing OLM has consequences that have not been acknowledged.",
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
# Ownership
# --------------------------------------------------------------------------- #

def _managed_by_us(obj: Any) -> bool:
    """Whether this object carries the console's managed-by label."""
    labels = get_field(obj, "metadata", "labels", default={}) or {}
    return labels.get(MANAGED_BY_LABEL) == MANAGED_BY


def _read_live(item: BundleObject) -> dict[str, Any] | None:
    """The object as it exists now, or ``None`` if it does not.

    Two failures become ``None``, and both are genuinely "it is not there":

    * ``NotFound`` — the API is served and has no such object.
    * ``Unsupported`` — the cluster does not serve that API **at all**, so no
      object of that kind can exist on it. This is not a swallowed error, it is
      the only sound reading: you cannot have an OperatorGroup on a cluster with
      no OperatorGroup CRD.

    Everything else propagates. Turning a forbidden read or an unanswered API
    server into "it is not there" would have the installer write over a running
    OLM and report it as a fresh install, which is the one outcome this whole
    scan exists to prevent.

    **The ``Unsupported`` case was missed and it broke the feature outright.**
    An earlier version of this docstring asserted that a missing API arrives as
    ``NotFound``; it does not — ``resolve()`` raises ``unsupported`` (501) before
    any request is made. Since the scan reads all twenty-six objects before
    writing any, and eleven of them are ``operators.coreos.com`` kinds whose CRDs
    phase one has not created yet, every install on every cluster with no OLM —
    which is the entire population §33 exists for — failed here with a 501 naming
    ``olmconfigs``, before touching anything. No unit test caught it because the
    fake stubs discovery with those CRDs already present. It took a real cluster.
    """
    try:
        return reader.get_resource(
            item.group, item.version, item.plural, item.name, namespace=item.namespace,
        )
    except (NotFound, Unsupported):
        return None


def _refuse_takeover(
    conflicts: list[dict[str, Any]], *, dry_run: bool,
) -> Conflict:
    """409 naming every object the console will not adopt, plus an audit row.

    Audited for the same reason the mutations gate audits its own refusal:
    somebody attempting to install OLM over an OLM they did not install here is
    exactly the event the trail exists to hold, and the funnel — which records
    everything else — is never reached, because this fires before the first
    :func:`mutate`. Recorded on a dry run too, with ``dry_run`` set honestly.
    """
    shown = ", ".join(f"{c['kind']} {c['where']}" for c in conflicts[:4])
    if len(conflicts) > 4:
        shown += f", and {len(conflicts) - 4} more"
    error = Conflict(
        f"{len(conflicts)} of this bundle's objects already exist and this "
        "console did not create them.",
        detail=(
            f"{shown}. None carries the label {MANAGED_BY_LABEL}={MANAGED_BY}. "
            "On a cluster that already runs Operator Lifecycle Manager this is "
            "what you should see: installing over it would replace a running "
            "operator control plane, restarting every operator it manages."
        ),
        hint=(
            "If OLM is already installed, there is nothing to do — the portal "
            "reads it either way, and this console does not manage an OLM it "
            "did not install. If these are remnants of a removed installation, "
            "delete them yourself and install again."
        ),
        context={
            "conflicts": conflicts,
            "shippedVersion": OLM_VERSION,
        },
    )
    # Never raises — see `app.audit`. A failed INSERT must not turn a refusal
    # into a 500, which would tell the operator the console is broken rather
    # than that it declined to overwrite their OLM.
    recorder.record(
        verb="create",
        target={
            "group": "apps", "version": "v1", "resource": "deployments",
            "namespace": olm_bundle.OLM_NAMESPACE,
            "name": olm_bundle.OLM_DEPLOYMENTS[0],
        },
        dry_run=dry_run,
        outcome="conflict",
        detail=(
            f"OLM install refused: {len(conflicts)} objects are not managed by "
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
    apply loop would create eight CRDs and *then* discover that the olm-operator
    Deployment belongs to a running OLM — leaving the cluster with this console's
    CRDs over somebody else's control plane, for a refusal that was knowable up
    front.
    """
    existing: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for item in objects:
        live = _read_live(item)
        if live is not None and not _managed_by_us(live):
            owner = (get_field(live, "metadata", "labels", default={}) or {}).get(
                MANAGED_BY_LABEL
            )
            conflicts.append({
                "kind": item.kind, "name": item.name, "namespace": item.namespace,
                "where": item.where, "managedBy": owner,
            })
        existing.append({
            "kind": item.kind,
            "name": item.name,
            "namespace": item.namespace,
            "exists": live is not None,
            "resourceVersion": (
                get_field(live, "metadata", "resourceVersion") if live else None
            ),
        })
    if conflicts:
        raise _refuse_takeover(conflicts, dry_run=dry_run)
    return existing


#: What the API server says when it refuses a ClusterRole write because the
#: writer does not itself hold the permissions it is trying to grant.
#:
#: The same constant as §14's, and the same narrowness rule: matched as a
#: substring of the API server's own detail, used **only** to replace the
#: ``hint``, never the code and never the status, so a miss degrades to the
#: ordinary rbac_denied hint rather than to a wrong one. Kept as its own name
#: here rather than imported from :mod:`app.admin.router` so that §14 remains
#: removable without taking §33 with it — ADR-0004 and ADR-0008 both promise
#: their feature is a clean excision.
_ESCALATION_MARKER = "attempting to grant rbac permissions not currently held"


def _escalation_hint(item: BundleObject, error: AdminError) -> AdminError:
    """Rewrite an rbac_denied hint when RBAC escalation prevention is the cause.

    §14 documents this failure as one preflight cannot see coming. Here it is not
    an edge case but the expected outcome on most clusters: the ClusterRole being
    created grants ``*`` on ``*``, so the API server accepts it only from a
    caller that either holds all of that already or holds ``escalate``. A console
    ServiceAccount scoped to what this product documents holds neither.

    Left alone, the operator is told they cannot ``create clusterroles`` — a verb
    the review just confirmed they hold — and goes to grant it again.
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
        "not itself hold. OLM's ClusterRole grants every verb on every resource, "
        "so creating it requires the console's identity to hold cluster-admin "
        "itself, or to hold `escalate` on rbac.authorization.k8s.io/clusterroles "
        "(and `bind` for the ClusterRoleBinding). deploy/rbac.yaml grants both, "
        "in a role that is not bound by default — see docs/rbac.md, which says "
        "what binding it means."
    )
    return error


def _dependency_hint(
    item: BundleObject, error: AdminError, *, failed_kinds: set[str],
) -> AdminError:
    """Say an object was not created because something it depends on was not.

    Two dependencies inside this bundle produce errors that name the wrong
    object. A ClusterRoleBinding whose roleRef names a missing ClusterRole comes
    back as a 404 naming *the binding* whenever the caller lacks ``bind``, and
    anything in the ``olm`` namespace comes back as a 404 naming the object when
    what is actually missing is the Namespace. Relayed unchanged, both read as
    "the thing you asked about does not exist" — wrong twice over, because
    nothing was asked to exist and the object that is missing is one the same
    report already lists as failed.

    Only the message and hint are rewritten. The code stays as the API server
    set it.
    """
    if item.kind == "ClusterRoleBinding" and "ClusterRole" in failed_kinds:
        error.message = (
            "Not created: the ClusterRole this binding references failed "
            "earlier in this same install."
        )
        error.hint = (
            "This object has no problem of its own — fix the ClusterRole failure "
            "reported above and install again. The 404 names this binding "
            "because that is how the API server words a missing roleRef."
        )
        return error
    if item.namespace is not None and "Namespace" in failed_kinds:
        error.message = (
            f"Not created: the {item.namespace} Namespace failed earlier in this "
            f"same install, and nothing can be created inside a namespace that "
            f"does not exist."
        )
        error.hint = (
            "Fix the Namespace failure reported above and install again."
        )
    return error


# --------------------------------------------------------------------------- #
# Applying one object
# --------------------------------------------------------------------------- #

def _apply_object(
    item: BundleObject, live_version: str | None, *, dry_run: bool,
) -> dict[str, Any]:
    """Create or replace one bundle object, through the generic apply path.

    Delegates to :func:`app.admin.apply.create_from_yaml` and
    :func:`app.admin.apply.update_from_yaml` rather than calling
    :func:`app.admin.mutate.mutate` directly, for the reason §14 gives: those two
    already carry the document/URL agreement check, the namespace resolution and
    rule 4's optimistic concurrency, and re-implementing any of it here would be
    a second write path for exactly the objects that most need the first one.

    A replace only ever happens over an object this console installed — the
    ownership scan refuses everything else — so it is a re-run of an install,
    not an upgrade of somebody's OLM.
    """
    document = reader.to_yaml(item.body)
    detail = f"OLM install: {item.kind} {item.where} ({OLM_VERSION})"
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
    skipped: str | None = None,
) -> dict[str, Any]:
    """One entry in the per-object report.

    ``applied`` is copied from the funnel's own answer and never derived here:
    §1.5 makes it the only evidence a cluster changed, and a second place that
    computed it would eventually compute it differently.

    ``projection`` says whose diff this is: ``server`` when the API server
    projected it, ``rendered`` when it is the bundle's own manifest because the
    API or the namespace it needs does not exist yet, ``None`` when there is no
    diff. A UI labels the two differently — a rendered manifest has not been
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
        "phase": item.phase,
        "verb": verb,
        "applied": bool(result and result.get("applied")),
        "diff": (result or {}).get("diff"),
        "projection": projection,
        "preflight": preflight_result,
        "skipped": skipped,
        "auditId": (result or {}).get("auditId"),
        "error": None if error is None else {
            "code": error.code, "message": error.message, "detail": error.detail,
            "hint": error.hint,
        },
    }


def _rendered_outcome(item: BundleObject, *, reason: str) -> dict[str, Any]:
    """The dry-run report for an object the API server cannot project yet.

    The manifest diffed against nothing, plus a preflight of the ``create`` the
    real install will use — the one thing about such an object that *can* be
    checked before its API exists, and the one most worth knowing first. No audit
    row: no request reached the cluster for this object.

    This is the normal case for most of phase two on a fresh cluster, and the
    reason is worth stating rather than leaving as a bare label. Eleven of these
    objects are ``operators.coreos.com`` kinds whose CRDs phase one has not
    created yet, and the API server answers ``dryRun=All`` against a kind it does
    not serve with a 404 — so a dry run that asked would report eleven
    ``not_found`` failures on every cluster this feature exists for.

    The preflight is best-effort for the same reason: reviewing ``create`` on a
    resource discovery has never heard of can itself fail, and a failed review is
    reported as a null verdict rather than as a denial. §0.2's rule — a review
    that could not be evaluated is not a refusal — applied to a review that could
    not be made at all.
    """
    try:
        check = preflight.check(
            "create", item.group, item.plural,
            namespace=item.namespace, name=item.name,
        )
        preflight_result = {
            "allowed": check.get("allowed"),
            "reason": check.get("reason"),
            "evaluationError": check.get("evaluationError"),
            "hint": check.get("hint"),
        }
    except AdminError as e:
        preflight_result = {
            "allowed": None,
            "reason": None,
            "evaluationError": (
                f"The access review could not be made: {e.message} This is "
                "expected for a resource whose CustomResourceDefinition does "
                "not exist yet, and is not a denial."
            ),
            "hint": None,
        }
    return _outcome(
        item,
        verb="create",
        result={"applied": False, "diff": build_diff(None, item.body), "auditId": None},
        error=None,
        projection="rendered",
        preflight_result=preflight_result,
        skipped=reason,
    )


# --------------------------------------------------------------------------- #
# The wait between the phases
# --------------------------------------------------------------------------- #

def _established(name: str) -> bool | None:
    """Whether one CRD reports ``Established``, or ``None`` if we could not tell.

    Tri-state, and the ``None`` is load-bearing: a read that failed must not be
    counted as "not established yet", which would spend the whole timeout on a
    cluster that was answering perfectly well about everything else, nor as
    established, which would send phase two at an API that is not ready.
    """
    try:
        crd = reader.get_resource(
            "apiextensions.k8s.io", "v1", "customresourcedefinitions", name,
        )
    except NotFound:
        return False
    except AdminError:
        return None
    for condition in get_field(crd, "status", "conditions", default=[]) or []:
        if get_field(condition, "type") == "Established":
            return get_field(condition, "status") == "True"
    # Present, with no Established condition written yet. That is the ordinary
    # first moment of a CRD's life, not a failure.
    return False


def _await_established(names: list[str]) -> dict[str, Any]:
    """Wait for phase one's CRDs, bounded, then invalidate discovery.

    The only wait in this codebase; see the module docstring for why it is not a
    reconcile loop. Returns a report rather than raising, because a timeout here
    is not an error in the install — the CRDs were created, and what did not
    happen is that they became usable within the budget. The install reports
    that and stops.

    Discovery is invalidated on success because the console's own catalog caches
    what a cluster serves for up to a minute (:mod:`app.resources.catalog`), and
    phase two addresses five ``operators.coreos.com`` kinds that were not in it
    when this request started. Without this, phase two fails with ``unsupported``
    against APIs that exist — the console disbelieving a cluster on the strength
    of a cache it filled itself, moments earlier.
    """
    deadline = time.monotonic() + settings.olm_establish_timeout_seconds
    pending = list(names)
    unreadable: list[str] = []
    while True:
        unreadable = []
        still_pending: list[str] = []
        for name in pending:
            state = _established(name)
            if state is None:
                unreadable.append(name)
                still_pending.append(name)
            elif not state:
                still_pending.append(name)
        pending = still_pending
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    if not pending:
        # Only on success. Dropping the cache after a timeout would advertise
        # APIs that are not established, which is the same wrong answer one
        # layer down.
        catalog_module.invalidate_cache(get_clients().cluster_id)
        return {"established": True, "pending": [], "unreadable": [], "detail": None}

    return {
        "established": False,
        "pending": pending,
        "unreadable": unreadable,
        "detail": (
            f"{len(pending)} of {len(names)} CustomResourceDefinitions had not "
            f"reported Established after "
            f"{settings.olm_establish_timeout_seconds:.0f}s"
            + (
                f", and {len(unreadable)} of those could not be read at all — "
                "so whether they are established is unknown, not no"
                if unreadable else ""
            )
            + ". The CRDs were created; the rest of the install was not "
            "attempted, because writing OLM's own objects into APIs that are "
            "not established yet fails with errors that describe this timeout "
            "and name something else. Re-run the install: the objects that "
            "already exist are this console's and will be re-applied, not "
            "refused."
        ),
    }


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

def install(
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """§33 ``POST /api/portal/olm`` — install Operator Lifecycle Manager.

    Twenty-six ordinary writes in two phases with one bounded wait between them.
    Returns the §33 install response: a per-object report, an aggregate
    ``installed`` that means "every object was accepted" and never "OLM works",
    and the ``crds`` wait report.

    On a dry run nothing is waited for and phase two's objects are reported with
    ``projection: "rendered"`` — see :func:`_rendered_outcome`. That is not a
    degraded preview: it is the honest one, because on a cluster with no OLM the
    API server genuinely cannot project an OperatorGroup.
    """
    options = olm_bundle.validate_options(payload)
    _require_open("install", dry_run=dry_run)

    consequences = _consequences(options)
    if not dry_run:
        _require_acknowledgement(consequences, acknowledge_consequences)

    objects = olm_bundle.build(options)
    # Before anything is written, and on a dry run too: an install that would
    # replace somebody's OLM is something to be told at preview time.
    existing = _check_ownership(objects, dry_run=dry_run)
    prior = {(item.kind, item.where): state for item, state in zip(objects, existing)}

    results: list[dict[str, Any]] = []
    failed = 0
    failed_kinds: set[str] = set()
    crd_report: dict[str, Any] | None = None

    for phase in (PHASE_CRDS, PHASE_CORE):
        phase_objects = [item for item in objects if item.phase == phase]

        if phase == PHASE_CORE:
            if dry_run:
                # Nothing was created, so nothing in phase two can be projected:
                # its APIs do not exist and neither do its namespaces. Every
                # object is rendered, with the reason.
                results.extend(
                    _rendered_outcome(item, reason=(
                        "Phase two is projected from the bundle rather than by "
                        "the API server: on a dry run the CustomResourceDefinitions "
                        "and namespaces it needs have not been created."
                    ))
                    for item in phase_objects
                )
                continue
            if failed:
                # Phase one did not fully land. Sending phase two anyway produces
                # a second wave of failures that all describe the first one.
                results.extend(
                    _outcome(item, verb="create", result=None, error=None, skipped=(
                        "Not attempted: one or more CustomResourceDefinitions "
                        "failed in phase one, and OLM's own objects cannot be "
                        "created until their APIs exist."
                    ))
                    for item in phase_objects
                )
                continue
            crd_report = _await_established(olm_bundle.crd_names())
            if not crd_report["established"]:
                results.extend(
                    _outcome(item, verb="create", result=None, error=None,
                             skipped=crd_report["detail"])
                    for item in phase_objects
                )
                continue

        for item in phase_objects:
            state = prior[(item.kind, item.where)]
            verb = "update" if state["exists"] else "create"
            try:
                result = _apply_object(item, state["resourceVersion"], dry_run=dry_run)
            except AdminError as e:
                failed += 1
                error = _escalation_hint(item, e)
                error = _dependency_hint(item, error, failed_kinds=failed_kinds)
                failed_kinds.add(item.kind)
                results.append(_outcome(item, verb=verb, result=None, error=error))
                # Keep going within the phase. Stopping at the first failure
                # would leave the operator with one error and no idea whether
                # the other twenty-five would also fail — the difference between
                # "grant this one verb" and "this install is not going to work
                # on this cluster".
                logger.warning(
                    "OLM install: %s %s %s failed: %s",
                    verb, item.kind, item.where, e.message,
                )
                continue
            results.append(_outcome(item, verb=verb, result=result, error=None))

    skipped = sum(1 for entry in results if entry["skipped"] and not dry_run)
    return {
        "dryRun": dry_run,
        # False whenever anything failed or was skipped, and false on every dry
        # run. It means twenty-six objects were accepted — see `ready` for what
        # it does not mean.
        "installed": (not dry_run) and failed == 0 and skipped == 0,
        "failed": failed,
        "skipped": skipped,
        "version": OLM_VERSION,
        "namespaces": list(olm_bundle.NAMESPACES),
        "communityCatalog": options.community_catalog,
        "objects": results,
        "crds": crd_report,
        "consequences": consequences,
        "notes": _notes(),
        # Always null on this response, and that is the point. Whether OLM is
        # running is a live read that cannot be true at the moment the last
        # object is created, so this endpoint refuses to guess and names the one
        # that can answer.
        "ready": None,
        "readyDetail": (
            "Whether OLM is running is not knowable from this response. The "
            "Deployments have to become Ready and OLM has to reconcile the "
            "packageserver ClusterServiceVersion before packages.operators."
            "coreos.com exists. GET /api/portal/olm answers that, as a live read."
        ),
    }


def plan(payload: dict[str, Any]) -> dict[str, Any]:
    """§33 ``POST /api/portal/olm/plan`` — the manifests, without touching the cluster.

    Pure and ungated. What this console would install is not a secret — it is a
    pinned copy of a public upstream release, and its SHA-256 is in this repo so
    anyone can check that claim — and an operator deciding whether to turn
    ``ADMIN_OLM_INSTALL_ENABLED`` on has to be able to read the ClusterRole
    first. Nothing here is preflighted or audited, because nothing happens.

    ``yaml`` is included per object, and for the eight CRDs that is 1.1 MiB of
    it. Included anyway: the alternative is a console that asks somebody to
    approve an object it will not show them.
    """
    options = olm_bundle.validate_options(payload)
    objects = olm_bundle.build(options)
    return {
        "version": OLM_VERSION,
        "upstream": olm_bundle.UPSTREAM_RELEASE,
        "digests": dict(olm_bundle.FILE_DIGESTS),
        "communityCatalog": options.community_catalog,
        "communityCatalogImage": olm_bundle.COMMUNITY_CATALOG_IMAGE,
        "namespaces": list(olm_bundle.NAMESPACES),
        "consequences": _consequences(options),
        "notes": _notes(),
        "phases": [
            {
                "phase": PHASE_CRDS,
                "detail": (
                    "Eight CustomResourceDefinitions. The install waits for "
                    "each to report Established before phase two, because "
                    "phase two's objects are instances of these kinds."
                ),
            },
            {
                "phase": PHASE_CORE,
                "detail": (
                    "OLM itself: two namespaces, five NetworkPolicies, the "
                    "ServiceAccount, the ClusterRole and its binding, three "
                    "aggregated roles, two Deployments, the OLMConfig, two "
                    "OperatorGroups and the packageserver ClusterServiceVersion."
                ),
            },
        ],
        "objects": [
            {
                "kind": item.kind,
                "name": item.name,
                "namespace": item.namespace,
                "group": item.group,
                "resource": item.plural,
                "phase": item.phase,
                "yaml": reader.to_yaml(item.body),
            }
            for item in objects
        ],
    }


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def _crd_state(unavailable: list[dict[str, Any]]) -> dict[str, Any]:
    """How many of the eight CRDs exist and are Established.

    ``None`` rather than ``0`` when the read did not happen, per §0.1's
    corollary: a console reporting "0 of 8 established" from a read nobody could
    make is what invites a second install on top of a working one.
    """
    expected = olm_bundle.crd_names()
    found: dict[str, Any] = {}
    # Eight gets by name, not a listing of every CustomResourceDefinition on the
    # cluster. A cluster with a handful of operators on it serves more than one
    # page of CRDs, and a first page that does not happen to hold OLM's reports
    # all eight missing — the portal saying "not installed" about a cluster where
    # OLM is running, which is the answer that invites a second install on top of
    # a working one. `NotFound` is a real absence; anything else is a read that
    # did not happen and leaves the whole block unknown.
    with collect(
        unavailable, "apiextensions.k8s.io", "customresourcedefinitions",
    ) as collected:
        for name in expected:
            try:
                found[name] = reader.get_resource(
                    "apiextensions.k8s.io", "v1", "customresourcedefinitions", name,
                )
            except NotFound:
                continue
    if collected.failed:
        return {
            "expected": len(expected),
            "present": None,
            "established": None,
            "missing": None,
            "detail": (
                "CustomResourceDefinitions could not be read, so how many of "
                "OLM's are present is unknown — not zero."
            ),
        }
    established = [
        name for name, obj in found.items()
        if any(
            get_field(c, "type") == "Established" and get_field(c, "status") == "True"
            for c in get_field(obj, "status", "conditions", default=[]) or []
        )
    ]
    return {
        "expected": len(expected),
        "present": len(found),
        "established": len(established),
        "missing": [name for name in expected if name not in found],
        "detail": None,
    }


def _deployment_state(
    name: str, unavailable: list[dict[str, Any]],
) -> dict[str, Any]:
    """One OLM Deployment's live state.

    Every count is ``None`` rather than ``0`` when the read failed or the
    controller has not reported on the current generation. "0 of 1 ready" is what
    sends an operator to debug an OLM that is fine, or to reinstall over one that
    is starting.
    """
    before = len(unavailable)
    deployment: dict[str, Any] | None = None
    with collect(unavailable, "apps", "deployments", namespace=olm_bundle.OLM_NAMESPACE):
        try:
            deployment = reader.get_resource(
                "apps", "v1", "deployments", name, namespace=olm_bundle.OLM_NAMESPACE,
            )
        except NotFound:
            deployment = None
    unreadable = len(unavailable) > before

    if deployment is None:
        return {
            "name": name,
            "present": None if unreadable else False,
            "desiredReplicas": None,
            "readyReplicas": None,
            "image": None,
            "managedByUs": None,
            "detail": (
                f"The {name} Deployment could not be read, so whether it is "
                "there is unknown — not absent."
                if unreadable else None
            ),
        }

    containers = get_field(
        deployment, "spec", "template", "spec", "containers", default=[]
    ) or []
    generation = get_field(deployment, "metadata", "generation")
    observed = get_field(deployment, "status", "observedGeneration")
    stale = generation is not None and observed is not None and observed < generation
    return {
        "name": name,
        "present": True,
        "desiredReplicas": get_field(deployment, "spec", "replicas"),
        "readyReplicas": (
            None if stale else get_field(deployment, "status", "readyReplicas", default=0)
        ),
        "image": get_field(containers[0], "image") if containers else None,
        "managedByUs": _managed_by_us(deployment),
        "detail": (
            "The Deployment controller has not yet reported on the current "
            f"generation ({observed} of {generation}), so how many replicas are "
            "ready is not known yet."
            if stale else None
        ),
    }


def _packageserver_state(unavailable: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the thing that makes the portal work is actually working.

    This is the block that keeps ``installed`` honest. The ``packageserver``
    ClusterServiceVersion is created by the install and reconciled by OLM, and
    only when OLM has finished does ``packages.operators.coreos.com`` start
    answering. Between those two moments every object exists, the Deployments are
    Ready, and §16 still reads an empty portal — correctly.

    ``apiAvailable`` is resolved through :mod:`app.services.portal`'s own source
    resolution rather than re-derived, so this endpoint and the portal cannot
    disagree about whether the package server answers.
    """
    csv: dict[str, Any] | None = None
    # `collect` leaves `csv` at None on failure as well as on absence, and the
    # two are different answers — "OLM has not created it yet" versus "we could
    # not look". Its own `failed` flag is what tells them apart; scanning the
    # sink afterwards would also match an entry some earlier read put there.
    with collect(
        unavailable, "operators.coreos.com", "clusterserviceversions",
        namespace=olm_bundle.OLM_NAMESPACE,
    ) as collected:
        try:
            csv = reader.get_resource(
                "operators.coreos.com", "v1alpha1", "clusterserviceversions",
                olm_bundle.PACKAGESERVER_CSV, namespace=olm_bundle.OLM_NAMESPACE,
            )
        except NotFound:
            csv = None

    # **A cluster that does not serve the API is not a cluster we failed to
    # read**, and this is the one endpoint where getting that wrong is
    # guaranteed rather than possible: every cluster §33 exists for serves no
    # `operators.coreos.com`, so the read above raises `unsupported` on all of
    # them. Left in `unavailable`, the status of a perfectly ordinary OLM-less
    # cluster comes back `partial: true` and the panel renders a banner saying it
    # could not answer, directly above four rows that answered correctly — §1.2's
    # "an absent API is not an error" turned into a permanent warning on the
    # normal case, which is how people learn to ignore the banner that matters.
    #
    # So an `unsupported` miss is withdrawn from the sink and read as what it is:
    # the API is not served, therefore the object does not exist, therefore
    # `csvPresent` is a real `False`. §16's own reader draws exactly this line —
    # only `unknown` makes its listing partial — and the two must not disagree
    # about the same cluster. Any other failure stays in the sink and leaves
    # `csvPresent` null.
    unsupported = bool(
        collected.failed and (collected.entry or {}).get("reason") == "unsupported"
    )
    if unsupported and collected.entry in unavailable:
        unavailable.remove(collected.entry)
    csv_readable = (not collected.failed) or unsupported

    state = portal_service.source_state(portal_service.PACKAGES)
    return {
        "csvPresent": (True if csv is not None else (False if csv_readable else None)),
        "phase": get_field(csv, "status", "phase") if csv else None,
        "message": get_field(csv, "status", "message") if csv else None,
        "apiAvailable": (
            True if state.available
            else (False if state.state == portal_service.STATE_UNSUPPORTED else None)
        ),
        "apiDetail": state.detail,
        "detail": (
            "packages.operators.coreos.com is the API the portal reads. It is "
            "served by an aggregated APIService that OLM registers only after it "
            "has reconciled the packageserver ClusterServiceVersion, so a "
            "complete install and an empty portal is an ordinary state for the "
            "minute or two in between."
        ),
    }


def status() -> dict[str, Any]:
    """§33 ``GET /api/portal/olm`` — what is on the cluster right now.

    A live read, every time. Nothing here is cached and nothing is remembered
    between calls.

    ``installed`` is a tri-state. ``True`` and ``False`` mean what they say;
    ``None`` means one of the reads failed and the question is open. A console
    that reported ``False`` during an API outage would invite an operator to
    install OLM on top of the one already running — which the ownership scan
    would refuse, but being saved by the second line of defence is not the same
    as being right.

    ``installed`` means OLM's two Deployments are present. It does not mean OLM
    works; ``packageServer`` is where that is answered, and ``ready`` combines
    the two so a UI does not have to.
    """
    unavailable: list[dict[str, Any]] = []
    gate = enabled_state()

    crds = _crd_state(unavailable)
    deployments = [
        _deployment_state(name, unavailable) for name in olm_bundle.OLM_DEPLOYMENTS
    ]
    package_server = _packageserver_state(unavailable)

    present = [d["present"] for d in deployments]
    if any(p is None for p in present):
        installed: bool | None = None
    else:
        installed = all(present)

    ready: bool | None
    if installed is not True:
        ready = False if installed is False else None
    elif package_server["apiAvailable"] is None:
        ready = None
    else:
        ready = bool(package_server["apiAvailable"]) and all(
            d["readyReplicas"] not in (None, 0) for d in deployments
        )

    managed = [d["managedByUs"] for d in deployments if d["present"]]
    return {
        "enabled": gate["enabled"],
        "enabledDetail": gate["detail"],
        # Tri-state; see the docstring. Never coerced to a boolean.
        "installed": installed,
        # Separately tri-state, and the one a UI should render as "OLM is
        # working". `installed and not ready` is the ordinary state for a minute
        # after an install, and the ordinary state forever on a cluster where
        # OLM cannot schedule.
        "ready": ready,
        "shippedVersion": OLM_VERSION,
        "upstream": olm_bundle.UPSTREAM_RELEASE,
        # Whether the OLM on this cluster is one this console installed. Null
        # when nothing is present to ask about. An OLM installed by anything else
        # is read, reported and never written to — see `_check_ownership`.
        "managedByUs": (None if not managed else all(bool(m) for m in managed)),
        "crds": crds,
        "deployments": deployments,
        "packageServer": package_server,
        "namespaces": list(olm_bundle.NAMESPACES),
        "notes": _notes(),
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


__all__ = [
    "WARN_CLUSTER_ADMIN_GRANT",
    "WARN_COMMUNITY_CATALOG",
    "WARN_CRD_OWNERSHIP",
    "enabled_state",
    "install",
    "plan",
    "status",
]
