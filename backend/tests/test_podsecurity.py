"""
Pod Security set-with-preview (§18).

Six labels on one Namespace, through the funnel. What is worth testing is not
that a patch is sent — §4 covers that — but the three things this endpoint
claims that a YAML editor does not:

* **The API server's admission warnings reach the operator verbatim.** They are
  the feature: a preview of "enforce restricted" comes back naming the pods
  already running that do not meet it. This console does not compute that list
  and does not parse it, so what is asserted is that it is passed through whole,
  on the preview and on the real write alike.

* **Removing a label is not setting `privileged`, and the request can say
  which.** ``None`` removes the declaration and hands the namespace back to a
  cluster default this console cannot read; ``privileged`` declares that
  everything is admitted. The merge patch expresses the first as JSON ``null``,
  and an operator who means it acknowledges a consequence that says the console
  cannot tell them what will apply afterwards.

* **Nothing is evicted.** Raising the level touches no running pod. That is the
  sentence between "enforce: restricted" and an operator believing the
  workloads in front of them are now restricted, and it is a consequence they
  name rather than a paragraph in a doc.
"""

from __future__ import annotations

import pytest

from app.admin import apply as apply_service
from app.admin import podsecurity
from app.audit import recorder
from app.config import settings
from app.errors import Conflict, Invalid, MutationsDisabled, NotFound, RBACDenied
from app.resources import catalog
from tests.conftest import obj
from tests.test_routes import _groups_payload, _resources

NAME = "payments"

#: What Pod Security admission actually returns when a namespace's enforce label
#: is raised over pods that do not meet it — one line naming the namespace and
#: the level, then one line per offending pod naming the fields. Verbatim in
#: shape, because the thing under test is that this console does not touch it.
ADMISSION_WARNINGS = [
    'existing pods in namespace "payments" violate the new PodSecurity enforce '
    'level "restricted:latest"',
    "payments-api-7f9c: allowPrivilegeEscalation != false, unrestricted "
    "capabilities, runAsNonRoot != true, seccompProfile",
    "legacy-batch-2xk: host namespaces, hostPath volumes",
]


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


def namespace(**labels) -> dict:
    """A live Namespace carrying the labels given, at resourceVersion 4021."""
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAME,
            "resourceVersion": "4021",
            "labels": {"kubernetes.io/metadata.name": NAME, **labels},
        },
        "status": {"phase": "Active"},
    }


def psa_labels(**modes) -> dict:
    """``enforce="baseline"`` → the label a namespace would carry for it."""
    return {f"pod-security.kubernetes.io/{k.replace('_', '-')}": v for k, v in modes.items()}


@pytest.fixture
def cluster(monkeypatch, fake_k8s):
    """Discovery for namespaces, an allowing preflight, and a patchable server."""
    core = _resources()
    core["resources"].append({
        "name": "namespaces", "kind": "Namespace", "namespaced": False,
        "verbs": ["get", "list", "create", "update", "patch", "delete"],
    })

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return _groups_payload()
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/api/v1":
            return core
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    return fake_k8s


def stub_read(monkeypatch, *, live=None, error=None):
    def fake_get(group, version, plural, name, namespace=None):
        assert plural == "namespaces", plural
        if error is not None:
            raise error
        return live if live is not None else globals()["namespace"]()

    monkeypatch.setattr(podsecurity.reader, "get_resource", fake_get)


def stub_patch(monkeypatch, *, warnings=(), raises=None):
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
        patched = namespace()
        patched["metadata"]["labels"] = {
            **patched["metadata"]["labels"],
            **{k: v for k, v in (body.get("metadata", {}).get("labels") or {}).items()
               if v is not None},
        }
        for key, value in (body.get("metadata", {}).get("labels") or {}).items():
            if value is None:
                patched["metadata"]["labels"].pop(key, None)
        return patched, list(warnings)

    monkeypatch.setattr(apply_service, "request_json", fake_request_json)
    return calls


def audit_rows():
    return recorder.query(limit=50)["items"]


def request_body(**modes):
    """A §18 body naming every mode, which is what the endpoint requires."""
    full = {mode: None for mode in ("enforce", "audit", "warn")}
    full.update({mode + "Version": None for mode in ("enforce", "audit", "warn")})
    full.update(modes)
    return {"podSecurity": full}


ALL_CODES = [
    podsecurity.WARN_DOES_NOT_EVICT,
    podsecurity.WARN_ENFORCEMENT_REMOVED,
    podsecurity.WARN_LOWERED,
    podsecurity.WARN_NO_WARN_LABEL,
    podsecurity.WARN_VERSION_PINNED,
]


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #

def test_every_mode_must_be_named_because_absent_means_remove():
    """A body whose omitted field could mean "leave it" or "remove it" is a body
    that eventually strips somebody's audit level from a blank form field."""
    with pytest.raises(Invalid) as caught:
        podsecurity.validate_request({})

    assert caught.value.context["parameter"] == "podSecurity"
    assert "leave out is removed" in (caught.value.hint or "")


@pytest.mark.parametrize("level", ["Restricted", "priviledged", "none", "strict"])
def test_a_level_outside_the_three_is_refused_naming_them(level):
    with pytest.raises(Invalid) as caught:
        podsecurity.validate_request(request_body(enforce=level))

    assert caught.value.context["parameter"] == "podSecurity.enforce"
    assert "privileged, baseline, restricted" in (caught.value.hint or "")


def test_a_version_without_its_level_is_refused_because_admission_ignores_it():
    with pytest.raises(Invalid) as caught:
        podsecurity.validate_request(request_body(enforce=None, enforceVersion="v1.31"))

    assert caught.value.context["parameter"] == "podSecurity.enforceVersion"
    assert "ignored by admission" in (caught.value.hint or "")


@pytest.mark.parametrize("version", ["1.31", "v1", "v1.31.2", "vNext", "latest "])
def test_a_version_that_is_not_latest_or_a_minor_is_refused(version):
    body = request_body(enforce="baseline", enforceVersion=version)
    if version.strip() == "latest":
        assert podsecurity.validate_request(body)["podSecurity"]["enforceVersion"] == "latest"
        return
    with pytest.raises(Invalid) as caught:
        podsecurity.validate_request(body)
    assert caught.value.context["parameter"] == "podSecurity.enforceVersion"


def test_an_unknown_mode_is_refused_rather_than_silently_dropped():
    """`podSecurity: {enforc: "restricted"}` must not read as "remove enforce"."""
    with pytest.raises(Invalid) as caught:
        podsecurity.validate_request({"podSecurity": {"enforc": "restricted"}})

    assert "enforc" in caught.value.message
    assert "enforce, audit, warn" in (caught.value.hint or "")


# --------------------------------------------------------------------------- #
# The patch
# --------------------------------------------------------------------------- #

def test_the_patch_names_all_six_labels_including_the_ones_not_changing():
    """A patch carrying only what this module decided was different would make
    the diff depend on that decision rather than on what was asked for."""
    request = podsecurity.validate_request(
        request_body(enforce="restricted", enforceVersion="latest")
    )
    patch = podsecurity.build_patch(request["podSecurity"], resource_version=None)

    labels = patch["metadata"]["labels"]
    assert sorted(labels) == [
        "pod-security.kubernetes.io/audit",
        "pod-security.kubernetes.io/audit-version",
        "pod-security.kubernetes.io/enforce",
        "pod-security.kubernetes.io/enforce-version",
        "pod-security.kubernetes.io/warn",
        "pod-security.kubernetes.io/warn-version",
    ]


def test_removing_a_label_is_json_null_which_is_what_a_merge_patch_removes_with():
    request = podsecurity.validate_request(request_body(enforce="baseline"))
    labels = podsecurity.build_patch(
        request["podSecurity"], resource_version=None
    )["metadata"]["labels"]

    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/audit"] is None, (
        "None must survive into the patch as null: dropping the key would leave "
        "the label in place and the operator would be told it was removed"
    )


def test_the_resource_version_rides_inside_the_patch(cluster, monkeypatch, db_engine,
                                                     allow_mutations):
    """Rule 4 enforced by the API server, not only locally: the submitted patch
    carries the caller's version, so it loses the race it should lose."""
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    podsecurity.set_level(
        NAME, {**request_body(enforce="privileged"), "resourceVersion": "4021"},
        dry_run=True, acknowledge_consequences=ALL_CODES,
    )

    assert calls[0]["body"]["metadata"]["resourceVersion"] == "4021"
    assert calls[0]["content_type"] == "application/merge-patch+json", (
        "a strategic merge patch does not remove a map key on null"
    )


# --------------------------------------------------------------------------- #
# The admission warnings — the reason this endpoint exists
# --------------------------------------------------------------------------- #

def test_the_dry_run_carries_the_api_servers_own_violating_pods_verbatim(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """The feature. Admission evaluates the pods already in the namespace when
    the labels change and reports them as Warning headers, on dryRun=All exactly
    as on a real write."""
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch, warnings=ADMISSION_WARNINGS)

    result = podsecurity.set_level(
        NAME, request_body(enforce="restricted", warn="restricted"),
        dry_run=True, acknowledge_consequences=ALL_CODES,
    )

    assert calls[0]["query"] == {"dryRun": "All"}
    assert result["applied"] is False
    assert result["admissionWarnings"] == ADMISSION_WARNINGS, (
        "verbatim: a summary of somebody else's admission decision is a summary "
        "that can be wrong about which pods are affected"
    )
    assert result["warnings"] == ADMISSION_WARNINGS, "§1.5's own key carries them too"
    assert "payments-api-7f9c" in result["admissionWarnings"][1]


def test_a_preview_with_no_warnings_is_an_empty_list_not_a_claim_of_safety(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """Empty means admission returned nothing, which is what it does when no pod
    in the namespace violates the level. It is not this console concluding so."""
    stub_read(monkeypatch)
    stub_patch(monkeypatch, warnings=[])

    result = podsecurity.set_level(
        NAME, request_body(enforce="baseline", warn="baseline"),
        dry_run=True, acknowledge_consequences=ALL_CODES,
    )

    assert result["admissionWarnings"] == []


def test_the_real_write_reports_the_warnings_too(cluster, monkeypatch, db_engine,
                                                 allow_mutations):
    """The same headers come back on the write. An operator who confirmed
    without reading the preview still gets told what admission found."""
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch, warnings=ADMISSION_WARNINGS)

    result = podsecurity.set_level(
        NAME, request_body(enforce="restricted", warn="restricted"),
        dry_run=False, acknowledge_consequences=ALL_CODES,
    )

    assert calls[0]["query"] == {}
    assert result["applied"] is True
    assert result["admissionWarnings"] == ADMISSION_WARNINGS


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def test_raising_the_level_says_nothing_running_is_evicted():
    """The sentence between "enforce: restricted" and an operator believing the
    workloads in front of them are now restricted."""
    current = podsecurity.pod_security_row({})
    codes = {c["code"] for c in podsecurity.consequences_for(
        current, podsecurity.validate_request(
            request_body(enforce="restricted", warn="restricted")
        )["podSecurity"],
    )}

    assert podsecurity.WARN_DOES_NOT_EVICT in codes
    entry = next(c for c in podsecurity.consequences_for(
        current, podsecurity.validate_request(
            request_body(enforce="restricted", warn="restricted")
        )["podSecurity"],
    ) if c["code"] == podsecurity.WARN_DOES_NOT_EVICT)
    assert "evicted" in entry["consequence"]
    assert "FailedCreate" in entry["consequence"]


def test_removing_the_enforce_label_says_it_is_not_the_same_as_privileged():
    """The cluster default lives in a file no API serves, so "unset" is a state
    this console cannot describe — and says so rather than implying privileged."""
    current = podsecurity.pod_security_row(psa_labels(enforce="restricted"))
    entries = podsecurity.consequences_for(
        current, podsecurity.validate_request(request_body())["podSecurity"]
    )
    entry = next(c for c in entries if c["code"] == podsecurity.WARN_ENFORCEMENT_REMOVED)

    assert "not mean everything is admitted" in entry["consequence"]
    assert "cannot tell you" in entry["consequence"]
    assert podsecurity.WARN_DOES_NOT_EVICT not in {c["code"] for c in entries}, (
        "nothing is being enforced, so there is nothing not to evict"
    )


def test_lowering_the_level_says_what_becomes_deployable():
    current = podsecurity.pod_security_row(psa_labels(enforce="restricted"))
    entries = podsecurity.consequences_for(
        current, podsecurity.validate_request(request_body(enforce="baseline"))["podSecurity"]
    )
    entry = next(c for c in entries if c["code"] == podsecurity.WARN_LOWERED)

    assert "restricted down to baseline" in entry["consequence"]


def test_raising_the_level_is_not_reported_as_lowering_it():
    current = podsecurity.pod_security_row(psa_labels(enforce="baseline"))
    codes = {c["code"] for c in podsecurity.consequences_for(
        current, podsecurity.validate_request(
            request_body(enforce="restricted", warn="restricted")
        )["podSecurity"],
    )}

    assert podsecurity.WARN_LOWERED not in codes


def test_a_level_outside_the_three_is_neither_raised_nor_lowered():
    """A namespace labelled with something admission does not recognise has
    whatever admission makes of it. Calling that weaker or stronger than the
    level being set would be this console inventing an ordering."""
    current = podsecurity.pod_security_row(psa_labels(enforce="hardened"))
    codes = {c["code"] for c in podsecurity.consequences_for(
        current, podsecurity.validate_request(
            request_body(enforce="baseline", warn="baseline")
        )["podSecurity"],
    )}

    assert podsecurity.WARN_LOWERED not in codes
    assert podsecurity.WARN_DOES_NOT_EVICT in codes, "the level is still changing"


def test_enforcing_without_a_warn_label_says_violations_go_unreported():
    codes = {c["code"] for c in podsecurity.consequences_for(
        podsecurity.pod_security_row({}),
        podsecurity.validate_request(request_body(enforce="restricted"))["podSecurity"],
    )}

    assert podsecurity.WARN_NO_WARN_LABEL in codes


def test_a_pinned_version_says_the_policy_stops_tightening():
    entries = podsecurity.consequences_for(
        podsecurity.pod_security_row({}),
        podsecurity.validate_request(
            request_body(enforce="restricted", enforceVersion="v1.31", warn="restricted")
        )["podSecurity"],
    )
    entry = next(c for c in entries if c["code"] == podsecurity.WARN_VERSION_PINNED)

    assert "enforce" in entry["label"] or "enforce" in entry["consequence"]
    assert "upgraded" in entry["consequence"]


def test_latest_is_not_a_pin():
    codes = {c["code"] for c in podsecurity.consequences_for(
        podsecurity.pod_security_row({}),
        podsecurity.validate_request(
            request_body(enforce="restricted", enforceVersion="latest", warn="restricted")
        )["podSecurity"],
    )}

    assert podsecurity.WARN_VERSION_PINNED not in codes


def test_a_write_with_unacknowledged_consequences_is_refused_naming_every_code(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    with pytest.raises(Invalid) as caught:
        podsecurity.set_level(
            NAME, request_body(enforce="restricted"), dry_run=False,
            acknowledge_consequences=[podsecurity.WARN_DOES_NOT_EVICT],
        )

    unacknowledged = caught.value.context["unacknowledged"]
    assert podsecurity.WARN_NO_WARN_LABEL in unacknowledged
    assert podsecurity.WARN_DOES_NOT_EVICT not in unacknowledged
    assert calls == [], "refused before the cluster was touched"


def test_the_consequences_are_recomputed_against_the_namespace_at_write_time(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """Not trusted from the plan. Somebody else lowering the level between the
    preview and the confirm changes what this write means, and the operator has
    not agreed to the new meaning."""
    stub_read(monkeypatch, live=namespace(**psa_labels(enforce="restricted")))
    stub_patch(monkeypatch)

    # Acknowledging what a plan against an unlabelled namespace would have said.
    with pytest.raises(Invalid) as caught:
        podsecurity.set_level(
            NAME, request_body(enforce="baseline", warn="baseline"), dry_run=False,
            acknowledge_consequences=[podsecurity.WARN_DOES_NOT_EVICT],
        )

    assert podsecurity.WARN_LOWERED in caught.value.context["unacknowledged"]


# --------------------------------------------------------------------------- #
# The funnel's five properties
# --------------------------------------------------------------------------- #

def test_a_real_write_is_refused_read_only_and_audited(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)

    with pytest.raises(MutationsDisabled):
        podsecurity.set_level(
            NAME, request_body(enforce="privileged"), dry_run=False,
            acknowledge_consequences=ALL_CODES,
        )

    (row,) = audit_rows()
    assert row["outcome"] == "denied"
    assert row["target"]["resource"] == "namespaces"
    assert calls == []


def test_a_read_only_console_still_previews_because_the_warnings_are_the_point(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch)
    stub_patch(monkeypatch, warnings=ADMISSION_WARNINGS)

    result = podsecurity.set_level(
        NAME, request_body(enforce="restricted", warn="restricted"), dry_run=True,
        acknowledge_consequences=ALL_CODES,
    )

    assert result["admissionWarnings"] == ADMISSION_WARNINGS


def test_a_denied_preflight_never_reaches_the_cluster(cluster, monkeypatch, db_engine,
                                                      allow_mutations):
    stub_read(monkeypatch)
    calls = stub_patch(monkeypatch)
    cluster.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no RBAC policy matched", evaluation_error=None, denied=True,
    )))

    with pytest.raises(RBACDenied) as caught:
        podsecurity.set_level(
            NAME, request_body(enforce="privileged"), dry_run=True,
            acknowledge_consequences=ALL_CODES,
        )

    assert caught.value.context["verb"] == "patch"
    assert caught.value.context["resource"] == "namespaces"
    assert calls == []


def test_the_diff_is_the_api_servers_own_projection(cluster, monkeypatch, db_engine,
                                                    allow_mutations):
    stub_read(monkeypatch, live=namespace(**psa_labels(enforce="baseline")))
    stub_patch(monkeypatch)

    result = podsecurity.set_level(
        NAME, request_body(enforce="restricted", warn="restricted"), dry_run=True,
        acknowledge_consequences=ALL_CODES,
    )

    assert result["diff"]["changed"] is True
    unified = result["diff"]["unified"]
    assert "-    pod-security.kubernetes.io/enforce: baseline" in unified
    assert "+    pod-security.kubernetes.io/enforce: restricted" in unified


def test_the_audit_sentence_names_the_level_it_moved_from_and_to(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """The question after an incident is what this namespace was enforcing
    before somebody deployed into it, and a diff digest cannot answer that."""
    stub_read(monkeypatch, live=namespace(**psa_labels(enforce="baseline")))
    stub_patch(monkeypatch)

    podsecurity.set_level(
        NAME, request_body(enforce="restricted", warn="restricted"), dry_run=False,
        acknowledge_consequences=ALL_CODES,
    )

    (row,) = audit_rows()
    assert row["outcome"] == "applied"
    assert row["detail"] == "pod security payments: enforce baseline -> restricted"
    assert row["diff_digest"]


def test_a_stale_resource_version_is_a_conflict_carrying_the_live_one(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """Checked locally as well as by the API server, because the local check is
    what can hand back the level as it is now rather than only a 409."""
    stub_read(monkeypatch, live=namespace(**psa_labels(enforce="restricted")))
    calls = stub_patch(monkeypatch)

    with pytest.raises(Conflict) as caught:
        podsecurity.set_level(
            NAME, {**request_body(enforce="privileged"), "resourceVersion": "3000"},
            dry_run=False, acknowledge_consequences=ALL_CODES,
        )

    assert caught.value.context["currentResourceVersion"] == "4021"
    assert caught.value.context["currentPodSecurity"]["enforce"] == "restricted"
    assert calls == []


def test_a_namespace_read_that_did_not_answer_stops_the_write(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    """Not "it has no labels": that renders as a diff removing labels this
    console never saw, and confirming it would strip somebody's audit level."""
    stub_read(monkeypatch, error=RBACDenied("namespaces is forbidden"))
    calls = stub_patch(monkeypatch)

    with pytest.raises(RBACDenied):
        podsecurity.set_level(
            NAME, request_body(enforce="restricted"), dry_run=True,
            acknowledge_consequences=ALL_CODES,
        )

    assert calls == []


def test_a_namespace_that_does_not_exist_is_a_404_not_a_create(
    cluster, monkeypatch, db_engine, allow_mutations,
):
    stub_read(monkeypatch, error=NotFound("namespaces/payments not found"))
    calls = stub_patch(monkeypatch)

    with pytest.raises(NotFound):
        podsecurity.set_level(
            NAME, request_body(enforce="restricted"), dry_run=True,
            acknowledge_consequences=ALL_CODES,
        )

    assert calls == []


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def test_the_plan_reads_the_namespace_and_writes_nothing(cluster, monkeypatch, db_engine):
    stub_read(monkeypatch, live=namespace(**psa_labels(enforce="baseline")))
    calls = stub_patch(monkeypatch)

    result = podsecurity.plan(NAME, request_body(enforce="restricted", warn="restricted"))

    assert result["current"]["enforce"] == "baseline"
    assert result["requested"]["enforce"] == "restricted"
    assert result["resourceVersion"] == "4021"
    assert result["changed"] is True
    assert podsecurity.WARN_DOES_NOT_EVICT in {c["code"] for c in result["consequences"]}
    assert calls == [], "a plan is a read"
    assert audit_rows() == []


def test_the_plan_reports_a_request_that_changes_nothing(cluster, monkeypatch, db_engine):
    """A no-op is worth saying out loud: the operator opened the dialog, changed
    nothing, and a preview that showed an empty diff without saying so reads as
    a preview that failed."""
    live = namespace(**psa_labels(enforce="baseline", warn="baseline"))
    stub_read(monkeypatch, live=live)

    result = podsecurity.plan(NAME, request_body(enforce="baseline", warn="baseline"))

    assert result["changed"] is False
    assert result["consequences"] == []


def test_the_plan_carries_the_gate_so_the_button_disables_with_the_reason(
    cluster, monkeypatch, db_engine,
):
    stub_read(monkeypatch)
    result = podsecurity.plan(NAME, request_body(enforce="baseline", warn="baseline"))

    assert result["gate"]["enabled"] is False
    assert "ADMIN_ALLOW_MUTATIONS" in result["gate"]["detail"]

    monkeypatch.setattr(settings, "admin_allow_mutations", True)
    assert podsecurity.plan(NAME, request_body())["gate"]["enabled"] is True


# --------------------------------------------------------------------------- #
# Through the API
# --------------------------------------------------------------------------- #

def test_the_endpoint_returns_the_warnings_and_the_consequences(
    client, cluster, monkeypatch, allow_mutations,
):
    stub_read(monkeypatch)
    stub_patch(monkeypatch, warnings=ADMISSION_WARNINGS)

    response = client.put(
        f"/api/projects/{NAME}/pod-security",
        json={
            **request_body(enforce="restricted", warn="restricted"),
            "dryRun": True,
            "acknowledgeConsequences": ALL_CODES,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is False
    assert body["admissionWarnings"] == ADMISSION_WARNINGS
    assert body["current"]["enforce"] is None
    assert body["requested"]["enforce"] == "restricted"


def test_the_endpoint_refuses_an_unacknowledged_write_as_422_invalid(
    client, cluster, monkeypatch, allow_mutations,
):
    stub_read(monkeypatch)
    stub_patch(monkeypatch)

    response = client.put(
        f"/api/projects/{NAME}/pod-security",
        json={**request_body(enforce="restricted"), "dryRun": False},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"


def test_the_plan_endpoint_answers_on_a_read_only_console(client, cluster, monkeypatch):
    stub_read(monkeypatch)

    response = client.post(
        f"/api/projects/{NAME}/pod-security/plan",
        json=request_body(enforce="restricted", warn="restricted"),
    )

    assert response.status_code == 200
    assert response.json()["gate"]["enabled"] is False
