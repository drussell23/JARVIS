"""Unit tests for ECAPA budget bridge core types, enums, and token lifecycle.

Disease 10 — ECAPA Budget Wiring, Tasks 1–3.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from backend.core.ecapa_budget_bridge import (
    BudgetToken,
    BudgetTokenState,
    EcapaBudgetBridge,
    EcapaBudgetRejection,
    ECAPA_CATEGORY_MAP,
)
from backend.core.startup_budget_policy import StartupBudgetPolicy
from backend.core.startup_concurrency_budget import HeavyTaskCategory
from backend.core.startup_config import BudgetConfig, SoftGatePrecondition
from backend.core.startup_telemetry import StartupEventBus


@pytest.fixture
def budget_policy() -> StartupBudgetPolicy:
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
    return StartupBudgetPolicy(config)


class TestCoreTypes:
    """Core type tests for ECAPA budget bridge enums, mapping, and dataclass."""

    # 1. BudgetTokenState enum
    def test_budget_token_state_values(self) -> None:
        """All 5 states exist with correct string values."""
        assert BudgetTokenState.ACQUIRED == "acquired"
        assert BudgetTokenState.TRANSFERRED == "transferred"
        assert BudgetTokenState.REUSED == "reused"
        assert BudgetTokenState.RELEASED == "released"
        assert BudgetTokenState.EXPIRED == "expired"
        # Exactly 5 members
        assert len(BudgetTokenState) == 5

    # 2. EcapaBudgetRejection enum
    def test_rejection_reason_values(self) -> None:
        """All 6 rejection reasons exist."""
        assert EcapaBudgetRejection.PHASE_BLOCKED == "phase_blocked"
        assert EcapaBudgetRejection.MEMORY_UNSTABLE == "memory_unstable"
        assert EcapaBudgetRejection.BUDGET_TIMEOUT == "budget_timeout"
        assert EcapaBudgetRejection.SLOT_UNAVAILABLE == "slot_unavailable"
        assert EcapaBudgetRejection.THRASH_EMERGENCY == "thrash_emergency"
        assert EcapaBudgetRejection.CONTRACT_MISMATCH == "contract_mismatch"
        # Exactly 6 members
        assert len(EcapaBudgetRejection) == 6

    # 3. ECAPA_CATEGORY_MAP — probe
    def test_category_mapping_probe(self) -> None:
        """'probe' maps to ML_INIT."""
        assert ECAPA_CATEGORY_MAP["probe"] is HeavyTaskCategory.ML_INIT
        assert len(ECAPA_CATEGORY_MAP) == 2

    # 4. ECAPA_CATEGORY_MAP — model_load
    def test_category_mapping_model_load(self) -> None:
        """'model_load' maps to MODEL_LOAD."""
        assert ECAPA_CATEGORY_MAP["model_load"] is HeavyTaskCategory.MODEL_LOAD

    # 5. BudgetToken creation
    def test_budget_token_creation(self) -> None:
        """BudgetToken created with required fields, optionals are None."""
        token = BudgetToken(
            token_id="test-123",
            owner_session_id="session-abc",
            state=BudgetTokenState.ACQUIRED,
            category=HeavyTaskCategory.ML_INIT,
            acquired_at=time.monotonic(),
        )

        assert token.owner_session_id == "session-abc"
        assert token.state is BudgetTokenState.ACQUIRED
        assert token.category is HeavyTaskCategory.ML_INIT
        # Optional fields default to None
        assert token.transferred_at is None
        assert token.released_at is None
        assert token.last_heartbeat_at is None
        assert token.rejection_reason is None
        assert token.probe_failure_reason is None

    # 6. BudgetToken mutability
    def test_budget_token_is_mutable(self) -> None:
        """State can be changed after creation."""
        token = BudgetToken(
            token_id="test-456",
            owner_session_id="session-mut",
            state=BudgetTokenState.ACQUIRED,
            category=HeavyTaskCategory.MODEL_LOAD,
            acquired_at=time.monotonic(),
        )

        # Mutate state
        token.state = BudgetTokenState.TRANSFERRED
        assert token.state is BudgetTokenState.TRANSFERRED

        # Mutate optional timestamp
        now = time.monotonic()
        token.transferred_at = now
        assert token.transferred_at == now

    # 7. BudgetToken default TTL
    def test_budget_token_default_ttl(self) -> None:
        """Default token_ttl_s is 120.0."""
        token = BudgetToken(
            token_id="test-789",
            owner_session_id="session-ttl",
            state=BudgetTokenState.ACQUIRED,
            category=HeavyTaskCategory.ML_INIT,
            acquired_at=time.monotonic(),
        )

        assert token.token_ttl_s == 120.0


class TestTokenLifecycle:
    """Token lifecycle tests for EcapaBudgetBridge (Task 2)."""

    def _make_bridge(self) -> EcapaBudgetBridge:
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        return bridge

    def _make_token(
        self,
        bridge: EcapaBudgetBridge,
        category: HeavyTaskCategory = HeavyTaskCategory.MODEL_LOAD,
        session_id: str = "test-session",
    ) -> BudgetToken:
        token = BudgetToken(
            token_id="tok-" + str(id(bridge))[-4:],
            owner_session_id=session_id,
            state=BudgetTokenState.ACQUIRED,
            category=category,
            acquired_at=time.monotonic(),
            last_heartbeat_at=time.monotonic(),
        )
        bridge._tokens[token.token_id] = token
        if category is HeavyTaskCategory.MODEL_LOAD:
            bridge._active_model_load_count += 1
        return token

    # 1. transfer CAS success
    def test_transfer_cas_success(self) -> None:
        """ACQUIRED -> TRANSFERRED succeeds; transferred_at is set."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        result = bridge.transfer_token(token)
        assert result.state is BudgetTokenState.TRANSFERRED
        assert result.transferred_at is not None

    # 2. transfer CAS double fails
    def test_transfer_cas_double_fails(self) -> None:
        """Second transfer_token raises ValueError mentioning CAS."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        bridge.transfer_token(token)
        with pytest.raises(ValueError, match="CAS"):
            bridge.transfer_token(token)

    # 3. reuse from transferred
    def test_reuse_from_transferred(self) -> None:
        """TRANSFERRED -> REUSED with matching session_id."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="owner-1")
        bridge.transfer_token(token)
        result = bridge.reuse_token(token, "owner-1")
        assert result.state is BudgetTokenState.REUSED
        assert result.last_heartbeat_at is not None

    # 4. reuse wrong owner rejected
    def test_reuse_wrong_owner_rejected(self) -> None:
        """Wrong session_id raises ValueError mentioning owner."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="owner-1")
        bridge.transfer_token(token)
        with pytest.raises(ValueError, match="owner"):
            bridge.reuse_token(token, "wrong-session")

    # 4b. reuse from wrong state (ACQUIRED) rejected
    def test_reuse_from_acquired_rejected(self) -> None:
        """reuse_token from ACQUIRED raises ValueError mentioning CAS."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="owner-1")
        # Token is still ACQUIRED — not transferred
        with pytest.raises(ValueError, match="CAS"):
            bridge.reuse_token(token, "owner-1")

    # 5. heartbeat updates timestamp
    def test_heartbeat_updates_timestamp(self) -> None:
        """heartbeat() updates last_heartbeat_at."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        old_hb = token.last_heartbeat_at
        time.sleep(0.01)
        bridge.heartbeat(token)
        assert token.last_heartbeat_at is not None
        assert token.last_heartbeat_at > old_hb

    # 6. release idempotent
    def test_release_idempotent(self) -> None:
        """Double release is safe, stays RELEASED."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        bridge.release(token)
        assert token.state is BudgetTokenState.RELEASED
        # Second release — no error
        bridge.release(token)
        assert token.state is BudgetTokenState.RELEASED

    # 7. release decrements model load count
    def test_release_decrements_model_load_count(self) -> None:
        """Releasing a MODEL_LOAD token decrements _active_model_load_count."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, category=HeavyTaskCategory.MODEL_LOAD)
        assert bridge._active_model_load_count == 1
        bridge.release(token)
        assert bridge._active_model_load_count == 0

    # 8. release no double decrement
    def test_release_no_double_decrement(self) -> None:
        """Double release of MODEL_LOAD doesn't go below 0."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, category=HeavyTaskCategory.MODEL_LOAD)
        bridge.release(token)
        bridge.release(token)
        assert bridge._active_model_load_count == 0

    # 9. cleanup expires stale acquired
    def test_cleanup_expires_stale_acquired(self) -> None:
        """ACQUIRED past TTL with no heartbeat -> EXPIRED."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        # Backdate acquired_at and clear heartbeat to simulate staleness
        token.acquired_at = time.monotonic() - 200.0
        token.last_heartbeat_at = None
        count = bridge.cleanup_expired(max_age_s=120.0)
        assert count == 1
        assert token.state is BudgetTokenState.EXPIRED

    # 10. cleanup preserves active reused
    def test_cleanup_preserves_active_reused(self) -> None:
        """REUSED with fresh heartbeat is NOT expired."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="owner-x")
        bridge.transfer_token(token)
        bridge.reuse_token(token, "owner-x")
        # Fresh heartbeat
        bridge.heartbeat(token)
        count = bridge.cleanup_expired(heartbeat_silence_s=45.0)
        assert count == 0
        assert token.state is BudgetTokenState.REUSED

    # 11. cleanup expires stale reused
    def test_cleanup_expires_stale_reused(self) -> None:
        """REUSED with stale heartbeat (>45s) -> EXPIRED."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="owner-y")
        bridge.transfer_token(token)
        bridge.reuse_token(token, "owner-y")
        # Backdate heartbeat to simulate staleness
        token.last_heartbeat_at = time.monotonic() - 60.0
        count = bridge.cleanup_expired(heartbeat_silence_s=45.0)
        assert count == 1
        assert token.state is BudgetTokenState.EXPIRED

    # 12. invariant freeze on model load exceed
    def test_invariant_freeze_on_model_load_exceed(self) -> None:
        """_active_model_load_count > 1 sets _frozen = True."""
        bridge = self._make_bridge()
        bridge._active_model_load_count = 2
        bridge._check_invariant()
        assert bridge._frozen is True

    # 13. release ML_INIT no count change
    def test_release_ml_init_no_count_change(self) -> None:
        """Releasing an ML_INIT token doesn't touch _active_model_load_count."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, category=HeavyTaskCategory.ML_INIT)
        assert bridge._active_model_load_count == 0
        bridge.release(token)
        assert bridge._active_model_load_count == 0


class TestBudgetSlotAcquisition:
    """Budget slot acquisition tests for EcapaBudgetBridge (Task 3)."""

    def _make_bridge(self) -> EcapaBudgetBridge:
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        return bridge

    # 1. acquire probe slot success
    @pytest.mark.asyncio
    async def test_acquire_probe_slot_success(
        self, budget_policy: StartupBudgetPolicy
    ) -> None:
        """Signal CORE_READY, acquire probe succeeds, token is ML_INIT, release works."""
        bridge = self._make_bridge()
        bridge.set_budget_policy(budget_policy)
        budget_policy.signal_phase_reached("CORE_READY")

        result = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(result, BudgetToken)
        assert result.category is HeavyTaskCategory.ML_INIT
        assert result.state is BudgetTokenState.ACQUIRED
        assert result.token_id in bridge._tokens

        # Release and verify cleanup
        bridge.release(result)
        assert result.state is BudgetTokenState.RELEASED
        # Allow the fire-and-forget __aexit__ task to run
        await asyncio.sleep(0.05)

    # 2. acquire probe slot phase blocked
    @pytest.mark.asyncio
    async def test_acquire_probe_slot_phase_blocked(
        self, budget_policy: StartupBudgetPolicy
    ) -> None:
        """Do NOT signal CORE_READY, acquire returns PHASE_BLOCKED."""
        bridge = self._make_bridge()
        bridge.set_budget_policy(budget_policy)
        # Deliberately NOT signalling CORE_READY

        result = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(result, EcapaBudgetRejection)
        assert result is EcapaBudgetRejection.PHASE_BLOCKED

    # 3. acquire model slot success
    @pytest.mark.asyncio
    async def test_acquire_model_slot_success(
        self, budget_policy: StartupBudgetPolicy
    ) -> None:
        """Acquire MODEL_LOAD succeeds, count=1, release brings it to 0."""
        bridge = self._make_bridge()
        bridge.set_budget_policy(budget_policy)

        result = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(result, BudgetToken)
        assert result.category is HeavyTaskCategory.MODEL_LOAD
        assert bridge._active_model_load_count == 1

        bridge.release(result)
        assert bridge._active_model_load_count == 0
        await asyncio.sleep(0.05)

    # 4. acquire model slot frozen rejected
    @pytest.mark.asyncio
    async def test_acquire_model_slot_frozen_rejected(
        self, budget_policy: StartupBudgetPolicy
    ) -> None:
        """Set _frozen=True, acquire returns SLOT_UNAVAILABLE."""
        bridge = self._make_bridge()
        bridge.set_budget_policy(budget_policy)
        bridge._frozen = True

        result = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(result, EcapaBudgetRejection)
        assert result is EcapaBudgetRejection.SLOT_UNAVAILABLE

    # 5. acquire model slot timeout
    @pytest.mark.asyncio
    async def test_acquire_model_slot_timeout(
        self, budget_policy: StartupBudgetPolicy
    ) -> None:
        """Hold first MODEL_LOAD slot, second times out, release first, third succeeds."""
        bridge = self._make_bridge()
        bridge.set_budget_policy(budget_policy)

        # Acquire the first MODEL_LOAD slot (hard-gate: max 1 concurrent)
        first = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(first, BudgetToken)

        # Second acquire should time out (only 1 hard slot available)
        second = await bridge.acquire_model_slot(timeout_s=0.5)
        assert isinstance(second, EcapaBudgetRejection)
        assert second is EcapaBudgetRejection.BUDGET_TIMEOUT

        # Release the first slot
        bridge.release(first)
        await asyncio.sleep(0.05)

        # Third acquire should succeed now
        third = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(third, BudgetToken)
        assert third.category is HeavyTaskCategory.MODEL_LOAD

        bridge.release(third)
        await asyncio.sleep(0.05)


class TestTelemetryEmission:
    """Every bridge action emits a canonical event to the bus."""

    @pytest.mark.asyncio
    async def test_acquire_emits_events(self, budget_policy):
        """acquire_probe_slot emits acquire_attempt + acquire_granted."""
        bus = StartupEventBus(trace_id="test-trace")
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy
        bridge._event_bus = bus
        budget_policy.signal_phase_reached("CORE_READY")

        token = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(token, BudgetToken)

        event_types = [e.event_type for e in bus.event_history]
        assert "ecapa_budget.acquire_attempt" in event_types
        assert "ecapa_budget.acquire_granted" in event_types

        bridge.release(token)

    @pytest.mark.asyncio
    async def test_denied_emits_denied_event(self, budget_policy):
        """Denied acquisition emits acquire_attempt + acquire_denied."""
        bus = StartupEventBus(trace_id="test-trace")
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy
        bridge._event_bus = bus
        # Do NOT signal CORE_READY — probe will be phase-blocked

        result = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert result is EcapaBudgetRejection.PHASE_BLOCKED

        event_types = [e.event_type for e in bus.event_history]
        assert "ecapa_budget.acquire_attempt" in event_types
        assert "ecapa_budget.acquire_denied" in event_types
        # Check denial detail contains reason
        denied_events = [e for e in bus.event_history if e.event_type == "ecapa_budget.acquire_denied"]
        assert denied_events[0].detail["reason"] == "phase_blocked"

    @pytest.mark.asyncio
    async def test_release_emits_event(self, budget_policy):
        """release() emits ecapa_budget.release event."""
        bus = StartupEventBus(trace_id="test-trace")
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy
        bridge._event_bus = bus
        budget_policy.signal_phase_reached("CORE_READY")

        token = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert isinstance(token, BudgetToken)
        bridge.release(token)

        event_types = [e.event_type for e in bus.event_history]
        assert "ecapa_budget.release" in event_types

    def test_transfer_emits_event(self):
        """transfer_token emits ecapa_budget.transfer event."""
        bus = StartupEventBus(trace_id="test-trace")
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._event_bus = bus

        token = BudgetToken(
            token_id="tel-test",
            owner_session_id="s1",
            state=BudgetTokenState.ACQUIRED,
            category=HeavyTaskCategory.MODEL_LOAD,
            acquired_at=time.monotonic(),
        )
        bridge._tokens[token.token_id] = token
        bridge._active_model_load_count = 1
        bridge.transfer_token(token)

        event_types = [e.event_type for e in bus.event_history]
        assert "ecapa_budget.transfer" in event_types
