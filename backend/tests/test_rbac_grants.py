"""
Granting and revoking a role in one namespace (§30).

Every assertion here is about a sentence a console can produce that is false,
and about the pair it comes from collapsing:

  a name vs a capability   `roleRef: ClusterRole/admin` is three words. `admin`
                           in a namespace includes `create rolebindings`, so the
                           grantee can grant themselves everything else here;
                           `edit` includes `create pods/exec`, which reads every
                           Secret mounted into every pod whether or not the role
                           mentions Secrets. Confirming the word is not
                           confirming the grant.

  unreadable vs empty      A role whose rules could not be fetched grants
                           *unknown*, never nothing. §0.1's corollary aimed at a
                           security control, where "grants nothing" is the one
                           answer that must not come out of a read that did not
                           happen.

  absent vs harmless       The API server accepts a binding to a role that does
                           not exist. It grants nothing today and starts
                           granting the moment somebody creates that role —
                           without a second decision by anybody.

  removed vs revoked       Taking a subject out of one binding is not taking
                           away their access. Another binding here, or any
                           ClusterRoleBinding, still grants it. "Revoked" is a
                           claim, and a console that makes it falsely is the
                           defect standard.

  [] vs null               An empty residual list means the cluster was searched
                           and nothing else grants this. `null` means the
                           cluster-wide listing was refused. The first sentence
                           closes the ticket.
"""

from __future__ import annotations

import copy

import pytest
from kubernetes.client.rest import ApiException

from app.admin import rbac_grants as grants
from app.audit import recorder
from app.errors import Conflict, Invalid, MutationsDisabled, RBACDenied
from app.resources import catalog
from tests.conftest import obj

NAMESPACE = "prod"
RBAC = "rbac.authorization.k8s.io"
BASE = f"/apis/{RBAC}/v1"
BINDINGS = f"{BASE}/namespaces/{NAMESPACE}/rolebindings"
CLUSTER_BINDINGS = f"{BASE}/clusterrolebindings"


def api_resource(resource, *, group=RBAC, version="v1", kind=None, namespaced=True,
                 verbs=("get", "list", "create", "patch", "delete"), preferred=True):
    return {
        "group": group,
        "version": version,
        "kind": kind or resource[:-1].title(),
        "resource": resource,
        "namespaced": namespaced,
        "verbs": list(verbs),
        "shortNames": [],
        "categories": [],
        "apiVersion": f"{group}/{version}" if group else version,
        "preferred": preferred,
    }


DEFAULT_CATALOG = [
    api_resource("rolebindings", kind="RoleBinding"),
    api_resource("clusterrolebindings", namespaced=False, kind="ClusterRoleBinding"),
    api_resource("roles", kind="Role"),
    api_resource("clusterroles", namespaced=False, kind="ClusterRole"),
]


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def rule(*, groups=("",), resources=("pods",), verbs=("get",), urls=None):
    entry: dict = {
        "apiGroups": list(groups),
        "resources": list(resources),
        "verbs": list(verbs),
    }
    if urls is not None:
        entry["nonResourceURLs"] = list(urls)
    return entry


def role_object(name="view", *, kind="ClusterRole", rules=(), aggregates=False,
                namespace=None, omit_rules=False):
    body: dict = {
        "apiVersion": f"{RBAC}/v1",
        "kind": kind,
        "metadata": {"name": name},
    }
    if namespace:
        body["metadata"]["namespace"] = namespace
    if not omit_rules:
        body["rules"] = [copy.deepcopy(r) for r in rules]
    if aggregates:
        body["aggregationRule"] = {"clusterRoleSelectors": [{"matchLabels": {"a": "b"}}]}
    return body


def subject(kind="User", name="alice", namespace=None):
    entry: dict = {"kind": kind, "name": name}
    if namespace:
        entry["namespace"] = namespace
    return entry


def binding(name="view", *, role_kind="ClusterRole", role_name="view",
            subjects=(), namespace=NAMESPACE, version="7781"):
    return {
        "apiVersion": f"{RBAC}/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace, "resourceVersion": version},
        "roleRef": {"apiGroup": RBAC, "kind": role_kind, "name": role_name},
        "subjects": [copy.deepcopy(s) for s in subjects],
    }


def cluster_binding(name="platform-admins", *, subjects=()):
    return {
        "apiVersion": f"{RBAC}/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name, "resourceVersion": "1"},
        "roleRef": {"apiGroup": RBAC, "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [copy.deepcopy(s) for s in subjects],
    }


def listing(items):
    return {"items": list(items), "metadata": {}}


def request(operation="grant", *, role_kind="ClusterRole", role_name="view",
            subject_kind="User", subject_name="alice", subject_namespace=None,
            **extra):
    body: dict = {
        "operation": operation,
        "role": {"kind": role_kind, "name": role_name},
        "subject": subject(subject_kind, subject_name, subject_namespace),
    }
    body.update(extra)
    return body


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
        self.requests: list[tuple[str, str, dict, dict]] = []

    def at(self, path, payload):
        self.payloads[path] = payload
        return self

    def fails(self, path, error=None):
        self.failures[path] = error or api_exception()
        return self

    def __call__(self, path, method, **kwargs):
        query = dict(kwargs.get("query_params") or [])
        body = kwargs.get("body") or {}
        self.requests.append((method, path, query, body))
        if path in self.failures:
            raise self.failures[path]
        if method in ("POST", "PATCH"):
            # The API server answers a write with the resulting object. Building
            # it from the request rather than from a canned payload is what makes
            # the projected diff in these tests the diff production would show —
            # including the one that matters here, `subjects` before and after.
            if method == "POST":
                return copy.deepcopy(body), 200, {}
            result = copy.deepcopy(self.payloads[path])
            result["subjects"] = copy.deepcopy(body.get("subjects", []))
            return result, 200, {}
        if path not in self.payloads:
            raise AssertionError(f"{method} {path} was requested but not stubbed.")
        payload = self.payloads[path]
        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, {}

    def of(self, method):
        return [(path, query, body) for verb, path, query, body in self.requests
                if verb == method]


@pytest.fixture
def cluster(monkeypatch, fake_k8s, db_engine, allow_mutations):
    """Discovery fixed, REST routed by path, preflight allowing everything.

    Discovery is stubbed *through* `catalog.discover`, so `catalog.resolve` still
    runs for real and a test that reaches a kind discovery does not serve fails
    the way production would. Preflight denial has its own coverage where the
    funnel lives; the one denial test below is about §30 reaching the funnel at
    all, not about what the funnel does with the answer.
    """
    items = list(DEFAULT_CATALOG)

    monkeypatch.setattr(catalog, "discover", lambda: (
        sorted((dict(i) for i in items),
               key=lambda i: (i["group"], i["resource"], i["version"])),
        [],
    ))
    server = FakeServer()
    fake_k8s.api_client.returns("call_api", server)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    server.catalog = items
    return server


@pytest.fixture
def simple(cluster):
    """One `view` ClusterRole that reads pods, and no bindings yet."""
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))
    return cluster


def codes(report):
    return [entry["code"] for entry in report["consequences"]]


def powers(report):
    return [entry["code"] for entry in report["capability"]["powers"]]


def audit_rows():
    return recorder.query(limit=50)["items"]


# --------------------------------------------------------------------------- #
# Powers — what a rule set confers, and what is deliberately not reported
# --------------------------------------------------------------------------- #

def test_a_wildcard_rule_is_full_control_and_every_power_under_it():
    """A wildcard really does confer all five, and the powers list is a
    *description* rather than a decision, so it says all five. Collapsing them
    here would be the console deciding what is worth mentioning about
    `cluster-admin`; the suppression that keeps the handshake short happens at
    the consequence layer, where the operator is being asked something."""
    found = grants.powers_of([rule(groups=["*"], resources=["*"], verbs=["*"])])
    listed = [p["code"] for p in found]

    assert listed[0] == grants.POWER_FULL_CONTROL
    assert set(listed) == {
        grants.POWER_FULL_CONTROL, grants.POWER_ESCALATION, grants.POWER_IMPERSONATE,
        grants.POWER_SECRET_READ, grants.POWER_POD_EXEC,
    }


def test_reading_secrets_is_reported_even_when_the_role_name_says_nothing():
    found = grants.powers_of([rule(resources=["secrets"], verbs=["get", "list"])])

    assert grants.POWER_SECRET_READ in [p["code"] for p in found]


def test_exec_is_reported_as_secret_exposure_although_no_rule_mentions_secrets():
    """The reason `edit` is not a lesser `admin` where credentials are concerned.

    A shell in a pod reads every Secret mounted into it. A console that only
    looked for a `secrets` rule would confirm this grant as touching none.
    """
    found = grants.powers_of([rule(resources=["pods/exec"], verbs=["create"])])
    entry = next(p for p in found if p["code"] == grants.POWER_POD_EXEC)

    assert "whether or not this role mentions Secrets" in entry["detail"]


def test_plain_pod_access_does_not_confer_exec():
    """`resources: [pods]` is not `pods/exec` — the subresource is named
    separately by RBAC, and reporting it here would fire the loudest finding in
    this module on the most ordinary role there is."""
    found = grants.powers_of([rule(resources=["pods"], verbs=["create", "get"])])

    assert found == []


def test_writing_rolebindings_is_privilege_escalation():
    """Fires on the stock `admin` ClusterRole, which is the point: binding
    `admin` in a namespace delegates the namespace's RBAC along with it."""
    found = grants.powers_of([
        rule(groups=[RBAC], resources=["rolebindings"], verbs=["create", "update"]),
    ])
    entry = next(p for p in found if p["code"] == grants.POWER_ESCALATION)

    assert "grant themselves" in entry["detail"]


def test_reading_rolebindings_is_not_escalation():
    """`view` lists bindings. Seeing who is bound is not being able to bind."""
    found = grants.powers_of([
        rule(groups=[RBAC], resources=["rolebindings"], verbs=["get", "list", "watch"]),
    ])

    assert found == []


def test_the_escalate_and_bind_verbs_are_escalation():
    escalate = grants.powers_of([rule(groups=[RBAC], resources=["roles"], verbs=["escalate"])])
    bind = grants.powers_of([rule(groups=[RBAC], resources=["clusterroles"], verbs=["bind"])])

    assert [p["code"] for p in escalate] == [grants.POWER_ESCALATION]
    assert [p["code"] for p in bind] == [grants.POWER_ESCALATION]


def test_impersonation_needs_the_verb_not_merely_access_to_serviceaccounts():
    """`admin` manages ServiceAccounts with an explicit verb list and does not
    impersonate them. Firing here would put the loudest word in this module on
    the most common grant an operator makes."""
    managing = grants.powers_of([
        rule(resources=["serviceaccounts"], verbs=["create", "get", "patch", "delete"]),
    ])
    impersonating = grants.powers_of([
        rule(resources=["serviceaccounts"], verbs=["impersonate"]),
    ])

    assert managing == []
    assert [p["code"] for p in impersonating] == [grants.POWER_IMPERSONATE]


def test_a_non_resource_url_rule_confers_none_of_these():
    """`nonResourceURLs` rules have no `resources` at all. They must fall out of
    every branch rather than raising on the missing key."""
    assert grants.powers_of([
        {"nonResourceURLs": ["/healthz"], "verbs": ["get"]},
    ]) == []


# --------------------------------------------------------------------------- #
# The role read — three states, and the one that must never be "empty"
# --------------------------------------------------------------------------- #

def test_a_role_that_reads_is_present_with_its_rules(simple):
    capability = grants.role_capability(
        {"kind": "ClusterRole", "name": "view"}, namespace=NAMESPACE,
    )

    assert capability["state"] == "present"
    assert capability["rule_count"] == 1


def test_an_explicitly_empty_rule_set_is_zero_not_unknown(cluster):
    """`rules: []` is a role that grants nothing, and it is a real zero."""
    cluster.at(f"{BASE}/clusterroles/empty", role_object("empty", rules=[]))

    capability = grants.role_capability(
        {"kind": "ClusterRole", "name": "empty"}, namespace=NAMESPACE,
    )

    assert capability["state"] == "present"
    assert capability["rule_count"] == 0
    assert capability["rules"] == []


def test_a_role_we_could_not_read_is_unknown_never_empty(cluster):
    """The failure this module is built around, pointed at a security control.

    `rule_count: 0` here would describe an unreadable role as one granting
    nothing, on the screen where somebody decides to bind it.
    """
    cluster.fails(f"{BASE}/clusterroles/view", api_exception(403))

    capability = grants.role_capability(
        {"kind": "ClusterRole", "name": "view"}, namespace=NAMESPACE,
    )

    assert capability["state"] == "unreadable"
    assert capability["rule_count"] is None
    assert capability["rules"] is None
    assert capability["powers"] == []


def test_a_role_that_does_not_exist_is_absent_not_unreadable(cluster):
    """Two different sentences: one sends the operator to check a spelling, the
    other to grant this console a permission."""
    cluster.fails(f"{BASE}/clusterroles/view", api_exception(404, "NotFound"))

    capability = grants.role_capability(
        {"kind": "ClusterRole", "name": "view"}, namespace=NAMESPACE,
    )

    assert capability["state"] == "absent"
    assert capability["rule_count"] is None


def test_an_aggregate_whose_rules_are_unwritten_is_null_not_zero(cluster):
    """The window before the aggregation controller fills them in. Reporting 0
    would describe an aggregate that will grant cluster-admin as granting
    nothing — and it grows on its own afterwards, with nobody deciding again."""
    cluster.at(
        f"{BASE}/clusterroles/aggregated",
        role_object("aggregated", aggregates=True, omit_rules=True),
    )

    capability = grants.role_capability(
        {"kind": "ClusterRole", "name": "aggregated"}, namespace=NAMESPACE,
    )

    assert capability["state"] == "present"
    assert capability["rules"] is None
    assert capability["rule_count"] is None
    assert capability["aggregates"] is True


def test_a_namespaced_role_is_read_inside_the_namespace(cluster):
    """A Role and a ClusterRole of the same name are different objects, and
    reading the wrong one would describe the wrong grant."""
    cluster.at(
        f"{BASE}/namespaces/{NAMESPACE}/roles/deployer",
        role_object("deployer", kind="Role", namespace=NAMESPACE, rules=[rule()]),
    )

    capability = grants.role_capability(
        {"kind": "Role", "name": "deployer"}, namespace=NAMESPACE,
    )

    assert capability["state"] == "present"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_a_serviceaccount_subject_defaults_to_the_namespace_being_granted_in():
    """A ServiceAccount subject with no namespace matches nobody, so a binding
    carrying one is a grant reported as made that applies to no identity."""
    parsed = grants.validate_subject(
        {"kind": "ServiceAccount", "name": "builder"}, namespace=NAMESPACE,
    )

    assert parsed["namespace"] == NAMESPACE
    assert parsed["apiGroup"] == ""


def test_a_user_subject_gets_the_rbac_api_group():
    parsed = grants.validate_subject({"kind": "User", "name": "alice"}, namespace=NAMESPACE)

    assert parsed["apiGroup"] == RBAC


@pytest.mark.parametrize("payload,parameter", [
    ({"kind": "Robot", "name": "x"}, "subject.kind"),
    ({"kind": "User", "name": "  "}, "subject.name"),
])
def test_a_subject_that_is_not_one_is_refused(payload, parameter):
    with pytest.raises(Invalid) as caught:
        grants.validate_subject(payload, namespace=NAMESPACE)

    assert caught.value.context["parameter"] == parameter


def test_a_rolebinding_cannot_reference_a_serviceaccount_as_its_role():
    with pytest.raises(Invalid) as caught:
        grants.validate_role({"kind": "ServiceAccount", "name": "x"})

    assert caught.value.context["parameter"] == "role.kind"


# --------------------------------------------------------------------------- #
# Subject matching
# --------------------------------------------------------------------------- #

def test_a_serviceaccount_in_another_namespace_is_a_different_subject():
    target = grants.validate_subject(
        {"kind": "ServiceAccount", "name": "builder"}, namespace=NAMESPACE,
    )

    assert grants.subject_matches(subject("ServiceAccount", "builder", NAMESPACE), target)
    assert not grants.subject_matches(subject("ServiceAccount", "builder", "staging"), target)


def test_an_omitted_api_group_does_not_make_it_a_different_subject():
    """A binding written by hand may spell `apiGroup` out, omit it, or give the
    empty string. Comparing it would report an identical subject as different —
    and revoke nothing."""
    target = grants.validate_subject({"kind": "User", "name": "alice"}, namespace=NAMESPACE)

    assert grants.subject_matches({"kind": "User", "name": "alice"}, target)


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def test_a_first_grant_plans_a_create_and_names_the_binding(simple):
    report = grants.plan(NAMESPACE, request())

    assert report["binding"] is None
    assert report["createName"] == "view"
    assert report["requestedSubjects"] == [
        {"kind": "User", "name": "alice", "apiGroup": RBAC},
    ]
    assert report["blocked"] is None


def test_a_second_grant_plans_a_patch_that_keeps_the_first_subject(cluster):
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "bob")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "bob")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request())

    assert report["binding"]["name"] == "view"
    assert [s["name"] for s in report["requestedSubjects"]] == ["bob", "alice"]


def test_granting_a_role_that_confers_full_control_says_so_before_the_write(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/cluster-admin", role_object(
        "cluster-admin", rules=[rule(groups=["*"], resources=["*"], verbs=["*"])],
    ))

    report = grants.plan(NAMESPACE, request(role_name="cluster-admin"))

    assert grants.POWER_FULL_CONTROL in powers(report)
    assert grants.WARN_FULL_CONTROL in codes(report)


def test_full_control_suppresses_the_narrower_consequences(cluster):
    """Both are true; one is actionable. Asking somebody to tick "this also reads
    Secrets" underneath "this grants everything" lengthens the list without
    improving the decision — §28.4's rule, at the handshake layer."""
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/cluster-admin", role_object(
        "cluster-admin", rules=[rule(groups=["*"], resources=["*"], verbs=["*"])],
    ))

    report = grants.plan(NAMESPACE, request(role_name="cluster-admin"))

    assert codes(report) == [grants.WARN_FULL_CONTROL]
    # The powers list is a description rather than a decision, and still carries
    # every one of them.
    assert len(powers(report)) > 1


def test_granting_admin_warns_that_the_grantee_can_grant(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/admin", role_object("admin", rules=[
        rule(groups=[RBAC], resources=["rolebindings"], verbs=["create", "update"]),
        rule(resources=["pods"], verbs=["get", "list"]),
    ]))

    report = grants.plan(NAMESPACE, request(role_name="admin"))
    entry = next(e for e in report["consequences"] if e["code"] == grants.WARN_ESCALATION)

    assert "Revoking this one binding later will not undo" in entry["consequence"]


def test_granting_edit_warns_about_secrets_through_exec(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/edit", role_object("edit", rules=[
        rule(resources=["pods/exec"], verbs=["create"]),
    ]))

    report = grants.plan(NAMESPACE, request(role_name="edit"))
    entry = next(e for e in report["consequences"] if e["code"] == grants.WARN_SECRET_ACCESS)

    assert "through `pods/exec`" in entry["consequence"]


def test_granting_an_unreadable_role_is_its_own_acknowledgement(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.fails(f"{BASE}/clusterroles/view", api_exception(403))

    report = grants.plan(NAMESPACE, request())

    assert grants.WARN_ROLE_UNREADABLE in codes(report)
    assert report["capability"]["rule_count"] is None


def test_granting_a_role_that_does_not_exist_says_when_it_starts_granting(cluster):
    """The API server accepts this binding. It is inert until somebody creates
    the role, and then it is not — with nobody deciding a second time."""
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.fails(f"{BASE}/clusterroles/view", api_exception(404, "NotFound"))

    report = grants.plan(NAMESPACE, request())
    entry = next(e for e in report["consequences"] if e["code"] == grants.WARN_ROLE_ABSENT)

    assert "starts granting" in entry["consequence"]


def test_granting_an_unaggregated_role_says_the_rules_will_grow(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/aggregated", role_object(
        "aggregated", aggregates=True, omit_rules=True,
    ))

    report = grants.plan(NAMESPACE, request(role_name="aggregated"))

    assert grants.WARN_RULES_PENDING in codes(report)


def test_a_plain_view_grant_raises_nothing_to_acknowledge(simple):
    """A handshake that fires on every grant is one nobody reads on the day it
    matters."""
    report = grants.plan(NAMESPACE, request())

    assert report["consequences"] == []


def test_granting_someone_already_bound_is_blocked_not_a_duplicate_subject(cluster):
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request())

    assert report["blocked"] is not None
    assert "already named" in report["blocked"]["message"]
    assert "roleRef cannot be changed" in report["blocked"]["hint"]


def test_two_bindings_for_one_role_are_refused_rather_than_picked_between(cluster):
    """"Remove alice from `view`" has two meanings here. Choosing the first
    silently would report a revoke that left her bound through the second."""
    first = binding("view", subjects=[subject("User", "alice")])
    second = binding("view-extra", subjects=[subject("User", "alice")])
    cluster.at(BINDINGS, listing([second, first]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert report["blocked"] is not None
    assert report["blocked"]["context"]["bindings"] == ["view", "view-extra"]


def test_a_name_collision_is_refused_rather_than_renamed(cluster):
    """§30 does not invent `view-1`. A console that picks a name the operator
    never saw has made a decision about an object they will go looking for."""
    cluster.at(BINDINGS, listing([binding("view", role_name="edit")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request())

    assert report["blocked"] is not None
    assert "already exists here" in report["blocked"]["message"]


# --------------------------------------------------------------------------- #
# The revoke, and the sentence it must not produce
# --------------------------------------------------------------------------- #

def test_a_revoke_removes_only_the_named_subject(cluster):
    cluster.at(BINDINGS, listing([
        binding(subjects=[subject("User", "alice"), subject("User", "bob")]),
    ]))
    cluster.at(f"{BINDINGS}/view", binding(
        subjects=[subject("User", "alice"), subject("User", "bob")],
    ))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert [s["name"] for s in report["requestedSubjects"]] == ["bob"]


def test_revoking_the_last_subject_leaves_an_empty_binding_not_a_delete(cluster):
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert report["requestedSubjects"] == []
    assert report["binding"]["name"] == "view"


def test_a_revoke_that_leaves_another_binding_says_the_access_remains(cluster):
    """The claim this module exists to stop being false. "Revoked" over a
    subject still named by a second binding is an action reported as done that
    was not."""
    cluster.at(BINDINGS, listing([
        binding("view", subjects=[subject("User", "alice")]),
        binding("edit", role_name="edit", subjects=[subject("User", "alice")]),
    ]))
    cluster.at(f"{BINDINGS}/view", binding("view", subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))
    entry = next(e for e in report["consequences"] if e["code"] == grants.WARN_ACCESS_REMAINS)

    assert [row["name"] for row in report["residual"]["namespace_bindings"]] == ["edit"]
    assert "not their access" in entry["consequence"]


def test_a_cluster_role_binding_counts_as_remaining_access(cluster):
    """A ClusterRoleBinding grants everywhere, which includes here. Ignoring it
    would answer "nothing else grants this" about a cluster administrator."""
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([
        cluster_binding(subjects=[subject("User", "alice")]),
    ]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert [row["name"] for row in report["residual"]["cluster_bindings"]] == ["platform-admins"]
    assert grants.WARN_ACCESS_REMAINS in codes(report)


def test_an_unreadable_cluster_listing_is_null_and_never_an_empty_list(cluster):
    """`[]` means the cluster was searched and nothing else grants this — the
    sentence somebody closes a ticket on. This read did not happen."""
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.fails(CLUSTER_BINDINGS, api_exception(403))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert report["residual"]["cluster_bindings"] is None
    assert grants.WARN_RESIDUAL_UNKNOWN in codes(report)
    assert report["partial"] is True
    assert [row["resource"] for row in report["unavailable"]] == ["clusterrolebindings"]


def test_a_clean_revoke_reports_an_empty_residual_rather_than_null(cluster):
    """The other half of the pair: this read *did* happen and found nothing."""
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert report["residual"] == {
        "namespace_bindings": [], "cluster_bindings": [], "cluster_truncated": False,
    }
    assert codes(report) == []


def test_a_truncated_cluster_listing_is_unknown_although_it_read_some(cluster):
    """`[]` with more pages behind it means "the first page did not name them",
    which is not the sentence an empty list otherwise makes. What was read is
    kept — it is strictly more useful than discarding it — and flagged."""
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, {"items": [], "metadata": {"continue": "next-page"}})
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert report["residual"]["cluster_bindings"] == []
    assert report["residual"]["cluster_truncated"] is True
    assert grants.WARN_RESIDUAL_UNKNOWN in codes(report)


def test_a_truncated_namespace_listing_refuses_rather_than_answering_from_page_one(cluster):
    """Which binding to write, whether the subject is already named, and what
    else grants them access are all read off this one listing. Answering any of
    them from a first page is a confident answer about bindings nobody read."""
    cluster.at(BINDINGS, {
        "items": [binding(subjects=[subject("User", "bob")])],
        "metadata": {"continue": "next-page"},
    })
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request())

    assert report["blocked"] is not None
    assert "more RoleBindings than this console read" in report["blocked"]["message"]


def test_a_truncated_namespace_listing_stops_the_write(cluster):
    cluster.at(BINDINGS, {
        "items": [binding(subjects=[subject("User", "bob")])],
        "metadata": {"continue": "next-page"},
    })
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    with pytest.raises(Invalid):
        grants.apply_grant(NAMESPACE, request(), dry_run=False)

    assert cluster.of("POST") == []
    assert cluster.of("PATCH") == []


def test_revoking_from_a_binding_that_does_not_name_them_still_shows_the_residual(cluster):
    """The blocked plan is where "then where does their access come from" gets
    answered, which is why it is a 200 and not only a 422."""
    cluster.at(BINDINGS, listing([
        binding("view", subjects=[subject("User", "bob")]),
        binding("edit", role_name="edit", subjects=[subject("User", "alice")]),
    ]))
    cluster.at(f"{BINDINGS}/view", binding("view", subjects=[subject("User", "bob")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    report = grants.plan(NAMESPACE, request("revoke"))

    assert report["blocked"] is not None
    assert [row["name"] for row in report["residual"]["namespace_bindings"]] == ["edit"]


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def test_a_first_grant_creates_the_binding_through_the_funnel(simple):
    result = grants.apply_grant(NAMESPACE, request(), dry_run=False)

    (path, query, body), = simple.of("POST")
    assert path == BINDINGS
    assert body["roleRef"] == {"apiGroup": RBAC, "kind": "ClusterRole", "name": "view"}
    assert body["subjects"] == [{"kind": "User", "name": "alice", "apiGroup": RBAC}]
    assert result["applied"] is True
    assert query.get("dryRun") is None


def test_a_dry_run_projects_the_create_and_applies_nothing(simple):
    result = grants.apply_grant(NAMESPACE, request(), dry_run=True)

    (_, query, _), = simple.of("POST")
    assert query["dryRun"] == "All"
    assert result["applied"] is False
    assert result["diff"]


def test_a_second_grant_patches_subjects_and_carries_the_version(cluster):
    live = binding(subjects=[subject("User", "bob")], version="9001")
    cluster.at(BINDINGS, listing([live]))
    cluster.at(f"{BINDINGS}/view", live)
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    grants.apply_grant(NAMESPACE, request(), dry_run=False)

    (path, _, body), = cluster.of("PATCH")
    assert path == f"{BINDINGS}/view"
    assert [s["name"] for s in body["subjects"]] == ["bob", "alice"]
    # §0.4 — the version the plan saw travels with the write, so a concurrent
    # grant is a 409 rather than a subject silently dropped by an array replace.
    assert body["metadata"]["resourceVersion"] == "9001"


def test_an_emptied_binding_is_patched_to_an_empty_list_not_to_null(cluster):
    """`null` and `[]` are the same grant — none — but only `[]` shows in the
    diff that the binding survives with nobody in it, which is the fact §30
    refuses to hide."""
    live = binding(subjects=[subject("User", "alice")])
    cluster.at(BINDINGS, listing([live]))
    cluster.at(f"{BINDINGS}/view", live)
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    grants.apply_grant(NAMESPACE, request("revoke"), dry_run=False)

    (_, _, body), = cluster.of("PATCH")
    assert body["subjects"] == []


def test_a_stale_version_is_a_conflict_before_anything_is_written(cluster):
    live = binding(subjects=[subject("User", "bob")], version="9001")
    cluster.at(BINDINGS, listing([live]))
    cluster.at(f"{BINDINGS}/view", live)
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    with pytest.raises(Conflict) as caught:
        grants.apply_grant(
            NAMESPACE, request(resourceVersion="1"), dry_run=False,
        )

    assert caught.value.context["currentResourceVersion"] == "9001"
    assert cluster.of("PATCH") == []


def test_an_unacknowledged_consequence_refuses_before_the_write(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/cluster-admin", role_object(
        "cluster-admin", rules=[rule(groups=["*"], resources=["*"], verbs=["*"])],
    ))

    with pytest.raises(Invalid) as caught:
        grants.apply_grant(NAMESPACE, request(role_name="cluster-admin"), dry_run=False)

    assert caught.value.context["unacknowledged"] == [grants.WARN_FULL_CONTROL]
    assert cluster.of("POST") == []


def test_acknowledging_the_consequence_lets_the_write_through(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/cluster-admin", role_object(
        "cluster-admin", rules=[rule(groups=["*"], resources=["*"], verbs=["*"])],
    ))

    result = grants.apply_grant(
        NAMESPACE, request(role_name="cluster-admin"), dry_run=False,
        acknowledge_consequences=[grants.WARN_FULL_CONTROL],
    )

    assert result["applied"] is True


def test_an_acknowledgement_of_one_role_cannot_be_spent_on_another(cluster):
    """Consequences are recomputed against the cluster as it is now, so a code
    ticked for a `view` preview does not carry into an `admin` write."""
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/admin", role_object("admin", rules=[
        rule(groups=[RBAC], resources=["rolebindings"], verbs=["create"]),
    ]))

    with pytest.raises(Invalid) as caught:
        grants.apply_grant(
            NAMESPACE, request(role_name="admin"), dry_run=False,
            acknowledge_consequences=[grants.WARN_SECRET_ACCESS],
        )

    assert caught.value.context["unacknowledged"] == [grants.WARN_ESCALATION]


def test_a_blocked_plan_is_a_422_at_the_write(cluster):
    cluster.at(BINDINGS, listing([binding(subjects=[subject("User", "alice")])]))
    cluster.at(f"{BINDINGS}/view", binding(subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    with pytest.raises(Invalid):
        grants.apply_grant(NAMESPACE, request(), dry_run=False)

    assert cluster.of("PATCH") == []
    assert cluster.of("POST") == []


def test_a_read_only_console_refuses_and_still_records_the_attempt(simple, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_allow_mutations", False)

    with pytest.raises(MutationsDisabled):
        grants.apply_grant(NAMESPACE, request(), dry_run=False)

    assert simple.of("POST") == []
    assert any(row["outcome"] == "denied" for row in audit_rows())


def test_a_denied_preflight_stops_the_write(simple, fake_k8s):
    """§30 goes through the funnel like everything else, so the preflight is the
    funnel's. This asserts §30 reaches it, not what it does with the answer."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no RBAC policy matched", evaluation_error=None, denied=False,
    )))

    with pytest.raises(RBACDenied):
        grants.apply_grant(NAMESPACE, request(), dry_run=False)

    assert simple.of("POST") == []


def test_the_grant_is_preflighted_as_create_and_the_patch_as_patch(cluster, fake_k8s):
    """`create` and `patch` name different permissions. Preflighting the wrong
    one tells an operator they lack access they hold, or the reverse."""
    seen: list[str] = []

    def review(body):
        seen.append(body.spec.resource_attributes.verb)
        return obj(status=obj(allowed=True, reason=None, evaluation_error=None, denied=False))

    fake_k8s.authorization_v1.returns("create_self_subject_access_review", review)
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))
    grants.apply_grant(NAMESPACE, request(), dry_run=False)

    live = binding(subjects=[subject("User", "bob")])
    cluster.at(BINDINGS, listing([live]))
    cluster.at(f"{BINDINGS}/view", live)
    grants.apply_grant(NAMESPACE, request(), dry_run=False)

    assert seen == ["create", "patch"]


def test_the_audit_sentence_names_the_role_and_what_it_confers(cluster):
    """"grant view to alice" and "grant admin to alice" are the same shape and
    not the same event. The row is what somebody reads a month later."""
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/admin", role_object("admin", rules=[
        rule(groups=[RBAC], resources=["rolebindings"], verbs=["create"]),
    ]))

    grants.apply_grant(
        NAMESPACE, request(role_name="admin"), dry_run=False,
        acknowledge_consequences=[grants.WARN_ESCALATION],
    )

    row = next(r for r in audit_rows() if r["outcome"] == "applied")
    assert "grant ClusterRole/admin to User alice in prod" in row["detail"]
    assert grants.POWER_ESCALATION in row["detail"]


def test_the_audit_sentence_says_when_the_role_was_not_readable(cluster):
    cluster.at(BINDINGS, listing([]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.fails(f"{BASE}/clusterroles/view", api_exception(403))

    grants.apply_grant(
        NAMESPACE, request(), dry_run=False,
        acknowledge_consequences=[grants.WARN_ROLE_UNREADABLE],
    )

    row = next(r for r in audit_rows() if r["outcome"] == "applied")
    assert "role unreadable" in row["detail"]


def test_the_write_reports_the_residual_so_applied_is_not_read_as_revoked(cluster):
    """`applied: true` on a revoke means the subject list is what was sent. The
    residual travelling with it is what stops that being read as "they can no
    longer act here"."""
    cluster.at(BINDINGS, listing([
        binding("view", subjects=[subject("User", "alice")]),
        binding("edit", role_name="edit", subjects=[subject("User", "alice")]),
    ]))
    cluster.at(f"{BINDINGS}/view", binding("view", subjects=[subject("User", "alice")]))
    cluster.at(CLUSTER_BINDINGS, listing([]))
    cluster.at(f"{BASE}/clusterroles/view", role_object("view", rules=[rule()]))

    result = grants.apply_grant(
        NAMESPACE, request("revoke"), dry_run=False,
        acknowledge_consequences=[grants.WARN_ACCESS_REMAINS],
    )

    assert result["applied"] is True
    assert [row["name"] for row in result["residual"]["namespace_bindings"]] == ["edit"]
