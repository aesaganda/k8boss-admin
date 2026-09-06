/**
 * Acting as the operator instead of as the console (ADR-0007).
 *
 * The registration form is where this feature is turned on, and it is the only
 * screen where the two things that make it work — a `resourceNames`-shaped
 * grant and a shared issuer — can still be got right before anybody is locked
 * out of a cluster that was working. So the assertions here are about what the
 * form *says* as much as what it sends:
 *
 *   sent vs assumed        The flag rides on the create and the update body.
 *                          A form that showed the checkbox and dropped the
 *                          field would leave a cluster acting as the console
 *                          while its own registration says otherwise.
 *
 *   prefilled vs blank     Unlike the token and the CA, this is not a
 *                          credential, so it comes back in ClusterPublic and
 *                          must prefill. A box that reset to unchecked on every
 *                          edit turns "rename this cluster" into "stop acting
 *                          as the operator".
 *
 *   info vs warning        This is the *safer* setting. Drawing it in the same
 *                          colour as "skip TLS verification" would teach
 *                          operators that both are risks to avoid.
 *
 *   refusal vs denial      `impersonation_unavailable` is not `rbac_denied`.
 *                          The operator's cluster permissions are not the
 *                          problem, and framing it as a denial sends them to
 *                          widen a ClusterRole that was already correct.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

async function openRegisterForm(page, writes) {
  await mockApi(page, { clusterWrites: writes });
  await page.goto('/clusters');
  await page.getByTestId('cluster-add').click();
  await expect(page.getByTestId('cluster-impersonation')).toBeVisible();
}

test.describe('turning impersonation on', () => {
  test('the checkbox is off until asked for, and rides on the create body', async ({
    page,
  }) => {
    const writes = [];
    await openRegisterForm(page, writes);

    const box = page.getByTestId('cluster-impersonation');
    await expect(box).not.toBeChecked();

    await page.getByTestId('cluster-name').fill('staging');
    await page.getByTestId('cluster-api-server').fill('https://api.staging:6443');
    await page.getByTestId('cluster-token').fill('t');
    await box.check();
    await page.getByRole('button', { name: 'Register' }).click();

    await expect.poll(() => writes.length).toBeGreaterThan(0);
    expect(writes[0].impersonation_enabled).toBe(true);
  });

  test('it names the grant shape and the shared issuer before anything is saved', async ({
    page,
  }) => {
    await openRegisterForm(page, []);
    await page.getByTestId('cluster-impersonation').check();

    const note = page.getByTestId('cluster-impersonation-note');
    // The grant. Unrestricted `impersonate users` is cluster-admin by proxy,
    // and this is the last screen before somebody writes it into a ClusterRole.
    await expect(note).toContainText('resourceNames');
    await expect(note).toContainText('cluster-admin by proxy');
    // The half that is not RBAC, and the one that locks people out silently.
    await expect(note).toContainText('identity provider');
    await expect(note).toContainText('different issuer');
  });

  test('it is drawn as information, not as a hazard', async ({ page }) => {
    await openRegisterForm(page, []);
    await page.getByTestId('cluster-impersonation').check();

    // `skip TLS verification` is the warning on this form. Sharing its colour
    // would say these two settings are the same kind of decision.
    const note = page.getByTestId('cluster-impersonation-note');
    await expect(note).toHaveClass(/pf-m-info/);
  });

  test('an existing setting prefills rather than resetting on edit', async ({ page }) => {
    const writes = [];
    await mockApi(page, {
      clusterWrites: writes,
      clusters: {
        ...FIXTURES.clusters,
        items: [{ ...FIXTURES.clusters.items[0], impersonation_enabled: true }],
      },
    });
    await page.goto('/clusters');
    await page.getByRole('row', { name: /prod-eu/ }).getByRole('button').click();
    await page.getByRole('menuitem', { name: 'Edit' }).click();
    await expect(page.getByTestId('cluster-impersonation')).toBeVisible();

    // The trap: a box that reset to unchecked would turn renaming a cluster
    // into silently taking it back to acting as the console.
    await expect(page.getByTestId('cluster-impersonation')).toBeChecked();

    await page.getByRole('button', { name: 'Save' }).click();
    await expect.poll(() => writes.length).toBeGreaterThan(0);
    expect(writes[0].impersonation_enabled).toBe(true);
  });
});
