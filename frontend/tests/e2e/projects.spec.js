import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi, projectCreateFor } from './fixtures.js';

/**
 * Projects (§17): a namespace read with what governs it, and created with it.
 *
 * The read half is five listings on one page, so what is asserted is what the
 * page says when one of them did not answer or when a number is unknown:
 *
 * **A refused listing is a failure panel, not an empty section.** "Nobody is
 * bound in this namespace" is the sentence that gets a binding added on top of
 * the one nobody could see.
 *
 * **A quota's unknown `used` is an em dash, never a zero.** The controller has
 * not written status; "nothing in use" is what somebody about to scale wants to
 * hear, and this cell must not say it.
 *
 * **No Pod Security label is "nothing declared", never `privileged`.** The
 * cluster default lives in a file no API serves.
 *
 * The write half is the §11.3 handshake over five objects, so what is asserted
 * is what the dialog refuses and what it admits to: consequences acknowledged
 * by name, a dry run that marks the rendered objects as rendered, a preflight
 * denial that blocks Confirm, and a partial create reported as partial.
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

test.describe('the namespace page', () => {
  test('shows what governs the namespace and where it could not look', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');

    // Rule 11.1: the refused listing is named, persistently.
    await expect(page.getByTestId('partial-banner')).toBeVisible();
    await expect(page.getByTestId('partial-banner')).toContainText('rolebindings');

    // The refused section is a failure panel where the table would be — and
    // the empty-state sentence is nowhere on the page.
    await expect(page.getByTestId('project-rolebindings-unavailable')).toBeVisible();
    await expect(page.getByTestId('project-rolebindings-unavailable')).toContainText('not permitted');
    await expect(page.getByText('Nobody is bound in this namespace')).toHaveCount(0);

    // The quota: exhausted is a red badge, an unrecorded `used` is an em dash.
    const quota = page.getByTestId('project-quota');
    await expect(quota).toHaveAttribute('data-reconciled', 'true');
    await expect(quota.getByRole('row', { name: /pods/ })).toContainText('Exhausted');
    const memory = quota.getByRole('row', { name: /requests.memory/ });
    await expect(memory.locator('[data-testid="nullable-cell"][data-nullable="true"]').first()).toHaveText('—');
    await expect(memory).not.toContainText(/\b0\b/);

    // Pod Security: enforce is declared, audit is not — and "not" is a dash
    // with a reason, not "privileged".
    const security = page.getByTestId('project-pod-security');
    await expect(security).toHaveAttribute('data-labelled', 'true');
    await expect(security).toContainText('restricted');
    await expect(security).not.toContainText('privileged');

    // Network policy: declared, with the caveat that says it is only declared.
    const policies = page.getByTestId('project-network-policies');
    await expect(policies).toHaveAttribute('data-ingress', 'true');
    await expect(policies).toContainText('Declared, not enforced');
  });

  test('a namespace declaring no Pod Security level is not rendered as privileged', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      project: {
        ...FIXTURES.project,
        labels: {},
        podSecurity: {
          enforce: null, enforceVersion: null, audit: null, auditVersion: null, warn: null, warnVersion: null, labelled: false,
        },
      },
    });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');

    const security = page.getByTestId('project-pod-security');
    await expect(security).toHaveAttribute('data-labelled', 'false');
    await expect(security).toContainText('declares no Pod Security level');
    await expect(security).toContainText('cannot tell you what it is');
    // The word appears once, inside the sentence saying it is NOT the same as this.
    await expect(security).toContainText('This is not the same as');
  });

  test('no quota at all is the finding, and says so', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, project: { ...FIXTURES.project, quotas: [] } });
    await page.goto('/namespaces/prod');
    await expectPageRendered(page, 'Production (prod)');
    await expect(page.getByText('Nothing bounds this namespace')).toBeVisible();
    await expect(page.getByTestId('project-resourcequotas-unavailable')).toHaveCount(0);
  });

  test('is reached from the namespace name without changing the scope', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/namespaces');
    await expectPageRendered(page, 'Namespaces');
    await page.getByTestId('namespace-link').filter({ hasText: 'prod' }).click();
    await expect(page).toHaveURL(/\/namespaces\/prod$/);
    await expectPageRendered(page, 'Production (prod)');
  });
});

test.describe('the new project handshake', () => {
  async function openDialog(page, name) {
    await page.goto('/namespaces');
    await expectPageRendered(page, 'Namespaces');
    await page.getByRole('button', { name: 'New project…' }).click();
    await expect(page.getByTestId('project-form')).toBeVisible();
    await page.getByTestId('project-name').fill(name);
    // The plan is what fills the dialog in; acting before it lands races it.
    await expect(page.getByTestId('project-target')).toHaveAttribute('data-exists', /true|false/);
  }

  test('Preview stays disabled until every consequence is acknowledged', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDialog(page, 'payments');

    // The defaults enforce `restricted` and bind nobody: two consequences, each
    // stated as what happens to the cluster and what to do instead.
    const consequences = page.getByTestId('project-consequences');
    await expect(consequences).toBeVisible();
    await expect(consequences).toContainText('fails to create a single pod');
    await expect(consequences).toContainText('Nobody is bound to this project');

    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    await expect(preview).toHaveAttribute('title', /Acknowledge what this project means/);

    await page.getByTestId('project-ack-psa_enforced').check();
    await expect(preview).toBeDisabled();
    await page.getByTestId('project-ack-no_admin').check();
    await expect(preview).toBeEnabled();

    // The plan lists exactly what the form asks for: with the defaults, a
    // Namespace, a ResourceQuota and a LimitRange. No RoleBinding — nobody was
    // named — and no NetworkPolicy.
    const planned = page.getByTestId('project-plan-object');
    await expect(planned).toHaveCount(3);
    await expect(planned.nth(0)).toHaveAttribute('data-kind', 'Namespace');
    await expect(planned.nth(1)).toHaveAttribute('data-kind', 'ResourceQuota');
    await expect(planned.nth(2)).toHaveAttribute('data-kind', 'LimitRange');
  });

  test('an existing namespace is refused at the preview, never taken over', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    // `prod` is in the namespaces fixture, so the plan reports it as existing.
    await openDialog(page, 'prod');
    await expect(page.getByTestId('project-target')).toHaveAttribute('data-exists', 'true');
    await expect(page.getByTestId('project-target-exists')).toBeVisible();
    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    await expect(preview).toHaveAttribute('title', /already exists/);
  });

  test('the dry run says whose diff each object carries, and the write reports each object', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDialog(page, 'payments');
    await page.getByTestId('project-ack-psa_enforced').check();
    await page.getByTestId('project-ack-no_admin').check();
    await page.getByTestId('mutation-preview').click();

    const objects = page.getByTestId('project-object');
    await expect(objects).toHaveCount(3);
    // The Namespace is the API server's projection; the rest cannot be until
    // it exists, and are labelled as the console's own rendering.
    await expect(objects.nth(0)).toHaveAttribute('data-projection', 'server');
    await expect(objects.nth(0)).toContainText('projected by the API server');
    await expect(objects.nth(1)).toHaveAttribute('data-projection', 'rendered');
    await expect(objects.nth(1)).toContainText('rendered by the console');
    // §1.5: nothing on the preview is a success.
    await expect(page.getByTestId('mutation-summary')).toHaveCount(0);

    await page.getByTestId('mutation-confirm').click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toBeVisible();
    await expect(summary).toContainText('Project payments created — 3 objects written');
    await expect(summary).not.toContainText('Applied to the cluster');
    // The report stays on screen after the write, per object.
    await expect(objects).toHaveCount(3);
    await expect(objects.nth(1)).toContainText('written');
  });

  test('a preflight denial on a rendered object blocks Confirm with the grant it needs', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      projectCreate: (body) => projectCreateFor(body, { preflightDenied: ['RoleBinding'] }),
    });
    await openDialog(page, 'payments');
    await page.getByTestId('project-admin-name').fill('payments-team');
    await expect(page.getByTestId('project-consequences')).not.toContainText('Nobody is bound');
    await page.getByTestId('project-ack-psa_enforced').check();
    await page.getByTestId('mutation-preview').click();

    await expect(page.getByTestId('project-object-preflight-denied')).toBeVisible();
    await expect(page.getByTestId('project-object-preflight-denied')).toContainText('rolebindings');
    // Rule 11.4: disabled with the reason, and the reason names the object.
    await expect(page.getByTestId('mutation-confirm')).toBeDisabled();
    await expect(page.getByTestId('mutation-blocked')).toContainText('RoleBinding admin');
    await expect(page.getByTestId('mutation-blocked')).toContainText('half-project');
  });

  test('a partial create is reported as partial, with the failed object and its grant', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      projectCreate: (body) => projectCreateFor(body, { failKinds: ['ResourceQuota'] }),
    });
    await openDialog(page, 'payments');
    await page.getByTestId('project-ack-psa_enforced').check();
    await page.getByTestId('project-ack-no_admin').check();
    await page.getByTestId('mutation-preview').click();
    await page.getByTestId('mutation-confirm').click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toBeVisible();
    await expect(summary).toContainText('1 of 3 objects were not created');
    await expect(summary).toContainText('Nothing was rolled back');
    await expect(summary).not.toContainText('created —');

    const quota = page.getByTestId('project-object').filter({ hasText: 'ResourceQuota' });
    await expect(quota).toContainText('rbac_denied');
    await expect(quota).toContainText('Grant `create` on `core/resourcequotas`');
  });
});
