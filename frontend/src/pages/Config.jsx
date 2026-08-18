/**
 * Config — ConfigMaps and Secrets.
 *
 * The Secrets half is the reason this page is written by hand instead of being
 * two more generic tabs. §8: *Values are never included in list responses. The
 * single-object read returns values only when `?reveal=true` **and** mutations
 * are enabled **and** a preflight on `get secrets` passes — and it writes an
 * audit record. A secret read is a privileged act and is treated as one.*
 *
 * So, in this order:
 *
 * 1. The table shows key **names** and sizes. There is no code path from a list
 *    response to a value, because the list response does not contain one.
 * 2. The drawer shows the same, plus a Reveal button that is disabled with the
 *    reason when the preflight says no (rule 11.4) — never hidden, so an
 *    operator can see the capability exists and what is missing.
 * 3. Reveal is one deliberate click per Secret, it never happens on open, and
 *    the drawer says plainly that the read is audited before it is made rather
 *    than after.
 * 4. A refusal from the backend (`SECRET_REVEAL_ENABLED` off, or an RBAC
 *    denial) is rendered inline and persistently, with the hint that names what
 *    would fix it. Not a toast: this is the explanation for the empty panel next
 *    to it.
 *
 * Without a reveal, `data` comes back with every key present and every value
 * `null` — the key names are not the secret, and dropping the keys entirely
 * would show a Secret that appears to be empty.
 */
import { useCallback, useMemo, useState } from 'react';
import { Alert, Button, Card, CardBody } from '@patternfly/react-core';
import EyeIcon from '@patternfly/react-icons/dist/esm/icons/eye-icon';
import {
  AgeCell,
  CodeBlock,
  DescriptionList,
  NullableCell,
  PageHeader,
} from '../components/ui';
import { api } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes } from '../utils/format';
import { useGates } from './_data';
import {
  ActionButton,
  ChipList,
  Muted,
  NoClusterState,
  ResourceTabsPage,
  YamlPanel,
} from './_parts';

const CHECKS = [{ id: 'reveal', verb: 'get', group: 'core', resource: 'secrets' }];

/**
 * Base64 → text, correctly for anything that is not ASCII.
 *
 * `atob` produces a *binary string*, one code unit per byte; rendering it
 * directly mangles every non-ASCII character in a certificate subject or a
 * password. Genuinely binary values (a TLS key's DER form, a keystore) are
 * reported as binary with their size rather than as replacement characters
 * pretending to be text.
 */
function decodeBase64(value) {
  if (value == null) return null;
  try {
    const binary = atob(String(value));
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    // Control characters other than tab (09), newline (0A) and carriage return
    // (0D) mean this is not text anyone wants printed into a browser: a DER
    // certificate decodes "successfully" and renders as noise plus a handful of
    // terminal escape sequences.
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(text)) {
      return { binary: true, bytes: bytes.length };
    }
    return { text };
  } catch {
    const size = Math.floor((String(value).length * 3) / 4);
    return { binary: true, bytes: size };
  }
}

/** The audited single-object Secret read (§8), with `reveal` the client omits. */
function readSecret(name, namespace, reveal) {
  // `resources.get()` in the API client takes no `reveal` argument, so this
  // builds the §4 path directly rather than adding a second, looser way to
  // reach Secrets. Everything else — cluster scoping, the actor header, the
  // error envelope — still comes from the shared client.
  return api.get(`/resources/core/v1/secrets/${encodeURIComponent(name)}`, { namespace, reveal });
}

function SecretDetail({ row, gate }) {
  const [revealed, setRevealed] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Reset when the drawer moves to another Secret: leaving the previous
  // Secret's values on screen under a new heading is the worst possible bug on
  // this particular page.
  const identity = `${row.namespace}/${row.name}`;
  const [shownFor, setShownFor] = useState(identity);
  if (shownFor !== identity) {
    setShownFor(identity);
    setRevealed(null);
    setError(null);
  }

  const reveal = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const object = await readSecret(row.name, row.namespace, true);
      setRevealed(object?.data ?? {});
    } catch (err) {
      // Kept on screen, with its hint. `mutations_disabled` here means
      // SECRET_REVEAL_ENABLED is off — a deployment-level decision the operator
      // cannot fix by changing RBAC, and the hint says which.
      setError(err);
      setRevealed(null);
    } finally {
      setLoading(false);
    }
  }, [row.name, row.namespace]);

  return (
    <>
      <DescriptionList
        items={[
          { label: 'Type', value: row.type ?? null },
          { label: 'Keys', value: <ChipList values={row.keys} max={8} emptyText="none" /> },
          { label: 'Size', value: formatBytes(row.data_bytes) },
          { label: 'Age', value: <AgeCell seconds={row.age_seconds} /> },
        ]}
      />

      <Alert
        isInline
        variant="info"
        title="Revealing a Secret is an audited action"
        style={{ margin: '0.75rem 0' }}
      >
        The console records who read this Secret, when, and whether the read succeeded — a denial is
        recorded too. Key names and sizes above needed no reveal; the values below do.
      </Alert>

      {error && (
        <Alert isInline variant={error.code === 'mutations_disabled' ? 'warning' : 'danger'} title={error.message}>
          {error.hint && <div className="admin-error-hint">{error.hint}</div>}
          {error.code && (
            <div className="admin-error-code">
              Error code: <code>{error.code}</code>
            </div>
          )}
        </Alert>
      )}

      <div style={{ margin: '0.75rem 0' }}>
        <ActionButton
          // Reading a Secret is a read, not a write, so it is not gated on the
          // console's write switch — SECRET_REVEAL_ENABLED is a separate flag
          // and only the backend knows it. What we can check up front is the
          // RBAC half; the deployment half surfaces as the refusal above.
          gate={gate('reveal', { requiresWrite: false })}
          icon={<EyeIcon />}
          isLoading={loading}
          onClick={reveal}
        >
          {revealed ? 'Reveal again' : 'Reveal values'}
        </ActionButton>
        {revealed && (
          <Button variant="link" onClick={() => setRevealed(null)}>
            Hide
          </Button>
        )}
      </div>

      {revealed &&
        Object.entries(revealed).map(([key, value]) => {
          const decoded = decodeBase64(value);
          return (
            <Card key={key} isCompact style={{ marginBottom: '0.5rem' }}>
              <CardBody>
                <strong>{key}</strong>
                {decoded == null ? (
                  <NullableCell value={null} reason="The API server returned this key with no value." />
                ) : decoded.binary ? (
                  <Muted>{` binary value, ${formatBytes(decoded.bytes)} — not rendered`}</Muted>
                ) : (
                  <CodeBlock code={decoded.text} ariaLabel={`${key} value`} maxHeight={200} />
                )}
              </CardBody>
            </Card>
          );
        })}

      <YamlPanel group="core" version="v1" plural="secrets" name={row.name} namespace={row.namespace} height={280} />
    </>
  );
}

export default function Config() {
  const { activeClusterId } = useCluster();
  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const tabs = useMemo(
    () => [
      {
        key: 'configmaps',
        title: 'ConfigMaps',
        group: 'core',
        version: 'v1',
        plural: 'configmaps',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'The listing succeeded and returned no ConfigMaps in this scope.',
        detailTitle: (row) => `ConfigMap ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          { key: 'namespace', title: 'Namespace', sortable: true },
          {
            key: 'keys',
            title: 'Keys',
            sortable: true,
            value: (row) => (row.keys ?? []).length,
            cell: (row) => <ChipList values={row.keys} max={3} emptyText="none" />,
          },
          {
            key: 'data_bytes',
            title: 'Size',
            sortable: true,
            cell: (row) => <NullableCell value={row.data_bytes} format={formatBytes} />,
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                { label: 'Keys', value: <ChipList values={row.keys} max={10} emptyText="none" /> },
                { label: 'Size', value: formatBytes(row.data_bytes) },
                { label: 'Age', value: <AgeCell seconds={row.age_seconds} /> },
              ]}
            />
            {/* Values come back from the single-object read, which is what the
                YAML endpoint is. A ConfigMap is not secret, but a list of two
                hundred of them carrying dashboards and certificates is megabytes
                nothing renders — hence values here and not in the table. */}
            <YamlPanel
              group="core"
              version="v1"
              plural="configmaps"
              name={row.name}
              namespace={row.namespace}
              height={420}
            />
          </>
        ),
      },

      {
        key: 'secrets',
        title: 'Secrets',
        group: 'core',
        version: 'v1',
        plural: 'secrets',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription:
          'The listing succeeded and returned no Secrets in this scope. If that is surprising, check the banner above — a forbidden listing is not an empty namespace.',
        detailTitle: (row) => `Secret ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          { key: 'namespace', title: 'Namespace', sortable: true },
          { key: 'type', title: 'Type', sortable: true },
          {
            key: 'keys',
            title: 'Keys',
            sortable: true,
            value: (row) => (row.keys ?? []).length,
            // Key names only, and never a value: the list response does not
            // contain one, so there is nothing here that could leak.
            cell: (row) => <ChipList values={row.keys} max={3} emptyText="none" />,
          },
          {
            key: 'data_bytes',
            title: 'Size',
            sortable: true,
            cell: (row) => <NullableCell value={row.data_bytes} format={formatBytes} />,
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => <SecretDetail row={row} gate={gate} />,
      },
    ],
    [gate],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Configuration" />
        <NoClusterState what="Configuration" />
      </>
    );
  }

  return (
    <ResourceTabsPage
      title="Configuration"
      subtitle="ConfigMaps and Secrets. Secret values are never in a listing; revealing one is a deliberate, audited act."
      tabs={tabs}
    />
  );
}
