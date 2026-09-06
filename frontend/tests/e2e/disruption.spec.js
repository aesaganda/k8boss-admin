/**
 * Disruption budgets (§28).
 *
 * Every assertion is about a pair this page must not collapse, and each
 * collapse has a distinct cost:
 *
 *   0 vs —              `selected_pods: 0` is the finding: this budget covers
 *                       nothing and protects nothing. `null` is a pod listing
 *                       that did not answer. Drawing the second as the first
 *                       gets a *working* budget deleted as dead.
 *
 *   never vs not now    `maxUnavailable: 0` can never allow an eviction, at any
 *                       replica count. `disruptionsAllowed: 0` usually clears
 *                       on its own. One is a config bug, the other is Tuesday.
 *
 *   quiet vs flagged    A page that flags every budget is one nobody reads on
 *                       the day a budget is actually broken.
 *
 *   [] vs null          "No pod is doubly covered" versus "we could not check".
 *                       A doubly covered pod cannot be evicted by anyone.
 */
import { expect, test } from '@playwright/test';

import { DISRUPTION_BUDGETS, mockApi } from './fixtures.js';

async function openPage(page, options = {}) {
  await mockApi(page, options);
  await page.goto('/disruption');
  await expect(page.getByRole('grid', { name: 'Pod disruption budgets' })).toBeVisible();
}

test.describe('what a budget covers', () => {
  test('a budget that covers nothing shows 0, not an em dash', async ({ page }) => {
    await openPage(page);

    const covers = page.getByTestId('pdb-covers-ghost');
    await expect(covers).toContainText('0 pods');
    // `NullableCell` always carries the testid and publishes which state it is
    // in as `data-nullable`. That attribute is the discriminator, and asserting
    // the testid's absence would pass for a cell rendering an em dash.
    await expect(covers.getByTestId('nullable-cell')).toHaveAttribute(
      'data-nullable', 'false',
    );
    await expect(page.getByRole('row', { name: /ghost/ })).toContainText(
      'covers no pods',
    );
  });

  test('a count that could not be derived is an em dash, never 0', async ({ page }) => {
    await openPage(page);

    const covers = page.getByTestId('pdb-covers-unread');
    await expect(covers.getByTestId('nullable-cell')).toHaveAttribute(
      'data-nullable', 'true',
    );
    await expect(covers).not.toContainText('0');
  });

  test('a real count reads as a count', async ({ page }) => {
    await openPage(page);

    await expect(page.getByTestId('pdb-covers-web')).toContainText('3 pods');
  });

  test('an unwritten eviction count is an em dash, not a blocking zero', async ({
    page,
  }) => {
    await openPage(page);

    await expect(
      page.getByTestId('pdb-allowed-ghost').getByTestId('nullable-cell'),
    ).toHaveAttribute('data-nullable', 'true');
  });
});

test.describe('the findings', () => {
  test('“can never allow” is separate from “is not allowing now”', async ({ page }) => {
    await openPage(page);

    await expect(page.getByRole('row', { name: /frozen/ })).toContainText(
      'No eviction can ever be permitted',
    );
    // And the row that is merely blocked right now is not described that way.
    await expect(page.getByRole('row', { name: /frozen/ })).not.toContainText(
      'right now',
    );
  });

  test('a permanent finding is not drawn like a transient one', async ({ page }) => {
    /* The distinction the whole page turns on, and it has to survive to the
       colour: "this can never work" and "this is not working this minute" send
       an operator to two different places. Text alone would let both render
       identically and read as one severity. */
    await openPage(page, {
      disruptionBudgets: {
        ...DISRUPTION_BUDGETS,
        overlappingPods: [],
        items: [
          {
            ...DISRUPTION_BUDGETS.items[2], name: 'permanent',
            findings: [{
              code: 'pdb_never_allows_disruption',
              label: 'No eviction can ever be permitted',
              detail: 'maxUnavailable is 0.',
            }],
          },
          {
            ...DISRUPTION_BUDGETS.items[5], name: 'transient', disruptions_allowed: 0,
            findings: [{
              code: 'pdb_blocking_now',
              label: 'No eviction is permitted right now',
              detail: 'disruptionsAllowed is 0. Usually temporary.',
            }],
          },
        ],
      },
    });

    const permanent = page
      .getByRole('row', { name: /permanent/ })
      .getByTestId('status-badge')
      .filter({ hasText: 'ever' });
    const transient = page
      .getByRole('row', { name: /transient/ })
      .getByTestId('status-badge')
      .filter({ hasText: 'right now' });

    await expect(permanent).toHaveClass(/pf-m-red/);
    await expect(transient).not.toHaveClass(/pf-m-red/);
  });

  test('an ordinary budget is flagged with nothing', async ({ page }) => {
    await openPage(page);

    // The reason the findings above are worth anything.
    await expect(page.getByRole('row', { name: /\bweb\b/ })).toContainText(
      'nothing to report',
    );
  });

  test('a budget declaring neither bound is not drawn as a blank', async ({ page }) => {
    await openPage(page, {
      disruptionBudgets: {
        ...DISRUPTION_BUDGETS,
        items: [{
          ...DISRUPTION_BUDGETS.items[5],
          name: 'loose', min_available: null, max_unavailable: null,
          findings: [{
            code: 'pdb_no_constraint',
            label: 'This budget constrains nothing',
            detail: 'Neither minAvailable nor maxUnavailable is set.',
          }],
        }],
        overlappingPods: [],
      },
    });

    const row = page.getByRole('row', { name: /loose/ });
    await expect(row).toContainText('neither set');
    await expect(row).toContainText('constrains nothing');
  });
});

test.describe('the overlap', () => {
  test('two budgets covering one pod are named above the table', async ({ page }) => {
    await openPage(page);

    const banner = page.getByTestId('pdb-overlaps');
    await expect(banner).toContainText('prod/api-0');
    await expect(banner).toContainText('prod/api');
    await expect(banner).toContainText('prod/api-extra');
    // The sentence that explains a drain nobody can account for.
    await expect(banner).toContainText('outright');
  });

  test('both overlapping budgets still report themselves healthy', async ({ page }) => {
    await openPage(page);

    // The whole reason the banner exists: neither object's own numbers reveal
    // the problem.
    await expect(page.getByTestId('pdb-allowed-api')).toContainText('1');
    await expect(page.getByTestId('pdb-allowed-api-extra')).toContainText('1');
  });

  test('no overlap shows no banner at all', async ({ page }) => {
    await openPage(page, {
      disruptionBudgets: { ...DISRUPTION_BUDGETS, overlappingPods: [] },
    });

    await expect(page.getByTestId('pdb-overlaps')).toHaveCount(0);
    await expect(page.getByTestId('pdb-overlaps-unknown')).toHaveCount(0);
  });

  test('an unchecked overlap says so rather than reporting none', async ({ page }) => {
    await openPage(page, {
      disruptionBudgets: {
        ...DISRUPTION_BUDGETS,
        overlappingPods: null,
        partial: true,
        unavailable: [{
          group: '', resource: 'pods', namespace: 'prod', error: 'rbac_denied',
          reason: 'forbidden', message: 'pods is forbidden in prod.', hint: null,
        }],
      },
    });

    const banner = page.getByTestId('pdb-overlaps-unknown');
    await expect(banner).toContainText('unknown');
    await expect(banner).toContainText('not');
    await expect(page.getByTestId('pdb-overlaps')).toHaveCount(0);
  });
});

test.describe('the empty case', () => {
  test('no budgets is a finding, not a gap', async ({ page }) => {
    await mockApi(page, {
      disruptionBudgets: {
        items: [], continue: null, remaining: null, partial: false,
        unavailable: [], overlappingPods: [],
      },
    });
    await page.goto('/disruption');

    await expect(page.getByText('No PodDisruptionBudgets in this scope')).toBeVisible();
    await expect(page.getByText(/evicts these pods freely/)).toBeVisible();
  });
});
