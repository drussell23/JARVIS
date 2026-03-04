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
        await grant.commit(95_000_000, ConfigProof("test:v1", {}, {}, True, "ok"))
        data = json.loads((tmp_path / "leases.json").read_text())
        lease = data["leases"][0]
        assert lease["state"] == "active"
        assert lease["actual_bytes"] == 95_000_000

    @pytest.mark.asyncio
    async def test_lease_removed_from_file_on_release(self, broker, tmp_path):
        grant = await broker.request("test:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        await grant.commit(95_000_000, ConfigProof("test:v1", {}, {}, True, "ok"))
        await grant.release()
        data = json.loads((tmp_path / "leases.json").read_text())
        assert len(data["leases"]) == 0

    @pytest.mark.asyncio
    async def test_lease_removed_from_file_on_rollback(self, broker, tmp_path):
        grant = await broker.request("test:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        await grant.rollback("test")
        data = json.loads((tmp_path / "leases.json").read_text())
        assert len(data["leases"]) == 0

    @pytest.mark.asyncio
    async def test_lease_file_has_pid(self, broker, tmp_path):
        await broker.request("test:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        data = json.loads((tmp_path / "leases.json").read_text())
        assert data["leases"][0]["pid"] == os.getpid()

    @pytest.mark.asyncio
    async def test_lease_file_atomic_write(self, broker, tmp_path):
        """Ensure no .tmp file is left behind after write."""
        await broker.request("test:v1", 100_000_000, BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE)
        assert not (tmp_path / "leases.tmp").exists()


class TestCrashReclaim:
    @pytest.mark.asyncio
    async def test_stale_epoch_reclaimed(self, tmp_path):
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
                "pid": 99999999,
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

    @pytest.mark.asyncio
    async def test_missing_file_handled(self, tmp_path):
        lease_file = tmp_path / "nonexistent" / "leases.json"
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=lease_file)
        report = await broker.reconcile_stale_leases()
        assert report["stale"] == 0
        assert report["corrupted"] is False

    @pytest.mark.asyncio
    async def test_dead_pid_reclaimed(self, tmp_path):
        """Leases from the current epoch but dead PIDs are reclaimed."""
        lease_file = tmp_path / "leases.json"
        lease_file.write_text(json.dumps({
            "schema_version": "1.0",
            "broker_epoch": 1,
            "leases": [{
                "lease_id": "dead-pid-lease",
                "component_id": "whisper:base@v1",
                "granted_bytes": 1_000_000_000,
                "actual_bytes": 900_000_000,
                "state": "active",
                "priority": "BOOT_OPTIONAL",
                "phase": "BOOT_OPTIONAL",
                "pid": 99999999,
                "epoch": 1,
            }],
        }))

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=lease_file)
        report = await broker.reconcile_stale_leases()
        assert report["stale"] == 1
        assert report["reclaimed_bytes"] == 900_000_000

    @pytest.mark.asyncio
    async def test_reconcile_overwrites_with_clean_state(self, tmp_path):
        """After reconciliation, lease file reflects current broker state."""
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
                "pid": 99999999,
                "epoch": 5,
            }],
        }))

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=6, lease_file=lease_file)
        await broker.reconcile_stale_leases()

        # The file should now reflect the clean broker state (no leases)
        data = json.loads(lease_file.read_text())
        assert data["broker_epoch"] == 6
        assert len(data["leases"]) == 0
