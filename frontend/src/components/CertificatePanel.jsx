/**
 * CertificatePanel — §32, the certificate behind each exposure.
 *
 * The Routes table above says `edge` and names a Secret. This says when that
 * certificate stops working and whether it is even for the hostname the
 * exposure serves — the two facts no Kubernetes API reports, and the two that
 * decide whether the site is up tomorrow.
 *
 * Four things this panel is careful about, each because the obvious rendering
 * would be wrong:
 *
 * **What is not checked is said first, not in a footnote.** No trust chain is
 * built, no revocation is checked, and nothing here is a handshake with the
 * exposure. A certificate this table calls valid can still be rejected by every
 * browser. Putting that under the table would let a green column read as "this
 * works", which is a claim the console cannot make.
 *
 * **Unknown is grey and is never a flavour of valid.** A Secret that could not
 * be read leaves the row with no certificate and a reason. It never renders as
 * an exposure that has none, which is the reading that sends somebody to create
 * a Secret that already exists.
 *
 * **A host with no verdict is not a failing host.** `covered: null` means no
 * certificate was read to check against, so the chip is grey and says so —
 * `false` is a claim about a certificate and needs one to have been read.
 *
 * **The two "no certificate here" cases are not faults.** A passthrough
 * exposure keeps its certificate in the pod and one that names nothing is
 * served by the router's default. Both are ordinary, both are drawn neutral,
 * and both say where the certificate actually is.
 */
import { useMemo, useState } from 'react';
import { Alert, Button, Label, LabelGroup } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  DataTable,
  NullableCell,
  PartialBanner,
  SearchInput,
  SectionHeader,
  StatusBadge,
  Toolbar,
} from './ui';
import { routes as routesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync } from '../pages/_data';
import { Muted } from '../pages/_parts';

/** State → how the pill reads. `unknown` stays grey: it is not a verdict. */
const STATE_BADGE = {
  valid: { status: 'healthy', label: 'Valid' },
  expiring: { status: 'warning', label: 'Expires soon' },
  expired: { status: 'failed', label: 'Expired' },
  not_yet_valid: { status: 'failed', label: 'Not yet valid' },
  unknown: { status: 'unknown', label: 'Unknown' },
};

/** The two places a certificate legitimately is not, and where it is instead. */
const ELSEWHERE = {
  backend: {
    label: 'In the pod',
    tooltip:
      'This exposure passes TLS through to the backend, which holds the certificate. The router never sees one and neither does this console.',
  },
  router_default: {
    label: "Router's default",
    tooltip:
      'This exposure terminates TLS and names no certificate, so the router serves its own default. That lives in the router’s namespace under a name this console is not told.',
  },
};

/** `in 12 days` / `4 days ago`, or null when there is nothing to say. */
function relative(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const days = Math.trunc(Math.abs(seconds) / 86400);
  const unit = days === 1 ? 'day' : 'days';
  if (seconds < 0) return days === 0 ? 'today' : `${days} ${unit} ago`;
  return days === 0 ? 'today' : `in ${days} ${unit}`;
}

/** The hosts an exposure serves, each with what the certificate says about it. */
function HostChips({ row }) {
  if (!row.hostsCovered.length) {
    return (
      <Muted>
        {row.kind === 'Gateway'
          ? 'This listener names no hostname, so it serves every name that reaches it.'
          : 'This exposure names no hostname.'}
      </Muted>
    );
  }
  return (
    <LabelGroup numLabels={4} data-testid={`certificate-hosts-${row.id}`}>
      {row.hostsCovered.map(({ host, covered }) => (
        <Label
          key={host}
          isCompact
          color={covered === true ? 'green' : covered === false ? 'red' : 'grey'}
          data-testid={`certificate-host-${covered === null ? 'unknown' : covered}`}
        >
          {host}
        </Label>
      ))}
    </LabelGroup>
  );
}

export default function CertificatePanel() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const [search, setSearch] = useState('');

  const { data, loading, error, reload } = useAsync(
    () => routesApi.certificates({ namespace: namespace || undefined }),
    {
      key: `route-certificates:${activeClusterId}:${namespace ?? ''}`,
      enabled: activeClusterId != null,
    },
  );

  const rows = data?.items ?? [];
  const kinds = data?.kinds ?? [];
  const unknownKinds = kinds.filter((k) => k.state === 'unknown');

  const counts = useMemo(() => {
    const tally = { expired: 0, expiring: 0, unknown: 0, uncovered: 0 };
    for (const row of rows) {
      if (row.state === 'expired' || row.state === 'not_yet_valid') tally.expired += 1;
      if (row.state === 'expiring') tally.expiring += 1;
      // Only certificates this console was meant to read count as unknown: the
      // two "it lives elsewhere" cases are not gaps in what we could see.
      if (row.state === 'unknown' && (row.source === 'secret' || row.source === 'inline')) {
        tally.unknown += 1;
      }
      if (row.hostsCovered.some((h) => h.covered === false)) tally.uncovered += 1;
    }
    return tally;
  }, [rows]);

  const columns = useMemo(
    () => [
      {
        key: 'name',
        title: 'Exposure',
        sortable: true,
        cell: (row) => (
          <div>
            <div>{row.name}</div>
            <Muted>
              {row.kind} · {row.namespace}
              {row.kind === 'Gateway' ? ` · listener ${row.slot}` : ''}
            </Muted>
          </div>
        ),
      },
      {
        key: 'hosts',
        title: 'Hosts',
        modifier: 'breakWord',
        cell: (row) => <HostChips row={row} />,
      },
      {
        key: 'state',
        title: 'Certificate',
        sortable: true,
        cell: (row) => {
          const elsewhere = ELSEWHERE[row.source];
          if (elsewhere) {
            return (
              <StatusBadge
                status="unsupported"
                label={elsewhere.label}
                tooltip={elsewhere.tooltip}
              />
            );
          }
          const badge = STATE_BADGE[row.state] ?? STATE_BADGE.unknown;
          return (
            <StatusBadge
              status={badge.status}
              label={badge.label}
              tooltip={
                row.state === 'unknown'
                  ? 'This console could not read the certificate this exposure names. That is not a report that it has none.'
                  : undefined
              }
            />
          );
        },
      },
      {
        key: 'expires',
        title: 'Expires',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.certificate?.not_after ?? null}
            reason="The certificate behind this exposure could not be read, so its expiry is unknown — not distant."
            format={(value) => (
              <span data-testid={`certificate-expiry-${row.id}`}>
                {value.slice(0, 10)} <Muted>{relative(row.expires_in_seconds)}</Muted>
              </span>
            )}
          />
        ),
      },
      {
        key: 'issuer',
        title: 'Issued by',
        cell: (row) => (
          <NullableCell
            value={row.certificate?.issuer_common_name ?? null}
            reason="No certificate was read for this exposure."
          />
        ),
      },
      {
        key: 'findings',
        title: 'What to know',
        modifier: 'breakWord',
        cell: (row) =>
          row.findings.length ? (
            <ul style={{ margin: 0, paddingInlineStart: '1.1rem' }}>
              {row.findings.map((finding) => (
                <li key={finding.code} data-testid={`certificate-finding-${finding.code}`}>
                  {finding.detail}
                </li>
              ))}
            </ul>
          ) : (
            <Muted>
              Nothing this console checks is wrong with this certificate. It does not check
              everything — see above.
            </Muted>
          ),
      },
    ],
    [],
  );

  return (
    <div data-testid="certificate-panel">
      <SectionHeader
        title="Certificates"
        description="Every certificate the exposures above point at, with its expiry and the hostnames it actually names."
      />

      <Alert
        isInline
        variant="info"
        data-testid="certificate-caveat"
        title="What this reads, and what it cannot tell you"
      >
        These are the certificates the objects <strong>name</strong>, decoded from the
        cluster. No chain is verified, no revocation is checked and no connection is made
        to any of these hosts — so a certificate shown here as valid can still be refused
        by a client, and a router configured with a default certificate of its own may be
        serving something else entirely. Only a handshake settles that.
      </Alert>

      {unknownKinds.length > 0 && (
        <Alert
          isInline
          variant="warning"
          data-testid="certificate-kind-unknown"
          title="One of the exposure APIs could not be read"
        >
          {unknownKinds.map((kind) => kind.detail).join(' ')}
        </Alert>
      )}

      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Certificate controls">
        <Toolbar.Item>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Filter by exposure or hostname…"
          />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>
            <span data-testid="certificate-summary">
              {counts.expired} expired or not yet valid · {counts.expiring} expiring within{' '}
              {Math.round((data?.expiringWindowSeconds ?? 0) / 86400)} days ·{' '}
              {counts.uncovered} not naming a host they serve · {counts.unknown} unreadable
            </span>
          </Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button
            variant="plain"
            aria-label="Refresh certificates"
            icon={<SyncAltIcon />}
            onClick={reload}
          />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Certificates behind the exposures"
        columns={columns}
        rows={rows}
        rowKey="id"
        loading={loading}
        error={error}
        onRetry={reload}
        filterText={search}
        emptyTitle="No exposure here terminates TLS"
        emptyDescription="The listings succeeded and none of the Routes, Ingresses or Gateways in scope carries a TLS block."
        footer={
          (data?.truncated ?? []).length > 0 ? (
            <div className="admin-table__widths" data-testid="certificate-truncated">
              <Muted>
                {data.truncated
                  .map((t) => `Showing the first ${t.shown} ${t.kind} objects`)
                  .join('. ')}
                . Filter to one namespace to see the rest.
              </Muted>
            </div>
          ) : null
        }
      />
    </div>
  );
}
