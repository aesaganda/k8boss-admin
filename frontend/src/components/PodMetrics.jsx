/**
 * PodMetrics — §7.7 `GET /api/pods/{namespace}/{name}/metrics`.
 *
 * This panel exists to answer "what is this pod actually using", and the whole
 * design of it is arranged around the one wrong answer it must never give:
 *
 * > **A pod we could not measure is never drawn at zero.** `metrics.k8s.io` is
 * > an aggregated API a great many clusters do not serve, and on the ones that
 * > do, a pod that started ten seconds ago has no sample yet. Both are ordinary
 * > facts. A bar at 0% and a tile reading "0 cores" describe an idle pod, and an
 * > idle pod is the one somebody scales to zero.
 *
 * So every number here goes through `NullableCell`, the backend sends `null`
 * rather than `0` for anything it did not measure, and the reason lands in
 * `unavailable[]` where `PartialBanner` names it. A cluster with no
 * metrics-server gets a calm sentence, not a red panel: §1.2's `unsupported` is
 * "not present on this cluster", and colouring that red trains people to ignore
 * red.
 *
 * The requests and limits come from the pod itself, so they render even when
 * there is no sample at all — which is what makes this tab worth opening on a
 * cluster with no metrics API: "what did this container ask for" is still an
 * answer.
 */
import { useEffect } from 'react';
import { Button, Card, CardBody, CardTitle, Grid, GridItem, Tooltip } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  DataTable,
  MetricCard,
  NullableCell,
  PartialBanner,
  StatGrid,
  Toolbar,
} from './ui';
import { pods as podsApi } from '../api/client';
import { formatBytes, formatCpu, formatTimestamp } from '../utils/format';
import { useAsync } from '../pages/_data';
import { Muted } from '../pages/_parts';

/**
 * How often the sample is re-read.
 *
 * metrics-server's own collection interval is 15 seconds by default, so a
 * shorter poll would redraw the same numbers; a much longer one would show a
 * "live" panel that is a minute behind during the incident it is open for.
 */
const REFRESH_MS = 15000;

/**
 * A used-against-declared bar, or nothing at all.
 *
 * Rendered only when **both** numbers are known. A bar needs a denominator to
 * mean anything, and drawing an empty track for a container with no limit would
 * say "using none of its allowance" about a container that has no allowance —
 * which is the opposite of the truth, since an unlimited container is the one
 * that can take the node down.
 */
function UsageBar({ used, declared, label }) {
  if (used == null || declared == null || declared <= 0) return null;
  const ratio = used / declared;
  const percent = Math.round(ratio * 100);
  const level = ratio >= 0.9 ? 'danger' : ratio >= 0.75 ? 'warning' : 'ok';
  return (
    <Tooltip content={`${label}: ${percent}% of ${declared > 0 ? 'the declared value' : ''}`.trim()}>
      <div className="admin-usage" data-level={level} aria-label={`${label}, ${percent} percent`}>
        <div className="admin-usage__fill" style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
    </Tooltip>
  );
}

export function PodMetrics({ namespace, name, clusterId }) {
  const metrics = useAsync(() => podsApi.metrics(namespace, name), {
    key: `pod-metrics:${clusterId}:${namespace}/${name}`,
  });

  const { reload } = metrics;
  useEffect(() => {
    // Paused while the tab is hidden. A console left open on a second monitor
    // overnight is otherwise several thousand reads of a pod nobody is looking
    // at, and a hidden tab re-reads the moment it is shown anyway.
    const timer = setInterval(() => {
      if (!document.hidden) reload();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [reload]);

  const data = metrics.data;
  const items = data?.items ?? [];
  const total = data?.pod ?? {};

  const columns = [
    { key: 'container', title: 'Container', sortable: true },
    {
      key: 'kind',
      title: 'Kind',
      // Init containers are listed because they are part of the pod. Whether
      // their missing sample is a gap is `sample_expected`, not `kind`: a
      // terminated init container holds nothing, and a running one — a
      // `restartPolicy: Always` sidecar — is using real resources.
      cell: (row) => <Muted>{row.kind === 'init' ? 'init container' : 'container'}</Muted>,
    },
    {
      key: 'cpu',
      title: 'CPU',
      cell: (row) => (
        <div className="admin-cell-inline">
          <NullableCell value={formatCpu(row.usage?.cpu_cores)} reason={missingReason(row)} />
          <UsageBar
            used={row.usage?.cpu_cores}
            declared={cpuOf(row.limits) ?? cpuOf(row.requests)}
            label="CPU"
          />
        </div>
      ),
    },
    {
      key: 'cpu_declared',
      title: 'CPU requested / limit',
      cell: (row) => <Declared request={row.requests?.cpu} limit={row.limits?.cpu} />,
    },
    {
      key: 'memory',
      title: 'Memory',
      cell: (row) => (
        <div className="admin-cell-inline">
          <NullableCell value={formatBytes(row.usage?.memory_bytes)} reason={missingReason(row)} />
          <UsageBar
            used={row.usage?.memory_bytes}
            declared={bytesOf(row.limits) ?? bytesOf(row.requests)}
            label="Memory"
          />
        </div>
      ),
    },
    {
      key: 'memory_declared',
      title: 'Memory requested / limit',
      cell: (row) => <Declared request={row.requests?.memory} limit={row.limits?.memory} />,
    },
  ];

  return (
    <>
      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Pod metrics controls">
        <Toolbar.Item>
          <span data-testid="pod-metrics-window">
            <Muted>
              {data?.timestamp
                ? `Sampled at ${formatTimestamp(data.timestamp)}${
                    data.window_seconds ? ` over ${data.window_seconds}s` : ''
                  }`
                : 'No sample — the numbers below are what each container asked for, not what it is using.'}
            </Muted>
          </span>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button
            variant="plain"
            aria-label="Refresh metrics"
            icon={<SyncAltIcon />}
            onClick={metrics.reload}
          />
        </Toolbar.Item>
      </Toolbar>

      <Grid hasGutter>
        <GridItem span={12}>
          <StatGrid>
            <MetricCard
              label="Pod CPU"
              value={formatCpu(total.cpu_cores)}
              help={
                'The sum of every sampled container. Unknown — not zero — whenever any container ' +
                'is missing from the sample, because a total that silently omits one understates ' +
                'the pod by however much that container is using.'
              }
              reason="A container in this pod is missing from the sample, so the pod's total is unknown — not zero."
            />
            <MetricCard
              label="Pod memory"
              value={formatBytes(total.memory_bytes)}
              help="The sum of every sampled container, or unknown when any container is missing from the sample."
              reason="A container in this pod is missing from the sample, so the pod's total is unknown — not zero."
            />
            <MetricCard
              label="Containers"
              value={items.length}
              sub={`${items.filter((item) => item.usage != null).length} with a live sample`}
              help={
                'Every container in the pod, init containers included. A terminated init ' +
                'container has no sample and is not expected to — it is not counted as a gap.'
              }
            />
          </StatGrid>
        </GridItem>

        <GridItem span={12}>
          <Card>
            <CardTitle>Per container</CardTitle>
            <CardBody>
              <DataTable
                ariaLabel="Container usage"
                tableId="pods:metrics"
                columns={columns}
                rows={items}
                rowKey={(row) => `${row.kind}/${row.container}`}
                loading={metrics.loading && !data}
                error={metrics.error}
                onRetry={metrics.reload}
                emptyTitle="This pod declares no containers"
                emptyDescription="Which the API server does not allow, so this is worth reporting."
              />
            </CardBody>
          </Card>
        </GridItem>
      </Grid>
    </>
  );
}

/**
 * Why this container's cell is a dash — and the two answers are not the same.
 *
 * `sample_expected: false` is a terminated init container: metrics-server does
 * not report one and there is nothing running to measure. Anything else is a
 * gap, named in the banner above. Collapsing the two would put "we could not
 * measure this" on every pod that has ever run a migration, which is how a
 * banner stops being read.
 */
function missingReason(row) {
  return row.sample_expected === false
    ? 'This init container has finished, so there is nothing running to measure. It is not a failed reading.'
    : 'This container has no sample — see the banner above for why. It is not zero.';
}

/** `250m / 1` — what the container asked for and what it is capped at. */
function Declared({ request, limit }) {
  return (
    <span className="admin-cell-inline">
      <NullableCell
        value={request}
        reason="This container declares no request. It is BestEffort for this resource — first in line to be evicted."
      />
      <Muted>/</Muted>
      <NullableCell value={limit} reason="This container declares no limit. It can use as much as the node has." />
    </span>
  );
}

/** The CPU quantity of a requests/limits map, in cores, or `null`. */
function cpuOf(quantities) {
  const raw = quantities?.cpu;
  if (raw == null) return null;
  const text = String(raw);
  const millis = text.endsWith('m');
  const number = Number(millis ? text.slice(0, -1) : text);
  return Number.isFinite(number) ? (millis ? number / 1000 : number) : null;
}

const BYTE_SUFFIXES = {
  Ki: 1024,
  Mi: 1024 ** 2,
  Gi: 1024 ** 3,
  Ti: 1024 ** 4,
  Pi: 1024 ** 5,
  k: 1e3,
  M: 1e6,
  G: 1e9,
  T: 1e12,
  P: 1e15,
};

/** The memory quantity of a requests/limits map, in bytes, or `null`. */
function bytesOf(quantities) {
  const raw = quantities?.memory;
  if (raw == null) return null;
  const text = String(raw).trim();
  const match = /^(\d+(?:\.\d+)?)([A-Za-z]*)$/.exec(text);
  if (!match) return null;
  const number = Number(match[1]);
  if (!Number.isFinite(number)) return null;
  const suffix = match[2];
  if (!suffix) return number;
  const factor = BYTE_SUFFIXES[suffix];
  // An unrecognised suffix means we do not know the magnitude. `null` leaves the
  // bar unrendered, where guessing `number` would draw one against a
  // denominator off by a factor of a billion.
  return factor ? number * factor : null;
}

export default PodMetrics;
