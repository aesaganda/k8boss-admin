/**
 * The create dialog's Form view (§11.9).
 *
 * The feature is OpenShift's "Configure via: Form view / YAML view", and every
 * assertion here is about the one property that separates this from OpenShift's:
 *
 *   **The document is the source of truth and the form is a projection of it.**
 *
 * OpenShift's console shows a standing note saying that *some fields may not be
 * represented in this form view*, and never says which. So the two tests that
 * matter below are `a field the form does not show survives an edit made in the
 * form` and `the form names, by path, what it is not showing` — a form that
 * quietly dropped a hand-written `spec.affinity` at the moment somebody renamed
 * the object would produce a diff that is a correct projection of a manifest
 * nobody wrote, and the operator would approve it.
 *
 * The rest are the states a convenient implementation collapses: an empty
 * numeric field is an *absent* key and never `0`; a selector that cannot match
 * its own pod template is reported rather than repaired behind the operator's
 * back; and the comments a form edit is about to destroy are counted while that
 * is still actionable.
 */
import { expect, test } from '@playwright/test';

import { containerControlTestIds } from '../../src/components/objectForm.js';
import { FIXTURES, mockApi } from './fixtures.js';

/** Answer every §9 check as allowed, so RBAC is not what is under test. */
const ALLOW_ALL = (checks) =>
  checks.map((check) => ({
    verb: check.verb,
    group: check.group,
    resource: check.resource,
    namespace: check.namespace ?? null,
    subresource: check.subresource ?? null,
    allowed: true,
    reason: '',
    evaluationError: null,
    hint: null,
  }));

const DEPLOYMENT = `apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
  namespace: prod
  labels:
    app: example
spec:
  replicas: 1
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      containers:
        - name: example
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
`;

/** Open the masthead's dialog with `text` in the editor, in YAML view. */
async function openImport(page, text, options = {}) {
  await mockApi(page, { preflight: ALLOW_ALL, ...options });
  await page.goto('/');
  await page.getByTestId('import-yaml-button').click();
  if (text) await page.getByTestId('yaml-editor-input').fill(text);
  return page.getByTestId('mutation-dialog');
}

/** The same, then switched to the form. */
async function openForm(page, text = DEPLOYMENT, options = {}) {
  const dialog = await openImport(page, text, options);
  await page.getByTestId('create-view-form').check();
  await expect(page.getByTestId('create-form')).toBeVisible();
  return dialog;
}

/** The manifest as the YAML view currently holds it. */
async function yamlText(page) {
  await page.getByTestId('create-view-yaml').check();
  return page.getByTestId('yaml-editor-input').inputValue();
}

test.describe('the create dialog form view', () => {
  test('a create seeded with a kind opens in the form; the masthead paste does not', async ({ page }) => {
    // The two entry points differ in exactly one thing — whether a document was
    // seeded — and that is what decides the opening view. Nothing switches the
    // view on the operator afterwards.
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/pods');
    await page.getByRole('button', { name: 'Create Pod' }).click();
    await expect(page.getByTestId('create-form')).toBeVisible();
    await expect(page.getByTestId('create-view-form')).toBeChecked();

    // The masthead's "+" has no document, so there is no kind, so there is no
    // form — offered and disabled with the reason (rule 11.4) rather than
    // absent, which would make "why is there no form" unanswerable.
    await openImport(page, '');
    await expect(page.getByTestId('yaml-editor')).toBeVisible();
    await expect(page.getByTestId('create-view-form')).toBeDisabled();
    await expect(page.getByTestId('create-view-form-reason')).toContainText('Paste or type a manifest first');

    // Pasting one enables the control. It does not press it.
    await page.getByTestId('yaml-editor-input').fill(DEPLOYMENT);
    await expect(page.getByTestId('create-view-form')).toBeEnabled();
    await expect(page.getByTestId('create-view-yaml')).toBeChecked();
    await expect(page.getByTestId('yaml-editor')).toBeVisible();
  });

  test('a kind with no form says so instead of offering a partial one', async ({ page }) => {
    await openImport(page, 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\n  namespace: prod\ndata:\n  a: b\n');

    await expect(page.getByTestId('create-view-form')).toBeDisabled();
    await expect(page.getByTestId('create-view-form-reason')).toContainText('no form for ConfigMap');
    // And the YAML path is untouched: the object is still creatable.
    await expect(page.getByTestId('mutation-preview')).toBeEnabled();
  });

  test('a field the form does not show survives an edit made in the form', async ({ page }) => {
    // The assertion this whole feature exists for. `spec.affinity` and the
    // annotation are things no control here can reach; renaming the object must
    // not be the moment they disappear.
    const handWritten = DEPLOYMENT.replace(
      '  replicas: 1\n',
      `  replicas: 1
  revisionHistoryLimit: 3
`,
    ).replace(
      '    spec:\n      containers:',
      `    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: topology.kubernetes.io/zone
                    operator: In
                    values:
                      - eu-west-1a
      containers:`,
    );

    await openForm(page, handWritten);
    await page.getByTestId('create-name').fill('checkout');

    const text = await yamlText(page);
    expect(text).toContain('name: checkout');
    expect(text).toContain('topology.kubernetes.io/zone');
    expect(text).toContain('eu-west-1a');
    expect(text).toContain('revisionHistoryLimit: 3');
  });

  test('the form names, by path, every field it is not showing', async ({ page }) => {
    await openForm(page, DEPLOYMENT.replace('  replicas: 1\n', '  replicas: 1\n  paused: false\n  minReadySeconds: 5\n'));

    const list = page.getByTestId('create-unrepresented');
    await expect(list).toContainText('spec.paused');
    await expect(list).toContainText('spec.minReadySeconds');
    // And the things it *does* show are not in the list, or the list would be
    // noise nobody reads.
    await expect(list).not.toContainText('spec.replicas');
    await expect(list).not.toContainText('metadata.name');
  });

  test('a document the form covers completely says nothing is hidden', async ({ page }) => {
    await openForm(page, DEPLOYMENT);
    await expect(page.getByTestId('create-unrepresented-none')).toBeVisible();
  });

  test('every container field the model counts as covered has a control on screen', async ({ page }) => {
    // `CONTAINER_COVERAGE` is a claim made in objectForm.js about a renderer in
    // another file, and it is the one input to the "not shown" list that is
    // written down rather than derived. If a control is deleted and its pattern
    // is not, its field moves silently into the set the form hides while saying
    // it hides nothing — so the two are checked against each other here.
    await openForm(page, DEPLOYMENT);
    for (const testid of containerControlTestIds(0)) {
      await expect(page.getByTestId(testid)).toBeVisible();
    }

    // And a second container carries its own set rather than sharing the
    // first's ids — two inputs with one id makes a label point at whichever the
    // browser found first.
    await page.getByTestId('create-add-container').click();
    for (const testid of containerControlTestIds(1)) {
      await expect(page.getByTestId(testid)).toHaveCount(1);
    }
  });

  test('clearing a number removes the key rather than writing zero', async ({ page }) => {
    // Rule 11.2 on the way out. `replicas: 0` is a Deployment with no pods and
    // no error anywhere on screen; absent is a Deployment with one.
    await openForm(page, DEPLOYMENT);
    await page.getByTestId('create-replicas').fill('');

    const cleared = await yamlText(page);
    expect(cleared).not.toContain('replicas');

    await page.getByTestId('create-view-form').check();
    await page.getByTestId('create-replicas').fill('0');
    expect(await yamlText(page)).toContain('replicas: 0');
  });

  test('a number box holding something that is not a number says so', async ({ page }) => {
    // `input[type=number]` reads back empty for an unparseable entry while the
    // box goes on showing it, so without saying this the field would vanish
    // from the manifest while the operator looked at the figure they typed.
    await openForm(page, DEPLOYMENT);
    await page.getByTestId('create-replicas').fill('3.5');

    await expect(page.getByTestId('create-replicas-bad')).toContainText('not a whole number');
    expect(await yamlText(page)).not.toContain('replicas');
  });

  test('a control whose field holds the wrong shape says so instead of showing it empty', async ({
    page,
  }) => {
    // `labels: production` is a string where a block of keys belongs. An empty
    // mapping editor over it would say the field is empty while the document
    // says otherwise, and the first row added would overwrite the string.
    await openForm(page, DEPLOYMENT.replace('  labels:\n    app: example\n', '  labels: production\n'));

    const labels = page.getByTestId('create-labels-add');
    await expect(labels).toBeDisabled();
    await expect(page.getByTestId('create-section-metadata')).toContainText(
      'this control expects a set of key/value pairs',
    );
    // And it is still there afterwards: the form left it alone.
    expect(await yamlText(page)).toContain('labels: production');
  });

  test('adding the first row of an empty block does not delete the block', async ({ page }) => {
    // A row with no key yet is not a key, so the editor publishes an empty
    // mapping — and unsetting on that would delete `spec.podSelector`, which
    // the default-deny template writes on purpose, at the moment the operator
    // pressed Add.
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/network');
    await page.getByRole('tab', { name: 'Network Policies' }).click();
    await page.getByRole('button', { name: 'New network policy…' }).click();
    await expect(page.getByTestId('create-form')).toBeVisible();

    await page.getByTestId('create-podSelector-add').click();
    // The row itself is not in the document — a row with no key is not a key —
    // so switching views discards it. What must survive is the block it was
    // added to.
    expect(await yamlText(page)).toContain('podSelector: {}');

    await page.getByTestId('create-view-form').check();
    await page.getByTestId('create-podSelector-add').click();
    await page.getByTestId('create-podSelector-key-0').fill('app');
    await page.getByTestId('create-podSelector-value-0').fill('checkout');
    expect(await yamlText(page)).toContain('app: checkout');
  });

  test('the alert says which of the two namespaces won', async ({ page }) => {
    // A create in the wrong namespace is invisible in a diff with no `before`
    // to compare against, which is the whole reason this alert exists.
    await openImport(page, DEPLOYMENT);
    await expect(page.getByTestId('import-yaml-target')).toContainText('in namespace "prod"');
    await expect(page.getByTestId('import-yaml-target')).toContainText("document's own metadata.namespace");

    await page.getByTestId('yaml-editor-input').fill(DEPLOYMENT.replace('  namespace: prod\n', ''));
    await expect(page.getByTestId('import-yaml-target')).toContainText('no namespace is selected above');
  });

  test('a selector that cannot match its own pods is reported, and repaired only on request', async ({
    page,
  }) => {
    await openForm(page, DEPLOYMENT);

    // Relabelling the pod template leaves the selector selecting nothing. The
    // API server refuses the object outright, so this is a state an operator
    // can otherwise only discover from a rejected write.
    await page.getByTestId('create-podLabels-value-0').fill('checkout');
    await expect(page.getByTestId('create-issues-error')).toContainText('selector does not match the pod labels');

    // Nothing was rewritten behind them: the selector still says what it said.
    expect(await yamlText(page)).toContain('matchLabels:\n      app: example');

    await page.getByTestId('create-view-form').check();
    await page.getByTestId('create-selector-repair').click();
    await expect(page.getByTestId('create-issues-error')).toHaveCount(0);
    expect(await yamlText(page)).toContain('app: checkout');
  });

  test('a selector written as matchExpressions is not called empty', async ({ page }) => {
    // The form's control edits matchLabels, but the API server's rule is about
    // the whole LabelSelector — a matchExpressions-only selector is legal and
    // accepted. Calling it "empty" under a heading that says the API server
    // will refuse the document is a flat, confident, false claim, contradicted
    // by the dry run on the same screen.
    const expressions = DEPLOYMENT.replace(
      '  selector:\n    matchLabels:\n      app: example\n',
      `  selector:
    matchExpressions:
      - key: app
        operator: In
        values:
          - example
`,
    );
    await openForm(page, expressions);
    await expect(page.getByTestId('create-issues-error')).toHaveCount(0);

    // The term is still checked against the pod labels — it just has to be read
    // correctly to be checked at all.
    await page.getByTestId('create-podLabels-value-0').fill('checkout');
    await expect(page.getByTestId('create-issues-error')).toContainText('is not satisfied by the pod labels');

    // And the part of the selector no control shows is named rather than hidden.
    await expect(page.getByTestId('create-unrepresented')).toContainText('spec.selector.matchExpressions');
  });

  test('removing a container does not leave its command in the surviving row', async ({ page }) => {
    // The container rows are index-keyed and their Command box holds its own
    // text, so without a remount the survivor goes on showing the deleted
    // container's command — and the next keystroke writes it onto the survivor.
    await openForm(
      page,
      DEPLOYMENT.replace(
        '        - name: example\n          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine\n',
        `        - name: sidecar
          image: busybox
          command:
            - /bin/sh
            - "-c"
            - tail -f /dev/null
        - name: app
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
          command:
            - nginx
            - "-g"
            - daemon off;
`,
      ),
    );

    await expect(page.getByTestId('create-container-command-0')).toHaveValue(/tail -f/);
    await page.getByTestId('create-container-remove-0').click();

    await expect(page.getByTestId('create-container-name-0')).toHaveValue('app');
    await expect(page.getByTestId('create-container-command-0')).toHaveValue(/nginx/);
    await expect(page.getByTestId('create-container-command-0')).not.toHaveValue(/tail -f/);

    // And a keystroke in that box writes the surviving container's command,
    // not the deleted one's.
    await page.getByTestId('create-container-image-0').fill('nginx:1.30');
    const text = await yamlText(page);
    expect(text).toContain('nginx');
    expect(text).not.toContain('tail -f');
  });

  test('with no pod labels the repair runs the other way rather than emptying the selector', async ({
    page,
  }) => {
    // "Copy the pod labels into the selector" with no pod labels copies nothing
    // — and a selector is immutable once the object exists, so a button that
    // deleted it would not be undoable.
    await openForm(
      page,
      DEPLOYMENT.replace('    metadata:\n      labels:\n        app: example\n', '    metadata: {}\n'),
    );

    await expect(page.getByTestId('create-selector-repair')).toHaveCount(0);
    await page.getByTestId('create-selector-repair-labels').click();

    const text = await yamlText(page);
    expect(text).toContain('app: example');
    expect(text).not.toContain('matchLabels: {}');
  });

  test('a self-referential anchor is refused by the form rather than crashing it', async ({ page }) => {
    // js-yaml resolves this into a genuinely cyclic object; walking it or
    // dumping it never returns. Named up front, in YAML view, with the document
    // still in the box.
    await openImport(
      page,
      `apiVersion: apps/v1
kind: Deployment
metadata: &meta
  name: example
  namespace: prod
  annotations:
    self: *meta
spec:
  replicas: 1
`,
    );

    await expect(page.getByTestId('create-view-form')).toBeDisabled();
    await expect(page.getByTestId('create-view-form-reason')).toContainText('refers to the block containing it');
    await expect(page.getByTestId('yaml-editor-input')).toHaveValue(/self: \*meta/);
  });

  test('the comments a form edit would destroy are counted before it happens', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/network');
    await page.getByRole('tab', { name: 'Network Policies' }).click();
    await page.getByRole('button', { name: 'New network policy…' }).click();

    await expect(page.getByTestId('create-form')).toBeVisible();
    await expect(page.getByTestId('create-rewrite-warning')).toContainText('3 comment lines would be dropped');

    // What those comments said is on the form as help text and as the two
    // statements below, which is why losing them costs nothing an operator was
    // reading. Both are the sentences the policy page uses.
    await expect(page.getByTestId('create-issues-info')).toContainText('every pod in the namespace');
    await expect(page.getByTestId('create-issues-info')).toContainText('denies all inbound traffic');

    // And `podSelector: {}` — an empty block the pod-selector control owns — is
    // not listed as a field the form does not touch. On the first policy
    // anybody creates, that would be wrong about its single most consequential
    // field.
    await expect(page.getByTestId('create-unrepresented-none')).toBeVisible();

    // And the warning clears once the rewrite has happened, rather than
    // standing there describing something already done.
    await page.getByTestId('create-name').fill('deny-all');
    await expect(page.getByTestId('create-rewrite-warning')).toHaveCount(0);
  });

  test('an anchor is reported as expanded, not as lost', async ({ page }) => {
    // The other half of what lives in the text rather than in the object. An
    // anchor is resolved by the parser, so the rewrite spells out what it stood
    // for — the object is identical and the document is not, and calling that
    // "dropped" would be as wrong as saying nothing.
    await openForm(
      page,
      `apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
  namespace: prod
  labels: &labels
    app: example
spec:
  replicas: 1
  selector:
    matchLabels: *labels
  template:
    metadata:
      labels: *labels
    spec:
      containers:
        - name: example
          image: nginx
`,
    );

    const warning = page.getByTestId('create-rewrite-warning');
    await expect(warning).toContainText('anchors or aliases');
    await expect(warning).not.toContainText('comment');

    // And the expansion is exactly that: three copies of what the anchor held,
    // and a document that still means the same thing.
    await page.getByTestId('create-name').fill('checkout');
    const text = await yamlText(page);
    expect(text.match(/app: example/g)).toHaveLength(3);
    expect(text).not.toContain('&labels');
    expect(text).not.toContain('*labels');
  });

  test('what the form produced is what is sent, and only the confirm claims a write', async ({ page }) => {
    const creates = [];
    await mockApi(page, { preflight: ALLOW_ALL, resourceCreates: creates });
    await page.goto('/');
    await page.getByTestId('import-yaml-button').click();
    await page.getByTestId('yaml-editor-input').fill(DEPLOYMENT);
    await page.getByTestId('create-view-form').check();

    await page.getByTestId('create-name').fill('checkout');
    await page.getByTestId('create-container-image-0').fill('registry.example:5000/checkout:1.4.2');

    await page.getByTestId('mutation-preview').click();
    await expect(page.getByTestId('mutation-confirm')).toBeVisible();

    // §11.3: the dry run goes first, and it carries the manifest the form
    // built — not a form model the backend would have to reassemble.
    expect(creates).toHaveLength(1);
    expect(creates[0].body.dryRun).toBe(true);
    expect(creates[0].body.yaml).toContain('name: checkout');
    expect(creates[0].body.yaml).toContain('registry.example:5000/checkout:1.4.2');
    expect(creates[0].plural).toBe('deployments');

    // The masthead's dialog closes on success and lands on the object it just
    // created, the way `Explorer`'s own catalog click does — so the evidence
    // that the write happened is the route, not a summary panel that is
    // unmounted before it can be read.
    await page.getByTestId('mutation-confirm').click();
    await expect(page.getByTestId('mutation-dialog')).toHaveCount(0);
    await expect(page).toHaveURL(/\/explorer\/apps\/v1\/deployments\?name=checkout&namespace=prod$/);

    expect(creates).toHaveLength(2);
    expect(creates[1].body.dryRun).toBe(false);
    // The two calls carry the same manifest: nothing is recomputed between the
    // diff the operator read and the write they approved.
    expect(creates[1].body.yaml).toBe(creates[0].body.yaml);
  });

  test('a kind missing from an incomplete catalog is not reported as one the cluster lacks', async ({
    page,
  }) => {
    // §11.1 at the point it decides whether an object can be created at all.
    // Discovery that could not read `apps` returns a catalog with no Deployment
    // in it, and "this cluster does not serve Deployment" would send an
    // operator to install what they are already running.
    await openImport(page, DEPLOYMENT, {
      catalog: {
        ...FIXTURES.catalog,
        items: FIXTURES.catalog.items.filter((item) => item.group !== 'apps'),
        partial: true,
        unavailable: [{ group: 'apps', version: 'v1', resource: null, namespace: null, reason: 'unreachable', detail: 'the apps APIService did not answer' }],
      },
    });

    await expect(page.getByTestId('partial-banner').first()).toBeVisible();
    await expect(page.getByTestId('mutation-preview-disabled-reason')).toContainText(
      'Discovery came back incomplete',
    );
  });

  test('a DaemonSet form offers no replica count, and says why', async ({ page }) => {
    await openForm(
      page,
      DEPLOYMENT.replace('kind: Deployment', 'kind: DaemonSet').replace('  replicas: 1\n', ''),
    );

    await expect(page.getByTestId('create-replicas')).toHaveCount(0);
    await expect(page.getByTestId('create-section-daemonset')).toContainText('no replica count');
  });

  test('a Job with no restartPolicy is caught locally, and the select says what is missing', async ({
    page,
  }) => {
    // The API server defaults an unset `restartPolicy` to Always and then
    // refuses the object for it, so a manifest that never mentions the field
    // fails for a value nobody wrote.
    await openForm(
      page,
      `apiVersion: batch/v1
kind: Job
metadata:
  name: example
  namespace: prod
spec:
  template:
    spec:
      containers:
        - name: example
          image: busybox
`,
    );

    await expect(page.getByTestId('create-issues-error')).toContainText('no restartPolicy');
    // The select shows the document's actual state rather than rendering its
    // first option, which would show a setting the manifest does not contain.
    await expect(page.getByTestId('create-restartPolicy')).toHaveValue('');
  });
});
