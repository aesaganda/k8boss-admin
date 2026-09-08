import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * The audit page and the single sign-on entry point.
 *
 * The first test in this file exists because of a shipped defect rather than a
 * hypothesis: `/audit` used thirteen components it never imported and threw
 * `ReferenceError` on first render. ESLint's core `no-undef` does not see JSX
 * element names, `vite build` leaves an undefined global alone, and no spec had
 * ever visited the route — so a page that could not render passed every gate.
 * The lint rule now catches the cause; this catches the symptom, which is the
 * half that keeps working when someone disagrees with the rule.
 *
 * The rest is about the one thing an audit UI must not do: make a claim the
 * backend did not. `partial` is not a pass, a failed check is not a silent one,
 * and a record whose actor was never verified says so.
 */

test.describe('audit', () => {
  test('the page renders its table rather than the error boundary', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');

    await expectPageRendered(page, 'Audit');
    await expect(page.getByText('replicas 3 -> 5')).toBeVisible();
  });

  test('cluster writes and console records are both shown, and distinguishable', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    // A sign-in and a Deployment patch in one table. Splitting them across two
    // pages would mean an incident review has to remember there are two.
    // Asserted per row rather than by hunting for isolated words: "Console" and
    // "Cluster" are also navigation and column-header text, so a loose match
    // would pass on a page whose table rendered no rows at all.
    const rows = page.getByRole('grid', { name: 'Audit trail' }).getByRole('row');

    const signIn = rows.filter({ hasText: 'someone-guessing' });
    await expect(signIn).toHaveCount(1);
    await expect(signIn).toContainText('Console');
    await expect(signIn).toContainText('login');
    // No cluster attribution, and rendered as "none" rather than blank — a fact
    // about the record, not a value that failed to load.
    await expect(signIn).toContainText('none');

    const write = rows.filter({ hasText: 'replicas 3 -> 5' });
    await expect(write).toHaveCount(1);
    await expect(write).toContainText('Cluster');
    await expect(write).toContainText('prod-eu');
  });

  test('a refused sign-in does not link its actor to a Kubernetes resource', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    // Console records are filed under a synthetic API group so they share one
    // row shape with cluster writes. Handing that to ResourceLink would build a
    // link to /explorer/k8boss-admin.io/v1/sessions — a well-formed route to an
    // API group no cluster serves, so an operator clicking a sign-in row lands
    // on "this cluster does not serve that API resource".
    await expect(page.locator('a[href*="k8boss-admin.io"]')).toHaveCount(0);
    await expect(page.getByText('session · someone-guessing')).toBeVisible();
  });

  test('the default scope really is unscoped, not the active cluster', async ({ page }) => {
    // The critical bug this file exists to keep fixed. `buildUrl` appends the
    // active cluster to every request whose query lacks one, so the audit
    // page's "Everything" position produced `?cluster_id=1` — while a banner
    // stated it was showing every cluster and the console's own records, and the
    // table contained one cluster's writes with no sign-ins in it. Nothing in
    // the response, the page or the exported file disclosed the narrowing.
    const listUrls = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/audit?')) listUrls.push(request.url());
    });

    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    await expect.poll(() => listUrls.length).toBeGreaterThan(0);
    for (const url of listUrls) {
      expect(url, `the default scope must not be narrowed: ${url}`).not.toMatch(
        /[?&]cluster_id=/,
      );
    }

    // And the export has to agree with the table it was taken from — the file
    // outlives the page, and whoever reads it never saw the banner.
    const href = await page.getByTestId('audit-export-ndjson').getAttribute('href');
    expect(href).not.toMatch(/[?&]cluster_id=/);
  });

  test('a failed reload does not leave a cursor from the previous query', async ({ page }) => {
    // `continue` is an opaque "id < N" tied to the query that produced it. Kept
    // across a failed filter change, "Load more" would fetch the NEW filter's
    // records older than the OLD filter's last row — silently omitting every
    // match newer than that id, which on an audit page means omitting the most
    // recent records of exactly what the operator just filtered for.
    const seen = [];
    let failFiltered = false;

    await mockApi(page);
    await page.route('**/api/audit?**', async (route) => {
      const url = new URL(route.request().url());
      seen.push(url.search);
      if (url.searchParams.get('outcome') === 'denied' && failFiltered) {
        return route.fulfill({
          status: 502,
          contentType: 'application/json',
          body: JSON.stringify({
            error: 'upstream_error', message: 'the database is unavailable',
            detail: null, hint: null, context: {},
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...FIXTURES.audit, continue: '4471', remaining: 90 }),
      });
    });

    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');
    await expect(page.getByTestId('audit-more')).toBeVisible();

    failFiltered = true;
    await page.getByTestId('audit-outcome').selectOption('denied');
    await expect.poll(() => seen.some((q) => q.includes('outcome=denied'))).toBe(true);

    // No paging control over a failed load, so no way to page with a cursor
    // that belonged to a different question.
    await expect(page.getByTestId('audit-more')).toHaveCount(0);
  });

  test('the scope filter can select records that belong to no cluster', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    const requests = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/audit?')) requests.push(request.url());
    });

    await page.getByTestId('audit-cluster').selectOption('0');
    await expect.poll(() => requests.some((url) => /cluster_id=0(&|$)/.test(url))).toBe(true);

    // The zero has to survive the query builder, which drops empty values. If it
    // were dropped, the request would fall back to the active cluster and the
    // page would show an empty table under a filter the operator believes is
    // selecting sign-ins.
    await expect(page.getByTestId('audit-scope')).toContainText('no cluster');
  });
});

test.describe('audit integrity', () => {
  test('the check is offered rather than run on every page load', async ({ page }) => {
    let verifyCalls = 0;
    await mockApi(page);
    page.on('request', (request) => {
      if (request.url().includes('/audit/verify')) verifyCalls += 1;
    });

    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    // A full scan of a table that only grows must not be the cost of opening the
    // page.
    await expect(page.getByTestId('audit-verify')).toBeVisible();
    expect(verifyCalls).toBe(0);
  });

  test('a partial verdict is never rendered as a pass', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    await page.getByTestId('audit-verify').click();

    const panel = page.getByTestId('audit-chain');
    // The whole point. 4460 of 4472 records verified and 12 cannot be spoken
    // for; a green "verified" here would be the console asserting something it
    // has no basis for about the 12.
    await expect(panel).toContainText('as far as it can be checked');
    await expect(page.getByTestId('audit-chain-unchained')).toHaveText('12');
    await expect(panel).toContainText('not back-filled');
    await expect(panel).not.toContainText('Nothing has been edited');
  });

  test('a broken chain names the record it broke at', async ({ page }) => {
    await mockApi(page, { chain: FIXTURES.auditVerifyBroken });
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    await page.getByTestId('audit-verify').click();

    const panel = page.getByTestId('audit-chain');
    await expect(panel).toContainText('does not verify');
    await expect(page.getByTestId('audit-chain-break')).toContainText('#89');
    // "The trail is broken somewhere" is as unactionable as "it is fine".
    await expect(page.getByTestId('audit-chain-break')).toContainText('content was modified');
  });

  test('a failed check says so instead of showing a stale verdict', async ({ page }) => {
    await mockApi(page);
    await page.route('**/api/audit/verify*', (route) =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'upstream_error',
          message: 'The console database could not be read.',
          detail: null,
          hint: null,
          context: {},
        }),
      }),
    );

    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');
    await page.getByTestId('audit-verify').click();

    const panel = page.getByTestId('audit-chain');
    await expect(panel).toContainText('could not run');
    // The distinction the whole project turns on: a check that did not complete
    // is not evidence of anything, and must not read as either verdict.
    await expect(panel).toContainText('says nothing about whether the trail is intact');
  });
});

test.describe('audit export', () => {
  test('both formats are offered and carry the active filters', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    await page.getByTestId('audit-category').selectOption('console');

    // The file has to describe the same query as the table it was taken from.
    await expect
      .poll(() => page.getByTestId('audit-export-ndjson').getAttribute('href'))
      .toContain('category=console');
    await expect
      .poll(() => page.getByTestId('audit-export-csv').getAttribute('href'))
      .toContain('format=csv');
  });

  test('a non-admin sees the export disabled with the reason, not hidden', async ({ page }) => {
    // Contract rule 11.4. Hidden, an operator cannot tell the feature exists;
    // live, clicking it navigates away from the SPA to a raw 403 JSON page,
    // which reads as a broken console rather than as a permission they lack.
    await mockApi(page, {
      auth: {
        authenticated: true,
        user: {
          id: 9, username: 'viewer', display_name: 'Viewer', email: null,
          role: 'user', auth_source: 'local',
        },
      },
    });
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    const ndjson = page.getByTestId('audit-export-ndjson');
    await expect(ndjson).toBeVisible();
    await expect(ndjson).toHaveAttribute('aria-disabled', 'true');
    await expect(ndjson).not.toHaveAttribute('href', /./);
  });

  test('the export is a navigation, not a blob built in the tab', async ({ page }) => {
    await mockApi(page);
    await page.goto('/audit');
    await expectPageRendered(page, 'Audit');

    // An `href` on an anchor, so the browser streams it to disk. Reading an
    // unbounded attachment through fetch to build a blob URL would materialise
    // the whole trail in the tab to hand it straight back to the filesystem.
    const href = await page.getByTestId('audit-export-ndjson').getAttribute('href');
    expect(href).toContain('/api/audit/export');
    expect(href).toContain('format=ndjson');
  });
});

test.describe('single sign-on', () => {
  test('the button appears only when the deployment has SSO configured', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: false } });
    await page.goto('/');

    // A button that leads to an error reads as a broken console. No button reads
    // as "SSO is not set up here", which is true and actionable.
    await expect(page.getByTestId('login-sso-oidc')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
  });

  test('a configured deployment offers it with its own label', async ({ page }) => {
    await mockApi(page, {
      auth: { authenticated: false, ssoEnabled: true, ssoLabel: 'Acme SSO' },
    });
    await page.goto('/');

    await expect(page.getByTestId('login-sso-oidc')).toContainText('Acme SSO');
  });

  test('every configured provider gets its own button, with its own name on it', async ({ page }) => {
    // Four kinds of single sign-on can be configured at once, and a deployment
    // offering two of them has to be able to say which is which: "Single
    // sign-on" twice is a choice nobody can make.
    await mockApi(page, {
      auth: {
        authenticated: false,
        ssoProviders: [
          { name: 'oidc', label: 'Keycloak', startPath: '/api/auth/oidc/start' },
          { name: 'openshift', label: 'OpenShift', startPath: '/api/auth/openshift/start' },
          { name: 'saml', label: 'Corporate SAML', startPath: '/api/auth/saml/start' },
        ],
      },
    });
    await page.goto('/');

    await expect(page.getByTestId('login-sso-oidc')).toContainText('Keycloak');
    await expect(page.getByTestId('login-sso-openshift')).toContainText('OpenShift');
    await expect(page.getByTestId('login-sso-saml')).toContainText('Corporate SAML');
    // A provider that is not configured has no button at all.
    await expect(page.getByTestId('login-sso-oauth')).toHaveCount(0);
  });

  test('each button starts its own provider, not the first one', async ({ page }) => {
    await mockApi(page, {
      auth: {
        authenticated: false,
        ssoProviders: [
          { name: 'oidc', label: 'Keycloak', startPath: '/api/auth/oidc/start' },
          { name: 'openshift', label: 'OpenShift', startPath: '/api/auth/openshift/start' },
        ],
      },
    });
    await page.route('**/api/auth/*/start*', (route) =>
      route.fulfill({ status: 200, contentType: 'text/html', body: '<html><body>idp</body></html>' }),
    );

    await page.goto('/nodes');
    await page.getByTestId('login-sso-openshift').click();

    await expect(page).toHaveURL(/\/api\/auth\/openshift\/start\?next=%2Fnodes/);
  });

  test('starting SSO leaves the SPA for the provider and carries the return path', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: false, ssoEnabled: true } });
    // The handshake needs a real top-level navigation — the provider renders a
    // login form and the handshake cookie is SameSite=Lax, so an XHR would both
    // fail to display anything and fail to send the cookie back.
    await page.route('**/api/auth/oidc/start*', (route) =>
      route.fulfill({ status: 200, contentType: 'text/html', body: '<html><body>idp</body></html>' }),
    );

    await page.goto('/workloads');
    await page.getByTestId('login-sso-oidc').click();

    await expect(page).toHaveURL(/\/api\/auth\/oidc\/start\?next=%2Fworkloads/);
  });

  test('a failed sign-on is explained and then cleared from the URL', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: false, ssoEnabled: true } });

    await page.goto('/?auth_error=permission_denied&auth_reason=account_refused');

    await expect(page.getByTestId('login-sso-error')).toContainText('will not issue a session');
    // Left in the URL it survives a reload and a bookmark, so a later successful
    // sign-in followed by a refresh would report the old failure over a working
    // session.
    await expect(page).toHaveURL(/\/$/);
  });

  test('an unrecognised reason is shown verbatim rather than replaced', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: false, ssoEnabled: true } });

    await page.goto('/?auth_error=invalid&auth_reason=some_future_reason');

    // "Sign-in failed" tells an operator nothing they did not know. A reason the
    // UI did not anticipate is exactly the one worth putting on screen.
    await expect(page.getByTestId('login-sso-error')).toContainText('some_future_reason');
  });
});

test.describe('sign-in throttling', () => {
  test('being throttled reads differently from being rejected', async ({ page }) => {
    await mockApi(page, { auth: { authenticated: false } });
    // The trailing `*` matters: the API client appends `?cluster_id=` to every
    // request, so a glob without it never matches and the shared catch-all in
    // mockApi answers instead — with a successful sign-in, which is the opposite
    // of what this test is about.
    await page.route('**/api/auth/login*', (route) =>
      route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'too_many_attempts',
          message: 'Too many sign-in attempts for this account.',
          detail: null,
          hint: null,
          context: { retryAfterSeconds: 300 },
        }),
      }),
    );

    await page.goto('/');
    await page.locator('#login-username').fill('erens');
    await page.locator('#login-password').fill('a-password-that-is-long');
    await page.getByRole('button', { name: 'Sign in' }).click();

    const alert = page.getByTestId('login-error');
    // Telling somebody to check their password while the console is refusing to
    // look at it is how a lockout becomes a support ticket.
    await expect(alert).toContainText('Too many attempts');
    await expect(alert).toContainText('5 minute');
  });
});
