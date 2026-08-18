/**
 * DrainDialog — `POST /api/nodes/{name}/drain` (§5).
 *
 * The most dangerous button in this console, and the one place where the
 * mutation envelope alone is not enough to describe what is about to happen.
 * A drain's diff is one line — `spec.unschedulable: true` — and that line says
 * nothing at all about the twenty pods that are about to be evicted off a
 * machine. So the backend returns a **plan** alongside the diff, and this
 * dialog's real job is to render it.
 *
 * Three rules, each taken directly from `app/admin/nodes.py`:
 *
 * **A plan before an action.** Every pod on the node is classified `evict`,
 * `skip` or `blocked`, with a reason, and the classification comes back whether
 * or not anything executes. The operator confirming a drain is confirming a
 * list, not a verb — so the list is on screen above the Confirm button, not
 * behind an expander.
 *
 * **Confirm stays disabled while anything is blocked and `force` is off.** The
 * backend refuses this too, with `422 invalid` before the node is even cordoned.
 * Duplicating the check here is not distrust of the backend; it is rule 11.4 —
 * the action is visible, disabled, and says which pods to resolve. Flipping
 * `force` means going back to the form, because `force` changes the request and
 * a plan projected without it is not the plan that would run with it.
 *
 * **Per-pod results, and no aggregate success.** After execution each entry
 * carries `result: "evicted" | "failed"` with an `error` and an `error_code`.
 * A drain over three pods the API server refused comes back `applied: true`,
 * `failed: 3`, `drained: false` — and the headline this dialog writes for that
 * is *"Partially drained — 3 pods could not be evicted"*, never "success".
 * "Drained" over three stuck pods is the sentence that gets a machine
 * terminated with a database on it, which is the whole reason this project
 * has a defect standard.
 *
 * `force` is also described honestly: it means "I have read this plan and I
 * accept it". It does not override a PodDisruptionBudget — the API server
 * enforces those on the eviction subresource and refuses a forced drain exactly
 * as it refuses an unforced one, reported per-pod.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  NumberInput,
  Tooltip,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { DataTable, PartialBanner, SectionHeader, StatusBadge } from './ui';
import { nodes as nodesApi } from '../api/client';

const ACTION_BADGE = {
  evict: { status: 'progressing', label: 'Evict' },
  skip: { status: 'unknown', label: 'Skip' },
  blocked: { status: 'blocked', label: 'Blocked' },
};

const RESULT_BADGE = {
  evicted: { status: 'succeeded', label: 'Evicted' },
  failed: { status: 'failed', label: 'Failed' },
};

/**
 * The plan table. Rendered at every phase from the diff onwards, because the
 * plan is what the operator is consenting to and it must not vanish at the
 * moment the confirm button appears.
 *
 * `executed` switches the trailing column from "what will happen" to "what
 * happened". Before execution every `result` is null by design (the backend
 * fills the field in and leaves it null on a dry run so this component can read
 * it unconditionally), and rendering that null as a blank cell in a "Result"
 * column would read as "nothing happened to this pod" rather than "this has not
 * run yet".
 */
function PlanTable({ plan, executed }) {
  const columns = useMemo(
    () => [
      {
        key: 'pod',
        title: 'Pod',
        sortable: true,
        value: (row) => `${row.namespace}/${row.pod}`,
        cell: (row) => (
          <span>
            <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>{row.namespace}/</span>
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
            // Not a dash-because-unknown: the backend read the pod and found no
            // controller reference. That is an answer, and it is the answer that
            // makes a pod unmanaged and therefore blocked.
            <Tooltip content="This pod has no owning controller, so nothing will recreate it elsewhere.">
              <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>unmanaged</span>
            </Tooltip>
          ),
      },
      {
        key: 'action',
        title: 'Plan',
        sortable: true,
        cell: (row) => {
          const badge = ACTION_BADGE[row.action] ?? { status: 'unknown', label: row.action };
          return <StatusBadge status={badge.status} label={badge.label} tooltip={row.reason || undefined} />;
        },
      },
      {
        key: 'reason',
        title: 'Why',
        modifier: 'breakWord',
        // A skipped pod without a reason is a plan entry that cannot be
        // audited by the person reading it, so the empty string is rendered as
        // the explicit "no qualification" rather than left blank.
        cell: (row) => row.reason || <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>—</span>,
      },
      ...(executed
        ? [
            {
              key: 'result',
              title: 'Result',
              sortable: true,
              cell: (row) => {
                if (row.action === 'skip') {
                  return <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>not attempted</span>;
                }
                const badge = RESULT_BADGE[row.result];
                if (!badge) {
                  // Neither evicted nor failed after a real drain means the
                  // loop never reached this pod. Saying so beats a blank cell
                  // that reads as "fine".
                  return (
                    <Tooltip content="The drain did not report an outcome for this pod. It may not have been attempted.">
                      <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>no outcome reported</span>
                    </Tooltip>
                  );
                }
                return (
                  <span>
                    <StatusBadge status={badge.status} label={badge.label} />
                    {row.error && (
                      <div style={{ fontSize: '0.8125rem', marginBlockStart: '0.25rem' }}>
                        {row.error}
                        {row.error_code && (
                          <>
                            {' '}
                            <code>{row.error_code}</code>
                          </>
                        )}
                      </div>
                    )}
                  </span>
                );
              },
            },
          ]
        : []),
    ],
    [executed],
  );

  return (
    <DataTable
      columns={columns}
      rows={plan}
      rowKey={(row) => `${row.namespace}/${row.pod}`}
      ariaLabel="Drain plan"
      emptyTitle="No pods on this node"
      emptyDescription="Nothing needs to be evicted; the drain only marks the node unschedulable."
    />
  );
}

export function DrainDialog({ isOpen, name, onClose, onApplied }) {
  const [gracePeriodSeconds, setGracePeriod] = useState(30);
  const [ignoreDaemonSets, setIgnoreDaemonSets] = useState(true);
  const [deleteEmptyDirData, setDeleteEmptyDirData] = useState(false);
  const [force, setForce] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setGracePeriod(30);
    // kubectl's own default, and the one that makes a drain of a normal cluster
    // possible at all: every CNI and log shipper is a DaemonSet, and none of
    // them can be evicted anywhere else.
    setIgnoreDaemonSets(true);
    setDeleteEmptyDirData(false);
    setForce(false);
  }, [isOpen]);

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Drain ${name}`}
      description={
        `${name} will be marked unschedulable and its pods evicted. Eviction honours PodDisruptionBudgets — ` +
        'the API server refuses a disruption that would take an application below its configured minimum, ' +
        'and this console does not have, and does not offer, a way around that.'
      }
      isDanger
      confirmLabel="Drain node"
      // The name typed out. Draining is not reversible in the sense that
      // matters: uncordoning afterwards does not put the pods back.
      requireTyped={name}
      request={(dryRun) =>
        nodesApi.drain(name, {
          dryRun,
          gracePeriodSeconds,
          ignoreDaemonSets,
          deleteEmptyDirData,
          force,
        })
      }
      confirmBlockedReason={(result) => {
        const blocked = result?.blocked ?? 0;
        if (blocked > 0 && !force) {
          return (
            `${blocked} pod${blocked === 1 ? '' : 's'} in the plan below ${blocked === 1 ? 'is' : 'are'} ` +
            'blocked. Resolve them, or go back and set the options that cover them — ' +
            '"Delete emptyDir data" and "Ignore DaemonSets" — or acknowledge the plan with Force. ' +
            'Changing any of those re-runs the dry run, because a plan projected without them is not the ' +
            'plan that would run with them.'
          );
        }
        return null;
      }}
      renderExtra={({ result, phase, error }) => {
        // The plan reaches us two ways: on the response, and — when the backend
        // refused a forced-off drain with `422 invalid` — inside the error's
        // §1.3 context. Reading only the first would leave the operator with a
        // refusal and no list of what to fix, which is the one thing the
        // refusal exists to give them.
        const plan = result?.plan ?? error?.context?.plan ?? null;
        if (!plan) return null;

        const executed = phase === 'done' && result?.applied === true;
        const counts = plan.reduce(
          (acc, entry) => ({ ...acc, [entry.action]: (acc[entry.action] || 0) + 1 }),
          {},
        );

        return (
          <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }}>
            <SectionHeader
              title={executed ? 'What happened to each pod' : 'What this drain will do'}
              description={
                executed
                  ? 'Per-pod outcomes. A drain is only "drained" when every eviction it attempted succeeded.'
                  : `${counts.evict || 0} to evict · ${counts.skip || 0} skipped · ${counts.blocked || 0} blocked`
              }
            />

            {/* §1.2: PodDisruptionBudgets we could not read are reported, not
                swallowed. A plan that says "evict" for every pod because the
                PDB listing was denied is a plan that will surprise its
                operator, and `pdb_checked` is how the backend says which one
                this is. */}
            <PartialBanner unavailable={result?.unavailable} />
            {result && result.pdb_checked === false && (
              <Alert
                isInline
                variant="warning"
                title="PodDisruptionBudgets could not be read"
                className="admin-confirm__alert"
              >
                The plan below could not take budgets into account, so a pod shown as “evict” may still be
                refused by the API server when the drain runs. That refusal will appear as a failed
                eviction, not as a silent skip.
              </Alert>
            )}

            <PlanTable plan={plan} executed={executed} />
          </div>
        );
      }}
      summarize={(result) => {
        const failed = result?.failed ?? 0;
        const evicted = result?.evicted ?? 0;
        if (result?.applied !== true) {
          return {
            variant: 'warning',
            title: 'The drain request completed without confirming it was applied',
            body: 'Re-read the node and its pods before assuming anything moved.',
          };
        }
        // `drained` is the backend's own verdict and it is false whenever a
        // single eviction failed. This branch is the entire reason the field
        // exists separately from `applied`.
        if (result?.drained === true) {
          return {
            variant: 'success',
            title: `${name} is drained`,
            body:
              `${evicted} pod${evicted === 1 ? '' : 's'} evicted, and the node is marked unschedulable. ` +
              'Eviction is asynchronous — pods terminate over their grace period — but every eviction the ' +
              'API server was asked for was accepted.',
          };
        }
        return {
          variant: 'danger',
          title: `Partially drained — ${failed} pod${failed === 1 ? '' : 's'} could not be evicted`,
          body:
            `${name} is marked unschedulable and ${evicted} pod${evicted === 1 ? ' was' : 's were'} evicted, ` +
            `but ${failed} ${failed === 1 ? 'eviction was' : 'evictions were'} refused or failed. This node ` +
            'is NOT empty. The per-pod results below say which pods and why — resolve those before treating ' +
            'this machine as free.',
        };
      }}
      onClose={onClose}
      onApplied={onApplied}
    >
      <Form>
        <FormGroup label="Grace period (seconds)" fieldId="drain-grace">
          <NumberInput
            id="drain-grace"
            value={gracePeriodSeconds}
            min={0}
            max={3600}
            onMinus={() => setGracePeriod((v) => Math.max(0, (v ?? 0) - 5))}
            onPlus={() => setGracePeriod((v) => Math.min(3600, (v ?? 0) + 5))}
            onChange={(event) => {
              const raw = Number(event.target.value);
              setGracePeriod(Number.isFinite(raw) ? Math.max(0, Math.min(3600, Math.trunc(raw))) : 0);
            }}
            inputAriaLabel="Grace period in seconds"
            minusBtnAriaLabel="Five seconds less"
            plusBtnAriaLabel="Five seconds more"
            data-testid="drain-grace"
          />
          <p style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem', marginBlockStart: '0.25rem' }}>
            How long each pod gets to shut down cleanly. Zero kills them immediately and skips every
            preStop hook.
          </p>
        </FormGroup>

        <FormGroup label="Options" fieldId="drain-options" isStack>
          <Checkbox
            id="drain-ignore-daemonsets"
            label="Ignore DaemonSet-managed pods"
            description="Skip them rather than block on them. A DaemonSet pod cannot be evicted anywhere else — its controller puts it straight back on this node — so skipping is the only correct handling, not a compromise."
            isChecked={ignoreDaemonSets}
            onChange={(_event, checked) => setIgnoreDaemonSets(checked)}
            data-testid="drain-ignore-daemonsets"
          />
          <Checkbox
            id="drain-delete-emptydir"
            label="Delete emptyDir data"
            description="Allow evicting pods that hold an emptyDir volume. That data lives on this node and is destroyed with the pod — it is not moved and not recoverable."
            isChecked={deleteEmptyDirData}
            onChange={(_event, checked) => setDeleteEmptyDirData(checked)}
            data-testid="drain-delete-emptydir"
          />
          <Checkbox
            id="drain-force"
            label="Force — proceed past blocked pods"
            description="Means “I have read this plan and I accept it”. It does not grant the console power it lacks: a PodDisruptionBudget is enforced by the API server and refuses a forced eviction exactly as it refuses an unforced one, reported per pod."
            isChecked={force}
            onChange={(_event, checked) => setForce(checked)}
            data-testid="drain-force"
          />
        </FormGroup>

        {force && (
          <Alert isInline variant="warning" title="Force is on">
            Blocked pods will be attempted rather than stopping the drain. Unmanaged pods evicted this way
            are not recreated anywhere — nothing owns them.
          </Alert>
        )}
      </Form>
    </MutationDialog>
  );
}

export default DrainDialog;
