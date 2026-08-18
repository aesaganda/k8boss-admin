/**
 * The namespace scope, per cluster.
 *
 * `selected` is a namespace name, or `null` meaning "all namespaces". One
 * selection, held here, so the masthead selector and every page filter are the
 * same control — two of them free to disagree is how a list ends up labelled
 * `prod` while showing `staging`.
 *
 * The scope is persisted per cluster id, because namespace names are only
 * meaningful within a cluster: restoring `prod` on a cluster that has no `prod`
 * silently filters every page to nothing, and an empty Workloads table is
 * indistinguishable from a cluster with no workloads. So the reconciliation
 * below drops a selection the current cluster does not have, and clears the
 * stored copy too — otherwise the restore effect brings the stale name straight
 * back on the next mount.
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
import { getActiveClusterId, namespaces as namespacesApi } from '../api/client';
import { useCluster } from './ClusterContext';

const NamespaceContext = createContext(null);

const storageKey = (clusterId) => `k8boss-admin.namespace.${clusterId ?? 'none'}`;

function readStored(clusterId) {
  try {
    return localStorage.getItem(storageKey(clusterId)) || null;
  } catch {
    return null;
  }
}

function writeStored(clusterId, name) {
  try {
    if (name) localStorage.setItem(storageKey(clusterId), name);
    else localStorage.removeItem(storageKey(clusterId));
  } catch {
    /* the selection still works for this session */
  }
}

export function NamespaceProvider({ children }) {
  const { activeClusterId } = useCluster();

  const [namespaces, setNamespaces] = useState([]);
  // The first render happens before ClusterProvider's value reaches this hook,
  // so the initial scope is read against the id the API client already seeded
  // itself with at module load. Without this the initial `selected` was always
  // null and the restored scope only appeared once the cluster effect fired —
  // one visible render of unscoped rows.
  const [selected, setSelectedState] = useState(() => readStored(getActiveClusterId()));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // §1.2: not being able to list namespaces is not the same as a cluster with
  // no namespaces, and the selector has to be able to say which it is.
  const [unavailable, setUnavailable] = useState([]);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Restore this cluster's own scope whenever the active cluster changes.
  useEffect(() => {
    setSelectedState(readStored(activeClusterId));
  }, [activeClusterId]);

  const refresh = useCallback(async () => {
    if (activeClusterId == null) {
      setNamespaces([]);
      setUnavailable([]);
      setError(null);
      setLoading(false);
      return [];
    }
    setLoading(true);
    try {
      const result = await namespacesApi.list();
      if (!mounted.current) return [];
      const items = result?.items ?? [];
      setNamespaces(items);
      setUnavailable(result?.unavailable ?? []);
      setError(null);

      // Drop a stored selection this cluster does not have — but only when the
      // listing was complete. On a partial listing the namespace may exist and
      // simply not be readable, and silently resetting the operator's scope
      // because of an RBAC gap would be a wrong answer about their intent.
      if (!result?.partial) {
        const known = new Set(items.map((n) => n.name));
        const stored = readStored(activeClusterId);
        if (stored && !known.has(stored)) {
          writeStored(activeClusterId, null);
          setSelectedState(null);
        }
      }
      return items;
    } catch (err) {
      if (!mounted.current) return [];
      // Empty the list — but with `error` set, so the selector renders "could
      // not load namespaces" rather than an innocent-looking empty dropdown.
      setNamespaces([]);
      setUnavailable([]);
      setError(err);
      return [];
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [activeClusterId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const setSelected = useCallback(
    (name) => {
      const next = name || null;
      setSelectedState(next);
      writeStored(activeClusterId, next);
    },
    [activeClusterId],
  );

  const value = useMemo(
    () => ({
      namespaces,
      selected,
      setSelected,
      loading,
      error,
      unavailable,
      partial: unavailable.length > 0,
      refresh,
    }),
    [namespaces, selected, setSelected, loading, error, unavailable, refresh],
  );

  return <NamespaceContext.Provider value={value}>{children}</NamespaceContext.Provider>;
}

export function useNamespace() {
  const ctx = useContext(NamespaceContext);
  if (!ctx) throw new Error('useNamespace must be used inside a NamespaceProvider');
  return ctx;
}
