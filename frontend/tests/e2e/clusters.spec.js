/**
 * Who may register a cluster — and the deployment where the answer is "anyone
 * the proxy let in".
 *
 * §3's create, update and delete carry `require_console_admin`, and
 * `Clusters.jsx` mirrors that gate. Two things about the mirror are worth
 * holding down with tests, because both fail silently:
 *
 *   disabled vs hidden     Rule 11.4. A control that vanishes reads as a
 *                          missing feature and becomes a support ticket; a
 *                          disabled one carrying its reason answers the
 *                          question where it was asked. So every case below
 *                          asserts the control is *visible* before it asserts
 *                          anything about its state — a future change to
 *                          hiding fails here rather than in a ticket.
 *
 *   `!authEnabled || admin` vs `admin`
 *                          With application authentication off there is no
 *                          console role at all and the proxy in front owns the
 *                          decision. The obvious simplification — gate on the
 *                          role alone — reads `user?.role` as undefined there
 *                          and disables cluster registration on a supported
 *                          deployment that never had a role to check. That is
 *                          the third test, and it is the one that matters:
 *                          without it the simplification passes the suite.
 *
 * The form's own behaviour lives in `impersonation.spec.js`, which drives it
 * through the same `cluster-add` button.
 */
import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/** A signed-in operator holding the `user` role. §3's three writes are not theirs. */
const NON_ADMIN = {
  authenticated: true,
  user: {
    id: 9,
    username: 'viewer',
    display_name: 'Viewer',
    email: null,
    role: 'user',
    auth_source: 'local',
  },
};

async function openClusters(page, options = {}) {
  await mockApi(page, options);
  await page.goto('/clusters');
  await expectPageRendered(page, 'Clusters');
}

/** The kebab on the one registered cluster — Edit and De-register live there. */
async function openRowMenu(page) {
  await page.getByRole('row', { name: /prod-eu/ }).getByRole('button').click();
}

/** Both write entries in the row kebab, by the labels the page gives them. */
const WRITE_ITEMS = ['Edit', 'De-register'];

test.describe('registration is administrator-only when the console authenticates', () => {
  test('a non-administrator sees the controls disabled with the reason, not gone', async ({
    page,
  }) => {
    await openClusters(page, { auth: NON_ADMIN });

    const register = page.getByTestId('cluster-add');
    // Visible first, and on purpose: this is the assertion that fails if the
    // gate is ever "simplified" into a conditional render.
    await expect(register).toBeVisible();
    await expect(register).toHaveAttribute('aria-disabled', 'true');

    // `isAriaDisabled`, so the button stays focusable and the tooltip stays
    // reachable. A reason nobody can read is not a reason.
    await register.hover();
    const tooltip = page.getByRole('tooltip');
    await expect(tooltip).toContainText('administrator-only');
    await expect(tooltip).toContainText('ask an administrator');

    await openRowMenu(page);
    for (const label of WRITE_ITEMS) {
      const item = page.getByRole('menuitem', { name: label });
      await expect(item).toBeVisible();
      await expect(item).toBeDisabled();
      // `menuAction` puts the reason in a `title` attribute rather than a
      // Tooltip, because PatternFly closes a menu item's tooltip along with
      // the menu. Asserted as the attribute for the same reason.
      await expect(item.locator('[title]')).toHaveAttribute('title', /administrator-only/);
    }

    // The gate covers §3's three writes and stops there. Testing the
    // connection is a read and making a cluster active only changes what this
    // browser is looking at — disabling those would take a non-admin's console
    // away to enforce a rule that does not exist.
    await expect(page.getByRole('menuitem', { name: 'Test connection' })).toBeEnabled();
  });

  test('an administrator gets them enabled, and the form actually opens', async ({ page }) => {
    // The default mock user holds the `admin` role.
    await openClusters(page, { auth: { authenticated: true } });

    const register = page.getByTestId('cluster-add');
    await expect(register).toBeVisible();
    await expect(register).not.toHaveAttribute('aria-disabled', 'true');
    await register.click();
    await expect(page.getByTestId('cluster-name')).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await openRowMenu(page);
    for (const label of WRITE_ITEMS) {
      await expect(page.getByRole('menuitem', { name: label })).toBeEnabled();
    }
  });
});

test.describe('legacy proxy mode has no console role to check', () => {
  test('with application authentication disabled the controls stay enabled', async ({ page }) => {
    // No `auth` option: `/auth/config` answers `enabled: false`, which is the
    // deployment where `require_console_admin` returns None and the proxy in
    // front decides who may write. Reading the role first here would disable
    // registration entirely — a supported deployment made unusable by a check
    // meant to protect a different one.
    await openClusters(page);

    const register = page.getByTestId('cluster-add');
    await expect(register).toBeVisible();
    await expect(register).not.toHaveAttribute('aria-disabled', 'true');
    await register.click();
    await expect(page.getByTestId('cluster-name')).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await openRowMenu(page);
    for (const label of WRITE_ITEMS) {
      await expect(page.getByRole('menuitem', { name: label })).toBeEnabled();
    }
  });
});
