"""
ADR-0007 — acting as the operator instead of as the console's ServiceAccount.

Every other call this console makes is made as one credential per cluster, and
three consequences follow that are documented rather than hidden: the preflight
answers about the console, the audit trail's actor is a name only this
application can vouch for, and §17 has to *ask* who to bind because it does not
know who is asking. `docs/adr-0007-impersonation.md` records that decision and
the conditions under which this exception may exist. This module is that
exception, and every condition in the ADR is enforced here rather than
distributed across call sites.

**What impersonation is.** Kubernetes' own answer: `Impersonate-User` and
`Impersonate-Group` request headers. The API server authenticates the
ServiceAccount, checks it holds `impersonate` on the requested subject, and then
evaluates the whole request — authorization, admission and **its own audit
record** — as the impersonated user. It is not a partial fix; the cluster's own
log names both parties, which is a stronger claim than ADR-0003 can make about a
table this application owns.

**Why it refuses rather than falls back.** A read that quietly reverted to the
ServiceAccount would show an operator data their own RBAC forbids, which is this
project's defect standard with a security consequence attached. So there is no
path through this module that returns "no headers" for a cluster that asked for
impersonation: it either produces headers or raises.

The four refusals, each an ADR condition:

* **The cluster did not ask.** `Cluster.impersonation_enabled` is off by
  default, per-cluster, and surfaced in every `ClusterPublic` response beside
  `skip_tls_verify` — a setting that can never be silently in effect. Nothing
  is refused here; the call is made as the ServiceAccount, which is the
  documented behaviour of every other cluster in the deployment.
* **The session is not from a source the cluster could have derived itself.**
  See :data:`IMPERSONATION_SOURCES`. A local or LDAP session cannot impersonate,
  because if this console's own password table could cause a request to arrive
  at the API server as `alice`, that table has become an identity provider the
  cluster trusts. Nothing in the API server checks that the name in
  `Impersonate-User` belongs to anyone real; it checks only that the
  impersonator may say it.
* **The groups claim was absent.** `identity_from_claims` keeps *absent* and
  *empty* apart on the grounds that an issuer which did not tell us must not
  demote anybody. Under impersonation that distinction stops being about console
  roles and becomes load-bearing on the cluster: impersonating with an empty
  group list, when the issuer merely omitted the claim, silently strips every
  group-derived permission the operator holds and produces a page full of
  correctly-reported denials for permissions they have. Empty is fine. Absent
  is not.
* **Application authentication is off.** Legacy proxy mode's actor is the
  advisory `X-K8Boss-User` header, which is caller-controlled. Impersonating
  from it would let anyone who can reach the port choose a cluster identity.

**What is deliberately not sent.** `Impersonate-Uid` and `Impersonate-Extra-*`
exist and are omitted: this console has no issuer-stated value for either, and
inventing one is the thing the ADR rejects when it refuses to send the console's
own opinion about somebody's groups to an API server.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import Any

from urllib3 import HTTPHeaderDict

from app.config import settings
from app.errors import ImpersonationUnavailable

logger = logging.getLogger(__name__)

USER_HEADER = "Impersonate-User"
GROUP_HEADER = "Impersonate-Group"

#: The group the API server adds to every request it authenticates itself, and
#: does **not** add to an impersonated one.
#:
#: This is the one group sent that the issuer did not state, and it is not the
#: exception the ADR rejects. That rejection is about `roles.py` — the console's
#: *opinion*, its mapping of a group to its own `admin` role, which must never
#: reach an authorization decision. `system:authenticated` is nobody's opinion:
#: it is what the API server itself would have attached had the operator
#: presented the same token directly, and the whole claim this feature makes is
#: that the console asserts an identity the cluster would have derived itself.
#:
#: Omitting it is the tempting alternative and it is the failure the ADR warns
#: about in a different sentence: an impersonated request carrying only the
#: issuer's groups loses every grant bound to `system:authenticated` — including
#: the discovery rules on a default cluster — so the operator is shown accurate
#: denials for permissions they demonstrably hold. The reliable end of that is a
#: ClusterRole widened to fix a problem that was never RBAC.
AUTHENTICATED_GROUP = "system:authenticated"

#: The console sign-in methods whose sessions may act as a cluster identity.
#:
#: The console offers six ways in — local, LDAP, OIDC, generic OAuth 2.0,
#: OpenShift's built-in OAuth server, and SAML — and this is the line between
#: them, so it is worth stating what the line actually is. It is **not** "did an
#: external system authenticate them": four of the six qualify on that reading.
#: It is *ADR-0007's* question: **would the API server have derived this same
#: username and group list had the operator presented their own credential to
#: it?** Only then is the console asserting an identity rather than inventing
#: one, and inventing one is what the ADR refuses.
#:
#: * ``oidc`` qualifies because a Kubernetes API server can be pointed at the
#:   very same issuer with ``--oidc-issuer-url``, and then `Impersonate-User`
#:   carries the name that issuer's own token would have produced. Whether a
#:   given deployment did point it there is the operator's to get right, and
#:   getting it wrong produces denials for an identity nobody holds — visible,
#:   and not a silent grant.
#: * ``openshift`` qualifies more strongly than anything else here: the username
#:   and groups are read from the cluster's own ``User`` object, by the cluster,
#:   in response to a token the cluster minted. There is no second system whose
#:   opinion has to line up with the API server's, because there is no second
#:   system.
#:
#: The three excluded sources are excluded for reasons that differ:
#:
#: * ``local`` and ``ldap`` — the password table and the directory bind are this
#:   console's own; see the bullet above.
#: * ``oauth`` and ``saml`` — the provider genuinely authenticated somebody, and
#:   asserted it in a vocabulary no API server consumes. A bare OAuth 2.0
#:   userinfo field and a SAML NameID are names *this console chose the shape
#:   of*; putting one in `Impersonate-User` and calling it the operator's cluster
#:   identity is precisely the invention ADR-0007 rejects when it refuses to send
#:   the console's own opinion about somebody's groups to an API server. An
#:   operator who needs impersonation behind a SAML IdP has a real answer
#:   available — put an OIDC broker (Dex, Keycloak) in front of it and configure
#:   *that* as the issuer for both this console and the API server — which is a
#:   deployment they can verify, rather than a claim only this console makes.
#:
#: Widening this set is an ADR-0007 amendment, not a config change: the whole
#: value of `decide` refusing rather than falling back is that the refusal is
#: reviewed in one place.
IMPERSONATION_SOURCES: frozenset[str] = frozenset({"oidc", "openshift"})


@dataclass(frozen=True)
class Impersonation:
    """The identity one request is made as, and the headers that say so."""

    username: str
    #: As the issuer stated them, plus :data:`AUTHENTICATED_GROUP`. Order is
    #: preserved because it is the issuer's, and deduplicated because an issuer
    #: that already states `system:authenticated` must not have it sent twice.
    groups: tuple[str, ...]

    def apply(self, headers: Any) -> HTTPHeaderDict:
        """Merge the impersonation headers into ``headers``.

        Returns an :class:`urllib3.HTTPHeaderDict` rather than a plain ``dict``
        for one reason that is invisible until it is wrong: **a user's groups
        are repeated headers, not one comma-joined header.** Go's
        ``http.Header`` does not split a comma-joined value, so
        ``Impersonate-Group: platform-admins,oncall`` reaches the authorizer as
        a single group of that literal name — a group nobody is in. The request
        then succeeds, is evaluated with none of the operator's group
        permissions, and produces a page of denials that look like an RBAC
        problem on the cluster.

        ``dict(HTTPHeaderDict)`` re-joins them, so anything downstream that
        normalises these headers back into a plain dict reintroduces exactly
        that bug. `tests/test_impersonation.py` asserts the repeated form.
        """
        merged = HTTPHeaderDict(headers or {})
        merged[USER_HEADER] = self.username
        for group in self.groups:
            merged.add(GROUP_HEADER, group)
        return merged


@dataclass(frozen=True)
class Decision:
    """Whether this request is impersonated, and whose permissions apply.

    ``subject`` is answered in both states, because §9's preflight and §10's
    audit row both have to say *whose* permission was checked. A disabled button
    that cannot say whose permission is missing sends the wrong person to edit a
    ClusterRole.
    """

    impersonation: Impersonation | None
    subject: str
    #: Present when inactive: why this call is the ServiceAccount's. Rendered
    #: nowhere as an error — an ordinary cluster is not misconfigured.
    reason: str | None = None

    @property
    def active(self) -> bool:
        return self.impersonation is not None

    def state(self) -> dict[str, Any]:
        """What an endpoint reports about the identity it answered for."""
        return {
            "impersonated": self.active,
            "subject": self.subject,
            "groups": list(self.impersonation.groups) if self.impersonation else None,
            "detail": self.reason,
        }


#: The subject named when a call is the console's own. Not a username: it is
#: deliberately not a string anybody could mistake for one in an audit trail.
SERVICE_ACCOUNT_SUBJECT = "the console's ServiceAccount"

_SERVICE_ACCOUNT = Decision(
    impersonation=None,
    subject=SERVICE_ACCOUNT_SUBJECT,
    reason="This cluster is not configured for impersonation.",
)

_current: contextvars.ContextVar[Decision | None] = contextvars.ContextVar(
    "current_impersonation", default=None
)

#: Set only by :func:`as_service_account`. Suppresses impersonation for one
#: block, and nothing else in the tree may set it.
_suppressed: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "impersonation_suppressed", default=None
)


class as_service_account:  # noqa: N801 - a context manager used as a verb
    """Make the calls in this block as the console, on a cluster that impersonates.

    **This is the whole of ADR-0007's "documented as one of the named
    ServiceAccount calls".** It exists because a small number of calls cannot be
    impersonated and must not silently be, and the ADR requires that the list be
    a list rather than an emergent property of whoever wrote the last endpoint.
    ``tests/test_impersonation.py`` enumerates every use and fails on a new one,
    so adding a call here is a reviewed act.

    ``why`` is recorded rather than decorative: it is the sentence that has to
    survive into a review of whether the exemption is still justified.
    """

    __slots__ = ("why", "_token")

    def __init__(self, why: str) -> None:
        self.why = why
        self._token = None

    def __enter__(self) -> "as_service_account":
        self._token = _suppressed.set(self.why)
        return self

    def __exit__(self, *exc) -> None:
        _suppressed.reset(self._token)
        return None


def set_current(decision: Decision):
    """Pin the decision for this request. Returns a token for :func:`reset_current`."""
    return _current.set(decision)


def reset_current(token) -> None:
    _current.reset(token)


def get_current() -> Decision:
    """The decision for this request, or the ServiceAccount when none was made.

    Defaulting to the ServiceAccount is safe **only** because nothing reaches an
    impersonating cluster without :func:`decide` having run and raised or
    returned: a request that could not build headers never gets a transport.
    The default covers the calls that legitimately have no cluster row at all —
    the local kubeconfig, the connection test — and those transports refuse
    impersonation structurally, in :func:`app.k8s.client._cluster_api_client`,
    rather than relying on this value.
    """
    return _current.get() or _SERVICE_ACCOUNT


def current_subject() -> str:
    """Whose permissions a preflight or an audit row is about."""
    return get_current().subject


def impersonated_user() -> str | None:
    """The impersonated username for the audit row, or ``None`` for the console."""
    decision = get_current()
    return decision.impersonation.username if decision.impersonation else None


def headers_for_current_request(headers: Any) -> Any:
    """Merge impersonation headers, or return ``headers`` untouched.

    Called from the one transport wrapper in :mod:`app.k8s.client`, so a new
    endpoint cannot forget it and a new client cannot bypass it.
    """
    if _suppressed.get() is not None:
        return headers
    decision = _current.get()
    if decision is None or decision.impersonation is None:
        return headers
    return decision.impersonation.apply(headers)


def _refuse(message: str, hint: str, **context: Any) -> ImpersonationUnavailable:
    return ImpersonationUnavailable(message, hint=hint, context=context)


def decide(cluster: Any, principal: Any) -> Decision:
    """Whether this request impersonates, and as whom. Raises rather than falls back.

    ``principal`` is the verified session identity
    (:class:`app.identity.service.Principal`) or ``None`` when nobody is signed
    in. It is passed rather than read from a contextvar so that this function is
    a pure decision over its two inputs and can be tested without a request.

    Raises:
        ImpersonationUnavailable: the cluster asked for impersonation and this
            session cannot supply it. **403, and deliberately not
            ``rbac_denied``**: the operator's cluster permissions are not the
            problem and telling them otherwise sends them to widen a ClusterRole
            that was already correct — the same distinction
            ``mutations_disabled`` draws for the read-only switch.
    """
    if not getattr(cluster, "impersonation_enabled", False):
        return _SERVICE_ACCOUNT

    if not settings.auth_enabled:
        raise _refuse(
            "This cluster acts as the signed-in operator, and this console has "
            "application authentication turned off.",
            "Enable AUTH_ENABLED with an OpenID Connect provider, or turn "
            "impersonation off for this cluster. The legacy X-K8Boss-User header "
            "is advisory and caller-controlled, so it cannot name a cluster identity.",
            reason="auth_disabled",
        )

    if principal is None:
        raise _refuse(
            "This cluster acts as the signed-in operator, and this request "
            "carries no verified session.",
            "Sign in before using this cluster.",
            reason="no_session",
        )

    source = getattr(principal, "auth_source", None)
    if source not in IMPERSONATION_SOURCES:
        raise _refuse(
            "This cluster acts as the signed-in operator, and only some single "
            "sign-on methods can supply a cluster identity — this one is "
            f"{source or 'unknown'}.",
            "Sign in through the console's OpenID Connect provider, or through "
            "the cluster's own OpenShift OAuth server. A local or LDAP password "
            "cannot be turned into a cluster identity — the cluster trusts its "
            "own issuer, not this console's user table — and a bare OAuth 2.0 "
            "or SAML provider states an identity in a vocabulary no API server "
            "consumes, so the name this console would send would be one it "
            "invented. See docs/adr-0007-impersonation.md.",
            reason="auth_source", auth_source=source,
        )

    username = (getattr(principal, "idp_username", None) or "").strip()
    if not username:
        raise _refuse(
            "This cluster acts as the signed-in operator, and this session does "
            "not carry the username the identity provider stated.",
            "Sign out and back in. Sessions created before impersonation was "
            "available do not carry it, and this console will not guess one.",
            reason="no_idp_username",
        )

    groups = getattr(principal, "idp_groups", None)
    if groups is None:
        raise _refuse(
            "This cluster acts as the signed-in operator, and the identity "
            "provider did not state which groups you are in.",
            "Configure the provider to report group membership — the claim "
            "named by OIDC_GROUPS_CLAIM with the scope that carries it, or the "
            "'user:info' scope on an OpenShift OAuth client. An "
            "absent claim is not an empty one: impersonating with no groups "
            "would strip every group-derived permission you hold and report the "
            "result as a permission you lack.",
            reason="groups_absent",
        )

    return Decision(
        impersonation=Impersonation(username=username, groups=_groups(groups)),
        subject=username,
    )


def _groups(stated: tuple[str, ...]) -> tuple[str, ...]:
    """The issuer's groups plus :data:`AUTHENTICATED_GROUP`, deduplicated, in order."""
    out: list[str] = []
    for group in (*stated, AUTHENTICATED_GROUP):
        value = str(group).strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


__all__ = [
    "AUTHENTICATED_GROUP",
    "Decision",
    "IMPERSONATION_SOURCES",
    "GROUP_HEADER",
    "Impersonation",
    "SERVICE_ACCOUNT_SUBJECT",
    "USER_HEADER",
    "as_service_account",
    "current_subject",
    "decide",
    "get_current",
    "headers_for_current_request",
    "impersonated_user",
    "reset_current",
    "set_current",
]
