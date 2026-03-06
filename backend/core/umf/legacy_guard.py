"""UMF Legacy Guard -- enforceable flag controlling legacy communication paths.

When UMF is authoritative (``JARVIS_UMF_MODE=active``), legacy paths
(Trinity Event Bus, Reactor Bridge) are disabled by default. The
``JARVIS_UMF_LEGACY_ENABLED`` env var can explicitly override this.

Design rules
------------
* Stdlib only -- no third-party or JARVIS imports.
* Pure functions -- no mutable state.
"""
from __future__ import annotations

import os


def is_legacy_enabled() -> bool:
    """Return True if legacy communication paths should be active.

    Logic:
    - If ``JARVIS_UMF_LEGACY_ENABLED`` is explicitly set to ``"true"``, return True.
    - If ``JARVIS_UMF_MODE`` is ``"active"``, return False (legacy disabled).
    - Otherwise (no UMF or shadow mode), return True (legacy enabled).
    """
    explicit = os.environ.get("JARVIS_UMF_LEGACY_ENABLED", "")
    if explicit.lower() == "true":
        return True

    mode = os.environ.get("JARVIS_UMF_MODE", "")
    if mode == "active":
        return False

    return True


def assert_legacy_allowed(caller: str) -> None:
    """Raise ``RuntimeError`` if legacy paths are disabled.

    Parameters
    ----------
    caller:
        Name of the caller for diagnostic messages.
    """
    if not is_legacy_enabled():
        raise RuntimeError(
            f"Legacy path disabled: {caller}. "
            f"UMF is in active mode. Set JARVIS_UMF_LEGACY_ENABLED=true to override."
        )
