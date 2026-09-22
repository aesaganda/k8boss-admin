import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * The workload page re-reads on its own.
 *
 * A scale is two events, not one: the API server accepts the new count in the
 * round trip the dialog makes, and the controller reconciles it over the next
 * several. The re-read fired on `onApplied` lands inside that gap, so the page
 * it produces is the count just set beside the pods that existed before it —
 * and with nothing reading again, that stayed on screen until an operator
 * pressed reload. The ring said 3 of 3 while the cluster was building the
 * fourth.
 *
 * So what is asserted here is the absence of an interaction: nothing is clicked
 * between the two expectations. The first is the answer the cluster gave on
 * load, the second is a later one, and the only thing that can carry the page
 * from one to the other is `useAsync`'s poll.
 */

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

const isDetail = (route) => new URL(route.request().url()).pathname.endsWith('/checkout');

/**
 * §6's detail, answered differently after the first read — a controller that
 * has caught up. Registered after `mockApi` so it takes the route: Playwright
 * matches the most recently added handler first.
 */
function reconcilingDetail(page) {
  const reads = { count: 0 };
  page.route('**/api/workloads/deployments/prod/checkout**', async (route) => {
    // The glob's trailing `**` also catches `/rollout`, which is a different
    // question with a different answer. Only the detail is answered here.
    if (route.request().method() !== 'GET' || !isDetail(route)) return route.fallback();
    reads.count += 1;
    const caughtUp = reads.count > 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...FIXTURES.workloadDetail,
        workload: {
          ...FIXTURES.workloadDetail.workload,
          replicas: caughtUp
            ? { desired: 5, ready: 5, updated: 5, available: 5 }
            : { desired: 5, ready: 4, updated: 5, available: 4 },
          status: caughtUp ? 'Healthy' : 'Progressing',
          status_reason: caughtUp ? null : '1 of 5 replicas not available',
        },
      }),
    });
  });
  return reads;
}

test('the replica ring catches up without a reload', async ({ page }) => {
  await mockApi(page, { preflight: ALLOW_ALL });
  const reads = reconcilingDetail(page);

  await page.goto('/workloads/deployments/prod/checkout');
  await expectPageRendered(page, 'checkout');

  const ring = page.getByTestId('workload-pod-ring');
  await expect(ring).toContainText('of 5');
  await expect(ring.getByRole('img', { name: '4 of 5 pods ready' })).toBeVisible();

  // Nothing clicked, nothing reloaded. Two reads is itself the assertion that
  // the interval fired; the ring is the assertion that the second one reached
  // the screen.
  await expect(ring.getByRole('img', { name: '5 of 5 pods ready' })).toBeVisible({ timeout: 20000 });
  expect(reads.count).toBeGreaterThan(1);
});

test('a failed poll keeps the last good answer on screen', async ({ page }) => {
  await mockApi(page, { preflight: ALLOW_ALL });

  let reads = 0;
  await page.route('**/api/workloads/deployments/prod/checkout**', async (route) => {
    if (route.request().method() !== 'GET' || !isDetail(route)) return route.fallback();
    reads += 1;
    // Every read after the first fails the way a console behind a flaky
    // connection does. A page that blanked to an error panel would lose the
    // rollout the operator opened it to watch — and the cluster is not
    // unreachable, one request was.
    if (reads > 1) return route.abort('failed');
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(FIXTURES.workloadDetail),
    });
  });

  await page.goto('/workloads/deployments/prod/checkout');
  await expectPageRendered(page, 'checkout');
  await expect(page.getByTestId('workload-pod-ring')).toContainText('of 5');

  await expect.poll(() => reads, { timeout: 20000 }).toBeGreaterThan(1);
  await expect(page.getByTestId('workload-pod-ring')).toContainText('of 5');
});
