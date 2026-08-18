"""
The generic read path (§4): what comes back, and what never does.

Trimming is the subject of most of this file, and it is not cosmetic.

* ``metadata.managedFields`` is server-side-apply bookkeeping that is routinely
  larger than the object it annotates and that nothing in this console renders.
  It is stripped from **every** response, list and single alike — so the
  assertion is written for both, because a rule that holds on one path and not
  the other is the rule that gets broken by the next endpoint.
* ``kubectl.kubernetes.io/last-applied-configuration`` is a whole serialised copy
  of the object stored inside the object. It is stripped from **list** responses,
  where it doubles the payload for nothing, and kept on the **single-object**
  read, where an operator editing YAML has a real reason to see what kubectl last
  applied.

There is exactly one object type where that second rule is dangerous, and the
last section of this file is about it: a Secret applied with ``kubectl apply``
carries every one of its values inside that annotation, so keeping it on a
single-object read would hand back the whole Secret with the reveal gate
untouched. ``redact_secret`` strips it, and this file asserts that the route
does too — through the JSON read and through the YAML read, because they are the
same bytes with a different Content-Type.

The rest is round-tripping: what the editor loads has to be what the API server
sent, in the order it sent it, wide enough to copy and paste.
"""

from __future__ import annotations

import base64
import json

import pytest
import yaml
from kubernetes.client.rest import ApiException

from app.errors import Invalid, RBACDenied, Unsupported
from app.resources import catalog, reader
from app.resources.reader import (
    LAST_APPLIED_ANNOTATION,
    get_resource,
    list_resource,
    resource_path,
    to_yaml,
    trim,
)

SECRET_VALUE = "hunter2-do-not-leak"
SECRET_B64 = base64.b64encode(SECRET_VALUE.encode()).decode()


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _deployment(annotations=None, managed=True):
    metadata = {
        "name": "checkout",
        "namespace": "prod",
        "uid": "0f2a",
        "resourceVersion": "884213",
        "creationTimestamp": "2026-05-01T08:00:00Z",
        "labels": {"app": "checkout"},
    }
    if annotations is not None:
        metadata["annotations"] = annotations
    if managed:
        metadata["managedFields"] = [
            {"manager": "kube-controller-manager", "operation": "Update",
             "fieldsV1": {"f:spec": {"f:replicas": {}}}},
        ]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {"replicas": 3},
        "status": {"readyReplicas": 3},
    }


def _info(resource="deployments", namespaced=True, verbs=("get", "list")):
    return {
        "group": "apps", "version": "v1", "kind": "Deployment", "resource": resource,
        "namespaced": namespaced, "verbs": list(verbs), "shortNames": [],
        "categories": [], "apiVersion": "apps/v1", "preferred": True,
    }


def _stub_read(monkeypatch, *, info=None, payload=None, error=None):
    """Stub `resolve` and `raw_get` so the read path can be exercised alone."""
    monkeypatch.setattr(catalog, "resolve", lambda g, v, p: info or _info())
    seen: list[tuple[str, list]] = []

    def fake_raw_get(path, *, query=None):
        seen.append((path, list(query or [])))
        if error is not None:
            raise error
        return payload

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    return seen


# --------------------------------------------------------------------------- #
# resource_path
# --------------------------------------------------------------------------- #

def test_the_core_group_lives_under_api_and_everything_else_under_apis():
    """The one structural asymmetry in the Kubernetes REST surface.

    It is also the whole reason §1.4 needs a wire spelling for a group whose real
    name is the empty string.
    """
    assert resource_path("", "v1", "pods") == "/api/v1/pods"
    assert resource_path("apps", "v1", "deployments") == "/apis/apps/v1/deployments"


def test_namespace_name_and_subresource_are_appended_in_order():
    assert resource_path(
        "apps", "v1", "deployments", namespace="prod", name="checkout",
        subresource="scale",
    ) == "/apis/apps/v1/namespaces/prod/deployments/checkout/scale"


def test_path_segments_are_percent_encoded():
    """A resource name read back from a CRD is caller-influenced input in a URL."""
    assert "%2F" in resource_path("apps", "v1", "deployments", name="a/b")


# --------------------------------------------------------------------------- #
# trim
# --------------------------------------------------------------------------- #

def test_managed_fields_are_stripped_from_a_list_row():
    trimmed = trim(_deployment(), for_list=True)

    assert "managedFields" not in trimmed["metadata"]


def test_managed_fields_are_stripped_from_a_single_object_too():
    """The rule has no exception, so neither does the assertion.

    A rule enforced on one path and not the other is the one the next endpoint
    breaks — and `managedFields` is two thirds of the payload on a Deployment
    reconciled by two controllers.
    """
    trimmed = trim(_deployment(), for_list=False)

    assert "managedFields" not in trimmed["metadata"]


def test_last_applied_is_stripped_from_a_list_row_only():
    obj = _deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}", "team": "payments"})

    listed = trim(obj, for_list=True)
    single = trim(obj, for_list=False)

    assert LAST_APPLIED_ANNOTATION not in listed["metadata"]["annotations"]
    assert listed["metadata"]["annotations"]["team"] == "payments"
    assert LAST_APPLIED_ANNOTATION in single["metadata"]["annotations"]


def test_the_annotations_key_survives_being_emptied():
    """An object whose only annotation was kubectl's copy still *has* annotations.

    Dropping the key would make the list view say a resource has none while its
    detail view — which keeps the annotation — shows one.
    """
    obj = _deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}"})

    listed = trim(obj, for_list=True)

    assert listed["metadata"]["annotations"] == {}


def test_trim_does_not_mutate_its_input():
    """A caller that trims for a list row still needs the full object for a diff.

    In-place mutation has exactly one failure mode and it is a bad one: a
    single-object read sharing a dict with a list row silently starts returning
    list-trimmed content.
    """
    obj = _deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}"})

    trim(obj, for_list=True)

    assert "managedFields" in obj["metadata"]
    assert LAST_APPLIED_ANNOTATION in obj["metadata"]["annotations"]


def test_trimming_the_same_object_twice_gives_two_independent_copies():
    obj = _deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}"})

    listed = trim(obj, for_list=True)
    single = trim(obj, for_list=False)

    assert listed["metadata"] is not single["metadata"]
    assert LAST_APPLIED_ANNOTATION in single["metadata"]["annotations"]


def test_trim_passes_through_anything_that_is_not_an_object():
    assert trim(None, for_list=True) is None
    assert trim("not-an-object", for_list=False) == "not-an-object"


def test_trim_survives_an_object_with_no_metadata():
    assert trim({"kind": "Status"}, for_list=True) == {"kind": "Status"}


# --------------------------------------------------------------------------- #
# to_yaml
# --------------------------------------------------------------------------- #

def test_yaml_round_trips_the_object_unchanged():
    obj = trim(_deployment(annotations={"team": "payments"}), for_list=False)

    assert yaml.safe_load(to_yaml(obj)) == obj


def test_yaml_keeps_the_api_server_s_field_order():
    """Alphabetical order puts `status` above `spec` and buries `kind`.

    The editor and the §1.5 diff both read far worse for it, and a diff that is
    hard to read is a diff that gets confirmed without being read.
    """
    text = to_yaml(trim(_deployment(), for_list=False))

    keys = [line.split(":")[0] for line in text.splitlines() if line and not line[0].isspace()]
    assert keys == ["apiVersion", "kind", "metadata", "spec", "status"]


def test_a_long_image_reference_is_not_wrapped():
    """Wrapped YAML still parses, but it breaks copy-paste into a shell."""
    image = "ghcr.io/acme/" + "a" * 200 + ":1.9.2"
    text = to_yaml({"image": image})

    assert image in text


def test_none_renders_as_the_empty_string_not_as_null():
    """`diff.after` is None for a delete dry-run (§4).

    A unified diff against the literal text `null` shows one line being *added*
    where the object is in fact being removed.
    """
    assert to_yaml(None) == ""


def test_unicode_is_not_escaped():
    assert "München" in to_yaml({"name": "München"})


# --------------------------------------------------------------------------- #
# list_resource
# --------------------------------------------------------------------------- #

def test_a_listing_trims_every_row(monkeypatch):
    _stub_read(monkeypatch, payload={
        "metadata": {"continue": ""},
        "items": [
            _deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}"}),
            _deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}"}),
        ],
    })

    result = list_resource("apps", "v1", "deployments")

    for item in result["items"]:
        assert "managedFields" not in item["metadata"]
        assert LAST_APPLIED_ANNOTATION not in item["metadata"]["annotations"]


def test_a_complete_listing_reports_a_null_continue(monkeypatch):
    _stub_read(monkeypatch, payload={"metadata": {"continue": ""}, "items": []})

    result = list_resource("apps", "v1", "deployments")

    assert result["continue"] is None
    assert result["remaining"] is None
    assert result["partial"] is False
    assert result["unavailable"] == []


def test_a_chunked_listing_passes_the_cursor_and_count_through(monkeypatch):
    _stub_read(monkeypatch, payload={
        "metadata": {"continue": "eyJ2Ijoi", "remainingItemCount": 412},
        "items": [_deployment()],
    })

    result = list_resource("apps", "v1", "deployments")

    assert result["continue"] == "eyJ2Ijoi"
    assert result["remaining"] == 412


def test_the_query_is_passed_to_the_api_server_verbatim(monkeypatch):
    seen = _stub_read(monkeypatch, payload={"metadata": {}, "items": []})

    list_resource(
        "apps", "v1", "deployments", namespace="prod",
        label_selector="app=checkout", field_selector="status.phase=Running",
        limit=100, cont="tok",
    )

    path, query = seen[0]
    assert path == "/apis/apps/v1/namespaces/prod/deployments"
    assert dict(query) == {
        "labelSelector": "app=checkout",
        "fieldSelector": "status.phase=Running",
        "limit": 100,
        "continue": "tok",
    }


def test_omitting_the_namespace_lists_across_all_of_them(monkeypatch):
    seen = _stub_read(monkeypatch, payload={"metadata": {}, "items": []})

    list_resource("apps", "v1", "deployments")

    assert seen[0][0] == "/apis/apps/v1/deployments"


def test_a_namespace_on_a_cluster_scoped_resource_is_refused(monkeypatch):
    """Ignoring it is the tempting alternative and is a confidently wrong answer.

    The UI would show every node in the cluster under a heading that says
    "namespace: prod", and nothing in the response would contradict it.
    """
    _stub_read(monkeypatch, info=_info("nodes", namespaced=False), payload={"items": []})

    with pytest.raises(Invalid):
        list_resource("", "v1", "nodes", namespace="prod")


def test_a_verb_the_resource_does_not_advertise_is_refused_by_name(monkeypatch):
    """"selfsubjectaccessreviews cannot be listed" is actionable; 405 is not."""
    _stub_read(
        monkeypatch,
        info=_info("selfsubjectaccessreviews", namespaced=False, verbs=("create",)),
        payload={"items": []},
    )

    with pytest.raises(Unsupported) as excinfo:
        list_resource("authorization.k8s.io", "v1", "selfsubjectaccessreviews")

    assert "list" in excinfo.value.message


def test_a_resource_that_advertises_no_verbs_is_not_second_guessed(monkeypatch):
    """Some aggregated APIs report an empty verb list and serve the verb anyway.

    Refusing on the strength of a resource's own incomplete self-description
    breaks a resource that works.
    """
    _stub_read(monkeypatch, info=_info(verbs=()), payload={"metadata": {}, "items": []})

    assert list_resource("apps", "v1", "deployments")["items"] == []


def test_a_refused_listing_raises_rather_than_returning_an_empty_page(monkeypatch):
    """This endpoint makes one read: it either answered or it failed.

    A 200 with an empty table and a footnote is strictly less useful than the
    §1.3 envelope, which carries the hint naming the grant that would fix it.
    """
    _stub_read(monkeypatch, error=ApiException(status=403, reason="Forbidden"))

    with pytest.raises(RBACDenied) as excinfo:
        list_resource("apps", "v1", "deployments", namespace="prod")

    assert excinfo.value.context["resource"] == "deployments"
    assert excinfo.value.context["namespace"] == "prod"
    assert excinfo.value.hint


# --------------------------------------------------------------------------- #
# get_resource
# --------------------------------------------------------------------------- #

def test_a_single_object_keeps_last_applied_and_loses_managed_fields(monkeypatch):
    _stub_read(
        monkeypatch,
        payload=_deployment(annotations={LAST_APPLIED_ANNOTATION: "{...}"}),
    )

    obj = get_resource("apps", "v1", "deployments", "checkout", namespace="prod")

    assert "managedFields" not in obj["metadata"]
    assert LAST_APPLIED_ANNOTATION in obj["metadata"]["annotations"]


def test_reading_a_namespaced_object_without_a_namespace_is_refused(monkeypatch):
    _stub_read(monkeypatch, payload=_deployment())

    with pytest.raises(Invalid) as excinfo:
        get_resource("apps", "v1", "deployments", "checkout")

    assert "namespace" in excinfo.value.message


def test_a_cluster_scoped_object_needs_no_namespace(monkeypatch):
    seen = _stub_read(
        monkeypatch, info=_info("nodes", namespaced=False), payload=_deployment(),
    )

    get_resource("", "v1", "nodes", "ip-10-0-1-4")

    assert seen[0][0] == "/api/v1/nodes/ip-10-0-1-4"


def test_a_missing_object_raises_not_found_with_its_target(monkeypatch):
    _stub_read(monkeypatch, error=ApiException(status=404, reason="Not Found"))

    with pytest.raises(Exception) as excinfo:
        get_resource("apps", "v1", "deployments", "ghost", namespace="prod")

    assert excinfo.value.code == "not_found"
    assert excinfo.value.context["name"] == "ghost"


# --------------------------------------------------------------------------- #
# The Secret that hides inside its own annotations
# --------------------------------------------------------------------------- #
#
# Regression tests for a defect found auditing the landed read path: §4 keeps
# `last-applied-configuration` on single-object reads, and on a Secret that
# annotation is a verbatim copy of `data`. The reveal gate nulled `data` and
# handed the values straight back one key away.

def _secret_object():
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "db", "namespace": "prod", "resourceVersion": "12",
            "annotations": {
                LAST_APPLIED_ANNOTATION: json.dumps({
                    "apiVersion": "v1", "kind": "Secret",
                    "metadata": {"name": "db", "namespace": "prod"},
                    "data": {"password": SECRET_B64},
                }),
                "team": "payments",
            },
            "managedFields": [{"manager": "kubectl"}],
        },
        "type": "Opaque",
        "data": {"password": SECRET_B64},
    }


def _stub_secret_cluster(monkeypatch):
    """Enough discovery and object payload to drive the real routes."""
    payloads = {
        "/api": {"versions": ["v1"]},
        "/apis": {"groups": []},
        "/api/v1": {"resources": [
            {"name": "secrets", "kind": "Secret", "namespaced": True,
             "verbs": ["get", "list"]},
        ]},
        "/api/v1/namespaces/prod/secrets/db": _secret_object(),
    }

    def fake_raw_get(path, *, query=None):
        if path not in payloads:
            raise AssertionError(f"unstubbed path: {path}")
        return payloads[path]

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)


def test_a_secret_read_does_not_return_its_values_inside_its_annotations(
    client, fake_k8s, monkeypatch
):
    """The reveal gate has to gate every copy of the value, not just `data`.

    `kubectl apply -f secret.yaml` stores the whole object — `data` included —
    in `last-applied-configuration`. Nulling `data` while returning that copy is
    a gate that gates nothing, and the request that walked through it would look,
    in the audit trail, like a read that was never granted.
    """
    _stub_secret_cluster(monkeypatch)

    response = client.get("/api/resources/core/v1/secrets/db?namespace=prod")
    body = response.text

    assert response.status_code == 200
    assert SECRET_VALUE not in body
    assert SECRET_B64 not in body
    assert response.json()["data"] == {"password": None}
    # The other annotations, and the fact that there *is* a value, both survive.
    assert response.json()["metadata"]["annotations"] == {"team": "payments"}


def test_the_yaml_read_of_a_secret_is_gated_the_same_way(client, fake_k8s, monkeypatch):
    """Same bytes, different Content-Type. A gate on one route is not a gate."""
    _stub_secret_cluster(monkeypatch)

    response = client.get("/api/resources/core/v1/secrets/db/yaml?namespace=prod")

    assert response.status_code == 200
    assert SECRET_VALUE not in response.text
    assert SECRET_B64 not in response.text
    assert "managedFields" not in response.text


def test_a_secret_listing_carries_neither_values_nor_managed_fields(
    client, fake_k8s, monkeypatch
):
    payloads = {
        "/api": {"versions": ["v1"]},
        "/apis": {"groups": []},
        "/api/v1": {"resources": [
            {"name": "secrets", "kind": "Secret", "namespaced": True,
             "verbs": ["get", "list"]},
        ]},
        "/api/v1/namespaces/prod/secrets": {
            "metadata": {"continue": ""}, "items": [_secret_object()],
        },
    }
    monkeypatch.setattr(
        catalog, "raw_get",
        lambda path, *, query=None: payloads[path],
    )

    for url in (
        "/api/resources/core/v1/secrets?namespace=prod",
        "/api/resources/core/v1/secrets?namespace=prod&shape=raw",
    ):
        response = client.get(url)

        assert response.status_code == 200, url
        assert SECRET_VALUE not in response.text, url
        assert SECRET_B64 not in response.text, url
        assert "managedFields" not in response.text, url
