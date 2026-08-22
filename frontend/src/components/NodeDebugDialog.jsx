/**
 * NodeDebugDialog — `POST /api/nodes/{name}/debug` (§5.5).
 *
 * A thin wrapper over `MutationDialog`, like every other write dialog here. What
 * is different is what it is asking the operator to agree to.
 *
 * A node debug pod is the most privileged object this console creates. It is
 * pinned to one machine, tolerates every taint, shares the host's PID and
 * network namespaces, and mounts the node's root filesystem. A shell in it is,
 * for practical purposes, root on that machine — it can read every Secret the
 * kubelet has written to disk and see every process on the box. That is more
 * than `pods/exec` on any pod running there, and more than every other write
 * this console offers put together.
 *
 * So the form's job is not to collect two fields. It is to make sure that what
 * the operator is about to create is something they actually read.
 *
 * **The consequences are listed before the preview, in plain words.** The diff
 * that follows contains `hostPath: /` and `hostPID: true`, and an operator who
 * knows Kubernetes well will read them correctly — but "this pod can read every
 * Secret on this node" is not a sentence a YAML diff says out loud, and it is
 * the sentence that matters.
 *
 * **Writable is a separate, deliberate act.** The host filesystem mounts
 * read-only by default — a departure from `kubectl debug`, which always mounts
 * it writable. Read-only covers almost all node debugging and cannot rewrite a
 * static pod manifest or leave a binary behind that runs as root at next boot.
 * Turning it writable changes the confirm button to a danger button and requires
 * the node's name to be typed, because at that point the operator is asking for
 * write access to the machine's root filesystem and should have to mean it.
 *
 * **The namespace is shown, not assumed.** It comes from the server
 * (`ADMIN_NODE_DEBUG_NAMESPACE`) and is where a privileged pod is about to
 * appear. An operator confirming one should not have to guess where.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  List,
  ListItem,
  TextInput,
} from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import { nodes as nodesApi } from '../api/client';

export function NodeDebugDialog({
  isOpen,
  /** The node the pod will be pinned to. */
  node,
  /** Where it will be created — from the server, never guessed by the client. */
  namespace,
  onClose,
  onApplied,
}) {
  const [image, setImage] = useState('');
  const [writable, setWritable] = useState(false);

  // Reset on every opening. A dialog reopened for a different node that kept
  // `writable` from last time would carry an escalation the operator agreed to
  // once into a machine they have not looked at.
  useEffect(() => {
    if (!isOpen) return;
    setImage('');
    setWritable(false);
  }, [isOpen, node]);

  return (
    <MutationDialog
      isOpen={isOpen}
      title={`Create a debug pod on ${node}`}
      description={`A pod pinned to ${node}, in namespace ${namespace ?? '—'}. It does not move workloads and does not change the node.`}
      previewLabel="Preview the pod"
      confirmLabel="Create debug pod"
      // Danger only when the host filesystem is writable. Red for the read-only
      // case as well would be crying wolf on the safer of the two, and this
      // console's own standard is that painting ordinary things red teaches
      // operators to ignore red.
      isDanger={writable}
      // The node's name, typed, when they are asking for write access to its
      // root filesystem. `DrainDialog` uses the same control for the same
      // reason: an action whose blast radius is a whole machine.
      requireTyped={writable ? node : undefined}
      request={(dryRun) =>
        nodesApi.createDebugPod(node, {
          image: image.trim() || null,
          writableHostFilesystem: writable,
          dryRun,
        })
      }
      onClose={onClose}
      onApplied={onApplied}
      summarize={(result) =>
        result?.applied === true
          ? {
              variant: 'success',
              title: `${result.pod} is scheduled on ${node}`,
              body:
                `The API server accepted the pod into ${result.namespace}. It bypasses the scheduler — ` +
                `spec.nodeName pins it — so it goes straight to the kubelet, which pulls ${result.image} ` +
                'and starts it. Remove it when you are done: nothing else will.',
            }
          : {
              variant: 'warning',
              title: 'The request completed without confirming the pod was created',
              body: 'Re-read the node before assuming a debug pod is running on it.',
            }
      }
      className="admin-node-debug-dialog"
    >
      <Form>
        <Alert
          isInline
          variant="warning"
          title="What this pod can do"
          data-testid="node-debug-consequences"
        >
          <List>
            <ListItem>
              Read the node&apos;s entire filesystem at <code>/host</code>. That includes the
              ServiceAccount token and mounted Secrets of <strong>every pod on this node</strong> —
              each one a live credential — and the node&apos;s own kubelet certificate.
            </ListItem>
            <ListItem>
              See every process on the node, including the environment variables of every container
              running here.
            </ListItem>
            <ListItem>
              Use the node&apos;s own network. NetworkPolicy does not apply to it, the cloud
              provider&apos;s instance-metadata endpoint is reachable, and it can capture traffic
              transiting this node.
            </ListItem>
            <ListItem>
              Land on this node even if it is cordoned or tainted. That is the point — a node worth
              debugging is often one the scheduler would refuse — but it means a control-plane node
              is reachable this way too.
            </ListItem>
            <ListItem>
              It is <strong>not</strong> privileged and carries no Kubernetes API token, so it
              cannot reconfigure the kernel or act on the cluster. Reading the machine is what it is
              for.
            </ListItem>
          </List>
        </Alert>

        <FormGroup label="Debug image" fieldId="node-debug-image">
          <TextInput
            id="node-debug-image"
            value={image}
            onChange={(_event, value) => setImage(value)}
            placeholder="Leave empty to use this console's configured default"
            aria-label="Debug image"
            data-testid="node-debug-image"
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Pulled with <code>IfNotPresent</code>: the node being debugged may be the one that
                cannot reach the registry, and a cached image that starts beats a fresh pull that
                hangs. The preview shows exactly which image will be requested.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        <FormGroup fieldId="node-debug-writable">
          <Checkbox
            id="node-debug-writable"
            label="Mount the host filesystem read-write"
            description={
              'Off by default, and deliberately unlike kubectl debug, which always mounts it ' +
              'writable. Read-only covers almost all node debugging and cannot rewrite a static ' +
              'pod manifest or leave behind a binary that runs as root at next boot.'
            }
            isChecked={writable}
            onChange={(_event, checked) => setWritable(checked)}
            data-testid="node-debug-writable"
          />
        </FormGroup>

        {writable && (
          <Alert
            isInline
            variant="danger"
            title="This gives write access to the node's root filesystem"
            data-testid="node-debug-writable-warning"
          >
            Anything with a shell in this pod can modify the machine: replace binaries, edit the
            kubelet&apos;s configuration, or write a static pod manifest that the kubelet will start
            as root. Confirming requires typing the node&apos;s name.
          </Alert>
        )}

        <Alert isInline variant="info" title="Nothing removes this pod for you">
          <code>kubectl debug</code> does not clean up either — it has no <code>--rm</code>, and
          leaked node debug pods are common. This one stays until it is removed, which the Remove
          button in this section does. A debug pod left running with a node&apos;s filesystem
          attached is a hazard that outlives whoever created it.
        </Alert>
      </Form>
    </MutationDialog>
  );
}

export default NodeDebugDialog;
