"""
Node rows (§5) — capacity, allocatable, and what is actually requested on each box.

This module answers the question an operator asks immediately before doing
something irreversible: *how full is this node, and what is on it?* Three pieces
of it are load-bearing enough to be worth stating before the code.

**Quantity parsing is the whole ballgame.** Every number on this page arrives as
a Kubernetes *quantity* string — ``16``, ``15800m``, ``64Gi``, ``68719476736``,
``2Ti`` — and a parser that guesses is a dashboard that lies quietly. ``Mi`` and
``M`` differ by 4.8%; on a 256 GiB node that is 12 GB of phantom headroom, which
is enough to schedule onto a box that cannot take it. :func:`parse_quantity`
implements the real grammar, returns :class:`~decimal.Decimal` so that summing a
hundred pods' ``350m`` requests lands on an exact number rather than on
``9.899999999999999``, and returns ``None`` — never a magnitude it invented —
for anything it does not recognise.

**``requested`` and ``pod_count`` are ``None`` when the pod listing failed.**
Not ``0``. §5 says so explicitly, and the reason is operational rather than
stylistic: a node showing ``0`` requested cores and ``0`` pods reads as an idle
box, and an idle box is the one an operator picks to drain, to cordon, or to
terminate. The listing failing and the node being empty must never render the
same.

**Only non-terminated pods count.** A node that has run ten thousand completed
Job pods has not used ten thousand pod slots; the scheduler counts pods that are
not ``Succeeded`` or ``Failed`` against ``capacity.pods``, and so does this. The
requested totals follow the same rule, because a finished pod holds no CPU.

A second quantity parser lives in :mod:`app.resources.shaping` and this is not an
accidental duplicate. That one answers "how big is this PVC" and returns a float,
which is right for a single value that gets rendered once. This one accumulates:
it is summed across every container of every pod on a node, and float error
accumulates with it, so it carries exact decimals and reports the byte counts the
way apimachinery's own ``Quantity.Value()`` does — rounded up — so the number
here equals the number the scheduler used.
"""

from __future__ import annotations

import contextlib
import decimal
import logging
import re
from decimal import Decimal
from typing import Any, Iterable, Iterator

from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import get_core_v1
from app.resources import shaping
from app.resources.envelope import collect, envelope

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Kubernetes quantities
# --------------------------------------------------------------------------- #

# The grammar from k8s.io/apimachinery/pkg/api/resource:
#
#   <quantity>        ::= <signedNumber><suffix>
#   <suffix>          ::= <binarySI> | <decimalExponent> | <decimalSI>
#   <binarySI>        ::= Ki | Mi | Gi | Ti | Pi | Ei
#   <decimalSI>       ::= n | u | m | "" | k | M | G | T | P | E
#   <decimalExponent> ::= ("e" | "E") <signedNumber>
#
# The alternation order matters. ``Ei`` has to be tried before the bare ``E``
# decimal suffix, and ``e3`` before it too — otherwise ``1e3`` parses as the
# number 1 with a leftover ``e3`` and is rejected, turning a legal capacity into
# an em dash.
_QUANTITY_RE = re.compile(
    r"""
    ^
    (?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+))
    (?:
        (?P<binary>Ki|Mi|Gi|Ti|Pi|Ei)
      | (?P<exponent>[eE][+-]?\d+)
      | (?P<decimal>[numkKMGTPE])
    )?
    $
    """,
    re.VERBOSE,
)

# Exact integers, not powers computed at parse time: 1024**6 is 1.15e18 and a
# Decimal power under the default 28-digit context is *not* guaranteed exact.
_BINARY_FACTOR: dict[str, Decimal] = {
    "Ki": Decimal(1024),
    "Mi": Decimal(1024**2),
    "Gi": Decimal(1024**3),
    "Ti": Decimal(1024**4),
    "Pi": Decimal(1024**5),
    "Ei": Decimal(1024**6),
}

# ``K`` is not canonical — SI reserves it for kelvin, apimachinery accepts only
# lowercase ``k``, and the API server rejects ``500K`` on write, so it cannot
# reach us from a cluster. It is accepted here because the one place it *can*
# arrive from is a hand-authored file an operator pasted in, where ``500K`` has
# exactly one possible reading. ``Ki`` stays strictly binary: the tolerance is
# for the case with no ambiguity, not for the case where guessing costs 2.4%.
_DECIMAL_FACTOR: dict[str, Decimal] = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}

# Wide enough that no realistic quantity is rounded. 8Ei is 19 digits, a cluster
# total of them is 22, and nanocores add nine more; 60 leaves headroom without
# being so large that a pathological input is expensive to multiply.
_CONTEXT = decimal.Context(prec=60)


def parse_quantity(value: Any) -> Decimal | None:
    """Parse a Kubernetes resource quantity exactly, or return ``None``.

    ``None`` — never a fallback magnitude — for anything outside the grammar. A
    capacity the console cannot parse is a capacity it does not know, and the row
    renders an em dash, which is honest. A zero would describe a 2 TiB node as
    empty and a ``500m`` request as free.

    Numbers arrive already parsed sometimes (the dynamic client turns a bare
    ``110`` into an ``int``), so ints and floats pass through. ``bool`` is
    rejected deliberately: it is an ``int`` subclass in Python, and a ``True``
    that silently became one core is a bug that would survive review.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str() first: Decimal(0.1) is 0.1000000000000000055511151231257827,
        # which then prints back as that in any diff or log line.
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None
    match = _QUANTITY_RE.match(text)
    if match is None:
        logger.debug("Unparseable Kubernetes quantity %r", value)
        return None

    number = Decimal(match.group("number"))
    binary = match.group("binary")
    exponent = match.group("exponent")
    decimal_suffix = match.group("decimal")

    if binary:
        return _CONTEXT.multiply(number, _BINARY_FACTOR[binary])
    if exponent:
        return _CONTEXT.multiply(number, Decimal(10) ** int(exponent[1:]))
    if decimal_suffix:
        return _CONTEXT.multiply(number, _DECIMAL_FACTOR[decimal_suffix])
    return number


def cpu_cores(value: Any) -> float | None:
    """A quantity read as CPU cores. ``15800m`` is 15.8, not 15800."""
    parsed = parse_quantity(value)
    return None if parsed is None else float(parsed)


def memory_bytes(value: Any) -> int | None:
    """A quantity read as a byte count, rounded **up**.

    Rounding up rather than truncating is apimachinery's own behaviour
    (``Quantity.Value()``), so the number here is the number the scheduler used.
    It matters for exactly one class of value — a fractional quantity such as
    ``1.5Ki`` or a ``100m`` byte request — and matching upstream costs nothing
    where diverging costs an unexplainable off-by-one between this console and
    ``kubectl describe node``.
    """
    parsed = parse_quantity(value)
    if parsed is None:
        return None
    return int(parsed.to_integral_value(rounding=decimal.ROUND_CEILING))


def pod_capacity(value: Any) -> int | None:
    """A quantity read as a pod slot count."""
    parsed = parse_quantity(value)
    return None if parsed is None else int(parsed.to_integral_value(rounding=decimal.ROUND_FLOOR))


# --------------------------------------------------------------------------- #
# Resource accounting
# --------------------------------------------------------------------------- #

#: Phases in which a pod holds no node resources and occupies no pod slot. The
#: scheduler excludes these from both tallies, and ``kubectl describe node``
#: filters on exactly this pair.
TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})


class _Totals:
    """A running (cpu, memory) sum that remembers when it stopped being trustworthy.

    The ``unknown`` flags are the point. If one container's request is a quantity
    this parser does not recognise, the sum that excludes it is *smaller* than
    the truth, and a smaller number here means more apparent headroom — which
    sends an operator to schedule onto a node that cannot take it. So an
    unparseable value poisons its dimension: the total becomes ``None`` and the
    row shows an em dash rather than a confident undercount.
    """

    __slots__ = ("cpu", "memory", "cpu_unknown", "memory_unknown")

    def __init__(self) -> None:
        self.cpu = Decimal(0)
        self.memory = Decimal(0)
        self.cpu_unknown = False
        self.memory_unknown = False

    def add(self, other: "_Totals") -> None:
        self.cpu = _CONTEXT.add(self.cpu, other.cpu)
        self.memory = _CONTEXT.add(self.memory, other.memory)
        self.cpu_unknown = self.cpu_unknown or other.cpu_unknown
        self.memory_unknown = self.memory_unknown or other.memory_unknown

    def max_with(self, other: "_Totals") -> None:
        self.cpu = max(self.cpu, other.cpu)
        self.memory = max(self.memory, other.memory)
        self.cpu_unknown = self.cpu_unknown or other.cpu_unknown
        self.memory_unknown = self.memory_unknown or other.memory_unknown

    def copy(self) -> "_Totals":
        clone = _Totals()
        clone.cpu, clone.memory = self.cpu, self.memory
        clone.cpu_unknown, clone.memory_unknown = self.cpu_unknown, self.memory_unknown
        return clone

    def as_row(self) -> dict[str, Any]:
        return {
            "cpu_cores": None if self.cpu_unknown else float(self.cpu),
            "memory_bytes": (
                None
                if self.memory_unknown
                else int(self.memory.to_integral_value(rounding=decimal.ROUND_CEILING))
            ),
        }


def _requests_of(container: Any) -> _Totals:
    """One container's ``resources.requests`` as a ``_Totals``.

    An absent ``requests`` block is a genuine zero — a BestEffort container
    requests nothing — while a *present* value that will not parse is unknown.
    Those two have to stay apart: the first is a fact about the pod and the
    second is a fact about our reading of it.
    """
    totals = _Totals()
    requests = shaping.get_field(container, "resources", "requests", default={}) or {}
    if not isinstance(requests, dict):
        return totals

    raw_cpu = requests.get("cpu")
    if raw_cpu is not None:
        parsed = parse_quantity(raw_cpu)
        if parsed is None:
            totals.cpu_unknown = True
        else:
            totals.cpu = parsed

    raw_memory = requests.get("memory")
    if raw_memory is not None:
        parsed = parse_quantity(raw_memory)
        if parsed is None:
            totals.memory_unknown = True
        else:
            totals.memory = parsed
    return totals


def pod_requests(pod: Any) -> _Totals:
    """The resources one pod actually reserves on its node.

    This is the upstream ``resourcehelper.PodRequests`` algorithm, not a sum of
    ``spec.containers``, and the difference is not academic:

    * **Init containers run before the app containers**, so a migration job that
      requests 4 cores for thirty seconds does not add 4 cores to the pod's
      steady-state footprint. The pod's request is the *larger* of the app
      containers' total and the biggest init container's peak.
    * **Sidecar init containers** (``restartPolicy: Always``, GA since 1.29) keep
      running alongside the app containers, so they add to the total *and* to
      every later init container's peak.
    * **``spec.overhead``** is the RuntimeClass's own cost — a Kata or gVisor
      sandbox is not free — and the scheduler charges it to the node.

    Summing ``spec.containers`` alone undercounts every pod with overhead and
    overcounts every pod with a large one-shot init container. Both errors show
    up as a node whose "requested" column disagrees with ``kubectl describe
    node``, and the operator then trusts neither.
    """
    spec = shaping.get_field(pod, "spec")

    total = _Totals()
    for container in shaping.get_field(spec, "containers", default=[]) or []:
        total.add(_requests_of(container))

    # Sidecars accumulate as we walk the init list in order, because an init
    # container's peak includes every sidecar that started before it.
    sidecars = _Totals()
    init_peak = _Totals()
    for container in shaping.get_field(spec, "initContainers", default=[]) or []:
        requests = _requests_of(container)
        if shaping.get_field(container, "restartPolicy") == "Always":
            sidecars.add(requests)
            init_peak.max_with(sidecars)
            continue
        candidate = sidecars.copy()
        candidate.add(requests)
        init_peak.max_with(candidate)

    total.add(sidecars)
    total.max_with(init_peak)

    overhead = shaping.get_field(spec, "overhead", default={}) or {}
    if isinstance(overhead, dict) and overhead:
        total.add(_requests_of({"resources": {"requests": overhead}}))
    return total


def is_terminated(pod: Any) -> bool:
    """Has this pod finished, releasing its node resources and its pod slot?"""
    return shaping.get_field(pod, "status", "phase") in TERMINAL_PHASES


def node_usage(pods: Iterable[Any]) -> dict[str, Any]:
    """``{"requested": {...}, "pod_count": n}`` for the pods scheduled to one node.

    Callers that could not list pods must not call this — they pass ``None`` for
    both fields instead. Passing an empty iterable here means "the node is
    genuinely empty", which is a different claim and gets genuine zeroes.
    """
    total = _Totals()
    count = 0
    for pod in pods:
        if is_terminated(pod):
            continue
        count += 1
        total.add(pod_requests(pod))
    return {"requested": total.as_row(), "pod_count": count}


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #

_ROLE_LABEL_PREFIX = "node-role.kubernetes.io/"
_LEGACY_ROLE_LABEL = "kubernetes.io/role"


def node_roles(node: Any) -> list[str]:
    """Roles declared by the node's own labels, sorted. Empty when it declares none.

    Nothing is invented here. An unlabelled node gets ``[]`` and the UI renders
    it the way ``kubectl get nodes`` does, as ``<none>`` — because defaulting to
    ``["worker"]`` would be the console asserting a fact the cluster never
    stated, and on a control-plane node whose role label was stripped during an
    upgrade it would be asserting the opposite of the truth next to a "drain"
    button.

    Both spellings are read: ``node-role.kubernetes.io/<role>`` (the modern one,
    where the role is in the key and the value is empty) and the legacy
    ``kubernetes.io/role=<role>`` that older kops and Rancher clusters still
    carry. ``master`` and ``control-plane`` are both reported as they are found,
    not merged: during a version upgrade a node can carry one, the other or both,
    and collapsing them would hide which.
    """
    labels = shaping.get_field(node, "metadata", "labels", default={}) or {}
    if not isinstance(labels, dict):
        return []

    roles: set[str] = set()
    for key, value in labels.items():
        key = str(key)
        if key.startswith(_ROLE_LABEL_PREFIX):
            role = key[len(_ROLE_LABEL_PREFIX):].strip()
            if role:
                roles.add(role)
        elif key == _LEGACY_ROLE_LABEL and value:
            roles.add(str(value).strip())
    return sorted(roles)


def node_ready(node: Any) -> bool | None:
    """Tri-state readiness from ``status.conditions``.

    ``True``/``False`` are the node's own answer. ``None`` covers two cases that
    are the same thing operationally: the ``Ready`` condition is absent, or its
    status is ``Unknown``, which is what the node controller writes when the
    kubelet has stopped reporting altogether.

    ``Unknown`` is deliberately not folded into ``False``. "The node says it is
    not ready" and "the node has stopped talking to us" lead to different next
    actions — the first is a kubelet or CNI problem on a machine that is up, the
    second is a machine that may no longer exist — and §11.2 renders a null as an
    em dash, which is the honest rendering of the second.
    """
    for condition in shaping.get_field(node, "status", "conditions", default=[]) or []:
        if shaping.get_field(condition, "type") != "Ready":
            continue
        status = shaping.get_field(condition, "status")
        if status == "True":
            return True
        if status == "False":
            return False
        return None
    return None


def _internal_ip(node: Any) -> str | None:
    """The node's ``InternalIP`` address, or None.

    Only ``InternalIP``. Falling back to ``ExternalIP`` or ``Hostname`` would put
    a value in a column labelled "internal IP" that is not one, and an operator
    who copies it into an ``ssh`` or a firewall rule gets a silent failure rather
    than a missing field they would have noticed.
    """
    for address in shaping.get_field(node, "status", "addresses", default=[]) or []:
        if shaping.get_field(address, "type") == "InternalIP":
            value = shaping.get_field(address, "address")
            if value:
                return str(value)
    return None


def _resource_block(source: Any) -> dict[str, Any]:
    """``{cpu_cores, memory_bytes, pods}`` from a node's capacity/allocatable map."""
    values = source if isinstance(source, dict) else {}
    return {
        "cpu_cores": cpu_cores(values.get("cpu")),
        "memory_bytes": memory_bytes(values.get("memory")),
        "pods": pod_capacity(values.get("pods")),
    }


def node_row(
    node: Any,
    *,
    requested: dict[str, Any] | None,
    pod_count: int | None,
) -> dict[str, Any]:
    """The §5 node row.

    ``requested`` and ``pod_count`` are **arguments, not derived here**, and both
    are ``None`` when the caller's pod listing failed. That is the shape of the
    whole module: a shaper cannot read from a cluster, so it cannot know whether
    a missing number is missing because the node is idle or because the read was
    refused — and only the caller that made the read can record the reason in
    ``unavailable[]``.
    """
    node_info = shaping.get_field(node, "status", "nodeInfo")
    return {
        "name": shaping.get_field(node, "metadata", "name"),
        "ready": node_ready(node),
        # Absent means schedulable: `unschedulable` is omitted from the object
        # entirely on a normal node, so None here is a real False, not a gap.
        "unschedulable": bool(shaping.get_field(node, "spec", "unschedulable", default=False)),
        "roles": node_roles(node),
        "kubelet_version": shaping.get_field(node_info, "kubeletVersion"),
        "os_image": shaping.get_field(node_info, "osImage"),
        "container_runtime": shaping.get_field(node_info, "containerRuntimeVersion"),
        "internal_ip": _internal_ip(node),
        "age_seconds": shaping.age_seconds(
            shaping.get_field(node, "metadata", "creationTimestamp")
        ),
        "capacity": _resource_block(shaping.get_field(node, "status", "capacity")),
        "allocatable": _resource_block(shaping.get_field(node, "status", "allocatable")),
        "requested": requested,
        "pod_count": pod_count,
        "conditions": [
            {
                "type": shaping.get_field(condition, "type"),
                "status": shaping.get_field(condition, "status"),
                "reason": shaping.get_field(condition, "reason"),
            }
            for condition in shaping.get_field(node, "status", "conditions", default=[]) or []
        ],
        "taints": [
            {
                "key": shaping.get_field(taint, "key"),
                "value": shaping.get_field(taint, "value"),
                "effect": shaping.get_field(taint, "effect"),
            }
            for taint in shaping.get_field(node, "spec", "taints", default=[]) or []
        ],
    }


# --------------------------------------------------------------------------- #
# Cluster reads
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _api_errors(**context: Any) -> Iterator[None]:
    """Turn an ``ApiException`` raised inside the block into a typed AdminError.

    The context is what makes the resulting error useful: an ``rbac_denied`` that
    does not name the verb and resource sends an operator to read a ClusterRole
    line by line. Nested inside :func:`~app.resources.envelope.collect`, it is
    also what gives the ``unavailable`` entry ``forbidden`` rather than the
    catch-all ``unreachable``.
    """
    try:
        yield
    except ApiException as e:
        raise from_api_exception(
            e, context={k: v for k, v in context.items() if v is not None}
        ) from e


#: Pods that hold no resources are excluded server-side rather than fetched and
#: filtered here. On a long-lived cluster the completed-Job pods outnumber the
#: live ones, and transferring them all just to drop them is how a node listing
#: hits the 30-second read deadline and returns nothing at all.
_ACTIVE_PODS_SELECTOR = "status.phase!=Succeeded,status.phase!=Failed"


def _pods_by_node(pods: Iterable[Any]) -> dict[str, list[Any]]:
    """Group pods by ``spec.nodeName``, dropping the unscheduled ones.

    A pod with no ``nodeName`` is pending scheduling and is on no node yet.
    Attributing it to one — or to a ``""`` bucket that a node then matched — would
    charge a node for resources it is not holding.
    """
    grouped: dict[str, list[Any]] = {}
    for pod in pods:
        node_name = shaping.get_field(pod, "spec", "nodeName")
        if not node_name:
            continue
        grouped.setdefault(str(node_name), []).append(pod)
    return grouped


def list_nodes() -> dict[str, Any]:
    """``GET /api/nodes`` (§5) — every node, as the §1.2 envelope.

    Two reads with deliberately different failure handling. The node listing is
    the endpoint's *primary* read: if it fails the endpoint has no answer, so it
    raises and is rendered as the §1.3 error envelope with the hint naming the
    missing grant. The pod listing is *secondary*: losing it costs two columns,
    not the page, so it is collected — and every node's ``requested`` and
    ``pod_count`` become ``None`` while the reason lands in ``unavailable[]``.

    One pod listing for the whole cluster, not one per node. A hundred-node
    cluster would otherwise make a hundred round trips to fill in one column, and
    the read deadline would fire long before the last of them.
    """
    unavailable: list[dict[str, Any]] = []

    with _api_errors(verb="list", group="", resource="nodes"):
        nodes = list(shaping.get_field(get_core_v1().list_node(), "items", default=[]) or [])

    grouped: dict[str, list[Any]] | None = None
    with collect(unavailable, "", "pods"), _api_errors(
        verb="list", group="", resource="pods"
    ):
        listing = get_core_v1().list_pod_for_all_namespaces(
            field_selector=_ACTIVE_PODS_SELECTOR
        )
        grouped = _pods_by_node(shaping.get_field(listing, "items", default=[]) or [])

    rows = []
    for node in nodes:
        name = shaping.get_field(node, "metadata", "name")
        if grouped is None:
            usage: dict[str, Any] = {"requested": None, "pod_count": None}
        else:
            usage = node_usage(grouped.get(str(name), []))
        rows.append(node_row(node, **usage))

    # Sorted by name so the table does not reshuffle under the operator's cursor
    # between refreshes; the API server's node order is not guaranteed stable.
    rows.sort(key=lambda row: row["name"] or "")
    return envelope(rows, unavailable=unavailable)


def get_node(name: str) -> dict[str, Any]:
    """``GET /api/nodes/{name}`` (§5) — the row plus the pods on it.

    ``pods`` is ``None``, not ``[]``, when the pod listing failed, and the reason
    is in ``unavailable[]``. An empty list on this page is the single most
    dangerous wrong answer the console can give: this is the page an operator
    reads immediately before draining, and "no pods on this node" is the sentence
    that makes them click the button.

    Unlike the list endpoint, this one asks for *every* pod on the node,
    terminated ones included, because the detail table should show what is
    actually there. ``pod_count`` and ``requested`` are still computed from the
    non-terminated subset, so the number that is compared against
    ``capacity.pods`` means the same thing on both pages.
    """
    unavailable: list[dict[str, Any]] = []

    with _api_errors(verb="get", group="", resource="nodes", name=name):
        node = get_core_v1().read_node(name)

    pods: list[Any] | None = None
    with collect(unavailable, "", "pods"), _api_errors(
        verb="list", group="", resource="pods", name=name
    ):
        listing = get_core_v1().list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={name}"
        )
        pods = list(shaping.get_field(listing, "items", default=[]) or [])

    if pods is None:
        usage: dict[str, Any] = {"requested": None, "pod_count": None}
    else:
        usage = node_usage(pods)

    row = node_row(node, **usage)
    row["pods"] = None if pods is None else [shaping.pod_row(pod) for pod in pods]
    row["unavailable"] = unavailable
    row["partial"] = bool(unavailable)
    return row


__all__ = [
    "TERMINAL_PHASES",
    "cpu_cores",
    "get_node",
    "is_terminated",
    "list_nodes",
    "memory_bytes",
    "node_ready",
    "node_roles",
    "node_row",
    "node_usage",
    "parse_quantity",
    "pod_capacity",
    "pod_requests",
]
