/**
 * The control strip above a table: the Filter menu, the chips saying what is
 * filtered, and Manage columns.
 *
 * Both controls are rendered by `DataTable` rather than placed by each page,
 * for the same reason `PartialBanner` is a component rather than a review
 * note: a filter that some list pages have and others do not is a filter
 * nobody looks for. The state they edit lives in `tableFilters.js` (pure, and
 * deliberately not persisted) and `columnVisibility.js` (persisted per table).
 *
 * The strip states what it is doing to the rows. A menu that quietly removed
 * two thirds of a listing would be this project's defect standard wearing a
 * checkbox: the chips name every active value, "Showing 3 of 76" names the
 * cost, and one link clears the lot.
 */
import { useEffect, useState } from 'react';
import {
  Button,
  Divider,
  Label,
  LabelGroup,
  Menu,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuList,
  MenuToggle,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Popper,
  Checkbox,
} from '@patternfly/react-core';
import FilterIcon from '@patternfly/react-icons/dist/esm/icons/filter-icon';

/**
 * The Filter menu.
 *
 * `facets` is `[{ facet, options }]` where each option already carries its
 * count — the counting rules, and why they are what they are, are in
 * `tableFilters.js`.
 */
export function FacetFilter({ facets, selections, onToggle, rowCount }) {
  const [open, setOpen] = useState(false);
  const [toggleElement, setToggleElement] = useState(null);
  const [menuElement, setMenuElement] = useState(null);

  // A menu of checkboxes must not close on the first tick — the whole point is
  // choosing several. PatternFly's Select closes on select, so this is a Menu
  // in a Popper, which leaves dismissal to us: outside click and Escape.
  useEffect(() => {
    if (!open) return undefined;
    const onDocumentClick = (event) => {
      if (menuElement?.contains(event.target) || toggleElement?.contains(event.target)) return;
      setOpen(false);
    };
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      // Back to the toggle rather than to <body>, from where the next Tab
      // restarts at the top of the page.
      toggleElement?.focus();
    };
    document.addEventListener('click', onDocumentClick);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('click', onDocumentClick);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, menuElement, toggleElement]);

  const toggle = (
    <MenuToggle
      ref={setToggleElement}
      icon={<FilterIcon />}
      isExpanded={open}
      onClick={() => setOpen((value) => !value)}
      data-testid="facet-filter-toggle"
    >
      Filter
    </MenuToggle>
  );

  const menu = (
    <Menu ref={setMenuElement} role="menu" className="admin-facet-menu" containsCheckbox>
      <MenuContent>
        {facets.map(({ facet, options }, index) => (
          <MenuGroup key={facet.key} label={facet.label}>
            {index > 0 && <Divider component="li" />}
            <MenuList>
              {options.map((option) => {
                const selected = (selections[facet.key] ?? []).includes(option.value);
                return (
                  <MenuItem
                    key={option.value}
                    hasCheckbox
                    isSelected={selected}
                    description={option.description}
                    onClick={() => onToggle(facet.key, option.value)}
                    data-testid={`facet-${facet.key}-${option.value}`}
                  >
                    <span className="admin-facet-menu__option">
                      <span>{option.label}</span>
                      {/* The count is the point of the menu, so it is text
                          rather than a badge: a badge is announced as
                          decoration by several screen readers. */}
                      <span className="admin-facet-menu__count">{option.count}</span>
                    </span>
                  </MenuItem>
                );
              })}
            </MenuList>
            {/* A facet may need a sentence of its own — the pod Status list
                filters on what the pill says rather than on the raw phase, and
                a menu that did not say so would look like it was ignoring
                `phase`. */}
            {facet.note && <div className="admin-facet-menu__note">{facet.note}</div>}
          </MenuGroup>
        ))}
        <Divider component="li" />
        {/* Said once, next to the numbers it qualifies. These are counts of the
            rows this page has loaded — a truncated listing has more behind it,
            and a count presented as a cluster total would be a wrong answer
            delivered confidently. */}
        <div className="admin-facet-menu__note">
          {`Counted over the ${rowCount} row${rowCount === 1 ? '' : 's'} loaded here, not the whole cluster.`}
        </div>
      </MenuContent>
    </Menu>
  );

  return (
    <Popper
      trigger={toggle}
      triggerRef={() => toggleElement}
      popper={menu}
      popperRef={() => menuElement}
      isVisible={open}
      appendTo={() => document.body}
    />
  );
}

/** One removable chip per selected value, plus the way out of all of them. */
export function FilterChips({ chips, onRemove, onClearAll }) {
  if (!chips.length) return null;
  return (
    <span className="admin-table-controls__chips">
      <LabelGroup categoryName="Filters" numLabels={6}>
        {chips.map((chip) => (
          <Label
            key={`${chip.facetKey}:${chip.value}`}
            variant="outline"
            onClose={() => onRemove(chip.facetKey, chip.value)}
            closeBtnAriaLabel={`Remove filter ${chip.facetLabel}: ${chip.label}`}
            data-testid={`filter-chip-${chip.facetKey}-${chip.value}`}
          >
            {`${chip.facetLabel}: ${chip.label}`}
          </Label>
        ))}
      </LabelGroup>
      <Button variant="link" isInline onClick={onClearAll} data-testid="clear-all-filters">
        Clear all filters
      </Button>
    </span>
  );
}

/**
 * Manage columns.
 *
 * Staged rather than live: the checkboxes edit a copy and Save commits it, so
 * an operator can change their mind about a five-column edit without undoing it
 * one box at a time. Cancel and Escape both discard.
 */
export function ManageColumnsDialog({ isOpen, columns, hiddenSet, lockedKey, onSave, onClose }) {
  const [staged, setStaged] = useState(() => new Set(hiddenSet));

  // Re-seeded each time it opens, not on every render: a poll that re-renders
  // the table underneath must not throw away a half-made edit.
  useEffect(() => {
    if (isOpen) setStaged(new Set(hiddenSet));
  }, [isOpen, hiddenSet]);

  if (!isOpen) return null;

  const toggle = (key) => {
    setStaged((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const shown = columns.filter((column) => !staged.has(column.key)).length;

  return (
    <Modal
      isOpen
      variant="small"
      onClose={onClose}
      aria-label="Manage columns"
      data-testid="manage-columns-dialog"
    >
      <ModalHeader title="Manage columns" />
      <ModalBody>
        <p className="admin-manage-columns__intro">
          Selected columns appear in the table. Hiding one hides it here only — nothing is filtered
          out of the rows, and the data is still read from the cluster.
        </p>
        <div className="admin-manage-columns__list" role="group" aria-label="Columns">
          {columns.map((column) => {
            const locked = column.key === lockedKey;
            const label = typeof column.title === 'string' ? column.title : column.key;
            return (
              <Checkbox
                key={column.key}
                id={`manage-column-${column.key}`}
                label={label}
                description={
                  locked
                    ? 'Always shown — a row that cannot be named cannot be acted on.'
                    : undefined
                }
                isChecked={locked || !staged.has(column.key)}
                isDisabled={locked}
                onChange={() => toggle(column.key)}
                data-testid={`manage-column-${column.key}`}
              />
            );
          })}
        </div>
        <p className="admin-manage-columns__count">{`${shown} of ${columns.length} columns shown`}</p>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={() => onSave([...staged])} data-testid="manage-columns-save">
          Save
        </Button>
        <Button variant="link" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="link"
          isInline
          className="admin-manage-columns__restore"
          onClick={() => setStaged(new Set())}
          data-testid="manage-columns-restore"
        >
          Restore default columns
        </Button>
      </ModalFooter>
    </Modal>
  );
}
