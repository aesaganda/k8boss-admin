/**
 * The topology view (rule 11.13).
 *
 * The page is a picture, and a picture is read as fact. Each assertion below is
 * about a way of drawing one that would be wrong:
 *
 *   grouped vs heaped     Two Deployments carrying the same `part-of` label are
 *                         one application and are drawn in one box. A workload
 *                         with no application label is not put in somebody
 *                         else's box to tidy the canvas.
 *
 *   reached vs bare       A node marked with the outward arrow has an exposure
 *                         that names a Service selecting its pods. A node
 *                         without one is a claim that nothing outside reaches
 *                         it, so it is only drawn bare when all three listings
 *                         answered.
 *
 *   none vs unknown       A refused Services listing marks every node unknown.
 *                         Drawing them unexposed instead would be the console
 *                         telling an operator, during an outage of its own read
 *                         path, that their front end is unreachable.
 *
 *   0/0 vs —              A controller that has not reported its replicas is an
 *                         em dash on the canvas, exactly as it is in the table.
 *                         `0/0` on a running workload reads as scaled to zero.
 *
 *   offered vs hidden     Every action is in the menu whether or not the
 *                         operator may use it, and the one they may not carries
 *                         the reason (rule 11.4).
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/** Two workloads of one application, one loose, and one nothing can attribute. */
const WORKLOADS = {
  items: [
    {
      kind: 'Deployment',
      name: 'shop-web',
      namespace: 'prod',
      replicas: { desired: 3, ready: 3, updated: 3, available: 3 },
      images: ['ghcr.io/acme/shop-web:2.0.0'],
      selector: { app: 'shop' },
      labels: { 'app.kubernetes.io/part-of': 'storefront', app: 'shop' },
      age_seconds: 8600,
      status: 'Healthy',
      status_reason: null,
      restarts_24h: 0,
      suspended: null,
      schedule: null,
      last_schedule: null,
    },
    {
      kind: 'Deployment',
      name: 'payments',
      namespace: 'prod',
      replicas: { desired: 2, ready: 2, updated: 2, available: 2 },
      images: ['ghcr.io/acme/payments:1.0.0'],
      selector: { app: 'payments' },
      labels: { 'app.kubernetes.io/part-of': 'storefront', app: 'payments' },
      age_seconds: 7200,
      status: 'Healthy',
      status_reason: null,
      restarts_24h: 0,
      suspended: null,
      schedule: null,
      last_schedule: null,
    },
    {
      // No application label: its own node, outside every box.
      kind: 'Deployment',
      name: 'checkout',
      namespace: 'prod',
      // The controller has not reported. `—`, never `0/0`.
      replicas: { desired: null, ready: null, updated: null, available: null },
      images: ['ghcr.io/acme/checkout:1.9.2'],
      selector: { app: 'checkout' },
      labels: {},
      age_seconds: 1209600,
      status: 'Unknown',
      status_reason: 'The controller has not observed the current generation.',
      restarts_24h: null,
      suspended: null,
      schedule: null,
      last_schedule: null,
    },
    {
      // A CronJob has no pod selector at all, so no Service can be attributed
      // to it from this listing — the `?` case that is not a failure.
      kind: 'CronJob',
      name: 'nightly-reindex',
      namespace: 'prod',
      replicas: { desired: null, ready: null, updated: null, available: null },
      images: ['ghcr.io/acme/reindex:3'],
      selector: {},
      labels: {},
      age_seconds: 400000,
      status: 'Healthy',
      status_reason: null,
      restarts_24h: null,
      suspended: false,
      schedule: '0 2 * * *',
      last_schedule: '2026-08-18T02:00:00Z',
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

const SERVICES = {
  items: [
    {
      name: 'shop',
      namespace: 'prod',
      type: 'ClusterIP',
      clusterIP: '10.96.0.20',
      externalIPs: [],
      ports: [{ name: 'http', port: 80, targetPort: 'http', protocol: 'TCP', nodePort: null }],
      selector: { app: 'shop' },
      age_seconds: 8600,
      endpoint_count: 3,
    },
    {
      name: 'payments',
      namespace: 'prod',
      type: 'ClusterIP',
      clusterIP: '10.96.0.12',
      externalIPs: [],
      ports: [{ name: 'http', port: 80, targetPort: 'http', protocol: 'TCP', nodePort: null }],
      selector: { app: 'payments' },
      age_seconds: 7200,
      endpoint_count: 2,
    },
    {
      // Selector-less: backed by hand-managed EndpointSlices, so it selects
      // nobody's pods and must not be drawn against a workload.
      name: 'legacy-db',
      namespace: 'prod',
      type: 'ClusterIP',
      clusterIP: '10.96.0.30',
      externalIPs: [],
      ports: [{ name: 'pg', port: 5432, targetPort: 5432, protocol: 'TCP', nodePort: null }],
      selector: {},
      age_seconds: 90000,
      endpoint_count: 1,
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/** One Ingress, and it reaches `shop` — not `payments`. */
const ROUTES = {
  items: [
    {
      id: 'ingress/prod/shop',
      backend: 'ingress',
      kind: 'Ingress',
      group: 'networking.k8s.io',
      version: 'v1',
      plural: 'ingresses',
      name: 'shop',
      namespace: 'prod',
      hosts: ['shop.example.com'],
      subdomain: null,
      path: '/',
      pathType: 'Prefix',
      paths: [{ path: '/', pathType: 'Prefix', service: 'shop', port: 80, weight: null }],
      targets: [{ service: 'shop', port: 80, weight: null }],
      tls: { termination: 'edge', insecurePolicy: null, inlineCertificate: false, secretName: 'shop-tls' },
      wildcardPolicy: null,
      admitted: null,
      admittedDetail: null,
      addresses: ['a1b2.elb.eu-west-1.amazonaws.com'],
      ingressClass: 'haproxy',
      tlsHosts: ['shop.example.com'],
      parents: [],
      age_seconds: 86400,
      resourceVersion: '4021',
      managedBy: { controller: null, tool: null, marker: null, detail: null },
    },
  ],
  continue: null,
  remaining: null,
  partial: false,
  unavailable: [],
};

/** Allow everything except what `denied` names, so rule 11.4 has something to say. */
function preflightAnswering(denied = []) {
  return (checks) =>
    checks.map((check) => {
      const refused = denied.includes(check.verb) && !check.subresource;
      return {
        verb: check.verb,
        group: check.group,
        resource: check.resource,
        namespace: check.namespace,
        allowed: !refused,
        reason: refused ? 'RBAC: denied' : '',
        evaluationError: null,
        hint: refused ? `Grant ${check.verb} on ${check.resource} in ${check.namespace ?? 'the cluster'}.` : null,
        subject: 'system:serviceaccount:k8boss:admin',
      };
    });
}

async function openTopology(page, options = {}) {
  await mockApi(page, {
    workloads: WORKLOADS,
    services: SERVICES,
    routes: ROUTES,
    preflight: preflightAnswering(),
    ...options,
  });
  await page.goto('/topology');
  await expectPageRendered(page, 'Topology');
}

/** The canvas, once it has something to draw. */
async function canvas(page) {
  await expect(page.getByTestId('topology-canvas')).toBeVisible();
}

const node = (page, id) => page.locator(`[data-testid="topology-node"][data-node="${id}"]`);

test.describe('what the canvas draws', () => {
  test('workloads sharing an application label are one box, and the rest stand alone', async ({ page }) => {
    await openTopology(page);
    await canvas(page);

    await expect(page.getByTestId('topology-node')).toHaveCount(4);
    const groups = page.getByTestId('topology-group');
    await expect(groups).toHaveCount(1);
    await expect(groups).toHaveText('storefront');

    // `checkout` carries no application label, so it is not swept into the one
    // box on the canvas to make the picture tidier. Asserted on the group the
    // node says it is in: "the node exists" passes either way.
    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute(
      'data-group',
      'app:prod/app.kubernetes.io/part-of=storefront',
    );
    await expect(node(page, 'prod/Deployment/payments')).toHaveAttribute(
      'data-group',
      'app:prod/app.kubernetes.io/part-of=storefront',
    );
    await expect(node(page, 'prod/Deployment/checkout')).toHaveAttribute(
      'data-group',
      'loose:prod/Deployment/checkout',
    );
  });

  test('a replica count the controller never reported is an em dash, not 0/0', async ({ page }) => {
    await openTopology(page);

    // The count element itself: the node's own <title> carries an em dash in
    // "Deployment prod/checkout — Unknown", so `toContainText('—')` on the
    // group passes whatever the count says.
    const count = (id) => node(page, id).getByTestId('topology-node-count');
    await expect(count('prod/Deployment/shop-web')).toHaveText('3/3');
    await expect(count('prod/Deployment/checkout')).toHaveText('—');
  });
});

test.describe('what reaches a workload', () => {
  test('the node an Ingress reaches is marked with its address', async ({ page }) => {
    await openTopology(page);

    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute('data-exposure', 'route');
    // The link is a sibling of the node's button, not a child of it: a
    // focusable <a> inside role="button" is a nested interactive control, and
    // the node's own Enter handler used to cancel its navigation.
    await expect(
      page.getByTestId('topology-canvas').getByRole('link', { name: 'Open https://shop.example.com/' }),
    ).toBeVisible();
  });

  test('a workload with a Service but no exposure is not marked as reachable', async ({ page }) => {
    await openTopology(page);

    // `payments` has a Service and nothing routes to it. That is a fact all
    // three listings answered, so it is drawn plainly.
    await expect(node(page, 'prod/Deployment/payments')).toHaveAttribute('data-exposure', 'service');
  });

  test('a Service with no selector is not attached to anything', async ({ page }) => {
    await openTopology(page, {
      // `legacy-db` has no selector — it is backed by hand-managed
      // EndpointSlices — so `checkout`, whose labels it would otherwise match
      // vacuously, is fronted by nothing.
      services: { ...SERVICES, items: SERVICES.items.filter((s) => s.name === 'legacy-db') },
    });

    await expect(node(page, 'prod/Deployment/checkout')).toHaveAttribute('data-exposure', 'none');
    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute('data-exposure', 'none');
  });

  test('a Service selecting on a label the row does not carry is not ruled out', async ({ page }) => {
    await openTopology(page, {
      services: {
        ...SERVICES,
        items: [
          {
            ...SERVICES.items[0],
            name: 'shop-edge',
            // `tier` is not in the workload row's matchLabels, and the row is
            // matchLabels only — the pod template may well carry it. Neither
            // attached nor ruled out, so the node cannot be drawn bare.
            selector: { app: 'shop', tier: 'frontend' },
          },
        ],
      },
    });

    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute('data-exposure', 'unknown');
    // And a Service that *disagrees* on a label the row does carry really is
    // ruled out, so the rule does not swallow the namespace.
    await expect(node(page, 'prod/Deployment/payments')).toHaveAttribute('data-exposure', 'none');
  });

  test('a workload whose pods cannot be attributed says unknown rather than nothing', async ({ page }) => {
    await openTopology(page);

    // A CronJob has no selector, so no Service can be matched to it from the
    // listing — which is not the same as no Service selecting it.
    await expect(node(page, 'prod/CronJob/nightly-reindex')).toHaveAttribute('data-exposure', 'unknown');
  });

  test('a cross-namespace backendRef must not mark the local workload', async ({ page }) => {
    // The HTTPRoute lives in `prod` and its backendRef names `payments` in
    // `billing` — a different Service than `prod`'s own `payments`, reachable
    // only via a ReferenceGrant. `prod/Deployment/payments` must stay at
    // `service`, never promoted to `route` by a same-named Service elsewhere.
    await openTopology(page, {
      routes: {
        ...ROUTES,
        items: [
          ...ROUTES.items,
          {
            id: 'gateway/prod/payments-route',
            backend: 'gateway',
            kind: 'HTTPRoute',
            group: 'gateway.networking.k8s.io',
            version: 'v1',
            plural: 'httproutes',
            name: 'payments-route',
            namespace: 'prod',
            hosts: ['payments.example.com'],
            subdomain: null,
            path: '/',
            pathType: 'PathPrefix',
            paths: [{ path: '/', pathType: 'PathPrefix', service: 'payments', port: 80, weight: null }],
            targets: [{ service: 'payments', port: 80, weight: null, namespace: 'billing', kind: 'Service' }],
            tls: { termination: null, insecurePolicy: null, inlineCertificate: false, secretName: null },
            wildcardPolicy: null,
            admitted: true,
            admittedDetail: 'Accepted by public.',
            addresses: [],
            ingressClass: null,
            tlsHosts: [],
            parents: ['public'],
            age_seconds: 3600,
            resourceVersion: '99',
            managedBy: { controller: null, tool: null, marker: null, detail: null },
          },
        ],
      },
    });

    await expect(node(page, 'prod/Deployment/payments')).toHaveAttribute('data-exposure', 'service');
  });

  test('a refused Services listing makes every node unknown and says so', async ({ page }) => {
    await openTopology(page, {
      services: {
        items: [],
        continue: null,
        remaining: null,
        partial: true,
        unavailable: [
          {
            group: '',
            resource: 'services',
            namespace: 'prod',
            reason: 'forbidden',
            detail: 'services is forbidden',
          },
        ],
      },
    });

    await expect(page.getByTestId('partial-banner')).toContainText('services');
    for (const id of [
      'prod/Deployment/shop-web',
      'prod/Deployment/payments',
      'prod/Deployment/checkout',
      'prod/CronJob/nightly-reindex',
    ]) {
      await expect(node(page, id)).toHaveAttribute('data-exposure', 'unknown');
    }
  });

  test('a node drawn before the Services listing answers is unknown, not unexposed', async ({ page }) => {
    await mockApi(page, { workloads: WORKLOADS, routes: ROUTES, preflight: preflightAnswering() });

    // The workload listing returns first on any real cluster. Held here on
    // purpose: for as long as the Services read is in flight the canvas knows
    // nothing about what reaches these pods, and a bare node in that window is
    // the console saying "nothing does" and then taking it back.
    let answer;
    const held = new Promise((resolve) => {
      answer = resolve;
    });
    await page.route('**/api/resources/core/v1/services*', async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(SERVICES),
      });
    });

    await page.goto('/topology');
    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute('data-exposure', 'unknown');

    answer();
    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute('data-exposure', 'route');
  });

  test('a cluster that serves no Route CRD is an ordinary absence, not a hole', async ({ page }) => {
    await openTopology(page, {
      routes: {
        items: [],
        continue: null,
        remaining: null,
        partial: true,
        unavailable: [
          {
            group: 'route.openshift.io',
            resource: 'routes',
            namespace: 'prod',
            reason: 'unsupported',
            detail: 'The cluster does not serve route.openshift.io/v1 routes.',
          },
        ],
      },
    });

    // `unsupported` is a fact about the cluster, so the nodes keep answering.
    await expect(node(page, 'prod/Deployment/shop-web')).toHaveAttribute('data-exposure', 'service');
    await expect(node(page, 'prod/CronJob/nightly-reindex')).toHaveAttribute('data-exposure', 'unknown');
  });
});

test.describe('the side panel', () => {
  test('selecting a node opens the panel and puts the selection in the URL', async ({ page }) => {
    await openTopology(page);

    await node(page, 'prod/Deployment/checkout').click();
    await expect(page.getByTestId('topology-panel-title')).toHaveText('checkout');
    expect(page.url()).toContain('selected=prod%2FDeployment%2Fcheckout');

    // The link is the point: reopening it lands on the same panel.
    await page.reload();
    await expect(page.getByTestId('topology-panel-title')).toHaveText('checkout');
  });

  test('the panel answers with the workload’s own read, not the canvas’s guess', async ({ page }) => {
    await openTopology(page);

    await node(page, 'prod/Deployment/checkout').click();
    // §6's detail reads the pod template, which is the authority on what a
    // Service selects; the fixture's detail names `checkout`.
    await expect(page.getByTestId('topology-services')).toContainText('checkout');
  });

  test('the panel says what it could not read rather than showing no Services', async ({ page }) => {
    await openTopology(page, {
      workloadDetail: {
        ...FIXTURES.workloadDetail,
        // The §6 detail's own shape for a refused Service listing: `[]` in the
        // payload, and the reason in `unavailable[]`.
        services: [],
        partial: true,
        unavailable: [
          { group: '', resource: 'services', namespace: 'prod', reason: 'forbidden', detail: 'forbidden' },
        ],
      },
    });

    // `checkout`, because the detail fixture is that workload's — a panel that
    // rendered another object's payload under this name is the bug the
    // identity check in `ExposurePanel` exists for.
    await node(page, 'prod/Deployment/checkout').click();
    await expect(page.getByTestId('topology-services-unknown')).toBeVisible();
    // And with the Services unknown, which exposures reach it is unknown too —
    // not none, even though the canvas proved one from the listing.
    await expect(page.getByTestId('topology-routes-unknown')).toBeVisible();
    await expect(page.getByTestId('topology-routes')).toHaveCount(0);
  });

  test('the Actions menu offers every write, and names why one is unavailable', async ({ page }) => {
    await openTopology(page, { preflight: preflightAnswering(['delete']) });

    await node(page, 'prod/Deployment/shop-web').click();
    await page.getByTestId('topology-actions').click();

    await expect(page.getByTestId('topology-action-scale')).toBeVisible();
    // Rule 11.4: present, disabled, and carrying the reason — never hidden.
    // PatternFly puts the testid on the <li> and `aria-disabled` on the button
    // inside it, so the assertion has to reach for the control itself.
    const del = page.getByTestId('topology-action-delete');
    await expect(del).toBeVisible();
    await expect(del.getByRole('menuitem')).toHaveAttribute('aria-disabled', 'true');
    await expect(del).toContainText('Grant delete on deployments');
  });

  test('Scale opens the same dry-run dialog the workload page uses', async ({ page }) => {
    await openTopology(page);

    await node(page, 'prod/Deployment/shop-web').click();
    await page.getByTestId('topology-actions').click();
    await page.getByTestId('topology-action-scale').click();

    await expect(page.getByRole('dialog')).toContainText('Scale shop-web');
    // The write funnel's own control: nothing is applied before a diff.
    await expect(page.getByRole('button', { name: /Preview/ })).toBeVisible();
  });

  test('the replica stepper opens that same dialog already holding the next count', async ({ page }) => {
    await openTopology(page);

    await node(page, 'prod/Deployment/shop-web').click();
    const stepper = page.getByTestId('topology-scale-stepper');
    await stepper.getByRole('button', { name: 'Scale shop-web up to 4' }).click();

    // An arrow is arithmetic, not a write: it lands in the §6 dialog with the
    // number filled in, and the diff is still what authorises the change.
    await expect(page.getByRole('dialog')).toContainText('Scale shop-web');
    await expect(page.getByRole('spinbutton', { name: 'Desired replicas' })).toHaveValue('4');
    await expect(page.getByRole('button', { name: /Preview/ })).toBeVisible();
  });

  test('the stepper is disabled for a kind with no scale subresource, and for an unread count', async ({
    page,
  }) => {
    await openTopology(page);

    // Rule 11.4: offered and disabled, never hidden. A CronJob has no replicas
    // of its own — a fact about the kind, true of every operator.
    await node(page, 'prod/CronJob/nightly-reindex').click();
    const cronjob = page.getByTestId('topology-scale-stepper').getByTestId('action-button');
    await expect(cronjob).toHaveCount(2);
    await expect(cronjob.first()).toHaveAttribute('data-allowed', 'false');

    // Rule 11.2 as an action: `replicas.desired` was not reported, and `null + 1`
    // is a guess dressed as an increment.
    await node(page, 'prod/Deployment/checkout').click();
    const unread = page.getByTestId('topology-scale-stepper').getByTestId('action-button');
    await expect(unread.first()).toHaveAttribute('data-allowed', 'false');
    await expect(unread.last()).toHaveAttribute('data-allowed', 'false');
  });
});

test.describe('zoom and pan', () => {
  /** The canvas's own `viewBox`, parsed — the one place the current view lives. */
  async function readViewBox(page) {
    const raw = await page.getByTestId('topology-canvas').getAttribute('viewBox');
    const [x, y, w, h] = raw.split(' ').map(Number);
    return { x, y, w, h };
  }

  test('zoom in shrinks the visible rectangle; reset restores it exactly', async ({ page }) => {
    await openTopology(page);
    await canvas(page);

    const initial = await readViewBox(page);
    // Reset starts disabled: there is nothing yet to reset from.
    await expect(page.getByTestId('topology-reset-view')).toBeDisabled();

    await page.getByTestId('topology-zoom-in').click();
    const zoomed = await readViewBox(page);
    expect(zoomed.w).toBeLessThan(initial.w);
    expect(zoomed.h).toBeLessThan(initial.h);

    await expect(page.getByTestId('topology-reset-view')).toBeEnabled();
    await page.getByTestId('topology-reset-view').click();
    const reset = await readViewBox(page);
    expect(reset).toEqual(initial);
    await expect(page.getByTestId('topology-reset-view')).toBeDisabled();
  });

  test('the first zoom-in aims at the drawn content, not at the middle of the fixed-width canvas', async ({
    page,
  }) => {
    // This fixture's content — one boxed application plus two loose nodes —
    // sits on the left of the 960-wide canvas `layoutTopology` always returns
    // (its own docstring says why the width is fixed rather than measured).
    // Zooming in "on the view's own centre" from a fresh page would zoom
    // toward x=480 — past the right edge of everything actually drawn here —
    // magnifying blank canvas instead of any workload.
    await openTopology(page);
    await canvas(page);

    await page.getByTestId('topology-zoom-in').click();

    // The node the fixture places furthest right (checkout, a loose node
    // after the boxed pair) must still be inside the shrunk rectangle.
    const box = await readViewBox(page);
    const checkoutBox = await node(page, 'prod/Deployment/checkout').boundingBox();
    const canvasBox = await page.getByTestId('topology-canvas').boundingBox();
    // The node's on-screen box must be within the canvas's own rendered box —
    // if the zoom had aimed at the canvas's nominal centre instead of the
    // content, this node would have been clipped out of the visible area.
    expect(checkoutBox.x).toBeGreaterThanOrEqual(canvasBox.x - 1);
    expect(checkoutBox.x + checkoutBox.width).toBeLessThanOrEqual(canvasBox.x + canvasBox.width + 1);
    // And the viewBox's own centre should have moved left, off the canvas's
    // nominal midpoint (960/2 = 480) and toward the content.
    expect(box.x + box.w / 2).toBeLessThan(480);
  });

  test('once the operator has zoomed, a further zoom-in keeps their own view centred, not the content', async ({
    page,
  }) => {
    // A manual pan away from the content must not be pulled back toward it by
    // a later zoom click — only the very first zoom, from a fresh page,
    // anchors on the content.
    await openTopology(page);
    await canvas(page);
    await page.getByTestId('topology-zoom-in').click(); // establishes a manual view

    const before = await readViewBox(page);
    await page.getByTestId('topology-zoom-in').click();
    const after = await readViewBox(page);

    expect(after.x + after.w / 2).toBeCloseTo(before.x + before.w / 2, 5);
    expect(after.y + after.h / 2).toBeCloseTo(before.y + before.h / 2, 5);
  });

  test('zoom out is disabled at the fitted view — there is nothing wider to show', async ({ page }) => {
    await openTopology(page);
    await canvas(page);

    // The floor is the whole drawing, and the whole drawing is what a fresh
    // page already shows: zooming out further would only add empty margin.
    await expect(page.getByTestId('topology-zoom-out')).toBeDisabled();

    await page.getByTestId('topology-zoom-in').click();
    await expect(page.getByTestId('topology-zoom-out')).toBeEnabled();
    await page.getByTestId('topology-zoom-out').click();
    await expect(page.getByTestId('topology-zoom-out')).toBeDisabled();
  });

  test('the wheel zooms toward the cursor, not toward the centre of the drawing', async ({ page }) => {
    await openTopology(page);
    await canvas(page);

    const initial = await readViewBox(page);
    const box = await page.getByTestId('topology-canvas').boundingBox();
    // A point away from centre, so a centred zoom and a cursor-anchored one
    // would disagree about where the rectangle ends up.
    const cursor = { x: box.x + box.width * 0.15, y: box.y + box.height * 0.2 };
    await page.mouse.move(cursor.x, cursor.y);
    await page.mouse.wheel(0, -200);

    await expect
      .poll(async () => (await readViewBox(page)).w)
      .toBeLessThan(initial.w);
    const zoomed = await readViewBox(page);
    // A zoom centred on the drawing's own middle would have kept this ratio at
    // 0.5; a cursor-anchored one moves it toward where the cursor pointed.
    const initialCentreRatio = 0.5;
    const zoomedCentreRatio = (initial.x + initial.w / 2 - zoomed.x) / zoomed.w;
    expect(zoomedCentreRatio).not.toBeCloseTo(initialCentreRatio, 1);
  });

  test('a drag that starts and ends on the same node pans, and does not select it', async ({ page }) => {
    await openTopology(page);
    await canvas(page);
    await page.getByTestId('topology-zoom-in').click();
    const before = await readViewBox(page);

    const target = node(page, 'prod/Deployment/shop-web');
    const box = await target.boundingBox();
    const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };

    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    // Past the four-pixel drag threshold, but a small enough move that the
    // release still lands on the *same* node it started from — the case that
    // actually exercises suppression: mousedown and mouseup share a target, so
    // an unsuppressed click would fire on that node's own handler rather than
    // failing to resolve to anything clickable at all. Several steps, so the
    // browser issues more than one pointermove — a single large jump would
    // still pass an implementation that only checked the endpoints.
    await page.mouse.move(start.x + 15, start.y + 10, { steps: 6 });
    await page.mouse.up();

    const after = await readViewBox(page);
    expect(after.w).toBeCloseTo(before.w, 5);
    expect(after.h).toBeCloseTo(before.h, 5);
    expect(after.x).not.toBeCloseTo(before.x, 3);

    // The gesture that just panned the canvas must not also have opened the
    // drawer on the node it started from.
    await expect(page.getByTestId('topology-panel-title')).toHaveCount(0);
  });

  test('a plain click still selects a node after the view has been panned and zoomed', async ({ page }) => {
    await openTopology(page);
    await canvas(page);
    await page.getByTestId('topology-zoom-in').click();

    const target = node(page, 'prod/Deployment/shop-web');
    const box = await target.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.up();

    await expect(page.getByTestId('topology-panel-title')).toHaveText('shop-web');
  });

  test('switching namespace resets a manual view back to fit', async ({ page }) => {
    await openTopology(page);
    await canvas(page);
    const initial = await readViewBox(page);

    await page.getByTestId('topology-zoom-in').click();
    expect(await readViewBox(page)).not.toEqual(initial);

    // A different scope is a different drawing; a manual view aimed at the
    // old one means nothing once its nodes are gone.
    await page.getByTestId('namespace-selector').click();
    await page.getByRole('menuitem', { name: 'kube-system', exact: true }).click();
    await canvas(page);

    await expect.poll(() => readViewBox(page)).toEqual(initial);
    await expect(page.getByTestId('topology-reset-view')).toBeDisabled();
  });
});

test.describe('nothing to draw', () => {
  test('an empty namespace says the listing succeeded', async ({ page }) => {
    await openTopology(page, { workloads: FIXTURES.emptyList });

    await expect(page.getByText('The workload listing succeeded and matched nothing.')).toBeVisible();
  });

  test('an empty canvas over a failed read says it is not the whole picture', async ({ page }) => {
    await openTopology(page, {
      workloads: {
        items: [],
        continue: null,
        remaining: null,
        partial: true,
        unavailable: [
          {
            group: 'apps',
            resource: 'deployments',
            namespace: 'prod',
            reason: 'forbidden',
            detail: 'deployments is forbidden',
          },
        ],
      },
    });

    await expect(page.getByText('this canvas is not the whole picture')).toBeVisible();
    await expect(page.getByTestId('partial-banner')).toContainText('deployments');
  });
});
