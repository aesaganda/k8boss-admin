/**
 * SubjectReviewPanel — §23, the "what can this subject do" tab.
 *
 * §9's preflight asks whether *the console* may act; that is what every button
 * needs and it is not the question an administrator has. This asks the API
 * server's own authorization chain about a named user or ServiceAccount, and it
 * takes no action as them — which is why it exists while
 * `docs/adr-0007-impersonation.md` stays proposed.
 *
 * **The banner above the results is not decoration.** A subject's access mostly
 * arrives through their groups, and the API server only considers the groups in
 * the review. For a ServiceAccount those are deterministic and the backend
 * supplies them. For a User they are not knowable here, so every answer is
 * conditional on the groups typed in — and a reader who misses that turns "a
 * user named alice in these groups cannot do this" into "alice cannot do this",
 * about somebody who is an administrator through a group nobody listed.
 *
 * **Three outcomes, not two.** Allowed; explicitly denied by an authorizer;
 * nothing granted it. Plus a fourth that is not an outcome at all — the
 * authorizer could not decide — which renders as unknown and never as "may not".
 */
import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  TextInput,
} from '@patternfly/react-core';
import { DataTable, NullableCell, SectionHeader, StatusBadge } from './ui';
import { access as accessApi } from '../api/client';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** The verbs worth asking about by default — RBAC's own set. */
const VERBS = ['get', 'list', 'watch', 'create', 'update', 'patch', 'delete'];

/** A starting set that shows the shape without pretending to be exhaustive. */
const DEFAULT_CHECKS = [
  { verb: 'get', group: 'core', resource: 'pods' },
  { verb: 'delete', group: 'core', resource: 'pods' },
  { verb: 'get', group: 'core', resource: 'secrets' },
  { verb: 'patch', group: 'apps', resource: 'deployments' },
];

function outcomeCell(row) {
  // Order matters: an undecided review must never fall through to a denial.
  if (row.evaluationError) {
    return (
      <NullableCell
        value={null}
        reason={`The authorizer could not decide: ${row.evaluationError}. This is not a denial — it is a question that was not answered.`}
      />
    );
  }
  if (row.allowed) {
    return <StatusBadge status="Ready" label="Allowed" tooltip={row.reason || undefined} />;
  }
  if (row.denied) {
    return (
      <StatusBadge
        status="NotReady"
        label="Denied"
        tooltip={
          row.reason ||
          'An authorizer explicitly refused this, which is not the same as nothing having granted it.'
        }
      />
    );
  }
  return (
    <StatusBadge
      status="Unknown"
      label="Not granted"
      tooltip={
        row.reason ||
        'No authorizer granted this. That is the ordinary "no" — distinct from an authorizer explicitly denying it.'
      }
    />
  );
}

export default function SubjectReviewPanel() {
  const [kind, setKind] = useState('User');
  const [name, setName] = useState('');
  const [namespace, setNamespace] = useState('');
  const [groups, setGroups] = useState('');
  const [checks, setChecks] = useState(DEFAULT_CHECKS);
  const [answer, setAnswer] = useState(null);
  const [error, setError] = useState(null);
  const [running, setRunning] = useState(false);

  const subject = useMemo(
    () => ({
      kind,
      name: name.trim(),
      namespace: kind === 'ServiceAccount' ? namespace.trim() : null,
      groups: groups
        .split(',')
        .map((g) => g.trim())
        .filter(Boolean),
    }),
    [kind, name, namespace, groups],
  );

  const ready =
    Boolean(subject.name) && (kind !== 'ServiceAccount' || Boolean(subject.namespace));

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      setAnswer(await accessApi.subjectReview(subject, checks));
    } catch (e) {
      // The answer is cleared rather than left stale: a results table under a
      // failed request is a table about a different question.
      setAnswer(null);
      setError(e);
    } finally {
      setRunning(false);
    }
  }, [subject, checks]);

  const columns = useMemo(
    () => [
      { key: 'verb', title: 'Verb', sortable: true },
      {
        key: 'resource',
        title: 'Resource',
        sortable: true,
        value: (row) => `${row.group}/${row.resource}`,
        cell: (row) => (
          <span>
            {row.group === 'core' ? '' : `${row.group}/`}
            {row.resource}
            {row.subresource ? `/${row.subresource}` : ''}
          </span>
        ),
      },
      {
        key: 'namespace',
        title: 'Namespace',
        cell: (row) =>
          row.namespace ? (
            row.namespace
          ) : (
            // Not the same as "all namespaces": a cluster-wide check on an
            // identity scoped to one namespace correctly answers no.
            <span style={MUTED}>cluster-wide</span>
          ),
      },
      {
        key: 'outcome',
        title: 'Outcome',
        sortable: true,
        value: (row) =>
          row.evaluationError ? 'unknown' : row.allowed ? 'allowed' : row.denied ? 'denied' : 'no',
        cell: outcomeCell,
      },
      {
        key: 'reason',
        title: 'Reason',
        cell: (row) =>
          row.reason ? (
            <span style={MUTED}>{row.reason}</span>
          ) : (
            <NullableCell
              value={null}
              reason="The authorizer returned no reason. Most RBAC-only clusters do not."
            />
          ),
      },
    ],
    [],
  );

  return (
    <>
      <SectionHeader
        title="What can this subject do?"
        description="Asked of the API server's own authorization chain — RBAC, the node authorizer, any webhook authorizer — so the answer covers what reading role bindings cannot. Nothing is done as the subject; this is a question about them."
      />

      <Form onSubmit={(event) => event.preventDefault()} data-testid="sar-form">
        <Grid hasGutter>
          <GridItem span={3}>
            <FormGroup label="Kind" fieldId="sar-kind">
              <FormSelect
                id="sar-kind"
                value={kind}
                onChange={(_e, value) => setKind(value)}
                data-testid="sar-kind"
              >
                <FormSelectOption value="User" label="User" />
                <FormSelectOption value="ServiceAccount" label="ServiceAccount" />
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem span={kind === 'ServiceAccount' ? 4 : 5}>
            <FormGroup label="Name" fieldId="sar-name" isRequired>
              <TextInput
                id="sar-name"
                value={name}
                onChange={(_e, value) => setName(value)}
                aria-label="Subject name"
                data-testid="sar-name"
              />
            </FormGroup>
          </GridItem>
          {kind === 'ServiceAccount' && (
            <GridItem span={4}>
              <FormGroup label="Namespace" fieldId="sar-namespace" isRequired>
                <TextInput
                  id="sar-namespace"
                  value={namespace}
                  onChange={(_e, value) => setNamespace(value)}
                  aria-label="ServiceAccount namespace"
                  data-testid="sar-namespace"
                />
              </FormGroup>
            </GridItem>
          )}
        </Grid>

        <FormGroup
          label="Groups"
          fieldId="sar-groups"
          labelHelp={undefined}
        >
          <TextInput
            id="sar-groups"
            value={groups}
            onChange={(_e, value) => setGroups(value)}
            placeholder="comma-separated, e.g. platform-admins, oncall"
            aria-label="Groups"
            data-testid="sar-groups"
            isDisabled={kind === 'ServiceAccount'}
          />
          <p style={{ ...MUTED, marginBlockStart: '0.25rem' }}>
            {kind === 'ServiceAccount'
              ? "A ServiceAccount's groups are assigned by the API server and filled in for you, so this answer is complete."
              : 'The API server only considers the groups named here. Access mostly arrives through groups, so leaving this empty asks about a user with no memberships — which is rarely the person you mean.'}
          </p>
        </FormGroup>

        <Button
          variant="primary"
          isDisabled={!ready || running}
          isLoading={running}
          onClick={run}
          data-testid="sar-run"
        >
          {running ? 'Asking…' : 'Ask'}
        </Button>
      </Form>

      {error && (
        <Alert
          isInline
          variant="danger"
          className="admin-confirm__alert"
          data-testid="sar-error"
          title="The review could not be run"
        >
          {error.message}
          {error.hint ? ` ${error.hint}` : ''}
        </Alert>
      )}

      {answer && (
        <>
          {answer.subject.groups_complete === false ? (
            <Alert
              isInline
              variant="warning"
              className="admin-confirm__alert"
              data-testid="sar-groups-incomplete"
              title="This answer is only about the groups listed"
            >
              {answer.subject.groups_detail} Every row below should be read as
              &ldquo;a user named <strong>{answer.subject.name}</strong>, in exactly these groups
              &rdquo; — not as what this person can do.
              <div style={{ marginBlockStart: '0.5rem' }}>
                Reviewed with: <code>{answer.subject.groups.join(', ')}</code>
              </div>
            </Alert>
          ) : (
            <Alert
              isInline
              variant="info"
              className="admin-confirm__alert"
              data-testid="sar-groups-complete"
              title="This answer is complete"
            >
              {answer.subject.groups_detail}
              <div style={{ marginBlockStart: '0.5rem' }}>
                Reviewed as <code>{answer.subject.username}</code> with{' '}
                <code>{answer.subject.groups.join(', ')}</code>
              </div>
            </Alert>
          )}

          {answer.undecided > 0 && (
            <Alert
              isInline
              variant="warning"
              className="admin-confirm__alert"
              data-testid="sar-undecided"
              title={`${answer.undecided} question${answer.undecided === 1 ? '' : 's'} the authorizer could not answer`}
            >
              Those rows are unknown, not refusals. Reading them as denials sends somebody to
              grant a permission that may already be there.
            </Alert>
          )}

          <DataTable
            columns={columns}
            rows={answer.results}
            rowKey={(row) =>
              `${row.verb}:${row.group}:${row.resource}:${row.namespace ?? ''}:${row.subresource ?? ''}`
            }
            ariaLabel="Access review results"
            emptyTitle="No checks"
          />

          <p style={MUTED} data-testid="sar-audit">
            This review was recorded in the audit trail
            {answer.auditId ? ` as record ${answer.auditId}` : ' — the record could not be written'}.
          </p>
        </>
      )}
    </>
  );
}

export { DEFAULT_CHECKS, VERBS };
