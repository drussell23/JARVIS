"""SkyLight true-Space attribution — native, dynamically resolved.

Operator authorization 2026-07-19. Replaces the coarse PID-bucketing
heuristic ("17 pseudo-Spaces from 71 windows") with the operator's
TRUE Mission-Control Space IDs, via Apple's private
CoreGraphics/SkyLight symbols.

Mandate 1+2 — Dynamic Symbol Resolution with graceful degradation:
``CGSCopySpacesForWindows`` / ``CGSGetActiveSpace`` are private
undocumented symbols that CAN vanish in a macOS update. They are
resolved at RUNTIME (never a static import) — this module reuses the
EXISTING ``MacOSSpaceDetector`` (which already dynamically loads the
CoreGraphics bundle + resolves the CGS symbol map with its own
``_private_api_available`` capability flag). If resolution fails or
throws, :func:`true_windows_by_space` returns ``None`` and the caller
falls back to PID bucketing — the FSM never crashes.

DRY (mandate 3): zero new ctypes/bundle-load code; the detector's
resolved symbol map IS the binding.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.SkyLightSpaces")

_DETECTOR: Any = None
_RESOLUTION_FAILED = False


def _get_detector() -> Any:
    """Lazily build (once) the existing MacOSSpaceDetector — it does
    the dynamic bundle load + CGS symbol resolution internally. A
    resolution fault is remembered so we never re-pay it. NEVER
    raises; returns None on any failure (→ caller degrades)."""
    global _DETECTOR, _RESOLUTION_FAILED
    if _RESOLUTION_FAILED:
        return None
    if _DETECTOR is not None:
        return _DETECTOR
    try:
        from backend.vision.macos_space_detector import (  # noqa: PLC0415
            MacOSSpaceDetector,
        )
        det = MacOSSpaceDetector()
        # The detector sets _private_api_available after its own
        # dynamic resolution — the honest capability signal.
        if not getattr(det, "_private_api_available", False):
            _RESOLUTION_FAILED = True
            logger.info(
                "[SkyLight] private Space API unavailable — degrading "
                "to PID-bucketing heuristic",
            )
            return None
        _DETECTOR = det
        return det
    except Exception as exc:  # noqa: BLE001 — missing symbol / bundle / import
        _RESOLUTION_FAILED = True
        logger.warning(
            "[SkyLight] symbol resolution failed (%s) — degrading to "
            "PID heuristic", exc,
        )
        return None


def _reset_for_tests() -> None:
    global _DETECTOR, _RESOLUTION_FAILED
    _DETECTOR = None
    _RESOLUTION_FAILED = False


def skylight_available() -> bool:
    """True iff the true-Space API resolved. NEVER raises."""
    return _get_detector() is not None


def true_windows_by_space(
    raw_windows: List[dict],
) -> Optional[Dict[int, List[dict]]]:
    """Attribute each raw CGWindow to its TRUE Mission-Control Space
    via the resolved private API. Returns ``None`` when the API is
    unavailable (caller falls back to PID bucketing) — NEVER raises,
    NEVER a partial/garbage map.

    ``raw_windows`` are the ``CGWindowListCopyWindowInfo`` dicts; each
    carries ``kCGWindowNumber``. We ask the detector for the
    window→space mapping through its already-resolved
    ``CGSCopySpacesForWindows`` symbol; a per-window resolution gap
    routes that window to the active Space (never dropped)."""
    det = _get_detector()
    if det is None:
        return None
    try:
        conn = getattr(det, "cgs_connection", None)
        copy_for_windows = getattr(det, "_cgs_copy_spaces_for_windows", None)
        get_active = getattr(det, "_cgs_get_active_space", None)
        if conn is None:
            return None
        active_space = 1
        if callable(get_active):
            try:
                active_space = int(get_active(conn))
            except Exception:  # noqa: BLE001
                active_space = 1
        by_space: Dict[int, List[dict]] = {}
        for w in raw_windows:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            wid = w.get("kCGWindowNumber")
            space_id = active_space
            if callable(copy_for_windows) and wid is not None:
                try:
                    spaces = copy_for_windows(conn, 0x7, [wid])  # all-space mask
                    if spaces:
                        space_id = int(spaces[0])
                except Exception:  # noqa: BLE001
                    space_id = active_space
            by_space.setdefault(space_id, []).append(dict(w))
        return by_space or None
    except Exception:  # noqa: BLE001
        logger.debug("[SkyLight] attribution degraded", exc_info=True)
        return None


__all__ = [
    "skylight_available",
    "true_windows_by_space",
]
