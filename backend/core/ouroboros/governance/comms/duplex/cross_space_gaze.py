"""Cross-Space Gaze — desktop-topology awareness, FSM-ticked.

Operator authorization 2026-07-19. Elevates O+V from single-screen
gaze to the whole macOS desktop: it orchestrates the EXISTING
``backend.vision.cross_space_context.CrossSpaceContextAnalyzer`` (per-
Space purpose / key-files / relationships / insights) — zero new
window-server code. It runs ONLY on Orchestrator FSM ticks (mandate 1
— no polling loop; a tick that finds nothing new costs one dhash).

Three hardening layers before any payload reaches the LLM context:

  1. **Ghost Filter (mandate 2):** the CoreGraphics window list is
     full of invisible daemon layers. A window survives only if it is
     ``kCGWindowIsOnscreen``, ≥ 100×100 px, and NOT owned by a known
     background process (WindowServer, Dock, …). Env-extendable
     denylist; no hardcoded magic beyond the documented CG defaults.
  2. **Cross-Space Dedup (mandate 2):** Mission-Control mirroring
     shows one window on several Spaces. Windows collapse on a
     composite ``(pid, title)`` key — the LLM never sees the same IDE
     twice and cannot hallucinate a conflict.
  3. **Temporal Focus Decay (mandate 2):** a Space last focused
     beyond ``JARVIS_CROSSSPACE_FOCUS_TTL_S`` (15 min) is STALE — O+V
     may synthesize *descriptively* over it but never PROACTIVELY
     interrupt about it (the Phantom-Space trap), unless the operator
     explicitly asks.

DRY (mandate 3): the SAME dhash pruning as SemanticGaze — if the
composite structural hash across active Spaces is unchanged since the
last tick, the analyzer is not even invoked. Thermal: the SAME
``evolution_permitted`` governor verdict. NEVER raises.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.CrossSpaceGaze")

#: Background processes whose windows are never workflow context.
_GHOST_OWNERS_DEFAULT = frozenset({
    "WindowServer", "Dock", "SystemUIServer", "Spotlight",
    "Notification Center", "Control Center", "Wallpaper",
    "coreautha", "screencapture", "TextInputMenuAgent",
})
_MIN_DIM = 100


def _ghost_owners() -> frozenset:
    extra = os.environ.get("JARVIS_CROSSSPACE_GHOST_OWNERS", "").strip()
    if not extra:
        return _GHOST_OWNERS_DEFAULT
    return _GHOST_OWNERS_DEFAULT | frozenset(
        w.strip() for w in extra.split(",") if w.strip()
    )


def _focus_ttl_s() -> float:
    try:
        return max(30.0, min(7200.0, float(os.environ.get(
            "JARVIS_CROSSSPACE_FOCUS_TTL_S", "900",     # 15 min
        ))))
    except (TypeError, ValueError):
        return 900.0


def is_ghost_window(win: Dict[str, Any]) -> bool:
    """True → discard (mandate 2). A window is real iff on-screen,
    ≥100×100, and not a background daemon. NEVER raises; unreadable
    windows are treated as ghosts (fail-closed against noise)."""
    try:
        if not win.get("kCGWindowIsOnscreen", win.get("is_onscreen", False)):
            return True
        owner = str(win.get("kCGWindowOwnerName", win.get("owner", "")))
        if owner in _ghost_owners():
            return True
        bounds = win.get("kCGWindowBounds", win.get("bounds", {})) or {}
        w = float(bounds.get("Width", bounds.get("width", 0)))
        h = float(bounds.get("Height", bounds.get("height", 0)))
        if w < _MIN_DIM or h < _MIN_DIM:
            return True
        return False
    except Exception:  # noqa: BLE001
        return True


def _window_key(win: Dict[str, Any]) -> Tuple[Any, str]:
    pid = win.get("kCGWindowOwnerPID", win.get("pid", win.get("owner_pid")))
    title = str(win.get("kCGWindowName", win.get("title", "")))
    return (pid, title)


def filter_and_dedup(
    spaces_windows: Dict[int, List[Dict[str, Any]]],
) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[str, int]]:
    """Ghost-filter every window, then dedup across Spaces by
    ``(pid, title)`` — a mirrored window is kept on its FIRST (lowest-
    id) Space only. Returns (clean_spaces, stats). NEVER raises."""
    stats = {"ghosts_dropped": 0, "duplicates_merged": 0, "kept": 0}
    seen: set = set()
    clean: Dict[int, List[Dict[str, Any]]] = {}
    try:
        for space_id in sorted(spaces_windows):
            kept: List[Dict[str, Any]] = []
            for win in spaces_windows[space_id] or []:
                if is_ghost_window(win):
                    stats["ghosts_dropped"] += 1
                    continue
                key = _window_key(win)
                if key in seen:
                    stats["duplicates_merged"] += 1
                    continue
                seen.add(key)
                kept.append(win)
                stats["kept"] += 1
            if kept:
                clean[space_id] = kept
    except Exception:  # noqa: BLE001
        logger.debug("[CrossSpaceGaze] filter degraded", exc_info=True)
    return clean, stats


def _spaces_dhash(clean: Dict[int, List[Dict[str, Any]]]) -> str:
    """Composite structural hash across active Spaces — DRY delta gate
    (mandate 3). Keyed on the surviving (space, pid, title) topology,
    NOT pixels: a workflow synthesis only matters when the WINDOW SET
    changes. NEVER raises."""
    try:
        topo = sorted(
            (sid, str(_window_key(w)))
            for sid, wins in clean.items() for w in wins
        )
        return hashlib.sha256(repr(topo).encode()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return str(time.time())


class CrossSpaceGaze:
    """FSM-ticked desktop synthesizer. Collaborators injected;
    production defaults resolve the existing analyzer + governor."""

    def __init__(
        self,
        *,
        thermal_ok: Optional[Callable[[], bool]] = None,
        analyzer: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._thermal_ok = thermal_ok or self._default_thermal_ok
        self._analyzer = analyzer
        self._clock = clock
        self._last_hash: Optional[str] = None
        self._last_context: Dict[str, Any] = {}
        #: space_id → last foreground-focus monotonic instant.
        self._focus_ts: Dict[int, float] = {}
        self.stats: Dict[str, int] = {
            "ticks": 0, "unchanged_skips": 0, "thermal_skips": 0,
            "syntheses": 0, "proactive_suppressed": 0,
        }

    @staticmethod
    def _default_thermal_ok() -> bool:
        try:
            from .sovereign_governor import evolution_permitted  # noqa: E501,PLC0415
            return evolution_permitted()
        except Exception:  # noqa: BLE001
            return False

    def _get_analyzer(self) -> Any:
        if self._analyzer is None:
            from backend.vision.cross_space_context import (  # noqa: E501,PLC0415
                CrossSpaceContextAnalyzer,
            )
            self._analyzer = CrossSpaceContextAnalyzer()
        return self._analyzer

    def note_focus(self, space_id: int) -> None:
        """Orchestrator focus-change hook — stamps the Space's last
        foreground instant. NEVER raises."""
        try:
            self._focus_ts[int(space_id)] = self._clock()
        except Exception:  # noqa: BLE001
            pass

    def space_is_fresh(self, space_id: int) -> bool:
        """Temporal Focus Decay: within the focus TTL. NEVER raises."""
        try:
            ts = self._focus_ts.get(int(space_id))
            return ts is not None and (self._clock() - ts) <= _focus_ttl_s()
        except Exception:  # noqa: BLE001
            return False

    def tick(
        self,
        spaces_windows: Dict[int, List[Dict[str, Any]]],
        *,
        explicit: bool = False,
    ) -> Dict[str, Any]:
        """One FSM tick (mandate 1 — called BY the orchestrator, never
        a self-loop). ``explicit`` = the operator asked, so stale
        Spaces are allowed and the dhash gate is bypassed.

        Returns ``{synthesized, proactive, context, stats}``. NEVER
        raises."""
        try:
            self.stats["ticks"] += 1
            if not self._thermal_ok():
                self.stats["thermal_skips"] += 1
                return {"synthesized": False, "reason": "thermal_locked"}
            clean, fstats = filter_and_dedup(spaces_windows)
            if not clean:
                return {"synthesized": False, "reason": "no_real_windows",
                        "filter": fstats}
            h = _spaces_dhash(clean)
            if not explicit and h == self._last_hash:
                self.stats["unchanged_skips"] += 1
                return {"synthesized": False, "reason": "unchanged",
                        "context": self._last_context, "filter": fstats}
            self._last_hash = h
            # Feed the EXISTING analyzer (per-space dict shape).
            spaces_data = {
                sid: {"windows": wins} for sid, wins in clean.items()
            }
            try:
                context = self._get_analyzer().analyze_cross_space_context(
                    spaces_data,
                )
            except Exception:  # noqa: BLE001
                logger.debug("[CrossSpaceGaze] analyzer degraded",
                             exc_info=True)
                context = {}
            self._last_context = context
            self.stats["syntheses"] += 1
            # Proactive gate: an insight may only INTERRUPT if every
            # Space it touches is temporally fresh (the Phantom-Space
            # trap) — unless the operator explicitly asked.
            proactive: List[Any] = []
            for insight in (context.get("insights", []) or []):
                spaces = self._insight_spaces(insight)
                if explicit or all(self.space_is_fresh(s) for s in spaces):
                    proactive.append(insight)
                else:
                    self.stats["proactive_suppressed"] += 1
            return {
                "synthesized": True, "proactive": proactive,
                "context": context, "filter": fstats,
                "explicit": explicit,
            }
        except Exception:  # noqa: BLE001
            logger.debug("[CrossSpaceGaze] tick degraded", exc_info=True)
            return {"synthesized": False, "reason": "error"}

    @staticmethod
    def _insight_spaces(insight: Any) -> List[int]:
        try:
            if isinstance(insight, dict):
                return [int(s) for s in insight.get("affected_spaces", [])]
            return [int(s) for s in getattr(insight, "affected_spaces", [])]
        except Exception:  # noqa: BLE001
            return []


__all__ = [
    "CrossSpaceGaze",
    "filter_and_dedup",
    "is_ghost_window",
]
