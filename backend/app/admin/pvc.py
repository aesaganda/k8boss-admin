"""
§20 — grow a PersistentVolumeClaim, and say plainly what a green result means.

**Why this is not §4's YAML editor.** A claim that has filled up is one of the
most ordinary production incidents there is, and the fix — ask for more — is one
field. The editor can already write that field. What it cannot do is any of the
four things that decide whether the write is a good idea:

* **It cannot tell you the StorageClass forbids expansion**, so the edit is
  accepted by the form, rejected by the API server, and the operator reads a
  relayed admission message about a field they did not think they were touching.
* **It cannot stop you shrinking.** Typing ``5Gi`` where the claim says ``50Gi``
  is one keystroke, it is refused by the API server on a bound claim — and on
  the claims where it is *not* refused it is a data-loss request. This module
  refuses it here, by arithmetic, before anything is sent.
* **It cannot name the pods that mount the volume**, which is what decides
  whether the filesystem grows now or after a restart.
* **And it cannot say what `applied: true` means**, which is the whole problem
  below.

**The claim this endpoint is careful not to make.** Expansion is two acts by two
different parties. This console patches ``spec.resources.requests.storage`` —
that is the entire write, and it is the only thing ``applied: true`` attests.
Afterwards the storage provider grows the volume (the claim carries a
``Resizing`` condition while it does), and then the *filesystem* on it has to be
grown too, which on many CSI drivers cannot happen while a pod has it mounted:
the claim sits at ``FileSystemResizePending`` until every pod using it restarts.
``status.capacity`` is the number that says how much space a workload actually
has, and **this write does not change it**. An operator who reads a green result
as "the volume is bigger now" is wrong about the disk their database is filling,
which is exactly the confident wrong answer §0 treats as a defect — so it is a
consequence they acknowledge by name, on every expansion, including the ones
that go on to work perfectly.

**Expansion is one way.** No Kubernetes API shrinks a bound claim, and no
provider this console can see refunds one. That is the second thing acknowledged
every time, and it is acknowledged separately because it is a different mistake:
the first is about *when*, this one is about *whether you can undo it*.

The gate is `ADMIN_ALLOW_MUTATIONS` alone, like §17 and §18. Growing a volume is
a namespaced write on an object the console already lists; it is not the larger
commitment §5.5's host-mounted pod is, and giving it a switch of its own would
be a switch nobody could describe the meaning of.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin.apply import MERGE_PATCH, patch_fn
from app.admin.mutate import FeatureGate, audit_conflict, mutate, read_only_switch
from app.errors import ClusterUnreachable, Conflict, Invalid
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import get_field, parse_bytes

logger = logging.getLogger(__name__)

#: The annotation that carried the class before `spec.storageClassName` existed.
#: Still written by some provisioners and still the only answer on old claims.
STORAGE_CLASS_ANNOTATION = "volume.beta.kubernetes.io/storage-class"

#: Conditions the claim carries while an expansion is in flight. Both are
#: `status.conditions[].type` values on a PersistentVolumeClaim.
RESIZING = "Resizing"
FILESYSTEM_RESIZE_PENDING = "FileSystemResizePending"

#: Consequence codes. Constants because the frontend branches on them, and a
#: typo in a string literal is a checkbox that never renders and a confirmation
#: nobody can give.
WARN_NOT_IMMEDIATE = "pvc_capacity_is_not_immediate"
WARN_ONE_WAY = "pvc_expansion_is_one_way"
WARN_EXPANSION_UNKNOWN = "pvc_expansion_unknown"
WARN_IN_USE = "pvc_in_use_offline_resize"
WARN_RESIZE_PENDING = "pvc_resize_already_pending"
WARN_MOUNTS_UNKNOWN = "pvc_mounts_unknown"

#: Paging for the pod listing behind :func:`mounted_by`. Same shape and the same
#: reason as §32's EndpointSlice tally: no field selector exists for a pod's
#: volumes, so every pod in the namespace has to be looked at one page at a
#: time, and a namespace with more than one page of pods is ordinary.
_POD_PAGE_SIZE = 500
_MAX_POD_PAGES = 10


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """One switch, and the dry run is not withheld.

    The projection is where the API server's own validation lands — the
    StorageClass check this module cannot make when it could not read the class,
    and any admission webhook on PVCs — so withholding the preview would leave
    an operator with strictly less than they have today with `kubectl`.
    """
    return FeatureGate(
        feature="expanding a persistent volume claim",
        message="Expanding a persistent volume claim is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The plan is still available — the "
                "claim's size, whether its StorageClass permits expansion at all, "
                "and which pods have the volume mounted are all reads."
            )),
        ),
        enabled_detail="This deployment permits expanding a persistent volume claim.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §20's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """``{"size": "20Gi", "resourceVersion": "…"}``, checked as far as arithmetic goes.

    The size is kept as **the string the operator typed** and parsed only for
    comparison. Reformatting it — sending ``21474836480`` for their ``20Gi`` —
    would put a number in the diff they have to convert back before they can
    confirm it, on the one screen where the number is the whole decision.
    """
    if not isinstance(payload, dict):
        raise _invalid("The request body must be an object.", parameter="body")

    raw = payload.get("size")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _invalid(
            "A size is required.",
            parameter="size",
            hint="Send the requested capacity as a Kubernetes quantity, e.g. \"20Gi\".",
        )
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise _invalid("The size must be a Kubernetes quantity string.", parameter="size")

    size = str(raw).strip()
    parsed = parse_bytes(size)
    if parsed is None:
        raise _invalid(
            f"{size!r} is not a Kubernetes quantity.",
            parameter="size",
            hint="Use a quantity the API server accepts, such as 20Gi, 500M or 1Ti.",
        )
    if parsed <= 0:
        raise _invalid("The size must be greater than zero.", parameter="size")

    resource_version = payload.get("resourceVersion")
    if resource_version is not None and not isinstance(resource_version, str):
        raise _invalid("resourceVersion must be a string.", parameter="resourceVersion")

    unknown = set(payload) - {"size", "resourceVersion"}
    if unknown:
        raise _invalid(
            "Unknown fields in the request body: " + ", ".join(sorted(unknown)),
            parameter="body",
        )

    return {"size": size, "size_bytes": parsed, "resourceVersion": resource_version}


# --------------------------------------------------------------------------- #
# Reading the claim, its class and what mounts it
# --------------------------------------------------------------------------- #

def _conditions(claim: Any) -> list[dict[str, Any]]:
    out = []
    for condition in get_field(claim, "status", "conditions", default=[]) or []:
        out.append({
            "type": get_field(condition, "type"),
            "status": get_field(condition, "status"),
            "reason": get_field(condition, "reason"),
            "message": get_field(condition, "message"),
        })
    return out


def claim_state(claim: Any) -> dict[str, Any]:
    """The claim as the plan and the write both need it.

    ``requested_bytes`` and ``capacity_bytes`` are **both** here and are not the
    same number, which is the point. The first is what the claim asks for and is
    what this endpoint edits; the second is what the volume actually provides and
    is what a workload runs out of. On a claim mid-expansion they differ, and a
    page showing only one of them cannot say so.
    """
    spec_class = get_field(claim, "spec", "storageClassName")
    annotated = (get_field(claim, "metadata", "annotations", default={}) or {}).get(
        STORAGE_CLASS_ANNOTATION
    )
    return {
        "namespace": get_field(claim, "metadata", "namespace"),
        "name": get_field(claim, "metadata", "name"),
        "phase": get_field(claim, "status", "phase"),
        "volume": get_field(claim, "spec", "volumeName"),
        "storage_class": spec_class or annotated,
        "requested": get_field(claim, "spec", "resources", "requests", "storage"),
        "requested_bytes": parse_bytes(
            get_field(claim, "spec", "resources", "requests", "storage")
        ),
        "capacity": get_field(claim, "status", "capacity", "storage"),
        "capacity_bytes": parse_bytes(get_field(claim, "status", "capacity", "storage")),
        "conditions": _conditions(claim),
    }


def expansion_support(
    storage_class: str | None, unavailable: list[dict[str, Any]]
) -> dict[str, Any]:
    """Whether the claim's StorageClass permits expansion — **tri-state**.

    ``True``/``False`` come from ``allowVolumeExpansion`` on the class. ``None``
    means we could not find out, and it is not a synonym for either:

    * A claim with **no class at all** is bound to a statically provisioned
      volume. Whether that can grow is between the administrator and the storage
      behind it, and no API here reports on it.
    * A class we were **refused** tells us nothing about what it allows. Reading
      that as ``False`` would refuse a write the cluster would have accepted and
      send an operator to argue with a StorageClass that is already correct —
      the same mistake §0.2 forbids on a failed access review.

    Only ``False`` is a refusal, because only ``False`` is knowledge.
    """
    if not storage_class:
        return {
            "supported": None,
            "storage_class": None,
            "reason": "no_storage_class",
            "detail": (
                "This claim names no StorageClass, so it is bound to a volume "
                "somebody provisioned by hand. Whether that volume can grow is "
                "not something any API on this cluster reports."
            ),
        }

    klass: Any = None
    with collect(unavailable, "storage.k8s.io", "storageclasses"):
        klass = reader.get_resource(
            "storage.k8s.io", "v1", "storageclasses", storage_class, namespace=None,
        )
    if klass is None:
        return {
            "supported": None,
            "storage_class": storage_class,
            "reason": "unreadable",
            "detail": (
                f"The StorageClass {storage_class} could not be read — it may "
                "have been deleted since this claim bound, or this console may "
                "not be permitted to read it — so whether it permits expansion is "
                "unknown. That is not the same as it forbidding expansion, and "
                "this console will not refuse a write on the strength of a read "
                "that did not happen."
            ),
        }

    allowed = get_field(klass, "allowVolumeExpansion")
    return {
        "supported": bool(allowed),
        "storage_class": storage_class,
        "reason": "allowed" if allowed else "not_allowed",
        "detail": (
            f"StorageClass {storage_class} sets allowVolumeExpansion: true."
            if allowed else
            f"StorageClass {storage_class} does not set allowVolumeExpansion, so "
            "the API server refuses any change to this claim's requested size."
        ),
    }


def mounted_by(
    namespace: str, name: str, unavailable: list[dict[str, Any]]
) -> list[str] | None:
    """Names of the pods with this claim mounted, or ``None`` if we could not look.

    ``None`` rather than ``[]`` for §0.1's reason, and here the two answers point
    an operator in opposite directions: an empty list says the filesystem can be
    grown without touching a workload, and that is the sentence that gets an
    expansion started on a volume a database has open.
    """
    pods: list[str] | None = None
    with collect(unavailable, "", "pods", namespace=namespace):
        found: list[str] = []
        cont: str | None = None
        for _page in range(_MAX_POD_PAGES):
            listing = reader.list_resource(
                "", "v1", "pods", namespace=namespace,
                limit=_POD_PAGE_SIZE, cont=cont,
            )
            for pod in listing.get("items") or []:
                for volume in get_field(pod, "spec", "volumes", default=[]) or []:
                    claim = get_field(volume, "persistentVolumeClaim", "claimName")
                    if claim == name:
                        found.append(get_field(pod, "metadata", "name"))
                        break
            # Every page, not just the first. One listing of 500 reported every
            # pod behind the cursor as not mounting the claim, and this list is
            # what tells an operator which workloads an expansion affects — so a
            # short one reads as "nothing else is attached" on the screen where
            # that sentence starts an offline resize. An expired cursor is a
            # 410, which `from_api_exception` maps to `invalid` and `collect`
            # re-raises rather than recording: a cursor that died mid-listing
            # surfaces as an error instead of as a shorter list.
            cont = listing.get("continue") or None
            if not cont:
                break
        else:
            # Still pods behind the cursor after the budget. Refused rather than
            # returned short, for §0.1: an unread page and a namespace where
            # nothing has the claim open are the same list once it is returned,
            # and only one of them is safe to act on. `collect` maps this to a
            # `timeout` entry and leaves `pods` at None.
            raise ClusterUnreachable(
                "The pod listing did not finish.",
                detail=(
                    f"Stopped after {_MAX_POD_PAGES} pages of {_POD_PAGE_SIZE} "
                    f"pods in {namespace} with more remaining, so which of them "
                    "mount this claim could not be established."
                ),
                context={"cause": "timeout"},
            )
        # Assigned last, so a listing that raised leaves `pods` at None rather
        # than at a partial tally that reads as "nothing has this mounted".
        pods = sorted(pod for pod in found if pod)
    return pods


# --------------------------------------------------------------------------- #
# The refusals — the things arithmetic settles before anything is sent
# --------------------------------------------------------------------------- #

def check_expandable(
    current: dict[str, Any], size_bytes: int, expansion: dict[str, Any]
) -> None:
    """Refuse what cannot work, with the reason, before a request leaves here.

    Three refusals, and each is a `422 invalid` rather than a relayed admission
    error, because the API server's message for these names a field the operator
    did not know they were editing.

    Deliberately **not** a refusal: an expansion the StorageClass may or may not
    allow. Only ``supported is False`` refuses — see :func:`expansion_support`.
    """
    if current["phase"] != "Bound":
        raise _invalid(
            f"This claim is {current['phase'] or 'not bound'}, so there is no "
            "volume to expand.",
            parameter="size",
            hint=(
                "An unbound claim has not been provisioned yet. Edit its requested "
                "size in the YAML editor if that is what you mean; there is nothing "
                "for a resize to act on."
            ),
            phase=current["phase"],
        )

    requested = current["requested_bytes"]
    if requested is None:
        raise _invalid(
            "This claim's requested size could not be read, so there is nothing "
            "to grow it from.",
            parameter="size",
            hint=(
                "Without the current request this console cannot tell an "
                "expansion from a shrink, and it will not send a size it cannot "
                "compare. Use the YAML editor if you need to edit it anyway."
            ),
            currentRequested=current["requested"],
        )

    if size_bytes < requested:
        raise _invalid(
            "A persistent volume claim cannot be shrunk.",
            parameter="size",
            hint=(
                f"This claim requests {current['requested']}. Ask for more than "
                "that, or leave it alone — no Kubernetes API makes a bound claim "
                "smaller, and a provider that did would be discarding the data "
                "past the new end."
            ),
            currentRequested=current["requested"],
        )
    if size_bytes == requested:
        raise _invalid(
            f"This claim already requests {current['requested']}.",
            parameter="size",
            hint="Ask for a larger size, or nothing needs to change.",
            currentRequested=current["requested"],
        )

    if expansion["supported"] is False:
        raise _invalid(
            f"StorageClass {expansion['storage_class']} does not allow volume "
            "expansion.",
            parameter="size",
            hint=(
                "An administrator can set allowVolumeExpansion: true on the "
                "StorageClass; existing claims pick it up. Until then the API "
                "server refuses every change to this claim's size, and this "
                "console will not send one."
            ),
            storageClass=expansion["storage_class"],
        )


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def consequences_for(
    current: dict[str, Any],
    size: str,
    expansion: dict[str, Any],
    mounts: list[str] | None,
) -> list[dict[str, Any]]:
    """What growing this claim means, each as a code the caller names back.

    Two of these are on **every** expansion, and that is deliberate rather than
    an oversight about friction: they are the two things people are reliably
    wrong about, and being wrong about either costs an incident rather than a
    click.
    """
    out: list[dict[str, Any]] = [
        {
            "code": WARN_NOT_IMMEDIATE,
            "label": "This changes the request, not the space a workload has",
            "consequence": (
                f"Writing this asks for {size}. The volume grows when the storage "
                "provider grows it, and the filesystem on it grows after that — "
                "status.capacity is what a workload actually has, and this write "
                "does not change it. A claim can sit at the new request and the "
                "old capacity for a long time, or for ever if the provider "
                "refuses."
            ),
            "mitigation": (
                "Watch the claim's capacity and its conditions afterwards rather "
                "than treating a successful write as more disk. Resizing means it "
                "is in progress; FileSystemResizePending means the volume grew and "
                "the filesystem has not."
            ),
        },
        {
            "code": WARN_ONE_WAY,
            "label": "Expansion cannot be undone",
            "consequence": (
                f"No Kubernetes API makes a bound claim smaller again. Going from "
                f"{current['requested'] or 'its current size'} to {size} is "
                "permanent for the life of this claim, and on a cloud provider it "
                "is what you are billed for from the moment the volume grows."
            ),
            "mitigation": (
                "If you are not sure how much is enough, the cheap mistake is a "
                "second expansion later, not a large first one."
            ),
        },
    ]

    if expansion["supported"] is None:
        out.append({
            "code": WARN_EXPANSION_UNKNOWN,
            "label": "Whether this volume can grow at all is unknown",
            "consequence": expansion["detail"] + (
                " The write will be sent and the API server will decide; this "
                "console cannot tell you in advance."
            ),
            "mitigation": (
                "Preview first. A dry run goes through the same validation as the "
                "real write, so a refusal shows up there without changing anything."
            ),
        })

    if mounts is None:
        out.append({
            "code": WARN_MOUNTS_UNKNOWN,
            "label": "Which pods have this mounted could not be read",
            "consequence": (
                "The pod listing for this namespace did not answer, so this "
                "console cannot say whether anything has the volume open. It is "
                "not saying nothing does."
            ),
            "mitigation": (
                "Check with kubectl before expanding if an offline resize would "
                "mean stopping something that matters."
            ),
        })
    elif mounts:
        out.append({
            "code": WARN_IN_USE,
            "label": f"{len(mounts)} pod{'' if len(mounts) == 1 else 's'} have this volume mounted",
            "consequence": (
                "Many CSI drivers cannot grow a filesystem while it is mounted. "
                f"The claim will report FileSystemResizePending until every pod "
                f"using it restarts — {', '.join(mounts)}. Until then the volume "
                "is larger and the filesystem the workload writes to is not."
            ),
            "mitigation": (
                "Plan the restart with the expansion rather than discovering it "
                "when the disk is still full afterwards. Drivers that support "
                "online expansion will do it without one, and the claim's "
                "conditions are what tell you which kind you have."
            ),
        })

    pending = [
        condition["type"] for condition in current["conditions"]
        if condition["type"] in (RESIZING, FILESYSTEM_RESIZE_PENDING)
        and condition["status"] == "True"
    ]
    if pending:
        out.append({
            "code": WARN_RESIZE_PENDING,
            "label": "An expansion of this claim has not finished",
            "consequence": (
                f"This claim already carries {', '.join(pending)}. Asking for more "
                "now stacks a second expansion on one the provider has not "
                "completed, and what the volume ends up at is decided by the "
                "driver rather than by either request."
            ),
            "mitigation": (
                "Wait for capacity to reach the size already asked for, then "
                "decide whether more is still needed."
            ),
        })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Recomputed from the claim as it is now, never trusted from the plan: a caller
    that could acknowledge a code the server did not derive could acknowledge
    every code it liked, and the one that matters most here is on every write.
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
# The read the plan and the write share
# --------------------------------------------------------------------------- #

def _live(namespace: str, name: str) -> tuple[Any, dict[str, Any], str | None]:
    """``(live claim, its state, its resourceVersion)``. A failed read propagates.

    Never a default: a change described against a claim we could not read is a
    diff about nothing, and the size in front of the operator would be the one
    they typed rather than the one they are changing.
    """
    live = reader.get_resource("", "v1", "persistentvolumeclaims", name, namespace=namespace)
    version = get_field(live, "metadata", "resourceVersion")
    return live, claim_state(live), (str(version) if version else None)


def build_patch(size: str, *, resource_version: str | None) -> dict[str, Any]:
    """The merge patch: one field, plus rule 4's version when the caller sent one.

    A merge patch rather than a strategic one because ``requests`` is a plain
    map: recursive object merge leaves every other key alone, and there is no
    list here whose merge key would matter.
    """
    metadata: dict[str, Any] = {}
    if resource_version:
        metadata["resourceVersion"] = resource_version
    patch: dict[str, Any] = {"spec": {"resources": {"requests": {"storage": size}}}}
    if metadata:
        patch["metadata"] = metadata
    return patch


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan(namespace: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/storage/claims/{namespace}/{name}/expand/plan`` — what this means.

    Ungated and unaudited: one claim read, one StorageClass read, one pod
    listing, and arithmetic. It does **not** dry-run the patch — a dry run is a
    write request the caller has not asked for yet, and it needs the preflight
    the funnel does.

    The two secondary reads degrade on their own. A refused StorageClass leaves
    ``expansion.supported`` at ``null`` and a refused pod listing leaves
    ``mountedBy`` at ``null``; both become consequences to acknowledge rather
    than either a refusal or a silent "fine".

    A size this claim cannot be given comes back as ``blocked`` — the same
    refusal :func:`expand` raises, as data — rather than as a `422`. The plan is
    the screen where an operator decides *what size to ask for*, and one that
    answered a too-small number with an error and nothing else would withhold
    the claim's current size, its capacity and its mounts at exactly the moment
    those are the three facts needed. The write still refuses.
    """
    request = validate_request(payload)
    unavailable: list[dict[str, Any]] = []

    _claim, current, resource_version = _live(namespace, name)
    expansion = expansion_support(current["storage_class"], unavailable)
    mounts = mounted_by(namespace, name, unavailable)

    # The same refusals the write raises, reported here as data. One source of
    # truth for the reasons — this catches what :func:`check_expandable` raises
    # rather than restating it — and a plan that still answers with the claim's
    # size, capacity and mounts, which is what an operator needs on screen to
    # decide what size to ask for instead. The write raises; the plan describes.
    blocked: dict[str, Any] | None = None
    consequences: list[dict[str, Any]] = []
    try:
        check_expandable(current, request["size_bytes"], expansion)
    except Invalid as refusal:
        blocked = {
            "message": refusal.message,
            "hint": refusal.hint,
            "context": refusal.context,
        }
    else:
        consequences = consequences_for(current, request["size"], expansion, mounts)

    return {
        "namespace": namespace,
        "name": name,
        "current": current,
        "requested": {"size": request["size"], "size_bytes": request["size_bytes"]},
        "expansion": expansion,
        "mountedBy": mounts,
        "resourceVersion": resource_version,
        # Never both: a plan that is blocked has no consequences to accept, and
        # one that is not has nothing standing in the way.
        "blocked": blocked,
        "consequences": consequences,
        "gate": enabled_state(),
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def expand(
    namespace: str,
    name: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/storage/claims/{namespace}/{name}/size`` — one patch, through the funnel.

    Order of refusals, each before the cluster is changed: request validation,
    the claim read — which must answer — the concurrency check, the arithmetic
    refusals recomputed against the claim as it is *now* rather than as the plan
    saw it, and the acknowledgement check. Then :func:`mutate`, which gates,
    preflights ``patch persistentvolumeclaims``, sends the patch with
    ``dryRun=All`` when this is a preview, diffs live against the API server's
    projection, and audits the outcome.

    The §1.5 response gains four keys: ``consequences`` (echoed, so the record of
    the confirmation is in the response the operator's client kept), ``current``
    and ``requested``, and ``expansion``.

    **``applied: true`` means this claim now requests the new size.** It does not
    mean the volume is bigger, and it very often does not mean the filesystem is:
    ``current.capacity`` in this same response is what a workload has, and it is
    read from before the write. The consequences say so rather than leaving an
    operator to infer it from a green result.
    """
    request = validate_request(payload)
    unavailable: list[dict[str, Any]] = []

    live, current, live_version = _live(namespace, name)
    sent_version = request["resourceVersion"]
    if sent_version and live_version and sent_version != live_version:
        conflict = Conflict(
            f"The claim {namespace}/{name} changed while you were reading it.",
            detail=(
                f"You are editing version {sent_version}; the cluster has "
                f"{live_version}."
            ),
            hint="Reload the claim and preview again against what it says now.",
            context={
                "group": "", "version": "v1", "resource": "persistentvolumeclaims",
                "namespace": namespace, "name": name, "verb": "patch",
                "currentResourceVersion": live_version,
                "currentSize": current["requested"],
                "currentCapacity": current["capacity"],
            },
        )
        # Rule 5 applies to a conflict too, and this one fires before the first
        # `mutate()` — so without this the trail held nothing to say two people
        # were resizing the same claim at once, which is the whole question
        # rule 4 exists to make answerable.
        audit_conflict(
            verb="patch", group="", version="v1", plural="persistentvolumeclaims",
            namespace=namespace, name=name, dry_run=dry_run, error=conflict,
            detail=(
                f"expand pvc {namespace}/{name}: refused, editing "
                f"{sent_version} and the cluster has {live_version}"
            ),
        )
        raise conflict

    expansion = expansion_support(current["storage_class"], unavailable)
    mounts = mounted_by(namespace, name, unavailable)
    check_expandable(current, request["size_bytes"], expansion)

    consequences = consequences_for(current, request["size"], expansion, mounts)
    _require_acknowledgement(consequences, acknowledge_consequences)

    result = mutate(
        verb="patch",
        group="",
        version="v1",
        plural="persistentvolumeclaims",
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        gate=_gate(),
        apply_fn=patch_fn(
            "", "v1", "persistentvolumeclaims", name,
            build_patch(request["size"], resource_version=sent_version or live_version),
            namespace=namespace,
            content_type=MERGE_PATCH,
        ),
        before=live,
        # Both sizes in the sentence, because the question after an incident is
        # what the claim was before somebody grew it — and the diff digest alone
        # cannot answer that without the object it was taken over.
        detail=(
            f"expand pvc {namespace}/{name}: "
            f"{current['requested'] or 'unknown'} -> {request['size']}"
        ),
    )
    result["consequences"] = consequences
    result["current"] = current
    result["requested"] = {"size": request["size"], "size_bytes": request["size_bytes"]}
    result["expansion"] = expansion
    result["mountedBy"] = mounts
    return result


__all__ = [
    "build_patch",
    "claim_state",
    "consequences_for",
    "enabled_state",
    "expand",
    "expansion_support",
    "mounted_by",
    "plan",
    "validate_request",
]
