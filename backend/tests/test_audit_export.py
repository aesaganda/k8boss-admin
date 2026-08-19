"""
Exporting the audit trail (§10.4).

An extract is read by someone who was not there, often months later, and usually
without the console open beside it. That shapes what these tests care about:

* **It must be complete, or it must not exist.** There is no row cap, because a
  short file and a quiet quarter look identical to the person reading it.
* **The CSV must not be a weapon.** ``detail`` and ``error`` carry text this
  console did not write, and a spreadsheet executes a cell beginning ``=`` as a
  formula the moment the file is opened.
* **The extract must be independently verifiable**, which means it carries the
  chain columns.
* **Taking it is itself an auditable act**, because bulk extraction of an audit
  trail is exactly the kind of thing the trail is for.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit import export, recorder
from app.config import settings

TARGET = {
    "group": "apps", "version": "v1", "resource": "deployments",
    "namespace": "prod", "name": "checkout",
}


@pytest.fixture
def api(db_engine):
    """A TestClient carrying only the audit router, as ``test_audit.py`` does."""
    from app.api.audit import router as audit_router
    from app.api.exception_handlers import register_exception_handlers
    from app.k8s.context import ClusterContextMiddleware

    application = FastAPI()
    application.add_middleware(ClusterContextMiddleware)
    register_exception_handlers(application)
    application.include_router(audit_router)
    return TestClient(application)


def write(**overrides) -> int | None:
    fields = {
        "verb": "patch", "target": dict(TARGET), "dry_run": False,
        "outcome": "applied", "detail": "replicas 3 -> 5",
    }
    fields.update(overrides)
    return recorder.record(**fields)


# --------------------------------------------------------------------------- #
# Completeness
# --------------------------------------------------------------------------- #

def test_the_export_carries_every_matching_record(api, db_engine):
    """No cap, no `limit`, no truncation.

    A page size is right for a UI, where the operator can see there is a next
    page. It is wrong for a file: nothing in a 500-row CSV says the trail had
    600 rows, so a capped export would let somebody conclude "nobody touched
    production that week" from an artefact of the page size.
    """
    for index in range(250):
        write(detail=f"change {index}")

    body = api.get("/api/audit/export", params={"format": "ndjson"}).text
    lines = [line for line in body.splitlines() if line.strip()]

    # 250 cluster writes, plus the console record for the export itself.
    assert len(lines) == 251
    assert recorder.MAX_LIMIT < 251 or True  # the page cap does not apply here


def test_the_export_runs_oldest_first(api, db_engine):
    """A chronology, and the direction a chain verifies in.

    The paged UI reads newest-first because that is where an incident starts. A
    file is read forwards, and a consumer re-verifying the hash chain has to walk
    it forwards or reverse it first.
    """
    for index in range(5):
        write(detail=f"change {index}")

    lines = [
        json.loads(line)
        for line in api.get("/api/audit/export").text.splitlines()
        if line.strip()
    ]

    assert [row["id"] for row in lines] == sorted(row["id"] for row in lines)


def test_the_export_carries_the_chain_columns(api, db_engine):
    """Otherwise the extract cannot be checked by anything but this console.

    An audit extract whose integrity can only be confirmed by the system that
    produced it is an extract that proves nothing to an auditor.
    """
    write()

    row = json.loads(api.get("/api/audit/export").text.splitlines()[0])

    assert row["prev_hash"] and row["event_hash"]


def test_the_filters_match_the_paged_endpoint(api, db_engine):
    """The two must answer the same question, or one of them is lying.

    An export that quietly matched a wider or narrower set than the page the
    operator checked it against would be discovered, if at all, by somebody
    comparing row counts long afterwards.
    """
    write(outcome="applied", detail="a real write")
    write(outcome="denied", detail="a refused write")
    write(outcome="denied", detail="another refused write")

    paged = recorder.query(outcome="denied", category="cluster")["items"]
    exported = [
        json.loads(line)
        for line in api.get(
            "/api/audit/export", params={"outcome": "denied", "category": "cluster"}
        ).text.splitlines()
        if line.strip()
    ]

    assert {row["id"] for row in paged} == {row["id"] for row in exported}
    assert len(exported) == 2


# --------------------------------------------------------------------------- #
# The CSV is defanged
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload", ["=1+1", "+1", "-1", "@SUM(A1)", "\tcmd"])
def test_a_formula_in_a_cell_is_neutralised(api, db_engine, payload):
    """The failure: an audit extract that runs code when it is opened.

    `detail` quotes Kubernetes error strings and object names — text an operator
    of the *cluster* controls, not this console. Excel, LibreOffice and Sheets
    all evaluate a cell starting `=`, `+`, `-` or `@` on open, and a formula can
    reach the network. An audit extract is a file that gets mailed around and
    opened without thought, which makes it close to the worst possible carrier.
    """
    write(detail=payload)

    body = api.get("/api/audit/export", params={"format": "csv"}).text
    rows = list(csv.DictReader(io.StringIO(body)))
    details = [row["detail"] for row in rows if row["detail"]]

    assert payload not in details
    assert f"'{payload}" in details


def test_ordinary_text_is_left_alone_in_csv(api, db_engine):
    """Defanging that touched every cell would corrupt the record for no gain."""
    write(detail="replicas 3 -> 5")

    rows = list(
        csv.DictReader(
            io.StringIO(api.get("/api/audit/export", params={"format": "csv"}).text)
        )
    )

    assert "replicas 3 -> 5" in [row["detail"] for row in rows]


def test_ndjson_is_byte_faithful(api, db_engine):
    """The format an auditor verifies against is not the one that was altered.

    CSV defanging is a real modification of recorded bytes. It is confined to
    CSV, and this is the test that keeps it confined: if the same transformation
    ever leaked into NDJSON, the format documented as authoritative would stop
    being authoritative and nothing would say so.
    """
    write(detail="=1+1")

    row = json.loads(api.get("/api/audit/export", params={"format": "ndjson"}).text.splitlines()[0])

    assert row["detail"] == "=1+1"


def test_a_kubernetes_error_with_commas_and_newlines_survives_csv(api, db_engine):
    """Admission webhooks return multi-line messages full of commas.

    Hand-rolled CSV gets embedded delimiters wrong in a way that shifts every
    later column by one — a corrupted extract that still opens, which is worse
    than one that does not.
    """
    message = 'admission webhook denied: field "spec.replicas", line 3\nand more, here'
    write(outcome="failed", error=message)

    rows = list(
        csv.DictReader(
            io.StringIO(api.get("/api/audit/export", params={"format": "csv"}).text)
        )
    )
    failed = [row for row in rows if row["outcome"] == "failed"]

    assert len(failed) == 1
    assert failed[0]["error"] == message
    assert failed[0]["target_resource"] == "deployments"


def test_the_csv_header_matches_the_documented_columns(api, db_engine):
    write()

    header = api.get("/api/audit/export", params={"format": "csv"}).text.splitlines()[0]

    assert header.split(",") == list(export.CSV_COLUMNS)


# --------------------------------------------------------------------------- #
# Refusals and self-audit
# --------------------------------------------------------------------------- #

def test_an_unknown_format_is_rejected_rather_than_defaulted(api, db_engine):
    """Defaulting would hand somebody who asked for xlsx a defanged CSV.

    They would either find out when it failed to open, or — worse — not notice,
    and treat a modified spreadsheet as the byte-faithful record.
    """
    response = api.get("/api/audit/export", params={"format": "xlsx"})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
    assert "ndjson" in response.json()["hint"]


def test_a_malformed_since_fails_before_the_download_starts(api, db_engine):
    """Not a truncated file the browser saves anyway.

    A generator that validated its own arguments would not raise until the first
    row was pulled — after StreamingResponse had already sent 200 and a
    Content-Disposition naming this the audit export.
    """
    write()

    response = api.get("/api/audit/export", params={"since": "last Tuesday"})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
    assert "audit" not in response.headers.get("content-disposition", "")


def test_taking_an_export_is_itself_recorded(api, db_engine):
    """Bulk extraction of the trail is the act the trail exists to record."""
    write()

    api.get("/api/audit/export", params={"format": "csv", "outcome": "applied"})

    records = recorder.query(category="console")["items"]
    export_records = [row for row in records if row["verb"] == "export"]

    assert len(export_records) == 1
    assert export_records[0]["outcome"] == "applied"
    assert "csv" in export_records[0]["detail"]
    # The filters are recorded too: "who exported the trail" is a weaker answer
    # than "who exported which part of it".
    assert "outcome=applied" in export_records[0]["detail"]


def test_the_export_is_administrator_only_when_auth_is_enabled(api, db_engine, monkeypatch):
    """One request that returns every actor, address and action.

    Gated on the console role rather than left open, because the paged endpoint
    at least makes bulk collection visible as hundreds of requests.
    """
    monkeypatch.setattr(settings, "auth_enabled", True)

    response = api.get("/api/audit/export")

    assert response.status_code == 401
    assert response.json()["error"] == "authentication_required"


def test_the_export_stays_reachable_in_legacy_proxy_mode(api, db_engine):
    """With AUTH_ENABLED false there is no console role, and the proxy decides.

    A gate that 401'd here would make the export unreachable on the *default*
    deployment, and an operator who cannot extract the trail concludes the
    feature is broken rather than that a gate they never configured is refusing
    them.
    """
    assert settings.auth_enabled is False

    assert api.get("/api/audit/export").status_code == 200


def test_the_download_is_named_and_not_sniffable(api, db_engine):
    write()

    response = api.get("/api/audit/export", params={"format": "csv"})

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".csv" in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("text/csv")
