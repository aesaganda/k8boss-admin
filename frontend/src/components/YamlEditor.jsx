/**
 * YamlEditor — the manifest an operator is about to send to a cluster.
 *
 * It feeds `MutationDialog`, which means whatever is in this box becomes a
 * `POST`/`PUT` body under §4 and then a diff the operator approves. So the
 * editor's job is not to be clever; it is to make sure the operator is never
 * surprised by what the text in front of them actually parses as.
 *
 * **Local validation is local, and says so.** `js-yaml` can tell us the text is
 * not YAML at all, that it holds several documents, or that it is a scalar
 * rather than an object. It cannot tell us whether the cluster will accept it —
 * that is admission control's answer and it arrives as `422 invalid` from the
 * dry run. Every check below is labelled as a local one for exactly that
 * reason: an editor that showed a green tick and then a rejected write would
 * have taught the operator to distrust the tick, and a console whose validation
 * cannot be trusted is a console whose diffs will not be read either.
 *
 * **A parse error blocks the preview.** Not because the backend cannot handle
 * it — it would answer `422 invalid` perfectly well — but because a round trip
 * to learn about a missing colon on line 40 is a round trip that also writes an
 * audit row for a write that was never possible. Local, instant, and free.
 *
 * **The parse error carries its position.** `js-yaml` puts the line and column
 * on the exception's `mark`, and the difference between "invalid YAML" and
 * "line 42, column 9: bad indentation of a mapping entry" is the difference
 * between an operator scanning 300 lines and fixing one.
 *
 * **Line numbers and colour are painted behind a real textarea.** The editor is
 * still one `<textarea>` — nothing here reimplements a caret, a selection, an
 * undo stack or an IME. The gutter is a sibling column and the syntax colouring
 * is a copy of the same text drawn underneath, with the textarea's own glyphs
 * turned transparent and its scroll offset mirrored onto both. That buys the
 * colour without buying a text editor's bug list; what it costs is one hard
 * constraint, and `index.css` states it where the styles are: the two layers
 * must lay out every character in the same place, so the highlight layer may
 * change colour and nothing else. A different font, weight, letter-spacing or
 * padding shifts the text out from under the caret — and an operator whose
 * caret is not where the glyph says it is will edit the wrong line of somebody's
 * production manifest. `yamlSyntax.js` guarantees the other half of it: the
 * tokens for a line concatenate back to exactly that line.
 *
 * **Tab indents, Escape then Tab escapes.** A textarea that swallows Tab is a
 * keyboard trap — the operator gets into the editor and cannot get out without
 * a mouse. A textarea that does *not* swallow Tab cannot indent YAML, which is
 * the one language where indentation is the entire syntax. The standard
 * resolution is both: Tab inserts, and Escape arms the next Tab to move focus.
 * The hint under the box says so, because an undiscoverable escape hatch is the
 * same trap with extra steps.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Alert, Split, SplitItem } from '@patternfly/react-core';
import yaml from 'js-yaml';

import { tokenizeYaml } from './yamlSyntax';

const INDENT = '  ';

/**
 * The size at which the highlight layer is dropped.
 *
 * Every keystroke re-tokenises the document and hands React one element per
 * token, so the cost is linear in the manifest and paid on each character
 * typed. Two thousand lines is comfortably past every Kubernetes object an
 * operator writes by hand and short of the generated CRDs (cert-manager's is
 * five figures) that would otherwise make typing lag a keystroke behind.
 */
const HIGHLIGHT_MAX_LINES = 2000;

/**
 * Parse and report. Never throws — the result object is the whole vocabulary
 * the caller branches on.
 *
 * `valid` means "this is one YAML document that parses". `notes` are things
 * worth saying that do not make it invalid, because refusing to preview a
 * manifest over a missing `kind` would block the one workflow (patch a field on
 * a fragment) this editor should not have an opinion about.
 */
export function validateYaml(text) {
  const source = String(text ?? '');
  if (!source.trim()) {
    return {
      valid: false,
      empty: true,
      error: null,
      documents: 0,
      parsed: null,
      notes: [],
    };
  }

  let documents;
  try {
    // `loadAll`, not `load`: `load` throws on a multi-document stream with a
    // message about the second `---` that reads like a syntax error, when what
    // the operator actually did was paste a `kubectl get -o yaml` of two
    // objects. Counting them lets us say that instead.
    documents = yaml.loadAll(source);
  } catch (e) {
    const mark = e?.mark;
    return {
      valid: false,
      empty: false,
      documents: 0,
      parsed: null,
      notes: [],
      error: {
        // `e.reason` is the specific complaint; `e.message` repeats it with a
        // snippet of the source, which is useful but too long for a headline.
        reason: e?.reason || e?.message || 'This is not valid YAML.',
        // js-yaml marks are zero-based; humans and editors are not.
        line: mark && typeof mark.line === 'number' ? mark.line + 1 : null,
        column: mark && typeof mark.column === 'number' ? mark.column + 1 : null,
        snippet: e?.message || null,
      },
    };
  }

  const notes = [];
  if (documents.length > 1) {
    notes.push({
      severity: 'error',
      text:
        `This is ${documents.length} YAML documents separated by \`---\`. The resource endpoints in §4 take ` +
        'exactly one object, so only the first would be sent. Split them and apply them one at a time.',
    });
  }

  const first = documents[0];
  if (documents.length === 1 && (first === null || typeof first !== 'object' || Array.isArray(first))) {
    notes.push({
      severity: 'error',
      text: 'This parses as a scalar or a list, not a Kubernetes object. An object with `apiVersion` and `kind` is expected.',
    });
  } else if (first && typeof first === 'object' && !Array.isArray(first)) {
    if (!first.apiVersion) {
      notes.push({ severity: 'warning', text: 'No `apiVersion`. The API server will reject this.' });
    }
    if (!first.kind) {
      notes.push({ severity: 'warning', text: 'No `kind`. The API server will reject this.' });
    }
    if (first.metadata && typeof first.metadata === 'object' && first.metadata.managedFields) {
      notes.push({
        severity: 'warning',
        text:
          '`metadata.managedFields` is present. The console strips it from everything it reads; sending it ' +
          'back can confuse server-side apply ownership.',
      });
    }
  }

  const blocking = notes.some((n) => n.severity === 'error');
  return {
    valid: !blocking,
    empty: false,
    error: null,
    documents: documents.length,
    parsed: first ?? null,
    notes,
  };
}

/**
 *   <YamlEditor
 *     value={text}
 *     onChange={(next) => setText(next)}
 *     onValidityChange={(v) => setValid(v.valid)}
 *     originalValue={loadedYaml}
 *   />
 *
 * `originalValue` is only used to report whether the operator has actually
 * edited anything — the diff that matters is the server's, not ours.
 */
export function YamlEditor({
  value,
  onChange,
  onValidityChange,
  originalValue,
  label = 'Manifest',
  rows = 22,
  readOnly = false,
  isDisabled = false,
  ariaLabel,
  id = 'yaml-editor',
  className,
}) {
  const [caret, setCaret] = useState({ line: 1, column: 1 });
  // Escape arms the next Tab to move focus instead of indenting. Reset after it
  // fires so the editor does not silently stay in "Tab escapes" mode for the
  // rest of the session, which would be just as confusing in the other
  // direction.
  const [tabEscapes, setTabEscapes] = useState(false);
  const areaRef = useRef(null);
  const gutterRef = useRef(null);
  const highlightRef = useRef(null);

  const validity = useMemo(() => validateYaml(value), [value]);

  // Latest callbacks in a ref so the key handler below is not rebuilt (and the
  // textarea not re-bound) on every keystroke of a parent that passes an inline
  // arrow — which every parent does.
  const onChangeRef = useRef(onChange);
  const onValidityRef = useRef(onValidityChange);
  useEffect(() => {
    onChangeRef.current = onChange;
    onValidityRef.current = onValidityChange;
  });

  // `onChange` fires only for edits the operator made. Validity is reported
  // separately, and also for text that arrived from outside — a parent that
  // loaded a manifest from `resources.yaml()` has to know whether Preview may
  // be enabled before anything has been typed. Two channels rather than one
  // re-fired `onChange`, because a parent whose `onChange` sets the text it was
  // just given is a render loop waiting for a stale-closure bug to start it.
  useEffect(() => {
    onValidityRef.current?.(validity);
  }, [validity]);

  /**
   * Mirror the textarea's scroll onto the two layers that cannot scroll
   * themselves.
   *
   * A transform rather than a `scrollTop`/`scrollLeft` assignment: the layers'
   * scrollable extents are not identical to the textarea's — a horizontal
   * scrollbar alone takes a row's worth off it — and an assignment past a
   * layer's own maximum is silently clamped, which shows up as the numbers
   * drifting out of step with the lines at the very bottom of a long manifest.
   * A transform has no maximum to clamp to.
   *
   * Written straight to the DOM, and deliberately not through state: this runs
   * on every scroll event, and a re-render per frame would make scrolling a
   * long manifest the slowest thing in the dialog. React never touches these
   * two `style` attributes, so nothing overwrites what is set here.
   */
  const syncScroll = useCallback(() => {
    const element = areaRef.current;
    if (!element) return;
    const { scrollLeft, scrollTop } = element;
    if (highlightRef.current) {
      highlightRef.current.style.transform = `translate(${-scrollLeft}px, ${-scrollTop}px)`;
    }
    if (gutterRef.current) {
      gutterRef.current.style.transform = `translateY(${-scrollTop}px)`;
    }
  }, []);

  // Also after a render, not only on the scroll event: typing at the bottom of
  // the box scrolls the textarea, and the layers are re-rendered by that same
  // keystroke — which drops the transform of a freshly mounted highlight layer
  // back to zero while the text under it is scrolled.
  useLayoutEffect(syncScroll, [syncScroll, value, rows]);

  const updateCaret = useCallback((element) => {
    const upto = element.value.slice(0, element.selectionStart);
    const lines = upto.split('\n');
    setCaret({ line: lines.length, column: lines[lines.length - 1].length + 1 });
  }, []);

  const handleKeyDown = useCallback(
    (event) => {
      const element = event.target;

      if (event.key === 'Escape') {
        setTabEscapes(true);
        return;
      }

      if (event.key !== 'Tab') {
        if (tabEscapes) setTabEscapes(false);
        return;
      }

      if (tabEscapes || event.ctrlKey || event.metaKey) {
        // Let the browser move focus. Ctrl/Cmd+Tab is the browser's own and
        // must never be intercepted.
        setTabEscapes(false);
        return;
      }

      event.preventDefault();
      const start = element.selectionStart;
      const end = element.selectionEnd;
      const text = element.value;

      if (start !== end || event.shiftKey) {
        // Block indent / outdent over the selected lines. Operating on whole
        // lines rather than on the selection means selecting the middle of a
        // line still indents that line, which is what every editor does and
        // what the muscle memory expects.
        const from = text.lastIndexOf('\n', start - 1) + 1;
        const to = end === start ? end : end - (text[end - 1] === '\n' ? 1 : 0);
        const lineEnd = text.indexOf('\n', to);
        const block = text.slice(from, lineEnd === -1 ? text.length : lineEnd);
        const shifted = block
          .split('\n')
          .map((line) =>
            event.shiftKey
              ? line.replace(new RegExp(`^ {1,${INDENT.length}}`), '')
              : INDENT + line,
          )
          .join('\n');
        const next = text.slice(0, from) + shifted + text.slice(lineEnd === -1 ? text.length : lineEnd);
        onChangeRef.current?.(next);
        // Restore a selection that covers the same lines, so repeated Tab
        // presses keep indenting the block instead of collapsing to a caret.
        requestAnimationFrame(() => {
          if (!areaRef.current) return;
          areaRef.current.selectionStart = from;
          areaRef.current.selectionEnd = from + shifted.length;
        });
        return;
      }

      const next = text.slice(0, start) + INDENT + text.slice(end);
      onChangeRef.current?.(next);
      requestAnimationFrame(() => {
        if (!areaRef.current) return;
        areaRef.current.selectionStart = start + INDENT.length;
        areaRef.current.selectionEnd = start + INDENT.length;
      });
    },
    [tabEscapes],
  );

  const edited = originalValue != null && String(value ?? '') !== String(originalValue);
  const lineCount = useMemo(() => String(value ?? '').split('\n').length, [value]);

  // The highlight layer is dropped rather than degraded: a coloured manifest
  // that stutters a keystroke behind the operator is worse than a plain one,
  // and a CRD's `openAPIV3Schema` really does run to thousands of lines. The
  // gutter stays either way — line numbers cost one element per line and are
  // what the parse error's "line 137" is measured against.
  //
  // A disabled textarea is excluded for a different reason: the browser mutes
  // its text, and a full-strength coloured copy painted behind it would read as
  // an editor that is still live.
  const highlighted = !isDisabled && lineCount <= HIGHLIGHT_MAX_LINES;

  // Clamped: js-yaml can mark a truncated document one line past its end, and a
  // gutter entry that does not exist would silently mark nothing at all.
  const errorLine =
    validity.error?.line && validity.error.line <= lineCount ? validity.error.line : null;

  const gutterLines = useMemo(
    () =>
      Array.from({ length: lineCount }, (_, i) => (
        <span
          key={i}
          className={`admin-yaml__lineno${i + 1 === errorLine ? ' admin-yaml__lineno--error' : ''}`}
        >
          {i + 1}
        </span>
      )),
    [lineCount, errorLine],
  );

  const highlightLines = useMemo(() => {
    if (!highlighted) return null;
    return tokenizeYaml(value ?? '').map((tokens, index) => (
      <span
        key={index}
        className={`admin-yaml__line${index + 1 === errorLine ? ' admin-yaml__line--error' : ''}`}
      >
        {tokens.map((token, i) =>
          token.kind ? (
            <span key={i} className={`admin-yaml__t--${token.kind}`}>
              {token.text}
            </span>
          ) : (
            token.text
          ),
        )}
      </span>
    ));
  }, [highlighted, value, errorLine]);

  return (
    <div className={className} data-testid="yaml-editor">
      <Split hasGutter style={{ alignItems: 'baseline', marginBlockEnd: 'var(--admin-gap-sm, 0.5rem)' }}>
        <SplitItem>
          <label htmlFor={id} style={{ fontWeight: 600 }}>
            {label}
          </label>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <span style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.8125rem' }}>
            {lineCount} {lineCount === 1 ? 'line' : 'lines'} · Ln {caret.line}, Col {caret.column}
            {edited ? ' · edited' : ''}
            {/* Said out loud rather than left to be noticed: an editor that
                silently stops colouring above a size looks broken, and the
                operator goes hunting for the syntax error that turned it off. */}
            {!highlighted && !isDisabled ? ' · highlighting off (large manifest)' : ''}
          </span>
        </SplitItem>
      </Split>

      {/* The border, the background and the focus ring belong to the frame: the
          gutter and the box are one control, and a border drawn around the
          textarea alone would leave the line numbers outside the thing it
          encloses. The textarea's own background stays transparent, or there
          would be nothing to see of the layer underneath it. */}
      <div
        className="admin-yaml"
        data-invalid={!validity.valid && !validity.empty ? 'true' : undefined}
        // The gutter is exactly as wide as the largest line number needs, so a
        // 40-line manifest does not carry the indent of a 4000-line one.
        style={{ '--admin-yaml-digits': String(lineCount).length }}
      >
        <div className="admin-yaml__gutter" aria-hidden="true" data-testid="yaml-editor-gutter">
          <div className="admin-yaml__gutter-inner" ref={gutterRef}>
            {gutterLines}
          </div>
        </div>

        <div className="admin-yaml__code">
          {highlighted && (
            <div className="admin-yaml__highlight" aria-hidden="true">
              <div
                className="admin-yaml__highlight-inner"
                ref={highlightRef}
                data-testid="yaml-editor-highlight"
              >
                {highlightLines}
              </div>
            </div>
          )}

          <textarea
            id={id}
            ref={areaRef}
            className={`admin-yaml__input${highlighted ? ' admin-yaml__input--overlaid' : ''}`}
            value={value ?? ''}
            readOnly={readOnly}
            disabled={isDisabled}
            spellCheck={false}
            // Autocorrect and autocapitalise on a mobile keyboard silently rewrite
            // `metadata` to `Metadata` and container image tags into sentences.
            autoCorrect="off"
            autoCapitalize="off"
            autoComplete="off"
            rows={rows}
            aria-label={ariaLabel || label}
            aria-invalid={!validity.valid && !validity.empty}
            aria-describedby={`${id}-status`}
            onChange={(event) => {
              updateCaret(event.target);
              onChangeRef.current?.(event.target.value);
            }}
            onKeyDown={handleKeyDown}
            onKeyUp={(event) => updateCaret(event.target)}
            onClick={(event) => updateCaret(event.target)}
            onBlur={() => setTabEscapes(false)}
            onScroll={syncScroll}
            data-testid="yaml-editor-input"
          />
        </div>
      </div>

      <p
        style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.8125rem', margin: '0.25rem 0 0' }}
      >
        Tab indents. Press Escape then Tab to move focus out of the editor.
      </p>

      <div id={`${id}-status`} aria-live="polite">
        {validity.error && (
          <Alert
            isInline
            variant="danger"
            title={
              validity.error.line
                ? `Line ${validity.error.line}, column ${validity.error.column}: ${validity.error.reason}`
                : validity.error.reason
            }
            className="admin-confirm__alert"
            data-testid="yaml-editor-error"
          >
            {/* js-yaml's full message includes the offending source line with a
                caret under the column. That is the most useful thing it
                produces and it is thrown away by anyone who renders only
                `reason`. */}
            {validity.error.snippet && (
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: '0.8125rem' }}>
                {validity.error.snippet}
              </pre>
            )}
          </Alert>
        )}

        {validity.notes.map((note, i) => (
          <Alert
            key={i}
            isInline
            variant={note.severity === 'error' ? 'danger' : 'warning'}
            title={note.text}
            className="admin-confirm__alert"
            data-testid="yaml-editor-note"
          />
        ))}

        {validity.valid && validity.notes.length === 0 && (
          <p style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.8125rem', margin: '0.25rem 0 0' }}>
            Parses as one YAML object. This is a local check only — schema and admission validation happen
            on the cluster, and the dry run is what reports them.
          </p>
        )}
      </div>
    </div>
  );
}

export default YamlEditor;
