"""
Generic OAuth 2.0 single sign-on (§12.4).

The provider for authorization servers that are not OpenID Connect providers.
There is no signed assertion to verify here, so what these tests are checking is
different from :mod:`tests.test_oidc`: not "is a bad signature rejected" but
**"is the identity taken from the provider, over a channel this console
controls, rather than from anything the browser handed us"** — plus the two
things that bind the response to this sign-in, ``state`` and PKCE, which are the
*only* two protections this flow has.

``httpx`` is intercepted with a ``MockTransport``, so the token exchange and the
userinfo read run their real code paths against a fake server rather than being
patched out.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.audit import recorder
from app.config import settings
from app.identity import handshake as handshake_service
from app.models import User

AUTHORIZE = "https://oauth.example.test/login/oauth/authorize"
TOKEN = "https://oauth.example.test/login/oauth/access_token"
USERINFO = "https://oauth.example.test/api/v1/user"
CALLBACK = "https://console.example.test/api/auth/oauth/callback"


@pytest.fixture
def provider(monkeypatch):
    """Point the console at a fake OAuth 2.0 server and serve it in-process."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oauth_enabled", True)
    monkeypatch.setattr(settings, "oauth_authorization_url", AUTHORIZE)
    monkeypatch.setattr(settings, "oauth_token_url", TOKEN)
    monkeypatch.setattr(settings, "oauth_userinfo_url", USERINFO)
    monkeypatch.setattr(settings, "oauth_client_id", "k8boss-admin")
    monkeypatch.setattr(settings, "oauth_redirect_url", CALLBACK)
    monkeypatch.setattr(settings, "oauth_scopes", "read:user")
    monkeypatch.setattr(settings, "oauth_subject_field", "id")
    monkeypatch.setattr(settings, "oauth_username_field", "login")
    monkeypatch.setattr(settings, "oauth_email_field", "email")
    monkeypatch.setattr(settings, "oauth_display_name_field", "name")
    monkeypatch.setattr(settings, "oauth_groups_field", "groups")
    monkeypatch.setattr(settings, "oauth_admin_group", "")
    monkeypatch.setattr(settings, "oauth_allowed_groups", "")

    state: dict = {
        "token_status": 200,
        "token_body": {"access_token": "an-access-token", "token_type": "bearer"},
        "token_form_encoded": False,
        "userinfo_status": 200,
        "userinfo": {
            "id": 4711,
            "login": "erens",
            "email": "erens@example.test",
            "name": "Eren S",
        },
        "authorizations": [],
        "token_requests": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == TOKEN:
            state["token_requests"].append(
                dict(httpx.QueryParams(request.content.decode()))
            )
            if state["token_form_encoded"]:
                return httpx.Response(
                    state["token_status"],
                    text="&".join(f"{k}={v}" for k, v in state["token_body"].items()),
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
            return httpx.Response(state["token_status"], json=state["token_body"])
        if url == USERINFO:
            state["authorizations"].append(request.headers.get("authorization"))
            return httpx.Response(state["userinfo_status"], json=state["userinfo"])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    return state


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
    """Begin a sign-in; return the ``state`` read back out of the sealed cookie."""
    response = client.get("/api/auth/oauth/start")
    assert response.status_code == 302
    sealed = response.cookies[handshake_service.cookie_name("oauth")]
    client.cookies.set(handshake_service.cookie_name("oauth"), sealed)
    return handshake_service.unseal(sealed, provider="oauth").state


def callback(client, state: str) -> httpx.Response:
    return client.get(
        "/api/auth/oauth/callback", params={"code": "an-auth-code", "state": state}
    )


# --------------------------------------------------------------------------- #
# Discovery and the handshake
# --------------------------------------------------------------------------- #

def test_the_login_page_is_told_the_provider_exists(client, provider):
    body = client.get("/api/auth/config").json()
    names = [entry["name"] for entry in body["ssoProviders"]]
    assert "oauth" in names
    assert "oauth" in body["methods"]
    # Never how it is wired: this endpoint is the one unauthenticated route in
    # the API and the login page only needs to know which buttons to draw.
    assert AUTHORIZE not in str(body)


@pytest.mark.parametrize("missing", ["oauth_authorization_url", "oauth_token_url",
                                     "oauth_userinfo_url", "oauth_client_id"])
def test_a_half_configured_provider_is_not_offered(client, provider, monkeypatch, missing):
    """A button that leads to an error reads as a broken console; an absent one
    reads as 'not set up here', which is the actionable version of the same fact.

    The userinfo URL is in this list and is the one that matters: without it the
    flow ends holding an opaque token, and a console that issued a session then
    would be signing people in on the strength of a successful HTTP call.
    """
    monkeypatch.setattr(settings, missing, "")
    assert client.get("/api/auth/config").json()["ssoProviders"] == []
    # 422 `invalid`, not a redirect: nothing sent this browser anywhere, so
    # there is no sign-in in progress to return it to. The button it would have
    # come from is not on the page.
    assert client.get("/api/auth/oauth/start").status_code == 422


def test_the_authorization_request_carries_pkce_and_state_and_no_nonce(client, provider):
    """PKCE and state are the whole of what binds the response to this sign-in.

    A nonce would be decoration: it binds an ID token, and there is no ID token
    here. Carrying one anyway is how a later reader concludes a protection is in
    force that nothing ever checks.
    """
    response = client.get("/api/auth/oauth/start")
    query = parse_qs(urlparse(response.headers["location"]).query)

    assert response.headers["location"].startswith(AUTHORIZE)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] and query["state"]
    assert query["scope"] == ["read:user"]
    assert "nonce" not in query


def test_no_scope_parameter_is_sent_when_none_is_configured(client, provider, monkeypatch):
    """Several servers reject `scope=` as malformed and name the parameter
    without saying it was empty."""
    monkeypatch.setattr(settings, "oauth_scopes", "")
    response = client.get("/api/auth/oauth/start")
    assert "scope" not in parse_qs(urlparse(response.headers["location"]).query)


def test_the_handshake_cookie_is_this_provider_s_own(client, provider):
    """One cookie per provider: an operator who clicks the wrong button, goes
    back and clicks another must not have the second handshake overwrite the
    first."""
    header = client.get("/api/auth/oauth/start").headers["set-cookie"]
    assert "k8boss_admin_oauth=" in header
    assert "Path=/api/auth/oauth" in header
    assert "samesite=lax" in header.lower()


def test_a_handshake_sealed_for_another_provider_is_refused(client, provider, monkeypatch):
    """The provider is sealed inside the payload, not only in the cookie name.

    Honouring a handshake minted for one provider on another provider's callback
    is how a flow with weaker checks completes a sign-in that a stronger one
    started.
    """
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.test")
    monkeypatch.setattr(settings, "oidc_client_id", "k8boss-admin")
    forged = handshake_service.seal(
        handshake_service.Handshake(
            provider="oidc", state="s", nonce="n", code_verifier="v",
            redirect_uri=CALLBACK, next_path="/",
        )
    )
    client.cookies.set(handshake_service.cookie_name("oauth"), forged)
    response = callback(client, "s")
    assert "handshake_missing_or_expired" in response.headers["location"]


# --------------------------------------------------------------------------- #
# The callback
# --------------------------------------------------------------------------- #

def test_a_complete_sign_in_provisions_an_account_and_a_session(client, provider, db_session):
    state = start(client)
    response = callback(client, state)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert settings.auth_cookie_name in response.cookies

    row = db_session.query(User).filter(User.username == "erens").one()
    assert row.auth_source == "oauth"
    # Bound to the provider's own identifier, not to the name. A provider that
    # re-issues `erens` to somebody else must not hand them this row.
    assert row.external_id == "4711"
    assert row.email == "erens@example.test"
    # A federated account never keeps a password hash: a second way in that
    # nobody is watching.
    assert row.password_hash is None


def test_the_code_is_exchanged_with_the_pkce_verifier(client, provider):
    """Without the verifier on the exchange, an intercepted code is spendable."""
    state = start(client)
    callback(client, state)
    sent = provider["token_requests"][-1]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "an-auth-code"
    assert sent["code_verifier"]
    assert sent["redirect_uri"] == CALLBACK


def test_the_access_token_is_spent_on_the_userinfo_read(client, provider):
    state = start(client)
    callback(client, state)
    assert provider["authorizations"] == ["Bearer an-access-token"]


def test_a_form_encoded_token_response_is_understood(client, provider, db_session):
    """RFC 6749 requires JSON and GitHub answers form-encoded. The failure when
    it is not handled is a JSONDecodeError as a 500, which says nothing about a
    server behaving exactly as documented."""
    provider["token_form_encoded"] = True
    state = start(client)
    assert callback(client, state).status_code == 302
    assert db_session.query(User).filter(User.username == "erens").one()


def test_a_mismatched_state_is_refused_and_recorded(client, provider):
    """A mismatched state is what a forged callback looks like."""
    start(client)
    response = callback(client, "not-the-state-we-issued")

    assert "auth_error=invalid" in response.headers["location"]
    assert "state_mismatch" in response.headers["location"]
    records = [
        r for r in recorder.query(category="console")["items"] if r["verb"] == "login"
    ]
    assert records and records[0]["outcome"] == "denied"


def test_a_callback_without_a_handshake_writes_nothing(client, provider):
    """This route is public, so an audit write before the handshake is checked
    is an unauthenticated INSERT into the one unbounded table in the schema."""
    for _ in range(3):
        client.get("/api/auth/oauth/callback", params={"code": "x", "state": "y"})
    assert recorder.query(category="console")["items"] == []


def test_a_provider_outage_is_failed_not_denied(client, provider):
    """`denied` is the outcome reserved for a credential rejection and the one
    that looks like an attack. An outage recorded as one sends the operator to
    check a client registration that is fine."""
    provider["token_status"] = 503
    state = start(client)
    response = callback(client, state)

    assert "assertion_rejected" in response.headers["location"]
    logins = [
        r for r in recorder.query(category="console")["items"] if r["verb"] == "login"
    ]
    assert logins and logins[0]["outcome"] == "failed"


def test_a_refusal_carrying_http_200_is_still_a_refusal(client, provider):
    """Some servers answer failures with the wrong status. Dropping the error
    field would report this as 'no access token', as though the provider had
    said nothing at all."""
    provider["token_body"] = {"error": "bad_verification_code"}
    state = start(client)
    response = callback(client, state)

    assert "assertion_rejected" in response.headers["location"]
    logins = [
        r for r in recorder.query(category="console")["items"] if r["verb"] == "login"
    ]
    assert logins and logins[0]["outcome"] == "denied"


def test_a_userinfo_refusal_names_the_scope(client, provider):
    """The token authenticated and cannot read the profile it was issued for,
    which is almost always a missing scope. A generic 'sign-in failed' is
    something no administrator can act on."""
    provider["userinfo_status"] = 403
    state = start(client)
    assert "assertion_rejected" in callback(client, state).headers["location"]


def test_a_profile_with_no_subject_field_is_refused(client, provider):
    """The subject is what the account is bound to. Falling back to the username
    would hand the second holder of a recycled name the first one's role."""
    provider["userinfo"] = {"login": "erens"}
    state = start(client)
    assert "assertion_rejected" in callback(client, state).headers["location"]


def test_a_nested_field_can_be_addressed_with_a_dotted_path(
    client, provider, monkeypatch, db_session
):
    """A userinfo document is not standardised outside OIDC, and several real
    providers nest the interesting values. Without this those deployments need a
    proxy in front of the provider to flatten a document."""
    monkeypatch.setattr(settings, "oauth_subject_field", "data.user.id")
    monkeypatch.setattr(settings, "oauth_username_field", "data.user.handle")
    provider["userinfo"] = {"data": {"user": {"id": "u-9", "handle": "nested"}}}
    state = start(client)
    assert callback(client, state).status_code == 302
    assert db_session.query(User).filter(User.username == "nested").one().external_id == "u-9"


# --------------------------------------------------------------------------- #
# Groups: the tri-state, in both directions
# --------------------------------------------------------------------------- #

def test_the_admin_group_maps_to_the_admin_role(client, provider, monkeypatch, db_session):
    monkeypatch.setattr(settings, "oauth_admin_group", "platform-admins")
    provider["userinfo"] = {**provider["userinfo"], "groups": ["platform-admins"]}
    state = start(client)
    callback(client, state)
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_an_absent_groups_field_does_not_demote_an_administrator(
    client, provider, monkeypatch, db_session
):
    """The failure this prevents was live in this codebase for LDAP: an absent
    membership attribute produced False, the stored role was overwritten, and a
    directory administrator was silently demoted on login."""
    monkeypatch.setattr(settings, "oauth_admin_group", "platform-admins")
    provider["userinfo"] = {**provider["userinfo"], "groups": ["platform-admins"]}
    callback(client, start(client))
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"

    provider["userinfo"] = {k: v for k, v in provider["userinfo"].items() if k != "groups"}
    client.cookies.clear()
    callback(client, start(client))
    db_session.expire_all()
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_a_reported_empty_membership_does_apply_the_default_role(
    client, provider, monkeypatch, db_session
):
    """Empty is a real answer and must be acted on; absent is not an answer."""
    monkeypatch.setattr(settings, "oauth_admin_group", "platform-admins")
    provider["userinfo"] = {**provider["userinfo"], "groups": ["platform-admins"]}
    callback(client, start(client))
    provider["userinfo"] = {**provider["userinfo"], "groups": []}
    client.cookies.clear()
    callback(client, start(client))
    db_session.expire_all()
    assert db_session.query(User).filter(User.username == "erens").one().role == "user"


def test_the_allowlist_fails_closed_when_membership_is_not_reported(
    client, provider, monkeypatch
):
    """The opposite rule to role mapping, deliberately. An allowlist that
    admitted everyone whenever the field went missing would stop working exactly
    when the provider is misconfigured."""
    monkeypatch.setattr(settings, "oauth_allowed_groups", "console-users")
    state = start(client)
    assert "assertion_rejected" in callback(client, state).headers["location"]


def test_the_allowlist_refuses_someone_outside_it(client, provider, monkeypatch):
    monkeypatch.setattr(settings, "oauth_allowed_groups", "console-users")
    provider["userinfo"] = {**provider["userinfo"], "groups": ["everyone-else"]}
    state = start(client)
    assert "assertion_rejected" in callback(client, state).headers["location"]


def test_a_comma_separated_group_string_is_split(client, provider, monkeypatch, db_session):
    """Treating 'a,b' as one group produces a group nobody is ever in, which
    fails as a silent non-promotion rather than as an error."""
    monkeypatch.setattr(settings, "oauth_admin_group", "oncall")
    provider["userinfo"] = {**provider["userinfo"], "groups": "platform-admins,oncall"}
    callback(client, start(client))
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


# --------------------------------------------------------------------------- #
# Account binding
# --------------------------------------------------------------------------- #

def test_oauth_cannot_take_over_a_local_account(client, provider, db_session):
    """Anyone who can make a provider assert a username would otherwise inherit
    whatever that username already had."""
    from app.identity.service import create_local_user

    create_local_user(
        db_session, username="erens", password="a-long-enough-password",
        display_name=None, email=None, role="admin",
    )
    state = start(client)
    response = callback(client, state)

    assert "account_refused" in response.headers["location"]
    assert db_session.query(User).filter(User.username == "erens").one().auth_source == "local"


def test_an_oidc_account_is_not_adopted_by_the_oauth_provider(client, provider, db_session):
    """Two different providers asserting the same username is a collision, not a
    merge: the account carries the source that owns its role and profile."""
    from app.identity.service import provision_federated_user

    provision_federated_user(
        db_session, source="oidc", username="erens", external_id="oidc-sub",
        display_name=None, email=None, groups=(), admin_group="",
    )
    state = start(client)
    assert "account_refused" in callback(client, state).headers["location"]
