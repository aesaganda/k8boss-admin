"""
Deleting a namespace, and what goes with it (§26).

`kubectl delete namespace prod` prints one line and takes everything inside with
it. §4's delete previews `before=live, after=null`, which is the *namespace
object's* YAML disappearing — a preview whose honesty is exactly the problem,
because what actually disappears is not in it.

Every test here is about a pair that a convenient implementation collapses, and
on this endpoint collapsing one destroys somebody's data:

  `Delete` vs `Retain`         Opposite outcomes for the same claim, decided on
                               a cluster-scoped object nobody is looking at. A
                               volume whose policy this console could not read
                               is **unknown**, and unknown is neither.

  `count: 0` vs `count: null`  §0.1's corollary at its sharpest. "This namespace
                               holds no PersistentVolumeClaims" is the sentence
                               that ends with a deleted database, and a listing
                               that was refused must never produce it.

  `[]` vs `None` for webhooks  "No admission webhook is served from here" versus
                               "we could not look". A `ValidatingWebhookConfig`
                               with `failurePolicy: Fail` and no backend refuses
                               every write it intercepts, cluster-wide.

  applied vs gone              `applied: true` means a deletionTimestamp exists.
                               A finalizer whose controller is not running holds
                               the namespace in `Terminating` forever, and this
                               module's own plan is the diagnosis of why.

  plan-time vs write-time      Consequences are recomputed at the write. A
                               volume provisioned between reading the plan and
                               confirming it is not covered by the tick the
                               operator gave the old one.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.admin import namespace_delete as nsd
from app.audit import recorder
from app.errors import Invalid, MutationsDisabled, NotFound, RBACDenied
from app.resources import catalog
from tests.conftest import obj

NAMESPACE = "prod"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def api_resource(resource, *, group="", version="v1", kind=None, namespaced=True,
                 verbs=("get", "list", "delete"), preferred=True):
    return {
        "group": group,
        "version": version,
        "kind": kind or resource[:-1].title(),
        "resource": resource,
        "namespaced": namespaced,
        "verbs": list(verbs),
        "shortNames": [],
        "categories": [],
        "apiVersion": version if not group else f"{group}/{version}",
        "preferred": preferred,
    }


DEFAULT_CATALOG = [
    api_resource("namespaces", namespaced=False, kind="Namespace"),
    api_resource("persistentvolumes", namespaced=False, kind="PersistentVolume"),
    api_resource("pods", kind="Pod"),
    api_resource("services", kind="Service"),
    api_resource("persistentvolumeclaims", kind="PersistentVolumeClaim"),
    api_resource("validatingwebhookconfigurations",
                 group="admissionregistration.k8s.io", namespaced=False,
                 kind="ValidatingWebhookConfiguration"),
    api_resource("mutatingwebhookconfigurations",
                 group="admissionregistration.k8s.io", namespaced=False,
                 kind="MutatingWebhookConfiguration"),
]


# --------------------------------------------------------------------------- #
# Object builders — plain dicts, because that is what `raw_get` returns
# --------------------------------------------------------------------------- #

def namespace(name=NAMESPACE, *, phase="Active", finalizers=("kubernetes",),
              version="4210", deletion_timestamp=None):
    metadata = {"name": name, "resourceVersion": version}
    if deletion_timestamp:
        metadata["deletionTimestamp"] = deletion_timestamp
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": metadata,
        "spec": {"finalizers": list(finalizers)},
        "status": {"phase": phase},
    }


def claim(name, *, volume="pv-1", phase="Bound", capacity="200Gi",
          storage_class="fast", finalizers=()):
    metadata = {"name": name, "namespace": NAMESPACE}
    if finalizers:
        metadata["finalizers"] = list(finalizers)
    spec = {"storageClassName": storage_class}
    if volume:
        spec["volumeName"] = volume
    return {
        "kind": "PersistentVolumeClaim",
        "metadata": metadata,
        "spec": spec,
        "status": {"phase": phase, "capacity": {"storage": capacity}},
    }


def volume(name="pv-1", *, reclaim="Delete"):
    return {
        "kind": "PersistentVolume",
        "metadata": {"name": name},
        "spec": {"persistentVolumeReclaimPolicy": reclaim},
    }


def service(name, *, kind="LoadBalancer", addresses=(), finalizers=()):
    metadata = {"name": name, "namespace": NAMESPACE}
    if finalizers:
        metadata["finalizers"] = list(finalizers)
    return {
        "kind": "Service",
        "metadata": metadata,
        "spec": {"type": kind},
        "status": {
            "loadBalancer": {
                "ingress": [
                    ({"ip": value} if value[0].isdigit() else {"hostname": value})
                    for value in addresses
                ],
            },
        },
    }


def pod(name, *, finalizers=()):
    metadata = {"name": name, "namespace": NAMESPACE}
    if finalizers:
        metadata["finalizers"] = list(finalizers)
    return {"kind": "Pod", "metadata": metadata, "spec": {}, "status": {"phase": "Running"}}


def webhook_configuration(name, *, kind="ValidatingWebhookConfiguration",
                          service_namespace=NAMESPACE, service_name="admission",
                          failure_policy="Fail", webhook="policy.example.com"):
    hook = {
        "name": webhook,
        "clientConfig": {"service": {"namespace": service_namespace, "name": service_name}},
    }
    if failure_policy is not None:
        hook["failurePolicy"] = failure_policy
    return {"kind": kind, "metadata": {"name": name}, "webhooks": [hook]}


def listing(items, *, cont=None, remaining=None):
    metadata = {}
    if cont:
        metadata["continue"] = cont
    if remaining is not None:
        metadata["remainingItemCount"] = remaining
    return {"items": list(items), "metadata": metadata}


# --------------------------------------------------------------------------- #
# The fake API server
# --------------------------------------------------------------------------- #

def api_exception(status=403, reason="Forbidden"):
    return ApiException(status=status, reason=reason)


class FakeServer:
    """Routes by REST path. An unrouted path raises, like every fake here."""

    def __init__(self):
        self.payloads: dict[str, object] = {}
        self.failures: dict[str, BaseException] = {}
        self.requests: list[tuple[str, str, dict]] = []

    def at(self, path, payload):
        self.payloads[path] = payload
        return self

    def fails(self, path, error=None):
        self.failures[path] = error or api_exception()
        return self

    def __call__(self, path, method, **kwargs):
        query = dict(kwargs.get("query_params") or [])
        self.requests.append((method, path, query))
        if method == "DELETE":
            payload = {"kind": "Status", "status": "Success"}
        else:
            if path in self.failures:
                raise self.failures[path]
            if path not in self.payloads:
                raise AssertionError(f"{method} {path} was requested but not stubbed.")
            payload = self.payloads[path]
            if isinstance(payload, BaseException):
                raise payload
        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, {}

    def of(self, method):
        return [(path, query) for verb, path, query in self.requests if verb == method]


def core(plural, *, name=None, namespaced=True):
    parts = ["/api/v1"]
    if namespaced:
        parts.append(f"namespaces/{NAMESPACE}")
    parts.append(plural)
    if name:
        parts.append(name)
    return "/".join(parts)


ADMISSION = "/apis/admissionregistration.k8s.io/v1"
VALIDATING = f"{ADMISSION}/validatingwebhookconfigurations"
MUTATING = f"{ADMISSION}/mutatingwebhookconfigurations"


@pytest.fixture
def cluster(monkeypatch, fake_k8s):
    """A cluster whose discovery is fixed and whose REST calls route by path.

    Discovery is stubbed rather than exercised — `tests/test_catalog.py` owns
    that — but it is stubbed *through* `catalog.discover`, so `catalog.resolve`
    still runs for real and a test that lists a kind discovery does not serve
    fails the way production would.
    """
    catalog_items: list[dict] = list(DEFAULT_CATALOG)
    gaps: list[dict] = []

    def discover():
        # Sorted the way the real `discover()` sorts, so a test cannot pass on an
        # ordering the cluster would never produce.
        items = sorted(
            (dict(item) for item in catalog_items),
            key=lambda item: (item["group"], item["resource"], item["version"]),
        )
        return items, [dict(gap) for gap in gaps]

    monkeypatch.setattr(catalog, "discover", discover)
    server = FakeServer()
    fake_k8s.api_client.returns("call_api", server)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    server.catalog = catalog_items
    server.gaps = gaps
    return server


@pytest.fixture
def quiet(cluster):
    """A namespace holding nothing anybody has to be warned about."""
    cluster.at(core("namespaces", name=NAMESPACE, namespaced=False), namespace())
    cluster.at(core("pods"), listing([]))
    cluster.at(core("services"), listing([]))
    cluster.at(core("persistentvolumeclaims"), listing([]))
    cluster.at(VALIDATING, listing([]))
    cluster.at(MUTATING, listing([]))
    return cluster


@pytest.fixture
def loaded(cluster):
    """The namespace this module exists for: a database, an address, a webhook."""
    cluster.at(core("namespaces", name=NAMESPACE, namespaced=False), namespace())
    cluster.at(core("pods"), listing([pod("api-0")]))
    cluster.at(core("services"), listing([service("edge", addresses=["203.0.113.9"])]))
    cluster.at(core("persistentvolumeclaims"), listing([claim("postgres-data")]))
    cluster.at(core("persistentvolumes", name="pv-1", namespaced=False), volume(reclaim="Delete"))
    cluster.at(VALIDATING, listing([webhook_configuration("policy")]))
    cluster.at(MUTATING, listing([]))
    return cluster


def codes(report):
    return [entry["code"] for entry in report["consequences"]]


def kind_row(report, resource):
    return next(row for row in report["inventory"]["kinds"] if row["resource"] == resource)


def audit_rows():
    return recorder.query(limit=50)["items"]


# --------------------------------------------------------------------------- #
# The inventory is driven by discovery, not by a curated list
# --------------------------------------------------------------------------- #

def test_a_custom_resource_the_cluster_serves_is_in_the_inventory(quiet):
    """A curated kind list is a completeness claim, and the namespace whose
    contents matter is the one full of an operator's custom resources."""
    quiet.catalog.append(api_resource("kafkas", group="kafka.strimzi.io", version="v1beta2",
                                      kind="Kafka"))
    quiet.at("/apis/kafka.strimzi.io/v1beta2/namespaces/prod/kafkas",
             listing([{"kind": "Kafka", "metadata": {"name": "events"}}]))

    report = nsd.plan(NAMESPACE)

    assert kind_row(report, "kafkas")["count"] == 1


def test_cluster_scoped_kinds_are_not_listed_in_a_namespace(quiet):
    """Nodes are not "in" prod, and a namespace claiming to hold them would be
    an inventory nobody could trust for the kinds that matter."""
    report = nsd.plan(NAMESPACE)

    assert [row["resource"] for row in report["inventory"]["kinds"]] == [
        "persistentvolumeclaims", "pods", "services",
    ]


def test_a_kind_that_cannot_be_listed_is_not_asked_for(quiet):
    """`selfsubjectaccessreviews` is namespaced and advertises only `create`.
    Listing it is a 405 the operator would read as a broken console."""
    quiet.catalog.append(api_resource("selfsubjectaccessreviews",
                                      group="authorization.k8s.io", verbs=("create",)))

    report = nsd.plan(NAMESPACE)

    assert "selfsubjectaccessreviews" not in [
        row["resource"] for row in report["inventory"]["kinds"]
    ]


def test_a_crd_serving_two_versions_is_read_once_at_the_preferred_one(quiet):
    """`v1beta1` and `v2` are the same objects. Listing both doubles the count
    and the finalizer findings, which is a plan that disagrees with itself — and
    the version to keep is the one the cluster prefers, not the one that sorts
    first, because a CRD's old version can be missing fields the new one has.
    Only `/v2` is stubbed here: reading `/v1beta1` fails the test outright."""
    quiet.catalog.append(api_resource("widgets", group="example.com", version="v2",
                                      kind="Widget", preferred=True))
    quiet.catalog.append(api_resource("widgets", group="example.com", version="v1beta1",
                                      kind="Widget", preferred=False))
    quiet.at("/apis/example.com/v2/namespaces/prod/widgets",
             listing([{"kind": "Widget", "metadata": {"name": "w1"}}]))

    report = nsd.plan(NAMESPACE)

    assert [row for row in report["inventory"]["kinds"] if row["resource"] == "widgets"] == [
        {"group": "example.com", "version": "v2", "resource": "widgets", "kind": "Widget",
         "count": 1, "truncated": False},
    ]


# --------------------------------------------------------------------------- #
# §0.1: a count we could not derive is null, never zero
# --------------------------------------------------------------------------- #

def test_a_refused_listing_is_null_and_names_itself(quiet):
    """The whole module in one assertion. "prod holds no PersistentVolumeClaims"
    is what an operator reads before deleting a database."""
    quiet.fails(core("persistentvolumeclaims"))

    report = nsd.plan(NAMESPACE)

    row = kind_row(report, "persistentvolumeclaims")
    assert row["count"] is None, "0 would read as 'this namespace holds no claims'"
    assert report["partial"] is True
    assert any(
        entry["resource"] == "persistentvolumeclaims" for entry in report["unavailable"]
    )


def test_a_refused_listing_makes_the_inventory_an_acknowledged_consequence(quiet):
    quiet.fails(core("pods"))

    report = nsd.plan(NAMESPACE)

    assert nsd.WARN_INVENTORY_INCOMPLETE in codes(report)
    (entry,) = [c for c in report["consequences"]
                if c["code"] == nsd.WARN_INVENTORY_INCOMPLETE]
    assert "pods" in entry["consequence"]
    assert "floor" in entry["mitigation"]


def test_a_truncated_listing_uses_the_servers_own_remaining_count(quiet):
    quiet.at(core("pods"), listing([pod(f"p{i}") for i in range(3)], cont="tok", remaining=497))

    report = nsd.plan(NAMESPACE)

    row = kind_row(report, "pods")
    assert (row["count"], row["truncated"]) == (500, True)


def test_a_truncated_listing_with_no_remaining_count_is_null_not_the_page_size(quiet):
    """`remainingItemCount` is absent whenever the API server cannot count
    cheaply. Reporting the page size would say "there are exactly 200"."""
    quiet.at(core("pods"), listing([pod(f"p{i}") for i in range(3)], cont="tok"))

    report = nsd.plan(NAMESPACE)

    row = kind_row(report, "pods")
    assert (row["count"], row["truncated"]) == (None, True)
    assert nsd.WARN_INVENTORY_INCOMPLETE in codes(report)


def test_a_complete_listing_is_not_flagged_incomplete(loaded):
    report = nsd.plan(NAMESPACE)

    assert nsd.WARN_INVENTORY_INCOMPLETE not in codes(report)


def test_a_discovery_gap_travels_into_the_plans_unavailable_list(quiet):
    """A group the cluster could not enumerate is a hole in the inventory, and a
    hole in the inventory is a hole in every finding scanned out of it."""
    quiet.gaps.append({
        "group": "kafka.strimzi.io", "resource": "*", "namespace": None,
        "error": "cluster_unreachable", "message": "the group could not be enumerated",
        "hint": None,
    })

    report = nsd.plan(NAMESPACE)

    assert any(entry["group"] == "kafka.strimzi.io" for entry in report["unavailable"])
    assert report["partial"] is True


def test_there_is_no_grand_total(loaded):
    """`events` is served by the core group and by events.k8s.io over the same
    objects, so any sum across kinds double-counts them."""
    assert "total" not in nsd.plan(NAMESPACE)["inventory"]


# --------------------------------------------------------------------------- #
# The volumes — the finding that is not reversible
# --------------------------------------------------------------------------- #

def test_a_delete_policy_volume_is_reported_as_destroyed_not_released(loaded):
    report = nsd.plan(NAMESPACE)

    assert report["volumes"][0]["reclaim_policy"] == "Delete"
    assert nsd.WARN_DESTROYS_VOLUME_DATA in codes(report)
    (entry,) = [c for c in report["consequences"]
                if c["code"] == nsd.WARN_DESTROYS_VOLUME_DATA]
    assert "postgres-data" in entry["consequence"]
    assert "The data is gone" in entry["consequence"]


def test_a_retain_policy_volume_is_a_different_consequence_from_a_deleted_one(loaded):
    """Opposite outcomes. One consequence covering both would have to be worded
    so vaguely that neither operator learns what happened to their data."""
    loaded.at(core("persistentvolumes", name="pv-1", namespaced=False),
              volume(reclaim="Retain"))

    report = nsd.plan(NAMESPACE)

    assert nsd.WARN_RELEASES_VOLUMES in codes(report)
    assert nsd.WARN_DESTROYS_VOLUME_DATA not in codes(report)
    (entry,) = [c for c in report["consequences"] if c["code"] == nsd.WARN_RELEASES_VOLUMES]
    assert "Released" in entry["consequence"]


def test_a_volume_that_could_not_be_read_is_unknown_and_never_assumed(loaded):
    """The reclaim policy lives on a cluster-scoped object the console may not
    be granted. Guessing either answer is a claim about somebody's database."""
    loaded.fails(core("persistentvolumes", name="pv-1", namespaced=False))

    report = nsd.plan(NAMESPACE)

    row = report["volumes"][0]
    assert row["reclaim_policy"] is None
    assert "unknown" in row["reason"]
    assert nsd.WARN_VOLUME_FATE_UNKNOWN in codes(report)
    assert nsd.WARN_DESTROYS_VOLUME_DATA not in codes(report)
    assert nsd.WARN_RELEASES_VOLUMES not in codes(report)
    assert any(entry["resource"] == "persistentvolumes" for entry in report["unavailable"])


def test_the_unknown_volume_consequence_says_it_is_not_reporting_them_safe(loaded):
    loaded.fails(core("persistentvolumes", name="pv-1", namespaced=False))

    (entry,) = [c for c in nsd.plan(NAMESPACE)["consequences"]
                if c["code"] == nsd.WARN_VOLUME_FATE_UNKNOWN]

    assert "not reporting that they are safe" in entry["consequence"]
    assert "persistentvolumes" in entry["mitigation"]


def test_an_unbound_claim_reads_no_volume_and_warns_about_nothing(loaded):
    """A real "nothing is lost", said as one — and distinct from the unknown
    above, which uses the same null in the same field."""
    loaded.at(core("persistentvolumeclaims"),
              listing([claim("scratch", volume=None, phase="Pending")]))

    report = nsd.plan(NAMESPACE)

    row = report["volumes"][0]
    assert (row["volume"], row["reclaim_policy"]) == (None, None)
    assert "no stored data" in row["reason"]
    assert nsd.WARN_VOLUME_FATE_UNKNOWN not in codes(report)
    assert not any(path.endswith("/persistentvolumes/pv-1") for path, _ in loaded.of("GET")), (
        "an unbound claim has no volume to read, and reading one would be a 404 "
        "rendered as a gap in the plan"
    )


def test_the_volume_row_carries_what_an_operator_needs_to_find_it_afterwards(loaded):
    loaded.at(core("persistentvolumes", name="pv-1", namespaced=False),
              volume(reclaim="Retain"))

    row = nsd.plan(NAMESPACE)["volumes"][0]

    assert row["claim"] == "postgres-data"
    assert row["volume"] == "pv-1"
    assert row["capacity"] == "200Gi"
    assert row["storage_class"] == "fast"


# --------------------------------------------------------------------------- #
# The addresses
# --------------------------------------------------------------------------- #

def test_a_loadbalancer_service_takes_its_address_with_it(loaded):
    report = nsd.plan(NAMESPACE)

    assert report["load_balancers"] == [{"name": "edge", "addresses": ["203.0.113.9"]}]
    (entry,) = [c for c in report["consequences"] if c["code"] == nsd.WARN_DROPS_LOAD_BALANCER]
    assert "203.0.113.9" in entry["consequence"]
    assert "different one" in entry["consequence"]


def test_a_clusterip_service_is_not_a_load_balancer_finding(loaded):
    loaded.at(core("services"), listing([service("api", kind="ClusterIP")]))

    report = nsd.plan(NAMESPACE)

    assert report["load_balancers"] == []
    assert nsd.WARN_DROPS_LOAD_BALANCER not in codes(report)


def test_a_loadbalancer_with_no_address_yet_is_still_a_finding(loaded):
    """The provider has not assigned one; deleting the Service still releases
    whatever it was about to get, and the Service itself still goes."""
    loaded.at(core("services"), listing([service("edge", addresses=[])]))

    report = nsd.plan(NAMESPACE)

    assert report["load_balancers"] == [{"name": "edge", "addresses": []}]
    assert nsd.WARN_DROPS_LOAD_BALANCER in codes(report)


def test_a_hostname_address_is_reported_like_an_ip(loaded):
    loaded.at(core("services"),
              listing([service("edge", addresses=["a1b2.elb.amazonaws.com"])]))

    assert nsd.plan(NAMESPACE)["load_balancers"][0]["addresses"] == [
        "a1b2.elb.amazonaws.com",
    ]


# --------------------------------------------------------------------------- #
# The admission webhooks — §19's finding, one step earlier
# --------------------------------------------------------------------------- #

def test_a_webhook_backed_from_this_namespace_is_found(loaded):
    report = nsd.plan(NAMESPACE)

    assert report["webhooks"] == [{
        "configuration": "policy", "kind": "ValidatingWebhookConfiguration",
        "webhook": "policy.example.com", "service": "admission", "failure_policy": "Fail",
    }]
    (entry,) = [c for c in report["consequences"]
                if c["code"] == nsd.WARN_BREAKS_ADMISSION_WEBHOOK]
    assert "failing closed" in entry["label"]
    assert "across the whole cluster" in entry["consequence"]


def test_a_webhook_backed_from_elsewhere_is_not_this_namespaces_problem(loaded):
    loaded.at(VALIDATING, listing([webhook_configuration("policy",
                                                         service_namespace="cert-manager")]))

    report = nsd.plan(NAMESPACE)

    assert report["webhooks"] == []
    assert nsd.WARN_BREAKS_ADMISSION_WEBHOOK not in codes(report)


def test_an_absent_failure_policy_is_fail_because_that_is_the_v1_default(loaded):
    """The dangerous direction is the *absent* field, which is the opposite of
    the v1beta1 default people remember."""
    loaded.at(VALIDATING, listing([webhook_configuration("policy", failure_policy=None)]))

    report = nsd.plan(NAMESPACE)

    assert report["webhooks"][0]["failure_policy"] == "Fail"
    assert "failing closed" in [
        c for c in report["consequences"] if c["code"] == nsd.WARN_BREAKS_ADMISSION_WEBHOOK
    ][0]["label"]


def test_a_failing_open_webhook_says_enforcement_stops_not_that_writes_stop(loaded):
    loaded.at(VALIDATING, listing([webhook_configuration("policy", failure_policy="Ignore")]))

    (entry,) = [c for c in nsd.plan(NAMESPACE)["consequences"]
                if c["code"] == nsd.WARN_BREAKS_ADMISSION_WEBHOOK]

    assert "all failing open" in entry["label"]
    assert "stops being enforced" in entry["consequence"]
    assert "refuses every write" not in entry["consequence"]


def test_a_mutating_configuration_counts_as_much_as_a_validating_one(loaded):
    loaded.at(VALIDATING, listing([]))
    loaded.at(MUTATING, listing([webhook_configuration(
        "inject", kind="MutatingWebhookConfiguration", webhook="sidecar.example.com",
    )]))

    assert nsd.plan(NAMESPACE)["webhooks"][0]["kind"] == "MutatingWebhookConfiguration"


def test_unreadable_webhook_configurations_are_none_and_never_an_empty_list(loaded):
    """`[]` says "nothing points here", which is the most reassuring possible
    description of "we could not look" — and the two differ by a cluster that
    stops accepting writes."""
    loaded.fails(VALIDATING)
    loaded.fails(MUTATING)

    report = nsd.plan(NAMESPACE)

    assert report["webhooks"] is None
    (entry,) = [c for c in report["consequences"]
                if c["code"] == nsd.WARN_BREAKS_ADMISSION_WEBHOOK]
    assert "unknown" in entry["label"]
    assert "cannot tell you whether one does" in entry["consequence"]


def test_one_readable_listing_is_reported_rather_than_discarded(loaded):
    """Losing the mutating listing must not turn a real validating finding into
    "we could not look" — a partial answer is still an answer about what it saw."""
    loaded.fails(MUTATING)

    report = nsd.plan(NAMESPACE)

    assert report["webhooks"] is not None
    assert [row["configuration"] for row in report["webhooks"]] == ["policy"]
    assert report["partial"] is True


# --------------------------------------------------------------------------- #
# The finalizers — why a namespace hangs in Terminating
# --------------------------------------------------------------------------- #

def test_objects_holding_a_finalizer_are_listed_with_the_finalizer_they_hold(loaded):
    loaded.at(core("pods"), listing([pod("api-0"), pod("job-0", finalizers=["batch/job"])]))

    report = nsd.plan(NAMESPACE)

    assert report["finalizers"] == [
        {"resource": "/pods", "name": "job-0", "finalizers": ["batch/job"]},
    ]
    (entry,) = [c for c in report["consequences"]
                if c["code"] == nsd.WARN_FINALIZERS_MAY_HANG]
    assert "Terminating" in entry["consequence"]
    assert "Do not clear finalizers by hand" in entry["mitigation"]


def test_a_namespace_holding_no_finalizers_is_not_warned_about_hanging(loaded):
    report = nsd.plan(NAMESPACE)

    assert report["finalizers"] == []
    assert nsd.WARN_FINALIZERS_MAY_HANG not in codes(report)


def test_the_namespaces_own_finalizers_exclude_the_one_the_api_server_adds(quiet):
    """Every namespace carries `kubernetes`. Reporting it would make the finding
    fire on every namespace, which is a finding nobody reads."""
    quiet.at(core("namespaces", name=NAMESPACE, namespaced=False),
             namespace(finalizers=["kubernetes", "example.com/protect"]))

    assert nsd.plan(NAMESPACE)["namespaceFinalizers"] == ["example.com/protect"]


# --------------------------------------------------------------------------- #
# The plan itself
# --------------------------------------------------------------------------- #

def test_a_namespace_that_does_not_exist_raises_rather_than_planning(cluster):
    """There is no useful plan for a namespace nobody can read, and 404 is the
    right answer to "what would deleting this take with it"."""
    cluster.fails(core("namespaces", name=NAMESPACE, namespaced=False),
                  api_exception(status=404, reason="Not Found"))

    with pytest.raises(NotFound):
        nsd.plan(NAMESPACE)


def test_an_empty_namespace_asks_for_no_acknowledgement(quiet):
    report = nsd.plan(NAMESPACE)

    assert report["consequences"] == []
    assert report["partial"] is False
    assert report["blocked"] is None


def test_the_plan_carries_the_gate_so_the_button_is_disabled_with_a_reason(quiet):
    report = nsd.plan(NAMESPACE)

    assert report["gate"]["enabled"] is False
    assert "ADMIN_ALLOW_MUTATIONS" in report["gate"]["detail"]


def test_the_plan_names_background_propagation_rather_than_leaving_it_implicit(quiet):
    """`Orphan` sounds like it would save the contents and does not: the
    namespace controller deletes them regardless of ownerReferences."""
    assert nsd.plan(NAMESPACE)["propagationPolicy"] == "Background"


def test_the_plan_is_a_read_and_sends_no_write(loaded):
    nsd.plan(NAMESPACE)

    assert loaded.of("DELETE") == []
    assert {method for method, _path, _query in loaded.requests} == {"GET"}


# --------------------------------------------------------------------------- #
# A namespace already Terminating
# --------------------------------------------------------------------------- #

@pytest.fixture
def stuck(loaded):
    loaded.at(core("namespaces", name=NAMESPACE, namespaced=False), namespace(
        phase="Terminating", deletion_timestamp="2026-09-06T09:00:00Z",
        finalizers=["kubernetes"],
    ))
    loaded.at(core("pods"), listing([pod("job-0", finalizers=["batch/job"])]))
    return loaded


def test_a_terminating_namespace_is_blocked_and_says_re_deleting_does_nothing(stuck):
    report = nsd.plan(NAMESPACE)

    assert report["blocked"]["message"] == "prod is already being deleted."
    assert "re-deleting it does nothing" in report["blocked"]["hint"]
    assert report["deletionTimestamp"] == "2026-09-06T09:00:00Z"


def test_the_blocked_plan_is_still_the_diagnosis_of_why_it_is_stuck(stuck):
    """The whole reason the plan runs on a namespace it refuses to delete."""
    report = nsd.plan(NAMESPACE)

    assert report["finalizers"] == [
        {"resource": "/pods", "name": "job-0", "finalizers": ["batch/job"]},
    ]
    assert report["consequences"] == [], "there is nothing left to acknowledge"


def test_deleting_a_terminating_namespace_is_refused_before_any_write(stuck, allow_mutations):
    with pytest.raises(Invalid) as caught:
        nsd.delete_namespace(NAMESPACE, dry_run=False)

    assert "already being deleted" in caught.value.message
    assert stuck.of("DELETE") == []


# --------------------------------------------------------------------------- #
# The write: the handshake
# --------------------------------------------------------------------------- #

ALL_CODES = [
    nsd.WARN_DESTROYS_VOLUME_DATA,
    nsd.WARN_DROPS_LOAD_BALANCER,
    nsd.WARN_BREAKS_ADMISSION_WEBHOOK,
]


def test_an_unacknowledged_consequence_refuses_the_write_and_names_the_codes(loaded):
    with pytest.raises(Invalid) as caught:
        nsd.delete_namespace(NAMESPACE, dry_run=True)

    assert caught.value.context["parameter"] == "acknowledgeConsequences"
    assert set(caught.value.context["unacknowledged"]) == set(ALL_CODES)
    assert loaded.of("DELETE") == []


def test_acknowledging_some_is_not_acknowledging_all(loaded):
    with pytest.raises(Invalid) as caught:
        nsd.delete_namespace(
            NAMESPACE, dry_run=True,
            acknowledge_consequences=[nsd.WARN_DESTROYS_VOLUME_DATA],
        )

    assert caught.value.context["unacknowledged"] == [
        nsd.WARN_DROPS_LOAD_BALANCER, nsd.WARN_BREAKS_ADMISSION_WEBHOOK,
    ]


def test_consequences_are_recomputed_at_write_time_not_trusted_from_the_plan(loaded):
    """A volume provisioned between reading the plan and confirming it is not
    covered by the tick the operator gave the old plan."""
    nsd.plan(NAMESPACE)
    loaded.at(core("persistentvolumeclaims"), listing([
        claim("postgres-data"), claim("redis-data", volume="pv-2"),
    ]))
    loaded.at(core("persistentvolumes", name="pv-2", namespaced=False), volume(reclaim="Delete"))
    loaded.at(VALIDATING, listing([]))
    loaded.at(core("services"), listing([]))

    with pytest.raises(Invalid) as caught:
        nsd.delete_namespace(
            NAMESPACE, dry_run=True, acknowledge_consequences=[nsd.WARN_DROPS_LOAD_BALANCER],
        )

    assert caught.value.context["unacknowledged"] == [nsd.WARN_DESTROYS_VOLUME_DATA]


def test_an_empty_namespace_needs_no_acknowledgement_to_delete(db_engine, quiet):
    response = nsd.delete_namespace(NAMESPACE, dry_run=True)

    assert response["dryRun"] is True
    assert response["consequences"] == []


# --------------------------------------------------------------------------- #
# The write: through the funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_sends_dryrun_all_and_reports_applied_false(db_engine, loaded):
    response = nsd.delete_namespace(
        NAMESPACE, dry_run=True, acknowledge_consequences=ALL_CODES,
    )

    (path, query), = loaded.of("DELETE")
    assert path == "/api/v1/namespaces/prod"
    assert query["dryRun"] == "All"
    assert query["propagationPolicy"] == "Background"
    assert response["applied"] is False
    assert audit_rows()[0]["outcome"] == "dry_run"


def test_a_real_delete_omits_dryrun_and_is_audited_as_applied(db_engine, loaded,
                                                              allow_mutations):
    response = nsd.delete_namespace(
        NAMESPACE, dry_run=False, acknowledge_consequences=ALL_CODES,
    )

    (_path, query), = loaded.of("DELETE")
    assert "dryRun" not in query
    assert response["applied"] is True
    assert audit_rows()[0]["outcome"] == "applied"


def test_the_diff_shows_the_namespace_disappearing(db_engine, loaded):
    response = nsd.delete_namespace(
        NAMESPACE, dry_run=True, acknowledge_consequences=ALL_CODES,
    )

    assert response["diff"]["after"] == ""
    assert "-kind: Namespace" in response["diff"]["unified"]


def test_the_audit_sentence_names_what_went_with_it(db_engine, loaded, allow_mutations):
    """"delete namespace prod" is not what happened if it took a database with
    it, and the audit trail is read after the incident, not before."""
    nsd.delete_namespace(NAMESPACE, dry_run=False, acknowledge_consequences=ALL_CODES)

    detail = audit_rows()[0]["detail"]
    assert "1 volume(s) destroyed" in detail
    assert "1 load balancer(s) released" in detail
    assert "1 admission webhook(s) left without a backend" in detail


def test_the_response_carries_the_plan_it_was_confirmed_against(db_engine, loaded):
    response = nsd.delete_namespace(
        NAMESPACE, dry_run=True, acknowledge_consequences=ALL_CODES,
    )

    assert response["plan"]["volumes"][0]["claim"] == "postgres-data"
    assert [entry["code"] for entry in response["consequences"]] == ALL_CODES


def test_a_read_only_console_refuses_the_delete_and_still_records_it(db_engine, loaded):
    with pytest.raises(MutationsDisabled):
        nsd.delete_namespace(NAMESPACE, dry_run=False, acknowledge_consequences=ALL_CODES)

    assert loaded.of("DELETE") == []
    assert audit_rows()[0]["outcome"] == "denied"


def test_a_read_only_console_still_answers_the_plan(quiet):
    """The plan is a read, and an operator deciding whether to enable writes has
    to be able to see what enabling them would destroy."""
    report = nsd.plan(NAMESPACE)

    assert report["gate"]["enabled"] is False
    assert "The plan is still available" in report["gate"]["detail"]


def test_a_denied_preflight_is_rbac_denied_and_names_the_permission(db_engine, loaded,
                                                                    allow_mutations, fake_k8s):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no binding", evaluation_error=None, denied=True,
    )))

    with pytest.raises(RBACDenied) as caught:
        nsd.delete_namespace(NAMESPACE, dry_run=False, acknowledge_consequences=ALL_CODES)

    assert "namespaces" in caught.value.message
    assert loaded.of("DELETE") == []
    assert audit_rows()[0]["outcome"] == "denied"


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

def test_the_plan_endpoint_returns_the_report(client, cluster_id, loaded):
    response = client.get(f"/api/projects/{NAMESPACE}/delete-plan",
                          params={"cluster_id": cluster_id})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == NAMESPACE
    assert [entry["code"] for entry in body["consequences"]] == ALL_CODES


def test_the_delete_endpoint_defaults_to_a_dry_run(client, cluster_id, loaded):
    response = client.request(
        "DELETE", f"/api/projects/{NAMESPACE}", params={"cluster_id": cluster_id},
        json={"acknowledgeConsequences": ALL_CODES},
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert loaded.of("DELETE")[0][1]["dryRun"] == "All"


def test_a_delete_whose_body_never_arrived_is_a_dry_run_not_a_deletion(client, cluster_id,
                                                                       quiet):
    """A `DELETE` body is legal and occasionally stripped by an intermediary.
    Losing it must fail in the safe direction, and this is that direction."""
    response = client.request("DELETE", f"/api/projects/{NAMESPACE}",
                              params={"cluster_id": cluster_id})

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert quiet.of("DELETE")[0][1]["dryRun"] == "All"


def test_an_unacknowledged_delete_is_422_with_the_codes_the_ui_ticks(client, cluster_id,
                                                                     loaded):
    response = client.request("DELETE", f"/api/projects/{NAMESPACE}",
                              params={"cluster_id": cluster_id}, json={"dryRun": True})

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert set(body["context"]["unacknowledged"]) == set(ALL_CODES)
