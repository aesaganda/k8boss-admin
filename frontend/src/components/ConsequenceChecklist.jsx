/**
 * ConsequenceChecklist — the checkboxes for §11.3's consequence handshake.
 *
 * Several endpoints (§18, §20, §21, §22, §24) return a `consequences` list, each
 * entry a `{ code, label, consequence, mitigation }`, and refuse the write until
 * the caller sends every `code` back. The UI half of that is the same in every
 * dialog: the label is the sentence, the description is what happens and what to
 * do instead. The state rule and the confirm guard live in `consequences.js`.
 *
 * Extracted here for §24, which adds two dialogs at once and would otherwise
 * have made seven copies of it. The five older dialogs still carry their own —
 * converting them is a separate change from adding a feature, and doing both in
 * one diff makes the feature impossible to review.
 */
import { Alert, Checkbox } from '@patternfly/react-core';

/**
 * The checkboxes. Renders nothing when there is nothing to acknowledge, so a
 * caller can drop it in unconditionally.
 *
 * `idPrefix` namespaces the DOM ids, because two of these can be on one page.
 */
export default function ConsequenceChecklist({
  consequences,
  acknowledged,
  onChange,
  idPrefix,
  title,
}) {
  const entries = consequences ?? [];
  if (!entries.length) return null;

  const heading =
    title ??
    (entries.length === 1
      ? 'One thing about this change needs acknowledging'
      : `${entries.length} things about this change need acknowledging`);

  return (
    <Alert
      isInline
      variant="warning"
      className="admin-confirm__alert"
      data-testid={`${idPrefix}-consequences`}
      title={heading}
    >
      {entries.map((entry) => (
        <div key={entry.code} style={{ marginBottom: '0.75rem' }}>
          <Checkbox
            id={`${idPrefix}-ack-${entry.code}`}
            data-testid={`${idPrefix}-ack-${entry.code}`}
            isChecked={acknowledged.includes(entry.code)}
            onChange={(_e, checked) =>
              onChange(
                checked
                  ? [...acknowledged, entry.code]
                  : acknowledged.filter((code) => code !== entry.code),
              )
            }
            label={<strong>{entry.label}</strong>}
            description={
              <>
                <div>{entry.consequence}</div>
                <div style={{ marginTop: '0.25rem' }}>
                  <em>{entry.mitigation}</em>
                </div>
              </>
            }
          />
        </div>
      ))}
    </Alert>
  );
}
