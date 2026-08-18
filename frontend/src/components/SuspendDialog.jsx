/**
 * SuspendDialog — `POST /api/workloads/{plural}/{ns}/{name}/suspend` (§6).
 *
 * Jobs and CronJobs only. The dialog is the same component for both directions,
 * because suspend and resume are one field and pretending otherwise gives the
 * operator two buttons whose difference they have to infer from a label.
 *
 * **The direction is always explicit.** The backend's `suspend` field has no
 * default (`app/api/workloads.py`: defaulting it either way lets a mis-sent
 * request suspend a production CronJob, or resume one that was suspended during
 * an incident). The same reasoning applies to the UI: the `suspend` prop is
 * required, the title says which way it goes, and the summary says what state
 * the object is now in rather than "done".
 *
 * **Suspending a CronJob is not stopping what is already running.** §6 makes
 * `suspend` a property of the schedule. Jobs the CronJob has already created
 * keep running, and an operator who suspended a CronJob to stop a runaway batch
 * needs to be told that in the dialog rather than to discover it from a
 * dashboard that did not go quiet. That sentence is the reason this dialog is
 * not just a confirm box.
 */
import { Alert, Form } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { workloads as workloadsApi } from '../api/client';

// Mirrors `_NO_SUSPEND` in backend/app/api/workloads.py, verbatim.
const NO_SUSPEND = {
  Deployment: 'Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.',
  StatefulSet: 'Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.',
  DaemonSet:
    'Only Jobs and CronJobs have spec.suspend. To stop a DaemonSet running, change its nodeSelector so it ' +
    'matches no nodes.',
  ReplicaSet: 'Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.',
};

export function SuspendDialog({
  isOpen,
  kind,
  plural,
  namespace,
  name,
  /** Required. `true` suspends, `false` resumes — never defaulted. */
  suspend,
  onClose,
  onApplied,
}) {
  const unsupported = kind ? NO_SUSPEND[kind] : null;
  const verb = suspend ? 'Suspend' : 'Resume';

  const consequence = suspend
    ? kind === 'CronJob'
      ? 'The schedule stops creating new Jobs. Jobs it has already created keep running to completion — ' +
        'suspending the CronJob does not stop work that is already in flight.'
      : 'The Job stops creating new pods. Pods it has already created keep running.'
    : kind === 'CronJob'
      ? 'The schedule starts creating Jobs again from the next matching time. Occurrences missed while it ' +
        'was suspended are not backfilled beyond the CronJob’s own startingDeadlineSeconds.'
      : 'The Job resumes creating pods towards its completion count.';

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`${verb} ${name}`}
      description={unsupported ? undefined : `${kind || 'Workload'} ${namespace}/${name}. ${consequence}`}
      autoPreview={!unsupported}
      canPreview={!unsupported}
      previewDisabledReason={unsupported || undefined}
      confirmLabel={verb}
      isDanger={Boolean(suspend)}
      request={(dryRun) => workloadsApi.suspend(plural, namespace, name, { suspend, dryRun })}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              // The new state, not the verb. "Suspended" is checkable against
              // the object; "suspend succeeded" is a claim about a request.
              title: `${name} is now ${suspend ? 'suspended' : 'active'}`,
              body: consequence,
            }
          : {
              variant: 'warning',
              title: `The ${verb.toLowerCase()} request completed without confirming it was applied`,
              body: 'Re-read the object to see whether spec.suspend changed.',
            }
      }
    >
      {unsupported ? (
        <Form>
          <Alert isInline variant="warning" title={`A ${kind} cannot be suspended`}>
            {unsupported}
          </Alert>
        </Form>
      ) : null}
    </MutationDialog>
  );
}

export default SuspendDialog;
