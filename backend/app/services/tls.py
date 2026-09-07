"""
§32 — the certificate an exposure serves, and whether it is the right one.

Every other section here is about an object a cluster serves. This one is about
a fact a cluster **does not report at all**: when the certificate behind a Route
or an Ingress stops working. `kubectl get ingress` prints `TLS: 1 secret`.
`oc get route` prints `edge`. Neither prints a date, and neither prints a name.
Both facts are in the certificate, in the clear — every client that completes a
handshake with that server is handed them — and reaching them means base64-
decoding a Secret and running `openssl x509 -text`, which is why nobody does it
until the site is down.

The certificate is public material and the private key is not, and this module
never touches the second one. It reads exactly one key of a
`kubernetes.io/tls` Secret — `tls.crt` — and returns decoded fields, never the
PEM. `tls.key` is not read, not decoded, not counted and not named.

**Two questions, and the second is the one that surprises people.**

*When does it expire* is the famous one. It is also the easy one, because it is
a date in the object.

*Is it even for this hostname* is the other, and a certificate can be current,
correctly issued and completely useless. An Ingress moved to a new host, a
wildcard that covers `*.example.com` and not `example.com`, a Secret that two
Ingresses share where only one of them was renamed — each produces a green row
in every Kubernetes tool there is and a browser that refuses to connect. The
check is done against the **subject alternative names only**. Every browser
shipping today ignores the Common Name for host verification, so a console that
matched on CN would call a certificate correct that no client will accept.

**What this module refuses to claim.**

*That the client will accept it.* No trust store is consulted, no chain is
built, no signature is verified and no revocation is checked. A certificate this
report calls unexpired and name-matching can still be rejected by every browser
on earth. What is reported is what the objects say, and the report says so where
an operator will read it rather than in a footnote.

*That this is what the router serves.* The Secret is what the object points at.
A router started with `--default-ssl-certificate`, a controller-specific
annotation overriding the Secret, a cert-manager renewal that has landed in the
Secret but not yet in a router that has not reloaded — in each of those the
bytes on the wire differ from the bytes here, and only a handshake settles it.
This console does not make handshakes with the exposures it lists.

*That an unreadable certificate is a missing one.* A Secret this console cannot
read leaves `certificate: null` with a `state` of `unknown` and a finding that
says which read failed. It never renders as an exposure with no certificate,
which is the reading that sends somebody to create a Secret that already exists
and is fine.

**Where a certificate can live**, and all four are reported:

* a Secret named by an Ingress `spec.tls[]` block — **one entry per block**, not
  one per Ingress, because an Ingress with two blocks has two certificates and
  reporting the first as though it covered every host is the mistake this
  section is about;
* a Secret named by a Route's `spec.tls.externalCertificate`;
* a Route's own `spec.tls.certificate`, inline in the object;
* a Secret named by a Gateway listener's `certificateRefs` — the §13 module says
  TLS belongs to the Gateway's listener rather than to an HTTPRoute, and this is
  the sentence that finishes: a Gateway API cluster has certificates, they are
  just somewhere else.

And two places where there is deliberately **no** certificate to find: a
`passthrough` Route terminates TLS in the pod, and an exposure that terminates
TLS while naming nothing is served by whatever default certificate the router
holds. Both are reported as what they are. Calling either one "no certificate"
would put a red row on an exposure that is working exactly as designed.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from app.resources import reader, shaping
from app.resources.envelope import collect, envelope
from app.resources.shaping import get_field
from app.services import routes as routes_service

logger = logging.getLogger(__name__)

#: The only key of a Secret this module reads. Named once, used once, and the
#: reason both are true: `tls.key` sits beside it in the same dict, and a
#: module that indexed `data` by a variable could be made to read it by a
#: rename. There is no code path here from a private key to a response.
CERTIFICATE_KEY = "tls.crt"

#: The Secret type Kubernetes reserves for a certificate/key pair. A Secret of
#: any other type is not read: `Opaque` Secrets hold arbitrary application data,
#: and a console that went rummaging through one looking for something
#: certificate-shaped would be reading application secrets on a page about
#: expiry dates.
TLS_SECRET_TYPE = "kubernetes.io/tls"

#: Where the certificate for one exposure lives.
SOURCE_SECRET = "secret"
SOURCE_INLINE = "inline"
#: TLS terminates at the router and nothing names a certificate, so the router
#: serves its own default. Not an error and not a missing certificate: it is the
#: ordinary state of an exposure on a cluster with a wildcard router cert. What
#: it *is* is unreadable from here — the router's default lives in the router's
#: own namespace under a name this console is not told.
SOURCE_ROUTER_DEFAULT = "router_default"
#: `passthrough`: the pod holds the certificate and the router never sees it.
SOURCE_BACKEND = "backend"

#: The certificate's own verdict about time. `unknown` is a fifth state and not
#: a flavour of `valid` — see the module docstring.
STATE_VALID = "valid"
STATE_EXPIRING = "expiring"
STATE_EXPIRED = "expired"
STATE_NOT_YET_VALID = "not_yet_valid"
STATE_UNKNOWN = "unknown"

#: How much warning is warning. Thirty days is Let's Encrypt's own renewal
#: window — a certificate that has not renewed with thirty days left has a
#: broken renewal, not a tight schedule — and it is long enough to survive a
#: change freeze, which is when these are actually discovered.
EXPIRING_WINDOW_SECONDS = 30 * 24 * 3600

#: How many distinct Secrets one report will read. A namespace with four hundred
#: Ingresses would otherwise turn one page load into four hundred reads against
#: the API server. Sources past the budget are reported with
#: `certificate: null` and the `certificate_not_read` finding — an explicit "we
#: stopped looking", never a row that looks examined.
MAX_CERTIFICATE_READS = 100

#: Findings. Each is a fact about the certificate or about this console's
#: ability to read it; none is a claim that a client will or will not connect.
FINDING_EXPIRED = "certificate_expired"
FINDING_EXPIRING = "certificate_expiring"
FINDING_NOT_YET_VALID = "certificate_not_yet_valid"
FINDING_HOST_NOT_COVERED = "host_not_covered"
FINDING_NO_SANS = "no_subject_alt_names"
FINDING_CHAIN_LEAF_ONLY = "chain_leaf_only"
FINDING_SELF_SIGNED = "self_signed"
FINDING_UNREADABLE = "certificate_unreadable"
FINDING_ROUTER_DEFAULT = "certificate_not_named"
FINDING_NOT_READ = "certificate_not_read"
FINDING_PASSTHROUGH = "certificate_in_backend"

#: Gateways are read here and nowhere else in the console: §13 lists HTTPRoutes,
#: which carry no TLS at all. The version preference mirrors §13's for the same
#: reason — pinning `v1` 404s on a cluster still serving `v1beta1`, and a 404
#: reads as "this cluster has no Gateways".
GATEWAY_GROUP = "gateway.networking.k8s.io"
GATEWAY_VERSIONS = ("v1", "v1beta1")
GATEWAYS = "gateways"

#: Gateway listener TLS modes. `Passthrough` is the Gateway API's spelling of
#: the same thing a passthrough Route means.
GATEWAY_TERMINATE = "Terminate"
GATEWAY_PASSTHROUGH = "Passthrough"

#: Default page size for the three listings, matching §13's.
DEFAULT_LIMIT = 500


# --------------------------------------------------------------------------- #
# Host matching — pure
# --------------------------------------------------------------------------- #

def _normalise_host(value: Any) -> str | None:
    """Lower-case, trailing dot removed, or ``None`` for nothing usable.

    A hostname is case-insensitive and `example.com.` and `example.com` are the
    same name. Both normalisations exist because certificates and Ingress specs
    are written by different people and only one of them is usually consistent.
    """
    text = str(value or "").strip().rstrip(".").lower()
    return text or None


def host_matches(host: str, pattern: str) -> bool:
    """Does one subject alternative name cover one hostname? (RFC 6125 §6.4.3)

    Wildcards are **one label, leftmost only, and the whole label**: `*.a.com`
    covers `b.a.com` and covers neither `a.com` nor `c.b.a.com`, and `w*.a.com`
    is not treated as a wildcard at all. Every one of those restrictions is a
    real browser rule, and relaxing any of them here would let this console
    report a host as covered that Chrome will refuse — a green row in front of
    an outage, which is worse than no row.
    """
    left = _normalise_host(host)
    right = _normalise_host(pattern)
    if left is None or right is None:
        return False
    if left == right:
        return True
    if not right.startswith("*."):
        return False
    suffix = right[2:]
    if "*" in suffix:
        # A second wildcard is not a wildcard: RFC 6125 allows one, leftmost.
        # Reaching this needs a hostname carrying a `*` somewhere other than its
        # first label, which the API server's own validation rejects for an
        # Ingress rule, a Route host and a Gateway listener alike — so it is a
        # malformed object meeting a malformed certificate, and the honest
        # answer to that pair is "no", not a clever one.
        #
        # There is deliberately no `not suffix` beside this: `_normalise_host`
        # strips trailing dots, so a pattern reaching here as `*.` has already
        # become `*` and failed the `startswith` above. A branch no input can
        # take is one no test can hold.
        return False
    # One label, and a label that is actually there: `a.com` against `*.a.com`
    # is not a match, which is the wildcard mistake that takes down an apex
    # domain the morning after somebody "simplified" the certificate.
    head, _, tail = left.partition(".")
    return bool(head) and tail == suffix


def covers_host(host: Any, dns_names: Iterable[str], ip_addresses: Iterable[str]) -> bool:
    """Whether any SAN on the certificate covers this hostname.

    An exposure host that is a literal IP address is matched against the
    certificate's IP SANs and never against its DNS SANs, because that is how a
    client matches it: a `dNSName` of `10.0.0.1` does not authenticate a
    connection to `https://10.0.0.1`.
    """
    normalised = _normalise_host(host)
    if normalised is None:
        return False
    try:
        address = ipaddress.ip_address(normalised)
    except ValueError:
        return any(host_matches(normalised, name) for name in dns_names)
    for candidate in ip_addresses:
        try:
            if ipaddress.ip_address(str(candidate).strip()) == address:
                return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------- #
# The certificate's verdict about time — pure
# --------------------------------------------------------------------------- #

def expiry_state(decoded: dict[str, Any] | None) -> tuple[str, int | None]:
    """``(state, seconds_until_notAfter)`` for one decoded certificate.

    ``None`` seconds — never ``0`` — whenever the certificate could not be read
    or carries no ``notAfter``. `0` is a real value here and it means *expires
    this second*; using it for "we could not look" is the corollary the whole
    contract is built on, aimed at the one number an operator would act on
    immediately.

    ``not_yet_valid`` outranks the expiry buckets because it is the state that
    explains a certificate nobody can use *today*, and it is not rare: a
    freshly-issued certificate on a cluster whose clock is behind is exactly
    this, and reading it as "valid, expires in 89 days" sends the operator to
    look at the router.
    """
    if not decoded or decoded.get("error"):
        return STATE_UNKNOWN, None

    remaining = shaping.seconds_until(decoded.get("not_after"))
    starts_in = shaping.seconds_until(decoded.get("not_before"))
    if remaining is None:
        return STATE_UNKNOWN, None
    if starts_in is not None and starts_in > 0:
        return STATE_NOT_YET_VALID, remaining
    if remaining <= 0:
        return STATE_EXPIRED, remaining
    if remaining <= EXPIRING_WINDOW_SECONDS:
        return STATE_EXPIRING, remaining
    return STATE_VALID, remaining


# --------------------------------------------------------------------------- #
# Where each kind keeps its certificates — pure
# --------------------------------------------------------------------------- #

def _source(
    *,
    kind: str,
    group: str,
    obj: Any,
    slot: str,
    source: str,
    hosts: list[str],
    termination: str | None,
    secret_name: str | None = None,
    secret_namespace: str | None = None,
    pem: str | None = None,
    detail: str,
) -> dict[str, Any]:
    """One place a certificate is (or provably is not), before it is read.

    ``slot`` distinguishes the several certificates one object can have — an
    Ingress TLS block's index, a Gateway listener's name — and is what makes the
    report's ``id`` unique per certificate rather than per object.
    """
    name = get_field(obj, "metadata", "name")
    namespace = get_field(obj, "metadata", "namespace")
    return {
        "id": f"{kind.lower()}/{namespace}/{name}#{slot}",
        "kind": kind,
        "group": group,
        "name": None if name is None else str(name),
        "namespace": None if namespace is None else str(namespace),
        "slot": slot,
        "source": source,
        "termination": termination,
        "hosts": hosts,
        "secret_name": secret_name,
        "secret_namespace": secret_namespace,
        # Consumed by the assembler and never copied into a response entry. The
        # PEM is public, but there is no reason for a report about expiry dates
        # to ship certificate bodies to a browser, and the smallest surface that
        # cannot leak one is the one that never carries it outward.
        "pem": pem,
        "detail": detail,
    }


def route_sources(obj: Any) -> list[dict[str, Any]]:
    """The certificate a Route points at — at most one, and sometimes none.

    ``wildcardPolicy: Subdomain`` adds a second hostname to check, and it is not
    cosmetic: a Route with that policy serves every sibling of its host, so a
    certificate naming only `foo.apps.example.com` covers the route's own name
    and none of the traffic the policy invites. That is a live exposure serving
    certificate errors to every host but one, and no field anywhere says so.
    """
    tls = get_field(obj, "spec", "tls")
    if not tls:
        return []

    termination = get_field(tls, "termination")
    spec_host = get_field(obj, "spec", "host")
    hosts = [str(spec_host)] if spec_host else []
    for entry in get_field(obj, "status", "ingress", default=[]) or []:
        host = get_field(entry, "host")
        if host and str(host) not in hosts:
            hosts.append(str(host))
    if str(get_field(obj, "spec", "wildcardPolicy") or "") == "Subdomain":
        for host in list(hosts):
            _, _, parent = str(host).partition(".")
            wildcard = f"*.{parent}"
            if parent and wildcard not in hosts:
                hosts.append(wildcard)

    common = {
        "kind": "Route", "group": "route.openshift.io", "obj": obj,
        "slot": "tls", "hosts": hosts, "termination": termination,
    }
    if str(termination or "") == "passthrough":
        return [_source(
            **common, source=SOURCE_BACKEND,
            detail=(
                "This Route passes TLS through to the pod, which holds the "
                "certificate. The router never sees one, and neither does this "
                "console."
            ),
        )]

    inline = get_field(tls, "certificate")
    if inline:
        return [_source(
            **common, source=SOURCE_INLINE, pem=str(inline),
            detail="The certificate is written into this Route's own spec.tls.certificate.",
        )]

    external = get_field(tls, "externalCertificate", "name")
    if external:
        return [_source(
            **common, source=SOURCE_SECRET,
            secret_name=str(external),
            secret_namespace=get_field(obj, "metadata", "namespace"),
            detail=f"spec.tls.externalCertificate names the Secret {external}.",
        )]

    return [_source(
        **common, source=SOURCE_ROUTER_DEFAULT,
        detail=(
            "This Route terminates TLS and names no certificate, so the router "
            "serves its own default. What that is lives in the router's "
            "namespace and is not readable from here."
        ),
    )]


def ingress_sources(obj: Any) -> list[dict[str, Any]]:
    """One entry **per ``spec.tls[]`` block**, which is the point.

    An Ingress with two TLS blocks has two certificates covering two host sets,
    and §13's row — which reports the first Secret — is a summary, not an
    answer. Reporting the first here would mean saying `shop.example.com` is
    covered by a certificate that has nothing to do with it.

    A block with no ``hosts`` covers every host on the Ingress. Missing that
    would report a working exposure as having a certificate for nothing.
    """
    rule_hosts: list[str] = []
    for rule in get_field(obj, "spec", "rules", default=[]) or []:
        host = get_field(rule, "host")
        if host and str(host) not in rule_hosts:
            rule_hosts.append(str(host))

    sources: list[dict[str, Any]] = []
    for index, block in enumerate(get_field(obj, "spec", "tls", default=[]) or []):
        hosts = [str(h) for h in (get_field(block, "hosts", default=[]) or []) if h]
        if not hosts:
            hosts = list(rule_hosts)
        secret = get_field(block, "secretName")
        common = {
            "kind": "Ingress", "group": "networking.k8s.io", "obj": obj,
            "slot": str(index), "hosts": hosts, "termination": "edge",
        }
        if secret:
            sources.append(_source(
                **common, source=SOURCE_SECRET,
                secret_name=str(secret),
                secret_namespace=get_field(obj, "metadata", "namespace"),
                detail=f"spec.tls[{index}].secretName names the Secret {secret}.",
            ))
        else:
            sources.append(_source(
                **common, source=SOURCE_ROUTER_DEFAULT,
                detail=(
                    f"spec.tls[{index}] names no Secret, so the ingress "
                    "controller serves its own default certificate for these "
                    "hosts. What that is depends on the controller and is not "
                    "readable from here."
                ),
            ))
    return sources


def gateway_sources(obj: Any) -> list[dict[str, Any]]:
    """One entry per certificate reference on every listener that terminates TLS.

    A listener can name several — that is how one address serves several names
    by SNI — and each is a separate certificate with its own expiry, so each is
    a separate row.

    ``certificateRefs`` may point into **another namespace**, which the Gateway
    API allows only with a ReferenceGrant in that namespace. This module does not
    read ReferenceGrants: whether the reference is permitted is the Gateway
    controller's decision and is visible in the listener's own conditions. What
    it does do is read the Secret where the ref actually points, so a
    cross-namespace certificate is reported at its real address rather than
    looked for in the Gateway's namespace and reported missing.
    """
    gateway_namespace = get_field(obj, "metadata", "namespace")
    sources: list[dict[str, Any]] = []

    for index, listener in enumerate(get_field(obj, "spec", "listeners", default=[]) or []):
        tls = get_field(listener, "tls")
        if not tls:
            continue
        listener_name = str(get_field(listener, "name") or index)
        hostname = get_field(listener, "hostname")
        # A listener with no hostname serves every name that reaches it. An
        # empty host list means there is nothing to check coverage against, not
        # that the certificate covers nothing.
        hosts = [str(hostname)] if hostname else []
        mode = str(get_field(tls, "mode") or GATEWAY_TERMINATE)
        common = {
            "kind": "Gateway", "group": GATEWAY_GROUP, "obj": obj,
            "hosts": hosts, "termination": mode.lower(),
        }
        if mode == GATEWAY_PASSTHROUGH:
            sources.append(_source(
                **common, slot=listener_name, source=SOURCE_BACKEND,
                detail=(
                    f"Listener {listener_name} passes TLS through to the "
                    "backend, which holds the certificate."
                ),
            ))
            continue

        refs = get_field(tls, "certificateRefs", default=[]) or []
        if not refs:
            sources.append(_source(
                **common, slot=listener_name, source=SOURCE_ROUTER_DEFAULT,
                detail=(
                    f"Listener {listener_name} terminates TLS and names no "
                    "certificate, so the Gateway implementation supplies one."
                ),
            ))
            continue

        for ref_index, ref in enumerate(refs):
            name = get_field(ref, "name")
            if not name:
                continue
            ref_namespace = get_field(ref, "namespace") or gateway_namespace
            sources.append(_source(
                **common,
                slot=f"{listener_name}/{ref_index}",
                source=SOURCE_SECRET,
                secret_name=str(name),
                secret_namespace=None if ref_namespace is None else str(ref_namespace),
                detail=(
                    f"Listener {listener_name} references the Secret {name} in "
                    f"namespace {ref_namespace}."
                ),
            ))
    return sources


#: The three kinds this section reads, in the order they are reported.
KIND_SPECS: tuple[dict[str, Any], ...] = (
    {
        "kind": "Route", "group": "route.openshift.io", "versions": ("v1",),
        "plural": "routes", "extract": route_sources,
    },
    {
        "kind": "Ingress", "group": "networking.k8s.io", "versions": ("v1",),
        "plural": "ingresses", "extract": ingress_sources,
    },
    {
        "kind": "Gateway", "group": GATEWAY_GROUP, "versions": GATEWAY_VERSIONS,
        "plural": GATEWAYS, "extract": gateway_sources,
    },
)


# --------------------------------------------------------------------------- #
# Reading the certificate
# --------------------------------------------------------------------------- #

def certificate_from_secret(secret: Any) -> tuple[str | None, str | None]:
    """``(pem, error)`` for one Secret — and never anything but ``tls.crt``.

    A Secret of the wrong type is refused by type rather than searched for
    something certificate-shaped: `Opaque` Secrets hold application data, and a
    console that went looking through one would be reading passwords on a page
    about expiry dates.
    """
    secret_type = get_field(secret, "type")
    if str(secret_type or "") != TLS_SECRET_TYPE:
        return None, (
            f"That Secret is of type {secret_type or 'none'}, not "
            f"{TLS_SECRET_TYPE}, so this console does not read it."
        )
    encoded = get_field(secret, "data", CERTIFICATE_KEY)
    if not encoded:
        return None, f"That Secret carries no {CERTIFICATE_KEY}."
    try:
        return base64.b64decode(str(encoded), validate=True).decode("utf-8", "replace"), None
    except (binascii.Error, ValueError) as exc:
        return None, f"The {CERTIFICATE_KEY} in that Secret is not valid base64: {exc}"


def _findings(
    source: dict[str, Any],
    decoded: dict[str, Any] | None,
    state: str,
    remaining: int | None,
    uncovered: list[str],
) -> list[dict[str, Any]]:
    """Everything true about this certificate that an operator would want told.

    Ordered by what would ruin a day soonest. Nothing here is a verdict on
    whether a client will connect — see the module docstring — and the two that
    look like faults but are not (`self_signed`, `certificate_in_backend`) are
    still reported, because a report that hid them would be answering a
    different question than the one on the screen.
    """
    findings: list[dict[str, Any]] = []

    if source["source"] == SOURCE_BACKEND:
        return [{"code": FINDING_PASSTHROUGH, "detail": source["detail"]}]
    if source["source"] == SOURCE_ROUTER_DEFAULT:
        return [{"code": FINDING_ROUTER_DEFAULT, "detail": source["detail"]}]

    if decoded is None or decoded.get("error"):
        # Three ways to arrive here and they are not the same sentence: the read
        # failed, the read was never attempted because the budget was spent, or
        # the bytes came back and would not decode. `read_code`/`read_error` are
        # set by the reader for the first two; the decoder's own error covers
        # the third.
        detail = source.get("read_error") or (decoded or {}).get("error") or (
            "This console could not read the certificate."
        )
        return [{
            "code": source.get("read_code") or FINDING_UNREADABLE,
            "detail": (
                f"{detail} Nothing here says this exposure has no certificate — "
                "only that this console could not read the one it names."
            ),
        }]

    days = None if remaining is None else remaining // 86400
    if state == STATE_EXPIRED:
        findings.append({
            "code": FINDING_EXPIRED,
            "detail": (
                f"This certificate expired on {decoded['not_after']}"
                + (f", {abs(days)} days ago." if days is not None else ".")
            ),
        })
    elif state == STATE_EXPIRING:
        findings.append({
            "code": FINDING_EXPIRING,
            "detail": (
                f"This certificate expires on {decoded['not_after']}"
                + (f", in {days} days." if days is not None else ".")
            ),
        })
    if state == STATE_NOT_YET_VALID:
        findings.append({
            "code": FINDING_NOT_YET_VALID,
            "detail": (
                f"This certificate is not valid until {decoded['not_before']}. "
                "Clients reject it now. Either it was issued for a future date "
                "or this cluster's clock disagrees with the issuer's."
            ),
        })

    if not decoded["dns_names"] and not decoded["ip_addresses"]:
        findings.append({
            "code": FINDING_NO_SANS,
            "detail": (
                "This certificate carries no subject alternative names. Every "
                "current browser requires one and ignores the Common Name, so "
                "it authenticates no hostname at all — however correct its "
                f"subject ({decoded['subject_common_name'] or 'unset'}) looks."
            ),
        })
    elif uncovered:
        findings.append({
            "code": FINDING_HOST_NOT_COVERED,
            "detail": (
                "This certificate does not name "
                + ", ".join(uncovered)
                + ". Its names are "
                + ", ".join(decoded["dns_names"] + decoded["ip_addresses"])
                + ". Clients reaching those hosts get a name-mismatch error, "
                "whatever the expiry says."
            ),
        })

    if decoded["self_signed"]:
        findings.append({
            "code": FINDING_SELF_SIGNED,
            "detail": (
                "The issuer and the subject of this certificate are the same "
                "name, so no certificate authority vouches for it. On an "
                "internal cluster that is usually deliberate; on a public "
                "hostname it is not."
            ),
        })
    elif decoded["chain_length"] == 1:
        findings.append({
            "code": FINDING_CHAIN_LEAF_ONLY,
            "detail": (
                "This bundle holds only the leaf certificate — no intermediate "
                "is included. Clients that do not already hold the issuer "
                f"({decoded['issuer_common_name'] or 'unnamed'}) cannot build a "
                "chain to it. This console does not verify chains, so this is a "
                "statement about the bundle rather than about any client."
            ),
        })

    return findings


def _entry(
    source: dict[str, Any],
    decoded: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble one report row, key by key.

    Explicitly rather than by spreading ``source``: that dict carries the PEM,
    and a row built by copying it would put certificate bodies on the wire the
    first time somebody added a key. Every field below is named, so nothing
    arrives here by accident.
    """
    state, remaining = expiry_state(decoded)
    readable = bool(decoded) and not decoded.get("error")

    hosts_covered: list[dict[str, Any]] = []
    uncovered: list[str] = []
    for host in source["hosts"]:
        if not readable:
            # Tri-state, and the null is load-bearing: `false` here is a claim
            # that the certificate does not name this host, and we have not read
            # a certificate to make it with.
            hosts_covered.append({"host": host, "covered": None})
            continue
        covered = covers_host(host, decoded["dns_names"], decoded["ip_addresses"])
        hosts_covered.append({"host": host, "covered": covered})
        if not covered:
            uncovered.append(host)

    return {
        "id": source["id"],
        "kind": source["kind"],
        "group": source["group"],
        "name": source["name"],
        "namespace": source["namespace"],
        "slot": source["slot"],
        "source": source["source"],
        "termination": source["termination"],
        "hosts": source["hosts"],
        "secret": (
            None if source["secret_name"] is None
            else {"namespace": source["secret_namespace"], "name": source["secret_name"]}
        ),
        "sourceDetail": source["detail"],
        # `decoded` can be the decoder's blank-with-an-error shape, which is a
        # dict and would render as a certificate with no dates and no names.
        # `remaining` needs no such guard: `expiry_state` already returns
        # `None` for exactly the input that makes `readable` false, and a
        # second condition saying the same thing is one no input can tell apart
        # from the first.
        "certificate": decoded if readable else None,
        "state": state,
        "expires_in_seconds": remaining,
        "hostsCovered": hosts_covered,
        "findings": _findings(source, decoded, state, remaining, uncovered),
    }


@dataclass
class _ReadBudget:
    """How many more distinct Secrets this report will open.

    A counter rather than a slice of the source list, because the budget is
    spent on *reads* and several rows can share one: forty Ingresses behind one
    wildcard Secret cost one, and capping the rows instead would have hidden
    thirty-nine of them for nothing.
    """

    remaining: int

    def spend(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


#: What one Secret read produced: the decoded certificate, or the sentence
#: saying why there is none. Cached as a pair rather than as the certificate
#: alone so the *second* exposure sharing an unreadable Secret gets the same
#: sentence as the first, instead of a vaguer one because the failure was
#: recorded on a row that has already been built.
_ReadResult = tuple["dict[str, Any] | None", "str | None"]


def _read_certificate(
    source: dict[str, Any],
    cache: dict[tuple[str | None, str], _ReadResult],
    unavailable: list[dict[str, Any]],
    budget: _ReadBudget,
) -> dict[str, Any] | None:
    """The decoded certificate for one source, or ``None`` with a reason on it.

    Secrets are cached by ``(namespace, name)`` for the life of one report, so
    forty Ingresses sharing one wildcard Secret cost one read — and, more
    usefully, all forty rows agree about it. The budget is spent per *distinct*
    Secret for the same reason.
    """
    if source["source"] == SOURCE_INLINE:
        return shaping.decode_certificate_chain(source["pem"])
    if source["source"] != SOURCE_SECRET:
        return None

    key = (source["secret_namespace"], source["secret_name"])
    if key in cache:
        decoded, error = cache[key]
        if error is not None:
            source["read_code"] = FINDING_UNREADABLE
            source["read_error"] = error
        return decoded

    if not budget.spend():
        source["read_code"] = FINDING_NOT_READ
        source["read_error"] = (
            f"This report reads at most {MAX_CERTIFICATE_READS} certificates and "
            "had already read that many when it reached this one. Narrow it to "
            "one namespace to see this row."
        )
        return None

    secret: dict[str, Any] | None = None
    with collect(unavailable, "", "secrets", namespace=source["secret_namespace"]):
        secret = reader.read_object(
            "", "v1", "secrets", str(source["secret_name"]),
            namespace=source["secret_namespace"],
        )

    if secret is None:
        source["read_code"] = FINDING_UNREADABLE
        source["read_error"] = (
            f"The Secret {source['secret_namespace']}/{source['secret_name']} "
            "could not be read; the banner above says why."
        )
        cache[key] = (None, source["read_error"])
        return None

    pem, error = certificate_from_secret(secret)
    # The blank shape comes from the decoder itself rather than being written
    # out here, so a field added to a decoded certificate cannot go missing from
    # the "we read the Secret and it holds no certificate" case.
    decoded = (
        {**shaping.decode_certificate_chain(None), "error": error} if pem is None
        else shaping.decode_certificate_chain(pem)
    )
    cache[key] = (decoded, None)
    return decoded


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def certificate_report(
    *,
    namespace: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """§32 ``GET /api/routes/certificates`` — every certificate the edge points at.

    Each of the three kinds is listed inside its own :func:`collect` block, so a
    cluster that serves Routes and forbids Gateways reports the Routes and names
    the Gateway failure, rather than showing a short list that looks complete.

    ``kinds[]`` carries all three regardless, with the state discovery reported
    for each. Without it a Gateway API cluster with no Ingresses would render an
    empty page that reads as "no certificates here" — the exact confidently
    wrong answer this section is about, applied to itself.
    """
    unavailable: list[dict[str, Any]] = []
    kinds: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []

    for spec in KIND_SPECS:
        served = routes_service.resolve_served_version(
            group=spec["group"], versions=spec["versions"],
            plural=spec["plural"], kind=spec["kind"],
        )
        kinds.append({
            "kind": spec["kind"],
            "group": spec["group"],
            "state": served.state,
            "version": served.version,
            "detail": served.detail,
        })

        if served.state == routes_service.STATE_UNKNOWN and served.error is not None:
            # §13's entry builder, not a local one. It raises rather than
            # inventing a token for an error that is not a statement about
            # whether we could look — and the token anybody inventing one
            # reaches for first is `unreachable`, which would send an operator
            # to check a network that answered fine.
            unavailable.append(
                routes_service.unknown_entry(spec["group"], spec["plural"], served.error)
            )
            continue
        if served.state != routes_service.STATE_AVAILABLE:
            continue

        listing: dict[str, Any] | None = None
        with collect(unavailable, spec["group"], spec["plural"], namespace=namespace):
            listing = reader.list_resource(
                spec["group"], str(served.version), spec["plural"],
                namespace=namespace, limit=limit,
            )
        if listing is None:
            continue

        for obj in listing["items"]:
            sources.extend(spec["extract"](obj))

        if listing["continue"]:
            truncated.append({
                "kind": spec["kind"],
                "shown": len(listing["items"]),
                "remaining": listing["remaining"],
            })

    cache: dict[tuple[str | None, str], _ReadResult] = {}
    budget = _ReadBudget(MAX_CERTIFICATE_READS)
    items = [
        _entry(source, _read_certificate(source, cache, unavailable, budget))
        for source in sources
    ]

    result = envelope(items, unavailable=unavailable)
    result["kinds"] = kinds
    result["truncated"] = truncated
    result["expiringWindowSeconds"] = EXPIRING_WINDOW_SECONDS
    result["maxCertificateReads"] = MAX_CERTIFICATE_READS
    return result


__all__ = [
    "CERTIFICATE_KEY",
    "EXPIRING_WINDOW_SECONDS",
    "FINDING_CHAIN_LEAF_ONLY",
    "FINDING_EXPIRED",
    "FINDING_EXPIRING",
    "FINDING_HOST_NOT_COVERED",
    "FINDING_NOT_READ",
    "FINDING_NOT_YET_VALID",
    "FINDING_NO_SANS",
    "FINDING_PASSTHROUGH",
    "FINDING_ROUTER_DEFAULT",
    "FINDING_SELF_SIGNED",
    "FINDING_UNREADABLE",
    "KIND_SPECS",
    "MAX_CERTIFICATE_READS",
    "SOURCE_BACKEND",
    "SOURCE_INLINE",
    "SOURCE_ROUTER_DEFAULT",
    "SOURCE_SECRET",
    "STATE_EXPIRED",
    "STATE_EXPIRING",
    "STATE_NOT_YET_VALID",
    "STATE_UNKNOWN",
    "STATE_VALID",
    "TLS_SECRET_TYPE",
    "certificate_from_secret",
    "certificate_report",
    "covers_host",
    "expiry_state",
    "gateway_sources",
    "host_matches",
    "ingress_sources",
    "route_sources",
]
