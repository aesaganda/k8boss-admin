"""
§25 — CertificateSigningRequests: approving one, and reading it first.

**The failure this exists for.** `kubectl get csr` shows a name, a signer, a
requestor and an age. It does not show what the request *asks to become*, and
those are different fields: `spec.username` is the identity that submitted it,
and the common name and organizations inside `spec.request` are the identity the
certificate would carry. A request submitted by an ordinary user whose
organization is `system:masters` looks, in that listing and in the approve
command, exactly like a kubelet renewing its certificate.

`system:masters` is not a group RBAC grants. The API server's authorizer treats
it as cluster-admin **before RBAC is consulted**, so no Role bounds it and no
RoleBinding takes it back. A certificate carrying it is revocable only by
rotating the CA that signed it. That is the single most consequential
approve-button in a Kubernetes cluster, and the one with the least on screen.

So §25 decodes the PKCS#10 and puts the subject, the organizations and the SANs
in front of the operator before the confirming call — and a request that could
not be decoded reports that rather than an empty subject, which reads as a
certificate asking for nothing.

**Two things approval is not.**

*It is not issuance.* Approving records a condition. A **signer** then has to
act, and only `kube-controller-manager` signs the three `kubernetes.io/…`
signerNames — if it was started with a signing CA, which no API here reports. A
request for any other signerName needs a controller somebody installed, and
without one it sits `Approved` with no certificate, forever, looking like a
success. §25 keeps `Approved` and `Issued` as separate states for that reason
alone.

*It is not reversible.* The API server refuses any update that changes an
existing Approved or Denied condition. There is no un-approve, here or in
`kubectl`, and this module does not pretend otherwise: a request that has been
decided comes back `blocked`, and the dialog says so before the first click
rather than after it.

**Two permissions, and the second is the one people miss.** Writing the decision
needs `update certificatesigningrequests/approval`. It is not enough: the API
server's `CertificateApproval` admission plugin separately requires the
**`approve` verb on `certificates.k8s.io/signers`, named for this request's
signerName**. A console that preflighted only the first would enable the button,
pass its own check and be refused by the API server — §13's `routes/custom-host`
failure exactly. Both are preflighted, and the second from inside the apply step
so a read-only console still answers `mutations_disabled` first.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from app.admin import preflight
from app.admin.mutate import FeatureGate, audit_conflict, mutate, read_only_switch
from app.errors import Conflict, Invalid
from app.resources import catalog, reader
from app.resources.shaping import (
    CSR_PENDING,
    KUBERNETES_SIGNERS,
    LEGACY_SIGNER,
    MASTERS_GROUP,
    NODE_CN_PREFIX,
    NODES_GROUP,
    certificatesigningrequest_row,
    get_field,
    rfc3339,
)
from app.resources.transport import request_json

logger = logging.getLogger(__name__)

GROUP, VERSION, PLURAL = "certificates.k8s.io", "v1", "certificatesigningrequests"

#: The decision is written to a subresource, not to the object. RBAC names it
#: separately, and so does the funnel's preflight.
SUBRESOURCE = "approval"

#: The *other* resource the API server checks, with the signerName as its
#: resource name. Not a subresource of the request — a resource of its own, which
#: is why it cannot ride along on `mutate`'s `also_requires`.
SIGNERS_RESOURCE = "signers"

DECISION_APPROVE = "Approved"
DECISION_DENY = "Denied"
DECISIONS = (DECISION_APPROVE, DECISION_DENY)

#: Written into the condition this console appends, so `kubectl describe csr`
#: says where the decision came from. The *actor* is deliberately not put here:
#: the CSR is readable by anyone who can read CSRs, and the audit trail is where
#: "who approved this" is answered.
CONDITION_REASON = "K8BossAdminDecision"

WARN_GRANTS_CLUSTER_ADMIN = "csr_grants_cluster_admin"
WARN_GRANTS_NODE_IDENTITY = "csr_grants_node_identity"
WARN_SUBJECT_IS_NOT_REQUESTOR = "csr_subject_is_not_requestor"
WARN_REQUEST_UNDECODABLE = "csr_request_undecodable"
WARN_SIGNATURE_INVALID = "csr_signature_invalid"
WARN_NO_KNOWN_SIGNER = "csr_no_known_signer"
WARN_DENY_BLOCKS_NODE = "csr_deny_blocks_node"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """`ADMIN_ALLOW_MUTATIONS` alone, and the dry run is not withheld.

    No switch of its own, for §23's reason rather than §5.5's. The real control
    here is already a permission, and a finer one than a deployment flag could
    be: `approve` on `signers` is granted **per signerName**, so a cluster can
    let this console approve kubelet-serving certificates and nothing else. A
    boolean on the deployment would be a coarser copy of a control RBAC already
    expresses exactly, and the coarser copy is the one that ends up on.

    The plan stays available read-only because it is the part worth having: what
    a request asks to become is a read, and it is the fact somebody wants before
    they go and ask for the permission to act on it.
    """
    return FeatureGate(
        feature="deciding certificate signing requests",
        message="Approving and denying certificate requests is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The plan is still available — what this "
                "request asks to become, who asked for it, and whether anything on "
                "this cluster would sign it are all reads."
            )),
        ),
        enabled_detail="This deployment permits deciding certificate signing requests.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §25's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _path(name: str, *, subresource: str | None = None) -> str:
    return reader.resource_path(GROUP, VERSION, PLURAL, name=name, subresource=subresource)


def _live(name: str) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """``(raw object, its §25 row, its resourceVersion)``. A failed read propagates.

    The **raw** object is kept because it is what gets PUT back: an update that
    dropped `managedFields` would hand the API server a different object than the
    one it stored, over a decision that cannot be taken back. The diff is taken
    over the trimmed pair instead, so it is a two-line condition rather than four
    hundred lines of server-side-apply bookkeeping.
    """
    raw = catalog.raw_get(_path(name))
    version = get_field(raw, "metadata", "resourceVersion")
    return raw, certificatesigningrequest_row(raw), (str(version) if version else None)


def validate_decision(value: Any) -> str:
    """`Approved` or `Denied`, or `422 invalid`.

    Spelled as the condition types the API uses rather than as verbs, because
    that is what lands in the object and what `kubectl describe csr` prints. A
    body naming an action and an object naming a state is one translation layer
    where a typo becomes an approval.
    """
    if value not in DECISIONS:
        raise Invalid(
            f"decision must be one of {', '.join(DECISIONS)}.",
            hint=(
                "Approved records that the certificate may be signed; Denied "
                "records that it may not. Neither can be changed afterwards."
            ),
            context={"parameter": "decision", "value": value},
        )
    return str(value)


def _already_decided(row: dict[str, Any]) -> Invalid | None:
    """The refusal for a request that has already been settled, or ``None``.

    Refused here rather than left to the API server, whose message for it names
    a field path. The console can say *which* decision was made and when, which
    is the thing an operator looking at an unexpected state actually needs.
    """
    if row["state"] == CSR_PENDING:
        return None
    decided = row["conditions"].get(DECISION_APPROVE) or row["conditions"].get(DECISION_DENY)
    when = (decided or {}).get("lastUpdateTime")
    return Invalid(
        f"This request was already {row['state'].lower()}"
        + (f" at {when}" if when else "")
        + ".",
        hint=(
            "A decision cannot be changed — the API server refuses any update "
            "that rewrites an Approved or Denied condition. The requester has to "
            "submit a new request."
        ),
        context={"parameter": "decision", "state": row["state"]},
    )


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def consequences_for(row: dict[str, Any], decision: str) -> list[dict[str, Any]]:
    """What deciding this request means, each as a code the caller names back.

    Deliberately **empty for an ordinary request** — a kubelet renewing a
    certificate it already holds, decoded cleanly, for a signer this cluster
    runs. Manufacturing a checkbox for that case is how a checkbox stops being
    read, and the two entries below that matter most are the ones that must still
    be read on the day they appear.
    """
    out: list[dict[str, Any]] = []
    subject = row["subject"] or {}
    organizations = subject.get("organizations") or []
    common_name = subject.get("common_name")

    if row["decode_error"]:
        out.append({
            "code": WARN_REQUEST_UNDECODABLE,
            "label": "This console could not read what the request asks for",
            "consequence": (
                f"{row['decode_error']} So the subject, the organizations and the "
                "SANs above are unknown — not empty. Approving means signing a "
                "certificate whose identity nobody on this screen has seen, and "
                "the organizations are what become the holder's groups."
            ),
            "mitigation": (
                "Decode spec.request yourself (`openssl req -noout -text`) before "
                "deciding, or deny it and ask for one this console can read."
            ),
        })

    if row["signature_valid"] is False:
        out.append({
            "code": WARN_SIGNATURE_INVALID,
            "label": "The request is not signed by the key it carries",
            "consequence": (
                "A PKCS#10 request is self-signed by its own private key, and this "
                "one's signature does not verify. Whoever submitted it may not hold "
                "the key the certificate would be issued for."
            ),
            "mitigation": "Deny it and ask for a freshly generated request.",
        })

    if decision == DECISION_APPROVE:
        if MASTERS_GROUP in organizations:
            out.append({
                "code": WARN_GRANTS_CLUSTER_ADMIN,
                "label": f"This grants cluster-admin — the certificate asks for {MASTERS_GROUP}",
                "consequence": (
                    "The API server's authorizer treats system:masters as "
                    "cluster-admin **before RBAC is consulted**, so no Role limits "
                    "it and no RoleBinding takes it back. Whoever holds the private "
                    "key for this request would have unrestricted access to this "
                    "cluster until the signing CA is rotated, which invalidates "
                    "every other certificate it issued."
                ),
                "mitigation": (
                    "Almost nothing legitimately needs this. If a human needs "
                    "cluster access, issue an ordinary client certificate and bind "
                    "it to a ClusterRole, which can be revoked."
                ),
            })

        requestor = row["requestor"]
        # Deliberately *not* fired when the kubelet is renewing its own
        # certificate, which is what nearly every CSR on a running cluster is.
        # That request grants nothing the requestor does not already hold, and a
        # checkbox on it is a checkbox nobody reads on the day one of these is
        # not a renewal. What makes a node identity worth stopping for is it
        # being handed to somebody who is not already that node.
        if NODES_GROUP in organizations and common_name != requestor:
            out.append({
                "code": WARN_GRANTS_NODE_IDENTITY,
                "label": "This grants a node identity to something that is not that node",
                "consequence": (
                    "A certificate in system:nodes"
                    + (f" with the common name {common_name}" if common_name else "")
                    + " is evaluated by the node authorizer: it can read every "
                    "Secret and ConfigMap mounted by a pod bound to that node, and "
                    "update that node's own object. "
                    + (f"{requestor} is asking for it. " if requestor else "")
                    + "That is ordinary for a bootstrap token creating a kubelet's "
                    "first certificate and is not ordinary for anything else."
                ),
                "mitigation": (
                    "Check that this really is a node joining — a bootstrap request "
                    "comes from system:bootstrap:… — before approving."
                ),
            })

        if common_name and requestor and common_name != requestor:
            out.append({
                "code": WARN_SUBJECT_IS_NOT_REQUESTOR,
                "label": "The identity being requested is not the identity that asked",
                "consequence": (
                    f"{requestor} submitted this request, and the certificate would "
                    f"carry the common name {common_name}. Approving it issues "
                    f"{requestor} a credential for somebody else. That is ordinary "
                    "for a bootstrap token creating a kubelet's first certificate "
                    "and is worth a second look everywhere else."
                ),
                "mitigation": (
                    "Confirm the requestor is entitled to act for that identity "
                    "before approving."
                ),
            })

        signer = row["signer_name"]
        if signer and signer not in KUBERNETES_SIGNERS:
            legacy = signer == LEGACY_SIGNER
            out.append({
                "code": WARN_NO_KNOWN_SIGNER,
                "label": f"Nothing built into this cluster signs {signer}",
                "consequence": (
                    (
                        "kubernetes.io/legacy-unknown is deprecated and is not "
                        "handled by kube-controller-manager's signer at all. "
                        if legacy else
                        "kube-controller-manager only signs the three "
                        "kubernetes.io/… signerNames; this is not one of them, so "
                        "it needs a signing controller somebody installed. "
                    )
                    + "If none is running, approving this leaves the request "
                    "Approved with no certificate — indefinitely, and looking "
                    "exactly like a success."
                ),
                "mitigation": (
                    "Check that a controller for this signerName is running before "
                    "approving, and re-read the request afterwards: Issued, not "
                    "Approved, is what says a certificate exists."
                ),
            })

    if decision == DECISION_DENY:
        if common_name and str(common_name).startswith(NODE_CN_PREFIX):
            node = str(common_name)[len(NODE_CN_PREFIX):]
            out.append({
                "code": WARN_DENY_BLOCKS_NODE,
                "label": f"This is {node}'s own certificate request",
                "consequence": (
                    "Denying it means that kubelet does not get the certificate it "
                    "asked for. A node joining the cluster will not become Ready, "
                    "and a node rotating an expiring certificate will stop being "
                    "able to talk to the API server when the old one runs out."
                ),
                "mitigation": (
                    "Deny it if the request is not really from that node. If it is, "
                    "the node needs a request that will be approved instead."
                ),
            })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a decision whose consequences the caller has not accepted by name.

    Recomputed from the request as it is *now*, never trusted from the plan. The
    object itself does not change between the two — a pending CSR is immutable in
    the fields that matter here — but somebody else deciding it in between does,
    and that is caught by the state check rather than this one. Recomputing
    anyway costs a dictionary and removes the question.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This decision has consequences that have not been acknowledged.",
        detail="; ".join(f"{entry['code']}: {entry['label']}" for entry in missing),
        hint=(
            "Re-send with acknowledgeConsequences naming each of "
            + ", ".join(entry["code"] for entry in missing)
            + "."
        ),
        context={
            "parameter": "acknowledgeConsequences",
            "unacknowledged": [entry["code"] for entry in missing],
        },
    )


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan(name: str, decision: str) -> dict[str, Any]:
    """``POST /api/certificates/signing-requests/{name}/plan`` (§25).

    Ungated and unaudited: one read and a PKCS#10 decode. It does not dry-run the
    update — a dry run is a write request the caller has not made yet, and it
    needs the preflight the funnel does.

    A request that is already decided comes back `blocked` rather than as a
    `422`, the way §20's, §21's and §24's plans do: this is the screen where the
    request is *read*, and answering with an error alone would withhold the
    decoded subject at the moment somebody is trying to work out what happened.
    """
    wanted = validate_decision(decision)
    _raw, row, resource_version = _live(name)

    refusal = _already_decided(row)
    return {
        "name": name,
        "resourceVersion": resource_version,
        "decision": wanted,
        "request": row,
        # Never both: a decided request has no consequences left to accept, and
        # one that is still pending has nothing standing in the way.
        "blocked": (
            {"message": refusal.message, "hint": refusal.hint, "context": refusal.context}
            if refusal else None
        ),
        "consequences": [] if refusal else consequences_for(row, wanted),
        "gate": enabled_state(),
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def build_condition(decision: str) -> dict[str, Any]:
    """The condition this console appends. ``lastUpdateTime`` set as kubectl does."""
    return {
        "type": decision,
        "status": "True",
        "reason": CONDITION_REASON,
        "message": (
            "Approved through the k8boss-admin console."
            if decision == DECISION_APPROVE
            else "Denied through the k8boss-admin console."
        ),
        "lastUpdateTime": rfc3339(datetime.now(timezone.utc)),
    }


def build_body(raw: dict[str, Any], decision: str) -> dict[str, Any]:
    """The object to PUT: the live request with one condition appended.

    A copy, never the caller's object: `raw` is also the left side of the diff,
    and mutating it in place would produce a diff of nothing against itself —
    which renders as "this write changes nothing" over a decision that cannot be
    taken back.
    """
    body = copy.deepcopy(raw)
    status = body.setdefault("status", {})
    conditions = list(status.get("conditions") or [])
    conditions.append(build_condition(decision))
    status["conditions"] = conditions
    return body


def decide(
    name: str,
    decision: str,
    *,
    dry_run: bool = True,
    resource_version: str | None = None,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/certificates/signing-requests/{name}`` (§25) — through the funnel.

    Order of refusals, each before the cluster is changed: the decision is
    validated, the request is read (which must answer), §0.4's concurrency check,
    the already-decided check recomputed against the object as it is *now*, and
    the acknowledgement check. Then :func:`mutate`, which gates, preflights
    ``update certificatesigningrequests/approval``, and audits the outcome —
    with the **second** preflight, ``approve`` on the signer, made from inside
    the apply step so a read-only console answers `mutations_disabled` before it
    reports a permission problem.

    ``PUT``, not ``PATCH``: the approval subresource takes the whole object, and
    the object carries the `resourceVersion` this console read, so §0.4 is the
    API server's decision as well as this module's.

    **`applied: true` means the condition is recorded.** It does not mean a
    certificate exists. A signer has to act, and on a cluster whose signer is
    missing or not configured for this signerName the request stops at
    `Approved` — which is why `Issued` is a separate state and why the response
    carries the request as it was read.
    """
    wanted = validate_decision(decision)
    raw, row, live_version = _live(name)

    if resource_version and live_version and resource_version != live_version:
        conflict = Conflict(
            f"The request {name} changed while you were reading it.",
            detail=f"You are deciding version {resource_version}; the cluster has {live_version}.",
            hint="Reload the request and preview again against what it says now.",
            context={
                "group": GROUP, "version": VERSION, "resource": PLURAL,
                "name": name, "verb": "update", "subresource": SUBRESOURCE,
                "currentResourceVersion": live_version,
                "currentState": row["state"],
            },
        )
        # Rule 5 applies to a conflict too, and this one fires before the first
        # `mutate()` — so without this the trail held nothing to say two people
        # were deciding the same request at once, which is the whole question
        # rule 4 exists to make answerable. On a signing request that matters
        # the losing decision is the one nobody can find afterwards.
        audit_conflict(
            verb="update", group=GROUP, version=VERSION, plural=PLURAL,
            namespace=None, name=name, subresource=SUBRESOURCE,
            dry_run=dry_run, error=conflict,
            detail=(
                f"csr {name}: refused, deciding {resource_version} and the "
                f"cluster has {live_version}"
            ),
        )
        raise conflict

    refusal = _already_decided(row)
    if refusal:
        raise refusal

    consequences = consequences_for(row, wanted)
    _require_acknowledgement(consequences, acknowledge_consequences)

    signer = row["signer_name"]
    before = reader.trim(raw, for_list=False)
    body = build_body(raw, wanted)

    def apply_fn(is_dry_run: bool) -> tuple[dict[str, Any] | None, list[str]]:
        # The permission the API server checks that RBAC on the subresource does
        # not imply. Inside the apply step, after the gate, so a read-only
        # console is told it is read-only rather than sent to edit a ClusterRole.
        if signer:
            preflight.require("approve", GROUP, SIGNERS_RESOURCE, name=str(signer))
        updated, warnings = request_json(
            "PUT", _path(name, subresource=SUBRESOURCE),
            query=[("dryRun", "All" if is_dry_run else None)],
            body=body,
        )
        return reader.trim(updated, for_list=False), warnings

    result = mutate(
        verb="update",
        group=GROUP,
        version=VERSION,
        plural=PLURAL,
        namespace=None,
        name=name,
        subresource=SUBRESOURCE,
        dry_run=dry_run,
        gate=_gate(),
        apply_fn=apply_fn,
        before=before,
        detail=(
            f"csr {name}: {wanted.lower()}"
            + (f" (signer {signer})" if signer else "")
            + (
                f", subject {row['subject']['common_name']}"
                if (row["subject"] or {}).get("common_name") else ""
            )
        ),
    )
    result["decision"] = wanted
    result["request"] = row
    result["consequences"] = consequences
    return result


__all__ = [
    "DECISIONS",
    "DECISION_APPROVE",
    "DECISION_DENY",
    "SIGNERS_RESOURCE",
    "SUBRESOURCE",
    "build_body",
    "build_condition",
    "consequences_for",
    "decide",
    "enabled_state",
    "plan",
    "validate_decision",
]
