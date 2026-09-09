"""The console's YAML, on the side that sends it.

**Every manifest this backend reads is read here, and every manifest it writes
is written here.** `frontend/src/components/clusterYaml.js` is the same module
on the other side of the wire, and the two are held to one corpus. Nothing else
may call ``yaml.safe_load`` or ``yaml.safe_dump`` on a document that is on its
way to, or out of, a cluster.

## What the dialect is

PyYAML's ``SafeLoader``, plus the four scalars ``y``, ``Y``, ``n`` and ``N`` read
as booleans.

Those four are the whole of the difference, and PyYAML's own source is the
clearest statement of why they are odd. Its boolean resolver is *registered*
under the first characters ``y``, ``Y``, ``n`` and ``N`` — and the pattern those
characters lead to matches only ``yes``/``no``/``true``/``false``/``on``/``off``
and their cased forms. The registration is the shape left behind by a library
that implements part of a type: YAML 1.1's boolean includes the single letters,
and PyYAML declines them.

``kubectl`` does not decline them. It converts a manifest to JSON with
``sigs.k8s.io/yaml``, which reads ``y`` as ``true`` and ``n`` as ``false``, so
until ADR-0010 a document reading ``verbose: y`` reached a cluster as the text
``"y"`` from this console and as ``true`` from the command line — and, unlike the
other differences ADR-0009 measured, **nothing said so**: the editor's warning
compares this reading against YAML 1.2's, and YAML 1.2 also calls ``y`` a
string, so the two agreed and the warning stayed quiet. ADR-0010 closes it by
completing the boolean type rather than by adding a third opinion.

## Why the dumper matters as much as the loader

A reading is only half of it. ``yaml.safe_dump({"a": "y"})`` writes ``a: y``,
because PyYAML's *emitter* asks PyYAML's *resolver* whether a plain scalar would
survive a round trip — and PyYAML's resolver says ``y`` is a string. Under this
dialect it is not, so the same text read back is the boolean ``True``.

That is not a hypothetical: §4's editor is seeded with
:func:`app.resources.reader.to_yaml` — this backend's own dump of a live
object — and the operator saves it back. A ConfigMap holding the string ``"y"``
would come back from a save as ``true`` and be refused, with the offending
character never having been typed by anybody. So the dumper carries the same
resolver, quotes what it would otherwise re-read as a boolean, and
``test_yaml_dialect.py`` asserts the round trip rather than trusting it.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.constructor import SafeConstructor

#: The four scalars this dialect adds to PyYAML's booleans. Anchored, and the
#: single letters only: ``yy`` and ``Ye`` are ordinary strings to every parser
#: in this system, and a resolver that swallowed them would invent a third
#: reading rather than close a gap.
_LETTER_BOOL = re.compile(r"^(?:y|Y|n|N)$")

#: The first characters the resolver above is registered under. PyYAML indexes
#: implicit resolvers by first character, and one omitted here is a scalar the
#: resolver is never offered.
_LETTER_FIRST = list("yYnN")

#: ``SafeConstructor.construct_yaml_bool`` looks the scalar up lowercased, so
#: two entries cover all four spellings. Copied rather than mutated: PyYAML
#: holds ``bool_values`` on ``SafeConstructor``, and adding to that dict in place
#: would change the meaning of ``yaml.safe_load`` for every other library in the
#: process.
_BOOL_VALUES = {**SafeConstructor.bool_values, "y": True, "n": False}


def _reading(base: type) -> type:
    """``base`` with the four letters resolved as booleans.

    ``add_implicit_resolver`` is a classmethod that copies the inherited table
    into the subclass before appending, so this leaves ``yaml.SafeLoader``
    itself untouched — which matters because this process also loads YAML that
    is not a manifest.
    """
    subclass = type(f"Console{base.__name__}", (base,), {"bool_values": _BOOL_VALUES})
    subclass.add_implicit_resolver("tag:yaml.org,2002:bool", _LETTER_BOOL, _LETTER_FIRST)
    return subclass


#: The reading, on PyYAML's own scanner. This is the one an operator's document
#: goes through, and the scanner is the reason: asked to read a manifest indented
#: with a tab, it answers *found character '\t' that cannot start any token*
#: and prints the offending line with a caret under it. libyaml, below, says
#: *found character that cannot start any token* and stops. §4 promises the
#: parser's own message because an editor that says "invalid" without saying
#: which line has a tab in it is an editor people stop using, and that promise is
#: kept by choosing the scanner that keeps it.
ConsoleLoader = _reading(yaml.SafeLoader)

#: The same reading on libyaml, for the one input nobody typed: ``deploy/olm/``
#: is 1.1 MiB of vendored manifests, pinned by SHA-256 and parsed once per
#: process, and the two scanners differ by 0.9 seconds on it. A scan error there
#: is a packaging fault, not a message an operator needs to read.
#:
#: **Two scanners, one reading.** libyaml scans; PyYAML's ``Resolver`` still
#: decides what each scalar means, so the dialect applies to both — and
#: ``test_yaml_dialect.py`` asserts they agree over the whole corpus rather than
#: leaving that to be true by construction.
ConsoleFastLoader = _reading(getattr(yaml, "CSafeLoader", yaml.SafeLoader))

#: One dumper, PyYAML's own. Nothing here is big enough for the emitter's speed
#: to matter, and two emitters would be two spellings of the same object in a
#: diff the operator is asked to confirm.
ConsoleDumper = _reading(yaml.SafeDumper)


def load_all(text: str, *, fast: bool = False) -> list[Any]:
    """Every document in ``text``, read as the API server will be sent it.

    ``fast`` picks the libyaml scanner and is for the vendored bundle only — see
    :data:`ConsoleFastLoader` for what it costs and why an operator's document
    does not get it.

    Materialised rather than returned as the generator ``yaml.load_all`` gives
    back: a caller that took the first document and dropped the rest would
    silently apply one object out of four, and counting them is how
    :func:`app.admin.apply.parse_document` refuses that instead.
    """
    return list(yaml.load_all(text, Loader=ConsoleFastLoader if fast else ConsoleLoader))


def dump(obj: Any) -> str:
    """One object as the text this console shows and accepts back.

    The three options are not cosmetic:

    * ``sort_keys=False`` keeps the API server's field order
      (``apiVersion``/``kind``/``metadata``/``spec``/``status``). Alphabetical
      order puts ``status`` above ``spec`` and buries ``kind`` in the middle,
      which makes a diff far harder to read than it needs to be.
    * ``width`` is very wide so a long image reference or container argument is
      not wrapped. Wrapped YAML still parses, but the wrap point moves when an
      unrelated field changes length, and the diff then shows lines nobody
      edited.
    * ``default_flow_style=False`` keeps everything block-style, which is what
      every Kubernetes manifest in the wild looks like.
    """
    return yaml.dump(
        obj,
        Dumper=ConsoleDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )


__all__ = ["ConsoleDumper", "ConsoleLoader", "dump", "load_all"]
