/**
 * The cluster the product tour is shot against.
 *
 * A tour is marketing material, and marketing material is where a console
 * usually starts lying: every table full, every number green, every read
 * successful. This console's entire argument is that it does not do that — so
 * the fixture cluster below is deliberately a *real* one, mid-incident, with
 * reads that failed.
 *
 * Every `null` here is load-bearing and matches a rule in `docs/api-contract.md`:
 *
 *   - `overview.capacity` / `overview.requested` are null because the node
 *     listing was denied (§0.1's corollary — a number we could not derive is
 *     `null`, never `0`).
 *   - `ip-10-0-1-6.requested` and `.pod_count` are null because that node's pod
 *     listing failed. `0` would read as an idle node, and an idle node is the
 *     one an operator drains.
 *   - `payments-…-qq4mn.restarts` is null: a pod in CrashLoopBackOff showing
 *     "0 restarts" is the wrong answer delivered confidently.
 *   - `log-shipper` is `Unknown`, not `Healthy`: its controller has not written
 *     a status for the current generation, so we have been told nothing.
 *
 * A screenshot of any of those is worth more than a screenshot of a clean
 * cluster, because the clean cluster is what every other console can also show.
 */

const HOUR = 3600;
const DAY = 24 * HOUR;

/* ── §3 the cluster itself ──────────────────────────────────────────────── */

export const CLUSTERS = {
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
      last_connected: '2026-08-20T09:41:02Z',
      created_at: '2026-02-11T10:00:00Z',
      updated_at: '2026-08-19T12:41:09Z',
    },
    {
      id: 2,
      name: 'staging-eu',
      platform: 'kubernetes',
      api_server: 'https://api.staging-eu.example:6443',
      authentication_type: 'client_certificate',
      has_ca_certificate: true,
      skip_tls_verify: false,
      status: 'connected',
      server_version: 'v1.31.2',
      last_connected: '2026-08-20T09:40:55Z',
      created_at: '2026-02-11T10:04:00Z',
      updated_at: '2026-07-30T08:15:00Z',
    },
    {
      id: 3,
      name: 'edge-us',
      platform: 'openshift',
      api_server: 'https://api.edge-us.example:6443',
      authentication_type: 'service_account_token',
      has_ca_certificate: false,
      skip_tls_verify: false,
      // Never reached, so nothing about its contents is claimed anywhere.
      status: 'unreachable',
      server_version: null,
      last_connected: '2026-08-19T22:14:07Z',
      created_at: '2026-05-02T09:00:00Z',
      updated_at: '2026-08-19T22:14:07Z',
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/**
 * §9's baseline permission report, in all three states the contract requires
 * the UI to keep apart: allowed, cleanly denied, and *review failed* — which
 * means we do not know, and which must never render as a denial.
 */
export const CLUSTER_TEST = {
  reachable: true,
  server_version: 'v1.31.4',
  latency_ms: 38,
  permissions: [
    { verb: 'list', group: '', resource: 'pods', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null },
    { verb: 'list', group: '', resource: 'nodes', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null },
    { verb: 'list', group: 'apps', resource: 'deployments', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null },
    { verb: 'patch', group: 'apps', resource: 'deployments', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null },
    {
      verb: 'delete', group: '', resource: 'namespaces', namespace: null, allowed: false,
      reason: 'no RBAC policy matched', evaluationError: null,
      hint: 'Grant `delete` on `namespaces` (core group) at the cluster scope to the console ServiceAccount.',
    },
    {
      // The third state. `allowed: false` with an evaluationError is "we could
      // not find out", and rendering it red sends someone to edit a ClusterRole
      // that is already correct.
      verb: 'create', group: '', resource: 'pods', namespace: null, subresource: 'eviction',
      allowed: false, reason: '',
      evaluationError: 'webhook "authz.example.com" denied the request: context deadline exceeded',
      hint: null,
    },
  ],
};

/* ── §3 overview ────────────────────────────────────────────────────────── */

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

export const OVERVIEW = {
  server_version: 'v1.31.4',
  platform: 'kubernetes',
  nodes: { total: 12, ready: 11, unschedulable: 1 },
  namespaces: 34,
  workloads: { deployments: 210, statefulsets: 12, daemonsets: 8, jobs: 41, cronjobs: 9 },
  pods: { total: 1204, running: 1180, pending: 8, failed: 3, succeeded: 13 },
  // Null because the node listing above was denied. The tiles render a dash,
  // and the banner says why. "0 cores" is the defect this project is built
  // against.
  capacity: null,
  requested: null,
  unavailable: OVERVIEW_UNAVAILABLE,
};

/* ── §5 nodes ───────────────────────────────────────────────────────────── */

const node = (over) => ({
  ready: true,
  unschedulable: false,
  roles: ['worker'],
  kubelet_version: 'v1.31.4',
  os_image: 'Amazon Linux 2',
  container_runtime: 'containerd://1.7.13',
  age_seconds: 94 * DAY,
  capacity: { cpu_cores: 16, memory_bytes: 68719476736, pods: 110 },
  allocatable: { cpu_cores: 15.8, memory_bytes: 66571993088, pods: 110 },
  conditions: [{ type: 'MemoryPressure', status: 'False', reason: 'KubeletHasSufficientMemory' }],
  taints: [],
  ...over,
});

export const NODES = {
  items: [
    node({
      name: 'ip-10-0-0-11', roles: ['control-plane'], internal_ip: '10.0.0.11',
      capacity: { cpu_cores: 8, memory_bytes: 34359738368, pods: 110 },
      allocatable: { cpu_cores: 7.9, memory_bytes: 33285996544, pods: 110 },
      requested: { cpu_cores: 2.35, memory_bytes: 6442450944, pods: 21 }, pod_count: 21,
      taints: [{ key: 'node-role.kubernetes.io/control-plane', effect: 'NoSchedule' }],
    }),
    node({
      name: 'ip-10-0-0-12', roles: ['control-plane'], internal_ip: '10.0.0.12',
      capacity: { cpu_cores: 8, memory_bytes: 34359738368, pods: 110 },
      allocatable: { cpu_cores: 7.9, memory_bytes: 33285996544, pods: 110 },
      requested: { cpu_cores: 2.10, memory_bytes: 5905580032, pods: 19 }, pod_count: 19,
      taints: [{ key: 'node-role.kubernetes.io/control-plane', effect: 'NoSchedule' }],
    }),
    node({
      name: 'ip-10-0-1-4', internal_ip: '10.0.1.4',
      requested: { cpu_cores: 11.2, memory_bytes: 45097156608, pods: 74 }, pod_count: 74,
    }),
    node({
      name: 'ip-10-0-1-5', internal_ip: '10.0.1.5',
      requested: { cpu_cores: 13.9, memory_bytes: 58720256000, pods: 92 }, pod_count: 92,
    }),
    node({
      // The one the whole page exists for. Its pod listing failed, so the two
      // derived numbers are null and render as dashes with a reason — never as
      // an idle-looking `0`.
      name: 'ip-10-0-1-6', internal_ip: '10.0.1.6',
      requested: null, pod_count: null,
    }),
    node({
      name: 'ip-10-0-2-11', internal_ip: '10.0.2.11', unschedulable: true,
      requested: { cpu_cores: 6.4, memory_bytes: 21474836480, pods: 38 }, pod_count: 38,
    }),
    node({
      name: 'ip-10-0-2-12', internal_ip: '10.0.2.12',
      requested: { cpu_cores: 9.75, memory_bytes: 38654705664, pods: 61 }, pod_count: 61,
      taints: [{ key: 'workload', value: 'batch', effect: 'NoSchedule' }],
    }),
    node({
      name: 'ip-10-0-3-7', internal_ip: '10.0.3.7', ready: false,
      requested: { cpu_cores: 4.2, memory_bytes: 17179869184, pods: 29 }, pod_count: 29,
      conditions: [
        { type: 'Ready', status: 'False', reason: 'KubeletNotReady' },
        { type: 'MemoryPressure', status: 'True', reason: 'KubeletHasInsufficientMemory' },
      ],
    }),
  ],
  continue: null,
  remaining: null,
  partial: true,
  unavailable: [
    {
      group: '', resource: 'pods', namespace: null, reason: 'forbidden',
      detail:
        'pods is forbidden: User "system:serviceaccount:k8boss-admin:console" cannot list resource "pods" ' +
        'with field selector spec.nodeName=ip-10-0-1-6',
    },
  ],
};

/* ── §6 workloads ───────────────────────────────────────────────────────── */

const workload = (over) => ({
  labels: {},
  restarts_24h: 0,
  suspended: null,
  schedule: null,
  last_schedule: null,
  status_reason: '',
  ...over,
});

export const WORKLOADS = {
  items: [
    workload({
      kind: 'Deployment', name: 'checkout', namespace: 'prod',
      replicas: { desired: 5, ready: 4, updated: 5, available: 4 },
      images: ['ghcr.io/acme/checkout:1.9.2'], selector: { app: 'checkout' },
      age_seconds: 14 * DAY, status: 'Progressing',
      status_reason: '1 of 5 replicas not available', restarts_24h: 2,
    }),
    workload({
      kind: 'Deployment', name: 'payments', namespace: 'prod',
      replicas: { desired: 3, ready: 0, updated: 3, available: 0 },
      images: ['ghcr.io/acme/payments:2.0.1'], selector: { app: 'payments' },
      age_seconds: 3 * HOUR, status: 'Degraded',
      status_reason: 'no replicas available; containers in CrashLoopBackOff',
      // Null, not zero: the pod listing behind this count failed, and a
      // degraded workload reporting "0 restarts in 24h" is a confident lie.
      restarts_24h: null,
    }),
    workload({
      kind: 'Deployment', name: 'web', namespace: 'prod',
      replicas: { desired: 6, ready: 6, updated: 6, available: 6 },
      images: ['ghcr.io/acme/web:4.2.0'], selector: { app: 'web' },
      age_seconds: 61 * DAY, status: 'Healthy',
    }),
    workload({
      kind: 'StatefulSet', name: 'sessions', namespace: 'prod',
      replicas: { desired: 3, ready: 3, updated: 3, available: 3 },
      images: ['redis:7.2-alpine'], selector: { app: 'sessions' },
      age_seconds: 120 * DAY, status: 'Healthy',
    }),
    workload({
      kind: 'DaemonSet', name: 'node-exporter', namespace: 'monitoring',
      replicas: { desired: 12, ready: 12, updated: 12, available: 12 },
      images: ['quay.io/prometheus/node-exporter:v1.8.2'], selector: { app: 'node-exporter' },
      age_seconds: 200 * DAY, status: 'Healthy',
    }),
    workload({
      // The status that makes the table honest. The controller has not written
      // a status for the current generation, so it has told us nothing —
      // rendering that as Healthy would make a green row a lie.
      kind: 'DaemonSet', name: 'log-shipper', namespace: 'monitoring',
      replicas: { desired: 12, ready: null, updated: null, available: null },
      images: ['ghcr.io/acme/log-shipper:0.7.4'], selector: { app: 'log-shipper' },
      age_seconds: 9 * DAY, status: 'Unknown',
      status_reason: 'controller has not reported on generation 14',
      restarts_24h: null,
    }),
    workload({
      kind: 'CronJob', name: 'db-backup', namespace: 'prod',
      replicas: { desired: null, ready: null, updated: null, available: null },
      images: ['ghcr.io/acme/pgdump:3.1.0'], selector: {},
      age_seconds: 300 * DAY, status: 'Healthy',
      suspended: false, schedule: '0 */6 * * *', last_schedule: '2026-08-20T06:00:00Z',
    }),
    workload({
      // Suspended on purpose is a settled state, not a problem to surface.
      kind: 'CronJob', name: 'nightly-report', namespace: 'analytics',
      replicas: { desired: null, ready: null, updated: null, available: null },
      images: ['ghcr.io/acme/reporter:2.2.1'], selector: {},
      age_seconds: 180 * DAY, status: 'Suspended',
      suspended: true, schedule: '30 2 * * *', last_schedule: '2026-08-14T02:30:00Z',
    }),
    workload({
      kind: 'Job', name: 'migrate-2026-08-19', namespace: 'prod',
      replicas: { desired: 1, ready: 0, updated: 1, available: 0 },
      images: ['ghcr.io/acme/migrate:1.4.0'], selector: { job: 'migrate' },
      age_seconds: 19 * HOUR, status: 'Healthy',
      status_reason: 'completed', suspended: false,
    }),
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/**
 * §6 detail for `prod/checkout`, the workload the write demo is shot on.
 *
 * The envelope is the one `get_workload_detail` actually returns —
 * `{workload, spec, pods, conditions, services, rollout, partial, unavailable}` —
 * not the flattened row. Binding a fixture to the shape the contract *describes*
 * rather than the shape the code *returns* is how a tour ends up photographing
 * an empty page.
 */
export const CHECKOUT_DETAIL = {
  workload: {
    kind: 'Deployment',
    name: 'checkout',
    namespace: 'prod',
    replicas: { desired: 5, ready: 4, updated: 5, available: 4 },
    images: ['ghcr.io/acme/checkout:1.9.2'],
    selector: { app: 'checkout' },
    labels: { app: 'checkout', 'app.kubernetes.io/part-of': 'storefront' },
    age_seconds: 14 * DAY,
    status: 'Progressing',
    status_reason: '1 of 5 replicas not available',
    restarts_24h: 2,
    suspended: null,
    schedule: null,
    last_schedule: null,
    resourceVersion: '884213',
    creationTimestamp: '2026-08-06T09:12:00Z',
  },
  spec: {
    serviceAccount: 'checkout',
    nodeSelector: {},
    tolerations: [],
    volumes: [{ name: 'config', type: 'ConfigMap' }, { name: 'tmp', type: 'EmptyDir' }],
    containers: [
      {
        name: 'app',
        image: 'ghcr.io/acme/checkout:1.9.2',
        type: 'container',
        ports: [{ name: 'http', containerPort: 8080, protocol: 'TCP' }],
        resources: { requests: { cpu: '250m', memory: '512Mi' }, limits: { cpu: '1', memory: '1Gi' } },
        env_count: 11,
        probes: { readiness: 'httpGet /healthz:8080', liveness: 'httpGet /livez:8080', startup: null },
      },
    ],
  },
  pods: [
    { name: 'checkout-7d9f8b6c4-hk2xv', namespace: 'prod', phase: 'Running', phase_detail: null, ready: '1/1', restarts: 0, node: 'ip-10-0-1-4', qos_class: 'Burstable', ip: '10.128.4.17', age_seconds: 14 * HOUR, creationTimestamp: '2026-08-19T19:00:00Z', containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: true, restart_count: 0 }] },
    { name: 'checkout-7d9f8b6c4-r2wq9', namespace: 'prod', phase: 'Running', phase_detail: null, ready: '1/1', restarts: 1, node: 'ip-10-0-1-5', qos_class: 'Burstable', ip: '10.128.5.31', age_seconds: 14 * HOUR, creationTimestamp: '2026-08-19T19:00:00Z', containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: true, restart_count: 1 }] },
    { name: 'checkout-7d9f8b6c4-t8m4x', namespace: 'prod', phase: 'Running', phase_detail: null, ready: '1/1', restarts: 0, node: 'ip-10-0-2-12', qos_class: 'Burstable', ip: '10.128.6.9', age_seconds: 14 * HOUR, creationTimestamp: '2026-08-19T19:00:00Z', containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: true, restart_count: 0 }] },
    { name: 'checkout-7d9f8b6c4-w3n7k', namespace: 'prod', phase: 'Running', phase_detail: null, ready: '1/1', restarts: 0, node: 'ip-10-0-1-4', qos_class: 'Burstable', ip: '10.128.4.22', age_seconds: 14 * HOUR, creationTimestamp: '2026-08-19T19:00:00Z', containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: true, restart_count: 0 }] },
    { name: 'checkout-7d9f8b6c4-p9k2m', namespace: 'prod', phase: 'Pending', phase_detail: 'Unschedulable', ready: '0/1', restarts: 0, node: null, qos_class: 'Burstable', ip: null, age_seconds: 16 * 60, creationTimestamp: '2026-08-20T09:30:00Z', containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: false, restart_count: 0 }] },
  ],
  conditions: [
    { type: 'Available', status: 'False', reason: 'MinimumReplicasUnavailable', message: 'Deployment does not have minimum availability.', lastTransitionTime: '2026-08-20T09:31:00Z' },
    { type: 'Progressing', status: 'True', reason: 'ReplicaSetUpdated', message: 'ReplicaSet "checkout-7d9f8b6c4" is progressing.', lastTransitionTime: '2026-08-20T09:30:12Z' },
  ],
  services: [
    { name: 'checkout', namespace: 'prod', type: 'ClusterIP', cluster_ip: '10.96.14.203', ports: [{ name: 'http', port: 80, targetPort: 8080, protocol: 'TCP' }], selector: { app: 'checkout' }, endpoint_count: 4 },
  ],
  rollout: { strategy: 'RollingUpdate', maxSurge: '25%', maxUnavailable: 0, revision: 14 },
  partial: false,
  unavailable: [],
};

/* ── §6 pods ────────────────────────────────────────────────────────────── */

const pod = (over) => ({
  namespace: 'prod',
  phase: 'Running',
  phase_detail: null,
  restarts: 0,
  qos_class: 'Burstable',
  ...over,
});

export const PODS = {
  items: [
    pod({
      name: 'checkout-7d9f8b6c4-hk2xv', ready: '1/1', node: 'ip-10-0-1-4',
      containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: true, restart_count: 0 }],
      age_seconds: 14 * HOUR, creationTimestamp: '2026-08-19T19:00:00Z',
    }),
    pod({
      name: 'checkout-7d9f8b6c4-r2wq9', ready: '1/1', node: 'ip-10-0-1-5',
      containers: [{ name: 'app', image: 'ghcr.io/acme/checkout:1.9.2', ready: true, restart_count: 1 }],
      restarts: 1, age_seconds: 14 * HOUR, creationTimestamp: '2026-08-19T19:00:00Z',
    }),
    pod({
      // The pod this page exists for: phase says Running, the container says
      // CrashLoopBackOff. The Status column renders the detail over the phase.
      name: 'payments-5c8d9f7b6-qq4mn', phase_detail: 'CrashLoopBackOff', ready: '0/1',
      node: 'ip-10-0-1-5',
      containers: [{ name: 'app', image: 'ghcr.io/acme/payments:2.0.1', ready: false, restart_count: null }],
      // Null, not zero. See the module docstring.
      restarts: null, age_seconds: 3 * HOUR, creationTimestamp: '2026-08-20T06:41:00Z',
    }),
    pod({
      name: 'payments-5c8d9f7b6-v8t2p', phase_detail: 'CrashLoopBackOff', ready: '0/1',
      node: 'ip-10-0-1-4',
      containers: [{ name: 'app', image: 'ghcr.io/acme/payments:2.0.1', ready: false, restart_count: 47 }],
      restarts: 47, age_seconds: 3 * HOUR, creationTimestamp: '2026-08-20T06:41:00Z',
    }),
    pod({
      name: 'web-6f4b8c9d5-2xkzp', ready: '2/2', node: 'ip-10-0-2-12', qos_class: 'Guaranteed',
      containers: [
        { name: 'web', image: 'ghcr.io/acme/web:4.2.0', ready: true, restart_count: 0 },
        { name: 'envoy', image: 'envoyproxy/envoy:v1.31.0', ready: true, restart_count: 0 },
      ],
      age_seconds: 6 * DAY, creationTimestamp: '2026-08-14T09:00:00Z',
    }),
    pod({
      name: 'sessions-0', ready: '1/1', node: 'ip-10-0-1-4', qos_class: 'Guaranteed',
      containers: [{ name: 'redis', image: 'redis:7.2-alpine', ready: true, restart_count: 0 }],
      age_seconds: 42 * DAY, creationTimestamp: '2026-07-09T09:00:00Z',
    }),
    pod({
      name: 'log-shipper-nx7c4', namespace: 'monitoring', phase: 'Pending',
      phase_detail: 'ImagePullBackOff', ready: '0/1', node: 'ip-10-0-2-11',
      containers: [{ name: 'shipper', image: 'ghcr.io/acme/log-shipper:0.7.4', ready: false, restart_count: 0 }],
      age_seconds: 2 * HOUR, creationTimestamp: '2026-08-20T07:30:00Z',
    }),
    pod({
      name: 'migrate-2026-08-19-lm4dq', phase: 'Succeeded', ready: '0/1', node: 'ip-10-0-2-12',
      qos_class: 'BestEffort',
      containers: [{ name: 'migrate', image: 'ghcr.io/acme/migrate:1.4.0', ready: false, restart_count: 0 }],
      age_seconds: 19 * HOUR, creationTimestamp: '2026-08-19T14:02:00Z',
    }),
    pod({
      // Phase lies the other way too: a pod being deleted still reports Running.
      name: 'web-5d9c7b8a4-ftr6l', phase_detail: 'Terminating', ready: '1/1', node: 'ip-10-0-2-11',
      containers: [{ name: 'web', image: 'ghcr.io/acme/web:4.1.9', ready: true, restart_count: 0 }],
      age_seconds: 8 * DAY, creationTimestamp: '2026-08-12T11:00:00Z',
    }),
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/* ── §5 namespaces ──────────────────────────────────────────────────────── */

export const NAMESPACES = {
  items: [
    { name: 'prod', status: 'Active', labels: { tier: 'production' }, annotations: {}, age_seconds: 228 * DAY, pod_count: 412, creationTimestamp: '2026-01-04T08:00:00Z' },
    { name: 'staging', status: 'Active', labels: { tier: 'staging' }, annotations: {}, age_seconds: 228 * DAY, pod_count: 168, creationTimestamp: '2026-01-04T08:01:00Z' },
    { name: 'analytics', status: 'Active', labels: {}, annotations: {}, age_seconds: 190 * DAY, pod_count: 54, creationTimestamp: '2026-02-11T08:00:00Z' },
    { name: 'monitoring', status: 'Active', labels: {}, annotations: {}, age_seconds: 228 * DAY, pod_count: 61, creationTimestamp: '2026-01-04T08:02:00Z' },
    // Null, not zero: this namespace's pod listing was denied. `0` would read
    // as "safe to delete".
    { name: 'kube-system', status: 'Active', labels: {}, annotations: {}, age_seconds: 228 * DAY, pod_count: null, creationTimestamp: '2026-01-04T07:59:00Z' },
    { name: 'cert-manager', status: 'Active', labels: {}, annotations: {}, age_seconds: 210 * DAY, pod_count: 6, creationTimestamp: '2026-01-22T10:00:00Z' },
    { name: 'legacy-batch', status: 'Terminating', labels: {}, annotations: {}, age_seconds: 400 * DAY, pod_count: 2, creationTimestamp: '2025-07-16T10:00:00Z' },
  ],
  continue: null,
  remaining: null,
  partial: true,
  unavailable: [
    {
      group: '', resource: 'pods', namespace: 'kube-system', reason: 'forbidden',
      detail: 'pods is forbidden: cannot list resource "pods" in namespace "kube-system"',
    },
  ],
};

/* ── §4 the catalog ─────────────────────────────────────────────────────── */

const api = (group, version, kind, resource, over = {}) => ({
  group, version, kind, resource,
  namespaced: true,
  verbs: ['get', 'list', 'create', 'update', 'patch', 'delete'],
  shortNames: [],
  categories: [],
  apiVersion: group ? `${group}/${version}` : version,
  preferred: true,
  ...over,
});

export const CATALOG = {
  items: [
    api('', 'v1', 'Pod', 'pods', { shortNames: ['po'], categories: ['all'] }),
    api('', 'v1', 'Service', 'services', { shortNames: ['svc'], categories: ['all'] }),
    api('', 'v1', 'ConfigMap', 'configmaps', { shortNames: ['cm'] }),
    api('', 'v1', 'Secret', 'secrets'),
    api('', 'v1', 'Namespace', 'namespaces', { namespaced: false, shortNames: ['ns'] }),
    api('', 'v1', 'Node', 'nodes', { namespaced: false, shortNames: ['no'] }),
    api('', 'v1', 'PersistentVolumeClaim', 'persistentvolumeclaims', { shortNames: ['pvc'] }),
    api('apps', 'v1', 'Deployment', 'deployments', { shortNames: ['deploy'], categories: ['all'] }),
    api('apps', 'v1', 'StatefulSet', 'statefulsets', { shortNames: ['sts'], categories: ['all'] }),
    api('apps', 'v1', 'DaemonSet', 'daemonsets', { shortNames: ['ds'], categories: ['all'] }),
    api('apps', 'v1', 'ReplicaSet', 'replicasets', { shortNames: ['rs'], categories: ['all'] }),
    api('batch', 'v1', 'CronJob', 'cronjobs', { shortNames: ['cj'], categories: ['all'] }),
    api('batch', 'v1', 'Job', 'jobs', { categories: ['all'] }),
    api('networking.k8s.io', 'v1', 'Ingress', 'ingresses', { shortNames: ['ing'] }),
    api('networking.k8s.io', 'v1', 'NetworkPolicy', 'networkpolicies', { shortNames: ['netpol'] }),
    api('rbac.authorization.k8s.io', 'v1', 'Role', 'roles'),
    api('rbac.authorization.k8s.io', 'v1', 'ClusterRole', 'clusterroles', { namespaced: false }),
    api('policy', 'v1', 'PodDisruptionBudget', 'poddisruptionbudgets', { shortNames: ['pdb'] }),
    api('apiextensions.k8s.io', 'v1', 'CustomResourceDefinition', 'customresourcedefinitions', { namespaced: false, shortNames: ['crd'] }),
    // The CRDs are the point of this page: nobody wrote a page for these, and
    // they are one navigation away with the same audited write path.
    api('cert-manager.io', 'v1', 'Certificate', 'certificates', { shortNames: ['cert'] }),
    api('cert-manager.io', 'v1', 'ClusterIssuer', 'clusterissuers', { namespaced: false }),
    api('argoproj.io', 'v1alpha1', 'Application', 'applications', { shortNames: ['app'] }),
    api('argoproj.io', 'v1alpha1', 'Rollout', 'rollouts'),
    api('monitoring.coreos.com', 'v1', 'ServiceMonitor', 'servicemonitors'),
    api('monitoring.coreos.com', 'v1', 'PrometheusRule', 'prometheusrules'),
    api('kafka.strimzi.io', 'v1beta2', 'KafkaTopic', 'kafkatopics', { shortNames: ['kt'] }),
    api('kafka.strimzi.io', 'v1beta2', 'Kafka', 'kafkas', { shortNames: ['k'] }),
    api('external-secrets.io', 'v1beta1', 'ExternalSecret', 'externalsecrets', { shortNames: ['es'] }),
  ],
  continue: null,
  remaining: null,
  partial: true,
  // The canonical §4 case: an aggregated APIService is down, so this group
  // could not be enumerated. Without this entry an operator cannot tell "this
  // cluster has no metrics" from "we could not look" — and backwards, that
  // sends someone to install what they already have.
  unavailable: [
    {
      group: 'metrics.k8s.io', resource: '', namespace: null, reason: 'unreachable',
      detail:
        'failed to enumerate group metrics.k8s.io: the APIService v1beta1.metrics.k8s.io is not available ' +
        '(FailedDiscoveryCheck)',
    },
  ],
};

/* ── §4 a listing inside the explorer ───────────────────────────────────── */

export const CERTIFICATES = {
  items: [
    {
      apiVersion: 'cert-manager.io/v1', kind: 'Certificate',
      metadata: { name: 'storefront-tls', namespace: 'prod', creationTimestamp: '2026-03-02T10:00:00Z', resourceVersion: '771204' },
      spec: { secretName: 'storefront-tls', dnsNames: ['shop.example', 'www.shop.example'], issuerRef: { name: 'letsencrypt', kind: 'ClusterIssuer' } },
      status: { conditions: [{ type: 'Ready', status: 'True' }], notAfter: '2026-10-19T09:00:00Z' },
    },
    {
      apiVersion: 'cert-manager.io/v1', kind: 'Certificate',
      metadata: { name: 'api-tls', namespace: 'prod', creationTimestamp: '2026-03-02T10:01:00Z', resourceVersion: '771208' },
      spec: { secretName: 'api-tls', dnsNames: ['api.example'], issuerRef: { name: 'letsencrypt', kind: 'ClusterIssuer' } },
      status: { conditions: [{ type: 'Ready', status: 'True' }], notAfter: '2026-11-01T09:00:00Z' },
    },
    {
      apiVersion: 'cert-manager.io/v1', kind: 'Certificate',
      metadata: { name: 'internal-tls', namespace: 'staging', creationTimestamp: '2026-06-11T08:00:00Z', resourceVersion: '803991' },
      spec: { secretName: 'internal-tls', dnsNames: ['internal.staging.example'], issuerRef: { name: 'internal-ca', kind: 'ClusterIssuer' } },
      status: { conditions: [{ type: 'Ready', status: 'False', reason: 'DoesNotExist' }] },
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/* ── §10 the audit trail ────────────────────────────────────────────────── */

const target = (resource, name, over = {}) => ({
  group: 'apps', version: 'v1', resource, namespace: 'prod', name, subresource: null, ...over,
});

export const AUDIT = {
  items: [
    {
      id: 8841, ts: '2026-08-20T09:44:12Z', category: 'cluster', actor: 'erens',
      cluster_id: 1, cluster_name: 'prod-eu', verb: 'patch',
      target: target('deployments', 'checkout', { subresource: 'scale' }),
      dry_run: false, outcome: 'applied', detail: 'replicas 5 -> 8',
      diff_digest: 'sha256:9f2c1188aa', error: null, source_ip: '10.4.2.9',
      prev_hash: 'd'.repeat(64), event_hash: 'e'.repeat(64),
    },
    {
      // The dry run that preceded it. Both are in the trail: the projection was
      // a real request to the cluster, and it is what the operator approved.
      id: 8840, ts: '2026-08-20T09:44:01Z', category: 'cluster', actor: 'erens',
      cluster_id: 1, cluster_name: 'prod-eu', verb: 'patch',
      target: target('deployments', 'checkout', { subresource: 'scale' }),
      dry_run: true, outcome: 'dry_run', detail: 'replicas 5 -> 8',
      diff_digest: 'sha256:9f2c1188aa', error: null, source_ip: '10.4.2.9',
      prev_hash: 'c'.repeat(64), event_hash: 'd'.repeat(64),
    },
    {
      // A refusal, in the trail. A trail of only the writes that worked cannot
      // answer "who tried".
      id: 8839, ts: '2026-08-20T09:31:55Z', category: 'cluster', actor: 'contractor.j',
      cluster_id: 1, cluster_name: 'prod-eu', verb: 'delete',
      target: target('deployments', 'payments'),
      dry_run: false, outcome: 'denied',
      detail: 'preflight refused: no RBAC policy matched',
      diff_digest: null, error: 'rbac_denied', source_ip: '10.4.9.61',
      prev_hash: 'b'.repeat(64), event_hash: 'c'.repeat(64),
    },
    {
      id: 8838, ts: '2026-08-20T09:12:40Z', category: 'cluster', actor: 'ops.katya',
      cluster_id: 1, cluster_name: 'prod-eu', verb: 'replace',
      target: target('statefulsets', 'sessions'),
      dry_run: false, outcome: 'conflict',
      detail: 'resourceVersion 884101 is stale; live is 884213',
      diff_digest: null, error: 'conflict', source_ip: '10.4.2.44',
      prev_hash: 'a'.repeat(64), event_hash: 'b'.repeat(64),
    },
    {
      id: 8837, ts: '2026-08-20T08:58:02Z', category: 'cluster', actor: 'ops.katya',
      cluster_id: 1, cluster_name: 'prod-eu', verb: 'drain',
      target: { group: '', version: 'v1', resource: 'nodes', namespace: null, name: 'ip-10-0-2-11', subresource: null },
      dry_run: false, outcome: 'failed',
      detail: 'cordoned; 3 of 41 pods could not be evicted',
      diff_digest: 'sha256:41b0c7e2fd', error: 'upstream_error', source_ip: '10.4.2.44',
      prev_hash: '9'.repeat(64), event_hash: 'a'.repeat(64),
    },
    {
      // Sign-ins live in the same trail, because "who tried" is asked about the
      // console as often as about a cluster. `cluster_id: null` is why the
      // scope filter needs its third position.
      id: 8836, ts: '2026-08-20T08:41:19Z', category: 'console', actor: 'someone-guessing',
      cluster_id: null, cluster_name: null, verb: 'login',
      target: { group: 'k8boss-admin.io', version: 'v1', resource: 'sessions', namespace: null, name: 'someone-guessing', subresource: null },
      dry_run: false, outcome: 'denied', detail: 'Sign-in rejected (auto).',
      diff_digest: null, error: null, source_ip: '198.51.100.23',
      prev_hash: '8'.repeat(64), event_hash: '9'.repeat(64),
    },
    {
      id: 8835, ts: '2026-08-20T08:40:03Z', category: 'console', actor: 'erens',
      cluster_id: null, cluster_name: null, verb: 'login',
      target: { group: 'k8boss-admin.io', version: 'v1', resource: 'sessions', namespace: null, name: 'erens', subresource: null },
      dry_run: false, outcome: 'applied', detail: 'Signed in via oidc.',
      diff_digest: null, error: null, source_ip: '10.4.2.9',
      prev_hash: '7'.repeat(64), event_hash: '8'.repeat(64),
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/**
 * §10.2's interesting verdict: `partial`, not `intact`.
 *
 * 12 records predate the hash chain and are never back-filled — hashing them
 * now would attest whatever they say today, turning "we do not know" into
 * "verified". The console counts them and withholds the verdict. A UI that
 * renders this as a pass is the defect.
 */
export const AUDIT_VERIFY = {
  status: 'partial',
  verified: 8829,
  unchained: 12,
  total: 8841,
  anchored: true,
  first_break: null,
  tip: 'e'.repeat(64),
  genesis: '0'.repeat(64),
  window: { requested_limit: null, oldest_unchained_id: 1 },
};

/* ── §7 events ──────────────────────────────────────────────────────────── */

export const EVENTS = {
  items: [
    { namespace: 'prod', type: 'Warning', reason: 'BackOff', message: 'Back-off restarting failed container app in pod payments-5c8d9f7b6-qq4mn', count: 128, first_seen: '2026-08-20T06:44:00Z', last_seen: '2026-08-20T09:47:10Z', involved: { kind: 'Pod', name: 'payments-5c8d9f7b6-qq4mn', namespace: 'prod', uid: 'e1' }, source: 'kubelet, ip-10-0-1-5' },
    { namespace: 'prod', type: 'Warning', reason: 'Unhealthy', message: 'Readiness probe failed: HTTP probe failed with statuscode: 503', count: 61, first_seen: '2026-08-20T07:02:00Z', last_seen: '2026-08-20T09:46:02Z', involved: { kind: 'Pod', name: 'checkout-7d9f8b6c4-hk2xv', namespace: 'prod', uid: 'e2' }, source: 'kubelet, ip-10-0-1-4' },
    { namespace: 'prod', type: 'Warning', reason: 'FailedScheduling', message: '0/12 nodes are available: 1 node(s) were unschedulable, 8 Insufficient cpu, 3 node(s) had untolerated taint.', count: 9, first_seen: '2026-08-20T09:30:00Z', last_seen: '2026-08-20T09:44:00Z', involved: { kind: 'Pod', name: 'checkout-7d9f8b6c4-p9k2m', namespace: 'prod', uid: 'e3' }, source: 'default-scheduler' },
    { namespace: 'monitoring', type: 'Warning', reason: 'Failed', message: 'Failed to pull image "ghcr.io/acme/log-shipper:0.7.4": manifest unknown', count: 14, first_seen: '2026-08-20T07:31:00Z', last_seen: '2026-08-20T09:40:00Z', involved: { kind: 'Pod', name: 'log-shipper-nx7c4', namespace: 'monitoring', uid: 'e4' }, source: 'kubelet, ip-10-0-2-11' },
    { namespace: 'prod', type: 'Normal', reason: 'ScalingReplicaSet', message: 'Scaled up replica set checkout-7d9f8b6c4 to 8 from 5', count: 1, first_seen: '2026-08-20T09:44:12Z', last_seen: '2026-08-20T09:44:12Z', involved: { kind: 'Deployment', name: 'checkout', namespace: 'prod', uid: 'e5' }, source: 'deployment-controller' },
    { namespace: 'prod', type: 'Normal', reason: 'SuccessfulCreate', message: 'Created pod: checkout-7d9f8b6c4-p9k2m', count: 1, first_seen: '2026-08-20T09:30:00Z', last_seen: '2026-08-20T09:30:00Z', involved: { kind: 'ReplicaSet', name: 'checkout-7d9f8b6c4', namespace: 'prod', uid: 'e6' }, source: 'replicaset-controller' },
    { namespace: 'prod', type: 'Normal', reason: 'Completed', message: 'Job completed', count: 1, first_seen: '2026-08-19T14:11:00Z', last_seen: '2026-08-19T14:11:00Z', involved: { kind: 'Job', name: 'migrate-2026-08-19', namespace: 'prod', uid: 'e7' }, source: 'job-controller' },
    { namespace: 'prod', type: 'Warning', reason: 'NodeNotReady', message: 'Node ip-10-0-3-7 status is now: NodeNotReady', count: 1, first_seen: '2026-08-20T08:12:00Z', last_seen: '2026-08-20T08:12:00Z', involved: { kind: 'Node', name: 'ip-10-0-3-7', namespace: null, uid: 'e8' }, source: 'node-controller' },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/* ── §12 console users ──────────────────────────────────────────────────── */

export const USERS = {
  items: [
    { id: 1, username: 'erens', display_name: 'Eren S.', email: 'erens@example.test', role: 'admin', auth_source: 'oidc', active: true, last_login: '2026-08-20T08:40:03Z', created_at: '2026-02-11T10:00:00Z', updated_at: '2026-08-20T08:40:03Z' },
    { id: 4, username: 'ops.katya', display_name: 'Katya P.', email: 'katya@example.test', role: 'operator', auth_source: 'ldap', active: true, last_login: '2026-08-20T08:12:44Z', created_at: '2026-03-01T09:00:00Z', updated_at: '2026-08-20T08:12:44Z' },
    { id: 9, username: 'contractor.j', display_name: 'J. Contractor', email: 'j@partner.test', role: 'viewer', auth_source: 'local', active: true, last_login: '2026-08-20T09:29:10Z', created_at: '2026-08-01T09:00:00Z', updated_at: '2026-08-20T09:29:10Z' },
    { id: 12, username: 'former.staff', display_name: 'Former Staff', email: 'former@example.test', role: 'operator', auth_source: 'ldap', active: false, last_login: '2026-06-30T16:02:00Z', created_at: '2026-02-14T09:00:00Z', updated_at: '2026-07-01T09:00:00Z' },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/* ── §1.5 the diff the write demo is shot on ────────────────────────────── */

/**
 * The unified diff for `checkout` 5 → 8 replicas, exactly as `build_diff`
 * produces it: normalised YAML on both sides, server bookkeeping removed, and
 * `--- live` / `+++ projected` headers with no timestamps.
 */
export const SCALE_DIFF_UNIFIED = [
  '--- live',
  '+++ projected (dryRun=All)',
  '@@ -1,14 +1,14 @@',
  ' apiVersion: apps/v1',
  ' kind: Deployment',
  ' metadata:',
  '   name: checkout',
  '   namespace: prod',
  '   labels:',
  '     app: checkout',
  '     app.kubernetes.io/part-of: storefront',
  ' spec:',
  '-  replicas: 5',
  '+  replicas: 8',
  '   selector:',
  '     matchLabels:',
  '       app: checkout',
  '   strategy:',
  '@@ -22,7 +22,7 @@',
  '       containers:',
  '         - name: app',
  '           image: ghcr.io/acme/checkout:1.9.2',
  '           ports:',
  '             - containerPort: 8080',
  '           resources:',
  '             requests:',
  '               cpu: 250m',
  '               memory: 512Mi',
  '',
].join('\n');

export const SCALE_BEFORE = [
  'apiVersion: apps/v1',
  'kind: Deployment',
  'metadata:',
  '  name: checkout',
  '  namespace: prod',
  '  labels:',
  '    app: checkout',
  '    app.kubernetes.io/part-of: storefront',
  'spec:',
  '  replicas: 5',
  '  selector:',
  '    matchLabels:',
  '      app: checkout',
  '',
].join('\n');

export const SCALE_AFTER = SCALE_BEFORE.replace('replicas: 5', 'replicas: 8');

/** The §1.5 envelope for a dry run: a full projection, and `applied: false`. */
export const SCALE_DRY_RUN = {
  dryRun: true,
  applied: false,
  verb: 'patch',
  target: { group: 'apps', version: 'v1', resource: 'deployments', namespace: 'prod', name: 'checkout', subresource: 'scale' },
  diff: { before: SCALE_BEFORE, after: SCALE_AFTER, unified: SCALE_DIFF_UNIFIED, changed: true },
  resourceVersion: '884213',
  warnings: [],
  auditId: 8840,
};

/** The confirming call. `applied: true` appears here and nowhere else. */
export const SCALE_APPLIED = {
  ...SCALE_DRY_RUN,
  dryRun: false,
  applied: true,
  resourceVersion: '884219',
  auditId: 8841,
};

/* ── the drain plan ─────────────────────────────────────────────────────── */

const planned = (namespace, podName, action, reason, controller) => ({
  namespace, pod: podName, action, reason, controller, result: null, error: null, error_code: null,
});

export const DRAIN_PLAN = [
  planned('prod', 'checkout-7d9f8b6c4-hk2xv', 'evict', '', { kind: 'ReplicaSet', name: 'checkout-7d9f8b6c4' }),
  planned('prod', 'sessions-0', 'evict', '', { kind: 'StatefulSet', name: 'sessions' }),
  planned('prod', 'web-6f4b8c9d5-9d2ck', 'evict', '', { kind: 'ReplicaSet', name: 'web-6f4b8c9d5' }),
  planned('monitoring', 'node-exporter-7klm2', 'skip', 'DaemonSet pod: ignoreDaemonSets is set, so it is left in place.', { kind: 'DaemonSet', name: 'node-exporter' }),
  planned('kube-system', 'kube-proxy-4vv8t', 'skip', 'DaemonSet pod: ignoreDaemonSets is set, so it is left in place.', { kind: 'DaemonSet', name: 'kube-proxy' }),
  // The two that stop the drain. A console that reported "drained" over these
  // is the sentence that gets a machine terminated with a database on it.
  planned('prod', 'pg-primary-0', 'blocked', 'A PodDisruptionBudget allows 0 more disruptions (2 healthy, 2 required).', { kind: 'StatefulSet', name: 'pg-primary' }),
  planned('analytics', 'adhoc-query-shell', 'blocked', 'No owning controller, so nothing would recreate it elsewhere.', null),
];

/** §4's read of one object's manifest — `text/plain`, with a resourceVersion. */
export function objectYaml({ kind = 'Deployment', name = 'checkout', namespace = 'prod', resourceVersion = '884213' } = {}) {
  return [
    'apiVersion: apps/v1',
    `kind: ${kind}`,
    'metadata:',
    `  name: ${name}`,
    `  namespace: ${namespace}`,
    `  resourceVersion: "${resourceVersion}"`,
    '  labels:',
    '    app: checkout',
    '    app.kubernetes.io/part-of: storefront',
    '  annotations:',
    '    deployment.kubernetes.io/revision: "14"',
    'spec:',
    '  replicas: 5',
    '  revisionHistoryLimit: 10',
    '  selector:',
    '    matchLabels:',
    '      app: checkout',
    '  strategy:',
    '    type: RollingUpdate',
    '    rollingUpdate:',
    '      maxSurge: 25%',
    '      maxUnavailable: 0',
    '  template:',
    '    metadata:',
    '      labels:',
    '        app: checkout',
    '    spec:',
    '      serviceAccountName: checkout',
    '      containers:',
    '        - name: app',
    '          image: ghcr.io/acme/checkout:1.9.2',
    '          ports:',
    '            - containerPort: 8080',
    '          resources:',
    '            requests:',
    '              cpu: 250m',
    '              memory: 512Mi',
    '          readinessProbe:',
    '            httpGet:',
    '              path: /healthz',
    '              port: 8080',
    '',
  ].join('\n');
}
