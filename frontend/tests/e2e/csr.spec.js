/**
 * Certificate signing requests (§25).
 *
 * Every assertion here is about a pair a convenient rendering would collapse,
 * and on this page collapsing one hands somebody a credential:
 *
 *   requestor vs subject      `spec.username` is who asked. The common name
 *                             inside the request is who they asked to *become*,
 *                             and it is on no other screen. A request from an
 *                             ordinary user for `system:masters` is cluster-admin
 *                             and looks like a kubelet renewal everywhere else.
 *   `subject: null` vs `{}`   Nobody could decode it, versus it asks for
 *                             nothing. The second is the most reassuring
 *                             possible description of the first.
 *   Approved vs Issued        A condition was recorded, versus a certificate
 *                             exists. Where no signer runs for the signerName,
 *                             a request stops at the first forever.
 *   decided vs decidable      There is no un-approve. A request that has been
 *                             decided is not offered a second decision.
 */
import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

const ALLOW = (checks) =>
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

async function openTab(page) {
  await page.goto('/access/csrs');
  await expect(page.getByRole('row', { name: /csr-kubelet-renew/ })).toBeVisible();
}

async function decide(page, name, action) {
  await openTab(page);
  await page.getByRole('row', { name: new RegExp(name) }).getByRole('button').click();
  await page.getByRole('menuitem', { name: action }).click();
  await expect(page.getByTestId('csr-final')).toBeVisible();
}

test.describe('certificate requests — the listing', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
  });

  test('the table shows what a request would become, not only who asked', async ({ page }) => {
    await openTab(page);

    // The row that matters: an ordinary user asking to be cluster-admin. Both
    // halves have to be on screen, because either alone is unremarkable.
    const escalation = page.getByRole('row', { name: /csr-escalation/ });
    await expect(escalation).toContainText('dev@example.com');
    await expect(escalation).toContainText('system:masters');
  });

  test('approved without a certificate is not drawn as issued', async ({ page }) => {
    await openTab(page);

    await expect(page.getByRole('row', { name: /csr-approved-unissued/ })).toContainText(
      'Approved',
    );
    await expect(page.getByRole('row', { name: /csr-issued/ })).toContainText('Issued');
    await expect(page.getByRole('row', { name: /csr-approved-unissued/ })).not.toContainText(
      'Issued',
    );
  });

  test('a request nobody could decode is an em dash, never an empty subject', async ({ page }) => {
    await openTab(page);

    const row = page.getByRole('row', { name: /csr-unreadable/ });
    await expect(row).toContainText('—');
    await expect(row).not.toContainText('no common name');
  });

  test('a signerName nothing built in signs is flagged in the listing', async ({ page }) => {
    await openTab(page);

    await expect(page.getByRole('row', { name: /csr-custom-signer/ })).toContainText(
      'no built-in signer',
    );
    await expect(page.getByRole('row', { name: /csr-kubelet-renew/ })).not.toContainText(
      'no built-in signer',
    );
  });

  test('a request that has already been decided is offered no second decision', async ({
    page,
  }) => {
    await openTab(page);

    // There is no un-approve, so there is no button that would always fail.
    await expect(
      page.getByRole('row', { name: /csr-issued/ }).getByRole('button'),
    ).toHaveCount(0);
    await expect(
      page.getByRole('row', { name: /csr-kubelet-renew/ }).getByRole('button'),
    ).toHaveCount(1);
  });
});

test.describe('certificate requests — deciding', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, { preflight: ALLOW });
  });

  test('the dialog says the decision is final before the first click', async ({ page }) => {
    await decide(page, 'csr-kubelet-renew', 'Approve…');

    const final = page.getByTestId('csr-final');
    await expect(final).toContainText('cannot be changed');
    await expect(final).toContainText('approving is not issuing');
  });

  test('an ordinary kubelet renewal asks for no acknowledgement', async ({ page }) => {
    await decide(page, 'csr-kubelet-renew', 'Approve…');

    await expect(page.getByTestId('csr-consequences')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Preview the decision' })).toBeEnabled();
  });

  test('system:masters is named as cluster-admin and blocks preview until ticked', async ({
    page,
  }) => {
    await decide(page, 'csr-escalation', 'Approve…');

    await expect(page.getByTestId('csr-organizations')).toContainText('system:masters');
    const consequence = page.getByTestId('csr-ack-csr_grants_cluster_admin');
    await expect(consequence).toBeVisible();

    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'Acknowledge what this decision means',
    );
    await consequence.check();
    await expect(page.getByRole('button', { name: 'Preview the decision' })).toBeEnabled();
  });

  test('a bootstrap request names both the mismatch and what a node identity reaches', async ({
    page,
  }) => {
    await decide(page, 'csr-bootstrap', 'Approve…');

    await expect(page.getByTestId('csr-ack-csr_grants_node_identity')).toBeVisible();
    await expect(page.getByTestId('csr-ack-csr_subject_is_not_requestor')).toBeVisible();
  });

  test('a request nobody could decode says the subject is unknown, not empty', async ({ page }) => {
    await decide(page, 'csr-unreadable', 'Approve…');

    const banner = page.getByTestId('csr-undecodable');
    await expect(banner).toContainText('unknown, not empty');
    await expect(page.getByTestId('csr-ack-csr_request_undecodable')).toBeVisible();
  });

  test('a signerName nothing signs warns that approval would stop at Approved', async ({
    page,
  }) => {
    await decide(page, 'csr-custom-signer', 'Approve…');

    // The checkbox itself carries the testid; the sentence is in the alert
    // around it, which is what an operator actually reads.
    await expect(page.getByTestId('csr-ack-csr_no_known_signer')).toBeVisible();
    await expect(page.getByTestId('csr-consequences')).toContainText(
      'Approved with no certificate',
    );
  });

  test('denying a node request says that node will not become Ready', async ({ page }) => {
    await decide(page, 'csr-kubelet-renew', 'Deny…');

    await expect(page.getByTestId('csr-ack-csr_deny_blocks_node')).toBeVisible();
    await expect(page.getByTestId('csr-consequences')).toContainText('not become Ready');
  });

  test('the first call is a projection and carries the decision as a condition type', async ({
    page,
  }) => {
    const decisions = [];
    await mockApi(page, { preflight: ALLOW, csrDecisions: decisions });
    await decide(page, 'csr-kubelet-renew', 'Approve…');

    await page.getByRole('button', { name: 'Preview the decision' }).click();
    await expect(page.getByRole('button', { name: 'Approve the request' })).toBeVisible();

    expect(decisions).toHaveLength(1);
    expect(decisions[0].body.decision).toBe('Approved');
    // §0.3: nothing has been written yet.
    expect(decisions[0].body.dryRun).toBe(true);
    // §0.4: the version the plan read rides along.
    expect(decisions[0].body.resourceVersion).toBe('7719');
  });

  test('confirming says the condition is recorded and not that a certificate exists', async ({
    page,
  }) => {
    await decide(page, 'csr-kubelet-renew', 'Approve…');

    await page.getByRole('button', { name: 'Preview the decision' }).click();
    await page.getByRole('button', { name: 'Approve the request' }).click();

    const summary = page.getByTestId('mutation-summary');
    await expect(summary).toContainText('is now approved');
    await expect(summary).toContainText('That is the condition, not a certificate');
    await expect(summary).toContainText('Issued rather than Approved');
  });
});
