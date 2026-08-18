import process from 'node:process';
import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests run against a production build served by `vite preview`,
 * not against the dev server.
 *
 * Two reasons. The lazy page chunks in App.jsx only exist as separate files in
 * a build, so a chunk-splitting mistake — a page that pulls PatternFly into the
 * initial bundle, or a circular import that makes a chunk fail to load — is
 * invisible in dev and caught here. And the dev server's on-demand transform
 * makes the first navigation to each route slow enough to produce flaky
 * timeouts that have nothing to do with the code under test.
 *
 * The suite is hermetic: every spec intercepts `**\/api/**` with `page.route`,
 * so no backend and no cluster is required. That is deliberate — a smoke suite
 * that needs a live cluster is a smoke suite nobody runs.
 */
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://localhost:5174',
    trace: 'on-first-retry',
    // Pin locale and timezone: several cells render timestamps, and an
    // assertion on rendered text otherwise passes in UTC and fails in CEST.
    locale: 'en-US',
    timezoneId: 'UTC',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: {
          // Normally undefined, so CI uses the browser `npx playwright install`
          // put in the default location — which is what we want there.
          //
          // The override exists for sandboxes that ship a pre-installed Chromium
          // pinned to a different build number than this @playwright/test
          // expects. Playwright then looks for a revision that is not on disk and
          // tells you to run `playwright install`, which those images
          // deliberately disable. Pointing at the binary that IS there is the
          // difference between the suite running and the suite being skipped —
          // and a smoke suite nobody can run locally stops catching anything.
          executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH || undefined,
        },
      },
    },
  ],
  webServer: {
    command: 'npm run build && npm run preview',
    url: 'http://localhost:5174',
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
