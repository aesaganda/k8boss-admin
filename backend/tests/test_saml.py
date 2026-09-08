"""
SAML 2.0 single sign-on (§12.4).

**These tests sign real assertions with a real key.** Mocking the signature check
and asserting that the ACS provisions a user would test the plumbing and none of
the security, and every check in :mod:`app.identity.saml` exists because skipping
it produces a login flow that works perfectly against a cooperative identity
provider.

The attack that matters most here has its own test, and it is the reason the
module is written the way it is: **XML Signature Wrapping**. The attacker takes a
genuinely signed assertion, leaves it somewhere the verifier will still find a
valid signature, and puts a forged one where the *reader* will look. Both halves
are true at once — the signature verifies and the identity is the attacker's —
and the only structural defence is to read the identity out of the subtree the
signature check itself returned. That is what
``test_a_wrapped_forged_assertion_is_not_believed`` is checking, and it fails
loudly if anybody ever reintroduces a "parse, verify, then re-read the document"
shape.
"""

from __future__ import annotations

import base64
import datetime
import zlib
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml import XMLSigner, methods
from signxml.algorithms import SignatureMethod

from app.audit import recorder
from app.config import settings
from app.identity import handshake as handshake_service
from app.identity import saml
from app.models import User

IDP_ENTITY_ID = "https://idp.example.test/metadata"
IDP_SSO_URL = "https://idp.example.test/sso/redirect"
ACS = "https://console.example.test/api/auth/saml/acs"
SP_ENTITY_ID = "https://console.example.test/saml"

NS = {"saml": saml.SAML_NS, "samlp": saml.SAMLP_NS}


# --------------------------------------------------------------------------- #
# A fake identity provider that actually signs things
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def idp_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def idp_certificate(idp_key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(idp_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(idp_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def idp_key_pem(idp_key):
    return idp_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def other_certificate():
    """A second, unrelated signing key — the one an attacker would have."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "evil.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        certificate.public_bytes(serialization.Encoding.PEM).decode(),
    )


@pytest.fixture
def idp(monkeypatch, idp_certificate):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "saml_enabled", True)
    # SAML needs a Secure handshake cookie, because the assertion arrives on a
    # cross-site POST that carries no SameSite=Lax cookie.
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "saml_idp_entity_id", IDP_ENTITY_ID)
    monkeypatch.setattr(settings, "saml_idp_sso_url", IDP_SSO_URL)
    monkeypatch.setattr(settings, "saml_idp_certificate", idp_certificate)
    monkeypatch.setattr(settings, "saml_sp_entity_id", SP_ENTITY_ID)
    monkeypatch.setattr(settings, "saml_acs_url", ACS)
    monkeypatch.setattr(settings, "saml_username_attribute", "")
    monkeypatch.setattr(settings, "saml_email_attribute", "email")
    monkeypatch.setattr(settings, "saml_display_name_attribute", "displayName")
    monkeypatch.setattr(settings, "saml_groups_attribute", "groups")
    monkeypatch.setattr(settings, "saml_admin_group", "")
    monkeypatch.setattr(settings, "saml_allowed_groups", "")
    return None


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


def response_xml(
    *,
    request_id: str,
    name_id: str = "erens",
    issuer: str = IDP_ENTITY_ID,
    audience: str | None = SP_ENTITY_ID,
    recipient: str = ACS,
    not_before: str = "2020-01-01T00:00:00Z",
    not_on_or_after: str = "2099-01-01T00:00:00Z",
    groups: list[str] | None = None,
    attributes: str = "",
    authn_statement: bool = True,
    status: str = saml.STATUS_SUCCESS,
) -> str:
    """One SAML response, with exactly one thing wrong when a test asks for it."""
    audience_xml = (
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
        "</saml:AudienceRestriction>"
        if audience
        else ""
    )
    groups_xml = ""
    if groups is not None:
        values = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
        groups_xml = f'<saml:Attribute Name="groups">{values}</saml:Attribute>'
    authn_xml = (
        '<saml:AuthnStatement AuthnInstant="2026-01-01T00:00:00Z" SessionIndex="_s1">'
        "<saml:AuthnContext><saml:AuthnContextClassRef>"
        "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        if authn_statement
        else ""
    )
    return (
        f'<samlp:Response xmlns:samlp="{saml.SAMLP_NS}" xmlns:saml="{saml.SAML_NS}" '
        f'ID="_resp1" Version="2.0" IssueInstant="2026-01-01T00:00:00Z" '
        f'Destination="{ACS}" InResponseTo="{request_id}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
        f'<saml:Assertion ID="_assert1" Version="2.0" IssueInstant="2026-01-01T00:00:00Z">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        "<saml:Subject>"
        f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">'
        f"{name_id}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData InResponseTo="{request_id}" '
        f'NotOnOrAfter="{not_on_or_after}" Recipient="{recipient}"/>'
        "</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">'
        f"{audience_xml}</saml:Conditions>"
        f"{authn_xml}"
        f"<saml:AttributeStatement>{groups_xml}{attributes}</saml:AttributeStatement>"
        "</saml:Assertion></samlp:Response>"
    )


def sign_assertion(xml: str, key_pem: str, cert_pem: str) -> bytes:
    """Sign the ``Assertion`` in place, as Shibboleth, Keycloak and Okta do."""
    root = etree.fromstring(xml.encode())
    assertion = root.find("saml:Assertion", NS)
    signed = XMLSigner(
        method=methods.enveloped, signature_algorithm=SignatureMethod.RSA_SHA256
    ).sign(assertion, key=key_pem, cert=cert_pem, reference_uri=assertion.get("ID"))
    root.replace(assertion, signed)
    return etree.tostring(root)


def sign_response(xml: str, key_pem: str, cert_pem: str) -> bytes:
    """Sign the whole ``Response``, as Entra ID and some ADFS setups do."""
    root = etree.fromstring(xml.encode())
    signed = XMLSigner(
        method=methods.enveloped, signature_algorithm=SignatureMethod.RSA_SHA256
    ).sign(root, key=key_pem, cert=cert_pem, reference_uri=root.get("ID"))
    return etree.tostring(signed)


def start(client) -> str:
    """Begin a sign-in; return the ``AuthnRequest`` id sealed in the cookie."""
    response = client.get("/api/auth/saml/start")
    assert response.status_code == 302
    sealed = response.cookies[handshake_service.cookie_name("saml")]
    client.cookies.set(handshake_service.cookie_name("saml"), sealed)
    return handshake_service.unseal(sealed, provider="saml").state


def post(client, document: bytes):
    return client.post(
        "/api/auth/saml/acs",
        data={"SAMLResponse": base64.b64encode(document).decode()},
    )


# --------------------------------------------------------------------------- #
# Being offered at all
# --------------------------------------------------------------------------- #

def test_saml_is_not_offered_on_a_plain_http_deployment(client, idp, monkeypatch):
    """The surprising prerequisite, and the one worth a test.

    The assertion arrives on a cross-site POST, which carries no SameSite=Lax
    cookie, so the handshake cookie has to be SameSite=None — and every current
    browser discards a SameSite=None cookie that is not Secure. On plain HTTP the
    cookie never comes back and every sign-in fails at "the sign-in did not
    match", which reads as a broken identity provider.
    """
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    assert client.get("/api/auth/config").json()["ssoProviders"] == []

    error = client.get("/api/auth/saml/start")
    assert error.status_code == 422
    # Named separately from "not configured", which would send an administrator
    # to re-check three settings that are already correct.
    assert "HTTPS" in error.json()["message"]


def test_the_handshake_cookie_is_samesite_none_and_secure(client, idp):
    header = client.get("/api/auth/saml/start").headers["set-cookie"]
    assert "k8boss_admin_saml=" in header
    assert "samesite=none" in header.lower()
    assert "Secure" in header


def test_the_authn_request_is_deflated_without_a_zlib_header(client, idp):
    """The single most common thing to get wrong in the Redirect binding. A
    zlib-wrapped payload produces 'could not parse the request' at the identity
    provider, with nothing on this side to say why."""
    location = client.get("/api/auth/saml/start").headers["location"]
    assert location.startswith(IDP_SSO_URL)
    encoded = parse_qs(urlparse(location).query)["SAMLRequest"][0]
    document = zlib.decompress(base64.b64decode(encoded), -zlib.MAX_WBITS).decode()
    assert document.startswith("<samlp:AuthnRequest")
    assert f'AssertionConsumerServiceURL="{ACS}"' in document
    assert f"<saml:Issuer>{SP_ENTITY_ID}</saml:Issuer>" in document


def test_the_service_provider_metadata_is_public_and_secret_free(client, idp):
    response = client.get("/api/auth/saml/metadata")
    assert response.status_code == 200
    assert f'entityID="{SP_ENTITY_ID}"' in response.text
    assert f'Location="{ACS}"' in response.text
    # No certificate is advertised, because this console has none: it does not
    # sign its requests and cannot decrypt an assertion. Advertising a key it
    # does not hold is how an IdP ends up encrypting to nobody.
    assert "X509Certificate" not in response.text


# --------------------------------------------------------------------------- #
# The assertion
# --------------------------------------------------------------------------- #

def test_a_signed_assertion_signs_the_operator_in(client, idp, idp_key_pem, idp_certificate, db_session):
    request_id = start(client)
    document = sign_assertion(
        response_xml(
            request_id=request_id,
            groups=["platform-admins"],
            attributes='<saml:Attribute Name="email">'
                       "<saml:AttributeValue>erens@example.test</saml:AttributeValue>"
                       "</saml:Attribute>",
        ),
        idp_key_pem,
        idp_certificate,
    )
    response = post(client, document)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert settings.auth_cookie_name in response.cookies
    row = db_session.query(User).filter(User.username == "erens").one()
    assert row.auth_source == "saml"
    assert row.email == "erens@example.test"
    assert row.password_hash is None


def test_a_response_level_signature_is_also_accepted(
    client, idp, idp_key_pem, idp_certificate, db_session
):
    """Both shapes are in the wild: Entra ID signs the Response, Shibboleth signs
    the Assertion. A console that accepted only one would be a console half the
    identity providers cannot talk to."""
    request_id = start(client)
    document = sign_response(response_xml(request_id=request_id), idp_key_pem, idp_certificate)
    assert post(client, document).status_code == 302
    assert db_session.query(User).filter(User.username == "erens").one()


def test_an_unsigned_response_is_refused(client, idp):
    """Without a signature the document is XML anybody can write."""
    request_id = start(client)
    response = post(client, response_xml(request_id=request_id).encode())
    assert "assertion_rejected" in response.headers["location"]


def test_an_assertion_signed_by_the_wrong_key_is_refused(
    client, idp, other_certificate
):
    key_pem, cert_pem = other_certificate
    request_id = start(client)
    document = sign_assertion(response_xml(request_id=request_id), key_pem, cert_pem)
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_a_tampered_assertion_is_refused(client, idp, idp_key_pem, idp_certificate):
    """The digest covers the content, so editing it after signing breaks it.
    This is the naive forgery; the next test is the one that is not naive."""
    request_id = start(client)
    document = sign_assertion(response_xml(request_id=request_id), idp_key_pem, idp_certificate)
    tampered = document.replace(b">erens<", b">root<")
    assert "assertion_rejected" in post(client, tampered).headers["location"]


def test_a_wrapped_forged_assertion_is_not_believed(
    client, idp, idp_key_pem, idp_certificate, db_session
):
    """XML Signature Wrapping — the canonical SAML vulnerability.

    The attacker keeps the genuinely signed assertion in the document, so a
    signature check somewhere still passes, and inserts their own forged one
    where a naive reader would find it first. An implementation that verifies and
    then re-reads the document signs the attacker in with a valid signature in
    the log.

    The defence is structural rather than a check: the identity is read out of
    the subtree the verifier returned, so the forged assertion is not merely
    rejected — it is never anywhere the reader looks.
    """
    request_id = start(client)
    genuine = sign_assertion(response_xml(request_id=request_id), idp_key_pem, idp_certificate)

    forged_root = etree.fromstring(response_xml(request_id=request_id, name_id="root").encode())
    forged_assertion = forged_root.find("saml:Assertion", NS)
    forged_assertion.set("ID", "_forged")

    wrapped = etree.fromstring(genuine)
    wrapped.insert(0, forged_assertion)
    document = etree.tostring(wrapped)
    assert b"_forged" in document and b">root<" in document

    response = post(client, document)

    # The signed assertion is still in there and still valid, so this *is* a
    # successful sign-in — as the genuine user, never as the forged one.
    assert response.status_code == 302
    assert db_session.query(User).filter(User.username == "erens").one()
    assert db_session.query(User).filter(User.username == "root").first() is None


def test_an_assertion_for_another_service_provider_is_refused(
    client, idp, idp_key_pem, idp_certificate
):
    """The SAML spelling of OIDC's `aud`. An assertion the same IdP minted for a
    different service provider is genuine and correctly signed, and without this
    check anyone holding one can sign in here."""
    request_id = start(client)
    document = sign_assertion(
        response_xml(request_id=request_id, audience="https://someone-else.example/saml"),
        idp_key_pem,
        idp_certificate,
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_assertion_from_another_issuer_is_refused(
    client, idp, idp_key_pem, idp_certificate
):
    """On a shared identity-provider platform this is the difference between one
    tenant and every tenant: a certificate this console trusts must not be able
    to assert an identity on somebody else's behalf."""
    request_id = start(client)
    document = sign_assertion(
        response_xml(request_id=request_id, issuer="https://other-tenant.example/metadata"),
        idp_key_pem,
        idp_certificate,
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_expired_assertion_is_refused(client, idp, idp_key_pem, idp_certificate):
    request_id = start(client)
    document = sign_assertion(
        response_xml(request_id=request_id, not_on_or_after="2021-01-01T00:00:00Z"),
        idp_key_pem,
        idp_certificate,
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_assertion_for_a_different_sign_in_is_refused(
    client, idp, idp_key_pem, idp_certificate
):
    """`InResponseTo` is what binds the assertion to *this browser's* sign-in.
    Without it a genuine assertion captured anywhere is replayable into anyone's
    session."""
    start(client)
    document = sign_assertion(
        response_xml(request_id="_some-other-request"), idp_key_pem, idp_certificate
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_unsolicited_assertion_is_refused(client, idp, idp_key_pem, idp_certificate):
    """Identity-provider-initiated sign-on carries no InResponseTo, so nothing
    binds it to a browser at all. Accepting it means accepting an assertion
    anyone can post into anyone else's session."""
    request_id = start(client)
    xml = response_xml(request_id=request_id).replace(
        f'InResponseTo="{request_id}" NotOnOrAfter', "NotOnOrAfter"
    )
    document = sign_assertion(xml, idp_key_pem, idp_certificate)
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_assertion_addressed_to_another_endpoint_is_refused(
    client, idp, idp_key_pem, idp_certificate
):
    request_id = start(client)
    document = sign_assertion(
        response_xml(request_id=request_id, recipient="https://elsewhere.example/acs"),
        idp_key_pem,
        idp_certificate,
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_attribute_only_assertion_is_refused(client, idp, idp_key_pem, idp_certificate):
    """An assertion with no AuthnStatement is a statement *about* somebody, not a
    statement that they just authenticated."""
    request_id = start(client)
    document = sign_assertion(
        response_xml(request_id=request_id, authn_statement=False),
        idp_key_pem,
        idp_certificate,
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_a_failed_status_is_reported_before_anything_is_believed(
    client, idp, idp_key_pem, idp_certificate
):
    request_id = start(client)
    document = sign_assertion(
        response_xml(
            request_id=request_id,
            status="urn:oasis:names:tc:SAML:2.0:status:AuthnFailed",
        ),
        idp_key_pem,
        idp_certificate,
    )
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_encrypted_assertion_is_named_rather_than_called_a_bad_signature(
    client, idp
):
    """The administrator's next action is completely different, and reporting it
    as a signature failure sends them to re-check a certificate that is fine."""
    request_id = start(client)
    xml = (
        f'<samlp:Response xmlns:samlp="{saml.SAMLP_NS}" xmlns:saml="{saml.SAML_NS}" '
        f'ID="_r" Version="2.0" IssueInstant="2026-01-01T00:00:00Z" '
        f'InResponseTo="{request_id}">'
        f"<saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="{saml.STATUS_SUCCESS}"/></samlp:Status>'
        "<saml:EncryptedAssertion><x/></saml:EncryptedAssertion></samlp:Response>"
    )
    assert "assertion_rejected" in post(client, xml.encode()).headers["location"]


def test_garbage_at_the_acs_is_refused_rather_than_raised(client, idp):
    """This endpoint is public, so anything that can be posted at it will be. An
    unhandled parse error is a stack trace in the log for every probe."""
    for body in (b"", b"not-base64!!", base64.b64encode(b"<not-saml"), b"x" * 16):
        response = client.post("/api/auth/saml/acs", data={"SAMLResponse": body.decode()})
        assert response.status_code == 302
        assert "auth_error" in response.headers["location"]


def test_an_acs_post_without_a_handshake_writes_nothing(client, idp):
    """The same rule as the redirect callbacks, and it matters more here: this is
    an unauthenticated POST."""
    for _ in range(3):
        client.post("/api/auth/saml/acs", data={"SAMLResponse": "AAAA"})
    assert recorder.query(category="console")["items"] == []


# --------------------------------------------------------------------------- #
# Certificates, attributes, groups
# --------------------------------------------------------------------------- #

def test_a_bare_base64_certificate_from_idp_metadata_is_accepted(
    client, idp, monkeypatch, idp_certificate, idp_key_pem, db_session
):
    """It is what an administrator will paste, straight out of
    `<ds:X509Certificate>`, and re-wrapping it by hand fails as 'every signature
    invalid'."""
    body = "".join(
        line for line in idp_certificate.splitlines() if "CERTIFICATE" not in line
    )
    monkeypatch.setattr(settings, "saml_idp_certificate", body)
    request_id = start(client)
    document = sign_assertion(response_xml(request_id=request_id), idp_key_pem, idp_certificate)
    assert post(client, document).status_code == 302


def test_either_of_two_configured_certificates_verifies(
    client, idp, monkeypatch, idp_certificate, idp_key_pem, other_certificate, db_session
):
    """During a signing-key rotation an IdP publishes two and signs with either.
    Accepting a list is what makes that a config change rather than a window in
    which every sign-in fails."""
    _, stale = other_certificate
    monkeypatch.setattr(settings, "saml_idp_certificate", stale + "\n" + idp_certificate)
    request_id = start(client)
    document = sign_assertion(response_xml(request_id=request_id), idp_key_pem, idp_certificate)
    assert post(client, document).status_code == 302


def test_the_admin_group_maps_to_the_admin_role(
    client, idp, monkeypatch, idp_key_pem, idp_certificate, db_session
):
    monkeypatch.setattr(settings, "saml_admin_group", "platform-admins")
    request_id = start(client)
    document = sign_assertion(
        response_xml(request_id=request_id, groups=["platform-admins", "oncall"]),
        idp_key_pem,
        idp_certificate,
    )
    post(client, document)
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_an_absent_groups_attribute_does_not_demote_an_administrator(
    client, idp, monkeypatch, idp_key_pem, idp_certificate, db_session
):
    monkeypatch.setattr(settings, "saml_admin_group", "platform-admins")
    request_id = start(client)
    post(client, sign_assertion(
        response_xml(request_id=request_id, groups=["platform-admins"]),
        idp_key_pem, idp_certificate,
    ))
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"

    client.cookies.clear()
    request_id = start(client)
    post(client, sign_assertion(
        response_xml(request_id=request_id), idp_key_pem, idp_certificate
    ))
    db_session.expire_all()
    assert db_session.query(User).filter(User.username == "erens").one().role == "admin"


def test_the_allowlist_fails_closed_when_no_groups_are_released(
    client, idp, monkeypatch, idp_key_pem, idp_certificate
):
    monkeypatch.setattr(settings, "saml_allowed_groups", "console-users")
    request_id = start(client)
    document = sign_assertion(response_xml(request_id=request_id), idp_key_pem, idp_certificate)
    assert "assertion_rejected" in post(client, document).headers["location"]


def test_an_attribute_is_found_by_its_friendly_name_too(
    client, idp, monkeypatch, idp_key_pem, idp_certificate, db_session
):
    """ADFS emits URN names with no friendly name, Shibboleth emits both, and an
    administrator writes whichever one their console showed them."""
    monkeypatch.setattr(
        settings, "saml_email_attribute", "http://schemas.xmlsoap.org/claims/EmailAddress"
    )
    request_id = start(client)
    document = sign_assertion(
        response_xml(
            request_id=request_id,
            attributes='<saml:Attribute '
                       'Name="http://schemas.xmlsoap.org/claims/EmailAddress" '
                       'FriendlyName="email">'
                       "<saml:AttributeValue>erens@example.test</saml:AttributeValue>"
                       "</saml:Attribute>",
        ),
        idp_key_pem,
        idp_certificate,
    )
    post(client, document)
    assert db_session.query(User).filter(User.username == "erens").one().email == "erens@example.test"


# --------------------------------------------------------------------------- #
# ADR-0007
# --------------------------------------------------------------------------- #

def test_a_saml_session_cannot_act_as_a_cluster_identity(
    client, idp, idp_key_pem, idp_certificate
):
    """A SAML NameID is a name in a vocabulary no API server consumes, so the
    string this console would put in `Impersonate-User` is one it chose the shape
    of. ADR-0007 refuses to send an invented cluster identity, and this is that
    refusal — a deliberate difference from the OpenShift provider, whose username
    is the cluster's own.
    """
    from app.identity.service import load_session

    request_id = start(client)
    response = post(client, sign_assertion(
        response_xml(request_id=request_id, groups=["platform-admins"]),
        idp_key_pem, idp_certificate,
    ))
    principal = load_session(response.cookies[settings.auth_cookie_name]).principal

    assert principal.auth_source == "saml"
    assert principal.can_impersonate is False
    # The provider's own words are still captured on the session. Which sources
    # may impersonate is ADR-0007's decision to revisit, and it cannot be
    # revisited for a session that never carried the values.
    assert principal.idp_username == "erens"
    assert principal.idp_groups == ("platform-admins",)
