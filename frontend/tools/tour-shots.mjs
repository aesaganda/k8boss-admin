/**
 * Shoot the product tour's screenshots against the real console.
 *
 * Same trick as the Playwright suite: every `/api/**` call is intercepted, so
 * this needs no backend and no cluster. It runs against the production build
 * served by `vite preview`, for the same reason `playwright.config.js` gives —
 * the lazy page chunks only exist as separate files in a build, so a tour shot
 * of a page whose chunk fails to load would be a picture of the error boundary.
 *
 * The images are the tour's evidence. They have to be pictures of *this* code,
 * regenerated when it changes, rather than mockups that drift away from it —
 * which is the whole reason this is a script in the repo and not a folder of
 * hand-cropped PNGs somebody made once.
 *
 *   cd frontend
 *   npm run build && npm run preview &
 *   node tools/tour-shots.mjs
 *
 * Output lands in `tools/tour-shots/`.
 */
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

import * as F from './tour-fixtures.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, 'tour-shots');
const BASE = process.env.TOUR_BASE_URL || 'http://localhost:5174';
// PNG is the archival default; `TOUR_FORMAT=jpeg` produces the smaller
// images the tour page embeds as data URIs, where total page weight is capped.
const FORMAT = process.env.TOUR_FORMAT === 'jpeg' ? 'jpeg' : 'png';
const QUALITY = Number(process.env.TOUR_QUALITY || 92);
// 1.5 rather than 2: these are flat-colour UI shots, so the extra pixels cost
// page weight without buying legibility once the tour scales them to a column.
const SCALE = Number(process.env.TOUR_SCALE || 1.5);

const HEALTH = {
  status: 'ok',
  version: '0.1.0',
  mutations: 'enabled',
  clusters: { registered: 3, reachable: 2 },
  degraded: [],
};

const EMPTY = { items: [], continue: null, remaining: null, partial: false, unavailable: [] };

/**
 * Answer every `/api` call from the fixtures.
 *
 * `options.mutations` flips the deployment-wide write gate, which is what the
 * read-only shot needs; `options.auth` turns the sign-in gate on so the login
 * screen can be shot at all.
 */
async function mockApi(page, { mutations = 'enabled', auth = null, theme = 'light' } = {}) {
  let signedIn = Boolean(auth?.authenticated);
  const user = {
    id: 1, username: 'erens', display_name: 'Eren S.', email: 'erens@example.test',
    role: 'admin', auth_source: 'oidc',
  };

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname.replace(/^\/api/, '');
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const text = (body) =>
      route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body });

    /* ── §12 identity ─────────────────────────────────────────────────── */
    if (p === '/auth/config') {
      return json(
        auth
          ? {
              enabled: true, localEnabled: true, ldapEnabled: true, oidcEnabled: true,
              methods: ['local', 'ldap', 'oidc'],
              oidc: { label: 'Continue with Okta', startPath: '/api/auth/oidc/start' },
            }
          : { enabled: false, localEnabled: true, ldapEnabled: false, oidcEnabled: false, methods: ['local'], oidc: null },
      );
    }
    if (p === '/auth/me') {
      return signedIn || !auth
        ? json({ enabled: Boolean(auth), authenticated: true, user, csrfToken: 'csrf-tour', expiresAt: '2026-08-21T00:00:00Z' })
        : json({ error: 'authentication_required', message: 'Sign in to continue.', detail: null, hint: null, context: {} }, 401);
    }
    if (p === '/auth/login') {
      signedIn = true;
      return json({ enabled: true, authenticated: true, user, csrfToken: 'csrf-tour', expiresAt: '2026-08-21T00:00:00Z' });
    }
    if (p === '/auth/logout') { signedIn = false; return route.fulfill({ status: 204, body: '' }); }
    if (p === '/auth/users') return json(F.USERS);

    /* ── health, clusters ─────────────────────────────────────────────── */
    if (p === '/health') return json({ ...HEALTH, mutations });
    if (p === '/clusters') return json(F.CLUSTERS);
    if (/^\/clusters\/\d+\/overview$/.test(p)) return json(F.OVERVIEW);
    if (/^\/clusters\/\d+\/test$/.test(p)) return json(F.CLUSTER_TEST);

    /* ── §10 audit ────────────────────────────────────────────────────── */
    if (p === '/audit/verify') return json(F.AUDIT_VERIFY);
    if (p === '/audit') return json(F.AUDIT);

    /* ── §5 §7 typed reads ────────────────────────────────────────────── */
    if (p === '/namespaces') return json(F.NAMESPACES);
    if (p === '/nodes') return json(F.NODES);
    if (/^\/nodes\/[^/]+$/.test(p)) {
      const name = decodeURIComponent(p.split('/').at(-1));
      const found = F.NODES.items.find((n) => n.name === name) ?? F.NODES.items[0];
      return json({ ...found, unavailable: [], partial: false });
    }
    if (p === '/events') return json(F.EVENTS);

    /* ── §6 workloads, and the write funnel ───────────────────────────── */
    if (p === '/workloads') return json(F.WORKLOADS);
    if (p.endsWith('/scale')) {
      const body = route.request().postDataJSON?.() ?? {};
      return json(body?.dryRun === false ? F.SCALE_APPLIED : F.SCALE_DRY_RUN);
    }
    if (p.endsWith('/rollout')) {
      return json({
        kind: 'Deployment', supported: true,
        revisions: [
          { revision: 14, current: true, created: '2026-08-06T09:12:00Z', images: ['ghcr.io/acme/checkout:1.9.2'], change_cause: 'image bump to 1.9.2' },
          { revision: 13, current: false, created: '2026-07-28T14:02:00Z', images: ['ghcr.io/acme/checkout:1.9.1'], change_cause: 'image bump to 1.9.1' },
          { revision: 12, current: false, created: '2026-07-11T10:31:00Z', images: ['ghcr.io/acme/checkout:1.8.7'], change_cause: 'raise memory request' },
        ],
        unavailable: [], partial: false,
      });
    }
    if (/^\/workloads\/[^/]+\/[^/]+\/[^/]+$/.test(p)) return json(F.CHECKOUT_DETAIL);

    /* ── the drain plan (§ safety model) ──────────────────────────────── */
    if (p.endsWith('/drain')) {
      const body = route.request().postDataJSON?.() ?? {};
      const blocked = F.DRAIN_PLAN.filter((row) => row.action === 'blocked').length;
      // Blocked pods stop the drain before the node is cordoned, and the plan
      // comes back on the error so the operator can read what stopped it.
      if (blocked && !body.force) {
        return json({
          error: 'invalid',
          message: `${blocked} pod(s) on ip-10-0-2-11 cannot be drained without force.`,
          detail: F.DRAIN_PLAN.filter((r) => r.action === 'blocked').map((r) => `${r.namespace}/${r.pod}: ${r.reason}`).join('; '),
          hint: 'Resolve the listed pods, or set deleteEmptyDirData / ignoreDaemonSets as appropriate. `force: true` proceeds anyway, but it cannot override a PodDisruptionBudget — the API server enforces those.',
          context: { group: '', version: 'v1', resource: 'nodes', name: 'ip-10-0-2-11', verb: 'drain', blocked, plan: F.DRAIN_PLAN, pdb_checked: true },
        }, 422);
      }
      return json({
        dryRun: true, applied: false, verb: 'drain',
        target: { group: '', version: 'v1', resource: 'nodes', namespace: null, name: 'ip-10-0-2-11', subresource: null },
        diff: { before: 'spec:\n  unschedulable: false\n', after: 'spec:\n  unschedulable: true\n', unified: '--- live\n+++ projected (dryRun=All)\n@@ -1,2 +1,2 @@\n spec:\n-  unschedulable: false\n+  unschedulable: true\n', changed: true },
        resourceVersion: '884301', warnings: [], auditId: null,
        plan: F.DRAIN_PLAN, blocked, skipped: 2, evicted: 0, failed: 0,
        drained: false, pdb_checked: true, unavailable: [], partial: false,
      });
    }
    if (p.endsWith('/cordon')) {
      return json({
        dryRun: true, applied: false, verb: 'patch',
        target: { group: '', version: 'v1', resource: 'nodes', namespace: null, name: 'ip-10-0-2-11', subresource: null },
        diff: { before: 'spec:\n  unschedulable: false\n', after: 'spec:\n  unschedulable: true\n', unified: '--- live\n+++ projected (dryRun=All)\n@@ -1,2 +1,2 @@\n spec:\n-  unschedulable: false\n+  unschedulable: true\n', changed: true },
        resourceVersion: '884301', warnings: [], auditId: null,
      });
    }

    /* ── §4 generic resource access ───────────────────────────────────── */
    if (p === '/resources/catalog') return json(F.CATALOG);
    if (p.endsWith('/yaml')) {
      const name = decodeURIComponent(p.split('/').at(-2) ?? 'checkout');
      return text(F.objectYaml({ name }));
    }
    const listing = p.match(/^\/resources\/([^/]+)\/([^/]+)\/([^/]+)$/);
    if (listing) {
      const plural = listing[3];
      if (plural === 'pods') return json(F.PODS);
      if (plural === 'certificates') return json(F.CERTIFICATES);
      return json(EMPTY);
    }

    /* ── §9 preflight ─────────────────────────────────────────────────── */
    if (p === '/access/preflight') {
      if (route.request().method() === 'POST') {
        // §9: results come back in the same order as the checks, because the
        // caller pairs them by index. Returning a shorter list silently gates
        // off every button past its end — which is what an empty `results`
        // did on the first run of this script.
        const checks = route.request().postDataJSON?.()?.checks ?? [];
        return json({
          results: checks.map((c) => ({
            ...c, allowed: true, reason: '', evaluationError: null, hint: null,
          })),
        });
      }
      return json({ verb: 'list', group: 'apps', resource: 'deployments', namespace: null, allowed: true, reason: '', evaluationError: null, hint: null });
    }

    // Everything unmocked answers as a complete, empty listing — never as an
    // error, so a shot never accidentally photographs a partial banner that
    // this script caused rather than the fixture.
    return json(EMPTY);
  });

  await page.addInitScript(([t]) => {
    localStorage.setItem('k8boss-admin.activeClusterId', '1');
    localStorage.setItem('k8boss-admin.theme', t);
  }, [theme]);
}

/* ── shooting ───────────────────────────────────────────────────────────── */

const shots = [];
function shot(name, caption, run) { shots.push({ name, caption, run }); }

/** Wait for a page to have actually rendered, then let layout settle. */
async function ready(page, heading) {
  if (heading) {
    await page.getByRole('heading', { level: 1, name: heading, exact: true }).waitFor({ timeout: 20000 });
  }
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(600);
}

shot('01-overview', 'Overview', async (page) => {
  await page.goto(`${BASE}/`);
  await ready(page, 'prod-eu overview');
});

shot('02-nodes', 'Nodes', async (page) => {
  await page.goto(`${BASE}/nodes`);
  await ready(page, 'Nodes');
});

shot('03-workloads', 'Workloads', async (page) => {
  await page.goto(`${BASE}/workloads`);
  await ready(page, 'Workloads');
});

shot('04-pods', 'Pods', async (page) => {
  await page.goto(`${BASE}/pods`);
  await ready(page, 'Pods');
});

shot('05-scale-form', 'Scale — the form', async (page) => {
  await page.goto(`${BASE}/workloads/deployments/prod/checkout`);
  await ready(page, 'checkout');
  await page.getByRole('button', { name: 'Scale…' }).click();
  await page.getByRole('dialog').waitFor();
  await page.getByLabel('One more replica').click();
  await page.getByLabel('One more replica').click();
  await page.getByLabel('One more replica').click();
  await page.waitForTimeout(400);
});

shot('06-scale-diff', 'Scale — the dry-run diff', async (page) => {
  await page.goto(`${BASE}/workloads/deployments/prod/checkout`);
  await ready(page, 'checkout');
  await page.getByRole('button', { name: 'Scale…' }).click();
  await page.getByRole('dialog').waitFor();
  for (let i = 0; i < 3; i += 1) await page.getByLabel('One more replica').click();
  await page.getByRole('button', { name: /Preview/i }).click();
  await page.getByText('--- live').waitFor({ timeout: 15000 });
  await page.waitForTimeout(600);
});

shot('07-scale-applied', 'Scale — applied', async (page) => {
  await page.goto(`${BASE}/workloads/deployments/prod/checkout`);
  await ready(page, 'checkout');
  await page.getByRole('button', { name: 'Scale…' }).click();
  await page.getByRole('dialog').waitFor();
  for (let i = 0; i < 3; i += 1) await page.getByLabel('One more replica').click();
  await page.getByRole('button', { name: /Preview/i }).click();
  await page.getByText('--- live').waitFor({ timeout: 15000 });
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await page.waitForTimeout(1200);
});

shot('08-drain-plan', 'Drain — the plan', async (page) => {
  await page.goto(`${BASE}/nodes`);
  await ready(page, 'Nodes');
  const row = page.getByRole('row').filter({ hasText: 'ip-10-0-2-11' }).first();
  await row.getByRole('button').last().click();
  await page.getByRole('menuitem', { name: 'Drain…' }).click();
  await page.getByRole('dialog').waitFor();
  await page.getByRole('button', { name: /Preview/i }).click();
  await page.getByText('pg-primary-0').first().waitFor({ timeout: 15000 });
  await page.waitForTimeout(600);
});
shots.at(-1).options = { viewport: { width: 1680, height: 1560 } };

shot('09-explorer', 'API explorer', async (page) => {
  await page.goto(`${BASE}/explorer`);
  await ready(page, 'API explorer');
});

shot('10-audit', 'Audit log', async (page) => {
  await page.goto(`${BASE}/audit`);
  await ready(page, 'Audit');
  // §10.2's verdict is the point of the page, and it runs on request rather
  // than on page load — an unclicked banner photographs as "not checked".
  await page.getByRole('button', { name: 'Check integrity' }).click();
  await page.locator('[data-testid="audit-chain"]').waitFor({ timeout: 15000 });
  await page.waitForTimeout(600);
});
shots.at(-1).options = { viewport: { width: 1680, height: 1500 } };

shot('11-clusters', 'Clusters', async (page) => {
  await page.goto(`${BASE}/clusters`);
  await ready(page, 'Clusters');
  const test = page.getByRole('button', { name: /^Test/ }).first();
  if (await test.count()) { await test.click(); await page.waitForTimeout(1200); }
});

shot('12-events', 'Events', async (page) => {
  await page.goto(`${BASE}/events`);
  await ready(page, 'Events');
});

shot('13-yaml', 'YAML', async (page) => {
  await page.goto(`${BASE}/workloads/deployments/prod/checkout`);
  await ready(page, 'checkout');
  await page.getByRole('tab', { name: 'YAML' }).click();
  await page.getByText('apiVersion').first().waitFor({ timeout: 15000 });
  await page.waitForTimeout(800);
});

shot('14-namespaces', 'Namespaces', async (page) => {
  await page.goto(`${BASE}/namespaces`);
  await ready(page, 'Namespaces');
});

/* Shots that need a different console configuration. */

shot('15-overview-dark', 'Overview in dark mode', async (page) => {
  await page.goto(`${BASE}/`);
  await ready(page, 'prod-eu overview');
}, );
shots.at(-1).options = { theme: 'dark' };

shot('16-login', 'Sign in', async (page) => {
  await page.goto(`${BASE}/`);
  await page.getByRole('heading', { name: 'Sign in' }).waitFor({ timeout: 20000 });
  await page.waitForTimeout(600);
});
shots.at(-1).options = { auth: { authenticated: false } };

shot('17-read-only', 'Read-only mode', async (page) => {
  await page.goto(`${BASE}/workloads/deployments/prod/checkout`);
  await ready(page, 'checkout');
});
shots.at(-1).options = { mutations: 'disabled' };

/* ── main ───────────────────────────────────────────────────────────────── */

const only = process.argv.slice(2);

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH || undefined,
});

await mkdir(OUT, { recursive: true });

let failures = 0;
for (const s of shots) {
  if (only.length && !only.some((f) => s.name.includes(f))) continue;
  const context = await browser.newContext({
    viewport: s.options?.viewport ?? { width: 1680, height: 1000 },
    deviceScaleFactor: SCALE,
    locale: 'en-US',
    timezoneId: 'UTC',
    colorScheme: s.options?.theme === 'dark' ? 'dark' : 'light',
  });
  const page = await context.newPage();
  page.on('console', (m) => { if (m.type() === 'error') console.warn(`  [console] ${m.text().slice(0, 160)}`); });
  try {
    await mockApi(page, s.options ?? {});
    await s.run(page);
    await page.screenshot({
      path: path.join(OUT, `${s.name}.${FORMAT}`),
      ...(FORMAT === 'jpeg' ? { type: 'jpeg', quality: QUALITY } : {}),
    });
    console.log(`✓ ${s.name}`);
  } catch (error) {
    failures += 1;
    console.error(`✗ ${s.name}: ${String(error).split('\n')[0]}`);
    await page.screenshot({ path: path.join(OUT, `FAILED-${s.name}.png`) }).catch(() => {});
  } finally {
    await context.close();
  }
}

await browser.close();
console.log(failures ? `\n${failures} shot(s) failed.` : '\nAll shots captured.');
process.exit(failures ? 1 : 0);
