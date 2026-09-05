/**
 * Subject access review (§23).
 *
 * Every assertion here is about a pair of states a convenient rendering would
 * collapse, and on this page collapsing them always produces a wrong statement
 * about somebody's access:
 *
 *   `groups_complete` false vs true   For a User the answer is only about the
 *                                     groups typed in — access mostly arrives
 *                                     through groups, and a reader who misses
 *                                     that turns "a user named alice in these
 *                                     groups cannot" into "alice cannot", about
 *                                     an administrator.
 *   `denied` vs `allowed: false`      An authorizer explicitly refusing versus
 *                                     nothing having granted it. Most RBAC-only
 *                                     clusters only ever produce the second.
 *   `evaluationError` vs a denial     §0.2. A question the authorizer could not
 *                                     answer is not a "no", and reading it as
 *                                     one sends somebody to grant a permission
 *                                     that is already there.
 */
import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

async function openReview(page) {
  await page.goto('/access');
  await page.getByRole('tab', { name: 'Access review' }).click();
  await expect(page.getByTestId('sar-form')).toBeVisible();
}

async function askAbout(page, { kind = 'User', name, namespace, groups } = {}) {
  await openReview(page);
  if (kind !== 'User') await page.getByTestId('sar-kind').selectOption(kind);
  await page.getByTestId('sar-name').fill(name);
  if (namespace) await page.getByTestId('sar-namespace').fill(namespace);
  if (groups) await page.getByTestId('sar-groups').fill(groups);
  await page.getByTestId('sar-run').click();
}

test.describe('subject access review', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('a user answer says out loud that it is only about the groups listed', async ({ page }) => {
    await askAbout(page, { name: 'alice', groups: 'platform-admins' });

    const banner = page.getByTestId('sar-groups-incomplete');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('may be fewer than they actually hold');
    await expect(banner).toContainText('platform-admins');
    await expect(page.getByTestId('sar-groups-complete')).toHaveCount(0);
  });

  test("a ServiceAccount answer is complete, and says so", async ({ page }) => {
    await askAbout(page, {
      kind: 'ServiceAccount', name: 'deployer', namespace: 'prod',
    });

    const banner = page.getByTestId('sar-groups-complete');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('system:serviceaccount:prod:deployer');
    await expect(banner).toContainText('system:serviceaccounts:prod');
    await expect(page.getByTestId('sar-groups-incomplete')).toHaveCount(0);
  });

  test('an explicit deny is not shown as an absent grant', async ({ page }) => {
    await askAbout(page, { name: 'alice' });

    // The fixture cycles allowed / denied / not-granted / undecided across the
    // four default checks, so all four renderings are on screen at once.
    await expect(page.getByRole('row', { name: /Allowed/ })).toHaveCount(1);
    await expect(page.getByRole('row', { name: /Denied/ })).toHaveCount(1);
    await expect(page.getByRole('row', { name: /Not granted/ })).toHaveCount(1);
  });

  test('a question the authorizer could not answer is unknown, never a denial', async ({
    page,
  }) => {
    await askAbout(page, { name: 'alice' });

    const banner = page.getByTestId('sar-undecided');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('not refusals');

    // The undecided row shows an em dash rather than any of the three verdicts.
    const rows = page.getByRole('row');
    await expect(rows.filter({ hasText: '—' }).first()).toBeVisible();
  });

  test('the groups field is filled in for a ServiceAccount rather than asked for', async ({
    page,
  }) => {
    await openReview(page);
    await expect(page.getByTestId('sar-groups')).toBeEnabled();

    await page.getByTestId('sar-kind').selectOption('ServiceAccount');
    await expect(page.getByTestId('sar-groups')).toBeDisabled();
    await expect(page.getByTestId('sar-form')).toContainText('assigned by the API server');
  });

  test('asking is blocked until the subject is named', async ({ page }) => {
    await openReview(page);

    await expect(page.getByTestId('sar-run')).toBeDisabled();
    await page.getByTestId('sar-name').fill('alice');
    await expect(page.getByTestId('sar-run')).toBeEnabled();
  });

  test('a ServiceAccount needs its namespace before it can be asked about', async ({ page }) => {
    await openReview(page);
    await page.getByTestId('sar-kind').selectOption('ServiceAccount');
    await page.getByTestId('sar-name').fill('deployer');

    await expect(page.getByTestId('sar-run')).toBeDisabled();
    await page.getByTestId('sar-namespace').fill('prod');
    await expect(page.getByTestId('sar-run')).toBeEnabled();
  });

  test('the review says it was recorded, and names the record', async ({ page }) => {
    await askAbout(page, { name: 'alice' });

    await expect(page.getByTestId('sar-audit')).toContainText('recorded in the audit trail');
    await expect(page.getByTestId('sar-audit')).toContainText('4821');
  });

});

test.describe('a review that fails', () => {
  test('clears the previous answer rather than leaving it stale', async ({ page }) => {
    // A **regex**, not a glob. Every request in this app carries `?cluster_id=`
    // (§1.1), and the glob `**/api/access/subject-review` does not match a URL
    // with a query string — which is why `mockApi`'s `**/api/**` answers and an
    // override written that way is silently never called.
    //
    // Registered after `mockApi` so it takes precedence, and falling through on
    // the first ask so the panel has a real answer to lose.
    let asks = 0;
    await mockApi(page);
    await page.route(/\/api\/access\/subject-review/, async (route) => {
      asks += 1;
      if (asks === 1) return route.fallback();
      return route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'rbac_denied',
          message: 'The console may not create subjectaccessreviews.',
          detail: null,
          hint: 'Grant create on authorization.k8s.io/subjectaccessreviews.',
          context: {},
        }),
      });
    });

    await askAbout(page, { name: 'alice' });
    await expect(page.getByTestId('sar-groups-incomplete')).toBeVisible();

    // A results table under a failed request is a table about a different
    // question, so it goes.
    await page.getByTestId('sar-run').click();

    await expect(page.getByTestId('sar-error')).toContainText('may not create subjectaccessreviews');
    await expect(page.getByTestId('sar-groups-incomplete')).toHaveCount(0);
    await expect(page.getByTestId('sar-audit')).toHaveCount(0);
  });
});
