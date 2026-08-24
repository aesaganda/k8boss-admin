"""
The engine, the session dependency, and what startup guarantees.

Small module, three properties worth pinning:

**The engine is configured differently for SQLite and PostgreSQL, and both
matter.** Dev runs one and production the other, so a setting that is right for
the developer and wrong for the deployment is a setting that is only ever
exercised in the place it is wrong. ``check_same_thread`` off is what lets a
sync handler in FastAPI's threadpool use a session another thread opened;
``pool_pre_ping`` is what keeps the first request after a Postgres failover from
failing with a stale-connection error that reads as a bug in whichever endpoint
happened to be first.

**``get_db`` resolves the session factory at call time.** Every test in this
suite depends on that: patching ``app.database.SessionLocal`` is how a test
database reaches the components that open their own sessions outside the request
lifecycle. Bound at import, the patch would be silently ignored and those
components would talk to the real database.

**``create_tables`` is the only place the audit guards are installed.** A startup
that created the tables and forgot the guards would produce a console that
records, and permits the editing of, an audit trail — with no signal until
someone audits the auditor.
"""

from __future__ import annotations

import importlib.util

import pytest
import sqlalchemy
from sqlalchemy import inspect, text

from app import database
from app.config import settings
from app.models import AuditImmutableError


def _load_database_module(monkeypatch, url):
    """Import a *second*, independent copy of ``app.database`` for ``url``.

    A fresh module rather than ``importlib.reload``: reloading would rebind
    ``Base`` to a new class while every already-imported model stays mapped to
    the old one, so the tests that follow would fail somewhere unrelated to the
    thing they assert.

    ``create_engine`` is captured rather than called, which keeps the assertion
    on the arguments this module *decides* — the pool shape and the connect
    args — instead of on the private attributes an Engine exposes for them.
    """
    calls: list[tuple[str, dict]] = []
    real_create_engine = sqlalchemy.create_engine

    def fake_create_engine(database_url, **kwargs):
        calls.append((database_url, kwargs))
        return real_create_engine("sqlite://")

    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)

    spec = importlib.util.spec_from_file_location(
        "app._database_under_test", database.__file__,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (_url, kwargs), = calls
    return module, kwargs


# --------------------------------------------------------------------------- #
# Engine configuration
# --------------------------------------------------------------------------- #

def test_sqlite_is_configured_for_fastapis_threadpool(monkeypatch):
    """A sync handler runs in a threadpool, so the thread that uses a session is
    rarely the one that opened its connection — which SQLite rejects by
    default."""
    _module, kwargs = _load_database_module(monkeypatch, "sqlite:///./k8boss.db")

    assert kwargs["connect_args"] == {"check_same_thread": False}
    assert kwargs["pool_pre_ping"] is False, (
        "a pre-ping on SQLite buys nothing: there is no connection to lose"
    )
    assert "pool_size" not in kwargs, (
        "SQLite's default pool is what a file-backed database wants; sizing it "
        "like a network database only adds connections to the same file"
    )


def test_postgres_gets_pre_ping_and_a_bounded_recycled_pool(monkeypatch):
    """Without the pre-ping, the first request after a failover or an idle
    load-balancer timeout fails on a connection the pool still believes in."""
    _module, kwargs = _load_database_module(
        monkeypatch, "postgresql+psycopg2://console@db.internal/k8boss",
    )

    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 10
    assert kwargs["max_overflow"] == 20
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["connect_args"] == {}, (
        "check_same_thread is a SQLite keyword: passed to psycopg2 it is a "
        "TypeError at connect time, on the first request"
    )


def test_sql_is_never_echoed_to_the_log(monkeypatch):
    """`echo` logs every statement, and the audit trail's statements carry the
    values of the rows being written."""
    for url in ("sqlite://", "postgresql://console@db.internal/k8boss"):
        _module, kwargs = _load_database_module(monkeypatch, url)
        assert kwargs["echo"] is False, url


# --------------------------------------------------------------------------- #
# get_db
# --------------------------------------------------------------------------- #

def test_get_db_yields_a_session_from_the_factory_installed_at_call_time(db_engine):
    """Bound at import instead, a test's in-memory database would never reach
    the code under test and the suite would exercise the real one."""
    generator = database.get_db()
    session = next(generator)

    assert session.bind is db_engine
    assert session.execute(text("select 1")).scalar() == 1

    with pytest.raises(StopIteration):
        next(generator)


def test_the_session_is_closed_even_when_the_request_fails(monkeypatch, db_engine):
    """The failure path is the one that leaks: a handler that raises with the
    session still checked out exhausts the pool one request at a time."""
    closed: list[bool] = []

    class RecordingSession:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(database, "SessionLocal", RecordingSession)

    generator = database.get_db()
    next(generator)
    with pytest.raises(RuntimeError):
        generator.throw(RuntimeError("the handler failed"))

    assert closed == [True]


# --------------------------------------------------------------------------- #
# create_tables
# --------------------------------------------------------------------------- #

def test_create_tables_creates_every_declared_table(monkeypatch, tmp_path):
    """`create_all` only creates the tables of models that are registered, and a
    model imported by nothing at startup registers nothing — its table is then
    missing until the first request touches it."""
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'console.db'}")
    monkeypatch.setattr(database, "engine", engine)

    database.create_tables()

    tables = set(inspect(engine).get_table_names())
    assert {"clusters", "users", "auth_sessions", "login_attempts", "audit_records"} <= (
        tables
    )


def test_create_tables_installs_the_append_only_guard_on_the_audit_trail(
    monkeypatch, tmp_path,
):
    """The guard is what makes the audit trail evidence. Installed anywhere other
    than startup, a deployment that skipped that path would let the record of a
    write be edited by whoever made it."""
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'console.db'}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "SessionLocal",
        sqlalchemy.orm.sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    database.create_tables()

    from app.audit import recorder

    recorder.record(
        verb="patch",
        target={"group": "apps", "version": "v1", "resource": "deployments",
                "namespace": "prod", "name": "checkout"},
        dry_run=False, outcome="applied", detail="replicas 3 -> 5",
    )

    session = database.SessionLocal()
    try:
        from app.models import AuditRecord

        row = session.query(AuditRecord).one()
        row.detail = "replicas 3 -> 3"
        with pytest.raises(AuditImmutableError):
            session.flush()
    finally:
        session.rollback()
        session.close()


def test_create_tables_is_safe_to_run_against_a_database_that_already_has_them(
    monkeypatch, tmp_path,
):
    """Every deployment runs it on every start, so the second run is the normal
    case rather than the exception."""
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'console.db'}")
    monkeypatch.setattr(database, "engine", engine)

    database.create_tables()
    database.create_tables()

    assert "audit_records" in inspect(engine).get_table_names()
