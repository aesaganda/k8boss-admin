/**
 * ClusterStatus — §19 `GET /api/cluster-status`.
 *
 * OpenShift puts a health rollup at the top of its console, one row per
 * platform component, from `ClusterOperator` objects. Vanilla Kubernetes has no
 * such API. It has five unrelated ones that between them answer most of the
 * same question, and this page is those five reads joined — leader-election
 * leases, aggregated APIService availability, CRD establishment, admission
 * webhooks, and kubelet version skew.
 *
 * Three rules govern how it renders, and all three are the same rule.
 *
 * **A section that is `null` is a failure panel, never an empty one.** "This
 * cluster has no admission webhooks" and "we could not read the admission
 * webhooks" are answers an operator acts on very differently, and a section
 * that could not be read must not be able to look like the reassuring one.
 *
 * **There is no aggregate verdict, and the page does not invent one.** No
 * traffic light, no "healthy" badge. A stale `cloud-controller-manager` lease
 * and no aggregated APIs is a normal Tuesday on one cluster and an outage on
 * another; a single indicator would have to pick, and picking is the operator's
 * job. The page counts findings and shows them.
 *
 * **A tri-state renders as three things.** `stale: null` is an em dash with the
 * reason, not a green tick; `endpoint_count: null` on a URL-addressed webhook
 * says there is nothing in the cluster to count, because a zero there would
 * read as "its backend is gone" — the one finding this page exists to make.
 */
import { useMemo } from 'react';
import { Card, CardBody, Grid, GridItem } from '@patternfly/react-core';
import {
  DataTable,
  DescriptionList,
  EmptyState,
  ErrorState,
  NullableCell,
  PageHeader,
  PartialBanner,
  SectionHeader,
  StatusBadge,
} from '../components/ui';
import { clusterStatus as clusterStatusApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useAsync } from './_data';
import { Muted, NoClusterState } from './_parts';

/**
 * The failure panel a section renders when its read did not answer.
 *
 * Deliberately the same shape as §17's, and deliberately *not* an empty state:
 * the whole point of this page is that "nothing is wrong here" and "we could
 * not check here" must never look alike.
 */
function SectionUnavailable({ unavailable, resource, what }) {
  const entry = (unavailable ?? []).find((item) => item.resource === resource);
  return (
    <div data-testid={`status-${resource}-unavailable`} className="admin-confirm__alert">
      <StatusBadge status="Unknown" label={entry?.reason || 'unavailable'} />{' '}
      {entry?.reason === 'forbidden'
        ? `The console is not permitted to list ${resource}. `
        : entry?.reason === 'unsupported'
          ? `This cluster does not serve ${resource}. `
          : `The ${resource} listing did not answer. `}
      {what} is unknown, which is not the same as there being nothing wrong with it.
      {entry?.detail ? (
        <pre style={{ whiteSpace: 'pre-wrap', marginTop: '0.5rem' }}>{entry.detail}</pre>
      ) : null}
    </div>
  );
}

/** `12s ago`, or an em dash with the reason when the timestamp was unusable. */
function renewCell(row) {
  if (row.seconds_since_renew == null) {
    return (
      <NullableCell
        value={null}
        reason="This lease carries no renewTime, so it has never been acquired — which is not the same as its holder having stopped."
      />
    );
  }
  return `${row.seconds_since_renew}s ago`;
}

function LeaseTable({ rows }) {
  const columns = useMemo(
    () => [
      { key: 'name', title: 'Lease', sortable: true },
      { key: 'holder', title: 'Held by', sortable: true },
      { key: 'renew', title: 'Last renewed', value: (row) => row.seconds_since_renew ?? -1, cell: renewCell },
      {
        key: 'stale',
        title: 'Renewing',
        value: (row) => (row.stale == null ? '' : row.stale ? 'stale' : 'renewing'),
        cell: (row) =>
          row.stale == null ? (
            <NullableCell value={null} reason="Whether this lease is being renewed could not be decided from what it carries." />
          ) : row.stale ? (
            <StatusBadge
              status="NotReady"
              label="Not renewing"
              tooltip="renewTime is older than the lease's own duration: whoever held this stopped renewing it."
            />
          ) : (
            <StatusBadge status="Ready" label="Renewing" />
          ),
      },
    ],
    [],
  );

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(row) => row.name}
      emptyTitle="No leases in kube-system"
      emptyDescription="The listing succeeded and returned none. On a managed control plane that is ordinary — the scheduler and controller-manager run where you cannot see them."
    />
  );
}

function ApiServiceTable({ rows }) {
  const columns = useMemo(
    () => [
      { key: 'name', title: 'APIService', sortable: true },
      {
        key: 'available',
        title: 'Available',
        value: (row) => (row.available == null ? '' : row.available ? 'yes' : 'no'),
        cell: (row) =>
          row.available == null ? (
            <NullableCell value={null} reason="No Available condition has been written yet. That is not the same as unavailable." />
          ) : row.available ? (
            <StatusBadge status="Ready" label="Available" />
          ) : (
            <StatusBadge status="NotReady" label="Unavailable" tooltip={row.message || row.reason} />
          ),
      },
      {
        key: 'backend',
        title: 'Served by',
        value: (row) => (row.service ? `${row.service.namespace}/${row.service.name}` : ''),
      },
      { key: 'reason', title: 'Reason', cell: (row) => row.reason || <Muted>—</Muted> },
    ],
    [],
  );

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(row) => row.name}
      emptyTitle="No aggregated APIs"
      emptyDescription="Every APIService on this cluster is served by the API server itself. Nothing extends it."
    />
  );
}

function WebhookTable({ rows }) {
  const columns = useMemo(
    () => [
      { key: 'name', title: 'Webhook', sortable: true },
      { key: 'configuration', title: 'Configuration', sortable: true },
      {
        key: 'kind',
        title: 'Type',
        value: (row) => (row.kind.startsWith('Validating') ? 'Validating' : 'Mutating'),
      },
      {
        key: 'failure_policy',
        title: 'On failure',
        sortable: true,
        cell: (row) =>
          row.failure_policy === 'Fail' ? (
            <StatusBadge
              status="Unknown"
              label="Fail"
              tooltip="If this webhook does not answer, the API server refuses the write."
            />
          ) : (
            <StatusBadge status="Ready" label="Ignore" tooltip="If this webhook does not answer, the write is admitted unchecked." />
          ),
      },
      {
        key: 'endpoint_count',
        title: 'Backends',
        value: (row) => row.endpoint_count ?? -1,
        cell: (row) =>
          row.endpoint_count == null ? (
            <NullableCell
              value={null}
              reason={
                row.url
                  ? 'This webhook is addressed by URL, so there is nothing in the cluster to count. It may be answering perfectly well.'
                  : 'The EndpointSlice listing did not answer, so how many backends this webhook has is unknown.'
              }
            />
          ) : row.endpoint_count === 0 && row.failure_policy === 'Fail' ? (
            <StatusBadge
              status="NotReady"
              label="0 — blocking writes"
              tooltip="Nothing is behind this webhook's Service and its failure policy is Fail, so every write it intercepts is being refused."
            />
          ) : (
            String(row.endpoint_count)
          ),
      },
    ],
    [],
  );

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(row) => `${row.configuration}/${row.name}`}
      emptyTitle="No admission webhooks"
      emptyDescription="Nothing is intercepting writes to this cluster."
    />
  );
}

export default function ClusterStatus() {
  const { activeClusterId } = useCluster();
  const { data, loading, error, reload } = useAsync(() => clusterStatusApi.get(), {
    key: `cluster-status:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Cluster status" />
        <NoClusterState what="Cluster status" />
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader title="Cluster status" />
        <ErrorState title="Cluster status could not be read" error={error} onRetry={reload} />
      </>
    );
  }

  const status = data ?? {};
  const webhooks = status.webhooks;
  const skew = status.versionSkew;
  const crds = status.crds;
  const apiServices = status.apiServices;

  const findings = [
    webhooks?.blocking_count,
    apiServices?.unavailable_count,
    crds?.unhealthy_count,
    skew?.out_of_skew_count,
    status.controlPlane?.filter((row) => row.stale === true).length,
  ].filter((count) => typeof count === 'number');
  const total = findings.reduce((sum, count) => sum + count, 0);

  return (
    <>
      <PageHeader
        title="Cluster status"
        subtitle={
          loading && !data
            ? 'Reading the cluster…'
            : // Counted, not judged. The page says how many findings it has and
              // leaves what they mean to the person who knows this cluster.
              `${total} finding${total === 1 ? '' : 's'} across ${findings.length} of 5 checks that answered`
        }
      />

      <PartialBanner unavailable={status.unavailable} />

      <Grid hasGutter>
        <GridItem span={12}>
          <Card>
            <CardBody>
              <SectionHeader
                title="Control plane leases"
                description="Leader-elected components renew a Lease in kube-system every few seconds. One that stopped renewing is what a wedged controller looks like from the API server's side. A component absent from this list is not reported as missing — on a managed control plane it runs where you cannot see it."
              />
              {status.controlPlane == null ? (
                <SectionUnavailable
                  unavailable={status.unavailable}
                  resource="leases"
                  what="Whether the control plane's components are renewing their leases"
                />
              ) : (
                <LeaseTable rows={status.controlPlane} />
              )}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem span={12}>
          <Card>
            <CardBody>
              <SectionHeader
                title="Aggregated APIs"
                description="APIServices served by a pod rather than by the API server. This is where `metrics.k8s.io` goes when the metrics tab empties, and the only place the reason is written down."
              />
              {apiServices == null ? (
                <SectionUnavailable
                  unavailable={status.unavailable}
                  resource="apiservices"
                  what="Which extension APIs are answering"
                />
              ) : (
                <>
                  <ApiServiceTable rows={apiServices.items} />
                  <Muted>
                    {apiServices.local_count} APIService
                    {apiServices.local_count === 1 ? ' is' : 's are'} served by the API server
                    itself and not listed: they are available whenever the cluster is answering
                    at all.
                  </Muted>
                </>
              )}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={6} md={12}>
          <Card isFullHeight>
            <CardBody>
              <SectionHeader
                title="Custom resource definitions"
                description="Only the ones with a problem. A CRD that is not Established serves nothing, and one with a non-structural schema cannot be pruned or converted."
              />
              {crds == null ? (
                <SectionUnavailable
                  unavailable={status.unavailable}
                  resource="customresourcedefinitions"
                  what="Whether every CRD on this cluster is serving"
                />
              ) : crds.items.length === 0 ? (
                <EmptyState
                  title="Every CRD is established"
                  description={`All ${crds.total} of them, with structural schemas.`}
                />
              ) : (
                <DescriptionList
                  items={crds.items.map((row) => ({
                    label: row.name,
                    value: (
                      <>
                        {row.established !== true && (
                          <StatusBadge status="NotReady" label="not established" tooltip={row.message} />
                        )}{' '}
                        {row.non_structural === true && (
                          <StatusBadge status="Unknown" label="non-structural schema" />
                        )}
                      </>
                    ),
                  }))}
                />
              )}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={6} md={12}>
          <Card isFullHeight>
            <CardBody>
              <SectionHeader
                title="Version skew"
                description={
                  skew?.supported_minors_behind
                    ? `A kubelet may be up to ${skew.supported_minors_behind} minor versions behind the API server, and never ahead of it.`
                    : 'How far each kubelet is from the API server.'
                }
              />
              {skew == null ? (
                <SectionUnavailable
                  unavailable={status.unavailable}
                  resource="nodes"
                  what="Whether every kubelet is within the supported skew"
                />
              ) : (
                <div data-testid="status-skew" data-out-of-skew={String(skew.out_of_skew_count)}>
                  <DescriptionList
                    items={[
                      { label: 'API server', value: skew.server_version ?? <Muted>unknown</Muted> },
                    ]}
                  />
                  {skew.nodes == null ? (
                    <SectionUnavailable
                      unavailable={status.unavailable}
                      resource="nodes"
                      what="Each node's kubelet version"
                    />
                  ) : (
                    <DescriptionList
                      items={skew.nodes.map((row) => ({
                        label: row.node,
                        value: (
                          <>
                            {row.kubelet_version ?? <Muted>unknown</Muted>}{' '}
                            {row.status === 'behind' && (
                              <StatusBadge status="NotReady" label="outside supported skew" />
                            )}
                            {row.status === 'ahead' && (
                              <StatusBadge
                                status="NotReady"
                                label="newer than the API server"
                                tooltip="A kubelet is never supported ahead of the API server it talks to."
                              />
                            )}
                            {row.status === 'unknown' && (
                              <NullableCell value={null} reason="One of the two versions could not be read, so the skew is unknown." />
                            )}
                          </>
                        ),
                      }))}
                    />
                  )}
                </div>
              )}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem span={12}>
          <Card>
            <CardBody>
              <SectionHeader
                title="Admission webhooks"
                description="What intercepts writes to this cluster. A webhook with failurePolicy Fail and nothing behind its Service is refusing every write it matches, right now — the most effective way to break a cluster without touching a node."
              />
              {webhooks == null ? (
                <SectionUnavailable
                  unavailable={status.unavailable}
                  resource="validatingwebhookconfigurations"
                  what="What is intercepting writes to this cluster"
                />
              ) : (
                <div data-testid="status-webhooks" data-blocking={String(webhooks.blocking_count)}>
                  {webhooks.complete === false && (
                    <div className="admin-confirm__alert" data-testid="status-webhooks-partial">
                      <StatusBadge status="Unknown" label="incomplete" /> One of the two webhook
                      listings did not answer, so these are not all of them.
                    </div>
                  )}
                  <WebhookTable rows={webhooks.items} />
                </div>
              )}
            </CardBody>
          </Card>
        </GridItem>
      </Grid>
    </>
  );
}
