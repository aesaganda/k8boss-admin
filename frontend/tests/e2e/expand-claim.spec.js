/**
 * Expanding a PersistentVolumeClaim (§20).
 *
 * Every assertion here is about a pair of states that a convenient rendering
 * would collapse, and on this screen collapsing them costs an incident rather
 * than a click.
 *
 *   request vs capacity          what the claim asks for versus what the volume
 *                                provides. They agree on a settled claim and
 *                                differ on one mid-expansion, and only the gap
 *                                says an earlier resize has not finished.
 *   `applied: true` vs more disk `applied` attests the request changed. A
 *                                dialog that closed on a green result without
 *                                saying so leaves an operator believing a
 *                                database has room it does not have.
 *   `supported: false` vs `null` the first refuses the write and names the
 *                                class. The second says we could not find out —
 *                                and does not refuse, because refusing would
 *                                send somebody to fix a StorageClass that is
 *                                already correct.
 *   `mountedBy: []` vs `null`    "nothing has this open" versus "we could not
 *                                look". The first starts an offline resize.
 *
 * A blocked plan is asserted to still render the claim: the plan answers 200
 * with `blocked` rather than 422 precisely so a too-small number does not
 * replace the current size, the capacity and the mounts with an error panel, on
 * the screen where those three facts are the decision.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

const CLAIMS = FIXTURES.claims.items;

/** §9 says yes to everything, so the row action is enabled and §20 is reachable. */
const ALLOW_ALL = (checks) =>
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

/** Open the expand dialog on one claim from the Storage page's first tab. */
async function openExpand(page, name = 'postgres-data') {
  await page.goto('/storage');
  const row = page.getByRole('row', { name: new RegExp(name) });
  await row.getByRole('button', { name: 'Kebab toggle' }).click();
  await page.getByRole('menuitem', { name: 'Expand…' }).click();
  await expect(page.getByTestId('pvc-form')).toBeVisible();
}

test.describe('expanding a claim', () => {
  test('the table shows what a claim asked for beside what it got', async ({ page }) => {
    await mockApi(page);
    await page.goto('/storage');

    // Asserted per cell rather than per row. A row-level `toContainText('—')`
    // passes on the em dash in some *other* null column, which is exactly how a
    // capacity that had quietly borrowed the request would go unnoticed.
    const cell = (row, column) => row.locator(`td[data-label="${column}"]`);

    // Mid-expansion: 100Gi requested, 50 GiB provided. The gap is the finding.
    const midway = page.getByRole('row', { name: /metrics-data/ });
    await expect(cell(midway, 'Requested')).toHaveText('100 GiB');
    await expect(cell(midway, 'Capacity')).toHaveText('50.0 GiB');

    // Settled: the two agree, and that is what "nothing in flight" looks like.
    const settled = page.getByRole('row', { name: /postgres-data/ });
    await expect(cell(settled, 'Requested')).toHaveText('50.0 GiB');
    await expect(cell(settled, 'Capacity')).toHaveText('50.0 GiB');

    // Pending: it asked for 10Gi and nothing has been provisioned. The request
    // must not stand in for a capacity that does not exist.
    const pending = page.getByRole('row', { name: /unbound-data/ });
    await expect(cell(pending, 'Requested')).toHaveText('10.0 GiB');
    await expect(cell(pending, 'Capacity')).toHaveText('—');
  });

  test('the dialog seeds from the claim and says why that size is refused', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openExpand(page);

    // Seeded with the claim's own request, which blocks — and the refusal is the
    // instruction rather than an error that hides the claim.
    await expect(page.getByTestId('pvc-size')).toHaveValue('50Gi');
    await expect(page.getByTestId('pvc-blocked')).toContainText('already requests 50Gi');
    await expect(page.getByTestId('pvc-current-requested')).toContainText('50Gi');
    await expect(page.getByTestId('pvc-current-capacity')).toContainText('50.0 GiB');
    await expect(page.getByRole('button', { name: /Preview/ })).toBeDisabled();
  });

  test('a shrink is refused with the claim still on screen', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openExpand(page);

    await page.getByTestId('pvc-size').fill('10Gi');

    await expect(page.getByTestId('pvc-blocked')).toContainText('cannot be shrunk');
    // The three facts that decide what size to ask for instead are all still here.
    await expect(page.getByTestId('pvc-current-requested')).toContainText('50Gi');
    await expect(page.getByTestId('pvc-current-capacity')).toContainText('50.0 GiB');
    await expect(page.getByTestId('pvc-consequences')).toHaveCount(0);
  });

  test('a class that forbids expansion refuses and names it', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      expandOptions: {
        expansion: {
          supported: false,
          storage_class: 'gp2',
          reason: 'not_allowed',
          detail: 'StorageClass gp2 does not set allowVolumeExpansion, so the API server refuses any change to this claim’s requested size.',
        },
      },
    });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    const notice = page.getByTestId('pvc-expansion');
    await expect(notice).toHaveAttribute('data-supported', 'false');
    await expect(notice).toContainText('gp2');
    await expect(page.getByTestId('pvc-blocked')).toContainText('does not allow volume expansion');
  });

  test('a class that could not be read is unknown and does not refuse', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      expandOptions: {
        expansion: {
          supported: null,
          storage_class: 'gp3',
          reason: 'unreadable',
          detail: 'The StorageClass gp3 could not be read, so whether it permits expansion is unknown.',
        },
      },
    });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    const notice = page.getByTestId('pvc-expansion');
    await expect(notice).toHaveAttribute('data-supported', 'null');
    await expect(notice).toContainText('unknown');
    // Not a refusal: only `false` refuses. Absence of the blocked panel is not
    // enough to prove that — the write has to actually be reachable, which is
    // what an implementation collapsing `null` into `false` would take away.
    await expect(page.getByTestId('pvc-blocked')).toHaveCount(0);
    await page.getByTestId('pvc-ack-pvc_capacity_is_not_immediate').click();
    await page.getByTestId('pvc-ack-pvc_expansion_is_one_way').click();
    await page.getByTestId('pvc-ack-pvc_expansion_unknown').click();
    await expect(page.getByRole('button', { name: /Preview/ })).toBeEnabled();
  });

  test('a claim nothing mounts says so, and one we could not check does not', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: [] } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    await expect(page.getByTestId('pvc-mounts-none')).toBeVisible();
    await expect(page.getByTestId('pvc-ack-pvc_in_use_offline_resize')).toHaveCount(0);
  });

  test('a refused pod listing is an em dash, never an empty mount list', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: null } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    await expect(page.getByTestId('pvc-mounts-unknown')).toBeVisible();
    await expect(page.getByTestId('pvc-mounts-none')).toHaveCount(0);
    await expect(page.getByTestId('pvc-ack-pvc_mounts_unknown')).toBeVisible();
    await expect(page.getByTestId('partial-banner')).toBeVisible();
  });

  test('a mounted volume warns that the filesystem may wait for a restart', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: ['postgres-0'] } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    await expect(page.getByTestId('pvc-mounts')).toContainText('postgres-0');
    const ack = page.getByTestId('pvc-ack-pvc_in_use_offline_resize');
    await expect(ack).toBeVisible();
    await expect(page.getByTestId('pvc-consequences')).toContainText('FileSystemResizePending');
  });

  test('Preview stays disabled until every consequence is acknowledged', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: [] } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    const preview = page.getByRole('button', { name: /Preview/ });
    await expect(preview).toBeDisabled();

    await page.getByTestId('pvc-ack-pvc_capacity_is_not_immediate').click();
    await expect(preview).toBeDisabled();
    await page.getByTestId('pvc-ack-pvc_expansion_is_one_way').click();
    await expect(preview).toBeEnabled();
  });

  test('changing the size clears the acknowledgements', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: [] } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');

    await page.getByTestId('pvc-ack-pvc_capacity_is_not_immediate').click();
    await page.getByTestId('pvc-ack-pvc_expansion_is_one_way').click();
    await expect(page.getByRole('button', { name: /Preview/ })).toBeEnabled();

    // Consent given for 100Gi is not consent for 500Gi.
    await page.getByTestId('pvc-size').fill('500Gi');
    await expect(page.getByRole('button', { name: /Preview/ })).toBeDisabled();
  });

  test('the preview shows the diff and says the capacity is not what changed', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: [] } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');
    await page.getByTestId('pvc-ack-pvc_capacity_is_not_immediate').click();
    await page.getByTestId('pvc-ack-pvc_expansion_is_one_way').click();
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect(page.getByTestId('pvc-outcome-requested')).toContainText('100Gi');
    // Read from before the write, and the number a workload actually has.
    await expect(page.getByTestId('pvc-outcome-capacity')).toContainText('50.0 GiB');
    await expect(page.getByTestId('pvc-outcome')).toContainText(
      'no write from this console changes it directly',
    );
  });

  test('after the write the two numbers still differ, and the dialog says so', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { mountedBy: [] } });
    await openExpand(page);
    await page.getByTestId('pvc-size').fill('100Gi');
    await page.getByTestId('pvc-ack-pvc_capacity_is_not_immediate').click();
    await page.getByTestId('pvc-ack-pvc_expansion_is_one_way').click();
    await page.getByRole('button', { name: /Preview/ }).click();
    await page.getByRole('button', { name: /Expand the claim/ }).click();

    await expect(page.getByTestId('pvc-outcome')).toContainText('What changed, and what has not');
    await expect(page.getByTestId('pvc-outcome-requested')).toContainText('100Gi');
    await expect(page.getByTestId('pvc-outcome-capacity')).toContainText('50.0 GiB');
  });

  test('the Pending claim cannot be expanded and says why', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, expandOptions: { claim: CLAIMS[2] } });
    await openExpand(page, 'unbound-data');
    await page.getByTestId('pvc-size').fill('100Gi');

    await expect(page.getByTestId('pvc-blocked')).toContainText('no volume to expand');
    await expect(page.getByRole('button', { name: /Preview/ })).toBeDisabled();
  });
});
