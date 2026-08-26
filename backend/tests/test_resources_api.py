"""
The generic resource endpoint's two pieces of judgement (§4).

``test_reader.py`` covers what the read path returns and what it strips. What is
left is the work the *route* does on top of it, and both halves of it are places
where a plausible answer would be a wrong one:

**Endpoint counts.** A Service row's ``endpoint_count`` is a second read, of
EndpointSlices, and its failure modes all look like zero. A headless Service
genuinely has no slice; a Service whose slices we were forbidden to list has an
unknown number of backends; a Service whose slice fell on the far side of a page
boundary has all of its backends. Rendered as ``0``, the last two say "nothing is
behind this Service" — which is the answer an operator acts on by restarting
something that was fine.

**The Secret reveal gate.** Values require the deployment to have opted in, a
preflight that passes, and an audit record either way. The interesting assertions
here are the negative ones: that a denial is still recorded, and that a refusal
happens before anything is returned.
"""

from __future__ import annotations

import base64

import pytest
from kubernetes.client.rest import ApiException

from app.api import resources as resources_api
from app.audit import recorder
from app.config import settings
from app.resources import catalog
from tests.conftest import obj

SECRET_VALUE = "hunter2-do-not-leak"
SECRET_B64 = base64.b64encode(SECRET_VALUE.encode()).decode()


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# A stubbed cluster, addressed by path
# --------------------------------------------------------------------------- #

DISCOVERY = {
    "/api": {"versions": ["v1"]},
    "/apis": {"groups": [{
        "name": "discovery.k8s.io",
        "preferredVersion": {"groupVersion": "discovery.k8s.io/v1", "version": "v1"},
        "versions": [{"groupVersion": "discovery.k8s.io/v1", "version": "v1"}],
    }]},
    "/api/v1": {"resources": [
        {"name": "services", "kind": "Service", "namespaced": True,
         "verbs": ["get", "list"]},
        {"name": "secrets", "kind": "Secret", "namespaced": True,
         "verbs": ["get", "list"]},
    ]},
    "/apis/discovery.k8s.io/v1": {"resources": [
        {"name": "endpointslices", "kind": "EndpointSlice", "namespaced": True,
         "verbs": ["get", "list"]},
    ]},
}


class FakeApiServer:
    """A path-addressed ``catalog.raw_get``.

    A payload may be a callable taking the query parameters, which is how the
    EndpointSlice paging tests answer the same path differently per page — the
    thing a dict-only fake cannot express and the thing the page budget is about.
    """

    def __init__(self, payloads=None, failures=None):
        self.payloads = {**DISCOVERY, **(payloads or {})}
        self.failures = failures or {}
        self.reads: list[tuple[str, dict]] = []

    def __call__(self, path, *, query=None):
        params = {key: value for key, value in (query or []) if value is not None}
        self.reads.append((path, params))
        if path in self.failures:
            raise self.failures[path]
        if path not in self.payloads:
            raise AssertionError(f"unstubbed read: {path}")
        payload = self.payloads[path]
        return payload(params) if callable(payload) else payload

    def reads_of(self, path):
        return [params for read_path, params in self.reads if read_path == path]


@pytest.fixture
def api_server(monkeypatch, fake_k8s):
    """Install a fake cluster and hand back the recorder of what was read."""
    def install(payloads=None, failures=None):
        server = FakeApiServer(payloads, failures)
        monkeypatch.setattr(catalog, "raw_get", server)
        return server

    return install


def _service(name, namespace="prod", *, cluster_ip="10.0.0.1"):
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "resourceVersion": "7"},
        "spec": {"type": "ClusterIP", "clusterIP": cluster_ip,
                 "selector": {"app": name},
                 "ports": [{"port": 80, "protocol": "TCP"}]},
    }


def _slice(service, addresses, *, namespace="prod", name=None):
    return {
        "apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
        "metadata": {
            "name": name or f"{service}-abc12", "namespace": namespace,
            "labels": {resources_api._SLICE_SERVICE_LABEL: service},
        },
        "endpoints": [{"addresses": [address]} for address in addresses],
    }


def _listing(items, *, cont=""):
    return {"metadata": {"continue": cont}, "items": items}


SERVICES_PATH = "/api/v1/namespaces/prod/services"
SLICES_PATH = "/apis/discovery.k8s.io/v1/namespaces/prod/endpointslices"
ALL_SLICES_PATH = "/apis/discovery.k8s.io/v1/endpointslices"


# --------------------------------------------------------------------------- #
# Endpoint counts
# --------------------------------------------------------------------------- #

def test_a_service_with_no_slice_counts_zero_and_one_with_slices_counts_addresses(
    client, api_server,
):
    """Both numbers come from the same successful tally, and the difference
    between them is the whole point of making it: `0` here is a real answer about
    a headless or mis-selected Service, not a failure rendered as a number."""
    api_server({
        SERVICES_PATH: _listing([_service("checkout"), _service("headless")]),
        SLICES_PATH: _listing([
            _slice("checkout", ["10.1.0.1", "10.1.0.2"]),
            _slice("checkout", ["10.1.0.3"], name="checkout-def34"),
        ]),
    })

    body = client.get("/api/resources/core/v1/services?namespace=prod").json()

    counts = {row["name"]: row["endpoint_count"] for row in body["items"]}
    assert counts == {"checkout": 3, "headless": 0}
    assert body["partial"] is False


def test_slices_that_could_not_be_listed_leave_every_count_null(client, api_server):
    """`0` would state that nothing backs these Services. The count is dropped,
    the rows are still returned, and the reason is named."""
    api_server(
        {SERVICES_PATH: _listing([_service("checkout")])},
        failures={SLICES_PATH: ApiException(status=403, reason="Forbidden")},
    )

    body = client.get("/api/resources/core/v1/services?namespace=prod").json()

    assert [row["endpoint_count"] for row in body["items"]] == [None]
    assert body["items"][0]["name"] == "checkout", "the Service row itself survives"
    assert body["partial"] is True
    assert body["unavailable"] == [{
        "group": "discovery.k8s.io", "resource": "endpointslices",
        "namespace": "prod", "reason": "forbidden",
        "detail": body["unavailable"][0]["detail"],
    }]


def test_a_tally_that_spans_pages_is_completed_before_it_is_used(client, api_server):
    """A Service whose slices straddle a page boundary would otherwise be
    reported with only the addresses on the first page."""
    pages = {
        None: _listing([_slice("checkout", ["10.1.0.1"])], cont="page-2"),
        "page-2": _listing([
            _slice("checkout", ["10.1.0.2"], name="checkout-def34"),
        ]),
    }
    api_server({
        SERVICES_PATH: _listing([_service("checkout")]),
        SLICES_PATH: lambda params: pages[params.get("continue")],
    })

    body = client.get("/api/resources/core/v1/services?namespace=prod").json()

    assert [row["endpoint_count"] for row in body["items"]] == [2]
    assert body["partial"] is False


def test_a_tally_that_outruns_the_page_budget_reports_unknown_not_a_short_count(
    client, api_server,
):
    """The dangerous middle case: a partial tally is a confident wrong number,
    and this is the one place the module gives up rather than round down."""
    api_server({
        SERVICES_PATH: _listing([_service("checkout")]),
        SLICES_PATH: lambda params: _listing(
            [_slice("checkout", ["10.1.0.1"])], cont="always-more",
        ),
    })

    body = client.get("/api/resources/core/v1/services?namespace=prod").json()

    assert [row["endpoint_count"] for row in body["items"]] == [None]
    assert body["unavailable"][0]["reason"] == "timeout"
    assert body["partial"] is True


def test_the_slice_listing_is_narrowed_to_the_namespace_the_page_is_showing(
    client, api_server,
):
    server = api_server({
        SERVICES_PATH: _listing([_service("checkout"), _service("payments")]),
        SLICES_PATH: _listing([]),
    })

    client.get("/api/resources/core/v1/services?namespace=prod")

    assert server.reads_of(SLICES_PATH), "the namespaced slice path was read"
    assert server.reads_of(ALL_SLICES_PATH) == [], (
        "a cluster-wide slice read for a single-namespace page transfers a "
        "listing whose every other namespace is then thrown away"
    )


def test_a_page_spanning_namespaces_needs_the_cluster_wide_slice_listing(
    client, api_server,
):
    """Filtering to one of them would leave every Service in the others counted
    as zero."""
    server = api_server({
        "/api/v1/services": _listing([
            _service("checkout", "prod"), _service("checkout", "staging"),
        ]),
        ALL_SLICES_PATH: _listing([
            _slice("checkout", ["10.1.0.1"], namespace="prod"),
            _slice("checkout", ["10.2.0.1", "10.2.0.2"], namespace="staging"),
        ]),
    })

    body = client.get("/api/resources/core/v1/services").json()

    assert [row["endpoint_count"] for row in body["items"]] == [1, 2], (
        "counted per (namespace, name): two Services of the same name in "
        "different namespaces are not the same Service"
    )
    assert server.reads_of(ALL_SLICES_PATH)


def test_an_empty_service_page_does_not_list_slices_at_all(client, api_server):
    """There is nothing to decorate, and the tally would be a read whose result
    could only be discarded."""
    server = api_server({SERVICES_PATH: _listing([])})

    body = client.get("/api/resources/core/v1/services?namespace=prod").json()

    assert body == {"items": [], "continue": None, "remaining": None,
                    "partial": False, "unavailable": []}
    assert server.reads_of(SLICES_PATH) == []


def test_a_slice_with_no_service_label_is_not_attributed_to_a_service(
    client, api_server,
):
    """The owning Service is not derivable from the slice's own name, which
    carries a random suffix, so an unlabelled slice cannot be counted anywhere —
    and guessing would inflate whichever Service the name resembled."""
    api_server({
        SERVICES_PATH: _listing([_service("checkout")]),
        SLICES_PATH: _listing([
            {"metadata": {"name": "checkout-orphan", "namespace": "prod"},
             "endpoints": [{"addresses": ["10.1.0.9"]}]},
        ]),
    })

    body = client.get("/api/resources/core/v1/services?namespace=prod").json()

    assert [row["endpoint_count"] for row in body["items"]] == [0]


def test_endpoint_counts_are_not_computed_for_the_raw_shape(client, api_server):
    """`raw` returns manifests, which have no count field to fill; making the
    second read anyway would slow an export down for a value nobody reads."""
    server = api_server({SERVICES_PATH: _listing([_service("checkout")])})

    body = client.get("/api/resources/core/v1/services?namespace=prod&shape=raw").json()

    assert body["items"][0]["kind"] == "Service", "the manifest, not the §8 row"
    assert server.reads_of(SLICES_PATH) == []


# --------------------------------------------------------------------------- #
# The Secret reveal gate
# --------------------------------------------------------------------------- #

def _secret():
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod", "resourceVersion": "12"},
        "type": "Opaque",
        "data": {"password": SECRET_B64},
    }


def _allow_preflight(fake_k8s, *, allowed=True):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(
        status=obj(allowed=allowed, reason=None, evaluation_error=None, denied=False),
    ))


def audit_rows():
    return recorder.query(limit=50)["items"]


def test_reveal_is_refused_when_the_deployment_has_not_opted_in(
    client, fake_k8s, api_server,
):
    """`SECRET_REVEAL_ENABLED` rather than the mutations flag: reading a Secret
    and writing a Deployment have different blast radii, and requiring the write
    flag would make the narrower setting unusable."""
    api_server({"/api/v1/namespaces/prod/secrets/db": _secret()})

    response = client.get("/api/resources/core/v1/secrets/db?namespace=prod&reveal=true")

    assert response.status_code == 403
    assert SECRET_VALUE not in response.text
    assert SECRET_B64 not in response.text
    assert response.json()["error"] == "mutations_disabled"
    assert "SECRET_REVEAL_ENABLED" in response.json()["hint"]


def test_a_reveal_the_operator_may_not_perform_is_refused_and_recorded(
    client, fake_k8s, api_server, monkeypatch,
):
    """An audit trail holding only successful reveals cannot answer "did anyone
    try", which is the question asked after an incident."""
    monkeypatch.setattr(settings, "secret_reveal_enabled", True)
    api_server({"/api/v1/namespaces/prod/secrets/db": _secret()})
    _allow_preflight(fake_k8s, allowed=False)

    response = client.get("/api/resources/core/v1/secrets/db?namespace=prod&reveal=true")

    assert response.status_code == 403
    assert SECRET_B64 not in response.text
    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["verb"] == "get"
    assert row["target"]["resource"] == "secrets"
    assert row["detail"] == "Secret values requested"
    assert SECRET_VALUE not in str(row), "the audit trail is not a copy of the Secret"


def test_a_permitted_reveal_returns_the_values_and_records_that_it_did(
    client, fake_k8s, api_server, monkeypatch,
):
    monkeypatch.setattr(settings, "secret_reveal_enabled", True)
    api_server({"/api/v1/namespaces/prod/secrets/db": _secret()})
    _allow_preflight(fake_k8s)

    response = client.get("/api/resources/core/v1/secrets/db?namespace=prod&reveal=true")

    assert response.json()["data"] == {"password": SECRET_B64}
    (row,) = audit_rows()
    assert row["outcome"] == "applied"
    assert row["dry_run"] is False
    assert row["detail"] == "Secret values revealed"


def test_a_read_without_reveal_is_redacted_and_not_audited(
    client, fake_k8s, api_server, monkeypatch,
):
    """Redacted reads are the normal way to browse Secrets, and recording every
    one of them would bury the reveals that matter in noise."""
    monkeypatch.setattr(settings, "secret_reveal_enabled", True)
    api_server({"/api/v1/namespaces/prod/secrets/db": _secret()})

    response = client.get("/api/resources/core/v1/secrets/db?namespace=prod")

    assert response.json()["data"] == {"password": None}, (
        "the key names are not the secret, and dropping `data` would show a "
        "Secret that appears to be empty"
    )
    assert audit_rows() == []


def test_the_yaml_route_refuses_a_reveal_on_the_same_terms(client, fake_k8s, api_server):
    """The same bytes with a different Content-Type. A gate on one route only is
    not a gate."""
    api_server({"/api/v1/namespaces/prod/secrets/db": _secret()})

    response = client.get(
        "/api/resources/core/v1/secrets/db/yaml?namespace=prod&reveal=true"
    )

    assert response.status_code == 403
    assert SECRET_B64 not in response.text


def test_a_permitted_reveal_through_the_yaml_route_is_audited_too(
    client, fake_k8s, api_server, monkeypatch,
):
    monkeypatch.setattr(settings, "secret_reveal_enabled", True)
    api_server({"/api/v1/namespaces/prod/secrets/db": _secret()})
    _allow_preflight(fake_k8s)

    response = client.get(
        "/api/resources/core/v1/secrets/db/yaml?namespace=prod&reveal=true"
    )

    assert response.status_code == 200
    assert SECRET_B64 in response.text
    assert [row["outcome"] for row in audit_rows()] == ["applied"]


# --------------------------------------------------------------------------- #
# The catalog, and the writes this module does not implement
# --------------------------------------------------------------------------- #

def test_the_catalog_reports_the_groups_it_could_not_enumerate(client, api_server):
    """One unreachable aggregated API server must not empty the picker, and the
    resources it serves must not silently disappear from it either."""
    api_server(failures={
        "/apis/discovery.k8s.io/v1": ApiException(status=503, reason="Service Unavailable"),
    })

    body = client.get("/api/resources/catalog").json()

    assert {item["resource"] for item in body["items"]} == {"services", "secrets"}
    assert body["partial"] is True
    assert body["unavailable"][0]["group"] == "discovery.k8s.io"


@pytest.mark.parametrize(
    ("method", "url", "payload", "delegate"),
    [
        ("post", "/api/resources/core/v1/configmaps",
         {"yaml": "kind: ConfigMap"}, "create_from_yaml"),
        ("put", "/api/resources/core/v1/configmaps/settings",
         {"yaml": "kind: ConfigMap", "resourceVersion": "9"}, "update_from_yaml"),
    ],
)
def test_a_write_is_delegated_to_the_funnel_with_the_core_group_normalized(
    client, monkeypatch, method, url, payload, delegate,
):
    """Implemented here, a write would be one that skipped the mutations gate,
    the preflight, the dry run and the audit record. And `core` is a URL
    spelling: nothing below the route may see it."""
    seen = {}

    def record(*args):
        seen["args"] = args
        return {"applied": False}

    monkeypatch.setattr(resources_api.apply_service, delegate, record)

    response = getattr(client, method)(url, json=payload)

    assert response.status_code == 200
    assert seen["args"][0] == "", "the core group is the empty string below the route"
    assert seen["args"][-1] is True, (
        "dryRun defaults to true: a client that forgot the field gets a "
        "projection, not a change to a production cluster"
    )


def test_a_delete_passes_the_propagation_policy_through_unchanged(client, monkeypatch):
    """`Orphan` leaves the dependents behind, which is a materially different
    outcome from the default — not something to be inferred."""
    seen = {}

    def record(*args):
        seen["args"] = args
        return {"applied": False}

    monkeypatch.setattr(resources_api.apply_service, "delete_resource", record)

    response = client.delete(
        "/api/resources/apps/v1/deployments/checkout"
        "?namespace=prod&propagationPolicy=Orphan&dryRun=false"
    )

    assert response.status_code == 200
    assert seen["args"] == ("apps", "v1", "deployments", "prod", "checkout",
                            "Orphan", False)


def test_a_propagation_policy_the_api_server_does_not_define_is_rejected(client):
    """Passed through, an invented policy is a delete whose dependents' fate is
    decided by whatever the API server does with an unknown value."""
    response = client.delete(
        "/api/resources/apps/v1/deployments/checkout?namespace=prod"
        "&propagationPolicy=Whenever"
    )

    assert response.status_code == 422
