/** Trusted console identity and the CSRF token bound to its HttpOnly session. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { auth as authApi, setAuthentication } from '../api/client';

const AuthContext = createContext(null);

/**
 * Read and clear a failed single sign-on from the URL.
 *
 * The OIDC callback is a browser navigation, so it cannot answer with a §1.3
 * error envelope — the SPA is not listening, the address bar is. It redirects
 * back carrying `auth_error` (a §1.3 code, so the app has one vocabulary rather
 * than two) and `auth_reason` (a slug the login page words).
 *
 * Stripped from the URL immediately. Left there it survives a reload and a
 * bookmark, so an operator who signs in successfully and refreshes would see the
 * previous failure reported again over a working session.
 */
function takeSsoFailure() {
  if (typeof window === 'undefined') return null;
  const params = new URLSearchParams(window.location.search);
  const code = params.get('auth_error');
  if (!code) return null;
  const reason = params.get('auth_reason');
  params.delete('auth_error');
  params.delete('auth_reason');
  const query = params.toString();
  window.history.replaceState(
    {},
    '',
    `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`,
  );
  return { code, reason };
}

export function AuthProvider({ children }) {
  const [config, setConfig] = useState(null);
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Read once, on the first render, before anything can navigate away from the
  // URL that carries it.
  const [ssoFailure, setSsoFailure] = useState(takeSsoFailure);

  const clearSession = useCallback(() => {
    setUser(null);
    setAuthentication({ enabled: true, csrfToken: null });
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextConfig = await authApi.config();
      setConfig(nextConfig);
      setAuthentication({ enabled: nextConfig.enabled, csrfToken: null });
      if (!nextConfig.enabled) {
        setUser(null);
        return null;
      }
      try {
        const session = await authApi.me();
        setUser(session.user);
        setAuthentication({ enabled: true, csrfToken: session.csrfToken });
        return session.user;
      } catch (err) {
        if (err.code === 'authentication_required') {
          clearSession();
          return null;
        }
        throw err;
      }
    } catch (err) {
      setError(err);
      return null;
    } finally {
      setLoading(false);
    }
  }, [clearSession]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const handleRequired = () => clearSession();
    window.addEventListener('k8boss-authentication-required', handleRequired);
    return () => window.removeEventListener('k8boss-authentication-required', handleRequired);
  }, [clearSession]);

  const login = useCallback(async (credentials) => {
    const session = await authApi.login(credentials);
    setUser(session.user);
    setError(null);
    setSsoFailure(null);
    setAuthentication({ enabled: true, csrfToken: session.csrfToken });
    return session.user;
  }, []);

  /**
   * Leave the SPA for the identity provider.
   *
   * A full-page navigation, deliberately: the handshake needs a real browsing
   * context (the IdP renders a login form, often a second factor, sometimes a
   * consent screen), and the handshake cookie only comes back on a top-level
   * navigation. `assign` rather than `replace` so the browser's Back button
   * still returns to the console.
   *
   * The current path rides along so an operator who was deep-linked to a page
   * and got bounced to sign in lands back where they were going.
   */
  const startSso = useCallback(() => {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(authApi.ssoStartUrl(next));
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const value = useMemo(
    () => ({
      enabled: Boolean(config?.enabled),
      ldapEnabled: Boolean(config?.ldapEnabled),
      ssoEnabled: Boolean(config?.oidcEnabled),
      sso: config?.oidc ?? null,
      methods: config?.methods ?? ['local'],
      user,
      loading,
      error,
      ssoFailure,
      login,
      logout,
      startSso,
      refresh,
    }),
    [config, user, loading, error, ssoFailure, login, logout, startSso, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}