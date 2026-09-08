"""
OpenShift built-in OAuth single sign-on (§12.4).

The provider that lets an operator sign in with the identity they already use
for ``oc login``. What these tests are protecting is the argument that makes an
*unsigned, opaque* access token good enough here: it is the cluster, not this
console, that resolves the token to a user, and the console reads the answer
from the cluster's own ``User`` object over a channel it verified.

So the interesting cases are the ones where that chain is broken — the API
server is unreachable, ``users/~`` refuses the token, the object names nobody —
and the one where it holds but the *console* would get the identity wrong: a
recycled username on a cluster that re-issued it, which the ``metadata.uid``
binding is there to refuse.

``httpx`` is intercepted with a ``MockTransport``, so discovery, the token
exchange and the ``users/~`` read all run their real code paths.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.audit import recorder
from app.config import settings
from app.identity import handshake as handshake_service
from app.identity import openshift
from app.models import User

API = "https://api.cluster.example.test:6443"
ISSUER = "https://oauth-openshift.apps.cluster.example.test"
CALLBACK = "https://console.example.test/api/auth/openshift/callback"


@pytest.fixture
def cluster(monkeypatch):
    """Point the console at a fake OpenShift cluster and serve its endpoints."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "openshift_enabled", True)
    monkeypatch.setattr(settings, "openshift_api_url", API)
    monkeypatch.setattr(settings, "openshift_client_id", "k8boss-admin")
    monkeypatch.setattr(settings, "openshift_redirect_url", CALLBACK)
    monkeypatch.setattr(settings, "openshift_admin_group", "")
    monkeypatch.setattr(settings, "openshift_allowed_groups", "")
    monkeypatch.setattr(settings, "openshift_scopes", "user:info")
    openshift.reset_discovery_cache()

    metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "scopes_supported": ["user:full", "user:info", "user:check-access"],
        "code_challenge_methods_supported": ["plain", "S256"],
    }
    state: dict = {
        "metadata": metadata,
        "discovery_status": 200,
        "token_status": 200,
        "token_body": {"access_token": "sha256~an-opaque-token", "token_type": "Bearer"},
        "user_status": 200,
        "user": {
            "metadata": {"name": "erens", "uid": "9f21c0de-0000-4000-8000-000000000001"},
            "fullName": "Eren S",
            "groups": ["system:authenticated", "system:authenticated:oauth", "platform-admins"],
        },
        "authorizations": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == f"{API}{openshift.DISCOVERY_PATH}":
            if state["discovery_status"] != 200:
                return httpx.Response(state["discovery_status"], text="nope")
            return httpx.Response(200, json=state["metadata"])
        if url == metadata["token_endpoint"]:
            return httpx.Response(state["token_status"], json=state["token_body"])
        if url == f"{API}{openshift.USER_PATH}":
            state["authorizations"].append(request.headers.get("authorization"))
            return httpx.Response(state["user_status"], json=state["user"])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    yield state
    openshift.reset_discovery_cache()


@pytest.fixture
def client(db_engine):
    from app.main import app
    from app.database import get_db
    from app.k8s.client import manager
    from tests.conftest import _override_get_db

    app.dependency_overrides[get_db] = _override_get_db
    manager.reset()
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


def start(client) -> str:
    response = client.get("/api/auth/openshift/start")
    assert response.status_code == 302
    sealed = response.cookies[handshake_service.cookie_name("openshift")]
    client.cookies.set(handshake_service.cookie_name("openshift"), sealed)
    return handshake_service.unseal(sealed, provider="openshift").state


def callback(client, state: str) -> httpx.Response:
    return client.get(
        "/api/auth/openshift/callback", params={"code": "an-auth-code", "state": state}
    )


# --------------------------------------------------------------------------- #
# Discovery: one URL, and the endpoints derived from it
# --------------------------------------------------------------------------- #

def test_the_authorization_endpoint_comes_from_the_api_server(client, cluster):
    """One setting yields three endpoints. Configuring them by hand is three
    chances to point half a flow at a different cluster."""
    response = client.get("/api/auth/openshift/start")
    assert response.headers["location"].startswith(f"{ISSUER}/oauth/authorize?")


def test_openshift_scopes_are_sent_and_openid_ones_are_not(client, cluster):
    """`openid profile email` is an OIDC vocabulary and OpenShift rejects it as
    unknown scopes — which is one of the three reasons this is not the OIDC
    provider."""
    query = parse_qs(urlparse(client.get("/api/auth/openshift/start").headers["location"]).query)
    assert query["scope"] == ["user:info"]
    assert query["code_challenge_method"] == ["S256"]
    assert "nonce" not in query


def test_an_unreachable_api_server_is_a_provider_failure_not_a_refusal(
    client, cluster
):
    """`denied` is what a credential rejection looks like in the trail. An
    unreachable cluster recorded as one sends the operator to check an
    OAuthClient that is fine."""
    cluster["discovery_status"] = 503
    response = client.get("/api/auth/openshift/start")

    assert response.status_code == 302
    assert "provider_unreachable" in response.headers["location"]
    logins = [
        r for r in recorder.query(category="console")["items"] if r["verb"] == "login"
    ]
    assert logins and logins[0]["outcome"] == "failed"


def test_incomplete_metadata_is_reported_rather_than_cached(client, cluster):
    """An incomplete document used anyway fails later, in the middle of the
    handshake, where the error reads as 'your sign-in is bad'."""
    cluster["metadata"] = {"issuer": ISSUER}
    assert "provider_unreachable" in client.get("/api/auth/openshift/start").headers["location"]


def test_the_connection_check_warns_about_what_will_bite_at_sign_in(client, cluster):
    """Reaching the metadata proves the API server is there and not that a
    sign-in will work. Both remaining failure modes surface at the user's first
    attempt, where nobody can act on them."""
    cluster["metadata"] = {**cluster["metadata"], "code_challenge_methods_supported": ["plain"]}
    result = openshift.test_connection()
    assert result["ok"] is True
    assert any("PKCE" in warning for warning in result["warnings"])


def test_a_parameterised_role_scope_is_not_reported_as_unknown(client, cluster, monkeypatch):
    """`role:<role>:<namespace>` scopes are never enumerated in
    scopes_supported, so checking them there produces a false warning on a
    correct configuration."""
    monkeypatch.setattr(settings, "openshift_scopes", "user:info role:admin:payments")
    assert openshift.test_connection()["warnings"] == []


# --------------------------------------------------------------------------- #
# Identity comes from the cluster
# --------------------------------------------------------------------------- #

def test_a_complete_sign_in_provisions_from_the_cluster_s_user_object(
    client, cluster, db_session
):
    state = start(client)
    response = callback(client, state)

    assert response.status_code == 302
    assert settings.auth_cookie_name in response.cookies
    row = db_session.query(User).filter(User.username == "erens").one()
    assert row.auth_source == "openshift"
    assert row.display_name == "Eren S"
    # metadata.uid, not the name: a username can be re-issued once the User
    # object is deleted, and the rebind guard keys on the uid.
    assert row.external_id == "9f21c0de-0000-4000-8000-000000000001"
    # OpenShift's User object carries no email. Synthesising one would be a
    # fabricated attribute that every later reader takes for a verified one.
    assert row.email is None


def test_the_token_is_spent_once_on_users_tilde(client, cluster):
    """The whole trust argument: it is the cluster that resolves this token to a
    person, and the console keeps nothing afterwards."""
    callback(client, start(client))
    assert cluster["authorizations"] == ["Bearer sha256~an-opaque-token"]


def test_a_refused_users_tilde_read_names_the_scope(client, cluster):
    """The token authenticated and cannot read its own user, which is almost
    always a scope problem."""
    cluster["user_status"] = 403
    assert "assertion_rejected" in callback(client, start(client)).headers["location"]


def test_a_user_object_with_no_name_is_refused(client, cluster):
    cluster["user"] = {"metadata": {"uid": "abc"}}
    assert "assertion_rejected" in callback(client, start(client)).headers["location"]


def test_a_recycled_username_with_a_new_uid_is_refused(client, cluster, db_session):
    """Two people, one name, at different times. Merging would hand the second
    one the first one's console role."""
    callback(client, start(client))
    cluster["user"] = {
        **cluster["user"],
        "metadata": {"name": "erens", "uid": "a-completely-different-uid"},
    }
    client.cookies.clear()
    response = callback(client, start(client))

    assert "account_refused" in response.headers["location"]
    db_session.expire_all()
    row = db_session.query(User).filter(User.username == "erens").one()
    assert row.external_id == "9f21c0de-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
# Groups, including OpenShift's virtual ones
# --------------------------------------------------------------------------- #

def test_a_cluster_group_maps_to_the_admin_role(client, cluster, monkeypatch, db_session):
    monkeypatch.setattr(settings, "openshift_admin_group", "platform-admins")
    callback(client, start(client))
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_the_virtual_groups_are_passed_through_unfiltered(client, cluster, monkeypatch, db_session):
    """`system:cluster-admins` is a real cluster group worth mapping, and the
    virtual ones cannot be stripped without taking it with them. The cost — that
    listing `system:authenticated` in the allowlist admits everybody — is
    documented rather than silently prevented."""
    monkeypatch.setattr(settings, "openshift_admin_group", "system:authenticated:oauth")
    callback(client, start(client))
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_an_absent_group_list_does_not_demote_an_administrator(
    client, cluster, monkeypatch, db_session
):
    monkeypatch.setattr(settings, "openshift_admin_group", "platform-admins")
    callback(client, start(client))
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"

    cluster["user"] = {k: v for k, v in cluster["user"].items() if k != "groups"}
    client.cookies.clear()
    callback(client, start(client))
    db_session.expire_all()
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_the_allowlist_fails_closed_when_the_cluster_reports_no_groups(
    client, cluster, monkeypatch
):
    monkeypatch.setattr(settings, "openshift_allowed_groups", "platform-admins")
    cluster["user"] = {k: v for k, v in cluster["user"].items() if k != "groups"}
    assert "assertion_rejected" in callback(client, start(client)).headers["location"]


# --------------------------------------------------------------------------- #
# ADR-0007
# --------------------------------------------------------------------------- #

def test_an_openshift_session_may_act_as_the_cluster_identity(client, cluster, db_session):
    """The strongest case in the tree for impersonation: the username and groups
    are the cluster's own record of an identity, read from the cluster, in
    response to a token the cluster minted. There is no second system whose
    opinion has to line up with the API server's."""
    from app.identity.service import load_session

    response = callback(client, start(client))
    principal = load_session(response.cookies[settings.auth_cookie_name]).principal

    assert principal.auth_source == "openshift"
    assert principal.can_impersonate is True
    assert principal.idp_username == "erens"
    assert "platform-admins" in principal.idp_groups
