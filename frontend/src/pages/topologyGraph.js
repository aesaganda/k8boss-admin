/**
 * topologyGraph — three listings, arranged into a picture.
 *
 * Rule 11.13's view draws §6's workloads, groups them the way their own labels say
 * they belong together, and marks the ones something outside the cluster can
 * reach. Everything here is a pure function of rows that have already been
 * read: no fetching, no clock, no DOM. That is the same rule
 * `resources/shaping.py` holds the backend's shapers to, and for the same
 * reason — a layout that could read from a cluster could also fail to, and then
 * the place that decides whether a missing edge means "there is none" or "we
 * could not look" would have nowhere to record the difference.
 *
 * **The one thing a picture must not do is imply a connection it has not
 * proved.** A drawn line is read as fact, and a workload drawn with no route on
 * it is read as "nothing outside can reach this" — which, said about the wrong
 * pod during an incident, is the sentence somebody acts on. So:
 *
 *   - A Service matches a workload only when its selector is a subset of the
 *     workload's `selector` (§6's `matchLabels`). That direction proves the
 *     match: the API server requires a Deployment's `matchLabels` to be present
 *     on its pod template, so a selector contained in `matchLabels` is
 *     contained in the template's labels and really does pick these pods.
 *   - The converse does not hold, which is why the join is three-valued and
 *     `exposure` has an `unknown` state rather than defaulting to `none`.
 *     `matchLabels` is a *subset* of the template's labels, so a Service
 *     selecting on a key the row does not carry can neither be attached nor
 *     ruled out (`undecidableServices`) — and a workload whose row carries no
 *     `matchLabels` at all (an expression-only selector, a CronJob) cannot be
 *     attributed either. Both get the marker and a sentence instead of being
 *     drawn bare. The workload's own page (§6 detail) reads the pod template
 *     and is the authority; the drawer asks it.
 *   - When the Services or Routes listing failed, has not answered, or stopped
 *     at its limit, every node is `unknown`. Rule 1 of the contract, applied to
 *     a drawing: an empty canvas is never allowed to mean an empty namespace.
 */
import { KIND_TO_PLURAL } from './_data';

/**
 * The labels that say "these objects are one application", most specific first.
 *
 * `part-of` is the Kubernetes-recommended label for exactly this and is what
 * OpenShift's own topology groups on; `name` and the bare `app` are what almost
 * every Helm chart and `kubectl create deployment` actually writes. Falling
 * through them in order means the grouping works on charts that predate the
 * recommended set, and the group header names which label it used — a group an
 * operator cannot account for is a group they distrust.
 */
export const APPLICATION_LABELS = ['app.kubernetes.io/part-of', 'app.kubernetes.io/name', 'app'];

/** `{ name, label }` for the application this workload belongs to, or `null`. */
export function applicationOf(workload) {
  const labels = workload?.labels ?? {};
  for (const label of APPLICATION_LABELS) {
    const value = labels[label];
    if (typeof value === 'string' && value.trim() !== '') return { name: value, label };
  }
  return null;
}

/**
 * The letters inside the node, which are the kind and nothing else.
 *
 * OpenShift draws these and they are worth copying: on a canvas the name is the
 * only text with room to be read, so without this a DaemonSet and a Deployment
 * are the same circle, and "why does scaling it do nothing" follows.
 */
const ABBREVIATIONS = {
  Deployment: 'D',
  StatefulSet: 'SS',
  DaemonSet: 'DS',
  Job: 'J',
  CronJob: 'CJ',
  ReplicaSet: 'RS',
};

export function abbreviate(kind) {
  return ABBREVIATIONS[kind] ?? (kind ? kind.slice(0, 2).toUpperCase() : '?');
}

/**
 * The Services whose selector this workload's row *proves* it satisfies.
 *
 * A Service with no selector is excluded, which is the same judgement
 * `app/services/workloads.py::_matching_services` makes: it is backed by
 * manually managed EndpointSlices, so it does not select these pods, and
 * drawing it would tell an operator that traffic reaches a workload it does not
 * reach.
 */
export function servicesFor(workload, services) {
  const selector = workload?.selector ?? {};
  if (!Object.keys(selector).length) return [];
  return (services ?? []).filter((service) => {
    if (service?.namespace !== workload?.namespace) return false;
    const wanted = service?.selector ?? {};
    if (!Object.keys(wanted).length) return false;
    return Object.entries(wanted).every(([key, value]) => selector[key] === value);
  });
}

/**
 * The Services this row can neither attach **nor rule out**.
 *
 * The third answer, and the one that keeps the absence of a marker honest.
 * `matchLabels` is a *subset* of the pod template's labels — the API server
 * requires the former to be on the latter and says nothing about the rest — so
 * a Service selecting on a key the row does not carry may well select these
 * pods. The row cannot say, and neither can the canvas.
 *
 * A key the row *does* carry with a different value is the one disagreement
 * that settles it: the template carries `matchLabels` verbatim, so such a
 * Service really does not select these pods. That is what stops this from
 * swallowing every Service in the namespace — only the genuinely undecidable
 * ones come back, and they turn the node's marker into `?` rather than leaving
 * it drawn bare, which is the claim that nothing reaches it.
 */
export function undecidableServices(workload, services) {
  const selector = workload?.selector ?? {};
  if (!Object.keys(selector).length) return [];
  return (services ?? []).filter((service) => {
    if (service?.namespace !== workload?.namespace) return false;
    const wanted = Object.entries(service?.selector ?? {});
    if (!wanted.length) return false;
    if (wanted.some(([key, value]) => key in selector && selector[key] !== value)) return false;
    return wanted.some(([key]) => !(key in selector));
  });
}

/**
 * The §13 exposures that name any of these Services as a target.
 *
 * Matched by Service name **and** namespace: a target's namespace is
 * `target.namespace ?? route.namespace` (an HTTPRoute `backendRef` with no
 * namespace targets its own route's namespace), which must equal the
 * workload's. A target whose `kind` is set to something other than `Service`
 * is never drawn against a workload — this join only proves Service
 * attachment.
 */
export function routesFor(routes, serviceNames, namespace) {
  if (!serviceNames.size) return [];
  return (routes ?? []).filter((route) =>
    (route?.targets ?? []).some((target) => {
      if (!target?.service || !serviceNames.has(target.service)) return false;
      if (target.kind && target.kind !== 'Service') return false;
      return (target.namespace ?? route?.namespace) === namespace;
    }),
  );
}

/**
 * The address an exposure answers on, or `null` when it names no host.
 *
 * An offer to open it, never a claim that it answers: whether a controller has
 * admitted the exposure is `admitted`'s job, and on a cluster with no wildcard
 * DNS this will not resolve at all. The Routes page (§13) builds the same
 * string from the same two fields.
 */
export function routeUrl(route) {
  const host = (route?.hosts ?? [])[0];
  if (!host) return null;
  return `${route?.tls?.termination ? 'https' : 'http'}://${host}${route?.path ?? ''}`;
}

/**
 * One node per workload, grouped by application.
 *
 * `exposureKnown` is false when either secondary listing failed, was cut off at
 * its limit, or has not answered yet, and `unknownReason` is the sentence that
 * says which. It is a parameter rather than something inferred from an empty
 * array here, because
 * `[]` from a refused listing and `[]` from a namespace with no Services are
 * the two things this whole module exists to keep apart — and the caller is the
 * one holding the `unavailable[]` entry that says which happened.
 */
export function buildTopology({ workloads, services, routes, exposureKnown = true, unknownReason = null }) {
  const nodes = (workloads ?? []).map((workload) => {
    const matched = exposureKnown ? servicesFor(workload, services) : [];
    const names = new Set(matched.map((service) => service.name));
    const reaching = exposureKnown ? routesFor(routes, names, workload.namespace) : [];
    const attributable = Object.keys(workload?.selector ?? {}).length > 0;
    const undecidable = exposureKnown && attributable ? undecidableServices(workload, services) : [];

    let exposure = 'none';
    if (!exposureKnown || !attributable) exposure = 'unknown';
    else if (reaching.length) exposure = 'route';
    // Ahead of `service` and of `none`: a proved attachment is worth drawing
    // even while another Service is undecidable, but an *absence* is not
    // claimable while one is.
    else if (undecidable.length) exposure = 'unknown';
    else if (matched.length) exposure = 'service';

    return {
      id: `${workload.namespace}/${workload.kind}/${workload.name}`,
      workload,
      kind: workload.kind,
      name: workload.name,
      namespace: workload.namespace,
      plural: KIND_TO_PLURAL[workload.kind] ?? null,
      application: applicationOf(workload),
      services: matched,
      routes: reaching,
      exposure,
      // Why the node cannot say anything about its exposure — shown on the node
      // and in the drawer, because a marker without a sentence is a shrug.
      exposureReason: !exposureKnown
        ? unknownReason ??
          'The Services or Routes listing could not be read, so what reaches this workload is unknown — not nothing.'
        : !attributable
          ? 'This workload’s row carries no matchLabels (an expression-only selector, or a CronJob, which has none), ' +
            'so the Services that select its pods cannot be worked out from this listing. Its own page reads the pod template.'
          : undecidable.length
            ? `${undecidable.map((service) => service.name).join(', ')} selects on a label this row does not carry. ` +
              'A row carries matchLabels only and the pod template may carry more, so whether that Service reaches ' +
              'this workload cannot be settled here — the workload’s own page reads the template.'
            : null,
    };
  });

  const groups = new Map();
  for (const node of nodes) {
    // Namespace and the *label* in the key, not just its value. Two namespaces
    // both running `app=checkout` are two applications, and merging them would
    // draw a cross-namespace grouping nothing in the cluster asserts; two
    // workloads that agree on the value under different labels
    // (`part-of: shop` and `app: shop`) are likewise not the same application,
    // and the box would carry one of their labels as an explanation of both.
    //
    // The prefixes are words rather than a control character. A literal NUL
    // here is what `.gitattributes` in this repo exists because of: it makes
    // git call the file binary, and `grep -r` stops printing its lines.
    const key = node.application
      ? `app:${node.namespace}/${node.application.label}=${node.application.name}`
      : `loose:${node.id}`;
    // On the node too: it is what the canvas puts in `data-group`, which is
    // the only thing that makes "this workload is not in that box" assertable
    // from outside the component.
    node.groupKey = key;
    const existing = groups.get(key);
    if (existing) existing.nodes.push(node);
    else
      groups.set(key, {
        key,
        title: node.application?.name ?? null,
        label: node.application?.label ?? null,
        namespace: node.namespace,
        boxed: Boolean(node.application),
        nodes: [node],
      });
  }

  // A total order, so the canvas does not reshuffle under the operator's cursor
  // on every refresh. Boxed applications first and alphabetical; the loose
  // workloads after them, also alphabetical.
  const ordered = [...groups.values()].sort((a, b) => {
    if (a.boxed !== b.boxed) return a.boxed ? -1 : 1;
    const left = `${a.namespace ?? ''}/${a.title ?? a.nodes[0].name}`;
    const right = `${b.namespace ?? ''}/${b.title ?? b.nodes[0].name}`;
    return left.localeCompare(right);
  });
  for (const group of ordered) {
    group.nodes.sort((a, b) => a.name.localeCompare(b.name));
  }
  return ordered;
}

/* ── Layout ─────────────────────────────────────────────────────────────── */

export const NODE_W = 108;
export const NODE_H = 126;
export const GROUP_PAD = 16;
export const GROUP_TITLE_H = 26;
export const GAP = 20;
export const CANVAS_W = 960;

/**
 * Shelf-pack the groups into a fixed-width canvas.
 *
 * Fixed width, not measured: the SVG scales to its container through its
 * viewBox, so the picture is identical at every window size and there is no
 * resize observer, no re-layout on a sidebar toggle, and nothing to get wrong
 * on the first paint. Four columns is the cap, so no group is ever wider than
 * the canvas and the wrap below cannot loop.
 */
export function layoutTopology(groups, { width = CANVAS_W } = {}) {
  const placed = [];
  let x = GAP;
  let shelfY = GAP;
  let shelfHeight = 0;

  for (const group of groups) {
    const count = group.nodes.length;
    const columns = Math.max(1, Math.min(4, Math.ceil(Math.sqrt(count))));
    const rows = Math.ceil(count / columns);
    const titleHeight = group.boxed ? GROUP_TITLE_H : 0;
    const boxWidth = columns * NODE_W + GROUP_PAD * 2;
    const boxHeight = rows * NODE_H + GROUP_PAD * 2 + titleHeight;

    if (x > GAP && x + boxWidth > width - GAP) {
      x = GAP;
      shelfY += shelfHeight + GAP;
      shelfHeight = 0;
    }

    placed.push({
      ...group,
      x,
      y: shelfY,
      width: boxWidth,
      height: boxHeight,
      nodes: group.nodes.map((node, index) => ({
        ...node,
        x: x + GROUP_PAD + (index % columns) * NODE_W,
        y: shelfY + GROUP_PAD + titleHeight + Math.floor(index / columns) * NODE_H,
      })),
    });

    x += boxWidth + GAP;
    shelfHeight = Math.max(shelfHeight, boxHeight);
  }

  return { groups: placed, width, height: shelfY + shelfHeight + GAP };
}
