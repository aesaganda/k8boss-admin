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
 * ## Scope, stated rather than assumed
 *
 * §10 is explicit that **`cluster_id` means something different here**: omitted
 * means *every* cluster, because "has anyone touched production?" answered with
 * a confident no while the row sits two clusters away is precisely the failure
 * this project is built against.
 *
 * That used to be unreachable from this page. `buildUrl` appends
 * `?cluster_id=<active>` to every request whose query does not already carry
 * one and drops empty values, so from a session with a cluster selected there
 * was no value meaning "omit it" — and this page said so on screen rather than
 * looking cross-cluster while being narrower than it appeared.
 *
 * §10 now defines `cluster_id=0` as "records that belong to no cluster", which
 * the query builder *can* express, so the scope selector is a real filter with
 * three positions: every cluster, one cluster, or the console's own records.
 * That last one matters more than it sounds: sign-ins carry no cluster at all,
 * so without it every authentication record on this page would be invisible
 * while the page looked like it was working.
 *
 * ## Integrity, and the three answers it can give
 *
 * The trail is append-only through this application and hash-chained against
 * everything else. `intact`, `broken` and `partial` are rendered as three
 * distinct states, and `partial` is never dressed up as a pass: it means records
 * exist that the chain cannot speak for, and an operator being shown a green
 * badge over those would be exactly the confidently-wrong answer this project
 * is built against.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  FormSelect,
  FormSelectOption,
  Split,
  SplitItem,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';

import { audit as auditApi } from '../api/client';
import {
  DataTable,
  FilterBar,
  PageHeader,
  PartialBanner,
  ResourceLink,
  StatusBadge,
} from '../components/ui';
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

// §10's two record kinds. "Any" first, because an incident review that starts by
// excluding half the trail starts by excluding the half it does not expect.
const CATEGORIES = [
  { value: '', label: 'Everything' },
  { value: 'cluster', label: 'Cluster writes' },
  { value: 'console', label: 'Console sign-ins and user changes' },
];

// The scope selector's third position. 0 is §10's sentinel for "no cluster",
// which is what every console record carries.
const UNSCOPED = '0';

const PAGE_SIZE = 100;

/**
 * How to render a chain verdict. Three states, and `partial` is its own — not a
 * gentler `intact`.
 */
const CHAIN_STATES = {
  intact: {
    variant: 'success',
    title: 'The audit trail verifies',
    body:
      'Every record is hash-chained, every hash recomputes, and the links run unbroken '
      + 'from the first record to the last. Nothing has been edited, deleted or reordered '
      + 'since it was written.',
  },
  broken: {
    variant: 'danger',
    title: 'The audit trail does not verify',
    body:
      'A record no longer matches its hash, or a record cannot be reached from the first '
      + 'one. That is evidence of modification, deletion or insertion after the fact — by '
      + 'something with direct database access, since this console cannot rewrite the '
      + 'table. Treat the trail as compromised from the named record onward.',
  },
  partial: {
    variant: 'warning',
    title: 'The audit trail verifies as far as it can be checked',
    body:
      'No break was found. Some records are outside the chain and cannot be verified at '
      + 'all: records written before hash chaining existed, and any the console had to '
      + 'store unlinked. They are deliberately not back-filled — hashing them now would '
      + 'attest whatever they say today, turning "we do not know" into proof.',
  },
};

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

/**
 * A console record's target in the console's own terms.
 *
 * Console records are filed under a synthetic group (`k8boss-admin.io`) so they
 * share one row shape with cluster writes. That shape must not be handed to
 * `ResourceLink`: it would build `/explorer/k8boss-admin.io/v1/sessions`, a
 * perfectly well-formed link to a Kubernetes API group no cluster serves. An
 * operator who clicks a sign-in row would land on "this cluster does not serve
 * that API resource" — a confident answer to a question they did not ask.
 */
function consoleTargetText(target) {
  if (!target) return null;
  const what = target.resource === 'sessions' ? 'session' : target.resource;
  return target.name ? `${what} · ${target.name}` : what;
}

/**
 * The chain verdict, or an invitation to ask for one.
 *
 * Rendered as three distinct states plus "not checked yet". The one thing this
 * component must never do is let `partial` read as a pass: it means records
 * exist that the chain cannot speak for, and a green badge over those would be
 * the console asserting something it has no basis for.
 *
 * `unchained` is always shown when it is non-zero, including under `intact` —
 * which cannot happen today, but a count that only appears in the bad case is a
 * count nobody learns to read.
 */
function IntegrityPanel({ chain, error, loading, onVerify }) {
  if (error) {
    return (
      <Alert
        isInline
        variant="danger"
        title="The integrity check could not run"
        data-testid="audit-chain"
      >
        <p>{error.message}</p>
        {/* Deliberately not falling back to a previous verdict. A stale
            "verified" over a check that just failed is a claim about records
            nobody read. */}
        <p>
          This says nothing about whether the trail is intact — only that the check did not
          complete.
        </p>
        <Button variant="link" isInline onClick={onVerify}>
          Try again
        </Button>
      </Alert>
    );
  }

  if (!chain) {
    return (
      <Alert
        isInline
        variant="info"
        title="Audit trail integrity has not been checked in this session"
        data-testid="audit-chain"
      >
        <p>
          Records are hash-chained: editing, deleting or reordering one after it was written
          breaks the chain and can be detected. The check walks the whole trail, so it runs on
          request rather than on every page load.
        </p>
        <Button variant="secondary" isLoading={loading} isDisabled={loading} onClick={onVerify} data-testid="audit-verify">
          Check integrity
        </Button>
      </Alert>
    );
  }

  const state = CHAIN_STATES[chain.status] ?? {
    variant: 'warning',
    title: `Unrecognised integrity verdict: ${chain.status}`,
    body:
      'This console does not know how to interpret that verdict, so it is shown verbatim '
      + 'rather than translated into a reassurance it cannot support.',
  };

  return (
    <Alert isInline variant={state.variant} title={state.title} data-testid="audit-chain">
      <p>{state.body}</p>
      <p>
        <strong>{chain.verified}</strong> of <strong>{chain.total}</strong> records verified
        {chain.unchained > 0 && (
          <>
            {' · '}
            <strong data-testid="audit-chain-unchained">{chain.unchained}</strong> outside the
            chain and unverifiable
          </>
        )}
        {chain.anchored === false && ' · checked as a window, not from the first record'}
      </p>
      {chain.first_break && (
        <p data-testid="audit-chain-break">
          First break at record <strong>#{chain.first_break.id}</strong>: {chain.first_break.reason}
        </p>
      )}
      <Button variant="link" isInline onClick={onVerify} isDisabled={loading}>
        Check again
      </Button>
    </Alert>
  );
}

export default function Audit() {
  const { clusters } = useCluster();
  const { enabled: authenticationEnabled, user } = useAuth();

  // §10.4 is administrator-only when application authentication is on; with it
  // off there is no console role and the proxy in front owns the decision, so
  // the buttons stay live. Rule 11.4: a control the caller cannot use is
  // **disabled with the reason**, never hidden — an operator has to be able to
  // see that the export exists and why it is unavailable, and a live button that
  // navigates to a raw 403 JSON page is the worse of the two failures.
  const mayExport = !authenticationEnabled || user?.role === 'admin';
  const exportReason = mayExport
    ? 'Downloads every record matching the filters above. No row cap.'
    : 'Exporting the whole trail is administrator-only: one request returns every '
      + 'actor, source address and action. Your console role is '
      + `${user?.role ?? 'unknown'}.`;

  const [actor, setActor] = useState('');
  const [actorInput, setActorInput] = useState('');
  const [outcome, setOutcome] = useState('');
  const [sinceHours, setSinceHours] = useState('');
  const [clusterFilter, setClusterFilter] = useState('');
  const [category, setCategory] = useState('');

  const [rows, setRows] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [remaining, setRemaining] = useState(null);
  const [unavailable, setUnavailable] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [chain, setChain] = useState(null);
  const [chainError, setChainError] = useState(null);
  const [chainLoading, setChainLoading] = useState(false);

  // The filters, in the shape both the listing and the export take. One object
  // so the downloaded file cannot describe a different query from the table it
  // was taken from — an export that quietly widened the window would be found,
  // if ever, by somebody comparing row counts months later.
  const filters = useMemo(
    () => ({
      actor: actor || undefined,
      outcome: outcome || undefined,
      category: category || undefined,
      since: sinceTimestamp(sinceHours),
      // '' means "every cluster" and is omitted; '0' is §10's sentinel for the
      // records that belong to no cluster and must survive as a real 0.
      cluster_id: clusterFilter === '' ? undefined : Number(clusterFilter),
    }),
    [actor, outcome, category, sinceHours, clusterFilter],
  );

  const load = useCallback(
    async ({ append = false, from = null } = {}) => {
      setLoading(true);
      try {
        const params = { ...filters, limit: PAGE_SIZE, cursor: from ?? undefined };

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
        //
        // The paging state IS cleared on a first-page failure, and that half
        // matters more. `continue` is an opaque "id < N" cursor tied to the
        // query that produced it: kept across a failed filter change, the
        // footer would still offer "Load more", and clicking it would fetch the
        // NEW filter's records older than the OLD filter's last row — silently
        // omitting every matching record newer than that id. An operator who
        // filtered to `denied`, saw the load fail, and paged onward would be
        // shown a list missing exactly the most recent denials, with nothing
        // indicating a gap.
        if (!append) {
          setRows([]);
          setNextCursor(null);
          setRemaining(null);
          setUnavailable([]);
        }
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [filters],
  );

  // Any filter change restarts paging from the top: appending a filtered page
  // onto an unfiltered one would present two different queries as one result.
  useEffect(() => {
    load();
  }, [load]);

  /**
   * Ask the backend to walk the hash chain.
   *
   * Not run on mount. It is a full scan of a table that only grows, and firing
   * it every time somebody opens the page would make the audit view the most
   * expensive request the console makes. It is also not a number anyone needs
   * continuously — it is a question asked deliberately, usually during an
   * incident, so it is a button.
   *
   * A failure sets `chainError` and leaves `chain` alone. Rendering a stale
   * "verified" badge over a check that just failed would be a claim the console
   * cannot support, and a blank panel would not say that anything went wrong.
   */
  const verify = useCallback(async () => {
    setChainLoading(true);
    setChainError(null);
    try {
      setChain(await auditApi.verify());
    } catch (err) {
      setChainError(err);
    } finally {
      setChainLoading(false);
    }
  }, []);

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
        key: 'category',
        title: 'Kind',
        sortable: true,
        width: 10,
        cell: (row) => (
          <Tooltip
            content={
              row.category === 'console'
                ? 'A console record: a sign-in, a sign-out, or a change to a console user. It belongs to no cluster.'
                : 'A write aimed at a Kubernetes API server.'
            }
          >
            <span>{row.category === 'console' ? 'Console' : 'Cluster'}</span>
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
              // A rejected sign-in has no verified identity behind it — there is
              // no session yet. The username is what the caller typed, and
              // saying so is the difference between evidence and an accusation.
              row.category === 'console' && row.outcome === 'denied'
                ? `The username submitted with a refused sign-in. Nothing verified it — this is a claim by the caller, not an identity.${row.source_ip ? ` Source ${row.source_ip}.` : ''}`
                : authenticationEnabled
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
        value: (row) =>
          row.category === 'console'
            ? consoleTargetText(row.target)
            : targetText(row.target),
        cell: (row) =>
          // Console records are plain text. See consoleTargetText: linking them
          // would send an operator to an explorer page for an API group that
          // does not exist on any cluster.
          row.category === 'console' ? (
            <span>{consoleTargetText(row.target)}</span>
          ) : (
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

  const scopeSentence =
    clusterFilter === UNSCOPED
      ? 'Showing the console’s own records — sign-ins, sign-outs and user changes. These belong to no cluster.'
      : clusterFilter
        ? `Showing records for ${clusters.find((c) => c.id === Number(clusterFilter))?.name ?? `cluster #${clusterFilter}`}.`
        : 'Showing records for every registered cluster, and the console’s own.';

  return (
    <>
      <PageHeader
        title="Audit"
        subtitle="Every attempted write and every sign-in, including the ones that were refused. Append-only — there is no delete endpoint."
      />

      <FilterBar ariaLabel="Audit filters">
        <FilterBar.Field label="Scope" htmlFor="audit-cluster">
          <FormSelect
            id="audit-cluster"
            value={clusterFilter}
            onChange={(_e, next) => setClusterFilter(next)}
            aria-label="Scope"
            data-testid="audit-cluster"
          >
            <FormSelectOption value="" label="Everything" />
            <FormSelectOption value={UNSCOPED} label="No cluster (console records)" />
            {clusters.map((c) => (
              <FormSelectOption key={c.id} value={String(c.id)} label={c.name} />
            ))}
          </FormSelect>
        </FilterBar.Field>

        <FilterBar.Field label="Kind" htmlFor="audit-category">
          <FormSelect
            id="audit-category"
            value={category}
            onChange={(_e, next) => setCategory(next)}
            aria-label="Record kind"
            data-testid="audit-category"
          >
            {CATEGORIES.map((c) => (
              <FormSelectOption key={c.value} value={c.value} label={c.label} />
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
      <Alert isInline variant="info" title={scopeSentence} data-testid="audit-scope" />

      <IntegrityPanel
        chain={chain}
        error={chainError}
        loading={chainLoading}
        onVerify={verify}
      />

      <Split hasGutter style={{ alignItems: 'center', marginBlockEnd: 'var(--admin-gap-sm, 0.5rem)' }}>
        <SplitItem isFilled />
        <SplitItem>
          {/*
            Plain links, not fetch-and-blob. The response is an attachment stream
            that can be much larger than this tab's memory, and reading it to
            hand it straight back to the disk would both waste that memory and
            throw away the streaming the backend does. `download` is not set:
            the server names the file in Content-Disposition, including the
            timestamp, and a client-side name would drift from it.
          */}
          <Tooltip content={exportReason}>
            <Button
              component={mayExport ? 'a' : 'button'}
              variant="secondary"
              isAriaDisabled={!mayExport}
              href={mayExport ? auditApi.exportUrl({ ...filters, format: 'ndjson' }) : undefined}
              data-testid="audit-export-ndjson"
            >
              Export NDJSON
            </Button>
          </Tooltip>
        </SplitItem>
        <SplitItem>
          <Tooltip
            content={
              mayExport
                ? 'Flattened for a spreadsheet. Cells a spreadsheet would run as a formula are '
                  + 'prefixed with an apostrophe, so this format is not byte-faithful — use '
                  + 'NDJSON to verify the hash chain.'
                : exportReason
            }
          >
            <Button
              component={mayExport ? 'a' : 'button'}
              variant="secondary"
              isAriaDisabled={!mayExport}
              href={mayExport ? auditApi.exportUrl({ ...filters, format: 'csv' }) : undefined}
              data-testid="audit-export-csv"
            >
              Export CSV
            </Button>
          </Tooltip>
        </SplitItem>
      </Split>

      <PartialBanner unavailable={unavailable} />

      <DataTable
        columns={columns}
        rows={rows}
        rowKey="id"
        loading={loading}
        error={error}
        onRetry={() => load()}
        ariaLabel="Audit trail"
        // Columns, but no Filter menu: this table is one server-side page of a
        // longer trail, so a menu counting the rows in front of it would put a
        // number on the wrong set. §10's own filters are above, and they ask
        // the server.
        manageableColumns
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
