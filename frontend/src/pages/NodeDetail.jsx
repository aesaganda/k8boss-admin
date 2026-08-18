/**
 * NodeDetail — §5 `GET /api/nodes/{name}`.
 *
 * This is the page an operator reads immediately before draining a node, which
 * makes one thing here more important than everything else on it:
 *
 * > `pods` is `null`, not `[]`, when the pod listing failed. An empty table here
 * > is the sentence that gets the button clicked.
 *
 * So a `null` pods list renders a **failure panel where the table would be** —
 * not an empty table, not an empty state, and certainly not "no pods on this
 * node". The Drain button stays available (the operator may still have a good
 * reason) but the page never claims the node is empty, because it does not know
 * that. Everything else on the page follows the same rule: `requested` and
 * `pod_count` go through `NullableCell`, and `unavailable[]` is named in a
 * persistent banner.
 */
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Card, CardBody, CardTitle, Grid, GridItem } from '@patternfly/react-core';
import {
  AgeCell,
  DataTable,
  DescriptionList,
  ErrorState,
  MetricCard,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SectionHeader,
  StatGrid,
  StatusBadge,
} from '../components/ui';
import CordonDialog from '../components/CordonDialog';
import DrainDialog from '../components/DrainDialog';
import { nodes as nodesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes, formatCpu } from '../utils/format';
import { useAsync, useGates } from './_data';
import { ActionButton, ChipList, Muted, NoClusterState, PodConsoleModal, menuAction } from './_parts';

const CHECKS = [
  { id: 'cordon', verb: 'patch', group: 'core', resource: 'nodes' },
  { id: 'drain', verb: 'create', group: 'core', resource: 'pods', subresource: 'eviction' },
  { id: 'logs', verb: 'get', group: 'core', resource: 'pods', subresource: 'log' },
  { id: 'exec', verb: 'create', group: 'core', resource: 'pods', subresource: 'exec' },
];

/** `9.2 cores` or the word for "we do not know", never a fabricated zero. */
function orUnknown(value, format) {
  const rendered = format ? format(value) : value;
  return rendered == null ? 'unknown' : rendered;
}

export default function NodeDetail() {
  const { name } = useParams();
  const { activeClusterId } = useCluster();
  const [cordonOpen, setCordonOpen] = useState(false);
  const [drainOpen, setDrainOpen] = useState(false);
  // `{ pod, tab }` — which pod's console is open and which half of it.
  const [podConsole, setPodConsole] = useState(null);

  const { data, loading, error, reload } = useAsync(() => nodesApi.get(name), {
    key: `node:${activeClusterId}:${name}`,
    enabled: activeClusterId != null && Boolean(name),
  });

  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const podColumns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Pod',
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
        value: (row) => row.phase_detail || row.phase,
        // `phase_detail` overrides `phase` (§6): a Running pod whose container is
        // CrashLoopBackOff must not show a green pill on the page somebody is
        // reading to decide whether this node is safe to empty.
        cell: (row) => <StatusBadge status={row.phase} detail={row.phase_detail} />,
      },
      { key: 'ready', title: 'Ready' },
      { key: 'restarts', title: 'Restarts', sortable: true },
      { key: 'qos_class', title: 'QoS' },
      { key: 'ip', title: 'IP' },
      { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
    ],
    [],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title={name ?? 'Node'} breadcrumbs={[{ label: 'Nodes', to: '/nodes' }, { label: name }]} />
        <NoClusterState what="This node" />
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader title={name} breadcrumbs={[{ label: 'Nodes', to: '/nodes' }, { label: name }]} />
        <ErrorState title={`Node ${name} could not be read`} error={error} onRetry={reload} />
      </>
    );
  }

  const node = data ?? {};
  const pods = node.pods; // null means "we could not look" — see the docstring.
  const readyStatus = node.ready === true ? 'Ready' : node.ready === false ? 'NotReady' : 'Unknown';

  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={[{ label: 'Nodes', to: '/nodes' }, { label: name }]}
        subtitle={node.internal_ip ? `${node.internal_ip} · ${node.os_image ?? 'unknown image'}` : undefined}
        badge={
          <span style={{ display: 'inline-flex', gap: '0.35rem' }}>
            <StatusBadge status={readyStatus} />
            {node.unschedulable && <StatusBadge status="Cordoned" />}
          </span>
        }
        actions={[
          <ActionButton key="cordon" gate={gate('cordon')} onClick={() => setCordonOpen(true)}>
            {node.unschedulable ? 'Uncordon' : 'Cordon'}
          </ActionButton>,
          <ActionButton key="drain" gate={gate('drain')} isDanger onClick={() => setDrainOpen(true)}>
            Drain…
          </ActionButton>,
        ]}
      />

      <PartialBanner unavailable={node.unavailable} />

      <Grid hasGutter>
        <GridItem lg={5} md={12}>
          <Card isFullHeight>
            <CardTitle>Node</CardTitle>
            <CardBody>
              <DescriptionList
                items={[
                  { label: 'Kubelet', value: node.kubelet_version },
                  { label: 'Container runtime', value: node.container_runtime },
                  { label: 'OS image', value: node.os_image },
                  { label: 'Internal IP', value: node.internal_ip },
                  {
                    label: 'Roles',
                    value: <ChipList values={node.roles} max={4} emptyText="none" />,
                  },
                  {
                    label: 'Schedulable',
                    value: (
                      <StatusBadge
                        status={node.unschedulable ? 'Unschedulable' : 'True'}
                        label={node.unschedulable ? 'No — cordoned' : 'Yes'}
                        tooltip={
                          node.unschedulable
                            ? 'spec.unschedulable is true. Pods already running here are untouched; only new scheduling is blocked.'
                            : undefined
                        }
                      />
                    ),
                  },
                  { label: 'Age', value: <AgeCell seconds={node.age_seconds} /> },
                ]}
              />
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={7} md={12}>
          <StatGrid minWidth={180}>
            <MetricCard
              label="CPU requested"
              value={node.requested?.cpu_cores}
              format={formatCpu}
              unit="cores"
              reason="The pods on this node could not be listed, so nothing is known about what is requested here."
              sub={<Muted>{`of ${orUnknown(node.allocatable?.cpu_cores, formatCpu)} allocatable`}</Muted>}
            />
            <MetricCard
              label="Memory requested"
              value={node.requested?.memory_bytes}
              format={formatBytes}
              reason="The pods on this node could not be listed, so nothing is known about what is requested here."
              sub={<Muted>{`of ${orUnknown(node.allocatable?.memory_bytes, formatBytes)} allocatable`}</Muted>}
            />
            <MetricCard
              label="Pods"
              value={node.pod_count}
              reason="The pods on this node could not be listed. This node is not empty — we did not look."
              sub={<Muted>{`of ${orUnknown(node.capacity?.pods)} capacity`}</Muted>}
            />
          </StatGrid>

          <SectionHeader title="Conditions" />
          <DataTable
            ariaLabel="Node conditions"
            isStickyHeader={false}
            columns={[
              { key: 'type', title: 'Type' },
              {
                key: 'status',
                title: 'Status',
                // A condition's status is a three-valued string ("True",
                // "False", "Unknown"), and Unknown means the kubelet has stopped
                // reporting. StatusBadge maps all three; nothing here collapses
                // Unknown into False.
                cell: (row) => <StatusBadge status={row.status} />,
              },
              { key: 'reason', title: 'Reason' },
            ]}
            rows={node.conditions ?? []}
            rowKey={(row, index) => `${row.type ?? index}`}
            loading={loading}
            emptyTitle="No conditions reported"
            emptyDescription="The API server returned no status conditions for this node."
          />
        </GridItem>
      </Grid>

      <SectionHeader
        title="Taints"
        description="A taint repels pods that do not tolerate it. Draining does not remove taints, and a cordon is not a taint."
      />
      <DataTable
        ariaLabel="Node taints"
        isStickyHeader={false}
        columns={[
          { key: 'key', title: 'Key' },
          { key: 'value', title: 'Value' },
          { key: 'effect', title: 'Effect' },
        ]}
        rows={node.taints ?? []}
        rowKey={(row, index) => `${row.key ?? ''}:${row.effect ?? ''}:${index}`}
        loading={loading}
        emptyTitle="No taints"
        emptyDescription="Nothing on this node repels pods."
      />

      <SectionHeader
        title="Pods on this node"
        description="Terminated pods are included here, unlike the count above, which follows what the scheduler treats as occupying the node."
      />
      {pods === null ? (
        // The single most dangerous empty table in this console. §5 nulls this
        // list rather than emptying it, and the page has to keep that
        // distinction visible right where the table would have been.
        <ErrorState
          title="The pods on this node could not be listed"
          detail={
            'This is not an empty node — it is a node we were unable to look at. The banner above names the ' +
            'reason. Do not read this as "nothing is running here"; check the reason, or list the pods with ' +
            `kubectl get pods --all-namespaces --field-selector spec.nodeName=${name}, before draining.`
          }
          onRetry={reload}
        />
      ) : (
        <DataTable
          ariaLabel="Pods on this node"
          columns={podColumns}
          rows={pods ?? []}
          rowKey={(row) => `${row.namespace}/${row.name}`}
          loading={loading}
          actions={(row) => [
            // Reading logs is a read, so it is not gated on the write switch —
            // §1.6 is explicit that a read-only deployment is still a useful
            // one, and greying out the logs would make it much less so.
            menuAction('View logs', gate('logs', { requiresWrite: false }), () =>
              setPodConsole({ pod: row, tab: 'logs' }),
            ),
            menuAction('Open terminal', gate('exec'), () => setPodConsole({ pod: row, tab: 'exec' })),
          ]}
          emptyTitle="No pods are scheduled here"
          emptyDescription="The pod listing succeeded and returned nothing, so this node really is empty."
        />
      )}

      {cordonOpen && (
        <CordonDialog
          isOpen
          node={node}
          name={name}
          unschedulable={!node.unschedulable}
          onClose={() => setCordonOpen(false)}
          onApplied={() => {
            setCordonOpen(false);
            reload();
          }}
        />
      )}

      {drainOpen && (
        <DrainDialog
          isOpen
          node={node}
          name={name}
          onClose={() => setDrainOpen(false)}
          onApplied={() => {
            setDrainOpen(false);
            reload();
          }}
        />
      )}

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
