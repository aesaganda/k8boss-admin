"""
Certificate signing requests (§25).

What is tested here is not that a condition can be appended — §4 covers writing
objects — but the four claims this feature makes that `kubectl certificate
approve` does not:

* **What the request asks to *become*, not who asked.** `spec.username` and the
  common name inside `spec.request` are different fields, and only the first is
  on any other screen. A request from an ordinary user whose organization is
  `system:masters` is a cluster-admin credential that RBAC cannot bound and no
  binding can revoke — and it looks exactly like a kubelet renewal until
  somebody decodes the PKCS#10.

* **A request nobody could decode says so.** Every derived field `None` and an
  error, never an empty subject — which renders as a certificate asking for
  nothing and is the most reassuring possible description of one that was never
  read.

* **Approved is not Issued.** Approving records a condition; a signer has to act.
  For a signerName nothing on the cluster signs, the request sits Approved with
  no certificate indefinitely, and the two states are kept apart for exactly
  that case.

* **Two permissions, and the second is the one people miss.** `update
  certificatesigningrequests/approval` is not enough: the API server's own
  admission plugin separately requires `approve` on `certificates.k8s.io/signers`
  named for the signerName. A console that preflighted only the first would
  enable a button the API server refuses.
"""

from __future__ import annotations

import base64
import copy
import ipaddress

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from app.admin import csr as csr_admin
from app.audit import recorder
from app.errors import Conflict, Invalid, MutationsDisabled, RBACDenied
from app.resources.shaping import (
    certificatesigningrequest_row,
    decode_certificate_request,
)
from tests.conftest import obj

NAME = "csr-node-ip-10-0-1-4"
KUBELET_SIGNER = "kubernetes.io/kubelet-serving"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def request_pem(
    *,
    common_name="system:node:ip-10-0-1-4",
    organizations=("system:nodes",),
    dns_names=(),
    ip_addresses=(),
    key=None,
    extra_common_names=(),
):
    """A real PKCS#10, because the thing under test is a real decode.

    EC rather than RSA: a P-256 key generates in about a millisecond and an
    RSA-2048 key in about a hundred, and this module builds one per case.
    """
    signing_key = key or ec.generate_private_key(ec.SECP256R1())
    attributes = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    attributes += [
        x509.NameAttribute(NameOID.COMMON_NAME, name) for name in extra_common_names
    ]
    attributes += [
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org) for org in organizations
    ]
    builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attributes))
    if dns_names or ip_addresses:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(name) for name in dns_names]
                + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ip_addresses]
            ),
            critical=False,
        )
    signed = builder.sign(signing_key, hashes.SHA256())
    return base64.b64encode(signed.public_bytes(serialization.Encoding.PEM)).decode()


def condition(kind, *, reason=None, when="2026-09-05T20:00:00Z"):
    return {"type": kind, "status": "True", "reason": reason, "message": None,
            "lastUpdateTime": when}


def csr_object(
    *,
    name=NAME,
    request=None,
    signer=KUBELET_SIGNER,
    username="system:node:ip-10-0-1-4",
    groups=("system:nodes", "system:authenticated"),
    usages=("digital signature", "key encipherment", "server auth"),
    conditions=(),
    certificate=None,
    resource_version="7719",
):
    body = {
        "apiVersion": "certificates.k8s.io/v1",
        "kind": "CertificateSigningRequest",
        "metadata": {
            "name": name,
            "resourceVersion": resource_version,
            "creationTimestamp": "2026-09-05T19:59:00Z",
            # Kept so the write path can be asserted to preserve it: an update
            # that dropped it hands the API server a different object than the
            # one it stored.
            "managedFields": [{"manager": "kubelet", "operation": "Update"}],
        },
        "spec": {
            "request": request if request is not None else request_pem(),
            "signerName": signer,
            "username": username,
            "groups": list(groups),
            "usages": list(usages),
        },
        "status": {"conditions": [dict(c) for c in conditions]},
    }
    if certificate is not None:
        body["status"]["certificate"] = certificate
    return body


class FakeCluster:
    """Routes the raw ``call_api`` calls §25 makes: the read and the approval PUT.

    ``puts`` staying empty is how "the refusal came before the cluster was
    touched" is proved, which is the assertion that matters for every refusal
    here — a decision that cannot be taken back must not be taken on a
    consequence nobody accepted.
    """

    def __init__(self, live=None, *, put_error=None):
        self.live = live if live is not None else csr_object()
        self.put_error = put_error
        self.puts: list[dict] = []

    def __call__(self, path, method, *args, **kwargs):
        if method == "GET" and "/certificatesigningrequests/" in path:
            return copy.deepcopy(self.live)
        if method == "PUT" and path.endswith("/approval"):
            if self.put_error is not None:
                raise self.put_error
            body = kwargs.get("body") or {}
            self.puts.append({
                "path": path,
                "body": body,
                "query": dict(kwargs.get("query_params") or []),
            })
            return copy.deepcopy(body), 200, {}
        raise AssertionError(f"Unexpected cluster call: {method} {path}")


def allow_all(_body=None, **_kwargs):
    return obj(status=obj(allowed=True, denied=False, reason=None, evaluation_error=None))


def deny_signers(body=None, **_kwargs):
    """Allow everything except `approve` on signers — the permission people miss."""
    attributes = body.spec.resource_attributes
    if attributes.resource == csr_admin.SIGNERS_RESOURCE:
        return obj(status=obj(allowed=False, denied=True, reason="no", evaluation_error=None))
    return allow_all()


@pytest.fixture
def cluster(fake_k8s, db_engine, allow_mutations):
    """A fake cluster wired for the write path, preflight allowing everything."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", allow_all)

    def install(fake=None):
        fake = fake if fake is not None else FakeCluster()
        fake_k8s.api_client.returns("call_api", fake)
        return fake

    return install


def codes(entries):
    return [entry["code"] for entry in entries]


def audit_rows():
    return recorder.query(limit=50)["items"]


# --------------------------------------------------------------------------- #
# The decode — the field kubectl does not show
# --------------------------------------------------------------------------- #

def test_the_subject_and_its_organizations_are_decoded():
    """The organizations are what become the identity's groups, and they are the
    reason this feature exists at all."""
    decoded = decode_certificate_request(
        request_pem(common_name="dev@example.com", organizations=("system:masters",))
    )

    assert decoded["subject"]["common_name"] == "dev@example.com"
    assert decoded["subject"]["organizations"] == ["system:masters"]
    assert decoded["error"] is None


def test_subject_alternative_names_are_decoded():
    decoded = decode_certificate_request(
        request_pem(dns_names=("ip-10-0-1-4",), ip_addresses=("10.0.1.4",))
    )

    assert decoded["dns_names"] == ["ip-10-0-1-4"]
    assert decoded["ip_addresses"] == ["10.0.1.4"]


def test_a_request_with_no_san_extension_reports_empty_lists_not_null():
    """A real "none requested": the request parsed and asks for no SANs. `None`
    is reserved for the case where nobody could read it."""
    decoded = decode_certificate_request(request_pem())

    assert decoded["dns_names"] == []
    assert decoded["ip_addresses"] == []
    assert decoded["error"] is None


def test_the_first_common_name_is_the_identity_and_the_rest_are_still_reported():
    """Several CNs is legal X.509 and Kubernetes reads only the first. Dropping
    the others would hide what a certificate also carries."""
    decoded = decode_certificate_request(
        request_pem(common_name="alice", extra_common_names=("bob",))
    )

    assert decoded["subject"]["common_name"] == "alice"
    assert decoded["subject"]["common_names"] == ["alice", "bob"]


@pytest.mark.parametrize(
    "raw",
    ["bm90IGEgcGVt", "", None, "!!!not base64!!!"],
    ids=["not-pem", "empty", "absent", "not-base64"],
)
def test_an_unreadable_request_is_null_everywhere_and_says_why(raw):
    """The §0.1 corollary on the most consequential decode in the codebase.

    An empty subject renders as a certificate that asks for nothing, which is
    the most reassuring possible description of one nobody could read.
    """
    decoded = decode_certificate_request(raw)

    assert decoded["subject"] is None
    assert decoded["dns_names"] is None
    assert decoded["key"] is None
    assert decoded["signature_valid"] is None
    assert decoded["error"]


def test_a_well_formed_request_verifies_against_its_own_key():
    assert decode_certificate_request(request_pem())["signature_valid"] is True


@pytest.mark.parametrize(
    ("key", "algorithm", "size"),
    [
        (ec.generate_private_key(ec.SECP256R1()), "ECDSA", 256),
        (rsa.generate_private_key(public_exponent=65537, key_size=2048), "RSA", 2048),
    ],
    ids=["ecdsa", "rsa"],
)
def test_the_key_is_named_by_type_not_by_the_libraries_class_name(key, algorithm, size):
    decoded = decode_certificate_request(request_pem(key=key))

    assert decoded["key"]["algorithm"] == algorithm
    assert decoded["key"]["size"] == size


# --------------------------------------------------------------------------- #
# The row — five states, and the one that matters
# --------------------------------------------------------------------------- #

def test_a_request_with_no_conditions_is_pending():
    assert certificatesigningrequest_row(csr_object())["state"] == "Pending"


def test_approved_without_a_certificate_is_approved_and_not_issued():
    """The state this section exists for. A signer has to act after an approval,
    and where none runs for the signerName the request stops here — looking
    exactly like a success."""
    row = certificatesigningrequest_row(
        csr_object(conditions=[condition("Approved")])
    )

    assert row["state"] == "Approved"
    assert row["issued"] is False


def test_approved_with_a_certificate_is_issued():
    row = certificatesigningrequest_row(
        csr_object(conditions=[condition("Approved")], certificate="LS0tLQo=")
    )

    assert row["state"] == "Issued"
    assert row["issued"] is True


def test_a_signer_that_refused_after_approval_is_failed_not_approved():
    """`Failed` is terminal. Reporting it as Approved describes a certificate
    that is still coming."""
    row = certificatesigningrequest_row(
        csr_object(conditions=[condition("Approved"), condition("Failed")])
    )

    assert row["state"] == "Failed"


def test_denied_is_reported_as_denied():
    assert certificatesigningrequest_row(
        csr_object(conditions=[condition("Denied")])
    )["state"] == "Denied"


@pytest.mark.parametrize(
    ("signer", "known"),
    [
        ("kubernetes.io/kubelet-serving", True),
        ("kubernetes.io/kube-apiserver-client", True),
        ("kubernetes.io/legacy-unknown", False),
        ("example.com/my-signer", False),
    ],
)
def test_signer_known_separates_the_built_ins_from_everything_else(signer, known):
    """`legacy-unknown` sits with the custom signers on purpose: it is deprecated
    and kube-controller-manager does not sign it either."""
    assert certificatesigningrequest_row(csr_object(signer=signer))["signer_known"] is known


def test_the_row_keeps_who_asked_apart_from_what_was_asked_for():
    row = certificatesigningrequest_row(
        csr_object(username="alice", request=request_pem(
            common_name="system:node:ip-10-0-1-4", organizations=("system:nodes",),
        ))
    )

    assert row["requestor"] == "alice"
    assert row["subject"]["common_name"] == "system:node:ip-10-0-1-4"


def test_a_row_whose_request_is_unreadable_still_renders_the_rest():
    """A malformed `spec.request` must not take a whole listing down with it."""
    row = certificatesigningrequest_row(csr_object(request="not-a-pem"))

    assert row["state"] == "Pending"
    assert row["subject"] is None
    assert row["decode_error"]


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def _consequences(decision="Approved", **kwargs):
    return csr_admin.consequences_for(
        certificatesigningrequest_row(csr_object(**kwargs)), decision
    )


def test_an_ordinary_kubelet_renewal_has_no_consequences_at_all():
    """The anti-friction property, asserted deliberately. A checkbox that appears
    on every request is a checkbox nobody reads on the one that matters."""
    assert _consequences() == []


def test_system_masters_is_named_as_cluster_admin_that_cannot_be_revoked():
    entries = _consequences(
        request=request_pem(common_name="dev", organizations=("system:masters",)),
        username="dev",
    )
    granted = next(e for e in entries if e["code"] == csr_admin.WARN_GRANTS_CLUSTER_ADMIN)

    assert "before RBAC is consulted" in granted["consequence"]
    assert "rotat" in granted["consequence"]


def test_a_node_identity_handed_to_something_else_is_named():
    """A bootstrap token asking for a kubelet's first certificate. Both the
    mismatch and what a node identity can reach are worth a tick, because they
    are two different facts about the same request."""
    entries = _consequences(username="system:bootstrap:abcdef")
    granted = next(
        e for e in entries if e["code"] == csr_admin.WARN_GRANTS_NODE_IDENTITY
    )

    assert "node authorizer" in granted["consequence"]
    assert "system:bootstrap:abcdef" in granted["consequence"]


def test_a_kubelet_renewing_its_own_certificate_raises_no_node_consequence():
    """Nearly every CSR on a running cluster. It grants the requestor nothing it
    does not already hold, and a checkbox here is one nobody reads on the day a
    request is not a renewal."""
    assert csr_admin.WARN_GRANTS_NODE_IDENTITY not in codes(_consequences())


def test_a_subject_that_is_not_the_requestor_is_named_with_both():
    entries = _consequences(username="system:bootstrap:abcdef")
    mismatch = next(
        e for e in entries if e["code"] == csr_admin.WARN_SUBJECT_IS_NOT_REQUESTOR
    )

    assert "system:bootstrap:abcdef" in mismatch["consequence"]
    assert "system:node:ip-10-0-1-4" in mismatch["consequence"]


def test_a_matching_subject_and_requestor_raises_nothing():
    assert csr_admin.WARN_SUBJECT_IS_NOT_REQUESTOR not in codes(_consequences())


@pytest.mark.parametrize(
    ("signer", "phrase"),
    [
        ("example.com/my-signer", "needs a signing controller"),
        ("kubernetes.io/legacy-unknown", "deprecated"),
    ],
)
def test_a_signer_nothing_signs_says_the_request_would_stop_at_approved(signer, phrase):
    entries = _consequences(signer=signer)
    unsigned = next(e for e in entries if e["code"] == csr_admin.WARN_NO_KNOWN_SIGNER)

    assert phrase in unsigned["consequence"]
    assert "Approved with no certificate" in unsigned["consequence"]


def test_the_missing_signer_is_not_a_consequence_of_denying():
    """Denying a request nothing would have signed costs nothing."""
    assert csr_admin.WARN_NO_KNOWN_SIGNER not in codes(
        _consequences("Denied", signer="example.com/my-signer")
    )


def test_an_undecodable_request_is_a_consequence_of_either_decision():
    for decision in ("Approved", "Denied"):
        assert csr_admin.WARN_REQUEST_UNDECODABLE in codes(
            _consequences(decision, request="not-a-pem")
        )


def test_denying_a_nodes_own_request_says_the_node_will_not_join():
    entries = _consequences("Denied")
    blocked = next(e for e in entries if e["code"] == csr_admin.WARN_DENY_BLOCKS_NODE)

    assert "ip-10-0-1-4" in blocked["label"]
    assert "not become Ready" in blocked["consequence"]


def test_cluster_admin_is_not_a_consequence_of_denying_it():
    assert csr_admin.WARN_GRANTS_CLUSTER_ADMIN not in codes(
        _consequences("Denied", request=request_pem(
            common_name="dev", organizations=("system:masters",),
        ))
    )


# --------------------------------------------------------------------------- #
# Validation and the plan
# --------------------------------------------------------------------------- #

def test_a_decision_that_is_not_a_condition_type_is_refused():
    with pytest.raises(Invalid) as caught:
        csr_admin.validate_decision("approve")

    assert "Approved" in caught.value.message


def test_the_plan_decodes_the_request_and_echoes_the_gate(cluster):
    cluster()

    plan = csr_admin.plan(NAME, "Approved")

    assert plan["request"]["subject"]["organizations"] == ["system:nodes"]
    assert plan["gate"]["enabled"] is True
    assert plan["blocked"] is None


def test_a_decided_request_is_blocked_on_the_plan_rather_than_an_error(cluster):
    """The plan is the screen where somebody works out what happened. Answering
    with an error alone would withhold the decoded subject at that moment."""
    cluster(FakeCluster(csr_object(conditions=[condition("Approved")])))

    plan = csr_admin.plan(NAME, "Denied")

    assert plan["blocked"] is not None
    assert "already approved" in plan["blocked"]["message"]
    assert plan["consequences"] == []
    assert plan["request"]["subject"] is not None


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def test_a_dry_run_puts_to_the_approval_subresource_and_reports_applied_false(cluster):
    fake = cluster()

    result = csr_admin.decide(NAME, "Approved", dry_run=True)

    assert result["applied"] is False
    assert fake.puts[0]["path"].endswith("/approval")
    assert fake.puts[0]["query"]["dryRun"] == "All"


def test_a_confirmed_decision_appends_the_condition_and_is_audited(cluster):
    fake = cluster()

    result = csr_admin.decide(NAME, "Approved", dry_run=False)

    assert result["applied"] is True
    appended = fake.puts[0]["body"]["status"]["conditions"][-1]
    assert appended["type"] == "Approved"
    assert appended["lastUpdateTime"]
    row = audit_rows()[0]
    assert row["verb"] == "update"
    assert "system:node:ip-10-0-1-4" in row["detail"]


def test_the_object_put_back_keeps_its_managed_fields(cluster):
    """The update sends the whole object. Dropping managedFields would hand the
    API server a different object than the one it stored, over a decision that
    cannot be taken back."""
    fake = cluster()

    csr_admin.decide(NAME, "Approved", dry_run=True)

    assert fake.puts[0]["body"]["metadata"]["managedFields"]


def test_building_the_body_does_not_mutate_the_object_being_diffed_against():
    """`build_body`'s input is also the left side of the diff. Mutating it in
    place produces a diff of the object against itself, which renders as "this
    write changes nothing" over an irreversible decision."""
    live = csr_object()
    before = copy.deepcopy(live)

    body = csr_admin.build_body(live, "Approved")

    assert live == before
    assert len(body["status"]["conditions"]) == 1


def test_an_unacknowledged_consequence_refuses_before_the_cluster_is_touched(cluster):
    fake = cluster(FakeCluster(csr_object(
        request=request_pem(common_name="dev", organizations=("system:masters",)),
        username="dev",
    )))

    with pytest.raises(Invalid) as caught:
        csr_admin.decide(NAME, "Approved", dry_run=False)

    assert fake.puts == []
    assert csr_admin.WARN_GRANTS_CLUSTER_ADMIN in caught.value.context["unacknowledged"]


def test_naming_every_consequence_lets_the_decision_through(cluster):
    fake = cluster(FakeCluster(csr_object(
        request=request_pem(common_name="dev", organizations=("system:masters",)),
        username="dev",
    )))
    plan = csr_admin.plan(NAME, "Approved")

    csr_admin.decide(
        NAME, "Approved", dry_run=False,
        acknowledge_consequences=[entry["code"] for entry in plan["consequences"]],
    )

    assert len(fake.puts) == 1


def test_the_signer_permission_is_preflighted_and_a_denial_stops_the_write(
    fake_k8s, db_engine, allow_mutations,
):
    """The permission people miss. `update certificatesigningrequests/approval`
    passes, and the API server's own admission plugin still refuses without
    `approve` on the signer — so the console asks first, by signerName."""
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", deny_signers)
    fake = FakeCluster()
    fake_k8s.api_client.returns("call_api", fake)

    with pytest.raises(RBACDenied) as caught:
        csr_admin.decide(NAME, "Approved", dry_run=True)

    assert fake.puts == []
    assert csr_admin.SIGNERS_RESOURCE in str(caught.value.context)
    # The review named the signer, not the request: the grant is per-signerName.
    reviews = fake_k8s.authorization_v1.called("create_self_subject_access_review")
    signer_review = [
        args[0] for args, _ in reviews
        if args[0].spec.resource_attributes.resource == csr_admin.SIGNERS_RESOURCE
    ]
    assert signer_review[0].spec.resource_attributes.name == KUBELET_SIGNER
    assert signer_review[0].spec.resource_attributes.verb == "approve"


def test_a_read_only_console_still_plans_and_previews_but_refuses_the_decision(
    fake_k8s, db_engine, monkeypatch,
):
    """`mutations_disabled`, not `rbac_denied` — and the dry run is not withheld,
    because what the request asks to become is a read."""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_allow_mutations", False)
    fake_k8s.authorization_v1.returns("create_self_subject_access_review", allow_all)
    fake_k8s.api_client.returns("call_api", FakeCluster())

    assert csr_admin.plan(NAME, "Approved")["gate"]["enabled"] is False
    assert csr_admin.decide(NAME, "Approved", dry_run=True)["applied"] is False
    with pytest.raises(MutationsDisabled):
        csr_admin.decide(NAME, "Approved", dry_run=False)


def test_a_stale_resource_version_conflicts_with_the_current_state(cluster):
    cluster()

    with pytest.raises(Conflict) as caught:
        csr_admin.decide(NAME, "Approved", dry_run=True, resource_version="1")

    assert caught.value.context["currentResourceVersion"] == "7719"
    assert caught.value.context["currentState"] == "Pending"


def test_deciding_an_already_decided_request_is_refused_before_the_put(cluster):
    """There is no un-approve. The API server refuses it too, with a message that
    names a field path rather than the decision and when it was made."""
    fake = cluster(FakeCluster(csr_object(
        conditions=[condition("Denied", when="2026-09-05T20:00:00Z")]
    )))

    with pytest.raises(Invalid) as caught:
        csr_admin.decide(NAME, "Approved", dry_run=False)

    assert fake.puts == []
    assert "2026-09-05T20:00:00Z" in caught.value.message


def test_the_response_carries_the_request_it_was_decided_against(cluster):
    """`applied: true` says the condition is recorded. Whether a certificate
    exists is a different field, and the response keeps the request as it was
    read so the client that recorded the confirmation has both."""
    cluster()

    result = csr_admin.decide(NAME, "Approved", dry_run=False)

    assert result["decision"] == "Approved"
    assert result["request"]["state"] == "Pending"
    assert result["request"]["issued"] is False
