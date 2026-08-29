"""Image-reference validation shared by administrative container features."""

from __future__ import annotations

import re

from app.errors import Invalid

_IMAGE_FORBIDDEN = re.compile(r"[\s\x00-\x1f]")
_MAX_IMAGE_REFERENCE_LENGTH = 512


def validate_image_reference(
    image: str | None,
    *,
    missing_message: str,
    missing_hint: str,
) -> str:
    """Return a trimmed image reference or raise the stable validation error."""
    value = (image or "").strip()
    if not value:
        raise Invalid(
            missing_message,
            hint=missing_hint,
            context={"parameter": "image"},
        )
    if len(value) > _MAX_IMAGE_REFERENCE_LENGTH:
        raise Invalid(
            f"That image reference is {len(value)} characters long "
            f"(limit {_MAX_IMAGE_REFERENCE_LENGTH}).",
            context={
                "parameter": "image",
                "length": len(value),
                "limit": _MAX_IMAGE_REFERENCE_LENGTH,
            },
        )
    if _IMAGE_FORBIDDEN.search(value):
        raise Invalid(
            "An image reference cannot contain whitespace or control characters.",
            detail=f"received {value!r}",
            hint="This is usually a line break picked up by a copy and paste.",
            context={"parameter": "image", "value": value},
        )
    return value


__all__ = ["validate_image_reference"]
