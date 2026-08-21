/**
 * DebugPanel — the Debug tab of the pod console (§7.4).
 *
 * The third question asked of a misbehaving pod. The first two — what did it
 * log, what does it look like from inside — are answered by `LogViewer` and
 * `PodTerminal`. This one answers the case those two cannot: **the pod's image
 * has no shell.** A distroless container is the one an operator most needs to
 * get inside and the one `exec` is useless against, because there is nothing to
 * exec. Kubernetes' answer is an ephemeral container, and this panel attaches
 * one and then opens a terminal in it.
 *
 * ## What it renders, and why each state is its own
 *
 * `supported` from §7.4 is **three-valued**, and all three are shown
 * differently:
 *
 *   `true`   the cluster serves `pods/ephemeralcontainers`. Normal operation.
 *   `false`  it does not — a cluster older than 1.16, or one with the feature
 *            gate off before 1.23. Rendered as an ordinary empty state, not as
 *            an error: §1.3 is explicit that `unsupported` is not an error in
 *            the UI, because rendering ordinary facts in red trains people to
 *            ignore red.
 *   `null`   the cluster's discovery document could not be read, so we do not
 *            know. Rendered as a warning that says exactly that, and the
 *            Attach button stays enabled — refusing here would tell an operator
 *            their cluster lacks a feature it may well have, and send them to
 *            plan an upgrade instead of to look at their API server.
 *
 * ## Why the terminal is not opened automatically
 *
 * The kubelet has to pull the image and start the container, and neither is
 * instant. A panel that dropped straight into a terminal would show a failing
 * socket for a container that is still being pulled, and the operator would
 * read that as "the debug container did not work". So the container is listed
 * with its real state, and Open terminal is offered — disabled, with the
 * reason, until the state is one a shell can attach to (rule 11.4 applied to a
 * container rather than to a permission).
 */
import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Split,
  SplitItem,
  Tooltip,
} from '@patternfly/react-core';
import BugIcon from '@patternfly/react-icons/dist/esm/icons/bug-icon';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';

import DebugDialog from './DebugDialog';
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
import { pods as podsApi } from '../api/client';
import { useAsync } from '../pages/_data';

/**
 * Why a shell cannot be opened in this container, or `null` when it can.
 *
 * `Running` is the only state that can. A `Waiting` container has not started —
 * exec into it fails with a message about the container not being found, which
 * reads as though the console attached the wrong thing. A `Terminated` one has
 * exited: its filesystem is gone and so is any point in a shell. A container
 * with no state at all is the kubelet not having reported, which is a third
 * thing and is said as one.
 */
function attachReason(row) {
  if (row.state === 'Running') return null;
  if (row.state === 'Waiting') {
    return (
      `This container is still waiting${row.reason ? ` (${row.reason})` : ''}. The kubelet has not started ` +
      'it yet — usually the image is still being pulled. Refresh in a moment.'
    );
  }
  if (row.state === 'Terminated') {
    return `This container has exited${row.reason ? ` (${row.reason})` : ''}. Attach a new one — an ephemeral container is never restarted.`;
  }
  return (
    'The kubelet has not reported on this container yet, so whether a shell can be opened in it is ' +
    'unknown. That is different from "waiting": nothing has been said about it at all.'
  );
}

export function DebugPanel({
  namespace,
  name,
  /** `{ allowed, reason }` for `patch pods/ephemeralcontainers` — rule 11.4. */
  gate,
  /** `{ allowed, reason }` for `create pods/exec`; the terminal needs its own. */
  execGate,
  /** The pod's own container names, for the target picker and name collisions. */
  containers = [],
  initContainers = [],
  height = 380,
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [openTerminal, setOpenTerminal] = useState(null);

  const listing = useAsync(() => podsApi.debugContainers(namespace, name), {
    key: `debug:${namespace}/${name}`,
  });

  const rows = listing.data?.items ?? [];
  const supported = listing.data?.supported;
  const supportDetail = listing.data?.supportDetail;

  const taken = useMemo(
    () => [...containers, ...initContainers, ...rows.map((row) => row.name)],
    [containers, initContainers, rows],
  );

  const onApplied = useCallback(() => {
    setDialogOpen(false);
    // Any terminal on screen belongs to a different container, and leaving it
    // open beside a freshly attached one invites typing into the wrong shell.
    setOpenTerminal(null);
    // Re-read rather than splicing the response into the list: what the API
    // server returned is the *projection* of the patch, and the container's
    // real state — Waiting, ImagePullBackOff, Running — comes from the kubelet
    // afterwards. Showing the projection as though it were the state would put
    // a green pill on a container that is still being pulled.
    listing.reload();
  }, [listing]);

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Container',
        sortable: true,
        cell: (row) => <code>{row.name}</code>,
      },
      { key: 'image', title: 'Image', sortable: true },
      {
        key: 'command',
        title: 'Command',
        // Deliberately not a `NullableCell`. That control's tooltip says "this
        // value could not be read", and an absent command here means the
        // opposite: it was read, and it says to run the image's own entrypoint.
        // The em dash is reserved for values this console could not see.
        cell: (row) =>
          row.command ? (
            <code>{row.command.join(' ')}</code>
          ) : (
            <span
              style={{ color: 'var(--admin-muted, #6a6e73)' }}
              title="No command was set, so the image's own entrypoint runs."
            >
              image entrypoint
            </span>
          ),
      },
      {
        key: 'state',
        title: 'State',
        sortable: true,
        cell: (row) => (
          // `state: null` is the kubelet not having reported, which is grey and
          // says so — never green, and never the same pill as Running.
          <StatusBadge
            status={row.state ?? 'unknown'}
            detail={row.reason}
            tooltip={
              row.state == null
                ? 'The kubelet has not reported a status for this container yet.'
                : undefined
            }
          />
        ),
      },
      {
        key: 'targetContainer',
        title: 'Sharing processes with',
        cell: (row) =>
          row.targetContainer ?? (
            // Not a NullableCell: an unset target is a real configuration, not
            // an unread value, and a dash here would say we could not tell.
            <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>Network and volumes only</span>
          ),
      },
      {
        key: 'started_at',
        title: 'Started',
        sortable: true,
        // Same reasoning as Command: a container with no start time has not
        // started, which the State column already says in its own words. A dash
        // claiming the value was unreadable would contradict it.
        cell: (row) =>
          row.started_at ? (
            <AgeCell timestamp={row.started_at} />
          ) : (
            <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>Not started</span>
          ),
      },
      {
        key: 'open',
        title: '',
        cell: (row) => {
          const containerReason = attachReason(row);
          const reason = execGate && !execGate.allowed ? execGate.reason : containerReason;
          const button = (
            <Button
              variant="secondary"
              isDisabled={Boolean(reason)}
              onClick={() => setOpenTerminal(row.name)}
              data-testid={`debug-open-${row.name}`}
            >
              {openTerminal === row.name ? 'Terminal open' : 'Open terminal'}
            </Button>
          );
          return reason ? <Tooltip content={reason}>{<span>{button}</span>}</Tooltip> : button;
        },
      },
    ],
    [execGate, openTerminal],
  );

  if (listing.loading && listing.data == null) {
    return <LoadingState label="Reading this pod's debug containers…" />;
  }

  if (listing.error) {
    return (
      <ErrorState
        title="This pod's debug containers could not be read"
        error={listing.error}
        onRetry={listing.reload}
      />
    );
  }

  return (
    <div className="admin-debug-panel" data-testid="debug-panel" data-supported={String(supported)}>
      {supported === false && (
        // Not an Alert and not red. §1.3: `unsupported` is an ordinary fact
        // about a cluster, and a console that paints ordinary facts red teaches
        // operators to ignore red.
        <EmptyState
          icon={BugIcon}
          title="This cluster does not serve debug containers"
          description={
            supportDetail ??
            'Ephemeral containers need Kubernetes 1.16 or later, and the EphemeralContainers feature gate enabled before 1.23.'
          }
          data-testid="debug-unsupported"
        >
          <p style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}>
            Read this pod&apos;s logs, or open a terminal in a container that has a shell.
          </p>
        </EmptyState>
      )}

      {supported == null && (
        <Alert
          isInline
          variant="warning"
          title="Whether this cluster serves debug containers could not be confirmed"
          data-testid="debug-support-unknown"
        >
          {supportDetail ??
            "The cluster's discovery document could not be read, so this console does not know whether ephemeral containers are available."}{' '}
          This is <strong>not</strong> a statement that they are unavailable. Attaching one is still
          offered; the API server will answer.
        </Alert>
      )}

      {supported !== false && (
        <>
          <Split hasGutter style={{ alignItems: 'center', margin: 'var(--admin-gap-sm, 0.5rem) 0' }}>
            <SplitItem>
              <span style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem' }}>
                {rows.length === 0
                  ? 'No debug container is attached to this pod.'
                  : `${rows.length} debug ${rows.length === 1 ? 'container' : 'containers'} attached.`}
              </span>
            </SplitItem>
            <SplitItem isFilled />
            <SplitItem>
              <Button
                variant="secondary"
                icon={<SyncAltIcon />}
                onClick={listing.reload}
                isLoading={listing.loading}
                data-testid="debug-refresh"
              >
                Refresh
              </Button>
            </SplitItem>
            <SplitItem>
              {/* Rule 11.4 through the shared control: disabled **with the
                  reason** in a tooltip, never hidden. Deliberately not
                  `isDanger` — this write is irreversible, which the form says
                  in as many words, but it is not destructive. Painting a
                  debugging aid the same red as "drain this node" is how a
                  console teaches its operators to stop reading red. */}
              <ActionButton
                gate={gate}
                variant="primary"
                icon={<BugIcon />}
                onClick={() => setDialogOpen(true)}
              >
                Attach a debug container
              </ActionButton>
            </SplitItem>
          </Split>

          {rows.length > 0 && (
            <DataTable
              ariaLabel="Debug containers"
              tableId="debug-containers"
              columns={columns}
              rows={rows}
              rowKey={(row) => row.name}
              resizableColumns={false}
              emptyTitle="No debug containers"
            />
          )}

          {rows.length === 0 && (
            <EmptyState
              icon={BugIcon}
              title="Nothing is debugging this pod"
              description={
                'A debug container is a second container scheduled into this running pod, sharing its ' +
                'network and volumes. It is what gets you a shell when the pod’s own image has none — ' +
                'and it cannot be removed once attached, so the change is previewed before it is made.'
              }
              data-testid="debug-empty"
            />
          )}

          {openTerminal && (
            <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }}>
              <Split hasGutter style={{ alignItems: 'center' }}>
                <SplitItem isFilled>
                  <strong>Terminal in {openTerminal}</strong>
                </SplitItem>
                <SplitItem>
                  <Button variant="link" isInline onClick={() => setOpenTerminal(null)}>
                    Close terminal
                  </Button>
                </SplitItem>
              </Split>
              {/* Keyed on the container so switching between two debug
                  containers tears the first socket down rather than reusing a
                  terminal that is still bound to the other one. */}
              <PodTerminal
                key={openTerminal}
                namespace={namespace}
                name={name}
                container={openTerminal}
                height={height}
              />
            </div>
          )}
        </>
      )}

      <DebugDialog
        isOpen={dialogOpen}
        namespace={namespace}
        name={name}
        containers={containers}
        taken={taken}
        onClose={() => setDialogOpen(false)}
        onApplied={onApplied}
      />
    </div>
  );
}

export default DebugPanel;
