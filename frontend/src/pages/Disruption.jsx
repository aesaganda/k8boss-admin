/**
 * Disruption — §28 `GET /api/disruption/budgets`.
 *
 * A PodDisruptionBudget is the object whose failure mode is silence. Three
 * things are true of a broken one and visible on no other screen:
 *
 * - **It may cover nothing.** A selector one label key away from the workload
 *   it was written for matches no pod. The YAML is indistinguishable from a
 *   budget guarding a production database, `kubectl describe` prints the same
 *   fields, and a team believes it has availability protection it does not
 *   have. `selected_pods: 0` is that finding — and `null`, which is a pod
 *   listing that did not answer, is drawn as an em dash instead, because
 *   rendering it as zero gets a *working* budget deleted as dead.
 *
 * - **It may never allow an eviction.** `maxUnavailable: 0`, or `minAvailable`
 *   at the pod count, is arithmetic: no voluntary eviction can ever succeed.
 *   That is a different row from `disruptionsAllowed: 0`, which is the
 *   controller's last written count and usually clears on its own. Collapsing
 *   the two either panics somebody about a healthy budget or hides a permanent
 *   one, so they are separate findings with separate colours.
 *
 * - **Two budgets may cover one pod.** Kubernetes does not support that, and
 *   the way it does not is the point: the eviction API refuses that pod
 *   outright, whatever either budget says. Both objects report
 *   `disruptionsAllowed: 1` and look perfectly healthy while a drain over them
 *   cannot finish. This page is the only place either object mentions the
 *   other.
 *
 * **Nothing here says "enforced".** The eviction subresource is the enforcer;
 * these are the objects it consults, as a controller last wrote them. §5's
 * drain plan asks the same objects the other question — will *this* eviction be
 * refused — and answers it per pod, at the drain.
 */
import { useMemo } from 'react';
import { Alert, Card, CardBody } from '@patternfly/react-core';
import {
  AgeCell,
  DataTable,
  EmptyState,
  ErrorState,
  NullableCell,
  PageHeader,
  PartialBanner,
  SectionHeader,
  StatusBadge,
} from '../components/ui';
import { disruption as disruptionApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync } from './_data';
import { Muted, NoClusterState } from './_parts';

/** Findings that mean "this will never work" rather than "not right now". */
const PERMANENT = new Set([
  'pdb_never_allows_disruption',
  'pdb_no_constraint',
  'pdb_selects_nothing',
  'pdb_overlaps',
]);

function constraint(row) {
  if (row.min_available !== null && row.min_available !== undefined) {
    return <code>minAvailable: {String(row.min_available)}</code>;
  }
  if (row.max_unavailable !== null && row.max_unavailable !== undefined) {
    return <code>maxUnavailable: {String(row.max_unavailable)}</code>;
  }
  // Neither set. The API server accepts it and the controller permits every
  // eviction, which is the `pdb_no_constraint` finding rather than a blank.
  return <Muted>neither set</Muted>;
}

function FindingList({ findings }) {
  if (!findings?.length) {
    return <Muted>nothing to report</Muted>;
  }
  return (
    <span data-testid="pdb-findings">
      {findings.map((finding) => (
        <StatusBadge
          key={finding.code}
          status={PERMANENT.has(finding.code) ? 'failed' : 'Unknown'}
          label={finding.label}
          tooltip={finding.detail}
        />
      ))}
    </span>
  );
}

export default function Disruption() {
  const { activeClusterId } = useCluster();
  const { selected } = useNamespace();

  const { data, loading, error, reload } = useAsync(
    () => disruptionApi.budgets(selected ? { namespace: selected } : undefined),
    {
      key: `disruption:${activeClusterId}:${selected ?? '*'}`,
      enabled: activeClusterId != null,
    },
  );

  const rows = useMemo(() => data?.items ?? [], [data]);
  const overlaps = data?.overlappingPods;

  const columns = useMemo(
    () => [
      { key: 'name', title: 'Budget', sortable: true },
      { key: 'namespace', title: 'Namespace', sortable: true },
      { key: 'constraint', title: 'Declares', cell: constraint, value: (row) =>
        String(row.min_available ?? row.max_unavailable ?? '') },
      {
        key: 'selected_pods',
        title: 'Covers',
        sortable: true,
        cell: (row) => (
          <span data-testid={`pdb-covers-${row.name}`}>
            <NullableCell
              value={row.selected_pods}
              reason={
                row.undecidable_pods
                  ? 'This budget uses a selector operator the console cannot evaluate, so how many pods it covers is unknown — not zero.'
                  : 'The pod listing did not answer, so how many pods this budget covers is unknown. It is not zero.'
              }
              format={(value) => `${value} pod${value === 1 ? '' : 's'}`}
            />
          </span>
        ),
      },
      {
        key: 'disruptions_allowed',
        title: 'Evictions allowed',
        sortable: true,
        cell: (row) => (
          <span data-testid={`pdb-allowed-${row.name}`}>
            <NullableCell
              value={row.disruptions_allowed}
              reason="The disruption controller has not written a count yet. That is a fresh budget, not a blocking one."
            />
          </span>
        ),
      },
      {
        key: 'findings',
        title: 'Findings',
        cell: (row) => <FindingList findings={row.findings} />,
        value: (row) => (row.findings ?? []).length,
      },
      { key: 'age_seconds', title: 'Age', sortable: true,
        cell: (row) => <AgeCell seconds={row.age_seconds} /> },
    ],
    [],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Disruption budgets" />
        <NoClusterState what="Disruption budgets" />
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader title="Disruption budgets" />
        <ErrorState
          title="Disruption budgets could not be read"
          error={error}
          onRetry={reload}
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Disruption budgets"
        subtitle={
          selected
            ? `What PodDisruptionBudgets in ${selected} actually cover, and which of them can never allow an eviction.`
            : 'What every PodDisruptionBudget actually covers, and which of them can never allow an eviction.'
        }
      />

      <PartialBanner unavailable={data?.unavailable} />

      {/* The finding no single budget can carry, so it goes above the table
          rather than inside it. `null` is not an empty list: it means the pod
          listing failed, and saying "no overlaps" there would be the console
          vouching for a check it never completed. */}
      {overlaps === null && (
        <Alert
          isInline
          variant="warning"
          data-testid="pdb-overlaps-unknown"
          title="Whether any pod is covered by two budgets is unknown"
        >
          The pod listing did not answer, so this could not be checked. It is{' '}
          <strong>not</strong> a report that no pod is doubly covered — and a pod
          that is cannot be evicted by anyone, whatever either budget says.
        </Alert>
      )}

      {overlaps?.length > 0 && (
        <Alert
          isInline
          variant="danger"
          data-testid="pdb-overlaps"
          title={`${overlaps.length} pod${overlaps.length === 1 ? ' is' : 's are'} covered by more than one budget`}
        >
          <p>
            Kubernetes does not support overlapping budgets. The eviction API
            refuses these pods <strong>outright</strong>, whatever either
            budget&apos;s <code>disruptionsAllowed</code> says — so a node drain
            over them fails on that pod, and nothing on either object explains
            why.
          </p>
          <ul>
            {overlaps.map((entry) => (
              <li key={entry.pod}>
                <code>{entry.pod}</code> — {entry.budgets.join(', ')}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      <Card>
        <CardBody>
          <SectionHeader
            title="Budgets"
            description="“Covers” is counted from a live pod listing, not from the budget's own status. An em dash means the count is unknown — never that it is zero, which is the finding that gets a working budget deleted."
          />
          <DataTable
            ariaLabel="Pod disruption budgets"
            tableId="disruption-budgets"
            columns={columns}
            rows={rows}
            rowKey={(row) => `${row.namespace}/${row.name}`}
            loading={loading}
            onRetry={reload}
            empty={
              <EmptyState
                title="No PodDisruptionBudgets in this scope"
                description="The listing succeeded and returned none. That is a finding, not a gap: nothing here constrains voluntary eviction, so a drain evicts these pods freely."
              />
            }
          />
        </CardBody>
      </Card>
    </>
  );
}
