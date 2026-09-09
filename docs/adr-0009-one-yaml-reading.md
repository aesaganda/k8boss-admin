# ADR-0009 — One reading of a manifest, and it is the one that gets sent

**Status.** Accepted. **"What this does not fix" is superseded in part by**
`docs/adr-0010-single-letter-booleans.md`, which closes `y` and `n` — including
the claim below that they *cannot* be covered by the warning. That reasoning is
left standing rather than edited: it was true of the comparison this console had,
and the way it turned out to be false is the substance of ADR-0010.
**Context.** `docs/safety-model.md` §4.1, `docs/api-contract.md` §11.12, §4.

---

## Context

This console parsed every submitted manifest twice, with two parsers that do not
agree.

`backend/app/admin/apply.py`'s `parse_document` uses PyYAML, whose implicit
resolvers are YAML 1.1. The browser used js-yaml's default schema, which is YAML
1.2's core schema. For most documents that distinction is academic. For a
handful of unquoted scalars it is not, and the two readings went to two different
places: PyYAML's became JSON and went to the API server, and js-yaml's became
everything the console said about the document on the way there — the form view's
controls, `unrepresented()`'s list of fields the form is not showing, and every
check in `localIssues()`.

The concrete failure is small to describe and unpleasant to be on the end of. An
operator pastes a Deployment with `managed: off` in its labels. The form view
shows a text field containing `off`. The label check written for exactly this
class — *"a label value that parsed as a number or a boolean is the one mistake
YAML makes on the operator's behalf"* — sees an ordinary string and says nothing.
The API server is sent `false`, refuses the object with an unmarshalling error
that names no field, and the console has spent the operator's time reasoning
carefully about a document it was not going to write.

The previous change (§11.12) made the console **say so**: it parsed both ways,
compared the trees, and warned by path. That was true and it was not a fix. A
console that knows it is about to show one object and send another, and settles
for a sentence about it, has documented the defect rather than removed it.

## What was actually measured

The decision below turns on which reading to keep, so the readings were probed
rather than remembered. Three parsers, over
`backend/tests/data/yaml_scalar_corpus.json`'s 45 scalars:

* **PyYAML 6.0.2**, through the real `parse_document` — what this console sends.
* **js-yaml 4** with its default schema — YAML 1.2's core schema, and what the
  browser used to read.
* **`sigs.k8s.io/yaml` v1.4.0** — the library `kubectl` converts a manifest to
  JSON with, and therefore what `kubectl apply -f` sends for the same file. Run
  as a throwaway Go program over the same corpus file, September 2026; `kubectl`
  vendors its own version of it, and v1.4.0 is the one measured here.

The third one is here because the question "which YAML version is correct" has no
answer that Kubernetes supplies. **The API server never sees YAML.** Both
`kubectl` and this console convert the document to JSON in the client and send
that, so the dialect is the client's choice, and the only external check
available is the client an operator will compare us against.

The counts:

| Reading | Agrees with `kubectl` on |
|---|---|
| PyYAML — what this console sends | **36 of 45** |
| js-yaml default — YAML 1.2 core | 24 of 45 |

And the single row that settles the direction on its own:

```
  defaultMode: 0755    PyYAML 493    kubectl 493    YAML 1.2 core: 755
```

`0755` is not an exotic scalar. It is how a file mode is written in every
Kubernetes manifest that sets one.

## Decision

**1. The console has one reading of a manifest, and it is PyYAML's — the one
already being sent.**

Not because YAML 1.1 is better; it is not, and `8:30` meaning 510 is indefensible
on the merits. Because it is the reading already on the wire, so adopting it
changes what no manifest means. Adopting the other one would have changed
`defaultMode: 0755` from 493 to 755 on every manifest this console has ever
accepted — silently, in the direction of a file mode nobody wrote, and away from
what `kubectl` sends for the same line. A fix whose first act is to give an
existing document a new meaning is not a fix, and "we now round-trip consistently"
is no comfort to the operator whose ConfigMap became world-writable.

**2. The browser adopts it, for reading and for writing.**
`frontend/src/components/clusterYaml.js` is now the browser's only YAML: every
parse of a manifest goes through its `load`/`loadAll`, and every serialisation
through its `toYaml`. The schema is js-yaml with three scalar resolvers replaced
by transcriptions of PyYAML's, and with the *writing* halves of those three types
borrowed from js-yaml unchanged — nothing about how a value is written is
transcribed, only which strings PyYAML calls numbers.

Writing matters as much as reading, and less obviously. A form edit re-serialises
the whole document, so a dumper working from a different reading writes back
scalars nobody touched: js-yaml's own dumper writes the string `"1_000"` as a
bare `1_000`, because YAML 1.2 has no underscore digits, and the backend then
sends the number 1000. Dumping against the same schema that parsed makes the
round trip exact for every row in the corpus, and there is a test per row that
says so.

**3. The warning stays, and it is now about the document rather than about us.**

It was tempting to delete it: the two halves of the console agree, so what is
left to report? The ambiguity, which was never the console's. `enabled: off` is
`false` to this console, to `kubectl` and to YAML 1.1, and is the text `"off"` to
the YAML 1.2 specification, to the operator's editor, and to most of what will
syntax-highlight the file on its way here. Which of those is right is not
something this console gets to settle. Which one it is going to send is, and that
is what the warning now says: *this looks like the text "off" and will be sent as
the boolean false*. It is still computed by parsing both ways — the second
reading is no longer a rival implementation, it is YAML 1.2, on purpose — and it
still never blocks.

**4. `.inf`, `-.Inf` and `.nan` are refused, by path, at parse time.**

The one place the reading produces a value that cannot be sent at all. PyYAML
resolves them to Python floats; `json.dumps` writes `Infinity`, `-Infinity` and
`NaN`, which are not JSON, and the API server rejects the *body* with a syntax
error at a character offset. The operator is told their manifest is malformed at
a position that is in neither the document they wrote nor the object they meant.
`parse_document` now refuses them with a 422 naming the path — before a request
that cannot succeed is made and audited. `kubectl` refuses the same three scalars
at the same point in its own pipeline, which is the useful cross-check: this is
not a rule this console invented, it is JSON's.

There is deliberately no second implementation of this check in the browser. One
place decides what can be sent, and the dry run — which the operator reaches by
pressing Preview, before anything is written — is where it is said.

## What this does not fix

**Nine scalars, where this console and `kubectl` still send different objects for
the same file.** Stated rather than left to be discovered:

| Scalar | This console sends | `kubectl` sends |
|---|---|---|
| `08`, `09` | the text `"08"`, `"09"` | 8, 9 |
| `0o10` | the text `"0o10"` | 8 |
| `8:30` | 510 | the text `"8:30"` |
| `1:2:3` | 3723 | the text `"1:2:3"` |
| `60:30.5` | 3630.5 | the text `"60:30.5"` |
| `1.0e3` | the text `"1.0e3"` | 1000 |
| `y`, `n` | the text `"y"`, `"n"` | `true`, `false` |

Seven of those nine are covered by the warning, because a YAML 1.2 reader
disagrees with us about them too. **`y` and `n` are not, and cannot be**, which
is worth being explicit about: PyYAML and js-yaml both read them as the letters,
so comparing the two readings finds nothing, and the disagreement is with a third
parser neither of them can see. A check that fired on the *text* `y` instead
could not tell a plain `y` from a quoted `"y"` — and telling an operator to quote
something they already quoted is how a warning stops being read.

Closing those nine means reproducing a Go library's resolver in Python and in
JavaScript and pinning all three to a corpus no CI job here can run the third
member of. That is a larger decision than this one and it is not taken here.
What is taken here is that the gap is measured, written down, and no longer a
thing this console would discover by being wrong in front of somebody.

## Consequences

* The form view, `unrepresented()` and `localIssues()` are lenses onto the object
  that is going to be sent. The label check that could not see `version: yes` now
  sees a boolean, because that is what it is.
* A form edit round-trips every scalar in the corpus exactly. It did not before.
* `toYaml` moved from `objectForm.js` to `clusterYaml.js`, next to the schema it
  has to dump against, and `objectForm.js` no longer imports js-yaml at all.
* A document with `.inf` or `.nan` in it now fails at the dialog with a sentence
  naming the path, instead of at the API server with a byte offset.
* **The transcription is a standing cost.** Three regexes and two constructors in
  `clusterYaml.js` are a claim about PyYAML made from a language that cannot call
  it. They are pinned from both sides over one corpus —
  `backend/tests/test_yaml_scalar_reading.py` through the real `parse_document`,
  `frontend/tests/e2e/cluster-yaml.spec.js` through the real js-yaml — so a
  PyYAML release that moves a resolver fails a test rather than quietly restoring
  the two-reading bug. It does not remove the cost; it makes drift loud.
* Changing the loader in `parse_document` is now a change to what every manifest
  this console has ever accepted means, and the mirror has to move with it. The
  docstring says so at the site.

## Alternatives considered

**Change the backend to YAML 1.2 instead, so no transcription is needed.** The
tidiest option on paper: delete the mirror, let js-yaml's default schema be the
reading, make PyYAML match it. Rejected on the evidence above. It re-reads every
`0755` as 755, moves the console from 36/45 agreement with `kubectl` to 24/45,
and does both to documents that already exist and to habits operators already
have. The transcription is the price of not doing that, and it is a price paid
in a test file rather than in somebody's cluster.

**Match `kubectl` exactly.** The most defensible target and the most expensive:
`sigs.k8s.io/yaml` is neither YAML 1.1 nor 1.2 — it takes `y`/`n` as booleans,
takes `0o10` as octal, and refuses sexagesimals — so matching it means
transcribing a third dialect into two languages and holding all three to a corpus
that CI cannot check the authority of. Recorded here with its measurements so
that a later decision to do it starts from data.

**Refuse every ambiguous scalar rather than choosing a reading.** Attractive
under this project's defect standard — *a wrong answer delivered confidently is
worse than no answer* — and wrong here, because there is no wrong answer to
avoid: the console knows exactly what it will send. Refusing would block
`defaultMode: 0755`, a line that means the same thing to this console and to
`kubectl`, on the grounds that a third reader disagrees. That is a gate charging
for a warning's worth of information.

**Send the document to the backend to be parsed, so there is only one parser.**
One reading, no transcription, and a network round trip on every keystroke in the
editor — plus a new endpoint whose only job is to parse, which is the second write
path this repository's funnel exists to refuse. The form view has to reason about
the document locally to be a form view at all.

**Vendor `ruamel.yaml` and pin it to YAML 1.2.** Solves the backend half of a
problem whose backend half was never the one that needed solving, and adds a
dependency to a `requirements.txt` where every entry carries a paragraph
justifying itself. The dependency would have been the smallest part of the cost.

## See also

* `docs/api-contract.md` §11.12 — the rule, and what the warning promises.
* `docs/safety-model.md` §4.1 — where this sits relative to the diff, which was
  never the thing at risk.
* `backend/tests/data/yaml_scalar_corpus.json` — the corpus, its columns, and
  which test asserts which.
* `frontend/src/components/clusterYaml.js` — the schema, and why a schema rather
  than a pattern over the source.
