import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * Routes (§13) and the shipped router (§14).
 *
 * What is asserted here is not that the feature works. It is that the console
 * cannot mislead an operator about what is reachable from outside their
 * cluster, or about what it is doing to the path that makes it reachable:
 *
 * **"Not on this cluster" and "we could not find out" are rendered
 * differently.** An unsupported backend is an ordinary fact in a neutral
 * notice; an unknown one is a warning that says so out loud. Collapsing them is
 * how an operator creates a second exposure on a hostname that is already
 * claimed.
 *
 * **An unclaimed exposure is not a rejected one.** Every Ingress is
 * `admitted: null` forever — the API has no admission condition — and an
 * Ingress with no published address is one no controller took. Neither is a
 * refusal, and neither is a success.
 *
 * **A lossy backend cannot be written past.** Asking an Ingress for passthrough
 * TLS produces a consequence the operator has to tick before Preview enables.
 * This is `force` on a drain, applied to traffic.
 *
 * **The router plan is readable with the feature switched off.** Being asked to
 * enable something sight unseen is the thing that stops people enabling it.
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
 * Pick a target Service, once the picker is actually a picker.
 *
 * The control starts as a text box and becomes a `<select>` when the
 * namespace's Services arrive, so acting the moment the namespace is typed
 * races the listing. Without this wait the specs fail roughly one run in ten,
 * on the assertion after the one that actually broke — and an e2e suite that
 * fails intermittently gets re-run rather than read, which costs it the only
 * thing it was for.
 */
async function pickService(page, index, name) {
  // A filterable dropdown, not a native select: open it, then click the option.
  // The menu is appended to the body so the dialog's scroll container cannot
  // clip it, which is why the option is located from `page` rather than from
  // within the toggle.
  const toggle = page.getByTestId(`route-target-service-${index}`);
  await expect(toggle).toBeVisible();
  await toggle.click();
  await page.getByTestId(`route-target-service-option-${name}`).click();
  await expect(toggle).toContainText(name);
}

async function openRoutes(page) {
  await page.goto('/routes');
  await expectPageRendered(page, 'Routes');
}

test.describe('the routes listing', () => {
  test('shows every exposure with the API it is written in', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    await expect(page.getByRole('link', { name: 'shop', exact: true })).toBeVisible();
    await expect(page.getByText('https://shop.example.com/')).toBeVisible();
    // The Kind column: the row is one exposure, and this says which API it is.
    await expect(page.getByRole('row', { name: /shop/ }).getByText('Ingress')).toBeVisible();
  });

  test('an unsupported backend is an ordinary fact, not an error', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const notice = page.getByTestId('routes-unsupported-backends');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText('OpenShift Route');
    await expect(notice).toContainText('not an error');
  });

  test('a backend we could not check is a warning that says it is not absence', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const warning = page.getByTestId('routes-unknown-backends');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('not the same as the cluster not having them');
    // And it is a *different* element from the unsupported notice — the whole
    // point is that the two are not one message.
    await expect(page.getByTestId('routes-unsupported-backends')).toBeVisible();
  });

  test('an exposure no controller has claimed is Unknown, never Refused', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const row = page.getByRole('row', { name: /admin/ });
    await expect(row.getByTestId('status-badge').filter({ hasText: 'Unknown' })).toBeVisible();
    await expect(row.getByText('Refused')).toHaveCount(0);
  });

  test('an exposure with no published address renders an em dash, not a blank', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const row = page.getByRole('row', { name: /admin/ });
    // `NullableCell` — rule 11.2. A blank cell reads as "no address configured";
    // the em dash carries the reason on hover.
    await expect(row.getByText('—').first()).toBeVisible();
  });

  test('a cluster serving none of the three says so instead of showing an empty table', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeCapabilities: {
        ...FIXTURES.routeCapabilities,
        items: FIXTURES.routeCapabilities.items.map((b) => ({ ...b, state: 'unsupported' })),
        partial: false,
        unavailable: [],
      },
      routes: { ...FIXTURES.routes, items: [] },
    });
    await openRoutes(page);

    await expect(page.getByTestId('routes-no-backends')).toBeVisible();
    await expect(page.getByTestId('routes-create')).toHaveAttribute('aria-disabled', 'true');
  });
});

test.describe('the address column', () => {
  test('the hostname is a link that opens away from the console', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const link = page.getByRole('link', { name: /^https?:\/\// }).first();
    await expect(link).toBeVisible();

    const href = await link.getAttribute('href');
    expect(href).toMatch(/^https?:\/\//);

    // Its own tab: this leaves the console for a workload on someone's
    // cluster, and losing the page you were working on to it is not a
    // navigation anyone asked for.
    await expect(link).toHaveAttribute('target', '_blank');
    // Not optional on a link whose target is a workload nobody has vetted.
    const rel = await link.getAttribute('rel');
    expect(rel).toContain('noopener');
    expect(rel).toContain('noreferrer');
  });

  test('an exposure with no hostname offers no link to follow', async ({ page }) => {
    // The empty state here is a reason, not a URL. Linking `—` would be a
    // link to nothing; linking a guessed host would be worse.
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routes: {
        ...FIXTURES.routes,
        items: FIXTURES.routes.items.map((r) => ({ ...r, hosts: [] })),
      },
    });
    await openRoutes(page);

    await expect(page.getByRole('link', { name: /^https?:\/\// })).toHaveCount(0);
  });
});

test.describe('the configuration screen', () => {
  test('offers a form and a YAML view of the same object', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    await page.getByTestId('routes-create').click();
    await expect(page.getByTestId('route-form')).toBeVisible();

    await page.getByTestId('route-name').fill('checkout');
    await page.getByTestId('route-namespace').fill('prod');
    await page.getByTestId('route-host').fill('checkout.example.com');
    await pickService(page, 0, 'checkout');
    await page.getByTestId('route-target-port-0').fill('8080');

    await page.getByRole('tab', { name: 'YAML' }).click();
    // The document the form compiled — not an empty editor the operator has to
    // fill in a second time.
    await expect(page.locator('#route-yaml')).toHaveValue(/kind: Ingress/);
    await expect(page.locator('#route-yaml')).toHaveValue(/name: checkout/);
  });

  test('a backend this cluster does not serve stays listed and disabled', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    // Rule 11.4: "why can I not choose a Route here" is asked exactly on the
    // clusters where the answer matters, so the option is present and inert
    // rather than absent.
    const option = page.locator('#route-backend option[value="openshift"]');
    await expect(option).toHaveAttribute('disabled', '');
    await expect(option).toContainText('not on this cluster');
  });

  test('editing the YAML disables the form so it cannot overwrite what was typed', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('checkout');
    await page.getByTestId('route-namespace').fill('prod');
    await pickService(page, 0, 'checkout');

    await page.getByRole('tab', { name: 'YAML' }).click();
    await page.locator('#route-yaml').fill(
      'apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: checkout\n  namespace: prod\n',
    );

    await page.getByRole('tab', { name: 'Form' }).click();
    await expect(page.getByTestId('route-form-locked')).toBeVisible();
    await expect(page.getByTestId('route-host')).toBeDisabled();
    // And it can be handed back, rather than the dialog being a dead end.
    await page.getByTestId('route-discard-yaml-edit').click();
    await expect(page.getByTestId('route-host')).toBeEnabled();
  });

  test('what the form is not showing is named, not merely hinted at', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeRender: (body) => ({
        backend: body.backend,
        kind: 'Ingress',
        group: 'networking.k8s.io',
        version: 'v1',
        plural: 'ingresses',
        document: {},
        yaml: 'apiVersion: networking.k8s.io/v1\nkind: Ingress\n',
        lossy: [],
        preserved: ['spec.rules[1]', 'metadata.annotations[haproxy.org/ssl-redirect]'],
        requested: [],
      }),
    });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('checkout');
    await page.getByTestId('route-namespace').fill('prod');
    await pickService(page, 0, 'checkout');

    const preserved = page.getByTestId('route-preserved');
    await expect(preserved).toBeVisible();
    await expect(preserved).toContainText('spec.rules[1]');
    await expect(preserved).toContainText('haproxy.org/ssl-redirect');
  });
});

/**
 * §13 hostname generation.
 *
 * Like the Service picker above it, this exists to stop the operator typing
 * something the console already knows. What is asserted here is mostly the
 * *restraint*: the generated hostname must stop the instant it is typed over,
 * and must not appear at all on a cluster with no wildcard domain to build it
 * under.
 */

/** The capabilities envelope with a wildcard domain on the cluster. */
function withAppDomain(domain, extra = {}) {
  return {
    ...FIXTURES.routeCapabilities,
    appDomain: {
      value: domain,
      source: 'configured',
      stored: domain,
      discovered: null,
      pattern: '<name>-<namespace>.<domain>',
      ...extra,
    },
  };
}

test.describe('the generated hostname', () => {
  test('is built from the name and namespace under the cluster domain', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeCapabilities: withAppDomain('apps.k8boss.local'),
    });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('shop');
    await page.getByTestId('route-namespace').fill('web');

    // OpenShift's rule, namespace included. Without it, `web` in two namespaces
    // generates one hostname twice and the second exposure quietly loses.
    await expect(page.getByTestId('route-host')).toHaveValue('shop-web.apps.k8boss.local');
  });

  test('follows the name until the operator types over it, then stops', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeCapabilities: withAppDomain('apps.k8boss.local'),
    });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('shop');
    await page.getByTestId('route-namespace').fill('web');
    await expect(page.getByTestId('route-host')).toHaveValue('shop-web.apps.k8boss.local');

    await page.getByTestId('route-host').fill('checkout.example.com');
    // The whole point: editing the name again must not take the field back. A
    // box that rewrites itself while somebody is typing in it is worse than one
    // that never filled itself in.
    await page.getByTestId('route-name').fill('shopfront');

    await expect(page.getByTestId('route-host')).toHaveValue('checkout.example.com');
  });

  test('offers the generated one back after an override, and takes it', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeCapabilities: withAppDomain('apps.k8boss.local'),
    });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('shop');
    await page.getByTestId('route-namespace').fill('web');
    await page.getByTestId('route-host').fill('mine.example.com');

    await page.getByTestId('route-host-reset').click();
    await expect(page.getByTestId('route-host')).toHaveValue('shop-web.apps.k8boss.local');
  });

  test('generates nothing when the cluster has no wildcard domain', async ({ page }) => {
    // The default fixture: appDomain.value is null. A suffix invented here
    // would produce an exposure that is created, admitted, and resolvable by
    // nobody — §14's failure with a hostname instead of a controller.
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('shop');
    await page.getByTestId('route-namespace').fill('web');

    await expect(page.getByTestId('route-host')).toHaveValue('');
    await expect(page.getByTestId('route-host-generated')).toHaveCount(0);
  });

  test('never rewrites the hostname of an exposure being edited', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeCapabilities: withAppDomain('apps.k8boss.local'),
    });
    await openRoutes(page);

    await page.getByRole('row', { name: /shop/ }).getByRole('button').click();
    await page.getByRole('menuitem', { name: 'Edit…' }).click();

    // Live and admitted under a hostname somebody chose. Changing it because a
    // domain was configured later is a routing outage delivered by a form the
    // operator opened to change something else.
    await expect(page.getByTestId('route-host')).toHaveValue('shop.example.com');
  });
});

test.describe('the Service picker', () => {
  test('lists the namespace Services instead of asking for a name', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();
    await page.getByTestId('route-namespace').fill('prod');

    await page.getByTestId('route-target-service-0').click();
    await expect(page.getByTestId('route-target-service-option-checkout')).toBeVisible();
    await expect(page.getByTestId('route-target-service-option-payments')).toBeVisible();

    // The filter is the reason this is a dropdown rather than a select: a
    // namespace with sixty Services makes a plain list scroll-and-squint.
    await page.getByTestId('route-target-service-filter-0').locator('input').fill('pay');
    await expect(page.getByTestId('route-target-service-option-checkout')).toHaveCount(0);
    await expect(page.getByTestId('route-target-service-option-payments')).toBeVisible();

    // And a filter that matches nothing says so, rather than showing an empty
    // menu an operator would read as "this namespace has no Services".
    await page.getByTestId('route-target-service-filter-0').locator('input').fill('zzz');
    await expect(page.getByTestId('route-target-service-nomatch-0')).toBeVisible();
  });

  test('fills the port in when the Service has exactly one', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();
    await page.getByTestId('route-namespace').fill('prod');

    await pickService(page, 0, 'checkout');
    await expect(page.getByTestId('route-target-port-0')).toHaveValue('http');

    // Two ports is a choice the console does not have the standing to make, so
    // it leaves the box alone rather than picking the first one.
    await pickService(page, 0, 'payments');
    await expect(page.getByTestId('route-target-port-0')).toHaveValue('http');
  });

  test('a namespace with no Services says so, rather than looking unread', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      services: { items: [], continue: null, remaining: null, partial: false, unavailable: [] },
    });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();
    await page.getByTestId('route-namespace').fill('prod');

    await expect(page.getByTestId('route-services-empty')).toBeVisible();
  });

  test('a Service listing that failed is not rendered as an empty namespace', async ({ page }) => {
    // The §1 rule applied to a dropdown. An empty picker after a failed read
    // says "this namespace has no Services", which sends the operator to create
    // one they already have — so the control falls back to free text and says
    // which question it could not answer.
    await mockApi(page, { preflight: ALLOW_ALL });
    // After mockApi, deliberately: page.route matches the most recently
    // registered handler first, so an override installed before the catch-all
    // never runs and the test would silently assert against the happy path.
    await page.route('**/api/resources/core/v1/services**', (route) =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'cluster_unreachable',
          message: 'The cluster API server could not be reached.',
        }),
      }),
    );
    await openRoutes(page);
    await page.getByTestId('routes-create').click();
    await page.getByTestId('route-namespace').fill('prod');

    await expect(page.getByTestId('route-services-unavailable')).toBeVisible();
    // Still usable: the operator who knows the name can type it.
    await page.getByTestId('route-target-service-0').fill('checkout');
    await expect(page.getByTestId('route-target-service-0')).toHaveValue('checkout');
  });
});

test.describe('a backend that cannot express what was asked for', () => {
  const LOSSY_RENDER = (body) => ({
    backend: body.backend,
    kind: 'Ingress',
    group: 'networking.k8s.io',
    version: 'v1',
    plural: 'ingresses',
    document: {},
    yaml: 'apiVersion: networking.k8s.io/v1\nkind: Ingress\n',
    lossy: [
      {
        feature: 'passthrough-tls',
        label: 'Pass TLS through to the pod without terminating it',
        consequence:
          'The Ingress API cannot express passthrough. This exposure will be written with the router terminating TLS instead, so client certificates the pod expects will not arrive.',
        mitigation: 'Write this as an OpenShift Route, or use a Gateway API TLSRoute.',
      },
    ],
    preserved: [],
    requested: ['passthrough-tls'],
  });

  async function askForPassthrough(page) {
    await mockApi(page, { preflight: ALLOW_ALL, routeRender: LOSSY_RENDER });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();
    await page.getByTestId('route-name').fill('checkout');
    await page.getByTestId('route-namespace').fill('prod');
    await pickService(page, 0, 'checkout');
    await page.getByTestId('route-host').fill('checkout.example.com');
    await page.getByTestId('route-termination').selectOption('passthrough');
  }

  test('states the consequence in terms of traffic, and what to do instead', async ({ page }) => {
    await askForPassthrough(page);

    const warning = page.getByTestId('route-lossy');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('client certificates the pod expects will not arrive');
    await expect(warning).toContainText('OpenShift Route');
  });

  test('Preview stays disabled until the consequence is acknowledged', async ({ page }) => {
    await askForPassthrough(page);

    const preview = page.getByTestId('mutation-preview');
    await expect(preview).toBeDisabled();
    // Rule 11.4: disabled, and saying why.
    await expect(preview).toHaveAttribute('title', /Acknowledge what Ingress cannot express/);

    await page.getByTestId('route-ack-passthrough-tls').check();
    await expect(preview).toBeEnabled();
  });

  test('the acknowledgement resets when the consequence changes', async ({ page }) => {
    await askForPassthrough(page);
    await page.getByTestId('route-ack-passthrough-tls').check();
    await expect(page.getByTestId('mutation-preview')).toBeEnabled();

    // Change the exposure so a different consequence is computed. Consent to
    // the old one must not carry forward onto a warning nobody read.
    await page.unroute('**/api/**');
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeRender: (body) => ({
        ...LOSSY_RENDER(body),
        lossy: [
          {
            feature: 'weighted-backends',
            label: 'Split traffic across several Services by weight',
            consequence: 'Only the first target will receive traffic.',
            mitigation: 'Write this as an OpenShift Route or an HTTPRoute.',
          },
        ],
      }),
    });
    await page.getByTestId('route-add-target').click();

    await expect(page.getByTestId('route-ack-weighted-backends')).not.toBeChecked();
    await expect(page.getByTestId('mutation-preview')).toBeDisabled();
  });
});

test.describe('writing an exposure', () => {
  test('shows the diff before anything is written, and only then confirms', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);
    await page.getByTestId('routes-create').click();

    await page.getByTestId('route-name').fill('checkout');
    await page.getByTestId('route-namespace').fill('prod');
    await page.getByTestId('route-host').fill('checkout.example.com');
    await pickService(page, 0, 'checkout');
    await page.getByTestId('route-target-port-0').fill('8080');

    await page.getByTestId('mutation-preview').click();
    await expect(page.getByTestId('diff-view')).toBeVisible();
    // §1.5: the dry run returns a full object and a resourceVersion, and none
    // of it is success. Nothing on screen may say the cluster changed.
    await expect(page.getByText('Applied to the cluster')).toHaveCount(0);

    await page.getByTestId('mutation-confirm').click();
    await expect(page.getByText('Applied to the cluster')).toBeVisible();
  });
});

test.describe('the shipped router', () => {
  test('says what it does not serve, before anyone writes an HTTPRoute', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const notice = page.getByTestId('router-not-served');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText('TCPRoute only');
    await expect(notice).toContainText("OpenShift's own router");
  });

  test('the plan is readable with the feature switched off', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    await expect(page.getByTestId('router-disabled')).toBeVisible();
    await page.getByTestId('router-plan-toggle').click();

    const plan = page.getByTestId('router-plan');
    await expect(plan).toBeVisible();
    await expect(plan).toContainText('haproxytech/kubernetes-ingress:3.2.13');
    await expect(plan).toContainText('ClusterRole');
  });

  test('an unreadable router state is Unknown, never "not installed"', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL, routerStatus: FIXTURES.routerUnknown });
    await openRoutes(page);

    const warning = page.getByTestId('router-unknown');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('second proxy');
    // And the state row agrees rather than contradicting the banner above it.
    await expect(page.getByTestId('router-panel').getByText('Unknown').first()).toBeVisible();
  });

  test('the install discloses the Secret-reading grant before the diff', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routerStatus: { ...FIXTURES.routerAbsent, enabled: true },
    });
    await openRoutes(page);

    await page.getByTestId('router-panel').getByRole('button', { name: /Install the router/ }).click();
    await expect(page.getByText('can read every Secret in the cluster')).toBeVisible();
  });

  test('making the router the cluster default requires typing the class name', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routerStatus: { ...FIXTURES.routerAbsent, enabled: true },
    });
    await openRoutes(page);

    await page.getByTestId('router-panel').getByRole('button', { name: /Install the router/ }).click();
    await page.getByTestId('router-default-class').check();
    await page.getByTestId('mutation-preview').click();

    // The typed confirmation is the same control a drain uses: it is what makes
    // "claim every unclassed Ingress in the cluster" a deliberate act.
    await expect(page.getByTestId('mutation-confirm')).toBeDisabled();
  });

  test('reinstall opens showing what is installed, not the bundle defaults', async ({
    page,
  }) => {
    // The failure this prevents: an operator opens Reinstall to take a version
    // bump, the form shows "make this the default class" and Gateway API
    // unchecked because that is what a fresh install defaults to, and
    // confirming turns both off. The write is honest — the diff carries it —
    // but a changed checkbox nobody touched is not what anyone reads an
    // eight-object diff for.
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routerStatus: FIXTURES.routerInstalledCustom,
    });
    await openRoutes(page);

    await page.getByTestId('router-panel').getByRole('button', { name: /Reinstall|Install/ }).click();

    await expect(page.getByTestId('router-namespace')).toHaveValue('edge-proxy');
    await expect(page.getByTestId('router-class')).toHaveValue('edge');
    await expect(page.getByTestId('router-replicas')).toHaveValue('3');
    await expect(page.getByTestId('router-service-type')).toHaveValue('NodePort');
    await expect(page.getByTestId('router-default-class')).toBeChecked();
    await expect(page.getByTestId('router-gateway-api')).toBeChecked();
  });

  test('a reinstall over an unreadable router does not silently switch things off', async ({
    page,
  }) => {
    // `gatewayApi: null` is "we could not look". Seeding the checkbox from it as
    // false would let a failed read turn the feature off on the next confirm,
    // which is the tri-state rule applied to a form that writes.
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routerStatus: {
        ...FIXTURES.routerInstalledCustom,
        deployment: { ...FIXTURES.routerInstalledCustom.deployment, gatewayApi: null },
      },
    });
    await openRoutes(page);

    await page.getByTestId('router-panel').getByRole('button', { name: /Reinstall|Install/ }).click();

    // Falls back to the form's own default rather than inventing `true`.
    await expect(page.getByTestId('router-gateway-api')).not.toBeChecked();
    // Everything the cluster could answer for is still seeded.
    await expect(page.getByTestId('router-class')).toHaveValue('edge');
  });

  test('a partial install is reported as partial, not as success', async ({ page }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routerStatus: { ...FIXTURES.routerAbsent, enabled: true },
      routerInstall: (body) => ({
        dryRun: body.dryRun !== false,
        // Deliberately: two objects failed, so this is false even on a real write.
        installed: false,
        failed: 2,
        version: '3.2.13',
        namespace: 'k8boss-router',
        ingressClassName: 'haproxy',
        objects: [
          { kind: 'Namespace', name: 'k8boss-router', namespace: null, verb: 'create', applied: body.dryRun === false, diff: null, auditId: 1, error: null },
          { kind: 'ClusterRole', name: 'k8boss-admin-router', namespace: null, verb: 'create', applied: false, diff: null, auditId: 2, error: { code: 'rbac_denied', message: 'clusterroles is forbidden', detail: null, hint: 'Grant create on rbac.authorization.k8s.io/clusterroles.' } },
        ],
        serves: FIXTURES.routerPlan.serves,
        options: FIXTURES.routerPlan.options,
      }),
    });
    await openRoutes(page);

    await page.getByTestId('router-panel').getByRole('button', { name: /Install the router/ }).click();
    await page.getByTestId('mutation-preview').click();
    await page.getByTestId('mutation-confirm').click();

    await expect(page.getByTestId('mutation-summary')).toContainText(
      '2 of 2 objects were not created',
    );
    await expect(page.getByTestId('mutation-summary')).toContainText('Nothing was rolled back');
    await expect(page.getByText('The router is installed')).toHaveCount(0);

    // And which one failed, with the grant it needed — the only actionable part.
    const report = page.getByTestId('router-object-report');
    await expect(report).toContainText('ClusterRole');
    await expect(report).toContainText('rbac_denied');
    await expect(report).toContainText('Grant create on rbac.authorization.k8s.io/clusterroles.');
  });
});

test.describe('the fixture router itself', () => {
  /**
   * `mockApi`'s fallback is `FIXTURES.emptyList` — a complete, non-partial,
   * empty envelope. So a forgotten or mis-ordered mock does not fail: it
   * renders as "this cluster has none of these", and every assertion about the
   * unknown-backend warning, the partial banner or the tri-state install state
   * passes for the wrong reason. That is this repo's flagship bug class,
   * reproduced inside its own test harness.
   *
   * Path ordering is what prevents it, and ordering is not something a comment
   * can enforce: `/routes/capabilities` and `/routes/render` both sit under
   * `/routes/…`, as does `/routes/{backend}/{ns}/{name}`, and `/router/plan`
   * sits under `/router`. These assert the router resolves each to the right
   * fixture rather than to a neighbour or to the fallback.
   */
  /** Fetch from inside the page, so `page.route` actually intercepts it. */
  const apiGet = (page, path) =>
    page.evaluate((p) => fetch(p).then((r) => r.json()), path);
  const apiPost = (page, path, body) =>
    page.evaluate(
      ([p, b]) =>
        fetch(p, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(b),
        }).then((r) => r.json()),
      [path, body],
    );

  test('each route endpoint is answered by its own fixture, not the empty fallback', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const capabilities = await apiGet(page, '/api/routes/capabilities');
    expect(capabilities.items.length).toBe(3);
    expect(capabilities.items.map((b) => b.backend).sort()).toEqual([
      'gateway',
      'ingress',
      'openshift',
    ]);

    const listing = await apiGet(page, '/api/routes');
    expect(listing.items.length).toBeGreaterThan(0);

    const detail = await apiGet(page, '/api/routes/ingress/prod/shop');
    expect(detail.route.name).toBe('shop');

    const rendered = await apiPost(page, '/api/routes/render', {
      backend: 'ingress',
      spec: { name: 'x', namespace: 'prod', targets: [] },
    });
    expect(rendered.yaml).toContain('kind: Ingress');
  });

  test('/router/plan is not answered by /router', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const plan = await apiPost(page, '/api/router/plan', {});
    expect(plan.objects.length).toBeGreaterThan(0);

    const status = await apiGet(page, '/api/router');
    // The status envelope, not the plan's, and not the empty listing.
    expect(status).toHaveProperty('shippedVersion');
    expect(status).not.toHaveProperty('items');
  });
});

test.describe('an exposure something else owns', () => {
  test('the table says which controller owns it', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openRoutes(page);

    const owned = page.getByRole('row', { name: /admin/ });
    await expect(owned.getByText('Shop storefront')).toBeVisible();

    // And an exposure nobody owns is quiet, or the warning stops meaning anything.
    const handMade = page.getByRole('row', { name: /shop/ }).first();
    await expect(handMade.getByText('nothing — created by hand')).toBeVisible();
  });

  test('editing one warns that the change will be reverted, before the diff', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      routeDetail: {
        route: FIXTURES.routes.items[1],
        manifest: {
          apiVersion: 'networking.k8s.io/v1',
          kind: 'Ingress',
          metadata: { name: 'admin', namespace: 'prod', resourceVersion: '4022' },
          spec: {},
        },
        backend: FIXTURES.routeCapabilities.items[1],
      },
    });
    await openRoutes(page);

    await page.getByRole('row', { name: /admin/ }).getByRole('button').click();
    await page.getByRole('menuitem', { name: 'Edit…' }).click();

    const warning = page.getByTestId('route-managed-by');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('will be applied and then reverted');
    // Before the diff, not after: the operator decides whether to bother at all.
    await expect(page.getByTestId('diff-view')).toHaveCount(0);
  });
});
