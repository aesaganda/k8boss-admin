/**
 * Why is this pod Pending (§31).
 *
 * The console used to answer this with the word `Pending`. Each assertion below
 * is about a way of answering it better that would still be wrong:
 *
 *   scheduler vs kubelet   A Pending pod that already has a node has been
 *                          *placed*. Showing it a table of cluster capacity
 *                          sends the operator to audit the fleet over a failed
 *                          image pull on one machine.
 *
 *   quoted vs blank        The scheduler's own message is the verdict over
 *                          plugins this console does not model, so it is shown
 *                          verbatim — with its age, because it is a snapshot and
 *                          a node added since does not rewrite it.
 *
 *   absent vs innocent     Events age out of etcd within the hour. A missing
 *                          message is drawn as "no explanation is readable",
 *                          never as blank space that reads like nothing is
 *                          wrong.
 *
 *   ruled out vs fits      Every node row is a rule-out or a shrug. There is no
 *                          third badge, and the shrug says so in words —
 *                          including the weaker shrug from a node whose free
 *                          capacity was never examined.
 */
import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

const POD = FIXTURES.podScheduling.pod;

async function openScheduling(page, options = {}) {
  await mockApi(page, options);
  await page.goto(`/pods/prod/${POD}?tab=scheduling`);
  await expectPageRendered(page, POD);
  await expect(page.getByTestId('pod-tab-scheduling')).toBeVisible();
}

test.describe('which question this is', () => {
  test('an unplaced pod gets the scheduler’s answer and the node table', async ({ page }) => {
    await openScheduling(page);

    await expect(page.getByTestId('pod-scheduling-waiting-on')).toContainText(
      'Waiting on the scheduler',
    );
    await expect(page.getByTestId('pod-scheduling-nodes')).toBeVisible();
  });

  test('a placed pod is sent to the node, not to cluster capacity', async ({ page }) => {
    // The split this panel exists to make. Everything wrong with this pod is on
    // one machine, and a node table would start a fleet-wide capacity audit
    // over a failed image pull.
    await openScheduling(page, {
      podScheduling: {
        ...FIXTURES.podScheduling,
        waiting_on: 'kubelet',
        node: 'ip-10-0-1-4',
        scheduler: null,
      },
    });

    await expect(page.getByTestId('pod-scheduling-kubelet')).toContainText(
      'Node capacity is not the question here',
    );
    await expect(page.getByTestId('pod-scheduling-nodes')).toHaveCount(0);
    await expect(page.getByTestId('pod-scheduling-no-verdict')).toHaveCount(0);
  });

  test('a settled pod says so rather than showing an empty investigation', async ({ page }) => {
    await openScheduling(page, {
      podScheduling: {
        ...FIXTURES.podScheduling,
        phase: 'Running',
        waiting_on: 'nothing',
        node: 'ip-10-0-1-4',
        pending_seconds: null,
        scheduler: null,
      },
    });

    await expect(page.getByTestId('pod-tab-scheduling')).toContainText(
      'placement is settled',
    );
  });
});

test.describe('the scheduler’s own answer', () => {
  test('the message is quoted verbatim and dated', async ({ page }) => {
    await openScheduling(page);

    await expect(page.getByTestId('pod-scheduling-message')).toContainText(
      '0/3 nodes are available: 2 Insufficient cpu',
    );
    // The age is what makes it usable: it is a record of one past attempt.
    await expect(page.getByTestId('pod-scheduling-age')).toContainText('40m ago');
    await expect(page.getByTestId('pod-scheduling-age')).toContainText(
      'does not rewrite the message',
    );
  });

  test('no readable event is a warning, not blank space', async ({ page }) => {
    // A blank here reads as "nothing is wrong" on the one screen where
    // something demonstrably is.
    await openScheduling(page, {
      podScheduling: { ...FIXTURES.podScheduling, scheduler: null },
    });

    const alert = page.getByTestId('pod-scheduling-no-verdict');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('age out of etcd');
    await expect(alert).toContainText('not a report that the scheduler is content');
    await expect(page.getByTestId('pod-scheduling-message')).toHaveCount(0);
  });
});

test.describe('the node table, which never says a node fits', () => {
  test('a ruled-out node names the reason', async ({ page }) => {
    await openScheduling(page);

    await expect(
      page.getByTestId('pod-scheduling-reason-node_insufficient_cpu'),
    ).toContainText('Needs 3 cores');
    await expect(
      page.getByTestId('pod-scheduling-reason-node_untolerated_taint'),
    ).toContainText('gpu=true:NoSchedule');
  });

  test('a node with nothing against it is a shrug, in words', async ({ page }) => {
    await openScheduling(page);

    const row = page.getByRole('row', { name: /ip-10-0-2-7/ });
    await expect(row).toContainText('No reason found');
    // And never the word that would turn a shrug into a claim.
    await expect(row).not.toContainText('Fits');
    await expect(row).not.toContainText('Available');
  });

  test('a node whose capacity was never checked says the weaker thing', async ({ page }) => {
    // Half-examined and fully-examined-and-cleared must not look alike: one of
    // them is a node this console did not compare the request against at all.
    await openScheduling(page);

    await expect(
      page.getByTestId('pod-scheduling-unchecked-ip-10-0-2-7'),
    ).toContainText('its free capacity was not one of them');
  });

  test('a checked node says the scheduler checks more', async ({ page }) => {
    await openScheduling(page, {
      podScheduling: {
        ...FIXTURES.podScheduling,
        nodes: [
          { name: 'ip-10-0-3-1', verdict: 'no_reason_found', capacity_checked: true, reasons: [] },
        ],
      },
    });

    await expect(page.getByTestId('pod-scheduling-nodes')).toContainText(
      'The scheduler checks more',
    );
    await expect(
      page.getByTestId('pod-scheduling-unchecked-ip-10-0-3-1'),
    ).toHaveCount(0);
  });

  test('an unreadable node listing is unknown, not a cluster with no nodes', async ({ page }) => {
    await openScheduling(page, {
      podScheduling: {
        ...FIXTURES.podScheduling,
        nodes: null,
        partial: true,
        unavailable: [
          {
            group: '',
            resource: 'nodes',
            namespace: null,
            reason: 'forbidden',
            detail: 'nodes is forbidden',
          },
        ],
      },
    });

    const alert = page.getByTestId('pod-scheduling-nodes-unavailable');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('not a report that the cluster has none');
    await expect(page.getByTestId('pod-scheduling-nodes')).toHaveCount(0);
  });
});

test.describe('volumes — cause or symptom', () => {
  test('a wait-for-first-consumer claim is not reported as the blocker', async ({ page }) => {
    // It is unbound *because* the pod is unscheduled. Reporting it as the cause
    // sends somebody to fix storage while storage waits on them.
    await openScheduling(page, {
      podScheduling: {
        ...FIXTURES.podScheduling,
        claims: [
          {
            name: 'data',
            exists: true,
            phase: 'Pending',
            storage_class: 'gp3',
            binding_mode: 'WaitForFirstConsumer',
            blocks_scheduling: false,
          },
        ],
      },
    });

    const row = page.getByRole('row', { name: /data/ });
    await expect(row).toContainText('WaitForFirstConsumer');
    await expect(row).toContainText('No');
  });

  test('an undecided claim is drawn as unknown rather than as either answer', async ({ page }) => {
    await openScheduling(page, {
      podScheduling: {
        ...FIXTURES.podScheduling,
        partial: true,
        claims: [
          {
            name: 'data',
            exists: true,
            phase: 'Pending',
            storage_class: 'gp3',
            binding_mode: null,
            blocks_scheduling: null,
          },
        ],
      },
    });

    await expect(page.getByTestId('pod-scheduling-claims')).toContainText('Unknown');
  });

  test('an unreadable claim listing is unknown, not a pod that mounts none', async ({ page }) => {
    await openScheduling(page, {
      podScheduling: { ...FIXTURES.podScheduling, claims: null },
    });

    await expect(page.getByTestId('pod-scheduling-claims-unavailable')).toContainText(
      'not a report that it mounts none',
    );
  });
});
