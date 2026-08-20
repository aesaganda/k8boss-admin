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
  DensityToggle,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import ImportYamlDialog from '../components/ImportYamlDialog';
import { useCluster } from '../contexts/ClusterContext';
import { useDensity } from '../contexts/DensityContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { POD_TEMPLATE, useGates, useResourceList } from './_data';
import {
  ActionButton,
  ImagesCell,
  NoClusterState,
  PodConsoleModal,
  TruncationFooter,
  menuAction,
} from './_parts';

const CHECKS = [
  { id: 'logs', verb: 'get', group: 'core', resource: 'pods', subresource: 'log' },
  { id: 'exec', verb: 'create', group: 'core', resource: 'pods', subresource: 'exec' },
  { id: 'create', verb: 'create', group: 'core', resource: 'pods' },
];

/** What the Status pill actually says, which is what a filter must match. */
function displayState(row) {
  return row.phase_detail || row.phase || 'Unknown';
}

/**
 * The Status filter, as §6's vocabulary rather than as whatever this cluster
 * happens to be running right now.
 *
 * Declared so that a state with no pods in it is still listed, at zero: "no
 * pods are Pending" is an answer, and an option that vanishes when it empties
 * makes it indistinguishable from a console that does not track Pending.
 * Anything a cluster produces that is not on this list — a container reason
 * nobody here has seen — is added to the menu from the rows themselves.
 */
const PHASE_OPTIONS = [
  'Running',
  'Pending',
  'Succeeded',
  'Failed',
  'CrashLoopBackOff',
  'Terminating',
  'Unknown',
  {
    value: 'Unhealthy',
    label: 'Not healthy (any reason)',
    // A predicate, not a value, and that is the point: it keeps matching states
    // this list has never heard of. During an incident the question is "what is
    // not fine", and enumerating the ways to be unwell is how one gets missed.
    match: (row) => !['Running', 'Succeeded'].includes(displayState(row)),
  },
];

const QOS_OPTIONS = ['Guaranteed', 'Burstable', 'BestEffort'];

export default function Pods() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const { density, setDensity } = useDensity();
  const navigate = useNavigate();

  const [search, setSearch] = useState('');
  const [podConsole, setPodConsole] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);

  const listing = useResourceList('core', 'v1', 'pods', {
    namespace,
    enabled: activeClusterId != null,
  });

  const { gate } = useGates(
    CHECKS.map((check) => ({ ...check, namespace })),
    { enabled: activeClusterId != null },
  );

  const rows = listing.items ?? [];

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
        facet: {
          options: PHASE_OPTIONS,
          note: 'Matches what the pill shows — the container state — rather than the raw phase.',
        },
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
      { key: 'qos_class', title: 'QoS', facet: { options: QOS_OPTIONS } },
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
        // What was read, not what is on screen. The table's own strip says how
        // many rows a filter is leaving of this number, and two places
        // computing "how many" differently is how they come to disagree.
        subtitle={
          listing.loading
            ? 'Reading the cluster…'
            : `${rows.length} in ${namespace ? `namespace ${namespace}` : 'all namespaces'}`
        }
        actions={
          <ActionButton variant="primary" gate={gate('create')} onClick={() => setCreateOpen(true)}>
            Create Pod
          </ActionButton>
        }
      />

      <PartialBanner unavailable={listing.unavailable} />

      <Toolbar ariaLabel="Pod filters">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name, node, image…" />
        </Toolbar.Item>
        <Toolbar.Spacer />
        {/* The same preference the workload table reads, so an operator who
            asked for compact rows once does not have to ask again here. */}
        <Toolbar.Item>
          <DensityToggle value={density} onChange={setDensity} />
        </Toolbar.Item>
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh pods" icon={<SyncAltIcon />} onClick={listing.reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Pods"
        density={density}
        manageableColumns
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
          // Straight to the manifest, rather than opening on Logs and asking
          // the operator to find the tab. Not gated: the pod is already in
          // front of them, so `get` on it has demonstrably been allowed —
          // greying this out on a preflight this page never ran would be
          // rule 11.4 used to hide a thing that works.
          {
            title: 'View YAML',
            onClick: () => setPodConsole({ pod: row, tab: 'yaml' }),
          },
          menuAction('Open terminal', gate('exec'), () => setPodConsole({ pod: row, tab: 'exec' })),
          { isSeparator: true },
          {
            title: 'Show its node',
            isDisabled: !row.node,
            onClick: () => row.node && navigate(`/nodes/${encodeURIComponent(row.node)}`),
          },
        ]}
        emptyTitle="No pods in scope"
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

      {createOpen && (
        <ImportYamlDialog
          isOpen
          title="Create Pod"
          initialText={POD_TEMPLATE}
          onClose={() => setCreateOpen(false)}
          onApplied={() => {
            setCreateOpen(false);
            listing.reload();
          }}
        />
      )}
    </>
  );
}
