/**
 * StatusBadge — one pill, one colour vocabulary, for every state string this
 * console renders.
 *
 * The rule that matters here: `Unknown` is grey, and is never treated as a
 * synonym for healthy. §6 requires the backend to emit `Unknown` for a workload
 * whose controller status has not been observed, precisely so the UI can say
 * "we have not seen this" instead of implying it is fine. A default branch that
 * fell through to green would erase that at the last possible moment.
 *
 * The same applies to a pod: §6 gives us `phase_detail` because `phase` lies —
 * a `Running` pod with a CrashLoopBackOff container is not running. Pass
 * `phase_detail` when it is present and the pill will be red.
 */
import { Label, Tooltip } from '@patternfly/react-core';

// color: a PatternFly Label colour. `grey` is the deliberate "we do not know".
const STATES = {
  // Healthy / settled
  healthy: { color: 'green', label: 'Healthy' },
  ready: { color: 'green', label: 'Ready' },
  running: { color: 'green', label: 'Running' },
  active: { color: 'green', label: 'Active' },
  bound: { color: 'green', label: 'Bound' },
  connected: { color: 'green', label: 'Connected' },
  succeeded: { color: 'green', label: 'Succeeded' },
  available: { color: 'green', label: 'Available' },
  complete: { color: 'green', label: 'Complete' },
  applied: { color: 'green', label: 'Applied' },
  allowed: { color: 'green', label: 'Allowed' },
  true: { color: 'green', label: 'True' },

  // In motion
  progressing: { color: 'blue', label: 'Progressing' },
  pending: { color: 'orange', label: 'Pending' },
  containercreating: { color: 'orange', label: 'ContainerCreating' },
  terminating: { color: 'orange', label: 'Terminating' },
  released: { color: 'orange', label: 'Released' },
  updating: { color: 'blue', label: 'Updating' },
  dry_run: { color: 'blue', label: 'Dry run' },

  // Wrong
  degraded: { color: 'red', label: 'Degraded' },
  failed: { color: 'red', label: 'Failed' },
  error: { color: 'red', label: 'Error' },
  crashloopbackoff: { color: 'red', label: 'CrashLoopBackOff' },
  imagepullbackoff: { color: 'red', label: 'ImagePullBackOff' },
  errimagepull: { color: 'red', label: 'ErrImagePull' },
  createcontainerconfigerror: { color: 'red', label: 'CreateContainerConfigError' },
  evicted: { color: 'red', label: 'Evicted' },
  notready: { color: 'red', label: 'NotReady' },
  unreachable: { color: 'red', label: 'Unreachable' },
  lost: { color: 'red', label: 'Lost' },
  denied: { color: 'red', label: 'Denied' },
  conflict: { color: 'red', label: 'Conflict' },
  blocked: { color: 'red', label: 'Blocked' },
  warning: { color: 'orange', label: 'Warning' },
  false: { color: 'red', label: 'False' },

  // Deliberately not running
  suspended: { color: 'purple', label: 'Suspended' },
  cordoned: { color: 'purple', label: 'Cordoned' },
  unschedulable: { color: 'purple', label: 'Unschedulable' },
  disabled: { color: 'grey', label: 'Disabled' },

  // We do not know. Grey, never green.
  unknown: { color: 'grey', label: 'Unknown' },
  not_observed: { color: 'grey', label: 'Not observed' },
  unsupported: { color: 'grey', label: 'Not present' },
  normal: { color: 'grey', label: 'Normal' },
};

const UNKNOWN_HINT =
  'The controller status for this object has not been observed. Unknown is not the same as healthy.';

export function StatusBadge({ status, label, detail, tooltip, isCompact = true, icon, className }) {
  const key = String(status ?? '').trim().toLowerCase().replace(/[\s-]+/g, '_');
  // `phase_detail` (§6) overrides the phase when it disagrees: a Running pod
  // whose container is CrashLoopBackOff must not show a green pill.
  const detailKey = detail ? String(detail).trim().toLowerCase().replace(/[\s-]+/g, '_') : null;
  const entry =
    (detailKey && STATES[detailKey]) ||
    STATES[key] ||
    // An unrecognised state is grey and shown verbatim. Guessing a colour for a
    // string we do not understand is how a new failure reason arrives on screen
    // looking reassuring.
    { color: 'grey', label: detail || status || 'Unknown' };

  const text = label ?? entry.label;
  const hint = tooltip ?? (key === 'unknown' || key === '' ? UNKNOWN_HINT : detail && detailKey !== key ? detail : null);

  const pill = (
    <Label isCompact={isCompact} color={entry.color} icon={icon} className={className} data-testid="status-badge">
      {text}
    </Label>
  );

  // Tooltip attaches a ref to its child; the wrapping span guarantees a DOM
  // node to attach to, and tabIndex makes the explanation of an `Unknown` pill
  // reachable from the keyboard rather than hover-only.
  return hint ? (
    <Tooltip content={hint}>
      <span tabIndex={0} className="admin-status-badge">
        {pill}
      </span>
    </Tooltip>
  ) : (
    pill
  );
}

export default StatusBadge;
