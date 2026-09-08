"""
Installing Operator Lifecycle Manager (§33) — the second bundle this console ships.

ADR-0004 said there would never be a second bundle and ADR-0008 re-opens that
boundary, so these tests are mostly about the ways this feature could become the
thing both ADRs warn about:

* **It never adopts an OLM it did not install.** A cluster already running OLM
  collides on most of the twenty-six objects, and the install refuses naming all
  of them — on a dry run as much as on a real one. Writing 0.35.0's Deployments
  over a running 0.30 would restart a cluster's whole operator control plane.
* **The vendored manifests are upstream's bytes.** Pinned by SHA-256 and checked
  on every load, so "byte for byte upstream" is enforced rather than claimed.
* **`installed` never means `working`.** Twenty-six accepted objects is not a
  running OLM, and the response that reports the install says so rather than
  letting a UI render a green banner over a package server that has not
  registered.
* **The community catalog is opt-in.** Installing it by default would make this
  console the thing that decided a cluster trusts operatorhub.io, at `:latest`.
* **Phase two never runs into APIs that do not exist.** A CRD failure or an
  establishment timeout stops the install and reports it, rather than producing
  eighteen 404s that all describe the first problem.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from app.admin import olm as olm_service
from app.admin import olm_bundle
from app.errors import (
    Conflict,
    Invalid,
    MutationsDisabled,
    NotFound,
    RBACDenied,
    Unsupported,
)
from app.resources import catalog
from tests import test_routes


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


@pytest.fixture
def allow_olm(monkeypatch, allow_mutations):
    """Both gates open, which is what a real install needs."""
    monkeypatch.setattr(olm_service.settings, "olm_install_enabled", True)
    return olm_service.settings


#: Every consequence a default install produces, which a real write must name.
_ACK = [olm_service.WARN_CLUSTER_ADMIN_GRANT, olm_service.WARN_CRD_OWNERSHIP]


def stub_discovery(monkeypatch, *, olm_apis=True):
    """Discovery covering every group the bundle writes into.

    ``olm_apis=False`` is the shape of a cluster that has never had OLM — the
    entire population §33 exists for — where ``operators.coreos.com`` is not
    served at all and every read of it raises ``unsupported`` (501) rather than
    ``not_found`` (404).

    **The default is the unrealistic one and that is deliberate.** Most cases
    below are about something else and want the APIs present so the flow reaches
    the code under test. But a suite that only ever ran the default is exactly
    how the ownership scan shipped assuming a missing API arrives as 404: every
    install on every OLM-less cluster failed with a 501 naming ``olmconfigs``
    before writing anything, and no test saw it because the fake said the CRDs
    were already there. `test_a_fresh_cluster_...` below is the case that pins it.
    """
    payloads = {
        "/api": {"versions": ["v1"]},
        "/api/v1": {
            "resources": [
                {"name": "serviceaccounts", "kind": "ServiceAccount",
                 "namespaced": True,
                 "verbs": ["get", "list", "create", "update", "patch", "delete"]},
                {"name": "namespaces", "kind": "Namespace", "namespaced": False,
                 "verbs": ["get", "list", "create", "update", "patch", "delete"]},
            ]
        },
        "/apis/apps/v1": test_routes._resources(("deployments", "Deployment")),
        "/apis/networking.k8s.io/v1": test_routes._resources(
            ("networkpolicies", "NetworkPolicy"),
        ),
        "/apis/rbac.authorization.k8s.io/v1": {
            "resources": [
                {"name": n, "kind": k, "namespaced": False,
                 "verbs": ["get", "list", "create", "update", "patch", "delete"]}
                for n, k in (("clusterroles", "ClusterRole"),
                             ("clusterrolebindings", "ClusterRoleBinding"))
            ]
        },
        "/apis/apiextensions.k8s.io/v1": {
            "resources": [
                {"name": "customresourcedefinitions",
                 "kind": "CustomResourceDefinition", "namespaced": False,
                 "verbs": ["get", "list", "create", "update", "patch", "delete"]},
            ]
        },
        "/apis/operators.coreos.com/v1": test_routes._resources(
            ("olmconfigs", "OLMConfig"), ("operatorgroups", "OperatorGroup"),
        ),
        "/apis/operators.coreos.com/v1alpha1": test_routes._resources(
            ("clusterserviceversions", "ClusterServiceVersion"),
            ("catalogsources", "CatalogSource"),
        ),
    }
    # OLMConfig is cluster-scoped; the shared helper marks everything namespaced.
    for entry in payloads["/apis/operators.coreos.com/v1"]["resources"]:
        if entry["name"] == "olmconfigs":
            entry["namespaced"] = False

    groups = [
        ("apps", "v1"), ("rbac.authorization.k8s.io", "v1"),
        ("networking.k8s.io", "v1"), ("apiextensions.k8s.io", "v1"),
    ]
    if olm_apis:
        groups += [("operators.coreos.com", "v1"), ("operators.coreos.com", "v1alpha1")]
    else:
        payloads.pop("/apis/operators.coreos.com/v1")
        payloads.pop("/apis/operators.coreos.com/v1alpha1")

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


def _established_crd(name):
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": name, "resourceVersion": "1",
                     "labels": dict(olm_bundle.LABELS)},
        "status": {"conditions": [{"type": "Established", "status": "True"}]},
    }


def _stub_absent_and_writable(
    monkeypatch, *, fail_on=None, existing=None, establish=True, olm_apis=True,
):
    """Nothing exists yet and every write succeeds, except where told otherwise.

    Stateful on purpose: a CRD becomes readable and Established only once this
    fake has been asked to create it. That is what lets the two-phase install and
    its wait be exercised as one flow rather than with the wait stubbed out — the
    wait is the only thing in this codebase that blocks on a cluster, so a test
    that skipped it would leave the riskiest code untested.

    ``establish=False`` creates the CRDs and never marks them Established, which
    is the timeout case.

    ``olm_apis=False`` makes reads of ``operators.coreos.com`` raise
    ``Unsupported`` rather than ``NotFound``, which is what a cluster that has
    never had OLM actually does. Without it the fresh-cluster test below passes
    without ever executing the branch it exists to pin — the fake answers every
    read with a 404 and the 501 path is never reached.
    """
    fail_on = fail_on or set()
    existing = dict(existing or {})
    created: set[str] = set()

    def fake_get(group, version, plural, name, namespace=None):
        key = (plural, namespace, name)
        if key in existing:
            return existing[key]
        if not olm_apis and group == "operators.coreos.com" and not created:
            raise Unsupported(
                f"This cluster does not serve {group}/{version}.",
                context={"group": group, "resource": plural, "version": version},
            )
        if plural == "customresourcedefinitions" and name in created and establish:
            return _established_crd(name)
        if plural == "customresourcedefinitions" and name in created:
            return {
                "apiVersion": "apiextensions.k8s.io/v1",
                "kind": "CustomResourceDefinition",
                "metadata": {"name": name, "resourceVersion": "1"},
                "status": {"conditions": []},
            }
        raise NotFound(f"{plural}/{name} not found", context={"resource": plural})

    def fake_list(group, version, plural, **kwargs):
        if plural == "customresourcedefinitions":
            return {"items": [_established_crd(n) for n in sorted(created)]}
        return {"items": []}

    def fake_request_json(method, path, **kwargs):
        for token in fail_on:
            if token in path:
                raise RBACDenied(f"{token} is forbidden", context={"resource": token})
        body = kwargs.get("body") or {}
        name = (body.get("metadata") or {}).get("name")
        if "customresourcedefinitions" in path and name:
            created.add(name)
        return (
            {**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}},
            [],
        )

    monkeypatch.setattr(olm_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(olm_service.reader, "list_resource", fake_list)
    monkeypatch.setattr(
        olm_service.apply_service.reader, "get_resource", fake_get, raising=False,
    )
    monkeypatch.setattr(olm_service.apply_service, "request_json", fake_request_json)
    monkeypatch.setattr(olm_service, "_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(olm_service.settings, "olm_establish_timeout_seconds", 0.05)
    return created


# --------------------------------------------------------------------------- #
# The bundle is vendored upstream data
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("filename", sorted(olm_bundle.FILE_DIGESTS))
def test_the_vendored_manifests_match_their_pinned_digests(filename):
    """The check that makes "byte for byte upstream" a fact rather than a claim.

    These two files are a cluster's whole operator control plane and a ClusterRole
    granting ``*`` on ``*``. A local edit to either — however well meant — is a
    change nobody reviewed against upstream, so it fails the build here and
    :func:`app.admin.olm_bundle._read` refuses to load it at run time.
    """
    path = olm_bundle._MANIFEST_DIR / filename
    actual = hashlib.sha256(path.read_bytes()).hexdigest()

    assert actual == olm_bundle.FILE_DIGESTS[filename], (
        f"deploy/olm/{filename} does not match the digest pinned in "
        "app.admin.olm_bundle. Re-download it from "
        f"{olm_bundle.UPSTREAM_RELEASE}/{filename} rather than editing it."
    )


def test_an_edited_manifest_refuses_to_load_rather_than_installing():
    """The run-time half of the digest check, which is the half that matters."""
    olm_bundle._documents.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="does not match the digest"):
            original = olm_bundle.FILE_DIGESTS["olm.yaml"]
            olm_bundle.FILE_DIGESTS["olm.yaml"] = "0" * 64
            olm_bundle._read("olm.yaml")
    finally:
        olm_bundle.FILE_DIGESTS["olm.yaml"] = original
        olm_bundle._documents.cache_clear()


def test_the_community_catalog_is_not_installed_by_default():
    """Installing it by default decides, for the operator, that they trust operatorhub.io."""
    kinds = [
        (o.kind, o.name) for o in olm_bundle.build()
    ]

    assert (olm_bundle.COMMUNITY_CATALOG_KIND, olm_bundle.COMMUNITY_CATALOG_NAME) \
        not in kinds


def test_the_community_catalog_is_installed_when_it_is_asked_for():
    objects = olm_bundle.build(olm_bundle.OLMOptions(community_catalog=True))
    catalogs = [o for o in objects if o.kind == olm_bundle.COMMUNITY_CATALOG_KIND]

    assert [o.name for o in catalogs] == [olm_bundle.COMMUNITY_CATALOG_NAME]
    assert catalogs[0].body["spec"]["image"] == olm_bundle.COMMUNITY_CATALOG_IMAGE


def test_upstreams_catalog_source_is_still_in_the_vendored_file():
    """The opt-in filters it out; it must not have been edited out of the manifest.

    Two different mechanisms with the same visible effect, and only one of them
    is reversible. If somebody "simplified" this by deleting the CatalogSource
    from ``deploy/olm/olm.yaml``, the default install would look identical and
    ``communityCatalog: true`` would silently install nothing.
    """
    documents = list(yaml.safe_load_all(olm_bundle._read("olm.yaml")))
    names = [d["metadata"]["name"] for d in documents if d]

    assert olm_bundle.COMMUNITY_CATALOG_NAME in names


def test_every_object_carries_the_managed_by_label():
    """It is how an install tells "no OLM here" from "somebody else's OLM"."""
    for item in olm_bundle.build(olm_bundle.OLMOptions(community_catalog=True)):
        labels = item.body["metadata"]["labels"]
        assert labels[olm_bundle.MANAGED_BY_LABEL] == olm_bundle.MANAGED_BY
        assert labels[olm_bundle.VERSION_LABEL] == olm_bundle.OLM_VERSION


def test_labelling_keeps_the_rbac_aggregation_labels_upstream_set():
    """Replacing metadata.labels would silently unaggregate OLM's verbs.

    ``aggregate-olm-edit`` and ``aggregate-olm-view`` are folded into the
    cluster's built-in ``edit`` and ``view`` roles by the aggregation controller,
    which matches on exactly these labels. Drop them and every object still
    installs, OLM still runs, and ordinary users simply cannot see Subscriptions
    — a failure with no error anywhere.
    """
    by_name = {o.name: o for o in olm_bundle.build()}

    for name in ("aggregate-olm-edit", "aggregate-olm-view"):
        labels = by_name[name].body["metadata"]["labels"]
        assert labels["rbac.authorization.k8s.io/aggregate-to-admin"] == "true"
        assert labels["rbac.authorization.k8s.io/aggregate-to-edit"] == "true"
        assert labels[olm_bundle.MANAGED_BY_LABEL] == olm_bundle.MANAGED_BY


def test_the_namespaces_keep_the_pod_security_levels_upstream_chose():
    """`olm` enforces `restricted`; an install that dropped that would weaken it."""
    by_name = {o.name: o for o in olm_bundle.build() if o.kind == "Namespace"}

    assert by_name["olm"].body["metadata"]["labels"][
        "pod-security.kubernetes.io/enforce"] == "restricted"
    assert by_name["operators"].body["metadata"]["labels"][
        "pod-security.kubernetes.io/enforce"] == "baseline"


def test_every_crd_is_in_phase_one_and_nothing_else_is():
    """Phase two's kinds are instances of phase one's CRDs; the order is not a preference."""
    objects = olm_bundle.build(olm_bundle.OLMOptions(community_catalog=True))
    phase_one = [o for o in objects if o.phase == olm_bundle.PHASE_CRDS]

    assert {o.kind for o in phase_one} == {"CustomResourceDefinition"}
    assert not any(
        o.kind == "CustomResourceDefinition"
        for o in objects if o.phase == olm_bundle.PHASE_CORE
    )
    # And they come first in the list, because `install` walks it in order.
    assert [o.phase for o in objects] == (
        [olm_bundle.PHASE_CRDS] * len(phase_one)
        + [olm_bundle.PHASE_CORE] * (len(objects) - len(phase_one))
    )


def _position(objects, kind, name):
    """Where one object sits in install order, addressed by kind *and* name.

    Both, because names repeat inside this bundle: ``packageserver`` is a
    NetworkPolicy *and* the ClusterServiceVersion, and ``olm-operator`` is a
    NetworkPolicy *and* a Deployment. A helper that matched on name alone found
    the NetworkPolicy at index 13 and quietly asserted the wrong ordering — which
    is also why :func:`app.admin.olm.install` keys its per-object state on
    ``(kind, where)`` rather than on the name.
    """
    return next(
        i for i, o in enumerate(objects) if o.kind == kind and o.name == name
    )


def test_names_repeat_across_kinds_in_this_bundle():
    """Pinned, because it is the trap every helper over this list falls into."""
    objects = olm_bundle.build()
    names = [o.name for o in objects]
    keys = [(o.kind, o.where) for o in objects]

    assert len(set(names)) < len(names), "names collide — see _position"
    assert len(set(keys)) == len(keys), "(kind, namespace/name) must be unique"


def test_the_operator_group_precedes_the_packageserver_csv():
    """Upstream's order inside olm.yaml, preserved because OLM depends on it.

    OLM refuses to install a ClusterServiceVersion into a namespace with no
    OperatorGroup. Sorting this list for tidiness would produce an install where
    every object is created and the package server never starts.
    """
    objects = olm_bundle.build()

    assert _position(objects, "OperatorGroup", "olm-operators") < _position(
        objects, "ClusterServiceVersion", olm_bundle.PACKAGESERVER_CSV
    )


def test_the_cluster_role_precedes_its_binding():
    objects = olm_bundle.build()

    assert _position(
        objects, "ClusterRole", "system:controller:operator-lifecycle-manager"
    ) < _position(objects, "ClusterRoleBinding", "olm-operator-binding-olm")


def test_the_plural_table_agrees_with_the_vendored_crds():
    """`_PLURALS` is hand-written because discovery cannot answer before the install.

    Asserted against ``spec.names.plural`` in the CRDs themselves rather than
    against a second copy of the same list. A wrong plural addresses the wrong
    URL, and the API server answers 404 — which reads as "OLM's object is
    missing" rather than "this console asked for the wrong resource".
    """
    documents = [d for d in yaml.safe_load_all(olm_bundle._read("crds.yaml")) if d]
    checked = set()
    for crd in documents:
        key = (crd["spec"]["group"], crd["spec"]["names"]["kind"])
        if key not in olm_bundle._PLURALS:
            # Four of the eight CRDs define kinds nothing in this bundle writes
            # — Subscription, InstallPlan, Operator, OperatorCondition. They are
            # §16's to address, through discovery, once they exist.
            continue
        assert olm_bundle._PLURALS[key] == crd["spec"]["names"]["plural"]
        checked.add(key)

    # And every operators.coreos.com kind the bundle *does* write was covered,
    # so a table entry deleted along with its object stops the build rather than
    # leaving the loop above vacuously passing.
    written = {
        (o.group, o.kind)
        for o in olm_bundle.build(olm_bundle.OLMOptions(community_catalog=True))
        if o.group == "operators.coreos.com"
    }
    assert written and written <= checked


def test_the_bundle_is_the_size_the_docs_say_it_is():
    """26 objects by default, 27 with the catalog. Quoted in ADR-0008 and §33."""
    assert len(olm_bundle.build()) == 26
    assert len(olm_bundle.build(olm_bundle.OLMOptions(community_catalog=True))) == 27
    assert len(olm_bundle.crd_names()) == 8


def test_the_shipped_reader_role_can_read_every_kind_the_ownership_scan_reads():
    """The scan reads all twenty-six objects before writing any. All of them.

    Miss one kind out of `deploy/rbac.yaml`'s reader role and nothing breaks on a
    cluster with no OLM — the API is not served, the read is `unsupported`, and
    the object provably cannot exist. It breaks on a cluster where OLM IS
    installed, which is precisely when the scan is the thing standing between an
    install and somebody's running operator control plane: instead of "this OLM
    is not mine, I will not touch it", the operator gets `rbac_denied` naming a
    resource they never asked about.

    That is how `olmconfigs` shipped missing — the §16 reader rule lists the five
    kinds the portal reads, and OLMConfig is not one of them, so nothing pointed
    at it until an install had already succeeded once. Asserted against the real
    file rather than a list here, because a second list is a second thing to
    forget.
    """
    import pathlib

    import yaml as _yaml

    rbac = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "rbac.yaml"
    docs = [d for d in _yaml.safe_load_all(rbac.read_text()) if d]
    # The READER role specifically. This file exists to support a console
    # installed read-only, the writer role is explicitly deletable, and its
    # wildcard rule grants create/update/patch/delete with no `get` — so a scan
    # that needs `get` needs it from here or it does not have it.
    readable: set[tuple[str, str]] = set()
    for doc in docs:
        if doc["kind"] != "ClusterRole" or doc["metadata"]["name"] != "k8boss-admin-reader":
            continue
        for rule in doc.get("rules") or []:
            if "get" not in rule.get("verbs", []):
                continue
            for group in rule.get("apiGroups", []):
                for resource in rule.get("resources", []):
                    readable.add((group, resource))

    needed = {
        (item.group, item.plural)
        for item in olm_bundle.build(olm_bundle.OLMOptions(community_catalog=True))
    }
    missing = {pair for pair in needed if pair not in readable}
    assert not missing, (
        "deploy/rbac.yaml grants no `get` on these, and app.admin.olm's ownership "
        f"scan reads every one of them before writing anything: {sorted(missing)}"
    )


def test_a_body_carrying_configuration_this_bundle_does_not_have_is_ignored():
    """§33 offers one boolean; a namespace field must not become configuration."""
    options = olm_bundle.validate_options({"namespace": "mine", "communityCatalog": True})

    assert options.community_catalog is True
    assert not hasattr(options, "namespace")
    assert olm_bundle.OLM_NAMESPACE == "olm"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_a_real_install_is_refused_when_the_feature_gate_is_off(
    monkeypatch, fake_k8s, allow_mutations,
):
    stub_discovery(monkeypatch)
    monkeypatch.setattr(olm_service.settings, "olm_install_enabled", False)

    with pytest.raises(MutationsDisabled) as caught:
        olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    assert "ADMIN_OLM_INSTALL_ENABLED" in caught.value.hint


def test_the_refusal_is_audited(monkeypatch, fake_k8s, allow_mutations, db_session):
    """Somebody trying to install a cluster's operator control plane, refused."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    monkeypatch.setattr(olm_service.settings, "olm_install_enabled", False)

    with pytest.raises(MutationsDisabled):
        olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "denied"
    assert "OLM install refused" in record.detail


def test_a_dry_run_is_permitted_with_the_feature_gate_off(
    monkeypatch, fake_k8s, allow_mutations,
):
    """Deciding whether to grant OLM `*` on `*` requires reading the object first."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)
    monkeypatch.setattr(olm_service.settings, "olm_install_enabled", False)

    result = olm_service.install({}, dry_run=True)

    assert result["dryRun"] is True
    assert result["installed"] is False
    assert result["failed"] == 0


def test_the_plan_is_readable_with_both_gates_off(monkeypatch):
    """It reads no cluster and writes nothing; it is how the gate decision gets made."""
    monkeypatch.setattr(olm_service.settings, "olm_install_enabled", False)
    monkeypatch.setattr(olm_service.settings, "admin_allow_mutations", False)

    plan = olm_service.plan({})

    assert plan["version"] == olm_bundle.OLM_VERSION
    assert len(plan["objects"]) == 26
    assert all(o["yaml"] for o in plan["objects"])


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def test_a_real_install_refuses_until_every_consequence_is_acknowledged(
    monkeypatch, fake_k8s, allow_olm,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    with pytest.raises(Invalid) as caught:
        olm_service.install({}, dry_run=False, acknowledge_consequences=[])

    unacknowledged = caught.value.context["unacknowledged"]
    assert olm_service.WARN_CLUSTER_ADMIN_GRANT in unacknowledged
    assert olm_service.WARN_CRD_OWNERSHIP in unacknowledged


def test_the_cluster_admin_grant_is_quoted_rather_than_paraphrased():
    """"Broad permissions" would make the object in the diff sound smaller than it is."""
    entry = next(
        c for c in olm_service.plan({})["consequences"]
        if c["code"] == olm_service.WARN_CLUSTER_ADMIN_GRANT
    )

    assert "escalate" in entry["consequence"]
    assert "bind" in entry["consequence"]
    assert "apiGroups: ['*']" in entry["consequence"]


def test_turning_the_catalog_on_adds_a_consequence_the_old_acknowledgement_misses(
    monkeypatch, fake_k8s, allow_olm,
):
    """§16's rule: acknowledging one list does not consent to a different one."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    with pytest.raises(Invalid) as caught:
        olm_service.install(
            {"communityCatalog": True}, dry_run=False, acknowledge_consequences=_ACK,
        )

    assert caught.value.context["unacknowledged"] == [
        olm_service.WARN_COMMUNITY_CATALOG
    ]


def test_a_dry_run_needs_no_acknowledgement(monkeypatch, fake_k8s, allow_olm):
    """Nothing is consented to by looking at what would happen."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=True, acknowledge_consequences=[])

    assert result["installed"] is False
    assert [c["code"] for c in result["consequences"]] == _ACK


def test_the_notes_are_not_acknowledgeable(monkeypatch, fake_k8s, allow_olm):
    """Four boxes to get past two that mattered is how consent stops meaning anything."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    codes = {n["code"] for n in result["notes"]}
    assert "installed_is_not_running" in codes
    assert not codes & {c["code"] for c in result["consequences"]}
    # And they carry a different shape, so a note cannot be dropped into
    # ConsequenceChecklist and quietly acquire a checkbox: that component reads
    # `consequence` and `mitigation`, and a note has neither.
    assert all({"code", "label", "detail"} == set(n) for n in result["notes"])
    assert all(
        {"code", "label", "consequence", "mitigation"} == set(c)
        for c in result["consequences"]
    )


# --------------------------------------------------------------------------- #
# The console never adopts an OLM it did not install
# --------------------------------------------------------------------------- #

def test_an_existing_unlabelled_object_refuses_the_whole_install(
    monkeypatch, fake_k8s, allow_olm,
):
    """A cluster already running OLM. Installing over it restarts every operator."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, existing={
        ("deployments", "olm", "olm-operator"): {
            "metadata": {"name": "olm-operator", "namespace": "olm",
                         "resourceVersion": "9", "labels": {"app": "olm-operator"}},
        },
    })

    with pytest.raises(Conflict) as caught:
        olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    conflicts = caught.value.context["conflicts"]
    assert [c["name"] for c in conflicts] == ["olm-operator"]


def test_the_refusal_names_every_conflict_rather_than_the_first(
    monkeypatch, fake_k8s, allow_olm,
):
    """A cluster with OLM collides on most of the twenty-six at once.

    Reporting one per attempt would have an operator re-run the install a dozen
    times to learn a fact that was knowable on the first read.
    """
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, existing={
        ("namespaces", None, "olm"): {"metadata": {"name": "olm", "labels": {}}},
        ("namespaces", None, "operators"): {
            "metadata": {"name": "operators", "labels": {}},
        },
        ("deployments", "olm", "catalog-operator"): {
            "metadata": {"name": "catalog-operator", "namespace": "olm", "labels": {}},
        },
    })

    with pytest.raises(Conflict) as caught:
        olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    names = {c["name"] for c in caught.value.context["conflicts"]}
    assert names == {"olm", "operators", "catalog-operator"}


def test_a_dry_run_refuses_the_takeover_too(monkeypatch, fake_k8s, allow_olm):
    """A takeover hit at confirm time is one the preview should have shown."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, existing={
        ("namespaces", None, "olm"): {"metadata": {"name": "olm", "labels": {}}},
    })

    with pytest.raises(Conflict):
        olm_service.install({}, dry_run=True)


def test_the_takeover_refusal_is_audited(
    monkeypatch, fake_k8s, allow_olm, db_session,
):
    """The funnel is never reached, so this row is written by hand — and must be."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, existing={
        ("namespaces", None, "olm"): {"metadata": {"name": "olm", "labels": {}}},
    })

    with pytest.raises(Conflict):
        olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "conflict"
    assert "not managed by this console" in record.detail


def test_an_object_this_console_installed_is_replaced_rather_than_refused(
    monkeypatch, fake_k8s, allow_olm,
):
    """Re-running an install after a partial failure has to be possible."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, existing={
        ("namespaces", None, "olm"): {
            "metadata": {"name": "olm", "resourceVersion": "7",
                         "labels": dict(olm_bundle.LABELS)},
        },
    })

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    namespace = next(
        o for o in result["objects"] if o["kind"] == "Namespace" and o["name"] == "olm"
    )
    assert namespace["verb"] == "update"


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

def test_a_clean_install_creates_every_object_in_two_phases(
    monkeypatch, fake_k8s, allow_olm,
):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    assert result["installed"] is True
    assert result["failed"] == 0
    assert len(result["objects"]) == 26
    assert all(o["applied"] for o in result["objects"])
    assert result["crds"]["established"] is True


def test_a_fresh_cluster_that_serves_no_olm_apis_can_still_be_installed_onto(
    monkeypatch, fake_k8s, allow_olm,
):
    """The case the whole feature is for, and the one the fake used to hide.

    On a cluster that has never had OLM, ``operators.coreos.com`` is not in
    discovery, so reading any of phase two's eleven objects raises ``unsupported``
    (501) — not ``not_found`` (404). The ownership scan reads all twenty-six
    before writing any, so this fires before a single object is created.

    Shipped, that made §33 fail on 100% of its target clusters with
    ``501 This cluster does not serve operators.coreos.com/v1``. Found on a real
    k3s API server, not here.
    """
    stub_discovery(monkeypatch, olm_apis=False)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, olm_apis=False)

    # A dry run is the first thing anybody does, and it must not 501.
    preview = olm_service.install({}, dry_run=True)

    assert preview["installed"] is False
    assert preview["failed"] == 0
    assert len(preview["objects"]) == 26
    # Phase two is rendered rather than projected, which is the correct answer
    # here for a second reason: the API server has no such kind to project.
    assert all(
        o["projection"] == "rendered"
        for o in preview["objects"] if o["phase"] == olm_bundle.PHASE_CORE
    )


def test_an_unsupported_api_is_read_as_absence_not_as_a_takeover(
    monkeypatch, fake_k8s, allow_olm,
):
    """"The API is not served" means the object cannot exist. That is sound.

    The other direction is what the scan exists for, so the inference has to be
    exactly this narrow: `unsupported` and `not_found` become "not there", and a
    forbidden read or an unreachable API server still propagates rather than
    being read as a clear field to write into.
    """
    stub_discovery(monkeypatch, olm_apis=False)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, olm_apis=False)

    from app.errors import ClusterUnreachable

    def unreachable(group, version, plural, name, namespace=None):
        raise ClusterUnreachable("the API server did not answer")

    monkeypatch.setattr(olm_service.reader, "get_resource", unreachable)

    with pytest.raises(ClusterUnreachable):
        olm_service.install({}, dry_run=True)


def test_the_install_never_claims_olm_is_running(monkeypatch, fake_k8s, allow_olm):
    """Twenty-six accepted objects is not a package server. §33's whole point."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    assert result["installed"] is True
    assert result["ready"] is None
    assert "packages.operators.coreos.com" in result["readyDetail"]


def test_a_dry_run_never_reports_installed(monkeypatch, fake_k8s, allow_olm):
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=True)

    assert result["installed"] is False
    assert all(o["applied"] is False for o in result["objects"])


def test_a_dry_run_renders_phase_two_rather_than_asking_the_api_server(
    monkeypatch, fake_k8s, allow_olm,
):
    """On a cluster with no OLM the API server genuinely cannot project an OperatorGroup.

    A dry run that asked would report eighteen not_found failures on every
    cluster this feature exists for, which is the wrong answer delivered
    confidently.
    """
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=True)

    phase_two = [o for o in result["objects"] if o["phase"] == olm_bundle.PHASE_CORE]
    assert phase_two
    assert all(o["projection"] == "rendered" for o in phase_two)
    assert all(o["diff"] for o in phase_two)
    assert all(o["error"] is None for o in phase_two)


def test_a_dry_run_still_projects_the_crds_through_the_api_server(
    monkeypatch, fake_k8s, allow_olm,
):
    """Phase one has no such problem: apiextensions.k8s.io is served everywhere."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    result = olm_service.install({}, dry_run=True)

    phase_one = [o for o in result["objects"] if o["phase"] == olm_bundle.PHASE_CRDS]
    assert len(phase_one) == 8
    assert all(o["projection"] == "server" for o in phase_one)


def test_a_failed_crd_stops_phase_two_rather_than_producing_eighteen_more_errors(
    monkeypatch, fake_k8s, allow_olm,
):
    """Eighteen 404s that all describe the first failure is not eighteen answers."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, fail_on={"customresourcedefinitions"})

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    assert result["installed"] is False
    assert result["failed"] == 8
    phase_two = [o for o in result["objects"] if o["phase"] == olm_bundle.PHASE_CORE]
    assert all(o["skipped"] for o in phase_two)
    assert all(o["error"] is None for o in phase_two)
    assert result["skipped"] == len(phase_two)


def test_an_establishment_timeout_stops_the_install_and_says_so(
    monkeypatch, fake_k8s, allow_olm,
):
    """The CRDs were created; what did not happen is that they became usable."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, establish=False)

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    assert result["installed"] is False
    assert result["crds"]["established"] is False
    assert len(result["crds"]["pending"]) == 8
    assert "Re-run the install" in result["crds"]["detail"]
    phase_one = [o for o in result["objects"] if o["phase"] == olm_bundle.PHASE_CRDS]
    assert all(o["applied"] for o in phase_one), (
        "The CRDs really were created; the report must not imply otherwise."
    )


def test_the_install_keeps_going_past_a_failure_inside_a_phase(
    monkeypatch, fake_k8s, allow_olm,
):
    """One error and twenty-five unknowns is not the same information as twenty-six answers."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch, fail_on={"networkpolicies"})

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    assert result["installed"] is False
    assert result["failed"] == 5
    assert sum(1 for o in result["objects"] if o["applied"]) == 21


def test_every_object_gets_its_own_audit_row(
    monkeypatch, fake_k8s, allow_olm, db_session,
):
    """Twenty-six writes, twenty-six rows. The funnel, not a special path."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)
    before = db_session.query(AuditRecord).count()

    olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    rows = db_session.query(AuditRecord).count() - before
    assert rows == 26


def test_the_escalation_hint_replaces_the_one_that_sends_people_to_the_wrong_place(
    monkeypatch, fake_k8s, allow_olm,
):
    """Preflight says yes and admission says no. The default hint names a verb they hold."""
    stub_discovery(monkeypatch)
    allow_preflight(fake_k8s)
    _stub_absent_and_writable(monkeypatch)

    def fake_request_json(method, path, **kwargs):
        body = kwargs.get("body") or {}
        name = (body.get("metadata") or {}).get("name")
        if "clusterroles" in path and "binding" not in path:
            raise RBACDenied(
                "clusterroles is forbidden",
                detail=(
                    'clusterroles.rbac.authorization.k8s.io is forbidden: user '
                    '"system:serviceaccount:k8boss:admin" is attempting to grant '
                    "RBAC permissions not currently held"
                ),
                context={"resource": "clusterroles"},
            )
        if "customresourcedefinitions" in path and name:
            pass
        return (
            {**body, "metadata": {**(body.get("metadata") or {}), "resourceVersion": "1"}},
            [],
        )

    # Keep the CRD bookkeeping from the shared stub, and fail only ClusterRoles.
    created = _stub_absent_and_writable(monkeypatch)
    original = olm_service.apply_service.request_json

    def layered(method, path, **kwargs):
        if "clusterroles" in path and "clusterrolebindings" not in path:
            return fake_request_json(method, path, **kwargs)
        return original(method, path, **kwargs)

    monkeypatch.setattr(olm_service.apply_service, "request_json", layered)

    result = olm_service.install({}, dry_run=False, acknowledge_consequences=_ACK)

    role = next(
        o for o in result["objects"]
        if o["kind"] == "ClusterRole"
        and o["name"] == "system:controller:operator-lifecycle-manager"
    )
    assert role["error"]["code"] == "rbac_denied"
    assert "escalation prevention" in role["error"]["hint"]
    assert "escalate" in role["error"]["hint"]
    assert created  # the CRDs still went in


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def _stub_status(monkeypatch, *, deployments=None, csv=None, crds=8, fail=None):
    """A cluster whose OLM state is whatever the test says it is."""
    deployments = deployments or {}

    def fake_get(group, version, plural, name, namespace=None):
        if fail and plural in fail:
            raise RBACDenied(f"{plural} is forbidden", context={"resource": plural})
        if plural == "deployments" and name in deployments:
            return deployments[name]
        if plural == "clusterserviceversions" and csv is not None:
            return csv
        raise NotFound(f"{plural}/{name} not found", context={"resource": plural})

    def fake_list(group, version, plural, **kwargs):
        if fail and plural in fail:
            raise RBACDenied(f"{plural} is forbidden", context={"resource": plural})
        if plural == "customresourcedefinitions":
            return {"items": [_established_crd(n)
                              for n in olm_bundle.crd_names()[:crds]]}
        return {"items": []}

    monkeypatch.setattr(olm_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(olm_service.reader, "list_resource", fake_list)


def _deployment(name, *, ready=1):
    return {
        "metadata": {"name": name, "namespace": "olm", "generation": 1,
                     "labels": dict(olm_bundle.LABELS)},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [
            {"image": "quay.io/operator-framework/olm@sha256:abc"}]}}},
        "status": {"observedGeneration": 1, "readyReplicas": ready},
    }


def test_status_reports_not_installed_when_nothing_is_there(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)
    _stub_status(monkeypatch, crds=0)

    result = olm_service.status()

    assert result["installed"] is False
    assert result["ready"] is False
    assert result["crds"]["present"] == 0


def test_status_is_null_rather_than_false_when_the_read_failed(monkeypatch, fake_k8s):
    """`false` during an outage invites an operator to install over a working OLM."""
    stub_discovery(monkeypatch)
    _stub_status(monkeypatch, fail={"deployments"})

    result = olm_service.status()

    assert result["installed"] is None
    assert result["ready"] is None
    assert result["partial"] is True
    assert result["unavailable"]


def test_a_cluster_with_no_olm_is_not_a_partial_read(monkeypatch, fake_k8s):
    """The normal case for this whole feature, and it must not raise the banner.

    Every cluster §33 exists for serves no `operators.coreos.com`, so reading the
    packageserver ClusterServiceVersion raises `unsupported` on all of them. If
    that lands in `unavailable[]`, the status of a perfectly ordinary OLM-less
    cluster is `partial: true` and the panel renders "we could not answer some of
    this" above four rows that answered correctly — §1.2's rule inverted, on the
    one path that is always taken.

    Found on a real cluster, not here: the fake stubs discovery with the OLM CRDs
    present, so every earlier test read the CSV successfully and this never fired.
    """
    stub_discovery(monkeypatch)

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "clusterserviceversions":
            raise Unsupported(
                "This cluster does not serve operators.coreos.com/v1alpha1.",
                context={"group": group, "resource": plural},
            )
        raise NotFound(f"{plural}/{name} not found", context={"resource": plural})

    monkeypatch.setattr(olm_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        olm_service.reader, "list_resource",
        lambda group, version, plural, **kw: {"items": []},
    )

    result = olm_service.status()

    assert result["partial"] is False
    assert result["unavailable"] == []
    # And the absence is reported as an absence rather than as a null: an API
    # the cluster does not serve cannot be hiding the object.
    assert result["packageServer"]["csvPresent"] is False
    assert result["installed"] is False


def test_a_read_that_really_failed_still_makes_it_partial(monkeypatch, fake_k8s):
    """The other half, so the fix above cannot swallow a genuine blind spot."""
    stub_discovery(monkeypatch)

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "clusterserviceversions":
            raise RBACDenied(
                "clusterserviceversions is forbidden",
                context={"group": group, "resource": plural},
            )
        raise NotFound(f"{plural}/{name} not found", context={"resource": plural})

    monkeypatch.setattr(olm_service.reader, "get_resource", fake_get)
    monkeypatch.setattr(
        olm_service.reader, "list_resource",
        lambda group, version, plural, **kw: {"items": []},
    )

    result = olm_service.status()

    assert result["partial"] is True
    assert [e["resource"] for e in result["unavailable"]] == ["clusterserviceversions"]
    assert result["packageServer"]["csvPresent"] is None


def test_a_crd_listing_that_failed_counts_null_rather_than_zero(monkeypatch, fake_k8s):
    """§0.1's corollary: "0 of 8 established" from a read nobody could make."""
    stub_discovery(monkeypatch)
    _stub_status(monkeypatch, fail={"customresourcedefinitions"})

    result = olm_service.status()

    assert result["crds"]["present"] is None
    assert result["crds"]["established"] is None
    assert "unknown — not zero" in result["crds"]["detail"]


def test_installed_but_not_ready_is_the_ordinary_state_after_an_install(
    monkeypatch, fake_k8s,
):
    """Every object exists, both Deployments are Ready, and the portal is still empty.

    That is not a bug and this console must not render it as success. The package
    server is an aggregated APIService that OLM registers only after reconciling
    the packageserver CSV.
    """
    stub_discovery(monkeypatch)
    _stub_status(
        monkeypatch,
        deployments={n: _deployment(n) for n in olm_bundle.OLM_DEPLOYMENTS},
        csv={"metadata": {"name": "packageserver", "namespace": "olm"},
             "status": {"phase": "Installing"}},
    )
    # Discovery serves the OLM CRDs but not packages.operators.coreos.com.
    monkeypatch.setattr(
        olm_service.portal_service, "source_state",
        lambda api: olm_service.portal_service.SourceState(
            api=api, state=olm_service.portal_service.STATE_UNSUPPORTED,
            version=None, detail="not served",
        ),
    )

    result = olm_service.status()

    assert result["installed"] is True
    assert result["ready"] is False
    assert result["packageServer"]["apiAvailable"] is False
    assert result["packageServer"]["phase"] == "Installing"


def test_ready_is_true_once_the_package_server_answers(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)
    _stub_status(
        monkeypatch,
        deployments={n: _deployment(n) for n in olm_bundle.OLM_DEPLOYMENTS},
        csv={"metadata": {"name": "packageserver", "namespace": "olm"},
             "status": {"phase": "Succeeded"}},
    )
    monkeypatch.setattr(
        olm_service.portal_service, "source_state",
        lambda api: olm_service.portal_service.SourceState(
            api=api, state=olm_service.portal_service.STATE_AVAILABLE,
            version="v1", detail="served",
        ),
    )

    result = olm_service.status()

    assert result["installed"] is True
    assert result["ready"] is True
    assert result["managedByUs"] is True


def test_an_olm_this_console_did_not_install_is_reported_as_such(
    monkeypatch, fake_k8s,
):
    """Read, reported, and never written to."""
    stub_discovery(monkeypatch)
    theirs = _deployment("olm-operator")
    theirs["metadata"]["labels"] = {"app": "olm-operator"}
    _stub_status(monkeypatch, deployments={
        "olm-operator": theirs,
        "catalog-operator": _deployment("catalog-operator"),
    })

    result = olm_service.status()

    assert result["installed"] is True
    assert result["managedByUs"] is False


def test_a_stale_deployment_generation_reports_null_ready_replicas(
    monkeypatch, fake_k8s,
):
    """The §6 staleness rule: last generation's ready count beside this one's spec."""
    stub_discovery(monkeypatch)
    stale = _deployment("olm-operator")
    stale["metadata"]["generation"] = 4
    stale["status"]["observedGeneration"] = 3
    _stub_status(monkeypatch, deployments={
        "olm-operator": stale,
        "catalog-operator": _deployment("catalog-operator"),
    })

    result = olm_service.status()

    entry = next(d for d in result["deployments"] if d["name"] == "olm-operator")
    assert entry["readyReplicas"] is None
    assert "has not yet reported" in entry["detail"]
