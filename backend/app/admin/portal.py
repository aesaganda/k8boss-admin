"""
The one write the operator portal makes (§16): a Subscription.

This module compiles a subscription request into a single ``Subscription``
object and puts it through :func:`app.admin.mutate.mutate` like every other
write in this console — one preflight, one dry run, one diff, one audit row. It
writes nothing else, ever. What installs the operator afterwards is Operator
Lifecycle Manager, which is the cluster's own software; this console ships no
bundle, pins no image and runs no controller.

That distinction is the whole boundary. ``docs/adr-0004-shipped-router.md`` says
k8boss-admin installs one pinned bundle "and nothing else, ever", and §16 does
not add a second: it writes one object into an API the cluster already serves,
exactly as the YAML editor in §4 could. ``docs/adr-0005-operator-portal.md``
records the argument on both sides and what this costs.

**A Subscription is not an installation, and this module refuses to blur the
two.** ``applied: true`` means one object was created. Whether an operator ends
up running depends on things that happen after the write returns, and three of
them are knowable *before* it:

* **The namespace needs exactly one OperatorGroup.** With none, OLM marks the
  ClusterServiceVersion ``Failed`` with ``NoOperatorGroup`` and nothing installs.
  With two, ``TooManyOperatorGroups``, same outcome.
* **The OperatorGroup's scope has to be one the operator supports.** A group
  watching every namespace requires an operator that declares the
  ``AllNamespaces`` install mode; one that does not gets
  ``UnsupportedOperatorGroup``.
* **A ``Manual`` approval strategy stops the install dead** until somebody
  approves the InstallPlan.

Each is reported as a ``consequences[]`` entry that the caller must acknowledge
by name before the write is accepted — the same shape §13 uses for a lossy
exposure, and for the same reason: consenting to a consequence is a separate act
from requesting the change. This is the §14 lesson applied to somebody else's
deployment engine. An exposure written into a cluster with no controller is an
object that routes nothing while looking created; a Subscription written into a
namespace with no OperatorGroup is an object that installs nothing while looking
created.

**Removal is not here, deliberately.** Deleting a Subscription does not
uninstall an operator — the ClusterServiceVersion it created stays, and so does
everything that CSV owns. A "Remove" button that deleted only the Subscription
would report an uninstall that did not happen, which is the one thing this
project's defect standard refuses. The portal says what removing actually takes
and leaves both deletions to §4, where each is its own diff and its own audit
row.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin.mutate import FeatureGate, Switch, read_only_switch, require_open
from app.config import settings
from app.errors import Invalid
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import get_field
from app.services import portal as portal_service

logger = logging.getLogger(__name__)

#: OLM's two approval strategies, exactly as the CRD spells them.
APPROVAL_STRATEGIES = ("Automatic", "Manual")

#: Warning codes. Closed set: the frontend renders one checkbox per entry and
#: the write refuses unless every code present is acknowledged by name, so a new
#: code is a contract change rather than a new string.
WARN_NO_OPERATOR_GROUP = "no_operator_group"
WARN_TOO_MANY_OPERATOR_GROUPS = "too_many_operator_groups"
WARN_OPERATOR_GROUP_UNKNOWN = "operator_group_unknown"
WARN_INSTALL_MODE_UNSUPPORTED = "install_mode_unsupported"
WARN_INSTALL_MODES_UNKNOWN = "install_modes_unknown"
WARN_ALREADY_SUBSCRIBED = "already_subscribed"
WARN_SUBSCRIPTIONS_UNKNOWN = "subscriptions_unknown"
WARN_MANUAL_APPROVAL = "manual_approval"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """The two switches in front of a subscribe, in the funnel's terms.

    **Neither withholds the dry run**, for the same reason §14's plan is
    readable with its gate off: what the projection shows is the caller's own
    request rendered as a Subscription plus the contents of a catalog the
    cluster already publishes, and an operator deciding whether to set
    ``ADMIN_PORTAL_INSTALL_ENABLED`` has to be able to read what it would let
    the console create. Nothing here is privileged — unlike §5.5's node debug
    pods, where the projected pod spec is itself the sensitive thing.
    """
    return FeatureGate(
        feature="subscribing to a catalog operator",
        message="Subscribing to catalog operators is switched off on this deployment.",
        hint=(
            "Set ADMIN_ALLOW_MUTATIONS and ADMIN_PORTAL_INSTALL_ENABLED on the "
            "console deployment. The subscription plan and its diff are readable "
            "without either."
        ),
        switches=(
            read_only_switch(detail=(
                "This deployment is read-only: ADMIN_ALLOW_MUTATIONS is off, so "
                "no write reaches any cluster from here."
            )),
            Switch(
                "ADMIN_PORTAL_INSTALL_ENABLED", settings.portal_install_enabled,
                detail=(
                    "Subscribing is switched off on this deployment: "
                    "ADMIN_PORTAL_INSTALL_ENABLED is not set. Browsing the catalog "
                    "and reading what a subscription would create are unaffected."
                ),
            ),
        ),
        enabled_detail="This deployment permits subscribing to catalog operators.",
    )


def enabled_state() -> dict[str, Any]:
    """Whether this deployment permits subscribing, and why not when it does not.

    Reported rather than only enforced, so the UI can disable the button *with*
    the reason. A greyed control with no explanation is the state rule 11.4
    exists to forbid.
    """
    return _gate().state()


def _require_open(*, dry_run: bool, namespace: str, package: str) -> None:
    """Refuse a real subscribe before the catalog is read.

    Step one of the funnel, hoisted: a caller whose deployment forbids this
    should get ``mutations_disabled`` rather than a 404 about a package name
    they were never going to be allowed to subscribe to. The row is written
    against the Subscription the write would create.
    """
    require_open(
        _gate(),
        verb="create", group="operators.coreos.com", version="v1alpha1",
        plural="subscriptions", namespace=namespace, name=package,
        dry_run=dry_run, detail=f"subscribe to {package} in {namespace}",
    )


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Second validation layer, behind the pydantic body.

    Duplicated deliberately: the service is called by tests and could be called
    by another service, and a validation that lived only in the FastAPI model
    would be a validation those callers skip.
    """
    package = (payload.get("package") or "").strip()
    if not package:
        raise Invalid(
            "No package was named.",
            hint="Pick an operator from the catalog.",
            context={"parameter": "package"},
        )

    namespace = (payload.get("namespace") or "").strip()
    if not namespace:
        raise Invalid(
            "No namespace was named.",
            hint=(
                "A Subscription is namespaced, and which namespace it goes in "
                "decides which OperatorGroup governs the install."
            ),
            context={"parameter": "namespace"},
        )

    approval = payload.get("installPlanApproval") or "Automatic"
    if approval not in APPROVAL_STRATEGIES:
        raise Invalid(
            f"{approval!r} is not an install plan approval strategy.",
            detail=f"OLM accepts {' or '.join(APPROVAL_STRATEGIES)}.",
            context={"parameter": "installPlanApproval", "value": approval},
        )

    return {
        "package": package,
        "namespace": namespace,
        "channel": (payload.get("channel") or "").strip() or None,
        "catalog": (payload.get("catalog") or "").strip() or None,
        "catalogNamespace": (payload.get("catalogNamespace") or "").strip() or None,
        "installPlanApproval": approval,
        "startingCSV": (payload.get("startingCSV") or "").strip() or None,
    }


# --------------------------------------------------------------------------- #
# The target namespace, and whether OLM can act in it
# --------------------------------------------------------------------------- #

def _operator_groups(
    namespace: str, unavailable: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """The OperatorGroups in one namespace, or ``None`` when we could not look.

    ``None`` and ``[]`` are the two answers that must never be confused here.
    ``[]`` means OLM will refuse to install; ``None`` means we do not know
    whether it will, and reporting the second as the first would put a blocking
    warning in front of an operator whose namespace is perfectly configured.
    """
    state = portal_service.source_state(portal_service.OPERATOR_GROUPS)
    if state.state == portal_service.STATE_UNSUPPORTED:
        # OLM's Subscription CRD without its OperatorGroup CRD is not a cluster
        # anyone runs on purpose, but it is a cluster we can be pointed at.
        return None
    if state.state == portal_service.STATE_UNKNOWN and state.error is not None:
        unavailable.append(portal_service.unknown_entry(state, namespace=namespace))
        return None

    version = state.version
    assert version is not None
    listing: dict[str, Any] | None = None
    with collect(
        unavailable,
        portal_service.OPERATOR_GROUPS.group,
        portal_service.OPERATOR_GROUPS.plural,
        namespace=namespace,
    ):
        listing = reader.list_resource(
            portal_service.OPERATOR_GROUPS.group,
            version,
            portal_service.OPERATOR_GROUPS.plural,
            namespace=namespace,
            limit=50,
        )
    if listing is None:
        return None
    return [portal_service.operator_group_row(obj) for obj in listing["items"]]


def _existing_subscriptions(
    namespace: str, package: str, unavailable: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Subscriptions in the namespace that already name this package.

    ``None`` when the listing did not happen. Nothing downstream may read that
    as "there are none": a second Subscription for the same package leaves two
    resolutions competing for the same CRDs, and the console offering to create
    it during an API outage is the console causing that.
    """
    state = portal_service.source_state(portal_service.SUBSCRIPTIONS)
    if not state.available:
        if state.state == portal_service.STATE_UNKNOWN and state.error is not None:
            unavailable.append(portal_service.unknown_entry(state, namespace=namespace))
        return None

    version = state.version
    assert version is not None
    listing: dict[str, Any] | None = None
    with collect(
        unavailable,
        portal_service.SUBSCRIPTIONS.group,
        portal_service.SUBSCRIPTIONS.plural,
        namespace=namespace,
    ):
        listing = reader.list_resource(
            portal_service.SUBSCRIPTIONS.group,
            version,
            portal_service.SUBSCRIPTIONS.plural,
            namespace=namespace,
            limit=500,
        )
    if listing is None:
        return None
    return [
        portal_service.subscription_row(obj, csv_read=False, install_plan_read=False)
        for obj in listing["items"]
        if get_field(obj, "spec", "name") == package
    ]


def _required_install_mode(group: dict[str, Any], namespace: str) -> str | None:
    """Which install mode OLM will demand of a CSV in this OperatorGroup.

    ``None`` when the group selects its namespaces by label: what that set
    contains is whatever the labels match right now, this console does not
    evaluate selectors, and naming a mode from a guess would put a confident
    blocking warning in front of a configuration that is fine.
    """
    if group.get("allNamespaces") is True:
        return "AllNamespaces"
    if group.get("allNamespaces") is None:
        return None
    targets = group.get("targetNamespaces") or []
    if len(targets) > 1:
        return "MultiNamespace"
    if len(targets) == 1:
        return "OwnNamespace" if targets[0] == namespace else "SingleNamespace"
    return None


def target_state(
    namespace: str,
    channel: Any,
    unavailable: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Can OLM install into this namespace, and what will stop it if not.

    Returns the ``target`` payload and the consequences it produced. ``ready`` is a
    tri-state and ``None`` is the common third state — an unreadable
    OperatorGroup listing, a label-selector group, or a catalog that published
    no install modes all leave us unable to say. ``true`` is only ever returned
    when every one of those was actually checked and passed.
    """
    consequences: list[dict[str, Any]] = []
    groups = _operator_groups(namespace, unavailable)

    if groups is None:
        return (
            {
                "namespace": namespace,
                "operatorGroups": None,
                "requiredInstallMode": None,
                "ready": None,
                "detail": (
                    "The OperatorGroups in this namespace could not be read, so "
                    "whether OLM can install here is unknown."
                ),
            },
            [{
                "code": WARN_OPERATOR_GROUP_UNKNOWN,
                "label": "The namespace's OperatorGroup could not be read",
                "consequence": (
                    "OLM installs an operator only into a namespace governed by "
                    "exactly one OperatorGroup, and whether this one has it could "
                    "not be determined. The Subscription will be created either "
                    "way; if the namespace has none, the ClusterServiceVersion "
                    "will fail with NoOperatorGroup and nothing will install."
                ),
                "mitigation": (
                    "Check the namespace's OperatorGroups directly, or retry when "
                    "the API server is answering."
                ),
            }],
        )

    if not groups:
        return (
            {
                "namespace": namespace,
                "operatorGroups": [],
                "requiredInstallMode": None,
                "ready": False,
                "detail": (
                    f"{namespace} has no OperatorGroup. OLM will not install an "
                    "operator into it."
                ),
            },
            [{
                "code": WARN_NO_OPERATOR_GROUP,
                "label": "This namespace has no OperatorGroup",
                "consequence": (
                    "The Subscription will be created and OLM will mark the "
                    "ClusterServiceVersion Failed with NoOperatorGroup. Nothing "
                    "will install, and the operator will keep looking subscribed."
                ),
                "mitigation": (
                    "Create one OperatorGroup in this namespace first, or "
                    "subscribe into a namespace that already has one."
                ),
            }],
        )

    if len(groups) > 1:
        names = ", ".join(str(g.get("name")) for g in groups)
        return (
            {
                "namespace": namespace,
                "operatorGroups": groups,
                "requiredInstallMode": None,
                "ready": False,
                "detail": (
                    f"{namespace} has {len(groups)} OperatorGroups ({names}). OLM "
                    "requires exactly one."
                ),
            },
            [{
                "code": WARN_TOO_MANY_OPERATOR_GROUPS,
                "label": f"This namespace has {len(groups)} OperatorGroups",
                "consequence": (
                    "OLM refuses to act in a namespace governed by more than one "
                    "OperatorGroup: the ClusterServiceVersion will fail with "
                    "TooManyOperatorGroups and nothing will install. Every "
                    "operator already in this namespace is affected too, not "
                    "only this one."
                ),
                "mitigation": f"Delete all but one of {names}.",
            }],
        )

    group = groups[0]
    required = _required_install_mode(group, namespace)
    supported = portal_service.supported_modes(channel)

    if supported is None:
        return (
            {
                "namespace": namespace,
                "operatorGroups": groups,
                "requiredInstallMode": required,
                "ready": None,
                "detail": (
                    "This catalog publishes no install modes for the selected "
                    "channel, so whether the operator can run in this "
                    "OperatorGroup's scope is unknown."
                ),
            },
            [{
                "code": WARN_INSTALL_MODES_UNKNOWN,
                "label": "The catalog publishes no install modes for this channel",
                "consequence": (
                    "Whether this operator supports the scope "
                    f"{group.get('name')} watches could not be checked. If it does "
                    "not, OLM will fail the ClusterServiceVersion with "
                    "UnsupportedOperatorGroup."
                ),
                "mitigation": (
                    "Read the package's ClusterServiceVersion description in the "
                    "API explorer, or pick a channel that publishes one."
                ),
            }],
        )

    if required is None:
        return (
            {
                "namespace": namespace,
                "operatorGroups": groups,
                "requiredInstallMode": None,
                "ready": None,
                "detail": (
                    f"OperatorGroup {group.get('name')} selects its namespaces by "
                    "label. Which namespaces that covers is not evaluated here, so "
                    "the install mode it requires is unknown."
                ),
            },
            [{
                "code": WARN_INSTALL_MODES_UNKNOWN,
                "label": "The OperatorGroup selects namespaces by label",
                "consequence": (
                    "Which namespaces it governs depends on labels this console "
                    "does not evaluate, so the install mode OLM will require of "
                    "this operator could not be checked."
                ),
                "mitigation": (
                    f"Read OperatorGroup {group.get('name')} and confirm the "
                    "operator supports the scope it resolves to."
                ),
            }],
        )

    if required not in supported:
        return (
            {
                "namespace": namespace,
                "operatorGroups": groups,
                "requiredInstallMode": required,
                "ready": False,
                "detail": (
                    f"OperatorGroup {group.get('name')} requires the {required} "
                    f"install mode; this channel supports {', '.join(supported) or 'none'}."
                ),
            },
            [{
                "code": WARN_INSTALL_MODE_UNSUPPORTED,
                "label": f"This operator does not support the {required} install mode",
                "consequence": (
                    f"OperatorGroup {group.get('name')} watches a scope that "
                    f"requires {required}, and this channel declares support for "
                    f"{', '.join(supported) or 'no install mode at all'}. OLM will "
                    "mark the ClusterServiceVersion Failed with "
                    "UnsupportedOperatorGroup and nothing will install."
                ),
                "mitigation": (
                    "Subscribe into a namespace whose OperatorGroup watches a "
                    "scope this operator supports, or change the OperatorGroup's "
                    "targetNamespaces."
                ),
            }],
        )

    return (
        {
            "namespace": namespace,
            "operatorGroups": groups,
            "requiredInstallMode": required,
            "ready": True,
            "detail": (
                f"OperatorGroup {group.get('name')} requires {required}, which this "
                "channel supports."
            ),
        },
        consequences,
    )


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #

def subscription_api_version() -> str:
    """Which ``operators.coreos.com`` version this cluster serves Subscriptions at.

    Falls back to the first candidate when the API is not served or could not be
    resolved, so that the plan still renders a document on a cluster with no OLM
    — the caller learns that from ``sources[]``, not from a missing field. The
    write resolves it again and ``apply.create_from_yaml`` refuses a document
    whose apiVersion disagrees with the URL, so the fallback can never write to
    the wrong endpoint.
    """
    state = portal_service.source_state(portal_service.SUBSCRIPTIONS)
    return state.version or portal_service.SUBSCRIPTIONS.versions[0]


def build_subscription(
    request: dict[str, Any], channel: Any, package: Any, *, version: str
) -> dict[str, Any]:
    """The one object this feature writes.

    Named after the package, which is what OLM's own tooling and the OpenShift
    console do — so a Subscription created here and one created with ``kubectl``
    collide rather than quietly coexisting, and a collision is a 409 the operator
    can read.

    Nothing is added that the caller did not ask for: no labels of our own, no
    annotations, no ``config`` block. An operator reading the diff sees exactly
    the fields they filled in, and this console leaves no fingerprint on an
    object OLM will go on to manage.
    """
    spec: dict[str, Any] = {
        "name": request["package"],
        "channel": get_field(channel, "name"),
        "source": get_field(package, "status", "catalogSource"),
        "sourceNamespace": get_field(package, "status", "catalogSourceNamespace"),
        "installPlanApproval": request["installPlanApproval"],
    }
    if request["startingCSV"]:
        spec["startingCSV"] = request["startingCSV"]

    return {
        "apiVersion": f"{portal_service.SUBSCRIPTIONS.group}/{version}",
        "kind": "Subscription",
        "metadata": {
            "name": request["package"],
            "namespace": request["namespace"],
        },
        "spec": spec,
    }


def plan(payload: dict[str, Any]) -> dict[str, Any]:
    """§16 ``POST /api/portal/subscriptions/plan`` — the object, and what will stop it.

    Ungated and unaudited: everything it does is a read. It resolves the package
    in the cluster's catalogs, picks the channel, renders the Subscription that
    would be created, and reports whether the target namespace is one OLM can
    actually install into. Nothing is written, and an operator deciding whether
    to enable subscribing has to be able to read this first.

    The reads it makes can fail independently, and each failure costs its own
    field rather than the answer: an unreadable OperatorGroup listing leaves
    ``target.ready`` at ``null`` with a consequence saying so, and the rendered
    document is returned regardless.
    """
    request = validate_request(payload)
    unavailable: list[dict[str, Any]] = []

    package = portal_service.find_package(
        request["package"],
        catalog_name=request["catalog"],
        catalog_namespace=request["catalogNamespace"],
    )
    channel = portal_service.channel_named(package, request["channel"])
    if channel is None:
        available = [
            name
            for name in (
                get_field(c, "name") for c in portal_service.channels_of(package)
            )
            if name
        ]
        raise Invalid(
            f"{request['package']} has no channel named {request['channel']!r}."
            if request["channel"]
            else f"{request['package']} publishes no default channel.",
            detail=(
                f"It publishes: {', '.join(available)}."
                if available
                else "It publishes no channels at all, so there is nothing to subscribe to."
            ),
            hint="Pick one of the channels the catalog publishes.",
            context={"parameter": "channel", "channels": available},
        )

    target, consequences = target_state(request["namespace"], channel, unavailable)

    existing = _existing_subscriptions(
        request["namespace"], request["package"], unavailable
    )
    if existing is None:
        consequences.append({
            "code": WARN_SUBSCRIPTIONS_UNKNOWN,
            "label": "Whether this operator is already subscribed here is unknown",
            "consequence": (
                "The Subscription listing for this namespace did not answer. If "
                "one already exists, creating a second leaves two resolutions "
                "competing for the same custom resources."
            ),
            "mitigation": "Retry when the API server is answering.",
        })
    elif existing:
        consequences.append({
            "code": WARN_ALREADY_SUBSCRIBED,
            "label": f"{request['package']} is already subscribed in {request['namespace']}",
            "consequence": (
                "A Subscription named after this package already exists here, so "
                "the create will be refused with a conflict. If it were written "
                "under another name, two Subscriptions would resolve the same "
                "package independently."
            ),
            "mitigation": (
                "Change its channel instead of subscribing again, or subscribe "
                "into a different namespace."
            ),
        })

    if request["installPlanApproval"] == "Manual":
        consequences.append({
            "code": WARN_MANUAL_APPROVAL,
            "label": "Manual approval holds the install until somebody approves it",
            "consequence": (
                "OLM will create an InstallPlan and stop. Nothing is installed, "
                "and nothing upgrades later, until the InstallPlan is approved — "
                "which this console does not do."
            ),
            "mitigation": (
                "Approve the InstallPlan from the API explorer after subscribing, "
                "or choose Automatic."
            ),
        })

    # The same resolution the write will use, so the document's apiVersion and
    # the URL it is posted to cannot disagree — a disagreement is refused by
    # apply._check_document_matches_url, loudly, but only after the operator
    # has read a diff of an object that was never going to be accepted.
    subscription_version = subscription_api_version()
    document = build_subscription(request, channel, package, version=subscription_version)

    body: dict[str, Any] = {
        "package": request["package"],
        "namespace": request["namespace"],
        "channel": get_field(channel, "name"),
        "catalog": get_field(package, "status", "catalogSource"),
        "catalogNamespace": get_field(package, "status", "catalogSourceNamespace"),
        "installPlanApproval": request["installPlanApproval"],
        "displayName": get_field(package, "status", "catalogSourceDisplayName"),
        "provider": get_field(package, "status", "provider", "name"),
        "defaultChannel": get_field(package, "status", "defaultChannel"),
        "channels": [
            portal_service.channel_payload(c)
            for c in portal_service.channels_of(package)
        ],
        "selected": portal_service.channel_payload(channel),
        "target": target,
        "existing": existing,
        "consequences": consequences,
        "document": reader.to_yaml(document),
        # Hand-built rather than through envelope(): this is an object, not a
        # collection. Kept adjacent to the return so the two cannot drift.
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }
    gate = enabled_state()
    body["enabled"] = gate["enabled"]
    body["enabledDetail"] = gate["detail"]
    return body


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Named, not boolean, and copied from §13's ``acknowledgeLossy`` for the reason
    given there: a UI that acknowledged everything once and then changed the
    namespace has to acknowledge the *new* list. Extra tokens are accepted;
    missing ones are not.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This subscription has consequences that have not been acknowledged.",
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


def subscribe(
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """§16 ``POST /api/portal/subscriptions`` — create one Subscription.

    The plan is recomputed here rather than trusted from the caller: a client
    that read the plan, changed the namespace and posted the old
    acknowledgements would otherwise consent to consequences that no longer describe
    the write.

    Returns the §1.5 mutation response with the plan's ``consequences``,
    ``installTarget``
    and the CSV OLM is expected to install riding along, because ``applied:
    true`` here means one object was created and nothing more. What the operator
    needs next is on the Installed view, and the response says so rather than
    letting a green toast imply an installation.
    """
    request = validate_request(payload)
    # Gated before the catalog is read, the way §14's install gates before it
    # builds: a caller whose deployment forbids this should get
    # ``mutations_disabled`` rather than a 404 about a package name they were
    # never going to be allowed to subscribe to anyway.
    _require_open(
        dry_run=dry_run,
        namespace=request["namespace"],
        package=request["package"],
    )
    computed = plan(payload)
    _require_acknowledgement(computed["consequences"], acknowledge_consequences)

    state = portal_service.source_state(portal_service.SUBSCRIPTIONS)
    if not state.available and state.error is not None:
        raise state.error
    version = subscription_api_version()

    selected = computed["selected"]
    result = apply_subscription(
        version=version,
        namespace=request["namespace"],
        document=computed["document"],
        detail=(
            f"subscribe to {request['package']} channel "
            f"{computed['channel']} from catalog {computed['catalog']} "
            f"({request['installPlanApproval']} approval)"
        ),
        dry_run=dry_run,
    )

    result["package"] = request["package"]
    result["channel"] = computed["channel"]
    result["catalog"] = computed["catalog"]
    result["installPlanApproval"] = request["installPlanApproval"]
    # The CSV OLM is *expected* to install. Not a claim that it did: nothing in
    # this response may be read as evidence that an operator is running, and the
    # Installed view is where that question is actually answered.
    result["expectedCSV"] = selected.get("currentCSV")
    result["expectedVersion"] = selected.get("version")
    # NOT `target`: §1.5 owns that key for the group-version-resource this write
    # addressed, which is what the audit row is filed under. Overwriting it with
    # the namespace verdict would leave a mutation response no generic §1.5
    # client could read, for the sake of a field only this dialog wants.
    result["installTarget"] = computed["target"]
    # NOT `warnings`: §1.5 already owns that key for the API server's own
    # Warning: headers, and overwriting it would drop a deprecation notice on the
    # very object being created.
    result["consequences"] = computed["consequences"]
    result["partial"] = computed["partial"]
    result["unavailable"] = computed["unavailable"]
    return result


def apply_subscription(
    *,
    version: str,
    namespace: str,
    document: str,
    detail: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Hand the rendered Subscription to the funnel.

    A separate function only so the write is one readable line in
    :func:`subscribe` and so nothing above it can be mistaken for reaching the
    cluster. :func:`app.admin.apply.create_from_yaml` already carries the
    document-matches-URL check, namespace resolution, the verb check and the
    call to :func:`app.admin.mutate.mutate` — which is where the preflight, the
    dry run, the diff and the audit row happen.
    """
    from app.admin import apply as apply_service

    return apply_service.create_from_yaml(
        portal_service.SUBSCRIPTIONS.group,
        version,
        portal_service.SUBSCRIPTIONS.plural,
        namespace,
        document,
        dry_run,
        detail=detail,
    )


__all__ = [
    "APPROVAL_STRATEGIES",
    "WARN_ALREADY_SUBSCRIBED",
    "WARN_INSTALL_MODES_UNKNOWN",
    "WARN_INSTALL_MODE_UNSUPPORTED",
    "WARN_MANUAL_APPROVAL",
    "WARN_NO_OPERATOR_GROUP",
    "WARN_OPERATOR_GROUP_UNKNOWN",
    "WARN_SUBSCRIPTIONS_UNKNOWN",
    "WARN_TOO_MANY_OPERATOR_GROUPS",
    "build_subscription",
    "subscription_api_version",
    "enabled_state",
    "plan",
    "subscribe",
    "target_state",
    "validate_request",
]
