import { expect, test } from '@playwright/test';

import {
  FIXTURES,
  PSA_ADMISSION_WARNINGS,
  expectPageRendered,
  mockApi,
  podSecuritySetFor,
} from './fixtures.js';

/**
 * Pod Security set-with-preview (§18).
 *
 * The write is six labels on one namespace, and §4's editor could already
 * write them. What is asserted here is the three things this dialog adds,
 * because each of them is a sentence that stops an operator being wrong about
 * their own cluster:
 *
 * **The API server's admission warnings are on screen before Confirm, and
 * verbatim.** They name the pods already running that do not meet the level.
 * The console neither computes nor parses that list, so the test asserts the
 * exact strings survive the round trip.
 *
 * **Confirm is blocked until they are read.** A Confirm enabled beside a list
 * of workloads that stop being deployable makes reading it optional.
 *
 * **"Nothing is evicted" is a checkbox, not a footnote.** An operator who
 * reads `enforce: restricted` as "the workloads in front of me are now
 * restricted" is wrong, and this is where they are told.
 */

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

const DENY_PATCH = (checks) =>
  checks.map((check) => ({
    verb: check.verb,
    group: check.group,
    resource: check.resource,
    namespace: check.namespace ?? null,
    subresource: check.subresource ?? null,
    allowed: check.verb !== 'patch',
    reason: check.verb === 'patch' ? 'no RBAC policy matched' : '',
    evaluationError: null,
    hint:
      check.verb === 'patch'
        ? "Grant `patch` on `core/namespaces` to the console's ServiceAccount."
        : null,
  }));

async function openDialog(page) {
  await mockApi(page, { preflight: ALLOW_ALL });
  await page.goto('/namespaces/prod');
  await expectPageRendered(page, 'Production (prod)');
  await page.getByRole('button', { name: 'Set level…' }).click();
  await expect(page.getByTestId('psa-form')).toBeVisible();
}

/**
 * Tick every consequence the plan produced. Preview stays disabled until they
 * are all named — which is the §11.3 handshake and is asserted on its own
 * below; here it is a precondition for reaching the preview at all.
 */
async function acknowledgeEverything(page) {
  // Wait for the panel first. The plan refetches whenever a mode changes, and
  // an acknowledgement ticked while the previous plan's list was on screen is
  // cleared when the new one lands — which is the dialog behaving correctly
  // and would look like a flaky test if this raced it.
  const panel = page.getByTestId('psa-consequences');
  await expect(panel).toBeVisible();
  const boxes = panel.getByRole('checkbox');
  const count = await boxes.count();
  for (let i = 0; i < count; i += 1) {
    await boxes.nth(i).check();
  }
  await expect(page.getByTestId('mutation-preview')).toBeEnabled();
}

test.describe('setting a namespace’s Pod Security level', () => {
  test('the preview shows the API server’s own violating pods, verbatim', async ({ page }) => {
    await openDialog(page);

    // The namespace enforces `restricted` already; move audit to match, which
    // is a real change with nothing to acknowledge about eviction.
    await page.getByTestId('psa-audit').selectOption('restricted');
    await acknowledgeEverything(page);
    await page.getByTestId('mutation-preview').click();

    const violations = page.getByTestId('psa-violations');
    await expect(violations).toBeVisible();
    await expect(violations).toHaveAttribute('data-count', String(PSA_ADMISSION_WARNINGS.length));
    for (const warning of PSA_ADMISSION_WARNINGS) {
      await expect(page.getByTestId('psa-violation').filter({ hasText: warning })).toHaveCount(1);
    }

    // The sentence that stops the misreading: admission runs on create.
    await expect(page.getByTestId('psa-admission')).toContainText('FailedCreate');
    await expect(page.getByTestId('psa-admission')).toContainText('verbatim');

    // §1.5: nothing on the preview is a success.
    await expect(page.getByTestId('mutation-summary')).toHaveCount(0);
  });

  test('Confirm is blocked until the admission warnings are read', async ({ page }) => {
    await openDialog(page);
    await page.getByTestId('psa-audit').selectOption('restricted');
    await acknowledgeEverything(page);
    await page.getByTestId('mutation-preview').click();

    const confirm = page.getByTestId('mutation-confirm');
    await expect(confirm).toBeDisabled();
    await expect(page.getByTestId('mutation-blocked')).toContainText('3 warnings');

    await page.getByTestId('psa-read-warnings').check();
    await expect(confirm).toBeEnabled();

    await confirm.click();
    await expect(page.getByTestId('mutation-summary')).toBeVisible();
    // The warnings stay on screen after the write: those pods are now running
    // under a level their spec does not meet.
    await expect(page.getByTestId('psa-violations')).toBeVisible();
    await expect(page.getByTestId('psa-admission')).toContainText('Nothing here was evicted');
  });

  test('a preview with no warnings says admission found nothing, not that we checked', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      podSecuritySet: (body) => podSecuritySetFor(body, { warnings: [] }),
    });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');
    await page.getByRole('button', { name: 'Set level…' }).click();
    await page.getByTestId('psa-audit').selectOption('restricted');
    await acknowledgeEverything(page);
    await page.getByTestId('mutation-preview').click();

    await expect(page.getByTestId('psa-admission-quiet')).toContainText('not that this console checked');
    await expect(page.getByTestId('psa-violations')).toHaveCount(0);
    // Nothing to read, so nothing blocks.
    await expect(page.getByTestId('mutation-confirm')).toBeEnabled();
  });

  test('raising the level must be acknowledged as not evicting anything', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      project: {
        ...FIXTURES.project,
        podSecurity: { ...FIXTURES.project.podSecurity, enforce: 'baseline', warn: 'baseline' },
      },
    });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');
    await page.getByRole('button', { name: 'Set level…' }).click();
    await page.getByTestId('psa-enforce').selectOption('restricted');

    const consequences = page.getByTestId('psa-consequences');
    await expect(consequences).toBeVisible();
    await expect(consequences).toContainText('Pods already running are not affected');

    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    await expect(preview).toHaveAttribute('title', /Acknowledge what this change means/);

    await page.getByTestId('psa-ack-psa_does_not_evict').check();
    await page.getByTestId('psa-ack-psa_no_warn_label').check();
    await expect(preview).toBeEnabled();
  });

  test('removing the enforce label says it is not the same as privileged', async ({ page }) => {
    await openDialog(page);
    await page.getByTestId('psa-enforce').selectOption('');

    const consequences = page.getByTestId('psa-consequences');
    await expect(consequences).toContainText('not set to privileged');
    await expect(consequences).toContainText('cannot tell you what it is');
    await expect(page.getByTestId('psa-ack-psa_enforcement_removed')).toBeVisible();
  });

  test('lowering the level says what becomes deployable', async ({ page }) => {
    await openDialog(page);
    await page.getByTestId('psa-enforce').selectOption('baseline');

    await expect(page.getByTestId('psa-consequences')).toContainText('refuses today will be admitted');
    await expect(page.getByTestId('psa-ack-psa_lowered')).toBeVisible();
  });

  test('a request that changes nothing cannot be previewed, and says why', async ({ page }) => {
    await openDialog(page);

    await expect(page.getByTestId('psa-unchanged')).toBeVisible();
    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    await expect(preview).toHaveAttribute('title', /already declares/);
  });

  test('the button states the missing grant rather than disappearing', async ({ page }) => {
    await mockApi(page, { preflight: DENY_PATCH });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');

    const button = page.getByTestId('action-button').filter({ hasText: 'Set level…' });
    await expect(button).toHaveAttribute('data-allowed', 'false');
    await button.hover();
    await expect(page.getByText('Grant `patch` on `core/namespaces`')).toBeVisible();
  });
});
