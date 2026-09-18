/**
 * Console health and the global write gate.
 *
 * `GET /api/health` (§2) reports whether this deployment allows mutations at
 * all. Contract §11.5: when it says `disabled`, the app shows a read-only
 * banner and disables every write affordance rather than offering buttons that
 * will fail with `403 mutations_disabled`.
 *
 * `mutationsEnabled` is `health.mutations === 'enabled'` and nothing looser.
 * While the first request is in flight, and after one that failed, we do not
 * know whether writes are permitted — and this file resolves that the honest
 * answer to "may I write" when we could not ask is "not yet", not "probably".
 * The opposite default offers a Delete button we cannot stand behind; this one
 * greys it out for a few hundred milliseconds. `readOnly` is deliberately a
 * separate flag, true only for an explicit `disabled`, so the banner claims a
 * read-only deployment only when the backend actually said so. A page that
 * needs to explain a greyed-out button reads `reason`.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { health as healthApi } from '../api/client';

const HealthContext = createContext(null);

// Health is cheap and answers a question that changes without us acting (a
// cluster going unreachable lands in `degraded[]`), so it is polled. Slow
// enough not to be noise in the backend's access log.
const POLL_MS = 30000;

export function HealthProvider({ children }) {
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const result = await healthApi.get();
      if (!mounted.current) return null;
      setHealth(result);
      setError(null);
      return result;
    } catch (err) {
      if (!mounted.current) return null;
      // Keep the last known health rather than blanking it: a single failed
      // poll should not flip a working console into "read-only" and hide every
      // button the operator was in the middle of using. `error` makes the
      // staleness visible, and the write gate below stays on the last answer
      // the backend actually gave.
      setError(err);
      return null;
    } finally {
      if (mounted.current && !silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Polls while the tab is hidden, unlike the cluster and namespace contexts,
    // and deliberately: this reads the console's own database every 30s, not a
    // customer's API server, and a `visibilitychange` guard on its own only
    // moves the cost — the tab comes back showing whatever the status pill and
    // the write gate said before it was hidden, which is the stale answer at
    // the moment somebody is looking again. Adding the guard means adding a
    // re-read on return, and that is a bigger change than the poll is worth.
    refresh();
    const handle = setInterval(() => refresh({ silent: true }), POLL_MS);
    return () => clearInterval(handle);
  }, [refresh]);

  const value = useMemo(() => {
    const mutations = health?.mutations ?? null;
    const mutationsEnabled = mutations === 'enabled';
    const readOnly = mutations === 'disabled';
    let reason = null;
    if (readOnly) {
      reason = 'This deployment runs read-only (ADMIN_ALLOW_MUTATIONS is off). Dry runs are still allowed.';
    } else if (!mutationsEnabled) {
      reason = error
        ? `Console health could not be read (${error.message}), so write permission is unknown.`
        : 'Checking whether this deployment allows writes…';
    }
    return {
      health,
      mutationsEnabled,
      readOnly,
      reason,
      loading,
      error,
      // §2: status is `ok` or `degraded`, never `error` — the console being up
      // is what this endpoint reports.
      degraded: health?.degraded ?? [],
      refresh,
    };
  }, [health, loading, error, refresh]);

  return <HealthContext.Provider value={value}>{children}</HealthContext.Provider>;
}

export function useHealth() {
  const ctx = useContext(HealthContext);
  if (!ctx) throw new Error('useHealth must be used inside a HealthProvider');
  return ctx;
}
