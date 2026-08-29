---
name: run-k8boss-admin
description: Build, run, and drive k8boss-admin (the FastAPI backend + React/PatternFly SPA). Use when asked to start k8boss-admin, run its backend or frontend dev servers, take a screenshot of the console, register a cluster, or confirm a change works in the real running app (not just pytest/Playwright unit runs).
---

k8boss-admin is a FastAPI backend (`backend/`) plus a Vite/React SPA
(`frontend/`), driven here with a small headless-Chromium REPL —
`.claude/skills/run-k8boss-admin/driver.mjs` — because `chromium-cli` is not
installed in this environment. Same idea as `chromium-cli`: pipe commands to
its stdin, screenshots land on disk, `console --errors` checks nothing threw.

All paths below are relative to the repo root (the directory with `backend/`
and `frontend/`), except where a command says to `cd` first.

## Prerequisites

Nothing to `apt-get` — Chromium is pre-installed in this environment at
`/opt/pw-browsers/chromium-1194/chrome-linux/chrome`
(`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`). Python 3.11+ and Node 20+ are
assumed present (verified here on Python 3.11.15 / Node 22.22.2).

## Setup

```bash
# Backend: a venv with the pinned requirements
cd backend
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# Frontend: npm ci matches package-lock.json exactly, which matters here —
# see Gotchas re: @playwright/test vs. the pre-installed Chromium revision.
cd ../frontend
npm ci
```

No environment variables are required for a read-only local run:
`DATABASE_URL` defaults to a local SQLite file, `ENCRYPTION_KEY` defaults to
a key persisted next to it, and `ADMIN_ALLOW_MUTATIONS` defaults to `false`
(read-only, dry-run still works). See `backend/app/config.py` for the full
set if you need to turn on auth or mutations for a specific check.

## Build

No separate frontend build step for local dev (Vite serves `src/` directly).
`npm run build` is only needed for the production bundle / Playwright e2e
suite (below) — verified here, `dist/` comes out clean.

## Run (agent path)

Launch both servers, then drive the SPA with the REPL. Everything here was
run end-to-end in this container.

```bash
# 1. Backend on :8020 (from backend/, with the venv above)
cd backend
nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8020 \
  --log-level info > /tmp/k8boss-backend.log 2>&1 &
disown
timeout 20 bash -c 'until curl -sf http://localhost:8020/api/health >/dev/null; do sleep 1; done' \
  && echo "backend ready" || { echo "backend did not come up -- check /tmp/k8boss-backend.log"; exit 1; }

# 2. Frontend dev server on :5174, proxying /api to :8020 (from frontend/)
cd ../frontend
nohup npm run dev > /tmp/k8boss-frontend.log 2>&1 &
disown
timeout 30 bash -c 'until curl -sf http://localhost:5174 >/dev/null; do sleep 1; done' \
  && echo "frontend ready" || { echo "frontend did not come up -- check /tmp/k8boss-frontend.log"; exit 1; }

# 3. Drive it. cd frontend/ first -- the driver resolves @playwright/test
#    from the caller's cwd (see Gotchas), so it must run from there.
cd frontend
node ../.claude/skills/run-k8boss-admin/driver.mjs <<'EOF'
nav http://localhost:5174/
wait-for text=Overview
screenshot 01-home
click text=Register or select a cluster
wait-for text=No clusters registered
click button:has-text("Register a cluster")
wait-for #cluster-name
fill #cluster-name smoke-test-cluster
fill #cluster-api-server https://127.0.0.1:6443
fill #cluster-token dummy-token-for-smoke-test
click div.pf-v6-c-modal-box button:has-text("Register")
sleep 1500
screenshot 02-registered
console --errors
quit
EOF
```

Stop the servers by killing the port listeners (not `pkill -f`, which can
match the agent's own command line):

```bash
lsof -ti:8020 -sTCP:LISTEN | xargs -r kill
lsof -ti:5174 -sTCP:LISTEN | xargs -r kill
```

Screenshots land in `/tmp/k8boss-admin-shots/` (override with
`DRIVER_SHOT_DIR`). The REPL commands:

| command | what it does |
|---|---|
| `nav <url>` | Navigate, waits for network idle |
| `wait-for <sel>` | Wait for a selector; `text=Foo` is shorthand for a text match |
| `click <sel>` | Click (same `text=` shorthand) |
| `fill <sel> <value>` | Fill a form field — goes through Playwright's input pipeline, so React's `onChange` fires |
| `press <sel> <key>` | Press a key on a locator |
| `screenshot [name]` | Full-page PNG to `$DRIVER_SHOT_DIR/<name>.png` |
| `text [sel]` | Print a selector's collapsed text content (default `body`) — useful for asserting without a screenshot |
| `console --errors` | Print collected `console.error`/`pageerror` messages since launch |
| `sleep <ms>` | Fixed wait, for a toast/animation `wait-for` can't target |
| `quit` / `exit` | Close the browser and exit |

Registering a cluster against a real API server works the same way — swap
the endpoint/token for a real one and the connection test, permission
matrix, and overview populate instead of showing `cluster_unreachable`.

## Run (human path)

```bash
make dev-backend    # :8020, reload on, mutations off
make dev-frontend   # :5174, proxies /api to :8020
```

Open `http://localhost:5174`. Useless headless; only for a machine with a
display. Ctrl-C each to stop.

## Test

```bash
cd backend && .venv/bin/python -m pytest -q          # 1121 passed, 1 skipped here
cd frontend && npm run build && npm run lint          # build clean; lint 0 errors (14 pre-existing warnings)

# Stop anything already listening on :5174 first -- see Gotchas.
lsof -ti:5174 -sTCP:LISTEN | xargs -r kill
cd frontend && PLAYWRIGHT_CHROMIUM_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  npx playwright test          # 172 passed here, ~2.4 minutes
```

The e2e suite builds and serves its own production bundle via `vite
preview` and mocks every `/api/**` call — it needs neither the backend nor
frontend dev servers above, and no cluster.

## Gotchas

- **`@playwright/test` vs. the pre-installed Chromium revision.**
  `package-lock.json` pins `@playwright/test@1.62.1`, which expects Chromium
  revision `1234`; this container only has revision `1194` pre-installed
  (`/opt/pw-browsers/chromium-1194/`), and `npx playwright install` can't
  reach `cdn.playwright.dev` through the proxy to fetch the matching one.
  `playwright.config.js` already has an escape hatch for exactly this:
  `launchOptions.executablePath` reads `PLAYWRIGHT_CHROMIUM_PATH`. Set it
  when running `npx playwright test` (as above) rather than downgrading
  `@playwright/test` — a downgrade drifts from what CI actually runs.
  `driver.mjs` reads the same env var, defaulting to the revision-1194
  binary, so both paths agree without extra configuration.
- **The driver's `import`s must resolve from `frontend/`, not from the
  skill directory.** Node's ESM resolver looks up `node_modules` relative to
  the *importing file's* location, so a bare `import '@playwright/test'`
  inside `driver.mjs` (which lives under `.claude/skills/`) fails outright.
  The driver instead resolves the package from `process.cwd()` via the CJS
  `createRequire` algorithm (which does walk up from cwd) — hence "run it
  from `frontend/`" above, not a path issue you can route around with a
  different cwd.
- **That resolved path is the CJS entry (`index.js`), and dynamically
  importing it directly loses the named exports.** `require.resolve` follows
  the package's `"require"` export condition; importing that file gives
  Node's CJS/ESM interop no static list to find `chromium` from, and it
  silently comes back `undefined` (`TypeError: Cannot read properties of
  undefined (reading 'launch')`). The driver resolves the package's own
  `index.mjs` sibling instead — see the comment in `driver.mjs`.
- **`npx playwright test` silently reuses a dev server left running on
  :5174.** `playwright.config.js` sets `webServer.reuseExistingServer:
  !process.env.CI`, so outside CI it happily attaches to whatever answers on
  that port instead of running its own `npm run build && npm run preview` —
  including the plain Vite dev server from "Run (agent path)" above. Every
  test then fails in ~5s (page renders, but the production-only
  chunk-splitting and asset paths the suite exercises aren't there). Kill
  the port before running the suite, as above.
- **Clicking a "Register or select a cluster" link only opens the Clusters
  *page*, not the registration modal.** The modal needs a second click on
  the page's own "Register a cluster" button (`#cluster-name` etc. only
  exist after that).
- **A cluster registered with an unreachable endpoint is not a broken
  test** — the app records it (`Active` registration, `Unknown` status) and
  every dependent read fails as a named, per-source `unreachable` entry
  rather than an empty page (contract rule "empty is never blind": see
  `CLAUDE.md`). The 502s you'll see in `console --errors` after registering
  a bogus cluster are `cluster_unreachable`, and they're the point.

## Troubleshooting

- **`ERR_MODULE_NOT_FOUND: Cannot find package '@playwright/test'`** running
  the driver: you ran it with cwd somewhere other than `frontend/`. `cd
  frontend` first.
- **`browserType.launch: Executable doesn't exist at
  .../chromium_headless_shell-1234/...`** running `npx playwright test`:
  see the Chromium-revision Gotcha above — set `PLAYWRIGHT_CHROMIUM_PATH`.
- **Backend starts but `/api/health` reports `clusters.registered: 0`
  forever**: expected with no `~/.kube/config` mounted and nothing
  registered yet — this container has neither. Register one through the UI
  (Run (agent path), above) or `POST /api/clusters`.
