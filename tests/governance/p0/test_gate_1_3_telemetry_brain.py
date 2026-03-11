"""Gate 1.3 — TelemetryContextualizer + BrainSelector purity.

Rubric requirements:
- test_telemetry_contextualizer_uses_remote_source_for_remote_route
- test_telemetry_contextualizer_uses_local_source_for_local_route
- test_remote_telemetry_disconnect_hard_fails_no_local_fallback
- test_brain_selector_is_pure_intent_complexity_no_resource_gate
- test_route_decision_consumes_contextualized_telemetry_only
"""
from __future__ import annotations

import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch
from typing import Any


from backend.core.telemetry_contextualizer import (
    TelemetryContextualizer,
    TelemetryRoute,
    TelemetryDisconnectError,
    LocalTelemetrySource,
    RemoteTelemetrySource,
    ResourceState,
)
from backend.core.ouroboros.governance.brain_selector import BrainSelector, BrainSelection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resource_state(**kwargs) -> ResourceState:
    defaults = dict(
        available_ram_gb=48.0,
        total_ram_gb=64.0,
        cpu_pressure=0.2,
        source="remote",
        endpoint="http://fake-jprime:8000",
    )
    defaults.update(kwargs)
    return ResourceState(**defaults)


def _make_local_resource_state(**kwargs) -> ResourceState:
    defaults = dict(
        available_ram_gb=6.0,
        total_ram_gb=16.0,
        cpu_pressure=0.3,
        source="local",
        endpoint=None,
    )
    defaults.update(kwargs)
    return ResourceState(**defaults)


# ---------------------------------------------------------------------------
# Gate 1.3 tests — TelemetryContextualizer
# ---------------------------------------------------------------------------


class TestTelemetryContextualizerUsesRemoteSourceForRemoteRoute:
    """test_telemetry_contextualizer_uses_remote_source_for_remote_route"""

    @pytest.mark.asyncio
    async def test_remote_route_calls_remote_source(self):
        remote_state = _make_resource_state()
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        remote_source.get_resource_state = AsyncMock(return_value=remote_state)
        local_source = MagicMock(spec=LocalTelemetrySource)
        local_source.get_resource_state = MagicMock()

        ctx = TelemetryContextualizer(
            local_source=local_source,
            remote_source=remote_source,
        )
        result = await ctx.get_resource_state(route=TelemetryRoute.REMOTE)

        remote_source.get_resource_state.assert_called_once()
        local_source.get_resource_state.assert_not_called()
        assert result.source == "remote"

    @pytest.mark.asyncio
    async def test_remote_route_result_has_remote_source_tag(self):
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        remote_source.get_resource_state = AsyncMock(
            return_value=_make_resource_state(source="remote")
        )
        local_source = MagicMock(spec=LocalTelemetrySource)
        ctx = TelemetryContextualizer(local_source=local_source, remote_source=remote_source)

        result = await ctx.get_resource_state(route=TelemetryRoute.REMOTE)
        assert result.source == "remote"


class TestTelemetryContextualizerUsesLocalSourceForLocalRoute:
    """test_telemetry_contextualizer_uses_local_source_for_local_route"""

    @pytest.mark.asyncio
    async def test_local_route_calls_local_source(self):
        local_state = _make_local_resource_state()
        local_source = MagicMock(spec=LocalTelemetrySource)
        local_source.get_resource_state = MagicMock(return_value=local_state)
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        remote_source.get_resource_state = AsyncMock()

        ctx = TelemetryContextualizer(local_source=local_source, remote_source=remote_source)
        result = await ctx.get_resource_state(route=TelemetryRoute.LOCAL)

        local_source.get_resource_state.assert_called_once()
        remote_source.get_resource_state.assert_not_called()
        assert result.source == "local"

    @pytest.mark.asyncio
    async def test_local_route_result_has_local_source_tag(self):
        local_source = MagicMock(spec=LocalTelemetrySource)
        local_source.get_resource_state = MagicMock(
            return_value=_make_local_resource_state(source="local")
        )
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        ctx = TelemetryContextualizer(local_source=local_source, remote_source=remote_source)

        result = await ctx.get_resource_state(route=TelemetryRoute.LOCAL)
        assert result.source == "local"


class TestRemoteTelemetryDisconnectHardFailsNoLocalFallback:
    """test_remote_telemetry_disconnect_hard_fails_no_local_fallback

    Critical constraint: remote route + remote unreachable = HARD FAIL.
    Must NOT silently fall back to local telemetry.
    """

    @pytest.mark.asyncio
    async def test_remote_disconnect_raises_telemetry_disconnect_error(self):
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        remote_source.get_resource_state = AsyncMock(
            side_effect=ConnectionError("J-Prime unreachable")
        )
        local_source = MagicMock(spec=LocalTelemetrySource)
        local_source.get_resource_state = MagicMock(return_value=_make_local_resource_state())

        ctx = TelemetryContextualizer(local_source=local_source, remote_source=remote_source)

        with pytest.raises(TelemetryDisconnectError) as exc_info:
            await ctx.get_resource_state(route=TelemetryRoute.REMOTE)

        assert "TELEMETRY_DISCONNECT" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_remote_timeout_raises_not_falls_back(self):
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        remote_source.get_resource_state = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        local_source = MagicMock(spec=LocalTelemetrySource)
        local_source.get_resource_state = MagicMock(return_value=_make_local_resource_state())

        ctx = TelemetryContextualizer(local_source=local_source, remote_source=remote_source)

        with pytest.raises(TelemetryDisconnectError):
            await ctx.get_resource_state(route=TelemetryRoute.REMOTE)

        # Local source must NOT have been consulted
        local_source.get_resource_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_error_carries_reason_code(self):
        remote_source = AsyncMock(spec=RemoteTelemetrySource)
        remote_source.get_resource_state = AsyncMock(
            side_effect=ConnectionError("refused")
        )
        local_source = MagicMock(spec=LocalTelemetrySource)
        ctx = TelemetryContextualizer(local_source=local_source, remote_source=remote_source)

        with pytest.raises(TelemetryDisconnectError) as exc_info:
            await ctx.get_resource_state(route=TelemetryRoute.REMOTE)

        err = exc_info.value
        assert hasattr(err, "reason_code")
        assert err.reason_code == "TELEMETRY_DISCONNECT"


class TestBrainSelectorIsPureIntentComplexityNoResourceGate:
    """test_brain_selector_is_pure_intent_complexity_no_resource_gate

    BrainSelector must NOT read psutil, MLX, or any resource telemetry.
    It must return only intent classification and complexity — no resource gates.
    """

    def test_brain_selector_select_does_not_import_psutil(self):
        """Inspect BrainSelector source: no psutil or mlx_lm references in the select path."""
        import inspect
        import backend.core.ouroboros.governance.brain_selector as bs_module
        source = inspect.getsource(bs_module)
        # psutil is the Mac-local RAM reader — must not appear in brain_selector
        assert "psutil" not in source, (
            "BrainSelector must not reference psutil — resource gating "
            "belongs to TelemetryContextualizer, not BrainSelector"
        )

    def test_brain_selector_select_does_not_call_psutil_at_runtime(self):
        """BrainSelector.select() must not trigger psutil calls."""
        with patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value = MagicMock(available=10 * 1024**3, total=16 * 1024**3)
            selector = BrainSelector.__new__(BrainSelector)
            # Minimal init — we only care about whether select() touches psutil
            try:
                selector._policy = MagicMock()
                selector._policy.select.return_value = BrainSelection(
                    brain_id="test_brain",
                    model_alias="test-model",
                    reason_code="test",
                    complexity="trivial",
                    intent_type="code_generation",
                )
                selector.select(
                    description="write a function",
                    intent_type="code_generation",
                    complexity="trivial",
                )
            except Exception:
                pass  # We only care that psutil was not called
            mock_vm.assert_not_called()

    def test_brain_selection_has_no_resource_fields(self):
        """BrainSelection dataclass must not carry ram_available or similar resource fields."""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(BrainSelection)}
        resource_fields = {"ram_available", "ram_threshold", "memory_pressure", "cpu_pressure"}
        overlap = fields & resource_fields
        assert not overlap, (
            f"BrainSelection must not contain resource fields: {overlap}. "
            "Resource decisions belong to RouteDecisionService."
        )

    def test_brain_selector_output_contains_intent_and_complexity(self):
        """BrainSelection must carry intent_type and complexity."""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(BrainSelection)}
        assert "intent_type" in fields
        assert "complexity" in fields


class TestRouteDecisionConsumesContextualizedTelemetryOnly:
    """test_route_decision_consumes_contextualized_telemetry_only

    RouteDecisionService must accept resource state only from TelemetryContextualizer.
    It must not call psutil or any local-resource API directly.
    """

    def test_route_decision_service_does_not_import_psutil(self):
        import inspect
        import backend.core.ouroboros.governance.route_decision_service as rds_module
        source = inspect.getsource(rds_module)
        assert "psutil" not in source, (
            "RouteDecisionService must not reference psutil directly — "
            "resource state must come from TelemetryContextualizer"
        )

    def test_route_decision_service_accepts_resource_state_param(self):
        """RouteDecisionService.decide() must accept a resource_state parameter."""
        from backend.core.ouroboros.governance.route_decision_service import RouteDecisionService
        sig = inspect.signature(RouteDecisionService.decide)
        params = set(sig.parameters.keys())
        assert "resource_state" in params, (
            "RouteDecisionService.decide() must accept resource_state so that "
            "the caller (orchestrator) passes contextualized telemetry"
        )

    @pytest.mark.asyncio
    async def test_route_decision_uses_provided_resource_state(self):
        """RouteDecisionService.decide() uses the passed resource_state, not its own reads."""
        from backend.core.ouroboros.governance.route_decision_service import RouteDecisionService

        remote_state = _make_resource_state(available_ram_gb=48.0, source="remote")
        svc = RouteDecisionService()

        with patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value = MagicMock(available=2 * 1024**3)
            try:
                svc.decide(
                    intent_type="code_generation",
                    complexity="heavy",
                    resource_state=remote_state,
                )
            except Exception:
                pass
            mock_vm.assert_not_called()
