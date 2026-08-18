/**
 * PartialBanner — contract rule 11.1, made mechanical.
 *
 * Every list endpoint answers with `{items, partial, unavailable[]}` (§1.2).
 * `items: []` means the cluster has none; `partial: true` means we could not
 * look at some of it. Those two render identically in a table — an empty grid —
 * so the only thing that keeps them apart on screen is this banner.
 *
 * It is an inline `Alert`, never a toast, and it has no auto-dismiss. The bug
 * this exists to prevent: a Secrets listing that was forbidden in `prod`
 * reported "no secrets found" for six seconds of toast and then looked, for the
 * rest of the session, like a namespace with no secrets in it. Absence is not
 * safety; it has to stay on the page next to the data it qualifies.
 *
 * One row per `unavailable` entry, naming the resource, the namespace and the
 * reason — not a count, because "3 resources unavailable" tells an operator
 * nothing they can act on.
 *
 * `unsupported` is explicitly not an error (§1.2): a cluster with no Ingress
 * CRDs is a normal cluster. A banner made only of `unsupported` entries is
 * informational and stays blue; one red row is enough to colour the whole
 * banner, because the most severe thing we failed at is what the operator
 * needs to see first.
 */
import { useState } from 'react';
import { Alert, Button } from '@patternfly/react-core';

/**
 * Reason vocabulary from §1.2. `label` is the human sentence; `variant` is how
 * loudly to say it. Callers branch on `reason` and never parse `detail`.
 */
const REASONS = {
  forbidden: {
    variant: 'warning',
    label: 'not permitted to read',
    explain: 'The console ServiceAccount lacks list permission here, so these objects are not in the list below.',
  },
  not_found: {
    variant: 'warning',
    label: 'not found',
    explain: 'The object or API resource no longer exists.',
  },
  unreachable: {
    variant: 'danger',
    label: 'unreachable',
    explain: 'The API server could not be reached, so this data is missing rather than absent.',
  },
  timeout: {
    variant: 'danger',
    label: 'timed out',
    explain: 'The request did not complete in time. What is shown below may be incomplete.',
  },
  not_registered: {
    variant: 'warning',
    label: 'cluster not registered',
    explain: 'No cluster is registered for this scope, so nothing was queried.',
  },
  unsupported: {
    variant: 'info',
    label: 'not present on this cluster',
    explain: 'This cluster does not serve that API. Nothing is wrong — there is simply nothing to show.',
  },
};

const SEVERITY = { danger: 3, warning: 2, info: 1 };

function describe(entry) {
  const spec = REASONS[entry?.reason] ?? {
    variant: 'warning',
    // An unrecognised reason is surfaced verbatim rather than mapped to
    // "unknown error": a backend that grew a new reason code must not be
    // silently flattened into a vaguer one by an older frontend.
    label: entry?.reason ? String(entry.reason) : 'unavailable',
    explain: null,
  };
  const group = entry?.group === '' || entry?.group == null ? 'core' : entry.group;
  const resource = entry?.resource ? `${group}/${entry.resource}` : group;
  const scope = entry?.namespace ? ` in namespace ${entry.namespace}` : '';
  return { ...spec, headline: `${resource}${scope} — ${spec.label}`, detail: entry?.detail || null };
}

export function PartialBanner({ unavailable, title, className }) {
  const entries = Array.isArray(unavailable) ? unavailable.filter(Boolean) : [];
  const [showDetail, setShowDetail] = useState(false);

  // `partial` is defined as "unavailable is non-empty" (§1.2), so the array is
  // the single source of truth here. Taking a separate `partial` boolean as
  // well would create a state where they disagree and the banner has to pick.
  if (!entries.length) return null;

  const described = entries.map(describe);
  const variant = described.reduce(
    (worst, e) => (SEVERITY[e.variant] > SEVERITY[worst] ? e.variant : worst),
    'info',
  );

  const heading =
    title ??
    (variant === 'info'
      ? 'Some resources are not served by this cluster'
      : `This view is incomplete — ${entries.length} ${entries.length === 1 ? 'source' : 'sources'} could not be read`);

  return (
    <Alert
      isInline
      variant={variant}
      title={heading}
      className={className}
      data-testid="partial-banner"
      // No `timeout`. This must not disappear on its own; see the file docstring.
    >
      <ul className="admin-partial__list">
        {described.map((entry, i) => (
          <li key={`${entry.headline}-${i}`} className="admin-partial__row" data-testid="partial-banner-row">
            <span className="admin-partial__headline">{entry.headline}</span>
            {entry.explain && <span className="admin-partial__explain"> {entry.explain}</span>}
            {showDetail && entry.detail && (
              // The API server's own words, shown only on request. §1.2 is
              // explicit that `detail` is for humans and must never be parsed,
              // so it is rendered as opaque text and nothing keys off it.
              <pre className="admin-partial__detail">{entry.detail}</pre>
            )}
          </li>
        ))}
      </ul>
      {described.some((e) => e.detail) && (
        <Button variant="link" isInline onClick={() => setShowDetail((v) => !v)}>
          {showDetail ? 'Hide server messages' : 'Show server messages'}
        </Button>
      )}
    </Alert>
  );
}

export default PartialBanner;
