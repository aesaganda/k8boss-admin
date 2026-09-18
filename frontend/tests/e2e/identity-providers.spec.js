/**
 * Identity providers, configured from the console (§12.8, ADR-0011).
 *
 * The assertions are on the pairs this screen exists to keep apart, because
 * collapsing any of them decides who can sign in to the console:
 *
 *   enabled vs usable      Switched on with a required field empty gets no
 *                          button on the login page. A card saying "Enabled"
 *                          there would disagree with the screen people sign in
 *                          on, and nothing would explain the gap.
 *   stored vs environment  A kind with no row reads the deployment's variables.
 *                          The card says which, and Delete — which only exists
 *                          for a stored row — says that removing it gives the
 *                          variables back rather than switching the provider
 *                          off.
 *   omitted vs cleared     An untouched secret is not in the request at all
 *                          (the listing never returns one, so the form has
 *                          nothing to send back). Removing it is an explicit
 *                          checkbox that sends "".
 */
import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

const ADMIN = { auth: { authenticated: true, ldapEnabled: true } };

async function open(page, options = {}) {
  await mockApi(page, { ...ADMIN, ...options });
  await page.goto('/identity-providers');
  await expect(
    page.getByRole('heading', { level: 1, name: 'Identity providers' }),
  ).toBeVisible();
}

test.describe('§12.8 the provider list', () => {
  test('one card per configured kind, and an add button for the rest', async ({ page }) => {
    await open(page);

    await expect(page.getByTestId('provider-local')).toContainText('Local accounts');
    await expect(page.getByTestId('provider-ldap')).toContainText(
      'ldaps://directory.internal.example:636',
    );
    await expect(page.getByTestId('provider-ldap')).toContainText('cn=platform-admins,ou=groups');

    // Nothing is configured for these two anywhere, which is a different fact
    // from "switched off" and gets an add button rather than a card.
    await expect(page.getByTestId('provider-oauth')).toHaveCount(0);
    await expect(page.getByRole('button', { name: '+ OAuth 2.0' })).toBeVisible();
    await expect(page.getByRole('button', { name: '+ SAML 2.0' })).toBeVisible();
  });

  test('switched on and not usable is its own state, and names what is missing', async ({ page }) => {
    await open(page);

    const oidc = page.getByTestId('provider-oidc');
    await expect(oidc).toContainText('Incomplete');
    await expect(oidc).toContainText('No sign-in button until client_id is set');
  });

  test('a kind read from the environment says so and has nothing to delete', async ({ page }) => {
    await open(page);

    const ldap = page.getByTestId('provider-ldap');
    await expect(ldap).toContainText('the environment');
    await expect(ldap).toContainText('LDAP_');
    await expect(ldap.getByRole('button', { name: 'Configure' })).toBeVisible();
    await expect(ldap.getByRole('button', { name: 'Delete' })).toHaveCount(0);

    // A stored one has both.
    const oidc = page.getByTestId('provider-oidc');
    await expect(oidc).toContainText('this console');
    await expect(oidc.getByRole('button', { name: 'Edit' })).toBeVisible();
    await expect(oidc.getByRole('button', { name: 'Delete' })).toBeVisible();
  });
});

test.describe('§12.8 saving', () => {
  test('configuring a directory writes a row and the card re-reads as stored', async ({ page }) => {
    const providerWrites = [];
    await open(page, { providerWrites });

    await page.getByTestId('provider-ldap').getByRole('button', { name: 'Configure' }).click();
    const dialog = page.getByRole('dialog', { name: 'Identity provider' });
    // Seeded from the effective configuration, so saving one field does not
    // blank the others.
    await expect(dialog.getByLabel('User search base')).toHaveValue('ou=people,dc=example,dc=com');
    await dialog.getByLabel('Server URL').fill('ldaps://new.example.test:636');
    await dialog.getByRole('button', { name: 'Save' }).click();

    expect(providerWrites).toHaveLength(1);
    expect(providerWrites[0].kind).toBe('ldap');
    expect(providerWrites[0].body.enabled).toBe(true);
    expect(providerWrites[0].body.values.url).toBe('ldaps://new.example.test:636');
    // The untouched search base went with it — the form sends the whole
    // effective configuration, not a delta it cannot compute.
    expect(providerWrites[0].body.values.user_search_base).toBe('ou=people,dc=example,dc=com');

    const ldap = page.getByTestId('provider-ldap');
    await expect(ldap).toContainText('this console');
    await expect(ldap).toContainText('ldaps://new.example.test:636');
  });

  test('an untouched secret is not sent, and removing it is explicit', async ({ page }) => {
    const providerWrites = [];
    await open(page, { providerWrites });

    await page.getByTestId('provider-oidc').getByRole('button', { name: 'Edit' }).click();
    let dialog = page.getByRole('dialog', { name: 'Identity provider' });
    await expect(dialog).toContainText('A value is stored and is never shown here');
    await dialog.getByLabel('Client ID').fill('console');
    await dialog.getByRole('button', { name: 'Save' }).click();

    // The stored client secret was not in the request at all: absent means
    // keep, and the form never had the value to send.
    expect('client_secret' in providerWrites[0].body.values).toBe(false);
    expect(providerWrites[0].body.values.client_id).toBe('console');

    await page.getByTestId('provider-oidc').getByRole('button', { name: 'Edit' }).click();
    dialog = page.getByRole('dialog', { name: 'Identity provider' });
    await dialog.getByLabel('Remove the stored value').check();
    await dialog.getByRole('button', { name: 'Save' }).click();

    // Emptied deliberately, which is the only way this happens.
    expect(providerWrites[1].body.values.client_secret).toBe('');
  });

  test('saving a provider that still cannot work says so instead of claiming success', async ({ page }) => {
    await open(page);

    await page.getByTestId('provider-oidc').getByRole('button', { name: 'Edit' }).click();
    const dialog = page.getByRole('dialog', { name: 'Identity provider' });
    await dialog.getByLabel('Client ID').fill('');
    await dialog.getByRole('button', { name: 'Save' }).click();

    await expect(page.getByText(/still not offered at sign-in/)).toBeVisible();
    await expect(page.getByTestId('provider-oidc')).toContainText('Incomplete');
  });

  test('a refused save shows the reason and the hint, and changes nothing', async ({ page }) => {
    const providerWrites = [];
    await open(page, { providerWrites });
    // The trailing ** matters: `buildUrl` appends ?cluster_id=, and a glob
    // without it matches nothing — the save would reach the mock and succeed.
    await page.route('**/api/auth/providers/ldap**', (route) =>
      route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'invalid',
          message: 'LDAP credentials may not be sent in clear text. Use ldaps:// or enable StartTLS.',
          detail: null,
          hint: 'Set LDAP_URL, then restart the console.',
          context: { field: 'url' },
        }),
      }),
    );

    await page.getByTestId('provider-ldap').getByRole('button', { name: 'Configure' }).click();
    const dialog = page.getByRole('dialog', { name: 'Identity provider' });
    await dialog.getByLabel('Server URL').fill('ldap://plaintext.example.test:389');
    await dialog.getByRole('button', { name: 'Save' }).click();

    // The dialog stays open with the refusal in it: a form that closed on a
    // rejected save would lose what the operator typed.
    await expect(dialog).toContainText('may not be sent in clear text');
    await expect(dialog.getByLabel('Server URL')).toHaveValue('ldap://plaintext.example.test:389');
  });
});

test.describe('§12.8 deleting', () => {
  test('removing a stored row warns that the environment comes back', async ({ page }) => {
    const providerWrites = [];
    await open(page, { providerWrites });

    await page.getByTestId('provider-oidc').getByRole('button', { name: 'Delete' }).click();
    const dialog = page.getByRole('dialog');
    // The surprise an operator should get before the click: delete can leave a
    // provider available, with the deployment's own values.
    await expect(dialog).toContainText('OIDC_* environment variables then apply again');
    await dialog.getByRole('button', { name: 'Remove' }).click();

    expect(providerWrites).toEqual([{ kind: 'oidc', deleted: true }]);
    await expect(page.getByTestId('provider-oidc')).toContainText('the environment');
  });
});

test.describe('§12.8 who may see it', () => {
  test('a deployment without application authentication has no providers to manage', async ({ page }) => {
    await mockApi(page);
    await page.goto('/identity-providers');

    await expect(page.getByText('Identity providers are not in use')).toBeVisible();
  });

  test('a failed read says so rather than reporting no providers', async ({ page }) => {
    await mockApi(page, ADMIN);
    await page.route('**/api/auth/providers**', (route) =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'upstream_error',
          message: 'the console database is unavailable',
          detail: null,
          hint: null,
          context: {},
        }),
      }),
    );
    await page.goto('/identity-providers');

    // An empty page here would say "nobody can sign in", on a console that just
    // authenticated the request.
    await expect(
      page.getByText('The configured sign-in methods could not be read'),
    ).toBeVisible();
  });
});
