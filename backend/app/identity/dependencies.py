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