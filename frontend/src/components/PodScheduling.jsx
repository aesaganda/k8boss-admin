/**
 * PodScheduling — §31, why is this pod Pending.
 *
 * The console used to answer this with the word `Pending`. The real answer
 * exists: the scheduler computed it over its whole predicate chain and wrote it
 * into an event, and it was three clicks away in a list nobody filters.
 *
 * The panel is ordered by what an operator has to know first, and each step
 * refuses a reading the data would otherwise invite:
 *
 * **Which question is this.** A Pending pod with a node already assigned has
 * been *placed*, and everything wrong with it is on that machine — an image, a
 * volume, an init container. Only `waiting_on: "scheduler"` is a scheduling
 * problem, and showing a node table to the other case sends somebody to audit
 * cluster capacity over a failed image pull.
 *
 * **The scheduler's own sentence, with its age.** Quoted, never paraphrased —
 * it is the verdict over plugins this console does not model. The age is beside
 * it because the message is a *snapshot*: a node added since does not rewrite
 * it, and a forty-minute-old "0/5 nodes are available" describes a cluster that
 * may no longer exist.
 *
 * **Its absence, said out loud.** Events age out of etcd within the hour. A
 * missing message is rendered as "no explanation is readable", never as a blank
 * space that reads like nothing is wrong.
 *
 * **The node table, which never says a node fits.** Every row is `ruled_out`
 * with reasons or `no_reason_found`. The scheduler also weighs affinity,
 * topology spread, volume zone, extended resources and every plugin the cluster
 * runs; none of that is evaluated. So the shrug is drawn as a shrug — and the
 * rows whose capacity could not even be checked say so, because a node that was
 * half-examined and one that was examined and cleared must not look alike.
 */
import { Alert } from '@patternfly/react-core';
import {
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  PartialBanner,
  SectionHeader,
  StatusBadge,
} from './ui';
import { pods as podsApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** `4m` / `2h 10m`, or null for an age this console could not derive. */
function age(seconds) {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * The scheduler's message, or the honest account of why there is none.
 *
 * The empty case is a warning rather than a blank, because a blank reads as
 * "nothing is wrong" on the one screen where something demonstrably is.
 */
function SchedulerVerdict({ report }) {
  const verdict = report.scheduler;
  if (!verdict) {
    return (
      <Alert
        isInline
        variant="warning"
        data-testid="pod-scheduling-no-verdict"
        title="The scheduler has not left a readable explanation"
      >
        No <code>FailedScheduling</code> event for this pod can be read right
        now. Events age out of etcd — an hour by default — so a pod that has been
        pending longer than that has an explanation that expired. This is not a
        report that the scheduler is content with the pod.
        {report.partial
          ? ' Some reads for this page also did not answer; the banner above says which.'
          : ''}
      </Alert>
    );
  }
  return (
    <div data-testid="pod-scheduling-verdict">
      <pre className="admin-code-block" data-testid="pod-scheduling-message">
        {verdict.message}
      </pre>
      <p style={MUTED} data-testid="pod-scheduling-age">
        {`The scheduler's last attempt was ${age(verdict.age_seconds) ?? 'at an unknown time'} ago`}
        {verdict.count ? `, after ${verdict.count} tries` : ''}
        {'. This is what it saw then — a node added, freed or untainted since '}
        {'does not rewrite the message.'}
      </p>
    </div>
  );
}

/** Every node, ruled out or not — and never reported as fitting. */
function NodeTable({ nodes }) {
  if (nodes === null) {
    return (
      <Alert
        isInline
        variant="warning"
        data-testid="pod-scheduling-nodes-unavailable"
        title="The nodes could not be listed"
      >
        Which nodes are ruled out is unknown. This is not a report that the
        cluster has none.
      </Alert>
    );
  }
  return (
    <div data-testid="pod-scheduling-nodes">
      <DataTable
        ariaLabel="Nodes and why each is ruled out"
        tableId="pod-scheduling-nodes"
        rows={nodes ?? []}
        rowKey={(row) => row.name}
        resizableColumns={false}
        emptyTitle="This cluster has no nodes"
        emptyDescription="The node listing answered and was empty."
        columns={[
          { key: 'name', title: 'Node', sortable: true },
          {
            key: 'verdict',
            title: 'Verdict',
            cell: (row) =>
              row.verdict === 'ruled_out' ? (
                <StatusBadge
                  status="blocked"
                  label="Ruled out"
                  tooltip="This console found a reason the scheduler would reject this node."
                />
              ) : (
                <StatusBadge
                  status="unknown"
                  label="No reason found"
                  tooltip="This console checked what it can check and found nothing against this node. That is not the same as the node fitting — the scheduler also weighs affinity, topology spread, volume zone and every plugin the cluster runs, none of which is evaluated here."
                />
              ),
          },
          {
            key: 'reasons',
            title: 'Why',
            modifier: 'breakWord',
            cell: (row) => {
              if (row.reasons.length) {
                return (
                  <ul style={{ margin: 0, paddingInlineStart: '1.1rem' }}>
                    {row.reasons.map((reason) => (
                      <li key={reason.code} data-testid={`pod-scheduling-reason-${reason.code}`}>
                        {reason.detail}
                      </li>
                    ))}
                  </ul>
                );
              }
              // The half-examined row. It reached "no reason found" without the
              // capacity check having run at all, which is weaker still — and
              // drawing it identically to a fully checked node would overstate
              // what this console looked at.
              return row.capacity_checked ? (
                <span style={MUTED}>
                  Nothing this console checks rules this node out. The scheduler
                  checks more.
                </span>
              ) : (
                <span style={MUTED} data-testid={`pod-scheduling-unchecked-${row.name}`}>
                  Nothing rules this node out among the checks that ran — and its
                  free capacity was not one of them, because what it already
                  holds could not be read.
                </span>
              );
            },
          },
        ]}
      />
    </div>
  );
}

/** The claims, and the inversion that sends people to the wrong system. */
function ClaimTable({ claims }) {
  if (claims === null) {
    return (
      <Alert
        isInline
        variant="warning"
        data-testid="pod-scheduling-claims-unavailable"
        title="The claims could not be listed"
      >
        Whether a volume is holding this pod back is unknown. This is not a
        report that it mounts none.
      </Alert>
    );
  }
  if (!claims.length) return null;
  return (
    <>
      <SectionHeader
        title="Volumes"
        description="An unbound claim usually blocks scheduling — unless its storage class waits for a consumer, in which case it is unbound because the pod is unscheduled, and fixing storage is the wrong errand."
      />
      <div data-testid="pod-scheduling-claims">
        <DataTable
          ariaLabel="Claims this pod mounts"
          tableId="pod-scheduling-claims"
          rows={claims}
          rowKey={(row) => row.name}
          resizableColumns={false}
          columns={[
            { key: 'name', title: 'Claim' },
            {
              key: 'phase',
              title: 'Phase',
              cell: (row) =>
                row.exists ? (
                  row.phase ?? <span style={MUTED}>unset</span>
                ) : (
                  <span style={MUTED}>does not exist</span>
                ),
            },
            {
              key: 'binding_mode',
              title: 'Binding mode',
              cell: (row) => row.binding_mode ?? <span style={MUTED}>unknown</span>,
            },
            {
              key: 'blocks_scheduling',
              title: 'Blocks scheduling',
              cell: (row) => {
                if (row.blocks_scheduling === true) {
                  return <StatusBadge status="blocked" label="Yes" />;
                }
                if (row.blocks_scheduling === false) {
                  return <StatusBadge status="Ready" label="No" />;
                }
                return (
                  <StatusBadge
                    status="unknown"
                    label="Unknown"
                    tooltip="The claim is unbound and its storage class could not be read, so whether it is the cause or the symptom is undecided. Guessing sends you to one of two systems with even odds."
                  />
                );
              },
            },
          ]}
        />
      </div>
    </>
  );
}

export default function PodScheduling({ namespace, name, clusterId }) {
  const { data, loading, error, reload } = useAsync(
    () => podsApi.scheduling(namespace, name),
    {
      key: `pod-scheduling:${clusterId}:${namespace}/${name}`,
      enabled: clusterId != null && Boolean(namespace) && Boolean(name),
    },
  );

  if (error) {
    return <ErrorState title="This pod's scheduling could not be read" error={error} onRetry={reload} />;
  }
  if (!data) return <LoadingState label="Asking why this pod is pending…" />;

  if (data.waiting_on === 'nothing') {
    return (
      <EmptyState
        title="This pod's placement is settled"
        description={
          data.node
            ? `It is on ${data.node}, and its phase is ${data.phase}. Nothing here is waiting on the scheduler.`
            : `Its phase is ${data.phase}. Nothing here is waiting on the scheduler.`
        }
      />
    );
  }

  const onKubelet = data.waiting_on === 'kubelet';

  return (
    <div data-testid="pod-scheduling">
      <PartialBanner unavailable={data.unavailable} />

      <SectionHeader
        title={onKubelet ? 'Placed, and waiting on the node' : 'Not placed on any node'}
        description={
          onKubelet
            ? 'The scheduler has already chosen a node for this pod. What is holding it now is on that machine — an image pull, a volume mount, an init container — and has nothing to do with cluster capacity.'
            : 'No node has been chosen. This is the scheduler’s answer, not this console’s.'
        }
      />

      <p data-testid="pod-scheduling-waiting-on">
        <StatusBadge
          status={onKubelet ? 'progressing' : 'blocked'}
          label={onKubelet ? `Waiting on the kubelet on ${data.node}` : 'Waiting on the scheduler'}
        />{' '}
        <span style={MUTED}>
          {data.pending_seconds !== null && data.pending_seconds !== undefined
            ? `Pending for ${age(data.pending_seconds)}.`
            : 'How long it has been pending could not be derived.'}
        </span>
      </p>

      {onKubelet ? (
        <Alert
          isInline
          variant="info"
          data-testid="pod-scheduling-kubelet"
          title="Node capacity is not the question here"
        >
          Look at this pod&rsquo;s <strong>Events</strong> tab and its container
          statuses. The scheduler is finished with it.
        </Alert>
      ) : (
        <>
          <SectionHeader
            title="What the scheduler said"
            description="Quoted from the scheduler's own FailedScheduling event, over the whole predicate chain — including the plugins this console does not model."
          />
          <SchedulerVerdict report={data} />

          <SectionHeader
            title="Nodes"
            description="This console's own re-derivation, and it only ever rules a node out. Nothing here says a node fits: the scheduler checks more than this, and it has already rejected them all."
          />
          <NodeTable nodes={data.nodes} />
        </>
      )}

      <ClaimTable claims={data.claims} />

      {loading ? <span style={MUTED} data-testid="pod-scheduling-refreshing">Re-reading…</span> : null}
    </div>
  );
}
