"""
Autoscaling writes (§21) — a HorizontalPodAutoscaler's replica bounds.

Thin, like every router here: parse, call, envelope. Whether the autoscaler is
scaling at all, what the bounds change means, and what `applied: true` does not
prove all live in :mod:`app.admin.hpa`, which reaches a cluster only through
:func:`app.admin.mutate.mutate`.

There is no listing endpoint here and deliberately will not be one. HPAs are
browsed through §4's generic path, which already returns the typed §21 row
because `hpa_row` is registered on the backend — a second listing in this file
would be a second shaping of the same object, free to drift from the first and
impossible to notice from outside.

The gate is echoed onto the plan rather than fetched separately, for §16's
reason: a dialog that had to ask a second endpoint whether writing is permitted
would render its button before knowing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin import hpa as hpa_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["autoscaling"])

_HPA = "/autoscaling/hpas/{namespace}/{name}"


class BoundsPlanRequest(BaseModel):
    """Both bounds, and the version the operator was looking at."""

    model_config = ConfigDict(populate_by_name=True)

    minReplicas: int = Field(  # noqa: N815
        ...,
        description=(
            "The floor. Named on every request even when it is not changing: a "
            "bound left out is ambiguous between 'leave it' and 'reset it', and "
            "the resolution that eventually happens is somebody's floor going "
            "back to 1 because a form field was blank."
        ),
    )
    maxReplicas: int = Field(  # noqa: N815
        ..., description="The ceiling. Named on every request, for the same reason.",
    )
    resourceVersion: str | None = Field(  # noqa: N815
        None,
        description=(
            "The autoscaler's resourceVersion as read. Sent back on the write, "
            "where it rides inside the merge patch and §0.4 is enforced twice."
        ),
    )


class BoundsRequest(MutationBody, BoundsPlanRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned, named. Recomputed "
            "server-side against the autoscaler as it is at write time — the "
            "replica count moves on its own, so 'this terminates two pods' can "
            "be a different number by the time the write lands."
        ),
    )


@router.post(_HPA + "/bounds/plan")
def get_bounds_plan(
    request: BoundsPlanRequest,
    namespace: str = Path(..., min_length=1, max_length=63, description="The namespace."),
    name: str = Path(..., min_length=1, max_length=253, description="The autoscaler."),
) -> dict[str, Any]:
    """§21 — the bounds, the replica count, and whether this autoscaler is scaling.

    Ungated, like §17's, §18's and §20's plans: one read and arithmetic. Writes
    nothing and records no audit row.

    A request that changes neither bound answers `200` with `blocked` set rather
    than `422` — the plan is the screen where the bounds are *decided*, and one
    that answered with an error alone would withhold the current bounds and the
    replica count at the moment those are the facts needed. The write refuses.
    """
    return hpa_admin.plan(namespace, name, request.model_dump(by_alias=True))


@router.put(_HPA + "/bounds")
def set_bounds(
    request: BoundsRequest,
    namespace: str = Path(..., min_length=1, max_length=63, description="The namespace."),
    name: str = Path(..., min_length=1, max_length=253, description="The autoscaler."),
) -> dict[str, Any]:
    """§21 — set the bounds, through the funnel, with the diff shown first.

    ``PUT`` rather than ``PATCH`` because §0.4's concurrency rule applies: the
    caller sends the version they were looking at, it is checked here — which is
    what produces a `409` carrying the bounds as they are *now* — and it rides
    inside the merge patch, so the API server refuses a stale write too.

    **`applied: true` means the bounds are stored**, and nothing more. Whether
    anything scales afterwards depends on the controller being able to read its
    metrics; `current.scaling_active` in the same response is that answer.
    """
    return hpa_admin.set_bounds(
        namespace,
        name,
        request.model_dump(by_alias=True, exclude={"acknowledgeConsequences", "dry_run"}),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


__all__ = ["router"]
