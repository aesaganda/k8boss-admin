/**
 * Row density — how much vertical room a table row is allowed to take.
 *
 * The workload and pod tables carry columns with no natural bound: a pod name
 * with a ReplicaSet hash on it, a status reason, two image references. At the
 * widths those columns get on a laptop they wrap, and a row that wraps is three
 * lines tall — so a listing of 76 workloads is several screens of scrolling for
 * an operator who is trying to spot the one row that is not Healthy. The fix is
 * the one Teams offers for its message list: a Comfy/Compact choice, made once
 * and remembered.
 *
 * **`comfy` is exactly what the tables did before this existed.** It is the
 * default, and it renders identically to the pre-density console — nobody who
 * never touches the control sees a change. `compact` is the new, denser
 * rendering: tighter rows, and one line per row rather than as many as the
 * content wants.
 *
 * **One preference, not one per table.** An operator who wants dense rows wants
 * them on Pods as well as on Workloads; making them say so twice is the kind of
 * setting people give up on. This is the same reasoning Teams applies — message
 * density is a chat-wide setting, not a per-conversation one.
 *
 * **A preference we could not store is not an error.** `localStorage` throws in
 * private modes and on a full quota. The choice still applies for the session;
 * refusing to apply it because we could not write it down would be the worse
 * failure, and it is the same bargain `columnWidths.js` makes.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

const STORAGE_KEY = 'k8boss-admin.density';

/** The two densities. Not exported: the toggle owns its own labelled list, and
 *  a second exported constant here is one more thing to keep in step. */
const DENSITIES = ['comfy', 'compact'];

const DEFAULT_DENSITY = 'comfy';

const DensityContext = createContext(null);

/**
 * Anything unrecognised — a stale key from an older build, a hand-edited entry
 * — reads as the default rather than being passed through. A density this app
 * has no rules for would render as an unstyled table with no way back to a
 * working one except clearing storage.
 */
function normalise(value) {
  return DENSITIES.includes(value) ? value : DEFAULT_DENSITY;
}

function readInitialDensity() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) return normalise(stored);
  } catch {
    /* localStorage is unavailable in private mode; the default still applies */
  }
  return DEFAULT_DENSITY;
}

export function DensityProvider({ children }) {
  const [density, setDensityState] = useState(readInitialDensity);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, density);
    } catch {
      /* the density still applies for this session */
    }
  }, [density]);

  const setDensity = useCallback((next) => {
    setDensityState((current) => normalise(typeof next === 'function' ? next(current) : next));
  }, []);

  const value = useMemo(() => ({ density, setDensity }), [density, setDensity]);
  return <DensityContext.Provider value={value}>{children}</DensityContext.Provider>;
}

export function useDensity() {
  const ctx = useContext(DensityContext);
  if (!ctx) throw new Error('useDensity must be used inside a DensityProvider');
  return ctx;
}
