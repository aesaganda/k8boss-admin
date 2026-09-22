/**
 * Access — ServiceAccounts, Roles, ClusterRoles and their bindings.
 *
 * Two fields on this page are tri-state, and both are places where collapsing
 * "unset" into "false"/"zero" would be a wrong answer about a security control:
 *
 * **`automount`.** §8 keeps it as `true` / `false` / `null`, where `null` means
 * the ServiceAccount does not decide and the pod spec does — which usually means
 * the token *is* mounted. Rendering `null` as "No" would tell an operator
 * auditing token exposure that a ServiceAccount does not mount its token when it
 * does.
 *
 * **`rule_count`.** `null` when `rules` is absent rather than empty. An
 * aggregated ClusterRole has its rules filled in by a controller; before that
 * happens the field is null, and reporting `0` would describe an aggregate that
 * grants cluster-admin as granting nothing. An explicit `rules: []` is a real
 * zero and shows as one.
 */
import { useMemo, useState } from 'react';
import {
  AgeCell,
  DataTable,
  DescriptionList,
  NullableCell,
  PageHeader,
  StatusBadge,
} from '../components/ui';
import { useCluster } from '../contexts/ClusterContext';
import { ChipList, Muted, NoClusterState, ResourceTabsPage, YamlPanel, menuAction } from './_parts';
import SubjectReviewPanel from '../components/SubjectReviewPanel';
import CsrDecisionDialog, { SubjectPanel } from '../components/CsrDecisionDialog';
import { useGates } from './_data';

const RBAC = 'rbac.authorization.k8s.io';
const CERTIFICATES = 'certificates.k8s.io';

// §25. `update` on the approval *subresource*, which RBAC names separately from
// the request itself. The second permission the API server checks — `approve` on
// `certificates.k8s.io/signers`, named for the request's signerName — is not
// checked here: it is per-signer, so a single answer for the tab would be about
// a signer nobody has picked yet. The write preflights it by name, and a denial
// there names the signer.
const CSR_CHECKS = [
  { id: 'decide', verb: 'update', group: CERTIFICATES, resource: 'certificatesigningrequests', subresource: 'approval' },
];

/**
 * The five states §25 keeps apart, and the pair that matters.
 *
 * `Approved` and `Issued` are not one state. Approving records a condition and a
 * *signer* then has to act; where none runs for the signerName the request stops
 * at Approved with no certificate, indefinitely, looking like a success.
 */
const CSR_STATES = {
  Pending: { status: 'progressing', label: 'Pending' },
  Approved: {
    status: 'Unknown',
    label: 'Approved',
    tooltip: 'Approved, and no certificate has been issued yet. A signer has to act — if none runs for this signerName, it stays here.',
  },
  Issued: { status: 'Ready', label: 'Issued' },
  Denied: { status: 'failed', label: 'Denied' },
  Failed: { status: 'failed', label: 'Failed' },
};

/** The rules of a Role or ClusterRole, as a table rather than a paragraph. */
function RulesTable({ rules, ruleCount }) {
  if (ruleCount == null) {
    return (
      <NullableCell
        value={null}
        reason="This role's rules are absent rather than empty — an aggregated ClusterRole whose controller has not filled them in yet. It is not a role that grants nothing."
      />
    );
  }
  return (
    <DataTable
      ariaLabel="Policy rules"
      isStickyHeader={false}
      columns={[
        {
          key: 'apiGroups',
          title: 'API groups',
          cell: (rule) => <ChipList values={(rule.apiGroups ?? []).map((g) => g || 'core')} max={3} emptyText="none" />,
        },
        {
          key: 'resources',
          title: 'Resources',
          cell: (rule) => <ChipList values={rule.resources} max={4} emptyText="none" />,
        },
        {
          key: 'verbs',
          title: 'Verbs',
          cell: (rule) => (
            <ChipList
              values={rule.verbs}
              max={6}
              // A wildcard verb is the thing an audit is looking for, so it is
              // coloured rather than sitting quietly among the others.
              color={(rule.verbs ?? []).includes('*') ? 'red' : 'blue'}
              emptyText="none"
            />
          ),
        },
        {
          key: 'resourceNames',
          title: 'Names',
          cell: (rule) =>
            (rule.resourceNames ?? []).length ? (
              <ChipList values={rule.resourceNames} max={3} />
            ) : (
              <Muted>all</Muted>
            ),
        },
      ]}
      rows={rules ?? []}
      rowKey={(rule, index) => index}
      emptyTitle="No rules"
      emptyDescription="This role explicitly grants nothing. `rules: []` is a real, empty rule set."
    />
  );
}

function roleColumns({ namespaced }) {
  return [
    { key: 'name', title: 'Name', sortable: true },
    ...(namespaced ? [{ key: 'namespace', title: 'Namespace', sortable: true }] : []),
    {
      key: 'rule_count',
      title: 'Rules',
      sortable: true,
      cell: (row) => (
        <NullableCell
          value={row.rule_count}
          reason="The rules are absent, not empty — typically an aggregated ClusterRole the controller has not populated yet."
        />
      ),
    },
    {
      key: 'verbs',
      title: 'Verbs granted',
      value: (row) => (row.rules ?? []).flatMap((rule) => rule.verbs ?? []).join(','),
      cell: (row) => {
        const verbs = [...new Set((row.rules ?? []).flatMap((rule) => rule.verbs ?? []))];
        return <ChipList values={verbs} max={4} color={verbs.includes('*') ? 'red' : 'blue'} emptyText="none" />;
      },
    },
    { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
  ];
}

function bindingColumns({ namespaced }) {
  return [
    { key: 'name', title: 'Name', sortable: true },
    ...(namespaced ? [{ key: 'namespace', title: 'Namespace', sortable: true }] : []),
    {
      key: 'role',
      title: 'Role',
      sortable: true,
      value: (row) => `${row.role?.kind ?? ''}/${row.role?.name ?? ''}`,
      cell: (row) =>
        row.role ? (
          <span>
            <Muted>{row.role.kind}/</Muted>
            {row.role.name}
          </span>
        ) : (
          <NullableCell value={null} reason="This binding carries no roleRef, which should not be possible." />
        ),
    },
    {
      key: 'subjects',
      title: 'Subjects',
      sortable: true,
      value: (row) => (row.subjects ?? []).length,
      cell: (row) => (
        <ChipList
          values={(row.subjects ?? []).map(
            (subject) =>
              `${subject.kind}:${subject.namespace ? `${subject.namespace}/` : ''}${subject.name}`,
          )}
          max={2}
          emptyText="none — this binding grants nothing to anyone"
        />
      ),
    },
    { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
  ];
}

export default function Access() {
  const { activeClusterId } = useCluster();
  // `{ name, decision }` — which request is being decided, and which way.
  const [deciding, setDeciding] = useState(null);
  const [csrRefresh, setCsrRefresh] = useState(0);
  const { gate: csrGate } = useGates(CSR_CHECKS, { enabled: activeClusterId != null });

  const tabs = useMemo(
    () => [
      {
        key: 'serviceaccounts',
        title: 'ServiceAccounts',
        group: 'core',
        version: 'v1',
        plural: 'serviceaccounts',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'No ServiceAccounts in this scope, which is impossible for a real namespace — check the banner.',
        detailTitle: (row) => `ServiceAccount ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          { key: 'namespace', title: 'Namespace', sortable: true },
          { key: 'secrets_count', title: 'Secrets', sortable: true },
          {
            key: 'automount',
            title: 'Automount token',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.automount}
                // Without a formatter, `false` renders as nothing at all in JSX —
                // and an empty cell next to a dash is exactly the ambiguity this
                // tri-state exists to remove.
                format={(value) => (value ? 'Yes' : 'No')}
                reason="This ServiceAccount does not set automountServiceAccountToken, so the decision is made by each pod spec — and defaults to mounting. Unset is not the same as No."
              />
            ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                { label: 'Secrets', value: row.secrets_count },
                {
                  label: 'Automount token',
                  value: (
                    <NullableCell
                      value={row.automount}
                      format={(value) => (value ? 'Yes' : 'No')}
                      reason="Unset: each pod spec decides, and the default is to mount."
                    />
                  ),
                },
              ]}
            />
            <YamlPanel
              group="core"
              version="v1"
              plural="serviceaccounts"
              name={row.name}
              namespace={row.namespace}
              height={340}
            />
          </>
        ),
      },

      {
        key: 'roles',
        title: 'Roles',
        group: RBAC,
        version: 'v1',
        plural: 'roles',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'No Roles in this scope.',
        detailTitle: (row) => `Role ${row?.namespace}/${row?.name}`,
        columns: roleColumns({ namespaced: true }),
        detail: (row) => (
          <>
            <RulesTable rules={row.rules} ruleCount={row.rule_count} />
            <YamlPanel group={RBAC} version="v1" plural="roles" name={row.name} namespace={row.namespace} height={300} />
          </>
        ),
      },

      {
        key: 'clusterroles',
        title: 'ClusterRoles',
        group: RBAC,
        version: 'v1',
        plural: 'clusterroles',
        namespaced: false,
        rowKey: (row) => row.name,
        emptyDescription: 'No ClusterRoles, which is impossible on a running cluster — check the banner above.',
        detailTitle: (row) => `ClusterRole ${row?.name}`,
        columns: roleColumns({ namespaced: false }),
        detail: (row) => (
          <>
            <RulesTable rules={row.rules} ruleCount={row.rule_count} />
            <YamlPanel group={RBAC} version="v1" plural="clusterroles" name={row.name} height={300} />
          </>
        ),
      },

      {
        key: 'rolebindings',
        title: 'RoleBindings',
        group: RBAC,
        version: 'v1',
        plural: 'rolebindings',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'No RoleBindings in this scope.',
        detailTitle: (row) => `RoleBinding ${row?.namespace}/${row?.name}`,
        columns: bindingColumns({ namespaced: true }),
        detail: (row) => (
          <YamlPanel group={RBAC} version="v1" plural="rolebindings" name={row.name} namespace={row.namespace} height={420} />
        ),
      },

      {
        key: 'clusterrolebindings',
        title: 'ClusterRoleBindings',
        group: RBAC,
        version: 'v1',
        plural: 'clusterrolebindings',
        namespaced: false,
        rowKey: (row) => row.name,
        emptyDescription: 'No ClusterRoleBindings, which is impossible on a running cluster — check the banner above.',
        detailTitle: (row) => `ClusterRoleBinding ${row?.name}`,
        columns: bindingColumns({ namespaced: false }),
        detail: (row) => (
          <YamlPanel group={RBAC} version="v1" plural="clusterrolebindings" name={row.name} height={420} />
        ),
      },

      // §25. The request's own listing, with the field `kubectl get csr` does
      // not have: what the certificate would *be*. It sits on this page because
      // approving one is the fastest way to create an identity on a cluster, and
      // the tabs beside it are where that identity's permissions are read
      // afterwards — by which time the certificate exists.
      {
        key: 'csrs',
        title: 'Certificate requests',
        group: CERTIFICATES,
        version: 'v1',
        plural: 'certificatesigningrequests',
        namespaced: false,
        // No §11.14 forms here, which is the one listing where they are not
        // ordinary. A CertificateSigningRequest exists for minutes and is
        // deleted by the control plane once it is finished; the row for a
        // decided one deliberately offers nothing at all, because there is no
        // un-approve and a menu on it would be a menu of things that cannot be
        // done. Labelling a request nobody will read again is not the exception
        // worth reopening that for.
        metadataForms: false,
        refreshToken: csrRefresh,
        rowKey: (row) => row.name,
        emptyDescription:
          'The listing succeeded and returned no certificate signing requests. Kubernetes deletes finished ones on a timer, so an empty list on a healthy cluster is ordinary.',
        detailTitle: (row) => `CertificateSigningRequest ${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          {
            key: 'state',
            title: 'State',
            sortable: true,
            cell: (row) => <StatusBadge {...(CSR_STATES[row.state] ?? { status: 'Unknown', label: row.state })} />,
          },
          {
            key: 'subject',
            title: 'Would become',
            sortable: true,
            value: (row) => row.subject?.common_name ?? '',
            // The column this tab exists for. `Requested by` below is who
            // asked; this is who they asked to be, and they are different
            // fields that no other screen puts side by side.
            cell: (row) =>
              row.subject ? (
                <span>
                  {row.subject.common_name ?? <Muted>no common name</Muted>}
                  {(row.subject.organizations ?? []).length > 0 && (
                    <ChipList
                      values={row.subject.organizations}
                      max={2}
                      color={row.subject.organizations.includes('system:masters') ? 'red' : 'blue'}
                    />
                  )}
                </span>
              ) : (
                <NullableCell
                  value={null}
                  reason={row.decode_error ?? 'The request could not be decoded, so what it asks for is unknown — not empty.'}
                />
              ),
          },
          { key: 'requestor', title: 'Requested by', sortable: true },
          {
            key: 'signer_name',
            title: 'Signer',
            sortable: true,
            cell: (row) => (
              <span>
                <code>{row.signer_name}</code>
                {row.signer_known === false && (
                  <>
                    {' '}
                    <StatusBadge
                      status="Unknown"
                      label="no built-in signer"
                      tooltip="kube-controller-manager signs only the three kubernetes.io/… signerNames. Approving this leaves it Approved with no certificate unless a controller for it is running."
                    />
                  </>
                )}
              </span>
            ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        actions: (row) => {
          const gate = csrGate('decide');
          // A decided request is not offered a second decision: the API server
          // refuses one, and an enabled button that always fails is worse than
          // an absent one.
          if (row.state !== 'Pending') return [];
          return [
            menuAction('Approve…', gate, () => setDeciding({ name: row.name, decision: 'Approved' })),
            menuAction('Deny…', gate, () => setDeciding({ name: row.name, decision: 'Denied' })),
          ];
        },
        detail: (row) => (
          <>
            <SubjectPanel request={row} />
            <YamlPanel
              group={CERTIFICATES}
              version="v1"
              plural="certificatesigningrequests"
              name={row.name}
              height={300}
            />
          </>
        ),
      },

      // §23. First a tab whose rows are not a §4 listing — it is a question put
      // to the API server, not a browse — so it uses `render` rather than the
      // listing machinery. It sits here because the tabs beside it are exactly
      // what somebody would otherwise try to subtract into this answer, and
      // that derived answer is blind to every authorizer that is not RBAC.
      {
        key: 'review',
        title: 'Access review',
        render: () => <SubjectReviewPanel />,
      },
    ],
    [csrGate, csrRefresh],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Access control" />
        <NoClusterState what="Access control" />
      </>
    );
  }

  return (
    <>
      {deciding && (
        <CsrDecisionDialog
          name={deciding.name}
          decision={deciding.decision}
          onClose={() => setDeciding(null)}
          // Reloads the tab but leaves the dialog open, like §24's. The summary
          // is the sentence that matters after this write: approving records a
          // condition, and `Issued` rather than `Approved` is what says a
          // certificate exists.
          onApplied={() => setCsrRefresh((token) => token + 1)}
        />
      )}
      <ResourceTabsPage
        basePath="/access"
        subtitle="Who can do what to this cluster. These are the objects the console's own ServiceAccount is subject to as well."
        tabs={tabs}
      />
    </>
  );
}
