/**
 * NamespaceDetail — §17 `GET /api/projects/{name}`.
 *
 * OpenShift calls this page a Project: the namespace together with the quota,
 * limit range, security posture and role bindings that decide what a team can
 * do in it. Vanilla Kubernetes has every one of those objects and no page that
 * shows them together, so the question this page answers — *why will the next
 * Deployment not be admitted* — is otherwise four `kubectl get` calls away.
 *
 * Every section here is a tri-state, and the middle state is the one that
 * matters:
 *
 * - `quotas: null` is a listing that did not answer. It renders as a failure
 *   panel where the table would be — never as "no quota", which would send an
 *   operator to add a second quota over the one they could not see.
 * - `quotas: []` is the finding: nothing bounds this namespace. It renders as
 *   an empty state that says so.
 * - A quota's `used` is `null` until the quota controller has written status,
 *   and renders as an em dash with the reason. `0` there would say nothing in
 *   the namespace counts against the quota yet.
 * - `podSecurity.enforce: null` is "this namespace declares nothing" — the
 *   cluster default applies, and that default is read from a file no API
 *   serves, so this console cannot say what it is. It is never rendered as
 *   `privileged`.
 * - "isolated" means a policy *declares* it. Whether the CNI plugin enforces
 *   NetworkPolicy is not knowable from any API here, and the page says so.
 */
import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, Button, Card, CardBody, Grid, GridItem } from '@patternfly/react-core';
import {
  AgeCell,
  DataTable,
  DescriptionList,
  EmptyState,
  ErrorState,
  NullableCell,
  ActionButton,
  PageHeader,
  PartialBanner,
  SectionHeader,
  StatusBadge,
} from '../components/ui';
import { projects as projectsApi } from '../api/client';
import DeleteNamespaceDialog from '../components/DeleteNamespaceDialog';
import QuotaAdvisor from '../components/QuotaAdvisor';
import MetadataDialog from '../components/MetadataDialog';
import PodSecurityDialog from '../components/PodSecurityDialog';
import GrantRoleDialog from '../components/GrantRoleDialog';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync, useGates } from './_data';
import { ChipList, EditLink, LabelsCell, Muted, NoClusterState } from './_parts';

const MODES = ['enforce', 'audit', 'warn'];

//: §18's write is a patch on the namespace itself, so the button is gated on
//: the verb the write will actually use rather than on anything in it. §26's is
//: `delete` on the same resource — gated separately, because an operator who may
//: set a Pod Security level is very often not one who may delete the namespace.
const CHECKS = [
  { id: 'patch', verb: 'patch', group: 'core', resource: 'namespaces' },
  // §11.14's labels and annotations forms send the whole object through §4's
  // PUT, so `update` is the permission they preflight — not the `patch` the
  // Pod Security level write above uses.
  { id: 'update', verb: 'update', group: 'core', resource: 'namespaces' },
  { id: 'delete', verb: 'delete', group: 'core', resource: 'namespaces' },
  // §30. `create` is the one asked for here because it is the harder half: a
  // first grant creates the binding, and a caller who may patch an existing one
  // but not create a new one still gets the button. The write preflights the
  // verb it is actually about to use, and a denial there names it.
  { id: 'bind', verb: 'create', group: 'rbac.authorization.k8s.io', resource: 'rolebindings' },
];

/** `cpu=500m, memory=512Mi` for a LimitRange map, or a muted "unset". */
function quantities(map) {
  const entries = Object.entries(map ?? {});
  if (!entries.length) return <Muted>unset</Muted>;
  return entries.map(([resource, value]) => `${resource}=${value}`).join(', ');
}

/**
 * The failure panel a section renders when its listing did not answer.
 *
 * Deliberately not an empty state and not hidden: the reason from
 * `unavailable[]` is the only thing that tells the operator whether to fix
 * RBAC or wait out an API server.
 */
function SectionUnavailable({ unavailable, resource, what }) {
  const entry = (unavailable ?? []).find((e) => e.resource === resource);
  return (
    <Alert
      isInline
      variant="warning"
      title={`${what} could not be read`}
      data-testid={`project-${resource}-unavailable`}
    >
      {entry?.reason === 'forbidden'
        ? `The console is not permitted to list ${resource} in this namespace. `
        : `The ${resource} listing did not answer. `}
      This is not the same as there being none: the section is unknown, not empty.
      {entry?.detail ? <pre style={{ whiteSpace: 'pre-wrap', marginTop: '0.5rem' }}>{entry.detail}</pre> : null}
    </Alert>
  );
}

function PodSecurity({ podSecurity }) {
  if (!podSecurity) return null;
  return (
    <div data-testid="project-pod-security" data-labelled={String(podSecurity.labelled)}>
      {!podSecurity.labelled && (
        <Alert isInline variant="info" title="This namespace declares no Pod Security level" className="admin-confirm__alert">
          Whatever the cluster&apos;s Pod Security admission default is applies here. That default is read
          from a file on the API server which no API serves, so this console cannot tell you what it
          is. This is not the same as <code>privileged</code>.
        </Alert>
      )}
      <DescriptionList
        columns={3}
        items={MODES.map((mode) => ({
          label: mode,
          value:
            podSecurity[mode] == null ? (
              <NullableCell
                value={null}
                reason={`No ${mode} label is set on this namespace, so the cluster default applies for this mode.`}
              />
            ) : (
              <>
                <StatusBadge
                  status={podSecurity[mode] === 'privileged' ? 'unknown' : 'Ready'}
                  label={podSecurity[mode]}
                />{' '}
                {podSecurity[`${mode}Version`] ? <Muted>{podSecurity[`${mode}Version`]}</Muted> : null}
              </>
            ),
        }))}
      />
    </div>
  );
}

function QuotaTable({ quota }) {
  const columns = useMemo(
    () => [
      { key: 'resource', title: 'Resource', sortable: true },
      {
        key: 'used',
        title: 'Used',
        cell: (row) => (
          <NullableCell
            value={row.used}
            reason={
              quota.reconciled
                ? 'The quota controller has not recorded usage for this resource.'
                : 'The quota controller has not written status for this quota yet — it is not zero, it is unknown.'
            }
          />
        ),
      },
      { key: 'hard', title: 'Hard limit' },
      {
        key: 'exhausted',
        title: 'Headroom',
        value: (row) => (row.exhausted == null ? '' : row.exhausted ? 'exhausted' : 'available'),
        cell: (row) =>
          row.exhausted == null ? (
            <NullableCell value={null} reason="Either side could not be compared, so whether this limit is reached is unknown." />
          ) : row.exhausted ? (
            <StatusBadge status="NotReady" label="Exhausted" tooltip="Used has reached the hard limit. The next object counted here is refused at admission." />
          ) : (
            <StatusBadge status="Ready" label="Available" />
          ),
      },
    ],
    [quota.reconciled],
  );
  return (
    <div data-testid="project-quota" data-reconciled={String(quota.reconciled)}>
      <p>
        <strong>{quota.name}</strong>
        {quota.scopes?.length ? <Muted> · scopes {quota.scopes.join(', ')}</Muted> : null}
        {quota.scoped ? (
          <Muted> · scope selector: these numbers cover a subset of the namespace&apos;s pods</Muted>
        ) : null}
      </p>
      <DataTable
        ariaLabel={`Quota ${quota.name}`}
        columns={columns}
        rows={quota.resources}
        rowKey="resource"
        emptyTitle="This quota bounds nothing"
        emptyDescription="spec.hard is empty — the object exists and constrains no resource."
      />
    </div>
  );
}

const LIMIT_COLUMNS = [
  { key: 'type', title: 'Type', sortable: true },
  { key: 'defaultRequest', title: 'Default request', cell: (row) => quantities(row.defaultRequest) },
  { key: 'default', title: 'Default limit', cell: (row) => quantities(row.default) },
  { key: 'min', title: 'Min', cell: (row) => quantities(row.min) },
  { key: 'max', title: 'Max', cell: (row) => quantities(row.max) },
  { key: 'maxLimitRequestRatio', title: 'Max limit/request', cell: (row) => quantities(row.maxLimitRequestRatio) },
];

const BINDING_COLUMNS = [
  { key: 'name', title: 'Binding', sortable: true },
  {
    key: 'role',
    title: 'Role',
    value: (row) => `${row.role?.kind ?? ''}/${row.role?.name ?? ''}`,
    cell: (row) =>
      row.role ? (
        <span>
          <Muted>{row.role.kind}/</Muted>
          {row.role.name}
        </span>
      ) : (
        <Muted>none</Muted>
      ),
  },
  {
    key: 'subjects',
    title: 'Subjects',
    value: (row) => (row.subjects ?? []).length,
    cell: (row) => (
      <ChipList
        values={(row.subjects ?? []).map((s) => `${s.kind}:${s.namespace ? `${s.namespace}/` : ''}${s.name}`)}
        max={3}
      />
    ),
  },
  { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
];

function isolationBadge(value, direction) {
  if (value === true) return <StatusBadge status="Ready" label={`${direction}: every pod selected`} />;
  if (value === false) return <StatusBadge status="unknown" label={`${direction}: no namespace-wide policy`} />;
  return (
    <NullableCell
      value={null}
      reason={`A policy's selector could not be evaluated, so whether every pod is selected for ${direction.toLowerCase()} is unknown.`}
    />
  );
}

export default function NamespaceDetail() {
  const { name } = useParams();
  const { activeClusterId } = useCluster();
  const { setSelected } = useNamespace();
  const navigate = useNavigate();

  const [settingLevel, setSettingLevel] = useState(false);
  // 'labels' | 'annotations' | null — which half of the namespace's metadata.
  const [editingMetadata, setEditingMetadata] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [granting, setGranting] = useState(false);

  const { data, loading, error, reload } = useAsync(() => projectsApi.get(name), {
    key: `project:${activeClusterId}:${name}`,
    enabled: activeClusterId != null && Boolean(name),
  });
  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const breadcrumbs = [{ label: 'Namespaces', to: '/namespaces' }, { label: name }];

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title={name ?? 'Namespace'} breadcrumbs={breadcrumbs} />
        <NoClusterState what="This namespace" />
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader title={name} breadcrumbs={breadcrumbs} />
        <ErrorState title={`Namespace ${name} could not be read`} error={error} onRetry={reload} />
      </>
    );
  }

  const project = data ?? {};
  const policies = project.networkPolicies;

  return (
    <>
      <PageHeader
        title={project.displayName ? `${project.displayName} (${name})` : name}
        breadcrumbs={breadcrumbs}
        subtitle={loading ? 'Reading the cluster…' : project.description || 'What governs this namespace: quota, limits, security posture, access and network policy.'}
        badge={project.status ? <StatusBadge status={project.status} /> : undefined}
        actions={[
          <Button
            key="scope"
            variant="secondary"
            data-testid="project-scope"
            onClick={() => {
              setSelected(name);
              navigate('/workloads');
            }}
          >
            Scope the console to this namespace
          </Button>,
          // §26. Danger-styled and last, and it opens a plan rather than a
          // confirmation: what deleting a namespace takes with it is not in the
          // namespace object, so there is nothing useful to confirm until the
          // plan has been read.
          <ActionButton
            key="delete"
            isDanger
            gate={gate('delete')}
            onClick={() => setDeleting(true)}
          >
            Delete namespace
          </ActionButton>,
        ]}
      />

      <PartialBanner unavailable={project.unavailable} />

      {editingMetadata && (
        <MetadataDialog
          target={{ group: 'core', version: 'v1', plural: 'namespaces', namespace: null, name, kind: 'Namespace' }}
          field={editingMetadata}
          onClose={() => setEditingMetadata(null)}
          onApplied={() => {
            setEditingMetadata(null);
            reload();
          }}
        />
      )}

      {settingLevel && (
        <PodSecurityDialog
          namespace={name}
          current={project.podSecurity}
          onClose={() => setSettingLevel(false)}
          onApplied={reload}
        />
      )}

      {granting && (
        <GrantRoleDialog
          namespace={name}
          onClose={() => setGranting(false)}
          onApplied={reload}
        />
      )}

      {deleting && (
        <DeleteNamespaceDialog
          name={name}
          onClose={() => setDeleting(false)}
          // Reload rather than navigate away: `applied` means a
          // deletionTimestamp, and the namespace is usually still there — in
          // Terminating — for as long as its finalizers take. Leaving the page
          // on success would report the deletion as finished.
          onApplied={reload}
        />
      )}

      <Grid hasGutter>
        <GridItem lg={5} md={12}>
          <Card isFullHeight>
            <CardBody>
              <SectionHeader title="Namespace" />
              <DescriptionList
                items={[
                  {
                    label: 'Pods',
                    value: (
                      <NullableCell
                        value={project.pod_count}
                        reason="Pods could not be listed in this namespace, so the count is unknown rather than zero."
                      />
                    ),
                  },
                  { label: 'Age', value: <AgeCell seconds={project.age_seconds} timestamp={project.creationTimestamp} /> },
                  {
                    label: 'Labels',
                    value: (
                      <span className="admin-cell-inline">
                        <LabelsCell labels={project.labels} max={6} />
                        <EditLink
                          gate={gate('update')}
                          label="Edit labels"
                          onClick={() => setEditingMetadata('labels')}
                        />
                      </span>
                    ),
                  },
                  {
                    label: 'Annotations',
                    value: (
                      <span className="admin-cell-inline">
                        {Object.keys(project.annotations ?? {}).length ? (
                          <LabelsCell labels={project.annotations} max={4} />
                        ) : (
                          <Muted>none</Muted>
                        )}
                        <EditLink
                          gate={gate('update')}
                          label="Edit annotations"
                          onClick={() => setEditingMetadata('annotations')}
                        />
                      </span>
                    ),
                  },
                ]}
              />
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={7} md={12}>
          <Card isFullHeight>
            <CardBody>
              <SectionHeader
                title="Pod Security"
                description="The admission level this namespace's labels declare. OpenShift's security context constraints, in vanilla Kubernetes' vocabulary."
                actions={
                  <ActionButton gate={gate('patch')} onClick={() => setSettingLevel(true)}>
                    Set level…
                  </ActionButton>
                }
              />
              {loading && !data ? <Muted>Reading…</Muted> : <PodSecurity podSecurity={project.podSecurity} />}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem span={12}>
          <Card>
            <CardBody>
              <SectionHeader
                title="Resource quotas"
                description="What this namespace may consume, and how much of it is already in use."
              />
              {project.quotas === null && (
                <SectionUnavailable unavailable={project.unavailable} resource="resourcequotas" what="Resource quotas" />
              )}
              {Array.isArray(project.quotas) && project.quotas.length === 0 && (
                <EmptyState
                  title="Nothing bounds this namespace"
                  description="No ResourceQuota exists here. One Deployment in it can request every core and every byte the cluster has, and the scheduler will let it."
                />
              )}
              {(project.quotas ?? []).map((quota) => (
                <QuotaTable key={quota.name} quota={quota} />
              ))}
            </CardBody>
          </Card>
        </GridItem>

        {/* §29. Directly under the quota tables, because it answers the
            question those tables raise: the numbers above say what is used,
            and the only thing anybody wants from them is whether the next
            workload fits. It reads its own endpoint rather than deriving from
            `project.quotas` — the advice needs the LimitRanges too, and a
            second derivation of the same arithmetic could disagree with the
            first. */}
        <GridItem span={12}>
          <Card>
            <CardBody>
              <QuotaAdvisor namespace={name} />
            </CardBody>
          </Card>
        </GridItem>

        <GridItem span={12}>
          <Card>
            <CardBody>
              <SectionHeader
                title="Limit ranges"
                description="Defaults and bounds applied to pods that do not state their own resources."
              />
              {project.limitRanges === null && (
                <SectionUnavailable unavailable={project.unavailable} resource="limitranges" what="Limit ranges" />
              )}
              {Array.isArray(project.limitRanges) && project.limitRanges.length === 0 && (
                <EmptyState
                  title="No LimitRange"
                  description="Pods that set no requests get none. If a compute quota exists here, the API server refuses those pods outright — `must specify requests.cpu`."
                />
              )}
              {(project.limitRanges ?? []).map((range) => (
                <div key={range.name} data-testid="project-limit-range">
                  <p>
                    <strong>{range.name}</strong>
                  </p>
                  <DataTable
                    ariaLabel={`Limit range ${range.name}`}
                    columns={LIMIT_COLUMNS}
                    rows={range.limits}
                    rowKey={(row) => `${range.name}/${row.type}`}
                    emptyTitle="This LimitRange constrains nothing"
                    emptyDescription="spec.limits is empty."
                  />
                </div>
              ))}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={7} md={12}>
          <Card isFullHeight>
            <CardBody>
              <SectionHeader
                title="Role bindings"
                description="Who is bound to a role inside this namespace. Cluster-wide bindings act here too and are not listed."
                actions={
                  // §30. It opens a plan rather than a form that writes: what a
                  // roleRef confers is in a different object, and there is
                  // nothing useful to confirm until that object has been read.
                  <ActionButton
                    key="grant"
                    variant="secondary"
                    gate={gate('bind')}
                    onClick={() => setGranting(true)}
                  >
                    Grant or revoke a role
                  </ActionButton>
                }
              />
              {project.roleBindings === null && (
                <SectionUnavailable unavailable={project.unavailable} resource="rolebindings" what="Role bindings" />
              )}
              {Array.isArray(project.roleBindings) && (
                <DataTable
                  ariaLabel="Role bindings"
                  columns={BINDING_COLUMNS}
                  rows={project.roleBindings}
                  rowKey="name"
                  emptyTitle="Nobody is bound in this namespace"
                  emptyDescription="Only identities holding a cluster-wide binding can act here."
                />
              )}
            </CardBody>
          </Card>
        </GridItem>

        <GridItem lg={5} md={12}>
          <Card isFullHeight>
            <CardBody>
              <SectionHeader title="Network policies" description="What the namespace's policies declare about every pod at once." />
              {policies === null && (
                <SectionUnavailable unavailable={project.unavailable} resource="networkpolicies" what="Network policies" />
              )}
              {policies && (
                <div data-testid="project-network-policies" data-ingress={String(policies.isolatesAllIngress)}>
                  <DescriptionList
                    items={[
                      { label: 'Policies', value: policies.count },
                      { label: 'Ingress', value: isolationBadge(policies.isolatesAllIngress, 'Ingress') },
                      { label: 'Egress', value: isolationBadge(policies.isolatesAllEgress, 'Egress') },
                      {
                        label: 'Names',
                        value: policies.names.length ? <ChipList values={policies.names} max={4} /> : <Muted>none</Muted>,
                      },
                    ]}
                  />
                  <p style={{ marginTop: '0.75rem' }}>
                    <Muted>
                      Declared, not enforced: NetworkPolicy is implemented by the cluster&apos;s CNI plugin, and no
                      API this console can reach says whether this cluster&apos;s does.
                    </Muted>
                  </p>
                </div>
              )}
            </CardBody>
          </Card>
        </GridItem>
      </Grid>
    </>
  );
}
