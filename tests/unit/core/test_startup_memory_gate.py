"""tests/unit/core/test_startup_memory_gate.py — Disease 5 OOM gate tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.core.startup_memory_gate import (
    ComponentMemoryBudget,
    MemoryGate,
    MemoryGateRefused,
    MemoryPressureLevel,
    _pressure_level,
    get_memory_gate,
)


class TestPressureLevel:
    def test_safe_when_above_20pct(self):
        assert _pressure_level(2100, 10000) == MemoryPressureLevel.SAFE

    def test_elevated_between_10_and_20(self):
        assert _pressure_level(1500, 10000) == MemoryPressureLevel.ELEVATED

    def test_critical_between_5_and_10(self):
        assert _pressure_level(700, 10000) == MemoryPressureLevel.CRITICAL

    def test_oom_imminent_below_5pct(self):
        assert _pressure_level(400, 10000) == MemoryPressureLevel.OOM_IMMINENT

    def test_safe_boundary_exactly_20pct(self):
        assert _pressure_level(2000, 10000) == MemoryPressureLevel.SAFE

    def test_zero_total_returns_safe(self):
        # Avoid ZeroDivisionError
        assert _pressure_level(0, 0) == MemoryPressureLevel.SAFE


class TestComponentMemoryBudget:
    def test_default_optional(self):
        b = ComponentMemoryBudget("svc", 512)
        assert b.optional is True

    def test_required_flag(self):
        b = ComponentMemoryBudget("svc", 512, optional=False)
        assert b.optional is False

    def test_frozen(self):
        import dataclasses
        b = ComponentMemoryBudget("svc", 512)
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            b.required_mib = 999  # type: ignore[misc]


class TestMemoryGateRefused:
    def test_attributes_set(self):
        err = MemoryGateRefused("svc", MemoryPressureLevel.OOM_IMMINENT, 200.0)
        assert err.component == "svc"
        assert err.pressure == MemoryPressureLevel.OOM_IMMINENT
        assert err.free_mib == 200.0

    def test_message_contains_component(self):
        err = MemoryGateRefused("neural_mesh", MemoryPressureLevel.CRITICAL, 512.0)
        assert "neural_mesh" in str(err)
        assert "critical" in str(err)


class TestMemoryGate:
    def _gate_with_free_pct(self, free_pct: float) -> MemoryGate:
        """Return a MemoryGate where free RAM = free_pct% of 10 GiB."""
        gate = MemoryGate()
        total_mib = 10240.0
        free_mib = total_mib * free_pct / 100.0
        gate._total_mib = total_mib
        with patch(
            "backend.core.startup_memory_gate._free_mib",
            return_value=free_mib,
        ):
            return gate, free_mib

    @pytest.mark.asyncio
    async def test_safe_pressure_proceeds(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        with patch("backend.core.startup_memory_gate._free_mib", return_value=3000.0):
            pressure = await gate.check("svc")
        assert pressure == MemoryPressureLevel.SAFE

    @pytest.mark.asyncio
    async def test_elevated_pressure_proceeds_with_warning(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        with patch("backend.core.startup_memory_gate._free_mib", return_value=1200.0):
            pressure = await gate.check("svc")
        assert pressure == MemoryPressureLevel.ELEVATED

    @pytest.mark.asyncio
    async def test_optional_component_refused_at_critical(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        gate.declare(ComponentMemoryBudget("neural", 2048, optional=True))
        with patch("backend.core.startup_memory_gate._free_mib", return_value=700.0):
            with pytest.raises(MemoryGateRefused) as exc_info:
                await gate.check("neural")
        assert exc_info.value.component == "neural"
        assert gate.shed_count == 1

    @pytest.mark.asyncio
    async def test_optional_component_refused_at_oom_imminent(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        gate.declare(ComponentMemoryBudget("vision", 1024, optional=True))
        with patch("backend.core.startup_memory_gate._free_mib", return_value=300.0):
            with pytest.raises(MemoryGateRefused):
                await gate.check("vision")

    @pytest.mark.asyncio
    async def test_unknown_component_defaults_to_optional(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        # No declaration for "unknown_svc"
        with patch("backend.core.startup_memory_gate._free_mib", return_value=400.0):
            with pytest.raises(MemoryGateRefused):
                await gate.check("unknown_svc")

    @pytest.mark.asyncio
    async def test_required_component_waits_and_recovers(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        gate.declare(ComponentMemoryBudget("router", 64, optional=False))

        # Pressure starts CRITICAL, recovers on second poll
        call_count = 0

        def free_mib_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return 600.0   # CRITICAL (< 10%)
            return 2200.0      # SAFE

        with patch("backend.core.startup_memory_gate._free_mib",
                   side_effect=free_mib_side_effect):
            with patch("backend.core.startup_memory_gate._POLL_INTERVAL_S", 0.01):
                pressure = await gate.check("router")
        assert pressure in (MemoryPressureLevel.SAFE, MemoryPressureLevel.ELEVATED)

    @pytest.mark.asyncio
    async def test_required_component_raises_after_max_wait(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        gate.declare(ComponentMemoryBudget("router", 64, optional=False))

        with patch("backend.core.startup_memory_gate._free_mib", return_value=300.0):
            with patch("backend.core.startup_memory_gate._MAX_WAIT_S", 0.05):
                with patch("backend.core.startup_memory_gate._POLL_INTERVAL_S", 0.01):
                    with pytest.raises(MemoryGateRefused):
                        await gate.check("router")

    def test_declare_registers_budget(self):
        gate = MemoryGate()
        gate.declare(ComponentMemoryBudget("svc", 512, optional=True))
        assert "svc" in gate._budgets

    def test_declare_many(self):
        gate = MemoryGate()
        gate.declare_many([
            ComponentMemoryBudget("a", 256),
            ComponentMemoryBudget("b", 512),
        ])
        assert "a" in gate._budgets
        assert "b" in gate._budgets

    def test_checked_count_increments(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        # Use asyncio.run to call async check from sync test
        import asyncio
        with patch("backend.core.startup_memory_gate._free_mib", return_value=3000.0):
            asyncio.get_event_loop().run_until_complete(gate.check("svc"))
        assert gate.checked_count == 1

    def test_current_pressure_safe(self):
        gate = MemoryGate()
        gate._total_mib = 10240.0
        with patch("backend.core.startup_memory_gate._free_mib", return_value=3000.0):
            assert gate.current_pressure() == MemoryPressureLevel.SAFE

    def test_shed_count_starts_at_zero(self):
        assert MemoryGate().shed_count == 0


class TestDefaultBudgets:
    def test_singleton_has_default_budgets(self):
        gate = get_memory_gate()
        # neural_mesh and agentic_system should be declared
        assert "neural_mesh" in gate._budgets
        assert "agentic_system" in gate._budgets

    def test_neural_mesh_is_optional(self):
        gate = get_memory_gate()
        assert gate._budgets["neural_mesh"].optional is True

    def test_cloud_sql_proxy_is_required(self):
        gate = get_memory_gate()
        assert gate._budgets["cloud_sql_proxy"].optional is False


class TestModuleSingleton:
    def test_get_memory_gate_is_reused(self):
        g1 = get_memory_gate()
        g2 = get_memory_gate()
        assert g1 is g2
