import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * Row density — the Comfy/Compact choice on the workload and pod tables.
 *
 * What is worth asserting here is not that a class lands on a table. It is the
 * four things that make the setting worth having, each of which is easy to lose
 * in a refactor somewhere else:
 *
 * **Compact actually collapses a row.** The padding is worth 8px; the wrapping
 * is worth two or three lines. An implementation that tightened the padding and
 * left the rows wrapping would look implemented, pass a class-name assertion,
 * and save an operator nothing — so the assertion is on the rendered line count
 * of a cell whose content is long enough to wrap.
 *
 * **The choice is remembered, and it is one choice.** Across a reload, and
 * across the two pages. A density that resets is a density nobody sets twice.
 *
 * **A clipped value says it was clipped, and stays readable.** Compact trades
 * wrapping for an ellipsis, and a truncated name presented as a whole one is
 * this project's defect standard with a shorter string. The ellipsis has to be
 * real (the cell overflows) and the full text has to still be reachable.
 *
 * **Comfy is the default and is unchanged.** Nobody who never touches the
 * control sees a difference.
 */

const LONG_WORKLOADS = {
  items: [
    {
      // Long on purpose, in three columns at once: this is the row shape the
      // setting exists for — a generated name, a status the controller
      // explained at length, and two images from a private registry.
      kind: 'Deployment',
      name: 'k8boss-coordinator-579bc6774d-payments-reconciler',
      namespace: 'k8boss-system',
      replicas: { desired: 3, ready: 1, updated: 3, available: 1 },
      images: [
        'registry.internal.example:5000/k8boss/coordinator:2.11.4',
        'registry.internal.example:5000/k8boss/sidecar-proxy:1.0.9',
      ],
      selector: { app: 'k8boss-coordinator' },
      labels: {},
      age_seconds: 864000,
      status: 'Progressing',
      status_reason: 'ReplicaSet "k8boss-coordinator-579bc6774d" is progressing',
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
};

/**
 * How many lines of text a cell is rendering, from its own box and its own
 * line height rather than from a hardcoded pixel count — a font change in a
 * PatternFly release must not turn this suite red.
 */
async function renderedLines(cell) {
  return cell.evaluate((el) => {
    const style = getComputedStyle(el);
    const lineHeight = parseFloat(style.lineHeight);
    const content =
      el.getBoundingClientRect().height -
      parseFloat(style.paddingBlockStart) -
      parseFloat(style.paddingBlockEnd);
    return Math.max(1, Math.round(content / lineHeight));
  });
}

function firstRow(page, table) {
  return page.getByRole('grid', { name: table }).locator('tbody tr').first();
}

/**
 * `locator('td')`, not `getByRole('cell')`. PatternFly renders these tables as
 * `role="grid"`, where a `<td>` is a `gridcell` — so `getByRole('cell')`
 * matches nothing, `.all()` returns an empty list, and a `for` loop of
 * assertions over it passes without running one. Which is exactly what the
 * first draft of this file did.
 */
function cellsOf(row) {
  return row.locator('td');
}

/** The density buttons, by the accessible name an operator reads. */
function densityButton(page, label) {
  return page.getByRole('button', { name: label, exact: true });
}

async function rowHeight(page, table) {
  const row = firstRow(page, table);
  await expect(row).toBeVisible();
  return (await row.boundingBox()).height;
}

/**
 * The Workloads table ships with Schedule and Images hidden so that nine
 * columns fit beside the navigation. These tests are about the Images column —
 * it is the widest cell with inline content, which is what makes it the one
 * worth measuring — so they put it back the way an operator would, by storing
 * what the Manage columns dialog stores.
 */
async function showEveryColumn(page) {
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('k8boss-admin.columns.Workloads', '[]');
    } catch {
      // Storage refused. The table falls back to its defaults and the
      // assertions below say so, rather than this helper throwing here.
    }
  });
}

async function openWorkloads(page, options = {}) {
  await showEveryColumn(page);
  await mockApi(page, { workloads: LONG_WORKLOADS, ...options });
  await page.goto('/workloads');
  await expectPageRendered(page, 'Workloads');
  // The row, not just the table: the table renders skeleton rows first, and a
  // height measured on one of those is a measurement of the wrong thing.
  await expect(page.getByRole('link', { name: LONG_WORKLOADS.items[0].name })).toBeVisible();
}

test.describe('row density', () => {
  test('the table opens comfy, and a long row wraps', async ({ page }) => {
    await openWorkloads(page);

    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'comfy');
    await expect(densityButton(page, 'Comfy')).toHaveAttribute('aria-pressed', 'true');
    await expect(densityButton(page, 'Compact')).toHaveAttribute('aria-pressed', 'false');

    // The premise of every assertion below: at this width, this row does not
    // fit on one line. If PatternFly ever makes it fit, the compact assertions
    // stop meaning anything and this is the test that says so.
    expect(await renderedLines(cellsOf(firstRow(page, 'Workloads')).first())).toBeGreaterThan(1);
  });

  test('compact holds the row to a single line, and it is visibly shorter', async ({ page }) => {
    await openWorkloads(page);
    const comfy = await rowHeight(page, 'Workloads');

    await densityButton(page, 'Compact').click();

    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');
    const row = firstRow(page, 'Workloads');
    // Every cell, not only the name: the status pill sits beside its reason in
    // a flex container of its own, and `nowrap` on the cell does not reach
    // inside one. That cell was two lines tall in the first draft of this.
    for (const cell of await cellsOf(row).all()) {
      expect(await renderedLines(cell)).toBe(1);
    }

    const compact = await rowHeight(page, 'Workloads');
    expect(compact).toBeLessThan(comfy);
  });

  test('switching back to comfy lets the row wrap again', async ({ page }) => {
    await openWorkloads(page);
    const comfy = await rowHeight(page, 'Workloads');

    await densityButton(page, 'Compact').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');

    await densityButton(page, 'Comfy').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'comfy');

    // Back to the height it started at, not merely "taller than compact": a
    // one-way setting is a setting an operator is stuck with.
    expect(await rowHeight(page, 'Workloads')).toBeCloseTo(comfy, 0);
  });

  test('a clipped name is marked as clipped and still carries its full text', async ({ page }) => {
    await openWorkloads(page);
    await densityButton(page, 'Compact').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');

    const nameCell = cellsOf(firstRow(page, 'Workloads')).first();
    // The clamp is really clamping something: the content is taller than the
    // one line on show, which is what draws the ellipsis. A cell that merely
    // fit would render the name in full, and this feature would be untested on
    // the one row it was written for.
    const clamped = await nameCell
      .locator('.admin-cell-clamp')
      .evaluate((el) => el.scrollHeight > el.clientHeight);
    expect(clamped).toBe(true);

    await expect(nameCell.getByRole('link')).toHaveAttribute(
      'title',
      /k8boss-coordinator-579bc6774d-payments-reconciler/,
    );
  });

  test('compact keeps the columns Comfy computed rather than widening the table', async ({ page }) => {
    await openWorkloads(page);
    const columnWidth = async (name) =>
      (await page.getByRole('columnheader', { name, exact: true }).boundingBox()).width;
    const table = page.getByRole('grid', { name: 'Workloads' });
    const tableWidth = () => table.evaluate((el) => el.scrollWidth);

    const nameBefore = await columnWidth('Name');
    const imagesBefore = await columnWidth('Images');
    const widthBefore = await tableWidth();

    await densityButton(page, 'Compact').click();
    await expect(table).toHaveAttribute('data-density', 'compact');

    // Compact is a change of height, not of layout. The obvious implementation
    // of "one line per row" is `white-space: nowrap`, which makes each cell's
    // full string its column's MINIMUM width: the rows do collapse, and the
    // table grows past the window instead — so an operator who asked to stop
    // scrolling down starts scrolling sideways. The tolerance is the inline
    // spacing that replaces the flex `gap` in the cells that have one.
    expect(await columnWidth('Name')).toBeCloseTo(nameBefore, 0);
    expect(await columnWidth('Images')).toBeLessThan(imagesBefore + 12);
    expect(await tableWidth()).toBeLessThan(widthBefore + 12);
  });

  test('the choice outlives a reload', async ({ page }) => {
    await openWorkloads(page);
    await densityButton(page, 'Compact').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');

    await page.reload();
    await expectPageRendered(page, 'Workloads');

    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');
    await expect(densityButton(page, 'Compact')).toHaveAttribute('aria-pressed', 'true');
  });

  test('the pods table honours the choice made on the workload table', async ({ page }) => {
    await openWorkloads(page);
    await densityButton(page, 'Compact').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');

    await page.getByRole('link', { name: 'Pods', exact: true }).click();
    await expectPageRendered(page, 'Pods');

    // One preference for both tables. Asking for compact rows once and getting
    // them on one page is how a setting reads as broken.
    await expect(page.getByRole('grid', { name: 'Pods' })).toHaveAttribute('data-density', 'compact');
    await expect(densityButton(page, 'Compact')).toHaveAttribute('aria-pressed', 'true');
    for (const cell of await cellsOf(firstRow(page, 'Pods')).all()) {
      expect(await renderedLines(cell)).toBe(1);
    }
  });

  test('an unreadable stored preference falls back to comfy rather than to nothing', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('k8boss-admin.density', 'ultra'));
    await openWorkloads(page);

    // A density with no rules behind it would render as a table with no density
    // at all, and there would be nothing on screen to get back from it.
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'comfy');
    await expect(densityButton(page, 'Comfy')).toHaveAttribute('aria-pressed', 'true');
  });

  test('the clamp is dropped where PatternFly restacks the table into cards', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openWorkloads(page);
    await densityButton(page, 'Compact').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');

    await page.setViewportSize({ width: 700, height: 900 });

    // In card mode there is no header, so there is no column to drag a clipped
    // name back out of and no neighbouring cell it could run into — a value
    // clamped here is clamped for good. The rows wrap again; the tighter
    // padding, which costs nothing, stays.
    const stacked = await page.getByRole('grid', { name: 'Workloads' }).evaluate((table) => {
      const clamp = table.querySelector('tbody td .admin-cell-clamp');
      return {
        headerShown: getComputedStyle(table.querySelector('thead')).display !== 'none',
        clamped: clamp.scrollHeight > clamp.clientHeight,
        fullName: clamp.textContent,
      };
    });
    expect(stacked.headerShown).toBe(false);
    expect(stacked.clamped).toBe(false);
    expect(stacked.fullName).toContain('k8boss-coordinator-579bc6774d-payments-reconciler');
  });

  test('compact rows still resize from the header', async ({ page }) => {
    await openWorkloads(page);
    await densityButton(page, 'Compact').click();
    await expect(page.getByRole('grid', { name: 'Workloads' })).toHaveAttribute('data-density', 'compact');

    const header = page.getByRole('columnheader', { name: 'Name', exact: true });
    const before = (await header.boundingBox()).width;

    const handle = page.getByTestId('column-resizer-name');
    const box = await handle.boundingBox();
    const y = box.y + box.height / 2;
    const x = box.x + box.width / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 120, y, { steps: 8 });
    await page.mouse.up();

    // The two features touch the same cells — one sets `overflow: hidden` for
    // its ellipsis, the other for its fixed layout — and widening a column is
    // how an operator reads a name compact clipped.
    expect((await header.boundingBox()).width).toBeCloseTo(before + 120, 0);
  });
});
