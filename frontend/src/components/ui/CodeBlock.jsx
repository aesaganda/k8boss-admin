/**
 * CodeBlock — YAML, unified diffs and log excerpts.
 *
 * Diffs are the reason this exists in the shape it does. §1.5 requires the UI
 * to show the backend's own unified diff before any confirming write, so the
 * diff must be rendered verbatim and legibly: `language="diff"` colours the
 * `+`/`-` gutters without reformatting or re-wrapping a single character.
 * Re-generating or prettifying the diff client-side would mean confirming
 * against something other than what the API server projected.
 *
 * `language="yaml"` numbers the lines and colours the syntax, with the same
 * tokenizer the manifest editor uses. That it is the same one is the point: an
 * object read on a detail page and the same object open in the editor have to
 * look like the same document, or the operator is comparing two renderings
 * rather than two versions. The read-only side is a single layer of text — no
 * transparent textarea over it — so it carries none of the editor's alignment
 * constraint, and the line numbers are ordinary content in the same flow.
 *
 * The numbers are `aria-hidden` and unselectable. A screen reader reading "1
 * apiVersion 2 kind" has been handed a worse document than the one on screen,
 * and a manifest copied out with its line numbers interleaved is not a manifest
 * anybody can apply.
 */
import { useCallback, useMemo, useState } from 'react';
import {
  ClipboardCopyButton,
  CodeBlock as PFCodeBlock,
  CodeBlockAction,
  CodeBlockCode,
} from '@patternfly/react-core';

import { HIGHLIGHT_MAX_LINES, tokenizeYaml } from '../yamlSyntax';

const DIFF_CLASS = {
  '+': 'admin-diff__add',
  '-': 'admin-diff__del',
  '@': 'admin-diff__hunk',
};

function renderDiff(code) {
  return String(code)
    .split('\n')
    .map((line, i) => {
      // `+++`/`---` are the file headers, not additions; they get the hunk
      // treatment so a diff does not open with two lines of green and red.
      const marker = line.startsWith('+++') || line.startsWith('---') ? '@' : line.charAt(0);
      const cls = DIFF_CLASS[marker] || '';
      return (
        <span key={i} className={`admin-diff__line ${cls}`.trim()}>
          {line || ' '}
          {'\n'}
        </span>
      );
    });
}

/**
 * Numbered, coloured YAML.
 *
 * Past `HIGHLIGHT_MAX_LINES` the colouring is dropped and the numbering is not:
 * a generated CRD is exactly the document where "which line am I looking at"
 * matters most, and it is also the one where a token per word is thousands of
 * elements re-created on every refresh.
 */
function renderYaml(code) {
  const source = String(code);
  const lines = source.split('\n');
  const coloured = lines.length <= HIGHLIGHT_MAX_LINES;
  const tokenized = coloured ? tokenizeYaml(source) : null;

  return lines.map((line, index) => (
    <span key={index} className="admin-yaml-code__line">
      <span className="admin-yaml-code__no" aria-hidden="true">
        {index + 1}
      </span>
      <span className="admin-yaml-code__text">
        {tokenized
          ? tokenized[index].map((token, i) =>
              token.kind ? (
                <span key={i} className={`admin-yaml__t--${token.kind}`}>
                  {token.text}
                </span>
              ) : (
                token.text
              ),
            )
          : line}
      </span>
    </span>
  ));
}

export function CodeBlock({
  code = '',
  language,
  copyable = true,
  maxHeight = 420,
  ariaLabel,
  actions,
  className,
}) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(String(code));
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      // Clipboard access is refused on insecure origins and in some locked-down
      // browsers. Saying nothing would look like a successful copy, and the
      // operator would paste stale YAML into a cluster.
      setCopied(false);
      window.prompt('Copy is blocked by the browser. Select and copy manually:', String(code));
    }
  }, [code]);

  const isDiff = language === 'diff';
  const isYaml = language === 'yaml';

  // Memoised on the text: an object that is re-read every few seconds and comes
  // back byte-identical must not re-render a thousand line elements to say so.
  const body = useMemo(() => {
    if (isDiff) return renderDiff(code);
    if (isYaml) return renderYaml(code);
    return String(code);
  }, [code, isDiff, isYaml]);

  const lineCount = useMemo(() => (isYaml ? String(code).split('\n').length : 0), [code, isYaml]);

  return (
    <PFCodeBlock
      className={`admin-codeblock ${isDiff ? 'admin-codeblock--diff' : ''} ${
        isYaml ? 'admin-codeblock--yaml' : ''
      } ${className || ''}`.trim()}
      actions={
        (copyable || actions) && (
          <>
            {copyable && (
              <CodeBlockAction>
                <ClipboardCopyButton
                  id={`copy-${ariaLabel || language || 'code'}`}
                  textId={`code-${ariaLabel || language || 'code'}`}
                  aria-label="Copy to clipboard"
                  onClick={copy}
                  exitDelay={1200}
                  variant="plain"
                >
                  {copied ? 'Copied' : 'Copy'}
                </ClipboardCopyButton>
              </CodeBlockAction>
            )}
            {actions && <CodeBlockAction>{actions}</CodeBlockAction>}
          </>
        )
      }
    >
      <CodeBlockCode
        style={{
          maxHeight,
          overflow: 'auto',
          // The gutter is exactly as wide as this document's largest line
          // number needs, so a 40-line object does not carry the indent of a
          // 4000-line one.
          ...(isYaml ? { '--admin-yaml-digits': String(lineCount).length } : null),
        }}
        aria-label={ariaLabel || (isDiff ? 'Unified diff' : 'Code')}
        data-testid="code-block"
      >
        {body}
      </CodeBlockCode>
    </PFCodeBlock>
  );
}

export default CodeBlock;
