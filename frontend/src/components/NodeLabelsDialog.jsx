/**
 * NodeLabelsDialog — §24's label write, wrapped in the §11.3 handshake.
 *
 * The sentence this dialog exists to say is the one that sounds like nothing is
 * happening: **removing a label evicts no pod, and that is the trap.** Node
 * affinity is `requiredDuringSchedulingIgnoredDuringExecution` — the rule is
 * evaluated when a pod is placed and never again — so a pod scheduled here by a
 * `nodeSelector` naming the key you are deleting keeps running exactly as it is.
 * The change is discovered at the next rollout, by somebody who was not in this
 * dialog.
 *
 * So the plan names the pods on this node whose placement rules mention a key
 * that is leaving. They are not a list of casualties; they are the list of
 * things that will not come back here.
 *
 * The other half is the labels the cluster's own components own — the
 * `kubernetes.io/` family. The kubelet re-applies some of them when it next
 * registers and never re-applies others, and which is which depends on flags and
 * a cloud provider that no API here reports. The console says that rather than
 * guessing, and it names `topology.kubernetes.io/zone` specifically, because a
 * node that has lost it stops being chosen for zonal volumes.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Form, Grid, GridItem, TextInput } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import ConsequenceChecklist from './ConsequenceChecklist';
import { blockedByConsequences, useAcknowledgements } from './consequences';
import { DataTable, PartialBanner, SectionHeader } from './ui';
import { nodes as nodesApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** Rows to the map the API takes. A blank key is a row still being typed. */
function wireLabels(rows) {
  const out = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (key) out[key] = row.value;
  }
  return out;
}

function signatureOf(labels) {
  return Object.entries(labels)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${value}`)
    .join('|');
}

/**
 * Pods placed here by a rule naming a key that is leaving.
 *
 * `null` renders as the failure it is, not as an empty table: "nothing depends
 * on this label" and "nobody looked" are opposite answers to the question an
 * operator is about to act on.
 */
function DependentsPanel({ dependents, podsChecked }) {
  const columns = useMemo(
    () => [
      {
        key: 'pod',
        title: 'Pod',
        sortable: true,
        value: (row) => `${row.namespace}/${row.pod}`,
        cell: (row) => (
          <span>
            <span style={MUTED}>{row.namespace}/</span>
            {row.pod}
          </span>
        ),
      },
      {
        key: 'controller',
        title: 'Managed by',
        sortable: true,
        value: (row) => (row.controller ? `${row.controller.kind}/${row.controller.name}` : ''),
        cell: (row) =>
          row.controller ? (
            `${row.controller.kind}/${row.controller.name}`
          ) : (
            <span style={MUTED}>unmanaged</span>
          ),
      },
      {
        key: 'keys',
        title: 'Requires',
        cell: (row) => <code>{row.keys.join(', ')}</code>,
      },
    ],
    [],
  );

  if (!podsChecked) {
    return (
      <Alert
        isInline
        variant="warning"
        className="admin-confirm__alert"
        data-testid="label-pods-unknown"
        title="The pods on this node could not be listed"
      >
        This console cannot tell you whether anything running here was placed by a
        rule naming the labels you are changing. That is not a report that nothing
        was.
      </Alert>
    );
  }

  return (
    <DataTable
      columns={columns}
      rows={dependents ?? []}
      rowKey={(row) => `${row.namespace}/${row.pod}`}
      ariaLabel="Pods whose placement rules name these labels"
      emptyTitle="No pod here names the labels you are changing"
      emptyDescription="Nothing running on this node declares a nodeSelector or a required node affinity that mentions a key being removed or re-valued. The listing succeeded."
    />
  );
}

function LabelRow({ row, index, onChange, onRemove }) {
  return (
    <Grid hasGutter data-testid={`label-row-${index}`}>
      <GridItem span={5}>
        <TextInput
          aria-label={`Label ${index + 1} key`}
          data-testid={`label-key-${index}`}
          value={row.key}
          onChange={(_e, value) => onChange({ ...row, key: value })}
          placeholder="key"
        />
      </GridItem>
      <GridItem span={5}>
        <TextInput
          aria-label={`Label ${index + 1} value`}
          data-testid={`label-value-${index}`}
          value={row.value}
          onChange={(_e, value) => onChange({ ...row, value })}
          placeholder="value"
        />
      </GridItem>
      <GridItem span={2}>
        <Button
          variant="link"
          isDanger
          data-testid={`label-remove-${index}`}
          onClick={onRemove}
        >
          Remove
        </Button>
      </GridItem>
    </Grid>
  );
}

export default function NodeLabelsDialog({ name, onClose, onApplied }) {
  const [rows, setRows] = useState([]);
  const [seeded, setSeeded] = useState(false);

  const labels = useMemo(() => wireLabels(rows), [rows]);
  const signature = signatureOf(labels);

  const { data: plan, loading, error } = useAsync(
    () => nodesApi.labelPlan(name, { labels }),
    { key: `node-labels:${name}:${signature}` },
  );

  const current = plan?.current;
  useEffect(() => {
    if (seeded || !current) return;
    setSeeded(true);
    setRows(
      Object.entries(current)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, value]) => ({ key, value })),
    );
  }, [seeded, current]);

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);
  const [acknowledged, setAcknowledged, unacknowledged] = useAcknowledgements(
    consequences,
    signature,
  );

  const resourceVersion = plan?.resourceVersion ?? null;

  const request = useCallback(
    (dryRun) =>
      nodesApi.setLabels(name, {
        labels,
        resourceVersion,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [name, labels, resourceVersion, acknowledged],
  );

  const duplicate = useMemo(() => {
    const seen = new Set();
    return rows.some((row) => {
      const key = row.key.trim();
      if (!key) return false;
      if (seen.has(key)) return true;
      seen.add(key);
      return false;
    });
  }, [rows]);

  let previewDisabledReason = null;
  if (error) previewDisabledReason = error.message;
  else if (loading && !plan) previewDisabledReason = 'Still reading the node…';
  else if (rows.some((row) => !row.key.trim()))
    previewDisabledReason = 'Every label needs a key. Remove the blank row or fill it in.';
  else if (duplicate)
    previewDisabledReason =
      'Two rows carry the same key. Only one of them would be written, and which is not something to leave to the form.';
  else if (plan?.blocked) previewDisabledReason = plan.blocked.message;
  else if (unacknowledged.length)
    previewDisabledReason = `Acknowledge what this change means: ${unacknowledged
      .map((entry) => entry.label)
      .join('; ')}.`;

  return (
    <MutationDialog
      isOpen
      title={`Labels on ${name}`}
      description="Labels decide where the scheduler is willing to place work. Changing them moves nothing that is already running."
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!previewDisabledReason}
      previewDisabledReason={previewDisabledReason}
      previewLabel="Preview the change"
      confirmLabel="Set the labels"
      autoPreview={false}
      confirmBlockedReason={blockedByConsequences(acknowledged)}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `The labels on ${name} are now what you sent`,
              body:
                'No pod moved. Anything running here that was placed by a rule naming a ' +
                'label you removed keeps running until something else restarts it, and will ' +
                'not be scheduled back to this node afterwards.',
            }
          : {
              variant: 'warning',
              title: 'The write completed without confirming it was applied',
              body: 'Re-read the node before assuming its labels changed.',
            }
      }
    >
      <Form onSubmit={(e) => e.preventDefault()} data-testid="label-form">
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="label-disabled"
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
            data-testid="label-plan-error"
            title="This node could not be read"
          >
            {error.message}
            {error.hint ? ` ${error.hint}` : ''}
          </Alert>
        )}

        <SectionHeader
          title="Labels"
          description="The whole map, not a change to it: a row removed here is a key removed from the node. Values are strings — a number needs no quotes in this form, but it is stored as text."
        />

        {rows.map((row, index) => (
          <LabelRow
            // Index-keyed on purpose: a row has no identity until it has a key,
            // and a half-typed one must not remount on every character.
            key={index}
            row={row}
            index={index}
            onChange={(next) =>
              setRows((all) => all.map((item, i) => (i === index ? next : item)))
            }
            onRemove={() => setRows((all) => all.filter((_item, i) => i !== index))}
          />
        ))}

        {rows.length === 0 && (
          <p style={MUTED} data-testid="label-none">
            This node carries no labels.
          </p>
        )}

        <div>
          <Button
            variant="secondary"
            data-testid="label-add"
            onClick={() => setRows((all) => [...all, { key: '', value: '' }])}
          >
            Add a label
          </Button>
        </div>

        <SectionHeader
          title="What was placed here by these labels"
          description="Nothing in this list is evicted by the change. It is what will not be scheduled back here after it next restarts."
        />

        <PartialBanner unavailable={plan?.unavailable} />
        <DependentsPanel
          dependents={plan?.dependents}
          podsChecked={plan?.pods_checked !== false}
        />

        {plan?.blocked && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="label-blocked"
            title="Nothing would change"
          >
            {plan.blocked.message} {plan.blocked.hint}
          </Alert>
        )}

        <ConsequenceChecklist
          consequences={consequences}
          acknowledged={acknowledged}
          onChange={setAcknowledged}
          idPrefix="label"
        />
      </Form>
    </MutationDialog>
  );
}
