/**
 * OlmPanel — §33, installing Operator Lifecycle Manager from the portal's empty state.
 *
 * The portal on a cluster with no OLM is a page that is empty, correct, and
 * useless. This panel is what turns it into an action, and everything about how
 * it renders is shaped by one distinction the backend is careful to keep and a
 * UI could easily throw away:
 *
 * **`installed` and `ready` are two different questions.** `installed` means
 * OLM's objects are on the cluster. `ready` means the package server is
 * answering, which is what makes the catalog above show anything. Between an
 * install landing and OLM reconciling the `packageserver` ClusterServiceVersion
 * there is a minute or two in which every object exists, both Deployments are
 * Ready, and the portal is *still* empty — and it is right to be. Rendering one
 * green badge over both is the §33.6 mistake, so this panel shows them as two
 * rows and says which is which.
 *
 * Both are tri-states. `null` is "a read failed", never `false`: a panel that
 * reported "OLM is not installed" during an API outage would offer an install on
 * top of a running one.
 *
 * **The plan is readable with both gates shut**, and that is the point rather
 * than a convenience. What the plan contains is a ClusterRole granting OLM every
 * verb on every resource in every API group, and deciding whether to set
 * `ADMIN_OLM_INSTALL_ENABLED` means reading it. So the "What this installs"
 * button is never disabled by the gate; only Install is.
 */
import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  Checkbox,
  Form,
  Tooltip,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import DiffView from './DiffView';
import ConsequenceChecklist from './ConsequenceChecklist';
import { useAcknowledgements, blockedByConsequences } from './consequences';
import { CodeBlock, DescriptionList, SectionHeader, StatusBadge } from './ui';
import { portal } from '../api/client';
import { useAsync } from '../pages/_data';

/**
 * A tri-state as a badge. `null` is grey and says so — it is not a "no".
 */
function TriBadge({ value, yes, no, unknown }) {
  if (value === true) return <StatusBadge status="Ready" label={yes} />;
  if (value === false) return <StatusBadge status="NotReady" label={no} />;
  return (
    <StatusBadge
      status="Unknown"
      label={unknown}
      tooltip="A read failed, so this is unknown rather than false."
    />
  );
}

/**
 * The two counts, rendered so that a failed listing cannot look like a zero.
 *
 * `present: null` is the §0.1 corollary: "0 of 8 established" from a listing
 * nobody could make is what sends somebody to reinstall over a working OLM.
 */
function crdSummary(crds) {
  if (!crds) return '—';
  if (crds.present === null || crds.established === null) {
    return crds.detail || 'Could not be read';
  }
  return `${crds.established} of ${crds.expected} established (${crds.present} present)`;
}

export default function OlmPanel({ gate, onChanged }) {
  const [installOpen, setInstallOpen] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const [communityCatalog, setCommunityCatalog] = useState(false);

  const status = useAsync(() => portal.olmStatus(), { key: 'olm-status' });
  const data = status.data;

  const body = useMemo(() => ({ communityCatalog }), [communityCatalog]);

  // The plan is a pure read and ungated — see the module docstring. It is keyed
  // on the option so turning the catalog on re-plans, which is what makes the
  // third consequence appear before the operator reaches the confirm that would
  // otherwise refuse them.
  const plan = useAsync(() => portal.olmPlan(body), {
    key: `olm-plan:${communityCatalog}`,
    enabled: planOpen || installOpen,
  });

  const consequences = useMemo(() => plan.data?.consequences ?? [], [plan.data]);
  const [acknowledged, setAcknowledged, unacknowledged] = useAcknowledgements(
    consequences,
    String(communityCatalog),
  );

  const install = useCallback(
    (dryRun) => portal.installOlm({ ...body, acknowledgeConsequences: acknowledged, dryRun }),
    [body, acknowledged],
  );

  const installedBadge = (
    <TriBadge
      value={data?.installed}
      yes="Objects are on the cluster"
      no="Not installed"
      unknown="Unknown — a read failed"
    />
  );

  let previewBlocked;
  if (!gate?.allowed) previewBlocked = undefined; // the gate blocks Confirm, not Preview
  if (unacknowledged.length) {
    previewBlocked = `Read and tick each of: ${unacknowledged.map((c) => c.label).join('; ')}.`;
  }

  return (
    <Card isCompact data-testid="olm-panel">
      <CardBody>
        <SectionHeader
          title="Operator Lifecycle Manager"
          description={
            'This console can install it. It ships upstream’s release manifests, pinned and ' +
            'vendored byte for byte, and applies them as twenty-six ordinary writes — each ' +
            'preflighted, diffed and audited like every other write here.'
          }
        />

        <DescriptionList
          items={[
            { label: 'Shipped version', value: data?.shippedVersion ?? null },
            { label: 'Installed', value: installedBadge },
            {
              label: 'Package server answering',
              value: (
                <>
                  <TriBadge
                    value={data?.ready}
                    yes="Yes — the portal can read catalogs"
                    no="Not yet"
                    unknown="Unknown — a read failed"
                  />{' '}
                  <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>
                    A separate question from the row above. OLM registers
                    packages.operators.coreos.com only after it has reconciled the packageserver
                    ClusterServiceVersion, so &ldquo;installed but not answering&rdquo; is the
                    ordinary state for a minute after an install.
                  </span>
                </>
              ),
            },
            { label: 'CustomResourceDefinitions', value: crdSummary(data?.crds) },
            ...(data?.installed
              ? [
                  {
                    label: 'Installed by this console',
                    value: (
                      <TriBadge
                        value={data?.managedByUs}
                        yes="Yes"
                        no="No — installed by something else, and this console will not write to it"
                        unknown="Unknown"
                      />
                    ),
                  },
                ]
              : []),
          ]}
        />

        <div style={{ display: 'flex', gap: '0.5rem', marginBlockStart: '1rem' }}>
          {/* Never gated. The plan is how somebody decides whether to open the
              gate, and the object it contains is the reason the gate exists. */}
          <Button
            variant="secondary"
            data-testid="olm-plan-open"
            onClick={() => setPlanOpen(true)}
          >
            What this installs
          </Button>

          {data?.installed === true ? (
            <Tooltip content="This console does not write over an OLM that is already there — installing 0.35.0 over a running OLM would restart every operator on the cluster.">
              <span>
                <Button variant="primary" isAriaDisabled data-testid="olm-install">
                  Install
                </Button>
              </span>
            </Tooltip>
          ) : (
            <Tooltip content={gate?.allowed ? 'Twenty-six writes, nothing until you confirm.' : gate?.reason}>
              <span>
                <Button
                  variant="primary"
                  isAriaDisabled={!gate?.allowed}
                  data-testid="olm-install"
                  onClick={() => setInstallOpen(true)}
                >
                  Install Operator Lifecycle Manager
                </Button>
              </span>
            </Tooltip>
          )}

          <Button
            variant="link"
            data-testid="olm-refresh"
            onClick={() => status.reload()}
            isDisabled={status.loading}
          >
            Re-check
          </Button>
        </div>

        {data?.installed === true && data?.ready === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="olm-installed-not-ready"
            title="OLM is installed and its package server is not answering yet"
          >
            {data.packageServer?.phase
              ? `The packageserver ClusterServiceVersion is ${data.packageServer.phase}. `
              : ''}
            The catalog above stays empty until OLM finishes — that is correct, not a failure.
            Press Re-check in a minute.
          </Alert>
        )}
      </CardBody>

      {planOpen && (
        <PlanDialog
          plan={plan}
          communityCatalog={communityCatalog}
          onToggleCatalog={setCommunityCatalog}
          onClose={() => setPlanOpen(false)}
        />
      )}

      {installOpen && (
        <MutationDialog
          isOpen
          title="Install Operator Lifecycle Manager"
          description={
            'Twenty-six objects in two phases: eight CustomResourceDefinitions, a wait for each ' +
            'to report Established, then OLM itself. Nothing is created until you confirm.'
          }
          request={(dryRun) => install(dryRun)}
          previewLabel="Preview the install"
          confirmLabel="Install it"
          // The typed confirmation is the ClusterRole's own name. It is not
          // ceremony: the thing being consented to is that object, and typing it
          // is the last moment somebody can notice they did not read it.
          requireTyped="operator-lifecycle-manager"
          autoPreview={false}
          isDanger
          summarize={summarizeInstall}
          confirmBlockedReason={blockedByConsequences(acknowledged)}
          canPreview={!unacknowledged.length}
          previewDisabledReason={previewBlocked}
          renderExtra={ObjectReport}
          onClose={() => setInstallOpen(false)}
          // Deliberately does NOT close on success. The per-object report is the
          // only place that says which of twenty-six writes failed and what
          // grant it needed; closing would replace all of it with a toast.
          onApplied={() => {
            status.reload();
            onChanged?.();
          }}
        >
          <Form onSubmit={(event) => event.preventDefault()}>
            <Alert
              isInline
              variant="danger"
              className="admin-confirm__alert"
              title="Read the ClusterRole before you confirm"
            >
              <code>system:controller:operator-lifecycle-manager</code> grants every verb on every
              resource in every API group, including <code>escalate</code> and <code>bind</code>.
              That is cluster-admin plus the ability to grant cluster-admin. It is inherent to OLM
              rather than a choice this console made — OLM installs operators that ask for
              arbitrary permissions, so it has to hold them — and the exact rule is in the diff you
              are about to read.
            </Alert>

            <Checkbox
              id="olm-community-catalog"
              data-testid="olm-community-catalog"
              isChecked={communityCatalog}
              onChange={(_e, checked) => setCommunityCatalog(checked)}
              label="Also install the community catalog (operatorhubio-catalog)"
              description={
                'Off by default. It pulls quay.io/operatorhubio/catalog:latest — an unpinned ' +
                'tag, re-polled hourly — and every package it lists becomes installable here. ' +
                'An OLM with no CatalogSource is a working OLM with an empty catalog, and you can ' +
                'add one you have chosen afterwards.'
              }
            />

            <ConsequenceChecklist
              consequences={consequences}
              acknowledged={acknowledged}
              onChange={setAcknowledged}
              idPrefix="olm"
              title={
                consequences.length === 1
                  ? 'One thing about this install needs acknowledging'
                  : `${consequences.length} things about this install need acknowledging`
              }
            />

            {(plan.data?.notes ?? []).map((note) => (
              <Alert
                key={note.code}
                isInline
                variant="info"
                className="admin-confirm__alert"
                data-testid={`olm-note-${note.code}`}
                title={note.label}
              >
                {note.detail}
              </Alert>
            ))}
          </Form>
        </MutationDialog>
      )}
    </Card>
  );
}

/**
 * The manifests, with no write behind them.
 *
 * A plain dialog rather than a `MutationDialog`, because nothing here can be
 * confirmed: `POST /api/portal/olm/plan` reads no cluster and writes nothing.
 */
function PlanDialog({ plan, communityCatalog, onToggleCatalog, onClose }) {
  const objects = plan.data?.objects ?? [];
  const phases = plan.data?.phases ?? [];

  return (
    <MutationDialog
      isOpen
      title={`What installing OLM ${plan.data?.version ?? ''} writes`}
      description={
        'Upstream’s release manifests, vendored byte for byte and pinned by SHA-256. This ' +
        'reads no cluster and writes nothing — it renders with both gates shut, because ' +
        'deciding whether to open them means reading what is below.'
      }
      request={null}
      canPreview={false}
      previewDisabledReason="This is a plan. There is nothing to write from here."
      onClose={onClose}
    >
      <Form onSubmit={(event) => event.preventDefault()}>
        <Checkbox
          id="olm-plan-community-catalog"
          data-testid="olm-plan-community-catalog"
          isChecked={communityCatalog}
          onChange={(_e, checked) => onToggleCatalog(checked)}
          label="Include the community catalog (operatorhubio-catalog)"
          description="Off by default. Turning it on adds one object below, and one more thing to acknowledge."
        />

        {plan.data?.digests && (
          <DescriptionList
            items={[
              { label: 'Upstream release', value: <code>{plan.data.upstream}</code> },
              ...Object.entries(plan.data.digests).map(([file, digest]) => ({
                label: `SHA-256 of ${file}`,
                value: <code style={{ wordBreak: 'break-all' }}>{digest}</code>,
              })),
            ]}
          />
        )}

        {phases.map((phase) => (
          <div key={phase.phase}>
            <SectionHeader
              title={phase.phase === 'crds' ? 'Phase one — the APIs' : 'Phase two — OLM itself'}
              description={phase.detail}
            />
            {objects
              .filter((object) => object.phase === phase.phase)
              .map((object) => (
                <details
                  key={`${object.kind}/${object.namespace ?? ''}/${object.name}`}
                  data-testid="olm-plan-object"
                  data-kind={object.kind}
                  style={{ marginBlockEnd: '0.5rem' }}
                >
                  <summary>
                    <strong>{object.kind}</strong>{' '}
                    {object.namespace ? `${object.namespace}/` : ''}
                    {object.name}
                  </summary>
                  {/* Collapsed by default, and that is not hiding it: the
                      ClusterServiceVersion CRD alone is about a megabyte of
                      OpenAPI schema, and rendering twenty-six of these expanded
                      would bury the ClusterRole that is the one anybody needs
                      to read. Every object is here and every one opens. */}
                  <CodeBlock
                    code={object.yaml}
                    language="yaml"
                    maxHeight={320}
                    ariaLabel={`${object.kind} ${object.name} manifest`}
                  />
                </details>
              ))}
          </div>
        ))}
      </Form>
    </MutationDialog>
  );
}

/**
 * Per-object outcomes, grouped by phase, with the wait between them.
 *
 * The wait gets its own row because it is the one step that is not a write and
 * the one that most often decides the outcome: a timeout there leaves eight CRDs
 * created and eighteen objects untouched, and an operator who only saw a list of
 * objects would have no idea why the second half is blank.
 */
function ObjectReport({ result, phase, error }) {
  const objects = result?.objects ?? [];
  if (!objects.length) return null;
  const executed = phase === 'done';
  const crds = result?.crds;

  const phases = [
    { key: 'crds', title: 'Phase one — eight CustomResourceDefinitions' },
    { key: 'core', title: 'Phase two — OLM itself' },
  ];

  return (
    <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }} data-testid="olm-object-report">
      <SectionHeader
        title={executed ? 'What happened to each object' : 'What this will write'}
        description={
          executed
            ? 'Each was a separate write through the funnel, with its own audit row. Nothing was rolled back.'
            : 'Phase one is projected by the API server. Phase two cannot be — its APIs do not exist until phase one lands — so those are the bundle’s own manifests, each with a preflight of the create the real install will make.'
        }
      />

      {phases.map(({ key, title }) => {
        const inPhase = objects.filter((object) => object.phase === key);
        if (!inPhase.length) return null;
        return (
          <div key={key}>
            <SectionHeader title={title} />
            {key === 'core' && executed && crds && (
              <Alert
                isInline
                variant={crds.established ? 'info' : 'warning'}
                className="admin-confirm__alert"
                data-testid="olm-crd-wait"
                title={
                  crds.established
                    ? 'The CustomResourceDefinitions reported Established, so phase two ran'
                    : 'The wait for Established expired, so phase two was not attempted'
                }
              >
                {crds.detail ||
                  'One bounded wait inside this request — not a reconcile loop. Nothing watches OLM after this.'}
              </Alert>
            )}
            {inPhase.map((object) => (
              <div
                key={`${object.kind}/${object.namespace ?? ''}/${object.name}`}
                style={{ marginBlockEnd: '0.75rem' }}
                data-testid="olm-object"
                data-kind={object.kind}
                data-phase={object.phase}
                data-projection={object.projection ?? ''}
              >
                <div>
                  <strong>
                    {object.kind} {object.namespace ? `${object.namespace}/` : ''}
                    {object.name}
                  </strong>{' '}
                  — {object.verb} {objectBadge(object, executed)}
                </div>
                {object.skipped && (
                  <div style={{ marginBlockStart: '0.25rem', color: 'var(--admin-muted, #6a6e73)' }}>
                    {object.skipped}
                  </div>
                )}
                {object.error && (
                  <div style={{ marginBlockStart: '0.25rem' }}>
                    {object.error.message}
                    {object.error.hint ? (
                      <div style={{ fontWeight: 600 }}>{object.error.hint}</div>
                    ) : null}
                  </div>
                )}
                {!executed && object.diff?.unified && (
                  <DiffView
                    unified={object.diff.unified}
                    changed={object.diff.changed}
                    maxHeight={200}
                    ariaLabel={`${object.kind} ${object.name} diff`}
                  />
                )}
              </div>
            ))}
          </div>
        );
      })}
      {error && !objects.length && <p>{error.message}</p>}
    </div>
  );
}

function objectBadge(object, executed) {
  if (object.error) return <StatusBadge status="NotReady" label={object.error.code} />;
  if (object.skipped && executed) {
    return (
      <StatusBadge
        status="Unknown"
        label="not attempted"
        tooltip="Nothing was written for this object, and it has no error of its own — the reason is above."
      />
    );
  }
  if (executed && object.applied) return <StatusBadge status="Ready" label="written" />;
  if (object.projection === 'rendered') {
    return (
      <StatusBadge
        status="Unknown"
        label="rendered by the console"
        tooltip="The API server cannot project an object whose CustomResourceDefinition does not exist yet. This is the bundle's manifest, not an admission-checked projection."
      />
    );
  }
  return (
    <StatusBadge
      status="Unknown"
      label="projected by the API server"
      tooltip="A dry run. Nothing was written."
    />
  );
}

/**
 * The install's own headline, replacing `MutationDialog`'s default.
 *
 * Two things it must not say. It must not say "applied", because twenty-six
 * writes of which nine can fail is not one write; and it must not say OLM is
 * working, because at the moment this renders the package server has certainly
 * not registered yet. `installed` is the aggregate over the objects and the
 * headline is careful to mean only that.
 */
function summarizeInstall(result) {
  if (result?.installed === true) {
    return {
      variant: 'success',
      title: `All ${result.objects?.length ?? 26} objects were accepted (OLM ${result.version})`,
      body:
        result.readyDetail ||
        'That is not yet a running OLM. Close this and press Re-check in a minute.',
    };
  }
  const failed = result?.failed ?? 0;
  const skipped = result?.skipped ?? 0;
  if (failed > 0) {
    return {
      variant: 'danger',
      title: `${failed} of ${result?.objects?.length ?? '?'} objects were not created`,
      body:
        'OLM is partially installed. Nothing was rolled back — the objects that were created are ' +
        'still there, and each failure is listed with the grant it needed. Fix those and install ' +
        'again; the objects that exist are this console’s and are replaced, not duplicated.',
    };
  }
  if (skipped > 0) {
    return {
      variant: 'warning',
      title: `${skipped} objects were not attempted`,
      body:
        'Phase one landed and phase two did not run. The reason is in the report below — usually ' +
        'the CustomResourceDefinitions had not reported Established within the timeout. Running ' +
        'the install again is safe and picks up where this stopped.',
    };
  }
  return {
    variant: 'warning',
    title: 'The install completed without reporting that it landed',
    body: 'Re-read the OLM state before assuming anything is running.',
  };
}
