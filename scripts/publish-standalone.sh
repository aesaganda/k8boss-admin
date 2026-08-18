#!/usr/bin/env bash
#
# Publish k8boss-admin as a standalone repository.
#
# k8boss-admin is developed inside the k8boss monorepo (so the work has a review
# trail on a branch and a PR) but it is a *separate product* and ships as its own
# repository — the same split the operator went through, for the same reason:
# k8boss carries the commercial licensing module and cannot be opened, while this
# console has no reason to be closed.
#
# This script does the split. It copies the tree into a scratch directory, makes a
# real git repository whose root is k8boss-admin/ (not k8boss/k8boss-admin/), and
# pushes it. History is deliberately NOT carried over: the monorepo history is
# k8boss's, it mentions subsystems this repo does not contain, and grafting it onto
# a fresh product would make `git log` a description of something else.
#
# Usage:
#   scripts/publish-standalone.sh <git-remote-url> [branch]
#
#   scripts/publish-standalone.sh git@github.com:aesaganda/k8boss-admin.git
#   scripts/publish-standalone.sh https://github.com/aesaganda/k8boss-admin.git main
#
# The remote repository must already exist. Create it first:
#   gh repo create aesaganda/k8boss-admin --private \
#     --description "Kubernetes administration console"
#
# It does NOT need to be empty. A repository created through the GitHub UI with
# the "Add a README" box ticked already has a commit, and pushing a fresh history
# onto it is rejected as a non-fast-forward. So the script fetches whatever is
# there and commits on top of it when it exists, rather than assuming a virgin
# remote and failing at the last step — which is the point at which failing is
# most annoying, because the tree is already staged.
#
set -euo pipefail

REMOTE="${1:-}"
BRANCH="${2:-main}"

if [[ -z "$REMOTE" ]]; then
  echo "usage: $0 <git-remote-url> [branch]" >&2
  exit 2
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$SRC/docs/api-contract.md" ]]; then
  echo "error: $SRC does not look like the k8boss-admin tree" >&2
  exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> staging $SRC -> $STAGE"
# --exclude rather than a clean checkout: the point is to publish the tree as it
# stands, minus the artifacts that should never have been in it. Anything listed
# here is also in .gitignore; the duplication is intentional, because a stale
# .gitignore must not be able to leak a virtualenv into a published repository.
tar -C "$SRC" -cf - \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='dist' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='*.db' \
  --exclude='*.key' \
  --exclude='.env' \
  --exclude='playwright-report' \
  --exclude='test-results' \
  . | tar -C "$STAGE" -xf -

cd "$STAGE"

# Fail loudly rather than publish a credential. This is cheap and the failure it
# prevents is not recoverable — a pushed secret is a leaked secret even after a
# force-push, which is why the check runs before `git init` and not after.
#
# Two checks, because the first one alone gave false confidence. A PEM header is
# easy to grep for, but the backend's at-rest key is a bare Fernet key written to
# k8boss_admin.db.key — no header, no marker, nothing a content grep would catch.
# The extension exclusion above should already have dropped it; this asserts that
# it did, so a future edit to the exclude list cannot quietly re-open the hole.
if grep -rIl --exclude-dir=.git -E 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' . >/dev/null 2>&1; then
  echo "error: a private key is present in the tree; refusing to publish" >&2
  grep -rIl --exclude-dir=.git -E 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' . >&2
  exit 1
fi

if find . -name '*.key' -o -name '.env' -o -name '*.db' | grep -q .; then
  echo "error: credential or database artifacts survived staging; refusing to publish" >&2
  find . -name '*.key' -o -name '.env' -o -name '*.db' >&2
  exit 1
fi

git init -q -b "$BRANCH"
git remote add origin "$REMOTE"

# Adopt the remote's existing history if it has any, so a repository created with
# a starter README is a valid target instead of a rejected push.
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "==> remote branch $BRANCH exists; committing on top of it"
  git fetch -q --depth 1 origin "$BRANCH"
  # --soft: keep the staged tree exactly as tarred, only move HEAD onto the
  # remote commit. A hard reset here would restore the remote's files over ours.
  git reset -q --soft FETCH_HEAD
fi

git add -A
git -c user.name="${GIT_AUTHOR_NAME:-$(git config --global user.name || echo k8boss-admin)}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-$(git config --global user.email || echo noreply@example.com)}" \
    commit -q -m "k8boss-admin: initial import

A Kubernetes administration console: browse every resource a cluster serves,
and change it through a path that preflights the permission, dry-runs the
write, shows the operator the diff, and records what happened.

Split out of the k8boss monorepo, where it was developed. See
docs/adr-0002-lineage.md for what was carried over and what was not."

echo "==> pushing to $REMOTE ($BRANCH)"
git push -u origin "$BRANCH"

echo "==> published $(git rev-list --count HEAD) commit, $(git ls-files | wc -l | tr -d ' ') files"
