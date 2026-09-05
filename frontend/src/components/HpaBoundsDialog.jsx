/**
 * HpaBoundsDialog — §21's write, wrapped in the §11.3 handshake.
 *
 * Two integers on one object, and §4's YAML editor could already write them.
 * What it could not do is answer the three questions that decide whether the
 * edit does anything, and those are this dialog's reason to exist.
 *
 * **Is this autoscaler scaling at all?** An HPA whose `ScalingActive` condition
 * is false cannot compute a desired replica count — usually the metrics API is
 * gone — and it looks entirely normal in `kubectl get hpa`. Raising its ceiling
 * during an incident stores a number and changes nothing. That is the first
 * thing on screen, and it is a checkbox rather than a footnote.
 *
 * **Does this take effect now?** Lowering `maxReplicas` below the running count
 * is not a cap on future growth: the controller clamps at its next decision,
 * seconds away, and pods terminate. The form field looks identical to the one
 * that raises a ceiling, so the consequence names how many pods move.
 *
 * **What is it actually reading?** Each metric's current value beside its
 * target, and a metric with no reading renders as an em dash — never `0`, which
 * on a CPU target reads as an idle workload and is the number that argues for
 * scaling *down*.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  Grid,
  GridItem,
  NumberInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { DescriptionList, NullableCell, SectionHeader, StatusBadge } from './ui';
import { hpa as hpaApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MAX_REPLICAS = 10000;
const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

function clamp(next) {
  if (next == null || Number.isNaN(next)) return null;
  return Math.max(0, Math.min(MAX_REPLICAS, Math.trunc(next)));
}

/**
 * Whether the autoscaler is scaling, as three renderings rather than two.
 *
 * `null` is a fresh HPA the controller has not observed yet, and drawing it as
 * either "scaling" or "not scaling" is a claim about a workload nobody checked.
 */
function ScalingState({ current }) {
  const active = current?.scaling_active;
  const condition = current?.conditions?.ScalingActive;

  if (active === false) {
    return (
      <Alert
        isInline
        variant="danger"
        className="admin-confirm__alert"
        data-testid="hpa-inert"
        title="This autoscaler is not scaling anything right now"
      >
        Its <code>ScalingActive</code> condition is false
        {condition?.reason ? ` (${condition.reason})` : ''}, so the controller cannot work out a
        desired replica count. {condition?.message ? `${condition.message}. ` : ''}
        New bounds will be stored and nothing will act on them until this is fixed.
      </Alert>
    );
  }
  if (active === null || active === undefined) {
    return (
      <p style={MUTED} data-testid="hpa-scaling-unknown">
        The controller has not written a <code>ScalingActive</code> condition for this autoscaler
        yet. That is what a newly created one looks like — it is not a report that it is broken, and
        it is not a report that it is working.
      </p>
    );
  }
  return <StatusBadge status="Ready" label="Scaling" data-testid="hpa-scaling-active" />;
}

/** Each metric's target beside its current reading, with `null` as an em dash. */
function Metrics({ metrics }) {
  if (!metrics?.length) {
    return (
      <p style={MUTED} data-testid="hpa-no-metrics">
        This autoscaler declares no metrics, so it only holds the workload between its bounds.
      </p>
    );
  }
  return (
    <DescriptionList
      items={metrics.map((metric) => ({
        label: metric.container ? `${metric.name} (${metric.container})` : metric.name,
        value: (
          <span data-testid={`hpa-metric-${metric.name}`}>
            {metric.current == null ? (
              <NullableCell
                value={null}
                reason={
                  'The controller has published no reading for this metric. That is what a missing ' +
                  'metrics API looks like — it is not a reading of zero, which would say this ' +
                  'workload is idle.'
                }
              />
            ) : (
              metric.current
            )}{' '}
            <span style={MUTED}>/ {metric.target ?? 'no target'}</span>
          </span>
        ),
      }))}
    />
  );
}

export default function HpaBoundsDialog({ namespace, name, onClose, onApplied }) {
  const [bounds, setBounds] = useState({ min: null, max: null });
  const [acknowledged, setAcknowledged] = useState([]);
  const [seeded, setSeeded] = useState(false);

  // Seeded from the autoscaler's own bounds so the dialog opens showing what is
  // true today, then left alone: re-seeding on every plan response would undo
  // the operator's typing on the round trip their typing caused.
  const seedBody = useMemo(
    () => ({ minReplicas: bounds.min ?? 1, maxReplicas: bounds.max ?? 1 }),
    [bounds.min, bounds.max],
  );

  const { data: plan, loading: planLoading, error: planError } = useAsync(
    () => hpaApi.plan(namespace, name, seedBody),
    { key: `hpa-plan:${namespace}:${name}:${seedBody.minReplicas}:${seedBody.maxReplicas}` },
  );

  const current = plan?.current;
  useEffect(() => {
    if (seeded || !current) return;
    setSeeded(true);
    setBounds({ min: current.min_replicas ?? null, max: current.max_replicas ?? null });
  }, [seeded, current]);

  const resourceVersion = plan?.resourceVersion ?? null;
  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);

  // Per-list, not a boolean: changing a bound changes what is being consented
  // to, and consent given for one set must not carry onto another.
  const signature = consequences.map((c) => c.code).sort().join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setAcknowledged([]);
  }, [signature]);

  const unacknowledged = consequences.filter((c) => !acknowledged.includes(c.code));

  const request = useCallback(
    (dryRun) =>
      hpaApi.setBounds(namespace, name, {
        minReplicas: bounds.min,
        maxReplicas: bounds.max,
        resourceVersion,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [namespace, name, bounds.min, bounds.max, resourceVersion, acknowledged],
  );

  /**
   * Re-checked against the dry run's own answer, which is the later read: the
   * replica count moves on its own, so "this terminates two pods" can be a
   * different number by the time the write lands.
   */
  const confirmBlockedReason = useCallback(
    (result) => {
      const missing = (result?.consequences ?? []).filter((c) => !acknowledged.includes(c.code));
      if (!missing.length) return null;
      return (
        'The dry run reported consequences that have not been acknowledged: ' +
        `${missing.map((c) => c.label).join('; ')}. Go back and tick each one.`
      );
    },
    [acknowledged],
  );

  const bothSet = bounds.min != null && bounds.max != null;
  let previewBlocked = null;
  if (planError) previewBlocked = planError.message;
  else if (planLoading && !plan) previewBlocked = 'Still reading the autoscaler…';
  else if (!bothSet) previewBlocked = 'Set both bounds. Each is sent on every write.';
  else if (bounds.min > bounds.max) previewBlocked = 'The floor cannot be above the ceiling.';
  else if (plan?.blocked) previewBlocked = plan.blocked.message;
  else if (unacknowledged.length) {
    previewBlocked = `Acknowledge what this change means: ${unacknowledged.map((c) => c.label).join('; ')}.`;
  }

  return (
    <MutationDialog
      isOpen
      title={`Autoscaler bounds for ${name}`}
      description={`${namespace}/${name}. Setting the bounds changes what the autoscaler is allowed to do — it does not scale the workload itself.`}
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the change"
      confirmLabel="Set the bounds"
      autoPreview={false}
      confirmBlockedReason={confirmBlockedReason}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="hpa-form">
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="hpa-disabled"
            title="This console runs read-only"
          >
            {plan.gate.detail}
          </Alert>
        )}

        {planError && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="hpa-plan-error"
            title="This autoscaler could not be read"
          >
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        <SectionHeader
          title="What this autoscaler is doing now"
          description="Read before the bounds, because it decides whether changing them does anything."
        />
        <ScalingState current={current} />

        <DescriptionList
          items={[
            {
              label: 'Running now',
              value: (
                <NullableCell
                  value={current?.current_replicas}
                  reason="The controller has published no replica count for this autoscaler, so whether new bounds take effect immediately cannot be worked out here."
                />
              ),
            },
            {
              label: 'Target',
              value: current?.target
                ? `${current.target.kind} ${current.target.name}`
                : <span style={MUTED}>none declared</span>,
            },
            {
              label: 'At its limit',
              value:
                current?.scaling_limited === true ? (
                  <StatusBadge
                    status="Unknown"
                    label="Yes"
                    tooltip="The desired replica count is being clamped to one of the bounds below. During an incident this is the answer to “why is this not scaling up”."
                  />
                ) : current?.scaling_limited === false ? (
                  <span style={MUTED}>no</span>
                ) : (
                  <NullableCell value={null} reason="The controller has not written a ScalingLimited condition yet." />
                ),
            },
          ]}
        />

        <SectionHeader title="Metrics" description="Each metric's current reading beside its target." />
        <Metrics metrics={current?.metrics} />

        <SectionHeader
          title="Bounds"
          description="Both are written on every change, including the one you are not moving: a bound left out would be ambiguous between “leave it” and “reset it”."
        />
        <Grid hasGutter>
          <GridItem span={6}>
            <FormGroup label="Minimum replicas" fieldId="hpa-min">
              <NumberInput
                id="hpa-min"
                value={bounds.min ?? ''}
                min={0}
                max={MAX_REPLICAS}
                onMinus={() => setBounds((b) => ({ ...b, min: clamp((b.min ?? 0) - 1) }))}
                onPlus={() => setBounds((b) => ({ ...b, min: clamp((b.min ?? 0) + 1) }))}
                onChange={(event) =>
                  setBounds((b) => ({
                    ...b,
                    min: event.target.value === '' ? null : clamp(Number(event.target.value)),
                  }))
                }
                inputAriaLabel="Minimum replicas"
                minusBtnAriaLabel="One fewer minimum replica"
                plusBtnAriaLabel="One more minimum replica"
                data-testid="hpa-min"
              />
            </FormGroup>
          </GridItem>
          <GridItem span={6}>
            <FormGroup label="Maximum replicas" fieldId="hpa-max">
              <NumberInput
                id="hpa-max"
                value={bounds.max ?? ''}
                min={1}
                max={MAX_REPLICAS}
                onMinus={() => setBounds((b) => ({ ...b, max: clamp((b.max ?? 1) - 1) }))}
                onPlus={() => setBounds((b) => ({ ...b, max: clamp((b.max ?? 1) + 1) }))}
                onChange={(event) =>
                  setBounds((b) => ({
                    ...b,
                    max: event.target.value === '' ? null : clamp(Number(event.target.value)),
                  }))
                }
                inputAriaLabel="Maximum replicas"
                minusBtnAriaLabel="One fewer maximum replica"
                plusBtnAriaLabel="One more maximum replica"
                data-testid="hpa-max"
              />
            </FormGroup>
          </GridItem>
        </Grid>

        {plan?.blocked && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="hpa-blocked"
            title="Nothing would change"
          >
            {plan.blocked.message} {plan.blocked.hint}
          </Alert>
        )}

        {consequences.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="hpa-consequences"
            title={
              consequences.length === 1
                ? 'One thing about this change needs acknowledging'
                : `${consequences.length} things about this change need acknowledging`
            }
          >
            {consequences.map((item) => (
              <div key={item.code} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`hpa-ack-${item.code}`}
                  data-testid={`hpa-ack-${item.code}`}
                  isChecked={acknowledged.includes(item.code)}
                  onChange={(_e, checked) =>
                    setAcknowledged((codes) =>
                      checked ? [...codes, item.code] : codes.filter((code) => code !== item.code),
                    )
                  }
                  label={<strong>{item.label}</strong>}
                  description={
                    <>
                      <div>{item.consequence}</div>
                      <div style={{ marginTop: '0.25rem' }}>
                        <em>{item.mitigation}</em>
                      </div>
                    </>
                  }
                />
              </div>
            ))}
          </Alert>
        )}
      </Form>
    </MutationDialog>
  );
}
