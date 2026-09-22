/**
 * TolerationsDialog — the pod template's `tolerations`, as a form.
 *
 * Same write as every other object edit (§4's update, through `objectEdit.js`),
 * and the same funnel. What is different is what the object being edited *is*:
 * this one is the pod template, and three sentences follow from that which the
 * form has to say rather than leave to be discovered.
 *
 * **Saving replaces every pod.** A change under `spec.template` changes the pod
 * template hash, and the controller rolls the workload. The count is unchanged,
 * the diff is two lines, and every running pod is replaced — which on a
 * StatefulSet is one ordered restart per replica and on a Deployment is a
 * surge. An operator adding a toleration "just so it can schedule later" needs
 * to know they are restarting the thing now.
 *
 * **A toleration is permission, not placement.** It lets the scheduler put a
 * pod on a node carrying the matching taint; it does not move anything, and it
 * does not make that node preferred. Nothing here can promise the next pod
 * lands anywhere in particular — §29 is where "why is this Pending" is
 * answered, and it reads the scheduler's own verdict rather than guessing from
 * a manifest.
 *
 * **An empty key with `Exists` tolerates every taint there is**, including the
 * control-plane's and every `NoExecute` an operator adds later to evacuate a
 * node. That is a legitimate thing to write and a catastrophic thing to write
 * by accident, so it is called out in the form rather than in a doc.
 */
import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  TextInput,
} from '@patternfly/react-core';
import PlusCircleIcon from '@patternfly/react-icons/dist/esm/icons/plus-circle-icon';
import MutationDialog from './MutationDialog';
import { podSpecOf, putObject, readBlocked, useEditableObject, withPodSpec } from './objectEdit';

const OPERATORS = ['Equal', 'Exists'];
// '' is "every effect", which is what the API means by an absent one. It is an
// option rather than a blank, because a select whose first entry is empty reads
// as unset rather than as a choice.
const EFFECTS = ['', 'NoSchedule', 'PreferNoSchedule', 'NoExecute'];

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** Rows to the list the API takes, with the fields it omits left out. */
function wire(rows) {
  return rows.map((row) => {
    const out = { operator: row.operator };
    if (row.key.trim()) out.key = row.key.trim();
    // `value` is meaningless under `Exists` and the API server rejects it
    // there, so the form drops it rather than sending a field it has disabled.
    if (row.operator === 'Equal' && row.value !== '') out.value = row.value;
    if (row.effect) out.effect = row.effect;
    if (row.effect === 'NoExecute' && row.seconds.trim() !== '') {
      out.tolerationSeconds = Number(row.seconds);
    }
    return out;
  });
}

function describe(row) {
  const key = row.key.trim() || 'every taint';
  if (row.operator === 'Exists') return `${key}${row.effect ? ` (${row.effect})` : ''}`;
  return `${key}=${row.value}${row.effect ? ` (${row.effect})` : ''}`;
}

export function TolerationsDialog({ target, onClose, onApplied }) {
  const { object, resourceVersion, loading, error } = useEditableObject({ ...target, isOpen: true });
  const podSpec = podSpecOf(object, target.kind);

  const [rows, setRows] = useState(null);
  const nextId = useRef(0);
  const make = (toleration = {}) => ({
    id: (nextId.current += 1),
    key: toleration.key ?? '',
    operator: toleration.operator ?? 'Equal',
    value: toleration.value ?? '',
    effect: toleration.effect ?? '',
    seconds: toleration.tolerationSeconds == null ? '' : String(toleration.tolerationSeconds),
  });

  useEffect(() => {
    if (!podSpec || rows !== null) return;
    setRows((podSpec.tolerations ?? []).map((toleration) => make(toleration)));
    // Seeded once. `rows` is the guard rather than a dependency: re-running on
    // every keystroke would undo the edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [podSpec]);

  const edited = rows ?? [];
  const tolerateEverything = edited.some((row) => !row.key.trim() && row.operator === 'Exists');

  const badSeconds = edited.some(
    (row) =>
      row.effect === 'NoExecute' &&
      row.seconds.trim() !== '' &&
      !/^\d+$/.test(row.seconds.trim()),
  );

  const blocked =
    readBlocked({ loading, error, object, resourceVersion }) ||
    (object && !podSpec
      ? `A ${target.kind} has no pod template on this object, so there are no tolerations to edit here.`
      : edited.some((row) => !row.key.trim() && row.operator === 'Equal')
        ? 'A toleration with no key has to use the Exists operator — an empty key matched by value ' +
          'matches nothing, and the API server refuses it.'
        : badSeconds
          ? 'Toleration seconds must be a whole number of seconds, or empty for "stay indefinitely".'
          : null);

  const request = (dryRun, context) => {
    const next = withPodSpec(object, target.kind, (spec) => {
      const list = wire(edited);
      if (list.length) spec.tolerations = list;
      else delete spec.tolerations;
    });
    return putObject(target, next, {
      resourceVersion: context?.resourceVersion ?? resourceVersion,
      dryRun,
    });
  };

  return (
    <MutationDialog
      isOpen
      title={`Tolerations on ${target.name}`}
      description={
        'Tolerations let the scheduler place this workload’s pods on nodes carrying a matching taint. ' +
        'They are part of the pod template, so saving replaces every running pod.'
      }
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!blocked}
      previewDisabledReason={blocked}
      confirmLabel="Set the tolerations"
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `The pod template on ${target.name} now carries the tolerations you sent`,
              body:
                'The controller replaces the existing pods to apply the new template — watch the ready ' +
                'count for the actual state. A toleration is permission to be scheduled onto a tainted ' +
                'node, not a decision to go there.',
            }
          : {
              variant: 'warning',
              title: 'The write completed without confirming it was applied',
              body: 'Re-read the workload before assuming its pod template changed.',
            }
      }
    >
      <Form onSubmit={(e) => e.preventDefault()} data-testid="tolerations-form">
        <Alert
          isInline
          variant="warning"
          title="Saving this restarts the workload"
          data-testid="tolerations-rollout"
        >
          Tolerations live in the pod template, so changing them changes the template hash and the
          controller rolls every pod. Nothing moves to a different node because of this change on its
          own: existing pods are replaced where they can be scheduled next.
        </Alert>

        {tolerateEverything && (
          <Alert
            isInline
            variant="warning"
            title="One row tolerates every taint on the cluster"
            data-testid="tolerations-catch-all"
          >
            A row with no key and the <code>Exists</code> operator matches every taint there is —
            including the control plane’s, and including a <code>NoExecute</code> somebody adds later to
            drain a node, which this workload’s pods would then ignore.
          </Alert>
        )}

        {rows === null ? (
          <p style={MUTED}>Reading the pod template…</p>
        ) : (
          <>
            {edited.map((row, index) => (
              <Grid hasGutter key={row.id} data-testid={`toleration-row-${index}`}>
                <GridItem span={3}>
                  <TextInput
                    aria-label={`Toleration ${index + 1} key`}
                    data-testid={`toleration-key-${index}`}
                    value={row.key}
                    onChange={(_e, value) =>
                      setRows((all) => all.map((r) => (r.id === row.id ? { ...r, key: value } : r)))
                    }
                    placeholder="taint key"
                  />
                </GridItem>
                <GridItem span={2}>
                  <FormSelect
                    aria-label={`Toleration ${index + 1} operator`}
                    data-testid={`toleration-operator-${index}`}
                    value={row.operator}
                    onChange={(_e, value) =>
                      setRows((all) => all.map((r) => (r.id === row.id ? { ...r, operator: value } : r)))
                    }
                  >
                    {OPERATORS.map((operator) => (
                      <FormSelectOption key={operator} value={operator} label={operator} />
                    ))}
                  </FormSelect>
                </GridItem>
                <GridItem span={2}>
                  <TextInput
                    aria-label={`Toleration ${index + 1} value`}
                    data-testid={`toleration-value-${index}`}
                    value={row.value}
                    // `Exists` means "whatever the value is", so the field is
                    // disabled rather than ignored: a value left in a box that
                    // is not sent is a change the operator believes they made.
                    isDisabled={row.operator === 'Exists'}
                    onChange={(_e, value) =>
                      setRows((all) => all.map((r) => (r.id === row.id ? { ...r, value } : r)))
                    }
                    placeholder={row.operator === 'Exists' ? 'any value' : 'value'}
                  />
                </GridItem>
                <GridItem span={2}>
                  <FormSelect
                    aria-label={`Toleration ${index + 1} effect`}
                    data-testid={`toleration-effect-${index}`}
                    value={row.effect}
                    onChange={(_e, value) =>
                      setRows((all) => all.map((r) => (r.id === row.id ? { ...r, effect: value } : r)))
                    }
                  >
                    {EFFECTS.map((effect) => (
                      <FormSelectOption key={effect || 'any'} value={effect} label={effect || 'Every effect'} />
                    ))}
                  </FormSelect>
                </GridItem>
                <GridItem span={2}>
                  <TextInput
                    aria-label={`Toleration ${index + 1} seconds`}
                    data-testid={`toleration-seconds-${index}`}
                    value={row.seconds}
                    // Only `NoExecute` evicts, so only `NoExecute` has a
                    // deadline to count down.
                    isDisabled={row.effect !== 'NoExecute'}
                    onChange={(_e, value) =>
                      setRows((all) => all.map((r) => (r.id === row.id ? { ...r, seconds: value } : r)))
                    }
                    placeholder={row.effect === 'NoExecute' ? 'seconds (blank = forever)' : 'NoExecute only'}
                  />
                </GridItem>
                <GridItem span={1}>
                  <Button
                    variant="link"
                    isDanger
                    data-testid={`toleration-remove-${index}`}
                    onClick={() => setRows((all) => all.filter((r) => r.id !== row.id))}
                  >
                    Remove
                  </Button>
                </GridItem>
                <GridItem span={12}>
                  <span style={MUTED}>{describe(row)}</span>
                </GridItem>
              </Grid>
            ))}

            {edited.length === 0 && (
              <p style={MUTED}>
                This pod template carries no tolerations, so its pods will not be scheduled onto any
                tainted node.
              </p>
            )}

            <Button
              variant="link"
              icon={<PlusCircleIcon />}
              data-testid="toleration-add"
              onClick={() => setRows((all) => [...all, make()])}
            >
              Add more
            </Button>
          </>
        )}
      </Form>
    </MutationDialog>
  );
}

export default TolerationsDialog;
