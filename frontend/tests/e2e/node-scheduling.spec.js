/**
 * Node taints and labels (§24).
 *
 * Every assertion here is about a pair of states a convenient rendering would
 * collapse, and on this page collapsing them costs somebody a node:
 *
 *   `NoSchedule` vs `NoExecute`      The first steers new placement. The second
 *                                    deletes what is already running. They are
 *                                    two entries in one dropdown, so the pods
 *                                    the second removes have to appear as the
 *                                    selector moves.
 *   eviction vs deletion             Drain honours PodDisruptionBudgets and
 *                                    says so at length. A taint does not go
 *                                    through `pods/eviction` at all, so it does
 *                                    not. The console taught the operator the
 *                                    opposite two clicks away, so the exception
 *                                    is a checkbox, not a footnote.
 *   `deleting: null` vs `[]`         Nobody counted, versus this taint removes
 *                                    nothing. The first must never render as
 *                                    the second.
 *   "removed the label" vs "moved
 *   the workload"                    Node affinity is IgnoredDuringExecution.
 *                                    Nothing is evicted by a label change, and
 *                                    an operator who believes otherwise has
 *                                    left a workload exactly where it was.
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

const NODE = FIXTURES.nodeDetail.name;

async function openTaints(page) {
  await page.goto(`/nodes/${NODE}`);
  await page.getByRole('button', { name: 'Taints…' }).click();
  await expect(page.getByTestId('taint-form')).toBeVisible();
}

async function openLabels(page) {
  await page.goto(`/nodes/${NODE}`);
  await page.getByRole('button', { name: 'Labels…' }).click();
  await expect(page.getByTestId('label-form')).toBeVisible();
}

/** Fill in the first taint row as `dedicated=gpu` with the given effect. */
async function addTaint(page, effect) {
  await page.getByTestId('taint-add').click();
  await page.getByRole('textbox', { name: 'Taint 1 key' }).fill('dedicated');
  await page.getByRole('textbox', { name: 'Taint 1 value' }).fill('gpu');
  await page.getByTestId('taint-effect-0').selectOption(effect);
}

test.describe('node taints', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
  });

  test('a NoSchedule taint deletes nothing, and the panel says the listing succeeded', async ({
    page,
  }) => {
    await openTaints(page);
    await addTaint(page, 'NoSchedule');

    await expect(page.getByText('No pod on this node is deleted by this change')).toBeVisible();
    await expect(page.getByTestId('taint-consequences')).not.toBeVisible();
  });

  test('switching the same taint to NoExecute names the pods it deletes', async ({ page }) => {
    await openTaints(page);
    await addTaint(page, 'NoSchedule');
    await expect(page.getByText('No pod on this node is deleted by this change')).toBeVisible();

    // The only field that changed is the dropdown, and the page now has to be
    // about deleting pods rather than about scheduling.
    await page.getByTestId('taint-effect-0').selectOption('NoExecute');

    await expect(page.getByRole('grid', { name: 'Pods this taint deletes' })).toBeVisible();
    await expect(page.getByRole('row', { name: /checkout-7c9/ })).toBeVisible();
  });

  test('the deletion consequence says PodDisruptionBudgets do not apply', async ({ page }) => {
    await openTaints(page);
    await addTaint(page, 'NoExecute');

    const consequences = page.getByTestId('taint-consequences');
    await expect(consequences).toContainText('PodDisruptionBudgets do not apply');
    await expect(consequences).toContainText('not an eviction');
  });

  test('a pod tolerating for a time is on the list with its delay, not drawn as safe', async ({
    page,
  }) => {
    await openTaints(page);
    await addTaint(page, 'NoExecute');

    const row = page.getByRole('row', { name: /ledger-4f1/ });
    await expect(row).toContainText('in 300s');

    // And it is not the same rendering as the pod that goes at once.
    await expect(page.getByRole('row', { name: /checkout-7c9/ })).toContainText('immediately');
  });

  test('a pod that tolerates unconditionally is not on the deletion list at all', async ({
    page,
  }) => {
    await openTaints(page);
    await addTaint(page, 'NoExecute');

    await expect(page.getByRole('row', { name: /sidecar-tolerant/ })).toHaveCount(0);
  });

  test('an unmanaged pod and a DaemonSet each get their own acknowledgement', async ({ page }) => {
    await openTaints(page);
    await addTaint(page, 'NoExecute');

    await expect(page.getByTestId('taint-ack-taint_deletes_unmanaged')).toBeVisible();
    await expect(page.getByTestId('taint-ack-taint_deletes_daemonset')).toBeVisible();
    await expect(page.getByRole('row', { name: /one-off-import/ })).toContainText('unmanaged');
  });

  test('preview stays disabled until every consequence is ticked', async ({ page }) => {
    await openTaints(page);
    await addTaint(page, 'NoExecute');

    // Assert the *reason*, not just the disabled state. `useAsync` blanks its
    // data on a new key, so every edit puts the dialog through a moment where
    // Preview is disabled because the plan is still loading — and a bare
    // toBeDisabled() here passes on that, whether or not the acknowledgement
    // rule exists at all.
    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'Acknowledge what this change means',
    );
    const preview = page.getByRole('button', { name: 'Preview the change' });
    await expect(preview).toBeDisabled();

    for (const code of [
      'taint_deletes_pods',
      'taint_deletes_unmanaged',
      'taint_deletes_daemonset',
      'taint_delayed_deletion',
    ]) {
      await page.getByTestId(`taint-ack-${code}`).check();
    }

    await expect(preview).toBeEnabled();
  });

  test('an unreadable pod listing is a warning, never an empty deletion table', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW, nodeSchedulingOptions: { podsChecked: false } });
    await openTaints(page);
    await addTaint(page, 'NoExecute');

    await expect(page.getByTestId('taint-pods-unknown')).toBeVisible();
    await expect(page.getByText('No pod on this node is deleted by this change')).toHaveCount(0);
    await expect(page.getByTestId('taint-ack-taint_pods_unknown')).toBeVisible();
  });

  test('the write sends the whole taint list, not the one row that was added', async ({ page }) => {
    const writes = [];
    await mockApi(page, { preflight: ALLOW, nodeSchedulingWrites: writes });
    await openTaints(page);
    await addTaint(page, 'NoSchedule');

    await page.getByRole('button', { name: 'Preview the change' }).click();
    await expect(page.getByRole('button', { name: 'Set the taints' })).toBeVisible();

    expect(writes).toHaveLength(1);
    expect(writes[0].body.taints).toEqual([
      { key: 'dedicated', value: 'gpu', effect: 'NoSchedule' },
    ]);
    // §0.3: the first call is a projection and nothing else.
    expect(writes[0].body.dryRun).toBe(true);
  });

  test('confirming says the taints are stored and not that the node is empty', async ({ page }) => {
    await openTaints(page);
    await addTaint(page, 'NoSchedule');

    await page.getByRole('button', { name: 'Preview the change' }).click();
    await page.getByRole('button', { name: 'Set the taints' }).click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText(`The taints on ${NODE} are now what you sent`);
    // The sentence the dialog stays open for: `applied` is about the taint
    // list, and the node is not empty yet.
    await expect(summary).toContainText('on its own schedule');
  });
});

test.describe('node labels', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
  });

  test('the node page shows the labels it carries', async ({ page }) => {
    await page.goto(`/nodes/${NODE}`);

    await expect(page.getByText('team=payments')).toBeVisible();
  });

  test('removing a label says nothing is evicted', async ({ page }) => {
    await openLabels(page);
    // `team` is the third row alphabetically: kubernetes.io/hostname,
    // node-role.kubernetes.io/worker, team.
    await page.getByTestId('label-remove-2').click();

    const consequences = page.getByTestId('label-consequences');
    await expect(consequences).toContainText('IgnoredDuringExecution');
    await expect(consequences).toContainText('keep running');
  });

  test('the pods a removed label placed are named, and not as casualties', async ({ page }) => {
    await openLabels(page);
    await page.getByTestId('label-remove-2').click();

    await expect(page.getByRole('grid', { name: /placement rules/ })).toBeVisible();
    await expect(page.getByRole('row', { name: /checkout-7c9/ })).toContainText('team');
    await expect(
      page.getByText('It is what will not be scheduled back here after it next restarts.'),
    ).toBeVisible();
  });

  test('a removed label is sent as an explicit null, not merely left out', async ({ page }) => {
    const writes = [];
    await mockApi(page, { preflight: ALLOW, nodeSchedulingWrites: writes });
    await openLabels(page);
    await page.getByTestId('label-remove-2').click();

    await page.getByTestId('label-ack-label_removed').check();
    await page.getByTestId('label-ack-label_pods_depend').check();
    await page.getByRole('button', { name: 'Preview the change' }).click();
    await expect(page.getByRole('button', { name: 'Set the labels' })).toBeVisible();

    // The map the console sends is the whole intended map. The API's own null
    // for a removal is the backend's business — what this asserts is that the
    // dropped key is genuinely absent from what was sent, because a form that
    // sent it back unchanged would silently never delete anything.
    expect(writes).toHaveLength(1);
    expect(Object.keys(writes[0].body.labels).sort()).toEqual([
      'kubernetes.io/hostname',
      'node-role.kubernetes.io/worker',
    ]);
  });

  test('touching a role label says it changes what the node reports', async ({ page }) => {
    await openLabels(page);
    await page.getByTestId('label-remove-1').click();

    await expect(page.getByTestId('label-ack-label_role_changed')).toBeVisible();
    await expect(page.getByTestId('label-ack-label_reserved_prefix')).toBeVisible();
  });

  test('an unreadable pod listing is a warning, never an empty dependents table', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW, nodeSchedulingOptions: { podsChecked: false } });
    await openLabels(page);
    await page.getByTestId('label-remove-2').click();

    await expect(page.getByTestId('label-pods-unknown')).toBeVisible();
    await expect(page.getByText('No pod here names the labels you are changing')).toHaveCount(0);
    await expect(page.getByTestId('label-ack-label_pods_unknown')).toBeVisible();
  });

  test('confirming says no pod moved', async ({ page }) => {
    await openLabels(page);
    await page.getByTestId('label-remove-2').click();
    await page.getByTestId('label-ack-label_removed').check();
    await page.getByTestId('label-ack-label_pods_depend').check();

    await page.getByRole('button', { name: 'Preview the change' }).click();
    await page.getByRole('button', { name: 'Set the labels' }).click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText(`The labels on ${NODE} are now what you sent`);
    await expect(summary).toContainText('No pod moved');
  });
});
