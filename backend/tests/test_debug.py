"""
Debug containers (§7.4) — ``kubectl debug`` through the write funnel.

What this file pins down, and why each one is worth a test rather than a comment:

* **It is a write, and it goes through the funnel.** The gate, the preflight on
  ``pods/ephemeralcontainers``, the dry run, the diff and the audit row are the
  funnel's, and the test that would catch a future refactor routing around it is
  the one asserting the *subresource* on the preflight and the audit target.

* **A cluster that does not serve ephemeral containers is told so, from
  discovery.** The API server answers a request for a subresource it does not
  serve with 404, and 404 maps to ``not_found`` — which reads as "that pod is
  gone" about a pod the operator is looking at. Asked of discovery instead, and
  asserted here including that nothing was patched.

* **Support is three-valued.** Discovery that could not be read is ``None``, not
  ``False``. "This cluster cannot do it" and "we could not find out" send an
  operator to two different places and only one of them is an upgrade.

* **Names are checked against every container the pod has.** Regular, init and
  ephemeral share one namespace of names, and the API server's duplicate error
  does not say which one was duplicated — which is the difference between a typo
  and somebody else already being in this pod.

* **A debug container can be exec'd into.** :func:`app.api.logs.resolve_container`
  validates a named container against the pod, and until §7.4 it did not know
  ephemeral containers existed — so attaching one and then trying to open a
  shell in it would have been refused by this console with "that container does
  not exist", about a container it had just created.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.admin import debug as debug_service
from app.api import logs as logs_api
from app.audit import recorder
from app.errors import Invalid, MutationsDisabled, RBACDenied, Unsupported
from tests.conftest import obj

NAMESPACE = "prod"
POD = "checkout-7d9f6c-abcde"

#: The core group's discovery document, trimmed to the two entries that matter.
#: `pods/log` is here so a test that removes `pods/ephemeralcontainers` still
#: leaves a plausible document behind — an empty resource list would also make
#: the "unsupported" assertion pass, for the wrong reason.
DISCOVERY = {
    "kind": "APIResourceList",
    "groupVersion": "v1",
    "resources": [
        {"name": "pods", "namespaced": True, "kind": "Pod",
         "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"name": "pods/log", "namespaced": True, "kind": "Pod", "verbs": ["get"]},
        {"name": "pods/ephemeralcontainers", "namespaced": True, "kind": "Pod",
         "verbs": ["get", "patch", "update"]},
    ],
}

DISCOVERY_WITHOUT_EPHEMERAL = {
    **DISCOVERY,
    "resources": [r for r in DISCOVERY["resources"] if r["name"] != "pods/ephemeralcontainers"],
}


def live_pod(*, ephemeral=(), phase="Running", statuses=()):
    """The pod as the API server hands it back, as a plain JSON dict.

    A dict rather than a ``V1Pod`` because the write path deliberately reaches
    past the typed clients — see :mod:`app.admin.apply` — so this is the shape
    the code under test actually receives.
    """
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": POD, "namespace": NAMESPACE, "resourceVersion": "884213",
            "managedFields": [{"manager": "kubelet"}],
        },
        "spec": {
            "containers": [{"name": "app", "image": "ghcr.io/acme/checkout:1.9.2"},
                           {"name": "envoy", "image": "envoyproxy/envoy:v1.29"}],
            "initContainers": [{"name": "migrate", "image": "ghcr.io/acme/migrate:1.9.2"}],
        },
        "status": {"phase": phase},
    }
    if ephemeral:
        pod["spec"]["ephemeralContainers"] = list(ephemeral)
    if statuses:
        pod["status"]["ephemeralContainerStatuses"] = list(statuses)
    return pod


class FakeApiServer:
    """A stand-in for ``ApiClient.call_api`` that answers per path.

    Unlike ``tests/test_apply.py``'s, this one has to distinguish three GETs —
    core discovery, the pod, and the pod's ``ephemeralcontainers`` subresource —
    because §7.4 reads all three and answering them identically would let a test
    pass while the code read the wrong one.

    The PATCH response is *computed* from the request body: the API server's own
    projection of a strategic merge on ``spec.ephemeralContainers`` appends by
    the ``name`` merge key, and a projection that ignored the body would make the
    diff assertions vacuous.
    """

    def __init__(self, *, pod=None, discovery=None):
        self.requests: list[SimpleNamespace] = []
        self.pod = pod if pod is not None else live_pod()
        self.discovery = discovery if discovery is not None else DISCOVERY
        self.raises: dict[str, BaseException] = {}

    def __call__(self, path, method, **kwargs):
        query = dict(kwargs.get("query_params") or [])
        self.requests.append(SimpleNamespace(
            path=path, method=method, query=query, body=kwargs.get("body"),
            content_type=(kwargs.get("header_params") or {}).get("Content-Type"),
        ))
        if path in self.raises:
            raise self.raises[path]
        if method in self.raises:
            raise self.raises[method]

        if method == "GET" and path == "/api/v1":
            payload = self.discovery
        elif method == "GET":
            payload = self.pod
        elif method == "PATCH":
            payload = self._project(kwargs.get("body") or {})
        else:  # pragma: no cover - the module issues no other method
            raise AssertionError(f"unexpected {method} {path}")

        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, {}

    def _project(self, body):
        added = ((body.get("spec") or {}).get("ephemeralContainers") or [])
        existing = list((self.pod.get("spec") or {}).get("ephemeralContainers") or [])
        by_name = {c["name"]: c for c in existing}
        for container in added:
            by_name[container["name"]] = {**by_name.get(container["name"], {}), **container}
        projected = json.loads(json.dumps(self.pod))
        projected["spec"]["ephemeralContainers"] = list(by_name.values())
        projected["metadata"]["resourceVersion"] = "884214"
        return projected

    def of(self, method):
        return [request for request in self.requests if request.method == method]


@pytest.fixture
def server(monkeypatch, fake_k8s):
    """A fake API server with discovery served and the access review answered."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer()
    fake_k8s.api_client.returns("call_api", api_server)
    return api_server


@pytest.fixture
def deny(fake_k8s):
    """Flip the access review to a clean denial."""
    def apply() -> None:
        fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
            allowed=False, denied=True, reason="no RBAC policy matched", evaluation_error=None,
        )))
    return apply


def audit_rows():
    return recorder.query(limit=50)["items"]


def attach(**kwargs):
    defaults = {"image": "busybox:1.36", "dry_run": True}
    return debug_service.attach_debug_container(NAMESPACE, POD, **{**defaults, **kwargs})


# --------------------------------------------------------------------------- #
# The funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_projects_the_write_and_reports_applied_false(db_engine, server):
    """§0.3: a successful dry run returns a full object, a resourceVersion and a
    diff — everything that looks like success. `applied` is the only evidence."""
    result = attach(container="debugger-fixed")

    (request,) = server.of("PATCH")
    assert request.path == f"/api/v1/namespaces/{NAMESPACE}/pods/{POD}/ephemeralcontainers"
    assert request.query == {"dryRun": "All"}
    assert result["applied"] is False
    assert result["dryRun"] is True
    assert result["container"] == "debugger-fixed"
    assert "+  - name: debugger-fixed" in result["diff"]["unified"]
    assert result["diff"]["changed"] is True
    assert audit_rows()[0]["outcome"] == "dry_run"


def test_the_real_write_is_the_same_request_without_the_dryrun_parameter(
    db_engine, server, allow_mutations,
):
    """§0.3: `dryRun=All` is a query parameter on the same call, not a second
    code path. A preview that took a different route would eventually preview
    something else."""
    result = attach(container="debugger-fixed", dry_run=False)

    (request,) = server.of("PATCH")
    assert request.query == {}
    assert request.path == f"/api/v1/namespaces/{NAMESPACE}/pods/{POD}/ephemeralcontainers"
    assert result["applied"] is True
    assert audit_rows()[0]["outcome"] == "applied"


def test_the_patch_is_a_strategic_merge_so_a_second_debug_container_appends(
    db_engine, fake_k8s, allow_mutations,
):
    """A plain merge patch replaces a list wholesale, so attaching a second debug
    container would delete the first from the manifest — an operation the API
    server then rejects, because ephemeral containers cannot be removed."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    existing = {"name": "debugger-first", "image": "busybox:1.36"}
    api_server = FakeApiServer(pod=live_pod(ephemeral=[existing]))
    fake_k8s.api_client.returns("call_api", api_server)

    result = debug_service.attach_debug_container(
        NAMESPACE, POD, image="busybox:1.36", container="debugger-second", dry_run=False,
    )

    (request,) = api_server.of("PATCH")
    assert request.content_type == "application/strategic-merge-patch+json"
    assert [c["name"] for c in request.body["spec"]["ephemeralContainers"]] == ["debugger-second"]
    # And the projection the operator confirms keeps both, which is what the
    # merge key buys.
    assert "debugger-first" in result["diff"]["after"]
    assert "debugger-second" in result["diff"]["after"]


def test_the_console_being_read_only_refuses_the_write_before_the_cluster_is_touched(
    db_engine, server,
):
    """§1.6. The gate is the deployment's, not the operator's, so the error is
    `mutations_disabled` and not `rbac_denied` — telling them otherwise sends
    them to fix a ClusterRole that is already correct."""
    with pytest.raises(MutationsDisabled):
        attach(dry_run=False)

    assert server.of("PATCH") == []
    assert audit_rows()[0]["outcome"] == "denied"


def test_a_dry_run_is_still_permitted_on_a_read_only_console(db_engine, server):
    """Inspecting what would change is a read, and that is what makes this
    console useful in an audit posture."""
    result = attach()
    assert result["applied"] is False
    assert len(server.of("PATCH")) == 1


def test_the_preflight_names_the_subresource_rbac_actually_names(db_engine, server):
    attach(container="debugger-fixed")

    # The body is passed positionally by `app.admin.preflight._review`.
    (args, _kwargs), = server_review_calls(server)
    resource = args[0].spec.resource_attributes
    assert resource.resource == "pods"
    assert resource.subresource == "ephemeralcontainers"
    assert resource.verb == "patch"
    assert resource.namespace == NAMESPACE


def server_review_calls(server):  # noqa: ARG001 - reads the fake bundle, not the server
    from app.k8s.client import manager
    return manager.get_clients().authorization_v1.called("create_self_subject_access_review")


def test_a_denied_preflight_is_a_403_that_never_reaches_the_cluster(db_engine, server, deny):
    deny()

    with pytest.raises(RBACDenied) as caught:
        attach()

    assert "ephemeralcontainers" in (caught.value.hint or "")
    assert server.of("PATCH") == []
    assert audit_rows()[0]["outcome"] == "denied"


def test_the_audit_row_names_the_image_that_was_put_in_the_pod(
    db_engine, server, allow_mutations,
):
    """The question an incident review asks about a debug container is not that
    one was attached but *what was put inside somebody's production pod*."""
    attach(
        container="debugger-fixed", image="ghcr.io/acme/netshoot:1.2",
        target_container="app", command=["sleep", "3600"], dry_run=False,
    )

    row = audit_rows()[0]
    assert "ghcr.io/acme/netshoot:1.2" in row["detail"]
    assert "targeting app" in row["detail"]
    assert "sleep 3600" in row["detail"]
    assert row["target"]["subresource"] == "ephemeralcontainers"
    assert row["target"]["resource"] == "pods"
    assert row["verb"] == "patch"


# --------------------------------------------------------------------------- #
# The container that gets attached
# --------------------------------------------------------------------------- #

def test_stdin_is_always_on_and_the_probe_fields_are_never_sent(db_engine, server):
    """A debug container with `stdin: false` running a shell reads EOF and
    terminates, and the operator watches a container they just created go
    straight to Completed with no explanation. The probe and resource fields are
    rejected outright by the API server for an ephemeral container."""
    attach(container="debugger-fixed")

    (request,) = server.of("PATCH")
    (spec,) = request.body["spec"]["ephemeralContainers"]
    assert spec["stdin"] is True
    assert spec["tty"] is True
    assert spec["imagePullPolicy"] == "IfNotPresent"
    for forbidden in ("resources", "ports", "livenessProbe", "readinessProbe", "lifecycle"):
        assert forbidden not in spec


def test_an_empty_command_is_omitted_rather_than_sent_as_an_empty_list(db_engine, server):
    """`command: []` clears the image's entrypoint, and a debug container with no
    command exits immediately."""
    attach(container="debugger-fixed", command=[])

    (request,) = server.of("PATCH")
    (spec,) = request.body["spec"]["ephemeralContainers"]
    assert "command" not in spec


def test_a_generated_name_is_recognisable_and_avoids_the_names_already_taken(
    db_engine, fake_k8s,
):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer(pod=live_pod(ephemeral=[{"name": "debugger-taken"}]))
    fake_k8s.api_client.returns("call_api", api_server)

    result = debug_service.attach_debug_container(NAMESPACE, POD, image="busybox:1.36")

    assert result["container"].startswith(debug_service.NAME_PREFIX)
    assert result["container"] not in ("debugger-taken", "app", "envoy", "migrate")


def test_a_name_that_collides_says_which_containers_the_pod_already_has(db_engine, server):
    """The API server refuses a duplicate too, with a message about
    `spec.ephemeralContainers[1].name` that does not say what it duplicates —
    and the answer matters: colliding with the app's container is a typo,
    colliding with an earlier debug container means somebody else is in here."""
    with pytest.raises(Invalid) as caught:
        attach(container="envoy")

    assert "already has a container named" in caught.value.message
    assert "app" in (caught.value.detail or "")
    assert "migrate" in (caught.value.detail or "")
    assert server.of("PATCH") == []


def test_a_name_that_is_not_a_dns_label_is_refused_with_the_rule(db_engine, server):
    with pytest.raises(Invalid) as caught:
        attach(container="Debugger_1")

    assert "DNS-1123" in (caught.value.detail or "")
    assert server.of("PATCH") == []


def test_a_target_container_the_pod_does_not_have_is_refused_naming_the_ones_it_does(
    db_engine, server,
):
    with pytest.raises(Invalid) as caught:
        attach(target_container="sidecar")

    assert "no container named" in caught.value.message
    assert "app, envoy" in (caught.value.detail or "")
    assert server.of("PATCH") == []


def test_an_init_container_cannot_be_targeted(db_engine, server):
    """It has exited. The manifest would accept the name and the runtime would
    quietly do nothing, which is worse than a refusal."""
    with pytest.raises(Invalid):
        attach(target_container="migrate")

    assert server.of("PATCH") == []


def test_a_blank_image_is_refused_before_the_pod_is_read(db_engine, server):
    with pytest.raises(Invalid) as caught:
        attach(image="   ")

    assert caught.value.context["parameter"] == "image"
    assert server.requests == []


def test_an_image_with_a_line_break_inside_it_is_named_as_a_paste_accident(db_engine, server):
    with pytest.raises(Invalid) as caught:
        attach(image="ghcr.io/acme/net\nshoot:1.2")

    assert "whitespace" in caught.value.message
    assert server.of("PATCH") == []


def test_a_finished_pod_is_refused_because_the_container_would_never_start(db_engine, fake_k8s):
    """The API server accepts the write regardless, and the operator watches a
    container sit in `waiting` forever on a pod that exited last Tuesday."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer(pod=live_pod(phase="Succeeded"))
    fake_k8s.api_client.returns("call_api", api_server)

    with pytest.raises(Invalid) as caught:
        debug_service.attach_debug_container(NAMESPACE, POD, image="busybox:1.36")

    assert "already finished" in caught.value.message
    assert api_server.of("PATCH") == []


def test_too_many_command_arguments_are_refused_with_the_limit(db_engine, server):
    with pytest.raises(Invalid) as caught:
        attach(command=["sh"] * (debug_service.MAX_COMMAND_ARGS + 1))

    assert caught.value.context["limit"] == debug_service.MAX_COMMAND_ARGS
    assert server.of("PATCH") == []


# --------------------------------------------------------------------------- #
# Whether the cluster can do this at all
# --------------------------------------------------------------------------- #

def test_a_cluster_that_does_not_serve_the_subresource_is_unsupported_not_not_found(
    db_engine, fake_k8s,
):
    """The API server answers 404 for a subresource it does not serve, and 404
    maps to `not_found` — which reads as "that pod is gone" about a pod the
    operator is looking at, and sends them hunting for a deletion that never
    happened."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    api_server = FakeApiServer(discovery=DISCOVERY_WITHOUT_EPHEMERAL)
    fake_k8s.api_client.returns("call_api", api_server)

    with pytest.raises(Unsupported) as caught:
        debug_service.attach_debug_container(NAMESPACE, POD, image="busybox:1.36")

    assert "does not serve ephemeral containers" in caught.value.message
    assert caught.value.http_status == 501
    assert api_server.of("PATCH") == []


def test_a_subresource_that_cannot_be_patched_is_named_with_its_verbs(db_engine, fake_k8s):
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    discovery = {
        **DISCOVERY,
        "resources": [
            {**r, "verbs": ["get", "update"]} if r["name"] == "pods/ephemeralcontainers" else r
            for r in DISCOVERY["resources"]
        ],
    }
    api_server = FakeApiServer(discovery=discovery)
    fake_k8s.api_client.returns("call_api", api_server)

    with pytest.raises(Unsupported) as caught:
        debug_service.attach_debug_container(NAMESPACE, POD, image="busybox:1.36")

    assert "get, update" in (caught.value.detail or "")


def test_discovery_that_could_not_be_read_is_unknown_and_the_request_proceeds(
    db_engine, server,
):
    """"We could not find out" is not "this cluster cannot do it". The first
    sends an operator to look at their API server; the second sends them to plan
    an upgrade they may not need. Unknown lets the API server judge."""
    server.raises["/api/v1"] = ApiException(status=403, reason="Forbidden")

    result = attach(container="debugger-fixed")

    assert result["applied"] is False
    assert len(server.of("PATCH")) == 1, "an unknown answer must not block the write"


def test_support_reports_none_rather_than_false_when_discovery_fails(db_engine, server):
    server.raises["/api/v1"] = ApiException(status=503, reason="Service Unavailable")

    state = debug_service.support()

    assert state["supported"] is None
    assert state["verbs"] is None
    assert "unknown" in state["detail"]


# --------------------------------------------------------------------------- #
# Listing what is already there
# --------------------------------------------------------------------------- #

def test_the_listing_joins_the_spec_to_the_status(db_engine, server):
    server.pod = live_pod(
        ephemeral=[{"name": "debugger-x4k2p", "image": "busybox:1.36",
                    "targetContainerName": "app", "tty": True}],
        statuses=[{"name": "debugger-x4k2p",
                   "state": {"running": {"startedAt": "2026-08-21T09:14:00Z"}}}],
    )

    result = debug_service.list_debug_containers(NAMESPACE, POD)

    (row,) = result["items"]
    assert row["name"] == "debugger-x4k2p"
    assert row["image"] == "busybox:1.36"
    assert row["targetContainer"] == "app"
    # The same word the pod rows use for the same fact, from the same shaper.
    assert row["state"] == "Running"
    assert row["started_at"] == "2026-08-21T09:14:00Z"
    assert result["partial"] is False
    assert result["supported"] is True


def test_a_debug_container_the_kubelet_has_not_reported_on_has_no_state(db_engine, server):
    """`None` and not `Waiting`: the difference is whether the operator should
    keep waiting or go and look at the node."""
    server.pod = live_pod(ephemeral=[{"name": "debugger-x4k2p", "image": "busybox:1.36"}])

    (row,) = debug_service.list_debug_containers(NAMESPACE, POD)["items"]

    assert row["state"] is None
    assert row["reason"] is None


def test_an_empty_listing_is_a_real_zero_because_the_pod_was_read(db_engine, server):
    result = debug_service.list_debug_containers(NAMESPACE, POD)

    assert result["items"] == []
    assert result["partial"] is False


def test_a_pod_that_could_not_be_read_raises_rather_than_answering_with_an_empty_list(
    db_engine, server,
):
    """§0.1. An empty list here would say "this pod has no debug containers"
    about a pod we never saw."""
    server.raises["GET"] = ApiException(status=403, reason="Forbidden")

    with pytest.raises(RBACDenied):
        debug_service.list_debug_containers(NAMESPACE, POD)


# --------------------------------------------------------------------------- #
# Reaching the container afterwards
# --------------------------------------------------------------------------- #

def _typed_pod(ephemeral=()):
    """The pod as ``CoreV1Api`` hands it back, which is what `resolve_container`
    reads — a different shape from the write path's dicts, on purpose."""
    return obj(
        metadata=obj(name=POD, namespace=NAMESPACE, annotations={}),
        spec=obj(
            containers=[obj(name="app"), obj(name="envoy")],
            init_containers=[obj(name="migrate")],
            ephemeral_containers=[obj(name=name) for name in ephemeral],
        ),
    )


def test_exec_and_logs_accept_a_debug_container_by_name(db_engine, fake_k8s):
    """Until §7.4 this console would have refused to open a shell in a container
    it had just created."""
    fake_k8s.core_v1.returns("read_namespaced_pod", _typed_pod(["debugger-x4k2p"]))

    assert logs_api.resolve_container(NAMESPACE, POD, "debugger-x4k2p") == "debugger-x4k2p"


def test_an_unknown_container_name_lists_the_debug_containers_separately(db_engine, fake_k8s):
    """"Containers: app, envoy. Debug containers: debugger-x4k2p." tells an
    operator both that they mistyped and that somebody else is already in this
    pod; a flat list of four names tells them neither."""
    fake_k8s.core_v1.returns("read_namespaced_pod", _typed_pod(["debugger-x4k2p"]))

    with pytest.raises(Invalid) as caught:
        logs_api.resolve_container(NAMESPACE, POD, "debuger-x4k2p")

    assert "Debug containers: debugger-x4k2p" in (caught.value.hint or "")
    assert caught.value.context["ephemeralContainers"] == ["debugger-x4k2p"]


def test_attaching_a_debug_container_does_not_make_a_single_container_pod_ambiguous(
    db_engine, fake_k8s,
):
    """The API server defaults the container only when `spec.containers` holds
    one, and never counts ephemeral containers. Counting them here would have
    turned every subsequent container-less log request on a single-container pod
    into a 422 — a console that broke its own log viewer as a side effect of
    opening a shell."""
    fake_k8s.core_v1.returns("read_namespaced_pod", obj(
        metadata=obj(name=POD, namespace=NAMESPACE, annotations={}),
        spec=obj(
            containers=[obj(name="app")],
            init_containers=[],
            ephemeral_containers=[obj(name="debugger-x4k2p")],
        ),
    ))

    assert logs_api.resolve_container(NAMESPACE, POD, None) is None


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #
# The routes are thin — parse, call, envelope — so what is asserted here is only
# what the service-level tests above cannot reach: the wire spelling of the body,
# the default that applies when a field is missing, and the status code each
# error class renders as. The write path itself is tested where it lives.

def test_the_post_body_defaults_to_a_dry_run_when_the_field_is_absent(
    db_engine, client, registered_cluster, server,
):
    """A client that forgets `dryRun` gets a projection, not a write. That
    default is safety, not convenience."""
    response = client.post(f"/api/pods/{NAMESPACE}/{POD}/debug", json={"image": "busybox:1.36"})

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert server.of("PATCH")[0].query == {"dryRun": "All"}


def test_the_post_body_accepts_both_spellings_of_dry_run(
    db_engine, client, registered_cluster, server, allow_mutations,
):
    """`dry_run` is understood rather than silently ignored — a caller asking for
    a real write and receiving a dry run reported as `applied: false` is the one
    misunderstanding in this API that costs an operator an incident."""
    for spelling in ("dryRun", "dry_run"):
        response = client.post(
            f"/api/pods/{NAMESPACE}/{POD}/debug",
            json={"image": "busybox:1.36", spelling: False},
        )
        assert response.status_code == 200, spelling
        assert response.json()["applied"] is True, spelling


def test_an_empty_post_body_uses_the_configured_default_image(
    db_engine, client, registered_cluster, server,
):
    from app.config import settings

    response = client.post(f"/api/pods/{NAMESPACE}/{POD}/debug", json={})

    assert response.status_code == 200
    assert response.json()["image"] == settings.debug_image


def test_an_unsupported_cluster_renders_as_501_not_404(
    db_engine, client, registered_cluster, fake_k8s,
):
    """§1.3: `unsupported` is not an error in the UI, and `not_found` about a pod
    the operator is looking at is a wrong answer they would act on."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason=None, evaluation_error=None, denied=False,
    )))
    fake_k8s.api_client.returns("call_api", FakeApiServer(discovery=DISCOVERY_WITHOUT_EPHEMERAL))

    response = client.post(f"/api/pods/{NAMESPACE}/{POD}/debug", json={})

    assert response.status_code == 501
    assert response.json()["error"] == "unsupported"


def test_the_listing_route_returns_the_envelope_with_supported(
    db_engine, client, registered_cluster, server,
):
    server.pod = live_pod(ephemeral=[{"name": "debugger-x4k2p", "image": "busybox:1.36"}])

    body = client.get(f"/api/pods/{NAMESPACE}/{POD}/debug").json()

    assert [row["name"] for row in body["items"]] == ["debugger-x4k2p"]
    assert body["partial"] is False
    assert body["supported"] is True
