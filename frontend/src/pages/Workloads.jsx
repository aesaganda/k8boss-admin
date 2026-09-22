/**
 * Workloads — §6, the console's flagship list.
 *
 * Six kinds in one table, which is the point: an operator asking "what is broken
 * in prod" does not think in terms of which controller owns the pods. Three
 * things here are contract rather than layout:
 *
 * **`Unknown` is a status, and it is not green.** §6 requires the backend to emit
 * `Unknown` for a workload whose controller status has not been observed.
 * `StatusBadge` renders it grey with an explanation. Nothing on this page maps
 * an unrecognised status to a healthy-looking pill.
 *
 * **`restarts_24h` is `null` when pod data was unavailable, and `0` means zero.**
 * They render as an em dash and as `0` respectively, never as the same thing.
 *
 * **A button a kind cannot support is disabled with the kind's own reason.**
 * A DaemonSet has no scale subresource; saying "you may not scale this" would
 * send an operator to grant a permission that would not help. The permission
 * gate is asked second, and only for actions the kind actually has.
 */
import { Suspense, lazy, useMemo, useState } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { Button, Dropdown, DropdownItem, DropdownList, MenuToggle } from '@patternfly/react-core';
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
import { templatesFor } from '../components/templates';
import MetadataDialog from '../components/MetadataDialog';
import ScaleDialog from '../components/ScaleDialog';
import RestartDialog from '../components/RestartDialog';
import SuspendDialog from '../components/SuspendDialog';
import { workloads as workloadsApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useDensity } from '../contexts/DensityContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { truncate } from '../utils/format';
import {
  KIND_TO_PLURAL,
  LIVE_POLL_MS,
  WORKLOAD_KINDS,
  capabilityGate,
  useAsync,
  useGates,
  withScopeNote,
  workloadChecks,
} from './_data';
import { ImagesCell, Muted, NoClusterState, UsageCell, menuAction } from './_parts';

// Lazy, like the copy in `Layout.jsx`, and for the same reason: this dialog
// carries `objectFormModel.js` and `templates.js`, and a single static import
// of it anywhere pulls both back into the chunk that does the importing. The
// saving only exists if every call site is `lazy`, so this one is too.
const ImportYamlDialog = lazy(() => import('../components/ImportYamlDialog'));

const KIND_OPTIONS = Object.entries(WORKLOAD_KINDS).map(([plural, spec]) => ({
  value: plural,
  label: spec.kind,
}));

/**
 * "Create workload" — one button standing in for six, since this page lists
 * all six kinds side by side rather than dedicating a page to each. Each item
 * is gated on that kind's own `create` check (rule 11.4): a ClusterRole that
 * grants Deployments but not Jobs must show that difference here rather than
 * disabling the whole menu on the first denial it happens to check.
 */
function CreateWorkloadMenu({ gate, onPick }) {
  const [open, setOpen] = useState(false);
  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      onSelect={() => setOpen(false)}
      toggle={(ref) => (
        <MenuToggle
          ref={ref}
          variant="primary"
          onClick={() => setOpen((v) => !v)}
          isExpanded={open}
          data-testid="create-workload-menu"
        >
          Create workload
        </MenuToggle>
      )}
    >
      <DropdownList>
        {Object.entries(WORKLOAD_KINDS).map(([plural, spec]) => {
          const g = gate(`create:${plural}`);
          return (
            <DropdownItem
              key={plural}
              isAriaDisabled={!g.allowed}
              description={!g.allowed ? g.reason : undefined}
              onClick={() => g.allowed && onPick(plural, spec)}
            >
              {spec.kind}
            </DropdownItem>
          );
        })}
      </DropdownList>
    </Dropdown>
  );
}

/**
 * The two columns this table starts with hidden.
 *
 * Nine columns and a row menu do not fit beside the navigation, and these are
 * the two that cost the least to lose. `Schedule` is CronJob-only and reads
 * "n/a" on every other row, which on a mixed table is most of them — a column
 * whose usual answer is "this question does not apply". `Images` is either
 * identical down the whole column or is read on the workload's own page, and
 * it is the same column the Pods table ships hidden for the same reason.
 *
 * A default, not a filter: every row is still here, a search still matches an
 * image, and the Manage columns button says how many are off.
 */
const DEFAULT_HIDDEN_COLUMNS = ['schedule', 'images'];

export default function Workloads() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const { density, setDensity } = useDensity();
  const navigate = useNavigate();

  // The kind filter is the URL, not component state: `/workloads/jobs` is what
  // the sidebar's Jobs entry points at, and a filter nobody can send a link to
  // is the same selector the four tabbed pages took out of a tab strip.
  const { plural } = useParams();
  const kind = plural && Object.hasOwn(WORKLOAD_KINDS, plural) ? plural : null;
  const setKind = (next) => navigate(next ? `/workloads/${next}` : '/workloads');
  const [search, setSearch] = useState('');
  const [scaleTarget, setScaleTarget] = useState(null);
  // `{row, plural, field}` — which workload, and which half of its metadata.
  const [metadataTarget, setMetadataTarget] = useState(null);
  const [restartTarget, setRestartTarget] = useState(null);
  const [suspendTarget, setSuspendTarget] = useState(null);
  const [createTarget, setCreateTarget] = useState(null);

  // Polled for the same reason the detail page is: the rows carry ready-over-
  // desired, and a controller takes seconds to make the scale this page just
  // confirmed true. Silent — the table never reverts to a skeleton under
  // someone reading it, and a dropped poll keeps the last good listing.
  const { data, loading, error, reload } = useAsync(
    () => workloadsApi.list({ namespace, kind: kind ?? undefined }),
    {
      key: `workloads:${activeClusterId}:${namespace ?? '*'}:${kind ?? '*'}`,
      enabled: activeClusterId != null,
      pollMs: LIVE_POLL_MS,
    },
  );

  // `update` is in the batch for §11.14's labels form: it sends the whole
  // object through §4's PUT, so the permission to ask about is the one that
  // call preflights, not the `patch` the scale and restart entries use.
  const checks = useMemo(
    () => workloadChecks(namespace, { verbs: ['create', 'patch', 'scale', 'update'] }),
    [namespace],
  );
  const { gate } = useGates(checks, { enabled: activeClusterId != null });

  const rows = data?.items ?? [];

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Name',
        sortable: true,
        cell: (row) => <ResourceLink kind={row.kind} name={row.name} namespace={row.namespace} />,
      },
      { key: 'kind', title: 'Kind', sortable: true },
      { key: 'namespace', title: 'Namespace', sortable: true },
      {
        key: 'status',
        title: 'Status',
        sortable: true,
        // §6's five, declared rather than discovered, so "Degraded 0" is on the
        // menu during the incident where it matters. `Unknown` is one of them:
        // a controller that has not reported is a state an operator filters
        // for, not an absence to leave off the list.
        facet: { options: ['Healthy', 'Progressing', 'Degraded', 'Suspended', 'Unknown'] },
        cell: (row) => (
          <span className="admin-cell-inline">
            <StatusBadge status={row.status} tooltip={row.status_reason ?? undefined} />
            {row.status_reason && (
              <Muted title={row.status_reason}>{truncate(row.status_reason, 34)}</Muted>
            )}
          </span>
        ),
      },
      {
        key: 'replicas',
        title: 'Ready',
        sortable: true,
        value: (row) => row.replicas?.ready ?? null,
        cell: (row) => (
          <UsageCell
            used={row.replicas?.ready}
            total={row.replicas?.desired}
            reason="The controller has not reported its replica status, so how many are ready is unknown."
          />
        ),
      },
      {
        key: 'images',
        title: 'Images',
        value: (row) => (row.images ?? []).join(' '),
        cell: (row) => <ImagesCell images={row.images} />,
      },
      {
        key: 'restarts_24h',
        title: 'Restarts (24h)',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.restarts_24h}
            reason="Pod data was unavailable for this workload, so its restart count is unknown — not zero."
          />
        ),
      },
      {
        key: 'schedule',
        title: 'Schedule',
        // CronJob-only, and null everywhere else by design (§6). Rendered as a
        // dash rather than blank so an empty cell is never mistaken for a
        // CronJob whose schedule we failed to read.
        cell: (row) =>
          row.kind === 'CronJob' ? (
            <code title={row.last_schedule ? `Last run ${row.last_schedule}` : 'Never run'}>{row.schedule ?? '—'}</code>
          ) : (
            <Muted>n/a</Muted>
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

  // A kind nobody serves is sent to the whole list rather than filtered by a
  // name the cluster has never heard of, which would render an empty table
  // under a heading claiming it is that kind's complete listing.
  if (plural && kind == null) return <Navigate to="/workloads" replace />;

  // The kind's own name when one is filtered, for the same reason the tabbed
  // pages title themselves after their section: "Workloads" over a table of
  // Jobs only says which sidebar entry you are not on.
  const heading = kind ? `${WORKLOAD_KINDS[kind].kind}s` : 'Workloads';

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title={heading} />
        <NoClusterState what="The workload list" />
      </>
    );
  }

  const actionsFor = (row) => {
    const plural = KIND_TO_PLURAL[row.kind];
    const spec = WORKLOAD_KINDS[plural];
    const scale = capabilityGate(row.kind, 'scale', spec, withScopeNote(gate(`scale:${plural}`), namespace));
    const restart = capabilityGate(row.kind, 'restart', spec, withScopeNote(gate(`patch:${plural}`), namespace));
    const suspend = capabilityGate(row.kind, 'suspend', spec, withScopeNote(gate(`patch:${plural}`), namespace));
    return [
      menuAction('Scale…', scale, () => setScaleTarget({ row, plural })),
      menuAction('Restart…', restart, () => setRestartTarget({ row, plural })),
      menuAction(row.suspended ? 'Resume…' : 'Suspend…', suspend, () =>
        setSuspendTarget({ row, plural, suspend: !row.suspended }),
      ),
      menuAction('Edit labels…', withScopeNote(gate(`update:${plural}`), namespace), () =>
        setMetadataTarget({ row, plural, field: 'labels' }),
      ),
      menuAction('Edit annotations…', withScopeNote(gate(`update:${plural}`), namespace), () =>
        setMetadataTarget({ row, plural, field: 'annotations' }),
      ),
      { isSeparator: true },
      {
        title: 'Open workload',
        onClick: () => navigate(`/workloads/${plural}/${row.namespace}/${row.name}`),
      },
    ];
  };

  return (
    <>
      <PageHeader
        title={heading}
        subtitle={
          loading
            ? 'Reading the cluster…'
            : `${rows.length} in ${namespace ? `namespace ${namespace}` : 'all namespaces'}`
        }
        actions={<CreateWorkloadMenu gate={gate} onPick={(plural, spec) => setCreateTarget({ plural, spec })} />}
      />

      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Workload filters">
        <Toolbar.Item>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Filter by name, image, status…"
            ariaLabel="Filter workloads"
          />
        </Toolbar.Item>
        <Toolbar.Item>
          <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
            <Button
              variant={kind == null ? 'primary' : 'secondary'}
              size="sm"
              onClick={() => setKind(null)}
            >
              All kinds
            </Button>
            {KIND_OPTIONS.map((option) => (
              <Button
                key={option.value}
                variant={kind === option.value ? 'primary' : 'secondary'}
                size="sm"
                onClick={() => setKind(option.value)}
              >
                {option.label}
              </Button>
            ))}
          </div>
        </Toolbar.Item>
        <Toolbar.Spacer />
        {/* Beside the refresh control rather than among the filters: density
            changes how the rows are drawn, not which rows are in them, and a
            control that looks like a filter gets read as one. */}
        <Toolbar.Item>
          <DensityToggle value={density} onChange={setDensity} />
        </Toolbar.Item>
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh workloads" icon={<SyncAltIcon />} onClick={reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Workloads"
        density={density}
        manageableColumns
        defaultHiddenColumns={DEFAULT_HIDDEN_COLUMNS}
        columns={namespace ? columns.filter((column) => column.key !== 'namespace') : columns}
        rows={rows}
        rowKey={(row) => `${row.kind}/${row.namespace}/${row.name}`}
        loading={loading}
        error={error}
        onRetry={reload}
        filterText={search}
        onRowClick={(row) =>
          navigate(`/workloads/${KIND_TO_PLURAL[row.kind]}/${row.namespace}/${row.name}`)
        }
        actions={actionsFor}
        emptyTitle="No workloads in scope"
        emptyDescription={
          data?.partial
            ? 'Some kinds could not be listed — see the banner above. This table is not a complete answer.'
            : 'The listing succeeded and returned nothing for this namespace and kind filter.'
        }
      />

      {metadataTarget && (
        <MetadataDialog
          target={{
            group: WORKLOAD_KINDS[metadataTarget.plural]?.group,
            version: WORKLOAD_KINDS[metadataTarget.plural]?.version,
            plural: metadataTarget.plural,
            namespace: metadataTarget.row.namespace,
            name: metadataTarget.row.name,
            kind: metadataTarget.row.kind,
          }}
          field={metadataTarget.field}
          onClose={() => setMetadataTarget(null)}
          onApplied={() => {
            setMetadataTarget(null);
            reload();
          }}
        />
      )}

      {scaleTarget && (
        <ScaleDialog
          isOpen
          plural={scaleTarget.plural}
          kind={scaleTarget.row.kind}
          namespace={scaleTarget.row.namespace}
          name={scaleTarget.row.name}
          // `replicas.desired` verbatim, including when it is null. The dialog
          // treats an unreadable current count as "no number to start from"
          // rather than as zero; defaulting it here would put a scale-to-zero in
          // the box for a workload that is running.
          current={scaleTarget.row.replicas?.desired ?? null}
          onClose={() => setScaleTarget(null)}
          onApplied={() => {
            setScaleTarget(null);
            reload();
          }}
        />
      )}

      {restartTarget && (
        <RestartDialog
          isOpen
          plural={restartTarget.plural}
          kind={restartTarget.row.kind}
          namespace={restartTarget.row.namespace}
          name={restartTarget.row.name}
          onClose={() => setRestartTarget(null)}
          onApplied={() => {
            setRestartTarget(null);
            reload();
          }}
        />
      )}

      {suspendTarget && (
        <SuspendDialog
          isOpen
          plural={suspendTarget.plural}
          kind={suspendTarget.row.kind}
          namespace={suspendTarget.row.namespace}
          name={suspendTarget.row.name}
          suspend={suspendTarget.suspend}
          onClose={() => setSuspendTarget(null)}
          onApplied={() => {
            setSuspendTarget(null);
            reload();
          }}
        />
      )}

      {createTarget && (
        <Suspense fallback={null}>
          <ImportYamlDialog
            isOpen
            title={`Create ${createTarget.spec.kind}`}
            templates={templatesFor({
              apiVersion: `${createTarget.spec.group}/${createTarget.spec.version}`,
              kind: createTarget.spec.kind,
            })}
            onClose={() => setCreateTarget(null)}
            onApplied={() => {
              setCreateTarget(null);
              reload();
            }}
          />
        </Suspense>
      )}
    </>
  );
}
