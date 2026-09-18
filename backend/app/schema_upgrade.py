"""
Additive schema upgrades for databases created before a column existed.

``Base.metadata.create_all`` creates missing *tables* and never adds a column to
a table that already exists. On a fresh developer database that is invisible,
because every table is created at once from the current models. On an upgraded
deployment it is an ``OperationalError`` on the first audit write — a console
that still serves every read, still shows every page, and silently records
nothing. That is the exact failure this project is built against: the operator
has no signal, and the evidence only surfaces when somebody goes looking for a
record that was never written.

So this module runs at startup, right after ``create_all``, and does two things:

1. adds any column in :data:`ADDITIVE_COLUMNS` that the live table is missing,
2. re-inspects and **raises** if anything is still missing.

Step 2 is the point. A best-effort upgrade that logged a warning and carried on
would reproduce the original failure one layer further in. Refusing to start is
loud, happens before any traffic, and names the column.

**Scope: additive only, and deliberately so.** Every statement issued here is
``ALTER TABLE … ADD COLUMN`` of a nullable column with no default, or
``CREATE … INDEX``. Both have identical semantics on SQLite and PostgreSQL, and
neither rewrites an existing row. This module never issues an ``UPDATE`` against
``audit_records`` — not to back-fill a hash, not to denormalise a field. The
append-only guard in :mod:`app.models` refuses those, and a migration that
learned to work around the guard would establish that the guard can be worked
around.

A column that needs a type change, a rename, a back-fill or a ``NOT NULL``
promotion is out of scope and needs a real migration tool. When that day comes,
this module is the thing to delete, not the thing to extend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdditiveColumn:
    """One nullable column that may be missing from an older database.

    ``ddl_type`` is spelled in the intersection of SQLite's and PostgreSQL's
    grammars rather than rendered from the SQLAlchemy type. ``VARCHAR(n)`` and
    ``INTEGER`` mean the same thing on both; anything that did not would be a
    migration this module has already said it does not do.
    """

    table: str
    column: str
    ddl_type: str
    #: Why the column exists, quoted into the startup log and into the failure
    #: message. An operator hitting a refused boot needs to know what the column
    #: is for before they decide whether to grant DDL and retry.
    purpose: str
    #: Emitted after the column is added. Used for the UNIQUE constraint that
    #: SQLite cannot express in ``ADD COLUMN`` (it rejects ``ADD COLUMN … UNIQUE``
    #: outright), so both engines get it the same way: as a separate index.
    index_ddl: str | None = None


#: Every column added since the first shipped schema, oldest first.
#:
#: Appending here is the whole cost of adding a nullable column to an existing
#: model. Forgetting to append is caught at boot by :func:`assert_schema_current`
#: rather than at the first write by the database.
ADDITIVE_COLUMNS: tuple[AdditiveColumn, ...] = (
    AdditiveColumn(
        table="users",
        column="external_id",
        ddl_type="VARCHAR(255)",
        purpose=(
            "the identity provider's stable subject for a federated account; "
            "binding an account to it is what stops a second issuer minting a "
            "login that takes over an existing username"
        ),
        index_ddl=(
            "CREATE INDEX IF NOT EXISTS ix_users_external_id "
            "ON users (external_id)"
        ),
    ),
    AdditiveColumn(
        table="audit_records",
        column="category",
        ddl_type="VARCHAR(16)",
        purpose=(
            "separates console sign-ins from cluster writes without parsing the "
            "JSON target column, which is the one filter in this schema that "
            "would be engine-divergent"
        ),
        index_ddl=(
            "CREATE INDEX IF NOT EXISTS ix_audit_category_ts "
            "ON audit_records (category, ts)"
        ),
    ),
    AdditiveColumn(
        table="audit_records",
        column="prev_hash",
        ddl_type="VARCHAR(64)",
        purpose="the previous link in the audit hash chain",
        # UNIQUE, because it is what makes a forked chain impossible rather than
        # merely detectable. Safe to add to a populated table: every existing row
        # has NULL here, and a UNIQUE index permits many NULLs on both engines.
        index_ddl=(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_prev_hash "
            "ON audit_records (prev_hash)"
        ),
    ),
    AdditiveColumn(
        table="audit_records",
        column="event_hash",
        ddl_type="VARCHAR(64)",
        purpose="this row's hash over its own content plus the previous link",
        index_ddl=(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_event_hash "
            "ON audit_records (event_hash)"
        ),
    ),
    AdditiveColumn(
        table="clusters",
        column="app_domain",
        ddl_type="VARCHAR(253)",
        purpose=(
            "the cluster's wildcard DNS domain, which §13's generated exposure "
            "hostnames are built under; NULL means the console does not know of "
            "one and therefore offers to generate nothing, because a hostname "
            "under a wildcard that does not exist routes nothing while looking "
            "created"
        ),
    ),
    AdditiveColumn(
        table="clusters",
        column="impersonation_enabled",
        ddl_type="BOOLEAN",
        purpose=(
            "ADR-0007's per-cluster opt-in: whether this console's cluster calls "
            "carry Impersonate-User for the signed-in operator instead of acting "
            "as its own ServiceAccount. NULL means a cluster registered before "
            "the setting existed, which is off — the only safe direction, since "
            "the grant it needs is cluster-admin by proxy when unrestricted"
        ),
    ),
    AdditiveColumn(
        table="auth_sessions",
        column="idp_username",
        ddl_type="VARCHAR(255)",
        purpose=(
            "ADR-0007: the username the identity provider stated, kept verbatim "
            "rather than casefolded, because Impersonate-User has to be the "
            "string the cluster would derive from the same token and not this "
            "console's normalised key for the same person"
        ),
    ),
    AdditiveColumn(
        table="auth_sessions",
        column="idp_groups",
        ddl_type="TEXT",
        purpose=(
            "ADR-0007: the groups claim as a JSON array, where NULL means the "
            "issuer sent no claim at all. That is not an empty list and refuses "
            "impersonation rather than stripping every group-derived permission "
            "the operator holds and reporting the result as permissions they lack"
        ),
    ),
    AdditiveColumn(
        table="audit_records",
        column="impersonated_user",
        ddl_type="VARCHAR(255)",
        purpose=(
            "ADR-0007: the cluster identity a write was actually made as, so an "
            "incident review can join this trail to the API server's own by a "
            "value neither side invented. NULL means the console acted as itself"
        ),
    ),
)

#: Indexes that belong to columns which already existed. Issued unconditionally
#: (every statement is ``IF NOT EXISTS``) because an index is not something
#: ``create_all`` adds to a table it did not create either.
STANDALONE_INDEXES: tuple[str, ...] = (
    # Used by the audit page's actor filter. The throttle has its own table.
    "CREATE INDEX IF NOT EXISTS ix_audit_actor_ts ON audit_records (actor, ts)",
    # The audit page's outcome filter, paged by descending id. Named to match
    # `ix_audit_outcome_id` in app/models.py: a statement here whose name differed
    # from the model's would leave an upgraded database carrying two indexes over
    # the same columns, both maintained on every audit INSERT.
    "CREATE INDEX IF NOT EXISTS ix_audit_outcome_id "
    "ON audit_records (outcome, id)",
    "CREATE INDEX IF NOT EXISTS ix_login_attempt_actor_ts "
    "ON login_attempts (actor, ts)",
)


def _existing_columns(bind, table: str) -> set[str] | None:
    """Column names on a live table, or ``None`` when the table is not there.

    ``None`` and ``set()`` are different answers and the caller branches on it: a
    table that does not exist yet was just created by ``create_all`` with every
    current column, so it needs no upgrade. Treating that as "no columns" would
    make this module try to add every column to a table that already has them.

    **``bind`` must be the same Connection the DDL is running on**, and this is
    not a style point — it is a PostgreSQL deadlock.

    PostgreSQL takes an ``ACCESS EXCLUSIVE`` lock on a table for the duration of
    an ``ALTER TABLE``, and DDL there is transactional, so the lock is held until
    the surrounding transaction commits. Inspecting through the *engine* opens a
    **second** connection, which then blocks on that lock — and the transaction
    holding it is waiting for the inspection to return. The process hangs at
    startup, forever, before it serves a request.

    SQLite does not reproduce it (one connection, different locking), so CI is
    green and only a real upgrade of a real deployment finds it. Passing the
    open connection keeps every catalog read inside the transaction that holds
    the lock, where it is free.
    """
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def apply_additive_upgrades(engine: Engine) -> list[str]:
    """Add every missing additive column and index. Returns what it changed.

    Idempotent: a database already carrying every column produces no statements
    and an empty list. Safe to run on every boot and on every replica — the
    ``ADD COLUMN`` is guarded by an inspection and the indexes are all
    ``IF NOT EXISTS``, so two replicas racing each other at startup produce at
    worst one harmless duplicate-column error, which is re-raised rather than
    swallowed so it cannot hide a real DDL failure.
    """
    applied: list[str] = []

    with engine.begin() as connection:
        for spec in ADDITIVE_COLUMNS:
            # Through `connection`, never `engine` — see _existing_columns for
            # the PostgreSQL deadlock a second connection causes here.
            columns = _existing_columns(connection, spec.table)
            if columns is None or spec.column in columns:
                continue
            statement = (
                f"ALTER TABLE {spec.table} ADD COLUMN {spec.column} {spec.ddl_type}"
            )
            logger.warning(
                "Schema upgrade: adding %s.%s (%s).",
                spec.table, spec.column, spec.purpose,
            )
            connection.execute(text(statement))
            applied.append(statement)

        # Indexes come after every column exists, in their own pass: an index on
        # a column added in this same loop must not be issued before the ALTER
        # that created it, and pairing them per-column would leave the index
        # missing on a database that had the column but not the index (which is
        # exactly what a half-finished previous upgrade leaves behind).
        for spec in ADDITIVE_COLUMNS:
            if not spec.index_ddl:
                continue
            if _existing_columns(connection, spec.table) is None:
                continue
            _execute_index(connection, spec.index_ddl)

        for statement in STANDALONE_INDEXES:
            _execute_index(connection, statement)

    return applied


def _execute_index(connection, statement: str) -> None:
    """Issue one ``CREATE INDEX IF NOT EXISTS``, tolerating only that it exists.

    A UNIQUE index over a column with pre-existing duplicate values fails here,
    and that failure is re-raised. It would mean the audit chain already forked
    before the constraint existed, which is a fact an operator has to be told at
    boot rather than have quietly ignored.
    """
    table = statement.split(" ON ", 1)[-1].split("(")[0].strip()
    if table and _existing_columns(connection, table) is None:
        # `login_attempts` is created by create_all on a fresh database and by
        # this run's create_all on an upgrade, but a database whose create_all
        # could not act has its own error — indexing a table that is not there
        # would mask it with a confusing one.
        return
    try:
        connection.execute(text(statement))
    except SQLAlchemyError:
        logger.error("Schema upgrade: index statement failed: %s", statement)
        raise


def assert_schema_current(engine: Engine) -> None:
    """Raise unless every additive column is present on its live table.

    Called after :func:`apply_additive_upgrades`, and the reason it is separate
    is that the upgrade can partially succeed — a database user with SELECT and
    INSERT but no ALTER gets a permission error on one statement, and without
    this check the process would start, serve reads, and fail on the first audit
    write with a message about a missing column that names nothing about
    permissions.

    Raising at boot costs a crash-looping pod, which is a signal. The
    alternative costs an audit trail with a hole in it, which is not.
    """
    missing: list[str] = []
    # One connection for the whole check, opened after the upgrade transaction
    # has committed. No lock is held here, so this is only about not opening one
    # connection per column.
    with engine.connect() as connection:
        checked = [
            (spec, _existing_columns(connection, spec.table))
            for spec in ADDITIVE_COLUMNS
        ]

    for spec, columns in checked:
        if columns is None:
            # create_all runs before this and creates missing tables. A table
            # still absent here means create_all itself could not act, which is
            # a different fault with its own error; do not mask it by reporting
            # a column.
            continue
        if spec.column not in columns:
            missing.append(f"{spec.table}.{spec.column} ({spec.purpose})")

    if missing:
        raise RuntimeError(
            "The database schema is missing column(s) this build requires:\n  - "
            + "\n  - ".join(missing)
            + "\n\nThe automatic upgrade could not add them — most often because "
            "the configured database user may INSERT but not ALTER. Grant DDL on "
            "these tables and restart, or apply the ALTER TABLE statements by "
            "hand. Refusing to start: a console that runs without these columns "
            "serves every page correctly and records nothing, and nothing about "
            "it looks wrong until someone goes looking for a record that was "
            "never written."
        )


def upgrade(engine: Engine) -> None:
    """Apply additive upgrades and verify the result. The startup entry point."""
    applied = apply_additive_upgrades(engine)
    if applied:
        logger.warning(
            "Schema upgrade applied %d statement(s). This database predates the "
            "current build; the changes are additive and no row was rewritten.",
            len(applied),
        )
    assert_schema_current(engine)


__all__ = [
    "ADDITIVE_COLUMNS",
    "AdditiveColumn",
    "apply_additive_upgrades",
    "assert_schema_current",
    "upgrade",
]
