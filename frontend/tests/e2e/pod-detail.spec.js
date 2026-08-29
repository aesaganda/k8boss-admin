import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * The pod page (§7.5–§7.7) — one pod, behind tabs.
 *
 * What is asserted here is not "the tabs render". It is the set of things a
 * page like this normally gets wrong, each of which would put a confident wrong
 * answer in front of an operator:
 *
 * **The tab is in the URL.** Every tab is a link, so an operator can send a
 * colleague the exact panel — and the pod table's row menu opens the panel the
 * operator asked for rather than dropping them on Details to find it.
 *
 * **Only the visible tab fetches.** Two of these tabs open a websocket. A
 * Terminal that connected while somebody was reading the YAML would open an
 * audited exec session behind a tab they never clicked.
 *
 * **A container that declares no resources says so.** `cpu 0` describes a
 * BestEffort container as one that asked for nothing and got it, when what
 * actually happens is that it is evicted first.
 *
 * **`Unknown` is not `False`.** A condition the kubelet has said nothing about
 * is what a node that stopped reporting looks like.
 *
 * **A pod with no metrics is not a pod at zero.** A cluster with no
 * metrics-server is an ordinary fact (§1.2's `unsupported`, rendered calmly),
 * and a bar at 0% would say the pod is idle — which is what gets it turned off.
 *
 * **Secret values never appear, and the four kinds of blank stay apart.**
 * "withheld", "could not be read", "set by the kubelet" and "empty" are four
 * different facts, and rendering them as one empty cell is this project's
 * defect standard applied to an environment viewer.
 */

/** Answer every §9 check as allowed, so the gates are not what is under test. */
const ALLOW_ALL = (checks) =>
  checks.map((check) => ({
    verb: check.verb,
    group: check.group,
    resource: check.resource,
    namespace: check.namespace ?? null,
    subresource: check.subresource ?? null,
    allowed: true,
    reason: '',
    evaluationError: null,
    hint: null,
  }));

/** Deny one check by id, allow the rest. */
const denying = (verb, subresource) => (checks) =>
  checks.map((check) => ({
    verb: check.verb,
    group: check.group,
    resource: check.resource,
    namespace: check.namespace ?? null,
    subresource: check.subresource ?? null,
    allowed: !(check.verb === verb && (check.subresource ?? null) === subresource),
    reason:
      check.verb === verb && (check.subresource ?? null) === subresource
        ? `${verb} ${check.resource}${subresource ? `/${subresource}` : ''} is forbidden for the console ServiceAccount.`
        : '',
    evaluationError: null,
    hint: null,
  }));

const POD_URL = '/pods/prod/checkout-7d9f8b6c4-hk2xv';

async function openPod(page, tab) {
  await page.goto(tab ? `${POD_URL}?tab=${tab}` : POD_URL);
  await expectPageRendered(page, 'checkout-7d9f8b6c4-hk2xv');
}

test.describe('reaching the pod page', () => {
  test('clicking a pod in the table opens its page, not a dialog', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');

    // The QoS cell rather than the name: the name is a link, and this is the
    // row-click path.
    await page.getByRole('gridcell', { name: 'Burstable', exact: true }).first().click();

    await expect(page).toHaveURL(new RegExp(`${POD_URL}\\?tab=details$`));
    await expectPageRendered(page, 'checkout-7d9f8b6c4-hk2xv');
  });

  test('the row menu opens the panel the operator asked for', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');

    await page.getByRole('button', { name: 'Kebab toggle' }).first().click();
    await page.getByRole('menuitem', { name: 'View environment' }).click();

    await expect(page).toHaveURL(new RegExp(`${POD_URL}\\?tab=environment$`));
    await expect(page.getByRole('grid', { name: 'Environment variables' })).toBeVisible();
  });

  test('the pod name links to the pod page from every table that lists one', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');

    await page.getByRole('link', { name: 'checkout-7d9f8b6c4-hk2xv' }).click();

    // With or without a `?tab=`: the row's own click handler also fires, and
    // which of the two wins is not what this test is about — landing on the
    // pod's page rather than in the generic explorer is.
    await expect(page).toHaveURL(new RegExp(`${POD_URL}(\\?|$)`));
  });

  test('an unrecognised tab falls back to Details rather than to nothing', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto(`${POD_URL}?tab=not-a-tab`);
    await expectPageRendered(page, 'checkout-7d9f8b6c4-hk2xv');

    await expect(page.getByTestId('pod-tab-details')).toBeVisible();
  });

  test('only the visible tab fetches', async ({ page }) => {
    const asked = [];
    await mockApi(page, { preflight: ALLOW_ALL });
    // Registered after mockApi so it observes rather than answers.
    await page.route('**/api/**', async (route) => {
      asked.push(new URL(route.request().url()).pathname);
      await route.fallback();
    });

    await openPod(page, 'details');
    await expect(page.getByTestId('pod-tab-details')).toBeVisible();

    // Neither the environment nor the metrics has been read, and no websocket
    // has been opened — an exec session behind a tab nobody clicked is an
    // audited, privileged thing happening without an operator asking for it.
    expect(asked.some((path) => path.endsWith('/environment'))).toBe(false);
    expect(asked.some((path) => path.endsWith('/metrics'))).toBe(false);

    await page.getByRole('tab', { name: 'Metrics', exact: true }).click();
    await expect(page.getByTestId('pod-metrics-window')).toBeVisible();
    expect(asked.some((path) => path.endsWith('/metrics'))).toBe(true);
  });
});

test.describe('the Details tab', () => {
  test('shows what the table has no room for, including why a container restarted', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'details');

    await expect(page.getByText('checkout', { exact: true }).first()).toBeVisible();
    // The whole reason §7.5 exists: "restarted 3 times" is the symptom and
    // this is the answer.
    await expect(page.getByRole('grid', { name: 'Containers', exact: true })).toContainText('OOMKilled');
    await expect(page.getByRole('grid', { name: 'Containers', exact: true })).toContainText('cpu 250m');
  });

  test('a container that declares no resources is a named absence, never a zero', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'details');

    const envoyRow = page.getByRole('row', { name: /envoy/ });
    await expect(envoyRow).not.toContainText('cpu 0');
    // The em dash the whole console uses for "not a zero", with its reason on
    // hover — see `NullableCell`.
    await expect(envoyRow.locator('[data-nullable="true"]').first()).toBeVisible();
  });

  test('an init container is listed apart from the app containers', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'details');

    // Merged into one table, a Terminated init container reads as a Terminated
    // app container — a red row on every healthy pod that has run a migration.
    await expect(page.getByRole('grid', { name: 'Containers', exact: true })).not.toContainText('migrate');
    await expect(page.getByRole('grid', { name: 'Init containers' })).toContainText('migrate');
  });

  test('an Unknown condition is not rendered as a False one', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'details');

    const conditions = page.getByRole('grid', { name: 'Pod conditions' });
    await expect(conditions).toContainText('Unknown');
    await expect(conditions).toContainText('kubelet stopped posting node status');
  });

  test('a crash-looping pod does not get a green pill on its own page', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      podDetail: { ...FIXTURES.podDetail, phase: 'Running', phase_detail: 'CrashLoopBackOff' },
    });
    await openPod(page, 'details');

    // §6: `phase_detail` wins over `phase`. This is the page somebody opens to
    // find out what is wrong, and "Running" at the top of it is the lie.
    await expect(page.getByTestId('status-badge').first()).toHaveText('CrashLoopBackOff');
  });
});

test.describe('the Metrics tab', () => {
  test('shows usage against what each container asked for', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'metrics');

    await expect(page.getByTestId('pod-metrics-window')).toContainText('over 30s');
    const usage = page.getByRole('grid', { name: 'Container usage' });
    await expect(usage).toContainText('120m');
    await expect(usage).toContainText('250m');
  });

  test('a container missing from the sample leaves the pod total unknown, not understated', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'metrics');

    // The fixture samples `app` and not `envoy`. Summing what we have and
    // calling it the pod's usage understates it by however much `envoy` is
    // using, with nothing on screen to say a container is missing.
    const cpuTile = page.getByTestId('metric-card').filter({ hasText: 'Pod CPU' });
    await expect(cpuTile.locator('[data-nullable="true"]')).toBeVisible();
    await expect(page.getByTestId('metric-card').filter({ hasText: 'Containers' })).toContainText(
      '1 with a live sample',
    );
  });

  test('a finished init container is not reported as an unmeasured one', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'metrics');

    // Both dashes look the same; the reasons must not. metrics-server does not
    // report a terminated init container, and calling that a gap would put
    // "we could not measure this" on every pod that has ever run a migration —
    // which is how a banner stops being read.
    const migrate = page.getByRole('row', { name: /migrate/ });
    await expect(migrate.locator('[data-nullable="true"]').first()).toHaveAttribute(
      'aria-label',
      /nothing running to measure/,
    );
    const envoy = page.getByRole('row', { name: /envoy/ });
    await expect(envoy.locator('[data-nullable="true"]').first()).toHaveAttribute(
      'aria-label',
      /no sample/,
    );
  });

  test('a cluster with no metrics-server is a calm sentence and never a zero', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, podMetrics: FIXTURES.podMetricsUnsupported });
    await openPod(page, 'metrics');

    // §1.2's `unsupported` — "not present on this cluster", named in the
    // persistent banner rule 11.1 requires.
    await expect(page.getByText('metrics.k8s.io')).toBeVisible();
    const usage = page.getByRole('grid', { name: 'Container usage' });
    await expect(usage.locator('[data-nullable="true"]').first()).toBeVisible();
    // The requests still render: they come from the pod, not from the sample,
    // and "what did this container ask for" is still an answer.
    await expect(usage).toContainText('250m');
  });
});

test.describe('the Environment tab', () => {
  test('never shows a Secret value, and says why the cell is blank', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'environment');

    const grid = page.getByRole('grid', { name: 'Environment variables' });
    await expect(grid).toContainText('DB_PASSWORD');
    await expect(grid).toContainText('Secret value');
    await expect(grid).toContainText('Secret checkout-db · password');
    // The tab states the promise where an operator will read it, rather than
    // leaving them to infer it from an empty cell.
    await expect(page.getByText('Secret values are never shown here')).toBeVisible();
  });

  test('the four kinds of blank are four different things on screen', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'environment');

    const grid = page.getByRole('grid', { name: 'Environment variables' });
    await expect(grid).toContainText('Secret value');
    // Not "empty": the ConfigMap was refused, and the variable is not known to
    // be unset.
    await expect(grid).toContainText('Could not be read');
    // Different again: the ConfigMap was read and has no such key, so unless
    // the reference is optional the container will not start.
    await expect(grid).toContainText('Key not present');
    await expect(grid).toContainText('Set by the kubelet');
    // And an actual value, so the four above are distinguishable from one.
    await expect(grid).toContainText('info');
  });

  test('an unreadable wholesale import says a set of variables is missing', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'environment');

    // Reporting zero variables from an object we could not read is the
    // empty-is-never-blind failure, hidden inside a container's environment.
    await expect(page.getByText('an unknown set of variables')).toBeVisible();
    // …and rule 11.1's banner names what failed.
    await expect(page.getByText('configmaps', { exact: false }).first()).toBeVisible();
  });

  test('an overridden variable is shown, struck through, rather than dropped', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPod(page, 'environment');

    await expect(page.locator('.admin-env__name--overridden')).toHaveCount(1);
    await expect(page.getByText('overridden', { exact: true })).toBeVisible();
  });
});

test.describe('the tabs that reach inside the pod', () => {
  test('a denied exec disables the Terminal with the reason, rather than hiding it', async ({
    page,
  }) => {
    await mockApi(page, { preflight: denying('create', 'exec') });
    await openPod(page, 'terminal');

    // Rule 11.4 for a panel: the reason is shown. A missing tab would read as a
    // console that cannot do this at all.
    await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toBeVisible();
    await expect(page.getByText('A terminal cannot be opened for this pod')).toBeVisible();
    await expect(page.getByText('create pods/exec is forbidden')).toBeVisible();
  });

  test('a denied log read says so where the log output would be', async ({ page }) => {
    await mockApi(page, { preflight: denying('get', 'log') });
    await openPod(page, 'logs');

    await expect(page.getByText('Logs cannot be read for this pod')).toBeVisible();
  });

  test('the Events tab asks the API server for this pod only', async ({ page }) => {
    const urls = [];
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.route('**/api/events**', async (route) => {
      urls.push(route.request().url());
      await route.fallback();
    });

    // Waited for rather than assumed to have happened by the time the grid is
    // visible: `DataTable` renders its grid while loading, so a bare
    // `queries.length` check races the fetch it is about.
    const read = page.waitForRequest((request) => request.url().includes('/api/events'));
    await openPod(page, 'events');
    await read;
    await expect(page.getByRole('grid', { name: 'Pod events' })).toBeVisible();

    // Server-side, not a client-side pass over the namespace: §5 scans a
    // bounded window, so filtering afterwards would mean "the events about this
    // pod among the newest 200 in the namespace" — usually none, with nothing
    // on screen to say so. Asserted over *every* events read this page makes,
    // because one unfiltered read is exactly what a client-side filter looks
    // like from here.
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) {
      const query = new URL(url).searchParams;
      expect(query.get('involvedObjectKind'), url).toBe('Pod');
      expect(query.get('involvedObjectName'), url).toBe('checkout-7d9f8b6c4-hk2xv');
      expect(query.get('namespace'), url).toBe('prod');
    }
  });
});
