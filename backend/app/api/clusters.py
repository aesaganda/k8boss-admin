"""
Cluster registration and per-cluster overview (§3).

A cluster is an API server URL plus a bearer token. The token is encrypted on
the way in (``app.crypto``) and never comes back out: every response is built by
:meth:`app.models.Cluster.to_public_dict`, which is an allowlist that raises
rather than serialize a credential column.

Two endpoints here do more than CRUD, and both exist to answer a question early
instead of late:

* ``POST /{id}/test`` connects *and* runs the baseline preflight set, so a
  half-permissioned ServiceAccount is visible at registration rather than at the
  first click on a page that turns out not to work.
* ``GET /{id}/overview`` collects six independent things, and each one degrades
  alone. The page must never 500 because one collector failed, and a collector
  that failed must never be reported as a zero — an overview showing "0 nodes"
  for a cluster we could not list nodes on is precisely the confidently wrong
  answer this console is built against.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from decimal import Decimal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import encrypt
from app.database import get_db
from app.errors import AdminError, Conflict, Invalid, NotFound, UpstreamError, from_api_exception
from app.k8s.auth import TOKEN_AUTH_TYPES
from app.k8s.client import manager
from app.k8s.context import reset_current_cluster_id, set_current_cluster_id
from app.k8s.quantities import add_quantities, parse_quantity
from app.identity import oidc, openshift
from app.identity.dependencies import require_console_admin
from app.models import Cluster, utcnow
from app.services import nodes as nodes_service
from app.services import route_domain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["clusters"])


# The permissions the console needs to be useful, checked once at registration
# (§9). Kept as data next to the endpoint that uses it so adding a feature that
# needs a new verb has one obvious place to declare it — the alternative is an
# operator discovering the gap from a 403 on a page they had no reason to expect
# to fail.
#
# Both halves matter. The `list` checks decide whether the console can *show*
# anything; the four write/read-sensitive checks at the end decide which buttons
# will work. A cluster that lists everything and cannot patch a Deployment is a
# perfectly valid read-only registration, and saying so at registration is worth
# more than discovering it mid-incident.
BASELINE_PREFLIGHT_CHECKS: tuple[dict, ...] = (
    {"verb": "list", "group": "core", "resource": "pods"},
    {"verb": "list", "group": "core", "resource": "services"},
    {"verb": "list", "group": "core", "resource": "namespaces"},
    {"verb": "list", "group": "core", "resource": "nodes"},
    {"verb": "list", "group": "core", "resource": "configmaps"},
    {"verb": "list", "group": "core", "resource": "events"},
    {"verb": "list", "group": "apps", "resource": "deployments"},
    {"verb": "list", "group": "apps", "resource": "statefulsets"},
    {"verb": "list", "group": "apps", "resource": "daemonsets"},
    {"verb": "list", "group": "batch", "resource": "jobs"},
    {"verb": "list", "group": "batch", "resource": "cronjobs"},
    {"verb": "list", "group": "networking.k8s.io", "resource": "ingresses"},
    {"verb": "list", "group": "rbac.authorization.k8s.io", "resource": "roles"},
    {"verb": "patch", "group": "apps", "resource": "deployments"},
    {"verb": "create", "group": "core", "resource": "pods", "subresource": "exec"},
    # §7.4. Checked here rather than discovered on the Debug tab: this is a
    # grant an operator is likely to have missed, because it is newer than the
    # rest of this file and because "we can exec" reads like "we can debug".
    {"verb": "patch", "group": "core", "resource": "pods",
     "subresource": "ephemeralcontainers"},
    {"verb": "delete", "group": "core", "resource": "pods"},
    {"verb": "get", "group": "core", "resource": "secrets"},
)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class ClusterCreate(BaseModel):
    """§3 POST body. ``token`` is write-only and has no read counterpart."""

    name: str = Field(..., min_length=1, max_length=255)
    platform: str = Field("kubernetes", max_length=50)
    api_server: str = Field(..., min_length=1, max_length=1024)
    authentication_type: str = Field("service_account_token", max_length=50)
    token: str = Field(..., min_length=1)
    ca_certificate: str | None = None
    skip_tls_verify: bool = False
    #: ADR-0007. When true, this console's calls to this cluster carry
    #: `Impersonate-User` for the signed-in operator, so the API server
    #: evaluates authorization, admission and its own audit log as that person
    #: rather than as this console's ServiceAccount.
    #:
    #: Off unless asked for, per-cluster rather than console-wide, and rejected
    #: outright unless this deployment authenticates with OpenID Connect — see
    #: `_validate_impersonation`. It also needs `impersonate` on `users` and
    #: `groups` in the cluster's own RBAC, which `deploy/rbac.yaml` ships
    #: commented out because an unrestricted form of that grant is cluster-admin
    #: by proxy.
    impersonation_enabled: bool = False
    #: §13. The cluster's wildcard DNS domain, used to generate exposure
    #: hostnames. Optional, and blank is meaningful: it means the console
    #: generates none rather than guessing one.
    app_domain: str | None = None


class ClusterUpdate(BaseModel):
    """§3 PUT body — partial. An omitted ``token`` keeps the stored one.

    Omitted, specifically: the handler uses ``exclude_unset`` rather than testing
    for None, so ``{"token": null}`` and "I did not send a token" stay
    distinguishable. Without that, every edit of a cluster's *name* through a UI
    that round-trips the whole object would wipe its credential.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    platform: str | None = Field(None, max_length=50)
    api_server: str | None = Field(None, min_length=1, max_length=1024)
    authentication_type: str | None = Field(None, max_length=50)
    token: str | None = None
    ca_certificate: str | None = None
    skip_tls_verify: bool | None = None
    impersonation_enabled: bool | None = None
    #: Sending "" clears it. That is the only way to clear it, because the
    #: generic assignment loop below skips None so that an omitted field cannot
    #: blank a stored one — and an operator who typed the wrong domain has to be
    #: able to remove it, not just overwrite it with another wrong one.
    app_domain: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load(db: Session, cluster_id: int) -> Cluster:
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise NotFound(
            f"Cluster {cluster_id} is not registered.",
            context={"resource": "clusters", "name": str(cluster_id)},
        )
    return cluster


def _validate_impersonation(enabled: bool | None) -> None:
    """Refuse ADR-0007's opt-in on a deployment that could never satisfy it.

    Turning it on where no sign-in method can supply a cluster identity produces
    a cluster that refuses **every** operator with ``impersonation_unavailable``,
    at the request rather than at the setting. That failure is accurate and
    arrives in the worst possible place: on a page, to somebody who did not
    change the setting and cannot see it.

    The methods that qualify are ADR-0007's ``IMPERSONATION_SOURCES`` — OpenID
    Connect and the cluster's own OpenShift OAuth server — and this checks that
    *at least one of them is configured*, not that the operator will arrive
    through it. It cannot check the second: the setting is edited long before
    anybody signs in, and a console offering both would otherwise have to guess
    which button a future operator will press. An operator who is signed in
    through a method that does not qualify still gets the refusal at the request,
    naming their own auth source.

    Refusing at the form is the same choice ``_validate`` makes about an API
    server URL that could never build a client. It is deliberately *not* a check
    that the cluster and the console share an issuer — this console cannot read
    a cluster's ``--oidc-issuer-url`` and will not pretend to; that alignment is
    stated in `docs/adr-0007-impersonation.md` and is the operator's to get
    right. On an OpenShift cluster whose operators sign in through
    ``OPENSHIFT_ENABLED`` there is nothing to align: the name the console sends
    was read from that cluster's own ``users/~``.
    """
    if not enabled:
        return
    if not settings.auth_enabled or not (
        oidc.enabled() or openshift.enabled()
    ):
        raise Invalid(
            "Impersonation needs this console to authenticate operators through "
            "OpenID Connect, or through the cluster's own OAuth server.",
            hint=(
                "Enable AUTH_ENABLED with either OpenID Connect (OIDC_ENABLED) "
                "or the cluster's own OAuth server (OPENSHIFT_ENABLED), or leave "
                "impersonation off. A local or LDAP password cannot become a "
                "cluster identity — the cluster trusts its own issuer, not this "
                "console's user table — and a plain OAuth 2.0 or SAML provider "
                "states an identity in a vocabulary no API server consumes."
            ),
            context={"field": "impersonation_enabled"},
        )


def _validate(*, api_server: str | None, authentication_type: str | None) -> None:
    """Reject a registration that could never build a client, at the form.

    The alternative is storing it and failing at the first request, where the
    error is about a cluster being unreachable rather than about a URL missing
    its scheme — a true statement that sends the operator to look at their
    network.
    """
    if api_server is not None:
        candidate = api_server.strip()
        if not candidate.startswith(("http://", "https://")):
            raise Invalid(
                "The API server URL must start with https:// (or http:// for a "
                "local test cluster).",
                context={"field": "api_server"},
            )
    if authentication_type is not None and authentication_type not in TOKEN_AUTH_TYPES:
        raise Invalid(
            f"Unsupported authentication type {authentication_type!r}.",
            hint="Supported types: " + ", ".join(sorted(TOKEN_AUTH_TYPES)) + ".",
            context={"field": "authentication_type"},
        )


@contextmanager
def _cluster_context(cluster_id: int):
    """Pin the request's cluster context to a path parameter.

    ``/clusters/{id}/test`` and ``/clusters/{id}/overview`` name their cluster in
    the path, while everything downstream — the client manager, preflight — reads
    it from the context (§1.1). Without this, testing cluster 3 from a page whose
    query string still said ``cluster_id=1`` would report on cluster 1 and label
    the answer "cluster 3".
    """
    token = set_current_cluster_id(cluster_id)
    try:
        yield
    finally:
        reset_current_cluster_id(token)


def _sum_quantities(values: list, *, what: str) -> Decimal:
    """Sum quantities, converting a parse failure into a reportable error."""
    total = Decimal(0)
    for value in values:
        parsed = parse_quantity(value)
        if parsed is None:
            raise UpstreamError(
                f"The cluster reported a {what} value this console could not parse.",
                detail=repr(value),
                hint="The totals are withheld rather than reported partially summed.",
                context={"resource": "nodes" if "capacit" in what else "pods"},
            )
        total = add_quantities(total, parsed)
    return total


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

@router.get("/clusters")
def list_clusters(db: Session = Depends(get_db)) -> dict:
    """Every registered cluster, as the §1.2 envelope.

    Always complete and never partial: this reads the console's own database, so
    there is no half-answer to report. It is deliberately still the standard
    envelope — one response shape for every collection means the frontend has one
    reader, and the day cluster rows start carrying something read from a cluster
    this endpoint gains a real ``unavailable`` case without a breaking change.
    """
    # Imported at call time, like the preflight import in `test` below: cluster
    # registration is the endpoint an operator reaches for when nothing else
    # works, so it must not stop loading because the resource-reading layer is
    # mid-change.
    from app.resources.envelope import envelope

    clusters = db.query(Cluster).order_by(Cluster.id.asc()).all()
    return envelope([c.to_public_dict() for c in clusters])


# The three writes below are administrator-only when the console authenticates
# operators, and unchanged in legacy proxy mode — `require_console_admin` returns
# None with AUTH_ENABLED false, where the proxy in front owns the decision.
#
# Ungated, they were reachable by any signed-in account down to the lowest
# "user" role, and two payloads walk straight through. Clearing
# `impersonation_enabled` makes every later call to that cluster run as this
# console's ServiceAccount instead of as the signed-in operator (ADR-0007), so
# the caller leaves their own RBAC behind and inherits the console's. Worse, and
# independent of impersonation: a PUT that moves `api_server` while *omitting*
# `token` keeps the stored credential — that is what `ClusterUpdate` promises —
# and points it at a host the caller controls, so the next request hands them
# the cluster's bearer token. Registering a cluster is an administrator act for
# the same reason: it decides which API server this console's ServiceAccount
# talks to.
@router.post("/clusters", status_code=201)
def create_cluster(
    payload: ClusterCreate,
    _admin=Depends(require_console_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Register a cluster. The token is encrypted before it reaches the database."""
    _validate(api_server=payload.api_server, authentication_type=payload.authentication_type)
    _validate_impersonation(payload.impersonation_enabled)

    cluster = Cluster(
        name=payload.name.strip(),
        platform=payload.platform,
        api_server=payload.api_server.strip(),
        authentication_type=payload.authentication_type,
        token_encrypted=encrypt(payload.token),
        ca_certificate=payload.ca_certificate,
        skip_tls_verify=payload.skip_tls_verify,
        impersonation_enabled=payload.impersonation_enabled,
        app_domain=route_domain.normalize_domain(payload.app_domain),
        # Never tested yet, and that is a distinct state from "failed". See
        # Cluster.status.
        status="unknown",
    )
    db.add(cluster)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise Conflict(
            f"A cluster named {payload.name!r} is already registered.",
            hint="Pick a different name, or edit the existing cluster.",
            context={"resource": "clusters", "name": payload.name},
        ) from e
    db.refresh(cluster)
    logger.info("Registered cluster id=%s name=%s", cluster.id, cluster.name)
    return cluster.to_public_dict()


@router.put("/clusters/{cluster_id}")
def update_cluster(
    cluster_id: int,
    payload: ClusterUpdate,
    _admin=Depends(require_console_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Partial update. An omitted ``token`` keeps the stored one."""
    cluster = _load(db, cluster_id)
    fields = payload.model_dump(exclude_unset=True)

    _validate(
        api_server=fields.get("api_server"),
        authentication_type=fields.get("authentication_type"),
    )
    if "impersonation_enabled" in fields:
        _validate_impersonation(fields["impersonation_enabled"])

    if "token" in fields:
        token = fields.pop("token")
        if not token:
            raise Invalid(
                "A cluster cannot be saved with an empty token.",
                hint="Omit the field entirely to keep the token already stored.",
                context={"field": "token"},
            )
        cluster.token_encrypted = encrypt(token)

    if "app_domain" in fields:
        # Normalised here rather than in the loop: it is the one field where a
        # blank is an instruction ("stop generating hostnames") rather than an
        # absent value, and normalize_domain refuses anything that would build a
        # hostname DNS cannot resolve.
        cluster.app_domain = route_domain.normalize_domain(fields.pop("app_domain"))

    for key, value in fields.items():
        if value is not None:
            setattr(cluster, key, value.strip() if isinstance(value, str) else value)

    # ``updated_at`` is the client cache key (see ClusterClientManager._cache_key),
    # so bumping it here is what makes an edited endpoint or token take effect on
    # the next request instead of at the next process restart. onupdate only
    # fires when a mapped column actually changed, and an edit that changes only
    # the token would otherwise leave the stale transport cached.
    cluster.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise Conflict(
            f"A cluster named {fields.get('name')!r} is already registered.",
            context={"resource": "clusters", "name": fields.get("name")},
        ) from e
    db.refresh(cluster)
    manager.invalidate(cluster.id)
    return cluster.to_public_dict()


@router.delete("/clusters/{cluster_id}", status_code=204)
def delete_cluster(
    cluster_id: int,
    _admin=Depends(require_console_admin),
    db: Session = Depends(get_db),
) -> Response:
    """De-register a cluster and drop its cached transport.

    The audit records naming it are left alone. They denormalise the cluster name
    for exactly this reason (see ``AuditRecord.cluster_name``): the trail has to
    stay readable after the cluster it describes is gone, which is often the
    moment someone goes looking at it.
    """
    cluster = _load(db, cluster_id)
    db.delete(cluster)
    db.commit()

    manager.invalidate(cluster_id)
    if manager.active_cluster_id == cluster_id:
        manager.set_active(None)
    logger.info("De-registered cluster id=%s", cluster_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #

@router.post("/clusters/{cluster_id}/test")
def test_cluster(cluster_id: int, db: Session = Depends(get_db)) -> dict:
    """Live connection check plus the baseline preflight set (§3, §9).

    Returns 200 whether or not the cluster answered: "we tried and it refused"
    is the result of the test, not a failure of the endpoint. An unreachable
    cluster carries the full §1.3 error envelope under ``error`` so the operator
    is told *why*, and ``permissions`` is **null** rather than ``[]`` — an empty
    list would say the ServiceAccount holds none of the baseline permissions,
    which is a claim about RBAC we never got close enough to make.
    """
    cluster = _load(db, cluster_id)

    # Build straight from the row, bypassing the cache: the point of a test is to
    # exercise the configuration as it now stands, and a cached transport from
    # before the operator's edit would report on the configuration they just
    # replaced.
    manager.invalidate(cluster_id)

    # Hand the pooled database connection back before contacting the cluster.
    # A test against an unreachable API server is the longest cluster call this
    # console makes — it runs to the connect timeout by definition — and a
    # session left open holds one pooled connection for all of it. Enough tabs
    # on one sick cluster exhaust the pool, and then this console's own sign-ins
    # and audit writes start failing because somebody *else's* API server is
    # down: one customer's outage becomes the whole console's.
    #
    # `close()` detaches the row with its loaded columns intact, which is all
    # the transport builder needs; the result below is written through a fresh
    # load, and the commit that records it releases the connection again before
    # the preflight round trips.
    db.close()

    started = time.perf_counter()
    try:
        with _cluster_context(cluster_id):
            clients = manager.get_clients_for_cluster(cluster)
            try:
                version = clients.version_api.get_code()
            finally:
                clients.close()
    except Exception as e:  # noqa: BLE001 - every failure is a test result
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        error = _as_envelope(e, cluster_id=cluster_id)
        # Re-read rather than reuse the detached row, and tolerate its absence:
        # the connection was released above, so the cluster could have been
        # de-registered while we waited on its timeout. Recording the result of
        # a test on a row that is gone is not worth failing the test over — the
        # operator asked whether the cluster answers, and it did not.
        row = db.get(Cluster, cluster_id)
        if row is not None:
            row.status = "disconnected"
            row.status_detail = error["message"]
            row.updated_at = utcnow()
        # Outside the branch: the read above opened a transaction, and leaving it
        # open would hold the connection across the return path we just released
        # it for.
        db.commit()
        logger.warning("Connection test failed for cluster id=%s: %s",
                       cluster_id, error["error"])
        return {
            "reachable": False,
            "server_version": None,
            "latency_ms": latency_ms,
            "permissions": None,
            "error": error,
        }

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    server_version = getattr(version, "git_version", None) or None

    # Same re-read, same reason as the failure branch above.
    row = db.get(Cluster, cluster_id)
    if row is not None:
        row.status = "connected"
        row.status_detail = None
        row.server_version = server_version
        row.last_connected = utcnow()
        row.updated_at = utcnow()
    db.commit()

    # Imported here, not at module scope: this is the only place in the
    # registration flow that needs the access layer, and a cluster must remain
    # registrable even while that layer is being changed.
    from app.admin import preflight

    with _cluster_context(cluster_id):
        permissions = preflight.check_many([dict(check) for check in BASELINE_PREFLIGHT_CHECKS])

    # §13. Offered, never applied: the stored value is the operator's and this
    # is only what the cluster says about itself. The dialog shows it as a
    # suggestion beside the field. None covers both "not OpenShift" and "we
    # could not ask", which are the same thing to a form that has nothing to
    # pre-fill — the difference is reported where it can be acted on, in the
    # capabilities envelope the route dialog reads.
    with _cluster_context(cluster_id):
        discovered = route_domain.discover_domain()

    return {
        "reachable": True,
        "server_version": server_version,
        "latency_ms": latency_ms,
        "permissions": permissions,
        "discovered_app_domain": discovered,
    }


def _as_envelope(exc: Exception, *, cluster_id: int) -> dict:
    """Render any exception from a connection attempt as a §1.3 error body.

    Everything reachable from here already has a class in ``app.errors`` or is a
    Kubernetes ``ApiException``; the final branch covers a genuinely unexpected
    fault (a malformed stored CA, for instance) and still produces the shape the
    UI parses, rather than a bare string that renders as "[object Object]".
    """
    from kubernetes.client.rest import ApiException

    context = {"resource": "clusters", "name": str(cluster_id)}
    if isinstance(exc, AdminError):
        merged = dict(context)
        merged.update(exc.context)
        exc.context = merged
        return exc.to_envelope()
    if isinstance(exc, ApiException):
        return from_api_exception(exc, context=context).to_envelope()
    return UpstreamError(
        "The connection test failed before the cluster answered.",
        detail=f"{type(exc).__name__}: {exc}",
        hint="Check the API server URL, the CA certificate and the stored token.",
        context=context,
    ).to_envelope()


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

@router.get("/clusters/{cluster_id}/overview")
def cluster_overview(cluster_id: int, db: Session = Depends(get_db)) -> dict:
    """Cluster summary (§3), each sub-object collected independently.

    Six collectors, six independent failure domains. A collector that fails sets
    **its own key to null** and appends to ``unavailable``; nothing else is
    affected and the response is still 200. Two rules make that honest:

    * ``null``, never ``0``. Zero nodes and "we could not list nodes" are
      different facts and the second one must not be able to masquerade as the
      first.
    * every failure is named in ``unavailable`` with the resource it concerns, so
      the page can say which panel is missing and why instead of rendering a
      dash with no explanation.
    """
    from app.resources.envelope import collect

    # `_load` is still what turns an unregistered id into a 404; the platform is
    # the only column the response needs from the row.
    platform = _load(db, cluster_id).platform

    # Everything below this line talks to a cluster and nothing below it talks to
    # the database, so the pooled connection goes back first. Held across six
    # collectors' round trips, one slow or unreachable cluster keeps a connection
    # for the whole of its timeout, and enough open tabs on that one cluster
    # exhaust the pool — at which point this console's own sign-ins and audit
    # writes start failing because somebody *else's* API server is down. The
    # client manager opens its own short-lived session to resolve the cluster, so
    # releasing this one costs the collectors nothing.
    db.close()

    result: dict = {
        "server_version": None,
        "platform": platform,
        "nodes": None,
        "namespaces": None,
        "workloads": None,
        "pods": None,
        "capacity": None,
        "requested": None,
    }
    unavailable: list[dict] = []

    with _cluster_context(cluster_id):
        with collect(unavailable, "", "version"):
            result["server_version"] = _collect_server_version()

        # Nodes and capacity come from one listing, so they share a failure: if
        # we could not list nodes we know neither how many there are nor how much
        # they hold. One `unavailable` entry, two null keys — reporting it twice
        # would suggest two separate faults.
        with collect(unavailable, "", "nodes"):
            result["nodes"], result["capacity"] = _collect_nodes()

        with collect(unavailable, "", "namespaces"):
            result["namespaces"] = _collect_namespaces()

        with collect(unavailable, "apps", "deployments"):
            result["workloads"] = _collect_workloads()

        # Same pairing: pod phases and the requested totals are both derived from
        # the single all-namespaces pod listing.
        with collect(unavailable, "", "pods"):
            result["pods"], result["requested"] = _collect_pods()

    result["unavailable"] = unavailable
    return result


def _collect_server_version() -> str | None:
    from app.k8s.client import get_version_api

    version = get_version_api().get_code()
    return getattr(version, "git_version", None) or None


def _collect_nodes() -> tuple[dict, dict]:
    """Node counts and aggregate capacity from one node listing."""
    from app.k8s.client import get_core_v1

    nodes = get_core_v1().list_node().items or []

    ready = 0
    unschedulable = 0
    cpu: list = []
    memory: list = []
    pods: list = []
    for node in nodes:
        conditions = getattr(getattr(node, "status", None), "conditions", None) or []
        if any(c.type == "Ready" and c.status == "True" for c in conditions):
            ready += 1
        if getattr(getattr(node, "spec", None), "unschedulable", None):
            unschedulable += 1
        capacity = getattr(getattr(node, "status", None), "capacity", None) or {}
        if "cpu" in capacity:
            cpu.append(capacity["cpu"])
        if "memory" in capacity:
            memory.append(capacity["memory"])
        if "pods" in capacity:
            pods.append(capacity["pods"])

    counts = {"total": len(nodes), "ready": ready, "unschedulable": unschedulable}
    capacity_totals = {
        "cpu_cores": float(round(_sum_quantities(cpu, what="node capacity cpu"), 3)),
        "memory_bytes": int(_sum_quantities(memory, what="node capacity memory")),
        "pods": int(_sum_quantities(pods, what="node capacity pods")),
    }
    return counts, capacity_totals


def _collect_namespaces() -> int:
    from app.k8s.client import get_core_v1

    return len(get_core_v1().list_namespace().items or [])


def _collect_workloads() -> dict:
    """Counts per workload kind.

    One failure here nulls the whole ``workloads`` object rather than reporting
    the kinds that answered. A partial map is indistinguishable from a cluster
    that genuinely has no CronJobs, and the operator has no way to tell which
    they are looking at.
    """
    from app.k8s.client import get_apps_v1, get_batch_v1

    apps = get_apps_v1()
    batch = get_batch_v1()
    return {
        "deployments": len(apps.list_deployment_for_all_namespaces().items or []),
        "statefulsets": len(apps.list_stateful_set_for_all_namespaces().items or []),
        "daemonsets": len(apps.list_daemon_set_for_all_namespaces().items or []),
        "jobs": len(batch.list_job_for_all_namespaces().items or []),
        "cronjobs": len(batch.list_cron_job_for_all_namespaces().items or []),
    }


def _collect_pods() -> tuple[dict, dict]:
    """Pod phase counts and the sum of container requests, from one listing."""
    from app.k8s.client import get_core_v1

    pods = get_core_v1().list_pod_for_all_namespaces().items or []

    phases = {"total": len(pods), "running": 0, "pending": 0, "failed": 0, "succeeded": 0}
    for pod in pods:
        phase = getattr(getattr(pod, "status", None), "phase", None)
        key = (phase or "").lower()
        if key in phases and key != "total":
            phases[key] += 1

    # §5's algorithm, not a second one. This used to sum `spec.containers` alone,
    # on the reasoning that init containers have finished by the time a pod is
    # Running — true of ordinary init containers, false of a sidecar
    # (`restartPolicy: Always`), which runs for the pod's whole life, and false
    # of `spec.overhead`, which the scheduler charges and no container carries.
    # A cluster running a service mesh has a sidecar in every pod, so this page
    # and the nodes page reported two different numbers for the same quantity,
    # and this one was the low one — the direction that reads as headroom.
    # Terminated pods are skipped there, as they were here.
    requested = nodes_service.requested_total(pods)
    return phases, requested


__all__ = ["BASELINE_PREFLIGHT_CHECKS", "router"]
