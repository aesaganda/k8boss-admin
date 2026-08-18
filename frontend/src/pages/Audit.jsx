/**
 * Audit — the write trail (§10).
 *
 * Append-only, and every attempted write is in it: `applied`, `dry_run`,
 * `denied`, `failed` and `conflict`. That vocabulary is the point of the page.
 * A trail of only the successful writes answers "what changed" but not "who
 * tried", and after an incident the second question is the one being asked —
 * so the outcome column is a first-class filter and the denials are as visible
 * as the applies.
 *
 * ## The scoping caveat, stated rather than hidden
 *
 * §10 is explicit that **`cluster_id` means something different here**: omitted
 * means *every* cluster, because "has anyone touched production?" answered with
 * a confident no while the row sits two clusters away is precisely the failure
 * this project is built against.
 *
 * `api/client.js` cannot express that. `buildUrl` appends
 * `?cluster_id=<active>` to every request whose query does not already carry
 * one, and its query builder drops `null` and `''` — so from a session with an
 * active cluster there is no value this page can pass that means "omit it".
 * Every listing it fetches is therefore scoped to one cluster, whatever §10
 * intends.
 *
 * The resolution here is to make the scope **visible and selectable** rather
 * than to let the page look cross-cluster while being narrower than it appears:
 *
 *   - the cluster filter is always populated and always says which cluster's
 *     trail is on screen;
 *   - "All clusters" is offered, and is **disabled with the reason** (rule
 *     11.4) whenever a cluster is active — with the precise condition under
 *     which it works, since with no active cluster the client appends nothing
 *     and the backend's every-cluster behaviour is reachable;
 *   - a persistent inline note states the scope above the table.
 *
 * That is a smaller page than §10 describes, and it says so. A page that
 * silently answered a narrower question would be the defect.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { audit as auditApi, getActiveClusterId } from '../api/client';
import { PageHeader } from '../components/ui';
import { useAuth } from '../contexts/AuthContext';
import { useCluster } from '../contexts/ClusterContext';
import { formatTimestamp } from '../utils/format';

// §10's outcome vocabulary, in the order an incident review reads them.
const OUTCOMES = [
  { value: '', label: 'Any outcome' },
  { value: 'applied', label: 'Applied — the cluster changed' },
  { value: 'dry_run', label: 'Dry run — nothing was written' },
  { value: 'denied', label: 'Denied — preflight or the API server refused it' },
  { value: 'failed', label: 'Failed — it was attempted and did not complete' },
  { value: 'conflict', label: 'Conflict — the object had moved' },
];

const SINCE_OPTIONS = [
  { value: '', label: 'Any time' },
  { value: '1', label: 'Last hour' },
  { value: '24', label: 'Last 24 hours' },
  { value: '168', label: 'Last 7 days' },
  { value: '720', label: 'Last 30 days' },
];

const PAGE_SIZE = 100;

function sinceTimestamp(hours) {
  if (!hours) return undefined;
  return new Date(Date.now() - Number(hours) * 3600 * 1000).toISOString();
}

/**
 * `target` in its Kubernetes spelling. §10 stores the real group name — the
 * empty string for the core group — because the audit row records what was
 * addressed on the API server, not the §1.4 URL encoding. Rendering it needs
 * the fallback that `core` in a URL provides.
 */
function targetText(target) {
  if (!target) return null;
  const group = target.group || 'core';
  const sub = target.subresource ? `/${target.subresource}` : '';
  const scope = target.namespace ? `${target.namespace}/` : '';
  return `${group}/${target.resource}${sub} ${scope}${target.name ?? ''}`.trim();
}

export default function Audit() {
  const { clusters, activeCluster } = useCluster();
  const { enabled: authenticationEnabled } = useAuth();

  const [actor, setActor] = useState('');
  const [actorInput, setActorInput] = useState('');
  const [outcome, setOutcome] = useState('');
  const [sinceHours, setSinceHours] = useState('');
  const [clusterFilter, setClusterFilter] = useState('');

  const [rows, setRows] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [remaining, setRemaining] = useState(null);
  const [unavailable, setUnavailable] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // The client seeds itself from localStorage before React renders, so this is
  // the value that will actually be appended to the request — not whatever the
  // context has settled on this frame.
  const clientScopedTo = getActiveClusterId();
  const scopeIsForced = clientScopedTo != null;

  const load = useCallback(
    async ({ append = false, from = null } = {}) => {
      setLoading(true);
      try {
        const params = {
          limit: PAGE_SIZE,
          cursor: from ?? undefined,
          actor: actor || undefined,
          outcome: outcome || undefined,
          since: sinceTimestamp(sinceHours),
        };
        // Only set when the operator picked a specific cluster. Leaving it
        // unset is what lets the client's own scoping apply — which is the
        // whole caveat in the docstring, and is stated on screen rather than
        // worked around here.
        if (clusterFilter) params.cluster_id = Number(clusterFilter);

        const result = await auditApi.list(params);
        const items = result?.items ?? [];
        setRows((current) => (append ? [...current, ...items] : items));
        setNextCursor(result?.continue ?? null);
        setRemaining(result?.remaining ?? null);
        // §10 says this endpoint's `unavailable` is always empty — one read
        // against the console's own database either answered or raised. It is
        // rendered anyway rather than assumed: an invariant the frontend
        // enforces by not looking is an invariant that breaks silently.
        setUnavailable(result?.unavailable ?? []);
        setError(null);
      } catch (err) {
        // The list is not cleared on an append failure — losing the page the
        // operator was reading because the *next* page failed is a worse
        // outcome than a stale list with an error above it.
        if (!append) setRows([]);
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [actor, outcome, sinceHours, clusterFilter],
  );

  // Any filter change restarts paging from the top: appending a filtered page
  // onto an unfiltered one would present two different queries as one result.
  useEffect(() => {
    load();
  }, [load]);

  const columns = useMemo(
    () => [
      {
        key: 'ts',
        title: 'When',
        sortable: true,
        width: 15,
        cell: (row) => (
          <Tooltip content={row.ts}>
            <span>{formatTimestamp(row.ts)}</span>
          </Tooltip>
        ),
      },
      {
        key: 'actor',
        title: 'Actor',
        sortable: true,
        cell: (row) => (
          <Tooltip
            content={
              authenticationEnabled
                ? `Verified console session identity.${row.source_ip ? ` Source ${row.source_ip}.` : ''}`
                : `From the advisory X-K8Boss-User header in proxy mode; this is attribution, not verified identity.${row.source_ip ? ` Source ${row.source_ip}.` : ''}`
            }
          >
            <span>{row.actor}</span>
          </Tooltip>
        ),
      },
      {
        key: 'cluster_name',
        title: 'Cluster',
        sortable: true,
        cell: (row) =>
          row.cluster_name ?? (
            // The record was written without a resolvable cluster. That is a
            // fact about the record, not a missing read, so it is not a dash.
            <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>
              {row.cluster_id != null ? `#${row.cluster_id}` : 'none'}
            </span>
          ),
      },
      { key: 'verb', title: 'Verb', sortable: true, width: 10 },
      {
        key: 'target',
        title: 'Target',
        modifier: 'breakWord',
        value: (row) => targetText(row.target),
        cell: (row) => (
          <ResourceLink
            group={row.target?.group}
            version={row.target?.version}
            plural={row.target?.resource}
            namespace={row.target?.namespace}
            name={row.target?.name}
          >
            {targetText(row.target)}
          </ResourceLink>
        ),
      },
      {
        key: 'outcome',
        title: 'Outcome',
        sortable: true,
        cell: (row) => (
          <Split hasGutter>
            <SplitItem>
              <StatusBadge status={row.outcome} />
            </SplitItem>
            {row.dry_run && row.outcome !== 'dry_run' && (
              <SplitItem>
                {/* A denied or failed dry run is still a dry run. Without this
                    the row reads as a refused write to a cluster, which is a
                    much louder fact than what happened. */}
                <StatusBadge status="dry_run" label="dry run" tooltip="Nothing was written to the cluster." />
              </SplitItem>
            )}
          </Split>
        ),
      },
      {
        key: 'detail',
        title: 'Detail',
        modifier: 'breakWord',
        cell: (row) => (
          <span>
            {row.detail}
            {row.error && (
              <div style={{ color: 'var(--pf-t--global--text--color--status--danger--default, #a30000)', fontSize: '0.8125rem' }}>
                {row.error}
              </div>
            )}
          </span>
        ),
      },
      {
        key: 'diff_digest',
        title: 'Diff digest',
        searchable: false,
        cell: (row) =>
          row.diff_digest ? (
            <Tooltip content={`${row.diff_digest} — a hash of the unified diff this write was approved against. It proves which change was applied; it does not contain the change.`}>
              <code style={{ fontSize: '0.75rem' }}>
                {String(row.diff_digest).replace(/^sha256:/, '').slice(0, 12)}
              </code>
            </Tooltip>
          ) : (
            <Tooltip content="This record has no diff digest — the write produced no diff, or failed before one was computed.">
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>—</span>
            </Tooltip>
          ),
      },
    ],
    [authenticationEnabled],
  );

  const scopeSentence = clusterFilter
    ? `Showing writes to ${clusters.find((c) => c.id === Number(clusterFilter))?.name ?? `cluster #${clusterFilter}`}.`
    : scopeIsForced
      ? `Showing writes to ${activeCluster?.name ?? `cluster #${clientScopedTo}`} only.`
      : 'Showing writes to every registered cluster.';

  return (
    <>
      <PageHeader
        title="Audit"
        subtitle="Every attempted write, including the ones that were refused. Append-only — there is no delete endpoint."
      />

      <FilterBar ariaLabel="Audit filters">
        <FilterBar.Field label="Cluster" htmlFor="audit-cluster">
          <FormSelect
            id="audit-cluster"
            value={clusterFilter}
            onChange={(_e, next) => setClusterFilter(next)}
            aria-label="Cluster"
            data-testid="audit-cluster"
          >
            <FormSelectOption
              value=""
              label={scopeIsForced ? 'All clusters (unavailable — see below)' : 'All clusters'}
              // Rule 11.4: offered, disabled, and the reason is on the page
              // directly beneath. Hiding it would make the limitation
              // invisible to the operator and to whoever maintains the client.
              isDisabled={scopeIsForced}
            />
            {clusters.map((c) => (
              <FormSelectOption key={c.id} value={String(c.id)} label={c.name} />
            ))}
          </FormSelect>
        </FilterBar.Field>

        <FilterBar.Field label="Outcome" htmlFor="audit-outcome">
          <FormSelect
            id="audit-outcome"
            value={outcome}
            onChange={(_e, next) => setOutcome(next)}
            aria-label="Outcome"
            data-testid="audit-outcome"
          >
            {OUTCOMES.map((o) => (
              <FormSelectOption key={o.value} value={o.value} label={o.label} />
            ))}
          </FormSelect>
        </FilterBar.Field>

        <FilterBar.Field label="Since" htmlFor="audit-since">
          <FormSelect
            id="audit-since"
            value={sinceHours}
            onChange={(_e, next) => setSinceHours(next)}
            aria-label="Time window"
            data-testid="audit-since"
          >
            {SINCE_OPTIONS.map((o) => (
              <FormSelectOption key={o.value} value={o.value} label={o.label} />
            ))}
          </FormSelect>
        </FilterBar.Field>

        <FilterBar.Field label="Actor" htmlFor="audit-actor">
          <Split hasGutter>
            <SplitItem>
              <TextInput
                id="audit-actor"
                value={actorInput}
                onChange={(_e, next) => setActorInput(next)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') setActor(actorInput.trim());
                }}
                // §10 filters on an exact match, so this is submitted rather
                // than applied per keystroke — a substring typed halfway
                // matches nothing and would read as "this person did nothing".
                placeholder="exact match, press Enter"
                aria-label="Actor"
                data-testid="audit-actor"
              />
            </SplitItem>
            <SplitItem>
              <Button variant="secondary" onClick={() => setActor(actorInput.trim())}>
                Apply
              </Button>
            </SplitItem>
            {actor && (
              <SplitItem>
                <Button
                  variant="link"
                  isInline
                  onClick={() => {
                    setActorInput('');
                    setActor('');
                  }}
                >
                  Clear
                </Button>
              </SplitItem>
            )}
          </Split>
        </FilterBar.Field>
      </FilterBar>

      {/* The scope, stated persistently and inline. Not a toast — an operator
          reading this table an hour from now has to be able to see which
          question it answered. */}
      <Alert
        isInline
        variant={scopeIsForced && !clusterFilter ? 'warning' : 'info'}
        title={scopeSentence}
        data-testid="audit-scope"
      >
        {scopeIsForced && !clusterFilter ? (
          <>
            <p>
              §10 says an audit listing with no <code>cluster_id</code> covers every cluster. This console
              cannot request that while a cluster is selected: <code>api/client.js</code> appends{' '}
              <code>cluster_id</code> to every request, and its query builder drops empty values, so there is
              no value meaning &ldquo;omit&rdquo;. Pick a cluster above to check a specific one — or clear
              the active cluster in the masthead, after which &ldquo;All clusters&rdquo; works.
            </p>
            <p>
              Until then, a row absent from this table is <strong>not</strong> evidence that nobody touched
              another cluster.
            </p>
          </>
        ) : null}
      </Alert>

      <PartialBanner unavailable={unavailable} />

      <DataTable
        columns={columns}
        rows={rows}
        rowKey="id"
        loading={loading}
        error={error}
        onRetry={() => load()}
        ariaLabel="Audit trail"
        emptyTitle="No matching audit records"
        emptyDescription={
          'Nothing in this console’s trail matches these filters. The trail records dry runs and denials ' +
          'too, so an empty result here means no attempt was made, not that no attempt succeeded.'
        }
        footer={
          <Split hasGutter style={{ alignItems: 'center', marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}>
            <SplitItem>
              <span style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem' }}>
                {rows.length} shown
                {/* §10 counts `remaining` only when a next page exists, so a
                    null here means "this is the last page", not "unknown". */}
                {remaining != null ? ` · ${remaining} older records` : nextCursor ? '' : ' · end of the trail'}
              </span>
            </SplitItem>
            <SplitItem isFilled />
            {nextCursor && (
              <SplitItem>
                <Button
                  variant="secondary"
                  isDisabled={loading}
                  onClick={() => load({ append: true, from: nextCursor })}
                  data-testid="audit-more"
                >
                  Load {Math.min(PAGE_SIZE, remaining ?? PAGE_SIZE)} more
                </Button>
              </SplitItem>
            )}
          </Split>
        }
      />
    </>
  );
}
