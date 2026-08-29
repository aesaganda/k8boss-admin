import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * Node debug pods (§5.5) — the most privileged thing this console creates.
 *
 * What is asserted here is not that the feature works. It is that the console
 * cannot mislead an operator about what they are about to do to a machine:
 *
 * **The consequences are on screen before the preview.** A YAML diff containing
 * `hostPath: /` and `hostPID: true` is read correctly by someone who already
 * knows what those mean. "This pod can read every pod's ServiceAccount token on
 * this node" is not a sentence a diff says out loud, and it is the sentence that
 * decides whether the operator should be doing this.
 *
 * **Read-only is the default, and writable is a different act.** A departure
 * from `kubectl debug`, which always mounts the host filesystem writable. If the
 * default ever inverts, or the escalation stops being deliberate, this console
 * starts handing out write access to machines on a checkbox nobody ticked.
 *
 * **The gate is explained, not merely enforced.** A disabled action with no
 * reason sends an operator to argue with a cluster admin about RBAC, when what
 * is off is a switch in their own deployment.
 *
 * **Removal is offered, because nothing else will do it.** The opposite of
 * §7.4's ephemeral container, which cannot be removed at all. Two features that
 * look alike; the difference is the one an operator most needs.
 */

async function openNode(page) {
  await page.goto('/nodes/ip-10-0-1-4');
  await expectPageRendered(page, 'ip-10-0-1-4');
  await expect(page.getByTestId('node-debug-panel')).toBeVisible();
}

/** Answer every §9 check as allowed, so RBAC is not what is under test. */
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

test.describe('node debug pods', () => {
  test('lists the debug pods on this node, and says whether each can write to it', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openNode(page);

    await expect(page.getByTestId('node-debug-panel')).toHaveAttribute('data-enabled', 'true');
    await expect(page.getByText('node-debugger-ip-10-0-1-4-x4k2p')).toBeVisible();
    // The distinction that is invisible from the pod's name or its phase.
    await expect(page.getByText('Read-only', { exact: true })).toBeVisible();
    // And the namespace a new one would land in — part of what is being confirmed.
    await expect(page.getByTestId('node-debug-panel')).toContainText('namespace default');
  });

  test('a writable pod is rendered differently from a read-only one', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      nodeDebug: {
        ...FIXTURES.nodeDebugEnabled,
        items: [{ ...FIXTURES.nodeDebugEnabled.items[0], hostFilesystemReadOnly: false }],
      },
    });
    await openNode(page);

    await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
    await expect(page.getByText('Read-only', { exact: true })).toHaveCount(0);
  });

  test('a pod we do not recognise is not reported as read-only', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      nodeDebug: {
        ...FIXTURES.nodeDebugEnabled,
        // Wears the console's label but has no host mount we know. Saying
        // "read-only" here would be a safety claim about an object we do not
        // understand.
        items: [{ ...FIXTURES.nodeDebugEnabled.items[0], hostFilesystemReadOnly: null }],
      },
    });
    await openNode(page);

    await expect(page.getByText('Unrecognised')).toBeVisible();
    await expect(page.getByText('Read-only', { exact: true })).toHaveCount(0);
  });

  test('the gate is explained, and the action disabled rather than hidden', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, nodeDebug: FIXTURES.nodeDebugDisabled });
    await openNode(page);

    await expect(page.getByTestId('node-debug-panel')).toHaveAttribute('data-enabled', 'false');
    const banner = page.getByTestId('node-debug-disabled');
    await expect(banner).toBeVisible();
    // It must name the switch. "Disabled" without the switch sends an operator
    // to the wrong system.
    await expect(banner).toContainText('ADMIN_NODE_DEBUG_ENABLED');

    // Rule 11.4: visible, disabled, carrying the reason — never hidden.
    const button = page.getByRole('button', { name: 'Create a debug pod' });
    await expect(button).toBeVisible();
    await expect(button).toHaveAttribute('data-allowed', 'false');
  });

  test('the consequences are stated before anything is sent', async ({ page }) => {
    const posts = [];
    await mockApi(page, {
      preflight: ALLOW_ALL,
      nodeDebugCreate: (body) => {
        posts.push(body);
        return {
          dryRun: body.dryRun !== false,
          applied: body.dryRun === false,
          verb: 'create',
          target: { group: '', version: 'v1', resource: 'pods', namespace: 'default' },
          diff: {
            before: '',
            after: 'kind: Pod\n',
            unified: '--- live\n+++ projected\n@@ -0,0 +1,2 @@\n+kind: Pod\n+  hostPID: true\n',
            changed: true,
          },
          resourceVersion: '9002',
          warnings: [],
          auditId: 5150,
          pod: 'node-debugger-ip-10-0-1-4-x4k2p',
          namespace: 'default',
          node: 'ip-10-0-1-4',
          image: body.image || 'busybox:1.36',
          writableHostFilesystem: body.writableHostFilesystem === true,
        };
      },
    });
    await openNode(page);

    await page.getByRole('button', { name: 'Create a debug pod' }).click();

    const consequences = page.getByTestId('node-debug-consequences');
    await expect(consequences).toBeVisible();
    await expect(consequences).toContainText('ServiceAccount token');
    await expect(consequences).toContainText('every pod on this node');
    await expect(consequences).toContainText('NetworkPolicy does not apply');
    expect(posts, 'opening the dialog must not touch the cluster').toHaveLength(0);

    await page.getByRole('button', { name: 'Preview the pod' }).click();
    await expect(page.getByTestId('diff-view')).toBeVisible();
    expect(posts).toHaveLength(1);
    expect(posts[0].dryRun).toBe(true);
    // The default the whole design rests on.
    expect(posts[0].writableHostFilesystem).toBe(false);

    await page.getByRole('button', { name: 'Create debug pod', exact: true }).click();
    expect(posts).toHaveLength(2);
    expect(posts[1].dryRun).toBe(false);
  });

  test('asking for a writable host filesystem is a deliberate, typed act', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openNode(page);
    await page.getByRole('button', { name: 'Create a debug pod' }).click();

    // Nothing scary until it is asked for.
    await expect(page.getByTestId('node-debug-writable-warning')).toHaveCount(0);

    await page.getByTestId('node-debug-writable').check();
    await expect(page.getByTestId('node-debug-writable-warning')).toBeVisible();
    await expect(page.getByTestId('node-debug-writable-warning')).toContainText('static pod manifest');

    await page.getByRole('button', { name: 'Preview the pod' }).click();
    await expect(page.getByTestId('diff-view')).toBeVisible();

    // The confirm is blocked until the node's name is typed — `DrainDialog`'s
    // control, for the same reason: the blast radius is a whole machine.
    const confirm = page.getByRole('button', { name: 'Create debug pod', exact: true });
    await expect(confirm).toBeDisabled();
    await page.getByRole('textbox', { name: /type/i }).fill('ip-10-0-1-4');
    await expect(confirm).toBeEnabled();
  });

  test('removal is offered on every pod, and previews the deletion first', async ({ page }) => {
    const deletes = [];
    await mockApi(page, { preflight: ALLOW_ALL, nodeDebugDeletes: deletes });
    await openNode(page);

    await page.getByTestId('node-debug-remove-node-debugger-ip-10-0-1-4-x4k2p').click();

    // No form, so the dialog dry-runs on open — §4's delete diff is the whole
    // manifest disappearing, which is the right thing to confirm against.
    await expect(page.getByTestId('diff-view')).toBeVisible();
    expect(deletes).toHaveLength(1);
    expect(deletes[0].dryRun).toBe(true);

    await page.getByRole('button', { name: 'Remove', exact: true }).click();
    expect(deletes).toHaveLength(2);
    expect(deletes[1].dryRun).toBe(false);
  });

  test('a node with no debug pod says so, and says nothing will clean one up', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      nodeDebug: { ...FIXTURES.nodeDebugEnabled, items: [] },
    });
    await openNode(page);

    const empty = page.getByTestId('node-debug-empty');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('nothing removes it afterwards except you');
    await expect(page.getByTestId('partial-banner')).toHaveCount(0);
  });
});
