# `frontend/tools` — the product tour

Three scripts and one page. Together they produce a screenshot tour of the
console that is shot **against the running build** rather than drawn, so it can
be regenerated when the console changes instead of slowly becoming a picture of
a version nobody ships any more.

| File | What it is |
|---|---|
| `tour-fixtures.mjs` | The cluster the tour is shot against: `prod-eu`, mid-incident, with reads that failed |
| `tour-shots.mjs` | Drives the console with Playwright and captures `tour-shots/*.png` |
| `tour.html` | The tour itself, referencing those images by relative path |
| `build-tour.mjs` | Inlines the images as `data:` URIs into one self-contained file |

## Regenerating

```bash
cd frontend
npm run build && npm run preview &      # :5174
node tools/tour-shots.mjs               # → tools/tour-shots/*.png
node tools/build-tour.mjs               # → tools/tour.build.html
```

Shoot a subset by passing name fragments: `node tools/tour-shots.mjs 06 08`.

`PLAYWRIGHT_CHROMIUM_PATH` points at a Chromium that is already on disk, for
sandboxes that ship one pinned to a different build number than this
`@playwright/test` expects — the same escape hatch `playwright.config.js` uses,
and the difference between the script running and the script being skipped.

`TOUR_SCALE` (default `1.5`) sets the device pixel ratio, and `TOUR_FORMAT=jpeg`
switches the output format. PNG is the default because these are flat-colour UI
shots: at quality 90 the JPEGs came out *larger* than the PNGs and softer on
type.

Neither the images nor `tour.build.html` is committed — see `.gitignore`. Both
are reproducible from what is here in about a minute.

## Why the fixture cluster is broken on purpose

A tour is where a console usually starts lying: every table full, every number
green, every read successful. This console's entire argument is that it does not
do that, so the fixtures carry a denied node listing, a down APIService, a
controller that has stopped reporting, and a pod whose phase disagrees with its
container.

Every `null` in `tour-fixtures.mjs` is annotated with the rule it exercises. A
fixture that quietly became a `0` would be a visible change in review, which is
the only reason those comments are worth their length.

## Binding to the code, not to the contract

The fixtures were written against `docs/api-contract.md` and then corrected
against what the handlers actually return — the workload detail envelope is
`{workload, spec, pods, conditions, services, rollout, partial, unavailable}`,
not the flattened row the contract's prose implies, and a batched preflight
answers with one result **per check, paired by index**. Both were caught the same
way: the shot came back a picture of an empty page.

`build-tour.mjs` refuses to build if `tour.html` references an image that is not
on disk, or if any `<img>` lacks substantive `alt` text. A tour whose evidence
silently did not load is the confidently-wrong artefact the product it documents
is built against.
