"""
The operator portal (§16) — what this cluster's own catalogs offer, and what is
already subscribed.

This is the read half of the portal. It reads six OLM APIs and shapes them into
two answers an operator actually asks: *what can I install here*, and *what did
somebody already install*. It installs nothing; :mod:`app.admin.portal` owns the
one write, and Operator Lifecycle Manager — which is the cluster's software, not
this console's — owns everything that happens after it.

**This console ships no catalog.** Every package here comes from a
``CatalogSource`` the cluster already runs. There is no bundled list, no curated
index, and no network call to anywhere but the API server. A cluster with no
catalogs has an empty portal, which is the true answer, and a cluster with a
private mirror gets its own contents with no configuration here. That boundary
is what keeps §16 a read of somebody else's registry rather than a second thing
this console ships; ``docs/adr-0005-operator-portal.md`` records why it matters.

Three distinctions carry the whole module.

**"This cluster does not run OLM" and "we could not find out" are different
answers.** Every API is resolved through :func:`app.resources.catalog.resolve`,
which already refuses to report ``unsupported`` for a group it could not
enumerate. :func:`source_state` maps that onto the same three states §13 uses
for route backends — ``available``, ``unsupported``, ``unknown`` — and only
``unknown`` makes the listing partial. A cluster with no OLM is a perfectly
ordinary cluster, and raising the §1.2 partial banner on every one of them
would be the banner nobody reads.

**``installed`` is a tri-state and ``false`` is a claim.** It is ``None``
whenever the Subscription listing failed. Rendering "not installed" for an
operator that *is* installed is not a cosmetic error here: the operator subscribes
again, and a second Subscription for the same package is how you get two
ClusterServiceVersions racing to own the same CRDs. ``false`` is reserved for a
listing that succeeded and matched nothing.

**A Subscription is a request, not an installation.** ``spec`` is what the
operator asked for; ``status.installedCSV`` is what OLM actually put on the
cluster, and it is empty for as long as resolution is pending, an InstallPlan is
waiting for approval, or the namespace is missing the OperatorGroup OLM needs.
Every row keeps the two apart, and :func:`installed_operators` reports the CSV
phase as ``None`` — never ``Succeeded``, never ``Failed`` — whenever it could not
read the CSV that would have answered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from app.errors import AdminError, NotFound, Unsupported, UpstreamError
from app.resources import catalog as discovery
from app.resources import reader
from app.resources.envelope import (
    collect,
    envelope,
    reason_for_error,
    unavailable_entry,
)
from app.resources.shaping import age_seconds, get_field

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# The APIs OLM serves
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OperatorApi:
    """One Operator Lifecycle Manager API this module reads.

    ``versions`` is a preference order rather than a single string for the same
    reason §13's backends carry one: these are CRDs (and, for
    ``packagemanifests``, an aggregated APIService), and which version a cluster
    serves depends on which release of OLM it installed. Pinning
    ``operators.coreos.com/v1`` for OperatorGroups would 404 on a cluster still
    serving ``v1alpha2`` — and a 404 reads as "this namespace has no
    OperatorGroup", which is the single most consequential wrong answer this
    module can give: it is the reason a subscription installs nothing.

    ``required`` marks the APIs without which there is no portal at all. The
    others degrade to a column.
    """

    key: str
    kind: str
    group: str
    versions: tuple[str, ...]
    plural: str
    label: str
    required: bool


#: Two API groups, six resources. ``packages.operators.coreos.com`` is served by
#: the OLM package server as an aggregated APIService — it is the one that
#: answers 503 when OLM is degraded rather than disappearing from discovery, and
#: that difference is exactly what :func:`source_state` must not flatten.
PACKAGES = OperatorApi(
    key="packages",
    kind="PackageManifest",
    group="packages.operators.coreos.com",
    versions=("v1",),
    plural="packagemanifests",
    label="Catalog contents",
    required=True,
)
SUBSCRIPTIONS = OperatorApi(
    key="subscriptions",
    kind="Subscription",
    group="operators.coreos.com",
    versions=("v1alpha1",),
    plural="subscriptions",
    label="Subscriptions",
    required=True,
)
CLUSTER_SERVICE_VERSIONS = OperatorApi(
    key="clusterserviceversions",
    kind="ClusterServiceVersion",
    group="operators.coreos.com",
    versions=("v1alpha1",),
    plural="clusterserviceversions",
    label="Installed operators",
    required=False,
)
INSTALL_PLANS = OperatorApi(
    key="installplans",
    kind="InstallPlan",
    group="operators.coreos.com",
    versions=("v1alpha1",),
    plural="installplans",
    label="Install plans",
    required=False,
)
CATALOG_SOURCES = OperatorApi(
    key="catalogsources",
    kind="CatalogSource",
    group="operators.coreos.com",
    versions=("v1alpha1",),
    plural="catalogsources",
    label="Catalogs",
    required=False,
)
OPERATOR_GROUPS = OperatorApi(
    key="operatorgroups",
    kind="OperatorGroup",
    group="operators.coreos.com",
    # v1 is current; v1alpha2 is what pre-0.17 OLM served and some long-lived
    # clusters still do. First one discovery actually serves wins.
    versions=("v1", "v1alpha2"),
    plural="operatorgroups",
    label="Operator groups",
    required=False,
)

APIS: tuple[OperatorApi, ...] = (
    PACKAGES,
    SUBSCRIPTIONS,
    CLUSTER_SERVICE_VERSIONS,
    INSTALL_PLANS,
    CATALOG_SOURCES,
    OPERATOR_GROUPS,
)


# --------------------------------------------------------------------------- #
# Three-valued API state
# --------------------------------------------------------------------------- #

STATE_AVAILABLE = "available"
STATE_UNSUPPORTED = "unsupported"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceState:
    """Whether this cluster serves one OLM API, and how we know.

    ``error`` is populated only for :data:`STATE_UNKNOWN`, and it is the failure
    discovery actually raised rather than a synthesised one, so the reason token
    the operator ends up seeing is ``forbidden`` when the read was refused and
    ``unreachable`` when the package server did not answer. Those two send
    somebody to two different places.
    """

    api: OperatorApi
    state: str
    version: str | None
    detail: str
    error: AdminError | None = None

    @property
    def available(self) -> bool:
        return self.state == STATE_AVAILABLE


def source_state(api: OperatorApi) -> SourceState:
    """Resolve one OLM API against this cluster's discovery.

    The classification of the failures is the entire point:

    * ``Unsupported`` — discovery answered and the group-version is not served.
      OLM is not installed. An ordinary fact about a cluster.
    * ``NotFound`` — the group-version is served and does not list the resource.
      Real: OLM's CRDs can be present while the package server APIService is not.
    * anything else — ``ClusterUnreachable``, ``RBACDenied``, ``UpstreamError``.
      We could not look, so whether the cluster serves it is unknown.

    A single ``unknown`` outranks any number of ``unsupported`` misses across the
    candidate versions: failing to read ``v1`` does not entitle us to conclude
    from the ``v1alpha2`` miss that the cluster has no OperatorGroups.
    """
    misses: list[str] = []
    blind: AdminError | None = None

    for version in api.versions:
        try:
            discovery.resolve(api.group, version, api.plural)
        except (Unsupported, NotFound) as e:
            misses.append(f"{api.group}/{version}: {e.message}")
        except AdminError as e:
            # Keep the first blind spot: they are all the same outage, and the
            # first carries the version the operator is most likely asking about.
            blind = blind or e
        else:
            return SourceState(
                api=api,
                state=STATE_AVAILABLE,
                version=version,
                detail=f"This cluster serves {api.group}/{version} {api.plural}.",
            )

    if blind is not None:
        return SourceState(
            api=api,
            state=STATE_UNKNOWN,
            version=None,
            detail=(
                f"Whether this cluster serves {api.kind} objects could not be "
                f"determined: {blind.message} This is not the same as the "
                "cluster not having them."
            ),
            error=blind,
        )

    return SourceState(
        api=api,
        state=STATE_UNSUPPORTED,
        version=None,
        detail=(
            f"This cluster does not serve {api.kind} objects. "
            + ("; ".join(misses) if misses else "")
        ).strip(),
    )


def source_payload(state: SourceState) -> dict[str, Any]:
    """One ``sources[]`` entry.

    Reported for every API, including the ones that are fine, because rule 11.4
    needs a *reason* to put on a disabled control and an absent key carries
    none.
    """
    return {
        "api": state.api.key,
        "kind": state.api.kind,
        "group": state.api.group,
        # Null rather than the first candidate when the API is not available:
        # naming a version we never confirmed is served lets a caller build a
        # URL out of it.
        "version": state.version,
        "plural": state.api.plural,
        "label": state.api.label,
        "required": state.api.required,
        "state": state.state,
        "detail": state.detail,
    }


def unknown_entry(state: SourceState, *, namespace: str | None = None) -> dict[str, Any]:
    """The §1.2 ``unavailable[]`` entry for an API we could not classify."""
    error = state.error
    assert error is not None  # only called for STATE_UNKNOWN, which always has one
    reason = reason_for_error(error)
    if reason is None:
        # See `app.services.routes._unknown_entry`: an error that is not about
        # availability must not be rendered as one.
        raise error
    return unavailable_entry(
        state.api.group,
        state.api.plural,
        reason,
        detail=error.detail or error.message,
        namespace=namespace,
    )


def resolve_sources(apis: Iterable[OperatorApi]) -> dict[str, SourceState]:
    """Resolve several APIs once, keyed by :attr:`OperatorApi.key`.

    Resolved together rather than per-read because discovery is cached per
    request-ish window anyway and because the *set* of states is what the
    response reports: a caller that resolved lazily would report only the APIs
    it happened to reach before the first failure.
    """
    return {api.key: source_state(api) for api in apis}


# --------------------------------------------------------------------------- #
# Shapers — pure, no I/O
# --------------------------------------------------------------------------- #

def _annotation(channel: Any, name: str) -> Any:
    """One ``currentCSVDesc.annotations`` value, or None."""
    annotations = get_field(channel, "currentCSVDesc", "annotations", default={}) or {}
    value = annotations.get(name)
    return value if value not in ("", None) else None


def _csv_bool(value: Any) -> bool | None:
    """Parse an OLM annotation that is a stringified boolean.

    ``None`` for absent *and* for anything that is not one of the two spellings.
    Guessing ``false`` for an unrecognised value would render "not certified"
    for an operator whose publisher wrote ``True`` — a claim about somebody
    else's software made from a parse failure.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes"):
            return True
        if lowered in ("false", "no"):
            return False
    return None


def _categories(channel: Any) -> list[str]:
    """The ``categories`` annotation, which OLM stores comma-separated."""
    raw = _annotation(channel, "categories")
    if not isinstance(raw, str):
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


#: The image types §16.10 will hand back, and the only ones ``icon_of`` reports.
#:
#: An allowlist rather than the catalog's own ``mediatype``, because that string
#: is written by whoever published the operator and ends up in a ``Content-Type``
#: header on this console's own origin. A catalog naming ``text/html`` there
#: would otherwise get the console to serve somebody else's markup as its own.
ICON_MEDIA_TYPES = frozenset({
    "image/svg+xml",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})

#: Refuse to serve a decoded icon larger than this.
#:
#: A logo is kilobytes. This bound exists because the bytes come from a registry
#: image the cluster pulled, not because any real icon approaches it — an
#: operator whose publisher embedded a megabyte of PNG gets the placeholder
#: tile rather than making every catalog tile wait on it.
ICON_MAX_BYTES = 1024 * 1024


def icon_of(channel: Any) -> dict[str, str] | None:
    """The channel CSV's first usable icon, as ``{mediatype, base64data}``.

    ``None`` when the catalog published none, when it published one in a type
    this console will not serve, or when the entry is missing its data. Filtering
    here rather than at the endpoint is deliberate: ``hasIcon`` on a §16.3 row is
    built from this, so a row claiming an icon is a row whose icon §16.10 will
    actually hand back. The alternative is a tile that requests an image, gets a
    404 and falls back — a promise the list made and the endpoint broke.
    """
    icons = get_field(channel, "currentCSVDesc", "icon", default=[]) or []
    for entry in icons:
        media = get_field(entry, "mediatype")
        data = get_field(entry, "base64data")
        if media in ICON_MEDIA_TYPES and data:
            return {"mediatype": media, "base64data": data}
    return None


def channels_of(package: Any) -> list[Any]:
    """``status.channels``, or an empty list.

    An empty list here is a real state and not a swallowed failure: a
    PackageManifest whose catalog has been pruned can genuinely carry no
    channels, and it is unsubscribable, which is what the caller needs to know.
    """
    return get_field(package, "status", "channels", default=[]) or []


def channel_named(package: Any, name: str | None) -> Any | None:
    """One channel by name, defaulting to ``status.defaultChannel``.

    Returns ``None`` when the named channel is not in the package — the caller
    turns that into a 422 listing the channels that *are*, rather than silently
    subscribing to the default and installing something nobody chose.
    """
    channels = channels_of(package)
    wanted = name or get_field(package, "status", "defaultChannel")
    for channel in channels:
        if get_field(channel, "name") == wanted:
            return channel
    return None


def install_modes(channel: Any) -> list[dict[str, Any]] | None:
    """The channel CSV's install modes, or ``None`` when it carries none.

    ``None`` rather than ``[]``: an empty list would read as "this operator
    supports no install mode", which is a claim no PackageManifest makes. The
    absence means the catalog did not publish the CSV description, and the
    OperatorGroup check downstream reports *that* rather than inventing a
    verdict from it.
    """
    modes = get_field(channel, "currentCSVDesc", "installModes", default=None)
    if not modes:
        return None
    shaped = [
        {
            "type": get_field(mode, "type"),
            "supported": bool(get_field(mode, "supported", default=False)),
        }
        for mode in modes
        if get_field(mode, "type")
    ]
    return shaped or None


def supported_modes(channel: Any) -> list[str] | None:
    """Just the names of the supported install modes, or ``None`` if unpublished."""
    modes = install_modes(channel)
    if modes is None:
        return None
    return [mode["type"] for mode in modes if mode["supported"]]


def owned_custom_resources(channel: Any) -> list[dict[str, Any]]:
    """The CRDs the channel's CSV says it owns.

    Shown before a subscription because these are the API objects that will
    appear in the cluster's discovery afterwards — the visible half of what
    installing an operator does to a cluster.
    """
    owned = get_field(
        channel, "currentCSVDesc", "customresourcedefinitions", "owned", default=[]
    ) or []
    return [
        {
            "kind": get_field(item, "kind"),
            "name": get_field(item, "name"),
            "version": get_field(item, "version"),
            "description": get_field(item, "description"),
        }
        for item in owned
    ]


def channel_payload(channel: Any) -> dict[str, Any]:
    """One channel, in full — what the subscribe dialog reads before writing."""
    return {
        "name": get_field(channel, "name"),
        "currentCSV": get_field(channel, "currentCSV"),
        "version": get_field(channel, "currentCSVDesc", "version"),
        "displayName": get_field(channel, "currentCSVDesc", "displayName"),
        "summary": _annotation(channel, "description"),
        # The long-form markdown the catalog publishes. Carried here rather than
        # on the catalog row because it is routinely tens of kilobytes and a
        # listing of three hundred packages would be a response nobody can use.
        "description": get_field(channel, "currentCSVDesc", "description"),
        "installModes": install_modes(channel),
        "minKubeVersion": get_field(channel, "currentCSVDesc", "minKubeVersion") or None,
        "ownedCustomResources": owned_custom_resources(channel),
        "containerImage": _annotation(channel, "containerImage"),
        "repository": _annotation(channel, "repository"),
        "capabilityLevel": _annotation(channel, "capabilities"),
        "certified": _csv_bool(_annotation(channel, "certified")),
        "categories": _categories(channel),
        "provider": get_field(channel, "currentCSVDesc", "provider", "name"),
    }


def package_row(package: Any, *, installations: list[dict[str, Any]] | None) -> dict[str, Any]:
    """One catalog row.

    ``installations`` is passed in rather than read here because shapers perform
    no I/O (house rule): the caller owns the Subscription listing and therefore
    owns the ``unavailable[]`` entry when it failed. ``None`` means that listing
    did not happen, and it propagates to ``installed: null`` — never ``false``,
    which would invite a second Subscription for an operator that already has
    one.
    """
    name = get_field(package, "status", "packageName") or get_field(
        package, "metadata", "name"
    )
    default_channel = get_field(package, "status", "defaultChannel")
    channel = channel_named(package, default_channel)
    catalog_namespace = get_field(package, "status", "catalogSourceNamespace")
    catalog_name = get_field(package, "status", "catalogSource")

    return {
        "id": f"{catalog_namespace or '-'}/{catalog_name or '-'}/{name}",
        "name": name,
        "displayName": (
            get_field(channel, "currentCSVDesc", "displayName") if channel else None
        )
        or name,
        "provider": get_field(package, "status", "provider", "name"),
        "providerUrl": get_field(package, "status", "provider", "url"),
        "catalog": catalog_name,
        "catalogNamespace": catalog_namespace,
        "catalogDisplayName": get_field(package, "status", "catalogSourceDisplayName"),
        "defaultChannel": default_channel,
        "channels": [
            n for n in (get_field(c, "name") for c in channels_of(package)) if n
        ],
        # The default channel's version. Null when the catalog published no CSV
        # description for it, which is a real state for a pruned catalog and is
        # not the same as version zero.
        "version": get_field(channel, "currentCSVDesc", "version") if channel else None,
        "summary": _annotation(channel, "description") if channel else None,
        "categories": _categories(channel) if channel else [],
        "capabilityLevel": _annotation(channel, "capabilities") if channel else None,
        "certified": _csv_bool(_annotation(channel, "certified")) if channel else None,
        "installModes": supported_modes(channel) if channel else None,
        # Whether §16.10 has an icon to serve for this package — not the icon.
        # The bytes stay off this row for the reason `description` does: a logo
        # is kilobytes, a catalog is hundreds of packages, and inlining both
        # would make the listing a response nobody can use. A plain bool rather
        # than a tri-state because nothing acts on it: the catalog either
        # published an icon or it did not, and a package with no CSV description
        # published none. The cost of being wrong is a placeholder tile.
        "hasIcon": icon_of(channel) is not None,
        "installed": None if installations is None else bool(installations),
        "installations": installations,
    }


def _subscription_conditions(obj: Any) -> list[dict[str, Any]]:
    """The Subscription conditions that explain a stalled install.

    ``ResolutionFailed`` and ``CatalogSourcesUnhealthy`` are the two that answer
    "I subscribed and nothing happened", so they are carried verbatim rather
    than collapsed into a boolean.
    """
    conditions = get_field(obj, "status", "conditions", default=[]) or []
    return [
        {
            "type": get_field(condition, "type"),
            "status": get_field(condition, "status"),
            "reason": get_field(condition, "reason"),
            "message": get_field(condition, "message"),
        }
        for condition in conditions
        if str(get_field(condition, "status")) == "True"
        and get_field(condition, "type")
    ]


def subscription_row(
    obj: Any,
    *,
    csv: Any | None = None,
    csv_read: bool = True,
    install_plan: Any | None = None,
    install_plan_read: bool = True,
) -> dict[str, Any]:
    """One installed-operator row: what was asked for, and what OLM did about it.

    The two are separate fields on purpose. ``spec.channel`` is the request;
    ``status.installedCSV`` is the only evidence anything was installed. A row
    that merged them would report an operator as installed the moment somebody
    typed its name, which is the console equivalent of the §1.5 rule that a
    successful dry run is not a write.

    ``csv_read``/``install_plan_read`` are how "we did not look" reaches the
    row. When either is false the corresponding phase is ``None`` with a reason
    naming the failed read, rather than the ``Failed`` that an absent object
    would otherwise imply.
    """
    namespace = get_field(obj, "metadata", "namespace")
    installed_csv = get_field(obj, "status", "installedCSV")
    current_csv = get_field(obj, "status", "currentCSV")

    if not csv_read:
        phase: str | None = None
        phase_detail = (
            "No complete ClusterServiceVersion listing was available, so whether "
            "this operator is running could not be read. This is not a failed "
            "install."
        )
    elif installed_csv is None:
        phase = None
        phase_detail = (
            "OLM has not installed anything for this Subscription yet — it "
            "publishes no installedCSV. Resolution may be pending, an "
            "InstallPlan may be waiting for approval, or the namespace may have "
            "no OperatorGroup."
        )
    elif csv is None:
        phase = None
        phase_detail = (
            f"The Subscription names {installed_csv}, and no "
            f"ClusterServiceVersion by that name is in {namespace}."
        )
    else:
        phase = get_field(csv, "status", "phase")
        phase_detail = get_field(csv, "status", "message") or get_field(
            csv, "status", "reason"
        )

    if not install_plan_read:
        approval_required: bool | None = None
        install_plan_detail = (
            "No complete InstallPlan listing was available, so whether one is "
            "waiting for approval is unknown."
        )
    elif install_plan is None:
        approval_required = None if get_field(obj, "status", "installPlanRef") else False
        install_plan_detail = (
            "The Subscription names an InstallPlan that is not in the listing."
            if approval_required is None
            else "No InstallPlan is outstanding."
        )
    else:
        phase_of_plan = get_field(install_plan, "status", "phase")
        approval_required = phase_of_plan == "RequiresApproval"
        install_plan_detail = (
            "This install is waiting for somebody to approve its InstallPlan; "
            "nothing is installed until they do."
            if approval_required
            else f"InstallPlan {get_field(install_plan, 'metadata', 'name')} is {phase_of_plan}."
        )

    return {
        "id": f"{namespace}/{get_field(obj, 'metadata', 'name')}",
        "name": get_field(obj, "metadata", "name"),
        "namespace": namespace,
        "package": get_field(obj, "spec", "name"),
        "channel": get_field(obj, "spec", "channel"),
        "catalog": get_field(obj, "spec", "source"),
        "catalogNamespace": get_field(obj, "spec", "sourceNamespace"),
        "installPlanApproval": get_field(obj, "spec", "installPlanApproval") or "Automatic",
        "startingCSV": get_field(obj, "spec", "startingCSV"),
        # What OLM actually did.
        "installedCSV": installed_csv,
        "currentCSV": current_csv,
        "state": get_field(obj, "status", "state"),
        "phase": phase,
        "phaseDetail": phase_detail,
        "approvalRequired": approval_required,
        "installPlanDetail": install_plan_detail,
        "installPlan": get_field(obj, "status", "installPlanRef", "name"),
        "conditions": _subscription_conditions(obj),
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def catalog_source_row(obj: Any) -> dict[str, Any]:
    """One CatalogSource, with its connection state kept three-valued.

    ``healthy`` is ``None`` until the catalog operator publishes a connection
    state. A freshly created CatalogSource has no status at all, and reporting
    that as unhealthy would send somebody to debug a registry that is merely
    still starting.
    """
    observed = get_field(obj, "status", "connectionState", "lastObservedState")
    healthy: bool | None
    if observed is None:
        healthy = None
    else:
        healthy = observed == "READY"

    return {
        "id": f"{get_field(obj, 'metadata', 'namespace')}/{get_field(obj, 'metadata', 'name')}",
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "displayName": get_field(obj, "spec", "displayName"),
        "publisher": get_field(obj, "spec", "publisher"),
        "sourceType": get_field(obj, "spec", "sourceType"),
        "image": get_field(obj, "spec", "image"),
        "state": observed,
        "healthy": healthy,
        "detail": (
            "This catalog has published no connection state yet."
            if observed is None
            else f"The catalog's last observed connection state is {observed}."
        ),
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def operator_group_row(obj: Any) -> dict[str, Any]:
    """One OperatorGroup, and the one thing about it that decides an install.

    ``allNamespaces`` is what OLM derives from an absent or empty
    ``spec.targetNamespaces``: the group watches every namespace, and an
    operator subscribed into it must support the ``AllNamespaces`` install mode
    or its CSV goes straight to ``Failed`` with ``UnsupportedOperatorGroup``.
    That is the check §16's plan exists to run before the write rather than
    after it.
    """
    targets = get_field(obj, "spec", "targetNamespaces", default=None)
    selector = get_field(obj, "spec", "selector", default=None)
    published = get_field(obj, "status", "namespaces", default=None)

    # Precedence matters and it is the opposite of the obvious reading: OLM
    # ignores `spec.selector` when `spec.targetNamespaces` is also set, so a
    # group carrying both is an explicit-list group. Checking the selector first
    # would report a perfectly determinate group as "we cannot tell", and put a
    # blocking consequence in front of an install that was going to work.
    #
    # `null` is reserved for the genuinely selector-only group: what it covers is
    # whatever the labels currently match, and this console does not evaluate
    # them. Absent-and-no-selector is the global group, whose status.namespaces
    # OLM publishes as a single empty string.
    if targets is not None:
        all_namespaces: bool | None = len(targets) == 0 or "" in targets
    elif selector:
        all_namespaces = None
    else:
        all_namespaces = True

    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "targetNamespaces": list(targets) if targets is not None else None,
        "publishedNamespaces": list(published) if published is not None else None,
        "selector": bool(selector),
        "allNamespaces": all_namespaces,
    }


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def _list(
    state: SourceState,
    unavailable: list[dict[str, Any]],
    *,
    namespace: str | None = None,
    limit: int = 500,
) -> dict[str, Any] | None:
    """List one available API inside its own ``collect`` block.

    Returns ``None`` for *both* "the API is not served" and "the read failed",
    which is why every caller pairs the result with the source state rather than
    reading emptiness off it.
    """
    if not state.available:
        return None
    version = state.version
    assert version is not None  # STATE_AVAILABLE always carries one
    listing: dict[str, Any] | None = None
    with collect(unavailable, state.api.group, state.api.plural, namespace=namespace):
        listing = reader.list_resource(
            state.api.group,
            version,
            state.api.plural,
            namespace=namespace,
            limit=limit,
        )
    return listing


def _index_subscriptions(items: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Group Subscription rows by the package they name.

    Keyed on ``spec.name`` alone, not on the catalog triple. An operator
    installed from a mirror is still installed, and a catalog row that reported
    it as available-to-install because the source strings differ would be
    telling somebody to subscribe to something they are already running.
    """
    by_package: dict[str, list[dict[str, Any]]] = {}
    for obj in items:
        package = get_field(obj, "spec", "name")
        if not package:
            continue
        by_package.setdefault(package, []).append(
            subscription_row(obj, csv_read=False, install_plan_read=False)
        )
    return by_package


def catalog(*, limit: int = 500) -> dict[str, Any]:
    """§16 ``GET /api/portal/catalog`` — every package this cluster's catalogs offer.

    Three reads, each in its own ``collect`` block: the PackageManifests, the
    Subscriptions that say what is already installed, and the CatalogSources
    that say where the packages came from and whether that registry is
    answering.

    Read cluster-wide rather than per namespace. PackageManifests are namespaced
    objects served by an aggregated API server, and listing them in one namespace
    returns the global catalogs plus that namespace's local ones — so a
    namespace-scoped read would silently omit another team's private catalog
    from a page whose whole purpose is "what can this cluster install".

    ``continue`` is deliberately not propagated. The package server does not
    implement pagination, so a cursor from the Subscription listing would be a
    cursor this response cannot honour; what a bounded read left out is reported
    in ``truncated[]`` instead of behind a paging control that lies.
    """
    unavailable: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []
    states = resolve_sources((PACKAGES, SUBSCRIPTIONS, CATALOG_SOURCES))

    for state in states.values():
        if state.state == STATE_UNKNOWN and state.error is not None:
            unavailable.append(unknown_entry(state))

    subscriptions = _list(states["subscriptions"], unavailable, limit=limit)
    # None covers both "OLM is not installed" and "the read failed", and those
    # must not produce the same row. Only a listing that actually happened
    # entitles a row to say `installed: false`.
    by_package = (
        _index_subscriptions(subscriptions["items"]) if subscriptions is not None else None
    )
    if subscriptions is not None and subscriptions["continue"]:
        truncated.append({
            "kind": "Subscription",
            "shown": len(subscriptions["items"]),
            "remaining": subscriptions["remaining"],
            "detail": (
                "More Subscriptions exist than were read, so a package shown as "
                "not installed may be installed by one that was not seen."
            ),
        })

    packages = _list(states["packages"], unavailable, limit=limit)
    rows: list[dict[str, Any]] = []
    if packages is not None:
        for obj in packages["items"]:
            name = get_field(obj, "status", "packageName") or get_field(
                obj, "metadata", "name"
            )
            rows.append(
                package_row(
                    obj,
                    installations=None if by_package is None else by_package.get(name, []),
                )
            )
        if packages["continue"]:
            truncated.append({
                "kind": "PackageManifest",
                "shown": len(packages["items"]),
                "remaining": packages["remaining"],
                "detail": "More packages are in the cluster's catalogs than are shown.",
            })

    sources_listing = _list(states["catalogsources"], unavailable, limit=limit)
    catalogs = (
        [catalog_source_row(obj) for obj in sources_listing["items"]]
        if sources_listing is not None
        else None  # never [], which would read as "this cluster has no catalogs"
    )

    body = envelope(rows, unavailable=unavailable)
    body["sources"] = [source_payload(states[key]) for key in states]
    body["catalogs"] = catalogs
    body["truncated"] = truncated
    return body


def _index_by_name(items: Iterable[Any]) -> dict[tuple[str | None, str | None], Any]:
    """Index namespaced objects by ``(namespace, name)``.

    OLM copies a CSV owned by an all-namespaces OperatorGroup into every other
    namespace, so a cluster-wide listing carries many objects with the same name.
    Keying on the pair is what keeps a copy in ``kube-system`` from being read as
    the installation that a Subscription in ``monitoring`` is waiting for.
    """
    index: dict[tuple[str | None, str | None], Any] = {}
    for obj in items:
        index[(
            get_field(obj, "metadata", "namespace"),
            get_field(obj, "metadata", "name"),
        )] = obj
    return index


def installed_operators(
    *, namespace: str | None = None, limit: int = 500
) -> dict[str, Any]:
    """§16 ``GET /api/portal/subscriptions`` — what somebody already installed.

    Subscriptions joined to the ClusterServiceVersion each one names and to its
    outstanding InstallPlan. The join is by ``(namespace, name)`` rather than by
    name, because OLM copies CSVs across namespaces and a copy is not the
    installation.

    A failed or truncated CSV read costs the phase column and says so — the row
    keeps ``phase: null`` with a sentence naming which read did not happen. It
    never degrades to ``Failed``, which would report a healthy operator as
    broken during an API outage.
    """
    unavailable: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []
    states = resolve_sources((SUBSCRIPTIONS, CLUSTER_SERVICE_VERSIONS, INSTALL_PLANS))

    for state in states.values():
        if state.state == STATE_UNKNOWN and state.error is not None:
            unavailable.append(unknown_entry(state, namespace=namespace))

    subscriptions = _list(
        states["subscriptions"], unavailable, namespace=namespace, limit=limit
    )
    csvs = _list(
        states["clusterserviceversions"], unavailable, namespace=namespace, limit=limit
    )
    plans = _list(states["installplans"], unavailable, namespace=namespace, limit=limit)

    # A truncated listing is a listing we only partly saw. Treated as "did not
    # read" for the join so that a Subscription whose CSV fell outside the page
    # reports an unknown phase rather than a missing installation.
    csv_read = csvs is not None and not csvs["continue"]
    plan_read = plans is not None and not plans["continue"]
    csv_index = _index_by_name(csvs["items"]) if csvs is not None else {}
    plan_index = _index_by_name(plans["items"]) if plans is not None else {}

    for listing, kind, read in (
        (csvs, "ClusterServiceVersion", csv_read),
        (plans, "InstallPlan", plan_read),
    ):
        if listing is not None and not read:
            truncated.append({
                "kind": kind,
                "shown": len(listing["items"]),
                "remaining": listing["remaining"],
                "detail": (
                    f"More {kind}s exist than were read, so every row's phase is "
                    "reported as unknown rather than guessed from a partial listing."
                ),
            })

    rows: list[dict[str, Any]] = []
    if subscriptions is not None:
        for obj in subscriptions["items"]:
            ns = get_field(obj, "metadata", "namespace")
            installed_csv = get_field(obj, "status", "installedCSV")
            plan_ref = get_field(obj, "status", "installPlanRef", "name")
            rows.append(
                subscription_row(
                    obj,
                    csv=csv_index.get((ns, installed_csv)) if installed_csv else None,
                    csv_read=csv_read,
                    install_plan=plan_index.get((ns, plan_ref)) if plan_ref else None,
                    install_plan_read=plan_read,
                )
            )
        if subscriptions["continue"]:
            truncated.append({
                "kind": "Subscription",
                "shown": len(subscriptions["items"]),
                "remaining": subscriptions["remaining"],
                "detail": "More Subscriptions exist on this cluster than are shown.",
            })

    body = envelope(rows, unavailable=unavailable)
    body["sources"] = [source_payload(states[key]) for key in states]
    body["truncated"] = truncated
    return body


def find_package(
    name: str, *, catalog_name: str | None = None, catalog_namespace: str | None = None
) -> Any:
    """Read one PackageManifest by package name.

    Listed and filtered rather than fetched by object name: a PackageManifest's
    ``metadata.name`` is the package name only by convention, and the same
    package name legitimately appears in more than one catalog. When the caller
    named a catalog, only that catalog's copy will do — subscribing to
    "prometheus from community-operators" and being handed the mirror's copy
    would write a Subscription pointing at a registry the operator did not pick.
    """
    state = source_state(PACKAGES)
    if state.state == STATE_UNSUPPORTED:
        raise Unsupported(
            "This cluster does not serve PackageManifests.",
            detail=state.detail,
            hint=(
                "Operator Lifecycle Manager is not installed here. There is no "
                "catalog to subscribe to."
            ),
        )
    if state.state == STATE_UNKNOWN and state.error is not None:
        raise state.error

    version = state.version
    assert version is not None
    # Cluster-wide, and with a bound well above any real cluster's catalogs. The
    # package server does not implement continuation, so in practice this is one
    # read that returns everything; the guard below exists for the cluster that
    # proves that wrong, rather than letting a bounded read answer "no such
    # package".
    listing = reader.list_resource(
        PACKAGES.group, version, PACKAGES.plural, namespace=None, limit=2000
    )

    matches = [
        obj
        for obj in listing["items"]
        if (get_field(obj, "status", "packageName") or get_field(obj, "metadata", "name"))
        == name
        and (catalog_name is None or get_field(obj, "status", "catalogSource") == catalog_name)
        and (
            catalog_namespace is None
            or get_field(obj, "status", "catalogSourceNamespace") == catalog_namespace
        )
    ]
    if not matches and listing["continue"]:
        # "We did not see all of them" is not "it is not there". Answering
        # NotFound off a truncated listing sends somebody to install a
        # CatalogSource the cluster already has.
        raise UpstreamError(
            f"The catalog listing was truncated before {name} could be found.",
            detail=(
                f"{len(listing['items'])} packages were read and more remain, so "
                "whether this cluster's catalogs offer that one could not be "
                "determined."
            ),
            hint="Name the catalog it should come from to narrow the read.",
            context={"package": name},
        )
    if not matches:
        raise NotFound(
            f"No package named {name} is in this cluster's catalogs.",
            detail=(
                "The catalogs were read and none of them offers it"
                + (f" from {catalog_name}." if catalog_name else ".")
            ),
            hint=(
                "Check the package name against the portal listing, or add the "
                "CatalogSource that carries it."
            ),
            context={"package": name, "catalog": catalog_name},
        )
    if len(matches) > 1 and catalog_name is None:
        # Two catalogs carrying the same package name is ordinary. Refusing to
        # guess is the point: the two are different software with different
        # publishers, and picking the first would subscribe to whichever one the
        # API server happened to list first.
        raise NotFound(
            f"More than one catalog offers a package named {name}.",
            detail="; ".join(
                f"{get_field(m, 'status', 'catalogSourceNamespace')}/"
                f"{get_field(m, 'status', 'catalogSource')}"
                for m in matches
            ),
            hint="Name the catalog it should come from.",
            context={"package": name, "parameter": "catalog"},
        )
    return matches[0]


__all__ = [
    "APIS",
    "CATALOG_SOURCES",
    "CLUSTER_SERVICE_VERSIONS",
    "INSTALL_PLANS",
    "OPERATOR_GROUPS",
    "PACKAGES",
    "STATE_AVAILABLE",
    "STATE_UNKNOWN",
    "STATE_UNSUPPORTED",
    "SUBSCRIPTIONS",
    "OperatorApi",
    "SourceState",
    "catalog",
    "catalog_source_row",
    "channel_named",
    "channel_payload",
    "channels_of",
    "find_package",
    "install_modes",
    "installed_operators",
    "operator_group_row",
    "package_row",
    "source_payload",
    "source_state",
    "subscription_row",
    "supported_modes",
    "unknown_entry",
]
