/**
 * Inline the tour's screenshots into a single self-contained HTML file.
 *
 * `tour.html` references its images by relative path, so it opens correctly
 * from the repository with the shots sitting next to it. A published artifact
 * cannot fetch anything from another host, so this step rewrites every
 * `src="tour-shots/…"` into a `data:` URI.
 *
 * A missing image is a hard failure rather than a broken `<img>`: a tour whose
 * evidence silently did not load is exactly the confidently-wrong artefact the
 * product it documents is built against.
 *
 *   node tools/build-tour.mjs [output.html]
 */
import { readFile, writeFile, stat } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SOURCE = path.join(HERE, 'tour.html');
const OUT = process.argv[2] || path.join(HERE, 'tour.build.html');

const MIME = { '.png': 'image/png', '.jpeg': 'image/jpeg', '.jpg': 'image/jpeg', '.webp': 'image/webp' };

const html = await readFile(SOURCE, 'utf8');

const referenced = [...html.matchAll(/src="(tour-shots\/[^"]+)"/g)].map((m) => m[1]);
const unique = [...new Set(referenced)];

const missing = [];
for (const rel of unique) {
  try {
    await stat(path.join(HERE, rel));
  } catch {
    missing.push(rel);
  }
}
if (missing.length) {
  console.error(`Missing ${missing.length} screenshot(s) referenced by tour.html:`);
  for (const rel of missing) console.error(`  ${rel}`);
  console.error('\nRun `node tools/tour-shots.mjs` against a running `vite preview` first.');
  process.exit(1);
}

let out = html;
let bytes = 0;
for (const rel of unique) {
  const buffer = await readFile(path.join(HERE, rel));
  bytes += buffer.length;
  const mime = MIME[path.extname(rel).toLowerCase()] || 'application/octet-stream';
  const uri = `data:${mime};base64,${buffer.toString('base64')}`;
  out = out.replaceAll(`src="${rel}"`, `src="${uri}"`);
}

// Every `<img>` in this page is evidence, and an `alt` that was never written
// makes the page unreadable to anyone not looking at it.
const imgs = [...out.matchAll(/<img\b[^>]*>/g)];
const noAlt = imgs.filter((m) => !/\balt="[^"]{20,}"/.test(m[0]));
if (noAlt.length) {
  console.error(`${noAlt.length} <img> tag(s) have no substantive alt text.`);
  process.exit(1);
}

await writeFile(OUT, out);

const mb = (n) => `${(n / 1024 / 1024).toFixed(2)} MB`;
console.log(`${unique.length} screenshots inlined (${mb(bytes)} raw)`);
console.log(`${imgs.length} <img> tags, all with alt text`);
console.log(`→ ${OUT}  ${mb(Buffer.byteLength(out))}`);
if (Buffer.byteLength(out) > 16 * 1024 * 1024) {
  console.error('Over the 16 MB artifact limit.');
  process.exit(1);
}
