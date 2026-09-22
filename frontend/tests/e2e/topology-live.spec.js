import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * The topology canvas re-reads on its own, and a manual view survives it.
 *
 * The canvas has the same staleness the workload pages had — its nodes carry
 * ready-over-desired, and a scale confirmed in its drawer is reconciled by a
 * controller over the following seconds rather than in the round trip that
 * accepted it — but it also has something they do not: a view the operator set
 * by hand. A page that polled by re-fitting the drawing would answer the first
 * complaint by creating a worse one, snapping the canvas back to fit every
 * thirty seconds while somebody is reading a corner of it.
 *
 * It does not, because zoom and pan are `view` state and nothing in the read
 * path writes to it — but "nothing writes to it" is exactly the kind of claim
 * that stays true until someone adds a `setView` to a data effect. So both
 * halves are asserted here off one poll: the counts changed, and the rectangle
 * did not.
 *
 * The second test is the interval itself. `SURVEY_POLL_MS` is deliberately not
 * `LIVE_POLL_MS`: a canvas re-reads a whole namespace, the drawer re-reads one
 * workload, and a test that let them drift to the same number would let the
 * distinction be deleted by accident.
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

/** One Deployment, so the graph's size — and so the fitted view — is fixed. */
const workloadsAt = (ready, desired) => ({
  items: [
    {
      kind: 'Deployment',
      name: 'checkout',
      namespace: 'prod',
      replicas: { desired, ready, updated: desired, available: ready },
      images: ['ghcr.io/acme/checkout:1.9.2'],
      selector: { app: 'checkout' },
      labels: { app: 'checkout' },
      age_seconds: 8600,
      status: ready === desired ? 'Healthy' : 'Progressing',
      status_reason: null,
      restarts_24h: 0,
      suspended: null,
      schedule: null,
      last_schedule: null,
    },
  ],
  partial: false,
  unavailable: [],
});

/**
 * The §6 listing, answered differently after the first read — a controller that
 * has caught up. Matched by regex rather than a glob so it cannot also take
 * `/workloads/deployments/...` or the SPA's own navigation to `/topology`.
 */
function reconcilingListing(page) {
  const reads = { count: 0 };
  page.route(/\/api\/workloads(\?|$)/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    reads.count += 1;
    // Only the counts move. Adding a node would change the graph's size, and
    // then a changed viewBox would be the drawing growing rather than the bug
    // this test is watching for.
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(reads.count > 1 ? workloadsAt(5, 5) : workloadsAt(3, 5)),
    });
  });
  return reads;
}

const readViewBox = async (page) => page.getByTestId('topology-canvas').getAttribute('viewBox');

test('the canvas catches up without a reload, and leaves a manual zoom alone', async ({ page }) => {
  // One real `SURVEY_POLL_MS` has to elapse, which is longer than the default.
  test.setTimeout(90_000);

  await mockApi(page, { preflight: ALLOW_ALL });
  const reads = reconcilingListing(page);

  await page.goto('/topology');
  await expectPageRendered(page, 'Topology');
  await expect(page.getByTestId('topology-canvas')).toBeVisible();

  const count = page.getByTestId('topology-node-count');
  await expect(count).toHaveText('3/5');

  // A view the operator chose, before the poll fires.
  await page.getByTestId('topology-zoom-in').click();
  const chosen = await readViewBox(page);
  await expect(page.getByTestId('topology-reset-view')).toBeEnabled();

  // Nothing clicked from here on.
  await expect(count).toHaveText('5/5', { timeout: 60_000 });
  expect(reads.count).toBeGreaterThan(1);
  // The rectangle the operator chose, to the character. New rows re-draw
  // inside it; they do not pull the canvas back to fit.
  expect(await readViewBox(page)).toBe(chosen);
});

test('the canvas and the drawer poll at their own intervals, not one shared one', async ({ page }) => {
  await mockApi(page, { workloads: workloadsAt(3, 5), preflight: ALLOW_ALL });

  let listings = 0;
  let details = 0;
  await page.route(/\/api\/workloads(\?|$)/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    listings += 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(workloadsAt(3, 5)),
    });
  });
  await page.route(/\/api\/workloads\/deployments\/prod\/checkout(\?|$)/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    details += 1;
    return route.fallback();
  });

  await page.goto('/topology');
  await expectPageRendered(page, 'Topology');
  await page.getByTestId('topology-node').click();
  await expect(page.getByTestId('topology-panel-title')).toContainText('checkout');

  // Inside one survey interval the drawer has re-read and the canvas has not:
  // the ten-second read is the one open on a single object somebody just
  // changed, and the thirty-second one is the survey of a namespace behind it.
  await expect.poll(() => details, { timeout: 25_000 }).toBeGreaterThan(1);
  expect(listings).toBe(1);
});
