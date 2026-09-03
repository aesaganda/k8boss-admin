"""
The §1.5 diff.

A diff is the last thing an operator reads before they change a production
cluster, and it has exactly one job: show them *their* change. The tests here are
mostly about what must **not** appear — ``managedFields``, a resourceVersion the
API server bumps on every write anywhere, a status block the controller owns —
because every one of those, left in, pushes the two lines that matter off the
screen. A diff nobody reads is worse than no diff: it is a confirmation dialog
that has been trained to be clicked through.

The second half of the file is about ``changed``. ``changed: false`` is a
promise that the write is a no-op (§1.5), and it can only be made honestly if the
comparison ignores the fields the API server was going to change regardless.
"""

from __future__ import annotations

from app.admin.diff import LAST_APPLIED_ANNOTATION, build_diff, digest


def deployment(replicas=3, *, resource_version="884213", generation=4, status=True, **extra):
    obj = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout",
            "namespace": "prod",
            "uid": "6f1f0a2e-1111-2222-3333-444455556666",
            "resourceVersion": resource_version,
            "generation": generation,
            "creationTimestamp": "2026-05-01T08:00:00Z",
            "labels": {"app": "checkout"},
            "managedFields": [
                {"manager": "kube-controller-manager", "operation": "Update",
                 "fieldsV1": {"f:status": {"f:replicas": {}}}},
            ],
        },
        "spec": {"replicas": replicas, "template": {"spec": {"containers": [
            {"name": "app", "image": "ghcr.io/acme/checkout:1.9.2"},
        ]}}},
    }
    if status:
        obj["status"] = {"replicas": replicas, "readyReplicas": replicas,
                         "observedGeneration": generation}
    obj.update(extra)
    return obj


# --------------------------------------------------------------------------- #
# What the operator must not have to read past
# --------------------------------------------------------------------------- #

def test_server_bookkeeping_is_normalised_out_of_both_sides():
    """resourceVersion, generation and managedFields differ on every single
    write. Left in, `changed` would be true for a no-op and the two lines that
    matter would be buried."""
    before = deployment(3, resource_version="884213", generation=4)
    after = deployment(3, resource_version="884999", generation=5)
    # The API server does not run controllers for a dry run, so its projection
    # carries the live status verbatim — including an observedGeneration that
    # still lags the bumped generation.
    after["status"] = dict(before["status"])

    result = build_diff(before, after)

    assert result["changed"] is False, "nothing the operator asked for has changed"
    assert result["unified"] == ""
    for buried in ("managedFields", "resourceVersion", "generation", "uid", "creationTimestamp"):
        assert buried not in result["before"]
        assert buried not in result["after"]


def test_the_operators_own_change_survives_normalisation():
    result = build_diff(deployment(3), deployment(5))

    assert result["changed"] is True
    assert "-  replicas: 3" in result["unified"]
    assert "+  replicas: 5" in result["unified"]
    assert result["unified"].startswith("--- live\n+++ proposed\n")


def test_kubectls_serialised_copy_of_the_object_is_dropped():
    """last-applied-configuration is the whole object again, on one line, inside
    the object. Kept, it is a 3 KB line no diff viewer can wrap usefully."""
    before = deployment(3)
    before["metadata"]["annotations"] = {
        LAST_APPLIED_ANNOTATION: '{"apiVersion":"apps/v1","kind":"Deployment","spec":{}}',
        "team": "payments",
    }
    after = deployment(5)
    after["metadata"]["annotations"] = {"team": "payments"}

    result = build_diff(before, after)

    assert LAST_APPLIED_ANNOTATION not in result["unified"]
    assert "team: payments" in result["before"] and "team: payments" in result["after"]
    assert "-  replicas: 3" in result["unified"]


def test_an_annotations_block_emptied_by_normalisation_leaves_no_phantom_hunk():
    """An object whose only annotation was kubectl's copy would otherwise show
    `annotations: {}` on one side and nothing on the other — a hunk describing a
    change that is not being made."""
    before = deployment(3)
    before["metadata"]["annotations"] = {LAST_APPLIED_ANNOTATION: "{}"}
    after = deployment(3)

    assert build_diff(before, after)["changed"] is False


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def test_a_hand_written_manifest_does_not_appear_to_delete_the_status():
    """The 409 path diffs live against the *submitted document*, which has no
    status. Showing 'your edit removes the entire status block' would describe an
    outcome that is not going to happen."""
    live = deployment(3, status=True)
    submitted = deployment(3, status=False)

    result = build_diff(live, submitted)

    assert result["changed"] is False
    assert "status" not in result["before"]
    assert "readyReplicas" not in result["before"]


def test_a_status_difference_between_two_server_objects_is_shown():
    """When both sides came from the API server, a status difference is real and
    is the operator's business — a rollback whose projection reports fewer ready
    replicas is exactly what they want to see."""
    before = deployment(3)
    after = deployment(3)
    after["status"]["readyReplicas"] = 1

    result = build_diff(before, after)

    assert result["changed"] is True
    assert "-  readyReplicas: 3" in result["unified"]
    assert "+  readyReplicas: 1" in result["unified"]


# --------------------------------------------------------------------------- #
# Create and delete
# --------------------------------------------------------------------------- #

def test_a_create_diffs_against_an_empty_live_side():
    result = build_diff(None, deployment(3))

    assert result["before"] == ""
    assert result["changed"] is True
    assert "+kind: Deployment" in result["unified"]


def test_a_delete_is_before_live_after_null_and_always_changed():
    """§4: the confirm dialog has to show exactly what disappears."""
    result = build_diff(deployment(3), None)

    assert result["after"] == "", "null renders as nothing, never as the text 'null'"
    assert result["changed"] is True
    assert "-kind: Deployment" in result["unified"]


def test_yaml_keeps_the_api_servers_field_order():
    """Alphabetical order puts status above spec and buries kind in the middle,
    which makes a diff materially harder to read."""
    text = build_diff(None, deployment(3))["after"]
    keys = [line.split(":")[0] for line in text.splitlines() if line and not line[0].isspace()]

    assert keys == ["apiVersion", "kind", "metadata", "spec", "status"]


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #

def test_the_digest_is_stable_across_identical_diffs():
    """This is the whole proof that what an operator confirmed in the dry run is
    what was applied: the two calls must produce the same digest."""
    dry_run = build_diff(deployment(3, resource_version="1"), deployment(5, resource_version="1"))
    confirmed = build_diff(
        deployment(3, resource_version="99"), deployment(5, resource_version="100"),
    )

    assert digest(dry_run) == digest(confirmed)
    assert digest(dry_run).startswith("sha256:")


def test_different_changes_have_different_digests():
    to_five = build_diff(deployment(3), deployment(5))
    to_six = build_diff(deployment(3), deployment(6))

    assert digest(to_five) != digest(to_six)


def test_an_unchanged_diff_digests_to_the_digest_of_nothing():
    """Recognisable on sight in the audit trail: a row whose digest is this one
    recorded a write that changed nothing."""
    unchanged = build_diff(deployment(3), deployment(3))

    assert unchanged["changed"] is False
    assert digest(unchanged) == digest({"unified": ""})


def test_normalisation_does_not_mutate_the_caller_s_objects():
    """`before` is also the object the conflict check reads resourceVersion from.
    Stripping it in place would disable optimistic concurrency as a side effect
    of rendering a diff."""
    before = deployment(3)
    build_diff(before, deployment(5))

    assert before["metadata"]["resourceVersion"] == "884213"
    assert "managedFields" in before["metadata"]


# --------------------------------------------------------------------------- #
# Secrets. The values must not be in the diff, and `changed` must still be true.
# --------------------------------------------------------------------------- #

import base64  # noqa: E402

PASSWORD = "hunter2-rotated-2026"
PASSWORD_B64 = base64.b64encode(PASSWORD.encode()).decode()
ROTATED = "hunter3-rotated-2026"  # same length, so a null-for-null redaction would hide it
ROTATED_B64 = base64.b64encode(ROTATED.encode()).decode()


def secret(password_b64=PASSWORD_B64, *, extra=None, resource_version="10"):
    data = {"password": password_b64, "username": base64.b64encode(b"app").decode()}
    data.update(extra or {})
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "db",
            "namespace": "prod",
            "resourceVersion": resource_version,
            "annotations": {
                LAST_APPLIED_ANNOTATION: '{"data":{"password":"' + password_b64 + '"}}',
                "team": "payments",
            },
        },
        "type": "Opaque",
        "data": data,
    }


def _everything(diff):
    return "\n".join(str(diff[key]) for key in ("before", "after", "unified"))


def test_a_secret_delete_diff_carries_no_value():
    """The leak this closes: a dry-run delete is permitted in read-only mode,
    preflighted on `delete`, and returned the whole live Secret as `before`."""
    diff = build_diff(secret(), None)

    assert PASSWORD_B64 not in _everything(diff)
    assert PASSWORD not in _everything(diff)
    assert "password: <redacted, 20 bytes>" in diff["before"]
    assert "username: <redacted, 3 bytes>" in diff["before"]
    assert diff["after"] == ""
    assert diff["changed"] is True


def test_a_same_length_rotation_is_still_a_change():
    """`null` on both sides would report a password rotation as a no-op, and a
    no-op gets no Confirm button. The marker is what keeps the write possible."""
    diff = build_diff(secret(PASSWORD_B64), secret(ROTATED_B64, resource_version="11"))

    assert ROTATED_B64 not in _everything(diff)
    assert diff["changed"] is True
    assert "-  password: <redacted, 20 bytes>" in diff["unified"]
    assert "+  password: <redacted, 20 bytes, changed>" in diff["unified"]
    # The unchanged key redacts to identical text on both sides: it may appear
    # as context, never as a change.
    assert "-  username" not in diff["unified"]
    assert "+  username" not in diff["unified"]


def test_an_unchanged_secret_is_still_a_no_op():
    diff = build_diff(secret(), secret(resource_version="11"))
    assert diff["changed"] is False
    assert diff["unified"] == ""


def test_an_added_and_a_removed_key_show_as_hunks_without_values():
    before = secret()
    after = secret(extra={"token": base64.b64encode(b"tok-123456").decode()}, resource_version="11")
    del after["data"]["username"]

    diff = build_diff(before, after)

    assert "+  token: <redacted, 10 bytes>" in diff["unified"]
    assert "-  username: <redacted, 3 bytes>" in diff["unified"]
    assert "tok-123456" not in _everything(diff)
    assert base64.b64encode(b"tok-123456").decode() not in _everything(diff)


def test_string_data_in_a_submitted_manifest_is_redacted_by_plaintext_length():
    """A 409 diffs the submitted document as-is against live, and a submitted
    Secret may carry `stringData` in plaintext."""
    submitted = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod"},
        "type": "Opaque",
        "stringData": {"password": "plain-text-password"},
    }
    diff = build_diff(secret(), submitted)

    assert "plain-text-password" not in _everything(diff)
    assert "password: <redacted, 19 bytes>" in diff["after"]


def test_the_last_applied_copy_of_a_secret_is_not_in_the_diff():
    diff = build_diff(secret(), None)
    assert LAST_APPLIED_ANNOTATION not in _everything(diff)
    assert "team: payments" in diff["before"]


def test_redaction_does_not_touch_the_secret_the_caller_still_holds():
    """`before` is the live object apply.py also reads `resourceVersion` from."""
    live = secret()
    build_diff(live, None)
    assert live["data"]["password"] == PASSWORD_B64
    assert LAST_APPLIED_ANNOTATION in live["metadata"]["annotations"]


def test_a_configmap_is_not_redacted():
    """Only the kind whose `data` is the secret. A ConfigMap's payload is the
    operator's own change and hiding it would hide the edit."""
    before = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"k": "visible"}}
    after = {**before, "data": {"k": "visible-2"}}
    diff = build_diff(before, after)
    assert "visible-2" in diff["after"]
    assert "<redacted" not in _everything(diff)


def test_a_secret_shaped_custom_resource_in_another_group_is_left_alone():
    obj = {"apiVersion": "vault.example/v1", "kind": "Secret", "metadata": {"name": "x"}, "data": {"ref": "path/to/x"}}
    diff = build_diff(obj, None)
    assert "path/to/x" in diff["before"]


def test_the_digest_of_a_secret_diff_is_over_the_redacted_text():
    """Two Secrets with different values but the same redacted diff digest the
    same: the digest proves what was confirmed is what was applied, and the
    audit table must not become a place a value can be recovered from."""
    a = build_diff(secret(PASSWORD_B64), None)
    b = build_diff(secret(base64.b64encode(b"different-but-20-chr").decode()), None)
    assert digest(a) == digest(b)
