"""Tests for resource governor MCP broker integration.

Verifies that ``AdaptiveResourceGovernor`` can register as a broker
pressure observer, correctly map ``PressureTier`` to ``DefconLevel``,
submit ``DEFCON_ESCALATE`` actions on escalation, and skip the legacy
psutil polling path when ``_mcp_active`` is True.
"""
from __future__ import annotations

import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.memory_types import (
    ActuatorAction,
    DecisionEnvelope,
    MemorySnapshot,
    PressurePolicy,
    PressureTier,
)
from backend.core.resource_governor import (
    AdaptiveResourceGovernor,
    DefconLevel,
    _TIER_TO_DEFCON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_governor(**kwargs) -> AdaptiveResourceGovernor:
    """Create a governor with narration disabled and short stabilization."""
    defaults = {
        "enable_narration": False,
    }
    defaults.update(kwargs)
    return AdaptiveResourceGovernor(**defaults)


def _mock_broker(epoch: int = 1) -> MagicMock:
    """Create a mock broker with the attributes the governor accesses."""
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.current_epoch = epoch
    broker.current_sequence = 5
    broker.policy = PressurePolicy()
    broker.coordinator = MagicMock()
    broker.coordinator.submit = MagicMock(return_value="dec-abc123")
    return broker


def _mock_snapshot(
    tier: PressureTier = PressureTier.OPTIMAL,
    snapshot_id: str = "snap-test-001",
) -> MagicMock:
    """Create a mock MemorySnapshot with the fields the governor reads."""
    snap = MagicMock(spec=MemorySnapshot)
    snap.pressure_tier = tier
    snap.snapshot_id = snapshot_id
    return snap


# ---------------------------------------------------------------------------
# Test: register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    def test_sets_mcp_active_true(self):
        gov = _make_governor()
        broker = _mock_broker()
        assert gov._mcp_active is False
        gov.register_with_broker(broker)
        assert gov._mcp_active is True

    def test_stores_broker_reference(self):
        gov = _make_governor()
        broker = _mock_broker()
        assert gov._broker is None
        gov.register_with_broker(broker)
        assert gov._broker is broker

    def test_calls_register_pressure_observer(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        broker.register_pressure_observer.assert_called_once_with(
            gov._on_pressure_change
        )


# ---------------------------------------------------------------------------
# Test: PressureTier -> DefconLevel mapping
# ---------------------------------------------------------------------------

class TestPressureTierMapping:
    """Verify the _TIER_TO_DEFCON module-level mapping table."""

    @pytest.mark.parametrize("tier", [
        PressureTier.ABUNDANT,
        PressureTier.OPTIMAL,
        PressureTier.ELEVATED,
    ])
    def test_nominal_elevated_map_to_green(self, tier: PressureTier):
        assert _TIER_TO_DEFCON[tier] == DefconLevel.GREEN

    def test_constrained_maps_to_yellow(self):
        assert _TIER_TO_DEFCON[PressureTier.CONSTRAINED] == DefconLevel.YELLOW

    @pytest.mark.parametrize("tier", [
        PressureTier.CRITICAL,
        PressureTier.EMERGENCY,
    ])
    def test_critical_emergency_map_to_red(self, tier: PressureTier):
        assert _TIER_TO_DEFCON[tier] == DefconLevel.RED

    def test_mapping_covers_all_tiers(self):
        """Every PressureTier must have a DefconLevel mapping."""
        for tier in PressureTier:
            assert tier in _TIER_TO_DEFCON, (
                f"PressureTier.{tier.name} is missing from _TIER_TO_DEFCON"
            )


# ---------------------------------------------------------------------------
# Test: _on_pressure_change callback
# ---------------------------------------------------------------------------

class TestOnPressureChange:

    @pytest.mark.asyncio
    async def test_nominal_sets_green(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        # Start at YELLOW so the transition to GREEN is visible
        gov._current_level = DefconLevel.YELLOW
        gov._level_start_time = time.time() - 100  # bypass stabilization

        await gov._on_pressure_change(PressureTier.OPTIMAL, _mock_snapshot())
        assert gov._current_level == DefconLevel.GREEN

    @pytest.mark.asyncio
    async def test_elevated_stays_green(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.ELEVATED, _mock_snapshot())
        assert gov._current_level == DefconLevel.GREEN

    @pytest.mark.asyncio
    async def test_constrained_sets_yellow(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.CONSTRAINED, _mock_snapshot())
        assert gov._current_level == DefconLevel.YELLOW

    @pytest.mark.asyncio
    async def test_critical_sets_red(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.CRITICAL, _mock_snapshot())
        assert gov._current_level == DefconLevel.RED

    @pytest.mark.asyncio
    async def test_emergency_sets_red(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.EMERGENCY, _mock_snapshot())
        assert gov._current_level == DefconLevel.RED

    @pytest.mark.asyncio
    async def test_same_level_no_transition(self):
        """If the mapped level equals current level, no transition occurs."""
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        initial_count = gov._transitions_count

        await gov._on_pressure_change(PressureTier.OPTIMAL, _mock_snapshot())
        assert gov._transitions_count == initial_count

    @pytest.mark.asyncio
    async def test_stabilization_timer_blocks_non_red(self):
        """Transition to YELLOW should be blocked if stabilization timer hasn't elapsed."""
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time()  # just now -- stabilization not elapsed

        await gov._on_pressure_change(PressureTier.CONSTRAINED, _mock_snapshot())
        # Should still be GREEN because stabilization timer hasn't elapsed
        assert gov._current_level == DefconLevel.GREEN

    @pytest.mark.asyncio
    async def test_red_bypasses_stabilization_timer(self):
        """Transition to RED should bypass stabilization timer."""
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time()  # just now

        await gov._on_pressure_change(PressureTier.CRITICAL, _mock_snapshot())
        # RED bypasses stabilization -- should transition immediately
        assert gov._current_level == DefconLevel.RED


# ---------------------------------------------------------------------------
# Test: DEFCON_ESCALATE submission on escalation
# ---------------------------------------------------------------------------

class TestDefconEscalateSubmission:

    @pytest.mark.asyncio
    async def test_escalation_green_to_yellow_submits(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.CONSTRAINED, _mock_snapshot())
        broker.coordinator.submit.assert_called_once()
        call_args = broker.coordinator.submit.call_args
        assert call_args[0][0] == ActuatorAction.DEFCON_ESCALATE
        # source is passed as keyword arg
        all_args = {**dict(enumerate(call_args[0])), **call_args[1]}
        assert all_args.get("source", all_args.get(2)) == "resource_governor"

    @pytest.mark.asyncio
    async def test_escalation_yellow_to_red_submits(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.YELLOW
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.CRITICAL, _mock_snapshot())
        broker.coordinator.submit.assert_called_once()
        call_args = broker.coordinator.submit.call_args
        assert call_args[0][0] == ActuatorAction.DEFCON_ESCALATE

    @pytest.mark.asyncio
    async def test_escalation_green_to_red_submits(self):
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.EMERGENCY, _mock_snapshot())
        broker.coordinator.submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_de_escalation_red_to_yellow_does_not_submit(self):
        """De-escalation (RED -> YELLOW) should NOT submit DEFCON_ESCALATE."""
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.RED
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.CONSTRAINED, _mock_snapshot())
        assert gov._current_level == DefconLevel.YELLOW
        broker.coordinator.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_de_escalation_yellow_to_green_does_not_submit(self):
        """De-escalation (YELLOW -> GREEN) should NOT submit DEFCON_ESCALATE."""
        gov = _make_governor()
        broker = _mock_broker()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.YELLOW
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.OPTIMAL, _mock_snapshot())
        assert gov._current_level == DefconLevel.GREEN
        broker.coordinator.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_envelope_uses_broker_epoch_and_sequence(self):
        """The DecisionEnvelope should use the broker's epoch and sequence."""
        gov = _make_governor()
        broker = _mock_broker(epoch=42)
        broker.current_sequence = 17
        broker.policy = PressurePolicy()
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        await gov._on_pressure_change(PressureTier.CONSTRAINED, _mock_snapshot())
        call_args = broker.coordinator.submit.call_args
        envelope = call_args[0][1]
        assert isinstance(envelope, DecisionEnvelope)
        assert envelope.epoch == 42
        assert envelope.sequence == 17
        assert envelope.policy_version == "v1.0"

    @pytest.mark.asyncio
    async def test_coordinator_submit_failure_is_caught(self):
        """If coordinator.submit raises, the governor should not crash."""
        gov = _make_governor()
        broker = _mock_broker()
        broker.coordinator.submit.side_effect = RuntimeError("boom")
        gov.register_with_broker(broker)
        gov._current_level = DefconLevel.GREEN
        gov._level_start_time = time.time() - 100

        # Should not raise
        await gov._on_pressure_change(PressureTier.CRITICAL, _mock_snapshot())
        assert gov._current_level == DefconLevel.RED


# ---------------------------------------------------------------------------
# Test: _update_memory_state with _mcp_active
# ---------------------------------------------------------------------------

class TestUpdateMemoryStateMcpActive:

    @pytest.mark.asyncio
    async def test_skips_psutil_when_mcp_active(self):
        """When _mcp_active is True, _update_memory_state should return
        immediately without calling psutil."""
        gov = _make_governor()
        gov._mcp_active = True
        gov._psutil = MagicMock()  # should NOT be called

        await gov._update_memory_state()
        # If psutil was called, virtual_memory() would have been invoked
        gov._psutil.virtual_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_psutil_when_mcp_inactive(self):
        """When _mcp_active is False, _update_memory_state should call psutil."""
        gov = _make_governor()
        gov._mcp_active = False

        mock_mem = MagicMock()
        mock_mem.percent = 55.0
        mock_mem.available = 8 * (1024 ** 3)
        mock_mem.used = 8 * (1024 ** 3)
        mock_mem.total = 16 * (1024 ** 3)

        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value = mock_mem
        gov._psutil = mock_psutil
        gov._is_macos = False  # skip page outs

        await gov._update_memory_state()
        mock_psutil.virtual_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_active_does_not_update_last_memory_check(self):
        """When mcp_active, _last_memory_check should not be updated by
        _update_memory_state (observer handles state externally)."""
        gov = _make_governor()
        gov._mcp_active = True
        gov._last_memory_check = None

        await gov._update_memory_state()
        assert gov._last_memory_check is None


# ---------------------------------------------------------------------------
# Test: Default attribute initialization
# ---------------------------------------------------------------------------

class TestDefaultAttributes:
    def test_mcp_active_default_false(self):
        gov = _make_governor()
        assert gov._mcp_active is False

    def test_broker_default_none(self):
        gov = _make_governor()
        assert gov._broker is None


# ---------------------------------------------------------------------------
# Test: Design intent (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_contains_register_pressure_observer(self):
        source = inspect.getsource(AdaptiveResourceGovernor)
        assert "register_pressure_observer" in source, (
            "resource_governor must call broker.register_pressure_observer()"
        )

    def test_source_contains_on_pressure_change(self):
        source = inspect.getsource(AdaptiveResourceGovernor)
        assert "_on_pressure_change" in source, (
            "resource_governor must define _on_pressure_change callback"
        )

    def test_on_pressure_change_is_async(self):
        assert asyncio.iscoroutinefunction(
            AdaptiveResourceGovernor._on_pressure_change
        ), "_on_pressure_change must be an async method"

    def test_source_contains_defcon_escalate(self):
        source = inspect.getsource(AdaptiveResourceGovernor._on_pressure_change)
        assert "DEFCON_ESCALATE" in source, (
            "_on_pressure_change must submit DEFCON_ESCALATE"
        )

    def test_source_contains_mcp_active_guard(self):
        source = inspect.getsource(AdaptiveResourceGovernor._update_memory_state)
        assert "_mcp_active" in source, (
            "_update_memory_state must check _mcp_active flag"
        )


# Need asyncio import for iscoroutinefunction in TestDesignIntent
import asyncio
