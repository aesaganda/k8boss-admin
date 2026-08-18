"""
The single write funnel.

§0 makes four promises about every mutation — gated, preflighted, diffed,
audited. This file is where they are actually checked, once, on the one function
every write goes through. Testing them again at each endpoint would let the
copies drift and start passing for the wrong reason; testing them only here means
a new endpoint inherits them by construction.

The two assertions that matter most:

* **``applied`` is true only when a real write succeeded.** A successful dry run
  returns a full projected object, a resourceVersion and a diff — everything a
  success looks like — and a UI that read any of those as "it worked" would tell
  an operator their production change had landed when nothing was written.
* **A refused write is still recorded.** An audit trail holding only the writes
  that worked answers "what changed" but not "who tried", and the second is the
  question asked after an incident.
"""

from __future__ import annotations

import pytest

from app.admin.diff import digest
from app.admin.mutate import mutate
from app.audit import recorder
from app.errors import Conflict, Invalid, MutationsDisabled, RBACDenied, UpstreamError
from tests.conftest import obj

LIVE = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "checkout", "namespace": "prod", "resourceVersion": "884213"},
    "spec": {"replicas": 3},
}
PROJECTED = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "checkout", "namespace": "prod", "resourceVersion": "884213"},
    "spec": {"replicas": 5},
}


def allow(fake, *, allowed=True, evaluation_error=None):
    """Stub the preflight review. Every test here goes through the real one."""
    fake.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=allowed, reason=None, evaluation_error=evaluation_error, denied=False,
    )))


def applier(result=PROJECTED, warnings=(), raises=None):
    """An ``apply_fn`` that records the dry-run flag it was called with."""
    calls: list[bool] = []

    def apply_fn(dry_run):
        calls.append(dry_run)
        if raises is not None:
            raise raises
        return result, list(warnings)

    apply_fn.calls = calls
    return apply_fn


def run(fake, apply_fn, *, dry_run=True, verb="patch", group="apps", plural="deployments",
        before=LIVE, **extra):
    return mutate(
        verb=verb, group=group, version="v1", plural=plural,
        namespace="prod", name="checkout", dry_run=dry_run,
        apply_fn=apply_fn, before=before, **extra,
    )


def audit_rows():
    return recorder.query(limit=50)["items"]


# --------------------------------------------------------------------------- #
# The mutations gate (§1.6)
# --------------------------------------------------------------------------- #

def test_read_only_mode_blocks_a_real_write_before_the_cluster_is_touched(db_engine, fake_k8s):
    allow(fake_k8s)
    apply_fn = applier()

    with pytest.raises(MutationsDisabled) as caught:
        run(fake_k8s, apply_fn, dry_run=False)

    assert caught.value.http_status == 403
    assert caught.value.code == "mutations_disabled", (
        "not rbac_denied: the operator's permissions are irrelevant, the "
        "deployment is read-only, and naming RBAC sends them to fix the wrong system"
    )
    assert apply_fn.calls == [], "the cluster must not be touched"
    assert fake_k8s.authorization_v1.calls == [], "nor asked whether we may"


def test_read_only_mode_still_allows_a_dry_run(db_engine, fake_k8s):
    """Inspecting what *would* change is a read. This is what makes the console
    usable in an audit posture (§1.6)."""
    allow(fake_k8s)
    apply_fn = applier()

    response = run(fake_k8s, apply_fn, dry_run=True)

    assert apply_fn.calls == [True]
    assert response["dryRun"] is True
    assert response["applied"] is False
    assert response["diff"]["changed"] is True


def test_a_write_refused_by_the_gate_is_still_audited(db_engine, fake_k8s):
    allow(fake_k8s)

    with pytest.raises(MutationsDisabled):
        run(fake_k8s, applier(), dry_run=False)

    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["dry_run"] is False
    assert "mutations_disabled" in row["error"]
    assert row["target"]["name"] == "checkout"


# --------------------------------------------------------------------------- #
# applied
# --------------------------------------------------------------------------- #

def test_applied_is_false_for_a_successful_dry_run(db_engine, fake_k8s, allow_mutations):
    """A dry run returns an object, a resourceVersion and a diff — everything a
    success looks like. `applied` is the only field that says whether a cluster
    changed."""
    allow(fake_k8s)

    response = run(fake_k8s, applier(), dry_run=True)

    assert response["applied"] is False
    assert response["dryRun"] is True
    assert response["resourceVersion"] == "884213"
    assert audit_rows()[0]["outcome"] == "dry_run"


def test_applied_is_true_only_for_a_real_successful_write(db_engine, fake_k8s, allow_mutations):
    allow(fake_k8s)
    apply_fn = applier()

    response = run(fake_k8s, apply_fn, dry_run=False)

    assert apply_fn.calls == [False], "the dry-run flag reaches the apply verbatim"
    assert response["applied"] is True
    assert audit_rows()[0]["outcome"] == "applied"


# --------------------------------------------------------------------------- #
# Preflight (§0.2)
# --------------------------------------------------------------------------- #

def test_a_denied_write_never_reaches_the_cluster_and_is_audited(db_engine, fake_k8s,
                                                                 allow_mutations):
    allow(fake_k8s, allowed=False)
    apply_fn = applier()

    with pytest.raises(RBACDenied) as caught:
        run(fake_k8s, apply_fn, dry_run=False)

    assert apply_fn.calls == []
    assert "`patch`" in caught.value.hint
    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["error"].startswith("rbac_denied:")
    assert row["diff_digest"] is None, "nothing was diffed because nothing was attempted"


def test_a_preflight_that_could_not_be_decided_is_not_reported_as_a_denial(
    db_engine, fake_k8s, allow_mutations,
):
    """The permission may well be held. 403 here would send the operator to edit
    a ClusterRole that is already correct."""
    allow(fake_k8s, allowed=False, evaluation_error="webhook authorizer unavailable")
    apply_fn = applier()

    with pytest.raises(UpstreamError) as caught:
        run(fake_k8s, apply_fn, dry_run=False)

    assert caught.value.http_status == 502
    assert apply_fn.calls == []
    assert audit_rows()[0]["outcome"] == "denied"


def test_a_dry_run_is_preflighted_too(db_engine, fake_k8s):
    """The API server needs the same permission to project a write as to perform
    one. Finding out at the confirm step rather than at the preview step is the
    worse of the two."""
    allow(fake_k8s, allowed=False)

    with pytest.raises(RBACDenied):
        run(fake_k8s, applier(), dry_run=True)

    assert len(fake_k8s.authorization_v1.called("create_self_subject_access_review")) == 1


def test_the_preflight_names_the_subresource_when_there_is_one(db_engine, fake_k8s):
    """RBAC names `deployments/scale` separately; preflighting `deployments`
    would check a broader permission than the write needs."""
    allow(fake_k8s)

    run(fake_k8s, applier(), subresource="scale")

    (args, _kwargs) = fake_k8s.authorization_v1.called("create_self_subject_access_review")[0]
    assert args[0].spec.resource_attributes.subresource == "scale"
    assert audit_rows()[0]["target"]["subresource"] == "scale"


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #

def test_a_failed_apply_is_audited_and_re_raised(db_engine, fake_k8s, allow_mutations):
    allow(fake_k8s)
    error = Invalid("The cluster rejected the object.", detail="spec.replicas: must be >= 0")

    with pytest.raises(Invalid):
        run(fake_k8s, applier(raises=error), dry_run=False)

    (row,) = audit_rows()
    assert row["outcome"] == "failed"
    assert row["error"] == "invalid: The cluster rejected the object."


def test_a_conflict_is_audited_as_a_conflict(db_engine, fake_k8s, allow_mutations):
    """§10 has `conflict` as its own outcome: a stale edit is a different event
    from a rejected one, and an operator reviewing the trail is looking for the
    concurrent write, not for a bad manifest."""
    allow(fake_k8s)

    with pytest.raises(Conflict):
        run(fake_k8s, applier(raises=Conflict("checkout changed since it was loaded.")),
            dry_run=False)

    assert audit_rows()[0]["outcome"] == "conflict"


# --------------------------------------------------------------------------- #
# The §1.5 response
# --------------------------------------------------------------------------- #

def test_the_response_carries_every_field_1_5_names(db_engine, fake_k8s):
    allow(fake_k8s)

    response = run(fake_k8s, applier(warnings=["metadata.annotations will be replaced"]))

    assert set(response) == {
        "dryRun", "applied", "verb", "target", "diff", "resourceVersion",
        "warnings", "auditId",
    }
    assert response["verb"] == "patch"
    assert response["warnings"] == ["metadata.annotations will be replaced"]
    assert set(response["diff"]) == {"before", "after", "unified", "changed"}
    assert response["auditId"] == audit_rows()[0]["id"]


def test_the_target_carries_the_real_group_name_not_the_wire_spelling(db_engine, fake_k8s):
    """§1.4: nothing downstream of the route sees `core`. The audit row is the
    record of what was addressed on the API server, and a group literally named
    `core` would be indistinguishable from one named core."""
    allow(fake_k8s)

    response = run(fake_k8s, applier(), group="core", plural="pods", verb="delete")

    assert response["target"]["group"] == ""
    assert audit_rows()[0]["target"] == {
        "group": "", "version": "v1", "resource": "pods",
        "namespace": "prod", "name": "checkout", "subresource": None,
    }


def test_the_recorded_digest_matches_the_diff_that_was_returned(db_engine, fake_k8s):
    """This is the proof that what an operator confirmed is what was applied."""
    allow(fake_k8s)

    response = run(fake_k8s, applier())

    assert audit_rows()[0]["diff_digest"] == digest(response["diff"])


def test_a_no_op_write_is_reported_as_changing_nothing(db_engine, fake_k8s):
    """§1.5: the UI offers "nothing would change" rather than a confirm button."""
    allow(fake_k8s)

    response = run(fake_k8s, applier(result=LIVE), before=LIVE)

    assert response["diff"]["changed"] is False
    assert response["diff"]["unified"] == ""


def test_a_delete_falls_back_to_the_live_resource_version(db_engine, fake_k8s):
    """There is no projection to read it from, and null would leave the UI with
    no version to send on the confirming call."""
    allow(fake_k8s)

    response = run(fake_k8s, applier(result=None), verb="delete")

    assert response["diff"]["after"] == ""
    assert response["diff"]["changed"] is True
    assert response["resourceVersion"] == "884213"


def test_the_audit_detail_reaches_the_row(db_engine, fake_k8s):
    allow(fake_k8s)

    run(fake_k8s, applier(), detail="replicas 3 -> 5")

    assert audit_rows()[0]["detail"] == "replicas 3 -> 5"


# --------------------------------------------------------------------------- #
# The funnel's callers (§6): scale, restart, suspend, rollout, rollback
# --------------------------------------------------------------------------- #
#
# Tested here rather than in their own file because the property under test is
# the same one: each of these is a *description of a patch* handed to the funnel,
# and what can go wrong is describing the wrong patch. That they are gated,
# preflighted, diffed and audited is already settled above, once, for all of them.

from types import SimpleNamespace  # noqa: E402

from app.admin.rollout import (  # noqa: E402
    POD_TEMPLATE_HASH_LABEL,
    REVISION_ANNOTATION,
    rollback_workload,
    rollout_history,
)
from app.admin.scale import (  # noqa: E402
    RESTART_ANNOTATION,
    restart_workload,
    scale_workload,
    suspend_workload,
)
from app.errors import NotFound  # noqa: E402


class FakeCluster:
    """A path-addressed stand-in for ``ApiClient.call_api``.

    Path-addressed because these callers make *two* different reads — the
    workload and its revisions — and a fake that answered both with one payload
    would let a module read the wrong URL and still pass.
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
    allow(fake_k8s)
    return api_server


DEPLOYMENT_PATH = "/apis/apps/v1/namespaces/prod/deployments/checkout"
SCALE_PATH = DEPLOYMENT_PATH + "/scale"

DEPLOYMENT = {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "metadata": {
        "name": "checkout", "namespace": "prod", "uid": "dep-uid",
        "resourceVersion": "884213", "annotations": {REVISION_ANNOTATION: "14"},
    },
    "spec": {
        "selector": {"matchLabels": {"app": "checkout"}},
        "replicas": 3,
        "template": {
            "metadata": {"labels": {"app": "checkout"}},
            "spec": {"containers": [{"name": "app", "image": "ghcr.io/acme/checkout:1.9.2"}]},
        },
    },
    "status": {"replicas": 3},
}

SCALE = {
    "apiVersion": "autoscaling/v1", "kind": "Scale",
    "metadata": {"name": "checkout", "namespace": "prod", "resourceVersion": "884213"},
    "spec": {"replicas": 3},
    "status": {"replicas": 3},
}


def _replicaset(revision, image, *, name="checkout-7d9"):
    return {
        "apiVersion": "apps/v1", "kind": "ReplicaSet",
        "metadata": {
            "name": name, "namespace": "prod", "uid": f"rs-{revision}",
            "creationTimestamp": "2026-05-01T08:00:00Z",
            "annotations": {REVISION_ANNOTATION: str(revision),
                            "kubernetes.io/change-cause": f"deploy {image}"},
            "ownerReferences": [{"kind": "Deployment", "name": "checkout", "uid": "dep-uid"}],
        },
        "spec": {"template": {
            "metadata": {"labels": {"app": "checkout", POD_TEMPLATE_HASH_LABEL: "7d9"}},
            "spec": {"containers": [{"name": "app", "image": image}]},
        }},
    }


def test_scale_patches_the_scale_subresource_not_the_object(db_engine, cluster):
    """`deployments/scale` is a separate RBAC resource: patching the object would
    need the broader grant for no reason, and would diff a whole Deployment where
    four lines say it."""
    cluster.objects[SCALE_PATH] = SCALE
    cluster.projections[SCALE_PATH] = {**SCALE, "spec": {"replicas": 5}}

    response = scale_workload("deployments", "prod", "checkout", 5, True)

    (request,) = cluster.of("PATCH")
    assert request.path == SCALE_PATH
    assert request.body == {"spec": {"replicas": 5}}
    assert request.query == {"dryRun": "All"}
    assert request.content_type == "application/merge-patch+json"
    assert response["target"]["subresource"] == "scale"
    assert "-  replicas: 3" in response["diff"]["unified"]
    assert audit_rows()[0]["detail"] == "replicas 3 -> 5"


def test_scaling_a_kind_with_no_scale_subresource_is_refused_by_name(db_engine, cluster):
    """Forwarded, this returns the API server's 404 on the missing /scale path,
    which reads as "your DaemonSet is gone"."""
    with pytest.raises(Invalid) as caught:
        scale_workload("daemonsets", "kube-system", "cilium", 3, True)

    assert "DaemonSet" in caught.value.message
    assert cluster.requests == []


def test_restart_stamps_the_pod_template_the_way_kubectl_does(db_engine, cluster):
    cluster.objects[DEPLOYMENT_PATH] = DEPLOYMENT

    restart_workload("deployments", "prod", "checkout", True)

    (request,) = cluster.of("PATCH")
    stamped = request.body["spec"]["template"]["metadata"]["annotations"]
    assert list(stamped) == [RESTART_ANNOTATION]
    assert stamped[RESTART_ANNOTATION].endswith("Z")
    assert RESTART_ANNOTATION in audit_rows()[0]["detail"]


def test_suspend_is_refused_on_a_kind_without_spec_suspend(db_engine, cluster):
    with pytest.raises(Invalid) as caught:
        suspend_workload("deployments", "prod", "checkout", True, True)

    assert "Deployment" in caught.value.message


def test_suspend_records_the_value_it_replaced(db_engine, cluster):
    path = "/apis/batch/v1/namespaces/prod/cronjobs/nightly"
    cluster.objects[path] = {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "nightly", "namespace": "prod", "resourceVersion": "1"},
        "spec": {"schedule": "0 3 * * *"},
    }

    suspend_workload("cronjobs", "prod", "nightly", True, True)

    (request,) = cluster.of("PATCH")
    assert request.body == {"spec": {"suspend": True}}
    assert audit_rows()[0]["detail"] == "suspend false -> true"


def test_a_kind_with_no_history_returns_the_unsupported_envelope(db_engine, cluster):
    """`{"current": null, "revisions": []}` alone would state that the Job exists
    and has never been rolled out. It has no revision concept at all."""
    history = rollout_history("jobs", "prod", "migrate")

    assert history["current"] is None
    assert history["revisions"] == []
    assert history["partial"] is True
    assert [entry["reason"] for entry in history["unavailable"]] == ["unsupported"]
    assert cluster.requests == [], "no cluster call is needed to know a Job has no history"


def test_deployment_history_reads_replicasets_by_their_revision_annotation(db_engine, cluster):
    cluster.objects[DEPLOYMENT_PATH] = DEPLOYMENT
    cluster.objects["/apis/apps/v1/namespaces/prod/replicasets"] = {"items": [
        _replicaset(13, "ghcr.io/acme/checkout:1.9.1", name="checkout-6c2"),
        _replicaset(14, "ghcr.io/acme/checkout:1.9.2"),
        {"metadata": {"name": "someone-elses", "uid": "x",
                      "annotations": {REVISION_ANNOTATION: "9"},
                      "ownerReferences": [{"kind": "Deployment", "uid": "other-uid"}]}},
    ]}

    history = rollout_history("deployments", "prod", "checkout")

    assert history["current"] == 14
    assert [row["revision"] for row in history["revisions"]] == [14, 13], "newest first"
    assert history["revisions"][1]["images"] == ["ghcr.io/acme/checkout:1.9.1"]
    assert history["revisions"][0]["change_cause"] == "deploy ghcr.io/acme/checkout:1.9.2"
    assert history["partial"] is False


def test_history_degrades_the_revision_list_without_losing_the_current_revision(
    db_engine, cluster, fake_k8s,
):
    """Two independent facts — which revision is current, and what the revisions
    are. Losing the listing must not lose the one we already hold."""
    from kubernetes.client.rest import ApiException as K8sApiException

    cluster.objects[DEPLOYMENT_PATH] = DEPLOYMENT

    def refuse_replicasets(path, method, **kwargs):
        if "replicasets" in path:
            raise K8sApiException(status=403, reason="Forbidden")
        return cluster(path, method, **kwargs)

    fake_k8s.api_client.returns("call_api", refuse_replicasets)

    history = rollout_history("deployments", "prod", "checkout")

    assert history["current"] == 14, "read from the Deployment's own annotation"
    assert history["revisions"] == []
    assert history["partial"] is True
    assert [entry["reason"] for entry in history["unavailable"]] == ["forbidden"]


def test_rollback_replaces_the_whole_pod_template(db_engine, cluster):
    """A merge patch would leave behind any container the old revision did not
    have — producing a workload that is neither revision, reported as a
    successful rollback."""
    cluster.objects[DEPLOYMENT_PATH] = DEPLOYMENT
    cluster.objects["/apis/apps/v1/namespaces/prod/replicasets"] = {"items": [
        _replicaset(13, "ghcr.io/acme/checkout:1.9.1", name="checkout-6c2"),
        _replicaset(14, "ghcr.io/acme/checkout:1.9.2"),
    ]}

    rollback_workload("deployments", "prod", "checkout", 13, True)

    (request,) = cluster.of("PATCH")
    assert request.content_type == "application/json-patch+json"
    (op,) = request.body
    assert op["op"] == "replace" and op["path"] == "/spec/template"
    assert op["value"]["spec"]["containers"][0]["image"] == "ghcr.io/acme/checkout:1.9.1"
    assert POD_TEMPLATE_HASH_LABEL not in op["value"]["metadata"]["labels"], (
        "left in, the controller hashes a template that already contains its own "
        "hash and the rollback creates a fourth revision instead of returning to "
        "the second"
    )
    assert audit_rows()[0]["detail"] == "rollback 14 -> 13"


def test_rolling_back_to_a_pruned_revision_says_which_ones_exist(db_engine, cluster):
    cluster.objects[DEPLOYMENT_PATH] = DEPLOYMENT
    cluster.objects["/apis/apps/v1/namespaces/prod/replicasets"] = {"items": [
        _replicaset(14, "ghcr.io/acme/checkout:1.9.2"),
    ]}

    with pytest.raises(NotFound) as caught:
        rollback_workload("deployments", "prod", "checkout", 2, True)

    assert "14" in caught.value.detail
    assert "revisionHistoryLimit" in caught.value.hint
    assert cluster.of("PATCH") == []
