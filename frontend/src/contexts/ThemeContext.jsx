/**
 * Light / dark theme.
 *
 * PatternFly 6 reads dark mode from the `pf-v6-theme-dark` class on <html>, so
 * that class is the single switch — the app defines no parallel colour system
 * of its own. `data-theme` is mirrored alongside it for the handful of
 * app-level rules in index.css that PatternFly has no token for.
 *
 * The choice is persisted. It is applied in a layout effect rather than a
 * passive one because a passive effect runs after paint: the first frame
 * rendered light, then flipped, and the flash was very visible on a dark
 * monitor in a dark room, which is the whole population that sets this.
 */
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useState } from 'react';

const STORAGE_KEY = 'k8boss-admin.theme';
const ThemeContext = createContext(null);

function readInitialTheme() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
  } catch {
    /* localStorage is unavailable in private mode; fall through to the OS hint */
  }
  try {
    if (window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark';
  } catch {
    /* matchMedia is absent in some embedded webviews */
  }
  return 'light';
}

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(readInitialTheme);

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('pf-v6-theme-dark', theme === 'dark');
    root.setAttribute('data-theme', theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* the theme still applies for this session */
    }
  }, [theme]);

  const setTheme = useCallback((next) => {
    setThemeState((current) => {
      const resolved = typeof next === 'function' ? next(current) : next;
      return resolved === 'dark' ? 'dark' : 'light';
    });
  }, []);

  const toggleTheme = useCallback(() => {
    setThemeState((t) => (t === 'dark' ? 'light' : 'dark'));
  }, []);

  // Follow the OS only while the user has never chosen for themselves. Once
  // they have, an OS change at sunset must not silently override them.
  useEffect(() => {
    let stored = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      stored = null;
    }
    if (stored) return undefined;
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!media) return undefined;
    const onChange = (event) => setThemeState(event.matches ? 'dark' : 'light');
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  const value = useMemo(() => ({ theme, setTheme, toggleTheme }), [theme, setTheme, toggleTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used inside a ThemeProvider');
  return ctx;
}
