/**
 * The non-component half of §11.3's consequence handshake.
 *
 * Several endpoints (§18, §20, §21, §22, §24) return a `consequences` list —
 * `{ code, label, consequence, mitigation }` per entry — and refuse the write
 * until the caller sends every `code` back in `acknowledgeConsequences`. This
 * file holds the state rule and the confirm guard; `ConsequenceChecklist.jsx`
 * holds the checkboxes.
 *
 * Split from the component so that file exports only a component, which is what
 * `react-refresh/only-export-components` asks for and what keeps a dialog's Fast
 * Refresh working while somebody is editing it.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

/**
 * `[acknowledged, setAcknowledged, unacknowledged]` for one consequence list.
 *
 * **The reset is the part worth reading.** Every tick is cleared whenever the
 * *set of codes* changes. Consent given for "this deletes two pods" must not
 * survive into "this deletes nine": the codes are stable strings and the
 * sentences behind them are not, so a checklist that kept its ticks across a
 * re-plan would carry an acknowledgement of something nobody read.
 *
 * `extraSignature` is anything else that changes what is being consented to
 * without changing the codes — a size, a taint value. Pass it and the ticks
 * reset when it moves.
 */
export function useAcknowledgements(consequences, extraSignature = '') {
  const [acknowledged, setAcknowledged] = useState([]);

  const signature = useMemo(
    () =>
      (consequences ?? [])
        .map((entry) => entry.code)
        .sort()
        .join(','),
    [consequences],
  );

  const last = useRef(null);
  useEffect(() => {
    const now = `${extraSignature}|${signature}`;
    if (last.current === now) return;
    last.current = now;
    setAcknowledged([]);
  }, [extraSignature, signature]);

  const unacknowledged = (consequences ?? []).filter(
    (entry) => !acknowledged.includes(entry.code),
  );

  return [acknowledged, setAcknowledged, unacknowledged];
}

/**
 * `(result) => string | null` for MutationDialog's `confirmBlockedReason`.
 *
 * Checked against the **dry run's own** consequence list rather than the plan's,
 * because that is the later read: the server recomputes against the object as it
 * is now, so a pod that arrived between the plan and the preview shows up here
 * instead of being written past.
 */
export function blockedByConsequences(acknowledged) {
  return (result) => {
    const missing = (result?.consequences ?? []).filter(
      (entry) => !acknowledged.includes(entry.code),
    );
    if (!missing.length) return null;
    return (
      'The dry run reported consequences that have not been acknowledged: ' +
      `${missing.map((entry) => entry.label).join('; ')}. Go back and tick each one.`
    );
  };
}
