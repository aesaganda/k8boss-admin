"""
Operator portal endpoints (§16).

Thin, like every router in this package. The two reads delegate to
:mod:`app.services.portal`; the plan and the one write delegate to
:mod:`app.admin.portal`, which reaches a cluster only through
:func:`app.admin.mutate.mutate`. Nothing here builds a request body for the API
server and nothing here decides whether a write is allowed.

The gate is echoed onto the catalog envelope rather than fetched separately.
A page that had to ask a second endpoint whether subscribing is permitted would
render its Subscribe button before knowing — and rule 11.4 wants the control
disabled *with the reason* from the first paint, not corrected a round trip
later.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin import portal as portal_admin
from app.services import portal as portal_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["portal"])


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class SubscribeRequest(BaseModel):
    """A subscription request, in the shape the catalog row already carries."""

    model_config = ConfigDict(populate_by_name=True)

    package: str = Field(..., min_length=1, max_length=253)
    namespace: str = Field(..., min_length=1, max_length=253)
    channel: str | None = Field(None, min_length=1, max_length=253)
    catalog: str | None = Field(None, min_length=1, max_length=253)
    catalogNamespace: str | None = Field(None, min_length=1, max_length=253)  # noqa: N815
    installPlanApproval: Literal["Automatic", "Manual"] = "Automatic"  # noqa: N815
    startingCSV: str | None = Field(None, min_length=1, max_length=253)  # noqa: N815


class SubscribeWriteRequest(MutationBody, SubscribeRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned. Named rather than a "
            "boolean, like §13's acknowledgeLossy: a caller that acknowledged one "
            "set and then changed the namespace has to read the new one."
        ),
    )


def _gate() -> dict[str, Any]:
    """The §16 feature gate, in the ``enabled``/``enabledDetail`` shape §14 uses."""
    state = portal_admin.enabled_state()
    return {"enabled": state["enabled"], "enabledDetail": state["detail"]}


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

@router.get("/portal/catalog")
def get_portal_catalog(
    limit: int = Query(
        500,
        ge=1,
        le=5000,
        description=(
            "Upper bound on each of the three listings. What a bound left out is "
            "reported in truncated[], never behind a paging control: the package "
            "server does not implement continuation, so a cursor this response "
            "handed back would be one it could not honour."
        ),
    ),
) -> dict[str, Any]:
    """§16 — every package this cluster's catalogs offer. A live read."""
    body = portal_service.catalog(limit=limit)
    body.update(_gate())
    return body


@router.get("/portal/subscriptions")
def get_portal_subscriptions(
    namespace: str | None = Query(
        None,
        min_length=1,
        max_length=253,
        description="Omit to read every namespace this deployment may read.",
    ),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """§16 — Subscriptions joined to what OLM actually installed for each."""
    body = portal_service.installed_operators(namespace=namespace, limit=limit)
    body.update(_gate())
    return body


# --------------------------------------------------------------------------- #
# The plan, and the one write
# --------------------------------------------------------------------------- #

@router.post("/portal/subscriptions/plan")
def get_portal_plan(request: SubscribeRequest) -> dict[str, Any]:
    """§16 — the Subscription that would be created, and what will stop it.

    Ungated deliberately, like §14's router plan: everything it does is a read,
    and an operator deciding whether to set ``ADMIN_PORTAL_INSTALL_ENABLED`` has
    to be able to see what it would let the console create. Writes nothing and
    records no audit row.
    """
    return portal_admin.plan(request.model_dump(by_alias=True))


@router.post("/portal/subscriptions")
def create_portal_subscription(request: SubscribeWriteRequest) -> dict[str, Any]:
    """§16 — create one Subscription, through the ordinary funnel.

    ``applied: true`` means the Subscription object exists. It is not a claim
    that an operator is installed: OLM does that afterwards, on its own
    schedule, and only if the namespace and the approval strategy let it. The
    response carries the plan's consequences and the CSV OLM is expected to
    install so the caller can say so rather than implying otherwise. §1.5's own
    ``warnings`` key is left alone — it carries the API server's Warning headers
    for this create, which is a different thing and must not be overwritten.
    """
    return portal_admin.subscribe(
        request.model_dump(by_alias=True),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )
