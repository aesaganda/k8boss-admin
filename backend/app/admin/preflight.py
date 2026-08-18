"""
Access preflight (§9): asking the cluster whether we may, before we try.

Every write goes through :func:`require` on its way into
:func:`app.admin.mutate.mutate`, and the UI calls :func:`check` / :func:`check_many`
to decide which buttons to disable and why (§11.4). Both sit on one Kubernetes
primitive, ``SelfSubjectAccessReview``: we hand the API server the exact
verb/group/resource/namespace/name/subresource and it answers for *the identity
this console is using*, which is the only identity that matters — the console's
ServiceAccount, not the operator sitting in front of it.

**The distinction this module exists to preserve.** A review answers with three
fields, and two of them can both be falsy:

    allowed=False, evaluationError=None   -> a clean denial. You may not.
    allowed=False, evaluationError="..."  -> the review itself failed. We do not
                                             know whether you may.

Collapsing the second into the first tells an operator they are missing a
permission they may well hold, and sends them to edit a ClusterRole that is
already correct. That is the project's defect standard exactly: a wrong answer
delivered confidently. So the two are kept apart in the :class:`dict` this module
returns (``evaluationError`` is a first-class field of every result), in what
:func:`require` raises (``rbac_denied`` versus ``upstream_error``), and therefore
in the HTTP status the caller sees — 403 versus 502.

The same reasoning covers a review we could not even issue: an API server that
is unreachable, or a ServiceAccount that may not create ``SelfSubjectAccessReviews``
at all. :func:`check` reports those as ``allowed: false`` with an
``evaluationError`` describing what failed, so a batch of seventeen checks at
cluster registration still returns seventeen rows; :func:`require` re-raises the
real error, so a write never proceeds on a permission we never established and
never reports a transport failure as a denial.

**Nothing is cached.** A cached ``allowed`` outlives the RBAC change that revoked
it, and a console that says "you may" because it asked five minutes ago is
answering a question about the past with the confidence of the present. One
review is one round trip against an endpoint designed for exactly this.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from app.errors import (
    AdminError,
    ClusterUnreachable,
    Invalid,
    RBACDenied,
    UpstreamError,
    from_api_exception,
)
from app.k8s.client import get_authorization_v1
from app.resources.catalog import normalize_group, wire_group
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: Upper bound on one :func:`check_many` batch. Each check is a round trip to the
#: API server, so an unbounded batch is both a way to hammer someone's control
#: plane through this console and a reliable way to exceed the read deadline —
#: which surfaces as "the cluster is unreachable", a diagnosis about the wrong
#: system. The baseline registration set (§9) is 17.
MAX_BATCH = 64

#: What the RBAC authorizer means when it allows a request without saying why,
#: and what it means when it denies one without saying why. The second is our
#: phrasing, not the API server's: the authorizer returns an empty reason for a
#: plain no-match, and an empty string in the UI reads as "denied, cause unknown"
#: when the cause is in fact known and mundane.
_NO_POLICY_MATCHED = "no RBAC policy matched"


def _target_phrase(group: str, resource: str, subresource: str | None) -> str:
    """``apps/deployments`` or ``core/pods/exec`` — the §1.4 wire spelling.

    Wire spelling in the human-facing strings because that is what the operator
    sees in the URL bar and in the §9 query parameters. The *real* group name
    (the empty string for core) is what goes to the API server, and the two are
    translated in exactly one place: :func:`_resource_attributes`.
    """
    target = f"{wire_group(group)}/{resource}"
    return f"{target}/{subresource}" if subresource else target


def _grant_hint(
    verb: str, group: str, resource: str, namespace: str | None, subresource: str | None
) -> str:
    """The concrete grant that would turn this denial into an allow.

    Named down to the namespace, because "forbidden" costs an operator a
    ClusterRole audit and this costs them one ``kubectl edit``. The parenthetical
    for the core group is not padding: RBAC rules spell it ``apiGroups: [""]``,
    and someone who copies ``core`` into a Role gets a rule that matches nothing
    and no error to say so.
    """
    scope = f" in `{namespace}`" if namespace else " cluster-wide"
    hint = (
        f"Grant `{verb}` on `{_target_phrase(group, resource, subresource)}`{scope} "
        "to the console's ServiceAccount."
    )
    if not normalize_group(group):
        hint += ' The core group is written as `apiGroups: [""]` in an RBAC rule.'
    return hint


def _result(
    *,
    verb: str,
    group: str,
    resource: str,
    namespace: str | None,
    name: str | None,
    subresource: str | None,
    allowed: bool,
    reason: str | None,
    evaluation_error: str | None,
    hint: str | None,
) -> dict[str, Any]:
    """One §9 ``PreflightResult``.

    ``group`` is echoed in the §1.4 **wire** spelling, so a batch response can be
    matched against the batch request key-for-key — §9's own vocabulary is the
    wire one ("``group`` (`core` for the core group)"), and a result that came
    back with ``""`` where the caller sent ``core`` cannot be joined to the check
    that produced it without every consumer re-implementing the translation.

    ``name`` and ``subresource`` are echoed too, beyond the fields §9 lists. A
    result for ``create core/pods/exec`` is otherwise indistinguishable from one
    for ``create core/pods``, and those are different permissions.
    """
    return {
        "verb": verb,
        "group": wire_group(group),
        "resource": resource,
        "namespace": namespace,
        "name": name,
        "subresource": subresource,
        "allowed": allowed,
        "reason": reason,
        "evaluationError": evaluation_error,
        "hint": hint,
    }


def _resource_attributes(
    verb: str, group: str, resource: str, namespace: str | None,
    name: str | None, subresource: str | None,
) -> k8s_client.V1ResourceAttributes:
    """The review's subject, with the group in the API server's own spelling.

    ``group=""`` is the core group and is what the authorizer matches against.
    Sending the literal string ``core`` here would produce a review of a group
    that does not exist — which answers ``allowed: false`` for every core
    resource, cleanly and wrongly.
    """
    return k8s_client.V1ResourceAttributes(
        group=normalize_group(group),
        resource=resource,
        verb=verb,
        namespace=namespace or None,
        name=name or None,
        subresource=subresource or None,
    )


def _review(
    verb: str,
    group: str,
    resource: str,
    *,
    namespace: str | None = None,
    name: str | None = None,
    subresource: str | None = None,
) -> tuple[dict[str, Any], AdminError | None]:
    """Issue one review. Returns ``(result, error_if_undecided)``.

    ``error`` is ``None`` whenever the cluster gave us an answer — including a
    denial, which is an answer. It is non-``None`` in exactly the cases where we
    do **not** know whether the caller may act: the review could not be issued,
    or the authorizer reported an evaluation error while denying.

    Splitting it this way rather than raising from one function and returning
    from another is what keeps :func:`check` and :func:`require` from drifting:
    they are two renderings of this one result, so a batch check and a write can
    never disagree about what the cluster said.
    """
    normalized = normalize_group(group)
    context = {
        "verb": verb, "group": normalized, "resource": resource,
        "namespace": namespace, "name": name, "subresource": subresource,
    }
    body = k8s_client.V1SelfSubjectAccessReview(
        spec=k8s_client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=_resource_attributes(
                verb, group, resource, namespace, name, subresource
            )
        )
    )

    try:
        review = get_authorization_v1().create_self_subject_access_review(body)
    except ApiException as e:
        # Mapped against the *review's* own target, not the caller's: what the
        # API server refused was the creation of a SelfSubjectAccessReview, and
        # an error saying "cannot patch apps/deployments" would name a permission
        # nobody has established anything about.
        mapped = from_api_exception(
            e,
            context={
                **context, "verb": "create", "group": "authorization.k8s.io",
                "resource": "selfsubjectaccessreviews", "requested": context["verb"],
            },
        )
        # Reported as an unknown, never as a denial of the requested verb. The
        # likeliest cause is that the console's identity may not create
        # SelfSubjectAccessReviews — normally granted to system:authenticated via
        # system:basic-user, so someone has removed it — and relaying that as
        # "you cannot patch Deployments" would send the operator to fix a
        # ClusterRole that is already correct.
        error: AdminError = mapped if isinstance(mapped, ClusterUnreachable) else UpstreamError(
            "Could not check whether the console may "
            f"{verb} {_target_phrase(group, resource, subresource)}: the access "
            "review itself was refused.",
            detail=mapped.detail or mapped.message,
            hint=(
                "The console's identity needs `create` on "
                "`authorization.k8s.io/selfsubjectaccessreviews`, which the built-in "
                "system:basic-user ClusterRole grants to every authenticated user. "
                "Until it has that, no permission can be checked and no write can proceed."
            ),
            context={**context, "reviewResource": "selfsubjectaccessreviews"},
        )
        return _undecided_result(
            verb, group, resource, namespace, name, subresource,
            evaluation_error=(
                f"The access review could not be issued: {mapped.message}"
                + (f" ({mapped.detail})" if mapped.detail else "")
            ),
        ), error
    except AdminError as e:
        # ClusterUnreachable from app.k8s.client's transport wrapper: DNS, TLS or
        # a deadline. Same reasoning as above.
        return _undecided_result(
            verb, group, resource, namespace, name, subresource,
            evaluation_error=f"The access review could not be issued: {e.message}",
        ), e

    allowed = bool(get_field(review, "status", "allowed", default=False))
    denied = bool(get_field(review, "status", "denied", default=False))
    reason = get_field(review, "status", "reason") or None
    evaluation_error = get_field(review, "status", "evaluationError") or None

    if allowed:
        # An authorizer erroring while another allows is legal, and `allowed` is
        # authoritative when true. The evaluation error is still reported rather
        # than dropped: it is the only evidence that one authorizer in the chain
        # is broken, and dropping it here is how that goes unnoticed until the
        # working authorizer's rule is removed.
        if evaluation_error:
            logger.warning(
                "Access review allowed %s %s but reported an evaluation error: %s",
                verb, _target_phrase(group, resource, subresource), evaluation_error,
            )
        return _result(
            verb=verb, group=group, resource=resource, namespace=namespace, name=name,
            subresource=subresource, allowed=True, reason=reason,
            evaluation_error=evaluation_error, hint=None,
        ), None

    if evaluation_error:
        result = _undecided_result(
            verb, group, resource, namespace, name, subresource,
            evaluation_error=evaluation_error, reason=reason,
        )
        return result, UpstreamError(
            f"The cluster could not decide whether the console may "
            f"{verb} {_target_phrase(group, resource, subresource)}.",
            detail=evaluation_error,
            hint=(
                "This is not a denial: an authorizer failed while evaluating the "
                "request, so the permission is unknown. Check the API server's "
                "authorization webhooks, then retry."
            ),
            context={**context, "evaluationError": evaluation_error},
        )

    return _result(
        verb=verb, group=group, resource=resource, namespace=namespace, name=name,
        subresource=subresource, allowed=False,
        reason=(
            f"explicitly denied by an authorizer: {reason}" if denied and reason
            else "explicitly denied by an authorizer" if denied
            else reason or _NO_POLICY_MATCHED
        ),
        evaluation_error=None,
        hint=_grant_hint(verb, group, resource, namespace, subresource),
    ), None


def _undecided_result(
    verb: str, group: str, resource: str, namespace: str | None,
    name: str | None, subresource: str | None, *,
    evaluation_error: str, reason: str | None = None,
) -> dict[str, Any]:
    """A result for "we could not find out", never for "no".

    ``allowed: false`` because the console must not act on it, and a non-null
    ``evaluationError`` because the UI renders that state differently (§9) —
    greyed with "permission unknown", not greyed with "you lack this grant".
    """
    return _result(
        verb=verb, group=group, resource=resource, namespace=namespace, name=name,
        subresource=subresource, allowed=False, reason=reason,
        evaluation_error=evaluation_error,
        hint=(
            "The permission is unknown, not missing: the access review itself "
            "failed. Retry, and check the API server's authorization chain if it "
            "keeps failing."
        ),
    )


def check(
    verb: str,
    group: str,
    resource: str,
    *,
    namespace: str | None = None,
    name: str | None = None,
    subresource: str | None = None,
) -> dict[str, Any]:
    """One §9 ``PreflightResult``. Never raises for a cluster-side failure.

    Used by the UI to decide whether a button is enabled, and by cluster
    registration to report the baseline verb set. Both want a row per question
    asked, including for the questions that could not be answered — an exception
    here would collapse a seventeen-row permissions report into one error and
    hide the sixteen answers we did have.
    """
    result, _error = _review(
        verb, group, resource, namespace=namespace, name=name, subresource=subresource
    )
    return result


def require(
    verb: str,
    group: str,
    resource: str,
    *,
    namespace: str | None = None,
    name: str | None = None,
    subresource: str | None = None,
) -> None:
    """Preflight a write. Returns ``None`` if permitted; raises otherwise.

    Raises:
        RBACDenied: a clean denial — 403 ``rbac_denied``, whose ``hint`` names
            the exact grant that would fix it.
        AdminError: the review could not be issued or the authorizer errored.
            Deliberately **not** ``RBACDenied``: the caller may well hold the
            permission, and reporting "you are not allowed" is the confidently
            wrong answer this module is built to avoid. It reaches the API as
            502 ``upstream_error`` (or the transport's own code), which the UI
            renders as "could not check" rather than "not permitted".
    """
    result, error = _review(
        verb, group, resource, namespace=namespace, name=name, subresource=subresource
    )
    if error is not None:
        logger.warning(
            "Preflight for %s %s could not be decided: %s",
            verb, _target_phrase(group, resource, subresource), result["evaluationError"],
        )
        raise error
    if result["allowed"]:
        return

    logger.info(
        "Preflight denied %s %s%s: %s",
        verb, _target_phrase(group, resource, subresource),
        f" in {namespace}" if namespace else "", result["reason"],
    )
    raise RBACDenied(
        f"Cannot {verb} {_target_phrase(group, resource, subresource)}"
        + (f' in namespace "{namespace}"' if namespace else "")
        + ".",
        detail=result["reason"],
        hint=result["hint"],
        context={
            "verb": verb,
            "group": normalize_group(group),
            "resource": resource,
            "namespace": namespace,
            "name": name,
            "subresource": subresource,
        },
    )


def check_many(checks: list[dict]) -> list[dict[str, Any]]:
    """Run a batch of checks, returning one result per check, in order.

    Order is part of the contract: the caller (cluster registration, a page
    deciding which of its buttons to enable) pairs results with the checks it
    sent by index. A shorter list, or a reordered one, silently mis-attributes a
    permission to the wrong button.

    A malformed check is rejected rather than skipped, and the error names its
    index — skipping it would shift every later result by one, which is the
    mis-attribution above with no error to explain it.
    """
    if not isinstance(checks, list):
        raise Invalid(
            "`checks` must be a list of preflight checks.",
            context={"parameter": "checks"},
        )
    if len(checks) > MAX_BATCH:
        raise Invalid(
            f"Too many preflight checks in one request: {len(checks)} (limit {MAX_BATCH}).",
            hint=(
                "Each check is a separate call to the cluster's authorization "
                f"endpoint. Split the batch into groups of {MAX_BATCH} or fewer."
            ),
            context={"parameter": "checks", "count": len(checks), "limit": MAX_BATCH},
        )

    results: list[dict[str, Any]] = []
    for index, raw in enumerate(checks):
        if not isinstance(raw, dict):
            raise Invalid(
                f"Preflight check {index} is not an object.",
                context={"parameter": f"checks[{index}]"},
            )
        verb = (raw.get("verb") or "").strip()
        resource = (raw.get("resource") or "").strip()
        if not verb or not resource:
            raise Invalid(
                f"Preflight check {index} needs both `verb` and `resource`.",
                hint='For example: {"verb": "list", "group": "apps", "resource": "deployments"}.',
                context={"parameter": f"checks[{index}]", "verb": verb or None,
                         "resource": resource or None},
            )
        results.append(check(
            verb,
            raw.get("group") or "",
            resource,
            namespace=raw.get("namespace") or None,
            name=raw.get("name") or None,
            subresource=raw.get("subresource") or None,
        ))
    return results


__all__ = ["MAX_BATCH", "check", "check_many", "require"]
