/**
 * LogViewer — `WS /api/ws/pods/{ns}/{name}/logs` (§7).
 *
 * §7 makes one promise about this socket: *the stream always terminates with
 * exactly one `end` or `error` frame, so a viewer can tell "the pod stopped
 * logging" from "we lost the connection"*. The backend goes to real trouble to
 * keep it (`StreamTerminator` in `app/api/logs.py` exists for nothing else),
 * and a viewer that rendered both as a grey "disconnected" line would throw all
 * of it away. So this component distinguishes **three** terminal states, not
 * two:
 *
 *   `end` frame     — the stream finished. The pod stopped producing output, or
 *                     the container exited. Neutral. Everything above it is the
 *                     complete log for the window that was requested.
 *   `error` frame   — the stream failed, and the backend named the reason and
 *                     computed a hint. Red. The log above it is truncated and
 *                     we know why.
 *   socket closed   — the socket went away *without* a terminal frame. Amber,
 *   with no frame     and worded as its own fact. §7 says this cannot happen
 *                     from the backend's side, so when it does the cause is
 *                     between us and it: a proxy idle timeout, a suspended
 *                     laptop, a restarted backend. The log above it is
 *                     truncated and we do **not** know why — which is a
 *                     different thing to tell an operator than either of the
 *                     other two, and the one most likely to be mistaken for
 *                     "the application went quiet".
 *
 * Two other places where silence would lie:
 *
 * **The buffer cap announces itself.** Holding an unbounded log in a React
 * state array kills the tab on a chatty pod. The cap is real, and the number of
 * lines dropped off the top is displayed — a viewer that silently discarded the
 * beginning of the log would have an operator scrolling to the top of what they
 * believe is the whole thing.
 *
 * **Pause does not disconnect.** Paused frames are buffered and counted, so
 * "paused, 412 lines held" is visibly different from a stream that stopped
 * producing. Resuming flushes them in order rather than skipping to the live
 * edge; a pause that quietly dropped what arrived during it would make the log
 * an unreliable record exactly when somebody paused it to read something.
 *
 * **A multi-container pod is never guessed at.** §7 makes a missing container
 * name on a multi-container pod a `422 invalid` listing the containers rather
 * than a silent pick of the first, and this viewer does the same locally: no
 * socket is opened until one is chosen.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  FormSelect,
  FormSelectOption,
  Split,
  SplitItem,
  TextInput,
} from '@patternfly/react-core';
import { EmptyState, FilterBar, StatusBadge } from './ui';
import { pods as podsApi } from '../api/client';

const TAIL_OPTIONS = [100, 500, 1000, 5000, 10000];

// Beyond this the DOM, not the network, is the bottleneck. The overflow is
// reported rather than silently discarded — see the docstring.
const MAX_BUFFERED_LINES = 5000;

/** Terminal-state vocabulary. `null` means the stream is still open. */
const TERMINAL = {
  ended: {
    variant: 'info',
    title: 'Stream ended',
    body:
      'The backend closed the stream normally. Everything above this line is the complete output for the ' +
      'window that was requested.',
  },
  lost: {
    variant: 'warning',
    title: 'Connection lost',
    body:
      'The socket closed without the end frame that §7 guarantees, so this did not come from the backend. ' +
      'Something between this browser and it dropped the connection — a proxy idle timeout, a sleeping ' +
      'laptop, a restarted backend. The output above is incomplete, and unlike a normal end this viewer ' +
      'cannot say what was missed.',
  },
};

export function LogViewer({
  namespace,
  name,
  /** `containers` from the §6 PodRow. Absent means we do not know how many. */
  containers,
  defaultContainer = null,
  height = 460,
  className,
}) {
  const entries = useMemo(
    () =>
      Array.isArray(containers)
        ? containers
            .map((c) => (typeof c === 'string' ? { name: c } : c))
            .filter((c) => c?.name)
        : null,
    [containers],
  );
  const names = useMemo(() => entries?.map((c) => c.name) ?? null, [entries]);

  // The pod's own containers, excluding §7.4 debug containers.
  //
  // This — not `names` — is what decides whether the pod is ambiguous, because
  // it is what the *API server* counts: it defaults the container only when
  // `spec.containers` holds one, and ephemeral containers never enter that
  // count. Keying off `names` instead would mean that attaching a debug
  // container to a single-container pod made this viewer start refusing to pick
  // a container it had happily picked a minute earlier — a console that broke
  // its own log viewer as a side effect of opening a shell, and one that had
  // become stricter than the API it is a client of.
  //
  // Debug containers stay in `names`, so they remain selectable: `kubectl logs
  // pod -c debugger-x4k2p` is a thing an operator wants.
  const ownNames = useMemo(
    () => entries?.filter((c) => (c.kind ?? 'container') !== 'ephemeral').map((c) => c.name) ?? null,
    [entries],
  );

  const [container, setContainer] = useState(
    // A single-container pod is unambiguous, so it is selected. Anything else
    // waits for the operator.
    defaultContainer ?? (ownNames && ownNames.length === 1 ? ownNames[0] : null),
  );
  const [manualContainer, setManualContainer] = useState('');
  const [tailLines, setTailLines] = useState(500);
  const [follow, setFollow] = useState(true);
  const [paused, setPaused] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [timestamps, setTimestamps] = useState(false);
  const [nonce, setNonce] = useState(0);

  const [lines, setLines] = useState([]);
  const [dropped, setDropped] = useState(0);
  const [status, setStatus] = useState('idle');
  const [endReason, setEndReason] = useState(null);
  const [errorFrame, setErrorFrame] = useState(null);
  const [terminal, setTerminal] = useState(null);
  const [held, setHeld] = useState(0);

  const scrollRef = useRef(null);
  const socketRef = useRef(null);
  const pendingRef = useRef([]);
  const pausedRef = useRef(paused);
  // Set the moment a terminal frame arrives, and read in `onclose` — the close
  // event fires for both a clean end and a dropped connection, and this ref is
  // the only thing that tells them apart.
  const sawTerminalRef = useRef(false);
  const seqRef = useRef(0);

  useEffect(() => {
    pausedRef.current = paused;
    if (!paused && pendingRef.current.length) {
      // Flush in arrival order. Skipping to the live edge would silently lose
      // whatever arrived while the operator was reading.
      const flushing = pendingRef.current;
      pendingRef.current = [];
      setHeld(0);
      setLines((current) => capLines([...current, ...flushing], setDropped));
    }
  }, [paused]);

  const ready = container != null || (ownNames != null && ownNames.length <= 1);

  const connect = useCallback(() => {
    if (!ready) return undefined;

    setStatus('connecting');
    setTerminal(null);
    setEndReason(null);
    setErrorFrame(null);
    sawTerminalRef.current = false;

    const url = podsApi.logStreamUrl(namespace, name, { container, tailLines });
    const socket = new WebSocket(url);
    socketRef.current = socket;

    socket.onopen = () => setStatus('streaming');

    socket.onmessage = (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        // A frame we cannot parse is a defect somewhere, but the operator's
        // log is still worth showing. Surface it as a line rather than
        // dropping it, so the corruption is visible instead of looking like
        // an application that went quiet.
        frame = { type: 'log', line: `[unparseable frame] ${String(event.data).slice(0, 500)}`, ts: null };
      }

      if (frame.type === 'log') {
        const entry = { seq: (seqRef.current += 1), line: frame.line ?? '', ts: frame.ts ?? null };
        if (pausedRef.current) {
          pendingRef.current.push(entry);
          setHeld(pendingRef.current.length);
        } else {
          setLines((current) => capLines([...current, entry], setDropped));
        }
        return;
      }

      if (frame.type === 'end') {
        sawTerminalRef.current = true;
        setEndReason(frame.reason || 'stream_closed');
        setTerminal('ended');
        setStatus('ended');
        return;
      }

      if (frame.type === 'error') {
        sawTerminalRef.current = true;
        setErrorFrame({
          reason: frame.reason || 'unknown',
          message: frame.message || 'The log stream failed.',
          detail: frame.detail ?? null,
          hint: frame.hint ?? null,
        });
        setTerminal('error');
        setStatus('error');
      }
    };

    socket.onclose = () => {
      // §7's guarantee makes the absence of a terminal frame informative. It
      // means the close did not come from the backend's own code path, so the
      // truncation has a cause nobody has told us about.
      if (!sawTerminalRef.current) {
        setTerminal('lost');
        setStatus('lost');
      }
      socketRef.current = null;
    };

    // No `onerror` handler that sets state: the browser fires `error` and then
    // always `close`, and reporting both produced two different explanations
    // for one event. `close` is the one that can distinguish the cases.

    return () => {
      // Explicitly detach before closing. A close initiated here still fires
      // `onclose`, which would otherwise paint "Connection lost" over a viewer
      // the operator simply navigated away from or reconfigured.
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close();
      }
      socketRef.current = null;
    };
  }, [ready, namespace, name, container, tailLines]);

  useEffect(() => {
    // A new stream is a new log. Keeping the old lines above the new ones would
    // present two different tails of two different requests as one continuous
    // record.
    setLines([]);
    setDropped(0);
    setHeld(0);
    pendingRef.current = [];
    seqRef.current = 0;
    return connect();
    // `nonce` is the reconnect button: it is not read inside `connect`, it only
    // needs to re-run this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect, nonce]);

  // Auto-scroll. Only while following and not paused — a paused viewer that
  // kept jumping to the bottom would be unreadable, which is what pause is for.
  useEffect(() => {
    if (!follow || paused) return;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [lines, follow, paused]);

  const onScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
    // Scrolling up turns following off, the way every terminal does. Without
    // this, reading anything more than a screen back is impossible on a chatty
    // pod: the next frame yanks the viewport away.
    if (!atBottom && follow) setFollow(false);
  }, [follow]);

  const download = useCallback(() => {
    const text = lines.map((entry) => (timestamps && entry.ts ? `${entry.ts} ${entry.line}` : entry.line)).join('\n');
    const header =
      dropped > 0
        ? `# NOTE: ${dropped} earlier line(s) were dropped by this viewer's ${MAX_BUFFERED_LINES}-line buffer ` +
          'and are not in this file.\n'
        : '';
    const blob = new Blob([header + text + '\n'], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${namespace}_${name}${container ? `_${container}` : ''}.log`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    // Revoking immediately races the download in some browsers; a frame is
    // enough and the object is small.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, [lines, timestamps, dropped, namespace, name, container]);

  const statusPill = {
    idle: { status: 'unknown', label: 'Not started' },
    connecting: { status: 'progressing', label: 'Connecting' },
    streaming: { status: paused ? 'pending' : 'running', label: paused ? `Paused · ${held} held` : 'Streaming' },
    ended: { status: 'unknown', label: 'Ended' },
    error: { status: 'failed', label: 'Failed' },
    lost: { status: 'unreachable', label: 'Connection lost' },
  }[status] ?? { status: 'unknown', label: status };

  return (
    <div className={className} data-testid="log-viewer" data-status={status}>
      <FilterBar ariaLabel="Log stream controls">
        {names && names.length > 1 && (
          <FilterBar.Field label="Container" htmlFor="log-container">
            <FormSelect
              id="log-container"
              value={container ?? ''}
              onChange={(_event, next) => setContainer(next || null)}
              aria-label="Container"
              data-testid="log-container"
            >
              {/* The empty option is not a default — it is the state the
                  viewer starts in and refuses to leave on its own. */}
              <FormSelectOption value="" label="Choose a container…" isDisabled />
              {entries.map((entry) => (
                <FormSelectOption
                  key={entry.name}
                  value={entry.name}
                  // §7.4 debug containers are labelled as such: "which of these
                  // five is the debugger" is not a question an operator should
                  // have to answer from the name alone.
                  label={entry.kind === 'ephemeral' ? `${entry.name} (debug)` : entry.name}
                />
              ))}
            </FormSelect>
          </FilterBar.Field>
        )}

        {names == null && (
          <FilterBar.Field label="Container (optional)" htmlFor="log-container-manual">
            <Split hasGutter>
              <SplitItem>
                <TextInput
                  id="log-container-manual"
                  value={manualContainer}
                  onChange={(_event, next) => setManualContainer(next)}
                  aria-label="Container name"
                  placeholder="leave blank for the default"
                />
              </SplitItem>
              <SplitItem>
                <Button variant="secondary" onClick={() => setContainer(manualContainer || null)}>
                  Apply
                </Button>
              </SplitItem>
            </Split>
          </FilterBar.Field>
        )}

        <FilterBar.Field label="Tail" htmlFor="log-tail">
          <FormSelect
            id="log-tail"
            value={String(tailLines)}
            onChange={(_event, next) => setTailLines(Number(next))}
            aria-label="Tail lines"
            data-testid="log-tail"
          >
            {TAIL_OPTIONS.map((n) => (
              <FormSelectOption key={n} value={String(n)} label={`${n} lines`} />
            ))}
          </FormSelect>
        </FilterBar.Field>

        <FilterBar.Field label="View" htmlFor="log-follow">
          <Split hasGutter>
            <SplitItem>
              <Checkbox
                id="log-follow"
                label="Follow"
                isChecked={follow}
                onChange={(_event, checked) => setFollow(checked)}
                data-testid="log-follow"
              />
            </SplitItem>
            <SplitItem>
              <Checkbox
                id="log-wrap"
                label="Wrap"
                isChecked={wrap}
                onChange={(_event, checked) => setWrap(checked)}
                data-testid="log-wrap"
              />
            </SplitItem>
            <SplitItem>
              <Checkbox
                id="log-timestamps"
                label="Timestamps"
                isChecked={timestamps}
                onChange={(_event, checked) => setTimestamps(checked)}
                data-testid="log-timestamps"
              />
            </SplitItem>
          </Split>
        </FilterBar.Field>
      </FilterBar>

      <Split hasGutter style={{ alignItems: 'center', margin: 'var(--admin-gap-sm, 0.5rem) 0' }}>
        <SplitItem>
          <StatusBadge status={statusPill.status} label={statusPill.label} />
        </SplitItem>
        <SplitItem>
          <span style={{ color: 'var(--admin-muted, #6a6e73)', fontSize: '0.875rem' }}>
            {lines.length} {lines.length === 1 ? 'line' : 'lines'}
            {dropped > 0 && ` · ${dropped} dropped from the top`}
          </span>
        </SplitItem>
        <SplitItem isFilled />
        <SplitItem>
          <Button
            variant="secondary"
            onClick={() => setPaused((v) => !v)}
            isDisabled={status !== 'streaming' && status !== 'connecting'}
            data-testid="log-pause"
          >
            {paused ? `Resume${held ? ` (${held} held)` : ''}` : 'Pause'}
          </Button>
        </SplitItem>
        <SplitItem>
          <Button variant="secondary" onClick={() => setNonce((n) => n + 1)} data-testid="log-reconnect">
            {status === 'streaming' || status === 'connecting' ? 'Restart stream' : 'Reconnect'}
          </Button>
        </SplitItem>
        <SplitItem>
          <Button variant="secondary" onClick={download} isDisabled={!lines.length} data-testid="log-download">
            Download
          </Button>
        </SplitItem>
      </Split>

      {dropped > 0 && (
        <Alert isInline variant="info" title={`${dropped} earlier lines are no longer held by this viewer`}>
          The viewer keeps the most recent {MAX_BUFFERED_LINES} lines. Older output has scrolled out of the
          buffer — it is not in the panel below and not in a download. Raise the tail size and reconnect to
          fetch an earlier window from the cluster.
        </Alert>
      )}

      {!ready && (
        <EmptyState
          title="Choose a container"
          description={`This pod has ${names?.length ?? 'several'} containers. The console will not pick one for you — the logs of the wrong container look exactly like the logs of the right one.`}
        />
      )}

      {ready && (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          tabIndex={0}
          role="log"
          aria-label={`Logs for ${namespace}/${name}`}
          aria-live="off"
          data-testid="log-output"
          style={{
            height,
            overflow: 'auto',
            border: '1px solid var(--admin-border, #d2d2d2)',
            borderRadius: 'var(--pf-t--global--border--radius--small, 4px)',
            background: 'var(--admin-surface, #f2f2f2)',
            fontFamily: 'var(--admin-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
            fontSize: '0.8125rem',
            lineHeight: 1.5,
            padding: '0.5rem',
          }}
        >
          {lines.length === 0 && status === 'streaming' && (
            <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>
              Connected. No output yet — this container has produced nothing in the requested window.
            </span>
          )}
          {lines.map((entry) => (
            <div
              key={entry.seq}
              style={{
                whiteSpace: wrap ? 'pre-wrap' : 'pre',
                wordBreak: wrap ? 'break-all' : 'normal',
              }}
            >
              {timestamps && entry.ts && (
                <span style={{ color: 'var(--admin-muted, #6a6e73)' }}>{entry.ts} </span>
              )}
              {entry.line}
            </div>
          ))}
        </div>
      )}

      {/* The three terminal states, each said in its own words. */}
      {terminal === 'error' && errorFrame && (
        <Alert
          isInline
          variant="danger"
          title={`Stream failed — ${errorFrame.reason}`}
          data-testid="log-error"
          style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}
        >
          <p>{errorFrame.message}</p>
          {errorFrame.detail && (
            <pre style={{ whiteSpace: 'pre-wrap', margin: '0.5rem 0 0', fontSize: '0.8125rem' }}>
              {errorFrame.detail}
            </pre>
          )}
          {errorFrame.hint && <p style={{ fontWeight: 600, marginBlockStart: '0.5rem' }}>{errorFrame.hint}</p>}
        </Alert>
      )}

      {(terminal === 'ended' || terminal === 'lost') && (
        <Alert
          isInline
          variant={TERMINAL[terminal].variant}
          title={terminal === 'ended' ? `${TERMINAL.ended.title} (${endReason})` : TERMINAL.lost.title}
          data-testid={`log-${terminal}`}
          style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}
        >
          {TERMINAL[terminal].body}
        </Alert>
      )}
    </div>
  );
}

/**
 * Trim to the buffer cap and report how much was lost.
 *
 * The counter is what keeps the cap honest: a viewer that silently discarded
 * the head of the log would let an operator scroll to the top of what they
 * believe is the whole thing and conclude the pod started cleanly.
 */
function capLines(next, setDropped) {
  if (next.length <= MAX_BUFFERED_LINES) return next;
  const overflow = next.length - MAX_BUFFERED_LINES;
  setDropped((d) => d + overflow);
  return next.slice(overflow);
}

export default LogViewer;
