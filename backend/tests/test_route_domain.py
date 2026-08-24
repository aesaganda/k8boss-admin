"""§13 — the cluster's wildcard domain and the hostnames generated under it.

The defect this file is written against is a hostname that looks right. A
generated `shop-web.apps.example.com` under a wildcard that does not exist
produces an exposure the API server accepts, the controller admits, and DNS
cannot resolve — created, green, and routing nothing. So the tests here care
much more about when a domain is *withheld* than about when one is produced.
"""

from __future__ import annotations

import pytest

from app.errors import ClusterUnreachable, Invalid
from app.k8s.context import reset_current_cluster_id, set_current_cluster_id
from app.resources import catalog
from app.services import route_domain, routes as routes_service


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Discovery is cached per cluster for a minute; these tests must not share it.

    Same reason as test_catalog.py's copy. Here it bites specifically: several
    tests in this file stub *different* clusters — one OpenShift, one plain, one
    whose discovery fails — and a cached result from the previous one makes the
    next pass or fail for reasons unrelated to what it stubbed.
    """
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


def _discovery(monkeypatch, *, groups, failures=None, config=None):
    """Discovery that serves ``groups``, and the OpenShift ingress config object."""
    failures = failures or {}

    def fake_raw_get(path, *, query=None):
        if path in failures:
            raise failures[path]
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/api/v1":
            return {"resources": []}
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": name,
                        "preferredVersion": {
                            "groupVersion": f"{name}/{version}", "version": version,
                        },
                        "versions": [
                            {"groupVersion": f"{name}/{version}", "version": version},
                        ],
                    }
                    for name, version in groups
                ]
            }
        if path == "/apis/config.openshift.io/v1":
            return {
                "resources": [
                    {"name": "ingresses", "kind": "Ingress", "namespaced": False,
                     "verbs": ["get", "list"]},
                ]
            }
        # Every other advertised group enumerates to nothing. It has to answer
        # rather than assert: a group listed in /apis whose resource list fails
        # puts an entry in catalog's own unavailable list, and `resolve` then
        # refuses to call anything `unsupported` — so an incomplete stub here
        # would make a plain cluster look like an outage and quietly test the
        # opposite of what these two tests claim.
        for name, version in groups:
            if path == f"/apis/{name}/{version}":
                return {"resources": []}
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)

    if config is not None:
        from app.resources import reader

        def fake_get_resource(group, version, plural, name, *, namespace=None):
            assert (group, plural, name) == ("config.openshift.io", "ingresses", "cluster")
            return config

        monkeypatch.setattr(reader, "get_resource", fake_get_resource)


# --------------------------------------------------------------------------- #
# normalize_domain
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "typed,expected",
    [
        ("apps.example.com", "apps.example.com"),
        # How the same domain is written in a zone file and said out loud. Both
        # mean the wildcard; refusing them teaches a distinction with no payoff.
        ("*.apps.example.com", "apps.example.com"),
        (".apps.example.com", "apps.example.com"),
        ("apps.example.com.", "apps.example.com"),
        ("  Apps.Example.COM  ", "apps.example.com"),
        ("", None),
        (None, None),
    ],
)
def test_a_domain_is_accepted_however_an_operator_writes_it(typed, expected):
    assert route_domain.normalize_domain(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "https://apps.example.com",   # a URL, not a domain
        "apps.example.com:443",       # a port
        "apps.example.com/routes",    # a path
        "example",                    # one label cannot carry a wildcard
        "apps..example.com",          # empty label
        "apps_1.example.com",         # underscore is not a DNS label character
    ],
)
def test_a_domain_that_would_not_resolve_is_refused_at_the_form(typed):
    """Refused here, where it is a field error, rather than at the exposure.

    Stored, this becomes a generated hostname on every route the operator
    creates afterwards — each one accepted by the API server and resolvable by
    nobody.
    """
    with pytest.raises(Invalid) as caught:
        route_domain.normalize_domain(typed)

    assert caught.value.context["field"] == "app_domain"


# Hostname *generation* is not tested here, because it does not happen here.
# The rule lives in RouteDialog.jsx — it depends on two fields that change per
# keystroke — and routes.spec.js covers it against the code that actually runs.
# There were three tests in this slot asserting against a Python copy nothing
# called; they passed regardless of whether the shipped rule worked.


# --------------------------------------------------------------------------- #
# discover_domain
# --------------------------------------------------------------------------- #

def test_openshift_publishes_its_domain_and_we_read_it(monkeypatch, fake_k8s):
    _discovery(
        monkeypatch,
        groups=[("config.openshift.io", "v1")],
        config={"spec": {"domain": "apps.ocp.example.com"}},
    )
    unavailable = []

    assert route_domain.discover_domain(unavailable) == "apps.ocp.example.com"
    assert unavailable == []


def test_plain_kubernetes_reports_no_domain_and_no_unavailable_entry(
    monkeypatch, fake_k8s,
):
    """The invariant that keeps this feature from spoiling the page it sits on.

    `config.openshift.io` is absent on every non-OpenShift cluster, so discovery
    raises Unsupported — and `collect` records every AdminError. Left to it,
    /routes/capabilities would come back partial:true on every read against
    every plain cluster, and §11.1's "some of this could not be read" banner
    would be permanently lit by a group nobody expected the cluster to serve.
    A banner that is always on is one nobody reads.
    """
    _discovery(monkeypatch, groups=[("networking.k8s.io", "v1")])
    unavailable = []

    assert route_domain.discover_domain(unavailable) is None
    assert unavailable == []


def test_a_discovery_failure_is_recorded_rather_than_read_as_absent(
    monkeypatch, fake_k8s,
):
    """"Not OpenShift" and "we could not ask" are different answers.

    The first is an ordinary fact. The second means the console does not know,
    and saying "this cluster publishes no domain" would be an invention.
    """
    _discovery(
        monkeypatch,
        groups=[("config.openshift.io", "v1")],
        failures={"/apis": ClusterUnreachable("the API server did not answer")},
    )
    unavailable = []

    assert route_domain.discover_domain(unavailable) is None
    assert unavailable, "a failed discovery must name itself in unavailable[]"


def test_a_domain_the_cluster_publishes_but_we_cannot_parse_is_not_offered(
    monkeypatch, fake_k8s, caplog,
):
    """A malformed cluster config is not a reason to fail the operator's form."""
    _discovery(
        monkeypatch,
        groups=[("config.openshift.io", "v1")],
        config={"spec": {"domain": "https://apps.example.com/nope"}},
    )

    assert route_domain.discover_domain([]) is None


# --------------------------------------------------------------------------- #
# What the dialog reads
# --------------------------------------------------------------------------- #

def test_the_stored_domain_wins_over_the_discovered_one(
    monkeypatch, fake_k8s, registered_cluster, db_session,
):
    """A console that "corrected" this would overwrite a deliberate choice.

    Exposing under a CNAME of the cluster's wildcard is a normal thing to do,
    and the operator who typed it does not want it replaced on every read by
    what the cluster says about itself.
    """
    registered_cluster.app_domain = "apps.cname.example.com"
    db_session.add(registered_cluster)
    db_session.commit()

    _discovery(
        monkeypatch,
        groups=[("config.openshift.io", "v1")],
        config={"spec": {"domain": "apps.ocp.example.com"}},
    )

    token = set_current_cluster_id(registered_cluster.id)
    try:
        report, unavailable = route_domain.domain_report()
    finally:
        reset_current_cluster_id(token)

    assert report["value"] == "apps.cname.example.com"
    assert report["source"] == "configured"
    # Both are reported: "what you typed differs from what the cluster says" is
    # worth seeing, and it is the only place an operator would notice a typo.
    assert report["discovered"] == "apps.ocp.example.com"
    assert unavailable == []


def test_capabilities_carries_the_domain_and_stays_unpartial_on_plain_kubernetes(
    monkeypatch, fake_k8s, registered_cluster,
):
    """The envelope the route dialog actually reads."""
    _discovery(
        monkeypatch,
        groups=[("networking.k8s.io", "v1")],
    )

    token = set_current_cluster_id(registered_cluster.id)
    try:
        body = routes_service.capabilities()
    finally:
        reset_current_cluster_id(token)

    assert body["appDomain"]["value"] is None
    assert body["appDomain"]["source"] is None
    # The whole point of the Unsupported catch: a plain cluster is not partial.
    assert body["partial"] is False
