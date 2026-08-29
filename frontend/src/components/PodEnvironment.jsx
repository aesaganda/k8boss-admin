/**
 * PodEnvironment — §7.6 `GET /api/pods/{namespace}/{name}/environment`.
 *
 * A container's environment is not a list of values, and the whole point of this
 * panel is to stop rendering it as one. Half of it is *references* — to
 * ConfigMap keys, to Secret keys, to fields the kubelet substitutes when the
 * container starts — and a viewer that printed `env[].value` and nothing else
 * would show four variables out of thirty, with the other twenty-six looking
 * like they do not exist.
 *
 * So every row carries a `value_state`, and this component branches on it rather
 * than on whether the value is empty. Four blanks that would otherwise look
 * identical:
 *
 *   `withheld`    it comes from a Secret. **This console does not show Secret
 *                 values here.** The key is named, so the row is identifiable.
 *   `absent`      the ConfigMap was read and has no such key. Unless the
 *                 reference is optional, the container will not start — which is
 *                 a finding, not a blank.
 *   `unreadable`  the ConfigMap or Secret could not be read. Named in the banner
 *                 above; the variable is *not* known to be empty.
 *   `runtime`     a `fieldRef` — the kubelet computes it at start and the API
 *                 server never stores the result. There is nothing to show and
 *                 nothing to guess.
 *   `literal` / `resolved` — an actual value, and the only two states where the
 *                 blank you see is the value the container sees.
 *
 * Rendering them all as an empty cell is the defect this file exists to avoid:
 * "this variable is unset", "we will not tell you", "we could not look" and
 * "the key it names is not there" send an operator to four different places.
 */
import { useMemo, useState } from 'react';
import { Button, Tooltip } from '@patternfly/react-core';
import SyncAltIcon from '@patternfly/react-icons/dist/esm/icons/sync-alt-icon';
import EyeIcon from '@patternfly/react-icons/dist/esm/icons/eye-icon';
import EyeSlashIcon from '@patternfly/react-icons/dist/esm/icons/eye-slash-icon';
import { DataTable, PartialBanner, SearchInput, Toolbar } from './ui';
import { pods as podsApi } from '../api/client';
import { truncate } from '../utils/format';
import { useAsync } from '../pages/_data';
import { Muted } from '../pages/_parts';

/**
 * What each `value_state` means on screen, in the words an operator needs.
 *
 * Keyed by the backend's closed vocabulary. An unknown state falls through to a
 * neutral "not shown" rather than to an empty cell, because a state this console
 * has not been taught about is still not evidence that the variable is unset.
 */
const STATES = {
  withheld: {
    text: 'Secret value',
    tone: 'withheld',
    hint:
      'This variable comes from a Secret. The console does not display Secret values in the ' +
      'environment view — this screen is the one people share. The key it reads is shown ' +
      'beside it.',
  },
  absent: {
    text: 'Key not present',
    tone: 'absent',
    hint:
      'The object this variable names was read and does not contain this key. Unless the ' +
      'reference is marked optional, the kubelet will refuse to start the container — this is ' +
      'a finding, not a missing reading.',
  },
  unreadable: {
    text: 'Could not be read',
    tone: 'unreadable',
    hint:
      'The object this variable comes from could not be read, so its value is unknown. It is ' +
      'NOT known to be empty. The banner above names what failed.',
  },
  runtime: {
    text: 'Set by the kubelet',
    tone: 'runtime',
    hint:
      'The kubelet substitutes this when the container starts. The API server never stores the ' +
      'result, so there is no value to show and guessing one would be a claim about what is ' +
      'running inside the container.',
  },
};

const UNKNOWN_STATE = {
  text: 'Not shown',
  tone: 'unreadable',
  hint: 'This console does not recognise how this variable gets its value, so it will not guess at one.',
};

/** `configMapKeyRef` → `ConfigMap checkout-config · flags`, for the Source column. */
function describeSource(source) {
  if (!source) return 'Set directly in the pod spec';
  const { kind, name, key } = source;
  switch (kind) {
    case 'configMapKeyRef':
      return `ConfigMap ${name} · ${key}`;
    case 'secretKeyRef':
      return `Secret ${name} · ${key}`;
    case 'configMapRef':
      return key ? `ConfigMap ${name} · ${key}` : `every key of ConfigMap ${name}`;
    case 'secretRef':
      return key ? `Secret ${name} · ${key}` : `every key of Secret ${name}`;
    case 'fieldRef':
      return `Pod field ${name}`;
    case 'resourceFieldRef':
      return key ? `Resource ${name} of container ${key}` : `Resource ${name}`;
    default:
      return 'An expression this console does not recognise';
  }
}

/**
 * The value cell.
 *
 * A real value renders as text, clamped and revealed on demand — a base64 blob
 * or a 4 KB JSON document in a table cell makes every other row unreadable. Any
 * other state renders as the *named* reason it is not there, never as blank.
 */
function ValueCell({ variable, revealed }) {
  const resolved = variable.value_state === 'literal' || variable.value_state === 'resolved';
  if (!resolved) {
    const state = STATES[variable.value_state] ?? UNKNOWN_STATE;
    return (
      <Tooltip content={state.hint}>
        <span className="admin-env__state" data-tone={state.tone} tabIndex={0}>
          {state.text}
        </span>
      </Tooltip>
    );
  }
  if (variable.value === '') return <Muted>empty string</Muted>;
  const text = String(variable.value);
  return (
    <span className="admin-env__value" title={revealed ? undefined : text}>
      {revealed ? text : truncate(text, 90)}
    </span>
  );
}

export function PodEnvironment({ namespace, name, clusterId }) {
  const [search, setSearch] = useState('');
  const [revealed, setRevealed] = useState(false);

  const environment = useAsync(() => podsApi.environment(namespace, name), {
    key: `pod-env:${clusterId}:${namespace}/${name}`,
  });

  const rows = useMemo(() => {
    const items = environment.data?.items ?? [];
    return items.flatMap((entry) =>
      entry.variables.map((variable, index) => ({
        ...variable,
        container: entry.container,
        containerKind: entry.kind,
        // Names are not unique within a container — an `envFrom` key overridden
        // by an explicit `env` appears twice, deliberately — so the row key
        // carries the position as well.
        id: `${entry.kind}/${entry.container}/${index}`,
      })),
    );
  }, [environment.data]);

  const columns = useMemo(
    () => [
      {
        key: 'container',
        title: 'Container',
        sortable: true,
        cell: (row) => (
          <span className="admin-cell-inline">
            {row.container}
            {row.containerKind !== 'container' && (
              <Muted>{row.containerKind === 'init' ? 'init' : 'debug'}</Muted>
            )}
          </span>
        ),
      },
      {
        key: 'name',
        title: 'Variable',
        sortable: true,
        value: (row) => row.name ?? '',
        cell: (row) =>
          row.name ? (
            <span className={row.overridden ? 'admin-env__name admin-env__name--overridden' : 'admin-env__name'}>
              {row.name}
            </span>
          ) : (
            // The stand-in row for an `envFrom` whose object could not be read:
            // we know a set of variables comes from it and cannot name them.
            // Reporting zero of them would be the wrong answer with nothing on
            // screen to contradict it.
            <Tooltip content="Every key of this object becomes a variable here. The object could not be read, so their names are unknown.">
              <span className="admin-env__name admin-env__name--unknown" tabIndex={0}>
                an unknown set of variables
              </span>
            </Tooltip>
          ),
      },
      {
        key: 'value',
        title: 'Value',
        value: (row) => (row.value == null ? row.value_state : String(row.value)),
        cell: (row) => <ValueCell variable={row} revealed={revealed} />,
      },
      {
        key: 'source',
        title: 'Source',
        value: (row) => describeSource(row.source),
        cell: (row) => (
          <span className="admin-cell-inline">
            <Muted>{describeSource(row.source)}</Muted>
            {row.overridden && (
              <Tooltip content="A later entry in this container defines the same variable, so this one is not what the container sees.">
                <span className="admin-env__state" data-tone="overridden" tabIndex={0}>
                  overridden
                </span>
              </Tooltip>
            )}
          </span>
        ),
      },
    ],
    [revealed],
  );

  return (
    <>
      <PartialBanner unavailable={environment.data?.unavailable} />

      <Toolbar ariaLabel="Environment filters">
        <Toolbar.Item>
          <SearchInput value={search} onChange={setSearch} placeholder="Filter by name, value, source…" />
        </Toolbar.Item>
        <Toolbar.Item>
          <Muted>Secret values are never shown here, whatever this console is configured to allow elsewhere.</Muted>
        </Toolbar.Item>
        <Toolbar.Spacer />
        <Toolbar.Item>
          <Button
            variant="link"
            isInline
            icon={revealed ? <EyeSlashIcon /> : <EyeIcon />}
            onClick={() => setRevealed((on) => !on)}
            data-testid="pod-env-expand"
          >
            {revealed ? 'Clamp long values' : 'Show full values'}
          </Button>
        </Toolbar.Item>
        <Toolbar.Item>
          <Button
            variant="plain"
            aria-label="Refresh environment"
            icon={<SyncAltIcon />}
            onClick={environment.reload}
          />
        </Toolbar.Item>
      </Toolbar>

      <DataTable
        ariaLabel="Environment variables"
        tableId="pods:environment"
        manageableColumns
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        loading={environment.loading && !environment.data}
        error={environment.error}
        onRetry={environment.reload}
        filterText={search}
        emptyTitle="No environment variables"
        emptyDescription={
          environment.data?.partial
            ? 'Some sources could not be read — see the banner above. This table is not a complete answer.'
            : 'Every container in this pod starts with the image default environment and nothing added.'
        }
      />
    </>
  );
}

export default PodEnvironment;
