"""Unit tests for ECAPA budget bridge core types and enums.

Disease 10 — ECAPA Budget Wiring, Task 1.
"""

from __future__ import annotations

import time

from backend.core.ecapa_budget_bridge import (
    BudgetToken,
    BudgetTokenState,
    EcapaBudgetRejection,
    ECAPA_CATEGORY_MAP,
)
from backend.core.startup_concurrency_budget import HeavyTaskCategory


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
