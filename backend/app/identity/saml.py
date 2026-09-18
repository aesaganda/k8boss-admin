"""
SAML 2.0 single sign-on: SP-initiated Redirect out, POST assertion back.

For the identity providers that speak SAML and nothing else — ADFS, Shibboleth,
PingFederate, an Okta or Entra application registered as SAML rather than OIDC.
One identity provider per deployment, configured from the environment exactly as
the other three are.

## The only thing that makes a SAML assertion trustworthy

An assertion is an XML document that says who somebody is, and the *entire*
trust argument is one XML signature over one subtree of it. That makes SAML
different in kind from the other three providers here: with OIDC the token is
opaque and either verifies or does not; with OAuth and OpenShift the console
fetches the identity itself over a channel it verified. Here the console is
handed a document by the browser — by an attacker's browser, on the attacks that
matter — and has to decide which part of it, if any, the identity provider
actually vouched for.

**Everything this module reads comes out of the verified subtree returned by the
signature check, and nothing is ever read from the document as posted.** That
sentence is the whole defence against XML Signature Wrapping, which is the
canonical SAML vulnerability and has broken essentially every implementation
that has ever got it wrong: the attacker takes a genuine signed assertion, moves
it somewhere the verifier will still find a valid signature, and puts their own
forged assertion where the *reader* will find it. Both halves are true at once —
the signature verifies, and the identity is the attacker's. Verifying and then
re-reading the original document is not a mistake anyone makes on purpose; it is
what happens when verification and extraction are written as two steps with a
document between them.

So :func:`verified_assertion` returns an element, and :func:`identity_from_assertion`
takes one. There is no code path in this module that parses the POSTed XML and
also produces an identity from it.

## What is checked, and what each check prevents

* **The signature**, against a configured certificate. Several may be
  configured, which is what makes an IdP signing-key rotation a config change
  rather than an outage.
* **The issuer**, against ``SAML_IDP_ENTITY_ID``. A certificate this console
  trusts must not be able to sign an assertion *for somebody else's issuer* — on
  a shared IdP platform that is the difference between one tenant and all of
  them.
* **The audience**, against this console's entity ID. An assertion minted for a
  different service provider by the same IdP is a genuine, correctly signed
  assertion; without this check, anyone who can obtain one can sign in here.
  This is the SAML spelling of OIDC's ``aud``.
* **``InResponseTo``**, against the ``AuthnRequest`` ID sealed in this browser's
  handshake cookie. This is what binds the assertion to a sign-in *this browser*
  started, and it is why unsolicited (IdP-initiated) responses are refused
  outright: they carry no ``InResponseTo``, so nothing binds them to anything,
  and accepting them means accepting an assertion anyone can replay into anyone
  else's browser.
* **``Recipient``**, against this console's ACS URL, and **required** rather
  than checked when it happens to be there. An assertion addressed to another
  endpoint was not meant for this one, and the party replaying it here can omit
  the attribute — so a check that only ran when it was present checked nothing
  in the one case it exists for. The response wrapper's ``Destination`` is not
  checked: on the assertion-signed shape (Shibboleth, Keycloak, Okta) it sits
  outside the signature, so it is a value the replayer writes, and a check
  against it would read as protection without being any.
* **``NotBefore`` / ``NotOnOrAfter``**, on both the conditions and the subject
  confirmation, with configurable leeway. An assertion with no expiry that
  verifies is a permanent credential.
* **An ``AuthnStatement`` must be present.** An assertion carrying only
  attributes is a statement *about* somebody, not a statement that they just
  authenticated.

## What this deliberately does not do

**Encrypted assertions are refused, not ignored.** Supporting them needs an SP
decryption key, its storage, and its rotation. A console that silently fell back
to reading the unencrypted parts of a response whose assertion it could not
decrypt would be signing people in on the strength of an envelope, so an
``EncryptedAssertion`` produces a named refusal that tells the administrator to
turn assertion encryption off for this service provider.

**The ``AuthnRequest`` is not signed.** Signing it needs an SP key pair with the
same storage and rotation problem, and it protects the IdP from unsolicited
requests rather than protecting this console from anything. IdPs that require a
signed request will refuse the handshake with their own error, which is a
configuration failure at setup rather than a silent weakness.

**Replay is bounded by the handshake, not by an assertion-ID store.** An
assertion can only be spent against the sealed cookie naming its
``AuthnRequest`` ID; that cookie is deleted the moment the ACS runs and is
unreadable after ten minutes regardless. A one-time-use store keyed on assertion
ID would be strictly stronger and needs the database table
:mod:`app.identity.handshake` explains why this flow does not have. The bound is
stated here rather than left for a reader to discover.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import logging
import secrets
import zlib
from urllib.parse import urlencode

from app.config import settings
from app.errors import Invalid, PermissionDenied
from app.identity import handshake as handshake_service
from app.identity import sso
from app.identity.sso import Begin, FederatedIdentity
from app.identity import provider_config

logger = logging.getLogger(__name__)

#: Registry identity. See :mod:`app.identity.sso`.
NAME = "saml"
#: The assertion arrives on a cross-site form POST, which is why the handshake
#: cookie has to be ``SameSite=None; Secure`` — see :mod:`app.identity.handshake`.
BINDING = "form"
#: "acs", not "callback": Assertion Consumer Service is the name every SAML
#: administrator registers, and a console that called its endpoint something else
#: would be asking them to mistype it into a form they fill in once.
CALLBACK_SUFFIX = "acs"

def _cfg() -> provider_config.ProviderConfig:
    """This deployment's saml configuration: the stored row, else the environment.

    Resolved per call rather than held, because a cached provider configuration
    outlives the edit that changed it — see :mod:`app.identity.provider_store`
    on why nothing there is cached.
    """
    return provider_config.resolve(NAME)


SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAMLP_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
_NS = {"saml": SAML_NS, "samlp": SAMLP_NS, "ds": DS_NS}

STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"

#: Largest ``SAMLResponse`` this console will base64-decode. A SAML response is a
#: few kilobytes; a megabyte of XML is somebody probing what the parser does with
#: it, and the answer should be "refuses it" rather than "finds out".
MAX_RESPONSE_BYTES = 512 * 1024


def enabled() -> bool:
    """True when this deployment can actually complete a SAML sign-in.

    ``AUTH_COOKIE_SECURE`` is in the list, and it is the surprising one. The
    assertion arrives on a cross-site form POST, which carries no ``SameSite=Lax``
    cookie, so the handshake has to be ``SameSite=None`` — and every current
    browser discards a ``SameSite=None`` cookie that is not also ``Secure``. On a
    plain-HTTP deployment the handshake cookie would therefore never come back
    and every sign-in would fail at "the sign-in did not match", which reads as a
    broken identity provider. Withholding the button instead is the same rule the
    other three providers follow for a missing client id.
    """
    cfg = _cfg()
    return bool(
        cfg.enabled
        and cfg.idp_sso_url.strip()
        and cfg.idp_certificate.strip()
        and settings.auth_cookie_secure
    )


def require_enabled() -> None:
    cfg = _cfg()
    if not enabled():
        if (
            cfg.enabled
            and cfg.idp_sso_url.strip()
            and cfg.idp_certificate.strip()
            and not settings.auth_cookie_secure
        ):
            # Named separately, because "not configured" would send an
            # administrator to re-check three settings that are already correct.
            raise Invalid(
                "SAML single sign-on needs the console to be served over HTTPS.",
                hint="The assertion arrives on a cross-site POST, which carries "
                     "no SameSite=Lax cookie, so the handshake cookie must be "
                     "SameSite=None — and a browser discards that unless it is "
                     "also Secure. Set AUTH_COOKIE_SECURE=true and serve the "
                     "console over TLS.",
                context={"provider": NAME},
            )
        raise sso.not_configured(
            NAME,
            provider_config.configure_hint(
                cfg, "SAML_ENABLED, SAML_IDP_SSO_URL and SAML_IDP_CERTIFICATE"
            ),
        )


def label() -> str:
    cfg = _cfg()
    return cfg.button_label


def admin_group() -> str:
    cfg = _cfg()
    return cfg.admin_group


def configured_callback_url() -> str:
    cfg = _cfg()
    return cfg.acs_url.strip()


def entity_id(*, acs_url: str) -> str:
    """This console's SAML entityID.

    Defaults to the ACS URL, which is the common convention and means one fewer
    value an administrator has to keep identical in two systems. It is what the
    ``Audience`` in an assertion is checked against, so a deployment that
    registered a different entityID at the IdP must set ``SAML_SP_ENTITY_ID`` —
    otherwise every assertion is refused for an audience mismatch, which is the
    correct refusal for the wrong reason.
    """
    cfg = _cfg()
    return cfg.sp_entity_id.strip() or acs_url


def certificates() -> list[str]:
    """The IdP signing certificates, as PEM blocks.

    Accepts PEM and the bare base64 body that IdP metadata carries in
    ``<ds:X509Certificate>``, because copying that value straight out of a
    metadata document is what an administrator will do and re-wrapping it by hand
    is an error with a confusing symptom (every signature "invalid").

    Several may be configured, concatenated. During a signing-key rotation an IdP
    publishes two and signs with either, so accepting a list is what makes the
    rotation a config change rather than a window in which every sign-in fails.
    """
    cfg = _cfg()
    raw = cfg.idp_certificate.strip()
    if not raw:
        return []
    if "-----BEGIN CERTIFICATE-----" in raw:
        blocks: list[str] = []
        for chunk in raw.split("-----BEGIN CERTIFICATE-----")[1:]:
            body = chunk.split("-----END CERTIFICATE-----")[0]
            blocks.append(
                "-----BEGIN CERTIFICATE-----"
                f"{body}"
                "-----END CERTIFICATE-----"
            )
        return blocks
    body = "".join(raw.split())
    wrapped = "\n".join(body[index:index + 64] for index in range(0, len(body), 64))
    return [f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----"]


def _parser():
    """A parser that will not fetch anything or expand anything.

    Entity expansion and DTD loading are how an XML document becomes a file read
    or an outbound request from inside this process, and this document arrives
    from an unauthenticated POST. ``huge_tree`` stays off so a deeply nested
    document is refused rather than exhausting memory.
    """
    from lxml import etree

    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def _refuse(message: str, **kwargs) -> PermissionDenied:
    return PermissionDenied(message, context={"provider": NAME}, **kwargs)


def _decode_response(encoded: str):
    """Base64-decode and parse the posted ``SAMLResponse``.

    A malformed document is a refusal rather than a 500: this endpoint is public,
    so anything that can be posted at it will be, and an unhandled parse error is
    a stack trace in the log for every probe.
    """
    from lxml import etree

    value = (encoded or "").strip()
    if not value:
        raise _refuse("The single sign-on response carried no assertion.")
    if len(value) > MAX_RESPONSE_BYTES:
        raise _refuse("The single sign-on response was too large to process.")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _refuse("The single sign-on response could not be decoded.") from exc
    try:
        return etree.fromstring(raw, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise _refuse("The single sign-on response was not readable XML.") from exc


def _status_of(root) -> str:
    code = root.find("samlp:Status/samlp:StatusCode", _NS)
    return (code.get("Value") if code is not None else "") or ""


def verified_assertion(root):
    """The one assertion this identity provider actually signed, or a refusal.

    Two shapes are accepted because both are in the wild: the signature may cover
    the whole ``Response`` (Entra ID, some ADFS configurations) or just the
    ``Assertion`` (Shibboleth, Keycloak, most Okta applications). Both are tried,
    the Response first, and the **element returned is the one the signature
    library re-parsed from the bytes it verified** — never a node located in the
    posted document. See the module docstring on XML Signature Wrapping; that
    substitution is the whole attack.

    Every configured certificate is tried, so a signing-key rotation is a
    configuration change rather than an outage.
    """
    from signxml import SignatureConfiguration, XMLVerifier
    from signxml.exceptions import InvalidSignature

    pems = certificates()
    if not pems:
        raise _refuse(
            "This console has no certificate to check the single sign-on "
            "response against.",
            hint="Set SAML_IDP_CERTIFICATE to the identity provider's signing "
                 "certificate.",
        )

    # `location="./"` restricts each check to a signature that is a *direct
    # child* of the element being verified. The library's default searches the
    # whole subtree, which would let a signature buried somewhere else in the
    # document satisfy a check about this element.
    config = SignatureConfiguration(
        require_x509=True, location="./", expect_references=1
    )
    failures: list[str] = []

    for pem in pems:
        try:
            result = XMLVerifier().verify(root, x509_cert=pem, expect_config=config)
        except InvalidSignature as exc:
            failures.append(f"response signature: {exc}")
        except Exception as exc:  # noqa: BLE001 - "no signature here" is one of these
            failures.append(f"response signature: {type(exc).__name__}")
        else:
            assertion = result.signed_xml.find("saml:Assertion", _NS)
            if assertion is None:
                raise _refuse(
                    "The identity provider signed a response that contains no "
                    "assertion."
                )
            return assertion

    for candidate in root.findall("saml:Assertion", _NS):
        for pem in pems:
            try:
                result = XMLVerifier().verify(
                    candidate, x509_cert=pem, expect_config=config
                )
            except InvalidSignature as exc:
                failures.append(f"assertion signature: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"assertion signature: {type(exc).__name__}")
            else:
                return result.signed_xml

    if root.find("saml:EncryptedAssertion", _NS) is not None:
        # Named rather than folded into "no valid signature": the administrator's
        # next action is completely different, and a console that reported this
        # as a signature failure would send them to re-check a certificate that
        # is correct.
        raise _refuse(
            "This console cannot read an encrypted SAML assertion.",
            hint="Turn assertion encryption off for this service provider. The "
                 "assertion is still signed, and it travels inside TLS; the "
                 "console holds no decryption key and will not pretend to.",
        )

    # The individual failures are logged and not returned. Which certificate
    # failed and how is useful to an operator reading logs and is an oracle to
    # whoever is posting the documents.
    logger.warning(
        "Refusing a SAML response: no signature verified against %d configured "
        "certificate(s). %s", len(pems), "; ".join(failures[:4]) or "no signature found",
    )
    raise _refuse(
        "The single sign-on response was not signed by this console's "
        "configured identity provider.",
        hint="If the identity provider has rotated its signing key, add the new "
             "certificate to SAML_IDP_CERTIFICATE.",
    )


def _instant(value: str | None) -> datetime.datetime | None:
    """One ``xsd:dateTime`` as an aware UTC datetime, or ``None`` if unparsable.

    ``None`` is never treated as "valid": every caller refuses on it, because a
    timestamp this console could not read is a condition it could not check and
    an unchecked expiry is no expiry.
    """
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _check_window(
    *,
    not_before: str | None,
    not_on_or_after: str | None,
    now: datetime.datetime,
    what: str,
    required: bool,
) -> None:
    """Refuse an assertion that is not valid at ``now``, within the configured leeway.

    The leeway exists because SAML condition windows are frequently five minutes
    wide and a console whose clock is a minute behind its IdP would otherwise
    reject every assertion — a symptom that reads as a broken identity provider
    and sends nobody to look at NTP.
    """
    cfg = _cfg()
    leeway = datetime.timedelta(seconds=cfg.clock_skew_seconds)

    if not_before:
        start = _instant(not_before)
        if start is None:
            raise _refuse(f"The assertion's {what} start time was not readable.")
        if now + leeway < start:
            raise _refuse(f"The assertion is not valid yet ({what}).")

    if not_on_or_after:
        end = _instant(not_on_or_after)
        if end is None:
            raise _refuse(f"The assertion's {what} expiry was not readable.")
        if now - leeway >= end:
            raise _refuse(f"The assertion has expired ({what}).")
    elif required:
        # An assertion with no expiry that verifies is a permanent credential,
        # replayable for as long as the signing key lives.
        raise _refuse(f"The assertion carries no expiry ({what}).")


def validate_assertion(assertion, *, request_id: str, acs_url: str) -> None:
    """Every check besides the signature. Raises, or returns having found nothing.

    ``assertion`` must be the element :func:`verified_assertion` returned. Passing
    anything else makes every check below a check on a document the identity
    provider never vouched for.
    """
    cfg = _cfg()
    now = datetime.datetime.now(datetime.timezone.utc)

    issuer = assertion.find("saml:Issuer", _NS)
    stated = (issuer.text or "").strip() if issuer is not None else ""
    expected_issuer = cfg.idp_entity_id.strip()
    if expected_issuer and stated != expected_issuer:
        # A certificate this console trusts must not be able to assert an
        # identity for a different issuer. On a shared IdP platform that is the
        # difference between one tenant and every tenant.
        raise _refuse(
            "The assertion was issued by a different identity provider.",
            detail=f"issuer {stated!r}",
        )

    conditions = assertion.find("saml:Conditions", _NS)
    if conditions is None:
        raise _refuse("The assertion carries no conditions, so it never expires.")
    _check_window(
        not_before=conditions.get("NotBefore"),
        not_on_or_after=conditions.get("NotOnOrAfter"),
        now=now,
        what="conditions",
        required=True,
    )

    audiences = [
        (node.text or "").strip()
        for node in conditions.findall(
            "saml:AudienceRestriction/saml:Audience", _NS
        )
    ]
    if audiences:
        expected_audience = entity_id(acs_url=acs_url)
        if expected_audience not in audiences:
            # The SAML spelling of OIDC's `aud`: an assertion the same IdP minted
            # for a different service provider is genuine and correctly signed,
            # and without this anyone holding one could sign in here.
            raise _refuse(
                "The assertion was issued for a different service provider.",
                detail=f"audiences {', '.join(audiences)!r}",
                hint="Set SAML_SP_ENTITY_ID to the entityID registered at the "
                     "identity provider.",
            )

    confirmations = assertion.findall(
        "saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", _NS
    )
    if not confirmations:
        raise _refuse(
            "The assertion carries no subject confirmation, so nothing binds it "
            "to this sign-in."
        )

    accepted = False
    for data in confirmations:
        in_response_to = (data.get("InResponseTo") or "").strip()
        if not in_response_to:
            # Unsolicited, IdP-initiated. Refused: nothing binds it to a browser,
            # so it can be replayed into anybody's.
            continue
        # Compared as bytes. `compare_digest` refuses a `str` carrying any
        # non-ASCII character, and the TypeError would escape as a 500 rather
        # than the audited refusal a replay is supposed to produce: the
        # attribute is the replayer's to write, so one non-ASCII character
        # would buy them an unrecorded server error instead.
        if not secrets.compare_digest(in_response_to.encode(), request_id.encode()):
            continue
        recipient = (data.get("Recipient") or "").strip()
        if recipient != acs_url:
            # Required, not checked-when-present. An assertion replayed at a
            # different service provider's endpoint is one somebody copied, and
            # the attribute that catches it is one they can simply leave out —
            # so validating it only when it happened to be there validated
            # nothing in the single case the check exists for. SAML core makes
            # it mandatory on bearer confirmation data, so no conforming IdP
            # loses a sign-in to this.
            continue
        try:
            _check_window(
                not_before=data.get("NotBefore"),
                not_on_or_after=data.get("NotOnOrAfter"),
                now=now,
                what="subject confirmation",
                required=True,
            )
        except PermissionDenied:
            continue
        accepted = True
        break

    if not accepted:
        raise _refuse(
            "The assertion does not match the sign-in this console started.",
            hint="This is what a replayed or misdirected assertion looks like. "
                 "Start the sign-in again. Identity-provider-initiated sign-on "
                 "is not accepted, because nothing binds it to your browser.",
        )

    if assertion.find("saml:AuthnStatement", _NS) is None:
        # An assertion carrying only attributes is a statement *about* somebody,
        # not a statement that they just authenticated.
        raise _refuse(
            "The assertion does not state that anybody authenticated.",
            hint="The identity provider sent an attribute assertion rather than "
                 "an authentication response.",
        )


def attributes(assertion) -> dict[str, list[str]]:
    """Every attribute in the verified assertion, by name and by friendly name.

    Both keys, because IdPs disagree about which one an administrator sees: ADFS
    emits URN names (``http://schemas.xmlsoap.org/…/emailaddress``) with no
    friendly name, Shibboleth emits both, and an operator configuring
    ``SAML_EMAIL_ATTRIBUTE`` will write whichever one their IdP's admin console
    showed them. Accepting either costs one dictionary and removes a
    configuration failure whose symptom is a silently empty profile field.
    """
    found: dict[str, list[str]] = {}
    for node in assertion.findall(
        "saml:AttributeStatement/saml:Attribute", _NS
    ):
        values = [
            (value.text or "").strip()
            for value in node.findall("saml:AttributeValue", _NS)
            if (value.text or "").strip()
        ]
        for key in (node.get("Name"), node.get("FriendlyName")):
            if key:
                found.setdefault(key.strip(), []).extend(values)
    return found


def identity_from_assertion(assertion) -> FederatedIdentity:
    """Reduce a *verified* assertion to the identity this console stores."""
    cfg = _cfg()
    values = attributes(assertion)
    name_id = assertion.find("saml:Subject/saml:NameID", _NS)
    subject = (name_id.text or "").strip() if name_id is not None else ""

    configured_username = cfg.username_attribute.strip()
    if configured_username:
        username = next(iter(values.get(configured_username, [])), "") or subject
    else:
        username = subject
    if not username:
        raise _refuse(
            "The assertion named nobody.",
            hint="The identity provider sent no NameID. Point "
                 "SAML_USERNAME_ATTRIBUTE at an attribute that carries the "
                 "username.",
        )

    if not subject:
        # The account binding hangs on this value, and an empty one would bind
        # every account to the same identity. Falling back to the username is
        # weaker than a NameID — a username can be re-issued — and it is still a
        # binding, which is what `provision_federated_user` needs to refuse a
        # second person arriving under a recycled name.
        subject = username

    email = next(iter(values.get(cfg.email_attribute.strip(), [])), None)
    display = next(
        iter(values.get(cfg.display_name_attribute.strip(), [])), None
    )

    groups_attribute = cfg.groups_attribute.strip()
    # Absent stays absent: an assertion with no groups attribute is "the IdP did
    # not tell us", which must not demote an administrator, and it is a different
    # fact from an attribute present with no values.
    raw_groups = values.get(groups_attribute) if groups_attribute in values else None
    groups = sso.parse_groups(raw_groups) if raw_groups is not None else None

    return FederatedIdentity(
        subject=subject,
        username=username,
        display_name=display or None,
        email=email or None,
        groups=groups,
    )


def check_group_allowlist(identity: FederatedIdentity) -> None:
    cfg = _cfg()
    sso.check_group_allowlist(
        identity,
        allowed=cfg.allowed_groups,
        provider=NAME,
        source_hint=(
            f"The assertion carried no {cfg.groups_attribute!r} "
            "attribute. Release the group attribute for this service provider, "
            "or clear SAML_ALLOWED_GROUPS."
        ),
    )


def authn_request(*, acs_url: str, request_id: str, issued_at: str) -> str:
    """The ``AuthnRequest`` XML this console sends.

    Deliberately minimal. ``ForceAuthn`` and ``AuthnContextClassRef`` are things
    an operator might want and are not offered, because each is a promise about
    what the identity provider will do that this console cannot verify it kept —
    an assertion comes back either way and looks identical.

    Not signed: see the module docstring.
    """
    cfg = _cfg()
    return (
        '<samlp:AuthnRequest '
        f'xmlns:samlp="{SAMLP_NS}" xmlns:saml="{SAML_NS}" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{issued_at}" '
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{acs_url}" '
        f'Destination="{cfg.idp_sso_url.strip()}">'
        f'<saml:Issuer>{entity_id(acs_url=acs_url)}</saml:Issuer>'
        '</samlp:AuthnRequest>'
    )


def redirect_url(*, acs_url: str, request_id: str) -> str:
    """The HTTP-Redirect binding URL for one ``AuthnRequest``.

    The request is DEFLATE-compressed with no zlib header, which is what the
    binding specifies and is the single most common thing to get wrong: a
    zlib-wrapped payload produces "could not parse the request" at the IdP, with
    nothing on this side to say why.

    Merged into whatever query the SSO URL already carries rather than appended,
    because several IdPs publish an SSO endpoint with a tenant parameter already
    in it and ``?SAMLRequest=`` after that is not a parameter at all.
    """
    cfg = _cfg()
    issued_at = (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    document = authn_request(
        acs_url=acs_url, request_id=request_id, issued_at=issued_at
    )
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    deflated = compressor.compress(document.encode("utf-8")) + compressor.flush()
    encoded = base64.b64encode(deflated).decode("ascii")

    base = cfg.idp_sso_url.strip()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'SAMLRequest': encoded})}"


def metadata_xml(*, acs_url: str) -> str:
    """This console's SP metadata, for pasting into the identity provider.

    A read with no secrets in it: the entityID, the ACS URL and the binding. It
    exists because the alternative is an administrator transcribing two URLs into
    a form, and a transcription error in the ACS URL fails as "the assertion does
    not match the sign-in this console started" — a message that names the
    symptom and not the cause.

    No signing or encryption certificate is advertised, because this console has
    neither: it does not sign its requests and cannot decrypt an assertion.
    Advertising a key it does not hold is how an IdP ends up encrypting to
    nobody.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{entity_id(acs_url=acs_url)}">\n'
        '  <md:SPSSODescriptor AuthnRequestsSigned="false" '
        'WantAssertionsSigned="true" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        '    <md:NameIDFormat>'
        'urn:oasis:names:tc:SAML:2.0:nameid-format:persistent'
        '</md:NameIDFormat>\n'
        '    <md:AssertionConsumerService '
        'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{acs_url}" index="0" isDefault="true"/>\n'
        '  </md:SPSSODescriptor>\n'
        '</md:EntityDescriptor>\n'
    )


# --------------------------------------------------------------------------- #
# The registry interface (see app.identity.sso)
# --------------------------------------------------------------------------- #


def begin(*, callback_url: str, next_path: str) -> Begin:
    """Send the browser to the identity provider with a fresh ``AuthnRequest``.

    The request ID is sealed as the handshake's ``state``, because that is
    exactly what it is: the value the assertion's ``InResponseTo`` must equal for
    the response to belong to this sign-in. It is prefixed with an underscore
    because ``ID`` is an XML ``xs:ID`` and may not begin with a digit — an
    ID starting with one is rejected by strict IdPs with a schema error that
    names neither this console nor the attribute.
    """
    request_id = f"_{secrets.token_hex(16)}"
    return Begin(
        url=redirect_url(acs_url=callback_url, request_id=request_id),
        handshake=handshake_service.Handshake(
            provider=NAME,
            state=request_id,
            # No token to bind and no code to exchange. Left empty rather than
            # filled with an unused secret, so nothing here reads as a
            # protection that is in force.
            nonce="",
            code_verifier="",
            redirect_uri=callback_url,
            next_path=next_path,
        ),
    )


def complete(*, handshake, params) -> FederatedIdentity:
    """Verify the posted response and produce the identity it asserts.

    The order is the point: verify, then validate the *verified* element, then
    read from it. Nothing is read from the posted document at any stage.
    """
    root = _decode_response(params.get("SAMLResponse") or "")

    status = _status_of(root)
    if status and status != STATUS_SUCCESS:
        # Read from the unsigned wrapper, and only ever used to produce a better
        # message on a request that is about to be refused anyway. A status of
        # Success is never taken as evidence of anything.
        raise _refuse(
            "The identity provider refused the sign-in.",
            detail=status.rsplit(":", 1)[-1],
        )

    assertion = verified_assertion(root)
    validate_assertion(
        assertion, request_id=handshake.state, acs_url=handshake.redirect_uri
    )
    identity = identity_from_assertion(assertion)
    check_group_allowlist(identity)
    return identity


__all__ = [
    "BINDING",
    "CALLBACK_SUFFIX",
    "MAX_RESPONSE_BYTES",
    "NAME",
    "STATUS_SUCCESS",
    "admin_group",
    "attributes",
    "authn_request",
    "begin",
    "certificates",
    "check_group_allowlist",
    "complete",
    "configured_callback_url",
    "enabled",
    "entity_id",
    "identity_from_assertion",
    "label",
    "metadata_xml",
    "redirect_url",
    "require_enabled",
    "validate_assertion",
    "verified_assertion",
]
