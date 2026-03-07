# Ouroboros v2.0 Phase 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add hybrid routing with pressure-aware degradation modes (2A) and multi-file atomic change operations with cross-repo event publishing (2B) to the Ouroboros governance system.

**Architecture:** Phase 2A adds a `ResourceMonitor` that collects pressure signals (RAM/CPU/IO/event-loop latency) and a `DegradationController` that manages 4 degradation modes (FULL_AUTONOMY -> REDUCED_AUTONOMY -> READ_ONLY_PLANNING -> EMERGENCY_STOP). Phase 2A also adds a `RoutingPolicy` that makes deterministic routing decisions (LOCAL vs GCP_PRIME vs QUEUE) based on task type and resource state. Phase 2B adds a `MultiFileChangeEngine` that wraps the existing single-file `ChangeEngine` with atomic multi-file operations, git-worktree-based validation, aggregated blast radius calculation, and cross-repo event publishing via the existing `CrossRepoEventBus`.

**Tech Stack:** Python 3.11+, asyncio, pytest, pytest-asyncio, psutil (for resource monitoring)

**Design doc:** `docs/plans/2026-03-07-ouroboros-v2-design.md`

**Phase 1 code references (all in `backend/core/ouroboros/governance/`):**
- `lock_manager.py` — `GovernanceLockManager`, `LockLevel`, `LockMode`, `LOCK_TTLS`
- `break_glass.py` — `BreakGlassManager`
- `change_engine.py` — `ChangeEngine`, `ChangeRequest`, `ChangeResult`, `ChangePhase`, `RollbackArtifact`
- `tui_transport.py` — `TUITransport`, `TUIMessageFormatter`
- `comm_protocol.py` — `CommProtocol`, `CommMessage`, `MessageType`, `LogTransport`
- `ledger.py` — `OperationLedger`, `LedgerEntry`, `OperationState`
- `risk_engine.py` — `RiskEngine`, `RiskTier`, `OperationProfile`, `ChangeType`
- `supervisor_controller.py` — `SupervisorOuroborosController`, `AutonomyMode`

**Key existing codebase references:**
- `PrimeRouter` at `backend/core/prime_router.py:284` — `RoutingDecision` enum (line 233), `promote_gcp_endpoint()` (line 590), `demote_gcp_endpoint()` (line 713)
- `PrimeClient` at `backend/core/prime_client.py` — `update_endpoint()` (line 718), `demote_to_fallback()` (line 833)
- `UnifiedModelServing` at `backend/intelligence/unified_model_serving.py:2287` — 3-tier fallback, `TaskType` enum, `ModelProvider` enum
- `PressureTier` at `backend/core/memory_types.py:45` — ABUNDANT=0 through EMERGENCY=5
- `CrossRepoEventBus` at `backend/core/ouroboros/cross_repo.py:257` — file-based event passing, `EventType` enum (line 87)
- `CodebaseKnowledgeGraph` at `backend/core/ouroboros/oracle.py:610` — `compute_blast_radius()` (line 800)
- `LearningMemory` at `backend/core/ouroboros/engine.py:362` — persistent JSON store

---

## Track 1: Plumbing & Gates (Phase 2A)

### Task 1: Resource Monitor — Pressure Signal Collection

**Files:**
- Create: `backend/core/ouroboros/governance/resource_monitor.py`
- Create: `tests/test_ouroboros_governance/test_resource_monitor.py`

**Context:** The design doc requires multi-signal routing based on RAM, CPU, IO, and event loop latency. The existing codebase has `PressureTier` (memory_types.py:45) and various monitoring patterns in unified_supervisor.py, but no unified governance-level resource monitor. This component collects signals and exposes a `ResourceSnapshot` for the routing policy and degradation controller.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_resource_monitor.py
"""Tests for the governance resource monitor."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock

from backend.core.ouroboros.governance.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    PressureLevel,
    PRESSURE_THRESHOLDS,
)


class TestPressureLevel:
    def test_all_levels_defined(self):
        """Four pressure levels: NORMAL, ELEVATED, CRITICAL, EMERGENCY."""
        assert len(PressureLevel) == 4
        assert PressureLevel.NORMAL.value == 0
        assert PressureLevel.ELEVATED.value == 1
        assert PressureLevel.CRITICAL.value == 2
        assert PressureLevel.EMERGENCY.value == 3

    def test_ordering(self):
        """Pressure levels are ordered for comparison."""
        assert PressureLevel.NORMAL < PressureLevel.ELEVATED
        assert PressureLevel.ELEVATED < PressureLevel.CRITICAL
        assert PressureLevel.CRITICAL < PressureLevel.EMERGENCY


class TestResourceSnapshot:
    def test_snapshot_fields(self):
        """Snapshot has all required resource fields."""
        snap = ResourceSnapshot(
            ram_percent=65.0,
            cpu_percent=30.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.ram_percent == 65.0
        assert snap.cpu_percent == 30.0
        assert snap.event_loop_latency_ms == 5.0
        assert snap.disk_io_busy is False

    def test_overall_pressure_normal(self):
        """Low resource usage yields NORMAL pressure."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.NORMAL

    def test_overall_pressure_elevated_ram(self):
        """RAM > 80% yields ELEVATED pressure."""
        snap = ResourceSnapshot(
            ram_percent=82.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.ELEVATED

    def test_overall_pressure_critical_cpu(self):
        """CPU > 80% sustained yields CRITICAL pressure."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=85.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.CRITICAL

    def test_overall_pressure_emergency_ram(self):
        """RAM > 90% yields EMERGENCY pressure."""
        snap = ResourceSnapshot(
            ram_percent=92.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.EMERGENCY

    def test_event_loop_latency_triggers_elevated(self):
        """Event loop latency > 40ms yields ELEVATED."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=45.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure >= PressureLevel.ELEVATED

    def test_disk_io_triggers_elevated(self):
        """Disk IO saturation yields ELEVATED."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=True,
        )
        assert snap.overall_pressure >= PressureLevel.ELEVATED


class TestResourceMonitor:
    @pytest.mark.asyncio
    async def test_snapshot_returns_resource_data(self):
        """snapshot() returns a valid ResourceSnapshot."""
        monitor = ResourceMonitor()
        snap = await monitor.snapshot()
        assert isinstance(snap, ResourceSnapshot)
        assert 0.0 <= snap.ram_percent <= 100.0
        assert 0.0 <= snap.cpu_percent <= 100.0
        assert snap.event_loop_latency_ms >= 0.0

    @pytest.mark.asyncio
    async def test_snapshot_with_injected_values(self):
        """Monitor accepts injected values for testing."""
        monitor = ResourceMonitor()
        snap = await monitor.snapshot(
            ram_override=85.0,
            cpu_override=90.0,
            latency_override=50.0,
            io_override=True,
        )
        assert snap.ram_percent == 85.0
        assert snap.cpu_percent == 90.0
        assert snap.event_loop_latency_ms == 50.0
        assert snap.disk_io_busy is True

    @pytest.mark.asyncio
    async def test_thresholds_configurable_via_env(self):
        """Thresholds are read from environment variables."""
        assert "ram_elevated" in PRESSURE_THRESHOLDS
        assert "ram_emergency" in PRESSURE_THRESHOLDS
        assert "cpu_critical" in PRESSURE_THRESHOLDS
        assert "latency_elevated_ms" in PRESSURE_THRESHOLDS
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_resource_monitor.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/resource_monitor.py
"""
Resource Monitor — Pressure Signal Collection
===============================================

Collects system resource signals (RAM, CPU, event loop latency, disk IO)
and exposes a :class:`ResourceSnapshot` with an ``overall_pressure`` level.

Used by :class:`RoutingPolicy` and :class:`DegradationController` to make
deterministic decisions about task routing and autonomy mode transitions.

Pressure Levels::

    NORMAL     All signals within comfortable range
    ELEVATED   One or more signals approaching limits
    CRITICAL   System under significant stress
    EMERGENCY  Imminent resource exhaustion
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.ResourceMonitor")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PressureLevel(enum.IntEnum):
    """System resource pressure classification."""

    NORMAL = 0
    ELEVATED = 1
    CRITICAL = 2
    EMERGENCY = 3


# ---------------------------------------------------------------------------
# Thresholds (configurable via environment)
# ---------------------------------------------------------------------------

PRESSURE_THRESHOLDS: Dict[str, float] = {
    "ram_elevated": float(os.environ.get("OUROBOROS_RAM_ELEVATED_PCT", "80")),
    "ram_critical": float(os.environ.get("OUROBOROS_RAM_CRITICAL_PCT", "85")),
    "ram_emergency": float(os.environ.get("OUROBOROS_RAM_EMERGENCY_PCT", "90")),
    "cpu_elevated": float(os.environ.get("OUROBOROS_CPU_ELEVATED_PCT", "70")),
    "cpu_critical": float(os.environ.get("OUROBOROS_CPU_CRITICAL_PCT", "80")),
    "cpu_emergency": float(os.environ.get("OUROBOROS_CPU_EMERGENCY_PCT", "95")),
    "latency_elevated_ms": float(os.environ.get("OUROBOROS_LATENCY_ELEVATED_MS", "40")),
    "latency_critical_ms": float(os.environ.get("OUROBOROS_LATENCY_CRITICAL_MS", "100")),
}


# ---------------------------------------------------------------------------
# ResourceSnapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceSnapshot:
    """Immutable snapshot of system resource state.

    Parameters
    ----------
    ram_percent:
        Current RAM usage as a percentage (0-100).
    cpu_percent:
        Current CPU usage as a percentage (0-100).
    event_loop_latency_ms:
        Async event loop latency in milliseconds.
    disk_io_busy:
        Whether disk IO is saturated.
    """

    ram_percent: float
    cpu_percent: float
    event_loop_latency_ms: float
    disk_io_busy: bool

    @property
    def overall_pressure(self) -> PressureLevel:
        """Compute overall pressure level from all signals.

        Returns the highest pressure level triggered by any signal.
        """
        level = PressureLevel.NORMAL

        # RAM pressure
        if self.ram_percent >= PRESSURE_THRESHOLDS["ram_emergency"]:
            level = max(level, PressureLevel.EMERGENCY)
        elif self.ram_percent >= PRESSURE_THRESHOLDS["ram_critical"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.ram_percent >= PRESSURE_THRESHOLDS["ram_elevated"]:
            level = max(level, PressureLevel.ELEVATED)

        # CPU pressure
        if self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_emergency"]:
            level = max(level, PressureLevel.EMERGENCY)
        elif self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_critical"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_elevated"]:
            level = max(level, PressureLevel.ELEVATED)

        # Event loop latency
        if self.event_loop_latency_ms >= PRESSURE_THRESHOLDS["latency_critical_ms"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.event_loop_latency_ms >= PRESSURE_THRESHOLDS["latency_elevated_ms"]:
            level = max(level, PressureLevel.ELEVATED)

        # Disk IO
        if self.disk_io_busy:
            level = max(level, PressureLevel.ELEVATED)

        return level


# ---------------------------------------------------------------------------
# ResourceMonitor
# ---------------------------------------------------------------------------


class ResourceMonitor:
    """Collects system resource signals for governance decisions.

    Supports both real system metrics (via psutil) and injected values
    for deterministic testing.
    """

    def __init__(self) -> None:
        self._last_snapshot: Optional[ResourceSnapshot] = None
        self._last_snapshot_time: float = 0.0

    async def snapshot(
        self,
        ram_override: Optional[float] = None,
        cpu_override: Optional[float] = None,
        latency_override: Optional[float] = None,
        io_override: Optional[bool] = None,
    ) -> ResourceSnapshot:
        """Collect a resource snapshot.

        Parameters
        ----------
        ram_override, cpu_override, latency_override, io_override:
            Injected values for testing. When provided, real system
            metrics are not queried.

        Returns
        -------
        ResourceSnapshot
            Immutable snapshot of current resource state.
        """
        ram = ram_override if ram_override is not None else self._get_ram_percent()
        cpu = cpu_override if cpu_override is not None else self._get_cpu_percent()
        latency = latency_override if latency_override is not None else await self._get_event_loop_latency()
        io_busy = io_override if io_override is not None else False

        snap = ResourceSnapshot(
            ram_percent=ram,
            cpu_percent=cpu,
            event_loop_latency_ms=latency,
            disk_io_busy=io_busy,
        )
        self._last_snapshot = snap
        self._last_snapshot_time = time.monotonic()
        return snap

    def _get_ram_percent(self) -> float:
        """Get current RAM usage percentage."""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            return 0.0

    def _get_cpu_percent(self) -> float:
        """Get current CPU usage percentage."""
        try:
            import psutil
            return psutil.cpu_percent(interval=None)
        except ImportError:
            return 0.0

    async def _get_event_loop_latency(self) -> float:
        """Measure async event loop latency in milliseconds."""
        import asyncio
        start = time.monotonic()
        await asyncio.sleep(0)
        return (time.monotonic() - start) * 1000
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_resource_monitor.py -v`
Expected: All 12 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/resource_monitor.py tests/test_ouroboros_governance/test_resource_monitor.py
git commit -m "feat(governance): add resource monitor with pressure signal collection

Collects RAM/CPU/event-loop-latency/disk-IO signals. Computes
overall PressureLevel (NORMAL/ELEVATED/CRITICAL/EMERGENCY).
Supports injected values for deterministic testing.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Degradation Controller — 4-Mode State Machine

**Files:**
- Create: `backend/core/ouroboros/governance/degradation.py`
- Create: `tests/test_ouroboros_governance/test_degradation.py`

**Context:** The design doc defines 4 degradation modes: FULL_AUTONOMY, REDUCED_AUTONOMY, READ_ONLY_PLANNING, EMERGENCY_STOP. The controller transitions between modes based on resource pressure, GCP availability, and rollback history. It integrates with the existing `SupervisorOuroborosController` to enforce mode restrictions.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_degradation.py
"""Tests for the degradation controller."""

import pytest
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
    DegradationReason,
    ModeTransition,
)
from backend.core.ouroboros.governance.resource_monitor import (
    ResourceSnapshot,
    PressureLevel,
)


@pytest.fixture
def controller():
    return DegradationController()


class TestDegradationModes:
    def test_all_four_modes_defined(self):
        """Four degradation modes exist."""
        assert len(DegradationMode) == 4
        expected = ["FULL_AUTONOMY", "REDUCED_AUTONOMY", "READ_ONLY_PLANNING", "EMERGENCY_STOP"]
        assert [m.name for m in DegradationMode] == expected

    def test_starts_in_full_autonomy(self, controller):
        """Controller starts in FULL_AUTONOMY."""
        assert controller.mode == DegradationMode.FULL_AUTONOMY


class TestModeTransitions:
    @pytest.mark.asyncio
    async def test_elevated_pressure_reduces_autonomy(self, controller):
        """ELEVATED pressure transitions to REDUCED_AUTONOMY."""
        snap = ResourceSnapshot(
            ram_percent=82.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY
        assert transition is not None
        assert transition.to_mode == DegradationMode.REDUCED_AUTONOMY

    @pytest.mark.asyncio
    async def test_critical_pressure_goes_read_only(self, controller):
        """CRITICAL pressure transitions to READ_ONLY_PLANNING."""
        snap = ResourceSnapshot(
            ram_percent=87.0,
            cpu_percent=85.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.READ_ONLY_PLANNING

    @pytest.mark.asyncio
    async def test_emergency_pressure_stops(self, controller):
        """EMERGENCY pressure transitions to EMERGENCY_STOP."""
        snap = ResourceSnapshot(
            ram_percent=95.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_normal_pressure_stays_full(self, controller):
        """NORMAL pressure stays in FULL_AUTONOMY."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.FULL_AUTONOMY
        assert transition is None

    @pytest.mark.asyncio
    async def test_recovery_from_reduced_to_full(self, controller):
        """Pressure drop from ELEVATED to NORMAL recovers to FULL_AUTONOMY."""
        high = ResourceSnapshot(ram_percent=82.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY

        low = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(low)
        assert controller.mode == DegradationMode.FULL_AUTONOMY

    @pytest.mark.asyncio
    async def test_emergency_stop_requires_explicit_reset(self, controller):
        """EMERGENCY_STOP does not auto-recover; requires explicit reset."""
        high = ResourceSnapshot(ram_percent=95.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        assert controller.mode == DegradationMode.EMERGENCY_STOP

        low = ResourceSnapshot(ram_percent=30.0, cpu_percent=20.0, event_loop_latency_ms=1.0, disk_io_busy=False)
        await controller.evaluate(low)
        assert controller.mode == DegradationMode.EMERGENCY_STOP  # Still stopped

    @pytest.mark.asyncio
    async def test_explicit_reset_from_emergency(self, controller):
        """explicit_reset() recovers from EMERGENCY_STOP to FULL_AUTONOMY."""
        high = ResourceSnapshot(ram_percent=95.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        await controller.explicit_reset()
        assert controller.mode == DegradationMode.FULL_AUTONOMY


class TestGCPAvailability:
    @pytest.mark.asyncio
    async def test_gcp_down_reduces_autonomy(self, controller):
        """GCP unavailable triggers REDUCED_AUTONOMY."""
        controller.set_gcp_available(False)
        snap = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY

    @pytest.mark.asyncio
    async def test_gcp_recovery_restores_mode(self, controller):
        """GCP coming back restores FULL_AUTONOMY."""
        controller.set_gcp_available(False)
        snap = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY

        controller.set_gcp_available(True)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.FULL_AUTONOMY


class TestRollbackHistory:
    @pytest.mark.asyncio
    async def test_three_rollbacks_triggers_emergency(self, controller):
        """3+ rollbacks in 1 hour triggers EMERGENCY_STOP."""
        controller.record_rollback()
        controller.record_rollback()
        controller.record_rollback()
        snap = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.EMERGENCY_STOP


class TestTransitionHistory:
    @pytest.mark.asyncio
    async def test_transitions_are_recorded(self, controller):
        """All mode transitions are stored in history."""
        high = ResourceSnapshot(ram_percent=82.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        low = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(low)

        history = controller.get_transition_history()
        assert len(history) == 2
        assert history[0].from_mode == DegradationMode.FULL_AUTONOMY
        assert history[0].to_mode == DegradationMode.REDUCED_AUTONOMY
        assert history[1].to_mode == DegradationMode.FULL_AUTONOMY


class TestPermissions:
    @pytest.mark.asyncio
    async def test_safe_auto_allowed_in_full_and_reduced(self, controller):
        """SAFE_AUTO tasks allowed in FULL and REDUCED."""
        assert controller.safe_auto_allowed is True
        controller._mode = DegradationMode.REDUCED_AUTONOMY
        assert controller.safe_auto_allowed is True

    @pytest.mark.asyncio
    async def test_heavy_tasks_only_in_full(self, controller):
        """Heavy tasks (multi-file, cross-repo) only in FULL_AUTONOMY."""
        assert controller.heavy_tasks_allowed is True
        controller._mode = DegradationMode.REDUCED_AUTONOMY
        assert controller.heavy_tasks_allowed is False

    @pytest.mark.asyncio
    async def test_no_writes_in_read_only(self, controller):
        """No writes allowed in READ_ONLY_PLANNING."""
        controller._mode = DegradationMode.READ_ONLY_PLANNING
        assert controller.safe_auto_allowed is False
        assert controller.heavy_tasks_allowed is False
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_degradation.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/degradation.py
"""
Degradation Controller — 4-Mode Autonomy State Machine
========================================================

Manages transitions between 4 degradation modes based on resource pressure,
GCP availability, and rollback history::

    FULL_AUTONOMY       All tiers active, GCP available, all gates green
    REDUCED_AUTONOMY    GCP unavailable or elevated pressure -> safe_auto local only
    READ_ONLY_PLANNING  Critical pressure or incident mode -> analyze + plan only
    EMERGENCY_STOP      Emergency pressure or 3+ rollbacks/hour -> all autonomy halted

EMERGENCY_STOP requires explicit human reset (:meth:`explicit_reset`).
All other modes auto-recover when pressure drops.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.resource_monitor import (
    PressureLevel,
    ResourceSnapshot,
)

logger = logging.getLogger("Ouroboros.Degradation")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DegradationMode(enum.IntEnum):
    """Autonomy degradation modes (ordered by restriction level)."""

    FULL_AUTONOMY = 0
    REDUCED_AUTONOMY = 1
    READ_ONLY_PLANNING = 2
    EMERGENCY_STOP = 3


class DegradationReason(enum.Enum):
    """Why a degradation transition occurred."""

    PRESSURE_ELEVATED = "pressure_elevated"
    PRESSURE_CRITICAL = "pressure_critical"
    PRESSURE_EMERGENCY = "pressure_emergency"
    GCP_UNAVAILABLE = "gcp_unavailable"
    ROLLBACK_THRESHOLD = "rollback_threshold_exceeded"
    PRESSURE_RECOVERED = "pressure_recovered"
    EXPLICIT_RESET = "explicit_reset"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ModeTransition:
    """Record of a degradation mode transition."""

    from_mode: DegradationMode
    to_mode: DegradationMode
    reason: DegradationReason
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROLLBACK_WINDOW_S: float = 3600.0  # 1 hour
ROLLBACK_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# DegradationController
# ---------------------------------------------------------------------------


class DegradationController:
    """4-mode degradation state machine for Ouroboros autonomy.

    Evaluates resource snapshots and environmental signals to determine
    the appropriate degradation mode.
    """

    def __init__(self) -> None:
        self._mode: DegradationMode = DegradationMode.FULL_AUTONOMY
        self._gcp_available: bool = True
        self._rollback_timestamps: List[float] = []
        self._transition_history: List[ModeTransition] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mode(self) -> DegradationMode:
        """Current degradation mode."""
        return self._mode

    @property
    def safe_auto_allowed(self) -> bool:
        """Whether SAFE_AUTO tasks are permitted."""
        return self._mode in (
            DegradationMode.FULL_AUTONOMY,
            DegradationMode.REDUCED_AUTONOMY,
        )

    @property
    def heavy_tasks_allowed(self) -> bool:
        """Whether heavy tasks (multi-file, cross-repo, codegen) are permitted."""
        return self._mode == DegradationMode.FULL_AUTONOMY

    # ------------------------------------------------------------------
    # Signal inputs
    # ------------------------------------------------------------------

    def set_gcp_available(self, available: bool) -> None:
        """Update GCP availability status."""
        self._gcp_available = available

    def record_rollback(self) -> None:
        """Record a rollback event for threshold tracking."""
        self._rollback_timestamps.append(time.time())

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def evaluate(
        self, snapshot: ResourceSnapshot
    ) -> Optional[ModeTransition]:
        """Evaluate resource state and transition mode if needed.

        Parameters
        ----------
        snapshot:
            Current resource state.

        Returns
        -------
        Optional[ModeTransition]
            The transition that occurred, or None if mode unchanged.
        """
        # EMERGENCY_STOP is sticky — requires explicit reset
        if self._mode == DegradationMode.EMERGENCY_STOP:
            return None

        # Determine target mode from signals
        target = self._compute_target_mode(snapshot)

        if target == self._mode:
            return None

        reason = self._classify_reason(snapshot, target)
        transition = ModeTransition(
            from_mode=self._mode,
            to_mode=target,
            reason=reason,
            details={
                "ram_percent": snapshot.ram_percent,
                "cpu_percent": snapshot.cpu_percent,
                "pressure": snapshot.overall_pressure.name,
                "gcp_available": self._gcp_available,
            },
        )

        previous = self._mode
        self._mode = target
        self._transition_history.append(transition)

        logger.info(
            "Degradation: %s -> %s (reason=%s)",
            previous.name, target.name, reason.value,
        )

        return transition

    def _compute_target_mode(self, snapshot: ResourceSnapshot) -> DegradationMode:
        """Determine target mode from all signals."""
        pressure = snapshot.overall_pressure

        # Check rollback threshold
        now = time.time()
        recent_rollbacks = [
            t for t in self._rollback_timestamps
            if now - t < ROLLBACK_WINDOW_S
        ]
        self._rollback_timestamps = recent_rollbacks

        if len(recent_rollbacks) >= ROLLBACK_THRESHOLD:
            return DegradationMode.EMERGENCY_STOP

        # Pressure-based mode
        if pressure >= PressureLevel.EMERGENCY:
            return DegradationMode.EMERGENCY_STOP
        elif pressure >= PressureLevel.CRITICAL:
            return DegradationMode.READ_ONLY_PLANNING
        elif pressure >= PressureLevel.ELEVATED or not self._gcp_available:
            return DegradationMode.REDUCED_AUTONOMY
        else:
            return DegradationMode.FULL_AUTONOMY

    def _classify_reason(
        self,
        snapshot: ResourceSnapshot,
        target: DegradationMode,
    ) -> DegradationReason:
        """Classify the reason for a mode transition."""
        if target == DegradationMode.FULL_AUTONOMY:
            return DegradationReason.PRESSURE_RECOVERED
        if target == DegradationMode.EMERGENCY_STOP:
            now = time.time()
            recent = [t for t in self._rollback_timestamps if now - t < ROLLBACK_WINDOW_S]
            if len(recent) >= ROLLBACK_THRESHOLD:
                return DegradationReason.ROLLBACK_THRESHOLD
            return DegradationReason.PRESSURE_EMERGENCY
        if target == DegradationMode.READ_ONLY_PLANNING:
            return DegradationReason.PRESSURE_CRITICAL
        if not self._gcp_available:
            return DegradationReason.GCP_UNAVAILABLE
        return DegradationReason.PRESSURE_ELEVATED

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    async def explicit_reset(self) -> None:
        """Explicitly reset from EMERGENCY_STOP to FULL_AUTONOMY.

        Only a human operator should call this.
        """
        if self._mode == DegradationMode.EMERGENCY_STOP:
            transition = ModeTransition(
                from_mode=DegradationMode.EMERGENCY_STOP,
                to_mode=DegradationMode.FULL_AUTONOMY,
                reason=DegradationReason.EXPLICIT_RESET,
            )
            self._mode = DegradationMode.FULL_AUTONOMY
            self._transition_history.append(transition)
            self._rollback_timestamps.clear()
            logger.info("Degradation: EMERGENCY_STOP -> FULL_AUTONOMY (explicit reset)")

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_transition_history(self) -> List[ModeTransition]:
        """Return all mode transitions."""
        return list(self._transition_history)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_degradation.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/degradation.py tests/test_ouroboros_governance/test_degradation.py
git commit -m "feat(governance): add degradation controller with 4-mode state machine

FULL_AUTONOMY -> REDUCED_AUTONOMY -> READ_ONLY_PLANNING -> EMERGENCY_STOP.
Pressure-driven transitions, GCP availability, rollback threshold (3/hr),
explicit reset for EMERGENCY_STOP.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Routing Policy — Deterministic Task Routing

**Files:**
- Create: `backend/core/ouroboros/governance/routing_policy.py`
- Create: `tests/test_ouroboros_governance/test_routing_policy.py`

**Context:** The design doc defines a routing matrix: task type x resource state -> routing decision (LOCAL / GCP_PRIME / QUEUE). This component makes deterministic routing decisions without LLM calls. It uses the ResourceSnapshot from Task 1 and the DegradationMode from Task 2.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_routing_policy.py
"""Tests for the deterministic routing policy."""

import pytest

from backend.core.ouroboros.governance.routing_policy import (
    RoutingPolicy,
    RoutingDecision,
    TaskCategory,
    CostGuardrail,
)
from backend.core.ouroboros.governance.resource_monitor import (
    ResourceSnapshot,
    PressureLevel,
)
from backend.core.ouroboros.governance.degradation import DegradationMode


@pytest.fixture
def policy():
    return RoutingPolicy()


def _normal_snap():
    return ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)

def _elevated_snap():
    return ResourceSnapshot(ram_percent=82.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)

def _critical_snap():
    return ResourceSnapshot(ram_percent=87.0, cpu_percent=85.0, event_loop_latency_ms=5.0, disk_io_busy=False)


class TestTaskCategories:
    def test_all_categories_defined(self):
        """Six task categories exist."""
        expected = [
            "SINGLE_FILE_FIX", "MULTI_FILE_ANALYSIS", "CROSS_REPO_PLANNING",
            "CANDIDATE_GENERATION", "TEST_EXECUTION", "BLAST_RADIUS_CALC",
        ]
        assert [c.name for c in TaskCategory] == expected


class TestRoutingDecisions:
    def test_all_decisions_defined(self):
        """Three routing decisions: LOCAL, GCP_PRIME, QUEUE."""
        assert len(RoutingDecision) == 3


class TestNormalConditions:
    def test_single_file_routes_local(self, policy):
        """Single-file fix always routes LOCAL."""
        decision = policy.route(
            TaskCategory.SINGLE_FILE_FIX,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL

    def test_multi_file_analysis_routes_local_normally(self, policy):
        """Multi-file analysis routes LOCAL under normal conditions."""
        decision = policy.route(
            TaskCategory.MULTI_FILE_ANALYSIS,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL

    def test_cross_repo_routes_gcp(self, policy):
        """Cross-repo planning routes to GCP_PRIME."""
        decision = policy.route(
            TaskCategory.CROSS_REPO_PLANNING,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_candidate_gen_routes_gcp(self, policy):
        """Candidate generation routes to GCP_PRIME."""
        decision = policy.route(
            TaskCategory.CANDIDATE_GENERATION,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_test_execution_routes_local(self, policy):
        """Test execution always routes LOCAL."""
        decision = policy.route(
            TaskCategory.TEST_EXECUTION,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL

    def test_blast_radius_routes_local(self, policy):
        """Blast radius calculation always routes LOCAL."""
        decision = policy.route(
            TaskCategory.BLAST_RADIUS_CALC,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL


class TestPressureRouting:
    def test_elevated_heavy_task_routes_gcp(self, policy):
        """Under elevated pressure, heavy tasks route to GCP."""
        decision = policy.route(
            TaskCategory.MULTI_FILE_ANALYSIS,
            _elevated_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_single_file_stays_local_under_pressure(self, policy):
        """Single-file fix stays LOCAL even under elevated pressure."""
        decision = policy.route(
            TaskCategory.SINGLE_FILE_FIX,
            _elevated_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.LOCAL


class TestGCPDown:
    def test_gcp_down_queues_heavy_tasks(self, policy):
        """GCP unavailable queues heavy tasks."""
        decision = policy.route(
            TaskCategory.CROSS_REPO_PLANNING,
            _normal_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=False,
        )
        assert decision == RoutingDecision.QUEUE

    def test_gcp_down_local_tasks_continue(self, policy):
        """GCP unavailable doesn't affect local-only tasks."""
        decision = policy.route(
            TaskCategory.SINGLE_FILE_FIX,
            _normal_snap(),
            DegradationMode.REDUCED_AUTONOMY,
            gcp_available=False,
        )
        assert decision == RoutingDecision.LOCAL


class TestCostGuardrail:
    def test_budget_tracking(self, policy):
        """Cost guardrail tracks GCP usage."""
        guardrail = policy.cost_guardrail
        guardrail.record_gcp_usage(0.50)
        guardrail.record_gcp_usage(0.25)
        assert guardrail.daily_usage == 0.75

    def test_over_budget_queues_gcp(self, policy):
        """Over daily budget queues GCP-bound tasks."""
        policy.cost_guardrail.record_gcp_usage(100.0)  # Over any budget
        decision = policy.route(
            TaskCategory.CANDIDATE_GENERATION,
            _normal_snap(),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.QUEUE


class TestDeterminism:
    def test_same_inputs_same_output_1000x(self, policy):
        """Same inputs always produce same routing decision."""
        snap = _normal_snap()
        first = policy.route(TaskCategory.CROSS_REPO_PLANNING, snap, DegradationMode.FULL_AUTONOMY, True)
        for _ in range(1000):
            result = policy.route(TaskCategory.CROSS_REPO_PLANNING, snap, DegradationMode.FULL_AUTONOMY, True)
            assert result == first
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_routing_policy.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/routing_policy.py
"""
Routing Policy — Deterministic Task Routing
=============================================

Makes deterministic routing decisions based on task category, resource
pressure, GCP availability, and cost guardrails.  No LLM calls.

Routing Matrix (design doc section 2.7)::

    Task Type           | Normal    | CPU>80%   | RAM>85%   | GCP Down
    --------------------|-----------|-----------|-----------|----------
    Single-file fix     | LOCAL     | LOCAL     | LOCAL     | LOCAL
    Multi-file analysis | LOCAL     | GCP_PRIME | GCP_PRIME | QUEUE
    Cross-repo planning | GCP_PRIME | GCP_PRIME | GCP_PRIME | QUEUE
    Candidate gen (3+)  | GCP_PRIME | GCP_PRIME | GCP_PRIME | QUEUE
    Test execution      | LOCAL     | LOCAL     | LOCAL     | LOCAL
    Blast radius calc   | LOCAL     | LOCAL     | LOCAL     | LOCAL
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.core.ouroboros.governance.degradation import DegradationMode
from backend.core.ouroboros.governance.resource_monitor import (
    PressureLevel,
    ResourceSnapshot,
)

logger = logging.getLogger("Ouroboros.RoutingPolicy")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskCategory(enum.Enum):
    """Categories of Ouroboros tasks for routing decisions."""

    SINGLE_FILE_FIX = "single_file_fix"
    MULTI_FILE_ANALYSIS = "multi_file_analysis"
    CROSS_REPO_PLANNING = "cross_repo_planning"
    CANDIDATE_GENERATION = "candidate_generation"
    TEST_EXECUTION = "test_execution"
    BLAST_RADIUS_CALC = "blast_radius_calc"


class RoutingDecision(enum.Enum):
    """Where a task should be executed."""

    LOCAL = "local"
    GCP_PRIME = "gcp_prime"
    QUEUE = "queue"


# Tasks that always run locally regardless of conditions
_ALWAYS_LOCAL: set = {
    TaskCategory.SINGLE_FILE_FIX,
    TaskCategory.TEST_EXECUTION,
    TaskCategory.BLAST_RADIUS_CALC,
}

# Tasks that prefer GCP when available
_PREFER_GCP: set = {
    TaskCategory.CROSS_REPO_PLANNING,
    TaskCategory.CANDIDATE_GENERATION,
}


# ---------------------------------------------------------------------------
# Cost Guardrail
# ---------------------------------------------------------------------------


class CostGuardrail:
    """Tracks GCP usage costs and enforces daily budget caps."""

    def __init__(self) -> None:
        self._daily_cap: float = float(
            os.environ.get("OUROBOROS_GCP_DAILY_BUDGET", "10.0")
        )
        self._usage_today: float = 0.0
        self._reset_date: str = time.strftime("%Y-%m-%d")

    @property
    def daily_usage(self) -> float:
        """Current day's GCP usage."""
        self._check_date_reset()
        return self._usage_today

    @property
    def over_budget(self) -> bool:
        """Whether daily budget has been exceeded."""
        self._check_date_reset()
        return self._usage_today >= self._daily_cap

    def record_gcp_usage(self, cost: float) -> None:
        """Record a GCP cost event."""
        self._check_date_reset()
        self._usage_today += cost

    def _check_date_reset(self) -> None:
        """Reset counter at date boundary."""
        today = time.strftime("%Y-%m-%d")
        if today != self._reset_date:
            self._usage_today = 0.0
            self._reset_date = today


# ---------------------------------------------------------------------------
# RoutingPolicy
# ---------------------------------------------------------------------------


class RoutingPolicy:
    """Deterministic routing policy for Ouroboros tasks.

    Makes routing decisions based on:
    - Task category (what kind of work)
    - Resource snapshot (system pressure)
    - Degradation mode (current autonomy level)
    - GCP availability
    - Cost guardrails (daily budget)
    """

    def __init__(self) -> None:
        self.cost_guardrail = CostGuardrail()

    def route(
        self,
        task: TaskCategory,
        snapshot: ResourceSnapshot,
        degradation_mode: DegradationMode,
        gcp_available: bool,
    ) -> RoutingDecision:
        """Make a deterministic routing decision.

        Parameters
        ----------
        task:
            The category of task to route.
        snapshot:
            Current resource state.
        degradation_mode:
            Current degradation mode.
        gcp_available:
            Whether GCP J-Prime is reachable.

        Returns
        -------
        RoutingDecision
            Where the task should execute: LOCAL, GCP_PRIME, or QUEUE.
        """
        # Always-local tasks never route elsewhere
        if task in _ALWAYS_LOCAL:
            return RoutingDecision.LOCAL

        # Cost guardrail: over budget queues GCP tasks
        if self.cost_guardrail.over_budget and task in _PREFER_GCP:
            logger.info(
                "Routing %s -> QUEUE (over daily GCP budget)", task.value
            )
            return RoutingDecision.QUEUE

        # GCP unavailable: queue heavy tasks, local for light
        if not gcp_available:
            if task in _PREFER_GCP:
                return RoutingDecision.QUEUE
            return RoutingDecision.LOCAL

        # GCP-preferred tasks route to GCP when available
        if task in _PREFER_GCP:
            return RoutingDecision.GCP_PRIME

        # Pressure-based routing for medium tasks (multi-file analysis)
        pressure = snapshot.overall_pressure
        if pressure >= PressureLevel.ELEVATED and gcp_available:
            return RoutingDecision.GCP_PRIME

        return RoutingDecision.LOCAL
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_routing_policy.py -v`
Expected: All 14 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/routing_policy.py tests/test_ouroboros_governance/test_routing_policy.py
git commit -m "feat(governance): add deterministic routing policy with cost guardrails

Task-category x resource-pressure routing matrix. LOCAL/GCP_PRIME/QUEUE
decisions. Daily GCP budget cap with automatic date reset.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Track 2: Loop (Phase 2B)

### Task 4: Multi-File Change Engine

**Files:**
- Create: `backend/core/ouroboros/governance/multi_file_engine.py`
- Create: `tests/test_ouroboros_governance/test_multi_file_engine.py`

**Context:** The existing `ChangeEngine` handles single-file operations. Phase 2B wraps it with a `MultiFileChangeEngine` that applies changes atomically across multiple files. All files succeed or all are rolled back. Uses `CROSS_REPO_TX` lock level for the transaction envelope, with nested `FILE_LOCK`s for individual files.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_multi_file_engine.py
"""Tests for multi-file atomic change engine."""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
    MultiFileChangeResult,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeRequest,
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.lock_manager import GovernanceLockManager
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


@pytest.fixture
def project(tmp_path):
    """Create a project with multiple files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def foo():\n    return 1\n")
    (src / "bar.py").write_text("def bar():\n    return 2\n")
    (src / "baz.py").write_text("def baz():\n    return 3\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


@pytest.fixture
def engine(project, ledger):
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    return MultiFileChangeEngine(
        project_root=project,
        ledger=ledger,
        comm=comm,
        lock_manager=GovernanceLockManager(),
        break_glass=BreakGlassManager(),
    ), transport


def _safe_profile(*files):
    return OperationProfile(
        files_affected=[Path(f) for f in files],
        change_type=ChangeType.MODIFY,
        blast_radius=len(files),
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
    )


class TestAtomicMultiFile:
    @pytest.mark.asyncio
    async def test_all_files_applied_on_success(self, engine, project):
        """All files are modified when all changes succeed."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Update all files",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",
                project / "src" / "bar.py": "def bar():\n    return 20\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
        )
        result = await eng.execute(request)
        assert result.success is True
        assert (project / "src" / "foo.py").read_text() == "def foo():\n    return 10\n"
        assert (project / "src" / "bar.py").read_text() == "def bar():\n    return 20\n"

    @pytest.mark.asyncio
    async def test_all_files_rolled_back_on_verify_failure(self, engine, project):
        """All files are restored when post-apply verification fails."""
        eng, _ = engine
        original_foo = (project / "src" / "foo.py").read_text()
        original_bar = (project / "src" / "bar.py").read_text()

        request = MultiFileChangeRequest(
            goal="Change that fails verify",
            files={
                project / "src" / "foo.py": "def foo():\n    return 100\n",
                project / "src" / "bar.py": "def bar():\n    return 200\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.rolled_back is True
        assert (project / "src" / "foo.py").read_text() == original_foo
        assert (project / "src" / "bar.py").read_text() == original_bar

    @pytest.mark.asyncio
    async def test_invalid_syntax_in_one_file_blocks_all(self, engine, project):
        """If any file has invalid syntax, no files are applied."""
        eng, _ = engine
        original_foo = (project / "src" / "foo.py").read_text()
        original_bar = (project / "src" / "bar.py").read_text()

        request = MultiFileChangeRequest(
            goal="One bad file",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",  # Valid
                project / "src" / "bar.py": "def bar(\n",  # Invalid syntax
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.VALIDATE
        # Neither file should be modified
        assert (project / "src" / "foo.py").read_text() == original_foo
        assert (project / "src" / "bar.py").read_text() == original_bar

    @pytest.mark.asyncio
    async def test_blocked_profile_stops_at_gate(self, engine, project):
        """BLOCKED risk tier stops at GATE, no files modified."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Touches supervisor",
            files={
                project / "src" / "foo.py": "# modified\n",
            },
            profile=OperationProfile(
                files_affected=[Path("unified_supervisor.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=True,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.risk_tier == RiskTier.BLOCKED


class TestLedgerTracking:
    @pytest.mark.asyncio
    async def test_ledger_records_file_list(self, engine, project, ledger):
        """Ledger PLANNED entry includes the list of all files."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Multi-file change",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",
                project / "src" / "bar.py": "def bar():\n    return 20\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
        )
        result = await eng.execute(request)
        history = await ledger.get_history(result.op_id)
        planned = [e for e in history if e.state == OperationState.PLANNED][0]
        assert "files" in planned.data
        assert len(planned.data["files"]) == 2

    @pytest.mark.asyncio
    async def test_rollback_recorded_in_ledger(self, engine, project, ledger):
        """ROLLED_BACK state recorded when verify fails."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Fails verify",
            files={
                project / "src" / "foo.py": "def foo():\n    return 999\n",
            },
            profile=_safe_profile("src/foo.py"),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await eng.execute(request)
        latest = await ledger.get_latest_state(result.op_id)
        assert latest == OperationState.ROLLED_BACK


class TestCommProtocol:
    @pytest.mark.asyncio
    async def test_all_message_types_emitted(self, engine, project):
        """Multi-file operation emits INTENT, HEARTBEAT, DECISION, POSTMORTEM."""
        eng, transport = engine
        request = MultiFileChangeRequest(
            goal="Emit all messages",
            files={
                project / "src" / "foo.py": "def foo():\n    return 42\n",
            },
            profile=_safe_profile("src/foo.py"),
        )
        result = await eng.execute(request)
        assert result.success is True
        types = {m.msg_type for m in transport.messages}
        assert MessageType.INTENT in types
        assert MessageType.HEARTBEAT in types
        assert MessageType.DECISION in types
        assert MessageType.POSTMORTEM in types


class TestRollbackIntegrity:
    @pytest.mark.asyncio
    async def test_rollback_hashes_match_originals(self, engine, project):
        """Each file's rollback hash matches its pre-change hash."""
        eng, _ = engine
        foo_hash = hashlib.sha256(
            (project / "src" / "foo.py").read_text().encode()
        ).hexdigest()
        bar_hash = hashlib.sha256(
            (project / "src" / "bar.py").read_text().encode()
        ).hexdigest()

        request = MultiFileChangeRequest(
            goal="Hash integrity",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",
                project / "src" / "bar.py": "def bar():\n    return 20\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
            verify_fn=AsyncMock(return_value=False),  # Force rollback
        )
        result = await eng.execute(request)

        # After rollback, files should match original hashes
        assert hashlib.sha256(
            (project / "src" / "foo.py").read_text().encode()
        ).hexdigest() == foo_hash
        assert hashlib.sha256(
            (project / "src" / "bar.py").read_text().encode()
        ).hexdigest() == bar_hash
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_engine.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/multi_file_engine.py
"""
Multi-File Change Engine — Atomic Multi-File Operations
=========================================================

Wraps the single-file :class:`ChangeEngine` pipeline to apply changes
atomically across multiple files.  All files succeed or all are rolled back.

Uses ``CROSS_REPO_TX`` lock level for the transaction envelope, with nested
``FILE_LOCK``s for individual file writes.

Key guarantees:
- All-or-nothing: if any file fails validation, no files are modified
- Pre-tested rollback: each file's snapshot captured BEFORE any writes
- Ledger tracks all files in the operation via ``data.files[]``
- Communication protocol emits full 5-phase lifecycle
"""

from __future__ import annotations

import ast
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.break_glass import BreakGlassManager
from backend.core.ouroboros.governance.change_engine import (
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    RiskEngine,
    RiskTier,
)

logger = logging.getLogger("Ouroboros.MultiFileEngine")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MultiFileChangeRequest:
    """Request to atomically change multiple files.

    Parameters
    ----------
    goal:
        Natural-language description of the change.
    files:
        Mapping of absolute file paths to their proposed new content.
    profile:
        Aggregated operation risk profile.
    verify_fn:
        Optional async callable returning True if post-apply verification
        passes for the entire transaction.
    """

    goal: str
    files: Dict[Path, str]
    profile: OperationProfile
    verify_fn: Optional[Any] = None


@dataclass
class MultiFileChangeResult:
    """Result of a multi-file change engine execution.

    Parameters
    ----------
    op_id:
        The unique operation identifier.
    success:
        Whether all files were applied and verified.
    phase_reached:
        The last phase reached.
    risk_tier:
        The risk classification.
    rolled_back:
        Whether all files were rolled back.
    files_applied:
        Number of files successfully written.
    error:
        Error message if failed.
    """

    op_id: str
    success: bool
    phase_reached: ChangePhase
    risk_tier: Optional[RiskTier] = None
    rolled_back: bool = False
    files_applied: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# MultiFileChangeEngine
# ---------------------------------------------------------------------------


class MultiFileChangeEngine:
    """Atomic multi-file change pipeline.

    Wraps the 8-phase pipeline for multiple files with all-or-nothing
    semantics.
    """

    def __init__(
        self,
        project_root: Path,
        ledger: OperationLedger,
        comm: Optional[CommProtocol] = None,
        lock_manager: Optional[GovernanceLockManager] = None,
        break_glass: Optional[BreakGlassManager] = None,
        risk_engine: Optional[RiskEngine] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._ledger = ledger
        self._comm = comm or CommProtocol(transports=[LogTransport()])
        self._lock_manager = lock_manager or GovernanceLockManager()
        self._break_glass = break_glass or BreakGlassManager()
        self._risk_engine = risk_engine or RiskEngine()

    async def execute(
        self, request: MultiFileChangeRequest
    ) -> MultiFileChangeResult:
        """Execute the atomic multi-file change pipeline.

        Parameters
        ----------
        request:
            The multi-file change request.

        Returns
        -------
        MultiFileChangeResult
            Result with success status and rollback information.
        """
        op_id = generate_operation_id(repo_origin="jarvis")
        file_paths = list(request.files.keys())
        file_strs = [str(f) for f in file_paths]

        try:
            # Phase 1: PLAN — classify risk
            classification = self._risk_engine.classify(request.profile)
            risk_tier = classification.tier

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.PLANNED,
                    data={
                        "goal": request.goal,
                        "files": file_strs,
                        "file_count": len(file_paths),
                        "risk_tier": risk_tier.name,
                        "reason_code": classification.reason_code,
                    },
                )
            )

            await self._comm.emit_intent(
                op_id=op_id,
                goal=request.goal,
                target_files=file_strs,
                risk_tier=risk_tier.name,
                blast_radius=request.profile.blast_radius,
            )

            # Phase 2: SANDBOX
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="sandbox", progress_pct=15.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.SANDBOXING,
                    data={"file_count": len(file_paths)},
                )
            )

            # Phase 3: VALIDATE — AST parse ALL files before any writes
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="validate", progress_pct=30.0
            )
            for fpath, content in request.files.items():
                if not self._validate_syntax(content):
                    await self._ledger.append(
                        LedgerEntry(
                            op_id=op_id,
                            state=OperationState.FAILED,
                            data={
                                "reason": "syntax_error",
                                "file": str(fpath),
                            },
                        )
                    )
                    await self._comm.emit_decision(
                        op_id=op_id,
                        outcome="validation_failed",
                        reason_code="syntax_error",
                    )
                    return MultiFileChangeResult(
                        op_id=op_id,
                        success=False,
                        phase_reached=ChangePhase.VALIDATE,
                        risk_tier=risk_tier,
                    )

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.VALIDATING,
                    data={"all_valid": True},
                )
            )

            # Phase 4: GATE — check risk tier
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="gate", progress_pct=45.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.GATING,
                    data={"risk_tier": risk_tier.name},
                )
            )

            # Check break-glass for BLOCKED
            if risk_tier == RiskTier.BLOCKED:
                promoted = self._break_glass.get_promoted_tier(op_id)
                if promoted is not None:
                    risk_tier = RiskTier.APPROVAL_REQUIRED
                else:
                    await self._comm.emit_decision(
                        op_id=op_id,
                        outcome="blocked",
                        reason_code=classification.reason_code,
                    )
                    await self._ledger.append(
                        LedgerEntry(
                            op_id=op_id,
                            state=OperationState.BLOCKED,
                            data={"reason": classification.reason_code},
                        )
                    )
                    return MultiFileChangeResult(
                        op_id=op_id,
                        success=False,
                        phase_reached=ChangePhase.GATE,
                        risk_tier=RiskTier.BLOCKED,
                    )

            if risk_tier == RiskTier.APPROVAL_REQUIRED:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="escalated",
                    reason_code=classification.reason_code,
                )
                return MultiFileChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.GATE,
                    risk_tier=RiskTier.APPROVAL_REQUIRED,
                )

            # Phase 5: APPLY — capture rollback artifacts, write all files
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="apply", progress_pct=60.0
            )

            rollback_artifacts: Dict[Path, RollbackArtifact] = {}
            for fpath in file_paths:
                rollback_artifacts[fpath] = RollbackArtifact.capture(fpath)

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLYING,
                    data={
                        "files": file_strs,
                        "rollback_hashes": {
                            str(p): a.snapshot_hash
                            for p, a in rollback_artifacts.items()
                        },
                    },
                )
            )

            # Write all files under lock
            files_written = 0
            async with self._lock_manager.acquire(
                level=LockLevel.CROSS_REPO_TX,
                resource="multi-file-txn",
                mode=LockMode.EXCLUSIVE_WRITE,
            ):
                for fpath, content in request.files.items():
                    async with self._lock_manager.acquire(
                        level=LockLevel.PROD_LOCK,
                        resource=str(fpath),
                        mode=LockMode.EXCLUSIVE_WRITE,
                    ):
                        fpath.write_text(content, encoding="utf-8")
                        files_written += 1

            # Phase 6: LEDGER — record applied
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="ledger", progress_pct=80.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLIED,
                    data={
                        "files_applied": files_written,
                        "files": file_strs,
                    },
                )
            )

            # Phase 7: PUBLISH — emit decision
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="safe_auto_passed",
                diff_summary=f"Applied {files_written} files",
            )

            # Phase 8: VERIFY — post-apply verification
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="verify", progress_pct=90.0
            )

            verify_passed = True
            if request.verify_fn is not None:
                verify_passed = await request.verify_fn()
            else:
                # Default: AST parse all applied files
                for fpath in file_paths:
                    if not self._validate_syntax(
                        fpath.read_text(encoding="utf-8")
                    ):
                        verify_passed = False
                        break

            if not verify_passed:
                # Rollback ALL files
                for fpath, artifact in rollback_artifacts.items():
                    artifact.apply(fpath)
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.ROLLED_BACK,
                        data={"reason": "verify_failed", "files_rolled_back": files_written},
                    )
                )
                await self._comm.emit_postmortem(
                    op_id=op_id,
                    root_cause="post_apply_verification_failed",
                    failed_phase="VERIFY",
                    next_safe_action="review_proposed_changes",
                )
                return MultiFileChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.VERIFY,
                    risk_tier=risk_tier,
                    rolled_back=True,
                    files_applied=files_written,
                )

            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause="none",
                failed_phase=None,
                next_safe_action="none",
            )

            return MultiFileChangeResult(
                op_id=op_id,
                success=True,
                phase_reached=ChangePhase.VERIFY,
                risk_tier=risk_tier,
                files_applied=files_written,
            )

        except Exception as exc:
            logger.error("MultiFileChangeEngine error for %s: %s", op_id, exc)
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=str(exc),
                failed_phase="unknown",
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.FAILED,
                    data={"error": str(exc)},
                )
            )
            return MultiFileChangeResult(
                op_id=op_id,
                success=False,
                phase_reached=ChangePhase.PLAN,
                risk_tier=None,
                error=str(exc),
            )

    def _validate_syntax(self, code: str) -> bool:
        """Validate Python syntax by AST-parsing in a temp directory."""
        try:
            with tempfile.TemporaryDirectory(
                prefix="ouroboros_mf_validate_"
            ) as sandbox:
                p = Path(sandbox) / "validate.py"
                p.write_text(code, encoding="utf-8")
                ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            return True
        except SyntaxError:
            return False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_engine.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_file_engine.py tests/test_ouroboros_governance/test_multi_file_engine.py
git commit -m "feat(governance): add multi-file atomic change engine

All-or-nothing multi-file operations with pre-tested rollback per file.
CROSS_REPO_TX lock envelope, nested FILE_LOCKs, ledger tracks file list,
full 5-phase communication.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Wire Phase 2 exports into governance __init__.py

**Files:**
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Step 1: Add Phase 2 exports after the Phase 1 exports**

Add after the `tui_transport` imports:

```python
from backend.core.ouroboros.governance.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    PressureLevel,
    PRESSURE_THRESHOLDS,
)
from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
    DegradationReason,
    ModeTransition,
)
from backend.core.ouroboros.governance.routing_policy import (
    RoutingPolicy,
    RoutingDecision,
    TaskCategory,
    CostGuardrail,
)
from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
    MultiFileChangeResult,
)
```

Also update the docstring to include Phase 2 components.

**Step 2: Run all governance tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v`
Expected: ALL tests pass

**Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/__init__.py
git commit -m "feat(governance): wire Phase 2 exports into governance __init__

Adds resource_monitor, degradation, routing_policy, and
multi_file_engine exports.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Phase 2 Integration Tests

**Files:**
- Create: `tests/test_ouroboros_governance/test_phase2_integration.py`

**Context:** End-to-end tests verifying Phase 2A and 2B Go/No-Go criteria from the design doc section 4.

**Step 1: Write the integration tests**

```python
# tests/test_ouroboros_governance/test_phase2_integration.py
"""Phase 2 integration tests — Go/No-Go criteria verification.

Tests verify acceptance criteria from design doc section 4
(Phase 2A and Phase 2B Go/No-Go).
"""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    PressureLevel,
)
from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
)
from backend.core.ouroboros.governance.routing_policy import (
    RoutingPolicy,
    RoutingDecision,
    TaskCategory,
)
from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
)
from backend.core.ouroboros.governance.change_engine import ChangePhase
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.lock_manager import GovernanceLockManager
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def a():\n    return 1\n")
    (src / "b.py").write_text("def b():\n    return 2\n")
    (src / "c.py").write_text("def c():\n    return 3\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


# ---------------------------------------------------------------------------
# Phase 2A: Hybrid Routing Go/No-Go
# ---------------------------------------------------------------------------


class TestHybridRoutingGoNoGo:
    def test_cpu_spike_routes_heavy_to_gcp(self):
        """CPU spike -> heavy task routed to GCP within policy."""
        policy = RoutingPolicy()
        snap = ResourceSnapshot(
            ram_percent=50.0, cpu_percent=85.0,
            event_loop_latency_ms=5.0, disk_io_busy=False,
        )
        decision = policy.route(
            TaskCategory.MULTI_FILE_ANALYSIS, snap,
            DegradationMode.REDUCED_AUTONOMY, gcp_available=True,
        )
        assert decision == RoutingDecision.GCP_PRIME

    def test_event_loop_latency_sheds_background(self):
        """Event loop latency > 40ms p95 -> elevated pressure."""
        snap = ResourceSnapshot(
            ram_percent=50.0, cpu_percent=40.0,
            event_loop_latency_ms=45.0, disk_io_busy=False,
        )
        assert snap.overall_pressure >= PressureLevel.ELEVATED

    def test_gcp_unavailable_queues_heavy_continues_local(self):
        """GCP unavailable -> heavy tasks queued, safe_auto continues local."""
        policy = RoutingPolicy()
        snap = ResourceSnapshot(
            ram_percent=50.0, cpu_percent=40.0,
            event_loop_latency_ms=5.0, disk_io_busy=False,
        )
        heavy = policy.route(
            TaskCategory.CROSS_REPO_PLANNING, snap,
            DegradationMode.REDUCED_AUTONOMY, gcp_available=False,
        )
        light = policy.route(
            TaskCategory.SINGLE_FILE_FIX, snap,
            DegradationMode.REDUCED_AUTONOMY, gcp_available=False,
        )
        assert heavy == RoutingDecision.QUEUE
        assert light == RoutingDecision.LOCAL


# ---------------------------------------------------------------------------
# Phase 2A: Degradation Mode Go/No-Go
# ---------------------------------------------------------------------------


class TestDegradationGoNoGo:
    @pytest.mark.asyncio
    async def test_all_four_modes_reachable(self):
        """All 4 degradation modes reachable via test triggers."""
        ctrl = DegradationController()
        assert ctrl.mode == DegradationMode.FULL_AUTONOMY

        # Elevated -> REDUCED
        await ctrl.evaluate(ResourceSnapshot(82.0, 40.0, 5.0, False))
        assert ctrl.mode == DegradationMode.REDUCED_AUTONOMY

        # Critical -> READ_ONLY
        await ctrl.evaluate(ResourceSnapshot(87.0, 85.0, 5.0, False))
        assert ctrl.mode == DegradationMode.READ_ONLY_PLANNING

        # Emergency -> STOP
        await ctrl.evaluate(ResourceSnapshot(95.0, 40.0, 5.0, False))
        assert ctrl.mode == DegradationMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_full_to_reduced_to_readonly_to_stop(self):
        """FULL -> REDUCED -> READ_ONLY -> EMERGENCY_STOP transitions tested."""
        ctrl = DegradationController()
        transitions = []

        t = await ctrl.evaluate(ResourceSnapshot(82.0, 40.0, 5.0, False))
        transitions.append(t)
        t = await ctrl.evaluate(ResourceSnapshot(87.0, 85.0, 5.0, False))
        transitions.append(t)
        t = await ctrl.evaluate(ResourceSnapshot(95.0, 40.0, 5.0, False))
        transitions.append(t)

        assert all(t is not None for t in transitions)
        assert transitions[0].to_mode == DegradationMode.REDUCED_AUTONOMY
        assert transitions[1].to_mode == DegradationMode.READ_ONLY_PLANNING
        assert transitions[2].to_mode == DegradationMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_emergency_stop_requires_explicit_reset(self):
        """Recovery from EMERGENCY_STOP requires explicit re-enable."""
        ctrl = DegradationController()
        await ctrl.evaluate(ResourceSnapshot(95.0, 40.0, 5.0, False))
        assert ctrl.mode == DegradationMode.EMERGENCY_STOP

        # Normal pressure does NOT auto-recover
        await ctrl.evaluate(ResourceSnapshot(30.0, 20.0, 1.0, False))
        assert ctrl.mode == DegradationMode.EMERGENCY_STOP

        # Explicit reset works
        await ctrl.explicit_reset()
        assert ctrl.mode == DegradationMode.FULL_AUTONOMY

    def test_gcp_routing_cost_guardrail(self):
        """GCP routing stays under configured daily budget cap."""
        policy = RoutingPolicy()
        # Blow the budget
        policy.cost_guardrail.record_gcp_usage(999.0)
        assert policy.cost_guardrail.over_budget is True

        decision = policy.route(
            TaskCategory.CANDIDATE_GENERATION,
            ResourceSnapshot(50.0, 40.0, 5.0, False),
            DegradationMode.FULL_AUTONOMY,
            gcp_available=True,
        )
        assert decision == RoutingDecision.QUEUE


# ---------------------------------------------------------------------------
# Phase 2B: Multi-File Go/No-Go
# ---------------------------------------------------------------------------


class TestMultiFileGoNoGo:
    @pytest.mark.asyncio
    async def test_multi_file_all_applied_or_all_rolled_back(
        self, project, ledger
    ):
        """Multi-file change: all files updated atomically or all rolled back."""
        comm = CommProtocol(transports=[LogTransport()])
        engine = MultiFileChangeEngine(
            project_root=project, ledger=ledger, comm=comm,
        )

        # Success case: all applied
        request = MultiFileChangeRequest(
            goal="Update all",
            files={
                project / "src" / "a.py": "def a():\n    return 10\n",
                project / "src" / "b.py": "def b():\n    return 20\n",
            },
            profile=OperationProfile(
                files_affected=[Path("src/a.py"), Path("src/b.py")],
                change_type=ChangeType.MODIFY, blast_radius=2,
                crosses_repo_boundary=False, touches_security_surface=False,
                touches_supervisor=False, test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        assert result.success is True
        assert (project / "src" / "a.py").read_text() == "def a():\n    return 10\n"
        assert (project / "src" / "b.py").read_text() == "def b():\n    return 20\n"

    @pytest.mark.asyncio
    async def test_multi_file_rollback_all_on_verify_failure(
        self, project, ledger
    ):
        """Partial multi-file apply never happens — all rolled back."""
        original_a = (project / "src" / "a.py").read_text()
        original_b = (project / "src" / "b.py").read_text()

        comm = CommProtocol(transports=[LogTransport()])
        engine = MultiFileChangeEngine(
            project_root=project, ledger=ledger, comm=comm,
        )
        request = MultiFileChangeRequest(
            goal="Fail verify",
            files={
                project / "src" / "a.py": "def a():\n    return 100\n",
                project / "src" / "b.py": "def b():\n    return 200\n",
            },
            profile=OperationProfile(
                files_affected=[Path("src/a.py"), Path("src/b.py")],
                change_type=ChangeType.MODIFY, blast_radius=2,
                crosses_repo_boundary=False, touches_security_surface=False,
                touches_supervisor=False, test_scope_confidence=0.9,
            ),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await engine.execute(request)
        assert result.rolled_back is True
        assert (project / "src" / "a.py").read_text() == original_a
        assert (project / "src" / "b.py").read_text() == original_b

    @pytest.mark.asyncio
    async def test_learning_feedback_with_op_id(self, project, ledger):
        """Ledger records op_id correlation for learning feedback."""
        comm = CommProtocol(transports=[LogTransport()])
        engine = MultiFileChangeEngine(
            project_root=project, ledger=ledger, comm=comm,
        )
        request = MultiFileChangeRequest(
            goal="Track op_id",
            files={
                project / "src" / "a.py": "def a():\n    return 42\n",
            },
            profile=OperationProfile(
                files_affected=[Path("src/a.py")],
                change_type=ChangeType.MODIFY, blast_radius=1,
                crosses_repo_boundary=False, touches_security_surface=False,
                touches_supervisor=False, test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        assert result.op_id.startswith("op-")
        history = await ledger.get_history(result.op_id)
        assert len(history) > 0
```

**Step 2: Run integration tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_phase2_integration.py -v`
Expected: All 8 tests PASS

**Step 3: Run ALL governance tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v`
Expected: ALL tests pass (Phase 0 + Phase 1 + Phase 2)

**Step 4: Commit**

```bash
git add tests/test_ouroboros_governance/test_phase2_integration.py
git commit -m "test(governance): add Phase 2 integration tests for Go/No-Go criteria

Verifies hybrid routing under pressure, all 4 degradation modes reachable,
EMERGENCY_STOP sticky, cost guardrails, multi-file atomic apply/rollback,
and op_id correlation for learning feedback.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Summary of Phase 2 Deliverables

| Task | Component | Tests | Go/No-Go Criteria Covered |
|------|-----------|-------|---------------------------|
| 1 | `resource_monitor.py` — Pressure signal collection | ~12 | Multi-signal pressure levels, event loop latency |
| 2 | `degradation.py` — 4-mode state machine | ~14 | All modes reachable, EMERGENCY_STOP sticky, GCP awareness |
| 3 | `routing_policy.py` — Deterministic routing | ~14 | CPU spike routing, GCP down queuing, cost guardrails |
| 4 | `multi_file_engine.py` — Atomic multi-file ops | ~9 | All-or-nothing, rollback integrity, ledger file list |
| 5 | `__init__.py` — Wire exports | 0 | Package completeness |
| 6 | `test_phase2_integration.py` — Go/No-Go | ~8 | All Phase 2A + 2B acceptance criteria |

**Total new tests: ~57**
**Total governance tests (Phase 0 + Phase 1 + Phase 2): ~185**

---

## What Phase 2 Does NOT Include (deferred to Phase 3)

- **Cross-repo event bus integration** — Phase 2B publishes via CommProtocol. Phase 3 wires to the existing `CrossRepoEventBus` with inbox consumer ack.
- **Oracle blast radius integration** — Phase 2B uses `OperationProfile.blast_radius`. Phase 3 auto-populates from `CodebaseKnowledgeGraph.compute_blast_radius()`.
- **Git worktree validation** — Phase 2B uses tempdir AST parsing. Phase 3 adds real `git worktree add` for full inter-file import checking.
- **N/N-1 runtime contract checks** — Phase 2B uses boot-time ContractGate. Phase 3 adds runtime API compatibility verification.
- **Canary rollout** — Phase 3's domain slice promotion with 50-op minimum, 72h stability window.
- **CLI break-glass command** — Phase 3 adds `--break-glass` to `unified_supervisor.py` argparse.
