/**
 * Overview — §3, the landing page.
 *
 * The whole design of this page follows from one sentence in §3: *each
 * sub-object is collected independently; a failure degrades that key only (set
 * to `null`) and appends to `unavailable`.* So:
 *
 *   - Every tile reads one key and renders it through `MetricCard`, which routes
 *     the value through `NullableCell`. A cluster whose node listing was denied
 *     shows a dash on the node tiles and real numbers everywhere else. It never
 *     shows `0 nodes`, which is a statement this page just failed to verify —
 *     and which is also what an empty cluster looks like.
 *   - `unavailable[]` renders in a `PartialBanner` at the top, naming each
 *     source. The banner is the only thing on screen that can tell "this cluster
 *     has no cordoned nodes" apart from "we could not look at the nodes".
 *   - The two panels (recent Warnings, unhealthy workloads) are separate reads
 *     with their own loading and error states. A denied events listing must not
 *     take the capacity tiles down with it.
 */
import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, CardBody, CardTitle, Grid, GridItem } from '@patternfly/react-core';
import {
  AgeCell,
  DataTable,
  MetricCard,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SectionHeader,
  StatGrid,
  StatusBadge,
} from '../components/ui';
import { clusters as clustersApi, events as eventsApi, workloads as workloadsApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { formatBytes, formatCpu, formatPercent, truncate } from '../utils/format';
import { KIND_TO_PLURAL, useAsync } from './_data';
import { Muted, NoClusterState } from './_parts';

const WARNING_LIMIT = 8;
const UNHEALTHY_LIMIT = 8;

// `Healthy` and `Suspended` are both settled states: a suspended CronJob is
// suspended on purpose and is not a problem to surface here. Everything else —
// including `Unknown` — is worth an operator's attention, and `Unknown` most of
// all, because §6 emits it precisely for a workload whose controller status has
// not been observed.
const UNHEALTHY_STATUSES = new Set(['Degraded', 'Progressing', 'Unknown']);

/**
 * The `unavailable[]` entry that explains why a key is null, as a sentence for
 * the dash's tooltip. Without this a failed collector renders as a bare dash and
 * the operator has to correlate it with the banner by eye.
 */
function reasonFrom(unavailable, resource) {
  const entry = (unavailable ?? []).find((item) => item.resource === resource);
  if (!entry) return undefined;
  const where = entry.namespace ? ` in ${entry.namespace}` : '';
  return `The ${resource} read${where} came back "${entry.reason}", so this number was never measured.`;
}

export default function Overview() {
  const { activeClusterId, activeCluster } = useCluster();
  const { selected: namespace } = useNamespace();
  const navigate = useNavigate();

  const overview = useAsync(() => clustersApi.overview(activeClusterId), {
    key: `overview:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  const warnings = useAsync(() => eventsApi.list({ type: 'Warning', namespace, limit: 100 }), {
    key: `overview-warnings:${activeClusterId}:${namespace ?? '*'}`,
    enabled: activeClusterId != null,
  });

  const workloads = useAsync(() => workloadsApi.list({ namespace }), {
    key: `overview-workloads:${activeClusterId}:${namespace ?? '*'}`,
    enabled: activeClusterId != null,
  });

  const data = overview.data;
  const unavailable = data?.unavailable ?? [];

  const unhealthy = useMemo(() => {
    const rows = workloads.data?.items ?? [];
    return rows
      .filter((row) => UNHEALTHY_STATUSES.has(row.status))
      // Degraded before Progressing before Unknown: the first is broken now, the
      // second may settle on its own, the third is a question we have not
      // answered. That is also the order an operator wants to read them in.
      .sort((a, b) => {
        const rank = { Degraded: 0, Unknown: 1, Progressing: 2 };
        return (rank[a.status] ?? 3) - (rank[b.status] ?? 3);
      });
  }, [workloads.data]);

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Overview" subtitle="Cluster summary" />
        <NoClusterState what="The overview" />
      </>
    );
  }

  const workloadTotal = data?.workloads
    ? Object.values(data.workloads).reduce((sum, n) => sum + (Number(n) || 0), 0)
    : null;

  return (
    <>
      <PageHeader
        title={activeCluster?.name ? `${activeCluster.name} overview` : 'Overview'}
        subtitle={
          activeCluster?.api_server
            ? `${activeCluster.api_server} — ${data?.platform ?? activeCluster.platform ?? 'kubernetes'}`
            : 'Cluster summary'
        }
        badge={activeCluster?.status ? <StatusBadge status={activeCluster.status} /> : null}
      />

      {/* Rule 11.1. Persistent, inline, one row per source, above the numbers it
          qualifies — not a toast that expires while the operator is still
          reading the tiles it applies to. */}
      <PartialBanner unavailable={unavailable} />

      {overview.error ? (
        <Card>
          <CardBody>
            <DataTable
              ariaLabel="Cluster overview"
              columns={[{ key: 'x', title: 'Overview' }]}
              rows={[]}
              error={overview.error}
              onRetry={overview.reload}
            />
          </CardBody>
        </Card>
      ) : null}

      <SectionHeader title="Cluster" description="Each figure below is read independently; a dash means that one read failed." />
      <StatGrid minWidth={200}>
        <MetricCard
          label="Kubernetes version"
          value={data?.server_version}
          reason={reasonFrom(unavailable, 'version')}
          help="Reported by the API server's /version endpoint."
        />
        <MetricCard
          label="Nodes"
          value={data?.nodes?.total}
          reason={reasonFrom(unavailable, 'nodes')}
          accent={data?.nodes && data.nodes.ready !== data.nodes.total ? 'warning' : undefined}
          sub={
            data?.nodes ? (
              <>
                <NullableCell value={data.nodes.ready} /> ready
                {data.nodes.unschedulable ? ` · ${data.nodes.unschedulable} cordoned` : null}
              </>
            ) : null
          }
          onClick={() => navigate('/nodes')}
        />
        <MetricCard
          label="Namespaces"
          value={data?.namespaces}
          reason={reasonFrom(unavailable, 'namespaces')}
          onClick={() => navigate('/namespaces')}
        />
        <MetricCard
          label="Workloads"
          value={workloadTotal}
          reason={reasonFrom(unavailable, 'deployments')}
          sub={
            data?.workloads ? (
              <Muted>
                {`${data.workloads.deployments ?? 0} deploy · ${data.workloads.statefulsets ?? 0} sts · ` +
                  `${data.workloads.daemonsets ?? 0} ds · ${data.workloads.cronjobs ?? 0} cron`}
              </Muted>
            ) : null
          }
          onClick={() => navigate('/workloads')}
        />
        <MetricCard
          label="Pods"
          value={data?.pods?.total}
          reason={reasonFrom(unavailable, 'pods')}
          accent={data?.pods?.failed ? 'danger' : undefined}
          sub={
            data?.pods ? (
              <Muted>
                {`${data.pods.running ?? 0} running · ${data.pods.pending ?? 0} pending · ${data.pods.failed ?? 0} failed`}
              </Muted>
            ) : null
          }
          onClick={() => navigate('/pods')}
        />
      </StatGrid>

      <SectionHeader
        title="Capacity and requests"
        description="Requests are what the scheduler has committed, not what is being used — this console reads no metrics API."
      />
      <StatGrid minWidth={200}>
        <MetricCard
          label="CPU capacity"
          value={data?.capacity?.cpu_cores}
          format={formatCpu}
          unit="cores"
          reason={reasonFrom(unavailable, 'nodes')}
        />
        <MetricCard
          label="CPU requested"
          value={data?.requested?.cpu_cores}
          format={formatCpu}
          unit="cores"
          reason={reasonFrom(unavailable, 'pods')}
          sub={
            data?.requested?.cpu_cores != null && data?.capacity?.cpu_cores != null ? (
              <Muted>{`${formatPercent(data.requested.cpu_cores, data.capacity.cpu_cores)} of capacity`}</Muted>
            ) : null
          }
        />
        <MetricCard
          label="Memory capacity"
          value={data?.capacity?.memory_bytes}
          format={formatBytes}
          reason={reasonFrom(unavailable, 'nodes')}
        />
        <MetricCard
          label="Memory requested"
          value={data?.requested?.memory_bytes}
          format={formatBytes}
          reason={reasonFrom(unavailable, 'pods')}
          sub={
            data?.requested?.memory_bytes != null && data?.capacity?.memory_bytes != null ? (
              <Muted>{`${formatPercent(data.requested.memory_bytes, data.capacity.memory_bytes)} of capacity`}</Muted>
            ) : null
          }
        />
        <MetricCard
          label="Pod capacity"
          value={data?.capacity?.pods}
          reason={reasonFrom(unavailable, 'nodes')}
          sub={
            data?.pods?.total != null && data?.capacity?.pods != null ? (
              <Muted>{`${formatPercent(data.pods.total, data.capacity.pods)} scheduled`}</Muted>
            ) : null
          }
        />
      </StatGrid>

      <Grid hasGutter style={{ marginTop: '1.5rem' }}>
        <GridItem lg={6} md={12}>
          <Card isFullHeight>
            <CardTitle>Workloads needing attention</CardTitle>
            <CardBody>
              <PartialBanner unavailable={workloads.data?.unavailable} />
              <DataTable
                ariaLabel="Workloads needing attention"
                variant="compact"
                isStickyHeader={false}
                columns={[
                  {
                    key: 'name',
                    title: 'Name',
                    cell: (row) => (
                      <ResourceLink kind={row.kind} name={row.name} namespace={row.namespace} />
                    ),
                  },
                  { key: 'kind', title: 'Kind' },
                  { key: 'namespace', title: 'Namespace' },
                  {
                    key: 'status',
                    title: 'Status',
                    cell: (row) => (
                      <StatusBadge status={row.status} tooltip={row.status_reason ?? undefined} />
                    ),
                  },
                  {
                    key: 'reason',
                    title: 'Why',
                    modifier: 'truncate',
                    cell: (row) => <Muted title={row.status_reason ?? undefined}>{truncate(row.status_reason, 48) ?? '—'}</Muted>,
                  },
                ]}
                rows={unhealthy.slice(0, UNHEALTHY_LIMIT)}
                rowKey={(row) => `${row.kind}/${row.namespace}/${row.name}`}
                loading={workloads.loading}
                error={workloads.error}
                onRetry={workloads.reload}
                onRowClick={(row) =>
                  navigate(`/workloads/${KIND_TO_PLURAL[row.kind] ?? 'deployments'}/${row.namespace}/${row.name}`)
                }
                emptyTitle="Every workload we could read is healthy"
                emptyDescription={
                  workloads.data?.partial
                    ? 'Some workload kinds could not be listed — see the banner above. This is not a clean bill of health.'
                    : 'No Degraded, Progressing or Unknown workloads in scope.'
                }
                footer={
                  unhealthy.length > UNHEALTHY_LIMIT ? (
                    <Muted>{`${unhealthy.length - UNHEALTHY_LIMIT} more not shown — open Workloads for the full list.`}</Muted>
                  ) : null
                }
              />
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={6} md={12}>
          <Card isFullHeight>
            <CardTitle>Recent Warning events</CardTitle>
            <CardBody>
              <PartialBanner unavailable={warnings.data?.unavailable} />
              <DataTable
                ariaLabel="Recent warning events"
                variant="compact"
                isStickyHeader={false}
                columns={[
                  { key: 'last_seen', title: 'Last seen', cell: (row) => <AgeCell timestamp={row.last_seen} /> },
                  { key: 'reason', title: 'Reason' },
                  {
                    key: 'object',
                    title: 'Object',
                    value: (row) => `${row.involved?.kind ?? ''}/${row.involved?.name ?? ''}`,
                    cell: (row) => (
                      <ResourceLink
                        kind={row.involved?.kind}
                        name={row.involved?.name}
                        namespace={row.involved?.namespace}
                      >
                        {`${row.involved?.kind ?? 'Object'}/${row.involved?.name ?? '—'}`}
                      </ResourceLink>
                    ),
                  },
                  {
                    key: 'message',
                    title: 'Message',
                    modifier: 'truncate',
                    cell: (row) => <span title={row.message ?? undefined}>{truncate(row.message, 60)}</span>,
                  },
                ]}
                rows={(warnings.data?.items ?? []).slice(0, WARNING_LIMIT)}
                rowKey={(row, index) => `${row.namespace ?? ''}/${row.involved?.name ?? ''}/${row.last_seen ?? index}`}
                loading={warnings.loading}
                error={warnings.error}
                onRetry={warnings.reload}
                onRowClick={() => navigate('/events?type=Warning')}
                emptyTitle="No Warning events in the window"
                emptyDescription="The API server keeps events for about an hour by default, so this is a recent picture and not a history."
              />
            </CardBody>
          </Card>
        </GridItem>
      </Grid>
    </>
  );
}
