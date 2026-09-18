/**
 * Sessions — who can act on this console right now (§12.7).
 *
 * The Users page answers "who has an account". This answers the question asked
 * during an incident, which is a different one: *who is signed in at this
 * moment, from where, and can I end it*. An account that is active and a session
 * that is live are not the same fact — deactivating an account revokes its
 * sessions, but a session belonging to an account nobody wants to deactivate is
 * exactly the thing a stolen laptop produces.
 *
 * Three properties of this page are deliberate:
 *
 * **Every row carries where it came from.** A list of sessions with no address
 * and no browser is a column of identical rows, and the safe way to use it is
 * not to. `null` renders as unknown rather than blank, because "the transport
 * reported no peer" and "nobody has been here" are different sentences (rule
 * 11.2).
 *
 * **The caller's own session is labelled.** It is the one row whose revocation
 * signs the operator out, and that is worth knowing before the click rather
 * than discovering afterwards at the login page.
 *
 * **Last used is coarse and says so.** The backend refreshes it at most once a
 * minute per session, because writing it on every request would put a database
 * write in front of every read the console serves. A page that implied
 * second-level precision would be inviting an operator to conclude a session is
 * idle when it is one request old.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button } from '@patternfly/react-core';

import { sessions as sessionsApi } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useNotify } from '../contexts/NotificationContext';
import { formatTimestamp } from '../utils/format';
import {
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorState,
  NullableCell,
  PageHeader,
  StatusBadge,
} from '../components/ui';

const SOURCE_LABELS = {
  local: 'Local',
  ldap: 'LDAP',
  oidc: 'OpenID Connect',
  oauth: 'OAuth 2.0',
  openshift: 'OpenShift',
  saml: 'SAML',
};

export default function Sessions() {
  const { enabled, user: currentUser } = useAuth();
  const { notify } = useNotify();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [revoking, setRevoking] = useState(null);
  const [working, setWorking] = useState(false);

  const isAdmin = enabled && currentUser?.role === 'admin';

  const refresh = useCallback(async () => {
    if (!isAdmin) return;
    setLoading(true);
    try {
      const result = await sessionsApi.list();
      setRows(result.items ?? []);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const columns = useMemo(
    () => [
      {
        key: 'username',
        title: 'User',
        sortable: true,
        cell: (row) => (
          <>
            {row.username}
            {row.current && (
              <>
                {' '}
                <StatusBadge status="info" label="this session" />
              </>
            )}
          </>
        ),
      },
      {
        key: 'auth_source',
        title: 'Source',
        sortable: true,
        // Named, never bucketed into "Local" — the same rule the Users table
        // follows. A source this build has not heard of is shown verbatim.
        cell: (row) => (
          <StatusBadge status="default" label={SOURCE_LABELS[row.auth_source] ?? row.auth_source} />
        ),
      },
      {
        key: 'ip_address',
        title: 'IP address',
        sortable: true,
        // Unknown, not blank. The value is the peer address the request
        // arrived from; a session created before the column existed has none.
        cell: (row) => (
          <NullableCell
            value={row.ip_address}
            reason="This session was opened before the console recorded addresses, or the transport reported no peer."
          />
        ),
      },
      {
        key: 'user_agent',
        title: 'User agent',
        sortable: true,
        // The string as sent, truncated by the table and complete on hover.
        // Deliberately not parsed into "Chrome on macOS": that mapping is a
        // guess, and a wrong guess about which browser somebody is using is a
        // wrong answer on the page an operator revokes access from.
        cell: (row) => (
          <NullableCell
            value={row.user_agent}
            format={(agent) => <span title={agent}>{agent}</span>}
            reason="This session was opened before the console recorded browsers, or the request sent no User-Agent."
          />
        ),
      },
      {
        key: 'created_at',
        title: 'Signed in',
        sortable: true,
        cell: (row) => formatTimestamp(row.created_at),
      },
      {
        key: 'last_used_at',
        title: 'Last used',
        sortable: true,
        cell: (row) => (
          <NullableCell
            value={row.last_used_at}
            format={(ts) => (
              <span title="Refreshed at most once a minute per session.">
                {formatTimestamp(ts)}
              </span>
            )}
            reason="This session has not been seen since the console started recording it."
          />
        ),
      },
      {
        key: 'expires_at',
        title: 'Expires',
        sortable: true,
        cell: (row) => formatTimestamp(row.expires_at),
      },
    ],
    [],
  );

  if (!enabled) {
    return (
      <EmptyState
        title="Session management is disabled"
        description="This deployment does not authenticate requests, so it holds no console sessions. Enable AUTH_ENABLED to sign operators in individually."
      />
    );
  }
  if (!isAdmin) {
    return (
      <ErrorState
        title="Administrator access is required"
        detail="Your console role cannot list or revoke other people's sessions."
      />
    );
  }

  const revoke = async () => {
    setWorking(true);
    const own = revoking.current;
    try {
      await sessionsApi.revoke(revoking.id);
      notify(
        own ? 'Revoked your own session' : `Revoked a session for ${revoking.username}`,
        'success',
      );
      setRevoking(null);
      // Refreshed even when it was the caller's own session: the list now 401s,
      // which is what dispatches the sign-in prompt. Pretending the row is gone
      // without asking the backend would leave the page looking signed in.
      refresh();
    } catch (err) {
      notify(`Could not revoke the session: ${err.message}`, 'danger', { sticky: true });
    } finally {
      setWorking(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Sessions"
        subtitle={
          loading
            ? 'Reading the console’s active sessions.'
            : `${rows.length} active. Expired sessions are not listed — they can no longer act on this console.`
        }
        actions={
          <Button variant="secondary" onClick={refresh} isDisabled={loading}>
            Refresh
          </Button>
        }
      />
      {error && <ErrorState title="Sessions could not be loaded" error={error} onRetry={refresh} />}
      <DataTable
        columns={columns}
        rows={rows}
        rowKey="id"
        loading={loading}
        ariaLabel="Active console sessions"
        manageableColumns
        emptyTitle="No active sessions"
        emptyDescription="Nobody is signed in to this console, including you — which means this page was read with a session that has just been revoked."
        actions={(row) => [
          {
            title: row.current ? 'Revoke (signs you out)' : 'Revoke',
            onClick: () => setRevoking(row),
            isDanger: true,
          },
        ]}
      />
      <ConfirmDialog
        isOpen={Boolean(revoking)}
        title={revoking?.current ? 'Revoke your own session' : `Revoke a session for ${revoking?.username ?? ''}`}
        description={
          revoking?.current
            ? 'This is the session you are using. Revoking it signs you out immediately and you will need to sign in again. The account itself is unchanged.'
            : `The next request from that browser will be refused and the operator will have to sign in again. The account stays active — deactivate it on the Users page if that is what is needed.`
        }
        confirmLabel="Revoke"
        isDanger
        isConfirming={working}
        onConfirm={revoke}
        onCancel={() => setRevoking(null)}
      />
    </>
  );
}
