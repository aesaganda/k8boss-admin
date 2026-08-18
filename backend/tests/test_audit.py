"""
The audit trail (§10).

An audit table is read by more people, for longer, than the cluster it describes,
and it is read at the worst possible moment. So three properties are checked here
rather than assumed:

* **Nothing sensitive lands in it.** The row is assembled from an allowlist, and
  the diff — which can contain Secret data and ConfigMap payloads — is
  deliberately never stored, only digested.
* **It cannot be rewritten.** §10 says there is no delete endpoint; the ORM guard
  is what makes that a property of the system rather than of the routing table.
* **Paging cannot skip or repeat a row.** The trail is appended to while it is
  being read, which is exactly the condition under which OFFSET paging loses
  records — and a record that exists and cannot be found is indistinguishable
  from one that was never written.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database
from app.audit import recorder
from app.errors import Invalid
from app.models import AuditImmutableError, AuditRecord
from tests.conftest import FIXTURE_TOKEN

TARGET = {
    "group": "apps", "version": "v1", "resource": "deployments",
    "namespace": "prod", "name": "checkout",
}


@pytest.fixture
def api(db_engine):
    """A TestClient carrying only this lane's audit router.

    Not ``app.main``, which imports every router in the product — several of them
    owned by other lanes. A lane test that cannot run until every lane has landed
    is a lane test nobody runs.
    """
    from app.api.audit import router as audit_router
    from app.api.exception_handlers import register_exception_handlers
    from app.k8s.context import ClusterContextMiddleware

    application = FastAPI()
    application.add_middleware(ClusterContextMiddleware)
    register_exception_handlers(application)
    application.include_router(audit_router)
    return TestClient(application)


def write(**overrides):
    """Append one record with sensible defaults."""
    fields = {
        "verb": "patch", "target": dict(TARGET), "dry_run": False,
        "outcome": "applied", "detail": "replicas 3 -> 5",
    }
    fields.update(overrides)
    return recorder.record(**fields)


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #

def test_a_record_round_trips_through_the_query(db_engine):
    audit_id = write(diff_digest="sha256:abc")

    (row,) = recorder.query()["items"]

    assert row["id"] == audit_id
    assert row["verb"] == "patch"
    assert row["outcome"] == "applied"
    assert row["dry_run"] is False
    assert row["detail"] == "replicas 3 -> 5"
    assert row["diff_digest"] == "sha256:abc"
    assert row["target"] == {**TARGET, "subresource": None}
    assert row["ts"].endswith("Z"), (
        "without the Z, JavaScript parses it as local time and every timestamp "
        "shifts by the viewer's UTC offset"
    )


def test_the_actor_defaults_to_anonymous_and_is_recorded_as_advisory(db_engine):
    """Auth-disabled legacy mode accepts advisory attribution; §10 says so plainly. A
    spoofable field presented as verified identity would be a wrong answer with a
    confident face — recording it as attribution is the honest version."""
    write()

    assert recorder.query()["items"][0]["actor"] == "anonymous"


def test_every_outcome_is_recordable_and_filterable(db_engine):
    for outcome in sorted(recorder.OUTCOMES):
        write(outcome=outcome)

    for outcome in sorted(recorder.OUTCOMES):
        rows = recorder.query(outcome=outcome)["items"]
        assert [row["outcome"] for row in rows] == [outcome]


def test_an_unknown_outcome_is_refused_rather_than_stored(db_engine):
    """Stored, it would be invisible to every filter the UI offers — a record
    that exists and cannot be found."""
    with pytest.raises(ValueError):
        write(outcome="succeeded")


def test_an_unknown_outcome_filter_is_refused_rather_than_ignored(db_engine):
    """Ignoring it returns every record under a filter the caller believes is
    applied."""
    with pytest.raises(Invalid):
        recorder.query(outcome="succeeded")


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def test_a_target_carrying_anything_but_the_allowlist_is_stripped(db_engine):
    """The target is caller-supplied, stored verbatim and echoed back. An
    allowlist is what stops a future call site putting a token in it by passing a
    richer dict."""
    write(target={**TARGET, "token": FIXTURE_TOKEN, "body": {"data": {"password": "hunter2"}}})

    row = recorder.query()["items"][0]

    assert set(row["target"]) == {
        "group", "version", "resource", "namespace", "name", "subresource",
    }
    assert FIXTURE_TOKEN not in str(row)
    assert "hunter2" not in str(row)


def test_the_whole_table_is_free_of_credential_material(db_engine):
    """Belt and braces over the allowlist: every column of every row, scanned."""
    write(target={**TARGET, "token": FIXTURE_TOKEN}, detail="replicas 3 -> 5",
          diff_digest="sha256:9f2c", error=None)

    session = database.SessionLocal()
    try:
        rows = session.query(AuditRecord).all()
        dumped = " ".join(
            str(getattr(row, column.name)) for row in rows
            for column in AuditRecord.__table__.columns
        )
    finally:
        session.close()

    assert FIXTURE_TOKEN not in dumped


def test_the_diff_is_never_stored_only_its_digest(db_engine):
    """A diff can carry Secret data and ConfigMap payloads. The digest still
    proves that what was confirmed is what was applied, without keeping the
    bytes that proved it."""
    columns = {column.name for column in AuditRecord.__table__.columns}

    assert "diff" not in columns
    assert "diff_digest" in columns


# --------------------------------------------------------------------------- #
# Append-only
# --------------------------------------------------------------------------- #

def test_an_existing_record_cannot_be_rewritten(db_engine):
    """An audit trail that can be edited answers a different question from the
    one operators believe they are asking it."""
    write()

    session = database.SessionLocal()
    try:
        row = session.query(AuditRecord).one()
        row.outcome = "dry_run"
        with pytest.raises(AuditImmutableError):
            session.flush()
    finally:
        session.rollback()
        session.close()


def test_an_existing_record_cannot_be_deleted(db_engine):
    write()

    session = database.SessionLocal()
    try:
        session.delete(session.query(AuditRecord).one())
        with pytest.raises(AuditImmutableError):
            session.flush()
    finally:
        session.rollback()
        session.close()


# --------------------------------------------------------------------------- #
# Failure to record
# --------------------------------------------------------------------------- #

def test_a_failed_insert_returns_null_rather_than_failing_the_write(db_engine, monkeypatch):
    """By the time a real write is audited it has already reached the cluster.
    Raising here would report a failure for a change that happened — the worst
    direction to be wrong in. `auditId: null` says the change is real and its
    record is not."""
    def explode():
        raise RuntimeError("the audit database is unreachable")

    monkeypatch.setattr(database, "SessionLocal", explode)

    assert write() is None


# --------------------------------------------------------------------------- #
# Cluster attribution
# --------------------------------------------------------------------------- #

def test_the_cluster_name_is_denormalised_onto_the_row(db_engine, registered_cluster):
    """A cluster can be de-registered, and a row that then renders as "cluster 7"
    is unreadable exactly when it is needed."""
    write()

    row = recorder.query()["items"][0]

    assert row["cluster_id"] == registered_cluster.id
    assert row["cluster_name"] == "prod-eu"


def test_the_cluster_filter_narrows_and_omitting_it_does_not(db_engine, registered_cluster):
    """§10's cluster_id is a filter, not §1.1's scoping: an audit page silently
    scoped to the selected cluster would answer "did anyone touch prod" with a no
    it cannot support."""
    write()

    assert len(recorder.query()["items"]) == 1
    assert len(recorder.query(cluster_id=registered_cluster.id)["items"]) == 1
    assert recorder.query(cluster_id=registered_cluster.id + 999)["items"] == []


# --------------------------------------------------------------------------- #
# Paging and filters
# --------------------------------------------------------------------------- #

def test_records_come_back_newest_first(db_engine):
    ids = [write(detail=f"write {n}") for n in range(5)]

    assert [row["id"] for row in recorder.query()["items"]] == list(reversed(ids))


def test_the_cursor_pages_without_skipping_or_repeating(db_engine):
    ids = [write(detail=f"write {n}") for n in range(5)]

    first = recorder.query(limit=2)
    assert [row["id"] for row in first["items"]] == ids[-1:-3:-1]
    assert first["continue"] is not None
    assert first["remaining"] == 3

    second = recorder.query(limit=2, cursor=first["continue"])
    third = recorder.query(limit=2, cursor=second["continue"])

    seen = [row["id"] for page in (first, second, third) for row in page["items"]]
    assert seen == list(reversed(ids)), "every record, exactly once"
    assert third["continue"] is None
    assert third["remaining"] is None, (
        "not 0: the count is only paid for when there is a next page, and null "
        "means unknown"
    )


def test_a_record_appended_mid_page_cannot_shift_the_window(db_engine):
    """Cursor paging exists for this: with OFFSET, an insert between two requests
    pushes a row from page one onto page two, where it is read twice."""
    ids = [write(detail=f"write {n}") for n in range(4)]

    first = recorder.query(limit=2)
    write(detail="a concurrent write")
    second = recorder.query(limit=2, cursor=first["continue"])

    assert [row["id"] for row in second["items"]] == ids[1::-1]


def test_an_unparseable_cursor_is_refused(db_engine):
    with pytest.raises(Invalid) as caught:
        recorder.query(cursor="page-two")

    assert caught.value.context["parameter"] == "cursor"


def test_the_actor_filter_matches_exactly(db_engine):
    from app.k8s.context import reset_current_user, set_current_user

    token = set_current_user("erens")
    try:
        write(detail="by erens")
    finally:
        reset_current_user(token)
    write(detail="by anonymous")

    rows = recorder.query(actor="erens")["items"]
    assert [row["detail"] for row in rows] == ["by erens"]


def test_since_bounds_the_window_and_a_bad_value_is_refused(db_engine):
    write()

    assert recorder.query(since="2000-01-01T00:00:00Z")["items"] != []
    assert recorder.query(since="2999-01-01T00:00:00Z")["items"] == []

    with pytest.raises(Invalid) as caught:
        recorder.query(since="last tuesday")
    assert caught.value.context["parameter"] == "since"


# --------------------------------------------------------------------------- #
# §10 over HTTP
# --------------------------------------------------------------------------- #

def test_the_endpoint_returns_the_1_2_envelope(db_engine, api):
    write()

    response = api.get("/api/audit")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "continue", "remaining", "partial", "unavailable"}
    assert body["partial"] is False
    assert body["unavailable"] == []
    assert body["items"][0]["verb"] == "patch"


def test_the_endpoint_rejects_an_unknown_outcome_with_the_1_3_envelope(db_engine, api):
    response = api.get("/api/audit?outcome=succeeded")

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert "applied" in body["hint"]


def test_the_endpoint_pages_with_the_continue_token(db_engine, api):
    for n in range(3):
        write(detail=f"write {n}")

    first = api.get("/api/audit?limit=2").json()
    second = api.get(f"/api/audit?limit=2&cursor={first['continue']}").json()

    assert len(first["items"]) == 2
    assert [row["detail"] for row in second["items"]] == ["write 0"]
    assert second["continue"] is None
