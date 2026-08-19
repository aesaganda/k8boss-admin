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

**A row that cannot be chained is still written.** Rows are hash-chained
(:mod:`app.audit.integrity`), and a concurrent writer that took the chain tip
first makes the INSERT fail on the UNIQUE constraint. That is retried against a
fresh tip. If the retries are exhausted, the record is written *unchained* rather
than dropped: losing the link costs the ability to prove that one row was not
altered, and losing the row costs the knowledge that the action happened at all.
``GET /api/audit/verify`` reports it as ``unchained``, so the weaker guarantee is
visible rather than assumed.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import database
from app.audit import integrity
from app.errors import Invalid
from app.k8s.client import manager
from app.k8s.context import get_current_cluster_id, get_current_source_ip, get_current_user
from app.models import (
    CATEGORIES,
    CATEGORY_CLUSTER,
    CATEGORY_CONSOLE,
    AuditRecord,
    Cluster,
    utcnow,
)
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

#: How many times an INSERT is retried when a concurrent writer claimed the chain
#: tip first. Each retry costs one SELECT and one INSERT, and the contention it
#: resolves is between console replicas writing audit rows — a rate measured in
#: writes per minute, not per millisecond. Five is far more headroom than that
#: needs; the number exists so the loop is bounded rather than because five is
#: significant.
CHAIN_RETRIES = 5


class _NoCluster:
    """Sentinel for "records that belong to no cluster".

    A distinct object rather than ``None``, because ``None`` already means "do
    not filter on cluster at all" and the two are opposite questions: one returns
    every record, the other returns only the console ones. Collapsing them would
    make a filter for "who signed in" return the whole trail, which reads as a
    working filter right up until somebody counts the rows.

    It cannot be a plain ``0`` either: SQL equality against NULL is never true,
    so ``cluster_id == 0`` matches nothing and would answer "nobody has ever
    signed in".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<NO_CLUSTER>"


#: Pass as ``cluster_id`` to select only records with no cluster attribution.
NO_CLUSTER = _NoCluster()


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
    cluster_scoped: bool = True,
    category: str = CATEGORY_CLUSTER,
    actor: str | None = None,
    source_ip: str | None = None,
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

    ``actor`` and ``source_ip`` default to the request context and should stay
    there for every cluster write — see the module docstring on why a caller that
    *can* pass an actor is a caller that can pass the wrong one. The one case
    that must override is a failed sign-in: the context actor is ``anonymous``
    (there is no session yet), and a trail that records every rejected login as
    ``anonymous`` cannot answer "whose account is being guessed at", which is the
    only question those rows exist for. The attempted username is stored, and
    §10 is explicit that on a ``denied`` console record it is a *claim* by the
    caller rather than a verified identity.
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
    if category not in CATEGORIES:
        raise ValueError(
            f"{category!r} is not a §10 audit category. Use one of: "
            + ", ".join(sorted(CATEGORIES))
        )

    fields = _row_fields(
        verb=verb, target=target, dry_run=dry_run, outcome=outcome, detail=detail,
        diff_digest=diff_digest, error=error, cluster_scoped=cluster_scoped,
        category=category, actor=actor, source_ip=source_ip,
    )
    if fields is None:
        return None
    return _insert_with_chain_retry(fields)


def _row_fields(
    *,
    verb: str,
    target: dict[str, Any],
    dry_run: bool,
    outcome: str,
    detail: str | None,
    diff_digest: str | None,
    error: str | None,
    cluster_scoped: bool,
    category: str,
    actor: str | None,
    source_ip: str | None,
) -> dict[str, Any] | None:
    """Everything the row will hold, resolved once.

    Built before the insert loop rather than inside it so a retry re-uses the
    same facts. Re-deriving them per attempt would let a row's timestamp drift by
    the duration of the contention, and two attempts at the same event would then
    be two different events as far as the hash is concerned.
    """
    db = None
    try:
        db = database.SessionLocal()
        cluster_id, cluster_name = (
            _cluster_identity(db) if cluster_scoped else (None, None)
        )
    except Exception as e:  # noqa: BLE001 - naming the cluster must not lose the row
        logger.error(
            "Could not resolve the cluster for a %s audit record (%s); recording "
            "it without cluster attribution rather than dropping it.", verb, e,
        )
        cluster_id, cluster_name = None, None
    finally:
        if db is not None:
            db.close()

    return {
        "ts": utcnow(),
        "category": category,
        "actor": (actor if actor is not None else get_current_user())[:255],
        "source_ip": source_ip if source_ip is not None else get_current_source_ip(),
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "verb": verb,
        "target": _clean_target(target),
        "dry_run": bool(dry_run),
        "outcome": outcome,
        "detail": _clip(detail, _MAX_DETAIL),
        "diff_digest": diff_digest,
        "error": _clip(error, _MAX_ERROR),
    }


def _insert_with_chain_retry(fields: dict[str, Any]) -> int | None:
    """INSERT one row, retrying when a concurrent writer took the chain tip.

    The UNIQUE constraint on ``prev_hash`` turns a chain fork into an
    ``IntegrityError`` here (see :mod:`app.audit.integrity`). A fresh
    ``AuditRecord`` is constructed per attempt because the previous instance
    carries the losing hashes and is attached to a session that has been rolled
    back; re-adding it would re-submit the same rejected values.

    After the retries, the row is written **unchained** rather than abandoned.
    Which way to be wrong is the whole decision: an unchained row costs the
    ability to prove that one record was not later edited, and ``verify`` says so
    out loud. A dropped row costs the knowledge that the action happened, and
    nothing says anything at all.
    """
    for attempt in range(CHAIN_RETRIES + 1):
        last = attempt == CHAIN_RETRIES
        db = None
        try:
            db = database.SessionLocal()
            row = AuditRecord(**fields)
            if last:
                # Give up on the link, keep the record. Marked explicitly so the
                # chaining listener leaves it alone instead of re-entering the
                # contention that has already failed CHAIN_RETRIES times.
                setattr(row, integrity.UNCHAINED_ATTR, True)
            db.add(row)
            db.commit()
            # Captured immediately after the COMMIT and before anything else can
            # fail. `record()` returning None means "the record was not written",
            # and the caller puts that straight into the §1.5 response's
            # `auditId` — so a refresh or a log call failing *after* a durable
            # commit would tell the operator their change went unrecorded while
            # the row sat in the table, verified and findable. Wrong in the
            # direction that makes somebody go looking for evidence they already
            # have.
            written_id = row.id
            if last:
                logger.error(
                    "Audit record %s was written OUTSIDE the hash chain after %d "
                    "contended attempts. The record is intact and complete; what "
                    "is missing is the cryptographic link that would prove it was "
                    "not altered afterwards. GET /api/audit/verify reports it as "
                    "unchained.", written_id, CHAIN_RETRIES,
                )
            try:
                logger.info(
                    "audit id=%s %s %s %s/%s %s outcome=%s dry_run=%s actor=%s",
                    written_id, fields["category"], fields["verb"],
                    fields["target"].get("resource"), fields["target"].get("name"),
                    f"in {fields['target'].get('namespace')}"
                    if fields["target"].get("namespace") else "cluster-wide",
                    fields["outcome"], fields["dry_run"], fields["actor"],
                )
            except Exception:  # noqa: BLE001 - a log line must not lose a record
                logger.debug("Could not log the audit record summary", exc_info=True)
            return written_id
        except IntegrityError:
            _rollback(db)
            if last:
                # The unchained attempt collided too, which cannot be the chain
                # constraint (that row carries NULLs, and NULLs do not collide).
                # Fall through to the generic handler's reporting.
                logger.error(
                    "Audit INSERT failed on a constraint even with chaining "
                    "disabled; the record is lost.", exc_info=True,
                )
                return None
            logger.info(
                "Audit chain tip was taken by a concurrent writer; retrying "
                "(attempt %d of %d).", attempt + 1, CHAIN_RETRIES,
            )
        except Exception as e:  # noqa: BLE001 - the write already happened
            _rollback(db)
            logger.error(
                "FAILED TO WRITE AUDIT RECORD for %s %s (outcome=%s, dry_run=%s, "
                "actor=%s): %s",
                fields["verb"], fields["target"].get("resource"), fields["outcome"],
                fields["dry_run"], fields["actor"], e, exc_info=True,
            )
            return None
        finally:
            if db is not None:
                db.close()
    return None


def _rollback(db) -> None:
    if db is None:
        return
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 - nothing left to salvage
        logger.debug("Audit session rollback failed", exc_info=True)


#: The synthetic group under which console-scoped records are filed. It is not a
#: Kubernetes group and no cluster serves it; it exists so a console record has
#: the same shaped ``target`` as a cluster write and the audit page needs one
#: renderer rather than two.
CONSOLE_GROUP = "k8boss-admin.io"
CONSOLE_VERSION = "v1"


def record_console_event(
    *,
    verb: str,
    resource: str,
    name: str | None,
    outcome: str,
    detail: str | None = None,
    error: str | None = None,
    actor: str | None = None,
    source_ip: str | None = None,
) -> int | None:
    """Append one console-scoped record: a sign-in, a sign-out, a user change.

    Deliberately the same table, the same outcome vocabulary and the same
    append-only guarantees as a cluster write. A separate table would mean an
    incident review has to remember there are two, and the review that forgets is
    the one that concludes nobody signed in.

    ``dry_run`` is always false — there is no such thing as a rehearsed sign-in —
    and ``cluster_scoped`` is false, so the row carries no cluster attribution
    rather than being attributed to whichever cluster the switcher happened to be
    on. That is why ``GET /api/audit`` had to stop scoping to a cluster by
    default: a console record scoped to a cluster it has nothing to do with is
    unfindable.
    """
    return record(
        verb=verb,
        target={
            "group": CONSOLE_GROUP,
            "version": CONSOLE_VERSION,
            "resource": resource,
            "namespace": None,
            "name": name,
        },
        dry_run=False,
        outcome=outcome,
        detail=detail,
        error=error,
        cluster_scoped=False,
        category=CATEGORY_CONSOLE,
        actor=actor,
        source_ip=source_ip,
    )


def _parse_timestamp(value: Any, parameter: str) -> datetime.datetime | None:
    """``?since=`` / ``?until=`` as the naive UTC the schema stores.

    Rejected rather than ignored when unparseable: ignoring it returns the whole
    trail under a filter the caller believes is applied, and "no writes since
    Tuesday" is precisely the answer nobody may be given wrongly.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as e:
            raise Invalid(
                f"`{parameter}` is not an RFC 3339 timestamp: {value!r}.",
                hint="Use a form like 2026-08-18T09:14:00Z.",
                context={"parameter": parameter, "value": str(value)},
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
    cluster_id: Any = None,
    actor: str | None = None,
    outcome: str | None = None,
    since: Any = None,
    until: Any = None,
    category: str | None = None,
    verb: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """§10 ``GET /api/audit`` as the §1.2 envelope.

    Newest first, paged by descending id rather than by offset: an audit trail is
    appended to while it is being read, and an OFFSET page would skip or repeat
    rows as soon as one write landed between two requests. The cursor is the last
    id of the previous page, so a concurrent insert cannot shift the window.

    ``remaining`` is counted only when there *is* a next page — the count is one
    extra indexed query, and paying for it on the last page (the common case)
    buys a zero the caller can already infer from a null ``continue``.

    Every filter is validated rather than ignored. An unrecognised ``outcome``,
    ``category`` or timestamp is rejected, because ignoring one returns the whole
    trail under a filter the caller believes is applied — and "no writes since
    Tuesday" is precisely the answer nobody may be given wrongly.

    Filtering happens on real columns only. ``target`` is JSON, and the accessor
    for it differs between SQLite and PostgreSQL; a filter written against one
    would silently match nothing on the other, which is the same wrong answer in
    a costume. ``category`` exists as a column for exactly this reason.
    """
    bounded = max(1, min(int(limit), MAX_LIMIT))
    since_ts, until_ts = validate_filters(
        outcome=outcome, category=category, since=since, until=until
    )
    after_id = _parse_cursor(cursor)

    def _filtered(statement):
        return _apply_filters(
            statement, cluster_id=cluster_id, actor=actor, outcome=outcome,
            category=category, verb=verb, dry_run=dry_run,
            since_ts=since_ts, until_ts=until_ts,
        )

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


def validate_filters(
    *,
    outcome: str | None = None,
    category: str | None = None,
    since: Any = None,
    until: Any = None,
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """Reject every malformed filter, returning the parsed timestamp window.

    Separated out so it can be run *eagerly* by a streaming caller. A generator
    that validated its own arguments would not raise until the first row was
    pulled — by which time ``StreamingResponse`` has already sent 200 and the
    headers, and the browser saves a truncated file with a Content-Disposition
    telling it this is the audit export. A malformed ``since`` has to be a §1.3
    error envelope, not a short download.
    """
    if outcome is not None and outcome not in OUTCOMES:
        raise Invalid(
            f"{outcome!r} is not an audit outcome.",
            hint="Use one of: " + ", ".join(sorted(OUTCOMES)) + ".",
            context={"parameter": "outcome", "value": outcome},
        )
    if category is not None and category not in CATEGORIES:
        raise Invalid(
            f"{category!r} is not an audit category.",
            hint="Use one of: " + ", ".join(sorted(CATEGORIES)) + ".",
            context={"parameter": "category", "value": category},
        )
    since_ts = _parse_timestamp(since, "since")
    until_ts = _parse_timestamp(until, "until")
    if since_ts is not None and until_ts is not None and until_ts < since_ts:
        raise Invalid(
            "`until` is earlier than `since`, so the window selects nothing.",
            hint="An empty result from an impossible window reads as 'nothing "
                 "happened'. Widen the window instead.",
            context={"parameter": "until", "since": str(since), "until": str(until)},
        )
    return since_ts, until_ts


def _apply_filters(
    statement,
    *,
    cluster_id: Any,
    actor: str | None,
    outcome: str | None,
    category: str | None,
    verb: str | None,
    dry_run: bool | None,
    since_ts: datetime.datetime | None,
    until_ts: datetime.datetime | None,
):
    """The §10 filter set, in one place.

    Shared by the paged read and the export so the two cannot answer the same
    query differently — an export that quietly matched a wider set than the page
    the operator checked it against would be discovered, if at all, by somebody
    comparing row counts months later.
    """
    if cluster_id is NO_CLUSTER:
        statement = statement.where(AuditRecord.cluster_id.is_(None))
    elif cluster_id is not None:
        statement = statement.where(AuditRecord.cluster_id == cluster_id)
    if actor:
        statement = statement.where(AuditRecord.actor == actor)
    if outcome:
        statement = statement.where(AuditRecord.outcome == outcome)
    if category == CATEGORY_CLUSTER:
        # Rows predating the column hold NULL and are cluster writes; without the
        # IS NULL arm they are unreachable from either category filter while
        # sitting in plain sight in the unfiltered view.
        statement = statement.where(
            (AuditRecord.category == CATEGORY_CLUSTER) | AuditRecord.category.is_(None)
        )
    elif category:
        statement = statement.where(AuditRecord.category == category)
    if verb:
        statement = statement.where(AuditRecord.verb == verb)
    if dry_run is not None:
        statement = statement.where(AuditRecord.dry_run == bool(dry_run))
    if since_ts is not None:
        statement = statement.where(AuditRecord.ts >= since_ts)
    if until_ts is not None:
        statement = statement.where(AuditRecord.ts <= until_ts)
    return statement


def stream(
    *,
    cluster_id: Any = None,
    actor: str | None = None,
    outcome: str | None = None,
    since: Any = None,
    until: Any = None,
    category: str | None = None,
    verb: str | None = None,
    dry_run: bool | None = None,
    batch: int = 500,
):
    """Every matching record, oldest first, without materialising the trail.

    Oldest first, unlike :func:`query`: an export is read as a chronology and
    verified as a chain, and both run forwards. The paged UI reads newest first
    because that is where an incident starts.

    There is deliberately **no limit parameter**. A truncated export that looked
    complete is the export-shaped version of an empty list that means "we could
    not look": the person reading the file cannot tell a short extract from a
    quiet quarter, and "nobody scaled that deployment" is the conclusion they
    would draw.

    Not itself a generator. Filters are validated here, on the call, so a
    malformed ``since`` raises before the response has begun; the generator it
    returns owns its session and closes it when exhausted, thrown into, or
    garbage-collected after an aborted download.
    """
    since_ts, until_ts = validate_filters(
        outcome=outcome, category=category, since=since, until=until
    )
    statement = _apply_filters(
        select(AuditRecord),
        cluster_id=cluster_id, actor=actor, outcome=outcome, category=category,
        verb=verb, dry_run=dry_run, since_ts=since_ts, until_ts=until_ts,
    )

    def _rows():
        db = database.SessionLocal()
        try:
            # yield_per streams from the server instead of building one list the
            # size of the table. The audit trail is the only table in this schema
            # with no upper bound on rows, so materialising it to serialise it
            # would put the console's memory ceiling at the mercy of how long it
            # has been running.
            result = db.execute(
                statement.order_by(AuditRecord.id).execution_options(yield_per=batch)
            )
            for row in result.scalars():
                yield row
        finally:
            db.close()

    return _rows()


def verify_chain(*, limit: int | None = None) -> dict[str, Any]:
    """§10.3 integrity report. See :func:`app.audit.integrity.verify`."""
    db = database.SessionLocal()
    try:
        return integrity.verify(db, limit=limit)
    finally:
        db.close()


__all__ = [
    "CHAIN_RETRIES",
    "NO_CLUSTER",
    "CONSOLE_GROUP",
    "CONSOLE_VERSION",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "OUTCOMES",
    "TARGET_FIELDS",
    "query",
    "record",
    "record_console_event",
    "stream",
    "validate_filters",
    "verify_chain",
]
