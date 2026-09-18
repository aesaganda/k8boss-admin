/**
 * Which columns a table shows — the state behind "Manage columns".
 *
 * The workload table is nine columns wide and the pod table is nine. Which of
 * them matter depends on the question: chasing an ImagePullBackOff you want
 * Images and Node and could lose QoS and Age; reviewing capacity it is the
 * other way round. Hiding the rest is what makes the remaining columns wide
 * enough to read without dragging every one of them.
 *
 * Four properties are load-bearing.
 *
 * **What is stored is the hidden set, not the visible one.** They are the same
 * information today and not the same information after the next release: a
 * column added later is absent from a stored *visible* list, so every operator
 * who ever opened this dialog would silently never see it. Stored as hidden,
 * a new column is visible by default and stays that way until someone turns it
 * off — the release note is the only way anyone finds out about it either way.
 *
 * **A table may ship with a column already hidden, and that is a default, not a
 * choice.** `defaultHidden` is what a browser that has never been told sees;
 * the Pods table uses it for QoS and Images, which are the two columns an
 * operator reaches for least often and the two that cost the most width. The
 * moment somebody saves the dialog their list is stored — including an empty
 * one — and the default never speaks again for that table. That is why the
 * storage below writes `[]` instead of deleting the key: without it, ticking
 * both columns back on survived exactly until the next reload.
 *
 * **The identity column cannot be hidden.** A row with no name cannot be acted
 * on, reported, or matched against `kubectl`, and a table of nine anonymous
 * status pills is not a smaller table but a useless one. The dialog shows it
 * ticked and disabled rather than omitting it, so the rule is visible instead
 * of merely enforced.
 *
 * **This is remembered and a filter is not.** A hidden column drops a fact from
 * the screen while the row stays on it; a remembered *filter* drops the row,
 * and a table that silently hides rows on a later visit is exactly how an
 * operator concludes a namespace is empty during a cleanup. See
 * `tableFilters.js`, whose selections deliberately live for one visit only.
 *
 * Storage failing costs the memory of the choice, never the choice — the same
 * bargain `columnWidths.js` makes, for the same reason.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const STORAGE_PREFIX = 'k8boss-admin.columns.';

// A shared frozen empty list, so a table that hides nothing does not hand a new
// array identity to the effect below on every render.
const EMPTY = Object.freeze([]);

function storageKeyFor(tableId) {
  return `${STORAGE_PREFIX}${tableId}`;
}

/**
 * The stored hidden keys for one table, or `null` when this browser has never
 * been told.
 *
 * `null` rather than `[]`, and the difference is load-bearing now that a table
 * may ship with columns hidden to begin with: `[]` is "I looked at the dialog
 * and I want all of them", and the two have to survive a reload as different
 * answers. Collapsing them turned ticking QoS back on into a choice that lasted
 * until the next refresh — the operator's own edit, silently undone by the
 * default it was overriding.
 */
function readHidden(tableId) {
  if (!tableId) return null;
  let raw = null;
  try {
    raw = window.localStorage.getItem(storageKeyFor(tableId));
  } catch {
    // Unreadable storage (private mode, disabled cookies) is no preference,
    // which is the table's own default — never a table with no columns.
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((key) => typeof key === 'string');
  } catch {
    // Half-written or hand-edited. Ignored rather than cleared: the next save
    // overwrites it, and clearing storage during a read is a surprise.
    return null;
  }
}

function writeHidden(tableId, hidden) {
  if (!tableId) return;
  try {
    // An empty list is written, not removed. Removing it is how "show me all of
    // them" became indistinguishable from "never asked", and on a table with a
    // default-hidden column that reads back as the default again.
    window.localStorage.setItem(storageKeyFor(tableId), JSON.stringify(hidden));
  } catch {
    // Quota or disabled storage. The choice still applies for this session.
  }
}

/**
 * Column visibility for one table.
 *
 * `columns` is the full set in render order. Returns the visible subset plus
 * what the dialog needs to edit it.
 */
export function useColumnVisibility({ tableId, columns, enabled = true, defaultHidden = EMPTY }) {
  const [hidden, setHiddenState] = useState(() =>
    enabled && tableId ? readHidden(tableId) ?? defaultHidden : EMPTY,
  );

  // The explorer swaps resource kinds under one component, and a Secret's
  // hidden columns are not a Deployment's. Without this the second table
  // inherits the first one's choice.
  const lastTableId = useRef(tableId);
  useEffect(() => {
    if (lastTableId.current === tableId) return;
    lastTableId.current = tableId;
    setHiddenState(enabled && tableId ? readHidden(tableId) ?? defaultHidden : EMPTY);
  }, [tableId, enabled, defaultHidden]);

  const setHidden = useCallback(
    (next) => {
      const keys = [...new Set(next)];
      setHiddenState(keys);
      writeHidden(tableId, keys);
    },
    [tableId],
  );

  // Back to what the table ships with, which is not the same as "every column
  // on" for a table that hides one to begin with. "Restore default columns"
  // that showed more than a first visit does would be a button whose label is
  // wrong on exactly the tables it matters on.
  const reset = useCallback(() => setHidden(defaultHidden), [setHidden, defaultHidden]);

  // The first column is the identity one by this table's own convention — it is
  // what `rowKey` and every `ResourceLink` are built around.
  const lockedKey = columns?.[0]?.key ?? null;

  const hiddenSet = useMemo(() => {
    const set = new Set(hidden);
    if (lockedKey) set.delete(lockedKey);
    return set;
  }, [hidden, lockedKey]);

  const visibleColumns = useMemo(
    () => (columns ?? []).filter((column) => !hiddenSet.has(column.key)),
    [columns, hiddenSet],
  );

  return {
    visibleColumns,
    hiddenSet,
    setHidden,
    reset,
    lockedKey,
    /** Whether anything is hidden right now — what puts a count on the button. */
    isCustomised: hiddenSet.size > 0,
  };
}
