"""MemoryPressureGate — advisory memory-pressure signal for worktree fan-out.

Each L3 worktree is a full working-copy ``git worktree add`` — copy-on-
write but still RAM-resident for the process metadata + any caches
the unit allocates. Under memory pressure, parallel fan-out can OOM
the harness. This module provides an *advisory* signal that the
subagent scheduler (and other consumers) can consult before spawning
N units.

Authority posture
-----------------

* §1 Boundary Principle — **advisory only**. ``can_fanout()`` returns
  a decision; the worktree manager CHOOSES to honor it. The gate does
  not import or reach into any scheduler/subagent module — callers
  pull from the gate on their own cadence.
* §5 Tier 0 — stdlib only; no LLM; probe path uses psutil if present,
  else /proc/meminfo (Linux), else ``vm_stat`` subprocess (Darwin),
  else ``psutil`` via ``subprocess`` fallback, else "OK always".
* §8 Observability — every probe is ``snapshot()``-able; level
  transitions are SSE-publishable via Slice 3 bridge.

Authority invariant (grep-pinned Slice 4): zero imports from
``orchestrator``, ``policy``, ``iron_gate``, ``risk_tier``,
``change_engine``, ``candidate_generator``, ``gate``.

Kill switch
-----------

``JARVIS_MEMORY_PRESSURE_GATE_ENABLED`` (default ``false`` Slice 1-3,
graduates Slice 4). When off, ``pressure()`` returns ``OK`` and
``can_fanout(N)`` returns ``FanoutDecision(allowed=True, n_allowed=N)``
so consumers fall through to the pre-gate status quo.

Thresholds
----------

  OK       : free_pct ≥ 30%
  WARN     : 20% ≤ free_pct < 30%
  HIGH     : 10% ≤ free_pct < 20%
  CRITICAL : free_pct < 10%

Per-level fanout caps:
  OK       : unlimited (n_allowed = n_requested)
  WARN     : 8
  HIGH     : 3
  CRITICAL : 1
"""
from __future__ import annotations

import enum
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


MEMORY_PRESSURE_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated 2026-04-21 via Slice 4 after
    Slices 1-2 shipped probe cascade + fanout decision math + Slice 3
    shipped REPL/GET/SSE surfaces). Explicit ``"false"`` reverts to
    Slice 1-3 deny-by-default posture:

      * pressure() returns OK unconditionally
      * can_fanout(N) returns FanoutDecision(allowed=True, n_allowed=N)
        so consumers fall through to the pre-gate path
      * GET /observability/memory-pressure returns 403
      * /governor memory REPL rejects
      * SSE publish_memory_pressure_event returns None

    Probe cascade (psutil → /proc/meminfo → vm_stat → fallback),
    threshold math, and authority invariants all stay in force
    regardless of flag state.
    """
    return _env_bool("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", True)


def warn_threshold_pct() -> float:
    """free_pct below this → WARN. Default 30.0."""
    return _env_float("JARVIS_MEMORY_PRESSURE_WARN_PCT", 30.0, minimum=1.0)


def high_threshold_pct() -> float:
    """free_pct below this → HIGH. Default 20.0."""
    return _env_float("JARVIS_MEMORY_PRESSURE_HIGH_PCT", 20.0, minimum=1.0)


def critical_threshold_pct() -> float:
    """free_pct below this → CRITICAL. Default 10.0."""
    return _env_float("JARVIS_MEMORY_PRESSURE_CRITICAL_PCT", 10.0, minimum=0.1)


def warn_fanout_cap() -> int:
    """Max parallel units under WARN pressure. Default 8."""
    return _env_int("JARVIS_MEMORY_PRESSURE_WARN_FANOUT_CAP", 8, minimum=1)


def high_fanout_cap() -> int:
    return _env_int("JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP", 3, minimum=1)


def critical_fanout_cap() -> int:
    return _env_int("JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP", 1, minimum=1)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class PressureLevel(str, enum.Enum):
    OK = "ok"
    WARN = "warn"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class MemoryProbe:
    """Result of one probe attempt. ``source`` identifies which cascade
    stage produced the reading for diagnostics."""

    free_pct: float
    total_bytes: int
    available_bytes: int
    source: str
    ok: bool = True
    error: Optional[str] = None


@dataclass(frozen=True)
class FanoutDecision:
    allowed: bool
    n_requested: int
    n_allowed: int
    level: PressureLevel
    free_pct: float
    reason_code: str
    source: str
    schema_version: str = MEMORY_PRESSURE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "allowed": self.allowed,
            "n_requested": self.n_requested,
            "n_allowed": self.n_allowed,
            "level": self.level.value,
            "free_pct": self.free_pct,
            "reason_code": self.reason_code,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Probe cascade
# ---------------------------------------------------------------------------


def _probe_psutil() -> Optional[MemoryProbe]:
    try:
        import psutil  # noqa: F401
    except ImportError:
        return None
    try:
        import psutil
        m = psutil.virtual_memory()
        # psutil.available is 'real available'; percent is used not free
        total = int(m.total)
        avail = int(m.available)
        free_pct = (avail / total * 100.0) if total > 0 else 0.0
        return MemoryProbe(
            free_pct=free_pct, total_bytes=total, available_bytes=avail,
            source="psutil",
        )
    except Exception as exc:  # noqa: BLE001
        return MemoryProbe(
            free_pct=0.0, total_bytes=0, available_bytes=0,
            source="psutil", ok=False, error=str(exc),
        )


def _probe_proc_meminfo() -> Optional[MemoryProbe]:
    """Linux /proc/meminfo parser. Returns None on non-Linux or missing file."""
    path = "/proc/meminfo"
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return MemoryProbe(
            free_pct=0.0, total_bytes=0, available_bytes=0,
            source="proc_meminfo", ok=False, error=str(exc),
        )
    # Parse MemTotal / MemAvailable (kB)
    total_kb = 0
    avail_kb = 0
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            m = re.search(r"(\d+)", line)
            if m:
                total_kb = int(m.group(1))
        elif line.startswith("MemAvailable:"):
            m = re.search(r"(\d+)", line)
            if m:
                avail_kb = int(m.group(1))
    if total_kb == 0:
        return MemoryProbe(
            free_pct=0.0, total_bytes=0, available_bytes=0,
            source="proc_meminfo", ok=False, error="zero total",
        )
    total = total_kb * 1024
    avail = avail_kb * 1024
    free_pct = (avail / total * 100.0) if total > 0 else 0.0
    return MemoryProbe(
        free_pct=free_pct, total_bytes=total, available_bytes=avail,
        source="proc_meminfo",
    )


def _probe_vm_stat() -> Optional[MemoryProbe]:
    """Darwin ``vm_stat`` subprocess parser. None on non-Darwin or on
    subprocess failure."""
    if not sys.platform.startswith("darwin"):
        return None
    try:
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=3.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return MemoryProbe(
            free_pct=0.0, total_bytes=0, available_bytes=0,
            source="vm_stat", ok=False, error=str(exc),
        )
    if result.returncode != 0:
        return MemoryProbe(
            free_pct=0.0, total_bytes=0, available_bytes=0,
            source="vm_stat", ok=False, error=f"rc={result.returncode}",
        )
    # Parse page size + pages free
    page_size = 4096
    pages_free = 0
    pages_active = 0
    pages_inactive = 0
    pages_wired = 0
    pages_speculative = 0
    for line in result.stdout.splitlines():
        m = re.match(r"Mach Virtual Memory Statistics: \(page size of (\d+) bytes", line)
        if m:
            page_size = int(m.group(1))
            continue
        for key, var_name in (
            ("Pages free:", "pages_free"),
            ("Pages active:", "pages_active"),
            ("Pages inactive:", "pages_inactive"),
            ("Pages wired down:", "pages_wired"),
            ("Pages speculative:", "pages_speculative"),
        ):
            if line.startswith(key):
                num = re.search(r"(\d+)", line)
                if num:
                    if var_name == "pages_free":
                        pages_free = int(num.group(1))
                    elif var_name == "pages_active":
                        pages_active = int(num.group(1))
                    elif var_name == "pages_inactive":
                        pages_inactive = int(num.group(1))
                    elif var_name == "pages_wired":
                        pages_wired = int(num.group(1))
                    elif var_name == "pages_speculative":
                        pages_speculative = int(num.group(1))
    total_pages = (pages_free + pages_active + pages_inactive
                   + pages_wired + pages_speculative)
    if total_pages == 0:
        return MemoryProbe(
            free_pct=0.0, total_bytes=0, available_bytes=0,
            source="vm_stat", ok=False, error="zero total pages",
        )
    total = total_pages * page_size
    # On Darwin, "available" ≈ free + inactive + speculative (inactive
    # pages are reclaimable). Closer to psutil.available semantics.
    avail = (pages_free + pages_inactive + pages_speculative) * page_size
    free_pct = (avail / total * 100.0)
    return MemoryProbe(
        free_pct=free_pct, total_bytes=total, available_bytes=avail,
        source="vm_stat",
    )


def _probe_fallback() -> MemoryProbe:
    """Last-resort fallback — no memory info available. Report 100% free
    (OK) so the gate doesn't block on platforms where we can't probe."""
    return MemoryProbe(
        free_pct=100.0, total_bytes=0, available_bytes=0,
        source="fallback", ok=True,
    )


_PROBE_CASCADE = (_probe_psutil, _probe_proc_meminfo, _probe_vm_stat)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class MemoryPressureGate:
    """Advisory memory-pressure decision provider.

    Consumers call ``pressure()`` for a level enum or ``can_fanout(n)``
    for a decision object. The gate caches nothing — each call triggers
    a fresh probe so environmental changes (e.g. psutil-observed swap
    pressure after GC) reflect immediately.
    """

    def __init__(
        self,
        *,
        probe_fn: Optional[Callable[[], MemoryProbe]] = None,
    ) -> None:
        # Tests inject a custom probe_fn; production uses the cascade
        self._probe_fn = probe_fn or self._cascaded_probe
        self._lock = threading.Lock()

    # -- probe --------------------------------------------------------------

    @staticmethod
    def _cascaded_probe() -> MemoryProbe:
        for fn in _PROBE_CASCADE:
            result = fn()
            if result is not None and result.ok:
                return result
        return _probe_fallback()

    def probe(self) -> MemoryProbe:
        """Invoke the cascade and return the raw probe."""
        return self._probe_fn()

    # -- level --------------------------------------------------------------

    def level_for_free_pct(self, free_pct: float) -> PressureLevel:
        if free_pct < critical_threshold_pct():
            return PressureLevel.CRITICAL
        if free_pct < high_threshold_pct():
            return PressureLevel.HIGH
        if free_pct < warn_threshold_pct():
            return PressureLevel.WARN
        return PressureLevel.OK

    def pressure(self) -> PressureLevel:
        """Current pressure level."""
        if not is_enabled():
            return PressureLevel.OK
        try:
            probe = self._probe_fn()
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryPressureGate] probe raised", exc_info=True)
            return PressureLevel.OK
        if not probe.ok:
            return PressureLevel.OK
        return self.level_for_free_pct(probe.free_pct)

    # -- fanout decision ----------------------------------------------------

    def _cap_for_level(self, level: PressureLevel) -> Optional[int]:
        """None = unlimited."""
        if level is PressureLevel.OK:
            return None
        if level is PressureLevel.WARN:
            return warn_fanout_cap()
        if level is PressureLevel.HIGH:
            return high_fanout_cap()
        if level is PressureLevel.CRITICAL:
            return critical_fanout_cap()
        return None

    def can_fanout(self, n_requested: int) -> FanoutDecision:
        """Advisory: may ``n_requested`` parallel units proceed?

        Returns ``FanoutDecision`` with:
          * ``allowed`` — True if n_allowed >= 1 (i.e. at least some
            forward progress is permitted)
          * ``n_allowed`` — clamp to level's cap; 0 only when
            n_requested=0 (degenerate request)
          * ``level`` — current pressure level
          * ``source`` — probe source ("psutil" / "proc_meminfo" / ...)
        """
        n_requested = max(0, int(n_requested))
        if not is_enabled():
            return FanoutDecision(
                allowed=True, n_requested=n_requested, n_allowed=n_requested,
                level=PressureLevel.OK, free_pct=100.0,
                reason_code="memory_pressure_gate.disabled",
                source="disabled",
            )
        try:
            probe = self._probe_fn()
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryPressureGate] probe raised", exc_info=True)
            return FanoutDecision(
                allowed=True, n_requested=n_requested, n_allowed=n_requested,
                level=PressureLevel.OK, free_pct=100.0,
                reason_code="memory_pressure_gate.probe_failed",
                source="fallback",
            )
        if not probe.ok:
            return FanoutDecision(
                allowed=True, n_requested=n_requested, n_allowed=n_requested,
                level=PressureLevel.OK, free_pct=100.0,
                reason_code="memory_pressure_gate.probe_unreliable",
                source=probe.source,
            )

        level = self.level_for_free_pct(probe.free_pct)
        cap = self._cap_for_level(level)
        if cap is None:
            n_allowed = n_requested
            reason = "memory_pressure_gate.ok"
        else:
            n_allowed = min(n_requested, cap)
            reason = f"memory_pressure_gate.capped_to_{cap}_at_{level.value}"
        return FanoutDecision(
            allowed=n_allowed >= 1 if n_requested >= 1 else True,
            n_requested=n_requested, n_allowed=n_allowed,
            level=level, free_pct=probe.free_pct,
            reason_code=reason, source=probe.source,
        )

    # -- diagnostics --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        try:
            probe = self._probe_fn()
        except Exception as exc:  # noqa: BLE001
            return {
                "schema_version": MEMORY_PRESSURE_SCHEMA_VERSION,
                "enabled": is_enabled(),
                "ok": False, "error": str(exc),
                "thresholds": {
                    "warn_pct": warn_threshold_pct(),
                    "high_pct": high_threshold_pct(),
                    "critical_pct": critical_threshold_pct(),
                },
            }
        level = self.level_for_free_pct(probe.free_pct) if probe.ok else PressureLevel.OK
        return {
            "schema_version": MEMORY_PRESSURE_SCHEMA_VERSION,
            "enabled": is_enabled(),
            "probe": {
                "free_pct": probe.free_pct,
                "total_bytes": probe.total_bytes,
                "available_bytes": probe.available_bytes,
                "source": probe.source,
                "ok": probe.ok,
                "error": probe.error,
            },
            "level": level.value,
            "thresholds": {
                "warn_pct": warn_threshold_pct(),
                "high_pct": high_threshold_pct(),
                "critical_pct": critical_threshold_pct(),
            },
            "fanout_caps": {
                "warn": warn_fanout_cap(),
                "high": high_fanout_cap(),
                "critical": critical_fanout_cap(),
            },
        }


# ---------------------------------------------------------------------------
# Singleton + FlagRegistry bridge
# ---------------------------------------------------------------------------


_default_gate: Optional[MemoryPressureGate] = None
_singleton_lock = threading.Lock()
_flags_registered = False


def get_default_gate() -> MemoryPressureGate:
    global _default_gate
    with _singleton_lock:
        if _default_gate is None:
            _default_gate = MemoryPressureGate()
        return _default_gate


def reset_default_gate() -> None:
    global _default_gate, _flags_registered
    with _singleton_lock:
        _default_gate = None
        _flags_registered = False


def ensure_bridged() -> MemoryPressureGate:
    """Idempotent Wave 1 #2 bridge — registers own flags in FlagRegistry."""
    global _flags_registered
    gate = get_default_gate()
    with _singleton_lock:
        if _flags_registered:
            return gate
        _flags_registered = True
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType, Relevance, ensure_seeded,
        )
        fr = ensure_seeded()
        for spec in _own_flag_specs():
            fr.register(spec, override=True)
    except ImportError:
        pass
    return gate


def _own_flag_specs() -> List[Any]:
    from backend.core.ouroboros.governance.flag_registry import (
        Category, FlagSpec, FlagType, Relevance,
    )
    _all_postures_critical = {
        "EXPLORE": Relevance.CRITICAL, "CONSOLIDATE": Relevance.CRITICAL,
        "HARDEN": Relevance.CRITICAL, "MAINTAIN": Relevance.CRITICAL,
    }
    return [
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_GATE_ENABLED",
            type=FlagType.BOOL, default=False,
            description=(
                "Master kill switch for the MemoryPressureGate — advisory "
                "signal for worktree fan-out and other memory-intensive "
                "parallel ops."
            ),
            category=Category.SAFETY,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="true", since="v1.0",
            posture_relevance=_all_postures_critical,
        ),
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_WARN_PCT",
            type=FlagType.FLOAT, default=30.0,
            description="free_pct below this → WARN level",
            category=Category.TUNING,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="30.0", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_HIGH_PCT",
            type=FlagType.FLOAT, default=20.0,
            description="free_pct below this → HIGH level",
            category=Category.TUNING,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="20.0", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_CRITICAL_PCT",
            type=FlagType.FLOAT, default=10.0,
            description="free_pct below this → CRITICAL level",
            category=Category.TUNING,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="10.0", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_WARN_FANOUT_CAP",
            type=FlagType.INT, default=8,
            description="Max parallel worktree units under WARN pressure",
            category=Category.CAPACITY,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="8", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP",
            type=FlagType.INT, default=3,
            description="Max parallel worktree units under HIGH pressure",
            category=Category.CAPACITY,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="3", since="v1.0",
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ),
        FlagSpec(
            name="JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP",
            type=FlagType.INT, default=1,
            description="Max parallel worktree units under CRITICAL pressure",
            category=Category.CAPACITY,
            source_file="backend/core/ouroboros/governance/memory_pressure_gate.py",
            example="1", since="v1.0",
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ),
    ]


__all__ = [
    "FanoutDecision",
    "MEMORY_PRESSURE_SCHEMA_VERSION",
    "MemoryProbe",
    "MemoryPressureGate",
    "PressureLevel",
    "critical_fanout_cap",
    "critical_threshold_pct",
    "ensure_bridged",
    "get_default_gate",
    "high_fanout_cap",
    "high_threshold_pct",
    "is_enabled",
    "reset_default_gate",
    "warn_fanout_cap",
    "warn_threshold_pct",
]
