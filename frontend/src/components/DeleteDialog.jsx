/**
 * DeleteDialog — `DELETE /api/resources/{group}/{version}/{plural}/{name}` (§4).
 *
 * Generic over every resource the cluster serves, because §4 is: the typed
 * endpoints shape rows, but deletion is one verb against one object and giving
 * each kind its own delete dialog would give each kind its own chance to skip
 * the diff.
 *
 * **The dry run of a delete is the most useful diff in the console.** §4
 * specifies `diff.before` = the live object, `diff.after` = `null`,
 * `changed: true` — so the preview is a full rendering of exactly what
 * disappears. That is worth more here than anywhere else: an operator deleting
 * a ConfigMap they believe is unused finds out from this diff that it has
 * fourteen keys and an ownerReference.
 *
 * **Propagation policy is a choice, and a consequential one.** `Background`
 * (the default, and kubectl's) deletes the object now and its dependents
 * asynchronously. `Foreground` blocks until the dependents are gone.
 * `Orphan` leaves them running with no owner — which is occasionally what
 * somebody wants and much more often is how a Deployment's ReplicaSet and its
 * pods end up alive in a cluster with nothing managing them. Each option says
 * what it does; the selector does not just list three words.
 *
 * **Typed confirmation for the ones that take a lot with them.** A namespace, a
 * node, a PersistentVolume or a CRD deletes far more than the row the operator
 * clicked. Those ask for the name to be typed. Deliberately slower.
 */
import { useEffect, useState } from 'react';
import { Alert, Form, FormGroup, FormSelect, FormSelectOption } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { resources as resourcesApi } from '../api/client';

const POLICIES = [
  {
    value: 'Background',
    label: 'Background — delete this object now, its dependents afterwards',
    note: 'The default, and what kubectl does. The object disappears immediately; the garbage collector removes what it owned.',
  },
  {
    value: 'Foreground',
    label: 'Foreground — delete dependents first, then this object',
    note: 'The object stays visible with a deletion timestamp until everything it owns is gone. Slower, and the only option that lets you watch the cascade finish.',
  },
  {
    value: 'Orphan',
    label: 'Orphan — leave dependents running',
    note: 'Whatever this object owned keeps running with no controller managing it. A Deployment deleted this way leaves its ReplicaSet and pods alive and unmanaged.',
  },
];

/**
 * Kinds whose deletion reaches well beyond the object named. Typing the name is
 * a deliberate speed bump, not a permission check — the permission check
 * already happened in the preflight.
 */
const TYPE_TO_CONFIRM_KINDS = new Set([
  'Namespace',
  'Node',
  'PersistentVolume',
  'PersistentVolumeClaim',
  'CustomResourceDefinition',
  'StorageClass',
  'ClusterRole',
  'ClusterRoleBinding',
]);

export function DeleteDialog({
  isOpen,
  group,
  version,
  plural,
  name,
  namespace,
  kind,
  /** Override the typed-confirmation decision; pass `false` to switch it off. */
  requireTyped,
  onClose,
  onApplied,
}) {
  const [policy, setPolicy] = useState('Background');

  useEffect(() => {
    if (isOpen) setPolicy('Background');
  }, [isOpen]);

  const clusterScoped = !namespace;
  const typed =
    requireTyped === false
      ? undefined
      : requireTyped ||
        // Cluster-scoped objects get the speed bump too: there is no namespace
        // boundary limiting what a mistake here reaches.
        (kind && TYPE_TO_CONFIRM_KINDS.has(kind) ? name : clusterScoped ? name : undefined);

  const selected = POLICIES.find((p) => p.value === policy);
  const label = `${kind || plural} ${namespace ? `${namespace}/${name}` : name}`;

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Delete ${name}`}
      description={
        `${label} will be removed from the cluster. The preview below is the live object in full — ` +
        'that is exactly what disappears.'
      }
      isDanger
      confirmLabel="Delete"
      requireTyped={typed}
      request={(dryRun) =>
        // §4: dryRun rides in the query string for DELETE, which is also what
        // the client's replay-safety check reads to decide this call may be
        // retried. A real delete is never replayed.
        resourcesApi.remove(group, version, plural, name, {
          namespace,
          propagationPolicy: policy,
          dryRun,
        })
      }
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${name} was deleted`,
              body:
                policy === 'Foreground'
                  ? 'The object is marked for deletion and will remain visible until its dependents are gone.'
                  : policy === 'Orphan'
                    ? 'Anything this object owned is still running and is no longer managed by it.'
                    : 'Dependents are being removed in the background by the garbage collector.',
            }
          : {
              variant: 'warning',
              title: 'The delete request completed without confirming it was applied',
              body: 'Re-read the object before assuming it is gone.',
            }
      }
    >
      <Form>
        <FormGroup label="Dependent objects" fieldId="delete-propagation">
          <FormSelect
            id="delete-propagation"
            value={policy}
            onChange={(_event, next) => setPolicy(next)}
            aria-label="Propagation policy"
            data-testid="delete-propagation"
          >
            {POLICIES.map((option) => (
              <FormSelectOption key={option.value} value={option.value} label={option.label} />
            ))}
          </FormSelect>
          <p style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem', marginBlockStart: '0.25rem' }}>
            {selected?.note}
          </p>
        </FormGroup>

        {policy === 'Orphan' && (
          <Alert isInline variant="warning" title="Orphaned objects keep running">
            Nothing will be managing them and nothing will clean them up. They stay until somebody deletes
            them by hand.
          </Alert>
        )}

        {clusterScoped && (
          <Alert isInline variant="warning" title="This object is cluster-scoped">
            It is not confined to a namespace, so the effect of removing it is not confined to one either.
          </Alert>
        )}
      </Form>
    </MutationDialog>
  );
}

export default DeleteDialog;
