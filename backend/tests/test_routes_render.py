"""
Compiling an exposure into an object (§13) — the form half.

The property under test throughout is **nothing is dropped silently**. An
exposure that asks for something a backend cannot express compiles to the
object the backend *can* hold, plus a ``lossy[]`` entry naming what was left
out, what that does to traffic, and what to do instead — and the write is then
refused until the caller acknowledges each one by name.

The second property is **the operator's document survives the form**. A field
somebody hand-wrote is still there after a trip through the form view, and
``preserved[]`` names it so the UI can say the form is not showing everything.
"""

from __future__ import annotations

import pytest
import yaml

from app.admin import routes as admin_routes
from app.errors import Invalid
from app.services import routes as svc
from tests.test_routes import stub_discovery, _clean_catalog_cache  # noqa: F401


def exposure(**overrides):
    """A minimal valid exposure model, as the wire sends it."""
    payload = {
        "name": "checkout",
        "namespace": "prod",
        "host": "checkout.example.com",
        "path": "/",
        "pathType": "Prefix",
        "targets": [{"service": "checkout", "port": 8080}],
        "tls": {},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Validation — the things a schema cannot express
# --------------------------------------------------------------------------- #

def test_reencrypt_without_a_destination_ca_is_refused():
    """Some routers then do not verify the backend at all — the property, silently absent."""
    with pytest.raises(Invalid) as caught:
        admin_routes.build_exposure(exposure(tls={"termination": "reencrypt"}))

    assert "destinationCACertificate" in caught.value.context["parameter"]
    assert "nothing to verify" in caught.value.detail


@pytest.mark.parametrize("field", ["certificate", "key"])
def test_an_inline_certificate_or_key_is_refused_not_ignored(field):
    """A private key must not travel through this model — and must not vanish either.

    Refused rather than dropped: silently discarding a key an operator pasted
    produces an exposure that is created and then does not serve, with nothing
    on screen saying why. The refusal names the Secret reference and the YAML
    view, which are the two ways to get what they wanted.
    """
    with pytest.raises(Invalid) as caught:
        admin_routes.build_exposure(
            exposure(tls={"termination": "edge", field: "-----BEGIN…"})
        )

    assert caught.value.context["parameter"] == f"tls.{field}"
    assert "secretName" in caught.value.hint
    assert "YAML view" in caught.value.hint


def test_a_private_key_never_reaches_a_compiled_document():
    """The property, asserted at the boundary rather than inferred from the model."""
    with pytest.raises(Invalid):
        admin_routes.build_exposure(exposure(tls={
            "termination": "edge", "certificate": "c", "key": "SUPERSECRET",
        }))


def test_switching_a_route_to_a_secret_reference_removes_an_inline_pair(
    monkeypatch, fake_k8s,
):
    """Otherwise the key stays in the spec after the operator believes it moved.

    The Route API rejects `certificate` together with `externalCertificate`, so
    leaving it would also make the write fail — but the reason it is removed is
    the first one.
    """
    stub_discovery(monkeypatch)
    existing = yaml.safe_dump({
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {"name": "checkout", "namespace": "prod"},
        "spec": {
            "host": "checkout.example.com",
            "to": {"kind": "Service", "name": "checkout"},
            "tls": {"termination": "edge", "certificate": "c", "key": "SUPERSECRET"},
        },
    })

    result = admin_routes.render(
        "openshift",
        exposure(tls={"termination": "edge", "secretName": "checkout-tls"}),
        document=existing,
    )

    assert "SUPERSECRET" not in result["yaml"]
    assert result["document"]["spec"]["tls"]["externalCertificate"] == {
        "name": "checkout-tls",
    }


def test_passthrough_with_allow_is_refused():
    """There is no plaintext listener to serve the same content on."""
    with pytest.raises(Invalid) as caught:
        admin_routes.build_exposure(
            exposure(tls={"termination": "passthrough", "insecurePolicy": "Allow"})
        )

    assert "cannot also serve plain HTTP" in caught.value.message


def test_an_insecure_policy_with_no_tls_is_refused():
    with pytest.raises(Invalid) as caught:
        admin_routes.build_exposure(exposure(tls={"insecurePolicy": "Redirect"}))

    assert "only means something on a TLS exposure" in caught.value.message


def test_a_host_and_a_subdomain_together_are_refused():
    """The API ignores subdomain when host is set, so the object lies about itself."""
    with pytest.raises(Invalid):
        admin_routes.build_exposure(exposure(subdomain="checkout"))


def test_a_path_without_a_leading_slash_is_refused():
    with pytest.raises(Invalid):
        admin_routes.build_exposure(exposure(path="api"))


def test_an_exposure_with_no_targets_is_refused():
    with pytest.raises(Invalid):
        admin_routes.build_exposure(exposure(targets=[]))


def test_more_targets_than_a_route_can_hold_are_refused():
    """The limit is applied to every backend so an exposure stays portable."""
    with pytest.raises(Invalid) as caught:
        admin_routes.build_exposure(exposure(targets=[
            {"service": f"svc-{i}"} for i in range(admin_routes.MAX_TARGETS + 1)
        ]))

    assert "at most" in caught.value.message


def test_a_blank_hostname_is_the_same_as_no_hostname():
    """Otherwise spec.host: "" is stored, and reads as a hostname nobody can type."""
    model = admin_routes.build_exposure(exposure(host="   "))

    assert model.host is None
    assert svc.FEATURE_GENERATED_HOST in model.requested_features()


# --------------------------------------------------------------------------- #
# Requested features
# --------------------------------------------------------------------------- #

def test_one_target_with_a_weight_is_not_a_traffic_split():
    """Otherwise every ordinary Ingress is lossy for a feature nobody used."""
    model = admin_routes.build_exposure(
        exposure(targets=[{"service": "checkout", "port": 8080, "weight": 100}])
    )

    assert svc.FEATURE_WEIGHTED_BACKENDS not in model.requested_features()


def test_two_targets_is_a_traffic_split():
    model = admin_routes.build_exposure(exposure(targets=[
        {"service": "checkout", "weight": 90},
        {"service": "checkout-canary", "weight": 10},
    ]))

    assert svc.FEATURE_WEIGHTED_BACKENDS in model.requested_features()


# --------------------------------------------------------------------------- #
# Compilation — Route
# --------------------------------------------------------------------------- #

def test_a_route_carries_every_feature_losslessly(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)

    result = admin_routes.render("openshift", exposure(
        targets=[
            {"service": "checkout", "port": 8080, "weight": 90},
            {"service": "checkout-canary", "port": 8080, "weight": 10},
        ],
        tls={"termination": "reencrypt", "insecurePolicy": "Redirect",
             "destinationCACertificate": "ca"},
        wildcardPolicy="Subdomain",
    ))

    assert result["lossy"] == []
    spec = result["document"]["spec"]
    assert spec["to"] == {"kind": "Service", "name": "checkout", "weight": 90}
    assert spec["alternateBackends"] == [
        {"kind": "Service", "name": "checkout-canary", "weight": 10}
    ]
    assert spec["tls"]["termination"] == "reencrypt"
    assert spec["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
    assert spec["tls"]["destinationCACertificate"] == "ca"
    assert spec["wildcardPolicy"] == "Subdomain"


def test_switching_a_route_from_a_host_to_a_subdomain_clears_the_host(monkeypatch, fake_k8s):
    """spec.host wins over spec.subdomain, so a leftover host makes the change a no-op."""
    stub_discovery(monkeypatch)
    existing = yaml.safe_dump({
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {"name": "checkout", "namespace": "prod"},
        "spec": {"host": "old.example.com", "to": {"kind": "Service", "name": "checkout"}},
    })

    result = admin_routes.render(
        "openshift",
        exposure(host=None, subdomain="checkout"),
        document=existing,
    )

    assert "host" not in result["document"]["spec"]
    assert result["document"]["spec"]["subdomain"] == "checkout"


def test_a_secret_reference_becomes_an_external_certificate(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)

    result = admin_routes.render("openshift", exposure(
        tls={"termination": "edge", "secretName": "checkout-tls"},
    ))

    assert result["document"]["spec"]["tls"]["externalCertificate"] == {"name": "checkout-tls"}
    assert "certificate" not in result["document"]["spec"]["tls"]


def test_an_exact_path_is_lossy_on_a_route(monkeypatch, fake_k8s):
    """A Route matches its path as a prefix; there is no exact mode."""
    stub_discovery(monkeypatch)

    result = admin_routes.render("openshift", exposure(pathType="Exact"))

    assert [e["feature"] for e in result["lossy"]] == [svc.FEATURE_PATH_EXACT]
    assert "prefix" in result["lossy"][0]["consequence"]


# --------------------------------------------------------------------------- #
# Compilation — Ingress, the lossy one
# --------------------------------------------------------------------------- #

def test_passthrough_on_an_ingress_is_reported_not_silently_downgraded(
    monkeypatch, fake_k8s,
):
    """The whole point of the feature: an mTLS client breaks, and it is said out loud."""
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", exposure(
        tls={"termination": "passthrough"},
    ))

    features = [e["feature"] for e in result["lossy"]]
    assert svc.FEATURE_PASSTHROUGH_TLS in features
    entry = next(e for e in result["lossy"] if e["feature"] == svc.FEATURE_PASSTHROUGH_TLS)
    assert "client certificates" in entry["consequence"]
    assert entry["mitigation"]


def test_the_compiler_never_writes_a_controller_specific_annotation(monkeypatch, fake_k8s):
    """An annotation that works on the author's controller is not portability."""
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", exposure(
        tls={"termination": "edge", "insecurePolicy": "Redirect", "secretName": "tls"},
    ))

    rendered = result["yaml"]
    assert "nginx.ingress.kubernetes.io" not in rendered
    assert "haproxy.org" not in rendered
    assert "traefik" not in rendered
    assert svc.FEATURE_INSECURE_REDIRECT in [e["feature"] for e in result["lossy"]]


def test_weighted_backends_on_an_ingress_say_the_others_receive_nothing(
    monkeypatch, fake_k8s,
):
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", exposure(targets=[
        {"service": "checkout", "port": 8080, "weight": 90},
        {"service": "checkout-canary", "port": 8080, "weight": 10},
    ]))

    entry = next(e for e in result["lossy"] if e["feature"] == svc.FEATURE_WEIGHTED_BACKENDS)
    assert "Only the first target" in entry["consequence"]


def test_a_numeric_port_string_becomes_a_number_not_a_port_name(monkeypatch, fake_k8s):
    """`port: {name: "8080"}` matches no port at all."""
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", exposure(
        targets=[{"service": "checkout", "port": "8080"}],
    ))

    backend = result["document"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert backend["service"]["port"] == {"number": 8080}


def test_a_named_port_stays_a_name(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", exposure(
        targets=[{"service": "checkout", "port": "http"}],
    ))

    backend = result["document"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert backend["service"]["port"] == {"name": "http"}


# --------------------------------------------------------------------------- #
# Compilation — HTTPRoute
# --------------------------------------------------------------------------- #

def test_a_redirect_becomes_a_second_rule_not_a_filter_on_the_forwarding_one(
    monkeypatch, fake_k8s,
):
    """A rule cannot both redirect and forward: the route would never reach the Service."""
    stub_discovery(monkeypatch)

    result = admin_routes.render("gateway", exposure(
        tls={"termination": "edge", "insecurePolicy": "Redirect"},
        parentRefs=[{"name": "public"}],
    ))

    rules = result["document"]["spec"]["rules"]
    assert len(rules) == 2
    assert "backendRefs" in rules[0]
    assert "filters" not in rules[0]
    assert rules[1]["filters"][0]["type"] == "RequestRedirect"


def test_tls_on_an_httproute_is_lossy_because_it_belongs_to_the_gateway(
    monkeypatch, fake_k8s,
):
    stub_discovery(monkeypatch)

    result = admin_routes.render("gateway", exposure(
        tls={"termination": "edge", "secretName": "tls"},
        parentRefs=[{"name": "public"}],
    ))

    entry = next(e for e in result["lossy"] if e["feature"] == svc.FEATURE_EDGE_TLS)
    assert "listener" in entry["consequence"]


def test_httproute_weights_are_written_as_real_fields(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)

    result = admin_routes.render("gateway", exposure(
        targets=[
            {"service": "api", "port": 8080, "weight": 90},
            {"service": "api-next", "port": 8080, "weight": 10},
        ],
        parentRefs=[{"name": "public"}],
    ))

    assert result["lossy"] == []
    refs = result["document"]["spec"]["rules"][0]["backendRefs"]
    assert refs == [
        {"name": "api", "port": 8080, "weight": 90},
        {"name": "api-next", "port": 8080, "weight": 10},
    ]


def test_a_named_port_is_left_out_of_a_backend_ref_rather_than_rejected_by_the_api(
    monkeypatch, fake_k8s,
):
    """Gateway API's backendRef port is an integer only — there is no name form."""
    stub_discovery(monkeypatch)

    result = admin_routes.render("gateway", exposure(
        targets=[{"service": "api", "port": "http"}],
        parentRefs=[{"name": "public"}],
    ))

    assert "port" not in result["document"]["spec"]["rules"][0]["backendRefs"][0]


# --------------------------------------------------------------------------- #
# The operator's document survives the form
# --------------------------------------------------------------------------- #

def test_a_hand_written_second_rule_survives_the_form(monkeypatch, fake_k8s):
    """This is the property that makes YAML the source of truth rather than a view."""
    stub_discovery(monkeypatch)
    existing = yaml.safe_dump({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "checkout", "namespace": "prod",
                     "annotations": {"haproxy.org/ssl-redirect": "true"}},
        "spec": {
            "rules": [
                {"host": "old.example.com",
                 "http": {"paths": [
                     {"path": "/", "pathType": "Prefix",
                      "backend": {"service": {"name": "old", "port": {"number": 80}}}},
                     {"path": "/static", "pathType": "Prefix",
                      "backend": {"service": {"name": "cdn", "port": {"number": 80}}}},
                 ]}},
                {"host": "admin.example.com",
                 "http": {"paths": [
                     {"path": "/", "pathType": "Prefix",
                      "backend": {"service": {"name": "admin", "port": {"number": 80}}}},
                 ]}},
            ],
        },
    })

    result = admin_routes.render("ingress", exposure(), document=existing)

    rules = result["document"]["spec"]["rules"]
    # The form's exposure landed on the first rule's first path…
    assert rules[0]["host"] == "checkout.example.com"
    assert rules[0]["http"]["paths"][0]["backend"]["service"]["name"] == "checkout"
    # …and everything the operator wrote is still there.
    assert rules[0]["http"]["paths"][1]["backend"]["service"]["name"] == "cdn"
    assert rules[1]["host"] == "admin.example.com"
    assert result["document"]["metadata"]["annotations"]["haproxy.org/ssl-redirect"] == "true"


def test_the_form_says_which_parts_of_the_document_it_is_not_showing(
    monkeypatch, fake_k8s,
):
    """A form that could not say what it hides leaves the operator to find out at confirm time."""
    stub_discovery(monkeypatch)
    existing = yaml.safe_dump({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "checkout", "namespace": "prod",
                     "annotations": {"haproxy.org/ssl-redirect": "true"}},
        "spec": {
            "defaultBackend": {"service": {"name": "fallback", "port": {"number": 80}}},
            "rules": [
                {"host": "a", "http": {"paths": [
                    {"path": "/", "pathType": "Prefix",
                     "backend": {"service": {"name": "a", "port": {"number": 80}}}},
                ]}},
                {"host": "b", "http": {"paths": []}},
            ],
        },
    })

    result = admin_routes.render("ingress", exposure(), document=existing)

    assert "spec.defaultBackend" in result["preserved"]
    assert "spec.rules[1]" in result["preserved"]
    assert "metadata.annotations[haproxy.org/ssl-redirect]" in result["preserved"]


def test_last_applied_configuration_is_not_reported_as_a_hidden_field(
    monkeypatch, fake_k8s,
):
    """It is kubectl's bookkeeping, not something the operator wrote and needs told about."""
    stub_discovery(monkeypatch)
    existing = yaml.safe_dump({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": "checkout", "namespace": "prod",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
        },
        "spec": {"rules": []},
    })

    result = admin_routes.render("ingress", exposure(), document=existing)

    assert not any("last-applied" in path for path in result["preserved"])


def test_labels_are_merged_not_replaced(monkeypatch, fake_k8s):
    """A label a controller or a GitOps tool uses to find this object belongs to it."""
    stub_discovery(monkeypatch)
    existing = yaml.safe_dump({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "checkout", "namespace": "prod",
                     "labels": {"argocd.argoproj.io/instance": "shop"}},
        "spec": {"rules": []},
    })

    result = admin_routes.render(
        "ingress", exposure(labels={"team": "payments"}), document=existing,
    )

    labels = result["document"]["metadata"]["labels"]
    assert labels["argocd.argoproj.io/instance"] == "shop"
    assert labels["team"] == "payments"


# --------------------------------------------------------------------------- #
# render writes nothing
# --------------------------------------------------------------------------- #

def test_render_produces_no_audit_row(monkeypatch, fake_k8s, db_session):
    """It is a POST because it carries a body, not because anything happens."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    before = db_session.query(AuditRecord).count()

    admin_routes.render("ingress", exposure())

    assert db_session.query(AuditRecord).count() == before


def test_rendering_against_an_unreadable_backend_reraises_the_real_failure(
    monkeypatch, fake_k8s,
):
    """Rendering against a guessed apiVersion is rejected for the wrong reason later."""
    from kubernetes.client.rest import ApiException

    from app.errors import ClusterUnreachable

    stub_discovery(
        monkeypatch,
        failures={"/apis/route.openshift.io/v1": ApiException(status=503, reason="down")},
    )

    with pytest.raises(ClusterUnreachable):
        admin_routes.render("openshift", exposure())


# --------------------------------------------------------------------------- #
# Acknowledgement
# --------------------------------------------------------------------------- #

def test_a_lossy_write_is_refused_without_acknowledgement(monkeypatch, fake_k8s):
    """Consenting to a consequence is a separate act from requesting the change."""
    stub_discovery(monkeypatch)

    with pytest.raises(Invalid) as caught:
        admin_routes.create_route(
            "ingress", exposure(tls={"termination": "passthrough"}), dry_run=True,
        )

    assert caught.value.context["parameter"] == "acknowledgeLossy"
    assert svc.FEATURE_PASSTHROUGH_TLS in caught.value.context["unacknowledged"]


def test_acknowledging_a_different_feature_does_not_satisfy_the_refusal(
    monkeypatch, fake_k8s,
):
    """A UI that acknowledged once and then changed the form must acknowledge again."""
    stub_discovery(monkeypatch)

    with pytest.raises(Invalid):
        admin_routes.create_route(
            "ingress",
            exposure(tls={"termination": "passthrough"}),
            acknowledge_lossy=[svc.FEATURE_WEIGHTED_BACKENDS],
            dry_run=True,
        )


def test_a_lossless_write_needs_no_acknowledgement(monkeypatch, fake_k8s, allow_mutations):
    stub_discovery(monkeypatch)
    _stub_write(monkeypatch, fake_k8s)

    result = admin_routes.create_route("openshift", exposure(), dry_run=True)

    assert result["dryRun"] is True
    assert result["applied"] is False
    assert result["route"]["lossy"] == []


# --------------------------------------------------------------------------- #
# The write path
# --------------------------------------------------------------------------- #

def _stub_write(monkeypatch, fake_k8s):
    """Let a create reach the funnel and come back with a projected object."""
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review",
        lambda body, **kw: type(
            "R", (), {"status": type("S", (), {
                "allowed": True, "reason": "", "evaluation_error": None,
            })()},
        )(),
    )
    monkeypatch.setattr(
        admin_routes.apply_service, "request_json",
        lambda method, path, **kw: (
            {
                "apiVersion": "route.openshift.io/v1",
                "kind": "Route",
                "metadata": {"name": "checkout", "namespace": "prod",
                             "resourceVersion": "99"},
                "spec": {"host": "checkout.example.com"},
            },
            [],
        ),
    )


def test_the_audit_sentence_names_the_hostname_and_the_service(
    monkeypatch, fake_k8s, allow_mutations, db_session,
):
    """"create Route checkout" does not answer "who exposed payments to the internet"."""
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    _stub_write(monkeypatch, fake_k8s)

    admin_routes.create_route("openshift", exposure(
        tls={"termination": "edge", "secretName": "tls"},
    ), dry_run=False)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert "checkout:8080" in record.detail
    assert "https://checkout.example.com/" in record.detail
    assert "edge" in record.detail


def test_a_dry_run_never_reports_applied(monkeypatch, fake_k8s, allow_mutations):
    stub_discovery(monkeypatch)
    _stub_write(monkeypatch, fake_k8s)

    result = admin_routes.create_route("openshift", exposure(), dry_run=True)

    assert result["applied"] is False


def test_a_replace_cannot_rename_the_exposure(monkeypatch, fake_k8s, allow_mutations):
    """Renaming would also have to move its hostname claim, which a PUT does not do."""
    stub_discovery(monkeypatch)

    with pytest.raises(Invalid) as caught:
        admin_routes.update_route(
            "openshift", "prod", "other-name", exposure(),
            resource_version="1", dry_run=True,
        )

    assert "cannot rename" in caught.value.hint


def test_the_write_is_refused_when_the_console_is_read_only(monkeypatch, fake_k8s):
    """The gate is the deployment's, and it fires before the cluster is touched."""
    from app.errors import MutationsDisabled

    stub_discovery(monkeypatch)
    _stub_write(monkeypatch, fake_k8s)

    with pytest.raises(MutationsDisabled):
        admin_routes.create_route("openshift", exposure(), dry_run=False)


# --------------------------------------------------------------------------- #
# The YAML view is authoritative
# --------------------------------------------------------------------------- #

HAND_WRITTEN = yaml.safe_dump({
    "apiVersion": "networking.k8s.io/v1",
    "kind": "Ingress",
    "metadata": {
        "name": "checkout", "namespace": "prod",
        "annotations": {"haproxy.org/ssl-redirect": "true"},
    },
    "spec": {
        "ingressClassName": "haproxy",
        "rules": [
            {"host": "hand-written.example.com",
             "http": {"paths": [
                 {"path": "/hand", "pathType": "Exact",
                  "backend": {"service": {"name": "hand", "port": {"number": 9000}}}},
             ]}},
        ],
    },
})


def test_a_document_with_no_spec_is_taken_verbatim(monkeypatch, fake_k8s):
    """The whole point of the YAML view.

    Without this mode the form's fields are recompiled over a hand-edited
    document at write time, silently undoing the edit the UI locked the form to
    protect.
    """
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", None, document=HAND_WRITTEN)

    assert result["verbatim"] is True
    rules = result["document"]["spec"]["rules"]
    assert rules[0]["host"] == "hand-written.example.com"
    assert rules[0]["http"]["paths"][0]["path"] == "/hand"
    assert rules[0]["http"]["paths"][0]["backend"]["service"]["name"] == "hand"


def test_a_verbatim_document_reports_nothing_lossy_and_nothing_hidden(
    monkeypatch, fake_k8s,
):
    """The console compiled nothing, so it dropped nothing and hides nothing.

    Both are statements of fact rather than defaults nobody filled in: an
    operator who wrote the object themselves has not had anything taken out of
    it by this console.
    """
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", None, document=HAND_WRITTEN)

    assert result["lossy"] == []
    assert result["preserved"] == []


def test_the_form_would_have_overwritten_the_hand_written_rule(monkeypatch, fake_k8s):
    """The bug the verbatim mode exists to prevent, asserted directly.

    Same document, once with a spec and once without. With the spec, the form's
    host and path land on the first rule; without it, the operator's do.
    """
    stub_discovery(monkeypatch)

    compiled = admin_routes.render("ingress", exposure(), document=HAND_WRITTEN)
    verbatim = admin_routes.render("ingress", None, document=HAND_WRITTEN)

    assert compiled["document"]["spec"]["rules"][0]["host"] == "checkout.example.com"
    assert verbatim["document"]["spec"]["rules"][0]["host"] == "hand-written.example.com"


def test_neither_a_spec_nor_a_document_is_refused(monkeypatch, fake_k8s):
    stub_discovery(monkeypatch)

    with pytest.raises(Invalid) as caught:
        admin_routes.render("ingress", None, document=None)

    assert "nothing to write" in caught.value.message


def test_a_verbatim_write_says_so_in_the_audit_trail(
    monkeypatch, fake_k8s, allow_mutations, db_session,
):
    """The console did not model the intent, so it must not narrate one.

    The compiled sentence describes an exposure the console understood. For a
    hand-written document the truthful sentence is which object was written and
    that the operator authored it.
    """
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    monkeypatch.setattr(
        admin_routes.apply_service, "request_json",
        lambda method, path, **kw: (
            {**(kw.get("body") or {}),
             "metadata": {**((kw.get("body") or {}).get("metadata") or {}),
                          "resourceVersion": "77"}},
            [],
        ),
    )
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review",
        lambda body, **kw: type(
            "R", (), {"status": type("S", (), {
                "allowed": True, "reason": "", "evaluation_error": None,
            })()},
        )(),
    )

    admin_routes.create_route("ingress", None, document=HAND_WRITTEN, dry_run=False)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert "written verbatim from the YAML view" in record.detail
    assert "prod/checkout" in record.detail


# --------------------------------------------------------------------------- #
# The no-vendor-annotation rule, as a mechanism rather than a policy
# --------------------------------------------------------------------------- #

#: Annotation prefixes that belong to one ingress controller rather than to the
#: Kubernetes API. The compiler must never emit one of its own.
VENDOR_PREFIXES = (
    "nginx.ingress.kubernetes.io/",
    "haproxy.org/",
    "haproxy-ingress.github.io/",
    "traefik.ingress.kubernetes.io/",
    "traefik.io/",
    "konghq.com/",
    "alb.ingress.kubernetes.io/",
    "cert-manager.io/",
    "route.openshift.io/",
)


@pytest.mark.parametrize("backend", ["openshift", "ingress", "gateway"])
def test_the_compiler_never_emits_a_vendor_annotation_of_its_own(
    backend, monkeypatch, fake_k8s,
):
    """The refusal to guess a controller is a mechanism, not a comment.

    "An Ingress cannot express passthrough, so we report it as lossy" is the
    whole design of §13. The pressure to instead emit
    `haproxy.org/ssl-redirect: "true"` — which works, on the one controller the
    author happened to be testing against — is exactly where that design gets
    quietly reversed, and a reversal would leave `lossy[]` claiming a feature
    was dropped while the object silently carried it.

    Every feature is requested at once so no compiler branch is skipped.
    """
    stub_discovery(monkeypatch)

    result = admin_routes.render(backend, exposure(
        targets=[
            {"service": "checkout", "port": 8080, "weight": 90},
            {"service": "checkout-canary", "port": 8080, "weight": 10},
        ],
        pathType="Exact",
        tls={
            "termination": "reencrypt",
            "insecurePolicy": "Redirect",
            "destinationCACertificate": "ca",
        },
        wildcardPolicy="Subdomain",
        parentRefs=[{"name": "public"}],
    ))

    emitted = (result["document"].get("metadata") or {}).get("annotations") or {}
    offenders = [
        key for key in emitted
        if any(key.startswith(prefix) for prefix in VENDOR_PREFIXES)
    ]
    assert not offenders, (
        f"{backend} compiler emitted controller-specific annotations {offenders}. "
        "§13 reports what a backend cannot express in `lossy[]`; it does not "
        "guess which controller is running and write an annotation for it."
    )


def test_an_annotation_the_operator_supplied_is_still_written(monkeypatch, fake_k8s):
    """The rule is about what the *compiler* invents, not about what it carries.

    An operator who writes `haproxy.org/ssl-redirect` themselves has chosen it
    for the controller they know they are running, and dropping it would be the
    field-eating this whole module is built to avoid.
    """
    stub_discovery(monkeypatch)

    result = admin_routes.render("ingress", exposure(
        annotations={"haproxy.org/ssl-redirect": "true"},
    ))

    assert (
        result["document"]["metadata"]["annotations"]["haproxy.org/ssl-redirect"] == "true"
    )


# --------------------------------------------------------------------------- #
# routes/custom-host — the denial invariant 2 would otherwise misname
# --------------------------------------------------------------------------- #

def _deny_custom_host(fake_k8s):
    """Allow every review except the one on the custom-host subresource."""

    def review(body, **kwargs):
        attributes = body.spec.resource_attributes
        allowed = getattr(attributes, "subresource", None) != "custom-host"
        return type("R", (), {"status": type("S", (), {
            "allowed": allowed,
            "reason": "" if allowed else "no RBAC policy matched",
            "evaluation_error": None,
        })()})()

    fake_k8s.authorization_v1.returns("create_self_subject_access_review", review)


def test_a_route_with_a_hostname_preflights_the_custom_host_subresource(
    monkeypatch, fake_k8s, allow_mutations,
):
    """OpenShift gates choosing a hostname separately from creating the Route.

    Without this the funnel preflights `create routes`, that review passes, and
    the API server refuses the write — so the operator is told they cannot
    create Routes, a permission the review just confirmed they hold.
    """
    from app.errors import RBACDenied

    stub_discovery(monkeypatch)
    _deny_custom_host(fake_k8s)

    with pytest.raises(RBACDenied) as caught:
        admin_routes.create_route("openshift", exposure(), dry_run=True)

    assert caught.value.context.get("subresource") == "custom-host"


def test_that_denial_is_audited_even_though_it_is_outside_the_funnel(
    monkeypatch, fake_k8s, allow_mutations, db_session,
):
    from app.errors import RBACDenied
    from app.models import AuditRecord

    stub_discovery(monkeypatch)
    _deny_custom_host(fake_k8s)

    with pytest.raises(RBACDenied):
        admin_routes.create_route("openshift", exposure(), dry_run=True)

    record = db_session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
    assert record.outcome == "denied"
    assert "checkout.example.com" in record.detail


def test_a_route_with_no_hostname_does_not_need_the_custom_host_grant(
    monkeypatch, fake_k8s, allow_mutations,
):
    """Preflighting it anyway would disable a control for a grant not required.

    A Route letting the router generate its hostname is not a custom host.
    Asking for the permission regardless is the same defect pointed the other
    way: a button greyed out with a reason that is not true.
    """
    stub_discovery(monkeypatch)
    _deny_custom_host(fake_k8s)
    monkeypatch.setattr(
        admin_routes.apply_service, "request_json",
        lambda method, path, **kw: (
            {"apiVersion": "route.openshift.io/v1", "kind": "Route",
             "metadata": {"name": "checkout", "namespace": "prod",
                          "resourceVersion": "3"}},
            [],
        ),
    )

    result = admin_routes.create_route(
        "openshift", exposure(host=None, subdomain="checkout"), dry_run=True,
    )

    assert result["dryRun"] is True


def test_an_ingress_never_preflights_custom_host(monkeypatch, fake_k8s, allow_mutations):
    """It is an OpenShift subresource; no other backend has one."""
    stub_discovery(monkeypatch)
    _deny_custom_host(fake_k8s)
    monkeypatch.setattr(
        admin_routes.apply_service, "request_json",
        lambda method, path, **kw: (
            {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
             "metadata": {"name": "checkout", "namespace": "prod",
                          "resourceVersion": "3"}},
            [],
        ),
    )

    result = admin_routes.create_route("ingress", exposure(), dry_run=True)

    assert result["dryRun"] is True


# --------------------------------------------------------------------------- #
# The real transport, not a monkeypatched stand-in
# --------------------------------------------------------------------------- #

def _dispatch_call_api(fake_k8s, *, warning=None, record=None):
    """Stub the transport itself, so `request_json` actually runs.

    Every other write test in this file patches ``apply_service.request_json``
    out, which is fine for asserting funnel behaviour and useless for asserting
    that the funnel talks to the cluster correctly — it skips URL construction,
    the ``dryRun`` query parameter, the ``_return_http_data_only=False``
    three-tuple, and ``parse_warnings`` entirely. This one stubs
    ``api_client.call_api``, the sharp-edged fake's own method, so all of that is
    exercised and an unstubbed call still raises.
    """

    def call_api(path, method, **kwargs):
        if record is not None:
            record.append((method, path, dict(kwargs.get("query_params") or [])))
        body = kwargs.get("body") or {}
        projected = {
            **body,
            "metadata": {**(body.get("metadata") or {}), "resourceVersion": "9001"},
        }
        headers = {"Warning": f'299 - "{warning}"'} if warning else {}
        # The three-tuple `_return_http_data_only=False` asks for. Returning the
        # body alone here would let a regression that stopped requesting headers
        # pass unnoticed — which is exactly how the Warning relay would go quiet.
        return projected, 200, headers

    fake_k8s.api_client.returns("call_api", call_api)


def test_a_create_goes_through_the_real_transport_with_dry_run_on_the_query(
    monkeypatch, fake_k8s, allow_mutations,
):
    """The projection and the real write differ by one query parameter, asserted.

    Two code paths — one that previews and one that writes — is how a console
    ends up showing a diff of something it is not about to do.
    """
    stub_discovery(monkeypatch)
    _allow_preflight(fake_k8s)
    calls: list = []
    _dispatch_call_api(fake_k8s, record=calls)

    admin_routes.create_route("ingress", exposure(), dry_run=True)
    admin_routes.create_route("ingress", exposure(), dry_run=False)

    assert [c[0] for c in calls] == ["POST", "POST"]
    assert calls[0][1] == calls[1][1], "preview and write must address the same URL"
    assert calls[0][2].get("dryRun") == "All"
    assert "dryRun" not in calls[1][2]


def test_api_server_warnings_reach_the_mutation_response(
    monkeypatch, fake_k8s, allow_mutations,
):
    """§1.5 relays `Warning:` headers verbatim.

    Only reachable through the real `request_json`, which is why every other
    write test in this file cannot catch it going quiet.
    """
    stub_discovery(monkeypatch)
    _allow_preflight(fake_k8s)
    _dispatch_call_api(
        fake_k8s,
        warning="annotation nginx.ingress.kubernetes.io/rewrite-target is deprecated",
    )

    result = admin_routes.create_route("ingress", exposure(), dry_run=True)

    assert result["warnings"] == [
        "annotation nginx.ingress.kubernetes.io/rewrite-target is deprecated",
    ]


def test_the_projected_resource_version_is_what_the_confirming_call_carries(
    monkeypatch, fake_k8s, allow_mutations,
):
    """The dry run echoes the current version, and §1.5 hands it back for the PUT."""
    stub_discovery(monkeypatch)
    _allow_preflight(fake_k8s)
    _dispatch_call_api(fake_k8s)

    result = admin_routes.create_route("ingress", exposure(), dry_run=True)

    assert result["resourceVersion"] == "9001"


def _allow_preflight(fake_k8s):
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review",
        lambda body, **kw: type(
            "R", (), {"status": type("S", (), {
                "allowed": True, "reason": "", "evaluation_error": None,
            })()},
        )(),
    )
