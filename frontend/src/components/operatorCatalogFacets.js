/**
 * The catalog grid's pure parts — what a row is, and which rows survive a filter.
 *
 * Split from `OperatorCatalog.jsx` for the reason `tableFilters.js` is split
 * from the table: these are functions, the file next door exports components,
 * and a module that exports both stops fast refresh working on the components.
 *
 * Everything here is pure. The tri-state vocabulary is the contract's — the
 * three words are the same three the table's facet uses, because a package that
 * filtered differently depending on the view would read as a package that
 * changed.
 */

/** The three states of a §16.3 row's `installed`, in the table's vocabulary. */
export const INSTALLED_STATES = ['Installed', 'Not installed', 'Unknown'];

/**
 * Which of the three a row is in.
 *
 * `null` is `Unknown` and is never folded into `Not installed`: the Subscription
 * listing did not answer, and a filter that hid that distinction would hand
 * somebody a page inviting the second Subscription §16.3 exists to prevent.
 */
export function installedState(row) {
  if (row.installed === true) return 'Installed';
  if (row.installed === false) return 'Not installed';
  return 'Unknown';
}

export const EMPTY_FILTERS = {
  category: null,
  sources: [],
  providers: [],
  capabilities: [],
  installed: [],
};

/**
 * The left rail's options, counted off the rows themselves.
 *
 * Counted rather than enumerated from a fixed list: these are somebody else's
 * catalogs, and a category this console hard-coded would be a filter that finds
 * nothing on a cluster whose operators use a vocabulary we did not predict.
 */
export function catalogFacets(rows) {
  const count = (key) => {
    const tally = new Map();
    rows.forEach((row) => {
      const values = key(row);
      (Array.isArray(values) ? values : [values])
        .filter((v) => v != null && v !== '')
        .forEach((v) => tally.set(v, (tally.get(v) ?? 0) + 1));
    });
    return [...tally.entries()]
      .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))
      .map(([value, n]) => ({ value, count: n }));
  };

  return {
    categories: count((row) => row.categories ?? []),
    sources: count((row) => row.catalogDisplayName || row.catalog),
    providers: count((row) => row.provider),
    capabilities: count((row) => row.capabilityLevel),
    installed: count((row) => installedState(row)),
  };
}

/** Does one row survive the current filter selection? */
export function matchesFilters(row, filters) {
  if (filters.category && !(row.categories ?? []).includes(filters.category)) return false;
  if (filters.sources.length && !filters.sources.includes(row.catalogDisplayName || row.catalog)) {
    return false;
  }
  if (filters.providers.length && !filters.providers.includes(row.provider)) return false;
  if (filters.capabilities.length && !filters.capabilities.includes(row.capabilityLevel)) {
    return false;
  }
  if (filters.installed.length && !filters.installed.includes(installedState(row))) return false;
  return true;
}

/**
 * Does a row survive the toolbar's search box?
 *
 * The table gets this for free from `DataTable`'s `filterText`; the tile grid is
 * not a table and has to do it here. Matching the same fields the table's
 * columns render keeps one search box honest across both views — a term that
 * found a package in one and not the other would read as a missing package.
 */
export function matchesSearch(row, text) {
  const needle = (text ?? '').trim().toLowerCase();
  if (!needle) return true;
  return [
    row.name,
    row.displayName,
    row.provider,
    row.summary,
    row.catalogDisplayName,
    row.catalog,
    ...(row.categories ?? []),
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(needle));
}

/**
 * A hue from the package name, for the placeholder tile.
 *
 * Deterministic on purpose: an operator whose colour changed between reloads
 * would be a different tile each time, which is the opposite of what a
 * placeholder is for. Nothing is claimed by the colour — it carries no meaning
 * and must not be read as one, which is why it is a hue and not the console's
 * status palette.
 */
export function hueOf(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) % 360;
  }
  return hash;
}
