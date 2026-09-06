"""
Quota advice (§29) — will the next workload be admitted, and what refuses it.

A ResourceQuota refuses at admission, and the refusal is a 403 somebody parses
under pressure. Everything needed to answer first is already in the namespace.
What is asserted here is that the arithmetic is honest about what it does not
know, and that it keeps apart two refusals a 403 makes look alike:

  no headroom vs must specify   The first is "the quota is full" and is fixed by
                                deleting something or raising the limit. The
                                second fires when the quota is 1% used, because
                                a quota bounding a compute resource makes it
                                COMPULSORY on every container — and it is fixed
                                by a LimitRange, an object the message never
                                mentions.

  admitted vs unknown           A `status.used` the controller has not written
                                is not zero. An advisor that read it as zero
                                would give its roomiest answer at the moment it
                                knows least, to the person about to deploy.

  applies vs might not          A scoped quota counts some pods. Which ones
                                depends on facts about an object that does not
                                exist yet, so the verdict is withheld rather
                                than invented in either direction.

  omitted vs "0"                An omitted request must be defaulted or the pod
                                is refused. A declared "0" satisfies the rule
                                and consumes nothing.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.services import quota
from tests.conftest import obj

NAMESPACE = "prod"


# --------------------------------------------------------------------------- #
# Builders — raw API objects, because that is what the service shapes
# --------------------------------------------------------------------------- #

def resource_quota(name="team", *, hard=None, used=None, scopes=None, selector=None):
    spec = {"hard": dict(hard or {})}
    if scopes:
        spec["scopes"] = list(scopes)
    if selector:
        spec["scopeSelector"] = selector
    status = {"used": dict(used)} if used is not None else {}
    return {"metadata": {"name": name, "namespace": NAMESPACE}, "spec": spec,
            "status": status}


def limit_range(name="defaults", *, default_request=None, default=None, kind="Container"):
    item = {"type": kind}
    if default_request:
        item["defaultRequest"] = dict(default_request)
    if default:
        item["default"] = dict(default)
    return {"metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {"limits": [item]}}


def container(name="app", *, requests=None, limits=None):
    out = {"name": name}
    if requests is not None:
        out["requests"] = dict(requests)
    if limits is not None:
        out["limits"] = dict(limits)
    return out


@pytest.fixture
def cluster(monkeypatch):
    """The two listings, each independently stubbable or failable."""
    state = {"quotas": [], "limit_ranges": [], "quotas_fail": None, "lr_fail": None}

    class Api:
        def list_namespaced_resource_quota(self, namespace):
            if state["quotas_fail"]:
                raise state["quotas_fail"]
            return obj(items=list(state["quotas"]))

        def list_namespaced_limit_range(self, namespace):
            if state["lr_fail"]:
                raise state["lr_fail"]
            return obj(items=list(state["limit_ranges"]))

    monkeypatch.setattr(quota, "get_core_v1", lambda: Api())
    return state


def codes(entries):
    return [entry["code"] for entry in entries]


def check_for(preview, resource):
    return next(c for c in preview["checks"] if c["resource"] == resource)


# --------------------------------------------------------------------------- #
# The mandatory-resource trap
# --------------------------------------------------------------------------- #

def test_a_quota_makes_its_compute_resources_compulsory(cluster):
    """The finding this module exists for. A quota bounding `requests.cpu` means
    every container must state it — the pod is refused when it does not, even
    with the quota barely used, and the message names the field rather than the
    LimitRange that would supply it."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]

    result = quota.advise(NAMESPACE)

    assert result["mandatory"] == ["requests.cpu"]
    assert quota.QUOTA_REQUIRES_UNSET_RESOURCE in codes(result["findings"])
    (finding,) = result["findings"]
    assert "must specify" in finding["detail"]
    assert "LimitRange" in finding["detail"]


def test_cpu_and_requests_cpu_are_the_same_bound(cluster):
    """`cpu` is the API's alias for `requests.cpu`. Treating them as two keys
    would report a namespace as bounding two things when it bounds one."""
    cluster["quotas"] = [resource_quota(hard={"cpu": "10"}, used={"cpu": "1"})]

    assert quota.advise(NAMESPACE)["mandatory"] == ["requests.cpu"]


def test_a_limitrange_default_discharges_the_requirement(cluster):
    """The fix, and the reason the finding names it."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]
    cluster["limit_ranges"] = [limit_range(default_request={"cpu": "100m"})]

    result = quota.advise(NAMESPACE)

    assert result["containerDefaults"] == {"requests.cpu": "100m"}
    assert result["findings"] == []


def test_a_pod_scoped_limitrange_defaults_nothing(cluster):
    """Only `Container` items inject values. A `Pod` item bounds the total and
    defaults nothing, so it must not be read as discharging the requirement."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]
    cluster["limit_ranges"] = [limit_range(default_request={"cpu": "100m"}, kind="Pod")]

    assert quota.QUOTA_REQUIRES_UNSET_RESOURCE in codes(quota.advise(NAMESPACE)["findings"])


def test_object_count_quotas_make_nothing_compulsory(cluster):
    """`pods` and `count/deployments.apps` bound how many, not what each states."""
    cluster["quotas"] = [resource_quota(
        hard={"pods": "50", "count/deployments.apps": "10"},
        used={"pods": "12", "count/deployments.apps": "3"},
    )]

    result = quota.advise(NAMESPACE)

    assert result["mandatory"] == []
    assert result["findings"] == []


# --------------------------------------------------------------------------- #
# The preview arithmetic
# --------------------------------------------------------------------------- #

def test_it_names_which_limit_refuses_the_workload(cluster):
    """The sentence a 403 does not carry: not "refused" but *which* of a quota's
    limits is the tight one, and by how much."""
    cluster["quotas"] = [resource_quota(
        hard={"requests.cpu": "10", "requests.memory": "20Gi", "pods": "50"},
        used={"requests.cpu": "9", "requests.memory": "4Gi", "pods": "12"},
    )]

    preview = quota.advise(NAMESPACE, {
        "replicas": 3,
        "containers": [container(requests={"cpu": "500m", "memory": "1Gi"})],
    })["preview"]

    assert preview["verdict"] == quota.REFUSED
    assert check_for(preview, "requests.cpu")["verdict"] == quota.REFUSED
    assert check_for(preview, "requests.cpu")["headroom"] == "1"
    assert check_for(preview, "requests.cpu")["needed"] == "1.500"
    # And the ones with room say so, so the operator knows what to change.
    assert check_for(preview, "requests.memory")["verdict"] == quota.ADMITTED
    assert check_for(preview, "pods")["verdict"] == quota.ADMITTED


def test_replicas_multiply_and_containers_sum(cluster):
    """A sidecar nobody counted is the usual reason an estimate and an admission
    decision disagree."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "0"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 2,
        "containers": [
            container("app", requests={"cpu": "500m"}),
            container("sidecar", requests={"cpu": "250m"}),
        ],
    })["preview"]

    assert preview["needed"]["requests.cpu"] == "1.500"
    assert preview["verdict"] == quota.ADMITTED


def test_a_workload_that_fits_is_admitted(cluster):
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 1, "containers": [container(requests={"cpu": "500m"})],
    })["preview"]

    assert preview["verdict"] == quota.ADMITTED


def test_a_namespace_with_no_quota_admits_anything(cluster):
    """A real answer, and the reason a refused listing must never look like it."""
    preview = quota.advise(NAMESPACE, {
        "replicas": 500, "containers": [container(requests={"cpu": "64"})],
    })["preview"]

    assert preview["verdict"] == quota.ADMITTED
    assert quota.advise(NAMESPACE)["quotas"] == []


def test_the_pods_count_is_checked_against_replicas(cluster):
    cluster["quotas"] = [resource_quota(hard={"pods": "10"}, used={"pods": "9"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 3, "containers": [container()],
    })["preview"]

    assert check_for(preview, "pods")["verdict"] == quota.REFUSED
    assert preview["verdict"] == quota.REFUSED


def test_an_object_count_the_request_does_not_describe_is_skipped(cluster):
    """Reporting a verdict for `count/deployments.apps` from a body that does not
    say how many it creates would be arithmetic over an invented number."""
    cluster["quotas"] = [resource_quota(hard={"count/deployments.apps": "1"},
                                        used={"count/deployments.apps": "1"})]

    preview = quota.advise(NAMESPACE, {"replicas": 1, "containers": [container()]})["preview"]

    assert preview["checks"] == []
    assert preview["verdict"] == quota.ADMITTED


# --------------------------------------------------------------------------- #
# "must specify" is a different refusal from "no room"
# --------------------------------------------------------------------------- #

def test_a_container_omitting_a_mandatory_resource_is_refused_with_room_to_spare(cluster):
    """The quota is 1% used and the pod is still refused. Listing this under the
    headroom checks would send the operator to raise a limit that is fine."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "100"},
                                        used={"requests.cpu": "1"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 1, "containers": [container("app")],
    })["preview"]

    assert preview["verdict"] == quota.REFUSED
    assert preview["unsetMandatory"] == [
        {"container": "app", "resource": "requests.cpu"},
    ]
    # The headroom check for that resource is not what refused it.
    assert check_for(preview, "requests.cpu")["verdict"] == quota.ADMITTED


def test_a_declared_zero_is_a_value_not_an_omission(cluster):
    """`requests.cpu: "0"` satisfies the rule and consumes no headroom.
    Collapsing it into "unset" would report a refusal the API server would not
    make."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 1, "containers": [container(requests={"cpu": "0"})],
    })["preview"]

    assert preview["unsetMandatory"] == []
    assert preview["verdict"] == quota.ADMITTED


def test_an_empty_string_is_an_omission_not_a_value(cluster):
    """A blank form field arrives as `""`, and it is not a declared quantity.
    Treating it as one discharges the compulsory rule with nothing behind it and
    reports a workload as admitted that the API server refuses."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 1, "containers": [container(requests={"cpu": ""})],
    })["preview"]

    assert preview["unsetMandatory"] == [
        {"container": "app", "resource": "requests.cpu"},
    ]
    assert preview["verdict"] == quota.REFUSED


def test_a_default_rescues_the_container_and_is_counted(cluster):
    """The LimitRange value is injected at admission, so it consumes headroom
    like a declared one — an advisor that discharged the requirement without
    counting the value would under-report demand."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "0"})]
    cluster["limit_ranges"] = [limit_range(default_request={"cpu": "100m"})]

    preview = quota.advise(NAMESPACE, {
        "replicas": 2, "containers": [container("app")],
    })["preview"]

    assert preview["unsetMandatory"] == []
    assert preview["needed"]["requests.cpu"] == "0.200"


def test_limits_are_defaulted_from_default_not_default_request(cluster):
    """`default` supplies limits and `defaultRequest` supplies requests. Reading
    one for the other discharges a requirement nothing actually satisfies."""
    cluster["quotas"] = [resource_quota(hard={"limits.memory": "10Gi"},
                                        used={"limits.memory": "0"})]
    cluster["limit_ranges"] = [limit_range(default_request={"memory": "1Gi"})]

    result = quota.advise(NAMESPACE, {"replicas": 1, "containers": [container()]})

    assert quota.QUOTA_REQUIRES_UNSET_RESOURCE in codes(result["findings"])
    assert result["preview"]["unsetMandatory"] == [
        {"container": "app", "resource": "limits.memory"},
    ]


# --------------------------------------------------------------------------- #
# Unknown is a real answer
# --------------------------------------------------------------------------- #

def test_an_unwritten_used_makes_the_verdict_unknown_not_admitted(cluster):
    """The quota controller writes status.used asynchronously. Reading absence as
    zero gives the roomiest possible answer at the moment this console knows
    least — to the person about to deploy."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"})]
    cluster["limit_ranges"] = [limit_range(default_request={"cpu": "100m"})]

    result = quota.advise(NAMESPACE, {"replicas": 1, "containers": [container()]})

    assert result["preview"]["verdict"] == quota.UNKNOWN
    assert check_for(result["preview"], "requests.cpu")["headroom"] is None
    assert quota.QUOTA_USAGE_UNKNOWN in codes(result["quotas"][0]["findings"])


def test_a_scoped_quota_withholds_its_verdict(cluster):
    """Which pods it counts depends on the priority class or terminating state of
    an object that does not exist yet. Including it invents a refusal; excluding
    it hides one."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "9"},
                                        scopes=["NotTerminating"])]
    cluster["limit_ranges"] = [limit_range(default_request={"cpu": "100m"})]

    result = quota.advise(NAMESPACE, {"replicas": 1, "containers": [container()]})

    assert result["quotas"][0]["applies"] is None
    assert quota.QUOTA_SCOPED in codes(result["quotas"][0]["findings"])
    assert result["preview"]["verdict"] == quota.UNKNOWN


def test_a_scope_selector_counts_as_scoped(cluster):
    cluster["quotas"] = [resource_quota(
        hard={"pods": "10"}, used={"pods": "1"},
        selector={"matchExpressions": [{"operator": "In", "scopeName": "PriorityClass",
                                        "values": ["high"]}]},
    )]

    assert quota.advise(NAMESPACE)["quotas"][0]["applies"] is None


def test_an_unscoped_quota_applies(cluster):
    """The common case, and it must stay decidable or every verdict is unknown."""
    cluster["quotas"] = [resource_quota(hard={"pods": "10"}, used={"pods": "1"})]

    assert quota.advise(NAMESPACE)["quotas"][0]["applies"] is True


def test_a_refusal_beats_an_unknown(cluster):
    """A quota that definitely refuses is the answer, whatever a second quota
    could not say."""
    cluster["quotas"] = [
        resource_quota("full", hard={"pods": "1"}, used={"pods": "1"}),
        resource_quota("fresh", hard={"pods": "100"}),
    ]

    preview = quota.advise(NAMESPACE, {"replicas": 1, "containers": [container()]})["preview"]

    assert preview["verdict"] == quota.REFUSED


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #

def test_a_refused_quota_listing_is_null_never_an_empty_list(cluster):
    """`[]` is the answer that says nothing bounds this namespace and every
    workload is admitted."""
    cluster["quotas_fail"] = ApiException(status=403, reason="Forbidden")

    result = quota.advise(NAMESPACE)

    assert result["quotas"] is None
    assert result["mandatory"] is None
    assert result["partial"] is True
    assert any(e["resource"] == "resourcequotas" for e in result["unavailable"])


def test_an_unreadable_limitrange_listing_withholds_the_verdict(cluster):
    """Without the defaults, every "must specify" conclusion would be wrong in
    the dangerous direction: reporting a workload as refused when a default the
    console could not read would have supplied the value."""
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]
    cluster["lr_fail"] = ApiException(status=403, reason="Forbidden")

    result = quota.advise(NAMESPACE, {"replicas": 1, "containers": [container()]})

    assert result["preview"]["verdict"] == quota.UNKNOWN
    assert result["findings"] == [], "no mandatory finding from half the picture"
    assert result["partial"] is True


def test_an_exhausted_quota_says_so_on_its_own_row(cluster):
    cluster["quotas"] = [resource_quota(hard={"pods": "10"}, used={"pods": "10"})]

    assert quota.QUOTA_EXHAUSTED in codes(quota.advise(NAMESPACE)["quotas"][0]["findings"])


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

def test_the_advice_endpoint_returns_the_namespace_shape(client, cluster_id, cluster):
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "1"})]

    response = client.get(f"/api/quota/{NAMESPACE}", params={"cluster_id": cluster_id})

    assert response.status_code == 200
    body = response.json()
    assert body["mandatory"] == ["requests.cpu"]
    assert quota.QUOTA_REQUIRES_UNSET_RESOURCE in codes(body["findings"])


def test_the_preview_endpoint_names_the_tight_limit(client, cluster_id, cluster):
    cluster["quotas"] = [resource_quota(hard={"requests.cpu": "10"},
                                        used={"requests.cpu": "9"})]
    cluster["limit_ranges"] = [limit_range(default_request={"cpu": "100m"})]

    response = client.post(
        f"/api/quota/{NAMESPACE}/preview",
        params={"cluster_id": cluster_id},
        json={"replicas": 20, "containers": [{"name": "app"}]},
    )

    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["verdict"] == "refused"
    assert preview["needed"]["requests.cpu"] == "2.000"


def test_the_preview_defaults_to_one_replica(client, cluster_id, cluster):
    cluster["quotas"] = [resource_quota(hard={"pods": "10"}, used={"pods": "1"})]

    response = client.post(
        f"/api/quota/{NAMESPACE}/preview",
        params={"cluster_id": cluster_id},
        json={"containers": [{"name": "app"}]},
    )

    assert response.json()["preview"]["replicas"] == 1
