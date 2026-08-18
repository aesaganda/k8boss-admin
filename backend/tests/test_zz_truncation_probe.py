from __future__ import annotations

from sqlalchemy import delete, select, update

from app import database
from app.audit import integrity, recorder
from app.models import AuditRecord

TARGET = {"group": "apps", "version": "v1", "resource": "deployments",
          "namespace": "prod", "name": "checkout"}


def write(**overrides):
    fields = {"verb": "patch", "target": dict(TARGET), "dry_run": False,
              "outcome": "applied", "detail": "replicas 3 -> 5"}
    fields.update(overrides)
    return recorder.record(**fields)


def rows():
    db = database.SessionLocal()
    try:
        return list(db.execute(select(AuditRecord).order_by(AuditRecord.id)).scalars())
    finally:
        db.close()


def test_truncating_the_newest_records(db_engine):
    for i in range(5):
        write(detail=f"change {i}")
    ids = [r.id for r in rows()]
    db = database.SessionLocal()
    try:
        db.execute(delete(AuditRecord).where(AuditRecord.id.in_(ids[-2:])))
        db.commit()
    finally:
        db.close()
    report = recorder.verify_chain()
    print("TRUNCATION REPORT:", report)
    assert report["status"] == integrity.STATUS_INTACT, report


def test_swapping_two_ids(db_engine):
    for i in range(3):
        write(detail=f"change {i}")
    chain = rows()
    a, b = chain[0].id, chain[1].id
    db = database.SessionLocal()
    try:
        db.execute(update(AuditRecord).where(AuditRecord.id == a).values(id=9001))
        db.execute(update(AuditRecord).where(AuditRecord.id == b).values(id=a))
        db.execute(update(AuditRecord).where(AuditRecord.id == 9001).values(id=b))
        db.commit()
    finally:
        db.close()
    report = recorder.verify_chain()
    print("ID SWAP REPORT:", report)
    listing = recorder.query(limit=10)
    print("LISTING ORDER:", [(i["id"], i["detail"], i["ts"]) for i in listing["items"]])
