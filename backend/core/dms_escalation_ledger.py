"""backend/core/dms_escalation_ledger.py — Nuances 5 + 11: progress-aware persistent DMS ledger.

Problems solved
---------------
Nuance 5 — DMS escalation ignores actual component progress
    DMS escalates warn → diagnostic → restart on fixed 60-second intervals
    regardless of whether components are actively making progress.  A component
    that starts legitimate heavy work at T+55s is interrupted at T+60s because
    the escalation timer doesn't know about beacon heartbeats.

Nuance 11 — DMS escalation counters reset on manual Supervisor restart
    ``_warnings_issued``, ``_diagnostics_dumped``, ``_restarts_attempted`` are
    per-phase in-memory counters.  A manual Supervisor restart (not via DMS)
    resets them, granting 3 more restart attempts before rollback.  The safety
    gate is escapable through normal operational behaviour.

Design
------
* ``DmsEscalationLedger`` — durable (file-backed) restart counter plus a
  beacon-aware escalation timer:
  - ``restart_count`` persists across process restarts via a JSON file.
    Path comes from ``JARVIS_DMS_LEDGER_PATH`` env (never hardcoded).
  - ``should_escalate(component)`` returns ``False`` when a fresh beacon
    heartbeat has been observed within the current escalation interval,
    preventing spurious escalation on busy-but-alive components.
  - ``reset_escalation_timer(component)`` is called whenever a beacon
    reports progress.
  - ``record_restart(component)`` increments the persistent counter and
    flushes atomically (write-temp, os.replace).

* ``get_dms_escalation_ledger()`` — process-wide singleton.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

__all__ = [
    "EscalationTier",
    "EscalationRecord",
    "DmsEscalationLedger",
    "get_dms_escalation_ledger",
]

logger = logging.getLogger(__name__)

_DEFAULT_ESCALATION_INTERVAL_S: float = float(
    os.getenv("JARVIS_DMS_ESCALATION_INTERVAL_S", "60.0")
)
_DEFAULT_LEDGER_PATH: Path = Path(
    os.getenv("JARVIS_DMS_LEDGER_PATH", "~/.jarvis/dms_escalation_ledger.json")
).expanduser()


class EscalationTier(int):
    """Escalation tier constants (numeric so they can be compared with >)."""
    NONE = 0
    WARN = 1
    DIAGNOSTIC = 2
    RESTART = 3
    ROLLBACK = 4


@dataclass
class EscalationRecord:
    """Per-component escalation state.  Mix of in-memory and persisted fields."""

    component: str
    restart_count: int = 0             # persisted — survives process restarts
    escalation_tier: int = EscalationTier.NONE
    last_escalation_mono: float = field(default_factory=time.monotonic)
    last_heartbeat_mono: float = field(default_factory=time.monotonic)
    last_heartbeat_progress_pct: float = 0.0


class DmsEscalationLedger:
    """Durable, beacon-aware escalation ledger for the Dead Man's Switch.

    Usage::

        ledger = get_dms_escalation_ledger()

        # When a beacon heartbeat arrives:
        ledger.reset_escalation_timer("neural_mesh")

        # In the DMS polling loop:
        if ledger.should_escalate("neural_mesh"):
            ledger.record_restart("neural_mesh")
            # ... trigger restart
    """

    def __init__(
        self,
        ledger_path: Optional[Path] = None,
        escalation_interval_s: Optional[float] = None,
    ) -> None:
        self._path = ledger_path or _DEFAULT_LEDGER_PATH
        self._interval_s = escalation_interval_s or _DEFAULT_ESCALATION_INTERVAL_S
        self._records: Dict[str, EscalationRecord] = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Component-side API (called when beacon heartbeats arrive)
    # ------------------------------------------------------------------

    def reset_escalation_timer(
        self, component: str, progress_pct: float = 0.0
    ) -> None:
        """Record a fresh heartbeat for *component*, resetting escalation clock.

        Call this whenever ``BeaconRegistry.get(component).heartbeat()`` fires.
        """
        with self._lock:
            rec = self._get_or_create(component)
            now = time.monotonic()
            rec.last_heartbeat_mono = now
            rec.last_escalation_mono = now  # reset escalation window
            rec.last_heartbeat_progress_pct = progress_pct
        logger.debug(
            "[DmsLedger] '%s' escalation timer reset (progress=%.0f%%)",
            component, progress_pct,
        )

    # ------------------------------------------------------------------
    # DMS-side API (called from watchdog polling loop)
    # ------------------------------------------------------------------

    def should_escalate(self, component: str) -> bool:
        """Return ``True`` if *component* should be escalated.

        Returns ``False`` when a beacon heartbeat was received within the
        current escalation interval — meaning the component is alive and
        progressing.  This prevents the DMS from interrupting genuine progress.
        """
        with self._lock:
            rec = self._get_or_create(component)
            elapsed_since_heartbeat = time.monotonic() - rec.last_heartbeat_mono
            elapsed_since_escalation = time.monotonic() - rec.last_escalation_mono

            # If the component sent a heartbeat within the interval, do NOT escalate.
            if elapsed_since_heartbeat < self._interval_s:
                return False

            # No heartbeat within interval: escalate if the escalation clock expired.
            return elapsed_since_escalation >= self._interval_s

    def record_restart(self, component: str) -> int:
        """Increment persistent restart counter and flush to disk.

        Returns the new restart count.
        """
        with self._lock:
            rec = self._get_or_create(component)
            rec.restart_count += 1
            rec.escalation_tier = min(
                rec.escalation_tier + 1, EscalationTier.ROLLBACK
            )
            rec.last_escalation_mono = time.monotonic()
            count = rec.restart_count
        self.flush()
        logger.warning(
            "[DmsLedger] '%s' restart #%d recorded (tier=%d)",
            component, count, self._records[component].escalation_tier,
        )
        return count

    def get_restart_count(self, component: str) -> int:
        with self._lock:
            rec = self._records.get(component)
            return rec.restart_count if rec is not None else 0

    def reset_component(self, component: str) -> None:
        """Reset in-memory escalation state for *component* (NOT restart_count).

        Call at the start of a new phase attempt to clear tier/timer state while
        preserving the persistent restart count.
        """
        with self._lock:
            rec = self._get_or_create(component)
            now = time.monotonic()
            rec.escalation_tier = EscalationTier.NONE
            rec.last_escalation_mono = now
            rec.last_heartbeat_mono = now

    def reset_all_timers(self) -> None:
        """Reset all escalation timers (e.g., at start of a new DMS cycle)."""
        with self._lock:
            now = time.monotonic()
            for rec in self._records.values():
                rec.last_escalation_mono = now
                rec.last_heartbeat_mono = now

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Persist restart counts atomically (write-tmp, os.replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            name: {"restart_count": rec.restart_count}
            for name, rec in self._records.items()
        }
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("[DmsLedger] flush failed: %s", exc)

    def _load(self) -> None:
        """Load persisted restart counts from disk (best-effort)."""
        try:
            raw = json.loads(self._path.read_text())
            for name, entry in raw.items():
                rec = self._get_or_create(name)
                rec.restart_count = int(entry.get("restart_count", 0))
            logger.info(
                "[DmsLedger] loaded %d component record(s) from %s",
                len(raw), self._path,
            )
        except FileNotFoundError:
            pass  # First run — no ledger file yet
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("[DmsLedger] failed to load ledger: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, component: str) -> EscalationRecord:
        """Return existing record or create a new one (caller holds _lock)."""
        if component not in self._records:
            self._records[component] = EscalationRecord(component=component)
        return self._records[component]

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def escalation_interval_s(self) -> float:
        return self._interval_s

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """Return a copy of all records as plain dicts."""
        with self._lock:
            return {
                name: {
                    "restart_count": rec.restart_count,
                    "escalation_tier": rec.escalation_tier,
                    "stall_s": time.monotonic() - rec.last_heartbeat_mono,
                }
                for name, rec in self._records.items()
            }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_ledger: Optional[DmsEscalationLedger] = None


def get_dms_escalation_ledger() -> DmsEscalationLedger:
    """Return (lazily creating) the process-wide DmsEscalationLedger."""
    global _g_ledger
    if _g_ledger is None:
        _g_ledger = DmsEscalationLedger()
    return _g_ledger
