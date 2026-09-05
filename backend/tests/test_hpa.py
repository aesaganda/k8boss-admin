"""
HorizontalPodAutoscaler management (§21).

Two integers on one object, through the funnel. What is worth testing is not
that a patch is sent — §4 covers that — but the four things this endpoint claims
that a YAML editor does not:

* **An autoscaler that is not scaling says so.** `ScalingActive: False` means the
  controller cannot compute a desired count, usually because the metric it needs
  is gone. Such an HPA looks entirely normal in `kubectl get hpa`, and raising
  its ceiling during an incident changes a number and nothing else. That is a
  consequence acknowledged by name, not a footnote.

* **A metric with no reading is `None`, never `0`.** A CPU target drawn at 0%
  reads as an idle workload, and idle is what gets scaled down — so a spec
  metric the controller has published no reading for stays unpaired rather than
  borrowing its neighbour's value.

* **Lowering the ceiling below the current count is a scale-down now.** Not a
  limit on future growth. The form field looks identical and the outcome is
  pods terminating, so the consequence names how many.

* **A manual scale on an autoscaled workload is reverted seconds later.** §6's
  scale reports `applied: true` truthfully and misleadingly; `governedBy` is the
  missing half, and it is tri-state because "we could not read the autoscalers"
  must never render as "nothing will undo this".
"""

from __future__ import annotations

import pytest

from app.admin import apply as apply_service
from app.admin import hpa
from app.audit import recorder
from app.errors import Conflict, Invalid, RBACDenied
from app.resources import catalog
from app.resources.shaping import hpa_row
from tests.conftest import obj
from tests.test_routes import _groups_payload, _resources

NAMESPACE = "prod"
NAME = "checkout"


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


def condition(kind, status, *, reason=None, message=None):
    return {"type": kind, "status": status, "reason": reason, "message": message}


def cpu_metric(target=70):
    return {"type": "Resource", "resource": {
        "name": "cpu", "target": {"type": "Utilization", "averageUtilization": target},
    }}


def cpu_current(value=45):
    return {"type": "Resource", "resource": {
        "name": "cpu", "current": {"averageUtilization": value, "averageValue": "90m"},
    }}


def autoscaler(
    *,
    minimum=2,
    maximum=10,
    current_replicas=3,
    desired_replicas=3,
    metrics=None,
    current_metrics=None,
    conditions=None,
    target_kind="Deployment",
    target_name="checkout",
    resource_version="9040",
) -> dict:
    body: dict = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": NAME, "namespace": NAMESPACE, "resourceVersion": resource_version,
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1", "kind": target_kind, "name": target_name,
            },
            "maxReplicas": maximum,
            "metrics": [cpu_metric()] if metrics is None else metrics,
        },
        "status": {
            "currentMetrics": [cpu_current()] if current_metrics is None else current_metrics,
            "conditions": list(conditions) if conditions is not None else [
                condition("AbleToScale", "True", reason="ReadyForNewScale"),
                condition("ScalingActive", "True", reason="ValidMetricFound"),
                condition("ScalingLimited", "False", reason="DesiredWithinRange"),
            ],
        },
    }
    if minimum is not None:
        body["spec"]["minReplicas"] = minimum
    if current_replicas is not None:
        body["status"]["currentReplicas"] = current_replicas
    if desired_replicas is not None:
        body["status"]["desiredReplicas"] = desired_replicas
    return body


@pytest.fixture
def cluster(monkeypatch, fake_k8s):
    """Discovery for HPAs, an allowing preflight, and a patchable server."""

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return _groups_payload(("autoscaling", "v2"))
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/api/v1":
            return _resources()
        if path == "/apis/autoscaling/v2":
            return _resources(("horizontalpodautoscalers", "HorizontalPodAutoscaler"))
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    return fake_k8s


def stub_read(monkeypatch, *, live=None, error=None):
    def fake_get(group, version, plural, name, namespace=None):
        assert plural == "horizontalpodautoscalers", plural
        if error is not None:
            raise error
        return live if live is not None else autoscaler()

    monkeypatch.setattr(hpa.reader, "get_resource", fake_get)


def stub_patch(monkeypatch, *, raises=None):
    calls: list[dict] = []

    def fake_request_json(method, path, **kwargs):
        calls.append({
            "method": method, "path": path,
            "query": dict(q for q in (kwargs.get("query") or []) if q[1] is not None),
            "body": kwargs.get("body"),
            "content_type": kwargs.get("content_type"),
        })
        if raises is not None:
            raise raises
        body = kwargs.get("body") or {}
        patched = autoscaler()
        patched["spec"].update(body.get("spec") or {})
        return patched, []

    monkeypatch.setattr(apply_service, "request_json", fake_request_json)
    return calls


def audit_rows():
    return recorder.query(limit=50)["items"]


def body(minimum=2, maximum=30, **extra):
    return {"minReplicas": minimum, "maxReplicas": maximum, **extra}


# --------------------------------------------------------------------------- #
# The row — whether this autoscaler is doing anything
# --------------------------------------------------------------------------- #

def test_a_healthy_autoscaler_reports_its_three_conditions_as_booleans():
    row = hpa_row(autoscaler())

    assert row["able_to_scale"] is True
    assert row["scaling_active"] is True
    assert row["scaling_limited"] is False


def test_an_autoscaler_that_cannot_read_its_metric_is_not_scaling():
    """The failure this row exists for. `kubectl get hpa` shows <unknown> in one
    column and everything else looks normal."""
    row = hpa_row(autoscaler(
        current_metrics=[],
        conditions=[
            condition("AbleToScale", "True", reason="SucceededGetScale"),
            condition(
                "ScalingActive", "False", reason="FailedGetResourceMetric",
                message="unable to get metrics for resource cpu: no metrics returned",
            ),
        ],
    ))

    assert row["scaling_active"] is False
    assert row["conditions"]["ScalingActive"]["reason"] == "FailedGetResourceMetric"


def test_a_condition_the_controller_has_not_written_is_none_not_false():
    """A fresh HPA has no conditions. `None` is "not observed yet"; `False` is
    "observed and broken", and rendering the first as the second reports a
    healthy autoscaler as failing for the seconds after it is created."""
    row = hpa_row(autoscaler(conditions=[]))

    assert row["scaling_active"] is None
    assert row["able_to_scale"] is None
    assert row["scaling_limited"] is None


def test_a_metric_with_no_reading_is_null_never_zero():
    """A CPU metric drawn at 0% reads as an idle workload, and idle is what gets
    scaled down."""
    row = hpa_row(autoscaler(current_metrics=[]))

    (metric,) = row["metrics"]
    assert metric["target"] == "70%"
    assert metric["current"] is None


def test_metrics_are_paired_by_identity_not_by_position():
    """Two lists in no guaranteed order. Pairing by index gives the memory
    metric the CPU reading the moment one of them is unobserved."""
    row = hpa_row(autoscaler(
        metrics=[
            cpu_metric(),
            {"type": "Resource", "resource": {
                "name": "memory",
                "target": {"type": "AverageValue", "averageValue": "500Mi"},
            }},
        ],
        # Only memory has been observed, and it is first in the status list.
        current_metrics=[
            {"type": "Resource", "resource": {
                "name": "memory", "current": {"averageValue": "482Mi"},
            }},
        ],
    ))

    cpu, memory = row["metrics"]
    assert cpu["name"] == "cpu"
    assert cpu["current"] is None
    assert memory["name"] == "memory"
    assert memory["current"] == "482Mi"


def test_the_metric_list_is_driven_by_spec_so_an_unread_metric_still_appears():
    """A status-driven list would drop exactly the metric worth seeing."""
    row = hpa_row(autoscaler(metrics=[cpu_metric()], current_metrics=[]))

    assert [m["name"] for m in row["metrics"]] == ["cpu"]


def test_an_external_metric_is_named_from_its_metric_block():
    row = hpa_row(autoscaler(
        metrics=[{"type": "External", "external": {
            "metric": {"name": "queue_depth"},
            "target": {"type": "AverageValue", "averageValue": "30"},
        }}],
        current_metrics=[{"type": "External", "external": {
            "metric": {"name": "queue_depth"}, "current": {"averageValue": "412"},
        }}],
    ))

    (metric,) = row["metrics"]
    assert (metric["kind"], metric["name"]) == ("External", "queue_depth")
    assert (metric["target"], metric["current"]) == ("30", "412")


def test_replica_counts_the_controller_has_not_published_are_null():
    row = hpa_row(autoscaler(current_replicas=None, desired_replicas=None))

    assert row["current_replicas"] is None
    assert row["desired_replicas"] is None


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("missing", ["minReplicas", "maxReplicas"])
def test_both_bounds_are_required_because_absent_would_be_ambiguous(missing):
    """A bound left out could mean "leave it" or "reset it", and the resolution
    that eventually happens is somebody's floor going back to 1."""
    payload = body()
    del payload[missing]

    with pytest.raises(Invalid) as caught:
        hpa.validate_request(payload)

    assert caught.value.context["parameter"] == missing


def test_a_floor_above_the_ceiling_is_refused_naming_both():
    with pytest.raises(Invalid) as caught:
        hpa.validate_request(body(minimum=30, maximum=10))

    assert caught.value.context["minReplicas"] == 30
    assert caught.value.context["maxReplicas"] == 10


def test_a_ceiling_of_zero_is_refused():
    with pytest.raises(Invalid) as caught:
        hpa.validate_request(body(minimum=0, maximum=0))

    assert caught.value.context["parameter"] == "maxReplicas"


def test_a_boolean_is_not_a_replica_count():
    """`True` is an int in Python and would arrive as one replica."""
    with pytest.raises(Invalid) as caught:
        hpa.validate_request(body(minimum=True))

    assert caught.value.context["parameter"] == "minReplicas"


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(Invalid):
        hpa.validate_request(body(behavior={"scaleDown": {}}))


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def test_an_inert_autoscaler_is_the_headline_consequence(cluster, monkeypatch, db_engine):
    """Raising the ceiling on an HPA that cannot read its metrics changes a
    number in etcd and nothing else."""
    stub_read(monkeypatch, live=autoscaler(conditions=[
        condition("AbleToScale", "True"),
        condition("ScalingActive", "False", reason="FailedGetResourceMetric",
                  message="no metrics returned from resource metrics API"),
    ]))

    plan = hpa.plan(NAMESPACE, NAME, body(maximum=30))
    entry = next(e for e in plan["consequences"] if e["code"] == hpa.WARN_NOT_SCALING)

    assert "FailedGetResourceMetric" in entry["consequence"]
    assert "no metrics returned" in entry["consequence"]


def test_a_scaling_autoscaler_raises_no_inert_consequence(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch)

    plan = hpa.plan(NAMESPACE, NAME, body(maximum=30))

    assert hpa.WARN_NOT_SCALING not in [e["code"] for e in plan["consequences"]]


def test_lowering_the_ceiling_below_the_current_count_says_how_many_pods_go(
    cluster, monkeypatch, db_engine,
):
    """Not a limit on future growth. Pods terminate at the next scale decision,
    which is seconds away."""
    stub_read(monkeypatch, live=autoscaler(current_replicas=9))

    plan = hpa.plan(NAMESPACE, NAME, body(minimum=2, maximum=4))
    entry = next(e for e in plan["consequences"] if e["code"] == hpa.WARN_MAX_BELOW_CURRENT)

    assert "5 pod(s) are terminated" in entry["label"]


def test_raising_the_floor_above_the_current_count_says_how_many_start(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch, live=autoscaler(current_replicas=3))

    plan = hpa.plan(NAMESPACE, NAME, body(minimum=8, maximum=30))
    entry = next(e for e in plan["consequences"] if e["code"] == hpa.WARN_MIN_ABOVE_CURRENT)

    assert "5 pod(s) are started" in entry["label"]


def test_bounds_that_straddle_the_current_count_are_neither(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch, live=autoscaler(current_replicas=5))

    codes = [e["code"] for e in hpa.plan(NAMESPACE, NAME, body(2, 30))["consequences"]]

    assert hpa.WARN_MAX_BELOW_CURRENT not in codes
    assert hpa.WARN_MIN_ABOVE_CURRENT not in codes


def test_an_unpublished_replica_count_withholds_the_immediacy_verdict(
    cluster, monkeypatch, db_engine,
):
    """"This terminates five pods" is arithmetic on a number we do not have, and
    guessing it either way is a claim about somebody's workload."""
    stub_read(monkeypatch, live=autoscaler(current_replicas=None))

    codes = [e["code"] for e in hpa.plan(NAMESPACE, NAME, body(2, 4))["consequences"]]

    assert hpa.WARN_REPLICAS_UNKNOWN in codes
    assert hpa.WARN_MAX_BELOW_CURRENT not in codes
    assert hpa.WARN_MIN_ABOVE_CURRENT not in codes


def test_a_floor_of_zero_names_the_feature_gate_no_api_reports(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch)

    codes = [e["code"] for e in hpa.plan(NAMESPACE, NAME, body(0, 30))["consequences"]]

    assert hpa.WARN_SCALE_TO_ZERO_GATED in codes


def test_an_autoscaler_that_cannot_reach_its_target_says_so(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch, live=autoscaler(conditions=[
        condition("AbleToScale", "False", reason="FailedGetScale",
                  message='deployments/scale.apps "checkout" not found'),
    ]))

    codes = [e["code"] for e in hpa.plan(NAMESPACE, NAME, body(2, 30))["consequences"]]

    assert hpa.WARN_CANNOT_REACH_TARGET in codes


def test_a_write_without_the_acknowledgements_is_refused_naming_them(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch, live=autoscaler(current_replicas=9))
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        hpa.set_bounds(NAMESPACE, NAME, body(2, 4), dry_run=False,
                       acknowledge_consequences=[])

    assert caught.value.context["unacknowledged"] == [hpa.WARN_MAX_BELOW_CURRENT]
    assert calls == []
    assert audit_rows() == []


def test_acknowledgements_are_recomputed_against_the_autoscaler_not_trusted(
    cluster, monkeypatch, db_engine,
):
    """The replica count moves on its own between the plan and the write, so the
    codes accepted must be the ones true at write time."""
    stub_read(monkeypatch, live=autoscaler(current_replicas=9, conditions=[
        condition("ScalingActive", "False", reason="FailedGetResourceMetric"),
    ]))
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        hpa.set_bounds(NAMESPACE, NAME, body(2, 4), dry_run=False,
                       acknowledge_consequences=[hpa.WARN_MAX_BELOW_CURRENT])

    assert caught.value.context["unacknowledged"] == [hpa.WARN_NOT_SCALING]
    assert calls == []


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def test_the_plan_reports_a_no_op_as_data_with_the_autoscaler_still_described(
    cluster, monkeypatch, db_engine,
):
    """The plan is the screen where the bounds are decided. One that answered
    "you changed nothing" with an error alone would withhold the current bounds
    and the replica count at the moment those are the facts needed."""
    stub_read(monkeypatch)

    plan = hpa.plan(NAMESPACE, NAME, body(2, 10))

    assert "already runs between 2 and 10" in plan["blocked"]["message"]
    assert plan["current"]["min_replicas"] == 2
    assert plan["current"]["max_replicas"] == 10
    assert plan["current"]["current_replicas"] == 3
    assert plan["consequences"] == []


def test_a_plan_that_changes_something_has_no_blocked_entry(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch)

    plan = hpa.plan(NAMESPACE, NAME, body(2, 30))

    assert plan["blocked"] is None


def test_the_plan_writes_nothing_and_audits_nothing(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    hpa.plan(NAMESPACE, NAME, body(2, 30))

    assert calls == []
    assert audit_rows() == []


# --------------------------------------------------------------------------- #
# The write, through the funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_sends_dryrun_all_and_changes_nothing(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    result = hpa.set_bounds(NAMESPACE, NAME, body(2, 30), dry_run=True,
                            acknowledge_consequences=[])

    (call,) = calls
    assert call["method"] == "PATCH"
    assert call["query"]["dryRun"] == "All"
    assert call["content_type"] == "application/merge-patch+json"
    assert result["applied"] is False
    (row,) = audit_rows()
    assert row["outcome"] == "dry_run"


def test_a_confirmed_write_patches_both_bounds_and_audits_both_sides(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    result = hpa.set_bounds(NAMESPACE, NAME, body(2, 30), dry_run=False,
                            acknowledge_consequences=[])

    (call,) = calls
    assert "dryRun" not in call["query"]
    assert call["body"]["spec"] == {"minReplicas": 2, "maxReplicas": 30}
    assert result["applied"] is True
    (row,) = audit_rows()
    assert row["detail"] == f"hpa bounds {NAMESPACE}/{NAME}: 2-10 -> 2-30"


def test_applied_true_reports_whether_the_autoscaler_scales_at_all(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """The one claim this endpoint must never make. `applied` says the bounds are
    stored; `current.scaling_active` says whether anything will act on them."""
    stub_read(monkeypatch, live=autoscaler(conditions=[
        condition("ScalingActive", "False", reason="FailedGetResourceMetric"),
    ]))
    stub_patch(monkeypatch)

    result = hpa.set_bounds(NAMESPACE, NAME, body(2, 30), dry_run=False,
                            acknowledge_consequences=[hpa.WARN_NOT_SCALING])

    assert result["applied"] is True
    assert result["current"]["scaling_active"] is False
    assert result["requested"] == {"minReplicas": 2, "maxReplicas": 30}


def test_the_resource_version_rides_inside_the_patch(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    hpa.set_bounds(NAMESPACE, NAME, body(2, 30, resourceVersion="9040"),
                   dry_run=False, acknowledge_consequences=[])

    assert calls[0]["body"]["metadata"]["resourceVersion"] == "9040"


def test_a_stale_resource_version_is_a_conflict_carrying_the_live_bounds(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    with pytest.raises(Conflict) as caught:
        hpa.set_bounds(NAMESPACE, NAME, body(2, 30, resourceVersion="8000"),
                       dry_run=False, acknowledge_consequences=[])

    assert caught.value.context["currentResourceVersion"] == "9040"
    assert caught.value.context["currentMaxReplicas"] == 10
    assert calls == []


def test_a_no_op_write_is_refused_rather_than_audited_as_a_change(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid):
        hpa.set_bounds(NAMESPACE, NAME, body(2, 10), dry_run=False,
                       acknowledge_consequences=[])

    assert calls == []
    assert audit_rows() == []


def test_the_preflight_asks_for_patch_on_hpas_in_this_namespace(
    cluster, monkeypatch, db_engine, fake_k8s,
):
    stub_read(monkeypatch)
    stub_patch(monkeypatch)

    hpa.set_bounds(NAMESPACE, NAME, body(2, 30), dry_run=True,
                   acknowledge_consequences=[])

    ((args, _kwargs),) = fake_k8s.authorization_v1.called(
        "create_self_subject_access_review"
    )
    attributes = args[0].spec.resource_attributes
    assert attributes.verb == "patch"
    assert attributes.resource == "horizontalpodautoscalers"
    assert attributes.group == "autoscaling"
    assert attributes.namespace == NAMESPACE


def test_an_autoscaler_that_could_not_be_read_is_not_written_against_a_default(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch, error=RBACDenied("horizontalpodautoscalers is forbidden"))
    calls = stub_patch(monkeypatch)

    with pytest.raises(RBACDenied):
        hpa.set_bounds(NAMESPACE, NAME, body(2, 30), dry_run=True,
                       acknowledge_consequences=[])

    assert calls == []


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

def test_the_plan_endpoint_answers_a_no_op_with_200(client, cluster, monkeypatch):
    stub_read(monkeypatch)

    response = client.post(
        f"/api/autoscaling/hpas/{NAMESPACE}/{NAME}/bounds/plan",
        json={"minReplicas": 2, "maxReplicas": 10},
    )

    assert response.status_code == 200
    assert response.json()["blocked"] is not None


def test_the_write_endpoint_defaults_to_a_dry_run(client, cluster, monkeypatch):
    """§0.3: a client that forgets the field gets a projection, not a write."""
    stub_read(monkeypatch)
    stub_patch(monkeypatch)

    response = client.put(
        f"/api/autoscaling/hpas/{NAMESPACE}/{NAME}/bounds",
        json={"minReplicas": 2, "maxReplicas": 30},
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False


def test_the_write_endpoint_refuses_an_inverted_range_with_422(client, cluster, monkeypatch):
    stub_read(monkeypatch)

    response = client.put(
        f"/api/autoscaling/hpas/{NAMESPACE}/{NAME}/bounds",
        json={"minReplicas": 30, "maxReplicas": 10, "dryRun": False},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
