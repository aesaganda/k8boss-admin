/**
 * SearchInput / FilterBar / Toolbar — the control strip above a table.
 *
 * `SearchInput` is debounced-by-the-caller: it reports every keystroke, and the
 * table filters client-side against rows already in memory, so there is no
 * request to debounce. When a page wires it to a server-side `labelSelector`
 * instead, it passes `onSearch` and the value only leaves on submit — a
 * fieldSelector round trip per keystroke is how a namespace listing turned into
 * forty API server calls.
 */
import {
  SearchInput as PFSearchInput,
  Toolbar as PFToolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
} from '@patternfly/react-core';

export function SearchInput({
  value = '',
  onChange,
  onSearch,
  placeholder = 'Filter by name…',
  ariaLabel,
  resultsCount,
  className,
  ...rest
}) {
  return (
    <PFSearchInput
      className={className}
      placeholder={placeholder}
      aria-label={ariaLabel || placeholder}
      value={value}
      onChange={(_event, next) => onChange?.(next)}
      onSearch={onSearch ? (_event, next) => onSearch(next) : undefined}
      onClear={() => onChange?.('')}
      resultsCount={resultsCount}
      data-testid="search-input"
      {...rest}
    />
  );
}

/**
 * A labelled row of filter controls (namespace, kind, type…). Labels sit above
 * their control rather than beside it so a row of five filters does not force
 * a horizontal scrollbar on a laptop.
 */
export function FilterBar({ children, className, ariaLabel = 'Filters' }) {
  return (
    <div className={`admin-filterbar ${className || ''}`.trim()} role="group" aria-label={ariaLabel}>
      {children}
    </div>
  );
}

FilterBar.Field = function FilterBarField({ label, htmlFor, children, className }) {
  return (
    <div className={`admin-filterbar__field ${className || ''}`.trim()}>
      <label className="admin-filterbar__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
    </div>
  );
};

/**
 * Toolbar — thin wrapper over PatternFly's, with `Toolbar.Item`,
 * `Toolbar.Group` and `Toolbar.Spacer` so a page does not import four names to
 * lay out one strip. `Toolbar.Spacer` pushes everything after it to the right,
 * which is where the mutating actions live on every page.
 */
export function Toolbar({ children, className, ariaLabel = 'Toolbar', clearAllFilters, ...rest }) {
  return (
    <PFToolbar
      className={className}
      clearAllFilters={clearAllFilters}
      inset={{ default: 'insetNone' }}
      {...rest}
    >
      <ToolbarContent aria-label={ariaLabel}>{children}</ToolbarContent>
    </PFToolbar>
  );
}

Toolbar.Item = function Item({ children, ...rest }) {
  return <ToolbarItem {...rest}>{children}</ToolbarItem>;
};

Toolbar.Group = function Group({ children, ...rest }) {
  return <ToolbarGroup {...rest}>{children}</ToolbarGroup>;
};

// A growing empty item rather than `align: alignEnd` on the trailing group:
// alignment only moves the one item it is set on, so a strip with two trailing
// groups had them align independently and drift apart at narrow widths. One
// spacer that absorbs the slack keeps everything after it as a single block.
Toolbar.Spacer = function Spacer() {
  return <ToolbarItem style={{ flexGrow: 1 }} aria-hidden="true" />;
};
