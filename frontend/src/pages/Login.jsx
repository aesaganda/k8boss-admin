/** Sign in with a local account, the configured LDAP directory, or single sign-on. */
import { useState } from 'react';
import {
  Alert,
  Button,
  Divider,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  TextInput,
} from '@patternfly/react-core';
import LockIcon from '@patternfly/react-icons/dist/esm/icons/lock-icon';
import { useAuth } from '../contexts/AuthContext';

/**
 * What a failed single sign-on means, in the operator's terms.
 *
 * The backend cannot answer a browser navigation with a §1.3 envelope, so it
 * redirects back with a code and a reason slug. The wording lives here rather
 * than in the redirect for two reasons: a sentence in a query string is
 * untranslatable and ends up in browser history, and the codes have to stay the
 * same ones the rest of the app branches on.
 *
 * An unknown slug is rendered verbatim rather than replaced with something
 * generic. "Sign-in failed" tells an operator nothing they did not already know,
 * and a reason we did not anticipate is exactly the one worth showing.
 */
const SSO_REASONS = {
  provider_unreachable:
    'The console could not reach the identity provider. This is the provider or the '
    + 'network path to it, not your account — nothing about your credentials was checked.',
  provider_refused:
    'The identity provider refused the sign-in before this console was involved. That '
    + 'usually means the account is not assigned to this application, or a consent '
    + 'screen was declined.',
  handshake_missing_or_expired:
    'The sign-in took too long, or it was started in a different browser session. '
    + 'Start it again.',
  state_mismatch:
    'The response did not match a sign-in this console started. If you did not just '
    + 'click sign in, someone else may have sent you this link.',
  assertion_rejected:
    'The identity provider\u2019s response could not be validated. If it keeps happening, '
    + 'the console\u2019s clock or its registered client id may not match the provider\u2019s.',
  account_refused:
    'You were signed in to the identity provider successfully, but this console will not '
    + 'issue a session for that account. The most common causes are a username that '
    + 'already belongs to a local account, and a directory group this console does not '
    + 'permit.',
};

export default function Login() {
  const { ldapEnabled, ssoEnabled, sso, ssoFailure, login, startSso } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [source, setSource] = useState('auto');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (event) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login({ username, password, source });
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  };

  // Branching on the stable code, never on the message — the same rule the rest
  // of the app follows. A throttled sign-in has to read differently from a
  // rejected one: telling someone to check their password while the console is
  // refusing to look at it is how a lockout turns into a support ticket.
  const throttled = error?.code === 'too_many_attempts';
  const retryAfter = error?.context?.retryAfterSeconds;

  return (
    <main className="admin-login">
      <section className="admin-login__panel" aria-labelledby="login-title">
        <div className="admin-login__mark" aria-hidden="true">
          <LockIcon />
        </div>
        <p className="admin-login__brand">k8boss-admin</p>
        <h1 id="login-title">Sign in</h1>
        <p className="admin-login__subtitle">Kubernetes administration console</p>

        {ssoFailure && (
          <Alert
            isInline
            variant="danger"
            title="Single sign-on did not complete"
            className="admin-login__alert"
            data-testid="login-sso-error"
          >
            {SSO_REASONS[ssoFailure.reason] ?? ssoFailure.reason ?? ssoFailure.code}
          </Alert>
        )}

        {error && (
          <Alert
            isInline
            variant={throttled ? 'warning' : 'danger'}
            title={throttled ? 'Too many attempts' : 'Sign-in failed'}
            className="admin-login__alert"
            data-testid="login-error"
          >
            {error.message}
            {throttled && retryAfter ? (
              <p>
                The console is not checking passwords for this account right now. Try again in
                about {Math.ceil(retryAfter / 60)} minute(s).
              </p>
            ) : null}
          </Alert>
        )}

        <Form onSubmit={submit} className="admin-login__form">
          <FormGroup label="Username" fieldId="login-username" isRequired>
            <TextInput
              id="login-username"
              value={username}
              onChange={(_event, value) => setUsername(value)}
              autoComplete="username"
              isRequired
              autoFocus
            />
          </FormGroup>
          <FormGroup label="Password" fieldId="login-password" isRequired>
            <TextInput
              id="login-password"
              type="password"
              value={password}
              onChange={(_event, value) => setPassword(value)}
              autoComplete="current-password"
              isRequired
            />
          </FormGroup>
          {ldapEnabled && (
            <FormGroup label="Account source" fieldId="login-source">
              <FormSelect
                id="login-source"
                value={source}
                onChange={(_event, value) => setSource(value)}
              >
                <FormSelectOption value="auto" label="Automatic" />
                <FormSelectOption value="local" label="Local account" />
                <FormSelectOption value="ldap" label="LDAP directory" />
              </FormSelect>
            </FormGroup>
          )}
          <Button
            type="submit"
            variant="primary"
            isBlock
            isLoading={submitting}
            isDisabled={submitting || !username.trim() || !password}
          >
            Sign in
          </Button>
        </Form>

        {/* Offered only when the deployment has a usable issuer AND client id.
            A button that leads to an error reads as a broken console; no button
            reads as "SSO is not set up here", which is both true and the thing
            an operator can act on. */}
        {ssoEnabled && (
          <>
            <Divider className="admin-login__divider" />
            <Button
              variant="secondary"
              isBlock
              onClick={startSso}
              data-testid="login-sso"
            >
              {sso?.label || 'Single sign-on'}
            </Button>
          </>
        )}
      </section>
    </main>
  );
}