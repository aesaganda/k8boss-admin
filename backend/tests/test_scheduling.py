"""
Why is this pod Pending (§31).

The most common question anybody asks a Kubernetes console, and every assertion
here is about a way of answering it that is wrong while looking right:

  scheduler vs kubelet   A Pending pod with `spec.nodeName` set has been placed.
                         Whatever is wrong is on that node, and sending the
                         operator to look at cluster capacity is sending them to
                         a different machine entirely. One phase in the API, two
                         investigations.

  quoted vs derived      The scheduler's own `FailedScheduling` message is the
                         authoritative verdict over the whole predicate chain,
                         plugins included. It is surfaced verbatim, with the age
                         of the attempt that produced it — because it is a
                         snapshot, and a node added since does not rewrite it.

  absent vs innocent     Events age out of etcd within the hour. `scheduler:
                         null` means no such event is readable now, never that
                         the pod has not been rejected — the second reading sends
                         somebody looking for a problem the scheduler already
                         named and forgot.

  ruled out vs fits      Every per-node verdict is a rule-out or a shrug. The
                         scheduler weighs affinity, topology spread, volume zone
                         and every plugin the cluster runs; none of that is
                         evaluated here. Promoting `no_reason_found` to "this
                         node has room" would send an operator to argue with the
                         scheduler about a node it already rejected.

  cause vs symptom       An unbound `WaitForFirstConsumer` claim is unbound
                         *because* the pod is unscheduled. Reporting it as the
                         blocker sends somebody to fix storage while storage
                         waits on them to fix scheduling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.services import scheduling
from tests.conftest import obj

NAMESPACE = "prod"
POD = "checkout-7d9-abc"
POD_PATH = f"/api/v1/namespaces/{NAMESPACE}/pods/{POD}"
NOW = datetime.now(timezone.utc)


def _ts(minutes_ago):
    return NOW - timedelta(minutes=minutes_ago)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def live_pod(
    *,
    phase="Pending",
    node=None,
    requests=None,
    tolerations=(),
    node_selector=None,
    volumes=(),
    conditions=None,
    created_minutes_ago=20,
):
    """The pod as the API server hands it back — a plain dict, because
    `read_object` reaches past the typed clients."""
    container = {"name": "app", "image": "ghcr.io/acme/checkout:1.9.2"}
    if requests is not None:
        container["resources"] = {"requests": dict(requests)}
    spec = {
        "containers": [container],
        "tolerations": [dict(t) for t in tolerations],
        "volumes": [dict(v) for v in volumes],
    }
    if node:
        spec["nodeName"] = node
    if node_selector:
        spec["nodeSelector"] = dict(node_selector)
    status = {"phase": phase}
    if conditions is not None:
        status["conditions"] = [dict(c) for c in conditions]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": POD,
            "namespace": NAMESPACE,
            "creationTimestamp": _ts(created_minutes_ago).isoformat().replace("+00:00", "Z"),
        },
        "spec": spec,
        "status": status,
    }


def node(
    name="ip-10-0-1-4",
    *,
    cpu="4",
    memory="8Gi",
    pods="110",
    taints=(),
    labels=None,
    unschedulable=False,
    ready=True,
):
    conditions = []
    if ready is not None:
        conditions.append(obj(type="Ready", status="True" if ready else "False"))
    return obj(
        metadata=obj(
            name=name,
            labels=dict(labels) if labels is not None else {"kubernetes.io/os": "linux"},
        ),
        spec=obj(
            unschedulable=unschedulable or None,
            taints=[obj(**t) for t in taints],
        ),
        status=obj(
            allocatable={"cpu": cpu, "memory": memory, "pods": pods},
            capacity={"cpu": cpu, "memory": memory, "pods": pods},
            conditions=conditions,
        ),
    )


def scheduled_pod(node_name, *, cpu="1", memory="1Gi"):
    """A pod already on a node, so it counts against that node's allocatable."""
    return obj(
        metadata=obj(name="other", namespace=NAMESPACE),
        spec=obj(
            node_name=node_name,
            containers=[obj(resources=obj(requests={"cpu": cpu, "memory": memory}))],
            init_containers=[],
            overhead=None,
        ),
        status=obj(phase="Running"),
    )


def failed_scheduling_event(*, minutes_ago=2, count=14, message=None):
    return obj(
        metadata=obj(name=f"{POD}.17b", namespace=NAMESPACE,
                     creation_timestamp=_ts(minutes_ago)),
        type="Warning",
        reason="FailedScheduling",
        message=message or (
            "0/3 nodes are available: 2 Insufficient cpu, "
            "1 node(s) had untolerated taint {gpu: true}."
        ),
        count=count,
        first_timestamp=_ts(minutes_ago + 10),
        last_timestamp=_ts(minutes_ago),
        event_time=None,
        series=None,
        involved_object=obj(kind="Pod", name=POD, namespace=NAMESPACE, uid="0f2b"),
        source=obj(component="default-scheduler", host=None),
        reporting_component="default-scheduler",
        reporting_instance="default-scheduler-ip-10-0-1-9",
    )


def other_event(reason="Pulling", minutes_ago=1):
    return obj(
        metadata=obj(name=f"{POD}.17c", namespace=NAMESPACE,
                     creation_timestamp=_ts(minutes_ago)),
        type="Normal",
        reason=reason,
        message="Pulling image",
        count=1,
        first_timestamp=_ts(minutes_ago),
        last_timestamp=_ts(minutes_ago),
        event_time=None,
        series=None,
        involved_object=obj(kind="Pod", name=POD, namespace=NAMESPACE, uid="0f2b"),
        source=obj(component="kubelet", host="ip-10-0-1-4"),
        reporting_component=None,
        reporting_instance=None,
    )


def claim(name="data", *, phase="Bound", storage_class="gp3"):
    return obj(
        metadata=obj(name=name, namespace=NAMESPACE),
        spec=obj(storage_class_name=storage_class),
        status=obj(phase=phase),
    )


def claim_volume(claim_name="data"):
    return {"name": "data", "persistentVolumeClaim": {"claimName": claim_name}}


def storage_class(name="gp3", *, mode="Immediate"):
    return obj(metadata=obj(name=name), volume_binding_mode=mode)


# --------------------------------------------------------------------------- #
# The fake API server
# --------------------------------------------------------------------------- #

class FakeApiServer:
    """`ApiClient.call_api` answering the pod read only. Anything else raises,
    which is how a test that expects a read this module does not make fails."""

    def __init__(self, pod=None):
        self.pod = live_pod() if pod is None else pod
        self.requests: list[SimpleNamespace] = []
        self.raises: dict[str, BaseException] = {}

    def __call__(self, path, method, **kwargs):
        self.requests.append(SimpleNamespace(path=path, method=method))
        if path in self.raises:
            raise self.raises[path]
        if path != POD_PATH:
            raise AssertionError(f"unexpected {method} {path}")
        if kwargs.get("_return_http_data_only"):
            return self.pod
        return self.pod, 200, {}


@pytest.fixture
def cluster(fake_k8s):
    """A cluster serving one pod, three nodes, no events and no claims.

    Every listing is stubbed rather than left to the fake's raise-on-unstubbed
    edge, because "this endpoint made a read it should not have" and "this
    endpoint did not make a read it should have" are different failures and the
    tests below assert both.
    """
    server = FakeApiServer()
    fake_k8s.api_client.returns("call_api", server)
    fake_k8s.core_v1.returns("list_node", obj(items=[node()]))
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[]))
    fake_k8s.core_v1.returns("list_namespaced_event", obj(
        items=[], metadata={"continue": None, "remainingItemCount": None},
    ))
    fake_k8s.core_v1.returns("list_namespaced_persistent_volume_claim", obj(items=[]))
    fake_k8s.storage_v1.returns("list_storage_class", obj(items=[]))
    server.fake = fake_k8s
    return server


def verdict(report, name):
    return next(row for row in report["nodes"] if row["name"] == name)


def codes(row):
    return [reason["code"] for reason in row["reasons"]]


# --------------------------------------------------------------------------- #
# Which question is this — the scheduler's or the kubelet's
# --------------------------------------------------------------------------- #

def test_a_pending_pod_with_no_node_is_waiting_on_the_scheduler():
    assert scheduling.waiting_on(live_pod()) == scheduling.WAITING_ON_SCHEDULER


def test_a_pending_pod_that_already_has_a_node_is_waiting_on_the_kubelet():
    """The split this endpoint exists to make. It has been *placed*; the problem
    is an image, a volume or an init container on that machine, and cluster
    capacity has nothing to do with it."""
    assert scheduling.waiting_on(live_pod(node="ip-10-0-1-4")) == scheduling.WAITING_ON_KUBELET


def test_a_running_pod_is_waiting_on_nothing():
    assert scheduling.waiting_on(live_pod(phase="Running", node="ip-10-0-1-4")) == (
        scheduling.WAITING_ON_NOTHING
    )


def test_the_endpoint_reports_which_of_the_two_it_is(cluster):
    cluster.pod = live_pod(node="ip-10-0-1-4")

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["waiting_on"] == scheduling.WAITING_ON_KUBELET
    assert report["node"] == "ip-10-0-1-4"


def test_a_settled_pod_is_not_given_a_pending_duration(cluster):
    """`pending_seconds` on a Running pod would be its age dressed up as a
    complaint."""
    cluster.pod = live_pod(phase="Running", node="ip-10-0-1-4")

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["pending_seconds"] is None


# --------------------------------------------------------------------------- #
# The scheduler's own answer
# --------------------------------------------------------------------------- #

def test_the_scheduler_message_is_quoted_verbatim_with_its_age(cluster):
    """The authoritative verdict over the whole predicate chain, plugins
    included. Nothing here paraphrases it."""
    cluster.fake.core_v1.returns("list_namespaced_event", obj(
        items=[failed_scheduling_event(minutes_ago=3, count=14)],
        metadata={"continue": None, "remainingItemCount": None},
    ))

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["scheduler"]["message"].startswith("0/3 nodes are available")
    assert report["scheduler"]["count"] == 14
    # The age is what makes the message usable: it is a snapshot of the moment
    # of that attempt, and a node added since does not rewrite it.
    assert 150 <= report["scheduler"]["age_seconds"] <= 250


def test_an_undatable_scheduler_message_has_a_null_age_not_a_fresh_one(cluster):
    """`0` would read as *just now*, which is the reassuring direction and the
    wrong one: the whole point of the age is that a large number means the
    message may no longer describe the cluster.

    The clamping and the null both come from `shaping.age_seconds`, which every
    other age in this codebase already uses. A second implementation here would
    be a second chance for one of them to start rendering an underivable age as
    a number.
    """
    event = failed_scheduling_event()
    event.last_timestamp = None
    event.first_timestamp = None
    event.event_time = None
    event.metadata.creation_timestamp = None
    cluster.fake.core_v1.returns("list_namespaced_event", obj(
        items=[event], metadata={"continue": None, "remainingItemCount": None},
    ))

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["scheduler"]["age_seconds"] is None


def test_an_event_that_is_not_the_scheduler_s_is_not_read_as_one(cluster):
    """A `Pulling` event on a Pending pod belongs to the kubelet. Reporting it
    under `scheduler` would attribute an image pull to the scheduler."""
    cluster.fake.core_v1.returns("list_namespaced_event", obj(
        items=[other_event()],
        metadata={"continue": None, "remainingItemCount": None},
    ))

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["scheduler"] is None


def test_no_readable_event_is_null_and_not_a_clean_bill(cluster):
    """Events age out of etcd within the hour, so a pod pending since this
    morning has an explanation that expired. `null` says the event is not
    readable; it never says the scheduler is content."""
    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["scheduler"] is None
    # And the pod is still reported as waiting on the scheduler, so the null is
    # not the same shape as "there is no scheduling problem".
    assert report["waiting_on"] == scheduling.WAITING_ON_SCHEDULER
    assert report["pending_seconds"] >= 1000


def test_a_refused_event_listing_names_itself_rather_than_reading_as_no_event(cluster):
    cluster.fake.core_v1.raises(
        "list_namespaced_event", ApiException(status=403, reason="Forbidden"),
    )

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["scheduler"] is None
    assert report["partial"] is True
    assert "events" in [entry["resource"] for entry in report["unavailable"]]


def test_a_truncated_event_scan_travels_into_unavailable(cluster):
    """`list_events` degrades rather than raising, so its own `unavailable`
    entries have to be carried across — a truncated scan is exactly the case
    where "no FailedScheduling event" must not be read as "not rejected"."""
    cluster.fake.core_v1.returns("list_namespaced_event", obj(
        items=[], metadata={"continue": "next-page", "remainingItemCount": 400},
    ))

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["partial"] is True


def test_the_scheduler_is_not_consulted_about_a_settled_pod(cluster):
    """A Running pod's scheduling is decided. Fetching its events to look for a
    rejection would spend a round trip answering a question nobody asked."""
    cluster.pod = live_pod(phase="Running", node="ip-10-0-1-4")

    scheduling.why_pending(NAMESPACE, POD)

    assert cluster.fake.core_v1.called("list_namespaced_event") == []


# --------------------------------------------------------------------------- #
# The PodScheduled condition
# --------------------------------------------------------------------------- #

def test_an_absent_condition_is_none_rather_than_a_manufactured_refusal():
    """The API server writes no conditions until something has evaluated the
    pod. Synthesising "not scheduled" would attribute a verdict to a scheduler
    that has not looked."""
    assert scheduling.scheduled_condition(live_pod()) is None


def test_the_condition_is_returned_verbatim():
    pod = live_pod(conditions=[{
        "type": "PodScheduled", "status": "False", "reason": "Unschedulable",
        "message": "0/3 nodes are available.",
        "lastTransitionTime": "2026-09-07T10:00:00Z",
    }])

    condition = scheduling.scheduled_condition(pod)

    assert condition["status"] == "False"
    assert condition["reason"] == "Unschedulable"


# --------------------------------------------------------------------------- #
# Per-node rule-outs — and the verdict that does not exist
# --------------------------------------------------------------------------- #

def test_a_node_with_nothing_against_it_is_not_reported_as_fitting(cluster):
    """The whole design. The scheduler weighs affinity, topology spread, volume
    zone, extended resources and every plugin the cluster runs — none of which
    is evaluated here. "Nothing ruled this out" is the strongest honest
    statement, and it is not "this node has room"."""
    report = scheduling.why_pending(NAMESPACE, POD)
    row = verdict(report, "ip-10-0-1-4")

    assert row["verdict"] == scheduling.NO_REASON_FOUND
    assert row["reasons"] == []
    # There is no third verdict, and there must not become one: the vocabulary
    # itself is the promise. A `fits` added later would be a claim this module
    # cannot support about any node.
    assert set(r["verdict"] for r in report["nodes"]) <= {
        scheduling.RULED_OUT, scheduling.NO_REASON_FOUND,
    }


def test_a_cordoned_node_is_ruled_out(cluster):
    cluster.fake.core_v1.returns("list_node", obj(items=[node(unschedulable=True)]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_CORDONED in codes(row)


def test_a_not_ready_node_is_ruled_out(cluster):
    cluster.fake.core_v1.returns("list_node", obj(items=[node(ready=False)]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_NOT_READY in codes(row)


def test_a_node_whose_readiness_is_unobserved_is_not_ruled_out(cluster):
    """Tri-state, and only `False` excludes. A node with no Ready condition has
    not been observed rather than been found unhealthy, and ruling it out would
    invent a rejection."""
    cluster.fake.core_v1.returns("list_node", obj(items=[node(ready=None)]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_NOT_READY not in codes(row)


def test_an_untolerated_taint_rules_the_node_out(cluster):
    cluster.fake.core_v1.returns("list_node", obj(items=[
        node(taints=[{"key": "gpu", "value": "true", "effect": "NoSchedule"}]),
    ]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_TAINTED in codes(row)
    assert "gpu=true:NoSchedule" in row["reasons"][0]["detail"]


def test_a_tolerated_taint_does_not(cluster):
    cluster.pod = live_pod(tolerations=[
        {"key": "gpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"},
    ])
    cluster.fake.core_v1.returns("list_node", obj(items=[
        node(taints=[{"key": "gpu", "value": "true", "effect": "NoSchedule"}]),
    ]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert row["verdict"] == scheduling.NO_REASON_FOUND


def test_prefer_no_schedule_is_not_a_rule_out(cluster):
    """It lowers the node's score and does not exclude it. Listing it as a
    rule-out would report a node as unavailable that the scheduler will happily
    use when nothing better exists."""
    cluster.fake.core_v1.returns("list_node", obj(items=[
        node(taints=[{"key": "spot", "value": "true", "effect": "PreferNoSchedule"}]),
    ]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert row["verdict"] == scheduling.NO_REASON_FOUND


def test_a_node_selector_the_node_does_not_carry_rules_it_out(cluster):
    cluster.pod = live_pod(node_selector={"disktype": "ssd"})

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_SELECTOR_MISMATCH in codes(row)
    assert "disktype=ssd" in row["reasons"][0]["detail"]


def test_a_matching_node_selector_does_not(cluster):
    cluster.pod = live_pod(node_selector={"kubernetes.io/os": "linux"})

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_SELECTOR_MISMATCH not in codes(row)


def test_a_request_larger_than_what_is_left_rules_the_node_out(cluster):
    cluster.pod = live_pod(requests={"cpu": "3", "memory": "1Gi"})
    cluster.fake.core_v1.returns("list_pod_for_all_namespaces", obj(
        items=[scheduled_pod("ip-10-0-1-4", cpu="2", memory="1Gi")],
    ))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_INSUFFICIENT_CPU in codes(row)
    assert row["capacity_checked"] is True


def test_allocatable_is_the_comparison_not_capacity(cluster):
    """Capacity is the machine; allocatable is what the kubelet offers the
    scheduler after its own reservations, and the scheduler only ever compares
    against the second."""
    cluster.pod = live_pod(requests={"cpu": "3500m"})
    cluster.fake.core_v1.returns("list_node", obj(items=[
        # A 4-core box that keeps half a core for itself.
        obj(
            metadata=obj(name="ip-10-0-1-4", labels={}),
            spec=obj(unschedulable=None, taints=[]),
            status=obj(
                capacity={"cpu": "4", "memory": "8Gi", "pods": "110"},
                allocatable={"cpu": "3500m", "memory": "7Gi", "pods": "110"},
                conditions=[obj(type="Ready", status="True")],
            ),
        ),
    ]))
    cluster.fake.core_v1.returns("list_pod_for_all_namespaces", obj(
        items=[scheduled_pod("ip-10-0-1-4", cpu="500m", memory="1Gi")],
    ))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_INSUFFICIENT_CPU in codes(row)


def test_a_full_pod_slot_count_rules_the_node_out_whatever_the_request(cluster):
    cluster.fake.core_v1.returns("list_node", obj(items=[node(pods="1")]))
    cluster.fake.core_v1.returns("list_pod_for_all_namespaces", obj(
        items=[scheduled_pod("ip-10-0-1-4", cpu="1m", memory="1Mi")],
    ))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_POD_SLOTS_FULL in codes(row)


def test_a_node_that_does_not_publish_a_pod_slot_count_is_only_half_examined(cluster):
    """Every node has a pod-slot limit and every scheduler enforces it, so a node
    that does not publish one has not been fully compared — whatever else it
    reported. Leaving `capacity_checked` true there would say this console
    checked a limit it never saw."""
    cluster.fake.core_v1.returns("list_node", obj(items=[
        obj(
            metadata=obj(name="ip-10-0-1-4", labels={}),
            spec=obj(unschedulable=None, taints=[]),
            status=obj(
                allocatable={"cpu": "4", "memory": "8Gi"},
                capacity={"cpu": "4", "memory": "8Gi"},
                conditions=[obj(type="Ready", status="True")],
            ),
        ),
    ]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert row["verdict"] == scheduling.NO_REASON_FOUND
    assert row["capacity_checked"] is False


def test_an_unparseable_request_on_a_neighbour_withholds_the_capacity_verdict(cluster):
    """The pod listing answered; one pod on the node has a request this console
    cannot read, so the node's requested total is unknown. Treating that as zero
    would report a full node as empty — the direction that sends somebody to
    schedule onto it — so no capacity verdict is reached for that dimension at
    all.
    """
    cluster.pod = live_pod(requests={"cpu": "3"})
    cluster.fake.core_v1.returns("list_pod_for_all_namespaces", obj(items=[
        obj(
            metadata=obj(name="odd", namespace=NAMESPACE),
            spec=obj(
                node_name="ip-10-0-1-4",
                containers=[obj(resources=obj(requests={"cpu": "three-ish"}))],
                init_containers=[],
                overhead=None,
            ),
            status=obj(phase="Running"),
        ),
    ]))

    row = verdict(scheduling.why_pending(NAMESPACE, POD), "ip-10-0-1-4")

    assert scheduling.NODE_INSUFFICIENT_CPU not in codes(row)
    assert row["verdict"] == scheduling.NO_REASON_FOUND
    # And the shrug is the weaker one. `capacity_checked` false is what stops
    # this node looking as examined as one whose totals were all readable —
    # which is the reading that would send somebody to schedule onto it.
    assert row["capacity_checked"] is False


def test_an_unreadable_pod_listing_withholds_the_capacity_verdict(cluster):
    """Nothing is known about what the node already holds, so it is not judged
    on room — and it is certainly not reported as having any. `capacity_checked`
    is how the response says which nodes were only half-examined."""
    cluster.fake.core_v1.raises(
        "list_pod_for_all_namespaces", ApiException(status=403, reason="Forbidden"),
    )

    report = scheduling.why_pending(NAMESPACE, POD)
    row = verdict(report, "ip-10-0-1-4")

    assert row["capacity_checked"] is False
    assert scheduling.NODE_INSUFFICIENT_CPU not in codes(row)
    assert report["partial"] is True


def test_an_unreadable_node_listing_is_null_and_never_an_empty_table(cluster):
    """`[]` would say the cluster has no nodes, on the screen where somebody is
    working out why nothing will take their pod."""
    cluster.fake.core_v1.raises("list_node", ApiException(status=403, reason="Forbidden"))

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["nodes"] is None
    assert report["partial"] is True


def test_the_pod_listing_is_not_fetched_when_there_are_no_nodes_to_attribute_it_to(cluster):
    cluster.fake.core_v1.raises("list_node", ApiException(status=403, reason="Forbidden"))

    scheduling.why_pending(NAMESPACE, POD)

    assert cluster.fake.core_v1.called("list_pod_for_all_namespaces") == []


def test_ruled_out_nodes_sort_before_the_shrugs(cluster):
    """The rows that say something come first. A table led by six nodes with no
    reasons buries the two that name the problem."""
    cluster.fake.core_v1.returns("list_node", obj(items=[
        node("a-clean"),
        node("b-cordoned", unschedulable=True),
    ]))

    report = scheduling.why_pending(NAMESPACE, POD)

    assert [row["name"] for row in report["nodes"]] == ["b-cordoned", "a-clean"]


# --------------------------------------------------------------------------- #
# Claims — cause or symptom
# --------------------------------------------------------------------------- #

def test_a_bound_claim_blocks_nothing(cluster):
    cluster.pod = live_pod(volumes=[claim_volume()])
    cluster.fake.core_v1.returns(
        "list_namespaced_persistent_volume_claim", obj(items=[claim(phase="Bound")]),
    )
    cluster.fake.storage_v1.returns("list_storage_class", obj(items=[storage_class()]))

    (row,) = scheduling.why_pending(NAMESPACE, POD)["claims"]

    assert row["blocks_scheduling"] is False


def test_an_unbound_immediate_claim_blocks_scheduling(cluster):
    cluster.pod = live_pod(volumes=[claim_volume()])
    cluster.fake.core_v1.returns(
        "list_namespaced_persistent_volume_claim", obj(items=[claim(phase="Pending")]),
    )
    cluster.fake.storage_v1.returns(
        "list_storage_class", obj(items=[storage_class(mode="Immediate")]),
    )

    (row,) = scheduling.why_pending(NAMESPACE, POD)["claims"]

    assert row["blocks_scheduling"] is True


def test_an_unbound_wait_for_first_consumer_claim_is_the_symptom_not_the_cause(cluster):
    """The inversion. The provisioner waits for the scheduler to pick a node so
    it can create the volume in the right zone — so this claim is unbound
    *because* the pod is unscheduled. Reporting it as the blocker sends somebody
    to fix storage while storage waits on them to fix scheduling."""
    cluster.pod = live_pod(volumes=[claim_volume()])
    cluster.fake.core_v1.returns(
        "list_namespaced_persistent_volume_claim", obj(items=[claim(phase="Pending")]),
    )
    cluster.fake.storage_v1.returns(
        "list_storage_class", obj(items=[storage_class(mode="WaitForFirstConsumer")]),
    )

    (row,) = scheduling.why_pending(NAMESPACE, POD)["claims"]

    assert row["blocks_scheduling"] is False
    assert row["binding_mode"] == "WaitForFirstConsumer"


def test_an_unreadable_binding_mode_leaves_the_question_open(cluster):
    """Unbound, and the mode could not be established. It is either the cause or
    the symptom, and guessing sends the operator to one of two systems with even
    odds."""
    cluster.pod = live_pod(volumes=[claim_volume()])
    cluster.fake.core_v1.returns(
        "list_namespaced_persistent_volume_claim", obj(items=[claim(phase="Pending")]),
    )
    cluster.fake.storage_v1.raises(
        "list_storage_class", ApiException(status=403, reason="Forbidden"),
    )

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["claims"][0]["blocks_scheduling"] is None
    assert report["partial"] is True


def test_a_claim_that_does_not_exist_blocks_outright(cluster):
    """Not in doubt: the kubelet cannot mount what is not there."""
    cluster.pod = live_pod(volumes=[claim_volume("missing")])

    (row,) = scheduling.why_pending(NAMESPACE, POD)["claims"]

    assert row["exists"] is False
    assert row["blocks_scheduling"] is True


def test_an_unreadable_claim_listing_is_null_and_never_an_empty_list(cluster):
    """This pod may mount four claims. `[]` would say it mounts none, on the
    screen where somebody is deciding whether storage is the problem."""
    cluster.pod = live_pod(volumes=[claim_volume()])
    cluster.fake.core_v1.raises(
        "list_namespaced_persistent_volume_claim",
        ApiException(status=403, reason="Forbidden"),
    )

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["claims"] is None
    assert report["partial"] is True


def test_a_pod_with_no_claims_reads_nothing_and_reports_an_empty_list(cluster):
    """A real empty: the pod mounts no claims. No listing is made for it, and
    the fake raises on an unstubbed call, so this asserts both."""
    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["claims"] == []
    assert cluster.fake.core_v1.called("list_namespaced_persistent_volume_claim") == []


# --------------------------------------------------------------------------- #
# The pod itself
# --------------------------------------------------------------------------- #

def test_an_unreadable_pod_raises_rather_than_rendering_an_empty_answer(cluster):
    """There is no honest answer to "why is this pod pending" about a pod that
    could not be read, and a page with a name at the top of it looks like one."""
    from app.errors import NotFound

    cluster.raises[POD_PATH] = ApiException(status=404, reason="Not Found")

    with pytest.raises(NotFound):
        scheduling.why_pending(NAMESPACE, POD)


def test_the_pod_s_own_request_is_reported_so_the_arithmetic_is_checkable(cluster):
    cluster.pod = live_pod(requests={"cpu": "250m", "memory": "256Mi"})

    report = scheduling.why_pending(NAMESPACE, POD)

    assert report["requests"]["cpu_cores"] == pytest.approx(0.25)
    assert report["requests"]["memory_bytes"] == 256 * 1024 * 1024
