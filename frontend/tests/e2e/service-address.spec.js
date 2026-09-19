import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

/**
 * The Services tab's External column, and its two empty states.
 *
 * A ClusterIP Service publishes no external address because that is what a
 * ClusterIP Service is. A LoadBalancer with none is one nothing has claimed,
 * and on a cluster with no load-balancer provider — every kind, k3s or Docker
 * Desktop cluster by default — it stays that way permanently while the object
 * reports no error at all. Both arrive here as the same empty `externalIPs`,
 * so the only thing keeping them apart on screen is this branch.
 *
 * The third row is the one that stops the branch from over-claiming: a
 * LoadBalancer that *does* have an address is not pending, and must not pick up
 * the sentence just for being a LoadBalancer.
 */
const SERVICES = {
  items: [
    {
      name: 'internal-api',
      namespace: 'prod',
      type: 'ClusterIP',
      clusterIP: '10.96.0.11',
      externalIPs: [],
      ports: [],
      selector: { app: 'internal-api' },
      age_seconds: 60,
      endpoint_count: 1,
    },
    {
      name: 'waiting-lb',
      namespace: 'prod',
      type: 'LoadBalancer',
      clusterIP: '10.96.0.12',
      externalIPs: [],
      ports: [],
      selector: { app: 'waiting-lb' },
      age_seconds: 60,
      endpoint_count: 1,
    },
    {
      name: 'answered-lb',
      namespace: 'prod',
      type: 'LoadBalancer',
      clusterIP: '10.96.0.13',
      externalIPs: ['192.168.165.240'],
      ports: [],
      selector: { app: 'answered-lb' },
      age_seconds: 60,
      endpoint_count: 1,
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

test.describe('Services — the External column', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { services: SERVICES });
    await page.goto('/network/services');
  });

  test('a LoadBalancer with no address is pending, and a ClusterIP is not', async ({ page }) => {
    await expect(page.getByRole('row', { name: /waiting-lb/ })).toContainText('pending');

    // "none" here is a complete answer, and must not be dressed up as a wait.
    const clusterIp = page.getByRole('row', { name: /internal-api/ });
    await expect(clusterIp).toContainText('none');
    await expect(clusterIp).not.toContainText('pending');

    const answered = page.getByRole('row', { name: /answered-lb/ });
    await expect(answered).toContainText('192.168.165.240');
    await expect(answered).not.toContainText('pending');
  });

  test('the filter finds the Services that are still waiting', async ({ page }) => {
    await page.getByPlaceholder('Filter services…').fill('pending');

    await expect(page.getByRole('row', { name: /waiting-lb/ })).toBeVisible();
    await expect(page.getByRole('row', { name: /internal-api/ })).toHaveCount(0);
    await expect(page.getByRole('row', { name: /answered-lb/ })).toHaveCount(0);
  });
});
