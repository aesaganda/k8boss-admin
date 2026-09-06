/**
 * DeleteNamespaceDialog — §26, the blast radius of `kubectl delete namespace`.
 *
 * §4's delete dialog is honest and useless here. It shows the namespace
 * object's YAML disappearing, which is a preview of a metadata block: nothing
 * on that screen is the database, the address or the admission webhook that
 * goes with it. This dialog is the missing screen, and it renders four things
 * `kubectl` does not print:
 *
 * **What is in it**, by kind, from discovery rather than from a curated list —
 * so an operator's custom resources are counted too. A kind whose listing was
 * refused is an em dash, never `0`: "this namespace holds no
 * PersistentVolumeClaims" is the sentence that ends with a deleted database.
 *
 * **What happens to the data.** A claim's volume is destroyed or kept depending
 * on a `persistentVolumeReclaimPolicy` that lives on a cluster-scoped object
 * nobody is looking at. `Delete` and `Retain` are opposite outcomes behind one
 * button, and a volume this console could not read is drawn as **unknown** —
 * which is neither, and is deliberately not reassuring.
 *
 * **What stops answering.** A `LoadBalancer` Service takes its address with it;
 * a ValidatingWebhookConfiguration backed from inside the namespace survives
 * the delete with nothing behind it and, at the v1 default of
 * `failurePolicy: Fail`, then refuses every write it intercepts cluster-wide.
 *
 * **Why it might not finish.** `applied: true` here means a deletionTimestamp
 * exists, not that the namespace is gone. The summary says exactly that, and
 * the finalizer table above is the answer to why a namespace sits in
 * `Terminating` — the same plan, read against a namespace already stuck, is the
 * diagnosis rather than a second tool.
 *
 * The typed confirmation is not decoration and is not the handshake: the
 * checklist is what covers the consequences the plan found, and typing the name
 * is the friction on the irreversible act itself, which is there whether or not
 * the namespace holds anything worth a checkbox.
 */
import { useCallback, useMemo } from 'react';
import { Alert } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import ConsequenceChecklist from './ConsequenceChecklist';
import { blockedByConsequences, useAcknowledgements } from './consequences';
import { DataTable, NullableCell, SectionHeader, StatusBadge } from './ui';
import { projects as projectsApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** `group/resource`, or just the resource for the core group. */
function kindLabel(row) {
  return row.group ? `${row.resource}.${row.group}` : row.resource;
}

/** What the namespace holds. An unreadable kind is an em dash, never a zero. */
function InventoryTable({ inventory }) {
  const rows = (inventory?.kinds ?? []).filter(
    (row) => row.count === null || row.count > 0,
  );

  return (
    <DataTable
      ariaLabel="What this namespace holds"
      tableId="namespace-delete-inventory"
      rows={rows}
      rowKey={(row) => `${row.group}/${row.resource}`}
      emptyTitle="Nothing was found in this namespace"
      emptyDescription="Every kind the cluster serves was listed and each came back empty. That is a real answer — an unreadable kind would be shown here with an em dash instead."
      resizableColumns={false}
      columns={[
        { key: 'resource', title: 'Kind', cell: kindLabel },
        {
          key: 'count',
          title: 'Objects',
          cell: (row) => (
            <span data-testid={`namespace-delete-count-${row.resource}`}>
              <NullableCell
                value={row.count}
                reason={`Listing ${kindLabel(row)} in this namespace failed, so how many there are is unknown. It is not zero.`}
              />
              {row.truncated ? (
                <span style={MUTED}> (more than this console read)</span>
              ) : null}
            </span>
          ),
        },
      ]}
    />
  );
}

/**
 * The volumes, with the only column that matters: what happens to the data.
 *
 * `reclaim_policy: null` on a bound claim is unknown and is drawn as unknown.
 * The two real answers point in opposite directions, so a default would be a
 * claim about somebody's database made by a console that did not look.
 */
function VolumeTable({ volumes }) {
  return (
    <DataTable
      ariaLabel="Volumes bound in this namespace"
      tableId="namespace-delete-volumes"
      rows={volumes ?? []}
      rowKey={(row) => row.claim}
      emptyTitle="No PersistentVolumeClaims"
      emptyDescription="Nothing in this namespace binds stored data."
      resizableColumns={false}
      columns={[
        { key: 'claim', title: 'Claim' },
        {
          key: 'volume',
          title: 'Volume',
          cell: (row) =>
            row.volume ? <code>{row.volume}</code> : <span style={MUTED}>unbound</span>,
        },
        {
          key: 'capacity',
          title: 'Size',
          cell: (row) => <NullableCell value={row.capacity} reason="The claim reports no capacity yet." />,
        },
        {
          key: 'reclaim_policy',
          title: 'What happens to the data',
          cell: (row) => (
            <span data-testid={`namespace-delete-fate-${row.claim}`}>
              {row.reclaim_policy === 'Delete' ? (
                <StatusBadge
                  status="failed"
                  label="destroyed"
                  tooltip="The volume's reclaim policy is Delete, so the storage provider deletes the underlying disk. Nothing brings it back."
                />
              ) : row.reclaim_policy === 'Retain' ? (
                <StatusBadge
                  status="Unknown"
                  label="kept, Released"
                  tooltip="The data survives and the volume stays as Released. No new claim can bind to it until its claimRef is cleared by hand."
                />
              ) : row.volume ? (
                <NullableCell value={null} reason={row.reason} />
              ) : (
                <span style={MUTED}>{row.reason}</span>
              )}
            </span>
          ),
        },
      ]}
    />
  );
}

/** Webhook configurations backed from inside. `null` is not an empty table. */
function WebhookPanel({ webhooks }) {
  if (webhooks === null || webhooks === undefined) {
    return (
      <Alert
        isInline
        variant="warning"
        className="admin-confirm__alert"
        data-testid="namespace-delete-webhooks-unknown"
        title="Whether an admission webhook is served from here is unknown"
      >
        The webhook configurations could not be read. This is <strong>not</strong>{' '}
        a report that none point at this namespace — a configuration whose
        backing Service lives here survives the delete, and at the v1 default of{' '}
        <code>failurePolicy: Fail</code> it then refuses every write it
        intercepts, cluster-wide.
      </Alert>
    );
  }

  if (!webhooks.length) {
    return (
      <p style={MUTED} data-testid="namespace-delete-webhooks-none">
        Both webhook configuration listings answered, and none points at a
        Service in this namespace.
      </p>
    );
  }

  return (
    <DataTable
      ariaLabel="Admission webhooks served from this namespace"
      tableId="namespace-delete-webhooks"
      rows={webhooks}
      rowKey={(row) => `${row.configuration}/${row.webhook}`}
      resizableColumns={false}
      columns={[
        { key: 'configuration', title: 'Configuration' },
        { key: 'webhook', title: 'Webhook' },
        { key: 'service', title: 'Backing service' },
        {
          key: 'failure_policy',
          title: 'On failure',
          cell: (row) => (
            <StatusBadge
              status={row.failure_policy === 'Fail' ? 'failed' : 'Unknown'}
              label={row.failure_policy}
              tooltip={
                row.failure_policy === 'Fail'
                  ? 'With no backend, the API server refuses every write this webhook intercepts, across the whole cluster.'
                  : 'With no backend, writes this webhook intercepts are admitted unchecked — whatever it enforced stops being enforced.'
              }
            />
          ),
        },
      ]}
    />
  );
}

/** The objects that can leave the namespace in Terminating. */
function FinalizerTable({ finalizers }) {
  if (!finalizers?.length) return null;
  return (
    <>
      <SectionHeader
        title="Objects holding a finalizer"
        description="A finalizer is a promise that a controller will act before the object may go. If that controller is not running — because it was uninstalled, or because it lives in this same namespace — the namespace stays in Terminating and re-deleting it does nothing."
      />
      <DataTable
        ariaLabel="Objects holding a finalizer"
        tableId="namespace-delete-finalizers"
        rows={finalizers}
        rowKey={(row) => `${row.resource}/${row.name}`}
        resizableColumns={false}
        columns={[
          { key: 'name', title: 'Object' },
          { key: 'resource', title: 'Kind', cell: (row) => row.resource.replace(/^\//, '') },
          {
            key: 'finalizers',
            title: 'Finalizers',
            cell: (row) => <code>{row.finalizers.join(', ')}</code>,
          },
        ]}
      />
    </>
  );
}

export default function DeleteNamespaceDialog({ name, onClose, onApplied }) {
  const { data: plan, loading, error } = useAsync(() => projectsApi.deletePlan(name), {
    key: `namespace-delete-plan:${name}`,
  });

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);
  const [acknowledged, setAcknowledged, unacknowledged] = useAcknowledgements(consequences);

  const requestBody = useCallback(
    (dryRun) =>
      projectsApi.remove(name, { dryRun, acknowledgeConsequences: acknowledged }),
    [name, acknowledged],
  );

  let previewDisabledReason = null;
  if (error) previewDisabledReason = error.message;
  else if (loading && !plan) previewDisabledReason = 'Still reading what this namespace holds…';
  else if (plan?.blocked) previewDisabledReason = plan.blocked.message;
  else if (unacknowledged.length)
    previewDisabledReason = `Acknowledge what this deletion means: ${unacknowledged
      .map((entry) => entry.label)
      .join('; ')}.`;

  return (
    <MutationDialog
      isOpen
      title={`Delete namespace ${name}`}
      description="Everything in a namespace goes with it, and most of what goes is not in the namespace object's own diff. Read what is below before previewing."
      request={requestBody}
      resourceVersion={plan?.resourceVersion ?? null}
      canPreview={!previewDisabledReason}
      previewDisabledReason={previewDisabledReason}
      previewLabel="Preview the deletion"
      confirmLabel="Delete the namespace"
      isDanger
      autoPreview={false}
      requireTyped={name}
      confirmBlockedReason={blockedByConsequences(acknowledged)}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${name} is being deleted`,
              body: 'That is a deletionTimestamp, not a finished deletion. The namespace controller now removes what is inside it, and a finalizer whose controller is not running holds the namespace in Terminating for as long as it goes unsatisfied — re-deleting it does nothing. Re-open this plan to see which object is holding it.',
            }
          : {
              variant: 'warning',
              title: 'The write completed without confirming it was applied',
              body: 'Re-read the namespace before assuming the deletion started.',
            }
      }
    >
      <>
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="namespace-delete-disabled"
            title="This console runs read-only"
          >
            {plan.gate.detail}
          </Alert>
        )}

        {error && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="namespace-delete-plan-error"
            title="What this namespace holds could not be read"
          >
            {error.message}
            {error.hint ? ` ${error.hint}` : ''}
          </Alert>
        )}

        {plan?.blocked && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="namespace-delete-blocked"
            title="This namespace is already being deleted"
          >
            {plan.blocked.message} {plan.blocked.hint}
          </Alert>
        )}

        {plan?.partial && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="namespace-delete-partial"
            title="This inventory is not complete"
          >
            {plan.unavailable.length} read
            {plan.unavailable.length === 1 ? '' : 's'} did not answer. Everything
            below is what was found in what was read, not what is in the
            namespace — treat it as a floor.
          </Alert>
        )}

        <SectionHeader
          title="What this namespace holds"
          description="Every namespaced kind the cluster serves, listed live. Kinds that came back empty are not shown; a kind whose listing was refused is shown with an em dash, because that is not zero."
        />
        <InventoryTable inventory={plan?.inventory} />

        <SectionHeader
          title="What happens to the stored data"
          description="Decided by the PersistentVolume's reclaim policy, which is on a cluster-scoped object — not on the claim, and not on this namespace."
        />
        <VolumeTable volumes={plan?.volumes} />

        {plan?.load_balancers?.length ? (
          <>
            <SectionHeader
              title="Addresses that stop answering"
              description="Deleting a LoadBalancer Service releases its cloud load balancer. Recreating the Service later gets a different address, and whatever points at the old one keeps pointing at nothing."
            />
            <DataTable
              ariaLabel="Load balancers released by this deletion"
              tableId="namespace-delete-loadbalancers"
              rows={plan.load_balancers}
              rowKey={(row) => row.name}
              resizableColumns={false}
              columns={[
                { key: 'name', title: 'Service' },
                {
                  key: 'addresses',
                  title: 'Address',
                  cell: (row) =>
                    row.addresses.length ? (
                      <code>{row.addresses.join(', ')}</code>
                    ) : (
                      <span style={MUTED}>none assigned yet</span>
                    ),
                },
              ]}
            />
          </>
        ) : null}

        <SectionHeader
          title="Admission webhooks served from here"
          description="The configurations are cluster-scoped and survive this delete. Their backing Service does not."
        />
        <WebhookPanel webhooks={plan?.webhooks} />

        <FinalizerTable finalizers={plan?.finalizers} />

        <ConsequenceChecklist
          consequences={consequences}
          acknowledged={acknowledged}
          onChange={setAcknowledged}
          idPrefix="namespace-delete"
          title={
            consequences.length === 1
              ? 'One thing about this deletion needs acknowledging'
              : `${consequences.length} things about this deletion need acknowledging`
          }
        />
      </>
    </MutationDialog>
  );
}
