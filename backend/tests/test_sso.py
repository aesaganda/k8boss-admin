"""
What the four single sign-on providers share: the registry, the routes, the gate.

:mod:`tests.test_oidc`, :mod:`tests.test_oauth`, :mod:`tests.test_openshift` and
:mod:`tests.test_saml` each check one provider's own argument for why an
assertion is trustworthy. This file checks the part that is written once for all
of them — and therefore the part where a fifth provider could arrive reachable in
the router and missing from the middleware's public list, or offered on a
deployment that has no sessions to issue.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.identity import sso
from app.middleware import auth as auth_middleware


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


@pytest.fixture
def every_provider(monkeypatch):
    """Configure all four well enough to be offered. None of them is contacted."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.test")
    monkeypatch.setattr(settings, "oidc_client_id", "console")
    monkeypatch.setattr(settings, "oauth_enabled", True)
    monkeypatch.setattr(settings, "oauth_authorization_url", "https://oauth.example.test/authorize")
    monkeypatch.setattr(settings, "oauth_token_url", "https://oauth.example.test/token")
    monkeypatch.setattr(settings, "oauth_userinfo_url", "https://oauth.example.test/user")
    monkeypatch.setattr(settings, "oauth_client_id", "console")
    monkeypatch.setattr(settings, "openshift_enabled", True)
    monkeypatch.setattr(settings, "openshift_api_url", "https://api.cluster.example.test:6443")
    monkeypatch.setattr(settings, "openshift_client_id", "console")
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "saml_idp_sso_url", "https://idp.example.test/sso")
    monkeypatch.setattr(settings, "saml_idp_certificate", "not-a-real-certificate")


def test_every_provider_is_offered_with_its_own_start_path(client, every_provider):
    body = client.get("/api/auth/config").json()
    entries = {entry["name"]: entry for entry in body["ssoProviders"]}

    assert set(entries) == {"oidc", "oauth", "openshift", "saml"}
    for name, entry in entries.items():
        assert entry["startPath"] == f"/api/auth/{name}/start"
        assert entry["label"]
    assert body["methods"] == ["local", "oidc", "oauth", "openshift", "saml"]


def test_the_oidc_shorthand_still_answers_for_an_older_frontend(client, every_provider):
    """A browser holding an older build of the SPA reads `oidcEnabled` and
    `oidc`. Dropping them would take that deployment's sign-in button away at the
    moment the backend was upgraded — a console nobody can log in to, produced by
    a release that changed no behaviour."""
    body = client.get("/api/auth/config").json()
    assert body["oidcEnabled"] is True
    assert body["oidc"]["startPath"] == "/api/auth/oidc/start"


def test_nothing_is_offered_when_the_console_is_not_authenticating(client, every_provider, monkeypatch):
    """Single sign-on issues a console session, and a console that is not
    authenticating requests has no session to issue. Offering the button anyway
    sends an operator through a full handshake to arrive back at a console that
    never asked who they were."""
    monkeypatch.setattr(settings, "auth_enabled", False)
    body = client.get("/api/auth/config").json()
    assert body["ssoProviders"] == []
    assert body["oidc"] is None


def test_the_config_endpoint_never_says_how_a_provider_is_wired(client, every_provider):
    """The one unauthenticated endpoint in the API. The login page needs to know
    which buttons to draw and nothing else; an issuer URL, a client id or an API
    server address here lets anyone who can reach the console enumerate its
    identity infrastructure."""
    body = client.get("/api/auth/config").text
    for secret in ("idp.example.test", "oauth.example.test", "api.cluster.example.test"):
        assert secret not in body


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/nope/start",
        "/api/auth/nope/callback",
        # SAML's assertion arrives on a POST and its path segment is `acs`, so
        # this is not one of its routes either.
        "/api/auth/saml/callback",
    ],
)
def test_a_path_that_is_not_a_provider_route_never_produces_a_redirect(
    client, every_provider, path
):
    """401 from the gate, because the exemption list is built from the registry:
    a name that is not a provider's route is not on it, and the router is never
    reached. Whatever the status, the property that matters is the same one — no
    redirect. The failure redirect interpolates the provider name, so an
    unvalidated one would be reflected into a URL this console sends a browser
    to.
    """
    response = client.get(path)
    assert response.status_code == 401
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "path",
    ["/api/auth/nope/start", "/api/auth/nope/callback", "/api/auth/saml/callback"],
)
def test_the_router_itself_answers_404_for_a_path_that_is_not_a_provider_route(
    client, every_provider, monkeypatch, path
):
    """The router's own answer, with no gate in front of it.

    A legacy proxy-mode deployment has no session gate, so this is what an
    administrator who registered the wrong callback URL with their identity
    provider actually sees. It is a 404 and never a redirect: a provider segment
    is validated before anything is done with it.
    """
    monkeypatch.setattr(settings, "auth_enabled", False)
    response = client.get(path)
    assert response.status_code == 404
    assert "location" not in response.headers


def test_the_public_path_list_is_built_from_the_registry(every_provider):
    """A provider reachable in the router and missing here fails as 'sign in to
    continue' on the callback of a provider that has just authenticated
    somebody — which reads as a broken identity provider."""
    public = auth_middleware._public_paths()
    for name, module in sso.providers().items():
        assert f"/api/auth/{name}/start" in public
        assert f"/api/auth/{name}/{module.CALLBACK_SUFFIX}" in public
    assert "/api/auth/saml/metadata" in public
    # Still exact strings, not a prefix rule: "anything under /api/auth/" is one
    # route away from exempting something that should never have been.
    assert "/api/auth/users" not in public
    assert "/api/auth/me" not in public


def test_the_acs_is_the_only_unauthenticated_post(every_provider):
    """Everything else in the exemption list is a GET browser navigation, so the
    middleware's CSRF check does not apply to it either. The ACS is the one
    exception and it has its own answer — a signed assertion whose InResponseTo
    must match this browser's sealed handshake — rather than inheriting this one.
    """
    posts = {
        path for path in auth_middleware._public_paths()
        if path.endswith("/acs") or path.endswith("/login")
    }
    assert posts == {"/api/auth/saml/acs", "/api/auth/login"}


def test_a_signed_in_session_is_still_required_everywhere_else(client, every_provider):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/users").status_code == 401
    assert client.get("/api/clusters").status_code == 401


def test_every_registered_provider_implements_the_whole_interface():
    """The registry is what keeps the route generic, so a provider that is
    missing a name the route calls would fail at a sign-in rather than at
    import."""
    required = (
        "NAME", "BINDING", "CALLBACK_SUFFIX", "enabled", "require_enabled",
        "label", "admin_group", "configured_callback_url", "begin", "complete",
    )
    for name, module in sso.providers().items():
        for attribute in required:
            assert hasattr(module, attribute), f"{name} is missing {attribute}"
        assert module.NAME == name
        assert module.BINDING in {"query", "form"}


def test_every_federated_source_is_a_registered_provider_or_ldap():
    """The two lists are separate and both decide whether a console
    administrator may edit an account's role. A source in one and not the other
    lets an administrator promote an account and watch the change silently revert
    at that user's next sign-in, which is the bug this pairing already produced
    once for OIDC.
    """
    from app.identity.service import AUTH_SOURCES, FEDERATED_SOURCES

    assert FEDERATED_SOURCES == AUTH_SOURCES - {"local"}
    assert set(sso.providers()) | {"ldap"} == FEDERATED_SOURCES


def test_only_the_two_cluster_native_sources_may_impersonate():
    """ADR-0007's line, asserted rather than left to a reading of `decide`.

    Widening this set is an amendment to that ADR, not a config change, and this
    test is what makes adding a provider to it a deliberate act.
    """
    from app.k8s.impersonation import IMPERSONATION_SOURCES

    assert IMPERSONATION_SOURCES == frozenset({"oidc", "openshift"})
    assert IMPERSONATION_SOURCES < sso.providers().keys() | {"ldap", "local"}
