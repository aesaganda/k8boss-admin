"""What PyYAML makes of the scalars a YAML 1.2 reader reads differently.

This console has **one** reading of a manifest and it is the one here:
:func:`app.admin.apply.parse_document` runs PyYAML, whose implicit resolvers are
YAML 1.1, and what it returns is what leaves as JSON. ``off`` is the boolean
``False``, ``0755`` is 493, ``8:30`` is 510. Since ADR-0009 the browser reads the
same document the same way, so that the form view's controls, ``unrepresented``
and ``localIssues`` are lenses onto the object that is actually going to be sent.

It reads it the same way by *transcription*, not by calling this parser:
``frontend/src/components/clusterYaml.js`` carries a js-yaml schema whose scalar
resolvers are copied from PyYAML's ``yaml/resolver.py``. A transcription is a
claim about another library made from inside a language that cannot call it, so
this is the test that holds the claim to the library — the same corpus, through
the real ``parse_document``.

**If this test fails, the browser and the API server have gone back to reading
the same manifest two ways**, which is the defect ADR-0009 exists to remove and
not a cosmetic one: it is what makes a form control, a diff of unrepresented
fields, or a local check describe an object nobody is about to write. Update
``PY_BOOL`` / ``PY_INT`` / ``PY_FLOAT`` in that module and the ``sent`` column of
the corpus together.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.admin.apply import parse_document
from app.errors import Invalid

CORPUS = Path(__file__).parent / "data" / "yaml_scalar_corpus.json"

#: JSON has no literal for these three, so the corpus spells them.
_SPELLED = {"@inf": math.inf, "@-inf": -math.inf, "@nan": math.nan}


def _rows() -> list[dict]:
    return json.loads(CORPUS.read_text())["rows"]


def _expected(value):
    return _SPELLED.get(value, value) if isinstance(value, str) else value


def _unsendable(value) -> bool:
    """A reading JSON has no spelling for, which ``parse_document`` refuses."""
    return isinstance(value, float) and not math.isfinite(value)


def _same(got, expected) -> bool:
    """Equality that does not call ``1`` and ``True`` the same reading.

    ``True == 1`` in Python, so a plain ``==`` would let the int resolver claim
    a boolean row and pass. The type has to match too, and ``bool`` is not
    ``int`` here even though Python says it is.
    """
    if expected is None:
        return got is None
    if isinstance(expected, bool) != isinstance(got, bool):
        return False
    return type(got) is type(expected) and got == expected


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["scalar"])
def test_pyyaml_reads_the_scalar_the_corpus_records(row):
    """Every ``sent`` value in the corpus is what the backend really parses.

    Through ``parse_document`` rather than ``yaml.safe_load`` directly: the
    function is what the write path calls, and a test that bypassed it would go
    on passing if somebody changed the loader underneath it.
    """
    expected = _expected(row["sent"])
    document = f"apiVersion: v1\nkind: ConfigMap\nvalue: {row['scalar']}\n"

    if _unsendable(expected):
        # The three the corpus records a reading for and this endpoint will not
        # send; `test_a_non_finite_number_is_refused_by_path` is where that is
        # asserted properly.
        with pytest.raises(Invalid):
            parse_document(document)
        return

    got = parse_document(document)["value"]
    assert _same(got, expected), (
        f"PyYAML reads `{row['scalar']}` as {got!r} ({type(got).__name__}), and the corpus records "
        f"{expected!r}. The mirror schema in frontend/src/components/clusterYaml.js is transcribed "
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


@pytest.mark.parametrize("scalar", [".inf", "-.Inf", ".nan"])
def test_a_non_finite_number_is_refused_by_path(scalar):
    """`.inf` and `.nan` are YAML numbers with no JSON spelling.

    Left alone they reach `json.dumps`, which writes `Infinity` and `NaN` — and
    the API server rejects the *body*, answering with a syntax error at a
    character offset that is in neither the operator's document nor the object
    they meant. Refused here instead, naming the path, before a request that
    cannot succeed is made and audited.
    """
    with pytest.raises(Invalid) as caught:
        parse_document(
            f"apiVersion: v1\nkind: ConfigMap\ndata:\n  nested:\n    - value: {scalar}\n"
        )
    assert caught.value.context["path"] == "data.nested.0.value"
    assert "JSON cannot carry" in str(caught.value.message)


def test_a_quoted_infinity_is_an_ordinary_string():
    """The refusal is about the number, not about the six characters.

    A ConfigMap whose value is the text `.inf` is an ordinary document, and a
    console that refused it would be refusing something JSON carries perfectly
    well — which is the false positive that turns a real refusal into noise.
    """
    document = parse_document(
        'apiVersion: v1\nkind: ConfigMap\ndata:\n  threshold: ".inf"\n'
    )
    assert document["data"]["threshold"] == ".inf"


def test_a_recursive_document_is_refused_rather_than_hung():
    """An anchor that contains itself must not cost the request its stack.

    PyYAML builds a self-referential anchor into a genuinely recursive object,
    so the walk that looks for unsendable numbers has to stop at one. It is a
    document nothing downstream can serialise either, but the failure mode this
    guards against is the walk itself never returning.
    """
    document = parse_document(
        "apiVersion: v1\nkind: ConfigMap\nmetadata: &meta\n  name: example\n  self: *meta\n"
    )
    assert document["metadata"]["name"] == "example"
