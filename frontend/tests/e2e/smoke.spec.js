import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

test.describe('shell', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('renders the masthead, the cluster selector and the navigation', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByText('k8boss-admin').first()).toBeVisible();
    await expect(page.getByTestId('cluster-selector')).toContainText('prod-eu');
    await expect(page.getByTestId('namespace-selector')).toBeVisible();

    const nav = page.getByRole('navigation', { name: 'Console navigation' });
    await expect(nav.getByRole('link', { name: 'Overview' })).toBeVisible();
    await expect(nav.getByRole('button', { name: 'Cluster' })).toHaveAttribute('aria-expanded', 'false');
    await expect(nav.getByRole('button', { name: 'Workloads' })).toHaveAttribute('aria-expanded', 'false');
    await expect(nav.getByRole('button', { name: 'Custom Resources' })).toHaveAttribute('aria-expanded', 'false');
    await expect(nav.getByRole('button', { name: 'Administration' })).toHaveAttribute('aria-expanded', 'false');

    const network = nav.getByRole('button', { name: 'Network' });
    await expect(network).toHaveAttribute('aria-expanded', 'false');
    await network.click();
    await expect(network).toHaveAttribute('aria-expanded', 'true');
    await expect(nav.getByRole('link', { name: 'Services', exact: true })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Network Policies' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Routes', exact: true })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Gateway (beta)' })).toBeVisible();
    // The header opens and closes the section and does nothing else: a click
    // that also navigated would take the operator off the page they were
    // reading every time they folded a section away.
    await expect(page).toHaveURL(/\/$/);

    await network.click();
    await expect(network).toHaveAttribute('aria-expanded', 'false');
    await expect(nav.getByRole('link', { name: 'Services', exact: true })).toBeHidden();
  });

  test('keeps the active network page visible in its navigation group', async ({ page }) => {
    await page.goto('/routes');

    const nav = page.getByRole('navigation', { name: 'Console navigation' });
    const network = nav.getByRole('button', { name: 'Network' });
    await expect(network).toHaveAttribute('aria-expanded', 'true');
    await expect(nav.getByRole('link', { name: 'Routes', exact: true })).toHaveAttribute('aria-current', 'page');

    // And the section you are standing in can still be folded away: the open
    // state follows arriving in the section, not being in it, so the header
    // is not fighting the router for the one section most likely to be in the
    // way. The page underneath is untouched.
    await network.click();
    await expect(network).toHaveAttribute('aria-expanded', 'false');
    await expect(nav.getByRole('link', { name: 'Routes', exact: true })).toBeHidden();
    await expect(page).toHaveURL(/\/routes$/);
  });

  test('keeps the sidebar mounted while a page chunk loads', async ({ page }) => {
    await page.goto('/');
    const nav = page.getByRole('navigation', { name: 'Console navigation' });
    await expect(nav).toBeVisible();

    // Navigating to a route whose chunk has not been fetched must not blank the
    // shell: the Suspense boundary lives around <Outlet/> only.
    await nav.getByRole('button', { name: 'Cluster' }).click();
    await nav.getByRole('link', { name: 'Nodes' }).click();
    await expect(nav).toBeVisible();
    await expect(page).toHaveURL(/\/nodes$/);
  });

  test('theme toggle switches PatternFly into dark mode', async ({ page }) => {
    await page.goto('/');
    const html = page.locator('html');
    await expect(html).not.toHaveClass(/pf-v6-theme-dark/);

    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveClass(/pf-v6-theme-dark/);
  });

  test('read-only deployments say so and are not silently write-capable', async ({ page }) => {
    await mockApi(page, { health: { ...FIXTURES.health, mutations: 'disabled' } });
    await page.goto('/');

    // §11.5: a banner, plus a badge in the masthead. Both persistent — a toast
    // would expire while the operator is still wondering why Delete is greyed.
    await expect(page.getByTestId('read-only-banner')).toBeVisible();
    await expect(page.getByTestId('read-only-badge')).toBeVisible();
  });
});

test.describe('console authentication', () => {
  test('signs in through LDAP and opens administrator user management', async ({ page }) => {
    await mockApi(page, { auth: { ldapEnabled: true } });
    await page.goto('/users');

    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await page.getByLabel('Username').fill('directory.admin');
    await page.getByLabel('Password').fill('directory-password');
    await page.getByLabel('Account source').selectOption('ldap');
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page.getByRole('heading', { name: 'Users' })).toBeVisible();
    await expect(page.getByText('LDAP authentication enabled')).toBeVisible();
    await expect(page.getByRole('grid', { name: 'Console users' })).toContainText('Directory Admin');
    await expect(page.getByRole('navigation', { name: 'Console navigation' }).getByRole('link', { name: 'Users' })).toBeVisible();
  });

  test('opens the local-user form and signs out through the masthead', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: true, ldapEnabled: true } });
    await page.goto('/users');

    await page.getByRole('button', { name: 'Add local user' }).click();
    await expect(page.getByRole('dialog', { name: 'Console user' })).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await page.getByRole('button', { name: 'User menu' }).click();
    await page.getByRole('menuitem', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  });
});

test.describe('contract rule 11.1 — partial responses are never silent', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('overview shows a persistent banner naming each unavailable source', async ({ page }) => {
    await page.goto('/');

    const banner = page.getByTestId('partial-banner').first();
    await expect(banner).toBeVisible({ timeout: 15000 });

    // Named, not counted: the operator has to be able to tell which question
    // went unanswered.
    await expect(banner).toContainText('nodes');
    await expect(banner).toContainText('not permitted to read');

    // `unsupported` is not an error (§1.2) — a cluster without metrics.k8s.io is
    // a normal cluster, and the row says so rather than colouring it red.
    await expect(banner).toContainText('not present on this cluster');

    // Persistent. A toast would have gone by now.
    await page.waitForTimeout(7000);
    await expect(banner).toBeVisible();
  });

  test('a complete listing shows no banner at all', async ({ page }) => {
    await page.goto('/namespaces');
    await expect(page.getByTestId('partial-banner')).toHaveCount(0);
  });
});

test.describe('contract rule 11.2 — a null number is never a zero', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test('overview renders an em dash for the collectors that failed', async ({ page }) => {
    await page.goto('/');

    const missing = page.locator('[data-testid="nullable-cell"][data-nullable="true"]');
    await expect(missing.first()).toBeVisible({ timeout: 15000 });
    await expect(missing.first()).toHaveText('—');

    // The whole point: not one of the unknown values renders as a number, and
    // certainly not as 0. `capacity` and `requested` are null in the fixture.
    const texts = await missing.allTextContents();
    for (const text of texts) {
      expect(text.trim()).toBe('—');
      expect(text.trim()).not.toBe('0');
    }
  });

  test('the dash carries an explanation rather than standing alone', async ({ page }) => {
    await page.goto('/');
    const missing = page.locator('[data-testid="nullable-cell"][data-nullable="true"]').first();
    await expect(missing).toBeVisible({ timeout: 15000 });

    // Accessible name, so the reason survives for a screen reader too — several
    // announce a lone em dash as nothing at all.
    await expect(missing).toHaveAttribute('aria-label', /could not be read/i);
  });

  test('a real zero still renders as zero', async ({ page }) => {
    await page.goto('/');
    // Present values keep data-nullable="false"; nothing in the app may route a
    // known number through the dash path.
    const present = page.locator('[data-testid="nullable-cell"][data-nullable="false"]');
    if (await present.count()) {
      for (const text of await present.allTextContents()) {
        expect(text.trim()).not.toBe('—');
      }
    }
  });
});
