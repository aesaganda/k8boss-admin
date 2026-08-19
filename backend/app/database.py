"""
SQLAlchemy engine, session factory and schema creation.

The console's own state only: registered clusters and the audit trail. Cluster
*contents* are never cached here — every resource the UI shows is read live, so
the console can never show a stale answer with a confident face. That is a
deliberate limit on this database's job, not an omission.

Dev runs SQLite and production runs PostgreSQL, so the engine is configured for
both and nothing above it may depend on engine-specific behaviour.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")

# SQLite needs check_same_thread off because FastAPI runs sync route handlers in
# a threadpool, so the connection that opened a session is rarely the thread
# that uses it. Everything else (PostgreSQL) gets pool_pre_ping, which recycles
# connections that a database restart or an idle load-balancer timeout killed —
# without it the first request after a failover fails with a stale-connection
# error that looks like a bug in whatever endpoint happened to be first.
_engine_kwargs: dict = {
    "connect_args": {"check_same_thread": False} if _is_sqlite else {},
    "pool_pre_ping": not _is_sqlite,
    "echo": False,
}
if not _is_sqlite:
    # Sized for the console's actual shape: sync handlers hold a session only
    # long enough to read a cluster row or append an audit record; the slow part
    # of every request is the Kubernetes call, which holds no database
    # connection at all. pool_recycle keeps connections from going stale across
    # a Postgres failover or an LB idle timeout.
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)

engine = create_engine(settings.database_url, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


def get_db():
    """FastAPI dependency yielding a session that is always closed.

    Resolves ``SessionLocal`` from the module globals at call time rather than
    binding it at import, so tests can swap in an in-memory engine by patching
    ``app.database.SessionLocal`` and have every consumer follow — including the
    cluster client manager, which opens its own sessions outside the request
    lifecycle.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    """Create every declared table, upgrade older ones, install the audit guards.

    Importing ``app.models`` here rather than at module scope keeps the import
    graph one-directional (models -> database, never the reverse) while still
    guaranteeing that every model class is registered on ``Base.metadata`` before
    ``create_all`` runs. A model imported by nothing at startup would otherwise
    have its table silently missing until the first request touched it.

    ``create_all`` creates missing *tables* and never adds a column to a table
    that already exists, so it is followed by :mod:`app.schema_upgrade`, which
    adds the additive columns an older database is missing and **refuses to
    start** if any is still absent afterwards. Without that second step an
    upgraded deployment serves every page correctly and fails on the first audit
    write — a console that records nothing, with no signal until somebody looks
    for a record that was never written.
    """
    from app import models  # noqa: F401  (registers the mapped classes)
    from app import schema_upgrade
    from app.audit import integrity

    Base.metadata.create_all(bind=engine)
    schema_upgrade.upgrade(engine)
    models.install_audit_append_only_guard()
    integrity.install_audit_chain()
