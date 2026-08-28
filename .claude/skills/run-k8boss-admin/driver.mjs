#!/usr/bin/env node
/**
 * Headless-Chromium driver for k8boss-admin, in place of `chromium-cli`
 * (not installed in this environment). Same idea: a tiny stdin REPL you
 * pipe commands into, screenshots land on disk, `console --errors` checks
 * nothing threw.
 *
 * Must be run with cwd = <repo>/frontend, so `@playwright/test` (already a
 * devDependency there) resolves. See SKILL.md for the launch sequence.
 *
 * Usage:
 *   cd frontend && node ../.claude/skills/run-k8boss-admin/driver.mjs <<'EOF'
 *   nav http://localhost:5174/
 *   wait-for text=Overview
 *   screenshot 01-home
 *   click text=Register or select a cluster
 *   screenshot 02-modal
 *   console --errors
 *   EOF
 */
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

// Node's ESM resolver looks up node_modules relative to *this file's*
// location, not the process cwd -- so a bare `import '@playwright/test'`
// fails when the driver lives under .claude/skills/ instead of frontend/.
// Resolve it from cwd via the CJS algorithm instead, which does walk up
// from cwd. This is why the driver must be launched with cwd = frontend/.
const require = createRequire(path.join(process.cwd(), 'package.json'));
// require.resolve('@playwright/test') follows the "require" export
// condition (index.js, CJS) -- dynamically importing *that* file gives
// Node's CJS/ESM interop no static export list to find, and `chromium`
// silently comes back undefined. Load the package's own index.mjs instead.
const pkgDir = path.dirname(require.resolve('@playwright/test/package.json'));
const { chromium } = await import(pathToFileURL(path.join(pkgDir, 'index.mjs')).href);

const SHOT_DIR = process.env.DRIVER_SHOT_DIR || '/tmp/k8boss-admin-shots';
// PLAYWRIGHT_CHROMIUM_PATH is the project's own name for this override (see
// playwright.config.js) -- reusing it means one env var points both the real
// e2e suite and this driver at the same pre-installed binary.
const CHROME_PATH =
  process.env.PLAYWRIGHT_CHROMIUM_PATH || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
fs.mkdirSync(SHOT_DIR, { recursive: true });

const browser = await chromium.launch({ args: ['--no-sandbox'], executablePath: CHROME_PATH });
const page = await browser.newPage();

const consoleMsgs = [];
page.on('console', (msg) => consoleMsgs.push({ type: msg.type(), text: msg.text() }));
page.on('pageerror', (err) => consoleMsgs.push({ type: 'pageerror', text: String(err) }));

function log(...args) {
  console.log(...args);
}

async function run(line) {
  const raw = line.trim();
  if (!raw || raw.startsWith('#')) return;
  const [cmd, ...rest] = raw.split(/\s+/);
  const arg = rest.join(' ');

  switch (cmd) {
    case 'nav': {
      await page.goto(arg, { waitUntil: 'networkidle', timeout: 30000 });
      log('nav ->', page.url());
      break;
    }
    case 'wait-for': {
      // arg forms: "text=Foo" or a raw CSS/role selector
      const sel = arg.startsWith('text=') ? `text=${arg.slice(5)}` : arg;
      await page.waitForSelector(sel, { timeout: 15000 });
      log('wait-for ok:', arg);
      break;
    }
    case 'click': {
      const sel = arg.startsWith('text=') ? `text=${arg.slice(5)}` : arg;
      await page.click(sel, { timeout: 15000 });
      log('click ok:', arg);
      break;
    }
    case 'fill': {
      const [sel, ...valueParts] = rest;
      await page.fill(sel, valueParts.join(' '), { timeout: 15000 });
      log('fill ok:', sel);
      break;
    }
    case 'press': {
      const [sel, key] = rest;
      await page.press(sel, key, { timeout: 15000 });
      log('press ok:', sel, key);
      break;
    }
    case 'screenshot': {
      const name = arg || `shot-${Date.now()}`;
      const path = `${SHOT_DIR}/${name}.png`;
      await page.screenshot({ path, fullPage: true });
      log('screenshot ->', path);
      break;
    }
    case 'text': {
      const sel = arg || 'body';
      const t = await page.textContent(sel);
      log('text:', (t || '').replace(/\s+/g, ' ').trim().slice(0, 800));
      break;
    }
    case 'console': {
      if (rest.includes('--errors')) {
        const errs = consoleMsgs.filter((m) => m.type === 'error' || m.type === 'pageerror');
        log('console errors:', JSON.stringify(errs));
      } else {
        log('console:', JSON.stringify(consoleMsgs));
      }
      break;
    }
    case 'sleep': {
      await page.waitForTimeout(Number(arg) || 1000);
      break;
    }
    case 'quit':
    case 'exit': {
      await browser.close();
      process.exit(0);
      break;
    }
    default:
      log('unknown command:', cmd);
  }
}

const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  try {
    await run(line);
  } catch (err) {
    log('ERROR running', JSON.stringify(line), '->', String(err));
  }
}
await browser.close();
