/**
 * Operator portal — §16, what this cluster can install and what it already has.
 *
 * Two tabs over two independent reads: the catalog (what the cluster's
 * CatalogSources offer) and the installed operators (Subscriptions joined to
 * what OLM actually did about each). They are separate endpoints, they fail
 * separately, and neither failure is allowed to empty the other.
 *
 * Three tri-states carry the weight of this page, and each of them is the
 * defect standard applied to a column:
 *
 * **`installed` on a catalog row.** `null` means the Subscription listing did
 * not answer. Rendering it as "not installed" is what invites an operator to
 * create a second Subscription on top of one that already exists, leaving two
 * resolutions competing for the same custom resources.
 *
 * **`phase` on an installed row.** `null` covers two different situations — the
 * ClusterServiceVersion could not be read, and OLM has simply not installed
 * anything yet — and `phaseDetail` is the sentence that says which. It never
 * degrades to `Failed`, which would report a healthy operator as broken during
 * an API outage.
 *
 * **`healthy` on a CatalogSource.** `null` until the catalog operator publishes
 * a connection state. A freshly created CatalogSource has no status at all, and
 * calling that unhealthy sends somebody to debug a registry that is still
 * starting.
 *
 * A cluster with no OLM is an ordinary cluster. When every source reports
 * `unsupported` this page says so once, calmly, in blue — the §1.2 rule that
 * rendering an absent API as an error trains people to ignore red. Sources in
 * state `unknown` are a different thing: those are already in `unavailable[]`
 * and the `PartialBanner` reports them, so they are not repeated here.
 */
import { useMemo, useState } from 'react';
import { Alert, Button, Tab, Tabs, TabTitleText, Tooltip } from '@patternfly/react-core';
import { Link } from 'react-router-dom';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  menuAction,
  NullableCell,
  PageHeader,
  PartialBanner,
  SearchInput,
  SectionHeader,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import SubscribeDialog from '../components/SubscribeDialog';
import OlmPanel from '../components/OlmPanel';
import { portal } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync, useGates } from './_data';
import { ChipList, Muted, NoClusterState } from './_parts';

/**
 * §9, asked once for the page.
 *
 * One check, because this feature makes exactly one kind of object. The write
 * still preflights for the specific namespace it is going into; this only
 * decides whether the console offers the action at all, disabled with the
 * reason (rule 11.4) rather than offered and then refused with a 403.
 */
const CHECKS = [
  { id: 'portal-subscribe', verb: 'create', group: 'operators.coreos.com', resource: 'subscriptions' },
  // §33. Asked against the CustomResourceDefinition create, which is phase one
  // of the install and the first thing it would do. It is deliberately NOT the
  // ClusterRole create: that one's review answers yes on almost every cluster
  // and the API server then refuses it at admission (escalation prevention),
  // so gating the button on it would offer an install that cannot work while
  // hiding one that can.
  {
    id: 'olm-install',
    verb: 'create',
    group: 'apiextensions.k8s.io',
    resource: 'customresourcedefinitions',
  },
];

const INSTALLED_STATES = ['Installed', 'Not installed', 'Unknown'];

const INSTALLED_UNKNOWN_REASON =
  'The Subscription listing did not answer, so whether this operator is already installed is ' +
  'unknown. This is NOT "not installed" — subscribing now could put a second Subscription on top ' +
  'of one that already exists.';

/** The tri-state a catalog row's `installed` field is in, as one word. */
function installedState(row) {
  if (row.installed === true) return 'Installed';
  if (row.installed === false) return 'Not installed';
  return 'Unknown';
}

/**
 * What a bounded read left out, under the table it left it out of.
 *
 * `truncated[]` rather than a paging control, because the package server does
 * not implement continuation — a cursor offered here would be one the backend
 * could not honour. The sentence matters more than the count: a package shown
 * as not installed may be installed by a Subscription that was not read.
 */
function TruncationFooter({ truncated, testId }) {
  if (!truncated?.length) return null;
  return (
    <div className="admin-table__widths" data-testid={testId}>
      <Muted>
        {truncated
          .map(
            (entry) =>
              `Showing the first ${entry.shown} ${entry.kind} objects` +
              (entry.remaining != null ? ` of at least ${entry.shown + entry.remaining}` : '') +
              (entry.detail ? `. ${entry.detail}` : ''),
          )
          .join(' ')}
      </Muted>
    </div>
  );
}

/**
 * The CatalogSources these packages came from, and whether each registry is
 * answering.
 *
 * `catalogs === null` is deliberately not the same rendering as an empty list:
 * "this cluster has no catalogs" is a claim, and an empty listing is the only
 * read that supports it.
 *
 * But `null` reaches here for three different reasons and only one of them is a
 * failed read. The backend's `_list()` returns `None` for "the API is not
 * served" as well as for "the read did not answer", so this component cannot
 * tell them apart from `catalogs` alone — it is told, by `source` and
 * `olmAbsent`. Rendering "the listing did not answer" on a cluster that simply
 * does not run OLM reports an outage that is not happening, directly under a
 * notice correctly saying there is no OLM here.
 */
function CatalogsStrip({ catalogs, source, olmAbsent }) {
  // The page-level notice above already says OLM is not installed. A second line
  // repeating it as a failed CatalogSource read would be both noise and wrong.
  if (olmAbsent) return null;

  if (catalogs == null && source?.state === 'unsupported') {
    return (
      <div data-testid="portal-catalogs-unsupported" style={{ marginBlockEnd: '0.75rem' }}>
        <Muted>
          {source.detail} Packages are still listed above; where they came from is not
          something this cluster publishes.
        </Muted>
      </div>
    );
  }
  if (catalogs == null) {
    return (
      <div data-testid="portal-catalogs-unknown" style={{ marginBlockEnd: '0.75rem' }}>
        <Muted>
          The CatalogSource listing did not answer, so which registries these packages come from —
          and whether those registries are healthy — is unknown. That is not the same as this
          cluster having no catalogs.
        </Muted>
      </div>
    );
  }
  if (catalogs.length === 0) {
    return (
      <div data-testid="portal-catalogs" style={{ marginBlockEnd: '0.75rem' }}>
        <Muted>
          The listing succeeded and this cluster has no CatalogSources, so there is nothing to
          install from.
        </Muted>
      </div>
    );
  }
  return (
    <div
      data-testid="portal-catalogs"
      style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', marginBlockEnd: '0.75rem' }}
    >
      {catalogs.map((entry) => (
        <StatusBadge
          key={entry.id}
          // `healthy: null` is grey and says so. It is the state of a
          // CatalogSource the catalog operator has not reported on yet.
          status={entry.healthy === true ? 'Ready' : entry.healthy === false ? 'NotReady' : 'Unknown'}
          label={`${entry.displayName || entry.name}${entry.state ? ` · ${entry.state}` : ''}`}
          tooltip={entry.detail || undefined}
        />
      ))}
    </div>
  );
}

export default function Portal() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const [tab, setTab] = useState('catalog');
  const [search, setSearch] = useState('');
  const [subscribing, setSubscribing] = useState(null);

  const catalog = useAsync(() => portal.catalog(), {
    key: `portal-catalog:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  // Scoped to the masthead's namespace, and keyed on it: the installed view is
  // the one place an operator asks "what is in *this* namespace", and a stale
  // table under a new heading would answer for the previous one.
  const installed = useAsync(() => portal.installed({ namespace: namespace || undefined }), {
    key: `portal-installed:${activeClusterId}:${namespace ?? ''}`,
    enabled: activeClusterId != null,
  });

  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const reloadAll = () => {
    catalog.reload();
    installed.reload();
  };

  // The deployment gate first, then RBAC — the same order as §5.5's node debug
  // panel, and for the same reason: both are "you cannot do this", they send an
  // operator to different systems, and the deployment one is the one they can
  // answer without a cluster admin. `enabled == null` is a third state: nothing
  // has told us yet, which is not permission.
  const enabled = catalog.data?.enabled ?? installed.data?.enabled;
  const enabledDetail = catalog.data?.enabledDetail ?? installed.data?.enabledDetail;
  const subscribeGate = useMemo(() => {
    if (enabled === false) return { allowed: false, reason: enabledDetail };
    if (enabled == null) {
      return {
        allowed: false,
        reason: 'Whether this deployment permits subscribing has not been read yet.',
      };
    }
    return gate('portal-subscribe');
  }, [enabled, enabledDetail, gate]);

  // §33's gate, and it is a *different* switch from the one above:
  // ADMIN_OLM_INSTALL_ENABLED, not ADMIN_PORTAL_INSTALL_ENABLED. Reading the
  // subscribe gate here would offer an install on a deployment that permits
  // subscribing and forbids installing, and disable it on one that does the
  // reverse — both of which send an operator to edit the wrong line of the same
  // file. It rides along on the catalog envelope for the reason `enabled` does:
  // the panel must paint disabled-with-the-reason on first render, not correct
  // itself a round trip later.
  const olmEnabled = catalog.data?.olmInstall ?? installed.data?.olmInstall;
  const olmGate = useMemo(() => {
    if (olmEnabled?.enabled === false) return { allowed: false, reason: olmEnabled.detail };
    if (olmEnabled == null) {
      return {
        allowed: false,
        reason: 'Whether this deployment permits installing OLM has not been read yet.',
      };
    }
    return gate('olm-install');
  }, [olmEnabled, gate]);

  const active = tab === 'catalog' ? catalog : installed;
  const sources = active.data?.sources ?? [];
  // Every source unsupported means one thing and one thing only: OLM is not
  // installed here. A source in `unknown` is a failed read and is already in
  // `unavailable[]`, so it is not counted into this and not reported twice.
  const olmAbsent = sources.length > 0 && sources.every((s) => s.state === 'unsupported');

  const catalogColumns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Package',
        sortable: true,
        value: (row) => `${row.displayName ?? ''} ${row.name ?? ''}`,
        cell: (row) => (
          <>
            <div>{row.displayName || row.name}</div>
            {row.displayName && row.displayName !== row.name ? <Muted>{row.name}</Muted> : null}
          </>
        ),
      },
      {
        key: 'provider',
        title: 'Provider',
        sortable: true,
        cell: (row) =>
          row.provider ? (
            row.providerUrl ? (
              <a href={row.providerUrl} target="_blank" rel="noreferrer noopener">
                {row.provider}
              </a>
            ) : (
              row.provider
            )
          ) : (
            <NullableCell value={null} reason="This package names no provider in its catalog entry." />
          ),
      },
      {
        key: 'catalog',
        title: 'Catalog',
        sortable: true,
        value: (row) => row.catalogDisplayName || row.catalog || '',
        facet: { value: (row) => row.catalog },
        cell: (row) => (
          <>
            <div>{row.catalogDisplayName || row.catalog}</div>
            {row.catalogNamespace ? <Muted>{row.catalogNamespace}</Muted> : null}
          </>
        ),
      },
      {
        key: 'version',
        title: 'Version',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.version}
            reason="The catalog published no CSV description for this package's default channel, which is ordinary for a pruned catalog. It is not version zero."
          />
        ),
      },
      {
        key: 'channels',
        title: 'Channels',
        value: (row) => (row.channels ?? []).join(' '),
        cell: (row) => (
          <ChipList
            values={row.channels ?? []}
            max={2}
            emptyText="none published"
            color={row.defaultChannel ? 'blue' : 'grey'}
          />
        ),
      },
      {
        key: 'categories',
        title: 'Categories',
        value: (row) => (row.categories ?? []).join(' '),
        facet: { value: (row) => row.categories ?? [] },
        cell: (row) => <ChipList values={row.categories ?? []} max={2} emptyText="uncategorised" />,
      },
      {
        key: 'installed',
        title: 'Installed',
        sortable: true,
        value: (row) => installedState(row),
        facet: { options: INSTALLED_STATES },
        cell: (row) => {
          if (row.installed == null) {
            // Rule 11.2, and the one column on this page where getting it wrong
            // writes a second object into somebody's cluster.
            return <NullableCell value={null} reason={INSTALLED_UNKNOWN_REASON} />;
          }
          if (!row.installed) return <Muted>not installed</Muted>;
          const where = (row.installations ?? [])
            .map((sub) => `${sub.namespace}/${sub.name} (${sub.channel ?? 'no channel'})`)
            .join(', ');
          return (
            <StatusBadge
              status="Ready"
              label="Installed"
              tooltip={
                where
                  ? `Subscribed by ${where}.`
                  : 'A Subscription for this package exists on the cluster.'
              }
            />
          );
        },
      },
    ],
    [],
  );

  const installedColumns = useMemo(
    () => [
      {
        key: 'package',
        title: 'Package',
        sortable: true,
        cell: (row) => (
          <>
            <div>{row.package || row.name}</div>
            {row.name !== row.package ? <Muted>Subscription {row.name}</Muted> : null}
          </>
        ),
      },
      { key: 'namespace', title: 'Namespace', sortable: true, facet: true },
      {
        key: 'channel',
        title: 'Channel',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.channel}
            reason="This Subscription names no channel, so OLM follows the package's default — which can change under it."
          />
        ),
      },
      {
        key: 'installedCSV',
        title: 'Installed version',
        sortable: true,
        cell: (row) => (
          // `installedCSV: null` is OLM having installed nothing yet, which is
          // a different thing from a read that failed — `phaseDetail` carries
          // the sentence saying which of the two this row is.
          <NullableCell value={row.installedCSV} reason={row.phaseDetail} />
        ),
      },
      {
        key: 'phase',
        title: 'Phase',
        sortable: true,
        value: (row) => row.phase ?? 'Unknown',
        facet: { options: ['Succeeded', 'Installing', 'Pending', 'Failed', 'Unknown'] },
        cell: (row) =>
          row.phase == null ? (
            <NullableCell value={null} reason={row.phaseDetail} />
          ) : (
            <StatusBadge status={row.phase} tooltip={row.phaseDetail || undefined} />
          ),
      },
      {
        key: 'installPlanApproval',
        title: 'Approval',
        sortable: true,
        facet: { options: ['Automatic', 'Manual'] },
        cell: (row) => (
          <StatusBadge
            // An InstallPlan waiting for approval is the state that looks like a
            // stuck install and is not one, so it gets the colour and the
            // sentence rather than the approval strategy alone.
            status={row.approvalRequired === true ? 'Warning' : 'unknown'}
            label={
              row.approvalRequired === true
                ? `${row.installPlanApproval} — awaiting approval`
                : row.installPlanApproval
            }
            tooltip={row.installPlanDetail || undefined}
          />
        ),
      },
      {
        key: 'conditions',
        title: 'Conditions',
        value: (row) => (row.conditions ?? []).map((c) => `${c.type}=${c.status}`).join(' '),
        cell: (row) => {
          const conditions = row.conditions ?? [];
          if (!conditions.length) return <Muted>none reported</Muted>;
          return (
            <Tooltip
              content={
                <div>
                  {conditions.map((c) => (
                    <div key={c.type}>
                      <strong>
                        {c.type}={c.status}
                      </strong>
                      {c.reason ? ` ${c.reason}` : ''}
                      {c.message ? ` — ${c.message}` : ''}
                    </div>
                  ))}
                </div>
              }
            >
              <span tabIndex={0} data-testid={`portal-conditions-${row.id}`}>
                {conditions.length} reported
              </span>
            </Tooltip>
          );
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
        <PageHeader title="Operator portal" />
        <NoClusterState what="The operator portal" />
      </>
    );
  }

  const catalogRows = catalog.data?.items ?? [];
  const installedRows = installed.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Operator portal"
        subtitle={
          active.loading
            ? 'Reading the cluster…'
            : tab === 'catalog'
              ? `${catalogRows.length} ${catalogRows.length === 1 ? 'package' : 'packages'} offered by this cluster's catalogs`
              : `${installedRows.length} ${installedRows.length === 1 ? 'Subscription' : 'Subscriptions'} in ${namespace || 'all namespaces'}`
        }
      />

      <PartialBanner unavailable={active.data?.unavailable} />

      {olmAbsent && (
        <>
          {/* Blue, once, and never in the PartialBanner. A cluster without
              Operator Lifecycle Manager is an ordinary cluster; §1.2 is explicit
              that rendering an absent API as an error is how red stops meaning
              anything. The panel below offers to change that (§33) and does not
              change the fact that this state is not a fault. */}
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="portal-unsupported"
            title="Operator Lifecycle Manager is not installed on this cluster"
          >
            None of the APIs this page reads — PackageManifests, Subscriptions,
            ClusterServiceVersions — are served here, so there is no catalog to install from and
            nothing to list. That is an ordinary state for a cluster that has not installed OLM, not
            a fault in this console or in the cluster.
          </Alert>

          {/* Rendered only when OLM is genuinely absent, which is `every source
              unsupported` — never when a source is `unknown`. Offering an
              install off the back of a read that failed is how somebody installs
              OLM on top of an OLM. */}
          <OlmPanel gate={olmGate} onChanged={() => active.reload()} />
        </>
      )}

      <Tabs
        activeKey={tab}
        onSelect={(_event, key) => {
          setTab(key);
          // The two tables hold different things; carrying a package filter
          // into the installed list looks like an empty namespace.
          setSearch('');
        }}
        aria-label="Operator portal sections"
        role="region"
        data-testid="portal-tabs"
      >
        <Tab eventKey="catalog" title={<TabTitleText>Catalog</TabTitleText>} aria-label="Catalog" />
        <Tab eventKey="installed" title={<TabTitleText>Installed</TabTitleText>} aria-label="Installed" />
      </Tabs>

      <Toolbar ariaLabel="Operator portal controls">
        <Toolbar.Item>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder={
              tab === 'catalog' ? 'Filter by package, provider or category…' : 'Filter by package or namespace…'
            }
            data-testid="portal-search"
          />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>
            {tab === 'catalog'
              ? 'What this cluster can install, from every catalog it serves.'
              : 'One row per Subscription, joined to what OLM actually installed for it.'}
          </Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button
            variant="plain"
            aria-label="Refresh the operator portal"
            icon={<SyncAltIcon />}
            onClick={reloadAll}
            data-testid="portal-refresh"
          />
        </Toolbar.Item>
      </Toolbar>

      {tab === 'catalog' ? (
        <>
          {catalog.data && (
            <CatalogsStrip
              catalogs={catalog.data.catalogs}
              source={sources.find((s) => s.api === 'catalogsources')}
              olmAbsent={olmAbsent}
            />
          )}
          <DataTable
            ariaLabel="Operator catalog"
            tableId="portal-catalog"
            manageableColumns
            columns={catalogColumns}
            rows={catalogRows}
            rowKey="id"
            loading={catalog.loading}
            error={catalog.error}
            onRetry={catalog.reload}
            filterText={search}
            actions={(row) => [
              menuAction('Subscribe…', subscribeGate, () => setSubscribing(row)),
            ]}
            emptyTitle="No packages"
            emptyDescription={
              olmAbsent
                ? 'The listing succeeded. This cluster does not serve PackageManifests, so there is nothing here to install.'
                : 'The listing succeeded and this cluster’s catalogs offer no packages.'
            }
            footer={
              <TruncationFooter
                truncated={catalog.data?.truncated}
                testId="portal-catalog-truncated"
              />
            }
          />
        </>
      ) : (
        <>
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="portal-removal-note"
            title="Removing an operator takes two deletions"
          >
            Deleting a Subscription stops OLM upgrading the operator. It does <strong>not</strong>{' '}
            uninstall it: the ClusterServiceVersion the Subscription created stays, and the
            operator keeps running from it. Both objects have to go — and this console does not
            delete them from here. Use the API explorer:{' '}
            <Link to="/explorer/operators.coreos.com/v1alpha1/subscriptions">Subscriptions</Link>{' '}
            and{' '}
            <Link to="/explorer/operators.coreos.com/v1alpha1/clusterserviceversions">
              ClusterServiceVersions
            </Link>
            . The CRDs the operator installed, and the custom resources made from them, are left
            behind by both deletions.
          </Alert>

          <DataTable
            ariaLabel="Installed operators"
            tableId="portal-installed"
            manageableColumns
            columns={installedColumns}
            rows={installedRows}
            rowKey="id"
            loading={installed.loading}
            error={installed.error}
            onRetry={installed.reload}
            filterText={search}
            emptyTitle="Nothing is subscribed"
            emptyDescription={
              olmAbsent
                ? 'The listing succeeded. This cluster does not serve Subscriptions, so no operator was installed through OLM here.'
                : `The listing succeeded and there are no Subscriptions in ${namespace || 'any namespace this console may read'}.`
            }
            footer={
              <TruncationFooter
                truncated={installed.data?.truncated}
                testId="portal-installed-truncated"
              />
            }
          />
        </>
      )}

      {sources.some((s) => s.state === 'unsupported') && !olmAbsent && (
        <div style={{ marginBlockStart: '1rem' }} data-testid="portal-partial-sources">
          <SectionHeader title="Not present on this cluster" />
          <Muted>
            {sources
              .filter((s) => s.state === 'unsupported')
              .map((s) => `${s.label}: ${s.detail}`)
              .join(' ')}
          </Muted>
        </div>
      )}

      {subscribing && (
        <SubscribeDialog
          row={subscribing}
          defaultNamespace={namespace || ''}
          onClose={() => setSubscribing(null)}
          onApplied={() => reloadAll()}
        />
      )}
    </>
  );
}
