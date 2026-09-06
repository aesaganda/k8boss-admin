/**
 * Deleting a namespace, and what goes with it (§26).
 *
 * `kubectl delete namespace prod` prints one line. §4's delete dialog shows the
 * namespace object's YAML disappearing, which is a preview of a metadata block:
 * nothing on that screen is the database, the address, or the admission webhook
 * that goes with it. Every assertion here is about something that is on this
 * screen and on no other:
 *
 *   `—` vs `0`             A kind whose listing was refused is an em dash. "This
 *                          namespace holds no PersistentVolumeClaims" is the
 *                          sentence that ends with a deleted database, and this
 *                          dialog is the last place it can be prevented.
 *
 *   destroyed vs kept      Decided by the volume's reclaim policy, on a
 *                          cluster-scoped object nobody is looking at. They are
 *                          opposite outcomes and are drawn as opposite things —
 *                          and a volume this console could not read is drawn as
 *                          neither.
 *
 *   `[]` vs `null`         "No admission webhook points here" versus "we could
 *                          not look". At the v1 default of `failurePolicy: Fail`
 *                          the second one ends with a cluster that stops
 *                          accepting writes.
 *
 *   started vs finished    `applied: true` is a deletionTimestamp. A finalizer
 *                          whose controller is not running holds the namespace
 *                          in Terminating indefinitely, and the summary says so
 *                          rather than reporting a deletion that has not
 *                          happened.
 */
import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi, namespaceDeletePlanFor } from './fixtures.js';

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

const DENY_DELETE = (checks) =>
  checks.map((check) => ({
    verb: check.verb,
    group: check.group,
    resource: check.resource,
    namespace: check.namespace ?? null,
    subresource: check.subresource ?? null,
    allowed: check.verb !== 'delete',
    reason: check.verb === 'delete' ? 'no RBAC policy matched' : '',
    evaluationError: null,
    hint:
      check.verb === 'delete'
        ? "Grant `delete` on `core/namespaces` to the console's ServiceAccount."
        : null,
  }));

const ALL_CODES = namespaceDeletePlanFor('prod').consequences.map((entry) => entry.code);

async function openDialog(page, options = {}) {
  await mockApi(page, { preflight: ALLOW_ALL, ...options });
  await page.goto('/namespaces/prod');
  await expectPageRendered(page, 'Production (prod)');
  await page.getByRole('button', { name: 'Delete namespace' }).click();
  await expect(page.getByTestId('mutation-dialog')).toBeVisible();
}

/** Tick every consequence. Preview stays disabled until all of them are named. */
async function acknowledgeEverything(page) {
  const panel = page.getByTestId('namespace-delete-consequences');
  await expect(panel).toBeVisible();
  const boxes = panel.getByRole('checkbox');
  const count = await boxes.count();
  for (let i = 0; i < count; i += 1) {
    await boxes.nth(i).check();
  }
  await expect(page.getByTestId('mutation-preview')).toBeEnabled();
}

test.describe('the blast radius', () => {
  test('a kind whose listing was refused is an em dash, never a zero', async ({ page }) => {
    await openDialog(page);

    const secrets = page.getByTestId('namespace-delete-count-secrets');
    await expect(secrets).toBeVisible();
    await expect(secrets.getByTestId('nullable-cell')).toBeVisible();
    await expect(secrets).not.toContainText('0');

    // A kind that really is empty is not shown at all, so there is no row that
    // could be mistaken for the refused one.
    await expect(page.getByTestId('namespace-delete-count-configmaps')).toHaveCount(0);
    await expect(page.getByTestId('namespace-delete-count-pods')).toContainText('14');
  });

  test('an incomplete inventory says so above the tables, not only in a checkbox', async ({
    page,
  }) => {
    await openDialog(page);

    const banner = page.getByTestId('namespace-delete-partial');
    await expect(banner).toContainText('not complete');
    await expect(banner).toContainText('floor');
  });

  test('a Delete-policy volume is drawn as destroyed and a Retain one is not', async ({
    page,
  }) => {
    await openDialog(page, {
      namespaceDeleteOverrides: {
        volumes: [
          {
            claim: 'postgres-data', volume: 'pv-9c2f', phase: 'Bound', capacity: '200Gi',
            storage_class: 'gp3', reclaim_policy: 'Delete', reason: null,
          },
          {
            claim: 'archive', volume: 'pv-11bb', phase: 'Bound', capacity: '4Ti',
            storage_class: 'sc1', reclaim_policy: 'Retain', reason: null,
          },
        ],
      },
    });

    await expect(page.getByTestId('namespace-delete-fate-postgres-data')).toContainText(
      'destroyed',
    );
    // Opposite outcome, opposite word. "released" for both would be the one
    // rendering that reads as safe for the one that is not.
    await expect(page.getByTestId('namespace-delete-fate-archive')).toContainText('kept');
    await expect(page.getByTestId('namespace-delete-fate-archive')).not.toContainText(
      'destroyed',
    );
  });

  test('a volume nobody could read is unknown, and not reported as safe', async ({ page }) => {
    await openDialog(page);

    const fate = page.getByTestId('namespace-delete-fate-analytics-scratch');
    await expect(fate.getByTestId('nullable-cell')).toBeVisible();
    await expect(fate).not.toContainText('kept');
    await expect(fate).not.toContainText('destroyed');

    // And it is its own consequence, distinct from the destroyed one.
    await expect(page.getByTestId('namespace-delete-ack-namespace_volume_fate_unknown')).toBeVisible();
    await expect(page.getByTestId('namespace-delete-consequences')).toContainText(
      'not reporting that they are safe',
    );
  });

  test('unreadable webhook configurations are a warning, not an empty table', async ({
    page,
  }) => {
    await openDialog(page, {
      namespaceDeleteOverrides: {
        webhooks: null,
        consequences: namespaceDeletePlanFor('prod').consequences.map((entry) =>
          entry.code === 'namespace_breaks_admission_webhook'
            ? {
                ...entry,
                label: 'Whether an admission webhook is served from here is unknown',
                consequence:
                  'The webhook configurations could not be read. This console cannot tell you whether one does.',
              }
            : entry,
        ),
      },
    });

    const banner = page.getByTestId('namespace-delete-webhooks-unknown');
    await expect(banner).toContainText('unknown');
    await expect(banner).toContainText('failurePolicy: Fail');
    await expect(page.getByTestId('namespace-delete-webhooks-none')).toHaveCount(0);
  });

  test('a failing-closed webhook says the whole cluster stops accepting writes', async ({
    page,
  }) => {
    await openDialog(page);

    await expect(page.getByTestId('namespace-delete-consequences')).toContainText(
      'across the whole cluster',
    );
  });

  test('the finalizers that leave a namespace Terminating are named', async ({ page }) => {
    await openDialog(page);

    const table = page.getByRole('grid', { name: 'Objects holding a finalizer' });
    await expect(table).toContainText('postgres-data');
    await expect(table).toContainText('kubernetes.io/pvc-protection');
  });
});

test.describe('the handshake', () => {
  test('preview is blocked until every consequence is named', async ({ page }) => {
    await openDialog(page);

    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'Acknowledge what this deletion means',
    );

    await acknowledgeEverything(page);
  });

  test('the first call is a projection carrying every acknowledged code', async ({ page }) => {
    const deletes = [];
    await openDialog(page, { namespaceDeletes: deletes });
    await acknowledgeEverything(page);

    await page.getByTestId('mutation-preview').click();
    await expect(page.getByTestId('mutation-confirm')).toBeVisible();

    expect(deletes).toHaveLength(1);
    // §0.3: nothing has been written yet.
    expect(deletes[0].body.dryRun).toBe(true);
    expect([...deletes[0].body.acknowledgeConsequences].sort()).toEqual([...ALL_CODES].sort());
  });

  test('confirm needs the namespace typed, because this one is not reversible', async ({
    page,
  }) => {
    await openDialog(page);
    await acknowledgeEverything(page);
    await page.getByTestId('mutation-preview').click();

    const confirm = page.getByTestId('mutation-confirm');
    await expect(page.getByTestId('mutation-typed')).toBeVisible();
    await expect(confirm).toBeDisabled();

    await page.getByTestId('mutation-typed').fill('production');
    await expect(confirm).toBeDisabled();

    await page.getByTestId('mutation-typed').fill('prod');
    await expect(confirm).toBeEnabled();
  });

  test('the diff shows the namespace disappearing before anything is written', async ({
    page,
  }) => {
    await openDialog(page);
    await acknowledgeEverything(page);
    await page.getByTestId('mutation-preview').click();

    await expect(page.getByTestId('mutation-dialog')).toContainText('-kind: Namespace');
    // §1.5: a dry run is never a success.
    await expect(page.getByTestId('mutation-summary')).toHaveCount(0);
  });
});

test.describe('the write', () => {
  test('confirming says a deletion started, not that the namespace is gone', async ({ page }) => {
    const deletes = [];
    await openDialog(page, { namespaceDeletes: deletes });
    await acknowledgeEverything(page);
    await page.getByTestId('mutation-preview').click();
    await page.getByTestId('mutation-typed').fill('prod');
    await page.getByTestId('mutation-confirm').click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText('is being deleted');
    await expect(summary).toContainText('deletionTimestamp, not a finished deletion');
    await expect(summary).toContainText('Terminating');

    expect(deletes).toHaveLength(2);
    expect(deletes[1].body.dryRun).toBe(false);
  });
});

test.describe('the refusals', () => {
  test('a namespace already Terminating is not offered a second delete', async ({ page }) => {
    await openDialog(page, {
      namespaceDeleteOverrides: {
        phase: 'Terminating',
        deletionTimestamp: '2026-09-06T09:00:00Z',
        consequences: [],
        blocked: {
          message: 'prod is already being deleted.',
          hint: 'It has a deletionTimestamp and the namespace controller has started. If it has not finished, the finalizers below are why — re-deleting it does nothing.',
          context: { phase: 'Terminating' },
        },
      },
    });

    await expect(page.getByTestId('namespace-delete-blocked')).toContainText(
      're-deleting it does nothing',
    );
    await expect(page.getByTestId('mutation-preview')).toBeDisabled();
    // The plan is still the diagnosis: the finalizer holding it is on screen.
    await expect(page.getByRole('grid', { name: 'Objects holding a finalizer' })).toContainText(
      'kubernetes.io/pvc-protection',
    );
  });

  test('a read-only console disables the delete with its reason, not a 403', async ({ page }) => {
    await openDialog(page, {
      namespaceDeleteOverrides: {
        gate: {
          enabled: false,
          detail:
            'This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it writes nothing to a cluster. The plan is still available.',
        },
      },
    });

    await expect(page.getByTestId('namespace-delete-disabled')).toContainText(
      'ADMIN_ALLOW_MUTATIONS',
    );
    // The plan is a read and is still on screen: an operator deciding whether
    // to enable writes has to be able to see what enabling them would destroy.
    await expect(page.getByTestId('namespace-delete-count-pods')).toContainText('14');
  });

  test('an account that may not delete sees the button and the reason', async ({ page }) => {
    await mockApi(page, { preflight: DENY_DELETE });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');

    const button = page.getByRole('button', { name: 'Delete namespace' });
    await expect(button).toHaveAttribute('data-allowed', 'false');
    await button.hover();
    await expect(page.getByText('Grant `delete` on `core/namespaces`')).toBeVisible();
  });
});
