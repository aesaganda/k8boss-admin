"""§12.7 active sessions and §12.8 the configured sign-in methods."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.identity import provider_config, sso
from app.identity.service import create_local_user
from app.models import AuditRecord, AuthSession, User, utcnow


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "ldap_enabled", False)
    return settings


def _signed_in_admin(client, db_session, username: str = "admin") -> dict:
    create_local_user(
        db_session,
        username=username,
        password="correct-horse-battery-staple",
        role="admin",
    )
    response = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": "correct-horse-battery-staple",
            "source": "local",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_session_list_marks_the_caller_and_names_where_it_came_from(
    client, db_session, auth_enabled
):
    """The row an administrator must not confuse with somebody else's.

    Provenance is the point of the page: a revoke list of identical rows is one
    nobody can act on safely, and the row that signs *you* out is the one worth
    labelling before it is clicked rather than after.
    """
    _signed_in_admin(client, db_session)

    body = client.get("/api/auth/sessions").json()

    assert body["partial"] is False
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["username"] == "admin"
    assert row["auth_source"] == "local"
    assert row["current"] is True
    # Captured at sign-in from the request, not invented later.
    assert row["ip_address"] == "testclient"
    assert row["user_agent"] == "testclient"
    assert row["created_at"].endswith("Z")
    assert row["last_used_at"] is not None
    # The stored digest, which is what the table is keyed by — and not the
    # bearer token, which is never stored at all.
    assert len(row["id"]) == 64
    assert client.cookies.get(settings.auth_cookie_name) != row["id"]


def test_an_expired_session_is_not_counted_as_active(client, db_session, auth_enabled):
    """Expired rows are filtered, never listed and never pruned by a GET.

    They are deleted lazily, when the cookie is next presented, so a browser
    that was simply closed leaves its row behind. Counting those would answer
    "who can act on this console right now" with a number that includes people
    who cannot — and pruning them here would make a listing a write.
    """
    _signed_in_admin(client, db_session)
    member = create_local_user(
        db_session, username="member", password="correct-horse-battery-staple"
    )
    db_session.add(
        AuthSession(
            token_hash="e" * 64,
            user_id=member.id,
            csrf_token="csrf",
            expires_at=utcnow() - datetime.timedelta(hours=1),
        )
    )
    db_session.commit()

    body = client.get("/api/auth/sessions").json()

    assert [row["username"] for row in body["items"]] == ["admin"]
    # Still on disk: the listing read past it rather than deleting it.
    db_session.expire_all()
    assert db_session.get(AuthSession, "e" * 64) is not None


def test_last_used_is_refreshed_coarsely_rather_than_on_every_request(
    client, db_session, auth_enabled
):
    """One UPDATE per minute per session, not one per request.

    `load_session` runs on every authenticated request. Writing the timestamp
    each time would put a commit in front of every read the console serves, and
    §12.7 only needs the value to answer "is this session still in use".
    """
    _signed_in_admin(client, db_session)
    row = db_session.scalar(select(AuthSession))

    # Inside the granularity window: the stored value must not move.
    fresh = utcnow()
    row.last_used_at = fresh
    db_session.commit()
    assert client.get("/api/auth/me").status_code == 200
    db_session.expire_all()
    assert db_session.scalar(select(AuthSession)).last_used_at == fresh

    # Outside it: the next request refreshes it.
    stale = utcnow() - datetime.timedelta(hours=2)
    row = db_session.scalar(select(AuthSession))
    row.last_used_at = stale
    db_session.commit()
    assert client.get("/api/auth/me").status_code == 200
    db_session.expire_all()
    assert db_session.scalar(select(AuthSession)).last_used_at > stale


def test_a_failed_last_used_write_does_not_sign_anybody_out(
    client, db_session, auth_enabled, monkeypatch
):
    """The bookkeeping write cannot become an authentication failure.

    `load_session` decides whether the caller is signed in and now writes on the
    way through. A database that has lost UPDATE would otherwise turn a stale
    timestamp into a 500 on every authenticated request — the whole console
    refusing everyone over a column nothing depends on.
    """
    _signed_in_admin(client, db_session)
    row = db_session.scalar(select(AuthSession))
    row.last_used_at = utcnow() - datetime.timedelta(hours=2)
    db_session.commit()

    def _no_writes(self):
        raise OperationalError("UPDATE auth_sessions", {}, Exception("read-only"))

    monkeypatch.setattr(Session, "commit", _no_writes)

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["user"]["username"] == "admin"


def test_revoking_a_session_ends_it_and_is_audited(client, db_session, auth_enabled):
    session = _signed_in_admin(client, db_session)
    member = create_local_user(
        db_session, username="member", password="correct-horse-battery-staple"
    )
    db_session.add(
        AuthSession(
            token_hash="a" * 64,
            user_id=member.id,
            csrf_token="csrf",
            expires_at=utcnow() + datetime.timedelta(hours=1),
        )
    )
    db_session.commit()

    response = client.delete(
        f"/api/auth/sessions/{'a' * 64}",
        headers={"X-CSRF-Token": session["csrfToken"]},
    )

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(AuthSession, "a" * 64) is None
    record = db_session.scalars(
        select(AuditRecord).where(AuditRecord.verb == "delete")
    ).one()
    assert record.outcome == "applied"
    assert record.actor == "admin"
    assert record.category == "console"
    # Whose session it was, which is the question asked of this row later. The
    # digest is not a searchable answer to it.
    assert record.target["name"] == "member"
    assert record.target["resource"] == "sessions"


def test_revoking_a_session_that_is_not_there_is_a_404_and_a_denied_record(
    client, db_session, auth_enabled
):
    """"I revoked that" and "there was no such session" are different answers.

    A revocation that found nothing, recorded as a success, tells an incident
    review that access was cut when it was not.
    """
    session = _signed_in_admin(client, db_session)

    response = client.delete(
        f"/api/auth/sessions/{'b' * 64}",
        headers={"X-CSRF-Token": session["csrfToken"]},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    record = db_session.scalars(
        select(AuditRecord).where(AuditRecord.verb == "delete")
    ).one()
    assert record.outcome == "denied"


def test_a_token_digest_that_is_not_one_is_invalid_rather_than_missing(
    client, db_session, auth_enabled
):
    session = _signed_in_admin(client, db_session)
    response = client.delete(
        "/api/auth/sessions/not-a-digest",
        headers={"X-CSRF-Token": session["csrfToken"]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid"


def test_sessions_and_sign_in_methods_are_administrator_only(
    client, db_session, auth_enabled
):
    create_local_user(
        db_session, username="member", password="correct-horse-battery-staple"
    )
    login = client.post(
        "/api/auth/login",
        json={
            "username": "member",
            "password": "correct-horse-battery-staple",
            "source": "local",
        },
    )
    assert login.status_code == 200

    for response in (
        client.get("/api/auth/sessions"),
        client.get("/api/auth/providers"),
        client.delete(
            f"/api/auth/sessions/{'c' * 64}",
            headers={"X-CSRF-Token": login.json()["csrfToken"]},
        ),
    ):
        assert response.status_code == 403
        assert response.json()["error"] == "permission_denied"


def test_sign_in_methods_name_where_each_one_points_and_what_confers_admin(
    client, db_session, auth_enabled, monkeypatch
):
    """§12.8 is the private half of §12.1, and it is admin-gated for that reason.

    The public discovery endpoint deliberately withholds every value here. An
    administrator looking at a user row and asking "why is this person an
    administrator" is asking about the group DN, and reading the container's
    environment is not an answer available from the Users page.
    """
    monkeypatch.setattr(settings, "ldap_enabled", True)
    monkeypatch.setattr(settings, "ldap_url", "ldaps://directory.internal.example:636")
    monkeypatch.setattr(settings, "ldap_admin_group_dn", "cn=platform-admins,ou=groups")
    _signed_in_admin(client, db_session)

    rows = {row["name"]: row for row in client.get("/api/auth/providers").json()["items"]}

    assert rows["local"]["enabled"] is True
    assert rows["local"]["editable"] is False
    ldap = rows["ldap"]
    assert ldap["title"] == "LDAP / Active Directory"
    assert ldap["enabled"] is True
    assert ldap["endpoint"] == "ldaps://directory.internal.example:636"
    assert ldap["admin_group"] == "cn=platform-admins,ou=groups"
    assert ldap["settings_prefix"] == "LDAP_"
    # ADR-0011: nothing is stored, so this is the deployment's environment
    # answering — and the console says which of the two it was.
    assert ldap["source"] == "environment"
    assert ldap["stored"] is False
    # Unconfigured methods are listed too, and `enabled` is what tells them
    # apart from a method this console does not have at all.
    assert rows["saml"]["enabled"] is False
    assert rows["saml"]["endpoint"] is None


def test_a_provider_field_the_environment_cannot_answer_is_a_broken_spec():
    """Every field in every spec must have an environment setting behind it.

    `from_env` builds the fallback by reading ``settings.{kind}_{field}``, so a
    field whose name does not match a setting reads as empty forever — an
    administrator would see the field on the form, leave it alone, and get a
    silently blank value rather than the deployment's configured one. There is
    no runtime signal for that at all, so it is asserted here.
    """
    for kind in provider_config.KINDS:
        for field in provider_config.spec(kind).fields:
            attribute = f"{kind}_{field.name}"
            assert hasattr(settings, attribute), (
                f"{kind}.{field.name} has no {attribute} setting behind it"
            )


def test_every_single_sign_on_provider_has_a_spec():
    """A fifth registry provider fails here rather than on screen.

    The §12.8 panel is built from `provider_config.SPECS`, so a provider that
    can complete a sign-in and has no spec would be missing from the screen that
    configures it — and from the screen an administrator checks to find out how
    people are getting in.
    """
    assert set(sso.providers()) <= set(provider_config.SPECS)


def test_deactivating_a_user_removes_their_sessions_from_the_list(
    client, db_session, auth_enabled
):
    """§12.6 already revoked them; this is the listing agreeing with it."""
    session = _signed_in_admin(client, db_session)
    member = create_local_user(
        db_session, username="member", password="correct-horse-battery-staple"
    )
    db_session.add(
        AuthSession(
            token_hash="d" * 64,
            user_id=member.id,
            csrf_token="csrf",
            expires_at=utcnow() + datetime.timedelta(hours=1),
        )
    )
    db_session.commit()

    client.delete(
        f"/api/auth/users/{member.id}",
        headers={"X-CSRF-Token": session["csrfToken"]},
    )

    body = client.get("/api/auth/sessions").json()
    assert [row["username"] for row in body["items"]] == ["admin"]
    db_session.expire_all()
    assert db_session.get(User, member.id).active is False
