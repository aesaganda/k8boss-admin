import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * The operator portal (§16).
 *
 * What is asserted here is not that packages list. It is that the page cannot
 * tell an operator something untrue about somebody else's cluster, in the four
 * places where a confident wrong answer costs them something:
 *
 * **A cluster with no OLM is an ordinary cluster.** Every source `unsupported`
 * is a fact, not a fault, and it renders calm and blue exactly once. Rendering
 * it red is how red stops meaning anything on every other page in the console.
 *
 * **`installed: null` is not "not installed".** The Subscription listing failed;
 * whether this operator is already here is unknown. Rendering that as absence
 * invites a second Subscription on top of one that exists, and two resolutions
 * then race for the same CRDs.
 *
 * **A Subscription that OLM has not acted on is not a failed one.** `phase:
 * null` with `installedCSV: null` means nothing has happened yet — usually an
 * InstallPlan waiting for a human. `Failed` would send somebody to debug an
 * install that was never attempted.
 *
 * **`applied: true` is one object, not an operator.** The write creates a
 * Subscription. Everything after it belongs to OLM, happens later, and can fail
 * for reasons this response cannot see — so the success alert must not claim an
 * installation it has no evidence of.
 */

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

/**
 * The catalog as a cluster with no Operator Lifecycle Manager answers it.
 *
 * Mapped off the real fixture rather than written out, so a source field added
 * to one is added to both. `catalogs: null` and an empty `items[]` are what
 * `catalog()` returns when the APIs are not served — and `unavailable[]` stays
 * empty on purpose: an unsupported source never becomes a partial read, which
 * is the whole reason this state is reported separately from the banner.
 */
const NO_OLM = {
  ...FIXTURES.portalCatalog,
  items: [],
  catalogs: null,
  sources: FIXTURES.portalCatalog.sources.map((source) => ({
    ...source,
    version: null,
    state: 'unsupported',
    detail: `This cluster does not serve ${source.kind} objects.`,
  })),
};

/** The catalog with the Subscription listing refused: every row's `installed` is null. */
const INSTALLED_UNREADABLE = {
  ...FIXTURES.portalCatalog,
  items: FIXTURES.portalCatalog.items.map((row) => ({
    ...row,
    installed: null,
    installations: null,
  })),
  partial: true,
  unavailable: [
    {
      group: 'operators.coreos.com',
      resource: 'subscriptions',
      namespace: null,
      reason: 'forbidden',
      detail: 'subscriptions.operators.coreos.com is forbidden at the cluster scope',
    },
  ],
};

async function openPortal(page) {
  await page.goto('/portal');
  await expectPageRendered(page, 'Operator portal');
}

/** Open the Subscribe dialog on a catalog row and get as far as a plan. */
async function openSubscribe(page, rowName, namespace = 'monitoring') {
  await page.getByRole('row', { name: rowName }).getByRole('button').click();
  await page.getByRole('menuitem', { name: 'Subscribe…' }).click();
  await expect(page.getByTestId('subscribe-form')).toBeVisible();

  await page.getByTestId('subscribe-namespace').fill(namespace);
  // The plan is what fills the dialog in; acting before it lands races the
  // request, and an e2e suite that fails one run in ten gets re-run rather
  // than read.
  await expect(page.getByTestId('subscribe-document')).toBeVisible();
}

test.describe('the catalog', () => {
  test('lists what this cluster can install, and where from', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPortal(page);

    const row = page.getByRole('row', { name: /Prometheus Operator/ });
    await expect(row).toBeVisible();
    await expect(row).toContainText('prometheus');
    await expect(row).toContainText('Red Hat');
    await expect(row).toContainText('0.71.2');

    // The CatalogSources the packages came from, with the registry state a
    // stale catalog would be explained by.
    const catalogs = page.getByTestId('portal-catalogs');
    await expect(catalogs).toContainText('Community Operators');
    // `healthy: null` is grey and says Unknown — a CatalogSource created moments
    // ago has published no connection state, and calling that unhealthy sends
    // somebody to debug a registry that is merely still starting.
    await expect(catalogs.getByTestId('status-badge').filter({ hasText: 'Private mirror' })).toBeVisible();
  });
});

test.describe('a cluster with no Operator Lifecycle Manager', () => {
  test('says so once, calmly, and never as an error', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: NO_OLM });
    await openPortal(page);

    const notice = page.getByTestId('portal-unsupported');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText('not a fault in this console or in the cluster');

    // The table said the listing happened and found nothing, which is the true
    // answer — not "could not load".
    const empty = page.getByTestId('empty-state');
    await expect(empty).toBeVisible();
    await expect(empty).toContainText('The listing succeeded');

    // Nothing red anywhere: no error panel, no partial banner (an unsupported
    // API is not a failed read), and no danger-variant alert. §1.2 — rendering
    // an absent API as an error is how operators learn to ignore red.
    await expect(page.getByTestId('error-state')).toHaveCount(0);
    await expect(page.getByTestId('partial-banner')).toHaveCount(0);
    await expect(page.locator('[class*="pf-m-danger"]')).toHaveCount(0);

    // And it says it ONCE. `catalogs: null` arrives here because the
    // CatalogSource API is not served, not because a read failed — so the strip
    // must stay silent rather than reporting an outage that is not happening
    // directly beneath a notice correctly saying there is no OLM.
    await expect(page.getByTestId('portal-catalogs-unknown')).toHaveCount(0);
    await expect(page.getByTestId('portal-catalogs-unsupported')).toHaveCount(0);
    await expect(page.getByTestId('portal-catalogs')).toHaveCount(0);
  });
});

test.describe('installing OLM from the empty portal (§33)', () => {
  test('offers the install, and the plan is readable', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: NO_OLM });
    await openPortal(page);

    const panel = page.getByTestId('olm-panel');
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('0.35.0');

    // The plan button is never gated. What it contains is the ClusterRole that
    // grants OLM every verb on every resource, and deciding whether to set
    // ADMIN_OLM_INSTALL_ENABLED means reading it.
    await panel.getByTestId('olm-plan-open').click();
    await expect(page.getByTestId('olm-plan-object').first()).toBeVisible();
    await expect(page.getByText('system:controller:operator-lifecycle-manager')).toBeVisible();
    // Both phases are named, because the order is not cosmetic: phase two's
    // objects are instances of phase one's CustomResourceDefinitions.
    await expect(page.getByText('Phase one — the APIs')).toBeVisible();
    await expect(page.getByText('Phase two — OLM itself')).toBeVisible();
  });

  test('refuses to preview until every consequence is ticked', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: NO_OLM });
    await openPortal(page);

    await page.getByTestId('olm-install').click();
    await expect(page.getByTestId('olm-consequences')).toBeVisible();

    // The ClusterRole is quoted rather than paraphrased. "Broad permissions"
    // would make the object in the diff sound smaller than it is.
    await expect(page.getByTestId('olm-ack-cluster_admin_grant')).toBeVisible();
    await expect(page.getByTestId('olm-ack-crd_ownership')).toBeVisible();

    const preview = page.getByRole('button', { name: /Preview the install/ });
    await expect(preview).toBeDisabled();

    await page.getByTestId('olm-ack-cluster_admin_grant').check();
    await expect(preview).toBeDisabled();
    await page.getByTestId('olm-ack-crd_ownership').check();
    await expect(preview).toBeEnabled();
  });

  test('turning the community catalog on clears the ticks and adds a third', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: NO_OLM });
    await openPortal(page);

    await page.getByTestId('olm-install').click();
    await page.getByTestId('olm-ack-cluster_admin_grant').check();
    await page.getByTestId('olm-ack-crd_ownership').check();
    await expect(page.getByRole('button', { name: /Preview the install/ })).toBeEnabled();

    // Consent given for two consequences must not survive into three. The
    // backend refuses the write naming the missing code; this is the half that
    // stops the operator meeting that refusal at confirm time.
    await page.getByTestId('olm-community-catalog').check();
    await expect(page.getByTestId('olm-ack-community_catalog')).toBeVisible();
    await expect(page.getByTestId('olm-ack-cluster_admin_grant')).not.toBeChecked();
    await expect(page.getByRole('button', { name: /Preview the install/ })).toBeDisabled();
  });

  test('a dry run shows phase two as rendered, not as projected', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: NO_OLM });
    await openPortal(page);

    await page.getByTestId('olm-install').click();
    await page.getByTestId('olm-ack-cluster_admin_grant').check();
    await page.getByTestId('olm-ack-crd_ownership').check();
    await page.getByRole('button', { name: /Preview the install/ }).click();

    const report = page.getByTestId('olm-object-report');
    await expect(report).toBeVisible();
    // On a cluster with no OLM the API server genuinely cannot project an
    // OperatorGroup, so those diffs are the bundle's own manifests and are
    // labelled as such. Calling them "projected" would make a promise about an
    // admission check that never happened.
    // Phase one IS projected by the API server — apiextensions.k8s.io is served
    // everywhere — and phase two is not, because its CRDs do not exist yet.
    // Asserted as that pairing rather than as "something is rendered": the
    // first version of this checked a `data-phase` attribute nothing carried,
    // so it passed whatever the component did.
    await expect(
      report.locator('[data-testid="olm-object"][data-phase="crds"][data-projection="server"]'),
    ).not.toHaveCount(0);
    await expect(
      report.locator('[data-testid="olm-object"][data-phase="core"][data-projection="server"]'),
    ).toHaveCount(0);
    await expect(
      report.locator('[data-testid="olm-object"][data-phase="core"][data-projection="rendered"]'),
    ).not.toHaveCount(0);
    await expect(page.getByText('rendered by the console').first()).toBeVisible();
  });

  test('a finished install never claims OLM is running', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: NO_OLM });
    await openPortal(page);

    await page.getByTestId('olm-install').click();
    await page.getByTestId('olm-ack-cluster_admin_grant').check();
    await page.getByTestId('olm-ack-crd_ownership').check();
    await page.getByRole('button', { name: /Preview the install/ }).click();
    await page.getByTestId('mutation-typed').fill('operator-lifecycle-manager');
    await page.getByRole('button', { name: /Install it/ }).click();

    // "objects were accepted", never "OLM is installed and working". At this
    // moment the package server has certainly not registered yet. Scoped to the
    // dialog because the toast carries the same headline, and the dialog is the
    // one that stays on screen with the per-object report under it.
    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText(/objects were accepted/);
    await expect(summary).toContainText(/not knowable from this response/);
  });

  test('installed but not ready is reported as two separate answers', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      portalCatalog: NO_OLM,
      olmStatus: FIXTURES.olmStatusInstalledNotReady,
    });
    await openPortal(page);

    const panel = page.getByTestId('olm-panel');
    await expect(panel).toContainText('Objects are on the cluster');
    await expect(panel).toContainText('Not yet');
    await expect(page.getByTestId('olm-installed-not-ready')).toBeVisible();
    await expect(page.getByTestId('olm-installed-not-ready')).toContainText(
      'that is correct, not a failure',
    );

    // And the install is not offered over an OLM that is already there.
    await expect(page.getByTestId('olm-install')).toBeDisabled();
  });

  test('the gate disables the install with the reason, and leaves the plan readable', async ({
    page,
  }) => {
    const gated = {
      ...NO_OLM,
      olmInstall: {
        enabled: false,
        detail: 'Installing OLM is switched off (ADMIN_OLM_INSTALL_ENABLED is not set).',
      },
    };
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: gated });
    await openPortal(page);

    await expect(page.getByTestId('olm-install')).toBeDisabled();
    // Rule 11.4: disabled WITH the reason, not hidden.
    await page.getByTestId('olm-install').hover();
    await expect(page.getByText(/ADMIN_OLM_INSTALL_ENABLED/)).toBeVisible();
    // The plan stays readable, which is the whole point of the gate being where
    // it is: you cannot decide to open it without reading what it allows.
    await expect(page.getByTestId('olm-plan-open')).toBeEnabled();
  });
});

test.describe('a Subscription listing that did not answer', () => {
  test('renders Unknown, never "not installed"', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalCatalog: INSTALLED_UNREADABLE });
    await openPortal(page);

    const row = page.getByRole('row', { name: /Prometheus Operator/ });
    const unknown = row.locator('[data-testid="nullable-cell"][data-nullable="true"]');
    await expect(unknown).toHaveCount(1);
    // The sentence is on the cell, because the operator hovering this one is
    // about to decide whether to create a second Subscription.
    await expect(unknown).toHaveAttribute('aria-label', /NOT "not installed"/);

    // And the words that would invite exactly that are nowhere in the row.
    await expect(row.getByText('not installed')).toHaveCount(0);
    // The failed read is reported as a failed read, in the banner that exists
    // for it — which is a different thing from the unsupported notice above.
    await expect(page.getByTestId('partial-banner')).toBeVisible();
    await expect(page.getByTestId('portal-unsupported')).toHaveCount(0);
  });
});

test.describe('a Subscription OLM has not acted on', () => {
  test('renders an em dash and the reason, never Failed and never blank', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPortal(page);

    await page.getByRole('tab', { name: 'Installed' }).click();

    const row = page.getByRole('row', { name: /grafana-operator/ });
    await expect(row).toBeVisible();

    // Two nulls in one row: no installed version, and no phase. Both are the
    // dash, and both carry the sentence saying which of the two nulls it is —
    // OLM has not acted yet, as against a CSV read that failed. Addressed by
    // column rather than by index, because this row has a third legitimate
    // null (no channel) and a positional assertion would move with it.
    for (const label of ['Installed version', 'Phase']) {
      const cell = row.locator(
        `td[data-label="${label}"] [data-testid="nullable-cell"][data-nullable="true"]`,
      );
      await expect(cell).toHaveCount(1);
      await expect(cell).toHaveAttribute(
        'aria-label',
        /has not installed anything for this Subscription yet/,
      );
    }

    // The two renderings that would send someone to debug an install that was
    // never attempted.
    await expect(row.getByText('Failed')).toHaveCount(0);
    await expect(row.getByText('Succeeded')).toHaveCount(0);

    // What is actually holding it up, on screen rather than implied.
    await expect(row).toContainText('awaiting approval');

    // The healthy row is unaffected: one row's unknown phase is not the table's.
    await expect(page.getByRole('row', { name: /prometheus/ })).toContainText('Succeeded');
  });
});

test.describe('the subscribe handshake', () => {
  /** A plan for a namespace with no OperatorGroup: one consequence, ready false. */
  const NO_OPERATOR_GROUP = (body) => ({
    ...FIXTURES.portalPlan,
    package: body.package,
    namespace: body.namespace,
    target: {
      namespace: body.namespace,
      operatorGroups: [],
      requiredInstallMode: null,
      ready: false,
      detail: `${body.namespace} has no OperatorGroup. OLM will not install an operator into it.`,
    },
    consequences: [
      {
        code: 'no_operator_group',
        label: 'This namespace has no OperatorGroup',
        consequence:
          'The Subscription will be created and OLM will mark the ClusterServiceVersion Failed ' +
          'with NoOperatorGroup. Nothing will install, and the operator will keep looking ' +
          'subscribed.',
        mitigation:
          'Create one OperatorGroup in this namespace first, or subscribe into a namespace that ' +
          'already has one.',
      },
    ],
  });

  test('Preview stays disabled until every consequence is acknowledged', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, portalPlan: NO_OPERATOR_GROUP });
    await openPortal(page);
    await openSubscribe(page, /Prometheus Operator/, 'no-group');

    // The consequence is stated in terms of what happens to the cluster, and
    // what to do instead — not "are you sure".
    const consequences = page.getByTestId('subscribe-consequences');
    await expect(consequences).toBeVisible();
    await expect(consequences).toContainText('NoOperatorGroup');
    await expect(consequences).toContainText('Create one OperatorGroup in this namespace first');

    // `ready: false` is a warning that says the Subscription can still be
    // created and will sit there — not a claim that the write will fail.
    await expect(page.getByTestId('subscribe-target')).toHaveAttribute('data-ready', 'false');
    await expect(page.getByTestId('subscribe-target-not-ready')).toBeVisible();

    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    // Rule 11.4: disabled, and saying why. Twice — on the control and beside it.
    await expect(preview).toHaveAttribute('title', /Acknowledge what this subscription means/);
    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'This namespace has no OperatorGroup',
    );

    await page.getByTestId('subscribe-ack-no_operator_group').check();
    await expect(preview).toBeEnabled();
  });

  test('the success alert does not claim the operator is installed', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openPortal(page);
    await openSubscribe(page, /Prometheus Operator/);

    await page.getByTestId('mutation-preview').click();
    await expect(page.getByTestId('diff-view')).toBeVisible();
    // §1.5: a dry run returns a full object, a resourceVersion and a diff, and
    // none of it is evidence that anything was written.
    await expect(page.getByTestId('mutation-summary')).toHaveCount(0);

    await page.getByTestId('mutation-confirm').click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toBeVisible();
    // The whole sentence: one object exists, and nothing is installed.
    await expect(summary).toContainText('The Subscription prometheus was created');
    await expect(summary).toContainText('nothing is installed yet');
    // The CSV is named as *expected*, and the page that answers the real
    // question is named too.
    await expect(summary).toContainText('It is expected to install');
    await expect(summary).toContainText('prometheusoperator.0.71.2');
    await expect(summary).toContainText('The Installed tab is where that question is actually answered');

    // The claims a green alert is otherwise read as making.
    await expect(summary).not.toContainText('Applied to the cluster');
    await expect(summary).not.toContainText('successfully installed');
  });
});
