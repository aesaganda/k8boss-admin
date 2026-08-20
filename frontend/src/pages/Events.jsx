/**
 * Events — §5.
 *
 * Two kinds of filter, and the difference matters. Type, namespace and the
 * involved object are **server-side**: they become a `fieldSelector`, so the
 * API server applies them before the scan budget is spent, and the newest 200
 * matching events come back rather than the newest 200 events of which a handful
 * match. The message box is **client-side**, over the rows already in memory.
 *
 * Sorting is the backend's: newest `last_seen` first, with events that have no
 * derivable timestamp at the bottom rather than at the top — an unknown age must
 * not be shown as the most recent thing that happened. `DataTable` is therefore
 * left unsorted by default here; asking it to re-sort would replace a total
 * order the backend established with one over the page it happened to return.
 *
 * The window is the cluster's, not ours: the API server keeps events for about
 * an hour by default. An empty table is "nothing recently", never "nothing ever",
 * and the page says so where an operator will read it.
 */
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import {
  AgeCell,
  DataTable,
  NullableCell,
  PageHeader,
  PartialBanner,
  ResourceLink,
  SearchInput,
  StatusBadge,
  Toolbar,
} from '../components/ui';
import { events as eventsApi } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { truncate } from '../utils/format';
import { useAsync } from './_data';
import { Muted, NoClusterState, PickList } from './_parts';

const TYPES = [
  { value: 'Warning', label: 'Warning' },
  { value: 'Normal', label: 'Normal' },
];

// The kinds that actually emit events worth filtering on. Case-sensitive,
// because the field selector is: `pod` matches nothing and returns an empty
// table that looks like a quiet cluster.
const KINDS = [
  'Pod',
  'Deployment',
  'ReplicaSet',
  'StatefulSet',
  'DaemonSet',
  'Job',
  'CronJob',
  'Node',
  'Service',
  'Ingress',
  'PersistentVolumeClaim',
  'HorizontalPodAutoscaler',
].map((kind) => ({ value: kind, label: kind }));

const LIMIT = 200;

export default function Events() {
  const { activeClusterId } = useCluster();
  const { selected: namespace } = useNamespace();
  const [searchParams, setSearchParams] = useSearchParams();

  const [type, setType] = useState(searchParams.get('type'));
  const [kind, setKind] = useState(searchParams.get('kind'));
  const [objectName, setObjectName] = useState(searchParams.get('name') ?? '');
  const [text, setText] = useState('');

  // The URL is the shareable form of this page's state — "look at these events"
  // is a link somebody pastes into an incident channel — so the three
  // server-side filters are mirrored into it. `replace` keeps the back button
  // meaning "the previous page", not "the previous filter keystroke".
  useEffect(() => {
    const next = {};
    if (type) next.type = type;
    if (kind) next.kind = kind;
    if (objectName) next.name = objectName;
    setSearchParams(next, { replace: true });
  }, [type, kind, objectName, setSearchParams]);

  const { data, loading, error, reload } = useAsync(
    () =>
      eventsApi.list({
        namespace,
        type: type ?? undefined,
        involvedObjectKind: kind ?? undefined,
        involvedObjectName: objectName || undefined,
        limit: LIMIT,
      }),
    {
      key: `events:${activeClusterId}:${namespace ?? '*'}:${type ?? '*'}:${kind ?? '*'}:${objectName}`,
      enabled: activeClusterId != null,
    },
  );

  const rows = data?.items ?? [];

  const columns = useMemo(
    () => [
      {
        key: 'last_seen',
        title: 'Last seen',
        cell: (row) => (
          <AgeCell
            timestamp={row.last_seen}
            reason="This event carries no usable timestamp, so how long ago it happened is unknown."
          />
        ),
      },
      {
        key: 'type',
        title: 'Type',
        cell: (row) => <StatusBadge status={row.type} />,
      },
      { key: 'reason', title: 'Reason' },
      {
        key: 'involved',
        title: 'Object',
        value: (row) => `${row.involved?.kind ?? ''}/${row.involved?.name ?? ''}`,
        cell: (row) => (
          <span>
            <Muted>{`${row.involved?.kind ?? '?'}/`}</Muted>
            <ResourceLink
              kind={row.involved?.kind}
              name={row.involved?.name}
              namespace={row.involved?.namespace}
            />
          </span>
        ),
      },
      { key: 'namespace', title: 'Namespace' },
      {
        key: 'count',
        title: 'Count',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.count}
            reason="This event's series count was not reported, so how many times it happened is unknown."
          />
        ),
      },
      {
        key: 'message',
        title: 'Message',
        modifier: 'truncate',
        cell: (row) => <span title={row.message ?? undefined}>{truncate(row.message, 90)}</span>,
      },
      {
        key: 'source',
        title: 'Source',
        value: (row) => row.source?.component ?? '',
        cell: (row) => (
          <Muted title={row.source?.host ?? undefined}>{row.source?.component ?? '—'}</Muted>
        ),
      },
    ],
    [],
  );

  if (activeClusterId == null) {
    return (
      <>
        <PageHeader title="Events" />
        <NoClusterState what="The event stream" />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Events"
        subtitle="The API server keeps events for roughly an hour. An empty table means nothing recently, not nothing ever."
      />

      <PartialBanner unavailable={data?.unavailable} />

      <Toolbar ariaLabel="Event filters">
        <Toolbar.Item>
          <PickList id="event-type" label="Type" value={type} options={TYPES} onChange={setType} placeholder="Any type" />
        </Toolbar.Item>
        <Toolbar.Item>
          <PickList id="event-kind" label="Object kind" value={kind} options={KINDS} onChange={setKind} placeholder="Any kind" />
        </Toolbar.Item>
        <Toolbar.Item>
          <div className="admin-filterbar__field">
            <label className="admin-filterbar__label" htmlFor="event-object">
              Object name
            </label>
            <SearchInput
              id="event-object"
              value={objectName}
              onChange={setObjectName}
              // Submit-only: this is a server-side field selector, and one
              // request per keystroke turns a name into forty API server calls.
              onSearch={setObjectName}
              placeholder="Exact name, then Enter"
              ariaLabel="Filter by involved object name"
            />
          </div>
        </Toolbar.Item>
        <Toolbar.Item>
          <div className="admin-filterbar__field">
            <label className="admin-filterbar__label" htmlFor="event-text">
              Message contains
            </label>
            <SearchInput
              id="event-text"
              value={text}
              onChange={setText}
              placeholder="Filter the rows below…"
              ariaLabel="Filter events by message"
            />
          </div>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Muted>{namespace ? `Namespace ${namespace}` : 'All namespaces'}</Muted>
        </Toolbar.Item>
        <Toolbar.Item>
          <Button variant="plain" aria-label="Refresh events" icon={<SyncAltIcon />} onClick={reload} />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Events"
        // No Filter menu: Type, kind and namespace are asked of the API server
        // above this table (a field selector, not a client-side pass), and a
        // second filter with its own counts over the fetched page would be two
        // answers to one question.
        manageableColumns
        columns={columns}
        rows={rows}
        rowKey={(row, index) => `${row.namespace ?? ''}/${row.involved?.name ?? ''}/${row.reason ?? ''}/${row.last_seen ?? index}`}
        loading={loading}
        error={error}
        onRetry={reload}
        filterText={text}
        emptyTitle="No matching events"
        emptyDescription={
          data?.partial
            ? 'The scan was cut short or a source could not be read — see the banner above.'
            : `Nothing in the API server's event window matches these filters. Up to ${LIMIT} of the newest matching events are shown.`
        }
        footer={
          rows.length >= LIMIT ? (
            <Muted>{`Showing the newest ${LIMIT} matching events. Narrow by type, kind or object to see further back.`}</Muted>
          ) : null
        }
      />
    </>
  );
}
