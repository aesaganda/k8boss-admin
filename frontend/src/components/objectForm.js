/**
 * objectForm — the field model behind the create dialog's Form view, and the
 * lenses that let it edit a manifest without owning it.
 *
 * The OpenShift console offers "Configure via: Form view / YAML view" on every
 * create screen, with a note saying *some fields may not be represented in this
 * form view*. That note is the honest part of the idea and the vague part of
 * it: it warns that the form is incomplete without ever saying **which** fields
 * it is not showing you, and it does not say what happens to them. This module
 * is the same idea with both halves answered:
 *
 *   **The document is the source of truth. The form is a projection of it.**
 *
 * Every control here is a *lens* — a path into the parsed object, a getter and
 * a setter. A form edit is `setIn(document, path, value)`: it rewrites one
 * place and copies everything else through. A `spec.affinity` somebody pasted,
 * a controller annotation, a `volumeClaimTemplates` block this console has
 * never heard of — none of it is touched by editing the container image, and
 * none of it has to be enumerated here for that to be true. `unrepresented()`
 * then walks the document, subtracts what the model covers, and returns the
 * paths that are left, so the dialog can *name* them rather than gesturing at
 * them. A form that silently dropped a field the operator wrote — at the moment
 * they touched an unrelated control — would be the defect standard in its most
 * expensive form: a confident write against somebody's cluster, with the
 * missing part invisible in the very diff that was supposed to show it.
 *
 * This is the same relationship `RouteDialog` has between its form and its
 * document (§13), reached the same way and for the same reason. The difference
 * is where the patching happens: an exposure is compiled server-side because
 * one form field can mean three different objects, and a create is one object
 * whose fields are already in front of us, so the patch is local and the round
 * trip stays where it belongs — the dry run, which is the only thing here that
 * gets to say whether a cluster will accept any of it.
 *
 * ## What the form does not, and must not, do
 *
 * **It does not validate for the API server.** `localIssues()` reports what can
 * be checked against the document alone — a selector that cannot match its own
 * pod template, a Job with `restartPolicy: Always`, a CronJob with no schedule.
 * Every one of them is labelled a local check, and **none of them blocks the
 * preview**: the dry run is the authority on whether a manifest is acceptable,
 * and a console that refused to *ask* would be substituting its own opinion for
 * the API server's on somebody else's cluster. The checks exist to save a round
 * trip, not to replace it.
 *
 * **It does not couple fields behind the operator's back.** Renaming a
 * Deployment does not silently rewrite its selector and its pod labels, the way
 * a template engine would. The mismatch is reported, and the repair is a button
 * the operator presses — because a selector is immutable after creation, and an
 * edit nobody saw is the one nobody reviews.
 *
 * **It edits the object, not the text.** A form edit re-serialises the parsed
 * document, and three things in a manifest live in the text rather than in the
 * object it parses to: comments, which nothing carries across a parse, and
 * anchors and merge keys, which a parse resolves — so a rewrite spells out what
 * they stood for. The first is a loss and the other two are an expansion, they
 * are reported as the different things they are (`rewriteLosses`), and both are
 * said before the first form edit rather than discovered afterwards in the
 * diff. The template comments worth keeping are restated as help text on the
 * fields they were attached to, which is where they were useful anyway. A
 * document whose anchor refers to the block containing it cannot be written
 * back at all, and `containsCycle` is how the form declines it instead of
 * hanging.
 *
 * No JSX lives here, deliberately: Vite only transforms JSX in `.jsx`, and this
 * file is also the one place a test can reach the model without mounting
 * anything. `ObjectForm.jsx` renders it.
 */
import yaml from 'js-yaml';

import { tokenizeYaml } from './yamlSyntax';

/* ── Path lenses ────────────────────────────────────────────────────────── */

/**
 * A plain mapping, as opposed to an array, a `null`, or a `Date` — js-yaml
 * resolves a bare `2024-01-01` to one of those, and treating it as a container
 * would build a form field inside a timestamp.
 */
export function isMapping(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value) && !(value instanceof Date);
}

/** The value at `path`, or `undefined` if any step of it is missing. */
export function getIn(object, path) {
  let cursor = object;
  for (const segment of path) {
    if (cursor == null || typeof cursor !== 'object') return undefined;
    cursor = cursor[segment];
  }
  return cursor;
}

/**
 * `object` with `path` set to `value`, as a new object.
 *
 * Structural sharing rather than a deep clone: only the nodes along `path` are
 * copied, so nothing else in the document can be altered by this call even in
 * principle — and nothing else has to survive a clone, which matters because a
 * pasted manifest can hold values `structuredClone` refuses (a `Date` from an
 * unquoted timestamp) or would silently normalise.
 *
 * A numeric segment builds an array, a string segment builds a mapping. A
 * segment landing on a scalar is a caller error: `scalarBlocker` exists so the
 * caller can disable that control with the reason instead of overwriting
 * whatever the operator wrote there.
 */
export function setIn(object, path, value) {
  if (path.length === 0) return value;
  const [head, ...rest] = path;
  if (typeof head === 'number') {
    const base = Array.isArray(object) ? object.slice() : [];
    base[head] = setIn(base[head], rest, value);
    return base;
  }
  const base = isMapping(object) ? { ...object } : {};
  base[head] = setIn(base[head], rest, value);
  return base;
}

/**
 * `object` with `path` removed, and any mapping or array that `path` left empty
 * removed with it.
 *
 * The pruning is what keeps the document readable across a session of form
 * edits: clearing `maxSurge` and then `maxUnavailable` should leave
 * `strategy: {type: RollingUpdate}`, not `strategy: {type: RollingUpdate,
 * rollingUpdate: {}}` — an empty block that reads as a setting somebody made.
 * It stops at the document root: an object with no `spec` is a manifest the API
 * server will reject and say so, which is more useful than this module quietly
 * deciding a top-level key was surplus.
 */
export function unsetIn(object, path) {
  if (path.length === 0) return undefined;
  const [head, ...rest] = path;
  if (typeof head === 'number') {
    if (!Array.isArray(object)) return object;
    const base = object.slice();
    if (rest.length === 0) base.splice(head, 1);
    else base[head] = unsetIn(base[head], rest);
    return base;
  }
  if (!isMapping(object)) return object;
  if (!(head in object)) return object;
  const base = { ...object };
  if (rest.length === 0) {
    delete base[head];
    return base;
  }
  const pruned = unsetIn(base[head], rest);
  // Nothing below was there to remove, so nothing above it is surplus either.
  // Without this, unsetting an absent path prunes the block that would have
  // contained it: `unsetIn(policy, ['spec','podSelector','matchLabels'])` on a
  // document whose `podSelector` is `{}` deletes the podSelector — a field the
  // default-deny template writes on purpose — for a key that was never there.
  if (pruned === base[head]) return object;
  const isEmptyMapping = isMapping(pruned) && Object.keys(pruned).length === 0;
  const isEmptyArray = Array.isArray(pruned) && pruned.length === 0;
  if (pruned === undefined || isEmptyMapping || isEmptyArray) delete base[head];
  else base[head] = pruned;
  return base;
}

/**
 * The prefix of `path` that is occupied by a scalar, or `null` if the write is
 * safe.
 *
 * `setIn` would replace that scalar with a mapping and the operator's value
 * would be gone from the manifest without appearing in any diff — they would be
 * comparing a projection of a document that no longer says what they typed. A
 * field whose blocker is non-null renders disabled, naming the path, with YAML
 * view as the way through. It is a rare shape (`strategy: Recreate` written as
 * a bare string, say) and an expensive one to guess at.
 */
export function scalarBlocker(object, path) {
  let cursor = object;
  for (let i = 0; i < path.length - 1; i += 1) {
    if (cursor == null) return null;
    if (typeof cursor !== 'object' || cursor instanceof Date) return path.slice(0, i);
    cursor = cursor[path[i]];
  }
  if (cursor != null && typeof cursor !== 'object') return path.slice(0, path.length - 1);
  return null;
}

/** `spec.template.spec.containers[0].image` — a path as an operator reads it. */
export function formatPath(path) {
  return path.reduce(
    (text, segment) =>
      typeof segment === 'number' ? `${text}[${segment}]` : text ? `${text}.${segment}` : String(segment),
    '',
  );
}

/**
 * Every leaf path in `object`, depth first.
 *
 * A leaf is a scalar, an empty mapping or an empty array — `podSelector: {}` is
 * a decision somebody made (it selects every pod in the namespace) and has to
 * be reportable as one, so it counts as a leaf rather than vanishing for having
 * nothing inside it.
 */
export function leafPaths(object, prefix = [], ancestors = new Set()) {
  // A YAML anchor can refer to the node that contains it, and js-yaml resolves
  // that into a genuinely cyclic object rather than refusing it. Without this
  // set the walk never returns, and because `unrepresented()` runs inside a
  // render the operator loses the dialog and everything typed into it to an
  // error boundary that can name neither the document nor the anchor.
  if (ancestors.has(object)) return [prefix];
  const nested = new Set(ancestors);
  if (object && typeof object === 'object') nested.add(object);
  if (Array.isArray(object)) {
    if (object.length === 0) return [prefix];
    return object.flatMap((item, index) => leafPaths(item, [...prefix, index], nested));
  }
  if (isMapping(object)) {
    const keys = Object.keys(object);
    if (keys.length === 0) return [prefix];
    return keys.flatMap((key) => leafPaths(object[key], [...prefix, key], nested));
  }
  return [prefix];
}

/**
 * Does this document contain a node that contains itself?
 *
 * `toYaml` cannot serialise one — `noRefs` expands shared nodes rather than
 * re-emitting the anchor, so a cycle expands forever — which means the form
 * cannot write such a document back and must not offer to. The YAML view is
 * unaffected: the text is the text, and the dry run will have its own opinion.
 */
export function containsCycle(value, ancestors = new Set()) {
  if (!value || typeof value !== 'object') return false;
  if (ancestors.has(value)) return true;
  const nested = new Set(ancestors);
  nested.add(value);
  const children = Array.isArray(value) ? value : isMapping(value) ? Object.values(value) : [];
  return children.some((child) => containsCycle(child, nested));
}

/**
 * Does `pattern` (which may use `'*'` for "any array index") cover `path`?
 *
 * `subtree` is the difference between a control that owns one value and one
 * that owns a whole block: the labels editor at `metadata.labels` represents
 * every key under it, whereas the replicas field at `spec.replicas` represents
 * exactly that number.
 */
function patternCovers(pattern, path, subtree) {
  if (subtree ? path.length < pattern.length : path.length !== pattern.length) return false;
  return pattern.every((segment, i) => (segment === '*' ? typeof path[i] === 'number' : segment === path[i]));
}

/**
 * What each control needs to find at its path, and what to call what it found.
 *
 * A document can hold any shape anywhere — `labels: production` where a block of
 * keys belongs, `command: run` where a list does. Two things read this table and
 * they have to agree: the renderer, which disables a control that cannot show
 * what is there, and `unrepresented()`, which must then stop counting that
 * field as represented. A control that is inert and a field that is called
 * covered is the one combination where the form hides something while saying it
 * hides nothing.
 */
export const CONTROL_SHAPES = {
  keyValue: { ok: (value) => isMapping(value), wants: 'a set of key/value pairs' },
  stringLines: { ok: (value) => Array.isArray(value), wants: 'a list' },
  checkboxSet: { ok: (value) => Array.isArray(value), wants: 'a list' },
  containers: { ok: (value) => Array.isArray(value), wants: 'a list' },
  objectList: { ok: (value) => Array.isArray(value), wants: 'a list' },
};

/** The shape a control expects; scalars are the default. */
export function shapeFor(control) {
  return CONTROL_SHAPES[control] ?? { ok: (v) => !isMapping(v) && !Array.isArray(v), wants: 'a single value' };
}

/** A value described as an operator would read it in the YAML. */
export function describeShape(value) {
  if (Array.isArray(value)) return 'a list';
  if (isMapping(value)) return 'a block of keys';
  if (value instanceof Date) return 'a timestamp';
  return `the ${typeof value} ${JSON.stringify(value)}`;
}

/**
 * Is `prefix` a strict prefix of `pattern`?
 *
 * `'*'` in the pattern matches an array index here exactly as it does in
 * `patternCovers`, and that is not a nicety. The one caller is
 * `unrepresented()`'s empty-container rule, and the case it exists for is a
 * blank row: press "Add a port" and the document holds `ports: [{}]`, whose
 * only leaf is the empty mapping at `ports[0]`. The control that owns it is
 * declared as `ports.*.containerPort` and friends, so a literal comparison of
 * `'*'` against `0` says nothing covers it — and the form announces it is
 * hiding a row the operator is looking at, added by the button they just
 * pressed.
 */
function startsWithPath(pattern, prefix) {
  return (
    pattern.length > prefix.length &&
    prefix.every((segment, i) => (pattern[i] === '*' ? typeof segment === 'number' : pattern[i] === segment))
  );
}

/* ── The document, as text ──────────────────────────────────────────────── */

/**
 * Serialise a form edit back into the editor's text.
 *
 * `lineWidth: -1` is the one option here that is not cosmetic. js-yaml folds
 * long scalars at 80 columns by default, which turns
 * `image: registry.example.com/team/service@sha256:…` into two lines joined by
 * a newline the parser puts back as a space — a valid document holding a
 * different image reference. `noRefs` is the same class of surprise: a
 * document with the same mapping in two places would come back with a `&a1`
 * anchor and a `*a1` alias, which is correct YAML and unreadable to somebody
 * reviewing a diff before applying it to production.
 */
export function toYaml(document) {
  return yaml.dump(document ?? {}, {
    indent: 2,
    lineWidth: -1,
    noRefs: true,
    sortKeys: false,
    quotingType: '"',
  });
}

/**
 * What the text says that the parsed object does not, counted.
 *
 * A form edit writes `toYaml` of the parsed document, and three things in a
 * manifest live in the *text* rather than in the object it parses to. Comments
 * are the one that is genuinely lost — nothing carries them across a parse.
 * Anchors and merge keys are not lost but *expanded*: the parser resolves them,
 * so the rewrite spells out what they stood for, and the object is unchanged
 * while the document that produced it is no longer the one the operator wrote.
 * All three are worth saying before the first form edit, which is the only
 * moment any of it is still actionable.
 *
 * Counted with the editor's own tokenizer rather than with regexes, because
 * `image: registry:5000/app#latest` holds a `#` that is not a comment and
 * `note: "&prod"` holds an `&` that is not an anchor. The tokenizer already
 * tells the two apart — it has to, to colour them — and a second, sloppier
 * implementation here would warn about documents that lose nothing and stay
 * quiet on ones that do.
 */
export function rewriteLosses(text) {
  const empty = { comments: 0, anchors: 0, merges: 0 };
  if (!text) return empty;
  const losses = { ...empty };
  for (const line of tokenizeYaml(text)) {
    if (line.some((token) => token.kind === 'comment')) losses.comments += 1;
    if (line.some((token) => token.kind === 'meta' && /^[&*]/.test(token.text))) losses.anchors += 1;
    if (line.some((token) => token.kind === 'key' && token.text === '<<')) losses.merges += 1;
  }
  return losses;
}

/**
 * An `IntOrString` from what the operator typed.
 *
 * `maxSurge: 25%` and `maxSurge: 1` are both legal and mean different things,
 * and the difference on the wire is the difference between a string and a
 * number. Quoting the `1` makes the API server reject it, so the coercion is
 * not a nicety.
 */
export function intOrString(text) {
  const trimmed = String(text ?? '').trim();
  if (!trimmed) return undefined;
  return /^\d+$/.test(trimmed) ? Number(trimmed) : trimmed;
}

/** A whole number, or `undefined` for "leave this out of the manifest". */
export function integerOrUndefined(text) {
  const trimmed = String(text ?? '').trim();
  if (!trimmed) return undefined;
  const value = Number(trimmed);
  return Number.isInteger(value) ? value : undefined;
}

/* ── Field vocabulary ───────────────────────────────────────────────────── */

/**
 * The controls a field can be. Each one is a shape `ObjectForm.jsx` knows how
 * to render and, more importantly, a **round trip**: what the control writes
 * back into the document is the type the Kubernetes API expects there, not the
 * string an `<input>` produced. `replicas: "3"` is rejected by the API server,
 * `maxSurge: "1"` is rejected by the API server, and both are the kind of
 * mistake a form makes silently.
 *
 * `triBool` is a select rather than a checkbox, and that is a decision. Absent
 * and `false` are not the same statement in a manifest: `runAsNonRoot` absent
 * means nobody said anything, `false` means somebody wrote down that root is
 * acceptable here. A checkbox can only offer two of those three, so it would
 * have to pick one to be a lie — and it would pick "unchecked means false",
 * which writes an explicit permission into a document nobody asked to change.
 */
export const CONTROLS = [
  'text',
  'number',
  'intOrString',
  'select',
  'triBool',
  'checkbox',
  'keyValue',
  'stringLines',
  'checkboxSet',
  'containers',
  'objectList',
];

export const PULL_POLICIES = [
  { value: '', label: 'Not set — the cluster decides' },
  { value: 'IfNotPresent', label: 'IfNotPresent' },
  { value: 'Always', label: 'Always' },
  { value: 'Never', label: 'Never' },
];

const SECCOMP_TYPES = [
  { value: '', label: 'Not set' },
  { value: 'RuntimeDefault', label: 'RuntimeDefault' },
  { value: 'Unconfined', label: 'Unconfined' },
  { value: 'Localhost', label: 'Localhost — needs localhostProfile, set it in YAML view' },
];

/**
 * What the container editor owns, relative to one element of the containers
 * array.
 *
 * Everything else on a container — `volumeMounts`, `livenessProbe`, `lifecycle`,
 * `resources.requests.ephemeral-storage` — is left exactly as written and shows
 * up by name in the "not shown here" list. That list is the honest half of the
 * OpenShift note this view is modelled on, so the patterns below are the
 * *whole* claim this form makes about containers, and adding a control without
 * adding its pattern would quietly widen it.
 */
const CONTAINER_COVERAGE = [
  { path: ['name'] },
  { path: ['image'] },
  { path: ['imagePullPolicy'] },
  { path: ['command'], subtree: true },
  { path: ['args'], subtree: true },
  { path: ['ports'] },
  { path: ['ports', '*', 'containerPort'] },
  { path: ['ports', '*', 'name'] },
  { path: ['ports', '*', 'protocol'] },
  { path: ['env'] },
  { path: ['env', '*', 'name'] },
  { path: ['env', '*', 'value'] },
  { path: ['resources', 'requests', 'cpu'] },
  { path: ['resources', 'requests', 'memory'] },
  { path: ['resources', 'limits', 'cpu'] },
  { path: ['resources', 'limits', 'memory'] },
  { path: ['securityContext', 'allowPrivilegeEscalation'] },
  { path: ['securityContext', 'capabilities', 'drop'], subtree: true },
  { path: ['securityContext', 'capabilities', 'add'], subtree: true },
];

/**
 * The container controls this file promises `ObjectForm.jsx` will render.
 *
 * Named here rather than left implicit because `CONTAINER_COVERAGE` above is a
 * claim about the renderer made in a different file, and the two have to be
 * checked against each other by something. `create-form-view.spec.js` asserts
 * every id below is on screen and editable, so a control deleted from the
 * container editor fails a test instead of quietly moving its field into the
 * set the form hides while saying it hides nothing — which is the one way the
 * "not shown" list can lie.
 */
export function containerControlTestIds(index) {
  return [
    `create-container-name-${index}`,
    `create-container-image-${index}`,
    `create-container-pull-${index}`,
    `create-container-command-${index}`,
    `create-container-args-${index}`,
    // The two sub-editors are proved by their add buttons: those are present
    // whether or not the container has any ports or variables yet, which is
    // what makes them the thing to assert.
    `create-container-${index}-port-add`,
    `create-container-${index}-env-add`,
    `create-container-requests-cpu-${index}`,
    `create-container-requests-memory-${index}`,
    `create-container-limits-cpu-${index}`,
    `create-container-limits-memory-${index}`,
    `create-container-ape-${index}`,
    `create-container-drop-${index}`,
    `create-container-capadd-${index}`,
  ];
}

/* ── Rows of objects ────────────────────────────────────────────────────── */

/**
 * A row control whose value is a block rather than a scalar, so the coverage it
 * generates has to claim the whole subtree under its key.
 *
 * `stringLines` at `rules.*.verbs` represents every element of that list; a
 * `text` at `subjects.*.name` represents exactly one string. Getting this
 * backwards in either direction is the failure `unrepresented()` exists to
 * prevent — too narrow and the form names a field it is showing, too wide and
 * it stays quiet about one it is not.
 */
const ROW_SUBTREE_CONTROLS = new Set(['stringLines', 'keyValue', 'checkboxSet']);

/** A row field's key, as a path. A string is the one-segment spelling of it. */
export function rowKeyPath(sub) {
  return Array.isArray(sub.key) ? sub.key : [sub.key];
}

/**
 * The id fragment a row field's control carries, unique within one row.
 *
 * Derived from the key rather than declared, because a declared id is a second
 * name for the same field and the two go out of sync exactly once — at which
 * point the Playwright assertion that every declared control is on screen is
 * asserting the wrong ids. `id` remains available for the case the key cannot
 * spell: two row fields pointing into the same key with different controls.
 */
export function rowFieldId(sub) {
  return sub.id ?? rowKeyPath(sub).join('-');
}

/**
 * What an `objectList` field covers, derived from the row it renders.
 *
 * Written down nowhere, so it cannot go stale: delete a row field from a model
 * and its key stops being covered on the next render, which puts it straight
 * into the "not shown in this form" list where an operator can act on it. The
 * array itself is covered too, so an explicit `subjects: []` — a document that
 * says "no subjects" rather than one that forgot them — is not reported as a
 * field this form is hiding.
 *
 * This is the same claim `containersSection` writes out by hand for the
 * container editor. That one stays hand-written because the container row is
 * hand-written; every other row editor in this console declares its fields, so
 * every other one derives this.
 */
export function objectListCoverage(field) {
  return [
    { path: field.path },
    ...(field.fields ?? []).map((sub) => ({
      path: [...field.path, '*', ...rowKeyPath(sub)],
      subtree: ROW_SUBTREE_CONTROLS.has(sub.control),
    })),
  ];
}

/**
 * The paths one field claims to represent.
 *
 * Three spellings, in precedence order: an explicit `coverage` (the container
 * editor, whose row is not declared), a derived one for `objectList`, and the
 * default — the field's own path, plus everything under it when the control
 * owns a block.
 */
function coverageFor(field) {
  if (field.coverage) return field.coverage;
  if (field.control === 'objectList') return objectListCoverage(field);
  return [{ path: field.path, subtree: Boolean(field.subtree) }];
}

/**
 * The controls one row of an `objectList` puts on screen, by test id.
 *
 * `containerControlTestIds` exists because the container row is written in
 * `ObjectForm.jsx` and its coverage is claimed in this file, and something has
 * to check the two against each other. A declared row derives both from the
 * same list, so the drift that assertion catches cannot happen here — what this
 * gives a test instead is the ability to assert, for any model, that every row
 * field a model declares is reachable on screen. A row field whose control
 * `ObjectForm.jsx` does not render is the one way a declared field can still be
 * invisible while counted as covered.
 */
export function objectListControlTestIds(field, index) {
  return [
    ...(field.fields ?? []).map((sub) => `create-${field.id}-${rowFieldId(sub)}-${index}`),
    `create-${field.id}-remove-${index}`,
  ];
}

/* ── Section builders ───────────────────────────────────────────────────── */

/** Name, labels and annotations — the same three on every kind. */
function metadataSection({ nameHelp }) {
  return {
    id: 'metadata',
    title: 'Metadata',
    fields: [
      {
        id: 'name',
        label: 'Name',
        control: 'text',
        path: ['metadata', 'name'],
        required: true,
        help: nameHelp,
      },
      {
        id: 'labels',
        label: 'Labels',
        control: 'keyValue',
        path: ['metadata', 'labels'],
        subtree: true,
        help: 'Labels on the object itself. For a workload these are not what the controller uses to find its pods — that is the selector below.',
      },
      {
        id: 'annotations',
        label: 'Annotations',
        control: 'keyValue',
        path: ['metadata', 'annotations'],
        subtree: true,
        help: 'Free-form metadata other controllers read. Nothing here interprets them.',
      },
    ],
  };
}

/**
 * The pod-level fields, at whatever depth this kind keeps its pod spec.
 *
 * `base` is the path to the **pod spec**: `spec` on a Pod, `spec.template.spec`
 * on the four `apps` kinds and a Job, `spec.jobTemplate.spec.template.spec` on
 * a CronJob. Passing it in rather than deriving it is why one builder covers
 * seven kinds without a table of exceptions inside it.
 */
function podSection({ base, labelsPath, labelsSelected = true, restartPolicies, oneShot = false }) {
  const fields = [];
  if (labelsPath) {
    fields.push({
      id: 'podLabels',
      label: 'Pod labels',
      control: 'keyValue',
      path: labelsPath,
      subtree: true,
      // Required only where something selects on them. A Job's selector is
      // generated by the controller from a unique id, so its pod labels are
      // free-form — and telling an operator they must match a selector that
      // does not exist sends them looking for a field this form deliberately
      // does not offer.
      required: labelsSelected,
      help: labelsSelected
        ? 'What the pods this workload creates are labelled with. The selector has to match these, or the API server refuses the object.'
        : 'Labels on the pods this creates. Nothing selects on them — the controller generates its own selector and adds its own labels alongside these — so they are for you and for anything else that reads labels.',
    });
  }
  if (restartPolicies) {
    fields.push({
      id: 'restartPolicy',
      label: 'Restart policy',
      control: 'select',
      path: [...base, 'restartPolicy'],
      options: restartPolicies,
    });
  }
  fields.push(
    {
      id: 'serviceAccountName',
      label: 'Service account',
      control: 'text',
      path: [...base, 'serviceAccountName'],
      help: 'Left empty, the pods run as the namespace’s "default" ServiceAccount, which is what its RoleBindings grant.',
    },
    {
      id: 'nodeSelector',
      label: 'Node selector',
      control: 'keyValue',
      path: [...base, 'nodeSelector'],
      subtree: true,
      help: 'Node labels a node must carry to be eligible. A selector no node matches leaves the pods Pending, and the Pods page says which nodes were ruled out.',
    },
    {
      id: 'runAsNonRoot',
      label: 'Run as non-root',
      control: 'triBool',
      path: [...base, 'securityContext', 'runAsNonRoot'],
      help: 'Required by the restricted Pod Security Standard, which most clusters enforce. It is a statement about the spec: the kubelet still refuses to start an image whose only user is root, after the object has been created.',
    },
    {
      id: 'seccompProfile',
      label: 'Seccomp profile',
      control: 'select',
      path: [...base, 'securityContext', 'seccompProfile', 'type'],
      options: SECCOMP_TYPES,
      help: 'RuntimeDefault is the other half of what the restricted profile asks for.',
    },
  );
  return {
    id: 'pod',
    title: 'Pod',
    // Only on a bare Pod. Every other kind here is a controller writing pod
    // templates, and editing the template rolls new pods; a Pod itself is
    // immutable in almost every field once it exists, so the form is the last
    // chance to get it right rather than the first draft of it.
    description: oneShot
      ? 'Almost everything on a Pod is immutable once it exists — the image and a few scheduling fields are the exceptions. There is no editing your way out of a mistake here; there is deleting it and creating another.'
      : undefined,
    fields,
  };
}

/** The container list. One control, because a container is one row. */
function containersSection({ base }) {
  return {
    id: 'containers',
    title: 'Containers',
    description:
      'At least one, each with a name unique in the pod. Anything not shown on a row — volume mounts, probes, lifecycle hooks — is kept exactly as written and listed under "Not shown in this form" below.',
    fields: [
      {
        id: 'containers',
        label: 'Containers',
        control: 'containers',
        path: [...base, 'containers'],
        required: true,
        coverage: [
          // The array itself, so an explicit `containers: []` — a document that
          // says "no containers" rather than one that forgot to mention them —
          // is not reported as a field this form is hiding.
          { path: [...base, 'containers'] },
          ...CONTAINER_COVERAGE.map((entry) => ({
            path: [...base, 'containers', '*', ...entry.path],
            subtree: Boolean(entry.subtree),
          })),
        ],
      },
    ],
  };
}

/** `spec.selector.matchLabels`, for the kinds where the operator owns it. */
function selectorField(path) {
  return {
    id: 'selector',
    label: 'Selector',
    control: 'keyValue',
    path,
    subtree: true,
    required: true,
    help: 'How the controller finds the pods it owns. It must match the pod labels above, and it is immutable once the object exists — changing it later means deleting and recreating.',
  };
}

/* ── The models ─────────────────────────────────────────────────────────── */

const DNS_NAME_HELP =
  'Lowercase letters, digits, "-" and "." — the API server is the authority and will say so if it disagrees.';

const RBAC_NAME_HELP =
  'RBAC names routinely contain ":", so this one is held to the path-segment rule rather than the DNS rule most kinds use. It is not the name of anything else: a binding is one object, and what it grants is the role it points at.';

/**
 * What a Role's and a ClusterRole's rule editor says about itself, including
 * the one column it does not have.
 *
 * `apiGroups` is left out on purpose and the sentence explaining it is not
 * decoration. The core group is spelled as the empty string, `stringLines`
 * drops an empty line — it has to, or a textarea somebody is still typing in
 * would write a blank argument — so a rule reading `apiGroups: [""]` would
 * render as an empty box. An operator adding "apps" to that box would be
 * *replacing* the core group rather than adding to it, silently, in the field
 * that decides which API the rule is even about. Left uncovered it is named in
 * the "not shown" list and copied through untouched, which is the honest half
 * of the same situation.
 */
const RBAC_RULES_DESCRIPTION =
  'Each rule is a set of verbs over a set of resources: a subject holding this may perform any verb in a rule against any resource in the same rule. `apiGroups` is deliberately not a column here — the core group is written as the empty string and this list editor cannot hold an empty line, so the box would show nothing for a rule that says "core" and adding a group to it would replace that rather than add to it. It is kept exactly as written, named under "Not shown in this form", and edited in YAML view.';

/**
 * One rule, as rows. Shared by Role and ClusterRole, which hold the same shape
 * and differ only in what a rule in it reaches — so the ClusterRole model
 * spreads this and appends the one column a namespaced Role may not have.
 */
const ROLE_RULES_FIELD = {
  id: 'rules',
  label: 'Rules',
  control: 'objectList',
  path: ['rules'],
  rowNoun: 'Rule',
  addLabel: 'Add a rule',
  fields: [
    {
      key: 'resources',
      label: 'Resources',
      control: 'stringLines',
      help: 'Plural and lowercase, as the API serves them — "pods", not "Pod". A subresource is its own entry: "pods/log", "pods/exec". A name RBAC does not recognise grants nothing and says nothing about it.',
    },
    {
      key: 'verbs',
      label: 'Verbs',
      control: 'stringLines',
      required: true,
      help: 'get, list, watch, create, update, patch, delete, deletecollection — plus whatever an aggregated API or a CRD defines for itself. "get" alone does not allow listing, and a "list" on secrets is a read of every secret in scope.',
    },
    {
      key: 'resourceNames',
      label: 'Resource names',
      control: 'stringLines',
      help: 'Narrows the rule to named objects. It does not narrow list, watch, create or deletecollection: those verbs become unusable rather than filtered, because the request names no object for RBAC to match.',
    },
  ],
};

/**
 * One subject, as rows. The namespace column carries different help on the two
 * binding kinds — required on a ClusterRoleBinding, defaulted on a RoleBinding
 * — which is why the ClusterRoleBinding model rewrites that one entry rather
 * than declaring a second list that would drift from this one.
 */
const SUBJECTS_FIELD = {
  id: 'subjects',
  label: 'Subjects',
  control: 'objectList',
  path: ['subjects'],
  rowNoun: 'Subject',
  addLabel: 'Add a subject',
  // A subject with no `kind` is refused outright, and there is no sensible
  // blank for it, so a new row starts as the kind this console creates most.
  rowSeed: { kind: 'ServiceAccount' },
  fields: [
    {
      key: 'kind',
      label: 'Kind',
      control: 'select',
      options: [
        { value: '', label: 'Not set' },
        { value: 'ServiceAccount', label: 'ServiceAccount' },
        { value: 'User', label: 'User' },
        { value: 'Group', label: 'Group' },
      ],
      help: 'Users and groups are strings the authenticator supplies; Kubernetes has no object for either, so nothing here can check that one exists or is spelled the way your identity provider spells it.',
    },
    { key: 'name', label: 'Name', control: 'text' },
    {
      key: 'namespace',
      label: 'Namespace',
      control: 'text',
      help: 'For a ServiceAccount subject. Left empty in a RoleBinding it means this binding’s own namespace, which is why a service account from somewhere else has to be named here explicitly.',
    },
    {
      key: 'apiGroup',
      label: 'API group',
      control: 'text',
      help: 'rbac.authorization.k8s.io for a User or a Group, and empty for a ServiceAccount. The API server refuses each of those the other way round.',
    },
  ],
};

/**
 * One model per kind this form can project.
 *
 * A kind absent from here is not a failure: the dialog offers YAML view alone
 * and says why, which is the same answer OpenShift gives for anything outside
 * its own list. What it must never do is offer a form that covers *some* of a
 * kind and calls itself the create screen for it.
 */
export const FORM_MODELS = [
  {
    apiVersion: 'v1',
    kind: 'Pod',
    podSpecPath: ['spec'],
    podLabelsPath: ['metadata', 'labels'],
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: `The pod’s own name; nothing generates one for it. ${DNS_NAME_HELP}` }),
      // No `podLabels` field: on a bare Pod the pod's labels *are* the object's
      // labels, and two controls writing `metadata.labels` would disagree the
      // moment one of them was edited.
      podSection({
        oneShot: true,
        base: ['spec'],
        labelsPath: null,
        restartPolicies: [
          { value: '', label: 'Not set — defaults to Always' },
          { value: 'Always', label: 'Always' },
          { value: 'OnFailure', label: 'OnFailure' },
          { value: 'Never', label: 'Never' },
        ],
      }),
      containersSection({ base: ['spec'] }),
    ],
  },
  {
    apiVersion: 'apps/v1',
    kind: 'Deployment',
    podSpecPath: ['spec', 'template', 'spec'],
    podLabelsPath: ['spec', 'template', 'metadata', 'labels'],
    selectorPath: ['spec', 'selector', 'matchLabels'],
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'deployment',
        title: 'Deployment strategy',
        fields: [
          {
            id: 'replicas',
            label: 'Replicas',
            control: 'number',
            path: ['spec', 'replicas'],
            min: 0,
            help: 'Left unset the Deployment starts with one. Zero is a valid, running Deployment with no pods.',
          },
          selectorField(['spec', 'selector', 'matchLabels']),
          {
            id: 'strategyType',
            label: 'Strategy type',
            control: 'select',
            path: ['spec', 'strategy', 'type'],
            options: [
              { value: '', label: 'Not set — defaults to RollingUpdate' },
              { value: 'RollingUpdate', label: 'RollingUpdate — start new pods before stopping old ones' },
              { value: 'Recreate', label: 'Recreate — stop every old pod first, then start new ones' },
            ],
            help: 'Recreate has a gap with no pods serving. It is what a workload that cannot run two versions at once needs, and an outage for anything else.',
          },
          {
            id: 'maxUnavailable',
            label: 'Max unavailable',
            control: 'intOrString',
            path: ['spec', 'strategy', 'rollingUpdate', 'maxUnavailable'],
            placeholder: '25%',
            help: 'A count or a percentage. Only read when the strategy is RollingUpdate — the API server rejects it alongside Recreate.',
          },
          {
            id: 'maxSurge',
            label: 'Max surge',
            control: 'intOrString',
            path: ['spec', 'strategy', 'rollingUpdate', 'maxSurge'],
            placeholder: '25%',
            help: 'How far above the replica count the rollout may go. Both of these at 0 is a rollout that can never start.',
          },
        ],
      },
      podSection({ base: ['spec', 'template', 'spec'], labelsPath: ['spec', 'template', 'metadata', 'labels'] }),
      containersSection({ base: ['spec', 'template', 'spec'] }),
    ],
  },
  {
    apiVersion: 'apps/v1',
    kind: 'StatefulSet',
    podSpecPath: ['spec', 'template', 'spec'],
    podLabelsPath: ['spec', 'template', 'metadata', 'labels'],
    selectorPath: ['spec', 'selector', 'matchLabels'],
    sections: [
      metadataSection({
        nameHelp: `Pods are named "<name>-0", "<name>-1" and keep those names across restarts. ${DNS_NAME_HELP}`,
      }),
      {
        id: 'statefulset',
        title: 'StatefulSet',
        fields: [
          {
            id: 'replicas',
            label: 'Replicas',
            control: 'number',
            path: ['spec', 'replicas'],
            min: 0,
          },
          {
            id: 'serviceName',
            label: 'Governing service',
            control: 'text',
            path: ['spec', 'serviceName'],
            help: 'The headless Service that gives each pod its DNS name. It is a required field, and this console does not create it — the pods get stable names either way, but nothing resolves them until that Service exists.',
          },
          selectorField(['spec', 'selector', 'matchLabels']),
          {
            id: 'updateStrategyType',
            label: 'Update strategy',
            control: 'select',
            path: ['spec', 'updateStrategy', 'type'],
            options: [
              { value: '', label: 'Not set — defaults to RollingUpdate' },
              { value: 'RollingUpdate', label: 'RollingUpdate — replace pods newest ordinal first' },
              { value: 'OnDelete', label: 'OnDelete — replace a pod only when it is deleted by hand' },
            ],
          },
          {
            id: 'partition',
            label: 'Rolling update partition',
            control: 'number',
            path: ['spec', 'updateStrategy', 'rollingUpdate', 'partition'],
            min: 0,
            help: 'Only pods with an ordinal at or above this are updated. It is how a staged rollout is held part-way, and a partition left set is a StatefulSet that looks updated and is not.',
          },
          {
            id: 'podManagementPolicy',
            label: 'Pod management policy',
            control: 'select',
            path: ['spec', 'podManagementPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to OrderedReady' },
              { value: 'OrderedReady', label: 'OrderedReady — one at a time, in order' },
              { value: 'Parallel', label: 'Parallel — all at once' },
            ],
            help: 'Immutable after creation.',
          },
        ],
      },
      podSection({ base: ['spec', 'template', 'spec'], labelsPath: ['spec', 'template', 'metadata', 'labels'] }),
      containersSection({ base: ['spec', 'template', 'spec'] }),
    ],
  },
  {
    apiVersion: 'apps/v1',
    kind: 'DaemonSet',
    podSpecPath: ['spec', 'template', 'spec'],
    podLabelsPath: ['spec', 'template', 'metadata', 'labels'],
    selectorPath: ['spec', 'selector', 'matchLabels'],
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'daemonset',
        title: 'DaemonSet',
        // Stated here rather than left to be inferred from a missing control:
        // the same sentence `KIND_LIMITS` gives the disabled Scale button.
        description:
          'A DaemonSet has no replica count — it runs one pod per matching node. The node selector under Pod is what changes how many that is.',
        fields: [
          selectorField(['spec', 'selector', 'matchLabels']),
          {
            id: 'updateStrategyType',
            label: 'Update strategy',
            control: 'select',
            path: ['spec', 'updateStrategy', 'type'],
            options: [
              { value: '', label: 'Not set — defaults to RollingUpdate' },
              { value: 'RollingUpdate', label: 'RollingUpdate' },
              { value: 'OnDelete', label: 'OnDelete — replace a pod only when it is deleted by hand' },
            ],
          },
          {
            id: 'maxUnavailable',
            label: 'Max unavailable',
            control: 'intOrString',
            path: ['spec', 'updateStrategy', 'rollingUpdate', 'maxUnavailable'],
            placeholder: '1',
            help: 'How many nodes may be without this pod during a rollout. Unset it is 1.',
          },
          {
            id: 'maxSurge',
            label: 'Max surge',
            control: 'intOrString',
            path: ['spec', 'updateStrategy', 'rollingUpdate', 'maxSurge'],
            placeholder: '0',
            help: 'How many nodes may briefly run two of this pod. A DaemonSet takes exactly one of these two above zero — setting this one means setting max unavailable to 0 as well, because it is 1 unless you say otherwise.',
          },
        ],
      },
      podSection({ base: ['spec', 'template', 'spec'], labelsPath: ['spec', 'template', 'metadata', 'labels'] }),
      containersSection({ base: ['spec', 'template', 'spec'] }),
    ],
  },
  {
    apiVersion: 'apps/v1',
    kind: 'ReplicaSet',
    podSpecPath: ['spec', 'template', 'spec'],
    podLabelsPath: ['spec', 'template', 'metadata', 'labels'],
    selectorPath: ['spec', 'selector', 'matchLabels'],
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'replicaset',
        title: 'ReplicaSet',
        description:
          'A ReplicaSet on its own has no rollout: it keeps a count of identical pods and nothing more. A Deployment is a ReplicaSet plus the revision history that makes a rollback possible.',
        fields: [
          {
            id: 'replicas',
            label: 'Replicas',
            control: 'number',
            path: ['spec', 'replicas'],
            min: 0,
          },
          selectorField(['spec', 'selector', 'matchLabels']),
        ],
      },
      podSection({ base: ['spec', 'template', 'spec'], labelsPath: ['spec', 'template', 'metadata', 'labels'] }),
      containersSection({ base: ['spec', 'template', 'spec'] }),
    ],
  },
  {
    apiVersion: 'batch/v1',
    kind: 'Job',
    podSpecPath: ['spec', 'template', 'spec'],
    podLabelsPath: ['spec', 'template', 'metadata', 'labels'],
    // Deliberately absent. A Job's selector and the matching pod labels are
    // generated by the controller from a unique id, and a hand-written one that
    // overlaps another Job makes each adopt the other's pods. Offering the
    // field would be offering that.
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'job',
        title: 'Job',
        description:
          'A Job runs its pods to completion. The starter image is a server, and a server does not exit — replace the image, or give it a command that finishes, or the Job stays incomplete until it is deleted.',
        fields: [
          {
            id: 'completions',
            label: 'Completions',
            control: 'number',
            path: ['spec', 'completions'],
            min: 0,
            help: 'How many pods must succeed before the Job is complete. Unset means one.',
          },
          {
            id: 'parallelism',
            label: 'Parallelism',
            control: 'number',
            path: ['spec', 'parallelism'],
            min: 0,
            help: 'How many may run at once. Unset means one.',
          },
          {
            id: 'backoffLimit',
            label: 'Backoff limit',
            control: 'number',
            path: ['spec', 'backoffLimit'],
            min: 0,
            help: 'Retries before the Job is marked failed. Unset means six.',
          },
          {
            id: 'ttlSecondsAfterFinished',
            label: 'Delete this many seconds after finishing',
            control: 'number',
            path: ['spec', 'ttlSecondsAfterFinished'],
            min: 0,
            help: 'Unset, the finished Job and its pods stay until somebody removes them — which is what keeps their logs readable.',
          },
          {
            id: 'suspend',
            label: 'Created suspended',
            control: 'checkbox',
            path: ['spec', 'suspend'],
            help: 'A suspended Job creates no pods until it is resumed. The Workloads page can suspend it afterwards; this is how it starts that way.',
          },
        ],
      },
      podSection({
        base: ['spec', 'template', 'spec'],
        labelsPath: ['spec', 'template', 'metadata', 'labels'],
        labelsSelected: false,
        restartPolicies: [
          { value: 'Never', label: 'Never — a failed pod is replaced' },
          { value: 'OnFailure', label: 'OnFailure — the container is restarted in place' },
        ],
      }),
      containersSection({ base: ['spec', 'template', 'spec'] }),
    ],
  },
  {
    apiVersion: 'batch/v1',
    kind: 'CronJob',
    podSpecPath: ['spec', 'jobTemplate', 'spec', 'template', 'spec'],
    podLabelsPath: ['spec', 'jobTemplate', 'spec', 'template', 'metadata', 'labels'],
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'schedule',
        title: 'Schedule',
        fields: [
          {
            id: 'schedule',
            label: 'Schedule',
            control: 'text',
            path: ['spec', 'schedule'],
            required: true,
            placeholder: '*/5 * * * *',
            help: 'Five cron fields: minute, hour, day of month, month, day of week.',
          },
          {
            id: 'timeZone',
            label: 'Time zone',
            control: 'text',
            path: ['spec', 'timeZone'],
            placeholder: 'Etc/UTC',
            help: 'An IANA name. Left empty the schedule runs in whatever zone the controller manager is in, which is the assumption behind most CronJobs that fire an hour out twice a year.',
          },
          {
            id: 'concurrencyPolicy',
            label: 'Concurrency policy',
            control: 'select',
            path: ['spec', 'concurrencyPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to Allow' },
              { value: 'Allow', label: 'Allow — runs may overlap' },
              { value: 'Forbid', label: 'Forbid — skip the run if the last one is still going' },
              { value: 'Replace', label: 'Replace — cancel the running one and start again' },
            ],
          },
          {
            id: 'suspend',
            label: 'Created suspended',
            control: 'checkbox',
            path: ['spec', 'suspend'],
            help: 'A suspended CronJob creates no Jobs. Unlike the security fields below, absent and false mean the same thing here, so this one is a checkbox.',
          },
        ],
      },
      {
        id: 'jobTemplate',
        title: 'Each run',
        fields: [
          {
            id: 'completions',
            label: 'Completions',
            control: 'number',
            path: ['spec', 'jobTemplate', 'spec', 'completions'],
            min: 0,
          },
          {
            id: 'parallelism',
            label: 'Parallelism',
            control: 'number',
            path: ['spec', 'jobTemplate', 'spec', 'parallelism'],
            min: 0,
          },
          {
            id: 'backoffLimit',
            label: 'Backoff limit',
            control: 'number',
            path: ['spec', 'jobTemplate', 'spec', 'backoffLimit'],
            min: 0,
          },
        ],
      },
      podSection({
        base: ['spec', 'jobTemplate', 'spec', 'template', 'spec'],
        labelsPath: ['spec', 'jobTemplate', 'spec', 'template', 'metadata', 'labels'],
        labelsSelected: false,
        restartPolicies: [
          { value: 'Never', label: 'Never — a failed pod is replaced' },
          { value: 'OnFailure', label: 'OnFailure — the container is restarted in place' },
        ],
      }),
      containersSection({ base: ['spec', 'jobTemplate', 'spec', 'template', 'spec'] }),
    ],
  },
  {
    apiVersion: 'networking.k8s.io/v1',
    kind: 'NetworkPolicy',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'policy',
        title: 'What this policy governs',
        description:
          'A NetworkPolicy describes what should be allowed. Whether any of it is enforced belongs to the CNI plugin, and no API this console reads reports on that.',
        fields: [
          {
            id: 'podSelector',
            label: 'Pod selector',
            control: 'keyValue',
            path: ['spec', 'podSelector', 'matchLabels'],
            subtree: true,
            help: 'Which pods in the namespace this policy applies to. Left empty it selects EVERY pod in the namespace — which is what a default-deny policy wants and a surprise for anything else.',
          },
          {
            id: 'policyTypes',
            label: 'Policy types',
            control: 'checkboxSet',
            path: ['spec', 'policyTypes'],
            subtree: true,
            options: [
              { value: 'Ingress', label: 'Ingress — inbound traffic' },
              { value: 'Egress', label: 'Egress — outbound traffic' },
            ],
            help: 'A type listed here with no matching rules denies that direction entirely. Leaving it out is the opposite: the policy declares no section for that direction, so it does not restrict it at all — and the two look almost identical in YAML. Cleared entirely, the key is removed rather than written as an empty list; the two are the same to the API server, which fills in Ingress either way, and the shorter one is what the document would have said.',
          },
        ],
      },
      {
        id: 'rules',
        title: 'Rules',
        description:
          'Ingress and egress rules are not represented in this form — a peer can be a pod selector, a namespace selector, both at once, or a CIDR block with exceptions, and a form that showed three of those four would be describing a policy that is not the one being created. They are listed below if the document has any, and they are edited in YAML view.',
        fields: [],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'Service',
    podSpecPath: null,
    podLabelsPath: null,
    // Not `spec.selector`, deliberately. `selectorPath` means "the selector of
    // a controller that owns a pod template on this same object", and the
    // checks it turns on compare the two. A Service selects pods some other
    // object created, so there is nothing here to compare against — and
    // "the selector does not match the pod labels" about a Service would be
    // naming a field the kind does not have.
    selectorPath: null,
    nameRule: 'dns1035Label',
    sections: [
      metadataSection({
        nameHelp:
          'Also the DNS name: "<name>.<namespace>.svc" resolves to this from anywhere in the cluster. Lowercase letters, digits and "-", starting with a letter, at most 63 characters.',
      }),
      {
        id: 'service',
        title: 'Service',
        fields: [
          {
            id: 'type',
            label: 'Type',
            control: 'select',
            path: ['spec', 'type'],
            options: [
              { value: '', label: 'Not set — defaults to ClusterIP' },
              { value: 'ClusterIP', label: 'ClusterIP — one address, reachable inside the cluster' },
              { value: 'NodePort', label: 'NodePort — that, plus a port on every node' },
              { value: 'LoadBalancer', label: 'LoadBalancer — that, plus an external address if something provides one' },
              { value: 'ExternalName', label: 'ExternalName — a DNS alias, with no proxying and no endpoints' },
            ],
            help: 'LoadBalancer asks a cloud provider or an in-cluster controller for an address. On a cluster with neither, the Service is created, reports no error, and its external address stays pending forever.',
          },
          {
            id: 'selector',
            label: 'Selector',
            control: 'keyValue',
            path: ['spec', 'selector'],
            subtree: true,
            help: 'The pod labels this sends traffic to. Left out entirely nothing fills in the endpoints and you write them by hand — which is what a Service pointing at an address outside the cluster is for.',
          },
          {
            id: 'clusterIP',
            label: 'Cluster IP',
            control: 'text',
            path: ['spec', 'clusterIP'],
            placeholder: 'None',
            help: '"None" makes it headless: no virtual address, and DNS answers with one record per ready pod — which is what a StatefulSet’s governing Service must be. Left empty the API server allocates one. Immutable after creation.',
          },
          {
            id: 'externalName',
            label: 'External name',
            control: 'text',
            path: ['spec', 'externalName'],
            placeholder: 'service.example.com',
            help: 'Required by type ExternalName and refused on every other type. DNS returns it as an alias and the client connects directly, so nothing here can report whether it resolves or answers.',
          },
        ],
      },
      {
        id: 'ports',
        title: 'Ports',
        description:
          'Two different numbers: "port" is what clients connect to on the Service, "target port" is what the container listens on. A target port written as a name is resolved against each pod, so pods numbering it differently can sit behind one Service.',
        fields: [
          {
            id: 'ports',
            label: 'Ports',
            control: 'objectList',
            path: ['spec', 'ports'],
            rowNoun: 'Port',
            addLabel: 'Add a port',
            fields: [
              {
                key: 'name',
                label: 'Name',
                control: 'text',
                help: 'Required as soon as there is more than one port, and what an Ingress or a NetworkPolicy refers to when it names a port rather than numbering it.',
              },
              { key: 'port', label: 'Port', control: 'number', min: 1, required: true },
              {
                key: 'targetPort',
                label: 'Target port',
                control: 'intOrString',
                placeholder: '8080',
                help: 'A number or a container port’s name. Left empty it is the same as "port", which is rarely what an unprivileged image listening on 8080 wants.',
              },
              {
                key: 'protocol',
                label: 'Protocol',
                control: 'select',
                options: [
                  { value: '', label: 'Not set — TCP' },
                  { value: 'TCP', label: 'TCP' },
                  { value: 'UDP', label: 'UDP' },
                  { value: 'SCTP', label: 'SCTP' },
                ],
              },
              {
                key: 'nodePort',
                label: 'Node port',
                control: 'number',
                help: 'Only on NodePort and LoadBalancer. Left empty the API server allocates one from the cluster’s range; pinning a number risks a collision that arrives as a rejected create.',
              },
            ],
          },
        ],
      },
      {
        id: 'traffic',
        title: 'Traffic',
        fields: [
          {
            id: 'sessionAffinity',
            label: 'Session affinity',
            control: 'select',
            path: ['spec', 'sessionAffinity'],
            options: [
              { value: '', label: 'Not set — defaults to None' },
              { value: 'None', label: 'None — every connection is balanced independently' },
              { value: 'ClientIP', label: 'ClientIP — a client keeps hitting the same pod' },
            ],
          },
          {
            id: 'sessionAffinityTimeout',
            label: 'Session affinity timeout (seconds)',
            control: 'number',
            path: ['spec', 'sessionAffinityConfig', 'clientIP', 'timeoutSeconds'],
            min: 1,
            help: 'Read only when the affinity is ClientIP. The API server refuses this block alongside None.',
          },
          {
            id: 'externalTrafficPolicy',
            label: 'External traffic policy',
            control: 'select',
            path: ['spec', 'externalTrafficPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to Cluster' },
              { value: 'Cluster', label: 'Cluster — any node forwards to any pod' },
              { value: 'Local', label: 'Local — keep the client address, and drop traffic on nodes with no pod' },
            ],
            help: 'Only on NodePort and LoadBalancer, and refused on a ClusterIP Service. Local is how the real client address survives; it also black-holes traffic reaching a node this workload does not run on.',
          },
          {
            id: 'internalTrafficPolicy',
            label: 'Internal traffic policy',
            control: 'select',
            path: ['spec', 'internalTrafficPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to Cluster' },
              { value: 'Cluster', label: 'Cluster — any pod in the cluster' },
              { value: 'Local', label: 'Local — only a pod on the caller’s own node' },
            ],
            help: 'Local with no pod on the calling node is a connection refused, not a slower path.',
          },
          {
            id: 'loadBalancerClass',
            label: 'Load balancer class',
            control: 'text',
            path: ['spec', 'loadBalancerClass'],
            help: 'Which implementation claims this Service. Only on type LoadBalancer, and immutable after creation; a class no controller answers for leaves the address pending with nothing reporting why.',
          },
          {
            id: 'loadBalancerSourceRanges',
            label: 'Load balancer source ranges',
            control: 'stringLines',
            path: ['spec', 'loadBalancerSourceRanges'],
            placeholder: '203.0.113.0/24',
            help: 'One CIDR per line. Whether any of it is enforced belongs to the load balancer implementation — several ignore the field entirely, and nothing in this console can tell you which yours does.',
          },
          {
            id: 'externalIPs',
            label: 'External IPs',
            control: 'stringLines',
            path: ['spec', 'externalIPs'],
            help: 'Addresses you already route to the nodes yourself. Kubernetes does not allocate, announce or verify them; it only forwards what arrives.',
          },
          {
            id: 'publishNotReadyAddresses',
            label: 'Publish not-ready addresses',
            control: 'checkbox',
            path: ['spec', 'publishNotReadyAddresses'],
            help: 'Pods appear in DNS before they pass their readiness probe. A StatefulSet’s peers need it to find each other during start-up; anything else gets traffic sent to pods that have said they are not ready for it.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'ConfigMap',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'configmap',
        title: 'Data',
        description:
          '`binaryData` is not shown here. Its values are base64, and a text box that accepted plaintext would produce a file whose contents decode to something nobody wrote — with a diff that faithfully shows what was typed. It is listed below if the document has any, and edited in YAML view.',
        fields: [
          {
            id: 'data',
            label: 'Data',
            control: 'keyValue',
            path: ['data'],
            subtree: true,
            help: 'One key per setting, or one key per file when this is mounted as a volume — the key becomes the filename. Values are strings: an unquoted number is refused for the whole object. A value holding more than one line is shown as what it is and left alone, because a single-line box would write back only the line it could show.',
          },
          {
            id: 'immutable',
            label: 'Immutable',
            control: 'checkbox',
            path: ['immutable'],
            help: 'An immutable ConfigMap cannot be edited afterwards, only deleted and recreated. That is the point of it: the kubelet stops watching, so nothing changes under a pod that has already read it.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'Secret',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'secret',
        title: 'Contents',
        description:
          '`data` is not shown, and that is not squeamishness: its values are base64, a text box invites plaintext, and the Secret that results is accepted by the API server and fails later as an application that cannot authenticate. Write plain values under `stringData` instead — the API server encodes them into `data` and the created object comes back with no `stringData` at all, which is why the dry-run diff shows the field moving.',
        fields: [
          {
            id: 'type',
            label: 'Type',
            // A text box and not a select, which is the opposite of what this
            // field looks like it wants. `Secret.type` is an open string: the
            // seven built-ins below are the ones with required-key rules, and
            // `helm.sh/release.v1` and `bootstrap.kubernetes.io/token` are
            // equally legal. A closed list would claim — through its coverage —
            // to represent a field it cannot write, and an operator making a
            // `kubernetes.io/dockercfg` pull secret would find no option for it
            // and nothing on screen saying to use YAML view.
            control: 'text',
            path: ['type'],
            placeholder: 'Opaque',
            help: 'Left empty this is Opaque. The built-in types are kubernetes.io/tls (needs tls.crt and tls.key), kubernetes.io/dockerconfigjson and the older kubernetes.io/dockercfg, kubernetes.io/basic-auth, kubernetes.io/ssh-auth (needs ssh-privatekey), kubernetes.io/service-account-token and bootstrap.kubernetes.io/token; anything else is a type somebody else defined, and the API server accepts it. The type decides which keys are required and the API server enforces that, but it does not check that what is under them parses: a tls.crt that is not a certificate is accepted here and fails at whatever loads it.',
          },
          {
            id: 'stringData',
            label: 'String data',
            control: 'keyValue',
            path: ['stringData'],
            subtree: true,
            help: 'Plain values, encoded for you. A value holding more than one line — a PEM certificate, an SSH key — is shown as what it is and left alone; a single-line box would write back one line of it.',
          },
          {
            id: 'immutable',
            label: 'Immutable',
            control: 'checkbox',
            path: ['immutable'],
            help: 'Cannot be edited afterwards, only deleted and recreated. The kubelet stops watching an immutable Secret, so nothing can change under a pod that has already read it.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'ServiceAccount',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'serviceaccount',
        title: 'ServiceAccount',
        description:
          'Creating this grants nothing until a RoleBinding names it, and creates no token Secret — since 1.24 pods get a short-lived projected token instead. `secrets` is not shown: the token controller filled it in before 1.24, and adding to it by hand is a way to mount a token this account does not own.',
        fields: [
          {
            id: 'automountServiceAccountToken',
            label: 'Automount API token',
            control: 'triBool',
            path: ['automountServiceAccountToken'],
            help: 'Three states, not two: unset leaves the decision to each pod spec, true mounts a token even where the pod said nothing, and false refuses one however the pod asks. A workload that never talks to the API server wants false.',
          },
          {
            id: 'imagePullSecrets',
            label: 'Image pull secrets',
            control: 'objectList',
            path: ['imagePullSecrets'],
            rowNoun: 'Secret',
            addLabel: 'Add a pull secret',
            fields: [
              {
                key: 'name',
                label: 'Secret name',
                control: 'text',
                required: true,
                help: 'A Secret in this same namespace, usually of type kubernetes.io/dockerconfigjson. It applies to every pod running as this account, which is how a private registry is configured once instead of per workload. Nothing here checks that it exists — a missing one arrives as ImagePullBackOff.',
              },
            ],
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'Namespace',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    nameRule: 'dnsLabel',
    sections: [
      metadataSection({
        nameHelp:
          'Lowercase letters, digits and "-", at most 63 characters and no dots — a namespace is a DNS label, not the subdomain most other names are held to. It cannot be renamed.',
      }),
      {
        id: 'namespace',
        title: 'What comes with it',
        description:
          'Nothing else: no quota, no limit range, no default-deny policy. The Projects action creates those alongside a namespace; this creates the namespace alone. Pod Security is configured with three ordinary labels on it — pod-security.kubernetes.io/enforce, /warn and /audit, each privileged, baseline or restricted, with an optional matching -version label — so they are written in the Labels editor above rather than in a control of their own. `spec.finalizers` is deliberately not shown: the API server writes it, and a finalizer list edited by hand is how a namespace ends up stuck Terminating with nothing in this console able to release it.',
        fields: [],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'PersistentVolumeClaim',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'claim',
        title: 'Storage',
        description:
          '`spec.storageClassName` is not shown, and the reason is the difference between absent and empty: absent means "use whatever the cluster’s default class is", `""` means "never provision this dynamically, bind only to a volume that already matches". A text box writes nothing at all for an empty input, so it could read the second and write the first — a claim that quietly starts provisioning. It is edited in YAML view. `spec.selector` is not shown either: it carries match expressions this form has no control for.',
        fields: [
          {
            id: 'accessModes',
            label: 'Access modes',
            control: 'checkboxSet',
            path: ['spec', 'accessModes'],
            subtree: true,
            required: true,
            options: [
              { value: 'ReadWriteOnce', label: 'ReadWriteOnce — one node at a time, read and write' },
              { value: 'ReadOnlyMany', label: 'ReadOnlyMany — many nodes, read only' },
              { value: 'ReadWriteMany', label: 'ReadWriteMany — many nodes, read and write' },
              { value: 'ReadWriteOncePod', label: 'ReadWriteOncePod — one pod, and no other may mount it' },
            ],
            help: 'What the volume must support, not what it will be restricted to. A driver that cannot offer the mode you ask for leaves the claim Pending with an event, which is the honest failure; asking for less than the workload needs is the one that shows up later.',
          },
          {
            id: 'storage',
            label: 'Storage',
            control: 'text',
            path: ['spec', 'resources', 'requests', 'storage'],
            required: true,
            placeholder: '10Gi',
            help: 'A quantity, so text rather than a number: 10Gi is 10 737 418 240 bytes and 10G is 10 000 000 000, a 7% difference that arrives as a volume smaller than expected.',
          },
          {
            id: 'volumeMode',
            label: 'Volume mode',
            control: 'select',
            path: ['spec', 'volumeMode'],
            options: [
              { value: '', label: 'Not set — defaults to Filesystem' },
              { value: 'Filesystem', label: 'Filesystem — a formatted volume, mounted at a path' },
              { value: 'Block', label: 'Block — a raw device, with no filesystem on it' },
            ],
            help: 'Immutable. A Block volume is handed to the container as a device rather than a mount, and a workload expecting a directory finds nothing there.',
          },
          {
            id: 'volumeName',
            label: 'Volume name',
            control: 'text',
            path: ['spec', 'volumeName'],
            help: 'Binds to one PersistentVolume by name instead of asking for a new one. The volume has to match on capacity and access modes or the claim stays Pending.',
          },
        ],
      },
      {
        id: 'dataSource',
        title: 'Populated from',
        description:
          '`spec.dataSourceRef` is the newer spelling of these three fields and is not shown here: it takes a namespace this one does not, and the API server refuses a claim whose two disagree. Fill in one or the other, and the newer one in YAML view.',
        fields: [
          {
            id: 'dataSourceKind',
            label: 'Kind',
            control: 'text',
            path: ['spec', 'dataSource', 'kind'],
            placeholder: 'VolumeSnapshot',
            help: 'VolumeSnapshot or PersistentVolumeClaim. The new volume is a copy: writing to it does not touch the source, and deleting the source afterwards does not affect it.',
          },
          {
            id: 'dataSourceApiGroup',
            label: 'API group',
            control: 'text',
            path: ['spec', 'dataSource', 'apiGroup'],
            placeholder: 'snapshot.storage.k8s.io',
            help: 'Left empty means the core group, which is what a PersistentVolumeClaim source needs. A VolumeSnapshot needs snapshot.storage.k8s.io.',
          },
          {
            id: 'dataSourceName',
            label: 'Name',
            control: 'text',
            path: ['spec', 'dataSource', 'name'],
            help: 'In this same namespace. The request above must be at least the snapshot’s restore size.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'v1',
    kind: 'ResourceQuota',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'quota',
        title: 'Hard limits',
        description:
          'The moment a quota constrains requests.cpu or requests.memory, every pod created in this namespace afterwards must state that request or admission refuses it — including pods from controllers that were working a minute ago, and the 403 does not name the rule. That is the compulsory-resource rule the Quota advisor exists to say out loud.',
        fields: [
          {
            id: 'hard',
            label: 'Hard limits',
            control: 'keyValue',
            path: ['spec', 'hard'],
            subtree: true,
            required: true,
            help: 'Capacity keys (requests.cpu, limits.memory) and count keys (pods, services.loadbalancers, count/deployments.apps) in one block. Values are quantities, which are strings — the editor here writes strings, which is what the API server wants.',
          },
        ],
      },
      {
        id: 'scopes',
        title: 'Scopes',
        description:
          'A scoped quota applies to the subset of pods its scopes select, which is how one namespace gets a small high-priority budget and a larger ordinary one. With no scopes it applies to everything in the namespace.',
        fields: [
          {
            id: 'quotaScopes',
            label: 'Scopes',
            control: 'checkboxSet',
            path: ['spec', 'scopes'],
            subtree: true,
            options: [
              { value: 'Terminating', label: 'Terminating — pods with an activeDeadlineSeconds' },
              { value: 'NotTerminating', label: 'NotTerminating — pods without one' },
              { value: 'BestEffort', label: 'BestEffort — pods with no requests or limits at all' },
              { value: 'NotBestEffort', label: 'NotBestEffort — pods that state some' },
              { value: 'PriorityClass', label: 'PriorityClass — needs a scope selector below' },
              { value: 'CrossNamespacePodAffinity', label: 'CrossNamespacePodAffinity' },
            ],
            help: 'BestEffort selects exactly the pods that state no requests or limits, so a capacity key can never be consumed under it and the API server refuses the pair.',
          },
          {
            id: 'scopeSelector',
            label: 'Scope selector',
            control: 'objectList',
            path: ['spec', 'scopeSelector', 'matchExpressions'],
            rowNoun: 'Term',
            addLabel: 'Add a scope term',
            fields: [
              {
                key: 'scopeName',
                label: 'Scope',
                control: 'select',
                options: [
                  { value: '', label: 'Not set' },
                  { value: 'PriorityClass', label: 'PriorityClass' },
                  { value: 'Terminating', label: 'Terminating' },
                  { value: 'NotTerminating', label: 'NotTerminating' },
                  { value: 'BestEffort', label: 'BestEffort' },
                  { value: 'NotBestEffort', label: 'NotBestEffort' },
                  { value: 'CrossNamespacePodAffinity', label: 'CrossNamespacePodAffinity' },
                ],
              },
              {
                key: 'operator',
                label: 'Operator',
                control: 'select',
                options: [
                  { value: '', label: 'Not set' },
                  { value: 'In', label: 'In' },
                  { value: 'NotIn', label: 'NotIn' },
                  { value: 'Exists', label: 'Exists' },
                  { value: 'DoesNotExist', label: 'DoesNotExist' },
                ],
              },
              {
                key: 'values',
                label: 'Values',
                control: 'stringLines',
                help: 'One per line, and only with In or NotIn — the API server refuses values beside Exists, and refuses In with none.',
              },
            ],
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'networking.k8s.io/v1',
    kind: 'Ingress',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'ingress',
        title: 'Ingress',
        fields: [
          {
            id: 'ingressClassName',
            label: 'Ingress class',
            control: 'text',
            path: ['spec', 'ingressClassName'],
            help: 'Which controller claims this. The Ingress Classes tab lists what this cluster actually serves; a name no class matches produces an object that is accepted, reports no error and routes nothing.',
          },
          {
            id: 'defaultBackendService',
            label: 'Default backend service',
            control: 'text',
            path: ['spec', 'defaultBackend', 'service', 'name'],
            help: 'Where anything matching no rule goes. A Service in this same namespace; a backend naming a resource rather than a Service is edited in YAML view.',
          },
          {
            id: 'defaultBackendPort',
            label: 'Default backend port',
            control: 'number',
            path: ['spec', 'defaultBackend', 'service', 'port', 'number'],
            min: 1,
            help: 'The Service’s port, not the container’s. A backend may name the port instead — `port.name` — and that spelling is edited in YAML view; the API server refuses both together.',
          },
        ],
      },
      {
        id: 'tls',
        title: 'TLS',
        fields: [
          {
            id: 'tls',
            label: 'TLS',
            control: 'objectList',
            path: ['spec', 'tls'],
            rowNoun: 'Certificate',
            addLabel: 'Add a certificate',
            fields: [
              {
                key: 'hosts',
                label: 'Hosts',
                control: 'stringLines',
                help: 'One per line. A host listed here that no rule below serves means a certificate that is loaded and never presented for that name.',
              },
              {
                key: 'secretName',
                label: 'Secret',
                control: 'text',
                help: 'A kubernetes.io/tls Secret in this namespace. Nothing here checks that it exists or that its subject alternative names cover the hosts — the Routes page reads the certificate an exposure points at and says both.',
              },
            ],
          },
        ],
      },
      {
        id: 'rules',
        title: 'Rules',
        description:
          'The host rules are not represented in this form. A rule holds a list of paths, each with its own path type and its own backend — a list inside a row, which this console’s row editor is one level too flat to hold — and a form that showed the first path of the first rule would be describing a different Ingress. They are listed below if the document has any, and they are edited in YAML view.',
        fields: [],
      },
    ],
  },
  {
    apiVersion: 'rbac.authorization.k8s.io/v1',
    kind: 'Role',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    nameRule: 'pathSegment',
    sections: [
      metadataSection({
        nameHelp:
          'RBAC names routinely contain ":" — "example.com:read" is a legal Role name — so this one is held to the path-segment rule rather than the DNS rule most kinds use.',
      }),
      {
        id: 'rules',
        title: 'Rules',
        description: RBAC_RULES_DESCRIPTION,
        fields: [ROLE_RULES_FIELD],
      },
    ],
  },
  {
    apiVersion: 'rbac.authorization.k8s.io/v1',
    kind: 'ClusterRole',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    nameRule: 'pathSegment',
    sections: [
      metadataSection({
        nameHelp:
          'RBAC names routinely contain ":" — "system:controller:example" is a legal ClusterRole name — so this one is held to the path-segment rule rather than the DNS rule most kinds use.',
      }),
      {
        id: 'rules',
        title: 'Rules',
        description: `${RBAC_RULES_DESCRIPTION} Bound with a ClusterRoleBinding these rules apply in every namespace, including ones created afterwards; bound with a RoleBinding the same object grants them in one namespace only.`,
        fields: [
          {
            ...ROLE_RULES_FIELD,
            fields: [
              ...ROLE_RULES_FIELD.fields,
              {
                key: 'nonResourceURLs',
                label: 'Non-resource URLs',
                control: 'stringLines',
                help: '/healthz, /metrics and the like. Only a ClusterRole can grant these, and the API server refuses a rule that carries them alongside resources.',
              },
            ],
          },
        ],
      },
      {
        id: 'aggregation',
        title: 'Aggregation',
        description:
          '`aggregationRule` is not shown: its selectors are blocks of match labels, and a row of single-line boxes cannot hold one. What it does is worth knowing before you write one in YAML view — the controller overwrites `rules` on this object from every ClusterRole the selectors match, so anything typed above disappears within seconds of the create and the object as stored is not the object in the diff you approved.',
        fields: [],
      },
    ],
  },
  {
    apiVersion: 'rbac.authorization.k8s.io/v1',
    kind: 'RoleBinding',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    nameRule: 'pathSegment',
    sections: [
      metadataSection({ nameHelp: RBAC_NAME_HELP }),
      {
        id: 'roleRef',
        title: 'Role',
        description:
          '`roleRef` is immutable. A binding pointing at the wrong role is deleted and recreated, not edited — which is why the Access page’s grant action changes subjects and never the role a binding names.',
        fields: [
          {
            id: 'roleRefKind',
            label: 'Role kind',
            control: 'select',
            path: ['roleRef', 'kind'],
            options: [
              { value: '', label: 'Not set' },
              { value: 'Role', label: 'Role — in this namespace' },
              { value: 'ClusterRole', label: 'ClusterRole — its rules, applied in this namespace only' },
            ],
            help: 'A ClusterRole named here grants its rules inside this namespace and nowhere else. That is how "view" and "edit" are handed out; the same role in a ClusterRoleBinding would be cluster-wide.',
          },
          {
            id: 'roleRefName',
            label: 'Role name',
            control: 'text',
            path: ['roleRef', 'name'],
            required: true,
            help: 'Nothing here checks that the role exists: a binding to a missing role is created without complaint and grants nothing until something with that name appears.',
          },
          {
            id: 'roleRefApiGroup',
            label: 'Role API group',
            control: 'text',
            path: ['roleRef', 'apiGroup'],
            placeholder: 'rbac.authorization.k8s.io',
            help: 'Always rbac.authorization.k8s.io. The API server refuses anything else, including the empty value a document that omits it decodes to.',
          },
        ],
      },
      {
        id: 'subjects',
        title: 'Subjects',
        fields: [SUBJECTS_FIELD],
      },
    ],
  },
  {
    apiVersion: 'rbac.authorization.k8s.io/v1',
    kind: 'ClusterRoleBinding',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    nameRule: 'pathSegment',
    sections: [
      metadataSection({ nameHelp: RBAC_NAME_HELP }),
      {
        id: 'roleRef',
        title: 'Role',
        description:
          'This grants the role in every namespace that exists and every namespace created afterwards. `roleRef` is immutable — a binding pointing at the wrong role is deleted and recreated, not edited.',
        fields: [
          {
            id: 'roleRefKind',
            label: 'Role kind',
            control: 'select',
            path: ['roleRef', 'kind'],
            options: [
              { value: '', label: 'Not set' },
              { value: 'ClusterRole', label: 'ClusterRole — the only kind a ClusterRoleBinding may name' },
            ],
            help: 'A ClusterRoleBinding cannot reference a namespaced Role, and the API server refuses one that tries.',
          },
          {
            id: 'roleRefName',
            label: 'Role name',
            control: 'text',
            path: ['roleRef', 'name'],
            required: true,
            help: 'Nothing here checks that the ClusterRole exists. A binding to a missing one is created without complaint and grants nothing until something with that name appears.',
          },
          {
            id: 'roleRefApiGroup',
            label: 'Role API group',
            control: 'text',
            path: ['roleRef', 'apiGroup'],
            placeholder: 'rbac.authorization.k8s.io',
            help: 'Always rbac.authorization.k8s.io. The API server refuses anything else, including the empty value a document that omits it decodes to.',
          },
        ],
      },
      {
        id: 'subjects',
        title: 'Subjects',
        fields: [
          {
            ...SUBJECTS_FIELD,
            fields: SUBJECTS_FIELD.fields.map((sub) =>
              sub.key === 'namespace'
                ? {
                    ...sub,
                    help: 'Required on a ServiceAccount subject here: a ClusterRoleBinding has no namespace of its own for one to default to, and the API server refuses a subject without it.',
                  }
                : sub,
            ),
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'policy/v1',
    kind: 'PodDisruptionBudget',
    podSpecPath: null,
    podLabelsPath: null,
    // Not `spec.selector.matchLabels`: a budget selects pods some other object
    // creates, so there is no pod template on this document to check it
    // against. The empty-selector reading a budget needs is its own check
    // below, because in policy/v1 an empty selector means the opposite of what
    // an absent one does.
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'budget',
        title: 'Budget',
        description:
          'A budget bounds voluntary evictions — what a drain uses. It does not stop a node failing, a pod crashing, or a plain delete, and `force` on a drain does not defeat it either: the API server enforces this on the eviction subresource.',
        fields: [
          {
            id: 'minAvailable',
            label: 'Min available',
            control: 'intOrString',
            path: ['spec', 'minAvailable'],
            placeholder: '1',
            help: 'A count or a percentage. An eviction is refused while it would drop the number of ready pods below this.',
          },
          {
            id: 'maxUnavailable',
            label: 'Max unavailable',
            control: 'intOrString',
            path: ['spec', 'maxUnavailable'],
            placeholder: '1',
            help: 'The same rule stated relative to the desired count, which is what a workload that scales wants. The API server refuses a budget carrying both this and min available.',
          },
          {
            id: 'pdbSelector',
            label: 'Selector',
            control: 'keyValue',
            path: ['spec', 'selector', 'matchLabels'],
            subtree: true,
            help: 'Which pods this covers. In policy/v1 an empty selector covers EVERY pod in the namespace and an absent one covers none — the reverse of the removed policy/v1beta1, and invisible in the YAML. Match expressions are not shown here and are edited in YAML view.',
          },
          {
            id: 'unhealthyPodEvictionPolicy',
            label: 'Unhealthy pod eviction policy',
            control: 'select',
            path: ['spec', 'unhealthyPodEvictionPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to IfHealthyBudget' },
              { value: 'IfHealthyBudget', label: 'IfHealthyBudget — a not-ready pod is protected too' },
              { value: 'AlwaysAllow', label: 'AlwaysAllow — a not-ready pod may always be evicted' },
            ],
            help: 'IfHealthyBudget is how a crash-looping workload makes a node undrainable: the pods are not ready, so the budget is never satisfied, so nothing may be evicted. This field reached GA recently — an older API server drops it silently, and the dry-run projection is what shows whether it survived.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'autoscaling/v2',
    kind: 'HorizontalPodAutoscaler',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: DNS_NAME_HELP }),
      {
        id: 'target',
        title: 'Target',
        fields: [
          {
            id: 'scaleTargetRefKind',
            label: 'Kind',
            // Text, for the same reason `Secret.type` is. A scale target is
            // anything serving a `scale` subresource, which on a real cluster
            // routinely means a CRD — an Argo Rollout, a Knative Service. A
            // closed list would offer four kinds, claim through its coverage to
            // represent the field, and leave an operator scaling a Rollout with
            // no option and no sentence telling them to use YAML view.
            control: 'text',
            path: ['spec', 'scaleTargetRef', 'kind'],
            placeholder: 'Deployment',
            help: 'Anything serving a scale subresource: Deployment, StatefulSet, ReplicaSet, ReplicationController, and whatever CRDs on this cluster serve one. A DaemonSet does not — it runs one pod per matching node — so an autoscaler pointed at one is created and scales nothing.',
          },
          {
            id: 'scaleTargetRefApiVersion',
            label: 'API version',
            control: 'text',
            path: ['spec', 'scaleTargetRef', 'apiVersion'],
            placeholder: 'apps/v1',
          },
          {
            id: 'scaleTargetRefName',
            label: 'Name',
            control: 'text',
            path: ['spec', 'scaleTargetRef', 'name'],
            required: true,
            help: 'In this same namespace. Nothing here checks that it exists; an autoscaler pointed at nothing reports it as a failure to fetch the scale subresource.',
          },
          {
            id: 'minReplicas',
            label: 'Min replicas',
            control: 'number',
            path: ['spec', 'minReplicas'],
            min: 0,
            help: 'Unset means one. Zero is refused unless the cluster runs with scale-to-zero enabled, which is a fact about the cluster this document cannot settle.',
          },
          {
            id: 'maxReplicas',
            label: 'Max replicas',
            control: 'number',
            path: ['spec', 'maxReplicas'],
            min: 1,
            required: true,
            help: 'Required, and the ceiling that actually protects the cluster. It must be at least min replicas.',
          },
        ],
      },
      {
        id: 'metrics',
        title: 'Metrics',
        description:
          '`spec.metrics` is not represented in this form. A metric is one of four shapes — Resource, Pods, Object, External — and exactly one block may be present beside its type; a row offering all four would invite a document carrying two, which the API server refuses. `spec.behavior` is the same answer for a different reason: two nested lists of scaling policies. Both are listed below if the document has them, and both are edited in YAML view. Note that CPU utilization is a percentage of the pod’s CPU *request*, so a workload with no request cannot be scaled on it at all.',
        fields: [],
      },
    ],
  },
  {
    apiVersion: 'storage.k8s.io/v1',
    kind: 'StorageClass',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({
        nameHelp: `The name a claim asks for in its "storageClassName", and it cannot be changed afterwards. ${DNS_NAME_HELP}`,
      }),
      {
        id: 'class',
        title: 'Provisioning',
        description:
          'A StorageClass has no `spec`: every field below is top-level. `allowedTopologies` is not shown — nested label expressions — and is edited in YAML view. Making this the cluster default is an annotation, storageclass.kubernetes.io/is-default-class: "true", written in the Annotations editor above; with two defaults, a claim naming no class is refused rather than defaulted.',
        fields: [
          {
            id: 'provisioner',
            label: 'Provisioner',
            control: 'text',
            path: ['provisioner'],
            required: true,
            placeholder: 'csi.example.com',
            help: 'The CSI driver that creates volumes for this class, or kubernetes.io/no-provisioner, which provisions nothing and exists so claims and hand-made volumes can name each other. Immutable after creation.',
          },
          {
            id: 'parameters',
            label: 'Parameters',
            control: 'keyValue',
            path: ['parameters'],
            subtree: true,
            help: 'Defined by the driver, not by Kubernetes — this console cannot tell you which ones yours takes. Values are strings: an unquoted number is refused.',
          },
          {
            id: 'reclaimPolicy',
            label: 'Reclaim policy',
            control: 'select',
            path: ['reclaimPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to Delete' },
              { value: 'Delete', label: 'Delete — deleting the claim destroys the volume' },
              { value: 'Retain', label: 'Retain — the volume outlives the claim and needs a human' },
            ],
          },
          {
            id: 'volumeBindingMode',
            label: 'Volume binding mode',
            control: 'select',
            path: ['volumeBindingMode'],
            options: [
              { value: '', label: 'Not set — defaults to Immediate' },
              { value: 'Immediate', label: 'Immediate — provision as soon as the claim exists' },
              { value: 'WaitForFirstConsumer', label: 'WaitForFirstConsumer — wait until a pod is scheduled' },
            ],
            help: 'WaitForFirstConsumer leaves a claim Pending until something uses it, which is expected rather than a fault — and it is what makes a node-local volume land on a node the pod can actually run on.',
          },
          {
            id: 'allowVolumeExpansion',
            label: 'Allow volume expansion',
            control: 'triBool',
            path: ['allowVolumeExpansion'],
            help: 'Whether a claim from this class can be grown later, which is what the Expand action on the Storage page needs. Unset and false both mean no; the driver has to support it either way.',
          },
          {
            id: 'mountOptions',
            label: 'Mount options',
            control: 'stringLines',
            path: ['mountOptions'],
            help: 'One per line, passed through to the mount. Nothing validates them here or at create time: an option the filesystem rejects surfaces as a pod that cannot mount its volume.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'scheduling.k8s.io/v1',
    kind: 'PriorityClass',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: `The name a pod spec puts in its "priorityClassName". ${DNS_NAME_HELP}` }),
      {
        id: 'priority',
        title: 'Priority',
        description:
          'A PriorityClass has no `spec`: all four fields below are top-level, and a document that nests them under one is accepted with none of them set.',
        fields: [
          {
            id: 'value',
            label: 'Value',
            control: 'number',
            path: ['value'],
            required: true,
            help: 'Higher is scheduled sooner. A user-defined class may not go above 1000000000; above that is reserved for the built-in system classes.',
          },
          {
            id: 'preemptionPolicy',
            label: 'Preemption policy',
            control: 'select',
            path: ['preemptionPolicy'],
            options: [
              { value: '', label: 'Not set — defaults to PreemptLowerPriority' },
              { value: 'PreemptLowerPriority', label: 'PreemptLowerPriority — evict lower-priority pods to fit' },
              { value: 'Never', label: 'Never — queue ahead of them, but evict nothing' },
            ],
            help: 'Never is what "important, but not at the cost of something already running" actually looks like.',
          },
          {
            id: 'globalDefault',
            label: 'Global default',
            control: 'checkbox',
            path: ['globalDefault'],
            help: 'The priority given to every pod created afterwards that names no class, cluster-wide. Pods already running are unaffected, and only one class in a cluster should carry it.',
          },
          {
            id: 'description',
            label: 'Description',
            control: 'text',
            path: ['description'],
            help: 'Free text for whoever reads this class later. Nothing interprets it.',
          },
        ],
      },
    ],
  },
  {
    apiVersion: 'node.k8s.io/v1',
    kind: 'RuntimeClass',
    podSpecPath: null,
    podLabelsPath: null,
    selectorPath: null,
    sections: [
      metadataSection({ nameHelp: `The name a pod spec puts in its "runtimeClassName". ${DNS_NAME_HELP}` }),
      {
        id: 'runtime',
        title: 'Runtime',
        description:
          'A RuntimeClass has no `spec`: the fields below are top-level. A document that nests them under one is accepted by the API server with nothing set, and refused for the handler it then does not have.',
        fields: [
          {
            id: 'handler',
            label: 'Handler',
            control: 'text',
            path: ['handler'],
            required: true,
            help: 'The name the CRI runtime on the nodes is configured with — this is not a name you choose here. A pod naming a class whose handler no node has stays Pending.',
          },
          {
            id: 'overhead',
            label: 'Pod overhead',
            control: 'keyValue',
            path: ['overhead', 'podFixed'],
            subtree: true,
            help: 'What this runtime costs per pod, added to the pod’s effective requests for scheduling and charged against the namespace’s ResourceQuota — so a quota that fit before may not now.',
          },
          {
            id: 'runtimeNodeSelector',
            label: 'Node selector',
            control: 'keyValue',
            path: ['scheduling', 'nodeSelector'],
            subtree: true,
            help: 'Node labels a node must carry to run pods of this class. Admission merges it into each such pod’s own node selector, and a key the pod already sets to something else is refused there rather than merged.',
          },
          {
            id: 'tolerations',
            label: 'Tolerations',
            control: 'objectList',
            path: ['scheduling', 'tolerations'],
            rowNoun: 'Toleration',
            addLabel: 'Add a toleration',
            fields: [
              { key: 'key', label: 'Key', control: 'text' },
              {
                key: 'operator',
                label: 'Operator',
                control: 'select',
                options: [
                  { value: '', label: 'Not set — Equal' },
                  { value: 'Equal', label: 'Equal — the value must match' },
                  { value: 'Exists', label: 'Exists — any value, and the value box must be empty' },
                ],
              },
              { key: 'value', label: 'Value', control: 'text' },
              {
                key: 'effect',
                label: 'Effect',
                control: 'select',
                options: [
                  { value: '', label: 'Not set — every effect' },
                  { value: 'NoSchedule', label: 'NoSchedule' },
                  { value: 'PreferNoSchedule', label: 'PreferNoSchedule' },
                  { value: 'NoExecute', label: 'NoExecute' },
                ],
              },
              {
                key: 'tolerationSeconds',
                label: 'Toleration seconds',
                control: 'number',
                min: 0,
                help: 'Only meaningful with NoExecute, and refused with anything else: it is how long a running pod stays after the taint appears.',
              },
            ],
          },
        ],
      },
    ],
  },
];

/** The model for a document, or `null` when this console has no form for it. */
export function formModelFor(apiVersion, kind) {
  if (!apiVersion || !kind) return null;
  return FORM_MODELS.find((model) => model.apiVersion === apiVersion && model.kind === kind) ?? null;
}

/** Every field in a model, sections flattened. */
function modelFields(model) {
  return model.sections.flatMap((section) => section.fields);
}

/**
 * The paths in `document` that no control in `model` represents.
 *
 * This is the sentence OpenShift's form view does not say. It is computed from
 * the document and the model rather than written down, so it cannot go stale:
 * a field added to a manifest this console has never seen appears here on the
 * next render, and a control removed from a model puts its path back in the
 * list without anybody having to remember to.
 *
 * `apiVersion` and `kind` are excluded because they are not editable in the
 * form at all — they are what chose the model, and the header states both.
 */
export function unrepresented(document, model) {
  if (!model || !isMapping(document)) return [];
  const patterns = [
    // `apiVersion` and `kind` chose the model and are stated in the dialog's
    // header. `metadata.namespace` is represented too, just not by a control:
    // the alert above the form names the namespace this will be created in and
    // which of the two sources — the document or the masthead — won. Listing
    // any of the three as "not shown" would send an operator to YAML view to
    // find something already on screen.
    { path: ['apiVersion'] },
    { path: ['kind'] },
    { path: ['metadata', 'namespace'] },
    ...modelFields(model).flatMap((field) => {
      // A field whose value is the wrong shape for its control has an inert
      // control (see `CONTROL_SHAPES`), so it is not represented however many
      // patterns point at it. `metadata.labels` written as a list is the case
      // that matters: it is the classic paste error, and it is what sends
      // somebody to this dialog in the first place.
      const value = getIn(document, field.path);
      if (value != null && !shapeFor(field.control).ok(value)) return [];
      return coverageFor(field);
    }),
  ];

  const covered = (path) => patterns.some((pattern) => patternCovers(pattern.path, path, Boolean(pattern.subtree)));

  return leafPaths(document).filter((path) => {
    if (covered(path)) return false;
    // An empty block or list is a leaf with nothing in it to hide, and a
    // control that writes *inside* it owns it: a NetworkPolicy's
    // `podSelector: {}` is exactly what the pod-selector control shows as no
    // rows, and listing it as a field the form does not touch — on the very
    // first policy anybody creates — is wrong in both directions at once.
    const value = getIn(document, path);
    const isEmptyContainer =
      (isMapping(value) && Object.keys(value).length === 0) || (Array.isArray(value) && value.length === 0);
    if (isEmptyContainer && patterns.some((pattern) => startsWithPath(pattern.path, path))) return false;
    return true;
  });
}

/* ── Local checks ───────────────────────────────────────────────────────── */

/** RFC 1123 label — what a container name, a port name and a Namespace are held to. */
const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

/** RFC 1123 subdomain — what `metadata.name` is held to for most kinds. */
const DNS_SUBDOMAIN = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$/;

/**
 * The name rules a `metadata.name` is actually held to, by kind.
 *
 * Kubernetes does not have one. `DNS_SUBDOMAIN` is the common case and it is
 * wrong in both directions on the kinds this console now offers to create:
 *
 * - **Too loose for a Service.** A Service name is a DNS-1035 *label*: at most
 *   63 characters, no dots, and it must start with a letter, because it becomes
 *   a DNS label that a numeric first character makes unresolvable.
 * - **Too loose for a Namespace**, which is a DNS-1123 label — 63 characters
 *   and no dots.
 * - **Too strict for the four RBAC kinds**, which use path-segment names.
 *   `system:controller:example` and `example.com:read` are legal ClusterRole
 *   names that a subdomain regex calls errors — and an error this console
 *   invents about a document the API server would accept is worse than no check
 *   at all, because it is the one the operator believes.
 *
 * A model with no rule keeps the subdomain default.
 */
export const NAME_RULES = {
  dnsSubdomain: {
    limit: 253,
    test: (name) => DNS_SUBDOMAIN.test(name),
    describe: 'lowercase letters, digits, "-" and "." only, starting and ending with a letter or digit',
  },
  dnsLabel: {
    limit: 63,
    test: (name) => DNS_LABEL.test(name),
    describe: 'lowercase letters, digits and "-" only, starting and ending with a letter or digit, and no dots',
  },
  dns1035Label: {
    limit: 63,
    test: (name) => /^[a-z]([-a-z0-9]*[a-z0-9])?$/.test(name),
    describe: 'lowercase letters, digits and "-" only, starting with a letter — it becomes a DNS label',
  },
  pathSegment: {
    limit: 253,
    // What `IsValidPathSegmentName` allows: anything that can be one segment of
    // a URL path. RBAC names routinely use ":" and that is not a defect.
    test: (name) => name !== '.' && name !== '..' && !/[/%]/.test(name),
    describe: 'anything except ".", ".." and a name containing "/" or "%"',
  },
};

/**
 * Is this IntOrString zero?
 *
 * `0`, `"0"` and `"0%"` are the same number written three ways, and the
 * rollout rules that turn on it are refusals rather than warnings — so the
 * comparison cannot be `=== 0`.
 */
function isZeroQuantity(value) {
  if (value === undefined || value === null) return false;
  const text = String(value).trim();
  return text === '0' || text === '0%';
}

/**
 * A LabelSelector that selects everything.
 *
 * Both keys, mirroring `shaping.selector_is_empty`: a selector with only
 * `matchExpressions` is not empty, and reporting it as empty would describe a
 * narrow policy as a namespace-wide one.
 */
function selectorIsEmpty(selector) {
  if (!isMapping(selector)) return true;
  const labels = selector.matchLabels;
  const expressions = selector.matchExpressions;
  const hasLabels = isMapping(labels) && Object.keys(labels).length > 0;
  const hasExpressions = Array.isArray(expressions) && expressions.length > 0;
  return !hasLabels && !hasExpressions;
}

/**
 * Does one `matchExpressions` term hold for these labels?
 *
 * `null` means "this console does not know" — an operator this Kubernetes does
 * not have yet, or a malformed term — and a `null` never produces a finding. A
 * check that guessed here would refuse a manifest the API server accepts, which
 * on a screen whose errors are headed "the API server will refuse this" is the
 * expensive direction to be wrong in.
 *
 * The absent-key cases follow `apimachinery`'s own `Requirement.Matches`, where
 * `NotIn` and `DoesNotExist` are **satisfied** by an object that lacks the key
 * entirely. Reading them the other way is the classic misreading of this API.
 */
function expressionSatisfiedBy(expression, labels) {
  const key = expression?.key;
  if (typeof key !== 'string') return null;
  const present = isMapping(labels) && Object.prototype.hasOwnProperty.call(labels, key);
  const value = present ? String(labels[key]) : null;
  const values = Array.isArray(expression?.values) ? expression.values.map(String) : [];
  switch (expression?.operator) {
    case 'In':
      return present && values.includes(value);
    case 'NotIn':
      return !present || !values.includes(value);
    case 'Exists':
      return present;
    case 'DoesNotExist':
      return !present;
    default:
      return null;
  }
}

/** `a` is a sub-mapping of `b`, comparing values as the API server does. */
function isSubsetOf(a, b) {
  if (!isMapping(a)) return true;
  const other = isMapping(b) ? b : {};
  return Object.entries(a).every(([key, value]) => String(other[key]) === String(value));
}

/* ── Per-kind checks ────────────────────────────────────────────────────── */

/**
 * A quantity, as `resource.ParseQuantity` reads one.
 *
 * The suffix is the whole point. `10Gi` is 10 737 418 240 bytes and `10G` is
 * 10 000 000 000; `10gi` and `10 Gi` are not quantities at all and the API
 * server refuses the object for a field the operator will read as correct
 * because it looks like the thing next to it. Capital `K` is deliberately
 * absent: kilo is lowercase and `10K` is a refusal.
 */
const QUANTITY = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+|[numkMGTPE]|Ki|Mi|Gi|Ti|Pi|Ei)?$/;

function isQuantity(value) {
  if (typeof value === 'number') return Number.isFinite(value);
  return typeof value === 'string' && QUANTITY.test(value.trim());
}

/**
 * Could this string be base64?
 *
 * Deliberately the weaker question. Anything that decodes is accepted here,
 * because the expensive direction is a console telling an operator their
 * working Secret is malformed — what this catches is the paste that is plainly
 * not encoded at all (`change-me`, a PEM block), which the API server refuses
 * with an error naming no key. Whitespace is stripped rather than rejected:
 * a value wrapped across lines is somebody's editor, not their mistake.
 */
function looksBase64(value) {
  const compact = String(value).replace(/\s+/g, '');
  return compact.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(compact);
}

/** Own key only: a document is untrusted input and `constructor` is in every prototype. */
function hasKey(mapping, key) {
  return isMapping(mapping) && Object.prototype.hasOwnProperty.call(mapping, key);
}

/**
 * Three kinds here keep every field at the top level and have no `spec` at all.
 *
 * Writing one anyway is the single most common mistake with them, and it is
 * silent: the API server drops a field it does not recognise, so the document
 * is accepted and nothing under `spec:` is part of the object. A StorageClass
 * created that way is then refused for the provisioner it does not have, and a
 * PriorityClass is created successfully at priority 0 — which is the same
 * priority as naming no class at all.
 */
function topLevelSpecIssue(document, add, kind, fields) {
  if (getIn(document, ['spec']) === undefined) return;
  add(
    'warning',
    `A ${kind} has no \`spec\`: ${fields} are top-level fields. The API server drops a block it does not recognise rather than refusing it, so nothing written under \`spec:\` here reaches the object.`,
  );
}

function serviceIssues(document, add) {
  const spec = getIn(document, ['spec']);
  if (!isMapping(spec)) return;
  const type = typeof spec.type === 'string' && spec.type ? spec.type : 'ClusterIP';

  if (type === 'ExternalName') {
    if (!spec.externalName) {
      add(
        'error',
        'The type is ExternalName and `spec.externalName` is not set. That name is the entire content of such a Service — there is nothing for DNS to answer with — and the API server refuses it.',
      );
    }
  } else {
    if (spec.externalName) {
      add(
        'error',
        `\`spec.externalName\` is set on a ${type} Service. The API server takes it only on type ExternalName, and refuses it here rather than ignoring it.`,
      );
    }
    if (spec.selector === undefined) {
      add(
        'info',
        'No selector, which is legal and means the endpoints are managed by hand: nothing fills them in for you. That is what a Service standing in front of an address outside the cluster is for, and a mistake anywhere else — the object exists, reports no error, and routes nowhere.',
      );
    } else if (isMapping(spec.selector) && Object.keys(spec.selector).length === 0) {
      // Here — and only here — the empty block and the absent key are the same
      // statement. `ServiceSpec.Selector` is a plain map with `omitempty`, so
      // `{}` never reaches storage and reads back as no selector at all. The
      // opposite is true of the two `LabelSelector` fields this file also
      // checks (a PodDisruptionBudget's and a NetworkPolicy's), where empty
      // means "everything" and absent means "nothing" — and transplanting that
      // reasoning onto this field would tell an operator two documents differ
      // when they do not, about the one field that decides whether the Service
      // routes anywhere.
      add(
        'warning',
        '`spec.selector` is empty, and the API server drops an empty selector: this reads back as a Service with no selector at all, which means nothing fills in its addresses and they are yours to manage. Write the labels the pods actually have if that is not what was meant.',
      );
    }
  }

  const ports = Array.isArray(spec.ports) ? spec.ports : [];
  const names = [];
  ports.forEach((port, index) => {
    if (!isMapping(port)) return;
    const where = `Port ${index + 1}`;
    if (port.port === undefined) {
      add('error', `${where} has no \`port\`. That is the number clients connect to on the Service, and it is required.`);
    }
    if (ports.length > 1 && !port.name) {
      add(
        'error',
        `${where} has no name. Every port must be named as soon as a Service has more than one, and the API server refuses the object rather than numbering them for you.`,
      );
    }
    if (port.name) names.push(String(port.name));
    if (port.nodePort !== undefined && type === 'ClusterIP') {
      add(
        'error',
        `${where} sets a nodePort on a ClusterIP Service${spec.type ? '' : ' — the type is unset, which means ClusterIP'}. Node ports exist only on NodePort and LoadBalancer, and the API server refuses this pair.`,
      );
    }
  });
  for (const duplicate of new Set(names.filter((value, index) => names.indexOf(value) !== index))) {
    add('error', `Two ports are both named "${duplicate}". Port names must be unique within a Service.`);
  }
}

function configMapIssues(document, add) {
  const data = getIn(document, ['data']);
  const binaryData = getIn(document, ['binaryData']);
  const validKey = /^[-._a-zA-Z0-9]+$/;

  for (const [where, block] of [
    ['data', data],
    ['binaryData', binaryData],
  ]) {
    if (!isMapping(block)) continue;
    for (const [key, value] of Object.entries(block)) {
      if (!validKey.test(key)) {
        add('error', `"${key}" is not a valid ${where} key: letters, digits, "-", "_" and "." only.`);
      }
      if (where !== 'data') continue;
      if (value === null) {
        // Accepted, and decoded as the empty string — so this is a warning
        // about what was probably meant, not a claim that the write fails.
        add('warning', `data.${key} has no value. The API server reads that as the empty string; write "" if that is what you meant.`);
      } else if (typeof value !== 'string') {
        add(
          'error',
          `data.${key} is ${describeShape(value)}. Every value in a ConfigMap is a string — an unquoted number or a bare "true" is refused, and the error names no key — so quote it.`,
        );
      }
    }
  }

  if (isMapping(data) && isMapping(binaryData)) {
    for (const key of Object.keys(binaryData)) {
      if (hasKey(data, key)) {
        add('error', `"${key}" is in both \`data\` and \`binaryData\`. The API server refuses a ConfigMap whose two blocks share a key.`);
      }
    }
  }

  if (getIn(document, ['immutable']) === true) {
    add(
      'info',
      'This ConfigMap is immutable: once created it can only be deleted and recreated. Nothing that has already read it will see a change, which is the point of the field.',
    );
  }
}

/**
 * The keys each Secret type is refused without.
 *
 * `kubernetes.io/basic-auth` is deliberately absent: which of `username` and
 * `password` is required has moved between Kubernetes versions, and a check
 * that reports a working Secret as invalid is the one an operator believes.
 */
const SECRET_REQUIRED_KEYS = {
  'kubernetes.io/tls': ['tls.crt', 'tls.key'],
  'kubernetes.io/dockerconfigjson': ['.dockerconfigjson'],
  'kubernetes.io/dockercfg': ['.dockercfg'],
  'kubernetes.io/ssh-auth': ['ssh-privatekey'],
};

function secretIssues(document, add) {
  const data = getIn(document, ['data']);
  const stringData = getIn(document, ['stringData']);
  const type = getIn(document, ['type']) ?? 'Opaque';
  const present = new Set([
    ...(isMapping(data) ? Object.keys(data) : []),
    ...(isMapping(stringData) ? Object.keys(stringData) : []),
  ]);

  for (const key of SECRET_REQUIRED_KEYS[type] ?? []) {
    if (!present.has(key)) {
      add(
        'error',
        `A ${type} Secret must carry a "${key}" key, under \`data\` or \`stringData\`. The API server enforces the keys a type requires; it does not check that what is under them parses.`,
      );
    }
  }
  if (
    type === 'kubernetes.io/service-account-token' &&
    !getIn(document, ['metadata', 'annotations', 'kubernetes.io/service-account.name'])
  ) {
    add(
      'error',
      'A service-account-token Secret needs the `kubernetes.io/service-account.name` annotation naming the account it belongs to. The token controller fills in the token from it, and the API server refuses the Secret without it.',
    );
  }

  if (isMapping(data) && isMapping(stringData)) {
    for (const key of Object.keys(stringData)) {
      if (hasKey(data, key)) {
        add(
          'warning',
          `"${key}" is in both \`data\` and \`stringData\`. stringData wins and the data entry is discarded silently, so the value that reaches the cluster is the one in stringData whichever you meant.`,
        );
      }
    }
  }
  if (isMapping(data)) {
    for (const [key, value] of Object.entries(data)) {
      if (value !== null && typeof value !== 'string') {
        add('error', `data.${key} is ${describeShape(value)}. Values under \`data\` are base64 text — quote it.`);
      } else if (typeof value === 'string' && !looksBase64(value)) {
        add(
          'error',
          `data.${key} is not base64, and every value under \`data\` is decoded as base64. Put plain text under \`stringData\` and let the API server encode it — that is what the field is for.`,
        );
      }
    }
  }
  if (isMapping(stringData)) {
    for (const [key, value] of Object.entries(stringData)) {
      if (value !== null && typeof value !== 'string') {
        add('error', `stringData.${key} is ${describeShape(value)}. Values here are strings — quote it.`);
      }
    }
  }
}

function serviceAccountIssues(document, add) {
  const pullSecrets = getIn(document, ['imagePullSecrets']);
  for (const [index, entry] of (Array.isArray(pullSecrets) ? pullSecrets : []).entries()) {
    if (!isMapping(entry) || !entry.name) {
      add('error', `Image pull secret ${index + 1} has no name. Each entry is a reference to a Secret in this namespace and the name is required.`);
    }
  }
  if (getIn(document, ['automountServiceAccountToken']) === false) {
    add(
      'info',
      'No API token is mounted into pods running as this account, whatever their pod spec asks for. That is the right default for a workload that does not talk to Kubernetes, and it breaks any sidecar that does.',
    );
  }
}

/** The three levels Pod Security admission takes, and refuses a namespace without. */
const POD_SECURITY_LEVELS = ['privileged', 'baseline', 'restricted'];

function namespaceIssues(document, add) {
  const labels = getIn(document, ['metadata', 'labels']);
  if (!isMapping(labels)) return;
  for (const mode of ['enforce', 'audit', 'warn']) {
    const key = `pod-security.kubernetes.io/${mode}`;
    if (!hasKey(labels, key)) continue;
    const value = labels[key];
    // A `null` here is `enforce:` with nothing after it, which the label check
    // above already reports as the empty string it decodes to. Saying it twice
    // in two different vocabularies tells an operator counting errors that two
    // things are wrong with one line.
    if (value === null) continue;
    if (typeof value !== 'string' || !POD_SECURITY_LEVELS.includes(value)) {
      add(
        'error',
        `${key} is ${describeShape(value)}. Pod Security admission takes privileged, baseline or restricted, and refuses the namespace itself for anything else — so the namespace is not created at all.`,
      );
    }
  }
}

function claimIssues(document, add) {
  const spec = getIn(document, ['spec']);
  if (!isMapping(spec)) return;
  const modes = Array.isArray(spec.accessModes) ? spec.accessModes : [];
  if (modes.length === 0) {
    add('error', 'No `spec.accessModes`. A claim must say how the volume will be mounted, and the field is required.');
  }
  if (modes.includes('ReadWriteOncePod') && modes.length > 1) {
    add(
      'error',
      'ReadWriteOncePod is listed alongside another access mode. It means one pod and no other, so the API server refuses it in company.',
    );
  }
  const storage = getIn(document, ['spec', 'resources', 'requests', 'storage']);
  if (storage === undefined) {
    add(
      'error',
      'No `spec.resources.requests.storage`. A claim with no size is refused — there is nothing for a provisioner to create and nothing for a volume to match.',
    );
  } else if (!isQuantity(storage)) {
    add(
      'error',
      `"${storage}" is not a quantity the API server parses. Sizes are written like 10Gi (binary) or 10G (decimal) — no space, and the "i" suffix is case-sensitive.`,
    );
  }
  const source = getIn(document, ['spec', 'dataSource']);
  const sourceRef = getIn(document, ['spec', 'dataSourceRef']);
  if (isMapping(source) && isMapping(sourceRef)) {
    const differs = ['apiGroup', 'kind', 'name'].some((key) => String(source[key]) !== String(sourceRef[key]));
    if (differs) {
      add(
        'error',
        '`spec.dataSource` and `spec.dataSourceRef` are both set and do not agree. They are two spellings of one field and the API server refuses a claim whose two disagree — set one, and the newer one if you need a source in another namespace.',
      );
    }
  }
}

/** The six scopes a ResourceQuota knows, and what a scope selector may name. */
const QUOTA_SCOPES = [
  'Terminating',
  'NotTerminating',
  'BestEffort',
  'NotBestEffort',
  'PriorityClass',
  'CrossNamespacePodAffinity',
];

function resourceQuotaIssues(document, add) {
  const hard = getIn(document, ['spec', 'hard']);
  if (isMapping(hard)) {
    for (const [key, value] of Object.entries(hard)) {
      if (value === null || !isQuantity(value)) {
        add(
          'error',
          `spec.hard["${key}"] is ${value === null ? 'empty' : `"${value}"`}, which is not a quantity. Counts are written as strings ("20"), capacity as 8Gi or 500m — and the API server refuses the whole quota for one bad value.`,
        );
      }
    }
  }
  const scopes = Array.isArray(getIn(document, ['spec', 'scopes'])) ? getIn(document, ['spec', 'scopes']) : [];
  if (scopes.includes('BestEffort') && isMapping(hard)) {
    const capacity = Object.keys(hard).filter((key) => key.startsWith('requests.') || key.startsWith('limits.'));
    if (capacity.length > 0) {
      add(
        'error',
        `The BestEffort scope is listed alongside ${capacity[0]}. That scope selects exactly the pods that state no requests or limits, so a capacity key under it can never be consumed — and the API server refuses the combination rather than leaving it inert.`,
      );
    }
  }
  const terms = getIn(document, ['spec', 'scopeSelector', 'matchExpressions']);
  for (const [index, term] of (Array.isArray(terms) ? terms : []).entries()) {
    if (!isMapping(term)) continue;
    const where = `Scope term ${index + 1}`;
    const values = Array.isArray(term.values) ? term.values : [];
    if ((term.operator === 'In' || term.operator === 'NotIn') && values.length === 0) {
      add('error', `${where} uses ${term.operator} with no values. The API server requires at least one.`);
    }
    if ((term.operator === 'Exists' || term.operator === 'DoesNotExist') && values.length > 0) {
      add('error', `${where} uses ${term.operator} with values. Those two operators take none, and the API server refuses the pair.`);
    }
    if (term.scopeName !== undefined && !QUOTA_SCOPES.includes(term.scopeName)) {
      add('warning', `${where} names the scope "${term.scopeName}", which is not one this console knows. If the API server does not know it either, it refuses the quota.`);
    }
  }
}

function ingressIssues(document, add) {
  const spec = getIn(document, ['spec']);
  if (!isMapping(spec)) return;
  if (spec.ingressClassName === undefined) {
    add(
      'info',
      'No `spec.ingressClassName`. This then depends on the cluster having an IngressClass marked default — with none, nothing claims this Ingress and it routes nothing while looking created.',
    );
  }
  const rules = Array.isArray(spec.rules) ? spec.rules : [];
  rules.forEach((rule, ruleIndex) => {
    const paths =
      isMapping(rule) && isMapping(rule.http) && Array.isArray(rule.http.paths) ? rule.http.paths : [];
    paths.forEach((entry, pathIndex) => {
      if (!isMapping(entry)) return;
      const where = `rules[${ruleIndex}].http.paths[${pathIndex}]`;
      if (!entry.pathType) {
        add(
          'error',
          `${where} has no pathType. It is required in networking.k8s.io/v1 and has no default — Exact, Prefix or ImplementationSpecific — and the API server refuses a path without one.`,
        );
      }
      if (
        (entry.pathType === 'Exact' || entry.pathType === 'Prefix') &&
        (typeof entry.path !== 'string' || !entry.path.startsWith('/'))
      ) {
        add('error', `${where} needs an absolute path beginning with "/" for pathType ${entry.pathType}.`);
      }
      const backend = isMapping(entry.backend) ? entry.backend : null;
      if (!backend || (backend.service === undefined && backend.resource === undefined)) {
        add('error', `${where} has no backend. A path routes to a Service or to a resource, and one of the two is required.`);
      } else if (isMapping(backend.service)) {
        const port = backend.service.port;
        const hasNumber = isMapping(port) && port.number !== undefined;
        const hasName = isMapping(port) && port.name !== undefined;
        if (!hasNumber && !hasName) {
          add('error', `${where}.backend.service.port names neither a number nor a name; exactly one is required.`);
        } else if (hasNumber && hasName) {
          add('error', `${where}.backend.service.port carries both a number and a name; the API server takes exactly one.`);
        }
      }
    });
  });
}

function roleIssues(document, model, add) {
  const rules = getIn(document, ['rules']);
  for (const [index, rule] of (Array.isArray(rules) ? rules : []).entries()) {
    if (!isMapping(rule)) continue;
    const where = `Rule ${index + 1}`;
    const verbs = Array.isArray(rule.verbs) ? rule.verbs : [];
    const resources = Array.isArray(rule.resources) ? rule.resources : [];
    const groups = Array.isArray(rule.apiGroups) ? rule.apiGroups : [];
    const urls = Array.isArray(rule.nonResourceURLs) ? rule.nonResourceURLs : [];

    if (verbs.length === 0) {
      add('error', `${where} lists no verbs. A rule that grants nothing is refused rather than ignored.`);
    }
    if (urls.length > 0) {
      if (model.kind === 'Role') {
        add(
          'error',
          `${where} lists nonResourceURLs. Paths like /healthz are not objects in a namespace, so only a ClusterRole can grant them and the API server refuses a namespaced rule that names them.`,
        );
      }
      if (resources.length > 0 || groups.length > 0) {
        add('error', `${where} mixes nonResourceURLs with resources. One rule covers one or the other, never both.`);
      }
    } else {
      if (groups.length === 0) {
        add(
          'error',
          `${where} names no apiGroups, and a resource rule needs at least one. The core group is written as a single empty string, which is the entry that is easy to leave out and impossible to see.`,
        );
      }
      if (resources.length === 0) {
        add('error', `${where} names no resources, and a resource rule needs at least one.`);
      }
    }
    for (const resource of resources) {
      if (typeof resource === 'string' && /^[A-Z]/.test(resource)) {
        add(
          'warning',
          `${where} names the resource "${resource}". RBAC matches the plural lowercase name the API serves — "pods", not "Pod" — and a name it does not recognise grants nothing, with no error anywhere and a denial the operator cannot explain.`,
        );
      }
    }
    if (groups.includes('*') && resources.includes('*') && verbs.includes('*')) {
      add(
        'info',
        model.kind === 'Role'
          ? `${where} grants every verb on every resource in this namespace, including the RBAC objects in it — so a subject holding it can rewrite who else may act here.`
          : `${where} grants every verb on every resource in every API group. That is the rule cluster-admin is made of, RBAC itself included, and a subject bound to it cluster-wide can grant itself anything this role does not already cover.`,
      );
    }
  }
}

/** Subject names that are every request rather than a team. */
const EVERYBODY = {
  'system:authenticated': 'every request the API server has authenticated, including every ServiceAccount in every namespace',
  'system:unauthenticated': 'every request the API server could not authenticate',
  'system:serviceaccounts': 'every ServiceAccount in the cluster',
};

function bindingIssues(document, model, add) {
  const cluster = model.kind === 'ClusterRoleBinding';
  const ref = isMapping(getIn(document, ['roleRef'])) ? getIn(document, ['roleRef']) : {};

  if (!ref.name) {
    add(
      'error',
      '`roleRef.name` is not set. A binding names exactly one role, the field is required, and it is immutable afterwards — a binding created against the wrong name is deleted and recreated, never edited.',
    );
  }
  if (ref.apiGroup !== 'rbac.authorization.k8s.io') {
    add(
      'error',
      `\`roleRef.apiGroup\` must be rbac.authorization.k8s.io${ref.apiGroup ? `, not "${ref.apiGroup}"` : ', and this document leaves it out'}. The API server refuses anything else.`,
    );
  }
  if (!ref.kind) {
    add('error', `\`roleRef.kind\` is not set. It says which of the two role kinds this points at, and ${cluster ? 'a ClusterRoleBinding takes ClusterRole' : 'a RoleBinding takes Role or ClusterRole'}.`);
  } else if (cluster && ref.kind !== 'ClusterRole') {
    add(
      'error',
      `\`roleRef.kind\` is ${ref.kind}. A ClusterRoleBinding can only reference a ClusterRole — a namespaced Role means nothing cluster-wide — and the API server refuses it.`,
    );
  } else if (!cluster && ref.kind !== 'Role' && ref.kind !== 'ClusterRole') {
    add('error', `\`roleRef.kind\` is ${ref.kind}; a RoleBinding takes Role or ClusterRole.`);
  }
  if (ref.name === 'cluster-admin') {
    add(
      'info',
      cluster
        ? 'The role is cluster-admin: every verb on every resource in every namespace, including RBAC itself. A subject holding it can grant itself anything else and can remove the restrictions this console runs under.'
        : 'The role is cluster-admin, bound into one namespace: every verb on every resource in it, including the RBAC objects that decide who else may act here.',
    );
  }

  const subjects = getIn(document, ['subjects']);
  if (!Array.isArray(subjects) || subjects.length === 0) {
    add(
      'warning',
      'No subjects. That is legal and grants nothing — it is also exactly what a revoke leaves behind, so nothing distinguishes a binding somebody emptied on purpose from one that was never finished.',
    );
    return;
  }
  subjects.forEach((subject, index) => {
    if (!isMapping(subject)) return;
    const where = `Subject ${index + 1}`;
    if (!subject.name) add('error', `${where} has no name.`);
    if (subject.kind === 'ServiceAccount') {
      if (subject.apiGroup) {
        add(
          'error',
          `${where} is a ServiceAccount carrying an apiGroup. A ServiceAccount subject belongs to the core group, written as empty or left out, and the API server refuses any other value.`,
        );
      }
      if (!subject.namespace) {
        if (cluster) {
          add(
            'error',
            `${where} is a ServiceAccount with no namespace. A ClusterRoleBinding has none of its own for it to default to, so the API server refuses the subject.`,
          );
        } else {
          add(
            'info',
            `${where} is a ServiceAccount with no namespace, so it means the account of that name in this binding’s own namespace. An account from anywhere else has to be named here explicitly.`,
          );
        }
      }
    } else if (subject.kind === 'User' || subject.kind === 'Group') {
      if (subject.apiGroup !== 'rbac.authorization.k8s.io') {
        add(
          'error',
          `${where} is a ${subject.kind} whose apiGroup is ${subject.apiGroup ? `"${subject.apiGroup}"` : 'not set'}. Users and groups belong to rbac.authorization.k8s.io and the API server refuses the subject without it.`,
        );
      }
      if (EVERYBODY[subject.name]) {
        add('warning', `${where} names ${subject.name}, which is not a team: it is ${EVERYBODY[subject.name]}.`);
      }
    } else {
      add('error', `${where} has kind ${subject.kind ? `"${subject.kind}"` : 'unset'}; a subject is a User, a Group or a ServiceAccount.`);
    }
  });
}

function disruptionIssues(document, add) {
  const spec = getIn(document, ['spec']);
  if (!isMapping(spec)) return;
  const min = spec.minAvailable;
  const max = spec.maxUnavailable;
  if (min !== undefined && max !== undefined) {
    add(
      'error',
      'Both `spec.minAvailable` and `spec.maxUnavailable` are set. They state the same rule from opposite ends and the API server refuses to choose between them.',
    );
  }
  if (min === undefined && max === undefined) {
    add('warning', 'Neither min available nor max unavailable is set, so this budget names no floor and refuses no eviction. It is accepted, and it protects nothing.');
  }
  const selector = getIn(document, ['spec', 'selector']);
  if (selector === undefined) {
    add('warning', 'No selector. In policy/v1 that covers no pods at all, so this budget is created, reports nothing and protects nothing.');
  } else if (selectorIsEmpty(selector)) {
    add(
      'warning',
      'The selector is empty, which in policy/v1 covers EVERY pod in the namespace — the reverse of what the same YAML meant in the removed policy/v1beta1, and invisible in the document itself.',
    );
  }
  if (String(min).trim() === '100%' || isZeroQuantity(max)) {
    add(
      'warning',
      `${String(min).trim() === '100%' ? 'Min available is 100%' : 'Max unavailable is 0'}: no pod this covers may ever be voluntarily evicted. A drain of any node running one never finishes, and \`force\` does not defeat a budget — the API server enforces it on the eviction subresource.`,
    );
  }
}

function autoscalerIssues(document, add) {
  const spec = getIn(document, ['spec']);
  if (!isMapping(spec)) return;
  const ref = isMapping(spec.scaleTargetRef) ? spec.scaleTargetRef : {};
  if (!ref.kind || !ref.name) {
    add('error', '`spec.scaleTargetRef` needs both a kind and a name. Without them there is nothing for this autoscaler to point at, and the API server refuses it.');
  }
  if (ref.kind === 'DaemonSet') {
    // A warning and not an error, and the distinction is the whole point of the
    // heading these render under. `validateCrossVersionObjectReference` checks
    // that `kind` and `name` are non-empty path segments and nothing else, so
    // this object is created without complaint — and an error saying "the API
    // server will refuse this" beside a dry run that succeeds is this console
    // contradicting itself about somebody's cluster.
    add(
      'warning',
      'The target is a DaemonSet, which serves no scale subresource: it runs one pod per matching node, so there is no replica count for an autoscaler to change. The API server accepts this object; the autoscaler then reports FailedGetScale and scales nothing, which is a state nothing on this page would show you.',
    );
  }
  if (spec.maxReplicas === undefined) {
    add('error', '`spec.maxReplicas` is not set. It is required, and it is the ceiling that keeps a metric spike from becoming a cluster-wide one.');
  }
  if (typeof spec.maxReplicas === 'number' && typeof spec.minReplicas === 'number' && spec.maxReplicas < spec.minReplicas) {
    add('error', `Max replicas (${spec.maxReplicas}) is below min replicas (${spec.minReplicas}). The API server refuses that.`);
  }
  if (spec.minReplicas === 0) {
    add(
      'warning',
      'Min replicas is 0. That is refused unless the cluster runs with scaling to zero enabled — a fact about the cluster this document cannot settle, and the dry run can.',
    );
  }
  if (ref.name) {
    add(
      'info',
      `While this autoscaler exists, a manual scale of ${ref.name} is reverted on its next sync. The Scale action on the Workloads page says which autoscaler will do that, by name.`,
    );
  }
}

function storageClassIssues(document, add) {
  topLevelSpecIssue(document, add, 'StorageClass', '`provisioner`, `parameters`, `reclaimPolicy`, `volumeBindingMode`, `allowVolumeExpansion` and `mountOptions`');
  if (!getIn(document, ['provisioner'])) {
    add('error', 'No `provisioner`. It is required, it is immutable afterwards, and it is the whole of what a class does — which driver creates volumes for the claims that name it.');
  }
  const parameters = getIn(document, ['parameters']);
  if (isMapping(parameters)) {
    for (const [key, value] of Object.entries(parameters)) {
      if (value !== null && typeof value !== 'string') {
        add('error', `parameters.${key} is ${describeShape(value)}. Parameters are strings — quote it, including a number like "3000".`);
      }
    }
  }
  const reclaim = getIn(document, ['reclaimPolicy']);
  if (reclaim === undefined || reclaim === 'Delete') {
    add(
      'info',
      `The reclaim policy is Delete${reclaim === undefined ? ' by default, which is what an unset field means here' : ''}: deleting a claim bound to a volume from this class destroys the data in the storage system, not only the Kubernetes object.`,
    );
  }
}

function priorityClassIssues(document, add) {
  topLevelSpecIssue(document, add, 'PriorityClass', '`value`, `globalDefault`, `description` and `preemptionPolicy`');
  const value = getIn(document, ['value']);
  const name = getIn(document, ['metadata', 'name']);
  if (value === undefined) {
    add(
      'warning',
      'No `value`. The API server reads that as 0, which is the same priority a pod naming no class at all gets — so this class is created and changes nothing.',
    );
  } else if (typeof value === 'number' && value > 1000000000 && !(typeof name === 'string' && name.startsWith('system-'))) {
    add(
      'error',
      `The value is ${value}. A user-defined priority may not exceed 1000000000; above that is reserved for the built-in system classes, and the API server refuses it.`,
    );
  }
  if (getIn(document, ['globalDefault']) === true) {
    add(
      'warning',
      'This is the global default: every pod created cluster-wide afterwards that names no priority class gets this value. Pods already running are unaffected, and a cluster with two such classes has an ambiguity nothing here can resolve.',
    );
  }
}

function runtimeClassIssues(document, add) {
  topLevelSpecIssue(document, add, 'RuntimeClass', '`handler`, `overhead` and `scheduling`');
  if (!getIn(document, ['handler'])) {
    add(
      'error',
      'No `handler`. It is required, and it is not a name you choose here: it must match a handler the CRI runtime on the nodes is already configured with.',
    );
  }
  const overhead = getIn(document, ['overhead', 'podFixed']);
  if (isMapping(overhead)) {
    for (const [key, value] of Object.entries(overhead)) {
      if (!isQuantity(value)) {
        add('error', `overhead.podFixed.${key} is ${value === null ? 'empty' : `"${value}"`}, which is not a quantity. Write it like 250m or 120Mi.`);
      }
    }
  }
  const tolerations = getIn(document, ['scheduling', 'tolerations']);
  for (const [index, toleration] of (Array.isArray(tolerations) ? tolerations : []).entries()) {
    if (!isMapping(toleration)) continue;
    const where = `Toleration ${index + 1}`;
    if (toleration.operator === 'Exists' && toleration.value) {
      add('error', `${where} uses Exists with a value. Exists matches any value, so the API server refuses the pair rather than ignoring one of them.`);
    }
    if (toleration.tolerationSeconds !== undefined && toleration.effect !== 'NoExecute') {
      add(
        'error',
        `${where} sets tolerationSeconds with effect ${toleration.effect ? `"${toleration.effect}"` : 'unset'}. It is how long a running pod stays after a taint appears, so the API server accepts it only with NoExecute.`,
      );
    }
  }
}

/**
 * What can be said about this document without asking the cluster.
 *
 * Every entry is a **local** check and is rendered as one. None of them blocks
 * the preview, and that is the point: the dry run is the API server's own
 * verdict on the object, and a console that refused to ask for it because its
 * own rule said no would be substituting a copy of the rules — written here,
 * against one version of Kubernetes, without the admission webhooks this
 * cluster runs — for the authority. What these buy is the round trip and the
 * audit row that a manifest with `restartPolicy: Always` on a Job was never
 * going to earn.
 *
 * They are ordered by how much they cost to find out the hard way: a selector
 * that cannot match its own pods produces a workload that exists, reports no
 * error, and runs nothing.
 */
export function localIssues(document, model) {
  if (!model || !isMapping(document)) return [];
  const issues = [];
  const add = (severity, text) => issues.push({ severity, text });

  const name = getIn(document, ['metadata', 'name']);
  const generateName = getIn(document, ['metadata', 'generateName']);
  const nameRule = NAME_RULES[model.nameRule ?? 'dnsSubdomain'];
  // A `generateName` prefix is the other legal way to name an object, and
  // `app.admin.apply` accepts it — its own refusal says "give it a name, or set
  // generateName to let the API server pick one". Reporting it as nameless here
  // would be this console contradicting its own backend about a document that
  // applies cleanly.
  if (!name && !generateName) {
    add(
      'error',
      'No `metadata.name`. Every object needs one, or a `metadata.generateName` prefix for the API server to build one from.',
    );
  } else if (name && (typeof name !== 'string' || !nameRule.test(name))) {
    add('error', `"${name}" is not a valid name for a ${model.kind}: ${nameRule.describe}.`);
  } else if (name) {
    // A CronJob's limit is 52, not 253: the controller appends "-<minute>" to
    // build each Job's name and validation reserves the eleven characters for
    // it. Found otherwise only after a round trip and an audit row for a write
    // that was never possible.
    const limit = model.kind === 'CronJob' ? 52 : nameRule.limit;
    if (name.length > limit) {
      add('error', `The name is ${name.length} characters. The limit for a ${model.kind} is ${limit}.`);
    }
  }

  // A label value that parsed as a number or a boolean is the one mistake YAML
  // makes on the operator's behalf: `version: 1` is an integer, and the API
  // server refuses the whole object with an unmarshalling error that names no
  // field. The form's own editor only ever writes strings, so this is here for
  // what was pasted.
  // Deduplicated by path: on a Pod, `podLabelsPath` *is* `metadata.labels` —
  // the same field reached two ways — and reporting it twice tells an operator
  // counting errors before a create that a second field is wrong too.
  const labelPaths = new Map(
    [
      ['metadata', 'labels'],
      ['metadata', 'annotations'],
      model.podLabelsPath,
      model.selectorPath,
    ]
      .filter(Boolean)
      .map((path) => [formatPath(path), path]),
  );
  for (const [where, path] of labelPaths) {
    const mapping = getIn(document, path);
    if (!isMapping(mapping)) continue;
    for (const [key, value] of Object.entries(mapping)) {
      if (value === null) {
        // `a:` with nothing after it. The API server decodes a null into the
        // empty string and accepts it, so this is a warning about what was
        // probably meant rather than a claim that the write will fail.
        add(
          'warning',
          `${where}.${key} has no value. The API server reads that as the empty string, which is a legal label value — write "" if that is what you meant.`,
        );
      } else if (typeof value !== 'string') {
        add(
          'error',
          `${where}.${key} is ${typeof value === 'object' ? 'not a string' : `the ${typeof value} ${JSON.stringify(value)}`}. Label and annotation values must be strings — quote it.`,
        );
      }
    }
  }

  if (model.selectorPath) {
    // The form's control edits `matchLabels`, but the API server's rule is
    // about the whole LabelSelector: a selector carrying only
    // `matchExpressions` is legal and is accepted. Reading only the control's
    // own path would call that one "empty" — a flat, confident, false claim
    // about somebody's manifest, contradicted by the dry run on the same
    // screen. `selectorPath` is the control's path; its parent is the selector.
    const selectorRoot = model.selectorPath.slice(0, -1);
    const selector = getIn(document, selectorRoot);
    const matchLabels = getIn(document, model.selectorPath);
    const podLabels = getIn(document, model.podLabelsPath);
    if (selectorIsEmpty(selector)) {
      add('error', 'The selector is empty. A workload selector must match at least one label.');
    } else {
      if (isMapping(matchLabels) && !isSubsetOf(matchLabels, podLabels)) {
        add(
          'error',
          'The selector does not match the pod labels. The API server refuses a workload whose selector cannot select its own pod template — nothing is created, so this is not a state you can end up running in.',
        );
      }
      for (const expression of Array.isArray(selector?.matchExpressions) ? selector.matchExpressions : []) {
        if (expressionSatisfiedBy(expression, podLabels) === false) {
          add(
            'error',
            `The selector term "${expression?.key ?? '?'} ${expression?.operator ?? '?'}" is not satisfied by the pod labels, so the selector cannot select its own pod template and the API server refuses the object.`,
          );
        }
      }
    }
  }

  if (model.podSpecPath) {
    const containers = getIn(document, [...model.podSpecPath, 'containers']);
    if (!Array.isArray(containers) || containers.length === 0) {
      add('error', 'No containers. A pod needs at least one.');
    } else {
      const names = [];
      const portNames = [];
      containers.forEach((container, index) => {
        const portNamesHere = [];
        const containerName = isMapping(container) ? container.name : null;
        const image = isMapping(container) ? container.image : null;
        if (!containerName) add('error', `Container ${index + 1} has no name.`);
        else {
          names.push(containerName);
          // A container name is a DNS *label*, not the subdomain a resource
          // name is: no dots, and 63 characters rather than 253.
          if (!DNS_LABEL.test(containerName) || containerName.length > 63) {
            add(
              'error',
              `"${containerName}" is not a valid container name: lowercase letters, digits and "-" only, no dots, at most 63 characters.`,
            );
          }
        }
        if (!image) add('error', `Container ${containerName || index + 1} has no image.`);
        for (const [portIndex, port] of (Array.isArray(container?.ports) ? container.ports : []).entries()) {
          const number = isMapping(port) ? port.containerPort : null;
          if (typeof number !== 'number' || number < 1 || number > 65535) {
            add(
              'error',
              `Port ${portIndex + 1} on container ${containerName || index + 1} needs a containerPort between 1 and 65535.`,
            );
          }
          if (isMapping(port) && port.name) {
            portNames.push(String(port.name));
            portNamesHere.push(String(port.name));
          }
        }
        for (const duplicate of new Set(
          portNamesHere.filter((value, i) => portNamesHere.indexOf(value) !== i),
        )) {
          add(
            'error',
            `Container ${containerName || index + 1} has two ports named "${duplicate}". The API server checks port names within a container.`,
          );
        }
      });
      const duplicates = names.filter((value, index) => names.indexOf(value) !== index);
      for (const duplicate of new Set(duplicates)) {
        add('error', `Two containers are both named "${duplicate}". Names must be unique within a pod.`);
      }
      // Across containers this is a hazard rather than a refusal, and the two
      // are not interchangeable. The API server checks port names *within* a
      // container, so it accepts the same name twice in one pod — but a Service
      // `targetPort` or a NetworkPolicy port referring to that name resolves
      // against the pod and has no way to say which container was meant.
      // Reporting it as a refusal would be this console contradicting the dry
      // run on the same screen.
      const duplicatePorts = portNames.filter((value, index) => portNames.indexOf(value) !== index);
      for (const duplicate of new Set(duplicatePorts)) {
        add(
          'warning',
          `Two containers in this pod both have a port named "${duplicate}". The API server accepts that; a Service or NetworkPolicy naming that port does not get to say which container it meant.`,
        );
      }
    }

    const restartPolicy = getIn(document, [...model.podSpecPath, 'restartPolicy']);
    const isBatch = model.kind === 'Job' || model.kind === 'CronJob';
    // Absent is not safe here, which is why this is checked separately from the
    // wrong-value case below: the API server defaults an unset `restartPolicy`
    // to Always and *then* refuses the object for it, so a manifest that never
    // mentions the field fails for a value nobody wrote.
    if (isBatch && !restartPolicy) {
      add(
        'error',
        `A ${model.kind}'s pods have no restartPolicy. The API server fills in "Always" and then refuses the object for it — set Never or OnFailure.`,
      );
    }
    if (isBatch && restartPolicy && restartPolicy !== 'Never' && restartPolicy !== 'OnFailure') {
      add(
        'error',
        `A ${model.kind}'s pods must have restartPolicy Never or OnFailure; this one says ${restartPolicy}. "Always" would mean a pod that can never complete, so the API server rejects it.`,
      );
    }
    if (!isBatch && model.kind !== 'Pod' && restartPolicy && restartPolicy !== 'Always') {
      add(
        'error',
        `A ${model.kind}'s pod template must have restartPolicy Always; this one says ${restartPolicy}.`,
      );
    }
  }

  if (model.kind === 'StatefulSet' && !getIn(document, ['spec', 'serviceName'])) {
    // A warning, not a refusal: recent Kubernetes accepts a StatefulSet with no
    // `serviceName`, older versions reject it, and this console cannot tell
    // which it is talking to from the document. What is true either way is what
    // the operator actually needs to know.
    add(
      'warning',
      'No governing service. `spec.serviceName` names the headless Service that gives each pod its DNS name — the pods get stable names either way, and nothing resolves them until that Service exists. Older Kubernetes versions refuse a StatefulSet without it outright.',
    );
  }

  if (model.kind === 'Deployment') {
    const type = getIn(document, ['spec', 'strategy', 'type']);
    const rolling = getIn(document, ['spec', 'strategy', 'rollingUpdate']);
    if (type === 'Recreate' && isMapping(rolling) && Object.keys(rolling).length > 0) {
      add(
        'error',
        '`spec.strategy.rollingUpdate` is set alongside the Recreate strategy. The API server rejects that combination — clear max unavailable and max surge, or switch the strategy back to RollingUpdate.',
      );
    }
    if (type !== 'Recreate' && isZeroQuantity(rolling?.maxUnavailable) && isZeroQuantity(rolling?.maxSurge)) {
      add(
        'error',
        'Max unavailable and max surge are both 0. That is a rollout with no way to start — it may neither take a pod down nor add one — and the API server refuses it.',
      );
    }
  }

  if (model.kind === 'DaemonSet') {
    // A DaemonSet is the one kind where both directions are refused: exactly
    // one of the two may be non-zero. The defaults matter to the arithmetic —
    // an absent maxUnavailable is 1 and an absent maxSurge is 0 — so a document
    // that sets only maxSurge is rejected for a maxUnavailable nobody wrote.
    const type = getIn(document, ['spec', 'updateStrategy', 'type']);
    const rolling = getIn(document, ['spec', 'updateStrategy', 'rollingUpdate']);
    if (type !== 'OnDelete') {
      const unavailable = rolling?.maxUnavailable === undefined ? 1 : rolling.maxUnavailable;
      const surge = rolling?.maxSurge === undefined ? 0 : rolling.maxSurge;
      if (isZeroQuantity(unavailable) && isZeroQuantity(surge)) {
        add('error', 'Max unavailable and max surge are both 0. A DaemonSet rollout needs one of them above zero.');
      } else if (!isZeroQuantity(unavailable) && !isZeroQuantity(surge)) {
        add(
          'error',
          `Max unavailable (${unavailable}) and max surge (${surge}) are both non-zero. A DaemonSet accepts exactly one of them${rolling?.maxUnavailable === undefined ? ' — and max unavailable defaults to 1, so setting max surge alone means setting it to 0 explicitly' : ''}.`,
        );
      }
    }
  }

  if (model.kind === 'StatefulSet' || model.kind === 'DaemonSet') {
    const type = getIn(document, ['spec', 'updateStrategy', 'type']);
    const rolling = getIn(document, ['spec', 'updateStrategy', 'rollingUpdate']);
    if (type === 'OnDelete' && isMapping(rolling) && Object.keys(rolling).length > 0) {
      // The same shape, two different answers, and they are not interchangeable:
      // StatefulSet validation forbids `rollingUpdate` under OnDelete, DaemonSet
      // validation says nothing about it and the controller ignores it.
      if (model.kind === 'StatefulSet') {
        add(
          'error',
          '`spec.updateStrategy.rollingUpdate` is set alongside the OnDelete strategy. A StatefulSet only accepts it with RollingUpdate.',
        );
      } else {
        add(
          'warning',
          '`spec.updateStrategy.rollingUpdate` is set alongside the OnDelete strategy. A DaemonSet accepts it and then ignores it — nothing rolls until a pod is deleted by hand.',
        );
      }
    }
  }

  if (model.kind === 'CronJob') {
    const schedule = getIn(document, ['spec', 'schedule']);
    if (!schedule) {
      add('error', 'No schedule. `spec.schedule` is required on a CronJob.');
    } else if (typeof schedule === 'string' && !schedule.startsWith('@')) {
      const fields = schedule.trim().split(/\s+/).length;
      if (fields !== 5) {
        add(
          'warning',
          `The schedule has ${fields} fields; a cron expression has five. The API server validates this and will say so.`,
        );
      }
    }
    // The schedule's own zone prefix. Which of the two sentences applies is a
    // fact about the cluster's version, not about the document — the prefix was
    // deprecated and is refused outright by recent Kubernetes, while every
    // version has refused it alongside `spec.timeZone`. Only the second is
    // stated as a refusal, because a console asserting a version rule it cannot
    // check would eventually be telling somebody their working manifest is
    // invalid.
    if (typeof schedule === 'string' && /^\s*(TZ|CRON_TZ)=/.test(schedule)) {
      if (getIn(document, ['spec', 'timeZone'])) {
        add(
          'error',
          'The schedule carries its own TZ=/CRON_TZ= prefix and `spec.timeZone` is set as well. No version of Kubernetes accepts both.',
        );
      } else {
        add(
          'warning',
          'The schedule carries a TZ=/CRON_TZ= prefix. That spelling is deprecated and recent Kubernetes refuses it on create — `spec.timeZone` is the field for it.',
        );
      }
    }
  }

  if (model.kind === 'NetworkPolicy') {
    const types = getIn(document, ['spec', 'policyTypes']);
    const list = Array.isArray(types) ? types : [];
    if (types != null && !Array.isArray(types)) {
      add('error', '`spec.policyTypes` is a single value; the API server expects a list of them.');
    }
    // Both halves of the selector, the way `shaping.selector_is_empty` reads
    // it. A policy carrying only `matchExpressions` is narrow, and calling it
    // "every pod in the namespace" is the wrong answer in the expensive
    // direction on the kind where it costs the most.
    if (selectorIsEmpty(getIn(document, ['spec', 'podSelector']))) {
      add('info', 'The pod selector is empty, so this policy applies to every pod in the namespace.');
    }
    if (types == null) {
      add(
        'warning',
        'No policy types are listed. The API server then infers them from the rules present, and sets Ingress regardless — so an empty policy is a deny-all-inbound policy, not an inert one.',
      );
    }
    const ingress = getIn(document, ['spec', 'ingress']);
    const egress = getIn(document, ['spec', 'egress']);
    if (list.includes('Ingress') && (!Array.isArray(ingress) || ingress.length === 0)) {
      add('info', 'Ingress is listed with no ingress rules, which denies all inbound traffic to the selected pods.');
    }
    if (list.includes('Egress') && (!Array.isArray(egress) || egress.length === 0)) {
      add(
        'warning',
        'Egress is listed with no egress rules, which denies all outbound traffic from the selected pods — including DNS, which is what usually makes this look like an application failure rather than a policy.',
      );
    }
  }

  // The kinds that carry no pod template, each with the handful of things its
  // own document decides. They are separate functions rather than more `if`
  // blocks here for one reason: every one of them is a claim about a
  // Kubernetes API, and a claim is easier to check when it is next to the
  // others about the same kind than when it is halfway down a function about
  // fifteen.
  if (model.kind === 'Service') serviceIssues(document, add);
  if (model.kind === 'ConfigMap') configMapIssues(document, add);
  if (model.kind === 'Secret') secretIssues(document, add);
  if (model.kind === 'ServiceAccount') serviceAccountIssues(document, add);
  if (model.kind === 'Namespace') namespaceIssues(document, add);
  if (model.kind === 'PersistentVolumeClaim') claimIssues(document, add);
  if (model.kind === 'ResourceQuota') resourceQuotaIssues(document, add);
  if (model.kind === 'Ingress') ingressIssues(document, add);
  if (model.kind === 'Role' || model.kind === 'ClusterRole') roleIssues(document, model, add);
  if (model.kind === 'RoleBinding' || model.kind === 'ClusterRoleBinding') bindingIssues(document, model, add);
  if (model.kind === 'PodDisruptionBudget') disruptionIssues(document, add);
  if (model.kind === 'HorizontalPodAutoscaler') autoscalerIssues(document, add);
  if (model.kind === 'StorageClass') storageClassIssues(document, add);
  if (model.kind === 'PriorityClass') priorityClassIssues(document, add);
  if (model.kind === 'RuntimeClass') runtimeClassIssues(document, add);

  return issues;
}
