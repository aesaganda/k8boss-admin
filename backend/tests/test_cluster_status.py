"""
Cluster status (§19) — five reads joined, and what the page says when one fails.

The findings themselves are arithmetic on fields the API server wrote, so most
of what is worth asserting is the honesty around them:

**A section that could not be read is `null`, never empty.** "This cluster has
no admission webhooks" and "we could not read the admission webhooks" are
answers an operator acts on very differently, and only the first is good news.

**A missing lease is never a missing component.** Managed control planes run the
scheduler somewhere the customer cannot see, and `kube-system` holds no lease
for it. A page reading "kube-scheduler: MISSING" on a healthy EKS cluster is the
confident wrong answer this project treats as a defect.

**A URL-addressed webhook has no endpoint count, and that is not a zero.** There
is nothing in the cluster to count. Zero would read as "its backend is gone",
which is the one finding this section exists to make.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.resources import catalog
from app.services import cluster_status
from app.errors import RBACDenied, Unsupported
from tests.conftest import obj

#: A timestamp for fields whose *value* is not what a test is about — a
#: condition's transition time, say.
NOW = "2026-09-03T12:00:00Z"


def just_now() -> str:
    """An RFC 3339 stamp of this moment.

    `stale` is computed against the clock, so a lease that is meant to be fresh
    has to be fresh when the test runs. A fixed literal here would pass on the
    afternoon it was written and start failing the next day, which is the kind
    of test that gets deleted rather than fixed.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# A stubbed cluster
# --------------------------------------------------------------------------- #

def lease(name, *, holder="host-a", renew="now", duration=15):
    renew = just_now() if renew == "now" else renew
    return {
        "metadata": {"name": name, "namespace": "kube-system"},
        "spec": {
            "holderIdentity": holder,
            "renewTime": renew,
            "leaseDurationSeconds": duration,
        },
    }


def api_service(name, *, group, version, service=None, available="True", reason=None):
    return {
        "metadata": {"name": name},
        "spec": {"group": group, "version": version, "service": service},
        "status": {"conditions": [{
            "type": "Available", "status": available, "reason": reason,
            "message": None if available == "True" else "no response from the backend",
            "lastTransitionTime": NOW,
        }]},
    }


def crd(name, *, group="example.io", established="True", non_structural=None):
    conditions = [{
        "type": "Established", "status": established, "reason": "InitialNamesAccepted",
        "message": None, "lastTransitionTime": NOW,
    }]
    if non_structural is not None:
        conditions.append({
            "type": "NonStructuralSchema", "status": non_structural,
            "reason": "Violations", "message": "spec.foo: must not have x-kubernetes",
            "lastTransitionTime": NOW,
        })
    return {"metadata": {"name": name}, "spec": {"group": group},
            "status": {"conditions": conditions}}


def webhook_config(name, *, hooks):
    return {"metadata": {"name": name}, "webhooks": hooks}


def hook(name, *, failure="Fail", service=None, url=None, timeout=10):
    client_config = {"service": service} if service else {"url": url}
    return {
        "name": name, "failurePolicy": failure, "timeoutSeconds": timeout,
        "sideEffects": "None", "clientConfig": client_config,
    }


def endpoint_slice(namespace, service, *, ready=1):
    return {
        "metadata": {
            "namespace": namespace,
            "labels": {"kubernetes.io/service-name": service},
        },
        "endpoints": [
            {"addresses": [f"10.0.0.{i}"], "conditions": {"ready": True}}
            for i in range(ready)
        ],
    }


def default_listings():
    """Fresh every time: the lease's `renewTime` has to be recent *now*."""
    return {
        ("coordination.k8s.io", "leases"): [lease("kube-scheduler")],
        ("apiregistration.k8s.io", "apiservices"): [
            api_service("v1.", group="", version="v1"),
        ],
        ("apiextensions.k8s.io", "customresourcedefinitions"): [
            crd("widgets.example.io"),
        ],
        ("admissionregistration.k8s.io", "validatingwebhookconfigurations"): [],
        ("admissionregistration.k8s.io", "mutatingwebhookconfigurations"): [],
        ("discovery.k8s.io", "endpointslices"): [],
    }


@pytest.fixture
def cluster(monkeypatch, fake_k8s):
    """Discovery serving every §19 group, and listings the test can override."""
    served = {
        ("coordination.k8s.io", "v1"): [("leases", "Lease", True)],
        ("apiregistration.k8s.io", "v1"): [("apiservices", "APIService", False)],
        ("apiextensions.k8s.io", "v1"): [
            ("customresourcedefinitions", "CustomResourceDefinition", False),
        ],
        ("admissionregistration.k8s.io", "v1"): [
            ("validatingwebhookconfigurations", "ValidatingWebhookConfiguration", False),
            ("mutatingwebhookconfigurations", "MutatingWebhookConfiguration", False),
        ],
        ("discovery.k8s.io", "v1"): [("endpointslices", "EndpointSlice", True)],
    }

    state = {"listings": default_listings(), "missing_groups": set(),
             "list_errors": {}}

    def fake_raw_get(path, *, query=None):
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/api/v1":
            return {"resources": []}
        if path == "/apis":
            return {"groups": [
                {"name": group, "versions": [{"groupVersion": f"{group}/{version}",
                                              "version": version}],
                 "preferredVersion": {"groupVersion": f"{group}/{version}",
                                      "version": version}}
                for (group, version) in served
                if group not in state["missing_groups"]
            ]}
        for (group, version), resources in served.items():
            if path == f"/apis/{group}/{version}":
                if group in state["missing_groups"]:
                    raise AssertionError("discovery asked for a group it does not serve")
                return {"resources": [
                    {"name": name, "kind": kind, "namespaced": namespaced,
                     "verbs": ["get", "list"]}
                    for name, kind, namespaced in resources
                ]}
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    def fake_list_resource(group, version, plural, *, namespace=None, **kwargs):
        key = (group, plural)
        if key in state["list_errors"]:
            raise state["list_errors"][key]
        return {"items": state["listings"].get(key, [])}

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    monkeypatch.setattr(cluster_status.reader, "list_resource", fake_list_resource)
    fake_k8s.version_api.returns("get_code", obj(git_version="v1.31.4"))
    fake_k8s.core_v1.returns("list_node", obj(items=[
        obj(metadata=obj(name="node-a"),
            status=obj(node_info=obj(kubelet_version="v1.31.4"))),
    ]))
    return state


def status(**_):
    return cluster_status.get_cluster_status()


# --------------------------------------------------------------------------- #
# Leader-election leases
# --------------------------------------------------------------------------- #

def test_a_lease_renewed_within_its_duration_is_not_stale(cluster, db_engine):
    body = status()

    (row,) = body["controlPlane"]
    assert row["name"] == "kube-scheduler"
    assert row["holder"] == "host-a"
    assert row["stale"] is False


def test_a_lease_whose_holder_stopped_renewing_is_stale(cluster, db_engine):
    """What a wedged controller-manager looks like from the API server's side."""
    cluster["listings"][("coordination.k8s.io", "leases")] = [
        lease("kube-controller-manager", renew="2020-01-01T00:00:00Z", duration=15),
    ]

    (row,) = status()["controlPlane"]

    assert row["stale"] is True
    assert row["seconds_since_renew"] > 15


def test_a_lease_with_no_renew_time_is_unknown_not_stale(cluster, db_engine):
    """A lease nobody has acquired has told us nothing. Calling it stale would
    report a component as wedged on the strength of a field nobody wrote."""
    cluster["listings"][("coordination.k8s.io", "leases")] = [
        lease("kube-scheduler", renew=None),
    ]

    (row,) = status()["controlPlane"]

    assert row["stale"] is None
    assert row["seconds_since_renew"] is None


def test_a_missing_well_known_lease_is_not_reported_at_all(cluster, db_engine):
    """Managed control planes run the scheduler where the customer cannot see it.

    "kube-scheduler: MISSING" on a healthy EKS cluster is the confident wrong
    answer this project exists to avoid, so the page lists what is there and
    says nothing about what is not.
    """
    cluster["listings"][("coordination.k8s.io", "leases")] = [
        lease("some-operator-lock", holder="operator-0"),
    ]

    rows = status()["controlPlane"]

    assert [row["name"] for row in rows] == ["some-operator-lock"]
    assert rows[0]["well_known"] is False


def test_a_refused_lease_listing_is_null_not_empty(cluster, db_engine):
    cluster["list_errors"][("coordination.k8s.io", "leases")] = RBACDenied("no")

    body = status()

    assert body["controlPlane"] is None, (
        "an empty list here would say the control plane holds no leases"
    )
    assert body["partial"] is True
    assert ("coordination.k8s.io", "leases", "forbidden") in [
        (e["group"], e["resource"], e["reason"]) for e in body["unavailable"]
    ]


# --------------------------------------------------------------------------- #
# Aggregated APIServices
# --------------------------------------------------------------------------- #

def test_only_aggregated_api_services_are_listed(cluster, db_engine):
    """An APIService with no `spec.service` is served by the API server itself
    and is Available on every cluster that is answering at all. Thirty rows of
    guaranteed-green is how a table stops being read."""
    cluster["listings"][("apiregistration.k8s.io", "apiservices")] = [
        api_service("v1.", group="", version="v1"),
        api_service("v1beta1.metrics.k8s.io", group="metrics.k8s.io", version="v1beta1",
                    service={"namespace": "kube-system", "name": "metrics-server"}),
    ]

    section = status()["apiServices"]

    assert [row["name"] for row in section["items"]] == ["v1beta1.metrics.k8s.io"]
    assert section["local_count"] == 1


def test_an_unavailable_aggregated_api_carries_the_reason_the_cluster_gave(
    cluster, db_engine,
):
    """The rest of the console reports metrics as `unsupported` — an ordinary
    fact — and can never say why. This is why."""
    cluster["listings"][("apiregistration.k8s.io", "apiservices")] = [
        api_service("v1beta1.metrics.k8s.io", group="metrics.k8s.io", version="v1beta1",
                    service={"namespace": "kube-system", "name": "metrics-server"},
                    available="False", reason="ServiceNotFound"),
    ]

    section = status()["apiServices"]

    assert section["unavailable_count"] == 1
    assert section["items"][0]["available"] is False
    assert section["items"][0]["reason"] == "ServiceNotFound"
    assert "no response" in section["items"][0]["message"]


def test_an_api_service_with_no_condition_yet_is_unknown_not_broken(cluster, db_engine):
    cluster["listings"][("apiregistration.k8s.io", "apiservices")] = [
        {"metadata": {"name": "v1beta1.metrics.k8s.io"},
         "spec": {"group": "metrics.k8s.io", "version": "v1beta1",
                  "service": {"namespace": "kube-system", "name": "metrics-server"}},
         "status": {}},
    ]

    section = status()["apiServices"]

    assert section["items"][0]["available"] is None
    assert section["unavailable_count"] == 0, "unknown is not counted as a failure"


# --------------------------------------------------------------------------- #
# CustomResourceDefinitions
# --------------------------------------------------------------------------- #

def test_only_unhealthy_crds_are_listed_with_the_total_beside_them(cluster, db_engine):
    """Three problems out of four hundred and three out of five are different
    mornings, so the count the finding is measured against is reported too."""
    cluster["listings"][("apiextensions.k8s.io", "customresourcedefinitions")] = [
        crd("healthy.example.io"),
        crd("broken.example.io", established="False"),
        crd("legacy.example.io", non_structural="True"),
    ]

    section = status()["crds"]

    assert section["total"] == 3
    assert section["unhealthy_count"] == 2
    assert sorted(row["name"] for row in section["items"]) == [
        "broken.example.io", "legacy.example.io",
    ]


def test_a_crd_with_no_established_condition_counts_as_unhealthy(cluster, db_engine):
    """It serves nothing until it is Established, and a controller that has
    written no condition has not established it."""
    cluster["listings"][("apiextensions.k8s.io", "customresourcedefinitions")] = [
        {"metadata": {"name": "fresh.example.io"}, "spec": {"group": "example.io"},
         "status": {}},
    ]

    section = status()["crds"]

    assert section["unhealthy_count"] == 1
    assert section["items"][0]["established"] is None


# --------------------------------------------------------------------------- #
# Admission webhooks — the finding this page is worth building for
# --------------------------------------------------------------------------- #

def test_a_failing_webhook_with_no_endpoints_is_counted_as_blocking(cluster, db_engine):
    """`failurePolicy: Fail` and nothing behind the Service means every write it
    intercepts is refused, cluster-wide, until the backend returns. It is the
    most effective way to break a cluster without touching a node."""
    cluster["listings"][("admissionregistration.k8s.io", "validatingwebhookconfigurations")] = [
        webhook_config("policy.example.io", hooks=[
            hook("deny.example.io", failure="Fail",
                 service={"namespace": "policy", "name": "webhook", "port": 443}),
        ]),
    ]
    cluster["listings"][("discovery.k8s.io", "endpointslices")] = []

    section = status()["webhooks"]

    assert section["blocking_count"] == 1
    row = section["items"][0]
    assert row["endpoint_count"] == 0
    assert row["failure_policy"] == "Fail"
    assert row["kind"] == "ValidatingWebhookConfiguration"


def test_a_webhook_with_endpoints_is_not_blocking(cluster, db_engine):
    cluster["listings"][("admissionregistration.k8s.io", "validatingwebhookconfigurations")] = [
        webhook_config("policy.example.io", hooks=[
            hook("deny.example.io",
                 service={"namespace": "policy", "name": "webhook", "port": 443}),
        ]),
    ]
    cluster["listings"][("discovery.k8s.io", "endpointslices")] = [
        endpoint_slice("policy", "webhook", ready=2),
    ]

    section = status()["webhooks"]

    assert section["blocking_count"] == 0
    assert section["items"][0]["endpoint_count"] == 2


def test_an_ignore_policy_webhook_with_no_endpoints_is_not_blocking(cluster, db_engine):
    """`failurePolicy: Ignore` means the API server admits the write when the
    webhook does not answer. Nothing is blocked; it is just not being checked."""
    cluster["listings"][("admissionregistration.k8s.io", "mutatingwebhookconfigurations")] = [
        webhook_config("inject.example.io", hooks=[
            hook("sidecar.example.io", failure="Ignore",
                 service={"namespace": "mesh", "name": "injector", "port": 443}),
        ]),
    ]

    section = status()["webhooks"]

    assert section["blocking_count"] == 0
    assert section["items"][0]["endpoint_count"] == 0


def test_a_url_addressed_webhook_has_no_endpoint_count_rather_than_zero(
    cluster, db_engine,
):
    """There is nothing in the cluster to count. A zero would read as "its
    backend is gone", which is the one finding this section exists to make."""
    cluster["listings"][("admissionregistration.k8s.io", "validatingwebhookconfigurations")] = [
        webhook_config("external.example.io", hooks=[
            hook("remote.example.io", url="https://policy.example.com/validate"),
        ]),
    ]

    section = status()["webhooks"]

    row = section["items"][0]
    assert row["service"] is None
    assert row["url"] == "https://policy.example.com/validate"
    assert row["endpoint_count"] is None
    assert section["blocking_count"] == 0


def test_an_endpoint_listing_that_failed_leaves_every_count_unknown(
    cluster, db_engine,
):
    """§0.1's corollary, twice over.

    A tally that could not be made must not become a zero for every row — which
    would report every webhook in the cluster as blocking. And the section's own
    `blocking_count` must not become 0 either: with every `endpoint_count` at
    None nothing matches the predicate, so the naive count reads "no webhook is
    refusing writes" off a listing that never answered. That is the single
    finding this section exists to make, delivered backwards."""
    cluster["listings"][("admissionregistration.k8s.io", "validatingwebhookconfigurations")] = [
        webhook_config("policy.example.io", hooks=[
            hook("deny.example.io",
                 service={"namespace": "policy", "name": "webhook", "port": 443}),
        ]),
    ]
    cluster["list_errors"][("discovery.k8s.io", "endpointslices")] = RBACDenied("no")

    body = status()

    assert body["webhooks"]["items"][0]["endpoint_count"] is None
    assert body["webhooks"]["blocking_count"] is None
    # The rows themselves are still real and still worth showing: the
    # configurations answered, only the endpoint tally did not.
    assert len(body["webhooks"]["items"]) == 1
    assert body["partial"] is True


def test_one_webhook_kind_failing_marks_the_section_incomplete(cluster, db_engine):
    """The rows that answered are still worth showing; a caller must be able to
    tell they are not all of them."""
    cluster["list_errors"][
        ("admissionregistration.k8s.io", "mutatingwebhookconfigurations")
    ] = RBACDenied("no")

    section = status()["webhooks"]

    assert section["complete"] is False
    assert section is not None


def test_both_webhook_kinds_failing_makes_the_section_null(cluster, db_engine):
    for plural in ("validatingwebhookconfigurations", "mutatingwebhookconfigurations"):
        cluster["list_errors"][("admissionregistration.k8s.io", plural)] = RBACDenied("no")

    body = status()

    assert body["webhooks"] is None
    assert body["partial"] is True


# --------------------------------------------------------------------------- #
# Version skew
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("server", "kubelet", "expected"),
    [
        ("v1.31.4", "v1.31.4", "ok"),
        ("v1.31.4", "v1.30.9", "ok"),
        ("v1.31.4", "v1.28.0", "ok"),
        ("v1.31.4", "v1.27.9", "behind"),
        ("v1.31.4", "v1.32.0", "ahead"),
        ("v1.31.4", "v2.0.0", "ahead"),
        ("v1.31.4", None, "unknown"),
        (None, "v1.31.4", "unknown"),
        ("v1.31.4", "not-a-version", "unknown"),
    ],
)
def test_the_skew_verdict_for_one_node(server, kubelet, expected):
    """n-3 is the documented policy since 1.28, and a kubelet ahead of the API
    server is unsupported at any distance. A version that will not parse is
    `unknown`, because a rule applied to a number we do not have produces a
    verdict about a node nobody checked."""
    assert cluster_status._skew_status(
        cluster_status._minor(server), cluster_status._minor(kubelet)
    ) == expected


def test_a_node_outside_the_skew_window_is_counted_and_sorted_first(
    cluster, db_engine, fake_k8s,
):
    fake_k8s.core_v1.returns("list_node", obj(items=[
        obj(metadata=obj(name="new"), status=obj(node_info=obj(kubelet_version="v1.31.0"))),
        obj(metadata=obj(name="ancient"),
            status=obj(node_info=obj(kubelet_version="v1.26.0"))),
    ]))

    section = status()["versionSkew"]

    assert section["server_version"] == "v1.31.4"
    assert section["out_of_skew_count"] == 1
    assert section["nodes"][0]["node"] == "ancient"
    assert section["nodes"][0]["status"] == "behind"


def test_a_refused_node_listing_leaves_the_section_without_nodes(
    cluster, db_engine, fake_k8s,
):
    fake_k8s.core_v1.raises("list_node", RBACDenied("nodes is forbidden"))

    body = status()

    assert body["versionSkew"]["nodes"] is None
    # Not 0. "No node is outside the supported skew" is a claim about nodes
    # nobody looked at, and it is the reassuring half of the pair.
    assert body["versionSkew"]["out_of_skew_count"] is None
    # The API server's own version still answered and is still worth showing,
    # so the section stays rather than disappearing entirely.
    assert body["versionSkew"]["server_version"] == "v1.31.4"
    assert body["partial"] is True


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_a_group_the_cluster_does_not_serve_is_unsupported_not_an_error(
    cluster, db_engine,
):
    """`apiregistration.k8s.io` is on nearly every cluster and not quite all of
    them, and its absence is a fact about the cluster rather than a failure."""
    cluster["list_errors"][("apiregistration.k8s.io", "apiservices")] = Unsupported(
        "this cluster does not serve apiservices"
    )

    body = status()

    assert body["apiServices"] is None
    assert ("apiregistration.k8s.io", "apiservices", "unsupported") in [
        (e["group"], e["resource"], e["reason"]) for e in body["unavailable"]
    ]


def test_one_failing_section_costs_only_itself(cluster, db_engine):
    cluster["list_errors"][("apiextensions.k8s.io", "customresourcedefinitions")] = (
        RBACDenied("no")
    )

    body = status()

    assert body["crds"] is None
    assert body["controlPlane"] is not None
    assert body["apiServices"] is not None
    assert body["webhooks"] is not None
    assert body["versionSkew"] is not None


def test_the_page_reports_no_aggregate_verdict(cluster, db_engine):
    """Deliberate. A stale cloud-controller-manager lease and no aggregated APIs
    is fine on some clusters and an outage on others; one boolean would have to
    pick, and picking is the operator's job."""
    body = status()

    assert "healthy" not in body
    assert "status" not in body


def test_the_endpoint_answers_through_the_api(client, cluster):
    response = client.get("/api/cluster-status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "controlPlane", "apiServices", "crds", "webhooks", "versionSkew",
        "partial", "unavailable",
    }
    assert body["partial"] is False
