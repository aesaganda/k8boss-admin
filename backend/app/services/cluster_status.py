"""
Cluster status (§19) — the control plane's own health, from what vanilla serves.

OpenShift answers "is this cluster healthy" with `ClusterOperator` and
`ClusterVersion`: one object per platform component, each with `Available`,
`Degraded` and `Progressing`, rolled up into a single line at the top of the
console. Vanilla Kubernetes has nothing of the sort. It has five *unrelated*
APIs that between them answer most of the same question, and an operator on a
vanilla cluster reaches them with five different `kubectl` invocations and a
lot of `jq`.

This module is those five reads, joined. It is **not** a monitoring system: no
history, no thresholds anyone can configure, no alerting, and every number is a
live read taken when the page loaded.

**What is here, and why each one is an honest vanilla signal.**

* **Leader-election leases.** `kube-controller-manager` and `kube-scheduler`
  hold `coordination.k8s.io` Leases in `kube-system` and renew them every few
  seconds. A lease whose `renewTime` is older than its own
  `leaseDurationSeconds` means the holder stopped renewing — which is what a
  wedged controller-manager looks like from the API server's side.
* **Aggregated APIServices.** An `APIService` with a `spec.service` is served by
  a pod, not by the API server, and its `Available` condition is that pod
  answering. This is precisely why `metrics.k8s.io` disappears, and the rest of
  the console reports that absence as an ordinary fact (§1.3 `unsupported`)
  without ever being able to say *why*. This page says why.
* **CustomResourceDefinitions.** A CRD whose `Established` condition is not true
  serves nothing, and one carrying `NonStructuralSchema` cannot be converted or
  pruned. Both are invisible until something that needs the resource fails.
* **Admission webhooks.** A `ValidatingWebhookConfiguration` with
  `failurePolicy: Fail` whose backing Service has no endpoints **rejects every
  write it intercepts, cluster-wide**. It is the single most effective way to
  break a Kubernetes cluster without touching a node, and nothing surfaces it.
* **Version skew.** A kubelet more than three minors behind the API server is
  outside what Kubernetes supports, and the failure mode is a feature that
  silently does not work on that node rather than an error anywhere.

**What is deliberately absent.**

*etcd.* No vanilla API reports etcd health to a client with ordinary RBAC. The
API server's own `/readyz` includes an etcd check and is not something this
console can read on most clusters. Reporting "etcd: unknown" in a table of
things that are known would be filler; the page says nothing about etcd because
this console knows nothing about etcd.

*Reachability.* This module never claims a webhook, an aggregated API or a
controller is *reachable*. It reports what the API server records about them —
a condition it wrote, an endpoint count, a renewal timestamp. Those are facts;
reachability from here would be a guess about a network this console is not on.

*A missing lease is not a missing component.* On EKS, GKE, AKS and every other
managed control plane, the scheduler and controller-manager run somewhere the
customer cannot see, and `kube-system` may hold no lease for them at all. So
this module lists the leases that **exist** and says nothing about the ones that
do not. A page reading "kube-scheduler: MISSING" on a healthy EKS cluster would
be the confident wrong answer this project treats as a defect.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from app.k8s.client import get_core_v1, get_version_api
from app.resources import catalog, reader
from app.resources.envelope import collect
from app.resources.shaping import age_seconds, get_field, rfc3339

logger = logging.getLogger(__name__)

#: Where leader-election leases live. Not configurable: it is where the
#: components Kubernetes ships put them, and a console that let an operator
#: point this at another namespace would be offering a setting whose only
#: correct value is this one.
CONTROL_PLANE_NAMESPACE = "kube-system"

#: Leases every conformant self-hosted control plane has, used **only** to sort
#: the familiar ones to the top. Absence is never reported as a fault — see the
#: module docstring on managed control planes.
WELL_KNOWN_LEASES = ("kube-controller-manager", "kube-scheduler", "cloud-controller-manager")

#: How far behind the API server a kubelet may be. Kubernetes' documented skew
#: policy is n-3 minors as of 1.28 (it was n-2 before), and a kubelet *ahead* of
#: the API server is unsupported at any distance.
KUBELET_MINORS_BEHIND = 3

#: `v1.31.4+abc`, `v1.31.4-eks-1234`, `1.31.4`. The suffixes distributions add
#: are why this is a prefix match rather than a parse of the whole string.
_VERSION_RE = re.compile(r"v?(?P<major>\d+)\.(?P<minor>\d+)")


# --------------------------------------------------------------------------- #
# Reading a group that may not be served
# --------------------------------------------------------------------------- #

def _list(
    unavailable: list[dict[str, Any]],
    group: str,
    version: str,
    plural: str,
    *,
    namespace: str | None = None,
    limit: int = 500,
) -> list[Any] | None:
    """Every object of one kind, or ``None`` when the read did not happen.

    ``None`` and ``[]`` are different answers all the way to the response.
    ``[]`` means the cluster has none of the thing — a cluster with no admission
    webhooks is an ordinary cluster — and ``None`` means we could not look, in
    which case a section that rendered "none" would be telling an operator that
    nothing is intercepting their writes when something may well be.

    A group discovery does not serve is an ``unsupported`` entry rather than an
    error: ``admissionregistration.k8s.io`` is on every cluster and
    ``apiregistration.k8s.io`` is not quite, and neither absence is a failure of
    anything.
    """
    items: list[Any] | None = None
    with collect(unavailable, group, plural, namespace=namespace):
        catalog.resolve(group, version, plural)
        listing = reader.list_resource(
            group, version, plural, namespace=namespace, limit=limit,
        )
        items = list(listing.get("items") or [])
    return items


def _condition(obj: Any, wanted: str) -> dict[str, Any] | None:
    """One condition off an object's status, or ``None`` if it has not been set.

    ``None`` is a real state and not the same as ``False``: a controller that
    has not yet written a condition has told us nothing, and rendering that as
    "not available" would report a resource as broken during the seconds after
    it was created.
    """
    for condition in get_field(obj, "status", "conditions", default=[]) or []:
        if get_field(condition, "type") == wanted:
            return {
                "status": get_field(condition, "status"),
                "reason": get_field(condition, "reason"),
                "message": get_field(condition, "message"),
                "since": rfc3339(get_field(condition, "lastTransitionTime")),
            }
    return None


# --------------------------------------------------------------------------- #
# 1. Leader-election leases
# --------------------------------------------------------------------------- #

def _lease_row(lease: Any) -> dict[str, Any]:
    """One Lease in `kube-system`, with whether its holder is still renewing.

    ``stale`` is the whole point and it is a tri-state. A lease renews every
    ``leaseDurationSeconds``; one whose ``renewTime`` is older than that has a
    holder that stopped. ``None`` when either half is missing — a lease with no
    ``renewTime`` has never been acquired, and calling that stale would report a
    component as wedged on the strength of a field nobody wrote.
    """
    spec = get_field(lease, "spec", default={}) or {}
    renew = get_field(spec, "renewTime")
    duration = get_field(spec, "leaseDurationSeconds")
    since_renew = age_seconds(renew)

    stale: bool | None = None
    if since_renew is not None and isinstance(duration, int) and duration > 0:
        stale = since_renew > duration

    return {
        "name": get_field(lease, "metadata", "name"),
        "holder": get_field(spec, "holderIdentity"),
        "renewTime": rfc3339(renew),
        "seconds_since_renew": since_renew,
        "lease_duration_seconds": duration if isinstance(duration, int) else None,
        "stale": stale,
        "well_known": get_field(lease, "metadata", "name") in WELL_KNOWN_LEASES,
    }


def _control_plane(unavailable: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Leases in `kube-system`, well-known ones first.

    Every lease, not a filtered set: a cluster runs leader-elected components
    this console has never heard of — a cloud provider's, an operator's — and
    the one that stopped renewing is exactly as interesting as
    `kube-scheduler`.
    """
    leases = _list(
        unavailable, "coordination.k8s.io", "v1", "leases",
        namespace=CONTROL_PLANE_NAMESPACE,
    )
    if leases is None:
        return None
    rows = [_lease_row(lease) for lease in leases]
    rows.sort(key=lambda row: (not row["well_known"], row["name"] or ""))
    return rows


# --------------------------------------------------------------------------- #
# 2. Aggregated APIServices
# --------------------------------------------------------------------------- #

def _api_service_row(api_service: Any) -> dict[str, Any]:
    service = get_field(api_service, "spec", "service", default=None)
    available = _condition(api_service, "Available")
    return {
        "name": get_field(api_service, "metadata", "name"),
        "group": get_field(api_service, "spec", "group"),
        "version": get_field(api_service, "spec", "version"),
        "service": None if not service else {
            "namespace": get_field(service, "namespace"),
            "name": get_field(service, "name"),
        },
        "available": None if available is None else available["status"] == "True",
        "reason": None if available is None else available["reason"],
        "message": None if available is None else available["message"],
        "since": None if available is None else available["since"],
    }


def _api_services(unavailable: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The aggregated APIServices, and a count of the local ones.

    Only the aggregated ones are listed. An APIService with no ``spec.service``
    is served by the API server itself and is `Available` on every cluster that
    is answering at all — thirty rows of guaranteed-green, which is how a table
    stops being read. They are counted so the total still adds up.
    """
    items = _list(unavailable, "apiregistration.k8s.io", "v1", "apiservices")
    if items is None:
        return None

    aggregated = [
        item for item in items
        if get_field(item, "spec", "service", default=None)
    ]
    rows = [_api_service_row(item) for item in aggregated]
    rows.sort(key=lambda row: (row["available"] is not False, row["name"] or ""))
    return {
        "items": rows,
        "local_count": len(items) - len(aggregated),
        "unavailable_count": sum(1 for row in rows if row["available"] is False),
    }


# --------------------------------------------------------------------------- #
# 3. CustomResourceDefinitions
# --------------------------------------------------------------------------- #

def _crd_row(crd: Any) -> dict[str, Any]:
    established = _condition(crd, "Established")
    non_structural = _condition(crd, "NonStructuralSchema")
    return {
        "name": get_field(crd, "metadata", "name"),
        "group": get_field(crd, "spec", "group"),
        "established": None if established is None else established["status"] == "True",
        "reason": None if established is None else established["reason"],
        "message": None if established is None else established["message"],
        # True is the *bad* value for this one: the condition exists to say a
        # schema is not structural, and its absence is the healthy state.
        "non_structural": (
            None if non_structural is None else non_structural["status"] == "True"
        ),
    }


def _crds(unavailable: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The CRDs worth looking at, and how many there are in total.

    Only the ones that are **not** healthy are listed. A cluster running a
    handful of operators has several hundred CRDs and all but a few are
    `Established` and structural; a table of those is a table nobody reads, and
    a page nobody reads is a page that hides the two rows that matter.

    ``total`` is reported alongside so "three problems" is anchored to something
    — three out of four hundred and three out of five are different mornings.
    """
    items = _list(unavailable, "apiextensions.k8s.io", "v1", "customresourcedefinitions")
    if items is None:
        return None

    rows = [_crd_row(crd) for crd in items]
    unhealthy = [
        row for row in rows
        if row["established"] is not True or row["non_structural"] is True
    ]
    unhealthy.sort(key=lambda row: row["name"] or "")
    return {"items": unhealthy, "total": len(rows), "unhealthy_count": len(unhealthy)}


# --------------------------------------------------------------------------- #
# 4. Admission webhooks
# --------------------------------------------------------------------------- #

def _webhook_rows(configuration: Any, kind: str) -> list[dict[str, Any]]:
    """One row per webhook inside one configuration object.

    Per webhook rather than per configuration, because `failurePolicy` and the
    backing service are set on each webhook and one configuration routinely
    holds several with different answers.
    """
    rows = []
    for webhook in get_field(configuration, "webhooks", default=[]) or []:
        client_config = get_field(webhook, "clientConfig", default={}) or {}
        service = get_field(client_config, "service", default=None)
        rows.append({
            "kind": kind,
            "configuration": get_field(configuration, "metadata", "name"),
            "name": get_field(webhook, "name"),
            "failure_policy": get_field(webhook, "failurePolicy") or "Fail",
            "timeout_seconds": get_field(webhook, "timeoutSeconds"),
            "side_effects": get_field(webhook, "sideEffects"),
            # A webhook addressed by URL is somewhere this console cannot look.
            # `url` and `service` are mutually exclusive in the API.
            "url": get_field(client_config, "url"),
            "service": None if not service else {
                "namespace": get_field(service, "namespace"),
                "name": get_field(service, "name"),
                "port": get_field(service, "port"),
            },
            "endpoint_count": None,
        })
    return rows


def _endpoint_counts(
    rows: Iterable[dict[str, Any]], unavailable: list[dict[str, Any]],
) -> dict[tuple[str, str], int] | None:
    """Ready endpoints for each Service a webhook points at, or ``None``.

    ``None`` for the whole tally rather than zero for each row, for §0.1's
    reason: "this webhook's backend has no endpoints" is the finding this
    section exists for, and a listing that failed must not produce it.
    """
    targets = {
        (row["service"]["namespace"], row["service"]["name"])
        for row in rows
        if row["service"] and row["service"].get("namespace") and row["service"].get("name")
    }
    if not targets:
        return {}

    counts: dict[tuple[str, str], int] | None = None
    with collect(unavailable, "discovery.k8s.io", "endpointslices"):
        # Tallied into a local and assigned to `counts` only on the last line of
        # the block. `collect()` leaves the variable it was assigning at None
        # when the call raises, and that only holds if nothing is assigned
        # *before* the read — a `counts = {}` up here would survive the failure
        # as an empty tally, which is exactly the "we could not look" that reads
        # as "nothing there".
        tally: dict[tuple[str, str], int] = {}
        listing = reader.list_resource(
            "discovery.k8s.io", "v1", "endpointslices", namespace=None, limit=500,
        )
        for slice_ in listing.get("items") or []:
            namespace = get_field(slice_, "metadata", "namespace")
            owner = get_field(
                slice_, "metadata", "labels", default={},
            ) or {}
            name = owner.get("kubernetes.io/service-name")
            if (namespace, name) not in targets:
                continue
            ready = 0
            for endpoint in get_field(slice_, "endpoints", default=[]) or []:
                conditions = get_field(endpoint, "conditions", default={}) or {}
                # `ready` absent means ready, per the EndpointSlice API: the
                # field is optional and its default is true.
                if conditions.get("ready") is not False:
                    ready += len(get_field(endpoint, "addresses", default=[]) or [])
            tally[(namespace, name)] = tally.get((namespace, name), 0) + ready
        # A Service with no EndpointSlice at all has no backends, which is a
        # real zero and the finding this section is for — distinct from the None
        # the whole tally becomes when the listing did not answer.
        for target in targets:
            tally.setdefault(target, 0)
        counts = tally
    return counts


def _webhooks(unavailable: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Every admission webhook, with what happens to writes if its backend is gone.

    The row that matters is ``failurePolicy: Fail`` with ``endpoint_count: 0``:
    that webhook rejects every write matching its rules, across the whole
    cluster, until its backend comes back. It is the most effective way to break
    a Kubernetes cluster without touching a node, and nothing else in this
    console — or in `kubectl` — puts it in front of anyone.

    ``endpoint_count`` is ``None`` for a URL-addressed webhook, and that is not
    a failure to look: there is nothing in the cluster to look at. The row says
    so rather than showing a zero that would read as "broken".
    """
    validating = _list(
        unavailable, "admissionregistration.k8s.io", "v1",
        "validatingwebhookconfigurations",
    )
    mutating = _list(
        unavailable, "admissionregistration.k8s.io", "v1",
        "mutatingwebhookconfigurations",
    )
    if validating is None and mutating is None:
        return None

    rows: list[dict[str, Any]] = []
    for item in validating or []:
        rows.extend(_webhook_rows(item, "ValidatingWebhookConfiguration"))
    for item in mutating or []:
        rows.extend(_webhook_rows(item, "MutatingWebhookConfiguration"))

    counts = _endpoint_counts(rows, unavailable)
    for row in rows:
        service = row["service"]
        if counts is not None and service:
            row["endpoint_count"] = counts.get(
                (service.get("namespace"), service.get("name"))
            )

    # `None`, not `0`, when the endpoint listing did not answer: with every
    # `endpoint_count` at None nothing matches the predicate, and "0 webhooks are
    # refusing writes" derived from a read that failed is the reassuring wrong
    # answer §0.1 exists to forbid — on the one finding this section is for.
    blocking: list[dict[str, Any]] | None = None
    if counts is not None:
        blocking = [
            row for row in rows
            if row["failure_policy"] == "Fail" and row["endpoint_count"] == 0
        ]
    rows.sort(key=lambda row: (
        not (row["failure_policy"] == "Fail" and row["endpoint_count"] == 0),
        row["configuration"] or "",
        row["name"] or "",
    ))
    return {
        "items": rows,
        # Named for what it is: these webhooks are refusing writes right now, on
        # the evidence of their own configuration and an endpoint listing.
        "blocking_count": None if blocking is None else len(blocking),
        # `partial` on the section: some configuration listing did not answer,
        # so the rows below are not all of them.
        "complete": validating is not None and mutating is not None,
    }


# --------------------------------------------------------------------------- #
# 5. Version skew
# --------------------------------------------------------------------------- #

def _minor(version: Any) -> tuple[int, int] | None:
    """``(major, minor)`` from a Kubernetes version string, or ``None``.

    Distributions append their own suffixes — ``v1.31.4+rke2r1``,
    ``v1.30.6-eks-abc1234`` — so this matches the prefix rather than parsing the
    whole string. A version it cannot read is ``None``, and every comparison
    against ``None`` is ``unknown`` rather than a guess.
    """
    if not version:
        return None
    match = _VERSION_RE.match(str(version).strip())
    if match is None:
        return None
    return int(match.group("major")), int(match.group("minor"))


def _skew_status(server: tuple[int, int] | None, kubelet: tuple[int, int] | None) -> str:
    """``ok``, ``behind``, ``ahead`` or ``unknown`` for one node.

    ``unknown`` whenever either version could not be read. The skew rule is
    about numbers, and a rule applied to a number we do not have produces a
    verdict about a node nobody checked.
    """
    if server is None or kubelet is None:
        return "unknown"
    if kubelet > server:
        # Supported nowhere, at any distance: a kubelet must never be newer than
        # the API server it talks to.
        return "ahead"
    if server[0] != kubelet[0] or (server[1] - kubelet[1]) > KUBELET_MINORS_BEHIND:
        return "behind"
    return "ok"


def _version_skew(unavailable: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The API server's version beside every kubelet's.

    A kubelet more than three minors behind is outside Kubernetes' supported
    skew, and what that costs is not an error message anywhere: it is a feature
    the cluster has that silently does not work on that node.
    """
    server_version: str | None = None
    with collect(unavailable, "", "version"):
        server_version = get_field(get_version_api().get_code(), "git_version")

    nodes: list[Any] | None = None
    with collect(unavailable, "", "nodes"):
        nodes = list(
            get_field(get_core_v1().list_node(), "items", default=[]) or []
        )

    if server_version is None and nodes is None:
        return None

    server = _minor(server_version)
    rows = []
    for node in nodes or []:
        kubelet = get_field(node, "status", "node_info", "kubelet_version")
        rows.append({
            "node": get_field(node, "metadata", "name"),
            "kubelet_version": kubelet,
            "status": _skew_status(server, _minor(kubelet)),
        })
    rows.sort(key=lambda row: (row["status"] == "ok", row["node"] or ""))
    return {
        "server_version": server_version,
        "supported_minors_behind": KUBELET_MINORS_BEHIND,
        "nodes": rows if nodes is not None else None,
        # `None`, not `0`, when the node listing failed: `rows` is empty then,
        # and "no node is outside the supported skew" is a claim about nodes
        # nobody looked at. The API server's version alone is still worth
        # showing, so the section stays rather than disappearing.
        "out_of_skew_count": (
            None if nodes is None
            else sum(1 for row in rows if row["status"] in ("behind", "ahead"))
        ),
    }


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def get_cluster_status() -> dict[str, Any]:
    """``GET /api/cluster-status`` (§19). Five independent reads, joined.

    Each section is collected on its own, so one refused listing costs that
    section and nothing else — its key is ``null``, the reason is in
    ``unavailable[]``, and ``partial`` is true. A section that is ``null`` is
    never rendered as healthy: "we could not read the admission webhooks" and
    "this cluster has no admission webhooks" are answers an operator acts on
    very differently, and only the second one is good news.

    There is no aggregate verdict, no ``healthy: true``. Every attempt to write
    one runs into the same wall: a cluster with a stale `cloud-controller-manager`
    lease and no aggregated APIs is fine on some clusters and an outage on
    others, and a single boolean would have to pick. The page shows the findings
    and the operator does the judging, which is the only division of labour this
    console can honestly offer.
    """
    unavailable: list[dict[str, Any]] = []

    sections = {
        "controlPlane": _control_plane(unavailable),
        "apiServices": _api_services(unavailable),
        "crds": _crds(unavailable),
        "webhooks": _webhooks(unavailable),
        "versionSkew": _version_skew(unavailable),
    }
    return {
        **sections,
        # Hand-built rather than through envelope(): this is five sections, not
        # a collection. Kept adjacent to the list it is derived from so the two
        # cannot drift, the way §17's namespace read does it.
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


__all__ = [
    "CONTROL_PLANE_NAMESPACE",
    "KUBELET_MINORS_BEHIND",
    "get_cluster_status",
]
