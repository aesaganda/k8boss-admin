/**
 * Topology — §6's workloads as a picture, with §8's Services and §13's
 * exposures marked on them (contract rule 11.13).
 *
 * It reads three listings the console already serves and joins them in the
 * browser (`topologyGraph.js`, which is pure and holds the join rules). There
 * is no topology endpoint and no new read model: a second server-side answer to
 * "which Service picks these pods" would be free to disagree with the one
 * §6's detail already gives, and the screen that disagreed would be this one.
 *
 * What the picture is allowed to say is the whole design:
 *
 *   - A node marked with the outward arrow **has** an exposure that names a
 *     Service whose selector this workload's row satisfies. That is proved, not
 *     inferred — see `servicesFor`.
 *   - A node marked `?` is one this listing cannot attribute (an expression-only
 *     selector, a CronJob), or one drawn while the Services or Routes listing
 *     was unreadable, unanswered or cut off at its limit. It is never drawn
 *     bare, because a bare node reads as "nothing reaches this" and that is a
 *     sentence operators act on.
 *   - An unmarked node is the one positive claim the canvas makes about
 *     absence, so the join is three-valued: a Service whose selector disagrees
 *     with a label the row carries is ruled out, one that agrees is attached,
 *     and one selecting on a key the row does not carry makes the node `?`.
 *     Nothing is drawn bare on a maybe.
 *   - The drawer asks §6's detail for the selected workload, which reads the
 *     pod template and is the authority on its Services. The canvas is a
 *     summary; the panel is the answer.
 *
 * The write surface is the one every other workload screen uses — the same five
 * dialogs, gated by the same §9 batch through rule 11.4's `ActionButton` and
 * `menuAction`. Nothing here posts to a cluster: a new place to click is not a
 * new write.
 */
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
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
  Dropdown,
  DropdownItem,
  DropdownList,
  MenuToggle,
  Title,
} from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DescriptionList,
  EmptyState,
  ErrorState,
  PageHeader,
  PartialBanner,
  SearchInput,
  SectionHeader,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import ScaleDialog from '../components/ScaleDialog';
import ScaleStepper from '../components/ScaleStepper';
import RestartDialog from '../components/RestartDialog';
import SuspendDialog from '../components/SuspendDialog';
import RollbackDialog from '../components/RollbackDialog';
import DeleteDialog from '../components/DeleteDialog';
import { routes as routesApi, workloads as workloadsApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import {
  KIND_TO_PLURAL,
  WORKLOAD_KINDS,
  capabilityGate,
  useAsync,
  useGates,
  useResourceList,
  withScopeNote,
  workloadChecks,
} from './_data';
import {
  ChipList,
  EditYamlDialog,
  ImagesCell,
  Muted,
  NoClusterState,
  UsageCell,
} from './_parts';
import {
  NODE_H,
  NODE_W,
  abbreviate,
  buildTopology,
  layoutTopology,
  routeUrl,
  routesFor,
} from './topologyGraph';

// A shared empty array, like `_data.js` keeps: `?? []` mints a new one on every
// render, and three memos downstream would recompute the whole layout while the
// first listing is still in flight.
const EMPTY = [];

/**
 * Is an `unavailable[]` entry a hole in what we know, or an ordinary absence?
 *
 * `unsupported` is the second one (§1.2, rule 11.7): a cluster that does not
 * serve `route.openshift.io` has no Routes, and treating that as blindness
 * would put a `?` on every node of every vanilla Kubernetes cluster — training
 * operators to read the marker that means "we could not look" as decoration.
 */
function blinding(unavailable) {
  return (unavailable ?? []).some((entry) => entry.reason !== 'unsupported');
}

/** Cut a label to what its cell can hold. The canvas has no wrapping. */
function clip(text, max) {
  const value = String(text ?? '');
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/** A 10×10 "reachable from outside" glyph. */
function ExposureGlyph({ x, y }) {
  return (
    <path
      className="admin-topology__glyph"
      transform={`translate(${x} ${y})`}
      d="M1 4 v5 h5 v-2 M5.5 1 h3.5 v3.5 M9 1 l-4.5 4.5"
    />
  );
}

/**
 * One workload, drawn.
 *
 * A `<g>` with `role="button"` rather than an SVG-shaped div: the canvas is one
 * element, and every node still has to be reachable by keyboard and named to a
 * screen reader — which is also the only way the picture is usable at all
 * without a mouse.
 *
 * Two things the first version got wrong, both invisible to a mouse:
 *
 * **The exposure link is a sibling of the button, not a child.** A focusable
 * `<a>` inside `role="button"` is a nested interactive control, and worse, the
 * node's own Enter/Space handler ran on the link's keydown as it bubbled and
 * `preventDefault()`-ed the navigation — the address could be tabbed to and
 * never opened.
 *
 * **Everything the node says is in `aria-label`.** An SVG `<title>` is the
 * mouse tooltip, but `aria-label` outranks it in the accessible-name
 * computation, so a reason left only in the title is a reason a screen reader
 * never reads — and the reason is the whole point of the `?` marker.
 */
function TopologyNode({ node, isSelected, onSelect }) {
  const cx = node.x + 54;
  const cy = node.y + 44;
  const url = node.routes.map(routeUrl).find(Boolean) ?? null;
  const ready = node.workload.replicas?.ready;
  const desired = node.workload.replicas?.desired;
  const status = node.workload.status ?? 'Unknown';

  const exposureSentence =
    node.exposure === 'route'
      ? url
        ? `reachable at ${url}`
        : 'named by an exposure that publishes no host'
      : node.exposure === 'service'
        ? 'fronted by a Service, with no exposure naming it'
        : node.exposure === 'none'
          ? 'no Service in this listing selects its pods'
          : node.exposureReason;

  const description = `${node.kind} ${node.namespace}/${node.name} — ${status}\n${exposureSentence}`;

  return (
    <>
      <g
        className={`admin-topology__node admin-topology__node--${status.toLowerCase()}${
          isSelected ? ' admin-topology__node--selected' : ''
        }`}
        data-testid="topology-node"
        data-node={node.id}
        data-group={node.groupKey}
        data-exposure={node.exposure}
        role="button"
        tabIndex={0}
        aria-pressed={isSelected}
        aria-label={description}
        onClick={() => onSelect(node.id)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onSelect(node.id);
          }
        }}
      >
        <title>{description}</title>
        {/* Invisible until :focus-visible, when it is the keyboard ring around
            the whole cell — the UA's own outline does not render on an SVG
            group in every engine. */}
        <rect
          className="admin-topology__focus"
          x={node.x + 3}
          y={node.y + 3}
          width={NODE_W - 6}
          height={NODE_H - 12}
          rx={8}
        />
        <circle className="admin-topology__disc" cx={cx} cy={cy} r={32} />
        <text className="admin-topology__kind" x={cx} y={cy + 5} textAnchor="middle">
          {abbreviate(node.kind)}
        </text>

        {/* Ready over desired, in the same em dash the tables use when the
            controller has not reported: rule 11.2 does not stop applying
            because the number is on a canvas. */}
        <text
          className="admin-topology__count"
          data-testid="topology-node-count"
          x={cx}
          y={node.y + 92}
          textAnchor="middle"
        >
          {ready == null || desired == null ? '—' : `${ready}/${desired}`}
        </text>
        <text className="admin-topology__name" x={cx} y={node.y + 108} textAnchor="middle">
          {clip(node.name, 13)}
        </text>

        {node.exposure === 'unknown' && (
          <>
            <circle
              className="admin-topology__badge admin-topology__badge--unknown"
              cx={cx + 26}
              cy={cy - 26}
              r={11}
            />
            <text className="admin-topology__badge-text" x={cx + 26} y={cy - 22} textAnchor="middle">
              ?
            </text>
          </>
        )}
      </g>

      {node.exposure === 'route' && (
        <>
          {url ? (
            <a href={url} target="_blank" rel="noreferrer noopener" aria-label={`Open ${url}`}>
              <title>{url}</title>
              <circle className="admin-topology__badge" cx={cx + 26} cy={cy - 26} r={11} />
              <ExposureGlyph x={cx + 21} y={cy - 31} />
            </a>
          ) : (
            <g aria-hidden="true">
              <circle className="admin-topology__badge" cx={cx + 26} cy={cy - 26} r={11} />
              <ExposureGlyph x={cx + 21} y={cy - 31} />
            </g>
          )}
        </>
      )}
    </>
  );
}

/** The `Actions` menu — the same five writes the workload's own page offers. */
function ActionsMenu({ node, gate, namespace, onPick }) {
  const [open, setOpen] = useState(false);
  const spec = WORKLOAD_KINDS[node.plural];
  const scoped = (id) => withScopeNote(gate(id), namespace);
  const items = [
    ['scale', 'Scale…', capabilityGate(node.kind, 'scale', spec, scoped(`scale:${node.plural}`))],
    ['restart', 'Restart…', capabilityGate(node.kind, 'restart', spec, scoped(`patch:${node.plural}`))],
    [
      'suspend',
      node.workload.suspended ? 'Resume…' : 'Suspend…',
      capabilityGate(node.kind, 'suspend', spec, scoped(`patch:${node.plural}`)),
    ],
    ['rollback', 'Roll back…', capabilityGate(node.kind, 'rollback', spec, scoped(`patch:${node.plural}`))],
    ['edit', 'Edit YAML…', scoped(`update:${node.plural}`)],
    ['delete', `Delete ${node.kind}…`, scoped(`delete:${node.plural}`)],
  ];

  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      onSelect={() => setOpen(false)}
      // Width-capped in CSS: rule 11.4's reasons are whole sentences, and an
      // uncapped menu sized itself to the longest of them — 1000px of dropdown
      // laid across the canvas it was opened from.
      className="admin-topology__menu"
      popperProps={{ position: 'right' }}
      toggle={(ref) => (
        <MenuToggle
          ref={ref}
          variant="primary"
          onClick={() => setOpen((value) => !value)}
          isExpanded={open}
          data-testid="topology-actions"
        >
          Actions
        </MenuToggle>
      )}
    >
      <DropdownList>
        {items.map(([id, label, itemGate]) => (
          <DropdownItem
            key={id}
            data-testid={`topology-action-${id}`}
            // Rule 11.4: visible, disabled, and carrying the reason — never
            // hidden, and never a bare "forbidden".
            isAriaDisabled={!itemGate.allowed}
            description={!itemGate.allowed ? itemGate.reason : undefined}
            isDanger={id === 'delete'}
            onClick={() => itemGate.allowed && onPick(id)}
          >
            {label}
          </DropdownItem>
        ))}
      </DropdownList>
    </Dropdown>
  );
}

/**
 * The Services and Routes block, which has three answers and not two.
 *
 * This is the authoritative half of the view: §6's detail reads the pod
 * template, so it sees a Service the canvas's `matchLabels` join cannot prove.
 * The exposures listed here are therefore matched against *its* Services, not
 * against the node's — otherwise the panel would repeat the canvas's summary
 * under a heading that claims to be the object's own answer.
 *
 * `payload` is the detail only when it is this node's detail. `useAsync` clears
 * its data in an effect, so on the render where the selection changes it still
 * holds the previous workload's — one frame of Service names under somebody
 * else's title, and the frame an operator screenshots.
 */
function ExposurePanel({ node, detail, routes }) {
  const answered = detail.data?.workload;
  const payload =
    answered &&
    `${answered.namespace}/${answered.kind}/${answered.name}` === node.id
      ? detail.data
      : null;
  const pending = detail.loading || (detail.data != null && payload == null);

  const detailServices = payload?.services ?? null;
  // Three answers, not two. The §6 detail hands back `services: []` for a
  // namespace with none and names a refused Service listing in `unavailable[]`
  // instead — and a detail read that failed outright answered nothing at all,
  // which must not render as the empty list.
  const servicesUnreadable =
    Boolean(detail.error) || (!pending && detailServices == null) ||
    (payload?.unavailable ?? []).some((entry) => entry.resource === 'services');

  // The exposures that name a Service the object's own read attributes to it.
  const reaching = routesFor(
    routes,
    new Set((detailServices ?? []).map((service) => service.name)),
    node.namespace,
  );

  return (
    <>
      <SectionHeader
        title="Services"
        headingLevel="h3"
        description="Read from the workload's pod template, which is the authority on what a Service selects."
      />
      {pending && <Muted>Reading…</Muted>}
      {!pending && servicesUnreadable && (
        <p data-testid="topology-services-unknown">
          <Muted>
            {detail.error
              ? `This workload's own read failed (${detail.error.message}), so what fronts it is unknown — not nothing.`
              : 'The Service listing for this namespace could not be read, so what fronts this workload is unknown — ' +
                'not nothing. The banner at the top of the page names the reason.'}
          </Muted>
        </p>
      )}
      {!pending && !servicesUnreadable && detailServices != null && (
        <ul className="admin-topology__list" data-testid="topology-services">
          {detailServices.length === 0 && (
            <li>
              <Muted>No Service selects this workload&rsquo;s pods.</Muted>
            </li>
          )}
          {detailServices.map((service) => (
            <li key={service.name}>
              <code>{service.name}</code>{' '}
              <Muted>
                {(service.ports ?? [])
                  .map((port) => `${port.port}/${port.protocol ?? 'TCP'}`)
                  .join(', ') || 'no ports'}
              </Muted>
            </li>
          ))}
        </ul>
      )}

      <SectionHeader title="Routes" headingLevel="h3" />
      {pending ? (
        <Muted>Reading…</Muted>
      ) : servicesUnreadable || node.exposure === 'unknown' ? (
        <p data-testid="topology-routes-unknown">
          <Muted>
            {servicesUnreadable
              ? 'Which Services front this workload is unknown, so which exposures reach it is unknown too — not none.'
              : node.exposureReason}
          </Muted>
        </p>
      ) : reaching.length === 0 ? (
        <p>
          <Muted>
            No Ingress, Route or HTTPRoute in this namespace names a Service that selects these pods.
          </Muted>
        </p>
      ) : (
        <ul className="admin-topology__list" data-testid="topology-routes">
          {reaching.map((route) => {
            const url = routeUrl(route);
            return (
              <li key={route.id}>
                {url ? (
                  <a href={url} target="_blank" rel="noreferrer noopener">
                    {url}
                  </a>
                ) : (
                  <code>{route.name}</code>
                )}{' '}
                <Muted>{route.kind}</Muted>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

export default function Topology() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const navigate = useNavigate();

  const [search, setSearch] = useState('');
  const [dialog, setDialog] = useState(null);

  // The selection is in the URL for rule 11.8's reason: a panel an operator
  // cannot link to is a panel they have to describe over the phone, and "the
  // Deployment I mean is this one" is the whole point of sending someone a
  // topology. `replace` so that clicking around the canvas does not fill the
  // back button with selections.
  const [params, setParams] = useSearchParams();
  const selectedId = params.get('selected');
  const setSelectedId = (id) => {
    const next = new URLSearchParams(params);
    if (id) next.set('selected', id);
    else next.delete('selected');
    setParams(next, { replace: true });
  };

  const enabled = activeClusterId != null;

  const workloads = useAsync(() => workloadsApi.list({ namespace }), {
    key: `topology:${activeClusterId}:${namespace ?? '*'}`,
    enabled,
  });
  const services = useResourceList('core', 'v1', 'services', { namespace, enabled });
  const exposures = useAsync(() => routesApi.list({ namespace }), {
    key: `topology-routes:${activeClusterId}:${namespace ?? '*'}`,
    enabled,
  });

  const rows = workloads.data?.items ?? EMPTY;
  const routeRows = exposures.data?.items ?? EMPTY;

  // Both secondary reads have to have *answered* for a node to be able to say
  // anything about what reaches it. An error, or an `unavailable` entry that is
  // not `unsupported`, makes every node's exposure unknown rather than empty.
  //
  // "Answered" and "not loading" are different, and the difference is a bug
  // that only shows on a slow cluster: the workload listing returns first, and
  // for as long as the other two are in flight every node would be drawn bare
  // — the console saying "nothing reaches any of this" and then quietly taking
  // it back. A *reload* is not that: `useAsync` keeps the previous answer while
  // it re-reads, so a refresh must not blink the whole canvas to unknown.
  const [servicesAnswered, setServicesAnswered] = useState(false);
  useEffect(() => {
    if (!services.loading && !services.error) setServicesAnswered(true);
  }, [services.loading, services.error]);

  // A listing cut off at its limit is the third way to be blind, and it is not
  // an `unavailable` entry: §4 reports it as a `continue` cursor and §13 as its
  // own `truncated[]`. A Service past the cursor still selects pods, so a node
  // drawn from a truncated listing cannot say nothing fronts it.
  const truncated =
    Boolean(services.hasMore) || ((exposures.data?.truncated ?? []).length > 0);

  const exposureKnown =
    servicesAnswered &&
    exposures.data != null &&
    !truncated &&
    !services.error &&
    !exposures.error &&
    !blinding(services.unavailable) &&
    !blinding(exposures.data?.unavailable);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => (row.name ?? '').toLowerCase().includes(needle));
  }, [rows, search]);

  const graph = useMemo(
    () =>
      layoutTopology(
        buildTopology({
          workloads: filtered,
          services: services.items,
          routes: routeRows,
          exposureKnown,
          unknownReason: truncated
            ? 'This namespace has more Services or exposures than one listing returns, so what reaches this ' +
              'workload cannot be worked out from what was read — not nothing.'
            : null,
        }),
      ),
    [filtered, services.items, routeRows, exposureKnown, truncated],
  );

  const nodes = useMemo(() => graph.groups.flatMap((group) => group.nodes), [graph]);
  const selected = nodes.find((node) => node.id === selectedId) ?? null;

  // Only the kinds actually on the canvas, so a namespace of Deployments does
  // not spend five more SelfSubjectAccessReviews asking about kinds nobody can
  // click.
  const plurals = useMemo(
    () => [...new Set(rows.map((row) => KIND_TO_PLURAL[row.kind]).filter(Boolean))].sort(),
    [rows],
  );
  const checks = useMemo(
    () => workloadChecks(namespace, { verbs: ['patch', 'scale', 'update', 'delete'], plurals }),
    [namespace, plurals],
  );
  const { gate } = useGates(checks, { enabled: enabled && plurals.length > 0 });

  const detail = useAsync(
    () => workloadsApi.detail(selected.plural, selected.namespace, selected.name),
    {
      key: `topology-detail:${activeClusterId}:${selectedId ?? ''}`,
      enabled: enabled && selected != null && selected.plural != null,
    },
  );

  const reloadAll = () => {
    workloads.reload();
    services.reload();
    exposures.reload();
    if (selected) detail.reload();
  };

  if (!enabled) {
    return (
      <>
        <PageHeader title="Topology" />
        <NoClusterState what="The topology" />
      </>
    );
  }

  if (workloads.error) {
    return (
      <>
        <PageHeader title="Topology" />
        <ErrorState
          title="The workloads in this scope could not be listed"
          error={workloads.error}
          onRetry={workloads.reload}
        />
      </>
    );
  }

  // One banner for three reads, because there is one picture and each entry
  // names its own resource, namespace and reason. A canvas cannot carry three
  // banners scoped to three regions of itself the way Overview's three cards
  // can: the reads are joined *into* the nodes, so what a refused Services
  // listing qualifies is every node on screen.
  const unavailable = [
    ...(workloads.data?.unavailable ?? []),
    ...(services.unavailable ?? []),
    ...(exposures.data?.unavailable ?? []),
  ];

  const panel = selected && (
    <DrawerPanelContent widths={{ default: 'width_33' }} isResizable>
      <DrawerHead>
        <Title headingLevel="h2" size="lg" data-testid="topology-panel-title">
          {selected.name}
        </Title>
        <Muted>{`${selected.kind} in ${selected.namespace}`}</Muted>
        <DrawerActions>
          <DrawerCloseButton onClick={() => setSelectedId(null)} />
        </DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>
        <div className="admin-topology__panel-actions">
          <ActionsMenu
            node={selected}
            gate={gate}
            namespace={namespace}
            onPick={(id) => setDialog({ id, node: selected })}
          />
          <Button
            variant="secondary"
            onClick={() =>
              navigate(
                `/workloads/${selected.plural}/${encodeURIComponent(selected.namespace)}/${encodeURIComponent(
                  selected.name,
                )}`,
              )
            }
          >
            Open workload
          </Button>
        </div>

        <DescriptionList
          items={[
            {
              label: 'Status',
              value: (
                <StatusBadge
                  status={selected.workload.status}
                  tooltip={selected.workload.status_reason ?? undefined}
                />
              ),
            },
            { label: 'Why', value: selected.workload.status_reason ?? null, hidden: !selected.workload.status_reason },
            {
              label: 'Ready',
              value: (
                // Rule 11.13's canvas gets the same step control the workload
                // page has, gated by the same §9 batch — an operator who found
                // the workload here should not have to leave to add a replica.
                // Still not a new write: the arrows open §6's scale dialog.
                <span className="admin-cell-inline">
                  <UsageCell
                    used={selected.workload.replicas?.ready}
                    total={selected.workload.replicas?.desired}
                    reason="The controller has not reported its replica status, so how many are ready is unknown."
                  />
                  <ScaleStepper
                    kind={selected.kind}
                    plural={selected.plural}
                    namespace={selected.namespace}
                    name={selected.name}
                    current={selected.workload.replicas?.desired ?? null}
                    gate={capabilityGate(
                      selected.kind,
                      'scale',
                      WORKLOAD_KINDS[selected.plural],
                      withScopeNote(gate(`scale:${selected.plural}`), namespace),
                    )}
                    onApplied={reloadAll}
                    testId="topology-scale-stepper"
                  />
                </span>
              ),
            },
            { label: 'Images', value: <ImagesCell images={selected.workload.images} max={3} /> },
            {
              label: 'Application',
              value: selected.application ? (
                <>
                  <code>{selected.application.name}</code> <Muted>{`(${selected.application.label})`}</Muted>
                </>
              ) : (
                <Muted>No application label, so it is drawn on its own.</Muted>
              ),
            },
            {
              label: 'Labels',
              value: (
                <ChipList
                  values={Object.entries(selected.workload.labels ?? {}).map(([key, value]) => `${key}=${value}`)}
                  max={4}
                  emptyText="none"
                />
              ),
            },
            { label: 'Age', value: <AgeCell seconds={selected.workload.age_seconds} /> },
          ]}
        />

        <ExposurePanel node={selected} detail={detail} routes={routeRows} />
      </DrawerPanelBody>
    </DrawerPanelContent>
  );

  return (
    <>
      <PageHeader
        title="Topology"
        subtitle={
          namespace
            ? `Workloads in ${namespace}, grouped by the application labels they carry.`
            : 'Workloads in every namespace, grouped by the application labels they carry. Select a namespace in the masthead to narrow it.'
        }
        actions={[
          <Button key="refresh" variant="secondary" icon={<SyncAltIcon />} onClick={reloadAll}>
            Refresh
          </Button>,
        ]}
      />

      <PartialBanner unavailable={unavailable} />

      {/* Not an `unavailable` entry and so not the banner's job, but the same
          kind of fact: the listings behind the markers stopped at their limit,
          so every node's exposure is unknown rather than absent. Silence here
          would be a cut-off listing rendered as a complete one. */}
      {truncated && (
        <Alert
          isInline
          variant="warning"
          className="admin-topology__truncated"
          data-testid="topology-truncated"
          title="More Services or exposures than one listing returns"
        >
          Every node is marked unknown: a Service past the cursor still selects pods, so nothing here can say what
          does or does not reach a workload. Select a namespace in the masthead to narrow the read.
        </Alert>
      )}

      {/* A `?selected=` that no longer resolves. The URL is the point of the
          selection (rule 11.8), so a link to a workload that has been deleted
          — or to one outside the namespace now selected — says so rather than
          rendering as nothing selected. */}
      {selectedId && !selected && !workloads.loading && (
        <Alert
          isInline
          variant="info"
          data-testid="topology-selection-gone"
          title="The workload this link selected is not on this canvas"
        >
          <code>{selectedId}</code> is not in what was read here. It may have been deleted, or it may be in another
          namespace than the one selected in the masthead.
        </Alert>
      )}

      <Toolbar ariaLabel="Topology filters">
        <Toolbar.Item>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Find by name…"
            ariaLabel="Find a workload by name"
          />
        </Toolbar.Item>
      </Toolbar>

      <Drawer isExpanded={selected != null} isInline>
        <DrawerContent panelContent={panel}>
          <DrawerContentBody>
            {workloads.loading && rows.length === 0 && <Muted>Reading the workloads…</Muted>}

            {!workloads.loading && rows.length === 0 && (
              <EmptyState
                title={
                  unavailable.length
                    ? 'Nothing could be drawn'
                    : namespace
                      ? `Nothing runs in ${namespace}`
                      : 'No workloads'
                }
                description={
                  unavailable.length
                    ? 'Some of the listings behind this view could not be read, so this canvas is not the whole ' +
                      'picture. The banner above names each one.'
                    : 'The workload listing succeeded and matched nothing.'
                }
              />
            )}

            {rows.length > 0 && nodes.length === 0 && (
              <EmptyState
                title="No workload matches that name"
                description="Clear the filter to see the whole scope again."
              />
            )}

            {nodes.length > 0 && (
              <svg
                className="admin-topology"
                data-testid="topology-canvas"
                viewBox={`0 0 ${graph.width} ${graph.height}`}
                width="100%"
                // No `height`: with a viewBox and `height: auto` in CSS the
                // element takes its height from the drawing's own ratio. A
                // fixed pixel height reserved the unscaled height while
                // `meet` scaled the picture down — with the drawer open, a
                // fifth of the canvas was blank space nothing could be in.
                // Left-aligned, because a shrunk drawing floating in the
                // middle of the page reads as a rendering fault.
                preserveAspectRatio="xMinYMin meet"
                role="group"
                aria-label="Workload topology"
              >
                {graph.groups.map((group) => (
                  <g key={group.key}>
                    {group.boxed && (
                      <>
                        <rect
                          className="admin-topology__group"
                          x={group.x}
                          y={group.y}
                          width={group.width}
                          height={group.height}
                          rx={12}
                        >
                          {/* On the rect, not on the enclosing <g>: a <title>
                              there is the tooltip for every node inside the box
                              as well, and the nodes have their own. */}
                          <title>{`Grouped by ${group.label}=${group.title}`}</title>
                        </rect>
                        <text
                          className="admin-topology__group-title"
                          x={group.x + 14}
                          y={group.y + 20}
                          data-testid="topology-group"
                        >
                          {/* Clipped to the box it titles: a one-workload
                              application is 140px wide, and an unclipped
                              heading ran across the group beside it. */}
                          {clip(group.title, Math.max(8, Math.floor((group.width - 28) / 7)))}
                        </text>
                      </>
                    )}
                    {group.nodes.map((node) => (
                      <TopologyNode
                        key={node.id}
                        node={node}
                        isSelected={node.id === selectedId}
                        onSelect={setSelectedId}
                      />
                    ))}
                  </g>
                ))}
              </svg>
            )}
          </DrawerContentBody>
        </DrawerContent>
      </Drawer>

      {dialog?.id === 'scale' && (
        <ScaleDialog
          isOpen
          kind={dialog.node.kind}
          plural={dialog.node.plural}
          namespace={dialog.node.namespace}
          name={dialog.node.name}
          current={dialog.node.workload.replicas?.desired ?? null}
          onClose={() => setDialog(null)}
          onApplied={reloadAll}
        />
      )}
      {dialog?.id === 'restart' && (
        <RestartDialog
          isOpen
          kind={dialog.node.kind}
          plural={dialog.node.plural}
          namespace={dialog.node.namespace}
          name={dialog.node.name}
          onClose={() => setDialog(null)}
          onApplied={reloadAll}
        />
      )}
      {dialog?.id === 'suspend' && (
        <SuspendDialog
          isOpen
          kind={dialog.node.kind}
          plural={dialog.node.plural}
          namespace={dialog.node.namespace}
          name={dialog.node.name}
          suspend={!dialog.node.workload.suspended}
          onClose={() => setDialog(null)}
          onApplied={reloadAll}
        />
      )}
      {dialog?.id === 'rollback' && (
        <RollbackDialog
          isOpen
          kind={dialog.node.kind}
          plural={dialog.node.plural}
          namespace={dialog.node.namespace}
          name={dialog.node.name}
          onClose={() => setDialog(null)}
          onApplied={reloadAll}
        />
      )}
      {dialog?.id === 'edit' && (
        <EditYamlDialog
          isOpen
          group={WORKLOAD_KINDS[dialog.node.plural]?.group}
          version={WORKLOAD_KINDS[dialog.node.plural]?.version}
          plural={dialog.node.plural}
          name={dialog.node.name}
          namespace={dialog.node.namespace}
          kind={dialog.node.kind}
          onClose={() => setDialog(null)}
          onApplied={reloadAll}
        />
      )}
      {dialog?.id === 'delete' && (
        <DeleteDialog
          isOpen
          group={WORKLOAD_KINDS[dialog.node.plural]?.group}
          version={WORKLOAD_KINDS[dialog.node.plural]?.version}
          plural={dialog.node.plural}
          name={dialog.node.name}
          namespace={dialog.node.namespace}
          kind={dialog.node.kind}
          onClose={() => setDialog(null)}
          onApplied={() => {
            // The object is gone, so the selection is a node that no longer
            // exists; leaving the drawer open would offer five more actions
            // against it.
            setSelectedId(null);
            reloadAll();
          }}
        />
      )}
    </>
  );
}
