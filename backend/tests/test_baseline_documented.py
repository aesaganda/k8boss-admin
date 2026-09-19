"""
The documented baseline check set, held to what the endpoint actually preflights.

`BASELINE_PREFLIGHT_CHECKS` is quoted in four documents by count and enumerated
in one of them, and all five had drifted. The count said *seventeen* in the
README, in `docs/rbac.md`, in `docs/safety-model.md` and in
`scripts/onboard-cluster.sh` while the tuple held eighteen — the
`pods/ephemeralcontainers` check was added with its own explanatory comment and
nobody counted again. `docs/rbac.md` also listed `create core/pods`, which this
console has never preflighted at registration.

Neither error could fail anything. The endpoint returned eighteen results, the
UI rendered eighteen rows, and the only place the disagreement existed was in
prose nobody diffs against the tuple. It was found by pointing the console at a
real cluster and counting the rows that came back.

That is precisely the shape this repository writes tests for, so:

* the four documents that state a number state the right one, and
* `docs/rbac.md`'s block is the tuple, exactly — same verbs, same targets,
  nothing extra.

The second assertion is the one that matters. A wrong count is embarrassing; a
listed permission the console never checks is a reader being told their
registration was verified for something it was not, which is the defect standard
with a table around it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.clusters import BASELINE_PREFLIGHT_CHECKS

REPO = Path(__file__).resolve().parents[2]

#: Number words this project writes out. The docs spell the count rather than
#: digit it, so the test has to read the same way a reviewer does.
NUMBER_WORDS = {
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen", 20: "twenty",
}

#: Every file that states the count, with the phrase it states it in. A file
#: added to this list is a file that can no longer drift; a file left off it is
#: one that can, so prefer stating the count in one of these.
COUNT_CLAIMS = (
    ("README.md", "preflights {word} baseline permissions"),
    ("docs/rbac.md", "preflights these {word}"),
    ("docs/safety-model.md", "preflights a baseline of {word} checks"),
    ("scripts/onboard-cluster.sh", "which of the {word} baseline permissions"),
)


def _target(check: dict) -> str:
    """One check as `docs/rbac.md` spells it: ``verb group/resource[/sub]``."""
    suffix = f"/{check['subresource']}" if check.get("subresource") else ""
    return f"{check['verb']} {check['group']}/{check['resource']}{suffix}"


@pytest.mark.parametrize("path,phrase", COUNT_CLAIMS)
def test_every_document_states_the_real_count(path, phrase):
    expected = NUMBER_WORDS[len(BASELINE_PREFLIGHT_CHECKS)]
    text = (REPO / path).read_text()

    assert phrase.format(word=expected) in text, (
        f"{path} does not say {expected!r}. BASELINE_PREFLIGHT_CHECKS holds "
        f"{len(BASELINE_PREFLIGHT_CHECKS)} checks; update the sentence, or add "
        "the number word to NUMBER_WORDS if the set grew past this table."
    )


def test_the_rbac_table_lists_exactly_what_is_preflighted():
    """The enumerated block in `docs/rbac.md`, parsed and compared as a set.

    Parsed rather than eyeballed because the block is column-aligned and
    continues a verb across lines — which is readable, and is how a whole extra
    row went unnoticed under the `create` column.
    """
    text = (REPO / "docs/rbac.md").read_text()
    block = re.search(
        r"## Baseline check at registration.*?```\n(.*?)```", text, re.S,
    )
    assert block, "the baseline block in docs/rbac.md moved or lost its fence"

    verbs = {"list", "patch", "create", "delete", "get", "update", "watch"}
    documented: set[str] = set()
    verb: str | None = None
    for line in block.group(1).splitlines():
        if not line.strip():
            continue
        tokens = line.split()
        if tokens[0] in verbs:
            verb, tokens = tokens[0], tokens[1:]
        assert verb, f"the block starts with a continuation line: {line!r}"
        documented.update(f"{verb} {token}" for token in tokens)

    actual = {_target(check) for check in BASELINE_PREFLIGHT_CHECKS}

    assert documented == actual, (
        "docs/rbac.md's baseline block and BASELINE_PREFLIGHT_CHECKS disagree.\n"
        f"  documented but never checked: {sorted(documented - actual) or 'none'}\n"
        f"  checked but not documented:   {sorted(actual - documented) or 'none'}\n"
        "The first is the serious one: it tells an operator their registration "
        "was verified for a permission this console never asked about."
    )
