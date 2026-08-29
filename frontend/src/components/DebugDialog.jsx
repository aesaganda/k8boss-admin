/**
 * DebugDialog — `POST /api/pods/{ns}/{name}/debug` (§7.4).
 *
 * A thin wrapper over `MutationDialog`, like `ScaleDialog` and `DrainDialog`:
 * it owns a form and a `request` closure, and inherits the dry-run-then-confirm
 * handshake, the projected diff, the 409 re-projection, the read-only gate and
 * the audit link from the spine. Attaching a debug container is a write — it
 * patches `pods/ephemeralcontainers` — so it goes the same way every other
 * write in this console goes, and there is no button here that reaches a
 * cluster without having shown a diff first.
 *
 * Three judgements live here rather than in the spine.
 *
 * **The permanence is said before the preview, not after it.** An ephemeral
 * container cannot be removed: the API has no verb for it, and it lives as long
 * as the pod does. That is the fact the operator is actually consenting to, and
 * a diff showing five added lines does not communicate it. It is the first
 * thing in the form.
 *
 * **A command is split here and shown back as argv.** The backend takes a list
 * of arguments precisely so that quoting in a text box does not decide what runs
 * as root inside somebody's pod. A field that quietly split on spaces and sent
 * the pieces would put that decision back — so the split is performed, and the
 * result is rendered as the literal argument list that will be sent. What the
 * operator confirms is what they can see.
 *
 * **The image field may be left empty, and says so.** The console has a
 * configured default (`ADMIN_DEBUG_IMAGE`) which the browser does not know: an
 * air-gapped deployment points it at an internal registry. Rather than guess it
 * and render a value that may be wrong, the field is optional and the *diff*
 * shows which image the API server was actually asked for — before the write.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Label,
  LabelGroup,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { pods as podsApi } from '../api/client';

/**
 * Images offered as one-click starting points.
 *
 * Not a policy and not a whitelist — the field takes anything, and the backend
 * refuses nothing but malformed references. These are here because the most
 * common reason an operator abandons a debug session is not knowing what to put
 * in the box, and because typing a registry path from memory during an incident
 * is how one ends up waiting on an ImagePullBackOff.
 */
const SUGGESTED_IMAGES = [
  { image: 'busybox:1.36', why: 'A shell, ps, wget, netstat. Two megabytes.' },
  { image: 'nicolaka/netshoot:v0.13', why: 'Network tools: dig, tcpdump, curl, iproute2.' },
  { image: 'ubuntu:24.04', why: 'A full userland when the small images are missing something.' },
];

const NAME_PATTERN = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const MAX_NAME_LENGTH = 63;

/** Split a command line into argv. Whitespace only — quoting is not interpreted. */
function toArgv(text) {
  return String(text ?? '')
    .split(/\s+/)
    .filter(Boolean);
}

export function DebugDialog({
  isOpen,
  namespace,
  name,
  /** The pod's own container names, for the process-namespace target picker. */
  containers = [],
  /** Every name already taken in this pod: containers, init and ephemeral. */
  taken = [],
  onClose,
  onApplied,
}) {
  const [image, setImage] = useState('');
  const [container, setContainer] = useState('');
  const [target, setTarget] = useState('');
  const [commandText, setCommandText] = useState('');
  const [tty, setTty] = useState(true);
  // Not state: changing it must not re-render, and it is read only inside
  // `request`. See the comment there for what it is for.
  const previewedName = useRef(null);

  // Reset on every opening: a dialog reopened for a different pod that kept the
  // previous pod's target container would preflight one thing and patch
  // another.
  useEffect(() => {
    if (!isOpen) return;
    setImage('');
    setContainer('');
    setTarget('');
    setCommandText('');
    setTty(true);
    // Cleared with the form: a name projected for the previous pod, replayed
    // into this one, would be the console choosing a container name from a
    // dialog the operator has already closed.
    previewedName.current = null;
  }, [isOpen, namespace, name]);

  const argv = useMemo(() => toArgv(commandText), [commandText]);

  const nameError = useMemo(() => {
    const value = container.trim();
    if (!value) return null;
    if (value.length > MAX_NAME_LENGTH || !NAME_PATTERN.test(value)) {
      return (
        'A container name is a DNS-1123 label: lower-case letters, digits and dashes, starting and ' +
        `ending with an alphanumeric, at most ${MAX_NAME_LENGTH} characters.`
      );
    }
    // Mirrored from the backend, which is the authority and refuses it too —
    // but rule 11.4 says an unusable action is disabled *with the reason*
    // rather than offered and then rejected, and a round trip spent to learn
    // that "envoy" is taken also spends an audit row.
    if (taken.includes(value)) {
      return `This pod already has a container named "${value}". Containers, init containers and debug containers share one namespace of names.`;
    }
    return null;
  }, [container, taken]);

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Attach a debug container to ${name}`}
      description={`Pod ${namespace}/${name}. A second container is scheduled into the running pod; nothing about the pod's own containers changes.`}
      previewLabel="Preview the change"
      confirmLabel="Attach"
      canPreview={!nameError}
      previewDisabledReason={nameError}
      request={async (dryRun) => {
        // The name the dry run projected, replayed on the confirming call.
        //
        // This is the difference between previewing the write and previewing
        // *a* write. When the operator leaves the name blank the backend
        // generates one, and it generates a fresh one per request — so without
        // this the diff on screen would say `debugger-x4k2p` and the container
        // that appeared in the pod would be `debugger-m7q3v`. The operator
        // approved a specific object; they must get that object.
        //
        // Carried by the client because the backend is stateless between the
        // two calls, which is the same reason `MutationDialog` carries
        // `resourceVersion` across them. A name the operator typed always wins.
        const chosen = container.trim() || (dryRun ? null : previewedName.current);
        const result = await podsApi.attachDebugContainer(namespace, name, {
          image: image.trim() || null,
          container: chosen,
          targetContainer: target || null,
          command: argv.length ? argv : null,
          tty,
          dryRun,
        });
        if (dryRun) previewedName.current = result?.container ?? null;
        return result;
      }}
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${result.container} is attached to ${name}`,
              body:
                `The API server accepted the container. The kubelet pulls ${result.image} and starts it ` +
                'asynchronously, so it may be a moment before a terminal can attach — and an image that ' +
                'cannot be pulled shows as ImagePullBackOff in the list, not as a failure of this action.',
            }
          : {
              variant: 'warning',
              title: 'The request completed without confirming the container was attached',
              body: 'Re-read the pod before assuming a debug container is running in it.',
            }
      }
      className="admin-debug-dialog"
    >
      <Form>
        <Alert
          isInline
          variant="warning"
          title="A debug container cannot be removed"
          data-testid="debug-permanence"
        >
          The Kubernetes API has no verb for deleting an ephemeral container. Once this is attached it
          stays in the pod&apos;s manifest, and keeps running until the pod is replaced. Restarting the
          workload is what removes it.
        </Alert>

        <FormGroup label="Debug image" fieldId="debug-image">
          <TextInput
            id="debug-image"
            value={image}
            onChange={(_event, value) => setImage(value)}
            placeholder="Leave empty to use this console's configured default"
            aria-label="Debug image"
            data-testid="debug-image"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                The image runs alongside the pod&apos;s containers, sharing its network and volumes. Pick
                one that carries what you need — the point of a debug container is that the pod&apos;s own
                image does not. The preview shows exactly which image will be requested.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
          <LabelGroup
            categoryName="Suggestions"
            style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}
          >
            {SUGGESTED_IMAGES.map((suggestion) => (
              <Label
                key={suggestion.image}
                color={image.trim() === suggestion.image ? 'blue' : 'grey'}
                isCompact
                onClick={() => setImage(suggestion.image)}
                // No `href`. PatternFly's Label renders its child as an anchor
                // when one is given and drops `onClick` on that branch, so the
                // chips looked clickable, showed the hover styling, and did
                // nothing. Without it the child is a real <button> and the
                // handler is wired — and `pf-m-clickable` is still applied,
                // because `onClick` alone satisfies Label's isClickable test.
                data-testid={`debug-image-suggestion-${suggestion.image}`}
                title={suggestion.why}
              >
                {suggestion.image}
              </Label>
            ))}
          </LabelGroup>
        </FormGroup>

        <FormGroup label="Container name" fieldId="debug-name">
          <TextInput
            id="debug-name"
            value={container}
            onChange={(_event, value) => setContainer(value)}
            placeholder="Generated as debugger-…"
            validated={nameError ? 'error' : 'default'}
            aria-label="Debug container name"
            data-testid="debug-name"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem variant={nameError ? 'error' : 'default'}>
                {nameError ??
                  'Optional. A generated name is recognisable as a debug container six months later, in a manifest nobody remembers editing.'}
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        <FormGroup label="Share the process namespace of" fieldId="debug-target">
          <FormSelect
            id="debug-target"
            value={target}
            onChange={(_event, value) => setTarget(value)}
            aria-label="Target container"
            data-testid="debug-target"
          >
            <FormSelectOption value="" label="No container — network and volumes only" />
            {containers.map((candidate) => (
              <FormSelectOption key={candidate} value={candidate} label={candidate} />
            ))}
          </FormSelect>
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Targeting a container lets the debug container see its processes and its
                <code> /proc/1/root</code>. Not every container runtime implements this, and where it is
                unimplemented the field is ignored rather than refused — so a debug container that shows
                you no application processes has told you something about the node, not about the pod.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        <FormGroup label="Command" fieldId="debug-command">
          <TextInput
            id="debug-command"
            value={commandText}
            onChange={(_event, value) => setCommandText(value)}
            placeholder="Leave empty to run the image's own entrypoint"
            aria-label="Command"
            data-testid="debug-command"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Split on spaces. Quotes are <strong>not</strong> interpreted — what is sent is the exact
                argument list shown below, so nothing about how you quote this decides what runs in the
                pod.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
          {argv.length > 0 && (
            <div
              data-testid="debug-argv"
              style={{
                marginBlockStart: 'var(--admin-gap-sm, 0.5rem)',
                fontFamily: 'var(--admin-mono, ui-monospace, monospace)',
                fontSize: '0.8125rem',
              }}
            >
              argv: [{argv.map((arg) => JSON.stringify(arg)).join(', ')}]
            </div>
          )}
        </FormGroup>

        <FormGroup fieldId="debug-tty">
          <Checkbox
            id="debug-tty"
            label="Allocate a terminal (TTY)"
            description="On for a shell you are going to type in. Off for a debug image whose entrypoint writes a report and exits."
            isChecked={tty}
            onChange={(_event, checked) => setTty(checked)}
            data-testid="debug-tty"
          />
        </FormGroup>
      </Form>
    </MutationDialog>
  );
}

export default DebugDialog;
