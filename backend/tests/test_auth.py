"""Application authentication, local user administration, and LDAP login."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.config import settings
from app.identity.ldap import LDAPProfile
from app.identity.service import create_local_user
from app.models import AuditRecord, AuthSession, User


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "ldap_enabled", False)
    return settings


def _login(client, username: str, password: str, source: str = "auto") -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "source": source},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_discovery_is_public_and_disabled_by_default(client):
    assert client.get("/api/auth/config").json() == {
        "enabled": False,
        "localEnabled": True,
        "ldapEnabled": False,
        "oidcEnabled": False,
        "methods": ["local"],
        # The general form. Empty rather than absent: a login page that cannot
        # tell "no single sign-on is configured" from "this backend does not
        # report providers" would have to guess, and the guess that renders
        # nothing hides a working button on the deployment that has one.
        "ssoProviders": [],
        # The OpenID Connect entry of `ssoProviders`, repeated. Retained because
        # an already-loaded older build of the SPA reads it.
        "oidc": None,
    }


def test_auth_discovery_never_names_how_sso_is_configured(client, auth_enabled, monkeypatch):
    """The public endpoint says a method exists, never how it is wired.

    This is the one unauthenticated endpoint in the API, and the login page only
    needs to know which buttons to draw. Returning the issuer, the client id or
    the configured groups would let anyone who can reach the console enumerate
    its identity provider, which is reconnaissance for free.
    """
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.internal.example/realms/x")
    monkeypatch.setattr(settings, "oidc_client_id", "k8boss-admin-console")
    monkeypatch.setattr(settings, "oidc_admin_group", "cn=platform-admins")

    body = client.get("/api/auth/config").json()

    assert body["oidcEnabled"] is True
    assert body["methods"] == ["local", "oidc"]
    assert body["oidc"] == {"label": "Single sign-on", "startPath": "/api/auth/oidc/start"}
    serialized = client.get("/api/auth/config").text
    for secret in ("idp.internal.example", "k8boss-admin-console", "platform-admins"):
        assert secret not in serialized


def test_sso_is_not_offered_without_an_issuer_and_client_id(client, auth_enabled, monkeypatch):
    """A button that cannot work is worse than no button.

    An operator who clicks an SSO button and gets an error concludes the console
    is broken. One who sees no button concludes SSO is not set up, which is both
    true and actionable.
    """
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "")
    monkeypatch.setattr(settings, "oidc_client_id", "")

    body = client.get("/api/auth/config").json()

    assert body["oidcEnabled"] is False
    assert "oidc" not in body["methods"]


def test_enabled_auth_protects_api_but_keeps_health_and_login_public(client, auth_enabled):
    protected = client.get("/api/clusters")
    assert protected.status_code == 401
    assert protected.json()["error"] == "authentication_required"
    assert protected.headers["www-authenticate"] == "Session"
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/config").status_code == 200


def test_local_login_sets_an_httponly_cookie_and_stores_only_its_hash(
    client, db_session, auth_enabled
):
    create_local_user(
        db_session,
        username="Admin",
        password="correct-horse-battery-staple",
        role="admin",
    )
    response = client.post(
        "/api/auth/login",
        json={
            "username": "ADMIN",
            "password": "correct-horse-battery-staple",
            "source": "local",
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert body["authenticated"] is True
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == "admin"
    assert body["csrfToken"]
    assert "HttpOnly" in response.headers["set-cookie"]

    raw_cookie = client.cookies.get(settings.auth_cookie_name)
    db_session.expire_all()
    stored = db_session.scalar(select(AuthSession))
    assert stored.token_hash == hashlib.sha256(raw_cookie.encode()).hexdigest()
    assert raw_cookie not in stored.token_hash
    assert client.get("/api/auth/me").json()["user"]["username"] == "admin"


def test_invalid_login_does_not_reveal_whether_the_user_exists(client, db_session, auth_enabled):
    create_local_user(
        db_session,
        username="known",
        password="correct-horse-battery-staple",
    )
    unknown = client.post(
        "/api/auth/login", json={"username": "missing", "password": "wrong-password"}
    )
    known = client.post(
        "/api/auth/login", json={"username": "known", "password": "wrong-password"}
    )
    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json()
    assert unknown.json()["error"] == "invalid_credentials"


def test_unsafe_requests_require_the_session_csrf_token(client, db_session, auth_enabled):
    create_local_user(
        db_session,
        username="admin",
        password="correct-horse-battery-staple",
        role="admin",
    )
    session = _login(client, "admin", "correct-horse-battery-staple")
    payload = {"username": "operator", "password": "another-long-password"}

    rejected = client.post("/api/auth/users", json=payload)
    assert rejected.status_code == 403
    assert rejected.json()["error"] == "permission_denied"

    created = client.post(
        "/api/auth/users",
        json=payload,
        headers={"X-CSRF-Token": session["csrfToken"]},
    )
    assert created.status_code == 201


def test_administrator_can_manage_users_and_actions_use_the_verified_actor(
    client, db_session, auth_enabled
):
    admin = create_local_user(
        db_session,
        username="admin",
        password="correct-horse-battery-staple",
        role="admin",
    )
    session = _login(client, admin.username, "correct-horse-battery-staple")
    headers = {
        "X-CSRF-Token": session["csrfToken"],
        "X-K8Boss-User": "spoofed-actor",
    }
    created = client.post(
        "/api/auth/users",
        json={
            "username": "viewer",
            "password": "viewer-password-long",
            "display_name": "Cluster Viewer",
            "email": "viewer@example.test",
            "role": "user",
        },
        headers=headers,
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    assert "password" not in created.text

    updated = client.put(
        f"/api/auth/users/{user_id}",
        json={"display_name": "Read Only", "role": "admin"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "admin"
    assert client.get("/api/auth/users").json()["items"][1]["display_name"] == "Read Only"

    deleted = client.delete(f"/api/auth/users/{user_id}", headers=headers)
    assert deleted.status_code == 204
    db_session.expire_all()
    assert db_session.get(User, user_id).active is False
    audit = db_session.scalars(select(AuditRecord).order_by(AuditRecord.id)).all()
    # The sign-in itself is the first record, then the three user changes. Every
    # one is attributed to the verified session identity — never to the
    # `X-K8Boss-User` header the request also carried, which is advisory in
    # legacy proxy mode and must be ignored outright once a session exists.
    assert [row.verb for row in audit] == ["login", "create", "patch", "delete"]
    assert [row.actor for row in audit] == ["admin"] * 4
    assert all(row.cluster_id is None for row in audit)
    assert all(row.category == "console" for row in audit)


def test_non_admin_cannot_list_or_manage_users(client, db_session, auth_enabled):
    create_local_user(
        db_session,
        username="member",
        password="correct-horse-battery-staple",
        role="user",
    )
    _login(client, "member", "correct-horse-battery-staple")
    response = client.get("/api/auth/users")
    assert response.status_code == 403
    assert response.json()["error"] == "permission_denied"


def test_last_administrator_and_current_account_cannot_be_deactivated(
    client, db_session, auth_enabled
):
    admin = create_local_user(
        db_session,
        username="admin",
        password="correct-horse-battery-staple",
        role="admin",
    )
    session = _login(client, "admin", "correct-horse-battery-staple")
    response = client.delete(
        f"/api/auth/users/{admin.id}",
        headers={"X-CSRF-Token": session["csrfToken"]},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid"


def test_logout_revokes_the_server_side_session(client, db_session, auth_enabled):
    create_local_user(
        db_session,
        username="member",
        password="correct-horse-battery-staple",
    )
    session = _login(client, "member", "correct-horse-battery-staple")
    response = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": session["csrfToken"]}
    )
    assert response.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_successful_ldap_bind_synchronizes_profile_and_admin_group(
    client, db_session, auth_enabled, monkeypatch
):
    monkeypatch.setattr(settings, "ldap_enabled", True)

    def fake_authenticate(username, password):
        assert (username, password) == ("directory.admin", "directory-password")
        return LDAPProfile(
            username="directory.admin",
            display_name="Directory Admin",
            email="directory.admin@example.test",
            groups=("CN=Platform Admins,OU=Groups,DC=example,DC=test",),
        )

    # Configured with different casing from what the directory returned. Group
    # DNs are case-insensitive by specification and real servers echo whatever
    # casing the attribute was written with, so a verbatim comparison produces a
    # configuration that looks right and grants nobody anything.
    monkeypatch.setattr(
        settings, "ldap_admin_group_dn", "cn=platform admins,ou=groups,dc=example,dc=test"
    )
    monkeypatch.setattr("app.identity.ldap.authenticate", fake_authenticate)
    body = _login(client, "directory.admin", "directory-password", source="ldap")
    assert body["user"] == {
        "id": body["user"]["id"],
        "username": "directory.admin",
        "display_name": "Directory Admin",
        "email": "directory.admin@example.test",
        "role": "admin",
        "auth_source": "ldap",
        # ADR-0007: an LDAP session carries no issuer-stated identity and can
        # never impersonate. Asserted here rather than only in the impersonation
        # tests because this is the exact-shape assertion a future field would
        # have to be added to deliberately.
        "idp_username": None,
        "can_impersonate": False,
    }
    db_session.expire_all()
    row = db_session.scalar(select(User).where(User.username == "directory.admin"))
    assert row.password_hash is None
    assert row.auth_source == "ldap"


def test_ldap_refuses_to_send_credentials_without_tls(client, auth_enabled, monkeypatch):
    monkeypatch.setattr(settings, "ldap_enabled", True)
    monkeypatch.setattr(settings, "ldap_url", "ldap://directory.example.test:389")
    monkeypatch.setattr(settings, "ldap_start_tls", False)
    monkeypatch.setattr(settings, "ldap_user_search_base", "ou=people,dc=example,dc=test")

    response = client.post(
        "/api/auth/login",
        json={"username": "directory.user", "password": "directory-password", "source": "ldap"},
    )
    assert response.status_code == 502
    assert response.json()["error"] == "identity_provider_unavailable"
    assert "clear text" in response.json()["message"]