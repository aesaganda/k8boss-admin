/**
 * MetadataDialog — one object's `metadata.labels`, `metadata.annotations` or
 * its pod template's `nodeSelector`, as a form.
 *
 * Three fields, one dialog, because they are one control over one shape: a map
 * of strings with a key that must not be blank and must not repeat. What
 * differs is where the map lives and what saving it costs, and that is what
 * `FIELDS` carries — including the `rollout` flag, which is the difference
 * between an edit that changes a name and an edit that replaces every running
 * pod.
 *
 * It writes through §4's update, which is the same call the YAML editor makes
 * (`objectEdit.js` says why there is no endpoint of its own). What this adds is
 * that the operator does not have to find the right two lines in a 200-line
 * manifest to add one label to it.
 *
 * **These are the object's own, not its pod template's, and the difference is a
 * rollout.** A Deployment's `metadata.labels` are how other objects find *it*;
 * `spec.template.metadata.labels` are what its pods carry, and changing those
 * replaces every pod. The two are one indentation level apart in the YAML and
 * this form only ever touches the first — said in the dialog, because an
 * operator who believes they have just relabelled the pods will go looking for
 * a rollout that is not coming.
 *
 * **Key syntax is the API server's ruling, not this form's.** A label key is
 * an optional DNS-subdomain prefix, a slash and a 63-character name; an
 * annotation key is the same prefix rule with no length limit on the value.
 * Re-implementing that here would produce a second opinion, and the first time
 * the two disagreed the form would be refusing something the cluster accepts.
 * What this blocks is the two things a *form* can be wrong about on its own: a
 * row with no key, and two rows with the same key — where only one would be
 * written and which one is not a question to answer by accident.
 */
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Form, Grid, GridItem, TextInput } from '@patternfly/react-core';
import PlusCircleIcon from '@patternfly/react-icons/dist/esm/icons/plus-circle-icon';
import MutationDialog from './MutationDialog';
import { podSpecOf, putObject, readBlocked, useEditableObject, withPodSpec } from './objectEdit';

const LAST_APPLIED = 'kubectl.kubernetes.io/last-applied-configuration';

/**
 * Why a Secret cannot be edited this way, stated before the round trip.
 *
 * §4's read redacts a Secret — every `data` value comes back `null` and
 * kubectl's `last-applied-configuration` is dropped, because that annotation
 * holds a verbatim copy of the values — and this console never asks for the
 * revealed form. So the object in hand is not the object on the cluster, and
 * sending it back would either be refused by the API server (null is not
 * base64) or, for a Secret with no data at all, quietly drop that annotation.
 *
 * Rule 11.4 says the action is offered and disabled with the reason rather than
 * hidden, so the entry stays in the menu and this is what it says.
 */
const SECRET_REFUSAL =
  'A Secret’s values are withheld from this console’s read, so the whole object cannot be sent back ' +
  'with a label attached — the copy in this dialog has null where every value should be. Use ' +
  '`kubectl label` (or `kubectl annotate`), which patches the metadata without rewriting the data.';

const FIELDS = {
  labels: {
    noun: 'Labels',
    lower: 'labels',
    singular: 'label',
    description:
      'These are the object’s own labels — what other objects use to find it. They are not the pod ' +
      'template’s labels, so changing them restarts nothing and starts no rollout.',
    confirm: 'Set the labels',
    applied: 'No pod was restarted: these are the object’s own labels, not its pod template’s.',
    empty: 'This object carries no labels.',
    read: (object) => object.metadata?.labels,
    write: (object, kind, map) => {
      const next = structuredClone(object);
      next.metadata = { ...(next.metadata ?? {}), labels: map };
      return next;
    },
  },
  annotations: {
    noun: 'Annotations',
    lower: 'annotations',
    singular: 'annotation',
    description:
      'Annotations hold data for tools rather than selectors for the scheduler. Some here are written ' +
      'by the object’s own controller — `deployment.kubernetes.io/revision` is one — and it will write ' +
      'them again whatever this form sends.',
    confirm: 'Set the annotations',
    applied: 'Any annotation a controller owns will be rewritten by that controller on its next pass.',
    empty: 'This object carries no annotations.',
    read: (object) => object.metadata?.annotations,
    write: (object, kind, map) => {
      const next = structuredClone(object);
      next.metadata = { ...(next.metadata ?? {}), annotations: map };
      return next;
    },
  },
  // The one entry that is not metadata, and it is here rather than in a dialog
  // of its own because it is the same control over the same shape: a map of
  // strings. What it is not is the same *consequence* — this one lives in the
  // pod template, so saving it rolls the workload, and an empty map is the
  // difference between "any node" and "no node at all".
  nodeSelector: {
    noun: 'Node selector',
    lower: 'node selector',
    singular: 'node selector entry',
    description:
      'Every key here must be present on a node, with this value, before the scheduler will place a ' +
      'pod there. It is part of the pod template, so saving replaces every running pod.',
    confirm: 'Set the node selector',
    applied:
      'The controller replaces the existing pods to apply the new template. A pod that matches no node ' +
      'stays Pending — the scheduler does not relax a nodeSelector, and nothing here can make a node match.',
    empty: 'This pod template carries no node selector, so its pods can be scheduled to any node.',
    rollout: true,
    read: (object, kind) => podSpecOf(object, kind)?.nodeSelector,
    write: (object, kind, map) =>
      withPodSpec(object, kind, (spec) => {
        // Deleted rather than left as `{}`: an empty map and an absent one mean
        // the same thing to the scheduler, and the diff an operator reads
        // should say the constraint is gone.
        if (Object.keys(map).length) spec.nodeSelector = map;
        else delete spec.nodeSelector;
      }),
  },
};

/** Rows to the map the API takes. A blank key is a row still being typed. */
function wire(rows) {
  const out = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (key) out[key] = row.value;
  }
  return out;
}

export function MetadataDialog({ target, field, onClose, onApplied }) {
  const copy = FIELDS[field];
  const { object, resourceVersion, loading, error } = useEditableObject({ ...target, isOpen: true });

  // `null` until the object arrives: an empty array is "this object has no
  // labels", which is a claim, and one the form would offer to save.
  const [rows, setRows] = useState(null);
  const nextId = useRef(0);
  const make = (key, value) => ({ id: (nextId.current += 1), key, value });

  useEffect(() => {
    if (!object || rows !== null) return;
    setRows(
      Object.entries(copy.read(object, target.kind) ?? {})
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, value]) => make(key, String(value))),
    );
    // `make` is a closure over a ref, and `rows` is the guard rather than a
    // dependency — re-running this on every keystroke would undo the edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [object, field]);

  const edited = rows ?? [];
  const seen = new Set();
  const duplicate = edited.some((row) => {
    const key = row.key.trim();
    if (!key) return false;
    if (seen.has(key)) return true;
    seen.add(key);
    return false;
  });

  const blocked =
    readBlocked({ loading, error, object, resourceVersion }) ||
    (object?.kind === 'Secret' ? SECRET_REFUSAL : null) ||
    (copy.rollout && object && !podSpecOf(object, target.kind)
      ? `A ${target.kind} has no pod template on this object, so there is no node selector to set here.`
      : edited.some((row) => !row.key.trim())
      ? `Every ${copy.singular} needs a key. Remove the blank row or fill it in.`
        : duplicate
          ? 'Two rows carry the same key. Only one of them would be written, and which is not something to leave to the form.'
          : null);

  const request = (dryRun, context) => {
    const next = copy.write(object, target.kind, wire(edited));
    return putObject(target, next, {
      resourceVersion: context?.resourceVersion ?? resourceVersion,
      dryRun,
    });
  };

  return (
    <MutationDialog
      isOpen
      title={`${copy.noun} on ${target.name}`}
      description={copy.description}
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!blocked}
      previewDisabledReason={blocked}
      confirmLabel={copy.confirm}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `The ${copy.lower} on ${target.name} are now what you sent`,
              body: copy.applied,
            }
          : {
              variant: 'warning',
              title: 'The write completed without confirming it was applied',
              body: `Re-read the object before assuming its ${copy.lower} changed.`,
            }
      }
    >
      <Form onSubmit={(e) => e.preventDefault()} data-testid="metadata-form">
        {copy.rollout && (
          <Alert
            isInline
            variant="warning"
            title="Saving this restarts the workload"
            data-testid="metadata-rollout"
          >
            This field is part of the pod template, so changing it changes the template hash and the
            controller replaces every pod. A pod whose selector matches no node stays Pending rather
            than being placed somewhere close enough.
          </Alert>
        )}

        {object?.kind === 'Secret' && (
          <Alert isInline variant="warning" title="A Secret cannot be edited here" data-testid="metadata-secret">
            {SECRET_REFUSAL}
          </Alert>
        )}

        {field === 'annotations' && object?.metadata?.annotations?.[LAST_APPLIED] != null && (
          <Alert
            isInline
            variant="info"
            title="One of these is kubectl’s own bookkeeping"
            data-testid="metadata-last-applied"
          >
            <code>{LAST_APPLIED}</code> is how <code>kubectl apply</code> works out what a later apply
            means to change. Editing it by hand changes what a future apply will do; removing it makes
            the next apply treat every field as new.
          </Alert>
        )}

        {rows === null ? (
          <p style={{ color: 'var(--admin-muted, #6a6e73)' }}>Reading the object…</p>
        ) : (
          <>
            {edited.map((row, index) => (
              <Grid hasGutter key={row.id} data-testid={`metadata-row-${index}`}>
                <GridItem span={5}>
                  <TextInput
                    aria-label={`Key ${index + 1}`}
                    data-testid={`metadata-key-${index}`}
                    value={row.key}
                    onChange={(_e, value) =>
                      setRows((current) => current.map((r) => (r.id === row.id ? { ...r, key: value } : r)))
                    }
                    placeholder="key"
                  />
                </GridItem>
                <GridItem span={5}>
                  <TextInput
                    aria-label={`Value ${index + 1}`}
                    data-testid={`metadata-value-${index}`}
                    value={row.value}
                    onChange={(_e, value) =>
                      setRows((current) => current.map((r) => (r.id === row.id ? { ...r, value } : r)))
                    }
                    placeholder="value"
                  />
                </GridItem>
                <GridItem span={2}>
                  <Button
                    variant="link"
                    isDanger
                    data-testid={`metadata-remove-${index}`}
                    onClick={() => setRows((current) => current.filter((r) => r.id !== row.id))}
                  >
                    Remove
                  </Button>
                </GridItem>
              </Grid>
            ))}

            {edited.length === 0 && (
              <p style={{ color: 'var(--admin-muted, #6a6e73)' }}>{copy.empty}</p>
            )}

            <Button
              variant="link"
              icon={<PlusCircleIcon />}
              data-testid="metadata-add"
              onClick={() => setRows((current) => [...current, make('', '')])}
            >
              Add more
            </Button>
          </>
        )}
      </Form>
    </MutationDialog>
  );
}

export default MetadataDialog;
