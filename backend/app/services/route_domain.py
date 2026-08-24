"""§13 — the cluster's wildcard DNS domain, and the hostnames generated under it.

An exposure needs a hostname, and on a cluster with wildcard DNS the operator
should not have to type the same suffix onto every one. OpenShift does this:
a Route created with no ``spec.host`` gets ``<name>-<namespace>.<appsDomain>``,
where the domain comes from the cluster's own ingress configuration.

This module is that, for every cluster the console talks to rather than only for
OpenShift.

**The domain belongs to the cluster, not to the console.** It is a column on the
registration (:class:`app.models.Cluster`), because two registered clusters have
two different wildcards. A console-wide setting would generate a hostname that
resolves on one of them and nowhere on the other, and the operator would find out
from a browser, not from this console.

**Not knowing is a first-class answer.** ``None`` here means the console does not
know of a wildcard domain, and the route dialog then offers to generate nothing
at all. That is deliberate and it is the §1 rule applied to a hostname: filling
in ``shop-web.apps.example.com`` on a cluster whose DNS has no such record
produces an exposure that is created, reports Admitted, and routes nothing —
§14's failure with a hostname in place of a controller. A suffix nobody can
resolve is worse than an empty field, because the empty field asks a question
and the suffix answers it wrongly.

**Discovery never overwrites what an operator typed.** OpenShift publishes its
wildcard at ``ingresses.config.openshift.io/cluster``; this module reads it and
offers it, and the stored value still wins. A cluster can perfectly well serve
its Routes under one domain and have the operator exposing things under a CNAME
of it, and a console that "corrected" that on every read would be overwriting a
deliberate choice with a discovered default.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app import database
from app.errors import Invalid, NotFound, Unsupported
from app.k8s.context import get_current_cluster_id
from app.models import Cluster
from app.resources import reader
from app.resources.envelope import collect
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: Where OpenShift publishes the cluster's wildcard domain. A singleton object
#: named ``cluster``; ``.spec.domain`` is the bare domain with no leading dot.
_OPENSHIFT_INGRESS_CONFIG = ("config.openshift.io", "v1", "ingresses", "cluster")

#: One DNS label: letters, digits and inner hyphens, 63 characters at most.
#: Applied to each label of a candidate domain and to each label of a generated
#: hostname, because both end up in the same place — a `spec.host` the API
#: server will accept and DNS will not resolve if it is malformed.
_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

#: RFC 1035 on the whole name. A generated hostname that exceeds it is refused
#: rather than truncated: a truncated hostname is a different hostname, and one
#: that silently points somewhere else is the defect this file is written
#: against.
_MAX_HOSTNAME = 253


def normalize_domain(value: str | None) -> str | None:
    """Validate a wildcard domain as an operator might type it, or raise.

    Accepts a leading ``*.`` or ``.`` and strips it — those are how the same
    domain is written in a DNS zone and in conversation, and refusing them
    teaches the operator a distinction that does not matter here. What is not
    accepted is anything that would not resolve: an empty label, an underscore,
    a scheme, a port, or a trailing path.
    """
    if value is None:
        return None
    candidate = value.strip().lower().rstrip(".")
    for prefix in ("*.", "."):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
    if not candidate:
        return None

    if "/" in candidate or ":" in candidate:
        raise Invalid(
            "The app domain is a bare DNS domain, not a URL.",
            detail=f"Got {value!r}.",
            hint="Write it as apps.example.com — no scheme, port or path.",
            context={"field": "app_domain"},
        )
    labels = candidate.split(".")
    if len(labels) < 2:
        raise Invalid(
            "The app domain needs at least two labels.",
            detail=f"Got {value!r}.",
            hint="A single label cannot carry a wildcard record — apps.example.com.",
            context={"field": "app_domain"},
        )
    for label in labels:
        if not _LABEL.match(label):
            raise Invalid(
                f"{label!r} is not a valid DNS label.",
                detail=f"In {value!r}.",
                hint=(
                    "Labels are lowercase letters, digits and inner hyphens, at "
                    "most 63 characters."
                ),
                context={"field": "app_domain"},
            )
    if len(candidate) > _MAX_HOSTNAME:
        raise Invalid(
            "The app domain is longer than a DNS name may be.",
            detail=f"{len(candidate)} characters; the limit is {_MAX_HOSTNAME}.",
            context={"field": "app_domain"},
        )
    return candidate


def stored_domain() -> str | None:
    """The domain recorded on the request's cluster, or None.

    Read straight from the registration each time rather than cached with the
    client: this is edited in the cluster dialog, and a cached copy would keep
    generating hostnames under the old domain until something else evicted it.
    """
    cluster_id = get_current_cluster_id()
    if cluster_id is None:
        return None
    db = database.SessionLocal()
    try:
        cluster = db.get(Cluster, cluster_id)
        return cluster.app_domain if cluster is not None else None
    finally:
        db.close()


def discover_domain(unavailable: list[dict[str, Any]] | None = None) -> str | None:
    """The wildcard domain the cluster publishes about itself, or None.

    Only OpenShift publishes one. On plain Kubernetes the object is absent, and
    that is an ordinary fact rather than a failure — a cluster with an ingress
    controller and a wildcard record has nowhere to write it down, which is
    exactly why the stored column exists.

    A read that *failed* is not the same as an absent object, and is recorded in
    ``unavailable`` when the caller offers a list. The difference matters at the
    cluster dialog: "this cluster publishes no domain, type one" and "we could
    not ask this cluster" lead to different actions, and the second one must not
    render as the first.

    ``Unsupported`` is caught here alongside ``NotFound``, and that is the line
    that keeps this feature from spoiling the page it appears on. On plain
    Kubernetes the whole ``config.openshift.io`` group is absent, so discovery
    raises ``Unsupported`` — and ``collect`` records every ``AdminError``,
    including that one. Left to it, the capabilities envelope would come back
    ``partial: true`` on every read on every non-OpenShift cluster, and §11.1's
    "some of this could not be read" banner would be permanently lit by a group
    the cluster was never expected to serve. A banner that is always on is a
    banner nobody reads, which costs the real partial reads their only signal.
    """
    group, version, plural, name = _OPENSHIFT_INGRESS_CONFIG
    config: dict[str, Any] | None = None

    with collect(unavailable if unavailable is not None else [], group, plural):
        try:
            config = reader.get_resource(group, version, plural, name)
        except (NotFound, Unsupported):
            # Not OpenShift, or OpenShift with no ingress config. Both are
            # ordinary facts about a cluster: None already says "no domain from
            # here", and there is nothing for an operator to go and fix.
            return None

    if config is None:
        return None

    domain = get_field(config, "spec", "domain")
    if not isinstance(domain, str) or not domain.strip():
        return None
    try:
        return normalize_domain(domain)
    except Invalid:
        # The cluster published something this console cannot build a hostname
        # from. Log it and offer nothing rather than propagating: the caller is
        # rendering a form, and a cluster's own malformed config is not a reason
        # to fail the operator's request to open it.
        logger.warning(
            "Cluster published an app domain that is not a DNS name: %r", domain,
        )
        return None


def generated_host(name: str, namespace: str, domain: str | None) -> str | None:
    """``<name>-<namespace>.<domain>``, OpenShift's rule, or None.

    The namespace is in there on purpose. Without it, a Service called ``web``
    in two namespaces generates one hostname twice; the second exposure is
    admitted by most controllers and then loses to the first, which is a routing
    outage whose cause is invisible in either object.

    Returns None rather than a partial string whenever a piece is missing or the
    result would not be a legal hostname — the caller renders an empty field and
    the operator types what they want, which is the honest outcome when this
    function cannot produce a correct answer.
    """
    if not domain or not name or not namespace:
        return None
    left = f"{name.strip().lower()}-{namespace.strip().lower()}"
    if not _LABEL.match(left):
        return None
    host = f"{left}.{domain}"
    if len(host) > _MAX_HOSTNAME:
        return None
    return host


def domain_report() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What ``/routes/capabilities`` publishes about hostname generation.

    Returns the payload and any ``unavailable`` entries the discovery read
    produced, for the caller to fold into its own envelope — this module builds
    no envelope of its own, so ``partial`` stays computed in one place.

    ``value`` is what a generated hostname will actually be built under, and
    ``source`` says which of the two it came from. Both are reported even when
    they agree, because "the domain you typed matches what the cluster
    publishes" is worth seeing, and so is the opposite.
    """
    unavailable: list[dict[str, Any]] = []
    stored = stored_domain()
    discovered = discover_domain(unavailable)

    value = stored or discovered
    return (
        {
            "value": value,
            "source": "configured" if stored else ("discovered" if discovered else None),
            "stored": stored,
            "discovered": discovered,
            # The rule the frontend renders under the field, held here so the
            # hostname the dialog previews and the one the backend would build
            # are described by the same sentence.
            "pattern": "<name>-<namespace>.<domain>" if value else None,
        },
        unavailable,
    )
