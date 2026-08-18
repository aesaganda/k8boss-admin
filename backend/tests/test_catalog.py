"""
Discovery (§4), and the two things it must never do.

**A missing group must not look like a smaller cluster.** Discovery is not one
call: it is ``/api``, then ``/apis``, then one call per group-version. An
aggregated APIService whose backing pod is down answers 503 for its own group and
nothing else. A discoverer that raised on the first failure would report a
cluster that serves nothing; one that skipped the failure quietly would report a
cluster that does not have ``metrics.k8s.io`` — and the operator would go and
install something they already have. The groups that answered are kept, and the
one that did not is named in ``unavailable``.

**"Not in the catalog" must stay distinguishable from "we could not look".**
:func:`~app.resources.catalog.resolve` refuses to answer ``unsupported`` for a
group that is in the ``unavailable`` list; it re-raises the failure that actually
happened, with the §1.3 status and hint that go with it — 403 naming a grant,
502 naming an APIService.

The core group round-trips through :func:`~app.resources.catalog.normalize_group`
in one place, at the edge. Everything below sees ``""``, so no comparison
downstream can silently fail to match the string ``core``.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.errors import ClusterUnreachable, NotFound, RBACDenied, Unsupported
from app.resources import catalog
from app.resources.catalog import discover, normalize_group, resolve, wire_group


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Discovery is cached per cluster for a minute; tests must not share it.

    Without this, the second test in the file sees the first one's stubbed
    cluster — including its failures — and passes or fails for reasons that have
    nothing to do with what it stubbed.
    """
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# A stubbed cluster
# --------------------------------------------------------------------------- #

def _core_v1():
    return {
        "resources": [
            {"name": "pods", "kind": "Pod", "namespaced": True,
             "verbs": ["get", "list", "watch", "create", "delete"],
             "shortNames": ["po"], "categories": ["all"]},
            # Subresources arrive in the same list as their parents and must not
            # reach the catalog: a subresource is not a collection you can list,
            # and a picker offering "pods/log" produces a request that 404s.
            {"name": "pods/log", "kind": "Pod", "namespaced": True, "verbs": ["get"]},
            {"name": "pods/exec", "kind": "Pod", "namespaced": True, "verbs": ["create"]},
            {"name": "namespaces", "kind": "Namespace", "namespaced": False,
             "verbs": ["get", "list"], "shortNames": ["ns"]},
            {"name": "secrets", "kind": "Secret", "namespaced": True,
             "verbs": ["get", "list"]},
        ]
    }


def _apps_v1():
    return {
        "resources": [
            {"name": "deployments", "kind": "Deployment", "namespaced": True,
             "verbs": ["get", "list", "patch"], "shortNames": ["deploy"],
             "categories": ["all"]},
            {"name": "deployments/scale", "kind": "Scale", "namespaced": True,
             "verbs": ["get", "patch"]},
        ]
    }


def _metrics_v1beta1():
    return {
        "resources": [
            {"name": "nodes", "kind": "NodeMetrics", "namespaced": False,
             "verbs": ["get", "list"]},
        ]
    }


def _groups_payload(*names_and_versions):
    groups = []
    for name, version in names_and_versions:
        groups.append({
            "name": name,
            "preferredVersion": {"groupVersion": f"{name}/{version}", "version": version},
            "versions": [{"groupVersion": f"{name}/{version}", "version": version}],
        })
    return {"groups": groups}


def _stub_discovery(monkeypatch, *, failures=None, groups=None):
    """Install a fake ``raw_get`` covering the whole discovery sequence.

    ``failures`` maps a path to the exception it raises, which is how a test
    produces "this one aggregated group is down" without inventing a transport.
    Every path the code asks for and the fake does not know is an ``AssertionError``,
    for the same reason ``FakeApi`` behaves that way: a permissive stub makes a
    swallowed failure indistinguishable from a genuinely small cluster.
    """
    failures = failures or {}
    groups = groups if groups is not None else [
        ("apps", "v1"), ("metrics.k8s.io", "v1beta1"),
    ]
    payloads = {
        "/api": {"versions": ["v1"]},
        "/apis": _groups_payload(*groups),
        "/api/v1": _core_v1(),
        "/apis/apps/v1": _apps_v1(),
        "/apis/metrics.k8s.io/v1beta1": _metrics_v1beta1(),
    }
    calls: list[str] = []

    def fake_raw_get(path, *, query=None):
        calls.append(path)
        if path in failures:
            raise failures[path]
        if path not in payloads:
            raise AssertionError(f"discovery asked for an unstubbed path: {path}")
        return payloads[path]

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    return calls


# --------------------------------------------------------------------------- #
# §1.4 — the core group's two spellings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("wire", "real"),
    [("core", ""), ("", ""), ("apps", "apps"), ("rbac.authorization.k8s.io",
                                                "rbac.authorization.k8s.io")],
)
def test_the_wire_spelling_normalises_to_the_real_group_name(wire, real):
    assert normalize_group(wire) == real


def test_normalising_is_idempotent():
    """Called twice by design: once at the route, again inside the reader.

    A second pass that turned "" back into something else would make the second
    call site the bug.
    """
    assert normalize_group(normalize_group("core")) == ""
    assert normalize_group(normalize_group("apps")) == "apps"


def test_a_missing_group_normalises_to_the_core_group():
    assert normalize_group(None) == ""
    assert normalize_group("  core  ") == ""


@pytest.mark.parametrize(("real", "wire"), [("", "core"), (None, "core"), ("apps", "apps")])
def test_the_real_group_name_round_trips_back_to_a_url_segment(real, wire):
    """The empty string cannot be a path segment, so it has to come back as `core`."""
    assert wire_group(real) == wire
    assert normalize_group(wire_group(real)) == (real or "")


# --------------------------------------------------------------------------- #
# A complete discovery
# --------------------------------------------------------------------------- #

def test_a_complete_discovery_lists_every_group_and_no_subresources(monkeypatch, fake_k8s):
    _stub_discovery(monkeypatch)

    items, unavailable = discover()

    assert unavailable == []
    names = {(item["group"], item["resource"]) for item in items}
    assert ("", "pods") in names
    assert ("apps", "deployments") in names
    assert ("metrics.k8s.io", "nodes") in names
    assert not [item for item in items if "/" in item["resource"]]


def test_the_core_group_is_catalogued_under_the_empty_string(monkeypatch, fake_k8s):
    _stub_discovery(monkeypatch)

    items, _ = discover()
    pods = next(item for item in items if item["resource"] == "pods")

    assert pods["group"] == ""
    assert pods["apiVersion"] == "v1"
    assert pods["namespaced"] is True
    assert pods["shortNames"] == ["po"]


def test_a_second_call_is_served_from_the_cache(monkeypatch, fake_k8s):
    """One round of ~40 requests is shared by the many pages that need it."""
    calls = _stub_discovery(monkeypatch)

    discover()
    first = len(calls)
    discover()

    assert len(calls) == first


def test_the_cache_hands_out_copies_so_a_caller_cannot_corrupt_it(monkeypatch, fake_k8s):
    """A caller that sorts or filters in place must not empty the next page's catalog."""
    _stub_discovery(monkeypatch)

    items, unavailable = discover()
    items.clear()
    unavailable.append({"bogus": True})

    again, again_unavailable = discover()
    assert again
    assert again_unavailable == []


# --------------------------------------------------------------------------- #
# A partial discovery — the canonical case
# --------------------------------------------------------------------------- #

def test_a_broken_group_is_reported_and_the_rest_are_kept(monkeypatch, fake_k8s):
    """The §4 case in one assertion pair.

    The cluster must not appear to have fewer resources because an aggregated
    APIService is down; it must appear to have the same resources, minus an
    honest hole with a reason on it.
    """
    _stub_discovery(monkeypatch, failures={
        "/apis/metrics.k8s.io/v1beta1": ApiException(status=503, reason="Service Unavailable"),
    })

    items, unavailable = discover()

    groups = {item["group"] for item in items}
    assert "" in groups and "apps" in groups
    assert "metrics.k8s.io" not in groups
    assert [entry["group"] for entry in unavailable] == ["metrics.k8s.io"]
    assert unavailable[0]["reason"] == "unreachable"


def test_a_forbidden_group_is_reported_as_forbidden(monkeypatch, fake_k8s):
    """Not `unreachable`: the operator's fix is a ClusterRole, not a network."""
    _stub_discovery(monkeypatch, failures={
        "/apis/apps/v1": ApiException(status=403, reason="Forbidden"),
    })

    _, unavailable = discover()

    assert unavailable[0] == {
        "group": "apps", "resource": "*", "namespace": None,
        "reason": "forbidden", "detail": "Forbidden",
    }


def test_losing_the_group_list_is_recorded_against_every_group(monkeypatch, fake_k8s):
    """`/apis` failing means we do not know *which* groups we cannot see.

    Naming one would be an invention, so the entry is recorded against `*` and
    the detail says so in words.
    """
    _stub_discovery(monkeypatch, failures={
        "/apis": ApiException(status=503, reason="Service Unavailable"),
    })

    items, unavailable = discover()

    assert [entry["group"] for entry in unavailable] == ["*"]
    # The core group came from /api and is unaffected.
    assert {item["group"] for item in items} == {""}


def test_losing_the_core_group_does_not_lose_the_others(monkeypatch, fake_k8s):
    _stub_discovery(monkeypatch, failures={
        "/api": ApiException(status=403, reason="Forbidden"),
    })

    items, unavailable = discover()

    assert [entry["group"] for entry in unavailable] == [""]
    assert "apps" in {item["group"] for item in items}


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #

def test_resolve_finds_a_resource_by_either_spelling_of_the_core_group(
    monkeypatch, fake_k8s
):
    _stub_discovery(monkeypatch)

    assert resolve("core", "v1", "pods") == resolve("", "v1", "pods")
    assert resolve("core", "v1", "pods")["kind"] == "Pod"


def test_resolve_refuses_to_call_a_blind_spot_unsupported(monkeypatch, fake_k8s):
    """The canonical "say which question you failed to answer" case.

    `unsupported` means "this cluster does not serve that API" and the UI renders
    it as an ordinary fact. Reporting it here would tell an operator to install
    metrics-server while metrics-server is running and merely unreachable.
    """
    _stub_discovery(monkeypatch, failures={
        "/apis/metrics.k8s.io/v1beta1": ApiException(status=503, reason="Service Unavailable"),
    })

    with pytest.raises(ClusterUnreachable) as excinfo:
        resolve("metrics.k8s.io", "v1beta1", "nodes")

    assert excinfo.value.code == "cluster_unreachable"
    assert "unknown" in excinfo.value.message.lower()


def test_a_forbidden_discovery_resolves_to_rbac_denied_not_unsupported(
    monkeypatch, fake_k8s
):
    """403 keeps its status and its hint, which name the grant that would fix it."""
    _stub_discovery(monkeypatch, failures={
        "/apis/apps/v1": ApiException(status=403, reason="Forbidden"),
    })

    with pytest.raises(RBACDenied):
        resolve("apps", "v1", "deployments")


def test_a_lost_group_list_blinds_every_non_core_group(monkeypatch, fake_k8s):
    _stub_discovery(monkeypatch, failures={
        "/apis": ApiException(status=503, reason="Service Unavailable"),
    })

    with pytest.raises(ClusterUnreachable):
        resolve("apps", "v1", "deployments")


def test_a_lost_group_list_does_not_blind_the_core_group(monkeypatch, fake_k8s):
    """`/api` and `/apis` are separate reads, so one says nothing about the other.

    A core plural that does not exist is a 404 about a typo, even during an
    aggregation outage. Answering "we cannot tell whether this cluster has pods"
    sends the operator to debug an APIService over a misspelt URL.
    """
    _stub_discovery(monkeypatch, failures={
        "/apis": ApiException(status=503, reason="Service Unavailable"),
    })

    assert resolve("core", "v1", "pods")["resource"] == "pods"
    with pytest.raises(NotFound):
        resolve("core", "v1", "podz")


def test_an_unknown_plural_in_a_served_group_is_not_found(monkeypatch, fake_k8s):
    _stub_discovery(monkeypatch)

    with pytest.raises(NotFound) as excinfo:
        resolve("apps", "v1", "deploymnets")

    # The detail lists what the group does serve, so the typo is visible.
    assert "deployments" in (excinfo.value.detail or "")


def test_an_unserved_group_version_is_unsupported(monkeypatch, fake_k8s):
    """501, and §1.2 says the UI renders it as "not present on this cluster"."""
    _stub_discovery(monkeypatch)

    with pytest.raises(Unsupported) as excinfo:
        resolve("networking.k8s.io", "v1", "ingresses")

    assert excinfo.value.http_status == 501


def test_an_unserved_version_of_a_served_group_names_the_versions_that_are(
    monkeypatch, fake_k8s
):
    _stub_discovery(monkeypatch)

    with pytest.raises(Unsupported) as excinfo:
        resolve("apps", "v2", "deployments")

    assert "v1" in (excinfo.value.hint or "")


def test_resolve_reports_the_real_group_name_in_its_context(monkeypatch, fake_k8s):
    """`core` is translated at the edge; nothing downstream, including an error,
    should carry the wire spelling back out."""
    _stub_discovery(monkeypatch)

    with pytest.raises(NotFound) as excinfo:
        resolve("core", "v1", "podz")

    assert excinfo.value.context["group"] == ""
