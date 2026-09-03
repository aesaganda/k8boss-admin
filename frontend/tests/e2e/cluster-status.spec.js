/**
 * The cluster status page (§19).
 *
 * Every assertion here is about a pair of states that a convenient rendering
 * would collapse, and on this page collapsing them is always the same mistake:
 * reporting a question we could not ask as a question with a reassuring answer.
 *
 *   `null` section vs `[]`          "we could not read the admission webhooks"
 *                                   versus "nothing is intercepting writes to
 *                                   this cluster". Both draw an empty area; one
 *                                   is the good news and the other is a hole in
 *                                   what we know.
 *   `stale: null` vs `stale: false` a lease nobody has acquired versus one being
 *                                   renewed. Rendering the first as the second
 *                                   is a green tick for a component that has
 *                                   never run.
 *   `endpoint_count: null` vs `0`   a URL-addressed webhook has nothing in the
 *                                   cluster to count; a zero there is the single
 *                                   finding this page exists to make, and would
 *                                   be a false alarm about a webhook that is
 *                                   answering perfectly well.
 *
 * The absence of a rollup is asserted too. OpenShift's console can put one
 * verdict at the top because `ClusterOperator` gives it one; vanilla has five
 * unrelated signals, and a single badge over them would have to guess.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

/** The §19 response with one section replaced, keeping the rest populated. */
function statusWith(overrides) {
  return { ...FIXTURES.clusterStatus, ...overrides };
}

test.describe('cluster status', () => {
  test('a lease that stopped renewing is called out, and one nobody holds is not', async ({
    page,
  }) => {
    await mockApi(page);
    await page.goto('/cluster-status');

    const scheduler = page.getByRole('row', { name: /kube-scheduler/ });
    await expect(scheduler).toContainText('Not renewing');

    const healthy = page.getByRole('row', { name: /kube-controller-manager/ });
    await expect(healthy).toContainText('Renewing');
    await expect(healthy).not.toContainText('Not renewing');

    // Never acquired: neither verdict, because the lease carries no renewTime
    // and "stale" would be a claim about a component nobody has checked.
    const unheld = page.getByRole('row', { name: /external-dns-controller/ });
    await expect(unheld).not.toContainText('Renewing');
    await expect(unheld).toContainText('—');
  });

  test('an unavailable aggregated API says which one and why', async ({ page }) => {
    await mockApi(page);
    await page.goto('/cluster-status');

    const metrics = page.getByRole('row', { name: /v1beta1\.metrics\.k8s\.io/ });
    await expect(metrics).toContainText('Unavailable');
    await expect(metrics).toContainText('FailedDiscoveryCheck');
    await expect(metrics).toContainText('kube-system/metrics-server');
  });

  test('a Fail webhook with no backends is marked as refusing writes', async ({ page }) => {
    await mockApi(page);
    await page.goto('/cluster-status');

    await expect(page.getByTestId('status-webhooks')).toHaveAttribute('data-blocking', '1');

    const blocking = page.getByRole('row', { name: /validate\.policy\.example\.test/ });
    await expect(blocking).toContainText('blocking writes');

    // Same zero would be wrong here: Ignore means the write is admitted
    // unchecked when the webhook does not answer, which is a different fact.
    const ignored = page.getByRole('row', { name: /inject\.sidecar\.example\.test/ });
    await expect(ignored).toContainText('Ignore');
    await expect(ignored).not.toContainText('blocking writes');
  });

  test('a URL-addressed webhook shows an em dash, never a zero', async ({ page }) => {
    await mockApi(page);
    await page.goto('/cluster-status');

    const external = page.getByRole('row', { name: /audit\.external\.example\.test/ });
    await expect(external).toContainText('—');
    await expect(external).not.toContainText('blocking writes');
  });

  test('a kubelet outside the supported skew is named, and the ones inside it are not', async ({
    page,
  }) => {
    await mockApi(page);
    await page.goto('/cluster-status');

    const skew = page.getByTestId('status-skew');
    await expect(skew).toHaveAttribute('data-out-of-skew', '1');
    await expect(skew).toContainText('v1.31.4');
    await expect(skew).toContainText('outside supported skew');

    // The distribution-suffixed version parses, so this node is not flagged.
    await expect(skew).toContainText('v1.30.6-eks-abc1234');
  });

  test('a section that could not be read is a failure panel, not an empty one', async ({
    page,
  }) => {
    await mockApi(page, {
      clusterStatus: statusWith({
        webhooks: null,
        partial: true,
        unavailable: [
          {
            headline: 'Admission webhooks could not be listed',
            group: 'admissionregistration.k8s.io',
            resource: 'validatingwebhookconfigurations',
            namespace: null,
            reason: 'forbidden',
            detail: 'admissionregistration.k8s.io is forbidden to the console service account',
          },
        ],
      }),
    });
    await page.goto('/cluster-status');

    const panel = page.getByTestId('status-validatingwebhookconfigurations-unavailable');
    await expect(panel).toContainText('not permitted');
    await expect(panel).toContainText('is unknown');

    // The reassuring empty state must not be reachable from a failed read.
    await expect(page.getByText('Nothing is intercepting writes')).toHaveCount(0);
    await expect(page.getByTestId('partial-banner')).toBeVisible();
  });

  test('an empty section says the cluster has none, and is not a failure', async ({ page }) => {
    await mockApi(page, {
      clusterStatus: statusWith({
        webhooks: { items: [], blocking_count: 0, complete: true },
      }),
    });
    await page.goto('/cluster-status');

    await expect(page.getByText('Nothing is intercepting writes')).toBeVisible();
    await expect(
      page.getByTestId('status-validatingwebhookconfigurations-unavailable'),
    ).toHaveCount(0);
    await expect(page.getByTestId('status-webhooks')).toHaveAttribute('data-blocking', '0');
  });

  test('one webhook listing answering and the other not says the rows are incomplete', async ({
    page,
  }) => {
    await mockApi(page, {
      clusterStatus: statusWith({
        webhooks: { ...FIXTURES.clusterStatus.webhooks, complete: false },
        partial: true,
        unavailable: [
          {
            headline: 'Mutating webhooks could not be listed',
            group: 'admissionregistration.k8s.io',
            resource: 'mutatingwebhookconfigurations',
            namespace: null,
            reason: 'forbidden',
            detail: null,
          },
        ],
      }),
    });
    await page.goto('/cluster-status');

    await expect(page.getByTestId('status-webhooks-partial')).toContainText('not all of them');
  });

  test('a withheld verdict is dropped from the finding count, not counted as zero', async ({
    page,
  }) => {
    // The EndpointSlice listing was refused, so `blocking_count` is null: the
    // webhook rows are real but nothing can be said about which of them is
    // refusing writes. The count in the subtitle must lose a *check*, not gain
    // a zero — "5 findings across 5 checks" would be a page claiming it looked.
    await mockApi(page, {
      clusterStatus: statusWith({
        webhooks: {
          items: FIXTURES.clusterStatus.webhooks.items.map((row) => ({
            ...row,
            endpoint_count: null,
          })),
          blocking_count: null,
          complete: true,
        },
        partial: true,
        unavailable: [
          {
            headline: 'Endpoints could not be listed',
            group: 'discovery.k8s.io',
            resource: 'endpointslices',
            namespace: null,
            reason: 'forbidden',
            detail: null,
          },
        ],
      }),
    });
    await page.goto('/cluster-status');

    await expect(page.getByText(/4 findings across 4 of 5 checks/)).toBeVisible();
    await expect(page.getByText(/blocking writes/)).toHaveCount(0);
    await expect(page.getByTestId('partial-banner')).toBeVisible();
  });

  test('the page counts findings and never renders one verdict for the cluster', async ({
    page,
  }) => {
    await mockApi(page);
    await page.goto('/cluster-status');

    // 1 blocking webhook + 1 unavailable API + 1 unhealthy CRD + 1 skewed node
    // + 1 stale lease. Counted, because a rollup would have to decide what a
    // stale cloud-controller-manager lease means, and that depends on a cluster
    // this console has never seen.
    await expect(page.getByText(/5 findings across 5 of 5 checks/)).toBeVisible();
    await expect(page.getByText(/^Healthy$/)).toHaveCount(0);
  });

  test('the sidebar reaches the page', async ({ page }) => {
    await mockApi(page);
    await page.goto('/');

    await page.getByRole('link', { name: 'Status', exact: true }).click();
    await expect(page).toHaveURL(/\/cluster-status$/);
    await expect(page.getByRole('heading', { name: 'Cluster status' })).toBeVisible();
  });
});
