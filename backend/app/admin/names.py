"""The generated-name suffix shared by §5.5, §7.4 and the CLI pod.

One alphabet in one place, because three copies of a character set drift and
the drift is invisible until two features disagree about what a name may
contain.
"""

from __future__ import annotations

import secrets

#: No vowels, so a generated name cannot spell a word, and none of the
#: characters most often misheard when a name is read aloud during an incident:
#: no `l`/`1`, no `o`/`0`. These pods get created under pressure and their names
#: get read down a phone line to somebody typing `kubectl delete`.
_SUFFIX_ALPHABET = "bcdfghjkmnpqrstvwxz23456789"


def random_suffix(length: int = 5) -> str:
    """A short random suffix from the unambiguous alphabet.

    ``secrets`` rather than ``random``: not because a colliding debug-pod name
    is a security failure, but because ``random`` is seeded per process and two
    replicas of this console starting together would generate the same names in
    the same order.
    """
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(length))


__all__ = ["random_suffix"]
