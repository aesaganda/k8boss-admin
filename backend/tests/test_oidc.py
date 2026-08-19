"""
OpenID Connect single sign-on (§12.4).

**These tests sign real tokens with a real key.** Mocking ``verify_id_token`` and
asserting that the callback provisions a user would test the plumbing and none of
the security, and every check in :mod:`app.identity.oidc` exists because skipping
it produces a login flow that works perfectly in a demo. So the fixtures stand up
an RSA keypair, publish a JWKS, and mint tokens the way an issuer would — which
means a token with the wrong audience, the wrong issuer, an expired ``exp`` or a
replayed ``nonce`` is genuinely rejected by the library rather than by an
assertion this file wrote.

The attacks each test stands for are named in its docstring. They are not
hypothetical: algorithm confusion, audience confusion and missing nonce binding
are the three ways OIDC integrations are routinely broken.
"""

from __future__ import annotations

import datetime
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.audit import recorder
from app.config import settings
from app.identity import handshake as handshake_service
from app.identity import oidc
from app.models import User

ISSUER = "https://idp.example.test/realms/console"
CLIENT_ID = "k8boss-admin"
KID = "test-signing-key"


# --------------------------------------------------------------------------- #
# A fake issuer that actually signs things
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(signing_key):
    """The public half, in the JWK form ``PyJWKClient`` consumes."""
    algorithm = jwt.algorithms.RSAAlgorithm
    document = json.loads(algorithm.to_jwk(signing_key.public_key()))
    document.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [document]}


@pytest.fixture
def issuer(monkeypatch, signing_key, jwks):
    """Point the console at the fake issuer and serve its endpoints in-process.

    ``httpx`` is intercepted with a ``MockTransport`` rather than by patching the
    functions under test, so discovery, JWKS retrieval and the token exchange all
    run their real code paths.
    """
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_redirect_url", "https://console.example.test/api/auth/oidc/callback")
    monkeypatch.setattr(settings, "oidc_admin_group", "")
    monkeypatch.setattr(settings, "oidc_allowed_groups", "")
    oidc.reset_discovery_cache()

    state: dict = {"token_response": None, "token_requests": []}

    document = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=document)
        if path == document["jwks_uri"]:
            return httpx.Response(200, json=jwks)
        if path == document["token_endpoint"]:
            state["token_requests"].append(dict(httpx.QueryParams(request.content.decode())))
            body = state["token_response"]
            if body is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    # PyJWKClient fetches the JWKS with urllib, not httpx, so it is pointed at the
    # same document directly. The signature verification it performs afterwards is
    # entirely real.
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "fetch_data",
        lambda self: jwks,
    )

    state["document"] = document
    yield state
    oidc.reset_discovery_cache()


def mint(signing_key, **overrides) -> str:
    """One ID token, signed for real. Overrides let a test break exactly one thing."""
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "9f21c0de-0000-4000-8000-000000000001",
        "exp": now + datetime.timedelta(minutes=5),
        "iat": now,
        "preferred_username": "erens",
        "email": "erens@example.test",
        "name": "Eren S",
    }
    claims.update(overrides)
    algorithm = overrides.pop("_alg", "RS256")
    return jwt.encode(claims, signing_key, algorithm=algorithm, headers={"kid": KID})


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


def start_handshake(client) -> tuple[str, str]:
    """Begin a sign-in; return ``(state, nonce)`` read back from the sealed cookie."""
    response = client.get("/api/auth/oidc/start")
    assert response.status_code == 302
    sealed = response.cookies[handshake_service.COOKIE_NAME]
    pending = handshake_service.unseal(sealed)
    client.cookies.set(handshake_service.COOKIE_NAME, sealed)
    return pending.state, pending.nonce


def callback(client, state: str) -> httpx.Response:
    return client.get("/api/auth/oidc/callback", params={"code": "an-auth-code", "state": state})


# --------------------------------------------------------------------------- #
# The handshake
# --------------------------------------------------------------------------- #

def test_start_redirects_to_the_issuer_with_pkce_and_a_nonce(client, issuer):
    response = client.get("/api/auth/oidc/start")

    assert response.status_code == 302
    location = httpx.URL(response.headers["location"])
    params = dict(location.params)
    assert str(location).startswith(issuer["document"]["authorization_endpoint"])
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    # S256, never `plain`: with plain the challenge *is* the verifier, so anyone
    # who saw the authorization request can complete the exchange — which is the
    # entire thing PKCE exists to stop.
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] and params["nonce"] and params["state"]


def test_the_handshake_cookie_is_lax_not_strict(client, issuer):
    """The one cookie attribute that cannot be copied from the session cookie.

    The callback is a top-level navigation *from the issuer's origin*. Browsers
    do not send a `SameSite=Strict` cookie on a cross-site navigation, so a Strict
    handshake cookie is simply absent when the callback runs and every single
    sign-in fails with "the sign-in did not match" — a total outage of SSO with a
    misleading message.
    """
    response = client.get("/api/auth/oidc/start")

    header = response.headers["set-cookie"]
    assert handshake_service.COOKIE_NAME in header
    assert "SameSite=lax" in header or "samesite=lax" in header.lower()
    assert "HttpOnly" in header


def test_the_oidc_routes_are_reachable_without_a_session(client, issuer):
    """SSO is how you *get* a session; challenging it for one is a deadlock.

    The auth middleware matches public paths as exact strings, so a route added
    without being listed there is challenged, and the symptom is a login button
    that answers 401.
    """
    assert client.get("/api/auth/oidc/start").status_code == 302
    # No handshake cookie: refused on its own terms (a redirect carrying the
    # failure), never with an authentication challenge.
    assert client.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"}).status_code == 302


def test_sso_is_refused_when_it_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", False)

    response = client.get("/api/auth/oidc/start")

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"


# --------------------------------------------------------------------------- #
# A clean sign-in
# --------------------------------------------------------------------------- #

def test_a_valid_assertion_provisions_an_account_and_a_session(
    client, issuer, signing_key, db_session
):
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}

    response = callback(client, state)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert settings.auth_cookie_name in response.cookies

    db_session.expire_all()
    from sqlalchemy import select

    user = db_session.scalar(select(User).where(User.username == "erens"))
    assert user is not None
    assert user.auth_source == "oidc"
    assert user.email == "erens@example.test"
    assert user.display_name == "Eren S"
    # Bound to the issuer's subject, which is what makes the refusals below work.
    assert user.external_id == "9f21c0de-0000-4000-8000-000000000001"
    # A federated account never carries a password: leaving one behind would be a
    # second way in that nobody is watching.
    assert user.password_hash is None


def test_the_pkce_verifier_is_sent_on_the_exchange(client, issuer, signing_key):
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}

    callback(client, state)

    assert issuer["token_requests"][0]["code_verifier"]
    assert issuer["token_requests"][0]["grant_type"] == "authorization_code"


def test_a_successful_sso_sign_in_is_audited(client, issuer, signing_key):
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}

    callback(client, state)

    records = [r for r in recorder.query(category="console")["items"] if r["verb"] == "login"]
    assert len(records) == 1
    assert records[0]["outcome"] == "applied"
    assert records[0]["actor"] == "erens"
    assert "single sign-on" in records[0]["detail"]


# --------------------------------------------------------------------------- #
# Token verification — each test is one attack
# --------------------------------------------------------------------------- #

def test_a_token_for_another_client_is_refused(client, issuer, signing_key):
    """Audience confusion.

    A token minted by the *same* issuer for a *different* application is a valid,
    correctly-signed token. Without an audience check this console accepts it, so
    anyone who can obtain a token for any other client of the same IdP can sign
    in here as whoever that token names.
    """
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, aud="some-other-application")
    }

    response = callback(client, state)

    assert "auth_error=" in response.headers["location"]
    assert _no_session(response)


def test_a_token_from_another_issuer_is_refused(client, issuer, signing_key):
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, iss="https://evil.example/realms/x")
    }

    response = callback(client, state)

    assert "auth_error=" in response.headers["location"]
    assert _no_session(response)


def test_an_expired_token_is_refused(client, issuer, signing_key):
    """A token with no enforced expiry is a permanent credential."""
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, exp=past, iat=past)
    }

    response = callback(client, state)

    assert "auth_error=" in response.headers["location"]
    assert _no_session(response)


def test_a_token_signed_by_the_wrong_key_is_refused(client, issuer):
    """Without signature verification, an ID token is just base64."""
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(attacker_key, nonce=nonce)}

    response = callback(client, state)

    assert "auth_error=" in response.headers["location"]
    assert _no_session(response)


def test_an_unsigned_token_is_refused(client, issuer, signing_key):
    """`alg: none` — the oldest JWT vulnerability there is.

    It works against any verifier that takes the algorithm from the token it is
    verifying. `ALLOWED_ALGORITHMS` is what makes it impossible here, and the
    same list is what blocks the HS256 confusion attack (signing with the
    issuer's public key and having it used as an HMAC secret).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    unsigned = jwt.encode(
        {
            "iss": ISSUER, "aud": CLIENT_ID, "sub": "attacker",
            "exp": now + datetime.timedelta(minutes=5), "iat": now,
            "preferred_username": "erens",
        },
        key="",
        algorithm="none",
        headers={"kid": KID},
    )
    state, _ = start_handshake(client)
    issuer["token_response"] = {"id_token": unsigned}

    response = callback(client, state)

    assert "auth_error=" in response.headers["location"]
    assert _no_session(response)
    assert "none" not in oidc.ALLOWED_ALGORITHMS


def test_a_replayed_token_from_another_sign_in_is_refused(client, issuer, signing_key):
    """The nonce is what binds an assertion to *this* browser's attempt.

    Without it, a token captured from any other sign-in at the same issuer — from
    a log, a proxy, another application's error report — can be presented here.
    """
    state, _ = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce="a-nonce-from-elsewhere")}

    response = callback(client, state)

    assert "auth_error=" in response.headers["location"]
    assert _no_session(response)


def test_a_callback_with_a_mismatched_state_is_refused_and_recorded(client, issuer):
    """A forged callback: somebody else started the sign-in."""
    start_handshake(client)

    response = callback(client, "not-the-state-we-issued")

    assert "state_mismatch" in response.headers["location"]
    assert _no_session(response)
    denied = [
        r for r in recorder.query(category="console")["items"]
        if r["verb"] == "login" and r["outcome"] == "denied"
    ]
    assert len(denied) == 1


def test_a_callback_without_a_handshake_is_refused(client, issuer):
    response = client.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"})

    assert "handshake_missing_or_expired" in response.headers["location"]
    assert _no_session(response)


def test_an_unauthenticated_callback_cannot_append_audit_records(client, issuer):
    """The callback is public, so nothing before the handshake check may write.

    `/api/auth/oidc/callback` has to be reachable without a session — single
    sign-on is how a session is obtained. That makes every code path before the
    handshake is verified reachable by anyone who can reach the console, and an
    audit write on one of them is an unauthenticated INSERT into the one table in
    this schema with no upper bound on rows. A loop over `?error=` would fill the
    operator's database.

    A callback that matches no handshake this console started is also not a
    failed sign-in. Recording it as one puts rows in the trail that no operator's
    action produced, which is noise in the table that exists to be evidence.
    """
    for index in range(25):
        response = client.get(
            "/api/auth/oidc/callback",
            params={"error": f"attacker-controlled-{index}"},
        )
        assert response.status_code == 302
        client.cookies.clear()

    client.get("/api/auth/oidc/callback")
    client.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"})

    assert recorder.query(category="console")["items"] == []


def test_a_provider_refusal_after_a_real_handshake_is_recorded(client, issuer):
    """The other half: a refusal we did start is a real event and is kept.

    Without this, the fix above would read as "stop recording SSO failures",
    which is the opposite of what it is. The handshake is what separates an
    operator's failed sign-in from a stranger's stray GET.
    """
    start_handshake(client)

    client.get("/api/auth/oidc/callback", params={"error": "access_denied"})

    records = [r for r in recorder.query(category="console")["items"] if r["verb"] == "login"]
    assert len(records) == 1
    assert records[0]["outcome"] == "denied"
    assert "access_denied" in records[0]["error"]


def test_the_callback_records_nothing_when_sso_is_not_configured(client, monkeypatch):
    """A deployment that never enabled SSO must not have a writable endpoint.

    The router is mounted unconditionally, so the route exists whether or not
    OIDC is configured. Without the check it is an audit-write endpoint on every
    deployment, including the ones whose operators have never heard of it.
    """
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", False)

    response = client.get("/api/auth/oidc/callback", params={"error": "whatever"})

    assert response.status_code == 302
    assert recorder.query(category="console")["items"] == []


def test_a_failed_assertion_is_audited(client, issuer, signing_key):
    state, _ = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce="wrong")}

    callback(client, state)

    records = [r for r in recorder.query(category="console")["items"] if r["verb"] == "login"]
    assert len(records) == 1
    assert records[0]["outcome"] == "denied"
    assert records[0]["error"]


# --------------------------------------------------------------------------- #
# Claims, groups and roles
# --------------------------------------------------------------------------- #

def test_the_admin_group_maps_to_the_admin_role(client, issuer, signing_key, monkeypatch, db_session):
    monkeypatch.setattr(settings, "oidc_admin_group", "cn=platform-admins")
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, groups=["CN=Platform-Admins", "cn=staff"])
    }

    callback(client, state)

    db_session.expire_all()
    from sqlalchemy import select

    assert db_session.scalar(select(User).where(User.username == "erens")).role == "admin"


def test_an_absent_groups_claim_does_not_demote_an_administrator(
    client, issuer, signing_key, monkeypatch, db_session
):
    """The tri-state, on the SSO path.

    Most issuers omit the groups claim entirely unless the scope was requested
    *and* the client is configured to emit it — so "absent" is the common state
    during setup, which is exactly when an administrator would otherwise be
    quietly downgraded and the next good login would silently restore them.
    """
    monkeypatch.setattr(settings, "oidc_admin_group", "cn=platform-admins")
    from sqlalchemy import select

    # First sign-in with the claim present: this account is an admin.
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, groups=["cn=platform-admins"])
    }
    callback(client, state)
    db_session.expire_all()
    assert db_session.scalar(select(User).where(User.username == "erens")).role == "admin"

    # Second sign-in, same person, and the issuer forgot to send the claim.
    client.cookies.clear()
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}
    callback(client, state)

    db_session.expire_all()
    assert db_session.scalar(select(User).where(User.username == "erens")).role == "admin"


def test_an_empty_groups_claim_does_apply_the_default_role(
    client, issuer, signing_key, monkeypatch, db_session
):
    """`[]` is an answer; `absent` is not. This is the half that must still work."""
    monkeypatch.setattr(settings, "oidc_admin_group", "cn=platform-admins")
    from sqlalchemy import select

    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, groups=["cn=platform-admins"])
    }
    callback(client, state)

    client.cookies.clear()
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce, groups=[])}
    callback(client, state)

    db_session.expire_all()
    assert db_session.scalar(select(User).where(User.username == "erens")).role == "user"


def test_the_group_allowlist_fails_closed_when_the_claim_is_absent(
    client, issuer, signing_key, monkeypatch
):
    """The opposite rule from role mapping, and deliberately so.

    Role mapping asks "should this person be promoted", where the safe answer
    under uncertainty is to change nothing. An allowlist asks "may this person in
    at all", where the safe answer under uncertainty is no. An allowlist that
    admitted everyone whenever the claim went missing would stop working exactly
    when the issuer is misconfigured.
    """
    monkeypatch.setattr(settings, "oidc_allowed_groups", "cn=console-users")
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}

    response = callback(client, state)

    assert "auth_error=permission_denied" in response.headers["location"]
    assert _no_session(response)


def test_the_group_allowlist_refuses_someone_outside_it(client, issuer, signing_key, monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_groups", "cn=console-users")
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, groups=["cn=everyone-else"])
    }

    response = callback(client, state)

    assert "auth_error=permission_denied" in response.headers["location"]
    assert _no_session(response)


def test_a_space_separated_groups_string_is_split(client, issuer, signing_key, monkeypatch, db_session):
    """Several issuers emit groups as one delimited string, not as a list.

    Treated as a single value, "admins staff" is a group nobody is ever in, and
    the symptom is an admin group that silently never matches.
    """
    monkeypatch.setattr(settings, "oidc_admin_group", "admins")
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce, groups="admins staff")}

    callback(client, state)

    db_session.expire_all()
    from sqlalchemy import select

    assert db_session.scalar(select(User).where(User.username == "erens")).role == "admin"


# --------------------------------------------------------------------------- #
# Account binding
# --------------------------------------------------------------------------- #

def test_sso_cannot_take_over_a_local_account(client, issuer, signing_key, db_session):
    """Otherwise anyone who can make an IdP assert a username inherits that account.

    `erens` is a local administrator with a password. An SSO assertion carrying
    the same username must not adopt that row — it would be a privilege
    escalation that looks exactly like a successful sign-in.
    """
    from app.identity.service import create_local_user

    create_local_user(
        db_session, username="erens", password="correct-horse-battery-staple", role="admin"
    )

    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}
    response = callback(client, state)

    assert "auth_error=permission_denied" in response.headers["location"]
    assert _no_session(response)

    db_session.expire_all()
    from sqlalchemy import select

    row = db_session.scalar(select(User).where(User.username == "erens"))
    assert row.auth_source == "local"
    assert row.password_hash is not None


def test_a_second_subject_cannot_claim_an_existing_username(client, issuer, signing_key, db_session):
    """A username is a label an issuer can reuse; the subject is the identity.

    Once an account is bound to a subject, a different subject presenting the
    same username is either a recycled name or a second issuer asserting it, and
    neither may inherit the first account's role.
    """
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}
    callback(client, state)

    client.cookies.clear()
    state, nonce = start_handshake(client)
    issuer["token_response"] = {
        "id_token": mint(signing_key, nonce=nonce, sub="a-completely-different-subject")
    }
    response = callback(client, state)

    assert "auth_error=permission_denied" in response.headers["location"]
    assert _no_session(response)


def test_a_deactivated_sso_account_cannot_sign_back_in(client, issuer, signing_key, db_session):
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}
    callback(client, state)

    from sqlalchemy import select

    db_session.expire_all()
    row = db_session.scalar(select(User).where(User.username == "erens"))
    row.active = False
    db_session.commit()

    client.cookies.clear()
    state, nonce = start_handshake(client)
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}
    response = callback(client, state)

    assert "auth_error=permission_denied" in response.headers["location"]
    assert _no_session(response)


# --------------------------------------------------------------------------- #
# The post-sign-in redirect
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "hostile",
    ["https://evil.example", "//evil.example", "/\\evil.example", "http://evil.example/x"],
)
def test_the_return_path_cannot_leave_this_origin(client, issuer, hostile):
    """An open redirect on the login route is a phishing amplifier.

    A link carrying `?next=https://evil.example` produces a page on the console's
    own domain that authenticates the operator and then hands them somewhere
    else, with the console's URL in the address bar the whole way.
    """
    response = client.get("/api/auth/oidc/start", params={"next": hostile})

    assert response.status_code == 302
    pending = handshake_service.unseal(response.cookies[handshake_service.COOKIE_NAME])
    assert pending.next_path == "/"


def test_a_same_origin_return_path_is_honoured(client, issuer, signing_key):
    state = None
    response = client.get("/api/auth/oidc/start", params={"next": "/workloads"})
    sealed = response.cookies[handshake_service.COOKIE_NAME]
    client.cookies.set(handshake_service.COOKIE_NAME, sealed)
    pending = handshake_service.unseal(sealed)
    state, nonce = pending.state, pending.nonce
    issuer["token_response"] = {"id_token": mint(signing_key, nonce=nonce)}

    result = callback(client, state)

    assert result.headers["location"] == "/workloads"


# --------------------------------------------------------------------------- #
# Provider failures are not credential rejections
# --------------------------------------------------------------------------- #

def test_an_unreachable_issuer_is_reported_as_a_provider_failure(client, monkeypatch):
    """"The directory could not answer" and "your credentials were wrong" send an
    operator to two different places, and the second one is a dead end when the
    first is true."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://unreachable.example/realms/x")
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    oidc.reset_discovery_cache()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(refuse), "verify": True}),
    )

    response = client.get("/api/auth/oidc/start")

    assert response.status_code == 302
    assert "auth_error=identity_provider_unavailable" in response.headers["location"]
    oidc.reset_discovery_cache()


def test_a_provider_failure_is_recorded_as_failed_not_denied(client, monkeypatch):
    """`failed` keeps a provider outage out of the throttle count.

    A directory that is down must not lock every operator out of the console for
    the throttle window on top of being down.
    """
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://unreachable.example/realms/x")
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    oidc.reset_discovery_cache()

    real_client = httpx.Client

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(refuse), "verify": True}),
    )

    client.get("/api/auth/oidc/start")

    records = [r for r in recorder.query(category="console")["items"] if r["verb"] == "login"]
    assert len(records) == 1
    assert records[0]["outcome"] == "failed"
    oidc.reset_discovery_cache()


def _no_session(response) -> bool:
    """True when the response did not hand out a console session."""
    return settings.auth_cookie_name not in response.cookies
