/**
 * clusterYaml — YAML, read the way this console sends it.
 *
 * **This module is the browser's only YAML.** Every parse of a manifest in this
 * app goes through `load`/`loadAll` here, and every serialisation of one goes
 * through `toYaml`. Nothing else may call js-yaml's own `load` or `dump` on a
 * document that is on its way to a cluster.
 *
 * ## Why there is a module for this at all
 *
 * `app.admin.apply.parse_document` reads the submitted manifest through
 * `app/yaml_dialect.py`, whose implicit resolvers are YAML 1.1's, and sends the
 * result to the API server as JSON. js-yaml's default schema is YAML 1.2's core
 * schema. For a handful of unquoted scalars those two disagree:
 *
 * ```
 *   enabled: off      YAML 1.2: the text "off"     sent: the boolean false
 *   mode: 0755        YAML 1.2: the number 755     sent: the number 493
 *   port: 8:30        YAML 1.2: the text "8:30"    sent: the number 510
 *   verbose: y        YAML 1.2: the text "y"       sent: the boolean true
 * ```
 *
 * Until ADR-0009 the browser used the left-hand column and the backend the
 * right-hand one, and the console warned where they differed. That warning was
 * true and it was not a fix: the form view's controls, `unrepresented()` and
 * every check in `localIssues()` are lenses onto the browser's reading, so on
 * those documents the console was reasoning about an object it was not going to
 * send. ADR-0009 makes the browser read the same document the backend does. The
 * schema below is how.
 *
 * **The reading that won is the backend's, and not because it is better.** It is
 * the one already being sent, so adopting it changes what no manifest means;
 * adopting js-yaml's would have turned `defaultMode: 0755` from `493` into
 * `755` on every manifest this console had ever accepted, silently, in the
 * direction of a file mode nobody wrote. It is also the reading `kubectl` sends
 * for that scalar, and since ADR-0010 for `y` and `n` as well — see ADR-0009 for
 * the seven scalars where the two still part company.
 *
 * ## Why a schema rather than a pattern over the source
 *
 * `yamlSyntax.js` has a tokenizer that could find the scalars, and says in its
 * own docstring why it is not allowed to decide anything: *"A regex-shaped
 * approximation of YAML that fed a gate would be the defect standard with syntax
 * colours on."* A pattern cannot tell a plain `off` from an `off` inside a `|`
 * block, from a quoted `"off"`, from a continuation line of a multi-line plain
 * scalar. A parser can, because that is what a parser is. So the change of
 * reading is made where a parser makes it — in the resolvers — and everything
 * downstream is ordinary js-yaml.
 *
 * ## What is transcribed, and what holds the transcription honest
 *
 * Only the three scalar resolvers below. The integer and float ones are copied
 * from PyYAML's own `yaml/resolver.py`; the boolean one is PyYAML's **plus the
 * four scalars `y`, `Y`, `n` and `N`**, which PyYAML declines and `kubectl`
 * reads as booleans — ADR-0010, and `backend/app/yaml_dialect.py` is the half of
 * that decision this file mirrors. The dump halves of the same three types are
 * js-yaml's own, borrowed unchanged from `yaml.types`, so nothing about how a
 * value is *written* is transcribed — only which strings the backend calls
 * numbers and booleans.
 *
 * A transcription is a claim about another parser made from inside a language
 * that cannot call it, so it is pinned from both sides over one corpus:
 * `backend/tests/data/yaml_scalar_corpus.json`.
 * `backend/tests/test_yaml_scalar_reading.py` runs every row through the real
 * backend by way of the real `parse_document`; `frontend/tests/e2e/
 * cluster-yaml.spec.js` runs the same rows through this schema. A PyYAML
 * release that moves a resolver fails a test rather than quietly making this
 * console read a document one way and write it another again.
 */
import yaml from 'js-yaml';

import { formatPath } from './objectFormModel';

/* ── The backend's implicit resolvers ───────────────────────────────────── */

/*
 * `SCALAR_INT` and `SCALAR_FLOAT` are transcribed from PyYAML's
 * `yaml/resolver.py`, verbatim modulo the `re.X` whitespace. `SCALAR_BOOL` is
 * that file's boolean **plus `y`, `Y`, `n` and `N`** — the four `app/
 * yaml_dialect.py` adds, because PyYAML registers its boolean resolver under
 * exactly those first characters and then excludes them from the pattern, and
 * `kubectl` does not (ADR-0010).
 *
 * They are the whole of what this module claims to know about the other side,
 * and they are checked against it by the backend test named in the docstring
 * above. The single letters only: `yy` and `Ye` are ordinary strings to every
 * parser in this system.
 */
const SCALAR_BOOL =
  /^(?:y|Y|n|N|yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/;

const SCALAR_INT =
  /^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/;

const SCALAR_FLOAT =
  /^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/;

const SCALAR_TRUE = /^(?:y|Y|yes|Yes|YES|true|True|TRUE|on|On|ON)$/;

/** What the backend makes of a scalar its integer resolver matched. */
function pyInt(raw) {
  const text = raw.replace(/_/g, '');
  const negative = text[0] === '-';
  const body = text.replace(/^[-+]/, '');
  let value;
  if (/^0b/.test(body)) value = parseInt(body.slice(2), 2);
  else if (/^0x/.test(body)) value = parseInt(body.slice(2), 16);
  // Sexagesimal: `8:30` is 8*60 + 30. YAML 1.1 kept it for durations and
  // Kubernetes has no field that wants one, which is exactly why nobody writing
  // `8:30` in a manifest means 510. It stays because PyYAML has it and this
  // schema's job is to say what PyYAML does, not what it should do — ADR-0009
  // records why removing it is a different decision from this one.
  else if (body.includes(':')) value = body.split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
  // A leading zero is octal, and `0` on its own is not: `[-+]?0[0-7_]+` needs a
  // digit after it.
  else if (/^0[0-7]+$/.test(body)) value = parseInt(body.slice(1), 8);
  else value = Number(body);
  // `-0` is `0`, not `-0`. Python has one zero and JavaScript has two, and
  // `Object.is` — which `sameScalar` compares with, so that two NaNs count as
  // one reading — is the one comparison that can tell them apart. Without this
  // line `replicas: -0` is reported as an ambiguous scalar, which is the false
  // positive that teaches people to ignore the true ones.
  if (value === 0) return 0;
  return negative ? -value : value;
}

/** What the backend makes of a scalar its float resolver matched. */
function pyFloat(raw) {
  const text = raw.replace(/_/g, '');
  if (/^[-+]?\.(?:inf|Inf|INF)$/.test(text)) return text[0] === '-' ? -Infinity : Infinity;
  if (/^\.(?:nan|NaN|NAN)$/.test(text)) return NaN;
  if (text.includes(':')) {
    const negative = text[0] === '-';
    const value = text
      .replace(/^[-+]/, '')
      .split(':')
      .reduce((acc, part) => acc * 60 + Number(part), 0);
    return negative ? -value : value;
  }
  return Number(text);
}

/**
 * The half of a js-yaml type that decides how a value is *written*.
 *
 * Borrowed from the built-in rather than transcribed, because only the reading
 * differs between the two parsers and a hand-written `represent` would be a
 * second thing that could drift. It also fails loudly if omitted: replacing a
 * type by tag replaces both halves, and a schema with no `predicate` for
 * booleans throws `unacceptable kind of an object to dump [object Boolean]` the
 * first time the form view serialises a document.
 *
 * `styleAliases` is deliberately not carried across. It is already compiled on a
 * constructed type and the constructor rejects the compiled form; nothing here
 * passes `styles`, so `defaultStyle` is the only one that is ever consulted.
 */
const writerHalfOf = (builtin) => ({
  instanceOf: builtin.instanceOf,
  predicate: builtin.predicate,
  represent: builtin.represent,
  representName: builtin.representName,
  defaultStyle: builtin.defaultStyle,
});

/**
 * js-yaml, reading and writing plain scalars the way the API server is sent
 * them.
 *
 * `extend({ implicit: [...] })` and not `extend([...])`: the array form adds
 * *explicit* types, which only apply to a scalar carrying a `!!tag`, and the
 * schema would then behave identically to the default one — every document
 * would parse the old way and every test would pass for the wrong reason. Only
 * the three scalar resolvers are replaced; everything else, timestamps
 * included, is js-yaml's own and matches PyYAML already.
 */
export const CLUSTER_SCHEMA = yaml.DEFAULT_SCHEMA.extend({
  implicit: [
    new yaml.Type('tag:yaml.org,2002:bool', {
      ...writerHalfOf(yaml.types.bool),
      kind: 'scalar',
      resolve: (text) => SCALAR_BOOL.test(text),
      construct: (text) => SCALAR_TRUE.test(text),
    }),
    new yaml.Type('tag:yaml.org,2002:int', {
      ...writerHalfOf(yaml.types.int),
      kind: 'scalar',
      resolve: (text) => SCALAR_INT.test(text),
      construct: pyInt,
    }),
    new yaml.Type('tag:yaml.org,2002:float', {
      ...writerHalfOf(yaml.types.float),
      kind: 'scalar',
      resolve: (text) => SCALAR_FLOAT.test(text),
      construct: pyFloat,
    }),
  ],
});

/* ── Reading and writing ────────────────────────────────────────────────── */

/** One document, read as the API server will be sent it. */
export function load(text) {
  return yaml.load(text, { schema: CLUSTER_SCHEMA });
}

/**
 * Every document in the stream, read as the API server will be sent them.
 *
 * `loadAll`, not `load`, at the call sites that validate: `load` throws on a
 * multi-document stream with a message about the second `---` that reads like a
 * syntax error, when what the operator actually did was paste a
 * `kubectl get -o yaml` of two objects. Counting them lets the editor say that
 * instead.
 */
export function loadAll(text) {
  return yaml.loadAll(text, null, { schema: CLUSTER_SCHEMA });
}

/**
 * Serialise a form edit back into the editor's text.
 *
 * Dumped against the same schema it was parsed with, which is what makes a form
 * edit round-trip: js-yaml quotes a string it would otherwise re-read as
 * something else, so the string `"1_000"` comes back as `"1_000"` rather than
 * as a bare `1_000` the backend would send as the number 1000. Dumping with
 * js-yaml's default schema instead gets that one wrong in the quiet direction —
 * a valid document, a changed value, and no diff line to say a form edit
 * touched a field nobody opened.
 *
 * `lineWidth: -1` is the one option here that is not cosmetic. js-yaml folds
 * long scalars at 80 columns by default, which turns
 * `image: registry.example.com/team/service@sha256:…` into two lines joined by
 * a newline the parser puts back as a space — a valid document holding a
 * different image reference. `noRefs` is the same class of surprise: a document
 * with the same mapping in two places would come back with a `&a1` anchor and a
 * `*a1` alias, which is correct YAML and unreadable to somebody reviewing a
 * diff before applying it to production.
 */
export function toYaml(document) {
  return yaml.dump(document ?? {}, {
    schema: CLUSTER_SCHEMA,
    indent: 2,
    lineWidth: -1,
    noRefs: true,
    sortKeys: false,
    quotingType: '"',
  });
}

/* ── Scalars that do not mean what they look like ───────────────────────── */

function isMapping(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value) && !(value instanceof Date);
}

/** Do these two leaves mean the same thing? */
function sameScalar(a, b) {
  if (a instanceof Date || b instanceof Date) {
    return a instanceof Date && b instanceof Date && a.getTime() === b.getTime();
  }
  if (typeof a !== typeof b) return false;
  // `Object.is` rather than `===` for the one pair of numbers that are equal to
  // nothing, themselves included: two NaNs are the same reading.
  if (typeof a === 'number') return Object.is(a, b);
  return a === b;
}

/** A value as an operator would want it named in a sentence. */
export function describeReading(value) {
  if (value === null || value === undefined) return 'null';
  if (value instanceof Date) return `the timestamp ${value.toISOString()}`;
  if (typeof value === 'string') return `the text "${value}"`;
  if (typeof value === 'boolean') return `the boolean ${value}`;
  if (typeof value === 'number') return `the number ${value}`;
  return 'a block';
}

/**
 * `ancestors` is the same guard `leafPaths` carries, for the same document.
 *
 * A YAML anchor can refer to the node that contains it, and js-yaml resolves
 * that into a genuinely cyclic object rather than refusing it — so this walk
 * would never return. It runs inside a render, so what an operator loses is not
 * a warning: it is the dialog and everything they had typed into it, to an
 * error boundary that can name neither the document nor the anchor. Ancestors
 * rather than a global seen-set, because the same node legitimately appearing
 * twice under different paths is an anchor used twice, and skipping its second
 * appearance would under-report a document that really is ambiguous there.
 */
function walk(plain, sent, path, found, ancestors = new Set()) {
  if (plain && typeof plain === 'object') {
    if (ancestors.has(plain)) return;
    ancestors = new Set(ancestors).add(plain);
  }
  if (Array.isArray(plain) && Array.isArray(sent) && plain.length === sent.length) {
    plain.forEach((item, index) => walk(item, sent[index], [...path, index], found, ancestors));
    return;
  }
  if (isMapping(plain) && isMapping(sent)) {
    const plainKeys = Object.keys(plain);
    const sentKeys = Object.keys(sent);
    // A key is a scalar too, and `off: value` is a key that reaches a ConfigMap
    // as a data key called "false". Reported once for the block rather than per
    // key: the object is already the wrong shape and walking into it would name
    // the same fact several times.
    const differing = plainKeys.find((key) => !sentKeys.includes(key));
    if (differing !== undefined) {
      found.push({
        path: [...path, differing],
        looksLike: differing,
        sent: sentKeys.find((key) => !plainKeys.includes(key)) ?? null,
        isKey: true,
      });
      return;
    }
    plainKeys.forEach((key) => walk(plain[key], sent[key], [...path, key], found, ancestors));
    return;
  }
  if (isMapping(plain) !== isMapping(sent) || Array.isArray(plain) !== Array.isArray(sent)) {
    // Only scalar resolution differs between the two schemas, so a shape that
    // differs is something this module did not predict. Saying nothing is the
    // conservative answer: a warning nobody can act on is worse than none.
    return;
  }
  if (!sameScalar(plain, sent)) {
    found.push({ path, looksLike: plain, sent, isKey: false });
  }
}

/**
 * Every scalar in `text` that this console sends as something other than a
 * YAML 1.2 reader would read it.
 *
 * The console no longer disagrees with itself about these — ADR-0009 — and this
 * still has a job, because the ambiguity was never the console's. `enabled: off`
 * is a Kubernetes manifest that means `false` to `kubectl`, to this console and
 * to YAML 1.1, and means the text `"off"` to the YAML 1.2 spec, to the operator's
 * editor and to most of what will syntax-highlight it on the way here. Which one
 * is right is not something this console gets to settle. Which one it is going
 * to send is, and that is what this reports.
 *
 * Empty for a document that does not parse — the editor already says so, and a
 * second complaint about a document with a missing colon on line 40 is noise on
 * top of the sentence that matters.
 */
export function divergences(text) {
  const source = String(text ?? '');
  if (!source.trim()) return [];

  let plainDocs;
  let sentDocs;
  try {
    // The default schema is not a stand-in for some other program here: it *is*
    // YAML 1.2's core schema, which is the reading the document's author most
    // likely has in their head and in their editor.
    plainDocs = yaml.loadAll(source);
    sentDocs = loadAll(source);
  } catch {
    return [];
  }
  if (plainDocs.length !== sentDocs.length) return [];

  const found = [];
  plainDocs.forEach((doc, index) => walk(doc, sentDocs[index], [], found));
  return found;
}

/** How many findings a sentence lists before it starts counting instead. */
const NAMED = 4;

/**
 * The one sentence both places that warn about this say.
 *
 * Written here rather than at either call site because there are two — the
 * editor, which shows it wherever a manifest is edited, and the create dialog's
 * form view, which does not mount the editor.
 */
export function divergenceNote(text) {
  const found = divergences(text);
  if (found.length === 0) return null;

  const named = found
    .slice(0, NAMED)
    .map((entry) =>
      entry.isKey
        ? `the key \`${formatPath(entry.path)}\` looks like ${describeReading(entry.looksLike)} and will be sent as ${describeReading(entry.sent)}`
        : `\`${formatPath(entry.path)}\` looks like ${describeReading(entry.looksLike)} and will be sent as ${describeReading(entry.sent)}`,
    );
  const rest = found.length - named.length;

  return {
    severity: 'warning',
    text:
      'This document has scalars that YAML readers disagree about. ' +
      `${named.join('; ')}${rest > 0 ? `; and ${rest} more like it` : ''}. ` +
      'This console and the API server agree on all of them — what the form view shows and what the dry run ' +
      'sends are one reading — but a YAML 1.2 reader, which is most editors, reads them the other way. ' +
      'Quoting the value settles it, and is the same in both.',
  };
}
