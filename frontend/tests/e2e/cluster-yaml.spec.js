/**
 * The console's reading of a manifest, held to the parser it is a copy of.
 *
 * `backend/tests/test_yaml_scalar_reading.py` runs the rows below through the
 * real backend — whose reading is what the API server is actually sent. This
 * side runs them through the mirror schema in `src/components/clusterYaml.js`,
 * which claims to reproduce `app/yaml_dialect.py` from inside a language that
 * cannot call it, and through js-yaml's own default schema, which is the YAML
 * 1.2 reading the warning is about.
 *
 * Neither test can see the other's parser, which is the whole reason there is
 * one corpus and two tests rather than two corpora: the file is what makes a
 * drift between the two a failure instead of a difference nobody notices.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';
import yaml from 'js-yaml';

import {
  CLUSTER_SCHEMA,
  divergenceNote,
  divergences,
  load,
  loadAll,
  toYaml,
} from '../../src/components/clusterYaml.js';
import { formModelFor, localIssues } from '../../src/components/objectFormModel.js';
import { mockApi } from './fixtures.js';

const CORPUS = JSON.parse(
  readFileSync(fileURLToPath(new URL('../../../backend/tests/data/yaml_scalar_corpus.json', import.meta.url)), 'utf8'),
);

/** JSON has no literal for these three, so the corpus spells them. */
const SPELLED = { '@inf': Infinity, '@-inf': -Infinity, '@nan': NaN };
const expected = (value) => (typeof value === 'string' && value in SPELLED ? SPELLED[value] : value);

const readPlain = (scalar) => yaml.load(`value: ${scalar}\n`).value;
const readSent = (scalar) => load(`value: ${scalar}\n`).value;

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
    test(`js-yaml's own schema reads \`${row.scalar}\` the way the corpus records`, () => {
      expect(readPlain(row.scalar)).toEqual(expected(row.yaml12));
    });

    test(`this console reads \`${row.scalar}\` the way the backend does`, () => {
      // The claim the whole module rests on. Its other half is the backend
      // test, which runs the same string through the parser being mirrored.
      expect(readSent(row.scalar)).toEqual(expected(row.sent));
    });

    test(`a form edit round-trips \`${row.scalar}\` unchanged`, () => {
      // The reason `toYaml` dumps against this schema and not js-yaml's
      // default. A form edit rewrites the whole document, so every scalar it
      // did not touch has to come back meaning what it meant — and the value
      // that catches a default dump is the string `"1_000"`, which js-yaml
      // writes bare because YAML 1.2 has no underscore digits, and which this
      // console would then send as the number 1000.
      const value = readSent(row.scalar);
      const text = toYaml({ value });
      expect(load(text).value, `dumped as \`${text.trim()}\``).toEqual(value);
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

  test('a key read differently is reported as a key', () => {
    // `off:` is a ConfigMap data key called "false" by the time it reaches the
    // cluster, and no control in the form view would show that.
    const found = divergences('apiVersion: v1\nkind: ConfigMap\ndata:\n  off: enabled\n');
    expect(found).toHaveLength(1);
    expect(found[0].isKey).toBe(true);
    expect(found[0].looksLike).toBe('off');
    expect(found[0].sent).toBe('false');
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
    expect(note.text).toContain('`spec.a` looks like the text "off" and will be sent as the boolean false');
    expect(note.text).toContain('`spec.b` looks like the number 10 and will be sent as the number 8');
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

test.describe('the four letters', () => {
  // ADR-0010. `kubectl` reads `y`, `Y`, `n` and `N` as booleans and this console
  // did not, and the warning could not see it: both parsers here called them
  // text, so there was nothing to compare. Closing the gap in what is *sent* is
  // what gives the warning something to find.
  for (const [scalar, sent] of [
    ['y', true],
    ['Y', true],
    ['n', false],
    ['N', false],
  ]) {
    test(`\`${scalar}\` is sent as ${sent}, and the editor says so`, () => {
      expect(load(`value: ${scalar}\n`).value).toBe(sent);

      const note = divergenceNote(`apiVersion: v1\nkind: ConfigMap\ndata:\n  verbose: ${scalar}\n`);
      expect(note.text).toContain(
        `\`data.verbose\` looks like the text "${scalar}" and will be sent as the boolean ${sent}`,
      );
    });
  }

  test('two letters are two letters', () => {
    // The resolver is anchored. `kubectl` reads every one of these as text and
    // so does YAML 1.2, so a wider mirror would not close a gap — it would open
    // one, and the warning would fire on a document nobody disagrees about.
    for (const scalar of ['yy', 'Ye', 'ny', 'yn', 'Nn', 'no_quotes']) {
      expect(load(`value: ${scalar}\n`).value, scalar).toBe(scalar);
      expect(divergences(`value: ${scalar}\n`), scalar).toEqual([]);
    }
  });

  test('a string that is only the letter comes back as the letter', () => {
    // js-yaml quotes all four on dump — it treats YAML 1.1's booleans as unsafe
    // plain scalars — so this passes today. It is asserted because the day it
    // stops, a form edit turns somebody's ConfigMap value into a boolean.
    const text = toYaml({ a: 'y', b: 'Y', c: 'n', d: 'N' });
    expect(load(text)).toEqual({ a: 'y', b: 'Y', c: 'n', d: 'N' });
  });
});

test.describe('what the one reading buys', () => {
  test('the local check written for this class can finally see it', () => {
    // The point of ADR-0009 in one assertion. `version: yes` is a label value
    // that reaches the API server as a boolean and gets the whole object
    // refused with an unmarshalling error naming no field. The check for it has
    // been in `localIssues` all along and could not see this one, because to
    // js-yaml it was the harmless string "yes" — so the console was reasoning
    // carefully about a document it was not going to send.
    const document = load(`apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
  labels:
    managed: off
    version: yes
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
    expect(document.metadata.labels).toEqual({ managed: false, version: true });

    const issues = localIssues(document, formModelFor(document.apiVersion, document.kind)).map(
      (issue) => issue.text,
    );
    expect(issues).toContain(
      'metadata.labels.version is the boolean true. Label and annotation values must be strings — quote it.',
    );
    expect(issues).toContain(
      'metadata.labels.managed is the boolean false. Label and annotation values must be strings — quote it.',
    );
  });
});

test.describe('the reader and the writer are the same one', () => {
  test('a multi-document stream is read with the schema too', () => {
    // `loadAll` is what the editor validates with, and a second document read
    // by a different schema than the first is the bug this module exists to
    // make impossible.
    const documents = loadAll('a: off\n---\nb: 010\n');
    expect(documents).toEqual([{ a: false }, { b: 8 }]);
  });

  test('a string that would come back as something else is quoted', () => {
    // The dump side of the same reading. All four are values a manifest really
    // holds: a label, a digit-grouped annotation, a ConfigMap key.
    const text = toYaml({ a: 'off', b: '1_000', c: '8:30', d: '010' });
    expect(load(text)).toEqual({ a: 'off', b: '1_000', c: '8:30', d: '010' });
  });

  test('an image reference is not folded onto two lines', () => {
    // `lineWidth: -1`, kept through the move: js-yaml folds at 80 columns by
    // default and the parser puts the fold back as a space, which is a valid
    // document holding a different image.
    const image = 'registry.example.com/team/service@sha256:2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00112';
    const text = toYaml({ spec: { containers: [{ name: 'app', image }] } });
    expect(text.split('\n').some((line) => line.includes(image))).toBe(true);
    expect(load(text).spec.containers[0].image).toBe(image);
  });

  test('the schema can write a boolean at all', () => {
    // Replacing a type by tag replaces both of its halves, and a schema that
    // kept only the resolver throws `unacceptable kind of an object to dump
    // [object Boolean]` the first time the form view serialises a document —
    // which is every form edit of every manifest with a boolean in it.
    expect(toYaml({ a: true, b: 1, c: 1.5 }).trim()).toBe('a: true\nb: 1\nc: 1.5');
  });

  test('the schema is the one thing exported for a second opinion', () => {
    // `divergences` needs js-yaml's default schema on one side, so the mirror
    // has to be nameable. Asserted so that removing the export is a failing
    // test rather than a silently different comparison.
    expect(yaml.load('a: off\n', { schema: CLUSTER_SCHEMA })).toEqual({ a: false });
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

    const note = page.getByTestId('yaml-editor-note').filter({ hasText: 'YAML readers disagree about' });
    await expect(note).toContainText('`data.debug` looks like the text "off"');
    await expect(note).toContainText('will be sent as the boolean false');

    // And it does not block. The document is legal YAML either way and the dry
    // run is what decides whether the cluster wants it.
    await expect(page.getByTestId('mutation-preview')).toBeEnabled();
  });

  test('the form view shows the value the cluster will be sent', async ({ page }) => {
    // Before ADR-0009 this control read "off" and the cluster was sent `false`,
    // and the check written for exactly this class — "a label value that parsed
    // as a number or a boolean" — could not see it, because to js-yaml it was
    // the string "off". Now the form is a lens onto the object being written.
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
      '`metadata.labels.managed` looks like the text "off"',
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
      page.getByTestId('yaml-editor-note').filter({ hasText: 'YAML readers disagree about' }),
    ).toHaveCount(0);
  });
});
