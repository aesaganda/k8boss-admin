/**
 * NodeDebugPanel — the Debug section of a node's page (§5.5).
 *
 * The node equivalent of §7.4's `DebugPanel`, and deliberately not the same
 * component: the two features look alike and behave differently in the ways
 * that matter.
 *
 *   §7.4  attaches a container *inside* a pod.   It cannot be removed — the API
 *         has no verb for it — so the dialog says so and there is no control.
 *   §5.5  creates a pod *on a machine*.          It can and must be removed, so
 *         every row has a Remove, and nothing else will do it.
 *
 * Sharing a component would have meant one of those two truths being rendered
 * about the other, which is the failure this whole codebase is written against.
 *
 * ## What it shows, and why
 *
 * `enabled` from §5.5 is the deployment's two gates answered together, so the
 * action can be disabled *with the reason* (rule 11.4) rather than offered and
 * then refused with a 403. It is one value rather than two because an operator
 * does not care which switch is off, they care what to go and change — and the
 * sentence names it.
 *
 * `namespace` comes from the server. The client never guesses it: it is where a
 * privileged pod is about to appear, which is part of what the operator is
 * confirming.
 *
 * Every row states whether the host filesystem is mounted read-only. That is the
 * difference between a pod that can read the machine and one that can rewrite
 * it, and it is not visible from the pod's name or its phase.
 */
import { useCallback, useMemo, useState } from 'react';
import { Alert, Button, Split, SplitItem, Tooltip } from '@patternfly/react-core';
import ServerIcon from '@patternfly/react-icons/dist/esm/icons/server-icon';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';

import MutationDialog from './MutationDialog';
import NodeDebugDialog from './NodeDebugDialog';
import PodTerminal from './PodTerminal';
import {
  ActionButton,
  AgeCell,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  StatusBadge,
} from './ui';
import { nodes as nodesApi } from '../api/client';
import { useAsync } from '../pages/_data';

/** The container name `build_pod` gives every debug pod. */
const DEBUG_CONTAINER = 'debugger';

/**
 * Why a shell cannot be opened in this pod, or `null` when it can.
 *
 * Keyed on the pod phase rather than on the container state, because a node
 * debug pod has exactly one container and the phase is the fact the operator is
 * already looking at in the table.
 */
function shellReason(row) {
  if (row.phase === 'Running') return null;
  if (row.phase === 'Pending') {
    return (
      `This pod is still Pending${row.reason ? ` (${row.reason})` : ''}. It bypasses the scheduler, so ` +
      'this is the kubelet pulling the image or refusing the pod — not a scheduling delay. Refresh in a moment.'
    );
  }
  if (row.phase === 'Succeeded' || row.phase === 'Failed') {
    return `This pod has finished (${row.phase}). Its filesystem is gone; create a new one.`;
  }
  return (
    'This pod has no phase yet, so whether a shell can be opened in it is unknown. That is not the ' +
    'same as it not being ready — nothing has been reported about it at all.'
  );
}

export function NodeDebugPanel({
  node,
  /** `{ allowed, reason }` for `create pods` — rule 11.4, the RBAC half. */
  gate,
  /** `{ allowed, reason }` for `create pods/exec`; the terminal needs its own. */
  execGate,
  height = 380,
}) {
  const [createOpen, setCreateOpen] = useState(false);
  const [removing, setRemoving] = useState(null);
  const [openTerminal, setOpenTerminal] = useState(null);

  const listing = useAsync(() => nodesApi.debugPods(node), { key: `node-debug:${node}` });

  const rows = listing.data?.items ?? [];
  const enabled = listing.data?.enabled;
  const enabledDetail = listing.data?.enabledDetail;
  const namespace = listing.data?.namespace;

  const onChanged = useCallback(() => {
    setCreateOpen(false);
    setRemoving(null);
    setOpenTerminal(null);
    // Re-read rather than splicing: what came back is the API server's
    // *projection* of the create, and the pod's real phase — Pending,
    // ImagePullBackOff, Running — comes from the kubelet afterwards.
    listing.reload();
  }, [listing]);

  // The deployment gate and the RBAC gate are combined, deployment first. Both
  // are "you cannot do this", but they send an operator to different systems,
  // and the deployment one is the one they can answer without a cluster admin.
  const actionGate = useMemo(() => {
    if (enabled === false) return { allowed: false, reason: enabledDetail };
    if (enabled == null) {
      return {
        allowed: false,
        reason: 'Whether this deployment permits node debug pods has not been read yet.',
      };
    }
    return gate ?? { allowed: true, reason: null };
  }, [enabled, enabledDetail, gate]);

  const columns = useMemo(
    () => [
      { key: 'name', title: 'Pod', sortable: true, cell: (row) => <code>{row.name}</code> },
      { key: 'image', title: 'Image', sortable: true },
      {
        key: 'phase',
        title: 'Phase',
        sortable: true,
        cell: (row) => <StatusBadge status={row.phase ?? 'unknown'} detail={row.reason} />,
      },
      {
        key: 'hostFilesystemReadOnly',
        title: 'Host filesystem',
        cell: (row) => {
          if (row.hostFilesystemReadOnly === true) {
            return <StatusBadge status="unknown" label="Read-only" />;
          }
          if (row.hostFilesystemReadOnly === false) {
            // The one row state worth colouring: this pod can rewrite the
            // machine.
            return <StatusBadge status="warning" label="Read-write" />;
          }
          // `null` — the pod wears this console's label but has no host mount we
          // recognise, so it is not one we created in the shape we create them.
          // Saying "read-only" here would be a safety claim about an object we
          // do not understand.
          return (
            <Tooltip content="This pod carries the console's label but no host mount this console recognises. It may not have been created here.">
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>Unrecognised</span>
            </Tooltip>
          );
        },
      },
      {
        key: 'created_at',
        title: 'Created',
        sortable: true,
        cell: (row) => <AgeCell timestamp={row.created_at} />,
      },
      {
        key: 'actions',
        title: '',
        cell: (row) => {
          const podReason = shellReason(row);
          const reason = execGate && !execGate.allowed ? execGate.reason : podReason;
          const shell = (
            <Button
              variant="secondary"
              isDisabled={Boolean(reason)}
              onClick={() => setOpenTerminal(row)}
              data-testid={`node-debug-shell-${row.name}`}
            >
              {openTerminal?.name === row.name ? 'Terminal open' : 'Open shell'}
            </Button>
          );
          return (
            <Split hasGutter>
              <SplitItem>{reason ? <Tooltip content={reason}><span>{shell}</span></Tooltip> : shell}</SplitItem>
              <SplitItem>
                {/* Always offered, at every phase. A pod that failed to start
                    still holds a hostPath mount in its manifest and still needs
                    removing — "it is not running" is not "it is gone". */}
                <Button
                  variant="link"
                  isDanger
                  onClick={() => setRemoving(row)}
                  data-testid={`node-debug-remove-${row.name}`}
                >
                  Remove
                </Button>
              </SplitItem>
            </Split>
          );
        },
      },
    ],
    [execGate, openTerminal],
  );

  if (listing.loading && listing.data == null) {
    return <LoadingState label="Reading this node's debug pods…" />;
  }
  if (listing.error) {
    return (
      <ErrorState
        title="This node's debug pods could not be read"
        error={listing.error}
        onRetry={listing.reload}
      />
    );
  }

  return (
    <div className="admin-node-debug" data-testid="node-debug-panel" data-enabled={String(enabled)}>
      <Split hasGutter style={{ alignItems: 'center', margin: 'var(--admin-gap-sm, 0.5rem) 0' }}>
        <SplitItem>
          <span style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem' }}>
            {rows.length === 0
              ? 'No debug pod is running on this node.'
              : `${rows.length} debug ${rows.length === 1 ? 'pod' : 'pods'} on this node`}
            {namespace ? ` · namespace ${namespace}` : ''}
          </span>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <Button
            variant="secondary"
            icon={<SyncAltIcon />}
            onClick={listing.reload}
            isLoading={listing.loading}
            data-testid="node-debug-refresh"
          >
            Refresh
          </Button>
        </SplitItem>
        <SplitItem>
          <ActionButton
            gate={actionGate}
            variant="secondary"
            icon={<ServerIcon />}
            onClick={() => setCreateOpen(true)}
          >
            Create a debug pod
          </ActionButton>
        </SplitItem>
      </Split>

      {enabled === false && (
        // Not an error and not red: a deployment that has switched this off has
        // made a decision, and rendering that decision as a fault would train
        // operators to ignore the colour that means something is wrong.
        <Alert isInline variant="info" title="Node debug pods are not available here" data-testid="node-debug-disabled">
          {enabledDetail}
        </Alert>
      )}

      {rows.length > 0 ? (
        <DataTable
          ariaLabel="Node debug pods"
          tableId="node-debug-pods"
          columns={columns}
          rows={rows}
          rowKey={(row) => row.name}
          resizableColumns={false}
        />
      ) : (
        <EmptyState
          icon={ServerIcon}
          title="Nothing is debugging this node"
          description={
            'A debug pod runs on this machine with its filesystem mounted, for when the thing that ' +
            'needs looking at is the node itself and there is no way to SSH to it. It is the most ' +
            'privileged object this console creates, so it is previewed in full before it is made — ' +
            'and nothing removes it afterwards except you.'
          }
          data-testid="node-debug-empty"
        />
      )}

      {openTerminal && (
        <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }}>
          <Split hasGutter style={{ alignItems: 'center' }}>
            <SplitItem isFilled>
              <strong>Shell in {openTerminal.name}</strong>{' '}
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>
                — the node&apos;s filesystem is at <code>/host</code>
                {openTerminal.hostFilesystemReadOnly === false ? ', mounted read-write' : ', read-only'}
              </span>
            </SplitItem>
            <SplitItem>
              <Button variant="link" isInline onClick={() => setOpenTerminal(null)}>
                Close terminal
              </Button>
            </SplitItem>
          </Split>
          <PodTerminal
            key={openTerminal.name}
            namespace={openTerminal.namespace ?? namespace}
            name={openTerminal.name}
            container={DEBUG_CONTAINER}
            height={height}
          />
        </div>
      )}

      <NodeDebugDialog
        isOpen={createOpen}
        node={node}
        namespace={namespace}
        onClose={() => setCreateOpen(false)}
        onApplied={onChanged}
      />

      {removing && (
        // No form, so `MutationDialog` dry-runs on open and shows the delete
        // diff — §4 fixes that as `before=live, after=null`, which is the whole
        // manifest disappearing. That is the right thing to confirm a removal
        // against, and it comes free from the funnel.
        <MutationDialog
          isOpen
          title={`Remove ${removing.name}`}
          description={`Deletes the debug pod from ${removing.namespace ?? namespace}. The node itself is not touched.`}
          confirmLabel="Remove"
          isDanger
          request={(dryRun) => nodesApi.deleteDebugPod(node, removing.name, { dryRun })}
          onClose={() => setRemoving(null)}
          onApplied={onChanged}
          summarize={(result) =>
            result?.applied === true
              ? {
                  variant: 'success',
                  title: `${removing.name} is removed`,
                  body:
                    'The pod is deleted and its mount of the node’s filesystem goes with it. The ' +
                    'node is unchanged — this pod never modified it unless it was mounted read-write ' +
                    'and something wrote.',
                }
              : {
                  variant: 'warning',
                  title: 'The request completed without confirming the pod was removed',
                  body: 'Re-read this node before assuming the debug pod is gone.',
                }
          }
        />
      )}
    </div>
  );
}

export default NodeDebugPanel;
