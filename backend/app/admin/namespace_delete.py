"""
§26 — deleting a namespace, and what goes with it.

**This is the failure `docs/api-contract.md` §0.1 already names.** Its motivating
story ends *"an operator reading 'this namespace has no pods' acts on it — during
a cleanup, that means deleting the namespace."* Every read in this console has
been built so that sentence cannot be produced. The delete itself was not: §4's
delete previews `before=live, after=null`, which is one object's YAML
disappearing. What actually disappears is everything the namespace contains, and
`kubectl delete namespace` says as little about it as this console did.

Four of those things are not obvious from the namespace object, and one of them
is not reversible:

**The volumes.** A PersistentVolumeClaim is deleted with the namespace. What
happens to the *volume* behind it is decided by the PersistentVolume's
`persistentVolumeReclaimPolicy`, which lives on a cluster-scoped object nobody
is looking at: `Delete` means the storage provider destroys the disk — the data
is gone, not unbound — and `Retain` means the volume survives as `Released` and
has to be cleaned up by hand. Those are opposite outcomes behind one button, and
a claim whose volume this console could not read is reported as **unknown**,
never as either.

**The addresses.** A `LoadBalancer` Service takes its cloud load balancer and its
external IP with it. Recreating the Service later gets a different address, and
whatever points at the old one — DNS, a firewall rule, somebody's bookmark —
keeps pointing at nothing.

**The admission webhooks.** A ValidatingWebhookConfiguration whose backing
Service lives in this namespace is a cluster-scoped object that survives the
delete and stops having anything to talk to. With `failurePolicy: Fail` — which
is the **default** in `admissionregistration.k8s.io/v1` — it then refuses every
write it intercepts, cluster-wide. That is §19's finding, arriving one step
earlier: before the namespace goes rather than after.

**The finalizers.** A namespace that will not finish deleting is the most common
complaint about this operation, and the cause is always a finalizer whose
controller is not running. They are listed *before* the delete, and — because
this module's plan is a read — the same plan run against a namespace already
stuck in `Terminating` is the diagnosis of why.

**What `applied: true` means here.** The namespace has a `deletionTimestamp` and
the namespace controller has started. It does not mean the namespace is gone: it
can sit in `Terminating` indefinitely, which is exactly what the finalizer
finding is about. The write's own summary says so rather than reporting a
deletion that has not finished.

**Scope.** §26 is about namespaces and deliberately not a general cascade
previewer. Computing what deleting an arbitrary object takes with it means
walking `ownerReferences` across every kind the cluster serves — the same cost
as the inventory below, for a far weaker payoff, since a Deployment taking its
ReplicaSets and Pods is understood and a namespace taking a database's volume is
not. §4's delete is unchanged and still the way to delete anything else.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin import apply
from app.admin.mutate import FeatureGate, read_only_switch
from app.errors import Invalid
from app.resources import catalog, reader
from app.resources.envelope import collect
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

GROUP, VERSION, PLURAL = "", "v1", "namespaces"

#: How many objects of each kind the inventory fetches. The objects themselves
#: are needed — the volume, load-balancer and finalizer findings are all scanned
#: out of them — so this is a real page rather than a count query. A kind with
#: more than this is reported as `truncated`, and the findings below are then
#: explicitly *over what was seen* rather than over the namespace.
PAGE = 200

#: `Background` is the only propagation policy §26 offers, and the reason is
#: worth stating because the other two look like they would help.
#:
#: `Foreground` makes the API call block until the whole cascade has finished,
#: which for a namespace is a request that hangs for as long as the slowest
#: finalizer. `Orphan` sounds like it saves the contents and does not: the
#: namespace controller deletes everything *in* the namespace regardless of
#: ownerReferences, so orphaning changes which objects outlive their owner and
#: not which objects survive.
PROPAGATION_POLICY = "Background"

#: `persistentVolumeReclaimPolicy` values. `Delete` is the one that destroys
#: data; `Retain` leaves a `Released` volume for somebody to clean up.
RECLAIM_DELETE = "Delete"
RECLAIM_RETAIN = "Retain"

#: The `failurePolicy` an `admissionregistration.k8s.io/v1` webhook has when it
#: declares none. It is `Fail`, which means an *absent* field is the dangerous
#: one — the opposite of the v1beta1 default people remember.
DEFAULT_FAILURE_POLICY = "Fail"

#: Every namespace carries this one; the API server puts it there. Anything else
#: in `spec.finalizers` belongs to a controller that has to run before the
#: namespace can go.
KUBERNETES_FINALIZER = "kubernetes"

WARN_DESTROYS_VOLUME_DATA = "namespace_destroys_volume_data"
WARN_VOLUME_FATE_UNKNOWN = "namespace_volume_fate_unknown"
WARN_RELEASES_VOLUMES = "namespace_releases_volumes"
WARN_DROPS_LOAD_BALANCER = "namespace_drops_load_balancer"
WARN_BREAKS_ADMISSION_WEBHOOK = "namespace_breaks_admission_webhook"
WARN_FINALIZERS_MAY_HANG = "namespace_finalizers_may_hang"
WARN_INVENTORY_INCOMPLETE = "namespace_inventory_incomplete"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """`ADMIN_ALLOW_MUTATIONS` alone, and the dry run is not withheld.

    No switch of its own: §4 can already delete a namespace on this deployment,
    and a flag that disabled only the endpoint which *explains* the deletion
    would leave the unexplained one in place — which is the reverse of the point.
    """
    return FeatureGate(
        feature="deleting a namespace",
        message="Deleting a namespace is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The plan is still available — what a "
                "namespace contains, which volumes it would destroy and which "
                "admission webhooks it would break are all reads, and they are "
                "worth having whether or not this console may act on them."
            )),
        ),
        enabled_detail="This deployment permits deleting a namespace.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §26's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# The inventory
# --------------------------------------------------------------------------- #

def namespaced_kinds() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(kinds, unavailable)`` — every namespaced kind this cluster can list.

    Driven by discovery rather than by a curated list, because a curated list is
    a **completeness claim** that goes stale the first time somebody installs a
    CRD. A namespace full of an operator's custom resources is exactly the case
    where "here is what is in it" has to include them.

    Preferred versions only, and deduplicated by ``(group, resource)``: a CRD
    serving `v1alpha1` and `v1` side by side holds one set of objects, and
    listing both would count them twice.
    """
    items, unavailable = catalog.discover()
    seen: set[tuple[str, str]] = set()
    kinds: list[dict[str, Any]] = []
    for item in items:
        if not item.get("namespaced") or "list" not in (item.get("verbs") or []):
            continue
        if not item.get("preferred"):
            continue
        key = (item["group"], item["resource"])
        if key in seen:
            continue
        seen.add(key)
        kinds.append({
            "group": item["group"],
            "version": item["version"],
            "resource": item["resource"],
            "kind": item["kind"],
        })
    kinds.sort(key=lambda k: (k["group"], k["resource"]))
    return kinds, unavailable


def _count_from(listing: dict[str, Any]) -> tuple[int | None, bool]:
    """``(count, truncated)`` for one page.

    Exact when the page held everything. When it did not, the API server's
    ``remainingItemCount`` is used if it sent one — it does not always, and
    §0.1's corollary applies to a count as much as to anything else: a number
    this console could not derive is ``None``, never the page size, which would
    read as "there are exactly two hundred of these".
    """
    items = listing.get("items") or []
    if not listing.get("continue"):
        return len(items), False
    remaining = listing.get("remaining")
    if isinstance(remaining, int) and not isinstance(remaining, bool):
        return len(items) + remaining, True
    return None, True


def inventory(name: str, unavailable: list[dict[str, Any]]) -> dict[str, Any]:
    """What the namespace holds, by kind, plus the objects the findings scan.

    **There is no grand total, on purpose.** `events` is served by both the core
    group and `events.k8s.io` over the same underlying objects, so any sum across
    kinds double-counts them — and a single headline number is the wrong headline
    anyway. What matters is which volumes are destroyed and which webhooks break,
    not that a namespace contains 1,247 things.

    A kind whose listing failed keeps ``count: null`` and adds one `unavailable`
    entry. Nothing here falls back to zero: a namespace reported as holding no
    PersistentVolumeClaims when the listing was refused is the deletion this
    module exists to prevent.
    """
    kinds, discovery_gaps = namespaced_kinds()
    unavailable.extend(discovery_gaps)

    rows: list[dict[str, Any]] = []
    objects: dict[str, list[Any]] = {}
    for kind in kinds:
        listing: dict[str, Any] | None = None
        with collect(unavailable, kind["group"], kind["resource"], namespace=name):
            listing = reader.list_resource(
                kind["group"], kind["version"], kind["resource"],
                namespace=name, limit=PAGE,
            )
        if listing is None:
            rows.append({**kind, "count": None, "truncated": None})
            continue
        count, truncated = _count_from(listing)
        rows.append({**kind, "count": count, "truncated": truncated})
        objects[f"{kind['group']}/{kind['resource']}"] = listing.get("items") or []

    rows.sort(key=lambda row: (-(row["count"] or 0), row["group"], row["resource"]))
    return {"kinds": rows, "objects": objects}


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #

def volume_fates(claims: list[Any], unavailable: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per PersistentVolumeClaim: what happens to the data behind it.

    `reclaim_policy` is `None` when the claim is unbound or its volume could not
    be read, and that stays `None` all the way to the screen. The two real
    answers point in opposite directions — `Delete` destroys the data and
    `Retain` keeps it — so guessing either one is a claim about somebody's
    database made by a console that did not look.
    """
    rows: list[dict[str, Any]] = []
    for claim in claims:
        claim_name = get_field(claim, "metadata", "name")
        volume = get_field(claim, "spec", "volumeName")
        row = {
            "claim": claim_name,
            "volume": volume or None,
            "phase": get_field(claim, "status", "phase"),
            "capacity": get_field(claim, "status", "capacity", "storage"),
            "storage_class": get_field(claim, "spec", "storageClassName"),
            "reclaim_policy": None,
            "reason": None,
        }
        if not volume:
            # An unbound claim has no volume yet, so nothing is destroyed by
            # deleting it — a real answer, not a gap, and said as one.
            row["reason"] = (
                "This claim is not bound to a volume, so there is no stored data "
                "behind it to lose."
            )
            rows.append(row)
            continue

        live: dict[str, Any] | None = None
        with collect(unavailable, "", "persistentvolumes"):
            live = reader.get_resource("", "v1", "persistentvolumes", str(volume))
        if live is None:
            row["reason"] = (
                "The volume behind this claim could not be read, so whether its "
                "data is destroyed or kept is unknown."
            )
        else:
            row["reclaim_policy"] = get_field(live, "spec", "persistentVolumeReclaimPolicy")
        rows.append(row)
    return rows


def load_balancers(services: list[Any]) -> list[dict[str, Any]]:
    """Services whose deletion releases an address something outside points at."""
    rows: list[dict[str, Any]] = []
    for service in services:
        if get_field(service, "spec", "type") != "LoadBalancer":
            continue
        ingress = get_field(service, "status", "loadBalancer", "ingress", default=[]) or []
        rows.append({
            "name": get_field(service, "metadata", "name"),
            # Empty when the provider has not assigned one yet — which is a real
            # "no address to lose", distinct from a Service whose address this
            # console did not read. Both come from the same object, so there is
            # no third state here.
            "addresses": [
                str(get_field(entry, "ip") or get_field(entry, "hostname") or "")
                for entry in ingress
                if get_field(entry, "ip") or get_field(entry, "hostname")
            ],
        })
    return rows


def finalizer_holders(objects: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Objects carrying a finalizer, which is what leaves a namespace Terminating.

    Scanned out of the inventory's own pages rather than re-listed. A kind the
    inventory truncated is therefore scanned only as far as it was read, which is
    why `truncated` travels with the plan and is a consequence of its own.
    """
    rows: list[dict[str, Any]] = []
    for key, items in objects.items():
        for item in items:
            finalizers = get_field(item, "metadata", "finalizers", default=[]) or []
            if not finalizers:
                continue
            rows.append({
                "resource": key,
                "name": get_field(item, "metadata", "name"),
                "finalizers": [str(value) for value in finalizers],
            })
    rows.sort(key=lambda row: (row["resource"], str(row["name"])))
    return rows


def webhook_backends(name: str, unavailable: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Admission webhooks served from inside this namespace. ``None`` if unread.

    The configurations are cluster-scoped, so they survive the delete with
    nothing behind them. `failure_policy` defaults to `Fail` when the field is
    absent, which is the v1 default and the dangerous direction — a webhook that
    fails closed and has no backend refuses every write it intercepts, on the
    whole cluster.
    """
    found: list[dict[str, Any]] = []
    seen_any = False
    for group_resource, plural in (
        ("validatingwebhookconfigurations", "validatingwebhookconfigurations"),
        ("mutatingwebhookconfigurations", "mutatingwebhookconfigurations"),
    ):
        listing: dict[str, Any] | None = None
        with collect(unavailable, "admissionregistration.k8s.io", group_resource):
            listing = reader.list_resource(
                "admissionregistration.k8s.io", "v1", plural, limit=500,
            )
        if listing is None:
            continue
        seen_any = True
        for configuration in listing.get("items") or []:
            for webhook in get_field(configuration, "webhooks", default=[]) or []:
                service = get_field(webhook, "clientConfig", "service")
                if get_field(service, "namespace") != name:
                    continue
                found.append({
                    "configuration": get_field(configuration, "metadata", "name"),
                    "kind": get_field(configuration, "kind") or plural,
                    "webhook": get_field(webhook, "name"),
                    "service": get_field(service, "name"),
                    "failure_policy": (
                        get_field(webhook, "failurePolicy") or DEFAULT_FAILURE_POLICY
                    ),
                })
    # `None` when neither listing answered: "no webhook points here" and "we
    # could not look" are the difference between a safe delete and a cluster
    # that stops accepting writes.
    return found if seen_any else None


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def _names(rows: list[dict[str, Any]], key: str, limit: int = 5) -> str:
    shown = [str(row.get(key)) for row in rows[:limit]]
    return ", ".join(shown) + ("…" if len(rows) > limit else "")


def consequences_for(report: dict[str, Any]) -> list[dict[str, Any]]:
    """What deleting this namespace means, each as a code the caller names back."""
    out: list[dict[str, Any]] = []

    destroyed = [row for row in report["volumes"] if row["reclaim_policy"] == RECLAIM_DELETE]
    retained = [row for row in report["volumes"] if row["reclaim_policy"] == RECLAIM_RETAIN]
    unknown = [
        row for row in report["volumes"]
        if row["reclaim_policy"] is None and row["volume"]
    ]

    if destroyed:
        out.append({
            "code": WARN_DESTROYS_VOLUME_DATA,
            "label": (
                f"{len(destroyed)} volume{'' if len(destroyed) == 1 else 's'} "
                "will be destroyed, not released"
            ),
            "consequence": (
                _names(destroyed, "claim")
                + " bind volumes whose reclaim policy is Delete, so the storage "
                "provider deletes the underlying disk when the claim goes. The "
                "data is gone — this is not a claim being unbound from a volume "
                "that survives, and nothing in this console or in Kubernetes "
                "brings it back."
            ),
            "mitigation": (
                "Take a snapshot first (the Storage page does), or edit each "
                "PersistentVolume's persistentVolumeReclaimPolicy to Retain "
                "before deleting the namespace."
            ),
        })

    if unknown:
        out.append({
            "code": WARN_VOLUME_FATE_UNKNOWN,
            "label": (
                f"Whether {len(unknown)} volume{'' if len(unknown) == 1 else 's'} "
                "survive this is unknown"
            ),
            "consequence": (
                _names(unknown, "claim")
                + " are bound to volumes this console could not read, so it "
                "cannot tell you whether their data is destroyed or kept. It is "
                "not reporting that they are safe."
            ),
            "mitigation": (
                "Read the PersistentVolumes yourself — the reclaim policy is on "
                "the volume, not the claim — or grant the console `get "
                "persistentvolumes` and try again."
            ),
        })

    if retained:
        out.append({
            "code": WARN_RELEASES_VOLUMES,
            "label": (
                f"{len(retained)} volume{'' if len(retained) == 1 else 's'} "
                "will be left behind for somebody to clean up"
            ),
            "consequence": (
                _names(retained, "claim")
                + " bind volumes with reclaim policy Retain. The data survives, "
                "and so does the volume — as `Released`, which no new claim can "
                "bind to until its claimRef is cleared by hand."
            ),
            "mitigation": (
                "Note the volume names now: after the namespace is gone, working "
                "out which released volume belonged to which claim is guesswork."
            ),
        })

    if report["load_balancers"]:
        rows = report["load_balancers"]
        addresses = [address for row in rows for address in row["addresses"]]
        out.append({
            "code": WARN_DROPS_LOAD_BALANCER,
            "label": (
                f"{len(rows)} load balancer{'' if len(rows) == 1 else 's'} "
                "and their addresses go with it"
            ),
            "consequence": (
                _names(rows, "name")
                + (f" currently answer on {', '.join(addresses[:5])}. " if addresses else " ")
                + "Deleting the Service releases the cloud load balancer and its "
                "address. Recreating the Service later gets a different one, and "
                "whatever points at the old address — DNS, a firewall rule, a "
                "partner's allowlist — keeps pointing at nothing."
            ),
            "mitigation": (
                "Check what resolves to these addresses before deleting, and "
                "reserve a static address first if the address itself matters."
            ),
        })

    webhooks = report["webhooks"]
    if webhooks is None:
        out.append({
            "code": WARN_BREAKS_ADMISSION_WEBHOOK,
            "label": "Whether an admission webhook is served from here is unknown",
            "consequence": (
                "The webhook configurations could not be read. A "
                "ValidatingWebhookConfiguration whose backing Service lives in "
                "this namespace survives the delete with nothing behind it, and "
                "with failurePolicy: Fail — the v1 default — it then refuses "
                "every write it intercepts, cluster-wide. This console cannot "
                "tell you whether one does."
            ),
            "mitigation": (
                "List the webhook configurations yourself and check each "
                "clientConfig.service.namespace before deleting."
            ),
        })
    elif webhooks:
        failing = [row for row in webhooks if row["failure_policy"] == DEFAULT_FAILURE_POLICY]
        out.append({
            "code": WARN_BREAKS_ADMISSION_WEBHOOK,
            "label": (
                f"{len(webhooks)} admission webhook{'' if len(webhooks) == 1 else 's'} "
                f"{'is' if len(webhooks) == 1 else 'are'} served from this namespace"
                + (
                    f", {len(failing)} of them failing closed" if failing else
                    ", all failing open"
                )
            ),
            "consequence": (
                _names(webhooks, "configuration")
                + " point at a Service in this namespace. The configurations are "
                "cluster-scoped and survive the delete; the Service does not. "
                + (
                    "With failurePolicy: Fail the API server then refuses every "
                    "write those webhooks intercept, across the whole cluster, "
                    "until the configuration is removed."
                    if failing else
                    "They fail open, so writes they intercept are admitted "
                    "unchecked rather than refused — whatever those webhooks "
                    "were enforcing stops being enforced."
                )
            ),
            "mitigation": (
                "Delete the webhook configuration before the namespace, not "
                "after: once the Service is gone, a failing-closed webhook can "
                "block the very write that would remove it."
            ),
        })

    if report["finalizers"]:
        rows = report["finalizers"]
        out.append({
            "code": WARN_FINALIZERS_MAY_HANG,
            "label": (
                f"{len(rows)} object{'' if len(rows) == 1 else 's'} carry a "
                "finalizer and can leave this stuck in Terminating"
            ),
            "consequence": (
                _names(rows, "name")
                + " hold finalizers, and a finalizer is a promise that a "
                "controller will do something before the object may go. If that "
                "controller is not running — because it was uninstalled, or "
                "because it lives in this same namespace — the namespace stays "
                "in Terminating and no amount of re-deleting moves it."
            ),
            "mitigation": (
                "Check that each finalizer's controller is still running. Do not "
                "clear finalizers by hand to unstick it: that skips the cleanup "
                "they exist to guarantee, which is usually an external resource "
                "nobody will now delete."
            ),
        })

    incomplete = [
        row for row in report["inventory"]["kinds"]
        if row["count"] is None or row["truncated"]
    ]
    if incomplete:
        out.append({
            "code": WARN_INVENTORY_INCOMPLETE,
            "label": "This inventory is not complete",
            "consequence": (
                f"{len(incomplete)} kind(s) were refused or held more objects "
                f"than the {PAGE} this console reads per kind: "
                + _names(incomplete, "resource")
                + ". Everything above — the volumes, the load balancers, the "
                "finalizers — is what was found in what was read, not what is in "
                "the namespace."
            ),
            "mitigation": (
                "Treat the findings as a floor rather than a total, and read the "
                "kinds named above directly if any of them matters."
            ),
        })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a delete whose consequences the caller has not accepted by name.

    Recomputed against the namespace as it is at write time, never trusted from
    the plan: a volume can be provisioned, a LoadBalancer can come up and a
    webhook can be installed between the two calls, and what the operator has to
    have accepted is what is true when the delete lands.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This deletion has consequences that have not been acknowledged.",
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

def _claims(objects: dict[str, list[Any]]) -> list[Any]:
    return objects.get("/persistentvolumeclaims", [])


def _services(objects: dict[str, list[Any]]) -> list[Any]:
    return objects.get("/services", [])


def plan(name: str) -> dict[str, Any]:
    """``GET /api/projects/{name}/delete-plan`` (§26).

    Ungated and unaudited: reads only. It does not dry-run the delete — a dry run
    is a write request the caller has not made yet, and it needs the preflight the
    funnel does.

    The namespace read is primary and raises: there is no useful plan for a
    namespace this console could not read, and a 404 is the right answer to
    "what would deleting this take with it" when there is nothing to delete.
    Everything after it is collected, so one refused listing costs its own row
    and names itself rather than emptying the page.

    A namespace already in `Terminating` comes back `blocked` — and the plan is
    still worth reading, because the finalizer list below is the answer to why it
    has not finished.
    """
    unavailable: list[dict[str, Any]] = []
    namespace = reader.get_resource(GROUP, VERSION, PLURAL, name)
    phase = get_field(namespace, "status", "phase")

    found = inventory(name, unavailable)
    objects = found.pop("objects")

    own_finalizers = [
        str(value)
        for value in (get_field(namespace, "spec", "finalizers", default=[]) or [])
        if value != KUBERNETES_FINALIZER
    ]

    report = {
        "inventory": found,
        "volumes": volume_fates(_claims(objects), unavailable),
        "load_balancers": load_balancers(_services(objects)),
        "webhooks": webhook_backends(name, unavailable),
        "finalizers": finalizer_holders(objects),
    }

    terminating = phase == "Terminating"
    return {
        "name": name,
        "phase": phase,
        "resourceVersion": (
            str(get_field(namespace, "metadata", "resourceVersion"))
            if get_field(namespace, "metadata", "resourceVersion") else None
        ),
        "deletionTimestamp": get_field(namespace, "metadata", "deletionTimestamp"),
        # Finalizers on the namespace object itself, minus the one the API
        # server always puts there. Anything left belongs to a controller.
        "namespaceFinalizers": own_finalizers,
        **report,
        "propagationPolicy": PROPAGATION_POLICY,
        "blocked": (
            {
                "message": f"{name} is already being deleted.",
                "hint": (
                    "It has a deletionTimestamp and the namespace controller has "
                    "started. If it has not finished, the finalizers below are "
                    "why — re-deleting it does nothing."
                ),
                "context": {"phase": phase},
            }
            if terminating else None
        ),
        "consequences": [] if terminating else consequences_for(report),
        "unavailable": unavailable,
        "partial": bool(unavailable),
        "gate": enabled_state(),
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def delete_namespace(
    name: str,
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``DELETE /api/projects/{name}`` (§26) — through the single funnel.

    Order of refusals, each before the cluster is changed: the plan is recomputed
    against the namespace as it is *now*, an already-terminating namespace is
    refused, and the acknowledgement check runs over those recomputed
    consequences. Then :func:`app.admin.apply.delete_resource`, which is §4's
    delete unchanged — the same `apply_fn`, the same :func:`mutate` call, the same
    preflight of `delete core/namespaces`, the same `before=live, after=null`
    diff and the same audit row.

    A second route to a write §4 already offers, for the reason §20, §21, §24 and
    §25 are: the endpoint exists to attach a preview and a handshake to an action
    whose consequences are not visible in its own diff. It does not add a second
    path to the cluster.

    **`applied: true` means the namespace has a deletionTimestamp**, and the
    namespace controller has started. It does not mean the namespace is gone — it
    can sit in `Terminating` for as long as an unsatisfied finalizer holds it,
    which is what the finalizer consequence is about.
    """
    report = plan(name)
    if report["blocked"]:
        raise Invalid(
            report["blocked"]["message"],
            hint=report["blocked"]["hint"],
            context={"parameter": "name", **report["blocked"]["context"]},
        )

    consequences = report["consequences"]
    _require_acknowledgement(consequences, acknowledge_consequences)

    result = apply.delete_resource(
        GROUP, VERSION, PLURAL, None, name, PROPAGATION_POLICY, dry_run,
        detail=_detail(name, report),
    )
    result["consequences"] = consequences
    result["plan"] = report
    return result


def _detail(name: str, report: dict[str, Any]) -> str:
    """The audit sentence. The counts are the point: "delete namespace prod" is
    not what happened if it took four volumes with it."""
    destroyed = sum(1 for row in report["volumes"] if row["reclaim_policy"] == RECLAIM_DELETE)
    parts = [f"delete namespace {name}"]
    if destroyed:
        parts.append(f"{destroyed} volume(s) destroyed")
    if report["load_balancers"]:
        parts.append(f"{len(report['load_balancers'])} load balancer(s) released")
    if report["webhooks"]:
        parts.append(f"{len(report['webhooks'])} admission webhook(s) left without a backend")
    if report["finalizers"]:
        parts.append(f"{len(report['finalizers'])} object(s) hold finalizers")
    return "; ".join(parts)


__all__ = [
    "PAGE",
    "PROPAGATION_POLICY",
    "consequences_for",
    "delete_namespace",
    "enabled_state",
    "finalizer_holders",
    "inventory",
    "load_balancers",
    "namespaced_kinds",
    "plan",
    "volume_fates",
    "webhook_backends",
]
