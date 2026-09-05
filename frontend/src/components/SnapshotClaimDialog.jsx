/**
 * SnapshotClaimDialog — §22's write, wrapped in the §11.3 handshake.
 *
 * Four lines of YAML, and §4's editor could write them. What it could not do is
 * correct the belief the word "snapshot" creates, and on this API the gap
 * between the name and the thing is the widest in Kubernetes.
 *
 * **A CSI snapshot is not a backup.** For nearly every driver it is a
 * point-in-time reference inside the same storage system as the volume — often
 * the same array, the same zone. It protects against the change the operator is
 * about to make; it does not survive the loss of the storage holding it, because
 * it was never anywhere else. A dialog that took the name and wrote the object
 * would be handing somebody a reason to skip a real backup.
 *
 * **And it is crash-consistent.** Nothing freezes a filesystem or flushes a
 * database. What is captured is what would be on disk after a power cut.
 *
 * **The third fact is about the future, and this is the only moment it is in
 * front of the right person.** `deletionPolicy` lives on the
 * VolumeSnapshotClass. Under `Delete` — the common default — removing the
 * namespaced VolumeSnapshot object destroys the snapshot in the storage system.
 * Whoever eventually does that will be looking at a list of namespaced objects,
 * not at the cluster-scoped class that decides what the click means.
 *
 * Both of the first two are acknowledged on every snapshot. That is friction on
 * purpose: being wrong about either is discovered during a restore.
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
import { DescriptionList, NullableCell, SectionHeader, StatusBadge } from './ui';
import { storage as storageApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** A default name that says what was captured and when, not `snapshot-1`. */
function suggestedName(claim) {
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '').toLowerCase();
  return `${claim}-${stamp}`.slice(0, 253);
}

/**
 * What deleting this snapshot will later do — three renderings for three
 * answers, because `null` here is neither reassuring nor alarming.
 */
function DeletionPolicy({ snapshotClass }) {
  const policy = snapshotClass?.deletion_policy;
  if (policy === 'Delete') {
    return (
      <StatusBadge
        status="Unknown"
        label="Delete"
        tooltip="Removing the VolumeSnapshot object will destroy the snapshot in the storage system."
      />
    );
  }
  if (policy === 'Retain') {
    return (
      <StatusBadge
        status="Ready"
        label="Retain"
        tooltip="Removing the VolumeSnapshot object leaves the underlying snapshot in place."
      />
    );
  }
  return (
    <NullableCell
      value={null}
      reason={
        snapshotClass?.detail ||
        'Which class applies could not be determined, so what deleting this snapshot would do is unknown.'
      }
    />
  );
}

export default function SnapshotClaimDialog({ namespace, claim, onClose, onApplied }) {
  const [name, setName] = useState(() => suggestedName(claim));
  const [snapshotClass, setSnapshotClass] = useState('');
  const [acknowledged, setAcknowledged] = useState([]);

  const requested = useMemo(
    () => ({ name: name.trim(), snapshotClass: snapshotClass.trim() || null }),
    [name, snapshotClass],
  );

  const {
    data: plan,
    loading: planLoading,
    error: planError,
  } = useAsync(() => storageApi.snapshotPlan(namespace, claim, requested), {
    key: `snapshot-plan:${namespace}:${claim}:${requested.name}:${requested.snapshotClass ?? ''}`,
    enabled: Boolean(requested.name),
  });

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);

  // Per-list, not a boolean: changing the class changes what is being consented
  // to — the deletion consequence appears and disappears with it — and consent
  // given for one set must not carry onto another.
  const signature = consequences.map((c) => c.code).sort().join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setAcknowledged([]);
  }, [signature]);

  const unacknowledged = consequences.filter((c) => !acknowledged.includes(c.code));

  const request = useCallback(
    (dryRun) =>
      storageApi.snapshot(namespace, claim, {
        ...requested,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [namespace, claim, requested, acknowledged],
  );

  /**
   * Re-checked against the dry run's own answer, which is the later read: the
   * cluster's default snapshot class can change between the plan and the
   * preview, and with it whether deleting this snapshot destroys the data.
   */
  const confirmBlockedReason = useCallback(
    (result) => {
      const missing = (result?.consequences ?? []).filter((c) => !acknowledged.includes(c.code));
      if (!missing.length) return null;
      return (
        'The dry run reported consequences that have not been acknowledged: ' +
        `${missing.map((c) => c.label).join('; ')}. Go back and tick each one.`
      );
    },
    [acknowledged],
  );

  let previewBlocked = null;
  if (planError) previewBlocked = planError.message;
  else if (!requested.name) previewBlocked = 'Name the snapshot.';
  else if (planLoading && !plan) previewBlocked = 'Still reading the claim…';
  else if (unacknowledged.length) {
    previewBlocked = `Acknowledge what a snapshot is: ${unacknowledged.map((c) => c.label).join('; ')}.`;
  }

  return (
    <MutationDialog
      isOpen
      title={`Snapshot ${namespace}/${claim}`}
      description="Creates a VolumeSnapshot of this claim. The object is created here; the storage system takes the snapshot afterwards."
      request={request}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the snapshot"
      confirmLabel="Take the snapshot"
      autoPreview={false}
      confirmBlockedReason={confirmBlockedReason}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${requested.name} has been created`,
              body:
                'The VolumeSnapshot object exists. The storage system takes the snapshot ' +
                'afterwards and reports it by setting readyToUse — which starts out unset and ' +
                'can end at false with an error. Check the VolumeSnapshots tab before relying ' +
                'on this: nothing here is evidence that there is yet anything to restore from.',
            }
          : {
              variant: 'warning',
              title: 'The request completed without confirming the object was created',
              body: 'Re-read the VolumeSnapshots tab before assuming a snapshot exists.',
            }
      }
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="snapshot-form">
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="snapshot-disabled"
            title="This console runs read-only"
          >
            {plan.gate.detail}
          </Alert>
        )}

        {planError && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="snapshot-plan-error"
            title="This snapshot could not be planned"
          >
            {planError.message}
            {planError.hint ? ` ${planError.hint}` : ''}
          </Alert>
        )}

        <SectionHeader title="What is being captured" />
        <DescriptionList
          items={[
            {
              label: 'Claim',
              value: `${namespace}/${claim}`,
            },
            {
              label: 'Provisioned capacity',
              // The backend hands this over as the API server's own quantity
              // string ("50Gi"), not a byte count — rendered verbatim so the
              // number on screen is the number the cluster reports.
              value: plan?.claim?.capacity ?? (
                <NullableCell
                  value={null}
                  reason="Nothing is bound to this claim yet, so there is no provisioned capacity to capture."
                />
              ),
            },
            {
              label: 'Claim status',
              value: plan?.claim?.phase ? (
                <StatusBadge status={plan.claim.phase} />
              ) : (
                <NullableCell value={null} reason="The claim's phase could not be read." />
              ),
            },
            {
              label: 'Snapshot class',
              value: plan?.snapshotClass?.name ?? (
                <NullableCell
                  value={null}
                  reason={plan?.snapshotClass?.detail || 'No class could be determined.'}
                />
              ),
            },
            { label: 'On deleting this snapshot', value: <DeletionPolicy snapshotClass={plan?.snapshotClass} /> },
          ]}
        />

        <FormGroup label="Snapshot name" fieldId="snapshot-name" isRequired>
          <TextInput
            id="snapshot-name"
            value={name}
            onChange={(_e, value) => setName(value)}
            data-testid="snapshot-name"
            aria-label="Snapshot name"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Not generated for you: this name is how anyone finds the snapshot again, and a
                generated one is a string nobody recognises during the incident it was taken for.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        <FormGroup label="Snapshot class (optional)" fieldId="snapshot-class">
          <TextInput
            id="snapshot-class"
            value={snapshotClass}
            onChange={(_e, value) => setSnapshotClass(value)}
            data-testid="snapshot-class"
            aria-label="Snapshot class"
            placeholder={plan?.snapshotClass?.name ? `default: ${plan.snapshotClass.name}` : 'cluster default'}
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Leave empty to use the cluster&apos;s default class. That is a real request to the
                controller, resolved when the object is written — not a name pinned here.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        {consequences.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="snapshot-consequences"
            title={
              consequences.length === 1
                ? 'One thing about this snapshot needs acknowledging'
                : `${consequences.length} things about this snapshot need acknowledging`
            }
          >
            {consequences.map((item) => (
              <div key={item.code} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`snapshot-ack-${item.code}`}
                  data-testid={`snapshot-ack-${item.code}`}
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

        <p style={MUTED}>
          Creating the object is all this does. The storage system takes the snapshot afterwards,
          and reports it by setting <code>readyToUse</code> — which starts out unset.
        </p>
      </Form>
    </MutationDialog>
  );
}
