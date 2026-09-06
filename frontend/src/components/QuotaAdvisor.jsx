/**
 * QuotaAdvisor — §29, answered before the write instead of as a 403 after it.
 *
 * A ResourceQuota refuses at admission, and the refusal is a sentence somebody
 * parses under pressure. Everything needed to answer first is already in the
 * namespace, so this panel does the arithmetic: type what the workload asks
 * for, and it says whether it fits and *which* limit is the tight one.
 *
 * Two things on this panel are not on the 403, and they are why it exists.
 *
 * **The headroom.** "Refused" does not say by how much, or which of a quota's
 * eight limits was the one. The check table says both, so the operator knows
 * whether to trim the request or raise the quota — and which resource to argue
 * about.
 *
 * **The trap.** A quota that bounds a compute resource makes it *compulsory* on
 * every container in the namespace. A pod that omits `requests.cpu` where some
 * quota bounds it is refused with "must specify requests.cpu" — with the quota
 * one percent used — and the fix is a LimitRange, an object the message never
 * mentions. That refusal is listed separately from the headroom checks,
 * because sending someone to raise a limit that is fine wastes the afternoon
 * the message already cost them.
 *
 * **`unknown` is rendered as unknown.** An unwritten `status.used`, a scoped
 * quota, or a LimitRange listing that did not answer all make the verdict
 * `unknown`, and it is drawn in its own colour with its own sentence. Drawing
 * it as "admitted" would give the roomiest possible answer at the moment the
 * console knows least, to the person about to deploy.
 */
import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormGroup,
  Grid,
  GridItem,
  TextInput,
} from '@patternfly/react-core';
import { DataTable, EmptyState, SectionHeader, StatusBadge } from './ui';
import { quota as quotaApi } from '../api/client';
import { useAsync } from '../pages/_data';
import { Muted } from '../pages/_parts';

const VERDICT = {
  admitted: { status: 'Ready', label: 'Would be admitted' },
  refused: { status: 'failed', label: 'Would be refused' },
  unknown: { status: 'Unknown', label: 'Cannot be determined' },
};

/** The sentence under the verdict — what it means and what to do. */
const VERDICT_DETAIL = {
  admitted:
    'Every quota that bounds this workload has room for it, and every resource those quotas make compulsory is set. This is arithmetic over what the quotas report — the API server still admits, and §4’s dry-run create is what asks it.',
  refused:
    'At least one bound is exceeded, or a container omits a resource some quota makes compulsory. Both are listed below, and they are fixed in different places.',
  unknown:
    'The arithmetic could not be completed honestly — a usage the quota controller has not written yet, a quota whose scopes decide whether it counts this workload at all, or a listing that did not answer. This is not a “probably fine”.',
};

export default function QuotaAdvisor({ namespace }) {
  const [replicas, setReplicas] = useState('1');
  const [cpu, setCpu] = useState('');
  const [memory, setMemory] = useState('');
  const [submitted, setSubmitted] = useState(null);

  const { data: advice, loading, error } = useAsync(
    () => quotaApi.advice(namespace),
    { key: `quota-advice:${namespace}` },
  );

  const preview = useAsync(
    () => (submitted ? quotaApi.preview(namespace, submitted) : Promise.resolve(null)),
    { key: `quota-preview:${namespace}:${JSON.stringify(submitted)}`, enabled: Boolean(submitted) },
  );

  const run = useCallback(() => {
    const requests = {};
    if (cpu.trim()) requests.cpu = cpu.trim();
    if (memory.trim()) requests.memory = memory.trim();
    setSubmitted({
      replicas: Number(replicas) || 1,
      // One container, named so the "must specify" rows have something to point
      // at. A real pod has more, and the panel says so rather than pretending
      // this is the whole estimate.
      containers: [{ name: 'container', requests }],
    });
  }, [replicas, cpu, memory]);

  const result = preview.data?.preview ?? null;
  const mandatory = advice?.mandatory ?? null;

  const checkColumns = useMemo(
    () => [
      { key: 'quota', title: 'Quota', sortable: true },
      { key: 'resource', title: 'Bound', sortable: true },
      { key: 'needed', title: 'This workload needs' },
      {
        key: 'headroom',
        title: 'Room left',
        cell: (row) =>
          row.headroom === null ? (
            <Muted>unknown — usage not written</Muted>
          ) : (
            row.headroom
          ),
      },
      {
        key: 'verdict',
        title: '',
        cell: (row) => (
          <span data-testid={`quota-check-${row.quota}-${row.resource}`}>
            <StatusBadge
              status={VERDICT[row.verdict]?.status ?? 'Unknown'}
              label={row.verdict}
            />
          </span>
        ),
      },
    ],
    [],
  );

  if (error) {
    return (
      <Alert isInline variant="warning" data-testid="quota-advice-error"
             title="What bounds this namespace could not be read">
        {error.message} Without it, nothing here can say whether a workload would be
        admitted — and guessing would be the answer this console exists not to give.
      </Alert>
    );
  }

  return (
    <div data-testid="quota-advisor">
      <SectionHeader
        title="Will the next workload be admitted?"
        description="Arithmetic over what the quotas report, before a manifest exists. The API server is what admits; this says which limit is tight and how much room is left, which a 403 does not."
      />

      {/* The trap, and it is worth showing whether or not anyone runs a
          preview: it refuses pods at any level of quota usage, and the fix is
          an object the refusal never names. */}
      {(advice?.findings ?? []).map((finding) => (
        <Alert
          key={finding.code}
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid={`quota-finding-${finding.code}`}
          title={finding.label}
        >
          {finding.detail}
        </Alert>
      ))}

      {Array.isArray(mandatory) && mandatory.length > 0 && (
        <p data-testid="quota-mandatory">
          Every container here must state{' '}
          <strong>{mandatory.join(', ')}</strong>
          {Object.keys(advice?.containerDefaults ?? {}).length > 0 ? (
            <>
              {' '}— a LimitRange supplies{' '}
              <code>
                {Object.entries(advice.containerDefaults)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(', ')}
              </code>{' '}
              for the ones it covers.
            </>
          ) : (
            <> — and no LimitRange supplies any of them.</>
          )}
        </p>
      )}

      {advice?.quotas === null && (
        <Alert isInline variant="warning" data-testid="quota-listing-unavailable"
               title="The quota listing did not answer">
          This is not the same as “nothing bounds this namespace”, and it is not
          rendered as one.
        </Alert>
      )}

      {Array.isArray(advice?.quotas) && advice.quotas.length === 0 && (
        <EmptyState
          title="Nothing bounds this namespace"
          description="No ResourceQuota exists here, so no workload is refused for consuming too much — and nothing is compulsory on a container either."
        />
      )}

      <Form className="admin-form" onSubmit={(e) => { e.preventDefault(); run(); }}>
        <Grid hasGutter>
          <GridItem span={3}>
            <FormGroup label="Replicas" fieldId="quota-replicas">
              <TextInput id="quota-replicas" value={replicas} type="number"
                         onChange={(_e, v) => setReplicas(v)}
                         data-testid="quota-replicas" aria-label="Replicas" />
            </FormGroup>
          </GridItem>
          <GridItem span={3}>
            <FormGroup label="CPU request" fieldId="quota-cpu">
              <TextInput id="quota-cpu" value={cpu} placeholder="500m"
                         onChange={(_e, v) => setCpu(v)}
                         data-testid="quota-cpu" aria-label="CPU request per container" />
            </FormGroup>
          </GridItem>
          <GridItem span={3}>
            <FormGroup label="Memory request" fieldId="quota-memory">
              <TextInput id="quota-memory" value={memory} placeholder="1Gi"
                         onChange={(_e, v) => setMemory(v)}
                         data-testid="quota-memory" aria-label="Memory request per container" />
            </FormGroup>
          </GridItem>
          <GridItem span={3}>
            <FormGroup label="&nbsp;" fieldId="quota-run">
              <Button variant="secondary" onClick={run} isDisabled={loading}
                      data-testid="quota-run">
                Check it
              </Button>
            </FormGroup>
          </GridItem>
        </Grid>
      </Form>

      {result && (
        <>
          <Alert
            isInline
            variant={
              result.verdict === 'refused' ? 'danger'
                : result.verdict === 'unknown' ? 'warning' : 'success'
            }
            className="admin-confirm__alert"
            data-testid="quota-verdict"
            data-verdict={result.verdict}
            title={VERDICT[result.verdict]?.label ?? result.verdict}
          >
            {VERDICT_DETAIL[result.verdict]}
          </Alert>

          {result.unsetMandatory.length > 0 && (
            <Alert
              isInline
              variant="danger"
              className="admin-confirm__alert"
              data-testid="quota-must-specify"
              title="A container omits a resource this namespace makes compulsory"
            >
              <p>
                {result.unsetMandatory
                  .map((entry) => `${entry.container} omits ${entry.resource}`)
                  .join('; ')}
                . The pod is refused with <code>must specify …</code>{' '}
                <strong>whatever the quota usage is</strong> — this is not a
                headroom problem, and raising the limit does not fix it.
              </p>
              <p>
                Set the value on the container, or add a LimitRange with a{' '}
                <code>defaultRequest</code> for it. The refusal names the field,
                never the missing LimitRange.
              </p>
            </Alert>
          )}

          <DataTable
            ariaLabel="Quota checks"
            tableId="quota-checks"
            columns={checkColumns}
            rows={result.checks}
            rowKey={(row) => `${row.quota}/${row.resource}`}
            resizableColumns={false}
            empty={
              <EmptyState
                title="No bound applies to this workload"
                description="Nothing in this namespace counts what it asks for."
              />
            }
          />
        </>
      )}
    </div>
  );
}
