/**
 * Facet filters — the Filter menu above a table, and what its counts mean.
 *
 * A column declares itself filterable and `DataTable` grows a Filter menu of
 * checkboxes for it, each with the number of rows it would leave on screen.
 * The whole of this module is pure: it takes columns and rows and returns
 * options and counts. Nothing here reads a cluster, and nothing here holds
 * state — which is what makes the counting rules below testable rather than
 * merely intended.
 *
 *   { key: 'phase', title: 'Status', facet: {
 *       value: (row) => row.phase_detail || row.phase,
 *       options: [
 *         'Running', 'Pending', 'Succeeded', 'Failed',
 *         { value: 'Unhealthy', label: 'Not healthy (any reason)',
 *           match: (row) => !['Running', 'Succeeded'].includes(state(row)) },
 *       ],
 *   }}
 *
 * Four rules, each of which is this project's "empty is never blind" applied to
 * a filter menu:
 *
 * **A declared option with no rows is shown, at zero.** "Pending 0" is an
 * answer — we looked, and there are none. Dropping the row from the menu makes
 * "none pending" and "this console does not track pending" look identical.
 *
 * **A value in the data that nobody declared is shown too.** A container state
 * we have never seen before must be filterable the moment a cluster produces
 * one, or the menu quietly becomes a list of the states we thought of.
 *
 * **A facet's own selection does not shrink its own counts.** Counting each
 * facet against every filter *except* its own is what keeps the other numbers
 * in that menu meaningful: with "Failed" ticked, "Pending 3" still says what
 * ticking Pending as well would add. Counted the naive way every unticked
 * option reads 0, and the menu says the cluster has nothing else on it.
 *
 * **Counts are counts of rows we hold.** A truncated listing is fewer rows than
 * the cluster has, so these are not cluster totals and the menu says so where
 * it shows them. `DataTable` renders the sentence; this module only ever counts
 * what it was handed.
 */

/** The row accessor a column already uses for sorting and searching. */
function columnValue(column) {
  return (row) => {
    if (typeof column.value === 'function') return column.value(row);
    return row?.[column.key];
  };
}

function normaliseOption(option) {
  if (option == null) return null;
  if (typeof option === 'string' || typeof option === 'number') {
    return { value: String(option), label: String(option), match: null, description: undefined };
  }
  if (option.value == null) return null;
  return {
    value: String(option.value),
    label: option.label ?? String(option.value),
    match: typeof option.match === 'function' ? option.match : null,
    description: option.description,
  };
}

/**
 * The facets a column set declares, in column order.
 *
 * Read from every column, including ones the operator has hidden through
 * Manage columns. Filtering by something you chose not to display is a
 * legitimate thing to want — and the alternative, dropping the filter when the
 * column goes, would silently put rows back on screen.
 */
export function readFacets(columns) {
  return (columns ?? [])
    .filter((column) => column?.facet)
    .map((column) => {
      const spec = column.facet === true ? {} : column.facet;
      return {
        key: column.key,
        label: spec.label ?? (typeof column.title === 'string' ? column.title : column.key),
        valueOf: typeof spec.value === 'function' ? spec.value : columnValue(column),
        declared: (spec.options ?? []).map(normaliseOption).filter(Boolean),
        /** Shown under the option list; for saying what a count is a count of. */
        note: spec.note,
      };
    });
}

/** Every value a row carries for a facet — plural, because a row may hold a list. */
function valuesFor(facet, row) {
  const raw = facet.valueOf(row);
  const list = Array.isArray(raw) ? raw : [raw];
  return list.filter((value) => value != null && value !== '').map(String);
}

/** Does this row belong under this option? */
export function optionMatches(facet, option, row) {
  // A declared `match` wins, and is the only way to express an option that is
  // not a value at all — "not healthy (any reason)" is a predicate over the
  // row, and it has to keep matching states nobody has enumerated.
  if (option.match) return Boolean(option.match(row));
  return valuesFor(facet, row).includes(option.value);
}

/**
 * The option list for a facet: everything declared, in declaration order, then
 * everything the rows turned out to hold, alphabetically.
 *
 * Alphabetical rather than by count, because the menu is re-derived on every
 * 30-second poll: ordering by count moves an option out from under the pointer
 * whenever a pod restarts.
 */
export function facetOptions(facet, rows) {
  const options = [...facet.declared];
  const declared = new Set(options.map((option) => option.value));

  const discovered = new Set();
  for (const row of rows ?? []) {
    for (const value of valuesFor(facet, row)) {
      if (!declared.has(value)) discovered.add(value);
    }
  }

  return [
    ...options,
    ...[...discovered]
      .sort((a, b) => a.localeCompare(b))
      .map((value) => ({ value, label: value, match: null })),
  ];
}

/** Rows that survive one facet's selected values. Within a facet, values OR. */
function applyFacet(rows, facet, selected, options) {
  if (!selected?.length) return rows;
  const chosen = options.filter((option) => selected.includes(option.value));
  // A selection whose option has vanished from the data filters nothing, rather
  // than filtering everything: the chip still says it is set, and an empty
  // table under a filter that no longer exists is the confident-empty failure.
  if (!chosen.length) return rows;
  return rows.filter((row) => chosen.some((option) => optionMatches(facet, option, row)));
}

/**
 * Rows that survive every facet.
 *
 * `except` is the facet whose own selection is being ignored — that is how the
 * counts in one menu stay meaningful while it has a selection of its own.
 */
export function applyFacets(rows, facets, selections, { except = null } = {}) {
  let out = rows ?? [];
  for (const facet of facets) {
    if (facet === except) continue;
    out = applyFacet(out, facet, selections[facet.key], facetOptions(facet, rows));
  }
  return out;
}

/** A facet's options with the count each would leave on screen. */
export function facetOptionsWithCounts(facet, rows, facets, selections) {
  const options = facetOptions(facet, rows);
  const countable = applyFacets(rows, facets, selections, { except: facet });
  return options.map((option) => ({
    ...option,
    count: countable.filter((row) => optionMatches(facet, option, row)).length,
  }));
}

/** Every selected value across every facet, as chips: `[{facetKey, value, label}]`. */
export function selectedChips(facets, selections, rows) {
  const chips = [];
  for (const facet of facets) {
    const selected = selections[facet.key] ?? [];
    if (!selected.length) continue;
    const options = facetOptions(facet, rows);
    for (const value of selected) {
      const option = options.find((candidate) => candidate.value === value);
      chips.push({
        facetKey: facet.key,
        facetLabel: facet.label,
        value,
        label: option?.label ?? value,
      });
    }
  }
  return chips;
}

/** Toggle one value in a selection map, returning a new map. */
export function toggleSelection(selections, facetKey, value) {
  const current = selections[facetKey] ?? [];
  const next = current.includes(value)
    ? current.filter((candidate) => candidate !== value)
    : [...current, value];
  const out = { ...selections };
  if (next.length) out[facetKey] = next;
  else delete out[facetKey];
  return out;
}

/** How many values are selected across every facet. */
export function selectionCount(selections) {
  return Object.values(selections ?? {}).reduce((total, values) => total + (values?.length ?? 0), 0);
}
