"""Neutral text predicates shared by speech accounting and state helpers."""
from __future__ import annotations

from typing import Tuple


PLACEHOLDER_MARKERS: Tuple[str, ...] = (
    "model response will be rendered here",
    "response will be rendered here",
    "will be rendered here",
)


def is_placeholder(text: str) -> bool:
    """Return whether text is an AstrBot rendering placeholder, not spoken text."""
    low = str(text or "").strip().lower()
    if not low:
        return False
    return any(marker in low for marker in PLACEHOLDER_MARKERS)
