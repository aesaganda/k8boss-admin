"""
The error vocabulary and the handlers that render it (§1.3).

The behaviour under test is not "an error produces a non-200". It is that the
*right* code comes back with the *right* status, because the frontend branches on
that code: it disables a button on ``rbac_denied``, offers reload-and-retry on
``conflict``, and renders "not present on this cluster" — not an error — on
``unsupported``. A handler that collapsed those into a generic failure would keep
every test that only checked the status code.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException
from pydantic import BaseModel

from app.api.exception_handlers import register_exception_handlers
from app.errors import (
    AdminError,
    ClusterUnreachable,
    Conflict,
    Invalid,
    MutationsDisabled,
    NoClusterSelected,
    NotFound,
    RBACDenied,
    Unsupported,
    UpstreamError,
    from_api_exception,
)


class _Body(BaseModel):
    replicas: int


@pytest.fixture
def error_client():
    """A minimal app with the real handlers installed and routes that raise.

    Deliberately not the production app: these tests are about the handlers, and
    routing them through a real endpoint would couple them to whatever that
    endpoint happens to do this month.
    """
    app = FastAPI()
    register_exception_handlers(app)
    slot: dict = {}

    @app.get("/raise")
    def _raise():
        raise slot["exc"]

    @app.post("/validate")
    def _validate(body: _Body):  # pragma: no cover - never reached with bad input
        return {"replicas": body.replicas}

    client = TestClient(app)
    client.slot = slot
    return client


def _raise_and_read(error_client, exc):
    error_client.slot["exc"] = exc
    return error_client.get("/raise")


# --------------------------------------------------------------------------- #
# Envelope shape
# --------------------------------------------------------------------------- #

def test_admin_error_renders_the_full_envelope(error_client):
    response = _raise_and_read(
        error_client,
        RBACDenied(
            'Cannot patch deployments in namespace "prod".',
            detail="deployments.apps is forbidden",
            hint="Grant `patch` on `apps/deployments`.",
            context={"verb": "patch", "group": "apps", "resource": "deployments",
                     "namespace": "prod", "name": "checkout"},
        ),
    )

    assert response.status_code == 403
    body = response.json()
    # Every key present, always. The SPA reads body.hint unconditionally.
    assert set(body) == {"error", "message", "detail", "hint", "context"}
    assert body["error"] == "rbac_denied"
    assert body["detail"] == "deployments.apps is forbidden"
    assert body["context"]["resource"] == "deployments"


def test_cluster_unreachable_renders_502_with_its_own_code(error_client):
    """The canonical case: an unreachable cluster must never be an opaque 500.

    Before k8boss had this, urllib3's MaxRetryError escaped to
    ServerErrorMiddleware, which renders *outside* CORSMiddleware — so the
    browser reported "Failed to fetch" and the operator never learned that their
    API server URL was wrong.
    """
    response = _raise_and_read(
        error_client,
        ClusterUnreachable(detail="NameResolutionError: api.prod-eu.example"),
    )

    assert response.status_code == 502
    assert response.json()["error"] == "cluster_unreachable"
    assert "NameResolutionError" in response.json()["detail"]


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (NoClusterSelected(), 409, "no_cluster_selected"),
        (ClusterUnreachable(), 502, "cluster_unreachable"),
        (RBACDenied(), 403, "rbac_denied"),
        (NotFound(), 404, "not_found"),
        (Conflict(), 409, "conflict"),
        (Invalid(), 422, "invalid"),
        (MutationsDisabled(), 403, "mutations_disabled"),
        (Unsupported(), 501, "unsupported"),
        (UpstreamError(), 502, "upstream_error"),
    ],
)
def test_every_contract_code_maps_to_its_documented_status(error_client, exc, status, code):
    response = _raise_and_read(error_client, exc)
    assert (response.status_code, response.json()["error"]) == (status, code)


def test_context_is_per_instance_not_shared():
    """Two errors of the same class must not share a context dict.

    A class-level default would leak one request's target into another's
    response, and the UI disables buttons based on that target.
    """
    first = RBACDenied(context={"resource": "pods"})
    second = RBACDenied()
    assert second.context == {}
    assert first.context == {"resource": "pods"}


# --------------------------------------------------------------------------- #
# Kubernetes ApiException mapping
# --------------------------------------------------------------------------- #

def _api_exception(status, message="upstream said so"):
    """An ApiException carrying a real Kubernetes ``Status`` body."""
    exc = ApiException(status=status, reason="Test")
    exc.body = json.dumps({"kind": "Status", "status": "Failure", "message": message})
    return exc


@pytest.mark.parametrize(
    "status,code,http_status",
    [
        (400, "invalid", 422),
        (401, "upstream_error", 502),
        (403, "rbac_denied", 403),
        (404, "not_found", 404),
        (405, "unsupported", 501),
        (409, "conflict", 409),
        (410, "invalid", 422),
        (422, "invalid", 422),
        (429, "upstream_error", 502),
        (500, "upstream_error", 502),
        (501, "unsupported", 501),
        (503, "upstream_error", 502),
    ],
)
def test_api_exception_status_mapping(status, code, http_status):
    error = from_api_exception(
        _api_exception(status),
        context={"verb": "list", "group": "apps", "resource": "deployments"},
    )
    assert isinstance(error, AdminError)
    assert (error.code, error.http_status) == (code, http_status)


def test_tls_failure_is_unreachable_not_upstream():
    """The kubernetes client signals "no HTTP exchange happened" with status=0.

    Nothing about the cluster's contents can be inferred from it, and the
    operator's next step (check the CA) differs from the one an upstream_error
    would send them on (check the API server's logs).
    """
    exc = ApiException(status=0, reason="SSLError certificate verify failed")
    error = from_api_exception(exc, context={"verb": "get", "resource": "version"})
    assert error.code == "cluster_unreachable"


def test_api_exception_detail_is_the_status_message_not_the_dump():
    """str(ApiException) is a multi-line dump including headers. Users get the sentence."""
    error = from_api_exception(
        _api_exception(403, 'pods is forbidden: User "sa" cannot list resource "pods"'),
        context={"verb": "list", "group": "core", "resource": "pods"},
    )
    assert error.detail == 'pods is forbidden: User "sa" cannot list resource "pods"'
    assert "HTTP response headers" not in (error.detail or "")


def test_non_json_body_is_reported_verbatim_and_truncated():
    """A proxy in front of the API server answers HTML. "That was not Kubernetes"
    is itself the diagnosis, so the body is kept rather than discarded."""
    exc = ApiException(status=502, reason="Bad Gateway")
    exc.body = "<html><body>" + ("x" * 2000) + "</body></html>"
    error = from_api_exception(exc, context={"verb": "list", "resource": "pods"})
    assert error.detail.startswith("<html>")
    assert len(error.detail) <= 520


def test_rbac_denial_names_the_grant_that_would_fix_it():
    error = from_api_exception(
        _api_exception(403),
        context={"verb": "create", "group": "core", "resource": "pods",
                 "subresource": "exec", "namespace": "prod"},
    )
    assert error.code == "rbac_denied"
    assert "`create`" in error.hint
    assert "core/pods/exec" in error.hint
    assert "`prod`" in error.hint


def test_unmapped_api_exception_is_rendered_by_the_handler(error_client):
    """A route that forgets to map ApiException still produces a usable answer."""
    response = _raise_and_read(error_client, _api_exception(403))
    assert response.status_code == 403
    assert response.json()["error"] == "rbac_denied"
    # We only know the path at that level, and saying so is better than inventing
    # a verb/resource the UI would then disable a button on.
    assert response.json()["context"] == {"path": "/raise"}


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def test_request_validation_is_rendered_as_invalid(error_client):
    response = error_client.post("/validate", json={"replicas": "many"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    # FastAPI's own {"detail": [...]} shape would be a second error format for
    # the frontend to parse; the per-field detail survives in context.
    assert "detail" in body and body["context"]["errors"]
    assert body["context"]["errors"][0]["location"].endswith("replicas")


# --------------------------------------------------------------------------- #
# The unavailable vocabulary
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "exc,reason",
    [
        (RBACDenied(), "forbidden"),
        (NotFound(), "not_found"),
        (ClusterUnreachable(), "unreachable"),
        (NoClusterSelected(), "not_registered"),
        (Unsupported(), "unsupported"),
    ],
)
def test_errors_translate_into_the_partial_read_vocabulary(exc, reason):
    """§1.2 reasons are a closed set, and this is the single join between them
    and the error classes — so a new class cannot quietly become the catch-all."""
    assert exc.unavailable_reason == reason
