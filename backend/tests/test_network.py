"""
NetworkPolicy: the shaper, the selector matcher, and the two §8.2 endpoints.

This kind is the one where "empty is never blind" stops being a slogan about
tables and becomes a statement about a security control, so the assertions here
are mostly about the difference between two things that look the same:

* **A direction nobody governs, and a direction that denies everything.**
  ``policyTypes: [Ingress]`` with no ``egress`` section leaves egress completely
  unrestricted. ``policyTypes: [Ingress, Egress]`` with no ``egress`` section
  denies *all* egress from every selected pod. The YAML around them is nearly
  identical, and a ``rule_count`` of ``0`` in both would render the catastrophic
  one as the harmless one.

* **A policy that selects nothing, and a policy whose pods we could not list.**
  The first is a dead object to delete; the second is a read that failed. Both
  would be ``selected_pods: []`` under the obvious implementation.

* **A pod nothing selects, and a pod we could not decide about.** The first is
  unrestricted and is the finding this console exists to surface; the second is
  unknown. Reporting the second as the first prints "nothing protects this pod"
  about a pod that may well be protected.

* **A rule that allows everything, and a rule that allows something.** Policy
  rules are a *union* of allowances: one wide-open rule cannot be narrowed by a
  restrictive neighbour, however many neighbours there are.

Both object shapes are exercised — camelCase dicts from the generic §4 reader and
attribute-access stand-ins for the typed clients the service layer uses — because
the shaper is reached from both and one that reads only one shape returns a row
of nulls from the other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from kubernetes import client as k8s
from kubernetes.client.rest import ApiException

from app.resources.shaping import (
    label_selector_matches,
    networkpolicy_row,
    policy_types,
    selector_is_empty,
    shaper_for,
)
from tests.conftest import obj

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def policy(
    name="allow-api",
    namespace="prod",
    pod_selector=None,
    types=("Ingress",),
    ingress=None,
    egress=None,
):
    """A NetworkPolicy as the generic §4 reader hands one over: a camelCase dict.

    ``types=None`` omits ``policyTypes`` entirely, which is how the API's own
    defaulting rule gets exercised; ``ingress``/``egress`` of ``None`` omit the
    section rather than sending an empty one, because those two are the pair the
    whole module is careful about.
    """
    spec = {"podSelector": {"matchLabels": {"app": "api"}} if pod_selector is None else pod_selector}
    if types is not None:
        spec["policyTypes"] = list(types)
    if ingress is not None:
        spec["ingress"] = ingress
    if egress is not None:
        spec["egress"] = egress
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z"),
        },
        "spec": spec,
    }


def pod(name="api-0", namespace="prod", labels=None, host_network=False):
    """A pod as the typed ``CoreV1Api`` hands one over."""
    return obj(
        metadata=obj(
            name=name,
            namespace=namespace,
            labels={"app": "api"} if labels is None else labels,
            creation_timestamp=NOW - timedelta(hours=5),
        ),
        spec=obj(containers=[obj(name="app", image="ghcr.io/acme/api:2")], host_network=host_network),
        status=obj(phase="Running", container_statuses=[obj(name="app", ready=True, restart_count=0)]),
    )


# --------------------------------------------------------------------------- #
# label_selector_matches — the tri-state
# --------------------------------------------------------------------------- #

def test_an_empty_selector_matches_every_object():
    """`podSelector: {}` is how "every pod in this namespace" is spelled, and it
    is also what a selector somebody forgot to fill in looks like."""
    assert label_selector_matches({}, {"app": "api"}) is True
    assert label_selector_matches({"matchLabels": {}, "matchExpressions": []}, {}) is True


def test_match_labels_require_every_pair():
    assert label_selector_matches({"matchLabels": {"app": "api"}}, {"app": "api", "tier": "web"}) is True
    assert label_selector_matches({"matchLabels": {"app": "api", "tier": "web"}}, {"app": "api"}) is False


@pytest.mark.parametrize(
    "expression,labels,expected",
    [
        ({"key": "tier", "operator": "In", "values": ["web", "api"]}, {"tier": "web"}, True),
        ({"key": "tier", "operator": "In", "values": ["web"]}, {"tier": "db"}, False),
        ({"key": "tier", "operator": "In", "values": ["web"]}, {}, False),
        ({"key": "tier", "operator": "NotIn", "values": ["db"]}, {"tier": "web"}, True),
        ({"key": "tier", "operator": "NotIn", "values": ["db"]}, {"tier": "db"}, False),
        # NotIn on an absent key matches: the label is not one of the values.
        ({"key": "tier", "operator": "NotIn", "values": ["db"]}, {}, True),
        ({"key": "tier", "operator": "Exists"}, {"tier": ""}, True),
        ({"key": "tier", "operator": "Exists"}, {}, False),
        ({"key": "tier", "operator": "DoesNotExist"}, {}, True),
        ({"key": "tier", "operator": "DoesNotExist"}, {"tier": "web"}, False),
    ],
)
def test_the_four_match_expression_operators(expression, labels, expected):
    assert label_selector_matches({"matchExpressions": [expression]}, labels) is expected


def test_an_operator_this_console_cannot_evaluate_is_unknown_not_false():
    """False would report a policy selecting the whole namespace as selecting
    nothing — so the operator deletes it as dead, or writes a second one."""
    result = label_selector_matches(
        {"matchExpressions": [{"key": "tier", "operator": "GreaterThan", "values": ["3"]}]},
        {"tier": "4"},
    )
    assert result is None


def test_an_absent_selector_is_unknown_rather_than_matching_nothing():
    """Absence is contextual in this API — an absent `namespaceSelector` on a
    peer means "this policy's namespace" — so callers resolve it, not this."""
    assert label_selector_matches(None, {"app": "api"}) is None
    assert selector_is_empty(None) is None
    assert selector_is_empty({}) is True
    assert selector_is_empty({"matchLabels": {"app": "api"}}) is False


def test_a_pod_with_no_labels_is_decidable():
    assert label_selector_matches({}, None) is True
    assert label_selector_matches({"matchLabels": {"app": "api"}}, None) is False


# --------------------------------------------------------------------------- #
# policy_types — declared vs derived
# --------------------------------------------------------------------------- #

def test_policy_types_are_reported_as_declared_when_the_object_declares_them():
    assert policy_types(policy(types=("Ingress", "Egress"))) == (["Ingress", "Egress"], "declared")


def test_an_absent_policy_types_is_derived_the_way_the_api_server_would():
    """Every policy affects ingress; only one carrying an egress section affects
    egress. The derivation is labelled, because it is this console's statement
    rather than the object's."""
    assert policy_types(policy(types=None)) == (["Ingress"], "derived")
    assert policy_types(policy(types=None, egress=[{}])) == (["Ingress", "Egress"], "derived")


def test_an_explicitly_empty_policy_types_governs_nothing_and_says_so():
    """`policyTypes: []` is a real, useless object. It is not an absent field,
    and deriving Ingress for it would invent a restriction."""
    assert policy_types(policy(types=())) == ([], "declared")


# --------------------------------------------------------------------------- #
# networkpolicy_row — the direction that is not governed
# --------------------------------------------------------------------------- #

def test_an_ungoverned_direction_has_a_null_rule_count_never_zero():
    """The assertion this whole module exists for. Ungoverned egress restricts
    nothing; `0` there reads as "no rules", which reads as denied."""
    row = networkpolicy_row(policy(types=("Ingress",), ingress=[{"from": [{"podSelector": {}}]}]))

    assert row["egress"] == {"governed": False, "rule_count": None, "effect": None, "rules": []}
    assert row["ingress"]["governed"] is True
    assert row["ingress"]["rule_count"] == 1


def test_a_governed_direction_with_no_rules_denies_everything():
    """The other half of the pair: `policyTypes: [Ingress, Egress]` with no
    egress section is a total egress block, and it must not look like the above."""
    row = networkpolicy_row(policy(types=("Ingress", "Egress")))

    assert row["egress"]["governed"] is True
    assert row["egress"]["rule_count"] == 0
    assert row["egress"]["effect"] == "deny_all"
    assert row["ingress"]["effect"] == "deny_all"


def test_a_rule_restricting_neither_peer_nor_port_allows_everything():
    """Rules are a union of allowances: the restrictive neighbour cannot narrow
    the open one, so the combined effect is allow_all rather than restricted."""
    row = networkpolicy_row(policy(
        types=("Ingress",),
        ingress=[{"from": [{"podSelector": {"matchLabels": {"app": "web"}}}]}, {}],
    ))

    assert row["ingress"]["effect"] == "allow_all"
    assert row["ingress"]["rules"][1]["allows_all_peers"] is True
    assert row["ingress"]["rules"][1]["allows_all_ports"] is True


def test_a_rule_naming_only_ports_is_restricted_not_open():
    """All sources, but only these ports: narrower than allow_all in the way
    that matters, and the two must not share a badge."""
    row = networkpolicy_row(policy(
        types=("Ingress",), ingress=[{"ports": [{"port": 8080, "protocol": "TCP"}]}],
    ))

    assert row["ingress"]["effect"] == "restricted"
    assert row["ingress"]["rules"][0]["allows_all_peers"] is True
    assert row["ingress"]["rules"][0]["allows_all_ports"] is False


def test_selects_all_pods_is_a_tristate():
    assert networkpolicy_row(policy(pod_selector={}))["selects_all_pods"] is True
    assert networkpolicy_row(policy())["selects_all_pods"] is False


# --------------------------------------------------------------------------- #
# networkpolicy_row — peers
# --------------------------------------------------------------------------- #

def test_the_three_peer_shapes_are_told_apart():
    """`podSelector` alone means pods in *this* namespace; `namespaceSelector`
    alone means every pod in the matching namespaces; both in one entry is the
    intersection. Two YAML characters separate the last two."""
    row = networkpolicy_row(policy(
        types=("Ingress",),
        ingress=[{
            "from": [
                {"podSelector": {"matchLabels": {"app": "web"}}},
                {"namespaceSelector": {"matchLabels": {"env": "prod"}}},
                {
                    "namespaceSelector": {"matchLabels": {"env": "prod"}},
                    "podSelector": {"matchLabels": {"app": "web"}},
                },
                {"ipBlock": {"cidr": "10.0.0.0/8", "except": ["10.1.0.0/16"]}},
            ],
        }],
    ))

    peers = row["ingress"]["rules"][0]["peers"]
    assert [peer["type"] for peer in peers] == ["pod", "namespace", "namespace_pod", "ipBlock"]
    assert peers[3]["cidr"] == "10.0.0.0/8"
    assert peers[3]["except"] == ["10.1.0.0/16"]
    assert peers[0]["namespaceSelector"] is None


def test_a_peer_this_console_cannot_classify_is_kept_and_labelled():
    """Dropping it would render a rule narrower than the one being enforced."""
    row = networkpolicy_row(policy(types=("Ingress",), ingress=[{"from": [{}]}]))

    peers = row["ingress"]["rules"][0]["peers"]
    assert [peer["type"] for peer in peers] == ["unknown"]
    # One unclassifiable peer is still a peer: the rule is not wide open.
    assert row["ingress"]["rules"][0]["allows_all_peers"] is False
    assert row["ingress"]["effect"] == "restricted"


def test_a_named_port_is_left_as_a_name_and_a_protocol_is_not_defaulted():
    row = networkpolicy_row(policy(
        types=("Egress",), egress=[{"ports": [{"port": "metrics"}, {"port": 53, "protocol": "UDP"}]}],
    ))

    assert row["egress"]["rules"][0]["ports"] == [
        {"protocol": None, "port": "metrics", "endPort": None},
        {"protocol": "UDP", "port": 53, "endPort": None},
    ]


def test_the_row_is_registered_for_the_generic_endpoint():
    """§4 serves the listing; a second list endpoint would be a second shaping
    of the same object that could disagree with this one."""
    assert shaper_for("networking.k8s.io", "networkpolicies") is networkpolicy_row


def test_the_row_reads_a_generated_client_object_too():
    """Real ``kubernetes.client`` models, not a stand-in, because this kind is
    the one where the SDK's spelling diverges from the wire's: ``from`` and
    ``except`` are Python keywords, so the generated attributes are ``_from`` and
    ``_except`` and only ``attribute_map`` connects them to the JSON names. A
    ``SimpleNamespace`` fake would let a shaper that guessed the snake_case
    spelling pass here and return an empty rule against a live cluster."""
    typed = k8s.V1NetworkPolicy(
        metadata=k8s.V1ObjectMeta(
            name="allow-api", namespace="prod", creation_timestamp=NOW - timedelta(days=1)
        ),
        spec=k8s.V1NetworkPolicySpec(
            pod_selector=k8s.V1LabelSelector(match_labels={"app": "api"}),
            policy_types=["Ingress", "Egress"],
            ingress=[
                k8s.V1NetworkPolicyIngressRule(
                    _from=[
                        k8s.V1NetworkPolicyPeer(
                            ip_block=k8s.V1IPBlock(cidr="10.0.0.0/8", _except=["10.1.0.0/16"])
                        )
                    ],
                    ports=[k8s.V1NetworkPolicyPort(port=8080, protocol="TCP")],
                )
            ],
        ),
    )

    row = networkpolicy_row(typed)

    assert row["name"] == "allow-api"
    assert row["pod_selector"]["matchLabels"] == {"app": "api"}
    peer = row["ingress"]["rules"][0]["peers"][0]
    assert peer["type"] == "ipBlock"
    assert (peer["cidr"], peer["except"]) == ("10.0.0.0/8", ["10.1.0.0/16"])
    assert row["ingress"]["effect"] == "restricted"
    assert row["egress"]["effect"] == "deny_all", "declared Egress with no section denies all"


# --------------------------------------------------------------------------- #
# GET /api/network/policies/{namespace}/{name}
# --------------------------------------------------------------------------- #

def _stub_policy(fake, np, pods=()):
    fake.networking_v1.returns("read_namespaced_network_policy", np)
    fake.core_v1.returns("list_namespaced_pod", obj(items=list(pods)))


def test_a_policy_reports_the_pods_it_selects(client, fake_k8s):
    _stub_policy(
        fake_k8s,
        policy(),
        pods=[pod("api-0"), pod("web-0", labels={"app": "web"}), pod("api-1")],
    )

    body = client.get("/api/network/policies/prod/allow-api").json()

    assert [row["name"] for row in body["selected_pods"]] == ["api-0", "api-1"]
    assert body["selected_pod_count"] == 2
    assert body["partial"] is False


def test_a_policy_that_selects_nothing_reports_a_real_zero(client, fake_k8s):
    """An empty list here is the finding that gets a dead policy deleted, so it
    has to mean what it says."""
    _stub_policy(fake_k8s, policy(), pods=[pod("web-0", labels={"app": "web"})])

    body = client.get("/api/network/policies/prod/allow-api").json()

    assert body["selected_pods"] == []
    assert body["selected_pod_count"] == 0
    assert body["partial"] is False


def test_pods_that_could_not_be_listed_are_null_not_an_empty_selection(client, fake_k8s):
    """The declared rules still render — losing the pod listing costs the panel,
    not the page — but the count must not read as "this policy governs nothing"."""
    fake_k8s.networking_v1.returns("read_namespaced_network_policy", policy())
    fake_k8s.core_v1.raises("list_namespaced_pod", ApiException(status=403, reason="Forbidden"))

    response = client.get("/api/network/policies/prod/allow-api")
    body = response.json()

    assert response.status_code == 200
    assert body["selected_pods"] is None
    assert body["selected_pod_count"] is None
    assert body["partial"] is True
    assert [(entry["resource"], entry["reason"]) for entry in body["unavailable"]] == [
        ("pods", "forbidden"),
    ]
    assert body["ingress"]["effect"] == "deny_all", "the declared rules survive the failed read"
    assert body["egress"]["governed"] is False


def test_an_undecidable_selector_reports_unknown_rather_than_a_selection(client, fake_k8s):
    _stub_policy(
        fake_k8s,
        policy(pod_selector={"matchExpressions": [{"key": "tier", "operator": "GreaterThan", "values": ["3"]}]}),
        pods=[pod("api-0")],
    )

    body = client.get("/api/network/policies/prod/allow-api").json()

    assert body["selected_pods"] is None
    assert body["selected_pod_count"] is None
    assert body["partial"] is True
    assert [entry["reason"] for entry in body["unavailable"]] == ["unsupported"]


def test_a_missing_policy_is_a_404_not_an_empty_object(client, fake_k8s):
    fake_k8s.networking_v1.raises(
        "read_namespaced_network_policy", ApiException(status=404, reason="Not Found")
    )

    response = client.get("/api/network/policies/prod/gone")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# GET /api/network/isolation
# --------------------------------------------------------------------------- #

def _stub_isolation(fake, policies=(), pods=(), namespaced=False):
    if namespaced:
        fake.networking_v1.returns("list_namespaced_network_policy", obj(items=list(policies)))
        fake.core_v1.returns("list_namespaced_pod", obj(items=list(pods)))
    else:
        fake.networking_v1.returns(
            "list_network_policy_for_all_namespaces", obj(items=list(policies))
        )
        fake.core_v1.returns("list_pod_for_all_namespaces", obj(items=list(pods)))


def test_a_pod_no_policy_selects_is_reported_unrestricted(client, fake_k8s):
    """Kubernetes defaults to allow, so this row is the whole point of the view."""
    _stub_isolation(fake_k8s, policies=[policy()], pods=[pod("web-0", labels={"app": "web"})])

    body = client.get("/api/network/isolation").json()

    row = body["items"][0]
    assert row["policies"] == []
    assert row["ingress"] == {"isolated": False, "effect": None, "policies": []}
    assert row["egress"] == {"isolated": False, "effect": None, "policies": []}
    assert body["summary"]["ingress"] == {"isolated": 0, "unrestricted": 1, "unknown": 0}


def test_a_selected_pod_names_the_policies_that_govern_each_direction(client, fake_k8s):
    _stub_isolation(
        fake_k8s,
        policies=[
            policy(name="deny-ingress", pod_selector={}, types=("Ingress",)),
            policy(name="allow-dns", types=("Egress",), egress=[{"ports": [{"port": 53}]}]),
        ],
        pods=[pod("api-0")],
    )

    row = client.get("/api/network/isolation").json()["items"][0]

    assert row["policies"] == ["deny-ingress", "allow-dns"]
    assert row["ingress"] == {"isolated": True, "effect": "deny_all", "policies": ["deny-ingress"]}
    assert row["egress"] == {"isolated": True, "effect": "restricted", "policies": ["allow-dns"]}


def test_allowances_from_several_policies_add_rather_than_narrow(client, fake_k8s):
    """One wide-open policy makes the pod's ingress allow_all however restrictive
    its neighbour is. A reader expecting first-match-wins gets this backwards."""
    _stub_isolation(
        fake_k8s,
        policies=[
            policy(name="tight", types=("Ingress",), ingress=[{"from": [{"podSelector": {"matchLabels": {"app": "web"}}}]}]),
            policy(name="wide", types=("Ingress",), ingress=[{}]),
        ],
        pods=[pod("api-0")],
    )

    row = client.get("/api/network/isolation").json()["items"][0]

    assert row["ingress"]["effect"] == "allow_all"
    assert row["ingress"]["policies"] == ["tight", "wide"]


def test_a_policy_does_not_select_across_namespaces(client, fake_k8s):
    """`podSelector` is namespace-local. Matching cluster-wide would report a
    policy in dev as protecting a pod in prod."""
    _stub_isolation(
        fake_k8s,
        policies=[policy(namespace="dev", pod_selector={})],
        pods=[pod("api-0", namespace="prod")],
    )

    row = client.get("/api/network/isolation").json()["items"][0]

    assert row["policies"] == []
    assert row["ingress"]["isolated"] is False


def test_an_undecidable_selector_leaves_isolation_unknown_not_false(client, fake_k8s):
    """"Nothing protects this pod" is the sentence an operator acts on. It is not
    said about a pod whose selector could not be evaluated."""
    _stub_isolation(
        fake_k8s,
        policies=[policy(pod_selector={"matchExpressions": [{"key": "t", "operator": "Newer", "values": []}]})],
        pods=[pod("api-0")],
    )

    body = client.get("/api/network/isolation").json()
    row = body["items"][0]

    assert row["ingress"]["isolated"] is None
    assert row["egress"]["isolated"] is None
    assert body["partial"] is True
    assert [entry["reason"] for entry in body["unavailable"]] == ["unsupported"]
    assert body["summary"]["ingress"] == {"isolated": 0, "unrestricted": 0, "unknown": 1}


def test_a_definite_governing_policy_still_isolates_when_another_is_undecidable(client, fake_k8s):
    """Isolation and effect fail in opposite directions: the pod *is* selected by
    something that governs ingress, but an undecided policy could widen what is
    permitted, so the effect — not the flag — is what goes unknown."""
    _stub_isolation(
        fake_k8s,
        policies=[
            policy(name="deny-ingress", pod_selector={}, types=("Ingress",)),
            policy(name="mystery", pod_selector={"matchExpressions": [{"key": "t", "operator": "Newer"}]}),
        ],
        pods=[pod("api-0")],
    )

    row = client.get("/api/network/isolation").json()["items"][0]

    assert row["ingress"]["isolated"] is True
    assert row["ingress"]["effect"] is None
    assert row["ingress"]["policies"] == ["deny-ingress"]


def test_a_failed_policy_listing_fails_the_page_rather_than_reporting_nothing_selected(
    client, fake_k8s
):
    """Degrading to a table of `isolated: false` would render a page confidently
    reporting that nothing is protected, assembled from a read that failed."""
    fake_k8s.networking_v1.raises(
        "list_network_policy_for_all_namespaces", ApiException(status=403, reason="Forbidden")
    )

    response = client.get("/api/network/isolation")

    assert response.status_code == 403
    assert response.json()["error"] == "rbac_denied"


def test_a_failed_pod_listing_fails_the_page_too(client, fake_k8s):
    _stub_isolation(fake_k8s, policies=[policy()])
    fake_k8s.core_v1.raises(
        "list_pod_for_all_namespaces", ApiException(status=403, reason="Forbidden")
    )

    assert client.get("/api/network/isolation").status_code == 403


def test_a_namespace_scoped_request_uses_the_namespaced_listings(client, fake_k8s):
    _stub_isolation(fake_k8s, policies=[policy()], pods=[pod("api-0")], namespaced=True)

    body = client.get("/api/network/isolation?namespace=prod").json()

    assert body["namespace"] == "prod"
    assert body["policy_count"] == 1
    assert fake_k8s.core_v1.called("list_namespaced_pod")[0][0] == ("prod",)


def test_host_network_pods_are_flagged_rather_than_judged(client, fake_k8s):
    """Most CNIs do not apply NetworkPolicy to host-network pods, but "most" is
    not a claim this console can make about a plugin it cannot see."""
    _stub_isolation(
        fake_k8s, policies=[policy(pod_selector={})], pods=[pod("api-0", host_network=True)]
    )

    body = client.get("/api/network/isolation").json()

    assert body["items"][0]["host_network"] is True
    assert body["items"][0]["ingress"]["isolated"] is True
    assert body["summary"]["host_network"] == 1


def test_rows_are_sorted_so_two_identical_reads_render_the_same(client, fake_k8s):
    _stub_isolation(
        fake_k8s,
        policies=[],
        pods=[pod("web-0", namespace="prod"), pod("api-0", namespace="dev"), pod("api-9", namespace="dev")],
    )

    body = client.get("/api/network/isolation").json()

    assert [(row["namespace"], row["name"]) for row in body["items"]] == [
        ("dev", "api-0"), ("dev", "api-9"), ("prod", "web-0"),
    ]
