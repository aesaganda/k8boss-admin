"""
The certificate behind an exposure (§32).

`kubectl get ingress` prints `TLS: 1 secret`. It does not print a date and it
does not print a name, and those are the two facts that decide whether the site
is up tomorrow. Every assertion here is about a way of reporting them that would
still be wrong:

  expired vs unread      A certificate this console could not read is `unknown`
                         with a reason, never a row that looks examined and
                         never "this exposure has no certificate" — the second
                         reading sends somebody to create a Secret that already
                         exists and is fine.

  null vs zero           `expires_in_seconds` is `None` when we could not look.
                         `0` is a real value here and it means *expires this
                         second*, which is the one number an operator acts on
                         without reading the rest of the row.

  current vs correct     A certificate can be freshly issued, correctly signed
                         and for the wrong hostname. Matching is done against
                         subject alternative names only, with RFC 6125 wildcard
                         rules, because that is what a browser does — and a
                         console that matched on Common Name would call a
                         certificate correct that no client will accept.

  first vs each          An Ingress with two `spec.tls[]` blocks has two
                         certificates covering two host sets. Reporting the
                         first as though it covered everything is exactly the
                         confidently wrong answer this section exists to stop.

  absent vs elsewhere    A passthrough Route's certificate is in the pod and a
                         router default is in the router's namespace. Neither is
                         a missing certificate, and drawing either as one puts a
                         red row on an exposure working as designed.
"""

from __future__ import annotations

import base64
import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from kubernetes.client.rest import ApiException

from app.resources import catalog, shaping
from app.services import tls as svc

NOW = datetime.datetime.now(datetime.timezone.utc)
NAMESPACE = "prod"


# --------------------------------------------------------------------------- #
# Real certificates, because a decoder tested against a fixture string tests the
# fixture. These are generated per call and never leave the process.
# --------------------------------------------------------------------------- #

def make_pem(
    *,
    common_name="shop.example.com",
    dns_names=("shop.example.com",),
    ip_addresses=(),
    valid_from_days=-30,
    valid_to_days=60,
    issuer_common_name=None,
    extra_chain=0,
):
    """One PEM bundle: a leaf, optionally followed by ``extra_chain`` CA certs."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, issuer_common_name or common_name)
    ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(0x03AB5F)
        .not_valid_before(NOW + datetime.timedelta(days=valid_from_days))
        .not_valid_after(NOW + datetime.timedelta(days=valid_to_days))
    )
    if dns_names is not None or ip_addresses:
        entries = [x509.DNSName(name) for name in (dns_names or ())]
        entries += [x509.IPAddress(ip) for ip in ip_addresses]
        if entries:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(entries), critical=False
            )
    pem = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    for index in range(extra_chain):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f"Intermediate {index}")
        ])
        ca = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(100 + index)
            .not_valid_before(NOW - datetime.timedelta(days=1000))
            .not_valid_after(NOW + datetime.timedelta(days=1000))
            .sign(ca_key, hashes.SHA256())
        )
        pem += ca.public_bytes(serialization.Encoding.PEM)
    return pem.decode()


def tls_secret(pem, *, name="shop-tls", namespace=NAMESPACE, secret_type=svc.TLS_SECRET_TYPE):
    data = {}
    if pem is not None:
        data[svc.CERTIFICATE_KEY] = base64.b64encode(pem.encode()).decode()
        # Present in every fixture on purpose: the tests below assert this value
        # never reaches a response, which is only meaningful if it is there to
        # be leaked.
        data["tls.key"] = base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nnope\n").decode()
    return {
        "apiVersion": "v1", "kind": "Secret", "type": secret_type,
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }


def ingress(*, name="checkout", rules=("shop.example.com",), tls=()):
    return {
        "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "rules": [{"host": host} for host in rules],
            "tls": [dict(block) for block in tls],
        },
    }


def route(*, name="checkout", host="shop.example.com", tls=None, wildcard=None, status_hosts=()):
    spec = {"host": host}
    if tls is not None:
        spec["tls"] = dict(tls)
    if wildcard:
        spec["wildcardPolicy"] = wildcard
    return {
        "apiVersion": "route.openshift.io/v1", "kind": "Route",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": spec,
        "status": {"ingress": [{"host": h} for h in status_hosts]},
    }


def gateway(*, name="edge", listeners=()):
    return {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"listeners": [dict(listener) for listener in listeners]},
    }


# --------------------------------------------------------------------------- #
# Host matching — the check every browser makes and no Kubernetes API does
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("host,pattern", [
    ("shop.example.com", "shop.example.com"),
    ("SHOP.Example.COM", "shop.example.com"),
    ("shop.example.com.", "shop.example.com"),
    ("b.example.com", "*.example.com"),
])
def test_a_name_the_certificate_covers_is_covered(host, pattern):
    assert svc.host_matches(host, pattern) is True


@pytest.mark.parametrize("host,pattern,why", [
    ("example.com", "*.example.com", "a wildcard never covers its own apex"),
    ("a.b.example.com", "*.example.com", "a wildcard is one label, not any number"),
    ("web1.example.com", "web*.example.com", "a partial label is not a wildcard"),
    ("example.com", "*", "a bare star matches nothing"),
    ("example.com", "*.", "a star with no suffix matches nothing"),
    ("a.example.com", "*.*.com", "a second star is not a wildcard this console honours"),
])
def test_a_name_the_certificate_does_not_cover_is_not_covered(host, pattern, why):
    assert svc.host_matches(host, pattern) is False, why


def test_an_ip_host_is_matched_against_ip_sans_and_never_against_dns_sans():
    # A `dNSName` of 10.0.0.1 does not authenticate a connection to
    # https://10.0.0.1, and reporting it as covering one would be a green row in
    # front of a certificate error.
    assert svc.covers_host("10.0.0.1", ["10.0.0.1"], []) is False
    assert svc.covers_host("10.0.0.1", [], ["10.0.0.1"]) is True


def test_a_host_that_is_missing_is_never_reported_as_covered():
    assert svc.covers_host(None, ["*.example.com"], []) is False


# --------------------------------------------------------------------------- #
# The decoder — pure, total, and never silently empty
# --------------------------------------------------------------------------- #

def test_the_leaf_is_decoded_with_its_names_dates_and_serial():
    decoded = shaping.decode_certificate_chain(
        make_pem(dns_names=("shop.example.com", "*.shop.example.com"))
    )
    assert decoded["error"] is None
    assert decoded["subject_common_name"] == "shop.example.com"
    assert decoded["dns_names"] == ["shop.example.com", "*.shop.example.com"]
    assert decoded["serial"] == "03:ab:5f"
    assert decoded["not_after"].endswith("Z")
    assert decoded["key"]["algorithm"] == "ECDSA"


def test_a_certificate_with_no_sans_reports_empty_lists_not_nulls():
    # The distinction the host check hangs off: [] is "we read it and it names
    # nothing", None is "we did not read it". A certificate that names nothing
    # authenticates no hostname at all, however correct its CN looks.
    decoded = shaping.decode_certificate_chain(make_pem(dns_names=None))
    assert decoded["error"] is None
    assert decoded["dns_names"] == []
    assert decoded["ip_addresses"] == []


def test_the_bundles_length_is_reported_so_a_leaf_only_bundle_is_visible():
    assert shaping.decode_certificate_chain(make_pem())["chain_length"] == 1
    assert shaping.decode_certificate_chain(
        make_pem(extra_chain=2)
    )["chain_length"] == 3


def test_self_signed_is_name_equality_and_says_so_by_being_false_when_issued():
    assert shaping.decode_certificate_chain(make_pem())["self_signed"] is True
    assert shaping.decode_certificate_chain(
        make_pem(issuer_common_name="Example CA")
    )["self_signed"] is False


@pytest.mark.parametrize("raw", ["", None, "not a pem at all", "-----BEGIN CERTIFICATE-----\nzz\n"])
def test_an_undecodable_bundle_is_every_field_null_with_a_reason(raw):
    decoded = shaping.decode_certificate_chain(raw)
    assert decoded["error"]
    # Never an empty name list beside a null error: that reads as a certificate
    # that was read and found to be nameless.
    assert decoded["dns_names"] is None
    assert decoded["not_after"] is None
    assert decoded["chain_length"] is None


def test_seconds_until_is_not_clamped_because_a_lapsed_expiry_is_the_point():
    past = (NOW - datetime.timedelta(days=3)).isoformat().replace("+00:00", "Z")
    assert shaping.seconds_until(past) < 0
    # The contrast that makes it a separate function: age_seconds floors at zero
    # because nothing is created in the future.
    assert shaping.age_seconds(past) > 0
    assert shaping.seconds_until(None) is None


# --------------------------------------------------------------------------- #
# The verdict about time
# --------------------------------------------------------------------------- #

def test_a_certificate_well_inside_its_life_is_valid():
    state, remaining = svc.expiry_state(shaping.decode_certificate_chain(make_pem()))
    assert state == svc.STATE_VALID
    assert remaining > svc.EXPIRING_WINDOW_SECONDS


def test_a_certificate_inside_the_renewal_window_is_expiring():
    state, remaining = svc.expiry_state(
        shaping.decode_certificate_chain(make_pem(valid_to_days=10))
    )
    assert state == svc.STATE_EXPIRING
    assert 0 < remaining <= svc.EXPIRING_WINDOW_SECONDS


def test_an_expired_certificate_keeps_its_negative_remaining():
    state, remaining = svc.expiry_state(
        shaping.decode_certificate_chain(make_pem(valid_from_days=-90, valid_to_days=-3))
    )
    assert state == svc.STATE_EXPIRED
    assert remaining < 0


def test_a_certificate_that_has_not_started_outranks_the_expiry_buckets():
    # Issued for a future date, or a cluster clock behind the issuer's. Reading
    # it as "valid, 89 days left" sends the operator to look at the router.
    state, _ = svc.expiry_state(
        shaping.decode_certificate_chain(make_pem(valid_from_days=2, valid_to_days=90))
    )
    assert state == svc.STATE_NOT_YET_VALID


@pytest.mark.parametrize("decoded", [None, {}, {"error": "nope"}])
def test_an_unread_certificate_is_unknown_with_a_null_and_never_a_zero(decoded):
    state, remaining = svc.expiry_state(decoded)
    assert state == svc.STATE_UNKNOWN
    # 0 here would render as "expires today" on a row nobody managed to read.
    assert remaining is None


# --------------------------------------------------------------------------- #
# Where each kind keeps its certificates
# --------------------------------------------------------------------------- #

def test_a_route_with_no_tls_has_no_certificate_to_report():
    assert svc.route_sources(route()) == []


def test_a_passthrough_route_points_at_the_pod_and_not_at_a_missing_secret():
    [source] = svc.route_sources(route(tls={"termination": "passthrough"}))
    assert source["source"] == svc.SOURCE_BACKEND
    assert source["secret_name"] is None


def test_a_route_carrying_its_certificate_inline_is_read_from_the_route():
    pem = make_pem()
    [source] = svc.route_sources(
        route(tls={"termination": "edge", "certificate": pem, "key": "PRIVATE"})
    )
    assert source["source"] == svc.SOURCE_INLINE
    assert source["pem"] == pem


def test_a_route_naming_an_external_certificate_points_at_that_secret():
    [source] = svc.route_sources(route(tls={
        "termination": "reencrypt", "externalCertificate": {"name": "shop-tls"},
    }))
    assert source["source"] == svc.SOURCE_SECRET
    assert (source["secret_namespace"], source["secret_name"]) == (NAMESPACE, "shop-tls")


def test_a_route_terminating_tls_and_naming_nothing_is_the_routers_default():
    [source] = svc.route_sources(route(tls={"termination": "edge"}))
    assert source["source"] == svc.SOURCE_ROUTER_DEFAULT


def test_a_subdomain_wildcard_route_adds_the_hostname_it_actually_serves():
    # The certificate names foo.apps.example.com and the policy invites every
    # sibling of it. No field anywhere says the rest get a name mismatch.
    [source] = svc.route_sources(route(
        host="foo.apps.example.com",
        tls={"termination": "edge", "certificate": make_pem()},
        wildcard="Subdomain",
    ))
    assert source["hosts"] == ["foo.apps.example.com", "*.apps.example.com"]


def test_an_ingress_reports_one_entry_per_tls_block_not_one_per_ingress():
    sources = svc.ingress_sources(ingress(
        rules=("shop.example.com", "admin.example.com"),
        tls=(
            {"hosts": ["shop.example.com"], "secretName": "shop-tls"},
            {"hosts": ["admin.example.com"], "secretName": "admin-tls"},
        ),
    ))
    assert [s["secret_name"] for s in sources] == ["shop-tls", "admin-tls"]
    assert [s["hosts"] for s in sources] == [["shop.example.com"], ["admin.example.com"]]
    assert len({s["id"] for s in sources}) == 2


def test_a_tls_block_with_no_hosts_covers_every_host_on_the_ingress():
    [source] = svc.ingress_sources(ingress(
        rules=("shop.example.com", "admin.example.com"),
        tls=({"secretName": "wildcard-tls"},),
    ))
    assert source["hosts"] == ["shop.example.com", "admin.example.com"]


def test_a_tls_block_naming_no_secret_is_the_controllers_default():
    [source] = svc.ingress_sources(ingress(tls=({"hosts": ["shop.example.com"]},)))
    assert source["source"] == svc.SOURCE_ROUTER_DEFAULT


def test_a_gateway_reports_one_entry_per_certificate_reference():
    sources = svc.gateway_sources(gateway(listeners=(
        {"name": "https", "hostname": "shop.example.com", "tls": {
            "mode": "Terminate",
            "certificateRefs": [{"name": "shop-tls"}, {"name": "legacy-tls"}],
        }},
    )))
    assert [s["secret_name"] for s in sources] == ["shop-tls", "legacy-tls"]
    assert all(s["hosts"] == ["shop.example.com"] for s in sources)


def test_a_gateway_listener_with_no_tls_is_not_reported_at_all():
    assert svc.gateway_sources(gateway(listeners=({"name": "http"},))) == []


def test_a_passthrough_listener_points_at_the_backend():
    [source] = svc.gateway_sources(gateway(listeners=(
        {"name": "tcp", "tls": {"mode": "Passthrough"}},
    )))
    assert source["source"] == svc.SOURCE_BACKEND


def test_a_terminating_listener_with_no_refs_is_the_implementations_default():
    [source] = svc.gateway_sources(gateway(listeners=(
        {"name": "https", "tls": {"mode": "Terminate"}},
    )))
    assert source["source"] == svc.SOURCE_ROUTER_DEFAULT


def test_a_cross_namespace_reference_is_read_where_it_actually_points():
    # Looking in the Gateway's own namespace would report a certificate that
    # exists and works as missing.
    [source] = svc.gateway_sources(gateway(listeners=(
        {"name": "https", "tls": {
            "mode": "Terminate",
            "certificateRefs": [{"name": "shared-tls", "namespace": "certs"}],
        }},
    )))
    assert (source["secret_namespace"], source["secret_name"]) == ("certs", "shared-tls")


# --------------------------------------------------------------------------- #
# Reading the Secret — one key, by name, and never the other one
# --------------------------------------------------------------------------- #

def test_a_secret_of_the_wrong_type_is_refused_by_type_and_not_searched():
    pem, error = svc.certificate_from_secret(
        tls_secret(make_pem(), secret_type="Opaque")
    )
    assert pem is None
    assert "not kubernetes.io/tls" in error


def test_a_tls_secret_with_no_certificate_key_says_which_key_is_missing():
    pem, error = svc.certificate_from_secret(tls_secret(None))
    assert pem is None
    assert svc.CERTIFICATE_KEY in error


def test_a_certificate_that_is_not_base64_is_a_reason_not_a_crash():
    secret = tls_secret(make_pem())
    secret["data"][svc.CERTIFICATE_KEY] = "!!!not base64!!!"
    pem, error = svc.certificate_from_secret(secret)
    assert pem is None
    assert "base64" in error


def test_the_private_key_beside_the_certificate_is_never_looked_up():
    """The guarantee, asserted mechanically rather than by reading the code."""
    class Watching(dict):
        def __init__(self, *args):
            super().__init__(*args)
            self.seen = []

        def get(self, key, default=None):
            self.seen.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.seen.append(key)
            return super().__getitem__(key)

    secret = tls_secret(make_pem())
    watching = Watching(secret["data"])
    secret["data"] = watching
    svc.certificate_from_secret(secret)
    assert "tls.key" not in watching.seen


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


_DISCOVERY = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {"resources": [
        {"name": "secrets", "kind": "Secret", "namespaced": True,
         "verbs": ["get", "list", "watch"]},
    ]},
    "/apis/networking.k8s.io/v1": {"resources": [
        {"name": "ingresses", "kind": "Ingress", "namespaced": True,
         "verbs": ["get", "list", "watch"]},
    ]},
    "/apis/route.openshift.io/v1": {"resources": [
        {"name": "routes", "kind": "Route", "namespaced": True,
         "verbs": ["get", "list", "watch"]},
    ]},
    "/apis/gateway.networking.k8s.io/v1": {"resources": [
        {"name": "gateways", "kind": "Gateway", "namespaced": True,
         "verbs": ["get", "list", "watch"]},
    ]},
}


def stub_cluster(monkeypatch, *, groups=None, listings=None, secrets=None, failures=None):
    """A cluster serving the three kinds, with the listings a test cares about.

    Unstubbed reads raise, for the same reason ``FakeApi`` does: a permissive
    stub makes a swallowed failure indistinguishable from a smaller cluster.
    """
    groups = [
        ("networking.k8s.io", "v1"),
        ("route.openshift.io", "v1"),
        ("gateway.networking.k8s.io", "v1"),
    ] if groups is None else groups
    listings = listings or {}
    secrets = secrets or {}
    failures = failures or {}

    def fake_raw_get(path, *, query=None):
        if path == "/apis":
            return {"groups": [
                {"name": name,
                 "preferredVersion": {"groupVersion": f"{name}/{v}", "version": v},
                 "versions": [{"groupVersion": f"{name}/{v}", "version": v}]}
                for name, v in groups
            ]}
        if path in failures:
            raise failures[path]
        if path in _DISCOVERY:
            return _DISCOVERY[path]
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)

    reads: list[tuple[str, str]] = []

    def fake_list(group, version, plural, **kwargs):
        if plural in failures:
            raise failures[plural]
        items = listings.get(plural, [])
        return {
            "items": items, "continue": listings.get(f"{plural}:continue"),
            "remaining": listings.get(f"{plural}:remaining"), "partial": False,
            "unavailable": [],
        }

    def fake_read(group, version, plural, name, *, namespace=None, subresource=None):
        assert plural == "secrets", f"unexpected read of {plural}"
        reads.append((namespace, name))
        key = (namespace, name)
        if key in failures:
            raise failures[key]
        if key not in secrets:
            raise ApiException(status=404, reason="Not Found")
        return secrets[key]

    monkeypatch.setattr(svc.reader, "list_resource", fake_list)
    monkeypatch.setattr(svc.reader, "read_object", fake_read)
    return reads


def find(report, source_id):
    return next(item for item in report["items"] if item["id"] == source_id)


def test_an_expiring_certificate_is_named_with_its_date_and_its_state(monkeypatch, fake_k8s):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=(
            {"hosts": ["shop.example.com"], "secretName": "shop-tls"},
        ))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(make_pem(valid_to_days=9))},
    )
    report = svc.certificate_report(namespace=NAMESPACE)

    row = find(report, f"ingress/{NAMESPACE}/checkout#0")
    assert row["state"] == svc.STATE_EXPIRING
    assert 0 < row["expires_in_seconds"] <= svc.EXPIRING_WINDOW_SECONDS
    assert [f["code"] for f in row["findings"]][0] == svc.FINDING_EXPIRING
    assert report["partial"] is False


def test_a_certificate_for_the_wrong_hostname_is_reported_however_fresh_it_is(
    monkeypatch, fake_k8s,
):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(rules=("new.example.com",), tls=(
            {"secretName": "shop-tls"},
        ))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(
            make_pem(dns_names=("old.example.com",))
        )},
    )
    row = find(svc.certificate_report(), f"ingress/{NAMESPACE}/checkout#0")

    assert row["state"] == svc.STATE_VALID
    assert row["hostsCovered"] == [{"host": "new.example.com", "covered": False}]
    codes = [f["code"] for f in row["findings"]]
    assert svc.FINDING_HOST_NOT_COVERED in codes


def test_a_wildcard_certificate_covers_the_subdomain_and_not_the_apex(monkeypatch, fake_k8s):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(
            rules=("example.com", "shop.example.com"), tls=({"secretName": "wild-tls"},),
        )]},
        secrets={(NAMESPACE, "wild-tls"): tls_secret(
            make_pem(dns_names=("*.example.com",))
        )},
    )
    row = find(svc.certificate_report(), f"ingress/{NAMESPACE}/checkout#0")
    assert row["hostsCovered"] == [
        {"host": "example.com", "covered": False},
        {"host": "shop.example.com", "covered": True},
    ]


def test_an_unreadable_secret_is_unknown_and_never_an_exposure_with_no_certificate(
    monkeypatch, fake_k8s,
):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        failures={(NAMESPACE, "shop-tls"): ApiException(status=403, reason="Forbidden")},
    )
    report = svc.certificate_report()
    row = find(report, f"ingress/{NAMESPACE}/checkout#0")

    assert row["certificate"] is None
    assert row["state"] == svc.STATE_UNKNOWN
    assert row["expires_in_seconds"] is None
    assert row["findings"][0]["code"] == svc.FINDING_UNREADABLE
    assert "no certificate" in row["findings"][0]["detail"]
    # The read that failed names itself rather than vanishing.
    assert report["partial"] is True
    assert [e["resource"] for e in report["unavailable"]] == ["secrets"]


def test_coverage_is_undecided_rather_than_false_when_the_certificate_is_unread(
    monkeypatch, fake_k8s,
):
    # `covered: false` is a claim about a certificate, and there is no
    # certificate here to make it with.
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        failures={(NAMESPACE, "shop-tls"): ApiException(status=403, reason="Forbidden")},
    )
    row = find(svc.certificate_report(), f"ingress/{NAMESPACE}/checkout#0")
    assert row["hostsCovered"] == [{"host": "shop.example.com", "covered": None}]


def test_one_secret_shared_by_many_exposures_is_read_once(monkeypatch, fake_k8s):
    reads = stub_cluster(
        monkeypatch,
        listings={"ingresses": [
            ingress(name="a", tls=({"secretName": "wild-tls"},)),
            ingress(name="b", tls=({"secretName": "wild-tls"},)),
            ingress(name="c", tls=({"secretName": "wild-tls"},)),
        ]},
        secrets={(NAMESPACE, "wild-tls"): tls_secret(make_pem(dns_names=("*.example.com",)))},
    )
    report = svc.certificate_report()
    assert len(report["items"]) == 3
    assert reads == [(NAMESPACE, "wild-tls")]
    # And every row agrees about it, which is the other half of caching it.
    assert len({row["certificate"]["serial"] for row in report["items"]}) == 1


def test_a_source_past_the_read_budget_says_so_rather_than_looking_examined(
    monkeypatch, fake_k8s,
):
    over = svc.MAX_CERTIFICATE_READS + 2
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [
            ingress(name=f"i{n}", tls=({"secretName": f"tls-{n}"},)) for n in range(over)
        ]},
        secrets={
            (NAMESPACE, f"tls-{n}"): tls_secret(make_pem()) for n in range(over)
        },
    )
    report = svc.certificate_report()
    last = find(report, f"ingress/{NAMESPACE}/i{over - 1}#0")

    assert last["certificate"] is None
    assert last["state"] == svc.STATE_UNKNOWN
    assert last["findings"][0]["code"] == svc.FINDING_NOT_READ
    assert report["maxCertificateReads"] == svc.MAX_CERTIFICATE_READS


def test_a_passthrough_route_is_reported_as_holding_its_certificate_elsewhere(
    monkeypatch, fake_k8s,
):
    stub_cluster(
        monkeypatch,
        listings={"routes": [route(tls={"termination": "passthrough"})]},
    )
    row = find(svc.certificate_report(), f"route/{NAMESPACE}/checkout#tls")
    assert row["source"] == svc.SOURCE_BACKEND
    assert [f["code"] for f in row["findings"]] == [svc.FINDING_PASSTHROUGH]


def test_an_exposure_naming_no_certificate_is_the_routers_default_not_a_fault(
    monkeypatch, fake_k8s,
):
    stub_cluster(monkeypatch, listings={"routes": [route(tls={"termination": "edge"})]})
    row = find(svc.certificate_report(), f"route/{NAMESPACE}/checkout#tls")
    assert [f["code"] for f in row["findings"]] == [svc.FINDING_ROUTER_DEFAULT]
    assert row["certificate"] is None


def test_an_inline_route_certificate_is_decoded_without_reading_any_secret(
    monkeypatch, fake_k8s,
):
    reads = stub_cluster(
        monkeypatch,
        listings={"routes": [route(tls={
            "termination": "edge",
            "certificate": make_pem(valid_to_days=200),
            "key": "-----BEGIN PRIVATE KEY-----\nnope\n",
        })]},
    )
    row = find(svc.certificate_report(), f"route/{NAMESPACE}/checkout#tls")
    assert row["source"] == svc.SOURCE_INLINE
    assert row["state"] == svc.STATE_VALID
    assert reads == []


def test_a_leaf_only_bundle_from_a_real_issuer_is_reported_as_such(monkeypatch, fake_k8s):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(
            make_pem(issuer_common_name="Example CA")
        )},
    )
    row = find(svc.certificate_report(), f"ingress/{NAMESPACE}/checkout#0")
    assert svc.FINDING_CHAIN_LEAF_ONLY in [f["code"] for f in row["findings"]]
    assert svc.FINDING_SELF_SIGNED not in [f["code"] for f in row["findings"]]


def test_a_certificate_with_no_names_says_it_authenticates_no_hostname(
    monkeypatch, fake_k8s,
):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(make_pem(dns_names=None))},
    )
    row = find(svc.certificate_report(), f"ingress/{NAMESPACE}/checkout#0")
    codes = [f["code"] for f in row["findings"]]
    assert svc.FINDING_NO_SANS in codes
    # And not also a host-mismatch: one sentence about a certificate that names
    # nothing, not one per host it fails to name.
    assert svc.FINDING_HOST_NOT_COVERED not in codes


def test_a_kind_this_cluster_does_not_serve_is_named_without_making_it_partial(
    monkeypatch, fake_k8s,
):
    # An `unsupported` API is an ordinary fact, and rendering it red trains
    # people to ignore red.
    stub_cluster(
        monkeypatch,
        groups=[("networking.k8s.io", "v1")],
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(make_pem())},
    )
    report = svc.certificate_report()
    states = {k["kind"]: k["state"] for k in report["kinds"]}

    assert states == {
        "Route": "unsupported", "Ingress": "available", "Gateway": "unsupported",
    }
    assert report["partial"] is False


def test_a_kind_discovery_could_not_answer_for_does_make_it_partial(monkeypatch, fake_k8s):
    stub_cluster(
        monkeypatch,
        failures={"/apis/route.openshift.io/v1": ApiException(status=503, reason="unavailable")},
        listings={"ingresses": []},
    )
    report = svc.certificate_report()
    states = {k["kind"]: k["state"] for k in report["kinds"]}

    assert states["Route"] == "unknown"
    assert report["partial"] is True
    assert "route.openshift.io" in {e["group"] for e in report["unavailable"]}


def test_one_listing_failing_costs_that_kind_and_not_the_others(monkeypatch, fake_k8s):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(make_pem())},
        failures={"routes": ApiException(status=403, reason="Forbidden")},
    )
    report = svc.certificate_report()

    assert len(report["items"]) == 1
    assert report["partial"] is True
    assert "routes" in {e["resource"] for e in report["unavailable"]}


def test_a_truncated_listing_is_reported_rather_than_paged_over(monkeypatch, fake_k8s):
    stub_cluster(monkeypatch, listings={
        "ingresses": [], "ingresses:continue": "next-page", "ingresses:remaining": 40,
    })
    report = svc.certificate_report()
    assert report["truncated"] == [{"kind": "Ingress", "shown": 0, "remaining": 40}]


def test_no_certificate_body_and_no_private_key_ever_reaches_the_response(
    monkeypatch, fake_k8s,
):
    """The report is about dates and names. It ships neither PEM."""
    pem = make_pem()
    stub_cluster(
        monkeypatch,
        listings={
            "ingresses": [ingress(tls=({"secretName": "shop-tls"},))],
            "routes": [route(tls={
                "termination": "edge", "certificate": pem,
                "key": "-----BEGIN PRIVATE KEY-----\nnope\n",
            })],
        },
        secrets={(NAMESPACE, "shop-tls"): tls_secret(pem)},
    )
    import json

    body = json.dumps(svc.certificate_report())
    assert "BEGIN CERTIFICATE" not in body
    assert "BEGIN PRIVATE KEY" not in body
    assert "tls.key" not in body


def test_an_empty_cluster_reports_no_certificates_and_says_it_could_look(
    monkeypatch, fake_k8s,
):
    stub_cluster(monkeypatch, listings={"routes": [], "ingresses": [], "gateways": []})
    report = svc.certificate_report()

    assert report["items"] == []
    assert report["partial"] is False
    assert all(k["state"] == "available" for k in report["kinds"])


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def test_the_endpoint_returns_the_envelope_and_its_own_thresholds(
    client, cluster_id, monkeypatch, fake_k8s,
):
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(make_pem(valid_to_days=5))},
    )

    response = client.get(
        "/api/routes/certificates",
        params={"cluster_id": cluster_id, "namespace": NAMESPACE},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["state"] == svc.STATE_EXPIRING
    # The window is data, so the UI does not carry a second copy of "30 days"
    # that can drift from the one the states were computed with.
    assert body["expiringWindowSeconds"] == svc.EXPIRING_WINDOW_SECONDS
    assert {k["kind"] for k in body["kinds"]} == {"Route", "Ingress", "Gateway"}


def test_the_endpoint_does_not_collide_with_the_single_exposure_path(
    client, cluster_id, monkeypatch, fake_k8s,
):
    # `/routes/certificates` has one segment after `routes`; the single-exposure
    # path has three. A collision would make this endpoint answer as a backend
    # named "certificates", which is a 422 and looks like a client bug.
    stub_cluster(monkeypatch, listings={"routes": [], "ingresses": [], "gateways": []})

    assert client.get(
        "/api/routes/certificates", params={"cluster_id": cluster_id},
    ).status_code == 200
    assert client.get(
        "/api/routes/certificates/prod/checkout", params={"cluster_id": cluster_id},
    ).status_code == 422


# --------------------------------------------------------------------------- #
# Boundaries the first pass of these tests walked past. Each was found by
# mutating the module and watching the suite stay green.
# --------------------------------------------------------------------------- #

def test_a_certificate_naming_a_domain_does_not_cover_its_subdomains():
    # Only a wildcard covers a child. A certificate for `example.com` presented
    # for `shop.example.com` is a name mismatch, and calling it covered would be
    # a green row in front of exactly that error.
    assert svc.host_matches("shop.example.com", "example.com") is False
    assert svc.host_matches("example.com", "example.com") is True


def test_a_second_wildcard_in_a_name_is_not_honoured():
    # A malformed hostname meeting a malformed certificate. The API server
    # rejects a `*` outside the first label, so reaching this means an object
    # nothing validated — which is when a console should be strict, not clever.
    assert svc.host_matches("com.b.*", "*.b.*") is False


def test_an_expiry_falling_exactly_now_is_expired_and_not_expiring(monkeypatch):
    # The boundary the buckets are built on. A certificate whose validity ends
    # this second is over, and reporting it as "expires soon" gives an outage
    # that has already happened a future deadline.
    monkeypatch.setattr(svc.shaping, "seconds_until", lambda value: 0)
    state, remaining = svc.expiry_state(shaping.decode_certificate_chain(make_pem()))
    assert (state, remaining) == (svc.STATE_EXPIRED, 0)


def test_a_self_signed_certificate_is_not_also_called_leaf_only(monkeypatch, fake_k8s):
    # It is one certificate by design and its issuer is itself. "No intermediate
    # is included" is both noise and wrong about which certificate is missing.
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): tls_secret(make_pem())},
    )
    row = find(svc.certificate_report(), f"ingress/{NAMESPACE}/checkout#0")
    codes = [f["code"] for f in row["findings"]]

    assert svc.FINDING_SELF_SIGNED in codes
    assert svc.FINDING_CHAIN_LEAF_ONLY not in codes


def test_a_secret_that_reads_but_will_not_decode_is_unknown_not_a_certificate(
    monkeypatch, fake_k8s,
):
    # Distinct from a Secret that could not be read: this one answered, and what
    # it holds is not a certificate. Returning the blank decode as though it
    # were one would put a row with no dates and no names on the page looking
    # like a certificate that has neither.
    secret = tls_secret(make_pem())
    secret["data"][svc.CERTIFICATE_KEY] = base64.b64encode(b"not a pem").decode()
    stub_cluster(
        monkeypatch,
        listings={"ingresses": [ingress(tls=({"secretName": "shop-tls"},))]},
        secrets={(NAMESPACE, "shop-tls"): secret},
    )
    report = svc.certificate_report()
    row = find(report, f"ingress/{NAMESPACE}/checkout#0")

    assert row["certificate"] is None
    assert row["state"] == svc.STATE_UNKNOWN
    assert row["expires_in_seconds"] is None
    assert row["hostsCovered"] == [{"host": "shop.example.com", "covered": None}]
    assert row["findings"][0]["code"] == svc.FINDING_UNREADABLE
    assert "PEM certificate bundle" in row["findings"][0]["detail"]
    # The Secret answered, so nothing failed to read: the report is not partial.
    assert report["partial"] is False


def test_two_exposures_sharing_an_unreadable_secret_get_the_same_sentence(
    monkeypatch, fake_k8s,
):
    # The read failed once and is cached. The second row must say what the first
    # says — a vaguer sentence on the second would read as a different problem.
    reads = stub_cluster(
        monkeypatch,
        listings={"ingresses": [
            ingress(name="a", tls=({"secretName": "shared-tls"},)),
            ingress(name="b", tls=({"secretName": "shared-tls"},)),
        ]},
        failures={(NAMESPACE, "shared-tls"): ApiException(status=403, reason="Forbidden")},
    )
    report = svc.certificate_report()
    first = find(report, f"ingress/{NAMESPACE}/a#0")
    second = find(report, f"ingress/{NAMESPACE}/b#0")

    assert reads == [(NAMESPACE, "shared-tls")]
    assert first["findings"] == second["findings"]
    assert "shared-tls" in second["findings"][0]["detail"]


def test_an_inline_certificate_that_will_not_decode_carries_the_decoders_reason(
    monkeypatch, fake_k8s,
):
    # No Secret is involved, so there is no failed read to name: the sentence
    # has to come from the decoder, and the row still has to be `unknown`.
    stub_cluster(
        monkeypatch,
        listings={"routes": [route(tls={
            "termination": "edge", "certificate": "-----BEGIN CERTIFICATE-----\nzz\n",
        })]},
    )
    report = svc.certificate_report()
    row = find(report, f"route/{NAMESPACE}/checkout#tls")

    assert row["source"] == svc.SOURCE_INLINE
    assert row["certificate"] is None
    assert row["state"] == svc.STATE_UNKNOWN
    assert row["findings"][0]["code"] == svc.FINDING_UNREADABLE
    assert "Not a PEM certificate bundle" in row["findings"][0]["detail"]
    # Nothing failed to *read* — the Route listing answered.
    assert report["partial"] is False


def test_a_discovery_failure_that_says_nothing_about_reading_is_propagated(
    monkeypatch, fake_k8s,
):
    """§13's rule, shared rather than reimplemented.

    A malformed request is not an unavailable backend, and the token anybody
    inventing one reaches for first is `unreachable` — which sends an operator
    to check a network that answered fine. So it raises instead.
    """
    from app.errors import Invalid

    stub_cluster(
        monkeypatch,
        failures={"/apis/route.openshift.io/v1": Invalid("that group name is not usable")},
        listings={"ingresses": []},
    )
    with pytest.raises(Invalid):
        svc.certificate_report()
