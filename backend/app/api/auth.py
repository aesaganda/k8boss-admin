"""
Console login, session identity, single sign-on, and administrator-managed users.

**Every terminal state of a sign-in is audited** — the successes, the rejections,
the throttled attempts and the SSO handshakes that failed verification. §10's
existing vocabulary carries all of them: ``applied`` for a sign-in that produced
a session, ``denied`` for one that was refused, ``failed`` for one this console
could not complete because the identity provider did not answer.

That distinction is the point of recording them at all. "Who tried" is the
question asked after an incident, and a trail that holds only the sign-ins that
worked cannot answer it — nor can it answer "is somebody guessing at this
account", which is also what feeds the throttle in :mod:`app.identity.throttle`.

**A rejected sign-in is recorded against the submitted username**, not against
``anonymous``. There is no session yet, so the request context has no verified
actor, and attributing every failed attempt to ``anonymous`` would collapse a
thousand attempts against one account into an undifferentiated pile. §10 states
that the actor on a ``denied`` console record is a *claim* by the caller rather
than a verified identity, which is exactly what it is.
"""

from __future__ import annotations

import functools
import logging
import secrets
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import recorder
from app.config import settings
from app.database import get_db
from app.errors import AdminError, Invalid, InvalidCredentials, NotFound
from app.identity import handshake as handshake_service
from app.identity import oidc, saml, sso, throttle
from app.identity.dependencies import current_session, require_admin
from app.identity.service import (
    FEDERATED_SOURCES,
    ROLES,
    active_admin_count,
    authenticate,
    create_local_user,
    create_session,
    hash_password,
    normalize_username,
    provision_federated_user,
    revoke_session,
    revoke_user_sessions,
)
from app.k8s.context import get_current_source_ip
from app.models import User, rfc3339
from app.resources.envelope import envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["authentication"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=4096)
    source: str = Field("auto", pattern="^(auto|local|ldap)$")


class UserCreateBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=12, max_length=4096)
    display_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=320)
    role: str = Field("user", pattern="^(admin|user)$")


class UserUpdateBody(BaseModel):
    display_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=320)
    role: str | None = Field(None, pattern="^(admin|user)$")
    active: bool | None = None
    password: str | None = Field(None, min_length=12, max_length=4096)


def _session_body(identity) -> dict:
    return {
        "enabled": True,
        "authenticated": True,
        "user": identity.principal.to_public_dict(),
        "csrfToken": identity.csrf_token,
        "expiresAt": rfc3339(identity.expires_at),
    }


def _audit_user_action(
    verb: str,
    username: str,
    detail: str,
    *,
    outcome: str = "applied",
    error: str | None = None,
) -> None:
    recorder.record_console_event(
        verb=verb, resource="users", name=username, outcome=outcome, detail=detail,
        error=error,
    )


def audited_user_change(verb: str, name_of):
    """Record the refusals as well as the successes on a user-administration route.

    §10's whole argument is that a trail holding only what worked answers "what
    changed" but not "who tried", and the second is the question asked after an
    incident. That argument was being applied to cluster writes and not to the
    endpoints that create administrators: every refusal above the success line —
    a non-admin caller, a missing user, an attempt to demote the last
    administrator or to deactivate one's own account — returned by raising, and
    left nothing behind.

    A decorator rather than try/except in three handlers, so a fourth handler
    cannot be added without the question "and where is its audit call?" being
    visible at the definition.
    """
    def decorate(handler):
        @functools.wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except AdminError as error:
                _audit_user_action(
                    verb,
                    name_of(kwargs) or "(unknown)",
                    f"Refused a console user change ({verb}).",
                    # A refusal by the console's own rules, not by a cluster:
                    # `denied` is §10's outcome for exactly that.
                    outcome="denied",
                    error=f"{error.code}: {error.message}",
                )
                raise
        return wrapper
    return decorate


def _audit_signin(
    *, outcome: str, username: str, method: str, detail: str, error: str | None = None
) -> None:
    """One console record for a sign-in attempt, whatever became of it.

    ``username`` is the *submitted* one on a rejection: the request has no
    verified identity at that point, and recording every failure as ``anonymous``
    would make "somebody is guessing at erens's account" indistinguishable from
    background noise — which is both the incident question and the input the
    throttle counts.
    """
    recorder.record_console_event(
        verb="login",
        resource="sessions",
        name=username,
        outcome=outcome,
        detail=detail,
        error=error,
        actor=username,
        source_ip=get_current_source_ip(),
    )


def _normalized_or_raw(username: str) -> str:
    """The canonical username when it is one, otherwise the submitted text.

    A malformed username still has to be recorded — it is what somebody typed,
    and a rejected login recorded against an empty actor is a row nobody can find
    when they search for the account being attacked. Bounded, because this value
    is caller-controlled and lands in a column.
    """
    try:
        return normalize_username(username)
    except Invalid:
        return (username or "").strip()[:255] or "(empty)"


def _set_session_cookie(response: Response, raw_token: str) -> None:
    """Attach the session cookie. One place, so its attributes cannot diverge.

    ``SameSite=Strict``, which is right for a console whose every request is
    same-site. Note that the OIDC *handshake* cookie is deliberately ``Lax`` —
    see :mod:`app.identity.handshake` for why the two cannot share a setting.
    """
    response.set_cookie(
        settings.auth_cookie_name,
        raw_token,
        max_age=settings.auth_session_ttl_hours * 3600,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )


@router.get("/config")
def auth_config() -> dict:
    """Public discovery. What the login page needs to render, and nothing more.

    Deliberately unauthenticated, so the SPA can decide what to show on first
    paint. It therefore says only *which* methods exist — never an issuer URL, a
    client id, an API server address, the configured groups or anything else that
    would let an unauthenticated caller enumerate how this deployment is wired.

    ``oidcEnabled`` and ``oidc`` are the OpenID Connect entries of
    ``ssoProviders``, repeated. They are retained rather than folded in because a
    browser holding an older build of the SPA reads them, and dropping them would
    take that deployment's sign-in button away at the moment the backend was
    upgraded — a console nobody can log in to, produced by a release that changed
    no behaviour.
    """
    available = sso.enabled_providers()
    entries = [
        {
            "name": provider.NAME,
            "label": provider.label(),
            "startPath": f"/api/auth/{provider.NAME}/start",
        }
        for provider in available
    ]
    oidc_entry = next(
        (entry for entry in entries if entry["name"] == oidc.NAME), None
    )
    return {
        "enabled": settings.auth_enabled,
        "localEnabled": True,
        "ldapEnabled": settings.ldap_enabled,
        "oidcEnabled": oidc_entry is not None,
        "methods": [
            "local",
            *(("ldap",) if settings.ldap_enabled else ()),
            *(entry["name"] for entry in entries),
        ],
        "ssoProviders": entries,
        "oidc": (
            {"label": oidc_entry["label"], "startPath": oidc_entry["startPath"]}
            if oidc_entry
            else None
        ),
    }


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)) -> JSONResponse:
    """Local or LDAP sign-in. Throttled, and audited on every terminal state."""
    if not settings.auth_enabled:
        raise Invalid("Application authentication is not enabled on this deployment.")

    actor = _normalized_or_raw(body.username)
    # Before the password check, so a throttled request costs one INSERT and one
    # indexed COUNT rather than a 310,000-round PBKDF2 verification — otherwise
    # the throttle still lets an attacker consume the console's CPU at the rate
    # they can send requests, and every sync handler shares one threadpool.
    #
    # It reserves rather than merely counts: counting alone let a simultaneous
    # burst all read the same number and all proceed. See app.identity.throttle.
    throttle.check(actor)

    try:
        user = authenticate(
            db, username=body.username, password=body.password, source=body.source
        )
    except AdminError as error:
        # An identity provider that could not answer is not a credential
        # rejection. The reservation taken above is released, so a directory
        # outage cannot lock out every operator who tries during it — they would
        # be told to wait for something that was never their attempt's fault,
        # on top of an outage.
        #
        # Released rather than never taken: the reservation has to precede the
        # password check for the burst protection to hold, and whether this is a
        # credential attempt is only known afterwards.
        throttle.release(actor)
        _audit_signin(
            outcome="failed", username=actor, method=body.source,
            detail=f"Sign-in could not be completed via {body.source}.",
            error=f"{error.code}: {error.message}",
        )
        raise

    if user is None:
        _audit_signin(
            outcome="denied", username=actor, method=body.source,
            detail=f"Sign-in rejected ({body.source}). The actor on this record is "
                   "the submitted username, not a verified identity.",
        )
        raise InvalidCredentials()

    # Clear the budget before anything else: a working account must not
    # accumulate a lockout against itself, and this must happen even if the
    # audit write below fails.
    throttle.release(actor)
    throttle.release(user.username)

    raw_token, identity = create_session(db, user)
    _audit_signin(
        outcome="applied", username=user.username, method=user.auth_source,
        detail=f"Signed in via {user.auth_source} with the {user.role} role.",
    )
    response = JSONResponse(content=_session_body(identity))
    _set_session_cookie(response, raw_token)
    return response


@router.get("/me")
def me(identity=Depends(current_session)) -> dict:
    return _session_body(identity)


@router.post("/logout")
def logout(request: Request) -> Response:
    """Revoke the server-side session and clear the cookie.

    Audited only when a session was actually revoked. A logout POST with no
    cookie — a second tab, a bookmarked call, a page reloaded after expiry —
    revokes nothing, and recording it would put sign-out rows in the trail for
    people who were never signed in.
    """
    raw_token = request.cookies.get(settings.auth_cookie_name)
    principal = getattr(request.state, "auth_principal", None)
    revoke_session(raw_token)
    if principal is not None:
        recorder.record_console_event(
            verb="logout",
            resource="sessions",
            name=principal.username,
            outcome="applied",
            detail="Signed out; the server-side session was revoked.",
        )
    response = Response(status_code=204)
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


# --------------------------------------------------------------------------- #
# Single sign-on (§12.4) — one pair of routes for four providers
# --------------------------------------------------------------------------- #
#
# Browser-navigation endpoints, which is what makes them different from every
# other route in this API: the caller is a redirect or a form POST, not
# `fetch()`, so a §1.3 error envelope would be rendered as raw JSON in the
# address bar instead of being read by the SPA. Failures therefore redirect back
# to the console with a query string the login page renders, and the codes in it
# are drawn from the same §1.3 vocabulary so there is one set of names rather
# than two.
#
# **One `/start` and one callback per binding, not one pair per provider.**
# OpenID Connect, generic OAuth 2.0, OpenShift and SAML differ in how an
# assertion is obtained and in what makes it trustworthy — all of which lives in
# `app/identity/*.py` — and differ in nothing that happens afterwards. Four
# hand-written route pairs would be four copies of the throttle, the audit calls,
# the account provisioning and the failure redirect, and the first one to lose a
# copy would be invisible from outside: same status, same shape, no failing test,
# and a hole in the audit trail found later by somebody asking who signed in.


#: Largest body accepted at the SAML assertion consumer service. A SAML response
#: is a few kilobytes once base64-encoded; a megabyte of it is somebody probing
#: what the parser does, and the answer should be "refuses it" rather than "finds
#: out". Checked against Content-Length first so an oversized body is refused
#: before it is read.
_MAX_ACS_BODY_BYTES = 1024 * 1024


def _provider_or_404(name: str):
    """The provider module for a URL segment, or a 404.

    A 404 rather than a validation error, and it is checked before anything else
    happens: the failure redirect interpolates the provider name, so an
    unvalidated one would be reflected into a URL this console sends a browser to.
    """
    module = sso.get(name)
    if module is None:
        raise NotFound(
            f"There is no {name!r} single sign-on provider.",
            context={"resource": "auth", "name": name},
        )
    return module


def _callback_url(request: Request, provider) -> str:
    """The callback URL to send the provider, and to send it again on exchange.

    OAuth requires the ``redirect_uri`` on the token exchange to be
    byte-identical to the one on the authorization request, and SAML checks the
    ``Recipient`` in the assertion against the ACS URL — so it is computed once,
    sealed into the handshake cookie, and read back, rather than recomputed at
    the callback where a different forwarded header would produce a different
    string and an ``invalid_grant`` that reads as a credential problem.

    Configured explicitly when the provider's redirect setting is set. Otherwise
    derived from the forwarded host, which is correct behind one well-configured
    ingress and wrong behind anything that rewrites Host — hence the setting.
    """
    configured = provider.configured_callback_url()
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}/api/auth/{provider.NAME}/{provider.CALLBACK_SUFFIX}"


def _sso_failure(next_path: str, code: str, reason: str) -> RedirectResponse:
    """Send the browser back to the console carrying a renderable failure.

    The reason is a slug from a closed set, not a sentence: the login page owns
    the wording, and a message assembled here would be untranslatable and would
    drift from the §1.3 codes the rest of the app branches on.
    """
    logger.info("SSO sign-in failed: %s (%s)", code, reason)
    # Merged into whatever query `next_path` already carries, rather than
    # concatenated. `safe_next_path` permits a query string — the SPA sends
    # `pathname + search`, so a deep link like `/events?type=Warning` is normal —
    # and appending `?auth_error=...` to that produced
    # `/events?type=Warning?auth_error=invalid`, in which `auth_error` is not a
    # parameter at all. `URLSearchParams.get('auth_error')` then returns null,
    # the login page renders no alert, and a failed sign-in looks like nothing
    # happened.
    path, _, existing = next_path.partition("?")
    params = parse_qsl(existing, keep_blank_values=True)
    params += [("auth_error", code), ("auth_reason", reason)]
    return RedirectResponse(url=f"{path}?{urlencode(params)}", status_code=302)


@router.get("/{provider}/start")
def sso_start(provider: str, request: Request, next: str = "/") -> RedirectResponse:
    """Begin a handshake: redirect to the identity provider.

    Everything the sign-in needs on the way back — a state or request id, a nonce
    and PKCE verifier where the flow has them, and the exact callback URL — goes
    into one sealed, short-lived cookie named for this provider. See
    :mod:`app.identity.handshake`, including why SAML's cannot share the others'
    ``SameSite``.
    """
    module = _provider_or_404(provider)
    next_path = handshake_service.safe_next_path(next)
    if not settings.auth_enabled:
        raise Invalid("Application authentication is not enabled on this deployment.")
    module.require_enabled()

    callback_url = _callback_url(request, module)
    try:
        started = module.begin(callback_url=callback_url, next_path=next_path)
    except AdminError as error:
        # Reaching the provider can fail here for the two flows that discover
        # their endpoints (OIDC, OpenShift). Recorded, because a run of these is
        # worth seeing, and redirected rather than raised because the caller is
        # a browser navigation.
        _audit_signin(
            outcome="failed", username="(sso)", method=module.NAME,
            detail="Single sign-on could not be started.",
            error=f"{error.code}: {error.message}",
        )
        return _sso_failure(next_path, error.code, "provider_unreachable")

    response = RedirectResponse(url=started.url, status_code=302)
    response.set_cookie(
        handshake_service.cookie_name(module.NAME),
        handshake_service.seal(started.handshake),
        **handshake_service.cookie_attributes(module.NAME),
    )
    return response


def _complete_sso(
    *,
    module,
    request: Request,
    params: dict,
    provider_error: str | None,
    db: Session,
) -> RedirectResponse:
    """The half of every single sign-on that is the same for all four providers.

    Verification is the provider's; everything from "is this response ours" to
    "issue the session" is here, once. ``params`` is the callback's query string
    for the redirect providers and its form body for SAML — the only shape
    difference between the two bindings, and the reason it is passed in rather
    than read off the request.
    """
    sealed = request.cookies.get(handshake_service.cookie_name(module.NAME))
    pending = handshake_service.unseal(sealed, provider=module.NAME)
    next_path = pending.next_path if pending else "/"

    def _clear(response: RedirectResponse) -> RedirectResponse:
        response.delete_cookie(
            handshake_service.cookie_name(module.NAME),
            path=handshake_service.cookie_path(module.NAME),
        )
        return response

    # ── Nothing above this line writes a record. ─────────────────────────────
    #
    # This route is public (it has to be — SSO is how a session is obtained), so
    # everything before the handshake is verified is reachable by anyone who can
    # reach the console. An audit write on that path is an unauthenticated
    # INSERT into the one table in this schema with no upper bound on rows: a
    # loop over `?error=` would fill the operator's database, and would do it on
    # a deployment that never enabled SSO at all.
    #
    # A callback that does not correspond to a handshake this console started is
    # not a failed sign-in. It is a stray request, and recording it as a refused
    # authentication would also put rows in the trail that no operator's action
    # produced — noise in the table that is supposed to be evidence.
    if not module.enabled():
        return _clear(_sso_failure(next_path, "invalid", "sso_not_configured"))
    if pending is None:
        return _clear(
            _sso_failure(next_path, "invalid", "handshake_missing_or_expired")
        )

    # ── Past here the handshake is ours, so a failure is a real event. ───────
    if provider_error:
        # The provider refused a sign-in we started — a declined consent screen,
        # a user not assigned to the application. Recorded because a run of these
        # against one console is worth being able to see. Bounded, because the
        # value is attacker-influenced and lands in a column.
        _audit_signin(
            outcome="denied", username="(sso)", method=module.NAME,
            detail="The identity provider refused the sign-in.",
            error=str(provider_error)[:200],
        )
        return _clear(_sso_failure(next_path, "permission_denied", "provider_refused"))

    if module.BINDING == "query":
        # `state` is checked here rather than inside the provider because it is a
        # property of the handshake, not of the assertion: the same comparison for
        # all three redirect flows, against the same sealed value. SAML's
        # equivalent — `InResponseTo` — is checked inside the *signed* assertion
        # instead, because a value read from the unsigned wrapper would be one the
        # attacker chose.
        state = params.get("state") or ""
        if not params.get("code") or not secrets.compare_digest(state, pending.state):
            _audit_signin(
                outcome="denied", username="(sso)", method=module.NAME,
                detail="The single sign-on callback did not match a handshake this "
                       "console started.",
            )
            return _clear(_sso_failure(next_path, "invalid", "state_mismatch"))

    try:
        identity = module.complete(handshake=pending, params=params)
    except AdminError as failure:
        # 5xx-shaped failures are the provider's; 4xx-shaped ones are a refusal.
        # Kept apart in the trail because they send an operator to different
        # places, and because only a refusal should ever look like an attack.
        outcome = "failed" if failure.http_status >= 500 else "denied"
        _audit_signin(
            outcome=outcome, username="(sso)", method=module.NAME,
            detail="Single sign-on assertion was not accepted.",
            error=f"{failure.code}: {failure.message}",
        )
        return _clear(_sso_failure(next_path, failure.code, "assertion_rejected"))

    try:
        user = provision_federated_user(
            db,
            source=module.NAME,
            username=identity.username,
            external_id=identity.subject,
            display_name=identity.display_name,
            email=identity.email,
            groups=identity.groups,
            admin_group=module.admin_group(),
        )
    except AdminError as failure:
        _audit_signin(
            outcome="denied", username=_normalized_or_raw(identity.username),
            method=module.NAME,
            detail="A verified single sign-on identity was refused a console account.",
            error=f"{failure.code}: {failure.message}",
        )
        return _clear(_sso_failure(next_path, failure.code, "account_refused"))

    # ADR-0007: the provider's own username and groups ride onto the session here
    # and nowhere else. This is the only place in the tree holding a verified
    # assertion, and a session that did not capture it at this moment can never
    # reconstruct it — which is why `decide` refuses an older session by name
    # rather than guessing a username from the console's account row. They are
    # captured for every provider, not only the two whose sessions may
    # impersonate: which sources qualify is ADR-0007's decision to change, and it
    # cannot be revisited for a session that never carried the values.
    raw_token, session = create_session(
        db, user, idp_username=identity.username, idp_groups=identity.groups,
    )
    _audit_signin(
        outcome="applied", username=user.username, method=module.NAME,
        detail=f"Signed in via {module.NAME} single sign-on with the "
               f"{user.role} role.",
    )
    response = RedirectResponse(url=next_path, status_code=302)
    _set_session_cookie(response, raw_token)
    return _clear(response)


@router.get("/{provider}/callback")
def sso_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Complete a redirect-binding handshake: verify, provision, issue a session.

    Serves OpenID Connect, generic OAuth 2.0 and OpenShift. SAML is not reachable
    here — its assertion arrives on a POST and its path segment is ``acs`` — and a
    request for it is a 404 rather than a redirect, so the path an administrator
    registered with the identity provider and the path this console answers on
    cannot silently differ.
    """
    module = _provider_or_404(provider)
    if module.BINDING != "query":
        raise NotFound(
            f"The {provider!r} provider does not call back on this path.",
            hint=f"It uses /api/auth/{provider}/{module.CALLBACK_SUFFIX}.",
            context={"resource": "auth", "name": provider},
        )
    return _complete_sso(
        module=module,
        request=request,
        params={"code": code, "state": state},
        provider_error=error,
        db=db,
    )


@router.post("/saml/acs")
async def saml_acs(
    request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """The SAML Assertion Consumer Service: a cross-site form POST from the IdP.

    **The one unauthenticated POST in this API**, and the middleware's blanket
    exemption for the sign-in routes was written for GET navigations only, so it
    is worth saying what protects this one. Not a CSRF token — the request comes
    from the identity provider's origin, and a console that demanded a token here
    would be demanding one from a party that has never seen a page of it. What
    protects it is the assertion: a signature this console checks against a
    configured certificate, and an ``InResponseTo`` inside that signed subtree
    which has to equal the ``AuthnRequest`` id sealed in this browser's handshake
    cookie. A forged POST fails the first; a genuine assertion replayed into
    somebody else's browser fails the second.

    ``RelayState`` is neither read nor honoured. The spec allows the identity
    provider to echo it back, which means an attacker who can make the IdP POST
    can choose it — so the post-sign-in destination is taken from the sealed
    cookie instead, where nobody outside this console can have set it.

    The body is parsed here rather than through ``request.form()``. The
    HTTP-POST binding mandates ``application/x-www-form-urlencoded``, so there is
    no multipart case to handle, and Starlette's form parser needs a dependency
    this application otherwise has no use for — one more package in an image that
    holds decryption keys, for a body of two fields. Parsing it directly is also
    what lets the size be bounded *before* the body is read, which matters on the
    one unauthenticated POST in this API.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_ACS_BODY_BYTES:
        raise Invalid(
            "The single sign-on response was too large.",
            context={"provider": saml.NAME},
        )
    raw = await request.body()
    if len(raw) > _MAX_ACS_BODY_BYTES:
        raise Invalid(
            "The single sign-on response was too large.",
            context={"provider": saml.NAME},
        )
    fields = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    return _complete_sso(
        module=saml,
        request=request,
        params={"SAMLResponse": fields.get("SAMLResponse") or ""},
        provider_error=None,
        db=db,
    )


@router.get("/saml/metadata")
def saml_metadata(request: Request) -> Response:
    """This console's SAML service-provider metadata, for the IdP's setup form.

    Public and secret-free: an entityID, an ACS URL and a binding, all of which
    an administrator would otherwise transcribe by hand — and a mistyped ACS URL
    fails later as "the assertion does not match the sign-in this console
    started", a message that names the symptom and not the cause.

    Served only when SAML is actually enabled, so the document can never describe
    an endpoint that would refuse the assertion it invites.
    """
    saml.require_enabled()
    return Response(
        content=saml.metadata_xml(acs_url=_callback_url(request, saml)),
        media_type="application/samlmetadata+xml",
    )


@router.get("/users")
def list_users(_admin=Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(User).order_by(User.username)).all()
    return envelope(row.to_public_dict() for row in rows)


@router.post("/users", status_code=201)
@audited_user_change("create", lambda kwargs: getattr(kwargs.get("body"), "username", None))
def create_user(
    body: UserCreateBody,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    row = create_local_user(
        db,
        username=body.username,
        password=body.password,
        display_name=body.display_name,
        email=body.email,
        role=body.role,
    )
    _audit_user_action("create", row.username, f"Created local console user {row.username}.")
    return row.to_public_dict()


def _load_user(db: Session, user_id: int) -> User:
    row = db.get(User, user_id)
    if row is None:
        raise NotFound(
            f"Console user {user_id} does not exist.",
            context={"resource": "users", "name": str(user_id)},
        )
    return row


@router.put("/users/{user_id}")
@audited_user_change("patch", lambda kwargs: str(kwargs.get("user_id", "")))
def update_user(
    user_id: int,
    body: UserUpdateBody,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    row = _load_user(db, user_id)
    changes = body.model_dump(exclude_unset=True)
    if row.auth_source in FEDERATED_SOURCES and any(
        key in changes for key in ("role", "password")
    ):
        # Every federated source, not just LDAP. The guard was written when LDAP
        # was the only one; leaving it there let an administrator promote an OIDC
        # account and watch the change silently revert at that user's next
        # sign-in, with nothing anywhere explaining why it did not stick.
        raise Invalid(
            f"{row.auth_source.upper()} roles and passwords are managed by the "
            "identity provider and refresh at login.",
            context={"field": "role", "auth_source": row.auth_source},
        )
    removing_admin = row.active and row.role == "admin" and (
        changes.get("active") is False or changes.get("role") == "user"
    )
    if removing_admin and active_admin_count(db) <= 1:
        raise Invalid("The last active administrator cannot be demoted or deactivated.")
    if row.id == admin.id and changes.get("active") is False:
        raise Invalid("You cannot deactivate your own signed-in account.")

    if "display_name" in changes:
        row.display_name = (changes["display_name"] or "").strip() or None
    if "email" in changes:
        row.email = (changes["email"] or "").strip() or None
    if "role" in changes:
        if changes["role"] not in ROLES:
            raise Invalid("Role must be either 'admin' or 'user'.")
        row.role = changes["role"]
    if "active" in changes:
        row.active = changes["active"]
    if changes.get("password"):
        if row.auth_source != "local":
            raise Invalid("Only local users have passwords stored by this console.")
        row.password_hash = hash_password(changes["password"])
        revoke_user_sessions(db, row.id)
    if changes.get("active") is False:
        revoke_user_sessions(db, row.id)
    db.commit()
    db.refresh(row)
    _audit_user_action("patch", row.username, f"Updated console user {row.username}.")
    return row.to_public_dict()


@router.delete("/users/{user_id}", status_code=204)
@audited_user_change("delete", lambda kwargs: str(kwargs.get("user_id", "")))
def deactivate_user(
    user_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    row = _load_user(db, user_id)
    if row.id == admin.id:
        raise Invalid("You cannot deactivate your own signed-in account.")
    if row.active and row.role == "admin" and active_admin_count(db) <= 1:
        raise Invalid("The last active administrator cannot be deactivated.")
    row.active = False
    revoke_user_sessions(db, row.id)
    db.commit()
    _audit_user_action("delete", row.username, f"Deactivated console user {row.username}.")
    return Response(status_code=204)