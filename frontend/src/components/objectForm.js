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

/** Is `prefix` a strict prefix of `pattern`? */
function startsWithPath(pattern, prefix) {
  return pattern.length > prefix.length && prefix.every((segment, i) => pattern[i] === segment);
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
            help: 'The headless Service that gives each pod its DNS name. This console does not create it — the pods get stable names either way, and nothing resolves them until that Service exists. Recent Kubernetes accepts a StatefulSet without one; older versions refuse it outright.',
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
      return field.coverage ?? [{ path: field.path, subtree: Boolean(field.subtree) }];
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

/** RFC 1123 subdomain — what `metadata.name` is held to for these kinds. */
const DNS_SUBDOMAIN = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$/;

/** RFC 1123 label — what a container name and a port name are held to. */
const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

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
  } else if (name && (typeof name !== 'string' || !DNS_SUBDOMAIN.test(name))) {
    add(
      'error',
      `"${name}" is not a valid name here: lowercase letters, digits, "-" and "." only, starting and ending with a letter or digit.`,
    );
  } else if (name) {
    // A CronJob's limit is 52, not 253: the controller appends "-<minute>" to
    // build each Job's name and validation reserves the eleven characters for
    // it. Found otherwise only after a round trip and an audit row for a write
    // that was never possible.
    const limit = model.kind === 'CronJob' ? 52 : 253;
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

  return issues;
}
