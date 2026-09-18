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
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
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
  ActionButton,
  AgeCell,
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
import DebugPanel from '../components/DebugPanel';
import { load } from '../components/clusterYaml';
import LogViewer from '../components/LogViewer';
import MutationDialog from '../components/MutationDialog';
import PodTerminal from '../components/PodTerminal';
import YamlEditor from '../components/YamlEditor';
import { templatesFor } from '../components/templates';
import { realGroup, resources } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import {
  useAsync,
  useGates,
  useLiveYaml,
  useResourceList,
  entriesOf,
  objectAgeSeconds,
  objectName,
  objectNamespace,
} from './_data';

// Lazy, like the copy in `Layout.jsx`, and for the same reason: this dialog
// carries `objectFormModel.js` and `templates.js`, and a single static import
// of it anywhere pulls both back into the chunk that does the importing. The
// saving only exists if every call site is `lazy`, so this one is too.
const ImportYamlDialog = lazy(() => import('../components/ImportYamlDialog'));

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

// `ActionButton` and `menuAction` moved to `components/ui/gated.jsx` — they are
// rule-11.4 enforcement in the same sense `PartialBanner` and `NullableCell` are
// rule-11.1 and 11.2 enforcement, and that is where the design system lives.
// Re-exported here because every page in this lane imports them from `./_parts`,
// and because `components/DebugPanel` needs them too: importing them from here
// would make this module and that one import each other, and a cycle across a
// lazy chunk boundary is the class of mistake this repo builds for production
// before it tests.
// `ActionButton` is also imported above, because `ResourceTabBody` renders one
// itself: the create button every listing tab carries.
export { menuAction } from '../components/ui';
export { ActionButton };

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

/**
 * Up to `max` chips, then "+n more" with the rest in a tooltip.
 *
 * `hrefFor` turns a chip into a link. It goes through PatternFly's `render`
 * prop rather than `Label href`, because `href` builds the anchor itself and
 * spreads extra props onto the *outer* span — so `target` and `rel` never reach
 * the `<a>`. Both are load-bearing here: an exposure opens somewhere that is not
 * this console, so it belongs in its own tab, and `rel="noopener noreferrer"`
 * is not optional on a link whose target is a cluster workload nobody has
 * vetted.
 *
 * The overflow chip stays plain text on purpose — it is a tooltip trigger, and
 * a link that swallows its own click is worse than one that is not offered.
 */
export function ChipList({ values, max = 3, color = 'grey', emptyText = 'None', hrefFor }) {
  const list = (values ?? []).filter((v) => v != null && v !== '');
  if (!list.length) return <Muted>{emptyText}</Muted>;
  const shown = list.slice(0, max);
  const hidden = list.slice(max);
  return (
    <LabelGroup numLabels={max + 1}>
      {shown.map((value, i) => {
        const href = hrefFor ? hrefFor(value) : null;
        return href ? (
          <Label
            key={`${value}-${i}`}
            isCompact
            color={color}
            isClickable
            render={({ className, content, componentRef }) => (
              <a
                className={className}
                ref={componentRef}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
              >
                {content}
              </a>
            )}
          >
            {String(value)}
          </Label>
        ) : (
          <Label key={`${value}-${i}`} isCompact color={color}>
            {String(value)}
          </Label>
        );
      })}
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
    // `admin-cell-inline` rather than an inline style: a compact table has to be
    // able to stop this wrapping, and an inline style can only be overridden
    // with `!important`.
    <span className="admin-cell-inline">
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

/** Wall-clock time of an epoch millisecond value, for "read at 16:20:31". */
function clock(ms) {
  return ms == null ? null : new Date(ms).toLocaleTimeString();
}

/**
 * `GET .../{name}/yaml` rendered read-only (§4), numbered, coloured and kept
 * current.
 *
 * Three things this panel refuses to do, each of which is the same mistake in a
 * different costume — presenting something as more current than it is:
 *
 * **It does not show a manifest without saying when it was read.** A YAML view
 * with no timestamp is indistinguishable from a live one, and the object on the
 * screen may have been replaced by a controller ten minutes ago.
 *
 * **It does not blank on a failed refresh.** The text stays and the header says
 * the refresh failed. Throwing away the copy the operator was reading because
 * one poll timed out is a worse answer than an old one, clearly labelled.
 *
 * **It does not present a stale copy as fresh.** `readAt` is the time the bytes
 * on screen were fetched, never the time of the last attempt.
 *
 * Reload is here because a poll is on somebody else's clock. An operator who
 * has just changed something wants to see it *now*, and "wait up to ten
 * seconds, then decide whether it worked" is not a thing to ask of somebody
 * mid-incident.
 */
export function YamlPanel({ group, version, plural, name, namespace, height = 520, watch = true }) {
  const live = useLiveYaml({ group, version, plural, name, namespace, watch });

  // Only while there is nothing to show. A skeleton on every refresh would take
  // the manifest away from the operator six times a minute.
  if (live.loading && live.text == null) return <Skeleton lines={8} height="0.8rem" />;
  if (live.error) {
    return <ErrorState title="This object's YAML could not be read" error={live.error} onRetry={live.reload} />;
  }

  return (
    <div className="admin-yaml-panel" data-testid="yaml-panel">
      <div className="admin-yaml-panel__bar">
        <Muted>
          <span data-testid="yaml-panel-read-at">Read at {clock(live.readAt) ?? '—'}</span>
          {live.changedAt != null && (
            <span data-testid="yaml-panel-changed"> · changed at {clock(live.changedAt)}</span>
          )}
          {live.watching && !live.refreshError && <span> · watching for changes</span>}
        </Muted>
        <Button
          variant="link"
          isInline
          icon={<SyncAltIcon />}
          onClick={live.reload}
          data-testid="yaml-panel-reload"
        >
          Reload
        </Button>
      </div>

      {live.refreshError && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="yaml-panel-stale"
          title={`This is the copy read at ${clock(live.readAt) ?? 'an earlier point'} — the last refresh failed: ${live.refreshError.message}`}
        />
      )}

      <CodeBlock code={live.text ?? ''} language="yaml" ariaLabel={`${name} YAML`} maxHeight={height} />
    </div>
  );
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
 *
 * **The object is re-read while the dialog is open, and the editor is never
 * touched by it.** Two operators on the same Deployment during an incident is
 * the §0.4 scenario, and the API server already refuses the second save — but a
 * `409` arrives *after* the second operator has finished typing, which is the
 * most expensive moment to learn it. The poll moves that discovery to the front:
 * a banner appears the moment the object changes underneath, with the choice of
 * what to do about it left to the operator.
 *
 * What the poll must never do is quietly re-base the edit. Adopting the newer
 * text would discard what they typed; adopting the newer `resourceVersion`
 * would be worse — it would make the `PUT` succeed against a version they never
 * saw, which is precisely the blind overwrite optimistic concurrency exists to
 * prevent, arranged by the console itself. So the version sent on `PUT` stays
 * the one the editor was seeded from, and taking the new manifest is a button
 * the operator presses, labelled with what it costs.
 */
export function EditYamlDialog({ isOpen, group, version, plural, name, namespace, kind, onClose, onApplied }) {
  const [text, setText] = useState('');
  const [validity, setValidity] = useState({ valid: false, message: 'Loading…' });
  const key = [group, version, plural, namespace ?? '', name].join('|');

  const live = useLiveYaml({ group, version, plural, name, namespace, enabled: isOpen });

  // The manifest the editor was seeded from — the base of the edit, and the
  // only thing `resourceVersion` may be read out of. `live.text` moves with the
  // cluster; this does not move until the operator says so.
  const [base, setBase] = useState(null);
  const seededRef = useRef(null);

  const adopt = useCallback((next) => {
    setBase(next);
    setText(next);
  }, []);

  // Seed once, when the manifest arrives, and only then: assigning on every
  // render would throw away everything the operator typed.
  useEffect(() => {
    if (!isOpen) {
      seededRef.current = null;
      setBase(null);
      return;
    }
    if (live.text != null && seededRef.current !== key) {
      seededRef.current = key;
      adopt(live.text);
    }
  }, [live.text, key, isOpen, adopt]);

  const resourceVersion = useMemo(() => {
    if (!base) return null;
    try {
      return load(base)?.metadata?.resourceVersion ?? null;
    } catch {
      // Unparseable YAML from our own backend is a defect, but it must not stop
      // the operator seeing it: PUT without a resourceVersion is refused by the
      // backend, which is the correct outcome and a clearer message than a
      // parse error thrown out of a dialog.
      return null;
    }
  }, [base]);

  // Not "the text differs from what is in the box" — that is just editing. This
  // is the cluster's copy differing from the one this edit was based on.
  const changedOnCluster = base != null && live.text != null && live.text !== base;
  const edited = base != null && text !== base;

  if (!isOpen) return null;

  const previewBlocked = live.loading && live.text == null
    ? 'The manifest is still loading.'
    : live.error
      ? `The manifest could not be read: ${live.error.message}`
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
      {changedOnCluster && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="edit-yaml-changed"
          title="This object has changed on the cluster since the editor was opened"
          actionLinks={
            <Button variant="link" isInline onClick={() => adopt(live.text)} data-testid="edit-yaml-adopt">
              {edited ? 'Discard my changes and load the new version' : 'Load the new version'}
            </Button>
          }
        >
          {/* Said plainly, because the alternative is learning it from a 409
              after finishing the edit. The version that will be sent is still
              the one this edit started from, which is what makes the API
              server's refusal the safe outcome rather than a lost change. */}
          Applying what is in the editor will be refused as a conflict, because it is written against
          {resourceVersion ? ` resourceVersion ${resourceVersion}` : ' an older version'} and the cluster has moved on.
        </Alert>
      )}

      {live.loading && live.text == null ? (
        <Skeleton lines={10} height="0.8rem" />
      ) : live.error ? (
        <ErrorState title="This manifest could not be read" error={live.error} onRetry={live.reload} />
      ) : (
        <YamlEditor
          value={text}
          onChange={setText}
          onValidityChange={setValidity}
          originalValue={base ?? ''}
          label={`${kind ?? plural}/${name}`}
          ariaLabel={`YAML for ${name}`}
        />
      )}
    </MutationDialog>
  );
}

/**
 * Logs, YAML and exec for one pod, in one modal.
 *
 * **This is the in-context peek, not the pod's page.** `/pods/{ns}/{name}`
 * (§7.5) is where a pod's whole story lives — details, metrics, environment,
 * events, and these same panels — and the pod table links there. This modal
 * survives because the node detail and workload detail pages are places an
 * operator is *deciding something else*: reading one pod's logs mid-drain
 * should not cost them the plan they were looking at. Where the two overlap
 * they render the same components, so there is one Logs viewer and one
 * terminal, in two frames.
 *
 * `LogViewer` and `PodTerminal` are inline panels rather than dialogs — they are
 * embeddable anywhere, and neither takes `isOpen` or `onClose` — so the modal,
 * the tab strip and the close button belong to whoever opens them. The pod
 * tables that open this one put them side by side: the overwhelmingly common
 * sequence is to read the logs, fail to find the answer, and go in with a shell.
 *
 * **YAML is a tab here rather than a page of its own.** A pod is the one kind
 * an operator opens by clicking a row rather than by browsing to it, and until
 * this tab existed it was also the one kind whose manifest this console would
 * not show them — the whole object was reachable from the API explorer and
 * nowhere along the path they were actually walking. The three questions asked
 * of a misbehaving pod are what it logged, what it is, and what it looks like
 * from inside; they belong behind one set of tabs.
 *
 * **Debug is the fourth tab, and it is the one for a pod with no shell.** §7.4:
 * an image built from `scratch` or `distroless` is the one an operator most
 * needs to get inside and the one the Terminal tab is useless against, because
 * there is nothing to exec. That tab attaches an ephemeral container carrying
 * the tools and then opens a shell in *it*.
 *
 * The exec and debug tabs are *rendered* even when the caller may not use them,
 * with the reason in place of the content (rule 11.4). Hiding a tab would leave
 * an operator wondering whether this console can exec or debug at all.
 */
export function PodConsoleModal({ pod, initialTab = 'logs', execGate, debugGate, onClose }) {
  const [tab, setTab] = useState(initialTab);
  if (!pod) return null;
  const execAllowed = !execGate || execGate.allowed;
  // Containers by kind. The Debug tab needs the pod's *own* containers for the
  // process-namespace target picker and for the name-collision check, and an
  // ephemeral container is neither a valid target nor a name it should suggest
  // — §6's `kind` is what tells them apart.
  const entries = Array.isArray(pod.containers) ? pod.containers : [];
  const ownContainers = entries
    .filter((entry) => (entry?.kind ?? 'container') === 'container')
    .map((entry) => entry?.name)
    .filter(Boolean);
  return (
    <Modal isOpen variant="large" onClose={onClose} aria-label={`Pod ${pod.name}`} data-testid="pod-console">
      <ModalHeader title={`${pod.namespace}/${pod.name}`} />
      <ModalBody>
        <Tabs activeKey={tab} onSelect={(_event, key) => setTab(key)} aria-label="Pod console">
          <Tab eventKey="logs" title={<TabTitleText>Logs</TabTitleText>} aria-label="Logs" />
          <Tab eventKey="yaml" title={<TabTitleText>YAML</TabTitleText>} aria-label="YAML" />
          <Tab eventKey="exec" title={<TabTitleText>Terminal</TabTitleText>} aria-label="Terminal" />
          <Tab eventKey="debug" title={<TabTitleText>Debug</TabTitleText>} aria-label="Debug" />
        </Tabs>
        {tab === 'logs' ? (
          <LogViewer namespace={pod.namespace} name={pod.name} containers={pod.containers} />
        ) : tab === 'yaml' ? (
          <YamlPanel group="core" version="v1" plural="pods" name={pod.name} namespace={pod.namespace} height={520} />
        ) : tab === 'debug' ? (
          <DebugPanel
            namespace={pod.namespace}
            name={pod.name}
            gate={debugGate}
            execGate={execGate}
            containers={ownContainers}
          />
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
 *
 * Two reads are the page's rather than the tab's, and both are here because
 * this is the only component that knows all the tabs. The §4 catalog answers
 * the same thing for every one of them, so fetching it in the body would be a
 * round trip per tab switch for an answer that does not change. The §9 create
 * preflight is one batched call for every listing on the page rather than one
 * per tab — which is what §9's batch endpoint is for, and what keeps switching
 * tabs from being a permission check each time.
 */
export function ResourceTabsPage({ title, subtitle, tabs, initialTab }) {
  const [activeKey, setActiveKey] = useState(initialTab ?? tabs[0]?.key);
  const active = tabs.find((tab) => tab.key === activeKey) ?? tabs[0];
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();

  // Read once for the page rather than once per tab body. `useAsync` holds no
  // cross-component cache — its `key` decides when to blank the previous answer,
  // nothing more — so a catalog fetched inside `ResourceTabBody` would be a
  // fresh round trip on every tab switch and every `refreshToken` bump, for an
  // answer that is the same each time. Keyed by cluster because the answer is
  // about that cluster: `Layout` already remounts this subtree when it changes,
  // and depending on that rather than saying so is how the dependency gets lost
  // in the next refactor.
  const catalog = useAsync(() => resources.catalog(), {
    key: `catalog:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  // One batched §9 preflight for every listing on the page, asked here because
  // only this component knows all the tabs — the body knows one. The namespace
  // is the masthead's, not absent: a check with no namespace asks whether the
  // caller may create the resource *anywhere*, and an operator holding a grant
  // in one namespace would be told they cannot do something they can.
  const createChecks = useMemo(
    () =>
      tabs
        .filter((tab) => !tab.render && tab.plural)
        .map((tab) => ({
          id: `create:${tab.key}`,
          verb: 'create',
          group: tab.group,
          resource: tab.plural,
          namespace: tab.namespaced ? namespace : null,
        })),
    [tabs, namespace],
  );
  const { gate: createGate } = useGates(createChecks, { enabled: activeClusterId != null });

  return (
    <>
      <PageHeader title={title} subtitle={subtitle} />
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
      {active &&
        (active.render ? (
          // A tab whose rows are not a §4 listing renders itself. The escape
          // hatch is narrow on purpose: everything that *is* a listing goes
          // through `ResourceTabBody`, which is where the partial banner, the
          // truncation footer, the namespace-column rule and the create button
          // live — four things a hand-rolled tab would have to remember and
          // would eventually not. It is also why the two tabs that are not
          // listings — Pod Isolation, Access review — get no create button
          // without anybody having to exclude them: they have no resource to
          // create one of.
          <CustomTabBody key={active.key} render={active.render} />
        ) : (
          // `refreshToken` is part of the key so a page that has just written to
          // the cluster can force this listing to be read again — by changing a
          // number, not by reaching into a hook it does not own. A write from a
          // page-level button has no row and therefore no listing to call
          // `reload` on, and a table still showing the object somebody just
          // deleted is the moment a console stops being believed.
          <ResourceTabBody
            key={`${active.key}:${active.refreshToken ?? ''}`}
            tab={active}
            catalog={catalog}
            createGate={createGate}
          />
        ))}
    </>
  );
}

/** Renders a tab's own body, keyed like the listing ones so it remounts too. */
function CustomTabBody({ render }) {
  return render();
}

/**
 * The catalog entry behind one tab: its `kind`, its `apiVersion`, the verbs the
 * API advertises for it, and — for the tabs that ask — the version it is
 * actually served at.
 *
 * The version half is why this started existing. Gateway API's kinds still move
 * between release channels on a live cluster (`ReferenceGrant` at `v1beta1`,
 * `BackendTLSPolicy` at `v1alpha2`/`v1alpha3` depending on which CRD bundle is
 * installed) — `resolve()` on the backend matches a version exactly, so a tab
 * that pinned one the way every other tab in this console does would 501 on any
 * cluster running a different channel than the one this code was written
 * against. A `resolveVersion` tab therefore takes the version the cluster marks
 * `preferred`; every other tab keeps the one it pinned, and its entry is looked
 * up at that exact version, so a tab pinned to an API this cluster does not
 * serve finds nothing rather than quietly binding to a different version of it.
 *
 * The rest is what a create button needs. **`kind` is read from discovery and
 * never derived from the tab's title.** `genericTab` already does
 * `title.replace(/s$/, '')` for its drawer heading, and on the tabs that matter
 * it produces "Endpoint Slice", "Network Policie" and "HPA" — none of which is
 * a kind. A button offering to create a kind that does not exist is the defect
 * standard with a click target on it; discovery is the only thing here that
 * knows the answer.
 */
function useCatalogEntry(tab, catalog) {
  return useMemo(() => {
    const items = catalog?.data?.items;
    if (!items) return null;
    const real = realGroup(tab.group);
    const matches = items.filter((item) => realGroup(item.group) === real && item.resource === tab.plural);
    if (tab.resolveVersion) return matches.find((item) => item.preferred) ?? matches[0] ?? null;
    return matches.find((item) => item.version === tab.version) ?? null;
  }, [catalog?.data, tab.group, tab.plural, tab.version, tab.resolveVersion]);
}

/**
 * Why this listing cannot be created into, as a property of the **API** rather
 * than of the caller — or `null` when it can.
 *
 * Asked before the permission gate and never merged into it. "This cluster does
 * not serve a create for this resource" and "you may not create one" send an
 * operator to two different places, and answering the first with the second
 * sends them to widen a ClusterRole that was already correct. The same order
 * `capabilityGate` uses for workload actions, for the same reason.
 *
 * "We have not looked yet" is a third answer and stays one. A catalog still in
 * flight is not a cluster without the resource.
 */
function createCapability(tab, catalog, entry, version) {
  if (catalog?.error) {
    return (
      `The API catalog could not be read (${catalog.error.message}), so whether this cluster serves a ` +
      'create for this resource is not something this console can say.'
    );
  }
  if (!catalog?.data) {
    return 'Discovery has not answered yet, so whether one of these can be created is still unknown.';
  }
  if (!entry) {
    return `This cluster does not serve ${tab.plural} in ${realGroup(tab.group) || 'core'}/${version}.`;
  }
  if (!(entry.verbs ?? []).includes('create')) {
    return (
      `Discovery reports the verbs ${(entry.verbs ?? []).join(', ') || '(none)'} for this resource, and ` +
      '"create" is not among them. This is a property of the API, not a permission problem.'
    );
  }
  return null;
}

function ResourceTabBody({ tab, catalog, createGate }) {
  const { selected } = useNamespace();
  const namespace = tab.namespaced ? selected : null;
  const [search, setSearch] = useState('');
  const [detailRow, setDetailRow] = useState(null);
  const [creating, setCreating] = useState(false);

  const entry = useCatalogEntry(tab, catalog);
  const version = (tab.resolveVersion && entry?.version) || tab.version;
  const listing = useResourceList(tab.group, version, tab.plural, {
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

  // The kind the button offers to create. Never guessed from the title: until
  // discovery answers, the button says "Create…" and the gate says why, which
  // is the honest version of not knowing yet.
  const kind = entry?.kind ?? null;
  const capability = createCapability(tab, catalog, entry, version);
  const createAllowed = capability
    ? { allowed: false, reason: capability }
    : createGate(`create:${tab.key}`);

  const table = (
    <>
      <PartialBanner unavailable={listing.unavailable} />
      {/* A standing caveat about what this table does NOT prove, above the rows
          rather than in a drawer nobody has opened yet. `PartialBanner` reports
          what could not be read; this reports what was read and still cannot be
          concluded from — see the NetworkPolicy tabs, where every value in the
          table is a declaration the cluster may or may not enforce. */}
      {tab.notice}
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
          <ActionButton
            variant="primary"
            gate={createAllowed}
            onClick={() => setCreating(true)}
            // Labelled only when the visible text does not name what it makes.
            // An `aria-label` replaces the text for anything reading the
            // accessible name, so one repeating it would just be a second copy
            // to keep in step — and one differing from it is the "label in
            // name" failure, where the words on screen are not the words a
            // voice control can say.
            ariaLabel={kind ? undefined : `Create a ${tab.title.toLowerCase()} object`}
          >
            {kind ? `Create ${kind}…` : 'Create…'}
          </ActionButton>
        </Toolbar.Item>
        <Toolbar.Item>
          <Button variant="plain" aria-label={`Refresh ${tab.title}`} icon={<SyncAltIcon />} onClick={listing.reload} />
        </Toolbar.Item>
      </Toolbar>
      <DataTable
        ariaLabel={tab.title}
        // Column widths are filed under the API resource rather than the tab's
        // title: the title is display text that a rename would silently orphan.
        tableId={`resources:${tab.group}/${tab.version}/${tab.plural}`}
        manageableColumns
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

  const panel = tab.detail ? (
    <DrawerPanelContent widths={{ default: 'width_50' }} isResizable>
      <DrawerHead>
        <Title headingLevel="h2" size="lg">
          {tab.detailTitle ? tab.detailTitle(detailRow) : detailRow?.name}
        </Title>
        <DrawerActions>
          <DrawerCloseButton onClick={close} />
        </DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>
        {detailRow && tab.detail(detailRow, { close, namespace, reload: listing.reload, version })}
      </DrawerPanelBody>
    </DrawerPanelContent>
  ) : null;

  return (
    <>
      {tab.detail ? (
        <Drawer isExpanded={Boolean(detailRow)} onExpand={() => {}} isInline>
          <DrawerContent panelContent={panel}>
            <DrawerContentBody>{table}</DrawerContentBody>
          </DrawerContent>
        </Drawer>
      ) : (
        table
      )}

      {/* `listing.reload` rather than the page's `refreshToken`: the button is
          inside the component that owns the listing, so the remount that token
          exists to force is not needed — and a remount would throw away the
          search text, the open drawer and every `continue` page the operator
          loaded, to show one new row that is on the first page anyway. */}
      {creating && (
        <Suspense fallback={null}>
          <ImportYamlDialog
            isOpen
            title={`Create ${kind}`}
            templates={templatesFor(entry)}
            onClose={() => setCreating(false)}
            onApplied={() => {
              setCreating(false);
              listing.reload();
            }}
          />
        </Suspense>
      )}
    </>
  );
}

/* ── Generic tabs ───────────────────────────────────────────────────────── */

/**
 * A tab spec for a resource with no typed row (§8 defines none) and nothing
 * page-specific to say about it: Name (+ Namespace, if namespaced) + Age, and
 * a YAML detail panel. This is most of what Configuration's and Gateway's new
 * tabs are — the cluster serves the object, nobody has written it a shaper,
 * and there is nothing beyond "here it is, here is its YAML". Written once so
 * a dozen near-identical column arrays don't drift from each other one typo at
 * a time; a tab with a real typed row or resource-specific columns (Services,
 * Ingresses, StorageClasses, …) is still hand-written, as those already are.
 *
 * `shape: 'raw'` is explicit rather than left to `shape=auto`'s fallback,
 * matching `Network.jsx`'s Endpoints tab: saying "no shaper" out loud here
 * costs nothing and reads better next to a tab that has one.
 *
 * `resolveVersion: true` is for a group whose served version is not stable
 * across clusters (Gateway API's release channels) — see `useResolvedVersion`
 * above. Every other caller pins a version, same as every hand-written tab in
 * this console.
 */
export function genericTab({
  key,
  title,
  group,
  version,
  plural,
  namespaced,
  resolveVersion = false,
  emptyDescription = `The listing succeeded and returned no ${title} in this scope.`,
}) {
  return {
    key,
    title,
    group,
    version,
    plural,
    namespaced,
    resolveVersion,
    shape: 'raw',
    rowKey: namespaced
      ? (row) => `${objectNamespace(row)}/${objectName(row)}`
      : (row) => objectName(row),
    emptyDescription,
    detailTitle: (row) => `${title.replace(/s$/, '')} ${namespaced ? `${objectNamespace(row)}/` : ''}${objectName(row)}`,
    columns: [
      { key: 'name', title: 'Name', sortable: true, value: (row) => objectName(row) },
      ...(namespaced
        ? [{ key: 'namespace', title: 'Namespace', sortable: true, value: (row) => objectNamespace(row) }]
        : []),
      {
        key: 'age_seconds',
        title: 'Age',
        sortable: true,
        value: (row) => objectAgeSeconds(row),
        cell: (row) => <AgeCell seconds={objectAgeSeconds(row)} timestamp={row?.metadata?.creationTimestamp} />,
      },
    ],
    detail: (row, ctx) => (
      <YamlPanel
        group={group}
        version={ctx?.version ?? version}
        plural={plural}
        name={objectName(row)}
        namespace={namespaced ? objectNamespace(row) : undefined}
        height={420}
      />
    ),
  };
}
