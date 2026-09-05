"""
§23 — ask the API server what a *named* subject may do, without becoming them.

**The question this answers, and why it is not §9's.** §9's preflight asks
``SelfSubjectAccessReview``: may *the console's own identity* do this? That is
the question every button needs. It is not the question an administrator has,
which is "what can alice do", "why can this ServiceAccount delete namespaces",
"did removing that binding actually take her access away". Nothing in this
console answered those, and the two ways people reach for them are both worse
than this one:

* **Reading the RBAC objects and computing.** §8 lists Roles, ClusterRoles and
  their bindings, and it is tempting to subtract them into an answer. That
  answer is *derived*, and it is blind to every authorizer that is not RBAC — the
  node authorizer, any webhook authorizer, ABAC where it survives. On a cluster
  with an authorization webhook the derived answer is confidently wrong in both
  directions, and it is wrong about somebody's access.
* **Impersonation.** `docs/adr-0007-impersonation.md` records why every cluster
  call here is made as one ServiceAccount, and what per-user impersonation would
  cost: the console becomes a credential issuer and holds a grant that is
  cluster-admin by proxy. People reach for impersonation largely to answer
  *this* question — and `SubjectAccessReview` answers it without taking a single
  action as anybody. It is a question put to the API server, not an identity
  borrowed.

``SubjectAccessReview`` runs the API server's **whole authorization chain** and
reports what it concluded. That makes it the authoritative answer, and it is why
this module exists rather than a cleverer reading of §8's tables.

**The failure this module is built around: groups.**

A subject's access is mostly not attached to their name. It arrives through
their groups, and the API server only considers the groups *in the review*. A
review for user `alice` with no groups asks "what can a user called alice with
no group memberships do", which is a question nobody has — and it answers
"nothing", cleanly, about somebody who is a cluster administrator through
`platform-admins`.

So the two subject kinds are handled differently, on purpose:

* A **ServiceAccount's** groups are *deterministic*. The API server assigns
  `system:serviceaccounts`, `system:serviceaccounts:<namespace>` and
  `system:authenticated` to every one of them, so this module adds them and the
  answer is complete.
* A **User's** groups come from whatever authenticated them — OIDC claims, a
  certificate's organisation fields, a proxy header — and **no API on this
  cluster reports them**. `system:authenticated` is the only one that can be
  assumed. Everything else must be supplied by the caller, and the response says
  `groups_complete: false` so that no reading of it can become "alice cannot do
  this" when the truthful sentence is "a user called alice, in exactly these
  groups, cannot do this".

**And the failure §9 already names.** ``allowed: false`` with a non-null
``evaluationError`` is not a denial: it means the authorizer could not decide.
Collapsing the two would report somebody as lacking access they hold. §23 keeps
them apart exactly as §9 does, and adds the third field the API actually returns:
``denied``, which is an authorizer saying *no* rather than no authorizer saying
*yes*. Most clusters only ever produce the second.

**Why this lives in `admin/` rather than `services/`.** It is a read — nothing
on the cluster changes — but it is not a *read model*. It needs
:mod:`app.admin.preflight` to establish the console's own right to ask, and
`services/` may never import `app.admin` (there is a test that says so, and it
caught this module in the wrong place). The authorization machinery already
lives here: §9's preflight is its sibling, asks the same API group, and keeps the
same `evaluationError`-is-not-a-denial rule. §23 belongs beside it.

**Why this is audited even though it changes nothing.** It is a privileged read,
like §4's Secret reveal: anyone who can issue these can map the authorization
state of every identity on the cluster. Nothing is exposed that §8's tables do
not already imply, which is why it has no switch of its own — the grant on
`subjectaccessreviews` is the control — but "who asked what the CFO's account
could reach" is a question worth being able to answer afterwards, so one row is
written per review request.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from app.admin import preflight
from app.audit import recorder
from app.errors import AdminError, ClusterUnreachable, Invalid, RBACDenied, UpstreamError
from app.errors import from_api_exception
from app.k8s.client import get_authorization_v1
from app.resources.catalog import normalize_group

logger = logging.getLogger(__name__)

#: The two subject kinds a review can name. `Group` is deliberately absent: the
#: API takes a user and a group *list*, not a group as the subject, and offering
#: "what can this group do" would be this console inventing a question the
#: authorizer cannot be asked.
SUBJECT_KINDS = ("User", "ServiceAccount")

#: Every authenticated request carries this, whoever made it.
AUTHENTICATED_GROUP = "system:authenticated"

#: What the API server assigns to every ServiceAccount, in addition to the above.
SERVICE_ACCOUNT_GROUP = "system:serviceaccounts"

#: The review's own target, for the preflight and the audit row. What is being
#: created is a SubjectAccessReview — not the thing being asked about.
REVIEW_TARGET = {
    "group": "authorization.k8s.io",
    "version": "v1",
    "resource": "subjectaccessreviews",
    "namespace": None,
    "name": None,
}

#: A batch bound. Each check is one round trip to the API server, and a caller
#: asking about two hundred verbs at once is a caller who wants a different
#: feature — see the module docstring on why "who can do X" is not this one.
MAX_CHECKS = 50


def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


# --------------------------------------------------------------------------- #
# The subject
# --------------------------------------------------------------------------- #

def resolve_subject(raw: Any) -> dict[str, Any]:
    """The subject as the API server will see it, with its groups made explicit.

    Returns the username the authorizer matches on, the group list the review
    will carry, and **``groups_complete``** — whether that list is all of them.

    ``groups_complete`` is the field this whole module turns on. For a
    ServiceAccount it is ``True``, because the API server's group assignment is
    deterministic and reproduced here. For a User it is ``False``, always, even
    when the caller supplies groups: no API on this cluster reports a user's real
    memberships, so the honest claim is never "these are alice's groups" but
    "this is the answer for a user called alice in exactly these groups".
    """
    if not isinstance(raw, dict):
        raise _invalid("subject must be an object.", parameter="subject")

    kind = raw.get("kind")
    if kind not in SUBJECT_KINDS:
        raise _invalid(
            f"subject.kind must be one of {', '.join(SUBJECT_KINDS)}.",
            parameter="subject.kind",
            hint=(
                "A group cannot be the subject of a review: the authorizer takes "
                "a user plus a list of groups, so ask about a user who is in the "
                "group instead."
            ),
            value=kind,
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _invalid("subject.name is required.", parameter="subject.name")
    name = name.strip()

    supplied = raw.get("groups", [])
    if not isinstance(supplied, list) or any(not isinstance(g, str) for g in supplied):
        raise _invalid("subject.groups must be a list of strings.", parameter="subject.groups")
    supplied = [g.strip() for g in supplied if g.strip()]

    if kind == "ServiceAccount":
        namespace = raw.get("namespace")
        if not isinstance(namespace, str) or not namespace.strip():
            raise _invalid(
                "subject.namespace is required for a ServiceAccount.",
                parameter="subject.namespace",
                hint=(
                    "A ServiceAccount's identity is "
                    "`system:serviceaccount:<namespace>:<name>` — the namespace is "
                    "part of who it is, not a filter on the question."
                ),
            )
        namespace = namespace.strip()
        username = f"system:serviceaccount:{namespace}:{name}"
        # Reproduced rather than asked for: these are what the API server itself
        # attaches to every ServiceAccount token, so a review without them asks
        # about an identity that cannot exist.
        groups = [
            AUTHENTICATED_GROUP,
            SERVICE_ACCOUNT_GROUP,
            f"{SERVICE_ACCOUNT_GROUP}:{namespace}",
        ]
        for extra in supplied:
            if extra not in groups:
                groups.append(extra)
        return {
            "kind": kind, "name": name, "namespace": namespace,
            "username": username, "groups": groups, "groups_complete": True,
            "groups_detail": (
                "The API server assigns exactly these groups to every "
                f"ServiceAccount in {namespace}, so this answer is complete."
            ),
        }

    groups = [AUTHENTICATED_GROUP] + [g for g in supplied if g != AUTHENTICATED_GROUP]
    return {
        "kind": kind, "name": name, "namespace": None,
        "username": name, "groups": groups,
        # Always false for a User, even when groups were supplied — see the
        # docstring. The caller can add groups; they cannot make the list
        # provably complete, and this field must never claim they did.
        "groups_complete": False,
        "groups_detail": (
            "A user's real group memberships come from whatever authenticated "
            "them — OIDC claims, a certificate's organisation, a proxy header — "
            "and no API on this cluster reports them. This answer is about a user "
            "named " + name + " in exactly the groups listed, which may be fewer "
            "than they actually hold."
        ),
    }


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #

def validate_checks(raw: Any) -> list[dict[str, Any]]:
    """The list of questions, each shaped like §9's check."""
    if not isinstance(raw, list) or not raw:
        raise _invalid(
            "checks must be a non-empty list.",
            parameter="checks",
            hint="Ask about at least one verb on one resource.",
        )
    if len(raw) > MAX_CHECKS:
        raise _invalid(
            f"At most {MAX_CHECKS} checks per request (got {len(raw)}).",
            parameter="checks",
            hint=(
                "Each check is one round trip to the API server. If you are trying "
                "to enumerate everything a subject can do, that is a different "
                "question and this endpoint does not answer it."
            ),
        )

    checks = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise _invalid(f"checks[{index}] must be an object.", parameter="checks")
        verb = entry.get("verb")
        resource = entry.get("resource")
        if not isinstance(verb, str) or not verb.strip():
            raise _invalid(f"checks[{index}].verb is required.", parameter="checks")
        if not isinstance(resource, str) or not resource.strip():
            raise _invalid(f"checks[{index}].resource is required.", parameter="checks")
        checks.append({
            "verb": verb.strip(),
            "group": (entry.get("group") or "core"),
            "resource": resource.strip(),
            "namespace": entry.get("namespace") or None,
            "name": entry.get("name") or None,
            "subresource": entry.get("subresource") or None,
        })
    return checks


# --------------------------------------------------------------------------- #
# One review
# --------------------------------------------------------------------------- #

def _result(check: dict[str, Any], **answer: Any) -> dict[str, Any]:
    """One §23 result: the question echoed in the caller's own spelling, plus the answer.

    ``group`` is echoed as it was sent (§1.4's wire spelling, `core` for the core
    group) so a batch response joins to a batch request key-for-key. A result
    that came back with ``""`` where the caller sent ``core`` cannot be matched
    to the row it answers.
    """
    return {**check, **answer}


def _undecided(check: dict[str, Any], evaluation_error: str) -> dict[str, Any]:
    """A question the authorizer did not answer. **Never rendered as a denial.**"""
    return _result(
        check, allowed=False, denied=False, reason=None,
        evaluationError=evaluation_error,
    )


def review_one(subject: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """Ask the API server one question about one subject.

    Three fields come back and all three are kept:

    * ``allowed`` — an authorizer said yes.
    * ``denied`` — an authorizer said **no**, explicitly. Distinct from
      ``allowed: false``, which is the ordinary "nothing granted it" and is what
      almost every RBAC-only cluster produces. A webhook authorizer is what makes
      the difference visible, and flattening them would hide a deliberate deny
      behind the same words as an absent grant.
    * ``evaluationError`` — the authorizer could not decide. §0.2: this is not a
      denial, and reporting it as one sends somebody to grant a permission that
      is already there.
    """
    body = k8s_client.V1SubjectAccessReview(
        spec=k8s_client.V1SubjectAccessReviewSpec(
            user=subject["username"],
            groups=list(subject["groups"]),
            resource_attributes=k8s_client.V1ResourceAttributes(
                # The *real* group name, not §1.4's wire spelling: `core` is a URL
                # segment this project invented, and sending it here would review
                # an API group that does not exist — which answers a clean,
                # wrong "no" for every core resource.
                group=normalize_group(check["group"]),
                resource=check["resource"],
                verb=check["verb"],
                namespace=check["namespace"],
                name=check["name"],
                subresource=check["subresource"],
            ),
        )
    )

    try:
        review = get_authorization_v1().create_subject_access_review(body)
    except ApiException as e:
        mapped = from_api_exception(e, context={**REVIEW_TARGET, "verb": "create"})
        return _undecided(
            check, f"The access review could not be issued: {mapped.message}",
        )
    except AdminError as e:
        # ClusterUnreachable from the transport wrapper: DNS, TLS, a deadline.
        return _undecided(check, f"The access review could not be issued: {e.message}")

    status = getattr(review, "status", None)
    evaluation_error = getattr(status, "evaluation_error", None) or None
    return _result(
        check,
        allowed=bool(getattr(status, "allowed", False)),
        denied=bool(getattr(status, "denied", False)),
        reason=getattr(status, "reason", None) or None,
        evaluationError=evaluation_error,
    )


# --------------------------------------------------------------------------- #
# The endpoint's work
# --------------------------------------------------------------------------- #

def review(payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/access/subject-review`` (§23).

    Validates, preflights the console's own right to ask, runs one review per
    check, and writes one audit row for the request.

    **The preflight is about `subjectaccessreviews`, not about what is being
    asked.** If the console's ServiceAccount may not create them, that is a
    `403 rbac_denied` naming *that* permission — never a relayed refusal about
    the resource in the question, which would send an operator to grant alice
    something when the missing grant is the console's.

    Answers `200` with per-check results even when individual reviews fail: a
    batch of twelve questions where one authorizer errored still returns twelve
    rows, each saying what it knows. That is §9's shape and the reason for it is
    the same — a page renders a row per question and must be able to show which
    ones are unknown rather than losing the lot.
    """
    if not isinstance(payload, dict):
        raise _invalid("The request body must be an object.", parameter="body")
    unknown = set(payload) - {"subject", "checks"}
    if unknown:
        raise _invalid(
            "Unknown fields in the request body: " + ", ".join(sorted(unknown)),
            parameter="body",
        )

    subject = resolve_subject(payload.get("subject"))
    checks = validate_checks(payload.get("checks"))

    detail = (
        f"access review for {subject['username']}: "
        + ", ".join(
            f"{c['verb']} {normalize_group(c['group']) or 'core'}/{c['resource']}"
            for c in checks[:5]
        )
        + (f" (+{len(checks) - 5} more)" if len(checks) > 5 else "")
    )

    try:
        preflight.require("create", "authorization.k8s.io", "subjectaccessreviews")
    except RBACDenied as e:
        recorder.record(
            verb="create", target=REVIEW_TARGET, dry_run=False, outcome="denied",
            detail=detail, error=e.message,
        )
        raise
    except (ClusterUnreachable, UpstreamError) as e:
        # The console could not establish whether it may ask. Recorded as failed
        # rather than denied: nobody refused anything, and a trail that called
        # this a denial would misreport an outage as a permissions decision.
        recorder.record(
            verb="create", target=REVIEW_TARGET, dry_run=False, outcome="failed",
            detail=detail, error=e.message,
        )
        raise

    results = [review_one(subject, check) for check in checks]
    audit_id = recorder.record(
        verb="create", target=REVIEW_TARGET, dry_run=False, outcome="applied",
        detail=detail,
    )

    undecided = sum(1 for r in results if r["evaluationError"])
    return {
        "subject": subject,
        "results": results,
        # Counted so a caller does not have to scan for them, and named
        # `undecided` rather than `errors`: these are questions without answers,
        # not failures of the request.
        "undecided": undecided,
        "auditId": audit_id,
    }


__all__ = [
    "MAX_CHECKS",
    "SUBJECT_KINDS",
    "resolve_subject",
    "review",
    "review_one",
    "validate_checks",
]
