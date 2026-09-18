import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * A list table is not made wider than the page by what is written in it.
 *
 * The failure this pins down: an image pinned by digest —
 * `sha256:f57b7e3868d9126c0d347397aba7f186242ae0ac6f54eed93a19a5cbca65ce9e`,
 * 71 characters, and neither a slash nor a colon is a break opportunity in CSS
 * — makes its column's *minimum* width that entire string. An automatic table
 * layout has to honour a column minimum, so the Pods table was drawn 552px
 * wider than the card holding it and the Workloads table 64px wider. The
 * operator then reads the cluster through a window they drag left and right,
 * losing the Name column off the left edge every time they go to check the Age
 * on the right. Both densities did it, so Compact was no escape: clamping drops
 * the lines after the first, it does not make the longest word any narrower.
 *
 * Measured at the table's own scroll parent rather than at the document,
 * because that is the box that actually scrolls — the page around it stays put
 * and the overflow is invisible to a `window.scrollX` assertion.
 *
 * **Why 1440, with the Images column switched on.** How many columns fit a
 * given window is a question about the column set, not about content — it is
 * why the Pods table ships with two of them hidden — and it is answered in
 * `table-filters.spec.js`. What this suite is for is the part that is *ours*:
 * whatever columns are on, a cell's content must never be what decides the
 * table's width. So these tests put the column under test back on and measure
 * at a width where that arrangement is comfortable, which makes any overflow
 * here attributable to the cells rather than to the arithmetic of ten
 * minimums.
 *
 * Not asserted here: that a table an operator has deliberately dragged wider
 * may still scroll. That is `table-columns.spec.js`'s subject, and it is the
 * one case where scrolling sideways answers a question somebody asked.
 */

const VIEWPORT = { width: 1440, height: 900 };

/**
 * The Pods table ships with QoS and Images hidden, and Images is the column this
 * whole suite is about — so these tests turn it on the way an operator would, by
 * storing what the Manage columns dialog stores. Measuring the default set would
 * be measuring a table that happens not to contain the string that used to
 * break it.
 *
 * QoS stays hidden: it has nothing to do with this and switching it on too is
 * asking whether nine columns fit, which is a different question with a
 * different answer and its own place to be answered (`table-filters.spec.js`).
 */
const SHOW_IMAGES_COLUMN = { key: 'k8boss-admin.columns.Pods', value: '["qos_class"]' };

async function seedColumnChoice(page, { key, value }) {
  await page.addInitScript(
    ([k, v]) => {
      try {
        window.localStorage.setItem(k, v);
      } catch {
        // Storage refused: the page falls back to its defaults and the
        // assertions below will say so rather than this helper throwing here.
      }
    },
    [key, value],
  );
}

/**
 * Two real rows off a kind cluster, carrying the two shapes a digest arrives in
 * — kubelet's bare `sha256:…` for a pulled-by-digest container, and a workload
 * pinned as `registry/name@sha256:…`, which is still 71 unbreakable characters
 * after its registry has been cut off. Between them they made the Pods table
 * 552px too wide; the stock fixture pods carry ordinary `registry/name:tag`
 * references and never showed it.
 */
const DIGEST_PODS = {
  ...FIXTURES.pods,
  items: [
    ...FIXTURES.pods.items,
    {
      name: 'echo-same-node-68fbb8dcd9-99w8l',
      namespace: 'cilium-test-1',
      phase: 'Running',
      phase_detail: null,
      ready: '2/2',
      restarts: 286,
      node: 'k8boss-control-plane',
      qos_class: 'BestEffort',
      containers: [
        {
          name: 'echo',
          image: 'sha256:f72407be9e08c3a1b29a88318cbfee87b9f2da489f84015a5090b1e386e4dbc1',
          ready: true,
          restart_count: 286,
          kind: 'container',
        },
        {
          name: 'proxy',
          image: 'sha256:f57b7e3868d9126c0d347397aba7f186242ae0ac6f54eed93a19a5cbca65ce9e',
          ready: true,
          restart_count: 0,
          kind: 'container',
        },
      ],
      age_seconds: 7200,
      creationTimestamp: '2026-09-18T08:00:00Z',
    },
    {
      name: 'pinned-deploy-5f9c7d4b88-2ktzq',
      namespace: 'prod',
      phase: 'Running',
      phase_detail: null,
      ready: '1/1',
      restarts: 0,
      node: 'k8boss-control-plane',
      qos_class: 'Burstable',
      containers: [
        {
          name: 'app',
          image:
            'registry.example:5000/checkout@sha256:c0ffee1234567890abcdef1234567890abcdef1234567890abcdef1234567890',
          ready: true,
          restart_count: 0,
          kind: 'container',
        },
      ],
      age_seconds: 600,
      creationTimestamp: '2026-09-18T09:30:00Z',
    },
  ],
};

/** How far past its own box a table's scroll parent is being asked to draw. */
async function sideScrollOf(page, tableName) {
  return page
    .getByRole('grid', { name: tableName })
    .evaluate((table) => table.parentElement.scrollWidth - table.parentElement.clientWidth);
}

async function expectNoSideScroll(page, tableName) {
  expect(await sideScrollOf(page, tableName), `${tableName} scrolls sideways`).toBeLessThanOrEqual(0);
}

/**
 * Headings whose longest word does not fit the box it is drawn in — which is
 * exactly where PatternFly puts an ellipsis.
 *
 * Measured against the longest WORD rather than the whole label because a
 * heading is allowed to wrap onto a second line; "Restarts (24h)" over two
 * lines is fine, "Restarts…" is not. The natural width is taken from a
 * detached copy in the same font: `scrollWidth` cannot be used here, because
 * Chrome reports it equal to `clientWidth` on an ellipsis-clipped span.
 */
async function clippedHeadings(page, tableName) {
  return page.getByRole('grid', { name: tableName }).evaluate((table) =>
    [...table.querySelectorAll('thead th')]
      .map((th) => {
        const label = th.querySelector('.pf-v6-c-table__text');
        if (!label || !label.textContent.trim()) return null;
        const longest = label.textContent
          .trim()
          .split(/\s+/)
          .reduce((a, b) => (a.length >= b.length ? a : b), '');
        const probe = document.createElement('span');
        const style = getComputedStyle(label);
        probe.style.cssText =
          'position:absolute;visibility:hidden;white-space:pre;width:max-content';
        probe.style.font = style.font;
        probe.style.letterSpacing = style.letterSpacing;
        probe.textContent = longest;
        document.body.appendChild(probe);
        const needed = probe.getBoundingClientRect().width;
        probe.remove();
        return needed - label.getBoundingClientRect().width > 0.5 ? label.textContent.trim() : null;
      })
      .filter(Boolean),
  );
}

test.use({ viewport: VIEWPORT });

test.describe('a cell never decides the table width', () => {
  for (const density of ['Comfy', 'Compact']) {
    test(`pods pinned by digest do not widen the table (${density})`, async ({ page }) => {
      await seedColumnChoice(page, SHOW_IMAGES_COLUMN);
      await mockApi(page, { pods: DIGEST_PODS });
      await page.goto('/pods');
      await expectPageRendered(page, 'Pods');
      await expect(page.getByRole('grid', { name: 'Pods' })).toBeVisible();

      await page.getByRole('button', { name: density, exact: true }).click();
      await expect(page.getByRole('grid', { name: 'Pods' })).toHaveAttribute(
        'data-density',
        density.toLowerCase(),
      );

      await expectNoSideScroll(page, 'Pods');

      // A shortened digest says it was shortened, and the whole of it stays
      // reachable. A column that fits by lying about what is in it is the same
      // defect wearing a narrower table.
      const grid = page.getByRole('grid', { name: 'Pods' });
      await expect(grid.getByText('sha256:f72407be9e08…', { exact: true })).toHaveAttribute(
        'title',
        'sha256:f72407be9e08c3a1b29a88318cbfee87b9f2da489f84015a5090b1e386e4dbc1',
      );
      // The registry-qualified form keeps the part that identifies the image —
      // cutting at the last slash alone would have left the digest whole.
      await expect(grid.getByText('checkout@sha256:c0ffee123456…', { exact: true })).toHaveAttribute(
        'title',
        'registry.example:5000/checkout@sha256:c0ffee1234567890abcdef1234567890abcdef1234567890abcdef1234567890',
      );
    });
  }

  test('the workload table fits at its widest row', async ({ page }) => {
    await mockApi(page);
    await page.goto('/workloads');
    await expectPageRendered(page, 'Workloads');
    await expect(page.getByRole('grid', { name: 'Workloads' })).toBeVisible();

    await expectNoSideScroll(page, 'Workloads');
  });

  test('the exposures table fits, and its URLs stay whole', async ({ page }) => {
    await mockApi(page);
    await page.goto('/routes');
    await expectPageRendered(page, 'Routes');
    await expect(page.getByRole('grid', { name: 'Routes' })).toBeVisible();

    await expectNoSideScroll(page, 'Routes');

    // A hostname has no break opportunity either — CSS breaks at spaces and
    // hyphens, and at neither a dot nor a slash — so one URL was this column's
    // minimum width and the table was drawn 251px past the card holding it.
    // Wrapped across lines rather than cut short: a hostname truncated to
    // "https://shop.exa…" cannot be read, copied or checked against DNS, and
    // the chip carries no tooltip to recover it from.
    const url = page
      .getByRole('grid', { name: 'Routes' })
      .getByText('https://shop.example.com/', { exact: true });
    await expect(url).toBeVisible();
  });

  test('the ConfigMaps table fits', async ({ page }) => {
    await mockApi(page);
    await page.goto('/config');
    // The sections are sidebar links now, so /config redirects to the first
    // one and the heading is that listing's own name.
    await expectPageRendered(page, 'ConfigMaps');
    await expect(page.getByRole('grid', { name: 'ConfigMaps' })).toBeVisible();

    await expectNoSideScroll(page, 'ConfigMaps');
  });
});

/**
 * A column is never narrower than its own heading.
 *
 * PatternFly reserves a flat 6ch for a sortable header's label and counts
 * neither the sort icon nor the button's trailing padding, so on a table of
 * short values — which is most of them here — every heading was cut: Namespace
 * read "N…", Schedule read "Sche…", and Ready and Restarts were BOTH headed
 * "R…". Two columns, one label, and no way to tell which count is which, on the
 * page an operator opens to find out why something is restarting.
 *
 * Asserted at 1280 as well as at the comfortable width, because that is where
 * every column sits at its minimum and the minimum is the thing under test.
 */
test.describe('every column fits its own heading', () => {
  for (const width of [1280, 1440]) {
    test(`pod headings are whole at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await mockApi(page);
      await page.goto('/pods');
      await expectPageRendered(page, 'Pods');

      expect(await clippedHeadings(page, 'Pods')).toEqual([]);
      await expectNoSideScroll(page, 'Pods');
    });
  }

  test('a two-word heading wraps rather than being cut', async ({ page }) => {
    await mockApi(page);
    await page.goto('/workloads');
    await expectPageRendered(page, 'Workloads');

    // "Restarts (24h)" is the one that does not fit on a line at this width.
    // It is whole, and the table still does not scroll — the alternative that
    // was tried first, holding it to one line, cost 13px the table did not
    // have.
    expect(await clippedHeadings(page, 'Workloads')).toEqual([]);
    await expect(
      page.getByRole('columnheader', { name: 'Restarts (24h)', exact: true }),
    ).toBeVisible();
    await expectNoSideScroll(page, 'Workloads');
  });

  test('exposure headings are whole', async ({ page }) => {
    await mockApi(page);
    await page.goto('/routes');
    await expectPageRendered(page, 'Routes');

    // Name is the case the flat allowance got wrong: the first cell of a row
    // is drawn with the wider page-chrome inset, which PatternFly applies to
    // the padding without updating the padding variables a sum can read.
    expect(await clippedHeadings(page, 'Routes')).toEqual([]);
  });

  test('Ready and Restarts are told apart', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockApi(page);
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');

    // The instance that made this worth fixing: both columns are counts, they
    // sit next to each other, and both rendered as "R…".
    await expect(page.getByRole('columnheader', { name: 'Ready', exact: true })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Restarts', exact: true })).toBeVisible();
  });
});

/**
 * The narrowest desktop window, with the navigation open.
 *
 * 1280px leaves 918px beside the sidebar, and ten columns each carrying a
 * minimum do not fit in it — which is a fact about the column set, not about
 * anything in the cells. Five tables were over: Pods by 132px, Workloads by 64,
 * Routes by 411, Nodes by 25 and claims by 43. Each now ships with the columns
 * it can most afford to lose already hidden, named and argued for at the top of
 * its own page, and one visit to Manage columns overrides that for good.
 *
 * Asserted per page rather than as a rule about tables, because the choice of
 * which columns go is a judgement made once per page and this is where a later
 * addition of a tenth column shows up as a failing test rather than as a table
 * an operator has to drag.
 */
test.describe('a list page fits the narrowest desktop window', () => {
  const PAGES = [
    ['/pods', 'Pods', 'Pods'],
    ['/workloads', 'Workloads', 'Workloads'],
    ['/routes', 'Routes', 'Routes'],
    ['/nodes', 'Nodes', 'Nodes'],
    ['/storage', 'PersistentVolumeClaims', 'PersistentVolumeClaims'],
  ];

  for (const [path, heading, table] of PAGES) {
    test(`${heading} fits at 1280px`, async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 900 });
      await mockApi(page);
      await page.goto(path);
      await expectPageRendered(page, heading);
      await expect(page.getByRole('grid', { name: table })).toBeVisible();

      await expectNoSideScroll(page, table);
      // And not by cutting the headings short, which is the other way to fit.
      expect(await clippedHeadings(page, table)).toEqual([]);
    });
  }
});
