"""
Cluster registration and per-cluster overview (§3).

A cluster is an API server URL plus a bearer token. The token is encrypted on
the way in (``app.crypto``) and never comes back out: every response is built by
:meth:`app.models.Cluster.to_public_dict`, which is an allowlist that raises
rather than serialize a credential column.

Three endpoints here do more than CRUD, and each exists to answer a question
early instead of late:

* ``GET /{...}/discovery`` and ``POST /clusters/import`` (§34) are onboarding.
  A console whose first screen asks for an API server URL and a bearer token is
  asking for four commands' worth of work from somebody who has a kind cluster
  running on the same laptop, so discovery reads the kubeconfig and offers what
  is there — and refuses, with the reason written out, what it cannot take.

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
from app.k8s import adoption
from app.k8s import kubeconfig as kubeconfig_reader
from app.k8s.auth import AUTH_CLIENT_CERTIFICATE, SUPPORTED_AUTH_TYPES
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
    # §5.5's node debug pod, §15's CLI pod and §4's create-from-YAML are one
    # grant — `deploy/rbac.yaml` says so at the rule itself, because RBAC cannot
    # tell an nginx pod from one mounting the node's root filesystem. Checked
    # here because all three are reached from pages that look like they work
    # until the confirm: the two switches an operator sets to enable them
    # (`ADMIN_NODE_DEBUG_ENABLED`, `ADMIN_CLI_ENABLED`) say nothing about
    # whether the ServiceAccount may create a pod, and the console's own gate
    # opening is the thing that makes the missing grant look like a bug in the
    # console rather than a permission nobody granted.
    #
    # It sat in `docs/rbac.md`'s published list for a long time without being
    # here, which is the direction that misleads: a reader was told their
    # registration had been verified for a permission this endpoint never asked
    # about. Adding the check is the honest way to close that, rather than
    # quietly shortening the list.
    {"verb": "create", "group": "core", "resource": "pods"},
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
    #: Optional since §34, and the model validator below is what keeps that
    #: from meaning "anonymous". A token cluster still needs one; a
    #: client-certificate cluster needs the pair instead, and a body with
    #: neither is refused at the form rather than stored as a registration that
    #: authenticates as nobody.
    token: str | None = Field(None, min_length=1)
    #: §34. The X.509 pair, PEM. Write-only in exactly the way ``token`` is:
    #: ``has_client_certificate`` is what comes back, never these.
    client_certificate: str | None = None
    client_key: str | None = None
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
    client_certificate: str | None = None
    client_key: str | None = None
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
    if authentication_type is not None and authentication_type not in SUPPORTED_AUTH_TYPES:
        raise Invalid(
            f"Unsupported authentication type {authentication_type!r}.",
            hint="Supported types: " + ", ".join(sorted(SUPPORTED_AUTH_TYPES)) + ".",
            context={"field": "authentication_type"},
        )


def _validate_credential(
    *,
    authentication_type: str,
    token: str | None,
    client_certificate: str | None,
    client_key: str | None,
) -> None:
    """Refuse a registration that names a credential it does not carry.

    ``get_auth_provider`` would refuse it too — at the first request, as a
    cluster that cannot connect, which is a sentence about somebody's network
    for a mistake made in a form. The same refusal here names the field.

    The certificate and the key are checked as a pair rather than individually
    because half of the pair is the failure that looks like success: it stores,
    it lists, and it dies in the TLS handshake with an error that mentions
    neither field.
    """
    if authentication_type == AUTH_CLIENT_CERTIFICATE:
        if not (client_certificate and client_key):
            raise Invalid(
                "Client-certificate authentication needs both the certificate "
                "and its private key.",
                hint=(
                    "Send client_certificate and client_key as PEM, or import "
                    "the context from your kubeconfig with POST "
                    "/api/clusters/import, which reads both for you."
                ),
                context={"field": "client_key" if client_certificate else "client_certificate"},
            )
        return
    if not token:
        raise Invalid(
            "A bearer token is required for token authentication.",
            hint=(
                "Mint one with `kubectl -n k8boss-admin create token "
                "k8boss-admin`, or register the cluster with "
                "authentication_type=client_certificate and a PEM pair."
            ),
            context={"field": "token"},
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
    """Register a cluster. The credential is encrypted before it reaches the database."""
    _validate(api_server=payload.api_server, authentication_type=payload.authentication_type)
    _validate_credential(
        authentication_type=payload.authentication_type,
        token=payload.token,
        client_certificate=payload.client_certificate,
        client_key=payload.client_key,
    )
    _validate_impersonation(payload.impersonation_enabled)

    cluster = Cluster(
        name=payload.name.strip(),
        platform=payload.platform,
        api_server=payload.api_server.strip(),
        authentication_type=payload.authentication_type,
        token_encrypted=encrypt(payload.token) if payload.token else None,
        client_certificate=payload.client_certificate,
        client_key_encrypted=(
            encrypt(payload.client_key) if payload.client_key else None
        ),
        ca_certificate=payload.ca_certificate,
        skip_tls_verify=payload.skip_tls_verify,
        impersonation_enabled=payload.impersonation_enabled,
        app_domain=route_domain.normalize_domain(payload.app_domain),
        # §34. Somebody sent this body; only the startup adoption writes a row
        # nobody asked for, and it says so with a different value.
        origin="manual",
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

    # §34, and the same rule as the token above: an omitted key keeps the stored
    # one, an empty one is refused rather than treated as "clear it". Clearing a
    # credential in place would leave a registration that lists, looks healthy
    # and authenticates as nobody — de-registering the cluster is how you remove
    # a credential, and it is the operation that says so.
    if "client_key" in fields:
        client_key = fields.pop("client_key")
        if not client_key:
            raise Invalid(
                "A cluster cannot be saved with an empty client key.",
                hint="Omit the field entirely to keep the key already stored.",
                context={"field": "client_key"},
            )
        cluster.client_key_encrypted = encrypt(client_key)

    if "app_domain" in fields:
        # Normalised here rather than in the loop: it is the one field where a
        # blank is an instruction ("stop generating hostnames") rather than an
        # absent value, and normalize_domain refuses anything that would build a
        # hostname DNS cannot resolve.
        cluster.app_domain = route_domain.normalize_domain(fields.pop("app_domain"))

    for key, value in fields.items():
        if value is not None:
            setattr(cluster, key, value.strip() if isinstance(value, str) else value)

    # §34. Checked against the row as it will be saved, not against the body:
    # a PUT that only flips `authentication_type` to client_certificate on a
    # cluster registered with a token sends no credential fields at all, so a
    # body-shaped check would wave it through and the next request would fail in
    # the TLS handshake.
    _validate_credential(
        authentication_type=cluster.authentication_type,
        token="stored" if cluster.token_encrypted else None,
        client_certificate=cluster.client_certificate,
        client_key="stored" if cluster.client_key_encrypted else None,
    )

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
# §34 — onboarding: what is on this machine, and adopting it
# --------------------------------------------------------------------------- #

class ClusterImport(BaseModel):
    """§34 POST body. One kubeconfig context, by name.

    Deliberately *not* the credential. The browser names a context and the
    backend re-reads the file; a body carrying the certificate and key would
    mean discovery had to return them, and then the onboarding panel would be
    the one page in this console that renders a private key.
    """

    context: str = Field(..., min_length=1, max_length=253)
    #: Defaults to the context name, which is what kind and k3d already call the
    #: cluster. A second field to fill in is a second chance to abandon the form.
    name: str | None = Field(None, min_length=1, max_length=255)
    app_domain: str | None = None


def _as_admin_error(e: kubeconfig_reader.KubeconfigError) -> AdminError:
    """Turn a kubeconfig failure into the §1.3 code the frontend branches on.

    The mapping is the whole point, so it is spelled out rather than defaulted:

    * ``not_found`` → 404. There is no such file, or no such context in it.
    * ``forbidden`` → **422, not 403**. The file is unreadable *by this process*
      — a mode-600 kubeconfig against a container running as uid 10001, which is
      the single most common way this feature silently does nothing. `rbac_denied`
      would send the operator to edit a ClusterRole, and there is no cluster
      involved in this failure at all.
    * ``unsupported`` → **422, not 501**. `unsupported` means *the cluster* does
      not serve something and the UI renders it as an ordinary fact in grey.
      This is a refusal by this console about a credential it will not copy, and
      it has a sentence the operator needs to read.
    """
    if e.reason == "not_found":
        return NotFound(str(e), context={"resource": "kubeconfig"})
    return Invalid(str(e), context={"resource": "kubeconfig"})


@router.get("/clusters/discovery")
def discover_clusters(_admin=Depends(require_console_admin)) -> dict:
    """Every context in this machine's kubeconfig, with what could be done with it.

    Administrator-only, like §3's three writes, and for a narrower reason than
    theirs: this reports on a file on the console's own filesystem — its path,
    the contexts in it, the addresses they point at. None of that is credential
    material and all of it is somebody's infrastructure.

    **Reads nothing from any cluster.** No connection is attempted, so a
    candidate listed here is a candidate that *could* be registered, never one
    that is known to answer — ``POST /clusters/{id}/test`` is still what settles
    that, and the panel says so.

    An absent, unreadable or malformed kubeconfig is an ``unavailable`` entry
    and a 200, not an error. "This machine has no kubeconfig" is an ordinary
    state for a console whose clusters are registered by hand, and the
    difference between that and "there is one and I may not read it" is the
    whole diagnosis — which is why it is reported rather than flattened into an
    empty list.
    """
    from app.resources.envelope import envelope

    found = kubeconfig_reader.discover()
    body = envelope(
        [candidate.to_public_dict() for candidate in found.candidates],
        unavailable=list(found.unavailable),
    )
    body["source"] = {
        "path": found.path,
        "current_context": found.current_context,
        "in_container": kubeconfig_reader.running_in_container(),
    }
    # What startup adoption would have done, so the panel can explain a cluster
    # that is already there — or the absence of one. `adopted` is not a claim
    # that this process did it: the row may predate this boot. It is the name of
    # the context adoption *would* pick today, which is what makes "nothing was
    # adopted and here is why" answerable at all.
    adoptable = kubeconfig_reader.adoptable(found)
    body["auto_discovery"] = {
        "enabled": settings.auto_discover_local_cluster,
        "candidates": [candidate.context for candidate in adoptable],
    }
    return body


@router.post("/clusters/import", status_code=201)
def import_cluster(
    payload: ClusterImport,
    _admin=Depends(require_console_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Register one discovered context, copying its credential into the registry.

    Administrator-only for §3's reason, unchanged: this decides which API server
    this console's transport talks to. That the credential came off the disk
    rather than out of a form does not make it a smaller decision — if anything
    it makes it an easier one to make carelessly, which is why it is a POST
    somebody sends rather than something the discovery listing does on render.

    The import is a *copy*. After it returns, the kubeconfig has no further part
    in this cluster's life: rotating it, moving it or deleting it changes
    nothing here, and re-importing is how you pick up a new credential. See
    `docs/adr-0012-kubeconfig-onboarding.md`.
    """
    try:
        credentials = kubeconfig_reader.credentials_for(payload.context)
    except kubeconfig_reader.KubeconfigError as e:
        raise _as_admin_error(e) from e

    cluster = adoption.register_context(
        db,
        credentials,
        name=(payload.name or credentials.context).strip(),
        origin=adoption.ORIGIN_IMPORTED,
        app_domain=route_domain.normalize_domain(payload.app_domain),
    )
    logger.info(
        "Imported cluster id=%s name=%s from kubeconfig context %s (%s)",
        cluster.id, cluster.name, credentials.context,
        credentials.distribution or "no recognised distribution",
    )
    return cluster.to_public_dict()


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
