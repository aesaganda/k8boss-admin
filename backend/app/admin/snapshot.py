"""
§22 — take a VolumeSnapshot of a PersistentVolumeClaim, and refuse to call it a
backup.

**The action.** An operator is about to do something they might regret — grow a
claim (§20), roll out a schema migration, delete a workload — and they want a
point they can get back to. `kubectl` can write the four-line object; §4's YAML
editor can too. What neither can do is tell them what they are actually getting,
and on this API the gap between what it is called and what it is is the widest
in Kubernetes.

**A CSI snapshot is not a backup.** For nearly every driver it is a
point-in-time reference *inside the same storage system* — the same array, the
same zone, often the same disk. If that storage is lost, the snapshot is lost
with it, because it was never anywhere else. It protects against the mistake you
are about to make. It does not protect against the failure of the thing holding
it. A console that renders a green "Ready" beside the word *snapshot* and says
nothing else is inviting the belief that gets somebody to skip a real backup, so
this module makes the operator name that fact before it writes.

**And it is crash-consistent, not quiesced.** Nothing here freezes a filesystem
or flushes a database. What the snapshot captures is what would be on disk after
a power cut at that instant. Journalled filesystems and most databases recover
from that; some do not, and none of them prefer it to a dump taken with the
application's own tooling.

**The third fact is about the future.** `deletionPolicy` lives on the
VolumeSnapshotClass, and it decides what happens when somebody later deletes the
namespaced VolumeSnapshot object. Under `Delete` — the common default — removing
that object destroys the snapshot in the storage system. Under `Retain` it does
not. The same click is bookkeeping under one policy and irreversible data loss
under the other, and the person who eventually makes it will be reading a
namespaced object, not the cluster-scoped class that decides. Reporting the
policy at the moment the snapshot is *created* is the only point in this
console's life where it is guaranteed to be in front of the right person.

`snapshot.storage.k8s.io` is CRD-backed and shipped by the external-snapshotter
rather than by Kubernetes, so a cluster without it serves nothing here. That is
§1.2's `unsupported`, which renders as an ordinary fact rather than red.

The gate is `ADMIN_ALLOW_MUTATIONS` alone, like §17, §18, §20 and §21: creating
one namespaced object is not the larger commitment §5.5's host-mounted pod is.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.admin.apply import create_fn
from app.admin.mutate import FeatureGate, mutate, read_only_switch
from app.errors import Invalid
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import (
    SNAPSHOT_DEFAULT_CLASS_ANNOTATION,
    get_field,
    volumesnapshotclass_row,
)

logger = logging.getLogger(__name__)

GROUP, VERSION, PLURAL = "snapshot.storage.k8s.io", "v1", "volumesnapshots"
CLASS_PLURAL = "volumesnapshotclasses"

#: RFC 1123 subdomain, which is what a VolumeSnapshot name must be. Checked here
#: so a bad name is a 422 naming the rule rather than a relayed admission error
#: about a regex the operator never saw.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
_MAX_NAME = 253

#: Consequence codes. Constants because the frontend branches on them, and a
#: typo in a string literal is a checkbox that never renders.
WARN_NOT_A_BACKUP = "snapshot_is_not_a_backup"
WARN_CRASH_CONSISTENT = "snapshot_is_crash_consistent"
WARN_DELETE_DESTROYS = "snapshot_delete_destroys_data"
WARN_DELETION_POLICY_UNKNOWN = "snapshot_deletion_policy_unknown"
WARN_CLAIM_NOT_BOUND = "snapshot_claim_not_bound"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """One switch, and the dry run is not withheld.

    On a read-only console the plan is still worth having: which class would be
    used, what its deletion policy is, and whether the claim is even bound are
    all reads, and they are what an operator wants before asking for the write.
    """
    return FeatureGate(
        feature="taking a volume snapshot",
        message="Taking a volume snapshot is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The plan is still available — the "
                "snapshot class that would be used, its deletion policy, and "
                "whether the claim is bound are all reads."
            )),
        ),
        enabled_detail="This deployment permits taking a volume snapshot.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §22's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """``{"name": "...", "snapshotClass": "..." | None}``.

    ``snapshotClass`` is **optional and its absence is meaningful**: it means
    "use the cluster's default class", which is a real thing the API does. It is
    not the same as a class this console could not read, and the plan keeps those
    two apart rather than reporting either as the other.
    """
    if not isinstance(payload, dict):
        raise _invalid("The request body must be an object.", parameter="body")

    raw = payload.get("name")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _invalid(
            "A name for the snapshot is required.",
            parameter="name",
            hint=(
                "Snapshots are not named for you: the name is how anyone finds "
                "this one again, and a generated one would be a string nobody "
                "recognises during the incident it was taken for."
            ),
        )
    if not isinstance(raw, str):
        raise _invalid("The name must be a string.", parameter="name")

    name = raw.strip()
    if len(name) > _MAX_NAME or not _NAME_RE.match(name):
        raise _invalid(
            f"{name!r} is not a valid object name.",
            parameter="name",
            hint=(
                "Use lowercase letters, digits, '-' and '.', starting and ending "
                f"with a letter or digit, at most {_MAX_NAME} characters."
            ),
        )

    snapshot_class = payload.get("snapshotClass")
    if snapshot_class is not None:
        if not isinstance(snapshot_class, str) or not snapshot_class.strip():
            raise _invalid(
                "snapshotClass must be a non-empty string, or omitted to use the "
                "cluster default.",
                parameter="snapshotClass",
            )
        snapshot_class = snapshot_class.strip()

    unknown = set(payload) - {"name", "snapshotClass"}
    if unknown:
        raise _invalid(
            "Unknown fields in the request body: " + ", ".join(sorted(unknown)),
            parameter="body",
        )

    return {"name": name, "snapshotClass": snapshot_class}


# --------------------------------------------------------------------------- #
# Which class, and what its deletion policy is
# --------------------------------------------------------------------------- #

def resolve_class(
    requested: str | None, unavailable: list[dict[str, Any]]
) -> dict[str, Any]:
    """Which VolumeSnapshotClass this snapshot will use, and what it does on delete.

    ``deletion_policy`` is **tri-state**, and the three states send an operator
    to three different places:

    * ``"Delete"`` / ``"Retain"`` — read off the class. Knowledge.
    * ``None`` — we could not find out. Either the listing was refused, or no
      class was named and the cluster has no default. Reporting that as
      ``Delete`` would put a data-loss warning in front of somebody whose class
      retains; reporting it as ``Retain`` would withhold one from somebody whose
      class deletes. Neither is acceptable, so it stays unknown and the operator
      acknowledges that it is.

    The whole listing is read rather than a single `get`, because the *default*
    class is only discoverable by looking at every class's annotations — there is
    no endpoint that answers "which one is the default".
    """
    classes: list[Any] | None = None
    with collect(unavailable, GROUP, CLASS_PLURAL):
        listing = reader.list_resource(GROUP, VERSION, CLASS_PLURAL, namespace=None, limit=500)
        # Assigned last, so a listing that raised leaves `classes` at None rather
        # than at an empty list that reads as "this cluster has no classes".
        classes = list(listing.get("items") or [])

    if classes is None:
        reason = unavailable[0]["reason"] if unavailable else "unreadable"
        return {
            "name": requested,
            "deletion_policy": None,
            "driver": None,
            "is_default": None,
            "reason": reason,
            "detail": (
                f"The VolumeSnapshotClass listing did not answer ({reason}), so "
                "this console cannot say which class this snapshot will use or "
                "what deleting it later will do to the data."
            ),
        }

    rows = [volumesnapshotclass_row(item) for item in classes]
    if requested is not None:
        match = next((row for row in rows if row["name"] == requested), None)
        if match is None:
            raise _invalid(
                f"No VolumeSnapshotClass named {requested!r} on this cluster.",
                parameter="snapshotClass",
                hint=(
                    "Available: " + (", ".join(row["name"] for row in rows if row["name"])
                                     or "none — this cluster defines no snapshot classes")
                    + "."
                ),
                available=[row["name"] for row in rows],
            )
        return {**match, "reason": None, "detail": (
            f"{match['name']} uses driver {match['driver']} and its deletionPolicy "
            f"is {match['deletion_policy']}."
        )}

    default = next((row for row in rows if row["is_default"]), None)
    if default is None:
        return {
            "name": None,
            "deletion_policy": None,
            "driver": None,
            "is_default": None,
            "reason": "no_default",
            "detail": (
                "No VolumeSnapshotClass was named and this cluster marks none as "
                "the default, so which class the snapshot controller will use — "
                "and what deleting the snapshot later will do — is not something "
                "this console can determine. The API server may well refuse the "
                "write outright."
            ),
        }
    return {**default, "reason": None, "detail": (
        f"No class was named, so the cluster default {default['name']} applies. "
        f"Its deletionPolicy is {default['deletion_policy']}."
    )}


# --------------------------------------------------------------------------- #
# The claim being snapshotted
# --------------------------------------------------------------------------- #

def claim_state(namespace: str, name: str) -> dict[str, Any]:
    """The claim as the plan and the write both need it. A failed read propagates.

    Never a default: a snapshot described against a claim we could not read would
    put the operator's own typing on both sides of the screen, and the size it
    reported would be a guess about the data being captured.
    """
    claim = reader.get_resource("", "v1", "persistentvolumeclaims", name, namespace=namespace)
    return {
        "name": get_field(claim, "metadata", "name"),
        "namespace": get_field(claim, "metadata", "namespace"),
        "phase": get_field(claim, "status", "phase"),
        "volume": get_field(claim, "spec", "volumeName"),
        "storage_class": get_field(claim, "spec", "storageClassName"),
        "capacity": get_field(claim, "status", "capacity", "storage"),
    }


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def consequences_for(claim: dict[str, Any], snapshot_class: dict[str, Any]) -> list[dict[str, Any]]:
    """What taking this snapshot does and does not give you.

    The first two are on **every** snapshot, and deliberately so: they are what
    the word "snapshot" is routinely believed to mean and does not, and being
    wrong about either is discovered during a restore, which is the worst
    possible moment to discover anything.
    """
    out: list[dict[str, Any]] = [
        {
            "code": WARN_NOT_A_BACKUP,
            "label": "A snapshot is not a backup",
            "consequence": (
                "For nearly every CSI driver this is a point-in-time reference "
                "held inside the same storage system as the volume — often the "
                "same array, the same zone, sometimes the same disk. It protects "
                "against the change you are about to make. It does not survive "
                "the loss of the storage holding it, because it is not anywhere "
                "else."
            ),
            "mitigation": (
                "Keep taking whatever real backups you take. If this is standing "
                "in for one, copy the data somewhere the storage system's failure "
                "would not reach."
            ),
        },
        {
            "code": WARN_CRASH_CONSISTENT,
            "label": "The data is captured as if the power were cut",
            "consequence": (
                "Nothing here freezes the filesystem or asks the application to "
                "flush. What is captured is what would be on disk at that instant "
                "after an abrupt stop. Journalled filesystems and most databases "
                "recover from that, but it is not equivalent to a dump taken with "
                "the application's own tooling, and some workloads do not survive "
                "it cleanly."
            ),
            "mitigation": (
                "For a database, take its own backup as well, or quiesce it "
                "before snapshotting if it supports that."
            ),
        },
    ]

    policy = snapshot_class["deletion_policy"]
    if policy == "Delete":
        out.append({
            "code": WARN_DELETE_DESTROYS,
            "label": "Deleting this snapshot later will destroy it in the storage system",
            "consequence": (
                f"The class {snapshot_class['name']} sets deletionPolicy: Delete, "
                "so removing this VolumeSnapshot object removes the underlying "
                "snapshot too. Whoever eventually deletes it will be looking at a "
                "namespaced object in a list, not at the cluster-scoped class that "
                "decides what that does."
            ),
            "mitigation": (
                "If this snapshot needs to outlive routine cleanup, use a class "
                "with deletionPolicy: Retain, and say so in the name."
            ),
        })
    elif policy is None:
        out.append({
            "code": WARN_DELETION_POLICY_UNKNOWN,
            "label": "What deleting this snapshot will do is unknown",
            "consequence": snapshot_class["detail"] + (
                " Deleting the object later may or may not destroy the data, and "
                "this console cannot tell you which."
            ),
            "mitigation": (
                "Read the VolumeSnapshotClass before relying on this snapshot "
                "surviving a cleanup."
            ),
        })

    if claim["phase"] != "Bound":
        out.append({
            "code": WARN_CLAIM_NOT_BOUND,
            "label": f"This claim is {claim['phase'] or 'not bound'}, so there may be nothing to capture",
            "consequence": (
                "A claim that is not Bound has no volume behind it yet. The "
                "snapshot object will be created and the controller will most "
                "likely leave it unready — this console is not refusing the write, "
                "because a claim can bind between now and then, but it will not "
                "pretend the result is a usable snapshot either."
            ),
            "mitigation": (
                "Wait for the claim to bind, then take the snapshot and check "
                "that readyToUse becomes true."
            ),
        })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Recomputed from the cluster at write time, never trusted from the plan: the
    default class can change, and the two facts that are always here are the ones
    a caller with a stale list would most like to skip.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This change has consequences that have not been acknowledged.",
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
# The object
# --------------------------------------------------------------------------- #

def build_snapshot(
    namespace: str, claim_name: str, request: dict[str, Any]
) -> dict[str, Any]:
    """The VolumeSnapshot to create.

    ``volumeSnapshotClassName`` is **omitted entirely** when the caller named no
    class, rather than sent as null or as the default's name resolved here.
    Omitting it is what asks the controller for the cluster default, and that
    resolution is the controller's to make at write time — pinning the name this
    console read a moment ago would quietly make the snapshot depend on which
    class was default when the dialog opened.
    """
    body: dict[str, Any] = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "VolumeSnapshot",
        "metadata": {"name": request["name"], "namespace": namespace},
        "spec": {"source": {"persistentVolumeClaimName": claim_name}},
    }
    if request["snapshotClass"] is not None:
        body["spec"]["volumeSnapshotClassName"] = request["snapshotClass"]
    return body


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan(namespace: str, claim_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/storage/claims/{namespace}/{name}/snapshot/plan`` — what this gives you.

    Ungated and unaudited: one claim read and one class listing. It does not
    dry-run the create — a dry run is a write request the caller has not asked
    for yet, and it needs the preflight the funnel does.

    A class named but absent is a `422` rather than a `blocked` entry: unlike
    §20's and §21's plans, there is nothing to decide from here — the name is
    wrong and the list of real ones is in the error's hint.
    """
    request = validate_request(payload)
    unavailable: list[dict[str, Any]] = []

    claim = claim_state(namespace, claim_name)
    snapshot_class = resolve_class(request["snapshotClass"], unavailable)

    return {
        "namespace": namespace,
        "claim": claim,
        "requested": {"name": request["name"], "snapshotClass": request["snapshotClass"]},
        "snapshotClass": snapshot_class,
        "consequences": consequences_for(claim, snapshot_class),
        "gate": enabled_state(),
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def take_snapshot(
    namespace: str,
    claim_name: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``POST /api/storage/claims/{namespace}/{name}/snapshot`` — one create, through the funnel.

    Order of refusals, each before the cluster is changed: request validation,
    the claim read — which must answer — the class resolution, and the
    acknowledgement check recomputed against the cluster as it is now. Then
    :func:`mutate`, which gates, preflights ``create volumesnapshots``, sends the
    create with ``dryRun=All`` when this is a preview, diffs against the API
    server's projection, and audits the outcome.

    The §1.5 response gains three keys: ``consequences`` (echoed, so the record of
    the confirmation is in the response the operator's client kept), ``claim`` and
    ``snapshotClass``.

    **`applied: true` means a VolumeSnapshot object exists.** It does not mean a
    snapshot has been taken: the controller does that afterwards, and the object
    reports it by setting ``status.readyToUse``, which starts out **null** and
    may end at false with an error. Nothing in this response is evidence that
    there is anything to restore from — §22's row is where that answer lives, and
    it is a tri-state for this reason.
    """
    request = validate_request(payload)
    unavailable: list[dict[str, Any]] = []

    claim = claim_state(namespace, claim_name)
    snapshot_class = resolve_class(request["snapshotClass"], unavailable)

    consequences = consequences_for(claim, snapshot_class)
    _require_acknowledgement(consequences, acknowledge_consequences)

    result = mutate(
        verb="create",
        group=GROUP,
        version=VERSION,
        plural=PLURAL,
        namespace=namespace,
        name=request["name"],
        dry_run=dry_run,
        gate=_gate(),
        apply_fn=create_fn(
            GROUP, VERSION, PLURAL,
            build_snapshot(namespace, claim_name, request),
            namespace=namespace,
            name=request["name"],
        ),
        before=None,
        # The claim is in the sentence, not just the snapshot's own name: the
        # question after an incident is what was captured, and a name somebody
        # chose under pressure does not always say.
        detail=(
            f"snapshot {namespace}/{request['name']} of claim {claim_name}"
            + (f" via {snapshot_class['name']}" if snapshot_class["name"] else "")
        ),
    )
    result["consequences"] = consequences
    result["claim"] = claim
    result["snapshotClass"] = snapshot_class
    return result


__all__ = [
    "build_snapshot",
    "claim_state",
    "consequences_for",
    "enabled_state",
    "plan",
    "resolve_class",
    "take_snapshot",
    "validate_request",
]
