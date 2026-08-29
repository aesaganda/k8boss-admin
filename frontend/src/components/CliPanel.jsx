/**
 * CliPanel — the masthead terminal (§15).
 *
 * A pod with `kubectl` in it, and §7's shell into that pod. The console has a
 * page for most things and there is always the one command it does not: an
 * `oc adm` subcommand, `kubectl get --raw /metrics`, `kubectl auth can-i --list`.
 * The alternative to this panel is leaving the console for a laptop with a
 * kubeconfig on it, which is the moment the audit trail stops.
 *
 * ## What it reuses, and why nothing here is new
 *
 * The terminal is `PodTerminal` — §7's exec socket, unchanged. The create and
 * the removal are `MutationDialog` over `POST /api/cli` and `DELETE /api/cli/…`,
 * so both get the funnel's dry run, diff and confirm without this file knowing
 * how any of that works. A CLI-specific terminal or a create that skipped the
 * diff would be a second place for the gate, the preflight and the audit records
 * to be got right, and the second place is the one that stops being right.
 *
 * ## The one thing this panel has to say out loud
 *
 * Everything typed in that shell happens **outside** the write funnel: no
 * preflight naming the permission, no `dryRun=All`, no diff, no
 * `resourceVersion` check, and no audit row saying what changed. The trail
 * records that a shell was opened on this pod, by whom, for how long and how
 * many bytes went through it — and nothing about the `kubectl delete` typed into
 * it. Every other write surface in this console is the opposite, so an operator
 * who has learned to trust the diff has to be told that this one does not have
 * one.
 *
 * And what that shell can actually do is decided entirely by the pod's
 * ServiceAccount, not by the console's permissions and not by the operator's. So
 * the account is named on screen at every phase — in the summary line, in the
 * table, and in the diff of the create — rather than being a configuration
 * detail the panel keeps to itself.
 *
 * ## Reuse rather than a pod per click
 *
 * Opening this panel with a Running pod already there shows its terminal
 * immediately: nothing is created and no session is opened until the operator
 * presses the terminal's own Open shell. A pod per click is how this feature
 * would leak a dozen of them into a namespace in an afternoon.
 *
 * Nothing removes the pod on close. A closed tab is not a signal and a restarted
 * console pod drops whatever would have issued the DELETE, so — like §5.5 — the
 * panel does the honest thing instead: `ADMIN_CLI_MAX_SECONDS` bounds how long
 * the container runs, and Remove is a button.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  List,
  ListItem,
  Split,
  SplitItem,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import TerminalIcon from '@patternfly/react-icons/dist/esm/icons/terminal-icon';

import MutationDialog from './MutationDialog';
import PodTerminal from './PodTerminal';
import {
  ActionButton,
  AgeCell,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  PartialBanner,
  StatusBadge,
} from './ui';
import { cli as cliApi } from '../api/client';
import { useAsync, useGates } from '../pages/_data';

/**
 * The create gate and the shell gate are separate RBAC questions and a caller
 * can hold either without the other — `pods/exec` is its own RBAC resource. A
 * single check would disable the terminal on a console that can only create, or
 * offer it on one that cannot exec and fail at the socket.
 */
const CHECKS = [
  { id: 'create', verb: 'create', group: 'core', resource: 'pods' },
  { id: 'exec', verb: 'create', group: 'core', resource: 'pods', subresource: 'exec' },
];

/**
 * Why a shell cannot be opened in this pod, or `null` when it can.
 *
 * Keyed on the pod phase, which is the fact the operator is already looking at
 * in the table. A CLI pod has exactly one container, so there is nothing finer
 * to key on.
 */
function shellReason(row) {
  if (row.phase === 'Running') return null;
  if (row.phase === 'Pending') {
    return (
      `This pod is still Pending${row.reason ? ` (${row.reason})` : ''}. The kubelet is pulling the ` +
      'image or the scheduler has not placed it yet. Refresh in a moment.'
    );
  }
  if (row.phase === 'Succeeded' || row.phase === 'Failed') {
    return (
      `This pod has finished (${row.phase}). If it ran past ADMIN_CLI_MAX_SECONDS the kubelet ` +
      'stopped the container at the deadline. Remove it and start a new session.'
    );
  }
  return (
    'This pod has no phase yet, so whether a shell can be opened in it is unknown. That is not the ' +
    'same as it not being ready — nothing has been reported about it at all.'
  );
}

/**
 * The create dialog — a `MutationDialog` with one field.
 *
 * The consequences are listed before the preview, in plain words. The diff that
 * follows contains `serviceAccountName` and an operator who knows Kubernetes
 * well will read it correctly, but "everything you type here runs as that
 * account and none of it is in the audit trail" is not a sentence a YAML diff
 * says out loud, and it is the sentence that matters.
 */
function CliSessionDialog({ isOpen, namespace, serviceAccount, defaultImage, onClose, onApplied }) {
  const [image, setImage] = useState('');

  // Reset on every opening, the way every write dialog here does: a dialog
  // reopened after a failure that kept the last image would preview one thing
  // and be remembered as another.
  useEffect(() => {
    if (!isOpen) return;
    setImage('');
  }, [isOpen]);

  return (
    <MutationDialog
      isOpen={isOpen}
      title="Start a CLI session"
      description={`Creates a pod in ${namespace ?? '—'} and opens a shell in it. It runs no workload and changes nothing on its own.`}
      previewLabel="Preview the pod"
      confirmLabel="Create CLI pod"
      request={(dryRun) => cliApi.create({ image: image.trim() || null, dryRun })}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${result.pod} is scheduled in ${result.namespace}`,
              body:
                `The API server accepted the pod. Once the kubelet has pulled ${result.image} and ` +
                `started it, open a shell from the table. kubectl in it authenticates as ` +
                `${result.serviceAccount} — nothing more and nothing less. Remove it when you are ` +
                'done: nothing else will.',
            }
          : {
              variant: 'warning',
              title: 'The request completed without confirming the pod was created',
              body: 'Re-read this panel before assuming a CLI pod is running.',
            }
      }
      className="admin-cli-dialog"
    >
      <Form>
        <Alert
          isInline
          variant="warning"
          title="A shell here is outside every control this console puts in front of a write"
          data-testid="cli-consequences"
        >
          <List>
            <ListItem>
              <strong>No diff, and no record of what changed.</strong> Every other action here is
              preflighted, dry-run and shown to you before it happens, and lands in the audit trail
              naming the object. A command typed in this shell is none of that. The trail records
              that a shell was opened, by whom and for how long — not what you ran in it.
            </ListItem>
            <ListItem>
              <strong>
                It runs as <code>{serviceAccount ?? 'unknown'}</code>, not as you.
              </strong>{' '}
              kubectl in this pod authenticates as that ServiceAccount, so it can do exactly what
              that account can do — which may be more than you can, or less. Kubernetes has no
              permission covering which account a pod may bind, so this is set by whoever configured
              the console (<code>ADMIN_CLI_SERVICE_ACCOUNT</code>) and cannot be narrowed afterwards.
            </ListItem>
            <ListItem>
              The pod holds a live API credential for as long as it exists. Nothing removes it when
              you close this panel — use Remove.
            </ListItem>
          </List>
        </Alert>

        <FormGroup label="CLI image" fieldId="cli-image">
          <TextInput
            id="cli-image"
            value={image}
            onChange={(_event, value) => setImage(value)}
            placeholder={defaultImage ? `Leave empty for ${defaultImage}` : "This console's configured default"}
            aria-label="CLI image"
            data-testid="cli-image"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                It needs <code>kubectl</code> (or <code>oc</code>) and a <code>/bin/sh</code>. The
                console cannot check either: an image without them starts fine and answers
                &ldquo;command not found&rdquo;. The preview shows exactly which image will be
                requested.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
      </Form>
    </MutationDialog>
  );
}

export function CliPanel({ height = 420 }) {
  const [createOpen, setCreateOpen] = useState(false);
  const [removing, setRemoving] = useState(null);
  const [openTerminal, setOpenTerminal] = useState(null);

  const listing = useAsync(() => cliApi.session(), { key: 'cli-session' });
  const { gate } = useGates(CHECKS);

  // Memoised because `?? []` is a fresh array on every render, and `running`
  // below derives from it — without this the auto-open effect re-runs on every
  // render and fights the operator closing the terminal.
  const rows = useMemo(() => listing.data?.items ?? [], [listing.data]);
  const enabled = listing.data?.enabled;
  const enabledDetail = listing.data?.enabledDetail;
  const namespace = listing.data?.namespace;
  const serviceAccount = listing.data?.serviceAccount;
  const container = listing.data?.container;

  const execGate = gate('exec');

  const onChanged = useCallback(() => {
    setCreateOpen(false);
    setRemoving(null);
    setOpenTerminal(null);
    // Re-read rather than splicing: what came back is the API server's
    // *projection* of the create, and the pod's real phase — Pending,
    // ImagePullBackOff, Running — comes from the kubelet afterwards.
    listing.reload();
  }, [listing]);

  // Reuse, made visible. One Running pod and no choice made yet is not an
  // ambiguity worth asking about, so its terminal is shown straight away.
  // Showing it opens nothing: `PodTerminal` connects only when its own Open
  // shell is pressed, so no session is started and no audit row is written for
  // an operator who was looking.
  //
  // It happens **once**, which the ref is for. Without it the effect re-derives
  // "exactly one Running pod, so show it" every time `openTerminal` goes back to
  // null — so Close terminal sets the state and the effect immediately undoes
  // it, and the button visibly does nothing in the single-pod case it is most
  // likely to be pressed in. A convenience that cannot be declined is not a
  // convenience.
  const running = useMemo(() => rows.filter((row) => row.phase === 'Running'), [rows]);
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (autoOpenedRef.current || openTerminal || running.length !== 1) return;
    autoOpenedRef.current = true;
    setOpenTerminal(running[0]);
  }, [openTerminal, running]);

  // The deployment gate and the RBAC gate, deployment first. Both are "you
  // cannot do this", but they send an operator to different systems, and the
  // deployment one is the one they can answer without a cluster admin.
  const createGate = useMemo(() => {
    if (enabled === false) return { allowed: false, reason: enabledDetail };
    if (enabled == null) {
      return {
        allowed: false,
        reason: 'Whether this deployment permits CLI pods has not been read yet.',
      };
    }
    return gate('create');
  }, [enabled, enabledDetail, gate]);

  const columns = useMemo(
    () => [
      { key: 'name', title: 'Pod', sortable: true, cell: (row) => <code>{row.name}</code> },
      { key: 'image', title: 'Image', sortable: true },
      {
        key: 'serviceAccount',
        title: 'Runs as',
        sortable: true,
        cell: (row) =>
          // `null` means the pod carries no serviceAccountName and the API
          // server defaulted it. Rendering that as "default" would be a claim
          // about what a shell in this pod is permitted to do, made from a field
          // we did not read.
          row.serviceAccount ? (
            <code>{row.serviceAccount}</code>
          ) : (
            <Tooltip content="This pod names no ServiceAccount, so the API server defaulted it. What kubectl in it can do is not known from here.">
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>Unknown</span>
            </Tooltip>
          ),
      },
      {
        key: 'phase',
        title: 'Phase',
        sortable: true,
        cell: (row) => <StatusBadge status={row.phase ?? 'unknown'} detail={row.reason} />,
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
          // RBAC outranks phase: "you may not exec" is true whatever the pod is
          // doing, and it is the one an operator can act on.
          const reason = !execGate.allowed ? execGate.reason : shellReason(row);
          const shell = (
            <Button
              variant="secondary"
              isAriaDisabled={Boolean(reason)}
              onClick={reason ? undefined : () => setOpenTerminal(row)}
              data-testid={`cli-shell-${row.name}`}
            >
              {openTerminal?.name === row.name ? 'Terminal open' : 'Open shell'}
            </Button>
          );
          return (
            <Split hasGutter>
              <SplitItem>
                {reason ? (
                  <Tooltip content={reason}>
                    <span>{shell}</span>
                  </Tooltip>
                ) : (
                  shell
                )}
              </SplitItem>
              <SplitItem>
                {/* Always offered, at every phase. A pod that failed to start
                    still exists and still holds its ServiceAccount token —
                    "it is not running" is not "it is gone". */}
                <Button
                  variant="link"
                  isDanger
                  onClick={() => setRemoving(row)}
                  data-testid={`cli-remove-${row.name}`}
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
    return <LoadingState label="Reading this cluster's CLI pods…" />;
  }
  if (listing.error) {
    return (
      <ErrorState
        title="CLI pods could not be read"
        error={listing.error}
        onRetry={listing.reload}
      />
    );
  }

  return (
    <div className="admin-cli" data-testid="cli-panel" data-enabled={String(enabled)}>
      {/* §11.1: a short list may be a short cluster or a short permission, and
          the difference matters here — a missed pod means a second one gets
          created beside it. */}
      <PartialBanner unavailable={listing.data?.unavailable} />

      <Split hasGutter style={{ alignItems: 'center', margin: 'var(--admin-gap-sm, 0.5rem) 0' }}>
        <SplitItem>
          <span style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem' }}>
            {rows.length === 0 ? 'No CLI session is running.' : `${rows.length} CLI ${rows.length === 1 ? 'pod' : 'pods'}`}
            {namespace ? ` · namespace ${namespace}` : ''}
            {serviceAccount ? ` · new sessions run as ${serviceAccount}` : ''}
          </span>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <Button
            variant="secondary"
            icon={<SyncAltIcon />}
            onClick={listing.reload}
            isLoading={listing.loading}
            data-testid="cli-refresh"
          >
            Refresh
          </Button>
        </SplitItem>
        <SplitItem>
          <ActionButton
            gate={createGate}
            variant="secondary"
            icon={<TerminalIcon />}
            onClick={() => setCreateOpen(true)}
          >
            Start a session
          </ActionButton>
        </SplitItem>
      </Split>

      {enabled === false && (
        // Not an error and not red: a deployment that has switched this off has
        // made a decision, and rendering that decision as a fault would train
        // operators to ignore the colour that means something is wrong.
        <Alert isInline variant="info" title="A CLI session is not available here" data-testid="cli-disabled">
          {enabledDetail}
        </Alert>
      )}

      {rows.length > 0 ? (
        <DataTable
          ariaLabel="CLI pods"
          tableId="cli-pods"
          columns={columns}
          rows={rows}
          rowKey={(row) => row.name}
          resizableColumns={false}
        />
      ) : (
        <EmptyState
          icon={TerminalIcon}
          title="No CLI session is running"
          description={
            'A CLI session is a pod carrying kubectl, with a shell into it, for the command this ' +
            'console has no page for. What it can reach is decided by the ServiceAccount the pod ' +
            'binds — not by your permissions — and nothing you type in it appears in the audit ' +
            'trail. The pod is previewed in full before it is made, and nothing removes it ' +
            'afterwards except you.'
          }
          data-testid="cli-empty"
        />
      )}

      {openTerminal && (
        <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }}>
          <Split hasGutter style={{ alignItems: 'center' }}>
            <SplitItem isFilled>
              <strong>Shell in {openTerminal.name}</strong>{' '}
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>
                — kubectl here runs as{' '}
                <code>{openTerminal.serviceAccount ?? 'an account this console could not read'}</code>
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
            container={container}
            height={height}
          />
        </div>
      )}

      <CliSessionDialog
        isOpen={createOpen}
        namespace={namespace}
        serviceAccount={serviceAccount}
        defaultImage={listing.data?.image}
        onClose={() => setCreateOpen(false)}
        onApplied={onChanged}
      />

      {removing && (
        // No form, so `MutationDialog` dry-runs on open and shows the delete
        // diff — §4 fixes that as `before=live, after=null`, the whole manifest
        // disappearing. That comes free from the funnel.
        <MutationDialog
          isOpen
          title={`Remove ${removing.name}`}
          description={`Deletes the CLI pod from ${removing.namespace ?? namespace}. Nothing it ran is undone.`}
          confirmLabel="Remove"
          isDanger
          request={(dryRun) => cliApi.remove(removing.name, { dryRun })}
          onClose={() => setRemoving(null)}
          onApplied={onChanged}
          summarize={(result) =>
            result?.applied === true
              ? {
                  variant: 'success',
                  title: `${removing.name} is removed`,
                  body:
                    'The pod is deleted and its ServiceAccount token goes with it. Anything it ' +
                    'already did to the cluster stands — removing the shell does not undo what was ' +
                    'typed into it.',
                }
              : {
                  variant: 'warning',
                  title: 'The request completed without confirming the pod was removed',
                  body: 'Refresh this panel before assuming the CLI pod is gone.',
                }
          }
        />
      )}
    </div>
  );
}

export default CliPanel;
