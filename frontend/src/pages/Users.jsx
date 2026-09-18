/** Local user administration and the identities every other sign-in method synchronizes. */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextInput,
} from '@patternfly/react-core';
import { sessions as sessionsApi, users as usersApi } from '../api/client';
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
  SectionHeader,
  StatusBadge,
} from '../components/ui';

const EMPTY_FORM = {
  username: '',
  display_name: '',
  email: '',
  role: 'user',
  password: '',
  active: true,
};

/**
 * How each `auth_source` is named in the table and in the edit dialog.
 *
 * An unrecognised value falls through to the raw string rather than to "Local":
 * a console upgraded ahead of this bundle can hold an account from a source this
 * build has never heard of, and calling that "Local" is a wrong answer delivered
 * confidently — it says the console holds a password for an account it does not.
 */
const SOURCE_LABELS = {
  local: 'Local',
  ldap: 'LDAP',
  oidc: 'OpenID Connect',
  oauth: 'OAuth 2.0',
  openshift: 'OpenShift',
  saml: 'SAML',
};

/**
 * Sources whose profile, role and password belong to the identity provider.
 *
 * Every source but `local`, which is how the backend derives it too
 * (`FEDERATED_SOURCES = AUTH_SOURCES - {"local"}`). Written as a subtraction
 * there and as an explicit set here for the same reason: the guard was once the
 * literal string "ldap", and when OIDC arrived an administrator could promote an
 * OIDC account and watch the change silently revert at that user's next sign-in.
 */
const PROVIDER_MANAGED = new Set(
  Object.keys(SOURCE_LABELS).filter((source) => source !== 'local'),
);

/**
 * §12.8. How anybody signs in to this deployment, above the accounts it produced.
 *
 * It is here rather than on a page of its own because it answers the question
 * this table raises and cannot answer: an `LDAP` account whose role is `admin`
 * says nothing about *which* directory it came from or which group made it an
 * administrator, and until now the only place those lived was the container's
 * environment.
 *
 * **Read-only, deliberately.** Providers are environment configuration, one of
 * each kind per deployment (§12.4), and the contract says out loud that several
 * of the same kind is a design change rather than a config key. An Edit button
 * here would edit a copy and report a save, so each row names the variable
 * prefix that does change it and stops there.
 *
 * The methods this deployment has **not** configured are named in one line
 * rather than given rows of their own: four "not configured" rows above the
 * table an administrator came for is noise, and dropping them entirely would
 * lose the difference between a method this console does not support and one
 * nobody has set up yet.
 */
function SignInMethods() {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    sessionsApi
      .providers()
      .then((result) => {
        if (live) setRows(result.items ?? []);
      })
      .catch((err) => {
        if (live) setError(err);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, []);

  const columns = useMemo(
    () => [
      { key: 'label', title: 'Method', sortable: true },
      {
        key: 'endpoint',
        title: 'Points at',
        // Unknown, not blank, and the explanation depends on the row: local
        // accounts have nowhere to point, while an *enabled* directory or
        // issuer with no address is a misconfiguration and must not read as a
        // normal absence.
        cell: (row) => (
          <NullableCell
            value={row.endpoint}
            reason={
              row.name === 'local'
                ? 'Local accounts are this console\u2019s own password table, so there is no address.'
                : 'This method is enabled with no address configured, which is a misconfiguration rather than a default.'
            }
          />
        ),
      },
      {
        key: 'admin_group',
        title: 'Administrators',
        cell: (row) => (
          <NullableCell
            value={row.admin_group}
            reason="No group is mapped, so this method grants the default console role and promotions are made here instead."
          />
        ),
      },
      {
        key: 'settings_prefix',
        title: 'Configured by',
        cell: (row) => <code>{row.settings_prefix}*</code>,
      },
    ],
    [],
  );

  // A failed read is reported and never rendered as "no methods": a console
  // that authenticated this request has at least one, so an empty panel here
  // would be a visibly impossible answer.
  if (error) {
    return (
      <ErrorState title="The configured sign-in methods could not be read" error={error} />
    );
  }

  const configured = rows.filter((row) => row.enabled);
  const absent = rows.filter((row) => !row.enabled);

  return (
    <>
      <SectionHeader
        title="Sign-in methods"
        description="Configured from the environment and read-only here. Every account below arrived through one of them."
      />
      <DataTable
        columns={columns}
        rows={configured}
        rowKey="name"
        loading={loading}
        ariaLabel="Sign-in methods"
        emptyTitle="No sign-in methods"
        emptyDescription="This deployment reports no configured method, which cannot be true of a console that just authenticated this request — read it again before acting on it."
      />
      {absent.length > 0 && (
        <p className="admin-form-help">
          Not configured on this deployment:{' '}
          {absent.map((row) => `${row.label} (${row.settings_prefix}*)`).join(', ')}.
        </p>
      )}
    </>
  );
}

function UserForm({ isOpen, editing, onClose, onSaved }) {
  const { notify } = useNotify();
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const providerManaged = editing ? PROVIDER_MANAGED.has(editing.auth_source) : false;

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setSaving(false);
    setForm(
      editing
        ? {
            username: editing.username,
            display_name: editing.display_name ?? '',
            email: editing.email ?? '',
            role: editing.role,
            password: '',
            active: editing.active,
          }
        : EMPTY_FORM,
    );
  }, [isOpen, editing]);

  if (!isOpen) return null;

  const set = (key) => (_event, value) => setForm((current) => ({ ...current, [key]: value }));
  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      if (editing) {
        const body = providerManaged
          ? { active: form.active }
          : {
              display_name: form.display_name || null,
              email: form.email || null,
              role: form.role,
              active: form.active,
              ...(form.password ? { password: form.password } : {}),
            };
        await usersApi.update(editing.id, body);
        notify(`Updated ${editing.username}`, 'success');
      } else {
        await usersApi.create({
          username: form.username,
          display_name: form.display_name || null,
          email: form.email || null,
          role: form.role,
          password: form.password,
        });
        notify(`Created ${form.username}`, 'success');
      }
      onSaved();
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  const valid = editing
    ? providerManaged || !form.password || form.password.length >= 12
    : form.username.trim() && form.password.length >= 12;

  return (
    <Modal isOpen variant="small" onClose={onClose} aria-label="Console user">
      <ModalHeader title={editing ? `Edit ${editing.username}` : 'Add local user'} />
      <ModalBody>
        {error && <Alert isInline variant="danger" title={error.message} />}
        {providerManaged && (
          <Alert isInline variant="info" title={`Managed by ${SOURCE_LABELS[editing.auth_source]}`}>
            Profile, role, and password refresh from the identity provider at login. This console
            can only deactivate or reactivate the synchronized account — an edit made here would be
            overwritten at that user&rsquo;s next sign-in, with nothing to explain why it did not
            stick, which is why the backend refuses it outright.
          </Alert>
        )}
        <Form>
          <FormGroup label="Username" fieldId="user-username" isRequired={!editing}>
            <TextInput
              id="user-username"
              value={form.username}
              onChange={set('username')}
              isDisabled={Boolean(editing)}
              autoComplete="off"
            />
          </FormGroup>
          {!providerManaged && (
            <>
              <FormGroup label="Display name" fieldId="user-display-name">
                <TextInput id="user-display-name" value={form.display_name} onChange={set('display_name')} />
              </FormGroup>
              <FormGroup label="Email" fieldId="user-email">
                <TextInput id="user-email" type="email" value={form.email} onChange={set('email')} />
              </FormGroup>
              <FormGroup label="Role" fieldId="user-role">
                <FormSelect id="user-role" value={form.role} onChange={set('role')}>
                  <FormSelectOption value="user" label="User" />
                  <FormSelectOption value="admin" label="Administrator" />
                </FormSelect>
              </FormGroup>
              <FormGroup
                label={editing ? 'New password' : 'Password'}
                fieldId="user-password"
                isRequired={!editing}
              >
                <TextInput
                  id="user-password"
                  type="password"
                  value={form.password}
                  onChange={set('password')}
                  autoComplete="new-password"
                />
                <p className="admin-form-help">
                  {editing ? 'Leave empty to keep the current password. ' : ''}Minimum 12 characters.
                </p>
              </FormGroup>
            </>
          )}
          {editing && (
            <Checkbox
              id="user-active"
              label="Account active"
              isChecked={form.active}
              onChange={set('active')}
            />
          )}
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={submit} isLoading={saving} isDisabled={saving || !valid}>
          {editing ? 'Save' : 'Create user'}
        </Button>
        <Button variant="link" onClick={onClose}>Cancel</Button>
      </ModalFooter>
    </Modal>
  );
}

export default function Users() {
  const { enabled, user: currentUser } = useAuth();
  const { notify } = useNotify();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null);
  const [deactivating, setDeactivating] = useState(null);
  const [working, setWorking] = useState(false);

  const refresh = useCallback(async () => {
    if (!enabled || currentUser?.role !== 'admin') return;
    setLoading(true);
    try {
      const result = await usersApi.list();
      setRows(result.items ?? []);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [enabled, currentUser?.role]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const columns = useMemo(
    () => [
      { key: 'username', title: 'Username', sortable: true },
      { key: 'display_name', title: 'Display name', sortable: true },
      { key: 'email', title: 'Email', sortable: true },
      {
        key: 'auth_source',
        title: 'Source',
        sortable: true,
        // Named, never bucketed into "Local". An account the console cannot
        // hold a password for, labelled as one it can, sends an administrator to
        // reset a password that does not exist — and an unfamiliar source is a
        // deployment fact worth showing verbatim rather than flattening.
        cell: (row) => (
          <StatusBadge status="info" label={SOURCE_LABELS[row.auth_source] ?? row.auth_source} />
        ),
      },
      {
        key: 'role',
        title: 'Role',
        sortable: true,
        cell: (row) => <StatusBadge status={row.role === 'admin' ? 'warning' : 'default'} label={row.role === 'admin' ? 'Admin' : 'User'} />,
      },
      {
        key: 'active',
        title: 'Status',
        sortable: true,
        cell: (row) => <StatusBadge status={row.active ? 'true' : 'false'} label={row.active ? 'Active' : 'Inactive'} />,
      },
      { key: 'last_login', title: 'Last login', sortable: true, cell: (row) => formatTimestamp(row.last_login) || 'Never' },
    ],
    [],
  );

  if (!enabled) {
    return <EmptyState title="User management is disabled" description="Enable AUTH_ENABLED to manage console identities." />;
  }
  if (currentUser?.role !== 'admin') {
    return <ErrorState title="Administrator access is required" detail="Your console role cannot manage users." />;
  }

  const deactivate = async () => {
    setWorking(true);
    try {
      await usersApi.deactivate(deactivating.id);
      notify(`Deactivated ${deactivating.username}`, 'success');
      setDeactivating(null);
      refresh();
    } catch (err) {
      notify(`Could not deactivate ${deactivating.username}: ${err.message}`, 'danger', { sticky: true });
    } finally {
      setWorking(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="Local accounts and identities synchronized from the configured directory."
        actions={<Button variant="primary" onClick={() => setEditing(EMPTY_FORM)}>Add local user</Button>}
      />
      {/* The LDAP-only banner that used to sit here is gone: the panel below
          says the same thing for every configured method and says it with the
          deployment's actual directory and group, rather than naming the
          variable the group is configured in. */}
      <SignInMethods />
      <SectionHeader
        title="Console accounts"
        description="Local users, plus every identity from a method above that has signed in at least once — directory and single sign-on accounts appear after their first successful login, never before."
      />
      {error && <ErrorState title="Users could not be loaded" error={error} onRetry={refresh} />}
      <DataTable
        columns={columns}
        rows={rows}
        rowKey="id"
        loading={loading}
        ariaLabel="Console users"
        manageableColumns
        emptyTitle="No managed users"
        emptyDescription="Create a local user or sign in through LDAP to synchronize a directory identity."
        actions={(row) => [
          { title: 'Edit', onClick: () => setEditing(row) },
          {
            title: row.active ? 'Deactivate' : 'Already inactive',
            onClick: () => setDeactivating(row),
            isDanger: true,
            isDisabled: !row.active || row.id === currentUser.id,
          },
        ]}
      />
      <UserForm
        isOpen={Boolean(editing)}
        editing={editing?.id ? editing : null}
        onClose={() => setEditing(null)}
        onSaved={refresh}
      />
      <ConfirmDialog
        isOpen={Boolean(deactivating)}
        title={`Deactivate ${deactivating?.username ?? ''}`}
        description="This revokes every active session for the account. Audit history is retained."
        confirmLabel="Deactivate"
        isDanger
        isConfirming={working}
        onConfirm={deactivate}
        onCancel={() => setDeactivating(null)}
      />
    </>
  );
}