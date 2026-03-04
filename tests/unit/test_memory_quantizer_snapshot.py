"""Tests for MemoryQuantizer.snapshot() method.

Verifies that the snapshot() method produces a fully-populated, frozen
MemorySnapshot from backend.core.memory_types with correct field values
derived from the quantizer's internal state and live psutil signals.
"""

import asyncio
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.memory_types import (
    KernelPressure,
    MemorySnapshot,
    PressureTier,
    PressureTrend,
    SignalQuality,
    ThrashState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vmem(
    total: int = 16 * 1024**3,
    available: int = 6 * 1024**3,
    percent: float = 62.5,
    used: int = 10 * 1024**3,
    free: int = 2 * 1024**3,
    active: int = 5 * 1024**3,
    inactive: int = 3 * 1024**3,
    wired: int = 3 * 1024**3,
    compressed: int = 1 * 1024**3,
):
    """Return a mock that behaves like psutil.virtual_memory()."""
    m = MagicMock()
    m.total = total
    m.available = available
    m.percent = percent
    m.used = used
    m.free = free
    m.active = active
    m.inactive = inactive
    m.wired = wired
    m.compressed = compressed
    # getattr / hasattr support
    m.__contains__ = lambda self, key: hasattr(self, key)
    return m


def _make_swap(total: int = 4 * 1024**3, used: int = 1 * 1024**3, percent: float = 25.0):
    """Return a mock that behaves like psutil.swap_memory()."""
    m = MagicMock()
    m.total = total
    m.used = used
    m.percent = percent
    return m


def _build_quantizer(**overrides):
    """Create a MemoryQuantizer instance bypassing normal __init__.

    Uses __new__ + manual field setup to avoid needing real psutil,
    learning_db, or other heavyweight dependencies.
    """
    from backend.core.memory_quantizer import MemoryQuantizer, MemoryTier

    q = object.__new__(MemoryQuantizer)

    # Minimal fields that snapshot() reads:
    q.current_tier = overrides.pop("current_tier", MemoryTier.OPTIMAL)
    q._thrash_state = overrides.pop("_thrash_state", "healthy")
    q._pagein_rate_ema = overrides.pop("_pagein_rate_ema", 0.0)
    q._supervisor_epoch = overrides.pop("_supervisor_epoch", 0)
    q._broker_ref = overrides.pop("_broker_ref", None)
    q._memory_reservations = overrides.pop("_memory_reservations", {})
    q._last_pageins = overrides.pop("_last_pageins", None)
    q._last_pagein_time = overrides.pop("_last_pagein_time", 0.0)
    q._pagein_rate = overrides.pop("_pagein_rate", 0.0)

    # Apply any remaining overrides
    for k, v in overrides.items():
        setattr(q, k, v)

    return q


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSnapshotReturnsMemorySnapshot:
    """snapshot() returns a MemorySnapshot with correct physical fields."""

    @pytest.mark.asyncio
    async def test_returns_memory_snapshot_type(self):
        q = _build_quantizer()
        vmem = _make_vmem()
        swap = _make_swap()

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = vmem
            mock_psutil.swap_memory.return_value = swap
            q._get_memory_pressure_async = AsyncMock(
                return_value=MagicMock(value="normal")
            )
            # Patch to return the correct enum
            from backend.core.memory_quantizer import MemoryPressure
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=42.0)

            snap = await q.snapshot()

        assert isinstance(snap, MemorySnapshot)

    @pytest.mark.asyncio
    async def test_physical_fields_match_psutil(self):
        q = _build_quantizer()
        total = 16 * 1024**3
        wired = 3 * 1024**3
        active = 5 * 1024**3
        inactive = 2 * 1024**3
        compressed = 1 * 1024**3
        free = 4 * 1024**3
        vmem = _make_vmem(
            total=total, wired=wired, active=active,
            inactive=inactive, compressed=compressed, free=free,
        )
        swap = _make_swap(total=4 * 1024**3, used=512 * 1024**2)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = vmem
            mock_psutil.swap_memory.return_value = swap
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.physical_total == total
        assert snap.physical_wired == wired
        assert snap.physical_active == active
        assert snap.physical_inactive == inactive
        assert snap.physical_compressed == compressed
        assert snap.physical_free == free
        assert snap.swap_total == 4 * 1024**3
        assert snap.swap_used == 512 * 1024**2


class TestSnapshotEpoch:
    """Epoch in snapshot matches supervisor epoch."""

    @pytest.mark.asyncio
    async def test_epoch_matches_supervisor_epoch(self):
        q = _build_quantizer(_supervisor_epoch=42)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.epoch == 42

    @pytest.mark.asyncio
    async def test_set_supervisor_epoch_updates_snapshot(self):
        q = _build_quantizer(_supervisor_epoch=0)
        q.set_supervisor_epoch(99)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.epoch == 99


class TestSnapshotSignalQuality:
    """Signal quality degrades when kernel pressure read fails."""

    @pytest.mark.asyncio
    async def test_good_quality_when_pressure_succeeds(self):
        q = _build_quantizer()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.signal_quality == SignalQuality.GOOD

    @pytest.mark.asyncio
    async def test_degraded_when_pressure_fails(self):
        q = _build_quantizer()

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                side_effect=RuntimeError("memory_pressure failed")
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.signal_quality == SignalQuality.DEGRADED

    @pytest.mark.asyncio
    async def test_degraded_when_pagein_fails(self):
        q = _build_quantizer()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(
                side_effect=OSError("vm_stat failed")
            )

            snap = await q.snapshot()

        assert snap.signal_quality == SignalQuality.DEGRADED


class TestSnapshotCommittedBytes:
    """committed_bytes comes from broker_ref when set."""

    @pytest.mark.asyncio
    async def test_committed_zero_without_broker(self):
        q = _build_quantizer(_broker_ref=None)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.committed_bytes == 0

    @pytest.mark.asyncio
    async def test_committed_from_broker(self):
        broker = MagicMock()
        broker.get_committed_bytes.return_value = 2 * 1024**3  # 2 GiB
        q = _build_quantizer(_broker_ref=broker)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.committed_bytes == 2 * 1024**3

    @pytest.mark.asyncio
    async def test_committed_zero_when_broker_raises(self):
        broker = MagicMock()
        broker.get_committed_bytes.side_effect = RuntimeError("broker down")
        q = _build_quantizer(_broker_ref=broker)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.committed_bytes == 0

    @pytest.mark.asyncio
    async def test_set_broker_ref(self):
        q = _build_quantizer(_broker_ref=None)
        broker = MagicMock()
        broker.get_committed_bytes.return_value = 1024
        q.set_broker_ref(broker)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.committed_bytes == 1024


class TestSnapshotUsableBytes:
    """usable_bytes = total - wired - compressed_trend (no safety floor)."""

    @pytest.mark.asyncio
    async def test_usable_bytes_formula(self):
        total = 16 * 1024**3
        wired = 3 * 1024**3
        compressed = 1 * 1024**3
        q = _build_quantizer()

        vmem = _make_vmem(total=total, wired=wired, compressed=compressed)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = vmem
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        expected = total - wired - compressed
        assert snap.usable_bytes == expected

    @pytest.mark.asyncio
    async def test_usable_bytes_floored_at_zero(self):
        """When wired + compressed > total, usable should be 0 not negative."""
        total = 4 * 1024**3
        wired = 3 * 1024**3
        compressed = 2 * 1024**3
        q = _build_quantizer()

        vmem = _make_vmem(total=total, wired=wired, compressed=compressed)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = vmem
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.usable_bytes == 0


class TestSnapshotPressureTierMapping:
    """current_tier (MemoryTier) maps correctly to PressureTier."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier_name,expected_pt", [
        ("ABUNDANT", PressureTier.ABUNDANT),
        ("OPTIMAL", PressureTier.OPTIMAL),
        ("ELEVATED", PressureTier.ELEVATED),
        ("CONSTRAINED", PressureTier.CONSTRAINED),
        ("CRITICAL", PressureTier.CRITICAL),
        ("EMERGENCY", PressureTier.EMERGENCY),
    ])
    async def test_tier_mapping(self, tier_name, expected_pt):
        from backend.core.memory_quantizer import MemoryTier, MemoryPressure
        q = _build_quantizer(current_tier=MemoryTier[tier_name])

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.pressure_tier == expected_pt


class TestSnapshotThrashStateMapping:
    """_thrash_state string maps to ThrashState enum."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state_str,expected_ts", [
        ("healthy", ThrashState.HEALTHY),
        ("thrashing", ThrashState.THRASHING),
        ("emergency", ThrashState.EMERGENCY),
    ])
    async def test_thrash_mapping(self, state_str, expected_ts):
        from backend.core.memory_quantizer import MemoryPressure
        q = _build_quantizer(_thrash_state=state_str)

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.thrash_state == expected_ts


class TestSnapshotSafetyFloor:
    """Safety floor scales with tier multiplier."""

    @pytest.mark.asyncio
    async def test_safety_floor_abundant(self):
        from backend.core.memory_quantizer import MemoryTier, MemoryPressure
        total = 16 * 1024**3
        q = _build_quantizer(current_tier=MemoryTier.ABUNDANT)

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem(total=total)
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        expected = int(total * 0.10 * 1.0)
        assert snap.safety_floor_bytes == expected

    @pytest.mark.asyncio
    async def test_safety_floor_emergency(self):
        from backend.core.memory_quantizer import MemoryTier, MemoryPressure
        total = 16 * 1024**3
        q = _build_quantizer(current_tier=MemoryTier.EMERGENCY)

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem(total=total)
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        expected = int(total * 0.10 * 2.5)
        assert snap.safety_floor_bytes == expected

    @pytest.mark.asyncio
    async def test_safety_floor_constrained(self):
        from backend.core.memory_quantizer import MemoryTier, MemoryPressure
        total = 16 * 1024**3
        q = _build_quantizer(current_tier=MemoryTier.CONSTRAINED)

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem(total=total)
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        expected = int(total * 0.10 * 1.5)
        assert snap.safety_floor_bytes == expected


class TestSnapshotAvailableBudget:
    """available_budget_bytes = usable_bytes - committed_bytes."""

    @pytest.mark.asyncio
    async def test_available_budget_with_broker(self):
        total = 16 * 1024**3
        wired = 3 * 1024**3
        compressed = 1 * 1024**3
        committed = 2 * 1024**3

        broker = MagicMock()
        broker.get_committed_bytes.return_value = committed
        q = _build_quantizer(_broker_ref=broker)

        vmem = _make_vmem(total=total, wired=wired, compressed=compressed)

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = vmem
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        usable = total - wired - compressed
        expected = usable - committed
        assert snap.available_budget_bytes == expected


class TestSnapshotKernelPressureMapping:
    """MemoryPressure -> KernelPressure mapping."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mp_val,expected_kp", [
        ("NORMAL", KernelPressure.NORMAL),
        ("WARN", KernelPressure.WARN),
        ("CRITICAL", KernelPressure.CRITICAL),
    ])
    async def test_kernel_pressure_mapping(self, mp_val, expected_kp):
        from backend.core.memory_quantizer import MemoryPressure
        q = _build_quantizer()

        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure[mp_val]
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.kernel_pressure == expected_kp


class TestSnapshotMaxAge:
    """max_age_ms passthrough."""

    @pytest.mark.asyncio
    async def test_max_age_default(self):
        q = _build_quantizer()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.max_age_ms == 0

    @pytest.mark.asyncio
    async def test_max_age_custom(self):
        q = _build_quantizer()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot(max_age_ms=500)

        assert snap.max_age_ms == 500


class TestSnapshotIdAndTimestamp:
    """Snapshot has unique ID and recent timestamp."""

    @pytest.mark.asyncio
    async def test_snapshot_id_is_prefixed(self):
        q = _build_quantizer()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        assert snap.snapshot_id.startswith("snap_")

    @pytest.mark.asyncio
    async def test_timestamp_is_recent(self):
        q = _build_quantizer()
        before = time.time()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        after = time.time()
        assert before <= snap.timestamp <= after


class TestSnapshotFrozen:
    """MemorySnapshot is frozen (immutable)."""

    @pytest.mark.asyncio
    async def test_snapshot_is_immutable(self):
        q = _build_quantizer()

        from backend.core.memory_quantizer import MemoryPressure
        with patch("backend.core.memory_quantizer.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = _make_vmem()
            mock_psutil.swap_memory.return_value = _make_swap()
            q._get_memory_pressure_async = AsyncMock(
                return_value=MemoryPressure.NORMAL
            )
            q._get_pagein_rate_async = AsyncMock(return_value=0.0)

            snap = await q.snapshot()

        with pytest.raises(AttributeError):
            snap.physical_total = 0
