/**
 * yamlDivergence — the manifest the browser read, against the manifest the API
 * server will be sent.
 *
 * This console parses every submitted manifest **twice, with two parsers that
 * do not agree**. The browser uses js-yaml 4, which implements YAML 1.2's core
 * schema. `app.admin.apply.parse_document` uses PyYAML, whose resolvers are
 * YAML 1.1. For most documents that distinction is academic. For a handful of
 * unquoted scalars it is not:
 *
 * ```
 *   enabled: off      browser: the text "off"     API server: the boolean false
 *   version: 010      browser: the number 10      API server: the number 8
 *   port: 8:30        browser: the text "8:30"    API server: the number 510
 *   limit: 1_000      browser: the text "1_000"   API server: the number 1000
 * ```
 *
 * **What that costs, precisely.** The dry-run diff is the API server's own
 * projection of what it was sent, so the diff is not lying — this is not a
 * console that shows one object and writes another. What is wrong is everything
 * *local*: the form view's controls are lenses onto the browser's reading, and
 * so is `unrepresented()`, and so is every check in `localIssues()`. The one
 * written to catch exactly this class — "a label value that parsed as a number
 * or a boolean is the one mistake YAML makes on the operator's behalf" — is
 * blind to half of it, because `version: yes` is a harmless string to js-yaml
 * and a boolean to the API server, which then refuses the whole object with an
 * unmarshalling error that names no field.
 *
 * So this module answers one question, before any of that: **do the two parsers
 * read this document the same way?** It is a warning and never a gate. The
 * document is legal YAML either way and the dry run is the authority on whether
 * the cluster wants it; what an operator gets here is the chance to quote a
 * scalar while quoting it is still free.
 *
 * ## Why a second parse rather than a regex
 *
 * The obvious implementation is a pattern over the text, and `yamlSyntax.js` —
 * which has a tokenizer that could find the scalars — says in its own docstring
 * why that is not allowed to decide anything: *"A regex-shaped approximation of
 * YAML that fed a gate would be the defect standard with syntax colours on."*
 * A pattern cannot tell a plain `off` from an `off` inside a `|` block, from a
 * quoted `"off"`, from a continuation line of a multi-line plain scalar. A
 * parser can, because that is what a parser is.
 *
 * So both readings come from js-yaml. The second one runs against a schema
 * whose scalar resolvers are transcribed from PyYAML's own
 * `yaml/resolver.py` — three regexes and three constructors, below — and the
 * two resulting trees are compared. Nothing here approximates YAML; the only
 * thing transcribed is which strings PyYAML calls numbers, and
 * `backend/tests/test_yaml_scalar_divergence.py` pins that against the real
 * PyYAML from the other side, over the same corpus.
 *
 * It costs a second parse per keystroke, which is the same order as the first
 * one the editor already does and is not what makes an editor feel slow —
 * rendering one element per token is, which is why `HIGHLIGHT_MAX_LINES`
 * exists and this has no equivalent.
 */
import yaml from 'js-yaml';

import { formatPath } from './objectForm';

/* ── PyYAML's implicit resolvers ────────────────────────────────────────── */

/*
 * Transcribed from PyYAML's `yaml/resolver.py`, verbatim modulo the `re.X`
 * whitespace. They are the whole of what this module claims to know about the
 * other parser, and they are checked against it by the backend test named in
 * the docstring above — so a PyYAML release that changes one of them fails a
 * test rather than quietly making this warning wrong in one direction or the
 * other. Wrong in the quiet direction is the worse one: an operator who has
 * been told a document is unambiguous, and it is not.
 */
const PY_BOOL = /^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/;

const PY_INT =
  /^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/;

const PY_FLOAT =
  /^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/;

const PY_TRUE = /^(?:yes|Yes|YES|true|True|TRUE|on|On|ON)$/;

/** What PyYAML makes of a scalar its integer resolver matched. */
function pyInt(raw) {
  const text = raw.replace(/_/g, '');
  const negative = text[0] === '-';
  const body = text.replace(/^[-+]/, '');
  let value;
  if (/^0b/.test(body)) value = parseInt(body.slice(2), 2);
  else if (/^0x/.test(body)) value = parseInt(body.slice(2), 16);
  // Sexagesimal: `8:30` is 8*60 + 30. YAML 1.1 kept it for durations and
  // Kubernetes has no field that wants one, which is exactly why nobody writing
  // `8:30` in a manifest means 510.
  else if (body.includes(':')) value = body.split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
  // A leading zero is octal, and `0` on its own is not: `[-+]?0[0-7_]+` needs a
  // digit after it.
  else if (/^0[0-7]+$/.test(body)) value = parseInt(body.slice(1), 8);
  else value = Number(body);
  // `-0` is `0`, not `-0`. Python has one zero and JavaScript has two, and
  // `Object.is` — which this module compares with, so that two NaNs count as
  // one reading — is the one comparison that can tell them apart. Without this
  // line `replicas: -0` is reported as a document the two parsers disagree
  // about, which is the false positive that teaches people to ignore the true
  // ones.
  if (value === 0) return 0;
  return negative ? -value : value;
}

/** What PyYAML makes of a scalar its float resolver matched. */
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
 * js-yaml, reading plain scalars the way PyYAML does.
 *
 * `extend({ implicit: [...] })` and not `extend([...])`: the array form adds
 * *explicit* types, which only apply to a scalar carrying a `!!tag`, and the
 * schema would then behave identically to the default one — a comparison that
 * can never find anything and a test that passes for the wrong reason. Only the
 * three scalar resolvers are replaced; everything else, timestamps included, is
 * js-yaml's own and matches PyYAML already.
 */
export const CLUSTER_SCHEMA = yaml.DEFAULT_SCHEMA.extend({
  implicit: [
    new yaml.Type('tag:yaml.org,2002:bool', {
      kind: 'scalar',
      resolve: (text) => PY_BOOL.test(text),
      construct: (text) => PY_TRUE.test(text),
    }),
    new yaml.Type('tag:yaml.org,2002:int', {
      kind: 'scalar',
      resolve: (text) => PY_INT.test(text),
      construct: pyInt,
    }),
    new yaml.Type('tag:yaml.org,2002:float', {
      kind: 'scalar',
      resolve: (text) => PY_FLOAT.test(text),
      construct: pyFloat,
    }),
  ],
});

/* ── Comparing the two readings ─────────────────────────────────────────── */

function isMapping(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value) && !(value instanceof Date);
}

/** Do these two leaves mean the same thing? */
function sameScalar(browser, cluster) {
  if (browser instanceof Date || cluster instanceof Date) {
    return browser instanceof Date && cluster instanceof Date && browser.getTime() === cluster.getTime();
  }
  if (typeof browser !== typeof cluster) return false;
  // `Object.is` rather than `===` for the one pair of numbers that are equal to
  // nothing, themselves included: two NaNs are the same reading.
  if (typeof browser === 'number') return Object.is(browser, cluster);
  return browser === cluster;
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
 * appearance would under-report a document that really does differ there.
 */
function walk(browser, cluster, path, found, ancestors = new Set()) {
  if (browser && typeof browser === 'object') {
    if (ancestors.has(browser)) return;
    ancestors = new Set(ancestors).add(browser);
  }
  if (Array.isArray(browser) && Array.isArray(cluster) && browser.length === cluster.length) {
    browser.forEach((item, index) => walk(item, cluster[index], [...path, index], found, ancestors));
    return;
  }
  if (isMapping(browser) && isMapping(cluster)) {
    const browserKeys = Object.keys(browser);
    const clusterKeys = Object.keys(cluster);
    // A key is a scalar too, and `off: value` is a key the two parsers read
    // differently — which on a ConfigMap is a data key called "False". Reported
    // once for the block rather than per key: the object is already the wrong
    // shape and walking into it would name the same fact several times.
    const differing = browserKeys.find((key) => !clusterKeys.includes(key));
    if (differing !== undefined) {
      found.push({
        path: [...path, differing],
        browser: differing,
        cluster: clusterKeys.find((key) => !browserKeys.includes(key)) ?? null,
        isKey: true,
      });
      return;
    }
    browserKeys.forEach((key) => walk(browser[key], cluster[key], [...path, key], found, ancestors));
    return;
  }
  if (isMapping(browser) !== isMapping(cluster) || Array.isArray(browser) !== Array.isArray(cluster)) {
    // Only scalar resolution differs between the two schemas, so a shape that
    // differs is something this module did not predict. Saying nothing is the
    // conservative answer: a warning nobody can act on is worse than none.
    return;
  }
  if (!sameScalar(browser, cluster)) {
    found.push({ path, browser, cluster, isKey: false });
  }
}

/**
 * Every place in `text` the two parsers read differently.
 *
 * Empty for a document that does not parse — the editor already says so, and a
 * second complaint about a document with a missing colon on line 40 is noise on
 * top of the sentence that matters.
 */
export function divergences(text) {
  const source = String(text ?? '');
  if (!source.trim()) return [];

  let browserDocs;
  let clusterDocs;
  try {
    browserDocs = yaml.loadAll(source);
    clusterDocs = yaml.loadAll(source, null, { schema: CLUSTER_SCHEMA });
  } catch {
    return [];
  }
  if (browserDocs.length !== clusterDocs.length) return [];

  const found = [];
  browserDocs.forEach((doc, index) => walk(doc, clusterDocs[index], [], found));
  return found;
}

/** How many findings a sentence lists before it starts counting instead. */
const NAMED = 4;

/**
 * The one sentence both places that warn about this say.
 *
 * Written here rather than at either call site because there are two — the
 * editor, which shows it wherever a manifest is edited, and the create dialog's
 * form view, which does not mount the editor and is the view most affected: its
 * controls *are* the browser's reading of the document.
 */
export function divergenceNote(text) {
  const found = divergences(text);
  if (found.length === 0) return null;

  const named = found
    .slice(0, NAMED)
    .map((entry) =>
      entry.isKey
        ? `the key \`${formatPath(entry.path)}\` is read here as ${describeReading(entry.browser)} and by the API server as ${describeReading(entry.cluster)}`
        : `\`${formatPath(entry.path)}\` is read here as ${describeReading(entry.browser)} and will be sent as ${describeReading(entry.cluster)}`,
    );
  const rest = found.length - named.length;

  return {
    severity: 'warning',
    text:
      'This console and the API server read this document differently. ' +
      `${named.join('; ')}${rest > 0 ? `; and ${rest} more like it` : ''}. ` +
      'Quoting the value settles it — the difference is YAML 1.1 against YAML 1.2, and only quoting is the ' +
      'same in both. The dry run below shows what the API server was actually sent, and it is the authority.',
  };
}
