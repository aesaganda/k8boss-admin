"""
Create, replace and delete (§4).

The two things this file exists to pin down:

* **Optimistic concurrency is real** (§0.4). A ``PUT`` carrying a stale
  ``resourceVersion`` is a 409 that hands the editor
  ``context.currentResourceVersion`` and a *fresh* diff against live — not a
  blind overwrite of somebody else's change, and not an error the operator has to
  answer by retyping an edit they already made.
* **A dry run and the real write are the same request.** They differ by one query
  parameter. Two code paths — one that builds a preview and one that writes — is
  how a console ends up showing a diff of something it is not about to do.

Everything is driven through a stand-in for ``ApiClient.call_api``, because the
write path deliberately reaches past the typed clients: they discard the response
headers, and §1.5 promises the API server's ``Warning:`` values verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from kubernetes.client.rest import ApiException

from app.admin import apply as apply_service
from app.audit import recorder
from app.errors import Conflict, Invalid, Unsupported
from app.resources import catalog
from tests.conftest import obj

DEPLOYMENT_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: prod
spec:
  replicas: 5
"""

LIVE = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {
        "name": "checkout", "namespace": "prod", "resourceVersion": "884213",
        "managedFields": [{"manager": "kubectl"}],
    },
    "spec": {"replicas": 3},
    "status": {"replicas": 3},
}

DEPLOYMENT_INFO = {
    "group": "apps", "version": "v1", "kind": "Deployment", "resource": "deployments",
    "namespaced": True,
    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    "shortNames": ["deploy"], "categories": ["all"], "apiVersion": "apps/v1",
    "preferred": True,
}


class FakeApiServer:
    """A stand-in for ``ApiClient.call_api``: records requests, returns payloads.

    Honours ``_return_http_data_only`` both ways, because the read path
    (``catalog.raw_get``) sets it True and the write path sets it False precisely
    so it can see the ``Warning:`` headers.
    """

    def __init__(self, live=None, projection=None, warnings=None):
        self.requests: list[SimpleNamespace] = []
        self.live = live
        self.projection = projection
        self.headers = {"Warning": warnings} if warnings else {}
        self.raises: dict[str, BaseException] = {}

    def __call__(self, path, method, **kwargs):
        query = dict(kwargs.get("query_params") or [])
        self.requests.append(SimpleNamespace(
            path=path, method=method, query=query, body=kwargs.get("body"),
            content_type=(kwargs.get("header_params") or {}).get("Content-Type"),
        ))
        if method in self.raises:
            raise self.raises[method]
        if method == "GET":
            payload = self.live
        elif method == "DELETE":
            payload = {"kind": "Status", "status": "Success"}
        else:
            payload = self.projection if self.projection is not None else kwargs.get("body")
        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, self.headers

    def of(self, method):
        return [request for request in self.requests if request.method == method]


@pytest.fixture
def server(monkeypatch, fake_k8s):
    """A fake API server, with discovery and the access review already answered.

    Discovery is stubbed rather than exercised: what this file is about is the
    write, and making every case stub ``/api``, ``/apis`` and a group listing
    first would bury it.
    """
    monkeypatch.setattr(catalog, "resolve", lambda group, version, plural: DEPLOYMENT_INFO)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer(live=LIVE)
    fake_k8s.api_client.returns("call_api", api_server)
    return api_server


def audit_rows():
    return recorder.query(limit=50)["items"]


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #

def test_a_create_dry_run_sends_dryrun_all_and_reports_applied_false(db_engine, server):
    response = apply_service.create_from_yaml(
        "apps", "v1", "deployments", "prod", DEPLOYMENT_YAML, True,
    )

    (request,) = server.of("POST")
    assert request.path == "/apis/apps/v1/namespaces/prod/deployments"
    assert request.query == {"dryRun": "All"}
    assert response["applied"] is False
    assert response["diff"]["before"] == "", "there is nothing live to diff a create against"
    assert "+kind: Deployment" in response["diff"]["unified"]
    assert audit_rows()[0]["outcome"] == "dry_run"


def test_a_real_create_omits_the_dryrun_parameter(db_engine, server, allow_mutations):
    response = apply_service.create_from_yaml(
        "apps", "v1", "deployments", "prod", DEPLOYMENT_YAML, False,
    )

    (request,) = server.of("POST")
    assert request.query == {}
    assert response["applied"] is True
    assert audit_rows()[0]["outcome"] == "applied"


def test_the_namespace_in_the_document_wins_over_the_one_on_the_request(db_engine, server):
    """The document is what the operator wrote; the request's namespace is often
    just whichever namespace the browser was filtered to."""
    apply_service.create_from_yaml(
        "apps", "v1", "deployments", "staging", DEPLOYMENT_YAML, True,
    )

    (request,) = server.of("POST")
    assert request.path == "/apis/apps/v1/namespaces/prod/deployments"
    assert request.body["metadata"]["namespace"] == "prod"


def test_a_namespaced_resource_with_no_namespace_anywhere_is_refused(db_engine, server):
    document = yaml.safe_load(DEPLOYMENT_YAML)
    document["metadata"].pop("namespace")

    with pytest.raises(Invalid) as caught:
        apply_service.create_from_yaml(
            "apps", "v1", "deployments", None, yaml.safe_dump(document), True,
        )

    assert "namespace" in caught.value.message
    assert server.of("POST") == []


def test_a_namespace_on_a_cluster_scoped_object_is_refused_not_ignored(db_engine, server,
                                                                      monkeypatch):
    """The API server drops it silently, and the operator goes on believing they
    created something namespaced."""
    monkeypatch.setattr(catalog, "resolve", lambda *a: {
        **DEPLOYMENT_INFO, "kind": "ClusterRole", "resource": "clusterroles",
        "namespaced": False,
    })
    document = (
        "apiVersion: apps/v1\nkind: ClusterRole\nmetadata:\n  name: viewer\n"
        "  namespace: prod\nrules: []\n"
    )

    with pytest.raises(Invalid) as caught:
        apply_service.create_from_yaml(
            "apps", "v1", "clusterroles", None, document, True,
        )

    assert "cluster-scoped" in caught.value.message


def test_a_document_that_does_not_match_the_url_is_refused_naming_both(db_engine, server):
    """Forwarded, the API server answers with a schema error about the resource
    *it* was asked for — which reads as "your Deployment is malformed" when a
    Service was pasted into a Deployment's editor."""
    document = "apiVersion: v1\nkind: Service\nmetadata:\n  name: checkout\n  namespace: prod\n"

    with pytest.raises(Invalid) as caught:
        apply_service.create_from_yaml("apps", "v1", "deployments", "prod", document, True)

    assert "apps/v1" in caught.value.message and "v1" in caught.value.message
    assert server.of("POST") == []


def test_a_multi_document_stream_is_refused(db_engine, server):
    """Applying the first and reporting success would leave the rest unwritten
    with nothing in the response to say so."""
    stream = DEPLOYMENT_YAML + "\n---\n" + DEPLOYMENT_YAML

    with pytest.raises(Invalid) as caught:
        apply_service.create_from_yaml("apps", "v1", "deployments", "prod", stream, True)

    assert caught.value.context["documents"] == 2


def test_unparseable_yaml_reports_the_parser_s_own_message(db_engine, server):
    with pytest.raises(Invalid) as caught:
        apply_service.create_from_yaml(
            "apps", "v1", "deployments", "prod", "spec:\n\tbad: tab\n", True,
        )

    assert caught.value.detail is not None and "tab" in caught.value.detail.lower()


def test_a_verb_the_resource_does_not_advertise_is_refused_with_the_list(db_engine, server,
                                                                        monkeypatch):
    monkeypatch.setattr(catalog, "resolve", lambda *a: {
        **DEPLOYMENT_INFO, "verbs": ["get", "list"],
    })

    with pytest.raises(Unsupported) as caught:
        apply_service.create_from_yaml("apps", "v1", "deployments", "prod", DEPLOYMENT_YAML, True)

    assert "get, list" in caught.value.detail


# --------------------------------------------------------------------------- #
# Replace, and the concurrency rule
# --------------------------------------------------------------------------- #

def test_a_replace_carries_the_callers_resource_version_to_the_api_server(db_engine, server):
    """Checked here *and* by the API server: the window between our read and our
    write is small, and a check that only closes it locally loses the race it
    exists to detect."""
    apply_service.update_from_yaml(
        "apps", "v1", "deployments", "prod", "checkout", DEPLOYMENT_YAML, "884213", True,
    )

    (request,) = server.of("PUT")
    assert request.path == "/apis/apps/v1/namespaces/prod/deployments/checkout"
    assert request.query == {"dryRun": "All"}
    assert request.body["metadata"]["resourceVersion"] == "884213"


def test_the_replace_diff_is_live_against_the_projection(db_engine, server):
    server.projection = {**LIVE, "spec": {"replicas": 5}}

    response = apply_service.update_from_yaml(
        "apps", "v1", "deployments", "prod", "checkout", DEPLOYMENT_YAML, "884213", True,
    )

    assert response["diff"]["changed"] is True
    assert "-  replicas: 3" in response["diff"]["unified"]
    assert "+  replicas: 5" in response["diff"]["unified"]
    assert "managedFields" not in response["diff"]["before"]


def test_a_stale_resource_version_is_a_409_with_a_fresh_diff(db_engine, server):
    with pytest.raises(Conflict) as caught:
        apply_service.update_from_yaml(
            "apps", "v1", "deployments", "prod", "checkout", DEPLOYMENT_YAML, "884000", True,
        )

    error = caught.value
    assert error.http_status == 409
    assert error.context["currentResourceVersion"] == "884213"
    assert error.context["submittedResourceVersion"] == "884000"

    fresh = error.context["diff"]
    assert fresh["changed"] is True, "the fresh diff is the submitted document against live"
    assert "-  replicas: 3" in fresh["unified"]
    assert "+  replicas: 5" in fresh["unified"]
    assert "status" not in fresh["before"], (
        "the submitted document has no status, so showing live's would claim the "
        "edit deletes it"
    )


def test_a_stale_replace_never_reaches_the_cluster_and_is_audited(db_engine, server):
    with pytest.raises(Conflict):
        apply_service.update_from_yaml(
            "apps", "v1", "deployments", "prod", "checkout", DEPLOYMENT_YAML, "884000", True,
        )

    assert server.of("PUT") == [], "a blind overwrite is what rule 4 exists to forbid"
    (row,) = audit_rows()
    assert row["outcome"] == "conflict"
    assert row["target"]["name"] == "checkout"


def test_a_replace_cannot_rename_the_object(db_engine, server):
    document = DEPLOYMENT_YAML.replace("name: checkout", "name: checkout-v2")

    with pytest.raises(Invalid) as caught:
        apply_service.update_from_yaml(
            "apps", "v1", "deployments", "prod", "checkout", document, "884213", True,
        )

    assert "checkout-v2" in caught.value.message


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #

def test_a_delete_diff_is_the_live_object_against_nothing(db_engine, server):
    """§4: the confirm dialog has one job, which is to show what disappears."""
    response = apply_service.delete_resource(
        "apps", "v1", "deployments", "prod", "checkout", "Background", True,
    )

    assert response["diff"]["after"] == ""
    assert response["diff"]["changed"] is True
    assert "-kind: Deployment" in response["diff"]["unified"]
    assert "-  replicas: 3" in response["diff"]["unified"]
    assert response["applied"] is False


def test_the_propagation_policy_reaches_the_api_server_unchanged(db_engine, server,
                                                                 allow_mutations):
    apply_service.delete_resource(
        "apps", "v1", "deployments", "prod", "checkout", "Orphan", False,
    )

    (request,) = server.of("DELETE")
    assert request.query == {"propagationPolicy": "Orphan"}


def test_an_unknown_propagation_policy_is_refused(db_engine, server):
    with pytest.raises(Invalid) as caught:
        apply_service.delete_resource(
            "apps", "v1", "deployments", "prod", "checkout", "Cascade", True,
        )

    assert "Background" in caught.value.hint


def test_a_delete_of_a_missing_object_is_a_404_not_an_empty_diff(db_engine, server):
    from app.errors import NotFound

    server.raises["GET"] = ApiException(status=404, reason="Not Found")

    with pytest.raises(NotFound):
        apply_service.delete_resource(
            "apps", "v1", "deployments", "prod", "ghost", "Background", True,
        )

    assert server.of("DELETE") == []


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #

def test_warning_headers_are_relayed_verbatim(db_engine, server):
    server.headers = {
        "Warning": '299 - "apps/v1beta1 Deployment is deprecated", '
                   '299 - "spec.template.spec.containers[0].resources, and 2 other fields"'
    }

    response = apply_service.create_from_yaml(
        "apps", "v1", "deployments", "prod", DEPLOYMENT_YAML, True,
    )

    assert response["warnings"] == [
        "apps/v1beta1 Deployment is deprecated",
        "spec.template.spec.containers[0].resources, and 2 other fields",
    ], "a comma inside a warning must not split it into two false ones"


def test_no_warnings_is_an_empty_list_not_a_null(db_engine, server):
    """Here an empty list is honest: the headers arrive in the same response as
    the object, so if we have one we have the other."""
    response = apply_service.create_from_yaml(
        "apps", "v1", "deployments", "prod", DEPLOYMENT_YAML, True,
    )

    assert response["warnings"] == []


def test_an_unparseable_warning_is_kept_rather_than_dropped(db_engine, server):
    server.headers = {"Warning": "something a proxy invented"}

    response = apply_service.create_from_yaml(
        "apps", "v1", "deployments", "prod", DEPLOYMENT_YAML, True,
    )

    assert response["warnings"] == ["something a proxy invented"]


# --------------------------------------------------------------------------- #
# Secrets. The write path is the other channel a value could leave by.
# --------------------------------------------------------------------------- #

import base64  # noqa: E402

SECRET_PASSWORD_B64 = base64.b64encode(b"hunter2-rotated-2026").decode()
SECRET_ROTATED_B64 = base64.b64encode(b"hunter3-rotated-2026").decode()

SECRET_LIVE = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": "db", "namespace": "prod", "resourceVersion": "10",
        "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration":
                '{"data":{"password":"' + SECRET_PASSWORD_B64 + '"}}',
        },
    },
    "type": "Opaque",
    "data": {"password": SECRET_PASSWORD_B64},
}

SECRET_INFO = {
    "group": "", "version": "v1", "kind": "Secret", "resource": "secrets",
    "namespaced": True,
    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    "shortNames": [], "categories": [], "apiVersion": "v1", "preferred": True,
}

SECRET_YAML = f"""
apiVersion: v1
kind: Secret
metadata:
  name: db
  namespace: prod
type: Opaque
data:
  password: {SECRET_ROTATED_B64}
"""


@pytest.fixture
def secret_server(monkeypatch, fake_k8s):
    """The `server` fixture, for a Secret: discovery answers Secrets, live is one."""
    monkeypatch.setattr(catalog, "resolve", lambda group, version, plural: SECRET_INFO)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer(live=SECRET_LIVE)
    fake_k8s.api_client.returns("call_api", api_server)
    return api_server


def _all_text(diff):
    return "\n".join(str(diff[key]) for key in ("before", "after", "unified"))


def test_a_dry_run_delete_of_a_secret_carries_no_value(db_engine, secret_server):
    """The leak: permitted in read-only mode, preflighted on `delete`, audited as
    an ordinary dry run — and, before this, the whole Secret in `diff.before`."""
    response = apply_service.delete_resource("", "v1", "secrets", "prod", "db", "Background", True)

    assert SECRET_PASSWORD_B64 not in _all_text(response["diff"])
    assert "hunter2" not in _all_text(response["diff"])
    assert "password: <redacted, 20 bytes>" in response["diff"]["before"]
    assert "last-applied-configuration" not in _all_text(response["diff"])
    assert response["diff"]["changed"] is True
    assert response["applied"] is False


def test_a_secret_replace_diff_names_the_changed_key_without_its_value(db_engine, secret_server):
    secret_server.projection = {
        **SECRET_LIVE, "metadata": {**SECRET_LIVE["metadata"], "resourceVersion": "11"},
        "data": {"password": SECRET_ROTATED_B64},
    }

    response = apply_service.update_from_yaml("", "v1", "secrets", "prod", "db", SECRET_YAML, "10", True)

    text = _all_text(response["diff"])
    assert SECRET_PASSWORD_B64 not in text and SECRET_ROTATED_B64 not in text
    assert response["diff"]["changed"] is True
    assert "+  password: <redacted, 20 bytes, changed>" in response["diff"]["unified"]


def test_the_fresh_diff_on_a_secret_conflict_carries_no_value(db_engine, secret_server):
    """A 409 diffs the submitted document against live, outside the funnel's own
    diff — the same rule has to hold there, and it does because it is keyed on
    the object inside build_diff rather than on the caller."""
    with pytest.raises(Conflict) as caught:
        apply_service.update_from_yaml("", "v1", "secrets", "prod", "db", SECRET_YAML, "9", True)

    fresh = caught.value.context["diff"]
    assert SECRET_PASSWORD_B64 not in _all_text(fresh)
    assert SECRET_ROTATED_B64 not in _all_text(fresh)
    assert fresh["changed"] is True


def test_the_delete_route_returns_a_redacted_diff(client, db_engine, secret_server):
    """Through the HTTP route, in read-only mode: the response body a browser
    would hold in its network tab has no value in it anywhere."""
    response = client.delete("/api/resources/core/v1/secrets/db", params={"namespace": "prod", "dryRun": "true"})

    assert response.status_code == 200, response.text
    assert SECRET_PASSWORD_B64 not in response.text
    assert "hunter2" not in response.text
    assert "<redacted, 20 bytes>" in response.text
