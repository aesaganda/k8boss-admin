/**
 * Empty / error / loading primitives.
 *
 * One set, used everywhere, so that "there is nothing here" and "we could not
 * look" never render as the same grey box. `EmptyState` is a statement about
 * the cluster; `ErrorState` is a statement about us. Anything in between —
 * a partially readable listing — belongs in `PartialBanner` above the data,
 * not here instead of it.
 */
import {
  Bullseye,
  Button,
  EmptyState as PFEmptyState,
  EmptyStateActions,
  EmptyStateBody,
  EmptyStateFooter,
  Skeleton as PFSkeleton,
  Spinner,
} from '@patternfly/react-core';
import CubesIcon from '@patternfly/react-icons/dist/esm/icons/cubes-icon';

export function EmptyState({
  title = 'Nothing to show',
  description,
  icon = CubesIcon,
  action,
  variant,
  children,
  ...rest
}) {
  return (
    // h2 rather than h3: an empty state usually sits directly under the page's
    // h1, and h3 skipped a heading level for anyone navigating by headings.
    <PFEmptyState
      headingLevel="h2"
      titleText={title}
      icon={icon}
      variant={variant}
      role="status"
      data-testid="empty-state"
      {...rest}
    >
      {(description || children) && (
        <EmptyStateBody>
          {description}
          {children}
        </EmptyStateBody>
      )}
      {action && (
        <EmptyStateFooter>
          <EmptyStateActions>{action}</EmptyStateActions>
        </EmptyStateFooter>
      )}
    </PFEmptyState>
  );
}

/**
 * A failure, named.
 *
 * Accepts an `ApiError` directly and renders its three separate facts: the
 * stable code (what class of failure), the message (what happened) and the
 * hint (what to do about it). Flattening those into one sentence is how
 * "Grant `patch` on `apps/deployments`" ends up truncated out of a toast and
 * the operator is left guessing which permission is missing.
 */
export function ErrorState({
  title = 'Could not load this',
  error,
  detail,
  onRetry,
  children,
  ...rest
}) {
  const message = detail ?? error?.message ?? null;
  const hint = error?.hint ?? null;
  const code = error?.code ?? null;

  return (
    <PFEmptyState
      headingLevel="h2"
      titleText={title}
      status="danger"
      role="alert"
      data-testid="error-state"
      {...rest}
    >
      <EmptyStateBody>
        {message && <div>{message}</div>}
        {hint && <div className="admin-error-hint">{hint}</div>}
        {code && (
          <div className="admin-error-code">
            Error code: <code>{code}</code>
          </div>
        )}
        {children}
      </EmptyStateBody>
      {onRetry && (
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button variant="secondary" onClick={onRetry}>
              Try again
            </Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </PFEmptyState>
  );
}

export function LoadingState({ label = 'Loading…', minHeight = 160 }) {
  return (
    <Bullseye style={{ minHeight }} data-testid="loading-state">
      <div className="admin-loading">
        <Spinner size="lg" aria-label={label} />
        <span className="admin-loading__label" aria-live="polite">
          {label}
        </span>
      </div>
    </Bullseye>
  );
}

/**
 * Skeleton placeholder. `lines` > 1 renders a stack with decreasing widths so a
 * loading block looks like text rather than a solid slab.
 */
export function Skeleton({ width, height = '1rem', lines = 1, screenreaderText, className, style }) {
  if (lines > 1) {
    return (
      <div className={className} aria-hidden="true" style={style}>
        {Array.from({ length: lines }).map((_, i) => (
          <PFSkeleton
            key={i}
            width={`${Math.max(35, 100 - i * 18)}%`}
            height={height}
            style={{ marginBottom: 'var(--pf-t--global--spacer--sm, 0.5rem)' }}
          />
        ))}
      </div>
    );
  }
  return (
    <PFSkeleton
      className={className}
      width={typeof width === 'number' ? `${width}px` : width}
      height={typeof height === 'number' ? `${height}px` : height}
      screenreaderText={screenreaderText}
      style={style}
    />
  );
}
