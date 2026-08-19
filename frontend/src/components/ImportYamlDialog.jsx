/**
 * ImportYamlDialog — create any object the cluster serves from a pasted manifest.
 *
 * Opened from the masthead's "+" button rather than from a specific resource
 * page, so unlike `EditYamlDialog` it does not know `group`/`version`/`plural`
 * up front — it has to learn them from what the operator pastes. It does that
 * with the same §4 catalog `Explorer` already uses to turn a click into a route:
 * `apiVersion` + `kind` from the parsed document, matched against a catalog
 * entry's own precomputed `apiVersion`, gives the one triple that can be POSTed
 * to.
 *
 * **The document's namespace wins; the masthead selector is the fallback.**
 * This mirrors `app.admin.apply._resolve_namespace` exactly — the backend
 * already applies that precedence, so sending the masthead's selection as
 * `namespace` on every request is enough. What this component adds is telling
 * the operator *before* they preview which one will actually be used, because
 * "created in the wrong namespace" is not a mistake a diff against nothing
 * (creates have no `before`) makes obvious — the diff is the whole object, and
 * the reviewer's eye goes to the spec, not to hunting `metadata.namespace` in a
 * wall of green.
 *
 * **`initialText` is how "Create Pod" differs from the masthead's blank
 * "Import YAML".** Both are this same dialog; a per-kind button on a list page
 * just seeds the editor with a starter manifest for that kind (see
 * `pages/_data.js`'s `POD_TEMPLATE` / `WORKLOAD_TEMPLATES`) instead of leaving
 * it empty. Deliberately never given a namespace of its own — the fallback
 * above already supplies one, and a template that hardcoded the namespace open
 * at click time would go stale the moment the operator switched the masthead
 * selector before finishing the edit.
 *
 * **Multi-document paste is refused, not split.** `YamlEditor`'s own validator
 * already blocks it with "apply them one at a time" — the same refusal
 * `create_from_yaml` enforces server-side. Splitting it here and firing N
 * requests would mean the whole surface silently grows a bulk-create feature
 * with its own partial-failure story, in the one dialog this codebase is most
 * careful to keep boring: an "Import YAML" that succeeds on 3 objects and fails
 * on the 4th, with nothing above summing that up, is exactly the confidently
 * wrong answer §0 exists to rule out.
 */
import { useEffect, useMemo, useState } from 'react';
import { Alert } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import YamlEditor from './YamlEditor';
import { resources as resourcesApi } from '../api/client';
import { useNamespace } from '../contexts/NamespaceContext';

export function ImportYamlDialog({ isOpen, title = 'Import YAML', initialText = '', onClose, onApplied }) {
  const { selected: namespaceFromMasthead } = useNamespace();
  const [text, setText] = useState(initialText);
  const [validity, setValidity] = useState({ valid: false, empty: true, error: null, notes: [], parsed: null });
  const [catalog, setCatalog] = useState({ items: [], loading: true, error: null });

  // One fetch per opening. Not `useAsync` (that hook is page-scoped, keyed for
  // cross-navigation caching this one-shot dialog does not need) — a plain
  // effect with a `live` guard against the unmount-mid-fetch race is the whole
  // requirement, and `pages/_data.js` would be a components -> pages import
  // running the wrong way.
  useEffect(() => {
    if (!isOpen) return undefined;
    let live = true;
    setCatalog({ items: [], loading: true, error: null });
    resourcesApi
      .catalog()
      .then((result) => {
        if (live) setCatalog({ items: result?.items ?? [], loading: false, error: null });
      })
      .catch((err) => {
        if (live) setCatalog({ items: [], loading: false, error: err });
      });
    return () => {
      live = false;
    };
  }, [isOpen]);

  const parsed = validity.parsed;
  const apiVersion = parsed?.apiVersion || null;
  const kind = parsed?.kind || null;
  const docNamespace = (parsed?.metadata && typeof parsed.metadata === 'object' ? parsed.metadata.namespace : null) || null;
  const targetNamespace = docNamespace || namespaceFromMasthead || null;

  // Catalog items carry their own precomputed `apiVersion` (§4's
  // `_resource_item`), so matching is one equality check rather than a
  // group/version split reimplemented on this side of the wire.
  const entry = useMemo(() => {
    if (!apiVersion || !kind) return null;
    return catalog.items.find((item) => item.apiVersion === apiVersion && item.kind === kind) ?? null;
  }, [catalog.items, apiVersion, kind]);

  const previewBlocked = catalog.loading
    ? 'The API catalog is still loading.'
    : catalog.error
      ? `The API catalog could not be read: ${catalog.error.message}`
      : validity.empty
        ? 'Paste the object to create.'
        : !validity.valid
          ? (validity.error?.reason ?? validity.notes.find((n) => n.severity === 'error')?.text ?? 'The YAML in the editor is not valid.')
          : !apiVersion || !kind
            ? 'The object needs both `apiVersion` and `kind` to know where to create it.'
            : !entry
              ? `This cluster does not serve ${kind} at apiVersion ${apiVersion}.`
              : !(entry.verbs ?? []).includes('create')
                ? `${entry.kind} does not support create on this cluster (verbs: ${(entry.verbs ?? []).join(', ') || 'none advertised'}).`
                : entry.namespaced && !targetNamespace
                  ? 'This is a namespaced resource. Set metadata.namespace in the document, or choose a namespace from the masthead selector.'
                  : null;

  if (!isOpen) return null;

  return (
    <MutationDialog
      isOpen
      title={title}
      description="Paste one Kubernetes object as YAML or JSON. Preview first — the diff below is the API server's own projection of what creating it would produce."
      request={(dryRun) =>
        resourcesApi.create(entry.group, entry.version, entry.resource, {
          yaml: text,
          namespace: targetNamespace,
          dryRun,
        })
      }
      canPreview={!previewBlocked}
      previewDisabledReason={previewBlocked}
      confirmLabel="Create"
      summarize={(result) => ({
        variant: 'success',
        title: `${entry?.kind ?? 'Object'}${result?.target?.name ? ` "${result.target.name}"` : ''} created`,
        body: null,
      })}
      onApplied={onApplied}
      onClose={onClose}
    >
      <Alert
        isInline
        variant="info"
        className="admin-confirm__alert"
        data-testid="import-yaml-target"
        title={
          entry
            ? `Will create ${entry.kind} (${entry.apiVersion}) ${
                entry.namespaced ? `in namespace "${targetNamespace ?? '(none selected)'}"` : '— cluster-scoped'
              }`
            : 'Where this is created is read from the document\'s own apiVersion and kind, once it parses as one.'
        }
      />
      <YamlEditor
        value={text}
        onChange={setText}
        onValidityChange={setValidity}
        label="New object"
        ariaLabel="YAML to import"
      />
    </MutationDialog>
  );
}

export default ImportYamlDialog;
