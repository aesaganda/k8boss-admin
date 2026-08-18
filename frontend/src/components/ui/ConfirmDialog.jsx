/**
 * ConfirmDialog — the second half of the mutation flow in contract §11.3.
 *
 * The flow is fixed: build the request → call with `dryRun: true` → show the
 * unified diff the API server itself projected → the operator confirms → call
 * with `dryRun: false`. There is no button in this console that writes to a
 * cluster without having shown a diff first, and this dialog is where the diff
 * is shown. It therefore takes the whole §1.5 mutation response rather than a
 * message string, so it can enforce the parts of that rule that are easy to
 * forget at a call site:
 *
 *   - `diff.changed === false` means the write is a no-op. The dialog says
 *     "nothing would change" and refuses to offer a confirm button, because
 *     confirming would produce an audit row and a success toast for a write
 *     that did nothing.
 *   - `applied` is `true` only when the write actually reached the cluster. A
 *     successful dry run is `applied: false`, and a dialog that reported
 *     success from it would tell the operator the cluster changed when it did
 *     not. This component never claims success at all — it hands control back.
 *   - `warnings` are the API server's own `Warning:` headers, shown verbatim
 *     above the confirm button, not folded into a toast that appears after the
 *     decision has already been made.
 *
 * `requireTyped` guards the irreversible ones: deleting a namespace or draining
 * a node asks for the object's name to be typed. Slower on purpose.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextInput,
} from '@patternfly/react-core';
import { CodeBlock } from './CodeBlock';

export function ConfirmDialog({
  isOpen,
  title,
  description,
  children,
  /** The §1.5 mutation response from the dry-run call. */
  dryRunResult,
  /** Override: show this unified diff instead of dryRunResult.diff.unified. */
  diff,
  warnings,
  confirmLabel = 'Apply',
  cancelLabel = 'Cancel',
  isDanger = false,
  isConfirming = false,
  /** When set, the operator must type this exact string to enable Confirm. */
  requireTyped,
  onConfirm,
  onCancel,
  variant = 'medium',
}) {
  const [typed, setTyped] = useState('');

  // Reset between openings: a stale confirmation phrase from the previous
  // object left the button enabled for a different target.
  useEffect(() => {
    if (isOpen) setTyped('');
  }, [isOpen, requireTyped]);

  if (!isOpen) return null;

  const unified = diff ?? dryRunResult?.diff?.unified ?? null;
  // `changed` is only meaningful when the backend actually reported it. An
  // absent diff (a dialog used for something that has none) must not be read as
  // "nothing would change".
  const hasDiffVerdict = typeof dryRunResult?.diff?.changed === 'boolean';
  const noOp = hasDiffVerdict && dryRunResult.diff.changed === false;
  const serverWarnings = warnings ?? dryRunResult?.warnings ?? [];
  const typedOk = !requireTyped || typed === requireTyped;
  const confirmDisabled = isConfirming || noOp || !typedOk;

  return (
    <Modal
      isOpen
      variant={variant}
      onClose={onCancel}
      aria-label={typeof title === 'string' ? title : 'Confirm'}
      data-testid="confirm-dialog"
    >
      <ModalHeader title={title} titleIconVariant={isDanger ? 'warning' : undefined} />
      <ModalBody>
        {description && <p className="admin-confirm__description">{description}</p>}
        {children}

        {noOp && (
          <Alert
            isInline
            variant="info"
            title="Nothing would change"
            className="admin-confirm__alert"
          >
            The API server compared this request against the live object and found no difference.
            Applying it would create an audit record for a write with no effect.
          </Alert>
        )}

        {serverWarnings.length > 0 && (
          <Alert
            isInline
            variant="warning"
            title={`The API server returned ${serverWarnings.length} warning${serverWarnings.length === 1 ? '' : 's'}`}
            className="admin-confirm__alert"
          >
            <ul className="admin-confirm__warnings">
              {serverWarnings.map((warning, i) => (
                <li key={i}>{warning}</li>
              ))}
            </ul>
          </Alert>
        )}

        {unified && (
          <div className="admin-confirm__diff">
            <p className="admin-confirm__diff-label">
              This is the difference the API server projected for this write. Nothing has been applied yet.
            </p>
            <CodeBlock code={unified} language="diff" ariaLabel="Projected changes" />
          </div>
        )}

        {requireTyped && (
          <div className="admin-confirm__typed">
            <label htmlFor="confirm-typed">
              Type <code>{requireTyped}</code> to confirm.
            </label>
            <TextInput
              id="confirm-typed"
              value={typed}
              onChange={(_event, next) => setTyped(next)}
              aria-label={`Type ${requireTyped} to confirm`}
              autoComplete="off"
            />
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        <Button
          variant={isDanger ? 'danger' : 'primary'}
          onClick={onConfirm}
          isDisabled={confirmDisabled}
          isLoading={isConfirming}
          data-testid="confirm-dialog-confirm"
        >
          {isConfirming ? 'Applying…' : confirmLabel}
        </Button>
        <Button variant="link" onClick={onCancel} data-testid="confirm-dialog-cancel">
          {cancelLabel}
        </Button>
      </ModalFooter>
    </Modal>
  );
}

export default ConfirmDialog;
