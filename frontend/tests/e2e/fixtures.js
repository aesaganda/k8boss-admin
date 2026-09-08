/**
 * The hermetic fixture set and the `/api/**` router every e2e spec runs against.
 *
 * Extracted from `smoke.spec.js` when a second spec file needed it. Kept as one
 * module rather than copied, because two divergent copies of an "empty is never
 * blind" fixture is how one of them quietly starts returning `0` where the other
 * returns `null` — and then the suite passes while asserting the wrong contract.
 *
 * Every fixture that carries a `null` numeric or a populated `unavailable[]`
 * does so on purpose. See the comments on each.
 */
import { expect } from '@playwright/test';

export const OVERVIEW_UNAVAILABLE = [
  {
    group: '',
    resource: 'nodes',
    namespace: null,
    reason: 'forbidden',
    detail:
      'nodes is forbidden: User "system:serviceaccount:k8boss-admin:console" cannot list resource "nodes" at the cluster scope',
  },
  {
    group: 'metrics.k8s.io',
    resource: 'nodemetrics',
    namespace: null,
    reason: 'unsupported',
    detail: 'the server could not find the requested resource',
  },
];

/**
 * `metadata.name` out of a posted manifest, without a YAML parser.
 *
 * Only the create mock uses it, and only to echo a plausible `target.name` back
 * — the assertions that matter read the manifest the dialog sent, not this. It
 * walks from the `metadata:` line rather than taking the first `name:` in the
 * document, because a pod template has one too and it is two levels deeper.
 */
function manifestName(text) {
  const lines = String(text ?? '').split('\n');
  const start = lines.findIndex((line) => line.trimEnd() === 'metadata:');
  if (start < 0) return null;
  for (const line of lines.slice(start + 1)) {
    if (/^\S/.test(line)) break;
    const match = /^ {2}name:\s*(.+?)\s*$/.exec(line);
    if (match) return match[1].replace(/^["']|["']$/g, '');
  }
  return null;
}

/**
 * One object's manifest, as `GET .../{name}/yaml` returns it: `text/plain`, and
 * carrying a `resourceVersion`, which is what every concurrency assertion in
 * the suite is actually about.
 */
export function objectYaml({
  kind = 'Pod',
  name = 'checkout-7d9f8b6c4-hk2xv',
  namespace = 'prod',
  resourceVersion = '884213',
  image = 'registry.example:5000/checkout:1.4.2',
  replicas = null,
} = {}) {
  return [
    'apiVersion: v1',
    `kind: ${kind}`,
    'metadata:',
    `  name: ${name}`,
    `  namespace: ${namespace}`,
    `  resourceVersion: "${resourceVersion}"`,
    '  labels:',
    '    app: checkout',
    'spec:',
    ...(replicas == null ? [] : [`  replicas: ${replicas}`]),
    '  nodeName: ip-10-0-1-4',
    '  containers:',
    '    - name: app',
    `      image: ${image}`,
    '      ports:',
    '        - containerPort: 8080',
    '  restartPolicy: Always',
    'status:',
    '  phase: Running',
    '  podIP: 10.128.4.17',
    '',
  ].join('\n');
}

/**
 * §16 `ChannelPayload`, as `channel_payload` in backend/app/services/portal.py
 * emits it.
 *
 * Hoisted because the plan fixture needs it three times over — in `channels[]`,
 * as `selected`, and as the older channel built by spreading it — and three
 * hand-copied literals is how one of them ends up with `installModes: []` where
 * the others have `null`. Those two are the tri-state this whole feature turns
 * on: `[]` is a catalog that published modes and supports none, `null` is a
 * catalog that published none at all.
 */
const PORTAL_BETA_CHANNEL = {
  name: 'beta',
  currentCSV: 'prometheusoperator.0.71.2',
  version: '0.71.2',
  displayName: 'Prometheus Operator',
  summary: 'Run and configure Prometheus, Alertmanager and their rules as Kubernetes objects.',
  description:
    'The Prometheus Operator creates, configures and manages Prometheus clusters.\n\n' +
    'It introduces the Prometheus, Alertmanager and ServiceMonitor kinds and keeps the ' +
    'generated configuration in step with them.',
  installModes: [
    { type: 'OwnNamespace', supported: true },
    { type: 'SingleNamespace', supported: true },
    // Not filtered out: rule 11.4 needs the reason a mode is unavailable on
    // screen, and an absent row carries none.
    { type: 'MultiNamespace', supported: false },
    { type: 'AllNamespaces', supported: true },
  ],
  minKubeVersion: '1.21.0',
  ownedCustomResources: [
    {
      kind: 'Prometheus',
      name: 'prometheuses.monitoring.coreos.com',
      version: 'v1',
      description: 'A running Prometheus instance and the objects it scrapes.',
    },
    {
      kind: 'Alertmanager',
      name: 'alertmanagers.monitoring.coreos.com',
      version: 'v1',
      description: 'An Alertmanager cluster.',
    },
  ],
  containerImage: 'quay.io/prometheus-operator/prometheus-operator:v0.71.2',
  repository: 'https://github.com/prometheus-operator/prometheus-operator',
  capabilityLevel: 'Deep Insights',
  certified: false,
  categories: ['Monitoring', 'Logging & Tracing'],
  provider: 'Red Hat',
};

export const FIXTURES = {
  authConfig: {
    enabled: false,
    localEnabled: true,
    ldapEnabled: false,
    oidcEnabled: false,
    methods: ['local'],
    ssoProviders: [],
    oidc: null,
  },

  health: {
    status: 'ok',
    version: '0.1.0',
    mutations: 'enabled',
    clusters: { registered: 1, reachable: 1 },
    degraded: [],
  },

  clusters: {
    items: [
      {
        id: 1,
        name: 'prod-eu',
        platform: 'kubernetes',
        api_server: 'https://api.prod-eu.example:6443',
        authentication_type: 'service_account_token',
        has_ca_certificate: true,
        skip_tls_verify: false,
        // ADR-0007. Present and false, like the backend's own ClusterPublic:
        // the setting decides who the cluster thinks is asking, so a fixture
        // that omitted it would let a form which never reads it pass.
        impersonation_enabled: false,
        status: 'connected',
        server_version: 'v1.31.4',
        last_connected: '2026-08-18T09:03:11Z',
        created_at: '2026-06-02T10:00:00Z',
        updated_at: '2026-08-01T12:41:09Z',
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  // The important fixture. `capacity` and `requested` are null — §3 says each
  // sub-object is collected independently and a failed collector nulls that key
  // — and `unavailable` names why. A page that renders "0 cores" here is the
  // defect this suite exists to catch.
  overview: {
    server_version: 'v1.31.4',
    platform: 'kubernetes',
    nodes: { total: 12, ready: 11, unschedulable: 1 },
    namespaces: 34,
    workloads: { deployments: 210, statefulsets: 12, daemonsets: 8, jobs: 41, cronjobs: 9 },
    pods: { total: 1204, running: 1180, pending: 8, failed: 3, succeeded: 13 },
    capacity: null,
    requested: null,
    unavailable: OVERVIEW_UNAVAILABLE,
  },

  namespaces: {
    items: [
      {
        name: 'prod',
        status: 'Active',
        labels: {},
        annotations: {},
        age_seconds: 8123456,
        pod_count: 62,
        creationTimestamp: '2026-01-04T08:00:00Z',
      },
      {
        name: 'kube-system',
        status: 'Active',
        labels: {},
        annotations: {},
        age_seconds: 8123999,
        // Null, not zero: the pod listing for this namespace was denied.
        pod_count: null,
        creationTimestamp: '2026-01-04T07:59:00Z',
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  nodes: {
    items: [
      {
        name: 'ip-10-0-1-4',
        ready: true,
        unschedulable: false,
        roles: ['worker'],
        kubelet_version: 'v1.31.4',
        os_image: 'Amazon Linux 2',
        container_runtime: 'containerd://1.7.13',
        internal_ip: '10.0.1.4',
        age_seconds: 8123456,
        capacity: { cpu_cores: 16, memory_bytes: 68719476736, pods: 110 },
        allocatable: { cpu_cores: 15.8, memory_bytes: 66571993088, pods: 110 },
        requested: null,
        pod_count: null,
        conditions: [{ type: 'MemoryPressure', status: 'False', reason: 'KubeletHasSufficientMemory' }],
        taints: [],
      },
    ],
    continue: null,
    remaining: null,
    partial: true,
    unavailable: [
      {
        group: '',
        resource: 'pods',
        namespace: null,
        reason: 'forbidden',
        detail: 'pods is forbidden: cannot list resource "pods" at the cluster scope',
      },
    ],
  },

  workloads: {
    items: [
      {
        kind: 'Deployment',
        name: 'checkout',
        namespace: 'prod',
        replicas: { desired: 5, ready: 4, updated: 5, available: 4 },
        images: ['ghcr.io/acme/checkout:1.9.2'],
        selector: { app: 'checkout' },
        labels: {},
        age_seconds: 1209600,
        status: 'Progressing',
        status_reason: '1 of 5 replicas not available',
        restarts_24h: null,
        suspended: null,
        schedule: null,
        last_schedule: null,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  users: {
    items: [
      {
        id: 7,
        username: 'directory.admin',
        display_name: 'Directory Admin',
        email: 'directory.admin@example.test',
        role: 'admin',
        auth_source: 'ldap',
        active: true,
        last_login: '2026-08-18T12:00:00Z',
        created_at: '2026-08-18T12:00:00Z',
        updated_at: '2026-08-18T12:00:00Z',
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  // §10. Two record kinds in one table on purpose: a cluster write and a refused
  // sign-in. The console record carries `cluster_id: null`, which is what makes
  // the scope filter's third position necessary — without it these rows are
  // unreachable from a session that has a cluster selected.
  audit: {
    items: [
      {
        id: 4472,
        ts: '2026-08-18T09:15:44Z',
        category: 'console',
        actor: 'someone-guessing',
        cluster_id: null,
        cluster_name: null,
        verb: 'login',
        target: {
          group: 'k8boss-admin.io', version: 'v1', resource: 'sessions',
          namespace: null, name: 'someone-guessing', subresource: null,
        },
        dry_run: false,
        outcome: 'denied',
        detail: 'Sign-in rejected (auto).',
        diff_digest: null,
        error: null,
        source_ip: '10.4.2.9',
        prev_hash: 'b'.repeat(64),
        event_hash: 'c'.repeat(64),
      },
      {
        id: 4471,
        ts: '2026-08-18T09:14:03Z',
        category: 'cluster',
        actor: 'erens',
        cluster_id: 1,
        cluster_name: 'prod-eu',
        verb: 'patch',
        target: {
          group: 'apps', version: 'v1', resource: 'deployments',
          namespace: 'prod', name: 'checkout', subresource: null,
        },
        dry_run: false,
        outcome: 'applied',
        detail: 'replicas 3 -> 5',
        diff_digest: 'sha256:9f2c1188aa',
        error: null,
        source_ip: '10.4.2.9',
        prev_hash: 'a'.repeat(64),
        event_hash: 'b'.repeat(64),
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  // §10.2, the interesting verdict. `partial`, not `intact`: 12 records predate
  // the hash chain and are never back-filled, so the console reports a count and
  // withholds the verdict. A UI that renders this as a pass is the defect.
  auditVerify: {
    status: 'partial',
    verified: 4460,
    unchained: 12,
    total: 4472,
    anchored: true,
    first_break: null,
    tip: 'c'.repeat(64),
    genesis: '0'.repeat(64),
    window: { requested_limit: null, oldest_unchained_id: 1 },
  },

  auditVerifyBroken: {
    status: 'broken',
    verified: 88,
    unchained: 0,
    total: 4472,
    anchored: true,
    first_break: { id: 89, reason: 'event_hash mismatch: this row\u2019s content was modified' },
    tip: null,
    genesis: '0'.repeat(64),
    window: { requested_limit: null, oldest_unchained_id: null },
  },

  // Enough of §4's catalog for `ImportYamlDialog` to resolve a pasted
  // `apiVersion` + `kind` into a route. `apiVersion` is precomputed on the item
  // exactly as `_resource_item` computes it, because that single equality is
  // what the dialog matches on.
  catalog: {
    items: [
      {
        group: 'apps',
        version: 'v1',
        kind: 'Deployment',
        resource: 'deployments',
        namespaced: true,
        verbs: ['get', 'list', 'create', 'update', 'patch', 'delete'],
        shortNames: ['deploy'],
        categories: ['all'],
        apiVersion: 'apps/v1',
        preferred: true,
      },
      {
        group: '',
        version: 'v1',
        kind: 'ConfigMap',
        resource: 'configmaps',
        namespaced: true,
        verbs: ['get', 'list', 'create', 'update', 'patch', 'delete'],
        shortNames: ['cm'],
        categories: [],
        apiVersion: 'v1',
        preferred: true,
      },
      // The rest of what §11.9's form view can project. Without them the dialog
      // resolves no route for a Pod or a CronJob and refuses to preview, which
      // would make every form assertion below pass against a disabled button.
      ...[
        ['', 'v1', 'Pod', 'pods', ['po']],
        ['apps', 'v1', 'StatefulSet', 'statefulsets', ['sts']],
        ['apps', 'v1', 'DaemonSet', 'daemonsets', ['ds']],
        ['apps', 'v1', 'ReplicaSet', 'replicasets', ['rs']],
        ['batch', 'v1', 'Job', 'jobs', []],
        ['batch', 'v1', 'CronJob', 'cronjobs', ['cj']],
        ['networking.k8s.io', 'v1', 'NetworkPolicy', 'networkpolicies', ['netpol']],
      ].map(([group, version, kind, resource, shortNames]) => ({
        group,
        version,
        kind,
        resource,
        namespaced: true,
        verbs: ['get', 'list', 'create', 'update', 'patch', 'delete'],
        shortNames,
        categories: [],
        apiVersion: group ? `${group}/${version}` : version,
        preferred: true,
      })),
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  // §6 pod rows. Two, and the second is the reason this page exists: `phase` is
  // Running and the container is not, so the Status column has to show the
  // detail rather than the phase.
  pods: {
    items: [
      {
        name: 'checkout-7d9f8b6c4-hk2xv',
        namespace: 'prod',
        phase: 'Running',
        phase_detail: null,
        ready: '1/1',
        restarts: 0,
        node: 'ip-10-0-1-4',
        qos_class: 'Burstable',
        // `kind` per §6: the Debug tab uses it to tell an application
        // container from an attached debug one, and it must not offer a
        // debug container as a process-namespace target.
        containers: [
          { name: 'app', image: 'registry.example:5000/checkout:1.4.2', ready: true, restart_count: 0, kind: 'container' },
        ],
        age_seconds: 86400,
        creationTimestamp: '2026-08-18T09:00:00Z',
      },
      {
        name: 'payments-5c8d9f7b6-qq4mn',
        namespace: 'prod',
        phase: 'Running',
        phase_detail: 'CrashLoopBackOff',
        ready: '0/1',
        // Null, not zero: the restart count for this pod could not be read, and
        // a pod in CrashLoopBackOff showing "0 restarts" is the wrong answer
        // delivered confidently.
        restarts: null,
        node: 'ip-10-0-1-5',
        qos_class: 'Burstable',
        containers: [
          { name: 'app', image: 'registry.example:5000/payments:2.0.1', ready: false, restart_count: null, kind: 'container' },
        ],
        age_seconds: 3600,
        creationTimestamp: '2026-08-19T08:00:00Z',
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  // §7.5 the pod detail: the §6 row plus what a table has no room for. The
  // container carries `last_terminated` because that is the whole reason this
  // endpoint exists — "restarted 3 times" is the symptom and `OOMKilled` is the
  // answer — and `envoy` declares no resources at all, which must render as a
  // named absence rather than as `cpu 0`.
  podDetail: {
    name: 'checkout-7d9f8b6c4-hk2xv',
    namespace: 'prod',
    node: 'ip-10-0-1-4',
    phase: 'Running',
    phase_detail: null,
    ready: '1/2',
    restarts: 3,
    age_seconds: 86400,
    ip: '10.128.4.17',
    host_ip: '10.0.1.4',
    qos_class: 'Burstable',
    owner: { kind: 'ReplicaSet', name: 'checkout-7d9f8b6c4' },
    containers: [
      {
        name: 'app',
        image: 'registry.example:5000/checkout:1.4.2',
        ready: true,
        restarts: 3,
        state: 'Running',
        reason: null,
        kind: 'container',
        image_id: 'docker-pullable://registry.example:5000/checkout@sha256:aaa',
        container_id: 'containerd://f1e2d3',
        started_at: '2026-08-18T09:00:20Z',
        last_terminated: {
          reason: 'OOMKilled',
          exit_code: 137,
          signal: null,
          started_at: '2026-08-18T08:59:00Z',
          finished_at: '2026-08-18T09:00:18Z',
          message: null,
        },
        ports: [{ name: 'http', container_port: 8080, protocol: 'TCP', host_port: null }],
        requests: { cpu: '250m', memory: '256Mi' },
        limits: { cpu: '1', memory: '512Mi' },
        command: null,
        args: null,
      },
      {
        name: 'envoy',
        image: 'envoyproxy/envoy:v1.29',
        ready: false,
        restarts: 0,
        state: 'Waiting',
        reason: 'CrashLoopBackOff',
        kind: 'container',
        image_id: null,
        container_id: null,
        started_at: null,
        last_terminated: null,
        ports: [],
        // Null, not `{}`: this container declares nothing, and `cpu 0` would
        // describe a BestEffort container as one that asked for nothing and got
        // it — when what actually happens is that it is evicted first.
        requests: null,
        limits: null,
        command: null,
        args: null,
      },
    ],
    init_containers: [
      {
        name: 'migrate',
        image: 'registry.example:5000/migrate:1.4.2',
        ready: true,
        restarts: 0,
        // Terminated with exit 0 is a *success* for an init container, which is
        // why they are a separate list from the app containers.
        state: 'Terminated',
        reason: 'Completed',
        kind: 'init',
        image_id: null,
        container_id: null,
        started_at: null,
        last_terminated: null,
        ports: [],
        requests: null,
        limits: null,
        command: null,
        args: null,
      },
    ],
    conditions: [
      {
        type: 'Ready',
        // Not `False`. The kubelet has said nothing, which is what a node that
        // stopped reporting looks like — and it must not render as "no".
        status: 'Unknown',
        reason: null,
        message: 'kubelet stopped posting node status',
        last_transition: '2026-08-19T08:00:00Z',
      },
      {
        type: 'Initialized',
        status: 'True',
        reason: null,
        message: null,
        last_transition: '2026-08-18T09:00:10Z',
      },
    ],
    volumes: [
      { name: 'config', kind: 'configMap', source: 'checkout-config' },
      { name: 'data', kind: 'persistentVolumeClaim', source: 'checkout-data' },
    ],
    uid: '5b1f7c0e-0f0a-4c1b-9a2e-b1d2c3e4f5a6',
    resource_version: '884213',
    created_at: '2026-08-18T09:00:00Z',
    deleted_at: null,
    labels: { app: 'checkout' },
    annotations: { 'kubectl.kubernetes.io/default-container': 'app' },
    service_account: 'checkout',
    restart_policy: 'Always',
    priority_class: null,
    node_selector: { 'kubernetes.io/os': 'linux' },
    host_network: false,
    start_time: '2026-08-18T09:00:04Z',
    status_reason: null,
    status_message: null,
    unavailable: [],
    partial: false,
  },

  // §7.6 the environment. One of each `value_state`, because the whole point of
  // the tab is that those four blanks are four different facts and must not
  // render as one empty cell.
  podEnvironment: {
    items: [
      {
        container: 'app',
        kind: 'container',
        variables: [
          {
            name: 'TIMEOUT',
            value: '30s',
            value_state: 'resolved',
            source: { kind: 'configMapRef', name: 'checkout-config', key: 'TIMEOUT', optional: null },
            all_keys: false,
            // Redefined below by an explicit `env` entry, so this is not what
            // the container sees — and a row shown as though it were in force
            // would be the wrong answer.
            overridden: true,
          },
          {
            name: 'LOG_LEVEL',
            value: 'info',
            value_state: 'literal',
            source: null,
            all_keys: false,
            overridden: false,
          },
          {
            name: 'TIMEOUT',
            value: '5s',
            value_state: 'literal',
            source: null,
            all_keys: false,
            overridden: false,
          },
          {
            name: 'DB_PASSWORD',
            // Never a value: this endpoint does not return Secret material,
            // whatever the console allows elsewhere.
            value: null,
            value_state: 'withheld',
            source: { kind: 'secretKeyRef', name: 'checkout-db', key: 'password', optional: null },
            all_keys: false,
            overridden: false,
          },
          {
            name: 'POD_IP',
            value: null,
            value_state: 'runtime',
            source: { kind: 'fieldRef', name: 'status.podIP', key: null, optional: null },
            all_keys: false,
            overridden: false,
          },
          {
            name: 'FEATURE_FLAGS',
            // Unknown, not empty. The ConfigMap was refused, which is named in
            // `unavailable` below.
            value: null,
            value_state: 'unreadable',
            source: { kind: 'configMapKeyRef', name: 'checkout-flags', key: 'flags', optional: null },
            all_keys: false,
            overridden: false,
          },
          {
            name: 'REGION',
            // A third kind of blank: the ConfigMap WAS read and has no such
            // key, so unless the reference is optional this container will not
            // start. A finding, not a missing reading.
            value: null,
            value_state: 'absent',
            source: { kind: 'configMapKeyRef', name: 'checkout-config', key: 'region', optional: null },
            all_keys: false,
            overridden: false,
          },
          {
            // The stand-in for a wholesale import whose object could not be
            // read: variables are coming from it and we cannot name them.
            name: null,
            value: null,
            value_state: 'unreadable',
            source: { kind: 'configMapRef', name: 'checkout-flags', key: null, optional: null },
            all_keys: true,
            overridden: false,
          },
        ],
      },
      { container: 'envoy', kind: 'container', variables: [] },
    ],
    continue: null,
    remaining: null,
    partial: true,
    unavailable: [
      {
        group: '',
        resource: 'configmaps',
        namespace: 'prod',
        reason: 'forbidden',
        detail: 'configmaps "checkout-flags" is forbidden: cannot get resource "configmaps" in namespace "prod"',
      },
    ],
  },

  // §7.7 the metrics, sampled. `envoy` is an app container missing from the
  // sample, which makes the pod total unknown rather than an understatement
  // presented as a fact; `migrate` has finished, so its missing sample is an
  // ordinary absence and is skipped by the arithmetic instead.
  // §31. The pod that will not schedule, with the three states the panel must
  // not collapse: the scheduler's own message, a node ruled out with reasons,
  // and a node whose capacity was not examined at all.
  podScheduling: {
    namespace: 'prod',
    pod: 'checkout-7d9f8b6c5d-abcde',
    phase: 'Pending',
    waiting_on: 'scheduler',
    node: null,
    pending_seconds: 1320,
    scheduled_condition: {
      status: 'False',
      reason: 'Unschedulable',
      message: '0/3 nodes are available.',
      last_transition: '2026-08-18T09:10:00Z',
    },
    scheduler: {
      reason: 'FailedScheduling',
      message:
        '0/3 nodes are available: 2 Insufficient cpu, 1 node(s) had untolerated taint {gpu: true}.',
      count: 14,
      last_seen: '2026-08-18T09:12:00Z',
      first_seen: '2026-08-18T09:02:00Z',
      age_seconds: 2400,
    },
    requests: { cpu_cores: 3, memory_bytes: 2147483648 },
    claims: [],
    nodes: [
      {
        name: 'ip-10-0-1-4',
        verdict: 'ruled_out',
        capacity_checked: true,
        reasons: [
          {
            code: 'node_insufficient_cpu',
            detail: 'Needs 3 cores; 1.2 of 4 allocatable are unrequested here.',
          },
        ],
      },
      {
        name: 'ip-10-0-1-9',
        verdict: 'ruled_out',
        capacity_checked: true,
        reasons: [
          {
            code: 'node_untolerated_taint',
            detail: 'Taint gpu=true:NoSchedule is not tolerated by this pod.',
          },
        ],
      },
      {
        name: 'ip-10-0-2-7',
        verdict: 'no_reason_found',
        capacity_checked: false,
        reasons: [],
      },
    ],
    unavailable: [],
    partial: false,
  },

  podMetrics: {
    items: [
      {
        container: 'migrate',
        kind: 'init',
        usage: null,
        sample_expected: false,
        requests: null,
        limits: null,
      },
      {
        container: 'app',
        kind: 'container',
        usage: { cpu_cores: 0.12, memory_bytes: 188743680 },
        sample_expected: true,
        requests: { cpu: '250m', memory: '256Mi' },
        limits: { cpu: '1', memory: '512Mi' },
      },
      {
        container: 'envoy',
        kind: 'container',
        usage: null,
        sample_expected: true,
        requests: null,
        limits: null,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    pod: { cpu_cores: null, memory_bytes: null },
    window_seconds: 30,
    timestamp: '2026-08-19T09:04:00Z',
  },

  // The same endpoint on a cluster with no metrics-server. A 200 with every
  // usage at null and an `unsupported` entry — §1.2's "not present on this
  // cluster", which the UI renders calmly and never as a zero.
  podMetricsUnsupported: {
    items: [
      {
        container: 'app',
        kind: 'container',
        usage: null,
        sample_expected: true,
        requests: { cpu: '250m', memory: '256Mi' },
        limits: { cpu: '1', memory: '512Mi' },
      },
    ],
    continue: null,
    remaining: null,
    partial: true,
    unavailable: [
      {
        group: 'metrics.k8s.io',
        resource: 'pods',
        namespace: 'prod',
        reason: 'unsupported',
        detail: 'This cluster does not serve metrics.k8s.io/v1beta1.',
      },
    ],
    pod: { cpu_cores: null, memory_bytes: null },
    window_seconds: null,
    timestamp: null,
  },

  // §7.4. Three fixtures for the three values of `supported`, because all
  // three render differently and collapsing any two is the bug: `false` says
  // this cluster cannot do it, `null` says we could not find out, and only one
  // of those is answered by upgrading a cluster.
  debugSupported: {
    items: [
      {
        name: 'debugger-x4k2p',
        image: 'busybox:1.36',
        targetContainer: 'app',
        command: null,
        tty: true,
        state: 'Running',
        reason: null,
        started_at: '2026-08-21T09:14:00Z',
      },
      {
        name: 'debugger-m7q3v',
        image: 'nicolaka/netshoot:v0.13',
        targetContainer: null,
        command: ['sleep', '3600'],
        tty: false,
        // Attached moments ago; the kubelet is still pulling. Not attachable,
        // and the panel must say why rather than offering a shell that fails.
        state: 'Waiting',
        reason: 'ContainerCreating',
        started_at: null,
      },
      {
        name: 'debugger-r8t5w',
        // A distinct image, so each row in this fixture is addressable on its
        // own — two rows sharing one image makes every `getByText` on it a
        // strict-mode violation waiting to happen.
        image: 'ghcr.io/acme/probe:2.1',
        targetContainer: null,
        command: ['sh', '-c', 'exit 1'],
        tty: false,
        // Ran and exited. `started_at` is populated: `startedAt` lives on the
        // terminated state too, and a row that said "Terminated" beside "Not
        // started" would be the console contradicting itself about one
        // container — and would send the operator looking for an image-pull
        // failure that did not happen.
        state: 'Terminated',
        reason: 'Error',
        started_at: '2026-08-21T09:10:00Z',
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    podContainers: ['app'],
    initContainers: [],
    supported: true,
    supportDetail: 'The API server serves pods/ephemeralcontainers with verbs: get, patch, update.',
  },

  debugUnsupported: {
    items: [],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    podContainers: ['app'],
    initContainers: [],
    supported: false,
    supportDetail:
      "This cluster's core API group does not serve pods/ephemeralcontainers. Ephemeral containers need " +
      'Kubernetes 1.16 or later, and the EphemeralContainers feature gate enabled before 1.23.',
  },

  debugSupportUnknown: {
    items: [],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    podContainers: ['app'],
    initContainers: [],
    // Not `false`. Discovery could not be read, so whether this cluster serves
    // ephemeral containers is unknown — and the panel must not render that as
    // "your cluster is too old".
    supported: null,
    supportDetail:
      "The core API group's discovery document could not be read (rbac_denied), so whether this cluster " +
      'serves ephemeral containers is unknown.',
  },

  // §5 node detail: the row plus its pods. `pods: []` here is a real zero — the
  // node was read and nothing is scheduled on it — which keeps the page's
  // "could not list" branch (a `null`) available for its own test.
  nodeDetail: {
    name: 'ip-10-0-1-4',
    ready: true,
    unschedulable: false,
    roles: ['worker'],
    kubelet_version: 'v1.31.4',
    os_image: 'Amazon Linux 2',
    container_runtime: 'containerd://1.7.13',
    internal_ip: '10.0.1.4',
    age_seconds: 8123456,
    capacity: { cpu_cores: 16, memory_bytes: 68719476736, pods: 110 },
    allocatable: { cpu_cores: 15.8, memory_bytes: 66571993088, pods: 110 },
    requested: null,
    pod_count: null,
    conditions: [{ type: 'MemoryPressure', status: 'False', reason: 'KubeletHasSufficientMemory' }],
    taints: [],
    // §24. Two of these matter to the label dialog: `team` is what
    // `nodeSchedulingPods` below selects on, and the role label is what turns
    // the ROLES column into something an edit can change.
    labels: {
      'kubernetes.io/hostname': 'ip-10-0-1-4',
      'node-role.kubernetes.io/worker': '',
      team: 'payments',
    },
    pods: [],
    unavailable: [],
  },

  /**
   * §24. The pods on `nodeDetail`, in the shape the plan derivations below
   * need — not a full PodSpec, because what they compute over is tolerations
   * and placement keys and nothing else.
   *
   * Chosen so one node exercises every row the taint plan can produce: a pod
   * that goes immediately, one that goes on a timer, one that stays, a
   * DaemonSet, and one with nothing owning it.
   */
  nodeSchedulingPods: [
    { namespace: 'prod', pod: 'checkout-7c9', controller: { kind: 'ReplicaSet', name: 'checkout-7c9' },
      tolerations: [], selectorKeys: ['team'] },
    { namespace: 'prod', pod: 'ledger-4f1', controller: { kind: 'StatefulSet', name: 'ledger' },
      tolerations: [{ key: 'dedicated', operator: 'Equal', value: 'gpu', effect: 'NoExecute',
                      tolerationSeconds: 300 }], selectorKeys: [] },
    { namespace: 'kube-system', pod: 'cilium-x4k2', controller: { kind: 'DaemonSet', name: 'cilium' },
      tolerations: [], selectorKeys: [] },
    { namespace: 'ops', pod: 'one-off-import', controller: null, tolerations: [], selectorKeys: [] },
    { namespace: 'prod', pod: 'sidecar-tolerant', controller: { kind: 'ReplicaSet', name: 'sidecar' },
      tolerations: [{ key: null, operator: 'Exists' }], selectorKeys: [] },
  ],

  // §5.5. Both gates on, one pod already there.
  nodeDebugEnabled: {
    items: [
      {
        name: 'node-debugger-ip-10-0-1-4-x4k2p',
        namespace: 'default',
        node: 'ip-10-0-1-4',
        image: 'busybox:1.36',
        phase: 'Running',
        state: 'Running',
        reason: null,
        started_at: '2026-08-21T09:00:05Z',
        created_at: '2026-08-21T09:00:00Z',
        // Read-only, which is this console's default and a departure from
        // kubectl. The writable case is its own fixture below, because the two
        // must not render the same.
        hostFilesystemReadOnly: true,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    enabled: true,
    enabledDetail: 'Node debug pods are enabled on this deployment.',
    namespace: 'default',
  },

  // The feature gate off while writes are on — the case the second switch
  // exists for, and the one the UI must explain rather than merely disable.
  nodeDebugDisabled: {
    items: [],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    enabled: false,
    enabledDetail:
      'Node debug pods are disabled on this deployment (ADMIN_NODE_DEBUG_ENABLED is off). ' +
      'This is a separate gate from ADMIN_ALLOW_MUTATIONS because a pod with the node\'s ' +
      'filesystem mounted is a larger grant than the rest of the write surface.',
    namespace: 'default',
  },

  // §15. Both gates on, one CLI pod already Running — the reuse case, where
  // opening the panel shows a terminal without creating anything.
  cliEnabled: {
    items: [
      {
        name: 'k8boss-cli-x4k2p',
        namespace: 'default',
        image: 'alpine/k8s:1.34.9',
        // The field that decides what a shell in this pod can do to the
        // cluster. It is not the console's permissions and not the operator's,
        // so it is on screen rather than implied.
        serviceAccount: 'k8boss-cli',
        phase: 'Running',
        state: 'Running',
        reason: null,
        node: 'ip-10-0-1-4',
        started_at: '2026-08-21T09:00:05Z',
        created_at: '2026-08-21T09:00:00Z',
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    enabled: true,
    enabledDetail: 'CLI pods are enabled on this deployment.',
    namespace: 'default',
    image: 'alpine/k8s:1.34.9',
    serviceAccount: 'k8boss-cli',
    container: 'cli',
  },

  // The feature gate off while writes are on — the case the second switch
  // exists for, and the one the UI must explain rather than merely disable.
  cliDisabled: {
    items: [],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    enabled: false,
    enabledDetail:
      'CLI pods are disabled on this deployment (ADMIN_CLI_ENABLED is off). This is a separate ' +
      'gate from ADMIN_ALLOW_MUTATIONS because a shell running kubectl bypasses every control ' +
      'this console puts in front of a write.',
    namespace: 'default',
    image: 'alpine/k8s:1.34.9',
    serviceAccount: 'default',
    container: 'cli',
  },


  /**
   * §13 route capabilities.
   *
   * The important part of this fixture is that it carries all three states at
   * once: `ingress` available, `openshift` unsupported, `gateway` unknown. A
   * page that renders the second and third the same way is the defect the whole
   * §13 read model exists to prevent — "this cluster has no Routes" and "we
   * could not find out whether it has Routes" send an operator to two very
   * different places.
   */
  routeCapabilities: {
    items: [
      {
        backend: 'openshift',
        kind: 'Route',
        group: 'route.openshift.io',
        version: null,
        plural: 'routes',
        label: 'OpenShift Route',
        summary: 'The native OpenShift exposure.',
        state: 'unsupported',
        detail: 'This cluster does not serve Route objects.',
        features: [
          { feature: 'edge-tls', label: 'Terminate TLS at the router (edge)', supported: true },
          { feature: 'passthrough-tls', label: 'Pass TLS through to the pod without terminating it', supported: true },
          { feature: 'reencrypt-tls', label: 'Terminate TLS at the router and re-encrypt to the pod', supported: true },
          { feature: 'insecure-redirect', label: 'Redirect plain HTTP to HTTPS', supported: true },
          { feature: 'insecure-allow', label: 'Serve the same content on plain HTTP as well as HTTPS', supported: true },
          { feature: 'weighted-backends', label: 'Split traffic across several Services by weight', supported: true },
          { feature: 'wildcard-subdomain', label: 'Answer for every subdomain of the hostname', supported: true },
          { feature: 'generated-host', label: 'Let the router pick the hostname', supported: true },
          { feature: 'path-exact', label: 'Match the path exactly rather than as a prefix', supported: false },
        ],
      },
      {
        backend: 'ingress',
        kind: 'Ingress',
        group: 'networking.k8s.io',
        version: 'v1',
        plural: 'ingresses',
        label: 'Ingress',
        summary: 'The portable exposure every ingress controller understands.',
        state: 'available',
        detail: 'This cluster serves networking.k8s.io/v1 ingresses.',
        features: [
          { feature: 'edge-tls', label: 'Terminate TLS at the router (edge)', supported: true },
          { feature: 'passthrough-tls', label: 'Pass TLS through to the pod without terminating it', supported: false },
          { feature: 'reencrypt-tls', label: 'Terminate TLS at the router and re-encrypt to the pod', supported: false },
          { feature: 'insecure-redirect', label: 'Redirect plain HTTP to HTTPS', supported: false },
          { feature: 'insecure-allow', label: 'Serve the same content on plain HTTP as well as HTTPS', supported: false },
          { feature: 'weighted-backends', label: 'Split traffic across several Services by weight', supported: false },
          { feature: 'wildcard-subdomain', label: 'Answer for every subdomain of the hostname', supported: false },
          { feature: 'generated-host', label: 'Let the router pick the hostname', supported: false },
          { feature: 'path-exact', label: 'Match the path exactly rather than as a prefix', supported: true },
        ],
      },
      {
        backend: 'gateway',
        kind: 'HTTPRoute',
        group: 'gateway.networking.k8s.io',
        version: null,
        plural: 'httproutes',
        label: 'Gateway API HTTPRoute',
        summary: 'The upstream successor to Ingress.',
        state: 'unknown',
        detail:
          'Whether this cluster serves HTTPRoute objects could not be determined: the API server did not answer for gateway.networking.k8s.io. This is not the same as the cluster not having them.',
        features: [
          { feature: 'edge-tls', label: 'Terminate TLS at the router (edge)', supported: false },
          { feature: 'passthrough-tls', label: 'Pass TLS through to the pod without terminating it', supported: false },
          { feature: 'reencrypt-tls', label: 'Terminate TLS at the router and re-encrypt to the pod', supported: false },
          { feature: 'insecure-redirect', label: 'Redirect plain HTTP to HTTPS', supported: true },
          { feature: 'insecure-allow', label: 'Serve the same content on plain HTTP as well as HTTPS', supported: false },
          { feature: 'weighted-backends', label: 'Split traffic across several Services by weight', supported: true },
          { feature: 'wildcard-subdomain', label: 'Answer for every subdomain of the hostname', supported: false },
          { feature: 'generated-host', label: 'Let the router pick the hostname', supported: false },
          { feature: 'path-exact', label: 'Match the path exactly rather than as a prefix', supported: true },
        ],
      },
    ],
    continue: null,
    remaining: null,
    partial: true,
    unavailable: [
      {
        group: 'gateway.networking.k8s.io',
        resource: 'httproutes',
        namespace: null,
        reason: 'unreachable',
        detail: 'the API server did not answer for gateway.networking.k8s.io',
      },
    ],
    // §13. The default fixture cluster has no wildcard domain — the ordinary
    // case, and the one where the dialog must generate nothing at all. The key
    // is present rather than omitted because the real endpoint always sends it,
    // and a fixture that leaves it out would let a `?.` typo pass here and fail
    // against a live backend.
    appDomain: {
      value: null,
      source: null,
      stored: null,
      discovered: null,
      pattern: null,
    },
  },

  /**
   * §13 exposures. Three rows carrying the three `admitted` states, because
   * that column is the one a green row can lie in.
   */
  /**
   * §32 — the certificates behind the exposures. Five rows, each a different
   * way of being right or wrong about one:
   *
   *   shop      valid, and covers the host it serves.
   *   admin     expiring inside the window, and names a host it no longer
   *             serves — current, correctly issued and useless.
   *   legacy    a Secret this console could not read: unknown, not absent.
   *   internal  a passthrough Route, whose certificate is in the pod.
   *   gw        a Gateway listener, because TLS lives there and not on an
   *             HTTPRoute.
   */
  routeCertificates: {
    items: [
      {
        id: 'ingress/prod/shop#0',
        kind: 'Ingress',
        group: 'networking.k8s.io',
        name: 'shop',
        namespace: 'prod',
        slot: '0',
        source: 'secret',
        termination: 'edge',
        hosts: ['shop.example.com'],
        secret: { namespace: 'prod', name: 'shop-tls' },
        sourceDetail: 'spec.tls[0].secretName names the Secret shop-tls.',
        certificate: {
          subject_common_name: 'shop.example.com',
          issuer_common_name: 'Example CA R3',
          serial: '03:ab:5f',
          not_before: '2026-07-01T00:00:00Z',
          not_after: '2026-12-01T00:00:00Z',
          dns_names: ['shop.example.com'],
          ip_addresses: [],
          email_addresses: [],
          uris: [],
          key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
          signature_algorithm: 'sha256',
          self_signed: false,
          chain_length: 2,
          error: null,
        },
        state: 'valid',
        expires_in_seconds: 7344000,
        hostsCovered: [{ host: 'shop.example.com', covered: true }],
        findings: [],
      },
      {
        id: 'ingress/prod/admin#0',
        kind: 'Ingress',
        group: 'networking.k8s.io',
        name: 'admin',
        namespace: 'prod',
        slot: '0',
        source: 'secret',
        termination: 'edge',
        hosts: ['admin.example.com'],
        secret: { namespace: 'prod', name: 'admin-tls' },
        sourceDetail: 'spec.tls[0].secretName names the Secret admin-tls.',
        certificate: {
          subject_common_name: 'old-admin.example.com',
          issuer_common_name: 'Example CA R3',
          serial: '0a:11',
          not_before: '2026-06-01T00:00:00Z',
          not_after: '2026-09-19T00:00:00Z',
          dns_names: ['old-admin.example.com'],
          ip_addresses: [],
          email_addresses: [],
          uris: [],
          key: { algorithm: 'RSA', size: 2048, curve: null },
          signature_algorithm: 'sha256',
          self_signed: false,
          chain_length: 1,
          error: null,
        },
        state: 'expiring',
        expires_in_seconds: 1036800,
        hostsCovered: [{ host: 'admin.example.com', covered: false }],
        findings: [
          {
            code: 'certificate_expiring',
            detail: 'This certificate expires on 2026-09-19T00:00:00Z, in 12 days.',
          },
          {
            code: 'host_not_covered',
            detail:
              'This certificate does not name admin.example.com. Its names are old-admin.example.com. Clients reaching those hosts get a name-mismatch error, whatever the expiry says.',
          },
          {
            code: 'chain_leaf_only',
            detail:
              'This bundle holds only the leaf certificate — no intermediate is included.',
          },
        ],
      },
      {
        id: 'ingress/prod/legacy#0',
        kind: 'Ingress',
        group: 'networking.k8s.io',
        name: 'legacy',
        namespace: 'prod',
        slot: '0',
        source: 'secret',
        termination: 'edge',
        hosts: ['legacy.example.com'],
        secret: { namespace: 'prod', name: 'legacy-tls' },
        sourceDetail: 'spec.tls[0].secretName names the Secret legacy-tls.',
        certificate: null,
        state: 'unknown',
        expires_in_seconds: null,
        hostsCovered: [{ host: 'legacy.example.com', covered: null }],
        findings: [
          {
            code: 'certificate_unreadable',
            detail:
              'The Secret prod/legacy-tls could not be read; the banner above says why. Nothing here says this exposure has no certificate — only that this console could not read the one it names.',
          },
        ],
      },
      {
        id: 'route/prod/internal#tls',
        kind: 'Route',
        group: 'route.openshift.io',
        name: 'internal',
        namespace: 'prod',
        slot: 'tls',
        source: 'backend',
        termination: 'passthrough',
        hosts: ['internal.apps.example.com'],
        secret: null,
        sourceDetail:
          'This Route passes TLS through to the pod, which holds the certificate.',
        certificate: null,
        state: 'unknown',
        expires_in_seconds: null,
        hostsCovered: [{ host: 'internal.apps.example.com', covered: null }],
        findings: [
          {
            code: 'certificate_in_backend',
            detail:
              'This Route passes TLS through to the pod, which holds the certificate. The router never sees one, and neither does this console.',
          },
        ],
      },
      {
        id: 'gateway/prod/edge#https/0',
        kind: 'Gateway',
        group: 'gateway.networking.k8s.io',
        name: 'edge',
        namespace: 'prod',
        slot: 'https/0',
        source: 'secret',
        termination: 'terminate',
        hosts: ['*.example.com'],
        secret: { namespace: 'certs', name: 'wildcard-tls' },
        sourceDetail:
          'Listener https references the Secret wildcard-tls in namespace certs.',
        certificate: {
          subject_common_name: '*.example.com',
          issuer_common_name: null,
          serial: 'ff',
          not_before: '2026-01-01T00:00:00Z',
          not_after: '2026-09-04T00:00:00Z',
          dns_names: ['*.example.com'],
          ip_addresses: [],
          email_addresses: [],
          uris: [],
          key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
          signature_algorithm: 'sha256',
          self_signed: true,
          chain_length: 1,
          error: null,
        },
        state: 'expired',
        expires_in_seconds: -259200,
        hostsCovered: [{ host: '*.example.com', covered: true }],
        findings: [
          {
            code: 'certificate_expired',
            detail: 'This certificate expired on 2026-09-04T00:00:00Z, 3 days ago.',
          },
          {
            code: 'self_signed',
            detail:
              'The issuer and the subject of this certificate are the same name, so no certificate authority vouches for it.',
          },
        ],
      },
    ],
    continue: null,
    remaining: null,
    partial: true,
    unavailable: [
      {
        group: '',
        resource: 'secrets',
        namespace: 'prod',
        reason: 'forbidden',
        detail: 'secrets "legacy-tls" is forbidden',
      },
    ],
    kinds: [
      {
        kind: 'Route',
        group: 'route.openshift.io',
        state: 'available',
        version: 'v1',
        detail: 'This cluster serves route.openshift.io/v1 routes.',
      },
      {
        kind: 'Ingress',
        group: 'networking.k8s.io',
        state: 'available',
        version: 'v1',
        detail: 'This cluster serves networking.k8s.io/v1 ingresses.',
      },
      {
        kind: 'Gateway',
        group: 'gateway.networking.k8s.io',
        state: 'available',
        version: 'v1',
        detail: 'This cluster serves gateway.networking.k8s.io/v1 gateways.',
      },
    ],
    truncated: [],
    expiringWindowSeconds: 2592000,
    maxCertificateReads: 100,
  },

  routes: {
    items: [
      {
        id: 'ingress/prod/shop',
        backend: 'ingress',
        kind: 'Ingress',
        group: 'networking.k8s.io',
        version: 'v1',
        plural: 'ingresses',
        name: 'shop',
        namespace: 'prod',
        hosts: ['shop.example.com'],
        subdomain: null,
        path: '/',
        pathType: 'Prefix',
        paths: [{ path: '/', pathType: 'Prefix', service: 'shop', port: 80, weight: null }],
        targets: [{ service: 'shop', port: 80, weight: null }],
        tls: { termination: 'edge', insecurePolicy: null, inlineCertificate: false, secretName: 'shop-tls' },
        wildcardPolicy: null,
        // Always null for an Ingress: the API has no admission condition.
        admitted: null,
        admittedDetail: null,
        addresses: ['a1b2.elb.eu-west-1.amazonaws.com'],
        ingressClass: 'haproxy',
        tlsHosts: ['shop.example.com'],
        parents: [],
        age_seconds: 86400,
        resourceVersion: '4021',
        managedBy: { controller: null, tool: null, marker: null, detail: null },
      },
      {
        id: 'ingress/prod/admin',
        backend: 'ingress',
        kind: 'Ingress',
        group: 'networking.k8s.io',
        version: 'v1',
        plural: 'ingresses',
        name: 'admin',
        namespace: 'prod',
        hosts: ['admin.example.com'],
        subdomain: null,
        path: '/',
        pathType: 'Prefix',
        paths: [{ path: '/', pathType: 'Prefix', service: 'admin', port: 8080, weight: null }],
        targets: [{ service: 'admin', port: 8080, weight: null }],
        tls: { termination: null, insecurePolicy: null, inlineCertificate: false, secretName: null },
        wildcardPolicy: null,
        admitted: null,
        // The fixture that matters: no controller has claimed it. Not "rejected".
        admittedDetail:
          'No ingress controller has published an address for this Ingress. That is what an unclaimed Ingress looks like, and also what one on a cluster with no ingress controller looks like.',
        addresses: [],
        ingressClass: 'haproxy',
        tlsHosts: [],
        parents: [],
        age_seconds: 3600,
        resourceVersion: '4022',
        // The case the column exists for: an edit here succeeds, reports
        // `applied: true` truthfully, and is reverted seconds later.
        managedBy: {
          controller: { kind: 'Shop', name: 'storefront', apiVersion: 'example.com/v1' },
          tool: null,
          marker: null,
          detail:
            'This exposure is owned by Shop storefront, which is reconciling it. An edit made here will be applied and then reverted, and nothing will say so.',
        },
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    backends: [],
    truncated: [],
  },

  /** §14 — nothing installed, both gates shut. The default a fresh console shows. */
  routerAbsent: {
    enabled: false,
    enabledDetail:
      'Managing the shipped router is disabled on this deployment (ADMIN_ROUTER_MANAGE_ENABLED is off).',
    installed: false,
    namespace: 'k8boss-router',
    namespaceDiscovered: false,
    shippedVersion: '3.2.13',
    installedVersion: null,
    upgradeAvailable: null,
    deployment: { present: false, version: null, desiredReplicas: null, readyReplicas: null, image: null, gatewayApi: null, detail: null },
    service: { present: false, type: null, addresses: [], nodePorts: [], detail: null },
    ingressClass: { present: false, name: 'haproxy', default: null, controller: null },
    serves: [
      { backend: 'ingress', served: true, detail: 'The shipped router is an Ingress controller.' },
      {
        backend: 'gateway',
        served: false,
        detail:
          'The HAProxy Kubernetes Ingress Controller implements Gateway API for TCPRoute only — HTTPRoute is not implemented.',
      },
      {
        backend: 'openshift',
        served: false,
        detail: "Routes are served by OpenShift's own router, which an OpenShift cluster already runs.",
      },
    ],
    image: 'docker.io/haproxytech/kubernetes-ingress:3.2.13',
    partial: false,
    unavailable: [],
  },

  /**
   * §14 — a router installed with options that are NOT the bundle's defaults.
   *
   * The reinstall form has to come up showing these rather than
   * `blankOptions()`. An operator who opens Reinstall to take a version bump
   * and finds "make this the default class" unchecked will turn it off by
   * confirming, and the only warning is a line in an eight-object diff.
   */
  routerInstalledCustom: {
    enabled: true,
    enabledDetail: 'Router management is enabled on this deployment.',
    installed: true,
    namespace: 'edge-proxy',
    namespaceDiscovered: true,
    shippedVersion: '3.2.13',
    installedVersion: '3.2.13',
    versionMatches: true,
    upgradeAvailable: false,
    deployment: {
      present: true, version: '3.2.13', desiredReplicas: 3, readyReplicas: 3,
      image: 'docker.io/haproxytech/kubernetes-ingress:3.2.13',
      gatewayApi: true, detail: null,
    },
    service: { present: true, type: 'NodePort', addresses: [], nodePorts: [], detail: null },
    ingressClass: {
      present: true, name: 'edge', default: true,
      controller: 'haproxy.org/ingress-controller/edge',
    },
    otherClasses: [],
    serves: [
      { backend: 'ingress', served: true, detail: 'The shipped router is an Ingress controller.' },
    ],
    image: 'docker.io/haproxytech/kubernetes-ingress:3.2.13',
    partial: false,
    unavailable: [],
  },

  /**
   * §14 — the read failed. `installed: null`, and the page must not render that
   * as "not installed": installing then would put a second proxy on the
   * cluster's ingress path.
   */
  routerUnknown: {
    enabled: true,
    enabledDetail: 'Router management is enabled on this deployment.',
    installed: null,
    namespace: 'k8boss-router',
    namespaceDiscovered: false,
    shippedVersion: '3.2.13',
    installedVersion: null,
    upgradeAvailable: null,
    deployment: { present: false, version: null, desiredReplicas: null, readyReplicas: null, image: null, gatewayApi: null, detail: null },
    service: { present: false, type: null, addresses: [], nodePorts: [], detail: null },
    ingressClass: { present: false, name: 'haproxy', default: null, controller: null },
    serves: [
      { backend: 'ingress', served: true, detail: 'The shipped router is an Ingress controller.' },
      { backend: 'gateway', served: false, detail: 'HTTPRoute is not implemented by this controller.' },
      { backend: 'openshift', served: false, detail: "Routes are served by OpenShift's own router." },
    ],
    image: 'docker.io/haproxytech/kubernetes-ingress:3.2.13',
    partial: true,
    unavailable: [
      {
        group: 'apps',
        resource: 'deployments',
        namespace: 'k8boss-router',
        reason: 'forbidden',
        detail: 'deployments is forbidden',
      },
    ],
  },

  routerPlan: {
    version: '3.2.13',
    image: 'docker.io/haproxytech/kubernetes-ingress:3.2.13',
    options: {
      namespace: 'k8boss-router',
      serviceType: 'LoadBalancer',
      replicas: 2,
      ingressClassName: 'haproxy',
      defaultClass: false,
      gatewayApi: false,
    },
    serves: [
      { backend: 'ingress', served: true, detail: 'The shipped router is an Ingress controller.' },
      {
        backend: 'gateway',
        served: false,
        detail: 'The HAProxy Kubernetes Ingress Controller implements Gateway API for TCPRoute only.',
      },
      { backend: 'openshift', served: false, detail: "Routes are served by OpenShift's own router." },
    ],
    objects: [
      { kind: 'Namespace', name: 'k8boss-router', namespace: null, group: '', resource: 'namespaces', yaml: 'apiVersion: v1\nkind: Namespace\n' },
      { kind: 'ClusterRole', name: 'k8boss-admin-router', namespace: null, group: 'rbac.authorization.k8s.io', resource: 'clusterroles', yaml: 'kind: ClusterRole\n' },
    ],
  },

  /**
   * §16 the operator catalog.
   *
   * `enabled: true` — a deployment that has switched `ADMIN_PORTAL_INSTALL_ENABLED`
   * on. The backend default is off, and §14's `routerAbsent` models that case for
   * the router; here the on state is the default because every catalog assertion
   * below is about the *rows*, and a page whose only action is greyed out is a
   * worse default fixture than one where the Subscribe path is reachable.
   *
   * `sources` carries three entries, not six: `catalog()` resolves only
   * PackageManifests, Subscriptions and CatalogSources — the installed view
   * resolves its own three.
   *
   * Every row's `installed` is a real boolean here because the Subscription
   * listing succeeded. The `null` case is a property of the *listing*, not of a
   * row, so specs that want it map the whole set — mixing `null` and `false`
   * would be a shape `package_row` cannot produce.
   */
  portalCatalog: {
    items: [
      {
        id: 'olm/community-operators/prometheus',
        name: 'prometheus',
        displayName: 'Prometheus Operator',
        provider: 'Red Hat',
        providerUrl: 'https://prometheus-operator.dev',
        catalog: 'community-operators',
        catalogNamespace: 'olm',
        catalogDisplayName: 'Community Operators',
        defaultChannel: 'beta',
        channels: ['beta', 'stable'],
        version: '0.71.2',
        summary: 'Run and configure Prometheus as Kubernetes objects.',
        categories: ['Monitoring', 'Logging & Tracing'],
        capabilityLevel: 'Deep Insights',
        certified: false,
        installModes: ['OwnNamespace', 'SingleNamespace', 'AllNamespaces'],
        installed: false,
        installations: [],
      },
      {
        id: 'olm/community-operators/grafana-operator',
        name: 'grafana-operator',
        displayName: 'Grafana Operator',
        provider: 'Grafana Labs',
        providerUrl: null,
        catalog: 'community-operators',
        catalogNamespace: 'olm',
        catalogDisplayName: 'Community Operators',
        defaultChannel: 'v5',
        channels: ['v5'],
        // Null, not "0": this catalog entry publishes no CSV description for its
        // default channel, which is ordinary for a pruned catalog.
        version: null,
        summary: 'Manage Grafana instances and dashboards.',
        categories: ['Monitoring'],
        capabilityLevel: null,
        certified: null,
        installModes: null,
        installed: true,
        installations: [
          {
            id: 'kube-system/grafana-operator',
            name: 'grafana-operator',
            namespace: 'kube-system',
            channel: 'v5',
          },
        ],
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    sources: [
      {
        api: 'packages',
        kind: 'PackageManifest',
        group: 'packages.operators.coreos.com',
        version: 'v1',
        plural: 'packagemanifests',
        label: 'Catalog contents',
        required: true,
        state: 'available',
        detail: 'This cluster serves packages.operators.coreos.com/v1 packagemanifests.',
      },
      {
        api: 'subscriptions',
        kind: 'Subscription',
        group: 'operators.coreos.com',
        version: 'v1alpha1',
        plural: 'subscriptions',
        label: 'Subscriptions',
        required: true,
        state: 'available',
        detail: 'This cluster serves operators.coreos.com/v1alpha1 subscriptions.',
      },
      {
        api: 'catalogsources',
        kind: 'CatalogSource',
        group: 'operators.coreos.com',
        version: 'v1alpha1',
        plural: 'catalogsources',
        label: 'Catalogs',
        required: false,
        state: 'available',
        detail: 'This cluster serves operators.coreos.com/v1alpha1 catalogsources.',
      },
    ],
    catalogs: [
      {
        id: 'olm/community-operators',
        name: 'community-operators',
        namespace: 'olm',
        displayName: 'Community Operators',
        publisher: 'OperatorHub.io',
        sourceType: 'grpc',
        image: 'quay.io/operatorhubio/catalog:latest',
        state: 'READY',
        healthy: true,
        detail: "The catalog's last observed connection state is READY.",
        age_seconds: 864000,
      },
      {
        id: 'olm/private-mirror',
        name: 'private-mirror',
        namespace: 'olm',
        displayName: 'Private mirror',
        publisher: 'Platform team',
        sourceType: 'grpc',
        image: 'registry.example:5000/catalog:2026-08',
        // Null, not false: this CatalogSource was created moments ago and the
        // catalog operator has published no connection state for it. Calling
        // that unhealthy sends somebody to debug a registry that is starting.
        state: null,
        healthy: null,
        detail: 'This catalog has published no connection state yet.',
        age_seconds: 30,
      },
    ],
    truncated: [],
    enabled: true,
    enabledDetail: 'This deployment permits subscribing to catalog operators.',
    // §33's gate, on §16's envelope. A DIFFERENT switch from `enabled` above —
    // ADMIN_OLM_INSTALL_ENABLED, not ADMIN_PORTAL_INSTALL_ENABLED — and it is
    // here rather than behind its own request so the install panel paints
    // disabled-with-the-reason on first render instead of correcting itself a
    // round trip later.
    olmInstall: {
      enabled: true,
      detail: 'This deployment permits installing Operator Lifecycle Manager.',
    },
  },

  /**
   * §33 `GET /portal/olm` on a cluster that does not run OLM.
   *
   * `installed: false` and `ready: false` are real answers here, not nulls:
   * every read succeeded and found nothing. The distinction matters because the
   * panel offers an install off the back of `installed === false`, and offering
   * one off the back of a read that *failed* is how somebody installs OLM on
   * top of an OLM.
   */
  olmStatusAbsent: {
    enabled: true,
    enabledDetail: 'This deployment permits installing Operator Lifecycle Manager.',
    installed: false,
    ready: false,
    shippedVersion: '0.35.0',
    upstream:
      'https://github.com/operator-framework/operator-lifecycle-manager/releases/download/v0.35.0',
    managedByUs: null,
    crds: { expected: 8, present: 0, established: 0, missing: [], detail: null },
    deployments: [
      { name: 'olm-operator', present: false, desiredReplicas: null, readyReplicas: null, image: null, managedByUs: null, detail: null },
      { name: 'catalog-operator', present: false, desiredReplicas: null, readyReplicas: null, image: null, managedByUs: null, detail: null },
    ],
    packageServer: {
      csvPresent: false,
      phase: null,
      message: null,
      apiAvailable: false,
      apiDetail: 'This cluster does not serve PackageManifest objects.',
      detail: 'packages.operators.coreos.com is the API the portal reads.',
    },
    namespaces: ['olm', 'operators'],
    notes: [],
    partial: false,
    unavailable: [],
  },

  /**
   * §33 the state the whole feature is careful about: every object landed and
   * OLM is not working yet.
   *
   * `installed: true` with `ready: false`. A UI that renders one badge over both
   * reports a working OLM over a package server that has not registered — and
   * then the catalog stays empty underneath it, correctly, with nothing on the
   * page explaining why.
   */
  olmStatusInstalledNotReady: {
    enabled: true,
    enabledDetail: 'This deployment permits installing Operator Lifecycle Manager.',
    installed: true,
    ready: false,
    shippedVersion: '0.35.0',
    upstream:
      'https://github.com/operator-framework/operator-lifecycle-manager/releases/download/v0.35.0',
    managedByUs: true,
    crds: { expected: 8, present: 8, established: 8, missing: [], detail: null },
    deployments: [
      { name: 'olm-operator', present: true, desiredReplicas: 1, readyReplicas: 1, image: 'quay.io/operator-framework/olm@sha256:8c86', managedByUs: true, detail: null },
      { name: 'catalog-operator', present: true, desiredReplicas: 1, readyReplicas: 1, image: 'quay.io/operator-framework/olm@sha256:8c86', managedByUs: true, detail: null },
    ],
    packageServer: {
      csvPresent: true,
      phase: 'Installing',
      message: 'installing: waiting for deployment packageserver to become ready',
      apiAvailable: false,
      apiDetail: 'This cluster does not serve PackageManifest objects.',
      detail: 'packages.operators.coreos.com is the API the portal reads.',
    },
    namespaces: ['olm', 'operators'],
    notes: [],
    partial: false,
    unavailable: [],
  },

  /**
   * §33 `POST /portal/olm/plan`. Writes nothing and is ungated.
   *
   * Three objects rather than twenty-six — enough for the dialog to render both
   * phases and the ClusterRole, which is the one anybody actually has to read.
   */
  olmPlan: {
    version: '0.35.0',
    upstream:
      'https://github.com/operator-framework/operator-lifecycle-manager/releases/download/v0.35.0',
    digests: {
      'crds.yaml': '0b66ca9d94298f04ec0704887adf663003447f50ca906187aa0ed14d701a9bd7',
      'olm.yaml': '5756646581f5a13fab43a20e7c548492c2494ed5899d8ad2d18c0c3032d2a590',
    },
    communityCatalog: false,
    communityCatalogImage: 'quay.io/operatorhubio/catalog:latest',
    namespaces: ['olm', 'operators'],
    consequences: [
      {
        code: 'cluster_admin_grant',
        label: 'This grants OLM full control of the cluster',
        consequence:
          "apiGroups: ['*'], resources: ['*'] with every verb including escalate and bind.",
        mitigation: 'There is no narrower version of this to choose.',
      },
      {
        code: 'crd_ownership',
        label: "Eight CustomResourceDefinitions become part of the cluster's API",
        consequence: 'Deleting one later deletes every custom resource made from it.',
        mitigation: 'This console does not offer that removal.',
      },
    ],
    notes: [
      {
        code: 'installed_is_not_running',
        label: 'A successful install is not a running OLM',
        detail:
          'The Deployments still have to become Ready and OLM has to reconcile the packageserver CSV.',
      },
    ],
    phases: [
      { phase: 'crds', detail: 'Eight CustomResourceDefinitions.' },
      { phase: 'core', detail: 'OLM itself.' },
    ],
    objects: [
      {
        kind: 'CustomResourceDefinition',
        name: 'subscriptions.operators.coreos.com',
        namespace: null,
        group: 'apiextensions.k8s.io',
        resource: 'customresourcedefinitions',
        phase: 'crds',
        yaml: 'apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\n',
      },
      {
        kind: 'Namespace',
        name: 'olm',
        namespace: null,
        group: '',
        resource: 'namespaces',
        phase: 'core',
        yaml: 'apiVersion: v1\nkind: Namespace\n',
      },
      {
        kind: 'ClusterRole',
        name: 'system:controller:operator-lifecycle-manager',
        namespace: null,
        group: 'rbac.authorization.k8s.io',
        resource: 'clusterroles',
        phase: 'core',
        yaml: "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nrules:\n- apiGroups: ['*']\n",
      },
    ],
  },

  /**
   * §16 installed operators. Two rows, and the second is why the page exists.
   *
   * `prometheus` is installed and running. `grafana-operator` has
   * `installedCSV: null` and `phase: null` at once — OLM has been asked and has
   * done nothing yet, because its InstallPlan is waiting for approval. Neither
   * null may render as `Failed` and neither may render as blank: `phaseDetail`
   * is the sentence that says which of the two nulls this is.
   */
  portalInstalled: {
    items: [
      {
        id: 'monitoring/prometheus',
        name: 'prometheus',
        namespace: 'monitoring',
        package: 'prometheus',
        channel: 'beta',
        catalog: 'community-operators',
        catalogNamespace: 'olm',
        installPlanApproval: 'Automatic',
        startingCSV: null,
        installedCSV: 'prometheusoperator.0.71.2',
        currentCSV: 'prometheusoperator.0.71.2',
        state: 'AtLatestKnown',
        phase: 'Succeeded',
        phaseDetail: 'install strategy completed with no errors',
        approvalRequired: false,
        installPlanDetail: 'InstallPlan install-abc12 is Complete.',
        installPlan: 'install-abc12',
        conditions: [],
        age_seconds: 864000,
      },
      {
        id: 'kube-system/grafana-operator',
        name: 'grafana-operator',
        namespace: 'kube-system',
        package: 'grafana-operator',
        // Null: this Subscription names no channel, so OLM follows the
        // package's default — which can change under it.
        channel: null,
        catalog: 'community-operators',
        catalogNamespace: 'olm',
        installPlanApproval: 'Manual',
        startingCSV: null,
        // OLM has installed nothing. Not a failed install, and not a failed
        // read — the two are distinguished by `phaseDetail`, never by the
        // phase degrading to something that renders red.
        installedCSV: null,
        currentCSV: 'grafana-operator.v5.6.0',
        state: 'UpgradePending',
        phase: null,
        phaseDetail:
          'OLM has not installed anything for this Subscription yet — it publishes no ' +
          'installedCSV. Resolution may be pending, an InstallPlan may be waiting for ' +
          'approval, or the namespace may have no OperatorGroup.',
        approvalRequired: true,
        installPlanDetail:
          'This install is waiting for somebody to approve its InstallPlan; nothing is ' +
          'installed until they do.',
        installPlan: 'install-xyz98',
        // Only conditions the API server reported `status: "True"` reach a row —
        // `_subscription_conditions` drops the rest — so this is the one that
        // answers "I subscribed and nothing happened".
        conditions: [
          {
            type: 'InstallPlanPending',
            status: 'True',
            reason: 'RequiresApproval',
            message: 'an InstallPlan for grafana-operator.v5.6.0 is waiting for approval',
          },
        ],
        age_seconds: 3600,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
    sources: [
      {
        api: 'subscriptions',
        kind: 'Subscription',
        group: 'operators.coreos.com',
        version: 'v1alpha1',
        plural: 'subscriptions',
        label: 'Subscriptions',
        required: true,
        state: 'available',
        detail: 'This cluster serves operators.coreos.com/v1alpha1 subscriptions.',
      },
      {
        api: 'clusterserviceversions',
        kind: 'ClusterServiceVersion',
        group: 'operators.coreos.com',
        version: 'v1alpha1',
        plural: 'clusterserviceversions',
        label: 'Installed operators',
        required: false,
        state: 'available',
        detail: 'This cluster serves operators.coreos.com/v1alpha1 clusterserviceversions.',
      },
      {
        api: 'installplans',
        kind: 'InstallPlan',
        group: 'operators.coreos.com',
        version: 'v1alpha1',
        plural: 'installplans',
        label: 'Install plans',
        required: false,
        state: 'available',
        detail: 'This cluster serves operators.coreos.com/v1alpha1 installplans.',
      },
    ],
    truncated: [],
    enabled: true,
    enabledDetail: 'This deployment permits subscribing to catalog operators.',
    // §33's gate, on §16's envelope. A DIFFERENT switch from `enabled` above —
    // ADMIN_OLM_INSTALL_ENABLED, not ADMIN_PORTAL_INSTALL_ENABLED — and it is
    // here rather than behind its own request so the install panel paints
    // disabled-with-the-reason on first render instead of correcting itself a
    // round trip later.
    olmInstall: {
      enabled: true,
      detail: 'This deployment permits installing Operator Lifecycle Manager.',
    },
  },

  /**
   * §16 `POST /portal/subscriptions/plan`. Writes nothing.
   *
   * `consequences: []` and `target.ready: true` — the namespace has exactly one
   * OperatorGroup and this channel supports the mode it demands, which is the
   * only combination that earns a `true`. Specs that need a blocked write
   * replace `consequences`, because the acknowledgement handshake is keyed on
   * the codes and nothing else.
   */
  /**
   * §17 `GET /projects/{name}` — one namespace with what governs it.
   *
   * Three deliberate holes, because the page is about the difference between
   * them: `roleBindings: null` is a listing that was refused (with the entry
   * in `unavailable[]`), `requests.memory`'s `used: null` is a quota the
   * controller has not recorded usage for, and `pods` is `exhausted: true` —
   * the row that explains why the next Deployment stays at 0 of N.
   */
  project: {
    name: 'prod',
    status: 'Active',
    labels: { 'pod-security.kubernetes.io/enforce': 'restricted', team: 'payments' },
    annotations: { 'openshift.io/display-name': 'Production' },
    age_seconds: 8123456,
    pod_count: 62,
    creationTimestamp: '2026-01-04T08:00:00Z',
    displayName: 'Production',
    description: null,
    podSecurity: {
      enforce: 'restricted',
      enforceVersion: null,
      audit: null,
      auditVersion: null,
      warn: 'baseline',
      warnVersion: 'v1.31',
      labelled: true,
    },
    quotas: [
      {
        name: 'project-quota',
        namespace: 'prod',
        scopes: [],
        scoped: false,
        reconciled: true,
        age_seconds: 8123000,
        resources: [
          { resource: 'pods', hard: '20', used: '20', hard_value: 20, used_value: 20, exhausted: true },
          { resource: 'requests.cpu', hard: '4', used: '1500m', hard_value: 4, used_value: 1.5, exhausted: false },
          // Null, not zero: the controller has not recorded usage for this one.
          { resource: 'requests.memory', hard: '8Gi', used: null, hard_value: 8589934592, used_value: null, exhausted: null },
        ],
      },
    ],
    limitRanges: [
      {
        name: 'project-limits',
        namespace: 'prod',
        age_seconds: 8123000,
        limits: [
          {
            type: 'Container',
            max: {},
            min: {},
            default: { cpu: '500m', memory: '512Mi' },
            defaultRequest: { cpu: '100m', memory: '128Mi' },
            maxLimitRequestRatio: {},
          },
        ],
      },
    ],
    // Null, not []: the listing was refused, and "nobody is bound" is the
    // sentence that gets a binding added on top of the one nobody could see.
    roleBindings: null,
    networkPolicies: {
      count: 1,
      names: ['allow-same-namespace'],
      isolatesAllIngress: true,
      isolatesAllEgress: false,
    },
    partial: true,
    unavailable: [
      {
        group: 'rbac.authorization.k8s.io',
        resource: 'rolebindings',
        namespace: 'prod',
        reason: 'forbidden',
        detail:
          'rolebindings.rbac.authorization.k8s.io is forbidden: User "system:serviceaccount:k8boss-admin:console" cannot list resource "rolebindings" in API group "rbac.authorization.k8s.io" in the namespace "prod"',
      },
    ],
  },

  portalPlan: {
    package: 'prometheus',
    namespace: 'monitoring',
    channel: 'beta',
    catalog: 'community-operators',
    catalogNamespace: 'olm',
    installPlanApproval: 'Automatic',
    // The *catalog's* display name, not the package's — `plan()` reads
    // `status.catalogSourceDisplayName` into this key.
    displayName: 'Community Operators',
    provider: 'Red Hat',
    defaultChannel: 'beta',
    channels: [
      PORTAL_BETA_CHANNEL,
      { ...PORTAL_BETA_CHANNEL, name: 'stable', currentCSV: 'prometheusoperator.0.68.0', version: '0.68.0' },
    ],
    selected: PORTAL_BETA_CHANNEL,
    target: {
      namespace: 'monitoring',
      operatorGroups: [
        {
          name: 'monitoring-operators',
          namespace: 'monitoring',
          targetNamespaces: ['monitoring'],
          publishedNamespaces: ['monitoring'],
          selector: false,
          allNamespaces: false,
        },
      ],
      requiredInstallMode: 'OwnNamespace',
      ready: true,
      detail:
        'OperatorGroup monitoring-operators requires OwnNamespace, which this channel supports.',
    },
    existing: [],
    consequences: [],
    document: [
      'apiVersion: operators.coreos.com/v1alpha1',
      'kind: Subscription',
      'metadata:',
      '  name: prometheus',
      '  namespace: monitoring',
      'spec:',
      '  name: prometheus',
      '  channel: beta',
      '  source: community-operators',
      '  sourceNamespace: olm',
      '  installPlanApproval: Automatic',
      '',
    ].join('\n'),
    partial: false,
    unavailable: [],
    enabled: true,
    enabledDetail: 'This deployment permits subscribing to catalog operators.',
    // §33's gate, on §16's envelope. A DIFFERENT switch from `enabled` above —
    // ADMIN_OLM_INSTALL_ENABLED, not ADMIN_PORTAL_INSTALL_ENABLED — and it is
    // here rather than behind its own request so the install panel paints
    // disabled-with-the-reason on first render instead of correcting itself a
    // round trip later.
    olmInstall: {
      enabled: true,
      detail: 'This deployment permits installing Operator Lifecycle Manager.',
    },
  },

  /**
   * §8.3 NetworkPolicy rows, chosen to be the pair that must never render alike:
   * `deny-all` governs ingress with no rules (a total block) and does not govern
   * egress at all (no restriction whatsoever), while `allow-metrics` shows the
   * union rule that makes a whole direction wide open.
   */
  networkPolicies: {
    items: [
      {
        name: 'default-deny-ingress',
        namespace: 'prod',
        pod_selector: { matchLabels: {}, matchExpressions: [] },
        selects_all_pods: true,
        policy_types: ['Ingress'],
        policy_types_source: 'declared',
        ingress: { governed: true, rule_count: 0, effect: 'deny_all', rules: [] },
        egress: { governed: false, rule_count: null, effect: null, rules: [] },
        age_seconds: 604800,
      },
      {
        name: 'allow-metrics',
        namespace: 'prod',
        pod_selector: { matchLabels: { app: 'checkout' }, matchExpressions: [] },
        selects_all_pods: false,
        policy_types: ['Ingress', 'Egress'],
        policy_types_source: 'declared',
        ingress: {
          governed: true,
          rule_count: 1,
          effect: 'restricted',
          rules: [
            {
              peers: [
                {
                  type: 'namespace',
                  podSelector: null,
                  namespaceSelector: { matchLabels: { name: 'monitoring' }, matchExpressions: [] },
                  cidr: null,
                  except: [],
                },
              ],
              allows_all_peers: false,
              ports: [{ protocol: 'TCP', port: 9090, endPort: null }],
              allows_all_ports: false,
            },
          ],
        },
        egress: {
          governed: true,
          rule_count: 1,
          effect: 'allow_all',
          rules: [{ peers: [], allows_all_peers: true, ports: [], allows_all_ports: true }],
        },
        age_seconds: 3600,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  /**
   * §8.4 pod isolation. Three rows, one per state the column has: covered,
   * unrestricted, and unknown — the third is what an unevaluable selector
   * produces, and it must render as an em dash rather than as "unrestricted".
   */
  isolation: {
    items: [
      {
        name: 'checkout-7d9f8b6c4-hk2xv',
        namespace: 'prod',
        phase: 'Running',
        phase_detail: null,
        ready: '1/1',
        restarts: 0,
        node: 'ip-10-0-1-4',
        labels: { app: 'checkout' },
        host_network: false,
        policies: ['default-deny-ingress', 'allow-metrics'],
        ingress: { isolated: true, effect: 'restricted', policies: ['default-deny-ingress', 'allow-metrics'] },
        egress: { isolated: true, effect: 'allow_all', policies: ['allow-metrics'] },
        age_seconds: 86400,
      },
      {
        name: 'legacy-batch-0',
        namespace: 'prod',
        phase: 'Running',
        phase_detail: null,
        ready: '1/1',
        restarts: 0,
        node: 'ip-10-0-1-5',
        labels: { app: 'legacy' },
        host_network: true,
        policies: [],
        ingress: { isolated: false, effect: null, policies: [] },
        egress: { isolated: false, effect: null, policies: [] },
        age_seconds: 7200,
      },
      {
        name: 'mystery-0',
        namespace: 'prod',
        phase: 'Running',
        phase_detail: null,
        ready: '1/1',
        restarts: 0,
        node: 'ip-10-0-1-6',
        labels: {},
        host_network: false,
        policies: [],
        ingress: { isolated: null, effect: null, policies: [] },
        egress: { isolated: null, effect: null, policies: [] },
        age_seconds: 60,
      },
    ],
    continue: null,
    remaining: null,
    partial: true,
    unavailable: [
      {
        group: 'networking.k8s.io',
        resource: 'networkpolicies',
        namespace: 'prod',
        reason: 'unsupported',
        detail:
          'A pod selector on this cluster uses a matchExpressions operator this console cannot evaluate.',
      },
    ],
    namespace: null,
    policy_count: 2,
    summary: {
      pod_count: 3,
      ingress: { isolated: 1, unrestricted: 1, unknown: 1 },
      egress: { isolated: 1, unrestricted: 1, unknown: 1 },
      host_network: 1,
    },
  },

  emptyList: { items: [], continue: null, remaining: null, partial: false, unavailable: [] },

  // §13. What the exposure dialog's Service picker reads. `checkout` is
  // single-port on purpose — that is the case where picking a Service also
  // fills the port in, and the specs assert it.
  // §8 rows, as `service_row` in backend/app/resources/shaping.py actually
  // emits them: `name` and `ports` at the top level, not `metadata.name` and
  // `spec.ports`. The first version of this fixture invented the raw-manifest
  // shape, matched the reader that was wrong in the same way, and passed on
  // every run while the picker found nothing on a real cluster. A fixture is
  // only worth what it costs if it is the shape the backend returns.
  services: {
    items: [
      {
        name: 'checkout',
        namespace: 'prod',
        type: 'ClusterIP',
        clusterIP: '10.96.0.11',
        externalIPs: [],
        ports: [{ name: 'http', port: 8080, targetPort: 'http', protocol: 'TCP', nodePort: null }],
        selector: { app: 'checkout' },
        age_seconds: 3600,
        endpoint_count: 2,
      },
      {
        name: 'payments',
        namespace: 'prod',
        type: 'ClusterIP',
        clusterIP: '10.96.0.12',
        externalIPs: [],
        ports: [
          { name: 'http', port: 80, targetPort: 'http', protocol: 'TCP', nodePort: null },
          { name: 'grpc', port: 9090, targetPort: 'grpc', protocol: 'TCP', nodePort: null },
        ],
        selector: { app: 'payments' },
        age_seconds: 7200,
        endpoint_count: 3,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  /**
   * §8/§20. Three claims that between them make the page's argument: one
   * settled, one mid-expansion (requested and capacity disagree — the only
   * thing in the table that shows an unfinished resize), and one Pending with
   * no capacity at all.
   */
  /**
   * §22. Four snapshots, one for each state the Ready column has to keep apart:
   * ready, still being taken (`readyToUse` absent — the *bool the API declares
   * on purpose), failed with the controller's reason, and one adopted from
   * content that already existed in the storage system rather than taken from a
   * claim.
   */
  /**
   * §25. Seven requests, chosen so one listing shows every state the console
   * distinguishes and every consequence it can raise.
   *
   * The rows are already shaped — the backend decodes the PKCS#10 and this is
   * what it returns — so nothing here parses a certificate. What matters is the
   * pair of fields no other screen puts side by side: `requestor` is who asked,
   * `subject` is who they asked to become.
   */
  certificateRequests: {
    items: [
      {
        // The overwhelmingly common case: a kubelet renewing its own
        // certificate. Requestor and subject match, so it raises nothing.
        name: 'csr-kubelet-renew',
        signer_name: 'kubernetes.io/kubelet-serving',
        requestor: 'system:node:ip-10-0-1-4',
        requestor_groups: ['system:nodes', 'system:authenticated'],
        usages: ['digital signature', 'key encipherment', 'server auth'],
        expiration_seconds: null,
        state: 'Pending',
        issued: false,
        conditions: {},
        signer_known: true,
        subject: {
          common_name: 'system:node:ip-10-0-1-4',
          common_names: ['system:node:ip-10-0-1-4'],
          organizations: ['system:nodes'],
          organizational_units: [],
        },
        dns_names: ['ip-10-0-1-4'],
        ip_addresses: ['10.0.1.4'],
        email_addresses: [],
        uris: [],
        key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
        signature_valid: true,
        signature_algorithm: 'sha256',
        decode_error: null,
        age_seconds: 240,
      },
      {
        // The one this whole section exists for. `kubectl get csr` shows
        // "dev@example.com" in the REQUESTOR column and nothing else.
        name: 'csr-escalation',
        signer_name: 'kubernetes.io/kube-apiserver-client',
        requestor: 'dev@example.com',
        requestor_groups: ['system:authenticated'],
        usages: ['digital signature', 'client auth'],
        expiration_seconds: null,
        state: 'Pending',
        issued: false,
        conditions: {},
        signer_known: true,
        subject: {
          common_name: 'dev@example.com',
          common_names: ['dev@example.com'],
          organizations: ['system:masters'],
          organizational_units: [],
        },
        dns_names: [],
        ip_addresses: [],
        email_addresses: [],
        uris: [],
        key: { algorithm: 'RSA', size: 2048, curve: null },
        signature_valid: true,
        signature_algorithm: 'sha256',
        decode_error: null,
        age_seconds: 95,
      },
      {
        // A node joining: a bootstrap token asking for a kubelet identity.
        name: 'csr-bootstrap',
        signer_name: 'kubernetes.io/kube-apiserver-client-kubelet',
        requestor: 'system:bootstrap:07401b',
        requestor_groups: ['system:bootstrappers'],
        usages: ['digital signature', 'client auth'],
        expiration_seconds: null,
        state: 'Pending',
        issued: false,
        conditions: {},
        signer_known: true,
        subject: {
          common_name: 'system:node:ip-10-0-1-9',
          common_names: ['system:node:ip-10-0-1-9'],
          organizations: ['system:nodes'],
          organizational_units: [],
        },
        dns_names: [],
        ip_addresses: [],
        email_addresses: [],
        uris: [],
        key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
        signature_valid: true,
        signature_algorithm: 'sha256',
        decode_error: null,
        age_seconds: 30,
      },
      {
        // Nothing built in signs this signerName.
        name: 'csr-custom-signer',
        signer_name: 'example.com/mesh-ca',
        requestor: 'system:serviceaccount:mesh:issuer',
        requestor_groups: ['system:serviceaccounts'],
        usages: ['digital signature', 'client auth'],
        expiration_seconds: 86400,
        state: 'Pending',
        issued: false,
        conditions: {},
        signer_known: false,
        subject: {
          common_name: 'sidecar.mesh',
          common_names: ['sidecar.mesh'],
          organizations: [],
          organizational_units: [],
        },
        dns_names: ['sidecar.mesh.svc'],
        ip_addresses: [],
        email_addresses: [],
        uris: [],
        key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
        signature_valid: true,
        signature_algorithm: 'sha256',
        decode_error: null,
        age_seconds: 610,
      },
      {
        // Nobody could read this one. `subject: null` — not an empty subject,
        // which would render as a certificate asking for nothing.
        name: 'csr-unreadable',
        signer_name: 'kubernetes.io/kube-apiserver-client',
        requestor: 'automation@example.com',
        requestor_groups: ['system:authenticated'],
        usages: ['client auth'],
        expiration_seconds: null,
        state: 'Pending',
        issued: false,
        conditions: {},
        signer_known: true,
        subject: null,
        dns_names: null,
        ip_addresses: null,
        email_addresses: null,
        uris: null,
        key: null,
        signature_valid: null,
        signature_algorithm: null,
        decode_error: 'Not a PEM certificate request this console could parse: MalformedFraming',
        age_seconds: 1200,
      },
      {
        // Approved, and no certificate. The state the console refuses to draw
        // as a success.
        name: 'csr-approved-unissued',
        signer_name: 'example.com/mesh-ca',
        requestor: 'system:serviceaccount:mesh:issuer',
        requestor_groups: ['system:serviceaccounts'],
        usages: ['client auth'],
        expiration_seconds: null,
        state: 'Approved',
        issued: false,
        conditions: {
          Approved: { status: 'True', reason: 'K8BossAdminDecision', message: null,
                      lastUpdateTime: '2026-09-05T20:04:00Z' },
        },
        signer_known: false,
        subject: {
          common_name: 'gateway.mesh',
          common_names: ['gateway.mesh'],
          organizations: [],
          organizational_units: [],
        },
        dns_names: [],
        ip_addresses: [],
        email_addresses: [],
        uris: [],
        key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
        signature_valid: true,
        signature_algorithm: 'sha256',
        decode_error: null,
        age_seconds: 3600,
      },
      {
        name: 'csr-issued',
        signer_name: 'kubernetes.io/kubelet-serving',
        requestor: 'system:node:ip-10-0-1-5',
        requestor_groups: ['system:nodes'],
        usages: ['server auth'],
        expiration_seconds: null,
        state: 'Issued',
        issued: true,
        conditions: {
          Approved: { status: 'True', reason: 'AutoApproved', message: null,
                      lastUpdateTime: '2026-09-05T19:30:00Z' },
        },
        signer_known: true,
        subject: {
          common_name: 'system:node:ip-10-0-1-5',
          common_names: ['system:node:ip-10-0-1-5'],
          organizations: ['system:nodes'],
          organizational_units: [],
        },
        dns_names: ['ip-10-0-1-5'],
        ip_addresses: ['10.0.1.5'],
        email_addresses: [],
        uris: [],
        key: { algorithm: 'ECDSA', size: 256, curve: 'secp256r1' },
        signature_valid: true,
        signature_algorithm: 'sha256',
        decode_error: null,
        age_seconds: 5400,
      },
    ],
    unavailable: [],
    partial: false,
  },

  snapshots: {
    items: [
      {
        name: 'postgres-nightly',
        namespace: 'prod',
        source_claim: 'postgres-data',
        source_content: null,
        snapshot_class: 'csi-ebs',
        ready_to_use: true,
        bound_content: 'snapcontent-9f2a',
        creation_time: '2026-09-05T02:00:11Z',
        restore_size_bytes: 53687091200,
        error: null,
        age_seconds: 39600,
      },
      {
        // Being taken right now. `null`, not false — the difference between
        // "your backup is running" and "your backup failed".
        name: 'postgres-before-upgrade',
        namespace: 'prod',
        source_claim: 'postgres-data',
        source_content: null,
        snapshot_class: 'csi-ebs',
        ready_to_use: null,
        bound_content: null,
        creation_time: null,
        restore_size_bytes: null,
        error: null,
        age_seconds: 12,
      },
      {
        name: 'analytics-failed',
        namespace: 'prod',
        source_claim: 'analytics-data',
        source_content: null,
        snapshot_class: 'csi-ebs',
        ready_to_use: false,
        bound_content: null,
        creation_time: null,
        restore_size_bytes: null,
        error: {
          message:
            'Failed to check and update snapshot content: failed to take snapshot of the '
            + 'volume vol-04f2: rpc error: code = ResourceExhausted',
          time: '2026-09-05T09:12:00Z',
        },
        age_seconds: 7200,
      },
      {
        name: 'imported-2026-08',
        namespace: 'prod',
        source_claim: null,
        source_content: 'snapcontent-imported',
        snapshot_class: null,
        ready_to_use: true,
        bound_content: 'snapcontent-imported',
        creation_time: '2026-08-01T00:00:00Z',
        restore_size_bytes: 107374182400,
        error: null,
        age_seconds: 3024000,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  /** §22. One class that destroys on delete, one that retains. */
  snapshotClasses: {
    items: [
      {
        name: 'csi-ebs',
        driver: 'ebs.csi.aws.com',
        deletion_policy: 'Delete',
        is_default: true,
        age_seconds: 8640000,
      },
      {
        name: 'csi-ebs-retain',
        driver: 'ebs.csi.aws.com',
        deletion_policy: 'Retain',
        is_default: false,
        age_seconds: 8640000,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  claims: {
    items: [
      {
        name: 'postgres-data',
        namespace: 'prod',
        status: 'Bound',
        volume: 'pvc-9f2a',
        capacity_bytes: 50 * 1024 * 1024 * 1024,
        requested: '50Gi',
        requested_bytes: 50 * 1024 * 1024 * 1024,
        access_modes: ['ReadWriteOnce'],
        storage_class: 'gp3',
        age_seconds: 86400,
      },
      {
        // Mid-expansion: it asked for 100Gi and the volume still provides 50.
        name: 'metrics-data',
        namespace: 'prod',
        status: 'Bound',
        volume: 'pvc-1c8e',
        capacity_bytes: 50 * 1024 * 1024 * 1024,
        requested: '100Gi',
        requested_bytes: 100 * 1024 * 1024 * 1024,
        access_modes: ['ReadWriteOnce'],
        storage_class: 'gp3',
        age_seconds: 7200,
      },
      {
        name: 'unbound-data',
        namespace: 'prod',
        status: 'Pending',
        volume: null,
        capacity_bytes: null,
        requested: '10Gi',
        requested_bytes: 10 * 1024 * 1024 * 1024,
        access_modes: ['ReadWriteOnce'],
        storage_class: 'slow',
        age_seconds: 60,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  /**
   * §21. Three autoscalers, one of each state the table has to keep apart: one
   * scaling normally, one **inert** — ScalingActive false because the metrics
   * API is gone, which looks entirely ordinary in `kubectl get hpa` — and one
   * the controller has not observed at all, whose conditions are absent rather
   * than false.
   */
  autoscalers: {
    items: [
      {
        name: 'checkout-hpa',
        namespace: 'prod',
        target: { api_version: 'apps/v1', kind: 'Deployment', name: 'checkout' },
        min_replicas: 2,
        max_replicas: 10,
        current_replicas: 8,
        desired_replicas: 8,
        metrics: [
          { kind: 'Resource', name: 'cpu', container: null,
            target_type: 'Utilization', target: '70%', current: '81%' },
        ],
        able_to_scale: true,
        scaling_active: true,
        // At its ceiling and wanting more — during an incident this is the
        // answer to "why is this not scaling up".
        scaling_limited: true,
        conditions: {
          AbleToScale: { status: 'True', reason: 'ReadyForNewScale', message: null },
          ScalingActive: { status: 'True', reason: 'ValidMetricFound', message: null },
          ScalingLimited: { status: 'True', reason: 'TooManyReplicas',
                            message: 'the desired replica count is more than the maximum' },
        },
        age_seconds: 864000,
      },
      {
        name: 'payments-hpa',
        namespace: 'prod',
        target: { api_version: 'apps/v1', kind: 'Deployment', name: 'payments' },
        min_replicas: 3,
        max_replicas: 20,
        current_replicas: 3,
        desired_replicas: 3,
        // The metric is declared and has no reading. `null`, never 0.
        metrics: [
          { kind: 'Resource', name: 'cpu', container: null,
            target_type: 'Utilization', target: '70%', current: null },
        ],
        able_to_scale: true,
        scaling_active: false,
        scaling_limited: null,
        conditions: {
          AbleToScale: { status: 'True', reason: 'SucceededGetScale', message: null },
          ScalingActive: {
            status: 'False', reason: 'FailedGetResourceMetric',
            message: 'unable to get metrics for resource cpu: no metrics returned from resource metrics API',
          },
          ScalingLimited: null,
        },
        age_seconds: 3600,
      },
      {
        name: 'fresh-hpa',
        namespace: 'prod',
        target: { api_version: 'apps/v1', kind: 'Deployment', name: 'reports' },
        min_replicas: 1,
        max_replicas: 5,
        current_replicas: null,
        desired_replicas: null,
        metrics: [
          { kind: 'Resource', name: 'cpu', container: null,
            target_type: 'Utilization', target: '80%', current: null },
        ],
        able_to_scale: null,
        scaling_active: null,
        scaling_limited: null,
        conditions: { AbleToScale: null, ScalingActive: null, ScalingLimited: null },
        age_seconds: 20,
      },
    ],
    continue: null,
    remaining: null,
    partial: false,
    unavailable: [],
  },

  /**
   * §19. A cluster with one of each finding, because the page's whole job is to
   * surface them: a lease that stopped renewing, an aggregated API that is not
   * Available, a CRD that never Established, a `failurePolicy: Fail` webhook
   * with nothing behind its Service, and a node three minors adrift.
   *
   * Every section is populated here. The `null` sections — the ones that mean
   * "we could not look" — are set per-spec, because that is the assertion
   * rather than the backdrop.
   */
  clusterStatus: {
    controlPlane: [
      {
        name: 'kube-controller-manager',
        holder: 'ip-10-0-1-4_9f2a',
        renewTime: '2026-08-19T09:59:58Z',
        seconds_since_renew: 2,
        lease_duration_seconds: 15,
        stale: false,
        well_known: true,
      },
      {
        name: 'kube-scheduler',
        holder: 'ip-10-0-1-5_1c8e',
        renewTime: '2026-08-19T09:52:10Z',
        seconds_since_renew: 470,
        lease_duration_seconds: 15,
        stale: true,
        well_known: true,
      },
      {
        // Acquired by nobody yet: `stale` is null, and the page must render an
        // em dash rather than either verdict.
        name: 'external-dns-controller',
        holder: null,
        renewTime: null,
        seconds_since_renew: null,
        lease_duration_seconds: 15,
        stale: null,
        well_known: false,
      },
    ],
    apiServices: {
      items: [
        {
          name: 'v1beta1.metrics.k8s.io',
          group: 'metrics.k8s.io',
          version: 'v1beta1',
          service: { namespace: 'kube-system', name: 'metrics-server' },
          available: false,
          reason: 'FailedDiscoveryCheck',
          message: 'failing or missing response from https://10.96.0.9:443/apis/metrics.k8s.io/v1beta1',
          since: '2026-08-19T08:40:00Z',
        },
        {
          name: 'v1.packages.operators.coreos.com',
          group: 'packages.operators.coreos.com',
          version: 'v1',
          service: { namespace: 'olm', name: 'packageserver-service' },
          available: true,
          reason: 'Passed',
          message: 'all checks passed',
          since: '2026-08-18T22:15:00Z',
        },
      ],
      local_count: 28,
      unavailable_count: 1,
    },
    crds: {
      items: [
        {
          name: 'widgets.example.test',
          group: 'example.test',
          established: false,
          reason: 'NotAccepted',
          message: 'not all names are accepted',
          non_structural: null,
        },
      ],
      total: 74,
      unhealthy_count: 1,
    },
    webhooks: {
      items: [
        {
          kind: 'ValidatingWebhookConfiguration',
          configuration: 'policy.example.test',
          name: 'validate.policy.example.test',
          failure_policy: 'Fail',
          timeout_seconds: 10,
          side_effects: 'None',
          url: null,
          service: { namespace: 'policy-system', name: 'policy-webhook', port: 443 },
          endpoint_count: 0,
        },
        {
          kind: 'MutatingWebhookConfiguration',
          configuration: 'sidecar-injector',
          name: 'inject.sidecar.example.test',
          failure_policy: 'Ignore',
          timeout_seconds: 5,
          side_effects: 'None',
          url: null,
          service: { namespace: 'mesh', name: 'injector', port: 443 },
          endpoint_count: 3,
        },
        {
          // Addressed by URL: there is nothing in the cluster to count, which
          // is not the same as counting zero.
          kind: 'ValidatingWebhookConfiguration',
          configuration: 'external-audit',
          name: 'audit.external.example.test',
          failure_policy: 'Ignore',
          timeout_seconds: 5,
          side_effects: 'None',
          url: 'https://audit.example.test/admit',
          service: null,
          endpoint_count: null,
        },
      ],
      blocking_count: 1,
      complete: true,
    },
    versionSkew: {
      server_version: 'v1.31.4',
      supported_minors_behind: 3,
      nodes: [
        { node: 'ip-10-0-1-9', kubelet_version: 'v1.26.15', status: 'behind' },
        { node: 'ip-10-0-1-4', kubelet_version: 'v1.31.4', status: 'ok' },
        { node: 'ip-10-0-1-5', kubelet_version: 'v1.30.6-eks-abc1234', status: 'ok' },
      ],
      out_of_skew_count: 1,
    },
    partial: false,
    unavailable: [],
  },
};

/** Answer every /api call from the fixtures above. */
/** The §17 objects a request renders, in the order the backend writes them. */
function projectObjectsFor(body) {
  const objects = [
    { kind: 'Namespace', name: body.name, namespace: null, group: '', version: 'v1', resource: 'namespaces',
      yaml: `apiVersion: v1\nkind: Namespace\nmetadata:\n  name: ${body.name}\n` },
  ];
  if (body.quota && Object.keys(body.quota).length) {
    objects.push({ kind: 'ResourceQuota', name: 'project-quota', namespace: body.name, group: '', version: 'v1', resource: 'resourcequotas',
      yaml: `apiVersion: v1\nkind: ResourceQuota\nmetadata:\n  name: project-quota\n  namespace: ${body.name}\n` });
  }
  if (body.limits?.length) {
    objects.push({ kind: 'LimitRange', name: 'project-limits', namespace: body.name, group: '', version: 'v1', resource: 'limitranges',
      yaml: `apiVersion: v1\nkind: LimitRange\nmetadata:\n  name: project-limits\n  namespace: ${body.name}\n` });
  }
  if (body.admins?.length) {
    objects.push({ kind: 'RoleBinding', name: body.adminRole || 'admin', namespace: body.name, group: 'rbac.authorization.k8s.io', version: 'v1', resource: 'rolebindings',
      yaml: `apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nmetadata:\n  name: ${body.adminRole || 'admin'}\n  namespace: ${body.name}\n` });
  }
  if (body.isolateIngress) {
    objects.push({ kind: 'NetworkPolicy', name: 'allow-same-namespace', namespace: body.name, group: 'networking.k8s.io', version: 'v1', resource: 'networkpolicies',
      yaml: `apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: allow-same-namespace\n  namespace: ${body.name}\n` });
  }
  return objects;
}

/** The consequences the backend derives from what a request leaves out. */
function projectConsequencesFor(body) {
  const out = [];
  const enforce = body.podSecurity?.enforce ?? null;
  if (enforce == null) {
    out.push({ code: 'psa_not_enforced', label: 'No Pod Security level is enforced',
      consequence: 'Without an enforce label, whatever the cluster default is applies, and this console cannot read it.',
      mitigation: 'Enforce baseline or restricted.' });
  } else if (enforce !== 'privileged') {
    out.push({ code: 'psa_enforced', label: `Pods that do not meet the ${enforce} level are refused`,
      consequence: 'A Deployment whose template violates it is accepted, and then its ReplicaSet fails to create a single pod.',
      mitigation: 'Set warn and audit to the same level.' });
  }
  if (!body.quota || !Object.keys(body.quota).length) {
    out.push({ code: 'no_quota', label: 'Nothing bounds what this project can consume',
      consequence: 'With no ResourceQuota, one Deployment can request every core the cluster has.',
      mitigation: 'Set at least requests.cpu, requests.memory and pods.' });
  }
  if (!body.admins?.length) {
    out.push({ code: 'no_admin', label: 'Nobody is bound to this project',
      consequence: 'No RoleBinding is created, so only cluster-wide bindings act here.',
      mitigation: 'Name a User, Group or ServiceAccount as its admin.' });
  }
  if (body.isolateIngress) {
    out.push({ code: 'ingress_isolated', label: "Traffic from other namespaces is refused, the router's included",
      consequence: 'An Ingress or Route to a Service here will not be served until a second policy admits the router.',
      mitigation: "Add a policy admitting the router's namespace after creating the project." });
  }
  return out;
}

/**
 * §18 the labels a namespace declares now, from the project fixture, so the
 * plan's `current` and the page's card cannot disagree.
 */
export function currentPodSecurity(project = FIXTURES.project) {
  return project.podSecurity;
}

/**
 * §18 `POST /projects/{name}/pod-security/plan`.
 *
 * `consequences` is derived from the difference between what the namespace
 * declares and what the body asks for — never echoed — so a dialog that never
 * reads them cannot pass.
 */
/**
 * §20's plan for one claim, derived from the size asked for.
 *
 * Derived rather than fixed, because the plan's whole shape depends on the
 * number: too small and it comes back `blocked` with the claim still described,
 * large enough and it comes back with consequences to acknowledge. A fixture
 * that always answered one of those could not test the other.
 */
/**
 * §21's plan for one autoscaler, derived from the body the way the backend is.
 *
 * Derived rather than canned so a spec cannot assert against a consequence list
 * the real endpoint would not have produced for those bounds — the two that
 * depend on the running replica count are exactly the ones whose arithmetic,
 * got backwards in a fixture, would hide the bug they exist to catch.
 */
export function boundsPlanFor(body, { autoscaler = null } = {}) {
  const hpa = autoscaler ?? FIXTURES.autoscalers.items[0];
  const running = hpa.current_replicas;
  const consequences = [];

  if (hpa.scaling_active === false) {
    consequences.push({
      code: 'hpa_not_scaling',
      label: 'This autoscaler is not scaling anything right now',
      consequence:
        'Its ScalingActive condition is false, so the controller cannot compute a desired ' +
        `replica count (${hpa.conditions?.ScalingActive?.reason}: ` +
        `${hpa.conditions?.ScalingActive?.message}). Changing the bounds is still a real write ` +
        'and the new numbers will be stored — but nothing scales until the controller can read ' +
        'its metrics again, and the workload stays at whatever count it has.',
      mitigation:
        'Check that the metrics API this autoscaler needs is answering — the cluster status page ' +
        'lists aggregated APIServices and says which are unavailable.',
    });
  }
  if (hpa.able_to_scale === false) {
    consequences.push({
      code: 'hpa_cannot_reach_target',
      label: 'This autoscaler cannot reach the workload it targets',
      consequence: 'Its AbleToScale condition is false. New bounds do not fix that.',
      mitigation: 'Check that the target still exists under the name and kind the autoscaler names.',
    });
  }
  if (running == null) {
    consequences.push({
      code: 'hpa_replicas_unknown',
      label: 'Whether this takes effect immediately is unknown',
      consequence:
        "This autoscaler's status carries no current replica count, so this console cannot tell " +
        'you whether the bounds you are setting are above or below what is running.',
      mitigation: "Read the workload's own replica count before confirming.",
    });
  } else {
    if (body.maxReplicas < running) {
      consequences.push({
        code: 'hpa_max_below_current',
        label: `${running - body.maxReplicas} pod(s) are terminated as soon as this is written`,
        consequence:
          `${running} replicas are running and the new ceiling is ${body.maxReplicas}. An HPA ` +
          'clamps to its bounds at its next scale decision, which is seconds away — this is not ' +
          'a limit that applies to future growth, it is a scale-down now.',
        mitigation: `Set maxReplicas to ${running} or above to cap growth without shedding what is running.`,
      });
    }
    if (body.minReplicas > running) {
      consequences.push({
        code: 'hpa_min_above_current',
        label: `${body.minReplicas - running} pod(s) are started as soon as this is written`,
        consequence:
          `${running} replicas are running and the new floor is ${body.minReplicas}. The HPA ` +
          'scales up to the floor regardless of what the metrics say.',
        mitigation: 'Check the cluster has room for them.',
      });
    }
  }
  if (body.minReplicas === 0) {
    consequences.push({
      code: 'hpa_scale_to_zero_gated',
      label: 'A floor of zero needs a feature gate this console cannot read',
      consequence:
        'minReplicas: 0 is only accepted when the API server runs with the HPAScaleToZero ' +
        'feature gate enabled. No API reports whether it is.',
      mitigation: 'Preview first — a dry run goes through the same validation as the real write.',
    });
  }

  const unchanged =
    body.minReplicas === hpa.min_replicas && body.maxReplicas === hpa.max_replicas;

  return {
    namespace: hpa.namespace,
    name: hpa.name,
    current: hpa,
    requested: { minReplicas: body.minReplicas, maxReplicas: body.maxReplicas },
    resourceVersion: '9040',
    blocked: unchanged
      ? {
          message: `This autoscaler already runs between ${hpa.min_replicas} and ${hpa.max_replicas} replicas.`,
          hint: 'Change one of the bounds, or nothing needs to happen.',
          context: { minReplicas: hpa.min_replicas, maxReplicas: hpa.max_replicas },
        }
      : null,
    // Never both, exactly as the endpoint guarantees.
    consequences: unchanged ? [] : consequences,
    gate: { enabled: true, detail: 'This deployment permits setting autoscaler bounds.' },
  };
}

/**
 * §24 — does one toleration tolerate one taint? The API server's rules, kept in
 * step with `app.resources.shaping.toleration_tolerates_taint`.
 *
 * Reproduced rather than canned for §21's reason: the two rules an
 * approximation gets wrong — a blank `effect` matching every effect, and an
 * empty key being a wildcard only with `Exists` — are exactly the ones whose
 * mistake in a fixture would hide the bug it exists to catch.
 */
function toleratesTaint(toleration, taint) {
  if (toleration.effect && toleration.effect !== taint.effect) return false;
  const operator = toleration.operator || 'Equal';
  if (!toleration.key) return operator === 'Exists';
  if (toleration.key !== taint.key) return false;
  if (operator === 'Exists') return true;
  return (toleration.value || '') === (taint.value || '');
}

/** §24's deletion plan, derived: which pods a NoExecute taint removes, and when. */
export function deletionPlanFor(taints, pods) {
  const enforced = taints.filter((taint) => taint.effect === 'NoExecute');
  const rows = [];
  for (const pod of pods) {
    let soonest = null;
    for (const taint of enforced) {
      const matches = (pod.tolerations || []).filter((t) => toleratesTaint(t, taint));
      if (matches.some((t) => t.tolerationSeconds == null)) continue;
      const delay = matches.length
        ? Math.min(...matches.map((t) => Math.max(0, t.tolerationSeconds)))
        : 0;
      if (soonest == null || delay < soonest.delay_seconds) {
        soonest = { delay_seconds: delay, taint };
      }
    }
    if (!soonest) continue;
    rows.push({
      namespace: pod.namespace, pod: pod.pod, controller: pod.controller,
      taint: soonest.taint, delay_seconds: soonest.delay_seconds,
    });
  }
  return rows;
}

/**
 * §24's taint plan, derived from the body the way the backend is.
 *
 * `podsChecked: false` produces `deleting: null` — not an empty list — because
 * that is the distinction the dialog is built around and a fixture that blurred
 * it would let a panel drawing "deletes nothing" over an unread node pass.
 */
export function taintPlanFor(body, { node = null, pods = null, podsChecked = true } = {}) {
  const live = node ?? FIXTURES.nodeDetail;
  const onNode = pods ?? FIXTURES.nodeSchedulingPods;
  const current = live.taints ?? [];
  const requested = body.taints ?? [];

  const identity = (t) => `${t.key}:${t.effect}`;
  const have = new Map(current.map((t) => [identity(t), t]));
  const want = new Map(requested.map((t) => [identity(t), t]));
  const added = requested.filter((t) => !have.has(identity(t)));
  const removed = current.filter((t) => !want.has(identity(t)));
  const changed = requested
    .filter((t) => have.has(identity(t)) && (have.get(identity(t)).value || '') !== (t.value || ''))
    .map((t) => ({ before: have.get(identity(t)), after: t }));

  const enforced = [...added, ...changed.map((c) => c.after)]
    .filter((t) => t.effect === 'NoExecute');
  const deleting = podsChecked ? deletionPlanFor(enforced, onNode) : null;
  const unchanged = !added.length && !removed.length && !changed.length;

  const consequences = [];
  if (!unchanged) {
    if (enforced.length && deleting === null) {
      consequences.push({
        code: 'taint_pods_unknown',
        label: 'Which pods this deletes could not be worked out',
        consequence:
          'This change adds a NoExecute taint and the pod listing for this node failed, so ' +
          'this console cannot tell you which pods those are. It is not reporting there are none.',
        mitigation: 'Retry once the listing works, or read the node\'s pods another way.',
      });
    }
    if (deleting && deleting.length) {
      const delayed = deleting.filter((row) => row.delay_seconds > 0);
      consequences.push({
        code: 'taint_deletes_pods',
        label: `${deleting.length} pod(s) on this node are deleted by this taint`,
        consequence:
          'That removal is a delete, not an eviction: it does not go through the ' +
          'pods/eviction subresource, so PodDisruptionBudgets do not apply to it.',
        mitigation: 'Drain the node instead if you want budgets honoured, or use NoSchedule.',
      });
      if (deleting.some((row) => row.controller === null)) {
        consequences.push({
          code: 'taint_deletes_unmanaged',
          label: 'Some of them have no controller and will not come back anywhere',
          consequence: 'A pod with no controller is deleted and that is the end of it.',
          mitigation: 'Check whether any of them is holding something you need.',
        });
      }
      if (deleting.some((row) => row.controller?.kind === 'DaemonSet')) {
        consequences.push({
          code: 'taint_deletes_daemonset',
          label: 'DaemonSet pods are removed and not replaced here',
          consequence:
            'The DaemonSet controller tolerates the node\'s own condition taints and nothing ' +
            'else, so its pods are deleted and not placed back while the taint stands.',
          mitigation: 'Add a matching toleration to the DaemonSets that must keep running here.',
        });
      }
      if (delayed.length) {
        consequences.push({
          code: 'taint_delayed_deletion',
          label: `${delayed.length} of them go later, the first in ` +
            `${Math.min(...delayed.map((row) => row.delay_seconds))}s`,
          consequence:
            'These pods carry a tolerationSeconds, so the node looks unaffected for as long as ' +
            'that timer runs and then empties on its own.',
          mitigation: 'Expect the node to keep changing after this write completes.',
        });
      }
    }
    if (removed.length) {
      consequences.push({
        code: 'taint_removed',
        label: 'This node stops excluding work',
        consequence: 'The scheduler stops treating this node as reserved.',
        mitigation: 'Cordon the node first if you want the taint gone without new work arriving.',
      });
    }
    if (removed.some((t) => String(t.key).startsWith('node-role.kubernetes.io/control-plane') ||
                            String(t.key).startsWith('node-role.kubernetes.io/master'))) {
      consequences.push({
        code: 'taint_control_plane_opened',
        label: 'This removes the taint that keeps ordinary workloads off a control-plane node',
        consequence: 'Application pods will be placed alongside the API server and etcd.',
        mitigation: 'On a cluster with worker nodes this is almost never what was intended.',
      });
    }
  }

  return {
    name: live.name,
    resourceVersion: '884213',
    current,
    requested,
    added,
    removed,
    changed,
    deleting,
    pods_checked: podsChecked,
    unavailable: podsChecked
      ? []
      : [{ group: '', resource: 'pods', reason: 'forbidden',
           message: 'pods is forbidden at the cluster scope' }],
    blocked: unchanged
      ? { message: "This node's taints already match what you sent.",
          hint: 'Change a taint, or nothing needs to happen.', context: {} }
      : null,
    consequences: unchanged ? [] : consequences,
    gate: { enabled: true, detail: 'This deployment permits editing node taints.' },
  };
}

/**
 * §25's plan, derived from the request and the decision the way the backend is.
 *
 * Derived rather than canned for §21's and §24's reason: the consequences here
 * turn on fields a fixture could get backwards — whether the subject is the
 * requestor, whether the organizations include `system:masters` — and those are
 * exactly the ones whose mistake would hide the bug they exist to catch.
 */
export function csrPlanFor(name, decision, { requests = null } = {}) {
  const rows = requests ?? FIXTURES.certificateRequests.items;
  const row = rows.find((item) => item.name === name);
  if (!row) throw new Error(`no such certificate request in the fixture: ${name}`);

  const organizations = row.subject?.organizations ?? [];
  const commonName = row.subject?.common_name ?? null;
  const consequences = [];
  const decided = row.state !== 'Pending';

  if (!decided) {
    if (row.decode_error) {
      consequences.push({
        code: 'csr_request_undecodable',
        label: 'This console could not read what the request asks for',
        consequence:
          `${row.decode_error} So the subject, the organizations and the SANs above are ` +
          'unknown — not empty.',
        mitigation: 'Decode spec.request yourself before deciding, or deny it.',
      });
    }
    if (row.signature_valid === false) {
      consequences.push({
        code: 'csr_signature_invalid',
        label: 'The request is not signed by the key it carries',
        consequence: 'Whoever submitted it may not hold the key the certificate would be issued for.',
        mitigation: 'Deny it and ask for a freshly generated request.',
      });
    }
    if (decision === 'Approved') {
      if (organizations.includes('system:masters')) {
        consequences.push({
          code: 'csr_grants_cluster_admin',
          label: 'This grants cluster-admin — the certificate asks for system:masters',
          consequence:
            "The API server's authorizer treats system:masters as cluster-admin before RBAC is " +
            'consulted, so no Role limits it and no RoleBinding takes it back. Only rotating the ' +
            'signing CA revokes it.',
          mitigation: 'Issue an ordinary client certificate and bind it to a ClusterRole instead.',
        });
      }
      if (organizations.includes('system:nodes') && commonName !== row.requestor) {
        consequences.push({
          code: 'csr_grants_node_identity',
          label: 'This grants a node identity to something that is not that node',
          consequence:
            `A certificate in system:nodes with the common name ${commonName} is evaluated by ` +
            `the node authorizer. ${row.requestor} is asking for it.`,
          mitigation: 'Check that this really is a node joining before approving.',
        });
      }
      if (commonName && row.requestor && commonName !== row.requestor) {
        consequences.push({
          code: 'csr_subject_is_not_requestor',
          label: 'The identity being requested is not the identity that asked',
          consequence:
            `${row.requestor} submitted this request, and the certificate would carry the ` +
            `common name ${commonName}.`,
          mitigation: 'Confirm the requestor is entitled to act for that identity.',
        });
      }
      if (row.signer_known === false) {
        consequences.push({
          code: 'csr_no_known_signer',
          label: `Nothing built into this cluster signs ${row.signer_name}`,
          consequence:
            'If no controller for this signerName is running, approving this leaves the request ' +
            'Approved with no certificate — indefinitely, and looking exactly like a success.',
          mitigation: 'Re-read the request afterwards: Issued, not Approved, is what says a certificate exists.',
        });
      }
    }
    if (decision === 'Denied' && commonName?.startsWith('system:node:')) {
      consequences.push({
        code: 'csr_deny_blocks_node',
        label: `This is ${commonName.slice('system:node:'.length)}'s own certificate request`,
        consequence:
          'A node joining the cluster will not become Ready, and a node rotating an expiring ' +
          'certificate will stop being able to talk to the API server.',
        mitigation: 'Deny it if the request is not really from that node.',
      });
    }
  }

  return {
    name,
    resourceVersion: '7719',
    decision,
    request: row,
    blocked: decided
      ? {
          message: `This request was already ${row.state.toLowerCase()}.`,
          hint: 'A decision cannot be changed — the requester has to submit a new request.',
          context: { parameter: 'decision', state: row.state },
        }
      : null,
    consequences: decided ? [] : consequences,
    gate: { enabled: true, detail: 'This deployment permits deciding certificate signing requests.' },
  };
}

/** §25's write: the §1.5 envelope plus the three keys §25 adds. */
export function csrDecisionFor(name, body, options = {}) {
  const plan = csrPlanFor(name, body.decision, options);
  return {
    dryRun: body.dryRun !== false,
    // Derived, never echoed: §1.5 makes `applied` the only evidence of a change.
    applied: body.dryRun === false,
    verb: 'update',
    target: {
      group: 'certificates.k8s.io', version: 'v1',
      resource: 'certificatesigningrequests', subresource: 'approval', name,
    },
    diff: {
      unified:
        '--- live\n+++ projected\n@@\n' +
        '+  conditions:\n' +
        `+    - type: ${body.decision}\n+      status: "True"\n`,
      digest: 'sha256:csr',
      changed: true,
    },
    resourceVersion: '7720',
    warnings: [],
    auditId: 5170,
    decision: body.decision,
    // The request **as it was read**, so `applied: true` and "a certificate
    // exists" stay two different statements in the same response.
    request: plan.request,
    consequences: plan.consequences,
  };
}

/** §24's taint write: the §1.5 envelope plus the keys §24 adds. */
export function taintWriteFor(body, options = {}) {
  const plan = taintPlanFor(body, options);
  return {
    dryRun: body.dryRun !== false,
    // Derived, never echoed: §1.5 makes `applied` the only evidence of a change.
    applied: body.dryRun === false,
    verb: 'patch',
    target: { group: '', version: 'v1', resource: 'nodes', name: plan.name },
    diff: {
      unified:
        '--- live\n+++ projected\n@@\n' +
        plan.removed.map((t) => `-    - key: ${t.key}\n`).join('') +
        plan.added.map((t) => `+    - key: ${t.key}\n`).join(''),
      digest: 'sha256:taints',
      changed: true,
    },
    resourceVersion: '884214',
    warnings: [],
    auditId: 5160,
    current: plan.current,
    requested: plan.requested,
    deleting: plan.deleting,
    pods_checked: plan.pods_checked,
    unavailable: plan.unavailable,
    consequences: plan.consequences,
  };
}

/** §24's label plan, derived from the body the way the backend is. */
export function labelPlanFor(body, { node = null, pods = null, podsChecked = true } = {}) {
  const live = node ?? FIXTURES.nodeDetail;
  const onNode = pods ?? FIXTURES.nodeSchedulingPods;
  const current = live.labels ?? {};
  const requested = body.labels ?? {};

  const added = {};
  const changed = {};
  const removed = {};
  for (const [key, value] of Object.entries(requested)) {
    if (!(key in current)) added[key] = value;
    else if (current[key] !== value) changed[key] = { before: current[key], after: value };
  }
  for (const [key, value] of Object.entries(current)) {
    if (!(key in requested)) removed[key] = value;
  }

  const touched = [...new Set([...Object.keys(removed), ...Object.keys(changed)])].sort();
  const named = [...new Set([...Object.keys(added), ...touched])].sort();
  const dependents = podsChecked
    ? onNode
        .map((pod) => ({
          namespace: pod.namespace, pod: pod.pod, controller: pod.controller,
          keys: (pod.selectorKeys || []).filter((key) => touched.includes(key)),
        }))
        .filter((row) => row.keys.length)
    : null;

  const unchanged = !Object.keys(added).length && !Object.keys(changed).length &&
    !Object.keys(removed).length;

  const consequences = [];
  if (!unchanged) {
    if (Object.keys(removed).length) {
      consequences.push({
        code: 'label_removed',
        label: `${Object.keys(removed).length} label(s) removed: ${Object.keys(removed).sort().join(', ')}`,
        consequence:
          'Nothing running on this node is disturbed by this. Node affinity is ' +
          'requiredDuringSchedulingIgnoredDuringExecution, so pods scheduled here because of ' +
          'these labels keep running exactly as they are.',
        mitigation: 'The effect shows up at the next rollout rather than now.',
      });
    }
    if (named.some((key) => key.startsWith('kubernetes.io/') || key.startsWith('topology.kubernetes.io/') ||
                            key.startsWith('node.kubernetes.io/') || key.startsWith('beta.kubernetes.io/') ||
                            key.startsWith('k8s.io/') || key.startsWith('node-role.kubernetes.io/'))) {
      consequences.push({
        code: 'label_reserved_prefix',
        label: "This changes labels the cluster's own components own",
        consequence:
          'The kubelet re-applies some of these when it next registers and never re-applies ' +
          'others, and this console cannot tell you which.',
        mitigation: 'Prefer a label of your own for scheduling decisions.',
      });
    }
    if (named.some((key) => key.startsWith('node-role.kubernetes.io/'))) {
      consequences.push({
        code: 'label_role_changed',
        label: 'This changes what this node reports as its role',
        consequence: 'It decides the ROLES column in `kubectl get nodes` and on this console.',
        mitigation: 'If you are trying to stop work running here, a taint does that.',
      });
    }
    if (touched.length && dependents === null) {
      consequences.push({
        code: 'label_pods_unknown',
        label: 'Which pods depend on these labels could not be worked out',
        consequence: 'The pod listing for this node failed. It is not reporting that none do.',
        mitigation: 'Retry once the listing works.',
      });
    } else if (dependents && dependents.length) {
      consequences.push({
        code: 'label_pods_depend',
        label: `${dependents.length} pod(s) here were placed by a rule naming these labels`,
        consequence:
          dependents.map((row) => `${row.namespace}/${row.pod}`).join(', ') +
          ' declare a nodeSelector or required node affinity that mentions a key you are ' +
          'changing. They keep running — the rule is not re-evaluated.',
        mitigation: 'Check that each of them can be scheduled somewhere else.',
      });
    }
  }

  return {
    name: live.name,
    resourceVersion: '884213',
    current,
    requested,
    added,
    changed,
    removed,
    dependents,
    pods_checked: podsChecked,
    unavailable: podsChecked
      ? []
      : [{ group: '', resource: 'pods', reason: 'forbidden',
           message: 'pods is forbidden at the cluster scope' }],
    blocked: unchanged
      ? { message: "This node's labels already match what you sent.",
          hint: 'Change a label, or nothing needs to happen.', context: {} }
      : null,
    consequences: unchanged ? [] : consequences,
    gate: { enabled: true, detail: 'This deployment permits editing node labels.' },
  };
}

/** §24's label write: the §1.5 envelope plus the keys §24 adds. */
export function labelWriteFor(body, options = {}) {
  const plan = labelPlanFor(body, options);
  return {
    dryRun: body.dryRun !== false,
    applied: body.dryRun === false,
    verb: 'patch',
    target: { group: '', version: 'v1', resource: 'nodes', name: plan.name },
    diff: {
      unified:
        '--- live\n+++ projected\n@@\n' +
        Object.keys(plan.removed).map((key) => `-    ${key}\n`).join('') +
        Object.keys(plan.added).map((key) => `+    ${key}\n`).join(''),
      digest: 'sha256:labels',
      changed: true,
    },
    resourceVersion: '884214',
    warnings: [],
    auditId: 5161,
    current: plan.current,
    requested: plan.requested,
    dependents: plan.dependents,
    pods_checked: plan.pods_checked,
    unavailable: plan.unavailable,
    consequences: plan.consequences,
  };
}

/** §21's write response: the §1.5 envelope plus the three keys §21 adds. */
export function boundsWriteFor(body, { autoscaler = null } = {}) {
  const plan = boundsPlanFor(body, { autoscaler });
  const hpa = plan.current;
  return {
    dryRun: body.dryRun !== false,
    // Derived, never echoed from the request: §1.5 makes `applied` the only
    // evidence a cluster changed.
    applied: body.dryRun === false,
    verb: 'patch',
    target: {
      group: 'autoscaling', version: 'v2', resource: 'horizontalpodautoscalers',
      namespace: hpa.namespace, name: hpa.name,
    },
    diff: {
      unified:
        '--- live\n+++ projected\n@@\n' +
        `-  minReplicas: ${hpa.min_replicas}\n-  maxReplicas: ${hpa.max_replicas}\n` +
        `+  minReplicas: ${body.minReplicas}\n+  maxReplicas: ${body.maxReplicas}\n`,
      digest: 'sha256:hpa',
    },
    warnings: [],
    consequences: plan.consequences,
    current: hpa,
    requested: { minReplicas: body.minReplicas, maxReplicas: body.maxReplicas },
  };
}

/**
 * §22's plan, derived from the body the way the backend is.
 *
 * The deletion consequence is the one that depends on cluster state, so it is
 * computed here from the class rather than canned — a fixture that always
 * emitted it would let a dialog that ignores `Retain` pass, and one that never
 * did would hide the warning this feature exists to give.
 */
/**
 * §23's subject review, derived from the request the way the backend is.
 *
 * `groups_complete` is computed from the subject kind rather than canned,
 * because it is the field the whole panel turns on: a fixture that always said
 * `true` would let a panel that never renders the conditional-answer banner
 * pass, and one that always said `false` would hide the ServiceAccount case.
 */
export function subjectReviewFor(body, { outcomes = null } = {}) {
  const isServiceAccount = body.subject.kind === 'ServiceAccount';
  const supplied = body.subject.groups ?? [];
  const groups = isServiceAccount
    ? [
        'system:authenticated',
        'system:serviceaccounts',
        `system:serviceaccounts:${body.subject.namespace}`,
        ...supplied,
      ]
    : ['system:authenticated', ...supplied.filter((g) => g !== 'system:authenticated')];

  // One outcome per check, cycling through the four the table has to keep
  // apart: allowed, explicitly denied, nothing granted it, and undecided.
  const cycle = outcomes ?? [
    { allowed: true, denied: false, reason: 'RBAC: allowed by ClusterRoleBinding "admins"', evaluationError: null },
    { allowed: false, denied: true, reason: 'denied by webhook authorizer', evaluationError: null },
    { allowed: false, denied: false, reason: null, evaluationError: null },
    { allowed: false, denied: false, reason: null, evaluationError: 'the webhook authorizer timed out' },
  ];

  const results = (body.checks ?? []).map((check, i) => ({
    verb: check.verb,
    group: check.group ?? 'core',
    resource: check.resource,
    namespace: check.namespace ?? null,
    name: check.name ?? null,
    subresource: check.subresource ?? null,
    ...cycle[i % cycle.length],
  }));

  return {
    subject: {
      kind: body.subject.kind,
      name: body.subject.name,
      namespace: body.subject.namespace ?? null,
      username: isServiceAccount
        ? `system:serviceaccount:${body.subject.namespace}:${body.subject.name}`
        : body.subject.name,
      groups,
      groups_complete: isServiceAccount,
      groups_detail: isServiceAccount
        ? `The API server assigns exactly these groups to every ServiceAccount in ${body.subject.namespace}, so this answer is complete.`
        : "A user's real group memberships come from whatever authenticated them — OIDC "
          + 'claims, a certificate\u2019s organisation, a proxy header — and no API on this '
          + `cluster reports them. This answer is about a user named ${body.subject.name} in `
          + 'exactly the groups listed, which may be fewer than they actually hold.',
    },
    results,
    undecided: results.filter((r) => r.evaluationError).length,
    auditId: 4821,
  };
}

export function snapshotPlanFor(body, { claim = null, snapshotClass = undefined } = {}) {
  const target = claim ?? FIXTURES.claims.items[0];
  const resolved =
    snapshotClass === undefined
      ? { ...FIXTURES.snapshotClasses.items[0], reason: null,
          detail: 'No class was named, so the cluster default csi-ebs applies. '
            + 'Its deletionPolicy is Delete.' }
      : snapshotClass;

  const consequences = [
    {
      code: 'snapshot_is_not_a_backup',
      label: 'A snapshot is not a backup',
      consequence:
        'For nearly every CSI driver this is a point-in-time reference held inside the same '
        + 'storage system as the volume — often the same array, the same zone. It does not '
        + 'survive the loss of the storage holding it, because it is not anywhere else.',
      mitigation: 'Keep taking whatever real backups you take.',
    },
    {
      code: 'snapshot_is_crash_consistent',
      label: 'The data is captured as if the power were cut',
      consequence:
        'Nothing here freezes the filesystem or asks the application to flush. What is '
        + 'captured is what would be on disk at that instant after an abrupt stop.',
      mitigation: "For a database, take its own backup as well.",
    },
  ];

  if (resolved?.deletion_policy === 'Delete') {
    consequences.push({
      code: 'snapshot_delete_destroys_data',
      label: 'Deleting this snapshot later will destroy it in the storage system',
      consequence:
        `The class ${resolved.name} sets deletionPolicy: Delete, so removing this `
        + 'VolumeSnapshot object removes the underlying snapshot too.',
      mitigation: 'If it needs to outlive routine cleanup, use a class with deletionPolicy: Retain.',
    });
  } else if (resolved == null || resolved.deletion_policy == null) {
    consequences.push({
      code: 'snapshot_deletion_policy_unknown',
      label: 'What deleting this snapshot will do is unknown',
      consequence:
        (resolved?.detail ?? 'The VolumeSnapshotClass listing did not answer.')
        + ' Deleting the object later may or may not destroy the data.',
      mitigation: 'Read the VolumeSnapshotClass before relying on this snapshot.',
    });
  }

  if (target.status !== 'Bound') {
    consequences.push({
      code: 'snapshot_claim_not_bound',
      label: `This claim is ${target.status}, so there may be nothing to capture`,
      consequence:
        'A claim that is not Bound has no volume behind it yet. The snapshot object will be '
        + 'created and the controller will most likely leave it unready.',
      mitigation: 'Wait for the claim to bind, then check that readyToUse becomes true.',
    });
  }

  return {
    namespace: target.namespace,
    claim: {
      name: target.name,
      namespace: target.namespace,
      phase: target.status,
      volume: target.volume,
      storage_class: target.storage_class,
      capacity: target.status === 'Bound' ? '50Gi' : null,
    },
    requested: { name: body.name, snapshotClass: body.snapshotClass ?? null },
    snapshotClass: resolved ?? {
      name: null, deletion_policy: null, driver: null, is_default: null,
      reason: 'forbidden',
      detail: 'The VolumeSnapshotClass listing did not answer (forbidden).',
    },
    consequences,
    gate: { enabled: true, detail: 'This deployment permits taking a volume snapshot.' },
    partial: resolved == null,
    unavailable: resolved == null
      ? [{
          headline: 'Snapshot classes could not be listed',
          group: 'snapshot.storage.k8s.io', resource: 'volumesnapshotclasses',
          namespace: null, reason: 'forbidden', detail: null,
        }]
      : [],
  };
}

/** §22's write response: the §1.5 envelope plus the three keys §22 adds. */
export function snapshotWriteFor(body, options = {}) {
  const plan = snapshotPlanFor(body, options);
  return {
    dryRun: body.dryRun !== false,
    // Derived, never echoed from the request: §1.5 makes `applied` the only
    // evidence a cluster changed — and here it attests an *object*, not a
    // snapshot, which the dialog has to say out loud.
    applied: body.dryRun === false,
    verb: 'create',
    target: {
      group: 'snapshot.storage.k8s.io', version: 'v1', resource: 'volumesnapshots',
      namespace: plan.namespace, name: body.name,
    },
    diff: {
      unified:
        '--- live\n+++ projected\n@@\n+apiVersion: snapshot.storage.k8s.io/v1\n'
        + `+kind: VolumeSnapshot\n+metadata:\n+  name: ${body.name}\n`
        + `+spec:\n+  source:\n+    persistentVolumeClaimName: ${plan.claim.name}\n`,
      digest: 'sha256:snap',
    },
    warnings: [],
    consequences: plan.consequences,
    claim: plan.claim,
    snapshotClass: plan.snapshotClass,
  };
}

export function expandPlanFor(body, { claim = null, mountedBy = [], expansion = null } = {}) {
  const target = claim ?? FIXTURES.claims.items[0];
  const bytes = parseQuantity(body.size);
  const support = expansion ?? {
    supported: true,
    storage_class: target.storage_class,
    reason: 'allowed',
    detail: `StorageClass ${target.storage_class} sets allowVolumeExpansion: true.`,
  };
  const current = {
    namespace: target.namespace,
    name: target.name,
    phase: target.status,
    volume: target.volume,
    storage_class: target.storage_class,
    requested: target.requested,
    requested_bytes: target.requested_bytes,
    capacity: target.capacity_bytes == null ? null : `${target.capacity_bytes / 1024 ** 3}Gi`,
    capacity_bytes: target.capacity_bytes,
    conditions: [],
  };

  let blocked = null;
  if (bytes == null) {
    blocked = { message: `'${body.size}' is not a Kubernetes quantity.`, hint: 'Use 20Gi, 500M or 1Ti.', context: {} };
  } else if (target.status !== 'Bound') {
    blocked = {
      message: `This claim is ${target.status}, so there is no volume to expand.`,
      hint: 'An unbound claim has not been provisioned yet.',
      context: { phase: target.status },
    };
  } else if (bytes < target.requested_bytes) {
    blocked = {
      message: 'A persistent volume claim cannot be shrunk.',
      hint: `This claim requests ${target.requested}. Ask for more than that, or leave it alone.`,
      context: { currentRequested: target.requested },
    };
  } else if (bytes === target.requested_bytes) {
    blocked = {
      message: `This claim already requests ${target.requested}.`,
      hint: 'Ask for a larger size, or nothing needs to change.',
      context: { currentRequested: target.requested },
    };
  } else if (support.supported === false) {
    blocked = {
      message: `StorageClass ${support.storage_class} does not allow volume expansion.`,
      hint: 'An administrator can set allowVolumeExpansion: true on the StorageClass.',
      context: { storageClass: support.storage_class },
    };
  }

  return {
    namespace: target.namespace,
    name: target.name,
    current,
    requested: { size: body.size, size_bytes: bytes },
    expansion: support,
    mountedBy,
    resourceVersion: '7710',
    blocked,
    consequences: blocked ? [] : expandConsequences(body.size, current, support, mountedBy),
    gate: { feature: 'expanding a persistent volume claim', enabled: true, detail: null, switches: [] },
    partial: mountedBy === null,
    unavailable: mountedBy === null
      ? [{ headline: 'Pods could not be listed', group: '', resource: 'pods',
           namespace: target.namespace, reason: 'forbidden', detail: null }]
      : [],
  };
}

/** The two codes on every expansion, plus the situational ones. */
function expandConsequences(size, current, support, mountedBy) {
  const out = [
    {
      code: 'pvc_capacity_is_not_immediate',
      label: 'This changes the request, not the space a workload has',
      consequence: `Writing this asks for ${size}. status.capacity is what a workload actually has, and this write does not change it.`,
      mitigation: 'Watch the claim\u2019s capacity and its conditions afterwards.',
    },
    {
      code: 'pvc_expansion_is_one_way',
      label: 'Expansion cannot be undone',
      consequence: `No Kubernetes API makes a bound claim smaller again. Going from ${current.requested} to ${size} is permanent.`,
      mitigation: 'The cheap mistake is a second expansion later, not a large first one.',
    },
  ];
  if (support.supported === null) {
    out.push({
      code: 'pvc_expansion_unknown',
      label: 'Whether this volume can grow at all is unknown',
      consequence: `${support.detail} The write will be sent and the API server will decide.`,
      mitigation: 'Preview first.',
    });
  }
  if (mountedBy === null) {
    out.push({
      code: 'pvc_mounts_unknown',
      label: 'Which pods have this mounted could not be read',
      consequence: 'The pod listing did not answer. It is not saying nothing has the volume open.',
      mitigation: 'Check with kubectl before expanding.',
    });
  } else if (mountedBy.length) {
    out.push({
      code: 'pvc_in_use_offline_resize',
      label: `${mountedBy.length} pod${mountedBy.length === 1 ? '' : 's'} have this volume mounted`,
      consequence: `Many CSI drivers cannot grow a filesystem while it is mounted. The claim will report FileSystemResizePending until every pod using it restarts \u2014 ${mountedBy.join(', ')}.`,
      mitigation: 'Plan the restart with the expansion.',
    });
  }
  return out;
}

/** §20's write response: the §1.5 envelope plus the four keys §20 adds. */
export function expandFor(body, options = {}) {
  const plan = expandPlanFor(body, options);
  const applied = body.dryRun === false;
  return {
    dryRun: body.dryRun !== false,
    // Derived, never echoed: §1.5 makes `applied` the only evidence a cluster
    // changed, and here it attests the *request* and nothing about the volume.
    applied,
    verb: 'patch',
    target: {
      group: '', version: 'v1', resource: 'persistentvolumeclaims',
      namespace: plan.namespace, name: plan.name,
    },
    diff: {
      unified:
        `--- live\n+++ projected\n@@\n-      storage: ${plan.current.requested}\n+      storage: ${body.size}\n`,
      digest: 'sha256:abcd',
    },
    object: null,
    resourceVersion: '7711',
    warnings: [],
    consequences: plan.consequences,
    current: plan.current,
    requested: plan.requested,
    expansion: plan.expansion,
    mountedBy: plan.mountedBy,
  };
}

/** `50Gi` to bytes. Only the suffixes the fixtures use, deliberately. */
function parseQuantity(text) {
  const match = /^(\d+(?:\.\d+)?)(Gi|Mi|Ti|G|M|T)?$/.exec(String(text ?? '').trim());
  if (!match) return null;
  const units = { Gi: 1024 ** 3, Mi: 1024 ** 2, Ti: 1024 ** 4, G: 1e9, M: 1e6, T: 1e12 };
  return Number(match[1]) * (match[2] ? units[match[2]] : 1);
}

export function podSecurityPlanFor(body, { current = null } = {}) {
  const now = current ?? currentPodSecurity();
  const asked = body.podSecurity ?? {};
  const order = ['privileged', 'baseline', 'restricted'];
  const consequences = [];

  if (asked.enforce && asked.enforce !== now.enforce) {
    consequences.push({
      code: 'psa_does_not_evict',
      label: 'Pods already running are not affected by this',
      consequence:
        'Pod Security admission runs when a pod is created, so nothing in this namespace stops, ' +
        `restarts or is evicted by enforcing ${asked.enforce}. A workload whose pods violate it ` +
        'keeps the pods it has and fails to make new ones — FailedCreate, and no pod to inspect.',
      mitigation: 'Read the admission warnings on the preview: they name the pods that violate it today.',
    });
  }
  if (now.enforce && !asked.enforce) {
    consequences.push({
      code: 'psa_enforcement_removed',
      label: 'The enforce label is removed, not set to privileged',
      consequence:
        `This namespace enforces ${now.enforce} today. Removing the label does not mean everything ` +
        'is admitted: the cluster default applies, and this console cannot tell you what it is.',
      mitigation: 'Set privileged explicitly if that is what you mean.',
    });
  }
  if (
    now.enforce && asked.enforce &&
    order.indexOf(asked.enforce) >= 0 && order.indexOf(now.enforce) >= 0 &&
    order.indexOf(asked.enforce) < order.indexOf(now.enforce)
  ) {
    consequences.push({
      code: 'psa_lowered',
      label: 'Pods this namespace refuses today will be admitted',
      consequence: `The enforce level goes from ${now.enforce} down to ${asked.enforce}.`,
      mitigation: `Set audit and warn to ${now.enforce} so what would have been refused is still reported.`,
    });
  }
  if (['baseline', 'restricted'].includes(asked.enforce) && asked.warn !== asked.enforce) {
    consequences.push({
      code: 'psa_no_warn_label',
      label: 'Violations will be refused without being reported',
      consequence: `With enforce at ${asked.enforce} and warn set to ${asked.warn || 'nothing'}, a ` +
        'workload that violates the level is refused at pod creation and nobody is told when they apply it.',
      mitigation: `Set warn to ${asked.enforce} as well, which is what \`oc\` does.`,
    });
  }

  const changed = ['enforce', 'audit', 'warn'].some(
    (mode) => (now[mode] ?? null) !== (asked[mode] ?? null)
      || (now[`${mode}Version`] ?? null) !== (asked[`${mode}Version`] ?? null),
  );

  return {
    namespace: 'prod',
    current: now,
    requested: asked,
    resourceVersion: '4021',
    changed,
    consequences,
    gate: { enabled: true, detail: 'This deployment permits setting a Pod Security level.' },
  };
}

/**
 * §18 `PUT /projects/{name}/pod-security`.
 *
 * `admissionWarnings` is what Pod Security admission returns when the level is
 * raised over pods that do not meet it — the API server's own text, which the
 * console must render verbatim. Returned on the dry run and on the write, the
 * way the API server returns it on both.
 */
export const PSA_ADMISSION_WARNINGS = [
  'existing pods in namespace "prod" violate the new PodSecurity enforce level "restricted:latest"',
  'payments-api-7f9c: allowPrivilegeEscalation != false, unrestricted capabilities, runAsNonRoot != true, seccompProfile',
  'legacy-batch-2xk: host namespaces, hostPath volumes',
];

export function podSecuritySetFor(body, { warnings = PSA_ADMISSION_WARNINGS, current = null } = {}) {
  const dryRun = body.dryRun !== false;
  const plan = podSecurityPlanFor(body, { current });
  const asked = body.podSecurity ?? {};
  const before = plan.current;
  const line = (mode, side, value) => `${side}    pod-security.kubernetes.io/${mode}: ${value}`;
  const unified =
    '--- live\n+++ proposed\n@@ -1,6 +1,6 @@\n' +
    ['enforce', 'audit', 'warn']
      .flatMap((mode) => {
        const was = before[mode];
        const now = asked[mode] ?? null;
        if (was === now) return [];
        return [was ? line(mode, '-', was) : null, now ? line(mode, '+', now) : null].filter(Boolean);
      })
      .join('\n') + '\n';

  return {
    dryRun,
    applied: !dryRun,
    verb: 'patch',
    target: { group: '', version: 'v1', resource: 'namespaces', namespace: null, name: 'prod' },
    diff: { before: null, after: null, unified, changed: true },
    resourceVersion: '4022',
    warnings,
    admissionWarnings: warnings,
    consequences: plan.consequences,
    current: plan.current,
    requested: asked,
    auditId: dryRun ? null : 9401,
  };
}

/* ── §29 quota advice ───────────────────────────────────────────────────── */

/**
 * A namespace whose quota bounds `requests.cpu` and whose LimitRange defaults
 * nothing — the trap §29 exists for. `requests.memory` is bounded *and*
 * defaulted, so the two cases sit side by side and a page that conflated them
 * would show it.
 */
export const QUOTA_ADVICE = {
  namespace: 'prod',
  quotas: [
    {
      name: 'team', namespace: 'prod', scopes: [], scoped: false, applies: true,
      resources: [
        { resource: 'pods', hard: '50', used: '12', hard_value: '50', used_value: '12',
          exhausted: false },
        { resource: 'requests.cpu', hard: '10', used: '9', hard_value: '10',
          used_value: '9', exhausted: false },
        { resource: 'requests.memory', hard: '20Gi', used: '4Gi',
          hard_value: '21474836480', used_value: '4294967296', exhausted: false },
      ],
      headroom: { pods: '38', 'requests.cpu': '1', 'requests.memory': '17179869184' },
      findings: [],
      age_seconds: 864000,
    },
  ],
  limitRanges: [
    { name: 'defaults', namespace: 'prod', age_seconds: 864000, limits: [
      { type: 'Container', max: {}, min: {}, default: {},
        defaultRequest: { memory: '256Mi' }, maxLimitRequestRatio: {} },
    ] },
  ],
  containerDefaults: { 'requests.memory': '256Mi' },
  mandatory: ['requests.cpu', 'requests.memory'],
  findings: [{
    code: 'quota_requires_unset_resource',
    label: '1 resource(s) every pod must state, with no default to supply them',
    detail:
      'A quota that bounds a compute resource makes it mandatory: requests.cpu must be set on every container, or the pod is refused with "must specify …" — even when the quota is barely used. No LimitRange in this namespace supplies a default for them.',
    resources: ['requests.cpu'],
    quotas: ['team'],
  }],
  unavailable: [],
  partial: false,
};

/**
 * §29 `POST /quota/{ns}/preview`, derived from the body the way the backend is.
 *
 * `verdict` is computed, never echoed: it is the one field a mock that returned
 * what it was handed would let a page ignore entirely.
 */
export function quotaPreviewFor(body, advice = QUOTA_ADVICE) {
  const replicas = Number(body.replicas ?? 1);
  const containers = body.containers ?? [];
  const defaults = advice.containerDefaults ?? {};
  const mandatory = advice.mandatory ?? [];

  const unset = [];
  const perPod = {};
  for (const [index, c] of containers.entries()) {
    for (const key of mandatory) {
      const [kind, resource] = [key.split('.')[0], key.split('.').slice(1).join('.')];
      const declared = (c[kind] ?? {})[resource];
      const value = declared ?? defaults[key];
      if (value === undefined || value === '') {
        unset.push({ container: c.name ?? `container[${index}]`, resource: key });
        continue;
      }
      const n = String(value).endsWith('m')
        ? Number(String(value).slice(0, -1)) / 1000
        : String(value).endsWith('Mi')
          ? Number(String(value).slice(0, -2)) * 1024 * 1024
          : String(value).endsWith('Gi')
            ? Number(String(value).slice(0, -2)) * 1024 * 1024 * 1024
            : Number(value);
      perPod[key] = (perPod[key] ?? 0) + n;
    }
  }

  const checks = [];
  for (const quota of advice.quotas ?? []) {
    for (const entry of quota.resources) {
      const key = entry.resource;
      const want = key === 'pods' ? replicas : (perPod[key] ?? 0) * replicas;
      const room = quota.headroom?.[key];
      const verdict =
        quota.applies === null || room === null || room === undefined
          ? 'unknown'
          : want <= Number(room) ? 'admitted' : 'refused';
      checks.push({ quota: quota.name, resource: key, needed: String(want),
                    headroom: room ?? null, verdict });
    }
  }

  const verdicts = checks.map((c) => c.verdict);
  if (unset.length) verdicts.push('refused');
  const verdict = verdicts.includes('refused')
    ? 'refused'
    : verdicts.includes('unknown') ? 'unknown' : 'admitted';

  return {
    ...advice,
    preview: {
      replicas,
      needed: Object.fromEntries(
        Object.entries(perPod).map(([k, v]) => [k, String(v * replicas)]),
      ),
      unsetMandatory: unset,
      checks,
      verdict,
    },
  };
}

/* ── §28 disruption budgets ─────────────────────────────────────────────── */

/**
 * Four budgets, one per failure this page exists to surface, plus one that is
 * simply fine — because a page that flags everything is one nobody reads.
 *
 *   api / api-extra  both healthy-looking, both covering `api-0`. Kubernetes
 *                    refuses that pod's eviction outright. This is the pair the
 *                    overlap banner is about.
 *   ghost            covers nothing. `selected_pods: 0` is the finding.
 *   frozen           `maxUnavailable: 0` — can never allow an eviction, ever.
 *   unread           the pod listing could not be evaluated for it, so
 *                    `selected_pods` is null and must render as an em dash.
 *   web              ordinary and quiet.
 */
export const DISRUPTION_BUDGETS = {
  items: [
    {
      name: 'api', namespace: 'prod', min_available: 1, max_unavailable: null,
      selector: { matchLabels: { app: 'api' } },
      unhealthy_pod_eviction_policy: 'IfHealthyBudget',
      disruptions_allowed: 1, current_healthy: 2, desired_healthy: 1,
      expected_pods: 2, disrupted_pods: [], status_stale: false,
      selected_pods: 2, undecidable_pods: null, age_seconds: 86400,
      findings: [{
        code: 'pdb_overlaps',
        label: 'A pod here is covered by more than one budget',
        detail: 'These pods are also covered by prod/api-extra. Kubernetes does not support overlapping budgets.',
      }],
    },
    {
      name: 'api-extra', namespace: 'prod', min_available: null, max_unavailable: 1,
      selector: { matchLabels: { app: 'api' } },
      unhealthy_pod_eviction_policy: 'IfHealthyBudget',
      disruptions_allowed: 1, current_healthy: 2, desired_healthy: 1,
      expected_pods: 2, disrupted_pods: [], status_stale: false,
      selected_pods: 2, undecidable_pods: null, age_seconds: 3600,
      findings: [{
        code: 'pdb_overlaps',
        label: 'A pod here is covered by more than one budget',
        detail: 'These pods are also covered by prod/api. Kubernetes does not support overlapping budgets.',
      }],
    },
    {
      name: 'frozen', namespace: 'prod', min_available: null, max_unavailable: 0,
      selector: { matchLabels: { app: 'payments' } },
      unhealthy_pod_eviction_policy: 'IfHealthyBudget',
      disruptions_allowed: 0, current_healthy: 3, desired_healthy: 3,
      expected_pods: 3, disrupted_pods: [], status_stale: false,
      selected_pods: 3, undecidable_pods: null, age_seconds: 604800,
      findings: [{
        code: 'pdb_never_allows_disruption',
        label: 'No eviction can ever be permitted',
        detail: 'maxUnavailable is 0, so no pod covered by this budget may ever be voluntarily evicted — at any replica count.',
      }],
    },
    {
      name: 'ghost', namespace: 'prod', min_available: 2, max_unavailable: null,
      selector: { matchLabels: { app: 'renamed' } },
      unhealthy_pod_eviction_policy: 'IfHealthyBudget',
      disruptions_allowed: null, current_healthy: null, desired_healthy: null,
      expected_pods: 0, disrupted_pods: [], status_stale: null,
      selected_pods: 0, undecidable_pods: null, age_seconds: 259200,
      findings: [{
        code: 'pdb_selects_nothing',
        label: 'This budget covers no pods',
        detail: 'Its selector matches nothing in this namespace, so it constrains no eviction.',
      }],
    },
    {
      name: 'unread', namespace: 'prod', min_available: 1, max_unavailable: null,
      selector: { matchExpressions: [{ key: 'app', operator: 'Wibble', values: ['x'] }] },
      unhealthy_pod_eviction_policy: 'AlwaysAllow',
      disruptions_allowed: 1, current_healthy: 2, desired_healthy: 1,
      expected_pods: 2, disrupted_pods: [], status_stale: false,
      selected_pods: null, undecidable_pods: 3, age_seconds: 7200,
      findings: [{
        code: 'pdb_selection_unknown',
        label: 'Which pods this covers could not be decided',
        detail: '3 pod(s) could not be evaluated against this selector.',
      }],
    },
    {
      name: 'web', namespace: 'prod', min_available: 1, max_unavailable: null,
      selector: { matchLabels: { app: 'web' } },
      unhealthy_pod_eviction_policy: 'IfHealthyBudget',
      disruptions_allowed: 2, current_healthy: 3, desired_healthy: 1,
      expected_pods: 3, disrupted_pods: [], status_stale: false,
      selected_pods: 3, undecidable_pods: null, age_seconds: 1209600,
      findings: [],
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
  overlappingPods: [
    { pod: 'prod/api-0', budgets: ['prod/api', 'prod/api-extra'] },
  ],
};

/* ── §26 the deletion blast radius ──────────────────────────────────────── */

/**
 * The namespace §26 exists for: a database on a `Delete`-policy volume, a
 * public address, and an admission webhook backed from inside it.
 *
 * Every tri-state this feature has is represented here rather than in a spec,
 * so a spec that overrides one is overriding a shape the default already
 * exercises:
 *
 *   `count: null` on `secrets`   the listing was refused, which is not zero.
 *   `reclaim_policy: null`       on `analytics-scratch`, whose volume could not
 *                                be read — unknown, not safe.
 *   `webhooks: []` vs `null`     `[]` here; `namespaceDeleteWebhooksUnknown`
 *                                turns it into the `null` case.
 */
export const NAMESPACE_DELETE_PLAN = {
  name: 'prod',
  phase: 'Active',
  resourceVersion: '4210',
  deletionTimestamp: null,
  namespaceFinalizers: [],
  inventory: {
    kinds: [
      { group: '', version: 'v1', resource: 'pods', kind: 'Pod', count: 14, truncated: false },
      {
        group: 'apps', version: 'v1', resource: 'deployments', kind: 'Deployment',
        count: 6, truncated: false,
      },
      {
        group: '', version: 'v1', resource: 'persistentvolumeclaims',
        kind: 'PersistentVolumeClaim', count: 2, truncated: false,
      },
      { group: '', version: 'v1', resource: 'services', kind: 'Service', count: 3, truncated: false },
      {
        group: '', version: 'v1', resource: 'configmaps', kind: 'ConfigMap',
        count: 0, truncated: false,
      },
      // The refused listing. Rendered as an em dash and never as 0.
      { group: '', version: 'v1', resource: 'secrets', kind: 'Secret', count: null, truncated: null },
    ],
  },
  volumes: [
    {
      claim: 'postgres-data', volume: 'pv-9c2f', phase: 'Bound', capacity: '200Gi',
      storage_class: 'gp3', reclaim_policy: 'Delete', reason: null,
    },
    {
      claim: 'analytics-scratch', volume: 'pv-77aa', phase: 'Bound', capacity: '1Ti',
      storage_class: 'gp3', reclaim_policy: null,
      reason:
        'The volume behind this claim could not be read, so whether its data is destroyed or kept is unknown.',
    },
  ],
  load_balancers: [{ name: 'edge', addresses: ['203.0.113.9'] }],
  webhooks: [
    {
      configuration: 'prod-policy', kind: 'ValidatingWebhookConfiguration',
      webhook: 'policy.example.com', service: 'admission', failure_policy: 'Fail',
    },
  ],
  finalizers: [
    { resource: '/persistentvolumeclaims', name: 'postgres-data', finalizers: ['kubernetes.io/pvc-protection'] },
  ],
  propagationPolicy: 'Background',
  blocked: null,
  consequences: [
    {
      code: 'namespace_destroys_volume_data',
      label: '1 volume will be destroyed, not released',
      consequence:
        'postgres-data binds a volume whose reclaim policy is Delete, so the storage provider deletes the underlying disk when the claim goes. The data is gone.',
      mitigation: 'Take a snapshot first, or set the PersistentVolume’s reclaim policy to Retain.',
    },
    {
      code: 'namespace_volume_fate_unknown',
      label: 'Whether 1 volume survives this is unknown',
      consequence:
        'analytics-scratch is bound to a volume this console could not read, so it cannot tell you whether its data is destroyed or kept. It is not reporting that they are safe.',
      mitigation: 'Read the PersistentVolumes yourself — the reclaim policy is on the volume, not the claim.',
    },
    {
      code: 'namespace_drops_load_balancer',
      label: '1 load balancer and their addresses go with it',
      consequence:
        'edge currently answers on 203.0.113.9. Recreating the Service later gets a different one, and whatever points at the old address keeps pointing at nothing.',
      mitigation: 'Check what resolves to these addresses before deleting.',
    },
    {
      code: 'namespace_breaks_admission_webhook',
      label: '1 admission webhook is served from this namespace, 1 of them failing closed',
      consequence:
        'prod-policy points at a Service in this namespace. The configurations are cluster-scoped and survive the delete; the Service does not. With failurePolicy: Fail the API server then refuses every write those webhooks intercept, across the whole cluster.',
      mitigation: 'Delete the webhook configuration before the namespace, not after.',
    },
    {
      code: 'namespace_inventory_incomplete',
      label: 'This inventory is not complete',
      consequence:
        '1 kind(s) were refused or held more objects than the 200 this console reads per kind: secrets. Everything above is what was found in what was read, not what is in the namespace.',
      mitigation: 'Treat the findings as a floor rather than a total.',
    },
  ],
  unavailable: [
    {
      group: '', resource: 'secrets', namespace: 'prod', error: 'rbac_denied',
      reason: 'forbidden', message: 'secrets is forbidden in prod.',
      hint: 'Grant `list secrets` in prod.',
    },
  ],
  partial: true,
  gate: { enabled: true, detail: 'This deployment permits deleting a namespace.' },
};

/**
 * §26 `GET /projects/{name}/delete-plan`.
 *
 * `overrides` is shallow-merged, so a spec asking for the unknown-webhook case
 * writes `{ webhooks: null }` and inherits everything else.
 */
export function namespaceDeletePlanFor(name, overrides = {}) {
  return { ...NAMESPACE_DELETE_PLAN, name, ...overrides };
}

/**
 * §26 `DELETE /projects/{name}`.
 *
 * `applied` is derived from `dryRun`, never echoed: §1.5 makes it the only
 * evidence a cluster changed, and a mock that returned what it was handed would
 * let a dialog reporting a dry run as a deletion pass.
 *
 * A body naming fewer codes than the plan carries comes back as the backend's
 * 422, because the acknowledgement is recomputed server-side and the dialog's
 * whole job is to have collected them.
 */
export function namespaceDeleteFor(name, body, overrides = {}) {
  const plan = namespaceDeletePlanFor(name, overrides);
  const acknowledged = body.acknowledgeConsequences ?? [];
  const missing = plan.consequences.filter((entry) => !acknowledged.includes(entry.code));
  if (missing.length) {
    return {
      status: 422,
      payload: {
        error: 'invalid',
        message: 'This deletion has consequences that have not been acknowledged.',
        detail: missing.map((entry) => `${entry.code}: ${entry.label}`).join('; '),
        hint: `Re-send with acknowledgeConsequences naming each of ${missing
          .map((entry) => entry.code)
          .join(', ')}.`,
        context: {
          parameter: 'acknowledgeConsequences',
          unacknowledged: missing.map((entry) => entry.code),
        },
      },
    };
  }
  return {
    status: 200,
    payload: {
      dryRun: body.dryRun !== false,
      applied: body.dryRun === false,
      verb: 'delete',
      target: { group: '', version: 'v1', resource: 'namespaces', name },
      diff: {
        before: `apiVersion: v1\nkind: Namespace\nmetadata:\n  name: ${name}\n`,
        after: '',
        unified: `--- live\n+++ projected\n@@\n-apiVersion: v1\n-kind: Namespace\n-metadata:\n-  name: ${name}\n`,
        digest: 'sha256:namespace-delete',
        changed: true,
      },
      resourceVersion: plan.resourceVersion,
      warnings: [],
      auditId: body.dryRun === false ? 9600 : null,
      consequences: plan.consequences,
      plan,
    },
  };
}

/** §17 `POST /projects/plan`. `exists` is true for the one namespace the list fixture already has. */
export function projectPlanFor(body) {
  const exists = FIXTURES.namespaces.items.some((row) => row.name === body.name);
  return {
    name: body.name,
    target: {
      exists,
      phase: exists ? 'Active' : null,
      detail: exists ? `${body.name} already exists; the create will be refused.` : `${body.name} does not exist and can be created.`,
    },
    objects: projectObjectsFor(body),
    consequences: projectConsequencesFor(body),
    enabled: true,
    enabledDetail: 'This deployment permits creating projects.',
    partial: false,
    unavailable: [],
  };
}

/**
 * §17 `POST /projects`. On a dry run the Namespace is projected and the rest
 * rendered, as the backend does; on a real write everything is server-projected
 * and applied. `created` is derived, never echoed.
 */
export function projectCreateFor(body, { failKinds = [], preflightDenied = [] } = {}) {
  const dryRun = body.dryRun !== false;
  const objects = projectObjectsFor(body).map((object, index) => {
    const failed = !dryRun && failKinds.includes(object.kind);
    const rendered = dryRun && object.kind !== 'Namespace';
    return {
      kind: object.kind,
      name: object.name,
      namespace: object.namespace,
      group: object.group,
      resource: object.resource,
      verb: 'create',
      applied: !dryRun && !failed,
      diff: failed ? null : { before: null, after: object.yaml, unified: `--- live\n+++ proposed\n@@ -0,0 +1,3 @@\n+kind: ${object.kind}\n+metadata:\n+  name: ${object.name}\n`, changed: true },
      projection: failed ? null : rendered ? 'rendered' : 'server',
      preflight: rendered
        ? { allowed: !preflightDenied.includes(object.kind), reason: preflightDenied.includes(object.kind) ? 'no RBAC policy matched' : '', evaluationError: null,
            hint: preflightDenied.includes(object.kind) ? `Grant \`create\` on \`${object.group || 'core'}/${object.resource}\` in \`${object.namespace}\` to the console's ServiceAccount.` : null }
        : null,
      auditId: dryRun ? (object.kind === 'Namespace' ? 9101 : null) : failed ? null : 9101 + index,
      skipped: null,
      error: failed
        ? { code: 'rbac_denied', message: `${object.resource} is forbidden`, detail: null,
            hint: `Grant \`create\` on \`${object.group || 'core'}/${object.resource}\` in \`${object.namespace}\` to the console's ServiceAccount.` }
        : null,
    };
  });
  const failed = objects.filter((o) => o.error).length;
  return {
    dryRun,
    created: !dryRun && failed === 0,
    failed,
    skipped: 0,
    name: body.name,
    objects,
    consequences: projectConsequencesFor(body),
  };
}

/**
 * §14 install report objects, derived from the plan the way the backend does:
 * on a dry run into a namespace that does not exist, the cluster-scoped
 * objects are server-projected and the ones inside the namespace are rendered,
 * each with a preflight; on a real write every object is projected and applied.
 */
const ROUTER_BUNDLE = [
  { kind: 'Namespace', name: 'k8boss-router', namespace: null, group: '', resource: 'namespaces' },
  { kind: 'ClusterRole', name: 'k8boss-admin-router', namespace: null, group: 'rbac.authorization.k8s.io', resource: 'clusterroles' },
  { kind: 'ClusterRoleBinding', name: 'k8boss-admin-router', namespace: null, group: 'rbac.authorization.k8s.io', resource: 'clusterrolebindings' },
  { kind: 'IngressClass', name: 'haproxy', namespace: null, group: 'networking.k8s.io', resource: 'ingressclasses' },
  { kind: 'ServiceAccount', name: 'k8boss-admin-router', namespace: 'k8boss-router', group: '', resource: 'serviceaccounts' },
  { kind: 'ConfigMap', name: 'k8boss-admin-router', namespace: 'k8boss-router', group: '', resource: 'configmaps' },
  { kind: 'Deployment', name: 'k8boss-admin-router', namespace: 'k8boss-router', group: 'apps', resource: 'deployments' },
  { kind: 'Service', name: 'k8boss-admin-router', namespace: 'k8boss-router', group: '', resource: 'services' },
];

export function routerInstallObjectsFor(body, { preflightDenied = [] } = {}) {
  const dryRun = body.dryRun !== false;
  const namespace = body.namespace || 'k8boss-router';
  return ROUTER_BUNDLE.map((template, index) => {
    const object = {
      ...template,
      namespace: template.namespace == null ? null : namespace,
      yaml: `apiVersion: v1\nkind: ${template.kind}\nmetadata:\n  name: ${template.name}\n`,
    };
    const rendered = dryRun && object.namespace != null;
    const denied = preflightDenied.includes(object.kind);
    return {
      kind: object.kind,
      name: object.name,
      namespace: object.namespace ?? null,
      group: object.group ?? '',
      resource: object.resource,
      verb: 'create',
      applied: !dryRun,
      diff: { before: null, after: object.yaml, unified: `--- live\n+++ proposed\n@@ -0,0 +1,2 @@\n+kind: ${object.kind}\n+name: ${object.name}\n`, changed: true },
      projection: rendered ? 'rendered' : 'server',
      preflight: rendered
        ? { allowed: !denied, reason: denied ? 'no RBAC policy matched' : '', evaluationError: null,
            hint: denied ? `Grant \`create\` on \`${object.group || 'core'}/${object.resource}\` in \`${object.namespace}\` to the console's ServiceAccount.` : null }
        : null,
      auditId: rendered ? null : 7000 + index,
      error: null,
    };
  });
}

/**
 * §30's plan. The three states of `capability` are the point of the feature, so
 * the builder makes each of them reachable rather than deriving one.
 *
 * `rule_count` is `null` for every state but `present`, and never `0` — that is
 * the whole §0.1 corollary this endpoint exists to hold, and a fixture that
 * defaulted it to a number would let the frontend's rendering of the null pass
 * untested.
 */
export function grantPlanFor(body, {
  state = 'present',
  ruleCount = 1,
  powers = [],
  aggregates = false,
  binding = null,
  residual = undefined,
  blocked = null,
  consequences = null,
  resourceVersion = '7781',
  unavailable = [],
} = {}) {
  const revoke = body.operation === 'revoke';
  const capability = {
    state,
    rules: state === 'present' && !aggregates ? [{}] : null,
    rule_count: state === 'present' && !aggregates ? ruleCount : null,
    aggregates,
    powers,
  };
  const derived = [];
  if (powers.some((p) => p.code === 'grant_full_control')) {
    derived.push({
      code: 'grant_confers_full_control',
      label: `ClusterRole ${body.role.name} confers full control of this namespace`,
      consequence: 'Every verb on every resource here, including the Secrets and these bindings.',
      mitigation: 'Bind a narrower role.',
    });
  }
  if (powers.some((p) => p.code === 'grant_pod_exec')) {
    derived.push({
      code: 'grant_confers_secret_access',
      label: `ClusterRole ${body.role.name} exposes this namespace's Secrets`,
      consequence:
        'They will be able to read the Secrets in this namespace — through `pods/exec`, which reaches them even though the role has no rule about Secrets.',
      mitigation: 'Bind a role without `create pods/exec`.',
    });
  }
  if (state === 'unreadable') {
    derived.push({
      code: 'grant_role_unreadable',
      label: `ClusterRole ${body.role.name} could not be read`,
      consequence:
        'This console could not fetch the role, so what it confers is unknown. The binding will still be created.',
      mitigation: 'Grant this console `get` on the role.',
    });
  }
  const residualBlock =
    residual === undefined
      ? revoke
        ? { namespace_bindings: [], cluster_bindings: [], cluster_truncated: false }
        : null
      : residual;
  if (revoke && residualBlock) {
    const other = residualBlock.namespace_bindings ?? [];
    const cluster = residualBlock.cluster_bindings;
    if (other.length || (cluster ?? []).length) {
      derived.push({
        code: 'revoke_access_remains',
        label: `${body.subject.kind} ${body.subject.name} is still bound elsewhere`,
        consequence: 'This revoke removes one binding, not their access.',
        mitigation: 'Ask the API server with a subject review.',
      });
    }
    if (cluster === null) {
      derived.push({
        code: 'revoke_residual_unknown',
        label: 'Cluster-wide bindings could not be listed',
        consequence:
          'A ClusterRoleBinding grants everywhere, including here, so whether they keep access through one is unknown. This is not a report that none exists.',
        mitigation: 'Grant this console `list` on clusterrolebindings.',
      });
    }
  }

  return {
    namespace: 'prod',
    operation: body.operation,
    role: body.role,
    subject: { ...body.subject, apiGroup: 'rbac.authorization.k8s.io' },
    binding,
    createName: binding ? null : body.role.name,
    resourceVersion: binding ? resourceVersion : null,
    capability,
    currentSubjects: [],
    requestedSubjects: blocked ? [] : [{ ...body.subject, apiGroup: 'rbac.authorization.k8s.io' }],
    residual: residualBlock,
    blocked,
    consequences: blocked ? [] : (consequences ?? derived),
    unavailable,
    partial: unavailable.length > 0,
    gate: { enabled: true, detail: 'This deployment permits changing role bindings.' },
  };
}

/** §30's write. `applied` is true only when the request was not a dry run. */
export function grantWriteFor(body, options = {}) {
  const plan = grantPlanFor(body, options);
  return {
    dryRun: body.dryRun !== false,
    applied: body.dryRun === false,
    resourceVersion: '7782',
    diff:
      '--- live\n+++ projected\n@@ -1,3 +1,4 @@\n subjects:\n+- kind: User\n+  name: alice\n',
    warnings: [],
    operation: plan.operation,
    role: plan.role,
    subject: plan.subject,
    binding: plan.binding ?? { name: body.role.name, namespace: 'prod', role: body.role },
    capability: plan.capability,
    currentSubjects: plan.currentSubjects,
    requestedSubjects: plan.requestedSubjects,
    residual: plan.residual,
    consequences: plan.consequences,
    unavailable: plan.unavailable,
  };
}

export async function mockApi(
  page,
  {
    health = FIXTURES.health,
    auth = null,
    services = null,
    audit = null,
    chain = null,
    yaml = null,
    workloads = null,
    pods = null,
    networkPolicies = null,
    // §4's generic create, which nothing mocked before §11.9's form view needed
    // to assert on what it sends. `resourceCreates` records every body; pass
    // `resourceCreate` to shape the response, `catalog` to change what the
    // dialog can resolve.
    resourceCreates = [],
    resourceCreate = null,
    catalog = null,
    isolation = null,
    podDetail = null,
    podEnvironment = null,
    podMetrics = null,
    podScheduling = null,
    debug = null,
    debugAttach = null,
    preflight = null,
    // §30. `grantOptions` varies the plan the two routes derive; `grant`
    // replaces the plan outright for the shapes the derivation cannot produce.
    grant = null,
    grantOptions = null,
    nodeDetail = null,
    nodeDebug = null,
    // §24. `nodeSchedulingOptions` varies the node and its pods for the two
    // derivations above; the four overrides replace them outright for the
    // handful of specs that need a shape the derivation cannot produce.
    nodeSchedulingOptions = null,
    taintPlan = null,
    taintWrite = null,
    labelPlan = null,
    labelWrite = null,
    // Every §24 write the page made, in order. A spec asserting on the *body*
    // is how "the whole list is sent, not a delta" and "a removed label goes as
    // an explicit null" are checked from outside the component.
    nodeSchedulingWrites = [],
    // §25. `certificateRequests` replaces the listing; `csrPlan`/`csrDecide`
    // replace the derivations for the few specs that need a shape those cannot
    // produce; `csrDecisions` records every write the page made.
    certificateRequests = null,
    csrPlan = null,
    csrDecide = null,
    csrDecisions = [],
    nodeDebugCreate = null,
    nodeDebugDeletes = [],
    cli = null,
    cliCreate = null,
    cliDeletes = [],
    routeCapabilities = null,
    routes = null,
    routeCertificates = null,
    routeRender = null,
    routeWrite = null,
    routeDetail = null,
    routerStatus = null,
    routerInstall = null,
    portalCatalog = null,
    portalInstalled = null,
    portalPlan = null,
    portalSubscribe = null,
    olmStatus = null,
    olmPlan = null,
    olmInstall = null,
    project = null,
    projectPlan = null,
    projectCreate = null,
    podSecurityPlan = null,
    podSecuritySet = null,
    namespaceDeletePlan = null,
    namespaceDeleteOverrides = undefined,
    namespaceDeletes = [],
    clusters = null,
    clusterWrites = [],
    disruptionBudgets = null,
    quotaAdvice = null,
    quotaPreviews = [],
    clusterStatus = null,
    claims = null,
    expandPlan = null,
    expandWrite = null,
    expandOptions = undefined,
    snapshots = null,
    snapshotClasses = null,
    snapshotPlan = null,
    snapshotWrite = null,
    snapshotOptions = undefined,
    subjectReview = null,
    subjectReviewOptions = undefined,
    autoscalers = null,
    boundsPlan = null,
    boundsWrite = null,
    autoscalerOptions = undefined,
    scaleGovernedBy = null,
  } = {},
) {
  // Counted so a spec can hand back a different manifest on the second read —
  // which is how "the panel notices the object changed" is testable at all.
  let yamlReads = 0;
  let signedIn = Boolean(auth?.authenticated);
  const authUser = auth?.user ?? {
    id: 7,
    username: 'directory.admin',
    display_name: 'Directory Admin',
    email: 'directory.admin@example.test',
    role: 'admin',
    auth_source: 'ldap',
  };

  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, '');

    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (path === '/auth/config') {
      // `ssoProviders` is a list because the console offers four kinds of
      // single sign-on — OpenID Connect, generic OAuth 2.0, OpenShift and SAML
      // — each configured independently and each drawing its own button.
      // `ssoEnabled` in a test's options is shorthand for "OIDC is configured";
      // `ssoProviders` passes an explicit list.
      const providers = auth?.ssoProviders
        ?? (auth?.ssoEnabled
          ? [{ name: 'oidc', label: auth.ssoLabel || 'Single sign-on', startPath: '/api/auth/oidc/start' }]
          : []);
      return json(
        auth
          ? {
              enabled: true,
              localEnabled: true,
              ldapEnabled: Boolean(auth.ldapEnabled),
              oidcEnabled: providers.some((provider) => provider.name === 'oidc'),
              methods: [
                'local',
                ...(auth.ldapEnabled ? ['ldap'] : []),
                ...providers.map((provider) => provider.name),
              ],
              ssoProviders: providers,
              // The OIDC entry repeated, as the backend still serves it for an
              // already-loaded older build of the SPA.
              oidc: providers.find((provider) => provider.name === 'oidc') ?? null,
            }
          : FIXTURES.authConfig,
      );
    }
    if (path === '/auth/me') {
      return signedIn
        ? json({ enabled: true, authenticated: true, user: authUser, csrfToken: 'csrf-test', expiresAt: '2026-08-19T00:00:00Z' })
        : json({ error: 'authentication_required', message: 'Sign in to continue.', detail: null, hint: null, context: {} }, 401);
    }
    if (path === '/auth/login') {
      signedIn = true;
      return json({ enabled: true, authenticated: true, user: authUser, csrfToken: 'csrf-test', expiresAt: '2026-08-19T00:00:00Z' });
    }
    if (path === '/auth/logout') {
      signedIn = false;
      return route.fulfill({ status: 204, body: '' });
    }
    if (path === '/auth/users') return json(FIXTURES.users);
    if (path === '/audit/verify') return json(chain ?? FIXTURES.auditVerify);
    if (path === '/audit') return json(audit ?? FIXTURES.audit);
    if (path === '/health') return json(health);
    if (path === '/clusters' && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      clusterWrites.push(body);
      return json({ ...FIXTURES.clusters.items[0], ...body, id: 2 }, 201);
    }
    if (/^\/clusters\/\d+$/.test(path) && route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}');
      clusterWrites.push(body);
      return json({ ...FIXTURES.clusters.items[0], ...body });
    }
    // §29. Ordered before the plain advice route: the preview lives under it.
    if (/^\/quota\/[^/]+\/preview$/.test(path) && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      quotaPreviews.push(body);
      return json(quotaPreviewFor(body, quotaAdvice ?? QUOTA_ADVICE));
    }
    if (/^\/quota\/[^/]+$/.test(path)) {
      return json(quotaAdvice ?? QUOTA_ADVICE);
    }
    if (path === '/disruption/budgets') {
      return json(disruptionBudgets ?? DISRUPTION_BUDGETS);
    }
    if (path === '/clusters') return json(clusters ?? FIXTURES.clusters);
    if (/^\/clusters\/\d+\/overview$/.test(path)) return json(FIXTURES.overview);
    if (/^\/clusters\/\d+\/test$/.test(path)) {
      return json({ reachable: true, server_version: 'v1.31.4', latency_ms: 42, permissions: [] });
    }
    if (path === '/resources/catalog') return json(catalog ?? FIXTURES.catalog);
    // Before the listing branches below, and method-checked, because a create
    // POSTs to the same path a list GETs from: without this, `POST
    // /resources/core/v1/pods` matched the pod listing and the dialog rendered a
    // table of pods as its own dry run.
    if (/^\/resources\/[^/]+\/[^/]+\/[^/]+$/.test(path) && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      const [, , group, version, plural] = path.split('/');
      resourceCreates.push({ group, version, plural, body });
      if (resourceCreate) return json(resourceCreate(body, { group, version, plural }));
      return json({
        dryRun: body.dryRun !== false,
        // Derived, never echoed: §1.5 makes `applied` the only evidence a
        // cluster changed.
        applied: body.dryRun === false,
        verb: 'create',
        target: {
          group: group === 'core' ? '' : group,
          version,
          resource: plural,
          namespace: body.namespace ?? null,
          name: manifestName(body.yaml) ?? 'example',
        },
        diff: {
          // A create has nothing to diff against, which is why the whole
          // object is the diff.
          before: '',
          after: body.yaml ?? '',
          // With the hunk header `difflib.unified_diff` always emits, and
          // without the phantom `+` a trailing newline would add. `DiffView`
          // derives both line-number gutters from that header and prints none
          // at all when it is missing — so a mock without one exercises a
          // rendering path the real endpoint can never produce.
          unified: (() => {
            const lines = (body.yaml ?? '').split('\n');
            if (lines.at(-1) === '') lines.pop();
            return [
              '--- live',
              '+++ projected',
              `@@ -0,0 +1,${lines.length} @@`,
              ...lines.map((line) => `+${line}`),
            ].join('\n');
          })(),
          changed: true,
        },
        resourceVersion: '5001',
        warnings: [],
        auditId: 7300,
      });
    }
    // `text/plain`, not JSON: §4's YAML read is the one endpoint in the API that
    // does not answer with an envelope, and a mock that returned JSON here
    // would let a client-side parse bug through.
    if (path.endsWith('/yaml')) {
      yamlReads += 1;
      const body =
        typeof yaml === 'function'
          ? yaml(yamlReads, new URL(route.request().url()))
          : yaml ?? objectYaml({ name: decodeURIComponent(path.split('/').at(-2) ?? 'object') });
      return route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body });
    }
    // §7.4. POST attaches, GET lists. Ordered before the generic listing
    // branch because both live under /pods/…, and a router that matched the
    // listing first would answer an attach with a pod table.
    if (/^\/pods\/[^/]+\/[^/]+\/debug$/.test(path)) {
      if (route.request().method() === 'POST') {
        const body = JSON.parse(route.request().postData() || '{}');
        return json(
          debugAttach
            ? typeof debugAttach === 'function'
              ? debugAttach(body)
              : debugAttach
            : {
                dryRun: body.dryRun !== false,
                // Derived, never echoed from the request: §1.5 makes `applied`
                // the only evidence a cluster changed, and a mock that reported
                // a dry run as applied would let a UI bug through that the real
                // backend structurally cannot have.
                applied: body.dryRun === false,
                verb: 'patch',
                target: {
                  group: '',
                  version: 'v1',
                  resource: 'pods',
                  namespace: 'prod',
                  name: 'checkout-7d9f8b6c4-hk2xv',
                  subresource: 'ephemeralcontainers',
                },
                diff: {
                  before: 'spec: {}\n',
                  after: `spec:\n  ephemeralContainers:\n    - name: debugger-x4k2p\n      image: ${body.image || 'busybox:1.36'}\n`,
                  unified:
                    '--- live\n+++ projected\n@@ -1,1 +1,4 @@\n spec: {}\n' +
                    `+  ephemeralContainers:\n+    - name: debugger-x4k2p\n+      image: ${body.image || 'busybox:1.36'}\n`,
                  changed: true,
                },
                resourceVersion: '884214',
                warnings: [],
                auditId: 4021,
                container: body.container || 'debugger-x4k2p',
                image: body.image || 'busybox:1.36',
                targetContainer: body.targetContainer ?? null,
                command: body.command ?? null,
                tty: body.tty !== false,
              },
        );
      }
      return json(debug ?? FIXTURES.debugSupported);
    }
    // §7.6 and §7.7, before the §7.5 detail below: all three live under
    // /pods/{ns}/{name}, and a router that matched the detail first would
    // answer an environment read with a pod.
    if (/^\/pods\/[^/]+\/[^/]+\/environment$/.test(path)) {
      return json(podEnvironment ?? FIXTURES.podEnvironment);
    }
    if (/^\/pods\/[^/]+\/[^/]+\/metrics$/.test(path)) {
      return json(podMetrics ?? FIXTURES.podMetrics);
    }
    if (/^\/pods\/[^/]+\/[^/]+\/scheduling$/.test(path)) {
      return json(podScheduling ?? FIXTURES.podScheduling);
    }
    if (/^\/pods\/[^/]+\/[^/]+$/.test(path)) {
      const name = decodeURIComponent(path.split('/').at(-1));
      const base = podDetail ?? FIXTURES.podDetail;
      // The name from the URL, so a spec that navigates to a second pod does
      // not get the first one's detail under the second one's heading.
      return json({ ...base, name });
    }
    if (path === '/resources/core/v1/pods') return json(pods ?? FIXTURES.pods);
    if (path === '/resources/networking.k8s.io/v1/networkpolicies') {
      return json(networkPolicies ?? FIXTURES.networkPolicies);
    }
    if (path === '/network/isolation') return json(isolation ?? FIXTURES.isolation);
    // §19. One GET, five sections, each separately nullable — a spec that wants
    // "we could not read the webhooks" passes `{ webhooks: null, unavailable:
    // [...], partial: true }` rather than an error, because that is what the
    // endpoint does: a refused listing costs its section and leaves the
    // response at 200.
    if (path === '/cluster-status') return json(clusterStatus ?? FIXTURES.clusterStatus);
    if (path === '/resources/core/v1/services') return json(services ?? FIXTURES.services);
    if (path === '/resources/autoscaling/v2/horizontalpodautoscalers') {
      return json(autoscalers ?? FIXTURES.autoscalers);
    }
    // §21. Like §20's, the plan answers 200 even when nothing would change —
    // `blocked` is set instead — because that is the endpoint's contract, and a
    // fixture that returned 422 there would let a dialog that hides the
    // autoscaler behind an error panel pass.
    if (/^\/autoscaling\/hpas\/[^/]+\/[^/]+\/bounds\/plan$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(boundsPlan ? boundsPlan(body) : boundsPlanFor(body, autoscalerOptions ?? {}));
    }
    if (/^\/autoscaling\/hpas\/[^/]+\/[^/]+\/bounds$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(boundsWrite ? boundsWrite(body) : boundsWriteFor(body, autoscalerOptions ?? {}));
    }
    if (path === '/resources/certificates.k8s.io/v1/certificatesigningrequests') {
      return json(certificateRequests ?? FIXTURES.certificateRequests);
    }
    // §25. Ordered before the decision route below only for readability — the
    // two paths do not overlap.
    if (/^\/certificates\/signing-requests\/[^/]+\/plan$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      const name = decodeURIComponent(path.split('/')[3]);
      return json(csrPlan ? csrPlan(name, body) : csrPlanFor(name, body.decision));
    }
    if (/^\/certificates\/signing-requests\/[^/]+$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      const name = decodeURIComponent(path.split('/')[3]);
      csrDecisions.push({ name, body });
      return json(csrDecide ? csrDecide(name, body) : csrDecisionFor(name, body));
    }
    if (path === '/resources/snapshot.storage.k8s.io/v1/volumesnapshots') {
      return json(snapshots ?? FIXTURES.snapshots);
    }
    if (path === '/resources/snapshot.storage.k8s.io/v1/volumesnapshotclasses') {
      return json(snapshotClasses ?? FIXTURES.snapshotClasses);
    }
    // §22. Ordered before the expand routes below only for readability — the
    // two paths do not overlap.
    if (/^\/storage\/claims\/[^/]+\/[^/]+\/snapshot\/plan$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(snapshotPlan ? snapshotPlan(body) : snapshotPlanFor(body, snapshotOptions ?? {}));
    }
    if (/^\/storage\/claims\/[^/]+\/[^/]+\/snapshot$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(snapshotWrite ? snapshotWrite(body) : snapshotWriteFor(body, snapshotOptions ?? {}));
    }
    if (path === '/resources/core/v1/persistentvolumeclaims') {
      return json(claims ?? FIXTURES.claims);
    }
    // §20. The plan answers 200 even for a size the claim cannot be given —
    // `blocked` is set instead — because that is the endpoint's contract, and a
    // fixture that returned 422 there would let a dialog that hides the claim
    // behind an error panel pass.
    if (/^\/storage\/claims\/[^/]+\/[^/]+\/expand\/plan$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(
        expandPlan ? expandPlan(body) : expandPlanFor(body, expandOptions ?? {}),
      );
    }
    if (/^\/storage\/claims\/[^/]+\/[^/]+\/size$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(
        expandWrite ? expandWrite(body) : expandFor(body, expandOptions ?? {}),
      );
    }
    // §15. The CLI pod, and the shell into it — the terminal itself is §7's
    // exec websocket, which no route here answers because Playwright never
    // opens one.
    if (path === '/cli') {
      if (route.request().method() === 'POST') {
        const body = JSON.parse(route.request().postData() || '{}');
        return json(
          cliCreate
            ? cliCreate(body)
            : {
                dryRun: body.dryRun !== false,
                // Derived, never echoed: §1.5 makes `applied` the only evidence
                // a cluster changed.
                applied: body.dryRun === false,
                verb: 'create',
                target: { group: '', version: 'v1', resource: 'pods',
                          namespace: 'default', name: 'k8boss-cli-x4k2p' },
                diff: {
                  before: '',
                  after: 'apiVersion: v1\nkind: Pod\n',
                  unified:
                    '--- live\n+++ projected\n@@ -0,0 +1,7 @@\n+apiVersion: v1\n+kind: Pod\n' +
                    '+spec:\n+  serviceAccountName: k8boss-cli\n' +
                    '+  automountServiceAccountToken: true\n+  containers:\n' +
                    '+    - image: alpine/k8s:1.34.9\n',
                  changed: true,
                },
                resourceVersion: '9002',
                warnings: [],
                auditId: 5151,
                pod: 'k8boss-cli-x4k2p',
                namespace: 'default',
                image: body.image || 'alpine/k8s:1.34.9',
                serviceAccount: 'k8boss-cli',
                container: 'cli',
              },
        );
      }
      return json(cli ?? FIXTURES.cliEnabled);
    }
    if (/^\/cli\/[^/]+$/.test(path) && route.request().method() === 'DELETE') {
      const url = new URL(route.request().url());
      const isDryRun = url.searchParams.get('dryRun') !== 'false';
      cliDeletes.push({ path, dryRun: isDryRun });
      return json({
        dryRun: isDryRun,
        applied: !isDryRun,
        verb: 'delete',
        target: { group: '', version: 'v1', resource: 'pods', namespace: 'default' },
        // §4: a delete diffs live against nothing.
        diff: { before: 'kind: Pod\n', after: '', unified: '--- live\n+++ projected\n@@ -1,1 +0,0 @@\n-kind: Pod\n', changed: true },
        resourceVersion: null,
        warnings: [],
        auditId: 5152,
      });
    }
    if (path === '/namespaces') return json(FIXTURES.namespaces);
    if (path === '/nodes') return json(FIXTURES.nodes);
    // §5.5. Ordered before the node detail branch: both live under /nodes/…, and
    // a router matching the detail first would answer a debug listing with a node.
    if (/^\/nodes\/[^/]+\/debug$/.test(path)) {
      if (route.request().method() === 'POST') {
        const body = JSON.parse(route.request().postData() || '{}');
        return json(
          nodeDebugCreate
            ? nodeDebugCreate(body)
            : {
                dryRun: body.dryRun !== false,
                // Derived, never echoed: §1.5 makes `applied` the only evidence
                // a cluster changed.
                applied: body.dryRun === false,
                verb: 'create',
                target: { group: '', version: 'v1', resource: 'pods',
                          namespace: 'default', name: 'node-debugger-ip-10-0-1-4-x4k2p' },
                diff: {
                  before: '',
                  after: 'apiVersion: v1\nkind: Pod\n',
                  unified:
                    '--- live\n+++ projected\n@@ -0,0 +1,8 @@\n+apiVersion: v1\n+kind: Pod\n' +
                    '+spec:\n+  nodeName: ip-10-0-1-4\n+  hostPID: true\n+  hostNetwork: true\n' +
                    '+  volumes:\n+    - hostPath:\n+        path: /\n',
                  changed: true,
                },
                resourceVersion: '9002',
                warnings: [],
                auditId: 5150,
                pod: 'node-debugger-ip-10-0-1-4-x4k2p',
                namespace: 'default',
                node: 'ip-10-0-1-4',
                image: body.image || 'busybox:1.36',
                writableHostFilesystem: body.writableHostFilesystem === true,
              },
        );
      }
      return json(nodeDebug ?? FIXTURES.nodeDebugEnabled);
    }
    if (/^\/nodes\/[^/]+\/debug\/[^/]+$/.test(path) && route.request().method() === 'DELETE') {
      const url = new URL(route.request().url());
      const isDryRun = url.searchParams.get('dryRun') !== 'false';
      nodeDebugDeletes.push({ path, dryRun: isDryRun });
      return json({
        dryRun: isDryRun,
        applied: !isDryRun,
        verb: 'delete',
        target: { group: '', version: 'v1', resource: 'pods', namespace: 'default' },
        // §4: a delete diffs live against nothing.
        diff: { before: 'kind: Pod\n', after: '', unified: '--- live\n+++ projected\n@@ -1,1 +0,0 @@\n-kind: Pod\n', changed: true },
        resourceVersion: null,
        warnings: [],
        auditId: 5151,
      });
    }
    // §24. Ordered before the node detail branch below: every one of these
    // lives under /nodes/…, and a router matching the detail first would answer
    // a taint plan with a node object.
    if (/^\/nodes\/[^/]+\/taints\/plan$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(taintPlan ? taintPlan(body) : taintPlanFor(body, nodeSchedulingOptions ?? {}));
    }
    if (/^\/nodes\/[^/]+\/taints$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      nodeSchedulingWrites.push({ kind: 'taints', body });
      return json(taintWrite ? taintWrite(body) : taintWriteFor(body, nodeSchedulingOptions ?? {}));
    }
    if (/^\/nodes\/[^/]+\/labels\/plan$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(labelPlan ? labelPlan(body) : labelPlanFor(body, nodeSchedulingOptions ?? {}));
    }
    if (/^\/nodes\/[^/]+\/labels$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      nodeSchedulingWrites.push({ kind: 'labels', body });
      return json(labelWrite ? labelWrite(body) : labelWriteFor(body, nodeSchedulingOptions ?? {}));
    }
    if (/^\/nodes\/[^/]+$/.test(path)) return json(nodeDetail ?? FIXTURES.nodeDetail);
    // §6's scale, with §21's `governedBy`. Mocked here rather than in a spec
    // because the key it carries is the whole point: a scale on an autoscaled
    // workload succeeds and is reverted, and the response is the only place
    // that says so.
    if (/^\/workloads\/[^/]+\/[^/]+\/[^/]+\/scale$/.test(path)) {
      const body = JSON.parse(route.request().postData() || '{}');
      const hpa = FIXTURES.autoscalers.items[0];
      return json({
        dryRun: body.dryRun !== false,
        applied: body.dryRun === false,
        verb: 'patch',
        target: { group: 'apps', version: 'v1', resource: 'deployments',
                  namespace: 'prod', name: 'checkout', subresource: 'scale' },
        diff: {
          unified: `--- live\n+++ projected\n@@\n-  replicas: 3\n+  replicas: ${body.replicas}\n`,
          digest: 'sha256:scale',
        },
        warnings: [],
        governedBy: scaleGovernedBy ?? {
          governed: true,
          autoscaler: hpa,
          reason: null,
          detail:
            `${hpa.name} autoscales this Deployment between ${hpa.min_replicas} and ` +
            `${hpa.max_replicas} replicas. Its next scale decision overrides the count set ` +
            'here — usually within seconds — unless it is not scaling at all.',
        },
      });
    }
    if (path === '/workloads') return json(workloads ?? FIXTURES.workloads);
    // §23. A POST that reads: the API server answers a question about somebody
    // else's access, and the backend writes one audit row for having asked.
    if (path === '/access/subject-review') {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(
        subjectReview ? subjectReview(body) : subjectReviewFor(body, subjectReviewOptions ?? {}),
      );
    }
    // §30. Two routes over one derivation, so the plan the dialog reads and the
    // response the write returns cannot drift apart in a fixture the way they
    // could in two hand-written payloads.
    if (path.startsWith('/access/namespaces/') && path.endsWith('/grants/plan')) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(grant ? grant(body) : grantPlanFor(body, grantOptions ?? {}));
    }
    if (path.startsWith('/access/namespaces/') && path.endsWith('/grants')) {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(grantWriteFor(body, grantOptions ?? {}));
    }
    if (path === '/access/preflight') {
      return json(
        route.request().method() === 'POST'
          ? // `preflight` is a `(checks) => results` function so a spec can answer
            // per check. §9 pairs results to checks strictly by index, and so
            // does this.
            {
              results: preflight
                ? preflight(JSON.parse(route.request().postData() || '{}').checks ?? [])
                : [],
            }
          : { verb: 'list', group: 'apps', resource: 'deployments', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null },
      );
    }

    /* ── §13 routes and §14 the shipped router ─────────────────────────── */

    // Ordered before the generic branches below and before each other's
    // prefixes: `/routes/capabilities` and `/routes/render` both live under
    // `/routes/…`, and a router matching the exposure path first would answer
    // a capabilities read with a single exposure.
    if (path === '/routes/capabilities') {
      return json(routeCapabilities ?? FIXTURES.routeCapabilities);
    }
    // §32. One segment after `/routes`, like capabilities, and ordered
    // with them for the same reason: the three-segment exposure path
    // below would otherwise answer this as a backend named
    // "certificates".
    if (path === '/routes/certificates') {
      return json(routeCertificates ?? FIXTURES.routeCertificates);
    }
    if (path === '/routes/render') {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(
        routeRender
          ? routeRender(body)
          : {
              backend: body.backend,
              kind: body.backend === 'ingress' ? 'Ingress' : 'Route',
              group: 'networking.k8s.io',
              version: 'v1',
              plural: 'ingresses',
              document: {},
              yaml: `apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: ${
                body.spec?.name ?? 'x'
              }\n`,
              lossy: [],
              preserved: [],
              requested: [],
            },
      );
    }
    if (path === '/routes' && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(
        routeWrite
          ? routeWrite(body)
          : {
              dryRun: body.dryRun !== false,
              // Derived, never echoed: §1.5 makes `applied` the only evidence
              // a cluster changed.
              applied: body.dryRun === false,
              verb: 'create',
              target: {
                group: 'networking.k8s.io', version: 'v1', resource: 'ingresses',
                namespace: body.spec?.namespace ?? 'prod', name: body.spec?.name ?? 'x',
              },
              diff: {
                before: '',
                after: 'apiVersion: networking.k8s.io/v1\nkind: Ingress\n',
                unified:
                  '--- live\n+++ projected\n@@ -0,0 +1,4 @@\n+apiVersion: networking.k8s.io/v1\n+kind: Ingress\n',
                changed: true,
              },
              resourceVersion: '5001',
              warnings: [],
              auditId: 7100,
              route: { backend: body.backend, kind: 'Ingress', lossy: [], preserved: [] },
            },
      );
    }
    if (path === '/routes') return json(routes ?? FIXTURES.routes);
    if (/^\/routes\/[^/]+\/[^/]+\/[^/]+$/.test(path)) {
      if (route.request().method() === 'GET') {
        return json(
          routeDetail ?? {
            route: FIXTURES.routes.items[0],
            manifest: {
              apiVersion: 'networking.k8s.io/v1',
              kind: 'Ingress',
              metadata: { name: 'shop', namespace: 'prod', resourceVersion: '4021' },
              spec: {},
            },
            backend: FIXTURES.routeCapabilities.items[1],
          },
        );
      }
      // PUT and DELETE answer with a §1.5 mutation response.
      const isDelete = route.request().method() === 'DELETE';
      const url = new URL(route.request().url());
      const dryRun = isDelete
        ? url.searchParams.get('dryRun') !== 'false'
        : JSON.parse(route.request().postData() || '{}').dryRun !== false;
      return json({
        dryRun,
        applied: !dryRun,
        verb: isDelete ? 'delete' : 'update',
        target: { group: 'networking.k8s.io', version: 'v1', resource: 'ingresses', namespace: 'prod', name: 'shop' },
        diff: { before: 'kind: Ingress\n', after: isDelete ? '' : 'kind: Ingress\n', unified: '--- live\n+++ projected\n@@ -1 +1 @@\n-a\n+b\n', changed: true },
        resourceVersion: isDelete ? null : '4022',
        warnings: [],
        auditId: 7101,
        route: { backend: 'ingress', kind: 'Ingress', lossy: [], preserved: [] },
      });
    }
    if (path === '/router/plan') return json(FIXTURES.routerPlan);
    if (path === '/router') {
      if (route.request().method() === 'POST') {
        const body = JSON.parse(route.request().postData() || '{}');
        return json(
          routerInstall
            ? routerInstall(body)
            : {
                dryRun: body.dryRun !== false,
                installed: body.dryRun === false,
                failed: 0,
                version: '3.2.13',
                namespace: body.namespace || 'k8boss-router',
                ingressClassName: body.ingressClassName || 'haproxy',
                objects: routerInstallObjectsFor(body),
                serves: FIXTURES.routerPlan.serves,
                options: FIXTURES.routerPlan.options,
              },
        );
      }
      if (route.request().method() === 'DELETE') {
        const url = new URL(route.request().url());
        const dryRun = url.searchParams.get('dryRun') !== 'false';
        return json({
          dryRun, removed: 7, failed: 0, uninstalled: !dryRun,
          objects: [], skipped: [],
          retained: [{ kind: 'Namespace', name: 'k8boss-router', reason: 'Deleting a namespace cannot be undone.' }],
        });
      }
      return json(routerStatus ?? FIXTURES.routerAbsent);
    }

    /* ── §16 the operator portal ───────────────────────────────────────── */

    if (path === '/portal/catalog') return json(portalCatalog ?? FIXTURES.portalCatalog);

    /* ── §33 installing OLM itself ─────────────────────────────────────── */

    // The plan is matched BEFORE the status route below: it lives under the
    // same prefix, and a router that matched `/portal/olm` first would answer a
    // plan with a status object — which renders as an empty dialog rather than
    // as an error, so nothing would fail loudly.
    if (path === '/portal/olm/plan' && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      if (olmPlan) return json(olmPlan(body));
      // `communityCatalog` adds a third consequence, derived rather than
      // echoed: a dialog that never re-reads the plan after ticking the box
      // would otherwise pass while the real backend refused the write.
      return json({
        ...FIXTURES.olmPlan,
        communityCatalog: Boolean(body.communityCatalog),
        consequences: body.communityCatalog
          ? [
              ...FIXTURES.olmPlan.consequences,
              {
                code: 'community_catalog',
                label: 'The cluster will pull and trust a community catalog',
                consequence: 'quay.io/operatorhubio/catalog:latest, re-polled hourly.',
                mitigation: 'Leave it off and add a CatalogSource you have chosen.',
              },
            ]
          : FIXTURES.olmPlan.consequences,
      });
    }
    if (path === '/portal/olm') {
      if (route.request().method() === 'POST') {
        const body = JSON.parse(route.request().postData() || '{}');
        if (olmInstall) return json(olmInstall(body));
        const dryRun = body.dryRun !== false;
        return json({
          dryRun,
          // Derived, never echoed. `installed` here means twenty-six objects
          // were accepted and nothing more — see `ready`, which is null on this
          // response on purpose.
          installed: !dryRun,
          failed: 0,
          skipped: 0,
          version: '0.35.0',
          namespaces: ['olm', 'operators'],
          communityCatalog: Boolean(body.communityCatalog),
          objects: FIXTURES.olmPlan.objects.map((object) => ({
            kind: object.kind,
            name: object.name,
            namespace: object.namespace,
            group: object.group,
            resource: object.resource,
            phase: object.phase,
            verb: 'create',
            applied: !dryRun,
            diff: { unified: `+++ ${object.kind}\n+${object.name}\n`, changed: true },
            projection: dryRun && object.phase === 'core' ? 'rendered' : 'server',
            preflight: null,
            skipped: null,
            auditId: dryRun ? null : 42,
            error: null,
          })),
          crds: dryRun
            ? null
            : { established: true, pending: [], unreadable: [], detail: null },
          consequences: FIXTURES.olmPlan.consequences,
          notes: FIXTURES.olmPlan.notes,
          ready: null,
          readyDetail:
            'Whether OLM is running is not knowable from this response. ' +
            'GET /api/portal/olm answers that, as a live read.',
        });
      }
      return json(olmStatus ?? FIXTURES.olmStatusAbsent);
    }
    // Ordered before `/portal/subscriptions`: the plan lives under it, and a
    // router that matched the listing first would answer a plan with a table.
    // §17 projects. The plan and the write derive their objects from the
    // request the way the backend does, and derive `consequences` from the
    // request's omissions rather than echoing anything the client sent — a
    // mock that returned whatever it was asked for would let a dialog that
    // never reads them pass.
    if (path === '/projects/plan' && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(projectPlan ? projectPlan(body) : projectPlanFor(body));
    }
    if (path === '/projects' && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(projectCreate ? projectCreate(body) : projectCreateFor(body));
    }
    // Ordered before the `/projects/{name}` read: that one matches any path
    // under the prefix, and a router that hit it first would answer §18's plan
    // with a namespace.
    //
    // `current` comes from whichever project fixture this test is using, not
    // from FIXTURES.project: a spec that overrides the namespace's labels to
    // test raising the level would otherwise get a plan computed against
    // different labels than the page is showing, and the disagreement would
    // look like a bug in the dialog.
    if (path.endsWith('/pod-security/plan') && route.request().method() === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}');
      const current = (project ?? FIXTURES.project).podSecurity;
      return json(podSecurityPlan ? podSecurityPlan(body) : podSecurityPlanFor(body, { current }));
    }
    if (path.endsWith('/pod-security') && route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}');
      const current = (project ?? FIXTURES.project).podSecurity;
      return json(podSecuritySet ? podSecuritySet(body) : podSecuritySetFor(body, { current }));
    }
    // §26. Ordered with the other subpaths, before the `/projects/{name}` read
    // that matches anything under the prefix.
    if (path.endsWith('/delete-plan') && route.request().method() === 'GET') {
      const name = decodeURIComponent(path.split('/')[2]);
      return json(
        namespaceDeletePlan
          ? namespaceDeletePlan(name)
          : namespaceDeletePlanFor(name, namespaceDeleteOverrides ?? {}),
      );
    }
    if (path.startsWith('/projects/') && route.request().method() === 'DELETE') {
      const name = decodeURIComponent(path.split('/')[2]);
      const body = JSON.parse(route.request().postData() || '{}');
      namespaceDeletes.push({ name, body });
      const { status, payload } = namespaceDeleteFor(
        name, body, namespaceDeleteOverrides ?? {},
      );
      return json(payload, status);
    }
    if (path.startsWith('/projects/')) {
      return json(project ?? FIXTURES.project);
    }
    if (path === '/portal/subscriptions/plan') {
      const body = JSON.parse(route.request().postData() || '{}');
      return json(
        portalPlan
          ? portalPlan(body)
          : {
              ...FIXTURES.portalPlan,
              // The request echoed back, because a plan *is* the caller's own
              // request rendered against the catalog. `consequences` is the one
              // thing never echoed — it is derived from the cluster, and a mock
              // that returned whatever the client asked for would let a UI that
              // never reads them pass.
              package: body.package,
              namespace: body.namespace,
              channel: body.channel || FIXTURES.portalPlan.channel,
              installPlanApproval: body.installPlanApproval,
              target: { ...FIXTURES.portalPlan.target, namespace: body.namespace },
            },
      );
    }
    if (path === '/portal/subscriptions') {
      if (route.request().method() === 'POST') {
        const body = JSON.parse(route.request().postData() || '{}');
        return json(
          portalSubscribe
            ? portalSubscribe(body)
            : {
                dryRun: body.dryRun !== false,
                // Derived, never echoed: §1.5 makes `applied` the only evidence
                // a cluster changed — and here it is evidence that one
                // Subscription object exists, never that an operator installed.
                applied: body.dryRun === false,
                verb: 'create',
                diff: {
                  before: '',
                  after: FIXTURES.portalPlan.document,
                  unified:
                    '--- live\n+++ projected\n@@ -0,0 +1,11 @@\n' +
                    FIXTURES.portalPlan.document.replace(/^(?!$)/gm, '+'),
                  changed: true,
                },
                resourceVersion: '77201',
                // §1.5's own key, left alone: it carries the API server's
                // Warning: headers for this create, which is a different thing
                // from the plan's consequences below.
                warnings: [],
                auditId: 8100,
                package: body.package,
                channel: body.channel,
                catalog: body.catalog,
                installPlanApproval: body.installPlanApproval,
                // What OLM is *expected* to install. Not a claim that it did.
                expectedCSV: FIXTURES.portalPlan.selected.currentCSV,
                expectedVersion: FIXTURES.portalPlan.selected.version,
                // The namespace verdict, under its own key: §1.5's `target`
                // above is the group-version-resource this write addressed and
                // is left alone.
                installTarget: { ...FIXTURES.portalPlan.target, namespace: body.namespace },
                consequences: [],
                partial: false,
                unavailable: [],
              },
        );
      }
      return json(portalInstalled ?? FIXTURES.portalInstalled);
    }

    // Everything else: a well-formed, complete, empty listing. Complete on
    // purpose — an unmocked endpoint must not accidentally satisfy the
    // partial-banner assertions below.
    return json(FIXTURES.emptyList);
  });

  // The API client seeds its cluster scope from localStorage at module load, so
  // this has to be installed before any script runs, not after navigation.
  await page.addInitScript(() => {
    localStorage.setItem('k8boss-admin.activeClusterId', '1');
    localStorage.setItem('k8boss-admin.theme', 'light');
  });
}

/**
 * Assert that a page rendered its content rather than the error boundary.
 *
 * Worth a helper because of what it is guarding. `Audit.jsx` and
 * `PodTerminal.jsx` both shipped using components they never imported: ESLint's
 * core `no-undef` does not see JSX element names, `vite build` treats an
 * undefined global as legal JavaScript, and no spec visited either surface. The
 * whole toolchain was silent about two pages that threw `ReferenceError` on
 * first render, and the operator got the error boundary's panel instead of the
 * page.
 *
 * A spec that visits a route and asserts on one element it expects is what
 * closes that gap for good; `react/jsx-no-undef` in `eslint.config.js` is what
 * closes it before it reaches a browser.
 */
export async function expectPageRendered(page, headingName) {
  await expect(page.getByText('failed to render')).toHaveCount(0);
  // Level 1 and exact: PatternFly renders every inline Alert title as a heading
  // too, so a page whose banner mentions its own name matches twice.
  await expect(
    page.getByRole('heading', { level: 1, name: headingName, exact: true }),
  ).toBeVisible();
}
