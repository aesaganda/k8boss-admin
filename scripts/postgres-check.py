#!/usr/bin/env python
"""
The deliberate PostgreSQL check that CI cannot run.

`.github/workflows/ci.yml` exercises SQLite only, and CLAUDE.md is explicit that
anything engine-divergent needs a real PostgreSQL run before it is believed. This
script is that run, kept in the repository so the next person changing the audit
schema or the throttle has something to execute rather than a paragraph telling
them they ought to.

It covers the four places this codebase is engine-divergent, and one of them was
already wrong when this script was first written:

1. `ALTER TABLE ... ADD COLUMN` in `app/schema_upgrade.py`, against a *populated*
   table. The first version inspected through the engine while its own ALTER held
   an ACCESS EXCLUSIVE lock, and PostgreSQL deadlocked at startup — forever,
   before serving a request. SQLite cannot reproduce it.
2. The UNIQUE index on a nullable column (`audit_records.prev_hash`), the many
   NULLs that must coexist under it, and the IntegrityError retry it drives.
3. `yield_per` streaming in the audit export.
4. The sign-in throttle's reservation, under genuinely concurrent connections —
   the race it closes cannot be exhibited by the test suite's shared in-memory
   SQLite connection.

It also confirms the two claims the audit trail makes about itself on the engine
that matters: a `psql`-level UPDATE is detected, and records written before the
chain existed are reported as `unchained` with the verdict withheld.

USAGE
-----
Point it at any throwaway PostgreSQL and run it from anywhere::

    K8BOSS_ADMIN_PG_URL=postgresql+psycopg2://postgres@localhost:5432/k8boss_check \
        python scripts/postgres-check.py

The default assumes a local cluster on a unix socket in /tmp, port 55432 — the
shape `docker run -p` or a hand-rolled `initdb` gives you.

**It DROPs and recreates `audit_records` and `users` in the target database.**
Give it a scratch database, never one holding anything.
"""

from __future__ import annotations

import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"

URL = os.environ.get(
    "K8BOSS_ADMIN_PG_URL",
    "postgresql+psycopg2://postgres@/k8bosstest?host=/tmp&port=55432",
)

os.environ["DATABASE_URL"] = URL
os.environ["ENCRYPTION_KEY"] = "postgres-check-key"
os.environ["ADMIN_ALLOW_MUTATIONS"] = "false"
os.environ["SECRET_REVEAL_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, inspect, text, select, update
from sqlalchemy.orm import sessionmaker

engine = create_engine(URL)

from app import database
database.engine = engine
database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

from app import schema_upgrade
from app.audit import integrity, recorder
from app.database import Base
from app.models import AuditRecord, install_audit_append_only_guard

# --- Start from the LEGACY schema, exactly as an upgraded deployment would. ---
with engine.begin() as c:
    c.execute(text("DROP TABLE IF EXISTS audit_records CASCADE"))
    c.execute(text("DROP TABLE IF EXISTS users CASCADE"))
    c.execute(text("""
        CREATE TABLE audit_records (
            id SERIAL PRIMARY KEY,
            ts TIMESTAMP NOT NULL,
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
            error TEXT)"""))
    c.execute(text("""
        CREATE TABLE users (
            id SERIAL PRIMARY KEY, username VARCHAR(255) NOT NULL UNIQUE,
            display_name VARCHAR(255), email VARCHAR(320), role VARCHAR(32) NOT NULL,
            auth_source VARCHAR(32) NOT NULL, password_hash TEXT,
            active BOOLEAN NOT NULL, last_login TIMESTAMP,
            created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)"""))
    c.execute(text("INSERT INTO audit_records (ts, actor, verb, target, dry_run, outcome, detail) "
                   "VALUES (now(), 'legacy', 'patch', '{}', false, 'applied', 'pre-chain row')"))

Base.metadata.create_all(bind=engine)

# --- 1. ADD COLUMN on a populated Postgres table ---
schema_upgrade.upgrade(engine)
cols = {c["name"] for c in inspect(engine).get_columns("audit_records")}
assert {"category", "prev_hash", "event_hash"} <= cols, cols
assert "external_id" in {c["name"] for c in inspect(engine).get_columns("users")}
print("PASS  ALTER TABLE ADD COLUMN on a populated PostgreSQL table")

assert schema_upgrade.apply_additive_upgrades(engine) == []
print("PASS  upgrade is idempotent on PostgreSQL")

idx = {i["name"]: i for i in inspect(engine).get_indexes("audit_records")}
assert idx["ix_audit_prev_hash"]["unique"], idx["ix_audit_prev_hash"]
print("PASS  UNIQUE index on prev_hash created on PostgreSQL")

install_audit_append_only_guard()
integrity.install_audit_chain()

TARGET = {"group": "apps", "version": "v1", "resource": "deployments",
          "namespace": "prod", "name": "checkout"}

# --- 2. Many NULLs coexist under the UNIQUE index (unchained rows) ---
with engine.begin() as c:
    for i in range(3):
        c.execute(text("INSERT INTO audit_records (ts, category, actor, verb, target, dry_run, outcome, detail) "
                       "VALUES (now(), 'cluster', 'legacy', 'patch', '{}', false, 'applied', :d"
                       ")").bindparams(d=f"unchained {i}"))
print("PASS  multiple NULL prev_hash rows coexist under a UNIQUE index on PostgreSQL")

# --- 3. The chain forms and verifies through real TIMESTAMP/BOOLEAN/JSON types ---
ids = [recorder.record(verb="patch", target=dict(TARGET), dry_run=False,
                       outcome="applied", detail=f"change {i}") for i in range(5)]
assert all(i is not None for i in ids), ids

report = recorder.verify_chain()
assert report["verified"] == 5, report
assert report["unchained"] == 4, report      # 1 legacy + 3 inserted above
assert report["status"] == integrity.STATUS_PARTIAL, report
assert report["first_break"] is None, report
print("PASS  hash canonicalisation round-trips through PostgreSQL TIMESTAMP/BOOLEAN/JSON")
print("PASS  pre-chain rows reported as unchained, verdict withheld as `partial`")

# --- 4. Tampering is detected on PostgreSQL ---
Session = database.SessionLocal
db = Session()
victim = db.execute(select(AuditRecord).where(AuditRecord.detail == "change 2")).scalar_one()
victim_id = victim.id
db.close()
with engine.begin() as c:
    c.execute(text("UPDATE audit_records SET actor = 'someone-else' WHERE id = :i"), {"i": victim_id})
report = recorder.verify_chain()
assert report["status"] == integrity.STATUS_BROKEN, report
assert report["first_break"]["id"] == victim_id, report
print("PASS  a psql-level UPDATE is detected on PostgreSQL")

# --- 5. The UNIQUE constraint refuses a forked chain on PostgreSQL ---
with engine.begin() as c:
    row = c.execute(text("SELECT prev_hash FROM audit_records WHERE id = :i"), {"i": victim_id}).scalar()
try:
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_records (ts, category, actor, verb, target, dry_run, outcome, prev_hash, event_hash) "
                       "VALUES (now(),'cluster','forker','patch','{}',false,'applied',:p,'deadbeef')"), {"p": row})
    raise SystemExit("FAIL: PostgreSQL accepted a duplicate prev_hash")
except Exception as e:
    if "SystemExit" in type(e).__name__: raise
    print("PASS  PostgreSQL refuses a duplicate prev_hash (chain fork impossible)")

# --- 6. The export streams from PostgreSQL (yield_per) ---
from app.audit import export as export_service
rows = list(recorder.stream())
assert len(rows) == 9, len(rows)
csv_text = "".join(export_service.render("csv", recorder.stream()))
assert "pre-chain row" in csv_text and "change 4" in csv_text
nd = [l for l in "".join(export_service.render("ndjson", recorder.stream())).splitlines() if l.strip()]
assert len(nd) == 9, len(nd)
print("PASS  streaming export (yield_per) works on PostgreSQL, both formats")

# --- 7. The new query filters run on PostgreSQL ---
assert len(recorder.query(category="cluster")["items"]) == 9
assert recorder.query(category="console")["items"] == []
assert len(recorder.query(verb="patch", dry_run=False)["items"]) == 9
assert len(recorder.query(cluster_id=recorder.NO_CLUSTER)["items"]) == 9
print("PASS  category / verb / dry_run / NO_CLUSTER filters run on PostgreSQL")

print("\nALL POSTGRESQL CHECKS PASSED")

# --- 8. The reservation throttle under REAL concurrent PostgreSQL connections ---
#
# The race this closes is a check-then-act window, and SQLite's shared-connection
# test fixture cannot exhibit it. PostgreSQL with a real pool is the engine the
# claim is actually about.
import threading
from app.config import settings as _settings
from app.errors import TooManyAttempts
from app.identity import throttle
from app.models import LoginAttempt

Base.metadata.create_all(bind=engine)
pooled = create_engine(URL, pool_size=20, max_overflow=10)
database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=pooled)
with pooled.begin() as c:
    c.execute(text("DELETE FROM login_attempts"))
_settings.auth_throttle_max_attempts = 3

BURST = 20
barrier = threading.Barrier(BURST)
allowed: list[bool] = []
guard = threading.Lock()

def _guess():
    barrier.wait()
    try:
        throttle.check("erens")
        ok = True
    except TooManyAttempts:
        ok = False
    with guard:
        allowed.append(ok)

threads = [threading.Thread(target=_guess) for _ in range(BURST)]
for t in threads: t.start()
for t in threads: t.join()

got_through = allowed.count(True)
assert got_through <= 3, (
    f"{got_through} of {BURST} simultaneous attempts got past a budget of 3 on "
    "PostgreSQL — the check-then-act race is still open"
)
assert allowed.count(False) > 0
print(f"PASS  throttle holds under {BURST} concurrent PostgreSQL connections "
      f"({got_through} allowed, budget 3)")

throttle.release("erens")
db = database.SessionLocal()
try:
    remaining = db.execute(select(LoginAttempt)).scalars().all()
finally:
    db.close()
assert remaining == [], remaining
print("PASS  a successful sign-in clears the reservation on PostgreSQL")

print("\nALL POSTGRESQL CHECKS PASSED (including concurrency)")
