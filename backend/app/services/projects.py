"""
Projects (§17) — one namespace, read together with what governs it.

OpenShift has a *Project*: a namespace plus the quota, limit range, security
posture and role bindings that make it a place a team can be handed. Vanilla
Kubernetes has every one of those objects and no page that shows them together,
so the question "what will this team be allowed to do in here" is answered by
four ``kubectl get`` calls and a label read. This module is those five reads,
and the page :mod:`app.api.projects` serves is the answer.

Five properties are load-bearing.

**The namespace is the primary read; everything else is secondary.** A
namespace that cannot be read is a 404 or a 403 with a hint, never a page. Each
of the other reads — quotas, limit ranges, role bindings, network policies, the
pod tally — is collected on its own: losing one costs its own key, which is
``None`` with the reason in ``unavailable[]``, and the rest of the page stands.

**``None`` and ``[]`` are different answers, and both are given.** ``quotas: []``
means the namespace has no ResourceQuota, which is the finding — nothing bounds
what runs here. ``quotas: null`` means the listing did not happen. A page that
rendered the second as the first would tell an operator a namespace is
unbounded when it may be tightly bounded, and the operator's next move is to
add a quota that will collide with the one they could not see.

**A quota's ``used`` is ``None`` until the controller has written it.** The
quota controller reconciles ``status.used`` asynchronously; a ResourceQuota
seconds old has ``spec.hard`` and no status. ``0`` there would say "nothing
counts against this quota yet", which is what somebody about to scale a
Deployment wants to hear and must not be told by a field that does not know.

**Pod Security is read off labels and stated as such.** Pod Security admission
takes its cluster-wide default from a file on the API server that no API
serves, so a namespace declaring no ``enforce`` label is reported as declaring
nothing — not as ``privileged``. See :func:`app.resources.shaping.pod_security_row`.

**NetworkPolicy is reported as declared, never as enforced.** The same rule
§8.4 follows, for the same reason: the objects are inert unless the CNI plugin
implements them and no API says whether this cluster's does.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator

from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import get_core_v1, get_networking_v1, get_rbac_v1
from app.resources import shaping
from app.resources.envelope import collect
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: The two OpenShift annotations a Project carries for its human-facing name and
#: purpose. Used on vanilla clusters deliberately: they are inert there, and a
#: namespace created here that is later read by an OpenShift console, or by
#: tooling written for one, shows the same name in both places.
DISPLAY_NAME_ANNOTATION = "openshift.io/display-name"
DESCRIPTION_ANNOTATION = "openshift.io/description"


@contextlib.contextmanager
def _api_errors(**context: Any) -> Iterator[None]:
    """Turn an ``ApiException`` raised inside the block into a typed AdminError.

    The same helper :mod:`app.api.namespaces` and :mod:`app.services.network`
    carry, for the same reason: nested inside
    :func:`~app.resources.envelope.collect` it is what gives the ``unavailable``
    entry ``forbidden`` rather than the catch-all ``unreachable``, and at the top
    level it is what puts the verb and resource into the 403's hint.
    """
    try:
        yield
    except ApiException as e:
        raise from_api_exception(
            e, context={k: v for k, v in context.items() if v is not None}
        ) from e


def _items(listing: Any) -> list[Any]:
    return list(get_field(listing, "items", default=[]) or [])


def _network_policy_summary(policies: list[Any]) -> dict[str, Any]:
    """What the namespace's policies declare about every pod at once.

    ``isolatesAllIngress`` is ``True`` when some policy with an *empty*
    ``podSelector`` — the one that selects every pod — declares ``Ingress`` in
    its policy types. That is the "default deny" shape and the only namespace-wide
    statement a listing of policies can make. ``False`` means no such policy
    exists, which says nothing about narrower policies: individual pods may well
    be selected by one. ``None`` means a selector could not be evaluated.

    None of this says traffic is blocked. The CNI plugin decides that, and the
    UI carries the caveat every time the word "isolated" appears.
    """
    ingress: bool | None = False
    egress: bool | None = False
    names: list[str] = []
    for policy in policies:
        name = get_field(policy, "metadata", "name")
        if name:
            names.append(str(name))
        selects_all = shaping.selector_is_empty(get_field(policy, "spec", "podSelector"))
        types, _source = shaping.policy_types(policy)
        if selects_all is None:
            # A policy whose selector we cannot read might be the one that
            # selects everything. Neither direction may claim a boolean now.
            if ingress is False:
                ingress = None
            if egress is False:
                egress = None
            continue
        if not selects_all:
            continue
        if "Ingress" in types:
            ingress = True
        if "Egress" in types:
            egress = True
    return {
        "count": len(policies),
        "names": sorted(names),
        "isolatesAllIngress": ingress,
        "isolatesAllEgress": egress,
    }


def get_project(name: str) -> dict[str, Any]:
    """§17 ``GET /api/projects/{name}`` — the namespace and what governs it.

    The namespace read is primary and raises. The five reads after it are each
    collected: a failure costs its own key and adds one ``unavailable`` entry,
    and ``partial`` follows from that list as it does everywhere.
    """
    unavailable: list[dict[str, Any]] = []

    with _api_errors(verb="get", group="", resource="namespaces", name=name):
        namespace = get_core_v1().read_namespace(name)

    # Every secondary starts at None — "we did not look" — and only a read that
    # returned moves it. `collect` leaves it there on failure; nothing below may
    # default any of these to an empty list or a zero.
    pod_count: int | None = None
    with collect(unavailable, "", "pods", namespace=name), _api_errors(
        verb="list", group="", resource="pods", namespace=name,
    ):
        pod_count = len(_items(get_core_v1().list_namespaced_pod(name)))

    quotas: list[dict[str, Any]] | None = None
    with collect(unavailable, "", "resourcequotas", namespace=name), _api_errors(
        verb="list", group="", resource="resourcequotas", namespace=name,
    ):
        quotas = [
            shaping.resourcequota_row(obj)
            for obj in _items(get_core_v1().list_namespaced_resource_quota(name))
        ]

    limit_ranges: list[dict[str, Any]] | None = None
    with collect(unavailable, "", "limitranges", namespace=name), _api_errors(
        verb="list", group="", resource="limitranges", namespace=name,
    ):
        limit_ranges = [
            shaping.limitrange_row(obj)
            for obj in _items(get_core_v1().list_namespaced_limit_range(name))
        ]

    role_bindings: list[dict[str, Any]] | None = None
    with collect(
        unavailable, "rbac.authorization.k8s.io", "rolebindings", namespace=name,
    ), _api_errors(
        verb="list", group="rbac.authorization.k8s.io", resource="rolebindings",
        namespace=name,
    ):
        role_bindings = [
            shaping.rolebinding_row(obj)
            for obj in _items(get_rbac_v1().list_namespaced_role_binding(name))
        ]

    network_policies: dict[str, Any] | None = None
    with collect(
        unavailable, "networking.k8s.io", "networkpolicies", namespace=name,
    ), _api_errors(
        verb="list", group="networking.k8s.io", resource="networkpolicies",
        namespace=name,
    ):
        network_policies = _network_policy_summary(
            _items(get_networking_v1().list_namespaced_network_policy(name))
        )

    row = shaping.namespace_row(namespace, pod_count=pod_count)
    annotations = row["annotations"]
    row.update({
        "displayName": annotations.get(DISPLAY_NAME_ANNOTATION),
        "description": annotations.get(DESCRIPTION_ANNOTATION),
        "podSecurity": shaping.pod_security_row(row["labels"]),
        "quotas": quotas,
        "limitRanges": limit_ranges,
        "roleBindings": role_bindings,
        "networkPolicies": network_policies,
        # Hand-built rather than through envelope(): this is one object, not a
        # collection. Kept adjacent so the two cannot drift.
        "partial": bool(unavailable),
        "unavailable": unavailable,
    })
    return row


__all__ = ["DESCRIPTION_ANNOTATION", "DISPLAY_NAME_ANNOTATION", "get_project"]
