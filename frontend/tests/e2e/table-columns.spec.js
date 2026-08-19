import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * Resizable columns on the tabulated pages.
 *
 * The behaviour is one component wide — every list page renders through
 * `DataTable` — so this suite drives the two pages the feature was asked for
 * and asserts the properties that are easy to break somewhere else:
 *
 * **A resize sticks.** Not just during the drag: across the next poll and
 * across a reload. A width that resets is worse than no resize, because the
 * operator has to re-do it every time they come back to the page.
 *
 * **Resizing one column does not move the others.** The layout switches from
 * automatic to fixed on the first drag, and the obvious implementation pins
 * only the column being dragged — which re-sizes every remaining column from
 * its content to an equal share, so the whole table jumps on first grab.
 *
 * **Grabbing the edge of a sortable column does not sort it.** The handle sits
 * inside a header cell that reacts to clicks. Re-sorting a table because
 * somebody grabbed its edge teaches people not to grab edges.
 */

const STORAGE_KEY = 'k8boss-admin.columnWidths.Workloads';

async function headerWidth(page, name) {
  const box = await page.getByRole('columnheader', { name, exact: true }).boundingBox();
  return box.width;
}

/** Drag a column's handle by `dx` pixels. */
async function dragColumn(page, key, dx) {
  const handle = page.getByTestId(`column-resizer-${key}`);
  const box = await handle.boundingBox();
  const y = box.y + box.height / 2;
  const x = box.x + box.width / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  // In steps: a single jump is one pointermove, and a drag implementation that
  // only ever sees the final position would pass a test it should not.
  await page.mouse.move(x + dx, y, { steps: 8 });
  await page.mouse.up();
}

async function openWorkloads(page) {
  await mockApi(page);
  await page.goto('/workloads');
  await expectPageRendered(page, 'Workloads');
  await expect(page.getByRole('grid', { name: 'Workloads' })).toBeVisible();
}

test.describe('resizable table columns', () => {
  test('a header drag resizes that column and nothing else', async ({ page }) => {
    await openWorkloads(page);

    const nameBefore = await headerWidth(page, 'Name');
    const kindBefore = await headerWidth(page, 'Kind');

    await dragColumn(page, 'name', 160);

    // Within a pixel of what was asked for, not merely "wider": a resize that
    // lands somewhere near the pointer is a resize the operator has to fight.
    expect(await headerWidth(page, 'Name')).toBeCloseTo(nameBefore + 160, 0);
    // The neighbour keeps the width it had. The slack comes out of the trailing
    // column, which is why that one is never pinned.
    expect(await headerWidth(page, 'Kind')).toBeCloseTo(kindBefore, 0);
  });

  test('a column dragged narrower stays narrow', async ({ page }) => {
    await openWorkloads(page);

    const before = await headerWidth(page, 'Images');
    await dragColumn(page, 'images', -60);

    // The failure this guards: with every column pinned and their total under
    // the table width, browsers hand the leftover back out proportionally, so
    // the column springs part of the way back and the drag looks ignored.
    expect(await headerWidth(page, 'Images')).toBeCloseTo(before - 60, 0);
  });

  test('the width outlives a reload', async ({ page }) => {
    await openWorkloads(page);

    await dragColumn(page, 'name', 120);
    const resized = await headerWidth(page, 'Name');

    const stored = await page.evaluate((key) => window.localStorage.getItem(key), STORAGE_KEY);
    expect(JSON.parse(stored).name).toBeCloseTo(resized, 0);

    await page.reload();
    await expectPageRendered(page, 'Workloads');
    expect(await headerWidth(page, 'Name')).toBeCloseTo(resized, 0);
  });

  test('grabbing the edge of a sortable column does not sort it', async ({ page }) => {
    await openWorkloads(page);

    const header = page.getByRole('columnheader', { name: 'Name', exact: true });
    const sortBefore = await header.getAttribute('aria-sort');

    await dragColumn(page, 'name', 90);

    expect(await header.getAttribute('aria-sort')).toBe(sortBefore);
    // And the sort itself still works afterwards — the handle must not have
    // swallowed the header's own click target.
    await header.getByRole('button', { name: 'Name' }).click();
    await expect(header).toHaveAttribute('aria-sort', 'ascending');
  });

  test('the handle resizes from the keyboard', async ({ page }) => {
    await openWorkloads(page);

    const before = await headerWidth(page, 'Kind');
    const handle = page.getByTestId('column-resizer-kind');
    await handle.focus();
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('ArrowRight');

    // A pointer-only resize is a resize half the operators of an admin console
    // cannot perform at all.
    expect(await headerWidth(page, 'Kind')).toBeCloseTo(before + 32, 0);

    // Home gives the column its automatic width back without disturbing the
    // ones the operator did set.
    await page.keyboard.press('Home');
    const stored = await page.evaluate((key) => window.localStorage.getItem(key), STORAGE_KEY);
    expect(JSON.parse(stored)).not.toHaveProperty('kind');
  });

  test('reset returns every column to an automatic width', async ({ page }) => {
    await openWorkloads(page);

    const before = await headerWidth(page, 'Name');
    await dragColumn(page, 'name', 140);

    const reset = page.getByTestId('reset-column-widths');
    await expect(reset).toBeVisible();
    await reset.click();

    // Gone from the table and gone from storage: a reset that left the entry
    // behind would come back on the next visit.
    await expect(reset).toHaveCount(0);
    expect(await headerWidth(page, 'Name')).toBeCloseTo(before, 0);
    expect(await page.evaluate((key) => window.localStorage.getItem(key), STORAGE_KEY)).toBeNull();
  });

  test('widening past the viewport scrolls rather than crushing the other columns', async ({ page }) => {
    await openWorkloads(page);

    const kindBefore = await headerWidth(page, 'Kind');
    await dragColumn(page, 'name', 900);

    // The table outgrows its container and the page offers the rest sideways.
    // The alternative — every other column squeezed to fit — hides the data the
    // operator was widening a column to read. Asserted on the scroll container
    // rather than on the table's width: a table that is wider than a container
    // which cannot scroll is worse than a narrow one, because the columns past
    // the edge are then unreachable rather than merely off-screen.
    const grid = page.getByRole('grid', { name: 'Workloads' });
    const scroll = await grid.evaluate((table) => {
      const scroller = table.closest('main');
      scroller.scrollLeft = 10_000;
      return {
        overflow: table.getBoundingClientRect().width - scroller.clientWidth,
        scrollable: scroller.scrollWidth - scroller.clientWidth,
        scrolled: scroller.scrollLeft,
      };
    });
    expect(scroll.overflow).toBeGreaterThan(0);
    expect(scroll.scrollable).toBeGreaterThan(0);
    expect(scroll.scrolled).toBeGreaterThan(0);
    expect(await headerWidth(page, 'Kind')).toBeCloseTo(kindBefore, 0);
  });

  test('the table does not move under the pointer when the first drag starts', async ({ page }) => {
    await openWorkloads(page);

    const header = page.getByRole('columnheader', { name: 'Name', exact: true });
    const before = await header.boundingBox();
    await dragColumn(page, 'name', 100);
    const after = await header.boundingBox();

    // The "Reset column widths" control appears the first time a table is
    // sized. Rendered above the table it pushed the header — and the handle
    // being held — down by its own height, mid-gesture.
    expect(after.y).toBeCloseTo(before.y, 0);
  });

  test('pinned widths are dropped where PatternFly restacks the table into cards', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openWorkloads(page);
    await dragColumn(page, 'name', 300);

    await page.setViewportSize({ width: 700, height: 900 });
    const stacked = await page.getByRole('grid', { name: 'Workloads' }).evaluate((table) => {
      const main = table.closest('main');
      return {
        headerShown: getComputedStyle(table.querySelector('thead')).display !== 'none',
        tableWidth: Math.round(table.getBoundingClientRect().width),
        containerWidth: main.clientWidth,
        overflow: main.scrollWidth - main.clientWidth,
      };
    });

    // Below this width the header — and with it every resize handle — is gone,
    // and the rows are cards rather than columns. A desktop pixel width left
    // over from a wider window leaves that card list scrolled sideways with no
    // handle left to undo it.
    expect(stacked.headerShown).toBe(false);
    expect(stacked.tableWidth).toBeLessThanOrEqual(stacked.containerWidth);
    expect(stacked.overflow).toBe(0);

    // And the widths come back with the columns, rather than being discarded.
    await page.setViewportSize({ width: 1280, height: 900 });
    await expect(page.getByTestId('column-resizer-name')).toBeVisible();
    expect(await headerWidth(page, 'Name')).toBeGreaterThan(300);
  });

  test('a column narrowed to its minimum clips rather than painting over its neighbour', async ({ page }) => {
    await openWorkloads(page);

    // 400px left of a 208px column: the drag clamps at the minimum width.
    await dragColumn(page, 'status', -400);

    const cells = await page.getByRole('grid', { name: 'Workloads' }).evaluate((table) => {
      const row = table.querySelector('tbody tr');
      return Array.from(row.querySelectorAll('td')).map((cell) => ({
        label: cell.getAttribute('data-label'),
        overflow: getComputedStyle(cell).overflow,
        width: Math.round(cell.getBoundingClientRect().width),
        content: cell.scrollWidth,
        isAction: cell.classList.contains('pf-v6-c-table__action'),
      }));
    });

    // Under a fixed layout a cell is exactly its column's width, and PatternFly
    // clips only the columns its `truncate` modifier is on. A StatusBadge in a
    // 56px Status column was painted across the Ready column's "4 / 5" — two
    // columns' values overprinting each other. Asserted on the clip rather than
    // on geometry: `overflow: hidden` bounds what is painted, not the boxes.
    const status = cells.find((cell) => cell.label === 'Status');
    expect(status.width).toBe(56);
    expect(status.content).toBeGreaterThan(status.width);
    expect(status.overflow).toBe('hidden');

    // Every data cell, and deliberately not the actions cell: PatternFly renders
    // the kebab's menu inline, inside that cell, so clipping it would clip the
    // menu shut. Asserted rather than assumed, because the exemption is written
    // against a PatternFly class name.
    expect(cells.filter((cell) => cell.isAction)).toHaveLength(1);
    for (const cell of cells) {
      expect(cell.overflow).toBe(cell.isAction ? 'visible' : 'hidden');
    }

    // And the menu really does open to its full size inside that cell.
    await page.getByRole('grid', { name: 'Workloads' }).getByRole('row').nth(1).getByRole('button').last().click();
    const menu = page.getByRole('menu');
    await expect(menu).toBeVisible();
    const clipped = await menu.evaluate((node) => {
      const cell = node.closest('td');
      if (!cell) return 0;
      const box = node.getBoundingClientRect();
      const cellBox = cell.getBoundingClientRect();
      return Math.round(Math.max(0, box.right - cellBox.right, cellBox.left - box.left));
    });
    expect(clipped).toBe(0);
  });

  test('resetting hands focus on rather than dropping it', async ({ page }) => {
    await openWorkloads(page);
    await dragColumn(page, 'name', 120);

    await page.getByTestId('reset-column-widths').focus();
    await page.keyboard.press('Enter');

    // The control removes itself by succeeding. Left alone, focus falls to
    // <body> and the operator's next Tab restarts at the masthead — roughly two
    // dozen stops from the table they were working in.
    const focused = await page.evaluate(() => ({
      tag: document.activeElement?.tagName,
      testid: document.activeElement?.getAttribute('data-testid'),
    }));
    expect(focused.testid).toBe('column-resizer-name');
  });

  test('a handle reports a width it measured, not one the browser invented', async ({ page }) => {
    await openWorkloads(page);

    const handle = page.getByTestId('column-resizer-kind');
    const width = await headerWidth(page, 'Kind');
    await handle.focus();

    // Chrome fills a focusable separator's missing aria-valuenow with 50 — below
    // the minimum this handle declares — so a screen reader announces a column
    // as narrower than its own floor while it is 100px wide. Omitting the value
    // is not silence.
    expect(Number(await handle.getAttribute('aria-valuenow'))).toBeCloseTo(width, 0);
    expect(await handle.getAttribute('aria-valuetext')).toContain('Automatic width');
  });

  test('a table inside a dialog scrolls the dialog rather than painting outside it', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockApi(page);
    // The drain plan is the one table this console renders inside a modal, and
    // a modal body scrolls vertically only.
    await page.route('**/access/preflight**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          results: ['cordon', 'drain'].map((id) => ({
            id,
            allowed: true,
            reason: '',
            evaluationError: null,
            hint: null,
          })),
        }),
      }),
    );
    await page.route('**/nodes/*/drain**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          dryRun: true,
          applied: false,
          blocked: 0,
          resourceVersion: '12345',
          diff: '--- live\n+++ projected\n@@ -1,2 +1,3 @@\n spec:\n+  unschedulable: true\n',
          plan: [
            {
              namespace: 'prod',
              pod: 'checkout-7d9f8b6c5d-abcde',
              action: 'evict',
              reason: '',
              controller: { kind: 'ReplicaSet', name: 'checkout-7d9f8b6c5d' },
            },
          ],
        }),
      }),
    );

    await page.goto('/nodes');
    await expectPageRendered(page, 'Nodes');
    await page.getByRole('grid', { name: 'Nodes' }).getByRole('row').nth(1).getByRole('button').last().click();
    await page.getByRole('menuitem', { name: /Drain/ }).click();
    await page.getByRole('button', { name: /Preview changes/ }).click();
    await expect(page.getByRole('grid', { name: 'Drain plan' })).toBeVisible();

    await dragColumn(page, 'controller', 500);

    const geometry = await page.getByRole('grid', { name: 'Drain plan' }).evaluate((table) => {
      const body = table.closest('.pf-v6-c-modal-box__body');
      const box = table.closest('.pf-v6-c-modal-box');
      body.scrollLeft = 5000;
      return {
        scrollable: body.scrollWidth - body.clientWidth,
        scrolled: body.scrollLeft,
        overhang: Math.round(table.getBoundingClientRect().right - box.getBoundingClientRect().right),
      };
    });

    // Without the dialog being told to scroll sideways, the widened column is
    // painted outside the modal box, where nothing the operator can do will
    // bring it back — there is no scrollbar and no page scroll behind a modal.
    expect(geometry.scrollable).toBeGreaterThan(0);
    expect(geometry.scrolled).toBeGreaterThan(0);
    expect(geometry.overhang).toBeLessThanOrEqual(0);
  });

  test('the pods table carries the same handles', async ({ page }) => {
    await mockApi(page);
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');

    // Every column but the trailing one, which absorbs the leftover width.
    await expect(page.getByTestId('column-resizer-name')).toBeVisible();
    await expect(page.getByTestId('column-resizer-containers')).toBeVisible();
    await expect(page.getByTestId('column-resizer-age_seconds')).toBeVisible();

    // Named for the column, not "resize handle" repeated nine times: a screen
    // reader user tabbing the header has to know which column they are on.
    await expect(page.getByRole('separator', { name: 'Resize the Node column' })).toHaveCount(1);
  });
});
