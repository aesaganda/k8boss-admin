/**
 * CsrDecisionDialog — §25's approve/deny, wrapped in the §11.3 handshake.
 *
 * The whole dialog is one screen of decoded certificate request, because the
 * screens it replaces show none of it. `kubectl get csr` lists a name, a signer,
 * a requestor and an age; `kubectl certificate approve` takes a name. Neither
 * shows what the request asks to **become**, and that is a different field from
 * who asked: `spec.username` is the submitter, and the common name and
 * organizations inside `spec.request` are the identity the certificate would
 * carry.
 *
 * A request whose organizations include `system:masters` is a cluster-admin
 * credential the API server honours ahead of RBAC — no Role bounds it, no
 * binding revokes it, and only rotating the CA takes it back. On every other
 * screen it looks exactly like a kubelet renewing its certificate.
 *
 * Two banners are permanent rather than checkboxes, because they are true of
 * every decision and a checkbox that always appears is one nobody reads:
 *
 * **The decision is final.** The API server refuses any update that rewrites an
 * Approved or Denied condition. There is no un-approve.
 *
 * **Approving is not issuing.** It records a condition; a signer then has to
 * act. `Issued` — not `Approved` — is the state that says a certificate exists.
 */
import { useCallback, useMemo } from 'react';
import { Alert, DescriptionListDescription } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import ConsequenceChecklist from './ConsequenceChecklist';
import { blockedByConsequences, useAcknowledgements } from './consequences';
import { AgeCell, DescriptionList, NullableCell, SectionHeader, StatusBadge } from './ui';
import { certificates as certificatesApi } from '../api/client';
import { useAsync } from '../pages/_data';

const MUTED = { color: 'var(--admin-muted, #6a6e73)' };

/** The identity the certificate would carry, or the reason nobody could read it. */
export function SubjectPanel({ request }) {
  if (!request) return null;

  if (!request.subject) {
    return (
      <Alert
        isInline
        variant="warning"
        className="admin-confirm__alert"
        data-testid="csr-undecodable"
        title="This console could not decode the request"
      >
        {request.decode_error} The subject, the organizations and the SANs are{' '}
        <strong>unknown, not empty</strong> — approving means signing a certificate
        whose identity nobody on this screen has seen.
      </Alert>
    );
  }

  const organizations = request.subject.organizations ?? [];
  return (
    <DescriptionList
      items={[
        {
          label: 'Common name',
          value: request.subject.common_name ?? (
            <NullableCell
              value={null}
              reason="The request carries no common name, so the certificate would name no user."
            />
          ),
        },
        {
          label: 'Organizations',
          value: organizations.length ? (
            <span data-testid="csr-organizations">
              {organizations.map((org) => (
                <StatusBadge
                  key={org}
                  // `system:masters` is cluster-admin ahead of RBAC. It does not
                  // share a colour with an ordinary group.
                  status={org === 'system:masters' ? 'failed' : 'info'}
                  label={org}
                  tooltip={
                    org === 'system:masters'
                      ? 'The API server treats this group as cluster-admin before RBAC is consulted. No Role bounds it and no binding revokes it.'
                      : 'This becomes one of the identity’s groups.'
                  }
                />
              ))}
            </span>
          ) : (
            <span style={MUTED}>none — the certificate would carry no groups</span>
          ),
        },
        {
          label: 'Subject alternative names',
          value:
            (request.dns_names ?? []).length || (request.ip_addresses ?? []).length ? (
              <code>{[...request.dns_names, ...request.ip_addresses].join(', ')}</code>
            ) : (
              <span style={MUTED}>none requested</span>
            ),
        },
        {
          label: 'Key',
          value: request.key?.algorithm ? (
            `${request.key.algorithm}${request.key.curve ? ` ${request.key.curve}` : ''}${
              request.key.size ? ` (${request.key.size} bits)` : ''
            }`
          ) : (
            <NullableCell
              value={null}
              reason="This console does not recognise the key type, so it cannot judge its strength either."
            />
          ),
        },
        {
          label: 'Self-signature',
          value:
            request.signature_valid === true ? (
              <StatusBadge status="Ready" label="verifies" />
            ) : request.signature_valid === false ? (
              <StatusBadge
                status="failed"
                label="does not verify"
                tooltip="The request is not signed by the key it carries, so whoever submitted it may not hold that key."
              />
            ) : (
              <NullableCell value={null} reason="The request could not be decoded." />
            ),
        },
      ]}
    />
  );
}

/** Who asked, which is not who they asked to become. */
function RequestorPanel({ request }) {
  return (
    <DescriptionList
      items={[
        { label: 'Requested by', value: request?.requestor ?? <span style={MUTED}>unknown</span> },
        {
          label: 'Signer',
          value: (
            <span data-testid="csr-signer">
              <code>{request?.signer_name}</code>{' '}
              {request?.signer_known === false && (
                <StatusBadge
                  status="Unknown"
                  label="no built-in signer"
                  tooltip="kube-controller-manager signs only the three kubernetes.io/… signerNames. This one needs a controller somebody installed."
                />
              )}
            </span>
          ),
        },
        { label: 'Usages', value: <code>{(request?.usages ?? []).join(', ') || 'none'}</code> },
        { label: 'State', value: <StatusBadge status={request?.state} /> },
        { label: 'Age', value: <AgeCell seconds={request?.age_seconds} /> },
      ]}
    />
  );
}

export default function CsrDecisionDialog({ name, decision, onClose, onApplied }) {
  const approving = decision === 'Approved';

  const { data: plan, loading, error } = useAsync(
    () => certificatesApi.plan(name, decision),
    { key: `csr-plan:${name}:${decision}` },
  );

  const consequences = useMemo(() => plan?.consequences ?? [], [plan]);
  const [acknowledged, setAcknowledged, unacknowledged] = useAcknowledgements(
    consequences,
    decision,
  );

  const resourceVersion = plan?.resourceVersion ?? null;
  const request = plan?.request;

  const requestBody = useCallback(
    (dryRun) =>
      certificatesApi.decide(name, {
        decision,
        resourceVersion,
        acknowledgeConsequences: acknowledged,
        dryRun,
      }),
    [name, decision, resourceVersion, acknowledged],
  );

  let previewDisabledReason = null;
  if (error) previewDisabledReason = error.message;
  else if (loading && !plan) previewDisabledReason = 'Still reading the request…';
  else if (plan?.blocked) previewDisabledReason = plan.blocked.message;
  else if (unacknowledged.length)
    previewDisabledReason = `Acknowledge what this decision means: ${unacknowledged
      .map((entry) => entry.label)
      .join('; ')}.`;

  return (
    <MutationDialog
      isOpen
      title={`${approving ? 'Approve' : 'Deny'} ${name}`}
      description={
        approving
          ? 'Read what this request asks to become before approving it. The identity in the certificate is not the identity that submitted the request, and only one of them is on the list you came from.'
          : 'Denying records that this certificate may not be signed. The requester has to submit a new request.'
      }
      request={requestBody}
      resourceVersion={resourceVersion}
      canPreview={!previewDisabledReason}
      previewDisabledReason={previewDisabledReason}
      previewLabel="Preview the decision"
      confirmLabel={approving ? 'Approve the request' : 'Deny the request'}
      isDanger={approving}
      autoPreview={false}
      confirmBlockedReason={blockedByConsequences(acknowledged)}
      onClose={onClose}
      onApplied={onApplied}
      variant="large"
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${name} is now ${approving ? 'approved' : 'denied'}`,
              body: approving
                ? 'That is the condition, not a certificate. A signer has to act before one exists — re-read this request and look for Issued rather than Approved. The decision itself cannot be changed.'
                : 'The requester has to submit a new request; this one cannot be re-decided.',
            }
          : {
              variant: 'warning',
              title: 'The write completed without confirming it was applied',
              body: 'Re-read the request before assuming the decision was recorded.',
            }
      }
    >
      <>
        {plan?.gate?.enabled === false && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="csr-disabled"
            title="This console runs read-only"
          >
            {plan.gate.detail}
          </Alert>
        )}

        {error && (
          <Alert
            isInline
            variant="danger"
            className="admin-confirm__alert"
            data-testid="csr-plan-error"
            title="This request could not be read"
          >
            {error.message}
            {error.hint ? ` ${error.hint}` : ''}
          </Alert>
        )}

        {plan?.blocked && (
          <Alert
            isInline
            variant="info"
            className="admin-confirm__alert"
            data-testid="csr-blocked"
            title="This request has already been decided"
          >
            {plan.blocked.message} {plan.blocked.hint}
          </Alert>
        )}

        <SectionHeader
          title="What this certificate would be"
          description="Decoded from spec.request. This is the identity the certificate carries — not the identity that asked for it."
        />
        <SubjectPanel request={request} />

        <SectionHeader title="Who asked, and who would sign" />
        <RequestorPanel request={request} />

        {/* Permanent, not a checkbox: true of every decision, and a tick that
            always appears is a tick nobody reads on the one that matters. */}
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="csr-final"
          title="This decision cannot be changed"
        >
          The API server refuses any update that rewrites an Approved or Denied
          condition. There is no un-approve — here, in kubectl, or anywhere else.
          {approving && (
            <DescriptionListDescription style={{ marginTop: '0.5rem' }}>
              And approving is not issuing: it records a condition, and a signer has
              to act before a certificate exists. Look for <strong>Issued</strong>{' '}
              afterwards, not Approved.
            </DescriptionListDescription>
          )}
        </Alert>

        <ConsequenceChecklist
          consequences={consequences}
          acknowledged={acknowledged}
          onChange={setAcknowledged}
          idPrefix="csr"
          title={
            consequences.length === 1
              ? 'One thing about this request needs acknowledging'
              : `${consequences.length} things about this request need acknowledging`
          }
        />
      </>
    </MutationDialog>
  );
}
