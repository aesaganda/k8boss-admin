"""
The audit endpoint (§10). One route, read-only, and read-only by construction:
§10 states there is no delete endpoint, and :class:`app.models.AuditRecord`
refuses an UPDATE or a DELETE at the ORM level so that statement is enforced
rather than merely intended.

**``cluster_id`` means something different here, and deliberately so.** Everywhere
else in this API an omitted ``cluster_id`` means "the active cluster" (§1.1). On
the audit trail it means *every* cluster. The question this page answers is "what
has been done, and by whom", and silently scoping it to whichever cluster the
switcher happens to be on would answer "has anyone touched production?" with a
confident no while the row sits two clusters away. Passing ``cluster_id``
explicitly still narrows it, which is the §10 filter.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from app.audit import recorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
def get_audit(
    limit: int = Query(
        recorder.DEFAULT_LIMIT,
        ge=1,
        le=recorder.MAX_LIMIT,
        description="Page size. The trail is unbounded; the page is not.",
    ),
    cursor: str | None = Query(
        None,
        description=(
            "The `continue` value from the previous page, unmodified. Paging is by "
            "descending id rather than by offset, so a write landing between two "
            "requests cannot make a row skip or repeat."
        ),
    ),
    cluster_id: int | None = Query(
        None,
        description=(
            "Narrow to one cluster. Unlike every other endpoint, omitting it means "
            "every cluster — an audit trail scoped silently to the selected cluster "
            "would answer 'did anyone touch prod' with a no it cannot support."
        ),
    ),
    actor: str | None = Query(
        None,
        description=(
            "Exact match on the X-K8Boss-User value recorded with the write. "
            "Advisory, as §10 says: there is no auth layer in front of this console, "
            "so the actor is attribution, not identity."
        ),
    ),
    outcome: str | None = Query(
        None,
        description="applied | dry_run | denied | failed | conflict. Anything else is rejected.",
    ),
    since: str | None = Query(
        None, description="RFC 3339 lower bound on the record's timestamp.",
    ),
) -> dict[str, Any]:
    """§10 audit listing, newest first, in the §1.2 envelope.

    ``unavailable`` is always empty and ``partial`` always false here: this
    endpoint makes exactly one read, against the console's own database, so it
    either answered or it failed. A database failure raises and is rendered as
    the §1.3 error envelope — strictly more useful than a 200 with an empty table
    that reads as "nothing has ever been written to your clusters".
    """
    return recorder.query(
        limit=limit,
        cursor=cursor,
        cluster_id=cluster_id,
        actor=actor,
        outcome=outcome,
        since=since,
    )


__all__ = ["router"]
