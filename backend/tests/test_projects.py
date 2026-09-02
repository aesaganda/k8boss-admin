"""
Projects (§17) — a namespace read with what governs it, and created with it.

The read half is five reads joined, so the tests are mostly about which of
them failed and what the page says when one did: ``quotas: null`` is a listing
that did not answer, ``quotas: []`` is a namespace nothing bounds, and a quota
whose controller has not written ``status`` yet reports ``used: null`` rather
than a reassuring zero.

The write half is five ordinary creates through the funnel, so the tests are
about what it refuses and what it admits to:

* **It never adopts a namespace.** One that exists is a 409 naming it, before
  anything is written and on a dry run too, and the refusal is audited.
* **A dry run projects the Namespace and renders the rest.** The API server
  cannot project into a namespace that does not exist, so the four objects
  inside it come back marked ``projection: "rendered"`` with a preflight each,
  and ``created`` is never true on a dry run.
* **A partial create is reported as partial.** A Namespace that failed skips
  the rest; any other failure is counted and named with its grant.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.admin import projects as projects_admin
from app.errors import Conflict, Invalid, MutationsDisabled, NotFound, RBACDenied
from app.models import AuditRecord
from app.resources import catalog
from app.services import projects as projects_service
from tests.conftest import obj
from tests.test_router import allow_preflight
from tests.test_routes import _groups_payload, _resources

NAME = "payments"


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# A stubbed cluster
# --------------------------------------------------------------------------- #

def stub_discovery(monkeypatch):
    """Discovery covering every group-version a project writes into."""
    core = _resources(
        ("pods", "Pod"), ("resourcequotas", "ResourceQuota"), ("limitranges", "LimitRange"),
    )
    core["resources"].append({
        "name": "namespaces", "kind": "Namespace", "namespaced": False,
        "verbs": ["get", "list", "create", "update", "delete"],
    })
    payloads = {
        "/api": {"versions": ["v1"]},
        "/api/v1": core,
        "/apis/rbac.authorization.k8s.io/v1": _resources(("rolebindings", "RoleBinding")),
        "/apis/networking.k8s.io/v1": _resources(("networkpolicies", "NetworkPolicy")),
    }
    groups = [("rbac.authorization.k8s.io", "v1"), ("networking.k8s.io", "v1")]

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return _groups_payload(*groups)
        if path in payloads:
            return payloads[path]
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)


def stub_namespace_read(monkeypatch, *, live=None, error=None):
    """What ``reader.get_resource`` says about the target namespace."""

    def fake_get(group, version, plural, name, namespace=None):
        assert plural == "namespaces", plural
        if error is not None:
            raise error
        if live is None:
            raise NotFound(f"namespaces/{name} not found", context={"resource": plural})
        return live

    monkeypatch.setattr(projects_admin.reader, "get_resource", fake_get)


def stub_writes(monkeypatch, *, fail_on=None, detail_on=None):
    """Every write succeeds — except the kinds in ``fail_on``."""
    fail_on = fail_on or set()
    detail_on = detail_on or {}

    def fake_request_json(method, path, **kwargs):
        for kind in fail_on:
            if kind in path:
                raise RBACDenied(
                    f"{kind} is forbidden", detail=detail_on.get(kind),
                    context={"resource": kind},
                )
        body = kwargs.get("body") or {}
        return (
            {**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}},
            [],
        )

    monkeypatch.setattr(projects_admin.apply_service, "request_json", fake_request_json)


FULL_REQUEST = {
    "name": NAME,
    "displayName": "Payments",
    "description": "The payments team's production namespace.",
    "podSecurity": {"enforce": "restricted", "audit": "restricted", "warn": "restricted"},
    "quota": {"requests.cpu": "4", "requests.memory": "8Gi", "pods": "20"},
    "limits": [{
        "type": "Container",
        "default": {"cpu": "500m", "memory": "512Mi"},
        "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
    }],
    "admins": [{"kind": "Group", "name": "payments-team"}],
    "adminRole": "admin",
    "isolateIngress": True,
}

FULL_CONSEQUENCES = ["psa_enforced", "ingress_isolated"]


# --------------------------------------------------------------------------- #
# The read model
# --------------------------------------------------------------------------- #

def _namespace(labels=None, annotations=None):
    return obj(
        metadata=obj(name=NAME, labels=labels, annotations=annotations, creation_timestamp=None,
                     deletion_timestamp=None),
        status=obj(phase="Active"),
    )


def _quota(hard, used=None, *, status=True):
    return obj(
        metadata=obj(name="project-quota", namespace=NAME, creation_timestamp=None),
        spec=obj(hard=hard, scopes=None, scope_selector=None),
        status=obj(hard=hard, used=used or {}) if status else None,
    )


def _limit_range():
    return obj(
        metadata=obj(name="project-limits", namespace=NAME, creation_timestamp=None),
        spec=obj(limits=[obj(
            type="Container", default={"cpu": "500m"}, default_request={"cpu": "100m"},
            max=None, min=None, max_limit_request_ratio=None,
        )]),
    )


def _binding():
    return obj(
        metadata=obj(name="admin", namespace=NAME, creation_timestamp=None),
        role_ref=obj(kind="ClusterRole", name="admin"),
        subjects=[obj(kind="Group", name="payments-team", namespace=None)],
    )


def _policy(name="allow-same-namespace", *, selector="empty", types=("Ingress",)):
    if selector == "empty":
        pod_selector = obj(match_labels=None, match_expressions=None)
    elif selector == "absent":
        pod_selector = None
    else:
        pod_selector = obj(match_labels={"app": "web"}, match_expressions=None)
    return obj(
        metadata=obj(name=name, namespace=NAME),
        spec=obj(pod_selector=pod_selector, policy_types=list(types), egress=None),
    )


def _stub_reads(fake, *, namespace=None, quotas=(), limits=(), bindings=(), policies=(), pods=()):
    fake.core_v1.returns("read_namespace", namespace or _namespace())
    fake.core_v1.returns("list_namespaced_pod", obj(items=list(pods)))
    fake.core_v1.returns("list_namespaced_resource_quota", obj(items=list(quotas)))
    fake.core_v1.returns("list_namespaced_limit_range", obj(items=list(limits)))
    fake.rbac_v1.returns("list_namespaced_role_binding", obj(items=list(bindings)))
    fake.networking_v1.returns("list_namespaced_network_policy", obj(items=list(policies)))


def test_the_project_joins_the_five_reads(client, fake_k8s):
    _stub_reads(
        fake_k8s,
        namespace=_namespace(
            labels={"pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/warn": "baseline",
                    "pod-security.kubernetes.io/warn-version": "v1.31"},
            annotations={"openshift.io/display-name": "Payments"},
        ),
        quotas=[_quota({"requests.cpu": "4", "pods": "20"}, {"requests.cpu": "1500m", "pods": "3"})],
        limits=[_limit_range()],
        bindings=[_binding()],
        policies=[_policy()],
        pods=[obj(metadata=obj(name="a")), obj(metadata=obj(name="b"))],
    )

    body = client.get(f"/api/projects/{NAME}").json()

    assert body["name"] == NAME
    assert body["displayName"] == "Payments"
    assert body["pod_count"] == 2
    assert body["partial"] is False and body["unavailable"] == []

    psa = body["podSecurity"]
    assert psa["enforce"] == "restricted" and psa["enforceVersion"] is None
    assert psa["warn"] == "baseline" and psa["warnVersion"] == "v1.31"
    assert psa["audit"] is None
    assert psa["labelled"] is True

    quota = body["quotas"][0]
    assert quota["reconciled"] is True
    cpu = next(r for r in quota["resources"] if r["resource"] == "requests.cpu")
    assert cpu["hard"] == "4" and cpu["used"] == "1500m"
    assert cpu["hard_value"] == 4 and cpu["used_value"] == 1.5
    assert cpu["exhausted"] is False

    assert body["limitRanges"][0]["limits"][0]["defaultRequest"] == {"cpu": "100m"}
    assert body["roleBindings"][0]["role"] == {"kind": "ClusterRole", "name": "admin"}
    assert body["networkPolicies"] == {
        "count": 1, "names": ["allow-same-namespace"],
        "isolatesAllIngress": True, "isolatesAllEgress": False,
    }


def test_a_failed_quota_listing_is_null_and_named_never_empty(client, fake_k8s):
    """`quotas: []` is "nothing bounds this namespace". A 403 is not that."""
    _stub_reads(fake_k8s, limits=[_limit_range()])
    fake_k8s.core_v1.raises(
        "list_namespaced_resource_quota", ApiException(status=403, reason="Forbidden"),
    )

    body = client.get(f"/api/projects/{NAME}").json()

    assert body["quotas"] is None
    assert body["limitRanges"] == [] or body["limitRanges"][0]["name"] == "project-limits"
    assert body["partial"] is True
    entry = next(e for e in body["unavailable"] if e["resource"] == "resourcequotas")
    assert entry["reason"] == "forbidden"
    assert entry["namespace"] == NAME


def test_quota_used_is_null_until_the_controller_writes_status(client, fake_k8s):
    """A quota seconds old has spec.hard and no status. That is not "nothing used"."""
    _stub_reads(fake_k8s, quotas=[_quota({"requests.cpu": "4"}, status=False)])

    quota = client.get(f"/api/projects/{NAME}").json()["quotas"][0]

    assert quota["reconciled"] is False
    cpu = quota["resources"][0]
    assert cpu["used"] is None and cpu["used_value"] is None
    assert cpu["exhausted"] is None


def test_an_exhausted_quota_says_so(client, fake_k8s):
    _stub_reads(fake_k8s, quotas=[_quota({"pods": "20"}, {"pods": "20"})])
    pods = client.get(f"/api/projects/{NAME}").json()["quotas"][0]["resources"][0]
    assert pods["exhausted"] is True


def test_no_pod_security_labels_is_nothing_declared_not_privileged(client, fake_k8s):
    """The cluster default lives in a file no API serves; the console cannot say."""
    _stub_reads(fake_k8s, namespace=_namespace(labels={"team": "payments"}))
    psa = client.get(f"/api/projects/{NAME}").json()["podSecurity"]
    assert psa == {
        "enforce": None, "enforceVersion": None, "audit": None, "auditVersion": None,
        "warn": None, "warnVersion": None, "labelled": False,
    }


def test_pod_count_is_null_when_pods_cannot_be_listed(client, fake_k8s):
    _stub_reads(fake_k8s)
    fake_k8s.core_v1.raises("list_namespaced_pod", ApiException(status=403, reason="Forbidden"))
    body = client.get(f"/api/projects/{NAME}").json()
    assert body["pod_count"] is None
    assert any(e["resource"] == "pods" for e in body["unavailable"])


def test_network_isolation_is_unknown_when_a_selector_cannot_be_read(client, fake_k8s):
    """A policy whose selector is absent might be the one selecting everything."""
    _stub_reads(fake_k8s, policies=[_policy(selector="absent")])
    summary = client.get(f"/api/projects/{NAME}").json()["networkPolicies"]
    assert summary["isolatesAllIngress"] is None


def test_a_narrow_policy_does_not_isolate_every_pod(client, fake_k8s):
    _stub_reads(fake_k8s, policies=[_policy(selector="narrow")])
    summary = client.get(f"/api/projects/{NAME}").json()["networkPolicies"]
    assert summary["count"] == 1
    assert summary["isolatesAllIngress"] is False


def test_an_unreadable_namespace_is_the_endpoints_own_error(client, fake_k8s):
    """The primary read failing is a 404, never a page with five empty sections."""
    fake_k8s.core_v1.raises("read_namespace", ApiException(status=404, reason="Not Found"))
    response = client.get(f"/api/projects/{NAME}")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Validation and the plan
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["Payments", "-payments", "a" * 64, "pay_ments", ""])
def test_an_invalid_name_is_refused_naming_the_rule(name):
    with pytest.raises(Invalid) as excinfo:
        projects_admin.validate_request({"name": name})
    assert excinfo.value.context["parameter"] == "name"


def test_a_bad_quantity_names_the_field_and_the_value():
    with pytest.raises(Invalid) as excinfo:
        projects_admin.validate_request({"name": NAME, "quota": {"requests.cpu": "four"}})
    assert excinfo.value.context["parameter"] == "quota"
    assert excinfo.value.context["value"] == "four"


def test_a_pod_security_version_without_its_level_is_refused():
    with pytest.raises(Invalid):
        projects_admin.validate_request(
            {"name": NAME, "podSecurity": {"enforceVersion": "v1.31"}}
        )


def test_a_service_account_subject_defaults_to_the_project_namespace():
    request = projects_admin.validate_request(
        {"name": NAME, "admins": [{"kind": "ServiceAccount", "name": "deployer"}]}
    )
    assert request["admins"] == [{"kind": "ServiceAccount", "name": "deployer", "namespace": NAME}]


def test_the_plan_renders_every_object_and_writes_nothing(monkeypatch, fake_k8s, db_session):
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch)

    result = projects_admin.plan(FULL_REQUEST)

    assert [o["kind"] for o in result["objects"]] == [
        "Namespace", "ResourceQuota", "LimitRange", "RoleBinding", "NetworkPolicy",
    ]
    assert result["target"]["exists"] is False
    assert [c["code"] for c in result["consequences"]] == FULL_CONSEQUENCES
    namespace_yaml = result["objects"][0]["yaml"]
    assert "pod-security.kubernetes.io/enforce: restricted" in namespace_yaml
    assert "openshift.io/display-name: Payments" in namespace_yaml
    # The RoleBinding subject carries the RBAC apiGroup a Group needs.
    assert "apiGroup: rbac.authorization.k8s.io" in result["objects"][3]["yaml"]
    # Nothing was written and nothing was recorded: a plan is a read.
    assert fake_k8s.api_client.called("call_api") == []
    assert db_session.query(AuditRecord).count() == 0


def test_the_plan_reports_an_existing_namespace(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch, live={"metadata": {"name": NAME}, "status": {"phase": "Active"}})
    result = projects_admin.plan({"name": NAME})
    assert result["target"]["exists"] is True
    assert result["target"]["phase"] == "Active"


def test_the_plan_says_unknown_when_the_namespace_read_did_not_answer(monkeypatch, fake_k8s):
    """`exists: null` plus a consequence — never `false`, which would promise a create."""
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch, error=RBACDenied("forbidden", context={}))

    result = projects_admin.plan({"name": NAME})

    assert result["target"]["exists"] is None
    assert result["partial"] is True
    assert result["unavailable"][0]["resource"] == "namespaces"
    assert "namespace_unknown" in [c["code"] for c in result["consequences"]]


def test_a_quota_without_limit_defaults_is_a_named_consequence(monkeypatch, fake_k8s):
    """The trap: `must specify requests.cpu` on every pod that did not."""
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch)

    bare = projects_admin.plan({"name": NAME, "quota": {"requests.cpu": "4", "limits.memory": "8Gi"}})
    codes = [c["code"] for c in bare["consequences"]]
    assert "quota_needs_defaults" in codes
    needs = next(c for c in bare["consequences"] if c["code"] == "quota_needs_defaults")
    assert "limits.memory" in needs["consequence"] and "requests.cpu" in needs["consequence"]

    covered = projects_admin.plan({
        "name": NAME,
        "quota": {"requests.cpu": "4", "limits.memory": "8Gi"},
        "limits": [{"type": "Container", "defaultRequest": {"cpu": "100m"}, "default": {"memory": "256Mi"}}],
    })
    assert "quota_needs_defaults" not in [c["code"] for c in covered["consequences"]]


def test_every_omission_is_a_consequence_the_operator_must_name(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch)

    result = projects_admin.plan({"name": "kube-payments"})

    assert [c["code"] for c in result["consequences"]] == [
        "reserved_prefix", "psa_not_enforced", "no_quota", "no_admin",
    ]
    # Only the Namespace: nothing else was asked for, so nothing else is rendered.
    assert [o["kind"] for o in result["objects"]] == ["Namespace"]


def test_the_plan_renders_with_mutations_off(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch)
    result = projects_admin.plan({"name": NAME})
    assert result["enabled"] is False
    assert "ADMIN_ALLOW_MUTATIONS" in result["enabledDetail"]
    assert result["objects"]


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def test_a_real_create_is_refused_read_only_and_audited(monkeypatch, fake_k8s, db_session):
    stub_discovery(monkeypatch)
    with pytest.raises(MutationsDisabled):
        projects_admin.create({"name": NAME}, dry_run=False)
    rows = db_session.query(AuditRecord).all()
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error.startswith("mutations_disabled")
    assert fake_k8s.api_client.called("call_api") == []


def test_an_existing_namespace_is_never_adopted(monkeypatch, fake_k8s, allow_mutations, db_session):
    """On a dry run too: a takeover the operator would hit at confirm time is one
    they should have been told about at preview time."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch, live={"metadata": {"name": NAME}, "status": {"phase": "Active"}})
    stub_writes(monkeypatch)

    for dry_run in (True, False):
        with pytest.raises(Conflict) as excinfo:
            projects_admin.create(
                FULL_REQUEST, dry_run=dry_run, acknowledge_consequences=FULL_CONSEQUENCES,
            )
        assert NAME in excinfo.value.message

    rows = db_session.query(AuditRecord).order_by(AuditRecord.id).all()
    assert [r.outcome for r in rows] == ["conflict", "conflict"]
    assert [r.dry_run for r in rows] == [True, False]
    # The funnel was never reached: no object's audit row, no write.
    assert fake_k8s.api_client.called("call_api") == []


def test_a_namespace_read_that_did_not_answer_stops_a_real_write(monkeypatch, fake_k8s, allow_mutations):
    """"Unknown" is not a state a create may proceed from."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch, error=RBACDenied("forbidden", context={}))
    stub_writes(monkeypatch)
    with pytest.raises(RBACDenied):
        projects_admin.create({"name": NAME}, dry_run=False,
                              acknowledge_consequences=["psa_not_enforced", "no_quota", "no_admin"])


def test_unacknowledged_consequences_refuse_the_write_by_name(monkeypatch, fake_k8s, allow_mutations):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    stub_writes(monkeypatch)

    with pytest.raises(Invalid) as excinfo:
        projects_admin.create(FULL_REQUEST, dry_run=True, acknowledge_consequences=["psa_enforced"])

    assert excinfo.value.context["unacknowledged"] == ["ingress_isolated"]


def test_a_dry_run_projects_the_namespace_and_renders_the_rest(monkeypatch, fake_k8s, db_session):
    """The API server cannot project into a namespace that does not exist, and
    the dry run says whose diff each object carries rather than reporting four
    not_founds."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    calls: list = []

    def call_api(path, method, **kwargs):
        calls.append((method, path, dict(kwargs.get("query_params") or [])))
        body = kwargs.get("body") or {}
        return ({**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}}, 200, {})

    fake_k8s.api_client.returns("call_api", call_api)

    result = projects_admin.create(FULL_REQUEST, dry_run=True, acknowledge_consequences=FULL_CONSEQUENCES)

    assert result["dryRun"] is True
    assert result["created"] is False
    assert result["failed"] == 0 and result["skipped"] == 0
    # One request reached the cluster: the Namespace, as a dry run.
    assert len(calls) == 1
    assert calls[0][0] == "POST" and calls[0][1] == "/api/v1/namespaces"
    assert calls[0][2].get("dryRun") == "All"

    namespace, *rest = result["objects"]
    assert namespace["projection"] == "server"
    assert namespace["applied"] is False
    assert namespace["diff"]["changed"] is True
    for entry in rest:
        assert entry["projection"] == "rendered"
        assert entry["applied"] is False
        assert entry["diff"]["before"] in (None, "")
        assert entry["kind"] in entry["diff"]["after"]
        assert entry["preflight"]["allowed"] is True
        assert entry["auditId"] is None
    # A dry-run audit row for the projected Namespace; none for the rendered four.
    rows = db_session.query(AuditRecord).all()
    assert [r.outcome for r in rows] == ["dry_run"]


def test_a_rendered_object_carries_its_preflight_denial(monkeypatch, fake_k8s):
    """The one thing about a namespaced object that can be checked before its
    namespace exists is the grant, so a missing one shows up on the preview."""
    stub_discovery(monkeypatch)
    stub_namespace_read(monkeypatch)
    stub_writes(monkeypatch)

    def review(body, **kw):
        attrs = body.spec.resource_attributes
        denied = attrs.resource == "rolebindings"
        return type("R", (), {"status": type("S", (), {
            "allowed": not denied, "reason": "no RBAC policy matched" if denied else "",
            "evaluation_error": None,
        })()})()

    fake_k8s.authorization_v1.returns("create_self_subject_access_review", review)

    result = projects_admin.create(FULL_REQUEST, dry_run=True, acknowledge_consequences=FULL_CONSEQUENCES)

    binding = next(o for o in result["objects"] if o["kind"] == "RoleBinding")
    assert binding["preflight"]["allowed"] is False
    assert binding["preflight"]["evaluationError"] is None
    assert "rolebindings" in (binding["preflight"]["hint"] or "")
    quota = next(o for o in result["objects"] if o["kind"] == "ResourceQuota")
    assert quota["preflight"]["allowed"] is True


def test_a_real_create_writes_every_object_through_the_funnel(monkeypatch, fake_k8s, allow_mutations, db_session):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    calls: list = []

    def call_api(path, method, **kwargs):
        calls.append((method, path, dict(kwargs.get("query_params") or [])))
        body = kwargs.get("body") or {}
        return ({**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}}, 200, {})

    fake_k8s.api_client.returns("call_api", call_api)

    result = projects_admin.create(FULL_REQUEST, dry_run=False, acknowledge_consequences=FULL_CONSEQUENCES)

    assert result["created"] is True
    assert result["failed"] == 0 and result["skipped"] == 0
    assert all(o["applied"] for o in result["objects"])
    assert all(o["projection"] == "server" for o in result["objects"])
    assert [c[1] for c in calls] == [
        "/api/v1/namespaces",
        f"/api/v1/namespaces/{NAME}/resourcequotas",
        f"/api/v1/namespaces/{NAME}/limitranges",
        f"/apis/rbac.authorization.k8s.io/v1/namespaces/{NAME}/rolebindings",
        f"/apis/networking.k8s.io/v1/namespaces/{NAME}/networkpolicies",
    ]
    assert all("dryRun" not in c[2] for c in calls)
    rows = db_session.query(AuditRecord).order_by(AuditRecord.id).all()
    assert [r.outcome for r in rows] == ["applied"] * 5
    assert {o["auditId"] for o in result["objects"]} == {r.id for r in rows}
    assert all(f"project {NAME}" in (r.detail or "") for r in rows)


def test_a_failed_namespace_skips_the_rest_rather_than_failing_four_more_times(
    monkeypatch, fake_k8s, allow_mutations,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    stub_writes(monkeypatch, fail_on={"namespaces"})

    result = projects_admin.create(FULL_REQUEST, dry_run=False, acknowledge_consequences=FULL_CONSEQUENCES)

    assert result["created"] is False
    assert result["failed"] == 1 and result["skipped"] == 4
    namespace, *rest = result["objects"]
    assert namespace["error"]["code"] == "rbac_denied"
    for entry in rest:
        assert entry["skipped"] and "Namespace" in entry["skipped"]
        assert entry["applied"] is False and entry["error"] is None


def test_one_failed_object_makes_the_project_not_created(monkeypatch, fake_k8s, allow_mutations):
    """"Created" over a namespace with no quota is the sentence that gets a
    cluster eaten by one Deployment."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    stub_writes(monkeypatch, fail_on={"resourcequotas"})

    result = projects_admin.create(FULL_REQUEST, dry_run=False, acknowledge_consequences=FULL_CONSEQUENCES)

    assert result["created"] is False
    assert result["failed"] == 1 and result["skipped"] == 0
    assert sum(1 for o in result["objects"] if o["applied"]) == 4
    quota = next(o for o in result["objects"] if o["kind"] == "ResourceQuota")
    assert quota["error"]["code"] == "rbac_denied"


def test_escalation_prevention_gets_a_hint_naming_bind(monkeypatch, fake_k8s, allow_mutations):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    stub_writes(
        monkeypatch, fail_on={"rolebindings"},
        detail_on={"rolebindings": 'rolebindings.rbac.authorization.k8s.io "admin" is forbidden: '
                                   "user is attempting to grant RBAC permissions not currently held"},
    )

    result = projects_admin.create(FULL_REQUEST, dry_run=False, acknowledge_consequences=FULL_CONSEQUENCES)

    binding = next(o for o in result["objects"] if o["kind"] == "RoleBinding")
    assert binding["error"]["code"] == "rbac_denied"
    assert "escalation prevention" in binding["error"]["hint"]
    assert "`bind`" in binding["error"]["hint"]


def test_the_endpoint_defaults_to_a_dry_run(client, fake_k8s, monkeypatch, allow_mutations):
    """A client that forgot dryRun gets a projection, not a namespace."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    calls: list = []
    fake_k8s.api_client.returns(
        "call_api",
        lambda path, method, **kwargs: (
            calls.append(dict(kwargs.get("query_params") or [])),
            ({**(kwargs.get("body") or {})}, 200, {}),
        )[1],
    )

    response = client.post(
        "/api/projects",
        json={**FULL_REQUEST, "acknowledgeConsequences": FULL_CONSEQUENCES},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dryRun"] is True and body["created"] is False
    assert len(calls) == 1 and calls[0].get("dryRun") == "All"


def test_the_endpoint_relays_an_unacknowledged_refusal_as_422(client, fake_k8s, monkeypatch):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    stub_namespace_read(monkeypatch)
    response = client.post("/api/projects", json={"name": NAME})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert set(body["context"]["unacknowledged"]) == {"psa_not_enforced", "no_quota", "no_admin"}
