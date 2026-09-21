#!/usr/bin/env bash
#
# Reach a `kind` cluster from the Compose backend container, and re-point an
# already-registered cluster at the address that works.
#
# WHY THIS IS A SCRIPT AND NOT A CHANGE TO docker-compose.yml
#
# `kind` writes `https://127.0.0.1:<port>` into your kubeconfig. Under
# Compose that address is the *container's own* loopback, not your host's,
# so the auto-adopted registration is created, looks correct, and reaches
# nothing — README's "If the console runs in a container and your cluster
# is on localhost" explains why, and ranks running the console on the host
# (`make dev-backend`) as the path with no caveats.
#
# This script automates its second-best option: attach the backend
# container to `kind`'s own Docker network and register the cluster's
# *internal* Docker-network address instead. That is NOT wired into
# docker-compose.yml itself, on purpose — a compose file that declares a
# hard dependency on a network named `kind` fails to come up at all for
# anyone using k3d, minikube, Docker Desktop, a remote cluster, or nothing
# yet, which is most onboarders. This script is the opt-in version: run it
# only if you use `kind`.
#
# WHAT THIS SCRIPT ACTUALLY DOES, IN ORDER:
#
#   1. Connects the backend container to kind's Docker network
#      (`docker network connect`) — a no-op if already connected.
#   2. Reads the cluster's *internal* kubeconfig (`kind get kubeconfig
#      --internal`), which names the control-plane container instead of
#      127.0.0.1 — reachable from another container on the same network.
#   3. Logs into the console if it requires a session, the same way
#      scripts/onboard-cluster.sh does.
#   4. Finds the cluster already registered under this context's name (the
#      one startup adoption created) and PUTs the internal address and
#      credentials onto it — never creates a second registration.
#   5. Runs the connection test and reports the result.
#
# This does not survive the backend container being recreated (a fresh
# `docker compose up -d --force-recreate`, or a pulled image update) —
# Docker network attachments are per-container, not part of the compose
# service definition. Re-run this script after that happens.
#
# USAGE
#
#   scripts/connect-kind-network.sh --context <kind-context> [options]
#
#   scripts/connect-kind-network.sh --context kind-dev
#
# OPTIONS
#
#   --context NAME        kubectl context for the kind cluster. Required,
#                         and must start with "kind-" — that prefix is how
#                         `kind` names every context it writes, and it is
#                         how this script derives the cluster name kind
#                         itself needs (the part after "kind-").
#   --container NAME      Backend container name. Default:
#                         k8boss-admin-backend (docker-compose.yml's fixed
#                         container_name).
#   --docker-network NAME kind's Docker network name. Default: kind (kind's
#                         own default; only different if you set
#                         KIND_EXPERIMENTAL_DOCKER_NETWORK when creating
#                         the cluster).
#   --console-url URL     Console base URL. Default: http://localhost:8020
#   -y, --yes             Skip the confirmation prompt.
#   -h, --help            This text.
#
# ENVIRONMENT (only read when the console requires a session)
#
#   K8BOSS_ADMIN_CONSOLE_USER       Console username. Prompted for if unset.
#   K8BOSS_ADMIN_CONSOLE_PASSWORD   Console password. Prompted for (hidden)
#                                   if unset.
#
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────── #
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────── #

CONTEXT=""
CONTAINER="k8boss-admin-backend"
DOCKER_NETWORK="kind"
CONSOLE_URL="http://localhost:8020"
ASSUME_YES="false"

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \?//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CONTEXT="${2:-}"; shift 2 ;;
    --container) CONTAINER="${2:-}"; shift 2 ;;
    --docker-network) DOCKER_NETWORK="${2:-}"; shift 2 ;;
    --console-url) CONSOLE_URL="${2:-}"; shift 2 ;;
    -y|--yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unrecognized argument: $1" >&2; echo "run with --help for usage" >&2; exit 2 ;;
  esac
done

if [[ -z "$CONTEXT" ]]; then
  echo "error: --context is required." >&2
  echo "       Available contexts:" >&2
  kubectl config get-contexts -o name 2>/dev/null | sed 's/^/         /' >&2 || true
  exit 2
fi
if [[ "$CONTEXT" != kind-* ]]; then
  echo "error: '$CONTEXT' does not start with 'kind-', so it is not a context" >&2
  echo "       kind wrote. This script only knows how to derive a kind cluster" >&2
  echo "       name (the part after 'kind-') and call 'kind get kubeconfig" >&2
  echo "       --internal' — the same idea applies to k3d, whose Docker network" >&2
  echo "       is named 'k3d-<cluster>', but that is not what this script does." >&2
  exit 2
fi
KIND_NAME="${CONTEXT#kind-}"

for tool in kubectl kind docker curl jq base64; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "error: '$tool' is required and was not found on PATH." >&2
    exit 1
  }
done

if ! kubectl config get-contexts -o name 2>/dev/null | grep -qxF "$CONTEXT"; then
  echo "error: no kubectl context named '$CONTEXT'." >&2
  exit 1
fi

echo "==> Plan:"
echo "      kind cluster     : $KIND_NAME"
echo "      backend container: $CONTAINER"
echo "      docker network   : $DOCKER_NETWORK"
echo "      console URL      : $CONSOLE_URL"

if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

# ─────────────────────────────────────────────────────────────────────────── #
# 1. Attach the backend container to kind's network (idempotent)
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Connecting $CONTAINER to the '$DOCKER_NETWORK' Docker network"
CONNECT_ERR="$(docker network connect "$DOCKER_NETWORK" "$CONTAINER" 2>&1)" && true
if [[ -n "$CONNECT_ERR" && "$CONNECT_ERR" != *"already exists"* ]]; then
  echo "error: could not connect $CONTAINER to network '$DOCKER_NETWORK':" >&2
  echo "       $CONNECT_ERR" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────── #
# 2. Read the internal kubeconfig — the Docker-network address, not 127.0.0.1
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Reading kind's internal kubeconfig for '$KIND_NAME'"
INTERNAL_KUBECONFIG="$(mktemp)"
trap 'rm -f "$INTERNAL_KUBECONFIG"' EXIT
kind get kubeconfig --internal --name "$KIND_NAME" > "$INTERNAL_KUBECONFIG"

API_SERVER="$(kubectl --kubeconfig="$INTERNAL_KUBECONFIG" config view --raw -o jsonpath='{.clusters[0].cluster.server}')"
CA_PEM="$(kubectl --kubeconfig="$INTERNAL_KUBECONFIG" config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)"
CLIENT_CERT_PEM="$(kubectl --kubeconfig="$INTERNAL_KUBECONFIG" config view --raw -o jsonpath='{.users[0].user.client-certificate-data}' | base64 -d)"
CLIENT_KEY_PEM="$(kubectl --kubeconfig="$INTERNAL_KUBECONFIG" config view --raw -o jsonpath='{.users[0].user.client-key-data}' | base64 -d)"
rm -f "$INTERNAL_KUBECONFIG"
trap - EXIT

if [[ -z "$API_SERVER" || -z "$CA_PEM" || -z "$CLIENT_CERT_PEM" || -z "$CLIENT_KEY_PEM" ]]; then
  echo "error: could not extract a complete internal credential for '$KIND_NAME'." >&2
  exit 1
fi
echo "==> Internal API server: $API_SERVER"

# ─────────────────────────────────────────────────────────────────────────── #
# Console HTTP helpers (same convention as scripts/onboard-cluster.sh)
# ─────────────────────────────────────────────────────────────────────────── #

COOKIE_JAR="$(mktemp)"
CSRF_TOKEN=""
cleanup() {
  rm -f "$COOKIE_JAR"
  unset CSRF_TOKEN K8BOSS_ADMIN_CONSOLE_PASSWORD CLIENT_KEY_PEM
}
trap cleanup EXIT

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
# 4. Find the already-registered cluster and point it at the internal address
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Looking up '$CONTEXT' in the console's cluster registry"
api_call GET /api/clusters
CLUSTER_ID="$(echo "$API_BODY" | jq -r --arg name "$CONTEXT" '.items[] | select(.name == $name) | .id')"
if [[ -z "$CLUSTER_ID" ]]; then
  echo "error: no cluster named '$CONTEXT' is registered yet." >&2
  echo "       Open the console's Clusters page once first — startup adoption" >&2
  echo "       or the Discovered-on-this-machine list registers it (unreachable" >&2
  echo "       is fine, that is what this script fixes), then re-run." >&2
  exit 1
fi
echo "==> Found cluster id $CLUSTER_ID"

UPDATE_BODY="$(jq -n \
  --arg server "$API_SERVER" \
  --arg ca "$CA_PEM" \
  --arg cert "$CLIENT_CERT_PEM" \
  --arg key "$CLIENT_KEY_PEM" \
  '{api_server: $server, authentication_type: "client_certificate",
    ca_certificate: $ca, client_certificate: $cert, client_key: $key}')"

echo "==> Updating cluster id $CLUSTER_ID with the internal address"
api_call PUT "/api/clusters/$CLUSTER_ID" "$UPDATE_BODY"
if [[ "$API_STATUS" != "200" ]]; then
  echo "error: updating cluster id $CLUSTER_ID failed (HTTP ${API_STATUS:-000})." >&2
  echo "$API_BODY" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────── #
# 5. Test it
# ─────────────────────────────────────────────────────────────────────────── #

echo "==> Testing the connection"
api_call POST "/api/clusters/$CLUSTER_ID/test"
if [[ "$API_STATUS" != "200" ]]; then
  echo "error: the test call itself failed (HTTP ${API_STATUS:-000})." >&2
  exit 1
fi

REACHABLE="$(echo "$API_BODY" | jq -r '.reachable')"
if [[ "$REACHABLE" != "true" ]]; then
  echo "!! Still not reachable:"
  echo "$API_BODY" | jq -r '.error.message // "no error message returned"' | sed 's/^/     /'
  exit 1
fi

SERVER_VERSION="$(echo "$API_BODY" | jq -r '.server_version // "unknown"')"
echo "==> Reachable. Server version: $SERVER_VERSION"
echo "==> Done. Remember: this network attachment is lost if $CONTAINER is"
echo "    recreated — re-run this script if that happens."
