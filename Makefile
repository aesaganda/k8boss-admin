# k8boss-admin — the commands you actually run.
#
# Every target below is a one-liner you could type by hand; the Makefile exists
# so that the *flags* are not a thing anyone has to remember, and so CI and a
# laptop run the identical command. Where CI differs from this file, CI is the
# defect.
#
# Ports are 8020 (backend) and 5174 (frontend dev server), not 8000/5173. k8boss
# — the product this console was split out of — owns 8010 and 5173, and the two
# are routinely run side by side. Sharing a port meant the second `npm run dev`
# silently took the next free one and every bookmark then hit whichever app had
# started first.

PYTHON  ?= python3
BACKEND  = backend
FRONTEND = frontend
API_PORT ?= 8020

# No default target that "does something" — a bare `make` prints the menu.
.DEFAULT_GOAL := help

.PHONY: help install dev-backend dev-frontend test test-backend test-frontend \
        lint build docker-build clean router-manifest

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── Setup ────────────────────────────────────────────────────────────────────

install:  ## Install backend and frontend dependencies (run this first)
	@# Every other target below assumes this has been run. Without it the first
	@# thing a new contributor sees is `No module named pytest` from
	@# `make test-backend`, which reads as a broken repository rather than as a
	@# missing step — the repository never told them there was a step.
	@#
	@# No venv is created here. Whether to isolate is the contributor's call and
	@# their tooling already has an opinion (venv, uv, conda, pyenv, a container);
	@# a Makefile that created one would install into a directory the next
	@# `python3 -m pytest` does not look in, which is a worse failure than this
	@# one because it looks like it worked. Activate an environment first if you
	@# want one — PYTHON=path/to/python make install also works.
	$(PYTHON) -m pip install -r $(BACKEND)/requirements.txt
	@# `ci`, not `install`: it installs exactly package-lock.json and fails if the
	@# lockfile and package.json disagree, which is what CI does. The Playwright
	@# browsers arrive with it through @playwright/test's install script, so
	@# `make test-frontend` needs nothing further.
	cd $(FRONTEND) && npm ci

# ── Development ──────────────────────────────────────────────────────────────

dev-backend:  ## Run the API on :8020 with reload (SQLite, mutations off)
	@# ADMIN_ALLOW_MUTATIONS is NOT set here. The default is read-only and this
	@# target does not quietly override it: a developer who wants to exercise a
	@# write path turns it on deliberately for that session
	@# (`ADMIN_ALLOW_MUTATIONS=true make dev-backend`), which is the same act an
	@# operator performs in production. A convenience default here would mean the
	@# write paths are only ever tested with the gate open.
	cd $(BACKEND) && $(PYTHON) -m uvicorn app.main:app \
	  --host 0.0.0.0 --port $(API_PORT) --reload --log-level info

dev-frontend:  ## Run the SPA dev server on :5174, proxying /api to :8020
	cd $(FRONTEND) && npm run dev

# ── Tests ────────────────────────────────────────────────────────────────────

test: test-backend test-frontend  ## Everything: backend suite, frontend build+lint, e2e

test-backend:  ## pytest against SQLite (the whole backend suite)
	cd $(BACKEND) && $(PYTHON) -m pytest -q

test-frontend:  ## Playwright end-to-end suite (hermetic, route-mocked)
	@# The suite intercepts every /api/** request, so it needs no backend and no
	@# cluster. playwright.config.js builds and serves the app itself.
	cd $(FRONTEND) && npx playwright test

# ── Static checks and build ──────────────────────────────────────────────────

lint:  ## ESLint over the frontend (non-zero exit on any error)
	@# Not `|| true`, and CI does not mark this continue-on-error either. k8boss
	@# ran its frontend lint advisory, which meant a growing pile of real errors
	@# that no one saw because the job was green. See .github/workflows/ci.yml.
	cd $(FRONTEND) && npm run lint

build:  ## Production bundle into frontend/dist
	cd $(FRONTEND) && npm run build

router-manifest:  ## Regenerate deploy/router.yaml from the shipped bundle (§14)
	@# The checked-in manifest and the objects the console installs are the same
	@# bundle, and a test fails the build when they disagree. Run this after
	@# changing ROUTER_VERSION or anything in app/admin/router_bundle.py.
	$(PYTHON) scripts/render-router-manifest.py

docker-build:  ## Build both images through docker compose
	docker compose build

clean:  ## Remove build output and test/tool caches (never the database)
	@# Deliberately does not touch *.db or *.key. Deleting the database drops
	@# every registered cluster and the audit trail, and an audit trail removed by
	@# a cleanup target is an audit trail nobody can rely on.
	rm -rf $(FRONTEND)/dist $(FRONTEND)/playwright-report $(FRONTEND)/test-results
	find $(BACKEND) -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf $(BACKEND)/.pytest_cache
