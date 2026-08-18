/** Sign in with a local account or the configured LDAP directory. */
import { useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  TextInput,
} from '@patternfly/react-core';
import LockIcon from '@patternfly/react-icons/dist/esm/icons/lock-icon';
import { useAuth } from '../contexts/AuthContext';

export default function Login() {
  const { ldapEnabled, login } = useAuth();
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

  return (
    <main className="admin-login">
      <section className="admin-login__panel" aria-labelledby="login-title">
        <div className="admin-login__mark" aria-hidden="true">
          <LockIcon />
        </div>
        <p className="admin-login__brand">k8boss-admin</p>
        <h1 id="login-title">Sign in</h1>
        <p className="admin-login__subtitle">Kubernetes administration console</p>

        {error && (
          <Alert isInline variant="danger" title="Sign-in failed" className="admin-login__alert">
            {error.message}
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
      </section>
    </main>
  );
}