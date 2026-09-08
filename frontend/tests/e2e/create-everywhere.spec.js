/**
 * Rule 11.10 and 11.11 — a create button on every resource listing, and the
 * starters behind it.
 *
 * What is worth testing here is not that a button exists. It is the order the
 * button answers two questions in, because nothing on the wire distinguishes
 * them: the API not serving a create and the caller not being allowed one are
 * two different sentences that a single boolean would collapse into whichever
 * one the code happened to check first — and the wrong one sends an operator to
 * widen a ClusterRole that was already correct.
 *
 * The other half is the starter picker. Choosing another starter is the one
 * destructive thing this dialog can do before it has sent anything anywhere,
 * and "destructive" here means the operator's own typing, so the confirmation
 * has to appear exactly when there is something to lose and not otherwise.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

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

/** Allow everything except `create` on one resource, which is refused by name. */
const DENY_CREATE_ON = (resource) => (checks) =>
  ALLOW_ALL(checks).map((result) =>
    result.verb === 'create' && result.resource === resource
      ? {
          ...result,
          allowed: false,
          reason: `${resource} is forbidden`,
          hint: `Grant \`create\` on \`${resource}\` to the console's ServiceAccount.`,
          subject: 'system:serviceaccount:k8boss-admin:console',
        }
      : result,
  );

/** The one create button in a tab's toolbar, whatever it currently says. */
const createButton = (page) => page.getByRole('button', { name: /^Create/ });

/** Scope the console to one namespace, the way an operator does. */
async function selectNamespace(page, name) {
  await page.getByTestId('namespace-selector').click();
  await page.getByRole('menuitem', { name, exact: true }).click();
}

async function openTab(page, path, tab, options = {}) {
  await mockApi(page, { preflight: ALLOW_ALL, ...options });
  await page.goto(path);
  await page.getByRole('tab', { name: tab }).click();
}

test.describe('a create button on every listing', () => {
  test('the kind on the button comes from discovery, not from the tab title', async ({ page }) => {
    // "Endpoint Slices" trimmed of its `s` is "Endpoint Slice", and
    // "Network Policies" is "Network Policie". Neither is a kind, and a button
    // offering to create one is the defect standard with a click target on it.
    await openTab(page, '/network', 'Services');
    await expect(createButton(page)).toHaveText('Create Service…');

    await page.getByRole('tab', { name: 'Network Policies' }).click();
    await expect(createButton(page)).toHaveText('Create NetworkPolicy…');
  });

  test('a listing whose API serves no create says that, and not that you lack permission', async ({ page }) => {
    // Endpoints advertises get and list and nothing else. Every §9 check here
    // is allowed, so a button reporting RBAC would be reporting a denial that
    // did not happen.
    await openTab(page, '/network', 'Endpoints');

    const button = createButton(page);
    await expect(button).toHaveAttribute('data-allowed', 'false');
    await button.hover();
    const tooltip = page.getByRole('tooltip');
    await expect(tooltip).toContainText('get, list');
    await expect(tooltip).toContainText('property of the API, not a permission problem');
    await expect(tooltip).not.toContainText('ServiceAccount');
  });

  test('a listing the catalog does not know about is not reported as a denial either', async ({ page }) => {
    // Leases are a tab in this console and absent from the fixture catalog,
    // which is exactly the shape of a cluster that does not serve them.
    await openTab(page, '/config', 'Leases');

    const button = createButton(page);
    await expect(button).toHaveText('Create…');
    await expect(button).toHaveAttribute('data-allowed', 'false');
    await button.hover();
    await expect(page.getByRole('tooltip')).toContainText(
      'This cluster does not serve leases in coordination.k8s.io/v1.',
    );
  });

  test('a preflight denial names the grant, on the same button', async ({ page }) => {
    await openTab(page, '/access', 'ServiceAccounts', { preflight: DENY_CREATE_ON('serviceaccounts') });

    const button = createButton(page);
    await expect(button).toHaveText('Create ServiceAccount…');
    await expect(button).toHaveAttribute('data-allowed', 'false');
    await button.hover();
    await expect(page.getByRole('tooltip')).toContainText('Grant `create` on `serviceaccounts`');

    // And the denial is this resource's, not the page's: RoleBindings on the
    // same page are unaffected, which is what a per-resource check buys.
    await page.getByRole('tab', { name: 'RoleBindings', exact: true }).click();
    await expect(createButton(page)).toHaveAttribute('data-allowed', 'true');
  });

  test('the dialog posts to the listing’s own resource, in the selected namespace', async ({ page }) => {
    const creates = [];
    await mockApi(page, { preflight: ALLOW_ALL, resourceCreates: creates });
    await page.goto('/access');
    await page.getByRole('tab', { name: 'ServiceAccounts' }).click();
    // Scope the console, because a namespaced create with no namespace is
    // refused before it is sent — which is a different assertion.
    await selectNamespace(page, 'prod');

    await createButton(page).click();
    await page.getByTestId('mutation-preview').click();
    await expect(page.getByTestId('mutation-confirm')).toBeVisible();

    expect(creates).toHaveLength(1);
    expect(creates[0]).toMatchObject({ group: 'core', version: 'v1', plural: 'serviceaccounts' });
    expect(creates[0].body.namespace).toBe('prod');
    expect(creates[0].body.dryRun).toBe(true);
    expect(creates[0].body.yaml).toContain('kind: ServiceAccount');
  });

  test('a cluster-scoped listing creates without a namespace', async ({ page }) => {
    const creates = [];
    await mockApi(page, { preflight: ALLOW_ALL, resourceCreates: creates });
    await page.goto('/storage');
    await page.getByRole('tab', { name: 'StorageClasses' }).click();

    await expect(createButton(page)).toHaveText('Create StorageClass…');
    await createButton(page).click();
    await expect(page.getByTestId('import-yaml-target')).toContainText('cluster-scoped');
    await page.getByTestId('mutation-preview').click();

    expect(creates[0]).toMatchObject({ group: 'storage.k8s.io', version: 'v1', plural: 'storageclasses' });
  });
});

test.describe('the create button on a CRD', () => {
  test('Custom Resources reaches a create for a kind this console has never heard of', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/custom-resources');
    await page.getByRole('button', { name: 'CiliumCIDRGroup' }).click();

    await expect(createButton(page)).toHaveText('Create CiliumCIDRGroup…');
    await createButton(page).click();

    // The skeleton, and it says it is one. Inventing a spec for somebody's CRD
    // would be a guess with a Create button under it.
    await expect(page.getByTestId('create-starter-description')).toContainText(
      'ships no starter for CiliumCIDRGroup',
    );
    await expect(page.getByTestId('yaml-editor-input')).toHaveValue(/kind: CiliumCIDRGroup/);
    await expect(page.getByTestId('yaml-editor-input')).toHaveValue(/apiVersion: cilium\.io\/v2alpha1/);

    // No form model, so YAML view — with the control present, disabled and
    // saying why rather than hidden.
    await expect(page.getByTestId('create-view-yaml')).toBeChecked();
    await expect(page.getByTestId('create-view-form')).toBeDisabled();
    await expect(page.getByTestId('create-view-form-reason')).toContainText('no form for CiliumCIDRGroup');
  });

  test('the API explorer’s listing carries the same button', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/explorer/core/v1/services');
    await expect(createButton(page)).toHaveText('Create Service…');
  });
});

test.describe('starters', () => {
  test('a kind with two starters offers both, and swapping an untouched one is immediate', async ({ page }) => {
    await openTab(page, '/network', 'Network Policies');
    await createButton(page).click();

    await expect(page.getByTestId('create-starters')).toBeVisible();
    await expect(page.getByTestId('create-starter-default-deny-ingress')).toBeChecked();

    await page.getByTestId('create-starter-allow-from-namespace').check();
    // No confirmation: there was nothing to lose, and a dialog about nothing
    // teaches people to click past the one that matters.
    await expect(page.getByTestId('create-starter-confirm')).toHaveCount(0);
    await expect(page.getByTestId('create-starter-allow-from-namespace')).toBeChecked();
    await page.getByTestId('create-view-yaml').check();
    await expect(page.getByTestId('yaml-editor-input')).toHaveValue(/name: allow-from-app/);
  });

  test('swapping a starter the operator has edited is confirmed, and refusing keeps the edit', async ({ page }) => {
    await openTab(page, '/network', 'Network Policies');
    await createButton(page).click();
    await page.getByTestId('create-view-yaml').check();

    const editor = page.getByTestId('yaml-editor-input');
    await editor.fill((await editor.inputValue()).replace('default-deny-ingress', 'mine'));

    await page.getByTestId('create-starter-allow-from-namespace').click();
    const confirm = page.getByTestId('create-starter-confirm');
    await expect(confirm).toContainText('replaces what is in the editor');
    // The radio has not moved: nothing has been replaced yet, and a control
    // showing the document the operator is not looking at would be lying.
    await expect(page.getByTestId('create-starter-default-deny-ingress')).toBeChecked();

    await page.getByTestId('create-starter-keep').click();
    await expect(confirm).toHaveCount(0);
    await expect(editor).toHaveValue(/name: mine/);

    // And accepting really does replace it.
    await page.getByTestId('create-starter-allow-from-namespace').click();
    await page.getByTestId('create-starter-replace').click();
    await expect(editor).toHaveValue(/name: allow-from-app/);
    await expect(editor).not.toHaveValue(/name: mine/);
  });

  test('a starter is a seed, not a claim that the cluster will accept it', async ({ page }) => {
    // The dry run is the authority. A starter that produced an error the
    // console had already decided about would be substituting its own opinion
    // for the API server's on somebody else's cluster.
    await mockApi(page, { preflight: ALLOW_ALL });
    // Layered after `mockApi` so it wins: this one has to answer with a status,
    // not just an error-shaped body, because the client branches on the code
    // and a 200 carrying an error is not a shape the API can produce.
    await page.route('**/resources/networking.k8s.io/v1/networkpolicies**', (route) =>
      route.request().method() === 'POST'
        ? route.fulfill({
            status: 422,
            contentType: 'application/json',
            body: JSON.stringify({
              error: 'invalid',
              message: 'admission webhook denied the request',
              detail: null,
              hint: null,
              context: { group: 'networking.k8s.io', resource: 'networkpolicies' },
            }),
          })
        : route.fallback(),
    );
    await page.goto('/network');
    await page.getByRole('tab', { name: 'Network Policies' }).click();
    await selectNamespace(page, 'prod');

    await createButton(page).click();
    await page.getByTestId('mutation-preview').click();
    await expect(page.getByTestId('mutation-error')).toContainText('admission webhook denied the request');
    await expect(page.getByTestId('mutation-confirm')).toHaveCount(0);
  });
});

test('the fixture catalog still carries every kind these specs create', () => {
  // A guard rather than a behaviour: half of the assertions above pass
  // vacuously against a disabled button if an entry is dropped from the
  // catalog fixture, because "this cluster does not serve it" is a legitimate
  // answer that looks exactly like a broken test.
  const served = new Set(FIXTURES.catalog.items.map((item) => `${item.apiVersion}/${item.kind}`));
  for (const wanted of [
    'v1/Service',
    'v1/ServiceAccount',
    'v1/Endpoints',
    'storage.k8s.io/v1/StorageClass',
    'networking.k8s.io/v1/NetworkPolicy',
    'cilium.io/v2alpha1/CiliumCIDRGroup',
  ]) {
    expect(served).toContain(wanted);
  }
});
