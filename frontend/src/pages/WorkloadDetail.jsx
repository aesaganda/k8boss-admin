/**
 * WorkloadDetail — §6 `GET /api/workloads/{plural}/{namespace}/{name}`.
 *
 * The workload itself is read outside the backend's `collect`, so a failure here
 * is a real failure and gets an error panel rather than an empty shell. Every
 * *other* block — pods, services, rollout history — is collected independently,
 * which is why they appear as tabs with their own emptiness: a namespace where
 * the console may read Deployments but not Services still renders this page,
 * with one line in the banner saying which question went unanswered.
 *
 * Two subtleties worth stating, because both are places a page like this
 * normally lies:
 *
 * **An empty Pods tab is not always "no pods".** Unlike §5's node detail, which
 * nulls its pod list, §6's detail returns `pods: []` and records the failure in
 * `unavailable[]`. So this page checks the envelope before deciding which
 * sentence to print under an empty table.
 *
 * **A kind with no revision history is not a workload that was never rolled
 * out.** `GET .../rollout` answers `unsupported` for Jobs, CronJobs and
 * ReplicaSets, and the tab says "this kind has no revision history" rather than
 * showing an empty list that implies one.
 */
import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Card, CardBody, CardTitle, Grid, GridItem, Tab, TabTitleText, Tabs } from '@patternfly/react-core';
import {
  AgeCell,
  DataTable,
  DescriptionList,
  EmptyState,
  ErrorState,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  StatusBadge,
} from '../components/ui';
import ScaleDialog from '../components/ScaleDialog';
import RestartDialog from '../components/RestartDialog';
import SuspendDialog from '../components/SuspendDialog';
import RollbackDialog from '../components/RollbackDialog';
import DeleteDialog from '../components/DeleteDialog';
import { workloads as workloadsApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { formatTimestamp, truncate } from '../utils/format';
import { WORKLOAD_KINDS, capabilityGate, useAsync, useGates } from './_data';
import {
  ActionButton,
  ChipList,
  EditYamlDialog,
  ImagesCell,
  Muted,
  NoClusterState,
  PodConsoleModal,
  UsageCell,
  YamlPanel,
  menuAction,
} from './_parts';

function buildChecks(spec, plural, namespace) {
  if (!spec) return [];
  const checks = [
    { id: 'patch', verb: 'patch', group: spec.group, resource: plural, namespace },
    { id: 'update', verb: 'update', group: spec.group, resource: plural, namespace },
    { id: 'delete', verb: 'delete', group: spec.group, resource: plural, namespace },
    { id: 'logs', verb: 'get', group: 'core', resource: 'pods', subresource: 'log', namespace },
    { id: 'exec', verb: 'create', group: 'core', resource: 'pods', subresource: 'exec', namespace },
  ];
  if (spec.scalable) {
    checks.push({
      id: 'scale',
      verb: 'patch',
      group: spec.group,
      resource: plural,
      subresource: 'scale',
      namespace,
    });
  }
  return checks;
}

/** `{cpu: "500m", memory: "1Gi"}` → `cpu 500m · memory 1Gi`, or "none set". */
function quantities(map) {
  const entries = Object.entries(map ?? {});
  if (!entries.length) return null;
  return entries.map(([key, value]) => `${key} ${value}`).join(' · ');
}

export default function WorkloadDetail() {
  const { plural, namespace, name } = useParams();
  const { activeClusterId } = useCluster();
  const navigate = useNavigate();

  const spec = WORKLOAD_KINDS[plural];
  const kind = spec?.kind;

  const [tab, setTab] = useState('pods');
  const [dialog, setDialog] = useState(null); // 'scale' | 'restart' | 'suspend' | 'rollback' | 'edit' | 'delete'
  const [podConsole, setPodConsole] = useState(null);

  const detail = useAsync(() => workloadsApi.detail(plural, namespace, name), {
    key: `workload:${activeClusterId}:${plural}:${namespace}:${name}`,
    enabled: activeClusterId != null && Boolean(spec),
  });

  const rollout = useAsync(() => workloadsApi.rollout(plural, namespace, name), {
    key: `rollout:${activeClusterId}:${plural}:${namespace}:${name}`,
    // Only for kinds that have history. Asking for a Job's rollout would spend a
    // round trip to be told `unsupported`, which the kind table already knows.
    enabled: activeClusterId != null && Boolean(spec?.revisioned),
  });

  const checks = useMemo(() => buildChecks(spec, plural, namespace), [spec, plural, namespace]);
  const { gate } = useGates(checks, { enabled: activeClusterId != null && Boolean(spec) });

  const workload = detail.data?.workload;
  const unavailable = detail.data?.unavailable ?? [];
  const pods = detail.data?.pods ?? [];
  // §6's detail hands back `[]` for an unreadable pod listing and names it here.
  // Without this check the table would print "no pods" over a listing we were
  // refused, which is the same wrong answer §5 goes to the trouble of nulling.
  const podsUnreadable = unavailable.some((entry) => entry.resource === 'pods');

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
      {
        key: 'phase',
        title: 'Status',
        sortable: true,
        value: (row) => row.phase_detail || row.phase,
        cell: (row) => <StatusBadge status={row.phase} detail={row.phase_detail} />,
      },
      { key: 'ready', title: 'Ready' },
      { key: 'restarts', title: 'Restarts', sortable: true },
      {
        key: 'node',
        title: 'Node',
        sortable: true,
        cell: (row) => (row.node ? <ResourceLink kind="Node" name={row.node} /> : <NullableCell value={null} reason="This pod has not been scheduled to a node yet." />),
      },
      { key: 'ip', title: 'IP' },
      { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
    ],
    [],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title={name ?? 'Workload'} breadcrumbs={[{ label: 'Workloads', to: '/workloads' }, { label: name }]} />
        <NoClusterState what="This workload" />
      </>
    );
  }

  if (!spec) {
    return (
      <>
        <PageHeader title={name ?? 'Workload'} breadcrumbs={[{ label: 'Workloads', to: '/workloads' }]} />
        <EmptyState
          title={`"${plural}" is not a workload kind`}
          description={
            'The workload views cover Deployments, StatefulSets, DaemonSets, Jobs, CronJobs and ReplicaSets. ' +
            'Anything else is reachable through the API explorer.'
          }
        />
      </>
    );
  }

  if (detail.error) {
    return (
      <>
        <PageHeader
          title={name}
          breadcrumbs={[{ label: 'Workloads', to: '/workloads' }, { label: `${namespace}/${name}` }]}
        />
        <ErrorState title={`${kind} ${namespace}/${name} could not be read`} error={detail.error} onRetry={detail.reload} />
      </>
    );
  }

  const scaleGate = capabilityGate(kind, 'scale', spec, gate('scale'));
  const restartGate = capabilityGate(kind, 'restart', spec, gate('patch'));
  const suspendGate = capabilityGate(kind, 'suspend', spec, gate('patch'));
  const rollbackGate = capabilityGate(kind, 'rollback', spec, gate('patch'));

  const refreshAll = () => {
    detail.reload();
    if (spec.revisioned) rollout.reload();
  };

  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={[{ label: 'Workloads', to: '/workloads' }, { label: `${namespace}/${name}` }]}
        subtitle={`${kind} in ${namespace}`}
        badge={
          workload ? (
            <span style={{ display: 'inline-flex', gap: '0.35rem' }}>
              <StatusBadge status={workload.status} tooltip={workload.status_reason ?? undefined} />
              {workload.suspended && <StatusBadge status="Suspended" />}
            </span>
          ) : null
        }
        actions={[
          <ActionButton key="scale" gate={scaleGate} onClick={() => setDialog('scale')}>
            Scale…
          </ActionButton>,
          <ActionButton key="restart" gate={restartGate} onClick={() => setDialog('restart')}>
            Restart…
          </ActionButton>,
          <ActionButton key="suspend" gate={suspendGate} onClick={() => setDialog('suspend')}>
            {workload?.suspended ? 'Resume…' : 'Suspend…'}
          </ActionButton>,
          <ActionButton key="rollback" gate={rollbackGate} onClick={() => setDialog('rollback')}>
            Roll back…
          </ActionButton>,
          <ActionButton key="edit" gate={gate('update')} onClick={() => setDialog('edit')}>
            Edit YAML…
          </ActionButton>,
          <ActionButton key="delete" gate={gate('delete')} isDanger onClick={() => setDialog('delete')}>
            Delete…
          </ActionButton>,
        ]}
      />

      <PartialBanner unavailable={unavailable} />

      <Grid hasGutter>
        <GridItem lg={6} md={12}>
          <Card isFullHeight>
            <CardTitle>Status</CardTitle>
            <CardBody>
              <DescriptionList
                items={[
                  {
                    label: 'Replicas',
                    value: (
                      <UsageCell
                        used={workload?.replicas?.ready}
                        total={workload?.replicas?.desired}
                        reason="The controller has not reported its replica status."
                      />
                    ),
                    help: 'Ready over desired, as the controller reports them.',
                  },
                  {
                    label: 'Updated / available',
                    value: (
                      <UsageCell used={workload?.replicas?.updated} total={workload?.replicas?.available} />
                    ),
                  },
                  {
                    label: 'Restarts (24h)',
                    value: (
                      <NullableCell
                        value={workload?.restarts_24h}
                        reason="Pod data was unavailable, so the restart count is unknown — not zero."
                      />
                    ),
                  },
                  { label: 'Why', value: workload?.status_reason ?? null, hidden: !workload?.status_reason },
                  { label: 'Images', value: <ImagesCell images={workload?.images} max={4} /> },
                  { label: 'Age', value: <AgeCell seconds={workload?.age_seconds} /> },
                  {
                    label: 'Schedule',
                    value: workload?.schedule ?? null,
                    hidden: kind !== 'CronJob',
                    help: 'Cron expression, in the CronJob’s own timezone setting.',
                  },
                  {
                    label: 'Last scheduled',
                    value: formatTimestamp(workload?.last_schedule) ?? null,
                    hidden: kind !== 'CronJob',
                  },
                ]}
              />
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={6} md={12}>
          <Card isFullHeight>
            <CardTitle>Pod template</CardTitle>
            <CardBody>
              <DescriptionList
                items={[
                  { label: 'ServiceAccount', value: detail.data?.spec?.serviceAccount ?? null },
                  {
                    label: 'Selector',
                    value: <ChipList values={Object.entries(workload?.selector ?? {}).map(([k, v]) => `${k}=${v}`)} max={3} emptyText="none (expression-only or unset)" />,
                  },
                  {
                    label: 'Node selector',
                    value: (
                      <ChipList
                        values={Object.entries(detail.data?.spec?.nodeSelector ?? {}).map(([k, v]) => `${k}=${v}`)}
                        max={3}
                        emptyText="any node"
                      />
                    ),
                  },
                  {
                    label: 'Tolerations',
                    value: (
                      <ChipList
                        values={(detail.data?.spec?.tolerations ?? []).map(
                          (t) => `${t.key ?? '*'}${t.value ? `=${t.value}` : ''}${t.effect ? `:${t.effect}` : ''}`,
                        )}
                        max={2}
                        emptyText="none"
                      />
                    ),
                  },
                  {
                    label: 'Volumes',
                    value: (
                      <ChipList
                        values={(detail.data?.spec?.volumes ?? []).map((v) => `${v.name}${v.type ? ` (${v.type})` : ''}`)}
                        max={3}
                        emptyText="none"
                      />
                    ),
                  },
                  {
                    label: 'Rollout',
                    value: detail.data?.rollout
                      ? `${detail.data.rollout.strategy ?? 'unknown strategy'}` +
                        (detail.data.rollout.maxSurge != null
                          ? ` · surge ${detail.data.rollout.maxSurge}, unavailable ${detail.data.rollout.maxUnavailable}`
                          : '')
                      : null,
                  },
                ]}
              />
            </CardBody>
          </Card>
        </GridItem>
      </Grid>

      <Tabs
        activeKey={tab}
        onSelect={(_event, key) => setTab(key)}
        aria-label="Workload sections"
        style={{ marginTop: '1rem' }}
      >
        <Tab eventKey="pods" title={<TabTitleText>{`Pods (${pods.length})`}</TabTitleText>} aria-label="Pods" />
        <Tab eventKey="containers" title={<TabTitleText>Containers</TabTitleText>} aria-label="Containers" />
        <Tab eventKey="conditions" title={<TabTitleText>Conditions</TabTitleText>} aria-label="Conditions" />
        <Tab eventKey="services" title={<TabTitleText>Services</TabTitleText>} aria-label="Services" />
        <Tab eventKey="rollout" title={<TabTitleText>Rollout history</TabTitleText>} aria-label="Rollout history" />
        <Tab eventKey="yaml" title={<TabTitleText>YAML</TabTitleText>} aria-label="YAML" />
      </Tabs>

      {tab === 'pods' &&
        (podsUnreadable && pods.length === 0 ? (
          <ErrorState
            title="The pods for this workload could not be listed"
            detail={
              'This is not a workload with no pods — it is a pod listing we were unable to make. The banner ' +
              'above names the reason.'
            }
            onRetry={detail.reload}
          />
        ) : (
          <DataTable
            ariaLabel="Pods"
            // Distinct from the Pods page, which shares the label: two tables
            // with different columns must not share one set of column widths.
            tableId="workload-detail-pods"
            columns={podColumns}
            rows={pods}
            rowKey={(row) => `${row.namespace}/${row.name}`}
            loading={detail.loading}
            onRetry={detail.reload}
            actions={(row) => [
              menuAction('View logs', gate('logs', { requiresWrite: false }), () =>
                setPodConsole({ pod: row, tab: 'logs' }),
              ),
              menuAction('Open terminal', gate('exec'), () => setPodConsole({ pod: row, tab: 'exec' })),
              { isSeparator: true },
              {
                title: 'Show on its node',
                onClick: () => row.node && navigate(`/nodes/${encodeURIComponent(row.node)}`),
                isDisabled: !row.node,
              },
            ]}
            emptyTitle="No pods"
            emptyDescription={`The pod listing succeeded and matched nothing for this ${kind}.`}
          />
        ))}

      {tab === 'containers' && (
        <DataTable
          ariaLabel="Containers"
          columns={[
            { key: 'name', title: 'Name' },
            {
              key: 'type',
              title: 'Type',
              // Init containers run to completion before the others start, so a
              // failing init container explains a pod that never becomes ready.
              // Mixing them into one undifferentiated list hides that.
              cell: (row) => <Muted>{row.type ?? 'container'}</Muted>,
            },
            { key: 'image', title: 'Image', cell: (row) => <ImagesCell images={[row.image]} max={1} /> },
            {
              key: 'ports',
              title: 'Ports',
              value: (row) => (row.ports ?? []).map((p) => p.containerPort).join(','),
              cell: (row) => (
                <ChipList
                  values={(row.ports ?? []).map((p) => `${p.containerPort}/${p.protocol ?? 'TCP'}`)}
                  max={3}
                  emptyText="none"
                />
              ),
            },
            {
              key: 'requests',
              title: 'Requests',
              cell: (row) => (
                <NullableCell
                  value={quantities(row.resources?.requests)}
                  reason="This container declares no resource requests, so the scheduler treats it as needing nothing."
                />
              ),
            },
            {
              key: 'limits',
              title: 'Limits',
              cell: (row) => <NullableCell value={quantities(row.resources?.limits)} reason="No limits are declared." />,
            },
            {
              key: 'probes',
              title: 'Probes',
              value: (row) => Object.entries(row.probes ?? {}).filter(([, on]) => on).map(([k]) => k).join(','),
              cell: (row) => (
                <ChipList
                  values={Object.entries(row.probes ?? {})
                    .filter(([, on]) => on)
                    .map(([probe]) => probe)}
                  max={3}
                  color="blue"
                  emptyText="none declared"
                />
              ),
            },
            {
              key: 'env_count',
              title: 'Env vars',
              cell: (row) => (
                <span title="Counts spec.env only. envFrom pulls an unknown number of variables from a ConfigMap or Secret and is not included.">
                  {row.env_count}
                </span>
              ),
            },
          ]}
          rows={detail.data?.spec?.containers ?? []}
          rowKey={(row, index) => `${row.name ?? index}`}
          loading={detail.loading}
          emptyTitle="No containers"
          emptyDescription="This workload's pod template declares none, which should not be possible."
        />
      )}

      {tab === 'conditions' && (
        <DataTable
          ariaLabel="Conditions"
          columns={[
            { key: 'type', title: 'Type' },
            { key: 'status', title: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            { key: 'reason', title: 'Reason' },
            {
              key: 'message',
              title: 'Message',
              modifier: 'truncate',
              cell: (row) => <span title={row.message ?? undefined}>{truncate(row.message, 80)}</span>,
            },
            {
              key: 'lastTransitionTime',
              title: 'Since',
              cell: (row) => <AgeCell timestamp={row.lastTransitionTime} />,
            },
          ]}
          rows={detail.data?.conditions ?? []}
          rowKey={(row, index) => `${row.type ?? index}`}
          loading={detail.loading}
          emptyTitle="No conditions reported"
          emptyDescription="This kind's controller publishes no status conditions, or has not published any yet."
        />
      )}

      {tab === 'services' && (
        <DataTable
          ariaLabel="Services"
          // Distinct from the Services tab on the Network page (same label,
          // different columns), so their column widths stay apart.
          tableId="workload-detail-services"
          columns={[
            { key: 'name', title: 'Name' },
            { key: 'type', title: 'Type' },
            { key: 'clusterIP', title: 'Cluster IP' },
            {
              key: 'ports',
              title: 'Ports',
              value: (row) => (row.ports ?? []).map((p) => p.port).join(','),
              cell: (row) => (
                <ChipList
                  values={(row.ports ?? []).map(
                    (p) => `${p.port}${p.targetPort != null ? `→${p.targetPort}` : ''}/${p.protocol ?? 'TCP'}`,
                  )}
                  max={3}
                  emptyText="none"
                />
              ),
            },
          ]}
          rows={detail.data?.services ?? []}
          rowKey={(row, index) => `${row.name ?? index}`}
          loading={detail.loading}
          emptyTitle="No Service selects these pods"
          emptyDescription={
            'Only Services with a selector matching this pod template are listed. A selectorless Service is ' +
            'backed by hand-managed EndpointSlices and does not select these pods, so it is not shown.'
          }
        />
      )}

      {tab === 'rollout' &&
        (!spec.revisioned ? (
          <EmptyState
            title={`A ${kind} has no revision history`}
            description={
              'Kubernetes stores none for this kind, so there is nothing to list and nothing to roll back to. ' +
              'This is not a workload that has never been rolled out.'
            }
          />
        ) : (
          <>
            <PartialBanner unavailable={rollout.data?.unavailable} />
            <DataTable
              ariaLabel="Rollout history"
              columns={[
                {
                  key: 'revision',
                  title: 'Revision',
                  sortable: true,
                  cell: (row) => (
                    <span>
                      {row.revision}
                      {row.revision === rollout.data?.current && <Muted> · current</Muted>}
                    </span>
                  ),
                },
                { key: 'created', title: 'Created', cell: (row) => <AgeCell timestamp={row.created} /> },
                { key: 'images', title: 'Images', cell: (row) => <ImagesCell images={row.images} max={3} /> },
                {
                  key: 'change_cause',
                  title: 'Change cause',
                  cell: (row) => (
                    <NullableCell
                      value={row.change_cause}
                      reason="No kubernetes.io/change-cause annotation was recorded for this revision."
                    />
                  ),
                },
              ]}
              rows={rollout.data?.revisions ?? []}
              rowKey={(row) => String(row.revision)}
              loading={rollout.loading}
              error={rollout.error}
              onRetry={rollout.reload}
              actions={(row) => [
                menuAction(
                  'Roll back to this revision…',
                  row.revision === rollout.data?.current
                    ? { allowed: false, reason: 'This is the revision the workload is already running.' }
                    : rollbackGate,
                  () => setDialog('rollback'),
                ),
              ]}
              emptyTitle="No revision history"
              emptyDescription="The controller keeps history up to revisionHistoryLimit; older revisions are pruned by Kubernetes, not by this console."
            />
          </>
        ))}

      {tab === 'yaml' && (
        <YamlPanel
          group={spec.group}
          version={spec.version}
          plural={plural}
          name={name}
          namespace={namespace}
        />
      )}

      {dialog === 'scale' && (
        <ScaleDialog
          isOpen
          kind={kind}
          plural={plural}
          namespace={namespace}
          name={name}
          current={workload?.replicas?.desired ?? null}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            refreshAll();
          }}
        />
      )}

      {dialog === 'restart' && (
        <RestartDialog
          isOpen
          kind={kind}
          plural={plural}
          namespace={namespace}
          name={name}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            refreshAll();
          }}
        />
      )}

      {dialog === 'suspend' && (
        <SuspendDialog
          isOpen
          kind={kind}
          plural={plural}
          namespace={namespace}
          name={name}
          // Never defaulted: `suspended` is null for kinds that have no such
          // field, and `!null` would be `true` — an empty body that suspends a
          // production CronJob is exactly what the backend refuses to allow.
          suspend={!workload?.suspended}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            refreshAll();
          }}
        />
      )}

      {dialog === 'rollback' && (
        <RollbackDialog
          isOpen
          kind={kind}
          plural={plural}
          namespace={namespace}
          name={name}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            refreshAll();
          }}
        />
      )}

      {dialog === 'edit' && (
        <EditYamlDialog
          isOpen
          group={spec.group}
          version={spec.version}
          plural={plural}
          namespace={namespace}
          name={name}
          kind={kind}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            refreshAll();
          }}
        />
      )}

      {dialog === 'delete' && (
        <DeleteDialog
          isOpen
          group={spec.group}
          version={spec.version}
          plural={plural}
          namespace={namespace}
          name={name}
          kind={kind}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            // The object is gone; staying on its detail page would poll a 404.
            navigate('/workloads');
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
