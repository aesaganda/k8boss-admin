/**
 * Explorer — the generic browser, and the reason this console can administer a
 * cluster it has never seen.
 *
 * §4: *The console browses **any** resource the cluster serves, via the dynamic
 * client. Typed endpoints exist for the resources that need shaped rows;
 * anything else is reachable here.* So a CRD nobody wrote a page for — a
 * Certificate, a Kafka, an ArgoCD Application — is one navigation away, with its
 * YAML readable and editable through the same audited, dry-run-first path as
 * everything else.
 *
 * The catalog is the canonical *"say which question you failed to answer"* case,
 * and it is why the banner on the first screen is not decoration. §4 again: *A
 * partially-broken discovery (an aggregated APIService that is down) populates
 * `unavailable` with `reason: "unreachable"` and keeps the groups that answered.
 * A missing group must not look like a cluster with fewer resources.* An
 * operator who cannot find `certificates.cert-manager.io` in this list has to be
 * able to tell "this cluster does not have cert-manager" from "cert-manager's
 * APIService is down and we could not enumerate it".
 *
 * Listings here ask for `shape: raw` deliberately. The generic endpoint returns
 * a typed §8 row where one is defined and a trimmed manifest otherwise; a table
 * that had to cope with both would read `row.name ?? row.metadata.name` in every
 * cell. Asking for raw makes every row the same shape — a Kubernetes object —
 * which is what this page is for.
 */
import { useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Button,
  Drawer,
  DrawerActions,
  DrawerCloseButton,
  DrawerContent,
  DrawerContentBody,
  DrawerHead,
  DrawerPanelBody,
  DrawerPanelContent,
  Title,
} from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  EmptyState,
  PageHeader,
  PartialBanner,
  SearchInput,
  Toolbar,
} from '../components/ui';
import DeleteDialog from '../components/DeleteDialog';
import { realGroup, resources as resourcesApi, wireGroup } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { objectAgeSeconds, objectName, objectNamespace, useAsync, useGates, useResourceList } from './_data';
import {
  ActionButton,
  ChipList,
  EditYamlDialog,
  LabelsCell,
  Muted,
  NoClusterState,
  TruncationFooter,
  YamlPanel,
  menuAction,
} from './_parts';

/* ── The catalog ────────────────────────────────────────────────────────── */

function Catalog({ catalog, onOpen }) {
  const [search, setSearch] = useState('');
  const [group, setGroup] = useState(null);

  // Memoised rather than derived inline: `?? []` mints a new array on every
  // render, and the two memos below would then recompute on every keystroke in
  // the search box — over a catalog that is several hundred rows on a cluster
  // with a normal number of CRDs.
  const items = useMemo(() => catalog.data?.items ?? [], [catalog.data]);

  const groups = useMemo(() => {
    const names = new Set(items.map((item) => realGroup(item.group) || 'core'));
    return [...names].sort((a, b) => a.localeCompare(b));
  }, [items]);

  const rows = useMemo(
    () => (group ? items.filter((item) => (realGroup(item.group) || 'core') === group) : items),
    [items, group],
  );

  return (
    <>
      {/* The canonical §4 banner: a group that failed to enumerate is named
          here, so a resource missing from the table below is never silently a
          resource this cluster does not have. */}
      <PartialBanner
        unavailable={catalog.data?.unavailable}
        title="Discovery was incomplete — some API groups could not be enumerated"
      />

      <Toolbar ariaLabel="Catalog filters">
        <Toolbar.Item>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Find a kind, resource or short name…"
            ariaLabel="Search the API catalog"
          />
        </Toolbar.Item>
        <Toolbar.Item>
          <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap', maxWidth: '48rem' }}>
            <Button variant={group == null ? 'primary' : 'secondary'} size="sm" onClick={() => setGroup(null)}>
              {`All groups (${groups.length})`}
            </Button>
            {groups.map((name) => (
              <Button
                key={name}
                variant={group === name ? 'primary' : 'secondary'}
                size="sm"
                onClick={() => setGroup(name)}
              >
                {name}
              </Button>
            ))}
          </div>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh the catalog" icon={<SyncAltIcon />} onClick={catalog.reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="API resources"
        columns={[
          { key: 'kind', title: 'Kind', sortable: true },
          {
            key: 'group',
            title: 'Group / version',
            sortable: true,
            value: (row) => `${realGroup(row.group) || 'core'}/${row.version}`,
            cell: (row) => (
              <span>
                {`${realGroup(row.group) || 'core'}/${row.version}`}
                {/* A CRD serving v1alpha1 and v1 side by side appears twice.
                    Marking the preferred one is the difference between reading
                    the version the cluster actually stores and reading a
                    deprecated view of it. */}
                {row.preferred && <Muted> · preferred</Muted>}
              </span>
            ),
          },
          { key: 'resource', title: 'Resource', sortable: true },
          {
            key: 'namespaced',
            title: 'Scope',
            sortable: true,
            cell: (row) => <Muted>{row.namespaced ? 'Namespaced' : 'Cluster'}</Muted>,
          },
          {
            key: 'verbs',
            title: 'Verbs',
            value: (row) => (row.verbs ?? []).join(','),
            cell: (row) => (
              <ChipList
                values={row.verbs}
                max={4}
                emptyText="none advertised"
                color={(row.verbs ?? []).includes('list') ? 'blue' : 'grey'}
              />
            ),
          },
          {
            key: 'shortNames',
            title: 'Short names',
            value: (row) => (row.shortNames ?? []).join(','),
            cell: (row) => <ChipList values={row.shortNames} max={3} emptyText="none" />,
          },
        ]}
        rows={rows}
        rowKey={(row) => `${row.group}/${row.version}/${row.resource}`}
        loading={catalog.loading}
        error={catalog.error}
        onRetry={catalog.reload}
        filterText={search}
        onRowClick={onOpen}
        actions={(row) => [
          {
            title: (row.verbs ?? []).includes('list') ? 'Browse' : <span title="This resource does not advertise the `list` verb, so it cannot be enumerated.">Browse</span>,
            isDisabled: !(row.verbs ?? []).includes('list'),
            onClick: () => onOpen(row),
          },
        ]}
        emptyTitle="No API resources match"
        emptyDescription="Discovery returned nothing under this filter. If a resource you expect is missing, check the banner above before concluding the cluster does not serve it."
      />
    </>
  );
}

/* ── One resource's objects ─────────────────────────────────────────────── */

export function Listing({ group, version, plural, catalog, initialName, initialNamespace }) {
  const { selected: scope } = useNamespace();
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(
    initialName ? { metadata: { name: initialName, namespace: initialNamespace } } : null,
  );
  const [editTarget, setEditTarget] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);

  const real = realGroup(group);
  const entry = (catalog.data?.items ?? []).find(
    (item) => realGroup(item.group) === real && item.version === version && item.resource === plural,
  );
  // Until discovery has answered we cannot know whether this resource is
  // namespaced. Assuming namespaced would send `namespace=` on a cluster-scoped
  // listing and get an empty result that looks like an empty cluster, so the
  // listing waits for the catalog instead of guessing.
  const namespaced = entry?.namespaced;
  const namespace = namespaced ? scope : null;

  const listing = useResourceList(group, version, plural, {
    namespace,
    shape: 'raw',
    enabled: catalog.data != null && entry != null && (entry.verbs ?? []).includes('list'),
  });

  const checks = useMemo(
    () => [
      { id: 'update', verb: 'update', group, resource: plural, namespace },
      { id: 'delete', verb: 'delete', group, resource: plural, namespace },
    ],
    [group, plural, namespace],
  );
  const { gate } = useGates(checks);

  if (catalog.data && !entry) {
    return (
      <EmptyState
        title={`This cluster does not serve ${plural} in ${real || 'core'}/${version}`}
        description={
          catalog.data.partial
            ? 'Discovery was incomplete, so it is also possible this resource exists and its group could not be enumerated — see the banner on the catalog page.'
            : 'Discovery enumerated every group successfully and this resource was not among them.'
        }
      />
    );
  }

  if (entry && !(entry.verbs ?? []).includes('list')) {
    return (
      <EmptyState
        title={`${entry.kind} cannot be listed`}
        description={`Discovery reports the verbs ${(entry.verbs ?? []).join(', ') || '(none)'} for this resource, and \`list\` is not among them. This is a property of the API, not a permission problem.`}
      />
    );
  }

  const columns = [
    {
      key: 'name',
      title: 'Name',
      sortable: true,
      value: (row) => objectName(row),
      cell: (row) => objectName(row),
    },
    ...(namespaced && !namespace
      ? [{ key: 'namespace', title: 'Namespace', sortable: true, value: (row) => objectNamespace(row) }]
      : []),
    {
      key: 'labels',
      title: 'Labels',
      value: (row) => JSON.stringify(row?.metadata?.labels ?? {}),
      cell: (row) => <LabelsCell labels={row?.metadata?.labels} max={2} />,
    },
    {
      key: 'age',
      title: 'Age',
      sortable: true,
      value: (row) => objectAgeSeconds(row),
      cell: (row) => <AgeCell seconds={objectAgeSeconds(row)} timestamp={row?.metadata?.creationTimestamp} />,
    },
  ];

  const panel = (
    <DrawerPanelContent widths={{ default: 'width_50' }} isResizable>
      <DrawerHead>
        <Title headingLevel="h2" size="lg">
          {objectName(selected) ?? ''}
        </Title>
        <DrawerActions>
          <DrawerCloseButton onClick={() => setSelected(null)} />
        </DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>
        {selected && (
          <>
            <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem' }}>
              <ActionButton gate={gate('update')} onClick={() => setEditTarget(selected)}>
                Edit YAML…
              </ActionButton>
              <ActionButton gate={gate('delete')} isDanger onClick={() => setDeleteTarget(selected)}>
                Delete…
              </ActionButton>
            </div>
            <YamlPanel
              group={group}
              version={version}
              plural={plural}
              name={objectName(selected)}
              namespace={objectNamespace(selected)}
              height={520}
            />
          </>
        )}
      </DrawerPanelBody>
    </DrawerPanelContent>
  );

  const table = (
    <>
      <PartialBanner unavailable={listing.unavailable} />
      <Toolbar ariaLabel="Object filters">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name or label…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>
            {namespaced ? (namespace ? `Namespace: ${namespace}` : 'All namespaces') : 'Cluster-scoped'}
          </Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh" icon={<SyncAltIcon />} onClick={listing.reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel={`${plural} objects`}
        // `plural` alone is not the resource: core/v1/events and
        // events.k8s.io/v1/events are different kinds with the same plural, and
        // they must not share one set of column widths.
        tableId={`explorer:${group}/${version}/${plural}`}
        manageableColumns
        columns={columns}
        rows={listing.items}
        rowKey={(row, index) => `${objectNamespace(row) ?? ''}/${objectName(row) ?? index}`}
        loading={listing.loading || catalog.loading}
        error={listing.error}
        onRetry={listing.reload}
        filterText={search}
        onRowClick={(row) => setSelected(row)}
        actions={(row) => [
          { title: 'View YAML', onClick: () => setSelected(row) },
          menuAction('Edit YAML…', gate('update'), () => {
            setSelected(row);
            setEditTarget(row);
          }),
          menuAction('Delete…', gate('delete'), () => setDeleteTarget(row), { isDanger: true }),
        ]}
        emptyTitle={`No ${plural}`}
        emptyDescription={
          listing.partial
            ? 'Part of this listing could not be read — see the banner above. This is not an empty result.'
            : 'The listing succeeded and this cluster holds none in scope.'
        }
        footer={<TruncationFooter listing={listing} noun={plural} />}
      />
    </>
  );

  return (
    <>
      <Drawer isExpanded={Boolean(selected)} isInline>
        <DrawerContent panelContent={panel}>
          <DrawerContentBody>{table}</DrawerContentBody>
        </DrawerContent>
      </Drawer>

      {editTarget && (
        <EditYamlDialog
          isOpen
          group={group}
          version={version}
          plural={plural}
          name={objectName(editTarget)}
          namespace={objectNamespace(editTarget)}
          kind={entry?.kind}
          onClose={() => setEditTarget(null)}
          onApplied={() => {
            setEditTarget(null);
            listing.reload();
          }}
        />
      )}

      {deleteTarget && (
        <DeleteDialog
          isOpen
          group={real}
          version={version}
          plural={plural}
          name={objectName(deleteTarget)}
          namespace={objectNamespace(deleteTarget)}
          kind={entry?.kind}
          onClose={() => setDeleteTarget(null)}
          onApplied={() => {
            setDeleteTarget(null);
            setSelected(null);
            listing.reload();
          }}
        />
      )}
    </>
  );
}

/* ── Page ───────────────────────────────────────────────────────────────── */

export default function Explorer() {
  const { group, version, plural } = useParams();
  const { activeClusterId } = useCluster();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  // One catalog fetch for both views. App.jsx routes both URLs to this one
  // component precisely so they cannot disagree about which resources the
  // cluster serves.
  const catalog = useAsync(() => resourcesApi.catalog(), {
    key: `catalog:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="API explorer" />
        <NoClusterState what="The API explorer" />
      </>
    );
  }

  if (!plural) {
    return (
      <>
        <PageHeader
          title="API explorer"
          subtitle="Every resource this cluster serves, including the ones this console has no dedicated page for."
        />
        <Catalog
          catalog={catalog}
          onOpen={(item) =>
            navigate(
              `/explorer/${encodeURIComponent(wireGroup(item.group))}/${encodeURIComponent(item.version)}/${encodeURIComponent(item.resource)}`,
            )
          }
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title={plural}
        breadcrumbs={[{ label: 'API explorer', to: '/explorer' }, { label: `${group}/${version}/${plural}` }]}
        subtitle={`apiVersion ${realGroup(group) ? `${realGroup(group)}/${version}` : version}`}
      />
      <Listing
        // Keyed on the triple: navigating from one resource to another must
        // reset the drawer and the accumulated pages, not reinterpret them
        // against a different kind.
        key={`${group}/${version}/${plural}`}
        group={group}
        version={version}
        plural={plural}
        catalog={catalog}
        initialName={searchParams.get('name')}
        initialNamespace={searchParams.get('namespace')}
      />
    </>
  );
}
