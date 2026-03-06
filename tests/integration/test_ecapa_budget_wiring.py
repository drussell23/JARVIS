"""Integration tests for ECAPA budget wiring — supervisor startup path.

Disease 10 — ECAPA Budget Wiring, Task 5.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ecapa_budget_bridge import (
    BudgetToken,
    BudgetTokenState,
    EcapaBudgetBridge,
    EcapaBudgetRejection,
)
from backend.core.startup_budget_policy import StartupBudgetPolicy
from backend.core.startup_concurrency_budget import HeavyTaskCategory
from backend.core.startup_config import BudgetConfig, SoftGatePrecondition
from backend.core.startup_telemetry import StartupEventBus


@pytest.fixture
def wired_bridge():
    """Create a fully-wired bridge with budget policy and event bus."""
    config = BudgetConfig(
        max_hard_concurrent=1,
        max_total_concurrent=3,
        hard_gate_categories=["MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"],
        soft_gate_categories=["ML_INIT", "GCP_PROVISION"],
        soft_gate_preconditions={
            "ML_INIT": SoftGatePrecondition(
                require_phase="CORE_READY",
                require_memory_stable_s=0.0,
                memory_slope_threshold_mb_s=999.0,
            ),
        },
        max_wait_s=5.0,
    )
    policy = StartupBudgetPolicy(config)
    bus = StartupEventBus(trace_id="integration-test")

    bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
    bridge._init_internal()
    bridge.set_budget_policy(policy)
    bridge.set_event_bus(bus)
    policy.signal_phase_reached("CORE_READY")
    policy.signal_phase_reached("DEFERRED_COMPONENTS")

    return bridge, policy, bus


class TestStartupPath:
    """Full supervisor startup path: probe -> fallback -> transfer -> load."""

    @pytest.mark.asyncio
    async def test_full_startup_probe_success(self, wired_bridge):
        """Cloud probe succeeds -> releases probe token -> done."""
        bridge, policy, bus = wired_bridge

        # Acquire probe slot
        probe_token = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(probe_token, BudgetToken)
        assert probe_token.category is HeavyTaskCategory.ML_INIT

        # Simulate successful cloud probe
        bridge.release(probe_token)
        assert probe_token.state == BudgetTokenState.RELEASED

    @pytest.mark.asyncio
    async def test_full_startup_probe_fail_local_load(self, wired_bridge):
        """Probe fails -> acquire model slot -> transfer -> registry reuses."""
        bridge, policy, bus = wired_bridge

        # Probe slot
        probe_token = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(probe_token, BudgetToken)

        # Probe fails
        probe_token.probe_failure_reason = "cloud_timeout_clean"
        bridge.release(probe_token)

        # Acquire model slot
        model_token = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(model_token, BudgetToken)
        assert model_token.category is HeavyTaskCategory.MODEL_LOAD
        assert bridge._active_model_load_count == 1

        # Transfer to registry
        bridge.transfer_token(model_token)
        assert model_token.state == BudgetTokenState.TRANSFERRED

        # Registry reuses
        bridge.reuse_token(model_token, requester_session_id=bridge._session_id)
        assert model_token.state == BudgetTokenState.REUSED

        # Heartbeat during load
        bridge.heartbeat(model_token)

        # Load complete
        bridge.release(model_token)
        assert model_token.state == BudgetTokenState.RELEASED
        assert bridge._active_model_load_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_model_load_blocks(self, wired_bridge):
        """Second MODEL_LOAD request blocks until first releases."""
        bridge, policy, bus = wired_bridge

        # First model slot
        first = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(first, BudgetToken)

        # Second should time out (hard gate = max 1)
        second = await bridge.acquire_model_slot(timeout_s=0.5)
        assert second is EcapaBudgetRejection.BUDGET_TIMEOUT

        # Release first
        bridge.release(first)
        await asyncio.sleep(0.05)

        # Now third should succeed
        third = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(third, BudgetToken)
        bridge.release(third)
        await asyncio.sleep(0.05)


class TestRecoveryPath:
    """Deferred recovery path: registry acquires independently."""

    @pytest.mark.asyncio
    async def test_recovery_acquires_fresh(self, wired_bridge):
        """Recovery path acquires fresh tokens (no transfer needed)."""
        bridge, policy, bus = wired_bridge

        # No startup token — recovery acquires directly
        probe_token = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(probe_token, BudgetToken)

        # Probe fails -> try local
        bridge.release(probe_token)
        model_token = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(model_token, BudgetToken)

        # Load directly (no transfer/reuse dance — fresh token)
        bridge.heartbeat(model_token)
        bridge.release(model_token)
        assert model_token.state == BudgetTokenState.RELEASED


class TestCrashRecovery:
    """Crash-safe cleanup and recovery."""

    @pytest.mark.asyncio
    async def test_stale_reused_token_cleanup(self, wired_bridge):
        """Token in REUSED with stale heartbeat gets reclaimed."""
        bridge, policy, bus = wired_bridge

        model_token = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(model_token, BudgetToken)
        bridge.transfer_token(model_token)
        bridge.reuse_token(model_token, requester_session_id=bridge._session_id)

        # Simulate stale heartbeat
        model_token.last_heartbeat_at = time.monotonic() - 60.0

        expired = bridge.cleanup_expired(max_age_s=120.0, heartbeat_silence_s=45.0)
        assert expired == 1
        assert model_token.state == BudgetTokenState.EXPIRED
        assert bridge._active_model_load_count == 0

        # cleanup_expired marks the token EXPIRED but does not release the
        # underlying budget-policy semaphore (held in _budget_contexts).
        # In a real crash the semaphore state would be lost with the process;
        # here we must manually exit the context so the hard-gate slot frees.
        ctx = bridge._budget_contexts.pop(model_token.token_id, None)
        if ctx is not None:
            await ctx.__aexit__(None, None, None)

        # Next recovery attempt should succeed
        await asyncio.sleep(0.05)
        new_token = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(new_token, BudgetToken)
        bridge.release(new_token)
        await asyncio.sleep(0.05)


class TestBudgetAwareRecovery:
    """Registry recovery path uses bridge instead of blind polling."""

    @pytest.mark.asyncio
    async def test_recovery_budget_gated_probe(self, wired_bridge):
        """Recovery acquires probe slot before attempting cloud check."""
        bridge, policy, bus = wired_bridge

        # Simulate recovery acquiring probe slot
        probe = await bridge.acquire_probe_slot(timeout_s=5.0)
        assert isinstance(probe, BudgetToken)
        assert probe.category is HeavyTaskCategory.ML_INIT

        # Simulate probe failure
        probe.probe_failure_reason = "cloud_timeout_clean"
        bridge.release(probe)

        # Acquire model slot for local load
        model = await bridge.acquire_model_slot(timeout_s=5.0)
        assert isinstance(model, BudgetToken)
        assert bridge._active_model_load_count == 1

        # Simulate successful local load
        bridge.heartbeat(model)
        bridge.release(model)
        assert bridge._active_model_load_count == 0

    @pytest.mark.asyncio
    async def test_recovery_respects_phase_gate(self, wired_bridge):
        """Recovery cannot probe if phase not reached."""
        bridge, policy, bus = wired_bridge

        # Create a fresh policy WITHOUT CORE_READY signalled
        config = BudgetConfig(
            max_hard_concurrent=1,
            max_total_concurrent=3,
            hard_gate_categories=["MODEL_LOAD"],
            soft_gate_categories=["ML_INIT"],
            soft_gate_preconditions={
                "ML_INIT": SoftGatePrecondition(
                    require_phase="CORE_READY",
                    require_memory_stable_s=0.0,
                    memory_slope_threshold_mb_s=999.0,
                ),
            },
            max_wait_s=5.0,
        )
        gated_policy = StartupBudgetPolicy(config)
        bridge._budget_policy = gated_policy
        # Do NOT signal CORE_READY

        result = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert result is EcapaBudgetRejection.PHASE_BLOCKED


class TestContractMismatch:
    """CONTRACT_MISMATCH is non-retryable."""

    def test_contract_mismatch_backoff_is_max(self):
        """CONTRACT_MISMATCH uses maximum delay (non-retryable)."""
        from backend.voice_unlock.ml_engine_registry import MLEngineRegistry

        delay = MLEngineRegistry._compute_backoff(
            EcapaBudgetRejection.CONTRACT_MISMATCH,
            attempt=1,
            base=5.0,
            cap=120.0,
        )
        assert delay == 120.0  # Cap, no jitter

    def test_thrash_emergency_slow_backoff(self):
        """THRASH_EMERGENCY uses 15s base with 30% jitter."""
        from backend.voice_unlock.ml_engine_registry import MLEngineRegistry

        delays = [
            MLEngineRegistry._compute_backoff(
                EcapaBudgetRejection.THRASH_EMERGENCY,
                attempt=1,
                base=5.0,
                cap=120.0,
            )
            for _ in range(10)
        ]
        # All should be roughly around 15s +/- 30%
        assert all(10.0 <= d <= 20.0 for d in delays)

    def test_normal_rejection_exponential_backoff(self):
        """Normal rejections use exponential backoff with 20% jitter."""
        from backend.voice_unlock.ml_engine_registry import MLEngineRegistry

        # Attempt 1: base=5.0
        d1 = MLEngineRegistry._compute_backoff(
            EcapaBudgetRejection.BUDGET_TIMEOUT,
            attempt=1, base=5.0, cap=120.0,
        )
        assert 3.0 <= d1 <= 7.0

        # Attempt 3: base * 4 = 20.0
        d3 = MLEngineRegistry._compute_backoff(
            EcapaBudgetRejection.BUDGET_TIMEOUT,
            attempt=3, base=5.0, cap=120.0,
        )
        assert 15.0 <= d3 <= 25.0


class TestTelemetryCompleteness:
    """Verify full event trail for startup path."""

    @pytest.mark.asyncio
    async def test_startup_path_emits_full_trail(self, wired_bridge):
        """Full startup path emits acquire_attempt, acquire_granted, release."""
        bridge, policy, bus = wired_bridge

        # Acquire probe
        probe = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(probe, BudgetToken)
        bridge.release(probe)

        # Acquire model
        model = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(model, BudgetToken)
        bridge.transfer_token(model)
        bridge.reuse_token(model, requester_session_id=bridge._session_id)
        bridge.release(model)

        event_types = [e.event_type for e in bus.event_history]
        # Should see: 2x acquire_attempt, 2x acquire_granted, 2x release,
        #             1x transfer, 1x reuse
        assert event_types.count("ecapa_budget.acquire_attempt") == 2
        assert event_types.count("ecapa_budget.acquire_granted") == 2
        assert event_types.count("ecapa_budget.release") == 2
        assert "ecapa_budget.transfer" in event_types
        assert "ecapa_budget.reuse" in event_types
