/**
 * Rule 11.11 — what every starter this console ships has to be true of.
 *
 * These run in Node with no browser, because what they check is the content of
 * a module and not the behaviour of a screen. That is the point: a starter is
 * the one manifest in this product that nobody wrote for their own cluster, so
 * a wrong field in one is a defect the console hands to every operator who
 * presses the button — and it would otherwise be found by the API server, after
 * the click, in a dialog that had already promised a valid starting point.
 */
import { expect, test } from '@playwright/test';
import yaml from 'js-yaml';

import { rewriteLosses } from '../../src/components/objectForm.js';
import { allStarters, skeletonStarter, templatesFor } from '../../src/components/templates.js';

/**
 * Scalars js-yaml and PyYAML resolve differently, unquoted.
 *
 * The browser parses with js-yaml 4 (YAML 1.2 core schema) and the backend with
 * PyYAML (YAML 1.1), so an unquoted `Off` is the string "Off" on one side and
 * `false` on the other — and the document that produced the diff is then not the
 * document that was applied. That divergence is a live property of this system;
 * what this test pins is the narrower promise that no starter *this console
 * ships* walks into it.
 */
const AMBIGUOUS = /^\s*[^#\s][^:]*:\s+(y|Y|yes|Yes|YES|n|N|no|No|NO|on|On|ON|off|Off|OFF|0\d+|\d+:\d+)\s*$/;

const starters = allStarters();

test('there is at least one starter, and every one is registered under the kind it declares', () => {
  expect(starters.length).toBeGreaterThan(20);
  for (const starter of starters) {
    const documents = yaml.loadAll(starter.text);
    expect(documents, `${starter.kind}/${starter.id} is not exactly one document`).toHaveLength(1);
    const [document] = documents;
    // The dialog resolves where to POST from these two fields alone. A starter
    // filed under a kind it does not declare would open on one listing and
    // create an object of another.
    expect(document.apiVersion, `${starter.kind}/${starter.id}`).toBe(starter.apiVersion);
    expect(document.kind, `${starter.kind}/${starter.id}`).toBe(starter.kind);
  }
});

test('no starter names a namespace, and every one names an object', () => {
  for (const starter of starters) {
    const document = yaml.load(starter.text);
    // The dialog falls back to the masthead's selection and says which
    // namespace won. A starter that hardcoded one would go stale the moment the
    // operator switched scope after opening the dialog.
    expect(document.metadata?.namespace, `${starter.kind}/${starter.id}`).toBeUndefined();
    expect(
      document.metadata?.name ?? document.metadata?.generateName,
      `${starter.kind}/${starter.id} has neither a name nor a generateName`,
    ).toBeTruthy();
  }
});

test('no starter carries a comment', () => {
  // Counted with the editor's own tokenizer, which tells a `#` inside an image
  // reference from one that starts a comment. A commented starter warns the
  // operator, on the first click and before they have typed anything, that the
  // form is about to destroy lines this console wrote.
  for (const starter of starters) {
    expect(rewriteLosses(starter.text), `${starter.kind}/${starter.id}`).toMatchObject({
      comments: 0,
      anchors: 0,
      merges: 0,
    });
  }
});

test('no starter holds a scalar the two parsers in this system disagree about', () => {
  for (const starter of starters) {
    for (const [index, line] of starter.text.split('\n').entries()) {
      expect(
        AMBIGUOUS.test(line),
        `${starter.kind}/${starter.id} line ${index + 1}: "${line.trim()}" reads as a string in the browser and as something else in the API server. Quote it.`,
      ).toBe(false);
    }
  }
});

test('every starter carrying a pod template satisfies the restricted profile', () => {
  // An admission rejection on the console's own starter reads as a broken
  // console rather than as a cluster policy, which is why this is a property of
  // the file and not of the reviewer who last touched it.
  const podSpecOf = (document) =>
    document.spec?.containers
      ? document.spec
      : document.spec?.template?.spec ?? document.spec?.jobTemplate?.spec?.template?.spec ?? null;

  for (const starter of starters) {
    const pod = podSpecOf(yaml.load(starter.text));
    if (!pod) continue;
    const where = `${starter.kind}/${starter.id}`;
    expect(pod.securityContext?.runAsNonRoot, where).toBe(true);
    expect(pod.securityContext?.seccompProfile?.type, where).toBe('RuntimeDefault');
    for (const container of pod.containers) {
      expect(container.securityContext?.allowPrivilegeEscalation, `${where} / ${container.name}`).toBe(false);
      expect(container.securityContext?.capabilities?.drop, `${where} / ${container.name}`).toContain('ALL');
    }
  }
});

test('a kind with no starter gets a labelled skeleton rather than a guess', () => {
  const entry = { apiVersion: 'cilium.io/v2alpha1', kind: 'CiliumCIDRGroup' };
  const offered = templatesFor(entry);

  expect(offered).toHaveLength(1);
  expect(offered[0].id).toBe(skeletonStarter(entry).id);
  // It says it is a skeleton. Inventing a spec for somebody's CRD would be a
  // guess with a Create button under it, and one that did not say so is the
  // same guess presented as an answer.
  expect(offered[0].description).toContain('ships no starter');

  const document = yaml.load(offered[0].text);
  expect(document).toEqual({ apiVersion: 'cilium.io/v2alpha1', kind: 'CiliumCIDRGroup', metadata: { name: 'example' } });
});

test('a caller with no kind is offered nothing rather than a skeleton of nothing', () => {
  // The masthead's blank "+" has no kind yet. A skeleton built from `undefined`
  // would be a document declaring a kind that does not exist.
  expect(templatesFor(null)).toEqual([]);
  expect(templatesFor({ apiVersion: 'v1' })).toEqual([]);
  expect(templatesFor({ kind: 'Pod' })).toEqual([]);
});
