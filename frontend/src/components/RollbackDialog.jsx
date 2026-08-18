/**
 * RollbackDialog — `GET .../rollout` then `POST .../rollback` (§6).
 *
 * The only dialog here that reads before it can offer anything: a rollback
 * needs a revision, and §6 puts the revision list behind its own endpoint
 * because Deployments read it from ReplicaSets and StatefulSets/DaemonSets from
 * ControllerRevisions. So this dialog has three states before the handshake
 * even starts, and the whole point is that they do not collapse into one:
 *
 *   - the history is loading;
 *   - the history is **empty** — the workload genuinely has one revision;
 *   - the history could not be read, or this kind has none
 *     (`unavailable: [{reason: "unsupported"}]`).
 *
 * §1.2 and the defect standard both land on the same rule here: a kind with no
 * revision history and a cluster we could not ask must not render as the same
 * empty dropdown. The first is a fact about the workload, the second is a fact
 * about us, and an operator who reads the second as the first concludes their
 * Deployment has no history and goes looking for a backup.
 *
 * The revision list shows images and `change_cause`, because "revision 13" is
 * not a thing anybody recognises and `…/checkout:1.9.1` is.
 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Form, FormGroup, Radio } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { LoadingState, PartialBanner } from './ui';
import { workloads as workloadsApi } from '../api/client';
import { formatTimestamp } from '../utils/format';

// Mirrors `_NO_ROLLBACK` in backend/app/api/workloads.py, verbatim.
const NO_ROLLBACK = {
  Job: 'A Job has no revision history: it runs once with the template it was created with.',
  CronJob:
    'A CronJob has no revision history. The Jobs it created keep the template they were created with; edit ' +
    'the CronJob to change future ones.',
  ReplicaSet:
    'A ReplicaSet is itself one revision of a Deployment. Roll back the Deployment that owns it and it will ' +
    'scale this ReplicaSet back up.',
};

export function RollbackDialog({ isOpen, kind, plural, namespace, name, onClose, onApplied }) {
  const [history, setHistory] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [revision, setRevision] = useState(null);

  const unsupported = kind ? NO_ROLLBACK[kind] : null;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await workloadsApi.rollout(plural, namespace, name);
      setHistory(result);
      // Preselect the newest revision that is not the current one — the
      // overwhelmingly common intent is "undo the last rollout", and making the
      // operator find it in a list they did not ask for is friction with no
      // safety value. Nothing is preselected when there is no such revision,
      // so the dialog cannot offer to "roll back" to where the workload
      // already is.
      const candidates = (result?.revisions ?? []).filter((r) => r.revision !== result?.current);
      setRevision(candidates.length ? candidates[0].revision : null);
    } catch (err) {
      // The list is left null, not emptied: an empty list is the statement
      // "this workload has one revision", and that is not what happened.
      setHistory(null);
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [plural, namespace, name]);

  useEffect(() => {
    if (!isOpen || unsupported) return;
    load();
  }, [isOpen, unsupported, load]);

  const revisions = history?.revisions ?? [];
  const unavailable = history?.unavailable ?? [];
  // §6: a kind with no revision history answers with `current: null`, an empty
  // list and an `unsupported` entry. That is a different sentence from an empty
  // list on its own.
  const noHistorySupported = unavailable.some((entry) => entry?.reason === 'unsupported');
  const selectable = revisions.filter((r) => r.revision !== history?.current);

  let blockReason = null;
  if (unsupported) blockReason = unsupported;
  else if (error) blockReason = `The revision history could not be read (${error.message}). Rolling back without knowing what you are rolling back to is not offered.`;
  else if (loading) blockReason = 'Reading the revision history…';
  else if (noHistorySupported) blockReason = 'This cluster does not keep revision history for this kind, so there is nothing to roll back to.';
  else if (!revisions.length) blockReason = `${name} has no recorded revisions.`;
  else if (!selectable.length) blockReason = `${name} has only its current revision (${history?.current}). There is nothing earlier to return to.`;
  else if (revision == null) blockReason = 'Choose a revision.';

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Roll back ${name}`}
      description={
        blockReason
          ? undefined
          : `${kind || 'Workload'} ${namespace}/${name} is on revision ${history?.current}. Rolling back ` +
            'restores that revision’s pod template and triggers a new rollout — it does not delete the ' +
            'current revision, it becomes another one.'
      }
      canPreview={!blockReason}
      previewDisabledReason={blockReason || undefined}
      confirmLabel={`Roll back to revision ${revision ?? ''}`.trim()}
      isDanger
      request={(dryRun) => workloadsApi.rollback(plural, namespace, name, { revision, dryRun })}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${name} is rolling back to revision ${revision}`,
              body:
                'The pod template was restored. The controller replaces pods under the rollout strategy, so ' +
                'this completes over the next few minutes rather than now.',
            }
          : {
              variant: 'warning',
              title: 'The rollback request completed without confirming it was applied',
              body: 'Re-read the workload before assuming the template changed.',
            }
      }
    >
      <Form>
        {unsupported && (
          <Alert isInline variant="warning" title={`A ${kind} cannot be rolled back`}>
            {unsupported}
          </Alert>
        )}

        {!unsupported && (
          <>
            {/* Rule 11.1: an `unsupported` entry is informational and stays on
                the page next to the (empty) list it explains. */}
            <PartialBanner unavailable={unavailable} />

            {error && (
              <Alert isInline variant="danger" title="The revision history could not be read">
                <p>{error.message}</p>
                {error.hint && <p style={{ fontWeight: 600 }}>{error.hint}</p>}
                <p>
                  This is a failure to read the history, not a workload without one. No revision is offered
                  because choosing blind is worse than not choosing.
                </p>
              </Alert>
            )}

            {loading && <LoadingState label="Reading revision history…" minHeight={100} />}

            {!loading && !error && selectable.length > 0 && (
              <FormGroup label="Revision to restore" fieldId="rollback-revision" isStack>
                {selectable.map((entry) => (
                  <Radio
                    key={entry.revision}
                    id={`rollback-revision-${entry.revision}`}
                    name="rollback-revision"
                    isChecked={revision === entry.revision}
                    onChange={() => setRevision(entry.revision)}
                    data-testid="rollback-revision"
                    label={`Revision ${entry.revision}`}
                    description={
                      <span>
                        {(entry.images ?? []).join(', ') || 'no images recorded'}
                        {entry.created ? ` · ${formatTimestamp(entry.created)}` : ''}
                        {/* `change_cause` is null far more often than not — it
                            only exists when somebody annotated the rollout. An
                            absent one is not rendered as an empty quote. */}
                        {entry.change_cause ? ` · ${entry.change_cause}` : ''}
                      </span>
                    }
                  />
                ))}
              </FormGroup>
            )}

            {!loading && !error && !noHistorySupported && revisions.length > 0 && !selectable.length && (
              <Alert isInline variant="info" title="Nothing earlier to roll back to">
                {name} has only revision {history?.current}.
              </Alert>
            )}
          </>
        )}
      </Form>
    </MutationDialog>
  );
}

export default RollbackDialog;
