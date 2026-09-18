/**
 * Identity providers — how anybody signs in to this console (§12.8, ADR-0011).
 *
 * One card per kind, edited here rather than in the container's environment.
 * The Users page shows the accounts that exist; this is where they come from,
 * and it answers the question that page raises and cannot: which directory, and
 * which group made this person an administrator.
 *
 * ## Three things this screen refuses to blur
 *
 * **Enabled is not the same as usable.** A provider missing a required value is
 * switched on and still gets no button on the login page — a button that leads
 * to an error reads as a broken console rather than an unconfigured one. So the
 * card says `Incomplete` and names the missing fields, instead of saying
 * `Enabled` while nothing appears where people sign in.
 *
 * **Stored here is not the same as configured at all.** A kind with no row
 * reads this deployment's `LDAP_*` / `OIDC_*` variables, and the card says so.
 * Editing one writes a row that takes over; deleting that row gives the
 * environment back. The Delete dialog says which of those two will happen,
 * because "delete" that re-enables a provider from a Compose file is a surprise
 * an operator should get before the click, not after.
 *
 * **A stored secret is never shown.** The form leaves a secret field empty with
 * a note saying one is stored: empty means keep, and clearing it is a separate,
 * explicit act. A field pre-filled with a bind password would be a bind password
 * in a browser cache and in the next screenshot.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  Checkbox,
  Form,
  FormGroup,
  TextArea,
  TextInput,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
} from '@patternfly/react-core';

import { identityProviders as providersApi } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useNotify } from '../contexts/NotificationContext';
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  NullableCell,
  PageHeader,
  SectionHeader,
  StatusBadge,
} from '../components/ui';

/** The badge a row's two booleans add up to. Three states, never two. */
function stateOf(row) {
  if (!row.enabled) return { status: 'disabled', label: 'Disabled' };
  if (!row.usable) return { status: 'warning', label: 'Incomplete' };
  return { status: 'active', label: 'Enabled' };
}

function FieldControl({ field, value, stored, cleared, onClear, onChange }) {
  const id = `provider-${field.name}`;
  const common = { id, value: value ?? '', onChange: (_e, next) => onChange(next) };

  if (field.type === 'bool') {
    return (
      <Checkbox
        id={id}
        label={field.label}
        isChecked={Boolean(value)}
        onChange={(_e, next) => onChange(next)}
        description={field.help || undefined}
      />
    );
  }

  const control =
    field.type === 'text' ? (
      <TextArea {...common} rows={6} aria-label={field.label} autoResize={false} />
    ) : (
      <TextInput
        {...common}
        type={field.secret ? 'password' : field.type === 'int' || field.type === 'float' ? 'number' : 'text'}
        placeholder={field.placeholder || undefined}
        autoComplete={field.secret ? 'new-password' : 'off'}
      />
    );

  return (
    <FormGroup label={field.label} fieldId={id} isRequired={field.required}>
      {control}
      {field.secret && stored && (
        <>
          <p className="admin-form-help">
            A value is stored and is never shown here. Leave this empty to keep it.
          </p>
          {/* The only way to empty a secret, and deliberately an explicit one:
              "leave it blank" already means keep, so removing a stored
              credential has to be something an operator says rather than
              something an empty field could mean by accident. */}
          <Checkbox
            id={`${id}-clear`}
            label="Remove the stored value"
            isChecked={Boolean(cleared)}
            onChange={(_e, next) => onClear(next)}
          />
        </>
      )}
      {field.secret && !stored && <p className="admin-form-help">Nothing is stored yet.</p>}
      {field.help && <p className="admin-form-help">{field.help}</p>}
    </FormGroup>
  );
}

function ProviderDialog({ row, onClose, onSaved }) {
  const { notify } = useNotify();
  const [values, setValues] = useState({});
  const [cleared, setCleared] = useState({});
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!row) return;
    setError(null);
    setSaving(false);
    setEnabled(Boolean(row.enabled));
    setCleared({});
    // Seeded from the effective configuration, which on a deployment with no
    // row is the environment's. Editing one field therefore does not blank the
    // rest — the same merge the backend performs, done where the operator can
    // see what they are about to save.
    setValues({ ...row.values });
  }, [row]);

  if (!row) return null;

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      // Built from the kind's own field list rather than from `values`, which
      // is seeded from the listing and therefore has no key at all for a
      // secret. Without this loop the "remove the stored value" checkbox could
      // not reach the request: there was nothing to map over.
      //
      // A secret goes in the body when something was typed, or as "" when its
      // removal was asked for. Absent means keep, which is what an untouched
      // form sends.
      const payload = {};
      for (const field of row.fields) {
        if (!field.secret) {
          if (field.name in values) payload[field.name] = values[field.name];
          continue;
        }
        const typed = String(values[field.name] ?? '');
        if (typed.length > 0) payload[field.name] = typed;
        else if (cleared[field.name]) payload[field.name] = '';
      }
      const saved = await providersApi.save(row.name, { enabled, values: payload });
      notify(
        saved.usable
          ? `${row.title} saved`
          : `${row.title} saved, and still not offered at sign-in: ${(saved.missing || []).join(', ')} is empty`,
        saved.usable ? 'success' : 'warning',
      );
      onSaved();
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen variant="medium" onClose={onClose} aria-label="Identity provider">
      <ModalHeader title={`${row.stored ? 'Edit' : 'Configure'} ${row.title}`} />
      <ModalBody>
        {error && (
          <Alert isInline variant="danger" title={error.message}>
            {error.hint || null}
          </Alert>
        )}
        {!row.stored && row.source === 'environment' && (
          <Alert isInline variant="info" title="This kind is currently read from the environment">
            Saving writes a stored configuration that takes over from this
            deployment&rsquo;s {row.settings_prefix}* variables. The fields below start
            from those values, so saving changes only what you change. Deleting the
            stored configuration later gives the variables back.
          </Alert>
        )}
        {row.caveat && <Alert isInline variant="warning" title="Also required" >{row.caveat}</Alert>}
        <Form>
          <Checkbox
            id="provider-enabled"
            label="Enabled"
            isChecked={enabled}
            onChange={(_e, next) => setEnabled(next)}
            description="A provider saved as disabled keeps its configuration and offers no sign-in button. Required fields are only enforced when it is enabled, so a configuration can be staged."
          />
          {row.fields.map((field) => (
            <FieldControl
              key={field.name}
              field={field}
              value={values[field.name]}
              stored={(row.secrets_stored || []).includes(field.name)}
              cleared={cleared[field.name]}
              onClear={(next) => setCleared((current) => ({ ...current, [field.name]: next }))}
              onChange={(next) => setValues((current) => ({ ...current, [field.name]: next }))}
            />
          ))}
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={submit} isLoading={saving} isDisabled={saving}>
          Save
        </Button>
        <Button variant="link" onClick={onClose}>Cancel</Button>
      </ModalFooter>
    </Modal>
  );
}

function ProviderCard({ row, onEdit, onDelete }) {
  const state = stateOf(row);
  return (
    <Card className="admin-tile" data-testid={`provider-${row.name}`}>
      <CardBody>
        <SectionHeader
          title={row.title}
          headingLevel="h3"
          actions={<StatusBadge status={state.status} label={state.label} />}
        />
        <p className="admin-form-help">{row.summary}</p>
        <dl className="admin-provider__facts">
          <dt>Points at</dt>
          <dd>
            <NullableCell
              value={row.endpoint}
              reason="This method needs no address."
            />
          </dd>
          <dt>Administrators</dt>
          <dd>
            <NullableCell
              value={row.admin_group}
              reason="No group is mapped, so this method grants the default console role and promotions are made on the Users page instead."
            />
          </dd>
          <dt>Configured in</dt>
          <dd>
            {row.stored ? (
              'this console'
            ) : (
              <>
                the environment (<code>{row.settings_prefix}*</code>)
              </>
            )}
          </dd>
        </dl>
        {row.enabled && !row.usable && (
          <Alert
            isInline
            isPlain
            variant="warning"
            title={`No sign-in button until ${(row.missing || []).join(', ')} is set`}
          />
        )}
        {row.secrets_unreadable && (
          <Alert
            isInline
            isPlain
            variant="danger"
            title="A stored secret cannot be decrypted"
          >
            The encryption key in use is not the one that wrote it, so this
            provider is falling back to the environment for that field. Re-enter
            it here rather than debugging the identity provider.
          </Alert>
        )}
        {row.editable && (
          <div className="admin-provider__actions">
            <Button variant="secondary" onClick={() => onEdit(row)}>
              {row.stored ? 'Edit' : 'Configure'}
            </Button>
            {row.stored && (
              <Button variant="link" isDanger onClick={() => onDelete(row)}>
                Delete
              </Button>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

export default function IdentityProviders() {
  const { enabled: authEnabled, user: currentUser } = useAuth();
  const { notify } = useNotify();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [working, setWorking] = useState(false);

  const isAdmin = authEnabled && currentUser?.role === 'admin';

  const refresh = useCallback(async () => {
    if (!isAdmin) return;
    setLoading(true);
    try {
      const result = await providersApi.list();
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

  // A kind belongs on a card once this deployment has said anything about it —
  // a stored row, or environment variables that configure it. The rest are
  // methods this build supports and nobody has set up, which is a different
  // fact and gets an "add" button rather than a card claiming to be off.
  const { configured, addable } = useMemo(() => {
    const present = rows.filter(
      (row) => row.name === 'local' || row.stored || row.enabled || row.usable,
    );
    return {
      configured: present,
      addable: rows.filter((row) => row.editable && !present.includes(row)),
    };
  }, [rows]);

  if (!authEnabled) {
    return (
      <EmptyState
        title="Identity providers are not in use"
        description="This deployment does not authenticate requests, so nothing here decides who anybody is. Enable AUTH_ENABLED to sign operators in individually."
      />
    );
  }
  if (!isAdmin) {
    return (
      <ErrorState
        title="Administrator access is required"
        detail="Your console role cannot read or change how this console authenticates."
      />
    );
  }

  const remove = async () => {
    setWorking(true);
    try {
      await providersApi.remove(deleting.name);
      notify(`Removed the stored ${deleting.title} configuration`, 'success');
      setDeleting(null);
      refresh();
    } catch (err) {
      notify(`Could not remove it: ${err.message}`, 'danger', { sticky: true });
    } finally {
      setWorking(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Identity providers"
        subtitle="How anybody signs in to this console. One provider of each kind; every account on the Users page arrived through one of them."
        actions={addable.map((row) => (
          <Button key={row.name} variant="secondary" onClick={() => setEditing(row)}>
            + {row.title}
          </Button>
        ))}
      />
      {error && (
        <ErrorState
          title="The configured sign-in methods could not be read"
          error={error}
          onRetry={refresh}
        />
      )}
      {loading && rows.length === 0 && !error && <LoadingState />}
      <div className="admin-tile-grid">
        {configured.map((row) => (
          <ProviderCard
            key={row.name}
            row={row}
            onEdit={setEditing}
            onDelete={setDeleting}
          />
        ))}
      </div>
      <ProviderDialog row={editing} onClose={() => setEditing(null)} onSaved={refresh} />
      <ConfirmDialog
        isOpen={Boolean(deleting)}
        title={`Remove the stored ${deleting?.title ?? ''} configuration`}
        description={
          deleting?.settings_prefix && deleting?.source === 'database'
            ? `This deletes what is stored in the console. This deployment's ${deleting.settings_prefix}* environment variables then apply again — on a deployment that has them set, that means this provider stays available with those values rather than being switched off. To switch it off instead, edit it and clear Enabled.`
            : 'This deletes what is stored in the console.'
        }
        confirmLabel="Remove"
        isDanger
        isConfirming={working}
        onConfirm={remove}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}
