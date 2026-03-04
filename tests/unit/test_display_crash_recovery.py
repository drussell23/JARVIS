"""Tests for display lease crash recovery in broker reconciliation."""
import json
import os

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.memory_types import (
    LeaseState,
    BudgetPriority,
    StartupPhase,
    PressureTier,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker


class TestDisplayLeaseRecovery:
    @pytest.mark.asyncio
    async def test_connected_display_lease_restored(self, tmp_path):
        """If ghost display is still connected, restore the lease."""
        lease_file = tmp_path / "leases.json"
        lease_file.write_text(json.dumps({
            "schema_version": "1.0",
            "broker_epoch": 5,
            "leases": [{
                "lease_id": "lease_display_001",
                "component_id": "display:ghost@v1",
                "granted_bytes": 32_000_000,
                "state": "active",
                "priority": "BOOT_OPTIONAL",
                "epoch": 5,
                "pid": os.getpid(),
                "metadata": {"resolution": "1920x1080"},
            }],
        }))

        q = MagicMock()
        q.snapshot = AsyncMock(return_value=MagicMock(
            pressure_tier=PressureTier.ABUNDANT,
        ))
        broker = MemoryBudgetBroker(q, epoch=5, lease_file=lease_file)

        with patch(
            "backend.core.memory_budget_broker._query_ghost_display_connected",
            new_callable=AsyncMock,
            return_value=True,
        ):
            report = await broker.reconcile_stale_leases()
            # Display is connected so it should NOT be marked stale
            assert report["stale"] == 0
            assert report["reclaimed_bytes"] == 0

    @pytest.mark.asyncio
    async def test_disconnected_display_lease_released(self, tmp_path):
        """If ghost display is not connected, release the lease."""
        lease_file = tmp_path / "leases.json"
        lease_file.write_text(json.dumps({
            "schema_version": "1.0",
            "broker_epoch": 5,
            "leases": [{
                "lease_id": "lease_display_002",
                "component_id": "display:ghost@v1",
                "granted_bytes": 32_000_000,
                "state": "active",
                "priority": "BOOT_OPTIONAL",
                "epoch": 5,
                "pid": os.getpid(),
                "metadata": {"resolution": "1920x1080"},
            }],
        }))

        q = MagicMock()
        q.snapshot = AsyncMock(return_value=MagicMock(
            pressure_tier=PressureTier.ABUNDANT,
        ))
        broker = MemoryBudgetBroker(q, epoch=5, lease_file=lease_file)

        with patch(
            "backend.core.memory_budget_broker._query_ghost_display_connected",
            new_callable=AsyncMock,
            return_value=False,
        ):
            report = await broker.reconcile_stale_leases()
            assert report["stale"] == 1
            assert report["reclaimed_bytes"] == 32_000_000

    @pytest.mark.asyncio
    async def test_non_display_lease_uses_pid_check(self, tmp_path):
        """Non-display leases should still use the standard PID-based check."""
        lease_file = tmp_path / "leases.json"
        lease_file.write_text(json.dumps({
            "schema_version": "1.0",
            "broker_epoch": 5,
            "leases": [{
                "lease_id": "lease_model_001",
                "component_id": "model:whisper@v3",
                "granted_bytes": 500_000_000,
                "state": "active",
                "priority": "BOOT_CRITICAL",
                "epoch": 5,
                "pid": os.getpid(),  # alive PID, same epoch -> not stale
            }],
        }))

        q = MagicMock()
        broker = MemoryBudgetBroker(q, epoch=5, lease_file=lease_file)

        report = await broker.reconcile_stale_leases()
        # Same epoch, alive PID -> not stale
        assert report["stale"] == 0
        assert report["reclaimed_bytes"] == 0

    @pytest.mark.asyncio
    async def test_mixed_display_and_model_leases(self, tmp_path):
        """Display disconnected + model with dead PID -> both reclaimed."""
        lease_file = tmp_path / "leases.json"
        lease_file.write_text(json.dumps({
            "schema_version": "1.0",
            "broker_epoch": 5,
            "leases": [
                {
                    "lease_id": "lease_display_003",
                    "component_id": "display:ghost@v1",
                    "granted_bytes": 32_000_000,
                    "state": "active",
                    "priority": "BOOT_OPTIONAL",
                    "epoch": 5,
                    "pid": os.getpid(),
                },
                {
                    "lease_id": "lease_model_002",
                    "component_id": "model:llm@v1",
                    "granted_bytes": 200_000_000,
                    "state": "active",
                    "priority": "BOOT_CRITICAL",
                    "epoch": 3,  # stale epoch
                    "pid": os.getpid(),
                },
            ],
        }))

        q = MagicMock()
        broker = MemoryBudgetBroker(q, epoch=5, lease_file=lease_file)

        with patch(
            "backend.core.memory_budget_broker._query_ghost_display_connected",
            new_callable=AsyncMock,
            return_value=False,
        ):
            report = await broker.reconcile_stale_leases()
            # Display disconnected (stale) + model with wrong epoch (stale)
            assert report["stale"] == 2
            assert report["reclaimed_bytes"] == 32_000_000 + 200_000_000
