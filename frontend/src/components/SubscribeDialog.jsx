/**
 * SubscribeDialog — §16's one write, wrapped in the §11.3 handshake.
 *
 * Three things about this dialog are load-bearing, and all three are about the
 * gap between "a Subscription exists" and "an operator is installed".
 *
 * **The write creates one object and nothing else.** OLM resolves the
 * Subscription afterwards, on its own schedule, and only if the namespace has a
 * usable OperatorGroup and the approval strategy lets it. `applied: true` is
 * evidence that the Subscription object exists and evidence of nothing more, so
 * `summarize` says exactly that and names the CSV OLM is *expected* to install
 * rather than reporting an install.
 *
 * **`target.ready` is a tri-state.** `null` is "we could not check" — an
 * unreadable OperatorGroup listing, a group that selects namespaces by label,
 * or a catalog that published no install modes. It renders grey, never as a
 * pass and never as a failure: telling an operator the namespace is fine when
 * the check did not happen is how a Subscription lands somewhere OLM will mark
 * `UnsupportedOperatorGroup` and never install from.
 *
 * **Every consequence is acknowledged by code, not by a single checkbox.** The
 * backend refuses the write with `422 invalid` unless every code the plan
 * returned is named, and the list is recomputed at write time — so consent
 * given for one namespace's consequences cannot be carried onto another's. The
 * acknowledgements here are cleared whenever the plan's set of codes changes,
 * which is what stops the operator meeting that refusal at confirm time.
 *
 * The plan itself is a pure read (`POST /api/portal/subscriptions/plan` writes
 * nothing and is ungated), so everything below is visible on a console where
 * `ADMIN_PORTAL_INSTALL_ENABLED` is off — which is exactly when somebody needs
 * to read what enabling it would allow.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  HelperText,
  HelperTextItem,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import {
  CodeBlock,
  DescriptionList,
  NullableCell,
  PartialBanner,
  SectionHeader,
  StatusBadge,
} from './ui';
import { portal } from '../api/client';
import { useAsync } from '../pages/_data';

const APPROVALS = ['Automatic', 'Manual'];

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/**
 * The namespace's readiness for this operator, as a badge.
 *
 * Deliberately the same three-way shape as §14's router readiness: `null` is a
 * state of our knowledge, not a state of the cluster, and it is grey.
 */
function readiness(ready) {
  if (ready === true) return { status: 'Ready', label: 'Ready for this operator' };
  if (ready === false) return { status: 'NotReady', label: 'Not ready' };
  return { status: 'Unknown', label: 'Could not check' };
}

/**
 * The `done` headline. It must not say the operator is installed.
 *
 * `applied: true` means one Subscription object was created. Everything after
 * that is OLM's, happens later, and can fail for reasons this response cannot
 * see — so the sentence names the CSV that is *expected* and points at the
 * Installed tab, which is the only place the real answer appears.
 */
function summarize(result) {
  if (result?.applied !== true) {
    return {
      variant: 'warning',
      title: 'The write completed without confirming the Subscription was created',
      body:
        'The API server answered, but the response did not report `applied: true`. Re-read the ' +
        'namespace before assuming anything was written.',
    };
  }
  return {
    variant: 'success',
    title: `The Subscription ${result.package} was created — nothing is installed yet`,
    body: (
      <>
        <p>
          OLM resolves this Subscription on its own schedule. Whether an operator ends up running
          depends on the namespace&apos;s OperatorGroup and, with Manual approval, on somebody
          approving the InstallPlan.
        </p>
        <p style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}>
          {result.expectedCSV ? (
            <>
              It is expected to install <code>{result.expectedCSV}</code>
              {result.expectedVersion ? ` (version ${result.expectedVersion})` : ''}.
            </>
          ) : (
            'The channel published no currentCSV, so which ClusterServiceVersion OLM will install ' +
            'is not known from here.'
          )}{' '}
          The Installed tab is where that question is actually answered.
        </p>
      </>
    ),
  };
}

export default function SubscribeDialog({ row, defaultNamespace = '', onClose, onApplied }) {
  const [form, setForm] = useState(() => ({
    namespace: defaultNamespace || '',
    // The default channel, or the first one the package publishes. Not left
    // blank for a package with no default: the select would show the first
    // channel while the request carried none, and the operator would be reading
    // a plan for a channel they did not ask for.
    channel: row.defaultChannel || row.channels?.[0] || '',
    installPlanApproval: 'Automatic',
  }));
  const [acknowledged, setAcknowledged] = useState([]);

  // The plan body. `catalog`/`catalogNamespace` come from the row rather than
  // the form: the same package name legitimately exists in several catalogs,
  // and subscribing to "prometheus from community-operators" must not be
  // resolved against a mirror's copy.
  const body = useMemo(
    () => ({
      package: row.name,
      namespace: form.namespace.trim(),
      channel: form.channel || null,
      catalog: row.catalog || null,
      catalogNamespace: row.catalogNamespace || null,
      installPlanApproval: form.installPlanApproval,
    }),
    [row, form],
  );

  const {
    data: plan,
    loading: planLoading,
    error: planError,
  } = useAsync(() => portal.plan(body), {
    key: `portal-plan:${JSON.stringify(body)}`,
    enabled: Boolean(body.namespace),
  });

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);

  // Per-list, not a boolean. Changing the namespace changes what the operator is
  // consenting to, and carrying the previous ticks forward would consent to a
  // consequence nobody read. The backend enforces this too; clearing here is
  // what stops the refusal arriving at confirm time instead of at the checkbox.
  const signature = consequences.map((c) => c.code).sort().join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setAcknowledged([]);
  }, [signature]);

  const unacknowledged = consequences.filter((c) => !acknowledged.includes(c.code));

  const request = useCallback(
    (dryRun) => portal.subscribe({ ...body, acknowledgeConsequences: acknowledged, dryRun }),
    [body, acknowledged],
  );

  /**
   * Re-checked against the *dry run's* list rather than the plan's.
   *
   * They are computed by the same code, but the dry run is the later read: an
   * operator who previewed, left the dialog open and confirmed after somebody
   * deleted the namespace's OperatorGroup would otherwise confirm against a
   * consent list that no longer describes what happens.
   */
  const confirmBlockedReason = useCallback(
    (result) => {
      const missing = (result?.consequences ?? []).filter((c) => !acknowledged.includes(c.code));
      if (!missing.length) return null;
      return (
        'The dry run reported consequences that have not been acknowledged: ' +
        `${missing.map((c) => c.label).join('; ')}. Go back and tick each one — the write is ` +
        'refused until every code is named.'
      );
    },
    [acknowledged],
  );

  let previewBlocked = null;
  if (!body.namespace) previewBlocked = 'Name the namespace the Subscription goes in.';
  else if (planError) previewBlocked = planError.message;
  else if (planLoading || !plan) previewBlocked = 'Still reading this package from the catalog…';
  else if (unacknowledged.length) {
    previewBlocked = `Acknowledge what this subscription means: ${unacknowledged
      .map((c) => c.label)
      .join('; ')}.`;
  }

  const selected = plan?.selected;
  const target = plan?.target;
  const targetState = readiness(target?.ready);
  const installModes = selected?.installModes;
  const owned = selected?.ownedCustomResources ?? [];
  const channels = row.channels?.length ? row.channels : plan?.channels?.map((c) => c.name) ?? [];

  return (
    <MutationDialog
      isOpen
      title={`Subscribe to ${row.displayName || row.name}`}
      description={
        'This creates one Subscription object. It does not install an operator — OLM does that ' +
        'afterwards, and only if this namespace lets it.'
      }
      request={request}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the Subscription"
      confirmLabel="Create the Subscription"
      autoPreview={false}
      confirmBlockedReason={confirmBlockedReason}
      summarize={summarize}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      className="admin-subscribe-dialog"
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="subscribe-form">
        <PartialBanner unavailable={plan?.unavailable} />

        {plan?.enabled === false && (
          // Not red. A deployment that has switched subscribing off has made a
          // decision, and the plan below stays readable precisely so that
          // decision can be made from evidence.
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="subscribe-disabled"
            title="Subscribing is switched off on this deployment"
          >
            {plan.enabledDetail} The dry run below still runs and shows exactly what would be
            created.
          </Alert>
        )}

        {planError && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="subscribe-plan-error"
            title="This package could not be planned"
          >
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        <Grid hasGutter>
          <GridItem span={4}>
            <FormGroup label="Namespace" fieldId="subscribe-namespace" isRequired>
              <TextInput
                id="subscribe-namespace"
                value={form.namespace}
                onChange={(_e, value) => setForm((f) => ({ ...f, namespace: value }))}
                data-testid="subscribe-namespace"
              />
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    Where the Subscription object goes. It has to hold an OperatorGroup whose
                    scope this operator supports, or OLM will refuse to install.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>
          </GridItem>
          <GridItem span={4}>
            <FormGroup label="Channel" fieldId="subscribe-channel">
              <FormSelect
                id="subscribe-channel"
                value={form.channel}
                onChange={(_e, value) => setForm((f) => ({ ...f, channel: value }))}
                data-testid="subscribe-channel"
              >
                {channels.map((name) => (
                  <FormSelectOption
                    key={name}
                    value={name}
                    label={name === row.defaultChannel ? `${name} (default)` : name}
                  />
                ))}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem span={4}>
            <FormGroup label="Install plan approval" fieldId="subscribe-approval">
              <FormSelect
                id="subscribe-approval"
                value={form.installPlanApproval}
                onChange={(_e, value) => setForm((f) => ({ ...f, installPlanApproval: value }))}
                data-testid="subscribe-approval"
              >
                {APPROVALS.map((value) => (
                  <FormSelectOption key={value} value={value} label={value} />
                ))}
              </FormSelect>
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    Manual holds every install and every later upgrade until somebody approves the
                    InstallPlan — which this console does not do.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>
          </GridItem>
        </Grid>

        {/* ── The target namespace ─────────────────────────────────────── */}

        <SectionHeader
          title="The namespace this goes in"
          description="Whether OLM can actually install from a Subscription written here."
        />
        <div data-testid="subscribe-target" data-ready={plan ? String(target?.ready) : 'unread'}>
          {plan ? (
            <>
              <StatusBadge
                status={targetState.status}
                label={targetState.label}
                tooltip={target?.detail || undefined}
              />{' '}
              <span style={MUTED}>{target?.detail}</span>
            </>
          ) : (
            // Not the grey "could not check" badge: nothing has been asked yet,
            // and a badge that says we looked and failed would be a claim about
            // a read that has not happened.
            <span style={MUTED}>
              {body.namespace
                ? 'Reading this namespace…'
                : 'Name a namespace and this is checked against its OperatorGroup.'}
            </span>
          )}
        </div>

        {target?.ready === false && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="subscribe-target-not-ready"
            title="OLM will not install into this namespace as it is"
          >
            {target.detail} The Subscription can still be created — it will sit there unresolved
            until the OperatorGroup is fixed.
          </Alert>
        )}

        {target?.ready == null && plan && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="subscribe-target-unknown"
            title="Whether this namespace is ready could not be checked"
          >
            {target?.detail} This is not a pass. Creating the Subscription here may produce an
            operator that never installs.
          </Alert>
        )}

        {(plan?.existing ?? []).length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="subscribe-existing"
            title="Something is already subscribed to this package here"
          >
            <ul>
              {plan.existing.map((sub) => (
                <li key={sub.id}>
                  {sub.namespace}/{sub.name} — channel {sub.channel ?? 'unset'}
                </li>
              ))}
            </ul>
          </Alert>
        )}

        {/* ── What this channel is ────────────────────────────────────── */}

        {selected && (
          <>
            <SectionHeader
              title={`Channel ${selected.name}`}
              description="What the catalog publishes about the version this channel currently points at."
            />
            <DescriptionList
              columns={2}
              items={[
                { label: 'Display name', value: selected.displayName },
                {
                  label: 'Version',
                  value: (
                    <NullableCell
                      value={selected.version}
                      reason="The catalog published no CSV description for this channel, which is ordinary for a pruned catalog."
                    />
                  ),
                },
                {
                  label: 'ClusterServiceVersion',
                  value: (
                    <NullableCell
                      value={selected.currentCSV}
                      reason="This channel names no currentCSV, so which object OLM would install is not known from here."
                    />
                  ),
                },
                { label: 'Provider', value: selected.provider ?? plan?.provider },
                { label: 'Capability level', value: selected.capabilityLevel },
                {
                  label: 'Certified',
                  value:
                    selected.certified == null ? (
                      <NullableCell
                        value={null}
                        reason="The catalog says nothing about certification for this channel. That is not the same as uncertified."
                      />
                    ) : (
                      <StatusBadge
                        status={selected.certified ? 'Ready' : 'unknown'}
                        label={selected.certified ? 'Certified' : 'Not certified'}
                      />
                    ),
                },
                { label: 'Minimum Kubernetes version', value: selected.minKubeVersion },
                { label: 'Container image', value: selected.containerImage },
                { label: 'Repository', value: selected.repository },
                { label: 'Summary', value: selected.summary },
              ]}
            />

            <SectionHeader title="Install modes this operator supports" />
            <div data-testid="subscribe-install-modes">
              {installModes == null ? (
                <NullableCell
                  value={null}
                  reason="The catalog published no install modes for this channel, so whether it can watch the scope this namespace's OperatorGroup asks for is unknown."
                />
              ) : (
                installModes.map((mode) => (
                  <span key={mode.type} style={{ marginInlineEnd: '0.5rem' }}>
                    <StatusBadge
                      status={mode.supported ? 'Ready' : 'unknown'}
                      label={`${mode.type}${mode.supported ? '' : ' (not supported)'}`}
                    />
                  </span>
                ))
              )}
              {target?.requiredInstallMode && (
                <div style={{ ...MUTED, marginBlockStart: '0.5rem' }}>
                  This namespace&apos;s OperatorGroup requires <code>{target.requiredInstallMode}</code>.
                </div>
              )}
            </div>

            <SectionHeader
              title="Custom resources it brings"
              description="The CRDs this operator owns. Installing it puts these API kinds on the cluster."
            />
            <div data-testid="subscribe-crds">
              {owned.length === 0 ? (
                <span style={MUTED}>
                  The catalog lists no owned custom resources for this channel.
                </span>
              ) : (
                <ul>
                  {owned.map((crd) => (
                    <li key={`${crd.kind}/${crd.name}/${crd.version}`}>
                      <strong>{crd.kind}</strong> <code>{crd.name}</code>{' '}
                      <span style={MUTED}>{crd.version}</span>
                      {crd.description ? <div style={MUTED}>{crd.description}</div> : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {selected.description && (
              <>
                <SectionHeader title="What the publisher says about it" />
                {/* The catalog publishes markdown. It is rendered as plain
                    preformatted text rather than parsed: a markdown renderer is
                    a dependency and an injection surface for a document that
                    arrives from a third-party registry, and the operator is
                    reading this to decide, not to enjoy it. */}
                <pre
                  data-testid="subscribe-description"
                  style={{
                    whiteSpace: 'pre-wrap',
                    maxHeight: 240,
                    overflow: 'auto',
                    fontSize: '0.8125rem',
                    margin: 0,
                  }}
                >
                  {selected.description}
                </pre>
              </>
            )}
          </>
        )}

        {/* ── Consequences ────────────────────────────────────────────── */}

        {consequences.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="subscribe-consequences"
            title={
              consequences.length === 1
                ? 'One thing about this subscription needs acknowledging'
                : `${consequences.length} things about this subscription need acknowledging`
            }
          >
            {consequences.map((item) => (
              <div key={item.code} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`subscribe-ack-${item.code}`}
                  data-testid={`subscribe-ack-${item.code}`}
                  isChecked={acknowledged.includes(item.code)}
                  onChange={(_e, checked) =>
                    setAcknowledged((current) =>
                      checked
                        ? [...current, item.code]
                        : current.filter((code) => code !== item.code),
                    )
                  }
                  label={<strong>{item.label}</strong>}
                  description={
                    <>
                      <div>{item.consequence}</div>
                      <div style={{ marginTop: '0.25rem' }}>
                        <em>{item.mitigation}</em>
                      </div>
                    </>
                  }
                />
              </div>
            ))}
          </Alert>
        )}

        {/* ── The object ──────────────────────────────────────────────── */}

        {plan?.document && (
          <>
            <SectionHeader
              title="The object that would be created"
              description="One Subscription, and nothing else. The diff on the next step is the API server's own projection of it."
            />
            <div data-testid="subscribe-document">
              <CodeBlock
                code={plan.document}
                language="yaml"
                maxHeight={280}
                ariaLabel="Subscription manifest"
              />
            </div>
          </>
        )}
      </Form>
    </MutationDialog>
  );
}
