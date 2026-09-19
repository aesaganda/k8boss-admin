"""
Who may register, re-point and de-register a cluster (§3, §12).

Cluster CRUD used to be ungated: any signed-in account, down to the lowest
"user" role, could call all three. Two payloads walk straight through such a
hole, and both tests below fire the real one rather than asserting on a status
code alone:

* clearing ``impersonation_enabled`` makes every later call to that cluster run
  as this console's ServiceAccount instead of as the signed-in operator, so the
  caller leaves their own RBAC behind and inherits the console's;
* a ``PUT`` that moves ``api_server`` while **omitting** ``token`` keeps the
  stored credential — that is what the partial update promises — and points it at
  a host the caller chose. The next request hands them the cluster's bearer
  token.

Every unsafe request here carries the session CSRF token on purpose. Without it
the request is refused with the same ``permission_denied`` the role gate
produces, and a test that omitted it would pass with the gate removed.
"""

from __future__ import annotations

import pytest

from app import database
from app.config import settings
from app.crypto import decrypt
from app.identity.service import create_local_user
from app.models import Cluster
from tests.conftest import FIXTURE_TOKEN

CREATE_BODY = {
    "name": "staging-us",
    "platform": "kubernetes",
    "api_server": "https://api.staging-us.example:6443",
    "authentication_type": "service_account_token",
    "token": "another-fixture-token",
}


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "ldap_enabled", False)
    return settings


def _sign_in(client, db_session, *, role: str) -> dict:
    create_local_user(
        db_session,
        username=f"{role}-account",
        password="correct-horse-battery-staple",
        role=role,
    )
    response = client.post(
        "/api/auth/login",
        json={"username": f"{role}-account", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrfToken"]}


def _row(cluster_id: int) -> Cluster:
    """The stored cluster, read outside the request session."""
    session = database.SessionLocal()
    try:
        row = session.get(Cluster, cluster_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def test_a_signed_in_non_admin_cannot_register_a_cluster(
    client, db_session, auth_enabled
):
    headers = _sign_in(client, db_session, role="user")

    response = client.post("/api/clusters", json=CREATE_BODY, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"] == "permission_denied"
    assert client.get("/api/clusters").json()["items"] == []


def test_a_signed_in_non_admin_cannot_repoint_a_cluster_at_a_host_they_control(
    client, db_session, auth_enabled, registered_cluster
):
    """The credential-exfiltration payload, refused.

    Omitting ``token`` preserves the stored one, so a successful PUT here would
    leave this console holding a real bearer token for a cluster and sending it
    to ``api.attacker.example`` on the next request.
    """
    headers = _sign_in(client, db_session, role="user")

    response = client.put(
        f"/api/clusters/{registered_cluster.id}",
        json={"api_server": "https://api.attacker.example:6443"},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["error"] == "permission_denied"
    row = _row(registered_cluster.id)
    assert row.api_server == "https://api.prod-eu.example:6443"
    assert decrypt(row.token_encrypted) == FIXTURE_TOKEN


def test_a_signed_in_non_admin_cannot_turn_impersonation_off(
    client, db_session, auth_enabled, registered_cluster, monkeypatch
):
    """ADR-0007's opt-in is an administrator's setting in both directions.

    Turning it *off* is the interesting direction: it does not fail, it silently
    promotes every later call that operator makes on this cluster from their own
    identity to the console's ServiceAccount.
    """
    monkeypatch.setattr(settings, "oidc_enabled", True)
    session = database.SessionLocal()
    try:
        session.get(Cluster, registered_cluster.id).impersonation_enabled = True
        session.commit()
    finally:
        session.close()

    headers = _sign_in(client, db_session, role="user")
    response = client.put(
        f"/api/clusters/{registered_cluster.id}",
        json={"impersonation_enabled": False},
        headers=headers,
    )

    assert response.status_code == 403
    assert _row(registered_cluster.id).impersonation_enabled is True


def test_a_signed_in_non_admin_cannot_de_register_a_cluster(
    client, db_session, auth_enabled, registered_cluster
):
    headers = _sign_in(client, db_session, role="user")

    response = client.delete(f"/api/clusters/{registered_cluster.id}", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"] == "permission_denied"
    assert _row(registered_cluster.id) is not None


def test_a_signed_in_non_admin_cannot_discover_or_import_a_cluster(
    client, db_session, auth_enabled
):
    """§34's two endpoints are gated with §3's three, and for the same reason.

    Discovery reports on a file on the console's own filesystem — its path, the
    contexts in it, the addresses they point at — and importing decides which
    API server this console's transport talks to. That the credential came off
    the disk rather than out of a form does not make it a smaller decision.
    """
    headers = _sign_in(client, db_session, role="user")

    listed = client.get("/api/clusters/discovery", headers=headers)
    assert listed.status_code == 403
    assert listed.json()["error"] == "permission_denied"

    imported = client.post(
        "/api/clusters/import", json={"context": "kind-dev"}, headers=headers,
    )
    assert imported.status_code == 403
    assert imported.json()["error"] == "permission_denied"
    assert client.get("/api/clusters").json()["items"] == []


def test_an_administrator_can_register_repoint_and_de_register(
    client, db_session, auth_enabled
):
    """The gate refuses a role, not the feature."""
    headers = _sign_in(client, db_session, role="admin")

    created = client.post("/api/clusters", json=CREATE_BODY, headers=headers)
    assert created.status_code == 201
    cluster_id = created.json()["id"]

    updated = client.put(
        f"/api/clusters/{cluster_id}",
        json={"api_server": "https://api.staging-us-2.example:6443"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["api_server"] == "https://api.staging-us-2.example:6443"

    assert client.delete(f"/api/clusters/{cluster_id}", headers=headers).status_code == 204


def test_legacy_proxy_mode_is_unaffected_by_the_gate(client, registered_cluster):
    """With AUTH_ENABLED false there is no console role to check.

    ``require_console_admin`` returns None rather than demanding a session, so
    the endpoints stay exactly as open as every other endpoint in that mode —
    the authenticating proxy in front is what decides, which is the premise of
    the mode. A gate that 401'd here would make cluster registration permanently
    unreachable on the default deployment.
    """
    assert settings.auth_enabled is False

    assert client.post("/api/clusters", json=CREATE_BODY).status_code == 201
    assert client.put(
        f"/api/clusters/{registered_cluster.id}", json={"name": "prod-eu-renamed"}
    ).status_code == 200
    assert client.delete(f"/api/clusters/{registered_cluster.id}").status_code == 204
