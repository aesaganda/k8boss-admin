/**
 * Page data hooks — the plumbing every page in this lane shares.
 *
 * No JSX lives here on purpose: Vite only transforms JSX in `.jsx`, and a hook
 * that quietly gained a `<Tooltip>` in a `.js` file fails at build time with a
 * parse error that reads like a syntax mistake. Presentation lives in
 * `_parts.jsx`; this file is fetching, staleness and permission arithmetic.
 *
 * Three things here are load-bearing for the contract rather than convenience:
 *
 *   - `useAsync` clears its data when the *key* changes but keeps it across a
 *     manual reload. A namespace switch that left the previous namespace's rows
 *     on screen while the new listing was in flight is a wrong answer with the
 *     right heading on it; a 30-second refresh that blanks the table the
 *     operator is reading is merely annoying. The two are not the same event, so
 *     they are not handled the same way.
 *   - `useResourceList` accumulates `continue` pages instead of replacing them,
 *     and reports the cursor so a page can say "this listing is truncated".
 *     A silently truncated table is a cluster that looks smaller than it is.
 *   - `useGates` implements contract rule 11.4 *and* §9's distinction between a
 *     denial and a failed review. "You may not do this" and "we could not find
 *     out whether you may" both grey out a button, but they are different
 *     sentences, and telling an operator they lack a grant they actually hold
 *     sends them to edit a ClusterRole that is already correct.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { access, resources } from '../api/client';
import { useHealth } from '../contexts/HealthContext';

/* ── Async reads ────────────────────────────────────────────────────────── */

/**
 * Run `fetcher` whenever `key` changes, and on `reload()`.
 *
 *   const { data, loading, error, reload } = useAsync(
 *     () => nodes.list(),
 *     { key: `nodes:${clusterId}` },
 *   );
 *
 * The fetcher is held in a ref and never appears in a dependency array, so a
 * caller can pass an inline arrow (they all do) without the effect re-firing on
 * every render. `key` is the single declared input: if a value changes what the
 * request asks for, it belongs in the key, and if it does not, it does not.
 */
export function useAsync(fetcher, { key, enabled = true } = {}) {
  const fetcherRef = useRef(fetcher);
  // Synced in an effect rather than during render: React 19 treats render-phase
  // ref writes as a side effect, and this effect is declared first so it lands
  // before the loading effect below reads it.
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const [state, setState] = useState({ data: null, loading: Boolean(enabled), error: null });
  const [tick, setTick] = useState(0);
  const keyRef = useRef(key);

  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) {
      keyRef.current = key;
      setState({ data: null, loading: false, error: null });
      return undefined;
    }

    const isNewKey = keyRef.current !== key;
    keyRef.current = key;

    let cancelled = false;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;

    // Drop the previous answer only when the question changed. See the file
    // docstring: a refresh must not blank the table, a re-scope must.
    setState((previous) => ({
      data: isNewKey ? null : previous.data,
      loading: true,
      error: null,
    }));

    Promise.resolve()
      .then(() => fetcherRef.current(controller?.signal))
      .then((data) => {
        if (cancelled) return;
        setState({ data, loading: false, error: null });
      })
      .catch((error) => {
        // An abort is us cancelling, not a failure. Rendering it as one made a
        // page that the operator navigated away from and back to show "could
        // not reach the backend" over perfectly good data.
        if (cancelled || error?.name === 'AbortError') return;
        setState({ data: null, loading: false, error });
      });

    return () => {
      cancelled = true;
      controller?.abort();
    };
  }, [key, enabled, tick]);

  return { data: state.data, loading: state.loading, error: state.error, reload };
}

/* ── One object's YAML, kept current (§4) ───────────────────────────────── */

/**
 * How often a YAML panel re-reads the object it is showing.
 *
 * Shorter than the 30 seconds `ClusterContext` and `HealthContext` poll on, and
 * deliberately: those refresh a whole listing in the background while the
 * operator is looking at something else, and this one is the single object they
 * are looking *at*. Ten seconds is a compromise between "this is a live view of
 * my cluster" and "this console re-reads a 4000-line CRD six times a minute".
 */
const YAML_WATCH_MS = 10000;

/**
 * `GET .../{name}/yaml`, re-read on an interval, with the two failure states
 * kept apart.
 *
 * There is no watch endpoint to subscribe to — §4 is a plain read, and this
 * console holds no cluster state — so "watching" here is polling, the same
 * mechanism the cluster and health contexts already use. What matters is not
 * the mechanism but what the caller is told:
 *
 *   `error`         we have nothing to show. The panel renders a failure.
 *   `refreshError`  we have something to show and it is older than it looks.
 *                   The text stays; the header says the refresh failed.
 *
 * Collapsing those two is the defect this hook exists to avoid. Blanking a
 * manifest because one poll timed out throws away the copy the operator was
 * reading; keeping it and saying nothing tells them a stale object is live —
 * and on this screen the next thing they do is decide whether to change it.
 * `readAt` is therefore always the time the *displayed* bytes were fetched,
 * never the time of the last attempt.
 *
 * The poll pauses while the document is hidden. A console left open on a second
 * monitor overnight is otherwise several thousand reads of an object nobody is
 * looking at, and the first thing a background tab does on being shown again is
 * read anyway.
 */
export function useLiveYaml({
  group,
  version,
  plural,
  name,
  namespace = null,
  enabled = true,
  watch = true,
  intervalMs = YAML_WATCH_MS,
} = {}) {
  const key = [group ?? '', version ?? '', plural ?? '', namespace ?? '', name ?? ''].join('|');
  const active = Boolean(enabled && name && plural);

  const [state, setState] = useState({
    text: null,
    error: null,
    refreshError: null,
    readAt: null,
    changedAt: null,
    loading: active,
  });
  const [tick, setTick] = useState(0);
  const keyRef = useRef(key);

  /** Read again now. Never blanks what is on screen. */
  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    if (!active) {
      setState({ text: null, error: null, refreshError: null, readAt: null, changedAt: null, loading: false });
      return undefined;
    }

    let cancelled = false;
    let inFlight = false;
    const controllers = new Set();

    // A new object is a new question: drop the previous answer rather than
    // showing one object's manifest, and one object's "read at", under another
    // one's heading. A refresh of the same object keeps what is on screen — see
    // `useAsync`'s docstring for why those two are not the same event.
    const isNewKey = keyRef.current !== key;
    keyRef.current = key;
    setState((previous) =>
      isNewKey
        ? { text: null, error: null, refreshError: null, readAt: null, changedAt: null, loading: true }
        : { ...previous, error: null, loading: previous.text == null },
    );

    const read = async () => {
      if (inFlight) return;
      inFlight = true;
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      if (controller) controllers.add(controller);
      try {
        const text = await resources.yaml(group, version, plural, name, namespace, controller?.signal);
        if (cancelled) return;
        const at = Date.now();
        setState((previous) => ({
          text,
          error: null,
          refreshError: null,
          readAt: at,
          // Only when the bytes actually differ. A pod's status is rewritten
          // constantly; "changed" has to mean the document changed, or it means
          // nothing and the operator stops reading it.
          changedAt: previous.text != null && previous.text !== text ? at : previous.changedAt,
          loading: false,
        }));
      } catch (err) {
        if (cancelled || err?.name === 'AbortError') return;
        setState((previous) =>
          previous.text == null
            ? { ...previous, error: err, refreshError: null, loading: false }
            : { ...previous, refreshError: err, loading: false },
        );
      } finally {
        inFlight = false;
        if (controller) controllers.delete(controller);
      }
    };

    read();

    if (!watch) {
      return () => {
        cancelled = true;
        controllers.forEach((c) => c.abort());
      };
    }

    const handle = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      read();
    }, intervalMs);

    // Coming back to a tab that has been hidden for an hour must not show what
    // the cluster looked like an hour ago for another ten seconds.
    const onVisible = () => {
      if (document.visibilityState === 'visible') read();
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      cancelled = true;
      clearInterval(handle);
      document.removeEventListener('visibilitychange', onVisible);
      controllers.forEach((c) => c.abort());
    };
    // `key` stands in for the five identity fields it is built from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, active, tick, watch, intervalMs]);

  return { ...state, reload, watching: Boolean(active && watch) };
}

/* ── Generic resource listings (§4) ─────────────────────────────────────── */

const EMPTY = [];

/**
 * A §4 listing, with its `continue` cursor followed on demand.
 *
 * `unavailable` from every page fetched is merged, because rule 11.1's banner
 * describes the whole table on screen: a second page that could not read
 * something must not be silently dropped just because the first page was clean.
 */
export function useResourceList(
  group,
  version,
  plural,
  { namespace = null, limit = 500, shape, labelSelector = null, fieldSelector = null, enabled = true } = {},
) {
  const key = [
    group ?? '',
    version ?? '',
    plural ?? '',
    namespace ?? '*',
    limit,
    shape ?? 'auto',
    labelSelector ?? '',
    fieldSelector ?? '',
  ].join('|');

  const base = useAsync(
    () =>
      resources.list(group, version, plural, {
        namespace,
        limit,
        shape,
        labelSelector,
        fieldSelector,
      }),
    { key, enabled: Boolean(enabled && plural) },
  );

  const [extra, setExtra] = useState({ items: [], unavailable: [], cont: null, loading: false, error: null });

  // Any change to the question invalidates the pages we followed from the old
  // answer — a `continue` token is only meaningful against the listing it came
  // from, and replaying it against another namespace returns rows from neither.
  useEffect(() => {
    setExtra({ items: [], unavailable: [], cont: null, loading: false, error: null });
  }, [key]);

  const nextCursor = extra.items.length ? extra.cont : base.data?.continue ?? null;

  const loadMore = useCallback(async () => {
    if (!nextCursor) return;
    setExtra((s) => ({ ...s, loading: true, error: null }));
    try {
      const page = await resources.list(group, version, plural, {
        namespace,
        limit,
        shape,
        labelSelector,
        fieldSelector,
        continue: nextCursor,
      });
      setExtra((s) => ({
        items: [...s.items, ...(page?.items ?? [])],
        unavailable: [...s.unavailable, ...(page?.unavailable ?? [])],
        cont: page?.continue ?? null,
        loading: false,
        error: null,
      }));
    } catch (error) {
      setExtra((s) => ({ ...s, loading: false, error }));
    }
  }, [group, version, plural, namespace, limit, shape, labelSelector, fieldSelector, nextCursor]);

  const items = useMemo(
    () => (extra.items.length ? [...(base.data?.items ?? EMPTY), ...extra.items] : base.data?.items ?? EMPTY),
    [base.data, extra.items],
  );

  const unavailable = useMemo(
    () => [...(base.data?.unavailable ?? EMPTY), ...extra.unavailable],
    [base.data, extra.unavailable],
  );

  return {
    items,
    unavailable,
    partial: unavailable.length > 0,
    remaining: base.data?.remaining ?? null,
    hasMore: Boolean(nextCursor),
    loadingMore: extra.loading,
    moreError: extra.error,
    loadMore,
    loading: base.loading,
    error: base.error,
    reload: base.reload,
  };
}

/* ── §9 preflight, batched ──────────────────────────────────────────────── */

/**
 * `POST /api/access/preflight` for every button a page is about to render.
 *
 * One request for the page, never one per row: a table of forty pods asking
 * separately whether it may exec into each is forty SelfSubjectAccessReviews,
 * which the API server rate-limits and which turns a table render into a
 * self-inflicted denial-of-service. Checks are therefore written per *resource*,
 * not per object — RBAC on a whole resource is what almost every real Role
 * grants, and the rare `resourceNames` grant is picked up when the write itself
 * is preflighted server-side (§0.2), which is the check that actually decides.
 *
 * `checks` is `[{ id, verb, group, resource, namespace, name, subresource }]`.
 * Only the contract fields are sent; `id` stays on this side.
 */
export function usePreflight(checks, { enabled = true } = {}) {
  const list = useMemo(() => (checks ?? []).filter(Boolean), [checks]);
  const key = useMemo(
    () =>
      list
        .map((c) => [c.id, c.verb, c.group ?? 'core', c.resource, c.namespace ?? '*', c.name ?? '', c.subresource ?? ''].join('~'))
        .join('|'),
    [list],
  );
  const listRef = useRef(list);
  useEffect(() => {
    listRef.current = list;
  });

  const { data, loading, error, reload } = useAsync(
    () =>
      access.preflightMany(
        listRef.current.map(({ verb, group, resource, namespace, name, subresource }) => ({
          verb,
          group,
          resource,
          namespace: namespace ?? null,
          name: name ?? null,
          subresource: subresource ?? null,
        })),
      ),
    { key, enabled: enabled && list.length > 0 },
  );

  const byId = useMemo(() => {
    const map = new Map();
    const results = data?.results ?? [];
    // Paired by index, which is what §9 promises: "results come back in the
    // same order, because the caller pairs them by index". Matching on
    // verb+resource instead would silently mis-pair two checks that differ only
    // by namespace.
    list.forEach((check, index) => {
      if (index < results.length) map.set(check.id, results[index]);
    });
    return map;
  }, [data, list]);

  return { byId, loading, error, reload, asked: list.length > 0 };
}

/**
 * The rule-11.4 gate: `gate(id)` → `{ allowed, reason }` for one action.
 *
 * Every `allowed: false` carries a sentence the UI puts on the disabled control.
 * The five ways to be not-allowed are deliberately distinct, because they send
 * an operator to five different places:
 *
 *   read-only deployment  → change ADMIN_ALLOW_MUTATIONS, not RBAC
 *   still checking        → wait
 *   review failed         → the permission is UNKNOWN; do not touch RBAC yet
 *   evaluationError       → same, but the authorizer said so itself (§9)
 *   clean denial          → grant the verb named in the hint
 */
export function useGates(checks, { enabled = true } = {}) {
  const { mutationsEnabled, reason: healthReason } = useHealth();
  const { byId, loading, error, asked, reload } = usePreflight(checks, { enabled });

  const gate = useCallback(
    (id, { requiresWrite = true } = {}) => {
      if (requiresWrite && !mutationsEnabled) {
        return {
          allowed: false,
          reason:
            healthReason ||
            'This deployment does not allow writes, so this action cannot be performed here.',
        };
      }
      if (!asked) {
        return {
          allowed: false,
          reason: 'No permission check was made for this action, so whether it is allowed is unknown.',
        };
      }
      if (loading) return { allowed: false, reason: 'Checking whether you have permission for this…' };
      if (error) {
        return {
          allowed: false,
          reason:
            `The permission check could not be run (${error.message}). This is not a denial — ` +
            'whether you may do this is unknown.',
        };
      }
      const result = byId.get(id);
      if (!result) {
        return {
          allowed: false,
          reason: 'The permission check returned no answer for this action, so it is unknown.',
        };
      }
      if (result.evaluationError) {
        return {
          allowed: false,
          reason:
            `The authorizer could not decide this: ${result.evaluationError}. ` +
            'This is not a denial — you may well hold the permission.',
        };
      }
      if (!result.allowed) {
        // ADR-0007 condition 5: whose permission, not just which. The fallback
        // sentence used to assert the console's ServiceAccount unconditionally,
        // which is wrong on an impersonating cluster and was only ever right by
        // accident — §9 now says which subject the review answered for, and
        // "the console's ServiceAccount" is one of the values it can carry.
        const subject = result.subject || 'the console ServiceAccount';
        return {
          allowed: false,
          reason: result.hint || result.reason || `The cluster refused this permission for ${subject}.`,
          subject,
        };
      }
      return { allowed: true, reason: null };
    },
    [mutationsEnabled, healthReason, asked, loading, error, byId],
  );

  return { gate, loading, error, reload };
}

/* ── The six workload kinds, mirrored from the backend ──────────────────── */

/**
 * `app/services/workloads.py` owns this table; it is repeated here because rule
 * 11.4 requires a button the caller cannot use to be disabled **with the
 * reason**, and "a DaemonSet has no scale subresource" is a fact about the kind
 * that no round trip can discover. The backend still rejects the call (422,
 * naming the kind) — this only stops the console offering an action that cannot
 * exist. The sentences are shortened from the backend's, which are the ones the
 * operator sees if they get there another way.
 */
export const WORKLOAD_KINDS = {
  deployments: {
    kind: 'Deployment',
    group: 'apps',
    version: 'v1',
    scalable: true,
    restartable: true,
    suspendable: false,
    revisioned: true,
  },
  statefulsets: {
    kind: 'StatefulSet',
    group: 'apps',
    version: 'v1',
    scalable: true,
    restartable: true,
    suspendable: false,
    revisioned: true,
  },
  daemonsets: {
    kind: 'DaemonSet',
    group: 'apps',
    version: 'v1',
    scalable: false,
    restartable: true,
    suspendable: false,
    revisioned: true,
  },
  jobs: {
    kind: 'Job',
    group: 'batch',
    version: 'v1',
    scalable: false,
    restartable: false,
    suspendable: true,
    revisioned: false,
  },
  cronjobs: {
    kind: 'CronJob',
    group: 'batch',
    version: 'v1',
    scalable: false,
    restartable: false,
    suspendable: true,
    revisioned: false,
  },
  replicasets: {
    kind: 'ReplicaSet',
    group: 'apps',
    version: 'v1',
    scalable: true,
    restartable: false,
    suspendable: false,
    revisioned: false,
  },
};

export const KIND_TO_PLURAL = Object.fromEntries(
  Object.entries(WORKLOAD_KINDS).map(([plural, spec]) => [spec.kind, plural]),
);

/** Why a kind cannot do a thing, for the tooltip on the disabled control. */
export const KIND_LIMITS = {
  scale: {
    DaemonSet:
      'A DaemonSet has no scale subresource — it runs one pod per matching node. Change its node selection instead.',
    Job: 'A Job has no scale subresource. Its size is spec.completions and spec.parallelism, fixed at creation.',
    CronJob: 'A CronJob has no replicas of its own. Suspend it to stop it creating Jobs.',
  },
  restart: {
    Job: "A Job's pod template is immutable once created, so there is nothing to re-roll.",
    CronJob: 'A CronJob does not run pods itself; its next Job will use the current template.',
    ReplicaSet:
      'Restarting a ReplicaSet directly is undone by the Deployment that owns it. Restart the Deployment.',
  },
  suspend: {
    Deployment: 'Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.',
    StatefulSet: 'Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.',
    DaemonSet:
      'Only Jobs and CronJobs have spec.suspend. To stop a DaemonSet running, change its nodeSelector.',
    ReplicaSet: 'Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.',
  },
  rollback: {
    Job: 'A Job has no revision history: it runs once with the template it was created with.',
    CronJob: 'A CronJob has no revision history.',
    ReplicaSet: 'A ReplicaSet is itself one revision of a Deployment. Roll the Deployment back.',
  },
};

/**
 * Combine a kind capability with a permission gate.
 *
 * Capability first: "a DaemonSet cannot be scaled" is true regardless of who is
 * asking, and reporting it as a permission problem would send an operator to
 * grant a verb that would not help.
 */
export function capabilityGate(kind, action, spec, permission) {
  const capable = {
    scale: spec?.scalable,
    restart: spec?.restartable,
    suspend: spec?.suspendable,
    rollback: spec?.revisioned,
  }[action];
  if (!capable) {
    return {
      allowed: false,
      reason: KIND_LIMITS[action]?.[kind] || `A ${kind} does not support this action.`,
    };
  }
  return permission;
}

/**
 * One §9 check per (kind, verb) pair, asked once for the whole page.
 *
 * `patch` is the verb for all four workload writes — `admin/scale.py` and
 * `admin/rollout.py` both patch — and scaling additionally names the `scale`
 * subresource, which RBAC treats as a separate resource. Asking per row instead
 * would be one SelfSubjectAccessReview per workload, which the API server
 * rate-limits and which would make a large namespace slower to render the more
 * of it the operator can see.
 *
 * Shared between the Workloads table and rule 11.13's topology rather than copied
 * into each: the id strings are what `gate(...)` is looked up by, and two lists
 * free to drift would disable a control on one screen and offer it on the
 * other, for the same operator on the same cluster.
 *
 * `plurals` narrows the batch to the kinds actually on screen. `verbs` narrows
 * it to the actions actually offered — a check nobody reads is a review the API
 * server ran for nothing.
 */
export function workloadChecks(
  namespace,
  { verbs = ['create', 'patch', 'scale'], plurals = null } = {},
) {
  const wanted = new Set(verbs);
  const checks = [];
  for (const [plural, spec] of Object.entries(WORKLOAD_KINDS)) {
    if (plurals && !plurals.includes(plural)) continue;
    for (const verb of ['create', 'patch', 'update', 'delete']) {
      if (wanted.has(verb)) {
        checks.push({ id: `${verb}:${plural}`, verb, group: spec.group, resource: plural, namespace });
      }
    }
    if (wanted.has('scale') && spec.scalable) {
      checks.push({
        id: `scale:${plural}`,
        verb: 'patch',
        group: spec.group,
        resource: plural,
        subresource: 'scale',
        namespace,
      });
    }
  }
  return checks;
}

/**
 * A cluster-wide review answers a different question from a namespaced one, and
 * §9 says so explicitly. With no namespace selected we can only ask the
 * cluster-wide form, and a `no` there does not rule out a namespace-scoped
 * grant — so the disabled control says that rather than implying the operator
 * lacks the permission everywhere.
 */
export function withScopeNote(gate, namespace) {
  if (gate.allowed || namespace) return gate;
  return {
    allowed: false,
    reason:
      `${gate.reason} This was checked cluster-wide because no namespace is selected; ` +
      'a grant that exists in one namespace would not show up here. Select a namespace to check it.',
  };
}

/* ── Small shared derivations ───────────────────────────────────────────── */

/** `{a: 1, b: 2}` → a stable `k=v` array, for chips and tooltips. */
export function entriesOf(map) {
  if (!map || typeof map !== 'object') return [];
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b));
}

/**
 * Name and namespace off either a typed §8 row or a raw §4 manifest.
 *
 * The generic browser sees both: `shape=auto` returns a typed row for the
 * resources that have one and a trimmed manifest for everything else, and the
 * Explorer asks for `raw` precisely so it does not have to guess. A reader that
 * only understood one shape rendered a blank Name column for half the catalog.
 */
export function objectName(row) {
  return row?.name ?? row?.metadata?.name ?? null;
}

export function objectNamespace(row) {
  return row?.namespace ?? row?.metadata?.namespace ?? null;
}

export function objectAgeSeconds(row) {
  if (row?.age_seconds != null) return row.age_seconds;
  const created = row?.metadata?.creationTimestamp;
  if (!created) return null;
  const parsed = Date.parse(created);
  if (Number.isNaN(parsed)) return null;
  return Math.max(0, Math.floor((Date.now() - parsed) / 1000));
}
