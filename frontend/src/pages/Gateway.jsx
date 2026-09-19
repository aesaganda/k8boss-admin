/**
 * Gateway — the Gateway API resources, over the generic §4 API.
 *
 * All six kinds here are CRDs, not core or built-in API-machinery types, so
 * every tab asks `resolveVersion: true` rather than trusting a hardcoded
 * version: which version a given kind serves (v1, v1beta1, v1alpha2, ...)
 * depends on which Gateway API release the cluster installed and when, and
 * guessing wrong would 404 on a cluster that has the resource under a
 * different version rather than genuinely lacking it. See `useResolvedVersion`
 * in `_parts.jsx`.
 *
 * These CRDs are also frequently just absent: a cluster with no Gateway API
 * installed serves none of these, and that is an ordinary, unremarkable state
 * — not an error — same as Ingress CRDs or `metrics.k8s.io` being missing
 * elsewhere in this console.
 */
import { useMemo } from 'react';
import { useCluster } from '../contexts/ClusterContext';
import { ChipList, genericTab, NoClusterState, ResourceTabsPage } from './_parts';

const GROUP = 'gateway.networking.k8s.io';

/**
 * The column an operator came here to click, on the three tabs that have a
 * hostname to offer — the same offer §13's Routes page makes, and with the
 * same reservation: **a link here is an invitation to try the exposure, never
 * a claim that it answers.** Whether a controller programmed the Gateway is
 * the object's own status, and whether the hostname resolves at all is DNS
 * this console has not read.
 *
 * Three things are deliberately NOT linked, because a link that cannot be
 * opened teaches people to stop trusting the ones that can:
 *
 * * **A wildcard hostname.** `*.example.com` is a pattern, not an address, and
 *   substituting a label into it would be inventing a hostname nobody wrote.
 *   It is still shown — as a plain chip — because it is what the object says.
 * * **A GRPCRoute's hostname.** gRPC is not a scheme a browser opens, so those
 *   hostnames are listed without an href rather than dressed up as an `http://`
 *   URL that would answer a browser with a protocol error.
 * * **A non-HTTP listener** (TCP, TLS, UDP): same reason, one layer down.
 *
 * GatewayClasses, ReferenceGrants and BackendTLSPolicies get no such column at
 * all. None of them carries a hostname — a ReferenceGrant is a permission and a
 * BackendTLSPolicy describes the hop *behind* the Gateway — and a column of
 * "None" on every row is a column that only costs width.
 */
const isWildcard = (host) => String(host).includes('*');

/** `:port` unless it is the one the scheme already implies. */
function portSuffix(scheme, port) {
  if (port == null) return '';
  if (scheme === 'http' && Number(port) === 80) return '';
  if (scheme === 'https' && Number(port) === 443) return '';
  return `:${port}`;
}

/**
 * One entry per HTTP/HTTPS listener: its own hostname when it names a concrete
 * one, and otherwise the Gateway's published addresses — which is where a
 * wildcard listener is actually reachable, and the only address a listener
 * without a hostname has. Empty when the Gateway has neither, which is the
 * true state of a Gateway no controller has given an address yet.
 */
function gatewayAddresses(row) {
  const published = (row?.status?.addresses ?? []).map((a) => a?.value).filter(Boolean);
  const out = [];
  for (const listener of row?.spec?.listeners ?? []) {
    const protocol = String(listener?.protocol ?? '').toUpperCase();
    if (protocol !== 'HTTP' && protocol !== 'HTTPS') continue;
    const scheme = protocol === 'HTTPS' ? 'https' : 'http';
    const suffix = portSuffix(scheme, listener?.port);
    const hostname = listener?.hostname;
    const hosts = hostname && !isWildcard(hostname) ? [hostname] : published;
    for (const host of hosts) out.push(`${scheme}://${host}${suffix}`);
  }
  return [...new Set(out)];
}

/**
 * An HTTPRoute's hostnames, carrying its first path so the link lands where
 * the route actually matches rather than on `/`.
 *
 * The scheme is `http`, and that is a limit rather than a reading: an HTTPRoute
 * has no TLS field — TLS belongs to the Gateway listener, a different object
 * with a different owner — so which of its parents' listeners a request should
 * use is not visible from here. §13's Routes page resolves it the same way for
 * the same reason; a route attached only to an HTTPS listener is the case where
 * an operator has to swap the scheme by hand.
 */
function httpRouteAddresses(row) {
  const rule = (row?.spec?.rules ?? [])[0];
  const path = ((rule?.matches ?? [])[0]?.path?.value) ?? '';
  return (row?.spec?.hostnames ?? []).map((host) =>
    isWildcard(host) ? String(host) : `http://${host}${path}`,
  );
}

/** A GRPCRoute's hostnames, as text. See the note above on why never a link. */
const grpcRouteAddresses = (row) => (row?.spec?.hostnames ?? []).map(String);

function addressColumn({ urls, emptyText, linked = true }) {
  return {
    key: 'address',
    title: 'Address it answers on',
    value: (row) => urls(row).join(' '),
    cell: (row) => (
      <ChipList
        values={urls(row)}
        emptyText={emptyText}
        // Only a full URL is offered as a link; a bare hostname chip (wildcard,
        // or gRPC) falls through to plain text, which is what `null` buys here.
        hrefFor={linked ? (value) => (value.startsWith('http') ? value : null) : undefined}
        max={2}
        // A URL breaks at neither a dot nor a slash, so one hostname would
        // otherwise be this column's minimum width. Same fix as §13's.
        breakAnywhere
      />
    ),
  };
}

export default function Gateway() {
  const { activeClusterId } = useCluster();

  const tabs = useMemo(
    () => [
      genericTab({
        key: 'gateways',
        title: 'Gateways',
        group: GROUP,
        version: 'v1',
        plural: 'gateways',
        namespaced: true,
        resolveVersion: true,
        extraColumns: [
          addressColumn({
            urls: gatewayAddresses,
            // Not "None": a Gateway whose listeners are all TCP/UDP has no URL
            // by design, and one still waiting for its controller has none yet.
            // Which of the two it is, the object's own status says.
            emptyText: 'No HTTP listener with an address',
          }),
        ],
      }),
      genericTab({
        key: 'gatewayclasses',
        title: 'Gateway Classes',
        group: GROUP,
        version: 'v1',
        plural: 'gatewayclasses',
        namespaced: false,
        resolveVersion: true,
      }),
      genericTab({
        key: 'httproutes',
        title: 'HTTP Routes',
        group: GROUP,
        version: 'v1',
        plural: 'httproutes',
        namespaced: true,
        resolveVersion: true,
        extraColumns: [
          addressColumn({
            urls: httpRouteAddresses,
            // Empty `spec.hostnames` is a real configuration, not a gap: the
            // route answers for every hostname its listener serves.
            emptyText: "Inherits its listener's hostnames",
          }),
        ],
      }),
      genericTab({
        key: 'grpcroutes',
        title: 'GRPC Routes',
        group: GROUP,
        version: 'v1',
        plural: 'grpcroutes',
        namespaced: true,
        resolveVersion: true,
        extraColumns: [
          addressColumn({
            urls: grpcRouteAddresses,
            linked: false,
            emptyText: "Inherits its listener's hostnames",
          }),
        ],
      }),
      genericTab({
        key: 'referencegrants',
        title: 'Reference Grants',
        group: GROUP,
        version: 'v1beta1',
        plural: 'referencegrants',
        namespaced: true,
        resolveVersion: true,
      }),
      genericTab({
        key: 'backendtlspolicies',
        title: 'Backend TLS Policies',
        group: GROUP,
        version: 'v1alpha3',
        plural: 'backendtlspolicies',
        namespaced: true,
        resolveVersion: true,
      }),

      // Deliberately no "Backend Traffic Policies" tab: `BackendTrafficPolicy`,
      // unlike `BackendTLSPolicy`, is not part of the upstream Gateway API
      // spec. It is shipped by individual Gateway API implementations under
      // their OWN api group (Envoy Gateway's is `gateway.envoyproxy.io`, not
      // `gateway.networking.k8s.io`), so which group is correct depends on
      // which implementation the target cluster runs. That has to be
      // confirmed against a real cluster before it's wired, not guessed here.
    ],
    [],
  );

  if (activeClusterId == null) {
    return <NoClusterState what="Gateway" />;
  }

  return (
    <ResourceTabsPage
      title="Gateway (beta)"
      subtitle="Gateway API resources — CRD-backed, and absent on clusters without Gateway API installed. That is an ordinary state, not an error."
      tabs={tabs}
    />
  );
}
