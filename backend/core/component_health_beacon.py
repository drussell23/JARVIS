"""backend/core/component_health_beacon.py — Disease 7: per-component progress heartbeats.

The startup watchdog (DMS) fires SIGTERM→SIGKILL when the phase timeout
expires — even if every component is actively making progress.  Without
heartbeats the watchdog cannot distinguish "working slowly" from "hung".

Design:
* ``ComponentHealthBeacon`` — per-component sink; call ``heartbeat()`` during
  long init operations so the DMS sees you're alive.
* ``BeaconRegistry``        — process-wide collection; DMS polls
  ``all_stalled(threshold_s)`` to find genuinely stuck components.
* ``get_beacon_registry()`` — module-level singleton.

All timestamps use ``time.monotonic()``.  Progress percentage (0–100) is
advisory; stall detection depends solely on heartbeat recency.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "BeaconStatus",
    "ProgressUpdate",
    "ComponentHealthBeacon",
    "BeaconRegistry",
    "get_beacon_registry",
]

logger = logging.getLogger(__name__)

_DEFAULT_STALL_S: float = float(os.getenv("JARVIS_BEACON_STALL_S", "30.0"))


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class BeaconStatus(str, enum.Enum):
    IDLE = "idle"          # registered, work not yet started
    WORKING = "working"    # at least one heartbeat received
    STALLED = "stalled"    # no heartbeat within stall threshold (computed)
    COMPLETE = "complete"  # component finished successfully
    FAILED = "failed"      # component reported failure


# ---------------------------------------------------------------------------
# Immutable progress snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressUpdate:
    """One heartbeat snapshot — immutable for safe sharing across tasks."""

    component: str
    status: BeaconStatus
    progress_pct: float          # 0.0–100.0, advisory
    note: str
    mono_ts: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Per-component beacon
# ---------------------------------------------------------------------------


class ComponentHealthBeacon:
    """Heartbeat sink for one component.

    Thread-safe: all attribute writes are scalar GIL-protected assignments on
    CPython.  The DMS reads are non-critical single reads — no torn writes.

    Usage::

        beacon = get_beacon_registry().get_or_create("neural_mesh")
        beacon.heartbeat("loading tokenizer", 10.0)
        # ... load weights ...
        beacon.heartbeat("loading weights", 50.0)
        beacon.complete()
    """

    def __init__(self, component: str) -> None:
        self.component = component
        self._status: BeaconStatus = BeaconStatus.IDLE
        self._progress_pct: float = 0.0
        self._note: str = ""
        self._last_mono: float = time.monotonic()
        self._created_mono: float = self._last_mono
        self._history: List[ProgressUpdate] = []

    # ------------------------------------------------------------------
    # Component-side API
    # ------------------------------------------------------------------

    def heartbeat(self, note: str = "", progress_pct: float = 0.0) -> None:
        """Record liveness.  Call frequently during long initialisation."""
        now = time.monotonic()
        self._status = BeaconStatus.WORKING
        self._progress_pct = max(0.0, min(100.0, progress_pct))
        self._note = note
        self._last_mono = now
        upd = ProgressUpdate(
            component=self.component,
            status=BeaconStatus.WORKING,
            progress_pct=self._progress_pct,
            note=note,
            mono_ts=now,
        )
        self._history.append(upd)
        logger.debug(
            "[Beacon] %s %.0f%% — %s",
            self.component, self._progress_pct, note,
        )

    def complete(self, note: str = "") -> None:
        """Mark initialisation successful."""
        self._status = BeaconStatus.COMPLETE
        self._progress_pct = 100.0
        self._note = note
        self._last_mono = time.monotonic()
        logger.info("[Beacon] %s COMPLETE — %s", self.component, note or "ok")

    def fail(self, message: str = "") -> None:
        """Mark initialisation failed."""
        self._status = BeaconStatus.FAILED
        self._note = message
        self._last_mono = time.monotonic()
        logger.error("[Beacon] %s FAILED — %s", self.component, message)

    # ------------------------------------------------------------------
    # DMS-side API
    # ------------------------------------------------------------------

    def stall_seconds(self) -> float:
        """Elapsed seconds since the last heartbeat (always ≥ 0)."""
        return max(0.0, time.monotonic() - self._last_mono)

    def is_stalled(self, threshold_s: float = _DEFAULT_STALL_S) -> bool:
        """True if working and no heartbeat for *threshold_s* seconds.

        COMPLETE/FAILED beacons are never stalled — they are done.
        """
        if self._status in (BeaconStatus.COMPLETE, BeaconStatus.FAILED):
            return False
        return self.stall_seconds() >= threshold_s

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def status(self) -> BeaconStatus:
        return self._status

    @property
    def progress_pct(self) -> float:
        return self._progress_pct

    @property
    def note(self) -> str:
        return self._note

    @property
    def last_heartbeat_mono(self) -> float:
        return self._last_mono

    def snapshot(self) -> ProgressUpdate:
        """Current state as an immutable ProgressUpdate."""
        return ProgressUpdate(
            component=self.component,
            status=self._status,
            progress_pct=self._progress_pct,
            note=self._note,
            mono_ts=self._last_mono,
        )

    def history(self) -> List[ProgressUpdate]:
        """Copy of all recorded updates."""
        return list(self._history)


# ---------------------------------------------------------------------------
# Process-wide registry
# ---------------------------------------------------------------------------


class BeaconRegistry:
    """Manages ComponentHealthBeacon instances for all startup components."""

    def __init__(self) -> None:
        self._beacons: Dict[str, ComponentHealthBeacon] = {}

    def get_or_create(self, component: str) -> ComponentHealthBeacon:
        """Return existing beacon or create and register a new one."""
        if component not in self._beacons:
            self._beacons[component] = ComponentHealthBeacon(component)
            logger.debug("[BeaconRegistry] registered '%s'", component)
        return self._beacons[component]

    def get(self, component: str) -> Optional[ComponentHealthBeacon]:
        return self._beacons.get(component)

    def all_stalled(self, threshold_s: float = _DEFAULT_STALL_S) -> List[ComponentHealthBeacon]:
        """Components with no heartbeat within *threshold_s* seconds."""
        return [b for b in self._beacons.values() if b.is_stalled(threshold_s)]

    def all_working(self) -> List[ComponentHealthBeacon]:
        return [b for b in self._beacons.values() if b.status == BeaconStatus.WORKING]

    def all_completed(self) -> List[ComponentHealthBeacon]:
        return [b for b in self._beacons.values() if b.status == BeaconStatus.COMPLETE]

    def all_failed(self) -> List[ComponentHealthBeacon]:
        return [b for b in self._beacons.values() if b.status == BeaconStatus.FAILED]

    def snapshot(self) -> Dict[str, ProgressUpdate]:
        """Immutable snapshot of all beacons."""
        return {name: b.snapshot() for name, b in self._beacons.items()}

    def reset(self) -> None:
        """Clear all beacons — call between DMS restart cycles."""
        count = len(self._beacons)
        self._beacons.clear()
        logger.info("[BeaconRegistry] reset — cleared %d beacons", count)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_registry: Optional[BeaconRegistry] = None


def get_beacon_registry() -> BeaconRegistry:
    """Return (lazily creating) the process-wide BeaconRegistry."""
    global _g_registry
    if _g_registry is None:
        _g_registry = BeaconRegistry()
    return _g_registry
