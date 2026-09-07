/**
 * The drain plan's third answer to the budget question (§5, contract §28.5).
 *
 * A PodDisruptionBudget selector can use a `matchExpressions` operator this
 * console does not model. The backend's matcher is tri-state and says so:
 * `pdbUnknown` names those budgets on the pod, and it is deliberately *not* a
 * blocker — the eviction subresource is the enforcer, and refusing a drain over
 * a selector we merely could not parse is how `force` becomes reflex.
 *
 * Both halves of that are load-bearing on this screen, and each fails
 * differently:
 *
 *   silence      The row says "Evict" and its Why cell reads "—", which is this
 *                table's rendering of "no qualification". An operator reads that
 *                as "the plan checked the budgets and none cover this pod". The
 *                plan checked and could not tell. They discover the difference
 *                from an eviction the API server refuses, mid-drain.
 *
 *   a blocker    Confirm goes dead over a selector nobody can read, the operator
 *                reaches for Force to get past it, and Force is now the habit
 *                they bring to the pod that is genuinely blocked.
 */
import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

const NODE = 'ip-10-0-1-4';

function planEntry(pod, overrides = {}) {
  return {
    namespace: 'prod',
    pod,
    action: 'evict',
    reason: '',
    controller: { kind: 'ReplicaSet', name: `${pod}-7d9f8b6c5d` },
    pdb: null,
    pdbUnknown: [],
    result: null,
    ...overrides,
  };
}

async function openPlan(page, plan) {
  await mockApi(page, {
    preflight: (checks) =>
      checks.map((check) => ({
        id: check.id,
        allowed: true,
        reason: '',
        evaluationError: null,
        hint: null,
      })),
  });
  await page.route('**/nodes/*/drain**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        dryRun: true,
        applied: false,
        blocked: plan.filter((entry) => entry.action === 'blocked').length,
        resourceVersion: '12345',
        pdb_checked: true,
        diff: '--- live\n+++ projected\n@@ -1,2 +1,3 @@\n spec:\n+  unschedulable: true\n',
        plan,
        unavailable: [],
      }),
    }),
  );

  await page.goto('/nodes');
  await expect(page.getByRole('grid', { name: 'Nodes' })).toBeVisible();
  await page.getByRole('grid', { name: 'Nodes' }).getByRole('row').nth(1).getByRole('button').last().click();
  await page.getByRole('menuitem', { name: /Drain/ }).click();
  await page.getByRole('button', { name: /Preview changes/ }).click();
  await expect(page.getByRole('grid', { name: 'Drain plan' })).toBeVisible();
}

test('a budget whose selector could not be read is named on the pod, not left blank', async ({ page }) => {
  await openPlan(page, [planEntry('checkout-abcde', { pdbUnknown: ['checkout-guard'] })]);

  const note = page.getByTestId('pdb-unknown-prod/checkout-abcde');
  await expect(note).toBeVisible();
  await expect(note).toContainText('checkout-guard');
  await expect(note).toContainText('could not be determined');
  // The claim that has to survive: this is not a report that no budget covers
  // the pod. The API server may still refuse the eviction.
  await expect(note).toContainText('may refuse the eviction');
});

test('an unreadable budget does not block the drain', async ({ page }) => {
  await openPlan(page, [planEntry('checkout-abcde', { pdbUnknown: ['checkout-guard'] })]);

  await expect(page.getByRole('row', { name: /checkout-abcde/ })).toContainText('Evict');
  // `mutation-blocked` is the banner that disables Confirm and tells the
  // operator to resolve the plan or set Force. It must not fire here.
  await expect(page.getByTestId('mutation-blocked')).toHaveCount(0);
  await page.getByTestId('mutation-typed').fill(NODE);
  await expect(page.getByTestId('mutation-confirm')).toBeEnabled();
});

test('an ordinary pod carries no coverage caveat', async ({ page }) => {
  await openPlan(page, [planEntry('api-abcde', { pdb: 'api-guard' })]);

  // The em dash here is the honest answer — the budgets were read and none of
  // them blocks this pod — so it must not be decorated into a warning.
  await expect(page.getByTestId('pdb-unknown-prod/api-abcde')).toHaveCount(0);
  await expect(page.getByRole('row', { name: /api-abcde/ })).toContainText('—');
});

test('a pod blocked for its own reason carries no coverage caveat either', async ({ page }) => {
  // The row that has a reason *and* nothing undecidable. It reaches the cell by
  // a different route than the em-dash row above — past the early return — so
  // an emptied caveat block would be painted here and nowhere else: a warning
  // strip under a real blocking reason, naming no budget, on the one row an
  // operator is already reading twice.
  await openPlan(page, [
    planEntry('legacy-abcde', {
      action: 'blocked',
      reason: 'unmanaged pod: no controller owns it, so nothing will recreate it anywhere else',
      controller: null,
    }),
  ]);

  const row = page.getByRole('row', { name: /legacy-abcde/ });
  await expect(row).toContainText('unmanaged pod');
  await expect(page.getByTestId('pdb-unknown-prod/legacy-abcde')).toHaveCount(0);
  await expect(row).not.toContainText('could not be determined');
});
