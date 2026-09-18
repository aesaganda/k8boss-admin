"""
The typed row shapers (§8, and ``pod_row``/``phase_detail`` from §6).

Three properties are asserted here, and each one is a way the console could be
confidently wrong about something an operator would then act on.

**A Secret's values never appear in a list row.** Asserted against the shaper's
actual output, serialised — not against the absence of a ``data`` key, which is
the assertion that passes while a value sits in ``metadata.annotations``. Both
the plaintext and its base64 encoding are searched for, because the encoded form
is the form that actually travels.

**``phase`` lies, and ``phase_detail`` is where that is corrected.** A pod
crash-looping for a day is ``Running``. A pod stuck terminating since last
Tuesday is ``Running``. §6 is explicit that reporting a CrashLooping pod as
Running is a confident wrong answer, so the correction is computed once here
rather than reinvented in each caller.

**A number that could not be derived is ``None``, never ``0``.** An unbound PVC
has no capacity, not zero capacity. A ServiceAccount with no
``automountServiceAccountToken`` has not declined to mount its token. An
aggregated ClusterRole whose ``rules`` the controller has not filled in yet
grants an unknown amount, not nothing — and "grants nothing" is the answer an
auditor would act on.

Both object shapes are exercised: camelCase dicts from the generic §4 reader and
attribute-access stand-ins for the typed clients, because ``pod_row`` is reached
from both and a shaper that reads only one silently returns a row of nulls from
the other.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.k8s.quantities import parse_quantity as k8s_parse_quantity
from app.resources.shaping import (
    LAST_APPLIED_ANNOTATION,
    age_seconds,
    clusterrole_row,
    clusterrolebinding_row,
    configmap_row,
    get_field,
    ingress_row,
    parse_bytes,
    parse_quantity,
    phase_detail,
    pod_row,
    pv_row,
    pvc_row,
    redact_secret,
    rfc3339,
    role_row,
    rolebinding_row,
    secret_row,
    service_row,
    serviceaccount_row,
    shaper_for,
    storageclass_row,
)
from tests.conftest import obj

NOW = datetime.now(timezone.utc)

PASSWORD = "hunter2-do-not-leak"
PASSWORD_B64 = base64.b64encode(PASSWORD.encode()).decode()


def _meta(name="thing", namespace="prod", **extra):
    return {
        "name": name,
        "namespace": namespace,
        "creationTimestamp": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **extra,
    }


# --------------------------------------------------------------------------- #
# get_field — the one helper everything else stands on
# --------------------------------------------------------------------------- #

def test_a_falsy_value_is_returned_rather_than_replaced_by_the_default():
    """`0 or None` is None, and a pod with zero restarts is not a pod with unknown ones."""
    assert get_field({"restartCount": 0}, "restartCount", default=None) == 0
    assert get_field({"automount": False}, "automount") is False
    assert get_field({"state": {}}, "state") == {}


def test_a_missing_step_short_circuits_to_the_default():
    assert get_field({"status": None}, "status", "phase", default="?") == "?"
    assert get_field(None, "status", "phase") is None


def test_camel_case_is_read_from_a_dict_and_from_an_attribute_object():
    """`clusterIP` is `cluster_ip`, not `cluster_i_p`.

    A naive "underscore before every capital" silently reads as None and renders
    a Service with no cluster IP — which looks like a broken Service.
    """
    assert get_field({"spec": {"clusterIP": "10.0.0.1"}}, "spec", "clusterIP") == "10.0.0.1"
    assert get_field(obj(spec=obj(cluster_ip="10.0.0.1")), "spec", "clusterIP") == "10.0.0.1"


def test_a_snake_case_dict_is_read_too():
    """The typed clients' `to_dict()` output uses snake_case keys."""
    assert get_field({"spec": {"node_name": "ip-10-0-1-4"}}, "spec", "nodeName") == "ip-10-0-1-4"


# --------------------------------------------------------------------------- #
# Timestamps and quantities
# --------------------------------------------------------------------------- #

def test_timestamps_render_as_rfc3339_utc():
    assert rfc3339("2026-05-01T08:00:00Z") == "2026-05-01T08:00:00Z"
    assert rfc3339(datetime(2026, 5, 1, 8, tzinfo=timezone.utc)) == "2026-05-01T08:00:00Z"
    assert rfc3339(None) is None
    assert rfc3339("not-a-date") is None


def test_age_is_clamped_at_zero_rather_than_going_negative():
    """The API server's clock and this pod's clock are not the same clock.

    "created in -2 seconds" reads as a bug in the console rather than as the
    sub-second skew it is.
    """
    # Computed against a live clock, not the module-level NOW: the module is
    # imported once and the suite runs for seconds afterwards, so a "2 seconds
    # in the future" built at import time is in the past by the time it is
    # asserted — and the test would fail on the size of the suite rather than on
    # the behaviour.
    now = datetime.now(timezone.utc)

    assert age_seconds(now + timedelta(seconds=2)) == 0
    assert age_seconds(now - timedelta(hours=1)) == pytest.approx(3600, abs=5)
    assert age_seconds(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5Gi", 5 * 1024**3), ("500M", 500_000_000), ("1e3", 1000.0),
     ("100m", 0.1), ("16", 16.0)],
)
def test_quantities_parse_by_their_real_grammar(value, expected):
    """`Mi` and `M` differ by 4.8%; on a 1 TiB volume that is 50 GB."""
    assert parse_quantity(value) == pytest.approx(expected)


def test_an_unparseable_quantity_is_none_rather_than_a_guessed_magnitude():
    """A capacity the console cannot parse is a capacity it does not know.

    Zero would describe a 2 TiB volume as empty.
    """
    assert parse_quantity("banana") is None
    assert parse_quantity("5Zi") is None
    assert parse_bytes(None) is None


@pytest.mark.parametrize(
    "text",
    ["5Gi", "100m", "500M", "1e3", "1.5e2", "16", "1K", "500K", ".5Gi", "5.", "1.",
     "1e3Gi", "1.5e2Gi", "banana", "5Zi", "1KI", "12 Mi", "", "  64Gi  "],
)
def test_the_row_and_the_calculation_read_a_quantity_the_same_way(text):
    """One grammar, or a number on a row disagrees with the number beside it.

    This module used to carry a second reading of the same strings. Nothing told
    anyone the two had drifted — the row rendered, the calculation ran, and the
    only symptom was a capacity report that did not add up against the node page
    built from the other grammar. Asserted as an identity against
    `app.k8s.quantities` rather than against a table of expected numbers,
    because a table is a third reading and would drift the same way.
    """
    expected = k8s_parse_quantity(text)
    assert parse_quantity(text) == (None if expected is None else float(expected))


def test_a_quantity_the_api_server_accepts_is_not_reported_as_unparseable():
    """`1K` and `.5Gi` are apimachinery's grammar; the old local one rejected them.

    An em dash is honest about a value we cannot read, and dishonest about one we
    can: it sends an operator to go and fix a manifest the cluster was perfectly
    happy with.
    """
    assert parse_quantity("1K") == 1000.0
    assert parse_bytes(".5Gi") == 536870912
    assert parse_quantity("5.") == 5.0


def test_an_exponent_and_a_suffix_together_are_not_a_quantity():
    """`1e3Gi` is not a value any API server would have accepted.

    The grammar allows an exponent *or* a suffix after the number, never both.
    The old local reading multiplied them into a terabyte — a magnitude invented
    for a string that cannot exist, which is the one thing this function promises
    not to do.
    """
    assert parse_quantity("1e3Gi") is None
    assert parse_bytes("1.5e2Gi") is None


# --------------------------------------------------------------------------- #
# §8 — Secrets. The values must not be in the row.
# --------------------------------------------------------------------------- #

def _secret():
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": _meta("db", annotations={
            LAST_APPLIED_ANNOTATION: json.dumps({"data": {"password": PASSWORD_B64}}),
            "team": "payments",
        }),
        "type": "Opaque",
        "data": {"password": PASSWORD_B64, "username": base64.b64encode(b"app").decode()},
    }


def test_no_secret_value_appears_anywhere_in_a_list_row():
    """Asserted on the serialised row, not on the absence of a `data` key.

    "There is no `data` field" is the assertion that keeps passing while a copy
    of the value sits in `metadata.annotations`. Both the plaintext and its
    base64 encoding are searched for, because the encoded form is the one that
    actually travels.
    """
    serialised = json.dumps(secret_row(_secret()))

    assert PASSWORD not in serialised
    assert PASSWORD_B64 not in serialised
    assert "data" not in json.loads(serialised)


def test_a_secret_row_reports_key_names_and_decoded_sizes():
    """Key names are not the secret, and the size is what tells an operator
    whether the key they are looking at is a password or a certificate."""
    row = secret_row(_secret())

    assert row["keys"] == ["password", "username"]
    assert row["type"] == "Opaque"
    assert row["data_bytes"] == len(PASSWORD) + len("app")


def test_a_secret_with_no_data_reports_zero_bytes_and_no_keys():
    row = secret_row({"metadata": _meta("empty"), "type": "Opaque"})

    assert row["keys"] == []
    assert row["data_bytes"] == 0


def test_redaction_keeps_the_keys_and_nulls_the_values():
    """Three options were considered and two of them are wrong.

    Dropping `data` shows a Secret that appears to have no contents. A
    `"********"` placeholder is a *value*: it survives copy-paste and can be
    applied back to the cluster, silently replacing a password with asterisks.
    `null` says exactly what happened, and YAML carrying it fails validation if
    anyone applies it — the correct outcome for an edit made blind.
    """
    redacted = redact_secret(_secret())

    assert redacted["data"] == {"password": None, "username": None}
    assert PASSWORD_B64 not in json.dumps(redacted)


def test_redaction_strips_the_annotation_that_holds_a_copy_of_the_secret():
    """`kubectl apply -f secret.yaml` stores every value inside the object.

    §4 keeps `last-applied-configuration` on single-object reads because an
    operator editing YAML wants to see what kubectl applied. On a Secret that
    annotation *is* the Secret, so nulling `data` beside it would be a reveal
    gate that gates nothing.
    """
    redacted = redact_secret(_secret())

    assert LAST_APPLIED_ANNOTATION not in redacted["metadata"]["annotations"]
    assert redacted["metadata"]["annotations"] == {"team": "payments"}


def test_redaction_does_not_mutate_the_object_the_caller_still_holds():
    original = _secret()

    redact_secret(original)

    assert original["data"]["password"] == PASSWORD_B64
    assert LAST_APPLIED_ANNOTATION in original["metadata"]["annotations"]


def test_redaction_drops_write_only_string_data():
    redacted = redact_secret({"metadata": _meta("db"), "stringData": {"password": PASSWORD}})

    assert "stringData" not in redacted
    assert PASSWORD not in json.dumps(redacted)


# --------------------------------------------------------------------------- #
# §6 — phase_detail, where the phase is corrected
# --------------------------------------------------------------------------- #

def _pod(
    *, phase="Running", statuses=None, init_statuses=None, deletion=None,
    reason=None, containers=None,
):
    metadata = _meta("checkout-7d9-abc")
    if deletion:
        metadata["deletionTimestamp"] = deletion
    return {
        "metadata": metadata,
        "spec": {"nodeName": "ip-10-0-1-4",
                 "containers": containers if containers is not None else [
                     {"name": "app", "image": "ghcr.io/acme/checkout:1.9.2"},
                 ]},
        "status": {
            "phase": phase,
            "reason": reason,
            "podIP": "10.1.2.3",
            "qosClass": "Burstable",
            "containerStatuses": statuses,
            "initContainerStatuses": init_statuses,
        },
    }


def _status(name="app", ready=True, restarts=0, state=None):
    return {
        "name": name, "image": "ghcr.io/acme/checkout:1.9.2",
        "ready": ready, "restartCount": restarts,
        "state": state if state is not None else {"running": {"startedAt": "2026-05-01T08:00:00Z"}},
    }


def test_a_running_pod_whose_container_is_crashlooping_reports_crashloopbackoff():
    """The assertion §6 names. `phase` says Running for a pod that has been
    failing for a day, and reporting that verbatim is the confident wrong answer.
    """
    pod = _pod(statuses=[_status(ready=False, restarts=42, state={
        "waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s"},
    })])

    assert phase_detail(pod) == "CrashLoopBackOff"
    assert pod_row(pod)["phase"] == "Running"
    assert pod_row(pod)["phase_detail"] == "CrashLoopBackOff"


def test_a_pod_being_deleted_reports_terminating():
    """A pod with a deletionTimestamp is Terminating whatever its phase says,
    and it is the state an operator is most often hunting for."""
    pod = _pod(deletion="2026-08-18T09:00:00Z", statuses=[_status()])

    assert phase_detail(pod) == "Terminating"


def test_deletion_outranks_a_crashloop():
    """Order matters: a pod that is both is being deleted, and that is the
    actionable fact — the crash loop is about to stop mattering."""
    pod = _pod(deletion="2026-08-18T09:00:00Z", statuses=[_status(state={
        "waiting": {"reason": "CrashLoopBackOff"},
    })])

    assert phase_detail(pod) == "Terminating"


def test_a_healthy_running_pod_has_no_correction():
    """`phase_detail` is null when the phase is the truth. A second status on
    every healthy row is a field operators learn to ignore."""
    assert phase_detail(_pod(statuses=[_status()])) is None


def test_a_container_being_created_is_not_a_correction():
    """A Pending pod whose containers are being created is exactly what Pending
    means. Surfacing it would fire on every pod during every rollout."""
    pod = _pod(phase="Pending", statuses=[_status(ready=False, state={
        "waiting": {"reason": "ContainerCreating"},
    })])

    assert phase_detail(pod) is None


def test_an_init_container_failure_is_prefixed_the_way_kubectl_spells_it():
    """A pod blocked on an init container reports the init container's reason,
    not the app container's uninformative PodInitializing."""
    pod = _pod(
        phase="Pending",
        init_statuses=[_status("migrate", ready=False, state={
            "waiting": {"reason": "ImagePullBackOff"},
        })],
        statuses=[_status(ready=False, state={"waiting": {"reason": "PodInitializing"}})],
    )

    assert phase_detail(pod) == "Init:ImagePullBackOff"


def test_a_container_that_exited_non_zero_under_a_running_phase_is_surfaced():
    pod = _pod(statuses=[_status(ready=False, state={
        "terminated": {"exitCode": 137, "reason": "OOMKilled"},
    })])

    assert phase_detail(pod) == "OOMKilled"


def test_an_evicted_pod_reports_the_status_reason():
    assert phase_detail(_pod(phase="Failed", reason="Evicted", statuses=[])) == "Evicted"


# --------------------------------------------------------------------------- #
# §6 — pod_row
# --------------------------------------------------------------------------- #

def test_ready_counts_ready_statuses_over_the_container_count():
    row = pod_row(_pod(statuses=[_status("app"), _status("sidecar", ready=False)]))

    assert row["ready"] == "1/2"


def test_the_ready_denominator_falls_back_to_the_spec_for_an_unscheduled_pod():
    """`0/2` is right for a pod that has not been scheduled; `0/0` says the pod
    has no containers."""
    row = pod_row(_pod(phase="Pending", statuses=None, containers=[
        {"name": "app", "image": "a"}, {"name": "sidecar", "image": "b"},
    ]))

    assert row["ready"] == "0/2"
    assert [c["name"] for c in row["containers"]] == ["app", "sidecar"]
    assert row["containers"][0]["state"] is None


def test_restarts_are_summed_across_containers_and_zero_is_a_real_zero():
    """Different from §6's `restarts_24h`, which is null when the pod *listing*
    failed. Here we read the pod: nothing has restarted, and that is a fact."""
    assert pod_row(_pod(statuses=[_status(restarts=3), _status("sidecar", restarts=1)]))[
        "restarts"] == 4
    assert pod_row(_pod(statuses=[_status(restarts=0)]))["restarts"] == 0


def test_the_owner_is_the_controlling_reference():
    pod = _pod(statuses=[_status()])
    pod["metadata"]["ownerReferences"] = [
        {"kind": "Node", "name": "ip-10-0-1-4", "controller": False},
        {"kind": "ReplicaSet", "name": "checkout-7d9", "controller": True},
    ]

    assert pod_row(pod)["owner"] == {"kind": "ReplicaSet", "name": "checkout-7d9"}


def test_a_pod_with_no_owner_reports_none_rather_than_an_empty_object():
    assert pod_row(_pod(statuses=[_status()]))["owner"] is None


def test_pod_row_reads_a_typed_client_object_too():
    """`pod_row` is called from the workloads lane, which holds V1Pod-style
    objects, and from the resource browser, which holds parsed JSON."""
    pod = obj(
        metadata=obj(name="checkout-7d9-abc", namespace="prod",
                     creation_timestamp=NOW - timedelta(hours=1), owner_references=[]),
        spec=obj(node_name="ip-10-0-1-4", containers=[obj(name="app", image="img")]),
        status=obj(phase="Running", pod_ip="10.1.2.3", qos_class="Burstable",
                   container_statuses=[obj(
                       name="app", image="img", ready=True, restart_count=2,
                       state=obj(running=obj(started_at=NOW)),
                   )],
                   init_container_statuses=[]),
    )

    row = pod_row(pod)

    assert row["node"] == "ip-10-0-1-4"
    assert row["ip"] == "10.1.2.3"
    assert row["ready"] == "1/1"
    assert row["restarts"] == 2
    assert row["containers"][0]["state"] == "Running"


# --------------------------------------------------------------------------- #
# §8 — the nulls that must not become zeros
# --------------------------------------------------------------------------- #

def test_a_service_row_defaults_its_endpoint_count_to_none():
    """"No backends" and "we failed to look up the backends" are the two states
    an operator most needs to tell apart when something returns 503."""
    row = service_row({"metadata": _meta("checkout"), "spec": {"type": "ClusterIP"}})

    assert row["endpoint_count"] is None
    assert service_row({"metadata": _meta("checkout"), "spec": {}}, endpoint_count=0)[
        "endpoint_count"] == 0


def test_a_service_row_merges_load_balancer_addresses_into_external_ips():
    """Keeping them separate leaves the column empty for every LoadBalancer."""
    row = service_row({
        "metadata": _meta("checkout"),
        "spec": {"type": "LoadBalancer", "clusterIP": "10.0.0.1",
                 "externalIPs": ["1.2.3.4"],
                 "ports": [{"name": "http", "port": 80, "targetPort": 8080,
                            "protocol": "TCP", "nodePort": 30080}]},
        "status": {"loadBalancer": {"ingress": [{"hostname": "elb.example"}]}},
    })

    assert row["externalIPs"] == ["1.2.3.4", "elb.example"]
    assert row["ports"][0]["nodePort"] == 30080


def test_a_pending_pvc_has_no_capacity_rather_than_zero_capacity():
    """`status.capacity` is what was provisioned; `spec.resources.requests` is
    what was asked for. Reporting the request would tell an operator a claim
    stuck in Pending has 500 GiB of storage."""
    row = pvc_row({
        "metadata": _meta("data-pg-0"),
        "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "gp3",
                 "resources": {"requests": {"storage": "500Gi"}}},
        "status": {"phase": "Pending"},
    })

    assert row["capacity_bytes"] is None
    assert row["status"] == "Pending"
    assert row["access_modes"] == ["ReadWriteOnce"]
    # The request is carried, separately and unmistakably: §20 needs it to seed
    # an expansion, and the row above is where it must not be mistaken for
    # capacity.
    assert row["requested"] == "500Gi"
    assert row["requested_bytes"] == 500 * 1024**3


def test_a_bound_pvc_reports_what_was_actually_provisioned():
    row = pvc_row({
        "metadata": _meta("data-pg-0"),
        "spec": {"accessModes": ["ReadWriteOnce"], "volumeName": "pv-1"},
        "status": {"phase": "Bound", "capacity": {"storage": "500Gi"},
                   "accessModes": ["ReadWriteOnce"]},
    })

    assert row["capacity_bytes"] == 500 * 1024**3
    assert row["volume"] == "pv-1"
    # No request on this claim's spec at all: null rather than borrowed from the
    # capacity beside it, which would make the two columns agree by construction
    # and stop the row from ever showing an expansion in flight.
    assert row["requested"] is None
    assert row["requested_bytes"] is None


def test_a_pv_reports_its_claim_as_namespace_slash_name():
    row = pv_row({
        "metadata": _meta("pv-1", namespace=None),
        "spec": {"capacity": {"storage": "500Gi"}, "accessModes": ["ReadWriteOnce"],
                 "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "gp3",
                 "claimRef": {"namespace": "data", "name": "data-pg-0"}},
        "status": {"phase": "Bound"},
    })

    assert row["claim"] == "data/data-pg-0"
    assert row["reclaim_policy"] == "Retain"


def test_an_unclaimed_pv_reports_a_null_claim():
    row = pv_row({"metadata": _meta("pv-1"), "spec": {}, "status": {"phase": "Available"}})

    assert row["claim"] is None


def test_a_service_account_keeps_automount_as_a_tri_state():
    """`None` means the pod spec decides; `False` is an explicit decision on the
    ServiceAccount. Collapsing the first into the second tells an auditor a token
    is not mounted when it is."""
    assert serviceaccount_row({"metadata": _meta("checkout")})["automount"] is None
    assert serviceaccount_row(
        {"metadata": _meta("checkout"), "automountServiceAccountToken": False}
    )["automount"] is False
    assert serviceaccount_row(
        {"metadata": _meta("checkout"), "automountServiceAccountToken": True}
    )["automount"] is True


def test_an_aggregated_clusterrole_has_an_unknown_rule_count_not_zero():
    """Before the controller fills `rules` in, the field is null. Reporting 0
    describes an aggregate that grants cluster-admin as granting nothing."""
    row = clusterrole_row({
        "metadata": _meta("view", namespace=None),
        "aggregationRule": {"clusterRoleSelectors": [{"matchLabels": {"rbac": "view"}}]},
    })

    assert row["rule_count"] is None
    assert row["rules"] == []
    assert row["namespace"] is None


def test_an_explicitly_empty_rule_list_is_a_real_zero():
    row = role_row({"metadata": _meta("empty"), "rules": []})

    assert row["rule_count"] == 0


def test_a_role_reports_its_rules_verbatim():
    row = role_row({"metadata": _meta("reader"), "rules": [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
    ]})

    assert row["rule_count"] == 1
    assert row["rules"][0]["resourceNames"] == []
    assert row["namespace"] == "prod"


def test_a_binding_keeps_subject_namespaces_exactly_as_written():
    """User and Group subjects are not namespaced. Defaulting them to the
    binding's namespace invents an attribution the object does not make."""
    row = rolebinding_row({
        "metadata": _meta("checkout"),
        "roleRef": {"kind": "Role", "name": "reader"},
        "subjects": [
            {"kind": "ServiceAccount", "name": "checkout", "namespace": "prod"},
            {"kind": "Group", "name": "sre"},
        ],
    })

    assert row["role"] == {"kind": "Role", "name": "reader"}
    assert row["subjects"][1]["namespace"] is None


def test_a_clusterrolebinding_is_cluster_scoped():
    row = clusterrolebinding_row({
        "metadata": _meta("admins", namespace=None),
        "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [],
    })

    assert row["namespace"] is None


# --------------------------------------------------------------------------- #
# §8 — the rest of the config and storage rows
# --------------------------------------------------------------------------- #

def test_a_configmap_row_reports_keys_and_size_but_not_values():
    """Not secret, but two hundred of them carrying embedded certificates is
    megabytes of payload nothing renders."""
    row = configmap_row({
        "metadata": _meta("app-config"),
        "data": {"log_level": "debug"},
        "binaryData": {"cert.der": base64.b64encode(b"x" * 40).decode()},
    })

    assert row["keys"] == ["cert.der", "log_level"]
    assert row["data_bytes"] == len("debug") + 40
    assert "debug" not in json.dumps(row["keys"])


def test_an_ingress_falls_back_to_the_legacy_class_annotation():
    """Reporting null for an Ingress that nginx is in fact serving has an
    operator debugging a rule that works."""
    row = ingress_row({
        "metadata": _meta("checkout", annotations={"kubernetes.io/ingress.class": "nginx"}),
        "spec": {"rules": [{"host": "shop.example", "http": {"paths": [
            {"path": "/", "pathType": "Prefix",
             "backend": {"service": {"name": "checkout", "port": {"number": 80}}}},
        ]}}], "tls": [{"hosts": ["shop.example"]}]},
        "status": {"loadBalancer": {"ingress": [{"ip": "1.2.3.4"}]}},
    })

    assert row["class"] == "nginx"
    assert row["rules"][0]["paths"][0]["service"] == "checkout"
    assert row["tls_hosts"] == ["shop.example"]
    assert row["address"] == "1.2.3.4"


def test_a_storage_class_reads_the_beta_default_annotation_too():
    """A cluster upgraded from 1.5 keeps the beta spelling indefinitely. Reading
    only the GA one reports every class as non-default, which makes a PVC with no
    storageClassName look like it will never bind."""
    row = storageclass_row({
        "metadata": _meta("gp3", namespace=None, annotations={
            "storageclass.beta.kubernetes.io/is-default-class": "true",
        }),
        "provisioner": "ebs.csi.aws.com",
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "WaitForFirstConsumer",
    })

    assert row["is_default"] is True
    assert row["provisioner"] == "ebs.csi.aws.com"


def test_a_storage_class_without_the_annotation_is_not_default():
    row = storageclass_row({"metadata": _meta("gp2", namespace=None),
                            "provisioner": "kubernetes.io/aws-ebs"})

    assert row["is_default"] is False


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

def test_the_registry_is_keyed_on_the_real_group_name():
    """`core` is translated at the edge, so a lookup with it must miss — if it
    hit, there would be two spellings live in the process at once."""
    assert shaper_for("", "secrets") is secret_row
    assert shaper_for("core", "secrets") is None
    assert shaper_for("rbac.authorization.k8s.io", "clusterroles") is clusterrole_row


def test_a_resource_with_no_typed_row_has_no_shaper():
    """The generic browser then returns the trimmed manifest, which is right for
    a CRD nobody wrote a table for."""
    assert shaper_for("acme.example", "widgets") is None
