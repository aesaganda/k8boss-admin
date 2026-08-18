/**
 * Network — Services, Ingresses and Endpoints, over the generic §4 API.
 *
 * The column that earns this page is `endpoint_count`. §8: *`endpoint_count` is
 * `null` if EndpointSlices could not be read.* A Service with no backends and a
 * Service whose backends we failed to look up are the two states an operator
 * most needs to tell apart when something is returning 503, so one renders as
 * `0` and the other as an em dash — never as each other.
 *
 * Endpoints are listed raw, because §8 defines no typed row for them and
 * inventing one here would put a second, quietly different shaping of the same
 * object in the frontend. Their `subsets` split ready from not-ready addresses,
 * which is the thing you actually want to see when a Service is black-holing:
 * an endpoint object with addresses only under `notReadyAddresses` is a Service
 * whose pods exist and are failing their readiness probe.
 */
import { useMemo } from 'react';
import {
  AgeCell,
  DescriptionList,
  NullableCell,
  ResourceLink,
  StatusBadge,
} from '../components/ui';
import { useCluster } from '../contexts/ClusterContext';
import { objectAgeSeconds, objectName, objectNamespace } from './_data';
import { ChipList, Muted, NoClusterState, ResourceTabsPage, YamlPanel } from './_parts';

/** Ready / not-ready address tallies from a core/v1 Endpoints object. */
function endpointTallies(row) {
  const subsets = row?.subsets ?? [];
  let ready = 0;
  let notReady = 0;
  const ports = new Set();
  for (const subset of subsets) {
    ready += (subset.addresses ?? []).length;
    notReady += (subset.notReadyAddresses ?? []).length;
    for (const port of subset.ports ?? []) ports.add(`${port.port}/${port.protocol ?? 'TCP'}`);
  }
  return { ready, notReady, ports: [...ports] };
}

const NAMESPACE_COLUMN = { key: 'namespace', title: 'Namespace', sortable: true };

export default function Network() {
  const { activeClusterId } = useCluster();

  const tabs = useMemo(
    () => [
      {
        key: 'services',
        title: 'Services',
        group: 'core',
        version: 'v1',
        plural: 'services',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'The listing succeeded and returned no Services in this scope.',
        detailTitle: (row) => `Service ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          NAMESPACE_COLUMN,
          { key: 'type', title: 'Type', sortable: true },
          {
            key: 'clusterIP',
            title: 'Cluster IP',
            cell: (row) => (
              <NullableCell
                value={row.clusterIP}
                reason="A headless Service (clusterIP: None) has no cluster IP; a Service still being assigned one has not got there yet."
              />
            ),
          },
          {
            key: 'externalIPs',
            title: 'External',
            value: (row) => (row.externalIPs ?? []).join(' '),
            cell: (row) => <ChipList values={row.externalIPs} max={2} emptyText="none" />,
          },
          {
            key: 'ports',
            title: 'Ports',
            value: (row) => (row.ports ?? []).map((p) => p.port).join(','),
            cell: (row) => (
              <ChipList
                values={(row.ports ?? []).map(
                  (p) =>
                    `${p.port}${p.targetPort != null ? `→${p.targetPort}` : ''}/${p.protocol ?? 'TCP'}` +
                    (p.nodePort ? ` (node ${p.nodePort})` : ''),
                )}
                max={2}
                emptyText="none"
              />
            ),
          },
          {
            key: 'endpoint_count',
            title: 'Endpoints',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.endpoint_count}
                reason="The EndpointSlices for this Service could not be read, so how many backends it has is unknown — not zero."
              />
            ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                { label: 'Type', value: row.type },
                { label: 'Cluster IP', value: row.clusterIP ?? null },
                {
                  label: 'Backends',
                  value: (
                    <NullableCell
                      value={row.endpoint_count}
                      reason="The EndpointSlices could not be read, so this is unknown rather than zero."
                    />
                  ),
                },
                {
                  label: 'Selector',
                  value: (
                    <ChipList
                      values={Object.entries(row.selector ?? {}).map(([k, v]) => `${k}=${v}`)}
                      max={5}
                      emptyText="none — this Service is backed by hand-managed EndpointSlices"
                    />
                  ),
                },
                {
                  label: 'External addresses',
                  value: <ChipList values={row.externalIPs} max={5} emptyText="none" />,
                },
              ]}
            />
            <YamlPanel group="core" version="v1" plural="services" name={row.name} namespace={row.namespace} height={320} />
          </>
        ),
      },

      {
        key: 'ingresses',
        title: 'Ingresses',
        group: 'networking.k8s.io',
        version: 'v1',
        plural: 'ingresses',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription:
          'No Ingresses in this scope. A cluster with no ingress controller serves the API but has nothing to list.',
        detailTitle: (row) => `Ingress ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          NAMESPACE_COLUMN,
          {
            key: 'class',
            title: 'Class',
            cell: (row) => (
              <NullableCell
                value={row.class}
                reason="Neither spec.ingressClassName nor the legacy kubernetes.io/ingress.class annotation is set, so which controller serves this is decided by the cluster default."
              />
            ),
          },
          {
            key: 'hosts',
            title: 'Hosts',
            value: (row) => (row.rules ?? []).map((rule) => rule.host).join(' '),
            cell: (row) => (
              <ChipList
                values={(row.rules ?? []).map((rule) => rule.host || '*')}
                max={2}
                emptyText="none"
              />
            ),
          },
          {
            key: 'tls_hosts',
            title: 'TLS',
            value: (row) => (row.tls_hosts ?? []).length,
            cell: (row) =>
              (row.tls_hosts ?? []).length ? (
                <ChipList values={row.tls_hosts} max={1} color="green" />
              ) : (
                <Muted>none</Muted>
              ),
          },
          {
            key: 'address',
            title: 'Address',
            cell: (row) => (
              <NullableCell
                value={row.address}
                reason="The ingress controller has not published an address for this Ingress yet."
              />
            ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                { label: 'Class', value: row.class ?? null },
                { label: 'Address', value: row.address ?? null },
                { label: 'TLS hosts', value: <ChipList values={row.tls_hosts} max={6} emptyText="none" /> },
              ]}
            />
            {(row.rules ?? []).map((rule, index) => (
              <div key={`${rule.host ?? '*'}-${index}`} style={{ marginTop: '0.75rem' }}>
                <strong>{rule.host || 'any host'}</strong>
                <ul>
                  {(rule.paths ?? []).map((path, i) => (
                    <li key={`${path.path ?? '/'}-${i}`}>
                      <code>{path.path || '/'}</code> <Muted>({path.pathType || 'ImplementationSpecific'})</Muted> →{' '}
                      {path.service ? (
                        <ResourceLink
                          group=""
                          version="v1"
                          plural="services"
                          name={path.service}
                          namespace={row.namespace}
                        >
                          {`${path.service}:${path.port ?? '?'}`}
                        </ResourceLink>
                      ) : (
                        <NullableCell value={null} reason="This path has no Service backend; it routes to a resource reference." />
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            <YamlPanel
              group="networking.k8s.io"
              version="v1"
              plural="ingresses"
              name={row.name}
              namespace={row.namespace}
              height={320}
            />
          </>
        ),
      },

      {
        key: 'endpoints',
        title: 'Endpoints',
        group: 'core',
        version: 'v1',
        plural: 'endpoints',
        namespaced: true,
        // Raw manifests: §8 defines no typed Endpoints row, and `shape: raw`
        // says so out loud rather than relying on the absence of a shaper.
        shape: 'raw',
        rowKey: (row) => `${objectNamespace(row)}/${objectName(row)}`,
        emptyDescription: 'No Endpoints objects in this scope.',
        detailTitle: (row) => `Endpoints ${objectNamespace(row)}/${objectName(row)}`,
        columns: [
          {
            key: 'name',
            title: 'Name',
            sortable: true,
            value: (row) => objectName(row),
            cell: (row) => (
              // An Endpoints object always shares its name with its Service, so
              // linking there is the useful move: "who owns these addresses".
              <ResourceLink
                group=""
                version="v1"
                plural="services"
                name={objectName(row)}
                namespace={objectNamespace(row)}
              />
            ),
          },
          { key: 'namespace', title: 'Namespace', sortable: true, value: (row) => objectNamespace(row) },
          {
            key: 'ready',
            title: 'Ready addresses',
            sortable: true,
            value: (row) => endpointTallies(row).ready,
            cell: (row) => {
              const { ready } = endpointTallies(row);
              return <StatusBadge status={ready ? 'Ready' : 'NotReady'} label={String(ready)} tooltip={ready ? undefined : 'This Service has no ready backends: traffic to it is being dropped.'} />;
            },
          },
          {
            key: 'notReady',
            title: 'Not ready',
            sortable: true,
            value: (row) => endpointTallies(row).notReady,
            cell: (row) => {
              const { notReady } = endpointTallies(row);
              return notReady ? (
                <StatusBadge
                  status="Warning"
                  label={String(notReady)}
                  tooltip="These pods exist but are failing their readiness probe, so the Service will not route to them."
                />
              ) : (
                <Muted>0</Muted>
              );
            },
          },
          {
            key: 'ports',
            title: 'Ports',
            value: (row) => endpointTallies(row).ports.join(','),
            cell: (row) => <ChipList values={endpointTallies(row).ports} max={3} emptyText="none" />,
          },
          {
            key: 'age',
            title: 'Age',
            sortable: true,
            value: (row) => objectAgeSeconds(row),
            cell: (row) => <AgeCell seconds={objectAgeSeconds(row)} timestamp={row?.metadata?.creationTimestamp} />,
          },
        ],
        detail: (row) => (
          <YamlPanel
            group="core"
            version="v1"
            plural="endpoints"
            name={objectName(row)}
            namespace={objectNamespace(row)}
            height={420}
          />
        ),
      },
    ],
    [],
  );

  if (activeClusterId == null) {
    return <NoClusterState what="Networking" />;
  }

  return (
    <ResourceTabsPage
      title="Networking"
      subtitle="Services, Ingresses and the addresses behind them. This console reads no service mesh — what is here is what the API server serves."
      tabs={tabs}
    />
  );
}
