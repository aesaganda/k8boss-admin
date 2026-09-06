/**
 * Quota advice (§29).
 *
 * A ResourceQuota refuses at admission and the refusal is a 403 somebody parses
 * under pressure. This panel answers first, and every assertion is about a pair
 * the 403 makes look alike:
 *
 *   no room vs must specify   The first is "the quota is full" and is fixed by
 *                             deleting something or raising the limit. The
 *                             second fires with the quota barely used, because
 *                             a quota bounding a resource makes it COMPULSORY —
 *                             and is fixed by a LimitRange, an object the
 *                             message never names. Sending someone to raise a
 *                             limit that is fine wastes the afternoon the
 *                             message already cost them.
 *
 *   refused vs unknown        An unwritten `status.used` or a scoped quota is
 *                             not "probably fine". Drawing it as admitted gives
 *                             the roomiest answer at the moment least is known,
 *                             to the person about to deploy.
 *
 *   which limit               "Refused" does not say by how much or which of
 *                             eight bounds was the one. That is the whole panel.
 */
import { expect, test } from '@playwright/test';

import { QUOTA_ADVICE, mockApi } from './fixtures.js';

async function openPage(page, options = {}) {
  await mockApi(page, options);
  await page.goto('/namespaces/prod');
  await expect(page.getByTestId('quota-advisor')).toBeVisible();
}

async function check(page, { replicas = '1', cpu = '', memory = '' } = {}) {
  await page.getByTestId('quota-replicas').fill(replicas);
  if (cpu) await page.getByTestId('quota-cpu').fill(cpu);
  if (memory) await page.getByTestId('quota-memory').fill(memory);
  await page.getByTestId('quota-run').click();
  await expect(page.getByTestId('quota-verdict')).toBeVisible();
}

test.describe('the compulsory-resource trap', () => {
  test('it says what every pod must state, before anything is checked', async ({ page }) => {
    await openPage(page);

    // Visible without running a preview: it refuses pods at any level of quota
    // usage, so it is not conditional on somebody asking.
    await expect(page.getByTestId('quota-mandatory')).toContainText('requests.cpu');
    await expect(page.getByTestId('quota-finding-quota_requires_unset_resource'))
      .toContainText('must specify');
  });

  test('a container omitting it is refused separately from the headroom checks',
    async ({ page }) => {
      await openPage(page);
      // Memory is defaulted by the LimitRange; cpu is not. One replica, so
      // nothing is close to a bound.
      await check(page, { replicas: '1' });

      const banner = page.getByTestId('quota-must-specify');
      await expect(banner).toContainText('requests.cpu');
      await expect(banner).toContainText('whatever the quota usage is');
      await expect(banner).toContainText('LimitRange');
      // And the cpu bound itself has room — so this is provably not a headroom
      // refusal, which is the distinction the panel exists to draw.
      await expect(page.getByTestId('quota-check-team-requests.cpu')).toContainText('admitted');
      await expect(page.getByTestId('quota-verdict')).toHaveAttribute('data-verdict', 'refused');
    });

  test('a declared value clears it', async ({ page }) => {
    await openPage(page);
    await check(page, { replicas: '1', cpu: '100m' });

    await expect(page.getByTestId('quota-must-specify')).toHaveCount(0);
    await expect(page.getByTestId('quota-verdict')).toHaveAttribute('data-verdict', 'admitted');
  });
});

test.describe('which limit refuses it', () => {
  test('the tight bound is named and the roomy ones are not', async ({ page }) => {
    await openPage(page);
    // 3 × 500m = 1.5 cores against 1 core of headroom.
    await check(page, { replicas: '3', cpu: '500m' });

    await expect(page.getByTestId('quota-verdict')).toHaveAttribute('data-verdict', 'refused');
    await expect(page.getByTestId('quota-check-team-requests.cpu')).toContainText('refused');
    // The two with room say so, so the operator knows what to change.
    await expect(page.getByTestId('quota-check-team-pods')).toContainText('admitted');
    await expect(page.getByTestId('quota-check-team-requests.memory'))
      .toContainText('admitted');
  });

  test('the headroom is on screen, which a 403 never carries', async ({ page }) => {
    await openPage(page);
    await check(page, { replicas: '3', cpu: '500m' });

    // Located by the verdict cell's testid rather than by accessible name:
    // several rows mention a resource, and the one that matters is the one
    // carrying this check.
    const row = page
      .getByRole('row')
      .filter({ has: page.getByTestId('quota-check-team-requests.cpu') });
    await expect(row).toContainText('1.5');
    await expect(row).toContainText('1');
  });

  test('a workload that fits is admitted', async ({ page }) => {
    await openPage(page);
    await check(page, { replicas: '1', cpu: '500m' });

    const verdict = page.getByTestId('quota-verdict');
    await expect(verdict).toHaveAttribute('data-verdict', 'admitted');
    await expect(verdict).toHaveClass(/pf-m-success/);
  });
});

test.describe('unknown is rendered as unknown', () => {
  test('an unwritten usage is not drawn as admitted', async ({ page }) => {
    await openPage(page, {
      quotaAdvice: {
        ...QUOTA_ADVICE,
        quotas: [{
          ...QUOTA_ADVICE.quotas[0],
          resources: [{ resource: 'requests.cpu', hard: '10', used: null,
                        hard_value: '10', used_value: null, exhausted: null }],
          headroom: { 'requests.cpu': null },
          findings: [{
            code: 'quota_usage_unknown',
            label: 'How much of this quota is used is unknown',
            detail: 'The quota controller has not written status.used for requests.cpu.',
          }],
        }],
        mandatory: ['requests.cpu'],
        containerDefaults: { 'requests.cpu': '100m' },
        findings: [],
      },
    });
    await check(page, { replicas: '1' });

    const verdict = page.getByTestId('quota-verdict');
    await expect(verdict).toHaveAttribute('data-verdict', 'unknown');
    await expect(verdict).toContainText('not a');
    // And drawn as unknown, not as success. The attribute alone would let an
    // `unknown` render in green — the roomiest possible impression at the
    // moment the console knows least, which is the whole failure this verdict
    // exists to avoid.
    await expect(verdict).toHaveClass(/pf-m-warning/);
    await expect(verdict).not.toHaveClass(/pf-m-success/);
    await expect(
      page.getByRole('row').filter({ has: page.getByTestId('quota-check-team-requests.cpu') }),
    ).toContainText('usage not written');
  });

  test('a refused quota listing is not drawn as an unbounded namespace',
    async ({ page }) => {
      await openPage(page, {
        quotaAdvice: {
          ...QUOTA_ADVICE, quotas: null, mandatory: null, findings: [], partial: true,
          unavailable: [{ group: '', resource: 'resourcequotas', namespace: 'prod',
                          error: 'rbac_denied', reason: 'forbidden',
                          message: 'forbidden', hint: null }],
        },
      });

      await expect(page.getByTestId('quota-listing-unavailable'))
        .toContainText('not the same as');
    });

  test('a namespace with no quota says so as a finding', async ({ page }) => {
    await openPage(page, {
      quotaAdvice: {
        ...QUOTA_ADVICE, quotas: [], mandatory: [], containerDefaults: {}, findings: [],
      },
    });

    await expect(page.getByText('Nothing bounds this namespace').first()).toBeVisible();
  });
});

test.describe('the request that goes out', () => {
  test('replicas and the container requests ride on the body', async ({ page }) => {
    const previews = [];
    await openPage(page, { quotaPreviews: previews });
    await check(page, { replicas: '4', cpu: '250m', memory: '512Mi' });

    await expect.poll(() => previews.length).toBeGreaterThan(0);
    expect(previews[0].replicas).toBe(4);
    expect(previews[0].containers[0].requests).toEqual({ cpu: '250m', memory: '512Mi' });
  });
});
