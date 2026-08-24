"""
The shipped router (§14) — installing a reverse proxy from a console.

This is the largest thing k8boss-admin writes, so the tests are mostly about
what it refuses to do:

* **It never adopts an object it did not create.** An install that finds a
  ClusterRole of the same name without the managed-by label refuses, names it,
  and writes nothing — on a dry run as much as on a real one.
* **A partial install is reported as partial.** Eight writes, and the sixth can
  fail. ``installed`` is false unless every one landed, and there is no silent
  rollback.
* **Uninstall leaves the namespace standing** and says so.
* **Status is a tri-state.** ``installed: null`` during an API outage, never
  ``false`` — which would invite an operator to install a second router on top
  of the one already running.
"""

from __future__ import annotations

import pytest

from app.admin import router as router_service
from app.admin import router_bundle
from app.errors import Conflict, Invalid, MutationsDisabled, NotFound, RBACDenied
from app.resources import catalog
from tests.test_routes import stub_discovery as _stub_routes_discovery


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


@pytest.fixture
def allow_router(monkeypatch, allow_mutations):
    """Both gates open, which is what a real install needs."""
    monkeypatch.setattr(router_service.settings, "router_manage_enabled", True)
    return router_service.settings


def stub_discovery(monkeypatch):
    """Discovery covering every group the bundle writes into."""
    from tests import test_routes

    payloads = dict(test_routes._PAYLOADS)
    payloads["/api/v1"] = test_routes._resources(
        ("services", "Service"), ("serviceaccounts", "ServiceAccount"),
        ("configmaps", "ConfigMap"),
    )
    # Namespaces are cluster-scoped; the shared helper marks everything
    # namespaced, so this group-version is built here rather than reused.
    payloads["/api/v1"]["resources"].append({
        "name": "namespaces", "kind": "Namespace", "namespaced": False,
        "verbs": ["get", "list", "create", "update", "delete"],
    })
    payloads["/apis/apps/v1"] = test_routes._resources(("deployments", "Deployment"))
    payloads["/apis/rbac.authorization.k8s.io/v1"] = {
        "resources": [
            {"name": n, "kind": k, "namespaced": False,
             "verbs": ["get", "list", "create", "update", "patch", "delete"]}
            for n, k in (("clusterroles", "ClusterRole"),
                         ("clusterrolebindings", "ClusterRoleBinding"))
        ]
    }
    payloads["/apis/networking.k8s.io/v1"] = {
        "resources": [
            {"name": "ingresses", "kind": "Ingress", "namespaced": True,
             "verbs": ["get", "list", "create", "update", "patch", "delete"]},
            {"name": "ingressclasses", "kind": "IngressClass", "namespaced": False,
             "verbs": ["get", "list", "create", "update", "patch", "delete"]},
        ]
    }
    groups = [
        ("apps", "v1"), ("rbac.authorization.k8s.io", "v1"),
        ("networking.k8s.io", "v1"), ("route.openshift.io", "v1"),
        ("gateway.networking.k8s.io", "v1"),
    ]

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return test_routes._groups_payload(*groups)
        if path in payloads:
            return payloads[path]
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)


def allow_preflight(fake_k8s, *, allowed=True):
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review",
        lambda body, **kw: type(
            "R", (), {"status": type("S", (), {
                "allowed": allowed, "reason": "", "evaluation_error": None,
            })()},
        )(),
    )


# --------------------------------------------------------------------------- #
# The bundle is data
# --------------------------------------------------------------------------- #

def test_the_bundle_pins_its_image():
    """`:latest` means the router silently changes major version on a pod restart."""
    assert ":" in router_bundle.ROUTER_IMAGE.rsplit("/", 1)[-1]
    assert router_bundle.ROUTER_IMAGE.endswith(router_bundle.ROUTER_VERSION)
    assert not router_bundle.ROUTER_IMAGE.endswith(":latest")


def test_the_bundle_creates_an_ingress_class():
    """Not in the upstream manifest, and §13 writes spec.ingressClassName."""
    objects = router_bundle.build(router_bundle.RouterOptions())
    classes = [o for o in objects if o.kind == "IngressClass"]

    assert len(classes) == 1
    assert classes[0].body["spec"]["controller"] == router_bundle.INGRESS_CONTROLLER + "/haproxy"


@pytest.mark.parametrize("class_name", ["haproxy", "edge", "k8boss-router"])
def test_the_class_controller_string_agrees_with_the_deployments_ingress_class_flag(
    class_name,
):
    """The two halves of the bundle that have to match, asserted against each other.

    The controller accepts an IngressClass only when its ``spec.controller`` is
    ``haproxy.org/ingress-controller/<value of --ingress.class>``. Ship the bare
    string beside the flag and it matches nothing: every Ingress naming the class
    is dropped, and the router serves no traffic at all while reporting itself
    installed, Available and Ready.

    Written as an agreement between the two objects rather than against a
    constant on purpose. The version of this test that compared
    ``spec.controller`` to ``INGRESS_CONTROLLER`` passed for as long as the
    defect existed, because the constant *was* the wrong value — a test and a
    bug that agree with each other prove only that they agree.
    """
    options = router_bundle.RouterOptions(ingress_class_name=class_name)
    objects = router_bundle.build(options)

    controller = next(o for o in objects if o.kind == "IngressClass").body["spec"]["controller"]
    deployment = next(o for o in objects if o.kind == "Deployment")
    args = deployment.body["spec"]["template"]["spec"]["containers"][0]["args"]
    flag = next(a.split("=", 1)[1] for a in args if a.startswith("--ingress.class="))

    assert controller == f"{router_bundle.INGRESS_CONTROLLER}/{flag}", (
        "The IngressClass controller string and --ingress.class must agree, or "
        "the router admits nothing while looking perfectly healthy."
    )

    # The second half of the rule, and the one that is not in upstream's
    # ingressclass.md: the class's *name* must equal the flag value too.
    # Verified in isolation — a class named `ic2-class` carrying the correct
    # `.../ic2` controller string, with `--ingress.class=ic2`, is never matched;
    # renaming it to `ic2` and changing nothing else is picked up in 15 seconds.
    # The bundle derives both from one option so they cannot drift today, which
    # is exactly why it is worth pinning: the coupling is invisible.
    name = next(o for o in objects if o.kind == "IngressClass").body["metadata"]["name"]
    assert name == flag, (
        "The IngressClass name must equal --ingress.class. A class whose "
        "controller string is right but whose name differs is never matched."
    )


def test_the_controller_publishes_its_service_so_ingress_status_gets_an_address():
    """Without --publish-service, §13's `addresses` is empty for every Ingress, forever."""
    objects = router_bundle.build(router_bundle.RouterOptions(namespace="rt"))
    deployment = next(o for o in objects if o.kind == "Deployment")
    args = deployment.body["spec"]["template"]["spec"]["containers"][0]["args"]

    assert any(a.startswith("--publish-service=rt/") for a in args)


def test_every_object_carries_the_managed_by_label():
    """It is how the console finds what it installed without storing anything."""
    for item in router_bundle.build(router_bundle.RouterOptions()):
        labels = item.body["metadata"]["labels"]
        assert labels[router_service.MANAGED_BY_LABEL] == router_bundle.MANAGED_BY
        assert labels[router_bundle.VERSION_LABEL] == router_bundle.ROUTER_VERSION


def test_the_deployment_selector_excludes_the_version_label():
    """A selector is immutable; including the version makes every upgrade an outage."""
    objects = router_bundle.build(router_bundle.RouterOptions())
    deployment = next(o for o in objects if o.kind == "Deployment")

    assert router_bundle.VERSION_LABEL not in deployment.body["spec"]["selector"]["matchLabels"]
    assert router_bundle.VERSION_LABEL in (
        deployment.body["spec"]["template"]["metadata"]["labels"]
    )


def test_the_service_does_not_publish_the_statistics_port():
    """Upstream exposes it; on a LoadBalancer that gives it a public IP."""
    objects = router_bundle.build(router_bundle.RouterOptions())
    service = next(o for o in objects if o.kind == "Service")

    assert [p["name"] for p in service.body["spec"]["ports"]] == ["http", "https"]


def test_the_default_class_annotation_is_off_unless_asked_for():
    """It makes this router claim every unclassed Ingress in the cluster."""
    plain = router_bundle.build(router_bundle.RouterOptions())
    ingress_class = next(o for o in plain if o.kind == "IngressClass")
    assert "annotations" not in ingress_class.body["metadata"]

    defaulted = router_bundle.build(router_bundle.RouterOptions(default_class=True))
    ingress_class = next(o for o in defaulted if o.kind == "IngressClass")
    assert (
        ingress_class.body["metadata"]["annotations"][
            "ingressclass.kubernetes.io/is-default-class"
        ] == "true"
    )


def test_gateway_api_rbac_is_only_granted_when_asked_for():
    plain = router_bundle.build(router_bundle.RouterOptions())
    role = next(o for o in plain if o.kind == "ClusterRole")
    groups = {g for rule in role.body["rules"] for g in rule["apiGroups"]}
    assert "gateway.networking.k8s.io" not in groups

    with_gw = router_bundle.build(router_bundle.RouterOptions(gateway_api=True))
    role = next(o for o in with_gw if o.kind == "ClusterRole")
    groups = {g for rule in role.body["rules"] for g in rule["apiGroups"]}
    assert "gateway.networking.k8s.io" in groups


def test_zero_replicas_is_refused():
    """A router scaled to zero is an ingress outage that looks like a config change."""
    from app.errors import Invalid

    with pytest.raises(Invalid):
        router_bundle.validate_options({"replicas": 0})


def test_an_unknown_service_type_is_refused_naming_the_valid_ones():
    from app.errors import Invalid

    with pytest.raises(Invalid) as caught:
        router_bundle.validate_options({"serviceType": "Magic"})

    assert "LoadBalancer" in caught.value.detail




def test_the_shipped_manifest_matches_the_bundle():
    """`deploy/router.yaml` and what the console installs are the same objects.

    Without this the repo documents one router and the console installs another
    — the same defect class as an advisory lint: a check that exists, passes and
    means nothing. Regenerate with `make router-manifest`.
    """
    import importlib.util
    import pathlib as _pathlib

    root = _pathlib.Path(__file__).resolve().parent.parent.parent
    script = root / "scripts" / "render-router-manifest.py"
    spec = importlib.util.spec_from_file_location("render_router_manifest", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    on_disk = (root / "deploy" / "router.yaml").read_text()

    assert on_disk == module.render(), (
        "deploy/router.yaml is out of date with app/admin/router_bundle.py. "
        "Run `make router-manifest`."
    )


def test_the_shipped_manifest_is_the_default_options():
    """The file is what an operator gets if they change nothing in the dialog."""
    import yaml as _yaml

    import pathlib as _pathlib

    root = _pathlib.Path(__file__).resolve().parent.parent.parent
    documents = [
        d for d in _yaml.safe_load_all((root / "deploy" / "router.yaml").read_text())
        if d is not None
    ]

    assert [d["kind"] for d in documents] == [
        item.kind for item in router_bundle.build(router_bundle.RouterOptions())
    ]
    deployment = next(d for d in documents if d["kind"] == "Deployment")
    assert deployment["spec"]["replicas"] == 2
    service = next(d for d in documents if d["kind"] == "Service")
    assert service["spec"]["type"] == "LoadBalancer"
    ingress_class = next(d for d in documents if d["kind"] == "IngressClass")
    # Not the cluster default: the file must not do the one thing the dialog
    # makes an operator type a confirmation for.
    assert "annotations" not in ingress_class["metadata"]


# --------------------------------------------------------------------------- #
# The plan is pure and ungated
# --------------------------------------------------------------------------- #

def test_the_plan_renders_with_both_gates_shut(fake_k8s):
    """Deciding whether to turn the gate on requires reading what it would create."""
    result = router_service.plan({})

    assert result["version"] == router_bundle.ROUTER_VERSION
    assert len(result["objects"]) == 8
    assert all(o["yaml"] for o in result["objects"])


def test_the_plan_writes_no_audit_row(fake_k8s, db_session):
    from app.models import AuditRecord

    before = db_session.query(AuditRecord).count()

    router_service.plan({})

    assert db_session.query(AuditRecord).count() == before


def test_the_plan_says_the_shipped_router_does_not_serve_httproutes():
    """The controller implements Gateway API for TCPRoute only. Said out loud."""
    result = router_service.plan({"gatewayApi": True})
    gateway = next(s for s in result["serves"] if s["backend"] == "gateway")

    assert gateway["served"] is False
    assert "TCPRoute only" in gateway["detail"]


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_a_real_install_is_refused_when_the_feature_gate_is_off(
    monkeypatch, fake_k8s, allow_mutations,
):
    stub_discovery(monkeypatch)
    monkeypatch.setattr(router_service.settings, "router_manage_enabled", False)

    with pytest.raises(MutationsDisabled) as caught:
        router_service.install({}, dry_run=False)

    assert "ADMIN_ROUTER_MANAGE_ENABLED" in caught.value.hint


def test_the_refusal_is_audited(monkeypatch, fake_k8s, allow_mutations, db_session):
    """Somebody trying to install a cluster-wide router on a console where that is off."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    monkeypatch.setattr(router_service.settings, "router_manage_enabled", False)

    with pytest.raises(MutationsDisabled):
        router_service.install({}, dry_run=False)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "denied"
    assert "router install refused" in record.detail


def test_a_dry_run_is_permitted_with_the_feature_gate_off(
    monkeypatch, fake_k8s, allow_mutations,
):
    """Deliberately unlike §5.5's node debug pods — see `_require_enabled`."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    monkeypatch.setattr(router_service.settings, "router_manage_enabled", False)
    _stub_absent_and_writable(monkeypatch)

    result = router_service.install({}, dry_run=True)

    assert result["dryRun"] is True
    assert result["installed"] is False
    assert result["failed"] == 0


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

def _stub_absent_and_writable(monkeypatch, *, fail_on=None):
    """Nothing exists yet, and every write succeeds — except ``fail_on`` kinds."""
    fail_on = fail_on or set()

    def fake_get(group, version, plural, name, namespace=None):
        raise NotFound(f"{plural}/{name} not found", context={"resource": plural})

    def fake_request_json(method, path, **kwargs):
        for kind in fail_on:
            if kind in path:
                raise RBACDenied(
                    f"{kind} is forbidden", context={"resource": kind},
                )
        body = kwargs.get("body") or {}
        return (
            {**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}},
            [],
        )

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        router_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(router_service.apply_service, "request_json", fake_request_json)


def test_a_clean_install_creates_every_object_and_reports_installed(
    monkeypatch, fake_k8s, allow_router,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = router_service.install({}, dry_run=False)

    assert result["installed"] is True
    assert result["failed"] == 0
    assert len(result["objects"]) == 8
    assert all(o["verb"] == "create" for o in result["objects"])
    assert all(o["applied"] for o in result["objects"])


def test_a_dry_run_install_never_reports_installed(monkeypatch, fake_k8s, allow_router):
    """§1.5, applied to the cluster's ingress path."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = router_service.install({}, dry_run=True)

    assert result["installed"] is False
    assert all(o["applied"] is False for o in result["objects"])


def test_one_failed_object_makes_the_whole_install_not_installed(
    monkeypatch, fake_k8s, allow_router,
):
    """"Installed" over a half-created router leaves somebody debugging a dead path."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, fail_on={"clusterroles"})

    result = router_service.install({}, dry_run=False)

    assert result["installed"] is False
    assert result["failed"] >= 1
    failed = [o for o in result["objects"] if o["error"]]
    assert failed
    assert failed[0]["error"]["code"] == "rbac_denied"


def test_the_install_keeps_going_past_a_failure(monkeypatch, fake_k8s, allow_router):
    """One error and seven unknowns is not the same information as eight answers."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, fail_on={"clusterroles"})

    result = router_service.install({}, dry_run=False)

    assert len(result["objects"]) == 8
    assert sum(1 for o in result["objects"] if o["applied"]) >= 6


def test_an_object_the_console_did_not_create_is_never_adopted(
    monkeypatch, fake_k8s, allow_router,
):
    """The worst case: the operator asked for an install and got a silent takeover."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "clusterroles":
            return {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "Helm"}},
            }
        raise NotFound("not found", context={"resource": plural})

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)

    with pytest.raises(Conflict) as caught:
        router_service.install({}, dry_run=False)

    assert "did not create it" in caught.value.message
    assert "Helm" in caught.value.detail


def test_the_takeover_refusal_happens_on_a_dry_run_too(
    monkeypatch, fake_k8s, allow_router,
):
    """A refusal at confirm time is a refusal the operator should have had at preview."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "namespaces":
            return {"apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": name, "labels": {}}}
        raise NotFound("not found", context={"resource": plural})

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)

    with pytest.raises(Conflict):
        router_service.install({}, dry_run=True)


def test_the_takeover_check_writes_nothing_before_it_refuses(
    monkeypatch, fake_k8s, allow_router,
):
    """A per-object check inside the loop would create five and then refuse."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    writes: list[str] = []

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "services":
            return {"apiVersion": "v1", "kind": "Service",
                    "metadata": {"name": name, "namespace": namespace, "labels": {}}}
        raise NotFound("not found", context={"resource": plural})

    def record_write(method, path, **kwargs):
        writes.append(path)
        return ({}, [])

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(router_service.apply_service, "request_json", record_write)

    with pytest.raises(Conflict):
        router_service.install({}, dry_run=False)

    assert writes == []


def test_reinstalling_replaces_the_objects_the_console_owns(
    monkeypatch, fake_k8s, allow_router,
):
    """An upgrade is the same eight writes: create if absent, replace if already ours."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        return {
            "apiVersion": f"{group}/{version}" if group else version,
            "kind": "Whatever",
            "metadata": {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
                "resourceVersion": "50",
                "labels": {
                    router_service.MANAGED_BY_LABEL: router_bundle.MANAGED_BY,
                    router_bundle.VERSION_LABEL: "3.1.0",
                },
            },
        }

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        router_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(
        router_service.apply_service, "request_json",
        lambda method, path, **kw: (
            {**(kw.get("body") or {}),
             "metadata": {**((kw.get("body") or {}).get("metadata") or {}),
                          "resourceVersion": "51"}},
            [],
        ),
    )

    result = router_service.install({}, dry_run=False)

    assert result["installed"] is True
    assert all(o["verb"] == "update" for o in result["objects"])


def test_every_installed_object_gets_its_own_audit_row(
    monkeypatch, fake_k8s, allow_router, db_session,
):
    """"Who created the ClusterRole" has to be answerable."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)
    before = db_session.query(AuditRecord).count()

    router_service.install({}, dry_run=False)

    assert db_session.query(AuditRecord).count() == before + 8




def test_an_install_goes_through_the_real_transport(monkeypatch, fake_k8s, allow_router):
    """One install exercised end to end against the fake that raises on surprises.

    The other install tests patch `apply_service.request_json` out, which skips
    URL construction, the dryRun query parameter and the three-tuple the funnel
    asks for. Stubbing `api_client.call_api` instead runs all of it — and an
    unstubbed call still raises, which is the whole point of that fake.
    """
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    calls: list = []

    def fake_get(group, version, plural, name, namespace=None):
        raise NotFound("not found", context={"resource": plural})

    def call_api(path, method, **kwargs):
        calls.append((method, path, dict(kwargs.get("query_params") or [])))
        body = kwargs.get("body") or {}
        return (
            {**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}},
            200,
            {},
        )

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    fake_k8s.api_client.returns("call_api", call_api)

    result = router_service.install({}, dry_run=False)

    assert result["installed"] is True
    assert len(calls) == 8
    assert {c[0] for c in calls} == {"POST"}
    # The cluster-scoped objects address a non-namespaced path, and the
    # namespaced ones do not — a mistake here creates the ClusterRole inside a
    # namespace, where nothing binds it.
    paths = [c[1] for c in calls]
    assert any("/apis/rbac.authorization.k8s.io/v1/clusterroles" == p for p in paths)
    assert any(p.endswith("/namespaces/k8boss-router/deployments") for p in paths)
    # A real install sends no dryRun; that difference is the only one.
    assert all("dryRun" not in c[2] for c in calls)


def test_a_dry_run_install_sends_dry_run_all_on_every_object(
    monkeypatch, fake_k8s, allow_router,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    calls: list = []

    monkeypatch.setattr(
        router_service.reader, "get_resource",
        lambda *a, **k: (_ for _ in ()).throw(NotFound("gone", context={})),
    )
    fake_k8s.api_client.returns(
        "call_api",
        lambda path, method, **kwargs: (
            calls.append(dict(kwargs.get("query_params") or [])),
            ({**(kwargs.get("body") or {})}, 200, {}),
        )[1],
    )

    router_service.install({}, dry_run=True)

    assert len(calls) == 8
    assert all(c.get("dryRun") == "All" for c in calls)


# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #

def _stub_ours_and_deletable(monkeypatch, *, unowned=()):
    def fake_get(group, version, plural, name, namespace=None):
        labels = (
            {}
            if plural in unowned
            else {router_service.MANAGED_BY_LABEL: router_bundle.MANAGED_BY}
        )
        return {
            "apiVersion": f"{group}/{version}" if group else version,
            "kind": "Whatever",
            "metadata": {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
                "resourceVersion": "50",
                "labels": labels,
            },
        }

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        router_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(
        router_service.apply_service, "request_json", lambda *a, **k: ({}, []),
    )


def test_uninstall_leaves_the_namespace_standing_and_says_so(
    monkeypatch, fake_k8s, allow_router,
):
    """Deleting a namespace deletes everything in it and cannot be undone."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_ours_and_deletable(monkeypatch)

    result = router_service.uninstall({}, dry_run=False)

    assert [o["kind"] for o in result["objects"]].count("Namespace") == 0
    assert result["retained"][0]["kind"] == "Namespace"
    assert "cannot be undone" in result["retained"][0]["reason"]


def test_uninstall_skips_objects_the_console_did_not_create(
    monkeypatch, fake_k8s, allow_router,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_ours_and_deletable(monkeypatch, unowned={"ingressclasses"})

    result = router_service.uninstall({}, dry_run=False)

    skipped = {s["kind"] for s in result["skipped"]}
    assert "IngressClass" in skipped
    assert all(o["kind"] != "IngressClass" for o in result["objects"])


def test_uninstall_reports_what_is_already_gone_rather_than_failing(
    monkeypatch, fake_k8s, allow_router,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    monkeypatch.setattr(
        router_service.reader, "get_resource",
        lambda *a, **k: (_ for _ in ()).throw(NotFound("gone", context={})),
    )

    result = router_service.uninstall({}, dry_run=False)

    assert result["removed"] == 0
    assert result["failed"] == 0
    assert len(result["skipped"]) == 7
    assert all("not on the cluster" in s["reason"] for s in result["skipped"])


def test_a_dry_run_uninstall_never_reports_uninstalled(
    monkeypatch, fake_k8s, allow_router,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_ours_and_deletable(monkeypatch)

    result = router_service.uninstall({}, dry_run=True)

    assert result["uninstalled"] is False


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def _stub_status(monkeypatch, objects, *, classes=None):
    """``objects`` maps plural -> object or an exception to raise.

    ``classes`` is the IngressClass listing `status` makes to answer "what else
    is serving Ingresses here". Stubbed separately because it is a *list* and
    the single-object map cannot express one; pass an exception to make it fail.
    """

    def fake_get(group, version, plural, name, namespace=None):
        value = objects.get(plural)
        if value is None:
            raise NotFound("not found", context={"resource": plural})
        if isinstance(value, Exception):
            raise value
        return value

    def fake_list(group, version, plural, **kwargs):
        if isinstance(classes, Exception):
            raise classes
        return {
            "items": list(classes or []), "continue": None, "remaining": None,
            "partial": False, "unavailable": [],
        }

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(router_service.reader, "list_resource", fake_list)


def _binding(namespace="k8boss-router"):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": router_bundle.NAME},
        "subjects": [{"kind": "ServiceAccount", "name": router_bundle.NAME,
                      "namespace": namespace}],
    }


def _deployment(*, version=None, ready=2, desired=2, generation=3, observed=3):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": router_bundle.NAME, "namespace": "k8boss-router",
            "generation": generation,
            "labels": {router_bundle.VERSION_LABEL: version or router_bundle.ROUTER_VERSION},
        },
        "spec": {"replicas": desired, "template": {"spec": {"containers": [
            {"image": router_bundle.ROUTER_IMAGE},
        ]}}},
        "status": {"readyReplicas": ready, "observedGeneration": observed},
    }


def test_status_reports_not_installed_when_nothing_is_there(monkeypatch, fake_k8s):
    _stub_status(monkeypatch, {})

    result = router_service.status()

    assert result["installed"] is False
    assert result["partial"] is False


def test_status_finds_the_namespace_off_the_cluster_rather_than_a_setting(
    monkeypatch, fake_k8s,
):
    """A router installed into a namespace this deployment is no longer configured for."""
    _stub_status(monkeypatch, {
        "clusterrolebindings": _binding("somewhere-else"),
        "deployments": _deployment(),
    })

    result = router_service.status()

    assert result["namespace"] == "somewhere-else"
    assert result["namespaceDiscovered"] is True


def test_status_is_unknown_not_false_when_a_read_fails(monkeypatch, fake_k8s):
    """`false` during an outage invites a second router on top of the running one."""
    _stub_status(monkeypatch, {
        "clusterrolebindings": _binding(),
        "deployments": RBACDenied("deployments is forbidden", context={}),
    })

    result = router_service.status()

    assert result["installed"] is None
    assert result["partial"] is True


def test_ready_replicas_is_null_while_the_controller_has_not_reported(
    monkeypatch, fake_k8s,
):
    """"0 of 2 ready" when nobody could look sends an operator to restart a healthy router."""
    _stub_status(monkeypatch, {
        "clusterrolebindings": _binding(),
        "deployments": _deployment(generation=4, observed=3),
    })

    result = router_service.status()

    assert result["deployment"]["readyReplicas"] is None
    assert "has not yet reported" in result["deployment"]["detail"]


def test_an_older_installed_version_offers_an_upgrade(monkeypatch, fake_k8s):
    _stub_status(monkeypatch, {
        "clusterrolebindings": _binding(),
        "deployments": _deployment(version="3.1.0"),
    })

    result = router_service.status()

    assert result["installedVersion"] == "3.1.0"
    assert result["upgradeAvailable"] is True


def test_upgrade_available_is_null_when_the_installed_version_is_unknown(
    monkeypatch, fake_k8s,
):
    """"No upgrade available" is a claim an unreadable Deployment does not support."""
    _stub_status(monkeypatch, {"clusterrolebindings": _binding()})

    result = router_service.status()

    assert result["installedVersion"] is None
    assert result["upgradeAvailable"] is None


def test_a_loadbalancer_with_no_address_says_what_that_means(monkeypatch, fake_k8s):
    _stub_status(monkeypatch, {
        "clusterrolebindings": _binding(),
        "deployments": _deployment(),
        "services": {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": router_bundle.NAME, "namespace": "k8boss-router"},
            "spec": {"type": "LoadBalancer", "ports": []},
            "status": {},
        },
    })

    result = router_service.status()

    assert result["service"]["addresses"] == []
    assert "no load-balancer provider" in result["service"]["detail"]


def test_status_says_the_shipped_router_does_not_serve_routes_or_httproutes(
    monkeypatch, fake_k8s,
):
    _stub_status(monkeypatch, {})

    result = router_service.status()
    served = {s["backend"]: s["served"] for s in result["serves"]}

    assert served == {"ingress": True, "gateway": False, "openshift": False}




def _ingress_class(name, controller, *, default=False, managed=False):
    labels = (
        {router_service.MANAGED_BY_LABEL: router_bundle.MANAGED_BY} if managed else {}
    )
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "IngressClass",
        "metadata": {
            "name": name,
            "labels": labels,
            **(
                {"annotations": {"ingressclass.kubernetes.io/is-default-class": "true"}}
                if default else {}
            ),
        },
        "spec": {"controller": controller},
    }


def test_status_names_what_else_is_serving_ingresses(monkeypatch, fake_k8s):
    """Offering an install without saying nginx is already here is offering it blind."""
    _stub_status(
        monkeypatch, {},
        classes=[_ingress_class("nginx", "k8s.io/ingress-nginx", default=True)],
    )

    result = router_service.status()

    assert result["otherClasses"] == [
        {
            "name": "nginx",
            "controller": "k8s.io/ingress-nginx",
            "default": True,
            "managedByUs": False,
            "retired": router_service.RETIRED_CONTROLLERS["k8s.io/ingress-nginx"],
        }
    ]


def test_the_retirement_advisory_matches_the_controller_exactly_not_the_word_nginx(
    monkeypatch, fake_k8s,
):
    """F5's NGINX controller is a different, supported product.

    Telling an operator their actively released controller is retired is the
    confidently-wrong answer aimed at their whole ingress path.
    """
    _stub_status(
        monkeypatch, {},
        classes=[_ingress_class("nginx", "nginx.org/ingress-controller")],
    )

    result = router_service.status()

    assert result["otherClasses"][0]["retired"] is None


def test_other_classes_is_null_when_the_listing_failed(monkeypatch, fake_k8s):
    """`[]` would say "nothing else is serving Ingresses", which is a real claim."""
    _stub_status(monkeypatch, {}, classes=RBACDenied("forbidden", context={}))

    result = router_service.status()

    assert result["otherClasses"] is None
    assert result["partial"] is True


def test_an_unreadable_deployment_is_present_none_not_present_false(
    monkeypatch, fake_k8s,
):
    """"Deployment: absent" rendered from a read nobody could make."""
    _stub_status(monkeypatch, {
        "clusterrolebindings": _binding(),
        "deployments": RBACDenied("deployments is forbidden", context={}),
    })

    result = router_service.status()

    assert result["deployment"]["present"] is None
    assert "not absent" in result["deployment"]["detail"]


def test_a_genuinely_absent_deployment_is_present_false(monkeypatch, fake_k8s):
    _stub_status(monkeypatch, {"clusterrolebindings": _binding()})

    result = router_service.status()

    assert result["deployment"]["present"] is False
    assert result["deployment"]["detail"] is None


def test_escalation_prevention_gets_a_hint_naming_escalate_not_the_verb(
    monkeypatch, fake_k8s, allow_router,
):
    """The one 403 preflight cannot see coming.

    SelfSubjectAccessReview says "yes, you may create clusterroles"; the create
    then fails because the role grants more than the caller holds. Left alone,
    the operator is sent to grant a verb they demonstrably have.
    """
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        raise NotFound("not found", context={"resource": plural})

    def fake_request(method, path, **kwargs):
        if "clusterroles" in path:
            raise RBACDenied(
                "clusterroles is forbidden",
                # Verbatim from a live API server (1.31), not paraphrased.
                # The invented wording this used to carry is what let the
                # marker sit wrong in app/admin/router.py while this test
                # stayed green.
                detail=(
                    'clusterroles.rbac.authorization.k8s.io "k8boss-admin-router" '
                    'is forbidden: user '
                    '"system:serviceaccount:k8boss-admin:k8boss-admin" '
                    '(groups=["system:authenticated"]) is attempting to grant '
                    'RBAC permissions not currently held:\n'
                    '{APIGroups:[""], Resources:["ingressclasses"], '
                    'Verbs:["get" "list" "watch"]}'
                ),
                context={"resource": "clusterroles"},
            )
        body = kwargs.get("body") or {}
        return ({**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}}, [])

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        router_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(router_service.apply_service, "request_json", fake_request)

    result = router_service.install({}, dry_run=False)

    failed = next(o for o in result["objects"] if o["kind"] == "ClusterRole")
    assert "escalation prevention" in failed["error"]["hint"]
    assert "escalate" in failed["error"]["hint"]
    # And the code is untouched: only the hint is rewritten.
    assert failed["error"]["code"] == "rbac_denied"


def test_a_binding_orphaned_by_its_clusterrole_says_so_not_404(
    monkeypatch, fake_k8s, allow_router,
):
    """The half-installed report has to name the object that is actually missing.

    When the ClusterRole create fails and the caller does not hold `bind`, the
    API server answers the ClusterRoleBinding create with a 404 naming *the
    binding*. Relayed unchanged that sends the operator to inspect an object
    with nothing wrong with it, while the real cause sits one row up in the
    same report.
    """
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        raise NotFound("not found", context={"resource": plural})

    def fake_request(method, path, **kwargs):
        if "clusterrolebindings" in path:
            # What the API server really returns here: 404, naming the binding.
            raise NotFound(
                'No such object: create rbac.authorization.k8s.io/'
                'clusterrolebindings "k8boss-admin-router".',
                detail=(
                    'clusterrolebindings.rbac.authorization.k8s.io '
                    '"k8boss-admin-router" not found'
                ),
                context={"resource": "clusterrolebindings"},
            )
        if "clusterroles" in path:
            raise RBACDenied(
                "clusterroles is forbidden",
                detail="is attempting to grant RBAC permissions not currently held",
                context={"resource": "clusterroles"},
            )
        body = kwargs.get("body") or {}
        return ({**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}}, [])

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        router_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(router_service.apply_service, "request_json", fake_request)

    result = router_service.install({}, dry_run=False)

    binding = next(o for o in result["objects"] if o["kind"] == "ClusterRoleBinding")
    # The cause named is the ClusterRole, not the binding's own absence.
    assert "ClusterRole" in binding["error"]["message"]
    assert "failed earlier" in binding["error"]["message"]
    assert "no problem of its own" in binding["error"]["hint"]
    # The cluster's own answer is not overwritten, only explained.
    assert binding["error"]["code"] == "not_found"
    assert binding["applied"] is False
    # And the install is honest about the whole thing.
    assert result["installed"] is False
    assert result["failed"] == 2


def test_a_binding_that_fails_on_its_own_is_not_blamed_on_the_clusterrole(
    monkeypatch, fake_k8s, allow_router,
):
    """The rewrite must not fire when the ClusterRole landed fine.

    Otherwise the console invents a cause for a failure that has its own, which
    is the same defect in the other direction.
    """
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        raise NotFound("not found", context={"resource": plural})

    def fake_request(method, path, **kwargs):
        if "clusterrolebindings" in path:
            raise RBACDenied(
                "clusterrolebindings is forbidden",
                detail="forbidden",
                context={"resource": "clusterrolebindings"},
            )
        body = kwargs.get("body") or {}
        return ({**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}}, [])

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        router_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(router_service.apply_service, "request_json", fake_request)

    result = router_service.install({}, dry_run=False)

    binding = next(o for o in result["objects"] if o["kind"] == "ClusterRoleBinding")
    assert binding["error"]["code"] == "rbac_denied"
    assert "failed earlier" not in (binding["error"]["message"] or "")
    assert result["failed"] == 1


def test_the_takeover_refusal_is_audited(monkeypatch, fake_k8s, allow_router, db_session):
    """"Did anyone try to install a router over my ClusterRole" has to be answerable."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "clusterroles":
            return {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "Helm"}},
            }
        raise NotFound("not found", context={"resource": plural})

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)

    with pytest.raises(Conflict):
        router_service.install({}, dry_run=False)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "conflict"
    assert "not managed by this console" in record.detail


def test_the_takeover_refusal_is_audited_on_a_dry_run_too(
    monkeypatch, fake_k8s, allow_router, db_session,
):
    """The attempt happened either way, and dry_run records which kind it was."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "namespaces":
            return {"apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": name, "labels": {}}}
        raise NotFound("not found", context={"resource": plural})

    monkeypatch.setattr(router_service.reader, "get_resource", fake_get)

    with pytest.raises(Conflict):
        router_service.install({}, dry_run=True)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "conflict"
    assert record.dry_run is True


# --------------------------------------------------------------------------- #
# The API surface
# --------------------------------------------------------------------------- #

def test_the_router_status_endpoint_answers(client, cluster_id, monkeypatch, fake_k8s):
    _stub_status(monkeypatch, {})

    response = client.get("/api/router", params={"cluster_id": cluster_id})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["shippedVersion"] == router_bundle.ROUTER_VERSION


def test_the_plan_endpoint_answers_with_both_gates_shut(
    client, cluster_id, fake_k8s,
):
    response = client.post(
        "/api/router/plan", params={"cluster_id": cluster_id}, json={},
    )

    assert response.status_code == 200
    assert len(response.json()["objects"]) == 8


def test_installing_through_the_api_is_refused_with_the_gate_off(
    client, cluster_id, monkeypatch, fake_k8s, allow_mutations,
):
    stub_discovery(monkeypatch)
    monkeypatch.setattr(router_service.settings, "router_manage_enabled", False)

    response = client.post(
        "/api/router", params={"cluster_id": cluster_id}, json={"dryRun": False},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "mutations_disabled"


def test_an_ingressclass_that_cannot_be_upgraded_says_what_to_do_about_it():
    """"field is immutable" is accurate and tells the operator nothing to do.

    A cluster carrying a router installed before the controller string was
    corrected cannot be upgraded in place — spec.controller cannot change — so
    the install stops at seven of eight objects, forever, until somebody deletes
    one object. The verbatim API server detail names the field and not the
    remedy, and this is the difference between a stuck install and a fixed one.
    """
    item = next(
        o for o in router_bundle.build(router_bundle.RouterOptions())
        if o.kind == "IngressClass"
    )
    error = Invalid(
        'The cluster rejected update networking.k8s.io/ingressclasses "haproxy".',
        detail=(
            'IngressClass.networking.k8s.io "haproxy" is invalid: spec.controller: '
            'Invalid value: "haproxy.org/ingress-controller/haproxy": field is immutable'
        ),
    )

    rewritten = router_service._immutable_class_hint(item, error)

    assert "kubectl delete ingressclass haproxy" in rewritten.hint
    # Still `invalid`: the API server refused it, and the frontend branches on
    # the code, not the prose.
    assert rewritten.code == "invalid"


def test_an_unrelated_ingressclass_failure_is_not_given_the_delete_advice():
    """The hint is for one cause. Told to delete their class over an RBAC denial,
    an operator would take a working object away for no reason."""
    item = next(
        o for o in router_bundle.build(router_bundle.RouterOptions())
        if o.kind == "IngressClass"
    )
    error = Invalid("Rejected by an admission webhook.", detail="policy denied")

    assert router_service._immutable_class_hint(item, error).hint != (
        "kubectl delete ingressclass haproxy"
    )
    assert "delete ingressclass" not in str(
        router_service._immutable_class_hint(item, error).hint or ""
    )
