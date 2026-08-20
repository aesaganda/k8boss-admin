import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * The Filter menu and Manage columns — the two controls above every list table.
 *
 * What is worth asserting here is not that a menu opens. It is the handful of
 * rules that make a filter menu honest, each of which is easy to lose and none
 * of which shows up as a broken-looking page when it goes:
 *
 * **A declared value with no rows is offered, at zero.** "Pending 0" says we
 * looked. An option that disappears when it empties makes "no pods are pending"
 * and "this console does not track pending" the same screen.
 *
 * **A value nobody declared is offered too.** A container reason this codebase
 * has never heard of has to be filterable the first time a cluster produces one.
 *
 * **A facet's own selection does not zero its own counts.** Counted the naive
 * way, ticking one status reads "0" against every other one — and the menu then
 * says the cluster holds nothing else.
 *
 * **A filtered table says so, in rows.** Chips for what is set, "Showing 3 of
 * 76" for what it costs, and an empty state that names the filter rather than
 * the cluster.
 *
 * **Hiding a column hides a column, not a row.** And the choice is remembered,
 * where a filter deliberately is not — a filter that came back days later would
 * be rows missing from a table that looks complete.
 */

/**
 * Four pods, chosen so every rule above has something to bite on: two QoS
 * classes, one state that is declared and absent (Pending), and one state
 * (`ImagePullBackOff`) that the page never declared and the menu therefore has
 * to discover.
 */
const PODS = {
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
      containers: [{ name: 'app', image: 'registry.example:5000/checkout:1.4.2', ready: true, restart_count: 0 }],
      age_seconds: 86400,
      creationTimestamp: '2026-08-18T09:00:00Z',
    },
    {
      name: 'payments-5c8d9f7b6-qq4mn',
      namespace: 'prod',
      phase: 'Running',
      phase_detail: 'CrashLoopBackOff',
      ready: '0/1',
      restarts: null,
      node: 'ip-10-0-1-5',
      qos_class: 'Burstable',
      containers: [{ name: 'app', image: 'registry.example:5000/payments:2.0.1', ready: false, restart_count: null }],
      age_seconds: 3600,
      creationTimestamp: '2026-08-19T08:00:00Z',
    },
    {
      name: 'ledger-6b4c7d8f9-t7x2p',
      namespace: 'prod',
      phase: 'Pending',
      // A state nobody enumerated in PHASE_OPTIONS. The menu has to find it.
      phase_detail: 'ImagePullBackOff',
      ready: '0/1',
      restarts: 0,
      node: 'ip-10-0-1-5',
      qos_class: 'Guaranteed',
      containers: [{ name: 'app', image: 'registry.example:5000/ledger:0.9.0', ready: false, restart_count: 0 }],
      age_seconds: 600,
      creationTimestamp: '2026-08-19T15:00:00Z',
    },
    {
      name: 'report-runner-28919',
      namespace: 'batch',
      phase: 'Succeeded',
      phase_detail: null,
      ready: '0/1',
      restarts: 0,
      node: 'ip-10-0-1-4',
      qos_class: 'BestEffort',
      containers: [{ name: 'app', image: 'registry.example:5000/report:3.1.0', ready: false, restart_count: 0 }],
      age_seconds: 7200,
      creationTimestamp: '2026-08-19T13:00:00Z',
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

const grid = (page) => page.getByRole('grid', { name: 'Pods' });
const bodyRows = (page) => grid(page).locator('tbody tr');

async function openPods(page) {
  await mockApi(page, { pods: PODS });
  await page.goto('/pods');
  await expectPageRendered(page, 'Pods');
  await expect(bodyRows(page)).toHaveCount(PODS.items.length);
}

async function openFilterMenu(page) {
  await page.getByTestId('facet-filter-toggle').click();
  await expect(page.locator('.admin-facet-menu')).toBeVisible();
}

async function closeFilterMenu(page) {
  await page.keyboard.press('Escape');
  await expect(page.locator('.admin-facet-menu')).toBeHidden();
}

/** The number a menu option is showing, as a number. */
async function facetCount(page, facetKey, value) {
  const text = await page
    .getByTestId(`facet-${facetKey}-${value}`)
    .locator('.admin-facet-menu__count')
    .innerText();
  return Number(text);
}

test.describe('table filter menu', () => {
  test('a declared state with no rows is offered, at zero', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);

    // Nothing in the fixture is Pending-without-a-container-reason, and the
    // option is still on the menu. Dropping it would be the console saying
    // nothing about a state it does track.
    await expect(page.getByTestId('facet-phase-Pending')).toBeVisible();
    expect(await facetCount(page, 'phase', 'Pending')).toBe(0);
    expect(await facetCount(page, 'phase', 'Running')).toBe(1);
    expect(await facetCount(page, 'phase', 'CrashLoopBackOff')).toBe(1);
  });

  test('a state the cluster produced that nobody declared is offered too', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);

    // `ImagePullBackOff` is not in the page's option list. It is in the data,
    // so it is in the menu — the alternative is a filter that only knows the
    // failure modes somebody thought of in advance.
    await expect(page.getByTestId('facet-phase-ImagePullBackOff')).toBeVisible();
    expect(await facetCount(page, 'phase', 'ImagePullBackOff')).toBe(1);
  });

  test('the counts say what they are counts of', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);

    // A truncated listing holds fewer rows than the cluster does, so these are
    // not cluster totals and the menu does not let them read as any.
    await expect(page.locator('.admin-facet-menu')).toContainText(
      'Counted over the 4 rows loaded here, not the whole cluster.',
    );
  });

  test('ticking a value filters the table and says what that cost', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-phase-CrashLoopBackOff').click();
    await closeFilterMenu(page);

    await expect(bodyRows(page)).toHaveCount(1);
    await expect(page.getByTestId('filter-chip-phase-CrashLoopBackOff')).toBeVisible();
    // The row count is not decoration: it is what keeps a filtered table from
    // reading as a short cluster.
    await expect(page.getByTestId('table-row-count')).toHaveText('Showing 1 of 4');
  });

  test('two values in one facet are an or, not an and', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-phase-CrashLoopBackOff').click();
    await page.getByTestId('facet-phase-Succeeded').click();
    await closeFilterMenu(page);

    // Anded, this is two states one pod would have to be in at once, and the
    // table is empty for a filter the operator would read as "either".
    await expect(bodyRows(page)).toHaveCount(2);
  });

  test('a facet does not zero its own other options', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-phase-Running').click();

    // Still counted against everything except this facet's own selection —
    // "CrashLoopBackOff 1" is what says ticking it as well would add a row.
    // Counted the naive way this reads 0, and the menu claims the cluster has
    // nothing crash-looping while a pod is crash-looping.
    expect(await facetCount(page, 'phase', 'CrashLoopBackOff')).toBe(1);
    expect(await facetCount(page, 'phase', 'Succeeded')).toBe(1);
  });

  test('a different facet does narrow the counts', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-qos_class-Guaranteed').click();

    // One Guaranteed pod, and it is the ImagePullBackOff one. The status counts
    // now describe that pod and nothing else — a facet narrows every menu but
    // its own.
    expect(await facetCount(page, 'phase', 'ImagePullBackOff')).toBe(1);
    expect(await facetCount(page, 'phase', 'Running')).toBe(0);
  });

  test('the derived "not healthy" option keeps matching states nobody listed', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);

    // CrashLoopBackOff, ImagePullBackOff — including the one the page never
    // declared. A "not healthy" built from a list of unhealthy states would
    // miss exactly the state nobody had seen before.
    expect(await facetCount(page, 'phase', 'Unhealthy')).toBe(2);
    await page.getByTestId('facet-phase-Unhealthy').click();
    await closeFilterMenu(page);
    await expect(bodyRows(page)).toHaveCount(2);
  });

  test('an empty result names the filter rather than the cluster', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-phase-Pending').click();
    await closeFilterMenu(page);

    // "No pods in scope" here would be a wrong answer delivered confidently:
    // there are four, and a filter is hiding them.
    await expect(grid(page)).toContainText('No rows match this filter');
    await expect(grid(page)).toContainText('Clear them to see all 4 rows');
  });

  test('clear all filters puts every row back', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-phase-CrashLoopBackOff').click();
    await closeFilterMenu(page);
    await expect(bodyRows(page)).toHaveCount(1);

    await page.getByTestId('clear-all-filters').click();

    await expect(bodyRows(page)).toHaveCount(4);
    await expect(page.getByTestId('table-row-count')).toHaveCount(0);
    await expect(page.getByTestId('filter-chip-phase-CrashLoopBackOff')).toHaveCount(0);
  });

  test('a filter does not survive a reload', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-phase-CrashLoopBackOff').click();
    await closeFilterMenu(page);
    await expect(bodyRows(page)).toHaveCount(1);

    await page.reload();
    await expectPageRendered(page, 'Pods');

    // Deliberate, and the opposite of the column choice below. A filter that
    // came back tomorrow is rows missing from a table that looks complete, with
    // nothing on screen having been clicked to hide them.
    await expect(bodyRows(page)).toHaveCount(4);
    await expect(page.getByTestId('filter-chip-phase-CrashLoopBackOff')).toHaveCount(0);
  });
});

test.describe('manage columns', () => {
  const header = (page, name) => page.getByRole('columnheader', { name, exact: true });

  async function hideNode(page) {
    await page.getByTestId('manage-columns').click();
    await expect(page.getByTestId('manage-columns-dialog')).toBeVisible();
    await page.getByTestId('manage-column-node').click();
    await page.getByTestId('manage-columns-save').click();
    await expect(page.getByTestId('manage-columns-dialog')).toHaveCount(0);
  }

  test('a hidden column leaves the header and the cells', async ({ page }) => {
    await openPods(page);
    await expect(header(page, 'Node')).toBeVisible();
    const cellsBefore = await bodyRows(page).first().locator('td').count();

    await hideNode(page);

    await expect(header(page, 'Node')).toHaveCount(0);
    expect(await bodyRows(page).first().locator('td').count()).toBe(cellsBefore - 1);
  });

  test('hiding a column hides a column, not a row', async ({ page }) => {
    await openPods(page);
    await hideNode(page);

    // The distinction the dialog's own text makes. A column control that
    // dropped rows would be a filter wearing the wrong label.
    await expect(bodyRows(page)).toHaveCount(4);
    await expect(page.getByTestId('table-row-count')).toHaveCount(0);
  });

  test('the choice outlives a reload', async ({ page }) => {
    await openPods(page);
    await hideNode(page);

    await page.reload();
    await expectPageRendered(page, 'Pods');

    await expect(header(page, 'Node')).toHaveCount(0);
    // And the button says the table is not showing everything, so a missing
    // column is never a mystery.
    await expect(page.getByTestId('manage-columns')).toHaveText('Manage columns (8 of 9)');
  });

  test('cancel discards the edit', async ({ page }) => {
    await openPods(page);
    await page.getByTestId('manage-columns').click();
    await page.getByTestId('manage-column-node').click();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await expect(page.getByTestId('manage-columns-dialog')).toHaveCount(0);
    await expect(header(page, 'Node')).toBeVisible();
  });

  test('restore default columns brings them all back', async ({ page }) => {
    await openPods(page);
    await hideNode(page);

    await page.getByTestId('manage-columns').click();
    await page.getByTestId('manage-columns-restore').click();
    await page.getByTestId('manage-columns-save').click();

    await expect(header(page, 'Node')).toBeVisible();
    await expect(page.getByTestId('manage-columns')).toHaveText('Manage columns');
  });

  test('the identity column cannot be hidden', async ({ page }) => {
    await openPods(page);
    await page.getByTestId('manage-columns').click();

    // Shown ticked and disabled rather than left off the list: the rule is
    // visible that way, and a table of anonymous status pills is not a smaller
    // table but a useless one.
    const name = page.getByTestId('manage-column-name');
    await expect(name).toBeChecked();
    await expect(name).toBeDisabled();
  });

  test('a filter on a hidden column keeps filtering, and keeps saying so', async ({ page }) => {
    await openPods(page);
    await openFilterMenu(page);
    await page.getByTestId('facet-qos_class-Guaranteed').click();
    await closeFilterMenu(page);
    await expect(bodyRows(page)).toHaveCount(1);

    await page.getByTestId('manage-columns').click();
    await page.getByTestId('manage-column-qos_class').click();
    await page.getByTestId('manage-columns-save').click();

    // The column is gone and the filter is not. Dropping the filter with the
    // column would put rows back on screen without anybody asking; keeping it
    // silently would hide them. The chip is what makes the third option work.
    await expect(page.getByRole('columnheader', { name: 'QoS', exact: true })).toHaveCount(0);
    await expect(bodyRows(page)).toHaveCount(1);
    await expect(page.getByTestId('filter-chip-qos_class-Guaranteed')).toBeVisible();
  });
});
