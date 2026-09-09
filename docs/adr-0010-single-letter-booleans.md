# ADR-0010 — `y` is `true`, because `kubectl` says so and PyYAML forgot to

**Status.** Accepted. Closes two of the nine differences ADR-0009 measured and
left open.
**Context.** `docs/adr-0009-one-yaml-reading.md`, `docs/safety-model.md` §4.1,
`docs/api-contract.md` §11.12.

---

## Context

ADR-0009 gave this console one reading of a manifest and measured it against
`kubectl`'s: agreement on 36 of the corpus's 45 scalars, with the nine
differences written down rather than left to be discovered. Two of the nine were
worse than the others, and ADR-0009 said so:

> **`y` and `n` are not, and cannot be** [covered by the warning], which is worth
> being explicit about: PyYAML and js-yaml both read them as the letters, so
> comparing the two readings finds nothing, and the disagreement is with a third
> parser neither of them can see.

That is the description of a *silent* difference. `verbose: y` reached a cluster
as the text `"y"` from this console and as `true` from `kubectl apply -f`, and
every mechanism this console has for saying so was blind to it: the editor's
warning compares this reading against YAML 1.2's, and YAML 1.2 calls `y` a string
too, so the two agreed and nothing was reported. For a CRD field with no schema
to reject the wrong type, that is a controller reading a string where the
operator's other tool would have stored a boolean, with nothing anywhere saying
which one they got.

## What settled it

Not a preference. PyYAML's own source.

Its boolean resolver is *registered* under the first characters `y`, `Y`, `n` and
`N` — and the pattern those characters lead to matches only
`yes`/`no`/`true`/`false`/`on`/`off` and their cased forms:

```
>>> yaml.resolver.Resolver.yaml_implicit_resolvers['y']
[('tag:yaml.org,2002:bool', re.compile('^(?:yes|Yes|YES|no|No|NO|true|True|TRUE
                                        |false|False|FALSE|on|On|ON|off|Off|OFF)$', re.X))]
>>> yaml.SafeLoader.bool_values
{'yes': True, 'no': False, 'true': True, 'false': False, 'on': True, 'off': False}
```

A resolver indexed under four characters it then refuses is the shape a library
leaves behind when it implements part of a type. YAML 1.1's boolean includes the
single letters; PyYAML declines them.

`sigs.k8s.io/yaml` does not, and it was measured rather than remembered — the
same throwaway Go program ADR-0009 used, over the same corpus:

```
  y  -> {"value":true}     Y  -> {"value":true}
  n  -> {"value":false}    N  -> {"value":false}
  yy -> {"value":"yy"}     Ye -> {"value":"Ye"}     -y -> {"value":"-y"}
```

So the gap is exactly four scalars wide, and on those four this console was not
implementing a different dialect from `kubectl` — it was implementing an
incomplete version of the same one.

## Decision

**1. `y`, `Y`, `n` and `N` are booleans.** `backend/app/yaml_dialect.py` is
PyYAML's `SafeLoader` with those four added to the boolean resolver and to
`bool_values`, and `frontend/src/components/clusterYaml.js` mirrors it. Nothing
else changes: the single letters only, anchored, so `yy`, `Ye` and `-y` stay the
text every parser in this system already calls them.

**2. This changes what a manifest means, and that is stated rather than
softened.** ADR-0009 kept PyYAML's reading precisely because adopting js-yaml's
would have changed one — `defaultMode: 0755` from 493 to 755. The rule it drew
was not "never change a reading"; it was that a change of reading has to be
argued for and its cost named. Here they differ in the way that matters:

| | `0755` → 755 | `y` → `true` |
|---|---|---|
| Away from `kubectl` or toward it | away | toward |
| What the API server does with it | accepts a file mode nobody wrote | rejects a boolean in a string field |
| How the operator finds out | a pod with wrong permissions, later | a 422 on the dry run, immediately |

A document that reads `verbose: y` and means the text now fails at Preview, with
the editor naming the path and telling the operator to quote it. It failed under
`kubectl` already. This is the one direction in which changing a reading makes a
silent difference loud, which is why it is the one taken.

**3. The warning gets these four for free, and that is the real repair.** Once
the console sends `true` for `y`, YAML 1.2 disagrees with it — and the existing
two-parse comparison, unchanged, reports the path and both readings like any
other ambiguous scalar. ADR-0009's "cannot be" was true of the two-reading
comparison and false of the console: the way to make an invisible difference
visible was to stop having it.

**4. Two scanners, one reading.** The dialect is applied by PyYAML's own scanner
for anything an operator wrote, and by libyaml for `deploy/olm/`'s 1.1 MiB of
vendored manifests, where the two differ by 0.9 seconds. That is a speed choice
about a SHA-pinned file, not a second dialect, and
`backend/tests/test_yaml_dialect.py` asserts the two agree over the whole corpus
rather than leaving it true by construction. An operator's document gets the slow
one on purpose: asked to read a manifest indented with a tab, PyYAML's scanner
answers *found character `'\t'` that cannot start any token* and prints the line
with a caret under it, and libyaml answers *found character that cannot start any
token* and stops. §4 promises the parser's own message.

**5. The dumper moves with the loader.** `yaml.safe_dump({"a": "y"})` writes
`a: y`, because PyYAML's emitter asks PyYAML's resolver whether a plain scalar
survives a round trip. Under this dialect it does not. That is not hypothetical:
§4's editor is seeded with `reader.to_yaml` — this backend's own dump of a live
object — and the operator saves it back, so a ConfigMap holding the string `"y"`
would have come back from an untouched save as `true`. `reader.to_yaml` and the
diff renderer now dump through the dialect, and the round trip is asserted over
the whole corpus on both sides of the wire.

## Consequences

* Agreement with `kubectl` goes from 36 of 45 to **40 of 47** — the four letters,
  over a corpus that gained the two spellings `Y` and `N` while they were being
  measured.
* The corpus's `y` and `n` rows move from the block that agrees to the block that
  diverges. That is the whole diff of this decision, in one file, which is what
  the corpus is for.
* **A manifest that applied yesterday can fail today.** `verbose: y` meant as
  text is now a boolean in a string field, refused at the dry run with the path
  named. There is no migration and no compatibility switch: a switch would be two
  readings again, which is the thing ADR-0009 exists to prevent.
* The console still differs from `kubectl` on seven scalars — `08`, `09`, `0o10`,
  `8:30`, `1:2:3`, `60:30.5`, `1.0e3` — and the warning covers all seven, because
  YAML 1.2 disagrees with us about each of them. **There is no longer a
  difference this console cannot see.** That, rather than the count, is what
  changed.

## Alternatives considered

**Warn about `y` instead of changing what it means.** The conservative option,
and the one that keeps ADR-0009's "changes what no manifest means" intact
verbatim. It needs a *third* schema in the browser — go-yaml's, transcribed — to
have something to compare against, because the two readings the console already
has agree about `y`. So the cost is a third transcription of a parser nothing in
CI can run, and the benefit is that the console goes on sending an object
`kubectl` would not. Rejected: it pays the larger maintenance price to preserve
the smaller of the two guarantees.

**Match `kubectl` on all nine.** Still rejected, and for the reason ADR-0009
gave: the remaining seven are `sigs.k8s.io/yaml`'s own dialect rather than a type
either side has half-implemented, so closing them means transcribing a Go
resolver into two languages and pinning all three to a corpus CI cannot check the
authority of. The four letters are separable precisely because they are not a
dialect difference — they are a gap in one.

**Keep `y` as text and refuse it.** A gate charging for a warning's worth of
information, which ADR-0009 already rejected for the other seven. It would also
refuse a document `kubectl` accepts, which is the wrong direction for a console
whose value is that it does what the command line does.

## See also

* `docs/adr-0009-one-yaml-reading.md` — the measurements, and the nine
  differences this closes two of. Its statement that `y` and `n` "cannot be"
  covered is superseded here; the reasoning that led to it is left standing.
* `backend/app/yaml_dialect.py` — the reading, the two scanners, and the dumper.
* `frontend/src/components/clusterYaml.js` — the mirror.
* `backend/tests/data/yaml_scalar_corpus.json` — the four rows that moved.
