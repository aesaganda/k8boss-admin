/**
 * VolumeSnapshots (§22).
 *
 * Every assertion here is about a pair of states a convenient rendering would
 * collapse, and on this feature collapsing them is always the same mistake:
 * making somebody believe they have a restorable copy of their data when they
 * do not.
 *
 *   `readyToUse: null` vs `false`   The controller has not reported yet — the
 *                                   first minutes of a large snapshot — versus
 *                                   it looked and the snapshot is unusable.
 *                                   Rendering the first as the second says a
 *                                   backup failed while it is being written.
 *   `readyToUse: null` vs `true`    Far worse in the other direction: it says a
 *                                   restorable snapshot exists when none may,
 *                                   and that is what gets somebody to delete the
 *                                   source volume.
 *   `deletionPolicy` null vs Delete Whether deleting the object later destroys
 *                                   the data. Guessing `Delete` warns about loss
 *                                   that will not happen; guessing `Retain`
 *                                   withholds a warning about loss that will.
 *   `applied: true` vs "taken"      The write creates an object. The storage
 *                                   system takes the snapshot afterwards, and
 *                                   nothing in the response says it did.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

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

async function openTab(page, name) {
  await page.goto('/storage');
  await page.getByRole('tab', { name }).click();
}

async function openSnapshotDialog(page) {
  await openTab(page, 'PersistentVolumeClaims');
  await page.getByRole('row', { name: /postgres-data/ }).getByRole('button').click();
  await page.getByRole('menuitem', { name: 'Snapshot…' }).click();
  await expect(page.getByTestId('snapshot-form')).toBeVisible();
}

test.describe('the snapshots table', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
  });

  test('a snapshot still being taken is neither ready nor failed', async ({ page }) => {
    await openTab(page, 'Volume Snapshots');

    const pending = page.getByRole('row', { name: /postgres-before-upgrade/ });
    await expect(pending).not.toContainText('Ready');
    await expect(pending).not.toContainText('Not ready');
    await expect(pending).toContainText('—');
  });

  test('a ready snapshot and a failed one are told apart', async ({ page }) => {
    await openTab(page, 'Volume Snapshots');

    await expect(page.getByRole('row', { name: /postgres-nightly/ })).toContainText('Ready');
    await expect(page.getByRole('row', { name: /analytics-failed/ })).toContainText('Not ready');
  });

  test('a snapshot with no restore size yet shows an em dash, not zero', async ({ page }) => {
    await openTab(page, 'Volume Snapshots');

    // Scoped to the Restore size cell: the row carries an age and a name that
    // would satisfy a looser assertion about the character '0'.
    const cell = page.getByRole('row', { name: /postgres-before-upgrade/ }).locator('td').nth(4);
    await expect(cell).toHaveText('—');

    // The one that does have a size shows it, so the dash is absence of data
    // rather than this column never rendering anything.
    await expect(page.getByRole('row', { name: /postgres-nightly/ })).toContainText('50');
  });

  test('a snapshot adopted from content is not shown as claim-sourced', async ({ page }) => {
    await openTab(page, 'Volume Snapshots');

    await expect(page.getByRole('row', { name: /imported-2026-08/ })).toContainText(
      'adopted content',
    );
  });

  test('the class table says what deleting a snapshot will do', async ({ page }) => {
    await openTab(page, 'Snapshot Classes');

    await expect(page.getByRole('row', { name: /csi-ebs-retain/ })).toContainText('Retain');
    await expect(page.getByRole('row', { name: /csi-ebs\s/ }).first()).toContainText('Delete');
  });
});

test.describe('taking a snapshot', () => {
  test('the two facts about what a snapshot is are on every one', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
    await openSnapshotDialog(page);

    const consequences = page.getByTestId('snapshot-consequences');
    await expect(consequences).toContainText('A snapshot is not a backup');
    await expect(consequences).toContainText('as if the power were cut');
  });

  test('a Delete policy warns that deleting the object destroys the snapshot', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
    await openSnapshotDialog(page);

    await expect(page.getByTestId('snapshot-ack-snapshot_delete_destroys_data')).toBeVisible();
    await expect(page.getByTestId('snapshot-form')).toContainText('Delete');
  });

  test('a Retain policy raises no deletion warning', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW,
      snapshotOptions: { snapshotClass: FIXTURES.snapshotClasses.items[1] },
    });
    await openSnapshotDialog(page);

    await expect(page.getByTestId('snapshot-consequences')).toBeVisible();
    await expect(page.getByTestId('snapshot-ack-snapshot_delete_destroys_data')).toHaveCount(0);
    await expect(page.getByTestId('snapshot-form')).toContainText('Retain');
  });

  test('an unreadable class is unknown, not assumed either way', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW, snapshotOptions: { snapshotClass: null } });
    await openSnapshotDialog(page);

    await expect(page.getByTestId('snapshot-ack-snapshot_deletion_policy_unknown')).toBeVisible();
    await expect(page.getByTestId('snapshot-ack-snapshot_delete_destroys_data')).toHaveCount(0);
  });

  test('preview is blocked until every consequence is acknowledged', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
    await openSnapshotDialog(page);

    // Wait for the plan before asserting: the dialog re-keys its plan on every
    // keystroke in the name field, and `useAsync` blanks its data on a new key,
    // so an assertion made too early passes on the loading state instead.
    await expect(page.getByTestId('snapshot-consequences')).toBeVisible();

    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'Acknowledge what a snapshot is',
    );

    await page.getByTestId('snapshot-ack-snapshot_is_not_a_backup').check();
    await page.getByTestId('snapshot-ack-snapshot_is_crash_consistent').check();
    await page.getByTestId('snapshot-ack-snapshot_delete_destroys_data').check();
    await expect(preview).toBeEnabled();
  });

  test('a preview does not claim anything was created', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
    await openSnapshotDialog(page);
    await expect(page.getByTestId('snapshot-consequences')).toBeVisible();

    await page.getByTestId('snapshot-ack-snapshot_is_not_a_backup').check();
    await page.getByTestId('snapshot-ack-snapshot_is_crash_consistent').check();
    await page.getByTestId('snapshot-ack-snapshot_delete_destroys_data').check();
    await page.getByTestId('mutation-preview').click();

    await expect(page.getByTestId('mutation-dialog')).toContainText('kind: VolumeSnapshot');
    await expect(page.getByTestId('mutation-summary')).toHaveCount(0);
  });

  test('a completed write says the object exists, not that a snapshot was taken', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW });
    await openSnapshotDialog(page);
    await expect(page.getByTestId('snapshot-consequences')).toBeVisible();

    await page.getByTestId('snapshot-ack-snapshot_is_not_a_backup').check();
    await page.getByTestId('snapshot-ack-snapshot_is_crash_consistent').check();
    await page.getByTestId('snapshot-ack-snapshot_delete_destroys_data').check();
    await page.getByTestId('mutation-preview').click();
    await page.getByTestId('mutation-confirm').click();

    // The one sentence this feature must get right on success.
    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText('object exists');
    await expect(summary).toContainText('readyToUse');
    await expect(summary).not.toContainText('has been taken');
  });
});
