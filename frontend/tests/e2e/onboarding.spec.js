/**
 * §34 — the onboarding panel, and the two things about it that fail silently.
 *
 *   a refusal is a row      An `exec` context cannot be imported, and the
 *                           tempting implementation filters it out. Then an
 *                           operator whose only context is an EKS one sees the
 *                           same empty panel as somebody with no kubeconfig,
 *                           and the two mean completely different things. §0.1
 *                           applied to a file instead of a cluster.
 *
 *   unreadable ≠ absent     A kubeconfig the backend process may not read is
 *                           the single most common way this feature does
 *                           nothing on a Compose deployment, and it used to
 *                           produce no signal whatsoever. It renders as a
 *                           warning naming the file permission — and pointedly
 *                           not as a Kubernetes permission, because no cluster
 *                           is involved and sending someone to edit a
 *                           ClusterRole over a `chmod` is the wrong-system
 *                           version of a confidently wrong answer.
 *
 * The panel is administrator-only because its two endpoints are, so the third
 * test asserts it is *absent* rather than disabled — rule 11.4 is about
 * controls an account could hold and does not, and this is one it cannot see.
 */
import { expect, test } from '@playwright/test';

import { expectPageRendered, mockApi } from './fixtures.js';

const NON_ADMIN = {
  authenticated: true,
  user: {
    id: 9,
    username: 'viewer',
    display_name: 'Viewer',
    email: null,
    role: 'user',
    auth_source: 'local',
  },
};

async function openClusters(page, options = {}) {
  await mockApi(page, options);
  await page.goto('/clusters');
  await expectPageRendered(page, 'Clusters');
}

test('every discovered context is listed, including the one it refuses', async ({ page }) => {
  await openClusters(page);

  const panel = page.getByTestId('discovery-panel');
  await expect(panel).toBeVisible();

  // The two it can take, offered.
  await expect(page.getByTestId('discovery-import-kind-dev')).toBeVisible();
  await expect(page.getByTestId('discovery-import-k3d-lab')).toBeVisible();

  // The one it cannot: present, and carrying its reason rather than absent.
  await expect(page.getByTestId('discovery-refused-prod-eks')).toBeVisible();
  await expect(page.getByTestId('discovery-import-prod-eks')).toHaveCount(0);
  await expect(panel.getByText('prod-eks')).toBeVisible();
});

test('importing sends the context and nothing else', async ({ page }) => {
  const clusterImports = [];
  await openClusters(page, { clusterImports });

  await page.getByTestId('discovery-import-kind-dev').click();

  await expect.poll(() => clusterImports).toEqual([{ context: 'kind-dev' }]);
  // The notification says what was *not* established: an import connects to
  // nothing, so the cluster's status is unknown rather than connected.
  await expect(page.getByText(/has not been connected to yet/i)).toBeVisible();
});

test('a loopback context inside a container says so before you import it', async ({ page }) => {
  await openClusters(page, {
    discovery: {
      items: [
        {
          context: 'kind-dev',
          cluster: 'kind-dev',
          api_server: 'https://127.0.0.1:6443',
          namespace: null,
          distribution: 'kind',
          is_local: true,
          is_current: true,
          credential: 'client_certificate',
          authentication_type: 'client_certificate',
          importable: true,
          reason: null,
          concern:
            'https://127.0.0.1:6443 is a loopback address and this console is running in a ' +
            'container, where that address is the container itself rather than the machine ' +
            'your cluster is on.',
          has_ca_certificate: true,
          skip_tls_verify: false,
        },
      ],
      continue: null,
      remaining: null,
      partial: false,
      unavailable: [],
      source: { path: '/home/k8boss/.kube/config', current_context: 'kind-dev', in_container: true },
      auto_discovery: { enabled: true, candidates: [] },
    },
  });

  // Still importable — the console cannot know whether the container shares the
  // host's network namespace, and refusing outright would be its own wrong
  // answer. Offered, with the concern beside it.
  await expect(page.getByTestId('discovery-import-kind-dev')).toBeEnabled();
  await expect(page.getByTestId('discovery-concern-kind-dev')).toContainText('loopback address');
});

test('an unreadable kubeconfig is a warning about a file, not about RBAC', async ({ page }) => {
  await openClusters(page, {
    discovery: {
      items: [],
      continue: null,
      remaining: null,
      partial: true,
      unavailable: [
        {
          group: '',
          resource: 'kubeconfig',
          namespace: null,
          reason: 'forbidden',
          detail:
            '/home/k8boss/.kube/config exists but this process cannot read it (running as uid 10001).',
        },
      ],
      source: { path: '/home/k8boss/.kube/config', current_context: null, in_container: true },
      auto_discovery: { enabled: true, candidates: [] },
    },
  });

  const alert = page.getByTestId('discovery-unavailable');
  await expect(alert).toContainText('running as uid 10001');
  await expect(alert).toContainText('not a Kubernetes one');
});

test('a machine with no kubeconfig says so quietly and offers nothing', async ({ page }) => {
  await openClusters(page, {
    discovery: {
      items: [],
      continue: null,
      remaining: null,
      partial: true,
      unavailable: [
        {
          group: '',
          resource: 'kubeconfig',
          namespace: null,
          reason: 'not_found',
          detail: 'No kubeconfig at /home/k8boss/.kube/config.',
        },
      ],
      source: { path: '/home/k8boss/.kube/config', current_context: null, in_container: true },
      auto_discovery: { enabled: true, candidates: [] },
    },
  });

  // Not a warning card: this is the ordinary state of a production console
  // whose clusters are registered by hand, and colouring it red would train
  // people to ignore the panel that reports the other two reasons.
  await expect(page.getByTestId('discovery-panel')).toHaveCount(0);
  await expect(page.getByTestId('discovery-absent')).toContainText('nothing to import');
});

test('a non-administrator does not see the panel at all', async ({ page }) => {
  await openClusters(page, { auth: NON_ADMIN });

  // Absent rather than disabled, unlike the register button beside it: §34's
  // endpoints are administrator-only, so there is no state of this account in
  // which the panel would have anything to show.
  await expect(page.getByTestId('discovery-panel')).toHaveCount(0);
  await expect(page.getByTestId('cluster-add')).toBeVisible();
});

test('an adopted cluster says it was adopted', async ({ page }) => {
  await openClusters(page, {
    clusters: {
      items: [
        {
          id: 1,
          name: 'kind-dev',
          platform: 'kubernetes',
          api_server: 'https://127.0.0.1:6443',
          authentication_type: 'client_certificate',
          has_ca_certificate: true,
          has_client_certificate: true,
          origin: 'autodiscovered',
          skip_tls_verify: false,
          impersonation_enabled: false,
          app_domain: null,
          status: 'unknown',
          server_version: null,
          last_connected: null,
          created_at: '2026-09-19T10:00:00Z',
          updated_at: '2026-09-19T10:00:00Z',
        },
      ],
      continue: null,
      remaining: null,
      partial: false,
      unavailable: [],
    },
  });

  await page.getByRole('gridcell', { name: 'kind-dev' }).first().click();

  // The first question about a cluster nobody remembers registering.
  await expect(page.getByText('Adopted automatically at startup')).toBeVisible();
});
