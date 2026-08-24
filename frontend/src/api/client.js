/**
 * k8boss-admin API client.
 *
 * One fetch wrapper, one error type, one place that knows about cluster
 * scoping. Everything the app sends to the backend goes through `request()`.
 *
 * Base URL is a RELATIVE `/api` so requests are same-origin and flow through
 * the dev (Vite) or production (nginx) reverse proxy. That removes CORS from
 * the picture entirely; `VITE_API_BASE` exists only for split-origin
 * deployments where the backend is on another host.
 *
 * Two things here are load-bearing for the project's central invariant:
 *
 *   - `ApiError.code` carries the stable `error` string from the §1.3 envelope.
 *     Callers branch on the code, never on the message. A UI that matched on
 *     "forbidden" appearing in a message string would silently change behaviour
 *     the day someone reworded a sentence.
 *   - A network-level failure is reported as `network_unreachable`, which is
 *     deliberately NOT `cluster_unreachable`. The first says we could not reach
 *     the console's own backend; the second says the backend could not reach the
 *     Kubernetes API server. Collapsing them would tell an operator their
 *     cluster is down when it is the console that is down.
 */

const API_BASE = import.meta.env?.VITE_API_BASE || '/api';

const CLUSTER_KEY = 'k8boss-admin.activeClusterId';
const USER_KEY = 'k8boss-admin.user';

const isDev = Boolean(import.meta.env && import.meta.env.DEV);

let applicationAuthEnabled = false;
let csrfToken = null;

export function setAuthentication({ enabled, csrfToken: nextToken = null }) {
  applicationAuthEnabled = Boolean(enabled);
  csrfToken = nextToken || null;
}

/* ── Error type ─────────────────────────────────────────────────────────── */

/**
 * Every non-2xx response becomes one of these.
 *
 * `.code` is the §1.3 stable error code (`rbac_denied`, `conflict`,
 * `mutations_disabled`, …). `.data` is the whole envelope so a caller that
 * needs `context.currentResourceVersion` after a 409 can read it without a
 * second round trip.
 */
export class ApiError extends Error {
  constructor(message, { status = 0, code = null, detail = null, hint = null, context = null, data = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.hint = hint;
    this.context = context;
    this.data = data;
  }

  /** True when retrying the identical request could plausibly succeed. */
  get isTransient() {
    return RETRYABLE_STATUSES.has(this.status);
  }
}

/* ── Cluster scoping ────────────────────────────────────────────────────── */

// Seeded from localStorage at module load, BEFORE React renders. ClusterContext
// also owns this value, but its provider cannot set it until after its first
// render — and HealthContext plus any page that mounts in the same commit will
// already have fired their requests by then. Those went out unscoped, so the
// backend answered for its own notion of the active cluster and the user saw a
// flash of another cluster's data before the correct fetch replaced it.
let activeClusterId = readStoredClusterId();

function readStoredClusterId() {
  try {
    const raw = localStorage.getItem(CLUSTER_KEY);
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  } catch {
    // localStorage throws outright in Safari private mode and under a
    // blocked-cookies policy. An unscoped session is degraded, not broken.
    return null;
  }
}

export function setActiveClusterId(id) {
  activeClusterId = id != null && id !== '' ? Number(id) : null;
  try {
    if (activeClusterId == null) localStorage.removeItem(CLUSTER_KEY);
    else localStorage.setItem(CLUSTER_KEY, String(activeClusterId));
  } catch {
    /* ignore storage errors — scoping still works for this session */
  }
}

export function getActiveClusterId() {
  return activeClusterId;
}

/* ── Actor identity ─────────────────────────────────────────────────────── */

// Legacy proxy mode: the audit actor comes from the advisory X-K8Boss-User
// header. Application-authenticated deployments ignore it and derive the actor
// from the verified session instead.
export function getUser() {
  try {
    return (localStorage.getItem(USER_KEY) || '').trim() || 'anonymous';
  } catch {
    return 'anonymous';
  }
}

export function setUser(name) {
  try {
    const trimmed = (name || '').trim();
    if (trimmed) localStorage.setItem(USER_KEY, trimmed);
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* ignore storage errors */
  }
}

/* ── URL building ───────────────────────────────────────────────────────── */

/**
 * Serialise query params. `null`/`undefined`/`''` are dropped so callers can
 * pass a whole options object without pruning it first; `false` and `0` are
 * kept, because `dryRun=false` and `tailLines=0` are meaningful values and
 * dropping them would turn a real write into a dry run (or the reverse).
 * Arrays repeat the key — that is how §7 spells the exec `command` param.
 */
function buildQuery(params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value == null || value === '') continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item == null || item === '') continue;
        qs.append(key, String(item));
      }
    } else {
      qs.append(key, String(value));
    }
  }
  return qs;
}

/**
 * Build a request URL, scoping it to the active cluster unless told not to.
 *
 * §1.1: omitting cluster_id means "the active cluster" server-side. We always
 * send it when we know it, so two browser tabs on two clusters cannot race each
 * other through a single server-side "active cluster" notion.
 *
 * `scoped: false` is the opt-out, and §10 is the only caller that uses it — see
 * the `audit` export below for why that endpoint inverts the rule.
 */
function buildUrl(path, params, { scoped = true } = {}) {
  const qs = buildQuery(params);
  if (scoped && activeClusterId != null && !qs.has('cluster_id')) {
    qs.set('cluster_id', String(activeClusterId));
  }
  const query = qs.toString();
  return `${API_BASE}${path}${query ? `?${query}` : ''}`;
}

/**
 * Absolute `ws://` / `wss://` URL for the streaming endpoints in §7.
 *
 * Built from `window.location` rather than from a configured host so it follows
 * the same reverse proxy as `/api` and inherits its TLS: a page served over
 * https that opened a `ws://` socket would be blocked as mixed content, with
 * the only symptom being a terminal that never connects.
 */
export function wsUrl(path, params = {}) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const qs = buildQuery(params);
  if (activeClusterId != null && !qs.has('cluster_id')) {
    qs.set('cluster_id', String(activeClusterId));
  }
  const query = qs.toString();
  return `${proto}//${window.location.host}${API_BASE}${path}${query ? `?${query}` : ''}`;
}

/* ── Transport ──────────────────────────────────────────────────────────── */

// Transient statuses. 0 is our own synthetic "the request never produced a
// response". 4xx other than 408/429 are NEVER retried: a 403 is not going to
// become a 200, and replaying it just multiplies audit rows for a denial.
const RETRYABLE_STATUSES = new Set([0, 408, 429, 502, 503, 504]);
const MAX_RETRIES = 2; // total attempts = MAX_RETRIES + 1
const BACKOFF_BASE_MS = 300;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Is replaying this request safe?
 *
 * GET always is. A write is replayed only when it is a dry run: §1.6 states
 * that inspecting what would change is a read, so a dry run has no cluster-side
 * effect to duplicate. A real write is never replayed, because a lost response
 * is indistinguishable from a response that was lost *after* the API server
 * applied the change — retrying would scale a Deployment twice, or delete an
 * object the operator then recreates by hand, and report success either way.
 * That is exactly the confidently-wrong answer this project exists to avoid.
 */
function isReplaySafe(method, body, params) {
  if (method === 'GET' || method === 'HEAD') return true;
  if (body && typeof body === 'object' && body.dryRun === true) return true;
  // DELETE carries dryRun in the query string (§4).
  if (params && (params.dryRun === true || params.dryRun === 'true')) return true;
  return false;
}

/** Turn a non-2xx response into an ApiError carrying the §1.3 envelope. */
async function toApiError(response) {
  let body = null;
  let text = '';
  try {
    text = await response.text();
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }

  if (body && typeof body === 'object' && typeof body.error === 'string') {
    return new ApiError(body.message || body.error, {
      status: response.status,
      code: body.error,
      detail: body.detail ?? null,
      hint: body.hint ?? null,
      context: body.context ?? null,
      data: body,
    });
  }

  // Not our envelope — a reverse proxy error page, or a framework-level 422.
  // `code` stays null on purpose rather than being guessed from the status:
  // a caller branching on `code === 'conflict'` must not be handed a code the
  // backend never asserted.
  const fallback = (body && (body.message || body.detail)) || text.slice(0, 300) || response.statusText;
  return new ApiError(fallback || `Request failed with status ${response.status}`, {
    status: response.status,
    code: null,
    data: body,
  });
}

async function readBody(response, expect) {
  if (response.status === 204 || expect === 'none') return null;
  if (expect === 'text') return response.text();
  if (expect === 'blob') return response.blob();
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    // A 200 that is not JSON where JSON was expected is a backend defect, not a
    // user error; say so rather than returning undefined and letting a page
    // render an empty table as if the cluster were empty.
    throw new ApiError('Backend returned a non-JSON body where JSON was expected.', {
      status: response.status,
      code: 'upstream_error',
      detail: text.slice(0, 300),
    });
  }
}

async function attempt(url, init, expect) {
  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    // A caller-initiated abort is not a transport failure. Wrapping it as one
    // made a cancelled in-flight request (the user navigated away, or a search
    // superseded itself) retry twice and then render "cannot reach the backend"
    // on a page that was working perfectly.
    if (error?.name === 'AbortError') throw error;
    throw new ApiError(
      `Cannot reach the k8boss-admin backend at ${API_BASE} (${error.message}).`,
      {
        status: 0,
        // Not `cluster_unreachable`: this is the console being unreachable,
        // which says nothing at all about the cluster's health.
        code: 'network_unreachable',
        detail: error.message,
      },
    );
  }
  if (!response.ok) {
    const error = await toApiError(response);
    if (error.code === 'authentication_required' && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('k8boss-authentication-required'));
    }
    throw error;
  }
  return readBody(response, expect);
}

/**
 * Core request. Options:
 *   method  — HTTP verb, default GET
 *   params  — query object; cluster_id is appended automatically
 *   scoped  — false to suppress that (only the §10 audit endpoints; see buildUrl)
 *   body    — JSON-serialised when present
 *   expect  — 'json' (default) | 'text' | 'none' | 'blob'
 *   headers — extra headers
 *   signal  — AbortSignal
 */
export async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const url = buildUrl(path, options.params, { scoped: options.scoped !== false });
  const headers = {
    Accept: options.expect === 'text' ? 'text/plain, application/json' : 'application/json',
    ...options.headers,
  };
  if (applicationAuthEnabled && csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers['X-CSRF-Token'] = csrfToken;
  } else if (!applicationAuthEnabled) {
    // Legacy authenticating-proxy mode. The backend ignores this header as
    // soon as application authentication is enabled.
    headers['X-K8Boss-User'] = getUser();
  }
  const init = { method, headers, signal: options.signal, credentials: 'same-origin' };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }

  const retryAllowed = options.retry ?? isReplaySafe(method, options.body, options.params);
  const started = typeof performance !== 'undefined' ? performance.now() : 0;

  for (let tries = 0; ; tries += 1) {
    try {
      const result = await attempt(url, init, options.expect || 'json');
      if (isDev) {
        console.debug('[admin-api]', method, url, {
          clusterId: activeClusterId,
          attempt: tries + 1,
          ms: Math.round((typeof performance !== 'undefined' ? performance.now() : 0) - started),
        });
      }
      return result;
    } catch (error) {
      const transient = error instanceof ApiError && RETRYABLE_STATUSES.has(error.status);
      const willRetry = retryAllowed && transient && tries < MAX_RETRIES;
      if (isDev) {
        console.debug('[admin-api] error', method, url, {
          status: error.status,
          code: error.code,
          attempt: tries + 1,
          willRetry,
        });
      }
      if (!willRetry) throw error;
      await delay(BACKOFF_BASE_MS * 3 ** tries);
    }
  }
}

/* ── Verb helpers ───────────────────────────────────────────────────────── */

export const api = {
  get: (path, params, options) => request(path, { ...options, method: 'GET', params }),
  post: (path, body, params, options) => request(path, { ...options, method: 'POST', body, params }),
  put: (path, body, params, options) => request(path, { ...options, method: 'PUT', body, params }),
  del: (path, params, options) => request(path, { ...options, method: 'DELETE', params }),
};

/* ── §1.4: the core group's wire spelling ───────────────────────────────── */

/**
 * The core API group's real name is the empty string, which cannot be a path
 * segment; the wire spelling is the literal `core`. Every caller in this app
 * passes a group through here so a component that read a real group name off a
 * live object (`""` for core) still produces a routable path.
 */
export function wireGroup(group) {
  return group === '' || group == null ? 'core' : group;
}

/** Inverse of `wireGroup`, for display and for building `apiVersion` strings. */
export function realGroup(group) {
  return group === 'core' ? '' : group;
}

/** `apiVersion` string for a group/version pair, using the real group name. */
export function apiVersionOf(group, version) {
  const real = realGroup(group);
  return real ? `${real}/${version}` : version;
}

const gvp = (group, version, plural) =>
  `/resources/${encodeURIComponent(wireGroup(group))}/${encodeURIComponent(version)}/${encodeURIComponent(plural)}`;

/* ── §2 Health ──────────────────────────────────────────────────────────── */

export const health = {
  get: () => api.get('/health'),
};

/* ── Console authentication and users ─────────────────────────────────── */

export const auth = {
  config: () => api.get('/auth/config'),
  me: () => api.get('/auth/me'),
  login: (body) => request('/auth/login', { method: 'POST', body, retry: false }),
  logout: () => request('/auth/logout', { method: 'POST', expect: 'none', retry: false }),

  /**
   * Where to send the browser to begin single sign-on.
   *
   * A full-page navigation, not a `fetch`. The handshake is a redirect to the
   * identity provider and back, and an XHR cannot follow that: the IdP needs to
   * show a login form, possibly a second factor, possibly a consent screen, in a
   * real browsing context. It also has to be a top-level navigation for the
   * handshake cookie to come back at all — see the backend's
   * `identity/handshake.py` on why that cookie is SameSite=Lax.
   *
   * `cluster_id` is deliberately not appended: `buildUrl` is bypassed because
   * this is not an API call, and a cluster scope means nothing to a sign-in.
   */
  ssoStartUrl: (nextPath = '/') => {
    const qs = new URLSearchParams({ next: nextPath || '/' });
    return `${API_BASE}/auth/oidc/start?${qs.toString()}`;
  },
};

export const users = {
  list: () => api.get('/auth/users'),
  create: (body) => api.post('/auth/users', body),
  update: (id, body) => api.put(`/auth/users/${id}`, body),
  deactivate: (id) => request(`/auth/users/${id}`, { method: 'DELETE', expect: 'none' }),
};

/* ── §3 Clusters ────────────────────────────────────────────────────────── */

export const clusters = {
  list: () => api.get('/clusters'),
  create: (body) => api.post('/clusters', body),
  update: (id, body) => api.put(`/clusters/${id}`, body),
  // 204, no body — `expect: 'none'` so we never try to parse an empty response.
  remove: (id) => request(`/clusters/${id}`, { method: 'DELETE', expect: 'none' }),
  test: (id) => api.post(`/clusters/${id}/test`),
  overview: (id) => api.get(`/clusters/${id}/overview`),
};

/* ── §4 Generic resource access ─────────────────────────────────────────── */

export const resources = {
  catalog: () => api.get('/resources/catalog'),

  /** params: { namespace, labelSelector, fieldSelector, limit, continue } */
  list: (group, version, plural, params) => api.get(gvp(group, version, plural), params),

  get: (group, version, plural, name, namespace) =>
    api.get(`${gvp(group, version, plural)}/${encodeURIComponent(name)}`, { namespace }),

  /** text/plain YAML for the editor (§4). */
  // `signal` is optional and, unlike most reads here, actually used: the YAML
  // panel re-reads on a timer, and a poll that outlives the panel it was
  // started for is a request nobody is waiting for against a cluster somebody
  // is paying for.
  yaml: (group, version, plural, name, namespace, signal) =>
    request(`${gvp(group, version, plural)}/${encodeURIComponent(name)}/yaml`, {
      params: { namespace },
      expect: 'text',
      signal,
    }),

  /** body: { yaml, namespace, dryRun } */
  create: (group, version, plural, body) => api.post(gvp(group, version, plural), body),

  /** body: { yaml, namespace, resourceVersion, dryRun } — 409 on a stale rV. */
  update: (group, version, plural, name, body) =>
    api.put(`${gvp(group, version, plural)}/${encodeURIComponent(name)}`, body),

  /** params: { namespace, propagationPolicy, dryRun } — dryRun defaults true server-side. */
  remove: (group, version, plural, name, params) =>
    api.del(`${gvp(group, version, plural)}/${encodeURIComponent(name)}`, params),
};

/* ── §6 Workloads ───────────────────────────────────────────────────────── */

const workloadPath = (plural, namespace, name) =>
  `/workloads/${encodeURIComponent(plural)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;

export const workloads = {
  /** params: { namespace, kind } */
  list: (params) => api.get('/workloads', params),
  detail: (plural, namespace, name) => api.get(workloadPath(plural, namespace, name)),
  /** body: { replicas, dryRun } */
  scale: (plural, namespace, name, body) => api.post(`${workloadPath(plural, namespace, name)}/scale`, body),
  /** body: { dryRun } */
  restart: (plural, namespace, name, body) => api.post(`${workloadPath(plural, namespace, name)}/restart`, body),
  /** body: { suspend, dryRun } — CronJobs and Jobs only. */
  suspend: (plural, namespace, name, body) => api.post(`${workloadPath(plural, namespace, name)}/suspend`, body),
  rollout: (plural, namespace, name) => api.get(`${workloadPath(plural, namespace, name)}/rollout`),
  /** body: { revision, dryRun } */
  rollback: (plural, namespace, name, body) => api.post(`${workloadPath(plural, namespace, name)}/rollback`, body),
};

/* ── §5 Nodes, namespaces, events ───────────────────────────────────────── */

export const nodes = {
  list: () => api.get('/nodes'),
  get: (name) => api.get(`/nodes/${encodeURIComponent(name)}`),
  /** body: { unschedulable, dryRun } */
  cordon: (name, body) => api.post(`/nodes/${encodeURIComponent(name)}/cordon`, body),
  /** body: { dryRun, gracePeriodSeconds, ignoreDaemonSets, deleteEmptyDirData, force } */
  drain: (name, body) => api.post(`/nodes/${encodeURIComponent(name)}/drain`, body),

  /**
   * Debug pods this console created for one node (§5.5).
   *
   * The envelope carries `enabled` — this deployment's two gates answered
   * together — `enabledDetail`, and `namespace`, which is where a new pod would
   * appear. The namespace is not cosmetic: an operator is about to be asked to
   * confirm a privileged pod, and "where" is part of what they are confirming.
   */
  debugPods: (name) => api.get(`/nodes/${encodeURIComponent(name)}/debug`),

  /**
   * Create a debug pod on this node (§5.5).
   * body: `{ image, writableHostFilesystem, dryRun }`.
   *
   * The most privileged object this console creates: pinned to the node,
   * tolerating every taint, sharing the host PID and network namespaces, with
   * the node's root filesystem at /host. The whole manifest comes back in the
   * diff, which is how all of that gets disclosed before the confirming call.
   */
  createDebugPod: (name, body) => api.post(`/nodes/${encodeURIComponent(name)}/debug`, body),

  /**
   * Remove one (§5.5). `dryRun` is a query parameter, not a body: DELETE bodies
   * are handled inconsistently by proxies and HTTP clients, and a `dryRun` that
   * went missing in transit would turn a projection into a deletion.
   *
   * Only removes pods this console created — anything else is `404 not_found`
   * from this route rather than a delete.
   */
  deleteDebugPod: (name, pod, params) =>
    api.del(
      `/nodes/${encodeURIComponent(name)}/debug/${encodeURIComponent(pod)}`,
      params,
    ),
};

/* ── §15 CLI pods — a pod with kubectl in it ────────────────────────────── */

export const cli = {
  /**
   * The CLI pods this console created, plus what a new one would be made of.
   *
   * The envelope carries `enabled` — this deployment's two gates answered
   * together — `enabledDetail`, `namespace`, `image`, `container`, and
   * `serviceAccount`. That last one is not cosmetic and not a detail of the
   * others: kubectl inside the pod authenticates as that account, so it is the
   * whole of what a shell there is able to do to the cluster.
   */
  session: () => api.get('/cli'),

  /**
   * Create one. body: `{ image, dryRun }`.
   *
   * There is no ServiceAccount field, deliberately. It comes from the
   * deployment's configuration, and letting a caller name it would hand the
   * choice of *what this shell can do* to whoever opens the dialog. The whole
   * manifest comes back in the diff, which is how the account gets disclosed
   * before the confirming call.
   */
  create: (body) => api.post('/cli', body),

  /**
   * Remove one. `dryRun` is a query parameter, not a body: DELETE bodies are
   * handled inconsistently by proxies and HTTP clients, and a `dryRun` that
   * went missing in transit would turn a projection into a deletion.
   *
   * Only removes pods this console created — anything else is `404 not_found`
   * from this route rather than a delete.
   */
  remove: (pod, params) => api.del(`/cli/${encodeURIComponent(pod)}`, params),
};

export const namespaces = {
  list: () => api.get('/namespaces'),
};

export const events = {
  /** params: { namespace, involvedObjectKind, involvedObjectName, type, limit } */
  list: (params) => api.get('/events', params),
};

/* ── §13 Routes — exposing a Service to the outside world ───────────────── */

const routePath = (backend, namespace, name) =>
  `/routes/${encodeURIComponent(backend)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;

export const routes = {
  /**
   * Which of the three route backends this cluster serves, and what each can
   * express. Read once per page: the answer drives which options the form
   * offers and which controls it disables *with the reason* (rule 11.4).
   */
  capabilities: () => api.get('/routes/capabilities'),

  /** params: { namespace, backend (repeatable), limit } */
  list: (params) => api.get('/routes', params),

  get: (backend, namespace, name) => api.get(routePath(backend, namespace, name)),

  /**
   * Compile an exposure into one object. **Writes nothing** — it returns a
   * document and a `lossy[]` list, and no cluster is changed.
   *
   * `retry: true` is safe and is set: this is a POST, which `isReplaySafe`
   * refuses to replay by default, but replaying a pure compilation costs
   * nothing and a rendered form that fails on a transient 503 is a form the
   * operator has to fill in again.
   *
   * body: { backend, spec, document }
   */
  render: (body) => request('/routes/render', { method: 'POST', body, retry: true }),

  /** body: { backend, spec, document, acknowledgeLossy, dryRun } */
  create: (body) => api.post('/routes', body),

  /** body: { backend, spec, document, acknowledgeLossy, resourceVersion, dryRun } */
  update: (backend, namespace, name, body) =>
    api.put(routePath(backend, namespace, name), body),

  /** params: { dryRun } — a query parameter, like §4's delete. */
  remove: (backend, namespace, name, params) =>
    api.del(routePath(backend, namespace, name), params),
};

/* ── §14 The shipped router ─────────────────────────────────────────────── */

export const routerApi = {
  /** Live read, every time. `installed` is a tri-state — null means unknown. */
  status: (params) => api.get('/router', params),

  /**
   * The manifests an install would create. Pure and ungated, so it renders on
   * a console where router management is switched off — which is exactly when
   * an operator needs to read it.
   */
  plan: (body) => request('/router/plan', { method: 'POST', body, retry: true }),

  /** body: { ...options, dryRun } — install or upgrade, same call. */
  install: (body) => api.post('/router', body),

  /** params: { namespace, ingressClassName, dryRun } */
  uninstall: (params) => api.del('/router', params),
};

/* ── §9 Access preflight ────────────────────────────────────────────────── */

// `group` is translated to its wire spelling only when the caller supplied one.
// Defaulting an absent group to `core` would turn "check this verb on any
// group" into a check against the core group and answer a different question.
const withWireGroup = (check) =>
  check && 'group' in check ? { ...check, group: wireGroup(check.group) } : check;

export const access = {
  /** params: { verb, group, resource, namespace, name, subresource } */
  preflight: (params) => api.get('/access/preflight', withWireGroup(params)),
  /**
   * Batch form. One round trip for the whole set of buttons a page needs to
   * decide about — n separate GETs made a page with a dozen actions issue a
   * dozen SelfSubjectAccessReviews, which the API server rate-limits.
   */
  preflightMany: (checks) =>
    api.post('/access/preflight', { checks: (checks || []).map(withWireGroup) }),
};

/* ── §10 Audit ──────────────────────────────────────────────────────────── */

/**
 * §10, and the one place in this client that is NOT cluster-scoped.
 *
 * Everywhere else, omitting `cluster_id` means "the active cluster" (§1.1) and
 * `buildUrl` appends it for us. On the audit endpoints an omitted `cluster_id`
 * means *every* cluster — the opposite — so `scoped: false` is passed on all
 * three. Without it the page offers an "Everything" scope, states in a banner
 * that it is showing every cluster and the console's own records, and fetches
 * one cluster's writes with no sign-ins in it, disclosing the narrowing nowhere.
 *
 * `cluster_id: 0` is §10's sentinel for "records that belong to no cluster",
 * and it survives `buildQuery` because a real `0` is deliberately kept there.
 */
export const audit = {
  /**
   * params: { limit, cursor, cluster_id, actor, outcome, since, until,
   *           category, verb, dry_run }
   */
  list: (params) => request('/audit', { params, scoped: false }),

  /** §10.3 — the hash-chain integrity report. */
  verify: (params) => request('/audit/verify', { params, scoped: false }),

  /**
   * §10.4 — a full-page navigation to the export, not a `fetch`.
   *
   * The response is a `Content-Disposition: attachment` stream that can be far
   * larger than the tab's memory. Reading it through `fetch` to build a blob URL
   * would materialise the whole trail in the browser to hand it straight back to
   * the disk, and would lose the streaming the backend went to the trouble of
   * doing. Letting the browser handle the download is both correct and free.
   *
   * Unscoped, like the other two. An export narrower than the table it was taken
   * from is the worse half of the same bug: the file outlives the page, and the
   * person reading it never saw the banner.
   */
  exportUrl: (params) => {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value == null || value === '') continue;
      qs.set(key, String(value));
    }
    return `${API_BASE}/audit/export?${qs.toString()}`;
  },
};

/* ── §7 Pod logs ────────────────────────────────────────────────────────── */

export const pods = {
  /**
   * params: { container, tailLines, previous, sinceSeconds, timestamps }
   * Returns text/plain. A multi-container pod with no `container` is a
   * 422 `invalid` listing the containers — never a silent pick of the first.
   */
  logs: (namespace, name, params) =>
    request(`/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/logs`, {
      params,
      expect: 'text',
    }),

  /** WebSocket URL for the live log stream (§7). */
  logStreamUrl: (namespace, name, params) =>
    wsUrl(`/ws/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/logs`, params),

  /** WebSocket URL for an exec session (§7). `command` may repeat. */
  execUrl: (namespace, name, params) =>
    wsUrl(`/ws/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/exec`, params),

  /**
   * Every debug (ephemeral) container already in this pod (§7.4).
   *
   * The envelope carries `supported`, which is **three-valued**: `true`,
   * `false`, and `null` when the cluster's discovery document could not be
   * read. A caller that treats `null` as `false` tells an operator their
   * cluster lacks a feature it may well have, and sends them to plan an
   * upgrade instead of to look at their API server.
   */
  debugContainers: (namespace, name) =>
    api.get(`/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/debug`),

  /**
   * Attach a debug container (§7.4). body:
   * `{ image, container, targetContainer, command, tty, dryRun }`.
   *
   * A §1.5 mutation, so it goes through `MutationDialog` like every other
   * write. The response adds `container` — the name that was generated — so the
   * caller can open a terminal on it without parsing the diff.
   */
  attachDebugContainer: (namespace, name, body) =>
    api.post(`/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/debug`, body),
};

export default api;
