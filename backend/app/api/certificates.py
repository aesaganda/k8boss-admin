"""
Certificate signing requests (§25) — reading one before deciding it.

Thin, like every router here: parse, call, envelope. The PKCS#10 decode, the
consequences and the two preflights live in :mod:`app.admin.csr`, which reaches
a cluster only through :func:`app.admin.mutate.mutate`.

There is no listing endpoint here and deliberately will not be one.
CertificateSigningRequests are browsed through §4's generic path, which already
returns the typed §25 row because `certificatesigningrequest_row` is registered
on the backend — a second listing in this file would be a second shaping of the
same object, free to drift from the first and impossible to notice from outside.

The gate is echoed onto the plan rather than fetched separately, for §16's
reason: a dialog that had to ask a second endpoint whether writing is permitted
would render its button before knowing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.admin import csr as csr_admin
from app.api.bodies import MutationBody

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["certificates"])

_CSR = "/certificates/signing-requests/{name}"


class DecisionPlanRequest(BaseModel):
    """Which decision the plan should describe."""

    model_config = ConfigDict(populate_by_name=True)

    decision: str = Field(
        ...,
        description=(
            "Approved or Denied — the condition type the API uses, not a verb. "
            "The consequences differ between them: approving is where the "
            "certificate's own subject matters, and denying is where the node "
            "that will not join does."
        ),
    )


class DecisionRequest(MutationBody, DecisionPlanRequest):
    """The plan's body plus the three fields that make it a write."""

    resourceVersion: str | None = Field(  # noqa: N815
        None,
        description=(
            "The request's resourceVersion as read. Sent back on the write, "
            "where it rides inside the object PUT to the approval subresource "
            "and §0.4 is enforced twice."
        ),
    )
    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned, named. Recomputed "
            "server-side against the request as it is at write time."
        ),
    )


@router.post(_CSR + "/plan")
def get_decision_plan(
    request: DecisionPlanRequest,
    name: str = Path(..., min_length=1, max_length=253, description="The request's name."),
) -> dict[str, Any]:
    """§25 — the decoded request, and what deciding it would mean.

    Ungated and unaudited: one read and a PKCS#10 decode. Writes nothing and
    records no audit row.

    The decoded `subject` is the field this endpoint exists for. `spec.username`
    says who *asked*; the subject says who they asked to **become**, and the two
    are different fields that `kubectl get csr` shows only the first of. A
    request whose organizations include `system:masters` is a cluster-admin
    credential, and on every other screen it looks like a kubelet renewal.

    A request that has already been decided answers `200` with `blocked` set
    rather than `422`: this is the screen where the request is read, and an
    error alone would withhold the decoded subject at the moment somebody is
    working out what happened.
    """
    return csr_admin.plan(name, request.decision)


@router.put(_CSR)
def decide_request(
    request: DecisionRequest,
    name: str = Path(..., min_length=1, max_length=253, description="The request's name."),
) -> dict[str, Any]:
    """§25 — approve or deny, through the funnel, with the diff shown first.

    `PUT` because §0.4 applies and because the approval subresource takes the
    whole object: the caller sends the version they were looking at, it is
    checked here — which is what produces a `409` carrying the request's current
    state — and it rides inside the object, so the API server refuses a stale
    write too.

    Two permissions are needed and the second is the one people miss: the funnel
    preflights `update certificatesigningrequests/approval`, and the apply step
    separately preflights **`approve` on `certificates.k8s.io/signers`, named for
    this request's signerName**, which the API server's own admission plugin
    requires. Holding the first without the second enables a button that the API
    server then refuses.

    **`applied: true` means the condition is recorded**, and nothing more. A
    signer still has to act, and on a cluster with no signer for this signerName
    the request stays `Approved` with no certificate — indefinitely. `Issued` is
    the state that says a certificate exists.
    """
    return csr_admin.decide(
        name,
        request.decision,
        dry_run=request.dry_run,
        resource_version=request.resourceVersion,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


__all__ = ["router"]
