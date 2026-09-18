/**
 * Namespaces — §5.
 *
 * `pod_count` is `null` (not `0`) when pods could not be listed, and the whole
 * column is one namespace-by-namespace demonstration of rule 11.2: the backend
 * leaves the count at `None` for *every* row when the single all-namespaces pod
 * listing fails, so a cluster where the console lacks `list pods` shows a column
 * of dashes rather than a cluster that appears to be running nothing.
 *
 * Clicking a row sets the console's namespace scope and opens Workloads. The
 * name is a link to the namespace's own page (§17), which is deliberately not a
 * Workloads page filtered by namespace — that list has one home — but the
 * objects no other page shows together: quota usage, limit ranges, the Pod
 * Security level, role bindings and network policies. The two destinations are
 * different questions ("what runs here" versus "what governs here"), so the row
 * and the name go to different places on purpose.
 *
 * "New project" is §17's write: a namespace created together with what governs
 * it, the way `oc new-project` instantiates a project request template. It is
 * gated on `create namespaces` and disabled with the reason, never hidden.
 *
 * "Create Namespace…" beside it is the §4 write, and the two are kept apart
 * rather than merged because they produce different numbers of objects. A
 * project is five writes into a namespace that does not exist yet — the
 * namespace, a quota, a limit range, a role binding and a default-deny policy —
 * and a bare namespace is one. An operator who wanted the second and pressed
 * the first would create four objects nobody asked for; one who wanted the
 * first and pressed the second would get an ungoverned namespace and no sign
 * that anything was missing.
 */
import { Suspense, lazy, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Button } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  NullableCell,
  PageHeader,
  PartialBanner,
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import NewProjectDialog from '../components/NewProjectDialog';
import { templatesFor } from '../components/templates';
import { namespaces as namespacesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync, useGates } from './_data';
import { ActionButton, LabelsCell, Muted, NoClusterState } from './_parts';

// Lazy, like the copy in `Layout.jsx`, and for the same reason: this dialog
// carries `objectFormModel.js` and `templates.js`, and a single static import
// of it anywhere pulls both back into the chunk that does the importing. The
// saving only exists if every call site is `lazy`, so this one is too.
const ImportYamlDialog = lazy(() => import('../components/ImportYamlDialog'));

// `create namespaces` is the first of the five verbs a project needs and the
// one every project needs; the other four are preflighted per object by the
// dialog's own dry run, where a denial names the object it belongs to.
const CHECKS = [{ id: 'create', verb: 'create', group: 'core', resource: 'namespaces' }];

export default function Namespaces() {
  const { activeClusterId } = useCluster();
  const { selected, setSelected, refresh: refreshScope } = useNamespace();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [creating, setCreating] = useState(false);
  const [creatingBare, setCreatingBare] = useState(false);

  const { data, loading, error, reload } = useAsync(() => namespacesApi.list(), {
    key: `namespaces:${activeClusterId}`,
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
        cell: (row) => (
          <span>
            {/* stopPropagation: the row click scopes the console and opens
                Workloads; the name opens the namespace's own page. Letting the
                click bubble would do both, and the second navigation wins. */}
            <Link
              to={`/namespaces/${encodeURIComponent(row.name)}`}
              onClick={(event) => event.stopPropagation()}
              data-testid="namespace-link"
            >
              {row.name}
            </Link>
            {row.name === selected && <Muted> · current scope</Muted>}
          </span>
        ),
      },
      {
        key: 'status',
        title: 'Status',
        sortable: true,
        facet: { options: ['Active', 'Terminating'] },
        cell: (row) => (
          <StatusBadge
            status={row.status}
            tooltip={
              row.status === 'Terminating'
                ? 'This namespace is being deleted. Anything created in it now will disappear with it.'
                : undefined
            }
          />
        ),
      },
      {
        key: 'pod_count',
        title: 'Pods',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.pod_count}
            reason="Pods could not be listed for this cluster, so the count is unknown rather than zero."
          />
        ),
      },
      {
        key: 'labels',
        title: 'Labels',
        value: (row) => JSON.stringify(row.labels ?? {}),
        cell: (row) => <LabelsCell labels={row.labels} max={2} />,
      },
      {
        key: 'age_seconds',
        title: 'Age',
        sortable: true,
        cell: (row) => <AgeCell seconds={row.age_seconds} timestamp={row.creationTimestamp} />,
      },
    ],
    [selected],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Namespaces" />
        <NoClusterState what="The namespace list" />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Namespaces"
        subtitle={loading ? 'Reading the cluster…' : `${rows.length} namespaces`}
        actions={[
          <ActionButton key="create" gate={gate('create')} variant="primary" onClick={() => setCreating(true)}>
            New project…
          </ActionButton>,
          <ActionButton key="create-bare" gate={gate('create')} onClick={() => setCreatingBare(true)}>
            Create Namespace…
          </ActionButton>,
        ]}
      />

      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Namespace controls">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name or label…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>Selecting a row scopes the whole console to that namespace; the name opens what governs it.</Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button
            variant="plain"
            aria-label="Refresh namespaces"
            icon={<SyncAltIcon />}
            onClick={() => {
              reload();
              // The masthead selector reads its own copy of this list. Refreshing
              // only one of them leaves the dropdown offering a namespace the
              // table no longer shows.
              refreshScope();
            }}
          />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Namespaces"
        manageableColumns
        columns={columns}
        rows={rows}
        rowKey="name"
        loading={loading}
        error={error}
        onRetry={reload}
        filterText={search}
        onRowClick={(row) => {
          setSelected(row.name);
          navigate('/workloads');
        }}
        emptyTitle="No namespaces"
        emptyDescription="The listing succeeded and returned none. On a real cluster that cannot happen — check the banner above."
      />

      {creating && (
        <NewProjectDialog
          onClose={() => setCreating(false)}
          onApplied={() => {
            reload();
            refreshScope();
          }}
        />
      )}

      {creatingBare && (
        <Suspense fallback={null}>
          <ImportYamlDialog
            isOpen
            title="Create Namespace"
            templates={templatesFor({ apiVersion: 'v1', kind: 'Namespace' })}
            onClose={() => setCreatingBare(false)}
            onApplied={() => {
              setCreatingBare(false);
              reload();
              // The masthead selector reads its own copy of this list, and a
              // namespace it does not know about is one the operator cannot scope
              // to — which is the first thing they will try to do with it.
              refreshScope();
            }}
          />
        </Suspense>
      )}
    </>
  );
}
