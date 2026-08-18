"""Local identities, LDAP synchronization, and revocable opaque sessions."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import database
from app.config import settings
from app.errors import Invalid
from app.models import AuthSession, User, utcnow

logger = logging.getLogger(__name__)

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
PASSWORD_MIN_LENGTH = 12
ROLES = frozenset({"admin", "user"})
AUTH_SOURCES = frozenset({"local", "ldap"})


@dataclass(frozen=True)
class Principal:
    """The trusted identity attached to one request."""

    id: int
    username: str
    display_name: str | None
    email: str | None
    role: str
    auth_source: str

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "role": self.role,
            "auth_source": self.auth_source,
        }


@dataclass(frozen=True)
class SessionIdentity:
    """A principal plus the CSRF value bound to its browser session."""

    principal: Principal
    csrf_token: str
    expires_at: datetime.datetime


def normalize_username(value: str) -> str:
    """Canonical username used for both local and LDAP identities."""
    username = (value or "").strip().casefold()
    if not username or len(username) > 255 or any(ch.isspace() or ord(ch) < 32 for ch in username):
        raise Invalid(
            "A username must be 1-255 non-whitespace characters.",
            context={"field": "username"},
        )
    return username


def validate_password(password: str) -> None:
    if len(password or "") < PASSWORD_MIN_LENGTH:
        raise Invalid(
            f"A local password must contain at least {PASSWORD_MIN_LENGTH} characters.",
            context={"field": "password"},
        )
    if len(password) > 4096:
        raise Invalid("The password is too long.", context={"field": "password"})


def hash_password(password: str, *, validate: bool = True) -> str:
    """Return a versioned salted PBKDF2 hash suitable for database storage."""
    if validate:
        validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS, dklen=32
    )
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    """Verify a local password without raising on malformed stored data."""
    try:
        algorithm, rounds, salt_text, digest_text = (encoded or "").split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(rounds), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, UnicodeError):
        return False


_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password", validate=False)


def _principal(user: User) -> Principal:
    return Principal(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        auth_source=user.auth_source,
    )


def find_user(db: Session, username: str) -> User | None:
    normalized = normalize_username(username)
    return db.scalar(select(User).where(User.username == normalized))


def create_local_user(
    db: Session,
    *,
    username: str,
    password: str,
    role: str = "user",
    display_name: str | None = None,
    email: str | None = None,
) -> User:
    normalized = normalize_username(username)
    if role not in ROLES:
        raise Invalid("Role must be either 'admin' or 'user'.", context={"field": "role"})
    if find_user(db, normalized) is not None:
        raise Invalid(
            f"User {normalized!r} already exists.", context={"field": "username"}
        )
    row = User(
        username=normalized,
        display_name=(display_name or "").strip() or None,
        email=(email or "").strip() or None,
        role=role,
        auth_source="local",
        password_hash=hash_password(password),
        active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def authenticate(db: Session, *, username: str, password: str, source: str = "auto") -> User | None:
    """Authenticate locally first, or bind through LDAP and synchronize metadata."""
    if source not in {"auto", *AUTH_SOURCES}:
        raise Invalid("Authentication source must be auto, local, or ldap.")
    try:
        normalized = normalize_username(username)
    except Invalid:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None

    row = find_user(db, normalized)
    if row is not None and not row.active:
        verify_password(password, row.password_hash or _DUMMY_PASSWORD_HASH)
        return None

    if row is not None and row.auth_source == "local":
        if source == "ldap" or not verify_password(password, row.password_hash):
            return None
        row.last_login = utcnow()
        db.commit()
        db.refresh(row)
        return row

    if source == "local" or not settings.ldap_enabled:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None

    from app.identity import ldap as ldap_provider

    profile = ldap_provider.authenticate(normalized, password)
    if profile is None:
        return None

    profile_username = normalize_username(profile.username or normalized)
    existing = find_user(db, profile_username)
    if existing is not None and existing.auth_source != "ldap":
        # An LDAP identity must never take over a same-named local account.
        return None
    if existing is not None and not existing.active:
        return None

    row = existing or User(username=profile_username, auth_source="ldap", active=True)
    row.display_name = profile.display_name
    row.email = profile.email
    row.role = "admin" if profile.is_admin else "user"
    row.password_hash = None
    row.last_login = utcnow()
    if existing is None:
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_session(db: Session, user: User) -> tuple[str, SessionIdentity]:
    """Create a session and return the one-time raw bearer token to set as a cookie."""
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = utcnow() + datetime.timedelta(hours=settings.auth_session_ttl_hours)
    db.add(
        AuthSession(
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            user_id=user.id,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
    )
    db.commit()
    return raw_token, SessionIdentity(_principal(user), csrf_token, expires_at)


def load_session(raw_token: str | None) -> SessionIdentity | None:
    """Resolve a raw cookie value using a short-lived database session."""
    if not raw_token:
        return None
    db = database.SessionLocal()
    try:
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        row = db.get(AuthSession, token_hash)
        if row is None:
            return None
        if row.expires_at <= utcnow():
            db.delete(row)
            db.commit()
            return None
        user = db.get(User, row.user_id)
        if user is None or not user.active:
            db.delete(row)
            db.commit()
            return None
        return SessionIdentity(_principal(user), row.csrf_token, row.expires_at)
    finally:
        db.close()


def revoke_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    db = database.SessionLocal()
    try:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash))
        db.commit()
    finally:
        db.close()


def revoke_user_sessions(db: Session, user_id: int) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


def active_admin_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(User).where(User.active.is_(True), User.role == "admin")
        )
        or 0
    )


def ensure_bootstrap_admin() -> None:
    """Create the first administrator once, or refuse an unusable auth deployment."""
    if not settings.auth_enabled:
        return
    db = database.SessionLocal()
    try:
        if db.scalar(select(func.count()).select_from(User)):
            return
        username = settings.auth_bootstrap_username.strip()
        password = settings.auth_bootstrap_password.get_secret_value()
        if username and password:
            create_local_user(db, username=username, password=password, role="admin")
            logger.warning("Created bootstrap administrator %s", normalize_username(username))
            return
        if not settings.ldap_enabled:
            raise RuntimeError(
                "AUTH_ENABLED is true but no users exist. Set AUTH_BOOTSTRAP_USERNAME "
                "and AUTH_BOOTSTRAP_PASSWORD, or configure LDAP_ENABLED."
            )
        logger.warning(
            "Authentication is enabled with LDAP and no local users. Configure "
            "LDAP_ADMIN_GROUP_DN so at least one directory user can administer accounts."
        )
    finally:
        db.close()