/**
 * ExpandClaimDialog — §20's write, wrapped in the §11.3 handshake.
 *
 * Growing a claim is one field on one object, and §4's YAML editor could
 * already write it. What it could not do is any of the four things that decide
 * whether the write is a good idea, and those four are this dialog.
 *
 * **The two numbers are shown side by side, always.** `spec.resources.requests`
 * is what the claim asks for; `status.capacity` is what a workload actually
 * has. They are the same on a settled claim and different on one mid-expansion,
 * and a dialog showing only one of them cannot say which situation you are in —
 * on the screen where that is the decision.
 *
 * **A green result is not more disk, and that is a checkbox rather than a
 * footnote.** `applied: true` means the request changed. The volume grows when
 * the provider grows it and the filesystem after that, often not until every
 * pod using it restarts. An operator who reads a successful write as "the disk
 * is bigger" is wrong about the volume their database is filling, which is the
 * confident-wrong-answer this product treats as a defect.
 *
 * **A refusal still shows the claim.** The plan answers `200` with `blocked`
 * for a size this claim cannot be given, so a too-small number renders as an
 * inline reason *next to the current size and the mounts* rather than replacing
 * them with an error. That is why the size field is usable at all.
 *
 * **`expansion.supported` is rendered as three states.** `false` says the write
 * will be refused and by which class. `null` — an unreadable class, or a claim
 * with none — says so and does **not** block: reading it as `false` would
 * refuse a write the cluster would have accepted and send somebody to argue
 * with a StorageClass that is already correct.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { DescriptionList, NullableCell, PartialBanner, SectionHeader, StatusBadge } from './ui';
import { storage as storageApi } from '../api/client';
import { formatBytes } from '../utils/format';
import { useAsync } from '../pages/_data';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** `50Gi (53.7 GB)` — the quantity as written, with the size a human reads. */
function quantity(text, bytes) {
  if (!text) return null;
  const human = formatBytes(bytes);
  return human ? `${text} · ${human}` : text;
}

/**
 * Whether this claim's StorageClass permits expansion, in three states.
 *
 * Never collapsed to two. The middle one is the whole reason this panel exists
 * as a component rather than a boolean beside the field.
 */
function ExpansionNotice({ expansion }) {
  if (!expansion) return null;
  if (expansion.supported === true) {
    return (
      <p data-testid="pvc-expansion-ok" style={MUTED}>
        <StatusBadge status="Ready" label="expandable" /> {expansion.detail}
      </p>
    );
  }
  return (
    <Alert
      isInline
      variant={expansion.supported === false ? 'danger' : 'warning'}
      className="admin-confirm__alert"
      data-testid="pvc-expansion"
      data-supported={String(expansion.supported)}
      title={
        expansion.supported === false
          ? 'This claim cannot be expanded'
          : 'Whether this claim can be expanded is unknown'
      }
    >
      {expansion.detail}
    </Alert>
  );
}

/** The pods with the volume mounted — a list, an explicit none, or an em dash. */
function MountNotice({ mounts }) {
  if (mounts == null) {
    return (
      <span data-testid="pvc-mounts-unknown">
        <NullableCell
          value={null}
          reason="The pod listing for this namespace did not answer, so this console cannot say whether anything has the volume open. It is not saying nothing does."
        />
      </span>
    );
  }
  if (mounts.length === 0) {
    return (
      <span data-testid="pvc-mounts-none" style={MUTED}>
        Nothing in this namespace has it mounted.
      </span>
    );
  }
  return <span data-testid="pvc-mounts">{mounts.join(', ')}</span>;
}

export default function ExpandClaimDialog({ claim, onClose, onApplied }) {
  const { namespace, name } = claim;
  // Seeded from the claim's own request, so the first thing on screen is the
  // number being changed rather than a blank field or a size this console
  // invented. It blocks — "already requests 50Gi" — and that refusal is the
  // instruction: ask for more than this.
  const [size, setSize] = useState(() => claim.requested ?? '');
  const [acknowledged, setAcknowledged] = useState([]);

  const {
    data: plan,
    loading: planLoading,
    error: planError,
  } = useAsync(() => storageApi.expandPlan(namespace, name, { size }), {
    key: `pvc-expand-plan:${namespace}:${name}:${size}`,
    enabled: Boolean(size.trim()),
  });

  // The version the *plan* read, not one the page passed down: it is the claim
  // as it was a moment ago rather than as the table loaded it, and it is what
  // rides inside the merge patch to make rule 4 the API server's decision too.
  const resourceVersion = plan?.resourceVersion ?? null;
  const current = plan?.current ?? null;
  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);

  // Per-list, not a boolean: changing the size changes what is being consented
  // to — a different number, and possibly a different set of codes — and
  // consent given for one must not carry onto another.
  const signature = consequences
    .map((c) => c.code)
    .sort()
    .join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current === `${size}|${signature}`) return;
    lastSignature.current = `${size}|${signature}`;
    setAcknowledged([]);
  }, [size, signature]);

  const unacknowledged = consequences.filter((c) => !acknowledged.includes(c.code));

  const request = useCallback(
    (dryRun) =>
      storageApi.expand(namespace, name, {
        size,
        resourceVersion,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [namespace, name, size, resourceVersion, acknowledged],
  );

  /**
   * Re-checked against the dry run's own answer, which is the later read: the
   * server recomputes the consequences against the claim as it is now, so a pod
   * that started mounting the volume between the plan and the preview shows up
   * here rather than being written past.
   */
  const confirmBlockedReason = useCallback(
    (result) => {
      const missing = (result?.consequences ?? []).filter((c) => !acknowledged.includes(c.code));
      if (missing.length) {
        return (
          'The dry run reported consequences that have not been acknowledged: ' +
          `${missing.map((c) => c.label).join('; ')}. Go back and tick each one.`
        );
      }
      return null;
    },
    [acknowledged],
  );

  /**
   * Rendered on the preview *and* after the write, because after the write the
   * same two numbers mean something sharper: the request is the new one, the
   * capacity is still the old one, and that gap is the thing an operator has to
   * go and watch rather than assume away.
   */
  const renderExtra = useCallback(({ result, phase }) => {
    if (!result?.current) return null;
    const executed = phase === 'done';
    return (
      <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }} data-testid="pvc-outcome">
        <SectionHeader
          title={executed ? 'What changed, and what has not' : 'What this write changes'}
          description={
            executed
              ? 'The claim now requests the new size. The capacity below is what the volume provided before the write, and this write did not change it — the provider grows the volume, and the filesystem grows after that.'
              : 'The request is what this write edits. The capacity is what the workload has, and no write from this console changes it directly.'
          }
        />
        <DescriptionList
          items={[
            {
              label: 'Requested after this write',
              value: (
                <span data-testid="pvc-outcome-requested">
                  {quantity(result.requested?.size, result.requested?.size_bytes)}
                </span>
              ),
            },
            {
              label: 'Capacity the volume provides',
              value: (
                <span data-testid="pvc-outcome-capacity">
                  <NullableCell
                    value={result.current.capacity_bytes}
                    format={formatBytes}
                    reason="Nothing is bound to this claim, so no capacity has been provisioned. This is not the size it requested."
                  />
                </span>
              ),
            },
          ]}
        />
      </div>
    );
  }, []);

  let previewBlocked = null;
  if (!size.trim()) previewBlocked = 'Enter the size to ask for.';
  else if (planError) previewBlocked = planError.message;
  else if (planLoading || !plan) previewBlocked = 'Still planning…';
  else if (plan.blocked) previewBlocked = plan.blocked.message;
  else if (unacknowledged.length) {
    previewBlocked = `Acknowledge what this change means: ${unacknowledged.map((c) => c.label).join('; ')}.`;
  }

  return (
    <MutationDialog
      isOpen
      title={`Expand ${namespace}/${name}`}
      description="One field on one claim, through the funnel. The preview asks the API server what would change; the volume and the filesystem grow afterwards, on the storage provider's schedule rather than this console's."
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the change"
      confirmLabel="Expand the claim"
      autoPreview={false}
      confirmBlockedReason={confirmBlockedReason}
      renderExtra={renderExtra}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="pvc-form">
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="pvc-disabled"
            title="This console runs read-only"
          >
            {plan.gate.detail}
          </Alert>
        )}

        <PartialBanner unavailable={plan?.unavailable} />

        {planError && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="pvc-plan-error"
            title="This change could not be planned"
          >
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        <SectionHeader
          title="The claim as it is now"
          description="Two numbers, not one. The request is what the claim asks for and is the only thing this write edits; the capacity is what the volume actually provides. On a claim mid-expansion they differ, and that difference is what tells you an earlier resize has not finished."
        />

        <DescriptionList
          items={[
            {
              label: 'Requested',
              value: (
                <span data-testid="pvc-current-requested">
                  {current ? (
                    quantity(current.requested, current.requested_bytes) ?? (
                      <NullableCell
                        value={null}
                        reason="This claim's requested size could not be read, so an expansion cannot be told from a shrink and none will be sent."
                      />
                    )
                  ) : (
                    <span style={MUTED}>reading…</span>
                  )}
                </span>
              ),
            },
            {
              label: 'Capacity provided',
              value: (
                <span data-testid="pvc-current-capacity">
                  <NullableCell
                    value={current?.capacity_bytes ?? null}
                    format={formatBytes}
                    reason="Nothing is bound to this claim yet, so no capacity has been provisioned. This is not the size it requested."
                  />
                </span>
              ),
            },
            {
              label: 'Phase',
              value: current ? <StatusBadge status={current.phase} /> : null,
            },
            { label: 'Storage class', value: current?.storage_class ?? null },
            { label: 'Mounted by', value: <MountNotice mounts={plan?.mountedBy} /> },
          ]}
        />

        <ExpansionNotice expansion={plan?.expansion} />

        <FormGroup label="Grow to" fieldId="pvc-size" isRequired>
          <TextInput
            id="pvc-size"
            data-testid="pvc-size"
            value={size}
            onChange={(_e, value) => setSize(value)}
            aria-label="The size to grow this claim to"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                A Kubernetes quantity — <code>20Gi</code>, <code>500M</code>, <code>1Ti</code>. Sent
                verbatim, so the number in the diff is the number you typed.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        {plan?.blocked && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="pvc-blocked"
            title={plan.blocked.message}
          >
            {plan.blocked.hint}
          </Alert>
        )}

        {consequences.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="pvc-consequences"
            title={
              consequences.length === 1
                ? 'One thing about this change needs acknowledging'
                : `${consequences.length} things about this change need acknowledging`
            }
          >
            {consequences.map((item) => (
              <div key={item.code} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`pvc-ack-${item.code}`}
                  data-testid={`pvc-ack-${item.code}`}
                  isChecked={acknowledged.includes(item.code)}
                  onChange={(_e, checked) =>
                    setAcknowledged((codes) =>
                      checked ? [...codes, item.code] : codes.filter((code) => code !== item.code),
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
      </Form>
    </MutationDialog>
  );
}
