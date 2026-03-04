"""Tests for backend.core.memory_types — canonical Memory Control Plane types.

TDD: these tests are written first, before the implementation exists.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# Helper: build a MemorySnapshot with sensible defaults for a 16 GB Mac
# ---------------------------------------------------------------------------

_16_GB = 16 * 1024 ** 3  # 17_179_869_184


def _make_snapshot(**overrides: Any) -> "MemorySnapshot":
    """Return a MemorySnapshot with 16 GB Mac defaults, overridden by kwargs."""
    from backend.core.memory_types import (
        KernelPressure,
        MemorySnapshot,
        PressureTier,
        PressureTrend,
        SignalQuality,
        ThrashState,
    )

    defaults: Dict[str, Any] = dict(
        # Physical truth (bytes)
        physical_total=_16_GB,
        physical_wired=int(2 * 1024 ** 3),      # 2 GB wired
        physical_active=int(6 * 1024 ** 3),      # 6 GB active
        physical_inactive=int(3 * 1024 ** 3),    # 3 GB inactive
        physical_compressed=int(1 * 1024 ** 3),  # 1 GB compressed
        physical_free=int(4 * 1024 ** 3),        # 4 GB free
        # Swap state
        swap_total=int(2 * 1024 ** 3),           # 2 GB swap total
        swap_used=0,
        swap_growth_rate_bps=0.0,
        # Derived budget fields
        usable_bytes=int(10 * 1024 ** 3),        # 10 GB usable
        committed_bytes=int(7 * 1024 ** 3),      # 7 GB committed
        available_budget_bytes=int(3 * 1024 ** 3),
        # Pressure signals
        kernel_pressure=KernelPressure.NORMAL,
        pressure_tier=PressureTier.OPTIMAL,
        thrash_state=ThrashState.HEALTHY,
        pageins_per_sec=0.0,
        # Trend derivatives (30s window)
        host_rss_slope_bps=0.0,
        jarvis_tree_rss_slope_bps=0.0,
        swap_slope_bps=0.0,
        pressure_trend=PressureTrend.STABLE,
        # Safety
        safety_floor_bytes=int(1 * 1024 ** 3),
        compressed_trend_bytes=0,
        # Signal quality
        signal_quality=SignalQuality.GOOD,
        # Metadata
        timestamp=1_000_000_000.0,
        max_age_ms=500,
        epoch=1,
        snapshot_id="test-snap-0001",
    )
    defaults.update(overrides)
    return MemorySnapshot(**defaults)


# ===================================================================
# PressureTier enum
# ===================================================================

class TestPressureTier:
    """PressureTier ordering and membership."""

    def test_all_tiers_present(self):
        from backend.core.memory_types import PressureTier

        expected = {
            "ABUNDANT", "OPTIMAL", "ELEVATED",
            "CONSTRAINED", "CRITICAL", "EMERGENCY",
        }
        actual = {t.name for t in PressureTier}
        assert actual == expected

    def test_ordering_abundant_lt_emergency(self):
        from backend.core.memory_types import PressureTier

        assert PressureTier.ABUNDANT < PressureTier.EMERGENCY

    def test_full_ordering(self):
        from backend.core.memory_types import PressureTier

        ordered = [
            PressureTier.ABUNDANT,
            PressureTier.OPTIMAL,
            PressureTier.ELEVATED,
            PressureTier.CONSTRAINED,
            PressureTier.CRITICAL,
            PressureTier.EMERGENCY,
        ]
        for i in range(len(ordered) - 1):
            assert ordered[i] < ordered[i + 1], (
                f"{ordered[i].name} should be < {ordered[i + 1].name}"
            )

    def test_int_values(self):
        from backend.core.memory_types import PressureTier

        assert PressureTier.ABUNDANT.value == 0
        assert PressureTier.EMERGENCY.value == 5


# ===================================================================
# BudgetPriority enum
# ===================================================================

class TestBudgetPriority:
    """BudgetPriority ordering and membership."""

    def test_all_priorities_present(self):
        from backend.core.memory_types import BudgetPriority

        expected = {
            "BOOT_CRITICAL", "BOOT_OPTIONAL",
            "RUNTIME_INTERACTIVE", "BACKGROUND",
        }
        actual = {p.name for p in BudgetPriority}
        assert actual == expected

    def test_ordering_boot_critical_lt_background(self):
        from backend.core.memory_types import BudgetPriority

        assert BudgetPriority.BOOT_CRITICAL < BudgetPriority.BACKGROUND

    def test_int_values(self):
        from backend.core.memory_types import BudgetPriority

        assert BudgetPriority.BOOT_CRITICAL.value == 0
        assert BudgetPriority.BACKGROUND.value == 3


# ===================================================================
# StartupPhase enum
# ===================================================================

class TestStartupPhase:
    """StartupPhase membership."""

    def test_all_phases_present(self):
        from backend.core.memory_types import StartupPhase

        expected = {
            "BOOT_CRITICAL", "BOOT_OPTIONAL",
            "RUNTIME_INTERACTIVE", "BACKGROUND",
        }
        actual = {p.name for p in StartupPhase}
        assert actual == expected

    def test_int_values(self):
        from backend.core.memory_types import StartupPhase

        assert StartupPhase.BOOT_CRITICAL.value == 0
        assert StartupPhase.BACKGROUND.value == 3


# ===================================================================
# LeaseState enum
# ===================================================================

class TestLeaseState:
    """LeaseState terminal / non-terminal semantics."""

    def test_terminal_states(self):
        from backend.core.memory_types import LeaseState

        terminal = {
            LeaseState.RELEASED,
            LeaseState.ROLLED_BACK,
            LeaseState.EXPIRED,
            LeaseState.PREEMPTED,
            LeaseState.DENIED,
        }
        for state in terminal:
            assert state.is_terminal, f"{state.name} should be terminal"

    def test_non_terminal_states(self):
        from backend.core.memory_types import LeaseState

        non_terminal = {
            LeaseState.PENDING,
            LeaseState.GRANTED,
            LeaseState.ACTIVE,
        }
        for state in non_terminal:
            assert not state.is_terminal, f"{state.name} should NOT be terminal"

    def test_all_states_present(self):
        from backend.core.memory_types import LeaseState

        expected = {
            "PENDING", "GRANTED", "ACTIVE",
            "RELEASED", "ROLLED_BACK", "EXPIRED",
            "PREEMPTED", "DENIED",
        }
        actual = {s.name for s in LeaseState}
        assert actual == expected


# ===================================================================
# KernelPressure, ThrashState, SignalQuality, PressureTrend
# ===================================================================

class TestAuxiliaryEnums:
    """Quick membership checks on smaller enums."""

    def test_kernel_pressure(self):
        from backend.core.memory_types import KernelPressure

        expected = {"NORMAL", "WARN", "CRITICAL"}
        assert {k.name for k in KernelPressure} == expected

    def test_thrash_state(self):
        from backend.core.memory_types import ThrashState

        expected = {"HEALTHY", "THRASHING", "EMERGENCY"}
        assert {t.name for t in ThrashState} == expected

    def test_signal_quality(self):
        from backend.core.memory_types import SignalQuality

        expected = {"GOOD", "DEGRADED", "FALLBACK"}
        assert {s.name for s in SignalQuality} == expected

    def test_pressure_trend(self):
        from backend.core.memory_types import PressureTrend

        expected = {"STABLE", "RISING", "FALLING"}
        assert {p.name for p in PressureTrend} == expected


# ===================================================================
# MemoryBudgetEventType enum
# ===================================================================

class TestMemoryBudgetEventType:
    """All 32 event types must be present (24 core + 8 display lifecycle)."""

    EXPECTED_EVENTS = {
        "grant_requested",
        "grant_issued",
        "grant_denied",
        "grant_degraded",
        "grant_queued",
        "heartbeat",
        "commit",
        "commit_overrun",
        "rollback",
        "release_requested",
        "release_verified",
        "release_failed",
        "preempt_requested",
        "preempt_cooperative",
        "preempt_forced",
        "lease_expired",
        "reconciliation",
        "phase_transition",
        "swap_hysteresis_trip",
        "swap_hysteresis_recover",
        "loader_quarantined",
        "loader_unquarantined",
        "estimate_calibration",
        "snapshot_stale_rejected",
        # Display lifecycle events
        "display_degrade_requested",
        "display_degraded",
        "display_disconnect_requested",
        "display_disconnected",
        "display_recovery_requested",
        "display_recovered",
        "display_action_failed",
        "display_action_phase",
    }

    def test_count_is_32(self):
        from backend.core.memory_types import MemoryBudgetEventType

        assert len(MemoryBudgetEventType) == 32

    def test_all_values_present(self):
        from backend.core.memory_types import MemoryBudgetEventType

        actual = {e.value for e in MemoryBudgetEventType}
        assert actual == self.EXPECTED_EVENTS

    def test_lookup_by_value(self):
        from backend.core.memory_types import MemoryBudgetEventType

        assert MemoryBudgetEventType("grant_requested") is MemoryBudgetEventType.GRANT_REQUESTED
        assert MemoryBudgetEventType("snapshot_stale_rejected") is MemoryBudgetEventType.SNAPSHOT_STALE_REJECTED


# ===================================================================
# MemorySnapshot — frozen dataclass
# ===================================================================

class TestMemorySnapshot:
    """MemorySnapshot immutability and computed properties."""

    def test_frozen_immutable(self):
        snap = _make_snapshot()
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.physical_total = 999  # type: ignore[misc]

    def test_headroom_bytes_positive(self):
        snap = _make_snapshot(
            available_budget_bytes=int(3 * 1024 ** 3),
            safety_floor_bytes=int(1 * 1024 ** 3),
        )
        expected = int(3 * 1024 ** 3) - int(1 * 1024 ** 3)
        assert snap.headroom_bytes == expected

    def test_headroom_bytes_never_negative(self):
        snap = _make_snapshot(
            available_budget_bytes=int(500 * 1024 ** 2),  # 500 MB
            safety_floor_bytes=int(1 * 1024 ** 3),        # 1 GB > budget
        )
        assert snap.headroom_bytes == 0

    def test_headroom_bytes_zero_when_equal(self):
        val = int(1 * 1024 ** 3)
        snap = _make_snapshot(available_budget_bytes=val, safety_floor_bytes=val)
        assert snap.headroom_bytes == 0

    def test_pressure_factor_abundant(self):
        from backend.core.memory_types import PressureTier

        snap = _make_snapshot(pressure_tier=PressureTier.ABUNDANT)
        assert snap.pressure_factor == 1.0

    def test_pressure_factor_optimal(self):
        from backend.core.memory_types import PressureTier

        snap = _make_snapshot(pressure_tier=PressureTier.OPTIMAL)
        assert snap.pressure_factor == 0.95

    def test_pressure_factor_elevated(self):
        from backend.core.memory_types import PressureTier

        snap = _make_snapshot(pressure_tier=PressureTier.ELEVATED)
        assert snap.pressure_factor == 0.85

    def test_pressure_factor_constrained(self):
        from backend.core.memory_types import PressureTier

        snap = _make_snapshot(pressure_tier=PressureTier.CONSTRAINED)
        assert snap.pressure_factor == 0.7

    def test_pressure_factor_critical(self):
        from backend.core.memory_types import PressureTier

        snap = _make_snapshot(pressure_tier=PressureTier.CRITICAL)
        assert snap.pressure_factor == 0.5

    def test_pressure_factor_emergency(self):
        from backend.core.memory_types import PressureTier

        snap = _make_snapshot(pressure_tier=PressureTier.EMERGENCY)
        assert snap.pressure_factor == 0.3

    def test_swap_hysteresis_inactive_when_zero(self):
        snap = _make_snapshot(swap_growth_rate_bps=0)
        assert snap.swap_hysteresis_active is False

    def test_swap_hysteresis_inactive_below_threshold(self):
        snap = _make_snapshot(swap_growth_rate_bps=49 * 1024 * 1024)
        assert snap.swap_hysteresis_active is False

    def test_swap_hysteresis_active_at_threshold(self):
        snap = _make_snapshot(swap_growth_rate_bps=50 * 1024 * 1024)
        assert snap.swap_hysteresis_active is False  # strictly greater than

    def test_swap_hysteresis_active_above_threshold(self):
        snap = _make_snapshot(swap_growth_rate_bps=51 * 1024 * 1024)
        assert snap.swap_hysteresis_active is True

    def test_all_fields_accessible(self):
        from backend.core.memory_types import (
            KernelPressure,
            PressureTier,
            PressureTrend,
            SignalQuality,
            ThrashState,
        )

        snap = _make_snapshot()

        # Physical truth (bytes) -- 6 fields
        assert snap.physical_total == _16_GB
        assert snap.physical_wired == int(2 * 1024 ** 3)
        assert snap.physical_active == int(6 * 1024 ** 3)
        assert snap.physical_inactive == int(3 * 1024 ** 3)
        assert snap.physical_compressed == int(1 * 1024 ** 3)
        assert snap.physical_free == int(4 * 1024 ** 3)

        # Swap state -- 3 fields
        assert snap.swap_total == int(2 * 1024 ** 3)
        assert snap.swap_used == 0
        assert snap.swap_growth_rate_bps == 0.0

        # Derived budget fields -- 3 fields
        assert snap.usable_bytes == int(10 * 1024 ** 3)
        assert snap.committed_bytes == int(7 * 1024 ** 3)
        assert snap.available_budget_bytes == int(3 * 1024 ** 3)

        # Pressure signals -- 4 fields
        assert snap.kernel_pressure is KernelPressure.NORMAL
        assert snap.pressure_tier is PressureTier.OPTIMAL
        assert snap.thrash_state is ThrashState.HEALTHY
        assert snap.pageins_per_sec == 0.0

        # Trend derivatives -- 4 fields
        assert snap.host_rss_slope_bps == 0.0
        assert snap.jarvis_tree_rss_slope_bps == 0.0
        assert snap.swap_slope_bps == 0.0
        assert snap.pressure_trend is PressureTrend.STABLE

        # Safety -- 2 fields
        assert snap.safety_floor_bytes == int(1 * 1024 ** 3)
        assert snap.compressed_trend_bytes == 0

        # Signal quality -- 1 field
        assert snap.signal_quality is SignalQuality.GOOD

        # Metadata -- 4 fields
        assert snap.timestamp == 1_000_000_000.0
        assert snap.max_age_ms == 500
        assert snap.epoch == 1
        assert snap.snapshot_id == "test-snap-0001"

    def test_field_count_is_27(self):
        """MemorySnapshot must have exactly 27 fields (spec listing)."""
        snap = _make_snapshot()
        fields = dataclasses.fields(snap)
        assert len(fields) == 27, (
            f"Expected 27 fields, got {len(fields)}: "
            f"{[f.name for f in fields]}"
        )


# ===================================================================
# DegradationOption
# ===================================================================

class TestDegradationOption:
    """DegradationOption field access."""

    def test_field_access(self):
        from backend.core.memory_types import DegradationOption

        opt = DegradationOption(
            name="quantize_4bit",
            bytes_required=int(2 * 1024 ** 3),
            quality_impact=0.15,
            constraints={"quantization": "4bit", "context_length": 2048},
        )
        assert opt.name == "quantize_4bit"
        assert opt.bytes_required == int(2 * 1024 ** 3)
        assert opt.quality_impact == 0.15
        assert opt.constraints == {"quantization": "4bit", "context_length": 2048}

    def test_empty_constraints(self):
        from backend.core.memory_types import DegradationOption

        opt = DegradationOption(
            name="noop",
            bytes_required=0,
            quality_impact=0.0,
            constraints={},
        )
        assert opt.constraints == {}


# ===================================================================
# ConfigProof
# ===================================================================

class TestConfigProof:
    """ConfigProof compliant flag and fields."""

    def test_compliant(self):
        from backend.core.memory_types import ConfigProof

        proof = ConfigProof(
            component_id="llama_7b",
            requested_constraints={"max_memory": int(4 * 1024 ** 3)},
            applied_config={"n_gpu_layers": 32, "n_ctx": 4096},
            compliant=True,
            evidence="loaded within budget",
        )
        assert proof.compliant is True
        assert proof.component_id == "llama_7b"

    def test_non_compliant(self):
        from backend.core.memory_types import ConfigProof

        proof = ConfigProof(
            component_id="llama_13b",
            requested_constraints={"max_memory": int(4 * 1024 ** 3)},
            applied_config={"n_gpu_layers": 0},
            compliant=False,
            evidence="exceeded budget by 1.2 GB",
        )
        assert proof.compliant is False


# ===================================================================
# LoadResult
# ===================================================================

class TestLoadResult:
    """LoadResult success/failure semantics."""

    def test_success(self):
        from backend.core.memory_types import ConfigProof, LoadResult

        proof = ConfigProof(
            component_id="whisper",
            requested_constraints={},
            applied_config={},
            compliant=True,
            evidence="ok",
        )
        result = LoadResult(
            success=True,
            actual_bytes=int(1.5 * 1024 ** 3),
            config_proof=proof,
            model_handle="whisper_v3",
            load_duration_ms=3200.0,
            error=None,
        )
        assert result.success is True
        assert result.actual_bytes == int(1.5 * 1024 ** 3)
        assert result.config_proof is not None
        assert result.config_proof.compliant is True
        assert result.model_handle == "whisper_v3"
        assert result.load_duration_ms == 3200.0
        assert result.error is None

    def test_failure(self):
        from backend.core.memory_types import LoadResult

        result = LoadResult(
            success=False,
            actual_bytes=0,
            config_proof=None,
            model_handle=None,
            load_duration_ms=150.0,
            error="OOM: insufficient headroom",
        )
        assert result.success is False
        assert result.error == "OOM: insufficient headroom"
        assert result.config_proof is None
        assert result.model_handle is None


# ===================================================================
# Module-level constants
# ===================================================================

class TestModuleConstants:
    """Verify module-level constants are exposed."""

    def test_pressure_factors_dict(self):
        from backend.core.memory_types import PressureTier, _PRESSURE_FACTORS

        assert isinstance(_PRESSURE_FACTORS, dict)
        assert len(_PRESSURE_FACTORS) == len(PressureTier)
        for tier in PressureTier:
            assert tier in _PRESSURE_FACTORS
            assert isinstance(_PRESSURE_FACTORS[tier], float)

    def test_swap_hysteresis_threshold(self):
        from backend.core.memory_types import _SWAP_HYSTERESIS_THRESHOLD_BPS

        assert _SWAP_HYSTERESIS_THRESHOLD_BPS == 50 * 1024 * 1024
