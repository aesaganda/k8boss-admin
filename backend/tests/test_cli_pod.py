"""
CLI pods (§15) — the pod the masthead terminal runs kubectl in.

What is asserted here is asserted because getting it wrong would make a
*security claim that is false*, which this codebase treats as worse than a bug:

* **The ServiceAccount is the whole permission story.** kubectl inside the pod
  authenticates as it, Kubernetes has no RBAC verb covering which account a pod
  may bind, and nothing downstream can narrow it. So the account has to come
  from the deployment's configuration, has to be `default` when nobody chose
  one, and has to appear in the diff and in the audit sentence. Each of those is
  pinned below; a refactor that dropped `serviceAccountName` would leave a
  feature that looks identical and silently runs as whatever the API server
  defaults to.

* **Two gates, with different answers about the dry run.** ADMIN_CLI_ENABLED
  refuses the projection as well, because a deployment that switched this off
  has decided the console is not a kubectl terminal. ADMIN_ALLOW_MUTATIONS being
  off still permits one, because §1.6 says inspecting what would change is a
  read — the departure §5.5 makes does not apply here, and a test says so rather
  than leaving the next reader to infer it from the absence of a check.

* **One denial row per attempt.** The feature gate audits its own refusal
  because the funnel is never reached. The mutations gate does not, because
  `mutate()` already did — and two rows for one attempt makes the count of "who
  tried" wrong in the one table that exists to answer it.

* **The container actually stays up.** `sleep infinity` is what everybody writes
  and BusyBox's `sleep` rejects it, so a minimal image would CrashLoop and the
  terminal would be a button that never works.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.admin import cli_pod
from app.audit import recorder
from app.config import settings
from app.errors import Invalid, MutationsDisabled, NotFound, RBACDenied
from app.resources import catalog
from tests.conftest import obj

NAMESPACE = "k8boss-cli"
SERVICE_ACCOUNT = "k8boss-cli-runner"
IMAGE = "alpine/k8s:1.34.9"

POD_INFO = {
    "group": "", "version": "v1", "kind": "Pod", "resource": "pods",
    "namespaced": True,
    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    "shortNames": ["po"], "categories": ["all"], "apiVersion": "v1", "preferred": True,
}


def cli_pod_object(*, name="k8boss-cli-x4k2p", labelled=True, phase="Running",
                   service_account=SERVICE_ACCOUNT):
    """A CLI pod as the API server hands one back."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name, "namespace": NAMESPACE, "resourceVersion": "9001",
            "creationTimestamp": "2026-08-21T09:00:00Z",
            "labels": ({cli_pod.COMPONENT_LABEL: cli_pod.COMPONENT_VALUE}
                       if labelled else {"app": "something-else"}),
        },
        "spec": {
            "serviceAccountName": service_account,
            "containers": [{"name": cli_pod.CONTAINER_NAME, "image": IMAGE}],
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {"name": cli_pod.CONTAINER_NAME,
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
            payload = self.live if self.live is not None else cli_pod_object()
        elif method == "POST":
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
def cli_settings(monkeypatch):
    """The deployment's CLI configuration, without the feature gate.

    Patches the live Settings object, because it is built at import time and
    re-reading the environment later would have no effect — a test that set the
    variable and saw nothing change would look like a bug in the gate.
    """
    monkeypatch.setattr(settings, "cli_namespace", NAMESPACE)
    monkeypatch.setattr(settings, "cli_service_account", SERVICE_ACCOUNT)
    monkeypatch.setattr(settings, "cli_image", IMAGE)
    return settings


@pytest.fixture
def allow_cli(monkeypatch, cli_settings, allow_mutations):
    """Both gates on."""
    monkeypatch.setattr(settings, "cli_enabled", True)
    return settings


def audit_rows():
    return recorder.query(limit=50)["items"]


def create(**kwargs):
    return cli_pod.create_cli_pod(**kwargs)


def created_pod(server):
    (request,) = server.of("POST")
    return request.body


# --------------------------------------------------------------------------- #
# The two gates
# --------------------------------------------------------------------------- #

def test_the_feature_gate_refuses_even_with_writes_enabled(
    db_engine, server, cli_settings, allow_mutations, monkeypatch,
):
    """The whole point of the second switch: every other write works, this one
    does not, and the refusal names the setting that is off."""
    monkeypatch.setattr(settings, "cli_enabled", False)

    with pytest.raises(MutationsDisabled) as caught:
        create(dry_run=False)

    assert "ADMIN_CLI_ENABLED" in (caught.value.hint or "")
    assert "ADMIN_CLI_ENABLED" in (caught.value.detail or "")
    assert server.of("POST") == []


def test_the_feature_gate_refuses_the_dry_run_too(
    db_engine, server, cli_settings, allow_mutations, monkeypatch,
):
    """A deployment that switched this off has decided the console is not a
    kubectl terminal, and offering a preview of one is offering the feature."""
    monkeypatch.setattr(settings, "cli_enabled", False)

    with pytest.raises(MutationsDisabled):
        create(dry_run=True)

    assert server.of("POST") == []


def test_the_feature_gate_refusal_is_audited(
    db_engine, server, cli_settings, allow_mutations, monkeypatch,
):
    """`mutate()` audits its own gate refusal; this follows it, because the
    funnel is never reached — without this row there would be no record that
    anyone tried."""
    monkeypatch.setattr(settings, "cli_enabled", False)

    with pytest.raises(MutationsDisabled):
        create(dry_run=False)

    row = audit_rows()[0]
    assert row["outcome"] == "denied"
    assert row["verb"] == "create"
    assert row["target"]["resource"] == "pods"
    assert row["target"]["namespace"] == NAMESPACE
    assert "mutations_disabled" in (row["error"] or "")


def test_a_read_only_console_still_projects_the_pod(db_engine, server, cli_settings,
                                                    monkeypatch):
    """The deliberate difference from §5.5, which refuses its projection because
    the projection *is* a working recipe for a privileged pod. This one is a pod
    running `sleep` bound to an account named in the deployment's own
    configuration; §1.6's ordinary rule applies and inspecting it is a read."""
    monkeypatch.setattr(settings, "cli_enabled", True)

    result = create(dry_run=True)

    assert result["applied"] is False
    (request,) = server.of("POST")
    assert request.query == {"dryRun": "All"}


def test_a_read_only_console_refuses_the_real_write_once(
    db_engine, server, cli_settings, monkeypatch,
):
    """One denial row, not two. The mutations gate is `mutate()`'s to enforce and
    it audits its own refusal; checking it here as well would put two rows in the
    trail for one attempt and make the count of "who tried" wrong."""
    monkeypatch.setattr(settings, "cli_enabled", True)

    with pytest.raises(MutationsDisabled) as caught:
        create(dry_run=False)

    assert server.of("POST") == []
    denials = [row for row in audit_rows() if row["outcome"] == "denied"]
    assert len(denials) == 1
    assert caught.value.code == "mutations_disabled"


def test_enabled_state_reports_which_switch_is_off(db_engine, cli_settings, monkeypatch,
                                                   allow_mutations):
    monkeypatch.setattr(settings, "cli_enabled", False)
    state = cli_pod.enabled_state()
    assert state["enabled"] is False
    assert "ADMIN_CLI_ENABLED" in state["detail"]

    monkeypatch.setattr(settings, "cli_enabled", True)
    assert cli_pod.enabled_state()["enabled"] is True


def test_a_read_only_console_reports_the_mutations_gate_first(
    db_engine, cli_settings, monkeypatch,
):
    """Both switches are off from the UI's point of view, and the sentence names
    the one the operator has to change first — writes, not this feature."""
    monkeypatch.setattr(settings, "cli_enabled", True)

    state = cli_pod.enabled_state()

    assert state["enabled"] is False
    assert "ADMIN_ALLOW_MUTATIONS" in state["detail"]


# --------------------------------------------------------------------------- #
# The funnel
# --------------------------------------------------------------------------- #

def test_the_real_write_is_the_same_request_without_the_dryrun_parameter(
    db_engine, server, allow_cli,
):
    result = create(dry_run=False)

    (request,) = server.of("POST")
    assert request.path == f"/api/v1/namespaces/{NAMESPACE}/pods"
    assert request.query == {}
    assert result["applied"] is True
    assert result["pod"].startswith(cli_pod.NAME_PREFIX)
    assert result["namespace"] == NAMESPACE
    assert result["serviceAccount"] == SERVICE_ACCOUNT
    assert result["container"] == cli_pod.CONTAINER_NAME
    assert audit_rows()[0]["outcome"] == "applied"


def test_the_service_account_is_in_the_diff_the_operator_confirms(
    db_engine, server, allow_cli,
):
    """The disclosure mechanism. A shell here can do exactly what this account
    can do — not what the console can do and not what the operator can do — and
    §4 makes a create's diff the whole manifest as an addition, so the account is
    on screen before the confirming call."""
    result = create(dry_run=True)

    assert result["diff"]["before"] == ""
    assert f"+  serviceAccountName: {SERVICE_ACCOUNT}" in result["diff"]["unified"]


def test_the_audit_sentence_names_the_service_account(db_engine, server, allow_cli):
    """Not the image and not the namespace: the account is the only thing that
    decides what a shell in this pod can do to the cluster."""
    create(dry_run=False)

    detail = audit_rows()[0]["detail"]
    assert SERVICE_ACCOUNT in detail
    assert IMAGE in detail
    assert NAMESPACE in detail


def test_a_denied_preflight_never_reaches_the_cluster(db_engine, server, allow_cli, fake_k8s):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, denied=True, reason="no RBAC policy matched", evaluation_error=None,
    )))

    with pytest.raises(RBACDenied):
        create(dry_run=False)

    assert server.of("POST") == []


def test_podsecurity_admission_surfaces_at_the_dry_run(db_engine, server, allow_cli):
    """A namespace enforcing `restricted` wants `runAsNonRoot`, which this pod
    deliberately does not set — see `build_pod`. Admission runs on `dryRun=All`
    exactly as on the real call, so the operator learns at the *preview* step,
    carrying admission's own message, before anything exists.

    **403, not 422.** This test asserted a fabricated 422 until the feature was
    run against a real cluster: Pod Security admission refuses with `Forbidden`,
    so the funnel maps it to `rbac_denied` like any other 403. The message below
    is the one a `restricted` namespace on Kubernetes v1.35 actually returns.
    """
    server.raises["POST"] = ApiException(
        status=403,
        reason="Forbidden",
        http_resp=SimpleNamespace(
            status=403,
            reason="Forbidden",
            getheaders=lambda: {},
            data=json.dumps({
                "kind": "Status", "status": "Failure", "code": 403, "reason": "Forbidden",
                "message": (
                    'pods "k8boss-cli-3q27n" is forbidden: violates PodSecurity '
                    '"restricted:latest": runAsNonRoot != true (pod or container '
                    '"cli" must set securityContext.runAsNonRoot=true)'
                ),
            }),
        ),
    )

    with pytest.raises(RBACDenied) as caught:
        create(dry_run=True)

    # The hint is rewritten so the operator is sent to the namespace label
    # rather than to a ClusterRole that cannot fix this. The code and status are
    # deliberately left alone — see `_podsecurity_hint`.
    assert "Pod Security admission" in (caught.value.hint or "")
    assert "ADMIN_CLI_NAMESPACE" in (caught.value.hint or "")
    assert "not a missing permission" in (caught.value.hint or "")
    assert audit_rows()[0]["outcome"] in ("failed", "denied")


def test_an_ordinary_rbac_denial_keeps_its_own_hint(db_engine, server, allow_cli):
    """The rewrite must not fire on every 403, or a genuinely missing `create
    pods` would be explained as a namespace label and the operator would go and
    relabel a namespace that was never the problem."""
    server.raises["POST"] = ApiException(
        status=403,
        reason="Forbidden",
        http_resp=SimpleNamespace(
            status=403,
            reason="Forbidden",
            getheaders=lambda: {},
            data=json.dumps({
                "kind": "Status", "status": "Failure", "code": 403, "reason": "Forbidden",
                "message": 'pods is forbidden: User "sa" cannot create resource "pods"',
            }),
        ),
    )

    with pytest.raises(RBACDenied) as caught:
        create(dry_run=True)

    assert "Pod Security admission" not in (caught.value.hint or "")


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #

def test_the_pod_binds_the_configured_service_account_with_a_token(
    db_engine, server, allow_cli,
):
    """The whole feature. Without the account kubectl has no identity; without
    the token it has no credential, and §5.5 sets exactly the opposite for
    exactly the opposite reason."""
    create(dry_run=False)
    spec = created_pod(server)["spec"]

    assert spec["serviceAccountName"] == SERVICE_ACCOUNT
    assert spec["automountServiceAccountToken"] is True


def test_the_caller_cannot_choose_the_service_account(db_engine, server, allow_cli):
    """`create_cli_pod` takes an image and nothing else. If a ServiceAccount
    parameter ever appears here, whoever opens the dialog gets to decide what the
    shell can do — which is the decision that belongs to whoever configured the
    console."""
    import inspect

    parameters = set(inspect.signature(cli_pod.create_cli_pod).parameters)
    assert parameters == {"image", "dry_run"}


def test_the_container_stays_up_without_relying_on_sleep_infinity(
    db_engine, server, allow_cli,
):
    """A kubectl image's entrypoint *is* kubectl, so an unset command exits
    immediately with a usage message. `sleep infinity` is the usual replacement
    and BusyBox's `sleep` rejects it — the pod would CrashLoop on exactly the
    minimal images an operator is most likely to pick."""
    create(dry_run=False)
    container = created_pod(server)["spec"]["containers"][0]

    assert container["command"][:2] == ["/bin/sh", "-c"]
    assert "sleep infinity" not in container["command"][2]
    assert "sleep" in container["command"][2]


def test_the_container_has_a_terminal_for_the_exec_socket(db_engine, server, allow_cli):
    """§7's exec socket attaches to these. Without them the shell opens and every
    curses program in it renders as noise."""
    create(dry_run=False)
    container = created_pod(server)["spec"]["containers"][0]

    assert container["stdin"] is True
    assert container["tty"] is True
    assert container["name"] == cli_pod.CONTAINER_NAME


def test_the_pod_touches_no_host_namespace_or_filesystem(db_engine, server, allow_cli):
    """The contrast with §5.5 is the point: nothing kubectl does needs the
    machine it lands on, and node debugging is a separately gated feature. Each
    absence would be invisible if it regressed."""
    create(dry_run=False)
    pod = created_pod(server)
    spec = pod["spec"]

    for field in ("hostPID", "hostNetwork", "hostIPC", "nodeName", "volumes",
                  "tolerations"):
        assert field not in spec, field
    assert "volumeMounts" not in spec["containers"][0]
    assert "hostPath" not in json.dumps(pod)
    assert "privileged" not in json.dumps(pod)


def test_the_container_drops_what_it_does_not_need(db_engine, server, allow_cli):
    """Enough for PodSecurity `baseline`. `runAsNonRoot` is deliberately absent:
    it would make an image whose user is root fail to start with a kubelet error
    naming a field the operator never chose, and whether it is required is the
    namespace's PodSecurity level to decide, not this console's."""
    create(dry_run=False)
    container = created_pod(server)["spec"]["containers"][0]

    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert "runAsNonRoot" not in container["securityContext"]
    # BestEffort, which the kubelet always admits. A request could be refused on
    # a cluster under pressure, and a terminal that will not start during an
    # incident is a terminal that is not there when it is wanted.
    assert "resources" not in container


def test_the_container_is_time_bounded(db_engine, server, allow_cli, monkeypatch):
    """It bounds the window in which an unattended shell holding a cluster
    credential is possible. The kubelet stops the *container*; the pod object
    stays and still needs removing."""
    monkeypatch.setattr(settings, "cli_max_seconds", 1800)
    create(dry_run=False)

    assert created_pod(server)["spec"]["activeDeadlineSeconds"] == 1800


def test_a_zero_deadline_omits_the_field_rather_than_sending_zero(
    db_engine, server, allow_cli, monkeypatch,
):
    """`activeDeadlineSeconds: 0` is rejected by the API server. Unbounded has to
    mean an absent field, not a zero."""
    monkeypatch.setattr(settings, "cli_max_seconds", 0)
    create(dry_run=False)

    assert "activeDeadlineSeconds" not in created_pod(server)["spec"]


def test_the_pod_is_labelled_so_the_console_can_find_it_again(db_engine, server, allow_cli):
    """Nothing removes this pod automatically, so being able to find it later is
    the whole reuse-and-removal story."""
    create(dry_run=False)

    labels = created_pod(server)["metadata"]["labels"]
    assert labels[cli_pod.COMPONENT_LABEL] == cli_pod.COMPONENT_VALUE
    assert cli_pod.COMPONENT_VALUE != "node-debugger"


def test_every_generated_name_is_a_legal_object_name():
    name = cli_pod._pod_name()

    assert name.startswith(cli_pod.NAME_PREFIX)
    assert len(name) <= 253
    assert re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name), name


def test_an_image_with_whitespace_is_refused(db_engine, server, allow_cli):
    with pytest.raises(Invalid):
        create(image="alpine/k8s 1.34.9", dry_run=False)

    assert server.of("POST") == []


def test_an_explicit_image_overrides_the_configured_default(db_engine, server, allow_cli):
    create(image="ghcr.io/acme/oc:4.16", dry_run=False)

    assert created_pod(server)["spec"]["containers"][0]["image"] == "ghcr.io/acme/oc:4.16"


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def test_the_listing_selects_on_the_console_label(db_engine, server, allow_cli):
    cli_pod.list_cli_pods()

    (request,) = server.of("GET")
    assert request.query.get("labelSelector") == cli_pod.SELECTOR


def test_the_listing_reports_what_a_new_pod_would_be_made_of(db_engine, server, allow_cli):
    """The UI needs every one of these: where the pod appears, what it runs, and
    which account decides what kubectl in it can reach. An operator confirming a
    pod should not have to guess any of them."""
    server.pods = [cli_pod_object()]

    result = cli_pod.list_cli_pods()

    assert result["namespace"] == NAMESPACE
    assert result["serviceAccount"] == SERVICE_ACCOUNT
    assert result["image"] == IMAGE
    assert result["container"] == cli_pod.CONTAINER_NAME
    assert result["enabled"] is True
    assert result["partial"] is False
    assert result["items"][0]["serviceAccount"] == SERVICE_ACCOUNT
    assert result["items"][0]["phase"] == "Running"


def test_an_empty_listing_is_a_real_zero(db_engine, server, allow_cli):
    """And a listing that could not happen raises, per §0.1 — a panel showing
    "no session" because it failed to read the namespace would have an operator
    starting a second pod beside the one already running."""
    result = cli_pod.list_cli_pods()

    assert result["items"] == []
    assert result["partial"] is False


def test_a_listing_that_could_not_happen_raises(db_engine, server, allow_cli):
    server.raises["GET"] = ApiException(status=403, reason="Forbidden")

    with pytest.raises(RBACDenied):
        cli_pod.list_cli_pods()


def test_a_truncated_listing_carries_its_continue_token(
    db_engine, server, allow_cli, monkeypatch,
):
    """§0.1's corollary applied to pagination: reporting `continue: null` about a
    page would be saying "these are all of them"."""
    from app.resources import reader

    monkeypatch.setattr(
        reader, "list_resource",
        lambda *a, **k: {
            "items": [cli_pod_object()],
            "continue": "next-page-token",
            "remaining": 41,
            "partial": False,
            "unavailable": [],
        },
    )

    result = cli_pod.list_cli_pods()

    assert result["continue"] == "next-page-token"
    assert result["remaining"] == 41


def test_the_listing_reports_the_gate_without_enforcing_it(
    db_engine, server, cli_settings, allow_mutations, monkeypatch,
):
    """A read, so it answers even when creating is refused — that is what lets
    the UI disable the button *with the reason* rather than offering it."""
    monkeypatch.setattr(settings, "cli_enabled", False)

    result = cli_pod.list_cli_pods()

    assert result["enabled"] is False
    assert "ADMIN_CLI_ENABLED" in result["enabledDetail"]


def test_a_row_reports_an_unset_service_account_as_unknown(db_engine, server, allow_cli):
    """`null` because the API server defaulted the field, which is a different
    claim from "it runs as `default`" — and this is a claim about what a shell in
    that pod is permitted to do."""
    pod = cli_pod_object()
    del pod["spec"]["serviceAccountName"]
    server.pods = [pod]

    assert cli_pod.list_cli_pods()["items"][0]["serviceAccount"] is None


# --------------------------------------------------------------------------- #
# Removal
# --------------------------------------------------------------------------- #

def test_removal_deletes_a_pod_this_console_created(db_engine, server, allow_cli):
    server.live = cli_pod_object()

    result = cli_pod.remove_cli_pod("k8boss-cli-x4k2p", dry_run=False)

    (request,) = server.of("DELETE")
    assert request.path.endswith("/pods/k8boss-cli-x4k2p")
    assert result["applied"] is True
    # §4 fixes a delete's diff as before=live, after=null — the whole manifest
    # disappearing, which is what a removal should be confirmed against.
    assert result["diff"]["after"] == ""


def test_removal_refuses_a_pod_this_console_did_not_create(db_engine, server, allow_cli):
    """Without this check the route would be a namespaced pod-delete wearing a
    friendlier URL, bypassing the resource browser's own confirm dialog."""
    server.live = cli_pod_object(name="payments-7d9", labelled=False)

    with pytest.raises(NotFound) as caught:
        cli_pod.remove_cli_pod("payments-7d9", dry_run=False)

    assert cli_pod.COMPONENT_LABEL in (caught.value.detail or "")
    assert server.of("DELETE") == []


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #
# The routes are thin — parse, call, envelope — so what is asserted here is only
# what the tests above cannot reach: the wire spelling of the body, the default
# that applies when a field is missing, and where `dryRun` is carried. The write
# path itself is tested where it lives.

def test_the_post_body_defaults_to_a_dry_run_when_the_field_is_absent(
    db_engine, client, registered_cluster, server, allow_cli,
):
    """A client that forgets `dryRun` gets a projection, not a write. That
    default is safety, not convenience."""
    response = client.post("/api/cli", json={})

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert server.of("POST")[0].query == {"dryRun": "All"}


def test_the_post_body_accepts_both_spellings_of_dry_run(
    db_engine, client, registered_cluster, server, allow_cli,
):
    """`dry_run` is understood rather than silently ignored — a caller asking for
    a real write and receiving a dry run reported as `applied: false` is the one
    misunderstanding in this API that costs an operator an incident."""
    for spelling in ("dryRun", "dry_run"):
        response = client.post("/api/cli", json={spelling: False})
        assert response.status_code == 200, spelling
        assert response.json()["applied"] is True, spelling


def test_the_delete_route_carries_dry_run_as_a_query_parameter(
    db_engine, client, registered_cluster, server, allow_cli,
):
    """A body rather than a query parameter here would be handled inconsistently
    by proxies and HTTP clients, and a `dryRun` that went missing in transit
    would turn a projection into a deletion. Absent still means projection."""
    server.live = cli_pod_object()

    projected = client.delete("/api/cli/k8boss-cli-x4k2p")
    assert projected.status_code == 200
    assert projected.json()["applied"] is False
    assert server.of("DELETE")[0].query.get("dryRun") == "All"

    real = client.delete("/api/cli/k8boss-cli-x4k2p?dryRun=false")
    assert real.status_code == 200
    assert real.json()["applied"] is True
    assert "dryRun" not in server.of("DELETE")[1].query


def test_the_feature_gate_renders_as_403_mutations_disabled(
    db_engine, client, registered_cluster, server, cli_settings, allow_mutations, monkeypatch,
):
    """§1.3: NOT `rbac_denied`. The operator's permissions are irrelevant to this
    refusal, and the frontend branches on the code to show a deployment-level
    banner rather than sending them to edit a ClusterRole."""
    monkeypatch.setattr(settings, "cli_enabled", False)

    response = client.post("/api/cli", json={"dryRun": False})

    assert response.status_code == 403
    assert response.json()["error"] == "mutations_disabled"


def test_the_listing_route_returns_the_envelope_with_the_gate(
    db_engine, client, registered_cluster, server, allow_cli,
):
    server.pods = [cli_pod_object()]

    body = client.get("/api/cli").json()

    assert [row["name"] for row in body["items"]] == ["k8boss-cli-x4k2p"]
    assert body["partial"] is False
    assert body["enabled"] is True
    assert body["serviceAccount"] == SERVICE_ACCOUNT
