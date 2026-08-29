/**
 * The NetworkPolicy tabs (§8.3, §8.4).
 *
 * Every assertion here is about a pair of states that a convenient rendering
 * would collapse, on the one kind where collapsing them is a wrong belief about
 * who can reach a database rather than a wrong number in a table:
 *
 *   "Not governed" vs "Deny all"     one restricts nothing, the other blocks
 *                                    everything, and their YAML differs by a
 *                                    single word inside `policyTypes`.
 *   "Unrestricted" vs an em dash     a pod nothing selects, versus a pod whose
 *                                    selectors could not be evaluated. The first
 *                                    is the finding; the second is a gap in what
 *                                    we know, and printing it as the first is
 *                                    the confidently wrong answer §0 rules out.
 *
 * The enforcement notice is asserted too. It is not decoration: nothing on these
 * tabs is evidence that a packet was dropped, because NetworkPolicy is enforced
 * by a CNI plugin whose behaviour no API here can read.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

async function openPolicies(page) {
  await page.goto('/network');
  await page.getByRole('tab', { name: 'Network Policies' }).click();
}

test.describe('network policies', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('an ungoverned direction never renders as a denial', async ({ page }) => {
    await openPolicies(page);

    const denyAll = page.getByRole('row', { name: /default-deny-ingress/ });
    await expect(denyAll).toContainText('Deny all');
    await expect(denyAll).toContainText('Not governed');

    // The other row proves the third state exists and is spelled differently
    // again: one rule that restricts neither peer nor port opens the direction
    // completely, however restrictive its neighbours are.
    const allowMetrics = page.getByRole('row', { name: /allow-metrics/ });
    await expect(allowMetrics).toContainText('Restricted');
    await expect(allowMetrics).toContainText('Allow all');
  });

  test('the table says out loud that it describes declarations, not traffic', async ({ page }) => {
    await openPolicies(page);

    await expect(page.getByTestId('policy-enforcement-notice').first()).toContainText(
      'enforced by the cluster',
    );
  });

  test('a policy that selects every pod says so rather than showing an empty selector', async ({
    page,
  }) => {
    await openPolicies(page);

    await expect(page.getByRole('row', { name: /default-deny-ingress/ })).toContainText('All pods');
  });

  test('the drawer spells a peer out as a sentence, in the policy own namespace', async ({ page }) => {
    await openPolicies(page);
    await page.getByRole('gridcell', { name: 'allow-metrics', exact: true }).click();

    const drawer = page.getByRole('heading', { name: /NetworkPolicy prod\/allow-metrics/ });
    await expect(drawer).toBeVisible();
    await expect(page.getByText('every pod in namespaces with name=monitoring')).toBeVisible();
  });

  test('a pod no policy selects is reported unrestricted, and an undecidable one is not', async ({
    page,
  }) => {
    await page.goto('/network');
    await page.getByRole('tab', { name: 'Pod Isolation' }).click();

    await expect(page.getByRole('row', { name: /legacy-batch-0/ })).toContainText('Unrestricted');

    // The undecidable pod must NOT say "Unrestricted": it renders as the em dash
    // every nullable value in this console renders as.
    const mystery = page.getByRole('row', { name: /mystery-0/ });
    await expect(mystery).not.toContainText('Unrestricted');
    await expect(mystery).toContainText('—');
  });

  test('the isolation headline counts unknowns apart from unrestricted pods', async ({ page }) => {
    await page.goto('/network');
    await page.getByRole('tab', { name: 'Pod Isolation' }).click();

    const unrestricted = page.getByTestId('metric-card').filter({ hasText: 'Ingress unrestricted' });
    await expect(unrestricted).toContainText('1');
    await expect(unrestricted).toContainText('1 unknown');

    // §11.1: the read that could not be made is named, not absorbed.
    await expect(page.getByTestId('partial-banner').first()).toBeVisible();
  });

  test('a namespace with no policies leaves every pod in it unrestricted, and says why', async ({
    page,
  }) => {
    await mockApi(page, { networkPolicies: FIXTURES.emptyList });
    await openPolicies(page);

    await expect(page.getByText(/Every pod in it is therefore unrestricted/)).toBeVisible();
  });
});
