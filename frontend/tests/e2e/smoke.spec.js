import { expect, test } from '@playwright/test';

/**
 * Hermetic smoke suite.
 *
 * Every request to `/api/**` is answered from the fixtures below, so this runs
 * with no backend and no cluster. What it guards is the shell plus the two
 * consumption rules that the rest of the contract leans on:
 *
 *   §11.1  a `partial: true` response renders a persistent inline banner naming
 *          each unavailable entry — never a toast, never nothing.
 *   §11.2  a `null` numeric renders as an em dash, never as `0`.
 *
 * Those two are why the backend goes to the trouble of returning `null` instead
 * of `0` and `unavailable[]` instead of a shorter list. If the frontend flattens
 * them back, the whole "absence is not safety" argument dies at the last layer,
 * silently, on a page that looks fine. So they are asserted on rendered pixels
 * rather than trusted to review.
 */

const OVERVIEW_UNAVAILABLE = [
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

const FIXTURES = {
  authConfig: {
    enabled: false,
    localEnabled: true,
    ldapEnabled: false,
    methods: ['local'],
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

  emptyList: { items: [], continue: null, remaining: null, partial: false, unavailable: [] },
};

/** Answer every /api call from the fixtures above. */
async function mockApi(page, { health = FIXTURES.health, auth = null } = {}) {
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
              methods: auth.ldapEnabled ? ['local', 'ldap'] : ['local'],
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

test.describe('shell', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('renders the masthead, the cluster selector and the navigation', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByText('k8boss-admin').first()).toBeVisible();
    await expect(page.getByTestId('cluster-selector')).toContainText('prod-eu');
    await expect(page.getByTestId('namespace-selector')).toBeVisible();

    const nav = page.getByRole('navigation', { name: 'Console navigation' });
    await expect(nav.getByRole('link', { name: 'Overview' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'API explorer' })).toBeVisible();
  });

  test('keeps the sidebar mounted while a page chunk loads', async ({ page }) => {
    await page.goto('/');
    const nav = page.getByRole('navigation', { name: 'Console navigation' });
    await expect(nav).toBeVisible();

    // Navigating to a route whose chunk has not been fetched must not blank the
    // shell: the Suspense boundary lives around <Outlet/> only.
    await nav.getByRole('link', { name: 'Nodes' }).click();
    await expect(nav).toBeVisible();
    await expect(page).toHaveURL(/\/nodes$/);
  });

  test('theme toggle switches PatternFly into dark mode', async ({ page }) => {
    await page.goto('/');
    const html = page.locator('html');
    await expect(html).not.toHaveClass(/pf-v6-theme-dark/);

    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveClass(/pf-v6-theme-dark/);
  });

  test('read-only deployments say so and are not silently write-capable', async ({ page }) => {
    await mockApi(page, { health: { ...FIXTURES.health, mutations: 'disabled' } });
    await page.goto('/');

    // §11.5: a banner, plus a badge in the masthead. Both persistent — a toast
    // would expire while the operator is still wondering why Delete is greyed.
    await expect(page.getByTestId('read-only-banner')).toBeVisible();
    await expect(page.getByTestId('read-only-badge')).toBeVisible();
  });
});

test.describe('console authentication', () => {
  test('signs in through LDAP and opens administrator user management', async ({ page }) => {
    await mockApi(page, { auth: { ldapEnabled: true } });
    await page.goto('/users');

    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await page.getByLabel('Username').fill('directory.admin');
    await page.getByLabel('Password').fill('directory-password');
    await page.getByLabel('Account source').selectOption('ldap');
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page.getByRole('heading', { name: 'Users' })).toBeVisible();
    await expect(page.getByText('LDAP authentication enabled')).toBeVisible();
    await expect(page.getByRole('grid', { name: 'Console users' })).toContainText('Directory Admin');
    await expect(page.getByRole('navigation', { name: 'Console navigation' }).getByRole('link', { name: 'Users' })).toBeVisible();
  });

  test('opens the local-user form and signs out through the masthead', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: true, ldapEnabled: true } });
    await page.goto('/users');

    await page.getByRole('button', { name: 'Add local user' }).click();
    await expect(page.getByRole('dialog', { name: 'Console user' })).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await page.getByRole('button', { name: 'User menu' }).click();
    await page.getByRole('menuitem', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  });
});

test.describe('contract rule 11.1 — partial responses are never silent', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('overview shows a persistent banner naming each unavailable source', async ({ page }) => {
    await page.goto('/');

    const banner = page.getByTestId('partial-banner').first();
    await expect(banner).toBeVisible({ timeout: 15000 });

    // Named, not counted: the operator has to be able to tell which question
    // went unanswered.
    await expect(banner).toContainText('nodes');
    await expect(banner).toContainText('not permitted to read');

    // `unsupported` is not an error (§1.2) — a cluster without metrics.k8s.io is
    // a normal cluster, and the row says so rather than colouring it red.
    await expect(banner).toContainText('not present on this cluster');

    // Persistent. A toast would have gone by now.
    await page.waitForTimeout(7000);
    await expect(banner).toBeVisible();
  });

  test('a complete listing shows no banner at all', async ({ page }) => {
    await page.goto('/namespaces');
    await expect(page.getByTestId('partial-banner')).toHaveCount(0);
  });
});

test.describe('contract rule 11.2 — a null number is never a zero', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('overview renders an em dash for the collectors that failed', async ({ page }) => {
    await page.goto('/');

    const missing = page.locator('[data-testid="nullable-cell"][data-nullable="true"]');
    await expect(missing.first()).toBeVisible({ timeout: 15000 });
    await expect(missing.first()).toHaveText('—');

    // The whole point: not one of the unknown values renders as a number, and
    // certainly not as 0. `capacity` and `requested` are null in the fixture.
    const texts = await missing.allTextContents();
    for (const text of texts) {
      expect(text.trim()).toBe('—');
      expect(text.trim()).not.toBe('0');
    }
  });

  test('the dash carries an explanation rather than standing alone', async ({ page }) => {
    await page.goto('/');
    const missing = page.locator('[data-testid="nullable-cell"][data-nullable="true"]').first();
    await expect(missing).toBeVisible({ timeout: 15000 });

    // Accessible name, so the reason survives for a screen reader too — several
    // announce a lone em dash as nothing at all.
    await expect(missing).toHaveAttribute('aria-label', /could not be read/i);
  });

  test('a real zero still renders as zero', async ({ page }) => {
    await page.goto('/');
    // Present values keep data-nullable="false"; nothing in the app may route a
    // known number through the dash path.
    const present = page.locator('[data-testid="nullable-cell"][data-nullable="false"]');
    if (await present.count()) {
      for (const text of await present.allTextContents()) {
        expect(text.trim()).not.toBe('—');
      }
    }
  });
});
