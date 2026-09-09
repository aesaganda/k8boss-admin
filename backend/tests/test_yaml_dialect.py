"""The backend's one YAML, and the round trip that has to survive it.

:mod:`app.yaml_dialect` is PyYAML plus four scalars — ``y``, ``Y``, ``n`` and
``N`` — read as booleans, because ``kubectl`` reads them that way and this
console did not (ADR-0010). Two properties matter enough to be asserted rather
than assumed:

**The reading applies wherever a manifest is read.** There are two *scanners* —
PyYAML's, which names the character it refused, and libyaml's, which is ten times
faster on the 1.1 MiB vendored bundle — and they are asserted here to be one
*reading*. A second reading anywhere in this backend is the bug ADR-0009 removed,
wearing a loader argument as a hat.

**The writing matches the reading.** ``yaml.safe_dump({"a": "y"})`` emits
``a: y``, because PyYAML's emitter asks PyYAML's resolver whether a plain scalar
survives a round trip and PyYAML's resolver says ``y`` is a string. Under this
dialect it is not. §4's editor is seeded with this backend's own dump of a live
object and the operator saves it back, so a dumper that disagreed with the
loader would turn a ConfigMap holding the string ``"y"`` into ``true`` on a save
nobody typed into.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from app import yaml_dialect
from app.admin import diff as diff_module
from app.resources import reader

CORPUS = Path(__file__).parent / "data" / "yaml_scalar_corpus.json"

#: The four this dialect adds, with what each one now means.
LETTERS = [("y", True), ("Y", True), ("n", False), ("N", False)]


@pytest.mark.parametrize("scalar,expected", LETTERS)
def test_the_single_letters_are_booleans(scalar, expected):
    """`kubectl` sends `true` for `verbose: y`; so, now, does this console."""
    value = yaml_dialect.load_all(f"value: {scalar}\n")[0]["value"]
    assert value is expected


@pytest.mark.parametrize("scalar", ["yy", "Ye", "ny", "-y", "y.", "yn", "Nn"])
def test_only_the_single_letters(scalar):
    """The resolver is anchored, and a wider one would invent a third reading.

    `kubectl` reads every one of these as text, and so does YAML 1.2. A dialect
    that swallowed them would not be closing a gap, it would be opening one no
    other parser in this system has.
    """
    assert yaml_dialect.load_all(f"value: {scalar}\n")[0]["value"] == scalar


@pytest.mark.parametrize("scalar,_expected", LETTERS)
def test_quoting_still_settles_it(scalar, _expected):
    """The advice the editor gives has to be true of the letters too."""
    assert yaml_dialect.load_all(f'value: "{scalar}"\n')[0]["value"] == scalar


def test_the_dialect_does_not_change_plain_pyyaml():
    """Adding a resolver to a subclass must not reach `yaml.safe_load`.

    ``bool_values`` lives on ``SafeConstructor`` and the resolver table on
    ``Resolver``; mutating either in place would change what every other library
    in this process makes of a YAML file. The subclass is the whole point.
    """
    assert yaml.safe_load("value: y\n")["value"] == "y"
    assert "y" not in yaml.SafeLoader.bool_values


@pytest.mark.parametrize(
    "value",
    ["y", "Y", "n", "N", "off", "yes", "1_000", "8:30", "0755", "plain", "100m", "", "null"],
)
def test_a_string_that_would_read_back_as_something_else_is_quoted(value):
    """The property the editor's seed depends on, asserted for the dumper.

    Not "the dumper quotes these particular scalars" — that would be a test of
    PyYAML's emitter. What is asserted is that the text this backend produces,
    read back by the reader this backend uses, is the value it started from.
    """
    text = yaml_dialect.dump({"a": value})
    assert yaml_dialect.load_all(text)[0]["a"] == value


@pytest.mark.parametrize("value", [True, False, None, 0, 8, 1.5, -1, "0", "true"])
def test_the_other_scalars_round_trip_too(value):
    """A dumper fixed for the letters and broken for numbers is not fixed."""
    text = yaml_dialect.dump({"a": value})
    back = yaml_dialect.load_all(text)[0]["a"]
    assert back == value and type(back) is type(value)


def test_the_whole_corpus_round_trips_through_the_backend():
    """Every scalar the console knows about, dumped and read back.

    The corpus is shared with the browser, whose own round-trip test asserts the
    same property against the mirror. Between them, a form edit and a save
    return the object they started from on both sides of the wire.
    """
    spelled = {"@inf": math.inf, "@-inf": -math.inf, "@nan": math.nan}
    for row in json.loads(CORPUS.read_text())["rows"]:
        sent = row["sent"]
        value = spelled.get(sent, sent) if isinstance(sent, str) else sent
        if isinstance(value, float) and not math.isfinite(value):
            continue  # refused before it can be sent; test_yaml_scalar_reading covers it
        text = yaml_dialect.dump({"a": value})
        back = yaml_dialect.load_all(text)[0]["a"]
        assert back == value and type(back) is type(value), (
            f"`{row['scalar']}` reads as {value!r}, dumps as {text.strip()!r} and comes back "
            f"as {back!r}. A form edit of a manifest holding it would change a field nobody opened."
        )


def test_the_two_scanners_are_one_reading():
    """`deploy/olm/` is read by libyaml and an operator's manifest by PyYAML.

    That is a speed choice about a 1.1 MiB vendored file, not a second dialect,
    and this is what keeps the difference to speed. If it ever fails, the console
    reads a manifest one way from the editor and another from the bundle — which
    is the two-reading defect ADR-0009 removed, reintroduced through a loader
    argument.
    """
    for row in json.loads(CORPUS.read_text())["rows"]:
        document = f"value: {row['scalar']}\n"
        slow = yaml_dialect.load_all(document)[0]["value"]
        fast = yaml_dialect.load_all(document, fast=True)[0]["value"]
        if isinstance(slow, float) and math.isnan(slow):
            assert isinstance(fast, float) and math.isnan(fast), row["scalar"]
            continue
        assert type(slow) is type(fast) and slow == fast, row["scalar"]


def test_the_slow_scanner_names_the_character_it_refused():
    """§4 promises the parser's own message, and the two scanners differ on it.

    PyYAML's scanner prints the offending character and the line it is on;
    libyaml prints neither. An operator who pasted a manifest indented with a tab
    gets the first one, which is the whole reason `parse_document` does not take
    the fast path.
    """
    with pytest.raises(yaml.YAMLError) as caught:
        yaml_dialect.load_all("spec:\n\tbad: tab\n")
    assert "\\t" in str(caught.value) or "\t" in str(caught.value)


def test_the_editor_is_seeded_with_text_that_means_what_it_shows():
    """§4's editor seed, end to end: a live object out and the same object back.

    `reader.to_yaml` is what fills the YAML view for an existing object, and the
    operator's save posts that text to `parse_document`. The string "y" in a
    ConfigMap is the value that catches a dumper reading from the wrong side.
    """
    live = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "example"},
        "data": {"verbose": "y", "quiet": "N", "debug": "off", "grouped": "1_000"},
    }
    assert yaml_dialect.load_all(reader.to_yaml(live))[0] == live


def test_the_diff_the_operator_confirms_is_rendered_the_same_way():
    """§3's diff is text, and text read back has to be the object it described.

    A diff rendered by a dumper that disagreed with the loader would show
    `verbose: y` on a line whose value is the boolean `true` — a diff that is
    accurate about the object and misleading about the document.
    """
    rendered = diff_module._to_yaml({"data": {"verbose": "y"}})
    assert yaml_dialect.load_all(rendered)[0] == {"data": {"verbose": "y"}}
