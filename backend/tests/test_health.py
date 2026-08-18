"""
GET /api/health (§2).

The property under test is that this endpoint reports on **the console**, not on
the clusters it manages. It answers 200 with ``status: "degraded"`` when a
cluster is down, and it never answers 5xx — a liveness probe that fails because
a customer's cluster is unreachable restarts a perfectly healthy pod, and the
restart fixes nothing.

The second property is that "we have not checked" and "we checked and it failed"
stay apart. Folding the first into the second sends an operator to debug a
cluster that has never been tested; after that happens twice they stop reading
the degraded list.
"""

from __future__ import annotations

import pytest

from app import database
from app.models import Cluster, utcnow


def _register(status: str, name: str = "prod-eu", detail: str | None = None) -> int:
    session = database.SessionLocal()
    try:
        cluster = Cluster(
            name=name,
            api_server="https://api.example:6443",
            token_encrypted="ciphertext",
            status=status,
            status_detail=detail,
            last_connected=utcnow() if status == "connected" else None,
        )
        session.add(cluster)
        session.commit()
        return cluster.id
    finally:
        session.close()


def test_health_is_ok_with_nothing_registered(client):
    response = client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["clusters"] == {"registered": 0, "reachable": 0}
    assert body["degraded"] == []


def test_mutations_reports_the_write_gate(client):
    """§1.6 / §11.5: the UI hides write affordances from this, so it must be
    exact — offering a Scale button that always 403s teaches nothing."""
    assert client.get("/api/health").json()["mutations"] == "disabled"


def test_mutations_enabled_is_reported(client, allow_mutations):
    assert client.get("/api/health").json()["mutations"] == "enabled"


def test_a_connected_cluster_counts_as_reachable(client):
    _register("connected")
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["clusters"] == {"registered": 1, "reachable": 1}
    assert body["degraded"] == []


def test_an_unreachable_cluster_degrades_the_console_not_its_status_code(client):
    _register("disconnected", detail="dial tcp 10.0.0.1:6443: connect: no route to host")
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["clusters"] == {"registered": 1, "reachable": 0}
    assert body["degraded"][0]["reason"] == "unreachable"
    assert "no route to host" in body["degraded"][0]["detail"]


def test_a_never_tested_cluster_is_not_reported_as_unreachable(client):
    """"Never checked" is a different fact from "checked and it failed"."""
    _register("unknown")
    body = client.get("/api/health").json()

    assert body["clusters"] == {"registered": 1, "reachable": 0}
    assert body["degraded"][0]["reason"] == "not_tested"
    assert body["degraded"][0]["component"].startswith("cluster:")


def test_each_failing_cluster_gets_its_own_entry(client):
    _register("connected", name="a")
    _register("disconnected", name="b")
    _register("unknown", name="c")

    body = client.get("/api/health").json()
    assert body["clusters"] == {"registered": 3, "reachable": 1}
    assert {entry["reason"] for entry in body["degraded"]} == {"unreachable", "not_tested"}


def test_a_database_failure_reports_null_counts_not_zero(client, monkeypatch):
    """Zero registered clusters is a claim; this code just failed to check it.

    Reporting 0 is what makes an empty cluster switcher look correct after the
    console has lost its own database.
    """
    def _explode():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(database, "SessionLocal", _explode)
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["clusters"] == {"registered": None, "reachable": None}
    assert body["degraded"][0]["component"] == "database"
    assert "not zero" in body["degraded"][0]["detail"]


@pytest.mark.parametrize("status", ["connected", "disconnected", "unknown"])
def test_health_never_returns_an_error_status(client, status):
    _register(status)
    assert client.get("/api/health").json()["status"] in ("ok", "degraded")
