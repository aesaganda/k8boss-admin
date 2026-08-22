"""
Node debug pods (§5.5) — the largest grant this console offers.

Everything asserted here is asserted because getting it wrong would be a
*security claim that is false*, which is worse than a bug:

* **Both gates, and the refusal is audited.** ``ADMIN_NODE_DEBUG_ENABLED`` is a
  second switch on top of ``ADMIN_ALLOW_MUTATIONS``, and unlike every other write
  here it refuses the dry run too — a projection of this pod is a working recipe
  for a privileged one. The refusal writes an audit row, the way ``mutate()``
  audits its own gate and unlike the Secret reveal, which does not: "who tried to
  put a host-mounted pod on a node while that was switched off" is exactly the
  question the trail exists for, and the funnel that would otherwise record it is
  never reached.

* **The manifest is exactly what the docstring claims.** Every privileged field
  is pinned by a test, and so is every field that is deliberately *absent*.
  ``privileged`` creeping in later, or ``automountServiceAccountToken`` being
  dropped in a refactor, would make the dialog's promises to the operator untrue
  while every other test still passed.

* **Read-only by default.** A departure from ``kubectl debug``, which always
  mounts the host filesystem writable. If that default ever inverts silently,
  the console starts handing out write access to machines on a checkbox nobody
  ticked.

* **Removal only touches pods this console created.** The route takes a pod name
  in its path; without the label and ``nodeName`` checks it would be a general
  pod-delete wearing a node's URL.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.admin import node_debug
from app.audit import recorder
from app.config import settings
from app.errors import Invalid, MutationsDisabled, NotFound, RBACDenied
from app.resources import catalog
from tests.conftest import obj

NODE = "ip-10-0-1-4"
NAMESPACE = "default"

POD_INFO = {
    "group": "", "version": "v1", "kind": "Pod", "resource": "pods",
    "namespaced": True,
    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    "shortNames": ["po"], "categories": ["all"], "apiVersion": "v1", "preferred": True,
}


def debug_pod_object(*, name="node-debugger-ip-10-0-1-4-x4k2p", node=NODE,
                     labelled=True, read_only=True, phase="Running"):
    """A node debug pod as the API server hands one back."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name, "namespace": NAMESPACE, "resourceVersion": "9001",
            "creationTimestamp": "2026-08-21T09:00:00Z",
            "labels": ({node_debug.COMPONENT_LABEL: node_debug.COMPONENT_VALUE}
                       if labelled else {"app": "something-else"}),
            "annotations": {node_debug.NODE_ANNOTATION: node},
        },
        "spec": {
            "nodeName": node,
            "containers": [{
                "name": "debugger", "image": "busybox:1.36",
                "volumeMounts": [{
                    "name": node_debug.HOST_VOLUME_NAME,
                    "mountPath": "/host",
                    **({"readOnly": True} if read_only else {}),
                }],
            }],
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {"name": "debugger",
                 "state": {"running": {"startedAt": "2026-08-21T09:00:05Z"}}}
            ],
        },
    }


class FakeApiServer:
    """A stand-in for ``ApiClient.call_api``, dispatching on method and path.

    Honours ``_return_http_data_only`` both ways, because the read path
    (``catalog.raw_get``) sets it True and the write path sets it False so it can
    see the ``Warning:`` headers §1.5 promises.
    """

    def __init__(self, *, pods=None, live=None):
        self.requests: list[SimpleNamespace] = []
        self.pods = pods if pods is not None else []
        self.live = live
        self.raises: dict[str, BaseException] = {}

    def __call__(self, path, method, **kwargs):
        query = dict(kwargs.get("query_params") or [])
        self.requests.append(SimpleNamespace(
            path=path, method=method, query=query, body=kwargs.get("body"),
        ))
        for key in (f"{method} {path}", method, path):
            if key in self.raises:
                raise self.raises[key]

        if method == "GET" and path.endswith("/pods"):
            payload = {"apiVersion": "v1", "kind": "PodList",
                       "metadata": {}, "items": list(self.pods)}
        elif method == "GET":
            payload = self.live if self.live is not None else debug_pod_object()
        elif method == "POST":
            # The API server echoes the object back, with the fields it fills in.
            payload = json.loads(json.dumps(kwargs.get("body") or {}))
            payload.setdefault("metadata", {})["resourceVersion"] = "9002"
        elif method == "DELETE":
            payload = {"kind": "Status", "status": "Success"}
        else:  # pragma: no cover - the module issues no other method
            raise AssertionError(f"unexpected {method} {path}")

        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, {}

    def of(self, method):
        return [r for r in self.requests if r.method == method]


@pytest.fixture
def server(monkeypatch, fake_k8s):
    """A fake API server, with discovery and the access review answered."""
    monkeypatch.setattr(catalog, "resolve", lambda group, version, plural: POD_INFO)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer()
    fake_k8s.api_client.returns("call_api", api_server)
    return api_server


@pytest.fixture
def allow_node_debug(monkeypatch, allow_mutations):
    """Both gates on. Patches the live Settings object, because it is built at
    import time and re-reading the environment later would have no effect."""
    monkeypatch.setattr(settings, "node_debug_enabled", True)
    monkeypatch.setattr(settings, "node_debug_namespace", NAMESPACE)
    return settings


def audit_rows():
    return recorder.query(limit=50)["items"]


def create(**kwargs):
    return node_debug.create_node_debug_pod(NODE, **kwargs)


def created_pod(server):
    (request,) = server.of("POST")
    return request.body


# --------------------------------------------------------------------------- #
# The two gates
# --------------------------------------------------------------------------- #

def test_a_read_only_console_refuses_and_never_touches_the_cluster(db_engine, server):
    with pytest.raises(MutationsDisabled) as caught:
        create(dry_run=False)

    assert "ADMIN_ALLOW_MUTATIONS" in (caught.value.hint or "")
    assert server.of("POST") == []


def test_the_feature_gate_refuses_even_with_writes_enabled(
    db_engine, server, allow_mutations, monkeypatch,
):
    """The whole point of the second switch: every other write works, this one
    does not, and the refusal names the setting that is off."""
    monkeypatch.setattr(settings, "node_debug_enabled", False)

    with pytest.raises(MutationsDisabled) as caught:
        create(dry_run=False)

    assert "ADMIN_NODE_DEBUG_ENABLED" in (caught.value.hint or "")
    assert "ADMIN_NODE_DEBUG_ENABLED" in (caught.value.detail or "")
    assert server.of("POST") == []


def test_the_dry_run_is_refused_too_unlike_every_other_write(
    db_engine, server, allow_mutations, monkeypatch,
):
    """§1.6 permits a projection on a read-only console because inspecting what
    would change is a read. Not here: the projection *is* a working manifest for
    a privileged pod, and a deployment that switched this off did not consent to
    handing one out."""
    monkeypatch.setattr(settings, "node_debug_enabled", False)

    with pytest.raises(MutationsDisabled):
        create(dry_run=True)

    assert server.of("POST") == []


def test_the_gate_refusal_is_audited(db_engine, server):
    """`mutate()` audits its own gate refusal; the Secret reveal does not. This
    follows the funnel, because the funnel is never reached — without this row
    there would be no record that anyone tried."""
    with pytest.raises(MutationsDisabled):
        create(dry_run=False)

    row = audit_rows()[0]
    assert row["outcome"] == "denied"
    assert row["verb"] == "create"
    assert row["target"]["resource"] == "pods"
    assert "mutations_disabled" in (row["error"] or "")
    assert NODE in (row["detail"] or "")


def test_enabled_state_reports_which_switch_is_off(db_engine, allow_mutations, monkeypatch):
    monkeypatch.setattr(settings, "node_debug_enabled", False)
    state = node_debug.enabled_state()
    assert state["enabled"] is False
    assert "ADMIN_NODE_DEBUG_ENABLED" in state["detail"]

    monkeypatch.setattr(settings, "node_debug_enabled", True)
    assert node_debug.enabled_state()["enabled"] is True


# --------------------------------------------------------------------------- #
# The funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_projects_the_pod_and_reports_applied_false(
    db_engine, server, allow_node_debug,
):
    result = create(dry_run=True)

    (request,) = server.of("POST")
    assert request.path == f"/api/v1/namespaces/{NAMESPACE}/pods"
    assert request.query == {"dryRun": "All"}
    assert result["applied"] is False
    # §4: a create has nothing live to diff against, so the whole manifest is an
    # addition — which is how every privileged field gets disclosed.
    assert result["diff"]["before"] == ""
    assert "+  hostPID: true" in result["diff"]["unified"]
    assert "+      path: /" in result["diff"]["unified"]
    assert audit_rows()[0]["outcome"] == "dry_run"


def test_the_real_write_is_the_same_request_without_the_dryrun_parameter(
    db_engine, server, allow_node_debug,
):
    result = create(dry_run=False)

    (request,) = server.of("POST")
    assert request.query == {}
    assert result["applied"] is True
    assert result["pod"].startswith(node_debug.NAME_PREFIX)
    assert result["namespace"] == NAMESPACE
    assert audit_rows()[0]["outcome"] == "applied"


def test_the_audit_row_names_the_node_the_image_and_the_mount_mode(
    db_engine, server, allow_node_debug,
):
    """Read-only versus read-write is the difference between reading the machine
    and being able to change it, so the trail records which one happened."""
    create(image="ghcr.io/acme/netshoot:1.2", writable_host=True, dry_run=False)

    detail = audit_rows()[0]["detail"]
    assert NODE in detail
    assert "ghcr.io/acme/netshoot:1.2" in detail
    assert "read-write" in detail


def test_a_denied_preflight_never_reaches_the_cluster(
    db_engine, server, allow_node_debug, fake_k8s,
):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, denied=True, reason="no RBAC policy matched", evaluation_error=None,
    )))

    with pytest.raises(RBACDenied):
        create(dry_run=False)

    assert server.of("POST") == []


def test_podsecurity_admission_surfaces_at_the_dry_run(db_engine, server, allow_node_debug):
    """The refusal that actually decides whether this is possible on a cluster.

    A namespace enforcing `baseline` or `restricted` rejects a pod with a
    hostPath volume and host namespaces — and admission runs on `dryRun=All`
    exactly as on the real call, so the operator learns at the *preview* step,
    before anything exists. Nothing in this module implements that; it is the
    funnel's dry run doing its job."""
    server.raises["POST"] = ApiException(status=422, reason="Unprocessable Entity")

    with pytest.raises(Invalid):
        create(dry_run=True)

    assert audit_rows()[0]["outcome"] == "failed"


# --------------------------------------------------------------------------- #
# The manifest — every privileged field, and every deliberate absence
# --------------------------------------------------------------------------- #

def test_the_host_filesystem_is_mounted_read_only_by_default(
    db_engine, server, allow_node_debug,
):
    """A deliberate departure from `kubectl debug`, which always mounts it
    writable. If this default ever inverts, the console starts handing out write
    access to machines on a checkbox nobody ticked."""
    create(dry_run=False)

    (mount,) = created_pod(server)["spec"]["containers"][0]["volumeMounts"]
    assert mount["mountPath"] == "/host"
    assert mount["readOnly"] is True


def test_writable_is_only_ever_what_the_caller_asked_for(db_engine, server, allow_node_debug):
    create(writable_host=True, dry_run=False)

    (mount,) = created_pod(server)["spec"]["containers"][0]["volumeMounts"]
    assert mount["readOnly"] is False


def test_the_pod_carries_the_privileged_fields_the_feature_needs(
    db_engine, server, allow_node_debug,
):
    create(dry_run=False)
    spec = created_pod(server)["spec"]

    assert spec["nodeName"] == NODE
    assert spec["hostPID"] is True
    assert spec["hostNetwork"] is True
    assert spec["restartPolicy"] == "Never"
    # Tolerates every taint. Not for the cordon or NoSchedule — `nodeName` has
    # already bypassed the scheduler that enforces those — but so the
    # taint-eviction controller does not throw the pod off a NoExecute node
    # moments after the kubelet starts it.
    assert spec["tolerations"] == [{"operator": "Exists"}]
    (volume,) = spec["volumes"]
    assert volume["hostPath"] == {"path": "/", "type": "Directory"}


def test_the_pod_does_not_carry_what_it_deliberately_omits(
    db_engine, server, allow_node_debug,
):
    """Each absence is a decision, and each would be invisible if it regressed.

    `privileged` is the line between reading the machine and reconfiguring its
    kernel. A service account token beside the host filesystem is strictly worse
    than either alone. `hostIPC` is what `kubectl` sets and node debugging does
    not need. A resource request could be refused by the very node under
    pressure — the one being debugged.
    """
    create(dry_run=False)
    pod = created_pod(server)
    spec = pod["spec"]
    container = spec["containers"][0]

    assert spec["automountServiceAccountToken"] is False
    assert "hostIPC" not in spec
    assert "securityContext" not in container
    assert "securityContext" not in spec
    assert "resources" not in container
    assert "privileged" not in json.dumps(pod)


def test_cluster_dns_works_inside_the_pod(db_engine, server, allow_node_debug):
    """An unset dnsPolicy defaults to ClusterFirst, and the kubelet silently
    downgrades that to Default for a hostNetwork pod — so the container resolves
    through the node's /etc/resolv.conf and `nslookup kubernetes.default` fails.
    An operator debugging "can this node reach my service" would read that as
    cluster DNS being broken. `kubectl debug` omits this field; we do not."""
    create(dry_run=False)

    assert created_pod(server)["spec"]["dnsPolicy"] == "ClusterFirstWithHostNet"


def test_the_container_is_time_bounded(db_engine, server, allow_node_debug, monkeypatch):
    """The only mitigation available for the hazard this feature cannot close:
    nothing deletes the pod when the operator walks away. The deadline stops the
    *container*; the object stays and still needs removing."""
    monkeypatch.setattr(settings, "node_debug_max_seconds", 1800)
    create(dry_run=False)

    assert created_pod(server)["spec"]["activeDeadlineSeconds"] == 1800


def test_a_zero_deadline_omits_the_field_rather_than_sending_zero(
    db_engine, server, allow_node_debug, monkeypatch,
):
    """`activeDeadlineSeconds: 0` is rejected by the API server. Unbounded has to
    mean an absent field, not a zero."""
    monkeypatch.setattr(settings, "node_debug_max_seconds", 0)
    create(dry_run=False)

    assert "activeDeadlineSeconds" not in created_pod(server)["spec"]


def test_the_pod_is_labelled_so_the_console_can_find_it_again(
    db_engine, server, allow_node_debug,
):
    """Nothing removes this pod automatically — `kubectl debug` has no `--rm`
    either — so being able to find it later is the whole removal story."""
    create(dry_run=False)
    metadata = created_pod(server)["metadata"]

    assert metadata["labels"][node_debug.COMPONENT_LABEL] == node_debug.COMPONENT_VALUE
    # The node in an annotation as well: a label value is capped at 63 characters
    # and a cloud provider's node name routinely exceeds it.
    assert metadata["annotations"][node_debug.NODE_ANNOTATION] == NODE


# --------------------------------------------------------------------------- #
# Names and validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "node",
    [
        "ip-10-0-1-4",
        "ip-10-0-1-4.eu-west-1.compute.internal",
        "a" * 250,
    ],
)
def test_every_generated_name_is_a_legal_object_name(node):
    """A node name is a DNS-1123 subdomain of up to 253 characters, so it cannot
    simply be concatenated into a pod name — the API server would refuse, and the
    operator would see a validation error about a field they never typed."""
    import re

    name = node_debug._pod_name(node)
    assert name.startswith(node_debug.NAME_PREFIX)
    assert len(name) <= 253
    assert re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name), name


def test_a_node_name_that_sanitises_away_still_produces_a_legal_name():
    """A node named only in characters a pod name cannot hold leaves nothing to
    put in the middle. The pod still needs a legal name — `spec.nodeName` is what
    actually pins it — so the prefix and the suffix carry it alone, with no
    double dash where the node used to be."""
    name = node_debug._pod_name("...")

    assert name.startswith(node_debug.NAME_PREFIX)
    assert len(name) == len(node_debug.NAME_PREFIX) + 5
    assert "--" not in name


def test_an_invalid_node_name_is_refused_before_anything_is_created(
    db_engine, server, allow_node_debug,
):
    with pytest.raises(Invalid) as caught:
        node_debug.create_node_debug_pod("Not A Node", dry_run=False)

    assert caught.value.context["parameter"] == "node"
    assert server.of("POST") == []


def test_an_image_with_whitespace_is_refused(db_engine, server, allow_node_debug):
    with pytest.raises(Invalid):
        create(image="busy box:1.36", dry_run=False)

    assert server.of("POST") == []


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def test_the_listing_returns_only_pods_on_this_node(db_engine, server, allow_node_debug):
    server.pods = [
        debug_pod_object(name="node-debugger-here-aaaaa", node=NODE),
        debug_pod_object(name="node-debugger-elsewhere-bbbbb", node="ip-10-0-9-9"),
    ]

    result = node_debug.list_node_debug_pods(NODE)

    assert [row["name"] for row in result["items"]] == ["node-debugger-here-aaaaa"]
    assert result["namespace"] == NAMESPACE
    assert result["enabled"] is True
    assert result["partial"] is False


def test_the_listing_selects_on_the_console_label(db_engine, server, allow_node_debug):
    node_debug.list_node_debug_pods(NODE)

    (request,) = server.of("GET")
    assert request.query.get("labelSelector") == node_debug.SELECTOR


def test_a_row_says_whether_the_host_filesystem_is_writable(
    db_engine, server, allow_node_debug,
):
    """Not visible from the pod's name or its phase, and it is the difference
    between a pod that can read the machine and one that can rewrite it."""
    server.pods = [
        debug_pod_object(name="node-debugger-ro-aaaaa", read_only=True),
        debug_pod_object(name="node-debugger-rw-bbbbb", read_only=False),
    ]

    rows = {r["name"]: r for r in node_debug.list_node_debug_pods(NODE)["items"]}

    assert rows["node-debugger-ro-aaaaa"]["hostFilesystemReadOnly"] is True
    assert rows["node-debugger-rw-bbbbb"]["hostFilesystemReadOnly"] is False


def test_a_truncated_listing_carries_its_continue_token(
    db_engine, server, allow_node_debug, monkeypatch,
):
    """§0.1's corollary, applied to pagination. A listing that stopped at the
    limit and reported `continue: null` would be saying "these are all of them"
    about a page — and the thing being under-reported here is host-mounted pods."""
    from app.resources import reader

    monkeypatch.setattr(
        reader, "list_resource",
        lambda *a, **k: {
            "items": [debug_pod_object(name="node-debugger-a-aaaaa")],
            "continue": "next-page-token",
            "remaining": 41,
            "partial": False,
            "unavailable": [],
        },
    )

    result = node_debug.list_node_debug_pods(NODE)

    assert result["continue"] == "next-page-token"
    assert result["remaining"] == 41


def test_an_empty_listing_is_a_real_zero(db_engine, server, allow_node_debug):
    result = node_debug.list_node_debug_pods(NODE)
    assert result["items"] == []
    assert result["partial"] is False


def test_the_listing_reports_the_gate_without_enforcing_it(db_engine, server, allow_mutations,
                                                           monkeypatch):
    """A read, so it answers even when creating is refused — that is what lets
    the UI disable the button *with the reason* rather than offering it."""
    monkeypatch.setattr(settings, "node_debug_enabled", False)
    monkeypatch.setattr(settings, "node_debug_namespace", NAMESPACE)

    result = node_debug.list_node_debug_pods(NODE)

    assert result["enabled"] is False
    assert "ADMIN_NODE_DEBUG_ENABLED" in result["enabledDetail"]


# --------------------------------------------------------------------------- #
# Removal
# --------------------------------------------------------------------------- #

def test_removal_deletes_a_pod_this_console_created(db_engine, server, allow_node_debug):
    server.live = debug_pod_object()

    result = node_debug.remove_node_debug_pod(
        NODE, "node-debugger-ip-10-0-1-4-x4k2p", dry_run=False,
    )

    (request,) = server.of("DELETE")
    assert request.path.endswith("/pods/node-debugger-ip-10-0-1-4-x4k2p")
    assert result["applied"] is True
    # §4 fixes a delete's diff as before=live, after=null — the whole manifest
    # disappearing, which is what a removal should be confirmed against.
    assert result["diff"]["after"] == ""


def test_removal_refuses_a_pod_this_console_did_not_create(
    db_engine, server, allow_node_debug,
):
    """Without this check the route would be a general pod-delete with a node in
    its path, bypassing the resource browser's own confirm dialog."""
    server.live = debug_pod_object(name="payments-7d9", labelled=False)

    with pytest.raises(NotFound) as caught:
        node_debug.remove_node_debug_pod(NODE, "payments-7d9", dry_run=False)

    assert node_debug.COMPONENT_LABEL in (caught.value.detail or "")
    assert server.of("DELETE") == []


def test_removal_refuses_a_debug_pod_belonging_to_another_node(
    db_engine, server, allow_node_debug,
):
    server.live = debug_pod_object(name="node-debugger-other-aaaaa", node="ip-10-0-9-9")

    with pytest.raises(NotFound):
        node_debug.remove_node_debug_pod(NODE, "node-debugger-other-aaaaa", dry_run=False)

    assert server.of("DELETE") == []


def test_removal_is_available_on_a_console_where_creating_is_not(
    db_engine, server, allow_mutations, monkeypatch,
):
    """Deliberate. A pod left behind after the gate was switched off is exactly
    the one that most needs removing, and the removal is an ordinary delete —
    it creates no privilege, it takes one away."""
    monkeypatch.setattr(settings, "node_debug_enabled", False)
    monkeypatch.setattr(settings, "node_debug_namespace", NAMESPACE)
    server.live = debug_pod_object()

    result = node_debug.remove_node_debug_pod(
        NODE, "node-debugger-ip-10-0-1-4-x4k2p", dry_run=False,
    )

    assert result["applied"] is True
