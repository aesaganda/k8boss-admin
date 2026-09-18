"""
Settings defaults that are security decisions rather than conveniences.

A default is what every deployment that never touched the variable is running,
so a default that grants something is a grant nobody reviewed.
"""

from __future__ import annotations

from app.config import Settings


def test_no_cross_origin_access_is_granted_by_default():
    """``allow_credentials=True`` in app.main means any origin listed here may
    make credentialed calls against this console's API. The shipped list used to
    be :5173, :3000 and :8080 — :5173 being K8Boss, a different application
    routinely run on the same machine, which therefore got a credentialed grant
    over this console purely by being the neighbour it was split out of."""
    assert Settings().cors_origins == ""


def test_an_empty_default_reaches_cors_middleware_as_no_origins_at_all():
    """The property the default rests on: '' is not one empty-string origin.
    CORSMiddleware matches an origin against this list literally, so a stray ''
    in it would be a rule that never matches — harmless — but a stray '*' from a
    hand-edited value would not be, and the split is the only thing between the
    two."""
    assert Settings().cors_origin_list == []


def test_configured_origins_survive_whitespace_around_the_commas():
    """Operators write these in a YAML Secret, where a space after a comma is
    invisible and an origin with a leading space matches nothing."""
    settings = Settings(cors_origins="http://a.example , http://b.example")

    assert settings.cors_origin_list == ["http://a.example", "http://b.example"]
