/**
 * ImportYamlDialog — create any object the cluster serves, from a form or from
 * a pasted manifest.
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
 * **`templates` is how "Create Service" differs from the masthead's blank
 * "Import YAML".** Both are this same dialog; a create button on a listing
 * hands it the starters for that one kind (`components/templates.js`) and the
 * editor opens on the first of them instead of empty. Deliberately never given
 * a namespace of its own — the fallback above already supplies one, and a
 * starter that hardcoded the namespace open at click time would go stale the
 * moment the operator switched the masthead selector before finishing the edit.
 *
 * Several kinds have more than one starter, because the shapes an operator
 * picks between are different objects: a headless Service and a LoadBalancer
 * share a kind and almost nothing else. Choosing another one **replaces the
 * document**, which is the one destructive thing this dialog can do before it
 * has sent anything anywhere — so it is said out loud and confirmed, and only
 * when there is something to lose. An untouched starter is swapped
 * immediately: refusing to switch away from a document nobody has typed into
 * would be a confirmation about nothing.
 *
 * **Multi-document paste is refused, not split.** `YamlEditor`'s own validator
 * already blocks it with "apply them one at a time" — the same refusal
 * `create_from_yaml` enforces server-side. Splitting it here and firing N
 * requests would mean the whole surface silently grows a bulk-create feature
 * with its own partial-failure story, in the one dialog this codebase is most
 * careful to keep boring: an "Import YAML" that succeeds on 3 objects and fails
 * on the 4th, with nothing above summing that up, is exactly the confidently
 * wrong answer §0 exists to rule out.
 *
 * ## The two views (§11.9)
 *
 * "Configure via: Form view / YAML view", which is OpenShift's own control on
 * its create screens, with the difference `RouteDialog` already draws for §13:
 *
 *   **The document is the source of truth and the form is a projection of it.**
 *
 * There is no form model. Every control is a lens into the parsed object, and a
 * form edit is one `setIn` that copies the rest of the document through
 * untouched, so a `spec.affinity` somebody pasted survives being nowhere in the
 * form. What the operator switches back to in YAML view is the object the
 * controls have been editing all along, which is why switching views needs no
 * "your changes will be discarded" warning — and `unrepresented()` names, by
 * path, every field the form is not showing, which is the half of OpenShift's
 * *"some fields may not be represented in this form view"* that leaves an
 * operator no way to find out which.
 *
 * **The form opens first when the dialog was seeded with a kind it can
 * project** — "Create Deployment" from the Workloads page, "Create Pod" from
 * Pods. The masthead's blank "+" has no document yet and therefore no kind, so
 * it opens in YAML view with the form offered and disabled, naming what it is
 * waiting for. Nothing ever switches the view on the operator: a paste that
 * makes a form possible enables the control and does not press it.
 *
 * **Re-serialising rewrites the text, and that is said before it happens.** A
 * form edit writes `toYaml` of the parsed object, and three things live in
 * the text rather than in the object: comments, which no parse carries; anchors
 * and merge keys, which a parse resolves. The first is lost and the other two
 * are expanded — different enough to be reported differently — and both are on
 * screen while that is still actionable, rather than discovered afterwards in
 * the diff.
 */
import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Radio, Tooltip } from '@patternfly/react-core';
import MutationDialog from './MutationDialog';
import ObjectForm from './ObjectForm';
import YamlEditor, { validateYaml } from './YamlEditor';
import { divergenceNote, toYaml } from './clusterYaml';
import {
  FORM_MODELS,
  containsCycle,
  formModelFor,
  formatPath,
  localIssues,
  rewriteLosses,
  unrepresented,
} from './objectForm';
import { PartialBanner } from './ui';
import { resources as resourcesApi } from '../api/client';
import { useNamespace } from '../contexts/NamespaceContext';

/**
 * The kinds the form view can project, listed rather than counted.
 *
 * Read off `FORM_MODELS` instead of written down, because the sentence beside a
 * disabled radio is the only place an operator finds out which kinds have a
 * form — and a hand-written list of them is one that stops being true the first
 * time somebody adds a model and does not think to come here. Rule 11.4 asks
 * for the reason a control is unavailable; a reason that has quietly gone stale
 * is worse than none, because it is the one they believe.
 */
const MODELLED_KINDS = [...new Set(FORM_MODELS.map((entry) => entry.kind))].sort().join(', ');

/** How `localIssues` severities become PatternFly alert variants and headings. */
const ISSUE_ALERTS = [
  {
    severity: 'error',
    variant: 'danger',
    title: 'The API server will refuse this document as it stands',
    testid: 'create-issues-error',
  },
  {
    severity: 'warning',
    variant: 'warning',
    title: 'Worth reading before creating this',
    testid: 'create-issues-warning',
  },
  {
    severity: 'info',
    variant: 'info',
    title: 'What this object does',
    testid: 'create-issues-info',
  },
];

export function ImportYamlDialog({
  isOpen,
  title = 'Import YAML',
  initialText = '',
  templates = [],
  onClose,
  onApplied,
}) {
  const { selected: namespaceFromMasthead } = useNamespace();
  // What the editor opened on. An explicit `initialText` wins so the masthead's
  // blank "+" and any caller with a document of its own keep working; otherwise
  // it is the first starter for the kind the button was on.
  const seedText = initialText || templates[0]?.text || '';
  const [text, setText] = useState(seedText);
  const [starterId, setStarterId] = useState(templates[0]?.id ?? null);
  // A starter the operator asked to switch to and has not confirmed. Held
  // rather than applied, because the document it would overwrite is the one
  // they have been editing.
  const [pendingId, setPendingId] = useState(null);
  const [catalog, setCatalog] = useState({ items: [], unavailable: [], loading: true, error: null });

  // Which view the dialog opens in is decided once, from the document it was
  // seeded with — never re-decided as the operator types. A view that switched
  // itself when a paste became parseable would move the control out from under
  // somebody mid-edit.
  const [view, setView] = useState(() => {
    const seeded = validateYaml(seedText);
    return seeded.valid && formModelFor(seeded.parsed?.apiVersion, seeded.parsed?.kind) ? 'form' : 'yaml';
  });

  const starter = templates.find((entry) => entry.id === starterId) ?? null;
  const pending = templates.find((entry) => entry.id === pendingId) ?? null;
  // "Edited" is measured against the starter itself rather than tracked with a
  // flag: an operator who types a character and deletes it again has not
  // changed the document, and warning them they are about to lose it would
  // teach them to click past the warning that matters.
  const edited = text !== (starter?.text ?? seedText);

  const applyStarter = (entry) => {
    setStarterId(entry.id);
    setText(entry.text);
    setPendingId(null);
  };

  const chooseStarter = (entry) => {
    if (entry.id === starterId) return;
    if (edited) setPendingId(entry.id);
    else applyStarter(entry);
  };

  // One fetch per opening. Not `useAsync` (that hook is page-scoped, keyed for
  // cross-navigation caching this one-shot dialog does not need) — a plain
  // effect with a `live` guard against the unmount-mid-fetch race is the whole
  // requirement, and `pages/_data.js` would be a components -> pages import
  // running the wrong way.
  useEffect(() => {
    if (!isOpen) return undefined;
    let live = true;
    setCatalog({ items: [], unavailable: [], loading: true, error: null });
    resourcesApi
      .catalog()
      .then((result) => {
        if (live) {
          // §1.2's `unavailable` is kept rather than dropped for the items. A
          // discovery that could not read `apps` returns a catalog with no
          // Deployment in it, and answering "this cluster does not serve
          // Deployment" from that is the empty-is-never-blind defect with a
          // create button next to it — it sends an operator to install what
          // they already have.
          setCatalog({
            items: result?.items ?? [],
            unavailable: result?.unavailable ?? [],
            loading: false,
            error: null,
          });
        }
      })
      .catch((err) => {
        if (live) setCatalog({ items: [], unavailable: [], loading: false, error: err });
      });
    return () => {
      live = false;
    };
  }, [isOpen]);

  // Parsed here rather than reported up from `YamlEditor`, because the form
  // view does not mount the editor and still needs the object. `validateYaml`
  // is the same function the editor runs on its own value, so the two views can
  // never disagree about what the text means.
  const validity = useMemo(() => validateYaml(text), [text]);
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

  const model = useMemo(() => (validity.valid ? formModelFor(apiVersion, kind) : null), [validity.valid, apiVersion, kind]);
  const issues = useMemo(() => localIssues(parsed, model), [parsed, model]);
  const unrepresentedPaths = useMemo(
    () => (model ? unrepresented(parsed, model).map(formatPath) : []),
    [parsed, model],
  );
  const losses = useMemo(
    () => (view === 'form' ? rewriteLosses(text) : { comments: 0, anchors: 0, merges: 0 }),
    [view, text],
  );
  // Only in form view. `YamlEditor` renders this note itself, so showing it here
  // as well would say the same thing twice on the same screen — and the form is
  // the view that needs it most, because every control in it is a lens onto the
  // browser's reading of a document the API server reads differently.
  const divergence = useMemo(() => (view === 'form' ? divergenceNote(text) : null), [view, text]);

  // Rule 11.4 on the view switch itself: Form view stays visible and says what
  // it is waiting for, rather than appearing only for the kinds that have one —
  // which would make "why is there no form for this" unanswerable from the
  // screen.
  const formUnavailableReason = validity.empty
    ? 'Paste or type a manifest first: the form is built from the object’s own kind.'
    : !validity.valid
      ? 'The document does not parse, so there is no object to build a form from.'
      : !kind
        ? 'The document has no `kind`, so this console cannot tell which form to show.'
        : !model
          ? `This console has no form for ${kind}. It has one for ${MODELLED_KINDS}. Everything else is created from YAML, which is every field of every kind.`
          : containsCycle(parsed)
            // A YAML anchor that refers to the node containing it parses into a
            // genuinely cyclic object, and `toYaml` expands shared nodes rather
            // than re-emitting the anchor — so the form could read this document
            // and never write it back. Refused up front and named, rather than
            // discovered as a stack overflow during a render that takes the
            // dialog and everything typed into it with it.
            ? 'This document has a YAML anchor that refers to the block containing it. The form would have to write it back out expanded, which never finishes, so it is edited in YAML view only.'
            : null;

  const catalogPartial = catalog.unavailable.length > 0;
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
              ? catalogPartial
                // "We could not look" and "the cluster does not have it" are
                // different sentences, and the banner above lists which groups
                // went unread. Reporting the first as the second is what sends
                // somebody to install an API server they are already running.
                ? `Discovery came back incomplete, so this console cannot tell whether this cluster serves ${kind} at apiVersion ${apiVersion}. The banner above says which API groups went unread.`
                : `This cluster does not serve ${kind} at apiVersion ${apiVersion}.`
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
      description="One Kubernetes object, from the form or as YAML. Preview first — the diff below is the API server's own projection of what creating it would produce."
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
      {/* §11.1. A catalog that came back short is the reason a kind might be
          missing below, so it is stated once, at the top, next to everything
          that depends on it. */}
      <PartialBanner
        unavailable={catalog.unavailable}
        title="The API catalog is incomplete"
        className="admin-confirm__alert"
      />

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
            : catalog.loading
              ? 'Reading what this cluster serves…'
              : catalog.error
                // Not "once it parses as one": the document may be perfect and
                // this console still could not look up where to send it. Naming
                // the wrong thing sends an operator to re-read a manifest that
                // was never the problem.
                ? 'The API catalog could not be read, so where this would be created is not something this console can say.'
                : 'Where this is created is read from the document\'s own apiVersion and kind, once it parses as one.'
        }
      >
        {/* Which of the two namespaces won, said out loud. `_resolve_namespace`
            prefers the document and falls back to the masthead, and the whole
            reason this alert exists is that a create in the wrong namespace is
            invisible in a diff that has no `before` to compare against. */}
        {entry?.namespaced &&
          (docNamespace
            ? `From this document's own metadata.namespace.`
            : namespaceFromMasthead
              ? `From the masthead selector — this document sets no metadata.namespace of its own.`
              : `This document sets no metadata.namespace and no namespace is selected above.`)}
      </Alert>

      {templates.length > 1 && (
        <div className="admin-confirm__views" data-testid="create-starters">
          <span className="admin-confirm__views-label" id="create-starter-label">
            Start from:
          </span>
          <div className="admin-confirm__views-options" role="radiogroup" aria-labelledby="create-starter-label">
            {templates.map((entry) => (
              <Radio
                key={entry.id}
                id={`create-starter-${entry.id}`}
                name="create-starter"
                label={entry.label}
                data-testid={`create-starter-${entry.id}`}
                // Checked follows the document, not the click. A starter the
                // operator asked for and has not confirmed has not replaced
                // anything, and a radio that moved first would be showing them
                // a document they are not looking at.
                isChecked={starterId === entry.id}
                onChange={() => chooseStarter(entry)}
              />
            ))}
          </div>
        </div>
      )}

      {starter?.description && (
        // The sentence a starter would otherwise have written as a YAML comment.
        // It lives here instead because a form edit re-serialises the document
        // and nothing carries a comment across a parse — so a commented starter
        // warns the operator it is about to destroy lines this console wrote,
        // on the first click, before they have typed anything.
        <p className="admin-confirm__views-reason" data-testid="create-starter-description">
          {starter.description}
        </p>
      )}

      {pending && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="create-starter-confirm"
          title={`Starting from "${pending.label}" replaces what is in the editor`}
        >
          <p>
            This document has been edited since it was seeded. Switching starters overwrites it with the
            other one, and there is no undo here. Nothing has been sent to the cluster either way.
          </p>
          <div style={{ display: 'flex', gap: 'var(--admin-gap-sm, 0.5rem)', marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}>
            <Button variant="warning" data-testid="create-starter-replace" onClick={() => applyStarter(pending)}>
              Replace the document
            </Button>
            <Button variant="link" data-testid="create-starter-keep" onClick={() => setPendingId(null)}>
              Keep what I have
            </Button>
          </div>
        </Alert>
      )}

      <div className="admin-confirm__views">
        <span className="admin-confirm__views-label" id="create-view-label">
          Configure via:
        </span>
        {/* The group holds the radios and nothing else, named by the label
            beside it: a `radiogroup` whose children are not all radios is one
            an assistive technology has to guess its way through. */}
        <div className="admin-confirm__views-options" role="radiogroup" aria-labelledby="create-view-label">
          <Tooltip content={formUnavailableReason || 'Edit this object field by field'}>
            <span>
              <Radio
                id="create-view-form"
                name="create-view"
                label="Form view"
                data-testid="create-view-form"
                isChecked={view === 'form'}
                isDisabled={Boolean(formUnavailableReason)}
                onChange={() => setView('form')}
              />
            </span>
          </Tooltip>
          <Radio
            id="create-view-yaml"
            name="create-view"
            label="YAML view"
            data-testid="create-view-yaml"
            isChecked={view === 'yaml'}
            onChange={() => setView('yaml')}
          />
        </div>
      </div>

      {formUnavailableReason && (
        // The reason in text as well as in the tooltip: a disabled control's
        // tooltip is unreachable by exactly the operators most likely to need
        // it, and rule 11.4 asks for the reason, not for a place it could be
        // found.
        <p className="admin-confirm__views-reason" data-testid="create-view-form-reason">
          {formUnavailableReason}
        </p>
      )}

      {ISSUE_ALERTS.map(({ severity, variant, title: issueTitle, testid }) => {
        const matching = issues.filter((issue) => issue.severity === severity);
        if (matching.length === 0) return null;
        return (
          <Alert
            key={severity}
            isInline
            variant={variant}
            className="admin-confirm__alert"
            data-testid={testid}
            data-count={matching.length}
            title={issueTitle}
          >
            <ul className="admin-confirm__warnings">
              {matching.map((issue, index) => (
                <li key={index}>{issue.text}</li>
              ))}
            </ul>
            {severity === 'error' && (
              <p style={{ marginBlockStart: 'var(--admin-gap-sm, 0.5rem)' }}>
                These are local checks, and they do not block the preview: the dry run is the API server’s own
                verdict on this object, and it is the one that counts.
              </p>
            )}
          </Alert>
        );
      })}

      {divergence && (
        // The note's own sentence, as the title and nothing else — the same way
        // `YamlEditor` renders it. Two renderings of one warning that worded it
        // differently would read as two different problems.
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="create-divergence-warning"
          title={divergence.text}
        />
      )}

      {view === 'form' && (losses.comments > 0 || losses.anchors > 0 || losses.merges > 0) && (
        <Alert
          isInline
          variant="warning"
          className="admin-confirm__alert"
          data-testid="create-rewrite-warning"
          title="Editing here rewrites this document from what it parses as"
        >
          <ul className="admin-confirm__warnings">
            {losses.comments > 0 && (
              <li>
                {losses.comments} comment {losses.comments === 1 ? 'line' : 'lines'} would be dropped. Nothing
                carries a comment across a parse, so the first change made here loses them.
              </li>
            )}
            {losses.anchors > 0 && (
              <li>
                {losses.anchors} {losses.anchors === 1 ? 'line carries a YAML anchor or alias' : 'lines carry YAML anchors or aliases'}. They
                are resolved by the parser rather than lost, so the rewrite writes out what they stood for:
                the object is the same, the sharing is not.
              </li>
            )}
            {losses.merges > 0 && (
              <li>
                {losses.merges} merge {losses.merges === 1 ? 'key' : 'keys'} (<code>&lt;&lt;</code>) would be
                spelled out for the same reason.
              </li>
            )}
          </ul>
          Switch to YAML view to keep the document exactly as written.
        </Alert>
      )}

      {view === 'form' && model ? (
        <ObjectForm
          document={parsed}
          model={model}
          unrepresentedPaths={unrepresentedPaths}
          onChange={(next) => setText(toYaml(next))}
        />
      ) : (
        <YamlEditor
          value={text}
          onChange={setText}
          label="New object"
          ariaLabel="YAML to import"
        />
      )}
    </MutationDialog>
  );
}

export default ImportYamlDialog;
