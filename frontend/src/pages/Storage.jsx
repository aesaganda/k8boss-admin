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
import SnapshotClaimDialog from '../components/SnapshotClaimDialog';
import { useCluster } from '../contexts/ClusterContext';
import { formatBytes } from '../utils/format';
import { ChipList, genericTab, Muted, NoClusterState, ResourceTabsPage, YamlPanel } from './_parts';
import { useGates } from './_data';

//: §20's write is a patch on the claim itself, so the action is gated on the
//: verb the write will actually use rather than on anything in it.
const CHECKS = [
  { id: 'patch', verb: 'patch', group: 'core', resource: 'persistentvolumeclaims' },
  // §22 creates a different object in a different group, so it is a separate
  // check: a console allowed to resize a claim is very often not allowed to
  // snapshot one, and one gate answering for both would disable or enable the
  // wrong button.
  { id: 'snapshot', verb: 'create', group: 'snapshot.storage.k8s.io', resource: 'volumesnapshots' },
];

export default function Storage() {
  const { activeClusterId } = useCluster();
  const [expanding, setExpanding] = useState(null);
  const [snapshotting, setSnapshotting] = useState(null);
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
          menuAction('Snapshot…', gate('snapshot'), () => setSnapshotting(row)),
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
      // §22. Not `genericTab`s: a raw VolumeSnapshot manifest cannot say the one
      // thing worth knowing about it, because the answer is a *bool — `null`
      // while the storage system is still taking it, and a generic row would
      // render that blank beside a snapshot that failed hours ago.
      //
      // `snapshot.storage.k8s.io` is CRD-backed and shipped by the
      // external-snapshotter rather than by Kubernetes, so a cluster without it
      // renders these as §1.2's calm "not present on this cluster".
      {
        key: 'volumesnapshots',
        title: 'Volume Snapshots',
        group: 'snapshot.storage.k8s.io',
        version: 'v1',
        plural: 'volumesnapshots',
        namespaced: true,
        refreshToken,
        rowKey: (row) => `${row.namespace}/${row.name}`,
        emptyDescription: 'The listing succeeded and returned no snapshots in this scope.',
        detailTitle: (row) => `VolumeSnapshot ${row?.namespace}/${row?.name}`,
        columns: [
          { key: 'name', title: 'Name', sortable: true },
          { key: 'namespace', title: 'Namespace', sortable: true },
          {
            key: 'ready_to_use',
            title: 'Ready',
            sortable: true,
            value: (row) => (row.ready_to_use == null ? '' : row.ready_to_use ? 'yes' : 'no'),
            cell: (row) =>
              row.ready_to_use === true ? (
                <StatusBadge
                  status="Ready"
                  label="Ready"
                  tooltip="The storage system reports this snapshot as usable. It is still held inside that storage system — it is not a backup."
                />
              ) : row.ready_to_use === false ? (
                <StatusBadge
                  status="NotReady"
                  label="Not ready"
                  tooltip={row.error?.message || 'The controller reports this snapshot as unusable.'}
                />
              ) : (
                <NullableCell
                  value={null}
                  reason="The controller has not reported on this snapshot yet — which is what the first minutes of a large one look like. It is not a report that the snapshot failed, and it is not a report that it succeeded."
                />
              ),
          },
          {
            key: 'source_claim',
            title: 'Source',
            value: (row) => row.source_claim || row.source_content || '',
            cell: (row) =>
              row.source_claim ? (
                row.source_claim
              ) : row.source_content ? (
                // Adopted from content that already existed in the storage
                // system rather than taken from a claim. Different object,
                // different meaning, so it is not flattened into one column.
                <span>
                  {row.source_content} <Muted>(adopted content)</Muted>
                </span>
              ) : (
                <NullableCell value={null} reason="This snapshot names neither a claim nor existing content." />
              ),
          },
          {
            key: 'restore_size_bytes',
            title: 'Restore size',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.restore_size_bytes}
                format={formatBytes}
                reason="The storage system has not reported a restore size yet. This is not a snapshot of nothing."
              />
            ),
          },
          {
            key: 'snapshot_class',
            title: 'Class',
            sortable: true,
            cell: (row) => (
              <NullableCell
                value={row.snapshot_class}
                reason="No class is named on this snapshot, so the cluster default was used when it was created."
              />
            ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <>
            <DescriptionList
              items={[
                {
                  label: 'Ready to use',
                  value:
                    row.ready_to_use === true ? (
                      <StatusBadge status="Ready" label="Yes" />
                    ) : row.ready_to_use === false ? (
                      <StatusBadge status="NotReady" label="No" />
                    ) : (
                      <NullableCell value={null} reason="Not reported by the controller yet." />
                    ),
                },
                {
                  label: 'Bound content',
                  value: (
                    <NullableCell
                      value={row.bound_content}
                      reason="No VolumeSnapshotContent is bound yet, so nothing in the storage system is holding this snapshot."
                    />
                  ),
                },
                {
                  label: 'Taken at',
                  value: (
                    <NullableCell
                      value={row.creation_time}
                      reason="The storage system has not reported when it took this snapshot."
                    />
                  ),
                },
                ...(row.error
                  ? [{ label: 'Error', value: <Muted>{row.error.message}</Muted> }]
                  : []),
              ]}
            />
            <YamlPanel
              group="snapshot.storage.k8s.io"
              version="v1"
              plural="volumesnapshots"
              name={row.name}
              namespace={row.namespace}
              height={340}
            />
          </>
        ),
      },

      {
        key: 'volumesnapshotclasses',
        title: 'Snapshot Classes',
        group: 'snapshot.storage.k8s.io',
        version: 'v1',
        plural: 'volumesnapshotclasses',
        namespaced: false,
        rowKey: (row) => row.name,
        emptyDescription: 'This cluster serves the VolumeSnapshotClass API and defines none.',
        detailTitle: (row) => `VolumeSnapshotClass ${row?.name}`,
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
          { key: 'driver', title: 'Driver', sortable: true },
          {
            key: 'deletion_policy',
            title: 'On delete',
            sortable: true,
            // The column this tab exists for. Under Delete, removing a
            // namespaced VolumeSnapshot destroys the snapshot in the storage
            // system; under Retain it does not. The person deleting one will be
            // looking at that namespaced object, not at this class.
            cell: (row) =>
              row.deletion_policy === 'Delete' ? (
                <StatusBadge
                  status="Unknown"
                  label="Delete"
                  tooltip="Deleting a VolumeSnapshot in this class destroys the underlying snapshot in the storage system."
                />
              ) : row.deletion_policy === 'Retain' ? (
                <StatusBadge
                  status="Ready"
                  label="Retain"
                  tooltip="Deleting a VolumeSnapshot in this class leaves the underlying snapshot in place."
                />
              ) : (
                <NullableCell value={null} reason="This class declares no deletionPolicy." />
              ),
          },
          { key: 'age_seconds', title: 'Age', sortable: true, cell: (row) => <AgeCell seconds={row.age_seconds} /> },
        ],
        detail: (row) => (
          <YamlPanel
            group="snapshot.storage.k8s.io"
            version="v1"
            plural="volumesnapshotclasses"
            name={row.name}
            height={420}
          />
        ),
      },

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
      {snapshotting && (
        <SnapshotClaimDialog
          namespace={snapshotting.namespace}
          claim={snapshotting.name}
          onClose={() => setSnapshotting(null)}
          onApplied={() => setRefreshToken((n) => n + 1)}
        />
      )}
      <ResourceTabsPage
        basePath="/storage"
        subtitle="Claims, the volumes behind them, and the classes that provision them."
        tabs={tabs}
      />
    </>
  );
}
