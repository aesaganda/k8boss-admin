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
    pods: [],
    unavailable: [],
  },

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
    isolation = null,
    podDetail = null,
    podEnvironment = null,
    podMetrics = null,
    debug = null,
    debugAttach = null,
    preflight = null,
    nodeDetail = null,
    nodeDebug = null,
    nodeDebugCreate = null,
    nodeDebugDeletes = [],
    cli = null,
    cliCreate = null,
    cliDeletes = [],
    routeCapabilities = null,
    routes = null,
    routeRender = null,
    routeWrite = null,
    routeDetail = null,
    routerStatus = null,
    routerInstall = null,
    portalCatalog = null,
    portalInstalled = null,
    portalPlan = null,
    portalSubscribe = null,
    project = null,
    projectPlan = null,
    projectCreate = null,
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
      return json(
        auth
          ? {
              enabled: true,
              localEnabled: true,
              ldapEnabled: Boolean(auth.ldapEnabled),
              oidcEnabled: Boolean(auth.ssoEnabled),
              methods: [
                'local',
                ...(auth.ldapEnabled ? ['ldap'] : []),
                ...(auth.ssoEnabled ? ['oidc'] : []),
              ],
              oidc: auth.ssoEnabled
                ? { label: auth.ssoLabel || 'Single sign-on', startPath: '/api/auth/oidc/start' }
                : null,
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
    if (path === '/clusters') return json(FIXTURES.clusters);
    if (/^\/clusters\/\d+\/overview$/.test(path)) return json(FIXTURES.overview);
    if (/^\/clusters\/\d+\/test$/.test(path)) {
      return json({ reachable: true, server_version: 'v1.31.4', latency_ms: 42, permissions: [] });
    }
    if (path === '/resources/catalog') return json(FIXTURES.catalog);
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
    if (path === '/resources/core/v1/services') return json(services ?? FIXTURES.services);
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
    if (/^\/nodes\/[^/]+$/.test(path)) return json(nodeDetail ?? FIXTURES.nodeDetail);
    if (path === '/workloads') return json(workloads ?? FIXTURES.workloads);
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
