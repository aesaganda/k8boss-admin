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
    deployment: { present: false, version: null, desiredReplicas: null, readyReplicas: null, image: null, detail: null },
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
    deployment: { present: false, version: null, desiredReplicas: null, readyReplicas: null, image: null, detail: null },
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

  emptyList: { items: [], continue: null, remaining: null, partial: false, unavailable: [] },
};

/** Answer every /api call from the fixtures above. */
export async function mockApi(
  page,
  {
    health = FIXTURES.health,
    auth = null,
    audit = null,
    chain = null,
    yaml = null,
    workloads = null,
    pods = null,
    debug = null,
    debugAttach = null,
    preflight = null,
    nodeDetail = null,
    nodeDebug = null,
    nodeDebugCreate = null,
    nodeDebugDeletes = [],
    routeCapabilities = null,
    routes = null,
    routeRender = null,
    routeWrite = null,
    routeDetail = null,
    routerStatus = null,
    routerInstall = null,
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
    if (path === '/resources/core/v1/pods') return json(pods ?? FIXTURES.pods);
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
                objects: [],
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
