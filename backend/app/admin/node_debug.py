"""
Node debug pods (§5.5) — ``kubectl debug node/<name>``, and the largest grant in
this product.

A node that will not join, a kubelet that will not start, a disk filling up with
nothing in any pod's logs: the machine is the thing that needs looking at, and
the operator often cannot SSH to it. Kubernetes' answer is a pod pinned to that
node with the host's filesystem mounted, and this module creates one.

**Read this before changing anything here.** A shell in this pod is, for
practical purposes, root on the machine. ``/host`` is the node's entire root
filesystem, and what is reachable there is worse than it sounds: under
``/var/lib/kubelet/pods`` sit the projected ServiceAccount token and every
mounted Secret of *every pod on that node*, each one a live bearer credential,
and ``/var/lib/kubelet/pki`` holds the node's own client certificate — an
identity in ``system:nodes``. ``hostPID`` shows every process on the box.
That is strictly more power than ``pods/exec`` on any pod scheduled there, and
more than every other write this console performs put together — a Deployment
edit changes one workload, this reads the machine that runs all of them.

The design follows from that, and each of these is a decision rather than a
default inherited from ``kubectl``:

**It has its own gate.** ``ADMIN_NODE_DEBUG_ENABLED`` must be on *as well as*
``ADMIN_ALLOW_MUTATIONS``. §8 of the safety model says two gates rather than
one; this is the third, on the same reasoning ``SECRET_REVEAL_ENABLED`` is the
second — a blast radius different enough in kind that an operator may reasonably
want every other write without it. Leaving it off is not a restriction on what
an operator may do; it is a decision about what this console can be used to do.

**The host filesystem is mounted read-only by default.** ``kubectl debug`` mounts
it writable. Most node debugging is *reading* — logs, kubelet config, disk usage
— and a read-only ``/host`` cannot rewrite a static pod manifest or plant a
binary that runs as root at next boot. Writable is offered, as one explicit
checkbox that says what it means. This is the one place where the safer default
does not cost the feature its purpose.

**No service account token.** ``automountServiceAccountToken: false``, which
``kubectl`` does not set. A pod holding both the node's filesystem and a
Kubernetes API credential is worse than one holding either, and this pod has no
reason to talk to the API server at all.

**Not privileged.** No ``securityContext.privileged``, no added capabilities.
That is the line between reading the machine and being able to reconfigure its
kernel; the reading is what a console should offer, and anything past it belongs
in a manifest somebody wrote and reviewed. ``hostIPC`` is off for the same
reason — node debugging does not need the host's message queues.

**The whole manifest is the diff.** This goes through :func:`mutate` as a
``create``, so ``before`` is ``None`` and the unified diff is the entire pod as
an addition — ``hostPath: /``, ``hostPID: true``, the toleration, all of it, on
screen before the operator confirms. That is the disclosure mechanism, and it
costs nothing because the funnel already does it.

**It is not removed automatically, and unlike an ephemeral container it *can* be
removed.** ``kubectl debug`` has no ``--rm``: it issues no
delete on detach, on exit or on Ctrl-C, says nothing about cleanup, and operators
reliably leak these pods. A console cannot do better *automatically* — a closed
tab is not a signal and a restarted console pod drops whatever would have issued
the DELETE — so it does the honest thing instead: it can find its own pods again,
by label, and offers a removal that goes through the same funnel. §7.4's ephemeral containers
are the opposite case and say so; the asymmetry is real and the UI shows it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.admin.apply import create_fn, delete_resource
from app.admin.images import validate_image_reference
from app.admin.mutate import FeatureGate, Switch, mutate, read_only_switch
from app.admin.names import random_suffix
from app.config import settings
from app.errors import (
    AdminError,
    Invalid,
    NotFound,
    podsecurity_hint,
)
from app.resources import reader
from app.resources.envelope import envelope
from app.resources.shaping import container_state, get_field

logger = logging.getLogger(__name__)

#: The label every pod this module creates carries, and the only thing it selects
#: on. A *label* rather than an annotation because it has to be selectable: the
#: console must be able to find the pods it created, on a cluster where somebody
#: else's tooling also creates debug pods.
COMPONENT_LABEL = "k8boss-admin/component"
COMPONENT_VALUE = "node-debugger"
SELECTOR = f"{COMPONENT_LABEL}={COMPONENT_VALUE}"

#: The node is recorded in an *annotation* as well, and this is not redundant.
#: Label values are capped at 63 characters and may not contain a great deal of
#: what a DNS-1123 node name may; a cloud provider's node name routinely exceeds
#: it. The annotation holds the name untruncated, and ``spec.nodeName`` on the
#: pod remains the authoritative answer to "which node" — the annotation is for
#: a human reading the manifest six months later.
NODE_ANNOTATION = "k8boss-admin/debugNode"

#: ``node-debugger-<node>-<suffix>``, which is ``kubectl debug``'s own shape.
#: Recognisable on purpose: an operator running `kubectl get pods` should be able
#: to tell at a glance that this is somebody debugging and not a workload.
NAME_PREFIX = "node-debugger-"

#: How much of the node name to keep in the pod name. A pod name is a DNS-1123
#: *subdomain* capped at 253 characters and a node name can approach that on its
#: own, so it cannot simply be concatenated. Forty characters is long enough to
#: identify the node at a glance and leaves the total nowhere near the limit.
_NODE_IN_NAME = 40

#: The mount point inside the debug container. ``/host`` is ``kubectl``'s, and
#: keeping it means an operator's muscle memory (`chroot /host`, `ls /host/var/log`)
#: works here too.
HOST_MOUNT_PATH = "/host"
HOST_VOLUME_NAME = "host-root"

_DNS_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_UNSAFE_IN_NAME = re.compile(r"[^a-z0-9-]+")


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """The two switches in front of a node debug pod, in the funnel's terms.

    **Both withhold the dry run**, which is a deliberate departure from every
    other write here. Elsewhere a dry run is a read and is permitted in
    read-only mode. A node debug pod's dry run would return the projected
    manifest — a working recipe for a privileged pod, complete with the
    namespace that admits it — on a deployment whose operator has said this
    feature is off. That is not a read they consented to.
    """
    return FeatureGate(
        feature="a node debug pod",
        message="Creating a node debug pod is disabled on this console.",
        hint=(
            "Set ADMIN_ALLOW_MUTATIONS=true and ADMIN_NODE_DEBUG_ENABLED=true to "
            "allow it. Both are required; the second exists so this one action "
            "can be withheld while every other write stays available."
        ),
        switches=(
            read_only_switch(
                detail=(
                    "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                    "creates nothing on a cluster. Unlike every other write here, a "
                    "dry run is refused too: the projection is a working recipe for a "
                    "privileged pod, and a deployment that has switched this off has "
                    "not consented to handing one out."
                ),
                withholds_dry_run=True,
            ),
            Switch(
                "ADMIN_NODE_DEBUG_ENABLED", settings.node_debug_enabled,
                detail=(
                    "Node debug pods are disabled on this deployment "
                    "(ADMIN_NODE_DEBUG_ENABLED is off). This is a separate gate from "
                    "ADMIN_ALLOW_MUTATIONS because a pod with the node's filesystem "
                    "mounted is a larger grant than the rest of the write surface: a "
                    "shell in one is effectively root on the machine."
                ),
                withholds_dry_run=True,
            ),
        ),
        enabled_detail="Node debug pods are enabled on this deployment.",
    )


def enabled_state() -> dict[str, Any]:
    """Whether this deployment permits node debug pods, and the sentence why.

    Returned to the caller rather than only enforced, so the UI can disable the
    button *with the reason* (rule 11.4) instead of offering it and producing a
    403. The two gates are reported separately because they send an operator to
    two different lines of the same file, and "writes are off" is a different
    conversation from "writes are on and this one specific thing is not".
    """
    return _gate().state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _check_node(node: str) -> str:
    """The node name, or 422 naming the rule.

    Validated rather than forwarded because it goes into ``spec.nodeName``, and a
    ``nodeName`` the cluster does not have produces a pod that is *accepted* and
    then sits in ``Pending`` forever with no scheduler to explain why — the
    scheduler is exactly what ``nodeName`` bypasses. An operator watching that
    would reasonably conclude the node is broken.
    """
    value = (node or "").strip().lower()
    if not value:
        raise Invalid(
            "A node debug pod needs a node to run on.",
            context={"parameter": "node"},
        )
    if len(value) > 253 or not _DNS_SUBDOMAIN.match(value):
        raise Invalid(
            f'"{node}" is not a valid node name.',
            detail=(
                "A node name is a DNS-1123 subdomain: lower-case letters, digits, "
                "dashes and dots, starting and ending with an alphanumeric."
            ),
            context={"parameter": "node", "value": node},
        )
    return value


def _check_image(image: str | None) -> str:
    """The debug image, or 422. Same rules as §7.4's, and the same reasons."""
    return validate_image_reference(
        image if image is not None else settings.debug_image,
        missing_message="A node debug pod needs an image.",
        missing_hint=(
            "Name an image with the tools you need. This console's configured "
            f"default is {settings.debug_image!r}."
        ),
    )


def _pod_name(node: str) -> str:
    """``node-debugger-<node>-<suffix>``, kept inside the name limits.

    The node portion is sanitised and truncated rather than used raw. A pod name
    is a DNS-1123 subdomain of at most 253 characters and a node name can be 253
    on its own, so concatenating them produces a name the API server refuses —
    which would surface as a validation error about a field the operator never
    typed. Dots are dropped rather than kept: they are legal in a pod name but
    make one that reads like a hostname, and the annotation holds the exact node
    anyway.
    """
    stem = _UNSAFE_IN_NAME.sub("-", node)[:_NODE_IN_NAME].strip("-")
    suffix = random_suffix()
    # A node name of nothing but dots would sanitise away entirely. The pod still
    # needs a legal name, and `spec.nodeName` is what actually pins it.
    return f"{NAME_PREFIX}{stem}-{suffix}" if stem else f"{NAME_PREFIX}{suffix}"


# --------------------------------------------------------------------------- #
# The pod
# --------------------------------------------------------------------------- #

def build_pod(
    *,
    node: str,
    name: str,
    image: str,
    namespace: str,
    writable_host: bool,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    """The manifest this module creates. Pure — no I/O, so it can be asserted on.

    Every privileged field below is here for a stated reason, and the ones that
    are *absent* are as deliberate as the ones present:

    * ``nodeName`` pins the pod and bypasses the scheduler, which is the point:
      the node being debugged is often one the scheduler would refuse.
    * ``tolerations: [{operator: Exists}]`` tolerates every taint. Note what this
      is and is not for: ``NoSchedule`` taints and a cordon are enforced by the
      *scheduler*, which ``nodeName`` has already bypassed, so the toleration
      buys nothing there — the pod lands on a cordoned node with or without it.
      What it prevents is the **taint-eviction controller** throwing the pod off
      a node tainted ``NoExecute`` moments after the kubelet started it, which is
      a state a node worth debugging is frequently in. It also means this pod
      will run on a control-plane node if that is the one named.
    * ``hostPID`` makes every process on the machine visible, which is most of
      the diagnostic value ("what is eating this node"). Be clear-eyed about the
      rest: the host ``/proc`` exposes every process's ``environ`` and
      ``cmdline`` — env-injected secrets from every container on the node — and
      ``/proc/<pid>/root`` reaches into other containers' mount namespaces,
      including tmpfs Secret mounts that never touch the host disk. ``SYS_PTRACE``
      is *not* added, so live memory inspection of host processes is not
      available; that is the one line this stops short of.
    * ``hostNetwork`` puts the pod on the node's network namespace, so its
      interfaces, routes and connectivity are the node's. This is the other half
      of why the feature exists.
    * The ``hostPath`` volume is the node's root filesystem, mounted read-only
      unless the caller explicitly asked otherwise.
    * ``automountServiceAccountToken: false`` — no API credential. This pod has
      no business talking to the API server, and a token beside the host
      filesystem is a strictly worse object than either alone.
    * ``restartPolicy: Never`` — a debug container that crash-loops on a node
      forever is litter, and the operator wants to see that it exited.
    * No ``securityContext``: not privileged, no added capabilities. That is the
      line between reading the machine and reconfiguring it.
    * No ``resources``: an unset request is BestEffort, which the kubelet always
      admits. A request could be refused on the very node that is under pressure
      — the one being debugged.
    * ``activeDeadlineSeconds`` (``ADMIN_NODE_DEBUG_MAX_SECONDS``, 0 to disable)
      bounds how long the container runs. It does **not** delete the pod — the
      kubelet stops the container and marks the pod Failed — so it mitigates the
      unattended-shell half of this feature's known hazard and leaves the
      removal half exactly where it was.
    """
    # 0 (or None) means unbounded, and the field is then omitted rather than
    # sent as 0 — the API server rejects activeDeadlineSeconds: 0.
    deadline = settings.node_debug_max_seconds if max_seconds is None else max_seconds

    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {COMPONENT_LABEL: COMPONENT_VALUE},
            # The untruncated node name, for whoever reads this manifest later.
            "annotations": {NODE_ANNOTATION: node},
        },
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "hostNetwork": True,
            "hostPID": True,
            # Required *because* hostNetwork is on, and a deliberate divergence
            # from `kubectl debug`, which omits it. An unset dnsPolicy defaults
            # to ClusterFirst, and the kubelet silently downgrades that to
            # Default for a hostNetwork pod — so the container resolves through
            # the node's /etc/resolv.conf and `nslookup kubernetes.default`
            # fails. An operator debugging "can this node reach my service"
            # would read that as cluster DNS being broken. It costs no privilege
            # to get right.
            "dnsPolicy": "ClusterFirstWithHostNet",
            "automountServiceAccountToken": False,
            "tolerations": [{"operator": "Exists"}],
            "volumes": [
                {
                    "name": HOST_VOLUME_NAME,
                    # `type: Directory` makes the kubelet refuse to create the
                    # path if it is somehow absent, rather than silently mounting
                    # a new empty directory and presenting it as the node's root.
                    "hostPath": {"path": "/", "type": "Directory"},
                }
            ],
            "containers": [
                {
                    "name": "debugger",
                    "image": image,
                    # `IfNotPresent`: the node being debugged may be the one that
                    # cannot reach the registry, and a cached image that starts
                    # beats a fresh pull that hangs in ImagePullBackOff.
                    "imagePullPolicy": "IfNotPresent",
                    "stdin": True,
                    "tty": True,
                    "terminationMessagePolicy": "File",
                    "volumeMounts": [
                        {
                            "name": HOST_VOLUME_NAME,
                            "mountPath": HOST_MOUNT_PATH,
                            "readOnly": not writable_host,
                        }
                    ],
                }
            ],
        },
    }
    if deadline:
        # The only mitigation available for the hazard the module docstring
        # admits it cannot close: nothing deletes this pod when the operator
        # walks away. The kubelet stops the *container* at the deadline and
        # marks the pod Failed — the object stays and still needs removing, so
        # this bounds the live window rather than solving the leak, and the UI
        # says so rather than implying a cleanup.
        pod["spec"]["activeDeadlineSeconds"] = int(deadline)
    return pod


# --------------------------------------------------------------------------- #
# Reading what is already there
# --------------------------------------------------------------------------- #

def _row(pod: Any) -> dict[str, Any]:
    """One node debug pod, as the UI reads it."""
    statuses = get_field(pod, "status", "containerStatuses", default=[]) or []
    state, reason = container_state(statuses[0] if statuses else None)
    mounts = (
        get_field(pod, "spec", "containers", default=[]) or [{}]
    )[0]
    read_only = None
    for mount in get_field(mounts, "volumeMounts", default=[]) or []:
        if get_field(mount, "name") == HOST_VOLUME_NAME:
            # `readOnly` is absent rather than false on a writable mount, which
            # is the API's own default. `bool(None)` would be correct here by
            # luck; it is written out so the next reader does not have to check.
            read_only = bool(get_field(mount, "readOnly", default=False))
            break
    return {
        "name": get_field(pod, "metadata", "name"),
        "namespace": get_field(pod, "metadata", "namespace"),
        # From the spec, not the annotation: this is what actually pinned the
        # pod, and it is not subject to the annotation being edited.
        "node": get_field(pod, "spec", "nodeName"),
        "image": get_field(mounts, "image"),
        "phase": get_field(pod, "status", "phase"),
        "state": state,
        "reason": reason,
        "started_at": (
            get_field(statuses[0] if statuses else None, "state", "running", "startedAt")
            or get_field(statuses[0] if statuses else None, "state", "terminated", "startedAt")
        ),
        "created_at": get_field(pod, "metadata", "creationTimestamp"),
        # `null` when no host mount was found at all, which is a pod wearing our
        # label that we did not create. Rendering that as "read-only" would be a
        # safety claim about somebody else's object.
        "hostFilesystemReadOnly": read_only,
    }


def list_node_debug_pods(node: str) -> dict[str, Any]:
    """``GET /api/nodes/{name}/debug`` (§5.5).

    Every debug pod this console created *for this node*, plus whether the
    deployment permits creating another.

    Listed by label and then filtered on ``spec.nodeName`` rather than by a field
    selector on both, because the pods live in one configured namespace and the
    list is small; doing the node match locally also means a pod whose label
    somebody copied onto an unrelated object cannot appear under a node it is not
    on.

    ``items: []`` is a real zero — the namespace was listed and holds none. A
    listing that could not happen raises, per §0.1.
    """
    resolved = _check_node(node)
    namespace = settings.node_debug_namespace
    listing = reader.list_resource(
        "", "v1", "pods", namespace=namespace, label_selector=SELECTOR, limit=200,
    )
    items = [
        _row(pod)
        for pod in listing.get("items") or []
        if get_field(pod, "spec", "nodeName") == resolved
    ]

    state = enabled_state()
    # The continue token is carried through rather than dropped. `envelope()`
    # derives nothing from it, so a listing that stopped at the limit and
    # reported `continue: null` would be saying "these are all of them" about a
    # page — §0.1's corollary applied to pagination, and on this feature the
    # thing being under-reported is host-mounted pods.
    #
    # `remaining` is the API server's count for the *label* query; the local
    # nodeName filter below can only reduce what is shown, so treat it as an
    # upper bound rather than an exact tally of this node's.
    result = envelope(items, cont=listing.get("continue"), remaining=listing.get("remaining"))
    # Additive to §1.2. The UI needs all three: whether it may offer the action,
    # the sentence to put on the disabled control, and the namespace to name in
    # the confirm dialog — an operator should not have to guess where a
    # privileged pod is about to appear.
    result["enabled"] = state["enabled"]
    result["enabledDetail"] = state["detail"]
    result["namespace"] = namespace
    return result


# --------------------------------------------------------------------------- #
# The writes
# --------------------------------------------------------------------------- #

def create_node_debug_pod(
    node: str,
    *,
    image: str | None = None,
    writable_host: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """``POST /api/nodes/{name}/debug`` (§5.5).

    Returns the §1.5 mutation response plus the pod that was (or would be)
    created, so the caller can open a terminal on it without parsing the diff.

    **PodSecurity admission is what decides whether this is possible at all, and
    the dry run is where that is discovered.** A namespace labelled
    ``baseline`` or ``restricted`` rejects a pod with a ``hostPath`` volume and
    ``hostPID``; admission runs on ``dryRun=All`` exactly as it does on the real
    call, so the refusal arrives at the preview step as ``422 invalid`` carrying
    the admission plugin's own message — before anything exists. That is not a
    special case this module implements; it is the funnel's dry run doing its
    job, and it is the reason the namespace is configurable.
    """
    resolved = _check_node(node)
    resolved_image = _check_image(image)

    namespace = settings.node_debug_namespace
    name = _pod_name(resolved)
    body = build_pod(
        node=resolved, name=name, image=resolved_image,
        namespace=namespace, writable_host=writable_host,
    )

    try:
        result = mutate(
            verb="create",
            group="",
            version="v1",
            plural="pods",
            namespace=namespace,
            name=name,
            dry_run=dry_run,
            # Step one of the funnel, and before the dry run: see `_gate` for why
            # a projection is refused here when every other write permits one.
            gate=_gate(),
            apply_fn=create_fn("", "v1", "pods", body, namespace=namespace, name=name),
            before=None,
            # The audit sentence names the node, the image and — because it is the
            # difference between reading the machine and being able to change it —
            # whether the host filesystem was mounted writable.
            detail=(
                f"node debug pod {name} on {resolved} ({resolved_image}); "
                f"host filesystem {'read-write' if writable_host else 'read-only'}"
            ),
        )
    except AdminError as e:
        # Pod Security is the likeliest refusal this feature meets, and the one
        # whose default advice is worst. This pod carries a hostPath volume and
        # host namespaces, so every level above `privileged` rejects it — with a
        # 403 that reads as a missing permission, sending the operator to widen a
        # ClusterRole that was never the obstacle. No RBAC grant admits a pod the
        # namespace's enforce label refuses.
        #
        # The funnel has already audited the failure; only the advice changes,
        # and the code, the status and the API server's own message are untouched.
        raise podsecurity_hint(
            e, namespace=namespace, setting="ADMIN_NODE_DEBUG_NAMESPACE"
        ) from None

    result["pod"] = name
    result["namespace"] = namespace
    result["node"] = resolved
    result["image"] = resolved_image
    result["writableHostFilesystem"] = bool(writable_host)
    return result


def remove_node_debug_pod(node: str, pod: str, *, dry_run: bool = True) -> dict[str, Any]:
    """``DELETE /api/nodes/{name}/debug/{pod}`` (§5.5).

    Removal is the half of this feature that §7.4 cannot offer: an ephemeral
    container cannot be deleted, and this can. A node debug pod outlives the tab
    that opened it, and one left running with the node's root filesystem attached
    is a hazard nobody is watching.

    **This endpoint deletes only pods this console created.** The label and the
    ``nodeName`` are both checked first, and a pod failing either is a
    ``404 not_found`` for *this* endpoint rather than a delete. Without that
    check the route would be a pod-delete with a node in the path, reachable by
    anyone who can call it and bypassing the resource browser's own confirm
    dialog — a privilege this feature has no reason to hand out. The generic
    delete in §4 remains the way to remove any other pod, gated as it already is.
    """
    resolved = _check_node(node)
    namespace = settings.node_debug_namespace
    context = {
        "group": "", "version": "v1", "resource": "pods",
        "namespace": namespace, "name": pod, "node": resolved,
    }

    live = reader.get_resource("", "v1", "pods", pod, namespace=namespace)
    labels = get_field(live, "metadata", "labels", default={}) or {}
    if labels.get(COMPONENT_LABEL) != COMPONENT_VALUE:
        raise NotFound(
            f'Pod "{pod}" is not a debug pod created by this console.',
            detail=(
                f"It does not carry {COMPONENT_LABEL}={COMPONENT_VALUE}."
            ),
            hint=(
                "This endpoint removes only the debug pods this console created. "
                "Delete any other pod from the resource browser."
            ),
            context=context,
        )
    if get_field(live, "spec", "nodeName") != resolved:
        raise NotFound(
            f'Pod "{pod}" is not a debug pod for node "{resolved}".',
            detail=f"It is pinned to {get_field(live, 'spec', 'nodeName')!r}.",
            context=context,
        )

    # Delegated rather than reimplemented: §4's delete already reads the live
    # object so the diff is `before=live, after=null`, already goes through the
    # funnel, and already refuses on a read-only console. A second delete path
    # would be a second place for those to be got right.
    #
    # `Background` propagation: a pod owns nothing, so there is nothing to cascade
    # to and nothing to wait for.
    return delete_resource("", "v1", "pods", namespace, pod, "Background", dry_run)


__all__ = [
    "COMPONENT_LABEL",
    "COMPONENT_VALUE",
    "HOST_MOUNT_PATH",
    "NAME_PREFIX",
    "NODE_ANNOTATION",
    "SELECTOR",
    "build_pod",
    "create_node_debug_pod",
    "enabled_state",
    "list_node_debug_pods",
    "remove_node_debug_pod",
]
