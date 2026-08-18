"""Console login, session identity, and administrator-managed users."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import recorder
from app.config import settings
from app.database import get_db
from app.errors import Invalid, InvalidCredentials, NotFound
from app.identity.dependencies import current_session, require_admin
from app.identity.service import (
    ROLES,
    active_admin_count,
    authenticate,
    create_local_user,
    create_session,
    hash_password,
    revoke_session,
    revoke_user_sessions,
)
from app.models import User, rfc3339
from app.resources.envelope import envelope

router = APIRouter(prefix="/api/auth", tags=["authentication"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=4096)
    source: str = Field("auto", pattern="^(auto|local|ldap)$")


class UserCreateBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=12, max_length=4096)
    display_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=320)
    role: str = Field("user", pattern="^(admin|user)$")


class UserUpdateBody(BaseModel):
    display_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=320)
    role: str | None = Field(None, pattern="^(admin|user)$")
    active: bool | None = None
    password: str | None = Field(None, min_length=12, max_length=4096)


def _session_body(identity) -> dict:
    return {
        "enabled": True,
        "authenticated": True,
        "user": identity.principal.to_public_dict(),
        "csrfToken": identity.csrf_token,
        "expiresAt": rfc3339(identity.expires_at),
    }


def _audit_user_action(verb: str, username: str, detail: str) -> None:
    recorder.record(
        verb=verb,
        target={
            "group": "k8boss-admin.io",
            "version": "v1",
            "resource": "users",
            "namespace": None,
            "name": username,
        },
        dry_run=False,
        outcome="applied",
        detail=detail,
        cluster_scoped=False,
    )


@router.get("/config")
def auth_config() -> dict:
    return {
        "enabled": settings.auth_enabled,
        "localEnabled": True,
        "ldapEnabled": settings.ldap_enabled,
        "methods": ["local", *(("ldap",) if settings.ldap_enabled else ())],
    }


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)) -> JSONResponse:
    if not settings.auth_enabled:
        raise Invalid("Application authentication is not enabled on this deployment.")
    user = authenticate(db, username=body.username, password=body.password, source=body.source)
    if user is None:
        raise InvalidCredentials()
    raw_token, identity = create_session(db, user)
    response = JSONResponse(content=_session_body(identity))
    response.set_cookie(
        settings.auth_cookie_name,
        raw_token,
        max_age=settings.auth_session_ttl_hours * 3600,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/me")
def me(identity=Depends(current_session)) -> dict:
    return _session_body(identity)


@router.post("/logout")
def logout(request: Request) -> Response:
    revoke_session(request.cookies.get(settings.auth_cookie_name))
    response = Response(status_code=204)
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/users")
def list_users(_admin=Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(User).order_by(User.username)).all()
    return envelope(row.to_public_dict() for row in rows)


@router.post("/users", status_code=201)
def create_user(
    body: UserCreateBody,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    row = create_local_user(
        db,
        username=body.username,
        password=body.password,
        display_name=body.display_name,
        email=body.email,
        role=body.role,
    )
    _audit_user_action("create", row.username, f"Created local console user {row.username}.")
    return row.to_public_dict()


def _load_user(db: Session, user_id: int) -> User:
    row = db.get(User, user_id)
    if row is None:
        raise NotFound(
            f"Console user {user_id} does not exist.",
            context={"resource": "users", "name": str(user_id)},
        )
    return row


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdateBody,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    row = _load_user(db, user_id)
    changes = body.model_dump(exclude_unset=True)
    if row.auth_source == "ldap" and any(key in changes for key in ("role", "password")):
        raise Invalid(
            "LDAP roles and passwords are managed by the directory and refresh at login."
        )
    removing_admin = row.active and row.role == "admin" and (
        changes.get("active") is False or changes.get("role") == "user"
    )
    if removing_admin and active_admin_count(db) <= 1:
        raise Invalid("The last active administrator cannot be demoted or deactivated.")
    if row.id == admin.id and changes.get("active") is False:
        raise Invalid("You cannot deactivate your own signed-in account.")

    if "display_name" in changes:
        row.display_name = (changes["display_name"] or "").strip() or None
    if "email" in changes:
        row.email = (changes["email"] or "").strip() or None
    if "role" in changes:
        if changes["role"] not in ROLES:
            raise Invalid("Role must be either 'admin' or 'user'.")
        row.role = changes["role"]
    if "active" in changes:
        row.active = changes["active"]
    if changes.get("password"):
        if row.auth_source != "local":
            raise Invalid("Only local users have passwords stored by this console.")
        row.password_hash = hash_password(changes["password"])
        revoke_user_sessions(db, row.id)
    if changes.get("active") is False:
        revoke_user_sessions(db, row.id)
    db.commit()
    db.refresh(row)
    _audit_user_action("patch", row.username, f"Updated console user {row.username}.")
    return row.to_public_dict()


@router.delete("/users/{user_id}", status_code=204)
def deactivate_user(
    user_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    row = _load_user(db, user_id)
    if row.id == admin.id:
        raise Invalid("You cannot deactivate your own signed-in account.")
    if row.active and row.role == "admin" and active_admin_count(db) <= 1:
        raise Invalid("The last active administrator cannot be deactivated.")
    row.active = False
    revoke_user_sessions(db, row.id)
    db.commit()
    _audit_user_action("delete", row.username, f"Deactivated console user {row.username}.")
    return Response(status_code=204)