"""
Access preflight (§9).

One distinction is on trial in this file, and everything else here exists to
protect it: **a denial and a failed review are not the same answer.**

``allowed: false, evaluationError: null`` means the cluster said no.
``allowed: false, evaluationError: "..."`` means the cluster could not work out
whether to say no. Collapsing the second into the first tells an operator they
are missing a permission they may well hold, and sends them to edit a ClusterRole
that is already correct — a wrong answer delivered confidently, which is the one
failure mode this project measures everything against.

So the distinction is asserted three times over, because it has to survive all
three of them: in the result dict, in what ``require`` raises, and in the HTTP
status the API returns.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from app.admin import preflight
from app.errors import ClusterUnreachable, Invalid, RBACDenied
from tests.conftest import obj


# --------------------------------------------------------------------------- #
# Builders and fixtures
# --------------------------------------------------------------------------- #

def review(*, allowed=False, reason=None, evaluation_error=None, denied=False):
    """A stubbed ``SelfSubjectAccessReview`` response, in the SDK's own shape."""
    return obj(status=obj(
        allowed=allowed, reason=reason, evaluation_error=evaluation_error, denied=denied,
    ))


def answer(fake, value):
    """Make every review this test issues return ``value``."""
    fake.authorization_v1.returns("create_self_subject_access_review", value)


@pytest.fixture
def api(db_engine, fake_k8s):
    """A TestClient carrying only this lane's router.

    Not ``app.main``: that module imports every router in the product, several of
    which belong to other lanes and land in other commits. A lane test that could
    not run until every lane had landed would be a lane test nobody ran.
    """
    from app.api.access import router as access_router
    from app.api.exception_handlers import register_exception_handlers
    from app.k8s.context import ClusterContextMiddleware

    application = FastAPI()
    application.add_middleware(ClusterContextMiddleware)
    register_exception_handlers(application)
    application.include_router(access_router)
    return TestClient(application)


# --------------------------------------------------------------------------- #
# The three answers a review can give
# --------------------------------------------------------------------------- #

def test_an_allowed_review_is_reported_as_allowed(fake_k8s):
    answer(fake_k8s, review(allowed=True, reason="RBAC: allowed by ClusterRoleBinding"))

    result = preflight.check("patch", "apps", "deployments", namespace="prod")

    assert result["allowed"] is True
    assert result["evaluationError"] is None
    # No hint on an allow: there is nothing to fix, and a hint here would render
    # as advice next to a button that already works.
    assert result["hint"] is None
    assert preflight.require("patch", "apps", "deployments", namespace="prod") is None


def test_a_clean_denial_names_the_grant_that_would_fix_it(fake_k8s):
    answer(fake_k8s, review(allowed=False))

    result = preflight.check("patch", "apps", "deployments", namespace="prod")

    assert result["allowed"] is False
    assert result["evaluationError"] is None, "a clean denial has no evaluation error"
    assert result["reason"] == "no RBAC policy matched"
    assert "`patch`" in result["hint"] and "`apps/deployments`" in result["hint"]
    assert "`prod`" in result["hint"], "the hint names the namespace, not just the resource"


def test_a_clean_denial_raises_rbac_denied(fake_k8s):
    answer(fake_k8s, review(allowed=False))

    with pytest.raises(RBACDenied) as caught:
        preflight.require("delete", "", "pods", namespace="prod", name="checkout-7d9")

    error = caught.value
    assert error.http_status == 403
    assert error.code == "rbac_denied"
    assert error.context["resource"] == "pods"
    assert error.context["group"] == "", "the API server's own spelling of the core group"
    assert error.context["name"] == "checkout-7d9"


def test_an_evaluation_error_is_never_reported_as_a_denial(fake_k8s):
    """The whole point of this module. `allowed: false` here means 'we do not
    know', and it must not read as 'you may not'."""
    answer(fake_k8s, review(
        allowed=False,
        evaluation_error="webhook authorizer 'gatekeeper' failed: connection refused",
    ))

    result = preflight.check("patch", "apps", "deployments", namespace="prod")

    assert result["allowed"] is False
    assert result["evaluationError"] == (
        "webhook authorizer 'gatekeeper' failed: connection refused"
    )
    assert result["hint"] is not None
    assert "unknown" in result["hint"], "the UI renders this differently from a denial"
    assert "Grant" not in result["hint"], (
        "an evaluation error must not tell the operator to add a grant they may "
        "already hold"
    )


def test_an_evaluation_error_raises_something_other_than_rbac_denied(fake_k8s):
    answer(fake_k8s, review(allowed=False, evaluation_error="authorizer timed out"))

    with pytest.raises(Exception) as caught:
        preflight.require("patch", "apps", "deployments", namespace="prod")

    error = caught.value
    assert not isinstance(error, RBACDenied), (
        "403 rbac_denied would state that the caller lacks a permission we never "
        "established anything about"
    )
    assert error.http_status == 502
    assert error.code == "upstream_error"
    assert error.context["evaluationError"] == "authorizer timed out"


def test_an_allowed_review_that_also_errored_is_still_allowed(fake_k8s):
    """`allowed: true` is authoritative even when one authorizer in the chain
    failed — but the failure is still reported, because it is the only evidence
    that something in the chain is broken."""
    answer(fake_k8s, review(allowed=True, evaluation_error="one authorizer errored"))

    result = preflight.check("get", "", "secrets", namespace="prod")

    assert result["allowed"] is True
    assert result["evaluationError"] == "one authorizer errored"
    assert preflight.require("get", "", "secrets", namespace="prod") is None


def test_an_explicit_denial_says_so(fake_k8s):
    """A webhook authorizer returning Denied is a different fact from no policy
    matching: one is a decision, the other is an absence."""
    answer(fake_k8s, review(allowed=False, denied=True, reason="blocked by policy P-14"))

    result = preflight.check("delete", "", "namespaces", name="prod")

    assert result["allowed"] is False
    assert "explicitly denied" in result["reason"]
    assert "P-14" in result["reason"]


# --------------------------------------------------------------------------- #
# Reviews we could not issue at all
# --------------------------------------------------------------------------- #

def test_a_review_we_could_not_issue_is_unknown_not_denied(fake_k8s):
    """Being refused permission to *ask* says nothing about the answer."""
    fake_k8s.authorization_v1.raises(
        "create_self_subject_access_review", ApiException(status=403, reason="Forbidden"),
    )

    result = preflight.check("patch", "apps", "deployments", namespace="prod")

    assert result["allowed"] is False
    assert result["evaluationError"] is not None
    assert "could not be issued" in result["evaluationError"]

    with pytest.raises(Exception) as caught:
        preflight.require("patch", "apps", "deployments", namespace="prod")
    assert not isinstance(caught.value, RBACDenied)
    assert caught.value.context["reviewResource"] == "selfsubjectaccessreviews"
    assert "selfsubjectaccessreviews" in caught.value.hint


def test_an_unreachable_cluster_stays_an_unreachable_cluster(fake_k8s):
    """Not re-badged as an authorization problem: the operator's next move is to
    check the network, not their RBAC."""
    fake_k8s.authorization_v1.raises(
        "create_self_subject_access_review",
        ClusterUnreachable("The cluster API server could not be reached."),
    )

    result = preflight.check("list", "", "pods")
    assert result["allowed"] is False
    assert result["evaluationError"] is not None

    with pytest.raises(ClusterUnreachable):
        preflight.require("list", "", "pods")


# --------------------------------------------------------------------------- #
# What is actually sent to the API server
# --------------------------------------------------------------------------- #

def test_the_core_group_is_sent_as_the_empty_string(fake_k8s):
    """`core` is a URL spelling (§1.4). Sending it to the authorizer would review
    a group that does not exist, which answers no for every core resource —
    cleanly, and wrongly."""
    answer(fake_k8s, review(allowed=True))

    preflight.check("list", "core", "pods", namespace="prod")

    (args, _kwargs) = fake_k8s.authorization_v1.called("create_self_subject_access_review")[0]
    attributes = args[0].spec.resource_attributes
    assert attributes.group == ""
    assert attributes.resource == "pods"
    assert attributes.verb == "list"
    assert attributes.namespace == "prod"


def test_a_subresource_is_reviewed_separately_from_its_parent(fake_k8s):
    """RBAC names `pods/exec` separately; a review of `pods` would answer a
    different question and enable the wrong button."""
    answer(fake_k8s, review(allowed=True))

    result = preflight.check("create", "core", "pods", namespace="prod", subresource="exec")

    (args, _kwargs) = fake_k8s.authorization_v1.called("create_self_subject_access_review")[0]
    assert args[0].spec.resource_attributes.subresource == "exec"
    assert result["subresource"] == "exec"


def test_the_result_echoes_the_wire_spelling_of_the_group(fake_k8s):
    """§9's own vocabulary is the wire one, and a batch caller pairs results with
    checks by their fields. A result carrying "" where the check said "core"
    cannot be joined back without every consumer re-implementing §1.4."""
    answer(fake_k8s, review(allowed=True))

    assert preflight.check("list", "core", "pods")["group"] == "core"
    assert preflight.check("list", "", "pods")["group"] == "core"
    assert preflight.check("list", "apps", "deployments")["group"] == "apps"


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #

def test_a_batch_returns_one_result_per_check_in_order(fake_k8s):
    """Callers pair results with checks by index. A shorter or reordered list
    silently attributes a permission to the wrong button."""
    answers = iter([
        review(allowed=True),
        review(allowed=False),
        review(allowed=False, evaluation_error="authorizer unavailable"),
    ])
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review", lambda *a, **k: next(answers),
    )

    results = preflight.check_many([
        {"verb": "list", "group": "core", "resource": "pods"},
        {"verb": "patch", "group": "apps", "resource": "deployments"},
        {"verb": "get", "group": "core", "resource": "secrets"},
    ])

    assert [r["resource"] for r in results] == ["pods", "deployments", "secrets"]
    assert [r["allowed"] for r in results] == [True, False, False]
    assert [r["evaluationError"] for r in results] == [None, None, "authorizer unavailable"]


def test_a_batch_with_a_malformed_check_is_rejected_not_skipped(fake_k8s):
    """Skipping it shifts every later result by one — the mis-attribution above,
    with no error to explain it."""
    answer(fake_k8s, review(allowed=True))

    with pytest.raises(Invalid) as caught:
        preflight.check_many([
            {"verb": "list", "group": "core", "resource": "pods"},
            {"group": "apps", "resource": "deployments"},
        ])

    assert "1" in caught.value.message, "the error names which check was malformed"


def test_an_oversized_batch_is_refused(fake_k8s):
    with pytest.raises(Invalid) as caught:
        preflight.check_many(
            [{"verb": "list", "group": "core", "resource": "pods"}] * (preflight.MAX_BATCH + 1)
        )
    assert caught.value.context["limit"] == preflight.MAX_BATCH


# --------------------------------------------------------------------------- #
# §9 over HTTP
# --------------------------------------------------------------------------- #

def test_the_endpoint_answers_200_with_allowed_false_not_403(api, fake_k8s):
    """The endpoint reports on a permission; it does not exercise one. A 403 here
    would make §11.4 impossible — a disabled button has to be able to say why."""
    answer(fake_k8s, review(allowed=False))

    response = api.get("/api/access/preflight?verb=patch&group=apps&resource=deployments")

    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is False
    assert body["evaluationError"] is None
    assert body["hint"].startswith("Grant `patch`")


def test_the_endpoint_keeps_an_evaluation_error_visible(api, fake_k8s):
    answer(fake_k8s, review(allowed=False, evaluation_error="webhook down"))

    body = api.get(
        "/api/access/preflight?verb=create&group=core&resource=pods&subresource=exec"
    ).json()

    assert body["allowed"] is False
    assert body["evaluationError"] == "webhook down"
    assert body["subresource"] == "exec"


def test_the_batch_endpoint_returns_results_in_request_order(api, fake_k8s):
    answers = iter([review(allowed=True), review(allowed=False)])
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review", lambda *a, **k: next(answers),
    )

    response = api.post("/api/access/preflight", json={"checks": [
        {"verb": "list", "group": "core", "resource": "pods"},
        {"verb": "delete", "group": "core", "resource": "pods", "namespace": "prod"},
    ]})

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["verb"] for r in results] == ["list", "delete"]
    assert [r["allowed"] for r in results] == [True, False]
