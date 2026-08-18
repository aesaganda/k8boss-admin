/**
 * CodeBlock — YAML, unified diffs and log excerpts.
 *
 * Diffs are the reason this exists in the shape it does. §1.5 requires the UI
 * to show the backend's own unified diff before any confirming write, so the
 * diff must be rendered verbatim and legibly: `language="diff"` colours the
 * `+`/`-` gutters without reformatting or re-wrapping a single character.
 * Re-generating or prettifying the diff client-side would mean confirming
 * against something other than what the API server projected.
 */
import { useCallback, useState } from 'react';
import {
  ClipboardCopyButton,
  CodeBlock as PFCodeBlock,
  CodeBlockAction,
  CodeBlockCode,
} from '@patternfly/react-core';

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

  return (
    <PFCodeBlock
      className={`admin-codeblock ${isDiff ? 'admin-codeblock--diff' : ''} ${className || ''}`.trim()}
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
        style={{ maxHeight, overflow: 'auto' }}
        aria-label={ariaLabel || (isDiff ? 'Unified diff' : 'Code')}
        data-testid="code-block"
      >
        {isDiff ? renderDiff(code) : String(code)}
      </CodeBlockCode>
    </PFCodeBlock>
  );
}

export default CodeBlock;
