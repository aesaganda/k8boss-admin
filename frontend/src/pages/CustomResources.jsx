/**
 * CustomResources — every CRD-sourced kind this cluster serves, grouped by API
 * group (e.g. `cilium.io`), with the ability to open one group's kind and
 * browse its live instances.
 *
 * There is no `isCRD` flag on a catalog item — `/resources/catalog` (§4)
 * carries no such marker — so "custom resource" is defined here by
 * elimination: anything whose group is not one of Kubernetes' own built-in API
 * groups. That makes `BUILTIN_GROUPS` load-bearing rather than decorative: a
 * built-in group missing from it would show up here mislabelled as a CRD, and
 * a real CRD group added to it by mistake would silently vanish from this
 * page while still being perfectly reachable from the API explorer.
 *
 * A cluster with none of this installed is an ordinary cluster, not a broken
 * one (§1.2 — "no Ingress CRDs and no metrics.k8s.io are ordinary facts"), so
 * that state renders as a calm `EmptyState`, never as an error.
 *
 * Instance browsing is the exact `Listing` component the API explorer already
 * uses for one resource type's objects — this page is a second, curated way
 * to reach it, not a second implementation of it. That is also how this page
 * gets a `Create <Kind>…` button for every CRD without writing one: the button
 * lives in `Listing`, and the kind, the verbs and the namespacing it needs all
 * come off the same catalog entry the card was rendered from.
 */
import { useMemo, useState } from 'react';
import { Button, Card, CardBody, CardTitle } from '@patternfly/react-core';
import { EmptyState, ErrorState, LoadingState, PageHeader, PartialBanner } from '../components/ui';
import { realGroup, resources as resourcesApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useAsync } from './_data';
import { ChipList, Muted, NoClusterState } from './_parts';
import { Listing } from './Explorer';

// Every API group Kubernetes itself serves. Anything reaching the catalog
// under a group not in this set came from a CustomResourceDefinition — there
// is no flag on a catalog item to key off instead, so "custom" is defined by
// elimination against this list.
const BUILTIN_GROUPS = new Set([
  '', 'apps', 'batch', 'networking.k8s.io', 'storage.k8s.io',
  'rbac.authorization.k8s.io', 'policy', 'coordination.k8s.io',
  'admissionregistration.k8s.io', 'autoscaling', 'autoscaling.k8s.io',
  'scheduling.k8s.io', 'discovery.k8s.io', 'node.k8s.io',
  'apiextensions.k8s.io', 'apiregistration.k8s.io', 'events.k8s.io',
  'certificates.k8s.io', 'authentication.k8s.io', 'authorization.k8s.io',
  'gateway.networking.k8s.io',
]);

/** One group's kinds, as a card. */
function GroupCard({ group, items, onOpen }) {
  return (
    <Card>
      <CardTitle>{group}</CardTitle>
      <CardBody>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
          {items.map((item) => (
            <div
              key={`${item.group}/${item.version}/${item.resource}`}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap' }}
            >
              <div>
                <Button variant="link" isInline onClick={() => onOpen(item)}>
                  {item.kind}
                </Button>
                <Muted>
                  {` ${item.resource} · ${item.version}`}
                  {item.preferred && ' · preferred'}
                </Muted>
              </div>
              <ChipList values={item.shortNames} max={3} emptyText="no short names" />
            </div>
          ))}
        </div>
      </CardBody>
    </Card>
  );
}

export default function CustomResources() {
  const { activeClusterId } = useCluster();

  // Its own fetch rather than sharing Explorer's: every page in this lane owns
  // the reads it needs (Config.jsx, Network.jsx, Explorer.jsx each do), so no
  // page's catalog view depends on another page having mounted first.
  const catalog = useAsync(() => resourcesApi.catalog(), {
    key: `catalog:${activeClusterId}`,
    enabled: activeClusterId != null,
  });

  const [selected, setSelected] = useState(null);

  const groups = useMemo(() => {
    const items = catalog.data?.items ?? [];
    const custom = items.filter((item) => !BUILTIN_GROUPS.has(realGroup(item.group)));
    const byGroup = new Map();
    for (const item of custom) {
      const key = realGroup(item.group);
      if (!byGroup.has(key)) byGroup.set(key, []);
      byGroup.get(key).push(item);
    }
    for (const list of byGroup.values()) {
      list.sort((a, b) => a.kind.localeCompare(b.kind) || a.version.localeCompare(b.version));
    }
    return [...byGroup.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [catalog.data]);

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Custom Resources" />
        <NoClusterState what="Custom Resources" />
      </>
    );
  }

  if (catalog.error) {
    return (
      <>
        <PageHeader title="Custom Resources" />
        <ErrorState title="The API catalog could not be loaded" error={catalog.error} onRetry={catalog.reload} />
      </>
    );
  }

  if (catalog.loading && !catalog.data) {
    return (
      <>
        <PageHeader title="Custom Resources" />
        <LoadingState label="Loading the API catalog…" />
      </>
    );
  }

  if (selected) {
    const real = realGroup(selected.group);
    return (
      <>
        <PageHeader
          title={selected.kind}
          subtitle={`${real || 'core'}/${selected.version} · ${selected.resource}`}
          actions={
            <Button variant="link" isInline onClick={() => setSelected(null)}>
              ← Back to Custom Resources
            </Button>
          }
        />
        <Listing
          // Keyed on the triple: switching kinds must reset the drawer and any
          // accumulated `continue` pages rather than reinterpret them against
          // a different resource.
          key={`${real}/${selected.version}/${selected.resource}`}
          group={real}
          version={selected.version}
          plural={selected.resource}
          catalog={catalog}
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Custom Resources"
        subtitle="Every resource this cluster serves through a CustomResourceDefinition, grouped by API group."
      />
      <PartialBanner
        unavailable={catalog.data?.unavailable}
        title="Discovery was incomplete — some API groups could not be enumerated"
      />
      {groups.length === 0 ? (
        <EmptyState
          title="No custom resources"
          description={
            catalog.data?.partial
              ? 'Discovery was incomplete, so it is also possible this cluster has CRDs whose group could not be enumerated — see the banner above.'
              : 'This cluster serves no CustomResourceDefinitions. That is an ordinary cluster state, not a failure.'
          }
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {groups.map(([group, items]) => (
            <GroupCard key={group} group={group} items={items} onOpen={setSelected} />
          ))}
        </div>
      )}
    </>
  );
}
