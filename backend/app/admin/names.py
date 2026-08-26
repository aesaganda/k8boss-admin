"""Random name generation shared by administrative container features."""

from __future__ import annotations

import secrets

_SUFFIX_ALPHABET = "bcdfghjkmnpqrstvwxz23456789"


def random_suffix(length: int = 5) -> str:
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(length))


__all__ = ["random_suffix"]
