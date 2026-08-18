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

  emptyList: { items: [], continue: null, remaining: null, partial: false, unavailable: [] },
};

/** Answer every /api call from the fixtures above. */
export async function mockApi(
  page,
  { health = FIXTURES.health, auth = null, audit = null, chain = null } = {},
) {
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
    if (path === '/namespaces') return json(FIXTURES.namespaces);
    if (path === '/nodes') return json(FIXTURES.nodes);
    if (path === '/workloads') return json(FIXTURES.workloads);
    if (path === '/access/preflight') {
      return json(
        route.request().method() === 'POST'
          ? { results: [] }
          : { verb: 'list', group: 'apps', resource: 'deployments', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null },
      );
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
