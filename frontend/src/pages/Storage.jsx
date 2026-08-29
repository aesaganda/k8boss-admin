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
import { useMemo } from 'react';
import {
  AgeCell,
  DescriptionList,
  NullableCell,
  PageHeader,
  ResourceLink,
  StatusBadge,
} from '../components/ui';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes } from '../utils/format';
import { ChipList, genericTab, Muted, NoClusterState, ResourceTabsPage, YamlPanel } from './_parts';

export default function Storage() {
  const { activeClusterId } = useCluster();

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
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                { label: 'Status', value: <StatusBadge status={row.status} /> },
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
    [],
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
    <ResourceTabsPage
      title="Storage"
      subtitle="Claims, the volumes behind them, and the classes that provision them."
      tabs={tabs}
    />
  );
}
