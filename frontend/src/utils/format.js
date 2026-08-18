/**
 * Display formatting helpers.
 *
 * Every formatter here returns `null` for a `null` input and propagates it —
 * it never substitutes a zero, a dash or an empty string of its own. Rendering
 * the absence is `NullableCell`'s job, and it needs to know the difference
 * between "the backend said 0" and "the backend said it could not look"
 * (contract §11.2). A formatter that quietly returned `'0'` for `null` would
 * erase that distinction one layer below where anyone would think to look for
 * it — which is how an unreadable node once rendered as an idle one.
 */

/** True for values that mean "not measured", as opposed to a real zero. */
export function isMissing(value) {
  return value == null || (typeof value === 'number' && Number.isNaN(value));
}

/* ── Age ────────────────────────────────────────────────────────────────── */

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const YEAR = 365 * DAY;

/**
 * kubectl-style compact age: `45s`, `12m`, `3h14m`, `5d2h`, `2y13d`.
 *
 * Two units at most, and the second is dropped when it is zero, because
 * `5d0h` reads like a rounding artefact. Negative inputs (a clock skew between
 * the API server and the browser) clamp to `0s` rather than rendering `-3s`,
 * which looked like a bug in the timestamp rather than in the clocks.
 */
export function formatAge(seconds) {
  if (isMissing(seconds)) return null;
  const s = Math.max(0, Math.floor(Number(seconds)));
  if (s < MINUTE) return `${s}s`;
  if (s < HOUR) {
    const m = Math.floor(s / MINUTE);
    const rem = s % MINUTE;
    return rem && m < 10 ? `${m}m${rem}s` : `${m}m`;
  }
  if (s < DAY) {
    const h = Math.floor(s / HOUR);
    const m = Math.floor((s % HOUR) / MINUTE);
    return m ? `${h}h${m}m` : `${h}h`;
  }
  if (s < YEAR) {
    const d = Math.floor(s / DAY);
    const h = Math.floor((s % DAY) / HOUR);
    return h && d < 10 ? `${d}d${h}h` : `${d}d`;
  }
  const y = Math.floor(s / YEAR);
  const d = Math.floor((s % YEAR) / DAY);
  return d ? `${y}y${d}d` : `${y}y`;
}

/** Seconds between an RFC 3339 timestamp and now; `null` if unparseable. */
export function ageSecondsSince(timestamp) {
  if (!timestamp) return null;
  const t = Date.parse(timestamp);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 1000));
}

/* ── Bytes ──────────────────────────────────────────────────────────────── */

const BINARY_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'];

/**
 * Binary (IEC) units, because that is what Kubernetes quantities mean: a node
 * reporting `68719476736` is 64 GiB, and printing it as "68.7 GB" makes an
 * operator's capacity arithmetic disagree with `kubectl describe node`.
 */
export function formatBytes(bytes, { precision } = {}) {
  if (isMissing(bytes)) return null;
  const n = Number(bytes);
  if (!Number.isFinite(n)) return null;
  const sign = n < 0 ? '-' : '';
  let value = Math.abs(n);
  let unit = 0;
  while (value >= 1024 && unit < BINARY_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  // Whole bytes never get a decimal point; larger units get one or two digits
  // depending on magnitude, so `1.5 GiB` and `824 GiB` both stay readable.
  const digits = precision ?? (unit === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 2);
  return `${sign}${value.toFixed(digits)} ${BINARY_UNITS[unit]}`;
}

/* ── CPU ────────────────────────────────────────────────────────────────── */

/**
 * CPU in cores, rendered the way Kubernetes talks about it.
 *
 * Below one core the millicore form is used (`250m`), because that is the unit
 * requests are actually written in and `0.25` invites the reader to compare it
 * against a `500m` limit in their head. At or above a core, decimal cores with
 * trailing zeros trimmed (`2`, `9.25`).
 */
export function formatCpu(cores) {
  if (isMissing(cores)) return null;
  const n = Number(cores);
  if (!Number.isFinite(n)) return null;
  if (n === 0) return '0';
  if (Math.abs(n) < 1) return `${Math.round(n * 1000)}m`;
  const fixed = n.toFixed(2);
  return fixed.replace(/\.?0+$/, '');
}

/** Percentage of a total, `null` when either side is missing or the total is 0. */
export function formatPercent(part, total, { digits = 0 } = {}) {
  if (isMissing(part) || isMissing(total)) return null;
  const t = Number(total);
  if (!t) return null;
  return `${((Number(part) / t) * 100).toFixed(digits)}%`;
}

/* ── Strings ────────────────────────────────────────────────────────────── */

/**
 * Middle-free truncation with an ellipsis. Returns the input untouched when it
 * already fits, so callers can pass a `title` of the original unconditionally.
 */
export function truncate(text, max = 60, ellipsis = '…') {
  if (text == null) return null;
  const s = String(text);
  if (s.length <= max) return s;
  return s.slice(0, Math.max(0, max - ellipsis.length)) + ellipsis;
}

/** `ns/name` → `name`, for image refs and long resource identifiers. */
export function lastSegment(text, separator = '/') {
  if (text == null) return null;
  const s = String(text);
  const i = s.lastIndexOf(separator);
  return i === -1 ? s : s.slice(i + 1);
}

/** `{a: '1', b: '2'}` → `a=1, b=2`. Empty object → `null`, not `''`. */
export function formatLabels(labels) {
  if (!labels || typeof labels !== 'object') return null;
  const entries = Object.entries(labels);
  if (!entries.length) return null;
  return entries.map(([k, v]) => `${k}=${v}`).join(', ');
}

/** RFC 3339 → locale string; `null` in, `null` out. */
export function formatTimestamp(ts) {
  if (!ts) return null;
  const t = Date.parse(ts);
  if (Number.isNaN(t)) return null;
  return new Date(t).toLocaleString();
}

/* ── Sorting ────────────────────────────────────────────────────────────── */

/**
 * Stable sort by a key or accessor.
 *
 * `null` always sorts last regardless of direction. A missing measurement is
 * not a small measurement: sorting nodes by "requested CPU" ascending and
 * getting the nodes we could not read at the top would put unknown capacity
 * exactly where an operator looks for idle capacity.
 *
 * Numbers compare numerically, everything else compares as a locale string, so
 * `pod-2` sorts before `pod-10`.
 */
export function sortBy(rows, key, direction = 'asc', accessor) {
  if (!Array.isArray(rows)) return [];
  if (!key && !accessor) return rows;
  const get = accessor || ((row) => row?.[key]);
  const dir = direction === 'desc' ? -1 : 1;
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

  // Decorate with the original index so ties keep their incoming order —
  // Array.prototype.sort is only specified as stable for the comparator's own
  // equality, and a table that reshuffled equal rows on every re-render made
  // rows appear to jump under the cursor.
  return rows
    .map((row, index) => ({ row, index, value: get(row) }))
    .sort((a, b) => {
      const aMissing = isMissing(a.value);
      const bMissing = isMissing(b.value);
      if (aMissing && bMissing) return a.index - b.index;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (typeof a.value === 'number' && typeof b.value === 'number') {
        return (a.value - b.value) * dir || a.index - b.index;
      }
      if (typeof a.value === 'boolean' && typeof b.value === 'boolean') {
        return (Number(a.value) - Number(b.value)) * dir || a.index - b.index;
      }
      return collator.compare(String(a.value), String(b.value)) * dir || a.index - b.index;
    })
    .map((entry) => entry.row);
}
