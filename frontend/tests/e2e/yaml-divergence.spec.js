/**
 * The two parsers this console reads a manifest with, held to the same corpus.
 *
 * `backend/tests/test_yaml_scalar_divergence.py` runs the rows below through
 * the real PyYAML — the parser whose reading is what the API server is actually
 * sent. This side runs them through the real js-yaml, and through the mirror
 * schema in `src/components/yamlDivergence.js` that claims to reproduce PyYAML
 * from inside a language that cannot call it.
 *
 * Neither test can see the other's parser, which is the whole reason there is
 * one corpus and two tests rather than two corpora: the file is what makes a
 * drift between the two a failure instead of a difference nobody notices.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';
import yaml from 'js-yaml';

import { CLUSTER_SCHEMA, divergenceNote, divergences } from '../../src/components/yamlDivergence.js';
import { mockApi } from './fixtures.js';

const CORPUS = JSON.parse(
  readFileSync(fileURLToPath(new URL('../../../backend/tests/data/yaml_scalar_corpus.json', import.meta.url)), 'utf8'),
);

/** JSON has no literal for these three, so the corpus spells them. */
const SPELLED = { '@inf': Infinity, '@-inf': -Infinity, '@nan': NaN };
const expected = (value) => (typeof value === 'string' && value in SPELLED ? SPELLED[value] : value);

const read = (scalar, options) => yaml.load(`value: ${scalar}\n`, options).value;

const POD = `apiVersion: v1
kind: Pod
metadata:
  name: example
  labels:
    app: example
spec:
  containers:
    - name: example
      image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
      ports:
        - containerPort: 8080
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
`;

test.describe('the corpus, from the browser side', () => {
  for (const row of CORPUS.rows) {
    test(`js-yaml reads \`${row.scalar}\` the way the corpus records`, () => {
      expect(read(row.scalar)).toEqual(expected(row.browser));
    });

    test(`the mirror reads \`${row.scalar}\` the way PyYAML does`, () => {
      // The claim this whole module rests on. Its other half is the backend
      // test, which runs the same string through the parser being mirrored.
      expect(read(row.scalar, { schema: CLUSTER_SCHEMA })).toEqual(expected(row.cluster));
    });
  }
});

test.describe('what the warning finds', () => {
  test('every divergent scalar in the corpus is reported, and no other', () => {
    for (const row of CORPUS.rows) {
      const found = divergences(`apiVersion: v1\nkind: ConfigMap\nvalue: ${row.scalar}\n`);
      expect(found.map((entry) => entry.path.join('.')), `\`${row.scalar}\``).toEqual(
        row.diverges ? ['value'] : [],
      );
    }
  });

  test('quoting settles it, which is what the warning tells the operator to do', () => {
    // The advice has to be true or it is worse than silence: an operator who
    // quotes a value on this console's say-so and gets the same warning back
    // has learnt that the warning is noise.
    for (const row of CORPUS.rows.filter((entry) => entry.diverges)) {
      expect(divergences(`value: "${row.scalar}"\n`), `\`${row.scalar}\``).toEqual([]);
      expect(divergences(`value: '${row.scalar}'\n`), `\`${row.scalar}\``).toEqual([]);
    }
  });

  test('a divergent word inside a block scalar is text, and is not reported', () => {
    // The case a pattern over the source gets wrong, and the reason this is two
    // parses rather than a regex. A ConfigMap holding a config file is the
    // common shape, not the exotic one.
    const found = divergences(`apiVersion: v1
kind: ConfigMap
metadata:
  name: example
data:
  app.conf: |
    debug off
    off
    010
`);
    expect(found).toEqual([]);
  });

  test('a key the two parsers read differently is reported as a key', () => {
    // `off:` is a ConfigMap data key called "False" by the time it reaches the
    // cluster, and no control in the form view would show that.
    const found = divergences('apiVersion: v1\nkind: ConfigMap\ndata:\n  off: enabled\n');
    expect(found).toHaveLength(1);
    expect(found[0].isKey).toBe(true);
    expect(found[0].browser).toBe('off');
    expect(found[0].cluster).toBe('false');
  });

  test('a document that does not parse is left to the parse error', () => {
    // One sentence about a missing colon on line 3 is what the operator needs;
    // a second about scalar resolution in a document that has no scalars yet is
    // noise on top of it.
    expect(divergences('a:\n  b: c\n d: e\n')).toEqual([]);
    expect(divergences('')).toEqual([]);
    expect(divergences(null)).toEqual([]);
  });

  test('a document whose anchor contains itself is walked once, not forever', () => {
    // js-yaml resolves a self-referential anchor into a genuinely cyclic object
    // rather than refusing it, and this runs inside a render — so an unguarded
    // walk costs the operator the dialog and everything typed into it, to an
    // error boundary that can name neither the document nor the anchor.
    const cyclic = `apiVersion: v1
kind: ConfigMap
metadata: &meta
  name: example
  self: *meta
data:
  debug: off
`;
    // Returns at all, and still finds the divergence outside the cycle.
    expect(divergences(cyclic).map((entry) => entry.path.join('.'))).toEqual(['data.debug']);
  });

  test('an anchor used twice is compared twice, not skipped the second time', () => {
    // The reason the guard tracks ancestors rather than every node it has seen:
    // a shared node is ordinary YAML, and reporting only its first appearance
    // would name one of two paths an operator has to fix.
    const shared = `spec:
  first: &flag off
  second: *flag
`;
    expect(divergences(shared).map((entry) => entry.path.join('.'))).toEqual(['spec.first', 'spec.second']);
  });

  test('the note names the path and both readings, and counts the rest', () => {
    const note = divergenceNote('spec:\n  a: off\n  b: 010\n  c: 8:30\n  d: yes\n  e: on\n  f: 1_000\n');
    expect(note.severity).toBe('warning');
    expect(note.text).toContain('`spec.a` is read here as the text "off" and will be sent as the boolean false');
    expect(note.text).toContain('`spec.b` is read here as the number 10 and will be sent as the number 8');
    // Four named, then counted rather than listed — and said, rather than the
    // list simply stopping.
    expect(note.text).toContain('and 2 more like it');
    expect(note.text).toContain('Quoting the value settles it');
  });

  test('an ordinary manifest produces nothing at all', () => {
    // Quantities, image tags and ports are what a manifest is mostly made of.
    // A warning that fired on those is one that gets dismissed on the day it is
    // right.
    expect(divergenceNote(POD)).toBeNull();
  });
});

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

test.describe('where the warning appears', () => {
  test('the editor says so, wherever a manifest is edited', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/');
    // Scoped before the dialog opens, because the masthead selector is behind
    // the modal once it is. A ConfigMap is namespaced, and the point below is
    // that this warning is not what holds Preview back.
    await page.getByTestId('namespace-selector').click();
    await page.getByRole('menuitem', { name: 'prod', exact: true }).click();

    await page.getByTestId('import-yaml-button').click();
    await page
      .getByTestId('yaml-editor-input')
      .fill('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: example\ndata:\n  debug: off\n');

    const note = page.getByTestId('yaml-editor-note').filter({ hasText: 'read this document differently' });
    await expect(note).toContainText('`data.debug` is read here as the text "off"');
    await expect(note).toContainText('will be sent as the boolean false');

    // And it does not block. The document is legal YAML either way and the dry
    // run is what decides whether the cluster wants it.
    await expect(page.getByTestId('mutation-preview')).toBeEnabled();
  });

  test('the form view says so too, because its controls are the browser’s reading', async ({ page }) => {
    // The label check written for exactly this class — "a label value that
    // parsed as a number or a boolean" — cannot see this one, because to
    // js-yaml it is the string "off". This warning is what covers it.
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/');
    await page.getByTestId('import-yaml-button').click();
    await page.getByTestId('yaml-editor-input').fill(`apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
  namespace: prod
  labels:
    managed: off
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
          image: nginx
`);
    await page.getByTestId('create-view-form').check();

    await expect(page.getByTestId('create-divergence-warning')).toContainText(
      '`metadata.labels.managed` is read here as the text "off"',
    );
    // Once, not twice: the editor is not mounted in this view.
    await expect(page.getByTestId('yaml-editor-note')).toHaveCount(0);
  });

  test('a clean manifest gets no warning in either view', async ({ page }) => {
    await mockApi(page, { preflight: ALLOW_ALL });
    await page.goto('/pods');
    await page.getByRole('button', { name: 'Create Pod' }).click();
    await expect(page.getByTestId('create-form')).toBeVisible();
    await expect(page.getByTestId('create-divergence-warning')).toHaveCount(0);

    await page.getByTestId('create-view-yaml').check();
    await expect(
      page.getByTestId('yaml-editor-note').filter({ hasText: 'read this document differently' }),
    ).toHaveCount(0);
  });
});
