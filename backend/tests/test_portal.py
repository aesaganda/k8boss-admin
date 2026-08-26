"""
The operator portal (§16) — reading somebody else's catalog, and writing one
Subscription into it.

The portal is the only feature that reports on software this console neither
ships nor controls, so almost every test here is about a distinction rather than
a value:

* **"This cluster does not run OLM" is an ordinary fact.** Every source is
  ``unsupported``, ``items`` is empty, and ``partial`` stays false — because a
  §1.2 partial banner raised on every vanilla cluster is a banner nobody reads.
  A discovery that *failed*, by contrast, is ``unknown``, lands in
  ``unavailable[]`` and does raise it.
* **``installed`` is a tri-state and ``false`` is a claim.** Only a Subscription
  listing that actually happened entitles a catalog row to say "not installed";
  saying it during an outage is how an operator creates a second Subscription
  for an operator that already has one, leaving two CSVs racing for the same
  CRDs.
* **A Subscription is a request, not an installation.** ``applied: true`` means
  one object was created. What installs the operator afterwards is OLM, and only
  if the namespace has exactly one OperatorGroup whose scope the operator
  supports and the approval strategy does not hold it. Each of those is checked
  *before* the write and has to be acknowledged by name.
"""

from __future__ import annotations

import pytest
import yaml
from kubernetes.client.rest import ApiException

from app.admin import portal as portal_admin
from app.errors import Invalid, MutationsDisabled, NotFound
from app.resources import catalog as discovery
from app.services import portal as portal_service
from tests.test_router import allow_preflight
from tests.test_routes import _groups_payload, _resources

NAMESPACE = "monitoring"
CATALOG = "community-operators"
CATALOG_NAMESPACE = "olm"

PACKAGES_GV = "/apis/packages.operators.coreos.com/v1"
OLM_GV = "/apis/operators.coreos.com/v1alpha1"
GROUPS_GV = "/apis/operators.coreos.com/v1"

#: What a cluster running Operator Lifecycle Manager serves. Two API groups: the
#: package server (an aggregated APIService) and OLM's own CRDs, whose
#: OperatorGroup lives on ``v1`` while everything else is still ``v1alpha1``.
OLM_GROUPS = (
    ("packages.operators.coreos.com", "v1"),
    ("operators.coreos.com", "v1alpha1"),
    ("operators.coreos.com", "v1"),
)

OLM_PLURALS = (
    "packagemanifests",
    "subscriptions",
    "clusterserviceversions",
    "installplans",
    "catalogsources",
    "operatorgroups",
)


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Discovery is cached per cluster; tests must not share one another's."""
    discovery.invalidate_cache(all_clusters=True)
    yield
    discovery.invalidate_cache(all_clusters=True)


@pytest.fixture
def allow_subscribing(monkeypatch, allow_mutations):
    """Both gates open, which is what a real subscribe needs."""
    monkeypatch.setattr(portal_admin.settings, "portal_install_enabled", True)
    return portal_admin.settings


# --------------------------------------------------------------------------- #
# A stubbed cluster
# --------------------------------------------------------------------------- #

def _listing(items, *, cont=None, remaining=None):
    """A ``List`` payload in the shape ``reader.list_resource`` unwraps."""
    metadata: dict[str, object] = {}
    if cont:
        metadata["continue"] = cont
    if remaining is not None:
        metadata["remainingItemCount"] = remaining
    return {"items": list(items), "metadata": metadata}


def stub_olm(monkeypatch, *, groups=OLM_GROUPS, discovery_failures=None, lists=None):
    """Install one fake ``raw_get`` covering both discovery and the listings.

    Discovery and every generic read go through the same function
    (``catalog.raw_get``), so one stub answers both — which is also what makes a
    listing path reachable at all: ``reader.list_resource`` builds its own URL
    from the resolved catalog item.

    ``lists`` maps a plural to a list of objects, to a full ``List`` payload (for
    a truncated listing) or to an exception to raise. Every OLM plural defaults
    to an empty listing, so a test says only what it is about; anything asked for
    that is neither a stubbed discovery path nor a known plural is an
    ``AssertionError``, for the same reason ``FakeApi`` behaves that way — a
    permissive stub makes a swallowed failure indistinguishable from a genuinely
    empty cluster, which is the bug class this module is about.

    Listing paths are dispatched on the trailing plural rather than matched
    whole, because the same resource is read cluster-wide by the catalog
    (``/apis/<group>/<version>/<plural>``) and per namespace by the plan
    (``/apis/<group>/<version>/namespaces/<ns>/<plural>``), and both are correct.
    """
    payloads = {
        "/api": {"versions": ["v1"]},
        "/api/v1": _resources(("services", "Service")),
        "/apis": _groups_payload(*groups),
        PACKAGES_GV: _resources(("packagemanifests", "PackageManifest")),
        OLM_GV: _resources(
            ("subscriptions", "Subscription"),
            ("clusterserviceversions", "ClusterServiceVersion"),
            ("installplans", "InstallPlan"),
            ("catalogsources", "CatalogSource"),
        ),
        GROUPS_GV: _resources(("operatorgroups", "OperatorGroup")),
    }
    payloads.update(discovery_failures or {})
    listings: dict[str, object] = {plural: [] for plural in OLM_PLURALS}
    listings.update(lists or {})

    def fake_raw_get(path, *, query=None):
        if path in payloads:
            value = payloads[path]
            if isinstance(value, Exception):
                raise value
            return value
        plural = path.rsplit("/", 1)[-1]
        if plural in listings:
            value = listings[plural]
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, dict) else _listing(value)
        raise AssertionError(f"the cluster was asked for an unstubbed path: {path}")

    monkeypatch.setattr(discovery, "raw_get", fake_raw_get)


def stub_create(monkeypatch, *, error=None, warnings=()):
    """Make the one write succeed (or fail), and record the request it sent.

    Patches ``app.admin.apply.request_json``, which is the single place the
    create reaches the cluster, so the funnel above it — gate, preflight, diff,
    audit — runs exactly as it does in production.

    ``warnings`` stands in for the API server's ``Warning:`` headers on the
    create, which §1.5 promises verbatim and which are a different thing from the
    portal's own consequences.

    Null query values are dropped the way the real ``request_json`` drops them,
    so a recorded call says what would actually have gone on the wire — a stub
    that kept ``dryRun=None`` would make "this write sent no dryRun"
    unassertable.
    """
    from app.admin import apply as apply_service

    calls: list[dict] = []

    def fake_request_json(method, path, *, query=None, body=None, content_type=None):
        calls.append({
            "method": method,
            "path": path,
            "query": {k: v for k, v in (query or []) if v is not None},
            "body": body,
        })
        if error is not None:
            raise error
        sent = body or {}
        return (
            {**sent, "metadata": {**(sent.get("metadata") or {}), "resourceVersion": "1"}},
            list(warnings),
        )

    monkeypatch.setattr(apply_service, "request_json", fake_request_json)
    return calls


# --------------------------------------------------------------------------- #
# Cluster objects
# --------------------------------------------------------------------------- #

def _channel(
    name="stable",
    *,
    version="1.0.0",
    supports=("AllNamespaces",),
    publishes_modes=True,
):
    """One ``status.channels[]`` entry, as the package server publishes it.

    ``publishes_modes=False`` is the pruned-catalog case: the channel exists and
    carries no ``installModes`` at all, which is "we do not know what scopes this
    operator supports" and not "it supports none".
    """
    csv_description: dict[str, object] = {
        "displayName": "Prometheus",
        "version": version,
        "provider": {"name": "Red Hat"},
        "annotations": {
            "description": "Monitors things.",
            "categories": "Monitoring, Logging",
            "capabilities": "Basic Install",
            "certified": "false",
            "containerImage": "quay.io/example/prometheus:1.0.0",
        },
    }
    if publishes_modes:
        csv_description["installModes"] = [
            {"type": mode, "supported": mode in supports}
            for mode in (
                "OwnNamespace", "SingleNamespace", "MultiNamespace", "AllNamespaces",
            )
        ]
    return {
        "name": name,
        "currentCSV": f"prometheus.v{version}",
        "currentCSVDesc": csv_description,
    }


def _package(
    name="prometheus",
    *,
    catalog=CATALOG,
    catalog_namespace=CATALOG_NAMESPACE,
    channels=None,
    default_channel="stable",
):
    return {
        "apiVersion": "packages.operators.coreos.com/v1",
        "kind": "PackageManifest",
        "metadata": {"name": name, "namespace": catalog_namespace},
        "status": {
            "packageName": name,
            "catalogSource": catalog,
            "catalogSourceNamespace": catalog_namespace,
            "catalogSourceDisplayName": "Community Operators",
            "provider": {"name": "Red Hat", "url": "https://example.invalid"},
            "defaultChannel": default_channel,
            "channels": list(channels) if channels is not None else [_channel()],
        },
    }


def _subscription(
    name="prometheus",
    *,
    namespace=NAMESPACE,
    package="prometheus",
    channel="stable",
    installed_csv=None,
    install_plan=None,
):
    status: dict[str, object] = {}
    if installed_csv:
        status["installedCSV"] = installed_csv
        status["currentCSV"] = installed_csv
    if install_plan:
        status["installPlanRef"] = {"name": install_plan}
    return {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {
            "name": package,
            "channel": channel,
            "source": CATALOG,
            "sourceNamespace": CATALOG_NAMESPACE,
            "installPlanApproval": "Automatic",
        },
        "status": status,
    }


def _csv(name="prometheus.v1.0.0", *, namespace=NAMESPACE, phase="Succeeded"):
    return {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "ClusterServiceVersion",
        "metadata": {"name": name, "namespace": namespace},
        "status": {"phase": phase, "message": "install strategy completed"},
    }


def _operator_group(name="og", *, namespace=NAMESPACE, targets=None, selector=None):
    spec: dict[str, object] = {}
    if targets is not None:
        spec["targetNamespaces"] = list(targets)
    if selector is not None:
        spec["selector"] = selector
    return {
        "apiVersion": "operators.coreos.com/v1",
        "kind": "OperatorGroup",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _plan_body(**overrides):
    body = {"package": "prometheus", "namespace": NAMESPACE}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# The catalog read — three-valued API state
# --------------------------------------------------------------------------- #

def test_a_cluster_without_olm_reports_unsupported_sources_and_is_not_partial(
    monkeypatch, fake_k8s,
):
    """A vanilla cluster has no OLM. That is a fact about it, not a failed read.

    Reported as ``unsupported`` and kept out of ``unavailable[]``, so §1.2's
    partial banner does not fire on every non-OpenShift cluster in the fleet —
    a banner that appears everywhere is a banner nobody reads, including on the
    one cluster where it means something.
    """
    stub_olm(monkeypatch, groups=[])

    body = portal_service.catalog()

    assert {s["api"]: s["state"] for s in body["sources"]} == {
        "packages": portal_service.STATE_UNSUPPORTED,
        "subscriptions": portal_service.STATE_UNSUPPORTED,
        "catalogsources": portal_service.STATE_UNSUPPORTED,
    }
    assert body["items"] == []
    assert body["partial"] is False
    assert body["unavailable"] == []


def test_a_package_server_that_did_not_answer_is_unknown_never_unsupported(
    monkeypatch, fake_k8s,
):
    """The package server is an aggregated APIService: it 503s rather than vanishing.

    Reported as "this cluster has no catalog" it would send somebody to install
    OLM on a cluster that is already running it, over an outage that resolves in
    seconds.
    """
    stub_olm(
        monkeypatch,
        discovery_failures={
            PACKAGES_GV: ApiException(status=503, reason="Service Unavailable"),
        },
    )

    body = portal_service.catalog()
    states = {s["api"]: s["state"] for s in body["sources"]}

    assert states["packages"] == portal_service.STATE_UNKNOWN
    assert states["packages"] != portal_service.STATE_UNSUPPORTED
    assert body["partial"] is True
    assert [
        entry for entry in body["unavailable"]
        if entry["group"] == "packages.operators.coreos.com"
    ]
    # The other two APIs are on a different group and are unaffected: a broken
    # package server does not make the Subscriptions unreadable.
    assert states["subscriptions"] == portal_service.STATE_AVAILABLE


def test_a_failed_subscription_listing_leaves_every_row_installed_null(
    monkeypatch, fake_k8s,
):
    """`installed: false` over an unread listing invites a second Subscription.

    Two Subscriptions for one package leave two resolutions competing for the
    same custom resources — a cluster-level failure produced by a console
    rendering a null as a no.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "subscriptions": ApiException(status=403, reason="Forbidden"),
    })

    body = portal_service.catalog()
    row = body["items"][0]

    assert row["installed"] is None
    assert row["installations"] is None
    assert body["partial"] is True


def test_a_subscription_listing_that_matched_nothing_makes_installed_false(
    monkeypatch, fake_k8s,
):
    """The other half of the same rule.

    Without it the null case above passes just as well for a code path that
    never returns anything but ``None`` — a tri-state that is really a one-state,
    and the "not installed" column would be blank for every operator forever.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})

    row = portal_service.catalog()["items"][0]

    assert row["installed"] is False
    assert row["installations"] == []


def test_an_installed_package_carries_the_subscription_that_installed_it(
    monkeypatch, fake_k8s,
):
    """Matched on ``spec.name`` alone: a mirror's copy is still the same operator."""
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "subscriptions": [_subscription()],
    })

    row = portal_service.catalog()["items"][0]

    assert row["installed"] is True
    assert [s["namespace"] for s in row["installations"]] == [NAMESPACE]


def test_catalogs_is_null_when_the_catalogsource_listing_failed(monkeypatch, fake_k8s):
    """`[]` would say "this cluster has no catalogs", which is a real claim.

    It is the claim that explains an empty portal, so making it from a failed
    read sends somebody to add a CatalogSource beside the one they already have.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "catalogsources": ApiException(status=500, reason="Internal Server Error"),
    })

    body = portal_service.catalog()

    assert body["catalogs"] is None
    assert body["partial"] is True


def test_a_catalogsource_with_no_status_is_healthy_null_not_unhealthy(
    monkeypatch, fake_k8s,
):
    """A CatalogSource that was created a second ago has no connection state yet.

    Rendered as unhealthy, it sends an operator to debug a registry that is
    merely still starting.
    """
    stub_olm(monkeypatch, lists={
        "catalogsources": [{
            "apiVersion": "operators.coreos.com/v1alpha1",
            "kind": "CatalogSource",
            "metadata": {"name": CATALOG, "namespace": CATALOG_NAMESPACE},
            "spec": {"sourceType": "grpc", "image": "quay.io/example/index:latest"},
        }],
    })

    row = portal_service.catalog()["catalogs"][0]

    assert row["healthy"] is None
    assert row["state"] is None
    assert "no connection state yet" in row["detail"]


# --------------------------------------------------------------------------- #
# The installed read — a Subscription is not an installation
# --------------------------------------------------------------------------- #

def test_a_subscription_olm_has_not_acted_on_yet_reports_no_phase(monkeypatch, fake_k8s):
    """No ``installedCSV`` means OLM installed nothing — it does not mean Failed.

    Resolution may be pending, an InstallPlan may be waiting for approval, or the
    namespace may have no OperatorGroup. All three are ordinary, and all three
    render red if the missing CSV is treated as a failed install.
    """
    stub_olm(monkeypatch, lists={"subscriptions": [_subscription()]})

    row = portal_service.installed_operators(namespace=NAMESPACE)["items"][0]

    assert row["phase"] is None
    assert row["installedCSV"] is None
    assert "has not installed anything" in row["phaseDetail"]


def test_a_failed_csv_listing_reports_no_phase_and_says_the_read_failed(
    monkeypatch, fake_k8s,
):
    """During an API outage every operator would otherwise be reported broken."""
    stub_olm(monkeypatch, lists={
        "subscriptions": [_subscription(installed_csv="prometheus.v1.0.0")],
        "clusterserviceversions": ApiException(status=403, reason="Forbidden"),
    })

    body = portal_service.installed_operators(namespace=NAMESPACE)
    row = body["items"][0]

    assert row["phase"] is None
    assert "No complete ClusterServiceVersion listing" in row["phaseDetail"]
    assert body["partial"] is True


def test_the_two_reasons_for_an_unknown_phase_do_not_share_a_sentence():
    """`phase: null` has two causes and the operator's next move differs.

    "OLM has not started yet" is a wait; "the CSV listing failed" is a read to
    retry or an RBAC grant to add. One sentence covering both would render the
    tri-state honestly and still tell nobody anything.
    """
    subscription = _subscription()

    pending = portal_service.subscription_row(subscription, csv_read=True)
    blind = portal_service.subscription_row(subscription, csv_read=False)

    assert pending["phase"] is None
    assert blind["phase"] is None
    assert pending["phaseDetail"] != blind["phaseDetail"]


def test_a_copied_csv_in_another_namespace_is_not_read_as_this_installation(
    monkeypatch, fake_k8s,
):
    """OLM copies a CSV owned by an all-namespaces group into every namespace.

    Joined by name alone, a copy in ``kube-system`` would answer for the
    installation a Subscription in ``monitoring`` is still waiting on — reporting
    Succeeded over an install that never happened.
    """
    stub_olm(monkeypatch, lists={
        "subscriptions": [_subscription(installed_csv="prometheus.v1.0.0")],
        "clusterserviceversions": [_csv(namespace="kube-system")],
    })

    row = portal_service.installed_operators()["items"][0]

    assert row["phase"] is None
    assert f"is in {NAMESPACE}" in row["phaseDetail"]


def test_a_csv_in_the_subscriptions_own_namespace_does_answer_for_it(
    monkeypatch, fake_k8s,
):
    """The other half: the join has to actually join, or the column is always null."""
    stub_olm(monkeypatch, lists={
        "subscriptions": [_subscription(installed_csv="prometheus.v1.0.0")],
        "clusterserviceversions": [_csv()],
    })

    row = portal_service.installed_operators()["items"][0]

    assert row["phase"] == "Succeeded"


def test_a_truncated_csv_listing_is_treated_as_a_read_that_did_not_happen(
    monkeypatch, fake_k8s,
):
    """A page of CSVs is not the CSVs.

    A Subscription whose CSV fell outside the bound would otherwise look like one
    whose CSV is missing — "the operator named an installation that is not
    there", which reads as a broken install rather than as a short read.
    """
    stub_olm(monkeypatch, lists={
        "subscriptions": [_subscription(installed_csv="prometheus.v1.0.0")],
        "clusterserviceversions": _listing([_csv()], cont="next-page", remaining=7),
    })

    body = portal_service.installed_operators()
    row = body["items"][0]

    assert row["phase"] is None
    assert "No complete ClusterServiceVersion listing" in row["phaseDetail"]
    assert [t["kind"] for t in body["truncated"]] == ["ClusterServiceVersion"]
    assert body["truncated"][0]["remaining"] == 7


# --------------------------------------------------------------------------- #
# The plan — what will stop this install, before it is made
# --------------------------------------------------------------------------- #

def _codes(result):
    """The consequence codes a plan or a subscribe reported, in order."""
    return [entry["code"] for entry in result["consequences"]]


def test_a_namespace_with_no_operator_group_is_not_ready_and_says_why(
    monkeypatch, fake_k8s,
):
    """OLM installs nothing into a namespace with no OperatorGroup.

    The Subscription is created and the ClusterServiceVersion fails with
    NoOperatorGroup, so the operator keeps looking subscribed while nothing
    whatsoever is running.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})

    result = portal_admin.plan(_plan_body())

    assert result["target"]["ready"] is False
    assert result["target"]["operatorGroups"] == []
    assert _codes(result) == [portal_admin.WARN_NO_OPERATOR_GROUP]


def test_two_operator_groups_are_not_ready_and_name_both(monkeypatch, fake_k8s):
    """OLM refuses to act in a namespace governed by more than one.

    Every operator already in the namespace is affected too, which is why the
    warning names them rather than saying "too many".
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group("first"), _operator_group("second")],
    })

    result = portal_admin.plan(_plan_body())

    assert result["target"]["ready"] is False
    assert _codes(result) == [portal_admin.WARN_TOO_MANY_OPERATOR_GROUPS]
    assert "first, second" in result["consequences"][0]["mitigation"]


def test_an_all_namespaces_group_refuses_an_operator_that_cannot_run_in_one(
    monkeypatch, fake_k8s,
):
    """An OperatorGroup with no ``targetNamespaces`` watches everything.

    A CSV that does not declare AllNamespaces goes straight to Failed with
    UnsupportedOperatorGroup, which is knowable from the catalog before the write
    rather than from the cluster twenty seconds after it.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package(channels=[_channel(supports=("OwnNamespace",))])],
        "operatorgroups": [_operator_group()],
    })

    result = portal_admin.plan(_plan_body())

    assert result["target"]["requiredInstallMode"] == "AllNamespaces"
    assert result["target"]["ready"] is False
    assert _codes(result) == [portal_admin.WARN_INSTALL_MODE_UNSUPPORTED]


def test_an_all_namespaces_group_accepts_an_operator_that_supports_it(
    monkeypatch, fake_k8s,
):
    """`ready: true` is only ever returned when every check ran and passed.

    Without this the refusals above would pass equally well for a plan that never
    says yes, and every install would carry a warning nobody can clear.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })

    result = portal_admin.plan(_plan_body())

    assert result["target"]["ready"] is True
    assert result["consequences"] == []
    assert result["partial"] is False


def test_a_group_targeting_its_own_namespace_requires_own_namespace(
    monkeypatch, fake_k8s,
):
    """OwnNamespace and SingleNamespace differ by whether the target is this one.

    Demanding SingleNamespace of an operator that only declares OwnNamespace
    blocks an install OLM would have accepted.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package(channels=[_channel(supports=("OwnNamespace",))])],
        "operatorgroups": [_operator_group(targets=[NAMESPACE])],
    })

    result = portal_admin.plan(_plan_body())

    assert result["target"]["requiredInstallMode"] == "OwnNamespace"
    assert result["target"]["ready"] is True
    assert result["consequences"] == []


def test_a_label_selector_operator_group_leaves_ready_unknown(monkeypatch, fake_k8s):
    """Which namespaces a selector covers is whatever the labels match right now.

    This console does not evaluate selectors, so both ``true`` and ``false`` here
    would be a confident verdict derived from a guess — one blocking a fine
    install, the other blessing a broken one.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [
            _operator_group(selector={"matchLabels": {"team": "platform"}}),
        ],
    })

    result = portal_admin.plan(_plan_body())

    assert result["target"]["ready"] is None
    assert result["target"]["requiredInstallMode"] is None
    assert _codes(result) == [portal_admin.WARN_INSTALL_MODES_UNKNOWN]


def test_an_unreadable_operator_group_listing_leaves_ready_unknown_and_still_renders(
    monkeypatch, fake_k8s,
):
    """The reads a plan makes fail independently, and each costs its own field.

    Losing the OperatorGroup listing costs the readiness verdict. It must not
    cost the document: what a subscribe would create is the thing the operator
    opened this dialog to read.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": ApiException(status=403, reason="Forbidden"),
    })

    result = portal_admin.plan(_plan_body())

    assert result["target"]["ready"] is None
    assert result["target"]["operatorGroups"] is None
    assert _codes(result) == [portal_admin.WARN_OPERATOR_GROUP_UNKNOWN]
    assert result["partial"] is True
    assert "kind: Subscription" in result["document"]


def test_a_channel_publishing_no_install_modes_leaves_ready_unknown(
    monkeypatch, fake_k8s,
):
    """An absent ``installModes`` is an unpublished CSV description, not an empty set.

    Read as "supports nothing" it would block every install from a pruned
    catalog; read as "supports everything" it would bless the one that fails.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package(channels=[_channel(publishes_modes=False)])],
        "operatorgroups": [_operator_group()],
    })

    result = portal_admin.plan(_plan_body())

    assert result["selected"]["installModes"] is None
    assert result["target"]["ready"] is None
    assert _codes(result) == [portal_admin.WARN_INSTALL_MODES_UNKNOWN]


def test_manual_approval_warns_that_nothing_installs_until_somebody_approves(
    monkeypatch, fake_k8s,
):
    """Manual approval stops the install dead, and this console does not approve.

    A subscribe that returned green while an InstallPlan sat waiting forever is
    exactly the "action reported as done" the defect standard refuses.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })

    result = portal_admin.plan(_plan_body(installPlanApproval="Manual"))

    assert _codes(result) == [portal_admin.WARN_MANUAL_APPROVAL]
    assert result["target"]["ready"] is True


def test_an_already_subscribed_package_warns_before_a_second_subscription(
    monkeypatch, fake_k8s,
):
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
        "subscriptions": [_subscription()],
    })

    result = portal_admin.plan(_plan_body())

    assert _codes(result) == [portal_admin.WARN_ALREADY_SUBSCRIBED]
    assert [s["name"] for s in result["existing"]] == ["prometheus"]


def test_an_unknown_channel_is_refused_naming_the_channels_that_exist(
    monkeypatch, fake_k8s,
):
    """Falling back to the default channel would install something nobody chose.

    Channels are not cosmetic: ``stable`` and ``nightly`` are different software
    with different upgrade paths.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})

    with pytest.raises(Invalid) as caught:
        portal_admin.plan(_plan_body(channel="nightly"))

    assert "nightly" in caught.value.message
    assert caught.value.context["channels"] == ["stable"]
    assert "stable" in caught.value.detail


def test_two_catalogs_offering_the_same_package_are_refused_rather_than_guessed(
    monkeypatch, fake_k8s,
):
    """One package name in two catalogs is ordinary, and they are not the same software.

    Picking whichever the API server listed first would write a Subscription
    pointing at a registry the operator did not choose — and the object would
    look exactly like the one they meant.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [
        _package(),
        _package(catalog="internal-mirror", catalog_namespace="mirror"),
    ]})

    with pytest.raises(NotFound) as caught:
        portal_admin.plan(_plan_body())

    assert caught.value.context["parameter"] == "catalog"
    assert "internal-mirror" in caught.value.detail


def test_naming_the_catalog_resolves_the_ambiguity(monkeypatch, fake_k8s):
    stub_olm(monkeypatch, lists={
        "packagemanifests": [
            _package(),
            _package(catalog="internal-mirror", catalog_namespace="mirror"),
        ],
        "operatorgroups": [_operator_group()],
    })

    result = portal_admin.plan(_plan_body(catalog="internal-mirror"))

    assert result["catalog"] == "internal-mirror"
    assert result["catalogNamespace"] == "mirror"


def test_the_plan_renders_with_both_gates_shut(monkeypatch, fake_k8s):
    """Deciding whether to set ADMIN_PORTAL_INSTALL_ENABLED means reading this first.

    Everything the plan does is a read of a catalog the cluster already
    publishes, so gating it would withhold the only evidence an operator has for
    the decision the gate exists for.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })

    result = portal_admin.plan(_plan_body())

    assert result["enabled"] is False
    assert "ADMIN_ALLOW_MUTATIONS" in result["enabledDetail"]
    assert "kind: Subscription" in result["document"]


def test_the_plan_writes_no_audit_row(monkeypatch, fake_k8s, db_session):
    """It reaches no cluster, so there is nothing for the trail to hold."""
    from app.models import AuditRecord

    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })
    before = db_session.query(AuditRecord).count()

    portal_admin.plan(_plan_body())

    assert db_session.query(AuditRecord).count() == before


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_a_real_subscribe_is_refused_when_the_feature_gate_is_off(
    monkeypatch, fake_k8s, allow_mutations,
):
    """`mutations_disabled`, never `rbac_denied`.

    The operator's permissions are not what is stopping this, and saying they are
    sends them to edit a ClusterRole that is already correct.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})
    monkeypatch.setattr(portal_admin.settings, "portal_install_enabled", False)

    with pytest.raises(MutationsDisabled) as caught:
        portal_admin.subscribe(_plan_body(), dry_run=False)

    assert caught.value.code == "mutations_disabled"
    assert "ADMIN_PORTAL_INSTALL_ENABLED" in caught.value.hint


def test_the_refusal_is_audited(monkeypatch, fake_k8s, allow_mutations, db_session):
    """The funnel never runs, so the row is written here or it is written nowhere.

    "Who tried to install an operator on a console where that is switched off" is
    exactly the question a hole in the trail cannot answer.
    """
    from app.models import AuditRecord

    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})
    monkeypatch.setattr(portal_admin.settings, "portal_install_enabled", False)

    with pytest.raises(MutationsDisabled):
        portal_admin.subscribe(_plan_body(), dry_run=False)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "denied"
    assert "subscribe to prometheus" in record.detail
    assert NAMESPACE in record.detail


def test_a_dry_run_is_permitted_with_the_feature_gate_off(
    monkeypatch, fake_k8s, allow_mutations, db_engine,
):
    """Like §14's router plan, and deliberately unlike §5.5's node debug pods.

    Nothing projected here is privileged: it is the caller's own request rendered
    as a Subscription against a catalog the cluster publishes to anyone who can
    read it.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })
    monkeypatch.setattr(portal_admin.settings, "portal_install_enabled", False)
    allow_preflight(fake_k8s)
    calls = stub_create(monkeypatch)

    result = portal_admin.subscribe(_plan_body(), dry_run=True)

    assert result["dryRun"] is True
    assert result["applied"] is False
    assert result["diff"]
    # The projection and the write are one request differing by one parameter.
    assert calls[0]["query"]["dryRun"] == "All"


# --------------------------------------------------------------------------- #
# Acknowledgement
# --------------------------------------------------------------------------- #

def _two_warning_cluster(monkeypatch):
    """A namespace with no OperatorGroup, subscribed with Manual approval.

    Two independent consequences, so "acknowledged" cannot be satisfied by
    naming whichever one the caller happened to read.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})


def test_a_write_with_unacknowledged_warnings_is_refused_naming_every_code(
    monkeypatch, fake_k8s, allow_subscribing,
):
    """Consenting to a consequence is a separate act from requesting the change.

    Both of these end with an operator that never installs; a subscribe that went
    through silently would report a create that is true and an installation that
    is not.
    """
    _two_warning_cluster(monkeypatch)
    allow_preflight(fake_k8s)

    with pytest.raises(Invalid) as caught:
        portal_admin.subscribe(
            _plan_body(installPlanApproval="Manual"), dry_run=False,
        )

    assert caught.value.context["unacknowledged"] == [
        portal_admin.WARN_NO_OPERATOR_GROUP,
        portal_admin.WARN_MANUAL_APPROVAL,
    ]


def test_acknowledging_only_some_of_the_codes_still_refuses(
    monkeypatch, fake_k8s, allow_subscribing,
):
    """Named rather than boolean, copied from §13's ``acknowledgeLossy``.

    A UI that acknowledged one list and then changed the namespace has to read
    the new one; a single "yes" checkbox would carry consent forward onto
    consequences nobody saw.
    """
    _two_warning_cluster(monkeypatch)
    allow_preflight(fake_k8s)

    with pytest.raises(Invalid) as caught:
        portal_admin.subscribe(
            _plan_body(installPlanApproval="Manual"),
            dry_run=False,
            acknowledge_consequences=[portal_admin.WARN_NO_OPERATOR_GROUP],
        )

    assert caught.value.context["unacknowledged"] == [portal_admin.WARN_MANUAL_APPROVAL]
    assert portal_admin.WARN_MANUAL_APPROVAL in caught.value.hint


def test_acknowledging_every_code_lets_the_write_through(
    monkeypatch, fake_k8s, allow_subscribing, db_engine,
):
    """The refusals above must be clearable, or the feature is a permanent no."""
    _two_warning_cluster(monkeypatch)
    allow_preflight(fake_k8s)
    stub_create(monkeypatch)

    result = portal_admin.subscribe(
        _plan_body(installPlanApproval="Manual"),
        dry_run=False,
        acknowledge_consequences=[
            portal_admin.WARN_NO_OPERATOR_GROUP,
            portal_admin.WARN_MANUAL_APPROVAL,
        ],
    )

    assert result["applied"] is True
    # And the consequences ride along on the response, because they are still
    # true after the create: this Subscription installs nothing.
    assert _codes(result) == [
        portal_admin.WARN_NO_OPERATOR_GROUP,
        portal_admin.WARN_MANUAL_APPROVAL,
    ]


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def test_a_clean_subscribe_creates_one_object_and_claims_nothing_more(
    monkeypatch, fake_k8s, allow_subscribing, db_engine,
):
    """`applied: true` means one Subscription exists. It is not an installation.

    OLM does that afterwards, on its own schedule. Nothing in this response may
    be read as evidence that an operator is running — which is why the CSV it
    carries is named ``expectedCSV`` and why no ``installed`` key exists here at
    all.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })
    allow_preflight(fake_k8s)
    calls = stub_create(monkeypatch)

    result = portal_admin.subscribe(_plan_body(), dry_run=False)

    assert result["applied"] is True
    assert result["verb"] == "create"
    assert result["expectedCSV"] == "prometheus.v1.0.0"
    assert result["expectedVersion"] == "1.0.0"
    assert result["installTarget"]["ready"] is True
    assert result["consequences"] == []
    assert "installed" not in result
    assert "installedCSV" not in result
    # One object, one POST, and no dryRun on the real write.
    assert [c["method"] for c in calls] == ["POST"]
    assert "dryRun" not in calls[0]["query"]
    assert calls[0]["path"].endswith(f"/namespaces/{NAMESPACE}/subscriptions")


def test_the_plans_consequences_do_not_overwrite_the_api_servers_own_warnings(
    monkeypatch, fake_k8s, allow_subscribing, db_engine,
):
    """§1.5 already owns ``warnings``, and it carries a different thing.

    Those are the cluster's own ``Warning:`` headers for this create — a
    deprecated apiVersion is the usual one — and dropping them to make room for
    the portal's consequences would silently discard the API server's notice
    about the very object being written.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })
    allow_preflight(fake_k8s)
    stub_create(monkeypatch, warnings=[
        "operators.coreos.com/v1alpha1 Subscription is deprecated",
    ])

    result = portal_admin.subscribe(_plan_body(), dry_run=False)

    assert result["warnings"] == [
        "operators.coreos.com/v1alpha1 Subscription is deprecated",
    ]
    assert result["consequences"] == []


def test_the_rendered_subscription_carries_exactly_what_the_operator_asked_for(
    monkeypatch, fake_k8s,
):
    """No labels of our own, no annotations, no ``config`` block.

    An operator reading the diff sees the fields they filled in and nothing else,
    and this console leaves no fingerprint on an object OLM goes on to manage.
    Naming it after the package is what makes a console subscribe and a
    ``kubectl`` one collide with a 409 instead of quietly coexisting.
    """
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })

    document = yaml.safe_load(portal_admin.plan(_plan_body())["document"])

    assert document["apiVersion"] == "operators.coreos.com/v1alpha1"
    assert document["kind"] == "Subscription"
    assert document["metadata"] == {"name": "prometheus", "namespace": NAMESPACE}
    assert document["spec"] == {
        "name": "prometheus",
        "channel": "stable",
        "source": CATALOG,
        "sourceNamespace": CATALOG_NAMESPACE,
        "installPlanApproval": "Automatic",
    }


def test_a_starting_csv_is_the_only_optional_field_added(monkeypatch, fake_k8s):
    """Present only when asked for: an empty ``startingCSV`` pins nothing."""
    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })

    document = yaml.safe_load(
        portal_admin.plan(_plan_body(startingCSV="prometheus.v0.9.0"))["document"]
    )

    assert document["spec"]["startingCSV"] == "prometheus.v0.9.0"


def test_the_audit_sentence_names_the_package_channel_and_catalog(
    monkeypatch, fake_k8s, allow_subscribing, db_session,
):
    """"Who subscribed us to this, from where, on which channel" is one question.

    A row saying only "create Subscription prometheus" answers none of it: the
    channel decides what version arrives and the catalog decides whose software
    it is.
    """
    from app.models import AuditRecord

    stub_olm(monkeypatch, lists={
        "packagemanifests": [_package()],
        "operatorgroups": [_operator_group()],
    })
    allow_preflight(fake_k8s)
    stub_create(monkeypatch)

    portal_admin.subscribe(_plan_body(), dry_run=False)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "applied"
    assert "prometheus" in record.detail
    assert "stable" in record.detail
    assert CATALOG in record.detail


# --------------------------------------------------------------------------- #
# The API surface
# --------------------------------------------------------------------------- #

def test_the_catalog_endpoint_echoes_the_gate_onto_the_envelope(
    client, cluster_id, monkeypatch, fake_k8s,
):
    """Rule 11.4: the Subscribe button is disabled *with* its reason on first paint.

    Fetched from a second endpoint it would render enabled for one round trip,
    which is one round trip in which somebody clicks it.
    """
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})

    response = client.get("/api/portal/catalog", params={"cluster_id": cluster_id})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert "ADMIN_ALLOW_MUTATIONS" in body["enabledDetail"]
    assert body["items"][0]["name"] == "prometheus"


def test_subscribing_through_the_api_is_refused_with_the_gate_off(
    client, cluster_id, monkeypatch, fake_k8s, allow_mutations,
):
    stub_olm(monkeypatch, lists={"packagemanifests": [_package()]})
    monkeypatch.setattr(portal_admin.settings, "portal_install_enabled", False)

    response = client.post(
        "/api/portal/subscriptions",
        params={"cluster_id": cluster_id},
        json={"package": "prometheus", "namespace": NAMESPACE, "dryRun": False},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "mutations_disabled"
