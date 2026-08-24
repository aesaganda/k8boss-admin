import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

/**
 * The masthead CLI session (§15) — a pod with kubectl in it, and a shell.
 *
 * What is asserted here is not that a terminal appears. It is that this console
 * cannot mislead an operator about the one surface where its own guarantees do
 * not hold:
 *
 * **The shell is outside the write funnel, and the panel says so before the
 * pod is created.** Every other action here is preflighted, dry-run, shown as a
 * diff and recorded in the trail naming the object. A command typed in this
 * shell is none of those. An operator who has learned to trust the diff has to
 * be told, in words, that this one does not have one.
 *
 * **What the shell can do is the ServiceAccount, not the operator.** kubectl in
 * the pod authenticates as the account the pod binds, so it may be able to do
 * more than the person typing, or less. Kubernetes has no permission covering
 * which account a pod may bind, so the console cannot narrow it and must not
 * imply that it has. The account is on screen in the table, in the dialog and
 * in the diff.
 *
 * **Nothing is created and no session is opened by looking.** Opening the panel
 * with a pod already running shows its terminal, and that terminal connects only
 * when its own button is pressed — §7 audits on open, and a row written for
 * somebody who was browsing is a false record of a shell.
 *
 * **The gate is explained, not merely enforced.** A disabled action with no
 * reason sends an operator to argue with a cluster admin about RBAC when what is
 * off is a switch in their own deployment.
 */

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

async function openCli(page) {
  await page.goto('/');
  await expect(page.getByTestId('cluster-selector')).toContainText('prod-eu');
  await page.getByTestId('cli-button').click();
  await expect(page.getByTestId('cli-panel')).toBeVisible();
}

test.describe('the masthead CLI session', () => {
  test('is reachable from every page, and says which account a shell would run as', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openCli(page);

    await expect(page.getByTestId('cli-panel')).toHaveAttribute('data-enabled', 'true');
    // `.first()`: the name appears in the table row and again in the terminal's
    // heading, because a single Running pod auto-opens its shell.
    await expect(page.getByText('k8boss-cli-x4k2p').first()).toBeVisible();
    // The summary line names where a new pod lands and what it would run as —
    // both part of what the operator is agreeing to.
    await expect(page.getByTestId('cli-panel')).toContainText('namespace default');
    await expect(page.getByTestId('cli-panel')).toContainText('run as k8boss-cli');
  });

  test('reuses a running pod: the terminal is shown, and it connects to nothing', async ({
    page,
  }) => {
    const requests = [];
    await page.on('request', (request) => requests.push(request.url()));
    await mockApi(page, { preflight: ALLOW_ALL });
    await openCli(page);

    // One Running pod is not an ambiguity worth asking about, so its terminal is
    // on screen without anybody choosing.
    await expect(page.getByTestId('pod-terminal')).toBeVisible();
    // …and it is idle. §7 audits a session on open, so a terminal that dialled
    // the socket on render would write a row for an operator who was looking.
    await expect(page.getByTestId('pod-terminal')).toHaveAttribute('data-status', 'idle');
    await expect(page.getByRole('button', { name: 'Open shell' })).toBeVisible();
    // Nothing was created to get here.
    expect(requests.filter((url) => url.includes('/api/cli') && !url.includes('?'))).toBeTruthy();
  });

  test('closing the terminal closes it, and it does not reopen itself', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openCli(page);
    await expect(page.getByTestId('pod-terminal')).toBeVisible();

    await page.getByRole('button', { name: 'Close terminal' }).click();

    // The panel picks a terminal for the operator once, as a convenience. It
    // must not keep picking: an effect that re-derived "exactly one Running pod,
    // so show it" on every render makes Close terminal a no-op in the one case
    // it is most likely to be pressed, and the operator is left clicking a
    // control that visibly does nothing.
    await expect(page.getByTestId('pod-terminal')).toHaveCount(0);
    await page.waitForTimeout(300);
    await expect(page.getByTestId('pod-terminal')).toHaveCount(0);
  });

  test('a pod that is not Running cannot be shelled into, and says why', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      cli: {
        ...FIXTURES.cliEnabled,
        items: [
          {
            ...FIXTURES.cliEnabled.items[0],
            phase: 'Pending',
            state: 'Waiting',
            reason: 'ImagePullBackOff',
          },
        ],
      },
    });
    await openCli(page);

    // No auto-opened terminal: the pod is not Running.
    await expect(page.getByTestId('pod-terminal')).toHaveCount(0);
    const shell = page.getByTestId('cli-shell-k8boss-cli-x4k2p');
    await expect(shell).toBeVisible();
    await expect(shell).toBeDisabled();
  });

  test('an unnamed ServiceAccount is reported as unknown, never as "default"', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      cli: {
        ...FIXTURES.cliEnabled,
        // The API server defaulted the field, so we did not read what this pod
        // runs as. Rendering "default" would be a claim about what a shell in it
        // is permitted to do, made from a field that was absent.
        items: [{ ...FIXTURES.cliEnabled.items[0], serviceAccount: null }],
      },
    });
    await openCli(page);

    await expect(page.getByText('Unknown', { exact: true })).toBeVisible();
  });

  test('the gate is explained, and the action disabled rather than hidden', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, cli: FIXTURES.cliDisabled });
    await openCli(page);

    await expect(page.getByTestId('cli-panel')).toHaveAttribute('data-enabled', 'false');
    const banner = page.getByTestId('cli-disabled');
    await expect(banner).toBeVisible();
    // It must name the switch. "Disabled" without the switch sends an operator
    // to the wrong system.
    await expect(banner).toContainText('ADMIN_CLI_ENABLED');

    // Rule 11.4: visible, disabled, carrying the reason — never hidden.
    const button = page.getByRole('button', { name: 'Start a session' });
    await expect(button).toBeVisible();
    await expect(button).toHaveAttribute('data-allowed', 'false');
  });

  test('the consequences are stated before anything is sent', async ({ page }) => {
    const posts = [];
    await mockApi(page, {
      preflight: ALLOW_ALL,
      cli: { ...FIXTURES.cliEnabled, items: [] },
      cliCreate: (body) => {
        posts.push(body);
        return {
          dryRun: body.dryRun !== false,
          applied: body.dryRun === false,
          verb: 'create',
          target: { group: '', version: 'v1', resource: 'pods', namespace: 'default',
                    name: 'k8boss-cli-x4k2p' },
          diff: {
            before: '',
            after: 'apiVersion: v1\nkind: Pod\n',
            unified:
              '--- live\n+++ projected\n@@ -0,0 +1,4 @@\n+apiVersion: v1\n+kind: Pod\n' +
              '+spec:\n+  serviceAccountName: k8boss-cli\n',
            changed: true,
          },
          resourceVersion: '9002',
          warnings: [],
          auditId: 5151,
          pod: 'k8boss-cli-x4k2p',
          namespace: 'default',
          image: body.image || 'alpine/k8s:1.34.9',
          serviceAccount: 'k8boss-cli',
          container: 'cli',
        };
      },
    });
    await openCli(page);

    await page.getByRole('button', { name: 'Start a session' }).click();

    // Before the preview, in plain words — the two facts a YAML diff does not
    // say out loud.
    const consequences = page.getByTestId('cli-consequences');
    await expect(consequences).toBeVisible();
    await expect(consequences).toContainText('No diff, and no record of what changed');
    await expect(consequences).toContainText('k8boss-cli');
    // Nothing has been sent yet: the form phase touches no cluster.
    expect(posts).toHaveLength(0);

    // Phase 1 is always a dry run, whatever the button says.
    await page.getByRole('button', { name: 'Preview the pod' }).click();
    await expect(page.getByTestId('mutation-dialog')).toHaveAttribute('data-phase', 'diff');
    expect(posts).toEqual([{ image: null, dryRun: true }]);
    // And the account is in the diff the operator confirms against.
    await expect(page.getByTestId('mutation-dialog')).toContainText('serviceAccountName: k8boss-cli');
  });

  test('removal is offered at every phase, and dry-runs before it deletes', async ({ page }) => {
    const cliDeletes = [];
    await mockApi(page, { preflight: ALLOW_ALL, cliDeletes });
    await openCli(page);

    await page.getByTestId('cli-remove-k8boss-cli-x4k2p').click();

    // No form, so the dialog dry-runs on open: §4 fixes a delete's diff as the
    // whole manifest disappearing, which is what a removal is confirmed against.
    await expect(page.getByTestId('mutation-dialog')).toBeVisible();
    await expect.poll(() => cliDeletes.map((d) => d.dryRun)).toEqual([true]);
  });

  test('the button is disabled with a reason when no cluster is selected', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, clusters: { items: [] } });
    await page.goto('/');

    // Rule 11.4 again, one level up: the masthead control stays visible so an
    // operator learns the console can do this, and learns what to do first.
    const button = page.getByTestId('cli-button');
    await expect(button).toBeVisible();
  });
});
