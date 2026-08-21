import { expect, test } from '@playwright/test';

import { FIXTURES, expectPageRendered, mockApi } from './fixtures.js';

/**
 * The Debug tab (§7.4) — attaching an ephemeral container to a running pod.
 *
 * The pod this feature exists for is the one whose image is a distroless binary
 * with no shell: the one an operator most needs to get inside, and the one the
 * Terminal tab cannot help with, because there is nothing to exec. What is
 * asserted here is everything about that flow that could tell an operator
 * something untrue.
 *
 * **`supported` is three-valued and all three render differently.** `false` is
 * an ordinary fact about an old cluster and is rendered as one — §1.3 says
 * `unsupported` is not an error in the UI, because a console that paints
 * ordinary facts red teaches its operators to ignore red. `null` is *we could
 * not find out*, which is a different sentence and does not disable the action:
 * telling somebody their cluster lacks a feature it may well have sends them to
 * plan an upgrade instead of to look at their API server.
 *
 * **Nothing is written without a diff first** (rule 11.3). The Attach button
 * runs a dry run and shows the projection; only the confirming call reports a
 * change, and `applied` is the only thing that may be read as one.
 *
 * **The permanence is stated before the preview.** An ephemeral container
 * cannot be removed — the API has no verb for it — and that, not the five added
 * lines of YAML, is what the operator is consenting to.
 *
 * **A shell is offered only where one can be opened.** A container the kubelet
 * is still pulling is not attachable, and the button says why rather than
 * opening a socket that fails and reads as "the debug container did not work".
 */

/**
 * Open the pod console on a tab from the pods table.
 *
 * Clicked on the QoS cell rather than the Name cell: the name is a
 * `ResourceLink` into the API explorer, so clicking it navigates instead of
 * opening the row's console.
 */
async function openDebugTab(page) {
  await page.goto('/pods');
  await expectPageRendered(page, 'Pods');
  await page.getByRole('gridcell', { name: 'Burstable', exact: true }).first().click();
  await expect(page.getByTestId('pod-console')).toBeVisible();
  await page.getByRole('tab', { name: 'Debug', exact: true }).click();
  await expect(page.getByTestId('debug-panel')).toBeVisible();
}

/** Answer every §9 check as allowed, so the gates are not what is under test. */
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

test.describe('the Debug tab', () => {
  test('lists the debug containers already in the pod, with their real state', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);

    await expect(page.getByTestId('debug-panel')).toHaveAttribute('data-supported', 'true');
    await expect(page.getByText('debugger-x4k2p')).toBeVisible();
    await expect(page.getByText('busybox:1.36')).toBeVisible();

    // The pulling container is Waiting, not Running, and says the reason. A
    // green pill on a container that has not started is the confident wrong
    // answer this project is built against.
    await expect(page.getByText('ContainerCreating')).toBeVisible();

    // `command: null` is "the image's own entrypoint" — a real answer. It must
    // not render as the em dash, which in this console means "could not be
    // read".
    await expect(page.getByText('image entrypoint')).toBeVisible();
    await expect(page.getByText('sleep 3600')).toBeVisible();
  });

  test('a shell is offered on the running container and refused, with a reason, on the pulling one', async ({
    page,
  }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);

    await expect(page.getByTestId('debug-open-debugger-x4k2p')).toBeEnabled();

    const waiting = page.getByTestId('debug-open-debugger-m7q3v');
    await expect(waiting).toBeDisabled();
    // The reason lives on the wrapper the tooltip is attached to, so it is
    // reachable without hovering — a reason the operator cannot read is not a
    // reason.
    await expect(page.getByTestId('debug-panel')).toContainText('debugger-m7q3v');
  });

  test('opening a terminal binds it to the debug container, not to the pod default', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);

    await page.getByTestId('debug-open-debugger-x4k2p').click();
    await expect(page.getByTestId('pod-terminal')).toBeVisible();
    await expect(page.getByTestId('debug-panel')).toContainText('Terminal in debugger-x4k2p');

    // No socket until the operator asks for one: §7 audits an exec session on
    // open, and a terminal that connected the moment a panel rendered would
    // write an audit row for somebody who was only looking.
    await expect(page.getByTestId('pod-terminal')).toHaveAttribute('data-status', 'idle');

    // Armed before the click: the socket opens synchronously enough that
    // waiting afterwards races it.
    const opened = page.waitForEvent('websocket');
    await page.getByTestId('pod-terminal-connect').click();
    const socket = await opened;

    // The whole point of the feature. Without the container name the API server
    // picks the pod's *default* container and drops the operator into the very
    // application they attached a debug container in order to inspect from
    // outside — a shell that works perfectly, in the wrong place.
    expect(decodeURIComponent(socket.url())).toContain('container=debugger-x4k2p');
    // And no picker, because the caller already named the container.
    await expect(page.getByTestId('pod-terminal-container')).toHaveCount(0);
  });

  test('a cluster that does not serve ephemeral containers says so, and not in red', async ({ page }) => {
    await mockApi(page, { debug: FIXTURES.debugUnsupported, preflight: ALLOW_ALL });
    await openDebugTab(page);

    await expect(page.getByTestId('debug-panel')).toHaveAttribute('data-supported', 'false');
    await expect(page.getByTestId('debug-unsupported')).toBeVisible();
    await expect(page.getByTestId('debug-unsupported')).toContainText('does not serve');
    // §1.3: `unsupported` is an ordinary fact, not an error. No danger alert,
    // and no error state.
    await expect(page.getByTestId('error-state')).toHaveCount(0);
    await expect(page.locator('.pf-m-danger')).toHaveCount(0);
    // And the action is not offered, because there is nothing to offer it
    // against. Asserted through the accessible name rather than a test id:
    // `ActionButton` sets its own `data-testid`, so a `debug-attach` locator
    // would match nothing whether the button were rendered or not — an
    // assertion that cannot fail is worse than no assertion.
    await expect(page.getByRole('button', { name: 'Attach a debug container' })).toHaveCount(0);
    await expect(page.getByTestId('debug-refresh')).toHaveCount(0);
  });

  test('a cluster whose support could not be determined is “unknown”, and the action stays available', async ({
    page,
  }) => {
    await mockApi(page, { debug: FIXTURES.debugSupportUnknown, preflight: ALLOW_ALL });
    await openDebugTab(page);

    await expect(page.getByTestId('debug-panel')).toHaveAttribute('data-supported', 'null');
    const banner = page.getByTestId('debug-support-unknown');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('could not be confirmed');
    await expect(banner).toContainText('not');

    // The distinction that matters: unknown does not disable the action. The
    // API server is allowed to answer.
    await expect(page.getByRole('button', { name: 'Attach a debug container' })).toBeEnabled();
  });

  test('a pod with no debug containers says so, and it is a real zero', async ({ page }) => {
    await mockApi(page, {
      debug: { ...FIXTURES.debugSupported, items: [] },
      preflight: ALLOW_ALL,
    });
    await openDebugTab(page);

    await expect(page.getByTestId('debug-empty')).toBeVisible();
    // `partial: false` with a populated read — the backend raises rather than
    // answering with an empty list it could not verify, so the empty state is
    // entitled to be confident.
    await expect(page.getByTestId('partial-banner')).toHaveCount(0);
  });

  test('attaching shows the permanence, then a diff, and only then writes', async ({ page }) => {
    const posts = [];
    await mockApi(page, {
      preflight: ALLOW_ALL,
      debugAttach: (body) => {
        posts.push(body);
        return {
          dryRun: body.dryRun !== false,
          applied: body.dryRun === false,
          verb: 'patch',
          target: {
            group: '',
            version: 'v1',
            resource: 'pods',
            namespace: 'prod',
            name: 'checkout-7d9f8b6c4-hk2xv',
            subresource: 'ephemeralcontainers',
          },
          diff: {
            before: 'spec: {}\n',
            after: 'spec:\n  ephemeralContainers:\n    - name: debugger-x4k2p\n',
            unified:
              '--- live\n+++ projected\n@@ -1,1 +1,3 @@\n spec: {}\n+  ephemeralContainers:\n+    - name: debugger-x4k2p\n',
            changed: true,
          },
          resourceVersion: '884214',
          warnings: [],
          auditId: 4021,
          container: 'debugger-x4k2p',
          image: body.image || 'busybox:1.36',
          targetContainer: body.targetContainer ?? null,
          command: body.command ?? null,
          tty: body.tty !== false,
        };
      },
    });
    await openDebugTab(page);

    await page.getByRole('button', { name: 'Attach a debug container' }).click();

    // The fact the operator is actually consenting to, before anything is sent.
    await expect(page.getByTestId('debug-permanence')).toBeVisible();
    await expect(page.getByTestId('debug-permanence')).toContainText('cannot be removed');
    expect(posts, 'opening the dialog must not touch the cluster').toHaveLength(0);

    await page.getByTestId('debug-image').fill('nicolaka/netshoot:v0.13');
    await page.getByRole('button', { name: 'Preview the change' }).click();

    // Rule 11.3: the diff is shown, and the call that produced it was a dry run.
    await expect(page.getByTestId('diff-view')).toBeVisible();
    expect(posts).toHaveLength(1);
    expect(posts[0].dryRun).toBe(true);
    expect(posts[0].image).toBe('nicolaka/netshoot:v0.13');

    await page.getByRole('button', { name: 'Attach', exact: true }).click();

    expect(posts).toHaveLength(2);
    expect(posts[1].dryRun).toBe(false);
    await expect(page.getByText('debugger-x4k2p is attached')).toBeVisible();
  });

  test('a terminated debug container reports when it started, not "Not started"', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);

    // The row that says Terminated must not also say the container never
    // started. `startedAt` lives on the terminated state too, and the two facts
    // the contradiction conflates — ran and exited, versus never started — send
    // an operator in opposite directions.
    const row = page.getByRole('row', { name: /debugger-r8t5w/ });
    // The pill shows the *reason* over the bare state, the same way a pod's
    // `phase_detail` overrides its phase — "Error" is more use than
    // "Terminated".
    await expect(row).toContainText('Error');
    // The Started column carries an age, because the container has one.
    await expect(row).not.toContainText('Not started');
    await expect(row).toContainText(/\d+[dhm]/);
  });

  test('clicking a suggested image fills the field', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);
    await page.getByRole('button', { name: 'Attach a debug container' }).click();

    // The chips carried an `href` and PatternFly's Label drops `onClick` on
    // that branch, so they rendered as clickable, styled as clickable, and did
    // nothing.
    await page.getByTestId('debug-image-suggestion-nicolaka/netshoot:v0.13').click();
    await expect(page.getByTestId('debug-image')).toHaveValue('nicolaka/netshoot:v0.13');
  });

  test('the container name the diff showed is the one that gets written', async ({ page }) => {
    const posts = [];
    await mockApi(page, {
      preflight: ALLOW_ALL,
      // Models the backend faithfully: with no name in the request it generates
      // a *fresh* one per call, which is exactly what makes the carry
      // necessary.
      debugAttach: (body) => {
        posts.push(body);
        const generated = body.container || `debugger-gen${posts.length}`;
        return {
          dryRun: body.dryRun !== false,
          applied: body.dryRun === false,
          verb: 'patch',
          target: { group: '', version: 'v1', resource: 'pods', namespace: 'prod',
                    name: 'checkout-7d9f8b6c4-hk2xv', subresource: 'ephemeralcontainers' },
          diff: {
            before: 'spec: {}\n',
            after: `spec:\n  ephemeralContainers:\n    - name: ${generated}\n`,
            unified: `--- live\n+++ projected\n@@ -1,1 +1,3 @@\n spec: {}\n+  ephemeralContainers:\n+    - name: ${generated}\n`,
            changed: true,
          },
          resourceVersion: '884214',
          warnings: [],
          auditId: 4021,
          container: generated,
          image: body.image || 'busybox:1.36',
          targetContainer: null,
          command: null,
          tty: true,
        };
      },
    });
    await openDebugTab(page);

    await page.getByRole('button', { name: 'Attach a debug container' }).click();
    await page.getByRole('button', { name: 'Preview the change' }).click();
    await expect(page.getByTestId('diff-view')).toBeVisible();

    // The name in the diff the operator is looking at.
    const previewed = posts[0].container ?? 'debugger-gen1';
    await expect(page.getByTestId('diff-view')).toContainText(previewed);

    await page.getByRole('button', { name: 'Attach', exact: true }).click();

    // The confirming call must carry it. Without the carry the backend would
    // generate a second name, and the container that appeared in the pod would
    // not be the one whose diff was approved — rule 11.3 satisfied on screen
    // and broken in the cluster.
    expect(posts).toHaveLength(2);
    expect(posts[1].container).toBe(previewed);
    await expect(page.getByText(`${previewed} is attached`)).toBeVisible();
  });

  test('attaching a debug container does not make a single-container pod ambiguous', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: ALLOW_ALL,
      // One application container plus one attached debug container — the §6
      // row now carries both, tagged by `kind`.
      pods: {
        ...FIXTURES.pods,
        items: [
          {
            ...FIXTURES.pods.items[0],
            containers: [
              { name: 'app', image: 'registry.example:5000/checkout:1.4.2', ready: true, restart_count: 0, kind: 'container' },
              { name: 'debugger-x4k2p', image: 'busybox:1.36', ready: false, restart_count: 0, kind: 'ephemeral' },
            ],
          },
          FIXTURES.pods.items[1],
        ],
      },
    });
    await page.goto('/pods');
    await expectPageRendered(page, 'Pods');
    await page.getByRole('gridcell', { name: 'Burstable', exact: true }).first().click();
    await expect(page.getByTestId('pod-console')).toBeVisible();

    // Logs: the API server still defaults the container, because it counts
    // `spec.containers` only. A viewer that started refusing here would have
    // become stricter than the API it is a client of — and would have broken
    // its own log viewer as a side effect of somebody opening a shell.
    // Asserted on the output pane rather than on the words "Choose a
    // container": the picker's own placeholder option carries that text, so a
    // text locator matches it whether the viewer is waiting or not.
    await expect(page.getByTestId('log-output')).toBeVisible();
    // The debug container is still selectable, and labelled as one.
    const options = await page.getByTestId('log-container').locator('option').allTextContents();
    expect(options).toContain('debugger-x4k2p (debug)');

    // Terminal: same rule.
    await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
    await expect(page.getByTestId('pod-terminal-choose')).toHaveCount(0);
    await expect(page.getByTestId('pod-terminal-connect')).toBeEnabled();
  });

  test('the command box shows the argv it will send, so quoting decides nothing', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);
    await page.getByRole('button', { name: 'Attach a debug container' }).click();

    await page.getByTestId('debug-command').fill('sh -c "echo hi"');

    // Split on whitespace, quotes not interpreted — and shown as the literal
    // list that goes on the wire, so nothing about how the operator quoted it
    // decides what runs in the pod.
    await expect(page.getByTestId('debug-argv')).toHaveText('argv: ["sh", "-c", "\\"echo", "hi\\""]');
  });

  test('a container name already in the pod is refused before a request is made', async ({ page }) => {
    const posts = [];
    await mockApi(page, {
      preflight: ALLOW_ALL,
      debugAttach: (body) => {
        posts.push(body);
        return { dryRun: true, applied: false, diff: { changed: false }, container: 'x' };
      },
    });
    await openDebugTab(page);
    await page.getByRole('button', { name: 'Attach a debug container' }).click();

    // `app` is the pod's own container; `debugger-x4k2p` is one already
    // attached. Both are taken — containers, init containers and ephemeral
    // containers share one namespace of names.
    await page.getByTestId('debug-name').fill('app');
    await expect(page.getByRole('button', { name: 'Preview the change' })).toBeDisabled();

    await page.getByTestId('debug-name').fill('debugger-x4k2p');
    await expect(page.getByRole('button', { name: 'Preview the change' })).toBeDisabled();

    await page.getByTestId('debug-name').fill('debugger-mine');
    await expect(page.getByRole('button', { name: 'Preview the change' })).toBeEnabled();

    expect(posts, 'a name clash must cost no round trip and no audit row').toHaveLength(0);
  });

  test('a debug container is not offered as a process-namespace target', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await openDebugTab(page);
    await page.getByRole('button', { name: 'Attach a debug container' }).click();

    const options = await page.getByTestId('debug-target').locator('option').allTextContents();
    expect(options).toContain('app');
    // Targeting another ephemeral container is not something the API supports:
    // the manifest would accept the name and the runtime would quietly do
    // nothing.
    expect(options).not.toContain('debugger-x4k2p');
  });

  test('without the permission, the action is disabled with the reason rather than hidden', async ({
    page,
  }) => {
    await mockApi(page, {
      preflight: (checks) =>
        checks.map((check) => ({
          verb: check.verb,
          group: check.group,
          resource: check.resource,
          namespace: check.namespace ?? null,
          subresource: check.subresource ?? null,
          allowed: check.subresource !== 'ephemeralcontainers',
          reason: '',
          evaluationError: null,
          hint:
            check.subresource === 'ephemeralcontainers'
              ? 'Grant patch on pods/ephemeralcontainers in namespace prod.'
              : null,
        })),
    });
    await openDebugTab(page);

    // Rule 11.4: visible, disabled, and carrying the grant the preflight
    // computed. Hidden would leave an operator wondering whether this console
    // can debug at all.
    const button = page.getByRole('button', { name: 'Attach a debug container' });
    await expect(button).toBeVisible();
    await expect(button).toHaveAttribute('data-allowed', 'false');
    await expect(page.locator('.admin-gated-action')).toBeVisible();
  });
});
