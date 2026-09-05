"""
Access preflight endpoints (§9) and the subject access review (§23).

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


__all__ = ["router"]
