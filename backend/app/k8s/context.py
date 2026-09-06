"""
Per-request cluster and caller context.

Handlers never take ``cluster_id`` as an argument (§1.1). The middleware reads
``?cluster_id=`` once and pins it in a :class:`contextvars.ContextVar`; every
Kubernetes helper below reads it from there. Threading the id through ~200 call
signatures is the alternative, and the failure mode of that alternative is one
function that forgets to pass it and quietly answers about a different cluster —
a misattributed answer, which is worse than no answer.

**Pure ASGI, not BaseHTTPMiddleware.** ``BaseHTTPMiddleware`` runs the downstream
app in a separate anyio task, and a contextvar set in the middleware's own frame
does not reliably survive into the threadpool that executes sync route handlers.
The symptom is not an exception: it is ``get_current_cluster_id()`` returning
None inside the handler and the request silently resolving to the fallback
cluster. Pure ASGI keeps the set/reset in the same task that awaits the app.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any
from urllib.parse import parse_qs

from app.config import settings

logger = logging.getLogger(__name__)

# None means "no cluster named on this request" — the manager then falls back to
# the single registered cluster, or raises NoClusterSelected.
_current_cluster_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_cluster_id", default=None
)

# Verified session identity when application auth is enabled; otherwise the
# legacy advisory X-K8Boss-User value used by authenticating-proxy deployments.
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user", default="anonymous"
)

# Peer address, recorded on audit rows alongside the actor.
_current_source_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_source_ip", default=None
)

# ADR-0007. The whole verified principal, not just its username, because
# impersonation needs the identity provider's own username and groups and those
# cannot be recovered from a string. Typed `Any` rather than `Principal`:
# `app.identity.service` imports `app.database`, and importing it here would put
# the ORM in the import path of every Kubernetes helper.
#
# None in legacy proxy mode and for anonymous requests, which is exactly the
# state `decide` refuses on an impersonating cluster.
_current_principal: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "current_principal", default=None
)


def set_current_cluster_id(cluster_id: int | None):
    """Pin the cluster for this context. Returns a token for ``reset``."""
    return _current_cluster_id.set(cluster_id)


def reset_current_cluster_id(token) -> None:
    _current_cluster_id.reset(token)


def get_current_cluster_id() -> int | None:
    """The cluster id named on this request, or None for 'the active cluster'."""
    return _current_cluster_id.get()


def set_current_user(user: str):
    return _current_user.set(user)


def reset_current_user(token) -> None:
    _current_user.reset(token)


def get_current_user() -> str:
    """Verified caller identity, or the legacy advisory actor when auth is off."""
    return _current_user.get()


def set_current_source_ip(ip: str | None):
    return _current_source_ip.set(ip)


def reset_current_source_ip(token) -> None:
    _current_source_ip.reset(token)


def get_current_source_ip() -> str | None:
    """Peer address of the caller, or None when the transport did not report one."""
    return _current_source_ip.get()


def set_current_principal(principal: Any):
    return _current_principal.set(principal)


def reset_current_principal(token) -> None:
    _current_principal.reset(token)


def get_current_principal() -> Any:
    """The verified session identity, or ``None``.

    ``None`` covers three different situations that all mean the same thing to
    ADR-0007 — application auth is off, nobody is signed in, or the endpoint is
    a public one — and all three refuse impersonation rather than falling back
    to the console's own credential.
    """
    return _current_principal.get()


class ClusterContextMiddleware:
    """ASGI middleware pinning cluster + caller context for the request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # WebSocket scopes carry ?cluster_id= too (§7 log and exec streams), so
        # they get the same treatment. Lifespan and anything else passes through
        # untouched — setting a contextvar around the lifespan span would pin one
        # value for the entire process.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        cluster_id: int | None = None
        raw = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("cluster_id")
        if raw:
            # int(), not str.isdigit(): isdigit rejects a leading '-', which
            # silently drops the cluster context for any negative id — and test
            # fixtures pick negative ids precisely to avoid colliding with a real
            # autoincrement primary key. A dropped context does not fail, it
            # answers about a different cluster.
            try:
                cluster_id = int(raw[0])
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring non-integer cluster_id on %s; falling back to the "
                    "active cluster.", scope.get("path"),
                )

        principal = scope.get("state", {}).get("auth_principal")
        user = principal.username[:255] if settings.auth_enabled and principal else "anonymous"
        if not settings.auth_enabled:
            for key, value in scope.get("headers", []):
                if key == b"x-k8boss-user":
                    # Bounded and latin-1 decoded: the legacy header is
                    # caller-controlled and lands in AuditRecord.actor.
                    candidate = value.decode("latin-1").strip()
                    if candidate:
                        user = candidate[:255]
                    break

        client = scope.get("client")
        source_ip = client[0] if client else None

        tokens = (
            set_current_cluster_id(cluster_id),
            set_current_user(user),
            set_current_source_ip(source_ip),
            # Only ever the *verified* principal. In legacy proxy mode `user`
            # above comes from a caller-controlled header, and pinning that as
            # an identity would let anyone who can reach the port pick a cluster
            # identity under ADR-0007.
            set_current_principal(principal if settings.auth_enabled else None),
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_cluster_id(tokens[0])
            reset_current_user(tokens[1])
            reset_current_source_ip(tokens[2])
            reset_current_principal(tokens[3])
