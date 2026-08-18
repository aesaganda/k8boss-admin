/**
 * Pods — every pod in scope, through the generic §4 listing.
 *
 * There is no typed `/api/pods` endpoint and there does not need to be: §8's
 * shaping layer registers `pod_row` for `core/v1/pods`, so the generic listing
 * already returns the §6 PodRow with `phase_detail` on it.
 *
 * `phase_detail` is the reason this page is worth having. §6: *`phase` is the
 * raw Kubernetes phase … the backend additionally supplies `phase_detail` for
 * the cases where phase lies — a `Running` pod with a `CrashLoopBackOff`
 * container gets `phase_detail: "CrashLoopBackOff"`. A pod being deleted gets
 * `"Terminating"`. Reporting a CrashLooping pod as `Running` is a confident
 * wrong answer.* The Status column therefore renders the detail over the phase,
 * and the filter below matches on the detail too — filtering to "Running" and
 * being shown a crash-looping pod would reintroduce the lie one layer up.
 */
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useGates, useResourceList } from './_data';
import {
  ImagesCell,
  Muted,
  NoClusterState,
  PickList,
  PodConsoleModal,
  TruncationFooter,
  menuAction,
} from './_parts';

const CHECKS = [
  { id: 'logs', verb: 'get', group: 'core', resource: 'pods', subresource: 'log' },
  { id: 'exec', verb: 'create', group: 'core', resource: 'pods', subresource: 'exec' },
];

const PHASES = [
  { value: 'Running', label: 'Running' },
  { value: 'Pending', label: 'Pending' },
  { value: 'Failed', label: 'Failed' },
  { value: 'Succeeded', label: 'Succeeded' },
  { value: 'Unhealthy', label: 'Not healthy (any reason)' },
];

/** What the Status pill actually says, which is what a filter must match. */
function displayState(row) {
  return row.phase_detail || row.phase || 'Unknown';
}

export default function Pods() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const navigate = useNavigate();

  const [search, setSearch] = useState('');
  const [phase, setPhase] = useState(null);
  const [podConsole, setPodConsole] = useState(null);

  const listing = useResourceList('core', 'v1', 'pods', {
    namespace,
    enabled: activeClusterId != null,
  });

  const { gate } = useGates(
    CHECKS.map((check) => ({ ...check, namespace })),
    { enabled: activeClusterId != null },
  );

  const rows = useMemo(() => {
    const items = listing.items ?? [];
    if (!phase) return items;
    if (phase === 'Unhealthy') {
      // Anything whose displayed state is not one of the two settled ones. A
      // CrashLoopBackOff pod is `phase: Running`, so this cannot be written
      // against `phase` — that is the whole point of `phase_detail`.
      return items.filter((row) => !['Running', 'Succeeded'].includes(displayState(row)));
    }
    return items.filter((row) => displayState(row) === phase);
  }, [listing.items, phase]);

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Name',
        sortable: true,
        cell: (row) => (
          <ResourceLink group="" version="v1" plural="pods" name={row.name} namespace={row.namespace} />
        ),
      },
      { key: 'namespace', title: 'Namespace', sortable: true },
      {
        key: 'phase',
        title: 'Status',
        sortable: true,
        value: (row) => displayState(row),
        cell: (row) => (
          <StatusBadge
            status={row.phase}
            detail={row.phase_detail}
            tooltip={
              row.phase_detail && row.phase_detail !== row.phase
                ? `The pod's phase is "${row.phase}", but its containers say "${row.phase_detail}". The container state is what is shown.`
                : undefined
            }
          />
        ),
      },
      {
        key: 'ready',
        title: 'Ready',
        sortable: true,
        // `2/2` is a string, so it sorts as one. That is deliberate: sorting on
        // the ratio would put 0/1 and 0/8 next to each other, and the second is
        // the more urgent by a long way.
        cell: (row) => <span>{row.ready}</span>,
      },
      {
        key: 'restarts',
        title: 'Restarts',
        sortable: true,
        cell: (row) => <NullableCell value={row.restarts} />,
      },
      {
        key: 'node',
        title: 'Node',
        sortable: true,
        cell: (row) =>
          row.node ? (
            <ResourceLink kind="Node" name={row.node} />
          ) : (
            <NullableCell value={null} reason="This pod has not been scheduled to a node yet." />
          ),
      },
      { key: 'qos_class', title: 'QoS' },
      {
        key: 'containers',
        title: 'Images',
        value: (row) => (row.containers ?? []).map((c) => c.image).join(' '),
        cell: (row) => <ImagesCell images={(row.containers ?? []).map((c) => c.image)} max={1} />,
      },
      { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
    ],
    [],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Pods" />
        <NoClusterState what="The pod list" />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Pods"
        subtitle={
          listing.loading
            ? 'Reading the cluster…'
            : `${rows.length}${rows.length !== listing.items.length ? ` of ${listing.items.length}` : ''} in ${
                namespace ? `namespace ${namespace}` : 'all namespaces'
              }`
        }
      />

      <PartialBanner unavailable={listing.unavailable} />

      <Toolbar ariaLabel="Pod filters">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name, node, image…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <PickList
            id="pod-phase"
            label="State"
            value={phase}
            options={PHASES}
            onChange={setPhase}
            placeholder="Any state"
          />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>State matches what the pill shows, not the raw phase.</Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh pods" icon={<SyncAltIcon />} onClick={listing.reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Pods"
        columns={namespace ? columns.filter((column) => column.key !== 'namespace') : columns}
        rows={rows}
        rowKey={(row) => `${row.namespace}/${row.name}`}
        loading={listing.loading}
        error={listing.error}
        onRetry={listing.reload}
        filterText={search}
        onRowClick={(row) => setPodConsole({ pod: row, tab: 'logs' })}
        actions={(row) => [
          menuAction('View logs', gate('logs', { requiresWrite: false }), () =>
            setPodConsole({ pod: row, tab: 'logs' }),
          ),
          menuAction('Open terminal', gate('exec'), () => setPodConsole({ pod: row, tab: 'exec' })),
          { isSeparator: true },
          {
            title: 'Show its node',
            isDisabled: !row.node,
            onClick: () => row.node && navigate(`/nodes/${encodeURIComponent(row.node)}`),
          },
        ]}
        emptyTitle={phase ? `No pods in state "${phase}"` : 'No pods in scope'}
        emptyDescription={
          listing.partial
            ? 'Some namespaces could not be read — see the banner above. This table is not a complete answer.'
            : 'The listing succeeded and returned nothing for this scope.'
        }
        footer={<TruncationFooter listing={listing} noun="pods" />}
      />

      {podConsole && (
        <PodConsoleModal
          pod={podConsole.pod}
          initialTab={podConsole.tab}
          execGate={gate('exec')}
          onClose={() => setPodConsole(null)}
        />
      )}
    </>
  );
}
