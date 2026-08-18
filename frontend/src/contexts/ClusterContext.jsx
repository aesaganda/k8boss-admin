/**
 * The registered clusters, and which one the console is pointed at.
 *
 * The active id is pushed into the API client (`setActiveClusterId`) so every
 * request carries `?cluster_id=` (§1.1) — the client, not each call site, owns
 * that. It is also persisted, scoped by nothing else: "which cluster am I
 * administering" is the single most consequential piece of state in this app
 * and it must survive a reload exactly, because the alternative is an operator
 * who thinks they are in staging.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { clusters as clustersApi, getActiveClusterId, setActiveClusterId as setClientClusterId } from '../api/client';

const ClusterContext = createContext(null);

// Cluster status (connected / unreachable) changes without us doing anything,
// and every page renders a status pill from it.
const POLL_MS = 30000;

export function ClusterProvider({ children }) {
  // Seeded from the client, which read localStorage at module load — before
  // React rendered — so the very first request from any provider is already
  // scoped. Reading localStorage a second time here would be the same value
  // with one more way to disagree.
  const [activeClusterId, setActiveIdState] = useState(() => getActiveClusterId());
  const [clusters, setClusters] = useState([]);
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
      const result = await clustersApi.list();
      if (!mounted.current) return [];
      const items = result?.items ?? [];
      setClusters(items);
      setError(null);

      // Pick a cluster when the stored one is gone (deleted, or this is a fresh
      // browser). Only on a foreground refresh: auto-selecting during a
      // background poll would remount the whole content area — Layout keys on
      // the active id — and throw away whatever the operator was typing into a
      // YAML editor at that moment.
      setActiveIdState((current) => {
        if (current != null && items.some((c) => c.id === current)) return current;
        if (silent) return current;
        return items.length ? items[0].id : null;
      });
      return items;
    } catch (err) {
      if (!mounted.current) return [];
      // The list is deliberately NOT cleared. A transient failure that blanked
      // the cluster selector would drop the operator out of the cluster they
      // are working in and re-scope every open page to "no cluster". Stale but
      // labelled beats blank; `error` is what labels it.
      setError(err);
      return [];
    } finally {
      if (mounted.current && !silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const handle = setInterval(() => refresh({ silent: true }), POLL_MS);
    return () => clearInterval(handle);
  }, [refresh]);

  // Keep the API client in sync on every path that changes the id, including
  // the auto-selection inside `refresh` above.
  useEffect(() => {
    setClientClusterId(activeClusterId);
  }, [activeClusterId]);

  const setActiveClusterId = useCallback((id) => {
    const next = id == null || id === '' ? null : Number(id);
    // Set the client synchronously as well as through the effect: a caller that
    // switches cluster and immediately fetches (the selector's onSelect does
    // exactly that on some pages) would otherwise send the old id, and the new
    // page would render the previous cluster's rows under the new cluster's
    // name — a wrong answer delivered confidently.
    setClientClusterId(next);
    setActiveIdState(next);
  }, []);

  const activeCluster = useMemo(
    () => clusters.find((c) => c.id === activeClusterId) ?? null,
    [clusters, activeClusterId],
  );

  const value = useMemo(
    () => ({
      clusters,
      activeCluster,
      activeClusterId,
      setActiveClusterId,
      loading,
      error,
      refresh,
    }),
    [clusters, activeCluster, activeClusterId, setActiveClusterId, loading, error, refresh],
  );

  return <ClusterContext.Provider value={value}>{children}</ClusterContext.Provider>;
}

export function useCluster() {
  const ctx = useContext(ClusterContext);
  if (!ctx) throw new Error('useCluster must be used inside a ClusterProvider');
  return ctx;
}
