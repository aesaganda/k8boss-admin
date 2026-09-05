/**
 * ScaleDialog — `POST /api/workloads/{plural}/{ns}/{name}/scale` (§6).
 *
 * A thin wrapper over `MutationDialog`: it owns a number field and a `request`
 * closure, and inherits the dry-run-then-confirm handshake, the diff, the 409
 * re-projection, the read-only gate and the audit link from the spine.
 *
 * Two judgements live here rather than in the spine.
 *
 * **A kind with no scale subresource is refused before the round trip.** The
 * backend answers `422 invalid` naming the kind (`app/api/workloads.py`
 * `_NO_SCALE`), and it is right to — it is the authority. But rule 11.4 says an
 * unusable action is *disabled with the reason*, not hidden and not offered and
 * then rejected, so the same sentences are mirrored here and the operator reads
 * them without spending a request and an audit row to find out. The wording is
 * kept identical to the backend's on purpose: two sentences that disagree about
 * why a DaemonSet cannot be scaled are worse than one that is only in one place.
 *
 * **An unknown current replica count does not become a starting value.** §6
 * makes `replicas.desired` nullable when the controller status was not
 * observed. Seeding the field with `0` in that case puts a working workload one
 * unread click away from being scaled to nothing, and seeding it with `1` is a
 * guess presented as the status quo. The field starts empty and Preview is
 * disabled with the reason — which is contract rule 11.2 applied to an input
 * rather than to a cell.
 *
 * **And an autoscaled workload says so, on the preview and after the write.**
 * §21's `governedBy` names the HorizontalPodAutoscaler that will put the count
 * back — usually within seconds. The scale itself succeeds and `applied: true`
 * is true; what is misleading is the belief that the number *stays*. It is
 * tri-state, because "we could not read the autoscalers" must never render as
 * "nothing will undo this".
 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Form, FormGroup, NumberInput } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { workloads as workloadsApi } from '../api/client';

/**
 * Which autoscaler will override the count being set (§21).
 *
 * Three renderings for three answers. `governed: null` is the one that must not
 * be collapsed into `false`: the autoscaler listing did not answer, and
 * "nothing will undo this" is exactly the sentence an operator would act on.
 */
function GovernedBy({ result, phase }) {
  const governed = result?.governedBy;
  if (!governed) return null;
  const executed = phase === 'done';

  if (governed.governed === true) {
    const hpa = governed.autoscaler;
    const inert = hpa?.scaling_active === false;
    return (
      <Alert
        isInline
        variant={inert ? 'info' : 'warning'}
        className="admin-confirm__alert"
        data-testid="scale-governed"
        data-governed="true"
        title={
          executed
            ? `${hpa?.name} decides this workload's replica count`
            : `${hpa?.name} will override the count you are setting`
        }
      >
        {governed.detail}
        {inert && (
          <p style={{ marginBlockStart: '0.5rem' }}>
            That autoscaler is <strong>not scaling right now</strong> — its ScalingActive condition
            is false — so this count will hold until the controller can read its metrics again. That
            is a reprieve, not a fix.
          </p>
        )}
      </Alert>
    );
  }

  if (governed.governed === null) {
    return (
      <Alert
        isInline
        variant="warning"
        className="admin-confirm__alert"
        data-testid="scale-governed"
        data-governed="unknown"
        title="Whether an autoscaler will undo this is unknown"
      >
        {governed.detail}
      </Alert>
    );
  }

  // `governed: false` is good news and gets no banner — a console that alerted
  // on the ordinary case would train people past the two above.
  return null;
}

// Mirrors `_NO_SCALE` in backend/app/api/workloads.py. Kept verbatim: the
// backend is the authority and this is a pre-emptive copy of its answer.
const NO_SCALE = {
  DaemonSet:
    'A DaemonSet has no scale subresource: it runs one pod per matching node, so its size comes from node ' +
    'selection. Change its nodeSelector, affinity or tolerations to change where it runs.',
  Job:
    'A Job has no scale subresource. Its size is spec.completions and spec.parallelism, which are set when ' +
    'it is created.',
  CronJob:
    'A CronJob has no replicas of its own — it creates Jobs on a schedule. Suspend it to stop it creating ' +
    'them.',
};

const MAX_REPLICAS = 10000;

export function ScaleDialog({
  isOpen,
  /** `Deployment` | `StatefulSet` | … — from the §6 WorkloadRow. */
  kind,
  plural,
  namespace,
  name,
  /** `replicas.desired` from the §6 row. `null` means it could not be read. */
  current,
  onClose,
  onApplied,
}) {
  // `null` until the operator commits to a number, so an unreadable current
  // count cannot silently become the request.
  const [replicas, setReplicas] = useState(current ?? null);

  useEffect(() => {
    if (isOpen) setReplicas(current ?? null);
  }, [isOpen, current]);

  const unsupported = kind ? NO_SCALE[kind] : null;
  const unknownCurrent = current == null;
  const valid = replicas != null && Number.isInteger(replicas) && replicas >= 0 && replicas <= MAX_REPLICAS;

  const clamp = (next) => {
    if (next == null || Number.isNaN(next)) return null;
    return Math.max(0, Math.min(MAX_REPLICAS, Math.trunc(next)));
  };

  // Rendered after the write as well as on the preview: an operator who
  // confirmed without reading it still needs to know the count will move back.
  const renderExtra = useCallback(
    ({ result, phase }) => <GovernedBy result={result} phase={phase} />,
    [],
  );

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Scale ${name}`}
      description={
        unsupported
          ? undefined
          : `${kind || 'Workload'} ${namespace}/${name}. Scaling sets an absolute replica count, not a delta.`
      }
      isDanger={replicas === 0}
      confirmLabel={replicas === 0 ? 'Scale to zero' : 'Apply'}
      canPreview={!unsupported && valid}
      previewDisabledReason={
        unsupported ||
        (replicas == null
          ? unknownCurrent
            ? 'Enter a replica count. The current count could not be read, so there is no safe value to start from.'
            : 'Enter a replica count.'
          : `Replicas must be a whole number between 0 and ${MAX_REPLICAS}.`)
      }
      request={(dryRun) => workloadsApi.scale(plural, namespace, name, { replicas, dryRun })}
      renderExtra={renderExtra}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${name} is now set to ${replicas} ${replicas === 1 ? 'replica' : 'replicas'}`,
              body:
                result?.governedBy?.governed === true
                  ? 'The API server accepted the new desired count — and an autoscaler owns this ' +
                    'workload, so its next scale decision replaces that count. See the panel below.'
                  : 'The API server accepted the new desired count. Pods reach it asynchronously — watch ' +
                    'the ready count on the workload for the actual state.',
            }
          : {
              variant: 'warning',
              title: 'The scale request completed without confirming it was applied',
              body: 'Re-read the workload before assuming the replica count changed.',
            }
      }
    >
      <Form>
        {unsupported && (
          <Alert isInline variant="warning" title={`A ${kind} cannot be scaled`}>
            {unsupported}
          </Alert>
        )}

        {!unsupported && (
          <>
            {unknownCurrent && (
              // §11.2, as an input rather than a cell: the dash in the table
              // means "not read", and the field must not quietly turn that into
              // a number.
              <Alert isInline variant="warning" title="The current replica count is unknown">
                The workload listing could not report `replicas.desired` for this object, so the field below
                starts empty rather than at a guessed value. The number you enter is the absolute desired
                count.
              </Alert>
            )}

            <FormGroup
              label="Desired replicas"
              fieldId="scale-replicas"
              // Not `isRequired` alone: the sentence is what makes an empty
              // field actionable.
              labelHelp={undefined}
            >
              <NumberInput
                id="scale-replicas"
                value={replicas ?? ''}
                min={0}
                max={MAX_REPLICAS}
                onMinus={() => setReplicas((v) => clamp((v ?? 0) - 1))}
                onPlus={() => setReplicas((v) => clamp((v ?? 0) + 1))}
                onChange={(event) => {
                  const raw = event.target.value;
                  setReplicas(raw === '' ? null : clamp(Number(raw)));
                }}
                inputAriaLabel="Desired replicas"
                minusBtnAriaLabel="One fewer replica"
                plusBtnAriaLabel="One more replica"
                data-testid="scale-replicas"
              />
              {!unknownCurrent && (
                <p style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem', marginBlockStart: '0.25rem' }}>
                  Currently {current} {current === 1 ? 'replica' : 'replicas'}.
                </p>
              )}
            </FormGroup>

            {replicas === 0 && (
              <Alert isInline variant="warning" title="This stops the workload entirely">
                Zero replicas terminates every pod. The object stays in the cluster and can be scaled back
                up, but anything it was serving stops being served now.
              </Alert>
            )}
          </>
        )}
      </Form>
    </MutationDialog>
  );
}

export default ScaleDialog;
