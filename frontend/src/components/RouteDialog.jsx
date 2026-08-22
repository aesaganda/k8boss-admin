/**
 * RouteDialog — the exposure configuration screen (§13), form and YAML.
 *
 * The OpenShift console offers the same two views and warns you that switching
 * between them discards your changes. This one does not need the warning,
 * because the two are not independent editors:
 *
 *   **The YAML document is the source of truth. The form is a projection of it.**
 *
 * Every form edit is compiled server-side by `POST /api/routes/render`, which
 * patches the form's fields *into the current document* rather than generating
 * a fresh object. A `spec.rules[1]` somebody hand-wrote, a controller
 * annotation, an `externalCertificate` reference — all of it survives a trip
 * through the form, and `preserved[]` names each one so the form can say out
 * loud which parts of the document it is not showing. A console that discarded
 * a field an operator wrote, at the moment they touched an unrelated control,
 * would be silently changing clusters.
 *
 * Typing in the YAML view is the other direction, and it is deliberately
 * one-way: the document becomes whatever was typed, and the form fields are
 * left as they were rather than being re-derived. Parsing an arbitrary document
 * back into the form model is where the field-dropping would come back — a
 * document the form cannot represent would round-trip to one it can.
 * `documentIsHandEdited` says so on screen, and from that point the form's
 * controls are disabled with the reason, so nothing can silently overwrite what
 * was typed.
 *
 * ## Lossy backends
 *
 * An Ingress cannot express passthrough TLS. The backend does not emit a
 * controller-specific annotation and hope, and it does not quietly downgrade —
 * it returns `lossy[]`, and the write endpoint **refuses** until each entry is
 * acknowledged by name. That acknowledgement is a checkbox here, and it resets
 * whenever the rendered `lossy` list changes, so an operator who acknowledged
 * one consequence and then edited the form has to read the new one.
 *
 * The whole thing hangs inside `MutationDialog`, which owns the §11.3
 * handshake: form → dry run → diff → confirm. Nothing here reaches a cluster.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
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
  Tab,
  TabTitleText,
  Tabs,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';
import PlusCircleIcon from '@patternfly/react-icons/dist/esm/icons/plus-circle-icon';
import TrashIcon from '@patternfly/react-icons/dist/esm/icons/trash-icon';
import MutationDialog from './MutationDialog';
import YamlEditor from './YamlEditor';
import { routes as routesApi } from '../api/client';

/** Termination modes, in the order the OpenShift console offers them. */
const TERMINATIONS = [
  { value: '', label: 'None — plain HTTP' },
  { value: 'edge', label: 'Edge — the router terminates TLS' },
  { value: 'passthrough', label: 'Passthrough — TLS reaches the pod untouched' },
  { value: 'reencrypt', label: 'Re-encrypt — terminate, then re-encrypt to the pod' },
];

const INSECURE_POLICIES = [
  { value: '', label: 'Refuse plain HTTP' },
  { value: 'Redirect', label: 'Redirect plain HTTP to HTTPS' },
  { value: 'Allow', label: 'Serve plain HTTP as well' },
];

/** An empty exposure, as the form's state. */
function blankSpec(namespace) {
  return {
    name: '',
    namespace: namespace || '',
    host: '',
    subdomain: '',
    path: '/',
    pathType: 'Prefix',
    targets: [{ service: '', port: '', weight: null }],
    tls: {
      termination: '',
      insecurePolicy: '',
      secretName: '',
      destinationCACertificate: '',
    },
    wildcardPolicy: '',
    ingressClassName: '',
    parentRefs: [],
    labels: {},
    annotations: {},
  };
}

/**
 * The wire form of the form's state.
 *
 * Blanks become `null` rather than `""`. The backend normalises them too, but
 * doing it here as well keeps the request honest about what was left empty —
 * and `""` for a hostname is a value the API server would store.
 */
function toWire(spec) {
  const blank = (value) => {
    const text = String(value ?? '').trim();
    return text === '' ? null : text;
  };
  return {
    name: blank(spec.name) ?? '',
    namespace: blank(spec.namespace) ?? '',
    host: blank(spec.host),
    subdomain: blank(spec.subdomain),
    path: blank(spec.path),
    pathType: spec.pathType || 'Prefix',
    targets: (spec.targets || [])
      .filter((t) => blank(t.service))
      .map((t) => ({
        service: blank(t.service),
        port: blank(t.port),
        // Only sent when the operator is actually splitting traffic. A weight on
        // a single-backend exposure would make the backend report a traffic
        // split as lossy on Ingress for something nobody configured.
        weight: t.weight == null || t.weight === '' ? null : Number(t.weight),
      })),
    tls: {
      termination: blank(spec.tls.termination),
      insecurePolicy: blank(spec.tls.insecurePolicy),
      secretName: blank(spec.tls.secretName),
      destinationCACertificate: blank(spec.tls.destinationCACertificate),
    },
    wildcardPolicy: blank(spec.wildcardPolicy),
    ingressClassName: blank(spec.ingressClassName),
    parentRefs: spec.parentRefs || [],
    labels: spec.labels || {},
    annotations: spec.annotations || {},
  };
}

/** `{feature: supported}` for one backend from the capabilities envelope. */
function featureMap(backendEntry) {
  const map = {};
  for (const feature of backendEntry?.features ?? []) {
    map[feature.feature] = feature.supported;
  }
  return map;
}

/**
 * A control that says why it is inert (rule 11.4), rather than vanishing.
 *
 * Wrapped in a Tooltip on a span, for the same reason `ActionButton` does it: a
 * natively disabled input swallows the hover, and a reason nobody can read is
 * the same as not having written one.
 */
function Reasoned({ reason, children }) {
  if (!reason) return children;
  return (
    <Tooltip content={reason}>
      <span className="admin-gated-action" style={{ display: 'block' }}>
        {children}
      </span>
    </Tooltip>
  );
}

export default function RouteDialog({
  isOpen,
  /** The capabilities envelope, already read by the page. */
  capabilities,
  /** Editing an existing exposure: its row, manifest and resourceVersion. */
  existing = null,
  defaultNamespace = '',
  defaultIngressClass = '',
  onClose,
  onApplied,
}) {
  const editing = existing != null;

  const [backend, setBackend] = useState(
    () => existing?.route?.backend ?? capabilities?.items?.find((b) => b.state === 'available')?.backend ?? 'ingress',
  );
  const [spec, setSpec] = useState(() => blankSpec(defaultNamespace));
  const [tab, setTab] = useState('form');
  const [document, setDocument] = useState('');
  const [handEdited, setHandEdited] = useState(false);
  const [yamlValid, setYamlValid] = useState(true);
  const [rendered, setRendered] = useState(null);
  const [renderError, setRenderError] = useState(null);
  const [acknowledged, setAcknowledged] = useState([]);

  const backends = capabilities?.items ?? [];
  const entry = backends.find((b) => b.backend === backend);
  const features = useMemo(() => featureMap(entry), [entry]);

  /* ── Seeding ─────────────────────────────────────────────────────────── */

  // Reset on every opening. A dialog reopened for a different exposure that
  // kept the previous document would show the operator someone else's object
  // under the right title.
  useEffect(() => {
    if (!isOpen) return;
    setTab('form');
    setHandEdited(false);
    setRendered(null);
    setRenderError(null);
    setAcknowledged([]);
    if (editing) {
      const row = existing.route;
      setBackend(row.backend);
      setSpec({
        name: row.name ?? '',
        namespace: row.namespace ?? '',
        host: row.hosts?.[0] ?? '',
        subdomain: row.subdomain ?? '',
        path: row.path ?? '/',
        pathType: row.pathType === 'Exact' ? 'Exact' : 'Prefix',
        targets: (row.targets ?? []).length
          ? row.targets.map((t) => ({
              service: t.service ?? '',
              port: t.port ?? '',
              weight: t.weight,
            }))
          : [{ service: '', port: '', weight: null }],
        tls: {
          termination: row.tls?.termination ?? '',
          insecurePolicy: row.tls?.insecurePolicy ?? '',
          secretName: row.tls?.secretName ?? '',
          destinationCACertificate: '',
        },
        wildcardPolicy: row.wildcardPolicy ?? '',
        ingressClassName: row.ingressClass ?? defaultIngressClass ?? '',
        parentRefs: (row.parents ?? []).map((name) => ({ name })),
        labels: {},
        annotations: {},
      });
      setDocument('');
    } else {
      setSpec({ ...blankSpec(defaultNamespace), ingressClassName: defaultIngressClass ?? '' });
      setDocument('');
    }
  }, [isOpen, editing, existing, defaultNamespace, defaultIngressClass]);

  /* ── Rendering ───────────────────────────────────────────────────────── */

  // Every form edit re-compiles server-side, because the compilation and its
  // `lossy[]` list are facts about the backend kind and must not have a second
  // implementation in the browser that can disagree with the one the write
  // endpoint enforces.
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const wire = useMemo(() => toWire(spec), [spec]);
  const renderKey = JSON.stringify({ backend, wire, document: handEdited ? document : null });

  useEffect(() => {
    if (!isOpen) return undefined;
    // Nothing to compile until there is a name, a namespace and a Service. The
    // backend would refuse with a 422 naming the field, and firing that on every
    // keystroke of an empty form turns validation into noise.
    if (!wire.name || !wire.namespace || !wire.targets.length) {
      setRendered(null);
      setRenderError(null);
      return undefined;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const result = await routesApi.render(
          handEdited
            ? { backend, spec: null, document }
            : {
                backend,
                spec: wire,
                document: editing ? existingManifestYaml(existing) : null,
              },
        );
        if (cancelled || !live.current) return;
        setRendered(result);
        setRenderError(null);
        if (!handEdited) setDocument(result.yaml);
      } catch (error) {
        if (cancelled || !live.current) return;
        setRendered(null);
        setRenderError(error);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderKey, isOpen]);

  // The acknowledgement is per *list*, not a boolean: a UI that acknowledged
  // once and then changed the form would carry consent forward onto a
  // consequence nobody read. The backend enforces this too; resetting here is
  // what stops the operator hitting the refusal at confirm time.
  const lossy = rendered?.lossy ?? [];
  const lossySignature = lossy.map((entry_) => entry_.feature).sort().join(',');
  const lastSignature = useRef('');
  useEffect(() => {
    if (lastSignature.current !== lossySignature) {
      lastSignature.current = lossySignature;
      setAcknowledged([]);
    }
  }, [lossySignature]);

  /* ── The request ─────────────────────────────────────────────────────── */

  const request = useCallback(
    (dryRun, { resourceVersion }) => {
      // Hand-edited: send the document and **no** spec, so the backend writes
      // it verbatim. Sending the spec as well would have the form's fields
      // recompiled over the top of the edit at write time — silently undoing
      // the change the form lock exists to protect.
      const body = handEdited
        ? { backend, spec: null, document, acknowledgeLossy: [], dryRun }
        : {
            backend,
            spec: wire,
            document: null,
            acknowledgeLossy: acknowledged,
            dryRun,
          };
      if (editing) {
        return routesApi.update(backend, existing.route.namespace, existing.route.name, {
          ...body,
          resourceVersion,
        });
      }
      return routesApi.create(body);
    },
    [backend, wire, document, handEdited, acknowledged, editing, existing],
  );

  /* ── Gating ──────────────────────────────────────────────────────────── */

  const unacknowledged = lossy.filter((e) => !acknowledged.includes(e.feature));
  let previewBlocked = null;
  if (!wire.name) previewBlocked = 'Give the exposure a name.';
  else if (!wire.namespace) previewBlocked = 'Choose a namespace.';
  else if (!wire.targets.length) previewBlocked = 'Name at least one target Service.';
  else if (handEdited && !yamlValid) previewBlocked = 'The document in the YAML view does not parse.';
  else if (renderError) previewBlocked = renderError.message;
  else if (!rendered) previewBlocked = 'Still compiling the object…';
  else if (unacknowledged.length) {
    previewBlocked = `Acknowledge what ${entry?.kind ?? 'this backend'} cannot express: ${unacknowledged
      .map((e) => e.label)
      .join('; ')}.`;
  }

  const setTls = (patch) => setSpec((s) => ({ ...s, tls: { ...s.tls, ...patch } }));
  const setTarget = (index, patch) =>
    setSpec((s) => ({
      ...s,
      targets: s.targets.map((t, i) => (i === index ? { ...t, ...patch } : t)),
    }));

  const formDisabledReason = handEdited
    ? 'The YAML has been edited by hand. The form is disabled so it cannot overwrite what you typed — clear the YAML edit to use it again.'
    : null;

  /* ── Render ──────────────────────────────────────────────────────────── */

  return (
    <MutationDialog
      isOpen={isOpen}
      title={editing ? `Edit route ${existing.route.namespace}/${existing.route.name}` : 'Expose a Service'}
      description={
        editing
          ? 'The object is replaced. The diff below is what changes on the cluster.'
          : 'This creates one object. Nothing is written until you confirm the diff.'
      }
      request={request}
      resourceVersion={existing?.route?.resourceVersion ?? null}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      previewLabel="Preview the object"
      confirmLabel={editing ? 'Replace it' : 'Create it'}
      autoPreview={false}
      onApplied={onApplied}
      onClose={onClose}
      variant="large"
      className="admin-route-dialog"
    >
      <Form onSubmit={(event) => event.preventDefault()} data-testid="route-form">
        {editing && existing?.route?.managedBy?.detail && (
          <Alert
            isInline
            variant={existing.route.managedBy.controller ? 'warning' : 'info'}
            className="admin-confirm__alert"
            data-testid="route-managed-by"
            title={
              existing.route.managedBy.controller
                ? 'Something else owns this exposure and will put its version back'
                : 'This exposure was deployed by a tool, not created by hand'
            }
          >
            {existing.route.managedBy.detail}
          </Alert>
        )}

        <FormGroup label="Route backend" fieldId="route-backend" isRequired>
          <FormSelect
            id="route-backend"
            value={backend}
            onChange={(_event, value) => setBackend(value)}
            isDisabled={editing}
            data-testid="route-backend"
          >
            {backends.map((b) => (
              <FormSelectOption
                key={b.backend}
                value={b.backend}
                label={
                  b.state === 'available'
                    ? b.label
                    : b.state === 'unknown'
                      ? `${b.label} — cannot tell whether this cluster serves it`
                      : `${b.label} — not on this cluster`
                }
                // Unavailable backends stay listed and disabled with the reason
                // rather than being dropped. "Why can I not choose a Route" is
                // asked exactly on the clusters where the answer matters.
                isDisabled={b.state !== 'available'}
              />
            ))}
          </FormSelect>
          <FormHelperText>
            <HelperText>
              <HelperTextItem data-testid="route-backend-summary">
                {entry?.summary ?? ''}
              </HelperTextItem>
              {entry?.state === 'unknown' && (
                <HelperTextItem variant="warning" data-testid="route-backend-unknown">
                  {entry.detail}
                </HelperTextItem>
              )}
            </HelperText>
          </FormHelperText>
        </FormGroup>

        <Tabs
          activeKey={tab}
          onSelect={(_event, key) => setTab(key)}
          aria-label="Route configuration views"
        >
          <Tab eventKey="form" title={<TabTitleText>Form</TabTitleText>} data-testid="route-tab-form">
            <div style={{ paddingTop: '1rem' }}>
              {handEdited && (
                <Alert
                  isInline
                  variant="info"
                  className="admin-confirm__alert"
                  data-testid="route-form-locked"
                  title="The YAML has been edited by hand"
                >
                  The form is disabled so that changing a field here cannot overwrite what you
                  typed, and the write sends the document with no form model at all — so what
                  gets written is exactly what is in the YAML view.{' '}
                  <Button
                    variant="link"
                    isInline
                    onClick={() => {
                      setHandEdited(false);
                      if (rendered) setDocument(rendered.yaml);
                    }}
                    data-testid="route-discard-yaml-edit"
                  >
                    Discard the YAML edit and use the form
                  </Button>
                </Alert>
              )}

              <Reasoned reason={formDisabledReason}>
                <Grid hasGutter>
                  <GridItem span={6}>
                    <FormGroup label="Name" fieldId="route-name" isRequired>
                      <TextInput
                        id="route-name"
                        value={spec.name}
                        isDisabled={editing || handEdited}
                        onChange={(_e, value) => setSpec((s) => ({ ...s, name: value }))}
                        data-testid="route-name"
                      />
                    </FormGroup>
                  </GridItem>
                  <GridItem span={6}>
                    <FormGroup label="Namespace" fieldId="route-namespace" isRequired>
                      <TextInput
                        id="route-namespace"
                        value={spec.namespace}
                        isDisabled={editing || handEdited}
                        onChange={(_e, value) => setSpec((s) => ({ ...s, namespace: value }))}
                        data-testid="route-namespace"
                      />
                      <FormHelperText>
                        <HelperText>
                          <HelperTextItem>
                            All three route kinds are namespaced and must sit with the Service
                            they target.
                          </HelperTextItem>
                        </HelperText>
                      </FormHelperText>
                    </FormGroup>
                  </GridItem>

                  <GridItem span={8}>
                    <FormGroup label="Hostname" fieldId="route-host">
                      <TextInput
                        id="route-host"
                        value={spec.host}
                        isDisabled={handEdited}
                        placeholder={
                          features['generated-host']
                            ? 'Leave blank to let the router pick one'
                            : 'shop.example.com'
                        }
                        onChange={(_e, value) => setSpec((s) => ({ ...s, host: value }))}
                        data-testid="route-host"
                      />
                      {!features['generated-host'] && !spec.host && (
                        <FormHelperText>
                          <HelperText>
                            <HelperTextItem variant="warning" data-testid="route-host-warning">
                              {entry?.kind === 'Ingress'
                                ? 'An Ingress with no hostname matches every hostname that reaches the controller, and collides with every other host-less Ingress on it.'
                                : 'This backend cannot generate a hostname; without one the exposure inherits whatever its parent serves.'}
                            </HelperTextItem>
                          </HelperText>
                        </FormHelperText>
                      )}
                    </FormGroup>
                  </GridItem>
                  <GridItem span={4}>
                    <FormGroup label="Path" fieldId="route-path">
                      <TextInput
                        id="route-path"
                        value={spec.path}
                        isDisabled={handEdited}
                        onChange={(_e, value) => setSpec((s) => ({ ...s, path: value }))}
                        data-testid="route-path"
                      />
                    </FormGroup>
                  </GridItem>

                  <GridItem span={4}>
                    <FormGroup label="Path matching" fieldId="route-path-type">
                      <Reasoned
                        reason={
                          features['path-exact']
                            ? null
                            : `A ${entry?.kind ?? 'Route'} matches its path as a prefix; there is no exact mode.`
                        }
                      >
                        <FormSelect
                          id="route-path-type"
                          value={spec.pathType}
                          isDisabled={handEdited}
                          onChange={(_e, value) => setSpec((s) => ({ ...s, pathType: value }))}
                          data-testid="route-path-type"
                        >
                          <FormSelectOption value="Prefix" label="Prefix" />
                          <FormSelectOption
                            value="Exact"
                            label={features['path-exact'] ? 'Exact' : 'Exact — not supported here'}
                          />
                        </FormSelect>
                      </Reasoned>
                    </FormGroup>
                  </GridItem>

                  {backend === 'ingress' && (
                    <GridItem span={4}>
                      <FormGroup label="Ingress class" fieldId="route-class">
                        <TextInput
                          id="route-class"
                          value={spec.ingressClassName}
                          isDisabled={handEdited}
                          onChange={(_e, value) =>
                            setSpec((s) => ({ ...s, ingressClassName: value }))
                          }
                          data-testid="route-class"
                        />
                        <FormHelperText>
                          <HelperText>
                            <HelperTextItem>
                              An Ingress naming a class no IngressClass object matches is accepted
                              by the API server and served by nothing.
                            </HelperTextItem>
                          </HelperText>
                        </FormHelperText>
                      </FormGroup>
                    </GridItem>
                  )}

                  {backend === 'gateway' && (
                    <GridItem span={4}>
                      <FormGroup label="Parent Gateway" fieldId="route-parent" isRequired>
                        <TextInput
                          id="route-parent"
                          value={spec.parentRefs?.[0]?.name ?? ''}
                          isDisabled={handEdited}
                          onChange={(_e, value) =>
                            setSpec((s) => ({ ...s, parentRefs: value ? [{ name: value }] : [] }))
                          }
                          data-testid="route-parent"
                        />
                        <FormHelperText>
                          <HelperText>
                            <HelperTextItem>
                              An HTTPRoute is served by a Gateway&apos;s listener, and TLS is
                              configured there rather than here.
                            </HelperTextItem>
                          </HelperText>
                        </FormHelperText>
                      </FormGroup>
                    </GridItem>
                  )}
                </Grid>
              </Reasoned>

              {/* ── Targets ───────────────────────────────────────────── */}

              <FormGroup
                label="Target Services"
                fieldId="route-targets"
                style={{ marginTop: '1rem' }}
                isRequired
              >
                {spec.targets.map((target, index) => (
                  <Grid hasGutter key={index} style={{ marginBottom: '0.5rem' }}>
                    <GridItem span={5}>
                      <TextInput
                        aria-label={`Target ${index + 1} Service`}
                        placeholder="Service name"
                        value={target.service}
                        isDisabled={handEdited}
                        onChange={(_e, value) => setTarget(index, { service: value })}
                        data-testid={`route-target-service-${index}`}
                      />
                    </GridItem>
                    <GridItem span={3}>
                      <TextInput
                        aria-label={`Target ${index + 1} port`}
                        placeholder="Port"
                        value={target.port ?? ''}
                        isDisabled={handEdited}
                        onChange={(_e, value) => setTarget(index, { port: value })}
                        data-testid={`route-target-port-${index}`}
                      />
                    </GridItem>
                    <GridItem span={2}>
                      <Reasoned
                        reason={
                          features['weighted-backends']
                            ? null
                            : `${entry?.kind ?? 'This backend'} has no weight field.`
                        }
                      >
                        <TextInput
                          aria-label={`Target ${index + 1} weight`}
                          placeholder="Weight"
                          value={target.weight ?? ''}
                          isDisabled={handEdited || spec.targets.length < 2}
                          onChange={(_e, value) =>
                            setTarget(index, { weight: value === '' ? null : value })
                          }
                          data-testid={`route-target-weight-${index}`}
                        />
                      </Reasoned>
                    </GridItem>
                    <GridItem span={2}>
                      {spec.targets.length > 1 && (
                        <Button
                          variant="plain"
                          aria-label={`Remove target ${index + 1}`}
                          icon={<TrashIcon />}
                          isDisabled={handEdited}
                          onClick={() =>
                            setSpec((s) => ({
                              ...s,
                              targets: s.targets.filter((_, i) => i !== index),
                            }))
                          }
                          data-testid={`route-target-remove-${index}`}
                        />
                      )}
                    </GridItem>
                  </Grid>
                ))}
                {spec.targets.length < 4 && (
                  <Button
                    variant="link"
                    isInline
                    icon={<PlusCircleIcon />}
                    isDisabled={handEdited}
                    onClick={() =>
                      setSpec((s) => ({
                        ...s,
                        // A second target is a traffic split, so both get an
                        // explicit weight — leaving the first one null would
                        // make the split's shares depend on an API default the
                        // operator never saw.
                        targets: [
                          ...s.targets.map((t, i) =>
                            i === 0 && t.weight == null ? { ...t, weight: 100 } : t,
                          ),
                          { service: '', port: '', weight: 0 },
                        ],
                      }))
                    }
                    data-testid="route-add-target"
                  >
                    Split traffic to another Service
                  </Button>
                )}
              </FormGroup>

              {/* ── TLS ───────────────────────────────────────────────── */}

              <Grid hasGutter style={{ marginTop: '1rem' }}>
                <GridItem span={6}>
                  <FormGroup label="TLS termination" fieldId="route-termination">
                    <FormSelect
                      id="route-termination"
                      value={spec.tls.termination}
                      isDisabled={handEdited}
                      onChange={(_e, value) => setTls({ termination: value })}
                      data-testid="route-termination"
                    >
                      {TERMINATIONS.map((mode) => {
                        const feature =
                          mode.value === '' ? null : `${mode.value === 'edge' ? 'edge' : mode.value}-tls`;
                        const supported = feature == null || features[feature];
                        return (
                          <FormSelectOption
                            key={mode.value}
                            value={mode.value}
                            // Not disabled when unsupported: choosing it is
                            // legitimate, and the consequence is disclosed
                            // below with a checkbox rather than hidden behind a
                            // control the operator cannot reach.
                            label={supported ? mode.label : `${mode.label} — see the warning below`}
                          />
                        );
                      })}
                    </FormSelect>
                  </FormGroup>
                </GridItem>
                <GridItem span={6}>
                  <FormGroup label="Plain HTTP" fieldId="route-insecure">
                    <FormSelect
                      id="route-insecure"
                      value={spec.tls.insecurePolicy}
                      isDisabled={handEdited || !spec.tls.termination}
                      onChange={(_e, value) => setTls({ insecurePolicy: value })}
                      data-testid="route-insecure"
                    >
                      {INSECURE_POLICIES.map((policy) => (
                        <FormSelectOption
                          key={policy.value}
                          value={policy.value}
                          label={policy.label}
                        />
                      ))}
                    </FormSelect>
                  </FormGroup>
                </GridItem>

                {spec.tls.termination && (
                  <GridItem span={6}>
                    <FormGroup label="Certificate Secret" fieldId="route-secret">
                      <TextInput
                        id="route-secret"
                        value={spec.tls.secretName}
                        isDisabled={handEdited}
                        placeholder="Leave blank for the router's default certificate"
                        onChange={(_e, value) => setTls({ secretName: value })}
                        data-testid="route-secret"
                      />
                      <FormHelperText>
                        <HelperText>
                          <HelperTextItem>
                            A Secret reference rather than a pasted key: it keeps the private key
                            out of this object, out of this request, and out of every diff this
                            console renders.
                          </HelperTextItem>
                        </HelperText>
                      </FormHelperText>
                    </FormGroup>
                  </GridItem>
                )}

                {spec.tls.termination === 'reencrypt' && (
                  <GridItem span={6}>
                    <FormGroup label="Destination CA certificate" fieldId="route-dest-ca" isRequired>
                      <TextInput
                        id="route-dest-ca"
                        value={spec.tls.destinationCACertificate}
                        isDisabled={handEdited}
                        onChange={(_e, value) => setTls({ destinationCACertificate: value })}
                        data-testid="route-dest-ca"
                      />
                      <FormHelperText>
                        <HelperText>
                          <HelperTextItem>
                            Without it the router has nothing to verify the pod&apos;s certificate
                            against, and some routers then do not verify it at all.
                          </HelperTextItem>
                        </HelperText>
                      </FormHelperText>
                    </FormGroup>
                  </GridItem>
                )}

                {features['wildcard-subdomain'] && (
                  <GridItem span={6}>
                    <FormGroup label="Wildcard policy" fieldId="route-wildcard">
                      <FormSelect
                        id="route-wildcard"
                        value={spec.wildcardPolicy}
                        isDisabled={handEdited}
                        onChange={(_e, value) => setSpec((s) => ({ ...s, wildcardPolicy: value }))}
                        data-testid="route-wildcard"
                      >
                        <FormSelectOption value="" label="This hostname only" />
                        <FormSelectOption value="Subdomain" label="Every subdomain of it" />
                      </FormSelect>
                    </FormGroup>
                  </GridItem>
                )}
              </Grid>
            </div>
          </Tab>

          <Tab eventKey="yaml" title={<TabTitleText>YAML</TabTitleText>} data-testid="route-tab-yaml">
            <div style={{ paddingTop: '1rem' }}>
              <Alert
                isInline
                variant="info"
                className="admin-confirm__alert"
                data-testid="route-yaml-notice"
                title="This document is what gets written"
              >
                The form patches its fields into it rather than regenerating it, so anything you
                write here that the form does not model is kept. Once you edit it, the form is
                disabled and the write sends this document verbatim — the console compiles
                nothing, so it can drop nothing.
              </Alert>
              <YamlEditor
                value={document}
                onChange={(next) => {
                  setDocument(next);
                  setHandEdited(true);
                }}
                onValidityChange={(v) => setYamlValid(v.valid)}
                label={`${entry?.kind ?? 'Object'} manifest`}
                rows={20}
                ariaLabel="Route manifest"
                id="route-yaml"
              />
            </div>
          </Tab>
        </Tabs>

        {/* ── Consequences ─────────────────────────────────────────────── */}

        {renderError && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="route-render-error"
            title="This exposure could not be compiled"
          >
            {renderError.message}
            {renderError.hint ? ` ${renderError.hint}` : ''}
          </Alert>
        )}

        {lossy.length > 0 && (
          <Alert
            isInline
            variant="warning"
            className="admin-confirm__alert"
            data-testid="route-lossy"
            title={`${entry?.kind ?? 'This backend'} cannot express ${
              lossy.length === 1 ? 'one thing' : `${lossy.length} things`
            } you asked for`}
          >
            {lossy.map((item) => (
              <div key={item.feature} style={{ marginBottom: '0.75rem' }}>
                <Checkbox
                  id={`route-ack-${item.feature}`}
                  data-testid={`route-ack-${item.feature}`}
                  isChecked={acknowledged.includes(item.feature)}
                  onChange={(_e, checked) =>
                    setAcknowledged((current) =>
                      checked
                        ? [...current, item.feature]
                        : current.filter((f) => f !== item.feature),
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

        {(rendered?.preserved ?? []).length > 0 && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="route-preserved"
            title="The form is not showing everything in this document"
          >
            These are kept as they are, and the form will not touch them:{' '}
            <code>{rendered.preserved.join(', ')}</code>
          </Alert>
        )}
      </Form>
    </MutationDialog>
  );
}

/**
 * The manifest of the exposure being edited, as YAML text.
 *
 * `js-yaml` is not imported here to re-serialise it: the backend already
 * returns the object, and the render call takes YAML *or* JSON — YAML is a
 * superset, so a JSON document parses. Sending JSON avoids a second
 * serialisation in the browser that could differ from the server's.
 */
function existingManifestYaml(existing) {
  return existing?.manifest ? JSON.stringify(existing.manifest) : null;
}
