"""
Access preflight endpoints (§9), the subject access review (§23), and §30's
grant and revoke.

Two routes over :mod:`app.admin.preflight`: one question, and a batch of them.
The batch exists because the interesting callers ask many at once — cluster
registration checks the baseline verb set so a half-permissioned ServiceAccount
is visible at registration rather than at first use, and a page asks about every
button it is going to render.

**Why this is a 200 with ``allowed: false`` and not a 403.** The endpoint's job is
to *report* on a permission, not to exercise it. A 403 here would be the console
saying "you may not ask", which is a different and untrue statement — and it
would make the UI's job impossible, because §11.4 requires buttons the caller
cannot use to be disabled **with the reason** rather than hidden. An operator has
to be able to see that an action exists and why it is unavailable; that needs a
body, not a status code.

The one thing this module must not do is flatten ``evaluationError`` into a
denial. ``app.admin.preflight`` keeps them apart and both reach the client
verbatim.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.admin import access_review
from app.admin import preflight
from app.admin import rbac_grants
from app.api.bodies import MutationBody
from app.errors import Invalid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["access"])


class PreflightCheck(BaseModel):
    """One entry of the §9 batch body."""

    verb: str = Field(
        ...,
        description="The RBAC verb: get, list, watch, create, update, patch, delete.",
    )
    group: str = Field(
        "core",
        description=(
            "API group, §1.4 wire spelling — `core` for the core group, whose real "
            "name is the empty string and cannot appear in a URL segment."
        ),
    )
    resource: str = Field(..., description="Plural resource name, as discovery reports it.")
    namespace: str | None = Field(
        None,
        description=(
            "Omit for a cluster-wide check. Omitting it is not the same as passing "
            "one: a ServiceAccount can hold `patch` in `prod` and nowhere else, and "
            "a cluster-wide check on that identity correctly answers no."
        ),
    )
    name: str | None = Field(
        None,
        description=(
            "For a check against one object, where a Role grants a verb by "
            "resourceNames. Omit to ask about the resource as a whole."
        ),
    )
    subresource: str | None = Field(
        None,
        description="`exec`, `log`, `scale` — RBAC names these separately from their parent.",
    )


class PreflightBatch(BaseModel):
    """§9 ``POST /api/access/preflight`` body."""

    checks: list[PreflightCheck] = Field(
        ...,
        description=(
            "The checks to run, in order. Results come back in the same order, "
            "because the caller pairs them by index."
        ),
    )


@router.get("/access/preflight")
def get_preflight(
    verb: str = Query(..., description="The RBAC verb to test."),
    resource: str = Query(..., description="Plural resource name."),
    group: str = Query("core", description="API group; `core` for the core group."),
    namespace: str | None = Query(None),
    name: str | None = Query(None),
    subresource: str | None = Query(None),
) -> dict[str, Any]:
    """§9 single check. Always 200; the answer is in the body.

    ``allowed: false`` with a non-null ``evaluationError`` means the review itself
    failed and the permission is **unknown** — the UI renders that differently
    from a clean denial, because telling an operator they lack a grant they hold
    sends them to edit a ClusterRole that is already correct.
    """
    if not verb.strip() or not resource.strip():
        raise Invalid(
            "Both `verb` and `resource` are required.",
            hint="For example: ?verb=patch&group=apps&resource=deployments&namespace=prod",
            context={"verb": verb, "resource": resource},
        )
    return preflight.check(
        verb.strip(), group, resource.strip(),
        namespace=namespace, name=name, subresource=subresource,
    )


@router.post("/access/preflight")
def post_preflight(body: PreflightBatch) -> dict[str, Any]:
    """§9 batch check → ``{"results": [PreflightResult]}``.

    Every check produces a result, including the ones whose review failed: a
    seventeen-row permissions report with one row missing is a report the reader
    has to diff against the request to interpret.
    """
    return {
        "results": preflight.check_many(
            [check.model_dump() for check in body.checks]
        )
    }


class ReviewSubject(BaseModel):
    """Who the §23 review is about."""

    kind: str = Field(
        ...,
        description=(
            "`User` or `ServiceAccount`. **Not `Group`** — the authorizer takes a "
            "user plus a list of groups, so a group cannot be the subject; ask "
            "about a user who is in it."
        ),
    )
    name: str = Field(..., description="The username, or the ServiceAccount's name.")
    namespace: str | None = Field(
        None,
        description=(
            "Required for a ServiceAccount: its identity is "
            "`system:serviceaccount:<namespace>:<name>`, so the namespace is part "
            "of who it is rather than a filter on the question."
        ),
    )
    groups: list[str] = Field(
        default_factory=list,
        description=(
            "Groups to include in the review. **This is the field that decides "
            "whether the answer is right.** The API server only considers the "
            "groups in the review, and a subject's access mostly arrives through "
            "them — so a review that omits a user's real groups answers 'no' "
            "about somebody who can. `system:authenticated` is always added, and "
            "a ServiceAccount's deterministic groups are added for it."
        ),
    )


class SubjectReviewRequest(BaseModel):
    """One subject, and the questions to ask about it."""

    subject: ReviewSubject
    checks: list[PreflightCheck] = Field(
        ...,
        description=(
            f"Up to {access_review.MAX_CHECKS} checks, each shaped like §9's. One "
            "round trip to the API server per check."
        ),
    )


@router.post("/access/subject-review")
def post_subject_review(body: SubjectReviewRequest) -> dict[str, Any]:
    """§23 — what may *this* subject do? Asked of the API server, not derived.

    `SubjectAccessReview` runs the API server's whole authorization chain — RBAC,
    the node authorizer, any webhook authorizer — so the answer is authoritative
    in a way that subtracting §8's RoleBindings can never be. And it takes no
    action as the subject: it is a question about them, which is why this exists
    while `docs/adr-0007-impersonation.md` stays proposed.

    Read `subject.groups_complete` before reading the results. For a
    ServiceAccount it is true and the answer is complete. **For a User it is
    always false**: no API here reports a person's real group memberships, so the
    honest reading is never "alice cannot do this" but "a user named alice, in
    exactly these groups, cannot do this".

    Per-check, `allowed: false` with a non-null `evaluationError` is **not** a
    denial — §0.2, the same rule §9 keeps — and `denied: true` is an authorizer
    explicitly refusing, which is a different fact from nothing having granted it.

    A privileged read: one audit row per request names who asked about whom.
    """
    return access_review.review(body.model_dump())


# --------------------------------------------------------------------------- #
# §30 — granting and revoking a role in one namespace
# --------------------------------------------------------------------------- #

class GrantSubject(BaseModel):
    """The identity being bound or unbound."""

    kind: str = Field(
        ...,
        description=f"One of {', '.join(rbac_grants.SUBJECT_KINDS)}.",
    )
    name: str = Field(..., description="The subject's name.")
    namespace: str | None = Field(
        None,
        description=(
            "A ServiceAccount's namespace. Defaults to the namespace being "
            "granted in — a ServiceAccount subject without one matches nobody, "
            "so it is filled rather than omitted."
        ),
    )


class GrantRole(BaseModel):
    """The `roleRef`. A RoleBinding to a **ClusterRole** confers that role's
    rules inside this namespace only — it is not a cluster-wide grant."""

    kind: str = Field(
        ..., description=f"One of {', '.join(rbac_grants.ROLE_KINDS)}."
    )
    name: str = Field(..., description="The role's name.")


class GrantPlanRequest(BaseModel):
    """§30's plan body. No `dryRun`: a plan writes nothing at all."""

    operation: str = Field(
        ..., description=f"One of {', '.join(rbac_grants.OPERATIONS)}."
    )
    role: GrantRole
    subject: GrantSubject


class GrantRequest(MutationBody):
    """§30's write body — the plan's fields, plus §0.4's version and §18's
    acknowledgement."""

    operation: str = Field(
        ..., description=f"One of {', '.join(rbac_grants.OPERATIONS)}."
    )
    role: GrantRole
    subject: GrantSubject
    resource_version: str | None = Field(
        None,
        alias="resourceVersion",
        description=(
            "The binding's version from the plan. A mismatch is `409 conflict` "
            "with a fresh subject list rather than a write that replaces the "
            "whole array over somebody else's grant. Absent on a create, which "
            "has no live object."
        ),
    )
    acknowledge_consequences: list[str] | None = Field(
        None,
        alias="acknowledgeConsequences",
        description=(
            "Consequence codes from the plan. Every one it reports must be "
            "named, and they are recomputed here — an acknowledgement of a "
            "`view` grant cannot be spent on an `admin` one."
        ),
    )


@router.post("/access/namespaces/{namespace}/grants/plan")
def post_grant_plan(request: GrantPlanRequest, namespace: str) -> dict[str, Any]:
    """§30 — what this grant or revoke would actually do. Reads only.

    The three questions the binding itself cannot answer: what the role confers
    (resolved and summarised, with `state: unreadable` kept apart from a role
    that grants nothing), whether the role exists at all (a binding to a missing
    role is accepted and starts granting when somebody creates it), and — for a
    revoke — what else still names the subject, with `cluster_bindings: null`
    when the cluster-wide listing was refused rather than `[]`.

    A request that cannot proceed comes back `blocked` with a 200, not a 422:
    this is the screen where the grant is decided, and the residual list is
    exactly what answers "then where does their access come from".
    """
    return rbac_grants.plan(namespace, request.model_dump(by_alias=True))


@router.put("/access/namespaces/{namespace}/grants")
def put_grant(request: GrantRequest, namespace: str) -> dict[str, Any]:
    """§30 — add or remove one subject on one RoleBinding, through the funnel.

    A grant with no binding to extend is a `create` of a whole RoleBinding;
    everything else is a `patch` of `subjects`. They are preflighted as what they
    are, so a caller who may patch but not create is told which one they lack.

    **`applied: true` means the binding's subject list is what you sent.** On a
    revoke it does not mean the subject can no longer act here — `residual` in
    this response lists what still names them, and §23's subject review is how
    to ask the API server the authoritative question afterwards.
    """
    payload = request.model_dump(by_alias=True)
    return rbac_grants.apply_grant(
        namespace,
        payload,
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledge_consequences,
    )


__all__ = ["router"]
