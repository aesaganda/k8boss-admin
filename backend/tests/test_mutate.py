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
* **A feature's own switch is the funnel's step one, not the feature's.** Five
  features once carried a copy of it. The tests below are what stops a sixth
  from carrying a sixth copy, and what makes the one difference that is real —
  which switch withholds the preview — comparable across all five in one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.admin import cli_pod
from app.admin import node_debug
from app.admin import portal as portal_admin
from app.admin import projects as projects_admin
from app.admin import router as router_admin
from app.admin.diff import digest
from app.admin.mutate import FeatureGate, Switch, mutate, read_only_switch, require_open
from app.audit import recorder
from app.config import settings
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
# The feature switches (§5.5, §14, §15, §16, §17)
# --------------------------------------------------------------------------- #
#
# The console-wide switch above is one of these; a feature's own is the rest.
# They are tested here, on the funnel, because that is now the only place any of
# them is enforced — the five features hand in a `FeatureGate` and get step one
# by construction.

def gate(*switches, **kwargs):
    return FeatureGate(feature="a test write", switches=switches, **kwargs)


OPEN = Switch("ADMIN_ALLOW_MUTATIONS", True, detail="writes are on")
SHUT = Switch("ADMIN_FEATURE_ENABLED", False, detail="the feature is off")
SHUT_AND_SILENT = Switch(
    "ADMIN_FEATURE_ENABLED", False, detail="the feature is off",
    withholds_dry_run=True,
)


def test_a_feature_switch_refuses_a_real_write_the_console_wide_one_would_allow(
    db_engine, fake_k8s, allow_mutations,
):
    """The whole point of a second switch: every other write works, this one does
    not, and the refusal names the setting that is off rather than an RBAC grant
    the operator already holds."""
    allow(fake_k8s)
    apply_fn = applier()

    with pytest.raises(MutationsDisabled) as caught:
        run(fake_k8s, apply_fn, dry_run=False,
            gate=gate(OPEN, SHUT, hint="Set ADMIN_FEATURE_ENABLED=true."))

    assert caught.value.code == "mutations_disabled"
    assert caught.value.detail == "the feature is off"
    assert "ADMIN_FEATURE_ENABLED" in caught.value.hint
    assert apply_fn.calls == [], "the cluster must not be touched"
    assert fake_k8s.authorization_v1.calls == [], "nor asked whether we may"


def test_a_closed_feature_switch_still_permits_the_dry_run(db_engine, fake_k8s,
                                                           allow_mutations):
    """§1.6's rule, and the default: inspecting what a switch would let the
    console write is a read, and an operator deciding whether to open it has to
    be able to see that."""
    allow(fake_k8s)
    apply_fn = applier()

    response = run(fake_k8s, apply_fn, dry_run=True, gate=gate(OPEN, SHUT))

    assert apply_fn.calls == [True]
    assert response["applied"] is False


def test_a_switch_that_withholds_the_projection_refuses_the_dry_run_too(
    db_engine, fake_k8s, allow_mutations,
):
    """§5.5 and §15's departure, and the only thing that distinguishes them from
    the other three: the projection *is* the sensitive thing — a working recipe
    for a privileged pod, the offer of a kubectl terminal — so previewing the
    feature is offering it."""
    allow(fake_k8s)
    apply_fn = applier()

    with pytest.raises(MutationsDisabled):
        run(fake_k8s, apply_fn, dry_run=True, gate=gate(OPEN, SHUT_AND_SILENT))

    assert apply_fn.calls == []
    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["dry_run"] is True, (
        "the refused preview is still an attempt, and the row that records it "
        "says it was a preview — a trail that recorded it as a real write would "
        "over-report what was tried"
    )


def test_the_refusal_names_the_first_closed_switch_not_the_last(db_engine, fake_k8s):
    """Both are off; "writes are off" is the one to change first, and it is a
    different conversation from "writes are on and this one thing is not"."""
    allow(fake_k8s)

    with pytest.raises(MutationsDisabled) as caught:
        run(fake_k8s, applier(), dry_run=False,
            gate=gate(Switch("ADMIN_ALLOW_MUTATIONS", False, detail="writes are off"), SHUT))

    assert caught.value.detail == "writes are off"


def test_a_refused_write_leaves_exactly_one_denial_row(db_engine, fake_k8s):
    """Two switches shut, one attempt, one row. Two rows for one attempt makes
    the count of "who tried" wrong in the one table that exists to answer it."""
    allow(fake_k8s)

    with pytest.raises(MutationsDisabled):
        run(fake_k8s, applier(), dry_run=False,
            gate=gate(Switch("ADMIN_ALLOW_MUTATIONS", False), SHUT))

    assert len(audit_rows()) == 1


def test_the_denial_row_carries_the_write_target_and_its_sentence(db_engine, fake_k8s):
    """What was attempted, not that something was. The row is assembled from the
    write's own target and detail, so the trail says which object somebody tried
    to touch on a console where that was switched off."""
    allow(fake_k8s)

    with pytest.raises(MutationsDisabled):
        run(fake_k8s, applier(), dry_run=False, detail="replicas 3 -> 5",
            gate=gate(SHUT))

    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["verb"] == "patch"
    assert row["target"]["resource"] == "deployments"
    assert row["target"]["namespace"] == "prod"
    assert row["target"]["name"] == "checkout"
    assert row["detail"] == "replicas 3 -> 5"
    assert "mutations_disabled" in row["error"]


def test_a_switch_reads_its_setting_when_the_gate_is_built(db_engine, monkeypatch):
    """Never at import time. A switch that remembered the value it saw when the
    module loaded would go on allowing writes the operator has since forbidden."""
    monkeypatch.setattr(settings, "admin_allow_mutations", False)
    assert read_only_switch().enabled is False

    monkeypatch.setattr(settings, "admin_allow_mutations", True)
    assert read_only_switch().enabled is True


def test_state_answers_may_i_write_not_may_i_preview(db_engine):
    """What the UI disables a button on. It names the first closed switch whether
    or not that switch would also withhold a preview — a control offered with no
    explanation is the state rule 11.4 exists to forbid."""
    assert gate(OPEN, SHUT).state() == {"enabled": False, "detail": "the feature is off"}
    assert gate(OPEN, OPEN, enabled_detail="all on").state() == {
        "enabled": True, "detail": "all on",
    }


def test_the_hoisted_gate_writes_the_row_the_funnel_would_have(db_engine, fake_k8s):
    """§14, §16 and §17 refuse before they read, because a refusal that waited
    for the first `mutate()` would arrive after a 404 about a package the caller
    was never going to be allowed to subscribe to — or, for a loop that reports
    each object, as one denial row and four skipped objects. `require_open` is
    the funnel's own step one called early, and this is what says so: the row and
    the error are the ones the funnel would have produced."""
    allow(fake_k8s)
    shut = gate(SHUT, message="Nope.", hint="Set ADMIN_FEATURE_ENABLED=true.")

    with pytest.raises(MutationsDisabled) as hoisted:
        require_open(
            shut, verb="patch", group="apps", version="v1", plural="deployments",
            namespace="prod", name="checkout", dry_run=False, detail="replicas 3 -> 5",
        )
    hoisted_row = audit_rows()[0]

    with pytest.raises(MutationsDisabled) as funnelled:
        run(fake_k8s, applier(), dry_run=False, detail="replicas 3 -> 5", gate=shut)
    funnelled_row = audit_rows()[0]

    for field in ("verb", "target", "outcome", "detail", "error", "dry_run"):
        assert hoisted_row[field] == funnelled_row[field], field
    assert hoisted.value.code == funnelled.value.code
    assert hoisted.value.message == funnelled.value.message
    assert hoisted.value.hint == funnelled.value.hint


# --------------------------------------------------------------------------- #
# The five features, compared in one place
# --------------------------------------------------------------------------- #

FEATURE_GATES = {
    "router install": (lambda: router_admin._gate("install"), "ADMIN_ROUTER_MANAGE_ENABLED"),
    "router uninstall": (lambda: router_admin._gate("uninstall"), "ADMIN_ROUTER_MANAGE_ENABLED"),
    "portal subscribe": (portal_admin._gate, "ADMIN_PORTAL_INSTALL_ENABLED"),
    "node debug pod": (node_debug._gate, "ADMIN_NODE_DEBUG_ENABLED"),
    "CLI pod": (cli_pod._gate, "ADMIN_CLI_ENABLED"),
    "project create": (projects_admin._gate, None),
}

#: The two features whose *projection* is the sensitive thing, and therefore the
#: only two whose switches withhold a dry run. Written out rather than derived,
#: because the point of this list is that adding a third is a decision somebody
#: made in this file rather than a default a new feature inherited.
WITHHOLDS_THE_PREVIEW = {"node debug pod", "CLI pod"}


@pytest.mark.parametrize("feature", sorted(FEATURE_GATES))
def test_every_feature_gate_starts_with_the_console_wide_switch(feature, db_engine):
    """`ADMIN_ALLOW_MUTATIONS` first, always. It is the one an operator has to
    change first, and a gate that named its own switch while writes were off
    would send them to the wrong line of the same file."""
    build, _ = FEATURE_GATES[feature]
    first = build().switches[0]

    assert first.setting == "ADMIN_ALLOW_MUTATIONS"
    assert first.detail, "and it says what a read-only console still offers of this feature"


@pytest.mark.parametrize("feature", sorted(FEATURE_GATES))
def test_every_feature_gate_names_its_own_setting_when_that_is_the_one_that_is_off(
    feature, db_engine, monkeypatch, allow_mutations,
):
    """Writes are on and this one thing is not: the sentence has to name the
    switch that is off, because it is the only one the operator has left to
    change."""
    build, setting = FEATURE_GATES[feature]
    if setting is None:
        assert build().state()["enabled"] is True, (
            "§17 has no switch of its own: with writes on, it is open"
        )
        return

    gate_now = build()
    field = {
        "ADMIN_ROUTER_MANAGE_ENABLED": "router_manage_enabled",
        "ADMIN_PORTAL_INSTALL_ENABLED": "portal_install_enabled",
        "ADMIN_NODE_DEBUG_ENABLED": "node_debug_enabled",
        "ADMIN_CLI_ENABLED": "cli_enabled",
    }[setting]
    assert [s.setting for s in gate_now.switches] == ["ADMIN_ALLOW_MUTATIONS", setting]

    monkeypatch.setattr(settings, field, False)
    state = build().state()
    assert state["enabled"] is False
    assert setting in state["detail"]
    assert setting in build().hint

    monkeypatch.setattr(settings, field, True)
    assert build().state()["enabled"] is True


@pytest.mark.parametrize("feature", sorted(FEATURE_GATES))
def test_only_the_two_features_whose_projection_is_the_secret_withhold_a_preview(
    feature, db_engine,
):
    """The one policy difference among the five, asserted for all five in one
    place rather than described in five docstrings that can disagree.

    A node debug pod's projected manifest is a working recipe for a privileged
    pod and a CLI pod's is the offer of a kubectl terminal, so a deployment that
    switched either off did not consent to the preview either. Everywhere else
    the projection is the operator's own request or a public upstream bundle,
    and §1.6's rule stands: reading what would change is a read."""
    build, _ = FEATURE_GATES[feature]
    withholds = any(s.withholds_dry_run for s in build().switches)

    assert withholds is (feature in WITHHOLDS_THE_PREVIEW)


def test_no_feature_builds_its_own_refusal_outside_the_funnel(db_engine):
    """The regression this fold exists to prevent.

    Five features once had their own copy of step one — build the error, write
    the denial row, log, raise — and the first evidence that a sixth had skipped
    the row would have been a hole in the trail, found by somebody asking who
    tried to install a router on a console where that was switched off. There is
    one constructor now, in the funnel, and this is what keeps it that way."""
    admin = Path(__file__).resolve().parent.parent / "app" / "admin"
    builders = sorted(
        path.name for path in admin.glob("*.py")
        if "MutationsDisabled(" in path.read_text()
    )

    assert builders == ["mutate.py"], (
        "a write that refuses on its own builds its own error, its own audit row "
        "and its own answer to whether a dry run is withheld; hand `mutate()` a "
        "FeatureGate instead"
    )


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
