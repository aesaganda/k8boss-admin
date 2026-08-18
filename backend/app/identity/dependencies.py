"""FastAPI dependencies for trusted console identities and roles."""

from __future__ import annotations

from fastapi import Request

from app.config import settings
from app.errors import AuthenticationRequired, PermissionDenied
from app.identity.service import Principal, SessionIdentity


def current_session(request: Request) -> SessionIdentity:
    session = getattr(request.state, "auth_session", None)
    if not settings.auth_enabled or session is None:
        raise AuthenticationRequired()
    return session


def current_principal(request: Request) -> Principal:
    return current_session(request).principal


def require_admin(request: Request) -> Principal:
    principal = current_principal(request)
    if principal.role != "admin":
        raise PermissionDenied("Administrator access is required.")
    return principal


def require_console_admin(request: Request) -> Principal | None:
    """Administrator when the console authenticates; the proxy's call when it does not.

    ``require_admin`` is the right dependency for the user-administration
    endpoints, which have no meaning at all without application authentication.
    It is the wrong one for an endpoint that a legacy proxy-mode deployment still
    needs — with ``AUTH_ENABLED=false`` there is no session, so ``require_admin``
    raises ``authentication_required`` and the endpoint becomes permanently
    unreachable rather than merely ungated.

    That failure is quiet in exactly the wrong way: the audit *export* would 401
    on the default deployment, and an operator who cannot extract the trail
    concludes the feature is broken, not that a gate they never configured is
    refusing them. So when application auth is off, this returns ``None`` and the
    endpoint is as open as every other endpoint in that mode — the authenticating
    proxy in front is what decides, which is the whole premise of that mode.

    Returning ``None`` rather than a synthetic principal is deliberate: a caller
    that treats the return value as an identity must fail loudly here rather than
    audit an action against an invented user.
    """
    if not settings.auth_enabled:
        return None
    return require_admin(request)
