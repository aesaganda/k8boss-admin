"""Kubernetes resource quantity parsing shared by every capacity surface."""

from __future__ import annotations

import decimal
import logging
import re
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# The grammar from k8s.io/apimachinery/pkg/api/resource:
#
#   <quantity>        ::= <signedNumber><suffix>
#   <suffix>          ::= <binarySI> | <decimalExponent> | <decimalSI>
#   <binarySI>        ::= Ki | Mi | Gi | Ti | Pi | Ei
#   <decimalSI>       ::= n | u | m | "" | k | M | G | T | P | E
#   <decimalExponent> ::= ("e" | "E") <signedNumber>
_QUANTITY_RE = re.compile(
    r"""
    ^
    (?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+))
    (?:
        (?P<binary>Ki|Mi|Gi|Ti|Pi|Ei)
      | (?P<exponent>[eE][+-]?\d+)
      | (?P<decimal>[numkKMGTPE])
    )?
    $
    """,
    re.VERBOSE,
)

_BINARY_FACTOR: dict[str, Decimal] = {
    "Ki": Decimal(1024),
    "Mi": Decimal(1024**2),
    "Gi": Decimal(1024**3),
    "Ti": Decimal(1024**4),
    "Pi": Decimal(1024**5),
    "Ei": Decimal(1024**6),
}

# ``K`` is not canonical, but it has one unambiguous reading in hand-authored
# values. ``Ki`` remains strictly binary.
_DECIMAL_FACTOR: dict[str, Decimal] = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}

# Wide enough that realistic node and cluster totals remain exact.
_CONTEXT = decimal.Context(prec=60)


def add_quantities(left: Decimal, right: Decimal) -> Decimal:
    """Add parsed quantities without falling back to Decimal's default precision."""
    return _CONTEXT.add(left, right)


def parse_quantity(value: Any) -> Decimal | None:
    """Parse the Kubernetes quantity grammar exactly, or return ``None``.

    Numbers sometimes arrive already parsed by the dynamic client. Booleans are
    rejected explicitly because Python otherwise treats them as integers.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None
    match = _QUANTITY_RE.match(text)
    if match is None:
        logger.debug("Unparseable Kubernetes quantity %r", value)
        return None

    number = Decimal(match.group("number"))
    binary = match.group("binary")
    exponent = match.group("exponent")
    decimal_suffix = match.group("decimal")

    if binary:
        return _CONTEXT.multiply(number, _BINARY_FACTOR[binary])
    if exponent:
        return _CONTEXT.multiply(number, Decimal(10) ** int(exponent[1:]))
    if decimal_suffix:
        return _CONTEXT.multiply(number, _DECIMAL_FACTOR[decimal_suffix])
    return number


__all__ = ["add_quantities", "parse_quantity"]
