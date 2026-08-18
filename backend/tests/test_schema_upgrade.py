"""
Upgrading a database that predates a column.

``Base.metadata.create_all`` creates missing *tables* and never adds a column to
one that already exists. On a fresh developer database that is invisible, because
every table is created at once from the current models — which is exactly why it
is worth a test file. The failure only appears on a real upgrade, and it appears
as a console that serves every page correctly and writes no audit records, with
nothing wrong until somebody goes looking for a record that was never written.

These tests build the *old* schema by hand, run the upgrade over it, and check
both halves: that the columns arrive, and that the process refuses to start if
they did not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import schema_upgrade
from app.models import AuditRecord

#: The audit table as it was before chaining and categories existed. Written out
#: rather than generated, because the point is to reproduce a database this build
#: has never seen.
LEGACY_AUDIT_TABLE = """
CREATE TABLE audit_records (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ts DATETIME NOT NULL,
    actor VARCHAR(255) NOT NULL,
    source_ip VARCHAR(64),
    cluster_id INTEGER,
    cluster_name VARCHAR(255),
    verb VARCHAR(32) NOT NULL,
    target JSON NOT NULL,
    dry_run BOOLEAN NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    detail TEXT,
    diff_digest VARCHAR(80),
    error TEXT
)
"""

LEGACY_USERS_TABLE = """
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(255),
    email VARCHAR(320),
    role VARCHAR(32) NOT NULL,
    auth_source VARCHAR(32) NOT NULL,
    password_hash TEXT,
    active BOOLEAN NOT NULL,
    last_login DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


@pytest.fixture
def legacy_engine():
    """An engine holding the pre-upgrade schema, with a row already in it."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(text(LEGACY_AUDIT_TABLE))
        connection.execute(text(LEGACY_USERS_TABLE))
        connection.execute(
            text(
                "INSERT INTO audit_records "
                "(ts, actor, verb, target, dry_run, outcome, detail) VALUES "
                "('2026-01-01 00:00:00', 'erens', 'patch', '{}', 0, 'applied', 'old row')"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_the_upgrade_adds_every_missing_column(legacy_engine):
    assert "prev_hash" not in columns(legacy_engine, "audit_records")

    schema_upgrade.upgrade(legacy_engine)

    audit = columns(legacy_engine, "audit_records")
    assert {"category", "prev_hash", "event_hash"} <= audit
    assert "external_id" in columns(legacy_engine, "users")


def test_the_upgrade_preserves_existing_rows(legacy_engine):
    """Additive only. Nothing here rewrites a record.

    Not a stylistic preference: the append-only guard refuses an UPDATE against
    this table, and a migration that learned to work around the guard would
    establish that the guard can be worked around.
    """
    schema_upgrade.upgrade(legacy_engine)

    session = sessionmaker(bind=legacy_engine)()
    try:
        (row,) = session.execute(select(AuditRecord)).scalars().all()
    finally:
        session.close()

    assert row.detail == "old row"
    assert row.actor == "erens"
    # Untouched by the upgrade, and therefore reported as `unchained` rather than
    # as verified. Back-filling a hash here would attest whatever the row says
    # today, turning "we do not know" into proof.
    assert row.prev_hash is None
    assert row.event_hash is None


def test_the_upgrade_is_idempotent(legacy_engine):
    """Runs on every boot and on every replica; two racing startups must be safe."""
    first = schema_upgrade.apply_additive_upgrades(legacy_engine)
    second = schema_upgrade.apply_additive_upgrades(legacy_engine)

    assert first, "the first pass should have had work to do"
    assert second == [], "the second pass should have found nothing to change"


def test_a_current_database_needs_no_statements(db_engine):
    """create_all already built it; the upgrader must not touch it."""
    assert schema_upgrade.apply_additive_upgrades(db_engine) == []
    schema_upgrade.assert_schema_current(db_engine)


def test_the_unique_index_on_prev_hash_is_created(legacy_engine):
    """It is what makes a forked audit chain impossible rather than detectable."""
    schema_upgrade.upgrade(legacy_engine)

    indexes = {index["name"] for index in inspect(legacy_engine).get_indexes("audit_records")}
    assert "ix_audit_prev_hash" in indexes
    unique = {
        index["name"]
        for index in inspect(legacy_engine).get_indexes("audit_records")
        if index["unique"]
    }
    assert "ix_audit_prev_hash" in unique


def test_startup_refuses_when_a_column_could_not_be_added(legacy_engine, monkeypatch):
    """The half that matters most.

    A database user with SELECT and INSERT but no ALTER gets a permission error
    on one statement. Without this check the process starts, serves reads, and
    fails on the first audit write with a message about a missing column that
    says nothing about permissions. Refusing to start costs a crash-looping pod,
    which is a signal; the alternative costs an audit trail with a hole in it,
    which is not.
    """
    monkeypatch.setattr(schema_upgrade, "apply_additive_upgrades", lambda engine: [])

    with pytest.raises(RuntimeError) as raised:
        schema_upgrade.upgrade(legacy_engine)

    message = str(raised.value)
    assert "audit_records.prev_hash" in message
    assert "ALTER" in message
    # The message has to say what the operator loses by ignoring it, or the
    # instinct is to remove the check rather than grant the permission.
    assert "records nothing" in message


def test_a_missing_table_is_not_reported_as_a_missing_column(legacy_engine):
    """create_all owns table creation, and its failures have their own error.

    Reporting `users.external_id` as missing when the whole table is absent
    would send an operator to grant ALTER on a table that does not exist.
    """
    schema_upgrade.upgrade(legacy_engine)
    with legacy_engine.begin() as connection:
        connection.execute(text("DROP TABLE users"))

    # The audit columns are present, and the absent `users` table contributes no
    # complaint of its own.
    schema_upgrade.assert_schema_current(legacy_engine)


def test_the_upgrade_inspects_through_its_own_connection(legacy_engine, monkeypatch):
    """The PostgreSQL deadlock, pinned on SQLite where it cannot reproduce.

    PostgreSQL holds an ACCESS EXCLUSIVE lock for an ALTER TABLE and its DDL is
    transactional, so inspecting through the *engine* opens a second connection
    that blocks on the lock the surrounding transaction holds — and that
    transaction is waiting for the inspection. The process hangs at startup,
    forever, before serving a request.

    SQLite does not reproduce it, so this asserts the property that prevents it
    rather than the symptom: every inspection during the upgrade is bound to the
    open connection, never to the engine. Without this the regression is
    invisible to CI and only a real upgrade of a real deployment finds it.
    """
    from sqlalchemy.engine import Connection

    binds: list[type] = []
    original = schema_upgrade._existing_columns

    def recording(bind, table):
        binds.append(type(bind))
        return original(bind, table)

    monkeypatch.setattr(schema_upgrade, "_existing_columns", recording)
    schema_upgrade.apply_additive_upgrades(legacy_engine)

    assert binds, "the upgrade inspected nothing at all"
    assert all(issubclass(kind, Connection) for kind in binds), (
        "apply_additive_upgrades inspected through something other than its own "
        f"Connection ({sorted({k.__name__ for k in binds})}), which deadlocks on "
        "PostgreSQL against the ACCESS EXCLUSIVE lock its own ALTER is holding."
    )


def test_rows_written_before_the_category_column_stay_reachable(legacy_engine):
    """A record no filter can reach is a record that is not in the trail.

    The legacy row predates `category`, so it holds NULL. Matching only non-null
    values would drop it out of **both** category filters while it sits in plain
    sight in the unfiltered view — an operator filtering "cluster writes" would
    be told a write they can see with their own eyes did not happen.

    It is read as `cluster` rather than back-filled: this table is append-only,
    and a migration that learned to UPDATE it would establish that the guard can
    be worked around.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    schema_upgrade.upgrade(legacy_engine)

    session = sessionmaker(bind=legacy_engine)()
    try:
        (row,) = session.execute(select(AuditRecord)).scalars().all()
        stored = row.category
        rendered = row.to_row_dict()["category"]
    finally:
        session.close()

    # Untouched in storage, and never null on the wire.
    assert stored is None
    assert rendered == "cluster"


def test_every_additive_column_states_why_it_exists(db_engine):
    """The purpose text is quoted into the refusal message an operator reads.

    A column with an empty purpose produces a startup failure that names a column
    and explains nothing, which is the failure this project's whole error
    vocabulary exists to avoid.
    """
    for spec in schema_upgrade.ADDITIVE_COLUMNS:
        assert spec.purpose.strip(), f"{spec.table}.{spec.column} has no stated purpose"
        assert spec.ddl_type.strip()
