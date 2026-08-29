/**
 * Network — Services, Ingresses, Endpoints, and NetworkPolicies.
 *
 * The column that earns the Services tab is `endpoint_count`. §8: *`endpoint_count`
 * is `null` if EndpointSlices could not be read.* A Service with no backends and
 * a Service whose backends we failed to look up are the two states an operator
 * most needs to tell apart when something is returning 503, so one renders as
 * `0` and the other as an em dash — never as each other.
 *
 * Endpoints are listed raw, because §8 defines no typed row for them and
 * inventing one here would put a second, quietly different shaping of the same
 * object in the frontend. Their `subsets` split ready from not-ready addresses,
 * which is the thing you actually want to see when a Service is black-holing:
 * an endpoint object with addresses only under `notReadyAddresses` is a Service
 * whose pods exist and are failing their readiness probe.
 *
 * ── The two NetworkPolicy tabs ───────────────────────────────────────────────
 *
 * This kind is the one where a confidently wrong cell stops being a wrong table
 * and becomes a wrong belief about who can reach a database, so four things are
 * rendered deliberately rather than conveniently:
 *
 * 1. **"Not governed" is not "Deny all".** A policy with `policyTypes: [Ingress]`
 *    and no egress section does not restrict egress at all; one with
 *    `policyTypes: [Ingress, Egress]` and no egress section blocks every packet
 *    out of every pod it selects. The backend keeps them apart —
 *    `rule_count: null` versus `0` — and this page keeps them apart on screen:
 *    an ungoverned direction is plain text, and only a governed one gets a pill.
 *
 * 2. **Rules are a union of allowances.** One rule that names neither peer nor
 *    port permits everything, and no number of restrictive neighbours narrows
 *    it. That is why the badge for such a policy is `Allow all` rather than the
 *    average of its rules, and why the drawer spells out each rule as its own
 *    sentence instead of a merged summary.
 *
 * 3. **A peer's shape decides its meaning, and the shapes look alike.**
 *    `podSelector` alone means pods **in the policy's own namespace**;
 *    `namespaceSelector` alone means every pod in the matching namespaces; both
 *    inside one list entry is the intersection, and the same two keys in two
 *    entries is the union. The rendering writes the sentence out rather than
 *    echoing the YAML, because the YAML is what people misread.
 *
 * 4. **Nothing here claims a policy is enforced.** NetworkPolicy objects are
 *    inert unless the cluster's CNI plugin implements them, and no API this
 *    console can reach reports whether it does. So both tabs carry a standing
 *    notice, and every word on them is about what the API server holds — never
 *    about what the network does with it.
 *
 * The Pod isolation tab inverts the listing: pods are the rows and policies are
 * the decoration. That is the whole point of it. Reading the policy list tells
 * an operator what they wrote; reading this tells them what they missed, and a
 * pod no policy selects — unrestricted, because Kubernetes defaults to allow —
 * appears on no policy's page at all.
 */
import { useCallback, useMemo, useState } from 'react';
import { Alert, Button } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  DescriptionList,
  ErrorState,
  MetricCard,
  NullableCell,
  PartialBanner,
  ResourceLink,
  SearchInput,
  Skeleton,
  StatGrid,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import { DeleteDialog } from '../components/DeleteDialog';
import { ImportYamlDialog } from '../components/ImportYamlDialog';
import { network as networkApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { objectAgeSeconds, objectName, objectNamespace, useAsync, useGates } from './_data';
import {
  ActionButton,
  ChipList,
  EditYamlDialog,
  Muted,
  NoClusterState,
  ResourceTabsPage,
  YamlPanel,
  genericTab,
  menuAction,
} from './_parts';

/** Ready / not-ready address tallies from a core/v1 Endpoints object. */
function endpointTallies(row) {
  const subsets = row?.subsets ?? [];
  let ready = 0;
  let notReady = 0;
  const ports = new Set();
  for (const subset of subsets) {
    ready += (subset.addresses ?? []).length;
    notReady += (subset.notReadyAddresses ?? []).length;
    for (const port of subset.ports ?? []) ports.add(`${port.port}/${port.protocol ?? 'TCP'}`);
  }
  return { ready, notReady, ports: [...ports] };
}

const NAMESPACE_COLUMN = { key: 'namespace', title: 'Namespace', sortable: true };

/* ── NetworkPolicy presentation ─────────────────────────────────────────── */

const POLICY_GVP = { group: 'networking.k8s.io', version: 'v1', plural: 'networkpolicies' };

/**
 * The permission checks both NetworkPolicy tabs need, built for the namespace in
 * scope. Every write on this page goes through §4's generic endpoints — the
 * console has no NetworkPolicy-shaped write of its own, so there is no second
 * path around the mutation funnel — which means these are the exact verbs the
 * backend will preflight.
 */
function policyChecks(namespace) {
  return [
    { id: 'create', verb: 'create', group: POLICY_GVP.group, resource: POLICY_GVP.plural, namespace },
    { id: 'update', verb: 'update', group: POLICY_GVP.group, resource: POLICY_GVP.plural, namespace },
    { id: 'delete', verb: 'delete', group: POLICY_GVP.group, resource: POLICY_GVP.plural, namespace },
  ];
}

/**
 * A starter manifest, seeded into the shared import dialog.
 *
 * Deliberately the *default-deny* policy rather than an allow rule. It is the
 * one every segmentation story starts with, it is four lines, and — unlike a
 * template full of placeholder selectors — there is no way to apply it by
 * accident and believe something was allowed. The namespace is left out on
 * purpose: `ImportYamlDialog` falls back to the masthead selection and tells the
 * operator which namespace it will use, and a hardcoded one would go stale the
 * moment they switched scope before finishing the edit.
 */
const POLICY_TEMPLATE = `apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
spec:
  # An empty podSelector selects EVERY pod in the namespace.
  podSelector: {}
  # Ingress listed with no ingress rules below denies all inbound traffic.
  # Removing this line would leave the policy governing nothing at all.
  policyTypes:
    - Ingress
`;

/** A LabelSelector as human-readable terms. `[]` means "matches everything". */
function selectorTerms(selector) {
  if (!selector) return [];
  const terms = Object.entries(selector.matchLabels ?? {}).map(([key, value]) => `${key}=${value}`);
  for (const expression of selector.matchExpressions ?? []) {
    const key = expression.key;
    const values = (expression.values ?? []).join(', ');
    if (expression.operator === 'Exists') terms.push(`${key} exists`);
    else if (expression.operator === 'DoesNotExist') terms.push(`${key} absent`);
    else if (expression.operator === 'In') terms.push(`${key} in (${values})`);
    else if (expression.operator === 'NotIn') terms.push(`${key} not in (${values})`);
    // An operator this console does not know is shown verbatim rather than
    // dropped: the backend already reports the selection as unknown, and a term
    // silently missing from the sentence would make the policy look narrower.
    else terms.push(`${key} ${expression.operator ?? '?'} (${values})`);
  }
  return terms;
}

/** "every pod", or the selector's terms joined — the subject of a sentence. */
function selectorPhrase(selector) {
  const terms = selectorTerms(selector);
  return terms.length ? terms.join(' and ') : 'any labels';
}

/**
 * One `from`/`to` entry as a sentence, in the policy's own namespace context.
 *
 * The namespace argument is not decoration. A bare `podSelector` peer selects
 * pods **in the policy's namespace**, and writing that namespace into the
 * sentence is the difference between a reader who knows that and a reader who
 * assumes the peer is cluster-wide — which is the most common way this API is
 * misread.
 */
function peerPhrase(peer, policyNamespace) {
  switch (peer?.type) {
    case 'ipBlock':
      return peer.except?.length
        ? `${peer.cidr} except ${peer.except.join(', ')}`
        : `${peer.cidr}`;
    case 'pod':
      return `pods in ${policyNamespace} with ${selectorPhrase(peer.podSelector)}`;
    case 'namespace':
      return `every pod in namespaces with ${selectorPhrase(peer.namespaceSelector)}`;
    case 'namespace_pod':
      return `pods with ${selectorPhrase(peer.podSelector)} in namespaces with ${selectorPhrase(
        peer.namespaceSelector,
      )}`;
    default:
      return 'a peer this console does not recognise';
  }
}

/** One `ports` entry. An unset protocol is TCP by the API's own default. */
function portPhrase(port) {
  const protocol = port.protocol ?? 'TCP';
  const range = port.endPort != null ? `${port.port}-${port.endPort}` : `${port.port ?? 'any'}`;
  return `${range}/${protocol}`;
}

const DIRECTION_HELP = {
  deny_all:
    'This direction is governed and the policy carries no rules, so it permits nothing. Traffic the other policies on these pods allow still gets through — policies add up.',
  allow_all:
    'One rule here restricts neither peer nor port, so this policy permits everything in this direction. The other rules beside it cannot narrow that: NetworkPolicy rules are a union of allowances, never an intersection.',
  restricted: 'This direction is governed and the policy names the peers or ports it permits.',
};

const NOT_GOVERNED_HELP =
  'This policy declares no section for this direction, so it does not restrict it at all. That is the opposite of denying it — and the two look almost identical in YAML.';

/** The label the direction column sorts and filters on. */
function directionLabel(direction) {
  if (!direction?.governed) return 'Not governed';
  if (direction.effect === 'deny_all') return 'Deny all';
  if (direction.effect === 'allow_all') return 'Allow all';
  if (direction.effect === 'restricted') return 'Restricted';
  return 'Unknown';
}

/**
 * A direction as a cell.
 *
 * "Not governed" is text and not a grey pill, deliberately. Grey in this
 * console's badge vocabulary means *we could not find out*; whether a policy
 * declares a direction is something we read straight off the object and know
 * for certain. A grey pill would file a fact under the same colour as an
 * unknown, on the page where that distinction is worth the most.
 */
function DirectionCell({ direction }) {
  if (!direction?.governed) return <Muted title={NOT_GOVERNED_HELP}>Not governed</Muted>;
  return (
    <StatusBadge
      status={direction.effect ?? 'unknown'}
      label={directionLabel(direction)}
      tooltip={DIRECTION_HELP[direction.effect]}
    />
  );
}

/**
 * The standing caveat, on both NetworkPolicy tabs and inside the drawer.
 *
 * Not dismissible and not a toast. Everything on these tabs is read from the API
 * server, and the API server has no idea whether the CNI plugin implements any
 * of it: a cluster running a plugin without NetworkPolicy support accepts,
 * stores and serves these objects while forwarding every packet they claim to
 * drop. "Deny all" on this page means the object says so.
 */
function EnforcementNotice() {
  return (
    <Alert
      isInline
      variant="info"
      title="These are declared rules, not observed traffic"
      data-testid="policy-enforcement-notice"
    >
      NetworkPolicy is enforced by the cluster&apos;s CNI plugin, and no API this console can read
      reports whether this cluster&apos;s plugin implements it. A cluster whose plugin does not will
      store and serve these objects while forwarding the traffic they describe as denied. Everything
      below is what the API server holds.
    </Alert>
  );
}

/** One direction of a policy, spelled out rule by rule. */
function PolicyRules({ direction, kind, namespace }) {
  if (!direction?.governed) {
    return (
      <p>
        <Muted>{`No ${kind} section. ${NOT_GOVERNED_HELP}`}</Muted>
      </p>
    );
  }
  if (!direction.rules.length) {
    return (
      <p>
        <Muted>
          {`${kind} is governed and no rules are listed, so this policy permits no ${kind} traffic ` +
            'to the pods it selects.'}
        </Muted>
      </p>
    );
  }
  const preposition = kind === 'Ingress' ? 'from' : 'to';
  return (
    <ol style={{ margin: '0.25rem 0 0', paddingLeft: '1.25rem' }}>
      {direction.rules.map((rule, index) => (
        <li key={index} style={{ marginBottom: '0.35rem' }}>
          <div>
            <strong>{preposition}</strong>{' '}
            {rule.allows_all_peers ? (
              <em>anywhere</em>
            ) : (
              rule.peers.map((peer, i) => (
                <span key={i}>
                  {i > 0 ? ' or ' : ''}
                  {peerPhrase(peer, namespace)}
                </span>
              ))
            )}
          </div>
          <div>
            <strong>on</strong>{' '}
            {rule.allows_all_ports ? (
              <em>any port</em>
            ) : (
              <ChipList values={rule.ports.map(portPhrase)} max={6} color="blue" />
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

/**
 * The drawer for one policy: what it declares, and who it actually selects.
 *
 * The second half needs a pod listing, so it comes from §8.4's typed endpoint
 * rather than from the row in the table. `selected_pod_count` is therefore
 * genuinely nullable here in the §0 sense — `null` means *the pods could not be
 * listed*, and `0` means *this policy governs nothing*, which is the finding
 * that gets a policy deleted as dead. The list row deliberately carries no such
 * field: a count that was always null in the table would make the em dash mean
 * "we did not look", and this page cannot afford a second meaning for it.
 */
function PolicyDetail({ row }) {
  const { activeClusterId } = useCluster();
  const detail = useAsync(() => networkApi.policy(row.namespace, row.name), {
    key: `networkpolicy:${activeClusterId}:${row.namespace}/${row.name}`,
  });
  const data = detail.data;
  const selected = data?.selected_pods ?? null;

  return (
    <>
      <EnforcementNotice />
      <PartialBanner unavailable={data?.unavailable} />

      <DescriptionList
        items={[
          {
            label: 'Applies to',
            value:
              row.selects_all_pods === true ? (
                <strong>{`Every pod in ${row.namespace}`}</strong>
              ) : row.selects_all_pods === false ? (
                <ChipList values={selectorTerms(row.pod_selector)} max={6} />
              ) : (
                <NullableCell
                  value={null}
                  reason="This object carries no spec.podSelector, which the API requires — so which pods it applies to cannot be read off it."
                />
              ),
          },
          {
            label: 'Governs',
            value: (
              <>
                <ChipList values={row.policy_types} max={2} emptyText="nothing — this policy has no effect" />
                {row.policy_types_source === 'derived' && (
                  <div>
                    <Muted title="spec.policyTypes is absent, so this is the API's own defaulting rule applied by the console: every policy affects ingress, and one carrying an egress section also affects egress.">
                      derived — the object does not declare this
                    </Muted>
                  </div>
                )}
              </>
            ),
          },
          {
            label: 'Selects',
            value: detail.loading && !data ? (
              <Skeleton lines={1} height="0.8rem" />
            ) : (
              <NullableCell
                value={data?.selected_pod_count}
                unit={data?.selected_pod_count === 1 ? 'pod' : 'pods'}
                reason="The pods in this namespace could not be listed, or a selector could not be evaluated — so how many this policy selects is unknown, not zero."
              />
            ),
          },
        ]}
      />

      {detail.error && <ErrorState title="The selected pods could not be read" error={detail.error} onRetry={detail.reload} />}

      {selected != null && (
        <div style={{ marginTop: '0.75rem' }}>
          {selected.length ? (
            <ChipList values={selected.map((pod) => pod.name)} max={12} color="blue" />
          ) : (
            <Alert
              isInline
              variant="warning"
              title="This policy selects no pods"
              data-testid="policy-selects-nothing"
            >
              Its podSelector matches nothing in {row.namespace}, so it is currently inert. A policy
              that was meant to deny traffic and selects nothing denies nothing.
            </Alert>
          )}
        </div>
      )}

      <h3 style={{ marginTop: '1rem' }}>Ingress</h3>
      <PolicyRules direction={row.ingress} kind="Ingress" namespace={row.namespace} />

      <h3 style={{ marginTop: '1rem' }}>Egress</h3>
      <PolicyRules direction={row.egress} kind="Egress" namespace={row.namespace} />

      <YamlPanel
        group={POLICY_GVP.group}
        version={POLICY_GVP.version}
        plural={POLICY_GVP.plural}
        name={row.name}
        namespace={row.namespace}
        height={320}
      />
    </>
  );
}

/* ── Pod isolation ──────────────────────────────────────────────────────── */

const ISOLATION_UNKNOWN_REASON =
  'A policy in this namespace uses a selector this console cannot evaluate, so whether it selects ' +
  'this pod is unknown. This is not "no policy selects it" — that sentence is the one an operator acts on.';

/**
 * The same three effects, worded for a pod rather than for a policy.
 *
 * Several policies can select one pod and their allowances add up, so the
 * sentence a pod row needs is about the total — "nothing reaches this pod",
 * not "this policy carries no rules". Reusing the per-policy wording here read
 * as a claim about a single object and understated how the combination works.
 */
const COMBINED_HELP = {
  deny_all:
    'Every policy selecting this pod governs this direction and none of them permits anything, so nothing is allowed through.',
  allow_all:
    'One policy selecting this pod permits every peer on every port. Policies are a union of allowances, so the stricter ones beside it change nothing.',
  restricted: 'The policies selecting this pod permit the peers and ports they name, and nothing else.',
};

/** One direction of one pod's coverage. */
function IsolationCell({ state, kind }) {
  if (state?.isolated == null) {
    return <NullableCell value={null} reason={ISOLATION_UNKNOWN_REASON} />;
  }
  if (state.isolated === false) {
    return (
      <StatusBadge
        status="warning"
        label="Unrestricted"
        tooltip={
          `No NetworkPolicy in this namespace both selects this pod and declares an ${kind.toLowerCase()} ` +
          `section, so Kubernetes' default applies and all ${kind.toLowerCase()} traffic is permitted.`
        }
      />
    );
  }
  if (!state.effect) {
    return (
      <StatusBadge
        status="unknown"
        label="Governed"
        tooltip={
          'A policy selecting this pod governs this direction, so it is restricted — but another ' +
          'policy could not be evaluated, and it may permit more. What is allowed in total is unknown.'
        }
      />
    );
  }
  return (
    <StatusBadge
      status={state.effect}
      label={directionLabel({ governed: true, effect: state.effect })}
      tooltip={`${COMBINED_HELP[state.effect]} Selected by: ${state.policies.join(', ')}.`}
    />
  );
}

/**
 * Every pod, and what selects it — the inverse of the policy listing.
 *
 * Rendered by hand rather than through `ResourceTabBody` because the rows are
 * not a §4 listing: the backend correlates two reads to produce them, and both
 * of those reads are primary. That is the deliberate part. Degrading a failed
 * *policy* listing to nulls would render a full table of pods above a headline
 * count of unprotected ones, assembled entirely from a read that failed — so
 * the endpoint fails instead, and this component shows the error where the
 * number would have been.
 */
function PodIsolation() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const [search, setSearch] = useState('');

  const view = useAsync(() => networkApi.isolation({ namespace }), {
    key: `network-isolation:${activeClusterId}:${namespace ?? '*'}`,
  });
  const data = view.data;
  const summary = data?.summary;
  const scope = namespace ? `namespace ${namespace}` : 'the cluster';

  const columns = useMemo(
    () => [
      { key: 'name', title: 'Pod', sortable: true },
      ...(namespace ? [] : [NAMESPACE_COLUMN]),
      {
        key: 'ingress',
        title: 'Ingress',
        sortable: true,
        value: (row) => (row.ingress?.isolated == null ? 'Unknown' : directionLabelFor(row.ingress)),
        cell: (row) => <IsolationCell state={row.ingress} kind="Ingress" />,
        facet: { options: ['Unrestricted', 'Deny all', 'Restricted', 'Allow all', 'Governed', 'Unknown'] },
      },
      {
        key: 'egress',
        title: 'Egress',
        sortable: true,
        value: (row) => (row.egress?.isolated == null ? 'Unknown' : directionLabelFor(row.egress)),
        cell: (row) => <IsolationCell state={row.egress} kind="Egress" />,
        facet: { options: ['Unrestricted', 'Deny all', 'Restricted', 'Allow all', 'Governed', 'Unknown'] },
      },
      {
        key: 'policies',
        title: 'Selected by',
        value: (row) => (row.policies ?? []).join(' '),
        cell: (row) => <ChipList values={row.policies} max={3} emptyText="nothing" />,
      },
      {
        key: 'host_network',
        title: 'Host network',
        sortable: true,
        value: (row) => (row.host_network ? 'yes' : 'no'),
        cell: (row) =>
          row.host_network ? (
            <StatusBadge
              status="warning"
              label="hostNetwork"
              tooltip="This pod shares the node's network namespace. Most CNI plugins do not apply NetworkPolicy to such pods — but which ones is a property of the plugin, and this console cannot see it."
            />
          ) : (
            <Muted>no</Muted>
          ),
      },
      {
        key: 'phase',
        title: 'Phase',
        sortable: true,
        cell: (row) => <StatusBadge status={row.phase} detail={row.phase_detail} />,
      },
    ],
    [namespace],
  );

  return (
    <>
      <EnforcementNotice />
      <PartialBanner unavailable={data?.unavailable} />

      {summary && (
        <StatGrid>
          <MetricCard
            label="Pods"
            value={summary.pod_count}
            sub={`in ${scope}`}
            help="Every pod the listing returned, whatever its phase."
          />
          <MetricCard
            label="Policies"
            value={data.policy_count}
            help="NetworkPolicy objects in scope. A namespace with none leaves every pod in it unrestricted."
          />
          <MetricCard
            label="Ingress unrestricted"
            value={summary.ingress.unrestricted}
            accent={summary.ingress.unrestricted ? 'warning' : 'success'}
            sub={summary.ingress.unknown ? `${summary.ingress.unknown} unknown` : undefined}
            help="Pods that no policy both selects and governs inbound for. Kubernetes defaults to allow, so these accept traffic from anywhere in the cluster."
          />
          <MetricCard
            label="Egress unrestricted"
            value={summary.egress.unrestricted}
            accent={summary.egress.unrestricted ? 'warning' : 'success'}
            sub={summary.egress.unknown ? `${summary.egress.unknown} unknown` : undefined}
            help="Pods no policy governs outbound for. Unrestricted egress is the ordinary default and is only a finding if this namespace was meant to be locked down."
          />
        </StatGrid>
      )}

      <Toolbar ariaLabel="Pod isolation controls">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter pods…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>{namespace ? `Namespace: ${namespace}` : 'All namespaces'}</Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh pod isolation" icon={<SyncAltIcon />} onClick={view.reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Pod isolation"
        tableId="network:isolation"
        manageableColumns
        columns={columns}
        rows={data?.items ?? []}
        rowKey={(row) => `${row.namespace}/${row.name}`}
        loading={view.loading && !data}
        error={view.error}
        onRetry={view.reload}
        filterText={search}
        emptyTitle="No pods here"
        emptyDescription={`The listing succeeded and ${scope} holds no pods, so there is nothing for a NetworkPolicy to select.`}
      />
    </>
  );
}

/** The sortable/filterable label for one pod's coverage in a direction. */
function directionLabelFor(state) {
  if (state?.isolated == null) return 'Unknown';
  if (!state.isolated) return 'Unrestricted';
  if (!state.effect) return 'Governed';
  return directionLabel({ governed: true, effect: state.effect });
}

/* ── The three listings this page opened with ───────────────────────────── */

const SERVICES_TAB = {
  key: 'services',
  title: 'Services',
  group: 'core',
  version: 'v1',
  plural: 'services',
  namespaced: true,
  rowKey: (row) => `${row.namespace}/${row.name}`,
  emptyDescription: 'The listing succeeded and returned no Services in this scope.',
  detailTitle: (row) => `Service ${row?.namespace}/${row?.name}`,
  columns: [
    { key: 'name', title: 'Name', sortable: true },
    NAMESPACE_COLUMN,
    {
      key: 'type',
      title: 'Type',
      sortable: true,
      facet: { options: ['ClusterIP', 'NodePort', 'LoadBalancer', 'ExternalName'] },
    },
    {
      key: 'clusterIP',
      title: 'Cluster IP',
      cell: (row) => (
        <NullableCell
          value={row.clusterIP}
          reason="A headless Service (clusterIP: None) has no cluster IP; a Service still being assigned one has not got there yet."
        />
      ),
    },
    {
      key: 'externalIPs',
      title: 'External',
      value: (row) => (row.externalIPs ?? []).join(' '),
      cell: (row) => <ChipList values={row.externalIPs} max={2} emptyText="none" />,
    },
    {
      key: 'ports',
      title: 'Ports',
      value: (row) => (row.ports ?? []).map((p) => p.port).join(','),
      cell: (row) => (
        <ChipList
          values={(row.ports ?? []).map(
            (p) =>
              `${p.port}${p.targetPort != null ? `→${p.targetPort}` : ''}/${p.protocol ?? 'TCP'}` +
              (p.nodePort ? ` (node ${p.nodePort})` : ''),
          )}
          max={2}
          emptyText="none"
        />
      ),
    },
    {
      key: 'endpoint_count',
      title: 'Endpoints',
      sortable: true,
      cell: (row) => (
        <NullableCell
          value={row.endpoint_count}
          reason="The EndpointSlices for this Service could not be read, so how many backends it has is unknown — not zero."
        />
      ),
    },
    { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
  ],
  detail: (row) => (
    <>
      <DescriptionList
        items={[
          { label: 'Type', value: row.type },
          { label: 'Cluster IP', value: row.clusterIP ?? null },
          {
            label: 'Backends',
            value: (
              <NullableCell
                value={row.endpoint_count}
                reason="The EndpointSlices could not be read, so this is unknown rather than zero."
              />
            ),
          },
          {
            label: 'Selector',
            value: (
              <ChipList
                values={Object.entries(row.selector ?? {}).map(([k, v]) => `${k}=${v}`)}
                max={5}
                emptyText="none — this Service is backed by hand-managed EndpointSlices"
              />
            ),
          },
          {
            label: 'External addresses',
            value: <ChipList values={row.externalIPs} max={5} emptyText="none" />,
          },
        ]}
      />
      <YamlPanel group="core" version="v1" plural="services" name={row.name} namespace={row.namespace} height={320} />
    </>
  ),
};

const INGRESSES_TAB = {
  key: 'ingresses',
  title: 'Ingresses',
  group: 'networking.k8s.io',
  version: 'v1',
  plural: 'ingresses',
  namespaced: true,
  rowKey: (row) => `${row.namespace}/${row.name}`,
  emptyDescription:
    'No Ingresses in this scope. A cluster with no ingress controller serves the API but has nothing to list.',
  detailTitle: (row) => `Ingress ${row?.namespace}/${row?.name}`,
  columns: [
    { key: 'name', title: 'Name', sortable: true },
    NAMESPACE_COLUMN,
    {
      key: 'class',
      title: 'Class',
      cell: (row) => (
        <NullableCell
          value={row.class}
          reason="Neither spec.ingressClassName nor the legacy kubernetes.io/ingress.class annotation is set, so which controller serves this is decided by the cluster default."
        />
      ),
    },
    {
      key: 'hosts',
      title: 'Hosts',
      value: (row) => (row.rules ?? []).map((rule) => rule.host).join(' '),
      cell: (row) => (
        <ChipList
          values={(row.rules ?? []).map((rule) => rule.host || '*')}
          max={2}
          emptyText="none"
        />
      ),
    },
    {
      key: 'tls_hosts',
      title: 'TLS',
      value: (row) => (row.tls_hosts ?? []).length,
      cell: (row) =>
        (row.tls_hosts ?? []).length ? (
          <ChipList values={row.tls_hosts} max={1} color="green" />
        ) : (
          <Muted>none</Muted>
        ),
    },
    {
      key: 'address',
      title: 'Address',
      cell: (row) => (
        <NullableCell
          value={row.address}
          reason="The ingress controller has not published an address for this Ingress yet."
        />
      ),
    },
    { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
  ],
  detail: (row) => (
    <>
      <DescriptionList
        items={[
          { label: 'Class', value: row.class ?? null },
          { label: 'Address', value: row.address ?? null },
          { label: 'TLS hosts', value: <ChipList values={row.tls_hosts} max={6} emptyText="none" /> },
        ]}
      />
      {(row.rules ?? []).map((rule, index) => (
        <div key={`${rule.host ?? '*'}-${index}`} style={{ marginTop: '0.75rem' }}>
          <strong>{rule.host || 'any host'}</strong>
          <ul>
            {(rule.paths ?? []).map((path, i) => (
              <li key={`${path.path ?? '/'}-${i}`}>
                <code>{path.path || '/'}</code> <Muted>({path.pathType || 'ImplementationSpecific'})</Muted> →{' '}
                {path.service ? (
                  <ResourceLink
                    group=""
                    version="v1"
                    plural="services"
                    name={path.service}
                    namespace={row.namespace}
                  >
                    {`${path.service}:${path.port ?? '?'}`}
                  </ResourceLink>
                ) : (
                  <NullableCell value={null} reason="This path has no Service backend; it routes to a resource reference." />
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      <YamlPanel
        group="networking.k8s.io"
        version="v1"
        plural="ingresses"
        name={row.name}
        namespace={row.namespace}
        height={320}
      />
    </>
  ),
};

const ENDPOINTS_TAB = {
  key: 'endpoints',
  title: 'Endpoints',
  group: 'core',
  version: 'v1',
  plural: 'endpoints',
  namespaced: true,
  // Raw manifests: §8 defines no typed Endpoints row, and `shape: raw`
  // says so out loud rather than relying on the absence of a shaper.
  shape: 'raw',
  rowKey: (row) => `${objectNamespace(row)}/${objectName(row)}`,
  emptyDescription: 'No Endpoints objects in this scope.',
  detailTitle: (row) => `Endpoints ${objectNamespace(row)}/${objectName(row)}`,
  columns: [
    {
      key: 'name',
      title: 'Name',
      sortable: true,
      value: (row) => objectName(row),
      cell: (row) => (
        // An Endpoints object always shares its name with its Service, so
        // linking there is the useful move: "who owns these addresses".
        <ResourceLink
          group=""
          version="v1"
          plural="services"
          name={objectName(row)}
          namespace={objectNamespace(row)}
        />
      ),
    },
    { key: 'namespace', title: 'Namespace', sortable: true, value: (row) => objectNamespace(row) },
    {
      key: 'ready',
      title: 'Ready addresses',
      sortable: true,
      value: (row) => endpointTallies(row).ready,
      cell: (row) => {
        const { ready } = endpointTallies(row);
        return <StatusBadge status={ready ? 'Ready' : 'NotReady'} label={String(ready)} tooltip={ready ? undefined : 'This Service has no ready backends: traffic to it is being dropped.'} />;
      },
    },
    {
      key: 'notReady',
      title: 'Not ready',
      sortable: true,
      value: (row) => endpointTallies(row).notReady,
      cell: (row) => {
        const { notReady } = endpointTallies(row);
        return notReady ? (
          <StatusBadge
            status="Warning"
            label={String(notReady)}
            tooltip="These pods exist but are failing their readiness probe, so the Service will not route to them."
          />
        ) : (
          <Muted>0</Muted>
        );
      },
    },
    {
      key: 'ports',
      title: 'Ports',
      value: (row) => endpointTallies(row).ports.join(','),
      cell: (row) => <ChipList values={endpointTallies(row).ports} max={3} emptyText="none" />,
    },
    {
      key: 'age',
      title: 'Age',
      sortable: true,
      value: (row) => objectAgeSeconds(row),
      cell: (row) => <AgeCell seconds={objectAgeSeconds(row)} timestamp={row?.metadata?.creationTimestamp} />,
    },
  ],
  detail: (row) => (
    <YamlPanel
      group="core"
      version="v1"
      plural="endpoints"
      name={objectName(row)}
      namespace={objectNamespace(row)}
      height={420}
    />
  ),
};

/* ── Page ───────────────────────────────────────────────────────────────── */

export default function Network() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();

  const [editTarget, setEditTarget] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [creating, setCreating] = useState(false);

  // Scoped to the namespace in the masthead, like the Explorer's. A check with
  // no namespace asks whether the caller may write NetworkPolicies *anywhere*,
  // and an operator with the grant in one namespace would see every button
  // disabled with a reason that is true about the cluster and wrong about them.
  const checks = useMemo(() => policyChecks(namespace), [namespace]);
  const { gate } = useGates(checks);

  // Bumped after any write, and threaded into the tab's key so the listing is
  // read again. One mechanism for all three writes rather than two: the create
  // button is page-level and has no row to take a `reload` from, and a table
  // that keeps showing a policy somebody just deleted is worse than a table that
  // also forgets the search box. The remount closes the drawer too, which is
  // what should happen to a panel describing an object that no longer exists.
  const [refreshToken, setRefreshToken] = useState(0);
  const finish = useCallback(() => {
    setEditTarget(null);
    setDeleteTarget(null);
    setCreating(false);
    setRefreshToken((token) => token + 1);
  }, []);

  const tabs = useMemo(
    () => [
      SERVICES_TAB,
      INGRESSES_TAB,
      ENDPOINTS_TAB,

      genericTab({
        key: 'endpointslices',
        title: 'Endpoint Slices',
        group: 'discovery.k8s.io',
        version: 'v1',
        plural: 'endpointslices',
        namespaced: true,
      }),
      genericTab({
        key: 'ingressclasses',
        title: 'Ingress Classes',
        group: 'networking.k8s.io',
        version: 'v1',
        plural: 'ingressclasses',
        namespaced: false,
      }),

      {
        key: 'networkpolicies',
        title: 'Network Policies',
        ...POLICY_GVP,
        namespaced: true,
        refreshToken,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        notice: <EnforcementNotice />,
        emptyDescription:
          'The listing succeeded and this scope holds no NetworkPolicies. Every pod in it is ' +
          'therefore unrestricted — Kubernetes permits all traffic to a pod no policy selects.',
        detailTitle: (row) => `NetworkPolicy ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          NAMESPACE_COLUMN,
          {
            key: 'applies_to',
            title: 'Applies to',
            value: (row) =>
              row.selects_all_pods ? 'all pods' : selectorTerms(row.pod_selector).join(' '),
            cell: (row) =>
              row.selects_all_pods === true ? (
                <strong title="An empty podSelector selects every pod in the namespace.">All pods</strong>
              ) : row.selects_all_pods === false ? (
                <ChipList values={selectorTerms(row.pod_selector)} max={2} />
              ) : (
                <NullableCell
                  value={null}
                  reason="This object carries no spec.podSelector, so which pods it applies to cannot be read off it."
                />
              ),
          },
          {
            key: 'ingress',
            title: 'Ingress',
            sortable: true,
            value: (row) => directionLabel(row.ingress),
            cell: (row) => <DirectionCell direction={row.ingress} />,
            facet: { options: ['Not governed', 'Deny all', 'Restricted', 'Allow all'] },
          },
          {
            key: 'egress',
            title: 'Egress',
            sortable: true,
            value: (row) => directionLabel(row.egress),
            cell: (row) => <DirectionCell direction={row.egress} />,
            facet: { options: ['Not governed', 'Deny all', 'Restricted', 'Allow all'] },
          },
          {
            key: 'age_seconds',
            title: 'Age',
            sortable: true,
            cell: (row) => <AgeCell seconds={row.age_seconds} />,
          },
        ],
        actions: (row) => [
          menuAction('Edit YAML…', gate('update'), () => setEditTarget(row)),
          menuAction('Delete…', gate('delete'), () => setDeleteTarget(row), { isDanger: true }),
        ],
        detail: (row) => (
          <>
            <PolicyDetail row={row} />
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem' }}>
              <ActionButton gate={gate('update')} onClick={() => setEditTarget(row)}>
                Edit YAML…
              </ActionButton>
              <ActionButton gate={gate('delete')} isDanger onClick={() => setDeleteTarget(row)}>
                Delete…
              </ActionButton>
            </div>
          </>
        ),
      },
      {
        key: 'isolation',
        title: 'Pod Isolation',
        render: () => <PodIsolation />,
      },
    ],
    [gate, refreshToken],
  );

  if (activeClusterId == null) {
    return <NoClusterState what="Networking" />;
  }

  return (
    <>
      <ResourceTabsPage
        title="Network"
        subtitle="Services, Ingresses, the addresses behind them, and the NetworkPolicies that describe who may reach what. This console reads no service mesh — what is here is what the API server serves."
        actions={(activeKey) =>
          activeKey === 'networkpolicies' ? (
            <ActionButton gate={gate('create')} onClick={() => setCreating(true)}>
              New network policy…
            </ActionButton>
          ) : null
        }
        tabs={tabs}
      />

      {creating && (
        <ImportYamlDialog
          isOpen
          title="New network policy"
          initialText={POLICY_TEMPLATE}
          onClose={() => setCreating(false)}
          onApplied={finish}
        />
      )}

      {editTarget && (
        <EditYamlDialog
          isOpen
          {...POLICY_GVP}
          name={editTarget.name}
          namespace={editTarget.namespace}
          kind="NetworkPolicy"
          onClose={() => setEditTarget(null)}
          onApplied={finish}
        />
      )}

      {deleteTarget && (
        <DeleteDialog
          isOpen
          {...POLICY_GVP}
          name={deleteTarget.name}
          namespace={deleteTarget.namespace}
          kind="NetworkPolicy"
          onClose={() => setDeleteTarget(null)}
          onApplied={finish}
        />
      )}
    </>
  );
}
