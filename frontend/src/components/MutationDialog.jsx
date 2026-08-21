/**
 * MutationDialog — contract rule 11.3, implemented once.
 *
 * > *Mutation flow is always: build request → call with `dryRun: true` → show
 * > the returned unified diff → user confirms → call with `dryRun: false`.
 * > There is no button that writes to a cluster without having shown a diff
 * > first.*
 *
 * That is a property of the whole console, and a property of a whole console is
 * not something ten dialogs can each be trusted to remember. Every write in
 * this app goes through here: Scale, Restart, Delete, Rollback, Suspend, Cordon
 * and Drain are all thin wrappers that supply a `request` closure and a form,
 * and none of them can reach the cluster without passing through the handshake
 * below. A dialog that re-implemented it would be one refactor away from
 * skipping the preview on the one path nobody re-tested.
 *
 * ## The handshake
 *
 * ```
 *   form ──preview──▶ diff ──confirm──▶ done
 *     ▲                 │
 *     └──── back ───────┘        (409 on confirm ──▶ re-preview ──▶ diff)
 * ```
 *
 * **The form only exists in the form phase.** Once a diff is on screen the
 * inputs are gone, so there is no state in which an operator can change
 * `replicas` (or `force`, or the YAML) and then confirm a diff that was
 * projected for a different request. Editing means going back, which discards
 * the diff and requires a fresh dry run. This is structural rather than
 * disciplined: the drift it prevents is invisible on screen exactly when it
 * matters.
 *
 * **`applied` is the only evidence of a write.** §1.5: a successful dry run
 * returns a full projected object, a `resourceVersion` and a diff, and
 * `applied: false`. Nothing in this component reports success from a dry run —
 * the success summary is reachable only from the `done` phase, which is
 * reachable only from a resolved `dryRun: false` call.
 *
 * **`changed: false` is not a write.** The API server compared the request to
 * the live object and found no difference. No Confirm button is offered at all;
 * confirming would produce an audit row and a success toast for a write with no
 * effect, which is a lie about the cluster in the one record that is supposed
 * to be authoritative about it.
 *
 * **A 409 re-runs the dry run.** §0.4 makes optimistic concurrency mandatory,
 * and the point of it is lost if the UI reacts to a conflict by retrying with
 * `force` or by re-sending the same body. Here a conflict adopts the server's
 * `currentResourceVersion`, re-projects against the object as it *now* is, and
 * puts the **new** diff in front of the operator with a banner saying the
 * object moved. What they approved the first time is discarded, not resubmitted:
 * the whole reason they were shown a diff was to consent to that specific
 * change, and the object underneath it is no longer the one they read.
 *
 * **A denial names the grant.** §0.2 has the backend compute a `hint` naming
 * the missing permission. Flattening that into "forbidden" throws away the only
 * part of the response that tells the operator what to do next.
 *
 * ## Wrapper contract
 *
 * ```jsx
 * <MutationDialog
 *   isOpen={open}
 *   title={`Scale ${name}`}
 *   request={(dryRun) => workloads.scale(plural, ns, name, { replicas, dryRun })}
 *   onClose={() => setOpen(false)}
 *   onApplied={(result) => refresh()}
 * >
 *   <NumberInput ... />
 * </MutationDialog>
 * ```
 *
 * `request(dryRun, { resourceVersion })` is the only thing a wrapper must
 * supply. The second argument carries the freshest `resourceVersion` this
 * dialog knows about — the one the operator loaded, then whatever the dry run
 * echoed, then whatever a 409 reported — so a wrapper that sends `PUT` bodies
 * (§4) has the correct value without tracking conflicts itself.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextInput,
} from '@patternfly/react-core';
import DiffView from './DiffView';
import { LoadingState } from './ui';
import { useHealth } from '../contexts/HealthContext';
import { useNotify } from '../contexts/NotificationContext';

/* ── Error presentation ─────────────────────────────────────────────────── */

/**
 * Per-code framing for the §1.3 envelope.
 *
 * Keyed on `error.code`, never on the message text — the client's docstring is
 * explicit that a UI matching on words in a sentence changes behaviour the day
 * somebody rewords it. An unknown code falls through to the error's own
 * message rather than to a generic "something went wrong", because a backend
 * that grew a new code must not be flattened into a vaguer one by an older
 * frontend.
 */
const ERROR_FRAMING = {
  rbac_denied: {
    variant: 'danger',
    title: 'This account is not permitted to make this change',
    lead: 'The preflight SelfSubjectAccessReview refused it, so nothing was sent to the cluster.',
  },
  mutations_disabled: {
    variant: 'warning',
    // Deliberately neutral. This code covers two different refusals: the global
    // read-only switch, and a per-feature gate such as the one on §5.5's node
    // debug pods. The old copy asserted both "this console is running read-only"
    // and "dry runs remain available", and neither is true of a feature gate —
    // the console writes perfectly well, and §5.5 refuses the projection too.
    // The error's own `message` and `hint` are rendered directly below this and
    // always name the specific switch, so nothing is lost by the framing
    // declining to guess which case it is looking at.
    title: 'This deployment does not permit this write',
    lead: 'It was refused before the cluster was touched.',
  },
  conflict: {
    variant: 'warning',
    title: 'The object changed while you were looking at it',
    lead: 'Nothing was written. The diff below has been re-projected against the object as it is now.',
  },
  invalid: {
    variant: 'danger',
    title: 'The cluster rejected this change as invalid',
    lead: 'Schema validation or an admission controller refused it. Nothing was written.',
  },
  not_found: {
    variant: 'warning',
    title: 'That object no longer exists',
    lead: 'It may have been deleted since this page was loaded.',
  },
  cluster_unreachable: {
    variant: 'danger',
    title: 'The cluster could not be reached',
    // The distinction the client draws between its two unreachables matters to
    // an operator deciding which component to go and look at.
    lead: 'The console is up; it could not reach this cluster’s API server. Whether the write landed is unknown.',
  },
  network_unreachable: {
    variant: 'danger',
    title: 'The console backend could not be reached',
    lead: 'This says nothing about the cluster’s health — the request never left this browser’s reach.',
  },
  unsupported: {
    variant: 'warning',
    title: 'This cluster does not serve that API',
    lead: 'Nothing is broken; the resource simply is not present here.',
  },
  no_cluster_selected: {
    variant: 'warning',
    title: 'No cluster is selected',
    lead: 'Choose a cluster before making changes.',
  },
};

function MutationError({ error, phase }) {
  if (!error) return null;
  const framing = ERROR_FRAMING[error.code] ?? {
    variant: 'danger',
    title: 'The change could not be completed',
    lead: null,
  };

  return (
    <Alert
      isInline
      variant={framing.variant}
      title={framing.title}
      className="admin-confirm__alert"
      data-testid="mutation-error"
      data-error-code={error.code || 'unknown'}
    >
      {framing.lead && <p>{framing.lead}</p>}
      <p>{error.message}</p>
      {/* §1.3: `detail` is the API server's verbatim words, for humans only.
          Rendered as opaque text; nothing in this app parses it. */}
      {error.detail && (
        <pre
          style={{
            whiteSpace: 'pre-wrap',
            margin: 'var(--admin-gap-sm, 0.5rem) 0 0',
            fontSize: '0.8125rem',
          }}
        >
          {error.detail}
        </pre>
      )}
      {/* The repair instruction. This is the entire reason app.admin.preflight
          computes a hint instead of relaying "forbidden", and dropping it here
          would waste that work at the last possible step. */}
      {error.hint && (
        <p style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)', fontWeight: 600 }}>{error.hint}</p>
      )}
      <p style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)', fontSize: '0.8125rem' }}>
        Error code: <code>{error.code || 'unknown'}</code>
        {phase === 'applying' && error.code !== 'rbac_denied' && error.code !== 'mutations_disabled' ? (
          // A failure *during* the confirming call is the one case where we
          // genuinely may not know whether the cluster changed. Saying "nothing
          // was written" here would be a guess presented as a fact.
          <> — this failed during the write. Re-check the object before retrying.</>
        ) : null}
      </p>
    </Alert>
  );
}

/* ── Default result summary ─────────────────────────────────────────────── */

/**
 * What the `done` phase says when a wrapper does not override it.
 *
 * `applied === true` is the only route to a success headline. A response that
 * came back without it — which should be impossible from a `dryRun: false`
 * call, but is exactly the case where a confident wrong answer is most
 * expensive — reports that we cannot confirm the write rather than assuming it.
 */
function defaultSummarize(result) {
  if (result?.applied === true) {
    return { variant: 'success', title: 'Applied to the cluster', body: null };
  }
  return {
    variant: 'warning',
    title: 'The write completed without confirming it was applied',
    body:
      'The API server answered, but the response did not report `applied: true`. Re-read the object before ' +
      'assuming this change landed.',
  };
}

/* ── The dialog ─────────────────────────────────────────────────────────── */

export function MutationDialog({
  isOpen,
  title,
  description,
  /** `(dryRun, { resourceVersion }) => Promise<MutationResponse>` (§1.5). */
  request,
  /** The `resourceVersion` the operator was looking at, for §0.4. */
  resourceVersion: initialResourceVersion = null,
  /** Phase-1 form controls. Rendered only in the form phase — see the docstring. */
  children,
  /** False disables Preview; pair it with `previewDisabledReason`. */
  canPreview = true,
  previewDisabledReason,
  previewLabel = 'Preview changes',
  confirmLabel = 'Apply',
  isDanger = false,
  /** The operator must type this exact string before Confirm enables. */
  requireTyped,
  /** Skip the form phase and dry-run on open. Defaults to "there is no form". */
  autoPreview,
  /** `(result) => string | null` — non-null disables Confirm and states why. */
  confirmBlockedReason,
  /** `({ result, phase, error }) => node` — extra body content, e.g. a drain plan. */
  renderExtra,
  /** `(result) => { variant, title, body }` — overrides the `done` headline. */
  summarize = defaultSummarize,
  onApplied,
  onClose,
  variant = 'large',
  className,
}) {
  const { mutationsEnabled, reason: healthReason } = useHealth();
  const { notify } = useNotify();

  const [phase, setPhase] = useState('form');
  const [preview, setPreview] = useState(null);
  const [applied, setApplied] = useState(null);
  const [error, setError] = useState(null);
  const [conflicted, setConflicted] = useState(false);
  const [typed, setTyped] = useState('');
  const [resourceVersion, setResourceVersion] = useState(initialResourceVersion);

  // Every async path checks this before touching state. The dialog is unmounted
  // by its parent the moment Cancel is pressed, and a dry run that resolves
  // afterwards would otherwise warn — or worse, a confirm that resolves after a
  // cancel would fire `onApplied` and refresh a page for a dialog the operator
  // believes they backed out of.
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const shouldAutoPreview = autoPreview ?? children == null;
  const autoRan = useRef(false);

  // Reset on every opening. A dialog reopened for a different object that kept
  // the previous diff would show the operator someone else's change under the
  // right title — and the typed-confirmation phrase from the last object would
  // already satisfy the guard for this one.
  useEffect(() => {
    if (!isOpen) return;
    setPhase('form');
    setPreview(null);
    setApplied(null);
    setError(null);
    setConflicted(false);
    setTyped('');
    setResourceVersion(initialResourceVersion);
    autoRan.current = false;
  }, [isOpen, initialResourceVersion]);

  /**
   * Phase 1. Always `dryRun: true`, permitted even in read-only mode (§1.6):
   * inspecting what would change is a read, and that is what makes this console
   * usable in an audit posture.
   */
  const runPreview = useCallback(
    async ({ afterConflict = false, version } = {}) => {
      setPhase('preview');
      setError(null);
      const rv = version ?? resourceVersion;
      try {
        const result = await request(true, { resourceVersion: rv });
        if (!live.current) return;
        setPreview(result);
        // The dry run succeeded, so the version we sent was accepted as current
        // and the echoed one is what the confirming call must carry (§1.5).
        if (result?.resourceVersion) setResourceVersion(String(result.resourceVersion));
        setConflicted(afterConflict);
        setPhase('diff');
      } catch (err) {
        if (!live.current) return;
        setError(err);
        // Back to the form so the operator can correct the input that caused
        // it. With no form this reads as "retry preview", which is the only
        // other useful move.
        setPhase('form');
      }
    },
    [request, resourceVersion],
  );

  useEffect(() => {
    if (!isOpen || !shouldAutoPreview || autoRan.current) return;
    autoRan.current = true;
    runPreview();
  }, [isOpen, shouldAutoPreview, runPreview]);

  /**
   * Phase 2. The only call in this component that can change a cluster.
   */
  const runApply = useCallback(async () => {
    setPhase('applying');
    setError(null);
    try {
      const result = await request(false, { resourceVersion });
      if (!live.current) return;
      setApplied(result);
      setPhase('done');
      const summary = summarize(result);
      notify(summary.title, summary.variant === 'success' ? 'success' : summary.variant);
      onApplied?.(result);
    } catch (err) {
      if (!live.current) return;
      if (err?.code === 'conflict') {
        // §0.4. Do not resubmit and do not offer a force: the operator approved
        // a diff against an object that has since moved, so that approval is
        // spent. Re-project against the current object and make them read the
        // new one.
        // §4 puts the current version in `context.currentResourceVersion`.
        // When it is absent — an API-server-side conflict rather than a stale
        // rV — the re-preview goes out with what we have and either succeeds
        // or reports the next failure honestly.
        const fresh = err?.context?.currentResourceVersion;
        if (fresh) setResourceVersion(String(fresh));
        // No `setError` here: `runPreview` clears it immediately anyway, and
        // the conflict banner it raises says more than the raw envelope would.
        // If the re-projection itself fails, that error is the one worth
        // showing — it is the more recent statement about the object.
        await runPreview({ afterConflict: true, version: fresh ? String(fresh) : undefined });
        return;
      }
      setError(err);
      // Stay on the diff. The projection is still the best description of what
      // was attempted, and blanking it would leave the operator with an error
      // and no record of what they had asked for.
      setPhase('diff');
    }
  }, [request, resourceVersion, summarize, notify, onApplied, runPreview]);

  const result = phase === 'done' ? applied : preview;

  // `changed` is only meaningful when the backend actually reported it (§1.5).
  // An absent diff — a wrapper whose action has no textual diff — must not be
  // read as "nothing would change" and silently lose its Confirm button.
  const hasVerdict = typeof preview?.diff?.changed === 'boolean';
  const noOp = hasVerdict && preview.diff.changed === false;

  const blockedReason = useMemo(
    () => (preview && confirmBlockedReason ? confirmBlockedReason(preview) : null),
    [preview, confirmBlockedReason],
  );

  const typedOk = !requireTyped || typed === requireTyped;
  // Rule 11.4: disabled with the reason, never hidden. The health gate outranks
  // the wrapper's own reason because it is the one the operator can do least
  // about — telling someone to resolve three blocked pods on a console that
  // cannot write at all wastes the trip. An unsatisfied `requireTyped` is
  // deliberately NOT in here: the text field is its own visible reason, and an
  // alert saying "type the name" above the box asking for the name is noise.
  const confirmDisabledReason = !mutationsEnabled
    ? healthReason || 'This console is not currently permitted to write to clusters.'
    : blockedReason || null;

  const warnings = preview?.warnings ?? [];
  const summary = phase === 'done' && applied ? summarize(applied) : null;

  if (!isOpen) return null;

  const busy = phase === 'preview' || phase === 'applying';

  return (
    <Modal
      isOpen
      variant={variant}
      onClose={busy ? undefined : onClose}
      aria-label={typeof title === 'string' ? title : 'Confirm change'}
      className={className}
      data-testid="mutation-dialog"
      data-phase={phase}
    >
      <ModalHeader title={title} titleIconVariant={isDanger ? 'warning' : undefined} />

      <ModalBody>
        {description && <p className="admin-confirm__description">{description}</p>}

        {/* Read-only mode is stated up front, at every phase, so an operator
            filling in a form learns that the Apply at the end is unavailable
            before they fill it in rather than after (§11.5). */}
        {!mutationsEnabled && (
          <Alert isInline variant="info" title="Preview only" className="admin-confirm__alert">
            {healthReason || 'Writes are unavailable on this console right now.'} You can still run the dry
            run below and read exactly what would change.
          </Alert>
        )}

        {conflicted && phase === 'diff' && (
          <Alert
            isInline
            variant="warning"
            title="This is a new diff — the object changed since you previewed it"
            className="admin-confirm__alert"
            data-testid="mutation-conflict"
          >
            Your previous confirmation was not applied. The API server reported that the object had been
            modified by someone or something else, so the change was re-projected against the current object.
            Read this diff before confirming: it is not the one you approved.
          </Alert>
        )}

        <MutationError error={error} phase={phase} />

        {phase === 'form' && children}

        {phase === 'preview' && <LoadingState label="Asking the API server what would change…" />}

        {(phase === 'diff' || phase === 'applying' || phase === 'done') && result && (
          <>
            {noOp && phase !== 'done' && (
              <Alert
                isInline
                variant="info"
                title="Nothing would change"
                className="admin-confirm__alert"
                data-testid="mutation-noop"
              >
                The API server compared this request against the live object and found no difference.
                There is nothing to apply, so no Apply button is offered — confirming would write an audit
                record for a change with no effect.
              </Alert>
            )}

            {warnings.length > 0 && (
              // §1.5: the API server's own `Warning:` headers, verbatim, above
              // the decision rather than in a toast that arrives after it.
              <Alert
                isInline
                variant="warning"
                title={`The API server returned ${warnings.length} warning${warnings.length === 1 ? '' : 's'}`}
                className="admin-confirm__alert"
                data-testid="mutation-warnings"
              >
                <ul className="admin-confirm__warnings">
                  {warnings.map((warning, i) => (
                    <li key={i}>{warning}</li>
                  ))}
                </ul>
              </Alert>
            )}

            {summary && (
              <Alert
                isInline
                variant={summary.variant}
                title={summary.title}
                className="admin-confirm__alert"
                data-testid="mutation-summary"
              >
                {summary.body}
                {applied?.auditId != null && (
                  <p style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}>
                    Recorded in the audit trail as{' '}
                    <Link to="/audit" onClick={onClose}>
                      #{applied.auditId}
                    </Link>
                    .
                  </p>
                )}
              </Alert>
            )}

            {phase !== 'done' && (
              <div className="admin-confirm__diff">
                <p className="admin-confirm__diff-label">
                  This is the difference the API server projected for this write. Nothing has been applied
                  yet.
                </p>
                <DiffView
                  unified={result?.diff?.unified}
                  changed={result?.diff?.changed}
                  ariaLabel="Projected changes"
                />
              </div>
            )}
          </>
        )}

        {renderExtra?.({ result, phase, error })}

        {phase === 'diff' && confirmDisabledReason && (
          <Alert
            isInline
            variant="warning"
            title="Apply is unavailable"
            className="admin-confirm__alert"
            data-testid="mutation-blocked"
          >
            {confirmDisabledReason}
          </Alert>
        )}

        {phase === 'diff' && !noOp && requireTyped && (
          <div className="admin-confirm__typed">
            <label htmlFor="mutation-typed">
              This one is not reversible. Type <code>{requireTyped}</code> to confirm.
            </label>
            <TextInput
              id="mutation-typed"
              value={typed}
              onChange={(_event, next) => setTyped(next)}
              aria-label={`Type ${requireTyped} to confirm`}
              autoComplete="off"
              data-testid="mutation-typed"
            />
          </div>
        )}
      </ModalBody>

      <ModalFooter>
        {phase === 'form' && (
          <Button
            variant="primary"
            onClick={() => runPreview()}
            isDisabled={!canPreview}
            data-testid="mutation-preview"
            // Rule 11.4 again: the button stays visible and says why it is off.
            title={!canPreview ? previewDisabledReason : undefined}
          >
            {error ? 'Try preview again' : previewLabel}
          </Button>
        )}

        {phase === 'preview' && (
          <Button variant="primary" isLoading isDisabled data-testid="mutation-preview">
            Previewing…
          </Button>
        )}

        {(phase === 'diff' || phase === 'applying') && !noOp && (
          <Button
            variant={isDanger ? 'danger' : 'primary'}
            onClick={runApply}
            isDisabled={phase === 'applying' || !typedOk || Boolean(confirmDisabledReason)}
            isLoading={phase === 'applying'}
            data-testid="mutation-confirm"
          >
            {phase === 'applying' ? 'Applying…' : confirmLabel}
          </Button>
        )}

        {(phase === 'diff' || phase === 'preview') && children != null && (
          <Button
            variant="secondary"
            onClick={() => {
              // Discard the projection with the inputs it was projected for.
              // Keeping it would let the next Confirm apply a diff that no
              // longer describes the request.
              setPreview(null);
              setError(null);
              setConflicted(false);
              setTyped('');
              setPhase('form');
            }}
            data-testid="mutation-back"
          >
            Back to edit
          </Button>
        )}

        {phase === 'done' ? (
          <Button variant="primary" onClick={onClose} data-testid="mutation-close">
            Close
          </Button>
        ) : (
          <Button
            variant="link"
            onClick={onClose}
            isDisabled={phase === 'applying'}
            data-testid="mutation-cancel"
          >
            {noOp ? 'Close' : 'Cancel'}
          </Button>
        )}

        {!canPreview && phase === 'form' && previewDisabledReason && (
          <span
            style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem' }}
            data-testid="mutation-preview-disabled-reason"
          >
            {previewDisabledReason}
          </span>
        )}
      </ModalFooter>
    </Modal>
  );
}

export default MutationDialog;
