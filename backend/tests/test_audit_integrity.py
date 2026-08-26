"""
The audit hash chain (§10.3).

The append-only guard already stops *this application* rewriting a record. These
tests are about the guarantee that starts where that one ends: a record altered
by something that is not this application — a ``psql`` session, a restored
backup, anyone with write access to the volume — has to be **detectable**, and
the report has to be honest about what it did and did not check.

The tests below therefore tamper with the table using SQLAlchemy Core, which
bypasses the ORM guard. That is the only place in this tree that issues an
``UPDATE`` against ``audit_records``, and it does so precisely because it is
simulating the attacker the chain exists to catch.

Three properties, in order of how badly getting them wrong would hurt:

1. **A modified, deleted or reordered record is reported as broken.** Without
   this the chain is decoration.
2. **An unmodified trail verifies.** A verifier that cried wolf on an untouched
   table would be switched off within a week, and then property 1 buys nothing.
3. **Records the chain cannot speak for are counted, never claimed.** Rows that
   predate chaining stay unhashed forever and the verdict is withheld — reporting
   them as verified would turn "we do not know" into proof, which is the one
   inversion this table must never make.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select, update

from app import database
from app.audit import integrity, recorder
from app.models import AuditRecord

TARGET = {
    "group": "apps", "version": "v1", "resource": "deployments",
    "namespace": "prod", "name": "checkout",
}


def write(**overrides) -> int | None:
    fields = {
        "verb": "patch", "target": dict(TARGET), "dry_run": False,
        "outcome": "applied", "detail": "replicas 3 -> 5",
    }
    fields.update(overrides)
    return recorder.record(**fields)


def rows() -> list[AuditRecord]:
    db = database.SessionLocal()
    try:
        return list(db.execute(select(AuditRecord).order_by(AuditRecord.id)).scalars())
    finally:
        db.close()


def verify(**kwargs) -> dict:
    return recorder.verify_chain(**kwargs)


# --------------------------------------------------------------------------- #
# The chain forms
# --------------------------------------------------------------------------- #

def test_the_first_record_links_to_genesis(db_engine):
    write()

    (row,) = rows()

    # A literal genesis rather than NULL, so the head of the chain is provably
    # the head: with NULL there, a verifier could not tell the first record from
    # one whose predecessor had been deleted.
    assert row.prev_hash == integrity.GENESIS
    assert row.event_hash and len(row.event_hash) == 64


def test_each_record_links_to_the_one_before_it(db_engine):
    for index in range(4):
        write(detail=f"change {index}")

    chain = rows()

    assert [row.prev_hash for row in chain[1:]] == [row.event_hash for row in chain[:-1]]


def test_an_untampered_trail_verifies_as_intact(db_engine):
    for index in range(5):
        write(detail=f"change {index}")

    report = verify()

    assert report["status"] == integrity.STATUS_INTACT
    assert report["verified"] == 5
    assert report["unchained"] == 0
    assert report["first_break"] is None
    assert report["anchored"] is True


def test_an_empty_trail_is_intact_rather_than_broken(db_engine):
    report = verify()

    assert report["status"] == integrity.STATUS_INTACT
    assert report["verified"] == 0
    assert report["total"] == 0


# --------------------------------------------------------------------------- #
# Tampering is detected
# --------------------------------------------------------------------------- #

def test_editing_a_committed_record_breaks_the_chain(db_engine):
    for index in range(3):
        write(detail=f"change {index}")
    target = rows()[1]

    # Core UPDATE, deliberately bypassing the ORM append-only guard: this is the
    # thing the guard cannot stop and the chain exists to catch.
    db = database.SessionLocal()
    try:
        db.execute(
            update(AuditRecord)
            .where(AuditRecord.id == target.id)
            .values(outcome="dry_run", detail="nothing was written")
        )
        db.commit()
    finally:
        db.close()

    report = verify()

    assert report["status"] == integrity.STATUS_BROKEN
    assert report["first_break"]["id"] == target.id
    assert "modified" in report["first_break"]["reason"]
    # The records before the tampered one still verified, and the report says how
    # many. "Everything is broken" would be as useless as "everything is fine".
    assert report["verified"] == 1


def test_deleting_a_committed_record_breaks_the_chain(db_engine):
    for index in range(4):
        write(detail=f"change {index}")
    victim = rows()[1]

    db = database.SessionLocal()
    try:
        db.execute(delete(AuditRecord).where(AuditRecord.id == victim.id))
        db.commit()
    finally:
        db.close()

    report = verify()

    # The deleted record is gone, so nothing can report *it*. What is detected is
    # that the records after it no longer link back — which is the whole reason
    # verification follows the links rather than iterating by id.
    assert report["status"] == integrity.STATUS_BROKEN
    assert report["first_break"] is not None
    assert "deleted" in report["first_break"]["reason"]


def test_a_forged_record_inserted_into_the_middle_is_unreachable(db_engine):
    for index in range(3):
        write(detail=f"change {index}")

    db = database.SessionLocal()
    try:
        # A plausible-looking row with invented hashes. It has to be given hashes
        # that collide with nothing, which is exactly what an attacker who cannot
        # recompute the chain would have to do.
        db.execute(
            AuditRecord.__table__.insert().values(
                ts=rows()[0].ts,
                category="cluster",
                actor="attacker",
                verb="delete",
                target=dict(TARGET),
                dry_run=False,
                outcome="applied",
                prev_hash="f" * 64,
                event_hash="e" * 64,
            )
        )
        db.commit()
    finally:
        db.close()

    report = verify()

    assert report["status"] == integrity.STATUS_BROKEN
    assert "cannot be reached" in report["first_break"]["reason"]
    assert "inserted" in report["first_break"]["reason"]


def test_renumbering_a_record_is_detected(db_engine):
    """The hash cannot cover the id, so the id is checked separately.

    `id` is assigned by the database *after* the hash is computed, so a
    renumbered record still verifies byte-for-byte and stays perfectly reachable
    from GENESIS. It is not cosmetic: `GET /api/audit` pages by descending id, so
    moving a record moves it in the listing an operator reads during an incident,
    while every hash continues to check out.
    """
    for index in range(4):
        write(detail=f"change {index}")
    target = rows()[1]

    db = database.SessionLocal()
    try:
        db.execute(update(AuditRecord).where(AuditRecord.id == target.id).values(id=99))
        db.commit()
    finally:
        db.close()

    report = verify()

    assert report["status"] == integrity.STATUS_BROKEN
    assert "id order" in report["first_break"]["reason"]


def test_renumbering_does_not_wedge_the_chain(db_engine):
    """A renumbered record must not stop the console recording anything again.

    `chain_tip` used to pick the highest id. Renumbering one committed record to
    the top made it the apparent tip; its `event_hash` was already the next
    record's `prev_hash`, and `prev_hash` is UNIQUE, so every later INSERT
    collided — deterministically, on every retry, forever. Each new record burned
    its retries and landed unchained, and `verify` reported that as `partial`:
    the verdict reserved for benign residue, over a chain that was dead.

    The tip is now the record nothing links to, which no renumbering can forge.
    """
    for index in range(3):
        write(detail=f"change {index}")
    target = rows()[1]

    db = database.SessionLocal()
    try:
        db.execute(update(AuditRecord).where(AuditRecord.id == target.id).values(id=500))
        db.commit()
    finally:
        db.close()

    after = write(detail="written after the renumbering")

    assert after is not None
    stored = [row for row in rows() if row.id == after][0]
    assert stored.event_hash is not None, (
        "a record written after a renumbering was forced outside the chain, so "
        "the tamper wedged the chain instead of merely being detected"
    )


def test_blanking_one_hash_column_is_reported_as_a_break_not_as_unchained(db_engine):
    """Half a link is corruption, not an honest gap.

    An unchained record has *both* columns null — that is what a record written
    before chaining, or by the last-resort path, looks like. One column cleared
    is something else entirely, and folding it into the unchained count would let
    an attacker downgrade a record from "verified" to "we never checked this one"
    by blanking a single field.
    """
    write()
    target = rows()[0]

    db = database.SessionLocal()
    try:
        db.execute(
            update(AuditRecord).where(AuditRecord.id == target.id).values(event_hash=None)
        )
        db.commit()
    finally:
        db.close()

    report = verify()

    assert report["status"] == integrity.STATUS_BROKEN
    assert report["first_break"]["id"] == target.id


# --------------------------------------------------------------------------- #
# What the chain cannot speak for, it does not claim
# --------------------------------------------------------------------------- #

def test_records_written_before_chaining_are_counted_never_claimed(db_engine):
    """The property this whole design is arranged around.

    A deployment that upgrades into hash chaining has a table full of records
    with no hashes. They are never back-filled, because back-filling computes a
    hash over whatever those records say *today* — if one was altered last month,
    the chain would then attest the altered version. That is strictly worse than
    no chain: it converts "unknown" into "verified".

    So they stay null, they are counted, and the verdict is `partial`.
    """
    db = database.SessionLocal()
    try:
        for index in range(3):
            db.execute(
                AuditRecord.__table__.insert().values(
                    ts=recorder.utcnow(),
                    category="cluster",
                    actor="legacy",
                    verb="patch",
                    target=dict(TARGET),
                    dry_run=False,
                    outcome="applied",
                    detail=f"pre-chain record {index}",
                )
            )
        db.commit()
    finally:
        db.close()

    write(detail="the first chained record")

    report = verify()

    assert report["status"] == integrity.STATUS_PARTIAL
    assert report["unchained"] == 3
    assert report["verified"] == 1
    assert report["total"] == 4
    # No break: the pre-chain records are not evidence of tampering, they are
    # evidence of an upgrade. Reporting them as broken would be its own false
    # alarm, and a verifier nobody believes catches nothing.
    assert report["first_break"] is None


def test_a_trail_with_nothing_chained_yet_is_partial_not_intact(db_engine):
    """The moment after an upgrade, before the first chained record is written.

    The table is full of pre-chain rows and the chain has nothing in it. The
    previous test covers the same property once a chained record exists; this
    one covers the window before that, which is a different branch — `verify`
    returns early when there are no chained rows at all — and it is the branch
    a freshly upgraded deployment is actually in.

    `intact` here would be the ADR-0003 inversion at its purest: a verifier
    reporting a pass over a table it has not checked a single row of. Nothing
    is broken, and nothing is verified either, and only one of those three
    words is honest about it.
    """
    db = database.SessionLocal()
    try:
        for index in range(3):
            db.execute(
                AuditRecord.__table__.insert().values(
                    ts=recorder.utcnow(),
                    category="cluster",
                    actor="legacy",
                    verb="patch",
                    target=dict(TARGET),
                    dry_run=False,
                    outcome="applied",
                    detail=f"pre-chain record {index}",
                )
            )
        db.commit()
    finally:
        db.close()

    report = verify()

    assert report["status"] == integrity.STATUS_PARTIAL
    assert report["verified"] == 0
    assert report["unchained"] == 3
    assert report["total"] == 3
    assert report["first_break"] is None
    # Nothing was walked, so there is no tip to report. A tip here would imply
    # a chain the trail does not have.
    assert report["tip"] is None
    assert report["window"]["oldest_unchained_id"] is not None


def test_an_empty_trail_stays_intact_when_nothing_is_unchained(db_engine):
    """The neighbouring branch, pinned so the fix above cannot overreach.

    Same early return, no records at all. `partial` would be wrong here for the
    opposite reason: there are no records this verifier cannot speak for, so
    withholding the verdict would light §11.1's banner on every fresh install
    and teach an operator to ignore it.
    """
    report = verify()

    assert report["status"] == integrity.STATUS_INTACT
    assert report["verified"] == 0
    assert report["unchained"] == 0
    assert report["total"] == 0


def test_a_windowed_verification_never_claims_intact(db_engine):
    """A window is a real check and an incomplete one, and says which.

    Verifying the newest N records catches an edit and a deletion inside the
    window. It cannot prove the records *before* the window still link back to
    the first one, so `intact` — which is a statement about the whole trail —
    is not available to it.
    """
    for index in range(6):
        write(detail=f"change {index}")

    report = verify(limit=3)

    assert report["status"] == integrity.STATUS_PARTIAL
    assert report["anchored"] is False
    assert report["verified"] == 3

    assert verify()["status"] == integrity.STATUS_INTACT


def test_a_windowed_verification_still_detects_an_edit_inside_it(db_engine):
    for index in range(6):
        write(detail=f"change {index}")
    target = rows()[-2]

    db = database.SessionLocal()
    try:
        db.execute(
            update(AuditRecord).where(AuditRecord.id == target.id).values(actor="someone-else")
        )
        db.commit()
    finally:
        db.close()

    report = verify(limit=3)

    assert report["status"] == integrity.STATUS_BROKEN
    assert report["first_break"]["id"] == target.id


# --------------------------------------------------------------------------- #
# Hashing is stable
# --------------------------------------------------------------------------- #

def test_the_hash_survives_a_round_trip_through_the_database(db_engine):
    """The verifier must agree with the writer about an untouched record.

    The hash is computed against Python objects at write time and against
    whatever the driver returns at verify time. Any value whose representation
    differs between those two points — a datetime's microseconds, a bool that
    comes back as an int, a dict whose keys reordered — produces a false report of
    tampering. A false alarm here is not a small bug: it is the alarm everyone
    learns to ignore, after which the real one is invisible too.
    """
    write(detail='a detail with unicode: åéîøü and a "quote"', diff_digest="sha256:abc")
    row = rows()[0]

    assert integrity.compute_event_hash(row, row.prev_hash) == row.event_hash


def test_every_hashed_field_actually_changes_the_hash(db_engine):
    """A field in HASHED_FIELDS that did not affect the hash would be alterable.

    Checked mechanically rather than by reading the list, because the failure is
    silent: a column that drops out of the payload can be edited freely and the
    chain still verifies, which is a verifier that reports `intact` over modified
    data.
    """
    write()
    row = rows()[0]
    baseline = integrity.compute_event_hash(row, row.prev_hash)

    for field in integrity.HASHED_FIELDS:
        original = getattr(row, field)
        setattr(row, field, "tampered" if not isinstance(original, dict) else {"x": "y"})
        assert integrity.compute_event_hash(row, row.prev_hash) != baseline, (
            f"{field} is listed in HASHED_FIELDS but changing it does not change "
            "the hash, so it is not actually covered by the chain."
        )
        setattr(row, field, original)

    assert integrity.compute_event_hash(row, row.prev_hash) == baseline


def test_the_prev_hash_is_part_of_the_hash(db_engine):
    """Otherwise records could be reordered without breaking anything."""
    write()
    row = rows()[0]

    assert integrity.compute_event_hash(row, "a" * 64) != integrity.compute_event_hash(
        row, "b" * 64
    )


def test_a_record_with_no_category_is_still_found_by_the_cluster_filter(db_engine):
    """The same NULL-category rule, asserted through the query surface.

    `test_schema_upgrade.py` covers how the row gets there. This covers the thing
    an operator actually does with it: filter the audit page to cluster writes
    and expect every cluster write to be in the result.
    """
    db = database.SessionLocal()
    try:
        db.execute(
            AuditRecord.__table__.insert().values(
                ts=recorder.utcnow(), actor="legacy", verb="patch",
                target=dict(TARGET), dry_run=False, outcome="applied",
                detail="written before the category column existed",
            )
        )
        db.commit()
    finally:
        db.close()
    write(detail="written after")

    cluster = recorder.query(category="cluster")["items"]
    console = recorder.query(category="console")["items"]

    assert {row["detail"] for row in cluster} == {
        "written before the category column existed",
        "written after",
    }
    assert console == []
    # And the unfiltered view agrees with the filtered one, which is the property
    # that makes the filter trustworthy at all.
    assert len(recorder.query()["items"]) == len(cluster)


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #

def test_two_records_cannot_share_a_predecessor(db_engine):
    """The UNIQUE constraint is what makes a forked chain impossible.

    Two replicas that read the same tip compute the same prev_hash. Without the
    constraint both INSERTs succeed and the chain silently branches — and a
    branched chain is one that can have a whole arm quietly amputated. With it,
    the second writer is refused and retries against the new tip.
    """
    write()
    first = rows()[0]

    db = database.SessionLocal()
    try:
        with pytest.raises(Exception):
            db.execute(
                AuditRecord.__table__.insert().values(
                    ts=recorder.utcnow(),
                    category="cluster",
                    actor="second-writer",
                    verb="patch",
                    target=dict(TARGET),
                    dry_run=False,
                    outcome="applied",
                    prev_hash=first.prev_hash,
                    event_hash="c" * 64,
                )
            )
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_a_record_that_cannot_be_chained_is_still_written(db_engine, monkeypatch):
    """Losing the link is bad. Losing the record is worse.

    When contention exhausts the retries, the row goes in unchained rather than
    being dropped: an unchained record still says what happened and reports
    itself as unverifiable, while a dropped one says nothing at all and nothing
    indicates it is missing.
    """
    write()

    # Every attempt collides, simulating a writer that never wins the tip.
    original = integrity.chain_tip
    monkeypatch.setattr(
        integrity, "chain_tip", lambda session: rows()[0].prev_hash
    )
    try:
        audit_id = write(detail="written while contended")
    finally:
        monkeypatch.setattr(integrity, "chain_tip", original)

    assert audit_id is not None, "the record must survive even when it cannot be linked"

    stored = [row for row in rows() if row.id == audit_id][0]
    assert stored.detail == "written while contended"
    assert stored.event_hash is None
    assert stored.prev_hash is None

    report = verify()
    assert report["status"] == integrity.STATUS_PARTIAL
    assert report["unchained"] == 1
    assert report["first_break"] is None
