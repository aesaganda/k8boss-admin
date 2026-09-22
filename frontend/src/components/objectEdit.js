/**
 * Reading one live object so a form can change one part of it, and sending the
 * whole thing back through §4's update.
 *
 * **There is no new write here, and that is the point.** A labels form could
 * have had an endpoint of its own; it would have been a second path to the
 * cluster with its own preflight, its own audit row and its own opportunity to
 * skip one. What these dialogs do instead is exactly what the §4 YAML editor
 * does — `PUT` the object with the `resourceVersion` it was read at — with the
 * text field replaced by a form over one field. The funnel, the diff, the 409
 * and the audit row are the ones that already exist.
 *
 * **The whole object goes back, so the whole object has to come in.** A form
 * that sent only `metadata.labels` would be sending an object with no spec, and
 * the API server would take it literally. `load()` is the browser's one reading
 * of a manifest (ADR-0009) and `toYaml()` its inverse, so the bytes that leave
 * here mean what the bytes that arrived meant.
 *
 * **`watch: false`.** The §4 editor polls because it is a page an operator
 * leaves open; a dialog is open for a minute, and a poll that re-seeded the
 * form under their hands would throw away what they typed. The cost is that a
 * concurrent change is learned from the API server's 409 rather than from a
 * banner — which is rule 0.4 working, not failing: the conflict carries the
 * current version and a fresh diff.
 */
import { useMemo } from 'react';
import { load, toYaml } from './clusterYaml';
import { resources } from '../api/client';
import { useLiveYaml } from '../pages/_data';

/**
 * The live object behind a dialog, parsed.
 *
 * `object` is `null` while the read is in flight and stays `null` if it failed
 * — never `{}`, which a form would render as an object with no labels and offer
 * to save.
 */
export function useEditableObject({ group, version, plural, name, namespace, isOpen }) {
  const live = useLiveYaml({ group, version, plural, name, namespace, enabled: isOpen, watch: false });

  const parsed = useMemo(() => {
    if (live.text == null) return { object: null, parseError: null };
    try {
      return { object: load(live.text), parseError: null };
    } catch (err) {
      // Our own backend serialised this, so a parse failure is a defect here
      // rather than bad input — but it must still read as a refusal to guess,
      // not as an object with nothing in it.
      return { object: null, parseError: err };
    }
  }, [live.text]);

  return {
    object: parsed.object,
    resourceVersion: parsed.object?.metadata?.resourceVersion ?? null,
    loading: live.loading && live.text == null,
    error: live.error ?? parsed.parseError,
    reload: live.reload,
  };
}

/** Why Preview cannot run yet, or `null`. Shared by every form over an object. */
export function readBlocked({ loading, error, object, resourceVersion }) {
  if (loading) return 'The object is still loading.';
  if (error) return `The object could not be read: ${error.message}`;
  if (!object) return 'The object could not be read.';
  if (resourceVersion == null) {
    return (
      'This object carries no metadata.resourceVersion, so the update cannot be made safe against a ' +
      'concurrent edit.'
    );
  }
  return null;
}

/** §4's `PUT`, with the edited object as its body. */
export function putObject({ group, version, plural, name, namespace }, object, { resourceVersion, dryRun }) {
  return resources.update(group, version, plural, name, {
    yaml: toYaml(object),
    namespace,
    resourceVersion,
    dryRun,
  });
}

/**
 * The pod template's spec inside a workload, by kind.
 *
 * Mirrors `pod_template()` in `backend/app/services/workloads.py`: a CronJob
 * keeps it under `jobTemplate` and every other kind has it at `spec.template`.
 * Returns `null` for a kind with no template rather than creating one — a
 * dialog that wrote `spec.template` into an object that has none would be
 * sending a manifest the API server rejects, having shown the operator a form
 * that looked like it worked.
 */
export function podSpecOf(object, kind) {
  const path = kind === 'CronJob'
    ? object?.spec?.jobTemplate?.spec?.template?.spec
    : object?.spec?.template?.spec;
  return path ?? null;
}

/** The same path, on a clone, ready to be written into. */
export function withPodSpec(object, kind, mutate) {
  const next = structuredClone(object);
  const spec = kind === 'CronJob'
    ? next.spec?.jobTemplate?.spec?.template?.spec
    : next.spec?.template?.spec;
  if (!spec) return null;
  mutate(spec);
  return next;
}
