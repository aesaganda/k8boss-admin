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
import { genericTab, NoClusterState, ResourceTabsPage } from './_parts';

const GROUP = 'gateway.networking.k8s.io';

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
      }),
      genericTab({
        key: 'grpcroutes',
        title: 'GRPC Routes',
        group: GROUP,
        version: 'v1',
        plural: 'grpcroutes',
        namespaced: true,
        resolveVersion: true,
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
