"""
The error vocabulary. One exception class per stable ``error`` code in the API
contract (§1.3), and one function that maps a Kubernetes ``ApiException`` onto
them.

Why a hierarchy rather than ``HTTPException(status, detail)`` everywhere: the
frontend *branches* on ``error``. It disables a button on ``rbac_denied``, offers
a refresh-and-retry on ``conflict``, renders "not present on this cluster" on
``unsupported``, and shows a global read-only banner on ``mutations_disabled``.
A free-text detail string cannot carry that, and a 500 carries nothing at all.

The failure this replaces, verbatim from k8boss: an unreachable cluster raised
``urllib3.MaxRetryError`` straight through the kubernetes client, nothing caught
it, and Starlette's ServerErrorMiddleware produced a 500 *outside* CORSMiddleware
— so the browser reported ``TypeError: Failed to fetch`` and the real reason
never reached the operator. Every error a route can produce has a class here, and
``app.api.exception_handlers`` renders all of them inside the CORS layer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


class AdminError(Exception):
    """Base for every error this API reports with a stable code.

    Subclasses set ``code`` and ``http_status``; instances carry a human
    ``message``, an optional verbatim upstream ``detail``, an actionable
    ``hint``, and a machine-readable ``context`` (the verb/group/resource that
    was being attempted). ``context`` is what lets the UI say *which* button to
    disable rather than greying out the whole page.
    """

    code: str = "upstream_error"
    http_status: int = 502
    default_message: str = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        hint: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.detail = detail
        self.hint = hint
        # A fresh dict per instance: a class-level default would be shared by
        # every error raised in the process and would leak one request's target
        # into another's response.
        self.context: dict[str, Any] = dict(context or {})
        super().__init__(self.message)

    def to_envelope(self) -> dict[str, Any]:
        """The §1.3 body. Keys are always present; values may be null.

        Always-present keys rather than omitting nulls: the frontend reads
        ``body.hint`` unconditionally, and an absent key versus a null one is a
        distinction that has never once been useful and has repeatedly been a
        ``TypeError`` in the browser.
        """
        return {
            "error": self.code,
            "message": self.message,
            "detail": self.detail,
            "hint": self.hint,
            "context": self.context,
        }

    @property
    def unavailable_reason(self) -> str:
        """This error expressed as an §1.2 ``unavailable[].reason`` token.

        Collection endpoints do not fail when one of several reads fails — they
        record the failure and keep going. That record has a small closed
        vocabulary (``forbidden``, ``not_found``, ``unreachable``, ``timeout``,
        ``not_registered``, ``unsupported``), and this is the single place the
        two vocabularies are joined, so a new error class cannot quietly start
        rendering as the catch-all.
        """
        return _UNAVAILABLE_REASON.get(self.code, "unreachable")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.code} {self.http_status} {self.message!r}>"


class NoClusterSelected(AdminError):
    """No ``cluster_id`` was given and no cluster is registered to fall back to.

    409 rather than 400: the request is well-formed, the *server* has nothing to
    answer it about. The UI turns this into "register a cluster", which is a
    setup action, not a request correction.
    """

    code = "no_cluster_selected"
    http_status = 409
    default_message = (
        "No cluster is selected. Register a cluster, then pass ?cluster_id= or "
        "select one in the cluster switcher."
    )


class ClusterUnreachable(AdminError):
    """DNS, TCP, TLS or deadline failure reaching the API server.

    Distinct from ``upstream_error`` on purpose: this one means we never got an
    answer, so nothing about the cluster's contents can be inferred from it. An
    empty page here is "we could not look", never "there is nothing there".
    """

    code = "cluster_unreachable"
    http_status = 502
    default_message = "The cluster API server could not be reached."


class RBACDenied(AdminError):
    """Preflight said no, or the API server said no.

    Never a bare relayed "forbidden": ``context`` names the exact
    verb/group/resource/namespace and ``hint`` names the grant that would fix it,
    because an operator reading "forbidden" has to go and guess which of the
    fifteen permissions in the request was the missing one.
    """

    code = "rbac_denied"
    http_status = 403
    default_message = "The console's credentials are not permitted to do that."


class NotFound(AdminError):
    """The object, or the API resource itself, does not exist."""

    code = "not_found"
    http_status = 404
    default_message = "Not found."


class Conflict(AdminError):
    """``resourceVersion`` mismatch, or the API server rejected a conflicting write.

    Always carries ``context.currentResourceVersion`` when it came from an
    optimistic-concurrency check, so the UI can re-diff against live rather than
    asking the user to retype their edit.
    """

    code = "conflict"
    http_status = 409
    default_message = (
        "The object changed since it was loaded. Reload it and reapply the change."
    )


class Invalid(AdminError):
    """Submitted content failed schema, admission or request validation."""

    code = "invalid"
    http_status = 422
    default_message = "The request was not valid."


class AuthenticationRequired(AdminError):
    """The console requires a valid application session for this request."""

    code = "authentication_required"
    http_status = 401
    default_message = "Sign in to continue."


class InvalidCredentials(AdminError):
    """A login failed without revealing whether the username exists."""

    code = "invalid_credentials"
    http_status = 401
    default_message = "The username or password was not accepted."


class PermissionDenied(AdminError):
    """The authenticated console user lacks an application-level role."""

    code = "permission_denied"
    http_status = 403
    default_message = "Your console role does not permit that action."


class TooManyAttempts(AdminError):
    """The caller has exhausted the sign-in budget for the current window.

    Its own code rather than ``invalid_credentials``, because the frontend has to
    behave differently: an operator who mistyped a password should try again, and
    an operator who is being told to wait should be shown for how long instead of
    being invited to keep guessing. A shared code makes the login form
    indistinguishable in the two cases and trains people to hammer it.

    429 rather than 403: the request was well-formed and may well be permitted
    later, which is exactly what 429 means. ``context.retryAfterSeconds`` carries
    the wait.
    """

    code = "too_many_attempts"
    http_status = 429
    default_message = "Too many sign-in attempts. Wait a moment before trying again."


class IdentityProviderUnavailable(AdminError):
    """LDAP could not answer, which is distinct from rejecting credentials."""

    code = "identity_provider_unavailable"
    http_status = 502
    default_message = "The configured identity provider could not be reached."


class MutationsDisabled(AdminError):
    """``ADMIN_ALLOW_MUTATIONS`` is false and the request was a write.

    403 with its own code rather than ``rbac_denied``: the operator's permissions
    are irrelevant here, the *deployment* is read-only, and telling someone they
    lack an RBAC grant they actually hold sends them to fix the wrong system.
    """

    code = "mutations_disabled"
    http_status = 403
    default_message = (
        "This console is running read-only. Writes are disabled by "
        "ADMIN_ALLOW_MUTATIONS; dry-run is still available."
    )


class Unsupported(AdminError):
    """This cluster does not serve that API resource.

    501, and *not* an error condition in the UI — no Ingress controller CRDs and
    no metrics.k8s.io are ordinary facts about a cluster. Rendering it red would
    train operators to ignore red.
    """

    code = "unsupported"
    http_status = 501
    default_message = "This cluster does not serve that API resource."


class UpstreamError(AdminError):
    """Any other API server failure: 5xx, 401, throttling, unparseable answer."""

    code = "upstream_error"
    http_status = 502
    default_message = "The cluster API server returned an error."


# Error code -> §1.2 unavailable reason. Kept next to the classes so adding a
# class without deciding how a partial read reports it is visible here.
_UNAVAILABLE_REASON: dict[str, str] = {
    "rbac_denied": "forbidden",
    "not_found": "not_found",
    "cluster_unreachable": "unreachable",
    "no_cluster_selected": "not_registered",
    "unsupported": "unsupported",
    "mutations_disabled": "forbidden",
    "conflict": "unreachable",
    "invalid": "unreachable",
    "authentication_required": "forbidden",
    "invalid_credentials": "forbidden",
    "permission_denied": "forbidden",
    "identity_provider_unavailable": "unreachable",
    "too_many_attempts": "unreachable",
    "upstream_error": "unreachable",
}


def _describe(context: dict[str, Any] | None) -> str:
    """Render a target as an English phrase: ``patch deployments in namespace "prod"``.

    Used for messages only. The machine-readable form is ``context`` itself; the
    contract is explicit that ``detail`` and ``message`` must never be parsed.
    """
    ctx = context or {}
    verb = ctx.get("verb")
    group = ctx.get("group")
    resource = ctx.get("resource")
    namespace = ctx.get("namespace")
    name = ctx.get("name")

    qualified = resource or "the resource"
    if group:
        qualified = f"{group}/{qualified}"
    parts = [p for p in (verb, qualified) if p]
    phrase = " ".join(parts) if parts else "that request"
    if name:
        phrase += f' "{name}"'
    if namespace:
        phrase += f' in namespace "{namespace}"'
    return phrase


def _rbac_hint(context: dict[str, Any] | None) -> str:
    """The grant that would fix an ``rbac_denied``, phrased as a sentence."""
    ctx = context or {}
    verb = ctx.get("verb") or "access"
    resource = ctx.get("resource") or "the resource"
    group = ctx.get("group")
    subresource = ctx.get("subresource")
    target = f"{group}/{resource}" if group else resource
    if subresource:
        target = f"{target}/{subresource}"
    scope = f" in `{ctx['namespace']}`" if ctx.get("namespace") else " cluster-wide"
    return (
        f"Grant `{verb}` on `{target}`{scope} to the console's ServiceAccount, "
        "then re-run the connection test."
    )


#: Substring identifying a Pod Security admission refusal in the API server's
#: message. Matched on the message rather than on ``Status.reason``, which is
#: ``Forbidden`` for this and for an ordinary RBAC denial alike — which is the
#: whole problem :func:`podsecurity_hint` exists to solve.
_PODSECURITY_MARKER = "violates podsecurity"


def podsecurity_hint(
    error: AdminError, *, namespace: str | None, setting: str | None = None
) -> AdminError:
    """Rewrite an ``rbac_denied`` hint when Pod Security admission is the cause.

    **The code is right and the advice it implies is wrong**, which is the only
    situation worth a special case like this one. Pod Security admission refuses
    with HTTP 403, so :func:`from_api_exception` maps it to ``rbac_denied`` —
    correctly, because this module maps by status and not by ``Status.reason``,
    which is ``Forbidden`` here exactly as it is for a genuine RBAC denial, and
    which is the reason that mapping exists at all.

    But the default hint then tells the operator to grant a verb, and no
    ClusterRole they can write will ever admit the pod: what refused it is a
    **label on the namespace**. Left alone this sends someone to argue with a
    cluster admin about a permission they already hold — the exact
    wrong-system misdirection ``rbac_denied`` exists to prevent.

    Only the hint changes. The code, the status and the API server's own detail
    are left exactly as they were, so a client branching on ``error`` behaves
    identically. That is the same treatment §14 gives RBAC escalation
    prevention, and for the same reason.

    Args:
        namespace: the namespace whose enforce label refused the pod.
        setting: the environment variable that chooses that namespace, when the
            caller has one. Omitted for writes with no namespace knob of their
            own — the YAML editor's create, where the operator picked the
            namespace themselves and the sentence about relabelling is the only
            useful half.

    Returns the error, mutated, so callers can ``raise podsecurity_hint(e, ...)``.

    Discovered against a real cluster rather than in a test: a ``restricted``
    namespace answers with ``violates PodSecurity "restricted:latest":
    runAsNonRoot != true``, and no fake client would have said so.
    """
    if error.code != "rbac_denied":
        return error
    if _PODSECURITY_MARKER not in str(error.detail or "").lower():
        return error

    where = f"The namespace {namespace!r}" if namespace else "That namespace"
    fix = (
        f"Point {setting} at a namespace whose level admits this pod, or relax "
        "the label on this one."
        if setting
        else "Create it in a namespace whose level admits it, or relax the label."
    )
    error.hint = (
        f"This is Pod Security admission, not a missing permission. {where} "
        "carries a `pod-security.kubernetes.io/enforce` label whose level this "
        f"pod does not satisfy, and no RBAC grant changes that. {fix} The "
        "message above names the exact field admission objected to."
    )
    return error


def _api_exception_detail(exc: ApiException) -> str | None:
    """Pull the API server's own ``Status.message`` out of an ApiException body.

    ``str(ApiException)`` is a multi-line dump that includes the response headers
    and the whole JSON body — unreadable in a toast and a small information leak
    into logs. The ``message`` field is the sentence a human wants; when the body
    is not the expected Status JSON (an HTML error page from a proxy in front of
    the API server is the usual case) the raw body is returned truncated, because
    "we got something back that was not Kubernetes" is itself the diagnosis.
    """
    body = getattr(exc, "body", None)
    if not body:
        return (exc.reason or "").strip() or None
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - decode with errors= cannot raise
            return None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        text = str(body).strip()
        return (text[:512] + "...") if len(text) > 512 else (text or None)
    if isinstance(parsed, dict):
        message = parsed.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        reason = parsed.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


def from_api_exception(exc: ApiException, *, context: dict[str, Any]) -> AdminError:
    """Map a Kubernetes ``ApiException`` to the console's error vocabulary.

    Mapped by HTTP status, not by ``Status.reason``: the reason string is not
    stable across API server versions or admission webhooks, while the status
    code is part of the Kubernetes API contract. ``context`` is required (not
    optional with a default) so no call site can produce an ``rbac_denied`` that
    fails to say what was denied — that answer is useless, and a useless answer
    delivered confidently is the failure mode this project is built against.
    """
    status = getattr(exc, "status", None)
    detail = _api_exception_detail(exc)
    what = _describe(context)

    if status in (None, 0):
        # The kubernetes client raises ApiException(status=0) for a TLS failure —
        # no HTTP exchange happened at all, so nothing about the cluster's
        # contents can be inferred from it. That is cluster_unreachable, not
        # upstream_error: the difference is whether the operator should go and
        # look at their certificates or at their API server's logs.
        return ClusterUnreachable(
            "The cluster API server could not be reached.",
            detail=detail,
            hint=(
                "No HTTP response was received — usually TLS verification. Check "
                "that the stored CA certificate matches the API server."
            ),
            context={**context, "cause": "unreachable"},
        )
    if status == 400:
        return Invalid(
            f"The cluster rejected {what} as malformed.",
            detail=detail, hint="Check the submitted object against the resource's schema.",
            context=context,
        )
    if status == 401:
        # NOT cluster_unreachable: the cluster answered, it just refused us. A
        # rotated or expired ServiceAccount token lands here, and telling the
        # operator the cluster is down sends them to check the wrong thing.
        return UpstreamError(
            "The cluster rejected the console's credentials.",
            detail=detail,
            hint=(
                "The stored token is invalid or has expired. Update the cluster's "
                "token in Settings and re-run the connection test."
            ),
            context=context,
        )
    if status == 403:
        return RBACDenied(
            f"Cannot {what}.", detail=detail, hint=_rbac_hint(context), context=context,
        )
    if status == 404:
        return NotFound(f"No such object: {what}.", detail=detail, context=context)
    if status == 405:
        return Unsupported(
            f"The cluster does not allow {what}.",
            detail=detail,
            hint="The resource does not support this verb on this cluster.",
            context=context,
        )
    if status == 409:
        return Conflict(
            f"The cluster reported a conflict on {what}.", detail=detail, context=context,
        )
    if status == 410:
        # Expired list cursor. The caller can recover by restarting the listing,
        # which no other status implies -- worth its own hint rather than being
        # folded into a generic 5xx that reads as "the cluster is broken".
        return Invalid(
            "The list cursor expired before the listing finished.",
            detail=detail,
            hint="Restart the listing without a `continue` token.",
            context=context,
        )
    if status in (415, 422):
        return Invalid(
            f"The cluster rejected {what} as invalid.",
            detail=detail,
            hint="Admission or schema validation refused the object; the detail is verbatim.",
            context=context,
        )
    if status == 429:
        return UpstreamError(
            "The cluster API server is throttling this client.",
            detail=detail, hint="Retry shortly; the API server applied a rate limit.",
            context=context,
        )
    if status == 501:
        return Unsupported(
            f"This cluster does not implement {what}.", detail=detail, context=context,
        )
    if status in (502, 503, 504):
        return UpstreamError(
            "The cluster API server is unavailable.",
            detail=detail,
            hint="An aggregated APIService or the API server itself is down. Retry shortly.",
            context=context,
        )

    # 500, and anything unrecognised. Reported as upstream_error rather than
    # re-raised: a 500 from this process would tell the operator that the console
    # is broken, when what actually happened is that the cluster is. The upstream
    # status is kept in context so the real code is never lost.
    return UpstreamError(
        f"The cluster API server failed on {what}.",
        detail=detail or (exc.reason or None),
        context={**context, "upstream_status": status},
    )
