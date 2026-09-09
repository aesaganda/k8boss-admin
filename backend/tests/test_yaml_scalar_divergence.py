"""What PyYAML makes of the scalars the browser reads differently.

The console parses every submitted manifest twice. The browser uses js-yaml 4,
which is YAML 1.2; :func:`app.admin.apply.parse_document` uses PyYAML, which is
YAML 1.1. For a handful of unquoted scalars those two disagree — ``off`` is the
text "off" in one and the boolean ``False`` in the other — and everything the
console reasons about locally (the create dialog's form view, ``unrepresented``,
``localIssues``) is built on the browser's reading of a document this side is
what actually sends.

The editor warns about that, and to do it ``frontend/src/components/
yamlDivergence.js`` carries a js-yaml schema whose scalar resolvers are
transcribed from PyYAML's ``yaml/resolver.py``. A transcription is a claim about
another library made from inside a language that cannot call it, so this is the
test that holds the claim to the library: it runs the same corpus through the
real PyYAML, by way of the real ``parse_document``.

**If this test fails, the frontend warning is wrong** — in one of two
directions, and the quiet one is worse. Too narrow, and an operator is told a
document is unambiguous when it is not. Too wide, and they are told to quote
something that never needed it, which is how a warning stops being read. Update
``PY_BOOL`` / ``PY_INT`` / ``PY_FLOAT`` in that module and the ``cluster``
column of the corpus together.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.admin.apply import parse_document

CORPUS = Path(__file__).parent / "data" / "yaml_scalar_corpus.json"

#: JSON has no literal for these three, so the corpus spells them.
_SPELLED = {"@inf": math.inf, "@-inf": -math.inf, "@nan": math.nan}


def _rows() -> list[dict]:
    return json.loads(CORPUS.read_text())["rows"]


def _expected(value):
    return _SPELLED.get(value, value) if isinstance(value, str) else value


def _same(got, expected) -> bool:
    """Equality that does not call ``1`` and ``True`` the same reading.

    ``True == 1`` in Python, so a plain ``==`` would let the int resolver claim
    a boolean row and pass. The type has to match too, and ``bool`` is not
    ``int`` here even though Python says it is.
    """
    if isinstance(expected, float) and math.isnan(expected):
        return isinstance(got, float) and math.isnan(got)
    if expected is None:
        return got is None
    if isinstance(expected, bool) != isinstance(got, bool):
        return False
    return type(got) is type(expected) and got == expected


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["scalar"])
def test_pyyaml_reads_the_scalar_the_corpus_records(row):
    """Every ``cluster`` value in the corpus is what the backend really parses.

    Through ``parse_document`` rather than ``yaml.safe_load`` directly: the
    function is what the write path calls, and a test that bypassed it would go
    on passing if somebody changed the loader underneath it.
    """
    document = parse_document(f"apiVersion: v1\nkind: ConfigMap\nvalue: {row['scalar']}\n")
    expected = _expected(row["cluster"])
    got = document["value"]
    assert _same(got, expected), (
        f"PyYAML reads `{row['scalar']}` as {got!r} ({type(got).__name__}), and the corpus records "
        f"{expected!r}. The mirror schema in frontend/src/components/yamlDivergence.js is transcribed "
        "from PyYAML's resolvers and is now wrong; fix both together."
    )


def test_the_corpus_covers_both_answers():
    """A corpus of only divergent rows would pass a mirror that always diverges.

    Both halves are load-bearing: the rows that differ prove the warning fires,
    and the rows that do not prove it stays quiet on `100m`, `2Gi` and an image
    tag — the scalars a real manifest is mostly made of, and the ones a warning
    that cried wolf about them would be dismissed for.
    """
    rows = _rows()
    assert sum(1 for row in rows if row["diverges"]) >= 10
    assert sum(1 for row in rows if not row["diverges"]) >= 10


def test_a_quoted_scalar_is_never_divergent():
    """Quoting is the advice the warning gives, so it has to be true.

    Every divergent row, quoted, parses as the text it looks like — which is
    also why the fix this console recommends is quoting rather than rewriting.
    """
    for row in _rows():
        if not row["diverges"]:
            continue
        document = parse_document(
            f'apiVersion: v1\nkind: ConfigMap\nvalue: "{row["scalar"]}"\n'
        )
        assert document["value"] == row["scalar"]
