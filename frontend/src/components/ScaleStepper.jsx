/**
 * ScaleStepper — the replica count with a step control either side of it.
 *
 * `kubectl scale` takes an absolute number, and so does §6's scale endpoint. The
 * control an operator reaches for during an incident is `+1`, and typing `4`
 * into a dialog after reading `3/3` off the page is that operation with a
 * transcription step in the middle. This is that arithmetic done for them, for
 * every kind the cluster gives a scale subresource — Deployment, StatefulSet,
 * ReplicaSet — and disabled-with-the-reason for the three it does not.
 *
 * **The arrows do not write.** They open `ScaleDialog` seeded at the number they
 * name, and that dialog is the ordinary §6 handshake: preflight, dry run, diff,
 * confirm. Rule 3 has no exception for a small change — "there is no button that
 * writes to a cluster without having shown a diff first" — and an arrow that
 * posted on click would be exactly that button, on the screen where the count
 * is a single digit and the workload is production.
 *
 * **An unreadable count is not a floor to step from.** §6 makes
 * `replicas.desired` nullable, and `null + 1` is a guess presented as an
 * increment. Both arrows are disabled with that sentence, which is rule 11.2
 * applied to an action rather than to a cell — the same judgement `ScaleDialog`
 * makes when it refuses to seed its field.
 */
import { useState } from 'react';
import AngleDownIcon from '@patternfly/react-icons/dist/esm/icons/angle-down-icon';
import AngleUpIcon from '@patternfly/react-icons/dist/esm/icons/angle-up-icon';
import ScaleDialog, { MAX_REPLICAS } from './ScaleDialog';
import { ActionButton } from './ui';

const UNKNOWN =
  'The controller has not reported a replica count for this workload, so there is no number to step ' +
  'from. Open Scale… and enter the count you want.';

/**
 * Narrow a gate for one arrow.
 *
 * Permission first, and the kind's own capability before that — both come in on
 * `gate`, already combined by `capabilityGate`. "A DaemonSet cannot be scaled"
 * must not be overwritten by "already at zero", which would send an operator
 * looking for the replica they think they lost.
 */
function step(gate, reason) {
  if (gate && !gate.allowed) return gate;
  if (reason) return { allowed: false, reason };
  return gate ?? { allowed: true };
}

export function ScaleStepper({
  /** `Deployment` | `StatefulSet` | … — from the §6 WorkloadRow. */
  kind,
  plural,
  namespace,
  name,
  /** `replicas.desired`. `null` means it could not be read — never zero. */
  current,
  /** `capabilityGate(kind, 'scale', spec, gate(...))`. */
  gate,
  onApplied,
  testId = 'scale-stepper',
}) {
  // The count the dialog opens at. `null` means no dialog: 0 is a real target
  // and must not be falsy-tested away.
  const [target, setTarget] = useState(null);

  const unknown = current == null;
  const up = step(
    gate,
    unknown ? UNKNOWN : current >= MAX_REPLICAS ? `This console will not set a count above ${MAX_REPLICAS}.` : null,
  );
  const down = step(
    gate,
    unknown ? UNKNOWN : current === 0 ? 'This workload is already scaled to zero.' : null,
  );

  return (
    <span className="admin-scale-stepper" data-testid={testId}>
      <span className="admin-scale-stepper__buttons">
        <ActionButton
          gate={up}
          variant="plain"
          size="sm"
          icon={<AngleUpIcon />}
          ariaLabel={unknown ? `Scale ${name} up` : `Scale ${name} up to ${current + 1}`}
          onClick={() => setTarget(current + 1)}
        />
        <ActionButton
          gate={down}
          variant="plain"
          size="sm"
          icon={<AngleDownIcon />}
          ariaLabel={unknown ? `Scale ${name} down` : `Scale ${name} down to ${current - 1}`}
          onClick={() => setTarget(current - 1)}
        />
      </span>

      {target != null && (
        <ScaleDialog
          isOpen
          kind={kind}
          plural={plural}
          namespace={namespace}
          name={name}
          current={current}
          initial={target}
          onClose={() => setTarget(null)}
          onApplied={() => {
            setTarget(null);
            onApplied?.();
          }}
        />
      )}
    </span>
  );
}

export default ScaleStepper;
