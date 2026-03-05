"""
Boundary adapters for vision intelligence subsystem.

These normalize untyped data at ingestion boundaries.
Applied at the boundary (once), not scattered across call sites.
"""
from typing import Any


def safe_state_key(key: Any) -> str:
    """Normalize state transition keys at ingestion boundary.

    Used where dict keys may be None (e.g., transition_matrix iteration).
    """
    if key is None:
        return "__none__"
    return str(key)


def safe_text(value: Any) -> str:
    """Normalize text values at ingestion boundary.

    Used where text from external sources may be None.
    """
    if value is None:
        return ""
    return str(value)
