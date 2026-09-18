import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi, objectYaml } from './fixtures.js';

/**
 * Reading an existing object's YAML: the panel behind every detail drawer, the
 * pod console's YAML tab, and what both of them promise about how current the
 * bytes on screen are.
 *
 * The colouring is the same tokenizer the editor uses and is covered there.
 * What is covered here is the part that can lie:
 *
 * **A manifest on screen is always labelled with when it was read.** An
 * unlabelled YAML view is indistinguishable from a live one, and the object may
 * have been replaced by a controller ten minutes ago.
 *
 * **A failed refresh keeps the text and says the refresh failed.** Blanking the
 * panel throws away the copy the operator was reading; keeping it silently
 * tells them a stale object is current.
 *
 * **A change on the cluster reaches the editor as a warning, never as an
 * edit.** The `resourceVersion` sent on PUT stays the one the edit was based
 * on — adopting the newer one would turn §0.4's protection into the blind
 * overwrite it exists to prevent, arranged by the console itself.
 */

/**
 * Open the pod page on a given tab.
 *
 * By URL: §7.5 puts the tab in the query string so every tab is a link, which
 * makes this a one-line navigation rather than three clicks through a table
 * that is covered by its own spec.
 */
async function openPod(page, tab = 'yaml') {
  await page.goto(`/pods/prod/checkout-7d9f8b6c4-hk2xv${tab ? `?tab=${tab}` : ''}`);
  await expectPageRendered(page, 'checkout-7d9f8b6c4-hk2xv');
}

function panelLines(page) {
  return page.getByTestId('code-block').locator('.admin-yaml-code__text').allTextContents();
}

function panelNumbers(page) {
  return page.getByTestId('code-block').locator('.admin-yaml-code__no').allTextContents();
}

test.describe('reading an object’s YAML', () => {
  test('clicking a pod leads to its manifest, numbered and coloured', async ({ page }) => {
    await mockApi(page);
    await openPod(page);

    // `allTextContents` below does not auto-wait, and the panel starts as a
    // skeleton — the pod page's heading is up before its YAML tab has read
    // anything, so without this the comparison races the fetch.
    await expect(page.getByTestId('yaml-panel')).toBeVisible();

    const expected = objectYaml({ name: 'checkout-7d9f8b6c4-hk2xv' }).split('\n');
    expect(await panelLines(page)).toEqual(expected);
    expect(await panelNumbers(page)).toEqual(expected.map((_, i) => String(i + 1)));

    // The same token classes the editor produces: one tokenizer, so an object
    // read here and the same object open in the editor are the same rendering
    // rather than two that happen to look alike.
    const block = page.getByTestId('code-block');
    await expect(block.locator('.admin-yaml__t--key').first()).toHaveText('apiVersion');
    await expect(block.locator('.admin-yaml__t--string').first()).toHaveText('"884213"');
    await expect(block.locator('.admin-yaml__t--number').first()).toHaveText('8080');
  });

  test('the row menu opens straight onto the manifest', async ({ page }) => {
    await mockApi(page);
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');

    await page.getByRole('button', { name: 'Kebab toggle' }).first().click();
    await page.getByRole('menuitem', { name: 'View YAML' }).click();

    // Straight to the pod's YAML tab rather than to its Details, because the
    // operator has already said which panel they want.
    await expect(page).toHaveURL(/\/pods\/prod\/checkout-7d9f8b6c4-hk2xv\?tab=yaml$/);
    await expect(page.getByTestId('yaml-panel')).toBeVisible();
    await expect(page.getByTestId('code-block')).toContainText('kind: Pod');
  });

  test('the manifest is labelled with when it was read', async ({ page }) => {
    await mockApi(page);
    await openPod(page);

    // Not "loaded" or "live" — the wall-clock time the bytes on screen came
    // back. Everything else the panel says depends on this being true.
    await expect(page.getByTestId('yaml-panel-read-at')).toContainText(/Read at \d/);
    await expect(page.getByTestId('yaml-panel')).toContainText('watching for changes');
  });

  test('Reload fetches again rather than re-rendering what is already there', async ({ page }) => {
    let reads = 0;
    await mockApi(page, {
      yaml: (n) => {
        reads = n;
        return objectYaml({ image: n === 1 ? 'checkout:1.4.2' : 'checkout:1.5.0' });
      },
    });
    await openPod(page);

    await expect(page.getByTestId('code-block')).toContainText('checkout:1.4.2');
    const before = reads;

    await page.getByTestId('yaml-panel-reload').click();

    // The new image, from the API, without waiting for the next poll: the
    // operator who has just changed something is not going to sit through ten
    // seconds of the old manifest to find out whether it worked.
    await expect(page.getByTestId('code-block')).toContainText('checkout:1.5.0');
    expect(reads).toBeGreaterThan(before);
  });

  test('a change on the cluster arrives without anybody clicking', async ({ page }) => {
    // The manifest changes from the third read on, so the panel can only show
    // it by having polled.
    await mockApi(page, {
      yaml: (n) => objectYaml({ image: n < 3 ? 'checkout:1.4.2' : 'checkout:9.9.9', resourceVersion: n < 3 ? '884213' : '884999' }),
    });
    await openPod(page);
    await expect(page.getByTestId('code-block')).toContainText('checkout:1.4.2');

    // Two poll intervals of headroom. The interval is the product's, not the
    // test's — a spec that shortened it would stop testing the shipped one.
    await expect(page.getByTestId('code-block')).toContainText('checkout:9.9.9', { timeout: 30000 });
    // And it says when it changed, separately from when it was last read:
    // "this object last changed at 16:20:31" is the sentence an operator
    // watching a rollout is actually after.
    await expect(page.getByTestId('yaml-panel-changed')).toBeVisible();
  });

  test('a failed refresh keeps the manifest and says it is not current', async ({ page }) => {
    await mockApi(page);
    await openPod(page);
    await expect(page.getByTestId('code-block')).toContainText('checkout:1.4.2');
    const readAt = await page.getByTestId('yaml-panel-read-at').textContent();

    // Every later read fails. Registered after mockApi, so it wins.
    //
    // Anchored under `/api/` rather than matching `yaml` anywhere in the URL.
    // The suite runs against a production build, so the page's own chunks are
    // real network requests: a bare `**/yaml**` also swallows
    // `/assets/yamlSyntax-*.js`, and the page then renders the error boundary's
    // "its JavaScript chunk did not download" instead of the panel this test is
    // about. That is a failure with nothing to do with the YAML endpoint, and
    // the only clue is a heading that never appears.
    await page.route('**/api/**/yaml**', (route) =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'cluster_unreachable',
          message: 'The cluster did not answer within the deadline.',
          detail: null,
          hint: null,
          context: {},
        }),
      }),
    );

    await page.getByTestId('yaml-panel-reload').click();

    // The manifest is still there — losing it would cost the operator the copy
    // they were reading, over one failed request.
    await expect(page.getByTestId('code-block')).toContainText('checkout:1.4.2');
    // And it is labelled as the older copy, with the time it was actually read
    // unchanged. A timestamp that moved on a failed refresh would be the panel
    // claiming to be current.
    await expect(page.getByTestId('yaml-panel-stale')).toBeVisible();
    await expect(page.getByTestId('yaml-panel-stale')).toContainText('the last refresh failed');
    expect(await page.getByTestId('yaml-panel-read-at').textContent()).toBe(readAt);
    await expect(page.getByTestId('yaml-panel')).not.toContainText('watching for changes');
  });

  test('nothing to show at all is still shown as a failure', async ({ page }) => {
    await mockApi(page);
    // Anchored under `/api/` — see the note on the refresh test above. This one
    // installs the route *before* the navigation, so an unanchored glob breaks
    // the page load itself rather than the read it is aiming at.
    await page.route('**/api/**/yaml**', (route) =>
      route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'rbac_denied',
          message: 'get pods is forbidden for the console ServiceAccount.',
          detail: null,
          hint: null,
          context: {},
        }),
      }),
    );
    await openPod(page);

    // Not an empty code block, which would read as an object with no fields.
    await expect(page.getByTestId('code-block')).toHaveCount(0);
    await expect(page.getByText("This object's YAML could not be read")).toBeVisible();
  });
});

/** Two ConfigMaps in the API explorer, so a spec can switch between them. */
async function mockConfigMapListing(page) {
  await page.route('**/resources/core/v1/configmaps?**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: ['app-config', 'other-config'].map((name) => ({
          apiVersion: 'v1',
          kind: 'ConfigMap',
          metadata: { name, namespace: 'prod', creationTimestamp: '2026-08-18T09:00:00Z' },
        })),
        continue: null,
        remaining: null,
        partial: false,
        unavailable: [],
      }),
    }),
  );
}

test.describe('switching between objects', () => {
  test('a second object never renders the first one’s manifest under its name', async ({ page }) => {
    await mockApi(page);
    await mockConfigMapListing(page);
    // The second object answers slowly, which is the window in which a panel
    // that kept the previous answer would be showing one object's manifest
    // under another object's heading — the wrong answer, delivered confidently,
    // on the screen from which the operator decides what to change.
    await page.route('**/other-config/yaml**', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      return route.fulfill({
        status: 200,
        contentType: 'text/plain; charset=utf-8',
        body: objectYaml({ kind: 'ConfigMap', name: 'other-config' }),
      });
    });

    await page.goto('/explorer/core/v1/configmaps');
    await page.getByRole('gridcell', { name: 'app-config', exact: true }).click();
    await expect(page.getByTestId('code-block')).toContainText('name: app-config');

    await page.getByRole('gridcell', { name: 'other-config', exact: true }).click();

    // While the second read is in flight there is nothing to show, and nothing
    // is what is shown.
    await expect(page.getByTestId('code-block')).toHaveCount(0);
    await expect(page.getByTestId('code-block')).toContainText('name: other-config');
    await expect(page.getByTestId('code-block')).not.toContainText('name: app-config');
  });
});

test.describe('editing while the cluster moves', () => {
  /** The Explorer's own listing, plus the write permission its menu is gated on. */
  async function openExplorerEdit(page, { yaml }) {
    await mockApi(page, { yaml });
    await page.route('**/access/preflight**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          results: ['update', 'delete', 'create'].map((id) => ({
            id,
            allowed: true,
            reason: '',
            evaluationError: null,
            hint: null,
          })),
        }),
      }),
    );
    await mockConfigMapListing(page);

    await page.goto('/explorer/core/v1/configmaps');
    await page.getByRole('button', { name: 'Kebab toggle' }).first().click();
    await page.getByRole('menuitem', { name: 'Edit YAML…' }).click();
    await expect(page.getByTestId('yaml-editor')).toBeVisible();
  }

  test('an object that changes underneath the editor warns, and never rewrites the edit', async ({ page }) => {
    await openExplorerEdit(page, {
      yaml: (n) =>
        objectYaml({
          kind: 'ConfigMap',
          name: 'app-config',
          resourceVersion: n < 3 ? '884213' : '884999',
        }),
    });

    const input = page.getByTestId('yaml-editor-input');
    await expect(input).toContainText('884213');

    // An edit in progress, of the kind that would be lost if a poll adopted the
    // cluster's copy on the operator's behalf.
    await input.fill('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\n  namespace: prod\n  resourceVersion: "884213"\ndata:\n  mine: yes\n');

    await expect(page.getByTestId('edit-yaml-changed')).toBeVisible({ timeout: 30000 });
    // The editor still holds what was typed. A watch that silently re-based the
    // edit would be a data-loss bug wearing a feature's clothes.
    await expect(input).toHaveValue(/mine: yes/);
    await expect(page.getByTestId('edit-yaml-adopt')).toContainText('Discard my changes');
  });

  test('the version sent on PUT is the one the edit started from', async ({ page }) => {
    let sent = null;
    await openExplorerEdit(page, {
      yaml: (n) =>
        objectYaml({ kind: 'ConfigMap', name: 'app-config', resourceVersion: n < 3 ? '884213' : '884999' }),
    });
    await page.route('**/resources/core/v1/configmaps/app-config?**', async (route) => {
      if (route.request().method() !== 'PUT') return route.fallback();
      sent = JSON.parse(route.request().postData() ?? '{}');
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'conflict',
          message: 'The object was modified; the resourceVersion you sent is stale.',
          detail: null,
          hint: null,
          context: { currentResourceVersion: '884999' },
        }),
      });
    });

    // Wait for the cluster's copy to move on, then preview anyway.
    await expect(page.getByTestId('edit-yaml-changed')).toBeVisible({ timeout: 30000 });
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect.poll(() => sent?.resourceVersion, { timeout: 15000 }).toBe('884213');
    // Which is exactly why the API server answers 409, and why the console must
    // not "helpfully" send 884999: that would be a blind overwrite of a change
    // nobody in this dialog has ever seen.
    expect(sent.dryRun).toBe(true);
  });
});
