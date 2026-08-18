/**
 * RestartDialog — `POST /api/workloads/{plural}/{ns}/{name}/restart` (§6).
 *
 * A rollout restart has no parameters, so this dialog has no form phase: it
 * dry-runs on open and lands the operator straight on the diff. The diff is the
 * point. `kubectl rollout restart` is a verb whose effect is invisible from its
 * name — it stamps an annotation on the pod template, and the pod template
 * changing is what makes the controller replace every pod. An operator who has
 * only ever read the verb tends to assume it restarts the *containers* in place.
 * The projected diff shows a one-line annotation change, and the description
 * below connects that line to "every pod is replaced", which is the fact that
 * actually matters at 3am.
 *
 * `_NO_RESTART` from the backend is mirrored for the same reason as in
 * `ScaleDialog`: rule 11.4 wants the action visible and disabled with its
 * reason, not offered and then refused.
 */
import { Alert, Form } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { workloads as workloadsApi } from '../api/client';

// Mirrors `_NO_RESTART` in backend/app/api/workloads.py, verbatim.
const NO_RESTART = {
  Job:
    "A Job's pod template is immutable once it is created, so there is nothing to re-roll. Create a new Job, " +
    'or suspend and delete this one.',
  CronJob:
    'A CronJob does not run pods itself. Its next Job will use the current template; restart the Jobs it has ' +
    'already created if you need to.',
  ReplicaSet:
    'kubectl rollout restart does not apply to a ReplicaSet. Restart the Deployment that owns it — restarting ' +
    'the ReplicaSet directly is undone by the Deployment controller on its next reconcile.',
};

export function RestartDialog({ isOpen, kind, plural, namespace, name, onClose, onApplied }) {
  const unsupported = kind ? NO_RESTART[kind] : null;

  // With an unsupported kind there is nothing to preview, so the auto-preview
  // that a parameterless action would normally use is switched off and the
  // dialog stays on a form phase whose only content is the explanation.
  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Restart ${name}`}
      description={
        unsupported
          ? undefined
          : `${kind || 'Workload'} ${namespace}/${name}. This stamps a timestamp annotation on the pod ` +
            'template, exactly as `kubectl rollout restart` does. Changing the template is what makes the ' +
            'controller replace the pods, so every pod is recreated under the rollout strategy — this is ' +
            'not an in-place container restart.'
      }
      autoPreview={!unsupported}
      canPreview={!unsupported}
      previewDisabledReason={unsupported || undefined}
      confirmLabel="Restart"
      request={(dryRun) => workloadsApi.restart(plural, namespace, name, { dryRun })}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `Rollout restart triggered for ${name}`,
              body:
                'The pod template annotation was updated. Pods are replaced by the controller under the ' +
                'workload’s rollout strategy, so this completes over the next few minutes rather than now — ' +
                'the rollout view is where it can be watched.',
            }
          : {
              variant: 'warning',
              title: 'The restart request completed without confirming it was applied',
              body: 'Re-read the workload before assuming a rollout has started.',
            }
      }
    >
      {unsupported ? (
        <Form>
          <Alert isInline variant="warning" title={`A ${kind} cannot be restarted this way`}>
            {unsupported}
          </Alert>
        </Form>
      ) : null}
    </MutationDialog>
  );
}

export default RestartDialog;
