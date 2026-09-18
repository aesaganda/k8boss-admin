import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * `unsupported` on a tab's *primary* list call renders calm, not red.
 *
 * §1.2 / api-contract.md §11.7: a `501 unsupported` means "this cluster does
 * not serve that API" — the same ordinary fact as no Ingress controller or no
 * `metrics.k8s.io`, and explicitly not an error. Before this test existed,
 * nothing had ever put a CRD-optional resource in a tab's *primary* list call:
 * `PartialBanner` already handled `unsupported` folded into a secondary read's
 * `unavailable[]`, but `DataTable`'s own `error` branch had no matching case,
 * so a whole tab failing with 501 fell through to `ErrorState`'s red "Could
 * not load these resources" panel — indistinguishable from an RBAC denial or a
 * dead cluster. That branch is exactly what Volume Attributes Classes, VPAs
 * and every Gateway (beta) tab hit on a cluster without the relevant CRD.
 */
test.describe('a tab whose primary listing is unsupported', () => {
  test('renders "not present on this cluster", not an error panel', async ({ page }) => {
    await mockApi(page);
    await page.route('**/resources/core/v1/services**', (route) =>
      route.fulfill({
        status: 501,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'unsupported',
          message: 'This cluster does not serve core/v1 services.',
          detail: null,
          hint: 'That group serves v2 on this cluster.',
          context: { group: '', resource: 'services', version: 'v1' },
        }),
      }),
    );

    await page.goto('/network/services');
    await expectPageRendered(page, 'Services');

    await expect(page.getByText('Not present on this cluster')).toBeVisible();
    await expect(page.getByText('That group serves v2 on this cluster.')).toBeVisible();

    // Never the error panel, and never the "why" a real failure would carry.
    await expect(page.getByTestId('error-state')).toHaveCount(0);
    await expect(page.getByText('Could not load these resources')).toHaveCount(0);
  });

  test('every other error code still renders the error panel', async ({ page }) => {
    await mockApi(page);
    await page.route('**/resources/core/v1/services**', (route) =>
      route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'rbac_denied',
          message: 'services is forbidden.',
          detail: null,
          hint: null,
          context: { group: '', resource: 'services', verb: 'list' },
        }),
      }),
    );

    await page.goto('/network/services');
    await expectPageRendered(page, 'Services');

    await expect(page.getByTestId('error-state')).toBeVisible();
    await expect(page.getByText('Could not load these resources')).toBeVisible();
    await expect(page.getByText('Not present on this cluster')).toHaveCount(0);
  });
});
