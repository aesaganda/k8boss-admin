/**
 * Autoscaler management (§21).
 *
 * Every assertion here is about a pair of states a convenient rendering would
 * collapse, and on this page collapsing them always produces the same mistake:
 * an autoscaler that is doing nothing looking like one that is working.
 *
 *   `scaling_active: false` vs `true`   The controller cannot compute a desired
 *                                       replica count. `kubectl get hpa` shows
 *                                       `<unknown>` in one column and nothing
 *                                       else looks wrong. Raising the ceiling
 *                                       on one of these stores a number and
 *                                       changes nothing.
 *   `scaling_active: null` vs `false`   A fresh autoscaler the controller has
 *                                       not observed, versus one it has
 *                                       observed and cannot use. Rendering the
 *                                       first as the second reports a healthy
 *                                       autoscaler as broken.
 *   metric `current: null` vs `0`       A CPU target drawn at 0% reads as an
 *                                       idle workload, and idle is the number
 *                                       that argues for scaling *down*.
 *   ceiling below the running count     Not a cap on future growth. Pods
 *                                       terminate at the next scale decision,
 *                                       seconds away, and the form field looks
 *                                       identical to the one that raises it.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

// PatternFly's NumberInput puts both `id` and `data-testid` on its wrapping
// div; the field itself is an unlabelled-by-id `<input type="number">`, which
// is a `spinbutton` carrying the aria-label. Reaching it by accessible name is
// both the working locator and the one that fails if the label is ever dropped.
const numberField = (page, label) => page.getByRole('spinbutton', { name: label });

const ALLOW = (checks) =>
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

async function openHpaTab(page) {
  await page.goto('/config/hpas');
}

async function openBounds(page, name = 'checkout-hpa') {
  await openHpaTab(page);
  await page.getByRole('row', { name: new RegExp(name) }).getByRole('button').click();
  await page.getByRole('menuitem', { name: 'Set bounds…' }).click();
  await expect(page.getByTestId('hpa-form')).toBeVisible();
}

test.describe('autoscalers', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
  });

  test('an autoscaler that cannot read its metric is marked as not scaling', async ({ page }) => {
    await openHpaTab(page);

    const inert = page.getByRole('row', { name: /payments-hpa/ });
    await expect(inert).toContainText('Not scaling');

    const healthy = page.getByRole('row', { name: /checkout-hpa/ });
    await expect(healthy).toContainText('Scaling');
    await expect(healthy).not.toContainText('Not scaling');
  });

  test('an unobserved autoscaler is neither scaling nor broken', async ({ page }) => {
    await openHpaTab(page);

    const fresh = page.getByRole('row', { name: /fresh-hpa/ });
    await expect(fresh).not.toContainText('Not scaling');
    await expect(fresh).not.toContainText('Scaling');
    await expect(fresh).toContainText('—');
  });

  test('a metric with no reading is an em dash, never a zero', async ({ page }) => {
    await openHpaTab(page);

    // The exact rendered form: an em dash where the reading would be, with the
    // target still beside it. `not.toContainText('0%')` would be satisfied by
    // the '70%' target, which is why this asserts the pair.
    await expect(page.getByRole('row', { name: /payments-hpa/ })).toContainText('cpu —/70%');

    // The one that does have a reading shows it, so the dash above is the
    // absence of data rather than this column never rendering anything.
    await expect(page.getByRole('row', { name: /checkout-hpa/ })).toContainText('cpu 81%/70%');
  });

  test('an autoscaler pinned at its ceiling says so', async ({ page }) => {
    await openHpaTab(page);

    await expect(page.getByRole('row', { name: /checkout-hpa/ })).toContainText('at limit');
  });

  test('an unpublished replica count is an em dash rather than zero', async ({ page }) => {
    await openHpaTab(page);

    // Scoped to the Replicas cell: the row also carries an '80%' target and a
    // '20s' age, so a row-wide assertion about the character '0' proves nothing.
    // `locator('td')` rather than `getByRole('cell')` — PatternFly renders these
    // tables as `role="grid"`, where a `<td>` is a `gridcell`. See
    // table-density.spec.js, which documents the same trap.
    const replicas = page.getByRole('row', { name: /fresh-hpa/ }).locator('td').nth(4);
    await expect(replicas).toHaveText('—');
    await expect(page.getByRole('row', { name: /fresh-hpa/ })).toContainText('1–5');
  });

  test('the dialog leads with whether the autoscaler is scaling at all', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW,
      autoscalerOptions: { autoscaler: FIXTURES.autoscalers.items[1] },
    });
    await openBounds(page, 'payments-hpa');

    const inert = page.getByTestId('hpa-inert');
    await expect(inert).toBeVisible();
    await expect(inert).toContainText('FailedGetResourceMetric');
    await expect(inert).toContainText('nothing will act on them');
  });

  test('lowering the ceiling below the running count says how many pods go', async ({ page }) => {
    await openBounds(page);

    // 8 running, ceiling to 4.
    await numberField(page, 'Maximum replicas').fill('4');
    await expect(page.getByTestId('hpa-consequences')).toContainText(
      '4 pod(s) are terminated as soon as this is written',
    );
    await expect(page.getByTestId('hpa-consequences')).toContainText('it is a scale-down now');
  });

  test('preview is blocked until every consequence is acknowledged', async ({ page }) => {
    await openBounds(page);
    await numberField(page, 'Maximum replicas').fill('4');

    // Wait for the plan to land before asserting. Changing a bound re-keys the
    // plan and `useAsync` blanks its data on a new key, so a disabled-button
    // assertion made immediately after the fill passes on the *loading* state
    // and proves nothing about the acknowledgement gate.
    await expect(page.getByTestId('hpa-consequences')).toBeVisible();

    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'Acknowledge what this change means',
    );

    await page.getByTestId('hpa-ack-hpa_max_below_current').check();
    await expect(preview).toBeEnabled();
  });

  test('a change that moves neither bound is blocked, with the bounds still shown', async ({
    page,
  }) => {
    await openBounds(page);

    await expect(page.getByTestId('hpa-blocked')).toContainText('already runs between 2 and 10');
    await expect(page.getByTestId('mutation-preview')).toBeDisabled();
    // The autoscaler is still described — that is the point of `blocked` over a 422.
    await expect(page.getByTestId('hpa-form')).toContainText('81%');
  });

  test('a preview does not claim anything was applied', async ({ page }) => {
    await openBounds(page);
    await numberField(page, 'Maximum replicas').fill('30');
    await page.getByTestId('mutation-preview').click();

    // The projected diff is on screen and nothing claims a write happened:
    // `mutation-summary` is the applied banner, and a dry run must not show it.
    await expect(page.getByTestId('mutation-dialog')).toContainText('maxReplicas: 30');
    await expect(page.getByTestId('mutation-summary')).toHaveCount(0);
    await expect(page.getByTestId('mutation-confirm')).toBeVisible();
  });
});

test.describe('scaling a workload an autoscaler owns', () => {
  test('an autoscaler listing that failed never reads as “nothing will undo this”', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW,
      scaleGovernedBy: {
        governed: null,
        autoscaler: null,
        reason: 'forbidden',
        detail:
          'The autoscaler listing for prod did not answer (forbidden), so this console cannot ' +
          'say whether a HorizontalPodAutoscaler will put this replica count back. It is not ' +
          'saying that none will.',
      },
    });
    await page.goto('/workloads');
    await page.getByRole('row', { name: /checkout/ }).first().getByRole('button').click();
    await page.getByRole('menuitem', { name: /Scale/ }).click();
    await numberField(page, 'Desired replicas').fill('12');
    await page.getByTestId('mutation-preview').click();

    const banner = page.getByTestId('scale-governed');
    await expect(banner).toHaveAttribute('data-governed', 'unknown');
    await expect(banner).toContainText('not saying that none will');
  });

  test('a workload nothing autoscales gets no banner at all', async ({ page }) => {
    // `governed: false` is the ordinary case and good news. A console that
    // alerted on it would train people straight past the two that matter.
    await mockApi(page, {
      preflight: ALLOW,
      scaleGovernedBy: {
        governed: false, autoscaler: null, reason: null,
        detail: 'No HorizontalPodAutoscaler in prod targets this Deployment.',
      },
    });
    await page.goto('/workloads');
    await page.getByRole('row', { name: /checkout/ }).first().getByRole('button').click();
    await page.getByRole('menuitem', { name: /Scale/ }).click();
    await numberField(page, 'Desired replicas').fill('12');
    await page.getByTestId('mutation-preview').click();

    await expect(page.getByTestId('mutation-dialog')).toContainText('replicas: 12');
    await expect(page.getByTestId('scale-governed')).toHaveCount(0);
  });

  test('the scale dialog names the autoscaler that will put the count back', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
    await page.goto('/workloads');
    await page.getByRole('row', { name: /checkout/ }).first().getByRole('button').click();
    await page.getByRole('menuitem', { name: /Scale/ }).click();

    await numberField(page, 'Desired replicas').fill('12');
    await page.getByTestId('mutation-preview').click();

    const banner = page.getByTestId('scale-governed');
    await expect(banner).toHaveAttribute('data-governed', 'true');
    await expect(banner).toContainText('overrides the count set here');
  });
});
