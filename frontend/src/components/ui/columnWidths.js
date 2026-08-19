/**
 * Resizable table columns — the state behind the header drag handles.
 *
 * Why this exists: the workload and pod tables put columns whose content has no
 * bound — an image reference, a status reason, a node name — next to columns
 * that are three characters wide. The browser sizes them from whatever happens
 * to be on the current page, so the same column is generous on one namespace
 * and clipped on the next, and which column matters depends on what the
 * operator is doing rather than on what the data measured. Dragging the header
 * edge is the fix, and it has to outlive a 30-second poll and a reload or it is
 * a toy: an operator who widens Images to read a digest does not want it back
 * at 90px the moment the table refreshes.
 *
 * Six properties are load-bearing.
 *
 * **The trailing column is never pinned.** It absorbs the leftover width, and
 * that is what makes every other column exactly as wide as it was dragged. With
 * every column pinned and their sum under the table width, browsers distribute
 * the slack proportionally across all of them — so a column dragged narrower
 * springs back part of the way and the resize visibly does not stick.
 *
 * **The first drag pins every column at its measured width, not just the one
 * being dragged.** Switching one column to a fixed width switches the table to
 * a fixed layout, and every column that still had an automatic width would be
 * re-sized from the content-derived one to an equal share — the whole table
 * would jump on first grab and nothing the operator did caused it.
 *
 * **Widths are pixels and the table may outgrow its container.** The caller
 * hands `minTableWidth` to the stylesheet as `--admin-table-min-width`, and
 * `.admin-table--sized` resolves it as `max(100%, …)`: widening one column
 * pushes the table into the page's horizontal scroll instead of squeezing the
 * others below a readable floor. It is a custom property rather than an inline
 * width because the stylesheet has to be able to take it away again — see the
 * next property.
 *
 * **The widths are abandoned where PatternFly restacks the table.** Below 48rem
 * the table becomes one card per row: the header is gone, every resize handle
 * with it, and there are no columns left for a pixel width to describe. A width
 * set on a desktop would leave that card list frozen at desktop width, scrolled
 * sideways, with nothing on screen to undo it.
 *
 * **A drag writes the DOM, not React state.** Only the first move goes through
 * a render — it is what puts the `<colgroup>` on the table — and every move
 * after it sets `col.style.width` directly, with one commit on release.
 * Rendering per move rebuilds every row, and PatternFly measures every cell it
 * renders: on a listing of a few thousand pods that is most of a second per
 * pointer event, and the column lags the cursor by the length of the drag.
 *
 * **A width we could not store is not an error.** `localStorage` throws in
 * private modes and when a quota is full. Resizing still works for the session;
 * only the memory of it is lost, and a table that refused to resize because it
 * could not write a preference would be the worse failure.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/**
 * Drag limits. The minimum is a floor on uselessness rather than on layout —
 * PatternFly cells stay legible well below it — and the maximum only exists so
 * that a pointer dragged off-screen, or a hand-edited storage entry, cannot
 * leave a table one column wide with everything else pushed out of view.
 */
export const MIN_COLUMN_WIDTH = 56;
export const MAX_COLUMN_WIDTH = 1600;

/**
 * Floors for the columns that are NOT pinned, used only to compute how wide the
 * table has to be. They are not applied to the columns themselves: a column
 * with an automatic width keeps it and shares whatever is left.
 */
const UNPINNED_MIN_WIDTH = 96;

const KEY_STEP = 16;
const KEY_STEP_LARGE = 64;

const STORAGE_PREFIX = 'k8boss-admin.columnWidths.';

/** Whole pixels inside the drag limits — what a drag or a keypress may produce. */
export function clampWidth(px) {
  const n = Number(px);
  if (!Number.isFinite(n)) return null;
  return Math.round(Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, n)));
}

/**
 * Whole pixels for a width we *measured* or read back, with only the sanity cap
 * applied. Deliberately not `clampWidth`: a column that is naturally narrower
 * than the drag minimum — a QoS class, a two-character count — would be widened
 * by the act of grabbing some other column's edge, and the table would jump for
 * a drag that had not started yet.
 */
function sanitizeWidth(px) {
  const n = Number(px);
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.round(Math.min(MAX_COLUMN_WIDTH, n));
}

function storageKeyFor(tableId) {
  return `${STORAGE_PREFIX}${tableId}`;
}

/**
 * Stored widths for one table, or `{}`.
 *
 * Every value is re-clamped on the way in rather than trusted. This entry is
 * user-writable storage on a machine we do not own, and a `1e9` in it would
 * render a table nobody can scroll back from — there is no reason to let a bad
 * value in and every reason to treat it as absent.
 */
function readWidths(tableId) {
  if (!tableId) return {};
  let raw = null;
  try {
    raw = window.localStorage.getItem(storageKeyFor(tableId));
  } catch {
    // Storage is unreadable (private mode, disabled cookies). No preference is
    // not the same as a broken table: fall back to automatic widths.
    return {};
  }
  if (!raw) return {};

  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // A half-written or hand-edited entry. Ignored rather than cleared: the
    // next resize overwrites it, and clearing storage on a read is a surprise.
    return {};
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};

  const out = {};
  for (const [key, value] of Object.entries(parsed)) {
    const width = sanitizeWidth(value);
    if (width != null) out[key] = width;
  }
  return out;
}

function writeWidths(tableId, widths) {
  if (!tableId) return;
  try {
    if (!Object.keys(widths).length) window.localStorage.removeItem(storageKeyFor(tableId));
    else window.localStorage.setItem(storageKeyFor(tableId), JSON.stringify(widths));
  } catch {
    // Quota exhausted or storage disabled. The widths still apply for this
    // session; only remembering them fails, and that is not worth an error.
  }
}

function isRtl(node) {
  try {
    return window.getComputedStyle(node).direction === 'rtl';
  } catch {
    // jsdom and other non-layout environments. Left-to-right is the assumption
    // everywhere else in this app.
    return false;
  }
}

/**
 * Column-width state for one table.
 *
 * `columnKeys` is the **pinnable** set in render order — the caller withholds
 * the trailing column, which has to stay automatic (see the header comment).
 * `trailingMinWidth` is the floor reserved for it when the table is wider than
 * its container.
 */
export function useColumnWidths({ tableId, columnKeys, enabled = true, trailingMinWidth = UNPINNED_MIN_WIDTH }) {
  const [widths, setWidths] = useState(() => (enabled ? readWidths(tableId) : {}));
  const [resizing, setResizing] = useState(null);

  // A synchronous mirror of `widths`. A pointermove computes the next width
  // from the previous one, and reading it back out of React state would read
  // whatever the last committed render held rather than what the move before
  // this one produced — the drag would lag or stutter under fast movement.
  const widthsRef = useRef(widths);
  const headerRefs = useRef(new Map());
  // The `<col>` elements and the table itself, written to directly for the
  // length of a drag. See `onPointerMove` for why they have to be.
  const colRefs = useRef(new Map());
  const tableRef = useRef(null);
  const drag = useRef(null);

  const applyWidths = useCallback(
    (next, { persist = false } = {}) => {
      widthsRef.current = next;
      setWidths(next);
      if (persist) writeWidths(tableId, next);
    },
    [tableId],
  );

  // A different table (the explorer switching resource kinds) has different
  // stored widths. Without this the new table would inherit the old one's.
  const lastTableId = useRef(tableId);
  useEffect(() => {
    if (lastTableId.current === tableId) return;
    lastTableId.current = tableId;
    const next = enabled ? readWidths(tableId) : {};
    widthsRef.current = next;
    setWidths(next);
  }, [tableId, enabled]);

  /**
   * A ref for a header cell: a fresh object every render, over one backing node
   * per column.
   *
   * It has to be an object rather than a callback, because PatternFly's `Th`
   * stores whatever it is given and dereferences `.current`. It has to be a new
   * object each render because `Th` also keys its truncation measurement on the
   * ref's identity (`useEffect(..., [cellRef])`, where its own fallback is a
   * fresh `createRef()` every render). Handing it one stable object would run
   * that measurement once and never again.
   *
   * What that measurement drives is one thing: the tab stop `Th` puts on a
   * header whose text is clipped, so it can be reached and read. It applies
   * only to columns without a sort or select control — a sortable header is
   * `tabIndex: -1` regardless — and not to the hover tooltip, which compares
   * `offsetWidth` to `scrollWidth` live and was never affected. Small, but it
   * is exactly the columns this feature lets an operator narrow.
   */
  const headerNode = useCallback((key) => {
    let node = headerRefs.current.get(key);
    if (!node) {
      node = { current: null };
      headerRefs.current.set(key, node);
    }
    return node;
  }, []);

  const headerRef = useCallback(
    (key) => {
      const node = headerNode(key);
      return {
        get current() {
          return node.current;
        },
        // React nulls the outgoing ref before it fills the incoming one, and
        // every one of them writes through to the same node, so the order
        // leaves the node holding the element rather than the null.
        set current(value) {
          node.current = value;
        },
      };
    },
    [headerNode],
  );

  /** The stable ref object for a column's `<col>`, same contract as `headerRef`. */
  const colRef = useCallback((key) => {
    let ref = colRefs.current.get(key);
    if (!ref) {
      ref = { current: null };
      colRefs.current.set(key, ref);
    }
    return ref;
  }, []);

  const measure = useCallback((key) => {
    const element = headerRefs.current.get(key)?.current;
    if (!element) return null;
    return sanitizeWidth(element.getBoundingClientRect().width);
  }, []);

  /**
   * Put the column's current width on the handle, for the screen reader.
   *
   * Written to the element rather than held in state, for the same reason the
   * drag writes `<col>` directly: this runs on focus and on blur, and a state
   * update there re-renders every row in the table. On a few thousand pods that
   * is half a second per Tab through the header — a cost only a keyboard user
   * pays, which is the wrong way round.
   *
   * Withholding the number is not an option either: Chrome fills a focusable
   * separator's missing `aria-valuenow` with 50, which is below the 56 this
   * handle declares as its minimum, so a 97px column is announced as narrower
   * than its own floor.
   */
  const syncValue = useCallback(
    (element, key) => {
      if (!element) return;
      const pinned = widthsRef.current[key];
      const width = pinned ?? measure(key);
      if (width == null) return;
      element.setAttribute('aria-valuenow', String(width));
      element.setAttribute(
        'aria-valuetext',
        pinned == null ? `Automatic width, ${width} pixels` : `${width} pixels`,
      );
    },
    [measure],
  );

  /** The same, after the browser has laid out whatever just changed. */
  const syncValueAfterCommit = useCallback(
    (element, key) => {
      if (!element) return;
      window.requestAnimationFrame(() => syncValue(element, key));
    },
    [syncValue],
  );

  /** Every pinnable column pinned at what it currently measures, leaving the
   *  ones already pinned exactly where the operator put them. */
  const freeze = useCallback(() => {
    const next = { ...widthsRef.current };
    for (const key of columnKeys) {
      if (next[key] != null) continue;
      const measured = measure(key);
      if (measured != null) next[key] = measured;
    }
    return next;
  }, [columnKeys, measure]);

  /**
   * How wide the table must be for a set of widths to be honoured exactly:
   * every pinned column, plus a floor for each column still on an automatic
   * width. Without the floors, one very wide column would squeeze the rest to
   * nothing instead of introducing a horizontal scroll.
   */
  const totalWidth = useCallback(
    (map) => {
      let total = trailingMinWidth;
      for (const key of columnKeys) total += map[key] ?? UNPINNED_MIN_WIDTH;
      return total;
    },
    [columnKeys, trailingMinWidth],
  );

  const finishDrag = useCallback(
    (committed) => {
      const state = drag.current;
      if (!state) return;
      drag.current = null;
      setResizing(null);
      try {
        state.node?.releasePointerCapture?.(state.pointerId);
      } catch {
        // The capture is already gone (the pointer left the document, the node
        // unmounted). Nothing to release and nothing to report.
      }
      // A drag that never moved changed nothing, so it neither persists nor
      // restores: writing here would file a preference the operator never made.
      if (!state.moved) return;
      // The live widths went to the DOM during the drag; this is where React
      // and storage are told what the operator settled on.
      if (committed) applyWidths(state.live, { persist: true });
      else applyWidths(state.previous);
      syncValueAfterCommit(state.node, state.key);
    },
    [applyWidths, syncValueAfterCommit],
  );

  const onPointerDown = useCallback(
    (event, key) => {
      // Secondary buttons open context menus; a drag on one is not a resize.
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      const node = headerRefs.current.get(key)?.current;
      if (!node) return;

      // Both matter: no text selection across the header, and no sort from the
      // click that started the drag.
      event.preventDefault();
      event.stopPropagation();

      // Measured now, while nothing has moved, but not applied until the
      // pointer actually travels. A click that turns out not to be a drag —
      // including each half of the double-click that resets a column — must
      // leave the table exactly as it found it rather than pinning every
      // column at whatever it happened to measure.
      const frozen = freeze();
      const base = frozen[key] ?? sanitizeWidth(node.getBoundingClientRect().width) ?? MIN_COLUMN_WIDTH;
      const handle = event.currentTarget;
      try {
        handle.setPointerCapture(event.pointerId);
      } catch {
        // Capture is an optimisation — it keeps the moves coming when the
        // pointer leaves the handle. Without it the drag ends early rather
        // than misbehaving, so a failure here is not worth aborting for.
      }
      // Deliberately NOT focusing the handle. The preventDefault above already
      // leaves focus where it was, and taking it here would mean the arrow keys
      // an operator presses next to scroll the page silently resize a column
      // instead — as well as leaving a focus ring behind every mouse drag.

      drag.current = {
        key,
        base,
        frozen,
        live: frozen,
        moved: false,
        startX: event.clientX,
        // In a right-to-left table the inline-end edge moves the other way, so
        // dragging "outward" must still widen the column.
        sign: isRtl(node) ? -1 : 1,
        pointerId: event.pointerId,
        node: handle,
        previous: widthsRef.current,
      };
      setResizing(key);
    },
    [freeze],
  );

  const onPointerMove = useCallback(
    (event) => {
      const state = drag.current;
      if (!state) return;
      const next = clampWidth(state.base + state.sign * (event.clientX - state.startX));
      if (next == null || state.live[state.key] === next) return;

      // Rebuilt from the frozen snapshot rather than from the live map, so the
      // column being dragged is the only one that can differ from what the
      // table looked like when the drag started.
      const live = { ...state.frozen, [state.key]: next };
      state.live = live;

      const col = colRefs.current.get(state.key)?.current;
      if (!state.moved || !col) {
        // The first move is the one that has to go through React: it is what
        // renders the `<colgroup>` and switches the table to a fixed layout.
        // The same path catches a `<col>` that has not been committed yet.
        state.moved = true;
        applyWidths(live);
        return;
      }

      // Every move after that writes the DOM directly. Through React it would
      // rebuild every row of the table for one column's width — on a listing of
      // a few thousand pods that is most of a second per pointer event, and the
      // column visibly lags the cursor by the length of the drag. The commit in
      // `finishDrag` is what puts React back in agreement with the DOM.
      state.moved = true;
      col.style.width = `${next}px`;
      tableRef.current?.style?.setProperty('--admin-table-min-width', `${totalWidth(live)}px`);
    },
    [applyWidths, totalWidth],
  );

  const onPointerUp = useCallback(() => finishDrag(true), [finishDrag]);
  const onPointerCancel = useCallback(() => finishDrag(false), [finishDrag]);

  const resetColumn = useCallback(
    (key) => {
      if (widthsRef.current[key] == null) return;
      const next = { ...widthsRef.current };
      delete next[key];
      applyWidths(next, { persist: true });
    },
    [applyWidths],
  );

  const resetAll = useCallback(() => applyWidths({}, { persist: true }), [applyWidths]);

  /**
   * Move focus to the first resize handle.
   *
   * The reset control unmounts itself the moment it is used — there is nothing
   * left to reset — and a keyboard user who activated it would otherwise be
   * dropped on `<body>`, restarting their next Tab at the masthead. The handles
   * are what they were working with, so that is where focus goes; the screen
   * reader then announces the column and its automatic width, which is also the
   * confirmation that the reset happened.
   */
  const focusFirstResizer = useCallback(() => {
    for (const key of columnKeys) {
      const handle = headerRefs.current.get(key)?.current?.querySelector('.admin-col-resizer');
      if (handle) {
        handle.focus();
        // Focus arrives before the reset has been rendered, so the width the
        // handle would announce is the one it is about to stop having.
        syncValueAfterCommit(handle, key);
        return true;
      }
    }
    return false;
  }, [columnKeys, syncValueAfterCommit]);

  const onKeyDown = useCallback(
    (event, key) => {
      if (event.key === 'Home') {
        // "Give this column its automatic width back". Escape is deliberately
        // not a second spelling of it: this table can be inside a modal, and
        // Escape there belongs to the modal.
        event.preventDefault();
        resetColumn(key);
        return;
      }
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;

      const node = headerRefs.current.get(key)?.current;
      if (!node) return;
      event.preventDefault();

      const step = event.shiftKey ? KEY_STEP_LARGE : KEY_STEP;
      const outward = event.key === (isRtl(node) ? 'ArrowLeft' : 'ArrowRight');
      const frozen = freeze();
      const base = frozen[key] ?? sanitizeWidth(node.getBoundingClientRect().width) ?? MIN_COLUMN_WIDTH;
      const next = clampWidth(base + (outward ? step : -step));
      applyWidths({ ...frozen, [key]: next }, { persist: true });
    },
    [applyWidths, freeze, resetColumn],
  );

  // Escape has to work while the pointer is captured, and the captured pointer
  // is not what is holding keyboard focus.
  useEffect(() => {
    if (!resizing) return undefined;
    const onWindowKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        finishDrag(false);
      }
    };
    window.addEventListener('keydown', onWindowKeyDown);
    return () => window.removeEventListener('keydown', onWindowKeyDown);
  }, [resizing, finishDrag]);

  // One class on <body> for the length of the drag: the pointer is captured by
  // the handle, so without it the cursor reverts to a text caret the moment it
  // crosses into a cell, and a stray selection paints over the rows.
  useEffect(() => {
    if (!resizing) return undefined;
    document.body.classList.add('admin-resizing');
    return () => document.body.classList.remove('admin-resizing');
  }, [resizing]);

  /** Widths for the columns actually on screen. A stored width for a column
   *  that is not currently rendered — Namespace, hidden while a namespace is
   *  selected — is kept but not applied, and comes back with the column. */
  const applied = useMemo(() => {
    const out = {};
    for (const key of columnKeys) {
      if (widths[key] != null) out[key] = widths[key];
    }
    return out;
  }, [columnKeys, widths]);

  const isActive = Object.keys(applied).length > 0;

  const minTableWidth = useMemo(() => (isActive ? totalWidth(applied) : null), [applied, isActive, totalWidth]);

  const resizerProps = useCallback(
    (key, title) => ({
      role: 'separator',
      'aria-orientation': 'vertical',
      'aria-label': `Resize the ${title} column`,
      // A pinned column's width is known without measuring, so React can
      // render it. An automatic one is measured onto the element by
      // `syncValue` when the handle takes focus, which is when it is read.
      'aria-valuenow': widths[key] ?? undefined,
      'aria-valuemin': MIN_COLUMN_WIDTH,
      'aria-valuemax': MAX_COLUMN_WIDTH,
      'aria-valuetext': widths[key] == null ? undefined : `${widths[key]} pixels`,
      tabIndex: 0,
      className: `admin-col-resizer${resizing === key ? ' admin-col-resizer--active' : ''}`,
      // Short on purpose: `title` becomes the accessible description, and this
      // is read out once per column to anybody tabbing through the header.
      title: 'Drag to resize. Home restores the automatic width.',
      'data-testid': `column-resizer-${key}`,
      onFocus: (event) => syncValue(event.currentTarget, key),
      onPointerDown: (event) => onPointerDown(event, key),
      onPointerMove,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture: onPointerCancel,
      onKeyDown: (event) => {
        onKeyDown(event, key);
        // The width the handle reports has just changed under it, and for a
        // column handed back its automatic width there is nothing to report
        // until the browser has laid it out again.
        syncValueAfterCommit(event.currentTarget, key);
      },
      onDoubleClick: (event) => {
        event.preventDefault();
        event.stopPropagation();
        resetColumn(key);
        syncValueAfterCommit(event.currentTarget, key);
      },
      // The handle sits inside the header cell, and on a sortable column that
      // cell reacts to clicks. Sorting a table because somebody grabbed its
      // edge is the kind of surprise that makes people stop grabbing edges.
      onClick: (event) => event.stopPropagation(),
    }),
    [
      onKeyDown,
      onPointerCancel,
      onPointerDown,
      onPointerMove,
      onPointerUp,
      resetColumn,
      resizing,
      syncValue,
      syncValueAfterCommit,
      widths,
    ],
  );

  return {
    widths: applied,
    isActive,
    minTableWidth,
    resizing,
    headerRef,
    colRef,
    tableRef,
    resizerProps,
    resetAll,
    focusFirstResizer,
  };
}
