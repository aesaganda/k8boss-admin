/**
 * Granting and revoking a role (§30).
 *
 * §4's YAML editor can already write this object. What this dialog adds is the
 * screen in front of it, and every assertion here is about a sentence that
 * would otherwise be produced falsely:
 *
 *   a name vs a capability   `roleRef: ClusterRole/admin` is three words.
 *                            Confirming the word is not confirming the grant,
 *                            so what the role confers has to be on the screen
 *                            the operator is looking at when they decide.
 *
 *   unreadable vs empty      A role whose rules could not be fetched grants
 *                            *unknown*. Drawing that as an empty rule list
 *                            tells somebody a role grants nothing on the screen
 *                            where they decide to bind it.
 *
 *   removed vs revoked       Taking a subject out of one binding is not taking
 *                            away their access. `applied: true` on a revoke is
 *                            a claim about a subject list, not about a
 *                            permission, and the summary has to say which.
 *
 *   [] vs unknown            An empty residual table means the cluster was
 *                            searched. An unread cluster listing means nobody
 *                            knows. The first sentence closes a ticket.
 */
import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

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

async function openDialog(page, options = {}) {
  await mockApi(page, { preflight: ALLOW_ALL, ...options });
  await page.goto('/namespaces/prod');
  await expectPageRendered(page, 'Production (prod)');
  await page.getByRole('button', { name: 'Grant or revoke a role' }).click();
  await expect(page.getByTestId('mutation-dialog')).toBeVisible();
}

async function fill(page, { role = 'view', subject = 'alice', revoke = false } = {}) {
  if (revoke) await page.getByTestId('grant-operation-revoke').click();
  await page.getByTestId('grant-role-name').fill(role);
  await page.getByTestId('grant-subject-name').fill(subject);
  await page.getByTestId('grant-read-role').click();
  await expect(page.getByTestId('grant-capability')).toBeVisible();
}

test.describe('what the role confers', () => {
  test('the powers are on screen before the preview button is usable', async ({ page }) => {
    await openDialog(page, {
      grantOptions: {
        powers: [
          {
            code: 'grant_pod_exec',
            detail:
              'Opens a shell in any pod here, which reads every Secret mounted into it — whether or not this role mentions Secrets.',
          },
        ],
      },
    });
    await fill(page, { role: 'edit' });

    await expect(page.getByTestId('grant-power-grant_pod_exec')).toBeVisible();
    await expect(page.getByTestId('grant-powers')).toContainText(
      'whether or not this role mentions Secrets',
    );
  });

  test('a role that confers none of the five says so rather than looking unread', async ({ page }) => {
    await openDialog(page);
    await fill(page);

    await expect(page.getByTestId('grant-powers-none')).toBeVisible();
    await expect(page.getByTestId('grant-rule-count')).toContainText('1');
  });

  test('a role that could not be read is unknown, never a count of zero', async ({ page }) => {
    await openDialog(page, { grantOptions: { state: 'unreadable' } });
    await fill(page);

    // `NullableCell` always carries the testid and publishes which state it is
    // in as `data-nullable`; asserting the testid's absence would pass for a
    // cell rendering an em dash.
    await expect(
      page.getByTestId('grant-rule-count').getByTestId('nullable-cell'),
    ).toHaveAttribute('data-nullable', 'true');
    await expect(page.getByTestId('grant-rule-count')).not.toContainText('0');
    await expect(page.getByTestId('grant-capability')).toContainText('Could not be read');
  });

  test('a role that does not exist is its own state, not an unreadable one', async ({ page }) => {
    await openDialog(page, { grantOptions: { state: 'absent' } });
    await fill(page, { role: 'vieww' });

    await expect(page.getByTestId('grant-capability')).toContainText('Does not exist');
  });
});

test.describe('the handshake', () => {
  test('confirm is blocked until the consequence is acknowledged', async ({ page }) => {
    await openDialog(page, {
      grantOptions: {
        powers: [{ code: 'grant_full_control', detail: 'Every verb on every resource.' }],
      },
    });
    await fill(page, { role: 'cluster-admin' });

    // The preview itself is gated: an unacknowledged consequence is not a thing
    // to discover after the diff is on screen.
    await expect(page.getByTestId('mutation-preview')).toBeDisabled();
    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'confers full control',
    );

    await page.getByTestId('grant-ack-grant_confers_full_control').check();
    await expect(page.getByTestId('mutation-preview')).toBeEnabled();
  });

  // Editing any field clears the plan, which is what drops the acknowledgement:
  // the codes are stable strings and the sentences behind them are not, so a
  // tick surviving a re-plan would carry consent for something nobody read. The
  // property is asserted per field rather than once, because it is enforced by
  // seven separate handlers and one of them going quiet would look like nothing
  // from outside — the plan simply staying on screen a moment longer.
  for (const [field, value] of [
    ['grant-role-name', 'cluster-admin-2'],
    ['grant-subject-name', 'bob'],
  ]) {
    test(`editing ${field} withdraws the acknowledgement and the plan with it`, async ({ page }) => {
      await openDialog(page, {
        grantOptions: {
          powers: [{ code: 'grant_full_control', detail: 'Every verb on every resource.' }],
        },
      });
      await fill(page, { role: 'cluster-admin' });
      await page.getByTestId('grant-ack-grant_confers_full_control').check();
      await expect(page.getByTestId('mutation-preview')).toBeEnabled();

      await page.getByTestId(field).fill(value);

      // The plan goes first. A checklist rendered beside inputs it was not
      // computed from is the screen this dialog exists to replace.
      await expect(page.getByTestId('grant-capability')).toHaveCount(0);
      await expect(page.getByTestId('mutation-preview')).toBeDisabled();
      await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
        'Read what this role confers',
      );

      await page.getByTestId('grant-read-role').click();
      await expect(page.getByTestId('grant-capability')).toBeVisible();
      await expect(
        page.getByTestId('grant-ack-grant_confers_full_control'),
      ).not.toBeChecked();
      await expect(page.getByTestId('mutation-preview')).toBeDisabled();
    });
  }
});

test.describe('what a revoke does not take away', () => {
  test('another binding is named, and the summary refuses to say revoked', async ({ page }) => {
    await openDialog(page, {
      grantOptions: {
        binding: { name: 'view', namespace: 'prod', role: { kind: 'ClusterRole', name: 'view' } },
        residual: {
          namespace_bindings: [
            { name: 'edit', namespace: 'prod', role: { kind: 'ClusterRole', name: 'edit' } },
          ],
          cluster_bindings: [],
        },
      },
    });
    await fill(page, { revoke: true });

    await expect(page.getByTestId('grant-residual')).toContainText('edit');
    await page.getByTestId('grant-ack-revoke_access_remains').check();
    await page.getByTestId('mutation-preview').click();
    await page.getByTestId('mutation-confirm').click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText('their access may not be gone');
    await expect(summary).toContainText('not a revoked permission');
  });

  test('an unread cluster listing is unknown, not an empty result', async ({ page }) => {
    await openDialog(page, {
      grantOptions: {
        binding: { name: 'view', namespace: 'prod', role: { kind: 'ClusterRole', name: 'view' } },
        residual: { namespace_bindings: [], cluster_bindings: null },
        unavailable: [
          {
            group: 'rbac.authorization.k8s.io',
            resource: 'clusterrolebindings',
            namespace: null,
            reason: 'forbidden',
            detail: 'clusterrolebindings is forbidden',
          },
        ],
      },
    });
    await fill(page, { revoke: true });

    await expect(page.getByTestId('grant-residual-unknown')).toBeVisible();
    await expect(page.getByTestId('grant-residual')).toContainText(
      'Nothing else was found in what could be read',
    );
  });

  test('a truncated cluster listing is drawn like a refused one', async ({ page }) => {
    // Refused and truncated are the same claim at different strengths. An empty
    // table beside a page that stopped early says "the first page did not name
    // them", and drawing that as the clean case is the sentence somebody closes
    // a ticket on.
    await openDialog(page, {
      grantOptions: {
        binding: { name: 'view', namespace: 'prod', role: { kind: 'ClusterRole', name: 'view' } },
        residual: { namespace_bindings: [], cluster_bindings: [], cluster_truncated: true },
        consequences: [
          {
            code: 'revoke_residual_unknown',
            label: 'More cluster-wide bindings exist than were read',
            consequence: 'Whether they keep access through one is unknown.',
            mitigation: 'Ask the API server with a subject review.',
          },
        ],
      },
    });
    await fill(page, { revoke: true });

    await expect(page.getByTestId('grant-residual-unknown')).toBeVisible();
    await expect(page.getByTestId('grant-residual-unknown')).toContainText(
      'More cluster-wide bindings exist than were read',
    );
    await expect(page.getByTestId('grant-residual')).not.toContainText(
      'Nothing else names this subject',
    );
  });

  test('a clean revoke says the cluster was searched, and still points at the review', async ({ page }) => {
    await openDialog(page, {
      grantOptions: {
        binding: { name: 'view', namespace: 'prod', role: { kind: 'ClusterRole', name: 'view' } },
      },
    });
    await fill(page, { revoke: true });

    await expect(page.getByTestId('grant-residual-unknown')).toHaveCount(0);
    await expect(page.getByTestId('grant-residual')).toContainText(
      'Nothing else names this subject',
    );

    await page.getByTestId('mutation-preview').click();
    await page.getByTestId('mutation-confirm').click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText('removed from this binding');
    // Even the clean case does not claim the permission is gone: this console
    // subtracted objects, and only the API server can answer authoritatively.
    await expect(summary).toContainText('subject review is still the authoritative answer');
  });
});

test('the write is previewed before it is confirmed', async ({ page }) => {
  const writes = [];
  await openDialog(page);
  page.on('request', (request) => {
    if (request.method() === 'PUT' && request.url().includes('/grants')) {
      writes.push(JSON.parse(request.postData() || '{}'));
    }
  });
  await fill(page);

  await page.getByTestId('mutation-preview').click();
  await expect(page.getByTestId('mutation-confirm')).toBeVisible();
  await page.getByTestId('mutation-confirm').click();
  await expect(page.getByTestId('mutation-summary')).toBeVisible();

  expect(writes.map((body) => body.dryRun)).toEqual([true, false]);
  expect(writes[1].role).toEqual({ kind: 'ClusterRole', name: 'view' });
  expect(writes[1].subject).toEqual({ kind: 'User', name: 'alice' });
});
