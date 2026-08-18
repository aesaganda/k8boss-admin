"""
Writing and reading the audit trail (§10).

``record`` is called by :func:`app.admin.mutate.mutate` on every terminal state of
every write, and directly by the two privileged *reads* that §8 treats as writes
(revealing a Secret, opening an exec session). ``query`` backs
``GET /api/audit``.

Three things this module is careful about:

**The row is assembled from context, not from arguments.** Actor, source IP and
cluster come from the request context (:mod:`app.k8s.context`), not from the
caller's parameters. A caller that had to pass them is a caller that can pass the
wrong ones, and an audit row attributing a write to the wrong operator is worse
than one attributing it to ``anonymous``.

**``target`` is filtered to an allowlist.** It is caller-supplied, it is stored
verbatim and it is echoed back in §10. An allowlist means a future call site
cannot put a token, a request body or a Secret value into the audit table by
passing a richer dict — a table that is, by design, kept for a long time and read
by more people than can read the cluster it describes.

**A failed INSERT never fails the write.** See the package docstring: by the time
a real write reaches this function it has already happened.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import func, select

from app import database
from app.errors import Invalid
from app.k8s.client import manager
from app.k8s.context import get_current_cluster_id, get_current_source_ip, get_current_user
from app.models import AuditRecord, Cluster, utcnow
from app.resources.envelope import envelope

logger = logging.getLogger(__name__)

#: The only keys that may appear in an audit ``target``. Anything else a caller
#: passes is dropped, not stored: this table outlives the cluster it describes
#: and is read by people who cannot read that cluster.
TARGET_FIELDS: tuple[str, ...] = (
    "group", "version", "resource", "namespace", "name", "subresource",
)

#: §10's closed set. Validated on the way in *and* on the way out (as a filter),
#: because a typo'd outcome would be stored happily by SQLite and would then be
#: invisible to every filter the UI offers — a record that exists and cannot be
#: found.
OUTCOMES: frozenset[str] = frozenset({"applied", "dry_run", "denied", "failed", "conflict"})

#: Bounds on the free-text columns. Both are Text, so this is not a schema
#: limit — it is a limit on how much of a Kubernetes error message (which can
#: embed an entire admission webhook response) one row may carry.
_MAX_DETAIL = 4000
_MAX_ERROR = 4000

#: §10 paging. The upper bound is the contract's; the default is what the audit
#: page requests.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def _clip(value: str | None, limit: int) -> str | None:
    """Bound a free-text column, marking the truncation rather than hiding it."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated, {len(text)} characters]"


def _clean_target(target: dict[str, Any] | None) -> dict[str, Any]:
    """The allowlisted, JSON-safe target.

    Every allowed key is present even when null, so a consumer can read
    ``row.target.namespace`` unconditionally — the same reasoning as
    :meth:`app.errors.AdminError.to_envelope`. Values are coerced to strings
    because this column is JSON on both SQLite and PostgreSQL and a stray
    non-serialisable object would fail the INSERT, i.e. would lose the record.
    """
    source = target or {}
    cleaned: dict[str, Any] = {}
    for field in TARGET_FIELDS:
        value = source.get(field)
        cleaned[field] = None if value is None else str(value)
    dropped = sorted(set(source) - set(TARGET_FIELDS))
    if dropped:
        # Logged, not stored. A call site passing extra keys is a bug worth
        # seeing, and silently accepting them is how a credential eventually
        # lands in this table.
        logger.warning(
            "Dropped non-allowlisted audit target field(s) %s from a %s record.",
            ", ".join(dropped), source.get("resource") or "?",
        )
    return cleaned


def _cluster_identity(db) -> tuple[int | None, str | None]:
    """``(cluster_id, cluster_name)`` for the cluster this request is about.

    The name is denormalised onto the row because a cluster can be de-registered
    and an audit entry that then renders as "cluster 7" is unreadable exactly
    when it is needed. Resolution goes through the client manager so that a
    request which named no cluster is attributed to the same cluster it actually
    talked to, rather than to null — an audit row that does not say which cluster
    was written to is not an audit row.
    """
    cluster_id = manager.resolve_cluster_id(get_current_cluster_id())
    if cluster_id is None:
        return None, None
    try:
        cluster = db.get(Cluster, cluster_id)
    except Exception:  # noqa: BLE001 - never let naming a cluster lose the record
        logger.debug("Could not read cluster %s for audit attribution", cluster_id,
                     exc_info=True)
        return cluster_id, None
    return cluster_id, (cluster.name if cluster is not None else None)


def record(
    *,
    verb: str,
    target: dict[str, Any],
    dry_run: bool,
    outcome: str,
    detail: str | None = None,
    diff_digest: str | None = None,
    error: str | None = None,
) -> int | None:
    """Append one audit record. Returns its id, or ``None`` if it could not be written.

    ``None`` rather than raising, and ``None`` rather than a sentinel id: the
    caller puts this straight into the §1.5 response's ``auditId``, where null
    tells the operator "the change happened and we failed to record it" — which
    is true, actionable and impossible to confuse with an id.

    Every write reaches this function, including the refused ones. ``outcome``
    carries which: ``applied`` (it reached the cluster), ``dry_run`` (it was
    projected), ``denied`` (the mutations gate or preflight refused it),
    ``conflict`` (a resourceVersion mismatch), ``failed`` (anything else).
    """
    if outcome not in OUTCOMES:
        # A programming error, and one that would otherwise produce a record no
        # filter can find. Raised rather than coerced: the coercion would have to
        # pick an outcome, and inventing "failed" for a write that succeeded is
        # the kind of quiet lie this table exists to make impossible.
        raise ValueError(
            f"{outcome!r} is not a §10 audit outcome. Use one of: "
            + ", ".join(sorted(OUTCOMES))
        )

    db = None
    try:
        # Resolved off the module rather than imported by name, so a test (or a
        # second engine) that swaps app.database.SessionLocal is followed here
        # too. The mutation funnel runs outside any FastAPI dependency, so there
        # is no injected session to use.
        db = database.SessionLocal()
        cluster_id, cluster_name = _cluster_identity(db)
        row = AuditRecord(
            ts=utcnow(),
            actor=get_current_user()[:255],
            source_ip=get_current_source_ip(),
            cluster_id=cluster_id,
            cluster_name=cluster_name,
            verb=verb,
            target=_clean_target(target),
            dry_run=bool(dry_run),
            outcome=outcome,
            detail=_clip(detail, _MAX_DETAIL),
            diff_digest=diff_digest,
            error=_clip(error, _MAX_ERROR),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            "audit id=%s %s %s/%s %s outcome=%s dry_run=%s actor=%s",
            row.id, verb, row.target.get("resource"), row.target.get("name"),
            f"in {row.target.get('namespace')}" if row.target.get("namespace") else "cluster-wide",
            outcome, dry_run, row.actor,
        )
        return row.id
    except Exception as e:  # noqa: BLE001 - the write already happened; see the module docstring
        logger.error(
            "FAILED TO WRITE AUDIT RECORD for %s %s (outcome=%s, dry_run=%s, actor=%s): %s",
            verb, (target or {}).get("resource"), outcome, dry_run, get_current_user(), e,
            exc_info=True,
        )
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - nothing left to salvage
                logger.debug("Audit session rollback failed", exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()


def _parse_since(since: Any) -> datetime.datetime | None:
    """``?since=`` as the naive UTC the schema stores.

    Rejected rather than ignored when unparseable: ignoring it returns the whole
    trail under a filter the caller believes is applied, and "no writes since
    Tuesday" is precisely the answer nobody may be given wrongly.
    """
    if since is None or since == "":
        return None
    if isinstance(since, datetime.datetime):
        parsed = since
    else:
        text = str(since).strip()
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as e:
            raise Invalid(
                f"`since` is not an RFC 3339 timestamp: {since!r}.",
                hint="Use a form like 2026-08-18T09:14:00Z.",
                context={"parameter": "since", "value": str(since)},
            ) from e
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_cursor(cursor: Any) -> int | None:
    """The opaque §10 cursor: the id of the last row of the previous page."""
    if cursor is None or cursor == "":
        return None
    try:
        return int(str(cursor))
    except (TypeError, ValueError) as e:
        raise Invalid(
            f"`cursor` is not a valid audit cursor: {cursor!r}.",
            hint="Pass back the `continue` value from the previous page, unmodified.",
            context={"parameter": "cursor", "value": str(cursor)},
        ) from e


def query(
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    cluster_id: int | None = None,
    actor: str | None = None,
    outcome: str | None = None,
    since: Any = None,
) -> dict[str, Any]:
    """§10 ``GET /api/audit`` as the §1.2 envelope.

    Newest first, paged by descending id rather than by offset: an audit trail is
    appended to while it is being read, and an OFFSET page would skip or repeat
    rows as soon as one write landed between two requests. The cursor is the last
    id of the previous page, so a concurrent insert cannot shift the window.

    ``remaining`` is counted only when there *is* a next page — the count is one
    extra indexed query, and paying for it on the last page (the common case)
    buys a zero the caller can already infer from a null ``continue``.

    An unrecognised ``outcome`` is rejected, not ignored: ignoring it returns
    every record under a filter the caller believes is applied.
    """
    bounded = max(1, min(int(limit), MAX_LIMIT))
    if outcome is not None and outcome not in OUTCOMES:
        raise Invalid(
            f"{outcome!r} is not an audit outcome.",
            hint="Use one of: " + ", ".join(sorted(OUTCOMES)) + ".",
            context={"parameter": "outcome", "value": outcome},
        )
    after_id = _parse_cursor(cursor)
    since_ts = _parse_since(since)

    def _filtered(statement):
        if cluster_id is not None:
            statement = statement.where(AuditRecord.cluster_id == cluster_id)
        if actor:
            statement = statement.where(AuditRecord.actor == actor)
        if outcome:
            statement = statement.where(AuditRecord.outcome == outcome)
        if since_ts is not None:
            statement = statement.where(AuditRecord.ts >= since_ts)
        return statement

    db = database.SessionLocal()
    try:
        statement = _filtered(select(AuditRecord))
        if after_id is not None:
            statement = statement.where(AuditRecord.id < after_id)
        # limit + 1: the extra row is how we know whether a next page exists
        # without counting the whole table on every request.
        rows = list(
            db.execute(statement.order_by(AuditRecord.id.desc()).limit(bounded + 1))
            .scalars()
            .all()
        )

        has_more = len(rows) > bounded
        page = rows[:bounded]
        next_cursor = str(page[-1].id) if (has_more and page) else None

        remaining: int | None = None
        if next_cursor is not None:
            # COUNT, not len(list(...)): the point of the cursor is that this
            # table grows without bound, and materialising every remaining id to
            # count them would make the second page of a busy trail the slowest
            # request the console makes.
            countable = _filtered(
                select(func.count()).select_from(AuditRecord)
            ).where(AuditRecord.id < page[-1].id)
            remaining = int(db.execute(countable).scalar() or 0)

        return envelope(
            [row.to_row_dict() for row in page],
            cont=next_cursor,
            remaining=remaining,
        )
    finally:
        db.close()


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "OUTCOMES",
    "TARGET_FIELDS",
    "query",
    "record",
]
