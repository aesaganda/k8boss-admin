"""
VolumeSnapshots (§22).

One object created through the funnel, and a row that refuses to overstate what
came of it. What is worth testing is not that a POST is sent — §4 covers that —
but the four claims this feature is careful about:

* **`readyToUse` is a real tri-state.** The API declares it `*bool` on purpose.
  `null` means the controller has not reported yet, which is what the first
  minutes of a large snapshot look like. Collapsing it into `false` says a backup
  failed while it is being taken; collapsing it into `true` says a restorable
  snapshot exists when none may, and only one of those gets somebody to delete
  the source volume.

* **`applied: true` means an object exists, not that a snapshot was taken.** The
  controller does the work afterwards. Nothing in the write response is evidence
  that there is anything to restore from.

* **A snapshot is not a backup, and is not quiesced.** Both are on every
  snapshot, because both are what the word is routinely believed to mean and
  does not — and being wrong about either is discovered during a restore.

* **`deletionPolicy` decides whether deleting the object later destroys the
  data**, and it lives on a cluster-scoped class the person deleting it will not
  have opened. Unknown stays unknown: reporting it as `Delete` warns about loss
  that will not happen, reporting it as `Retain` withholds a warning about loss
  that will.
"""

from __future__ import annotations

import pytest

from app.admin import apply as apply_service
from app.admin import snapshot
from app.audit import recorder
from app.errors import Invalid, NotFound, RBACDenied
from app.resources import catalog
from app.resources.shaping import volumesnapshot_row, volumesnapshotclass_row
from tests.conftest import obj
from tests.test_routes import _groups_payload, _resources

NAMESPACE = "prod"
CLAIM = "postgres-data"
NAME = "postgres-before-upgrade"

ALWAYS = [snapshot.WARN_NOT_A_BACKUP, snapshot.WARN_CRASH_CONSISTENT]


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


def snapshot_class(name="csi-ebs", *, policy="Delete", default=False, driver="ebs.csi.aws.com"):
    body = {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshotClass",
        "metadata": {"name": name},
        "driver": driver,
        "deletionPolicy": policy,
    }
    if default:
        body["metadata"]["annotations"] = {
            snapshot.SNAPSHOT_DEFAULT_CLASS_ANNOTATION: "true",
        }
    return body


def claim(*, phase="Bound", capacity="50Gi"):
    body = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": CLAIM, "namespace": NAMESPACE, "resourceVersion": "7710"},
        "spec": {"storageClassName": "gp3", "volumeName": "pvc-9f2a"},
        "status": {"phase": phase},
    }
    if capacity is not None:
        body["status"]["capacity"] = {"storage": capacity}
    return body


@pytest.fixture
def cluster(monkeypatch, fake_k8s):
    """Discovery for snapshots and claims, an allowing preflight, a writable server."""

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return _groups_payload(("snapshot.storage.k8s.io", "v1"))
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/api/v1":
            return _resources(("persistentvolumeclaims", "PersistentVolumeClaim"))
        if path == "/apis/snapshot.storage.k8s.io/v1":
            return _resources(
                ("volumesnapshots", "VolumeSnapshot"),
                ("volumesnapshotclasses", "VolumeSnapshotClass"),
            )
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    return fake_k8s


def stub_reads(monkeypatch, *, live=None, claim_error=None, classes=None, class_error=None):
    def fake_get(group, version, plural, name, namespace=None):
        assert plural == "persistentvolumeclaims", plural
        if claim_error is not None:
            raise claim_error
        return live if live is not None else claim()

    def fake_list(group, version, plural, *, namespace=None, limit=500, **kwargs):
        assert plural == "volumesnapshotclasses", plural
        if class_error is not None:
            raise class_error
        return {"items": list(classes if classes is not None else [snapshot_class(default=True)])}

    monkeypatch.setattr(snapshot.reader, "get_resource", fake_get)
    monkeypatch.setattr(snapshot.reader, "list_resource", fake_list)


def stub_create(monkeypatch, *, raises=None):
    calls: list[dict] = []

    def fake_request_json(method, path, **kwargs):
        calls.append({
            "method": method, "path": path,
            "query": dict(q for q in (kwargs.get("query") or []) if q[1] is not None),
            "body": kwargs.get("body"),
        })
        if raises is not None:
            raise raises
        created = dict(kwargs.get("body") or {})
        created.setdefault("status", {})
        return created, []

    monkeypatch.setattr(apply_service, "request_json", fake_request_json)
    return calls


def audit_rows():
    return recorder.query(limit=50)["items"]


def body(**extra):
    return {"name": NAME, **extra}


# --------------------------------------------------------------------------- #
# The row — readyToUse is three answers, not two
# --------------------------------------------------------------------------- #

def volume_snapshot(*, ready=True, error=None, content="snapcontent-9f2a", size="50Gi"):
    body_ = {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": {
            "source": {"persistentVolumeClaimName": CLAIM},
            "volumeSnapshotClassName": "csi-ebs",
        },
        "status": {},
    }
    if ready is not None:
        body_["status"]["readyToUse"] = ready
    if content is not None:
        body_["status"]["boundVolumeSnapshotContentName"] = content
    if size is not None:
        body_["status"]["restoreSize"] = size
    if error is not None:
        body_["status"]["error"] = error
    return body_


def test_a_ready_snapshot_reports_true():
    row = volumesnapshot_row(volume_snapshot(ready=True))

    assert row["ready_to_use"] is True
    assert row["source_claim"] == CLAIM
    assert row["restore_size_bytes"] == 50 * 1024**3


def test_a_snapshot_still_being_taken_is_null_not_false():
    """The first minutes of a large snapshot. `false` here says the backup
    failed while it is being written."""
    row = volumesnapshot_row(volume_snapshot(ready=None, content=None, size=None))

    assert row["ready_to_use"] is None
    assert row["bound_content"] is None
    assert row["restore_size_bytes"] is None


def test_a_failed_snapshot_is_false_and_carries_the_reason():
    row = volumesnapshot_row(volume_snapshot(ready=False, error={
        "message": "Failed to check and update snapshot content: "
                   "rpc error: code = NotFound",
        "time": "2026-09-05T10:00:00Z",
    }))

    assert row["ready_to_use"] is False
    assert "rpc error" in row["error"]["message"]


def test_a_snapshot_adopted_from_content_is_not_reported_as_claim_sourced():
    """The two sources are mutually exclusive and mean different things: one
    captures a claim, the other adopts something already in the storage system."""
    adopted = volume_snapshot()
    adopted["spec"]["source"] = {"volumeSnapshotContentName": "snapcontent-imported"}

    row = volumesnapshot_row(adopted)

    assert row["source_claim"] is None
    assert row["source_content"] == "snapcontent-imported"


def test_the_class_row_carries_the_deletion_policy_and_default_marker():
    row = volumesnapshotclass_row(snapshot_class(policy="Retain", default=True))

    assert row["deletion_policy"] == "Retain"
    assert row["is_default"] is True

    assert volumesnapshotclass_row(snapshot_class())["is_default"] is False


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #

def test_a_name_is_required_rather_than_generated():
    with pytest.raises(Invalid) as caught:
        snapshot.validate_request({})

    assert caught.value.context["parameter"] == "name"


@pytest.mark.parametrize("name", ["Snapshot", "with spaces", "-leading", "trailing-", "a" * 254])
def test_a_name_the_api_server_would_refuse_is_refused_here(name):
    with pytest.raises(Invalid) as caught:
        snapshot.validate_request({"name": name})

    assert caught.value.context["parameter"] == "name"


def test_an_omitted_class_is_kept_as_none_rather_than_defaulted_here():
    """Absence is a request for the cluster default, which is a real thing the
    API does — and resolving it here would pin whichever class was default when
    the dialog opened."""
    assert snapshot.validate_request(body())["snapshotClass"] is None


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(Invalid):
        snapshot.validate_request(body(source="somewhere-else"))


# --------------------------------------------------------------------------- #
# Which class, and what it does on delete
# --------------------------------------------------------------------------- #

def test_a_named_class_is_resolved_with_its_policy(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, classes=[snapshot_class("csi-ebs", policy="Retain")])
    unavailable: list = []

    resolved = snapshot.resolve_class("csi-ebs", unavailable)

    assert resolved["deletion_policy"] == "Retain"
    assert unavailable == []


def test_an_omitted_class_resolves_to_the_cluster_default(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, classes=[
        snapshot_class("other", policy="Retain"),
        snapshot_class("csi-ebs", policy="Delete", default=True),
    ])

    resolved = snapshot.resolve_class(None, [])

    assert resolved["name"] == "csi-ebs"
    assert resolved["deletion_policy"] == "Delete"


def test_a_class_named_but_absent_is_refused_listing_the_real_ones(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, classes=[snapshot_class("csi-ebs")])

    with pytest.raises(Invalid) as caught:
        snapshot.resolve_class("typo-class", [])

    assert caught.value.context["parameter"] == "snapshotClass"
    assert caught.value.context["available"] == ["csi-ebs"]


def test_a_refused_class_listing_leaves_the_policy_unknown(cluster, monkeypatch, db_engine):
    """`null` is not `Delete` and it is not `Retain`. One would warn about data
    loss that will not happen; the other withholds a warning about loss that
    will."""
    stub_reads(monkeypatch, class_error=RBACDenied("volumesnapshotclasses is forbidden"))
    unavailable: list = []

    resolved = snapshot.resolve_class(None, unavailable)

    assert resolved["deletion_policy"] is None
    assert resolved["reason"] == "forbidden"
    assert unavailable and unavailable[0]["reason"] == "forbidden"


def test_no_default_class_is_unknown_rather_than_a_guess(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, classes=[snapshot_class("a"), snapshot_class("b")])

    resolved = snapshot.resolve_class(None, [])

    assert resolved["name"] is None
    assert resolved["deletion_policy"] is None
    assert resolved["reason"] == "no_default"


def test_a_cluster_with_no_snapshot_classes_at_all_is_unknown(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, classes=[])

    assert snapshot.resolve_class(None, [])["reason"] == "no_default"


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def test_every_snapshot_carries_the_two_that_are_always_true(
    cluster, monkeypatch, db_engine,
):
    """Both are what "snapshot" is routinely believed to mean and does not, and
    being wrong about either is discovered during a restore."""
    stub_reads(monkeypatch)

    codes = [e["code"] for e in snapshot.plan(NAMESPACE, CLAIM, body())["consequences"]]

    assert snapshot.WARN_NOT_A_BACKUP in codes
    assert snapshot.WARN_CRASH_CONSISTENT in codes


def test_a_delete_policy_warns_that_deleting_the_object_destroys_the_snapshot(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Delete", default=True)])

    plan = snapshot.plan(NAMESPACE, CLAIM, body())
    entry = next(e for e in plan["consequences"] if e["code"] == snapshot.WARN_DELETE_DESTROYS)

    assert "deletionPolicy: Delete" in entry["consequence"]


def test_a_retain_policy_raises_no_deletion_consequence(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])

    codes = [e["code"] for e in snapshot.plan(NAMESPACE, CLAIM, body())["consequences"]]

    assert snapshot.WARN_DELETE_DESTROYS not in codes
    assert snapshot.WARN_DELETION_POLICY_UNKNOWN not in codes


def test_an_unknown_policy_warns_that_it_is_unknown_rather_than_either_way(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, class_error=RBACDenied("no"))

    codes = [e["code"] for e in snapshot.plan(NAMESPACE, CLAIM, body())["consequences"]]

    assert snapshot.WARN_DELETION_POLICY_UNKNOWN in codes
    assert snapshot.WARN_DELETE_DESTROYS not in codes


def test_an_unbound_claim_is_a_consequence_not_a_refusal(cluster, monkeypatch, db_engine):
    """A claim can bind between the plan and the write, so this console does not
    refuse — it just declines to pretend the result will be usable."""
    stub_reads(monkeypatch, live=claim(phase="Pending", capacity=None))

    codes = [e["code"] for e in snapshot.plan(NAMESPACE, CLAIM, body())["consequences"]]

    assert snapshot.WARN_CLAIM_NOT_BOUND in codes


def test_a_write_without_the_acknowledgements_is_refused_naming_them(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    calls = stub_create(monkeypatch)

    with pytest.raises(Invalid) as caught:
        snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=False,
                               acknowledge_consequences=[])

    assert set(caught.value.context["unacknowledged"]) == set(ALWAYS)
    assert calls == []
    assert audit_rows() == []


def test_acknowledgements_are_recomputed_against_the_cluster_not_trusted(
    cluster, monkeypatch, db_engine,
):
    """The default class can change between the plan and the write, and the
    deletion consequence is the one a caller with a stale list would skip."""
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Delete", default=True)])
    calls = stub_create(monkeypatch)

    with pytest.raises(Invalid) as caught:
        snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=False,
                               acknowledge_consequences=ALWAYS)

    assert caught.value.context["unacknowledged"] == [snapshot.WARN_DELETE_DESTROYS]
    assert calls == []


# --------------------------------------------------------------------------- #
# The object built
# --------------------------------------------------------------------------- #

def test_the_snapshot_names_the_claim_as_its_source():
    built = snapshot.build_snapshot(NAMESPACE, CLAIM, {"name": NAME, "snapshotClass": "csi-ebs"})

    assert built["spec"]["source"] == {"persistentVolumeClaimName": CLAIM}
    assert built["spec"]["volumeSnapshotClassName"] == "csi-ebs"
    assert built["metadata"] == {"name": NAME, "namespace": NAMESPACE}


def test_an_omitted_class_is_left_out_of_the_object_entirely():
    """Omitting the field is what asks the controller for the default. Pinning
    the name this console read a moment ago would make the snapshot depend on
    which class was default when the dialog opened."""
    built = snapshot.build_snapshot(NAMESPACE, CLAIM, {"name": NAME, "snapshotClass": None})

    assert "volumeSnapshotClassName" not in built["spec"]


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def test_the_plan_writes_nothing_and_audits_nothing(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch)
    calls = stub_create(monkeypatch)

    snapshot.plan(NAMESPACE, CLAIM, body())

    assert calls == []
    assert audit_rows() == []


def test_the_plan_describes_the_claim_being_captured(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch)

    plan = snapshot.plan(NAMESPACE, CLAIM, body())

    assert plan["claim"]["capacity"] == "50Gi"
    assert plan["claim"]["phase"] == "Bound"
    assert plan["snapshotClass"]["name"] == "csi-ebs"


def test_a_refused_class_listing_makes_the_plan_partial(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, class_error=RBACDenied("no"))

    plan = snapshot.plan(NAMESPACE, CLAIM, body())

    assert plan["partial"] is True
    assert plan["unavailable"][0]["resource"] == "volumesnapshotclasses"


def test_the_plan_carries_the_gate_so_the_dialog_can_disable_with_the_reason(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_reads(monkeypatch)

    assert snapshot.plan(NAMESPACE, CLAIM, body())["gate"]["enabled"] is True


# --------------------------------------------------------------------------- #
# The write, through the funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_sends_dryrun_all_and_creates_nothing(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    calls = stub_create(monkeypatch)

    result = snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=True,
                                    acknowledge_consequences=ALWAYS)

    (call,) = calls
    assert call["method"] == "POST"
    assert call["query"]["dryRun"] == "All"
    assert result["applied"] is False
    (row,) = audit_rows()
    assert row["outcome"] == "dry_run"


def test_a_confirmed_write_creates_the_object_and_audits_the_claim(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    calls = stub_create(monkeypatch)

    result = snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=False,
                                    acknowledge_consequences=ALWAYS)

    (call,) = calls
    assert "dryRun" not in call["query"]
    assert call["body"]["spec"]["source"]["persistentVolumeClaimName"] == CLAIM
    assert result["applied"] is True
    (row,) = audit_rows()
    # The claim is in the sentence: the question afterwards is what was captured,
    # and a name chosen under pressure does not always say.
    assert row["detail"] == (
        f"snapshot {NAMESPACE}/{NAME} of claim {CLAIM} via csi-ebs"
    )


def test_applied_true_does_not_claim_a_snapshot_was_taken(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """The one claim this endpoint must never make. The controller does the work
    afterwards and reports it in readyToUse, which starts out null."""
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    stub_create(monkeypatch)

    result = snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=False,
                                    acknowledge_consequences=ALWAYS)

    assert result["applied"] is True
    # Nothing in the response asserts readiness — the object as created carries
    # no status the controller has written.
    assert result["diff"]["unified"]
    assert "readyToUse" not in (result["diff"]["unified"] or "")


def test_the_preflight_asks_to_create_snapshots_in_this_namespace(
    cluster, monkeypatch, db_engine, fake_k8s,
):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    stub_create(monkeypatch)

    snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=True,
                           acknowledge_consequences=ALWAYS)

    ((args, _kwargs),) = fake_k8s.authorization_v1.called(
        "create_self_subject_access_review"
    )
    attributes = args[0].spec.resource_attributes
    assert attributes.verb == "create"
    assert attributes.resource == "volumesnapshots"
    assert attributes.group == "snapshot.storage.k8s.io"
    assert attributes.namespace == NAMESPACE


def test_a_denied_preflight_records_the_denial_and_creates_nothing(
    cluster, monkeypatch, db_engine, fake_k8s,
):
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    calls = stub_create(monkeypatch)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no create on volumesnapshots",
        evaluation_error=None, denied=True,
    )))

    with pytest.raises(RBACDenied):
        snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=True,
                               acknowledge_consequences=ALWAYS)

    assert calls == []
    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["target"]["resource"] == "volumesnapshots"


def test_a_claim_that_could_not_be_read_is_not_snapshotted(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, claim_error=NotFound("no such claim"))
    calls = stub_create(monkeypatch)

    with pytest.raises(NotFound):
        snapshot.take_snapshot(NAMESPACE, CLAIM, body(), dry_run=True,
                               acknowledge_consequences=ALWAYS)

    assert calls == []


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

def test_the_plan_endpoint_answers_with_the_consequences(client, cluster, monkeypatch):
    stub_reads(monkeypatch)

    response = client.post(
        f"/api/storage/claims/{NAMESPACE}/{CLAIM}/snapshot/plan", json={"name": NAME},
    )

    assert response.status_code == 200
    codes = [e["code"] for e in response.json()["consequences"]]
    assert snapshot.WARN_NOT_A_BACKUP in codes


def test_the_write_endpoint_defaults_to_a_dry_run(client, cluster, monkeypatch):
    """§0.3: a client that forgets the field gets a projection, not a write."""
    stub_reads(monkeypatch, classes=[snapshot_class(policy="Retain", default=True)])
    stub_create(monkeypatch)

    response = client.post(
        f"/api/storage/claims/{NAMESPACE}/{CLAIM}/snapshot",
        json={"name": NAME, "acknowledgeConsequences": ALWAYS},
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False


def test_the_write_endpoint_refuses_a_bad_name_with_422(client, cluster, monkeypatch):
    stub_reads(monkeypatch)

    response = client.post(
        f"/api/storage/claims/{NAMESPACE}/{CLAIM}/snapshot",
        json={"name": "Not A Valid Name", "dryRun": False},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
