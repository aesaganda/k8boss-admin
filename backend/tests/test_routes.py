"""
The unified route model (§13) — the read half.

Three things are tested here harder than anything else, because they are the
three the module exists to get right:

**A cluster that does not serve a backend, and a cluster we could not ask, are
different answers.** ``unsupported`` and ``unknown`` are separate states, and
only ``unknown`` raises the §1.2 partial banner. Getting this backwards during
an aggregated-API outage tells an operator their Routes are gone.

**``admitted`` has three values and the third one is not "no".** A router that
has not reported has not rejected anything. ``None`` means "no router has said",
``False`` means "a router refused it, here is the reason".

**A Route's private key never appears in a row.** It lives in the object's own
spec, which is an OpenShift API design fact rather than a choice this console
gets to make — but a list endpoint that echoed it would put key material in a
table.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.errors import ClusterUnreachable, RBACDenied, Unsupported
from app.resources import catalog
from app.services import routes as svc


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Discovery is cached per cluster; tests must not share one another's."""
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# A stubbed cluster
# --------------------------------------------------------------------------- #

def _resources(*names):
    """A group-version payload serving ``names``, all namespaced and writable."""
    return {
        "resources": [
            {
                "name": name,
                "kind": kind,
                "namespaced": True,
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            }
            for name, kind in names
        ]
    }


def _groups_payload(*names_and_versions):
    groups: list[dict] = []
    by_name: dict[str, list[str]] = {}
    for name, version in names_and_versions:
        by_name.setdefault(name, []).append(version)
    for name, versions in by_name.items():
        groups.append({
            "name": name,
            "preferredVersion": {
                "groupVersion": f"{name}/{versions[0]}", "version": versions[0],
            },
            "versions": [
                {"groupVersion": f"{name}/{v}", "version": v} for v in versions
            ],
        })
    return {"groups": groups}


#: Every group-version this file's stubbed clusters can serve, and what each has.
_PAYLOADS = {
    "/api": {"versions": ["v1"]},
    "/api/v1": _resources(("services", "Service")),
    "/apis/networking.k8s.io/v1": _resources(
        ("ingresses", "Ingress"), ("ingressclasses", "IngressClass"),
    ),
    "/apis/route.openshift.io/v1": _resources(("routes", "Route")),
    "/apis/gateway.networking.k8s.io/v1": _resources(("httproutes", "HTTPRoute")),
    "/apis/gateway.networking.k8s.io/v1beta1": _resources(("httproutes", "HTTPRoute")),
}


def stub_discovery(monkeypatch, *, groups=None, failures=None):
    """Install a fake ``raw_get`` for discovery only, listing ``groups``.

    Anything asked for that is not stubbed is an ``AssertionError``, for the
    same reason ``FakeApi`` behaves that way: a permissive stub would make a
    swallowed failure indistinguishable from a genuinely smaller cluster, which
    is the bug class this whole module is about.
    """
    failures = failures or {}
    if groups is None:
        groups = [
            ("networking.k8s.io", "v1"),
            ("route.openshift.io", "v1"),
            ("gateway.networking.k8s.io", "v1"),
        ]

    def fake_raw_get(path, *, query=None):
        if path in failures:
            raise failures[path]
        if path == "/apis":
            return _groups_payload(*groups)
        if path in _PAYLOADS:
            return _PAYLOADS[path]
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)


# --------------------------------------------------------------------------- #
# Backend state — the three-way distinction
# --------------------------------------------------------------------------- #

def test_a_served_backend_is_available_with_the_version_that_answered(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)

    state = svc.backend_state(svc.BACKENDS_BY_KEY["openshift"])

    assert state.state == svc.STATE_AVAILABLE
    assert state.version == "v1"


def test_a_backend_the_cluster_does_not_serve_is_unsupported(monkeypatch, fake_k8s):
    """A vanilla cluster has no route.openshift.io. That is a fact, not a fault."""
    stub_discovery(monkeypatch, groups=[("networking.k8s.io", "v1")])

    state = svc.backend_state(svc.BACKENDS_BY_KEY["openshift"])

    assert state.state == svc.STATE_UNSUPPORTED
    assert state.version is None
    assert state.error is None


def test_the_second_candidate_version_is_used_when_the_first_is_absent(monkeypatch, fake_k8s):
    """Gateway API's served version depends on which release the cluster installed.

    Pinning v1 would 404 on a v1beta1 cluster, and a 404 reads as "this cluster
    has no HTTPRoutes" — which is the confidently wrong answer.
    """
    stub_discovery(monkeypatch, groups=[("gateway.networking.k8s.io", "v1beta1")])

    state = svc.backend_state(svc.BACKENDS_BY_KEY["gateway"])

    assert state.state == svc.STATE_AVAILABLE
    assert state.version == "v1beta1"


def test_a_group_discovery_could_not_read_is_unknown_and_never_unsupported(
    monkeypatch, fake_k8s,
):
    """The canonical failure this module exists to prevent.

    During an aggregation outage, reporting "this cluster does not serve Routes"
    sends an operator to install an API they already have — or, far worse, to
    re-create an exposure that already exists on a hostname already claimed.
    """
    stub_discovery(
        monkeypatch,
        failures={
            "/apis/route.openshift.io/v1": ApiException(status=503, reason="Service Unavailable"),
        },
    )

    state = svc.backend_state(svc.BACKENDS_BY_KEY["openshift"])

    assert state.state == svc.STATE_UNKNOWN
    assert state.error is not None
    assert "not the same as the cluster not having them" in state.detail


def test_a_forbidden_discovery_is_unknown_and_keeps_the_forbidden_reason(
    monkeypatch, fake_k8s,
):
    """`forbidden` and `unreachable` send an operator to two different places."""
    stub_discovery(
        monkeypatch,
        failures={
            "/apis/route.openshift.io/v1": ApiException(status=403, reason="Forbidden"),
        },
    )

    state = svc.backend_state(svc.BACKENDS_BY_KEY["openshift"])

    assert state.state == svc.STATE_UNKNOWN
    assert isinstance(state.error, RBACDenied)


def test_one_unreadable_version_outranks_another_version_being_absent(
    monkeypatch, fake_k8s,
):
    """v1 unreadable and v1beta1 genuinely absent still means "we do not know".

    Concluding "no HTTPRoutes here" from the v1beta1 miss would be drawing a
    conclusion the v1 blind spot does not entitle us to.
    """
    stub_discovery(
        monkeypatch,
        groups=[("gateway.networking.k8s.io", "v1")],
        failures={
            "/apis/gateway.networking.k8s.io/v1": ApiException(status=503, reason="down"),
        },
    )

    state = svc.backend_state(svc.BACKENDS_BY_KEY["gateway"])

    assert state.state == svc.STATE_UNKNOWN


# --------------------------------------------------------------------------- #
# unsupported is not partial; unknown is
# --------------------------------------------------------------------------- #

def test_an_unsupported_backend_does_not_make_the_listing_partial(monkeypatch, fake_k8s):
    """The banner has to mean something.

    A cluster with no route.openshift.io is not a cluster whose Routes we failed
    to read — there are none. Raising the partial banner on the Routes page of
    every non-OpenShift cluster would train operators to ignore it, which is the
    same reasoning §1.2 gives for not colouring `unsupported` red.
    """
    stub_discovery(monkeypatch, groups=[("networking.k8s.io", "v1")])
    fake_k8s  # noqa: B018 - the cluster context fixture

    monkeypatch.setattr(
        svc.reader, "list_resource",
        lambda *a, **k: {"items": [], "continue": None, "remaining": None,
                         "partial": False, "unavailable": []},
    )

    result = svc.list_routes()

    assert result["partial"] is False
    assert result["unavailable"] == []
    states = {b["backend"]: b["state"] for b in result["backends"]}
    assert states["openshift"] == "unsupported"
    assert states["gateway"] == "unsupported"
    assert states["ingress"] == "available"


def test_an_unknown_backend_does_make_the_listing_partial(monkeypatch, fake_k8s):
    """"We could not look" is exactly what `unavailable[]` is for."""
    stub_discovery(
        monkeypatch,
        failures={
            "/apis/route.openshift.io/v1": ApiException(status=503, reason="down"),
        },
    )
    monkeypatch.setattr(
        svc.reader, "list_resource",
        lambda *a, **k: {"items": [], "continue": None, "remaining": None,
                         "partial": False, "unavailable": []},
    )

    result = svc.list_routes()

    assert result["partial"] is True
    entries = {e["resource"]: e for e in result["unavailable"]}
    assert "routes" in entries
    assert entries["routes"]["reason"] in {"unreachable", "upstream", "timeout"}


def test_one_backend_listing_failing_costs_only_that_backend(monkeypatch, fake_k8s):
    """Degradation is isolated: a forbidden verb on one kind is one entry, not a blank page."""
    stub_discovery(monkeypatch)

    def fake_list(group, version, plural, **kwargs):
        if plural == "routes":
            raise RBACDenied("routes is forbidden", context={"resource": "routes"})
        return {
            "items": [], "continue": None, "remaining": None,
            "partial": False, "unavailable": [],
        }

    monkeypatch.setattr(svc.reader, "list_resource", fake_list)

    result = svc.list_routes()

    assert result["partial"] is True
    assert [e["resource"] for e in result["unavailable"]] == ["routes"]
    # The other two backends still answered.
    states = {b["backend"]: b["state"] for b in result["backends"]}
    assert states["ingress"] == "available"
    assert states["gateway"] == "available"


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

def test_capabilities_reports_every_feature_including_the_unsupported_ones(
    monkeypatch, fake_k8s,
):
    """Rule 11.4 needs a reason for a disabled control, and an absent key has none."""
    stub_discovery(monkeypatch)

    result = svc.capabilities()

    for entry in result["items"]:
        reported = {f["feature"] for f in entry["features"]}
        assert reported == set(svc.ALL_FEATURES), entry["backend"]
        assert all(f["label"] for f in entry["features"])


def test_ingress_does_not_claim_passthrough_or_weighted_backends(monkeypatch, fake_k8s):
    """Both exist only as controller-specific annotations, so neither is claimed."""
    stub_discovery(monkeypatch)

    result = svc.capabilities()
    ingress = next(e for e in result["items"] if e["backend"] == "ingress")
    supported = {f["feature"] for f in ingress["features"] if f["supported"]}

    assert svc.FEATURE_PASSTHROUGH_TLS not in supported
    assert svc.FEATURE_REENCRYPT_TLS not in supported
    assert svc.FEATURE_WEIGHTED_BACKENDS not in supported
    assert svc.FEATURE_EDGE_TLS in supported


def test_an_unavailable_backend_reports_a_null_version_not_a_guess(monkeypatch, fake_k8s):
    """Naming a version we never confirmed would let a caller build a URL from it."""
    stub_discovery(monkeypatch, groups=[("networking.k8s.io", "v1")])

    result = svc.capabilities()
    openshift = next(e for e in result["items"] if e["backend"] == "openshift")

    assert openshift["state"] == "unsupported"
    assert openshift["version"] is None


# --------------------------------------------------------------------------- #
# Route rows — admission is a tri-state
# --------------------------------------------------------------------------- #

def _route(**spec_overrides):
    spec = {
        "host": "checkout.apps.example.com",
        "path": "/",
        "to": {"kind": "Service", "name": "checkout", "weight": 100},
        "port": {"targetPort": 8080},
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {"name": "checkout", "namespace": "prod", "resourceVersion": "41"},
        "spec": spec,
    }


def test_a_route_no_router_has_reported_on_is_admitted_none():
    """Not False. `False` says a router refused it, which is a specific claim."""
    row = svc.route_row(_route(), version="v1")

    assert row["admitted"] is None
    assert row["admittedDetail"] is None


def test_a_route_a_router_admitted_is_true_and_names_the_router():
    obj = _route()
    obj["status"] = {
        "ingress": [
            {
                "host": "checkout.apps.example.com",
                "routerName": "default",
                "routerCanonicalHostname": "router-default.apps.example.com",
                "conditions": [{"type": "Admitted", "status": "True"}],
            }
        ]
    }

    row = svc.route_row(obj, version="v1")

    assert row["admitted"] is True
    assert "default" in row["admittedDetail"]
    assert row["addresses"] == ["router-default.apps.example.com"]


def test_a_refused_route_carries_the_reason_verbatim():
    """`HostAlreadyClaimed` is the word an operator searches for."""
    obj = _route()
    obj["status"] = {
        "ingress": [
            {
                "routerName": "default",
                "conditions": [
                    {
                        "type": "Admitted",
                        "status": "False",
                        "reason": "HostAlreadyClaimed",
                        "message": "route prod/other already exposes checkout.apps.example.com",
                    }
                ],
            }
        ]
    }

    row = svc.route_row(obj, version="v1")

    assert row["admitted"] is False
    assert "HostAlreadyClaimed" in row["admittedDetail"]


def test_a_route_one_router_admitted_and_another_ignored_is_admitted():
    """It is being served. Reporting it as rejected sends someone to debug a working exposure."""
    obj = _route()
    obj["status"] = {
        "ingress": [
            {"routerName": "shard-a",
             "conditions": [{"type": "Admitted", "status": "False", "reason": "NotMine"}]},
            {"routerName": "shard-b",
             "conditions": [{"type": "Admitted", "status": "True"}]},
        ]
    }

    row = svc.route_row(obj, version="v1")

    assert row["admitted"] is True


def test_a_route_row_never_carries_the_private_key():
    """The key is in the object's spec — an OpenShift API fact — but not in a table."""
    obj = _route(tls={
        "termination": "edge",
        "certificate": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
        "key": "-----BEGIN PRIVATE KEY-----\nSUPERSECRET\n-----END PRIVATE KEY-----",
        "insecureEdgeTerminationPolicy": "Redirect",
    })

    row = svc.route_row(obj, version="v1")

    assert "SUPERSECRET" not in repr(row)
    assert row["tls"]["inlineCertificate"] is True
    assert row["tls"]["termination"] == "edge"
    assert row["tls"]["insecurePolicy"] == "Redirect"


def test_a_subdomain_route_reports_the_hostname_it_actually_answers_on():
    """The effective hostname of a generated Route exists only in status."""
    obj = _route(host=None, subdomain="checkout")
    obj["spec"].pop("host")
    obj["status"] = {
        "ingress": [
            {"host": "checkout.apps.example.com", "routerName": "default",
             "conditions": [{"type": "Admitted", "status": "True"}]},
        ]
    }

    row = svc.route_row(obj, version="v1")

    assert row["hosts"] == ["checkout.apps.example.com"]
    assert row["subdomain"] == "checkout"


def test_alternate_backends_become_weighted_targets():
    obj = _route(alternateBackends=[{"kind": "Service", "name": "checkout-canary", "weight": 10}])

    row = svc.route_row(obj, version="v1")

    assert [t["service"] for t in row["targets"]] == ["checkout", "checkout-canary"]
    assert [t["weight"] for t in row["targets"]] == [100, 10]


# --------------------------------------------------------------------------- #
# Ingress rows
# --------------------------------------------------------------------------- #

def _ingress(**spec_overrides):
    spec = {
        "ingressClassName": "haproxy",
        "rules": [
            {
                "host": "shop.example.com",
                "http": {
                    "paths": [
                        {
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {"service": {"name": "shop", "port": {"number": 80}}},
                        }
                    ]
                },
            }
        ],
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "shop", "namespace": "prod", "resourceVersion": "7"},
        "spec": spec,
    }


def test_an_ingress_is_always_admitted_none_because_the_api_has_no_such_condition():
    """Not a gap. Reporting False would invent a rejection the API cannot express."""
    row = svc.ingress_route_row(_ingress(), version="v1")

    assert row["admitted"] is None


def test_an_ingress_with_no_published_address_says_no_controller_claimed_it():
    row = svc.ingress_route_row(_ingress(), version="v1")

    assert row["addresses"] == []
    assert "No ingress controller has published an address" in row["admittedDetail"]


def test_an_ingress_target_has_a_null_weight_not_a_hundred():
    """The Ingress API has no weight field; rendering 100% would imply a split."""
    row = svc.ingress_route_row(_ingress(), version="v1")

    assert row["targets"][0]["weight"] is None


def test_a_tls_block_with_no_hosts_covers_every_host_on_the_ingress():
    """Missing this shows "TLS: none" on an Ingress that is serving HTTPS."""
    row = svc.ingress_route_row(
        _ingress(tls=[{"secretName": "wildcard"}]), version="v1",
    )

    assert row["tlsHosts"] == ["shop.example.com"]
    assert row["tls"]["termination"] == "edge"
    assert row["tls"]["secretName"] == "wildcard"


def test_an_ingress_with_no_tls_reports_no_termination():
    row = svc.ingress_route_row(_ingress(), version="v1")

    assert row["tls"]["termination"] is None


# --------------------------------------------------------------------------- #
# HTTPRoute rows
# --------------------------------------------------------------------------- #

def _httproute(**spec_overrides):
    spec = {
        "parentRefs": [{"name": "public", "namespace": "gateways"}],
        "hostnames": ["api.example.com"],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/v1"}}],
                "backendRefs": [
                    {"name": "api", "port": 8080, "weight": 90},
                    {"name": "api-next", "port": 8080, "weight": 10},
                ],
            }
        ],
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": "api", "namespace": "prod", "resourceVersion": "12"},
        "spec": spec,
    }


def test_an_httproute_reports_no_tls_because_tls_belongs_to_its_gateway():
    """Not "plaintext" — we cannot see the listener from this object."""
    row = svc.httproute_row(_httproute(), version="v1")

    assert row["tls"]["termination"] is None
    assert row["parents"] == ["public"]


def test_httproute_weights_survive_into_the_row():
    row = svc.httproute_row(_httproute(), version="v1")

    assert [(t["service"], t["weight"]) for t in row["targets"]] == [
        ("api", 90), ("api-next", 10),
    ]


def test_an_httproute_accepted_but_with_unresolved_refs_says_so_while_staying_accepted():
    """The Gateway did accept it. Reporting False sends someone to the wrong object."""
    obj = _httproute()
    obj["status"] = {
        "parents": [
            {
                "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                "conditions": [
                    {"type": "Accepted", "status": "True"},
                    {"type": "ResolvedRefs", "status": "False",
                     "reason": "BackendNotFound", "message": "Service api-next not found"},
                ],
            }
        ]
    }

    row = svc.httproute_row(obj, version="v1")

    assert row["admitted"] is True
    assert "do not resolve" in row["admittedDetail"]
    assert "BackendNotFound" in row["admittedDetail"]


def test_an_httproute_no_gateway_has_reported_on_is_admitted_none():
    row = svc.httproute_row(_httproute(), version="v1")

    assert row["admitted"] is None


def test_a_rule_with_no_matches_renders_as_a_catch_all_not_a_blank():
    """An HTTPRoute rule with no matches matches everything; a blank cell hides that."""
    obj = _httproute(rules=[{"backendRefs": [{"name": "api", "port": 80}]}])

    row = svc.httproute_row(obj, version="v1")

    assert row["paths"][0]["path"] == "/"
    assert row["paths"][0]["pathType"] == "PathPrefix"


# --------------------------------------------------------------------------- #
# The row shape is the same for all three
# --------------------------------------------------------------------------- #

def test_every_backend_produces_the_same_row_keys():
    """A shape that varied by backend would push a three-way branch into every cell."""
    rows = [
        svc.route_row(_route(), version="v1"),
        svc.ingress_route_row(_ingress(), version="v1"),
        svc.httproute_row(_httproute(), version="v1"),
    ]

    keys = [set(row) for row in rows]
    assert keys[0] == keys[1] == keys[2]


def test_row_ids_are_unique_across_backends():
    """Two exposures of the same name in the same namespace, different kinds."""
    ingress = svc.ingress_route_row(_ingress(), version="v1")
    route = svc.route_row(_route(), version="v1")

    assert ingress["id"] != route["id"]


# --------------------------------------------------------------------------- #
# Single reads
# --------------------------------------------------------------------------- #

def test_reading_one_exposure_on_an_unreadable_backend_reraises_the_real_failure(
    monkeypatch, fake_k8s,
):
    """"Not found" during an API outage tells an operator their Route was deleted."""
    stub_discovery(
        monkeypatch,
        failures={
            "/apis/route.openshift.io/v1": ApiException(status=503, reason="down"),
        },
    )

    with pytest.raises(ClusterUnreachable):
        svc.get_route("openshift", "prod", "checkout")


def test_reading_one_exposure_on_an_unsupported_backend_is_unsupported(
    monkeypatch, fake_k8s,
):
    stub_discovery(monkeypatch, groups=[("networking.k8s.io", "v1")])

    with pytest.raises(Unsupported):
        svc.get_route("openshift", "prod", "checkout")


def test_an_unknown_backend_key_is_invalid_not_not_found():
    """404 would read as "that backend is not on this cluster", which means something else."""
    from app.errors import Invalid

    with pytest.raises(Invalid):
        svc.resolve_backend("nginx")


# --------------------------------------------------------------------------- #
# The API surface
# --------------------------------------------------------------------------- #

def test_the_capabilities_endpoint_returns_the_envelope(
    client, cluster_id, monkeypatch, fake_k8s,
):
    stub_discovery(monkeypatch)

    response = client.get("/api/routes/capabilities", params={"cluster_id": cluster_id})

    assert response.status_code == 200
    body = response.json()
    assert {b["backend"] for b in body["items"]} == {"openshift", "ingress", "gateway"}
    assert body["partial"] is False


def test_the_routes_endpoint_returns_rows_from_every_served_backend(
    client, cluster_id, monkeypatch, fake_k8s,
):
    stub_discovery(monkeypatch)

    def fake_list(group, version, plural, **kwargs):
        objects = {
            "routes": [_route()],
            "ingresses": [_ingress()],
            "httproutes": [_httproute()],
        }[plural]
        return {
            "items": objects, "continue": None, "remaining": None,
            "partial": False, "unavailable": [],
        }

    monkeypatch.setattr(svc.reader, "list_resource", fake_list)

    response = client.get("/api/routes", params={"cluster_id": cluster_id})

    assert response.status_code == 200
    body = response.json()
    assert {row["backend"] for row in body["items"]} == {"openshift", "ingress", "gateway"}
    assert body["partial"] is False
