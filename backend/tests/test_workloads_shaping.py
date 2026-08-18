"""
Row shaping for the unified workload model (§6).

The three properties under test are the three the contract is explicit about,
and each of them is a case where the easy implementation produces a number that
is wrong rather than absent:

* **``Unknown`` is a real branch.** A controller that has not reported on the
  current generation of an object has told us nothing, and "nothing reported"
  satisfies every equality test for health — 0 desired, 0 ready. A workload that
  goes out green because its controller has not run yet is the defect standard
  in one row.
* **Zero and unknown are different numbers.** Kubernetes omits zero-valued
  status counters from the wire, so an absent ``readyReplicas`` means zero once
  the controller has written a status and means *unknown* before that. Likewise
  ``restarts_24h`` is ``0`` when we looked and found none, and ``None`` when we
  could not look.
* **Images come from every container list.** An init container that cannot pull
  its image is why a workload never starts; a row that shows only
  ``spec.containers`` cannot show the operator the image they need to fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.workloads import (
    PodIndex,
    container_images,
    controller_observed,
    derive_status,
    replica_counts,
    restarts_in_window,
    selector_matches,
    workload_row,
)
from tests.conftest import obj

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _template(containers=("ghcr.io/acme/checkout:1.9.2",), init=(), ephemeral=(), labels=None):
    return obj(
        metadata=obj(labels=labels if labels is not None else {"app": "checkout"}),
        spec=obj(
            containers=[obj(name=f"c{i}", image=image) for i, image in enumerate(containers)],
            init_containers=[obj(name=f"i{i}", image=image) for i, image in enumerate(init)],
            ephemeral_containers=[obj(name=f"e{i}", image=image) for i, image in enumerate(ephemeral)],
            service_account_name="checkout",
        ),
    )


def _selector(labels=None, expressions=()):
    return obj(match_labels=labels if labels is not None else {"app": "checkout"},
               match_expressions=list(expressions))


def _deployment(*, generation=4, observed=4, desired=5, ready=5, updated=5, available=5,
                conditions=(), template=None, created=None, annotations=None):
    return obj(
        metadata=obj(
            name="checkout", namespace="prod", uid="dep-uid", generation=generation,
            labels={"app": "checkout"}, annotations=annotations or {},
            creation_timestamp=created,
        ),
        spec=obj(replicas=desired, selector=_selector(), template=template or _template()),
        status=obj(
            observed_generation=observed, ready_replicas=ready, updated_replicas=updated,
            available_replicas=available, conditions=list(conditions),
        ),
    )


def _condition(type_, status, reason=None, message=None):
    return obj(type=type_, status=status, reason=reason, message=message,
               last_transition_time=NOW)


def _daemonset(*, generation=1, observed=1, desired=6, ready=6, updated=6, available=6):
    return obj(
        metadata=obj(name="cilium", namespace="kube-system", uid="ds-uid",
                     generation=generation, labels={}, creation_timestamp=None),
        spec=obj(selector=_selector({"k8s-app": "cilium"}), template=_template()),
        status=obj(
            observed_generation=observed, desired_number_scheduled=desired,
            number_ready=ready, updated_number_scheduled=updated, number_available=available,
        ),
    )


def _job(*, suspend=False, completions=3, succeeded=None, active=None, failed=None,
         start_time=NOW, conditions=()):
    return obj(
        metadata=obj(name="migrate", namespace="prod", uid="job-uid", labels={},
                     creation_timestamp=None),
        spec=obj(suspend=suspend, completions=completions, parallelism=1,
                 selector=_selector({"job-name": "migrate"}), template=_template()),
        status=obj(start_time=start_time, succeeded=succeeded, active=active,
                   failed=failed, conditions=list(conditions)),
    )


def _cronjob(*, suspend=False, schedule="0 3 * * *", last_schedule=None,
             last_success=None, active=()):
    return obj(
        metadata=obj(name="nightly", namespace="prod", uid="cj-uid", labels={},
                     creation_timestamp=None),
        spec=obj(
            suspend=suspend, schedule=schedule,
            job_template=obj(spec=obj(template=_template(("ghcr.io/acme/nightly:3",)))),
        ),
        status=obj(last_schedule_time=last_schedule, last_successful_time=last_success,
                   active=list(active)),
    )


def _pod(*, restarts=0, init_restarts=0, started=None, last_exit=None,
         labels=None, namespace="prod", owner_uid=None, name="checkout-7d9-abc"):
    def _status(count, exit_at):
        return obj(
            name="app", restart_count=count,
            last_state=obj(terminated=obj(finished_at=exit_at)) if exit_at else obj(),
        )

    return obj(
        metadata=obj(
            name=name, namespace=namespace, labels=labels or {"app": "checkout"},
            owner_references=[obj(kind="ReplicaSet", name="checkout-7d9", uid=owner_uid)]
            if owner_uid else [],
        ),
        status=obj(
            start_time=started,
            container_statuses=[_status(restarts, last_exit)] if restarts else [],
            init_container_statuses=[_status(init_restarts, last_exit)] if init_restarts else [],
        ),
    )


# --------------------------------------------------------------------------- #
# Unknown — the branch that must exist
# --------------------------------------------------------------------------- #

def test_a_controller_that_has_written_no_status_is_unknown_not_healthy():
    """0 desired / 0 ready satisfies every equality test for health. It is not health."""
    fresh = _deployment(generation=1, observed=None, desired=None,
                        ready=None, updated=None, available=None)

    status, reason = derive_status("Deployment", fresh)

    assert status == "Unknown"
    assert "not written a status" in reason


def test_a_stale_observed_generation_is_unknown_and_says_which_revision():
    """The counts are real — they just describe the spec from before the edit."""
    mid_apply = _deployment(generation=9, observed=8, desired=5, ready=5, updated=5, available=5)

    status, reason = derive_status("Deployment", mid_apply)

    assert status == "Unknown"
    assert "8" in reason and "9" in reason


def test_a_current_generation_is_observed():
    assert controller_observed("Deployment", _deployment(generation=4, observed=4)) == (True, None)


def test_a_job_the_controller_has_not_touched_is_unknown():
    untouched = _job(start_time=None, succeeded=None, active=None, failed=None, conditions=())

    status, reason = derive_status("Job", untouched)

    assert status == "Unknown"
    assert "not started" in reason


def test_a_cronjob_between_runs_is_not_unknown():
    """A correct CronJob has no status between runs. Marking those Unknown would
    bury the rows that genuinely are."""
    assert derive_status("CronJob", _cronjob())[0] == "Healthy"


def test_a_cronjob_without_a_schedule_is_unknown():
    status, reason = derive_status("CronJob", _cronjob(schedule=None))

    assert status == "Unknown"
    assert "schedule" in reason


# --------------------------------------------------------------------------- #
# The status table
# --------------------------------------------------------------------------- #

def test_a_fully_rolled_out_deployment_is_healthy_with_no_reason():
    status, reason = derive_status("Deployment", _deployment())

    assert status == "Healthy"
    assert reason is None, "Healthy needs no explanation; the UI renders nothing"


def test_a_partially_updated_deployment_is_progressing():
    status, reason = derive_status("Deployment", _deployment(updated=3, ready=3, available=3))

    assert status == "Progressing"
    assert "3 of 5" in reason


def test_a_deployment_past_its_progress_deadline_is_degraded():
    """Progressing=False means the controller gave up. Reporting it as Progressing
    tells the operator to wait for something that has already stopped."""
    stalled = _deployment(
        ready=4, available=4,
        conditions=[_condition("Progressing", "False", "ProgressDeadlineExceeded",
                               'ReplicaSet "checkout-7d9" has timed out progressing.')],
    )

    status, reason = derive_status("Deployment", stalled)

    assert status == "Degraded"
    assert "ProgressDeadlineExceeded" in reason
    assert "timed out" in reason


def test_a_deployment_that_cannot_create_pods_is_degraded():
    quota_bound = _deployment(
        ready=0, updated=5, available=0,
        conditions=[_condition("ReplicaFailure", "True", "FailedCreate",
                               "pods \"checkout-\" is forbidden: exceeded quota")],
    )

    status, reason = derive_status("Deployment", quota_bound)

    assert status == "Degraded"
    assert "exceeded quota" in reason


def test_a_deployment_with_nothing_available_is_degraded():
    down = _deployment(
        ready=0, updated=5, available=0,
        conditions=[_condition("Available", "False", "MinimumReplicasUnavailable",
                               "Deployment does not have minimum availability.")],
    )

    assert derive_status("Deployment", down)[0] == "Degraded"


def test_scaling_to_zero_is_suspended_not_degraded():
    """Zero replicas is a deliberate state. Colouring it red trains operators to
    ignore red; colouring it green claims pods are running."""
    status, reason = derive_status("Deployment", _deployment(desired=0, ready=None,
                                                            updated=None, available=None))

    assert status == "Suspended"
    assert "zero" in reason


def test_a_healthy_daemonset():
    assert derive_status("DaemonSet", _daemonset()) == ("Healthy", None)


def test_a_daemonset_that_matches_no_nodes_is_suspended_not_healthy():
    """0 ready of 0 desired passes every equality test while the DaemonSet runs
    nowhere — the usual symptom of a nodeSelector that matches nothing."""
    status, reason = derive_status(
        "DaemonSet", _daemonset(desired=0, ready=0, updated=0, available=0)
    )

    assert status == "Suspended"
    assert "nodeSelector" in reason


def test_a_daemonset_missing_pods_on_some_nodes_is_progressing():
    status, reason = derive_status("DaemonSet", _daemonset(ready=4, updated=6, available=4))

    assert status == "Progressing"
    assert "2 of 6" in reason


def test_a_daemonset_with_nothing_ready_is_degraded():
    assert derive_status("DaemonSet", _daemonset(ready=0, available=0))[0] == "Degraded"


def test_a_statefulset_on_a_cluster_without_available_replicas_is_still_healthy():
    """availableReplicas only went GA in 1.22. Judging health on it would show
    every healthy StatefulSet on an older cluster as permanently Progressing."""
    sts = obj(
        metadata=obj(name="pg", namespace="data", generation=2, labels={},
                     creation_timestamp=None),
        spec=obj(replicas=3, selector=_selector({"app": "pg"}), template=_template()),
        status=obj(observed_generation=2, ready_replicas=3, updated_replicas=3,
                   available_replicas=None),
    )

    assert derive_status("StatefulSet", sts) == ("Healthy", None)


def test_a_suspended_job_is_suspended_even_though_it_has_no_status():
    """Suspension is checked before the observation gate: a suspended Job never
    starts, so the gate would report the most deliberate state a workload can be
    in as 'unknown'."""
    status, reason = derive_status("Job", _job(suspend=True, start_time=None))

    assert status == "Suspended"
    assert "resumed" in reason


def test_a_suspended_cronjob_is_suspended():
    assert derive_status("CronJob", _cronjob(suspend=True))[0] == "Suspended"


def test_a_completed_job_is_healthy():
    done = _job(succeeded=3, conditions=[_condition("Complete", "True")])

    assert derive_status("Job", done) == ("Healthy", None)


def test_a_failed_job_is_degraded():
    dead = _job(failed=6, conditions=[_condition("Failed", "True", "BackoffLimitExceeded",
                                                 "Job has reached the specified backoff limit")])

    status, reason = derive_status("Job", dead)

    assert status == "Degraded"
    assert "BackoffLimitExceeded" in reason


def test_a_running_job_is_progressing():
    status, reason = derive_status("Job", _job(succeeded=1, active=1))

    assert status == "Progressing"
    assert "1 of 3" in reason


def test_a_cronjob_that_has_never_succeeded_is_degraded():
    """Positive evidence, not an inference from silence: the controller says it
    scheduled a Job and never recorded a success."""
    status, reason = derive_status(
        "CronJob", _cronjob(last_schedule=NOW - timedelta(hours=3), last_success=None)
    )

    assert status == "Degraded"
    assert "successfully" in reason


def test_a_superseded_replicaset_is_suspended():
    """Every Deployment leaves these behind at zero. Flagging them makes the
    ReplicaSet list mostly red on a healthy cluster."""
    old = obj(
        metadata=obj(name="checkout-6c8", namespace="prod", generation=1, labels={},
                     creation_timestamp=None),
        spec=obj(replicas=0, selector=_selector(), template=_template()),
        status=obj(observed_generation=1, ready_replicas=None, available_replicas=None),
    )

    assert derive_status("ReplicaSet", old)[0] == "Suspended"


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet"])
def test_every_non_healthy_status_carries_a_reason(kind):
    """status_reason exists so a badge is never the whole answer."""
    unobserved = obj(metadata=obj(name="x", namespace="prod", generation=3, labels={}),
                     spec=obj(), status=obj())

    status, reason = derive_status(kind, unobserved)

    assert status != "Healthy"
    assert reason and reason.endswith(".")


# --------------------------------------------------------------------------- #
# Zero is not unknown
# --------------------------------------------------------------------------- #

def test_an_omitted_counter_is_zero_once_the_controller_has_written_a_status():
    """Kubernetes drops zero-valued counters from the wire (omitempty)."""
    counts = replica_counts("Deployment", _deployment(ready=None, available=None), True)

    assert counts["ready"] == 0
    assert counts["available"] == 0


def test_the_same_omitted_counter_is_null_before_the_controller_has_written_one():
    counts = replica_counts("Deployment", _deployment(observed=None, ready=None), False)

    assert counts["ready"] is None


def test_a_replicaset_reports_no_updated_count_rather_than_inventing_one():
    """A ReplicaSet's pods are by definition at its own template; there is no
    updatedReplicas field, and reporting updated == desired would invent
    agreement with a field the API does not have."""
    rs = obj(metadata=obj(name="checkout-7d9", namespace="prod", generation=1),
             spec=obj(replicas=5), status=obj(observed_generation=1, ready_replicas=5))

    assert replica_counts("ReplicaSet", rs, True)["updated"] is None


def test_a_cronjob_reports_null_replicas_not_zero():
    """A CronJob owns no pods. 0/0 would render as a workload with nothing
    running, which is a claim about pods it never had."""
    assert replica_counts("CronJob", _cronjob(), True) == {
        "desired": None, "ready": None, "updated": None, "available": None,
    }


# --------------------------------------------------------------------------- #
# restarts_24h
# --------------------------------------------------------------------------- #

def test_a_pod_younger_than_the_window_contributes_its_whole_restart_count():
    """Every restart it has ever had happened inside the window, so the
    cumulative counter is the windowed count."""
    young = _pod(restarts=4, started=NOW - timedelta(hours=3),
                 last_exit=NOW - timedelta(minutes=5))

    assert restarts_in_window(young, now=NOW) == 4


def test_an_old_pod_whose_last_restart_predates_the_window_contributes_nothing():
    """The kubelet's counter is cumulative for the life of the pod: reporting it
    would put a three-week-old crash loop on today's dashboard."""
    old = _pod(restarts=200, started=NOW - timedelta(days=30),
               last_exit=NOW - timedelta(days=21))

    assert restarts_in_window(old, now=NOW) == 0


def test_an_old_pod_that_restarted_inside_the_window_contributes_a_lower_bound():
    """The kubelet keeps exactly one previous termination, so 20-yesterday-and-1
    -today is indistinguishable from 1-today. Under-counting is the safe
    direction: the crash loop is already reported by status/status_reason."""
    recent = _pod(restarts=200, started=NOW - timedelta(days=30),
                  last_exit=NOW - timedelta(minutes=10))

    assert restarts_in_window(recent, now=NOW) == 1


def test_init_container_restarts_are_counted():
    """A pod stuck on a failing init container restarts forever and never
    reports a restart on spec.containers."""
    stuck = _pod(init_restarts=7, started=NOW - timedelta(hours=1))

    assert restarts_in_window(stuck, now=NOW) == 7


def test_zero_restarts_is_zero_not_null():
    index = PodIndex([_pod(restarts=0)], now=NOW)

    assert index.restarts_for_selector("prod", _selector()) == 0


def test_restarts_are_summed_over_the_pods_the_selector_picks():
    index = PodIndex(
        [
            _pod(restarts=2, started=NOW - timedelta(hours=1), name="a"),
            _pod(restarts=3, started=NOW - timedelta(hours=1), name="b"),
            _pod(restarts=9, started=NOW - timedelta(hours=1), name="other",
                 labels={"app": "payments"}),
        ],
        now=NOW,
    )

    assert index.restarts_for_selector("prod", _selector()) == 5


def test_restarts_from_another_namespace_are_not_attributed():
    index = PodIndex([_pod(restarts=4, started=NOW, namespace="staging")], now=NOW)

    assert index.restarts_for_selector("prod", _selector()) == 0


def test_an_empty_selector_yields_unknown_rather_than_a_namespace_wide_total():
    """An empty LabelSelector matches every pod in the namespace. Attributing a
    whole namespace's restarts to one workload is confidently wrong, not
    missing."""
    index = PodIndex([_pod(restarts=4, started=NOW)], now=NOW)

    assert index.restarts_for_selector("prod", _selector(labels={})) is None


def test_restarts_are_attributable_by_owner_for_cronjobs():
    """A CronJob has no selector; ownership is the only true link to its pods."""
    index = PodIndex(
        [_pod(restarts=2, started=NOW, owner_uid="job-1"),
         _pod(restarts=5, started=NOW, owner_uid="job-2", name="other")],
        now=NOW,
    )

    assert index.restarts_for_owners({"job-1"}) == 2


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "operator,values,labels,expected",
    [
        ("In", ["prod", "staging"], {"tier": "prod"}, True),
        ("In", ["prod"], {"tier": "dev"}, False),
        ("NotIn", ["dev"], {"tier": "prod"}, True),
        ("NotIn", ["prod"], {"tier": "prod"}, False),
        ("Exists", [], {"tier": "anything"}, True),
        ("Exists", [], {}, False),
        ("DoesNotExist", [], {}, True),
        ("DoesNotExist", [], {"tier": "prod"}, False),
    ],
)
def test_match_expressions_are_implemented(operator, values, labels, expected):
    """Supporting only matchLabels would attribute no pods to any workload that
    used an expression — and the row would then report restarts_24h: 0, which is
    a number, not a gap, so nothing downstream could tell it was invented."""
    selector = _selector(labels={}, expressions=[obj(key="tier", operator=operator, values=values)])

    assert selector_matches(selector, labels) is expected


def test_an_unknown_selector_operator_does_not_match():
    """Over-matching would attribute other workloads' pods to this row."""
    selector = _selector(labels={}, expressions=[obj(key="tier", operator="Gt", values=["3"])])

    assert selector_matches(selector, {"tier": "5"}) is False


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #

def test_images_come_from_every_container_list():
    deployment = _deployment(template=_template(
        containers=("app:1.0", "istio-proxy:1.20"),
        init=("migrations:1.0",),
        ephemeral=("busybox:debug",),
    ))

    assert container_images(deployment, "Deployment") == [
        "migrations:1.0", "app:1.0", "istio-proxy:1.20", "busybox:debug",
    ]


def test_images_are_deduplicated_in_declaration_order():
    """sorted(set(...)) would shuffle a sidecar stack and make two rows running
    the same images look different."""
    deployment = _deployment(template=_template(containers=("app:1.0", "app:1.0", "sidecar:2")))

    assert container_images(deployment, "Deployment") == ["app:1.0", "sidecar:2"]


def test_cronjob_images_come_from_the_job_template():
    assert container_images(_cronjob(), "CronJob") == ["ghcr.io/acme/nightly:3"]


# --------------------------------------------------------------------------- #
# The row
# --------------------------------------------------------------------------- #

def test_a_deployment_row_carries_the_contract_fields():
    row = workload_row(
        _deployment(created=NOW - timedelta(days=14)), "Deployment",
        restarts_24h=3, now=NOW,
    )

    assert row == {
        "kind": "Deployment",
        "name": "checkout",
        "namespace": "prod",
        "replicas": {"desired": 5, "ready": 5, "updated": 5, "available": 5},
        "images": ["ghcr.io/acme/checkout:1.9.2"],
        "selector": {"app": "checkout"},
        "labels": {"app": "checkout"},
        "age_seconds": 14 * 24 * 3600,
        "status": "Healthy",
        "status_reason": None,
        "restarts_24h": 3,
        "suspended": None,
        "schedule": None,
        "last_schedule": None,
    }


def test_scheduling_fields_are_null_off_cronjobs_and_populated_on_them():
    cron = workload_row(
        _cronjob(last_schedule=datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc)),
        "CronJob", restarts_24h=0, now=NOW,
    )

    assert cron["schedule"] == "0 3 * * *"
    assert cron["last_schedule"] == "2026-08-18T03:00:00Z"
    assert cron["suspended"] is False


def test_an_undatable_object_has_a_null_age_not_a_zero_one():
    """0 reads as 'created this second', which is a specific claim about an
    object we could not date."""
    assert workload_row(_deployment(created=None), "Deployment",
                        restarts_24h=None, now=NOW)["age_seconds"] is None
