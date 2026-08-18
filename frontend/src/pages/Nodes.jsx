/**
 * Nodes — §5.
 *
 * The column that this page exists for is `requested / allocatable`, and the
 * reason it is built the way it is comes straight out of §5: *`requested` and
 * `pod_count` derive from listing pods with `spec.nodeName=<node>`. If that
 * listing fails they are `null` and the envelope reports `unavailable` — never
 * `0`, which would read as an idle node.*
 *
 * An idle node is the node an operator drains. So every number in those columns
 * goes through `NullableCell`, and the banner above the table says which nodes
 * we could not measure. There is no code path here that turns a missing
 * measurement into a small one.
 */
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import CordonDialog from '../components/CordonDialog';
import DrainDialog from '../components/DrainDialog';
import { nodes as nodesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes, formatCpu } from '../utils/format';
import { useAsync, useGates } from './_data';
import { ChipList, Muted, NoClusterState, UsageCell, menuAction } from './_parts';

// §9 spelling of the two writes this page offers, mirrored from the backend:
// `admin/nodes.py` preflights `patch core/nodes` for cordon and
// `create core/pods` (subresource `eviction`) for drain. Asked once for the
// page, not once per row — forty rows would be forty SelfSubjectAccessReviews.
const CHECKS = [
  { id: 'cordon', verb: 'patch', group: 'core', resource: 'nodes' },
  { id: 'drain', verb: 'create', group: 'core', resource: 'pods', subresource: 'eviction' },
];

function readyStatus(node) {
  // Tri-state, deliberately. `node_ready` returns null when the Ready condition
  // is absent, and a node whose readiness we have not observed is not a ready
  // node — StatusBadge renders `Unknown` grey with the "not the same as healthy"
  // tooltip, which is exactly the claim we can support.
  if (node.ready === true) return 'Ready';
  if (node.ready === false) return 'NotReady';
  return 'Unknown';
}

export default function Nodes() {
  const { activeClusterId } = useCluster();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [cordonTarget, setCordonTarget] = useState(null);
  const [drainTarget, setDrainTarget] = useState(null);

  const { data, loading, error, reload } = useAsync(() => nodesApi.list(), {
    key: `nodes:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const rows = data?.items ?? [];

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Name',
        sortable: true,
        cell: (row) => <ResourceLink kind="Node" name={row.name} />,
      },
      {
        key: 'ready',
        title: 'Status',
        sortable: true,
        value: (row) => readyStatus(row),
        cell: (row) => (
          <span style={{ display: 'inline-flex', gap: '0.35rem', flexWrap: 'wrap' }}>
            <StatusBadge status={readyStatus(row)} />
            {/* Cordoned is not a health state and does not replace the Ready
                pill: a cordoned node that is also NotReady is two facts, and
                collapsing them hides whichever one the operator was not
                already thinking about. */}
            {row.unschedulable && <StatusBadge status="Cordoned" tooltip="spec.unschedulable is true: the scheduler will place nothing new here. Pods already running are untouched." />}
          </span>
        ),
      },
      {
        key: 'roles',
        title: 'Roles',
        value: (row) => (row.roles ?? []).join(','),
        cell: (row) => <ChipList values={row.roles} max={2} emptyText="none" />,
      },
      { key: 'kubelet_version', title: 'Version', sortable: true },
      {
        key: 'cpu',
        title: 'CPU requested',
        sortable: true,
        value: (row) => row.requested?.cpu_cores ?? null,
        cell: (row) => (
          <UsageCell
            used={row.requested?.cpu_cores}
            total={row.allocatable?.cpu_cores}
            format={formatCpu}
            reason="The pods on this node could not be listed, so nothing is known about what is requested here."
          />
        ),
      },
      {
        key: 'memory',
        title: 'Memory requested',
        sortable: true,
        value: (row) => row.requested?.memory_bytes ?? null,
        cell: (row) => (
          <UsageCell
            used={row.requested?.memory_bytes}
            total={row.allocatable?.memory_bytes}
            format={formatBytes}
            reason="The pods on this node could not be listed, so nothing is known about what is requested here."
          />
        ),
      },
      {
        key: 'pod_count',
        title: 'Pods',
        sortable: true,
        value: (row) => row.pod_count,
        cell: (row) => (
          <UsageCell
            used={row.pod_count}
            total={row.capacity?.pods}
            reason="The pods on this node could not be listed. This node is not empty — we did not look."
          />
        ),
      },
      {
        key: 'taints',
        title: 'Taints',
        sortable: true,
        value: (row) => (row.taints ?? []).length,
        cell: (row) =>
          (row.taints ?? []).length ? (
            <ChipList
              values={(row.taints ?? []).map((t) => `${t.key}${t.value ? `=${t.value}` : ''}:${t.effect}`)}
              max={1}
              color="orange"
            />
          ) : (
            <Muted>none</Muted>
          ),
      },
      {
        key: 'age_seconds',
        title: 'Age',
        sortable: true,
        cell: (row) => <AgeCell seconds={row.age_seconds} />,
      },
    ],
    [],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Nodes" />
        <NoClusterState what="The node list" />
      </>
    );
  }

  const readyCount = rows.filter((row) => row.ready === true).length;

  return (
    <>
      <PageHeader
        title="Nodes"
        subtitle={
          loading
            ? 'Reading the cluster…'
            : `${readyCount} of ${rows.length} ready${data?.partial ? ' — some data could not be read' : ''}`
        }
      />

      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Node controls">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name, role or version…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>
            {'Requested is what the scheduler has committed on each node — this console reads no metrics API.'}
          </Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh nodes" icon={<SyncAltIcon />} onClick={reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Nodes"
        columns={columns}
        rows={rows}
        rowKey="name"
        loading={loading}
        error={error}
        onRetry={reload}
        filterText={search}
        onRowClick={(row) => navigate(`/nodes/${encodeURIComponent(row.name)}`)}
        actions={(row) => [
          menuAction(row.unschedulable ? 'Uncordon' : 'Cordon', gate('cordon'), () =>
            setCordonTarget({ node: row, unschedulable: !row.unschedulable }),
          ),
          menuAction('Drain…', gate('drain'), () => setDrainTarget(row), { isDanger: true }),
          { isSeparator: true },
          { title: 'Open node', onClick: () => navigate(`/nodes/${encodeURIComponent(row.name)}`) },
        ]}
        emptyTitle="No nodes"
        emptyDescription="The API server returned an empty node list. On a working cluster that is not possible, so check the banner above."
      />

      {cordonTarget && (
        <CordonDialog
          isOpen
          name={cordonTarget.node.name}
          unschedulable={cordonTarget.unschedulable}
          // `pod_count` is null when the per-node pod listing failed, and the
          // dialog sizes its warning from it — passing 0 there would tell an
          // operator the node is empty on the screen where that claim does the
          // most damage.
          podCount={cordonTarget.node.pod_count}
          onClose={() => setCordonTarget(null)}
          onApplied={() => {
            setCordonTarget(null);
            reload();
          }}
        />
      )}

      {drainTarget && (
        <DrainDialog
          isOpen
          name={drainTarget.name}
          onClose={() => setDrainTarget(null)}
          onApplied={() => {
            setDrainTarget(null);
            reload();
          }}
        />
      )}
    </>
  );
}
