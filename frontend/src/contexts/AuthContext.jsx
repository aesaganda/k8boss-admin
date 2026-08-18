/** Trusted console identity and the CSRF token bound to its HttpOnly session. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { auth as authApi, setAuthentication } from '../api/client';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [config, setConfig] = useState(null);
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

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
    setAuthentication({ enabled: true, csrfToken: session.csrfToken });
    return session.user;
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
      methods: config?.methods ?? ['local'],
      user,
      loading,
      error,
      login,
      logout,
      refresh,
    }),
    [config, user, loading, error, login, logout, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}