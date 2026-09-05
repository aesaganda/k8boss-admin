/**
 * NodeTaintsDialog — §24's taint write, wrapped in the §11.3 handshake.
 *
 * Three fields on one object, and §4's YAML editor could already write them.
 * What it cannot do is answer the question that decides whether this is a
 * scheduling rule or an outage, and that question is the whole dialog:
 *
 * **Which pods running here does this delete?** `NoSchedule` and
 * `PreferNoSchedule` are consulted when a pod is *placed*, so adding one moves
 * nothing. `NoExecute` is applied to what is already running, and the pods that
 * do not tolerate it are removed. The three are three entries in one dropdown,
 * so the plan below the form names the pods and the effect selector is the field
 * that changes it.
 *
 * **And that removal is a delete, not an eviction.** It does not go through
 * `pods/eviction`, so PodDisruptionBudgets do not apply to it. Two clicks away,
 * `DrainDialog` tells this same operator that eviction honours budgets and that
 * `force` will not get them past one — both true, and this is the exception. It
 * is stated as a checkbox they have to tick rather than as a note they can skim,
 * because the console taught them the opposite.
 *
 * **A pod can be neither staying nor going.** `tolerationSeconds` gives it a
 * deadline: the node looks entirely healthy for five minutes and then empties.
 * Those rows carry their delay rather than being drawn as safe.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import ConsequenceChecklist from './ConsequenceChecklist';
import { blockedByConsequences, useAcknowledgements } from './consequences';
import { DataTable, PartialBanner, SectionHeader, StatusBadge } from './ui';
import { nodes as nodesApi } from '../api/client';
import { useAsync } from '../pages/_data';

const EFFECTS = ['NoSchedule', 'PreferNoSchedule', 'NoExecute'];
const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** The plan is keyed on this, so an edit re-asks the server rather than guessing. */
function signatureOf(taints) {
  return taints.map((t) => `${t.key}=${t.value}:${t.effect}`).join('|');
}

/** `{key, value, effect}` with the blanks the API omits, ready to send. */
function wireTaints(rows) {
  return rows
    .filter((row) => row.key.trim())
    .map((row) => ({
      key: row.key.trim(),
      value: row.value.trim() ? row.value.trim() : null,
      effect: row.effect,
    }));
}

/**
 * The pods a NoExecute taint removes, with when.
 *
 * `null` is not an empty table. A pod listing that failed means nobody counted,
 * and drawing that as "this taint deletes nothing" over a node full of work is
 * the exact confident-and-wrong answer this project is written against — so the
 * two render as different panels.
 */
function DeletionPanel({ deleting, podsChecked }) {
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
            <StatusBadge
              status="Unknown"
              label="unmanaged"
              tooltip="Nothing owns this pod, so nothing recreates it — here or anywhere else."
            />
          ),
      },
      {
        key: 'when',
        title: 'When',
        sortable: true,
        value: (row) => row.delay_seconds,
        cell: (row) =>
          row.delay_seconds === 0 ? (
            <StatusBadge status="failed" label="immediately" />
          ) : (
            <StatusBadge
              status="progressing"
              label={`in ${row.delay_seconds}s`}
              tooltip="This pod tolerates the taint for a limited time. It keeps running until that timer expires and is then deleted."
            />
          ),
      },
      {
        key: 'taint',
        title: 'Taint',
        cell: (row) => (
          <code>
            {row.taint.key}
            {row.taint.value ? `=${row.taint.value}` : ''}:{row.taint.effect}
          </code>
        ),
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
        data-testid="taint-pods-unknown"
        title="The pods on this node could not be listed"
      >
        This console cannot tell you which pods a NoExecute taint would delete, or
        how many. That is not a report that there are none.
      </Alert>
    );
  }

  return (
    <DataTable
      columns={columns}
      rows={deleting ?? []}
      rowKey={(row) => `${row.namespace}/${row.pod}`}
      ariaLabel="Pods this taint deletes"
      emptyTitle="No pod on this node is deleted by this change"
      emptyDescription="Every pod running here either tolerates the taints being added, or the change adds no NoExecute taint. The listing succeeded."
    />
  );
}

/** One editable taint row. */
function TaintRow({ row, index, onChange, onRemove }) {
  return (
    <Grid hasGutter data-testid={`taint-row-${index}`}>
      <GridItem span={4}>
        <TextInput
          aria-label={`Taint ${index + 1} key`}
          data-testid={`taint-key-${index}`}
          value={row.key}
          onChange={(_e, value) => onChange({ ...row, key: value })}
          placeholder="key"
        />
      </GridItem>
      <GridItem span={3}>
        <TextInput
          aria-label={`Taint ${index + 1} value`}
          data-testid={`taint-value-${index}`}
          value={row.value}
          onChange={(_e, value) => onChange({ ...row, value })}
          placeholder="value (optional)"
        />
      </GridItem>
      <GridItem span={3}>
        <FormSelect
          aria-label={`Taint ${index + 1} effect`}
          data-testid={`taint-effect-${index}`}
          value={row.effect}
          onChange={(_e, value) => onChange({ ...row, effect: value })}
        >
          {EFFECTS.map((effect) => (
            <FormSelectOption key={effect} value={effect} label={effect} />
          ))}
        </FormSelect>
      </GridItem>
      <GridItem span={2}>
        <Button
          variant="link"
          isDanger
          data-testid={`taint-remove-${index}`}
          onClick={onRemove}
        >
          Remove
        </Button>
      </GridItem>
    </Grid>
  );
}

export default function NodeTaintsDialog({ name, onClose, onApplied }) {
  const [rows, setRows] = useState([]);
  const [seeded, setSeeded] = useState(false);

  const taints = useMemo(() => wireTaints(rows), [rows]);
  const signature = signatureOf(taints);

  const { data: plan, loading, error } = useAsync(
    () => nodesApi.taintPlan(name, { taints }),
    { key: `node-taints:${name}:${signature}` },
  );

  // Seeded from the first plan, not from a row the page passed down: the plan's
  // `current` is the node as it was a moment ago rather than as a table loaded
  // it, and it is what the diff on screen is taken against.
  const current = plan?.current;
  useEffect(() => {
    if (seeded || !current) return;
    setSeeded(true);
    setRows(
      current.map((taint) => ({
        key: taint.key ?? '',
        value: taint.value ?? '',
        effect: taint.effect ?? 'NoSchedule',
      })),
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
      nodesApi.setTaints(name, {
        taints,
        resourceVersion,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [name, taints, resourceVersion, acknowledged],
  );

  let previewDisabledReason = null;
  if (error) previewDisabledReason = error.message;
  else if (loading && !plan) previewDisabledReason = 'Still reading the node…';
  else if (rows.some((row) => !row.key.trim()))
    previewDisabledReason = 'Every taint needs a key. Remove the blank row or fill it in.';
  else if (plan?.blocked) previewDisabledReason = plan.blocked.message;
  else if (unacknowledged.length)
    previewDisabledReason = `Acknowledge what this change means: ${unacknowledged
      .map((entry) => entry.label)
      .join('; ')}.`;

  return (
    <MutationDialog
      isOpen
      title={`Taints on ${name}`}
      description={`Taints decide what the scheduler will place here, and a NoExecute taint also removes pods that are already running and do not tolerate it.`}
      request={request}
      resourceVersion={resourceVersion}
      canPreview={!previewDisabledReason}
      previewDisabledReason={previewDisabledReason}
      previewLabel="Preview the change"
      confirmLabel="Set the taints"
      isDanger
      autoPreview={false}
      confirmBlockedReason={blockedByConsequences(acknowledged)}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `The taints on ${name} are now what you sent`,
              body:
                'That is what this write changed. Pods the taint removes are deleted by the ' +
                'taint manager on its own schedule, and any carrying a tolerationSeconds are ' +
                'still running by design — re-read the node rather than assuming it is empty.',
            }
          : {
              variant: 'warning',
              title: 'The write completed without confirming it was applied',
              body: 'Re-read the node before assuming spec.taints changed.',
            }
      }
    >
      <Form onSubmit={(e) => e.preventDefault()} data-testid="taint-form">
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="taint-disabled"
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
            data-testid="taint-plan-error"
            title="This node could not be read"
          >
            {error.message}
            {error.hint ? ` ${error.hint}` : ''}
          </Alert>
        )}

        <SectionHeader
          title="Taints"
          description="The whole list, not a change to it: spec.taints is an atomic list in the API, so every write replaces it. A row removed here is a taint removed from the node."
        />

        {rows.map((row, index) => (
          <TaintRow
            // Index-keyed on purpose: these rows have no identity of their own
            // until they have a key, and a half-typed one must not remount and
            // lose focus on every character.
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
          <p style={MUTED} data-testid="taint-none">
            This node carries no taints. Everything the scheduler considers can be placed
            here.
          </p>
        )}

        <div>
          <Button
            variant="secondary"
            data-testid="taint-add"
            onClick={() =>
              setRows((all) => [...all, { key: '', value: '', effect: 'NoSchedule' }])
            }
          >
            Add a taint
          </Button>
        </div>

        <SectionHeader
          title="What this does to the pods running here"
          description="Read before the effect selector: it is the field that decides whether this list is empty."
        />

        <PartialBanner unavailable={plan?.unavailable} />
        <DeletionPanel
          deleting={plan?.deleting}
          podsChecked={plan?.pods_checked !== false}
        />

        {plan?.blocked && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="taint-blocked"
            title="Nothing would change"
          >
            {plan.blocked.message} {plan.blocked.hint}
          </Alert>
        )}

        <ConsequenceChecklist
          consequences={consequences}
          acknowledged={acknowledged}
          onChange={setAcknowledged}
          idPrefix="taint"
        />
      </Form>
    </MutationDialog>
  );
}
