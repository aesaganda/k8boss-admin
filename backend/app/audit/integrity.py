"""
Tamper *evidence* for the audit trail: a hash chain over ``audit_records``.

**This is not the same guarantee as the append-only guard, and the difference is
the whole reason the module exists.** ``install_audit_append_only_guard`` refuses
an UPDATE or a DELETE issued through this application's ORM. It is a real
protection against this codebase growing a bug, and it is no protection at all
against a ``psql`` session, a restored backup, or anyone with write access to the
volume — which, for a table whose value is that it can be trusted after an
incident, is the population that matters.

A chain cannot prevent any of that either. What it does is make it **detectable**:
each row's ``event_hash`` is SHA-256 over that row's immutable content plus the
previous row's ``event_hash``, so editing, deleting, inserting or reordering a
committed row breaks the links from that point on and :func:`verify` says where.

## The three states, and why the third one is not optional

A row is ``verified``, ``broken``, or ``unchained``. That last state is the one
this module is most careful about.

Rows written before chaining existed have ``prev_hash IS NULL`` and
``event_hash IS NULL``. **They are never back-filled.** Back-filling would
compute a hash over whatever those rows say *today* and store it as proof —
which converts "we do not know whether this was altered" into "this is verified",
in the one table where that inversion is least acceptable. So they stay null, and
:func:`verify` counts them separately and refuses to report ``intact`` while any
are in scope.

The same applies to the last-resort write path in :mod:`app.audit.recorder`: when
a row cannot be linked, it is stored **unchained rather than dropped**, because
losing the record entirely is worse than losing its link, and an unchained row
reports itself as unchained.

## Concurrency

Two replicas reading the same chain tip compute the same ``prev_hash``. The
UNIQUE constraint on that column means the second INSERT is refused by the
database rather than silently forking the chain into two branches, and the writer
retries against the new tip (:func:`app.audit.recorder.record`). That is why
``ix_audit_prev_hash`` is UNIQUE and not merely an index: it converts a race that
would corrupt the chain into a race that costs one retry.

K8Boss serialises the same operation with an in-process ``threading.Lock``, which
holds for one writer and silently forks across replicas — its own docstring says
so. The constraint is engine-enforced, so it holds across replicas, across
processes, and across anything else that writes through this schema.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session as OrmSession

from app.models import AuditRecord, utcnow

logger = logging.getLogger(__name__)

#: The audit table under an alias, so :func:`chain_tip` can correlate a row
#: against the rest of the table in one statement without SQLAlchemy folding the
#: two references into the same FROM entry.
_tip_candidate = AuditRecord.__table__.alias("tip_candidate")

#: ``prev_hash`` of the first chained row. A literal rather than NULL so the
#: first row is provably first: with NULL there, a verifier could not tell the
#: genuine head of the chain from a row whose predecessor was deleted.
GENESIS: str = "0" * 64

#: Instance attribute a caller sets to ask that one row be written outside the
#: chain. Read by the flush listener. Exists for exactly one caller — the
#: last-resort path in ``recorder.record`` after the chain retries are spent —
#: and named unmistakably so a second use has to be a deliberate act.
UNCHAINED_ATTR = "_k8boss_write_unchained"

#: The columns the hash covers, in a fixed order. Everything an incident review
#: would act on is in here; anything not in here could be altered without
#: breaking the chain, which is why the list is explicit rather than derived from
#: the model (a derived list would silently start covering, or stop covering,
#: whatever the next schema change did).
#:
#: ``id`` is excluded because it is assigned by the database *after* this runs,
#: so there is nothing to hash yet.
#:
#: That means the hash alone cannot speak for a record's id, and renumbering is
#: **not** caught by the link walk — a renumbered record stays perfectly
#: reachable from GENESIS. It matters because ``GET /api/audit`` pages by
#: descending id, so moving a record moves it in the operator's listing. The walk
#: therefore checks separately that chain order and id order agree; see
#: :func:`_diagnose_break`.
HASHED_FIELDS: tuple[str, ...] = (
    "ts",
    "category",
    "actor",
    "source_ip",
    "cluster_id",
    "cluster_name",
    "verb",
    "target",
    "dry_run",
    "outcome",
    "detail",
    "diff_digest",
    "error",
)

#: Fields the hash covers **only when they are set**, added after rows already
#: existed. Same tamper evidence, no false alarms.
#:
#: The problem this solves, stated plainly: the verifier recomputes a stored
#: row's hash from that row's own columns. Appending a name to
#: :data:`HASHED_FIELDS` therefore changes the computation for *every* row ever
#: written — including the millions written before the column existed, whose
#: stored digests were computed without it — and the whole table verifies as
#: `broken`. That is the false "this row was modified" alarm ``_canonical``
#: exists to prevent, arriving instead by way of a schema change, and it is the
#: alarm everyone learns to ignore.
#:
#: So a field listed here contributes a key to the payload when its value is not
#: None, and no key at all when it is. A row written before ADR-0007 has
#: ``impersonated_user`` NULL, produces the payload it always produced, and
#: verifies exactly as it did.
#:
#: **This is not a hole.** Every mutation of the field crosses the boundary and
#: is caught: NULL → a name adds a key the stored digest did not cover, a name →
#: NULL removes one it did, and one name → another changes its value. All three
#: recompute to something other than what is stored. The only thing the omission
#: costs is the ability to distinguish "written before the column existed" from
#: "written by a console that acted as itself" — and those are the same fact
#: about attribution, which is that the API server saw the ServiceAccount.
#:
#: A field belongs here only if it was added to a table that already had rows.
#: Anything present from the start belongs in :data:`HASHED_FIELDS`, where NULL
#: is hashed as NULL and cannot be introduced undetectably.
OPTIONAL_HASHED_FIELDS: tuple[str, ...] = (
    "impersonated_user",
)


def _canonical(value: Any) -> Any:
    """One value in a form that survives a round trip through either engine.

    The hash is computed once at write time against Python objects and again at
    verify time against whatever SQLAlchemy hands back from SQLite or
    PostgreSQL. Anything whose representation differs between those two points
    would produce a false "this row was modified" — an alarm about tampering
    that did not happen, which is worse than useless because it is the alarm
    everyone learns to ignore.

    So: datetimes become a fixed-precision ISO string (SQLite stores a string,
    PostgreSQL a timestamp, and the two round-trip to equal ``datetime`` objects
    but not to equal ``repr``s); bools become 0/1 (SQLite has no boolean type and
    returns ints from some drivers); dicts are recursed so ``target`` hashes by
    content rather than by insertion order.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if hasattr(value, "isoformat"):
        # Microsecond precision, explicitly. `str(datetime)` omits the
        # microseconds when they are zero, so one row in a million would hash
        # differently on the way back in.
        return value.isoformat(timespec="microseconds")
    return str(value)


def compute_event_hash(row: AuditRecord, prev_hash: str) -> str:
    """SHA-256 over this row's immutable content plus ``prev_hash``.

    Deterministic by construction: a fixed field list, canonicalised values,
    ``sort_keys`` and no whitespace. Any of those left to chance would make the
    verifier disagree with the writer about an untouched row.
    """
    payload = {field: _canonical(getattr(row, field, None)) for field in HASHED_FIELDS}
    # Omitted entirely when None, so a row written before the column existed
    # hashes to what it hashed to then. See OPTIONAL_HASHED_FIELDS.
    for field in OPTIONAL_HASHED_FIELDS:
        value = getattr(row, field, None)
        if value is not None:
            payload[field] = _canonical(value)
    payload["prev_hash"] = prev_hash
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def chain_tip(session: OrmSession) -> str:
    """The ``event_hash`` to link the next row to, or :data:`GENESIS`.

    The tip is the chained row **whose hash nothing else links to**, not simply
    the one with the highest id. Those are the same row in an untouched trail,
    and choosing by id looks simpler — but it hands an attacker a way to stop the
    console recording anything ever again.

    Renumbering one committed row to the highest id makes it the apparent tip.
    Its ``event_hash`` is already the *next* row's ``prev_hash``, and
    ``prev_hash`` is UNIQUE, so every subsequent chained INSERT collides. The
    lookup has no advancing state, so it collides again on every retry, forever:
    each new record burns its retries and lands unchained, and ``verify`` reports
    the result as ``partial`` — the verdict reserved for benign residue. A single
    ``UPDATE ... SET id`` silently converts the chain into a permanently dead one
    that reports itself as merely incomplete.

    The correlated subquery costs one indexed lookup against ``ix_audit_prev_hash``
    per write, which is the same order of work the previous version did, and it
    depends on the links rather than on a column an attacker can renumber.

    ``no_autoflush`` because this runs inside ``before_flush``: a session
    configured with ``autoflush=True`` would otherwise re-enter the flush it is
    already in.
    """
    successor = select(AuditRecord.id).where(
        AuditRecord.prev_hash == _tip_candidate.c.event_hash
    )
    with session.no_autoflush:
        newest = session.execute(
            select(_tip_candidate.c.event_hash)
            .where(
                _tip_candidate.c.event_hash.is_not(None),
                ~successor.exists(),
            )
            .order_by(_tip_candidate.c.id.desc())
            .limit(1)
        ).scalar()
    return newest or GENESIS


def _stamp_pending(session: OrmSession, flush_context, instances) -> None:
    """``before_flush`` listener: link every new, unstamped audit row.

    A listener rather than a call inside ``recorder.record`` for the same reason
    the append-only guard is one: it covers every write path, including a future
    call site that constructs an ``AuditRecord`` directly. A rule that only
    applies to the callers who remembered it is not a rule.
    """
    pending = [
        obj
        for obj in session.new
        if isinstance(obj, AuditRecord)
        and obj.event_hash is None
        and not getattr(obj, UNCHAINED_ATTR, False)
    ]
    if not pending:
        return

    prev = chain_tip(session)
    # Column defaults are applied during INSERT, i.e. after this listener. A row
    # whose ts was still None here would be hashed as null and read back as a
    # real timestamp, and verify would report a modification that never happened.
    for row in pending:
        if row.ts is None:
            row.ts = utcnow()
        if row.category is None:
            row.category = AuditRecord.__table__.c.category.default.arg

    # `session.new` is an unordered set. Sorting gives one flush a deterministic
    # internal order; the links then preserve whatever order was chosen, so the
    # verifier does not need to reproduce this sort.
    for row in sorted(pending, key=lambda r: (r.ts, id(r))):
        row.prev_hash = prev
        row.event_hash = compute_event_hash(row, prev)
        prev = row.event_hash


def install_audit_chain() -> None:
    """Register the chaining listener. Idempotent.

    Bound to the ``Session`` class rather than to ``SessionLocal``, matching
    ``install_audit_append_only_guard``: a listener on one sessionmaker protects
    only the sessions that factory produces, and the tests build their own.
    """
    if not event.contains(OrmSession, "before_flush", _stamp_pending):
        event.listen(OrmSession, "before_flush", _stamp_pending)


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

#: The three verdicts. ``partial`` is not a softer ``intact`` — it is the answer
#: "no break was found, and the trail contains records this mechanism cannot
#: speak for". Reporting those as intact is the one thing a verifier must never
#: do, because the whole value of the answer is that it can be relied on.
STATUS_INTACT = "intact"
STATUS_BROKEN = "broken"
STATUS_PARTIAL = "partial"


def _break(row_id: int | None, reason: str) -> dict[str, Any]:
    return {"id": row_id, "reason": reason}


def verify(session: OrmSession, *, limit: int | None = None) -> dict[str, Any]:
    """Walk the chain and report what can and cannot be attested.

    With no ``limit`` the walk starts at :data:`GENESIS` and covers the whole
    trail, which is the only form that can return ``intact``.

    With a ``limit`` it verifies the newest ``limit`` chained rows: each row's
    content against its own stored hash, and each link against its neighbour.
    That is a real check — it catches an edited row and a deleted one — but it
    cannot prove the rows *before* the window still link back to GENESIS, so the
    result is always ``partial`` and says ``anchored: false``. A windowed check
    reported as ``intact`` would be a claim about records nobody looked at.

    Returns the §10.3 body::

        {"status": "intact"|"broken"|"partial",
         "verified": int, "unchained": int, "total": int,
         "anchored": bool, "first_break": {"id":…, "reason":…}|None,
         "tip": str|None, "genesis": str, "window": {...}}
    """
    # Counted by the database, not by materialising the ids and calling len() on
    # them. Anyone who can click Verify reaches this, and a list built only to be
    # measured put an allocation the size of the whole trail behind that click.
    total_count = int(
        session.execute(select(func.count()).select_from(AuditRecord)).scalar() or 0
    )
    unchained_count, oldest_unchained_id = session.execute(
        select(func.count(), func.min(AuditRecord.id))
        .select_from(AuditRecord)
        .where(AuditRecord.event_hash.is_(None))
    ).one()
    unchained_count = int(unchained_count or 0)

    # A row with exactly one of the two hashes is a corrupt half-state that no
    # normal write path produces. Reported as a break rather than counted as
    # unchained: "we could not link this one" and "somebody blanked a column"
    # deserve different answers.
    half = session.execute(
        select(AuditRecord.id)
        .where(
            (AuditRecord.event_hash.is_(None) & AuditRecord.prev_hash.is_not(None))
            | (AuditRecord.event_hash.is_not(None) & AuditRecord.prev_hash.is_(None))
        )
        .order_by(AuditRecord.id)
        .limit(1)
    ).scalars().first()

    base = {
        "verified": 0,
        "unchained": unchained_count,
        "total": total_count,
        "genesis": GENESIS,
        "tip": None,
        "window": {
            "requested_limit": limit,
            "oldest_unchained_id": oldest_unchained_id,
        },
    }

    if half is not None:
        return {
            **base,
            "status": STATUS_BROKEN,
            "anchored": False,
            "first_break": _break(
                half,
                "one of prev_hash/event_hash is set and the other is not; the "
                "row's chain link was partially cleared",
            ),
        }

    if limit is not None and limit > 0:
        return {**base, **_verify_window(session, limit)}
    return {**base, **_verify_from_genesis(session, unchained_count)}


#: Rows per round trip while the chain walk streams. Bounds what one
#: verification holds in memory at a time; it is what
#: :func:`app.audit.recorder.stream` uses for the same reason.
_WALK_BATCH = 500


def _verify_from_genesis(session: OrmSession, unchained: int) -> dict[str, Any]:
    """Walk the whole trail from GENESIS and report the first disagreement.

    Streamed in id order with ``yield_per`` rather than loaded first. The
    previous version materialised every chained record as an ORM object before
    checking any of them, and ``GET /api/audit/verify`` is reachable by anyone
    who can click Verify on the audit page — so at a few hundred thousand
    records that click was an out-of-memory kill of the single backend replica,
    repeatable at will by anyone signed in. None of this reduces the SHA-256
    work, which is the thing actually being asked for; only the memory the
    answer is assembled in.

    Reading in id order instead of following the links is what makes the walk
    streamable, and it checks the same things: every chained row is visited
    exactly once, every hash is recomputed against the link it claims, and a row
    that does not link to the one before it stops the walk. What id order gives
    up is the *diagnosis* — from here, "the next link is somewhere else" and
    "there is no next link" look identical — and :func:`_diagnose_break` buys
    that back with one indexed lookup on the break path. It has to be bought
    back: a renumbered record verifies byte for byte and still reaches GENESIS,
    so if the two collapsed into one answer the only tamper that shows up
    nowhere else would be reported as a deletion.
    """
    result = session.execute(
        select(AuditRecord)
        .where(AuditRecord.event_hash.is_not(None))
        .order_by(AuditRecord.id)
        .execution_options(yield_per=_WALK_BATCH)
    )

    expected = GENESIS
    previous: AuditRecord | None = None
    verified = 0

    for row in result.scalars():
        if row.prev_hash != expected:
            return {**_diagnose_break(session, row, previous), "verified": verified}
        if compute_event_hash(row, expected) != row.event_hash:
            return {
                "status": STATUS_BROKEN,
                "anchored": True,
                "verified": verified,
                "first_break": _break(
                    row.id, "event_hash mismatch: this row's content was modified"
                ),
            }
        expected = row.event_hash
        previous = row
        verified += 1

    # `verified == 0` is both the empty trail and a table holding nothing but
    # pre-chain rows: nothing was walked, so there is no tip to report, and
    # `partial` is what withholds the verdict rather than claiming a pass over
    # rows this mechanism cannot speak for.
    return {
        "status": STATUS_PARTIAL if unchained else STATUS_INTACT,
        "anchored": True,
        "verified": verified,
        "tip": previous.event_hash if previous is not None else None,
        "first_break": None,
    }


def _diagnose_break(
    session: OrmSession, row: AuditRecord, previous: AuditRecord | None
) -> dict[str, Any]:
    """Say what the first unlinked record means. One lookup, on this path only.

    ``prev_hash`` is UNIQUE and indexed, so asking "does anything at all claim
    the record we just walked as its predecessor?" is a single seek. It runs
    after the walk has already stopped rather than once per row, which is what
    keeps the diagnosis off the cost of verifying an untampered trail.

    If something does claim it, the chain continues at a record that is not the
    next one by id, and an id was changed after the fact. That is worth its own
    sentence because every hash in such a trail still verifies and every record
    still reaches GENESIS: the only thing that moved is where the record sits in
    ``GET /api/audit``, which pages by descending id — the listing an operator
    reads during an incident.

    If nothing does, the link genuinely ends here: a record was deleted,
    inserted or reordered. The fork is named separately when this row and the
    one before it claim the *same* predecessor, which is what two writers
    produce on a database whose UNIQUE index was not there to refuse the second
    one.
    """
    expected = previous.event_hash if previous is not None else GENESIS
    successor_id = session.execute(
        select(AuditRecord.id)
        .where(AuditRecord.prev_hash == expected)
        .limit(1)
    ).scalars().first()

    if successor_id is not None:
        return {
            "status": STATUS_BROKEN,
            "anchored": True,
            "first_break": _break(
                row.id,
                f"record {successor_id}, not record {row.id}, is the next link in "
                "the chain, so chain order and id order disagree — an id was "
                "changed after the record was written, which moves it in every "
                "listing that pages by id",
            ),
        }

    if previous is not None and row.prev_hash == previous.prev_hash:
        return {
            "status": STATUS_BROKEN,
            "anchored": True,
            "first_break": _break(
                previous.id,
                "chain fork: two rows claim the same predecessor, which means "
                "concurrent writers linked to one tip",
            ),
        }

    return {
        "status": STATUS_BROKEN,
        "anchored": True,
        "first_break": _break(
            row.id,
            "chain broken: record(s) cannot be reached from the first link, "
            "which means one was deleted, inserted, or reordered",
        ),
    }


def _verify_window(session: OrmSession, limit: int) -> dict[str, Any]:
    """Verify the newest ``limit`` chained rows without anchoring to GENESIS."""
    newest = session.execute(
        select(AuditRecord)
        .where(AuditRecord.event_hash.is_not(None))
        .order_by(AuditRecord.id.desc())
        .limit(limit)
    ).scalars().all()
    rows = list(reversed(newest))

    if not rows:
        return {
            "status": STATUS_PARTIAL,
            "anchored": False,
            "verified": 0,
            "first_break": None,
        }

    verified = 0
    previous: AuditRecord | None = None
    for row in rows:
        if compute_event_hash(row, row.prev_hash) != row.event_hash:
            return {
                "status": STATUS_BROKEN,
                "anchored": False,
                "verified": verified,
                "first_break": _break(
                    row.id, "event_hash mismatch: this row's content was modified"
                ),
            }
        if previous is not None and row.prev_hash != previous.event_hash:
            return {
                "status": STATUS_BROKEN,
                "anchored": False,
                "verified": verified,
                "first_break": _break(
                    row.id,
                    "this record does not link to the one before it, which means "
                    "a record between them was deleted or reordered",
                ),
            }
        if previous is not None and row.id <= previous.id:
            # Same reasoning as the anchored walk: the hash cannot cover the id,
            # so id order is checked against chain order separately.
            return {
                "status": STATUS_BROKEN,
                "anchored": False,
                "verified": verified,
                "first_break": _break(
                    row.id,
                    f"record {row.id} follows record {previous.id} in the chain but "
                    "not in id order, so an id was changed after the record was "
                    "written",
                ),
            }
        previous = row
        verified += 1

    return {
        "status": STATUS_PARTIAL,
        "anchored": False,
        "verified": verified,
        "tip": rows[-1].event_hash,
        "first_break": None,
    }


__all__ = [
    "GENESIS",
    "HASHED_FIELDS",
    "OPTIONAL_HASHED_FIELDS",
    "STATUS_BROKEN",
    "STATUS_INTACT",
    "STATUS_PARTIAL",
    "UNCHAINED_ATTR",
    "chain_tip",
    "compute_event_hash",
    "install_audit_chain",
    "verify",
]
