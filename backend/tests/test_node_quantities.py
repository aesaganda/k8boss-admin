"""
The Kubernetes quantity parser (§5).

Table-driven on purpose. Quantity parsing is where a capacity dashboard lies
quietly: the numbers it produces are always plausible, so a wrong one is never
noticed by looking at the page — only by an operator who scheduled onto a node
that turned out to be full. The table is the specification.

Three families of case, and each exists because getting it wrong is a specific
incident:

* **Binary against decimal.** ``Mi`` and ``M`` differ by 4.8%, ``Gi`` and ``G``
  by 7.4%. Reading one as the other on a 256 GiB node invents 19 GB of headroom.
* **Rejection over invention.** Anything outside the grammar returns ``None``,
  which renders as an em dash. A parser that fell back to ``0`` would describe a
  2 TiB node as empty; one that stripped the suffix would describe ``64Gi`` as 64
  bytes.
* **Exactness under summation.** A node's requested CPU is the sum of a hundred
  containers' ``350m``. In float that lands on 34.99999999999997 and renders as
  a number nobody trusts; the parser returns ``Decimal`` so it lands on 35.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.nodes import (
    cpu_cores,
    memory_bytes,
    node_usage,
    parse_quantity,
    pod_capacity,
)

# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

# (input, expected exact value). Decimal on the right-hand side throughout, so a
# comparison never routes through float and quietly passes on a value the parser
# got very slightly wrong.
VALID = [
    # Plain integers — a node's cpu capacity and a raw byte count.
    ("16", Decimal(16)),
    ("68719476736", Decimal(68719476736)),
    ("0", Decimal(0)),
    ("+3", Decimal(3)),
    ("-1", Decimal(-1)),
    # Decimals. `1.5` is a legal CPU request and must not become 1 or 15.
    ("1.5", Decimal("1.5")),
    (".5", Decimal("0.5")),
    ("2.", Decimal(2)),
    # Milli — the suffix that costs three orders of magnitude when dropped.
    ("15800m", Decimal("15.8")),
    ("100m", Decimal("0.1")),
    ("350m", Decimal("0.35")),
    ("1n", Decimal("1e-9")),
    ("1u", Decimal("1e-6")),
    # Binary SI. Whole tokens, never a single character.
    ("1Ki", Decimal(1024)),
    ("64Gi", Decimal(68719476736)),
    ("2Ti", Decimal(1024**4) * 2),
    ("1Mi", Decimal(1024**2)),
    ("1Pi", Decimal(1024**5)),
    ("1Ei", Decimal(1024**6)),
    ("1.5Ki", Decimal(1536)),
    # Decimal SI, which is what `M` means and it is not `Mi`.
    ("500k", Decimal(500_000)),
    ("100M", Decimal(100_000_000)),
    ("1G", Decimal(1_000_000_000)),
    ("5E", Decimal("5e18")),
    # Uppercase K: not canonical (SI reserves K for kelvin and the API server
    # rejects it), accepted because the only place it can arrive from is a
    # hand-authored file and it has exactly one possible reading.
    ("500K", Decimal(500_000)),
    # Decimal exponent form, which the API server emits for large values.
    ("1e3", Decimal(1000)),
    ("129e6", Decimal(129_000_000)),
    ("1.5e2", Decimal(150)),
    ("1E3", Decimal(1000)),
    ("2e-3", Decimal("0.002")),
    # Whitespace around a value is tolerated; whitespace inside it is not.
    ("  64Gi  ", Decimal(68719476736)),
]

INVALID = [
    "",
    "   ",
    "abc",
    "Gi",             # suffix with no number
    "12Xi",           # not a binary suffix
    "1Gib",           # trailing junk
    "1KI",            # wrong case: K is decimal, Ki is binary, KI is neither
    "1e",             # exponent with no exponent
    "e3",
    "1.2.3",
    "12 Mi",          # internal whitespace
    "1Ki2",
    "--1",
    "1Ki1Ki",
    "0x10",
    "NaN",
    "Infinity",
]


@pytest.mark.parametrize(("text", "expected"), VALID, ids=[case[0] for case in VALID])
def test_parses_the_grammar_exactly(text, expected):
    assert parse_quantity(text) == expected


@pytest.mark.parametrize("text", INVALID, ids=[repr(case) for case in INVALID])
def test_refuses_to_invent_a_magnitude(text):
    """Unparseable is ``None``, which renders as an em dash.

    Never ``0``: on a capacity column that is the difference between "we do not
    know how big this node is" and "this node is empty", and the second one gets
    workloads scheduled onto it.
    """
    assert parse_quantity(text) is None


def test_none_and_bool_are_not_quantities():
    """``True`` is an ``int`` in Python, and one core is not a plausible reading of it."""
    assert parse_quantity(None) is None
    assert parse_quantity(True) is None
    assert parse_quantity(False) is None


def test_already_parsed_numbers_pass_through():
    """The dynamic client hands back a bare ``110`` for ``capacity.pods``."""
    assert parse_quantity(110) == Decimal(110)
    assert parse_quantity(1.5) == Decimal("1.5")
    # str() first, so a float does not arrive as 0.1000000000000000055511151231.
    assert parse_quantity(0.1) == Decimal("0.1")


# --------------------------------------------------------------------------- #
# The readings built on top of it
# --------------------------------------------------------------------------- #

def test_binary_and_decimal_suffixes_are_not_interchangeable():
    """The 4.8% that turns into 19 GB of phantom headroom on a large node."""
    assert memory_bytes("100Mi") == 104857600
    assert memory_bytes("100M") == 100000000
    assert memory_bytes("1Gi") != memory_bytes("1G")


def test_memory_beyond_float_precision_is_exact():
    """Integer bytes, not a float that rounds at 2^53.

    A float round-trip of this value lands on ...456768. The number is a byte
    count an operator may compare against another tool's output, and an
    unexplainable off-by-21 is how a console loses their trust over something
    that is not even the interesting part of the page.
    """
    assert memory_bytes("1234567890123456789") == 1234567890123456789
    assert parse_quantity("1Ei") == Decimal(1024**6)


def test_memory_rounds_up_like_apimachinery():
    """``Quantity.Value()`` rounds up, so this does, so the two agree."""
    assert memory_bytes("1.5") == 2
    assert memory_bytes("1.5Ki") == 1536
    assert memory_bytes("0.2") == 1


def test_cpu_milli_is_thousandths_not_units():
    assert cpu_cores("15800m") == 15.8
    assert cpu_cores("100m") == 0.1
    assert cpu_cores("16") == 16.0
    assert cpu_cores("nonsense") is None


def test_pod_capacity_is_an_integer_slot_count():
    assert pod_capacity("110") == 110
    assert pod_capacity(110) == 110
    assert pod_capacity(None) is None


def test_summing_milli_cpu_does_not_drift():
    """Three pods at 350m are 1.05 cores, not 1.0499999999999998.

    This is the reason the parser returns ``Decimal`` rather than ``float``. The
    drift is invisible at three pods and obvious at a hundred, which is exactly
    the size of node where somebody is reading this column to decide something.
    """
    pods = [
        {
            "status": {"phase": "Running"},
            "spec": {"containers": [{"resources": {"requests": {"cpu": "350m"}}}]},
        }
        for _ in range(3)
    ]
    usage = node_usage(pods)
    assert usage["requested"]["cpu_cores"] == 1.05
    assert usage["pod_count"] == 3


def test_an_unparseable_request_poisons_its_dimension_only():
    """A request we could not read makes the total unknown, not smaller.

    Silently excluding it would report *more* free capacity than the node has,
    and more free capacity is what gets something scheduled onto it. Memory is
    unaffected, because that dimension was read cleanly.
    """
    pods = [
        {
            "status": {"phase": "Running"},
            "spec": {"containers": [{"resources": {"requests": {"cpu": "2", "memory": "1Gi"}}}]},
        },
        {
            "status": {"phase": "Running"},
            "spec": {"containers": [{"resources": {"requests": {"cpu": "banana", "memory": "1Gi"}}}]},
        },
    ]
    usage = node_usage(pods)
    assert usage["requested"]["cpu_cores"] is None
    assert usage["requested"]["memory_bytes"] == 2 * 1024**3
    assert usage["pod_count"] == 2
