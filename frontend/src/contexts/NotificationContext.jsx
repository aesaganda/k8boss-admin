/**
 * Toast notifications.
 *
 *   const { notify } = useNotify();
 *   notify('Scaled checkout to 5 replicas', 'success');
 *
 * Variants are PatternFly Alert variants: success | danger | warning | info.
 *
 * Deliberately narrow. Toasts are for the outcome of an action the user just
 * took — they are transient by definition, so nothing that the operator needs
 * to still be able to read a minute later may live here. In particular a
 * `partial: true` response is NEVER a toast (contract §11.1): "we could not
 * read secrets in prod" has to survive on the page, next to the table it
 * applies to, which is what PartialBanner is for. A toast for that was the
 * original bug — the table looked complete and the explanation had expired.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, AlertActionCloseButton, AlertGroup } from '@patternfly/react-core';

const NotificationContext = createContext(null);

const AUTO_DISMISS_MS = 6000;
const MAX_VISIBLE = 4;
const VARIANTS = new Set(['success', 'danger', 'warning', 'info', 'custom']);

let nextId = 1;

export function NotificationProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const timers = useRef(new Map());

  const dismiss = useCallback((id) => {
    setToasts((list) => list.filter((t) => t.id !== id));
    const handle = timers.current.get(id);
    if (handle) {
      clearTimeout(handle);
      timers.current.delete(id);
    }
  }, []);

  const notify = useCallback(
    (message, variant = 'info', options = {}) => {
      const id = nextId++;
      const toast = {
        id,
        message: String(message ?? ''),
        variant: VARIANTS.has(variant) ? variant : 'info',
        detail: options.detail ?? null,
      };
      // Cap the stack. A burst (a drain evicting twenty pods) otherwise covered
      // the page it was reporting on.
      setToasts((list) => [...list.slice(-(MAX_VISIBLE - 1)), toast]);
      // Errors stay until dismissed: an auto-hiding failure message is a
      // failure the operator can miss entirely by looking away.
      const sticky = options.sticky ?? toast.variant === 'danger';
      if (!sticky) {
        timers.current.set(id, setTimeout(() => dismiss(id), options.duration ?? AUTO_DISMISS_MS));
      }
      return id;
    },
    [dismiss],
  );

  // Timers outlive the component if the provider unmounts mid-flight (a hot
  // reload, a route-level remount), and firing into an unmounted tree warns.
  // The map is captured in the effect body rather than read as `timers.current`
  // inside the cleanup: by the time cleanup runs, the ref may already point
  // somewhere else, and the pending timeouts would never be cleared.
  useEffect(() => {
    const map = timers.current;
    return () => {
      for (const handle of map.values()) clearTimeout(handle);
      map.clear();
    };
  }, []);

  const value = useMemo(() => ({ notify, dismiss, toasts }), [notify, dismiss, toasts]);

  return (
    <NotificationContext.Provider value={value}>
      {children}
      <AlertGroup isToast isLiveRegion aria-label="Notifications">
        {toasts.map((toast) => (
          <Alert
            key={toast.id}
            variant={toast.variant}
            title={toast.message}
            timeout={false}
            actionClose={
              <AlertActionCloseButton
                title={toast.message}
                onClose={() => dismiss(toast.id)}
              />
            }
          >
            {toast.detail}
          </Alert>
        ))}
      </AlertGroup>
    </NotificationContext.Provider>
  );
}

export function useNotify() {
  const ctx = useContext(NotificationContext);
  if (!ctx) throw new Error('useNotify must be used inside a NotificationProvider');
  return ctx;
}
