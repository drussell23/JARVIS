# Memory Control Plane Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Centralized memory admission control that prevents concurrent model loading from exceeding physical RAM on Apple Silicon.

**Architecture:** New `MemoryBudgetBroker` consuming `MemoryQuantizer` as sole signal source. All model loaders (LLM, Whisper, ECAPA, SentenceTransformer) must acquire a transactional grant before allocating. Phase-gated startup prevents additive spikes.

**Tech Stack:** Python 3.11+, asyncio, psutil (confined to MemoryQuantizer), macOS `memory_pressure`/`vm_stat` kernel tools, pytest

**Design doc:** `docs/plans/2026-03-03-memory-control-plane-design.md`

---

## Task 1: Create Memory Type Definitions

**Files:**
- Create: `backend/core/memory_types.py`
- Test: `tests/unit/test_memory_types.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_memory_types.py
"""Tests for memory control plane type definitions."""
import pytest
from backend.core.memory_types import (
    PressureTier, KernelPressure, ThrashState, SignalQuality,
    PressureTrend, BudgetPriority, StartupPhase, LeaseState,
    MemorySnapshot, DegradationOption, ConfigProof, LoadResult,
    MemoryBudgetEventType,
)


class TestPressureTier:
    def test_ordering(self):
        assert PressureTier.ABUNDANT.value < PressureTier.EMERGENCY.value

    def test_all_tiers_present(self):
        names = {t.name for t in PressureTier}
        assert names == {"ABUNDANT", "OPTIMAL", "ELEVATED", "CONSTRAINED", "CRITICAL", "EMERGENCY"}


class TestBudgetPriority:
    def test_ordering(self):
        assert BudgetPriority.BOOT_CRITICAL.value < BudgetPriority.BACKGROUND.value

    def test_all_priorities(self):
        names = {p.name for p in BudgetPriority}
        assert names == {"BOOT_CRITICAL", "BOOT_OPTIONAL", "RUNTIME_INTERACTIVE", "BACKGROUND"}


class TestStartupPhase:
    def test_all_phases(self):
        names = {p.name for p in StartupPhase}
        assert names == {"BOOT_CRITICAL", "BOOT_OPTIONAL", "RUNTIME_INTERACTIVE", "BACKGROUND"}


class TestLeaseState:
    def test_terminal_states(self):
        terminal = {LeaseState.RELEASED, LeaseState.ROLLED_BACK, LeaseState.EXPIRED, LeaseState.PREEMPTED, LeaseState.DENIED}
        for state in terminal:
            assert state.is_terminal

    def test_non_terminal_states(self):
        non_terminal = {LeaseState.PENDING, LeaseState.GRANTED, LeaseState.ACTIVE}
        for state in non_terminal:
            assert not state.is_terminal


class TestMemorySnapshot:
    def test_immutable(self):
        snap = _make_snapshot()
        with pytest.raises(AttributeError):
            snap.physical_total = 0

    def test_headroom_subtracts_safety_floor(self):
        snap = _make_snapshot(
            usable_bytes=10_000_000_000,
            committed_bytes=4_000_000_000,
            safety_floor_bytes=1_600_000_000,
        )
        # available_budget = usable - committed = 6GB
        # headroom = available_budget - safety_floor = 4.4GB
        assert snap.headroom_bytes == 4_400_000_000

    def test_headroom_never_negative(self):
        snap = _make_snapshot(
            usable_bytes=2_000_000_000,
            committed_bytes=1_500_000_000,
            safety_floor_bytes=1_600_000_000,
        )
        assert snap.headroom_bytes == 0

    def test_pressure_factor(self):
        for tier, expected in [
            (PressureTier.ABUNDANT, 1.0),
            (PressureTier.CRITICAL, 0.5),
            (PressureTier.EMERGENCY, 0.3),
        ]:
            snap = _make_snapshot(pressure_tier=tier)
            assert snap.pressure_factor == expected

    def test_swap_hysteresis_active(self):
        snap = _make_snapshot(swap_growth_rate_bps=60 * 1024 * 1024)
        assert snap.swap_hysteresis_active is True

    def test_swap_hysteresis_inactive(self):
        snap = _make_snapshot(swap_growth_rate_bps=10 * 1024 * 1024)
        assert snap.swap_hysteresis_active is False


class TestDegradationOption:
    def test_fields(self):
        opt = DegradationOption(
            name="reduce_context_2048",
            bytes_required=500_000_000,
            quality_impact="Context reduced to 2048",
            constraints={"max_context": 2048},
        )
        assert opt.name == "reduce_context_2048"
        assert opt.constraints["max_context"] == 2048


class TestConfigProof:
    def test_compliant(self):
        proof = ConfigProof(
            component_id="llm:test@v1",
            requested_constraints={"max_context": 2048},
            applied_config={"n_ctx": 2048},
            compliant=True,
            evidence={"n_ctx": 2048},
        )
        assert proof.compliant is True


class TestLoadResult:
    def test_success(self):
        proof = ConfigProof("test", {}, {}, True, {})
        result = LoadResult(success=True, actual_bytes=100, config_proof=proof,
                           model_handle=object(), load_duration_ms=50.0)
        assert result.success

    def test_failure(self):
        result = LoadResult(success=False, actual_bytes=0, config_proof=None,
                           model_handle=None, load_duration_ms=10.0, error="OOM")
        assert result.error == "OOM"


def _make_snapshot(**overrides):
    """Helper to build a MemorySnapshot with sensible defaults."""
    defaults = dict(
        physical_total=17_179_869_184,
        physical_wired=3_000_000_000,
        physical_active=5_000_000_000,
        physical_inactive=2_000_000_000,
        physical_compressed=1_000_000_000,
        physical_free=6_000_000_000,
        swap_total=8_000_000_000,
        swap_used=500_000_000,
        swap_growth_rate_bps=0.0,
        usable_bytes=13_000_000_000,
        committed_bytes=0,
        available_budget_bytes=13_000_000_000,
        kernel_pressure=KernelPressure.NORMAL,
        pressure_tier=PressureTier.ABUNDANT,
        thrash_state=ThrashState.HEALTHY,
        pageins_per_sec=0.0,
        host_rss_slope_bps=0.0,
        jarvis_tree_rss_slope_bps=0.0,
        swap_slope_bps=0.0,
        pressure_trend=PressureTrend.STABLE,
        safety_floor_bytes=1_600_000_000,
        compressed_trend_bytes=500_000_000,
        signal_quality=SignalQuality.GOOD,
        timestamp=1000.0,
        max_age_ms=0,
        epoch=1,
        snapshot_id="test-snapshot-001",
    )
    defaults.update(overrides)
    return MemorySnapshot(**defaults)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_memory_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.memory_types'`

**Step 3: Write the implementation**

```python
# backend/core/memory_types.py
"""Memory Control Plane type definitions.

Canonical types shared across MemoryQuantizer, MemoryBudgetBroker,
and all BudgetedLoader implementations. Single source of truth for
enums, snapshots, and contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, Enum
from typing import Any, Dict, List, Optional


# ─── Enums ───────────────────────────────────────────────────────────

class PressureTier(IntEnum):
    ABUNDANT = 0
    OPTIMAL = 1
    ELEVATED = 2
    CONSTRAINED = 3
    CRITICAL = 4
    EMERGENCY = 5


class KernelPressure(str, Enum):
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"


class ThrashState(str, Enum):
    HEALTHY = "healthy"
    THRASHING = "thrashing"
    EMERGENCY = "emergency"


class SignalQuality(str, Enum):
    GOOD = "good"
    DEGRADED = "degraded"
    FALLBACK = "fallback"


class PressureTrend(str, Enum):
    STABLE = "stable"
    RISING = "rising"
    FALLING = "falling"


class BudgetPriority(IntEnum):
    BOOT_CRITICAL = 0
    BOOT_OPTIONAL = 1
    RUNTIME_INTERACTIVE = 2
    BACKGROUND = 3


class StartupPhase(IntEnum):
    BOOT_CRITICAL = 0
    BOOT_OPTIONAL = 1
    RUNTIME_INTERACTIVE = 2
    BACKGROUND = 3


class LeaseState(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    ACTIVE = "active"
    RELEASED = "released"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"
    PREEMPTED = "preempted"
    DENIED = "denied"

    @property
    def is_terminal(self) -> bool:
        return self in {
            LeaseState.RELEASED,
            LeaseState.ROLLED_BACK,
            LeaseState.EXPIRED,
            LeaseState.PREEMPTED,
            LeaseState.DENIED,
        }


class MemoryBudgetEventType(str, Enum):
    GRANT_REQUESTED = "grant_requested"
    GRANT_ISSUED = "grant_issued"
    GRANT_DENIED = "grant_denied"
    GRANT_DEGRADED = "grant_degraded"
    GRANT_QUEUED = "grant_queued"
    HEARTBEAT = "heartbeat"
    COMMIT = "commit"
    COMMIT_OVERRUN = "commit_overrun"
    ROLLBACK = "rollback"
    RELEASE_REQUESTED = "release_requested"
    RELEASE_VERIFIED = "release_verified"
    RELEASE_FAILED = "release_failed"
    PREEMPT_REQUESTED = "preempt_requested"
    PREEMPT_COOPERATIVE = "preempt_cooperative"
    PREEMPT_FORCED = "preempt_forced"
    LEASE_EXPIRED = "lease_expired"
    RECONCILIATION = "reconciliation"
    PHASE_TRANSITION = "phase_transition"
    SWAP_HYSTERESIS_TRIP = "swap_hysteresis_trip"
    SWAP_HYSTERESIS_RECOVER = "swap_hysteresis_recover"
    LOADER_QUARANTINED = "loader_quarantined"
    LOADER_UNQUARANTINED = "loader_unquarantined"
    ESTIMATE_CALIBRATION = "estimate_calibration"
    SNAPSHOT_STALE_REJECTED = "snapshot_stale_rejected"


# ─── Snapshot ────────────────────────────────────────────────────────

_PRESSURE_FACTORS = {
    PressureTier.ABUNDANT: 1.0,
    PressureTier.OPTIMAL: 0.95,
    PressureTier.ELEVATED: 0.85,
    PressureTier.CONSTRAINED: 0.7,
    PressureTier.CRITICAL: 0.5,
    PressureTier.EMERGENCY: 0.3,
}

_SWAP_HYSTERESIS_THRESHOLD_BPS = 50 * 1024 * 1024  # 50 MB/s


@dataclass(frozen=True)
class MemorySnapshot:
    """Immutable point-in-time memory state. Single source of truth.

    Every budget decision, tier calculation, and telemetry event
    consumes this object. No component may call psutil, vm_stat,
    or memory_pressure directly.
    """
    # Physical truth (bytes)
    physical_total: int
    physical_wired: int
    physical_active: int
    physical_inactive: int
    physical_compressed: int
    physical_free: int

    # Swap state
    swap_total: int
    swap_used: int
    swap_growth_rate_bps: float

    # Derived budget fields
    usable_bytes: int
    committed_bytes: int
    available_budget_bytes: int

    # Pressure signals
    kernel_pressure: KernelPressure
    pressure_tier: PressureTier
    thrash_state: ThrashState
    pageins_per_sec: float

    # Trend derivatives (30s window)
    host_rss_slope_bps: float
    jarvis_tree_rss_slope_bps: float
    swap_slope_bps: float
    pressure_trend: PressureTrend

    # Safety
    safety_floor_bytes: int
    compressed_trend_bytes: int

    # Signal quality
    signal_quality: SignalQuality

    # Metadata
    timestamp: float
    max_age_ms: int
    epoch: int
    snapshot_id: str

    @property
    def headroom_bytes(self) -> int:
        return max(0, self.available_budget_bytes - self.safety_floor_bytes)

    @property
    def pressure_factor(self) -> float:
        return _PRESSURE_FACTORS.get(self.pressure_tier, 0.5)

    @property
    def swap_hysteresis_active(self) -> bool:
        return self.swap_growth_rate_bps > _SWAP_HYSTERESIS_THRESHOLD_BPS


# ─── Contract Types ──────────────────────────────────────────────────

@dataclass
class DegradationOption:
    name: str
    bytes_required: int
    quality_impact: str
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfigProof:
    component_id: str
    requested_constraints: Dict[str, Any]
    applied_config: Dict[str, Any]
    compliant: bool
    evidence: Dict[str, Any]


@dataclass
class LoadResult:
    success: bool
    actual_bytes: int
    config_proof: Optional[ConfigProof]
    model_handle: Any
    load_duration_ms: float
    error: Optional[str] = None
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_memory_types.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/memory_types.py tests/unit/test_memory_types.py
git commit -m "feat(memory): add Memory Control Plane type definitions

Canonical enums (PressureTier, BudgetPriority, StartupPhase, LeaseState),
immutable MemorySnapshot, and contract types (ConfigProof, LoadResult,
DegradationOption) shared across broker and loaders."
```

---

## Task 2: Add `snapshot()` Method to MemoryQuantizer

**Files:**
- Modify: `backend/core/memory_quantizer.py:748-800` (tier calc), `1413-1460` (reservations), `1563-1591` (singleton)
- Test: `tests/unit/test_memory_quantizer_snapshot.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_memory_quantizer_snapshot.py
"""Tests for MemoryQuantizer.snapshot() producing MemorySnapshot."""
import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from backend.core.memory_types import (
    MemorySnapshot, PressureTier, KernelPressure, ThrashState,
    SignalQuality, PressureTrend,
)


class TestMemoryQuantizerSnapshot:
    @pytest.fixture
    def mock_psutil_mem(self):
        mem = MagicMock()
        mem.total = 17_179_869_184  # 16 GB
        mem.available = 8_000_000_000
        mem.percent = 53.0
        mem.free = 2_000_000_000
        return mem

    @pytest.fixture
    def mock_swap(self):
        swap = MagicMock()
        swap.total = 8_000_000_000
        swap.used = 500_000_000
        swap.percent = 6.25
        return swap

    @pytest.mark.asyncio
    async def test_snapshot_returns_memory_snapshot(self, mock_psutil_mem, mock_swap):
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil, \
             patch("backend.core.memory_quantizer.MemoryQuantizer._read_kernel_pressure",
                   new_callable=AsyncMock, return_value="normal"), \
             patch("backend.core.memory_quantizer.MemoryQuantizer._read_vm_stat_pageins",
                   new_callable=AsyncMock, return_value=5.0):
            mock_psutil.virtual_memory.return_value = mock_psutil_mem
            mock_psutil.swap_memory.return_value = mock_swap

            from backend.core.memory_quantizer import MemoryQuantizer
            mq = MemoryQuantizer.__new__(MemoryQuantizer)
            mq._initialized = True
            mq._current_tier = "ABUNDANT"
            mq._thrash_state = "healthy"
            mq._memory_reservations = {}
            mq._ema_swap_growth = MagicMock(value=0.0)
            mq._ema_pageins = MagicMock(value=5.0)
            mq._ema_rss_slope = MagicMock(value=0.0)
            mq._ema_compressed = MagicMock(value=500_000_000)
            mq._supervisor_epoch = 1
            mq._broker_ref = None

            snap = await mq.snapshot()

            assert isinstance(snap, MemorySnapshot)
            assert snap.physical_total == 17_179_869_184
            assert snap.epoch == 1
            assert snap.signal_quality == SignalQuality.GOOD
            assert isinstance(snap.pressure_tier, PressureTier)

    @pytest.mark.asyncio
    async def test_snapshot_epoch_matches_supervisor(self, mock_psutil_mem, mock_swap):
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil, \
             patch("backend.core.memory_quantizer.MemoryQuantizer._read_kernel_pressure",
                   new_callable=AsyncMock, return_value="normal"), \
             patch("backend.core.memory_quantizer.MemoryQuantizer._read_vm_stat_pageins",
                   new_callable=AsyncMock, return_value=0.0):
            mock_psutil.virtual_memory.return_value = mock_psutil_mem
            mock_psutil.swap_memory.return_value = mock_swap

            from backend.core.memory_quantizer import MemoryQuantizer
            mq = MemoryQuantizer.__new__(MemoryQuantizer)
            mq._initialized = True
            mq._current_tier = "OPTIMAL"
            mq._thrash_state = "healthy"
            mq._memory_reservations = {}
            mq._ema_swap_growth = MagicMock(value=0.0)
            mq._ema_pageins = MagicMock(value=0.0)
            mq._ema_rss_slope = MagicMock(value=0.0)
            mq._ema_compressed = MagicMock(value=0)
            mq._supervisor_epoch = 42
            mq._broker_ref = None

            snap = await mq.snapshot()
            assert snap.epoch == 42

    @pytest.mark.asyncio
    async def test_snapshot_degraded_on_pressure_failure(self, mock_psutil_mem, mock_swap):
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil, \
             patch("backend.core.memory_quantizer.MemoryQuantizer._read_kernel_pressure",
                   new_callable=AsyncMock, side_effect=OSError("command not found")), \
             patch("backend.core.memory_quantizer.MemoryQuantizer._read_vm_stat_pageins",
                   new_callable=AsyncMock, return_value=0.0):
            mock_psutil.virtual_memory.return_value = mock_psutil_mem
            mock_psutil.swap_memory.return_value = mock_swap

            from backend.core.memory_quantizer import MemoryQuantizer
            mq = MemoryQuantizer.__new__(MemoryQuantizer)
            mq._initialized = True
            mq._current_tier = "ABUNDANT"
            mq._thrash_state = "healthy"
            mq._memory_reservations = {}
            mq._ema_swap_growth = MagicMock(value=0.0)
            mq._ema_pageins = MagicMock(value=0.0)
            mq._ema_rss_slope = MagicMock(value=0.0)
            mq._ema_compressed = MagicMock(value=0)
            mq._supervisor_epoch = 1
            mq._broker_ref = None

            snap = await mq.snapshot()
            assert snap.signal_quality == SignalQuality.DEGRADED
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_memory_quantizer_snapshot.py -v`
Expected: FAIL — `AttributeError: 'MemoryQuantizer' object has no attribute 'snapshot'`

**Step 3: Implement `snapshot()` method**

Add to `backend/core/memory_quantizer.py` after the `get_reservations()` method (~line 1460):

```python
async def snapshot(self, max_age_ms: int = 0) -> "MemorySnapshot":
    """Produce canonical MemorySnapshot. Called by broker, never by loaders.

    Args:
        max_age_ms: staleness hint for callers (stored on snapshot, not enforced here)
    """
    import uuid as _uuid
    from backend.core.memory_types import (
        MemorySnapshot, PressureTier, KernelPressure, ThrashState,
        SignalQuality, PressureTrend,
    )

    signal_quality = SignalQuality.GOOD

    # Collect raw signals
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    try:
        kernel_pressure_raw = await self._read_kernel_pressure()
        kernel_pressure = KernelPressure(kernel_pressure_raw)
    except Exception:
        kernel_pressure = KernelPressure.WARN
        signal_quality = SignalQuality.DEGRADED

    try:
        pageins = await self._read_vm_stat_pageins()
    except Exception:
        pageins = self._ema_pageins.value if hasattr(self._ema_pageins, 'value') else 0.0
        if signal_quality == SignalQuality.GOOD:
            signal_quality = SignalQuality.DEGRADED

    # Extract macOS-specific memory breakdown
    wired = getattr(mem, 'wired', 0) or 0
    active = getattr(mem, 'active', 0) or 0
    inactive = getattr(mem, 'inactive', 0) or 0
    # compressed comes from vm_stat EMA
    compressed = self._ema_compressed.value if hasattr(self._ema_compressed, 'value') else 0
    compressed = int(compressed)
    free = mem.free

    # Map tier string to enum
    tier_map = {
        "ABUNDANT": PressureTier.ABUNDANT, "OPTIMAL": PressureTier.OPTIMAL,
        "ELEVATED": PressureTier.ELEVATED, "CONSTRAINED": PressureTier.CONSTRAINED,
        "CRITICAL": PressureTier.CRITICAL, "EMERGENCY": PressureTier.EMERGENCY,
    }
    pressure_tier = tier_map.get(self._current_tier, PressureTier.CONSTRAINED)

    # Map thrash state
    thrash_map = {
        "healthy": ThrashState.HEALTHY, "thrashing": ThrashState.THRASHING,
        "emergency": ThrashState.EMERGENCY,
    }
    thrash_state = thrash_map.get(self._thrash_state, ThrashState.HEALTHY)

    # Safety floor: scales with pressure tier
    safety_pct = 0.10
    tier_multipliers = {
        PressureTier.ABUNDANT: 1.0, PressureTier.OPTIMAL: 1.0,
        PressureTier.ELEVATED: 1.25, PressureTier.CONSTRAINED: 1.5,
        PressureTier.CRITICAL: 2.0, PressureTier.EMERGENCY: 2.5,
    }
    safety_floor = int(mem.total * safety_pct * tier_multipliers.get(pressure_tier, 2.0))

    compressed_trend = int(self._ema_compressed.value) if hasattr(self._ema_compressed, 'value') else 0

    # usable = total - wired - compressed_trend (safety floor applied only in headroom)
    usable = max(0, mem.total - wired - compressed_trend)

    # committed = sum of broker ACTIVE leases
    committed = 0
    if self._broker_ref is not None:
        try:
            committed = self._broker_ref.get_committed_bytes()
        except Exception:
            pass

    available_budget = max(0, usable - committed)

    # Swap growth rate
    swap_growth = self._ema_swap_growth.value if hasattr(self._ema_swap_growth, 'value') else 0.0

    # RSS slopes
    host_rss_slope = 0.0  # TODO: implement host-level RSS tracking
    jarvis_tree_slope = self._ema_rss_slope.value if hasattr(self._ema_rss_slope, 'value') else 0.0

    # Pressure trend (simple heuristic from tier history)
    pressure_trend = PressureTrend.STABLE  # TODO: derive from tier change history

    return MemorySnapshot(
        physical_total=mem.total,
        physical_wired=wired,
        physical_active=active,
        physical_inactive=inactive,
        physical_compressed=compressed,
        physical_free=free,
        swap_total=swap.total,
        swap_used=swap.used,
        swap_growth_rate_bps=swap_growth,
        usable_bytes=usable,
        committed_bytes=committed,
        available_budget_bytes=available_budget,
        kernel_pressure=kernel_pressure,
        pressure_tier=pressure_tier,
        thrash_state=thrash_state,
        pageins_per_sec=pageins,
        host_rss_slope_bps=host_rss_slope,
        jarvis_tree_rss_slope_bps=jarvis_tree_slope,
        swap_slope_bps=swap_growth,
        pressure_trend=pressure_trend,
        safety_floor_bytes=safety_floor,
        compressed_trend_bytes=compressed_trend,
        signal_quality=signal_quality,
        timestamp=time.monotonic(),
        max_age_ms=max_age_ms,
        epoch=getattr(self, '_supervisor_epoch', 0),
        snapshot_id=str(_uuid.uuid4()),
    )
```

Also add to `MemoryQuantizer.__init__` (~line 378-463):

```python
self._supervisor_epoch: int = 0
self._broker_ref = None  # Set by MemoryBudgetBroker after init
```

And add a setter:

```python
def set_supervisor_epoch(self, epoch: int) -> None:
    self._supervisor_epoch = epoch

def set_broker_ref(self, broker) -> None:
    self._broker_ref = broker
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_memory_quantizer_snapshot.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/memory_quantizer.py tests/unit/test_memory_quantizer_snapshot.py
git commit -m "feat(memory): add snapshot() to MemoryQuantizer

Returns canonical MemorySnapshot with macOS-aware signals, safety floor,
signal quality tracking, and epoch fencing. Broker ref for committed_bytes."
```

---

## Task 3: Implement MemoryBudgetBroker Core (Grant/Rollback/Commit)

**Files:**
- Create: `backend/core/memory_budget_broker.py`
- Test: `tests/unit/test_memory_budget_broker.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_memory_budget_broker.py
"""Tests for MemoryBudgetBroker core grant lifecycle."""
import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState, PressureTier,
    MemorySnapshot, KernelPressure, ThrashState, SignalQuality,
    PressureTrend, DegradationOption, ConfigProof,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker, BudgetGrant


def _make_snapshot(**overrides):
    defaults = dict(
        physical_total=17_179_869_184, physical_wired=3_000_000_000,
        physical_active=5_000_000_000, physical_inactive=2_000_000_000,
        physical_compressed=1_000_000_000, physical_free=6_000_000_000,
        swap_total=8_000_000_000, swap_used=500_000_000,
        swap_growth_rate_bps=0.0, usable_bytes=13_000_000_000,
        committed_bytes=0, available_budget_bytes=13_000_000_000,
        kernel_pressure=KernelPressure.NORMAL, pressure_tier=PressureTier.ABUNDANT,
        thrash_state=ThrashState.HEALTHY, pageins_per_sec=0.0,
        host_rss_slope_bps=0.0, jarvis_tree_rss_slope_bps=0.0,
        swap_slope_bps=0.0, pressure_trend=PressureTrend.STABLE,
        safety_floor_bytes=1_600_000_000, compressed_trend_bytes=500_000_000,
        signal_quality=SignalQuality.GOOD, timestamp=1000.0, max_age_ms=0,
        epoch=1, snapshot_id="test-001",
    )
    defaults.update(overrides)
    return MemorySnapshot(**defaults)


class TestBrokerGrant:
    @pytest.fixture
    def mock_quantizer(self):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        return mq

    @pytest.fixture
    def broker(self, mock_quantizer):
        b = MemoryBudgetBroker(quantizer=mock_quantizer, epoch=1)
        b.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return b

    @pytest.mark.asyncio
    async def test_grant_issued_when_headroom_sufficient(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        assert isinstance(grant, BudgetGrant)
        assert grant.granted_bytes == 500_000_000
        assert grant.state == LeaseState.GRANTED

    @pytest.mark.asyncio
    async def test_grant_denied_when_headroom_insufficient(self, broker, mock_quantizer):
        mock_quantizer.snapshot.return_value = _make_snapshot(
            available_budget_bytes=500_000_000,
            safety_floor_bytes=400_000_000,
        )
        with pytest.raises(Exception, match="denied|insufficient"):
            await broker.request(
                component="test:v1",
                bytes_requested=5_000_000_000,
                priority=BudgetPriority.RUNTIME_INTERACTIVE,
                phase=StartupPhase.RUNTIME_INTERACTIVE,
                deadline=time.monotonic() + 0.1,
            )

    @pytest.mark.asyncio
    async def test_commit_transitions_to_active(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        proof = ConfigProof("test:v1", {}, {}, True, {})
        await grant.commit(actual_bytes=480_000_000, config_proof=proof)
        assert grant.state == LeaseState.ACTIVE

    @pytest.mark.asyncio
    async def test_rollback_releases_capacity(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        await grant.rollback(reason="test failure")
        assert grant.state == LeaseState.ROLLED_BACK
        assert broker.get_committed_bytes() == 0

    @pytest.mark.asyncio
    async def test_rollback_idempotent(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        await grant.rollback(reason="first")
        await grant.rollback(reason="second")  # no error
        assert grant.state == LeaseState.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_release_after_commit(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        proof = ConfigProof("test:v1", {}, {}, True, {})
        await grant.commit(actual_bytes=480_000_000, config_proof=proof)
        assert broker.get_committed_bytes() == 480_000_000

        await grant.release()
        assert grant.state == LeaseState.RELEASED
        assert broker.get_committed_bytes() == 0

    @pytest.mark.asyncio
    async def test_context_manager_rollback_on_exception(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        with pytest.raises(RuntimeError):
            async with grant:
                raise RuntimeError("load failed")
        assert grant.state == LeaseState.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_epoch_mismatch_rejected(self, broker):
        grant = await broker.request(
            component="test:v1",
            bytes_requested=500_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        broker._epoch = 2  # simulate restart
        with pytest.raises(Exception, match="stale.*epoch"):
            proof = ConfigProof("test:v1", {}, {}, True, {})
            await grant.commit(actual_bytes=480_000_000, config_proof=proof)

    @pytest.mark.asyncio
    async def test_get_committed_bytes_tracks_active(self, broker):
        g1 = await broker.request("a:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        g2 = await broker.request("b:v1", 200_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        await g1.commit(90_000_000, ConfigProof("a:v1", {}, {}, True, {}))
        await g2.commit(210_000_000, ConfigProof("b:v1", {}, {}, True, {}))
        assert broker.get_committed_bytes() == 300_000_000

    @pytest.mark.asyncio
    async def test_degraded_grant_when_full_doesnt_fit(self, broker, mock_quantizer):
        mock_quantizer.snapshot.return_value = _make_snapshot(
            available_budget_bytes=2_000_000_000,
            safety_floor_bytes=1_600_000_000,
        )
        # headroom = 400MB, requesting 1GB with degradation to 300MB
        grant = await broker.request(
            component="test:v1",
            bytes_requested=1_000_000_000,
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
            can_degrade=True,
            degradation_options=[
                DegradationOption("small", 300_000_000, "smaller model", {"size": "small"}),
            ],
        )
        assert grant.degraded is True
        assert grant.granted_bytes == 300_000_000
        assert grant.constraints == {"size": "small"}


class TestBrokerPhasePolicy:
    @pytest.fixture
    def mock_quantizer(self):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        return mq

    @pytest.fixture
    def broker(self, mock_quantizer):
        return MemoryBudgetBroker(quantizer=mock_quantizer, epoch=1)

    @pytest.mark.asyncio
    async def test_boot_critical_rejects_optional_priority(self, broker):
        broker.set_phase(StartupPhase.BOOT_CRITICAL)
        with pytest.raises(Exception, match="priority.*not allowed"):
            await broker.request(
                component="test:v1",
                bytes_requested=100_000_000,
                priority=BudgetPriority.BOOT_OPTIONAL,
                phase=StartupPhase.BOOT_CRITICAL,
                deadline=time.monotonic() + 0.1,
            )

    @pytest.mark.asyncio
    async def test_phase_concurrent_limit(self, broker, mock_quantizer):
        broker.set_phase(StartupPhase.BOOT_CRITICAL)
        # BOOT_CRITICAL allows only 1 concurrent grant
        g1 = await broker.request("a:v1", 100_000_000, BudgetPriority.BOOT_CRITICAL, StartupPhase.BOOT_CRITICAL)
        # Second should block or be denied
        with pytest.raises(Exception):
            await broker.request(
                "b:v1", 100_000_000, BudgetPriority.BOOT_CRITICAL,
                StartupPhase.BOOT_CRITICAL, deadline=time.monotonic() + 0.2,
            )
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_memory_budget_broker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.memory_budget_broker'`

**Step 3: Implement MemoryBudgetBroker**

```python
# backend/core/memory_budget_broker.py
"""Memory Budget Broker — single admission authority for all model loads.

No model loader may allocate memory-intensive resources without a grant
from this broker. Consumes MemoryQuantizer as its sole signal source.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState, MemorySnapshot,
    DegradationOption, ConfigProof, MemoryBudgetEventType, SignalQuality,
)

if TYPE_CHECKING:
    from backend.core.memory_quantizer import MemoryQuantizer

logger = logging.getLogger(__name__)

# ─── Phase Policy ────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhasePolicy:
    max_concurrent: int
    budget_cap_pct: float  # of physical RAM, before pressure_factor
    allowed_priorities: frozenset


_PHASE_POLICIES: Dict[StartupPhase, PhasePolicy] = {
    StartupPhase.BOOT_CRITICAL: PhasePolicy(
        max_concurrent=int(os.getenv("JARVIS_MCP_BOOT_CRITICAL_CONCURRENT", "1")),
        budget_cap_pct=float(os.getenv("JARVIS_MCP_PHASE_CAP_BOOT_CRITICAL", "0.60")),
        allowed_priorities=frozenset({BudgetPriority.BOOT_CRITICAL}),
    ),
    StartupPhase.BOOT_OPTIONAL: PhasePolicy(
        max_concurrent=int(os.getenv("JARVIS_MCP_BOOT_OPTIONAL_CONCURRENT", "2")),
        budget_cap_pct=float(os.getenv("JARVIS_MCP_PHASE_CAP_BOOT_OPTIONAL", "0.70")),
        allowed_priorities=frozenset({BudgetPriority.BOOT_CRITICAL, BudgetPriority.BOOT_OPTIONAL}),
    ),
    StartupPhase.RUNTIME_INTERACTIVE: PhasePolicy(
        max_concurrent=int(os.getenv("JARVIS_MCP_RUNTIME_CONCURRENT", "3")),
        budget_cap_pct=float(os.getenv("JARVIS_MCP_PHASE_CAP_RUNTIME", "0.80")),
        allowed_priorities=frozenset({BudgetPriority.BOOT_CRITICAL, BudgetPriority.BOOT_OPTIONAL, BudgetPriority.RUNTIME_INTERACTIVE}),
    ),
    StartupPhase.BACKGROUND: PhasePolicy(
        max_concurrent=int(os.getenv("JARVIS_MCP_BACKGROUND_CONCURRENT", "2")),
        budget_cap_pct=float(os.getenv("JARVIS_MCP_PHASE_CAP_BACKGROUND", "0.70")),
        allowed_priorities=frozenset(BudgetPriority),
    ),
}

# ─── Errors ──────────────────────────────────────────────────────────

class BudgetDeniedError(Exception):
    """Grant request denied."""
    def __init__(self, reason: str, snapshot_id: Optional[str] = None):
        self.reason = reason
        self.snapshot_id = snapshot_id
        super().__init__(f"Budget denied: {reason}")


class StaleEpochError(Exception):
    """Operation attempted with stale epoch."""


class ConstraintViolationError(Exception):
    """Loader violated grant constraints."""


# ─── BudgetGrant ─────────────────────────────────────────────────────

class BudgetGrant:
    """Transactional memory grant. Use as async context manager."""

    def __init__(
        self,
        broker: 'MemoryBudgetBroker',
        lease_id: str,
        component_id: str,
        granted_bytes: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        epoch: int,
        ttl_seconds: float,
        degraded: bool = False,
        degradation_applied: Optional[str] = None,
        constraints: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ):
        self._broker = broker
        self.lease_id = lease_id
        self.component_id = component_id
        self.granted_bytes = granted_bytes
        self.priority = priority
        self.phase = phase
        self.epoch = epoch
        self.degraded = degraded
        self.degradation_applied = degradation_applied
        self.constraints = constraints or {}
        self.trace_id = trace_id or str(uuid.uuid4())
        self.created_at = time.monotonic()
        self.expires_at = self.created_at + ttl_seconds
        self.state = LeaseState.GRANTED
        self.actual_bytes: Optional[int] = None
        self.config_proof: Optional[ConfigProof] = None
        self._committed = False
        self._rolled_back = False

    async def heartbeat(self) -> None:
        if self.state.is_terminal:
            return
        self._broker._validate_epoch(self.epoch)
        ttl = float(os.getenv("JARVIS_MCP_LEASE_TTL_SECONDS", "120"))
        self.expires_at = time.monotonic() + ttl

    async def commit(self, actual_bytes: int, config_proof: Optional[ConfigProof] = None) -> None:
        if self._committed or self.state == LeaseState.ACTIVE:
            return  # idempotent
        if self.state.is_terminal:
            return  # already rolled back/expired/preempted
        self._broker._validate_epoch(self.epoch)
        self.actual_bytes = actual_bytes
        self.config_proof = config_proof
        self.state = LeaseState.ACTIVE
        self._committed = True
        self._broker._on_commit(self)

    async def rollback(self, reason: str = "") -> None:
        if self._rolled_back or self.state in {LeaseState.ROLLED_BACK, LeaseState.RELEASED}:
            return  # idempotent
        if self.state == LeaseState.ACTIVE:
            # Active lease rollback = release
            await self.release()
            return
        self.state = LeaseState.ROLLED_BACK
        self._rolled_back = True
        self._broker._on_rollback(self)
        logger.info("Grant %s rolled back: %s", self.lease_id[:8], reason)

    async def release(self) -> None:
        if self.state == LeaseState.RELEASED:
            return  # idempotent
        if self.state.is_terminal and self.state != LeaseState.ACTIVE:
            return
        self.state = LeaseState.RELEASED
        self._broker._on_release(self)
        logger.info("Grant %s released for %s", self.lease_id[:8], self.component_id)

    async def __aenter__(self) -> 'BudgetGrant':
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            await self.rollback(reason=f"{exc_type.__name__}: {exc_val}")
        elif not self._committed and not self._rolled_back and not self.state.is_terminal:
            logger.warning(
                "Grant %s for %s exited without commit or rollback — auto-rolling back",
                self.lease_id[:8], self.component_id,
            )
            await self.rollback(reason="auto_rollback_no_commit")


# ─── MemoryBudgetBroker ─────────────────────────────────────────────

class MemoryBudgetBroker:
    """Single admission authority for all memory-intensive operations."""

    def __init__(self, quantizer: 'MemoryQuantizer', epoch: int):
        self._quantizer = quantizer
        self._epoch = epoch
        self._phase = StartupPhase.BOOT_CRITICAL
        self._leases: Dict[str, BudgetGrant] = {}
        self._lock = asyncio.Lock()
        self._phase_policies = dict(_PHASE_POLICIES)
        # Swap hysteresis state
        self._swap_hysteresis_active = False
        self._swap_hysteresis_tripped_at: Optional[float] = None
        self._swap_recovery_seconds = float(os.getenv("JARVIS_MCP_SWAP_RECOVERY_SECONDS", "60"))
        self._swap_hysteresis_multiplier = 0.7
        # Tier dwell dampener
        self._tier_dwell_seconds = float(os.getenv("JARVIS_MCP_TIER_DWELL_SECONDS", "15"))

    def set_phase(self, phase: StartupPhase) -> None:
        prev = self._phase
        self._phase = phase
        logger.info("Broker phase: %s -> %s", prev.name, phase.name)

    def get_committed_bytes(self) -> int:
        return sum(
            g.actual_bytes or g.granted_bytes
            for g in self._leases.values()
            if g.state in {LeaseState.GRANTED, LeaseState.ACTIVE}
        )

    def _validate_epoch(self, epoch: int) -> None:
        if epoch != self._epoch:
            raise StaleEpochError(
                f"stale epoch: grant has {epoch}, broker has {self._epoch}"
            )

    def _active_grant_count(self) -> int:
        return sum(
            1 for g in self._leases.values()
            if g.state in {LeaseState.GRANTED, LeaseState.ACTIVE}
        )

    async def request(
        self,
        component: str,
        bytes_requested: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        *,
        ttl_seconds: float = 120.0,
        can_degrade: bool = False,
        degradation_options: Optional[List[DegradationOption]] = None,
        deadline: Optional[float] = None,
        trace_id: Optional[str] = None,
    ) -> BudgetGrant:
        if deadline is None:
            deadline = time.monotonic() + ttl_seconds

        async with self._lock:
            return await self._evaluate_request(
                component, bytes_requested, priority, phase,
                ttl_seconds, can_degrade, degradation_options or [],
                deadline, trace_id,
            )

    async def try_request(
        self,
        component: str,
        bytes_requested: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        **kwargs,
    ) -> Optional[BudgetGrant]:
        try:
            kwargs.setdefault("deadline", time.monotonic() + 0.01)
            return await self.request(component, bytes_requested, priority, phase, **kwargs)
        except BudgetDeniedError:
            return None

    async def _evaluate_request(
        self,
        component: str,
        bytes_requested: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        ttl_seconds: float,
        can_degrade: bool,
        degradation_options: List[DegradationOption],
        deadline: float,
        trace_id: Optional[str],
    ) -> BudgetGrant:
        policy = self._phase_policies.get(self._phase)
        if policy is None:
            raise BudgetDeniedError(f"unknown phase {self._phase}")

        # Phase priority check
        if priority not in policy.allowed_priorities:
            raise BudgetDeniedError(
                f"priority {priority.name} not allowed in phase {self._phase.name}"
            )

        # Concurrent grant limit
        if self._active_grant_count() >= policy.max_concurrent:
            if time.monotonic() >= deadline:
                raise BudgetDeniedError(
                    f"concurrent limit ({policy.max_concurrent}) reached, deadline expired"
                )
            raise BudgetDeniedError(
                f"concurrent limit ({policy.max_concurrent}) reached"
            )

        # Swap hysteresis check for BACKGROUND
        if self._swap_hysteresis_active and priority == BudgetPriority.BACKGROUND:
            raise BudgetDeniedError("swap hysteresis active, BACKGROUND grants blocked")

        # Take snapshot
        snapshot = await self._quantizer.snapshot()

        # Signal quality gate
        if snapshot.signal_quality == SignalQuality.FALLBACK:
            if priority in {BudgetPriority.BACKGROUND, BudgetPriority.RUNTIME_INTERACTIVE}:
                raise BudgetDeniedError("signal quality FALLBACK, non-critical grants blocked")

        headroom = snapshot.headroom_bytes

        # Apply phase cap
        phase_cap = int(snapshot.physical_total * policy.budget_cap_pct * snapshot.pressure_factor)
        effective_headroom = min(headroom, max(0, phase_cap - self.get_committed_bytes()))

        # Try full grant
        if bytes_requested <= effective_headroom:
            return self._issue_grant(
                component, bytes_requested, priority, phase, ttl_seconds,
                trace_id=trace_id,
            )

        # Try degradation
        if can_degrade and degradation_options:
            for option in degradation_options:
                if option.bytes_required <= effective_headroom:
                    return self._issue_grant(
                        component, option.bytes_required, priority, phase, ttl_seconds,
                        degraded=True, degradation_applied=option.name,
                        constraints=option.constraints, trace_id=trace_id,
                    )

        # Check deadline
        if time.monotonic() >= deadline:
            raise BudgetDeniedError(
                f"insufficient headroom ({effective_headroom} < {bytes_requested}) "
                f"and deadline expired",
                snapshot_id=snapshot.snapshot_id,
            )

        raise BudgetDeniedError(
            f"insufficient headroom: {effective_headroom} available, {bytes_requested} requested",
            snapshot_id=snapshot.snapshot_id,
        )

    def _issue_grant(
        self,
        component: str,
        bytes_granted: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        ttl_seconds: float,
        degraded: bool = False,
        degradation_applied: Optional[str] = None,
        constraints: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> BudgetGrant:
        lease_id = str(uuid.uuid4())
        grant = BudgetGrant(
            broker=self,
            lease_id=lease_id,
            component_id=component,
            granted_bytes=bytes_granted,
            priority=priority,
            phase=phase,
            epoch=self._epoch,
            ttl_seconds=ttl_seconds,
            degraded=degraded,
            degradation_applied=degradation_applied,
            constraints=constraints,
            trace_id=trace_id,
        )
        self._leases[lease_id] = grant
        logger.info(
            "Grant issued: %s -> %s (%d MB, degraded=%s)",
            lease_id[:8], component, bytes_granted // (1024 * 1024), degraded,
        )
        return grant

    def _on_commit(self, grant: BudgetGrant) -> None:
        """Called by BudgetGrant.commit()."""
        if grant.actual_bytes and grant.actual_bytes > grant.granted_bytes:
            logger.warning(
                "OVERRUN: %s committed %d MB but was granted %d MB",
                grant.component_id,
                grant.actual_bytes // (1024 * 1024),
                grant.granted_bytes // (1024 * 1024),
            )

    def _on_rollback(self, grant: BudgetGrant) -> None:
        """Called by BudgetGrant.rollback()."""
        pass  # lease stays in dict for telemetry; state is terminal

    def _on_release(self, grant: BudgetGrant) -> None:
        """Called by BudgetGrant.release()."""
        pass  # lease stays in dict for telemetry; state is terminal

    def get_active_leases(self) -> List[BudgetGrant]:
        return [
            g for g in self._leases.values()
            if g.state in {LeaseState.GRANTED, LeaseState.ACTIVE}
        ]

    def get_status(self) -> Dict[str, Any]:
        return {
            "broker_epoch": self._epoch,
            "phase": self._phase.name,
            "active_leases": [
                {
                    "lease_id": g.lease_id[:8],
                    "component_id": g.component_id,
                    "granted_bytes": g.granted_bytes,
                    "actual_bytes": g.actual_bytes,
                    "state": g.state.value,
                }
                for g in self.get_active_leases()
            ],
            "total_committed_bytes": self.get_committed_bytes(),
            "swap_hysteresis_active": self._swap_hysteresis_active,
        }


# ─── Singleton ───────────────────────────────────────────────────────

_broker_instance: Optional[MemoryBudgetBroker] = None


def get_memory_budget_broker() -> Optional[MemoryBudgetBroker]:
    """Return broker instance or None if not initialized."""
    return _broker_instance


async def init_memory_budget_broker(quantizer: 'MemoryQuantizer', epoch: int) -> MemoryBudgetBroker:
    """Initialize the global broker singleton."""
    global _broker_instance
    _broker_instance = MemoryBudgetBroker(quantizer=quantizer, epoch=epoch)
    quantizer.set_broker_ref(_broker_instance)
    return _broker_instance
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_memory_budget_broker.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/memory_budget_broker.py tests/unit/test_memory_budget_broker.py
git commit -m "feat(memory): implement MemoryBudgetBroker core

Transactional grant lifecycle (request -> commit/rollback -> release),
phase policy enforcement, degradation fallback, epoch fencing,
swap hysteresis, concurrent grant limits, and priority filtering."
```

---

## Task 4: Implement Estimate Calibrator

**Files:**
- Create: `backend/core/estimate_calibrator.py`
- Test: `tests/unit/test_estimate_calibrator.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_estimate_calibrator.py
"""Tests for EstimateCalibrator."""
import pytest
import json
from pathlib import Path
from backend.core.estimate_calibrator import EstimateCalibrator


class TestEstimateCalibrator:
    @pytest.fixture
    def calibrator(self, tmp_path):
        return EstimateCalibrator(history_file=tmp_path / "estimates.json")

    def test_default_factor_with_no_history(self, calibrator):
        result = calibrator.get_calibrated_estimate("llm:test@v1", 1_000_000_000)
        assert result == 1_200_000_000  # 1.2x default

    def test_records_and_uses_history(self, calibrator):
        for _ in range(5):
            calibrator.record("llm:test@v1", estimated=1000, actual=1100)
        result = calibrator.get_calibrated_estimate("llm:test@v1", 1000)
        assert result >= 1100  # should be at least p95 of 1.1x

    def test_never_shrinks_below_raw(self, calibrator):
        for _ in range(5):
            calibrator.record("llm:test@v1", estimated=1000, actual=800)
        result = calibrator.get_calibrated_estimate("llm:test@v1", 1000)
        assert result >= 1000

    def test_persists_to_file(self, calibrator, tmp_path):
        calibrator.record("test:v1", estimated=100, actual=120)
        data = json.loads((tmp_path / "estimates.json").read_text())
        assert "test:v1" in data

    def test_max_history_per_component(self, calibrator):
        for i in range(60):
            calibrator.record("test:v1", estimated=100, actual=100 + i)
        assert len(calibrator._history["test:v1"]) == 50
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_estimate_calibrator.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement**

```python
# backend/core/estimate_calibrator.py
"""Estimate Calibrator — tracks estimate vs actual to auto-adjust future grants."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_DEFAULT_HISTORY_FILE = Path("~/.jarvis/memory/estimate_history.json").expanduser()


class EstimateCalibrator:
    MAX_HISTORY_PER_COMPONENT = 50
    DEFAULT_OVERRUN_FACTOR = 1.2

    def __init__(self, history_file: Path = _DEFAULT_HISTORY_FILE):
        self._history_file = Path(history_file)
        self._history: Dict[str, List[dict]] = {}
        self._load()

    def _load(self) -> None:
        if self._history_file.exists():
            try:
                self._history = json.loads(self._history_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt estimate history, starting fresh")
                self._history = {}

    def _persist(self) -> None:
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._history_file.with_suffix(".tmp")
        content = json.dumps(self._history, indent=2).encode()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(str(tmp), str(self._history_file))

    def record(self, component_id: str, estimated: int, actual: int) -> None:
        ratio = actual / max(estimated, 1)
        entry = {
            "estimated": estimated,
            "actual": actual,
            "ratio": ratio,
            "timestamp": time.time(),
        }
        self._history.setdefault(component_id, []).append(entry)
        self._history[component_id] = self._history[component_id][-self.MAX_HISTORY_PER_COMPONENT:]
        self._persist()

    def get_calibrated_estimate(self, component_id: str, raw_estimate: int) -> int:
        history = self._history.get(component_id, [])
        if len(history) < 3:
            return int(raw_estimate * self.DEFAULT_OVERRUN_FACTOR)

        ratios = sorted(h["ratio"] for h in history)
        p95_idx = min(int(len(ratios) * 0.95), len(ratios) - 1)
        p95_factor = max(ratios[p95_idx], 1.0)
        return int(raw_estimate * p95_factor)

    def get_stats(self) -> Dict[str, dict]:
        stats = {}
        for comp, history in self._history.items():
            if not history:
                continue
            ratios = sorted(h["ratio"] for h in history)
            p95_idx = min(int(len(ratios) * 0.95), len(ratios) - 1)
            stats[comp] = {
                "p95_factor": round(max(ratios[p95_idx], 1.0), 3),
                "samples": len(history),
            }
        return stats
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/test_estimate_calibrator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/estimate_calibrator.py tests/unit/test_estimate_calibrator.py
git commit -m "feat(memory): implement EstimateCalibrator

Tracks estimate vs actual bytes per component, persists history,
computes p95 overrun factor for self-improving grant accuracy."
```

---

## Task 5: Implement BudgetedLoader Protocol and Adapters

**Files:**
- Create: `backend/core/budgeted_loaders.py`
- Test: `tests/unit/test_budgeted_loaders.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_budgeted_loaders.py
"""Tests for BudgetedLoader protocol and concrete loader adapters."""
import pytest
from backend.core.budgeted_loaders import (
    BudgetedLoader, LLMBudgetedLoader, WhisperBudgetedLoader,
    EcapaBudgetedLoader, EmbeddingBudgetedLoader,
)
from backend.core.memory_types import BudgetPriority, StartupPhase


class TestLLMLoader:
    def test_component_id_includes_model(self):
        loader = LLMBudgetedLoader(model_name="mistral-7b-q4", size_mb=4370)
        assert loader.component_id == "llm:mistral-7b-q4@v1"

    def test_phase_default_interactive(self):
        loader = LLMBudgetedLoader(model_name="test", size_mb=1000)
        assert loader.phase == StartupPhase.BOOT_OPTIONAL

    def test_estimate_includes_kv_cache(self):
        loader = LLMBudgetedLoader(model_name="test", size_mb=4370, context_length=2048)
        estimate = loader.estimate_bytes({})
        # Model + KV cache + overhead, should be > model size alone
        assert estimate > 4370 * 1024 * 1024

    def test_degradation_options_present(self):
        loader = LLMBudgetedLoader(model_name="test", size_mb=4370, context_length=4096)
        opts = loader.degradation_options
        assert len(opts) >= 2
        assert any("context" in o.name for o in opts)


class TestWhisperLoader:
    def test_component_id(self):
        loader = WhisperBudgetedLoader(model_size="base")
        assert loader.component_id == "whisper:base@v1"

    def test_estimate_known_sizes(self):
        for size, min_mb in [("tiny", 75), ("base", 150), ("small", 500)]:
            loader = WhisperBudgetedLoader(model_size=size)
            estimate = loader.estimate_bytes({})
            assert estimate >= min_mb * 1024 * 1024

    def test_degradation_to_tiny(self):
        loader = WhisperBudgetedLoader(model_size="base")
        opts = loader.degradation_options
        assert len(opts) >= 1
        assert opts[0].name == "whisper_tiny"


class TestEcapaLoader:
    def test_component_id(self):
        loader = EcapaBudgetedLoader()
        assert loader.component_id == "ecapa_tdnn@v1"

    def test_estimate(self):
        loader = EcapaBudgetedLoader()
        assert loader.estimate_bytes({}) >= 300 * 1024 * 1024


class TestEmbeddingLoader:
    def test_component_id(self):
        loader = EmbeddingBudgetedLoader()
        assert loader.component_id == "embedding:all-MiniLM-L6-v2@v1"

    def test_estimate(self):
        loader = EmbeddingBudgetedLoader()
        assert loader.estimate_bytes({}) >= 300 * 1024 * 1024
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_budgeted_loaders.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement**

```python
# backend/core/budgeted_loaders.py
"""BudgetedLoader protocol and concrete loader adapters.

Every model loader must implement BudgetedLoader to participate in
broker-governed memory allocation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from backend.core.memory_types import (
    BudgetPriority, StartupPhase, DegradationOption,
    ConfigProof, LoadResult,
)
from backend.core.memory_budget_broker import BudgetGrant


# ─── Protocol ────────────────────────────────────────────────────────

@runtime_checkable
class BudgetedLoader(Protocol):
    @property
    def component_id(self) -> str: ...

    @property
    def phase(self) -> StartupPhase: ...

    @property
    def priority(self) -> BudgetPriority: ...

    def estimate_bytes(self, config: Dict[str, Any]) -> int: ...

    async def load_with_grant(self, grant: BudgetGrant) -> LoadResult: ...

    def prove_config(self, constraints: Dict[str, Any]) -> ConfigProof: ...

    def measure_actual_bytes(self) -> int: ...

    async def release_handle(self, reason: str) -> None: ...


# ─── LLM Loader ──────────────────────────────────────────────────────

_BOOT_PROFILE = os.getenv("JARVIS_BOOT_PROFILE", "interactive").lower()


class LLMBudgetedLoader:
    def __init__(
        self,
        model_name: str = "unknown",
        size_mb: int = 0,
        context_length: int = 2048,
    ):
        self._model_name = model_name
        self._size_mb = size_mb
        self._context_length = context_length
        self._model_handle = None
        self._actual_bytes = 0

    @property
    def component_id(self) -> str:
        return f"llm:{self._model_name}@v1"

    @property
    def phase(self) -> StartupPhase:
        if _BOOT_PROFILE == "headless":
            return StartupPhase.BACKGROUND
        return StartupPhase.BOOT_OPTIONAL

    @property
    def priority(self) -> BudgetPriority:
        if _BOOT_PROFILE == "headless":
            return BudgetPriority.BACKGROUND
        return BudgetPriority.BOOT_OPTIONAL

    def estimate_bytes(self, config: Dict[str, Any]) -> int:
        size_mb = config.get("size_mb", self._size_mb)
        ctx = config.get("context_length", self._context_length)
        size_scale = min(2.0, size_mb / 4000)
        kv_cache_mb = (ctx / 1024) * 64 * size_scale
        overhead_mb = 512
        return int((size_mb + kv_cache_mb + overhead_mb) * 1024 * 1024)

    @property
    def degradation_options(self) -> List[DegradationOption]:
        opts = []
        if self._context_length > 2048:
            opts.append(DegradationOption(
                name="reduce_context_2048",
                bytes_required=self.estimate_bytes({"context_length": 2048, "size_mb": self._size_mb}),
                quality_impact="Context window reduced to 2048",
                constraints={"max_context": 2048},
            ))
        if self._context_length > 1024:
            opts.append(DegradationOption(
                name="reduce_context_1024",
                bytes_required=self.estimate_bytes({"context_length": 1024, "size_mb": self._size_mb}),
                quality_impact="Context window reduced to 1024",
                constraints={"max_context": 1024},
            ))
        opts.append(DegradationOption(
            name="cpu_only",
            bytes_required=self.estimate_bytes({"context_length": min(self._context_length, 2048), "size_mb": self._size_mb}),
            quality_impact="No Metal GPU offload",
            constraints={"n_gpu_layers": 0, "max_context": min(self._context_length, 2048)},
        ))
        return opts

    async def load_with_grant(self, grant: BudgetGrant) -> LoadResult:
        raise NotImplementedError("Wired in Task 7")

    def prove_config(self, constraints: Dict[str, Any]) -> ConfigProof:
        return ConfigProof(
            component_id=self.component_id,
            requested_constraints=constraints,
            applied_config={},
            compliant=True,
            evidence={},
        )

    def measure_actual_bytes(self) -> int:
        return self._actual_bytes

    async def release_handle(self, reason: str) -> None:
        self._model_handle = None


# ─── Whisper Loader ──────────────────────────────────────────────────

class WhisperBudgetedLoader:
    MODEL_SIZES_MB = {"tiny": 75, "base": 150, "small": 500, "medium": 1500, "large": 3000}
    OVERHEAD_MB = 200

    def __init__(self, model_size: str = "base"):
        self._model_size = model_size
        self._model_handle = None
        self._actual_bytes = 0

    @property
    def component_id(self) -> str:
        return f"whisper:{self._model_size}@v1"

    @property
    def phase(self) -> StartupPhase:
        return StartupPhase.BOOT_OPTIONAL

    @property
    def priority(self) -> BudgetPriority:
        return BudgetPriority.BOOT_OPTIONAL

    def estimate_bytes(self, config: Dict[str, Any]) -> int:
        size = config.get("model_size", self._model_size)
        size_mb = self.MODEL_SIZES_MB.get(size, 150)
        return int((size_mb + self.OVERHEAD_MB) * 1024 * 1024)

    @property
    def degradation_options(self) -> List[DegradationOption]:
        if self._model_size != "tiny":
            return [DegradationOption(
                name="whisper_tiny",
                bytes_required=int((75 + self.OVERHEAD_MB) * 1024 * 1024),
                quality_impact="Whisper tiny: lower accuracy, much less memory",
                constraints={"model_size": "tiny"},
            )]
        return []

    async def load_with_grant(self, grant: BudgetGrant) -> LoadResult:
        raise NotImplementedError("Wired in Task 8")

    def prove_config(self, constraints: Dict[str, Any]) -> ConfigProof:
        return ConfigProof(self.component_id, constraints, {}, True, {})

    def measure_actual_bytes(self) -> int:
        return self._actual_bytes

    async def release_handle(self, reason: str) -> None:
        self._model_handle = None


# ─── ECAPA-TDNN Loader ───────────────────────────────────────────────

class EcapaBudgetedLoader:
    def __init__(self):
        self._model_handle = None
        self._actual_bytes = 0

    @property
    def component_id(self) -> str:
        return "ecapa_tdnn@v1"

    @property
    def phase(self) -> StartupPhase:
        return StartupPhase.BOOT_OPTIONAL

    @property
    def priority(self) -> BudgetPriority:
        return BudgetPriority.BOOT_OPTIONAL

    def estimate_bytes(self, config: Dict[str, Any]) -> int:
        return int(350 * 1024 * 1024)

    @property
    def degradation_options(self) -> List[DegradationOption]:
        return []

    async def load_with_grant(self, grant: BudgetGrant) -> LoadResult:
        raise NotImplementedError("Wired in Task 8")

    def prove_config(self, constraints: Dict[str, Any]) -> ConfigProof:
        return ConfigProof(self.component_id, constraints, {}, True, {})

    def measure_actual_bytes(self) -> int:
        return self._actual_bytes

    async def release_handle(self, reason: str) -> None:
        self._model_handle = None


# ─── Embedding Loader ────────────────────────────────────────────────

class EmbeddingBudgetedLoader:
    def __init__(self):
        self._actual_bytes = 0

    @property
    def component_id(self) -> str:
        return "embedding:all-MiniLM-L6-v2@v1"

    @property
    def phase(self) -> StartupPhase:
        return StartupPhase.BOOT_OPTIONAL

    @property
    def priority(self) -> BudgetPriority:
        return BudgetPriority.BOOT_OPTIONAL

    def estimate_bytes(self, config: Dict[str, Any]) -> int:
        return int(400 * 1024 * 1024)

    @property
    def degradation_options(self) -> List[DegradationOption]:
        return []

    async def load_with_grant(self, grant: BudgetGrant) -> LoadResult:
        raise NotImplementedError("Wired in Task 9")

    def prove_config(self, constraints: Dict[str, Any]) -> ConfigProof:
        return ConfigProof(self.component_id, constraints, {}, True, {})

    def measure_actual_bytes(self) -> int:
        return self._actual_bytes

    async def release_handle(self, reason: str) -> None:
        pass
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/test_budgeted_loaders.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/budgeted_loaders.py tests/unit/test_budgeted_loaders.py
git commit -m "feat(memory): implement BudgetedLoader protocol and adapters

LLM, Whisper, ECAPA, and Embedding loader adapters with estimate_bytes,
degradation_options, and prove_config. load_with_grant() stubs for
wiring in subsequent tasks."
```

---

## Task 6: Implement Lease Persistence and Crash Reclaim

**Files:**
- Modify: `backend/core/memory_budget_broker.py`
- Test: `tests/unit/test_lease_persistence.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_lease_persistence.py
"""Tests for lease persistence and crash reclaim."""
import pytest
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState, ConfigProof,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker


def _make_snapshot(**overrides):
    defaults = dict(
        physical_total=17_179_869_184, physical_wired=3_000_000_000,
        physical_active=5_000_000_000, physical_inactive=2_000_000_000,
        physical_compressed=1_000_000_000, physical_free=6_000_000_000,
        swap_total=8_000_000_000, swap_used=500_000_000,
        swap_growth_rate_bps=0.0, usable_bytes=13_000_000_000,
        committed_bytes=0, available_budget_bytes=13_000_000_000,
        kernel_pressure=KernelPressure.NORMAL, pressure_tier=PressureTier.ABUNDANT,
        thrash_state=ThrashState.HEALTHY, pageins_per_sec=0.0,
        host_rss_slope_bps=0.0, jarvis_tree_rss_slope_bps=0.0,
        swap_slope_bps=0.0, pressure_trend=PressureTrend.STABLE,
        safety_floor_bytes=1_600_000_000, compressed_trend_bytes=500_000_000,
        signal_quality=SignalQuality.GOOD, timestamp=1000.0, max_age_ms=0,
        epoch=1, snapshot_id="test-001",
    )
    defaults.update(overrides)
    return MemorySnapshot(**defaults)


class TestLeasePersistence:
    @pytest.fixture
    def broker(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        b = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        b.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return b

    @pytest.mark.asyncio
    async def test_lease_file_created_on_grant(self, broker, tmp_path):
        await broker.request("test:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        lease_file = tmp_path / "leases.json"
        assert lease_file.exists()
        data = json.loads(lease_file.read_text())
        assert data["broker_epoch"] == 1
        assert len(data["leases"]) == 1

    @pytest.mark.asyncio
    async def test_lease_file_updated_on_commit(self, broker, tmp_path):
        grant = await broker.request("test:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        await grant.commit(95_000_000, ConfigProof("test:v1", {}, {}, True, {}))
        data = json.loads((tmp_path / "leases.json").read_text())
        lease = data["leases"][0]
        assert lease["state"] == "active"
        assert lease["actual_bytes"] == 95_000_000


class TestCrashReclaim:
    @pytest.mark.asyncio
    async def test_stale_epoch_reclaimed(self, tmp_path):
        # Write a lease file from epoch 5
        lease_file = tmp_path / "leases.json"
        lease_file.write_text(json.dumps({
            "schema_version": "1.0",
            "broker_epoch": 5,
            "leases": [{
                "lease_id": "old-lease",
                "component_id": "llm:test@v1",
                "granted_bytes": 5_000_000_000,
                "actual_bytes": 4_800_000_000,
                "state": "active",
                "priority": "BOOT_OPTIONAL",
                "phase": "BOOT_OPTIONAL",
                "pid": 99999999,  # definitely not alive
                "epoch": 5,
            }],
        }))

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=6, lease_file=lease_file)
        report = await broker.reconcile_stale_leases()
        assert report["stale"] == 1
        assert report["reclaimed_bytes"] == 4_800_000_000
        assert broker.get_committed_bytes() == 0

    @pytest.mark.asyncio
    async def test_corrupted_file_handled(self, tmp_path):
        lease_file = tmp_path / "leases.json"
        lease_file.write_text("NOT VALID JSON{{{")

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=lease_file)
        report = await broker.reconcile_stale_leases()
        assert report.get("corrupted") is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_lease_persistence.py -v`
Expected: FAIL — `TypeError: __init__() got unexpected keyword argument 'lease_file'`

**Step 3: Add lease persistence to MemoryBudgetBroker**

Add `lease_file` param to `__init__`, `_persist_leases()` method, `reconcile_stale_leases()` method. Wire `_persist_leases()` calls into `_issue_grant()`, `_on_commit()`, `_on_rollback()`, `_on_release()`. See design doc Section 4.1-4.2 for full spec.

Modify `backend/core/memory_budget_broker.py`:
- Add `lease_file: Optional[Path] = None` to `__init__`
- Default to `Path("~/.jarvis/memory/leases.json").expanduser()`
- Add `_persist_leases()` with atomic write-temp+fsync+rename
- Add `reconcile_stale_leases()` with epoch fence + PID liveness + TTL check
- Call `_persist_leases()` after grant issue, commit, rollback, release

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/test_lease_persistence.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/memory_budget_broker.py tests/unit/test_lease_persistence.py
git commit -m "feat(memory): add lease persistence and crash reclaim

Atomic lease file writes (tmp+fsync+rename), epoch-fenced crash
reconciliation, PID liveness checks, corrupted file recovery."
```

---

## Task 7: Wire LLM Loader to Broker

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py:642-880` (load_model)
- Modify: `backend/core/budgeted_loaders.py` (LLMBudgetedLoader.load_with_grant)
- Test: `tests/unit/test_llm_broker_integration.py`

**Step 1: Write the failing integration test**

Test that `PrimeLocalClient.load_model()` now acquires a broker grant before calling `Llama()`.

**Step 2: Run to verify failure**

**Step 3: Implement**

In `unified_model_serving.py:load_model()` (~line 642):
- Replace the direct `MemoryQuantizer.reserve_memory()` call (line 831-846) with broker grant
- Replace the headroom/tier check block (lines 747-826) with broker `request()` call
- Pass constraints from grant into `Llama()` constructor (n_ctx, n_gpu_layers)
- Call `grant.commit()` after successful load, `grant.rollback()` on failure
- Remove old reservation cleanup code

In `LLMBudgetedLoader.load_with_grant()`:
- Defer `from llama_cpp import Llama` to inside method body
- Apply grant constraints (max_context, n_gpu_layers)
- Call `grant.heartbeat()` before `Llama()` constructor (it can take 30+ seconds)
- Return `LoadResult` with actual bytes and config proof

**Step 4: Run tests**

**Step 5: Commit**

```bash
git commit -m "feat(memory): wire LLM loader to MemoryBudgetBroker

Replace direct MemoryQuantizer.reserve_memory() with transactional broker
grants. Constraints (context, gpu layers) enforced via grant."
```

---

## Task 8: Wire Voice Loaders to Broker

**Files:**
- Modify: `backend/voice/parallel_model_loader.py:552-586` (load_all_voice_models)
- Modify: `backend/core/budgeted_loaders.py` (Whisper/ECAPA load_with_grant)
- Test: `tests/unit/test_voice_broker_integration.py`

**Step 1-5: Same TDD cycle**

Key changes to `parallel_model_loader.py`:
- `load_all_voice_models()` now acquires broker grants before dispatching to thread pool
- Whisper grant first, then ECAPA grant (sequential within voice, parallel with other phases)
- Grants carry constraints through to actual model loading
- Remove fire-and-forget pattern; wrap in `async with grant:`

```bash
git commit -m "feat(memory): wire Whisper and ECAPA loaders to broker

Voice model loading now requires broker grants. Sequential within voice
phase, parallel with embedding phase."
```

---

## Task 9: Wire Embedding Loader and Eliminate Bypasses

**Files:**
- Modify: `backend/core/embedding_service.py:231-288` (_load_model)
- Modify: 14 bypass files (see design doc Section 3 bypass table)
- Test: `tests/unit/test_embedding_broker_integration.py`

**Step 1-5: Same TDD cycle**

Key changes:
- `EmbeddingService._load_model()` acquires broker grant before `SentenceTransformer()` constructor
- `EmbeddingService._check_memory_budget()` replaced with broker call (retire PRG path)
- `encode_sync()` inline `SentenceTransformer` creation removed; uses `_model` with thread lock
- All 14 bypass files: replace `SentenceTransformer(...)` with `EmbeddingService.get_instance()`
- Add `EmbeddingServiceAdapter` with `encode()`, `encode_batch()`, `get_embedding_dim()`

**Important:** Do each bypass file as a sub-step. Verify imports resolve after each change.

```bash
git commit -m "feat(memory): eliminate SentenceTransformer bypasses

All 14 direct instantiation sites now use EmbeddingService singleton.
EmbeddingService wired to broker. encode_sync() inline creation removed."
```

---

## Task 10: Wire Startup Phase Transitions

**Files:**
- Modify: `backend/main.py:879-929` (parallel_import_components), `9365-9368` (fresh MQ)
- Modify: `backend/main.py:6533-6620` (/api/system/status)
- Test: `tests/unit/test_startup_phases.py`

**Step 1-5: Same TDD cycle**

Key changes to `backend/main.py`:
- Replace fresh `MemoryQuantizer()` at line 9365 with `get_memory_quantizer()` singleton
- Init broker early in startup: `init_memory_budget_broker(quantizer, epoch)`
- Add phase transitions at lifecycle boundaries:
  - After broker init: `broker.set_phase(StartupPhase.BOOT_OPTIONAL)`
  - After all models loaded: `broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)`
- Add `memory_control_plane` to `/api/system/status` response (broker.get_status())
- Wire supervisor epoch from `unified_supervisor.py` startup

```bash
git commit -m "feat(memory): wire startup phase transitions to broker

Broker initialized early, phase transitions at lifecycle boundaries,
fresh MemoryQuantizer() replaced with singleton, MCP status in API."
```

---

## Task 11: Retire Legacy Budget Systems

**Files:**
- Modify: `backend/core/proactive_resource_guard.py:297-329` (request_memory_budget)
- Modify: `backend/ml_memory_manager.py:392-403` (_can_load_model)
- Modify: `backend/core/memory_quantizer.py:1413-1451` (reserve_memory/release_reservation)
- Test: `tests/unit/test_legacy_retirement.py`

**Step 1-5: Same TDD cycle**

Key changes:
- `ProactiveResourceGuard.request_memory_budget()`: deprecation warning + delegate to broker
- `IntelligentMLMemoryManager._can_load_model()`: delegate to broker.try_request()
- `IntelligentMLMemoryManager._monitor_loop()`: replaced by broker tier callbacks
- `MemoryQuantizer.reserve_memory()`/`release_reservation()`: deprecation warning + no-op (broker owns leases now)
- Keep classes alive (callers may still reference them) but gut the budget logic

```bash
git commit -m "refactor(memory): retire PRG and MLMemoryManager budget paths

ProactiveResourceGuard.request_memory_budget() and MLMemoryManager budget
logic deprecated. Broker is now sole admission authority."
```

---

## Task 12: Add CI Governance Checks

**Files:**
- Create: `.github/workflows/memory-governance.yml`
- Create: `scripts/check_memory_governance.py` (AST-based checker)
- Test: `tests/unit/test_governance_checker.py`

**Step 1-5: Same TDD cycle**

```python
# scripts/check_memory_governance.py
"""AST-based checker for banned direct constructors and psutil calls."""
import ast
import sys
from pathlib import Path

BANNED = {
    "SentenceTransformer": {"allowed": {"backend/core/embedding_service.py"}},
    "psutil.virtual_memory": {"allowed": {"backend/core/memory_quantizer.py"}},
    "psutil.swap_memory": {"allowed": {"backend/core/memory_quantizer.py"}},
}
# ... AST walker implementation
```

```bash
git commit -m "ci(memory): add AST-based governance checks

Ban direct SentenceTransformer() and psutil.virtual_memory() outside
approved modules. Grep fast gate + AST checker for bypass-proof enforcement."
```

---

## Task 13: Stress and Acceptance Tests

**Files:**
- Create: `tests/stress/test_memory_stress.py`
- Create: `tests/stress/test_loader_contracts.py`

**Step 1: Write per-loader contract test suite**

See design doc Section 4.7 for the full required test list:
- `test_no_allocation_without_grant`
- `test_estimate_conservative`
- `test_constraint_compliance`
- `test_overrun_reported`
- `test_preemption_cooperative`
- `test_rollback_idempotent`
- `test_stale_epoch_rejected`
- `test_heartbeat_extends_ttl`
- `test_release_after_commit`

**Step 2: Write broker stress tests**

- `test_concurrent_grant_respects_phase_cap`
- `test_priority_ordering`
- `test_degradation_fallback`
- `test_swap_hysteresis_tightens_caps`
- `test_crash_reclaim`
- `test_epoch_fence`
- `test_quarantine_after_release_failures`
- `test_estimate_calibration_improves`

**Step 3: Run all tests**

Run: `python3 -m pytest tests/ -v --timeout=60`
Expected: All PASS

**Step 4: Commit**

```bash
git commit -m "test(memory): add stress tests and loader contract suite

Per-loader contract tests, broker stress tests, concurrent grant
verification, crash reclaim, and estimate calibration tests."
```

---

## Summary

| Task | Component | Key Outcome |
|---|---|---|
| 1 | memory_types.py | Canonical enums, MemorySnapshot, contract types |
| 2 | memory_quantizer.py | snapshot() method returning MemorySnapshot |
| 3 | memory_budget_broker.py | Core grant/commit/rollback/release lifecycle |
| 4 | estimate_calibrator.py | Self-improving estimate accuracy |
| 5 | budgeted_loaders.py | Protocol + LLM/Whisper/ECAPA/Embedding adapters |
| 6 | Lease persistence | Atomic file, crash reclaim, epoch fence |
| 7 | LLM wiring | PrimeLocalClient uses broker grants |
| 8 | Voice wiring | Whisper + ECAPA use broker grants |
| 9 | Embedding wiring | SentenceTransformer bypasses eliminated |
| 10 | Startup phases | Phase transitions, singleton fix, API status |
| 11 | Legacy retirement | PRG + MLMemoryManager gutted |
| 12 | CI governance | AST-based ban enforcement |
| 13 | Stress tests | Contract suite + acceptance gates |
