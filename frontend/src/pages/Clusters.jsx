/**
 * Clusters — registration, connection test, and the baseline permission matrix
 * (§3, §9).
 *
 * The page exists to answer one question at the moment it is cheapest to
 * answer: *will this ServiceAccount actually work?* §3's `POST /{id}/test`
 * returns the baseline preflight set alongside the connection result precisely
 * so a half-permissioned registration is visible here rather than as a 403 on
 * the Workloads page during an incident. This page renders that set as a
 * per-verb grid rather than as a single "connected" tick, because "the token is
 * valid" and "the token can do the job" are different facts and only the first
 * one is easy.
 *
 * Three things it is careful about.
 *
 * **`permissions: null` is not "denied everything".** An unreachable cluster
 * returns `permissions: null` (the backend's own comment: *an empty list would
 * say the ServiceAccount holds none of the baseline permissions, which is a
 * claim about RBAC we never got close enough to make*). Rendering that as a
 * grid of red crosses would send an operator to rewrite a ClusterRole when
 * their real problem is DNS. It renders as "not checked", with the connection
 * error stated.
 *
 * **A failed review is a third state.** §9: `allowed: false` with a non-null
 * `evaluationError` means the SelfSubjectAccessReview itself failed — *we do
 * not know* whether the caller may act. The contract requires the UI to render
 * that differently from a clean denial, and this grid does: grey "unknown",
 * never a red "no". Conflating them tells an operator they lack a permission
 * they may well hold.
 *
 * **Credentials go in and never come back.** `ClusterPublic` has no token
 * field, so the edit form's token box starts empty and an empty box means
 * "keep the stored one" (§3's PUT is partial and uses `exclude_unset` for
 * exactly this). The form never displays, echoes or round-trips a secret.
 *
 * Registration CRUD deliberately does **not** go through `MutationDialog`.
 * That component implements contract rule 11.3 — the dry-run-then-diff
 * handshake against a *cluster*. Registering a cluster writes a row in the
 * console's own database; there is no `dryRun`, no API server projection and no
 * diff to show, and forcing it through the mutation flow would mean inventing
 * one. Deleting a registration still asks for the name to be typed, because
 * that is a speed bump rather than a diff.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Split,
  SplitItem,
  TextArea,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';

import { clusters as clustersApi } from '../api/client';
import {
  ConfirmDialog,
  DataTable,
  DescriptionList,
  ErrorState,
  PageHeader,
  SectionHeader,
  StatusBadge,
  menuAction,
} from '../components/ui';
import { useAuth } from '../contexts/AuthContext';
import { useCluster } from '../contexts/ClusterContext';
import { useNotify } from '../contexts/NotificationContext';
import { formatTimestamp } from '../utils/format';

const AUTH_TYPES = [
  { value: 'service_account_token', label: 'ServiceAccount token' },
  { value: 'bearer_token', label: 'Bearer token' },
];

const EMPTY_FORM = {
  name: '',
  platform: 'kubernetes',
  api_server: '',
  authentication_type: 'service_account_token',
  token: '',
  ca_certificate: '',
  skip_tls_verify: false,
  impersonation_enabled: false,
  app_domain: '',
};

/**
 * The three states of a §9 PreflightResult, and the reason there are three.
 *
 * `allowed: false, evaluationError: null`   → a clean denial. Red.
 * `allowed: false, evaluationError: "..."`  → the review failed. Grey. We do
 *                                             not know, and saying "no" here is
 *                                             a claim the response cannot
 *                                             support.
 * `allowed: true`                           → green.
 */
function preflightState(result) {
  if (result?.evaluationError) {
    return {
      status: 'unknown',
      label: 'Unknown',
      tooltip:
        `The access review itself failed (${result.evaluationError}), so whether this account may perform ` +
        'this action was never determined. This is not a denial.',
    };
  }
  if (result?.allowed) return { status: 'allowed', label: 'Allowed', tooltip: null };
  return {
    status: 'denied',
    label: 'Denied',
    tooltip: result?.hint || result?.reason || 'No RBAC policy matched this request.',
  };
}

function targetLabel(result) {
  const group = result?.group || 'core';
  const resource = result?.resource ?? '';
  const sub = result?.subresource ? `/${result.subresource}` : '';
  return `${group}/${resource}${sub}`;
}

/* ── Registration form ──────────────────────────────────────────────────── */

function ClusterFormModal({ isOpen, editing, onClose, onSaved }) {
  const { notify } = useNotify();
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setSaving(false);
    setForm(
      editing
        ? {
            name: editing.name ?? '',
            platform: editing.platform ?? 'kubernetes',
            api_server: editing.api_server ?? '',
            authentication_type: editing.authentication_type ?? 'service_account_token',
            // Always blank. §3's ClusterPublic carries no credential material,
            // so there is nothing to prefill even if we wanted to — and an
            // asterisk placeholder that round-tripped as a literal value is
            // how a working cluster registration gets its token replaced with
            // "********".
            token: '',
            // Blank for the same reason as the token: `has_ca_certificate` is a
            // boolean, not the certificate, so there is nothing to prefill.
            ca_certificate: '',
            skip_tls_verify: Boolean(editing.skip_tls_verify),
            // ADR-0007, and it prefills for real like `app_domain`: it is not a
            // credential, so ClusterPublic carries it. A setting that decides
            // who the cluster thinks is asking must be visible in the form that
            // edits it, not merely in effect.
            impersonation_enabled: Boolean(editing.impersonation_enabled),
            // Unlike the token and the CA, this one is not a credential, so
            // ClusterPublic carries it and it prefills for real. Blanking the
            // box and saving clears it — the backend reads "" as "generate no
            // hostnames" rather than as "field omitted".
            app_domain: editing.app_domain ?? '',
          }
        : EMPTY_FORM,
    );
  }, [isOpen, editing]);

  const set = (key) => (value) => setForm((f) => ({ ...f, [key]: value }));

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      if (editing) {
        // Partial by construction: only fields the operator actually filled in
        // are sent, so an untouched token box keeps the stored credential.
        const body = {
          name: form.name,
          platform: form.platform,
          api_server: form.api_server,
          authentication_type: form.authentication_type,
          skip_tls_verify: form.skip_tls_verify,
          impersonation_enabled: form.impersonation_enabled,
          // Always sent, including empty: "" is how the operator clears a
          // domain they got wrong, and omitting it would make a wrong domain
          // unremovable through this form.
          app_domain: form.app_domain.trim(),
        };
        if (form.token.trim()) body.token = form.token;
        if (form.ca_certificate.trim()) body.ca_certificate = form.ca_certificate;
        await clustersApi.update(editing.id, body);
        notify(`Updated ${form.name}`, 'success');
      } else {
        await clustersApi.create({
          ...form,
          ca_certificate: form.ca_certificate.trim() || null,
        });
        notify(`Registered ${form.name}`, 'success');
      }
      onSaved();
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  if (!isOpen) return null;

  const missingToken = !editing && !form.token.trim();
  const canSubmit = form.name.trim() && form.api_server.trim() && !missingToken;

  return (
    <Modal isOpen variant="medium" onClose={onClose} aria-label="Cluster registration">
      <ModalHeader title={editing ? `Edit ${editing.name}` : 'Register a cluster'} />
      <ModalBody>
        {error && (
          <Alert isInline variant="danger" title="The registration could not be saved">
            <p>{error.message}</p>
            {error.hint && <p style={{ fontWeight: 600 }}>{error.hint}</p>}
          </Alert>
        )}
        <Form>
          <FormGroup label="Name" fieldId="cluster-name" isRequired>
            <TextInput
              id="cluster-name"
              value={form.name}
              onChange={(_e, v) => set('name')(v)}
              aria-label="Cluster name"
              data-testid="cluster-name"
            />
          </FormGroup>

          <FormGroup label="API server" fieldId="cluster-api-server" isRequired>
            <TextInput
              id="cluster-api-server"
              value={form.api_server}
              onChange={(_e, v) => set('api_server')(v)}
              placeholder="https://api.prod-eu.example:6443"
              aria-label="API server URL"
              data-testid="cluster-api-server"
            />
          </FormGroup>

          <FormGroup label="Authentication" fieldId="cluster-auth-type">
            <FormSelect
              id="cluster-auth-type"
              value={form.authentication_type}
              onChange={(_e, v) => set('authentication_type')(v)}
              aria-label="Authentication type"
            >
              {AUTH_TYPES.map((t) => (
                <FormSelectOption key={t.value} value={t.value} label={t.label} />
              ))}
            </FormSelect>
          </FormGroup>

          <FormGroup label="Token" fieldId="cluster-token" isRequired={!editing}>
            <TextArea
              id="cluster-token"
              value={form.token}
              onChange={(_e, v) => set('token')(v)}
              rows={3}
              aria-label="Bearer token"
              autoComplete="off"
              spellCheck={false}
              data-testid="cluster-token"
            />
            <p style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem', marginBlockStart: '0.25rem' }}>
              {editing
                ? 'Stored encrypted and never returned by the API, so this box starts empty. Leave it empty to keep the token already stored; type a new one to replace it.'
                : 'Stored encrypted. It is never included in any response from this console.'}
            </p>
          </FormGroup>

          <FormGroup label="CA certificate (PEM)" fieldId="cluster-ca">
            <TextArea
              id="cluster-ca"
              value={form.ca_certificate}
              onChange={(_e, v) => set('ca_certificate')(v)}
              rows={4}
              aria-label="CA certificate"
              spellCheck={false}
              placeholder="-----BEGIN CERTIFICATE-----"
            />
            {editing?.has_ca_certificate && (
              <p style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem', marginBlockStart: '0.25rem' }}>
                A CA certificate is already stored. Leave this empty to keep it.
              </p>
            )}
          </FormGroup>

          <FormGroup label="App domain" fieldId="cluster-app-domain">
            <TextInput
              id="cluster-app-domain"
              value={form.app_domain}
              placeholder="apps.example.com"
              onChange={(_e, v) => set('app_domain')(v)}
              data-testid="cluster-app-domain"
            />
            <FormHelperText>
              <HelperText>
                <HelperTextItem>
                  The cluster&apos;s wildcard DNS domain. With one set, exposing a Service
                  offers <code>&lt;name&gt;-&lt;namespace&gt;.{form.app_domain.trim() || 'apps.example.com'}</code> as
                  the hostname instead of asking you to type it. Leave it empty if this
                  cluster has no wildcard record — a generated hostname nothing resolves is
                  worse than a blank field, because the blank field asks the question and
                  the hostname answers it wrongly.
                </HelperTextItem>
              </HelperText>
            </FormHelperText>
          </FormGroup>

          <FormGroup fieldId="cluster-skip-tls">
            <Checkbox
              id="cluster-skip-tls"
              label="Skip TLS verification"
              description="Every request to this cluster becomes vulnerable to interception, and the token in the request with it. Only for a cluster with a self-signed certificate you cannot supply."
              isChecked={form.skip_tls_verify}
              onChange={(_e, checked) => set('skip_tls_verify')(checked)}
              data-testid="cluster-skip-tls"
            />
          </FormGroup>

          {form.skip_tls_verify && (
            <Alert isInline variant="warning" title="TLS verification off">
              The console will accept any certificate this endpoint presents, including one substituted by
              something in the network path — which then holds a token with the permissions below.
            </Alert>
          )}

          <FormGroup fieldId="cluster-impersonation">
            <Checkbox
              id="cluster-impersonation"
              label="Act as the signed-in operator"
              description="Every call to this cluster carries Impersonate-User, so the API server checks permissions, runs admission and writes its own audit log as the person using the console rather than as the console."
              isChecked={form.impersonation_enabled}
              onChange={(_e, checked) => set('impersonation_enabled')(checked)}
              data-testid="cluster-impersonation"
            />
          </FormGroup>

          {/* Not a warning: this is the *safer* setting, and drawing it in the
              same colour as "skip TLS verification" would teach operators that
              both are risks to be avoided. What it needs is a grant and a
              matching issuer, and getting either wrong locks people out of a
              cluster that was working — so the two conditions are stated here,
              at the checkbox, rather than in a document nobody opens while
              filling in a form. */}
          {form.impersonation_enabled && (
            <Alert
              isInline
              variant="info"
              title="This needs a grant and a shared issuer"
              data-testid="cluster-impersonation-note"
            >
              <p>
                The console&apos;s ServiceAccount needs <code>impersonate</code> on{' '}
                <code>users</code> and <code>groups</code>.{' '}
                <strong>Grant it with <code>resourceNames</code></strong>: an
                unrestricted <code>impersonate users</code> can impersonate the most
                powerful user on the cluster, which is cluster-admin by proxy.
              </p>
              <p>
                Only single sign-on sessions can act as an operator, and the name sent
                is the one <em>your identity provider</em> states. If this cluster
                authenticates against a different issuer — or none — it will not know
                that name, and every request will be refused as that person rather
                than served as the console.
              </p>
            </Alert>
          )}
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={submit} isDisabled={!canSubmit || saving} isLoading={saving}>
          {editing ? 'Save' : 'Register'}
        </Button>
        <Button variant="link" onClick={onClose}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}

/* ── The permission matrix ──────────────────────────────────────────────── */

function PermissionMatrix({ test }) {
  const columns = useMemo(
    () => [
      { key: 'verb', title: 'Verb', sortable: true, width: 15 },
      { key: 'target', title: 'Resource', sortable: true, value: (row) => targetLabel(row), cell: (row) => <code>{targetLabel(row)}</code> },
      {
        key: 'state',
        title: 'Result',
        sortable: true,
        value: (row) => preflightState(row).label,
        cell: (row) => {
          const state = preflightState(row);
          return <StatusBadge status={state.status} label={state.label} tooltip={state.tooltip || undefined} />;
        },
      },
      {
        key: 'why',
        title: 'Why',
        modifier: 'breakWord',
        cell: (row) => {
          const state = preflightState(row);
          if (state.status === 'allowed') return null;
          return (
            <span style={{ fontSize: '0.875rem' }}>
              {row.evaluationError || row.reason || 'No RBAC policy matched.'}
              {row.hint && <div style={{ fontWeight: 600, marginBlockStart: '0.25rem' }}>{row.hint}</div>}
            </span>
          );
        },
      },
    ],
    [],
  );

  if (!test) return null;

  // The load-bearing branch. `null` permissions means the test never got far
  // enough to ask, and a grid of denials here would be an invented answer.
  if (test.permissions == null) {
    return (
      <Alert
        isInline
        variant="warning"
        title="Permissions were not checked"
        data-testid="permissions-not-checked"
      >
        <p>
          {test.reachable
            ? 'The cluster answered but the baseline access review was not returned.'
            : 'The cluster could not be reached, so no access review was performed.'}{' '}
          This is not a statement that the ServiceAccount lacks these permissions — nothing was asked.
        </p>
        {test.error && (
          <>
            <p>{test.error.message}</p>
            {test.error.hint && <p style={{ fontWeight: 600 }}>{test.error.hint}</p>}
          </>
        )}
      </Alert>
    );
  }

  const denied = test.permissions.filter((p) => !p.allowed && !p.evaluationError);
  const unknown = test.permissions.filter((p) => p.evaluationError);

  return (
    <div data-testid="permission-matrix">
      <SectionHeader
        title="Baseline permissions"
        description={
          `${test.permissions.length - denied.length - unknown.length} allowed · ${denied.length} denied · ` +
          `${unknown.length} could not be determined`
        }
      />
      {denied.length > 0 && (
        <Alert
          isInline
          variant="warning"
          title={`${denied.length} of the console's baseline permissions ${denied.length === 1 ? 'is' : 'are'} missing`}
        >
          This cluster registers and connects. The pages and buttons that need the permissions below will
          not work, and each will say so where it appears — but it is cheaper to know now.
        </Alert>
      )}
      {unknown.length > 0 && (
        <Alert
          isInline
          variant="info"
          title={`${unknown.length} access ${unknown.length === 1 ? 'review' : 'reviews'} could not be evaluated`}
        >
          The API server accepted the review and then failed to answer it. These are not denials: whether
          this account holds those permissions is unknown.
        </Alert>
      )}
      <DataTable
        columns={columns}
        rows={test.permissions}
        rowKey={(row) => `${row.verb}:${targetLabel(row)}`}
        ariaLabel="Baseline permission matrix"
      />
    </div>
  );
}

/* ── Page ───────────────────────────────────────────────────────────────── */

export default function Clusters() {
  const { clusters, activeClusterId, setActiveClusterId, loading, error, refresh } = useCluster();
  const { enabled: authEnabled, user } = useAuth();
  const { notify } = useNotify();

  /**
   * §3's three writes are administrator-only, and this is the mirror of that.
   *
   * `require_console_admin` gates create, update and delete when the console
   * authenticates operators, and returns `None` when it does not — in legacy
   * proxy mode there is no console role to check and the proxy in front owns
   * the decision. So the gate is `!authEnabled || admin`, in that order:
   * reading the role first would disable registration entirely on a deployment
   * that never had one, which is a supported deployment made unusable by a
   * check meant to protect a different one.
   *
   * Disabled rather than hidden, per rule 11.4. A control that vanishes reads
   * as a missing feature and becomes a support ticket; a disabled one carrying
   * its reason answers the question where it was asked. Offering it enabled,
   * which is what this page did, spends a click to arrive at a 403 toast that
   * says nothing this sentence could not have said first.
   */
  const adminGate = useMemo(
    () =>
      !authEnabled || user?.role === 'admin'
        ? { allowed: true, reason: null }
        : {
            allowed: false,
            reason:
              'Registering, editing and de-registering a cluster is administrator-only on this ' +
              'console. This account holds the user role; ask an administrator to make the change.',
          },
    [authEnabled, user],
  );

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [removing, setRemoving] = useState(false);
  const [selected, setSelected] = useState(null);
  const [tests, setTests] = useState({});
  const [testing, setTesting] = useState({});

  const runTest = useCallback(
    async (cluster) => {
      setTesting((t) => ({ ...t, [cluster.id]: true }));
      try {
        const result = await clustersApi.test(cluster.id);
        setTests((t) => ({ ...t, [cluster.id]: result }));
        setSelected(cluster.id);
        notify(
          result.reachable
            ? `${cluster.name} answered in ${result.latency_ms} ms`
            : `${cluster.name} could not be reached`,
          result.reachable ? 'success' : 'danger',
        );
      } catch (err) {
        // §3 returns 200 for an unreachable cluster — a non-2xx here is the
        // console failing, not the cluster, and it is stored as such rather
        // than rendered in the "cluster unreachable" shape.
        setTests((t) => ({
          ...t,
          [cluster.id]: {
            reachable: false,
            server_version: null,
            latency_ms: null,
            permissions: null,
            error: { message: err.message, hint: err.hint, error: err.code },
          },
        }));
        setSelected(cluster.id);
      } finally {
        setTesting((t) => ({ ...t, [cluster.id]: false }));
        refresh({ silent: true });
      }
    },
    [notify, refresh],
  );

  const remove = useCallback(async () => {
    if (!deleting) return;
    setRemoving(true);
    try {
      await clustersApi.remove(deleting.id);
      notify(`De-registered ${deleting.name}`, 'success');
      if (activeClusterId === deleting.id) setActiveClusterId(null);
      setDeleting(null);
      refresh();
    } catch (err) {
      notify(`Could not de-register ${deleting.name}: ${err.message}`, 'danger', { sticky: true });
    } finally {
      setRemoving(false);
    }
  }, [deleting, activeClusterId, setActiveClusterId, notify, refresh]);

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Name',
        sortable: true,
        cell: (row) => (
          <span>
            {row.name}
            {row.id === activeClusterId && (
              <>
                {' '}
                <StatusBadge status="active" label="Active" />
              </>
            )}
          </span>
        ),
      },
      { key: 'api_server', title: 'API server', sortable: true, modifier: 'breakWord' },
      {
        key: 'status',
        title: 'Status',
        sortable: true,
        // §3's ClusterPublic carries `status` but not the reason behind it —
        // `Cluster.status_detail` exists in the model and is not in the
        // allowlist. So the pill can say "disconnected" and not why; the
        // connection test below is the only place that reports the cause.
        cell: (row) => <StatusBadge status={row.status} />,
      },
      {
        key: 'server_version',
        title: 'Version',
        sortable: true,
        cell: (row) =>
          row.server_version || (
            <Tooltip content="The console has not successfully read this cluster's version.">
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>—</span>
            </Tooltip>
          ),
      },
      {
        key: 'tls',
        title: 'TLS',
        cell: (row) =>
          row.skip_tls_verify ? (
            <StatusBadge status="warning" label="Unverified" tooltip="skip_tls_verify is set for this cluster." />
          ) : (
            <StatusBadge status="true" label={row.has_ca_certificate ? 'Custom CA' : 'Verified'} />
          ),
      },
      {
        key: 'last_connected',
        title: 'Last connected',
        sortable: true,
        cell: (row) =>
          formatTimestamp(row.last_connected) || (
            <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>never</span>
          ),
      },
    ],
    [activeClusterId],
  );

  const detail = clusters.find((c) => c.id === selected) ?? null;

  // Not `ActionButton`: that one names its own `data-testid`, and this button's
  // is what the impersonation suite drives the registration form from. Same
  // shape otherwise — `isAriaDisabled` keeps it focusable so the tooltip is
  // reachable, and the span gives Tooltip a node to attach its ref to.
  const registerButton = (
    <Button
      variant="primary"
      isAriaDisabled={!adminGate.allowed}
      onClick={
        adminGate.allowed
          ? () => {
              setEditing(null);
              setFormOpen(true);
            }
          : undefined
      }
      data-testid="cluster-add"
    >
      Register a cluster
    </Button>
  );

  return (
    <>
      <PageHeader
        title="Clusters"
        subtitle="Registered API servers. Credentials are stored encrypted and are never returned by this console."
        actions={
          adminGate.allowed ? (
            registerButton
          ) : (
            <Tooltip content={adminGate.reason}>
              <span className="admin-gated-action">{registerButton}</span>
            </Tooltip>
          )
        }
      />

      {error && (
        <ErrorState
          title="The cluster list could not be refreshed"
          error={error}
          onRetry={() => refresh()}
        >
          <p>What is shown below is the last list this console successfully read.</p>
        </ErrorState>
      )}

      <DataTable
        columns={columns}
        rows={clusters}
        rowKey="id"
        loading={loading}
        ariaLabel="Registered clusters"
        emptyTitle="No clusters registered"
        emptyDescription="Register an API server endpoint and a ServiceAccount token to begin."
        onRowClick={(row) => setSelected(row.id)}
        actions={(row) => [
          {
            title: 'Test connection',
            onClick: () => runTest(row),
            isDisabled: Boolean(testing[row.id]),
          },
          { title: row.id === activeClusterId ? 'Already active' : 'Make active', onClick: () => setActiveClusterId(row.id), isDisabled: row.id === activeClusterId },
          // Test connection and Make active stay ungated: neither is one of
          // §3's administrator-only writes — the first is a read, the second
          // only changes which cluster this browser is looking at.
          menuAction('Edit', adminGate, () => {
            setEditing(row);
            setFormOpen(true);
          }),
          menuAction('De-register', adminGate, () => setDeleting(row), { isDanger: true }),
        ]}
      />

      {detail && (
        <Card style={{ marginBlockStart: 'var(--admin-gap-lg, 1.5rem)' }}>
          <CardBody>
            <Split hasGutter style={{ alignItems: 'center' }}>
              <SplitItem isFilled>
                <SectionHeader title={detail.name} description={detail.api_server} />
              </SplitItem>
              <SplitItem>
                <Button
                  variant="secondary"
                  onClick={() => runTest(detail)}
                  isLoading={Boolean(testing[detail.id])}
                  isDisabled={Boolean(testing[detail.id])}
                  data-testid="cluster-test"
                >
                  Test connection and permissions
                </Button>
              </SplitItem>
            </Split>

            <DescriptionList
              columns={2}
              items={[
                { label: 'Platform', value: detail.platform },
                { label: 'Authentication', value: detail.authentication_type },
                { label: 'Server version', value: detail.server_version },
                { label: 'CA certificate', value: detail.has_ca_certificate ? 'Stored' : 'System trust store' },
                {
                  label: 'TLS verification',
                  value: detail.skip_tls_verify ? 'Disabled' : 'Enabled',
                },
                { label: 'Registered', value: formatTimestamp(detail.created_at) },
                { label: 'Last connected', value: formatTimestamp(detail.last_connected) },
                {
                  label: 'Status',
                  value: <StatusBadge status={detail.status} />,
                },
              ]}
            />

            {tests[detail.id] ? (
              <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }}>
                {tests[detail.id].reachable ? (
                  <Alert
                    isInline
                    variant="success"
                    title={`Reachable — ${tests[detail.id].server_version ?? 'version not reported'} in ${
                      tests[detail.id].latency_ms
                    } ms`}
                  />
                ) : (
                  <Alert isInline variant="danger" title="Not reachable">
                    <p>{tests[detail.id].error?.message || 'The cluster did not answer.'}</p>
                    {tests[detail.id].error?.hint && (
                      <p style={{ fontWeight: 600 }}>{tests[detail.id].error.hint}</p>
                    )}
                  </Alert>
                )}
                <PermissionMatrix test={tests[detail.id]} />
              </div>
            ) : (
              <Alert
                isInline
                variant="info"
                title="Permissions have not been checked in this session"
                style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }}
              >
                A registration that connects can still be missing permissions the console needs. Run the
                test to see the baseline verb set as a pass/fail grid — the alternative is finding out from
                a 403 on a page you needed.
              </Alert>
            )}
          </CardBody>
        </Card>
      )}

      <ClusterFormModal
        isOpen={formOpen}
        editing={editing}
        onClose={() => setFormOpen(false)}
        onSaved={() => refresh()}
      />

      <ConfirmDialog
        isOpen={Boolean(deleting)}
        title={`De-register ${deleting?.name ?? ''}`}
        description={
          'This removes the registration and its stored credential from the console. It does not touch the ' +
          'cluster: nothing running there is affected, and re-registering restores access. Audit records ' +
          'for this cluster are kept — the trail is append-only.'
        }
        confirmLabel="De-register"
        isDanger
        isConfirming={removing}
        requireTyped={deleting?.name}
        onConfirm={remove}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}
