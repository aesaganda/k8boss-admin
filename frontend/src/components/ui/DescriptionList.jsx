/**
 * DescriptionList / InfoGrid — key/value rendering for detail panes.
 *
 * Both take `items: [{ label, value, help, hidden }]`. A `value` of `null`
 * renders through `NullableCell`, so a detail pane full of dashes is honest
 * about which fields could not be read rather than showing blank rows that look
 * like empty strings the object genuinely has.
 *
 * `DescriptionList` is the PatternFly two-column layout for a card body;
 * `InfoGrid` is the denser bordered-row form used inside drawers and side
 * panels where horizontal room is short.
 */
import { isValidElement } from 'react';
import {
  DescriptionList as PFDescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Tooltip,
} from '@patternfly/react-core';
import OutlinedQuestionCircleIcon from '@patternfly/react-icons/dist/esm/icons/outlined-question-circle-icon';
import { NullableCell } from './cells';

function renderValue(item) {
  // A React element is rendered as-is: the caller has already decided how to
  // present it (a StatusBadge, a ResourceLink, a NullableCell of its own).
  if (isValidElement(item.value)) return item.value;
  // Only null takes the dash. `0`, `false` and `''` are answers the object
  // actually gave, and rendering them as "could not be read" would be the same
  // confident wrong answer in the opposite direction.
  if (item.value == null) return <NullableCell value={null} reason={item.reason} />;
  return item.value;
}

function Term({ item }) {
  return (
    <DescriptionListTerm>
      {item.label}
      {item.help && (
        <Tooltip content={item.help}>
          <span className="admin-dl__help" tabIndex={0} aria-label={`About ${item.label}`}>
            <OutlinedQuestionCircleIcon />
          </span>
        </Tooltip>
      )}
    </DescriptionListTerm>
  );
}

export function DescriptionList({ items = [], columns = 1, isHorizontal = true, isCompact = true, className }) {
  const visible = items.filter((item) => item && !item.hidden);
  return (
    <PFDescriptionList
      isHorizontal={isHorizontal}
      isCompact={isCompact}
      columnModifier={columns > 1 ? { default: `${columns}Col` } : undefined}
      className={className}
    >
      {visible.map((item, i) => (
        <DescriptionListGroup key={item.label ?? i}>
          <Term item={item} />
          <DescriptionListDescription>{renderValue(item)}</DescriptionListDescription>
        </DescriptionListGroup>
      ))}
    </PFDescriptionList>
  );
}

export function InfoGrid({ items = [], className }) {
  const visible = items.filter((item) => item && !item.hidden);
  return (
    <dl className={`admin-info-grid ${className || ''}`.trim()}>
      {visible.map((item, i) => (
        <div className="admin-info-grid__row" key={item.label ?? i}>
          <dt className="admin-info-grid__label">
            {item.label}
            {item.help && (
              <Tooltip content={item.help}>
                <span className="admin-dl__help" tabIndex={0} aria-label={`About ${item.label}`}>
                  <OutlinedQuestionCircleIcon />
                </span>
              </Tooltip>
            )}
          </dt>
          <dd className="admin-info-grid__value">{renderValue(item)}</dd>
        </div>
      ))}
    </dl>
  );
}
