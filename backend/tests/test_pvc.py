"""
Expanding a PersistentVolumeClaim (§20).

One field on one claim, through the funnel. What is worth testing is not that a
patch is sent — §4 covers that — but the four things this endpoint claims that a
YAML editor does not:

* **A green result is not more disk.** `applied: true` means the claim requests
  the new size. The volume grows when the provider grows it and the filesystem
  after that, and on a mounted volume often not until every pod restarts. That
  sentence is the difference between an operator believing a database has room
  and finding out it does not, so it is a consequence acknowledged on every
  expansion — including the ones that go on to work.

* **A shrink is refused by arithmetic, here.** Typing 5Gi where the claim says
  50Gi is one keystroke. It is refused before anything is sent, with the current
  size in the message, rather than relayed back from admission naming a field
  the operator did not think they were editing.

* **`allowVolumeExpansion` is tri-state.** `false` refuses the write. A class we
  could not read is `null`, and `null` never refuses: reading it as `false`
  would block a write the cluster would have accepted and send somebody to argue
  with a StorageClass that is already correct.

* **What has the volume mounted is `None`, never `[]`, when the pod listing
  failed.** "Nothing has this open" is the sentence that gets an offline resize
  started on a volume a database is using.
"""

from __future__ import annotations

import pytest

from app.admin import apply as apply_service
from app.admin import pvc
from app.audit import recorder
from app.errors import Conflict, Invalid, NotFound, RBACDenied
from app.resources import catalog
from tests.conftest import obj
from tests.test_routes import _groups_payload, _resources

NAMESPACE = "prod"
NAME = "postgres-data"


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


def claim(
    *,
    requested="50Gi",
    capacity="50Gi",
    phase="Bound",
    storage_class="gp3",
    conditions=(),
    resource_version="7710",
) -> dict:
    """A live PVC. ``capacity`` is `status`, ``requested`` is `spec` — not the same."""
    body: dict = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": NAME, "namespace": NAMESPACE, "resourceVersion": resource_version,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "volumeName": "pvc-9f2a",
            "resources": {"requests": {"storage": requested}} if requested else {},
        },
        "status": {"phase": phase, "conditions": list(conditions)},
    }
    if storage_class is not None:
        body["spec"]["storageClassName"] = storage_class
    if capacity is not None:
        body["status"]["capacity"] = {"storage": capacity}
    return body


def storage_class(*, name="gp3", expansion=True) -> dict:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": name},
        "provisioner": "ebs.csi.aws.com",
        "allowVolumeExpansion": expansion,
    }


def pod(name, *, claim_name=NAME):
    return {
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"volumes": [
            {"name": "config", "configMap": {"name": "settings"}},
            {"name": "data", "persistentVolumeClaim": {"claimName": claim_name}},
        ]},
    }


@pytest.fixture
def cluster(monkeypatch, fake_k8s):
    """Discovery for claims, an allowing preflight, and a patchable server."""
    core = _resources(("persistentvolumeclaims", "PersistentVolumeClaim"))

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return _groups_payload(("storage.k8s.io", "v1"))
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/api/v1":
            return core
        if path == "/apis/storage.k8s.io/v1":
            return _resources(("storageclasses", "StorageClass"))
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    return fake_k8s


def stub_reads(
    monkeypatch,
    *,
    live=None,
    claim_error=None,
    klass=None,
    class_error=None,
    pods=None,
    pods_error=None,
    pod_pages=None,
    pages_end=True,
):
    """The three reads §20 makes, each independently stubbable and failable.

    ``pod_pages`` serves the pod listing one page at a time, so a test can put a
    pod on page two: the single unpaged listing this module used to make
    reported everything behind the cursor as not mounting the claim. Same shape
    as §32's `endpoint_slice_pages`, including ``pages_end=False`` for the
    namespace that outruns the page budget.
    """

    def fake_get(group, version, plural, name, namespace=None):
        if plural == "persistentvolumeclaims":
            if claim_error is not None:
                raise claim_error
            return live if live is not None else claim()
        if plural == "storageclasses":
            if class_error is not None:
                raise class_error
            return klass if klass is not None else storage_class()
        raise AssertionError(f"unexpected get_resource for {plural}")

    def fake_list(group, version, plural, *, namespace=None, limit=500, cont=None,
                  **kwargs):
        assert plural == "pods", plural
        if pods_error is not None:
            raise pods_error
        if pod_pages is None:
            return {"items": list(pods or [])}
        # The cursor is the index of the page to serve. A page that is an
        # exception is what the API server raising mid-listing looks like.
        index = int(cont or 0)
        page = pod_pages[index % len(pod_pages)]
        if isinstance(page, Exception):
            raise page
        more = not pages_end or index + 1 < len(pod_pages)
        return {"items": list(page), "continue": str(index + 1) if more else None}

    monkeypatch.setattr(pvc.reader, "get_resource", fake_get)
    monkeypatch.setattr(pvc.reader, "list_resource", fake_list)


def stub_patch(monkeypatch, *, raises=None):
    """Record every PATCH and answer it the way the API server would."""
    calls: list[dict] = []

    def fake_request_json(method, path, **kwargs):
        calls.append({
            "method": method, "path": path,
            "query": dict(q for q in (kwargs.get("query") or []) if q[1] is not None),
            "body": kwargs.get("body"),
            "content_type": kwargs.get("content_type"),
        })
        if raises is not None:
            raise raises
        body = kwargs.get("body") or {}
        size = (
            body.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")
        )
        patched = claim()
        patched["spec"]["resources"]["requests"]["storage"] = size
        return patched, []

    monkeypatch.setattr(apply_service, "request_json", fake_request_json)
    return calls


def audit_rows():
    return recorder.query(limit=50)["items"]


ALWAYS = [pvc.WARN_NOT_IMMEDIATE, pvc.WARN_ONE_WAY]
ALL_CODES = ALWAYS + [
    pvc.WARN_EXPANSION_UNKNOWN, pvc.WARN_IN_USE,
    pvc.WARN_RESIZE_PENDING, pvc.WARN_MOUNTS_UNKNOWN,
]


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #

def test_a_size_is_required():
    with pytest.raises(Invalid) as caught:
        pvc.validate_request({})

    assert caught.value.context["parameter"] == "size"


@pytest.mark.parametrize("size", ["big", "20 GB", "", "Gi", "20Gib"])
def test_a_size_that_is_not_a_quantity_is_refused_rather_than_guessed(size):
    """A magnitude guessed from an unparseable string is a claim resized to
    something nobody asked for."""
    with pytest.raises(Invalid) as caught:
        pvc.validate_request({"size": size})

    assert caught.value.context["parameter"] == "size"


def test_the_size_reaches_the_cluster_as_the_string_that_was_typed():
    """Reformatting 20Gi as 21474836480 puts a number in the diff the operator
    has to convert back before they can confirm it, on the one screen where the
    number is the whole decision."""
    request = pvc.validate_request({"size": "20Gi"})

    assert request["size"] == "20Gi"
    assert request["size_bytes"] == 20 * 1024**3
    assert pvc.build_patch("20Gi", resource_version=None)["spec"]["resources"]["requests"] == {
        "storage": "20Gi",
    }


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(Invalid):
        pvc.validate_request({"size": "20Gi", "shrink": True})


# --------------------------------------------------------------------------- #
# The refusals arithmetic settles
# --------------------------------------------------------------------------- #

def test_a_shrink_is_refused_here_with_the_current_size_named(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch)
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "10Gi"}, dry_run=True,
                   acknowledge_consequences=ALL_CODES)

    assert "cannot be shrunk" in caught.value.message
    assert caught.value.context["currentRequested"] == "50Gi"
    # Nothing was sent: the point of refusing by arithmetic is that no request
    # for a smaller volume ever leaves this process.
    assert calls == []
    assert audit_rows() == []


def test_asking_for_the_size_it_already_has_is_refused_as_a_no_op(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch)
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "50Gi"}, dry_run=True,
                   acknowledge_consequences=ALL_CODES)

    assert "already requests 50Gi" in caught.value.message
    assert calls == []


def test_an_unbound_claim_has_no_volume_to_expand(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, live=claim(phase="Pending", capacity=None))
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                   acknowledge_consequences=ALL_CODES)

    assert caught.value.context["phase"] == "Pending"
    assert calls == []


def test_a_class_that_forbids_expansion_refuses_the_write_naming_it(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, klass=storage_class(expansion=False))
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                   acknowledge_consequences=ALL_CODES)

    assert "does not allow volume expansion" in caught.value.message
    assert caught.value.context["storageClass"] == "gp3"
    assert calls == []


def test_a_claim_whose_requested_size_is_unreadable_is_not_grown_blindly(
    cluster, monkeypatch, db_engine,
):
    """Without the current request this console cannot tell an expansion from a
    shrink, and it will not send a size it could not compare."""
    stub_reads(monkeypatch, live=claim(requested=None))
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid):
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                   acknowledge_consequences=ALL_CODES)

    assert calls == []


# --------------------------------------------------------------------------- #
# allowVolumeExpansion is tri-state
# --------------------------------------------------------------------------- #

def test_a_class_that_allows_expansion_is_supported(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch)
    unavailable: list = []

    assert pvc.expansion_support("gp3", unavailable)["supported"] is True
    assert unavailable == []


def test_a_refused_class_is_unknown_and_never_a_refusal(cluster, monkeypatch, db_engine):
    """`null` is not `false`. Reading it as false blocks a write the cluster
    would have accepted and sends somebody to fix a StorageClass that is fine."""
    stub_reads(monkeypatch, class_error=RBACDenied("storageclasses is forbidden"))
    unavailable: list = []

    support = pvc.expansion_support("gp3", unavailable)

    assert support["supported"] is None
    assert support["reason"] == "unreadable"
    assert unavailable and unavailable[0]["reason"] == "forbidden"


def test_a_deleted_class_is_unknown_too(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, class_error=NotFound("no such storageclass"))
    unavailable: list = []

    assert pvc.expansion_support("gp3", unavailable)["supported"] is None
    assert unavailable and unavailable[0]["reason"] == "not_found"


def test_a_claim_with_no_class_is_unknown_not_unsupported(cluster, monkeypatch, db_engine):
    """A statically provisioned volume may well be expandable. No API here says."""
    unavailable: list = []

    support = pvc.expansion_support(None, unavailable)

    assert support["supported"] is None
    assert support["reason"] == "no_storage_class"
    # Nothing was read, so nothing failed: this is not a degraded response.
    assert unavailable == []


def test_an_unknown_class_does_not_block_the_write(cluster, monkeypatch, db_engine):
    """Only `false` refuses. The API server decides the rest."""
    stub_reads(monkeypatch, class_error=RBACDenied("no"))
    calls = stub_patch(monkeypatch)

    result = pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                        acknowledge_consequences=ALL_CODES)

    assert result["expansion"]["supported"] is None
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# What has the volume mounted
# --------------------------------------------------------------------------- #

def test_the_pods_that_mount_the_claim_are_named(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, pods=[
        pod("postgres-0"),
        pod("unrelated-7f9c", claim_name="other-data"),
        pod("backup-runner"),
    ])

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})

    assert plan["mountedBy"] == ["backup-runner", "postgres-0"]


def test_a_refused_pod_listing_is_null_never_empty(cluster, monkeypatch, db_engine):
    """"Nothing has this open" is the sentence that gets an offline resize
    started on a volume a database is using."""
    stub_reads(monkeypatch, pods_error=RBACDenied("pods is forbidden"))

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})

    assert plan["mountedBy"] is None
    assert plan["partial"] is True
    codes = [entry["code"] for entry in plan["consequences"]]
    assert pvc.WARN_MOUNTS_UNKNOWN in codes
    assert pvc.WARN_IN_USE not in codes


def test_a_pod_on_the_second_page_of_the_listing_is_still_named(
    cluster, monkeypatch, db_engine,
):
    """One unpaged listing reported every pod behind the cursor as not mounting
    the claim, so the operator was told which workloads an expansion affects
    from a list that was silently short — and resized believing nothing else was
    attached."""
    stub_reads(monkeypatch, pod_pages=[[pod("api")], [pod("postgres-0")]])
    unavailable = []

    assert pvc.mounted_by(NAMESPACE, NAME, unavailable) == ["api", "postgres-0"]
    assert unavailable == []


def test_a_pod_listing_longer_than_the_budget_is_refused_rather_than_returned_short(
    cluster, monkeypatch, db_engine,
):
    """An unread page and a namespace where nothing has the claim open are the
    same list once it is returned, and only one of them is safe to resize on."""
    stub_reads(monkeypatch, pod_pages=[[]], pages_end=False)
    unavailable = []

    assert pvc.mounted_by(NAMESPACE, NAME, unavailable) is None
    assert [entry["reason"] for entry in unavailable] == ["timeout"]


def test_a_cursor_that_expired_mid_listing_is_raised_rather_than_swallowed(
    cluster, monkeypatch, db_engine,
):
    """`collect` records unavailability, not a bad request: a 410 means the
    listing this console built cannot be finished, and reporting it as a degraded
    column would leave a short mount list looking like a complete one."""
    stub_reads(monkeypatch, pod_pages=[
        [pod("api")],
        Invalid("The list cursor expired before the listing finished."),
    ])

    with pytest.raises(Invalid):
        pvc.mounted_by(NAMESPACE, NAME, [])


def test_a_claim_nothing_mounts_is_an_empty_list_and_no_restart_warning(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, pods=[pod("unrelated", claim_name="other")])

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})

    assert plan["mountedBy"] == []
    codes = [entry["code"] for entry in plan["consequences"]]
    assert pvc.WARN_IN_USE not in codes
    assert pvc.WARN_MOUNTS_UNKNOWN not in codes


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def test_every_expansion_carries_the_two_that_are_always_true(
    cluster, monkeypatch, db_engine,
):
    """Not friction for its own sake: these are the two things people are
    reliably wrong about, and being wrong about either costs an incident."""
    stub_reads(monkeypatch, pods=[])

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})
    codes = [entry["code"] for entry in plan["consequences"]]

    assert pvc.WARN_NOT_IMMEDIATE in codes
    assert pvc.WARN_ONE_WAY in codes


def test_a_mounted_volume_warns_that_the_filesystem_may_wait_for_a_restart(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, pods=[pod("postgres-0")])

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})
    entry = next(e for e in plan["consequences"] if e["code"] == pvc.WARN_IN_USE)

    assert "postgres-0" in entry["consequence"]
    assert "FileSystemResizePending" in entry["consequence"]


def test_an_expansion_already_in_flight_is_called_out(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, pods=[], live=claim(conditions=[
        {"type": "FileSystemResizePending", "status": "True",
         "reason": "Resizing", "message": "waiting for the pod to restart"},
    ]))

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})
    codes = [entry["code"] for entry in plan["consequences"]]

    assert pvc.WARN_RESIZE_PENDING in codes


def test_a_condition_that_is_false_is_not_an_expansion_in_flight(
    cluster, monkeypatch, db_engine,
):
    """A condition the controller wrote and then cleared says the resize
    finished, not that one is pending."""
    stub_reads(monkeypatch, pods=[], live=claim(conditions=[
        {"type": "Resizing", "status": "False", "reason": None, "message": None},
    ]))

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})
    codes = [entry["code"] for entry in plan["consequences"]]

    assert pvc.WARN_RESIZE_PENDING not in codes


def test_a_write_without_the_acknowledgements_is_refused_naming_them(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=False,
                   acknowledge_consequences=[])

    assert set(caught.value.context["unacknowledged"]) == set(ALWAYS)
    assert calls == []
    assert audit_rows() == []


def test_acknowledgements_are_recomputed_against_the_claim_not_trusted(
    cluster, monkeypatch, db_engine,
):
    """A caller that could acknowledge a code the server did not derive could
    acknowledge every code it liked — including the one about the pods that have
    the volume open."""
    stub_reads(monkeypatch, pods=[pod("postgres-0")])
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=False,
                   acknowledge_consequences=ALWAYS)

    assert caught.value.context["unacknowledged"] == [pvc.WARN_IN_USE]
    assert calls == []


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def test_the_plan_reports_a_refusal_as_data_with_the_claim_still_described(
    cluster, monkeypatch, db_engine,
):
    """The plan is the screen where the size is decided. One that answered a
    too-small number with an error alone would withhold the current size, the
    capacity and the mounts at the moment those are the three facts needed."""
    stub_reads(monkeypatch, pods=[pod("postgres-0")])

    plan = pvc.plan(NAMESPACE, NAME, {"size": "10Gi"})

    assert plan["blocked"]["message"].startswith("A persistent volume claim cannot be shrunk")
    assert plan["blocked"]["context"]["currentRequested"] == "50Gi"
    assert plan["current"]["requested"] == "50Gi"
    assert plan["current"]["capacity"] == "50Gi"
    assert plan["mountedBy"] == ["postgres-0"]
    # Never both: a blocked plan has no consequences to accept.
    assert plan["consequences"] == []


def test_a_plan_that_is_not_blocked_has_no_blocked_entry(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, pods=[])

    plan = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})

    assert plan["blocked"] is None
    assert plan["consequences"]


def test_the_plan_separates_what_is_asked_for_from_what_the_volume_provides(
    cluster, monkeypatch, db_engine,
):
    """A claim mid-expansion requests one number and provides another, and a
    page showing only one of them cannot say so."""
    stub_reads(monkeypatch, pods=[], live=claim(requested="100Gi", capacity="50Gi"))

    plan = pvc.plan(NAMESPACE, NAME, {"size": "200Gi"})

    assert plan["current"]["requested_bytes"] == 100 * 1024**3
    assert plan["current"]["capacity_bytes"] == 50 * 1024**3


def test_the_plan_writes_nothing_and_audits_nothing(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)

    pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})

    assert calls == []
    assert audit_rows() == []


def test_the_plan_carries_the_gate_so_the_dialog_can_disable_with_the_reason(
    cluster, monkeypatch, db_engine, allow_mutations
):
    stub_reads(monkeypatch, pods=[])

    gate = pvc.plan(NAMESPACE, NAME, {"size": "100Gi"})["gate"]

    assert gate["enabled"] is True


# --------------------------------------------------------------------------- #
# The write, through the funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_sends_dryrun_all_and_changes_nothing(cluster, monkeypatch, db_engine):
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)

    result = pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                        acknowledge_consequences=ALWAYS)

    (call,) = calls
    assert call["method"] == "PATCH"
    assert call["query"]["dryRun"] == "All"
    assert call["content_type"] == "application/merge-patch+json"
    assert result["applied"] is False
    assert result["diff"]["unified"]
    (row,) = audit_rows()
    assert row["outcome"] == "dry_run"


def test_a_confirmed_write_patches_one_field_and_audits_both_sizes(
    cluster, monkeypatch, db_engine, allow_mutations
):
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)

    result = pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=False,
                        acknowledge_consequences=ALWAYS)

    (call,) = calls
    assert "dryRun" not in call["query"]
    assert call["body"]["spec"] == {"resources": {"requests": {"storage": "100Gi"}}}
    assert result["applied"] is True
    (row,) = audit_rows()
    assert row["outcome"] == "applied"
    # Both sizes in the sentence: the question after an incident is what the
    # claim was before somebody grew it.
    assert row["detail"] == f"expand pvc {NAMESPACE}/{NAME}: 50Gi -> 100Gi"


def test_applied_true_reports_the_capacity_it_did_not_change(
    cluster, monkeypatch, db_engine, allow_mutations
):
    """The one claim this endpoint must never make. `applied` says the request
    changed; `current.capacity` is what the workload has, read from before."""
    stub_reads(monkeypatch, pods=[], live=claim(requested="50Gi", capacity="50Gi"))
    stub_patch(monkeypatch)

    result = pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=False,
                        acknowledge_consequences=ALWAYS)

    assert result["applied"] is True
    assert result["requested"]["size"] == "100Gi"
    assert result["current"]["capacity"] == "50Gi"
    codes = [entry["code"] for entry in result["consequences"]]
    assert pvc.WARN_NOT_IMMEDIATE in codes


def test_the_resource_version_rides_inside_the_patch(cluster, monkeypatch, db_engine, allow_mutations):
    """Rule 4 enforced by the API server too: a patch carrying a stale version is
    rejected rather than silently overwriting whoever changed the claim."""
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)

    pvc.expand(NAMESPACE, NAME, {"size": "100Gi", "resourceVersion": "7710"},
               dry_run=False, acknowledge_consequences=ALWAYS)

    assert calls[0]["body"]["metadata"]["resourceVersion"] == "7710"


def test_a_stale_resource_version_is_a_conflict_carrying_the_live_size(
    cluster, monkeypatch, db_engine,
):
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)

    with pytest.raises(Conflict) as caught:
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi", "resourceVersion": "6000"},
                   dry_run=False, acknowledge_consequences=ALWAYS)

    assert caught.value.context["currentResourceVersion"] == "7710"
    assert caught.value.context["currentSize"] == "50Gi"
    assert caught.value.context["currentCapacity"] == "50Gi"
    assert calls == []


def test_a_conflict_that_never_reaches_the_funnel_is_still_in_the_trail(
    cluster, monkeypatch, db_engine,
):
    """Rule 5 covers the writes that conflicted, and this refusal fires before
    `mutate()` — so nothing else would record that two people were resizing the
    same claim at once, which is what rule 4 exists to make answerable."""
    stub_reads(monkeypatch, pods=[])
    stub_patch(monkeypatch)

    with pytest.raises(Conflict):
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi", "resourceVersion": "6000"},
                   dry_run=False, acknowledge_consequences=ALWAYS)

    (row,) = audit_rows()
    assert row["outcome"] == "conflict"
    assert row["target"]["name"] == NAME
    assert row["error"].startswith("conflict:")
    assert "6000" in row["detail"] and "7710" in row["detail"]


def test_the_preflight_asks_for_patch_on_claims_in_this_namespace(
    cluster, monkeypatch, db_engine, fake_k8s, allow_mutations
):
    stub_reads(monkeypatch, pods=[])
    stub_patch(monkeypatch)

    pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
               acknowledge_consequences=ALWAYS)

    ((args, _kwargs),) = fake_k8s.authorization_v1.called(
        "create_self_subject_access_review"
    )
    attributes = args[0].spec.resource_attributes
    assert attributes.verb == "patch"
    assert attributes.resource == "persistentvolumeclaims"
    assert attributes.namespace == NAMESPACE
    assert attributes.name == NAME


def test_a_denied_preflight_records_the_denial_and_sends_nothing(
    cluster, monkeypatch, db_engine, fake_k8s,
):
    stub_reads(monkeypatch, pods=[])
    calls = stub_patch(monkeypatch)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no patch on persistentvolumeclaims",
        evaluation_error=None, denied=True,
    )))

    with pytest.raises(RBACDenied):
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                   acknowledge_consequences=ALWAYS)

    assert calls == []
    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["target"]["resource"] == "persistentvolumeclaims"


def test_a_claim_that_could_not_be_read_is_not_expanded_against_a_default(
    cluster, monkeypatch, db_engine,
):
    """A change described against a claim we could not read is a diff about
    nothing, and the size on screen would be the one typed rather than the one
    being changed."""
    stub_reads(monkeypatch, claim_error=RBACDenied("persistentvolumeclaims is forbidden"))
    calls = stub_patch(monkeypatch)

    with pytest.raises(RBACDenied):
        pvc.expand(NAMESPACE, NAME, {"size": "100Gi"}, dry_run=True,
                   acknowledge_consequences=ALL_CODES)

    assert calls == []


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

def test_the_plan_endpoint_answers_a_blocked_size_with_200(client, cluster, monkeypatch):
    stub_reads(monkeypatch, pods=[])

    response = client.post(
        f"/api/storage/claims/{NAMESPACE}/{NAME}/expand/plan", json={"size": "1Gi"},
    )

    assert response.status_code == 200
    assert response.json()["blocked"] is not None


def test_the_write_endpoint_defaults_to_a_dry_run(client, cluster, monkeypatch):
    """§0.3: a client that forgets the field gets a projection, not a write."""
    stub_reads(monkeypatch, pods=[])
    stub_patch(monkeypatch)

    response = client.put(
        f"/api/storage/claims/{NAMESPACE}/{NAME}/size",
        json={"size": "100Gi", "acknowledgeConsequences": ALWAYS},
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False


def test_the_write_endpoint_refuses_a_shrink_with_422(client, cluster, monkeypatch):
    stub_reads(monkeypatch, pods=[])

    response = client.put(
        f"/api/storage/claims/{NAMESPACE}/{NAME}/size",
        json={"size": "1Gi", "dryRun": False, "acknowledgeConsequences": ALL_CODES},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
