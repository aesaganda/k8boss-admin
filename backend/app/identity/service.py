"""Local identities, LDAP synchronization, and revocable opaque sessions."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import database
from app.config import settings
from app.errors import Invalid, PermissionDenied
from app.identity import roles
from app.models import AuthSession, User, rfc3339, utcnow

logger = logging.getLogger(__name__)

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
PASSWORD_MIN_LENGTH = 12
#: Re-exported from :mod:`app.identity.roles`, which owns the vocabulary because
#: two providers map onto it. Kept as a name here so existing importers of
#: ``service.ROLES`` do not have to move.
ROLES = roles.ROLES
AUTH_SOURCES = frozenset({"local", "ldap", "oidc", "oauth", "openshift", "saml"})

#: Auth sources whose role and profile are owned by the identity provider and
#: refreshed at login. A console administrator cannot edit these fields, because
#: the next successful login would overwrite the edit and the operator would have
#: no way to see why it did not stick.
#:
#: Every source but ``local``. Derived by subtraction rather than written out a
#: second time: the guard in ``PUT /api/auth/users/{id}`` was originally a
#: literal ``"ldap"``, and when OIDC arrived an administrator could promote an
#: OIDC account and watch the change silently revert at that user's next sign-in
#: with nothing anywhere explaining why. A fifth provider must not be able to
#: reintroduce that by being added to one list and not the other.
FEDERATED_SOURCES = AUTH_SOURCES - {"local"}


@dataclass(frozen=True)
class Principal:
    """The trusted identity attached to one request."""

    id: int
    username: str
    display_name: str | None
    email: str | None
    role: str
    auth_source: str

    # ADR-0007. The identity provider's own words, carried for the life of the
    # session and used for nothing but building `Impersonate-*` headers.
    #
    # `username` above is this console's key for the person — normalised and
    # casefolded — and is the wrong string to send an API server: a cluster
    # whose `--oidc-username-claim` yields `Alice@example.com` does not know
    # anybody called `alice@example.com`. So the claim is kept unmodified beside
    # it rather than derived back out of it, which cannot be done.
    #
    # `idp_groups` is `None` when the issuer sent no groups claim, and that is
    # not `()`. See `app.k8s.impersonation.decide`, which refuses on the first
    # and impersonates happily on the second.
    idp_username: str | None = None
    idp_groups: tuple[str, ...] | None = None

    @property
    def can_impersonate(self) -> bool:
        """Whether this session could act as a cluster identity at all.

        Reported to the UI so a cluster that impersonates can say *before* the
        first request why it will refuse — rule 11.4 applied to a refusal that
        is not about permissions. It does not consult any cluster: whether a
        given cluster asks for impersonation is that cluster's own flag.
        """
        from app.k8s.impersonation import IMPERSONATION_SOURCES

        return (
            self.auth_source in IMPERSONATION_SOURCES
            and bool(self.idp_username)
            and self.idp_groups is not None
        )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "role": self.role,
            "auth_source": self.auth_source,
            # ADR-0007. The name a cluster would see, and whether one can be
            # produced at all. The *groups* are deliberately not echoed: the UI
            # has no use for them, and a list this console received in a token
            # is not something to hand back out because it happened to be in
            # memory.
            "idp_username": self.idp_username,
            "can_impersonate": self.can_impersonate,
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


def _principal(
    user: User,
    *,
    idp_username: str | None = None,
    idp_groups: tuple[str, ...] | None = None,
) -> Principal:
    return Principal(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        auth_source=user.auth_source,
        idp_username=idp_username,
        idp_groups=idp_groups,
    )


def _encode_groups(groups: tuple[str, ...] | None) -> str | None:
    """Groups for the session row. ``None`` stays NULL — the absent claim."""
    if groups is None:
        return None
    return json.dumps([str(group) for group in groups])


def _decode_groups(raw: str | None) -> tuple[str, ...] | None:
    """Groups back off the session row, preserving absent-versus-empty.

    A stored value this function cannot parse is treated as **absent**, not as
    empty. Both are wrong answers, and only one of them is dangerous: absent
    refuses impersonation and says why, while empty impersonates with no groups
    and reports the operator's real permissions as denied.
    """
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring unparseable idp_groups on a session row.")
        return None
    if not isinstance(decoded, list):
        logger.warning("Ignoring non-list idp_groups on a session row.")
        return None
    return tuple(str(group) for group in decoded)


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
    row.role = _resolved_role(
        roles.role_from_groups(
            profile.groups,
            admin_group=settings.ldap_admin_group_dn,
            provider="ldap",
        ),
        existing=existing,
        username=profile_username,
    )
    row.password_hash = None
    row.last_login = utcnow()
    if existing is None:
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _resolved_role(mapped: str | None, *, existing: User | None, username: str) -> str:
    """The role to store, given what the provider was able to tell us.

    ``mapped is None`` means the provider could not resolve group membership at
    all. Writing the default there is what silently demoted directory
    administrators (see :mod:`app.identity.roles`), so the stored role is kept
    instead. A brand-new account has no stored role to keep and gets the default,
    which is the safe direction: a person who has never signed in before is not
    made an administrator by a directory failure either.
    """
    if mapped is not None:
        return mapped
    if existing is not None:
        logger.warning(
            "Keeping the stored console role %r for %r: the directory did not "
            "report group membership on this login.", existing.role, username,
        )
        return existing.role
    logger.warning(
        "Creating %r with the default console role: the identity provider did "
        "not report group membership, so no role could be derived.", username,
    )
    return roles.DEFAULT_ROLE


def provision_federated_user(
    db: Session,
    *,
    source: str,
    username: str,
    external_id: str | None,
    display_name: str | None,
    email: str | None,
    groups: tuple[str, ...] | None,
    admin_group: str,
) -> User:
    """Create or refresh the local row behind a federated (SSO) identity.

    Two refusals here are the security-relevant part, and both are refusals
    rather than merges because a merge would be an account takeover that looked
    like a successful login:

    **A username already owned by another auth source is refused.** Usernames are
    globally unique in this schema. If ``alice`` is a local account with a
    password, an SSO login as ``alice`` must not adopt that row — otherwise
    anyone who can make an identity provider assert a username can inherit
    whatever that username already had.

    **A username already bound to a different provider subject is refused.** The
    subject claim (`sub`) is the provider's own stable identifier and is what
    actually names a person; the username is a label the provider can reuse. When
    an account carries a subject and the incoming one differs, this is either a
    second person with a recycled username or a second issuer asserting the same
    name, and neither may silently take over the first one's role.

    A row with a NULL ``external_id`` predates that binding and is adopted once,
    which is what lets an existing deployment turn SSO on without every account
    being refused. Rebinding an account to a new subject afterwards is
    deliberately not a login-time action.
    """
    if source not in FEDERATED_SOURCES:
        raise ValueError(f"{source!r} is not a federated auth source.")

    normalized = normalize_username(username)
    existing = find_user(db, normalized)

    if existing is not None and existing.auth_source != source:
        logger.warning(
            "Refusing %s login for %r: the username belongs to auth source %r.",
            source, normalized, existing.auth_source,
        )
        raise PermissionDenied(
            "This username is managed by a different authentication source.",
        )
    if (
        existing is not None
        and existing.external_id
        and external_id
        and existing.external_id != external_id
    ):
        logger.warning(
            "Refusing %s login for %r: the account is bound to a different "
            "provider subject.", source, normalized,
        )
        raise PermissionDenied(
            "This username is already bound to a different identity provider "
            "account.",
        )
    if existing is not None and not existing.active:
        raise PermissionDenied("This console account is deactivated.")

    row = existing or User(username=normalized, auth_source=source, active=True)
    row.display_name = display_name
    row.email = email
    row.external_id = external_id or row.external_id
    row.role = _resolved_role(
        roles.role_from_groups(groups, admin_group=admin_group, provider=source),
        existing=existing,
        username=normalized,
    )
    # A federated account never has a stored password. Clearing it matters on the
    # adoption path: a row that predated SSO could otherwise keep a usable
    # password hash while the console reports the account as provider-managed,
    # leaving a second way in that nobody is watching.
    row.password_hash = None
    row.last_login = utcnow()
    if existing is None:
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_session(
    db: Session,
    user: User,
    *,
    idp_username: str | None = None,
    idp_groups: tuple[str, ...] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, SessionIdentity]:
    """Create a session and return the one-time raw bearer token to set as a cookie.

    ``idp_username`` and ``idp_groups`` are ADR-0007's material and are supplied
    only by the single sign-on callback, which is the only caller that has an
    issuer's assertion in front of it. A local or LDAP sign-in passes neither,
    and the resulting session cannot impersonate — enforced again in
    ``app.k8s.impersonation.decide`` by ``auth_source``, because a session that
    somehow carried both would still be the console's password table asserting a
    cluster identity.

    ``ip_address`` and ``user_agent`` are §12.7's provenance, supplied by the
    route because it is the layer holding the request. Both default to ``None``,
    and ``None`` is stored as unknown rather than as a blank: a session list that
    rendered an empty cell as "no address" would be inviting an administrator to
    revoke by elimination.
    """
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = utcnow() + datetime.timedelta(hours=settings.auth_session_ttl_hours)
    db.add(
        AuthSession(
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            user_id=user.id,
            csrf_token=csrf_token,
            expires_at=expires_at,
            idp_username=idp_username,
            idp_groups=_encode_groups(idp_groups),
            ip_address=ip_address,
            user_agent=user_agent,
            # Set at creation rather than left NULL: a session that has been
            # used exactly once — the sign-in — has been used, and NULL here
            # means "not seen since the column existed", which is a different
            # sentence the listing renders differently.
            last_used_at=utcnow(),
        )
    )
    db.commit()
    return raw_token, SessionIdentity(
        _principal(user, idp_username=idp_username, idp_groups=idp_groups),
        csrf_token,
        expires_at,
    )


#: How stale ``AuthSession.last_used_at`` is allowed to be.
#:
#: This function runs on **every authenticated request**, so writing the
#: timestamp each time would put an UPDATE and a commit in front of every read
#: the console serves — a write-per-read on the one table every request already
#: touches. §12.7 only needs the value to answer "is this session still in use",
#: where a minute of lag changes no decision, so the row is written at most once
#: per minute per session and the listing documents the granularity rather than
#: implying a precision it does not have.
_LAST_USED_GRANULARITY = datetime.timedelta(minutes=1)


def load_session(raw_token: str | None) -> SessionIdentity | None:
    """Resolve a raw cookie value using a short-lived database session."""
    if not raw_token:
        return None
    db = database.SessionLocal()
    try:
        now = utcnow()
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        row = db.get(AuthSession, token_hash)
        if row is None:
            return None
        if row.expires_at <= now:
            db.delete(row)
            db.commit()
            return None
        user = db.get(User, row.user_id)
        if user is None or not user.active:
            db.delete(row)
            db.commit()
            return None
        identity = SessionIdentity(
            _principal(
                user,
                idp_username=row.idp_username,
                idp_groups=_decode_groups(row.idp_groups),
            ),
            row.csrf_token,
            row.expires_at,
        )
        # Built before the refresh, so the values returned are read off the row
        # rather than re-selected after the commit expired them.
        if row.last_used_at is None or now - row.last_used_at >= _LAST_USED_GRANULARITY:
            row.last_used_at = now
            try:
                db.commit()
            except SQLAlchemyError:
                # A failed bookkeeping write must never fail the request. This
                # function is what decides whether the caller is signed in, and
                # it now writes on the way through: a database that has lost
                # INSERT/UPDATE — full disk, a hot standby, a revoked grant —
                # would otherwise turn a stale timestamp into "every
                # authenticated request is a 500", which is the whole console
                # refusing everyone over a column nothing depends on. Same
                # reasoning as the audit recorder's failed INSERT: by the time
                # this runs, the answer to "is this session valid" is already
                # known and it is yes.
                db.rollback()
                logger.warning(
                    "Could not refresh last_used_at for a console session; the "
                    "session list will show a stale value. The session itself "
                    "is unaffected.", exc_info=True,
                )
        return identity
    finally:
        db.close()


def list_active_sessions(
    db: Session, *, current_token_hash: str | None = None
) -> list[dict]:
    """Every unexpired session, newest first, with who opened it and from where.

    **Expired rows are filtered rather than counted.** They are deleted lazily,
    when :func:`load_session` next sees one, so a browser that was closed leaves
    a row behind indefinitely; including those would make "142 active sessions"
    a number with no relationship to how many people can currently act on this
    console. Nothing here deletes them either — a listing that pruned would make
    a GET a write.

    Ordered by ``created_at`` descending and never by ``last_used_at``: that
    column is nullable, and the two engines disagree about where NULLs sort in a
    DESC order (PostgreSQL first, SQLite last). A listing whose row order
    depended on which database the deployment runs is the kind of divergence
    CLAUDE.md asks to be deliberate about, and here it buys nothing.

    ``id`` is the stored SHA-256 digest of the bearer token, which is what this
    table is keyed by. It is safe to hand to the browser and it is **not** a
    credential: the middleware hashes the cookie it is given and looks the row up
    by the result, so possessing the digest authenticates nobody.
    """
    rows = db.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.expires_at > utcnow())
        .order_by(AuthSession.created_at.desc(), AuthSession.token_hash)
    ).all()
    return [
        {
            "id": row.token_hash,
            "username": user.username,
            "display_name": user.display_name,
            "auth_source": user.auth_source,
            "role": user.role,
            "ip_address": row.ip_address,
            "user_agent": row.user_agent,
            "created_at": rfc3339(row.created_at),
            "last_used_at": rfc3339(row.last_used_at),
            "expires_at": rfc3339(row.expires_at),
            # Whether this row is the caller's own session. The one row whose
            # revocation signs the administrator out, which is a thing to know
            # before clicking rather than after.
            "current": bool(current_token_hash) and row.token_hash == current_token_hash,
        }
        for row, user in rows
    ]


def revoke_session_hash(db: Session, token_hash: str) -> str | None:
    """Revoke one session by its stored digest. Returns whose it was.

    The username is returned rather than looked up again by the caller, because
    the caller needs it for the audit row and the row is gone by then. ``None``
    means there was no such session — which the route reports as a 404 and
    records as a refusal, since "revoke a session that is not there" and "revoke
    a session" must not produce the same trail entry.
    """
    row = db.get(AuthSession, token_hash)
    if row is None:
        return None
    # The account row is deleted with its sessions by an ON DELETE CASCADE, so a
    # session without one should not exist. If it does, the revoke still happens
    # and the trail says the owner could not be named — the alternative is
    # refusing to revoke a session because of a defect in a different table.
    username = db.scalar(select(User.username).where(User.id == row.user_id))
    db.delete(row)
    db.commit()
    return username or "(unknown)"


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