/**
 * NewProjectDialog — §17's write, wrapped in the §11.3 handshake.
 *
 * `oc new-project` on OpenShift instantiates a project request template: a
 * Namespace, a ResourceQuota, a LimitRange, a RoleBinding for the requester
 * and, on most production clusters, a NetworkPolicy. This dialog is that
 * request for vanilla Kubernetes, and it is built out of five ordinary
 * creates through the funnel rather than a template engine — the defaults
 * live in this form, nothing is stored, and nothing reconciles afterwards.
 *
 * Four things here are load-bearing.
 *
 * **The dry run projects the Namespace and renders the rest, and says so.**
 * The API server cannot project a create into a namespace that does not exist
 * yet — admission refuses it with a 404 — so on the preview only the Namespace
 * carries the API server's own diff. The four objects inside it carry
 * `projection: "rendered"`: this console's manifest, diffed against nothing,
 * with a preflight of the verb the real write will use. The report labels
 * each object's diff with whose it is, so nobody reads a rendered manifest as
 * an admission-checked projection.
 *
 * **`created` is the only success, and it is never true on a dry run.** Five
 * writes of which four can succeed is a partial project, and `summarize` says
 * how many landed rather than "Applied".
 *
 * **Every consequence is acknowledged by code.** The backend refuses the write
 * unless every code the plan returned is named, and the list is recomputed at
 * write time — so consent given for one form's consequences cannot be carried
 * onto another's. Acknowledgements clear whenever the plan's set changes.
 *
 * **A preflight denial blocks Confirm with the grant it needs.** It is the
 * one thing about a namespaced object that can be checked before its
 * namespace exists, and finding it out after the Namespace was created leaves
 * a half-project behind for a refusal that was knowable up front.
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
  TextArea,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import DiffView from './DiffView';
import { CodeBlock, PartialBanner, SectionHeader, StatusBadge } from './ui';
import { projects } from '../api/client';
import { useAsync } from '../pages/_data';

const LEVELS = [
  { value: '', label: 'not set (cluster default applies)' },
  { value: 'privileged', label: 'privileged' },
  { value: 'baseline', label: 'baseline' },
  { value: 'restricted', label: 'restricted' },
];
const SUBJECT_KINDS = ['Group', 'User', 'ServiceAccount'];
const ROLES = ['admin', 'edit', 'view'];
const QUOTA_FIELDS = [
  { key: 'requests.cpu', label: 'CPU requests', placeholder: '2' },
  { key: 'requests.memory', label: 'Memory requests', placeholder: '4Gi' },
  { key: 'limits.cpu', label: 'CPU limits', placeholder: '4' },
  { key: 'limits.memory', label: 'Memory limits', placeholder: '8Gi' },
  { key: 'pods', label: 'Pods', placeholder: '20' },
];
const NAME_RULE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/**
 * The defaults are OpenShift's documented project-template example, not
 * numbers this console invented: restricted admission on every mode, a quota
 * that bounds compute and pod count, and a Container LimitRange whose defaults
 * keep that quota from refusing every pod that did not state its own requests.
 */
const INITIAL = {
  name: '',
  displayName: '',
  description: '',
  enforce: 'restricted',
  audit: 'restricted',
  warn: 'restricted',
  quota: { 'requests.cpu': '2', 'requests.memory': '4Gi', 'limits.cpu': '4', 'limits.memory': '8Gi', pods: '20' },
  limits: { requestCpu: '100m', requestMemory: '128Mi', limitCpu: '500m', limitMemory: '512Mi' },
  adminKind: 'Group',
  adminName: '',
  adminRole: 'admin',
  isolateIngress: false,
};

function compact(map) {
  return Object.fromEntries(Object.entries(map).filter(([, v]) => String(v ?? '').trim() !== ''));
}

/** The §17 request body, with every blank field left out rather than sent empty. */
function buildBody(form) {
  const body = {
    name: form.name.trim(),
    displayName: form.displayName.trim() || null,
    description: form.description.trim() || null,
    podSecurity: {
      enforce: form.enforce || null,
      audit: form.audit || null,
      warn: form.warn || null,
    },
    adminRole: form.adminRole,
    isolateIngress: form.isolateIngress,
  };
  const quota = compact(form.quota);
  if (Object.keys(quota).length) body.quota = quota;
  const defaultRequest = compact({ cpu: form.limits.requestCpu, memory: form.limits.requestMemory });
  const defaults = compact({ cpu: form.limits.limitCpu, memory: form.limits.limitMemory });
  if (Object.keys(defaultRequest).length || Object.keys(defaults).length) {
    const item = { type: 'Container' };
    if (Object.keys(defaultRequest).length) item.defaultRequest = defaultRequest;
    if (Object.keys(defaults).length) item.default = defaults;
    body.limits = [item];
  }
  if (form.adminName.trim()) body.admins = [{ kind: form.adminKind, name: form.adminName.trim() }];
  return body;
}

function targetState(target) {
  if (!target) return { status: 'Unknown', label: 'Not checked yet' };
  if (target.exists === false) return { status: 'Ready', label: 'Does not exist — can be created' };
  if (target.exists === true) return { status: 'NotReady', label: 'Already exists' };
  return { status: 'Unknown', label: 'Could not check' };
}

/**
 * The `done` headline, replacing MutationDialog's default. "Applied to the
 * cluster" is the wrong sentence for five writes of which four can succeed.
 */
function summarize(result) {
  const total = result?.objects?.length ?? 0;
  if (result?.created === true) {
    return {
      variant: 'success',
      title: `Project ${result.name} created — ${total} object${total === 1 ? '' : 's'} written`,
      body: 'Each object went through the funnel on its own and has its own audit row. Open the namespace to see what governs it now.',
    };
  }
  const missing = (result?.failed ?? 0) + (result?.skipped ?? 0);
  if (missing > 0) {
    return {
      variant: 'danger',
      title: `${missing} of ${total} objects were not created`,
      body:
        'The project is partially created. Nothing was rolled back — the objects that were written are ' +
        'still there, and each failure below names the grant it needed. Add the missing objects through ' +
        'the YAML editor, where each is its own diff.',
    };
  }
  return {
    variant: 'warning',
    title: 'The write completed without reporting that the project was created',
    body: 'Re-read the namespace before assuming anything landed.',
  };
}

function projectionBadge(object, executed) {
  if (object.skipped) {
    return <StatusBadge status="unknown" label="skipped" tooltip={object.skipped} />;
  }
  if (object.error) {
    return <StatusBadge status="NotReady" label={object.error.code} />;
  }
  if (executed && object.applied) {
    return <StatusBadge status="Ready" label="written" />;
  }
  if (object.projection === 'rendered') {
    return (
      <StatusBadge
        status="Unknown"
        label="rendered by the console"
        tooltip="The API server cannot project into a namespace that does not exist yet. This is the console's own manifest, not an admission-checked projection."
      />
    );
  }
  return <StatusBadge status="Unknown" label="projected by the API server" tooltip="A dry run. Nothing was written." />;
}

/**
 * What happens, or happened, to each object. Rendered on the preview and again
 * on `done`, because the grant a failed object needed is per-object and that
 * is when it matters most.
 */
function ObjectReport({ result, phase }) {
  const objects = result?.objects ?? [];
  if (!objects.length) return null;
  const executed = phase === 'done';
  return (
    <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }} data-testid="project-object-report">
      <SectionHeader
        title={executed ? 'What happened to each object' : 'What this will write'}
        description={
          executed
            ? 'Each was a separate write through the funnel, with its own audit row. Nothing was rolled back.'
            : 'The Namespace is projected by the API server. The objects inside it cannot be until it exists, so they are shown as rendered, each with a preflight of the grant the write will need.'
        }
      />
      {objects.map((object) => (
        <div
          key={`${object.kind}/${object.namespace ?? ''}/${object.name}`}
          style={{ marginBlockEnd: '0.75rem' }}
          data-testid="project-object"
          data-kind={object.kind}
          data-projection={object.projection ?? ''}
        >
          <div>
            <strong>
              {object.kind} {object.namespace ? `${object.namespace}/` : ''}
              {object.name}
            </strong>{' '}
            {projectionBadge(object, executed)}
            {object.preflight?.allowed === false && (
              <span style={{ marginInlineStart: '0.5rem' }} data-testid="project-object-preflight-denied">
                <StatusBadge status="NotReady" label="preflight refused" />{' '}
                <span style={MUTED}>{object.preflight.hint || object.preflight.reason}</span>
              </span>
            )}
            {object.preflight && object.preflight.allowed !== true && object.preflight.evaluationError && (
              <span style={{ marginInlineStart: '0.5rem' }}>
                <StatusBadge status="Unknown" label="preflight undecided" tooltip={object.preflight.evaluationError} />
              </span>
            )}
          </div>
          {object.error && (
            <div style={{ marginBlockStart: '0.25rem' }}>
              {object.error.message}
              {object.error.hint ? <div style={{ fontWeight: 600 }}>{object.error.hint}</div> : null}
            </div>
          )}
          {object.skipped && <div style={{ ...MUTED, marginBlockStart: '0.25rem' }}>{object.skipped}</div>}
          {!executed && object.diff?.unified && (
            <DiffView unified={object.diff.unified} changed={object.diff.changed} maxHeight={200} ariaLabel={`${object.kind} ${object.name} diff`} />
          )}
        </div>
      ))}
    </div>
  );
}

export default function NewProjectDialog({ onClose, onApplied }) {
  const [form, setForm] = useState(INITIAL);
  const [acknowledged, setAcknowledged] = useState([]);
  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  const body = useMemo(() => buildBody(form), [form]);
  const nameValid = NAME_RULE.test(body.name);

  const {
    data: plan,
    loading: planLoading,
    error: planError,
  } = useAsync(() => projects.plan(body), {
    key: `project-plan:${JSON.stringify(body)}`,
    enabled: nameValid,
  });

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);

  // Per-list, not a boolean: changing the quota changes what is consented to.
  const signature = consequences.map((c) => c.code).sort().join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setAcknowledged([]);
  }, [signature]);

  const unacknowledged = consequences.filter((c) => !acknowledged.includes(c.code));

  const request = useCallback(
    (dryRun) => projects.create({ ...body, acknowledgeConsequences: acknowledged, dryRun }),
    [body, acknowledged],
  );

  /** Re-checked against the dry run's own answer, which is the later read. */
  const confirmBlockedReason = useCallback(
    (result) => {
      const missing = (result?.consequences ?? []).filter((c) => !acknowledged.includes(c.code));
      if (missing.length) {
        return (
          'The dry run reported consequences that have not been acknowledged: ' +
          `${missing.map((c) => c.label).join('; ')}. Go back and tick each one.`
        );
      }
      const refused = (result?.objects ?? []).filter((o) => o.preflight?.allowed === false);
      if (refused.length) {
        return (
          `The preflight refused ${refused.map((o) => `${o.kind} ${o.name}`).join(', ')}. ` +
          `${refused[0].preflight.hint || refused[0].preflight.reason || ''} Creating the Namespace now would leave a half-project behind for a refusal that is already known.`
        );
      }
      return null;
    },
    [acknowledged],
  );

  let previewBlocked = null;
  if (!body.name) previewBlocked = 'Name the project.';
  else if (!nameValid) previewBlocked = 'A namespace name is lower-case letters, digits and hyphens, at most 63 characters, starting and ending with a letter or digit.';
  else if (planError) previewBlocked = planError.message;
  else if (planLoading || !plan) previewBlocked = 'Still planning…';
  else if (plan.target?.exists === true) previewBlocked = `${body.name} already exists. A project is created into a namespace that does not exist; this console does not take one over.`;
  else if (unacknowledged.length) previewBlocked = `Acknowledge what this project means: ${unacknowledged.map((c) => c.label).join('; ')}.`;

  const target = targetState(plan?.target);

  return (
    <MutationDialog
      isOpen
      title="New project"
      description="A namespace and what governs it — quota, limits, Pod Security level, a role binding and, if asked, network isolation — as five writes through the funnel. Nothing is created until you confirm."
      request={request}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the project"
      confirmLabel="Create the project"
      autoPreview={false}
      confirmBlockedReason={confirmBlockedReason}
      renderExtra={ObjectReport}
      summarize={summarize}
      onClose={onClose}
      // Deliberately does NOT close: the per-object report is the only place
      // that says which object failed and what grant it needed.
      onApplied={onApplied}
      variant="large"
      className="admin-project-dialog"
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="project-form">
        <PartialBanner unavailable={plan?.unavailable} />

        {plan?.enabled === false && (
          <Alert isInline variant="info" className="admin-confirm__alert" data-testid="project-disabled" title="This console runs read-only">
            {plan.enabledDetail} The preview below still runs and shows exactly what would be created.
          </Alert>
        )}

        {planError && (
          <Alert isInline variant="danger" className="admin-confirm__alert" data-testid="project-plan-error" title="This project could not be planned">
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        <Grid hasGutter>
          <GridItem span={4}>
            <FormGroup label="Name" fieldId="project-name" isRequired>
              <TextInput
                id="project-name"
                value={form.name}
                onChange={(_e, value) => set({ name: value })}
                validated={form.name && !nameValid ? 'error' : 'default'}
                data-testid="project-name"
              />
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>The namespace name. Lower-case, DNS label, at most 63 characters.</HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>
          </GridItem>
          <GridItem span={4}>
            <FormGroup label="Display name" fieldId="project-display-name">
              <TextInput id="project-display-name" value={form.displayName} onChange={(_e, value) => set({ displayName: value })} data-testid="project-display-name" />
            </FormGroup>
          </GridItem>
          <GridItem span={4}>
            <FormGroup label="Description" fieldId="project-description">
              <TextArea id="project-description" value={form.description} onChange={(_e, value) => set({ description: value })} resizeOrientation="vertical" rows={1} />
            </FormGroup>
          </GridItem>
        </Grid>

        <SectionHeader
          title="Pod Security"
          description="The admission level the namespace declares — OpenShift's security context constraints in vanilla vocabulary. `enforce` refuses pods; `warn` and `audit` only report."
        />
        <Grid hasGutter>
          {['enforce', 'audit', 'warn'].map((mode) => (
            <GridItem span={4} key={mode}>
              <FormGroup label={mode} fieldId={`project-psa-${mode}`}>
                <FormSelect id={`project-psa-${mode}`} value={form[mode]} onChange={(_e, value) => set({ [mode]: value })} data-testid={`project-psa-${mode}`}>
                  {LEVELS.map((level) => (
                    <FormSelectOption key={level.value} value={level.value} label={level.label} />
                  ))}
                </FormSelect>
              </FormGroup>
            </GridItem>
          ))}
        </Grid>

        <SectionHeader
          title="Resource quota"
          description="Hard limits on what the whole namespace may consume. Leave every field blank to create no quota — and acknowledge that nothing bounds it."
        />
        <Grid hasGutter>
          {QUOTA_FIELDS.map((field) => (
            <GridItem span={2} key={field.key}>
              <FormGroup label={field.label} fieldId={`project-quota-${field.key}`}>
                <TextInput
                  id={`project-quota-${field.key}`}
                  value={form.quota[field.key]}
                  placeholder={field.placeholder}
                  onChange={(_e, value) => set({ quota: { ...form.quota, [field.key]: value } })}
                  data-testid={`project-quota-${field.key}`}
                />
              </FormGroup>
            </GridItem>
          ))}
        </Grid>

        <SectionHeader
          title="Container defaults"
          description="A LimitRange giving containers that state no resources a request and a limit. Without it, a compute quota refuses every such pod at admission."
        />
        <Grid hasGutter>
          {[
            ['requestCpu', 'Default CPU request'],
            ['requestMemory', 'Default memory request'],
            ['limitCpu', 'Default CPU limit'],
            ['limitMemory', 'Default memory limit'],
          ].map(([key, label]) => (
            <GridItem span={3} key={key}>
              <FormGroup label={label} fieldId={`project-limit-${key}`}>
                <TextInput id={`project-limit-${key}`} value={form.limits[key]} onChange={(_e, value) => set({ limits: { ...form.limits, [key]: value } })} data-testid={`project-limit-${key}`} />
              </FormGroup>
            </GridItem>
          ))}
        </Grid>

        <SectionHeader title="Access" description="Who administers the project. One RoleBinding to a built-in aggregated ClusterRole, inside the namespace." />
        <Grid hasGutter>
          <GridItem span={3}>
            <FormGroup label="Subject kind" fieldId="project-admin-kind">
              <FormSelect id="project-admin-kind" value={form.adminKind} onChange={(_e, value) => set({ adminKind: value })} data-testid="project-admin-kind">
                {SUBJECT_KINDS.map((kind) => (
                  <FormSelectOption key={kind} value={kind} label={kind} />
                ))}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem span={6}>
            <FormGroup label="Subject name" fieldId="project-admin-name">
              <TextInput id="project-admin-name" value={form.adminName} onChange={(_e, value) => set({ adminName: value })} placeholder="payments-team" data-testid="project-admin-name" />
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    As your identity provider spells it. A ServiceAccount subject is bound in this project&apos;s own namespace. Leave blank to bind nobody — and acknowledge it.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>
          </GridItem>
          <GridItem span={3}>
            <FormGroup label="Role" fieldId="project-admin-role">
              <FormSelect id="project-admin-role" value={form.adminRole} onChange={(_e, value) => set({ adminRole: value })} data-testid="project-admin-role">
                {ROLES.map((role) => (
                  <FormSelectOption key={role} value={role} label={role} />
                ))}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem span={12}>
            <Checkbox
              id="project-isolate"
              data-testid="project-isolate"
              isChecked={form.isolateIngress}
              onChange={(_e, checked) => set({ isolateIngress: checked })}
              label="Refuse ingress from other namespaces"
              description="An allow-same-namespace NetworkPolicy: every pod accepts traffic from pods in this namespace and from nowhere else — the router included, until a second policy admits it."
            />
          </GridItem>
        </Grid>

        {/* ── The plan ───────────────────────────────────────────────── */}

        <SectionHeader title="The namespace" description="Whether a project can be created under this name." />
        <div data-testid="project-target" data-exists={plan ? String(plan.target?.exists) : 'unread'}>
          {plan ? (
            <>
              <StatusBadge status={target.status} label={target.label} tooltip={plan.target?.detail || undefined} />{' '}
              <span style={MUTED}>{plan.target?.detail}</span>
            </>
          ) : (
            <span style={MUTED}>{nameValid ? 'Reading the cluster…' : 'Name the project and it is checked against the cluster.'}</span>
          )}
        </div>

        {plan?.target?.exists === true && (
          <Alert isInline variant="warning" className="admin-confirm__alert" data-testid="project-target-exists" title="This namespace already exists">
            {plan.target.detail} A project is created into a namespace that does not exist. Add quotas, limits or bindings to an existing namespace one at a time through the YAML editor, where each is its own diff.
          </Alert>
        )}

        {consequences.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="project-consequences"
            title={
              consequences.length === 1
                ? 'One thing about this project needs acknowledging'
                : `${consequences.length} things about this project need acknowledging`
            }
          >
            {consequences.map((item) => (
              <div key={item.code} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`project-ack-${item.code}`}
                  data-testid={`project-ack-${item.code}`}
                  isChecked={acknowledged.includes(item.code)}
                  onChange={(_e, checked) =>
                    setAcknowledged((current) =>
                      checked ? [...current, item.code] : current.filter((code) => code !== item.code),
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

        {plan?.objects?.length ? (
          <>
            <SectionHeader
              title={`The ${plan.objects.length} object${plan.objects.length === 1 ? '' : 's'} that would be created`}
              description="Rendered from this form. The preview on the next step projects the Namespace through the API server and preflights the rest."
            />
            {plan.objects.map((object) => (
              <div key={`${object.kind}/${object.name}`} style={{ marginTop: '0.5rem' }} data-testid="project-plan-object" data-kind={object.kind}>
                <strong>
                  {object.kind} {object.namespace ? `${object.namespace}/` : ''}
                  {object.name}
                </strong>
                <CodeBlock code={object.yaml} language="yaml" maxHeight={180} ariaLabel={`${object.kind} manifest`} />
              </div>
            ))}
          </>
        ) : null}
      </Form>
    </MutationDialog>
  );
}
