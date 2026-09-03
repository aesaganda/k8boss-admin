"""
Event endpoints (§5).

Thin, like every router here: four query parameters, one call, one envelope.
Everything that decides what the answer *is* — the bounded scan, the two
timestamp spellings, the local sort — lives in :mod:`app.services.events`,
which is where the rest of the typed read models are and where its docstring
explains the three decisions that make the answer correct rather than plausible.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from app.services import events as events_service
from app.services.events import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
def list_events(
    namespace: str | None = Query(
        None, description="Restrict to one namespace. Omit for the whole cluster."
    ),
    involved_object_kind: str | None = Query(
        None, alias="involvedObjectKind",
        description="Kind of the object the event is about, e.g. `Pod`. Case-sensitive.",
    ),
    involved_object_name: str | None = Query(
        None, alias="involvedObjectName",
        description="Name of the object the event is about. Exact match.",
    ),
    event_type: Literal["Normal", "Warning"] | None = Query(
        None, alias="type",
        description=(
            "`Normal` or `Warning`. Rejected rather than ignored if it is neither: "
            "an unrecognised value silently dropped would return every event under "
            "a heading that says the list is filtered."
        ),
    ),
    limit: int = Query(
        DEFAULT_LIMIT, ge=1, le=MAX_LIMIT,
        description=(
            "How many of the newest events to return. Bounded above by the scan "
            "budget, because a limit larger than what is scanned could not be "
            "honestly filled."
        ),
    ),
) -> dict[str, Any]:
    """§5 events, newest ``last_seen`` first, as the §1.2 envelope."""
    return events_service.list_events(
        namespace=namespace,
        involved_kind=involved_object_kind,
        involved_name=involved_object_name,
        event_type=event_type,
        limit=limit,
    )


__all__ = ["list_events", "router"]
