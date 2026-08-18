/**
 * Namespaces — §5.
 *
 * `pod_count` is `null` (not `0`) when pods could not be listed, and the whole
 * column is one namespace-by-namespace demonstration of rule 11.2: the backend
 * leaves the count at `None` for *every* row when the single all-namespaces pod
 * listing fails, so a cluster where the console lacks `list pods` shows a column
 * of dashes rather than a cluster that appears to be running nothing.
 *
 * Clicking a row sets the console's namespace scope and opens Workloads. There
 * is no namespace detail page, and inventing one that repeated Workloads
 * filtered by namespace would be a second place for the same list to disagree
 * with the first.
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
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import { namespaces as namespacesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync } from './_data';
import { LabelsCell, Muted, NoClusterState } from './_parts';

export default function Namespaces() {
  const { activeClusterId } = useCluster();
  const { selected, setSelected, refresh: refreshScope } = useNamespace();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');

  const { data, loading, error, reload } = useAsync(() => namespacesApi.list(), {
    key: `namespaces:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  const rows = data?.items ?? [];

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Name',
        sortable: true,
        cell: (row) => (
          <span>
            {row.name}
            {row.name === selected && <Muted> · current scope</Muted>}
          </span>
        ),
      },
      {
        key: 'status',
        title: 'Status',
        sortable: true,
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
      />

      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Namespace controls">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name or label…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>Selecting a row scopes the whole console to that namespace.</Muted>
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
    </>
  );
}
