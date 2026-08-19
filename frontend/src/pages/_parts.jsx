/**
 * Shared presentation for the resource pages.
 *
 * Everything here exists because it encodes a contract rule that is easy to get
 * wrong once per page and impossible to get wrong once in total:
 *
 *   ActionButton / menuAction   rule 11.4 — a control the caller cannot use is
 *                               DISABLED WITH THE REASON, never hidden and never
 *                               silently clickable-then-403. Disabled controls
 *                               use `isAriaDisabled` rather than `disabled`,
 *                               because a natively disabled button swallows the
 *                               hover and the reason never appears — which is
 *                               the same as not having written one.
 *   ResourceTabsPage            every tabbed listing renders through DataTable
 *                               with a PartialBanner above it and an honest
 *                               truncation footer below it.
 *   TruncationFooter            a listing cut short by `continue` says so. A
 *                               table that silently shows the first 500 of 4000
 *                               objects is a cluster that looks smaller than it
 *                               is, and the operator has no way to notice.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import yaml from 'js-yaml';
import {
  Button,
  Drawer,
  DrawerActions,
  DrawerCloseButton,
  DrawerContent,
  DrawerContentBody,
  DrawerHead,
  DrawerPanelBody,
  DrawerPanelContent,
  Label,
  LabelGroup,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Select,
  SelectList,
  SelectOption,
  MenuToggle,
  Tab,
  TabTitleText,
  Tabs,
  Title,
  Tooltip,
} from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  NullableCell,
  PageHeader,
  PartialBanner,
  SearchInput,
  Skeleton,
  Toolbar,
} from '../components/ui';
import LogViewer from '../components/LogViewer';
import MutationDialog from '../components/MutationDialog';
import PodTerminal from '../components/PodTerminal';
import YamlEditor from '../components/YamlEditor';
import { resources } from '../api/client';
import { useNamespace } from '../contexts/NamespaceContext';
import { useAsync, useResourceList, entriesOf } from './_data';

/* ── Cluster scope ──────────────────────────────────────────────────────── */

/**
 * What a page renders instead of a wall of `409 no_cluster_selected`.
 *
 * Every endpoint in this console is cluster-scoped (§1.1). With nothing
 * registered, firing the request anyway produces an error panel that describes
 * a failure when the real state is "you have not told us which cluster yet".
 */
export function NoClusterState({ what = 'this view' }) {
  return (
    <EmptyState
      title="No cluster is selected"
      description={`${what} reads from a registered Kubernetes cluster, and none is currently active.`}
      action={
        <Button variant="primary" component={ClusterSettingsLink}>
          Register or select a cluster
        </Button>
      }
    />
  );
}

// Module level: an inline arrow would be a new component type on every render,
// remounting the link mid-click. Same reasoning as Layout's BrandLink.
function ClusterSettingsLink(props) {
  return <Link to="/clusters" {...props} />;
}

/* ── Rule 11.4 controls ─────────────────────────────────────────────────── */

/**
 * A button that states why it cannot be pressed.
 *
 *   <ActionButton gate={gate('scale')} onClick={...}>Scale</ActionButton>
 *
 * `gate` is `{ allowed, reason }` from `useGates`. When it is not allowed the
 * button stays visible and focusable (`isAriaDisabled`) with the reason in a
 * tooltip, which is the whole of rule 11.4: an operator must be able to see that
 * the action exists and why it is unavailable.
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

/* ── Small cells ────────────────────────────────────────────────────────── */

/**
 * Secondary text. A PatternFly token with a literal fallback rather than a class
 * in index.css: this lane does not own that stylesheet, and a class that is not
 * defined there renders as unstyled body text — legible, but indistinguishable
 * from the primary value it is meant to sit behind.
 */
export function Muted({ children, title }) {
  return (
    <span title={title} style={{ color: 'var(--pf-t--global--text--color--subtle, #6a6e73)', fontSize: '0.875em' }}>
      {children}
    </span>
  );
}

/** Up to `max` chips, then "+n more" with the rest in a tooltip. */
export function ChipList({ values, max = 3, color = 'grey', emptyText = 'None' }) {
  const list = (values ?? []).filter((v) => v != null && v !== '');
  if (!list.length) return <Muted>{emptyText}</Muted>;
  const shown = list.slice(0, max);
  const hidden = list.slice(max);
  return (
    <LabelGroup numLabels={max + 1}>
      {shown.map((value, i) => (
        <Label key={`${value}-${i}`} isCompact color={color}>
          {String(value)}
        </Label>
      ))}
      {hidden.length > 0 && (
        <Tooltip content={hidden.join(', ')}>
          <Label isCompact color={color} tabIndex={0}>
            {`+${hidden.length} more`}
          </Label>
        </Tooltip>
      )}
    </LabelGroup>
  );
}

/**
 * `requested / allocatable` in one cell, with **both** halves routed through
 * `NullableCell`.
 *
 * The numerator is the one §5 nulls when the per-node pod listing failed, and it
 * is the one that matters: a node showing `0 / 16 cores` reads as an idle box,
 * and an idle box is the one an operator picks to drain. The denominator is
 * nullable for a different reason (a node that reports no `allocatable`), and
 * giving it the same treatment costs nothing and removes the temptation to
 * default it at the call site.
 */
export function UsageCell({ used, total, format, unit, reason }) {
  return (
    <span style={{ whiteSpace: 'nowrap' }}>
      <NullableCell value={used} format={format} reason={reason} />
      <Muted> / </Muted>
      <NullableCell value={total} format={format} unit={unit} />
    </span>
  );
}

/**
 * Container images, shortened to `repo:tag` with the full reference on hover.
 *
 * The registry host and org are the same for every row in almost every cluster,
 * so showing them in full pushes the tag — the only part that differs between a
 * working workload and the one that just broke — off the right edge of the
 * column. The full reference stays reachable, because "which registry is this
 * pulling from" is exactly the question an ImagePullBackOff raises.
 */
export function ImagesCell({ images, max = 2 }) {
  const list = (images ?? []).filter(Boolean);
  if (!list.length) return <Muted>none</Muted>;
  const shown = list.slice(0, max);
  return (
    <span style={{ display: 'inline-flex', gap: '0.35rem', flexWrap: 'wrap', alignItems: 'center' }}>
      {shown.map((image) => {
        const short = image.includes('/') ? image.slice(image.lastIndexOf('/') + 1) : image;
        return (
          <code key={image} title={image} style={{ fontSize: '0.85em' }}>
            {short}
          </code>
        );
      })}
      {list.length > max && (
        <Tooltip content={list.slice(max).join('\n')}>
          <span tabIndex={0}>
            <Muted>{`+${list.length - max}`}</Muted>
          </span>
        </Tooltip>
      )}
    </span>
  );
}

/** `metadata.labels` as chips. An object with no keys is "None", not blank. */
export function LabelsCell({ labels, max = 2 }) {
  const entries = entriesOf(labels);
  return <ChipList values={entries.map(([k, v]) => `${k}=${v}`)} max={max} emptyText="None" />;
}

/* ── Filters ────────────────────────────────────────────────────────────── */

/**
 * A labelled single-select. Thin wrapper so a page does not import four
 * PatternFly names to render one dropdown.
 *
 * `options` is `[{ value, label, description }]`; `value` of `null` selects
 * `placeholder`, which is how "All kinds" and "All namespaces" are spelled.
 */
export function PickList({ id, label, value, options, onChange, placeholder = 'All', width = 200 }) {
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === value);
  return (
    <div className="admin-filterbar__field">
      {label && (
        <label className="admin-filterbar__label" htmlFor={id}>
          {label}
        </label>
      )}
      <Select
        id={id}
        isOpen={open}
        selected={value}
        onOpenChange={setOpen}
        onSelect={(_event, next) => {
          onChange(next === '__all__' ? null : next);
          setOpen(false);
        }}
        toggle={(ref) => (
          <MenuToggle
            ref={ref}
            id={id}
            onClick={() => setOpen((v) => !v)}
            isExpanded={open}
            style={{ minWidth: width }}
            data-testid={`picklist-${id}`}
          >
            {selected ? selected.label : placeholder}
          </MenuToggle>
        )}
      >
        <SelectList>
          <SelectOption value="__all__" isSelected={value == null}>
            {placeholder}
          </SelectOption>
          {options.map((option) => (
            <SelectOption
              key={String(option.value)}
              value={option.value}
              description={option.description}
              isSelected={option.value === value}
            >
              {option.label}
            </SelectOption>
          ))}
        </SelectList>
      </Select>
    </div>
  );
}

/* ── Truncation ─────────────────────────────────────────────────────────── */

/**
 * "There is more than this" — never left implicit.
 *
 * `remaining` is the API server's own estimate of what is past the cursor; when
 * it is absent we say only that the listing continues, rather than inventing a
 * number.
 */
export function TruncationFooter({ listing, noun = 'objects' }) {
  if (!listing?.hasMore && !listing?.moreError) return null;
  return (
    <div className="admin-table-footer" style={{ padding: '0.75rem 0', display: 'flex', gap: '0.75rem', alignItems: 'center', flexWrap: 'wrap' }}>
      <span>
        {listing.remaining != null
          ? `This listing is truncated — about ${listing.remaining} more ${noun} were not fetched.`
          : `This listing is truncated — there are more ${noun} past this page.`}
      </span>
      <Button variant="secondary" size="sm" isLoading={listing.loadingMore} isDisabled={listing.loadingMore} onClick={listing.loadMore}>
        {listing.loadingMore ? 'Loading…' : 'Load more'}
      </Button>
      {listing.moreError && (
        <span className="admin-error-hint">
          The next page could not be fetched: {listing.moreError.message}
        </span>
      )}
    </div>
  );
}

/* ── YAML ───────────────────────────────────────────────────────────────── */

/**
 * `GET .../{name}/yaml` rendered read-only (§4).
 *
 * A failure here is shown as a failure, not as an empty editor: an operator who
 * is about to edit an object must never be handed a blank buffer that looks like
 * an empty manifest.
 */
export function YamlPanel({ group, version, plural, name, namespace, height = 520 }) {
  const key = [group, version, plural, namespace ?? '', name].join('|');
  const { data, loading, error, reload } = useAsync(
    () => resources.yaml(group, version, plural, name, namespace),
    { key, enabled: Boolean(name && plural) },
  );

  if (loading) return <Skeleton lines={8} height="0.8rem" />;
  if (error) return <ErrorState title="This object's YAML could not be read" error={error} onRetry={reload} />;
  return <CodeBlock code={data ?? ''} language="yaml" ariaLabel={`${name} YAML`} maxHeight={height} />;
}

/**
 * Edit an object's manifest: `YamlEditor` in the form phase of a
 * `MutationDialog`.
 *
 * `YamlEditor` is a controlled editor, not a dialog, and `MutationDialog` owns
 * the §11.3 handshake — so the edit flow is the composition of the two rather
 * than a third component that re-implements either. What that buys, concretely:
 * the operator types, presses Preview, and the diff they approve is the one the
 * API server projected from the text that was in the box at that moment. The
 * form is unmounted once the diff is up, so the text cannot drift underneath a
 * diff they have already read.
 *
 * `resourceVersion` is parsed out of the fetched manifest rather than fetched
 * separately. §0.4 makes it mandatory on `PUT`, and reading it from the same
 * bytes the operator is editing means the version we send is provably the
 * version they were looking at — two requests could straddle a change and send
 * a `resourceVersion` for a manifest nobody ever saw.
 */
export function EditYamlDialog({ isOpen, group, version, plural, name, namespace, kind, onClose, onApplied }) {
  const [text, setText] = useState('');
  const [validity, setValidity] = useState({ valid: false, message: 'Loading…' });
  const key = [group, version, plural, namespace ?? '', name].join('|');

  const loaded = useAsync(() => resources.yaml(group, version, plural, name, namespace), {
    key,
    enabled: Boolean(isOpen && name && plural),
  });

  // Seed the editor once the manifest arrives, and only then: assigning on
  // every render would throw away everything the operator typed.
  const seededRef = useRef(null);
  useEffect(() => {
    if (loaded.data != null && seededRef.current !== key) {
      seededRef.current = key;
      setText(loaded.data);
    }
    if (!isOpen) seededRef.current = null;
  }, [loaded.data, key, isOpen]);

  const resourceVersion = useMemo(() => {
    if (!loaded.data) return null;
    try {
      return yaml.load(loaded.data)?.metadata?.resourceVersion ?? null;
    } catch {
      // Unparseable YAML from our own backend is a defect, but it must not stop
      // the operator seeing it: PUT without a resourceVersion is refused by the
      // backend, which is the correct outcome and a clearer message than a
      // parse error thrown out of a dialog.
      return null;
    }
  }, [loaded.data]);

  if (!isOpen) return null;

  const previewBlocked = loaded.loading
    ? 'The manifest is still loading.'
    : loaded.error
      ? `The manifest could not be read: ${loaded.error.message}`
      : !validity.valid
        ? validity.message || 'The YAML in the editor is not valid.'
        : resourceVersion == null
          ? 'This manifest carries no metadata.resourceVersion, so the update cannot be made safe against a concurrent edit.'
          : null;

  return (
    <MutationDialog
      isOpen
      title={`Edit ${kind ?? plural} ${name}`}
      description={
        `The whole manifest is replaced with what is in the editor. Preview first — the diff below ` +
        `is the API server's own projection of the change.`
      }
      request={(dryRun, context) =>
        resources.update(group, version, plural, name, {
          yaml: text,
          namespace,
          resourceVersion: context?.resourceVersion ?? resourceVersion,
          dryRun,
        })
      }
      resourceVersion={resourceVersion}
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      confirmLabel="Apply manifest"
      onApplied={onApplied}
      onClose={onClose}
    >
      {loaded.loading ? (
        <Skeleton lines={10} height="0.8rem" />
      ) : loaded.error ? (
        <ErrorState title="This manifest could not be read" error={loaded.error} onRetry={loaded.reload} />
      ) : (
        <YamlEditor
          value={text}
          onChange={setText}
          onValidityChange={setValidity}
          originalValue={loaded.data ?? ''}
          label={`${kind ?? plural}/${name}`}
          ariaLabel={`YAML for ${name}`}
        />
      )}
    </MutationDialog>
  );
}

/**
 * Logs and exec for one pod, in one modal.
 *
 * `LogViewer` and `PodTerminal` are inline panels rather than dialogs — they are
 * embeddable anywhere, and neither takes `isOpen` or `onClose` — so the modal,
 * the tab strip and the close button belong to whoever opens them. Every pod
 * table in this lane opens the same one, so the two live side by side: the
 * overwhelmingly common sequence is to read the logs, fail to find the answer,
 * and go in with a shell.
 *
 * The exec tab is *rendered* even when the caller may not use it, with the
 * reason in place of the terminal (rule 11.4). Hiding the tab would leave an
 * operator wondering whether this console can exec at all.
 */
export function PodConsoleModal({ pod, initialTab = 'logs', execGate, onClose }) {
  const [tab, setTab] = useState(initialTab);
  if (!pod) return null;
  const execAllowed = !execGate || execGate.allowed;
  return (
    <Modal isOpen variant="large" onClose={onClose} aria-label={`Pod ${pod.name}`} data-testid="pod-console">
      <ModalHeader title={`${pod.namespace}/${pod.name}`} />
      <ModalBody>
        <Tabs activeKey={tab} onSelect={(_event, key) => setTab(key)} aria-label="Pod console">
          <Tab eventKey="logs" title={<TabTitleText>Logs</TabTitleText>} aria-label="Logs" />
          <Tab eventKey="exec" title={<TabTitleText>Terminal</TabTitleText>} aria-label="Terminal" />
        </Tabs>
        {tab === 'logs' ? (
          <LogViewer namespace={pod.namespace} name={pod.name} containers={pod.containers} />
        ) : execAllowed ? (
          <PodTerminal namespace={pod.namespace} name={pod.name} containers={pod.containers} />
        ) : (
          <EmptyState title="A terminal cannot be opened for this pod" description={execGate.reason} />
        )}
      </ModalBody>
      <ModalFooter>
        <Button variant="link" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}

/* ── Tabbed resource pages (§8) ─────────────────────────────────────────── */

/**
 * The Network / Config / Storage / Access shape: several §4 listings behind
 * tabs, each with its own columns and its own detail panel.
 *
 * Only the active tab fetches. Rendering all of them and hiding the inactive
 * ones issued five listings on mount — five chances to be denied, five banners,
 * and four of them describing a table nobody is looking at.
 */
export function ResourceTabsPage({ title, subtitle, actions, tabs, initialTab }) {
  const [activeKey, setActiveKey] = useState(initialTab ?? tabs[0]?.key);
  const active = tabs.find((tab) => tab.key === activeKey) ?? tabs[0];

  return (
    <>
      <PageHeader title={title} subtitle={subtitle} actions={actions} />
      <Tabs
        activeKey={active?.key}
        onSelect={(_event, key) => setActiveKey(key)}
        aria-label={`${title} sections`}
        role="region"
      >
        {tabs.map((tab) => (
          <Tab key={tab.key} eventKey={tab.key} title={<TabTitleText>{tab.title}</TabTitleText>} aria-label={tab.title} />
        ))}
      </Tabs>
      {/* Keyed on the tab so switching tabs resets the search box, the drawer
          and the accumulated `continue` pages together. Carrying a Secrets
          filter over into ConfigMaps looked like an empty namespace. */}
      {active && <ResourceTabBody key={active.key} tab={active} />}
    </>
  );
}

function ResourceTabBody({ tab }) {
  const { selected } = useNamespace();
  const namespace = tab.namespaced ? selected : null;
  const [search, setSearch] = useState('');
  const [detailRow, setDetailRow] = useState(null);

  const listing = useResourceList(tab.group, tab.version, tab.plural, {
    namespace,
    shape: tab.shape,
    limit: tab.limit,
  });

  const close = useCallback(() => setDetailRow(null), []);

  // A Namespace column repeating the scope the masthead already shows is a
  // column of one value; it is dropped while a namespace is selected and comes
  // back for "All namespaces", where it is the column that disambiguates rows.
  const columns = useMemo(
    () => (tab.namespaced && namespace ? tab.columns.filter((column) => column.key !== 'namespace') : tab.columns),
    [tab.columns, tab.namespaced, namespace],
  );

  const onRowClick = tab.detail ? (row) => setDetailRow(row) : tab.onRowClick;

  const table = (
    <>
      <PartialBanner unavailable={listing.unavailable} />
      <Toolbar ariaLabel={`${tab.title} controls`}>
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder={`Filter ${tab.title.toLowerCase()}…`} />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>
            {tab.namespaced ? (namespace ? `Namespace: ${namespace}` : 'All namespaces') : 'Cluster-scoped'}
          </Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button variant="plain" aria-label={`Refresh ${tab.title}`} icon={<SyncAltIcon />} onClick={listing.reload} />
        </Toolbar.Item>
      </Toolbar>
      <DataTable
        ariaLabel={tab.title}
        // Column widths are filed under the API resource rather than the tab's
        // title: the title is display text that a rename would silently orphan.
        tableId={`resources:${tab.group}/${tab.version}/${tab.plural}`}
        columns={columns}
        rows={listing.items}
        rowKey={tab.rowKey}
        loading={listing.loading}
        error={listing.error}
        onRetry={listing.reload}
        filterText={search}
        onRowClick={onRowClick}
        actions={tab.actions}
        emptyTitle={`No ${tab.title.toLowerCase()} here`}
        emptyDescription={
          listing.partial
            ? 'Some sources could not be read — see the banner above. This table is not a complete answer.'
            : tab.emptyDescription
        }
        footer={<TruncationFooter listing={listing} noun={tab.title.toLowerCase()} />}
      />
    </>
  );

  if (!tab.detail) return table;

  const panel = (
    <DrawerPanelContent widths={{ default: 'width_50' }} isResizable>
      <DrawerHead>
        <Title headingLevel="h2" size="lg">
          {tab.detailTitle ? tab.detailTitle(detailRow) : detailRow?.name}
        </Title>
        <DrawerActions>
          <DrawerCloseButton onClick={close} />
        </DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>{detailRow && tab.detail(detailRow, { close, namespace, reload: listing.reload })}</DrawerPanelBody>
    </DrawerPanelContent>
  );

  return (
    <Drawer isExpanded={Boolean(detailRow)} onExpand={() => {}} isInline>
      <DrawerContent panelContent={panel}>
        <DrawerContentBody>{table}</DrawerContentBody>
      </DrawerContent>
    </Drawer>
  );
}
