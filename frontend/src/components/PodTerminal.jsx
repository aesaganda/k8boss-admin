/**
 * PodTerminal — `WS /api/ws/pods/{ns}/{name}/exec` (§7).
 *
 * A shell in a pod, over xterm.js. §7 gates it behind `ADMIN_ALLOW_MUTATIONS`
 * **and** a preflight on `create pods/exec`, and the backend's own comment says
 * why the first of those is not an odd place to put a shell: *exec is gated
 * with the writes because a shell can do everything a write can*. So this
 * component checks the same gate before it opens a socket, and — rule 11.4 —
 * renders the terminal area with a banner explaining the refusal rather than
 * hiding the tab and leaving an operator to wonder whether the feature exists.
 *
 * The refusals are kept apart because they have different fixes:
 *
 *   mutations disabled  — a deployment setting. `ADMIN_ALLOW_MUTATIONS=false`.
 *                         Nobody's RBAC is wrong; this console will not do it.
 *   `rbac_denied`       — this ServiceAccount lacks `create pods/exec`. The
 *                         backend computes the exact grant; it is displayed.
 *   anything else       — named by its `reason` token, verbatim.
 *
 * Collapsing them into "exec unavailable" sends an operator to argue with a
 * cluster admin about a permission they already hold, or to file an RBAC ticket
 * for an environment variable.
 *
 * **Resize frames are sent, and sent on a real observer.** A pty whose size the
 * far end never learns renders `top`, `vim` and any curses program into
 * nonsense wrapped at 80 columns. `FitAddon` computes the geometry from the
 * container's real box, a `ResizeObserver` re-runs it when the panel changes,
 * and each new geometry goes out as `{"type":"resize","cols","rows"}`.
 *
 * **The end frame carries an exit code, and `null` is not zero.** `app/api/
 * exec_ws.py` is explicit that a `null` code means *we do not know what the
 * command returned* — the session ended without the API server telling us.
 * Printing `exit 0` there would be a fabricated success for a command that may
 * have failed.
 *
 * **The container is chosen, never defaulted.** §7 refuses to pick one for a
 * multi-container pod and answers `422` listing them, and the reasoning it
 * gives about logs applies harder here: a shell in the wrong container of a
 * payments pod looks exactly like a shell in the right one, and this one can
 * also change things. So the picker starts empty and Open shell is disabled
 * until the operator says which — the same refusal, made before the round trip
 * rather than after it, and so before an audit row is written for a session
 * that could not happen. A single-container pod is unambiguous and is selected;
 * a caller that already knows (§7.4's debug panel) passes `container` and gets
 * no picker at all.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  Alert,
  Button,
  FormSelect,
  FormSelectOption,
  Split,
  SplitItem,
  TextInput,
} from '@patternfly/react-core';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';

import { pods as podsApi } from '../api/client';
import { StatusBadge } from './ui';
import { useHealth } from '../contexts/HealthContext';
import { useTheme } from '../contexts/ThemeContext';

const DEFAULT_COMMAND = '/bin/sh';

// Matching PatternFly's own surfaces rather than xterm's black default, so a
// terminal in light mode is not a hole in the page.
const THEMES = {
  light: {
    background: '#ffffff',
    foreground: '#151515',
    cursor: '#151515',
    selectionBackground: 'rgba(0, 102, 204, 0.25)',
  },
  dark: {
    background: '#1b1d21',
    foreground: '#e0e0e0',
    cursor: '#e0e0e0',
    selectionBackground: 'rgba(115, 188, 247, 0.3)',
  },
};

export function PodTerminal({
  namespace,
  name,
  /**
   * Fixed container. When given there is no picker: the caller already knows
   * which container this terminal is for — §7.4's debug panel opens a shell in
   * the container it just attached, and offering a dropdown there would let an
   * operator retarget a terminal labelled "Terminal in debugger-x4k2p".
   */
  container = null,
  /**
   * `containers` from the §6 PodRow, for the picker. Entries may be plain
   * strings or `{ name, kind }`; `kind: "ephemeral"` is a §7.4 debug container
   * and is labelled as one, because "which of these five is the debugger" is a
   * question an operator should not have to answer from the name alone.
   * Absent means we do not know how many the pod has.
   */
  containers,
  /** Default shell. §7 takes `command` as a repeatable query parameter. */
  command = DEFAULT_COMMAND,
  height = 420,
  className,
}) {
  const { mutationsEnabled, readOnly, reason: healthReason } = useHealth();
  const { theme } = useTheme();

  const entries = useMemo(
    () =>
      Array.isArray(containers)
        ? containers
            .map((entry) => (typeof entry === 'string' ? { name: entry } : entry))
            .filter((entry) => entry?.name)
        : null,
    [containers],
  );

  const [chosen, setChosen] = useState(
    // A single-container pod is unambiguous, so it is selected. Anything else
    // waits for the operator: §7 refuses to default a container, and the reason
    // it gives applies just as hard to a shell as to a log — a root shell in
    // the wrong container of a payments pod looks exactly like a root shell in
    // the right one.
    () => (entries && entries.length === 1 ? entries[0].name : null),
  );

  // The prop wins when the caller fixed one. `null` from both is only safe when
  // the pod has exactly one container, which is the case the API server itself
  // defaults — see `resolve_container` in app/api/logs.py.
  const activeContainer = container ?? chosen;
  const mustChoose = !container && entries != null && entries.length > 1 && !chosen;

  const [commandText, setCommandText] = useState(command);
  // Not connected until the operator asks. A terminal that opens a session the
  // moment a tab is rendered writes an audit row (§7 audits on open) for
  // somebody who was only browsing.
  const [session, setSession] = useState(0);
  const [status, setStatus] = useState('idle');
  const [errorFrame, setErrorFrame] = useState(null);
  const [exit, setExit] = useState(null);

  const hostRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const socketRef = useRef(null);
  const sawTerminalRef = useRef(false);
  // Last geometry actually sent, so a ResizeObserver that fires on every
  // animation frame of a drawer opening does not send forty identical frames.
  const lastSizeRef = useRef({ cols: 0, rows: 0 });

  const started = session > 0;

  const sendResize = useCallback(() => {
    const term = termRef.current;
    const socket = socketRef.current;
    if (!term || !fitRef.current) return;
    try {
      fitRef.current.fit();
    } catch {
      // fit() throws when the host element has no layout yet — a hidden tab, a
      // drawer mid-animation. The next observation will have a real box.
      return;
    }
    const { cols, rows } = term;
    if (!cols || !rows) return;
    if (cols === lastSizeRef.current.cols && rows === lastSizeRef.current.rows) return;
    lastSizeRef.current = { cols, rows };
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'resize', cols, rows }));
    }
  }, []);

  useEffect(() => {
    if (!started || !mutationsEnabled) return undefined;
    const host = hostRef.current;
    if (!host) return undefined;

    const term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily:
        'var(--admin-mono, ui-monospace, SFMono-Regular, Menlo, monospace), ui-monospace, monospace',
      fontSize: 13,
      theme: THEMES[theme] ?? THEMES.light,
      // The pty is the authority on what has been echoed. Local echo would
      // double every keystroke the far end also echoes and hide a password
      // prompt that deliberately does not.
      scrollback: 5000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;
    lastSizeRef.current = { cols: 0, rows: 0 };

    setStatus('connecting');
    setErrorFrame(null);
    setExit(null);
    sawTerminalRef.current = false;

    const parsedCommand = commandText.trim() ? commandText.trim().split(/\s+/) : [DEFAULT_COMMAND];
    const socket = new WebSocket(
      podsApi.execUrl(namespace, name, { container: activeContainer, command: parsedCommand }),
    );
    socketRef.current = socket;

    socket.onopen = () => {
      setStatus('open');
      // The first geometry has to reach the far end before anything is drawn,
      // or the shell's prompt is laid out for xterm's 80x24 default and every
      // subsequent line wraps in the wrong place.
      sendResize();
      term.focus();
    };

    socket.onmessage = (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        term.write(`\r\n[unparseable frame from the console backend]\r\n`);
        return;
      }
      if (frame.type === 'stdout' || frame.type === 'stderr') {
        term.write(frame.data ?? '');
        return;
      }
      if (frame.type === 'error') {
        sawTerminalRef.current = true;
        setErrorFrame({
          reason: frame.reason || 'unknown',
          message: frame.message || 'The exec session failed.',
          detail: frame.detail ?? null,
          hint: frame.hint ?? null,
        });
        setStatus('error');
        return;
      }
      if (frame.type === 'end') {
        sawTerminalRef.current = true;
        setExit({ code: frame.code ?? null, detail: frame.detail ?? null });
        setStatus('ended');
        term.write(
          `\r\n\x1b[2m— session ended (${
            // §7 / exec_ws.py: a null code is "we do not know", never zero.
            frame.code == null ? 'exit code unknown' : `exit ${frame.code}`
          })\x1b[0m\r\n`,
        );
      }
    };

    socket.onclose = () => {
      if (!sawTerminalRef.current) {
        setStatus('lost');
        term.write('\r\n\x1b[2m— connection lost\x1b[0m\r\n');
      }
      socketRef.current = null;
    };

    const dataSub = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'stdin', data }));
      }
    });

    const observer = new ResizeObserver(() => sendResize());
    observer.observe(host);

    return () => {
      observer.disconnect();
      dataSub.dispose();
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close();
      }
      socketRef.current = null;
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
    // `commandText` and `theme` are deliberately not dependencies. Both would
    // tear down and rebuild the terminal — which means closing the socket — and
    // neither is a reason to end somebody's shell. `commandText` is read when a
    // session starts (and `session` is what starts one); `theme` is applied to
    // the live instance by the effect below. Toggling dark mode mid-command was
    // exactly this bug: the shell vanished and the operator lost their working
    // directory, their history and whatever they had half-typed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [started, session, mutationsEnabled, namespace, name, activeContainer, sendResize]);

  // Re-theme in place. `term.options.theme` is a live setter in xterm 5 and
  // repaints the existing buffer.
  useEffect(() => {
    if (termRef.current) termRef.current.options.theme = THEMES[theme] ?? THEMES.light;
  }, [theme]);

  const pill = {
    idle: { status: 'unknown', label: 'Not connected' },
    connecting: { status: 'progressing', label: 'Connecting' },
    open: { status: 'running', label: 'Connected' },
    ended: { status: 'unknown', label: 'Session ended' },
    error: { status: 'failed', label: 'Refused' },
    lost: { status: 'unreachable', label: 'Connection lost' },
  }[status] ?? { status: 'unknown', label: status };

  // §1.6 / §11.5: the gate is checked here so no socket is opened and no audit
  // row is written for a session that cannot happen. Disabled with the reason,
  // never hidden.
  if (!mutationsEnabled) {
    return (
      <div className={className} data-testid="pod-terminal" data-status="disabled">
        <Alert
          isInline
          variant={readOnly ? 'info' : 'warning'}
          title={readOnly ? 'Exec is disabled on this console' : 'Exec availability is unknown'}
          data-testid="pod-terminal-gate"
        >
          <p>{healthReason}</p>
          <p>
            {readOnly
              ? 'A shell can do everything a write can, so it is gated with the writes rather than treated ' +
                'as a read. Logs remain available — they are a read, and reads are not affected.'
              : 'The console has not confirmed that this deployment permits writes, so a shell is not ' +
                'offered yet. This resolves on its own once health is readable.'}
          </p>
        </Alert>
      </div>
    );
  }

  return (
    <div className={className} data-testid="pod-terminal" data-status={status}>
      <Split hasGutter style={{ alignItems: 'center', marginBlockEnd: 'var(--admin-gap-sm, 0.5rem)' }}>
        <SplitItem>
          <StatusBadge status={pill.status} label={pill.label} />
        </SplitItem>
        {!container && entries && entries.length > 1 && (
          <SplitItem>
            <FormSelect
              value={chosen ?? ''}
              onChange={(_event, next) => setChosen(next || null)}
              aria-label="Container"
              data-testid="pod-terminal-container"
              style={{ minWidth: '14rem' }}
            >
              {/* Not a default — the state the terminal starts in and refuses
                  to leave on its own. §7 will not pick a container and neither
                  will this. */}
              <FormSelectOption value="" label="Choose a container…" isDisabled />
              {entries.map((entry) => (
                <FormSelectOption
                  key={entry.name}
                  value={entry.name}
                  label={entry.kind === 'ephemeral' ? `${entry.name} (debug)` : entry.name}
                />
              ))}
            </FormSelect>
          </SplitItem>
        )}
        <SplitItem>
          {/* Editable at every phase, and applied on the next session rather
              than this one. Disabling it while connected left the operator no
              way to change the command except by ending the session first — and
              a "Restart session" that silently reused the old command was worse
              still. */}
          <TextInput
            value={commandText}
            onChange={(_event, next) => setCommandText(next)}
            aria-label="Command to run when a session starts"
            placeholder={DEFAULT_COMMAND}
            data-testid="pod-terminal-command"
            style={{ minWidth: '18rem' }}
          />
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <Button
            variant={status === 'open' ? 'secondary' : 'primary'}
            onClick={() => setSession((n) => n + 1)}
            isDisabled={mustChoose}
            data-testid="pod-terminal-connect"
          >
            {status === 'open' || status === 'connecting' ? 'Restart session' : started ? 'Reconnect' : 'Open shell'}
          </Button>
        </SplitItem>
      </Split>

      {mustChoose && (
        // The same refusal §7 makes on the wire, made here so it costs no round
        // trip and no audit row. `app/api/logs.py`: *it is not defaulted:
        // reading the wrong container's logs looks exactly like reading the
        // right one* — and typing into the wrong container is worse, because it
        // also changes something.
        <Alert isInline variant="info" title="Choose a container" data-testid="pod-terminal-choose">
          This pod has {entries.length} containers. The console will not pick one for you: a shell in the
          wrong container of this pod looks exactly like a shell in the right one, and this one can change
          things.
        </Alert>
      )}

      {!started && !mustChoose && (
        <Alert isInline variant="info" title="No session is open">
          Opening a shell is audited on open and on close (§7), and runs as the console’s ServiceAccount —
          not as you. Nothing is sent to the cluster until you press Open shell.
        </Alert>
      )}

      {errorFrame && (
        <Alert
          isInline
          variant="danger"
          title={
            errorFrame.reason === 'rbac_denied'
              ? 'This ServiceAccount may not exec into pods'
              : `Exec refused — ${errorFrame.reason}`
          }
          data-testid="pod-terminal-error"
        >
          <p>{errorFrame.message}</p>
          {errorFrame.detail && (
            <pre style={{ whiteSpace: 'pre-wrap', margin: '0.5rem 0 0', fontSize: '0.8125rem' }}>
              {errorFrame.detail}
            </pre>
          )}
          {/* The preflight's computed grant. It is the only part of this that
              tells the operator what to change. */}
          {errorFrame.hint && <p style={{ fontWeight: 600, marginBlockStart: '0.5rem' }}>{errorFrame.hint}</p>}
        </Alert>
      )}

      {status === 'ended' && exit && (
        <Alert
          isInline
          // A non-zero exit and an unknown exit are both "not a clean finish".
          // Only a reported 0 is green.
          variant={exit.code === 0 ? 'success' : 'warning'}
          title={
            exit.code == null
              ? 'Session ended — exit code unknown'
              : `Session ended — exit ${exit.code}`
          }
          data-testid="pod-terminal-end"
        >
          {exit.code == null
            ? 'The session closed without the API server reporting what the command returned. This is not ' +
              'the same as a successful exit, and it is not reported as one.'
            : exit.detail || 'The command exited.'}
        </Alert>
      )}

      {status === 'lost' && (
        <Alert isInline variant="warning" title="Connection lost" data-testid="pod-terminal-lost">
          The socket closed without the end frame §7 guarantees, so this did not come from the backend.
          Whatever the command was doing in the pod may still be running — closing a terminal does not stop
          a process.
        </Alert>
      )}

      <div
        ref={hostRef}
        data-testid="pod-terminal-host"
        style={{
          height,
          // A visible box even before a session exists, so the panel does not
          // jump in size the moment a shell opens.
          border: '1px solid var(--admin-border, #d2d2d2)',
          borderRadius: 'var(--pf-t--global--border--radius--small, 4px)',
          background: (THEMES[theme] ?? THEMES.light).background,
          padding: '0.25rem',
          marginBlockStart: 'var(--admin-gap-sm, 0.5rem)',
          overflow: 'hidden',
        }}
      />
    </div>
  );
}

export default PodTerminal;
