/**
 * PodSecurityDialog — §18's write, wrapped in the §11.3 handshake.
 *
 * Setting a namespace's Pod Security level is six labels on one object, and
 * §4's YAML editor could already write them. What it could not do is answer
 * the question an operator actually has first — *what does this break* — and
 * that answer is this dialog's whole reason to exist.
 *
 * Three things here are load-bearing.
 *
 * **The preview's warnings are the API server's, not this console's.** Pod
 * Security admission evaluates the pods already in the namespace when the
 * labels change and reports them as `Warning:` headers, on `dryRun=All`
 * exactly as on a real write. They arrive as `admissionWarnings` and are
 * rendered verbatim, one line each: this console neither computes that list
 * nor parses it, because a summary of somebody else's admission decision is a
 * summary that can be wrong about which pods are affected.
 *
 * **Confirm is blocked until those warnings are read.** The whole point of the
 * preview is the list of workloads that stop being deployable; a Confirm that
 * stayed enabled beside it would make reading optional. This is the one
 * acknowledgement the server cannot enforce — it holds no state between the
 * preview and the confirm, and the warnings only exist on a response — so the
 * dialog says so rather than implying the backend is checking.
 *
 * **Nothing is evicted, and that is a checkbox rather than a footnote.**
 * Admission runs when a pod is created. An operator who reads "enforce:
 * restricted" as "the workloads in front of me are now restricted" is wrong
 * about their cluster, which is the confident-wrong-answer this product treats
 * as a defect.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  HelperText,
  HelperTextItem,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { SectionHeader, StatusBadge } from './ui';
import { podSecurity as podSecurityApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MODES = ['enforce', 'audit', 'warn'];

const LEVELS = [
  { value: '', label: 'not set (cluster default applies)' },
  { value: 'privileged', label: 'privileged' },
  { value: 'baseline', label: 'baseline' },
  { value: 'restricted', label: 'restricted' },
];

const VERSIONS = [
  { value: '', label: 'not set' },
  { value: 'latest', label: 'latest (tightens as the cluster upgrades)' },
  { value: 'v1.31', label: 'v1.31 (pinned)' },
  { value: 'v1.32', label: 'v1.32 (pinned)' },
  { value: 'v1.33', label: 'v1.33 (pinned)' },
];

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** The dialog's form state, seeded from the labels the namespace has now. */
function initialForm(current) {
  const form = {};
  for (const mode of MODES) {
    form[mode] = current?.[mode] ?? '';
    form[`${mode}Version`] = current?.[`${mode}Version`] ?? '';
  }
  return form;
}

/**
 * The §18 body. Every mode is named on every request, `null` meaning "remove
 * that label" — a body whose omitted field could mean either "leave it" or
 * "remove it" is a body that strips somebody's audit level from a blank field.
 */
function buildPodSecurity(form) {
  const podSecurity = {};
  for (const mode of MODES) {
    const level = form[mode] || null;
    podSecurity[mode] = level;
    podSecurity[`${mode}Version`] = level ? form[`${mode}Version`] || null : null;
  }
  return podSecurity;
}

function levelLabel(value) {
  if (!value) return <span style={MUTED}>not set</span>;
  return <StatusBadge status={value === 'privileged' ? 'unknown' : 'Ready'} label={value} />;
}

/**
 * The panel under the diff: what admission said about the pods already running.
 *
 * Rendered on the preview *and* after the write, because an operator who
 * confirmed without reading it still needs to be told what admission found —
 * and after the write the same warnings mean something sharper: those pods are
 * running under a level their spec does not meet, and the next thing that
 * replaces them will not start.
 */
function AdmissionReport({ result, phase }) {
  if (!result) return null;
  const warnings = result.admissionWarnings ?? [];
  const executed = phase === 'done';

  return (
    <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }} data-testid="psa-admission">
      <SectionHeader
        title="What Pod Security admission says about the pods already running"
        description={
          executed
            ? 'These are the API server’s own warnings from the write. Nothing here was evicted — admission runs when a pod is created, so each of these keeps running until something replaces it.'
            : 'The API server evaluated the pods in this namespace against the level being set and returned this. It is reproduced verbatim; this console does not compute or interpret it.'
        }
      />
      {warnings.length === 0 ? (
        <p data-testid="psa-admission-quiet" style={MUTED}>
          Admission returned no warnings. That means it found nothing in this namespace that violates
          the level — not that this console checked and agreed.
        </p>
      ) : (
        <>
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="psa-violations"
            data-count={String(warnings.length)}
            title={`${warnings.length} admission warning${warnings.length === 1 ? '' : 's'} about pods already in this namespace`}
          >
            <ul className="admin-confirm__warnings">
              {warnings.map((warning, i) => (
                <li key={i} data-testid="psa-violation">
                  {warning}
                </li>
              ))}
            </ul>
          </Alert>
          <p style={MUTED}>
            None of these pods stops, restarts or is evicted by this change. Each keeps running until
            something replaces it, and then its controller cannot create the replacement — the
            workload sits below its replica count with a <code>FailedCreate</code> event and no pod to
            inspect.
          </p>
        </>
      )}
    </div>
  );
}

export default function PodSecurityDialog({ namespace, current, onClose, onApplied }) {
  const [form, setForm] = useState(() => initialForm(current));
  const [acknowledged, setAcknowledged] = useState([]);
  const [readWarnings, setReadWarnings] = useState(false);
  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  const podSecurity = useMemo(() => buildPodSecurity(form), [form]);

  const {
    data: plan,
    loading: planLoading,
    error: planError,
  } = useAsync(() => podSecurityApi.plan(namespace, { podSecurity }), {
    key: `psa-plan:${namespace}:${JSON.stringify(podSecurity)}`,
  });

  // The version the *plan* read, not one the page passed down: it is the
  // namespace as it was a moment ago rather than as it was when the page
  // loaded, and it is what rides inside the merge patch to make rule 4 the
  // API server's decision rather than only this console's.
  const resourceVersion = plan?.resourceVersion ?? null;

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);

  // Per-list, not a boolean: changing a mode changes what is being consented
  // to, and consent given for one set must not carry onto another.
  const signature = consequences
    .map((c) => c.code)
    .sort()
    .join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setAcknowledged([]);
    setReadWarnings(false);
  }, [signature]);

  const unacknowledged = consequences.filter((c) => !acknowledged.includes(c.code));

  const request = useCallback(
    (dryRun) =>
      podSecurityApi.set(namespace, {
        podSecurity,
        resourceVersion,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [namespace, podSecurity, resourceVersion, acknowledged],
  );

  /**
   * Re-checked against the dry run's own answer, which is the later read: the
   * server recomputes the consequences against the namespace as it is now, so
   * somebody else's change between the plan and the preview shows up here.
   */
  const confirmBlockedReason = useCallback(
    (result) => {
      const missing = (result?.consequences ?? []).filter((c) => !acknowledged.includes(c.code));
      if (missing.length) {
        return (
          'The dry run reported consequences that have not been acknowledged: ' +
          `${missing.map((c) => c.label).join('; ')}. Go back and tick each one.`
        );
      }
      const warnings = result?.admissionWarnings ?? [];
      if (warnings.length && !readWarnings) {
        return (
          `Pod Security admission returned ${warnings.length} warning${warnings.length === 1 ? '' : 's'} about pods already ` +
          'running in this namespace. Read them and tick the box below them before confirming.'
        );
      }
      return null;
    },
    [acknowledged, readWarnings],
  );

  const renderExtra = useCallback(
    ({ result, phase }) => {
      const warnings = result?.admissionWarnings ?? [];
      return (
        <>
          <AdmissionReport result={result} phase={phase} />
          {phase !== 'done' && warnings.length > 0 && (
            <Checkbox
              id="psa-read-warnings"
              data-testid="psa-read-warnings"
              isChecked={readWarnings}
              onChange={(_e, checked) => setReadWarnings(checked)}
              label={<strong>I have read the admission warnings above</strong>}
              description={
                'The API server, not this console, produced that list. Nothing checks this box on the ' +
                'server: the console holds no state between this preview and the confirm, so this is ' +
                'the dialog asking, not the backend enforcing.'
              }
            />
          )}
        </>
      );
    },
    [readWarnings],
  );

  let previewBlocked = null;
  if (planError) previewBlocked = planError.message;
  else if (planLoading || !plan) previewBlocked = 'Still planning…';
  else if (!plan.changed) previewBlocked = 'These are the levels this namespace already declares. Change one to preview.';
  else if (unacknowledged.length) {
    previewBlocked = `Acknowledge what this change means: ${unacknowledged.map((c) => c.label).join('; ')}.`;
  }

  return (
    <MutationDialog
      isOpen
      title={`Pod Security level for ${namespace}`}
      description="Six labels on one namespace, through the funnel. The preview asks the API server what would change and Pod Security admission answers with the pods already running that do not meet the new level."
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the change"
      confirmLabel="Set the level"
      autoPreview={false}
      confirmBlockedReason={confirmBlockedReason}
      renderExtra={renderExtra}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="psa-form">
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="psa-disabled"
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
            data-testid="psa-plan-error"
            title="This change could not be planned"
          >
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        <SectionHeader
          title="The level this namespace declares"
          description="`enforce` refuses pods that do not meet it. `warn` reports the same violation to whoever runs the apply, and `audit` records it in the API server's audit log. Setting a mode to “not set” removes the label."
        />

        <Grid hasGutter>
          {MODES.map((mode) => (
            <GridItem span={4} key={mode}>
              <FormGroup label={mode} fieldId={`psa-${mode}`}>
                <FormSelect
                  id={`psa-${mode}`}
                  value={form[mode]}
                  onChange={(_e, value) => set({ [mode]: value })}
                  data-testid={`psa-${mode}`}
                >
                  {LEVELS.map((level) => (
                    <FormSelectOption key={level.value} value={level.value} label={level.label} />
                  ))}
                </FormSelect>
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>
                      now: {levelLabel(current?.[mode])}
                      {current?.[`${mode}Version`] ? ` (${current[`${mode}Version`]})` : ''}
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </GridItem>
          ))}
        </Grid>

        <Grid hasGutter>
          {MODES.map((mode) => (
            <GridItem span={4} key={`${mode}-version`}>
              <FormGroup label={`${mode} version`} fieldId={`psa-${mode}-version`}>
                <FormSelect
                  id={`psa-${mode}-version`}
                  value={form[`${mode}Version`]}
                  isDisabled={!form[mode]}
                  onChange={(_e, value) => set({ [`${mode}Version`]: value })}
                  data-testid={`psa-${mode}-version`}
                >
                  {VERSIONS.map((version) => (
                    <FormSelectOption key={version.value} value={version.value} label={version.label} />
                  ))}
                </FormSelect>
              </FormGroup>
            </GridItem>
          ))}
        </Grid>

        {plan?.changed === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="psa-unchanged"
            title="This is what the namespace already declares"
          >
            Nothing would change. Pick a different level to preview one.
          </Alert>
        )}

        {consequences.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="psa-consequences"
            title={
              consequences.length === 1
                ? 'One thing about this change needs acknowledging'
                : `${consequences.length} things about this change need acknowledging`
            }
          >
            {consequences.map((item) => (
              <div key={item.code} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`psa-ack-${item.code}`}
                  data-testid={`psa-ack-${item.code}`}
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
