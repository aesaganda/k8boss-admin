/*
 * Rule 11.4, as two controls.
 *
 * > *A control the caller may not use is rendered **disabled with the reason**,
 * > never hidden.*
 *
 * They live in the design system rather than beside the pages that use them for
 * the same reason `PartialBanner` and `NullableCell` do: a contract rule that is
 * only a convention is a rule that the next page forgets. Here it is a component
 * with a `gate` prop, so the way to render an action is also the way to render
 * why it is unavailable — and a control that skipped it would have to be written
 * differently from every other one in the app.
 *
 * `gate` is `{ allowed, reason }` from `useGates`, which produces **six** kinds
 * of `allowed: false` and only one of them is a denial. Neither control here
 * interprets the sentence; both display it verbatim. Flattening "the authorizer
 * could not decide this" into "forbidden" sends an operator to edit a ClusterRole
 * that is already correct.
 */
import { Button, Tooltip } from '@patternfly/react-core';

/**
 * A button that states why it cannot be pressed.
 *
 *   <ActionButton gate={gate('scale')} onClick={...}>Scale</ActionButton>
 *
 * When it is not allowed the button stays visible and focusable
 * (`isAriaDisabled`) with the reason in a tooltip, which is the whole of rule
 * 11.4: an operator must be able to see that the action exists and why it is
 * unavailable.
 */
export function ActionButton({
  gate,
  onClick,
  children,
  variant = 'secondary',
  icon,
  isDanger = false,
  isLoading = false,
  size,
  ariaLabel,
}) {
  const allowed = gate ? gate.allowed : true;
  const button = (
    <Button
      variant={isDanger ? 'danger' : variant}
      icon={icon}
      size={size}
      isLoading={isLoading}
      isAriaDisabled={!allowed || isLoading}
      aria-label={ariaLabel}
      onClick={allowed && !isLoading ? onClick : undefined}
      data-testid="action-button"
      data-allowed={allowed ? 'true' : 'false'}
    >
      {children}
    </Button>
  );
  if (allowed) return button;
  // The span guarantees Tooltip a DOM node to attach its ref to, and keeps the
  // explanation reachable when the button itself is aria-disabled.
  return (
    <Tooltip content={gate.reason}>
      <span className="admin-gated-action">{button}</span>
    </Tooltip>
  );
}

/**
 * One entry for `DataTable`'s `actions` kebab, gated the same way.
 *
 * Not a component — `ActionsColumn` takes plain objects — so it renders the
 * reason with a `title` attribute on the label rather than a `Tooltip`. That is
 * deliberate: PatternFly's menu closes the item's tooltip along with the menu on
 * some interactions, and a reason the operator cannot read is not a reason.
 */
export function menuAction(label, gate, onClick, { isDanger = false } = {}) {
  const allowed = gate ? gate.allowed : true;
  return {
    title: allowed ? label : <span title={gate.reason}>{label}</span>,
    isDisabled: !allowed,
    isDanger,
    onClick: allowed ? onClick : undefined,
  };
}
