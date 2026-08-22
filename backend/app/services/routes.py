"""
The unified route model (§13) — one row for every way a cluster exposes a
Service to the outside world.

Three unrelated APIs answer the same operator question. OpenShift serves
``route.openshift.io/v1`` Routes; every cluster with an ingress controller
serves ``networking.k8s.io/v1`` Ingresses; a cluster with Gateway API installed
serves ``gateway.networking.k8s.io`` HTTPRoutes. An operator asking "what is
reachable from outside, and where does it go" does not care which of the three
a given exposure happens to be written in — but they do care, very much, when
the console's answer is missing one of them.

So this module reads all three and shapes them into one row. What it will not
do is pretend they are the same thing: every row carries the ``backend`` it came
from, and :func:`capabilities` reports, per backend, exactly which of the six
exposure features that backend can express on *this* cluster.

Three decisions here are the whole reason the module is careful.

**"This cluster does not serve Routes" and "we could not find out" are
different answers, and they are kept apart by construction.** Every backend is
resolved through :func:`app.resources.catalog.resolve`, which already refuses to
report ``unsupported`` for a group it could not enumerate — it re-raises the
real failure instead. :func:`backend_state` maps that distinction onto three
states and never collapses them:

===============  =============================================================
``available``    discovery lists the resource. It can be read and written.
``unsupported``  discovery answered, and the group-version or the resource is
                 not there. An ordinary fact about a cluster, not an error.
``unknown``      discovery could not answer for that group. We do **not** know
                 whether the cluster serves it.
===============  =============================================================

Getting this backwards is the failure the whole state machine exists to
prevent: during an aggregated-API outage, telling an operator "this cluster has
no Routes" sends them to install an API they already have, or — far worse —
to re-create an exposure that already exists under a hostname that is already
claimed.

**Only ``unknown`` makes the listing partial.** ``unsupported`` deliberately
does *not* go in ``unavailable[]``. A cluster that does not serve
``route.openshift.io`` is not a cluster whose Routes we failed to read; there
are none, and ``items: []`` for that backend is the true answer. Putting it in
``unavailable[]`` would raise the §1.2 partial banner on the Routes page of
every non-OpenShift cluster in the world, and a banner that is always up is a
banner nobody reads — the same reasoning §1.2 gives for not colouring
``unsupported`` red. The per-backend state is reported in its own ``backends[]``
field instead, where the UI renders it as "not present on this cluster".

**``admitted`` is a tri-state and the third state is the common one.** A router
that has not yet written a status for an exposure has told us nothing about it.
Reporting that as ``false`` says the router *rejected* it, which is a specific
and alarming claim; reporting it as ``true`` is the green-row lie §6 already
refuses for workloads. It is ``None`` until a router says otherwise, and the UI
renders that as "no router has reported on this yet" — which is also the honest
description of an Ingress on a cluster with no ingress controller installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from app.errors import AdminError, Invalid, NotFound, Unsupported
from app.resources import catalog, reader
from app.resources.envelope import (
    collect,
    envelope,
    reason_for_error,
    unavailable_entry,
)
from app.resources.shaping import age_seconds, get_field

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# The exposure feature vocabulary
# --------------------------------------------------------------------------- #

#: The features an exposure can ask for, as stable tokens. The frontend branches
#: on these to grey out a form control with the reason (rule 11.4), so they are
#: part of the contract and not an internal enum: renaming one silently disables
#: a control that should be live.
#:
#: They are deliberately phrased as *what the operator wants*, not as the field
#: that happens to carry it on one of the three APIs. "Terminate TLS at the
#: router" is one intention whether it is written as ``spec.tls.termination:
#: edge`` on a Route or as ``spec.tls[]`` plus a Secret on an Ingress.
FEATURE_EDGE_TLS = "edge-tls"
FEATURE_PASSTHROUGH_TLS = "passthrough-tls"
FEATURE_REENCRYPT_TLS = "reencrypt-tls"
FEATURE_INSECURE_REDIRECT = "insecure-redirect"
FEATURE_INSECURE_ALLOW = "insecure-allow"
FEATURE_WEIGHTED_BACKENDS = "weighted-backends"
FEATURE_WILDCARD_SUBDOMAIN = "wildcard-subdomain"
FEATURE_GENERATED_HOST = "generated-host"
FEATURE_PATH_EXACT = "path-exact"

ALL_FEATURES: tuple[str, ...] = (
    FEATURE_EDGE_TLS,
    FEATURE_PASSTHROUGH_TLS,
    FEATURE_REENCRYPT_TLS,
    FEATURE_INSECURE_REDIRECT,
    FEATURE_INSECURE_ALLOW,
    FEATURE_WEIGHTED_BACKENDS,
    FEATURE_WILDCARD_SUBDOMAIN,
    FEATURE_GENERATED_HOST,
    FEATURE_PATH_EXACT,
)

#: One human sentence per feature, for the UI and for the ``lossy[]`` entries
#: :mod:`app.admin.routes` produces. Held here rather than in the frontend so
#: that the reason an operator reads on a greyed-out control and the reason they
#: read in a refusal are the same words.
FEATURE_LABELS: dict[str, str] = {
    FEATURE_EDGE_TLS: "Terminate TLS at the router (edge)",
    FEATURE_PASSTHROUGH_TLS: "Pass TLS through to the pod without terminating it",
    FEATURE_REENCRYPT_TLS: "Terminate TLS at the router and re-encrypt to the pod",
    FEATURE_INSECURE_REDIRECT: "Redirect plain HTTP to HTTPS",
    FEATURE_INSECURE_ALLOW: "Serve the same content on plain HTTP as well as HTTPS",
    FEATURE_WEIGHTED_BACKENDS: "Split traffic across several Services by weight",
    FEATURE_WILDCARD_SUBDOMAIN: "Answer for every subdomain of the hostname",
    FEATURE_GENERATED_HOST: "Let the router pick the hostname",
    FEATURE_PATH_EXACT: "Match the path exactly rather than as a prefix",
}


# --------------------------------------------------------------------------- #
# The three backends
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RouteBackend:
    """One API this console can write an exposure in.

    ``versions`` is a preference order, not a single value, because two of the
    three are CRDs whose served version depends on which release of the API the
    cluster installed. Pinning ``gateway.networking.k8s.io/v1`` would 404 on a
    cluster serving only ``v1beta1`` — and a 404 reads as "this cluster has no
    HTTPRoutes", which is the confidently wrong answer this module exists to
    avoid. The first version discovery actually serves is the one used.

    ``features`` is what the *kind* can express. Whether a controller is
    installed that will act on it is a different question, answered by the
    exposures' own ``admitted`` field and by :mod:`app.admin.router`.
    """

    key: str
    kind: str
    group: str
    versions: tuple[str, ...]
    plural: str
    label: str
    features: frozenset[str]
    summary: str


BACKENDS: tuple[RouteBackend, ...] = (
    RouteBackend(
        key="openshift",
        kind="Route",
        group="route.openshift.io",
        versions=("v1",),
        plural="routes",
        label="OpenShift Route",
        # The richest of the three, and the reason the console's form uses its
        # vocabulary: every feature an operator can ask for here is one field on
        # one object, with no controller-specific annotation anywhere.
        features=frozenset(ALL_FEATURES) - frozenset({FEATURE_PATH_EXACT}),
        summary=(
            "The native OpenShift exposure. One object carries the hostname, the "
            "path, the target Service, the TLS termination mode and the traffic "
            "split, and the cluster's own router serves it."
        ),
    ),
    RouteBackend(
        key="ingress",
        kind="Ingress",
        group="networking.k8s.io",
        versions=("v1",),
        plural="ingresses",
        label="Ingress",
        # Deliberately short. Ingress can terminate TLS at the edge with a
        # Secret, and it can match a path exactly. Everything else in the
        # vocabulary — passthrough, reencrypt, the HTTP redirect, weighted
        # backends — exists only as controller-specific annotations, which are
        # not portable and are therefore not claimed here. `app.admin.routes`
        # names each one it had to drop rather than emitting an annotation that
        # happens to work on the controller the author was testing against.
        features=frozenset({FEATURE_EDGE_TLS, FEATURE_PATH_EXACT}),
        summary=(
            "The portable exposure every ingress controller understands. It "
            "terminates TLS at the edge with a Secret and routes by host and "
            "path; the richer modes are controller-specific and this console "
            "will not write them behind your back."
        ),
    ),
    RouteBackend(
        key="gateway",
        kind="HTTPRoute",
        group="gateway.networking.k8s.io",
        versions=("v1", "v1beta1"),
        plural="httproutes",
        label="Gateway API HTTPRoute",
        # TLS is not on an HTTPRoute at all — it is on the Gateway's listener,
        # which is a separate object with a separate owner, and is why no TLS
        # feature is claimed here. The redirect is a first-class filter
        # (`RequestRedirect`), and weights are a first-class field on
        # `backendRefs`, so both are real.
        features=frozenset({
            FEATURE_INSECURE_REDIRECT,
            FEATURE_WEIGHTED_BACKENDS,
            FEATURE_PATH_EXACT,
        }),
        summary=(
            "The upstream successor to Ingress. Traffic splitting and redirects "
            "are first-class fields rather than annotations; TLS belongs to the "
            "Gateway's listener, which is a separate object this console does "
            "not write from here."
        ),
    ),
)

BACKENDS_BY_KEY: dict[str, RouteBackend] = {b.key: b for b in BACKENDS}


def resolve_backend(key: str) -> RouteBackend:
    """The backend named by ``key``, or 422 listing the ones that exist.

    422 rather than 404: the caller sent a parameter this API does not define,
    which is a bad request, not a missing object. A 404 here would read as "that
    backend is not on this cluster", which is the answer :func:`backend_state`
    gives and means something entirely different.
    """
    backend = BACKENDS_BY_KEY.get(key)
    if backend is None:
        raise Invalid(
            f"{key!r} is not a route backend this console knows.",
            detail=f"Known backends: {', '.join(sorted(BACKENDS_BY_KEY))}.",
            hint="Use GET /api/routes/capabilities to see which of them this cluster serves.",
            context={"parameter": "backend", "value": key},
        )
    return backend


# --------------------------------------------------------------------------- #
# Capability detection
# --------------------------------------------------------------------------- #

STATE_AVAILABLE = "available"
STATE_UNSUPPORTED = "unsupported"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackendState:
    """Whether this cluster serves one backend, and how we know.

    ``error`` is populated only for :data:`STATE_UNKNOWN`, and it is the real
    failure discovery raised — not a synthesised one. The caller turns it into
    the ``unavailable[]`` entry, so the reason token an operator sees
    (``forbidden``, ``unreachable``, ``timeout``) is the one that actually
    happened rather than a flattened "we could not look".
    """

    backend: RouteBackend
    state: str
    version: str | None
    detail: str
    error: AdminError | None = None

    @property
    def available(self) -> bool:
        return self.state == STATE_AVAILABLE


def backend_state(backend: RouteBackend) -> BackendState:
    """Resolve one backend against this cluster's discovery.

    Each candidate version is tried in preference order. The *classification of
    the failures* is the part that matters:

    * ``Unsupported`` — discovery answered and the group-version is not served.
    * ``NotFound`` — the group-version is served and does not list the resource.
      A real state: a cluster can install the Gateway API CRDs for Gateway and
      GatewayClass and not for HTTPRoute.
    * anything else — ``ClusterUnreachable``, ``RBACDenied``, ``UpstreamError``.
      Discovery could not answer *for this group*, so whether the cluster serves
      it is unknown. :func:`app.resources.catalog.resolve` guarantees these are
      re-raised rather than reported as ``unsupported``, and this function's
      only job is to not undo that.

    A single ``unknown`` outranks any number of ``unsupported`` results across
    the candidate versions: if we could not look at ``v1``, "the cluster does
    not serve HTTPRoutes" is not a conclusion the ``v1beta1`` miss entitles us
    to draw.
    """
    misses: list[str] = []
    blind: AdminError | None = None

    for version in backend.versions:
        try:
            catalog.resolve(backend.group, version, backend.plural)
        except Unsupported as e:
            misses.append(f"{backend.group}/{version}: {e.message}")
        except NotFound as e:
            misses.append(f"{backend.group}/{version}: {e.message}")
        except AdminError as e:
            # Keep the first blind spot. They are all the same outage, and the
            # first one carries the version the operator is most likely to be
            # asking about.
            blind = blind or e
        else:
            return BackendState(
                backend=backend,
                state=STATE_AVAILABLE,
                version=version,
                detail=(
                    f"This cluster serves {backend.group}/{version} "
                    f"{backend.plural}."
                ),
            )

    if blind is not None:
        return BackendState(
            backend=backend,
            state=STATE_UNKNOWN,
            version=None,
            detail=(
                f"Whether this cluster serves {backend.kind} objects could not be "
                f"determined: {blind.message} This is not the same as the cluster "
                "not having them."
            ),
            error=blind,
        )

    return BackendState(
        backend=backend,
        state=STATE_UNSUPPORTED,
        version=None,
        detail=(
            f"This cluster does not serve {backend.kind} objects. "
            + ("; ".join(misses) if misses else "")
        ).strip(),
    )


def _feature_report(backend: RouteBackend) -> list[dict[str, Any]]:
    """Every feature with whether this backend expresses it, and the sentence why.

    All nine are reported, including the ones the backend does not support,
    because rule 11.4 needs a *reason* to put on a disabled control and an
    absent key carries none. A frontend that had to infer "not in the list means
    unsupported" would render the control greyed with no explanation, which is
    the state 11.4 exists to forbid.
    """
    return [
        {
            "feature": feature,
            "label": FEATURE_LABELS[feature],
            "supported": feature in backend.features,
        }
        for feature in ALL_FEATURES
    ]


def capabilities() -> dict[str, Any]:
    """§13 ``GET /api/routes/capabilities``.

    Reports every backend, not only the served ones. A console that listed only
    what works cannot answer "why can I not choose passthrough here", and that
    question is asked on exactly the clusters where the answer matters.
    """
    unavailable: list[dict[str, Any]] = []
    backends: list[dict[str, Any]] = []

    for backend in BACKENDS:
        state = backend_state(backend)
        backends.append(_backend_payload(state))
        if state.state == STATE_UNKNOWN and state.error is not None:
            unavailable.append(_unknown_entry(state))

    return envelope(backends, unavailable=unavailable)


def _backend_payload(state: BackendState) -> dict[str, Any]:
    """One ``backends[]`` entry, in the §13 shape."""
    backend = state.backend
    return {
        "backend": backend.key,
        "kind": backend.kind,
        "group": backend.group,
        # Null rather than the first candidate when the backend is not available:
        # naming a version we never confirmed is served would let a caller build
        # a URL out of it.
        "version": state.version,
        "plural": backend.plural,
        "label": backend.label,
        "summary": backend.summary,
        "state": state.state,
        "detail": state.detail,
        "features": _feature_report(backend),
    }


def _unknown_entry(state: BackendState) -> dict[str, Any]:
    """The §1.2 ``unavailable[]`` entry for a backend we could not classify.

    Built from the real error, so the reason token is ``forbidden`` when
    discovery was refused and ``unreachable`` when the API server did not
    answer. Those send an operator to two different places.
    """
    error = state.error
    assert error is not None  # only called for STATE_UNKNOWN, which always has one
    return unavailable_entry(
        state.backend.group,
        state.backend.plural,
        reason_for_error(error),
        detail=error.detail or error.message,
    )



# --------------------------------------------------------------------------- #
# Who owns this object
# --------------------------------------------------------------------------- #

#: Labels and annotations that mean a tool outside this cluster owns the object
#: and will put its own version back. Mapped to the tool's name, because
#: "something will revert this" is not actionable and "Argo CD will revert this
#: on its next sync" is.
#:
#: Deliberately short. `kubectl.kubernetes.io/last-applied-configuration` is NOT
#: here: it means somebody once ran `kubectl apply`, not that anything is
#: watching. Flagging it would put a warning on a large fraction of every
#: cluster's objects, and a warning that is always up is one nobody reads.
_MANAGEMENT_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("annotations", "meta.helm.sh/release-name", "Helm"),
    ("labels", "argocd.argoproj.io/instance", "Argo CD"),
    ("labels", "app.kubernetes.io/instance", None),  # ambiguous — see below
    ("labels", "kustomize.toolkit.fluxcd.io/name", "Flux"),
    ("labels", "helm.toolkit.fluxcd.io/name", "Flux"),
)


def _managed_by(obj: Any) -> dict[str, Any]:
    """Whether something other than a person is expected to own this exposure.

    An edit the console makes to an object a controller owns succeeds, reports
    ``applied: true`` truthfully, and is reverted seconds later. Both halves are
    true at once, which is the shape of confidently-wrong this project cares
    about most: the console's claim is accurate and the operator's conclusion
    from it is not.

    Two kinds of owner, reported separately because they are different
    certainties:

    * **A controller in the cluster** — ``ownerReferences`` with
      ``controller: true``. Definitive. Something is reconciling this object
      right now, and an edit will be undone without anybody being told.
    * **A deployment tool** — a Helm release annotation, an Argo CD instance
      label, a Flux label. Advisory: it means the object was *applied* by that
      tool, and whether an edit survives depends on whether the tool is running
      in a mode that reverts drift. Argo CD with automated self-heal reverts it;
      a Helm release sits still until the next `helm upgrade`, which then
      overwrites it.

    ``app.kubernetes.io/instance`` is recognised but is deliberately **not**
    attributed to a tool: it is the standard recommended label, set by Helm
    charts, by hand-written manifests and by half the operators in the
    ecosystem. Naming a tool from it would be a guess, so the row says the
    object carries an instance label and does not say what put it there.
    """
    metadata = get_field(obj, "metadata", default={}) or {}
    labels = get_field(metadata, "labels", default={}) or {}
    annotations = get_field(metadata, "annotations", default={}) or {}

    controller: dict[str, Any] | None = None
    for reference in get_field(metadata, "ownerReferences", default=[]) or []:
        if get_field(reference, "controller") is True:
            controller = {
                "kind": get_field(reference, "kind"),
                "name": get_field(reference, "name"),
                "apiVersion": get_field(reference, "apiVersion"),
            }
            break

    tool: str | None = None
    marker: str | None = None
    for where, key, name in _MANAGEMENT_MARKERS:
        source = annotations if where == "annotations" else labels
        if key in source:
            marker = key
            if name is not None:
                tool = name
                break
            # An ambiguous marker is remembered but does not stop the scan: a
            # definite one later in the list is the better answer.

    if controller is None and marker is None:
        return {"controller": None, "tool": None, "marker": None, "detail": None}

    if controller is not None:
        detail = (
            f"This exposure is owned by {controller['kind']} "
            f"{controller['name']}, which is reconciling it. An edit made here "
            "will be applied and then reverted, and nothing will say so."
        )
    elif tool is not None:
        detail = (
            f"This exposure was applied by {tool}. Whether an edit here survives "
            "depends on how that tool is configured — a Git-sync tool set to "
            "self-heal reverts it, and a release tool overwrites it at the next "
            "upgrade. The durable change is in the source it deploys from."
        )
    else:
        detail = (
            f"This exposure carries {marker}, so something deployed it rather "
            "than a person creating it by hand. Which tool is not knowable from "
            "the object — that label is set by several. An edit here may not be "
            "the durable place to make the change."
        )

    return {"controller": controller, "tool": tool, "marker": marker, "detail": detail}


# --------------------------------------------------------------------------- #
# Row shaping — one per backend, all producing the same row
# --------------------------------------------------------------------------- #

def _target(name: Any, port: Any, weight: Any) -> dict[str, Any]:
    """One backend Service of an exposure.

    ``weight`` stays ``None`` when the object does not carry one rather than
    defaulting to 100 or to 1. A single-backend exposure has no weight in any of
    the three APIs, and inventing one would render a "100%" chip on a row where
    the operator never chose a split — which then reads as though a split
    exists.
    """
    return {
        "service": None if name is None else str(name),
        "port": port,
        "weight": weight,
    }


def _condition(obj: Any, conditions_path: tuple[str, ...], wanted: str) -> Any:
    """The condition named ``wanted``, or ``None`` if it is not present."""
    for condition in get_field(obj, *conditions_path, default=[]) or []:
        if get_field(condition, "type") == wanted:
            return condition
    return None


def _route_admission(obj: Any) -> tuple[bool | None, str | None]:
    """Admission state of an OpenShift Route, across every router that answered.

    A Route's ``status.ingress`` has one entry per router that has *considered*
    it, each with its own ``Admitted`` condition. The three answers are:

    * no entries, or entries with no ``Admitted`` condition — no router has
      reported. ``None``: we do not know, which is also the state of a Route on
      a cluster whose router is down.
    * at least one router admitted it — ``True``, naming the router. **And, when
      another shard refused it, naming that too.** A Route admitted by ``default``
      and refused by ``internal`` with ``HostAlreadyClaimed`` is being served on
      one shard and not the other, which is a real and confusing state; a green
      badge with no caveat hides exactly the half the operator opened the page
      to find.
    * every router that reported refused it — ``False``, carrying the first
      reason verbatim. ``HostAlreadyClaimed`` is the common one and the operator
      needs the exact word to search for it.

    "At least one" rather than "all" for the badge itself: a Route admitted by
    one router and ignored by another *is* being served, and reporting it as
    rejected would send someone to debug a working exposure. The mixed case is
    resolved in the detail rather than in the verdict — the same shape
    :func:`_httproute_admission` uses for a route whose Gateway accepted it and
    whose backends do not resolve.

    Every shard is examined before answering, rather than returning on the first
    admission: an early return would make the caveat depend on the order the API
    server happened to list the shards in.

    **What this cannot tell you, and does not pretend to.**
    ``RouteIngressCondition`` has no ``observedGeneration``. §6 checks a
    workload's ``status.observedGeneration`` against ``metadata.generation``
    before believing any count, and that check is simply not available here:
    there is no field saying which generation of the spec a router's verdict is
    about. So an ``Admitted: True`` written before the operator changed the
    hostname still reads as ``True`` afterwards, and nothing in the object
    distinguishes the two.

    ``lastTransitionTime`` is deliberately not used to synthesise one. It moves
    when the condition's *status* changes, not when the spec does — a router
    that re-admits an edited Route to the same verdict does not touch it — so
    comparing it against anything would manufacture confidence out of a
    timestamp that means something else. §13 states the limitation instead.
    """
    reported = False
    refusal: str | None = None
    admitted_by: str | None = None

    for entry in get_field(obj, "status", "ingress", default=[]) or []:
        condition = _condition(entry, ("conditions",), "Admitted")
        if condition is None:
            continue
        reported = True
        status = str(get_field(condition, "status") or "")
        router = get_field(entry, "routerName") or "a router"
        if status == "True":
            admitted_by = admitted_by or str(router)
        elif refusal is None:
            reason = get_field(condition, "reason") or "no reason given"
            message = get_field(condition, "message") or ""
            refusal = f"{router} refused it: {reason}. {message}".strip()

    if not reported:
        return None, None
    if admitted_by:
        if refusal:
            return True, (
                f"Admitted by {admitted_by}, but not by every router that "
                f"reported: {refusal}"
            )
        return True, f"Admitted by {admitted_by}."
    return False, refusal


def _route_addresses(obj: Any) -> list[str]:
    """Hostnames the routers published for a Route, in the order they reported."""
    addresses: list[str] = []
    for entry in get_field(obj, "status", "ingress", default=[]) or []:
        value = get_field(entry, "routerCanonicalHostname") or get_field(entry, "host")
        if value and str(value) not in addresses:
            addresses.append(str(value))
    return addresses


def route_row(obj: Any, *, version: str) -> dict[str, Any]:
    """§13 row for a ``route.openshift.io/v1`` Route.

    ``spec.host`` is what the operator or the router settled on; ``spec.subdomain``
    is the "let the router pick" form, where the effective hostname only exists
    in ``status``. Both are reported, and ``hosts`` is built from the spec host
    when there is one and from the admitted status hosts otherwise — so a Route
    created with a subdomain shows the hostname it is actually answering on
    rather than an empty cell.
    """
    spec_host = get_field(obj, "spec", "host")
    status_hosts = [
        str(h) for h in (
            get_field(entry, "host")
            for entry in get_field(obj, "status", "ingress", default=[]) or []
        ) if h
    ]
    hosts = [str(spec_host)] if spec_host else []
    for host in status_hosts:
        if host not in hosts:
            hosts.append(host)

    to = get_field(obj, "spec", "to")
    targets = [
        _target(
            get_field(to, "name"),
            get_field(obj, "spec", "port", "targetPort"),
            get_field(to, "weight"),
        )
    ]
    for alternate in get_field(obj, "spec", "alternateBackends", default=[]) or []:
        targets.append(
            _target(
                get_field(alternate, "name"),
                get_field(obj, "spec", "port", "targetPort"),
                get_field(alternate, "weight"),
            )
        )

    tls = get_field(obj, "spec", "tls")
    admitted, admitted_detail = _route_admission(obj)
    addresses = _route_addresses(obj)

    return _row(
        backend="openshift",
        kind="Route",
        group="route.openshift.io",
        version=version,
        plural="routes",
        obj=obj,
        hosts=hosts,
        subdomain=get_field(obj, "spec", "subdomain"),
        path=get_field(obj, "spec", "path"),
        path_type=None,
        targets=targets,
        tls={
            "termination": get_field(tls, "termination") if tls else None,
            "insecurePolicy": get_field(tls, "insecureEdgeTerminationPolicy") if tls else None,
            # A Route carries its certificate inline. The row reports only
            # *whether* one is set: the private key lives in the same object,
            # and a list endpoint that echoed either would put key material in
            # a table. `redact_secret` guards Secrets by name; this is the same
            # rule applied to the one other object in the cluster that carries
            # a key in its spec.
            "inlineCertificate": bool(get_field(tls, "certificate")) if tls else False,
            "secretName": (
                get_field(tls, "externalCertificate", "name") if tls else None
            ),
        },
        wildcard_policy=get_field(obj, "spec", "wildcardPolicy"),
        admitted=admitted,
        admitted_detail=admitted_detail,
        addresses=addresses,
    )


def ingress_route_row(obj: Any, *, version: str) -> dict[str, Any]:
    """§13 row for a ``networking.k8s.io/v1`` Ingress.

    An Ingress can carry many rules and many paths; the §13 row flattens them,
    because the row is "one exposure" and an Ingress *is* one exposure with
    several ways in. ``paths`` keeps the detail.

    ``admitted`` is ``None`` for every Ingress, always, and that is not a gap:
    the Ingress API has no admission condition. A controller signals that it
    took the object by writing ``status.loadBalancer``, which is what
    ``addresses`` reports — so an Ingress with no address is one no controller
    has claimed, and the UI says exactly that rather than inventing a rejection.
    """
    hosts: list[str] = []
    paths: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []

    for rule in get_field(obj, "spec", "rules", default=[]) or []:
        host = get_field(rule, "host")
        if host and str(host) not in hosts:
            hosts.append(str(host))
        for path in get_field(rule, "http", "paths", default=[]) or []:
            service = get_field(path, "backend", "service")
            port = get_field(service, "port") if service is not None else None
            port_value = (
                (get_field(port, "number") or get_field(port, "name"))
                if port is not None else None
            )
            name = (
                get_field(service, "name") if service is not None
                else get_field(path, "backend", "resource", "name")
            )
            paths.append({
                "path": get_field(path, "path"),
                "pathType": get_field(path, "pathType"),
                "service": None if name is None else str(name),
                "port": port_value,
                # No weight: the Ingress API has no field for one. Null, never
                # 100 — see `_target`.
                "weight": None,
            })
            targets.append(_target(name, port_value, None))

    default_backend = get_field(obj, "spec", "defaultBackend", "service")
    if default_backend is not None:
        port = get_field(default_backend, "port")
        targets.append(
            _target(
                get_field(default_backend, "name"),
                (get_field(port, "number") or get_field(port, "name")) if port else None,
                None,
            )
        )

    tls_hosts: list[str] = []
    secret_names: list[str] = []
    for tls in get_field(obj, "spec", "tls", default=[]) or []:
        for host in get_field(tls, "hosts", default=[]) or []:
            if str(host) not in tls_hosts:
                tls_hosts.append(str(host))
        secret = get_field(tls, "secretName")
        if secret and str(secret) not in secret_names:
            secret_names.append(str(secret))
        # A TLS block with no hosts covers every host on the Ingress. Missing
        # that would show "TLS: none" on an Ingress that is serving HTTPS.
        if not (get_field(tls, "hosts", default=[]) or []):
            for host in hosts:
                if host not in tls_hosts:
                    tls_hosts.append(host)

    addresses: list[str] = []
    for entry in get_field(obj, "status", "loadBalancer", "ingress", default=[]) or []:
        value = get_field(entry, "hostname") or get_field(entry, "ip")
        if value and str(value) not in addresses:
            addresses.append(str(value))

    return _row(
        backend="ingress",
        kind="Ingress",
        group="networking.k8s.io",
        version=version,
        plural="ingresses",
        obj=obj,
        hosts=hosts,
        subdomain=None,
        path=paths[0]["path"] if paths else None,
        path_type=paths[0]["pathType"] if paths else None,
        targets=targets,
        tls={
            # `edge` is the only mode an Ingress can express. Reporting it as
            # the termination when a TLS block exists is accurate; reporting
            # `None` when one does not is also accurate, and the two together
            # are the whole truth about an Ingress's TLS.
            "termination": "edge" if tls_hosts or secret_names else None,
            "insecurePolicy": None,
            "inlineCertificate": False,
            "secretName": secret_names[0] if secret_names else None,
        },
        wildcard_policy=None,
        # See the docstring: the Ingress API has no admission condition, so
        # this is `None` by definition and never `False`.
        admitted=None,
        admitted_detail=(
            None if addresses else
            "No ingress controller has published an address for this Ingress. "
            "That is what an unclaimed Ingress looks like, and also what one on "
            "a cluster with no ingress controller looks like."
        ),
        addresses=addresses,
        paths=paths,
        ingress_class=get_field(obj, "spec", "ingressClassName"),
        tls_hosts=tls_hosts,
    )


def _httproute_admission(obj: Any) -> tuple[bool | None, str | None]:
    """Accepted state of an HTTPRoute, across every parent Gateway.

    ``status.parents[]`` carries one entry per Gateway the route asked to attach
    to, each with an ``Accepted`` condition and — separately — a
    ``ResolvedRefs`` condition saying whether its backend Services exist. Both
    matter: a route accepted by its Gateway whose backendRefs do not resolve is
    attached and serving 500s, which is a state worth surfacing rather than
    calling it admitted and moving on.
    """
    reported = False
    refusal: str | None = None
    unresolved: str | None = None
    accepted_by: str | None = None

    for parent in get_field(obj, "status", "parents", default=[]) or []:
        controller = get_field(parent, "controllerName") or "a Gateway controller"
        accepted = _condition(parent, ("conditions",), "Accepted")
        resolved = _condition(parent, ("conditions",), "ResolvedRefs")
        if accepted is None:
            continue
        reported = True
        if str(get_field(accepted, "status") or "") == "True":
            accepted_by = accepted_by or str(controller)
            if resolved is not None and str(get_field(resolved, "status") or "") != "True":
                unresolved = unresolved or (
                    f"{controller} accepted it, but its backend references do not "
                    f"resolve: {get_field(resolved, 'reason') or 'no reason given'}. "
                    f"{get_field(resolved, 'message') or ''}"
                ).strip()
        elif refusal is None:
            refusal = (
                f"{controller} did not accept it: "
                f"{get_field(accepted, 'reason') or 'no reason given'}. "
                f"{get_field(accepted, 'message') or ''}"
            ).strip()

    if not reported:
        return None, None
    if accepted_by:
        # Accepted, but say so with the caveat when the refs are broken. Not
        # `False`: the Gateway did accept it, and reporting otherwise would send
        # an operator to look at the Gateway instead of at their Service name.
        return True, unresolved or f"Accepted by {accepted_by}."
    return False, refusal


def httproute_row(obj: Any, *, version: str) -> dict[str, Any]:
    """§13 row for a ``gateway.networking.k8s.io`` HTTPRoute.

    ``hosts`` comes from ``spec.hostnames``, which may be empty — an HTTPRoute
    with no hostnames answers for every hostname its Gateway listener does.
    That is a real and deliberate configuration, so an empty list here means
    "inherits the listener's hostnames", and the UI says so rather than showing
    a blank.
    """
    hosts = [str(h) for h in (get_field(obj, "spec", "hostnames", default=[]) or [])]

    paths: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    redirects = False

    for rule in get_field(obj, "spec", "rules", default=[]) or []:
        matched: list[tuple[Any, Any]] = []
        for match in get_field(rule, "matches", default=[]) or []:
            path = get_field(match, "path")
            matched.append((
                get_field(path, "value") if path is not None else None,
                get_field(path, "type") if path is not None else None,
            ))
        if not matched:
            # An HTTPRoute rule with no matches matches everything. Rendering
            # that as an empty path cell would hide a catch-all.
            matched.append(("/", "PathPrefix"))

        for filt in get_field(rule, "filters", default=[]) or []:
            if get_field(filt, "type") == "RequestRedirect":
                redirects = True

        for ref in get_field(rule, "backendRefs", default=[]) or []:
            name = get_field(ref, "name")
            port = get_field(ref, "port")
            weight = get_field(ref, "weight")
            targets.append(_target(name, port, weight))
            for value, kind in matched:
                paths.append({
                    "path": value,
                    "pathType": kind,
                    "service": None if name is None else str(name),
                    "port": port,
                    "weight": weight,
                })

    admitted, admitted_detail = _httproute_admission(obj)

    parents = [
        str(get_field(parent, "name"))
        for parent in get_field(obj, "spec", "parentRefs", default=[]) or []
        if get_field(parent, "name")
    ]

    return _row(
        backend="gateway",
        kind="HTTPRoute",
        group="gateway.networking.k8s.io",
        version=version,
        plural="httproutes",
        obj=obj,
        hosts=hosts,
        subdomain=None,
        path=paths[0]["path"] if paths else None,
        path_type=paths[0]["pathType"] if paths else None,
        targets=targets,
        tls={
            # Not a gap and not unknown: an HTTPRoute has no TLS field. TLS for
            # a Gateway API exposure is configured on the Gateway's listener,
            # which is a different object with a different owner. Reporting
            # `null` here with the parent Gateways named beside it is the honest
            # answer; reporting "none" would say this exposure is plaintext,
            # which we cannot see from this object.
            "termination": None,
            "insecurePolicy": "Redirect" if redirects else None,
            "inlineCertificate": False,
            "secretName": None,
        },
        wildcard_policy=None,
        admitted=admitted,
        admitted_detail=admitted_detail,
        # An HTTPRoute publishes no address of its own; the address belongs to
        # its Gateway. Empty, with the parents named, rather than a guess.
        addresses=[],
        paths=paths,
        parents=parents,
    )


def _row(
    *,
    backend: str,
    kind: str,
    group: str,
    version: str,
    plural: str,
    obj: Any,
    hosts: list[str],
    subdomain: Any,
    path: Any,
    path_type: Any,
    targets: list[dict[str, Any]],
    tls: dict[str, Any],
    wildcard_policy: Any,
    admitted: bool | None,
    admitted_detail: str | None,
    addresses: list[str],
    paths: list[dict[str, Any]] | None = None,
    ingress_class: Any = None,
    tls_hosts: list[str] | None = None,
    parents: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the one §13 row shape all three shapers produce.

    Every key is always present, with ``None`` or ``[]`` where the backend has
    no such concept, so the frontend can read ``row.tls.termination``
    unconditionally. A row shape that varied by backend would push a
    three-way branch into every cell renderer, and the branch that got forgotten
    would render `undefined`.
    """
    name = get_field(obj, "metadata", "name")
    namespace = get_field(obj, "metadata", "namespace")
    return {
        # Stable across a refresh and unique across backends, which is what the
        # table's row key and the detail drawer's selection both need. Built
        # from the four things that identify an exposure rather than from
        # `metadata.uid`, so a row can be addressed from a URL.
        "id": f"{backend}/{namespace}/{name}",
        "backend": backend,
        "kind": kind,
        "group": group,
        "version": version,
        "plural": plural,
        "name": name,
        "namespace": namespace,
        "hosts": hosts,
        "subdomain": subdomain,
        "path": path,
        "pathType": path_type,
        "paths": paths if paths is not None else [],
        "targets": targets,
        "tls": tls,
        "wildcardPolicy": wildcard_policy,
        "admitted": admitted,
        "admittedDetail": admitted_detail,
        "addresses": addresses,
        "ingressClass": ingress_class,
        "tlsHosts": tls_hosts if tls_hosts is not None else [],
        "parents": parents if parents is not None else [],
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
        "resourceVersion": get_field(obj, "metadata", "resourceVersion"),
        # Every key present, `None` throughout when nothing owns it — so the UI
        # reads `row.managedBy.detail` unconditionally.
        "managedBy": _managed_by(obj),
    }


_SHAPERS = {
    "openshift": route_row,
    "ingress": ingress_route_row,
    "gateway": httproute_row,
}


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def list_routes(
    *,
    namespace: str | None = None,
    backends: Iterable[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """§13 ``GET /api/routes`` — every exposure on the cluster, from all backends.

    Each backend is listed inside its own :func:`app.resources.envelope.collect`
    block, so one forbidden verb costs that backend's rows and names itself,
    rather than blanking the page or — the bug this guards — returning the other
    two backends' rows as though they were the whole answer.

    ``continue`` is deliberately **not** propagated. Three independent listings
    cannot share one cursor, and a single token that silently meant "page 2 of
    the Ingresses and page 1 of everything else" would produce a table that
    skips rows on the way past. Instead each backend is read with the same
    bounded ``limit`` and a backend that had more is reported in
    ``truncated[]`` — an honest "there are more of these than are shown" beats
    a paging control that lies.
    """
    selected = (
        [resolve_backend(key) for key in backends]
        if backends is not None
        else list(BACKENDS)
    )

    items: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    backend_payloads: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []

    for backend in selected:
        state = backend_state(backend)
        backend_payloads.append(_backend_payload(state))

        if state.state == STATE_UNKNOWN and state.error is not None:
            unavailable.append(_unknown_entry(state))
            continue
        if not state.available:
            continue

        version = state.version
        assert version is not None  # STATE_AVAILABLE always carries one
        listing: dict[str, Any] | None = None
        with collect(unavailable, backend.group, backend.plural, namespace=namespace):
            listing = reader.list_resource(
                backend.group, version, backend.plural,
                namespace=namespace, limit=limit,
            )
        if listing is None:
            continue

        shaper = _SHAPERS[backend.key]
        for obj in listing["items"]:
            items.append(shaper(obj, version=version))

        if listing["continue"]:
            truncated.append({
                "backend": backend.key,
                "kind": backend.kind,
                "shown": len(listing["items"]),
                "remaining": listing["remaining"],
            })

    result = envelope(items, unavailable=unavailable)
    result["backends"] = backend_payloads
    result["truncated"] = truncated
    return result


def get_route(backend_key: str, namespace: str, name: str) -> dict[str, Any]:
    """§13 single-exposure read: the shaped row plus the live manifest.

    Both, because the config screen needs both: the row drives the form, and the
    manifest is what the YAML view edits and what the ``resourceVersion`` on the
    confirming PUT has to come from. Reading them separately would open a window
    in which the form and the document describe different generations of the
    same object.
    """
    backend = resolve_backend(backend_key)
    state = backend_state(backend)
    if state.state == STATE_UNKNOWN:
        # Re-raise the real discovery failure. Returning "not found" here would
        # tell an operator their Route was deleted during an API outage.
        assert state.error is not None
        raise state.error
    if not state.available:
        raise Unsupported(
            f"This cluster does not serve {backend.kind} objects.",
            detail=state.detail,
            hint="GET /api/routes/capabilities lists the backends this cluster serves.",
            context={"backend": backend.key, "group": backend.group, "resource": backend.plural},
        )

    version = state.version
    assert version is not None
    obj = reader.get_resource(
        backend.group, version, backend.plural, name, namespace=namespace,
    )
    return {
        "route": _SHAPERS[backend.key](obj, version=version),
        "manifest": obj,
        "backend": _backend_payload(state),
    }


__all__ = [
    "ALL_FEATURES",
    "BACKENDS",
    "BACKENDS_BY_KEY",
    "BackendState",
    "FEATURE_EDGE_TLS",
    "FEATURE_GENERATED_HOST",
    "FEATURE_INSECURE_ALLOW",
    "FEATURE_INSECURE_REDIRECT",
    "FEATURE_LABELS",
    "FEATURE_PASSTHROUGH_TLS",
    "FEATURE_PATH_EXACT",
    "FEATURE_REENCRYPT_TLS",
    "FEATURE_WEIGHTED_BACKENDS",
    "FEATURE_WILDCARD_SUBDOMAIN",
    "RouteBackend",
    "STATE_AVAILABLE",
    "STATE_UNKNOWN",
    "STATE_UNSUPPORTED",
    "backend_state",
    "capabilities",
    "get_route",
    "httproute_row",
    "ingress_route_row",
    "list_routes",
    "resolve_backend",
    "route_row",
]
