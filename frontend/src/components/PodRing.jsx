/**
 * PodRing — ready-over-desired drawn as a ring, with `ScaleStepper`'s arrows
 * beside it.
 *
 * The numbers are the ones §6's WorkloadRow already carries, and the ring is a
 * second rendering of them, not a second reading: nothing here fetches, and the
 * arrows are the same ones the workload page has, which open `ScaleDialog`
 * rather than writing. A picture of a replica count that could post one would
 * be rule 3's "button that writes to a cluster without having shown a diff
 * first" with a nicer shape.
 *
 * **A ring is a proportion, and `null` has none.** §6 makes both
 * `replicas.ready` and `replicas.desired` nullable, and the obvious drawing —
 * an arc at `ready / desired` with the nulls coerced to zero — is an empty ring
 * around a `0`, which is what a scaled-to-zero workload looks like. That is
 * rule 11.2's failure in a new medium: an operator reading "no pods" from a
 * controller that simply did not report acts on it. So an unreported count
 * draws the track **dashed** and puts the em dash in the middle, the same two
 * signals the tables use, and the reason is in `title` and in the accessible
 * name rather than only in a tooltip.
 *
 * `desired: 0` is the opposite case and must stay distinguishable from it: a
 * solid, empty track and a real `0`, because that one *is* the answer.
 */
import ScaleStepper from './ScaleStepper';

const R = 40;
const CIRCUMFERENCE = 2 * Math.PI * R;

const UNREADABLE = 'The controller has not reported this count, so it is unknown — not zero.';

export function PodRing({
  /** `replicas.ready`. `null` means unread. */
  ready,
  /** `replicas.desired`. `null` means unread. */
  desired,
  /** §6's workload status, which colours the arc. */
  status = 'Unknown',
  /** Everything `ScaleStepper` needs; omit `gate` to render the ring alone. */
  kind,
  plural,
  namespace,
  name,
  gate,
  onApplied,
  testId = 'pod-ring',
  stepperTestId,
}) {
  const known = ready != null && desired != null;
  // Clamped: a controller reporting more ready than desired mid-rollout must
  // not draw an arc longer than the ring it sits on.
  const fraction = known && desired > 0 ? Math.min(1, ready / desired) : 0;

  const label = known
    ? `${ready} of ${desired} ${desired === 1 ? 'pod' : 'pods'} ready`
    : `Pods ready: unknown. ${UNREADABLE}`;

  return (
    <div className="admin-pod-ring" data-testid={testId} data-status={status} data-known={known}>
      <svg viewBox="0 0 96 96" width="96" height="96" role="img" aria-label={label}>
        <title>{label}</title>
        <circle className="admin-pod-ring__track" cx="48" cy="48" r={R} />
        {known && fraction > 0 && (
          <circle
            className="admin-pod-ring__arc"
            cx="48"
            cy="48"
            r={R}
            // Dash one arc's worth, then a gap longer than the rest of the
            // ring, so the pattern never repeats. Rotated so it starts at 12
            // o'clock, which is where an operator reads a dial from.
            strokeDasharray={`${CIRCUMFERENCE * fraction} ${CIRCUMFERENCE}`}
            transform="rotate(-90 48 48)"
          />
        )}
        <text className="admin-pod-ring__count" x="48" y="48" textAnchor="middle">
          {known ? ready : '—'}
        </text>
        <text className="admin-pod-ring__label" x="48" y="64" textAnchor="middle">
          {known ? `of ${desired}` : 'unread'}
        </text>
      </svg>

      {gate && (
        <ScaleStepper
          kind={kind}
          plural={plural}
          namespace={namespace}
          name={name}
          current={desired ?? null}
          gate={gate}
          onApplied={onApplied}
          testId={stepperTestId}
        />
      )}
    </div>
  );
}

export default PodRing;
