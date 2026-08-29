/**
 * RouterPanel — the shipped router (§14), installed and removed from here.
 *
 * This is the surface where k8boss-admin stops being purely a console and puts
 * a reverse proxy on somebody's cluster. Three things about that are visible in
 * this component on purpose, because hiding any of them would make the console
 * claim more than it does:
 *
 * **`installed` is a tri-state.** `null` is "one of the reads failed and we do
 * not know", and it renders as a warning rather than as "not installed" — which
 * would invite an operator to install a second router on top of one that is
 * already running.
 *
 * **What the router does not serve is stated up front.** The HAProxy controller
 * this console ships implements Gateway API for TCPRoute only; it will never
 * accept an HTTPRoute, and OpenShift Routes are served by OpenShift's own
 * router. An operator finding that out by watching an HTTPRoute sit unaccepted
 * is the failure this panel exists to prevent.
 *
 * **The plan is readable with the feature switched off.** `POST /api/router/plan`
 * is ungated and writes nothing, so the exact objects an install would create
 * can be read *before* deciding whether to set `ADMIN_ROUTER_MANAGE_ENABLED`.
 * Withholding it would be asking somebody to enable a feature sight unseen.
 *
 * The install itself goes through `MutationDialog` like every other write. It is
 * eight objects, so it is eight passes through the funnel and eight audit rows;
 * `installed` in the response is true only when every one of them landed.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
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
  ActionButton,
  CodeBlock,
  DescriptionList,
  NullableCell,
  PartialBanner,
  SectionHeader,
  StatusBadge,
} from './ui';
import { routerApi } from '../api/client';
import { useAsync } from '../pages/_data';

const SERVICE_TYPES = ['LoadBalancer', 'NodePort', 'ClusterIP'];

/** The install form's state. */
function blankOptions(namespace) {
  return {
    namespace: namespace || 'k8boss-router',
    serviceType: 'LoadBalancer',
    replicas: 2,
    ingressClassName: 'haproxy',
    defaultClass: false,
    gatewayApi: false,
  };
}

/**
 * The router's readiness, as a badge and a sentence.
 *
 * `Unknown` is a real state here and not a placeholder — see the module
 * docstring. It is the same tri-state discipline §5's node readiness uses.
 */
function readiness(status) {
  if (status?.installed == null) {
    return {
      badge: 'Unknown',
      tooltip:
        'One of the reads failed, so whether a router is installed is unknown. This is not "no router" — installing one now could put a second proxy on the cluster.',
    };
  }
  if (!status.installed) return { badge: 'NotReady', tooltip: 'No router installed by this console is on the cluster.' };
  const { readyReplicas, desiredReplicas } = status.deployment ?? {};
  if (readyReplicas == null) {
    return {
      badge: 'Unknown',
      tooltip:
        status.deployment?.detail ||
        'How many replicas are ready is not known — the count was not readable.',
    };
  }
  if (desiredReplicas != null && readyReplicas >= desiredReplicas && readyReplicas > 0) {
    return { badge: 'Ready', tooltip: null };
  }
  return {
    badge: readyReplicas > 0 ? 'Warning' : 'NotReady',
    tooltip: `${readyReplicas} of ${desiredReplicas ?? '?'} replicas are ready.`,
  };
}

export default function RouterPanel({ gate, onChanged }) {
  const [installOpen, setInstallOpen] = useState(false);
  const [uninstallOpen, setUninstallOpen] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const [options, setOptions] = useState(() => blankOptions());

  const { data: status, loading, error, reload } = useAsync(() => routerApi.status(), {
    key: 'router-status',
  });

  // Seed the form from the router that is actually installed, once, when the
  // dialog opens over one.
  //
  // Without this the form offers `blankOptions()` — the bundle's defaults — no
  // matter what is on the cluster, so an operator who installed with "make this
  // the default class" and Gateway API on, and later opens Reinstall to take a
  // version bump, is shown both boxes unchecked. Confirming turns their choices
  // off. The write itself is honest about it — the diff shows the Deployment
  // args and the removed annotation — but a diff across eight objects is a thin
  // place to be told that a control you never touched has changed meaning, and
  // it reads as noise from an upgrade rather than as a change of behaviour.
  //
  // Only fields the cluster can actually answer for are taken. Anything status
  // reports as null is left at the form's default rather than guessed, because
  // a guess here is written to the cluster on confirm.
  useEffect(() => {
    if (!installOpen || !status?.installed) return;
    setOptions((current) => ({
      ...current,
      namespace: status.namespace ?? current.namespace,
      serviceType: status.service?.type ?? current.serviceType,
      replicas: status.deployment?.desiredReplicas ?? current.replicas,
      ingressClassName: status.ingressClass?.name ?? current.ingressClassName,
      defaultClass: status.ingressClass?.default ?? current.defaultClass,
      gatewayApi: status.deployment?.gatewayApi ?? current.gatewayApi,
    }));
  }, [installOpen, status]);

  const { data: plan } = useAsync(
    () => routerApi.plan({ ...options, replicas: Number(options.replicas) }),
    { key: `router-plan:${JSON.stringify(options)}`, enabled: planOpen || installOpen },
  );

  const state = readiness(status);
  const installed = status?.installed;

  const install = useCallback(
    (dryRun) =>
      routerApi.install({ ...options, replicas: Number(options.replicas), dryRun }),
    [options],
  );

  const uninstall = useCallback(
    (dryRun) =>
      routerApi.uninstall({
        namespace: status?.namespace,
        ingressClassName: status?.ingressClass?.name,
        dryRun,
      }),
    [status],
  );

  const notServed = useMemo(
    () => (status?.serves ?? plan?.serves ?? []).filter((s) => !s.served),
    [status, plan],
  );

  const addresses = status?.service?.addresses ?? [];

  return (
    <Card isCompact data-testid="router-panel">
      <CardTitle>The router this console ships</CardTitle>
      <CardBody>
        <PartialBanner unavailable={status?.unavailable} />

        {error && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            title="The router's state could not be read"
          >
            {error.message}
          </Alert>
        )}

        {installed == null && !loading && !error && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="router-unknown"
            title="Whether a router is installed is unknown"
          >
            One of the reads this needs did not answer. Installing now could put a second proxy
            on the cluster&apos;s ingress path alongside one that is already running.
          </Alert>
        )}

        <DescriptionList
          items={[
            {
              label: 'State',
              value: (
                <StatusBadge
                  status={state.badge}
                  tooltip={state.tooltip ?? undefined}
                  label={installed === true ? 'Installed' : installed === false ? 'Not installed' : 'Unknown'}
                />
              ),
            },
            {
              label: 'Replicas ready',
              value: (
                <NullableCell
                  value={
                    status?.deployment?.readyReplicas == null
                      ? null
                      : `${status.deployment.readyReplicas} of ${status.deployment.desiredReplicas ?? '?'}`
                  }
                  reason={
                    status?.deployment?.detail ||
                    'The Deployment could not be read, so how many replicas are ready is unknown — not zero.'
                  }
                />
              ),
            },
            {
              label: 'Version',
              value: (
                <NullableCell
                  value={status?.installedVersion}
                  reason="The installed version could not be read, so whether it is current is unknown."
                />
              ),
            },
            { label: 'Version this console ships', value: status?.shippedVersion ?? '—' },
            { label: 'Namespace', value: status?.namespace ?? '—' },
            {
              label: 'Ingress class',
              value: status?.ingressClass?.present
                ? `${status.ingressClass.name}${status.ingressClass.default ? ' (cluster default)' : ''}`
                : '—',
            },
            {
              label: 'Reachable at',
              value: addresses.length ? (
                addresses.join(', ')
              ) : (
                <NullableCell
                  value={null}
                  reason={
                    status?.service?.detail ||
                    'No address is published for the router Service yet.'
                  }
                />
              ),
            },
          ]}
        />

        {status?.upgradeAvailable === true && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="router-upgrade"
            // Not "a newer router is available". This console knows one thing:
            // which version it ships. Whether that is newer than what is running
            // is a claim about HAProxy's release history made from a string
            // baked into this repo, and it is wrong in both directions — stale
            // after an upstream release, and backwards on a cluster running
            // something newer than this console has heard of.
            title={`The running router is not the version this console ships`}
          >
            The cluster is running {status.installedVersion}; this console ships{' '}
            {status.shippedVersion}. Installing again writes every object at the shipped version —
            which is a downgrade if the cluster is ahead. The Deployment&apos;s selector is
            unchanged, so the pods roll rather than being recreated.
          </Alert>
        )}

        {notServed.length > 0 && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="router-not-served"
            title="What this router does not serve"
          >
            <ul>
              {notServed.map((entry) => (
                <li key={entry.backend}>{entry.detail}</li>
              ))}
            </ul>
          </Alert>
        )}

        {!status?.enabled && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="router-disabled"
            title="Router management is switched off on this deployment"
          >
            {status?.enabledDetail}{' '}
            The plan below is still readable — deciding whether to enable this requires reading
            what it would create.
          </Alert>
        )}

        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem', flexWrap: 'wrap' }}>
          <ActionButton gate={gate('router-install')} variant="primary" onClick={() => setInstallOpen(true)}>
            {installed === true ? 'Reinstall or upgrade…' : 'Install the router…'}
          </ActionButton>
          <Button variant="secondary" onClick={() => setPlanOpen((open) => !open)} data-testid="router-plan-toggle">
            {planOpen ? 'Hide what it would create' : 'Show what it would create'}
          </Button>
          {installed === true && (
            <ActionButton
              gate={gate('router-uninstall')}
              variant="secondary"
              isDanger
              onClick={() => setUninstallOpen(true)}
            >
              Uninstall…
            </ActionButton>
          )}
          <Button variant="link" onClick={reload} data-testid="router-reload">
            Refresh
          </Button>
        </div>

        {planOpen && (
          <div style={{ marginTop: '1rem' }} data-testid="router-plan">
            <p>
              {plan?.objects?.length ?? 0} objects, image <code>{plan?.image}</code>. Each is
              created through the same preflight, dry run, diff and audit row as every other write
              in this console.
            </p>
            {(plan?.objects ?? []).map((object) => (
              <div key={`${object.kind}/${object.namespace ?? ''}/${object.name}`} style={{ marginTop: '0.5rem' }}>
                <strong>
                  {object.kind} {object.namespace ? `${object.namespace}/` : ''}
                  {object.name}
                </strong>
                <CodeBlock code={object.yaml} language="yaml" maxHeight={220} ariaLabel={`${object.kind} manifest`} />
              </div>
            ))}
          </div>
        )}
      </CardBody>

      {installOpen && (
        <MutationDialog
          isOpen
          title={installed === true ? 'Reinstall the router' : 'Install the router'}
          description={
            'Eight objects, each written through the mutation funnel. Nothing is created until you confirm.'
          }
          request={(dryRun) => install(dryRun)}
          previewLabel="Preview the install"
          confirmLabel="Install it"
          requireTyped={options.defaultClass ? options.ingressClassName : undefined}
          autoPreview={false}
          summarize={summarizeInstall}
          renderExtra={ObjectReport}
          onClose={() => setInstallOpen(false)}
          // Deliberately does NOT close the dialog. An install is eight writes
          // and the per-object report is the only place that says which one
          // failed and what grant it needed; closing on `applied` would replace
          // all of it with a toast carrying the headline alone.
          onApplied={() => {
            reload();
            onChanged?.();
          }}
        >
          <Form onSubmit={(event) => event.preventDefault()}>
            <Alert
              isInline
              variant="warning"
              className="admin-confirm__alert"
              title="What an install grants"
            >
              The router&apos;s ClusterRole can read every Secret in the cluster. That is what any
              ingress controller needs to terminate TLS and RBAC cannot narrow it — the exact rule
              is in the diff you are about to read.
            </Alert>

            <Grid hasGutter>
              <GridItem span={6}>
                <FormGroup label="Namespace" fieldId="router-namespace">
                  <TextInput
                    id="router-namespace"
                    value={options.namespace}
                    onChange={(_e, value) => setOptions((o) => ({ ...o, namespace: value }))}
                    data-testid="router-namespace"
                  />
                </FormGroup>
              </GridItem>
              <GridItem span={6}>
                <FormGroup label="Service type" fieldId="router-service-type">
                  <FormSelect
                    id="router-service-type"
                    value={options.serviceType}
                    onChange={(_e, value) => setOptions((o) => ({ ...o, serviceType: value }))}
                    data-testid="router-service-type"
                  >
                    {SERVICE_TYPES.map((type) => (
                      <FormSelectOption key={type} value={type} label={type} />
                    ))}
                  </FormSelect>
                  <FormHelperText>
                    <HelperText>
                      <HelperTextItem>
                        LoadBalancer on a cloud cluster; NodePort where there is no load-balancer
                        provider, because a LoadBalancer Service there never gets an address.
                      </HelperTextItem>
                    </HelperText>
                  </FormHelperText>
                </FormGroup>
              </GridItem>
              <GridItem span={6}>
                <FormGroup label="Replicas" fieldId="router-replicas">
                  <TextInput
                    id="router-replicas"
                    type="number"
                    value={options.replicas}
                    onChange={(_e, value) => setOptions((o) => ({ ...o, replicas: value }))}
                    data-testid="router-replicas"
                  />
                </FormGroup>
              </GridItem>
              <GridItem span={6}>
                <FormGroup label="Ingress class name" fieldId="router-class">
                  <TextInput
                    id="router-class"
                    value={options.ingressClassName}
                    onChange={(_e, value) => setOptions((o) => ({ ...o, ingressClassName: value }))}
                    data-testid="router-class"
                  />
                </FormGroup>
              </GridItem>
              <GridItem span={12}>
                <Checkbox
                  id="router-default-class"
                  data-testid="router-default-class"
                  isChecked={options.defaultClass}
                  onChange={(_e, checked) => setOptions((o) => ({ ...o, defaultClass: checked }))}
                  label="Make this the cluster's default IngressClass"
                  description={
                    'The most consequential option here. It makes this router claim every Ingress ' +
                    'in the cluster that names no class — including ones another controller is ' +
                    'already serving. Confirming it requires typing the class name.'
                  }
                />
              </GridItem>
              <GridItem span={12}>
                <Checkbox
                  id="router-gateway-api"
                  data-testid="router-gateway-api"
                  isChecked={options.gatewayApi}
                  onChange={(_e, checked) => setOptions((o) => ({ ...o, gatewayApi: checked }))}
                  label="Grant and enable Gateway API"
                  description={
                    'The controller implements Gateway API for TCPRoute only. Turning this on ' +
                    'does not make HTTPRoutes work — nothing this console installs serves those.'
                  }
                />
              </GridItem>
            </Grid>
          </Form>
        </MutationDialog>
      )}

      {uninstallOpen && (
        <MutationDialog
          isOpen
          isDanger
          title="Uninstall the router"
          description={
            'Every exposure this router is serving stops being reachable from outside the cluster ' +
            'the moment it goes. The namespace is left standing.'
          }
          request={(dryRun) => uninstall(dryRun)}
          previewLabel="Preview the removal"
          confirmLabel="Remove it"
          requireTyped={status?.namespace}
          autoPreview
          summarize={summarizeUninstall}
          renderExtra={ObjectReport}
          onClose={() => setUninstallOpen(false)}
          // Left open for the same reason as the install: what was skipped, and
          // what was retained, is the part worth reading.
          onApplied={() => {
            reload();
            onChanged?.();
          }}
        />
      )}
    </Card>
  );
}


/**
 * What happened to each object, on the preview and again afterwards.
 *
 * An install is eight writes, and the aggregate sentence above it ("2 of 8
 * were not created") says how many without saying which. The grant a failed
 * object needed is the only actionable thing in the whole response, and it is
 * per-object — so it is rendered per-object, and it is rendered on the `done`
 * phase as well as the diff, because that is when it matters most.
 */
function ObjectReport({ result, phase, error }) {
  const objects = result?.objects ?? [];
  if (!objects.length) return null;
  const executed = phase === 'done';

  return (
    <div style={{ marginBlockStart: 'var(--admin-gap, 1rem)' }} data-testid="router-object-report">
      <SectionHeader
        title={executed ? 'What happened to each object' : 'What this will write'}
        description={
          executed
            ? 'Each of these was a separate write through the funnel, with its own audit row. Nothing was rolled back.'
            : 'Each is preflighted, projected and audited on its own.'
        }
      />
      <ul>
        {objects.map((object) => (
          <li key={`${object.kind}/${object.namespace ?? ''}/${object.name}`}>
            <strong>
              {object.kind} {object.namespace ? `${object.namespace}/` : ''}
              {object.name}
            </strong>{' '}
            — {object.verb}
            {object.error ? (
              <>
                {' '}
                <StatusBadge status="NotReady" label={object.error.code} />{' '}
                {object.error.message}
                {object.error.hint ? ` ${object.error.hint}` : ''}
              </>
            ) : executed && object.applied ? (
              <> <StatusBadge status="Ready" label="written" /></>
            ) : (
              <> <StatusBadge status="Unknown" label="projected" tooltip="A dry run. Nothing was written." /></>
            )}
          </li>
        ))}
      </ul>
      {error && !objects.length && <p>{error.message}</p>}
    </div>
  );
}

/**
 * The install's own headline, replacing `MutationDialog`'s default.
 *
 * The default reads `applied` and says "Applied to the cluster". That is the
 * wrong sentence for eight writes of which six can succeed: `installed` is the
 * aggregate, and it is false whenever anything failed. A half-created router is
 * reported as a half-created router.
 */
function summarizeInstall(result) {
  if (result?.installed === true) {
    return {
      variant: 'success',
      title: `The router is installed (${result.version})`,
      body: null,
    };
  }
  const failed = result?.failed ?? 0;
  if (failed > 0) {
    return {
      variant: 'danger',
      title: `${failed} of ${result?.objects?.length ?? '?'} objects were not created`,
      body:
        'The router is partially installed. Nothing was rolled back — the objects that were ' +
        'created are still there, and each failure is listed with the grant it needed. Fix those ' +
        'and install again; the objects that exist will be replaced rather than duplicated.',
    };
  }
  return {
    variant: 'warning',
    title: 'The install completed without reporting that it landed',
    body: 'Re-read the router state before assuming it is running.',
  };
}

function summarizeUninstall(result) {
  if (result?.uninstalled === true) {
    return {
      variant: 'success',
      title: `${result.removed} objects removed`,
      body:
        'The namespace was left standing on purpose — deleting one deletes everything in it, ' +
        'including anything put there since, and it cannot be undone.',
    };
  }
  return {
    variant: 'danger',
    title: `${result?.failed ?? '?'} objects could not be removed`,
    body: 'The router is partially removed and may still be serving traffic.',
  };
}
