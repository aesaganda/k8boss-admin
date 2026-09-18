/**
 * Active sessions (§12.7) and the configured sign-in methods (§12.8).
 *
 * Both pages exist because the Users table cannot answer the questions they
 * answer, and the assertions here are on the pairs a convenient rendering would
 * collapse:
 *
 *   account vs session        Deactivating an account revokes its sessions. A
 *                             live session belonging to an account nobody wants
 *                             deactivated is what a stolen laptop produces, and
 *                             it is on no other screen.
 *   mine vs somebody else's   One row signs the operator out. It is labelled
 *                             before the click rather than discovered after it
 *                             at the login page.
 *   unknown vs blank          A session with no recorded address renders as an
 *                             em dash with a reason, never as an empty cell —
 *                             an administrator revoking by elimination reads
 *                             that cell as evidence (rule 11.2).
 */
import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

const ADMIN = { auth: { authenticated: true, ldapEnabled: true } };

test.describe('§12.7 active sessions', () => {
  test('lists who is signed in, from where, and marks the caller', async ({ page }) => {
    await mockApi(page, ADMIN);
    await page.goto('/sessions');

    await expect(page.getByRole('heading', { level: 1, name: 'Sessions' })).toBeVisible();
    await expect(page.getByText('3 active')).toBeVisible();

    const grid = page.getByRole('grid', { name: 'Active console sessions' });
    await expect(grid).toContainText('10.4.1.22');
    // The caller's own row, named as such.
    const own = grid.getByRole('row', { name: /10\.4\.1\.22/ });
    await expect(own).toContainText('this session');

    // Rule 11.2 on the row that recorded neither address nor browser: an em
    // dash carrying a reason, not an empty cell.
    const unrecorded = grid.getByRole('row', { name: /operator/ });
    await expect(unrecorded.getByTestId('nullable-cell').first()).toHaveAttribute(
      'data-nullable',
      'true',
    );
  });

  test('revoking a session confirms first and then re-reads the backend', async ({ page }) => {
    const sessionRevokes = [];
    await mockApi(page, { ...ADMIN, sessionRevokes });
    await page.goto('/sessions');

    const grid = page.getByRole('grid', { name: 'Active console sessions' });
    await grid.getByRole('row', { name: /10\.4\.1\.90/ }).getByRole('button').click();
    await page.getByRole('menuitem', { name: 'Revoke' }).click();
    await page.getByRole('button', { name: 'Revoke' }).click();

    // Gone because the listing was read again, not because the row was removed
    // locally — the mock answers without whatever digest was revoked.
    await expect(grid.getByRole('row', { name: /10\.4\.1\.90/ })).toHaveCount(0);
    expect(sessionRevokes).toEqual(['b'.repeat(64)]);
    await expect(grid).toContainText('10.4.1.22');
  });

  test('the row that signs the operator out says so before it is clicked', async ({ page }) => {
    await mockApi(page, ADMIN);
    await page.goto('/sessions');

    const grid = page.getByRole('grid', { name: 'Active console sessions' });
    await grid.getByRole('row', { name: /10\.4\.1\.22/ }).getByRole('button').click();
    await page.getByRole('menuitem', { name: 'Revoke (signs you out)' }).click();

    await expect(page.getByRole('dialog')).toContainText('signs you out immediately');
    await page.getByRole('button', { name: 'Cancel' }).click();
  });

  test('a deployment without application authentication holds no sessions', async ({ page }) => {
    await mockApi(page);
    await page.goto('/sessions');

    await expect(page.getByText('Session management is disabled')).toBeVisible();
    await expect(page.getByRole('grid', { name: 'Active console sessions' })).toHaveCount(0);
  });
});
