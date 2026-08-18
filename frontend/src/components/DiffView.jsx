/**
 * DiffView — the unified diff an operator reads before approving a write.
 *
 * Contract §11.3 makes this the load-bearing surface of the whole console: no
 * button writes to a cluster without having shown a diff first, so whatever is
 * on screen here is what the operator is consenting to. Three consequences,
 * each of which is a rule rather than a preference:
 *
 * **The text is rendered verbatim.** `diff.unified` is produced by the backend
 * from the API server's own `dryRun=All` projection (§1.5). This component
 * splits it into lines and colours the gutters; it never re-wraps, re-indents,
 * re-orders, prettifies or regenerates a character of it. A diff that was
 * beautified client-side is a diff the operator approved and the cluster never
 * saw, and the two would differ exactly where it mattered least often and worst.
 *
 * **Long lines scroll, they do not wrap by default.** A wrapped diff line loses
 * its alignment with the +/- gutter, and a base64 Secret value or a 900-column
 * container command reflows into a paragraph in which an added line and a
 * removed line look identical. The scroll container is local — `overflow: auto`
 * on this component's own box — so a wide diff never makes the page scroll
 * sideways. Wrapping is available as a toggle for the cases where reading the
 * whole value matters more than the alignment.
 *
 * **The `+`/`-` characters stay in the text.** Colour is reinforcement, never
 * the only signal: the tints come from `admin-diff__add` / `admin-diff__del` in
 * index.css, which are `--pf-t--global--*` tokens with dark-mode overrides, and
 * the marker column repeats the sign for greyscale screenshots, colour-blind
 * readers, and anything pasted into a ticket.
 *
 * Line numbers are computed from the hunk headers rather than counted from the
 * top of the file, because a unified diff omits everything outside its context
 * window. Numbering sequentially would have printed confident, wrong line
 * numbers for every hunk after the first — the operator's next move after
 * reading a diff is often to go look at that line in the YAML.
 */
import { useMemo, useState } from 'react';
import { Button, Split, SplitItem, Tooltip } from '@patternfly/react-core';

/** `@@ -12,7 +12,9 @@ optional section heading` */
const HUNK_RE = /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/;

/**
 * One rendered row. `kind` drives the tint; `oldNo`/`newNo` are null where the
 * line does not exist on that side, which is the whole point of the two gutters
 * — an added line has no old number and a removed line has no new one, and
 * printing a number in both columns would imply a correspondence that is not
 * there.
 */
function parseUnified(unified) {
  const lines = String(unified ?? '').split('\n');
  // A trailing newline produces one empty final element. Rendering it adds a
  // blank numbered row to the bottom of every diff.
  if (lines.length && lines[lines.length - 1] === '') lines.pop();

  const rows = [];
  let oldNo = null;
  let newNo = null;
  let added = 0;
  let removed = 0;

  for (const text of lines) {
    // File headers first: `---` and `+++` open every unified diff and are NOT a
    // removal and an addition. Testing the single leading character before
    // these would open every diff with a red line and a green line and teach
    // the reader to distrust the colours.
    if (text.startsWith('---') || text.startsWith('+++')) {
      rows.push({ kind: 'meta', text, oldNo: null, newNo: null });
      continue;
    }
    const hunk = HUNK_RE.exec(text);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[3]);
      rows.push({ kind: 'hunk', text, oldNo: null, newNo: null });
      continue;
    }
    const marker = text.charAt(0);
    if (marker === '+') {
      added += 1;
      rows.push({ kind: 'add', text, oldNo: null, newNo: newNo });
      if (newNo != null) newNo += 1;
    } else if (marker === '-') {
      removed += 1;
      rows.push({ kind: 'del', text, oldNo: oldNo, newNo: null });
      if (oldNo != null) oldNo += 1;
    } else if (marker === '\\') {
      // `\ No newline at end of file` belongs to neither side and advances
      // neither counter.
      rows.push({ kind: 'meta', text, oldNo: null, newNo: null });
    } else {
      rows.push({ kind: 'context', text, oldNo: oldNo, newNo: newNo });
      if (oldNo != null) oldNo += 1;
      if (newNo != null) newNo += 1;
    }
  }

  return { rows, added, removed };
}

const KIND_CLASS = {
  add: 'admin-diff__add',
  del: 'admin-diff__del',
  hunk: 'admin-diff__hunk',
  meta: 'admin-diff__hunk',
  context: '',
};

const GUTTER_STYLE = {
  display: 'inline-block',
  width: '3.25rem',
  paddingInlineEnd: 'var(--admin-gap-sm, 0.5rem)',
  textAlign: 'right',
  color: 'var(--admin-muted, #6a6e73)',
  userSelect: 'none',
  flex: '0 0 auto',
};

/**
 *   <DiffView unified={result.diff.unified} changed={result.diff.changed} />
 *
 * `changed` is passed separately rather than inferred from the text, because
 * §1.5 makes it the backend's verdict: an empty `unified` with `changed: true`
 * (a delete, whose `after` is null) and an empty `unified` with
 * `changed: false` (a genuine no-op) are different facts, and only the caller's
 * response can tell them apart.
 */
export function DiffView({
  unified,
  changed,
  emptyLabel,
  maxHeight = 420,
  showLineNumbers = true,
  ariaLabel = 'Unified diff',
  className,
}) {
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);
  const { rows, added, removed } = useMemo(() => parseUnified(unified), [unified]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(String(unified ?? ''));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access is refused on insecure origins. Silently doing nothing
      // looks like a successful copy, and the operator pastes whatever was in
      // the buffer before into a ticket as "the diff we approved".
      setCopied(false);
      window.prompt('Copy is blocked by this browser. Select and copy manually:', String(unified ?? ''));
    }
  };

  if (!rows.length) {
    return (
      <p
        className={className}
        data-testid="diff-view-empty"
        style={{ color: 'var(--admin-muted, #6a6e73)', margin: 0 }}
      >
        {emptyLabel ??
          (changed === false
            ? 'The API server found no difference between this request and the live object.'
            : // Not "no changes": an absent diff is a statement about the diff,
              // not about the write. A delete has no textual diff on the
              // `after` side and is very much a change.
              'No textual diff was returned for this change.')}
      </p>
    );
  }

  return (
    <div className={className} data-testid="diff-view">
      <Split hasGutter style={{ alignItems: 'center', marginBlockEnd: 'var(--admin-gap-sm, 0.5rem)' }}>
        <SplitItem>
          <span style={{ fontSize: '0.875rem', color: 'var(--admin-muted, #6a6e73)' }}>
            <span className="admin-diff__add" style={{ padding: '0 0.25rem' }}>
              +{added}
            </span>{' '}
            <span className="admin-diff__del" style={{ padding: '0 0.25rem' }}>
              −{removed}
            </span>{' '}
            {added + removed === 1 ? 'line' : 'lines'}
          </span>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <Tooltip
            content={
              wrap
                ? 'Wrapping keeps whole values visible but breaks the alignment of the +/- gutter.'
                : 'Long lines scroll sideways so added and removed lines stay aligned.'
            }
          >
            <Button variant="link" isInline onClick={() => setWrap((v) => !v)} data-testid="diff-wrap-toggle">
              {wrap ? 'No wrap' : 'Wrap lines'}
            </Button>
          </Tooltip>
        </SplitItem>
        <SplitItem>
          <Button variant="link" isInline onClick={copy} data-testid="diff-copy">
            {copied ? 'Copied' : 'Copy diff'}
          </Button>
        </SplitItem>
      </Split>

      <div
        // The local scroll container. Both axes are owned here so a 900-column
        // container command scrolls inside the diff instead of widening the
        // dialog and then the page behind it.
        style={{
          maxHeight,
          overflow: 'auto',
          border: '1px solid var(--admin-border, #d2d2d2)',
          borderRadius: 'var(--pf-t--global--border--radius--small, 4px)',
          background: 'var(--admin-surface, #f2f2f2)',
          fontFamily: 'var(--admin-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
          fontSize: '0.8125rem',
          lineHeight: 1.5,
        }}
        tabIndex={0}
        role="region"
        aria-label={ariaLabel}
      >
        <pre style={{ margin: 0, padding: '0.25rem 0' }}>
          {rows.map((row, i) => (
            <span
              key={i}
              className={`admin-diff__line ${KIND_CLASS[row.kind]}`.trim()}
              data-diff-kind={row.kind}
              style={{
                display: 'flex',
                // `pre-wrap` alone would also collapse the leading run of
                // spaces that carries YAML's entire meaning, so the wrapped
                // mode keeps `pre` semantics and only adds the break.
                whiteSpace: wrap ? 'pre-wrap' : 'pre',
                wordBreak: wrap ? 'break-all' : 'normal',
                minWidth: wrap ? undefined : 'max-content',
                paddingInlineEnd: '0.5rem',
              }}
            >
              {showLineNumbers && (
                <>
                  <span style={GUTTER_STYLE} aria-hidden="true">
                    {row.oldNo ?? ''}
                  </span>
                  <span style={GUTTER_STYLE} aria-hidden="true">
                    {row.newNo ?? ''}
                  </span>
                </>
              )}
              {/* The line's own text, sign included. An empty line still needs a
                  glyph or the row collapses to zero height and the numbering
                  visibly skips. */}
              <span style={{ flex: '1 1 auto' }}>{row.text || ' '}</span>
            </span>
          ))}
        </pre>
      </div>
    </div>
  );
}

export default DiffView;
