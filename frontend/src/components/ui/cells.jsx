/**
 * Table cell primitives.
 *
 * `NullableCell` is contract rule 11.2 made mechanical: a `null` numeric renders
 * as an em dash with a tooltip saying it could not be read, and NEVER as `0`.
 *
 * The whole backend is written to keep that distinction — `pod_count` is `null`
 * rather than `0` when the pod listing failed, `restarts_24h` is `null` when pod
 * data was unavailable, node `requested` is `null` rather than `0` when the
 * per-node pod listing was denied. All of that care is undone by one `{value ||
 * 0}` in a cell, which is exactly how a node whose pods we could not read once
 * rendered as an idle node with capacity to spare. Pages must route every
 * nullable number through here rather than defaulting it at the call site.
 */
import { Link } from 'react-router-dom';
import { Tooltip } from '@patternfly/react-core';
import { formatAge, isMissing } from '../../utils/format';

// Em dash, not a hyphen and not "N/A": visually distinct from a real value at a
// glance down a column of numbers.
const EM_DASH = '—';

const DEFAULT_REASON = 'This value could not be read, so it is unknown — it is not zero.';

/**
 * Renders a value that may legitimately be absent.
 *
 *   <NullableCell value={node.pod_count} />
 *   <NullableCell value={formatCpu(node.requested?.cpu_cores)} unit="cores" />
 *   <NullableCell value={row.restarts_24h} reason="Pod data was unavailable for this workload." />
 *
 * `0` and `false` render as themselves — they are answers. Only `null`,
 * `undefined` and `NaN` render as the dash.
 */
export function NullableCell({ value, unit, reason, format, ariaLabel }) {
  if (isMissing(value)) {
    const explanation = reason ? `${DEFAULT_REASON} ${reason}` : DEFAULT_REASON;
    return (
      <Tooltip content={explanation}>
        <span
          className="admin-nullable"
          data-testid="nullable-cell"
          data-nullable="true"
          // Screen readers get the sentence, not a lone dash character, which
          // several announce as nothing at all.
          aria-label={ariaLabel || `Unknown: ${explanation}`}
          tabIndex={0}
        >
          {EM_DASH}
        </span>
      </Tooltip>
    );
  }

  const rendered = format ? format(value) : value;
  // A formatter that returned null for a present value is still a "we cannot
  // display this", so it takes the same path rather than rendering "null".
  if (isMissing(rendered)) {
    return (
      <Tooltip content={DEFAULT_REASON}>
        <span className="admin-nullable" data-testid="nullable-cell" data-nullable="true" tabIndex={0}>
          {EM_DASH}
        </span>
      </Tooltip>
    );
  }

  return (
    <span className="admin-nullable" data-testid="nullable-cell" data-nullable="false">
      {rendered}
      {unit ? <span className="admin-nullable__unit"> {unit}</span> : null}
    </span>
  );
}

/**
 * Age from `age_seconds`, or from an RFC 3339 timestamp when that is all a row
 * carries. `age_seconds` wins when both are present: it is computed against the
 * API server's clock, and a browser several minutes out of sync produced ages
 * that disagreed with `kubectl get` — including negative ones.
 */
export function AgeCell({ seconds, timestamp, reason }) {
  let value = null;
  if (!isMissing(seconds)) {
    value = formatAge(seconds);
  } else if (timestamp) {
    const parsed = Date.parse(timestamp);
    if (!Number.isNaN(parsed)) value = formatAge(Math.max(0, (Date.now() - parsed) / 1000));
  }
  const title = timestamp || undefined;
  if (value == null) return <NullableCell value={null} reason={reason} />;
  return (
    <span className="admin-age" title={title}>
      {value}
    </span>
  );
}

// Kinds this console has a dedicated detail route for. Anything else goes to
// the generic explorer, and anything we cannot address at all renders as plain
// text — a link that navigates to a 404 is worse than no link.
const WORKLOAD_PLURALS = new Set([
  'deployments',
  'statefulsets',
  'daemonsets',
  'jobs',
  'cronjobs',
  'replicasets',
]);

const KIND_TO_PLURAL = {
  Deployment: 'deployments',
  StatefulSet: 'statefulsets',
  DaemonSet: 'daemonsets',
  Job: 'jobs',
  CronJob: 'cronjobs',
  ReplicaSet: 'replicasets',
};

/**
 * Link to whatever page in this console can show the named object.
 *
 *   <ResourceLink kind="Deployment" namespace="prod" name="checkout" />
 *   <ResourceLink kind="Node" name="ip-10-0-1-4" />
 *   <ResourceLink group="apps" version="v1" plural="deployments" name="checkout" namespace="prod" />
 *   <ResourceLink to="/audit?actor=erens" name="12 writes" />
 */
export function ResourceLink({ kind, name, namespace, plural, group, version, to, className, children }) {
  const label = children ?? name;
  if (name == null && !children) return <NullableCell value={null} />;

  // The name alone when there is no kind to qualify it, rather than no title at
  // all. In a compact table the cell is one line and a long name is clipped
  // with an ellipsis; the pod listing passes group/version/plural rather than a
  // kind, so without this fallback those were exactly the rows whose full name
  // had nowhere left to be read.
  const title = kind && name ? `${kind} ${name}` : name ?? undefined;

  const href = to ?? deriveHref({ kind, name, namespace, plural, group, version });
  if (!href) {
    return (
      <span className={className} title={title}>
        {label}
      </span>
    );
  }
  return (
    <Link to={href} className={className} title={title}>
      {label}
    </Link>
  );
}

function deriveHref({ kind, name, namespace, plural, group, version }) {
  if (!name) return null;
  if (kind === 'Node' || plural === 'nodes') return `/nodes/${encodeURIComponent(name)}`;

  // Pods have their own page (§7.5) rather than a row in the generic explorer:
  // it is where the logs, the terminal, the environment and the metrics live,
  // and every pod link in the console — the pod table, a node's pod list, a
  // workload's — should land on the same place.
  if ((kind === 'Pod' || plural === 'pods') && namespace) {
    return `/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;
  }

  const resolvedPlural = plural || KIND_TO_PLURAL[kind] || null;
  if (resolvedPlural && WORKLOAD_PLURALS.has(resolvedPlural) && namespace) {
    return `/workloads/${resolvedPlural}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`;
  }

  // Generic explorer needs the full group/version/plural triple; with anything
  // missing we cannot build a URL that resolves, so we do not pretend to.
  if (resolvedPlural && group != null && version) {
    const wireGroup = group === '' ? 'core' : group;
    const params = new URLSearchParams({ name });
    if (namespace) params.set('namespace', namespace);
    return `/explorer/${encodeURIComponent(wireGroup)}/${encodeURIComponent(version)}/${encodeURIComponent(
      resolvedPlural,
    )}?${params.toString()}`;
  }
  return null;
}
