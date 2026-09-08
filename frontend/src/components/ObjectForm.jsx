/**
 * ObjectForm — the create dialog's Form view.
 *
 * `objectForm.js` holds the model and the lenses and explains why the document
 * is authoritative; this file is the rendering of it, and it has exactly one
 * rule of its own:
 *
 *   **Every control writes through a lens, and nothing here builds an object.**
 *
 * A control's `onChange` calls `setIn(document, field.path, value)` — or
 * `unsetIn` when the operator empties it — and hands the result up. There is no
 * form model between the inputs and the manifest, so there is nothing that can
 * hold a stale copy of a field, and no serialisation step that could forget
 * one. What the operator sees in YAML view a moment later is the same object
 * these controls have been editing all along, which is what makes the two views
 * safe to switch between without the warning OpenShift's console has to show.
 *
 * **Empty means absent, not zero and not "".** `spec.replicas` left blank is a
 * Deployment that starts with one pod, because the API server's default is one;
 * writing `0` there would create a workload with no pods and no error, and the
 * operator would read the diff they asked for and see nothing wrong with it.
 * Rule 11.2 is usually about rendering a `null` we could not read — this is the
 * same rule one layer up, on the way back out.
 *
 * **Two controls keep local state, and both say why.** A mapping editor (labels,
 * a selector) and a lines editor (a command, dropped capabilities) both have
 * intermediate states the document cannot hold: a row whose key is still being
 * typed is not a key, and a trailing newline is not an argument. They keep the
 * half-typed shape locally and publish only what parses. The form is remounted
 * — by `key` — whenever the document changes from outside it, which is the one
 * case where their local state would be describing something that is no longer
 * there.
 */
import { useState } from 'react';
import {
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
  TextArea,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';
import PlusCircleIcon from '@patternfly/react-icons/dist/esm/icons/plus-circle-icon';
import TrashIcon from '@patternfly/react-icons/dist/esm/icons/trash-icon';

import { SectionHeader } from './ui';
import {
  CONTROLS,
  PULL_POLICIES,
  formatPath,
  getIn,
  integerOrUndefined,
  intOrString,
  isMapping,
  scalarBlocker,
  setIn,
  unsetIn,
} from './objectForm';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/**
 * A control that says why it is inert (rule 11.4) rather than vanishing.
 *
 * The same helper `RouteDialog` carries, for the same reason: a natively
 * disabled input swallows the hover, so the reason has to hang off a span
 * around it or nobody can read it.
 */
function Reasoned({ reason, children }) {
  if (!reason) return children;
  return (
    <Tooltip content={reason}>
      <span style={{ display: 'block' }}>{children}</span>
    </Tooltip>
  );
}

/* ── Mapping editor ─────────────────────────────────────────────────────── */

function toRows(mapping) {
  if (!isMapping(mapping)) return [];
  return Object.entries(mapping).map(([key, value]) => ({ key, value: value == null ? '' : String(value) }));
}

/**
 * Rows back into a mapping, dropping the ones with no key yet.
 *
 * A blank key is a row the operator is still typing, not a label called "". The
 * duplicate-key case is *not* resolved here — the last one would silently win,
 * and which of two labels reaches the cluster is not something to settle by
 * iteration order. `duplicateKeys` reports it and the form says so.
 */
function fromRows(rows) {
  const mapping = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (key) mapping[key] = row.value;
  }
  return mapping;
}

function duplicateKeys(rows) {
  const seen = new Set();
  const duplicates = new Set();
  for (const row of rows) {
    const key = row.key.trim();
    if (!key) continue;
    if (seen.has(key)) duplicates.add(key);
    seen.add(key);
  }
  return [...duplicates];
}

function KeyValueRows({ value, onChange, idPrefix, noun, isDisabled }) {
  // Seeded once. See the file docstring: the form is remounted when the
  // document changes underneath it, so there is no second source to reconcile
  // with — and reconciling on every render is what makes a row disappear
  // halfway through typing its key.
  const [rows, setRows] = useState(() => toRows(value));
  const duplicates = duplicateKeys(rows);

  const publish = (next) => {
    setRows(next);
    onChange(fromRows(next));
  };

  return (
    <>
      {rows.map((row, index) => (
        <Grid
          hasGutter
          // Index-keyed on purpose: a row has no identity until it has a key,
          // and a half-typed one must not remount and lose focus on every
          // character.
          key={index}
          style={{ marginBlockEnd: '0.5rem' }}
          data-testid={`${idPrefix}-row-${index}`}
        >
          <GridItem span={5}>
            <TextInput
              aria-label={`${noun} ${index + 1} key`}
              data-testid={`${idPrefix}-key-${index}`}
              value={row.key}
              isDisabled={isDisabled}
              placeholder="key"
              onChange={(_e, next) => publish(rows.map((item, i) => (i === index ? { ...item, key: next } : item)))}
            />
          </GridItem>
          <GridItem span={5}>
            <TextInput
              aria-label={`${noun} ${index + 1} value`}
              data-testid={`${idPrefix}-value-${index}`}
              value={row.value}
              isDisabled={isDisabled}
              placeholder="value"
              onChange={(_e, next) =>
                publish(rows.map((item, i) => (i === index ? { ...item, value: next } : item)))
              }
            />
          </GridItem>
          <GridItem span={2}>
            <Button
              variant="plain"
              aria-label={`Remove ${noun.toLowerCase()} ${index + 1}`}
              icon={<TrashIcon />}
              isDisabled={isDisabled}
              data-testid={`${idPrefix}-remove-${index}`}
              onClick={() => publish(rows.filter((_item, i) => i !== index))}
            />
          </GridItem>
        </Grid>
      ))}

      {duplicates.length > 0 && (
        <p style={{ ...MUTED, fontSize: '0.875rem' }} data-testid={`${idPrefix}-duplicate`}>
          Two rows carry the key &quot;{duplicates[0]}&quot;. Only one of them can be written, and which is not
          something to leave to the order they were typed in.
        </p>
      )}

      <Button
        variant="link"
        isInline
        icon={<PlusCircleIcon />}
        isDisabled={isDisabled}
        data-testid={`${idPrefix}-add`}
        onClick={() => publish([...rows, { key: '', value: '' }])}
      >
        Add {noun.toLowerCase()}
      </Button>
    </>
  );
}

/* ── Lines editor ───────────────────────────────────────────────────────── */

function StringLines({ value, onChange, id, ariaLabel, placeholder, isDisabled }) {
  const [text, setText] = useState(() => (Array.isArray(value) ? value.join('\n') : ''));
  return (
    <TextArea
      id={id}
      data-testid={id}
      aria-label={ariaLabel}
      value={text}
      rows={2}
      isDisabled={isDisabled}
      placeholder={placeholder}
      resizeOrientation="vertical"
      onChange={(_e, next) => {
        setText(next);
        // The trailing empty line of a textarea somebody is still typing into
        // is not an argument, so it never reaches the document — but it stays
        // in the box, which is why this control holds its own text.
        const entries = next.split('\n').filter((line) => line.trim() !== '');
        onChange(entries.length ? entries : undefined);
      }}
    />
  );
}

/* ── Containers ─────────────────────────────────────────────────────────── */

/** One row of `ports`. `containerPort` is the only required field on it. */
function PortRows({ ports, base, document: doc, onDocument, idPrefix, isDisabled }) {
  const rows = Array.isArray(ports) ? ports : [];
  const write = (next) => onDocument(next.length ? setIn(doc, base, next) : unsetIn(doc, base));
  return (
    <>
      {rows.map((port, index) => (
        <Grid hasGutter key={index} style={{ marginBlockEnd: '0.5rem' }} data-testid={`${idPrefix}-row-${index}`}>
          <GridItem span={4}>
            <TextInput
              aria-label={`Port ${index + 1} number`}
              data-testid={`${idPrefix}-number-${index}`}
              value={isMapping(port) && port.containerPort != null ? String(port.containerPort) : ''}
              isDisabled={isDisabled}
              placeholder="containerPort"
              onChange={(_e, next) => {
                const parsed = integerOrUndefined(next);
                const updated = rows.map((item, i) =>
                  i === index
                    ? parsed === undefined
                      ? unsetIn(item, ['containerPort'])
                      : setIn(item, ['containerPort'], parsed)
                    : item,
                );
                write(updated);
              }}
            />
          </GridItem>
          <GridItem span={4}>
            <TextInput
              aria-label={`Port ${index + 1} name`}
              data-testid={`${idPrefix}-name-${index}`}
              value={isMapping(port) && port.name != null ? String(port.name) : ''}
              isDisabled={isDisabled}
              placeholder="name (optional)"
              onChange={(_e, next) =>
                write(
                  rows.map((item, i) =>
                    i === index ? (next ? setIn(item, ['name'], next) : unsetIn(item, ['name'])) : item,
                  ),
                )
              }
            />
          </GridItem>
          <GridItem span={3}>
            <FormSelect
              aria-label={`Port ${index + 1} protocol`}
              data-testid={`${idPrefix}-protocol-${index}`}
              value={isMapping(port) && port.protocol ? String(port.protocol) : ''}
              isDisabled={isDisabled}
              onChange={(_e, next) =>
                write(
                  rows.map((item, i) =>
                    i === index ? (next ? setIn(item, ['protocol'], next) : unsetIn(item, ['protocol'])) : item,
                  ),
                )
              }
            >
              <FormSelectOption value="" label="TCP (default)" />
              <FormSelectOption value="TCP" label="TCP" />
              <FormSelectOption value="UDP" label="UDP" />
              <FormSelectOption value="SCTP" label="SCTP" />
            </FormSelect>
          </GridItem>
          <GridItem span={1}>
            <Button
              variant="plain"
              aria-label={`Remove port ${index + 1}`}
              icon={<TrashIcon />}
              isDisabled={isDisabled}
              data-testid={`${idPrefix}-remove-${index}`}
              onClick={() => write(rows.filter((_item, i) => i !== index))}
            />
          </GridItem>
        </Grid>
      ))}
      <Button
        variant="link"
        isInline
        icon={<PlusCircleIcon />}
        isDisabled={isDisabled}
        data-testid={`${idPrefix}-add`}
        onClick={() => write([...rows, {}])}
      >
        Add a port
      </Button>
    </>
  );
}

/** One row of `env`. A variable with a `valueFrom` is shown and not editable. */
function EnvRows({ env, base, document: doc, onDocument, idPrefix, isDisabled }) {
  const rows = Array.isArray(env) ? env : [];
  const write = (next) => onDocument(next.length ? setIn(doc, base, next) : unsetIn(doc, base));
  return (
    <>
      {rows.map((entry, index) => {
        const fromElsewhere = isMapping(entry) && entry.valueFrom != null;
        const reason = fromElsewhere
          ? 'This variable takes its value from somewhere else in the cluster — a Secret, a ConfigMap, or the pod’s own fields. The form would have to flatten that to a literal, so it is left alone; edit it in YAML view.'
          : null;
        return (
          <Grid hasGutter key={index} style={{ marginBlockEnd: '0.5rem' }} data-testid={`${idPrefix}-row-${index}`}>
            <GridItem span={5}>
              <TextInput
                aria-label={`Variable ${index + 1} name`}
                data-testid={`${idPrefix}-name-${index}`}
                value={isMapping(entry) && entry.name != null ? String(entry.name) : ''}
                isDisabled={isDisabled}
                placeholder="NAME"
                onChange={(_e, next) =>
                  write(
                    rows.map((item, i) =>
                      i === index ? (next ? setIn(item, ['name'], next) : unsetIn(item, ['name'])) : item,
                    ),
                  )
                }
              />
            </GridItem>
            <GridItem span={5}>
              <Reasoned reason={reason}>
                <TextInput
                  aria-label={`Variable ${index + 1} value`}
                  data-testid={`${idPrefix}-value-${index}`}
                  value={
                    fromElsewhere
                      ? `(from ${Object.keys(entry.valueFrom ?? {})[0] ?? 'elsewhere'})`
                      : isMapping(entry) && entry.value != null
                        ? String(entry.value)
                        : ''
                  }
                  isDisabled={isDisabled || fromElsewhere}
                  placeholder="value"
                  onChange={(_e, next) =>
                    write(
                      rows.map((item, i) =>
                        i === index ? (next ? setIn(item, ['value'], next) : unsetIn(item, ['value'])) : item,
                      ),
                    )
                  }
                />
              </Reasoned>
            </GridItem>
            <GridItem span={2}>
              <Button
                variant="plain"
                aria-label={`Remove variable ${index + 1}`}
                icon={<TrashIcon />}
                isDisabled={isDisabled}
                data-testid={`${idPrefix}-remove-${index}`}
                onClick={() => write(rows.filter((_item, i) => i !== index))}
              />
            </GridItem>
          </Grid>
        );
      })}
      <Button
        variant="link"
        isInline
        icon={<PlusCircleIcon />}
        isDisabled={isDisabled}
        data-testid={`${idPrefix}-add`}
        onClick={() => write([...rows, {}])}
      >
        Add an environment variable
      </Button>
    </>
  );
}

/** A quantity: CPU or memory, request or limit. Blank removes the key. */
function QuantityInput({ document: doc, onDocument, path, label, placeholder, isDisabled }) {
  const value = getIn(doc, path);
  const id = `create-${path.slice(-3).join('-')}`;
  return (
    <TextInput
      id={id}
      data-testid={id}
      aria-label={label}
      value={value == null ? '' : String(value)}
      isDisabled={isDisabled}
      placeholder={placeholder}
      onChange={(_e, next) => onDocument(next.trim() ? setIn(doc, path, next.trim()) : unsetIn(doc, path))}
    />
  );
}

function ContainersEditor({ document: doc, onDocument, path, isDisabled }) {
  const containers = getIn(doc, path);
  const rows = Array.isArray(containers) ? containers : [];
  const write = (next) => onDocument(next.length ? setIn(doc, path, next) : unsetIn(doc, path));

  return (
    <>
      {rows.map((container, index) => {
        const at = (...rest) => [...path, index, ...rest];
        const field = (key) => (isMapping(container) && container[key] != null ? String(container[key]) : '');
        const setField = (key, next) =>
          onDocument(next ? setIn(doc, at(key), next) : unsetIn(doc, at(key)));
        return (
          <div key={index} className="admin-create-container" data-testid={`create-container-${index}`}>
            <Grid hasGutter style={{ marginBlockEnd: '0.5rem' }}>
              <GridItem span={5}>
                <FormGroup label="Container name" fieldId={`create-container-name-${index}`} isRequired>
                  <TextInput
                    id={`create-container-name-${index}`}
                    data-testid={`create-container-name-${index}`}
                    value={field('name')}
                    isDisabled={isDisabled}
                    onChange={(_e, next) => setField('name', next)}
                  />
                </FormGroup>
              </GridItem>
              <GridItem span={5}>
                <FormGroup label="Image" fieldId={`create-container-image-${index}`} isRequired>
                  <TextInput
                    id={`create-container-image-${index}`}
                    data-testid={`create-container-image-${index}`}
                    value={field('image')}
                    isDisabled={isDisabled}
                    onChange={(_e, next) => setField('image', next)}
                  />
                </FormGroup>
              </GridItem>
              <GridItem span={2}>
                <FormGroup label="&nbsp;" fieldId={`create-container-remove-${index}`}>
                  <Reasoned reason={rows.length === 1 ? 'A pod needs at least one container.' : null}>
                    <Button
                      variant="link"
                      isDanger
                      isDisabled={isDisabled || rows.length === 1}
                      data-testid={`create-container-remove-${index}`}
                      onClick={() => write(rows.filter((_item, i) => i !== index))}
                    >
                      Remove
                    </Button>
                  </Reasoned>
                </FormGroup>
              </GridItem>
            </Grid>

            <FormGroup label="Image pull policy" fieldId={`create-container-pull-${index}`}>
              <FormSelect
                id={`create-container-pull-${index}`}
                data-testid={`create-container-pull-${index}`}
                value={field('imagePullPolicy')}
                isDisabled={isDisabled}
                onChange={(_e, next) => setField('imagePullPolicy', next)}
              >
                {PULL_POLICIES.map((option) => (
                  <FormSelectOption key={option.value} value={option.value} label={option.label} />
                ))}
              </FormSelect>
            </FormGroup>

            <FormGroup label="Command" fieldId={`create-container-command-${index}`}>
              <StringLines
                id={`create-container-command-${index}`}
                ariaLabel={`Container ${index + 1} command`}
                placeholder="One argument per line. Empty runs the image's own entrypoint."
                value={isMapping(container) ? container.command : undefined}
                isDisabled={isDisabled}
                onChange={(next) =>
                  onDocument(next ? setIn(doc, at('command'), next) : unsetIn(doc, at('command')))
                }
              />
            </FormGroup>

            <FormGroup label="Arguments" fieldId={`create-container-args-${index}`}>
              <StringLines
                id={`create-container-args-${index}`}
                ariaLabel={`Container ${index + 1} arguments`}
                placeholder="One argument per line."
                value={isMapping(container) ? container.args : undefined}
                isDisabled={isDisabled}
                onChange={(next) => onDocument(next ? setIn(doc, at('args'), next) : unsetIn(doc, at('args')))}
              />
            </FormGroup>

            <FormGroup label="Ports" fieldId={`create-container-ports-${index}`}>
              <PortRows
                ports={isMapping(container) ? container.ports : undefined}
                base={at('ports')}
                document={doc}
                onDocument={onDocument}
                idPrefix={`create-container-${index}-port`}
                isDisabled={isDisabled}
              />
            </FormGroup>

            <FormGroup label="Environment variables" fieldId={`create-container-env-${index}`}>
              <EnvRows
                env={isMapping(container) ? container.env : undefined}
                base={at('env')}
                document={doc}
                onDocument={onDocument}
                idPrefix={`create-container-${index}-env`}
                isDisabled={isDisabled}
              />
            </FormGroup>

            <FormGroup label="Resources" fieldId={`create-container-resources-${index}`}>
              <Grid hasGutter>
                <GridItem span={3}>
                  <QuantityInput
                    document={doc}
                    onDocument={onDocument}
                    path={at('resources', 'requests', 'cpu')}
                    label={`Container ${index + 1} CPU request`}
                    placeholder="CPU request"
                    isDisabled={isDisabled}
                  />
                </GridItem>
                <GridItem span={3}>
                  <QuantityInput
                    document={doc}
                    onDocument={onDocument}
                    path={at('resources', 'requests', 'memory')}
                    label={`Container ${index + 1} memory request`}
                    placeholder="Memory request"
                    isDisabled={isDisabled}
                  />
                </GridItem>
                <GridItem span={3}>
                  <QuantityInput
                    document={doc}
                    onDocument={onDocument}
                    path={at('resources', 'limits', 'cpu')}
                    label={`Container ${index + 1} CPU limit`}
                    placeholder="CPU limit"
                    isDisabled={isDisabled}
                  />
                </GridItem>
                <GridItem span={3}>
                  <QuantityInput
                    document={doc}
                    onDocument={onDocument}
                    path={at('resources', 'limits', 'memory')}
                    label={`Container ${index + 1} memory limit`}
                    placeholder="Memory limit"
                    isDisabled={isDisabled}
                  />
                </GridItem>
              </Grid>
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    Requests are what the scheduler reserves; limits are what the kubelet enforces. A pod with
                    no requests is scheduled onto a node that has no room for it.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>

            <Grid hasGutter>
              <GridItem span={6}>
                <FormGroup label="Allow privilege escalation" fieldId={`create-container-ape-${index}`}>
                  <FormSelect
                    id={`create-container-ape-${index}`}
                    data-testid={`create-container-ape-${index}`}
                    value={triValue(getIn(doc, at('securityContext', 'allowPrivilegeEscalation')))}
                    isDisabled={isDisabled}
                    onChange={(_e, next) =>
                      onDocument(
                        next === ''
                          ? unsetIn(doc, at('securityContext', 'allowPrivilegeEscalation'))
                          : setIn(doc, at('securityContext', 'allowPrivilegeEscalation'), next === 'true'),
                      )
                    }
                  >
                    <FormSelectOption value="" label="Not set" />
                    <FormSelectOption value="false" label="false — what the restricted profile requires" />
                    <FormSelectOption value="true" label="true" />
                  </FormSelect>
                </FormGroup>
              </GridItem>
              <GridItem span={6}>
                <FormGroup label="Dropped capabilities" fieldId={`create-container-drop-${index}`}>
                  <StringLines
                    id={`create-container-drop-${index}`}
                    ariaLabel={`Container ${index + 1} dropped capabilities`}
                    placeholder="ALL"
                    value={getIn(doc, at('securityContext', 'capabilities', 'drop'))}
                    isDisabled={isDisabled}
                    onChange={(next) =>
                      onDocument(
                        next
                          ? setIn(doc, at('securityContext', 'capabilities', 'drop'), next)
                          : unsetIn(doc, at('securityContext', 'capabilities', 'drop')),
                      )
                    }
                  />
                </FormGroup>
              </GridItem>
            </Grid>
          </div>
        );
      })}

      <Button
        variant="secondary"
        isDisabled={isDisabled}
        data-testid="create-container-add"
        // Seeded with the restricted profile's two container-level
        // requirements, because admission judges the pod and not the container:
        // a second container added bare gets the whole object rejected,
        // including the first one that was fine. The starter templates make the
        // same choice for the same reason, and both values are visible here and
        // in YAML view rather than being applied on the way out.
        onClick={() =>
          write([
            ...rows,
            {
              name: '',
              image: '',
              securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: ['ALL'] } },
            },
          ])
        }
      >
        Add a container
      </Button>
    </>
  );
}

/** A three-state boolean as the string a `FormSelect` can hold. */
function triValue(value) {
  if (value === true) return 'true';
  if (value === false) return 'false';
  return '';
}

/* ── One field ──────────────────────────────────────────────────────────── */

/**
 * A whole number, or nothing.
 *
 * A plain numeric `TextInput`, and deliberately **not** PatternFly's
 * `NumberInput`, which every other numeric field in this app uses.
 * `NumberInput` normalises on blur — `event.target.value =
 * Number(event.target.value).toString()` — so an emptied box fires a change
 * carrying `"0"` the moment focus leaves it. Everywhere else in this console
 * that is harmless, because the field is a required count with no absent state.
 * Here it would turn "I do not want to set replicas" into `replicas: 0` — a
 * Deployment with no pods, reported by nothing, in a diff that looks exactly
 * like the one the operator asked for. Rule 11.2 says a number we could not
 * derive is null and never 0; this is the same rule on the way back out, and it
 * costs the stepper buttons.
 *
 * The other half is everything the box can hold that is not a whole number, and
 * it arrives in two shapes. Letters read back as an empty `value` with
 * `validity.badInput` set, while the box goes on showing them; `3.5` reads back
 * as itself and is simply not an integer. Both would otherwise leave the field
 * absent from the manifest while the operator looked at the figure they typed —
 * the screen and the document saying different things, which is the whole
 * failure this view exists to prevent. Neither is silently rounded: `3.5`
 * replicas is a question only the person who typed it can answer.
 */
function NumberField({ id, field, value, isDisabled, onPut }) {
  const [badInput, setBadInput] = useState(false);
  return (
    <>
      <TextInput
        id={id}
        data-testid={id}
        type="number"
        min={field.min}
        value={typeof value === 'number' ? String(value) : ''}
        isDisabled={isDisabled}
        placeholder={field.placeholder}
        aria-label={field.label}
        validated={badInput ? 'error' : 'default'}
        onChange={(event, next) => {
          const parsed = integerOrUndefined(next);
          setBadInput(
            Boolean(event?.currentTarget?.validity?.badInput) || (next.trim() !== '' && parsed === undefined),
          );
          onPut(parsed);
        }}
      />
      {badInput && (
        <FormHelperText>
          <HelperText>
            <HelperTextItem variant="error" data-testid={`${id}-bad`}>
              That is not a whole number, so {formatPath(field.path)} is not set at all. Clear the box or type
              an integer.
            </HelperTextItem>
          </HelperText>
        </FormHelperText>
      )}
    </>
  );
}

function Help({ text }) {
  if (!text) return null;
  return (
    <FormHelperText>
      <HelperText>
        <HelperTextItem>{text}</HelperTextItem>
      </HelperText>
    </FormHelperText>
  );
}

function Field({ field, model, document: doc, onDocument, onReplace, isDisabled }) {
  const id = `create-${field.id}`;
  const value = getIn(doc, field.path);

  // A path that runs through a scalar cannot be written without destroying it.
  // Rule 11.4: the control stays, disabled, saying which path and where to fix
  // it — rather than a form that appears to work and silently drops what was
  // written there.
  const blocker = scalarBlocker(doc, field.path);
  const blockedReason = blocker
    ? `"${formatPath(blocker)}" in this document holds a single value rather than a block, so this control cannot write inside it. Edit it in YAML view.`
    : null;
  const inert = isDisabled || Boolean(blockedReason);

  const put = (next) => onDocument(next === undefined ? unsetIn(doc, field.path) : setIn(doc, field.path, next));

  let control = null;
  switch (field.control) {
    case 'text':
      control = (
        <TextInput
          id={id}
          data-testid={id}
          value={value == null ? '' : String(value)}
          isDisabled={inert}
          placeholder={field.placeholder}
          onChange={(_e, next) => put(next === '' ? undefined : next)}
        />
      );
      break;

    case 'number':
      control = <NumberField id={id} field={field} value={value} isDisabled={inert} onPut={put} />;
      break;

    case 'intOrString':
      control = (
        <TextInput
          id={id}
          data-testid={id}
          value={value == null ? '' : String(value)}
          isDisabled={inert}
          placeholder={field.placeholder}
          onChange={(_e, next) => put(intOrString(next))}
        />
      );
      break;

    case 'select': {
      // A document can hold a value this form does not offer — an enum from a
      // newer API, or an absent field on a kind where absent is not a legal
      // choice (a Job's `restartPolicy` is the one that matters: the API server
      // fills in "Always" and then refuses the object for it). A select whose
      // value matches no option renders as its *first* option, which would show
      // the operator a setting the document does not contain. So the actual
      // state is listed, said to be the actual state, and left selectable.
      const current = value == null ? '' : String(value);
      const known = field.options.some((option) => option.value === current);
      const options = known
        ? field.options
        : [
            {
              value: current,
              label: current === '' ? 'Not set — this kind has no default you would want' : `${current} — in the document; not a value this form offers`,
            },
            ...field.options,
          ];
      control = (
        <FormSelect
          id={id}
          data-testid={id}
          value={current}
          isDisabled={inert}
          onChange={(_e, next) => put(next === '' ? undefined : next)}
        >
          {options.map((option) => (
            <FormSelectOption key={option.value} value={option.value} label={option.label} />
          ))}
        </FormSelect>
      );
      break;
    }

    case 'triBool':
      control = (
        <FormSelect
          id={id}
          data-testid={id}
          value={triValue(value)}
          isDisabled={inert}
          onChange={(_e, next) => put(next === '' ? undefined : next === 'true')}
        >
          <FormSelectOption value="" label="Not set" />
          <FormSelectOption value="true" label="true" />
          <FormSelectOption value="false" label="false" />
        </FormSelect>
      );
      break;

    case 'checkbox':
      control = (
        <Checkbox
          id={id}
          data-testid={id}
          label={field.label}
          isChecked={value === true}
          isDisabled={inert}
          onChange={(_e, checked) => put(checked ? true : undefined)}
        />
      );
      break;

    case 'keyValue':
      control = (
        <KeyValueRows
          value={value}
          idPrefix={id}
          noun={field.label.replace(/s$/, '')}
          isDisabled={inert}
          onChange={(next) => put(Object.keys(next).length ? next : undefined)}
        />
      );
      break;

    case 'stringLines':
      control = (
        <StringLines
          id={id}
          ariaLabel={field.label}
          placeholder={field.placeholder}
          value={value}
          isDisabled={inert}
          onChange={(next) => put(next)}
        />
      );
      break;

    case 'checkboxSet': {
      const listed = Array.isArray(value) ? value.map(String) : [];
      control = (
        <>
          {field.options.map((option) => (
            <Checkbox
              key={option.value}
              id={`${id}-${option.value}`}
              data-testid={`${id}-${option.value}`}
              label={option.label}
              isChecked={listed.includes(option.value)}
              isDisabled={inert}
              onChange={(_e, checked) => {
                // Rebuilt from the model's own option order rather than by
                // appending, so the list a reviewer reads in the diff does not
                // depend on which box was ticked first.
                const next = field.options
                  .map((entry) => entry.value)
                  .filter((entry) => (entry === option.value ? checked : listed.includes(entry)));
                // Anything the document listed that this form does not offer
                // stays: an unknown policy type is somebody else's field.
                const unknown = listed.filter((entry) => !field.options.some((o) => o.value === entry));
                const merged = [...next, ...unknown];
                put(merged.length ? merged : undefined);
              }}
            />
          ))}
        </>
      );
      break;
    }

    case 'containers':
      control = (
        <ContainersEditor document={doc} onDocument={onDocument} path={field.path} isDisabled={inert} />
      );
      break;

    default:
      // Every entry of `CONTROLS` has a case above, so this is unreachable
      // until a model grows a control this file does not render. Saying so on
      // screen rather than rendering nothing is the point: `unrepresented()`
      // cannot catch it — the model claims the path *is* covered — so a silent
      // blank would be the one field the form hides while telling the operator
      // it hides nothing. The two spellings of the failure are worth keeping
      // apart for whoever reads it: a name outside the vocabulary is a typo in
      // a model, a name inside it is a case missing here.
      control = (
        <p style={MUTED} data-testid={`${id}-unrenderable`}>
          {CONTROLS.includes(field.control)
            ? `This console knows the "${field.control}" control but does not render it here.`
            : `"${field.control}" is not a control this console has.`}{' '}
          Edit {formatPath(field.path)} in YAML view.
        </p>
      );
  }

  // The selector is the one field where a mismatch is silent and expensive: the
  // API server refuses the object outright, and until then everything on screen
  // looks finished. The repair is offered as a button rather than done
  // automatically, because a selector is immutable after creation and an edit
  // nobody saw is an edit nobody reviewed.
  const selectorMismatch =
    field.id === 'selector' &&
    model.podLabelsPath &&
    isMapping(value) &&
    Object.entries(value).some(([key, entry]) => String(getIn(doc, [...model.podLabelsPath, key])) !== String(entry));

  return (
    <FormGroup
      label={field.control === 'checkbox' ? undefined : field.label}
      fieldId={id}
      isRequired={Boolean(field.required)}
      style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}
    >
      <Reasoned reason={blockedReason}>{control}</Reasoned>
      {selectorMismatch && (
        <div style={{ marginBlockStart: '0.5rem' }}>
          <Button
            variant="secondary"
            data-testid="create-selector-repair"
            isDisabled={isDisabled}
            onClick={() => onReplace(setIn(doc, field.path, { ...getIn(doc, model.podLabelsPath) }))}
          >
            Copy the pod labels into the selector
          </Button>
        </div>
      )}
      <Help text={blockedReason || field.help} />
    </FormGroup>
  );
}

/* ── The form ───────────────────────────────────────────────────────────── */

/** How many unrepresented paths are listed before the count stands in. */
const PATH_LIST_LIMIT = 40;

/**
 *   <ObjectForm
 *     document={parsed}
 *     model={formModelFor(apiVersion, kind)}
 *     onChange={(next) => setText(toYaml(next))}
 *     onReplace={(next) => { setText(toYaml(next)); remountTheForm(); }}
 *   />
 *
 * `onReplace` is `onChange` plus "the controls holding local state are now
 * describing a document that no longer exists". Only the selector repair uses
 * it today; anything else that rewrites a field from outside its own control
 * needs it too.
 */
export function ObjectForm({ document: doc, model, onChange, onReplace, unrepresentedPaths = [], isDisabled = false }) {
  if (!model) return null;
  return (
    <Form onSubmit={(event) => event.preventDefault()} data-testid="create-form">
      {model.sections.map((section) => (
        <section key={section.id} data-testid={`create-section-${section.id}`}>
          <SectionHeader title={section.title} description={section.description} headingLevel="h3" />
          {section.fields.map((field) => (
            <Field
              key={field.id}
              field={field}
              model={model}
              document={doc}
              onDocument={onChange}
              onReplace={onReplace}
              isDisabled={isDisabled}
            />
          ))}
        </section>
      ))}

      <section data-testid="create-section-unrepresented">
        <SectionHeader
          title="Not shown in this form"
          description="Everything below is in the document and is kept exactly as written — creating this object sends all of it. Switch to YAML view to change any of it."
          headingLevel="h3"
        />
        {unrepresentedPaths.length === 0 ? (
          <p style={MUTED} data-testid="create-unrepresented-none">
            Nothing. Every field in this document has a control above it.
          </p>
        ) : (
          <>
            <ul className="admin-create-paths" data-testid="create-unrepresented" data-count={unrepresentedPaths.length}>
              {unrepresentedPaths.slice(0, PATH_LIST_LIMIT).map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
            {unrepresentedPaths.length > PATH_LIST_LIMIT && (
              // Said rather than silently truncated: a list that stopped at 40
              // with no note reads as "that was all of them".
              <p style={{ ...MUTED, fontSize: '0.875rem' }} data-testid="create-unrepresented-more">
                and {unrepresentedPaths.length - PATH_LIST_LIMIT} more, all of them preserved. YAML view shows
                the whole document.
              </p>
            )}
          </>
        )}
      </section>
    </Form>
  );
}

export default ObjectForm;
