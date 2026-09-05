"""
Cordon and drain (§5) — the two node writes, and the most dangerous button here.

Cordon is a one-field patch. Drain is the only multi-step action in the console:
it decides what happens to every pod on a machine, and it is the action whose
failure mode is somebody's production traffic. So it is written to be explicit
about three things, in this order:

**A plan before an action.** Every pod on the node is classified first —
``evict``, ``skip`` or ``blocked`` — and the classification is returned whether
or not anything is executed. An operator confirming a drain is confirming a
list, not a verb.

**A refusal that explains itself.** ``blocked`` pods with ``force: false`` stop
the drain before the node is even cordoned, with the plan attached to the error
(§1.3 ``context``). The operator sees exactly which pods to resolve and why,
rather than a drain that half-runs and leaves them to work out where it stopped.

**Per-pod results, and no aggregate "success".** Evictions are reported
individually. A drain over three pods the API server refused returns
``applied: true`` with ``failed: 3`` and ``drained: false`` — never a bare
success. "Drained" over three stuck pods is precisely the confidently wrong
answer this project exists to prevent: it is the sentence that gets a machine
terminated with a database on it.

**What ``force`` is, and is not.** ``force`` means "I have read this plan and I
accept it" — it lets execution proceed past the blocked entries. It does not
give the console power it does not have: a PodDisruptionBudget is enforced by the
API server on the eviction subresource, and a pod it refuses is refused with
``force`` too, reported per-pod as a failure. Saying otherwise would sell the
operator a bigger hammer than exists.

Both writes go through :func:`app.admin.mutate.mutate`, the single funnel, so
they are gated, preflighted, diffed and audited like every other write. Drain
additionally preflights ``create`` on ``core/pods/eviction`` from inside the
apply step, because permission to cordon a node is not permission to evict what
is running on it, and finding that out three pods into a drain is not a discovery
anyone wants to make.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterable, Iterator

from kubernetes.client.rest import ApiException

from app.admin import preflight
from app.admin.mutate import mutate
from app.errors import AdminError, Invalid, from_api_exception
from app.k8s.client import get_api_client, get_core_v1
from app.resources import catalog, reader, shaping
from app.resources.envelope import collect
from app.services.workloads import selector_matches

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Plan vocabulary
# --------------------------------------------------------------------------- #

#: The pod will be evicted when the drain executes.
ACTION_EVICT = "evict"
#: The pod is left alone, and leaving it alone is correct — nothing to resolve.
ACTION_SKIP = "skip"
#: The drain will not proceed past this pod without ``force``.
ACTION_BLOCKED = "blocked"

#: The kubelet writes this annotation on a static pod's API mirror. A mirror pod
#: is a read-only shadow of a file on the node's disk: deleting it changes
#: nothing, the kubelet recreates it immediately, and evicting it is refused.
#: Skipping it is not a compromise, it is the only correct handling.
MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"

#: Eviction is refused with 429 when a PodDisruptionBudget would be violated.
#: That status normally means "you are being rate limited", and mapping it that
#: way here would tell an operator to retry a drain that will never succeed
#: until they scale something up. It gets its own reason.
_TOO_MANY_REQUESTS = 429


# --------------------------------------------------------------------------- #
# Cluster access — dicts in, dicts out
# --------------------------------------------------------------------------- #
#
# The node is read and patched as a plain JSON dict rather than through the
# typed client, because the §1.5 diff is built from ``before``/``after`` dicts
# and rendered as YAML. Converting a ``V1Node`` back into that shape is a second
# representation of the same object and a second chance for the two to disagree
# about a field name in a diff an operator is about to approve.

@contextlib.contextmanager
def _api_errors(**context: Any) -> Iterator[None]:
    """Turn an ``ApiException`` from the block into a typed AdminError with a target."""
    try:
        yield
    except ApiException as e:
        raise from_api_exception(
            e, context={k: v for k, v in context.items() if v is not None}
        ) from e


def _node_path(name: str) -> str:
    return reader.resource_path("", "v1", "nodes", name=name)


def _warnings_from(headers: Any) -> list[str]:
    """The API server's ``Warning:`` headers, as §1.5 wants them.

    Deprecation and admission warnings are the one channel through which a
    cluster tells an operator that a write succeeded *and* something about it is
    wrong. Dropping them makes the console quieter than kubectl about exactly
    the thing kubectl is trying to say.

    A warning header is ``299 - "the message"``. The quoted text is what a human
    wants; the agent and the code are noise in a toast.
    """
    if headers is None:
        return []
    values: list[str] = []
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = list(getlist("Warning"))
    else:
        with contextlib.suppress(AttributeError, TypeError):
            raw = headers.get("Warning") if hasattr(headers, "get") else None
            if raw:
                values = [raw]

    parsed: list[str] = []
    for value in values:
        text = str(value)
        first, _, rest = text.partition('"')
        if rest:
            parsed.append(rest.rpartition('"')[0] or text)
        else:
            parsed.append(first.strip())
    return [w for w in parsed if w]


def read_node(name: str) -> dict[str, Any]:
    """The live node as a trimmed dict, for the ``before`` side of the diff.

    Trimmed of ``managedFields``, which on a node reconciled by a cloud
    controller and a kubelet is routinely larger than the rest of the object. A
    diff that buries a one-line ``unschedulable: true`` under four hundred lines
    of server-side-apply bookkeeping is a diff nobody reads, and §11.3 rests on
    the operator reading it.
    """
    with _api_errors(verb="get", group="", resource="nodes", name=name):
        return reader.trim(catalog.raw_get(_node_path(name)), for_list=False)


def _patch_node(name: str, patch: dict[str, Any], *, dry_run: bool) -> tuple[dict[str, Any], list[str]]:
    """Strategic-merge patch a node. Returns ``(object, warnings)``.

    ``dryRun=All`` makes the API server run admission and return the object it
    *would* have stored without storing it, which is what makes the §1.5 diff a
    projection from the cluster rather than a guess assembled here.
    """
    query = [("dryRun", "All")] if dry_run else []
    with _api_errors(verb="patch", group="", resource="nodes", name=name):
        data, _status, headers = get_api_client().call_api(
            _node_path(name),
            "PATCH",
            query_params=query,
            header_params={
                "Accept": "application/json",
                "Content-Type": "application/strategic-merge-patch+json",
            },
            body=patch,
            response_type="object",
            auth_settings=["BearerToken"],
            _return_http_data_only=False,
        )
    return reader.trim(data, for_list=False), _warnings_from(headers)


def _post_eviction(namespace: str, name: str, grace_period_seconds: int | None) -> None:
    """Ask the API server to evict one pod, honouring PodDisruptionBudgets.

    The Eviction subresource, not ``DELETE pod``. The difference is the entire
    safety story of a drain: a delete removes the pod whatever a
    PodDisruptionBudget says, and an eviction is refused when it would take a
    service below its budget. A console that "drained" by deleting would be
    quietly bypassing the one guardrail the application team configured.
    """
    body: dict[str, Any] = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": name, "namespace": namespace},
    }
    if grace_period_seconds is not None:
        body["deleteOptions"] = {"gracePeriodSeconds": grace_period_seconds}

    path = reader.resource_path(
        "", "v1", "pods", namespace=namespace, name=name, subresource="eviction"
    )
    get_api_client().call_api(
        path,
        "POST",
        header_params={"Accept": "application/json", "Content-Type": "application/json"},
        body=body,
        response_type="object",
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
    )


def list_node_pods(node_name: str) -> list[Any]:
    """Every pod the API server says is on this node."""
    with _api_errors(verb="list", group="", resource="pods", name=node_name):
        listing = get_core_v1().list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        )
    return list(shaping.get_field(listing, "items", default=[]) or [])


def _pdb_index(unavailable: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]] | None:
    """PodDisruptionBudgets by namespace, or ``None`` if they could not be read.

    ``None`` is not "there are none". The two are kept apart all the way into the
    response's ``pdb_checked`` flag, because a plan that says ``evict`` for every
    pod looks identical whether we checked the budgets or never managed to.
    """
    budgets: dict[str, list[dict[str, Any]]] | None = None
    with collect(unavailable, "policy", "poddisruptionbudgets"):
        payload = catalog.raw_get(
            reader.resource_path("policy", "v1", "poddisruptionbudgets")
        )
        budgets = {}
        for item in payload.get("items") or []:
            namespace = str(shaping.get_field(item, "metadata", "namespace") or "")
            budgets.setdefault(namespace, []).append(item)
    return budgets


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def controller_ref(pod: Any) -> Any | None:
    """The ownerReference that will recreate this pod, or None if nothing will."""
    for ref in shaping.get_field(pod, "metadata", "ownerReferences", default=[]) or []:
        if shaping.get_field(ref, "controller") is True:
            return ref
    return None


def _has_emptydir(pod: Any) -> bool:
    """Does the pod carry an ``emptyDir`` volume?

    An emptyDir lives on the node's disk and does not survive the pod moving.
    Whatever is in it — a cache, a scratch index, a half-written export — is
    gone. That is why deleting it needs a separate opt-in from ``force``:
    accepting an unmanaged pod's deletion and accepting data loss are different
    decisions, and kubectl keeps them separate for the same reason.
    """
    for volume in shaping.get_field(pod, "spec", "volumes", default=[]) or []:
        if shaping.get_field(volume, "emptyDir") is not None:
            return True
    return False


def _blocking_pdb(pod: Any, budgets: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """``(matching PDB name, blocking reason)`` for one pod.

    A budget blocks only when its ``status.disruptionsAllowed`` is a number we
    read and that number is zero or less. A budget whose status the controller
    has not computed yet is reported as *matching* but not as blocking: the
    eviction API is the actual enforcer, and refusing a whole drain on the
    strength of a field that is momentarily absent would train operators to reach
    straight for ``force``, which is worse than the risk it avoids.
    """
    labels = shaping.get_field(pod, "metadata", "labels", default={}) or {}
    matched: str | None = None
    for budget in budgets:
        selector = shaping.get_field(budget, "spec", "selector")
        if selector is None:
            continue
        # An empty selector on a PDB selects every pod in the namespace. That is
        # what the API means, so it is what is applied.
        if not selector_matches(selector, labels):
            continue
        name = str(shaping.get_field(budget, "metadata", "name") or "")
        matched = matched or name
        allowed = shaping.get_field(budget, "status", "disruptionsAllowed")
        if isinstance(allowed, bool) or not isinstance(allowed, int):
            continue
        if allowed <= 0:
            return name, (
                f"PodDisruptionBudget {name!r} allows 0 more disruptions; "
                "evicting this pod would violate it"
            )
    return matched, None


def classify_pod(
    pod: Any,
    *,
    ignore_daemonsets: bool,
    delete_emptydir_data: bool,
    budgets: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """One plan entry: what the drain will do with this pod, and why.

    The order below is the order of certainty, not of severity. A mirror pod
    cannot be evicted by anyone, so nothing about budgets or storage changes the
    answer; a pod that is already terminating is already leaving. Only once those
    are out of the way does the classification start describing choices the
    operator can make.

    **Every blocking reason is reported, not just the first.** A pod that is both
    unmanaged and holds an emptyDir needs two decisions from the operator, and a
    plan that surfaced one of them would send them round the loop twice.
    """
    namespace = shaping.get_field(pod, "metadata", "namespace")
    name = shaping.get_field(pod, "metadata", "name")
    controller = controller_ref(pod)
    entry: dict[str, Any] = {
        "namespace": namespace,
        "pod": name,
        "action": ACTION_EVICT,
        "reason": None,
        "controller": (
            {
                "kind": shaping.get_field(controller, "kind"),
                "name": shaping.get_field(controller, "name"),
            }
            if controller is not None
            else None
        ),
        "pdb": None,
        # Filled in by execution. Present and null on a dry run so the frontend
        # can read `entry.result` unconditionally.
        "result": None,
    }

    annotations = shaping.get_field(pod, "metadata", "annotations", default={}) or {}
    if isinstance(annotations, dict) and MIRROR_POD_ANNOTATION in annotations:
        entry["action"] = ACTION_SKIP
        entry["reason"] = (
            "mirror pod: the kubelet owns it from a file on the node, so the API "
            "server cannot evict it"
        )
        return entry

    if shaping.get_field(pod, "metadata", "deletionTimestamp"):
        entry["action"] = ACTION_SKIP
        entry["reason"] = "already terminating"
        return entry

    phase = shaping.get_field(pod, "status", "phase")
    if phase in ("Succeeded", "Failed"):
        entry["action"] = ACTION_SKIP
        entry["reason"] = f"finished ({phase}): holds no node resources"
        return entry

    controller_kind = shaping.get_field(controller, "kind") if controller is not None else None
    if controller_kind == "DaemonSet":
        if ignore_daemonsets:
            entry["action"] = ACTION_SKIP
            entry["reason"] = (
                "DaemonSet-managed: its controller would recreate it on this node "
                "immediately, so draining it achieves nothing"
            )
        else:
            entry["action"] = ACTION_BLOCKED
            entry["reason"] = (
                "DaemonSet-managed and ignoreDaemonSets=false. Set ignoreDaemonSets "
                "to skip it; it cannot be drained off a node it is scheduled to."
            )
        return entry

    blockers: list[str] = []
    if _has_emptydir(pod) and not delete_emptydir_data:
        blockers.append(
            "has an emptyDir volume and deleteEmptyDirData=false: its contents are "
            "on this node's disk and will not survive the move"
        )
    if controller is None:
        blockers.append(
            "unmanaged pod: no controller owns it, so nothing will recreate it "
            "anywhere else"
        )
    if budgets is not None:
        pdb_name, pdb_reason = _blocking_pdb(pod, budgets)
        entry["pdb"] = pdb_name
        if pdb_reason:
            blockers.append(pdb_reason)

    if blockers:
        entry["action"] = ACTION_BLOCKED
        entry["reason"] = "; ".join(blockers)
    return entry


def build_plan(
    pods: Iterable[Any],
    *,
    ignore_daemonsets: bool,
    delete_emptydir_data: bool,
    budgets: dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Classify every pod on the node, in a stable namespace/name order.

    Sorted because this list is read by a human under time pressure and compared
    against the same list from thirty seconds ago. The API server's pod order is
    not stable, and a confirm dialog whose rows move between the dry run and the
    apply is a confirm dialog that gets clicked without being read.
    """
    plan = [
        classify_pod(
            pod,
            ignore_daemonsets=ignore_daemonsets,
            delete_emptydir_data=delete_emptydir_data,
            budgets=(
                None
                if budgets is None
                else budgets.get(str(shaping.get_field(pod, "metadata", "namespace") or ""), [])
            ),
        )
        for pod in pods
    ]
    plan.sort(key=lambda entry: (entry["namespace"] or "", entry["pod"] or ""))
    return plan


def _tally(plan: list[dict[str, Any]]) -> dict[str, int]:
    counts = {ACTION_EVICT: 0, ACTION_SKIP: 0, ACTION_BLOCKED: 0}
    for entry in plan:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def cordon_node(name: str, unschedulable: bool, dry_run: bool) -> dict[str, Any]:
    """``POST /api/nodes/{name}/cordon`` (§5). Returns the §1.5 mutation response.

    Cordoning does not move anything: it marks the node so the scheduler places
    nothing *new* there. Everything already running keeps running, which is why
    this is a separate button from drain rather than the first half of it — an
    operator investigating a bad node wants to stop the bleeding without
    disturbing what is up.
    """
    before = read_node(name)
    was = bool(shaping.get_field(before, "spec", "unschedulable", default=False))
    target = bool(unschedulable)

    def apply_fn(dry: bool) -> tuple[dict[str, Any] | None, list[str]]:
        return _patch_node(name, {"spec": {"unschedulable": target}}, dry_run=dry)

    return mutate(
        verb="patch",
        group="",
        version="v1",
        plural="nodes",
        namespace=None,
        name=name,
        dry_run=dry_run,
        apply_fn=apply_fn,
        before=before,
        detail=f"{'cordon' if target else 'uncordon'}: unschedulable {was} -> {target}",
    )


def drain_node(
    name: str,
    *,
    dry_run: bool,
    grace_period_seconds: int | None,
    ignore_daemonsets: bool,
    delete_emptydir_data: bool,
    force: bool,
) -> dict[str, Any]:
    """``POST /api/nodes/{name}/drain`` (§5). The §1.5 response plus the plan.

    Sequence inside the single write funnel, which supplies the mutations gate,
    the ``patch core/nodes`` preflight, the diff and the audit record around it:

    1. Preflight ``create core/pods/eviction``. Cordon permission is not eviction
       permission, and the difference has to surface before anything moves —
       including on a dry run, where "what would happen" has to include "we would
       be refused".
    2. List the pods on the node and the PodDisruptionBudgets, and build the plan.
    3. If anything is ``blocked`` and ``force`` is false, **stop here** — before
       the cordon, so the node is left exactly as it was — and raise ``422
       invalid`` carrying the plan.
    4. Cordon, so the scheduler does not replace what we are about to remove.
    5. Evict, one pod at a time, recording each result.

    Returns ``drained``: true only when the drain executed and every eviction
    succeeded. It is a separate field from ``applied`` because ``applied`` means
    "we wrote to the cluster" and that is true of a drain that failed on half its
    pods. The UI headline hangs off ``drained``.
    """
    before = read_node(name)
    # apply_fn owns the plan and the results; this is how they get back out to
    # the response, since mutate's contract is about the object being changed.
    outcome: dict[str, Any] = {}
    #: Whether the eviction loop ran to completion, which `outcome` cannot say
    #: on its own. Kept out of `outcome` because that dict is merged into the
    #: §1.5 response and this is bookkeeping, not something the API promises.
    progress: dict[str, bool] = {"evictions_ran": False}

    def apply_fn(dry: bool) -> tuple[dict[str, Any] | None, list[str]]:
        preflight.require("create", "", "pods", subresource="eviction")

        unavailable: list[dict[str, Any]] = []
        budgets = _pdb_index(unavailable)
        pods = list_node_pods(name)
        plan = build_plan(
            pods,
            ignore_daemonsets=ignore_daemonsets,
            delete_emptydir_data=delete_emptydir_data,
            budgets=budgets,
        )
        counts = _tally(plan)
        outcome.update({
            "plan": plan,
            "blocked": counts[ACTION_BLOCKED],
            "skipped": counts[ACTION_SKIP],
            "evicted": 0,
            "failed": 0,
            "drained": False,
            "pdb_checked": budgets is not None,
            "unavailable": unavailable,
            "partial": bool(unavailable),
        })

        if counts[ACTION_BLOCKED] and not force:
            # Raised before the cordon: a refused drain must leave the node
            # exactly as it found it, or the operator resolves the blockers on a
            # node that has silently stopped accepting work.
            raise Invalid(
                f"{counts[ACTION_BLOCKED]} pod(s) on {name} cannot be drained "
                "without force.",
                detail="; ".join(
                    f"{entry['namespace']}/{entry['pod']}: {entry['reason']}"
                    for entry in plan
                    if entry["action"] == ACTION_BLOCKED
                ),
                hint=(
                    "Resolve the listed pods, or set deleteEmptyDirData / "
                    "ignoreDaemonSets as appropriate. `force: true` proceeds "
                    "anyway, but it cannot override a PodDisruptionBudget — the "
                    "API server enforces those."
                ),
                context={
                    "group": "", "version": "v1", "resource": "nodes", "name": name,
                    "verb": "drain",
                    "blocked": counts[ACTION_BLOCKED],
                    "plan": plan,
                    "pdb_checked": budgets is not None,
                },
            )

        node_after, warnings = _patch_node(
            name, {"spec": {"unschedulable": True}}, dry_run=dry
        )

        if dry:
            # Nothing is evicted on a dry run, so every `result` stays null and
            # `drained` stays false. Reporting a projected drain as drained is
            # the §1.5 rule that `applied: false` exists to enforce.
            return node_after, warnings

        evicted = failed = 0
        for entry in plan:
            if entry["action"] == ACTION_SKIP:
                continue
            try:
                _post_eviction(entry["namespace"], entry["pod"], grace_period_seconds)
            except ApiException as e:
                failed += 1
                entry["result"] = "failed"
                entry["error"] = _eviction_message(e)
                entry["error_code"] = _eviction_code(e)
                logger.warning(
                    "Eviction of %s/%s during drain of %s failed: %s",
                    entry["namespace"], entry["pod"], name, entry["error"],
                )
            except AdminError as e:
                # A transport failure (the API server went away mid-drain). The
                # remaining pods are still attempted: stopping here would leave
                # the operator with a partially drained node and no list of what
                # was left.
                failed += 1
                entry["result"] = "failed"
                entry["error"] = e.message
                entry["error_code"] = e.code
                logger.warning(
                    "Eviction of %s/%s during drain of %s failed: %s",
                    entry["namespace"], entry["pod"], name, e.message,
                )
            else:
                evicted += 1
                entry["result"] = "evicted"

        outcome["evicted"] = evicted
        outcome["failed"] = failed
        outcome["drained"] = failed == 0
        # Only here, after the loop. `outcome["evicted"]` is initialised to 0
        # with the plan, so it cannot distinguish "no eviction succeeded" from
        # "no eviction was attempted" — and an audit sentence reading
        # "evicted 0" over a drain refused before it started is a worse lie than
        # the fixed sentence it replaced.
        progress["evictions_ran"] = True
        return node_after, warnings

    def _drain_detail() -> str:
        """The audit sentence, written when the row is — not before the evictions.

        A drain is one funnel call that performs many writes: the cordon patch
        the funnel preflights and diffs, and then one eviction per pod inside
        ``apply_fn``. The row's outcome is therefore ``applied`` as soon as the
        cordon lands, whatever the evictions did — and a trail saying "drain
        node-5, applied" over a node that drained nothing is exactly the claim
        §1.5's ``drained: false`` exists to stop the *response* making. The
        response was honest and the record that outlives it was not.

        So the counts go in the sentence, resolved at audit time. `evicted`
        being absent means the evictions never ran — a refusal at the gate, at
        the preflight, or the blocked-pods `Invalid` raised before the cordon —
        and the sentence says only what was attempted, because nothing else
        happened.
        """
        line = (
            f"drain {name}"
            + (" (dry run)" if dry_run else "")
            + (", force" if force else "")
        )
        if dry_run or not progress["evictions_ran"]:
            return line
        line += f": evicted {outcome['evicted']}"
        if outcome["failed"]:
            # Named rather than counted into the success: "drained" over pods
            # the API server refused is the sentence that gets a machine
            # terminated with a database on it.
            line += f", {outcome['failed']} refused, node NOT drained"
        if outcome.get("skipped"):
            line += f", {outcome['skipped']} skipped"
        return line

    result = mutate(
        verb="patch",
        group="",
        version="v1",
        plural="nodes",
        namespace=None,
        name=name,
        dry_run=dry_run,
        apply_fn=apply_fn,
        before=before,
        # `verb` is `patch` because that is the RBAC verb preflighted against
        # core/nodes — the funnel issues a real SelfSubjectAccessReview and there
        # is no `drain` verb for an authorizer to match. The audit row's detail
        # says what it actually was.
        detail=_drain_detail,
    )
    result.update(outcome)
    return result


def _eviction_message(exc: ApiException) -> str:
    """A human sentence for one failed eviction.

    The 429 case is called out because its normal meaning is throttling. On the
    eviction subresource it means a PodDisruptionBudget refused the disruption,
    and "the API server is rate limiting you" would send an operator to wait for
    a condition that will not change on its own.
    """
    mapped = from_api_exception(exc, context={"verb": "create", "group": "", "resource": "pods",
                                              "subresource": "eviction"})
    if getattr(exc, "status", None) == _TOO_MANY_REQUESTS:
        return (
            "refused by a PodDisruptionBudget: evicting it now would take the "
            "application below its configured minimum availability"
            + (f" ({mapped.detail})" if mapped.detail else "")
        )
    return mapped.detail or mapped.message


def _eviction_code(exc: ApiException) -> str:
    """The stable error code for one failed eviction, for the UI to branch on."""
    if getattr(exc, "status", None) == _TOO_MANY_REQUESTS:
        return "disruption_budget"
    return from_api_exception(
        exc, context={"verb": "create", "group": "", "resource": "pods",
                      "subresource": "eviction"},
    ).code


__all__ = [
    "ACTION_BLOCKED",
    "ACTION_EVICT",
    "ACTION_SKIP",
    "MIRROR_POD_ANNOTATION",
    "build_plan",
    "classify_pod",
    "controller_ref",
    "cordon_node",
    "drain_node",
    "list_node_pods",
    "read_node",
]
