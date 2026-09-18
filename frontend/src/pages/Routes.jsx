/**
 * Routes — §13, one table for every way the cluster is exposed to the outside.
 *
 * An operator asking "what is reachable from outside, and where does it go"
 * does not care whether a given exposure is an OpenShift Route, an Ingress or a
 * Gateway API HTTPRoute. They care very much when the console's answer is
 * missing one of them, which is why this page reads all three and why the
 * `Kind` column is always visible: the row is one exposure, and the column says
 * which API it happens to be written in.
 *
 * Two columns carry the contract weight.
 *
 * **`Admitted` is a tri-state.** `Unknown` is the honest state for an exposure
 * no router has reported on, and it is also — permanently, by design — the
 * state of every Ingress, because the Ingress API has no admission condition at
 * all. Rendering either as "rejected" would send an operator to debug a router
 * that never saw the object; rendering them as "admitted" would be a green row
 * that is a lie.
 *
 * **`Address` is null, never blank-meaning-none.** An exposure with no address
 * is one no controller has claimed — which on a cluster with no ingress
 * controller installed is every one of them. The em dash carries the reason.
 *
 * The backend states sit above the table rather than in `unavailable[]`. A
 * cluster that does not serve `route.openshift.io` is not a cluster whose
 * Routes we failed to read; there are none. Putting that in the §1.2 partial
 * banner would raise it on the Routes page of every non-OpenShift cluster in
 * the world, and a banner that is always up is a banner nobody reads.
 */
import { useMemo, useState } from 'react';
import { Alert, Button } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import DeleteDialog from '../components/DeleteDialog';
import RouteDialog from '../components/RouteDialog';
import RouterPanel from '../components/RouterPanel';
import CertificatePanel from '../components/CertificatePanel';
import { routes as routesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync, useGates } from './_data';
import { ChipList, Muted, NoClusterState } from './_parts';

/**
 * The three columns this table starts with hidden.
 *
 * Ten columns and a row menu do not fit beside the navigation, and unlike most
 * tables here two of these hold URLs — the one thing on this page an operator
 * came to read. Something had to give way so those two could be wide enough to
 * read, and these are the two that give way most cheaply: the termination mode
 * is told in more detail by the Certificates panel directly below this table,
 * which names the certificate each exposure points at and when it expires, and
 * an exposure's age is the least diagnostic fact about it — nothing on this
 * page is answered by "it is 14 days old".
 *
 * `Namespace` is the third, and it is the one to argue about: in "All
 * namespaces" it is a fact you now have to click a row to get. It goes because
 * the alternatives are worse — see below — and because the masthead already
 * carries a namespace scope, so the question it answers has another way to be
 * asked.
 *
 * What does NOT ship hidden, and why, since all three are bigger: `Kind`,
 * because the row is one exposure and which API it is written in decides what
 * can be changed about it — this module's own heading says it is always
 * visible; `Goes to`, because "where does it go" is half of what this page is
 * for; and `Managed by`, because an edit to an object a controller owns
 * succeeds, reports `applied: true` truthfully, and is reverted seconds later.
 * That column is the only warning of it anywhere in this console.
 */
const DEFAULT_HIDDEN_COLUMNS = ['tls', 'age_seconds', 'namespace'];

/**
 * §9 checks, asked once for the page.
 *
 * The three route kinds are preflighted separately because they are three
 * different RBAC resources: an account allowed to write Ingresses is very often
 * not allowed to write Routes, and offering one button for all three would
 * disable it for the wrong reason.
 */
const CHECKS = [
  { id: 'create-ingress', verb: 'create', group: 'networking.k8s.io', resource: 'ingresses' },
  { id: 'delete-ingress', verb: 'delete', group: 'networking.k8s.io', resource: 'ingresses' },
  { id: 'create-openshift', verb: 'create', group: 'route.openshift.io', resource: 'routes' },
  { id: 'delete-openshift', verb: 'delete', group: 'route.openshift.io', resource: 'routes' },
  { id: 'create-gateway', verb: 'create', group: 'gateway.networking.k8s.io', resource: 'httproutes' },
  { id: 'delete-gateway', verb: 'delete', group: 'gateway.networking.k8s.io', resource: 'httproutes' },
  // §14. `create deployments` is the narrowest single verb that stands for the
  // whole install: it is the object that actually runs the router, and an
  // account that cannot create it cannot install one no matter what else it
  // holds. The install still preflights every object individually.
  { id: 'router-install', verb: 'create', group: 'apps', resource: 'deployments' },
  { id: 'router-uninstall', verb: 'delete', group: 'apps', resource: 'deployments' },
];

/** The URL scheme an exposure answers on, from its TLS mode. */
function scheme(row) {
  return row.tls?.termination ? 'https' : 'http';
}

function admittedStatus(row) {
  if (row.admitted === true) return 'Ready';
  if (row.admitted === false) return 'NotReady';
  return 'Unknown';
}

export default function Routes() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const [search, setSearch] = useState('');
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const {
    data: capabilities,
    loading: capsLoading,
    reload: reloadCaps,
  } = useAsync(() => routesApi.capabilities(), {
    key: `route-capabilities:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  const { data, loading, error, reload } = useAsync(
    () => routesApi.list({ namespace: namespace || undefined }),
    {
      key: `routes:${activeClusterId}:${namespace ?? ''}`,
      enabled: activeClusterId != null,
    },
  );

  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const rows = data?.items ?? [];
  const backends = capabilities?.items ?? data?.backends ?? [];
  const anyAvailable = backends.some((b) => b.state === 'available');
  const unknownBackends = backends.filter((b) => b.state === 'unknown');
  const unsupportedBackends = backends.filter((b) => b.state === 'unsupported');

  const reloadAll = () => {
    reload();
    reloadCaps();
  };

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Name',
        sortable: true,
        // group/version/plural as well as the kind: without the triple
        // `deriveHref` cannot build a URL and the name renders as inert text,
        // which on this page is the one cell an operator most wants to follow.
        cell: (row) => (
          <ResourceLink
            kind={row.kind}
            name={row.name}
            namespace={row.namespace}
            group={row.group}
            version={row.version}
            plural={row.plural}
          />
        ),
      },
      { key: 'namespace', title: 'Namespace', sortable: true },
      {
        key: 'kind',
        title: 'Kind',
        sortable: true,
        // Always visible and always faceted: the row is one exposure, and which
        // API it is written in is the fact that decides what can be changed
        // about it.
        facet: { options: ['Route', 'Ingress', 'HTTPRoute'] },
      },
      {
        key: 'hosts',
        title: 'Address it answers on',
        value: (row) => (row.hosts ?? []).join(' '),
        cell: (row) =>
          (row.hosts ?? []).length ? (
            // Linked, because this column is the one thing on the page an
            // operator wants to *try*. The link is an offer to open it, not a
            // claim that it answers: whether a controller has admitted the
            // exposure is the Admitted column's job, and on a cluster with no
            // wildcard DNS this will not resolve at all. Saying so in the link
            // would be inventing a verdict the console has not read.
            <ChipList
              values={(row.hosts ?? []).map((h) => `${scheme(row)}://${h}${row.path ?? ''}`)}
              hrefFor={(url) => url}
              max={2}
              // A URL has no break opportunity — not at a dot, not at a slash —
              // so one hostname was this column's minimum width and this table
              // was drawn 254px wider than the page it sits in.
              breakAnywhere
            />
          ) : (
            <NullableCell
              value={null}
              reason={
                row.kind === 'HTTPRoute'
                  ? 'This HTTPRoute names no hostnames, so it answers for every hostname its Gateway listener serves.'
                  : row.subdomain
                    ? 'The router generates this hostname from its own domain; it appears here once a router has admitted it.'
                    : 'This exposure names no hostname, so it matches every hostname that reaches its controller.'
              }
            />
          ),
      },
      {
        key: 'targets',
        title: 'Goes to',
        value: (row) => (row.targets ?? []).map((t) => t.service).join(' '),
        cell: (row) => (
          <ChipList
            values={(row.targets ?? []).map(
              (t) =>
                `${t.service}${t.port != null ? `:${t.port}` : ''}` +
                (t.weight != null ? ` (${t.weight})` : ''),
            )}
            max={2}
            emptyText="nothing"
          />
        ),
      },
      {
        key: 'tls',
        title: 'TLS',
        sortable: true,
        value: (row) => row.tls?.termination ?? '',
        facet: { options: ['edge', 'passthrough', 'reencrypt'] },
        cell: (row) =>
          row.tls?.termination ? (
            <StatusBadge
              status="Ready"
              label={row.tls.termination}
              tooltip={
                row.tls.insecurePolicy
                  ? `Plain HTTP: ${row.tls.insecurePolicy}.`
                  : undefined
              }
            />
          ) : row.kind === 'HTTPRoute' ? (
            <NullableCell
              value={null}
              reason="An HTTPRoute has no TLS field — TLS belongs to its Gateway's listener, which is a different object. This is not a claim that the exposure is plaintext."
            />
          ) : (
            <Muted>none</Muted>
          ),
      },
      {
        key: 'admitted',
        title: 'Admitted',
        sortable: true,
        value: (row) => admittedStatus(row),
        facet: { options: ['Ready', 'NotReady', 'Unknown'] },
        cell: (row) => (
          <StatusBadge
            status={admittedStatus(row)}
            label={row.admitted === true ? 'Admitted' : row.admitted === false ? 'Refused' : 'Unknown'}
            tooltip={
              row.admittedDetail ||
              (row.kind === 'Ingress'
                ? 'The Ingress API has no admission condition. Whether a controller took this object is visible only through the address it publishes.'
                : 'No router has reported on this exposure yet.')
            }
          />
        ),
      },
      {
        key: 'address',
        title: 'Published address',
        value: (row) => (row.addresses ?? []).join(' '),
        cell: (row) =>
          (row.addresses ?? []).length ? (
            <ChipList values={row.addresses} max={1} color="blue" breakAnywhere />
          ) : (
            <NullableCell
              value={null}
              reason={
                row.kind === 'HTTPRoute'
                  ? 'An HTTPRoute publishes no address of its own; the address belongs to its Gateway.'
                  : 'No controller has published an address for this exposure. That is what an unclaimed exposure looks like — and also what one on a cluster with no controller looks like.'
              }
            />
          ),
      },
      {
        key: 'managedBy',
        title: 'Managed by',
        sortable: true,
        value: (row) =>
          row.managedBy?.controller
            ? `${row.managedBy.controller.kind} ${row.managedBy.controller.name}`
            : (row.managedBy?.tool ?? (row.managedBy?.marker ? 'a deployment tool' : '')),
        // Not decoration. An edit to an object a controller owns succeeds,
        // reports `applied: true` truthfully, and is reverted seconds later —
        // both halves true at once. The column is where that stops being a
        // surprise discovered afterwards.
        cell: (row) => {
          const managed = row.managedBy ?? {};
          if (managed.controller) {
            return (
              <StatusBadge
                status="Warning"
                label={`${managed.controller.kind} ${managed.controller.name}`}
                tooltip={managed.detail}
              />
            );
          }
          if (managed.tool || managed.marker) {
            return (
              <StatusBadge
                status="Unknown"
                label={managed.tool ?? 'a deployment tool'}
                tooltip={managed.detail}
              />
            );
          }
          return <Muted>nothing — created by hand</Muted>;
        },
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
        <PageHeader title="Routes" />
        <NoClusterState what="Routes" />
      </>
    );
  }

  const createGate = (backendKey) =>
    gate(
      backendKey === 'openshift'
        ? 'create-openshift'
        : backendKey === 'gateway'
          ? 'create-gateway'
          : 'create-ingress',
    );

  return (
    <>
      <PageHeader
        title="Routes"
        subtitle={
          loading
            ? 'Reading the cluster…'
            : `${rows.length} ${rows.length === 1 ? 'exposure' : 'exposures'} across ` +
              `${backends.filter((b) => b.state === 'available').length} of ${backends.length} backends`
        }
      />

      <PartialBanner unavailable={data?.unavailable} />

      {unknownBackends.length > 0 && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="routes-unknown-backends"
          title="Some route kinds could not be checked"
        >
          <ul>
            {unknownBackends.map((b) => (
              <li key={b.backend}>{b.detail}</li>
            ))}
          </ul>
          This is not the same as the cluster not having them. Creating an exposure that already
          exists can claim a hostname twice.
        </Alert>
      )}

      {unsupportedBackends.length > 0 && (
        <Alert
          isInline
          variant="info"
          className="admin-confirm__alert"
          data-testid="routes-unsupported-backends"
          title="Not present on this cluster"
        >
          {unsupportedBackends.map((b) => b.label).join(', ')} — ordinary for a cluster that has
          not installed them, and not an error.
        </Alert>
      )}

      {!anyAvailable && !capsLoading && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="routes-no-backends"
          title="This cluster serves none of the three route kinds"
        >
          There is no API here to write an exposure into. Installing the router below adds the
          IngressClass that makes Ingresses useful; the Ingress API itself is built into
          Kubernetes and should already be present.
        </Alert>
      )}

      <Toolbar ariaLabel="Route controls">
        <Toolbar.Item>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Filter by name, hostname or Service…"
          />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>
            Every exposure the cluster serves, whichever API it is written in.
          </Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button
            variant="primary"
            isAriaDisabled={!anyAvailable}
            onClick={() => setCreating(true)}
            data-testid="routes-create"
          >
            Expose a Service
          </Button>
        </Toolbar.Item>
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh routes" icon={<SyncAltIcon />} onClick={reloadAll} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Routes"
        manageableColumns
        defaultHiddenColumns={DEFAULT_HIDDEN_COLUMNS}
        columns={columns}
        rows={rows}
        rowKey="id"
        loading={loading || capsLoading}
        error={error}
        onRetry={reloadAll}
        filterText={search}
        actions={(row) => [
          {
            title: 'Edit…',
            isDisabled: !createGate(row.backend).allowed,
            onClick: async () => {
              const detail = await routesApi.get(row.backend, row.namespace, row.name);
              setEditing(detail);
            },
          },
          { isSeparator: true },
          {
            title: 'Delete…',
            isDanger: true,
            isDisabled: !gate(
              row.backend === 'openshift'
                ? 'delete-openshift'
                : row.backend === 'gateway'
                  ? 'delete-gateway'
                  : 'delete-ingress',
            ).allowed,
            onClick: () => setDeleting(row),
          },
        ]}
        emptyTitle="Nothing is exposed"
        emptyDescription="The listing succeeded and this cluster has no Routes, Ingresses or HTTPRoutes in scope."
        footer={
          (data?.truncated ?? []).length > 0 ? (
            <div className="admin-table__widths" data-testid="routes-truncated">
              <Muted>
                {data.truncated
                  .map(
                    (t) =>
                      `Showing the first ${t.shown} ${t.kind} objects` +
                      (t.remaining != null ? ` of at least ${t.shown + t.remaining}` : ''),
                  )
                  .join('. ')}
                . Filter to one namespace to see the rest.
              </Muted>
            </div>
          ) : null
        }
      />

      <div style={{ marginTop: '1.5rem' }}>
        <CertificatePanel />
      </div>

      <div style={{ marginTop: '1.5rem' }}>
        <RouterPanel gate={gate} onChanged={reloadAll} />
      </div>

      {creating && (
        <RouteDialog
          isOpen
          capabilities={capabilities}
          defaultNamespace={namespace || ''}
          onClose={() => setCreating(false)}
          onApplied={() => {
            setCreating(false);
            reloadAll();
          }}
        />
      )}

      {editing && (
        <RouteDialog
          isOpen
          capabilities={capabilities}
          existing={editing}
          onClose={() => setEditing(null)}
          onApplied={() => {
            setEditing(null);
            reloadAll();
          }}
        />
      )}

      {deleting && (
        <DeleteDialog
          isOpen
          group={deleting.group}
          version={deleting.version}
          plural={deleting.plural}
          kind={deleting.kind}
          name={deleting.name}
          namespace={deleting.namespace}
          onClose={() => setDeleting(null)}
          onApplied={() => {
            setDeleting(null);
            reloadAll();
          }}
        />
      )}
    </>
  );
}
