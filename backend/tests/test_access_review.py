"""
Subject access review (§23).

Asking the API server what a *named* subject may do. What is worth testing is
not that a review is issued — §9 covers the mechanics — but the four claims that
make this answer trustworthy or not:

* **Groups decide the answer, and for a User they are never provably complete.**
  A subject's access mostly arrives through their groups, and the API server only
  considers the groups in the review. A review for `alice` with no groups answers
  "no" about somebody who is a cluster administrator through `platform-admins`.
  A ServiceAccount's groups are deterministic and reproduced; a User's are not
  knowable here, and `groups_complete` must say so every time.

* **`evaluationError` is not a denial.** §0.2, the rule §9 already keeps. A
  review the authorizer could not decide reports as undecided, never as "may
  not" — which would send somebody to grant a permission that is already there.

* **`denied: true` is not `allowed: false`.** One is an authorizer explicitly
  refusing; the other is nothing having granted it. Flattening them hides a
  deliberate deny behind the same words as an absent grant.

* **The preflight is about `subjectaccessreviews`.** If the console may not ask,
  the denial names *that* permission — not the resource in the question, which
  would send an operator to grant alice something when the missing grant is the
  console's.
"""

from __future__ import annotations

import pytest

from app.audit import recorder
from app.errors import Invalid, RBACDenied
from app.admin import access_review
from tests.conftest import obj

# Built through `validate_checks`, because that is the only shape `review_one`
# is ever handed: testing it against a hand-written dict would be testing a
# shape the code does not produce.
(CHECK,) = access_review.validate_checks(
    [{"verb": "delete", "group": "core", "resource": "pods", "namespace": "prod"}]
)


def allow_preflight(fake_k8s):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))


def stub_review(fake_k8s, *, allowed=True, denied=False, reason=None, evaluation_error=None):
    fake_k8s.authorization_v1.returns("create_subject_access_review", obj(status=obj(
        allowed=allowed, denied=denied, reason=reason, evaluation_error=evaluation_error,
    )))


def audit_rows():
    return recorder.query(limit=50)["items"]


def body(subject=None, checks=None):
    return {
        "subject": subject or {"kind": "User", "name": "alice"},
        "checks": checks or [CHECK],
    }


# --------------------------------------------------------------------------- #
# The subject and its groups
# --------------------------------------------------------------------------- #

def test_a_service_account_gets_the_groups_the_api_server_assigns_it():
    """Reproduced rather than asked for: a review without them asks about an
    identity that cannot exist."""
    subject = access_review.resolve_subject(
        {"kind": "ServiceAccount", "name": "deployer", "namespace": "prod"}
    )

    assert subject["username"] == "system:serviceaccount:prod:deployer"
    assert subject["groups"] == [
        "system:authenticated", "system:serviceaccounts", "system:serviceaccounts:prod",
    ]
    assert subject["groups_complete"] is True


def test_a_users_groups_are_never_complete_even_when_supplied():
    """The field must never claim a list is all of them. No API on this cluster
    reports a person's real memberships, so the truthful sentence is always
    conditional on the groups named."""
    subject = access_review.resolve_subject(
        {"kind": "User", "name": "alice", "groups": ["platform-admins"]}
    )

    assert subject["username"] == "alice"
    assert subject["groups"] == ["system:authenticated", "platform-admins"]
    assert subject["groups_complete"] is False
    assert "may be fewer than they actually hold" in subject["groups_detail"]


def test_system_authenticated_is_always_included_and_never_duplicated():
    subject = access_review.resolve_subject(
        {"kind": "User", "name": "alice", "groups": ["system:authenticated", "ops"]}
    )

    assert subject["groups"] == ["system:authenticated", "ops"]


def test_extra_groups_on_a_service_account_are_added_after_the_assigned_ones():
    subject = access_review.resolve_subject({
        "kind": "ServiceAccount", "name": "deployer", "namespace": "prod",
        "groups": ["extra-group"],
    })

    assert subject["groups"][-1] == "extra-group"
    assert subject["groups_complete"] is True


def test_a_service_account_without_a_namespace_is_refused():
    """The namespace is part of who a ServiceAccount is, not a filter."""
    with pytest.raises(Invalid) as caught:
        access_review.resolve_subject({"kind": "ServiceAccount", "name": "deployer"})

    assert caught.value.context["parameter"] == "subject.namespace"


def test_a_group_cannot_be_the_subject():
    """The authorizer takes a user plus groups. Offering "what can this group do"
    would be inventing a question it cannot be asked."""
    with pytest.raises(Invalid) as caught:
        access_review.resolve_subject({"kind": "Group", "name": "platform-admins"})

    assert caught.value.context["parameter"] == "subject.kind"
    assert "cannot be the subject" in (caught.value.hint or "")


def test_a_subject_without_a_name_is_refused():
    with pytest.raises(Invalid) as caught:
        access_review.resolve_subject({"kind": "User"})

    assert caught.value.context["parameter"] == "subject.name"


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #

def test_an_empty_check_list_is_refused():
    with pytest.raises(Invalid) as caught:
        access_review.validate_checks([])

    assert caught.value.context["parameter"] == "checks"


def test_more_checks_than_the_bound_are_refused_saying_why():
    """Each check is a round trip. Somebody asking about two hundred verbs wants
    a different feature."""
    with pytest.raises(Invalid) as caught:
        access_review.validate_checks([CHECK] * (access_review.MAX_CHECKS + 1))

    assert "different question" in (caught.value.hint or "")


def test_a_check_without_a_verb_or_resource_is_refused():
    for bad in ({"resource": "pods"}, {"verb": "get"}):
        with pytest.raises(Invalid):
            access_review.validate_checks([bad])


def test_the_core_group_defaults_and_is_kept_in_the_wire_spelling():
    """§1.4: the response joins to the request key-for-key, so `core` must come
    back as `core` rather than as the empty string it becomes on the wire."""
    (check,) = access_review.validate_checks([{"verb": "get", "resource": "pods"}])

    assert check["group"] == "core"


# --------------------------------------------------------------------------- #
# One review
# --------------------------------------------------------------------------- #

def test_an_allowed_review_reports_allowed(fake_k8s):
    stub_review(fake_k8s, allowed=True, reason="RBAC: allowed by ClusterRoleBinding")
    subject = access_review.resolve_subject({"kind": "User", "name": "alice"})

    result = access_review.review_one(subject, CHECK)

    assert result["allowed"] is True
    assert result["denied"] is False
    assert result["evaluationError"] is None
    assert "ClusterRoleBinding" in result["reason"]


def test_an_explicit_deny_is_kept_apart_from_nothing_granting_it(fake_k8s):
    """One is an authorizer refusing; the other is an absent grant. A webhook
    authorizer is what makes the difference visible."""
    stub_review(fake_k8s, allowed=False, denied=True, reason="denied by webhook")
    subject = access_review.resolve_subject({"kind": "User", "name": "alice"})

    result = access_review.review_one(subject, CHECK)

    assert result["allowed"] is False
    assert result["denied"] is True

    stub_review(fake_k8s, allowed=False, denied=False)
    assert access_review.review_one(subject, CHECK)["denied"] is False


def test_an_evaluation_error_is_undecided_not_a_denial(fake_k8s):
    """§0.2. Reporting this as "may not" sends somebody to grant a permission
    that is already there."""
    stub_review(fake_k8s, allowed=False, evaluation_error="webhook timed out")
    subject = access_review.resolve_subject({"kind": "User", "name": "alice"})

    result = access_review.review_one(subject, CHECK)

    assert result["allowed"] is False
    assert result["evaluationError"] == "webhook timed out"


def test_a_review_the_transport_could_not_carry_is_undecided(fake_k8s):
    from app.errors import ClusterUnreachable

    fake_k8s.authorization_v1.raises(
        "create_subject_access_review", ClusterUnreachable("api server unreachable"),
    )
    subject = access_review.resolve_subject({"kind": "User", "name": "alice"})

    result = access_review.review_one(subject, CHECK)

    assert result["allowed"] is False
    assert "could not be issued" in result["evaluationError"]


def test_a_review_the_api_server_refused_is_undecided_not_a_denial(fake_k8s):
    """The other failure branch, and the one that matters most: the API server
    itself refuses to create the review — a 403 on `subjectaccessreviews`, a 500
    from an authorizer. Reporting that as `allowed: false` with no error says
    "alice may not delete pods" about a question nobody ever answered."""
    from kubernetes.client.rest import ApiException

    fake_k8s.authorization_v1.raises(
        "create_subject_access_review", ApiException(status=403, reason="Forbidden"),
    )
    subject = access_review.resolve_subject({"kind": "User", "name": "alice"})

    result = access_review.review_one(subject, CHECK)

    assert result["allowed"] is False
    assert result["denied"] is False
    assert result["evaluationError"] is not None
    assert "could not be issued" in result["evaluationError"]


def test_the_core_group_reaches_the_authorizer_as_the_empty_string(fake_k8s):
    """`core` is a URL segment this project invented. Sending it to the
    authorizer would review a group that does not exist — a clean, wrong "no"
    for every core resource."""
    stub_review(fake_k8s)
    subject = access_review.resolve_subject({"kind": "User", "name": "alice"})

    access_review.review_one(subject, CHECK)

    ((args, _kwargs),) = fake_k8s.authorization_v1.called("create_subject_access_review")
    assert args[0].spec.resource_attributes.group == ""
    # …and the result still echoes the caller's spelling.
    assert access_review.review_one(subject, CHECK)["group"] == "core"


def test_the_review_carries_the_subject_and_its_groups(fake_k8s):
    stub_review(fake_k8s)
    subject = access_review.resolve_subject(
        {"kind": "ServiceAccount", "name": "deployer", "namespace": "prod"}
    )

    access_review.review_one(subject, CHECK)

    ((args, _kwargs),) = fake_k8s.authorization_v1.called("create_subject_access_review")
    assert args[0].spec.user == "system:serviceaccount:prod:deployer"
    assert "system:serviceaccounts:prod" in args[0].spec.groups


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #

def test_a_batch_returns_one_row_per_check_even_when_one_is_undecided(
    fake_k8s, db_engine,
):
    """A twelve-question report with one row missing is a report the reader has
    to diff against the request to interpret."""
    allow_preflight(fake_k8s)
    answers = iter([
        obj(status=obj(allowed=True, denied=False, reason=None, evaluation_error=None)),
        obj(status=obj(allowed=False, denied=False, reason=None,
                       evaluation_error="webhook timed out")),
        obj(status=obj(allowed=False, denied=False, reason=None, evaluation_error=None)),
    ])
    fake_k8s.authorization_v1.returns(
        "create_subject_access_review", lambda *a, **k: next(answers),
    )

    result = access_review.review(body(checks=[
        {"verb": "get", "resource": "pods"},
        {"verb": "delete", "resource": "pods"},
        {"verb": "create", "group": "apps", "resource": "deployments"},
    ]))

    assert len(result["results"]) == 3
    assert result["undecided"] == 1
    assert [r["allowed"] for r in result["results"]] == [True, False, False]


def test_the_preflight_asks_about_subjectaccessreviews_not_the_question(
    fake_k8s, db_engine,
):
    allow_preflight(fake_k8s)
    stub_review(fake_k8s)

    access_review.review(body())

    ((args, _kwargs),) = fake_k8s.authorization_v1.called(
        "create_self_subject_access_review"
    )
    attributes = args[0].spec.resource_attributes
    assert attributes.verb == "create"
    assert attributes.resource == "subjectaccessreviews"
    assert attributes.group == "authorization.k8s.io"


def test_a_console_that_may_not_ask_is_denied_naming_its_own_permission(
    fake_k8s, db_engine,
):
    """Never a relayed refusal about the resource in the question, which would
    send an operator to grant alice something when the missing grant is the
    console's."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no create on subjectaccessreviews",
        evaluation_error=None, denied=True,
    )))

    with pytest.raises(RBACDenied):
        access_review.review(body())

    assert fake_k8s.authorization_v1.called("create_subject_access_review") == []
    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["target"]["resource"] == "subjectaccessreviews"


def test_a_review_is_audited_naming_who_was_asked_about(fake_k8s, db_engine):
    """A privileged read: "who asked what the CFO's account could reach" is a
    question worth being able to answer afterwards."""
    allow_preflight(fake_k8s)
    stub_review(fake_k8s)

    result = access_review.review(body(
        subject={"kind": "ServiceAccount", "name": "deployer", "namespace": "prod"},
    ))

    (row,) = audit_rows()
    assert row["outcome"] == "applied"
    assert "system:serviceaccount:prod:deployer" in row["detail"]
    assert "delete core/pods" in row["detail"]
    assert result["auditId"] == row["id"]


def test_a_long_batch_is_summarised_in_the_audit_detail(fake_k8s, db_engine):
    allow_preflight(fake_k8s)
    stub_review(fake_k8s)

    access_review.review(body(checks=[
        {"verb": v, "resource": "pods"} for v in
        ["get", "list", "watch", "create", "update", "patch", "delete"]
    ]))

    (row,) = audit_rows()
    assert "(+2 more)" in row["detail"]


def test_an_unknown_body_field_is_refused_rather_than_ignored(fake_k8s, db_engine):
    with pytest.raises(Invalid):
        access_review.review({**body(), "impersonate": True})


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def test_the_endpoint_answers_200_with_the_subject_and_its_completeness(
    client, fake_k8s,
):
    allow_preflight(fake_k8s)
    stub_review(fake_k8s, allowed=True)

    response = client.post("/api/access/subject-review", json=body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["subject"]["groups_complete"] is False
    assert payload["results"][0]["allowed"] is True


def test_the_endpoint_refuses_a_group_subject_with_422(client, fake_k8s):
    response = client.post(
        "/api/access/subject-review",
        json=body(subject={"kind": "Group", "name": "platform-admins"}),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
