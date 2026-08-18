/**
 * MetricCard / StatGrid — the number tiles on Overview and the detail pages.
 *
 * Values go through `NullableCell`, which is the whole point: §3's overview
 * collects each sub-object independently and sets the key to `null` when its
 * collector failed. A tile that rendered `{value ?? 0}` would report a cluster
 * with zero nodes when what actually happened is that we could not count them.
 */
import { Card, CardBody, Flex, FlexItem, Tooltip } from '@patternfly/react-core';
import OutlinedQuestionCircleIcon from '@patternfly/react-icons/dist/esm/icons/outlined-question-circle-icon';
import { NullableCell } from './cells';

/**
 *   <MetricCard label="Nodes" value={overview.nodes?.total} sub="11 ready" />
 *   <MetricCard label="Capacity" value={formatBytes(cap)} reason="Node listing was denied." />
 *
 * `accent` tints the left edge: 'success' | 'warning' | 'danger' | 'info'.
 * `reason` is passed straight through to the dash's tooltip, so a tile can say
 * *why* it has no number instead of just that it has none.
 */
export function MetricCard({
  label,
  value,
  unit,
  sub,
  reason,
  accent,
  icon,
  help,
  onClick,
  format,
  className,
}) {
  const body = (
    <CardBody className="admin-metric__body">
      <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }} alignItems={{ default: 'alignItemsCenter' }}>
        <FlexItem>
          <span className="admin-metric__label">
            {label}
            {help && (
              <Tooltip content={help}>
                <span className="admin-metric__help" tabIndex={0} aria-label={`About ${label}`}>
                  <OutlinedQuestionCircleIcon />
                </span>
              </Tooltip>
            )}
          </span>
        </FlexItem>
        {icon && <FlexItem className="admin-metric__icon">{icon}</FlexItem>}
      </Flex>
      <div className="admin-metric__value">
        <NullableCell value={value} unit={unit} reason={reason} format={format} />
      </div>
      {sub && <div className="admin-metric__sub">{sub}</div>}
    </CardBody>
  );

  const classes = ['admin-metric', accent ? `admin-metric--${accent}` : '', className || '']
    .filter(Boolean)
    .join(' ');

  return (
    <Card
      isCompact
      className={classes}
      data-testid="metric-card"
      onClick={onClick}
      // A clickable card is a control, so it is announced and reachable as one.
      // Without the role and tabIndex the drill-down it offers exists for the
      // mouse only, and a screen reader is told nothing is interactive here.
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onClick(event);
              }
            }
          : undefined
      }
    >
      {body}
    </Card>
  );
}

/**
 * Responsive tile row. Uses CSS grid with an auto-fit minimum rather than a
 * fixed column count so a four-tile row and a seven-tile row both stay readable
 * without every caller picking a breakpoint.
 */
export function StatGrid({ children, minWidth = 190, className }) {
  return (
    <div
      className={`admin-stat-grid ${className || ''}`.trim()}
      style={{ '--admin-stat-min': `${minWidth}px` }}
    >
      {children}
    </div>
  );
}
