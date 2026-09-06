"""
Quota advice endpoints (§29) — answered before the write, not as a 403 after it.

Thin, like every router here: it parses the request and delegates to
:mod:`app.services.quota`, which owns the arithmetic and its nulls.

**Both endpoints are reads.** They are ungated and unaudited, like §17's and
§18's plans, and they touch no write path — the whole point is to answer from
`spec.hard`, `status.used` and a LimitRange's defaults *before* a manifest
exists. Editing a quota or a LimitRange is §4's `PUT` through the single
mutation funnel.

**`POST` for the preview**, because the proposed workload is a body rather than
a path: replicas and a container list do not belong in a query string, and §17's
project plan and §18's Pod Security plan set the precedent for a POST that
writes nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.services.quota import advise

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["quota"])


class ContainerRequest(BaseModel):
    """One container's declared requests and limits, as the operator would type them.

    Both maps are the API's own `{resource: quantity}` shape, so `cpu: "500m"`
    and `memory: "1Gi"` are written exactly as they would be in the manifest.
    An **omitted** resource is the interesting case and is not the same as a
    zero: omitted means the LimitRange has to supply it or the pod is refused
    with "must specify …", while `"0"` is a declared value that satisfies the
    rule and consumes no headroom.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(None, max_length=253)
    requests: dict[str, str] | None = None
    limits: dict[str, str] | None = None


class QuotaPreviewRequest(BaseModel):
    """A workload that does not exist yet, described well enough to be counted."""

    model_config = ConfigDict(populate_by_name=True)

    replicas: int = Field(
        1, ge=0, le=10000,
        description=(
            "How many pods the workload would create. Multiplies every "
            "container total, because a quota counts pods and not Deployments."
        ),
    )
    containers: list[ContainerRequest] = Field(
        default_factory=list,
        description=(
            "Every container in the pod. A pod's demand is the sum across them, "
            "so a sidecar nobody counted is the usual reason an estimate and an "
            "admission decision disagree."
        ),
    )


@router.get("/quota/{namespace}")
def get_quota_advice(
    namespace: str = Path(..., min_length=1, max_length=63),
) -> dict[str, Any]:
    """What bounds this namespace, and what every pod in it must declare (§29).

    `quotas: null` means the listing was refused — **never `[]`**, which is the
    answer that says nothing bounds this namespace and every workload is
    admitted.

    `mandatory[]` is the list this endpoint exists for. A quota that bounds a
    compute resource makes that resource compulsory on every container in the
    namespace, and a pod omitting it is refused with *"must specify …"* even
    when the quota is barely used. `containerDefaults` is what the LimitRanges
    supply toward that; a mandatory resource with no default is the finding.
    """
    return advise(namespace)


@router.post("/quota/{namespace}/preview")
def preview_quota(
    request: QuotaPreviewRequest,
    namespace: str = Path(..., min_length=1, max_length=63),
) -> dict[str, Any]:
    """Would this workload be admitted, and which limit refuses it (§29)?

    **Advice, not admission.** The API server admits; §4's dry-run create asks
    it and gets the authoritative answer for a manifest that exists. This
    answers from arithmetic for one that does not, and it says the two things a
    403 does not: how much room is left, and *which* of a quota's limits is the
    tight one.

    `verdict` is `admitted`, `refused` or **`unknown`**, and the third is a real
    answer rather than a failure. It is returned whenever the arithmetic cannot
    be completed honestly — a `status.used` the quota controller has not written
    yet, or a scoped quota whose applicability to a workload that does not exist
    this console will not guess at. An advisor that resolved either to
    `admitted` would give its roomiest answer at the moment it knows least.

    `unsetMandatory[]` is a refusal by a different rule and is listed
    separately: the container omits a resource some quota bounds, no LimitRange
    supplies it, and the fix is a LimitRange rather than more headroom.
    """
    return advise(namespace, request.model_dump(by_alias=True))


__all__ = ["router"]
