/**
 * GrantRoleDialog — §30, what a role binding actually confers.
 *
 * §4's YAML editor can already write this object, and its diff is honest and
 * useless: a name appearing in a list. What that name can now *do* is in a
 * different object, and the operator confirming the grant has not opened it.
 * This dialog is that object, read and summarised at the moment of the decision.
 *
 * Four things it renders that the binding does not say:
 *
 * **What the role confers.** Not every verb — a finding that fires on every
 * grant is one nobody reads on the day it matters. Five: a wildcard, reading
 * Secrets, `create pods/exec` (which reaches the Secrets mounted into every pod
 * here whether or not the role mentions them), writing RoleBindings (the
 * grantee can grant themselves the rest), and `impersonate`.
 *
 * **Whether the role could be read at all.** `unreadable` is drawn as unknown
 * and never as an empty rule list. "This role grants nothing" is the one
 * sentence that must not be produced by a read that failed, and it would be
 * produced on the screen where somebody decides to bind it.
 *
 * **Whether the role exists.** The API server accepts a binding to a role that
 * is not there. It grants nothing today and starts granting whenever somebody
 * creates that name — with no second decision by anybody.
 *
 * **What a revoke does not take away.** Removing a subject from one binding is
 * not revoking their access: another binding here, or any ClusterRoleBinding,
 * still grants it. The residual list is what stops `applied: true` being read as
 * "they can no longer act here" — and when the cluster-wide listing is refused
 * it is drawn as unknown, because `[]` there means "the cluster was searched"
 * and that is the sentence somebody closes a ticket on.
 */
import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Form,
  FormGroup,
  Radio,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import ConsequenceChecklist from './ConsequenceChecklist';
import { blockedByConsequences, useAcknowledgements } from './consequences';
import { DataTable, NullableCell, SectionHeader, StatusBadge } from './ui';
import { access as accessApi } from '../api/client';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

const SUBJECT_KINDS = ['User', 'Group', 'ServiceAccount'];
const ROLE_KINDS = ['ClusterRole', 'Role'];

/**
 * The one-line verdict on the role, and the three states behind it.
 *
 * `present` is the only one drawn in a resolved colour. `absent` and
 * `unreadable` are both drawn as warnings and say different things, because
 * they send an operator to two different places: a spelling, and a permission.
 */
function CapabilityBadge({ capability }) {
  const state = capability?.state;
  if (state === 'unreadable') {
    return (
      <StatusBadge
        status="warning"
        label="Could not be read"
        tooltip="This console could not fetch the role, so what this binding confers is unknown. It is not a role that grants nothing."
      />
    );
  }
  if (state === 'absent') {
    return (
      <StatusBadge
        status="warning"
        label="Does not exist"
        tooltip="The API server accepts a binding to a role that is not there. It grants nothing until somebody creates that name."
      />
    );
  }
  if (capability?.rules === null) {
    return (
      <StatusBadge
        status="warning"
        label="Rules not written yet"
        tooltip="An aggregated ClusterRole whose rules the aggregation controller has not filled in. It is not empty; it is unwritten, and it grows on its own."
      />
    );
  }
  return <StatusBadge status="Ready" label="Read" />;
}

/** What the role confers, or the reason there is no list to show. */
function CapabilityPanel({ capability }) {
  const powers = capability?.powers ?? [];

  return (
    <div data-testid="grant-capability">
      <p>
        <CapabilityBadge capability={capability} />{' '}
        <span style={MUTED}>
          {'rules: '}
        </span>
        <span data-testid="grant-rule-count">
          <NullableCell
            value={capability?.rule_count}
            reason={
              capability?.state === 'unreadable'
                ? 'The role could not be read, so how many rules it holds is unknown — not zero.'
                : capability?.state === 'absent'
                  ? 'There is no such role, so it holds no rules yet. It will hold whatever the role holds once one is created.'
                  : 'An aggregated role whose rules the controller has not written yet. Not zero.'
            }
          />
        </span>
      </p>
      {powers.length ? (
        <div data-testid="grant-powers">
          <DataTable
            ariaLabel="What this role confers"
            tableId="grant-powers"
            rows={powers}
            rowKey={(row) => row.code}
            resizableColumns={false}
            columns={[
              {
                key: 'code',
                title: 'Confers',
                cell: (row) => <code data-testid={`grant-power-${row.code}`}>{row.code}</code>,
              },
              { key: 'detail', title: 'What that means', modifier: 'breakWord' },
            ]}
          />
        </div>
      ) : capability?.state === 'present' && capability?.rules !== null ? (
        <p style={MUTED} data-testid="grant-powers-none">
          None of the five capabilities this console calls out — a wildcard,
          reading Secrets, exec into pods, writing role bindings, impersonation.
          The role still grants whatever its {capability.rule_count} rule
          {capability.rule_count === 1 ? '' : 's'} say; this list is the subset
          worth a sentence of its own.
        </p>
      ) : null}
    </div>
  );
}

/**
 * What still names the subject after the revoke.
 *
 * The `null` case is the one this panel exists for. An empty table means the
 * cluster was searched and nothing else grants this; an unread cluster listing
 * means nobody knows, and the two must not look alike.
 */
function ResidualPanel({ residual }) {
  const here = residual?.namespace_bindings ?? [];
  const cluster = residual?.cluster_bindings;
  // Refused and truncated are the same claim at different strengths — nobody
  // knows whether a cluster-wide binding still names them — so they are drawn
  // the same way. A truncated listing keeps the rows it found; an empty table
  // beside this alert says "the first page did not name them", never "nothing
  // else grants this".
  const unsure = cluster === null || residual?.cluster_truncated === true;
  const rows = [
    ...here.map((row) => ({ ...row, scope: 'This namespace' })),
    ...(cluster ?? []).map((row) => ({ ...row, scope: 'Cluster-wide' })),
  ];

  return (
    <>
      {unsure && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="grant-residual-unknown"
          title={
            cluster === null
              ? 'Cluster-wide bindings could not be listed'
              : 'More cluster-wide bindings exist than were read'
          }
        >
          A ClusterRoleBinding grants everywhere, which includes here. Whether
          one still names this subject is unknown — this is not a report that
          none exists.
        </Alert>
      )}
      <div data-testid="grant-residual">
        <DataTable
          ariaLabel="What still grants this subject access"
          tableId="grant-residual"
          rows={rows}
          rowKey={(row) => `${row.scope}/${row.name}`}
          resizableColumns={false}
          emptyTitle={
            unsure
              ? 'Nothing else was found in what could be read'
              : 'Nothing else names this subject'
          }
          emptyDescription={
            unsure
              ? 'The cluster-wide bindings were not fully read, so this is a floor rather than an answer.'
              : 'Both this namespace and the cluster-wide bindings were listed, and no other binding names them.'
          }
          columns={[
            { key: 'scope', title: 'Scope' },
            { key: 'name', title: 'Binding' },
            {
              key: 'role',
              title: 'Role',
              cell: (row) =>
                row.role ? (
                  <span>
                    <span style={MUTED}>{row.role.kind}/</span>
                    {row.role.name}
                  </span>
                ) : (
                  <span style={MUTED}>none</span>
                ),
            },
          ]}
        />
      </div>
    </>
  );
}

export default function GrantRoleDialog({ namespace, onClose, onApplied }) {
  const [operation, setOperation] = useState('grant');
  const [roleKind, setRoleKind] = useState('ClusterRole');
  const [roleName, setRoleName] = useState('');
  const [subjectKind, setSubjectKind] = useState('User');
  const [subjectName, setSubjectName] = useState('');
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState(null);
  const [planning, setPlanning] = useState(false);

  const body = useMemo(
    () => ({
      operation,
      role: { kind: roleKind, name: roleName.trim() },
      subject: { kind: subjectKind, name: subjectName.trim() },
    }),
    [operation, roleKind, roleName, subjectKind, subjectName],
  );

  const ready = Boolean(body.role.name && body.subject.name);

  // The plan is fetched on demand rather than on every keystroke: it is several
  // cluster reads, and one per character typed into a role name would issue a
  // burst of them at the API server for answers about roles nobody asked for.
  const readPlan = useCallback(async () => {
    if (!ready) return;
    setPlanning(true);
    setPlanError(null);
    try {
      setPlan(await accessApi.grantPlan(namespace, body));
    } catch (error) {
      setPlan(null);
      setPlanError(error);
    } finally {
      setPlanning(false);
    }
  }, [namespace, body, ready]);

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);
  // No `extraSignature` here, and that is a statement about the form rather
  // than an omission: **every input handler clears the plan**, so editing any
  // field empties `consequences`, which changes the code signature, which drops
  // the ticks. Ticking "this confers full control" for `cluster-admin` cannot
  // survive the role name being changed to something that happens to raise the
  // same code, because there is no moment where the old plan is still on screen
  // beside the new inputs. Clearing the plan is the load-bearing part — a plan
  // rendered next to fields it was not computed from is the confidently wrong
  // screen this dialog exists to replace — and the acknowledgement reset falls
  // out of it.
  const [acknowledged, setAcknowledged, unacknowledged] = useAcknowledgements(consequences);

  const requestBody = useCallback(
    (dryRun) =>
      accessApi.grant(namespace, {
        ...body,
        dryRun,
        resourceVersion: plan?.resourceVersion ?? null,
        acknowledgeConsequences: acknowledged,
      }),
    [namespace, body, plan, acknowledged],
  );

  let previewDisabledReason = null;
  if (!ready) previewDisabledReason = 'Name a role and a subject.';
  else if (planning) previewDisabledReason = 'Still reading what this role confers…';
  else if (planError) previewDisabledReason = planError.message;
  else if (!plan) previewDisabledReason = 'Read what this role confers before previewing.';
  else if (plan.blocked) previewDisabledReason = plan.blocked.message;
  else if (unacknowledged.length)
    previewDisabledReason = `Acknowledge what this change means: ${unacknowledged
      .map((entry) => entry.label)
      .join('; ')}.`;

  return (
    <MutationDialog
      isOpen
      title={`${operation === 'grant' ? 'Grant' : 'Revoke'} a role in ${namespace}`}
      description="A roleRef is a name. What it confers is in a different object, and this dialog reads it before the write."
      request={requestBody}
      resourceVersion={plan?.resourceVersion ?? null}
      canPreview={!previewDisabledReason}
      previewDisabledReason={previewDisabledReason}
      previewLabel="Preview the change"
      confirmLabel={operation === 'grant' ? 'Grant the role' : 'Revoke the role'}
      isDanger={operation === 'revoke' || consequences.length > 0}
      autoPreview={false}
      confirmBlockedReason={blockedByConsequences(acknowledged)}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      summarize={(result) => {
        if (result?.applied !== true) {
          return {
            variant: 'warning',
            title: 'The write completed without confirming it was applied',
            body: 'Re-read the binding before assuming anything changed.',
          };
        }
        if (result.operation === 'revoke') {
          const here = result.residual?.namespace_bindings ?? [];
          const cluster = result.residual?.cluster_bindings;
          const remaining = here.length + (cluster?.length ?? 0);
          if (remaining > 0 || cluster === null) {
            return {
              variant: 'warning',
              title: 'The binding was changed — their access may not be gone',
              body:
                cluster === null
                  ? 'The subject was removed from this binding. Cluster-wide bindings could not be listed, so whether they still have access here is unknown. Ask the API server with a subject review.'
                  : `The subject was removed from this binding, and ${remaining} other binding(s) still name them. That is not a revoked permission — check with a subject review.`,
            };
          }
          return {
            variant: 'success',
            title: 'The subject was removed from this binding',
            body: 'No other binding that could be read names them. A subject review is still the authoritative answer, because it asks the API server rather than subtracting objects.',
          };
        }
        return {
          variant: 'success',
          title: 'The binding now names this subject',
          body: 'RBAC changes take effect on the next request the API server authorizes; there is nothing to restart.',
        };
      }}
    >
      <>
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="grant-disabled"
            title="This console runs read-only"
          >
            {plan.gate.detail}
          </Alert>
        )}

        <Form isHorizontal>
          <FormGroup label="Operation" fieldId="grant-operation" isStack role="radiogroup">
            <Radio
              id="grant-operation-grant"
              name="grant-operation"
              label="Grant"
              data-testid="grant-operation-grant"
              isChecked={operation === 'grant'}
              onChange={() => {
                setOperation('grant');
                setPlan(null);
              }}
            />
            <Radio
              id="grant-operation-revoke"
              name="grant-operation"
              label="Revoke"
              data-testid="grant-operation-revoke"
              isChecked={operation === 'revoke'}
              onChange={() => {
                setOperation('revoke');
                setPlan(null);
              }}
            />
          </FormGroup>

          <FormGroup label="Role kind" fieldId="grant-role-kind" isStack role="radiogroup">
            {ROLE_KINDS.map((kind) => (
              <Radio
                key={kind}
                id={`grant-role-kind-${kind}`}
                name="grant-role-kind"
                label={kind}
                data-testid={`grant-role-kind-${kind}`}
                isChecked={roleKind === kind}
                onChange={() => {
                  setRoleKind(kind);
                  setPlan(null);
                }}
              />
            ))}
          </FormGroup>

          <FormGroup label="Role name" fieldId="grant-role-name">
            <TextInput
              id="grant-role-name"
              data-testid="grant-role-name"
              value={roleName}
              onChange={(_e, value) => {
                setRoleName(value);
                setPlan(null);
              }}
              aria-label="Role name"
            />
          </FormGroup>

          <FormGroup label="Subject kind" fieldId="grant-subject-kind" isStack role="radiogroup">
            {SUBJECT_KINDS.map((kind) => (
              <Radio
                key={kind}
                id={`grant-subject-kind-${kind}`}
                name="grant-subject-kind"
                label={kind}
                data-testid={`grant-subject-kind-${kind}`}
                isChecked={subjectKind === kind}
                onChange={() => {
                  setSubjectKind(kind);
                  setPlan(null);
                }}
              />
            ))}
          </FormGroup>

          <FormGroup label="Subject name" fieldId="grant-subject-name">
            <TextInput
              id="grant-subject-name"
              data-testid="grant-subject-name"
              value={subjectName}
              onChange={(_e, value) => {
                setSubjectName(value);
                setPlan(null);
              }}
              aria-label="Subject name"
            />
          </FormGroup>
        </Form>

        <button
          type="button"
          className="pf-v6-c-button pf-m-secondary"
          data-testid="grant-read-role"
          disabled={!ready || planning}
          onClick={readPlan}
        >
          {planning ? 'Reading…' : 'Read what this would do'}
        </button>

        {planError && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="grant-plan-error"
            title="This change could not be planned"
          >
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        {plan?.blocked && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="grant-blocked"
            title="This change cannot be made as asked"
          >
            {plan.blocked.message} {plan.blocked.hint}
          </Alert>
        )}

        {plan && (
          <>
            <SectionHeader
              title="What this role confers"
              description="A RoleBinding to a ClusterRole grants that role's rules inside this namespace only — it is not a cluster-wide grant, and it is not weaker than the ClusterRole."
            />
            <CapabilityPanel capability={plan.capability} />

            {plan.operation === 'revoke' && (
              <>
                <SectionHeader
                  title="What would still grant this subject access"
                  description="Removing a subject from one binding is not revoking their access. This is what else names them."
                />
                <ResidualPanel residual={plan.residual} />
              </>
            )}

            <ConsequenceChecklist
              consequences={consequences}
              acknowledged={acknowledged}
              onChange={setAcknowledged}
              idPrefix="grant"
              title={
                consequences.length === 1
                  ? 'One thing about this change needs acknowledging'
                  : `${consequences.length} things about this change need acknowledging`
              }
            />
          </>
        )}
      </>
    </MutationDialog>
  );
}
