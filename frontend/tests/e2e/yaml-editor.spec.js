import { expect, test } from '@playwright/test';

import { mockApi } from './fixtures.js';

/**
 * The manifest editor's gutter and syntax colouring, driven through the Import
 * YAML dialog the masthead's "+" opens.
 *
 * The assertions here are mostly about one property, because it is the only one
 * that can hurt anybody: **the coloured copy painted behind the textarea has to
 * hold exactly the text the textarea holds, laid out in exactly the same
 * place.** The operator reads the copy and types into the box; if the two drift
 * by a character, the line they are reading is not the line they are editing —
 * on a screen whose next button applies the result to a cluster.
 *
 * So `the coloured copy is the text, line for line` and `both layers lay the
 * text out the same` are the two that matter. The colour-by-colour assertions
 * below them are the ordinary kind: they say a key looks like a key.
 */

const MANIFEST = `apiVersion: apps/v1
kind: Deployment  # the workload
metadata:
  name: "checkout"
  namespace: prod
spec:
  replicas: 3
  paused: false
  template:
    spec:
      containers:
        - name: app
          image: registry.example:5000/checkout:1.4.2
          command: ["sh", "-c"]`;

/** Open the masthead's Import YAML dialog with `text` in the editor. */
async function openEditor(page, text = MANIFEST) {
  await mockApi(page);
  await page.goto('/');
  await page.getByTestId('import-yaml-button').click();
  const editor = page.getByTestId('yaml-editor');
  await expect(editor).toBeVisible();
  if (text) await page.getByTestId('yaml-editor-input').fill(text);
  return editor;
}

/** The gutter's numbers, top to bottom, as strings. */
function gutterNumbers(page) {
  return page.getByTestId('yaml-editor-gutter').locator('.admin-yaml__lineno').allTextContents();
}

/** Every rendered line of the highlight layer, in order. */
function highlightLines(page) {
  return page.getByTestId('yaml-editor-highlight').locator('.admin-yaml__line').allTextContents();
}

test.describe('YAML editor gutter and highlighting', () => {
  test('every line is numbered, and the numbering follows the text', async ({ page }) => {
    await openEditor(page);

    const numbers = await gutterNumbers(page);
    expect(numbers).toEqual(MANIFEST.split('\n').map((_, i) => String(i + 1)));

    // A trailing newline is a real empty last line — js-yaml counts it when it
    // reports a position, so a gutter that did not would be off by one in
    // exactly the situation the numbers exist for.
    await page.getByTestId('yaml-editor-input').fill(`${MANIFEST}\n`);
    expect(await gutterNumbers(page)).toHaveLength(MANIFEST.split('\n').length + 1);

    // And the meta line above the box agrees with the gutter.
    await expect(page.getByTestId('yaml-editor')).toContainText(
      `${MANIFEST.split('\n').length + 1} lines`,
    );
  });

  test('the coloured copy is the text, line for line', async ({ page }) => {
    await openEditor(page);

    // Not a substring check: every line, in order, character for character.
    // This is the assertion that catches a tokenizer dropping or duplicating a
    // space, which is invisible in a screenshot and moves every glyph after it.
    expect(await highlightLines(page)).toEqual(MANIFEST.split('\n'));

    // Including while it is being typed into, where the tokenizer is running
    // over half-finished syntax.
    const half = 'metadata:\n  annotations:\n    note: "half a quo';
    await page.getByTestId('yaml-editor-input').fill(half);
    expect(await highlightLines(page)).toEqual(half.split('\n'));
  });

  test('both layers lay the text out the same', async ({ page }) => {
    // Longer and wider than the box on both axes, so both extents are decided
    // by the text rather than by the size of the element it sits in.
    await openEditor(page, Array.from({ length: 60 }, (_, i) => `key${i}: ${'v'.repeat(160)}`).join('\n'));

    // Every property that decides where a character lands, read off both layers
    // as the browser resolved them. This is the constraint stated directly: the
    // highlight layer may differ from the textarea in colour and in nothing
    // else, and a stylesheet that gives it its own font, weight, italic
    // comments, letter-spacing, tab-size or padding fails here rather than on
    // somebody's screen halfway through an edit.
    const metrics = await page.evaluate(() => {
      const PROPERTIES = [
        'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontStretch', 'fontKerning',
        'letterSpacing', 'wordSpacing', 'lineHeight', 'tabSize', 'whiteSpace', 'textIndent',
        'textTransform', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
      ];
      const read = (selector) => {
        const style = getComputedStyle(document.querySelector(selector));
        return Object.fromEntries(PROPERTIES.map((property) => [property, style[property]]));
      };
      const input = document.querySelector('.admin-yaml__input');
      const copy = document.querySelector('.admin-yaml__highlight-inner');
      return {
        input: read('.admin-yaml__input'),
        copy: read('.admin-yaml__highlight-inner'),
        inputHeight: input.scrollHeight,
        copyHeight: copy.scrollHeight,
        inputWidth: input.scrollWidth,
        copyWidth: copy.scrollWidth,
      };
    });

    expect(metrics.copy).toEqual(metrics.input);
    // Same properties, same text, so the same number of lines at the same
    // height: this is the one that fails if a future line ever renders as two.
    expect(metrics.copyHeight).toBe(metrics.inputHeight);
    // The copy has to reach at least as far right as the textarea can scroll,
    // or the last characters of the longest line are painted for one layer and
    // not the other.
    expect(metrics.copyWidth).toBeGreaterThanOrEqual(metrics.inputWidth);
  });

  test('the gutter and the colouring follow the textarea when it scrolls', async ({ page }) => {
    // Long enough and wide enough to scroll on both axes.
    const long = Array.from({ length: 80 }, (_, i) => `key${i}: ${'v'.repeat(140)}`).join('\n');
    await openEditor(page, long);

    await page.evaluate(() => {
      const input = document.querySelector('.admin-yaml__input');
      input.scrollTop = 200;
      input.scrollLeft = 120;
      input.dispatchEvent(new Event('scroll'));
    });

    const offsets = await page.evaluate(() => {
      const input = document.querySelector('.admin-yaml__input');
      const read = (selector) => {
        const matrix = new DOMMatrixReadOnly(getComputedStyle(document.querySelector(selector)).transform);
        return { x: matrix.m41, y: matrix.m42 };
      };
      return {
        scrollTop: input.scrollTop,
        scrollLeft: input.scrollLeft,
        copy: read('.admin-yaml__highlight-inner'),
        gutter: read('.admin-yaml__gutter-inner'),
      };
    });

    expect(offsets.scrollTop).toBeGreaterThan(0);
    expect(offsets.copy).toEqual({ x: -offsets.scrollLeft, y: -offsets.scrollTop });
    // The gutter tracks the vertical axis only: line numbers do not move
    // sideways when a long image reference scrolls out of view.
    expect(offsets.gutter).toEqual({ x: 0, y: -offsets.scrollTop });
  });

  test('keys, values and comments are told apart', async ({ page }) => {
    await openEditor(page);
    const highlight = page.getByTestId('yaml-editor-highlight');

    await expect(highlight.locator('.admin-yaml__t--key').first()).toHaveText('apiVersion');
    await expect(highlight.locator('.admin-yaml__t--comment')).toHaveText('# the workload');
    await expect(highlight.locator('.admin-yaml__t--string').first()).toHaveText('"checkout"');
    await expect(highlight.locator('.admin-yaml__t--number')).toHaveText('3');
    await expect(highlight.locator('.admin-yaml__t--const')).toHaveText('false');

    // The registry port is not a key. A rule that took the first colon on the
    // line would paint `image: registry.example` as one, and the operator would
    // be reading a field name that is not in the document.
    const keys = await highlight.locator('.admin-yaml__t--key').allTextContents();
    expect(keys).toContain('image');
    expect(keys).not.toContain('registry.example');
  });

  test('block scalar content is not coloured as if it were YAML', async ({ page }) => {
    await openEditor(
      page,
      ['apiVersion: v1', 'kind: ConfigMap', 'data:', '  nginx.conf: |', '    server: 8080;', '  owner: platform'].join('\n'),
    );
    const highlight = page.getByTestId('yaml-editor-highlight');

    // `server: 8080;` is a line of an nginx config that happens to contain a
    // colon. Colouring it as a key draws structure the document does not have.
    const keys = await highlight.locator('.admin-yaml__t--key').allTextContents();
    expect(keys).toEqual(['apiVersion', 'kind', 'data', 'nginx.conf', 'owner']);
    await expect(highlight.locator('.admin-yaml__t--literal')).toHaveText('    server: 8080;');
  });

  test('the line the parser refused is marked in the gutter and the text', async ({ page }) => {
    await openEditor(page, 'apiVersion: v1\nkind: ConfigMap\ndata:\n  a: b\n   bad indent: x\n');

    // The alert already named a line; the gutter is what makes that number
    // findable without counting down the box by hand.
    const alert = page.getByTestId('yaml-editor-error');
    await expect(alert).toBeVisible();
    const reported = Number(/Line (\d+)/.exec(await alert.textContent())[1]);
    expect(reported).toBeGreaterThan(0);

    const marked = page.getByTestId('yaml-editor-gutter').locator('.admin-yaml__lineno--error');
    await expect(marked).toHaveCount(1);
    await expect(marked).toHaveText(String(reported));
    await expect(page.getByTestId('yaml-editor-highlight').locator('.admin-yaml__line--error')).toHaveCount(1);

    // And it clears when the document parses again, rather than marking a line
    // that is no longer wrong.
    await page.getByTestId('yaml-editor-input').fill(MANIFEST);
    await expect(page.getByTestId('yaml-editor-gutter').locator('.admin-yaml__lineno--error')).toHaveCount(0);
  });

  test('Escape arms the way out of the box, and does not throw the manifest away', async ({ page }) => {
    await openEditor(page, 'a: 1');
    const input = page.getByTestId('yaml-editor-input');
    await input.click();

    // The keystroke the hint under the box recommends. It used to reach
    // PatternFly's Modal, which closes on Escape — so following the editor's
    // own instructions discarded whatever had been typed into it.
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('mutation-dialog')).toBeVisible();
    await expect(input).toHaveValue('a: 1');

    // And the Tab it armed moves focus out rather than indenting: that is the
    // whole point of arming it, and it is what makes the box escapable without
    // a mouse.
    await page.keyboard.press('Tab');
    await expect(input).toHaveValue('a: 1');
    await expect(input).not.toBeFocused();
  });

  test('Escape twice still closes the dialog', async ({ page }) => {
    await openEditor(page, 'a: 1');
    await page.getByTestId('yaml-editor-input').click();

    // The other exit. A dialog that cannot be dismissed from the control that
    // fills it is the same trap in the other direction, so the second Escape is
    // deliberately not intercepted.
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('mutation-dialog')).toHaveCount(0);
  });

  test('Tab still indents inside the box', async ({ page }) => {
    // The textarea is now transparent, overlaid and inside a flex frame. None
    // of that may cost it the editing behaviour it had: Tab is what indents
    // YAML, and it is handled on the element itself.
    await openEditor(page, 'a: 1');
    const input = page.getByTestId('yaml-editor-input');

    await input.click();
    await page.keyboard.press('Home');
    await page.keyboard.press('Tab');

    await expect(input).toHaveValue('  a: 1');
    // And the colouring follows the edit rather than the value it was mounted
    // with — the highlight layer is re-tokenised, not merely repositioned.
    expect(await highlightLines(page)).toEqual(['  a: 1']);
  });

  test('a manifest past the size limit keeps its numbers and says the colour is off', async ({ page }) => {
    // Above `HIGHLIGHT_MAX_LINES`. Colouring is dropped rather than allowed to
    // lag a keystroke behind — but the operator is told, because an editor that
    // silently stops colouring sends someone hunting for the syntax error that
    // turned it off.
    const huge = Array.from({ length: 2100 }, (_, i) => `key${i}: value`).join('\n');
    await openEditor(page, huge);

    await expect(page.getByTestId('yaml-editor')).toContainText('highlighting off (large manifest)');
    await expect(page.getByTestId('yaml-editor-highlight')).toHaveCount(0);
    // The gutter is not part of the trade: the numbers are still there.
    expect(await gutterNumbers(page)).toHaveLength(2100);
    // And the text is still readable, which it would not be if the textarea had
    // been left transparent with nothing painted behind it.
    await expect(page.getByTestId('yaml-editor-input')).not.toHaveCSS('color', 'rgba(0, 0, 0, 0)');
  });
});
