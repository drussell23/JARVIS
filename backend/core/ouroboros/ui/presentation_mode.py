"""backend/core/ouroboros/ui/presentation_mode.py -- COCKPIT vs SOAK skin.

One resolution point for the ov presentation split (spec §3.5). SOAK is the
fail-safe default: every legacy launch path (the battle-test script, ov run,
CI, daemons) keeps byte-identical output unless `ov` cockpit explicitly opts
in via JARVIS_OV_PRESENTATION=cockpit.

Leaf module: stdlib only. The gate this feeds NEVER carries fatal telemetry
(Mandate 1) -- ERROR/CRITICAL paths are emitted unconditionally at their
sources and do not consult this module.
"""
from __future__ import annotations

import enum
import os
from typing import Mapping, Optional

ENV_KEY = "JARVIS_OV_PRESENTATION"


class PresentationMode(str, enum.Enum):
    COCKPIT = "cockpit"   # ov awakening: banners withheld, WARNING logging
    SOAK = "soak"         # legacy verbose harness output (default)


def resolve_presentation_mode(
    env: Optional[Mapping[str, str]] = None,
) -> PresentationMode:
    """Resolve the mode. Unknown/absent values fail safe to SOAK."""
    source = os.environ if env is None else env
    raw = (source.get(ENV_KEY) or "").strip().lower()
    if raw == PresentationMode.COCKPIT.value:
        return PresentationMode.COCKPIT
    return PresentationMode.SOAK


def is_cockpit() -> bool:
    return resolve_presentation_mode() is PresentationMode.COCKPIT


__all__ = ["ENV_KEY", "PresentationMode", "resolve_presentation_mode", "is_cockpit"]
