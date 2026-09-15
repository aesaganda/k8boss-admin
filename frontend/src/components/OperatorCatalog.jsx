/**
 * The catalog as tiles — §16.3 rendered the way an operator actually browses it.
 *
 * A table answers "what is the version of the thing I already named". Picking an
 * operator out of several hundred is the other question, and it is answered by
 * scanning: a logo, a name, one line of what it does, and which catalog vouched
 * for it. That is what this renders, and it is why the table did not go away —
 * the toolbar switches between them, because sorting by version and comparing
 * channels is still a table's job.
 *
 * **The icon is the one thing here that is somebody else's asset.** It is
 * fetched per tile from §16.10 rather than carried on the row (a few hundred
 * base64 logos is a listing nobody can use), and it is rendered in an `<img>`,
 * which does not execute script in an SVG. Both halves of that are the
 * contract's, not this component's, but this is where the `<img>` has to be.
 *
 * **A missing icon is the ordinary case, not a failure.** Not one of
 * operatorhub.io's packages publishes one. So the placeholder is a designed
 * state — an initial on a colour derived from the package name, stable across
 * reloads so the same operator is the same tile every time — and never a broken
 * image or an error glyph. `onError` falls back to it too, for the catalog that
 * changes what it publishes between the listing and the tile.
 */
import { useMemo, useState } from 'react';
import { Card, CardBody, Label } from '@patternfly/react-core';

import { portal } from '../api/client';
import { StatusBadge } from './ui';
import { hueOf, installedState } from './operatorCatalogFacets';

const SUBTLE = 'var(--pf-t--global--text--color--subtle, #6a6e73)';

/**
 * One package's logo, or the placeholder standing in for it.
 *
 * `hasIcon` is the contract's promise that §16.10 will answer with an image, so
 * a request is only made when it is true. `failed` covers the case it cannot
 * promise: a catalog re-pointed at a new image between the listing and this
 * render, which is a placeholder rather than a broken-image glyph.
 */
export function PackageIcon({ row, size = 40 }) {
  const [failed, setFailed] = useState(false);
  const showImage = row.hasIcon === true && !failed;
  const label = (row.displayName || row.name || '?').trim();

  if (showImage) {
    return (
      <img
        className="admin-tile__icon"
        src={portal.iconUrl(row)}
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
        loading="lazy"
        onError={() => setFailed(true)}
        data-testid="package-icon"
      />
    );
  }

  return (
    <span
      className="admin-tile__icon admin-tile__icon--placeholder"
      aria-hidden="true"
      data-testid="package-icon-placeholder"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.45,
        background: `hsl(${hueOf(label)} 45% 92%)`,
        color: `hsl(${hueOf(label)} 45% 30%)`,
      }}
    >
      {label.charAt(0).toUpperCase()}
    </span>
  );
}

/* ── The filter rail ────────────────────────────────────────────────────── */

/** How many options a facet shows before it collapses the rest behind a link. */
const FACET_HEAD = 6;

/**
 * One multi-select facet, collapsed to its head.
 *
 * The tail is real and it is long: operatorhub.io's 452 packages come from over
 * two hundred distinct providers, most of them publishing one operator each, and
 * a rail that listed all of them would be several screens of checkboxes nobody
 * scrolls to the bottom of. Options are ordered by count, so the head is the
 * part that filters anything; a ticked option is always shown, because a filter
 * you cannot see is a filter you cannot turn off.
 */
function CheckboxFacet({ title, name, options, selected, onToggle }) {
  const [expanded, setExpanded] = useState(false);
  if (!options.length) return null;

  const visible = expanded
    ? options
    : options.filter((o, i) => i < FACET_HEAD || selected.includes(o.value));
  const hidden = options.length - visible.length;

  return (
    <div className="admin-catalog-filters__group">
      <div className="admin-catalog-filters__title">{title}</div>
      {visible.map((option) => (
        <label className="admin-catalog-filters__check" key={option.value}>
          <input
            type="checkbox"
            checked={selected.includes(option.value)}
            onChange={() => onToggle(name, option.value)}
            data-testid={`catalog-facet-${name}-${option.value}`}
          />
          <span>{option.value}</span>
          <span style={{ color: SUBTLE }}>({option.count})</span>
        </label>
      ))}
      {(hidden > 0 || expanded) && (
        <button
          type="button"
          className="admin-catalog-filters__more"
          onClick={() => setExpanded(!expanded)}
          data-testid={`catalog-facet-more-${name}`}
        >
          {expanded ? 'Show less' : `Show ${hidden} more`}
        </button>
      )}
    </div>
  );
}

/**
 * The left rail: one category at a time, then the checkbox facets.
 *
 * Single-select on categories and multi-select everywhere else is OpenShift's
 * shape, and it is the right one — a package carries several categories, so
 * ticking two of them and intersecting would empty the page for reasons nobody
 * would guess.
 */
export function CatalogFilters({ facets, filters, onChange, total, shown }) {
  const toggle = (name, value) =>
    onChange({
      ...filters,
      [name]: filters[name].includes(value)
        ? filters[name].filter((v) => v !== value)
        : [...filters[name], value],
    });

  return (
    <div className="admin-catalog-filters" data-testid="catalog-filters">
      <div className="admin-catalog-filters__group">
        <button
          type="button"
          className={`admin-catalog-filters__item${filters.category === null ? ' is-active' : ''}`}
          onClick={() => onChange({ ...filters, category: null })}
          data-testid="catalog-category-all"
        >
          All items <span style={{ color: SUBTLE }}>({total})</span>
        </button>
        {facets.categories.map((option) => (
          <button
            type="button"
            key={option.value}
            className={`admin-catalog-filters__item${
              filters.category === option.value ? ' is-active' : ''
            }`}
            onClick={() =>
              onChange({
                ...filters,
                category: filters.category === option.value ? null : option.value,
              })
            }
            data-testid={`catalog-category-${option.value}`}
          >
            {option.value} <span style={{ color: SUBTLE }}>({option.count})</span>
          </button>
        ))}
      </div>

      <CheckboxFacet
        title="Source"
        name="sources"
        options={facets.sources}
        selected={filters.sources}
        onToggle={toggle}
      />
      <CheckboxFacet
        title="Provider"
        name="providers"
        options={facets.providers}
        selected={filters.providers}
        onToggle={toggle}
      />
      <CheckboxFacet
        title="Capability level"
        name="capabilities"
        options={facets.capabilities}
        selected={filters.capabilities}
        onToggle={toggle}
      />
      {/* The install-state facet keeps the tri-state's own words. "Unknown" is
          a Subscription listing that did not answer, and collapsing it into
          "Not installed" here would hand somebody a filtered page that invites
          the second Subscription §16.3 exists to prevent. */}
      <CheckboxFacet
        title="Install state"
        name="installed"
        options={facets.installed}
        selected={filters.installed}
        onToggle={toggle}
      />

      <div className="admin-catalog-filters__group" style={{ color: SUBTLE }}>
        Showing {shown} of {total}
      </div>
    </div>
  );
}

/* ── Tiles ──────────────────────────────────────────────────────────────── */

/**
 * One package.
 *
 * The whole tile is the control, because a card with a small action in the
 * corner is a card people click the middle of. What it opens is the subscribe
 * dialog.
 *
 * **It opens whether or not this deployment permits subscribing**, which is not
 * an oversight in the gating. `POST /portal/subscriptions/plan` writes nothing
 * and is ungated on purpose, and `SubscribeDialog` says so in its own header:
 * the dry run stays readable on a console with `ADMIN_PORTAL_INSTALL_ENABLED`
 * off, "which is exactly when somebody needs to read what enabling it would
 * allow". The control rule 11.4 disables is the one that writes — Confirm,
 * inside the dialog, which is already disabled there with this reason. Greying
 * every tile instead would render an ordinary read-only deployment as a catalog
 * that could not be read.
 */
function PackageTile({ row, onSelect }) {
  const state = installedState(row);

  return (
    <Card isCompact className="admin-tile" data-testid={`package-tile-${row.name}`}>
      <CardBody>
        <button
          type="button"
          className="admin-tile__button"
          onClick={() => onSelect(row)}
          aria-label={`${row.displayName || row.name} — subscribe`}
        >
          <div className="admin-tile__head">
            <PackageIcon row={row} />
            <div className="admin-tile__heading">
              <div className="admin-tile__name">{row.displayName || row.name}</div>
              <div className="admin-tile__provider" style={{ color: SUBTLE }}>
                {row.provider ? `provided by ${row.provider}` : 'no provider named'}
              </div>
            </div>
          </div>

          <div className="admin-tile__labels">
            {/* The catalog that vouched for this package, which is what
                OpenShift's corner badge is. `certified` is the publisher's own
                claim and tri-state: shown only when it is true, because `null`
                is "the catalog said nothing" and rendering that as "not
                certified" is a claim about somebody else's software made from
                an absent annotation. */}
            {(row.catalogDisplayName || row.catalog) && (
              <Label isCompact color="blue">
                {row.catalogDisplayName || row.catalog}
              </Label>
            )}
            {row.certified === true && (
              <Label isCompact color="green">
                Certified
              </Label>
            )}
            {state === 'Installed' && <StatusBadge status="Ready" label="Installed" />}
            {state === 'Unknown' && (
              <StatusBadge
                status="unknown"
                label="Install state unknown"
                tooltip={
                  'The Subscription listing did not answer, so whether this operator is already ' +
                  'installed is unknown. This is NOT "not installed" — subscribing now could put ' +
                  'a second Subscription on top of one that already exists.'
                }
              />
            )}
          </div>

          <div className="admin-tile__summary">
            {row.summary || (
              <span style={{ color: SUBTLE }}>The catalog published no description.</span>
            )}
          </div>
        </button>
      </CardBody>
    </Card>
  );
}

export function PackageTiles({ rows, onSelect }) {
  const tiles = useMemo(
    () => rows.map((row) => <PackageTile key={row.id} row={row} onSelect={onSelect} />),
    [rows, onSelect],
  );
  return (
    <div className="admin-tile-grid" data-testid="package-tiles">
      {tiles}
    </div>
  );
}
