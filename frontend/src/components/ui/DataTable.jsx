/**
 * DataTable — the one table every list page in this console renders through.
 *
 *   <DataTable
 *     columns={[
 *       { key: 'name', title: 'Name', sortable: true, cell: (r) => <ResourceLink .../> },
 *       { key: 'pod_count', title: 'Pods', sortable: true,
 *         cell: (r) => <NullableCell value={r.pod_count} /> },
 *     ]}
 *     rows={rows}
 *     rowKey={(r) => `${r.namespace}/${r.name}`}
 *     loading={loading} error={error}
 *     filterText={search}
 *     onRowClick={(r) => navigate(...)}
 *     actions={(r) => [{ title: 'Delete', onClick: ..., isDanger: true }]}
 *   />
 *
 * Column shape:
 *   key         unique id, and the default row accessor
 *   title       header text
 *   cell(row)   node to render; defaults to the raw value
 *   value(row)  the primitive used for sorting and filtering; defaults to row[key]
 *   sortable    include this column in the sort affordances
 *   width       PatternFly Th width (10|15|20|25|30|35|40|45|50|60|70|80|90|100)
 *   modifier    Th/Td text modifier, e.g. 'truncate', 'nowrap', 'breakWord'
 *   searchable  set false to keep a column out of `filterText` matching
 *
 * Columns are resizable from their header. Every column but the trailing one
 * carries a drag handle at its inline-end edge; the width it is dragged to is
 * remembered per table in localStorage and survives a reload and every poll.
 * `columnWidths.js` says why the trailing column is the one that cannot be
 * pinned, and what happens when the pinned widths outgrow the viewport. Pass
 * `resizableColumns={false}` where that is unwanted, and `tableId` where two
 * tables share an `ariaLabel` and should not share one set of widths. A table
 * that passes neither an `ariaLabel` nor a `tableId` still resizes; it has no
 * identity to file the widths under, so it does not remember them.
 *
 * `density` is the Comfy/Compact choice from `contexts/DensityContext.jsx`.
 * `comfy` — the default — is the rendering this table has always had: a cell
 * wraps onto as many lines as its content needs. `compact` tightens the row
 * padding and holds every row to one line, clipping what does not fit with an
 * ellipsis. It is a prop rather than a context read because this component is
 * a pure design-system piece; the pages that offer the toggle pass their
 * preference down.
 *
 * Sorting is uncontrolled by default. Pass `sort` + `onSort` together to hand
 * sorting to the server (the API returns chunked lists, so a page that pages
 * through `continue` must sort server-side or it sorts one chunk and calls it
 * the answer). Passing `sort` alone is read as an initial sort.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@patternfly/react-core';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { EmptyState, ErrorState, Skeleton } from './states';
import { useColumnWidths } from './columnWidths';
import { sortBy as sortRows } from '../../utils/format';

const SKELETON_ROWS = 5;

function defaultValue(column, row) {
  if (typeof column.value === 'function') return column.value(row);
  return row?.[column.key];
}

function searchHaystack(columns, row) {
  return columns
    .filter((c) => c.searchable !== false)
    .map((c) => {
      const v = defaultValue(c, row);
      if (v == null) return '';
      if (typeof v === 'object') return JSON.stringify(v);
      return String(v);
    })
    // A NUL between the columns, written as an escape rather than as the
    // literal byte it used to be: a source file carrying a raw NUL is
    // classified as binary, and `grep` and `git diff` then refuse to show
    // this component at all. The separator itself stays, because it is the
    // one character no cell value can hold, so a search term can never
    // match across the join between two columns.
    .join(' \u0000 ')
    .toLowerCase();
}

export function DataTable({
  columns = [],
  rows = [],
  rowKey,
  loading = false,
  error = null,
  empty,
  emptyTitle = 'No matching resources',
  emptyDescription,
  onRowClick,
  actions,
  sort,
  onSort,
  filterText = '',
  ariaLabel,
  tableId,
  resizableColumns = true,
  density = 'comfy',
  variant = 'compact',
  isStickyHeader = true,
  onRetry,
  footer,
  className,
}) {
  // Defaulted here rather than in the parameter list, because the same value is
  // the identity column widths are stored under: a table that never named
  // itself would otherwise file its widths under "Resources" and share them
  // with every other unnamed table, silently and across pages.
  const label = ariaLabel ?? 'Resources';

  const controlled = typeof onSort === 'function';
  const [internalSort, setInternalSort] = useState(() => sort ?? { key: null, direction: 'asc' });

  // An uncontrolled table still honours a changed `sort` prop — a page that
  // resets its sort when the namespace scope changes must not be ignored.
  const sortSignature = sort ? `${sort.key}:${sort.direction}` : '';
  const lastSignature = useRef(sortSignature);
  useEffect(() => {
    if (controlled) return;
    if (sortSignature !== lastSignature.current) {
      lastSignature.current = sortSignature;
      if (sort) setInternalSort(sort);
    }
  }, [controlled, sort, sortSignature]);

  const activeSort = controlled ? sort ?? { key: null, direction: 'asc' } : internalSort;

  const handleSort = useCallback(
    (key, direction) => {
      if (controlled) onSort(key, direction);
      else setInternalSort({ key, direction });
    },
    [controlled, onSort],
  );

  const visible = useMemo(() => {
    let out = rows ?? [];

    const needle = (filterText || '').trim().toLowerCase();
    if (needle) {
      // Whitespace-separated terms are ANDed, so "prod checkout" finds the
      // checkout Deployment in prod rather than every row mentioning either.
      const terms = needle.split(/\s+/);
      out = out.filter((row) => {
        const hay = searchHaystack(columns, row);
        return terms.every((t) => hay.includes(t));
      });
    }

    // Server-side sorting means the rows arrived in order; re-sorting them here
    // would sort only the chunk we hold and silently disagree with the server.
    if (!controlled && activeSort?.key) {
      const column = columns.find((c) => c.key === activeSort.key);
      if (column) {
        out = sortRows(out, column.key, activeSort.direction, (row) => defaultValue(column, row));
      }
    }
    return out;
  }, [rows, columns, filterText, controlled, activeSort]);

  const keyOf = useCallback(
    (row, index) => {
      if (typeof rowKey === 'function') return rowKey(row, index);
      if (typeof rowKey === 'string') return row?.[rowKey] ?? index;
      return row?.uid ?? row?.metadata?.uid ?? `${row?.namespace ?? ''}/${row?.name ?? index}`;
    },
    [rowKey],
  );

  const hasActions = typeof actions === 'function';
  const columnCount = columns.length + (hasActions ? 1 : 0);

  // `key` is the column's identity everywhere else in this component, so it is
  // also the identity a stored width is filed under. The index fallback exists
  // only so a column declared without one cannot throw; such a column loses its
  // width when the column set changes, which is the caller's bug, not a crash.
  const columnIds = useMemo(
    () => columns.map((column, index) => column.key ?? `column-${index}`),
    [columns],
  );

  // Everything but the trailing column. That column absorbs the width left
  // over, and it is what makes the pinned columns exactly as wide as they were
  // dragged rather than proportionally rescaled — see `columnWidths.js`. With
  // a row-actions column present it is the one holding nothing but a kebab, so
  // no data column has to give up its handle for the arrangement.
  const resizableKeys = useMemo(() => {
    if (!resizableColumns) return [];
    return hasActions ? columnIds : columnIds.slice(0, -1);
  }, [columnIds, hasActions, resizableColumns]);
  const resizableSet = useMemo(() => new Set(resizableKeys), [resizableKeys]);

  const {
    widths: columnWidths,
    isActive: hasSizedColumns,
    minTableWidth,
    headerRef,
    colRef,
    tableRef,
    resizerProps,
    resetAll: resetColumnWidths,
    focusFirstResizer,
  } = useColumnWidths({
    tableId: tableId ?? ariaLabel,   // undefined for both: resize, do not remember
    columnKeys: resizableKeys,
    enabled: resizableColumns,
    // The row-actions column holds one kebab, so it needs far less room than a
    // data column before the table has to start scrolling sideways.
    trailingMinWidth: hasActions ? 48 : 96,
  });

  // "dense" rather than "compact": `pf-m-compact` is already on this table and
  // means a different thing — PatternFly's cell-padding preset, which BOTH
  // densities start from. A second class by that name in this stylesheet would
  // read as the PatternFly one to whoever edits it next.
  const tableClass = [
    hasSizedColumns ? 'admin-table--sized' : null,
    density === 'compact' ? 'admin-table--dense' : null,
  ]
    .filter(Boolean)
    .join(' ');

  const header = (
    <Thead>
      <Tr>
        {columns.map((column, index) => {
          const columnId = columnIds[index];
          const resizable = resizableSet.has(columnId);
          const columnName = typeof column.title === 'string' ? column.title : columnId;
          return (
            <Th
              key={columnId}
              // The ref is what a drag measures the column from, so it is only
              // wanted on the columns that can be dragged.
              ref={resizable ? headerRef(columnId) : undefined}
              width={column.width}
              modifier={column.modifier}
              // A header cell's accessible name is computed from its contents,
              // and the resize handle is one of them. Without naming the header
              // outright, every cell in the column is announced as "Name, Resize
              // the Name column" — the handle's label read out once per row.
              aria-label={resizable ? columnName : undefined}
              // `additionalContent` renders as a sibling of the header content,
              // which for a sortable column means outside its sort button — a
              // focusable handle nested inside that button would be both invalid
              // and unreachable.
              additionalContent={
                resizable ? <span {...resizerProps(columnId, columnName)} /> : undefined
              }
              sort={
                column.sortable
                  ? {
                      sortBy: {
                        index: activeSort?.key ? columns.findIndex((c) => c.key === activeSort.key) : undefined,
                        direction: activeSort?.direction,
                        defaultDirection: 'asc',
                      },
                      onSort: (_event, _index, direction) => handleSort(column.key, direction),
                      columnIndex: index,
                    }
                  : undefined
              }
            >
              {column.title}
            </Th>
          );
        })}
        {hasActions && <Th screenReaderText="Row actions" />}
      </Tr>
    </Thead>
  );

  const fullWidthCell = (content) => (
    <Tbody>
      <Tr>
        <Td colSpan={columnCount || 1}>{content}</Td>
      </Tr>
    </Tbody>
  );

  let body;
  if (error) {
    // The table keeps its header so the page does not visibly collapse, and the
    // failure is stated in place of the rows rather than as an empty grid.
    body = fullWidthCell(<ErrorState title="Could not load these resources" error={error} onRetry={onRetry} />);
  } else if (loading && !(rows ?? []).length) {
    body = (
      <Tbody>
        {Array.from({ length: SKELETON_ROWS }).map((_, r) => (
          <Tr key={`skeleton-${r}`}>
            {columns.map((column, c) => (
              <Td key={column.key ?? c} dataLabel={typeof column.title === 'string' ? column.title : undefined}>
                <Skeleton height="0.85rem" width={c === 0 ? '60%' : '35%'} screenreaderText="Loading" />
              </Td>
            ))}
            {hasActions && <Td />}
          </Tr>
        ))}
      </Tbody>
    );
  } else if (!visible.length) {
    body = fullWidthCell(
      empty ?? (
        <EmptyState
          title={filterText ? 'No rows match this filter' : emptyTitle}
          description={
            filterText
              ? `Nothing matches “${filterText}”. Clear the filter to see all ${rows.length} rows.`
              : emptyDescription
          }
          variant="sm"
        />
      ),
    );
  } else {
    body = (
      <Tbody>
        {visible.map((row, index) => {
          const clickable = typeof onRowClick === 'function';
          return (
            <Tr
              key={keyOf(row, index)}
              isClickable={clickable}
              // Keyboard parity with the mouse. A clickable row that is only
              // reachable by mouse makes the whole detail view unreachable
              // without one, and rows are the primary navigation in this app.
              tabIndex={clickable ? 0 : undefined}
              onClick={clickable ? () => onRowClick(row) : undefined}
              onKeyDown={
                clickable
                  ? (event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
            >
              {columns.map((column, c) => (
                <Td
                  key={column.key ?? c}
                  dataLabel={typeof column.title === 'string' ? column.title : undefined}
                  modifier={column.modifier}
                >
                  {/*
                    * A block wrapper inside the cell, in both densities, so the
                    * compact one has something to clamp. A `<td>` cannot do it
                    * itself: line clamping needs `display: -webkit-box`, and a
                    * cell that is not `display: table-cell` is not a cell.
                    *
                    * Always rendered, never conditional. It changes nothing in
                    * comfy — a block box where the cell already had an
                    * anonymous one — and a wrapper that appeared only under one
                    * density would make that density a second layout to get
                    * right rather than the same one, clamped.
                    */}
                  <span className="admin-cell-clamp">
                    {typeof column.cell === 'function' ? column.cell(row, index) : defaultValue(column, row)}
                  </span>
                </Td>
              ))}
              {hasActions && (
                <Td
                  isActionCell
                  // Without this, opening the kebab also fires the row click and
                  // the detail page swallowed the menu before it rendered.
                  onClick={(event) => event.stopPropagation()}
                >
                  <ActionsColumn items={actions(row) ?? []} />
                </Td>
              )}
            </Tr>
          );
        })}
      </Tbody>
    );
  }

  return (
    <div className={className}>
      <Table
        aria-label={label}
        // A drag writes the column widths straight onto these elements rather
        // than through a render; `columnWidths.js` says why.
        ref={tableRef}
        variant={variant}
        isStickyHeader={isStickyHeader}
        className={tableClass || undefined}
        // Rendered as an attribute as well as a class so the density a table is
        // actually in can be asserted on, and read off the DOM, without
        // depending on which class name happens to spell it this month.
        data-density={density}
        // Handed to the stylesheet rather than applied here, because the width
        // must not survive into PatternFly's stacked layout — see the
        // `admin-table--sized` rules, which drop it at the same width where
        // PatternFly turns the table into cards with no columns at all.
        style={hasSizedColumns ? { '--admin-table-min-width': `${minTableWidth}px` } : undefined}
        // Announce a refresh-in-progress without swapping the rows out for
        // skeletons: a table that blanks on every 30s poll is unusable.
        aria-busy={loading || undefined}
      >
        {hasSizedColumns && (
          <colgroup>
            {columnIds.map((id) => (
              <col
                key={id}
                ref={resizableSet.has(id) ? colRef(id) : undefined}
                style={columnWidths[id] ? { width: `${columnWidths[id]}px` } : undefined}
              />
            ))}
            {hasActions && <col />}
          </colgroup>
        )}
        {header}
        {body}
      </Table>
      {footer}
      {hasSizedColumns && (
        // Below the table, not above it. Above, this row appears the moment the
        // first drag pins the columns — pushing the header, and the handle the
        // operator is holding, 26px down mid-gesture.
        <div className="admin-table__widths">
          <Button
            variant="link"
            isInline
            // This control removes itself by succeeding, so it hands focus to
            // the first resize handle rather than letting it fall to <body> —
            // from where the next Tab restarts at the top of the page.
            onClick={() => {
              resetColumnWidths();
              focusFirstResizer();
            }}
            data-testid="reset-column-widths"
          >
            Reset column widths
          </Button>
        </div>
      )}
    </div>
  );
}

export default DataTable;
