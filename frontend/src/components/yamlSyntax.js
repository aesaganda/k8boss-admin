/**
 * A line-oriented YAML tokenizer, for colouring a manifest the operator is
 * about to send to a cluster.
 *
 * **It colours; it never decides.** `validateYaml` in `YamlEditor.jsx` is what
 * says whether the text parses, using `js-yaml` — a real parser. This module
 * exists because a real parser gives you an object, and an object cannot tell
 * you which characters on line 137 were the key. Nothing here is allowed to
 * feed a decision: no caller may read a token kind to decide whether Preview is
 * enabled, whether the document is valid, or what it contains. A regex-shaped
 * approximation of YAML that fed a gate would be the defect standard with
 * syntax colours on.
 *
 * **The invariant that makes it safe to be approximate.** The tokens for a line
 * concatenate back to exactly that line, character for character — that is
 * asserted per line in `tokenizeYaml`, and a line that fails falls back to one
 * uncoloured token. The highlight layer is painted *behind* a transparent
 * textarea, so a token that drops or adds a single space slides every glyph
 * after it out from under the operator's caret, and the manifest they are
 * reading is no longer the manifest they are editing. Mis-coloured is
 * cosmetic. Mis-aligned is a lie about which line you are on, told at the exact
 * moment somebody is deciding whether to apply it.
 *
 * **Block scalars are tracked across lines, and that is not a nicety.** Inside
 * a `|` block, `key: value` is literal text — a ConfigMap holding an nginx
 * config or another manifest is the common case, not the exotic one. Colouring
 * those inner lines as YAML keys would draw structure that the document does
 * not have, and reading the indentation of an embedded manifest as if it were
 * the outer one is how somebody convinces themselves a key is at the wrong
 * depth and "fixes" it.
 *
 * Kinds are deliberately few: `key`, `string`, `number`, `const` (booleans and
 * nulls), `comment`, `punct`, `meta` (anchors, aliases, tags, document
 * markers), `literal` (block scalar content), and undefined for plain text.
 */

/** Everything YAML 1.1 treats as a boolean or a null in an unquoted scalar. */
const CONSTANT = new Set([
  'true', 'True', 'TRUE', 'false', 'False', 'FALSE',
  'yes', 'Yes', 'YES', 'no', 'No', 'NO',
  'on', 'On', 'ON', 'off', 'Off', 'OFF',
  'null', 'Null', 'NULL', '~', '',
]);

// Kubernetes fields are ints, floats and quantities. A quantity ("100m",
// "2Gi") is NOT matched on purpose: it is a string to the API server, and
// colouring it as a number would say the opposite of what the schema does.
const NUMBER = /^[-+]?(?:0b[01_]+|0o?[0-7_]+|0x[0-9a-fA-F_]+|(?:\d[\d_]*)?\.?\d[\d_]*(?:[eE][-+]?\d+)?)$/;

/** Booleans, nulls and numbers, else undefined for "plain text". */
function scalarKind(text) {
  if (CONSTANT.has(text)) return 'const';
  if (NUMBER.test(text)) return 'number';
  return undefined;
}

/**
 * A cursor over one line, accumulating tokens.
 *
 * Every `take*` moves the cursor by exactly the characters it emits, which is
 * what keeps the concatenation invariant true by construction rather than by
 * inspection.
 */
class LineScanner {
  constructor(line) {
    this.line = line;
    this.at = 0;
    this.tokens = [];
  }

  get rest() {
    return this.line.slice(this.at);
  }

  get done() {
    return this.at >= this.line.length;
  }

  /** Emit `length` characters as `kind`. Zero-length emissions are dropped. */
  take(length, kind) {
    if (length <= 0) return '';
    const text = this.line.slice(this.at, this.at + length);
    this.at += text.length;
    this.tokens.push(kind ? { text, kind } : { text });
    return text;
  }

  /** Emit the run of spaces and tabs at the cursor, if any, as plain text. */
  takeSpace() {
    const run = /^[ \t]*/.exec(this.rest)[0];
    return this.take(run.length);
  }

  takeRest(kind) {
    return this.take(this.line.length - this.at, kind);
  }
}

/**
 * Length of the quoted scalar starting at `text[0]`, including both quotes.
 *
 * An unterminated quote runs to the end of the line: the operator is mid-type,
 * and the alternative — refusing to colour it — makes the whole tail of the
 * line flicker between two colours as they type the closing quote.
 */
function quotedLength(text) {
  const quote = text[0];
  for (let i = 1; i < text.length; i += 1) {
    if (quote === '"' && text[i] === '\\') {
      i += 1;
    } else if (text[i] === quote) {
      // '' is an escaped single quote inside a single-quoted scalar.
      if (quote === "'" && text[i + 1] === "'") i += 1;
      else return i + 1;
    }
  }
  return text.length;
}

/** `#` opens a comment only at the start of a line or after whitespace. */
function commentStart(text, offset) {
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === '"' || text[i] === "'") {
      i += quotedLength(text.slice(i)) - 1;
    } else if (text[i] === '#' && (offset + i === 0 || /[ \t]/.test(text[i - 1] ?? ' '))) {
      return i;
    }
  }
  return -1;
}

/** Trailing ` # comment`, if the cursor is sitting on one. */
function takeComment(scan) {
  if (scan.rest.startsWith('#') && (scan.at === 0 || /[ \t]/.test(scan.line[scan.at - 1]))) {
    scan.takeRest('comment');
    return true;
  }
  return false;
}

/**
 * A flow collection — `{a: b}`, `[1, 2]`, `command: ["sh", "-c"]`.
 *
 * Deliberately shallow: brackets, commas and colons are punctuation, quoted
 * runs are strings, and everything else is classified as a scalar. Nesting
 * needs no stack because nothing here depends on depth.
 */
function takeFlow(scan) {
  while (!scan.done) {
    const rest = scan.rest;
    const char = rest[0];

    if (/[ \t]/.test(char)) {
      scan.takeSpace();
    } else if ('{}[],:'.includes(char)) {
      scan.take(1, 'punct');
    } else if (char === '"' || char === "'") {
      scan.take(quotedLength(rest), 'string');
    } else if (char === '#' && /[ \t]/.test(scan.line[scan.at - 1] ?? ' ')) {
      scan.takeRest('comment');
    } else {
      // A bare word up to the next structural character. If a colon follows it,
      // it was a key.
      const word = /^[^ \t{}[\],:#]*/.exec(rest)[0] || rest[0];
      const key = rest[word.length] === ':';
      scan.take(word.length, key ? 'key' : scalarKind(word));
    }
  }
}

/**
 * The value half of `key: value`, or a bare scalar in a sequence entry.
 *
 * Returns the block-scalar indent to open (`|`/`>` seen), or null.
 */
function takeValue(scan, indent) {
  scan.takeSpace();
  if (scan.done || takeComment(scan)) return null;

  const rest = scan.rest;
  const char = rest[0];

  // Anchors, aliases and tags bind to whatever follows, so emit and continue.
  if (char === '&' || char === '*' || char === '!') {
    scan.take(/^\S+/.exec(rest)[0].length, 'meta');
    return takeValue(scan, indent);
  }

  // `|`, `>`, `|-`, `>+`, `|2-` … everything after the header on that line is a
  // comment, and every more-indented line below it is literal text.
  if (char === '|' || char === '>') {
    scan.take(/^[|>][+-]?\d*[+-]?/.exec(rest)[0].length, 'punct');
    scan.takeSpace();
    takeComment(scan);
    return indent;
  }

  if (char === '"' || char === "'") {
    scan.take(quotedLength(rest), 'string');
    scan.takeSpace();
    takeComment(scan);
    return null;
  }

  if (char === '{' || char === '[') {
    takeFlow(scan);
    return null;
  }

  // A plain scalar runs to the end of the line or to a comment, and keeps its
  // inner spaces: `command: sleep 3600` is one value, not a value and a number.
  const cut = commentStart(rest, scan.at);
  const raw = cut === -1 ? rest : rest.slice(0, cut);
  const trimmed = raw.replace(/[ \t]+$/, '');
  scan.take(trimmed.length, scalarKind(trimmed));
  scan.takeSpace();
  takeComment(scan);
  return null;
}

/**
 * Length of the key at the cursor if this line opens `key:`, else -1.
 *
 * The colon has to be followed by whitespace or end-of-line to be a mapping
 * separator — `image: registry.example:5000/app` has two colons and one key,
 * and a rule that took the first would colour half a registry host as a field
 * name.
 */
function keyLength(rest) {
  if (rest[0] === '"' || rest[0] === "'") {
    const quoted = quotedLength(rest);
    return rest[quoted] === ':' ? quoted : -1;
  }
  for (let i = 0; i < rest.length; i += 1) {
    if (rest[i] === '#' && (i === 0 || /[ \t]/.test(rest[i - 1]))) return -1;
    if (rest[i] === ':' && (i + 1 === rest.length || /[ \t]/.test(rest[i + 1]))) {
      // Trailing space before the colon belongs to the key run; `a :` is legal.
      return i;
    }
  }
  return -1;
}

/** One line, given the enclosing block-scalar indent (or null). Never throws. */
function tokenizeLine(line, blockIndent) {
  const scan = new LineScanner(line);
  const indentRun = /^[ \t]*/.exec(line)[0];
  const indent = indentRun.length;
  const blank = line.trim() === '';

  // Inside a block scalar: literal text, and a blank line does not close it.
  if (blockIndent !== null && (blank || indent > blockIndent)) {
    scan.takeRest(blank ? undefined : 'literal');
    return { tokens: scan.tokens, blockIndent };
  }

  scan.takeSpace();
  if (scan.done) return { tokens: scan.tokens, blockIndent: null };

  // `---` and `...` are document markers only when they own the line's start.
  if (indent === 0 && /^(---|\.\.\.)(\s|$)/.test(scan.rest)) {
    scan.take(3, 'meta');
    scan.takeSpace();
    if (scan.done) return { tokens: scan.tokens, blockIndent: null };
  }

  // Sequence entries nest: `- - name: a` is legal, and each dash shifts the
  // column the entry's own content sits at.
  let dash = -1;
  let nested = scan.at;
  while (/^-([ \t]|$)/.test(scan.rest)) {
    dash = scan.at;
    scan.take(1, 'punct');
    scan.takeSpace();
    nested = scan.at;
  }

  if (takeComment(scan)) return { tokens: scan.tokens, blockIndent: null };

  const key = keyLength(scan.rest);
  if (key >= 0) {
    scan.take(key, 'key');
    scan.take(1, 'punct');
    // A block scalar under a key is indented past that key's own column, which
    // after a `- ` is the column the dash pushed it to.
    return { tokens: scan.tokens, blockIndent: takeValue(scan, nested) };
  }

  // No colon: a sequence entry's scalar, a flow collection, or a plain scalar
  // continued from the line above. `- |` measures its content against the dash,
  // not against the `|` — `- |` with content two columns further in is the
  // shape every `args:` list uses, and measuring from the `|` would close the
  // block on its own first line.
  return { tokens: scan.tokens, blockIndent: takeValue(scan, dash === -1 ? indent : dash) };
}

/**
 * Tokenize a whole document, one array of tokens per line.
 *
 * `tokenizeYaml('a: 1\n')` has two entries, matching `split('\n')` and the line
 * numbers in the gutter — a trailing newline really is an empty last line, and
 * js-yaml's error marks count it.
 */
export function tokenizeYaml(text) {
  const lines = String(text ?? '').split('\n');
  const out = [];
  let blockIndent = null;

  for (const line of lines) {
    let tokens;
    try {
      const result = tokenizeLine(line, blockIndent);
      tokens = result.tokens;
      blockIndent = result.blockIndent;
    } catch {
      // A tokenizer bug must cost colour and nothing else.
      tokens = [{ text: line }];
      blockIndent = null;
    }
    // The invariant, checked rather than trusted: this is the cheap comparison
    // that stands between a scanner mistake and an operator editing a manifest
    // whose highlighted copy has drifted a character to the left.
    if (tokens.map((t) => t.text).join('') !== line) tokens = [{ text: line }];
    out.push(tokens);
  }

  return out;
}

export default tokenizeYaml;
