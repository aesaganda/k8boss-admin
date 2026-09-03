/**
 * Storage — PersistentVolumeClaims, PersistentVolumes and StorageClasses.
 *
 * The subtle column here is `capacity_bytes` on a PVC. §8 takes it from
 * `status.capacity` — the size actually provisioned — and deliberately does not
 * fall back to `spec.resources.requests`, because that is what was *asked for*.
 * A Pending claim therefore has a null capacity, and it renders as an em dash:
 * reporting the request as a capacity would tell an operator that a claim stuck
 * in Pending has 500 GiB of storage behind it.
 *
 * `is_default` on a StorageClass reads both the GA and the beta annotation
 * backend-side, which is why a cluster upgraded from 1.5 does not show every
 * class as non-default here.
 */
import { useMemo, useState } from 'react';
import {
  ActionButton,
  AgeCell,
  DescriptionList,
  menuAction,
  NullableCell,
  PageHeader,
  ResourceLink,
  StatusBadge,
} from '../components/ui';
import ExpandClaimDialog from '../components/ExpandClaimDialog';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes } from '../utils/format';
import { ChipList, genericTab, Muted, NoClusterState, ResourceTabsPage, YamlPanel } from './_parts';
import { useGates } from './_data';

//: §20's write is a patch on the claim itself, so the action is gated on the
//: verb the write will actually use rather than on anything in it.
const CHECKS = [
  { id: 'patch', verb: 'patch', group: 'core', resource: 'persistentvolumeclaims' },
];

export default function Storage() {
  const { activeClusterId } = useCluster();
  const [expanding, setExpanding] = useState(null);
  // Bumped after a write so the claims listing is read again: a table still
  // showing the old requested size is the moment a console stops being believed.
  const [refreshToken, setRefreshToken] = useState(0);
  const { gate } = useGates(CHECKS, { enabled: activeClusterId != null });

  const tabs = useMemo(
    () => [
      {
        key: 'pvcs',
        title: 'PersistentVolumeClaims',
        group: 'core',
        version: 'v1',
        plural: 'persistentvolumeclaims',
        namespaced: true,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'The listing succeeded and returned no claims in this scope.',
        detailTitle: (row) => `PVC ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          { key: 'namespace', title: 'Namespace', sortable: true },
          {
            key: 'status',
            title: 'Status',
            facet: { options: ['Bound', 'Pending', 'Lost'] },
            sortable: true,
            cell: (row) => (
              <StatusBadge
                status={row.status}
                tooltip={
                  row.status === 'Pending'
                    ? 'Nothing has been provisioned for this claim yet. Its capacity is unknown, not zero.'
                    : undefined
                }
              />
            ),
          },
          {
            key: 'capacity_bytes',
            title: 'Capacity',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.capacity_bytes}
                format={formatBytes}
                reason="Nothing is bound to this claim yet, so no capacity has been provisioned. This is not the size it requested."
              />
            ),
          },
          {
            // Beside Capacity rather than instead of it. The two agree on a
            // settled claim and differ on one mid-expansion, and that gap is the
            // only thing in this table that says an earlier resize has not
            // finished — §8's capacity column alone cannot show it.
            key: 'requested_bytes',
            title: 'Requested',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.requested_bytes}
                format={formatBytes}
                reason="This claim's spec carries no storage request, so what it asked for is unknown. It is not the capacity beside it."
              />
            ),
          },
          {
            key: 'volume',
            title: 'Volume',
            cell: (row) =>
              row.volume ? (
                <ResourceLink group="" version="v1" plural="persistentvolumes" name={row.volume} />
              ) : (
                <NullableCell value={null} reason="This claim is not bound to a PersistentVolume." />
              ),
          },
          {
            key: 'access_modes',
            title: 'Access',
            value: (row) => (row.access_modes ?? []).join(','),
            cell: (row) => <ChipList values={row.access_modes} max={2} emptyText="unset" />,
          },
          {
            key: 'storage_class',
            title: 'Class',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.storage_class}
                reason="No storageClassName is set, so this claim binds through the cluster's default class."
              />
            ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        refreshToken,
        actions: (row) => [
          menuAction('Expand…', gate('patch'), () => setExpanding(row)),
        ],
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                { label: 'Status', value: <StatusBadge status={row.status} /> },
                {
                  label: 'Requested',
                  value: (
                    <NullableCell
                      value={row.requested_bytes}
                      format={formatBytes}
                      reason="This claim's spec carries no storage request. It is not the capacity below."
                    />
                  ),
                },
                {
                  label: 'Provisioned capacity',
                  value: (
                    <NullableCell
                      value={row.capacity_bytes}
                      format={formatBytes}
                      reason="Nothing is bound yet. This is not the requested size."
                    />
                  ),
                },
                { label: 'Volume', value: row.volume ?? null },
                { label: 'Storage class', value: row.storage_class ?? null },
                { label: 'Access modes', value: <ChipList values={row.access_modes} max={4} emptyText="unset" /> },
              ]}
            />
            <div style={{ marginTop: '0.75rem' }}>
              <ActionButton gate={gate('patch')} onClick={() => setExpanding(row)}>
                Expand…
              </ActionButton>
            </div>
            <YamlPanel
              group="core"
              version="v1"
              plural="persistentvolumeclaims"
              name={row.name}
              namespace={row.namespace}
              height={340}
            />
          </>
        ),
      },

      {
        key: 'pvs',
        title: 'PersistentVolumes',
        group: 'core',
        version: 'v1',
        plural: 'persistentvolumes',
        namespaced: false,
        rowKey: (row) => row.name,
        emptyDescription:
          'No PersistentVolumes. On a cluster that provisions dynamically this is normal until the first claim binds.',
        detailTitle: (row) => `PersistentVolume ${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          {
            key: 'status',
            title: 'Status',
            sortable: true,
            facet: { options: ['Available', 'Bound', 'Released', 'Failed'] },
            cell: (row) => <StatusBadge status={row.status} />,
          },
          {
            key: 'capacity_bytes',
            title: 'Capacity',
            sortable: true,
            cell: (row) => <NullableCell value={row.capacity_bytes} format={formatBytes} />,
          },
          {
            key: 'claim',
            title: 'Claim',
            cell: (row) => (
              <NullableCell
                value={row.claim}
                reason="This volume is not claimed. Whether that is safe depends on its reclaim policy."
              />
            ),
          },
          { key: 'reclaim_policy', title: 'Reclaim', sortable: true },
          { key: 'storage_class', title: 'Class', sortable: true },
          {
            key: 'access_modes',
            title: 'Access',
            value: (row) => (row.access_modes ?? []).join(','),
            cell: (row) => <ChipList values={row.access_modes} max={2} emptyText="unset" />,
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <YamlPanel group="core" version="v1" plural="persistentvolumes" name={row.name} height={420} />
        ),
      },

      {
        key: 'storageclasses',
        title: 'StorageClasses',
        group: 'storage.k8s.io',
        version: 'v1',
        plural: 'storageclasses',
        namespaced: false,
        rowKey: (row) => row.name,
        emptyDescription: 'This cluster serves the StorageClass API and has none defined.',
        detailTitle: (row) => `StorageClass ${row?.name}`,
        columns: [
          {
            key: 'name',
            title: 'Name',
            sortable: true,
            cell: (row) => (
              <span>
                {row.name}
                {row.is_default && <Muted> · default</Muted>}
              </span>
            ),
          },
          { key: 'provisioner', title: 'Provisioner', sortable: true },
          { key: 'reclaim_policy', title: 'Reclaim', sortable: true },
          {
            key: 'volume_binding_mode',
            title: 'Binding',
            sortable: true,
            cell: (row) => (
              <span
                title={
                  row.volume_binding_mode === 'WaitForFirstConsumer'
                    ? 'Provisioning is deferred until a pod using the claim is scheduled, so a claim staying Pending here is expected.'
                    : undefined
                }
              >
                {row.volume_binding_mode}
              </span>
            ),
          },
          {
            key: 'is_default',
            title: 'Default',
            sortable: true,
            cell: (row) => (row.is_default ? <StatusBadge status="True" label="Default" /> : <Muted>no</Muted>),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <YamlPanel group="storage.k8s.io" version="v1" plural="storageclasses" name={row.name} height={420} />
        ),
      },

      // VolumeAttributesClass is a newer, still-graduating API (storage.k8s.io/v1,
      // GA'd late enough that many clusters don't serve it yet). "Not present on
      // this cluster" here is a normal, calm outcome, not a bug to chase.
      genericTab({
        key: 'volumeattributesclasses',
        title: 'Volume Attributes Classes',
        group: 'storage.k8s.io',
        version: 'v1',
        plural: 'volumeattributesclasses',
        namespaced: false,
      }),
    ],
    [gate, refreshToken],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Storage" />
        <NoClusterState what="Storage" />
      </>
    );
  }

  return (
    <>
      {expanding && (
        <ExpandClaimDialog
          claim={expanding}
          onClose={() => setExpanding(null)}
          onApplied={() => setRefreshToken((n) => n + 1)}
        />
      )}
      <ResourceTabsPage
        title="Storage"
        subtitle="Claims, the volumes behind them, and the classes that provision them."
        tabs={tabs}
      />
    </>
  );
}
