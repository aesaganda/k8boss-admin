/**
 * The certificate behind an exposure (§32).
 *
 * The Routes table says `edge` and names a Secret. Every assertion below is
 * about a way of going one step further and still being wrong:
 *
 *   valid vs correct    A certificate can be freshly issued, correctly signed
 *                       and for a hostname this exposure no longer serves.
 *                       That row is green on expiry and red on the host, and
 *                       both have to be visible at once.
 *
 *   unknown vs absent   A Secret that could not be read leaves the row with no
 *                       certificate and a reason. Drawing it as an exposure
 *                       with none sends somebody to create a Secret that
 *                       already exists.
 *
 *   false vs null       `covered: false` is a claim about a certificate. With
 *                       none read there is nothing to make it with, so the
 *                       chip is grey rather than red.
 *
 *   fault vs elsewhere  A passthrough exposure keeps its certificate in the
 *                       pod. That is not a missing certificate and is not
 *                       drawn as one.
 *
 *   read vs verified    Nothing here is a handshake. The caveat saying so is
 *                       above the table, not under it, because a green column
 *                       read as "this works" is a claim the console cannot
 *                       make.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, mockApi } from './fixtures.js';

async function openCertificates(page, options = {}) {
  await mockApi(page, options);
  await page.goto('/routes');
  await expect(page.getByTestId('certificate-panel')).toBeVisible();
}

/**
 * A row in *this* table. Scoped on purpose: the §13 exposures table is on the
 * same page and renders the word `edge` in its TLS column, so an unscoped row
 * lookup finds an exposure when it means a certificate.
 */
function certificateRow(page, name) {
  return page.getByTestId('certificate-panel').getByRole('row', { name });
}

test.describe('what the panel says before any row', () => {
  test('the caveat about what is not checked is above the table', async ({ page }) => {
    await openCertificates(page);

    const caveat = page.getByTestId('certificate-caveat');
    await expect(caveat).toBeVisible();
    // The title, not only the body: "Certificates" over this paragraph reads as
    // a section heading, and the paragraph then reads as detail rather than as
    // the limit on everything below it.
    await expect(caveat).toContainText('what it cannot tell you');
    await expect(caveat).toContainText('No chain is verified');
    await expect(caveat).toContainText('Only a handshake settles that');
  });

  test('the summary counts what is wrong without reading every row', async ({ page }) => {
    await openCertificates(page);

    const summary = page.getByTestId('certificate-summary');
    await expect(summary).toContainText('1 expired or not yet valid');
    await expect(summary).toContainText('1 expiring within 30 days');
    await expect(summary).toContainText('1 not naming a host they serve');
    await expect(summary).toContainText('1 unreadable');
  });
});

test.describe('a state this build has not heard of', () => {
  test('an unrecognised state is grey and never reads as valid', async ({ page }) => {
    // The fallback matters more than the mapped states: a state added to the
    // backend and not yet to this table must arrive looking uncertain. Falling
    // through to green is how a new failure reason lands on screen reassuring.
    await mockApi(page, {
      routeCertificates: {
        ...FIXTURES.routeCertificates,
        items: [{ ...FIXTURES.routeCertificates.items[0], state: 'revoked' }],
      },
    });
    await page.goto('/routes');
    await expect(page.getByTestId('certificate-panel')).toBeVisible();

    const row = certificateRow(page, /shop/);
    await expect(row).toContainText('Unknown');
    await expect(row).not.toContainText('Valid');
  });
});

test.describe('current, correct, and for the wrong name', () => {
  test('an expiring certificate shows its date and how long is left', async ({ page }) => {
    await openCertificates(page);

    const row = certificateRow(page, /admin/);
    await expect(row).toContainText('Expires soon');
    await expect(page.getByTestId('certificate-expiry-ingress/prod/admin#0')).toContainText(
      '2026-09-19',
    );
  });

  test('a host the certificate does not name is red on a row that is not expired', async ({
    page,
  }) => {
    // The failure this section exists for: valid on every date field and
    // refused by every browser.
    await openCertificates(page);

    await expect(
      page.getByTestId('certificate-finding-host_not_covered'),
    ).toContainText('does not name admin.example.com');
    const chips = page.getByTestId('certificate-hosts-ingress/prod/admin#0');
    await expect(chips.getByTestId('certificate-host-false')).toContainText(
      'admin.example.com',
    );
  });

  test('an expired certificate is red and says how long ago', async ({ page }) => {
    await openCertificates(page);

    const row = certificateRow(page, /edge/);
    await expect(row).toContainText('Expired');
    await expect(
      page.getByTestId('certificate-expiry-gateway/prod/edge#https/0'),
    ).toContainText('days ago');
  });
});

test.describe('unknown is not absent, and not a flavour of valid', () => {
  test('an unreadable Secret leaves the row unknown with the reason', async ({ page }) => {
    await openCertificates(page);

    const row = certificateRow(page, /legacy/);
    await expect(row).toContainText('Unknown');
    await expect(row).not.toContainText('Valid');
    await expect(
      page.getByTestId('certificate-finding-certificate_unreadable'),
    ).toContainText('only that this console could not read the one it names');
  });

  test('its expiry is an em dash, never a distant date', async ({ page }) => {
    await openCertificates(page);

    const row = certificateRow(page, /legacy/);
    await expect(row.getByTestId('nullable-cell').first()).toHaveAttribute(
      'data-nullable',
      'true',
    );
  });

  test('its hosts are undecided rather than failing', async ({ page }) => {
    await openCertificates(page);

    const chips = page.getByTestId('certificate-hosts-ingress/prod/legacy#0');
    await expect(chips.getByTestId('certificate-host-unknown')).toContainText(
      'legacy.example.com',
    );
    await expect(chips.getByTestId('certificate-host-false')).toHaveCount(0);
  });

  test('the failed read names itself in the partial banner', async ({ page }) => {
    await openCertificates(page);

    await expect(page.getByTestId('partial-banner')).toContainText('secrets');
  });
});

test.describe('the certificates that are somewhere else', () => {
  test('a passthrough exposure says the pod holds it, and is not drawn as a fault', async ({
    page,
  }) => {
    await openCertificates(page);

    const row = certificateRow(page, /internal/);
    await expect(row).toContainText('In the pod');
    await expect(row).not.toContainText('Expired');
    await expect(
      page.getByTestId('certificate-finding-certificate_in_backend'),
    ).toContainText('which holds the certificate');
  });

  test('an exposure naming nothing is the router’s default', async ({ page }) => {
    await openCertificates(page, {
      routeCertificates: {
        ...FIXTURES.routeCertificates,
        items: [
          {
            ...FIXTURES.routeCertificates.items[3],
            id: 'route/prod/plain#tls',
            name: 'plain',
            source: 'router_default',
            termination: 'edge',
            findings: [
              {
                code: 'certificate_not_named',
                detail:
                  'This Route terminates TLS and names no certificate, so the router serves its own default.',
              },
            ],
          },
        ],
      },
    });

    const row = certificateRow(page, /plain/);
    await expect(row).toContainText("Router's default");
  });
});

test.describe('the APIs this report could not look at', () => {
  test('a kind discovery failed on is a warning, not a cluster without them', async ({
    page,
  }) => {
    await openCertificates(page, {
      routeCertificates: {
        ...FIXTURES.routeCertificates,
        kinds: [
          {
            kind: 'Route',
            group: 'route.openshift.io',
            state: 'unknown',
            version: null,
            detail:
              'Whether this cluster serves Route objects could not be determined. This is not the same as the cluster not having them.',
          },
          ...FIXTURES.routeCertificates.kinds.slice(1),
        ],
      },
    });

    await expect(page.getByTestId('certificate-kind-unknown')).toContainText(
      'This is not the same as the cluster not having them',
    );
  });

  test('a kind the cluster does not serve raises nothing', async ({ page }) => {
    await openCertificates(page, {
      routeCertificates: {
        ...FIXTURES.routeCertificates,
        kinds: FIXTURES.routeCertificates.kinds.map((kind) =>
          kind.kind === 'Route' ? { ...kind, state: 'unsupported', version: null } : kind,
        ),
      },
    });

    await expect(page.getByTestId('certificate-kind-unknown')).toHaveCount(0);
  });
});

test.describe('nothing to report', () => {
  test('a cluster with no TLS anywhere says the listings succeeded', async ({ page }) => {
    await openCertificates(page, {
      routeCertificates: {
        ...FIXTURES.routeCertificates,
        items: [],
        partial: false,
        unavailable: [],
      },
    });

    await expect(page.getByTestId('certificate-panel')).toContainText(
      'No exposure here terminates TLS',
    );
    await expect(page.getByTestId('certificate-panel')).toContainText(
      'The listings succeeded',
    );
  });
});
