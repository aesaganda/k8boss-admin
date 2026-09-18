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
  StatusBadge,
} from '../components/ui';
import { api } from '../api/client';
import HpaBoundsDialog from '../components/HpaBoundsDialog';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes } from '../utils/format';
import { useGates } from './_data';
import {
  ActionButton,
  ChipList,
  genericTab,
  Muted,
  NoClusterState,
  ResourceTabsPage,
  YamlPanel,
} from './_parts';

const CHECKS = [
  { id: 'reveal', verb: 'get', group: 'core', resource: 'secrets' },
  // §21's write is a patch on the autoscaler itself, so the button is gated on
  // the verb the write will actually use rather than on anything in it.
  { id: 'patch', verb: 'patch', group: 'autoscaling', resource: 'horizontalpodautoscalers' },
];

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
  const [boundsFor, setBoundsFor] = useState(null);
  // Bumped after a write so the HPA listing is read again — by changing a
  // number, not by reaching into a hook this page does not own. A table still
  // showing the bounds somebody just changed is the moment a console stops
  // being believed.
  const [hpaToken, setHpaToken] = useState(0);

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

      // §21. Not a `genericTab`: an HPA's raw manifest does not answer the one
      // question worth asking about it — whether it is scaling at all. An
      // autoscaler whose ScalingActive condition is false is inert, and it
      // looks completely ordinary in `kubectl get hpa` and in a generic row.
      {
        key: 'hpas',
        title: 'HPAs',
        group: 'autoscaling',
        version: 'v2',
        plural: 'horizontalpodautoscalers',
        namespaced: true,
        refreshToken: hpaToken,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'The listing succeeded and returned no autoscalers in this scope.',
        detailTitle: (row) => `HPA ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          { key: 'namespace', title: 'Namespace', sortable: true },
          {
            key: 'target',
            title: 'Target',
            value: (row) => (row.target ? `${row.target.kind}/${row.target.name}` : ''),
            cell: (row) =>
              row.target ? (
                `${row.target.kind}/${row.target.name}`
              ) : (
                <NullableCell value={null} reason="This autoscaler declares no scaleTargetRef." />
              ),
          },
          {
            key: 'scaling_active',
            title: 'Scaling',
            sortable: true,
            value: (row) => (row.scaling_active == null ? '' : row.scaling_active ? 'yes' : 'no'),
            cell: (row) =>
              row.scaling_active === false ? (
                <StatusBadge
                  status="NotReady"
                  label="Not scaling"
                  tooltip={
                    row.conditions?.ScalingActive?.message ||
                    'The controller cannot compute a desired replica count, so this autoscaler is doing nothing.'
                  }
                />
              ) : row.scaling_active === true ? (
                <StatusBadge status="Ready" label="Scaling" />
              ) : (
                <NullableCell
                  value={null}
                  reason="No ScalingActive condition has been written yet. That is a fresh autoscaler, not a broken one."
                />
              ),
          },
          {
            key: 'replicas',
            title: 'Replicas',
            value: (row) => row.current_replicas ?? -1,
            cell: (row) => (
              <NullableCell
                value={row.current_replicas}
                reason="The controller has published no replica count for this autoscaler yet."
              />
            ),
          },
          {
            key: 'bounds',
            title: 'Bounds',
            value: (row) => row.max_replicas ?? -1,
            cell: (row) => (
              <span>
                {row.min_replicas ?? '?'}–{row.max_replicas ?? '?'}
                {row.scaling_limited === true && (
                  <>
                    {' '}
                    <StatusBadge
                      status="Unknown"
                      label="at limit"
                      tooltip="The desired count is being clamped to one of these bounds. During an incident this is the answer to “why is this not scaling up”."
                    />
                  </>
                )}
              </span>
            ),
          },
          {
            key: 'metrics',
            title: 'Metrics',
            // Never a bare number: a metric with no reading is an em dash, not
            // 0%. A CPU target drawn at zero reads as an idle workload.
            cell: (row) =>
              (row.metrics ?? []).length === 0 ? (
                <Muted>none</Muted>
              ) : (
                <span>
                  {row.metrics.map((metric, i) => (
                    <span key={`${metric.kind}-${metric.name}-${i}`}>
                      {i > 0 && ', '}
                      {metric.name}{' '}
                      {metric.current == null ? (
                        <NullableCell
                          value={null}
                          reason="The controller has published no reading for this metric — a missing metrics API looks exactly like this. It is not a reading of zero."
                        />
                      ) : (
                        metric.current
                      )}
                      <Muted>/{metric.target ?? '?'}</Muted>
                    </span>
                  ))}
                </span>
              ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        actions: (row) => {
          // `gate` is a function of the check id, not a map — see `useGates`.
          // Read here rather than hoisted so the reason is the current one.
          const patch = gate('patch');
          return [
            {
              title: 'Set bounds…',
              onClick: () => setBoundsFor(row),
              isDisabled: !patch.allowed,
              tooltip: patch.reason,
            },
          ];
        },
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                {
                  label: 'Scaling',
                  value:
                    row.scaling_active === false ? (
                      <StatusBadge
                        status="NotReady"
                        label={row.conditions?.ScalingActive?.reason || 'Not scaling'}
                        tooltip={row.conditions?.ScalingActive?.message}
                      />
                    ) : row.scaling_active === true ? (
                      <StatusBadge status="Ready" label="Scaling" />
                    ) : (
                      <NullableCell value={null} reason="Not observed by the controller yet." />
                    ),
                },
                {
                  label: 'Able to scale',
                  value:
                    row.able_to_scale === false ? (
                      <StatusBadge
                        status="NotReady"
                        label={row.conditions?.AbleToScale?.reason || 'No'}
                        tooltip={row.conditions?.AbleToScale?.message}
                      />
                    ) : row.able_to_scale === true ? (
                      <StatusBadge status="Ready" label="Yes" />
                    ) : (
                      <NullableCell value={null} reason="Not observed by the controller yet." />
                    ),
                },
                { label: 'Bounds', value: `${row.min_replicas ?? '?'}–${row.max_replicas ?? '?'}` },
                {
                  label: 'Desired',
                  value: (
                    <NullableCell
                      value={row.desired_replicas}
                      reason="The controller has published no desired count for this autoscaler."
                    />
                  ),
                },
              ]}
            />
            <YamlPanel
              group="autoscaling"
              version="v2"
              plural="horizontalpodautoscalers"
              name={row.name}
              namespace={row.namespace}
              height={340}
            />
          </>
        ),
      },
      // VerticalPodAutoscaler is a CRD, not a built-in API — this tab shows
      // "Not present on this cluster" (§ unsupported) on most clusters, which
      // is expected and not an error.
      genericTab({ key: 'vpas', title: 'VPAs', group: 'autoscaling.k8s.io', version: 'v1', plural: 'verticalpodautoscalers', namespaced: true }),
      genericTab({
        key: 'poddisruptionbudgets',
        title: 'Pod Disruption Budgets',
        group: 'policy',
        version: 'v1',
        plural: 'poddisruptionbudgets',
        namespaced: true,
      }),
      genericTab({ key: 'resourcequotas', title: 'Resource Quotas', group: 'core', version: 'v1', plural: 'resourcequotas', namespaced: true }),
      genericTab({ key: 'limitranges', title: 'Limit Ranges', group: 'core', version: 'v1', plural: 'limitranges', namespaced: true }),
      genericTab({
        key: 'priorityclasses',
        title: 'Priority Classes',
        group: 'scheduling.k8s.io',
        version: 'v1',
        plural: 'priorityclasses',
        namespaced: false,
      }),
      genericTab({
        key: 'runtimeclasses',
        title: 'Runtime Classes',
        group: 'node.k8s.io',
        version: 'v1',
        plural: 'runtimeclasses',
        namespaced: false,
      }),
      genericTab({ key: 'leases', title: 'Leases', group: 'coordination.k8s.io', version: 'v1', plural: 'leases', namespaced: true }),
      genericTab({
        key: 'mutatingwebhookconfigurations',
        title: 'Mutating Webhook Configurations',
        group: 'admissionregistration.k8s.io',
        version: 'v1',
        plural: 'mutatingwebhookconfigurations',
        namespaced: false,
      }),
      genericTab({
        key: 'validatingwebhookconfigurations',
        title: 'Validating Webhook Configurations',
        group: 'admissionregistration.k8s.io',
        version: 'v1',
        plural: 'validatingwebhookconfigurations',
        namespaced: false,
      }),
    ],
    [gate, hpaToken],
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
    <>
      {boundsFor && (
        <HpaBoundsDialog
          namespace={boundsFor.namespace}
          name={boundsFor.name}
          onClose={() => setBoundsFor(null)}
          onApplied={() => {
            setBoundsFor(null);
            setHpaToken((n) => n + 1);
          }}
        />
      )}
      <ResourceTabsPage
        basePath="/config"
        subtitle="ConfigMaps and Secrets. Secret values are never in a listing; revealing one is a deliberate, audited act."
        tabs={tabs}
      />
    </>
  );
}
