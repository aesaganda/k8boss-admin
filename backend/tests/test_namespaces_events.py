"""
The namespace and event endpoints (§5).

Two pages, one shared failure mode, and one that is unique to events.

**Namespaces.** ``pod_count`` is ``null`` — never ``0`` — when the pod listing
failed, and ``0`` when the namespace is genuinely empty. Those two rows look
identical to an operator doing a cleanup, and one of them is the namespace they
are about to delete. The namespace listing itself is the primary read: refused,
it is a 403 with a hint, not a 200 with an empty table saying the cluster has no
namespaces.

**Events.** The API server returns events in etcd key order and offers no sort,
so "newest first" has to be computed here — over a scan that is bounded, and that
says so when the bound was reached. And half the events on a current cluster
carry ``eventTime``/``series`` instead of ``lastTimestamp``, because that is what
``events.k8s.io`` writes; reading only the legacy field buries every one of them
at the bottom of the list with a blank age. Those are the events an operator is
looking for during an incident, so both spellings are asserted here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from kubernetes.client.rest import ApiException

from app.api.events import _field_selector
from tests.conftest import obj

NOW = datetime.now(timezone.utc)


def _ts(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Namespaces
# --------------------------------------------------------------------------- #

def _namespace(name="prod", phase="Active", labels=None, annotations=None, deletion=None):
    return obj(
        metadata=obj(
            name=name,
            labels=labels,
            annotations=annotations,
            creation_timestamp=NOW - timedelta(days=94),
            deletion_timestamp=deletion,
        ),
        status=obj(phase=phase),
    )


def _pod(namespace="prod", name="checkout-7d9-abc"):
    return obj(metadata=obj(name=name, namespace=namespace))


def _stub_namespaces(fake, namespaces, pods=None, pod_error=None):
    fake.core_v1.returns("list_namespace", obj(items=namespaces))
    if pod_error is not None:
        fake.core_v1.raises("list_pod_for_all_namespaces", pod_error)
    else:
        fake.core_v1.returns("list_pod_for_all_namespaces", obj(items=pods or []))


def test_namespaces_come_back_shaped_and_sorted(client, fake_k8s):
    _stub_namespaces(
        fake_k8s,
        [_namespace("prod", labels={"env": "prod"}, annotations={"owner": "payments"}),
         _namespace("default"), _namespace("kube-system")],
        pods=[_pod("prod"), _pod("prod", "checkout-7d9-def"), _pod("kube-system")],
    )

    body = client.get("/api/namespaces").json()

    assert [row["name"] for row in body["items"]] == ["default", "kube-system", "prod"]
    prod = body["items"][2]
    assert prod["status"] == "Active"
    assert prod["labels"] == {"env": "prod"}
    assert prod["annotations"] == {"owner": "payments"}
    assert prod["pod_count"] == 2
    assert prod["age_seconds"] > 0
    assert prod["creationTimestamp"].endswith("Z")
    assert body["partial"] is False
    assert body["unavailable"] == []


def test_an_empty_namespace_reports_zero_pods(client, fake_k8s):
    """A real zero. Flattening it to null would be the same lie pointing the
    other way, and would put an em dash on every idle namespace."""
    _stub_namespaces(fake_k8s, [_namespace("staging")], pods=[])

    body = client.get("/api/namespaces").json()

    assert body["items"][0]["pod_count"] == 0
    assert body["partial"] is False


def test_pod_count_is_null_and_the_reason_is_named_when_pods_cannot_be_listed(
    client, fake_k8s
):
    """The assertion this endpoint exists for.

    A namespace showing 0 pods is the one an operator deletes during a cleanup.
    "We were refused the pod listing" must never render as "there is nothing in
    here".
    """
    _stub_namespaces(
        fake_k8s, [_namespace("prod"), _namespace("staging")],
        pod_error=ApiException(status=403, reason="Forbidden"),
    )

    response = client.get("/api/namespaces")
    body = response.json()

    assert response.status_code == 200
    assert [row["pod_count"] for row in body["items"]] == [None, None]
    assert body["partial"] is True
    assert body["unavailable"] == [
        {"group": "", "resource": "pods", "namespace": None,
         "reason": "forbidden", "detail": "Forbidden"},
    ]


def test_the_namespaces_themselves_being_refused_is_an_error_not_an_empty_list(
    client, fake_k8s
):
    """The primary read. An empty page here would say the cluster has no
    namespaces, which is a sentence no cluster can produce."""
    fake_k8s.core_v1.raises("list_namespace", ApiException(status=403, reason="Forbidden"))

    response = client.get("/api/namespaces")

    assert response.status_code == 403
    assert response.json()["error"] == "rbac_denied"
    assert response.json()["hint"]


def test_a_namespace_being_deleted_is_not_reported_as_active(client, fake_k8s):
    """The phase and the deletionTimestamp are written by different actors and
    there is a window where they disagree. Reporting Active is the row somebody
    deploys into, and then spends an afternoon wondering where it went."""
    _stub_namespaces(
        fake_k8s, [_namespace("doomed", phase="Active", deletion=NOW)], pods=[],
    )

    assert client.get("/api/namespaces").json()["items"][0]["status"] == "Terminating"


def test_missing_labels_and_annotations_render_as_objects_not_null(client, fake_k8s):
    """So the frontend can call Object.entries on them without a guard."""
    _stub_namespaces(fake_k8s, [_namespace("bare")], pods=[])

    row = client.get("/api/namespaces").json()["items"][0]

    assert row["labels"] == {}
    assert row["annotations"] == {}


def test_an_unreachable_cluster_degrades_the_pod_column_as_unreachable(client, fake_k8s):
    _stub_namespaces(
        fake_k8s, [_namespace("prod")],
        pod_error=ApiException(status=0, reason="SSLError: certificate verify failed"),
    )

    body = client.get("/api/namespaces").json()

    assert body["unavailable"][0]["reason"] == "unreachable"
    assert body["items"][0]["pod_count"] is None


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

def _legacy_event(
    *, reason="BackOff", minutes_ago=5, count=7, type_="Warning",
    name="checkout-7d9-abc", namespace="prod",
):
    """An event as the pre-1.19 recorder writes it: firstTimestamp/lastTimestamp."""
    return obj(
        metadata=obj(name=f"{name}.17a", namespace=namespace,
                     creation_timestamp=NOW - timedelta(minutes=minutes_ago)),
        type=type_,
        reason=reason,
        message="Back-off restarting failed container",
        count=count,
        first_timestamp=_ts(minutes_ago + 30),
        last_timestamp=_ts(minutes_ago),
        event_time=None,
        series=None,
        involved_object=obj(kind="Pod", name=name, namespace=namespace, uid="0f2a"),
        source=obj(component="kubelet", host="ip-10-0-1-4"),
        reporting_component=None,
        reporting_instance=None,
    )


def _modern_event(
    *, reason="Scheduled", minutes_ago=1, series_count=None, type_="Normal",
    name="checkout-7d9-def", namespace="prod",
):
    """An event as `events.k8s.io` writes it: eventTime, series, reporting*.

    The legacy fields are left null, which is exactly what makes reading only
    `lastTimestamp` bury these rows.
    """
    return obj(
        metadata=obj(name=f"{name}.17b", namespace=namespace,
                     creation_timestamp=NOW - timedelta(minutes=minutes_ago)),
        type=type_,
        reason=reason,
        message="Successfully assigned prod/checkout to ip-10-0-1-4",
        count=None,
        first_timestamp=None,
        last_timestamp=None,
        event_time=_ts(minutes_ago + (2 if series_count else 0)),
        series=(
            obj(count=series_count, last_observed_time=_ts(minutes_ago))
            if series_count else None
        ),
        involved_object=obj(kind="Pod", name=name, namespace=namespace, uid="0f2b"),
        source=obj(component=None, host=None),
        reporting_component="default-scheduler",
        reporting_instance="default-scheduler-ip-10-0-1-9",
    )


def _page(items, cont=None, remaining=None):
    return obj(
        items=items,
        metadata={"continue": cont, "remainingItemCount": remaining},
    )


def _stub_events(fake, pages, namespaced=False):
    """Serve `pages` in order to successive calls, raising if asked for more."""
    method = "list_namespaced_event" if namespaced else "list_event_for_all_namespaces"
    served = {"n": 0}

    def serve(*args, **kwargs):
        index = served["n"]
        served["n"] += 1
        if index >= len(pages):
            raise AssertionError(f"{method} was called more times than the test stubbed")
        page = pages[index]
        if isinstance(page, BaseException):
            raise page
        return page

    fake.core_v1.returns(method, serve)
    return served


def test_events_come_back_newest_last_seen_first(client, fake_k8s):
    _stub_events(fake_k8s, [_page([
        _legacy_event(reason="Old", minutes_ago=120),
        _legacy_event(reason="Newest", minutes_ago=1),
        _legacy_event(reason="Middle", minutes_ago=30),
    ])])

    body = client.get("/api/events").json()

    assert [row["reason"] for row in body["items"]] == ["Newest", "Middle", "Old"]
    assert body["partial"] is False


def test_a_modern_event_sorts_with_the_rest_instead_of_at_the_bottom(client, fake_k8s):
    """`events.k8s.io` leaves `lastTimestamp` null and writes `eventTime`.

    Sorting on the legacy field alone puts every event from a current controller
    below every event from an old one, with a blank age column — and the new ones
    are the ones somebody is on this page to read.
    """
    _stub_events(fake_k8s, [_page([
        _legacy_event(reason="LegacyOld", minutes_ago=90),
        _modern_event(reason="ModernNewest", minutes_ago=1),
        _legacy_event(reason="LegacyRecent", minutes_ago=10),
    ])])

    body = client.get("/api/events").json()

    assert [row["reason"] for row in body["items"]] == [
        "ModernNewest", "LegacyRecent", "LegacyOld",
    ]
    assert body["items"][0]["last_seen"] is not None


def test_a_repeating_modern_event_is_dated_by_its_series(client, fake_k8s):
    """`series.lastObservedTime` is the newest observation; `eventTime` is the
    first. Dating the row by the first would show a still-firing event as old."""
    _stub_events(fake_k8s, [_page([_modern_event(minutes_ago=2, series_count=14)])])

    row = client.get("/api/events").json()["items"][0]

    assert row["count"] == 14
    assert row["last_seen"] == _ts(2)
    assert row["first_seen"] == _ts(4)


def test_a_single_modern_event_counts_as_one_rather_than_as_unknown(client, fake_k8s):
    """`series` is set precisely when an event repeats, so its absence alongside
    an eventTime *is* the API saying "once". A null there would put an em dash on
    nearly every row of a modern cluster."""
    _stub_events(fake_k8s, [_page([_modern_event(minutes_ago=1)])])

    row = client.get("/api/events").json()["items"][0]

    assert row["count"] == 1


def test_a_legacy_event_reports_its_own_count(client, fake_k8s):
    _stub_events(fake_k8s, [_page([_legacy_event(count=7)])])

    assert client.get("/api/events").json()["items"][0]["count"] == 7


def test_the_source_falls_back_to_the_reporting_fields(client, fake_k8s):
    """`events.k8s.io` replaced `source` with reportingComponent/reportingInstance
    and leaves the old pair empty. Reading only the first spelling gives a blank
    Source column that reads as "nobody reported this"."""
    _stub_events(fake_k8s, [_page([_modern_event()])])

    row = client.get("/api/events").json()["items"][0]

    assert row["source"] == {
        "component": "default-scheduler",
        "host": "default-scheduler-ip-10-0-1-9",
    }


def test_a_legacy_event_keeps_its_own_source(client, fake_k8s):
    _stub_events(fake_k8s, [_page([_legacy_event()])])

    assert client.get("/api/events").json()["items"][0]["source"] == {
        "component": "kubelet", "host": "ip-10-0-1-4",
    }


def test_the_row_carries_the_object_the_event_is_about(client, fake_k8s):
    _stub_events(fake_k8s, [_page([_legacy_event()])])

    row = client.get("/api/events").json()["items"][0]

    assert row["involved"] == {
        "kind": "Pod", "name": "checkout-7d9-abc", "namespace": "prod", "uid": "0f2a",
    }
    assert row["namespace"] == "prod"
    assert row["type"] == "Warning"


# --------------------------------------------------------------------------- #
# Events — filters
# --------------------------------------------------------------------------- #

def test_the_filters_become_one_server_side_field_selector(client, fake_k8s):
    """Filtering after a bounded scan would make "the 200 newest Warnings" mean
    "the Warnings among the 200 newest events" — a different question, usually
    answered with far fewer rows than exist."""
    _stub_events(fake_k8s, [_page([])])

    client.get("/api/events?involvedObjectKind=Pod&involvedObjectName=checkout&type=Warning")

    _, kwargs = fake_k8s.core_v1.called("list_event_for_all_namespaces")[0]
    assert kwargs["field_selector"] == (
        "involvedObject.kind=Pod,involvedObject.name=checkout,type=Warning"
    )


def test_no_filters_means_no_field_selector(client, fake_k8s):
    _stub_events(fake_k8s, [_page([])])

    client.get("/api/events")

    _, kwargs = fake_k8s.core_v1.called("list_event_for_all_namespaces")[0]
    assert kwargs["field_selector"] is None


def test_a_namespace_filter_uses_the_namespaced_listing(client, fake_k8s):
    _stub_events(fake_k8s, [_page([_legacy_event()])], namespaced=True)

    body = client.get("/api/events?namespace=prod").json()

    args, _ = fake_k8s.core_v1.called("list_namespaced_event")[0]
    assert args == ("prod",)
    assert len(body["items"]) == 1


def test_an_unknown_event_type_is_rejected_rather_than_ignored(client, fake_k8s):
    """A dropped filter returns every event under a heading saying it is filtered."""
    assert client.get("/api/events?type=Critical").status_code == 422


def test_field_selector_values_are_escaped():
    """Object names cannot contain `,` or `=`, which is exactly why this is easy
    to skip — and why it would sit there until a value arrived from somewhere
    less disciplined than the API server and turned one filter into two."""
    selector = _field_selector(
        involved_kind=None, involved_name="odd,name=x", event_type=None,
    )

    assert selector == "involvedObject.name=odd\\,name\\=x"


# --------------------------------------------------------------------------- #
# Events — bounds and failures
# --------------------------------------------------------------------------- #

def test_the_limit_keeps_the_newest_after_sorting_not_the_first_page_of_etcd(
    client, fake_k8s
):
    _stub_events(fake_k8s, [_page([
        _legacy_event(reason=f"E{minutes}", minutes_ago=minutes)
        for minutes in (50, 5, 30, 1, 90)
    ])])

    body = client.get("/api/events?limit=2").json()

    assert [row["reason"] for row in body["items"]] == ["E1", "E5"]
    # Truncating a locally sorted list is not a partial read: everything in the
    # window was compared, and every row dropped is older than every row kept.
    assert body["partial"] is False


def test_a_scan_that_hits_its_budget_says_so(client, fake_k8s):
    """Otherwise the page presents the newest of an arbitrary subset as the
    newest overall — the API server does not sort, so the true newest event may
    simply not have been fetched."""
    fake_k8s.core_v1.returns(
        "list_event_for_all_namespaces",
        lambda *a, **kw: _page([_legacy_event()], cont="more", remaining=900),
    )

    body = client.get("/api/events").json()

    assert body["partial"] is True
    assert body["unavailable"][0]["reason"] == "timeout"
    assert "newest first" in body["unavailable"][0]["detail"]
    assert body["remaining"] == 900
    assert len(fake_k8s.core_v1.called("list_event_for_all_namespaces")) == 10


def test_a_complete_scan_reports_no_continue_and_no_remaining(client, fake_k8s):
    _stub_events(fake_k8s, [
        _page([_legacy_event(minutes_ago=9)], cont="tok", remaining=1),
        _page([_legacy_event(minutes_ago=1)]),
    ])

    body = client.get("/api/events").json()

    assert len(body["items"]) == 2
    assert body["continue"] is None
    assert body["remaining"] is None
    assert body["partial"] is False


def test_the_first_page_failing_is_an_error_not_an_empty_page(client, fake_k8s):
    """An empty event list says "nothing has happened on this cluster" to an
    operator who is on this page because something did."""
    fake_k8s.core_v1.raises(
        "list_event_for_all_namespaces", ApiException(status=403, reason="Forbidden"),
    )

    response = client.get("/api/events")

    assert response.status_code == 403
    assert response.json()["error"] == "rbac_denied"


def test_a_later_page_failing_keeps_what_was_read_and_names_the_loss(
    client, fake_k8s
):
    """We hold real events, so returning them is right — but claiming the list is
    complete is not."""
    _stub_events(fake_k8s, [
        _page([_legacy_event(reason="Kept", minutes_ago=3)], cont="tok"),
        ApiException(status=504, reason="Gateway Timeout"),
    ])

    response = client.get("/api/events")
    body = response.json()

    assert response.status_code == 200
    assert [row["reason"] for row in body["items"]] == ["Kept"]
    assert body["partial"] is True
    assert body["unavailable"][0]["reason"] == "timeout"
    assert body["unavailable"][0]["resource"] == "events"


def test_a_cluster_with_no_events_is_an_honest_empty_list(client, fake_k8s):
    """`items: []` means the cluster has none — which is only trustworthy
    because every way of failing to look lands in `unavailable` instead."""
    _stub_events(fake_k8s, [_page([])])

    body = client.get("/api/events").json()

    assert body["items"] == []
    assert body["partial"] is False
    assert body["unavailable"] == []


def test_an_event_with_no_usable_timestamp_sorts_to_the_bottom(client, fake_k8s):
    """An unknown age must not be presented as the most recent thing that
    happened on a page whose entire purpose is recency."""
    undated = obj(
        metadata=obj(name="mystery.17c", namespace="prod", creation_timestamp=None),
        type="Normal", reason="Undated", message="", count=None,
        first_timestamp=None, last_timestamp=None, event_time=None, series=None,
        involved_object=obj(kind="Pod", name="mystery", namespace="prod", uid="x"),
        source=obj(component=None, host=None),
        reporting_component=None, reporting_instance=None,
    )
    _stub_events(fake_k8s, [_page([undated, _legacy_event(reason="Dated", minutes_ago=90)])])

    body = client.get("/api/events").json()

    assert [row["reason"] for row in body["items"]] == ["Dated", "Undated"]
    assert body["items"][1]["last_seen"] is None
    assert body["items"][1]["count"] is None
