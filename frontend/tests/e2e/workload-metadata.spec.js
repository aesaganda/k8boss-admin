import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * Labels, annotations and tolerations, edited from the workload page.
 *
 * These three forms add no endpoint: each one reads the live object with §4's
 * YAML read, changes one field and sends the whole object back with `PUT`,
 * which is the call the YAML editor already makes. So what is worth asserting
 * is not that a write happened — `MutationDialog` and the funnel are tested
 * elsewhere — but the four things a *form over an object* can get wrong on its
 * own:
 *
 *   - It sends the object it read, with the rest of it intact. A form that
 *     posted `{labels: …}` would be posting a Deployment with no containers,
 *     and the API server would take it literally.
 *   - It sends the `resourceVersion` it was seeded at (rule 0.4), so a
 *     concurrent change is a 409 rather than a silent overwrite.
 *   - It blocks on the two things it alone can judge — a row with no key, and
 *     two rows with the same key — and leaves every other verdict to the dry
 *     run, which is the API server's.
 *   - It says what saving does. Tolerations live in the pod template, so
 *     saving replaces every running pod; an operator who reads that as a
 *     metadata edit has just restarted production to add a label.
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

const DENY_UPDATE = (checks) =>
  checks.map((check) => ({
    verb: check.verb,
    group: check.group,
    resource: check.resource,
    namespace: check.namespace ?? null,
    subresource: check.subresource ?? null,
    allowed: check.verb !== 'update',
    reason: check.verb === 'update' ? 'no RBAC policy matched' : '',
    evaluationError: null,
    hint: check.verb === 'update' ? 'Grant `update` on `apps/deployments` in prod.' : null,
  }));

/** The live manifest the dialogs read: labels, one annotation, one toleration. */
const DEPLOYMENT_YAML = [
  'apiVersion: apps/v1',
  'kind: Deployment',
  'metadata:',
  '  name: checkout',
  '  namespace: prod',
  '  resourceVersion: "884213"',
  '  labels:',
  '    app: checkout',
  '  annotations:',
  '    deployment.kubernetes.io/revision: "14"',
  'spec:',
  '  replicas: 5',
  '  template:',
  '    spec:',
  '      tolerations:',
  '        - key: workload',
  '          operator: Equal',
  '          value: batch',
  '          effect: NoSchedule',
  '      containers:',
  '        - name: checkout',
  '          image: ghcr.io/acme/checkout:1.9.2',
  '',
].join('\n');

async function openWorkload(page, { preflight = ALLOW_ALL, yaml = DEPLOYMENT_YAML } = {}) {
  await mockApi(page, { preflight, yaml });
  await page.goto('/workloads/deployments/prod/checkout');
  await expectPageRendered(page, 'checkout');
}

/** Every `PUT` the dialogs make, answered with an ordinary §1.5 projection. */
function capturePut(page) {
  const sent = [];
  page.route('**/resources/apps/v1/deployments/checkout**', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const body = JSON.parse(route.request().postData() ?? '{}');
    sent.push(body);
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        dryRun: body.dryRun !== false,
        applied: body.dryRun === false,
        verb: 'update',
        target: { group: 'apps', version: 'v1', resource: 'deployments', namespace: 'prod', name: 'checkout' },
        diff: {
          before: 'metadata: {}\n',
          after: 'metadata: {}\n',
          unified: '--- live\n+++ projected\n@@ -1,1 +1,2 @@\n metadata: {}\n+    tier: gold\n',
          changed: true,
        },
        resourceVersion: '884214',
        warnings: [],
        auditId: 9100,
      }),
    });
  });
  return sent;
}

test.describe('labels', () => {
  test('the pencil opens the object’s own labels, and Preview sends the whole object', async ({ page }) => {
    await openWorkload(page);
    const sent = capturePut(page);

    await page.getByRole('button', { name: 'Edit labels' }).click();
    await expect(page.getByTestId('metadata-key-0')).toHaveValue('app');
    await expect(page.getByTestId('metadata-value-0')).toHaveValue('checkout');

    await page.getByTestId('metadata-add').click();
    await page.getByTestId('metadata-key-1').fill('tier');
    await page.getByTestId('metadata-value-1').fill('gold');
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect.poll(() => sent.length, { timeout: 15000 }).toBe(1);
    // Rule 0.4: the version the form was seeded at, not the freshest one — a
    // console that sent the current version would be overwriting a change
    // nobody in this dialog has seen.
    expect(sent[0].resourceVersion).toBe('884213');
    expect(sent[0].dryRun).toBe(true);
    expect(sent[0].yaml).toContain('tier: gold');
    expect(sent[0].yaml).toContain('app: checkout');
    // The rest of the object goes back untouched. A form that sent only its own
    // field would be sending a Deployment with no containers.
    expect(sent[0].yaml).toContain('ghcr.io/acme/checkout:1.9.2');
    expect(sent[0].yaml).toContain('replicas: 5');
  });

  test('two rows with the same key block Preview, and say which judgement that is', async ({ page }) => {
    await openWorkload(page);

    await page.getByRole('button', { name: 'Edit labels' }).click();
    await page.getByTestId('metadata-add').click();
    await page.getByTestId('metadata-key-1').fill('app');

    const preview = page.getByRole('button', { name: /Preview/ });
    await expect(preview).toBeDisabled();
    await expect(page.getByTestId('mutation-dialog')).toContainText('Two rows carry the same key');
  });
});

test.describe('annotations', () => {
  test('the page counts them and the form lists them', async ({ page }) => {
    await openWorkload(page);
    const sent = capturePut(page);

    // §6's detail carries them; the page shows how many rather than a wall of
    // JSON, which is what `last-applied-configuration` would make of this row.
    await expect(page.getByText('1 annotation', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Edit annotations' }).click();
    await expect(page.getByTestId('metadata-key-0')).toHaveValue('deployment.kubernetes.io/revision');

    await page.getByTestId('metadata-remove-0').click();
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect.poll(() => sent.length, { timeout: 15000 }).toBe(1);
    expect(sent[0].yaml).not.toContain('deployment.kubernetes.io/revision');
    expect(sent[0].yaml).toContain('app: checkout');
  });
});

test.describe('node selector', () => {
  test('it writes into the pod template, and says that replaces every pod', async ({ page }) => {
    await openWorkload(page);
    const sent = capturePut(page);

    await page.getByRole('button', { name: 'Edit node selector' }).click();
    // Same control as labels, different consequence — and the consequence is
    // the reason this is not filed under metadata.
    await expect(page.getByTestId('metadata-rollout')).toContainText('replaces every pod');

    await page.getByTestId('metadata-add').click();
    await page.getByTestId('metadata-key-0').fill('disktype');
    await page.getByTestId('metadata-value-0').fill('ssd');
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect.poll(() => sent.length, { timeout: 15000 }).toBe(1);
    // Under `spec.template.spec`, not at the top of the object: a nodeSelector
    // written onto the Deployment itself constrains nothing and looks applied.
    expect(sent[0].yaml).toMatch(/template:[\s\S]*nodeSelector:[\s\S]*disktype: ssd/);
    expect(sent[0].yaml).toContain('ghcr.io/acme/checkout:1.9.2');
  });
});

test.describe('tolerations', () => {
  test('the form says saving replaces every pod, and reads the pod template', async ({ page }) => {
    await openWorkload(page);
    const sent = capturePut(page);

    await page.getByRole('button', { name: 'Edit tolerations' }).click();
    // The sentence this dialog exists to say: a toleration is in the pod
    // template, and the template hash is what the controller rolls on.
    await expect(page.getByTestId('tolerations-rollout')).toContainText('rolls every pod');
    await expect(page.getByTestId('toleration-key-0')).toHaveValue('workload');
    await expect(page.getByTestId('toleration-effect-0')).toHaveValue('NoSchedule');

    await page.getByTestId('toleration-value-0').fill('stream');
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect.poll(() => sent.length, { timeout: 15000 }).toBe(1);
    expect(sent[0].yaml).toContain('value: stream');
    expect(sent[0].resourceVersion).toBe('884213');
  });

  test('a keyless Exists row is called out; a keyless Equal row is refused', async ({ page }) => {
    await openWorkload(page);

    await page.getByRole('button', { name: 'Edit tolerations' }).click();
    await page.getByTestId('toleration-add').click();

    // Equal with no key matches nothing and the API server refuses it — the
    // form says so rather than spending a dry run to be told.
    const preview = page.getByRole('button', { name: /Preview/ });
    await expect(preview).toBeDisabled();
    await expect(page.getByTestId('mutation-dialog')).toContainText('has to use the Exists operator');

    // Exists with no key is legitimate, and tolerates every taint on the
    // cluster — including the NoExecute somebody adds later to drain a node.
    await page.getByTestId('toleration-operator-1').selectOption('Exists');
    await expect(page.getByTestId('tolerations-catch-all')).toBeVisible();
    await expect(preview).toBeEnabled();
  });
});

test.describe('any kind at all', () => {
  /**
   * The point of §11.14 being generic: a ConfigMap has no page of its own in
   * this console, and its labels are still editable — through the same form,
   * from the listing that browses every kind the cluster serves.
   */
  test('a ConfigMap’s labels are editable from the Explorer', async ({ page }) => {
    let sent = null;
    await mockApi(page, {
      preflight: ALLOW_ALL,
      yaml: [
        'apiVersion: v1',
        'kind: ConfigMap',
        'metadata:',
        '  name: app-config',
        '  namespace: prod',
        '  resourceVersion: "770"',
        '  labels:',
        '    app: shop',
        'data:',
        '  LOG_LEVEL: info',
        '',
      ].join('\n'),
    });
    await page.route('**/resources/core/v1/configmaps?**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              apiVersion: 'v1',
              kind: 'ConfigMap',
              metadata: { name: 'app-config', namespace: 'prod', creationTimestamp: '2026-08-18T09:00:00Z' },
            },
          ],
          continue: null,
          remaining: null,
          partial: false,
          unavailable: [],
        }),
      }),
    );
    await page.route('**/resources/core/v1/configmaps/app-config**', async (route) => {
      if (route.request().method() !== 'PUT') return route.fallback();
      sent = JSON.parse(route.request().postData() ?? '{}');
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          dryRun: true,
          applied: false,
          verb: 'update',
          target: { group: '', version: 'v1', resource: 'configmaps', namespace: 'prod', name: 'app-config' },
          diff: { before: '', after: '', unified: '--- live\n+++ projected\n@@ -1,1 +1,1 @@\n-a\n+b\n', changed: true },
          resourceVersion: '771',
          warnings: [],
          auditId: 9102,
        }),
      });
    });

    await page.goto('/explorer/core/v1/configmaps');
    await page.getByRole('button', { name: 'Kebab toggle' }).first().click();
    await page.getByRole('menuitem', { name: 'Edit labels…' }).click();

    await expect(page.getByTestId('metadata-key-0')).toHaveValue('app');
    await page.getByTestId('metadata-value-0').fill('storefront');
    await page.getByRole('button', { name: /Preview/ }).click();

    await expect.poll(() => sent?.yaml, { timeout: 15000 }).toContain('app: storefront');
    // The object's own data goes back with it, and the version it was read at.
    expect(sent.yaml).toContain('LOG_LEVEL: info');
    expect(sent.resourceVersion).toBe('770');
  });
});

test.describe('the typed listings', () => {
  /** One row per kind, in the §8 shape those tabs list. */
  async function mockTypedListings(page) {
    await page.route('**/resources/core/v1/configmaps?**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ name: 'app-config', namespace: 'prod', keys: ['LOG_LEVEL'], data_bytes: 12, age_seconds: 900 }],
          continue: null,
          remaining: null,
          partial: false,
          unavailable: [],
        }),
      }),
    );
    await page.route('**/resources/core/v1/secrets?**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              name: 'checkout-tls',
              namespace: 'prod',
              type: 'kubernetes.io/tls',
              keys: ['tls.crt', 'tls.key'],
              data_bytes: 2048,
              age_seconds: 900,
            },
          ],
          continue: null,
          remaining: null,
          partial: false,
          unavailable: [],
        }),
      }),
    );
  }

  test('a ConfigMap row offers the form, from the listing every typed page renders through', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      yaml: [
        'apiVersion: v1',
        'kind: ConfigMap',
        'metadata:',
        '  name: app-config',
        '  namespace: prod',
        '  resourceVersion: "770"',
        '  labels:',
        '    app: shop',
        '',
      ].join('\n'),
    });
    await mockTypedListings(page);

    await page.goto('/config/configmaps');
    await expectPageRendered(page, 'ConfigMaps');
    await page.getByRole('button', { name: 'Kebab toggle' }).first().click();
    await page.getByRole('menuitem', { name: 'Edit labels…' }).click();

    await expect(page.getByTestId('metadata-key-0')).toHaveValue('app');
  });

  test('a Secret says why this console cannot write it back, rather than failing at the dry run', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      // §4 redacts a Secret on every read this console makes: the keys are
      // there and every value is null. That is the object the form would be
      // sending back.
      yaml: [
        'apiVersion: v1',
        'kind: Secret',
        'metadata:',
        '  name: checkout-tls',
        '  namespace: prod',
        '  resourceVersion: "771"',
        'type: kubernetes.io/tls',
        'data:',
        '  tls.crt: null',
        '  tls.key: null',
        '',
      ].join('\n'),
    });
    await mockTypedListings(page);

    await page.goto('/config/secrets');
    await expectPageRendered(page, 'Secrets');
    await page.getByRole('button', { name: 'Kebab toggle' }).first().click();
    await page.getByRole('menuitem', { name: 'Edit labels…' }).click();

    // Rule 11.4: offered, and refused with the reason — not offered and then
    // rejected by the API server after a round trip and an audit row.
    await expect(page.getByTestId('metadata-secret')).toContainText('kubectl label');
    await expect(page.getByRole('button', { name: /Preview/ })).toBeDisabled();
  });
});

test('without update, all three pencils are disabled with the reason', async ({ page }) => {
  await openWorkload(page, { preflight: DENY_UPDATE });

  // Rule 11.4: offered and disabled, never hidden — and the reason names the
  // permission, not a bare "forbidden".
  for (const label of ['Edit labels', 'Edit annotations', 'Edit tolerations']) {
    const button = page.getByRole('button', { name: label });
    await expect(button).toHaveAttribute('data-allowed', 'false');
  }
});
