"""Tests for backend.core.memory_budget_broker -- MemoryBudgetBroker core.

Covers the full grant lifecycle (request -> commit/rollback -> release),
phase policy enforcement, degradation fallback, epoch fencing, swap
hysteresis, concurrent grant limits, signal quality gating, and the
context-manager auto-rollback behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.memory_types import (
    BudgetPriority,
    ConfigProof,
    DegradationOption,
    KernelPressure,
    LeaseState,
    MemorySnapshot,
    PressureTier,
    PressureTrend,
    SignalQuality,
    StartupPhase,
    ThrashState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_16_GB = 16 * 1024 ** 3


def _make_snapshot(**overrides: Any) -> MemorySnapshot:
    """Return a MemorySnapshot with 16 GB Mac defaults, overridden by kwargs."""
    defaults: Dict[str, Any] = dict(
        # Physical truth (bytes)
        physical_total=_16_GB,
        physical_wired=int(2 * 1024 ** 3),
        physical_active=int(6 * 1024 ** 3),
        physical_inactive=int(3 * 1024 ** 3),
        physical_compressed=int(1 * 1024 ** 3),
        physical_free=int(4 * 1024 ** 3),
        # Swap state
        swap_total=int(2 * 1024 ** 3),
        swap_used=0,
        swap_growth_rate_bps=0.0,
        # Derived budget fields
        usable_bytes=int(10 * 1024 ** 3),
        committed_bytes=0,
        available_budget_bytes=int(4 * 1024 ** 3),
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


def _make_quantizer(**snap_overrides: Any) -> MagicMock:
    """Return a mock quantizer whose ``snapshot()`` yields a default snapshot."""
    snap = _make_snapshot(**snap_overrides)
    quantizer = MagicMock()
    quantizer.snapshot = AsyncMock(return_value=snap)
    quantizer.set_broker_ref = MagicMock()
    return quantizer


def _make_broker(epoch: int = 1, **snap_overrides: Any):
    """Shortcut: create a broker with a mock quantizer."""
    from backend.core.memory_budget_broker import MemoryBudgetBroker
    quantizer = _make_quantizer(**snap_overrides)
    return MemoryBudgetBroker(quantizer, epoch), quantizer


# ===================================================================
# 1. Grant issued when headroom sufficient
# ===================================================================


class TestGrantIssuedWhenHeadroomSufficient:
    """Broker issues a GRANTED lease when requested bytes fit."""

    @pytest.mark.asyncio
    async def test_grant_issued(self):
        from backend.core.memory_budget_broker import MemoryBudgetBroker
        broker, _ = _make_broker(epoch=1)
        # Default snapshot has 3 GB headroom (4 GB available - 1 GB safety floor)
        # Request 1 GB -- should fit easily
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.state == LeaseState.GRANTED
        assert grant.granted_bytes == int(1 * 1024 ** 3)
        assert grant.component_id == "whisper"

    @pytest.mark.asyncio
    async def test_grant_epoch_matches_broker(self):
        broker, _ = _make_broker(epoch=42)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.epoch == 42


# ===================================================================
# 2. Grant denied when headroom insufficient
# ===================================================================


class TestGrantDeniedInsufficientHeadroom:
    """Broker raises BudgetDeniedError when headroom is too small."""

    @pytest.mark.asyncio
    async def test_denied_when_too_large(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker(
            epoch=1,
            available_budget_bytes=int(1 * 1024 ** 3),
            safety_floor_bytes=int(512 * 1024 ** 2),
        )
        # headroom = 1 GB - 0.5 GB = 0.5 GB, request 2 GB
        with pytest.raises(BudgetDeniedError, match="Insufficient headroom"):
            await broker.request(
                component="llm",
                bytes_requested=int(2 * 1024 ** 3),
                priority=BudgetPriority.BOOT_CRITICAL,
                phase=StartupPhase.BOOT_CRITICAL,
            )


# ===================================================================
# 3. Commit transitions GRANTED -> ACTIVE
# ===================================================================


class TestCommitTransition:
    """commit() moves a lease from GRANTED to ACTIVE."""

    @pytest.mark.asyncio
    async def test_commit_granted_to_active(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.state == LeaseState.GRANTED
        await grant.commit(actual_bytes=int(900 * 1024 ** 2))
        assert grant.state == LeaseState.ACTIVE
        assert grant.actual_bytes == int(900 * 1024 ** 2)

    @pytest.mark.asyncio
    async def test_commit_idempotent(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.commit(actual_bytes=int(900 * 1024 ** 2))
        # Second commit is a no-op
        await grant.commit(actual_bytes=int(800 * 1024 ** 2))
        assert grant.state == LeaseState.ACTIVE
        # actual_bytes unchanged from first commit
        assert grant.actual_bytes == int(900 * 1024 ** 2)


# ===================================================================
# 4. Rollback releases capacity (GRANTED -> ROLLED_BACK)
# ===================================================================


class TestRollbackReleasesCapacity:
    """rollback() transitions to ROLLED_BACK and removes from active leases."""

    @pytest.mark.asyncio
    async def test_rollback_transitions_state(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.rollback(reason="test abort")
        assert grant.state == LeaseState.ROLLED_BACK
        assert grant.state.is_terminal

    @pytest.mark.asyncio
    async def test_rollback_removes_from_committed(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert broker.get_committed_bytes() == int(1 * 1024 ** 3)
        await grant.rollback()
        assert broker.get_committed_bytes() == 0


# ===================================================================
# 5. Rollback is idempotent (multiple calls OK)
# ===================================================================


class TestRollbackIdempotent:
    """Multiple rollback() calls are safe on terminal states."""

    @pytest.mark.asyncio
    async def test_double_rollback_no_error(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.rollback(reason="first")
        # Second call should be a no-op, not raise
        await grant.rollback(reason="second")
        assert grant.state == LeaseState.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_rollback_after_release_is_noop(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.commit(actual_bytes=int(1 * 1024 ** 3))
        await grant.release()
        assert grant.state == LeaseState.RELEASED
        # Rollback after release is a no-op
        await grant.rollback()
        assert grant.state == LeaseState.RELEASED


# ===================================================================
# 6. Release after commit (ACTIVE -> RELEASED, committed_bytes drops)
# ===================================================================


class TestReleaseAfterCommit:
    """release() transitions ACTIVE -> RELEASED and frees capacity."""

    @pytest.mark.asyncio
    async def test_release_transitions_active_to_released(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.commit(actual_bytes=int(900 * 1024 ** 2))
        assert broker.get_committed_bytes() == int(900 * 1024 ** 2)
        await grant.release()
        assert grant.state == LeaseState.RELEASED
        assert grant.state.is_terminal
        assert broker.get_committed_bytes() == 0


# ===================================================================
# 7. Context manager auto-rollback on exception
# ===================================================================


class TestContextManagerAutoRollback:
    """async with grant: ... auto-rolls-back on exception."""

    @pytest.mark.asyncio
    async def test_auto_rollback_on_exception(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        with pytest.raises(ValueError, match="load failed"):
            async with grant:
                raise ValueError("load failed")
        assert grant.state == LeaseState.ROLLED_BACK
        assert broker.get_committed_bytes() == 0

    @pytest.mark.asyncio
    async def test_auto_rollback_warning_no_commit(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        with pytest.warns(ResourceWarning, match="neither committed nor rolled back"):
            async with grant:
                pass  # forgot to commit
        assert grant.state == LeaseState.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_no_warning_after_commit(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        # Normal exit after commit should NOT warn
        async with grant:
            await grant.commit(actual_bytes=int(1 * 1024 ** 3))
        # State should be ACTIVE (not rolled back), since commit happened
        assert grant.state == LeaseState.ACTIVE


# ===================================================================
# 8. Epoch mismatch rejected (StaleEpochError)
# ===================================================================


class TestEpochMismatch:
    """Operations on a grant with stale epoch raise StaleEpochError."""

    @pytest.mark.asyncio
    async def test_commit_with_wrong_epoch(self):
        from backend.core.memory_budget_broker import StaleEpochError
        broker, _ = _make_broker(epoch=1)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        # Simulate epoch advance
        broker._epoch = 2
        with pytest.raises(StaleEpochError):
            await grant.commit(actual_bytes=int(1 * 1024 ** 3))

    @pytest.mark.asyncio
    async def test_rollback_with_wrong_epoch(self):
        from backend.core.memory_budget_broker import StaleEpochError
        broker, _ = _make_broker(epoch=1)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        broker._epoch = 2
        with pytest.raises(StaleEpochError):
            await grant.rollback()

    @pytest.mark.asyncio
    async def test_release_with_wrong_epoch(self):
        from backend.core.memory_budget_broker import StaleEpochError
        broker, _ = _make_broker(epoch=1)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.commit(actual_bytes=int(1 * 1024 ** 3))
        broker._epoch = 2
        with pytest.raises(StaleEpochError):
            await grant.release()

    @pytest.mark.asyncio
    async def test_heartbeat_with_wrong_epoch(self):
        from backend.core.memory_budget_broker import StaleEpochError
        broker, _ = _make_broker(epoch=1)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        broker._epoch = 2
        with pytest.raises(StaleEpochError):
            await grant.heartbeat()


# ===================================================================
# 9. get_committed_bytes tracks active leases correctly
# ===================================================================


class TestGetCommittedBytes:
    """get_committed_bytes() sums effective_bytes of GRANTED + ACTIVE leases."""

    @pytest.mark.asyncio
    async def test_tracks_granted(self):
        broker, quantizer = _make_broker()
        # Raise concurrent limit so we can have multiple grants
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        grant1 = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        grant2 = await broker.request(
            component="ecapa",
            bytes_requested=int(512 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        expected = int(1 * 1024 ** 3) + int(512 * 1024 ** 2)
        assert broker.get_committed_bytes() == expected

    @pytest.mark.asyncio
    async def test_committed_uses_actual_after_commit(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert broker.get_committed_bytes() == int(1 * 1024 ** 3)
        # Commit with smaller actual_bytes
        await grant.commit(actual_bytes=int(800 * 1024 ** 2))
        # Now committed_bytes should reflect actual
        assert broker.get_committed_bytes() == int(800 * 1024 ** 2)

    @pytest.mark.asyncio
    async def test_committed_drops_after_release(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.commit(actual_bytes=int(1 * 1024 ** 3))
        assert broker.get_committed_bytes() == int(1 * 1024 ** 3)
        await grant.release()
        assert broker.get_committed_bytes() == 0

    @pytest.mark.asyncio
    async def test_committed_drops_after_rollback(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert broker.get_committed_bytes() == int(1 * 1024 ** 3)
        await grant.rollback()
        assert broker.get_committed_bytes() == 0


# ===================================================================
# 10. Degraded grant issued when full doesn't fit but degradation does
# ===================================================================


class TestDegradedGrant:
    """Broker tries degradation_options when full request doesn't fit."""

    @pytest.mark.asyncio
    async def test_degraded_grant_issued(self):
        broker, _ = _make_broker(
            # headroom = 2 GB - 1 GB safety = 1 GB
            available_budget_bytes=int(2 * 1024 ** 3),
            safety_floor_bytes=int(1 * 1024 ** 3),
        )
        degradation_options = [
            DegradationOption(
                name="quantize_4bit",
                bytes_required=int(512 * 1024 ** 2),  # 0.5 GB fits in 1 GB headroom
                quality_impact=0.15,
                constraints={"quantization": "4bit"},
            ),
        ]
        # Request 3 GB -- too large, but degradation fits
        grant = await broker.request(
            component="llm",
            bytes_requested=int(3 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
            can_degrade=True,
            degradation_options=degradation_options,
        )
        assert grant.state == LeaseState.GRANTED
        assert grant.degraded is True
        assert grant.degradation_applied is not None
        assert grant.degradation_applied.name == "quantize_4bit"
        assert grant.granted_bytes == int(512 * 1024 ** 2)

    @pytest.mark.asyncio
    async def test_first_fitting_degradation_wins(self):
        broker, _ = _make_broker(
            available_budget_bytes=int(2 * 1024 ** 3),
            safety_floor_bytes=int(1 * 1024 ** 3),
        )
        degradation_options = [
            DegradationOption(
                name="8bit",
                bytes_required=int(2 * 1024 ** 3),  # 2 GB -- too large
                quality_impact=0.05,
                constraints={"quantization": "8bit"},
            ),
            DegradationOption(
                name="4bit",
                bytes_required=int(512 * 1024 ** 2),  # 0.5 GB -- fits
                quality_impact=0.15,
                constraints={"quantization": "4bit"},
            ),
        ]
        grant = await broker.request(
            component="llm",
            bytes_requested=int(4 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
            can_degrade=True,
            degradation_options=degradation_options,
        )
        assert grant.degraded is True
        assert grant.degradation_applied.name == "4bit"

    @pytest.mark.asyncio
    async def test_denied_when_no_degradation_fits(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker(
            available_budget_bytes=int(1 * 1024 ** 3),
            safety_floor_bytes=int(1 * 1024 ** 3),
        )
        # headroom = 0
        degradation_options = [
            DegradationOption(
                name="4bit",
                bytes_required=int(512 * 1024 ** 2),
                quality_impact=0.15,
                constraints={},
            ),
        ]
        with pytest.raises(BudgetDeniedError):
            await broker.request(
                component="llm",
                bytes_requested=int(4 * 1024 ** 3),
                priority=BudgetPriority.BOOT_CRITICAL,
                phase=StartupPhase.BOOT_CRITICAL,
                can_degrade=True,
                degradation_options=degradation_options,
            )


# ===================================================================
# 11. BOOT_CRITICAL phase rejects BOOT_OPTIONAL priority
# ===================================================================


class TestPhaseRejectsWrongPriority:
    """During BOOT_CRITICAL phase, only BOOT_CRITICAL priority is allowed."""

    @pytest.mark.asyncio
    async def test_boot_optional_rejected_in_boot_critical_phase(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker()
        # Phase is BOOT_CRITICAL by default
        with pytest.raises(BudgetDeniedError, match="not allowed in phase"):
            await broker.request(
                component="sentence_transformer",
                bytes_requested=int(512 * 1024 ** 2),
                priority=BudgetPriority.BOOT_OPTIONAL,
                phase=StartupPhase.BOOT_OPTIONAL,
            )

    @pytest.mark.asyncio
    async def test_runtime_interactive_rejected_in_boot_optional_phase(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker()
        broker.set_phase(StartupPhase.BOOT_OPTIONAL)
        with pytest.raises(BudgetDeniedError, match="not allowed in phase"):
            await broker.request(
                component="tool",
                bytes_requested=int(256 * 1024 ** 2),
                priority=BudgetPriority.RUNTIME_INTERACTIVE,
                phase=StartupPhase.RUNTIME_INTERACTIVE,
            )

    @pytest.mark.asyncio
    async def test_boot_critical_allowed_in_any_phase(self):
        broker, _ = _make_broker()
        broker.set_phase(StartupPhase.BACKGROUND)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.state == LeaseState.GRANTED


# ===================================================================
# 12. Phase concurrent limit enforced
# ===================================================================


class TestPhaseConcurrentLimit:
    """Broker enforces max_concurrent per phase policy."""

    @pytest.mark.asyncio
    async def test_boot_critical_allows_only_one(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker()
        # Phase BOOT_CRITICAL: max_concurrent=1
        grant1 = await broker.request(
            component="whisper",
            bytes_requested=int(512 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        with pytest.raises(BudgetDeniedError, match="Concurrent grant limit"):
            await broker.request(
                component="ecapa",
                bytes_requested=int(256 * 1024 ** 2),
                priority=BudgetPriority.BOOT_CRITICAL,
                phase=StartupPhase.BOOT_CRITICAL,
            )

    @pytest.mark.asyncio
    async def test_runtime_interactive_allows_three(self):
        broker, _ = _make_broker()
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        grants = []
        for i in range(3):
            grant = await broker.request(
                component=f"comp_{i}",
                bytes_requested=int(256 * 1024 ** 2),
                priority=BudgetPriority.BOOT_CRITICAL,
                phase=StartupPhase.BOOT_CRITICAL,
            )
            grants.append(grant)
        assert len(grants) == 3
        # 4th should fail
        from backend.core.memory_budget_broker import BudgetDeniedError
        with pytest.raises(BudgetDeniedError, match="Concurrent grant limit"):
            await broker.request(
                component="comp_3",
                bytes_requested=int(256 * 1024 ** 2),
                priority=BudgetPriority.BOOT_CRITICAL,
                phase=StartupPhase.BOOT_CRITICAL,
            )

    @pytest.mark.asyncio
    async def test_rollback_frees_concurrent_slot(self):
        broker, _ = _make_broker()
        # Phase BOOT_CRITICAL: max_concurrent=1
        grant1 = await broker.request(
            component="whisper",
            bytes_requested=int(512 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant1.rollback()
        # Now another grant should succeed
        grant2 = await broker.request(
            component="ecapa",
            bytes_requested=int(256 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant2.state == LeaseState.GRANTED


# ===================================================================
# 13. Swap hysteresis blocks BACKGROUND grants
# ===================================================================


class TestSwapHysteresisBlocksBackground:
    """BACKGROUND grants denied when swap hysteresis is active."""

    @pytest.mark.asyncio
    async def test_background_blocked_during_swap_hysteresis(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker(
            swap_growth_rate_bps=60 * 1024 * 1024,  # > 50 MB/s threshold
        )
        broker.set_phase(StartupPhase.BACKGROUND)
        with pytest.raises(BudgetDeniedError, match="Swap hysteresis"):
            await broker.request(
                component="bg_task",
                bytes_requested=int(256 * 1024 ** 2),
                priority=BudgetPriority.BACKGROUND,
                phase=StartupPhase.BACKGROUND,
            )

    @pytest.mark.asyncio
    async def test_boot_critical_not_blocked_by_swap(self):
        broker, _ = _make_broker(
            swap_growth_rate_bps=60 * 1024 * 1024,
        )
        broker.set_phase(StartupPhase.BACKGROUND)
        # BOOT_CRITICAL should still work
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(256 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.state == LeaseState.GRANTED

    @pytest.mark.asyncio
    async def test_background_allowed_when_swap_below_threshold(self):
        broker, _ = _make_broker(
            swap_growth_rate_bps=10 * 1024 * 1024,  # well below threshold
        )
        broker.set_phase(StartupPhase.BACKGROUND)
        grant = await broker.request(
            component="bg_task",
            bytes_requested=int(256 * 1024 ** 2),
            priority=BudgetPriority.BACKGROUND,
            phase=StartupPhase.BACKGROUND,
        )
        assert grant.state == LeaseState.GRANTED


# ===================================================================
# 14. Signal quality FALLBACK blocks non-critical
# ===================================================================


class TestSignalQualityFallback:
    """Non-BOOT_CRITICAL grants denied when signal quality is FALLBACK."""

    @pytest.mark.asyncio
    async def test_fallback_blocks_boot_optional(self):
        from backend.core.memory_budget_broker import BudgetDeniedError
        broker, _ = _make_broker(signal_quality=SignalQuality.FALLBACK)
        broker.set_phase(StartupPhase.BOOT_OPTIONAL)
        with pytest.raises(BudgetDeniedError, match="FALLBACK"):
            await broker.request(
                component="sentence_transformer",
                bytes_requested=int(256 * 1024 ** 2),
                priority=BudgetPriority.BOOT_OPTIONAL,
                phase=StartupPhase.BOOT_OPTIONAL,
            )

    @pytest.mark.asyncio
    async def test_fallback_allows_boot_critical(self):
        broker, _ = _make_broker(signal_quality=SignalQuality.FALLBACK)
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(256 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.state == LeaseState.GRANTED

    @pytest.mark.asyncio
    async def test_good_quality_allows_all(self):
        broker, _ = _make_broker(signal_quality=SignalQuality.GOOD)
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        grant = await broker.request(
            component="tool",
            bytes_requested=int(256 * 1024 ** 2),
            priority=BudgetPriority.RUNTIME_INTERACTIVE,
            phase=StartupPhase.RUNTIME_INTERACTIVE,
        )
        assert grant.state == LeaseState.GRANTED


# ===================================================================
# Additional: try_request returns None on denial
# ===================================================================


class TestTryRequest:
    """try_request returns None instead of raising."""

    @pytest.mark.asyncio
    async def test_returns_none_on_denial(self):
        broker, _ = _make_broker(
            available_budget_bytes=int(1 * 1024 ** 3),
            safety_floor_bytes=int(1 * 1024 ** 3),
        )
        result = await broker.try_request(
            component="llm",
            bytes_requested=int(4 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_grant_on_success(self):
        broker, _ = _make_broker()
        result = await broker.try_request(
            component="whisper",
            bytes_requested=int(512 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert result is not None
        assert result.state == LeaseState.GRANTED


# ===================================================================
# Additional: Singleton pattern
# ===================================================================


class TestSingleton:
    """Module-level singleton functions."""

    @pytest.mark.asyncio
    async def test_init_and_get(self):
        from backend.core.memory_budget_broker import (
            get_memory_budget_broker,
            init_memory_budget_broker,
        )
        import backend.core.memory_budget_broker as mod

        # Save original
        original = mod._broker_instance
        try:
            mod._broker_instance = None
            assert get_memory_budget_broker() is None
            quantizer = _make_quantizer()
            broker = await init_memory_budget_broker(quantizer, epoch=99)
            assert broker is not None
            assert get_memory_budget_broker() is broker
            assert broker._epoch == 99
        finally:
            mod._broker_instance = original


# ===================================================================
# Additional: get_status and get_active_leases
# ===================================================================


class TestStatusAndLeases:
    """Status dict and active lease listing."""

    @pytest.mark.asyncio
    async def test_get_status_fields(self):
        broker, _ = _make_broker(epoch=7)
        status = broker.get_status()
        assert status["epoch"] == 7
        assert status["phase"] == "BOOT_CRITICAL"
        assert status["committed_bytes"] == 0
        assert status["active_leases"] == 0

    @pytest.mark.asyncio
    async def test_get_active_leases(self):
        broker, _ = _make_broker()
        assert broker.get_active_leases() == []
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(512 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        active = broker.get_active_leases()
        assert len(active) == 1
        assert active[0].lease_id == grant.lease_id

    @pytest.mark.asyncio
    async def test_active_leases_excludes_rolled_back(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(512 * 1024 ** 2),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.rollback()
        assert broker.get_active_leases() == []


# ===================================================================
# Additional: Phase policy env var configurability
# ===================================================================


class TestPhaseCapEnvVars:
    """Budget cap percentages are configurable via env vars."""

    @pytest.mark.asyncio
    async def test_custom_boot_critical_cap(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MCP_PHASE_CAP_BOOT_CRITICAL", "0.50")
        from backend.core.memory_budget_broker import _build_phase_policies
        policies = _build_phase_policies()
        assert policies[StartupPhase.BOOT_CRITICAL].budget_cap_pct == 0.50

    @pytest.mark.asyncio
    async def test_invalid_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MCP_PHASE_CAP_BOOT_CRITICAL", "invalid")
        from backend.core.memory_budget_broker import _build_phase_policies
        policies = _build_phase_policies()
        assert policies[StartupPhase.BOOT_CRITICAL].budget_cap_pct == 0.60

    @pytest.mark.asyncio
    async def test_out_of_range_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MCP_PHASE_CAP_BOOT_CRITICAL", "1.5")
        from backend.core.memory_budget_broker import _build_phase_policies
        policies = _build_phase_policies()
        assert policies[StartupPhase.BOOT_CRITICAL].budget_cap_pct == 0.60


# ===================================================================
# Additional: BudgetGrant effective_bytes property
# ===================================================================


class TestEffectiveBytes:
    """effective_bytes returns actual_bytes after commit, granted_bytes before."""

    @pytest.mark.asyncio
    async def test_effective_before_commit(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        assert grant.effective_bytes == int(1 * 1024 ** 3)

    @pytest.mark.asyncio
    async def test_effective_after_commit(self):
        broker, _ = _make_broker()
        grant = await broker.request(
            component="whisper",
            bytes_requested=int(1 * 1024 ** 3),
            priority=BudgetPriority.BOOT_CRITICAL,
            phase=StartupPhase.BOOT_CRITICAL,
        )
        await grant.commit(actual_bytes=int(800 * 1024 ** 2))
        assert grant.effective_bytes == int(800 * 1024 ** 2)
