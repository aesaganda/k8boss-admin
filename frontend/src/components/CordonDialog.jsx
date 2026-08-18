/**
 * CordonDialog — `POST /api/nodes/{name}/cordon` (§5).
 *
 * One field on the node: `spec.unschedulable`. The dialog exists mostly to say
 * what that field does and, more importantly, what it does *not* do — cordoning
 * is routinely confused with draining, and the confusion runs in the dangerous
 * direction. An operator who cordons a node believing it also evicted the pods
 * walks away thinking a machine is empty when it is still serving production
 * traffic. That is a confidently wrong belief the console handed them, so the
 * sentence stating otherwise is not decoration.
 *
 * Uncordon is the same endpoint with `unschedulable: false`, so it is the same
 * dialog with the direction as a prop — never defaulted, for the reason §6's
 * suspend field is not defaulted either.
 */
import { Alert } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { nodes as nodesApi } from '../api/client';

export function CordonDialog({
  isOpen,
  name,
  /** Required. `true` cordons, `false` uncordons. */
  unschedulable,
  /** Optional: pods currently on the node, purely to size the warning. */
  podCount,
  onClose,
  onApplied,
}) {
  const cordoning = Boolean(unschedulable);

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`${cordoning ? 'Cordon' : 'Uncordon'} ${name}`}
      description={
        cordoning
          ? `The scheduler will stop placing new pods on ${name}. Nothing currently running on it is ` +
            'affected — cordoning does not move, evict or restart a single pod. Use Drain for that.'
          : `The scheduler will start placing new pods on ${name} again. Pods already evicted from it are ` +
            'not moved back; the scheduler places new work wherever it fits.'
      }
      confirmLabel={cordoning ? 'Cordon' : 'Uncordon'}
      isDanger={cordoning}
      request={(dryRun) => nodesApi.cordon(name, { unschedulable: cordoning, dryRun })}
      onClose={onClose}
      onApplied={onApplied}
      // `renderExtra` rather than children: this panel is an explanation, not a
      // form, so it belongs next to the diff and next to the result — not only
      // in a form phase the operator passes through before either exists.
      renderExtra={() =>
        cordoning ? (
          <Alert isInline variant="info" title="Cordon is not drain" className="admin-confirm__alert">
            {podCount == null
              ? // §11.2 in prose: an unknown pod count is not zero, and a
                // sentence that said "0 pods will keep running" would be the
                // exact wrong reassurance.
                'Whatever is running on this node keeps running. The pod count for this node could not be ' +
                'read, so this dialog cannot tell you how much that is.'
              : `${podCount} ${podCount === 1 ? 'pod is' : 'pods are'} running on this node and will keep ` +
                'running. Cordoning only stops new work being scheduled here.'}
          </Alert>
        ) : null
      }
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              // The node's new state, not the verb's outcome.
              title: `${name} is now ${cordoning ? 'unschedulable' : 'schedulable'}`,
              body: cordoning
                ? 'Existing pods are untouched and still serving. Drain the node if you need it emptied.'
                : 'The scheduler may place new pods here again.',
            }
          : {
              variant: 'warning',
              title: 'The request completed without confirming it was applied',
              body: 'Re-read the node before assuming spec.unschedulable changed.',
            }
      }
    />
  );
}

export default CordonDialog;
