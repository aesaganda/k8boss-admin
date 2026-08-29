"""
Revision history and rollback for the kinds whose revisions are not ReplicaSets.

``test_mutate.py`` covers the funnel and the Deployment path through it. What is
left — and what this file is about — is the second, entirely different place
Kubernetes keeps history: a StatefulSet's and a DaemonSet's revisions are
``ControllerRevision`` objects carrying a stored patch, and "current" is named by
a *name* in status rather than by a number in an annotation. Reading the wrong
one of those produces a history panel that is plausible and wrong, and a rollback
targeted from it restores a revision the operator did not pick.

The other half is the refusals. Every one of them exists because the alternative
is a rollback that reports success over a workload that is now neither revision:
a ControllerRevision with an empty ``data``, a ReplicaSet with no
``spec.template``, a revision number the API server sent as something other than
a number.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException as K8sApiException

from app.admin.rollout import (
    CHANGE_CAUSE_ANNOTATION,
    REVISION_ANNOTATION,
    rollback_workload,
    rollout_history,
)
from app.errors import Invalid, NotFound, UpstreamError
from tests.conftest import obj

CONTROLLER_REVISIONS_PATH = "/apis/apps/v1/namespaces/prod/controllerrevisions"
STATEFULSET_PATH = "/apis/apps/v1/namespaces/prod/statefulsets/postgres"
DAEMONSET_PATH = "/apis/apps/v1/namespaces/prod/daemonsets/node-exporter"


class FakeCluster:
    """A path-addressed stand-in for ``ApiClient.call_api``.

    Path-addressed because rollout makes two different reads — the workload and
    its revisions — and a fake that answered both with one payload would let the
    module read the wrong URL and still pass.
    """

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.projections: dict[str, dict] = {}
        self.requests: list[SimpleNamespace] = []

    def __call__(self, path, method, **kwargs):
        self.requests.append(SimpleNamespace(
            path=path, method=method,
            query=dict(kwargs.get("query_params") or []),
            body=kwargs.get("body"),
            content_type=(kwargs.get("header_params") or {}).get("Content-Type"),
        ))
        if method == "GET":
            if path not in self.objects:
                raise AssertionError(f"unstubbed GET {path}")
            payload = self.objects[path]
        else:
            payload = self.projections.get(path, self.objects.get(path))
        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, {}

    def of(self, method):
        return [request for request in self.requests if request.method == method]


@pytest.fixture
def cluster(fake_k8s):
    api_server = FakeCluster()
    fake_k8s.api_client.returns("call_api", api_server)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(
        status=obj(allowed=True, reason=None, evaluation_error=None, denied=False),
    ))
    return api_server


def _statefulset(*, update_revision="postgres-7", current_revision="postgres-6"):
    return {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {
            "name": "postgres", "namespace": "prod", "uid": "sts-uid",
            "resourceVersion": "5512",
        },
        "spec": {
            "selector": {"matchLabels": {"app": "postgres"}},
            "replicas": 3,
            "template": {
                "metadata": {"labels": {"app": "postgres"}},
                "spec": {"containers": [{"name": "db", "image": "postgres:16.2"}]},
            },
        },
        "status": {
            "replicas": 3,
            "updateRevision": update_revision,
            "currentRevision": current_revision,
        },
    }


def _daemonset():
    return {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {
            "name": "node-exporter", "namespace": "prod", "uid": "ds-uid",
            "resourceVersion": "9001",
        },
        "spec": {
            "selector": {"matchLabels": {"app": "node-exporter"}},
            "template": {
                "metadata": {"labels": {"app": "node-exporter"}},
                "spec": {"containers": [{"name": "exporter", "image": "exporter:1.7.0"}]},
            },
        },
        # No revision pointer of any kind: the DaemonSet controller publishes
        # none, which is why the highest ControllerRevision is the current one.
        "status": {"numberReady": 4},
    }


def _controller_revision(
    revision, image, *, name=None, uid="sts-uid", data=None, change_cause=None,
    init_image=None,
):
    template = {
        "metadata": {"labels": {"app": "postgres"}},
        "spec": {"containers": [{"name": "db", "image": image}]},
    }
    if init_image:
        template["spec"]["initContainers"] = [{"name": "migrate", "image": init_image}]
    annotations = {CHANGE_CAUSE_ANNOTATION: change_cause} if change_cause else {}
    return {
        "apiVersion": "apps/v1", "kind": "ControllerRevision",
        "metadata": {
            "name": name or f"postgres-{revision}", "namespace": "prod",
            "creationTimestamp": "2026-05-01T08:00:00Z",
            "annotations": annotations,
            "ownerReferences": [{"kind": "StatefulSet", "name": "postgres", "uid": uid}],
        },
        "revision": revision,
        "data": {"spec": {"template": template}} if data is None else data,
    }


# --------------------------------------------------------------------------- #
# History: ControllerRevisions
# --------------------------------------------------------------------------- #

def test_statefulset_history_reads_controller_revisions(db_engine, cluster):
    """Not ReplicaSets: a StatefulSet has none, and listing them would return an
    empty history for a workload that has one."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(6, "postgres:16.1", change_cause="pin 16.1"),
        _controller_revision(7, "postgres:16.2"),
    ]}

    history = rollout_history("statefulsets", "prod", "postgres")

    assert [row["revision"] for row in history["revisions"]] == [7, 6], "newest first"
    assert history["revisions"][1]["images"] == ["postgres:16.1"]
    assert history["revisions"][1]["change_cause"] == "pin 16.1"
    assert history["revisions"][0]["created"] == "2026-05-01T08:00:00Z"
    assert history["partial"] is False
    (listing,) = [r for r in cluster.of("GET") if r.path == CONTROLLER_REVISIONS_PATH]
    assert listing.query["labelSelector"] == "app=postgres", (
        "server-side: a namespace of revisions for forty workloads should not be "
        "transferred to render one panel"
    )


def test_the_statefulsets_current_revision_is_the_one_status_names(db_engine, cluster):
    """A StatefulSet names it by ControllerRevision *name*. Taking the maximum
    instead would report the revision being rolled out as the one in effect,
    which is exactly wrong during a partitioned update."""
    cluster.objects[STATEFULSET_PATH] = _statefulset(
        update_revision="postgres-6", current_revision="postgres-5",
    )
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(5, "postgres:16.0"),
        _controller_revision(6, "postgres:16.1"),
        _controller_revision(7, "postgres:16.2"),
    ]}

    assert rollout_history("statefulsets", "prod", "postgres")["current"] == 6


def test_a_status_pointer_naming_a_pruned_revision_falls_back_to_the_highest(
    db_engine, cluster,
):
    """The pointer names a ControllerRevision that has aged out. Reporting null
    would leave the panel unable to say which revision is running at all."""
    cluster.objects[STATEFULSET_PATH] = _statefulset(
        update_revision="postgres-99", current_revision="postgres-98",
    )
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(6, "postgres:16.1"),
        _controller_revision(7, "postgres:16.2"),
    ]}

    assert rollout_history("statefulsets", "prod", "postgres")["current"] == 7


def test_a_daemonset_has_no_revision_pointer_so_the_highest_is_current(
    db_engine, cluster,
):
    """Exact rather than approximate: the DaemonSet controller renumbers a
    recurring template to the new maximum, so the highest revision is the one in
    effect even immediately after a rollback."""
    cluster.objects[DAEMONSET_PATH] = _daemonset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(3, "exporter:1.6.1", uid="ds-uid"),
        _controller_revision(4, "exporter:1.7.0", uid="ds-uid"),
    ]}

    history = rollout_history("daemonsets", "prod", "node-exporter")

    assert history["current"] == 4
    assert [row["revision"] for row in history["revisions"]] == [4, 3]


def test_a_workload_with_no_revisions_yet_reports_a_null_current(db_engine, cluster):
    """An empty list here is a real answer — the workload has never rolled out —
    and is not the same as a listing that failed, which is `partial`."""
    cluster.objects[STATEFULSET_PATH] = _statefulset(
        update_revision="", current_revision="",
    )
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": []}

    history = rollout_history("statefulsets", "prod", "postgres")

    assert history == {"current": None, "revisions": [], "partial": False, "unavailable": []}


def test_another_workloads_revisions_are_excluded_by_owner_uid(db_engine, cluster):
    """A hand-written selector can legitimately match another workload's pods,
    and its revisions would otherwise appear in this history — offering a
    rollback to a template that was never this workload's."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(7, "postgres:16.2"),
        _controller_revision(9, "redis:7", name="redis-9", uid="other-uid"),
    ]}

    history = rollout_history("statefulsets", "prod", "postgres")

    assert [row["revision"] for row in history["revisions"]] == [7]


def test_a_revision_whose_number_is_not_a_number_is_left_out(db_engine, cluster):
    """Coercing it to 0 would sort it to the bottom and read as the oldest
    revision rather than as one whose number we could not read — and a rollback
    to "0" would target nothing."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(7, "postgres:16.2"),
        _controller_revision("seven", "postgres:x", name="postgres-bad"),
    ]}

    history = rollout_history("statefulsets", "prod", "postgres")

    assert [row["revision"] for row in history["revisions"]] == [7]


def test_a_revision_with_no_stored_template_reports_null_images(db_engine, cluster):
    """`[]` would say the revision ran no containers, which is not a thing a pod
    template can be. Null renders as "could not be read from the revision"."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(7, "postgres:16.2", data={"spec": {}}),
    ]}

    assert rollout_history("statefulsets", "prod", "postgres")["revisions"][0][
        "images"
    ] is None


def test_init_containers_are_listed_in_declaration_order(db_engine, cluster):
    """Sorting makes two revisions running the same images look different, and a
    sidecar stack reads in the order it is declared."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(7, "postgres:16.2", init_image="migrate:3"),
    ]}

    images = rollout_history("statefulsets", "prod", "postgres")["revisions"][0]["images"]

    assert images == ["migrate:3", "postgres:16.2"]


def test_controller_revision_history_degrades_without_losing_the_workload_read(
    db_engine, cluster, fake_k8s,
):
    """The revision listing is a panel; the workload read is the endpoint. Losing
    the first names itself in `unavailable`, and losing the second is a real
    403."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()

    def refuse_revisions(path, method, **kwargs):
        if "controllerrevisions" in path:
            raise K8sApiException(status=403, reason="Forbidden")
        return cluster(path, method, **kwargs)

    fake_k8s.api_client.returns("call_api", refuse_revisions)

    history = rollout_history("statefulsets", "prod", "postgres")

    assert history["revisions"] == []
    assert history["partial"] is True
    assert history["unavailable"][0]["resource"] == "controllerrevisions"
    assert history["unavailable"][0]["reason"] == "forbidden"


def test_a_listing_that_is_not_a_kubernetes_list_is_an_upstream_error(
    db_engine, cluster,
):
    """A proxy or aggregation layer answering 200 with something else must not be
    read as "this workload has no revisions"."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = ["not", "a", "list-object"]

    history = rollout_history("statefulsets", "prod", "postgres")

    assert history["partial"] is True, (
        "collected as a partial panel rather than raised: the current revision is "
        "still worth returning"
    )


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #

def test_rolling_back_a_statefulset_applies_the_stored_patch(db_engine, cluster):
    """A ControllerRevision's `data` already *is* a patch — that is what the
    field holds — so it is applied as the strategic merge it was built as,
    exactly as `kubectl rollout undo` does."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(6, "postgres:16.1"),
        _controller_revision(7, "postgres:16.2"),
    ]}

    response = rollback_workload("statefulsets", "prod", "postgres", 6, True)

    (request,) = cluster.of("PATCH")
    assert request.content_type == "application/strategic-merge-patch+json"
    assert request.body["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "postgres:16.1"
    )
    assert request.query == {"dryRun": "All"}
    assert response["applied"] is False, "a dry run changes nothing"


def test_a_revision_with_an_empty_patch_cannot_be_restored(db_engine, cluster):
    """Applying `{}` is a no-op the funnel would report as a successful
    rollback, leaving the workload on the revision the operator was leaving."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(6, "postgres:16.1", data={}),
        _controller_revision(7, "postgres:16.2"),
    ]}

    with pytest.raises(UpstreamError) as caught:
        rollback_workload("statefulsets", "prod", "postgres", 6, True)

    assert "no stored patch" in caught.value.message
    assert cluster.of("PATCH") == []


def test_a_replicaset_with_no_pod_template_cannot_be_restored(db_engine, cluster):
    """Rolling back to it would replace `/spec/template` with nothing."""
    deployment_path = "/apis/apps/v1/namespaces/prod/deployments/checkout"
    cluster.objects[deployment_path] = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": "checkout", "namespace": "prod", "uid": "dep-uid",
            "resourceVersion": "1", "annotations": {REVISION_ANNOTATION: "14"},
        },
        "spec": {"selector": {"matchLabels": {"app": "checkout"}}, "replicas": 1},
    }
    cluster.objects["/apis/apps/v1/namespaces/prod/replicasets"] = {"items": [{
        "metadata": {
            "name": "checkout-old", "namespace": "prod",
            "annotations": {REVISION_ANNOTATION: "13"},
            "ownerReferences": [{"kind": "Deployment", "uid": "dep-uid"}],
        },
        "spec": {"replicas": 0},
    }]}

    with pytest.raises(UpstreamError) as caught:
        rollback_workload("deployments", "prod", "checkout", 13, True)

    assert "no pod template" in caught.value.message
    assert cluster.of("PATCH") == []


def test_rolling_back_a_kind_with_no_history_is_refused_by_name(db_engine, cluster):
    """`invalid` naming the kind, not a 404: the CronJob exists, and Kubernetes
    keeps no revisions for it at all."""
    with pytest.raises(Invalid) as caught:
        rollback_workload("cronjobs", "prod", "nightly", 3, True)

    assert "CronJob" in caught.value.message
    assert "Deployment, StatefulSet, DaemonSet" in caught.value.hint
    assert cluster.requests == [], "no cluster call is needed to know this"


def test_rolling_back_a_statefulset_to_a_pruned_revision_lists_what_exists(
    db_engine, cluster,
):
    cluster.objects[STATEFULSET_PATH] = _statefulset()
    cluster.objects[CONTROLLER_REVISIONS_PATH] = {"items": [
        _controller_revision(7, "postgres:16.2"),
    ]}

    with pytest.raises(NotFound) as caught:
        rollback_workload("statefulsets", "prod", "postgres", 3, True)

    assert caught.value.context["revision"] == 3
    assert caught.value.context["current"] == 7
    assert "7" in caught.value.detail
    assert cluster.of("PATCH") == []


def test_a_rollback_whose_revision_listing_failed_does_not_proceed(
    db_engine, cluster, fake_k8s,
):
    """History degrades; a rollback does not. Picking a template out of an
    incomplete listing is how an operator rolls back to the wrong revision."""
    cluster.objects[STATEFULSET_PATH] = _statefulset()

    def refuse_revisions(path, method, **kwargs):
        if "controllerrevisions" in path:
            raise K8sApiException(status=403, reason="Forbidden")
        return cluster(path, method, **kwargs)

    fake_k8s.api_client.returns("call_api", refuse_revisions)

    with pytest.raises(Exception) as caught:
        rollback_workload("statefulsets", "prod", "postgres", 6, True)

    assert caught.value.code == "rbac_denied"
