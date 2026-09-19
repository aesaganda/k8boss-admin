#!/usr/bin/env bash
#
# Onboard one cluster into a running k8boss-admin console, end to end.
#
# This is every step under "Register a cluster" / "Turning on writes" in
# README.md, run in order, against one explicitly named kubectl context. It
# exists because that walkthrough is five separate commands plus a login step
# the README does not mention at all (see below), and every one of them has a
# way to point at the wrong cluster or the wrong console if a person is doing
# it by hand at the end of a long day. An onboarding script's whole job is to
# remove exactly that kind of mistake — the target this script was written
# against was made by the author of this repository, twice, in one session,
# by trusting `kubectl`'s ambient current-context instead of naming one.
#
# So the one rule this script never breaks: every `kubectl` call below names
# --context explicitly. Nothing here ever reads or writes the ambient
# current-context, and this script does not change it either.
#
# WHAT THIS SCRIPT ACTUALLY DOES, IN ORDER:
#
#   1. Applies deploy/namespace.yaml and deploy/rbac.yaml to the target
#      cluster. As deploy/rbac.yaml stands today, that binds BOTH the
#      read-only role and the write role (wildcard, "cluster-admin wearing a
#      different name" — read that file's own top comment before running
#      this against anything that matters). This script does not add a
#      read-only mode: the file already made that call, on the record, and an
#      onboarding script second-guessing it quietly would be worse than the
#      file being wrong loudly. If you want read-only, edit deploy/rbac.yaml
#      yourself and re-run.
#   2. Mints a ServiceAccount token via the TokenRequest API
#      (`kubectl create token`) — never a long-lived Secret, never printed to
#      the terminal, never logged.
#   3. Logs into the console if it requires a session (checked via the public
#      `/api/auth/config`, so this script works unmodified whether or not
#      AUTH_ENABLED is set) — README's own `curl -X POST .../clusters`
#      example predates AUTH_ENABLED and 401s against a real deployment,
#      because /api/clusters sits behind AuthenticationMiddleware like
#      everything else under /api. This script logs in first and carries the
#      session cookie *and* the X-CSRF-Token the middleware requires on every
#      write, exactly like the SPA does.
#   4. Registers the cluster (POST /api/clusters), or updates it in place
#      (PUT) if a cluster with the same name is already registered — so
#      running this twice for the same cluster is a no-op plus a token
#      rotation, not a 409.
#   5. Runs the connection test (POST /api/clusters/{id}/test) and prints
#      exactly which of the seventeen baseline permissions are missing, if
#      any — the whole point of that endpoint per its own docstring: a
#      half-permissioned ServiceAccount should show up now, not at the first
#      click on a page that turns out not to work.
#
# NOT FOR A LOCAL CLUSTER. §34 onboards kind, k3d, minikube, Docker Desktop
# and the rest with nothing typed at all: the console finds them in your
# kubeconfig and registers them on first start, or offers them under
# Clusters -> Discovered on this machine. Running this script against one
# applies a ClusterRoleBinding to a throwaway cluster to mint a token for a
# credential path that cluster already has. Use it for the clusters it was
# written for — the remote ones, where a scoped, rotatable ServiceAccount
# token is exactly what you want and a kubeconfig's `exec` plugin is not
# something this console will run. See README's "Connecting a cluster".
#
# USAGE
#
#   scripts/onboard-cluster.sh --context <kubectl-context> [options]
#
#   scripts/onboard-cluster.sh --context kind-prod-eu --name prod-eu
#
# OPTIONS
#
#   --context NAME       kubectl context to onboard. Required — there is no
#                         default, on purpose (see above).
#   --name NAME           Display name for this cluster in the console.
#                         Defaults to the context name.
#   --console-url URL     Console base URL. Default: http://localhost:8020
#   --duration DURATION   TokenRequest duration, Go duration syntax. Default:
#                         8760h (one year), same as README's own example.
#   --skip-tls-verify     Register without a CA certificate, accepting
#                         whatever the API server presents. Use only when the
#                         context's kubeconfig has no extractable CA (neither
#                         embedded data nor a readable file path) and you
#                         have another way to know you are talking to the
#                         right server.
#   -y, --yes             Skip the confirmation prompt.
#   -h, --help            This text.
#
# ENVIRONMENT (only read when the console requires a session)
#
#   K8BOSS_ADMIN_CONSOLE_USER       Console username. Prompted for if unset.
#   K8BOSS_ADMIN_CONSOLE_PASSWORD   Console password. Prompted for (hidden)
#                                   if unset — set this for non-interactive
#                                   use, e.g. from a secrets manager, never
#                                   typed into a shell history.
#
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────── #
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────── #

CONTEXT=""
NAME=""
CONSOLE_URL="http://localhost:8020"
DURATION="8760h"
SKIP_TLS_VERIFY="false"
ASSUME_YES="false"

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \?//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CONTEXT="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --console-url) CONSOLE_URL="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-}"; shift 2 ;;
    --skip-tls-verify) SKIP_TLS_VERIFY="true"; shift ;;
    -y|--yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unrecognized argument: $1" >&2; echo "run with --help for usage" >&2; exit 2 ;;
  esac
done

if [[ -z "$CONTEXT" ]]; then
  echo "error: --context is required — there is no ambient default (see this" >&2
  echo "       script's own header for why)." >&2
  echo "       Available contexts:" >&2
  kubectl config get-contexts -o name 2>/dev/null | sed 's/^/         /' >&2 || true
  exit 2
fi
NAME="${NAME:-$CONTEXT}"

# ─────────────────────────────────────────────────────────────────────────── #
# Preflight: tools, repo location, the context itself
# ─────────────────────────────────────────────────────────────────────────── #

for tool in kubectl curl jq base64; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "error: '$tool' is required and was not found on PATH." >&2
    exit 1
  }
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RBAC_FILE="$REPO_ROOT/deploy/rbac.yaml"
NAMESPACE_FILE="$REPO_ROOT/deploy/namespace.yaml"
for f in "$RBAC_FILE" "$NAMESPACE_FILE"; do
  [[ -f "$f" ]] || { echo "error: $f not found — is this script running from inside the repo?" >&2; exit 1; }
done

if ! kubectl config get-contexts -o name 2>/dev/null | grep -qxF "$CONTEXT"; then
  echo "error: no kubectl context named '$CONTEXT'." >&2
  echo "       Available contexts:" >&2
  kubectl config get-contexts -o name 2>/dev/null | sed 's/^/         /' >&2 || true
  exit 1
fi

API_SERVER="$(kubectl config view --minify --raw --context="$CONTEXT" -o jsonpath='{.clusters[0].cluster.server}')"
if [[ -z "$API_SERVER" ]]; then
  echo "error: could not resolve an API server address for context '$CONTEXT'." >&2
  exit 1
fi

# The CA can be embedded (most kubeconfigs) or a file path (common for
# cluster-provisioning tools that keep the CA on disk next to the config). A
# script that only handled the embedded case would work everywhere the author
# tested it and nowhere else, which is worse than not automating this at all.
CA_PEM=""
if [[ "$SKIP_TLS_VERIFY" != "true" ]]; then
  CA_DATA="$(kubectl config view --minify --raw --context="$CONTEXT" -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
  if [[ -n "$CA_DATA" ]]; then
    CA_PEM="$(echo "$CA_DATA" | base64 -d)"
  else
    CA_FILE="$(kubectl config view --minify --raw --context="$CONTEXT" -o jsonpath='{.clusters[0].cluster.certificate-authority}')"
    if [[ -n "$CA_FILE" && -f "$CA_FILE" ]]; then
      CA_PEM="$(cat "$CA_FILE")"
    else
      echo "error: context '$CONTEXT' has no embedded CA data and no readable CA file." >&2
      echo "       Re-run with --skip-tls-verify if you have another way to trust this" >&2
      echo "       server, or register the cluster by hand with the correct CA." >&2
      exit 1
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────── #
# Confirm the target before touching anything
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Onboarding plan:"
echo "      kubectl context : $CONTEXT"
echo "      API server      : $API_SERVER"
echo "      TLS verification: $([[ "$SKIP_TLS_VERIFY" == "true" ]] && echo "disabled (--skip-tls-verify)" || echo "enabled, CA extracted from kubeconfig")"
echo "      console name    : $NAME"
echo "      console URL     : $CONSOLE_URL"
echo "      token duration  : $DURATION"
echo "      deploy/rbac.yaml as committed grants read AND write (wildcard) RBAC —"
echo "      see that file's top comment. This script applies it as-is."

if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

# ─────────────────────────────────────────────────────────────────────────── #
# 1. RBAC, on the named context only
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Applying $NAMESPACE_FILE to $CONTEXT"
kubectl --context="$CONTEXT" apply -f "$NAMESPACE_FILE"

echo "==> Applying $RBAC_FILE to $CONTEXT"
kubectl --context="$CONTEXT" apply -f "$RBAC_FILE"

# ─────────────────────────────────────────────────────────────────────────── #
# 2. Mint the token — captured, never printed
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Minting a ${DURATION} token for k8boss-admin/k8boss-admin on $CONTEXT"
TOKEN="$(kubectl --context="$CONTEXT" -n k8boss-admin create token k8boss-admin --duration="$DURATION")"

# ─────────────────────────────────────────────────────────────────────────── #
# Console HTTP helpers
# ─────────────────────────────────────────────────────────────────────────── #

COOKIE_JAR="$(mktemp)"
CSRF_TOKEN=""
cleanup() {
  rm -f "$COOKIE_JAR"
  unset TOKEN CSRF_TOKEN K8BOSS_ADMIN_CONSOLE_PASSWORD
}
trap cleanup EXIT

# One call, body and status both captured, without assuming anything about
# where a literal newline can appear in the body — a sentinel line is safer
# than "the status is whatever is after the last newline" once the body is
# JSON with any possibility of pretty-printing.
# Usage: api_call METHOD PATH [JSON_BODY]  -> sets API_STATUS, API_BODY
api_call() {
  local method="$1" path="$2" body="${3:-}"
  local -a curl_args=(-sS -c "$COOKIE_JAR" -b "$COOKIE_JAR" -X "$method"
                       -w '\n__HTTP_STATUS__:%{http_code}')
  if [[ -n "$body" ]]; then
    curl_args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  if [[ -n "$CSRF_TOKEN" ]]; then
    curl_args+=(-H "X-CSRF-Token: $CSRF_TOKEN")
  fi
  local raw
  raw="$(curl "${curl_args[@]}" "$CONSOLE_URL$path")"
  API_STATUS="$(echo "$raw" | grep -o '__HTTP_STATUS__:[0-9]*$' | cut -d: -f2)"
  API_BODY="$(echo "$raw" | sed '$ d')"
}

# ─────────────────────────────────────────────────────────────────────────── #
# 3. Console session, only if this deployment requires one
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Checking whether $CONSOLE_URL requires a session"
api_call GET /api/auth/config
if [[ "$API_STATUS" != "200" ]]; then
  echo "error: could not reach $CONSOLE_URL/api/auth/config (HTTP ${API_STATUS:-000})." >&2
  echo "       Is the console running and reachable at --console-url?" >&2
  exit 1
fi
AUTH_ENABLED="$(echo "$API_BODY" | jq -r '.enabled')"

if [[ "$AUTH_ENABLED" == "true" ]]; then
  CONSOLE_USER="${K8BOSS_ADMIN_CONSOLE_USER:-}"
  if [[ -z "$CONSOLE_USER" ]]; then
    read -r -p "Console username: " CONSOLE_USER
  fi
  CONSOLE_PASSWORD="${K8BOSS_ADMIN_CONSOLE_PASSWORD:-}"
  if [[ -z "$CONSOLE_PASSWORD" ]]; then
    read -r -s -p "Console password: " CONSOLE_PASSWORD
    echo
  fi

  echo "==> Signing in to $CONSOLE_URL as $CONSOLE_USER"
  LOGIN_BODY="$(jq -n --arg u "$CONSOLE_USER" --arg p "$CONSOLE_PASSWORD" \
    '{username: $u, password: $p, source: "auto"}')"
  unset CONSOLE_PASSWORD
  api_call POST /api/auth/login "$LOGIN_BODY"
  if [[ "$API_STATUS" != "200" ]]; then
    echo "error: console sign-in failed (HTTP ${API_STATUS:-000})." >&2
    echo "$API_BODY" | jq -r '.message // .' >&2 2>/dev/null || echo "$API_BODY" >&2
    exit 1
  fi
  CSRF_TOKEN="$(echo "$API_BODY" | jq -r '.csrfToken')"
else
  echo "==> $CONSOLE_URL runs without console authentication — no session needed"
fi

# ─────────────────────────────────────────────────────────────────────────── #
# 4. Register or update the cluster
# ─────────────────────────────────────────────────────────────────────────── #

REGISTER_BODY="$(jq -n \
  --arg name "$NAME" \
  --arg server "$API_SERVER" \
  --arg token "$TOKEN" \
  --arg ca "$CA_PEM" \
  --argjson skip_tls "$SKIP_TLS_VERIFY" \
  '{name: $name, platform: "kubernetes", api_server: $server,
    authentication_type: "service_account_token", token: $token,
    ca_certificate: (if $ca == "" then null else $ca end),
    skip_tls_verify: $skip_tls}')"

echo "==> Registering '$NAME' with the console"
api_call POST /api/clusters "$REGISTER_BODY"

if [[ "$API_STATUS" == "201" ]]; then
  CLUSTER_ID="$(echo "$API_BODY" | jq -r '.id')"
  echo "==> Registered as cluster id $CLUSTER_ID"
elif [[ "$API_STATUS" == "409" ]]; then
  echo "==> '$NAME' is already registered — updating its credentials instead"
  api_call GET /api/clusters
  CLUSTER_ID="$(echo "$API_BODY" | jq -r --arg name "$NAME" '.items[] | select(.name == $name) | .id')"
  if [[ -z "$CLUSTER_ID" ]]; then
    echo "error: the console reported '$NAME' as a conflict but it is not in the" >&2
    echo "       current listing — someone may be renaming clusters concurrently." >&2
    exit 1
  fi
  api_call PUT "/api/clusters/$CLUSTER_ID" "$REGISTER_BODY"
  if [[ "$API_STATUS" != "200" ]]; then
    echo "error: updating cluster id $CLUSTER_ID failed (HTTP ${API_STATUS:-000})." >&2
    echo "$API_BODY" >&2
    exit 1
  fi
  echo "==> Updated cluster id $CLUSTER_ID"
else
  echo "error: registering '$NAME' failed (HTTP ${API_STATUS:-000})." >&2
  echo "$API_BODY" | jq -r '.message // .' >&2 2>/dev/null || echo "$API_BODY" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────── #
# 5. Test it, and say exactly what is missing if anything is
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Testing the connection and the baseline permission set"
api_call POST "/api/clusters/$CLUSTER_ID/test"
if [[ "$API_STATUS" != "200" ]]; then
  echo "error: the test call itself failed (HTTP ${API_STATUS:-000}), which is" >&2
  echo "       different from the cluster failing the test — see the console." >&2
  exit 1
fi

REACHABLE="$(echo "$API_BODY" | jq -r '.reachable')"
if [[ "$REACHABLE" != "true" ]]; then
  echo "!! Cluster is registered but NOT reachable:"
  echo "$API_BODY" | jq -r '.error.message // "no error message returned"' | sed 's/^/     /'
  echo "   Fix connectivity/credentials, then re-run:"
  echo "     curl -sS -b \"<your console session>\" -X POST $CONSOLE_URL/api/clusters/$CLUSTER_ID/test"
  exit 1
fi

SERVER_VERSION="$(echo "$API_BODY" | jq -r '.server_version // "unknown"')"
echo "==> Reachable. Server version: $SERVER_VERSION"

DENIED="$(echo "$API_BODY" | jq -r '
  [.permissions[] | select(.allowed == false)] as $d
  | if ($d | length) == 0 then empty
    else $d[] | "     - " + .verb + " " + (.group // "core") + "/" + .resource
         + (if .subresource then "/" + .subresource else "" end)
         + ": " + (.hint // .evaluationError // "denied")
    end')"

if [[ -z "$DENIED" ]]; then
  echo "==> All baseline permissions are held. Onboarding complete."
else
  echo "!! Reachable, but missing some baseline permissions — the console will"
  echo "   still work, with the pages/actions these cover degraded or disabled:"
  echo "$DENIED"
  echo "   Add the missing grants to deploy/rbac.yaml (or a cluster-specific role)"
  echo "   and re-run: kubectl --context=$CONTEXT apply -f deploy/rbac.yaml"
fi

echo "==> Done. Cluster '$NAME' is registered as id $CLUSTER_ID at $CONSOLE_URL."
