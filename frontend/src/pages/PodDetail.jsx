/**
 * PodDetail — one pod, and everything this console can say about it, behind tabs.
 *
 * Before this page existed, a pod's logs, terminal and manifest lived in a modal
 * that the pod table opened. That worked and it did not scale: the modal is not
 * addressable (an operator cannot send a colleague "look at this pod's
 * environment"), it has nowhere to put the pod's own detail, and a shell inside
 * a dialog closes when the operator clicks anywhere they should have been able
 * to click. So the pod is a route — `/pods/{namespace}/{name}` — and the tab is
 * in the query string, which makes every tab a link.
 *
 * **Only the visible tab fetches.** Eight tabs mounted at once is eight reads on
 * arrival — eight chances to be denied, eight banners, and seven of them about a
 * panel nobody is looking at. It also matters here more than on a listing page:
 * two of these tabs open a websocket, and a Terminal tab that connected while
 * the operator was reading the YAML would put an exec session — an audited,
 * privileged thing — behind a tab they never opened.
 *
 * The pod itself is read once, here, and passed down. Every tab needs the
 * container list, and five independent reads of the same pod would let the tabs
 * disagree about which containers exist.
 *
 * What the header must not do is render a green pill over a broken pod: §6's
 * `phase_detail` overrides `phase`, because a `Running` pod whose only container
 * is in `CrashLoopBackOff` is not running, and this is the page somebody opens
 * to find that out.
 */
import { useCallback, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Card,
  CardBody,
  CardTitle,
  Grid,
  GridItem,
  Tab,
  TabTitleText,
  Tabs,
} from '@patternfly/react-core';
import {
  AgeCell,
  DataTable,
  DescriptionList,
  EmptyState,
  ErrorState,
  LoadingState,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SectionHeader,
  StatusBadge,
} from '../components/ui';
import DebugPanel from '../components/DebugPanel';
import LogViewer from '../components/LogViewer';
import PodEnvironment from '../components/PodEnvironment';
import PodScheduling from '../components/PodScheduling';
import PodMetrics from '../components/PodMetrics';
import PodTerminal from '../components/PodTerminal';
import DeleteDialog from '../components/DeleteDialog';
import { events as eventsApi, pods as podsApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { formatTimestamp, truncate } from '../utils/format';
import { useAsync, useGates } from './_data';
import {
  ActionButton,
  ChipList,
  EditYamlDialog,
  Muted,
  NoClusterState,
  YamlPanel,
} from './_parts';

/**
 * The tabs, in the order an operator reads them: what is it, what is it doing,
 * what is it configured with, what is it saying, what happened to it, and only
 * then the two that reach inside it.
 *
 * `key` is what lands in `?tab=`, so renaming one breaks somebody's bookmark.
 */
const TABS = [
  { key: 'details', title: 'Details' },
  { key: 'metrics', title: 'Metrics' },
  { key: 'yaml', title: 'YAML' },
  { key: 'environment', title: 'Environment' },
  { key: 'logs', title: 'Logs' },
  { key: 'events', title: 'Events' },
  // §31. Beside Events rather than inside Details, because it is the question
  // asked *instead* of reading the details: an operator whose pod will not start
  // is not browsing its labels.
  { key: 'scheduling', title: 'Scheduling' },
  { key: 'terminal', title: 'Terminal' },
  // §7.4. Its own tab rather than a step inside the Terminal: the pod this is
  // for is the one whose image has no shell, so an operator reaching for it has
  // already found that the terminal cannot help them.
  { key: 'debug', title: 'Debug' },
];

const TAB_KEYS = new Set(TABS.map((tab) => tab.key));

function buildChecks(namespace) {
  return [
    { id: 'update', verb: 'update', group: 'core', resource: 'pods', namespace },
    { id: 'delete', verb: 'delete', group: 'core', resource: 'pods', namespace },
    { id: 'logs', verb: 'get', group: 'core', resource: 'pods', subresource: 'log', namespace },
    { id: 'exec', verb: 'create', group: 'core', resource: 'pods', subresource: 'exec', namespace },
    // §7.4. `patch`, matching what `mutate` preflights — a check naming a
    // different verb would report a permission nobody is about to exercise. The
    // subresource is named separately because RBAC does: a ServiceAccount can
    // hold `patch pods` and not this. Appended rather than spliced in, because
    // `usePreflight` pairs results to checks strictly by index (§9).
    {
      id: 'debug',
      verb: 'patch',
      group: 'core',
      resource: 'pods',
      subresource: 'ephemeralcontainers',
      namespace,
    },
  ];
}

/* ── Details ────────────────────────────────────────────────────────────── */

const CONTAINER_COLUMNS = [
  { key: 'name', title: 'Container', sortable: true },
  {
    key: 'state',
    title: 'State',
    sortable: true,
    value: (row) => row.reason || row.state || 'Unknown',
    // `state` alone is not the answer: a Waiting container is waiting *for*
    // something, and `CrashLoopBackOff` is the reason a page like this is open.
    cell: (row) => <StatusBadge status={row.state} detail={row.reason} />,
  },
  {
    key: 'ready',
    title: 'Ready',
    cell: (row) => (row.kind === 'ephemeral' ? <Muted>n/a</Muted> : <span>{row.ready ? 'Yes' : 'No'}</span>),
  },
  { key: 'restarts', title: 'Restarts', sortable: true },
  {
    key: 'last_terminated',
    title: 'Last exit',
    value: (row) => row.last_terminated?.reason ?? '',
    cell: (row) =>
      row.last_terminated ? (
        <span title={row.last_terminated.message ?? undefined}>
          {row.last_terminated.reason ?? `exit ${row.last_terminated.exit_code}`}
          {row.last_terminated.finished_at && (
            <Muted> · {formatTimestamp(row.last_terminated.finished_at)}</Muted>
          )}
        </span>
      ) : (
        // Never restarted is a real fact, not a missing reading, so this is a
        // sentence rather than an em dash.
        <Muted>never</Muted>
      ),
  },
  {
    key: 'resources',
    title: 'Requests / limits',
    value: (row) => `${quantities(row.requests) ?? ''} ${quantities(row.limits) ?? ''}`,
    cell: (row) => (
      <span>
        <NullableCell
          value={quantities(row.requests)}
          reason="This container declares no requests — it is BestEffort, and first in line to be evicted."
        />
        <Muted> / </Muted>
        <NullableCell
          value={quantities(row.limits)}
          reason="This container declares no limits — it can use as much as the node has."
        />
      </span>
    ),
  },
  {
    key: 'ports',
    title: 'Ports',
    value: (row) => (row.ports ?? []).map((port) => port.container_port).join(' '),
    cell: (row) =>
      row.ports?.length ? (
        <span>{row.ports.map((port) => `${port.container_port}/${port.protocol}`).join(', ')}</span>
      ) : (
        <Muted>none declared</Muted>
      ),
  },
  {
    key: 'image',
    title: 'Image',
    cell: (row) => <span title={row.image ?? undefined}>{truncate(row.image, 48)}</span>,
  },
];

/** `{cpu: "500m", memory: "1Gi"}` → `cpu 500m · memory 1Gi`, or `null`. */
function quantities(map) {
  const entries = Object.entries(map ?? {});
  if (!entries.length) return null;
  return entries.map(([key, value]) => `${key} ${value}`).join(' · ');
}

function DetailsTab({ pod }) {
  const conditions = pod.conditions ?? [];
  return (
    <Grid hasGutter>
      <GridItem lg={6} md={12}>
        <Card isFullHeight>
          <CardTitle>Pod</CardTitle>
          <CardBody>
            <DescriptionList
              items={[
                { label: 'Namespace', value: pod.namespace },
                {
                  label: 'Node',
                  value: pod.node ? (
                    <ResourceLink kind="Node" name={pod.node} />
                  ) : (
                    <NullableCell value={null} reason="This pod has not been scheduled to a node yet." />
                  ),
                },
                { label: 'Pod IP', value: <NullableCell value={pod.ip} reason="The kubelet has not reported an address for this pod yet." /> },
                { label: 'Host IP', value: <NullableCell value={pod.host_ip} reason="This pod is not on a node yet." /> },
                {
                  label: 'Controlled by',
                  value: pod.owner?.name ? (
                    <ResourceLink kind={pod.owner.kind} name={pod.owner.name} namespace={pod.namespace} />
                  ) : (
                    // A pod with no controller is a real and important state —
                    // nothing will recreate it — so it is a sentence, not a dash.
                    <Muted>nothing — this pod is not managed by a controller</Muted>
                  ),
                },
                { label: 'QoS class', value: pod.qos_class },
                { label: 'Service account', value: pod.service_account },
                { label: 'Restart policy', value: pod.restart_policy },
                { label: 'Priority class', value: pod.priority_class || <Muted>none</Muted> },
                { label: 'Started', value: formatTimestamp(pod.start_time) },
                { label: 'Age', value: <AgeCell seconds={pod.age_seconds} /> },
                {
                  label: 'Being deleted',
                  hidden: !pod.deleted_at,
                  value: `since ${formatTimestamp(pod.deleted_at)}`,
                },
              ]}
            />
          </CardBody>
        </Card>
      </GridItem>

      <GridItem lg={6} md={12}>
        <Card isFullHeight>
          <CardTitle>Labels, selectors and volumes</CardTitle>
          <CardBody>
            <DescriptionList
              items={[
                {
                  label: 'Labels',
                  value: (
                    <ChipList
                      values={Object.entries(pod.labels ?? {}).map(([key, value]) => `${key}=${value}`)}
                      max={6}
                      emptyText="none"
                    />
                  ),
                },
                {
                  label: 'Node selector',
                  value: (
                    <ChipList
                      values={Object.entries(pod.node_selector ?? {}).map(([key, value]) => `${key}=${value}`)}
                      max={4}
                      emptyText="none"
                    />
                  ),
                },
                { label: 'Host network', value: pod.host_network ? 'Yes' : 'No' },
                {
                  label: 'Volumes',
                  value: (
                    <ChipList
                      values={(pod.volumes ?? []).map((volume) =>
                        volume.source ? `${volume.name} (${volume.kind}: ${volume.source})` : `${volume.name} (${volume.kind ?? 'unknown'})`,
                      )}
                      max={4}
                      emptyText="none"
                    />
                  ),
                },
                {
                  label: 'Annotations',
                  value: <Muted>{Object.keys(pod.annotations ?? {}).length} set</Muted>,
                },
                {
                  label: 'Status message',
                  hidden: !pod.status_message && !pod.status_reason,
                  value: `${pod.status_reason ?? ''} ${pod.status_message ?? ''}`.trim(),
                },
              ]}
            />
          </CardBody>
        </Card>
      </GridItem>

      <GridItem span={12}>
        <SectionHeader
          title="Containers"
          description="Init containers are listed separately below: a Terminated init container with exit code 0 is a success, and the same words on an app container are an outage."
        />
        <DataTable
          ariaLabel="Containers"
          tableId="pods:containers"
          columns={CONTAINER_COLUMNS}
          rows={pod.containers ?? []}
          rowKey={(row) => `${row.kind}/${row.name}`}
          emptyTitle="This pod declares no containers"
        />
      </GridItem>

      {(pod.init_containers ?? []).length > 0 && (
        <GridItem span={12}>
          <SectionHeader title="Init containers" />
          <DataTable
            ariaLabel="Init containers"
            tableId="pods:init-containers"
            columns={CONTAINER_COLUMNS}
            rows={pod.init_containers}
            rowKey={(row) => `init/${row.name}`}
          />
        </GridItem>
      )}

      <GridItem span={12}>
        <SectionHeader
          title="Conditions"
          description="`Unknown` is not `False`: it means the kubelet has said nothing, which is what a node that stopped reporting looks like."
        />
        <DataTable
          ariaLabel="Pod conditions"
          tableId="pods:conditions"
          columns={[
            { key: 'type', title: 'Condition', sortable: true },
            {
              key: 'status',
              title: 'Status',
              cell: (row) => <StatusBadge status={row.status} label={row.status} />,
            },
            { key: 'reason', title: 'Reason', cell: (row) => row.reason ?? <Muted>none given</Muted> },
            {
              key: 'last_transition',
              title: 'Since',
              cell: (row) => formatTimestamp(row.last_transition) ?? <Muted>unknown</Muted>,
            },
            {
              key: 'message',
              title: 'Message',
              cell: (row) => <span title={row.message ?? undefined}>{truncate(row.message, 70)}</span>,
            },
          ]}
          rows={conditions}
          rowKey={(row) => row.type}
          emptyTitle="No conditions reported"
          emptyDescription="The kubelet has not written any conditions for this pod, which is normal for a pod that has only just been created."
        />
      </GridItem>
    </Grid>
  );
}

/* ── Events ─────────────────────────────────────────────────────────────── */

/**
 * This pod's events, server-side filtered to it.
 *
 * The filter is `involvedObjectKind`/`involvedObjectName` rather than a
 * client-side pass over the namespace's events, because §5 scans a bounded
 * window: filtering afterwards would mean "the events about this pod among the
 * newest 200 in the namespace", which on a busy namespace is usually none, with
 * nothing on screen to say so.
 */
function EventsTab({ namespace, name, clusterId }) {
  const listing = useAsync(
    () =>
      eventsApi.list({
        namespace,
        involvedObjectKind: 'Pod',
        involvedObjectName: name,
        limit: 100,
      }),
    { key: `pod-events:${clusterId}:${namespace}/${name}` },
  );

  return (
    <>
      <PartialBanner unavailable={listing.data?.unavailable} />
      <DataTable
        ariaLabel="Pod events"
        tableId="pods:events"
        columns={[
          {
            key: 'type',
            title: 'Type',
            sortable: true,
            cell: (row) => <StatusBadge status={row.type} />,
          },
          { key: 'reason', title: 'Reason', sortable: true },
          {
            key: 'message',
            title: 'Message',
            cell: (row) => <span title={row.message ?? undefined}>{truncate(row.message, 120)}</span>,
          },
          {
            key: 'count',
            title: 'Count',
            // `null`, never `0`: an event that occurred zero times is not an
            // event, so a dash here means the object carried no count.
            cell: (row) => <NullableCell value={row.count} />,
          },
          {
            key: 'last_seen',
            title: 'Last seen',
            cell: (row) => formatTimestamp(row.last_seen) ?? <Muted>unknown</Muted>,
          },
        ]}
        rows={listing.data?.items ?? []}
        rowKey={(row, index) => `${row.reason}-${row.last_seen}-${index}`}
        loading={listing.loading}
        error={listing.error}
        onRetry={listing.reload}
        emptyTitle="No recent events for this pod"
        emptyDescription={
          'The API server keeps events for about an hour by default, so this is "nothing recently", ' +
          'never "nothing ever".'
        }
      />
    </>
  );
}

/* ── Page ───────────────────────────────────────────────────────────────── */

export default function PodDetail() {
  const { namespace, name } = useParams();
  const { activeClusterId } = useCluster();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [dialog, setDialog] = useState(null); // 'edit' | 'delete'

  // The tab lives in the URL so every tab is a link — the pod table opens this
  // page straight on Logs or Terminal, and an operator can paste a colleague
  // the exact panel they are looking at. An unrecognised value falls back to
  // Details rather than rendering nothing.
  const requested = searchParams.get('tab');
  const tab = TAB_KEYS.has(requested) ? requested : 'details';

  const selectTab = useCallback(
    (key) => {
      const next = new URLSearchParams(searchParams);
      next.set('tab', key);
      // Replace, not push: eight tabs would otherwise put eight entries in the
      // history and make Back mean "the tab before this one" rather than "the
      // page I came from".
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const detail = useAsync(() => podsApi.detail(namespace, name), {
    key: `pod:${activeClusterId}:${namespace}/${name}`,
    enabled: activeClusterId != null && Boolean(namespace) && Boolean(name),
  });

  const checks = useMemo(() => buildChecks(namespace), [namespace]);
  const { gate } = useGates(checks, { enabled: activeClusterId != null });

  const pod = detail.data;
  const containers = pod?.containers;

  // The pod's own containers, for §7.4's target picker and its name-collision
  // check. An ephemeral container is neither a valid target nor a name it should
  // suggest, and §6's `kind` is what tells them apart.
  const ownContainers = useMemo(
    () => (containers ?? []).filter((entry) => (entry.kind ?? 'container') === 'container').map((entry) => entry.name),
    [containers],
  );
  const initContainers = useMemo(
    () => (pod?.init_containers ?? []).map((entry) => entry.name),
    [pod],
  );

  const breadcrumbs = [{ label: 'Pods', to: '/pods' }, { label: `${namespace}/${name}` }];

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title={name ?? 'Pod'} breadcrumbs={breadcrumbs} />
        <NoClusterState what="This pod" />
      </>
    );
  }

  if (detail.error) {
    return (
      <>
        <PageHeader title={name} breadcrumbs={breadcrumbs} />
        <ErrorState
          title={`Pod ${namespace}/${name} could not be read`}
          error={detail.error}
          onRetry={detail.reload}
        />
      </>
    );
  }

  const execGate = gate('exec');
  const debugGate = gate('debug');
  const logsGate = gate('logs', { requiresWrite: false });

  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={breadcrumbs}
        subtitle={
          pod
            ? `${pod.namespace} · ${pod.ready} ready${pod.node ? ` · on ${pod.node}` : ' · not scheduled'}`
            : 'Reading the cluster…'
        }
        badge={
          pod ? (
            // §6: `phase_detail` wins. A `Running` pod whose container is
            // CrashLoopBackOff must not show a green pill on the page somebody
            // opened to find out what is wrong with it.
            <StatusBadge
              status={pod.phase}
              detail={pod.phase_detail}
              tooltip={
                pod.phase_detail && pod.phase_detail !== pod.phase
                  ? `The pod's phase is "${pod.phase}", but its containers say "${pod.phase_detail}". The container state is what is shown.`
                  : undefined
              }
            />
          ) : undefined
        }
        actions={[
          <ActionButton key="edit" gate={gate('update')} onClick={() => setDialog('edit')}>
            Edit YAML
          </ActionButton>,
          <ActionButton key="delete" gate={gate('delete')} isDanger onClick={() => setDialog('delete')}>
            Delete
          </ActionButton>,
        ]}
      />

      <PartialBanner unavailable={pod?.unavailable} />

      <Tabs
        activeKey={tab}
        onSelect={(_event, key) => selectTab(key)}
        aria-label="Pod sections"
        role="region"
        data-testid="pod-tabs"
      >
        {TABS.map((entry) => (
          <Tab
            key={entry.key}
            eventKey={entry.key}
            title={<TabTitleText>{entry.title}</TabTitleText>}
            aria-label={entry.title}
          />
        ))}
      </Tabs>

      <div className="admin-pod-tab" data-testid={`pod-tab-${tab}`}>
        {/* Every tab below needs the pod's containers, so none of them renders
            until the pod is in hand. A Logs picker built from an empty container
            list would ask the operator to choose between nothing. */}
        {!pod ? (
          <LoadingState label="Reading the pod…" />
        ) : tab === 'details' ? (
          <DetailsTab pod={pod} />
        ) : tab === 'metrics' ? (
          <PodMetrics namespace={namespace} name={name} clusterId={activeClusterId} />
        ) : tab === 'yaml' ? (
          <YamlPanel group="core" version="v1" plural="pods" name={name} namespace={namespace} height={620} />
        ) : tab === 'environment' ? (
          <PodEnvironment namespace={namespace} name={name} clusterId={activeClusterId} />
        ) : tab === 'logs' ? (
          logsGate.allowed ? (
            <LogViewer namespace={namespace} name={name} containers={containers} height={560} />
          ) : (
            // Rule 11.4 for a panel rather than a button: the reason is shown,
            // not the panel hidden. A missing tab reads as a console that cannot
            // do this at all.
            <EmptyState title="Logs cannot be read for this pod" description={logsGate.reason} />
          )
        ) : tab === 'events' ? (
          <EventsTab namespace={namespace} name={name} clusterId={activeClusterId} />
        ) : tab === 'scheduling' ? (
          <PodScheduling namespace={namespace} name={name} clusterId={activeClusterId} />
        ) : tab === 'terminal' ? (
          execGate.allowed ? (
            <PodTerminal namespace={namespace} name={name} containers={containers} height={520} />
          ) : (
            <EmptyState title="A terminal cannot be opened for this pod" description={execGate.reason} />
          )
        ) : (
          <DebugPanel
            namespace={namespace}
            name={name}
            gate={debugGate}
            execGate={execGate}
            containers={ownContainers}
            initContainers={initContainers}
            height={460}
          />
        )}
      </div>

      {dialog === 'edit' && (
        <EditYamlDialog
          isOpen
          group="core"
          version="v1"
          plural="pods"
          name={name}
          namespace={namespace}
          kind="Pod"
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            detail.reload();
          }}
        />
      )}

      {dialog === 'delete' && (
        <DeleteDialog
          isOpen
          group=""
          version="v1"
          plural="pods"
          name={name}
          namespace={namespace}
          kind="Pod"
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            // Back to the listing: the object this page is about no longer
            // exists, and leaving the operator on a detail page that now 404s
            // reads as the delete having failed.
            navigate('/pods');
          }}
        />
      )}
    </>
  );
}
