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
import ColumnsIcon from '@patternfly/react-icons/dist/esm/icons/columns-icon';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { EmptyState, ErrorState, Skeleton } from './states';
import { useColumnWidths } from './columnWidths';
import { useColumnVisibility } from './columnVisibility';
import { FacetFilter, FilterChips, ManageColumnsDialog } from './TableControls';
import {
  applyFacets,
  facetOptionsWithCounts,
  readFacets,
  selectedChips,
  selectionCount,
  toggleSelection,
} from './tableFilters';
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
  columns: declaredColumns = [],
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
  manageableColumns = false,
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

  // Hidden columns are remembered under the same identity as the widths, and
  // for the same reason: two unnamed tables must not share one preference.
  const {
    visibleColumns: columns,
    hiddenSet,
    setHidden,
    lockedKey,
    isCustomised: hasHiddenColumns,
  } = useColumnVisibility({
    tableId: tableId ?? ariaLabel,
    columns: declaredColumns,
    enabled: manageableColumns,
  });
  const [columnsDialogOpen, setColumnsDialogOpen] = useState(false);

  // Read from every declared column, not the visible ones: filtering by a
  // column you chose not to display is legitimate, and dropping the filter
  // along with the column would put rows back on screen without saying so.
  const facets = useMemo(() => readFacets(declaredColumns), [declaredColumns]);

  // Not persisted, and that is the decision rather than the shortcut. A
  // remembered filter is rows missing from a table that looks complete, days
  // later, with nothing on screen having been clicked — which is how somebody
  // concludes a namespace is empty and deletes it. `columnVisibility.js` says
  // why a hidden column is a different bargain.
  const [selections, setSelections] = useState({});

  // A filter set on one resource kind means nothing on the next, and a
  // selection that survives the switch silently hides rows in a table the
  // operator has only just opened.
  const facetSignature = facets.map((facet) => facet.key).join('|');
  const lastFacetSignature = useRef(facetSignature);
  useEffect(() => {
    if (lastFacetSignature.current === facetSignature) return;
    lastFacetSignature.current = facetSignature;
    setSelections({});
  }, [facetSignature]);

  // Text first, and the facets are counted against its result: the search box
  // narrows what the menu is describing, rather than the two disagreeing.
  const searched = useMemo(() => {
    const needle = (filterText || '').trim().toLowerCase();
    if (!needle) return rows ?? [];
    // Whitespace-separated terms are ANDed, so "prod checkout" finds the
    // checkout Deployment in prod rather than every row mentioning either.
    // Matched against every declared column, including hidden ones — a search
    // whose results changed with the column dialog would be a third thing to
    // reason about.
    const terms = needle.split(/\s+/);
    return (rows ?? []).filter((row) => {
      const hay = searchHaystack(declaredColumns, row);
      return terms.every((t) => hay.includes(t));
    });
  }, [rows, declaredColumns, filterText]);

  const facetViews = useMemo(
    () =>
      facets.map((facet) => ({
        facet,
        options: facetOptionsWithCounts(facet, searched, facets, selections),
      })),
    [facets, searched, selections],
  );

  const chips = useMemo(
    () => selectedChips(facets, selections, searched),
    [facets, selections, searched],
  );

  const visible = useMemo(() => {
    let out = applyFacets(searched, facets, selections);

    // Server-side sorting means the rows arrived in order; re-sorting them here
    // would sort only the chunk we hold and silently disagree with the server.
    if (!controlled && activeSort?.key) {
      const column = columns.find((c) => c.key === activeSort.key);
      if (column) {
        out = sortRows(out, column.key, activeSort.direction, (row) => defaultValue(column, row));
      }
    }
    return out;
  }, [searched, facets, selections, columns, controlled, activeSort]);

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

  const activeSelections = selectionCount(selections);
  const narrowed = visible.length !== (rows ?? []).length;
  const showControls = facets.length > 0 || manageableColumns;

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
  if (error?.code === 'unsupported') {
    // §1.2: `unsupported` is not an error — it is "this cluster does not serve
    // that API" (no Ingress CRDs, no metrics.k8s.io, and now no Gateway API /
    // VPA / VolumeAttributesClasses on plenty of real clusters). Until this
    // branch existed, a primary list call answering 501 fell into the `error`
    // case below and rendered the same red "Could not load these resources"
    // panel as an RBAC denial or a network failure — training operators to
    // ignore red on the one page in the console where red is supposed to mean
    // something. `error.hint` carries whatever `resolve()` could say about it
    // (e.g. "That group serves v1beta1 on this cluster"), which is worth
    // showing; there is nothing to retry, so no retry action is offered.
    body = fullWidthCell(
      <EmptyState
        title="Not present on this cluster"
        description={
          error.hint || 'This cluster does not serve that API. Nothing is wrong — there is simply nothing to show.'
        }
      />,
    );
  } else if (error) {
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
    // Which filter emptied the table, named. "No resources" under a filter the
    // operator set ten minutes ago reads as a cluster with nothing in it.
    const filtered = Boolean(filterText) || activeSelections > 0;
    let emptyReason = emptyDescription;
    if (filterText && activeSelections) {
      emptyReason = `Nothing matches “${filterText}” with the selected filters. Clear them to see all ${rows.length} rows.`;
    } else if (filterText) {
      emptyReason = `Nothing matches “${filterText}”. Clear the filter to see all ${rows.length} rows.`;
    } else if (activeSelections) {
      emptyReason = `No row matches the selected filters. Clear them to see all ${rows.length} rows.`;
    }
    body = fullWidthCell(
      empty ?? (
        <EmptyState
          title={filtered ? 'No rows match this filter' : emptyTitle}
          description={emptyReason}
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
      {showControls && (
        <div className="admin-table-controls">
          {facets.length > 0 && (
            <FacetFilter
              facets={facetViews}
              selections={selections}
              rowCount={(rows ?? []).length}
              onToggle={(facetKey, value) =>
                setSelections((current) => toggleSelection(current, facetKey, value))
              }
            />
          )}
          <FilterChips
            chips={chips}
            onRemove={(facetKey, value) =>
              setSelections((current) => toggleSelection(current, facetKey, value))
            }
            onClearAll={() => setSelections({})}
          />
          {/* What the controls above are costing, in rows. The chips say what
              is filtered; this says how much of the listing that leaves, so a
              short table is never mistaken for a short cluster. */}
          {narrowed && (
            <span className="admin-table-controls__count" data-testid="table-row-count">
              {`Showing ${visible.length} of ${(rows ?? []).length}`}
            </span>
          )}
          <span className="admin-table-controls__spacer" />
          {manageableColumns && (
            <Button
              variant="link"
              isInline
              icon={<ColumnsIcon />}
              onClick={() => setColumnsDialogOpen(true)}
              data-testid="manage-columns"
            >
              {hasHiddenColumns
                ? `Manage columns (${columns.length} of ${declaredColumns.length})`
                : 'Manage columns'}
            </Button>
          )}
        </div>
      )}

      {columnsDialogOpen && (
        <ManageColumnsDialog
          isOpen
          columns={declaredColumns}
          hiddenSet={hiddenSet}
          lockedKey={lockedKey}
          onClose={() => setColumnsDialogOpen(false)}
          onSave={(hidden) => {
            setHidden(hidden);
            setColumnsDialogOpen(false);
          }}
        />
      )}

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
