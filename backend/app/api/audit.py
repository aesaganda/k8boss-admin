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

Since console records (sign-ins, sign-outs, user administration) carry **no**
cluster attribution by design, the distinction stopped being a nicety: a trail
silently scoped to a cluster cannot contain a single login row, so an operator
filtering for "who signed in" would get an empty table and no indication that the
question was never asked. ``cluster_id=0`` therefore means "records with no
cluster", which is a filter the frontend can actually express — see the parameter
docstring.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.audit import export as export_service
from app.audit import recorder
from app.identity.dependencies import require_console_admin
from app.models import rfc3339, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audit"])

#: ``?cluster_id=0`` selects the records that have no cluster. Zero is not a
#: valid ``clusters.id`` (the column is a positive autoincrement), so it is free
#: to carry this meaning, and it exists because the frontend's request builder
#: appends ``cluster_id`` to every call and drops empty values — leaving no way
#: to express "unscoped" from a session that has a cluster selected. Without it,
#: console records would be permanently invisible in the UI.
UNSCOPED_CLUSTER_SENTINEL = 0


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
        ge=0,
        description=(
            "Narrow to one cluster. Unlike every other endpoint, omitting it means "
            "every cluster — an audit trail scoped silently to the selected cluster "
            "would answer 'did anyone touch prod' with a no it cannot support. "
            "Pass 0 for the records that belong to no cluster: every console "
            "sign-in, sign-out and user change is one of those."
        ),
    ),
    actor: str | None = Query(
        None,
        description=(
            "Exact match on the actor recorded with the write. This is the verified "
            "session username when application auth is enabled, or the advisory "
            "X-K8Boss-User value in legacy proxy mode."
        ),
    ),
    outcome: str | None = Query(
        None,
        description="applied | dry_run | denied | failed | conflict. Anything else is rejected.",
    ),
    since: str | None = Query(
        None, description="RFC 3339 lower bound on the record's timestamp.",
    ),
    until: str | None = Query(
        None,
        description=(
            "RFC 3339 upper bound on the record's timestamp. With `since`, this "
            "is the incident window. A window that selects nothing because "
            "`until` precedes `since` is rejected rather than answered with an "
            "empty table."
        ),
    ),
    category: str | None = Query(
        None,
        description=(
            "cluster | console. `cluster` is a write aimed at a Kubernetes API "
            "server; `console` is a sign-in, sign-out or console user change. "
            "Anything else is rejected."
        ),
    ),
    verb: str | None = Query(
        None,
        description=(
            "Exact match on the recorded verb: create, update, patch, delete for "
            "cluster writes; login, logout for console records."
        ),
    ),
    dry_run: bool | None = Query(
        None,
        description=(
            "Separate rehearsals from real attempts. Omitted means both — a "
            "review that silently excluded dry runs would miss the operator who "
            "previewed a change fifty times before making it."
        ),
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
        cluster_id=_resolve_cluster_filter(cluster_id),
        actor=actor,
        outcome=outcome,
        since=since,
        until=until,
        category=category,
        verb=verb,
        dry_run=dry_run,
    )


def _resolve_cluster_filter(cluster_id: int | None) -> Any:
    """Translate the sentinel. ``0`` -> "no cluster"; anything else passes through.

    ``recorder.query`` compares ``cluster_id`` to the column, and the column is
    NULL on console records. SQL equality against NULL is never true, so the
    sentinel has to become the ``IS NULL`` case rather than being passed down as
    a zero that quietly matches nothing — an empty table under a filter the
    caller believes is selecting sign-ins.
    """
    if cluster_id == UNSCOPED_CLUSTER_SENTINEL:
        return recorder.NO_CLUSTER
    return cluster_id


@router.get("/audit/verify")
def verify_audit(
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Verify only the newest N records. Faster on a long trail, and the "
            "result is always `partial`: a window cannot prove that the records "
            "before it still link back to the first one, and reporting it as "
            "`intact` would be a claim about records nobody read. Omit to verify "
            "the whole trail, which is the only form that can return `intact`."
        ),
    ),
) -> dict[str, Any]:
    """§10.3 — walk the audit hash chain and report what can be attested.

    Three verdicts, and the third is the one that matters:

    ``intact``
        Every record in the trail is chained, every hash recomputes, and the
        links run unbroken from the first record to the last.
    ``broken``
        A record's content no longer matches its hash, or a record cannot be
        reached from the first one. ``first_break`` names the id and what it
        means. This is evidence of modification, deletion, insertion or
        reordering **after** the record was committed.
    ``partial``
        No break was found, **and** the trail contains records this mechanism
        cannot speak for — records written before chaining existed, or one the
        writer had to store unchained. ``unchained`` counts them.

    ``partial`` is not a softer ``intact``. Records written before the chain
    existed are never back-filled: hashing them now would compute a hash over
    whatever they say today and store it as proof, turning "we do not know" into
    "verified" in the one table where that inversion does the most damage. So the
    count is reported and the verdict withheld.

    Read-only, and one read of the console's own database, so ``unavailable`` is
    always empty. A database failure raises the §1.3 error envelope rather than
    returning a reassuring report about a table that could not be read.
    """
    return recorder.verify_chain(limit=limit)


@router.get("/audit/export")
def export_audit(
    format: str = Query(
        "ndjson",
        description=(
            "ndjson | csv. `ndjson` is byte-faithful and carries the chain "
            "columns — it is the format to verify against. `csv` is flattened "
            "for a spreadsheet and defangs cells a spreadsheet would execute as "
            "a formula, so it is NOT byte-faithful. Anything else is rejected."
        ),
    ),
    cluster_id: int | None = Query(None, ge=0),
    actor: str | None = Query(None),
    outcome: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    category: str | None = Query(None),
    verb: str | None = Query(None),
    dry_run: bool | None = Query(None),
    _admin=Depends(require_console_admin),
) -> StreamingResponse:
    """§10.4 — the whole matching trail as one downloadable file.

    Administrator-only when application authentication is enabled, because this
    is the endpoint that returns every actor, every source address and every
    action in one request. In legacy proxy mode there is no console role to check
    and the proxy owns the decision, exactly as it does for every other endpoint.

    **The export is itself audited** before a byte is streamed. Bulk extraction
    of an audit trail is precisely the kind of act that trail exists to record,
    and recording it only on completion would mean an aborted download left no
    trace of the attempt.

    No row cap and no ``limit``. A truncated extract that looked complete would
    be unfalsifiable at the far end: the reader has no way to tell a short file
    from a quiet quarter.

    The filters are validated up front by :func:`app.audit.recorder.stream`'s
    shared parsing, so a malformed ``since`` is a §1.3 error envelope rather than
    a 200 that starts streaming and then dies mid-file — a half-written download
    the browser saves anyway.
    """
    chosen = export_service.validate_format(format)

    filters: dict[str, Any] = {
        "cluster_id": _resolve_cluster_filter(cluster_id),
        "actor": actor,
        "outcome": outcome,
        "since": since,
        "until": until,
        "category": category,
        "verb": verb,
        "dry_run": dry_run,
    }
    stated = ", ".join(
        f"{key}={value}" for key, value in sorted(filters.items()) if value is not None
    ) or "no filters (the whole trail)"

    # Everything is validated BEFORE the record is written, and the generator is
    # built before it too. `stream()` is deliberately not a generator so its
    # filter validation runs on the call — but running it after the audit write
    # left a permanent, append-only record saying `outcome: applied` for an
    # export that returned 422 and streamed zero bytes. The trail cannot be
    # corrected afterwards; there is no delete endpoint, and that is the point.
    rows = recorder.stream(**filters)

    recorder.record_console_event(
        verb="export",
        resource="audit",
        name=chosen,
        outcome="applied",
        detail=f"Exported the audit trail as {chosen} with {stated}.",
    )

    media_type, extension = export_service.MEDIA_TYPES[chosen]
    stamp = (rfc3339(utcnow()) or "").replace(":", "").replace("-", "")
    return StreamingResponse(
        export_service.render(chosen, rows),
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="k8boss-admin-audit-{stamp}.{extension}"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["router"]
