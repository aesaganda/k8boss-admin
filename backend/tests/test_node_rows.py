"""
Node rows (§5), and the one null that matters most.

``requested`` and ``pod_count`` are ``None`` — never ``0`` — when the pod listing
failed. That is the assertion this file exists for. A node reporting zero
requested cores and zero pods reads as idle, an idle node is the one an operator
picks to drain or terminate, and "we were refused the pod listing" and "nothing
is running here" must never render as the same row.

Everything else here is about not lying more quietly: readiness that is
``Unknown`` is not ``false``, a node with no role labels does not get an invented
``worker``, and a finished Job pod does not hold a pod slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from kubernetes.client.rest import ApiException

from app.errors import RBACDenied
from app.services.nodes import get_node, list_nodes, node_row, node_usage
from tests.conftest import obj

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _node(
    name="ip-10-0-1-4",
    labels=None,
    ready="True",
    unschedulable=None,
    taints=None,
    conditions=None,
):
    return obj(
        metadata=obj(
            name=name,
            labels={} if labels is None else labels,
            creation_timestamp=NOW - timedelta(days=94),
            resource_version="884213",
        ),
        spec=obj(unschedulable=unschedulable, taints=taints or []),
        status=obj(
            capacity={"cpu": "16", "memory": "64Gi", "pods": "110",
                      "ephemeral-storage": "104857600Ki"},
            allocatable={"cpu": "15800m", "memory": "62Gi", "pods": "110"},
            conditions=(
                conditions
                if conditions is not None
                else [
                    obj(type="MemoryPressure", status="False",
                        reason="KubeletHasSufficientMemory"),
                    obj(type="Ready", status=ready, reason="KubeletReady"),
                ]
            ),
            addresses=[
                obj(type="Hostname", address="ip-10-0-1-4.eu-west-1.compute.internal"),
                obj(type="InternalIP", address="10.0.1.4"),
                obj(type="ExternalIP", address="52.1.1.1"),
            ],
            node_info=obj(
                kubelet_version="v1.31.4",
                os_image="Amazon Linux 2023",
                container_runtime_version="containerd://1.7.13",
            ),
        ),
    )


def _pod(
    name="checkout-7d9-abc",
    namespace="prod",
    node="ip-10-0-1-4",
    phase="Running",
    cpu="500m",
    memory="1Gi",
    init=None,
    overhead=None,
):
    return obj(
        metadata=obj(name=name, namespace=namespace, labels={"app": "checkout"},
                     annotations={}, creation_timestamp=NOW - timedelta(hours=3),
                     owner_references=[]),
        spec=obj(
            node_name=node,
            containers=[obj(name="app", image="ghcr.io/acme/checkout:1.9.2",
                            resources=obj(requests={"cpu": cpu, "memory": memory}))],
            init_containers=init or [],
            overhead=overhead,
            volumes=[],
        ),
        status=obj(phase=phase, container_statuses=[], pod_ip="10.4.2.9",
                   qos_class="Burstable"),
    )


def _forbidden(message="pods is forbidden"):
    return ApiException(status=403, reason="Forbidden")


# --------------------------------------------------------------------------- #
# The null that matters
# --------------------------------------------------------------------------- #

def test_pod_listing_failure_nulls_requested_and_pod_count(fake_k8s):
    """The whole point of the module, asserted on the list endpoint.

    Zero here would read as an idle node. The node listing itself succeeded, so
    the page still renders — the gap is named in ``unavailable`` and every
    affected number is null.
    """
    fake_k8s.core_v1.returns("list_node", obj(items=[_node(), _node("ip-10-0-1-5")]))
    fake_k8s.core_v1.raises("list_pod_for_all_namespaces", _forbidden())

    result = list_nodes()

    assert len(result["items"]) == 2
    for row in result["items"]:
        assert row["requested"] is None
        assert row["pod_count"] is None
        # Capacity came off the node object and is unaffected: losing the pod
        # listing costs two columns, not the page.
        assert row["capacity"]["cpu_cores"] == 16.0

    assert result["partial"] is True
    assert [entry["reason"] for entry in result["unavailable"]] == ["forbidden"]
    assert result["unavailable"][0]["resource"] == "pods"


def test_an_empty_node_gets_real_zeroes(fake_k8s):
    """Zero is correct when the listing *succeeded* and found nothing.

    The counterpart to the test above: this is what makes the null meaningful.
    If an unreadable node and a genuinely empty one both showed ``0``, the null
    would be decoration.
    """
    fake_k8s.core_v1.returns("list_node", obj(items=[_node()]))
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[]))

    row = list_nodes()["items"][0]

    assert row["pod_count"] == 0
    assert row["requested"] == {"cpu_cores": 0.0, "memory_bytes": 0}
    assert list_nodes()["partial"] is False


def test_node_listing_failure_is_an_error_not_an_empty_table(fake_k8s):
    """The primary read failing means the endpoint failed.

    ``items: []`` with a footnote would be a cluster with no nodes, which is a
    statement about the cluster we are in no position to make. It raises, and the
    §1.3 envelope carries the grant that would fix it.
    """
    fake_k8s.core_v1.raises("list_node", _forbidden())

    with pytest.raises(RBACDenied) as raised:
        list_nodes()
    assert raised.value.context["resource"] == "nodes"
    assert raised.value.hint and "nodes" in raised.value.hint


def test_detail_pods_are_null_not_empty_when_unreadable(fake_k8s):
    """The single most dangerous empty list in the console.

    This is the page an operator reads immediately before draining. ``[]`` here
    is the sentence "nothing is running on this node", and it is the sentence
    that gets the button clicked.
    """
    fake_k8s.core_v1.returns("read_node", _node())
    fake_k8s.core_v1.raises("list_pod_for_all_namespaces", _forbidden())

    detail = get_node("ip-10-0-1-4")

    assert detail["pods"] is None
    assert detail["pod_count"] is None
    assert detail["requested"] is None
    assert detail["partial"] is True


def test_detail_returns_pod_rows(fake_k8s):
    fake_k8s.core_v1.returns("read_node", _node())
    fake_k8s.core_v1.returns(
        "list_pod_for_all_namespaces", obj(items=[_pod(), _pod("pg-0", "data")])
    )

    detail = get_node("ip-10-0-1-4")

    assert [pod["name"] for pod in detail["pods"]] == ["checkout-7d9-abc", "pg-0"]
    assert detail["pods"][0]["namespace"] == "prod"
    assert detail["pod_count"] == 2
    assert detail["partial"] is False


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #

def test_finished_pods_hold_neither_slots_nor_resources():
    """A node that has run ten thousand Jobs has not used ten thousand pod slots.

    The scheduler excludes Succeeded and Failed pods from both tallies. Counting
    them would make every long-lived node look wildly over-subscribed, and an
    over-subscribed node is one nobody schedules to.
    """
    usage = node_usage([
        _pod("live", phase="Running", cpu="1"),
        _pod("done", phase="Succeeded", cpu="8"),
        _pod("gone", phase="Failed", cpu="8"),
    ])
    assert usage["pod_count"] == 1
    assert usage["requested"]["cpu_cores"] == 1.0


def test_init_containers_do_not_add_to_the_steady_state():
    """A 4-core migration that runs for thirty seconds is not 4 cores of footprint.

    The pod's request is the larger of (app containers) and (biggest init
    container). Summing both would charge this node 4.5 cores for a pod holding
    0.5, and the "requested" column would then disagree with
    ``kubectl describe node`` on every workload that runs a migration.
    """
    pod = _pod(
        cpu="500m", memory="1Gi",
        init=[obj(name="migrate", resources=obj(requests={"cpu": "4", "memory": "2Gi"}),
                  restart_policy=None)],
    )
    usage = node_usage([pod])
    assert usage["requested"]["cpu_cores"] == 4.0
    assert usage["requested"]["memory_bytes"] == 2 * 1024**3


def test_sidecar_init_containers_do_add():
    """``restartPolicy: Always`` init containers keep running, so they keep costing."""
    pod = _pod(
        cpu="500m", memory="1Gi",
        init=[obj(name="proxy", resources=obj(requests={"cpu": "250m", "memory": "128Mi"}),
                  restart_policy="Always")],
    )
    usage = node_usage([pod])
    assert usage["requested"]["cpu_cores"] == 0.75
    assert usage["requested"]["memory_bytes"] == 1024**3 + 128 * 1024**2


def test_pod_overhead_is_charged_to_the_node():
    """A Kata or gVisor sandbox is not free, and the scheduler charges it here."""
    pod = _pod(cpu="500m", memory="1Gi", overhead={"cpu": "250m", "memory": "120Mi"})
    usage = node_usage([pod])
    assert usage["requested"]["cpu_cores"] == 0.75
    assert usage["requested"]["memory_bytes"] == 1024**3 + 120 * 1024**2


def test_unscheduled_pods_are_charged_to_no_node(fake_k8s):
    """A Pending pod with no ``nodeName`` is on no node yet.

    Attributing it to one — or letting a ``""`` bucket match a node — charges a
    machine for resources it is not holding.
    """
    fake_k8s.core_v1.returns("list_node", obj(items=[_node()]))
    fake_k8s.core_v1.returns(
        "list_pod_for_all_namespaces",
        obj(items=[_pod(), _pod("unscheduled", node=None, phase="Pending", cpu="8")]),
    )

    row = list_nodes()["items"][0]
    assert row["pod_count"] == 1
    assert row["requested"]["cpu_cores"] == 0.5


# --------------------------------------------------------------------------- #
# Row shape
# --------------------------------------------------------------------------- #

def test_capacity_and_allocatable_are_parsed_not_echoed():
    row = node_row(_node(), requested=None, pod_count=None)
    assert row["capacity"] == {"cpu_cores": 16.0, "memory_bytes": 64 * 1024**3, "pods": 110}
    assert row["allocatable"] == {
        "cpu_cores": 15.8, "memory_bytes": 62 * 1024**3, "pods": 110,
    }


def test_roles_come_from_labels_and_are_never_invented():
    """An unlabelled node gets ``[]``, the way ``kubectl get nodes`` shows ``<none>``.

    Defaulting to ``["worker"]`` would be the console asserting a fact the
    cluster never stated — and on a control-plane node whose role label was
    stripped during an upgrade it would assert the opposite of the truth, next to
    a drain button.
    """
    assert node_row(_node(labels={}), requested=None, pod_count=None)["roles"] == []

    labelled = _node(labels={
        "node-role.kubernetes.io/control-plane": "",
        "node-role.kubernetes.io/master": "",
        "kubernetes.io/os": "linux",
    })
    assert node_row(labelled, requested=None, pod_count=None)["roles"] == [
        "control-plane", "master",
    ]

    legacy = _node(labels={"kubernetes.io/role": "infra"})
    assert node_row(legacy, requested=None, pod_count=None)["roles"] == ["infra"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("True", True), ("False", False), ("Unknown", None)],
)
def test_ready_is_tri_state(status, expected):
    """``Unknown`` is not ``false``.

    ``False`` is the node telling us it is not ready — a kubelet or CNI problem
    on a machine that is up and talking. ``Unknown`` is the node controller
    saying the kubelet has stopped reporting, which may mean the machine is gone.
    They lead to different next actions, so they render differently.
    """
    assert node_row(_node(ready=status), requested=None, pod_count=None)["ready"] is expected


def test_ready_is_null_when_the_condition_is_absent():
    assert node_row(_node(conditions=[]), requested=None, pod_count=None)["ready"] is None


def test_unschedulable_defaults_to_false_not_null():
    """The field is omitted on a normal node, and omitted means schedulable.

    This is a real default rather than a missing reading, so it is a boolean —
    unlike ``ready``, where the absence of a condition genuinely means we do not
    know.
    """
    assert node_row(_node(), requested=None, pod_count=None)["unschedulable"] is False
    assert node_row(_node(unschedulable=True), requested=None, pod_count=None)["unschedulable"] is True


def test_internal_ip_never_falls_back_to_an_external_one():
    """A value in a column labelled "internal IP" that is not one is worse than a gap."""
    row = node_row(_node(), requested=None, pod_count=None)
    assert row["internal_ip"] == "10.0.1.4"

    without = _node()
    without.status.addresses = [obj(type="ExternalIP", address="52.1.1.1")]
    assert node_row(without, requested=None, pod_count=None)["internal_ip"] is None


def test_taints_and_conditions_and_node_info():
    node = _node(taints=[obj(key="node-role", value="infra", effect="NoSchedule")])
    row = node_row(node, requested=None, pod_count=None)

    assert row["taints"] == [{"key": "node-role", "value": "infra", "effect": "NoSchedule"}]
    assert {"type": "MemoryPressure", "status": "False",
            "reason": "KubeletHasSufficientMemory"} in row["conditions"]
    assert row["kubelet_version"] == "v1.31.4"
    assert row["os_image"] == "Amazon Linux 2023"
    assert row["container_runtime"] == "containerd://1.7.13"
    assert row["age_seconds"] > 0


def test_rows_are_sorted_by_name(fake_k8s):
    """A table that reshuffles between refreshes is a table nobody can compare."""
    fake_k8s.core_v1.returns(
        "list_node",
        obj(items=[_node("ip-10-0-1-9"), _node("ip-10-0-1-4"), _node("ip-10-0-1-7")]),
    )
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[]))

    assert [row["name"] for row in list_nodes()["items"]] == [
        "ip-10-0-1-4", "ip-10-0-1-7", "ip-10-0-1-9",
    ]


def test_dict_shaped_nodes_are_read_too():
    """The generic reader hands back camelCase JSON; the typed client hands back models.

    Both reach the shaper, so both are read. A shaper that only understood one of
    them would return a row of nulls for the other, and nulls render as "could
    not read" — a gap invented by a spelling mismatch inside this process.
    """
    row = node_row(
        {
            "metadata": {"name": "kind-worker",
                         "labels": {"node-role.kubernetes.io/worker": ""}},
            "spec": {"unschedulable": True},
            "status": {
                "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
                "allocatable": {"cpu": "8", "memory": "16Gi", "pods": "110"},
                "conditions": [{"type": "Ready", "status": "True"}],
                "addresses": [{"type": "InternalIP", "address": "172.18.0.3"}],
                "nodeInfo": {"kubeletVersion": "v1.31.4", "osImage": "Debian 12",
                             "containerRuntimeVersion": "containerd://1.7.13"},
            },
        },
        requested=None,
        pod_count=None,
    )

    assert row["name"] == "kind-worker"
    assert row["roles"] == ["worker"]
    assert row["ready"] is True
    assert row["unschedulable"] is True
    assert row["internal_ip"] == "172.18.0.3"
    assert row["kubelet_version"] == "v1.31.4"
    assert row["capacity"]["memory_bytes"] == 16 * 1024**3
