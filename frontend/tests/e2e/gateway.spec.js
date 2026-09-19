import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

/**
 * The Gateway page's "Address it answers on" column.
 *
 * What is under test is not that a hostname is displayed — it is **which
 * hostnames this console is willing to turn into a click**. A link an operator
 * cannot open is worse than no link: the first one that answers a browser with
 * a protocol error, or resolves nowhere because it was a wildcard pattern with
 * a label invented into it, is the one that teaches people to stop trying the
 * column at all.
 *
 * So: a concrete hostname is a link, a wildcard is text, and a GRPCRoute's
 * hostname is text whatever it looks like.
 */

/** §1.1's envelope, with nothing unavailable — the reads here all succeeded. */
const envelope = (items) => ({
  items,
  unavailable: [],
  partial: false,
  truncated: false,
  count: items.length,
});

const meta = (name) => ({ name, namespace: 'gw-demo', creationTimestamp: '2026-09-19T09:00:00Z' });

const GATEWAYS = envelope([
  {
    apiVersion: 'gateway.networking.k8s.io/v1',
    kind: 'Gateway',
    metadata: meta('demo-gateway'),
    spec: {
      gatewayClassName: 'eg',
      listeners: [
        // Wildcard hostname: the listener names no openable host, so the
        // Gateway's own published address is what the link has to use.
        { name: 'http', protocol: 'HTTP', port: 80, hostname: '*.127-0-0-1.sslip.io' },
        { name: 'https', protocol: 'HTTPS', port: 443, hostname: '*.127-0-0-1.sslip.io' },
        // Not HTTP at all: no URL, and never an `http://` one invented for it.
        { name: 'postgres', protocol: 'TCP', port: 5432 },
      ],
    },
    status: { addresses: [{ type: 'IPAddress', value: '192.168.165.240' }] },
  },
  {
    apiVersion: 'gateway.networking.k8s.io/v1',
    kind: 'Gateway',
    metadata: meta('unprogrammed-gateway'),
    spec: { gatewayClassName: 'eg', listeners: [{ name: 'http', protocol: 'HTTP', port: 80 }] },
    // No controller has given it an address yet.
    status: { addresses: [] },
  },
]);

const HTTPROUTES = envelope([
  {
    apiVersion: 'gateway.networking.k8s.io/v1',
    kind: 'HTTPRoute',
    metadata: meta('echo-route'),
    spec: {
      hostnames: ['echo.127-0-0-1.sslip.io'],
      rules: [{ matches: [{ path: { type: 'PathPrefix', value: '/api' } }] }],
    },
  },
  {
    apiVersion: 'gateway.networking.k8s.io/v1',
    kind: 'HTTPRoute',
    metadata: meta('wildcard-route'),
    spec: { hostnames: ['*.apps.example.com'] },
  },
  {
    apiVersion: 'gateway.networking.k8s.io/v1',
    kind: 'HTTPRoute',
    metadata: meta('inherits-route'),
    spec: { rules: [{ backendRefs: [{ name: 'echo', port: 80 }] }] },
  },
]);

const GRPCROUTES = envelope([
  {
    apiVersion: 'gateway.networking.k8s.io/v1',
    kind: 'GRPCRoute',
    metadata: meta('yages-route'),
    spec: { hostnames: ['grpc.127-0-0-1.sslip.io'] },
  },
]);

async function mockGateway(page) {
  await mockApi(page);
  for (const [plural, body] of [
    ['gateways', GATEWAYS],
    ['httproutes', HTTPROUTES],
    ['grpcroutes', GRPCROUTES],
  ]) {
    await page.route(`**/resources/gateway.networking.k8s.io/*/${plural}**`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }),
    );
  }
}

/** The one link on a row, or `null` when the row offers none. */
const linkIn = (page, name) => page.getByRole('row', { name }).getByRole('link');

test.describe('Gateway — the address column', () => {
  test('a Gateway links its HTTP and HTTPS listeners at its published address', async ({ page }) => {
    await mockGateway(page);
    await page.goto('/gateway');
    await expectPageRendered(page, 'Gateway (beta)');

    const links = linkIn(page, 'demo-gateway');
    await expect(links).toHaveCount(2);
    // Default ports are implied by the scheme, so neither carries `:80`/`:443`.
    await expect(links.nth(0)).toHaveAttribute('href', 'http://192.168.165.240');
    await expect(links.nth(1)).toHaveAttribute('href', 'https://192.168.165.240');
    // Its own tab is not optional: the target is a cluster workload nobody vetted.
    await expect(links.nth(0)).toHaveAttribute('rel', 'noopener noreferrer');

    // No address yet is not "None": the row says which question is unanswered.
    const pending = page.getByRole('row', { name: 'unprogrammed-gateway' });
    await expect(pending.getByRole('link')).toHaveCount(0);
    await expect(pending.getByText('No HTTP listener with an address')).toBeVisible();
  });

  test('an HTTPRoute links a concrete hostname at its first path, and never a wildcard', async ({ page }) => {
    await mockGateway(page);
    await page.goto('/gateway');
    await expectPageRendered(page, 'Gateway (beta)');
    await page.getByRole('tab', { name: 'HTTP Routes' }).click();

    await expect(linkIn(page, 'echo-route')).toHaveAttribute(
      'href',
      'http://echo.127-0-0-1.sslip.io/api',
    );

    // A pattern is shown, because it is what the object says, and is not a link:
    // substituting a label would invent a hostname nobody wrote.
    const wildcard = page.getByRole('row', { name: 'wildcard-route' });
    await expect(wildcard.getByText('*.apps.example.com')).toBeVisible();
    await expect(wildcard.getByRole('link')).toHaveCount(0);

    await expect(
      page.getByRole('row', { name: 'inherits-route' }).getByText("Inherits its listener's hostnames"),
    ).toBeVisible();
  });

  test('a GRPCRoute shows its hostname and offers no link', async ({ page }) => {
    await mockGateway(page);
    await page.goto('/gateway');
    await expectPageRendered(page, 'Gateway (beta)');
    await page.getByRole('tab', { name: 'GRPC Routes' }).click();

    const row = page.getByRole('row', { name: 'yages-route' });
    await expect(row.getByText('grpc.127-0-0-1.sslip.io')).toBeVisible();
    // gRPC is not a scheme a browser opens.
    await expect(row.getByRole('link')).toHaveCount(0);
  });
});
