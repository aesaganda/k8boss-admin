"""
What this cluster actually serves (§4 catalog), and the resolver every generic
read goes through.

Discovery is done here rather than through ``DynamicClient``'s own discoverer for
two reasons, both of which are about the contract rather than about taste.

**A broken group must not look like a smaller cluster.** ``/apis`` lists the
group-versions, and each one is then fetched separately. An aggregated APIService
whose backing pod is down answers 503 for *its* group and nothing else — so that
group becomes an ``unavailable`` entry with ``reason: "unreachable"`` and every
other group is still listed. A discoverer that raised on the first failure would
give an empty catalog ("this cluster serves nothing"), and one that skipped the
failure silently would give a shorter one ("this cluster has no
metrics.k8s.io") — which is the exact case §4 calls out: *a missing group must
not look like a cluster with fewer resources*.

**"Not in the catalog" has to be distinguishable from "we could not look".**
:func:`resolve` refuses to report ``unsupported`` for a group that appears in the
``unavailable`` list. It re-raises the real failure instead, so a caller browsing
``metrics.k8s.io`` during an aggregation outage is told the API server could not
answer — not that their cluster does not have metrics. Getting this backwards
would send an operator to install something they already have.

The result is cached per cluster with a **bounded** TTL. Bounded, not
invalidate-on-write: installing a CRD is not an action this console takes, so
there is no event to hang an invalidation on, and an unbounded cache would make a
freshly installed operator's resources invisible until the pod restarted.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.errors import (
    AdminError,
    ClusterUnreachable,
    NotFound,
    RBACDenied,
    Unsupported,
    UpstreamError,
)
from app.k8s.client import get_api_client, get_clients
from app.k8s.impersonation import as_service_account
from app.resources.envelope import ALL_RESOURCES, collect

logger = logging.getLogger(__name__)

#: The §1.4 wire spelling of the core group. The core group's real name is the
#: empty string, which cannot appear in a URL path segment.
CORE_WIRE_NAME = "core"

# A complete discovery is cached for a minute: long enough that the console's
# many pages share one round of ~40 requests, short enough that a CRD installed
# while an operator is looking at the console shows up on their next refresh
# rather than on the next pod restart.
_TTL_COMPLETE_SECONDS = 60.0

# A partial discovery is cached far more briefly. The usual cause is an
# aggregated APIService restarting, which resolves in seconds — holding the
# degraded catalog for a full minute would keep telling the operator a group is
# unreachable well after it came back, and they would learn to ignore the banner.
_TTL_PARTIAL_SECONDS = 10.0

# Subresources ("pods/log", "deployments/scale") come back from discovery in the
# same list as their parents. They are excluded from the catalog because the
# catalog drives a resource *browser* and a subresource is not a collection you
# can list — it is addressed through its parent object. They remain reachable
# for preflight, which names them separately (§9 `subresource`).
_SUBRESOURCE_MARKER = "/"


@dataclass
class _CatalogEntry:
    """One cluster's cached discovery."""

    items: list[dict[str, Any]]
    unavailable: list[dict[str, Any]]
    expires_at: float = field(default=0.0)


# Keyed on (cluster id, client cache key). The second half is the cluster row's
# `updated_at` stamp, already computed by ClusterClientManager: re-pointing a
# cluster at a different API server therefore drops its catalog by construction,
# rather than serving the old cluster's resource list against the new endpoint.
_cache: dict[tuple[int | None, str], _CatalogEntry] = {}

# Discovery is issued from sync route handlers, which FastAPI runs in a
# threadpool, so two requests can miss the cache at the same instant. The lock
# guards the dict, not the HTTP calls: holding it across ~40 round trips would
# serialise every cold request behind one slow cluster.
_cache_lock = threading.Lock()


def normalize_group(group: str | None) -> str:
    """Translate the §1.4 wire spelling ``core`` to the real group name ``""``.

    Applied once, at the edge. Everything downstream — path building, catalog
    lookups, preflight, audit targets — sees the real group name, so there is
    never a second place that has to remember the translation and never a
    ``group == "core"`` comparison that silently fails to match.

    Anything that is not ``core`` passes through unchanged, including the empty
    string, so calling this twice is harmless.
    """
    if group is None:
        return ""
    stripped = group.strip()
    return "" if stripped == CORE_WIRE_NAME else stripped


def wire_group(group: str | None) -> str:
    """The inverse: the spelling that can appear in a URL path segment."""
    return group.strip() if (group or "").strip() else CORE_WIRE_NAME


def raw_get(path: str, *, query: list[tuple[str, Any]] | None = None) -> dict[str, Any]:
    """Issue one authenticated GET against the cluster API server, returning JSON.

    The single place in this package where a raw REST call is made. It lives in
    ``catalog`` rather than in ``reader`` only because ``reader`` imports this
    module for :func:`resolve`, and the reverse import would be a cycle.

    Three arguments are load-bearing and easy to omit:

    * ``response_type="object"`` — with ``None`` the generated client discards
      the body and returns ``None``, which reads as "the cluster returned an
      empty resource" rather than as "we asked for nothing back".
    * ``auth_settings=["BearerToken"]`` — without it the Authorization header is
      never applied and every call comes back 401.
    * ``_return_http_data_only=True`` — otherwise the return value is a
      ``(data, status, headers)`` tuple.

    ``_preload_content`` is deliberately left at its default. Setting it False
    would be read by ``app.k8s.client._is_streaming`` as a long-lived stream and
    would apply the ten-minute watch deadline to an ordinary listing, so a
    black-holed connection would hold a threadpool worker for ten minutes
    instead of thirty seconds.
    """
    payload = get_api_client().call_api(
        path,
        "GET",
        query_params=[(key, value) for key, value in (query or []) if value is not None],
        header_params={"Accept": "application/json"},
        response_type="object",
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
    )
    if not isinstance(payload, dict):
        # A JSON body that is not an object means something other than the API
        # server answered — an authenticating proxy's error page, or a captive
        # portal. Saying so is the diagnosis; treating it as an empty result
        # would report the cluster as having no resources.
        raise UpstreamError(
            "The cluster API server returned a body that is not a Kubernetes object.",
            detail=f"GET {path} returned {type(payload).__name__}, expected a JSON object.",
            hint="Check whether a proxy in front of the API server is answering instead of it.",
            context={"path": path},
        )
    return payload


def quote_segment(value: str) -> str:
    """Percent-encode one URL path segment.

    ``ApiClient.call_api`` substitutes ``{name}`` placeholders and encodes those,
    but a path assembled by hand bypasses that entirely. Kubernetes object names
    are restricted enough that this is rarely load-bearing, but a resource name
    read back from a CRD is caller-influenced input reaching a URL, and encoding
    it at the point of assembly is cheaper than auditing every call site.
    """
    return quote(str(value), safe="")


def _resource_item(
    payload: dict[str, Any],
    *,
    group: str,
    version: str,
    preferred: bool,
) -> dict[str, Any] | None:
    """Shape one discovery ``APIResource`` into a §4 catalog item, or skip it."""
    name = payload.get("name") or ""
    if not name or _SUBRESOURCE_MARKER in name:
        return None
    # An aggregated APIService may report a group/version on the resource that
    # differs from the group-version it was fetched under. The resource's own
    # answer wins: it is the one that will route.
    item_group = payload.get("group")
    item_version = payload.get("version")
    return {
        "group": group if item_group is None else item_group,
        "version": version if item_version is None else item_version,
        "kind": payload.get("kind") or "",
        "resource": name,
        "namespaced": bool(payload.get("namespaced")),
        "verbs": list(payload.get("verbs") or []),
        "shortNames": list(payload.get("shortNames") or []),
        "categories": list(payload.get("categories") or []),
        # Convenience fields, additive to the §4 shape. `apiVersion` is what a
        # generated manifest needs and is otherwise reassembled by every caller;
        # `preferred` lets a UI pick a default when a CRD serves v1alpha1 and v1
        # side by side, which the bare list cannot express.
        "apiVersion": version if not group else f"{group}/{version}",
        "preferred": preferred,
    }


#: Why discovery is not impersonated, in one sentence the exemption test quotes.
_DISCOVERY_IS_SHARED = (
    "ADR-0007 names discovery as a ServiceAccount call: the catalog is cached "
    "per cluster and shared between every signed-in operator, so a listing made "
    "as one of them would be served to the rest."
)


def _discovery_get(path: str) -> dict[str, Any]:
    """``raw_get`` for the three discovery reads, made as the console (ADR-0007).

    **This is an exemption the ADR requires be named, and it is the one worth
    reading twice**, because the obvious objection is right and the answer is
    that the cache is the problem rather than the call.

    ``discover()`` memoises its result per ``(cluster_id, cache_key)`` and hands
    the same catalog to everybody. Impersonating it would mean whichever
    operator warmed the cache decided what every other operator sees the cluster
    as serving — and on a cache miss, an unlucky one would be told a resource
    does not exist on a cluster that serves it. Keying the cache per identity
    instead would multiply a cluster-wide round trip by the number of signed-in
    people, to answer a question whose answer does not vary by person.

    It does not vary because discovery reports what the **API** serves, not what
    the caller may do with it: ``/api`` and ``/apis`` are readable by
    ``system:authenticated`` on a default cluster, the ``verbs`` in a discovery
    document are the resource's own capabilities, and nothing here is filtered
    by RBAC. Every question that *is* about the caller — may I list this, may I
    patch that — goes through §9's preflight, which is impersonated.
    """
    with as_service_account(_DISCOVERY_IS_SHARED):
        return raw_get(path)


def _discover_group_versions(unavailable: list[dict[str, Any]]) -> list[tuple[str, str, bool]]:
    """Enumerate every served ``(group, version, is_preferred)``.

    Two independent reads: ``/api`` for the core group and ``/apis`` for
    everything else. Collected separately so losing one does not lose the other —
    an RBAC policy that denies ``/apis`` is unusual but a proxy that mangles it is
    not, and core resources are the ones the console cannot function without.
    """
    group_versions: list[tuple[str, str, bool]] = []

    with collect(unavailable, "", ALL_RESOURCES):
        payload = _discovery_get("/api")
        versions = [v for v in (payload.get("versions") or []) if v]
        for version in versions:
            group_versions.append(("", version, version == versions[0]))

    # `group="*"` because the failure is the group *list* itself: we do not know
    # which groups we failed to see, and naming one would be an invention. The
    # detail says so in words for the operator.
    with collect(unavailable, ALL_RESOURCES, ALL_RESOURCES):
        payload = _discovery_get("/apis")
        for group_payload in payload.get("groups") or []:
            name = group_payload.get("name") or ""
            preferred_gv = (group_payload.get("preferredVersion") or {}).get("groupVersion")
            for version_payload in group_payload.get("versions") or []:
                version = version_payload.get("version")
                if not version:
                    continue
                group_versions.append(
                    (name, version, version_payload.get("groupVersion") == preferred_gv)
                )

    return group_versions


def discover() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every API resource this cluster serves, plus what could not be enumerated.

    Returns ``(items, unavailable)`` for :func:`app.resources.envelope.envelope`
    to wrap. Both lists are fresh copies of the cached ones, so a caller that
    sorts, filters or appends in place cannot corrupt the next caller's catalog —
    a bug that presents as resources vanishing from unrelated pages.

    Failures reaching the cluster at all (no registered cluster, unusable stored
    credentials) are *not* collected: they are raised, because there is no
    partial answer to give and §1.1 requires ``409 no_cluster_selected`` rather
    than an empty catalog.
    """
    clients = get_clients()
    key = (clients.cluster_id, clients.cache_key)
    now = time.monotonic()

    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry.expires_at > now:
            return [dict(item) for item in entry.items], [dict(u) for u in entry.unavailable]

    unavailable: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    for group, version, preferred in _discover_group_versions(unavailable):
        path = f"/api/{quote_segment(version)}" if not group else (
            f"/apis/{quote_segment(group)}/{quote_segment(version)}"
        )
        with collect(unavailable, group, ALL_RESOURCES):
            payload = _discovery_get(path)
            for resource_payload in payload.get("resources") or []:
                item = _resource_item(
                    resource_payload, group=group, version=version, preferred=preferred
                )
                if item is not None:
                    items.append(item)

    # Stable order: the catalog drives a picker, and a list whose order changes
    # between two identical requests makes the picker jump under the cursor.
    items.sort(key=lambda item: (item["group"], item["resource"], item["version"]))

    ttl = _TTL_PARTIAL_SECONDS if unavailable else _TTL_COMPLETE_SECONDS
    with _cache_lock:
        # Evict expired entries for clusters nobody is looking at any more, so a
        # long-lived process that has served a large fleet does not hold a
        # catalog per cluster per configuration revision forever.
        for stale_key in [k for k, v in _cache.items() if v.expires_at <= now and k != key]:
            _cache.pop(stale_key, None)
        _cache[key] = _CatalogEntry(
            items=items, unavailable=unavailable, expires_at=time.monotonic() + ttl
        )

    logger.info(
        "Discovered %d API resources on cluster %s (%d group(s) unavailable)",
        len(items), clients.cluster_id, len(unavailable),
    )
    return [dict(item) for item in items], [dict(u) for u in unavailable]


def invalidate_cache(cluster_id: int | None = None, *, all_clusters: bool = False) -> None:
    """Drop cached discovery, for one cluster or for all of them.

    Called when a cluster's registration changes and by tests, which would
    otherwise share one process-wide catalog between cases and see the first
    case's stubbed discovery in the second.
    """
    with _cache_lock:
        if all_clusters:
            _cache.clear()
            return
        for key in [k for k in _cache if k[0] == cluster_id]:
            _cache.pop(key, None)


def _blind_spot(unavailable: list[dict[str, Any]], group: str) -> dict[str, Any] | None:
    """The ``unavailable`` entry, if any, that covers ``group``.

    ``"*"`` is what ``_discover_group_versions`` records when the group *list*
    itself could not be read, and in that state we know nothing about any
    non-core group — so it matches all of them.

    It deliberately does **not** match the core group. The core group comes from
    ``/api`` and every other group from ``/apis``; those are two independent
    reads, collected separately for exactly this reason, and a failure of the
    second says nothing about the first. Letting ``"*"`` cover the core group
    turns a typo in a core plural (``podz``) into "an aggregated APIService is
    down, so we cannot tell whether this cluster has pods" during an aggregation
    outage — a 502 sending the operator to debug an APIService, where the honest
    answer is a 404 saying the resource does not exist. A core-group failure is
    recorded under its own real name, ``""``, and is matched by the first test.
    """
    for entry in unavailable:
        if entry["group"] == group:
            return entry
        if entry["group"] == ALL_RESOURCES and group != "":
            return entry
    return None


def _error_for_blind_spot(entry: dict[str, Any], context: dict[str, Any]) -> AdminError:
    """Turn a discovery failure back into the error the caller should see.

    The whole point of :func:`resolve` refusing to answer here is that
    ``unsupported`` ("this cluster does not have that API") and "we could not
    find out" are different answers with different fixes. Reconstructing the
    original class keeps the §1.3 status code and hint accurate: a *forbidden*
    discovery stays a 403 telling the operator which grant to add, rather than
    becoming a 501 telling them to install something.
    """
    reason = entry["reason"]
    detail = entry.get("detail")
    if reason == "forbidden":
        return RBACDenied(
            "The console is not permitted to discover that API group.",
            detail=detail, context=context,
        )
    if reason in ("unreachable", "timeout"):
        return ClusterUnreachable(
            "That API group could not be enumerated, so whether the cluster "
            "serves this resource is unknown.",
            detail=detail,
            hint=(
                "An aggregated APIService for this group is not answering. Retry, "
                "or check the APIService's backing service."
            ),
            context={**context, "cause": "unreachable" if reason == "unreachable" else "timeout"},
        )
    return UpstreamError(
        "That API group could not be enumerated.", detail=detail, context=context,
    )


def resolve(group: str, version: str, plural: str) -> dict[str, Any]:
    """Look up one ``group/version/plural`` in the catalog.

    Returns the §4 catalog item, which carries the ``namespaced`` flag and the
    ``verbs`` list every read and write needs before it builds a URL.

    Raises:
        AdminError: whatever discovery actually failed with, when the requested
            group is one that could not be enumerated. Reporting ``unsupported``
            in that state would tell an operator their cluster does not serve a
            resource it may well serve.
        NotFound: the group-version is served and does not list that plural — a
            typo, or a resource whose CRD was removed. 404, because the thing
            asked for does not exist.
        Unsupported: the group-version itself is not served — no Ingress
            controller CRDs, no ``metrics.k8s.io``. 501, which §1.2 says the UI
            renders as "not present on this cluster" and deliberately does not
            colour red.
    """
    normalized = normalize_group(group)
    context = {"group": normalized, "resource": plural, "version": version}
    items, unavailable = discover()

    for item in items:
        if (
            item["group"] == normalized
            and item["version"] == version
            and item["resource"] == plural
        ):
            return item

    blind = _blind_spot(unavailable, normalized)
    if blind is not None:
        raise _error_for_blind_spot(blind, context)

    if any(item["group"] == normalized and item["version"] == version for item in items):
        served = sorted(
            item["resource"]
            for item in items
            if item["group"] == normalized and item["version"] == version
        )
        raise NotFound(
            f"{version} of the "
            f"{normalized or 'core'} API group does not have a resource named "
            f"{plural!r}.",
            detail=f"Resources served by that group-version: {', '.join(served)}.",
            hint="Check the plural spelling against GET /api/resources/catalog.",
            context=context,
        )

    versions = sorted({item["version"] for item in items if item["group"] == normalized})
    hint = (
        f"That group serves {', '.join(versions)} on this cluster."
        if versions
        else "The group is not served by this cluster at all."
    )
    raise Unsupported(
        f"This cluster does not serve {normalized or 'core'}/{version}.",
        hint=hint,
        context=context,
    )


__all__ = [
    "CORE_WIRE_NAME",
    "discover",
    "invalidate_cache",
    "normalize_group",
    "quote_segment",
    "raw_get",
    "resolve",
    "wire_group",
]
