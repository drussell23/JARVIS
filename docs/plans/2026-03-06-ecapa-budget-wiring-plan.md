# ECAPA Budget Wiring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire ECAPA voice unlock into the Disease 10 startup sequencing system via a Budget Token Bridge so voice unlock participates in budget/phase gates instead of hitting memory walls.

**Architecture:** New `ecapa_budget_bridge.py` singleton mediates all ECAPA budget interactions. Supervisor acquires slots and transfers tokens to MLEngineRegistry on startup path. Registry acquires independently on recovery path. Both paths use the same tiered budget (ML_INIT for cloud probe, MODEL_LOAD for local load).

**Tech Stack:** Python 3.9+, asyncio, existing Disease 10 modules (`StartupBudgetPolicy`, `StartupEventBus`, `HeavyTaskCategory`), pytest + pytest-asyncio for testing.

**Design doc:** `docs/plans/2026-03-06-ecapa-budget-wiring-design.md`

---

## Task 1: Core Types and Enums

**Files:**
- Create: `backend/core/ecapa_budget_bridge.py`
- Test: `tests/unit/core/test_ecapa_budget_bridge.py`

**Context:** This task creates the foundational types — enums, dataclass, and the category mapping constant. No bridge logic yet; just types that everything else builds on.

**Reference files to read first:**
- `backend/core/startup_concurrency_budget.py` — for `HeavyTaskCategory` enum (used by `BudgetToken.category`)
- `backend/core/startup_telemetry.py` — for `StartupEvent` and `StartupEventBus` (used later by bridge)
- `backend/core/startup_budget_policy.py` — for `StartupBudgetPolicy` API (used later by bridge)

**Step 1: Write the failing tests**

Create `tests/unit/core/test_ecapa_budget_bridge.py`:

```python
"""Unit tests for EcapaBudgetBridge core types and lifecycle."""

from __future__ import annotations

import time
import pytest

from backend.core.ecapa_budget_bridge import (
    BudgetToken,
    BudgetTokenState,
    EcapaBudgetRejection,
    ECAPA_CATEGORY_MAP,
)
from backend.core.startup_concurrency_budget import HeavyTaskCategory


class TestCoreTypes:
    """Verify enums, dataclass, and category mapping."""

    def test_budget_token_state_values(self):
        """All five lifecycle states exist with string values."""
        assert BudgetTokenState.ACQUIRED.value == "acquired"
        assert BudgetTokenState.TRANSFERRED.value == "transferred"
        assert BudgetTokenState.REUSED.value == "reused"
        assert BudgetTokenState.RELEASED.value == "released"
        assert BudgetTokenState.EXPIRED.value == "expired"

    def test_rejection_reason_values(self):
        """All six rejection reasons exist."""
        assert EcapaBudgetRejection.PHASE_BLOCKED.value == "phase_blocked"
        assert EcapaBudgetRejection.MEMORY_UNSTABLE.value == "memory_unstable"
        assert EcapaBudgetRejection.BUDGET_TIMEOUT.value == "budget_timeout"
        assert EcapaBudgetRejection.SLOT_UNAVAILABLE.value == "slot_unavailable"
        assert EcapaBudgetRejection.THRASH_EMERGENCY.value == "thrash_emergency"
        assert EcapaBudgetRejection.CONTRACT_MISMATCH.value == "contract_mismatch"

    def test_category_mapping_probe(self):
        """Probe maps to ML_INIT (lightweight soft gate)."""
        assert ECAPA_CATEGORY_MAP["probe"] is HeavyTaskCategory.ML_INIT

    def test_category_mapping_model_load(self):
        """Model load maps to MODEL_LOAD (heavyweight hard gate)."""
        assert ECAPA_CATEGORY_MAP["model_load"] is HeavyTaskCategory.MODEL_LOAD

    def test_budget_token_creation(self):
        """BudgetToken can be created with required fields."""
        token = BudgetToken(
            token_id="test-123",
            owner_session_id="session-abc",
            state=BudgetTokenState.ACQUIRED,
            category=HeavyTaskCategory.ML_INIT,
            acquired_at=time.monotonic(),
        )
        assert token.state == BudgetTokenState.ACQUIRED
        assert token.transferred_at is None
        assert token.released_at is None
        assert token.last_heartbeat_at is None
        assert token.token_ttl_s == 120.0
        assert token.rejection_reason is None
        assert token.probe_failure_reason is None

    def test_budget_token_is_mutable(self):
        """BudgetToken state can be mutated (not frozen)."""
        token = BudgetToken(
            token_id="test-456",
            owner_session_id="session-def",
            state=BudgetTokenState.ACQUIRED,
            category=HeavyTaskCategory.MODEL_LOAD,
            acquired_at=time.monotonic(),
        )
        token.state = BudgetTokenState.TRANSFERRED
        token.transferred_at = time.monotonic()
        assert token.state == BudgetTokenState.TRANSFERRED
        assert token.transferred_at is not None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ecapa_budget_bridge'`

**Step 3: Write minimal implementation**

Create `backend/core/ecapa_budget_bridge.py`:

```python
"""EcapaBudgetBridge — shared coordinator for ECAPA budget/phase integration.

Wires ECAPA voice unlock into the Disease 10 startup sequencing system.
Provides a Budget Token Bridge that mediates all ECAPA budget interactions
between the supervisor (startup path) and MLEngineRegistry (recovery path).

Both layers use one shared bridge instance with deterministic token lifecycle:
ACQUIRED -> TRANSFERRED -> REUSED -> RELEASED (or EXPIRED on crash).
"""

from __future__ import annotations

import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from backend.core.startup_concurrency_budget import HeavyTaskCategory

__all__ = [
    "BudgetTokenState",
    "EcapaBudgetRejection",
    "BudgetToken",
    "ECAPA_CATEGORY_MAP",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BudgetTokenState(str, enum.Enum):
    """Lifecycle states of a budget token."""

    ACQUIRED = "acquired"
    TRANSFERRED = "transferred"
    REUSED = "reused"
    RELEASED = "released"
    EXPIRED = "expired"


class EcapaBudgetRejection(str, enum.Enum):
    """Reason codes for denied budget acquisitions."""

    PHASE_BLOCKED = "phase_blocked"
    MEMORY_UNSTABLE = "memory_unstable"
    BUDGET_TIMEOUT = "budget_timeout"
    SLOT_UNAVAILABLE = "slot_unavailable"
    THRASH_EMERGENCY = "thrash_emergency"
    CONTRACT_MISMATCH = "contract_mismatch"


# ---------------------------------------------------------------------------
# Category mapping — single source of truth
# ---------------------------------------------------------------------------

ECAPA_CATEGORY_MAP = {
    "probe": HeavyTaskCategory.ML_INIT,
    "model_load": HeavyTaskCategory.MODEL_LOAD,
}


# ---------------------------------------------------------------------------
# Budget Token dataclass
# ---------------------------------------------------------------------------


@dataclass
class BudgetToken:
    """Tracks ownership of a budget slot through its lifecycle.

    Attributes
    ----------
    token_id:
        Unique identifier (uuid4).
    owner_session_id:
        Identifies the owner for crash-safe cleanup disambiguation.
    state:
        Current lifecycle state.
    category:
        Which HeavyTaskCategory this token holds a slot for.
    acquired_at:
        Monotonic timestamp when the slot was acquired.
    transferred_at:
        When the token was transferred from supervisor to registry.
    released_at:
        When the slot was returned to the pool.
    last_heartbeat_at:
        Last heartbeat from the token holder (for TTL enforcement).
    token_ttl_s:
        Maximum seconds the token may be held before auto-expiry.
    rejection_reason:
        Set when acquisition is denied (None on success).
    probe_failure_reason:
        Persisted probe failure context for recovery path selection.
    """

    token_id: str
    owner_session_id: str
    state: BudgetTokenState
    category: HeavyTaskCategory
    acquired_at: float
    transferred_at: Optional[float] = None
    released_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None
    token_ttl_s: float = 120.0
    rejection_reason: Optional[EcapaBudgetRejection] = None
    probe_failure_reason: Optional[str] = None
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: 7 PASSED

**Step 5: Commit**

```bash
git add backend/core/ecapa_budget_bridge.py tests/unit/core/test_ecapa_budget_bridge.py
git commit -m "feat(disease10): add ECAPA budget bridge core types and enums"
```

---

## Task 2: Bridge Singleton — Token Lifecycle Methods

**Files:**
- Modify: `backend/core/ecapa_budget_bridge.py`
- Test: `tests/unit/core/test_ecapa_budget_bridge.py`

**Context:** Add the `EcapaBudgetBridge` singleton class with token lifecycle methods: `transfer_token()` (CAS), `reuse_token()` (ownership-verified), `heartbeat()`, `release()` (idempotent), and `cleanup_expired()`. These methods manage token state transitions but do NOT yet acquire budget slots (that's Task 3). This task isolates the token state machine from budget integration.

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_ecapa_budget_bridge.py`:

```python
from backend.core.ecapa_budget_bridge import EcapaBudgetBridge


class TestTokenLifecycle:
    """Token state transitions: transfer, reuse, heartbeat, release, cleanup."""

    def _make_bridge(self) -> EcapaBudgetBridge:
        """Create a fresh bridge instance (not singleton) for test isolation."""
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        return bridge

    def _make_token(
        self,
        bridge: EcapaBudgetBridge,
        category: HeavyTaskCategory = HeavyTaskCategory.MODEL_LOAD,
        session_id: str = "test-session",
    ) -> BudgetToken:
        """Create an ACQUIRED token registered with the bridge."""
        token = BudgetToken(
            token_id=str(uuid.uuid4()),
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

    def test_transfer_cas_success(self):
        """transfer_token transitions ACQUIRED -> TRANSFERRED exactly once."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        result = bridge.transfer_token(token)
        assert result.state == BudgetTokenState.TRANSFERRED
        assert result.transferred_at is not None

    def test_transfer_cas_double_fails(self):
        """Second transfer_token call raises ValueError (CAS enforcement)."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        bridge.transfer_token(token)
        with pytest.raises(ValueError, match="CAS"):
            bridge.transfer_token(token)

    def test_reuse_from_transferred(self):
        """reuse_token transitions TRANSFERRED -> REUSED with ownership check."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="supervisor")
        bridge.transfer_token(token)
        result = bridge.reuse_token(token, requester_session_id="supervisor")
        assert result.state == BudgetTokenState.REUSED

    def test_reuse_wrong_owner_rejected(self):
        """reuse_token rejects when requester_session_id doesn't match."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="supervisor")
        bridge.transfer_token(token)
        with pytest.raises(ValueError, match="owner"):
            bridge.reuse_token(token, requester_session_id="intruder")

    def test_heartbeat_updates_timestamp(self):
        """heartbeat() updates last_heartbeat_at."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        old_hb = token.last_heartbeat_at
        time.sleep(0.01)
        bridge.heartbeat(token)
        assert token.last_heartbeat_at > old_hb

    def test_release_idempotent(self):
        """release() is safe to call twice (idempotent)."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        bridge.release(token)
        assert token.state == BudgetTokenState.RELEASED
        assert token.released_at is not None
        # Second call is a no-op
        bridge.release(token)
        assert token.state == BudgetTokenState.RELEASED

    def test_release_decrements_model_load_count(self):
        """release() decrements _active_model_load_count for MODEL_LOAD tokens."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, category=HeavyTaskCategory.MODEL_LOAD)
        assert bridge._active_model_load_count == 1
        bridge.release(token)
        assert bridge._active_model_load_count == 0

    def test_release_no_double_decrement(self):
        """Double release doesn't decrement count below zero."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, category=HeavyTaskCategory.MODEL_LOAD)
        bridge.release(token)
        bridge.release(token)
        assert bridge._active_model_load_count == 0

    def test_cleanup_expires_stale_acquired(self):
        """cleanup_expired marks ACQUIRED tokens past TTL as EXPIRED."""
        bridge = self._make_bridge()
        token = self._make_token(bridge)
        token.acquired_at = time.monotonic() - 200.0  # Way past TTL
        token.last_heartbeat_at = None  # No heartbeat ever
        token.token_ttl_s = 120.0
        bridge.cleanup_expired(max_age_s=120.0)
        assert token.state == BudgetTokenState.EXPIRED

    def test_cleanup_preserves_active_reused(self):
        """cleanup_expired does NOT reclaim REUSED tokens with fresh heartbeat."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="supervisor")
        bridge.transfer_token(token)
        bridge.reuse_token(token, requester_session_id="supervisor")
        token.last_heartbeat_at = time.monotonic()  # Fresh heartbeat
        bridge.cleanup_expired(max_age_s=120.0)
        assert token.state == BudgetTokenState.REUSED  # NOT expired

    def test_cleanup_expires_stale_reused(self):
        """cleanup_expired reclaims REUSED tokens with stale heartbeat."""
        bridge = self._make_bridge()
        token = self._make_token(bridge, session_id="supervisor")
        bridge.transfer_token(token)
        bridge.reuse_token(token, requester_session_id="supervisor")
        token.last_heartbeat_at = time.monotonic() - 60.0  # Stale (>45s)
        bridge.cleanup_expired(max_age_s=120.0, heartbeat_silence_s=45.0)
        assert token.state == BudgetTokenState.EXPIRED

    def test_invariant_freeze_on_model_load_exceed(self):
        """If MODEL_LOAD count exceeds 1, bridge enters frozen state."""
        bridge = self._make_bridge()
        self._make_token(bridge, category=HeavyTaskCategory.MODEL_LOAD)
        # Force a second active MODEL_LOAD (shouldn't happen normally)
        bridge._active_model_load_count = 2
        bridge._check_invariant()
        assert bridge._frozen is True
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py::TestTokenLifecycle -v`
Expected: FAIL — `EcapaBudgetBridge` not yet defined

**Step 3: Write minimal implementation**

Append to `backend/core/ecapa_budget_bridge.py` (add `EcapaBudgetBridge` to `__all__`):

```python
# Add to __all__:
# "EcapaBudgetBridge",

# Add import at top:
# import uuid  (already used in tests — add to module)

class EcapaBudgetBridge:
    """Process-wide singleton mediating all ECAPA budget interactions.

    Manages BudgetToken lifecycle: ACQUIRED -> TRANSFERRED -> REUSED -> RELEASED.
    Crash-safe cleanup auto-expires orphaned tokens.
    """

    _instance: Optional["EcapaBudgetBridge"] = None

    def __init__(self) -> None:
        self._init_internal()

    def _init_internal(self) -> None:
        """Separated init for test isolation (can call on __new__ instances)."""
        self._tokens: dict[str, BudgetToken] = {}
        self._active_model_load_count: int = 0
        self._frozen: bool = False
        self._session_id: str = str(uuid.uuid4())

    @classmethod
    def get_instance(cls) -> "EcapaBudgetBridge":
        """Return the process-wide singleton, creating it if needed."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton for testing only."""
        cls._instance = None

    # -- Token lifecycle ------------------------------------------------------

    def transfer_token(self, token: BudgetToken) -> BudgetToken:
        """CAS: ACQUIRED -> TRANSFERRED. Single-use; second call raises.

        Raises
        ------
        ValueError
            If token is not in ACQUIRED state (CAS violation).
        """
        if token.state != BudgetTokenState.ACQUIRED:
            raise ValueError(
                f"CAS violation: transfer_token requires ACQUIRED state, "
                f"got {token.state.value}"
            )
        token.state = BudgetTokenState.TRANSFERRED
        token.transferred_at = time.monotonic()
        logger.info(
            "Token %s transferred (category=%s)",
            token.token_id,
            token.category.name,
        )
        return token

    def reuse_token(
        self,
        token: BudgetToken,
        requester_session_id: str,
    ) -> BudgetToken:
        """TRANSFERRED -> REUSED with ownership verification.

        Parameters
        ----------
        token:
            The token to reuse (must be in TRANSFERRED state).
        requester_session_id:
            Must match the token's owner_session_id.

        Raises
        ------
        ValueError
            If token is not TRANSFERRED or ownership check fails.
        """
        if token.state != BudgetTokenState.TRANSFERRED:
            raise ValueError(
                f"reuse_token requires TRANSFERRED state, "
                f"got {token.state.value}"
            )
        if token.owner_session_id != requester_session_id:
            raise ValueError(
                f"reuse_token owner mismatch: token owner "
                f"{token.owner_session_id!r} != requester "
                f"{requester_session_id!r}"
            )
        token.state = BudgetTokenState.REUSED
        token.last_heartbeat_at = time.monotonic()
        logger.info(
            "Token %s reused by session %s",
            token.token_id,
            requester_session_id,
        )
        return token

    def heartbeat(self, token: BudgetToken) -> None:
        """Update the token's heartbeat timestamp."""
        token.last_heartbeat_at = time.monotonic()

    def release(self, token: BudgetToken) -> None:
        """Idempotent release — returns slot to pool, marks RELEASED.

        Safe to call multiple times. Only decrements counters once.
        """
        if token.state == BudgetTokenState.RELEASED:
            return  # Already released — idempotent
        if token.state == BudgetTokenState.EXPIRED:
            return  # Already expired — nothing to do

        was_active = token.state in (
            BudgetTokenState.ACQUIRED,
            BudgetTokenState.TRANSFERRED,
            BudgetTokenState.REUSED,
        )

        token.state = BudgetTokenState.RELEASED
        token.released_at = time.monotonic()

        if was_active and token.category is HeavyTaskCategory.MODEL_LOAD:
            self._active_model_load_count = max(
                0, self._active_model_load_count - 1
            )

        logger.info(
            "Token %s released (category=%s, model_load_active=%d)",
            token.token_id,
            token.category.name,
            self._active_model_load_count,
        )

    def cleanup_expired(
        self,
        max_age_s: float = 120.0,
        heartbeat_silence_s: float = 45.0,
    ) -> int:
        """Reclaim orphaned tokens. Returns count of expired tokens.

        Rules:
        - ACQUIRED/TRANSFERRED with no heartbeat + age > max_age_s -> EXPIRED
        - REUSED with stale heartbeat (> heartbeat_silence_s) -> EXPIRED
        - REUSED with fresh heartbeat -> PRESERVED (not reclaimed)
        """
        now = time.monotonic()
        expired_count = 0

        for token in list(self._tokens.values()):
            if token.state in (BudgetTokenState.RELEASED, BudgetTokenState.EXPIRED):
                continue

            should_expire = False

            if token.state == BudgetTokenState.REUSED:
                # Only expire if heartbeat is stale
                if token.last_heartbeat_at is not None:
                    silence = now - token.last_heartbeat_at
                    if silence > heartbeat_silence_s:
                        should_expire = True
                else:
                    # No heartbeat ever in REUSED = stale
                    should_expire = True
            elif token.state in (
                BudgetTokenState.ACQUIRED,
                BudgetTokenState.TRANSFERRED,
            ):
                age = now - token.acquired_at
                has_heartbeat = (
                    token.last_heartbeat_at is not None
                    and (now - token.last_heartbeat_at) < heartbeat_silence_s
                )
                if age > max_age_s and not has_heartbeat:
                    should_expire = True

            if should_expire:
                was_model_load = (
                    token.category is HeavyTaskCategory.MODEL_LOAD
                    and token.state
                    in (
                        BudgetTokenState.ACQUIRED,
                        BudgetTokenState.TRANSFERRED,
                        BudgetTokenState.REUSED,
                    )
                )
                token.state = BudgetTokenState.EXPIRED
                if was_model_load:
                    self._active_model_load_count = max(
                        0, self._active_model_load_count - 1
                    )
                expired_count += 1
                logger.warning(
                    "Token %s expired by cleanup (category=%s)",
                    token.token_id,
                    token.category.name,
                )

        return expired_count

    def _check_invariant(self) -> None:
        """Verify MODEL_LOAD invariant: at most 1 active at a time.

        If violated: freeze new acquisitions, log CRITICAL.
        Does NOT force-release in-flight tokens.
        """
        if self._active_model_load_count > 1:
            self._frozen = True
            logger.critical(
                "INVARIANT VIOLATION: %d active MODEL_LOAD tokens "
                "(max 1). New acquisitions frozen.",
                self._active_model_load_count,
            )
```

Also update `__all__` to include `"EcapaBudgetBridge"` and add `import uuid` at the top of the file.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: All 20 tests PASS (7 from Task 1 + 13 from Task 2)

**Step 5: Commit**

```bash
git add backend/core/ecapa_budget_bridge.py tests/unit/core/test_ecapa_budget_bridge.py
git commit -m "feat(disease10): add EcapaBudgetBridge token lifecycle and invariant checks"
```

---

## Task 3: Bridge Singleton — Budget Slot Acquisition

**Files:**
- Modify: `backend/core/ecapa_budget_bridge.py`
- Modify: `backend/core/startup_config.py`
- Test: `tests/unit/core/test_ecapa_budget_bridge.py`

**Context:** Add `acquire_probe_slot()` and `acquire_model_slot()` methods that delegate to `StartupBudgetPolicy`. These methods check memory stability via MemoryQuantizer, enforce phase preconditions, and return `Result[BudgetToken, EcapaBudgetRejection]`. Also update `startup_config.py` to add the ECAPA probe precondition.

**Reference:** `StartupBudgetPolicy.acquire()` is an async context manager that yields a `TaskSlot`. We wrap it to manage `BudgetToken` creation and track MODEL_LOAD count.

**Step 1: Update startup_config.py**

Read `backend/core/startup_config.py` line 396-407 (current soft_preconditions). Add ECAPA_PROBE mapping.

Modify `backend/core/startup_config.py` — in the `_make_default_config()` function, add to the `soft_preconditions` dict:

```python
    # Existing:
    soft_preconditions: Dict[str, SoftGatePrecondition] = {
        "ML_INIT": SoftGatePrecondition(
            require_phase="CORE_READY",
            require_memory_stable_s=memory_stable_s,
            memory_slope_threshold_mb_s=memory_slope,
        ),
        "GCP_PROVISION": SoftGatePrecondition(
            require_phase="PREWARM_GCP",
            require_memory_stable_s=memory_stable_s,
            memory_slope_threshold_mb_s=memory_slope,
        ),
    }
```

No changes needed — `ML_INIT` already has the `CORE_READY` precondition. The bridge uses `ML_INIT` for probes and `MODEL_LOAD` for loads. `MODEL_LOAD` is a hard gate (no precondition needed — it's serialized by the hard semaphore). The existing config is sufficient.

**Step 2: Write the failing tests**

Append to `tests/unit/core/test_ecapa_budget_bridge.py`:

```python
import asyncio

from backend.core.startup_budget_policy import StartupBudgetPolicy
from backend.core.startup_config import BudgetConfig, SoftGatePrecondition


@pytest.fixture
def budget_policy() -> StartupBudgetPolicy:
    """Create a budget policy with standard Disease 10 config."""
    config = BudgetConfig(
        max_hard_concurrent=1,
        max_total_concurrent=3,
        hard_gate_categories=["MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"],
        soft_gate_categories=["ML_INIT", "GCP_PROVISION"],
        soft_gate_preconditions={
            "ML_INIT": SoftGatePrecondition(
                require_phase="CORE_READY",
                require_memory_stable_s=0.0,  # Disable for tests
                memory_slope_threshold_mb_s=999.0,
            ),
        },
        max_wait_s=5.0,
    )
    policy = StartupBudgetPolicy(config)
    return policy


class TestBudgetSlotAcquisition:
    """Test acquire_probe_slot and acquire_model_slot."""

    @pytest.mark.asyncio
    async def test_acquire_probe_slot_success(self, budget_policy):
        """Probe slot acquired when phase is reached."""
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy
        budget_policy.signal_phase_reached("CORE_READY")

        result = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert result is not None
        assert not isinstance(result, EcapaBudgetRejection)
        assert result.state == BudgetTokenState.ACQUIRED
        assert result.category is HeavyTaskCategory.ML_INIT
        bridge.release(result)

    @pytest.mark.asyncio
    async def test_acquire_probe_slot_phase_blocked(self, budget_policy):
        """Probe slot denied when CORE_READY phase not reached."""
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy
        # Do NOT signal CORE_READY

        result = await bridge.acquire_probe_slot(timeout_s=2.0)
        assert result is EcapaBudgetRejection.PHASE_BLOCKED

    @pytest.mark.asyncio
    async def test_acquire_model_slot_success(self, budget_policy):
        """Model slot acquired (hard gate, no precondition)."""
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy

        result = await bridge.acquire_model_slot(timeout_s=2.0)
        assert result is not None
        assert not isinstance(result, EcapaBudgetRejection)
        assert result.state == BudgetTokenState.ACQUIRED
        assert result.category is HeavyTaskCategory.MODEL_LOAD
        assert bridge._active_model_load_count == 1
        bridge.release(result)
        assert bridge._active_model_load_count == 0

    @pytest.mark.asyncio
    async def test_acquire_model_slot_frozen_rejected(self, budget_policy):
        """Model slot denied when bridge is frozen (invariant violation)."""
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy
        bridge._frozen = True

        result = await bridge.acquire_model_slot(timeout_s=2.0)
        assert result is EcapaBudgetRejection.SLOT_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_acquire_model_slot_timeout(self, budget_policy):
        """Model slot times out when hard gate is held."""
        bridge = EcapaBudgetBridge.__new__(EcapaBudgetBridge)
        bridge._init_internal()
        bridge._budget_policy = budget_policy

        # Hold the MODEL_LOAD slot
        first = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(first, BudgetToken)

        # Second acquisition should time out
        result = await bridge.acquire_model_slot(timeout_s=0.5)
        assert result is EcapaBudgetRejection.BUDGET_TIMEOUT

        bridge.release(first)
```

**Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py::TestBudgetSlotAcquisition -v`
Expected: FAIL — `acquire_probe_slot` / `acquire_model_slot` not defined

**Step 4: Write implementation**

Add to `EcapaBudgetBridge` in `backend/core/ecapa_budget_bridge.py`:

```python
    # Add to _init_internal():
    #   self._budget_policy: Optional[StartupBudgetPolicy] = None
    #   self._budget_contexts: dict[str, Any] = {}  # token_id -> context manager

    def set_budget_policy(self, policy: StartupBudgetPolicy) -> None:
        """Inject the budget policy (called once during startup wiring)."""
        self._budget_policy = policy

    async def acquire_probe_slot(
        self,
        timeout_s: float = 10.0,
        priority: str = "normal",
    ) -> BudgetToken | EcapaBudgetRejection:
        """Acquire an ML_INIT slot for cloud ECAPA probe.

        Returns a BudgetToken on success, or an EcapaBudgetRejection on failure.
        """
        if self._frozen:
            return EcapaBudgetRejection.SLOT_UNAVAILABLE

        category = ECAPA_CATEGORY_MAP["probe"]
        return await self._acquire_slot(category, "ecapa_probe", timeout_s)

    async def acquire_model_slot(
        self,
        timeout_s: float = 30.0,
        priority: str = "normal",
    ) -> BudgetToken | EcapaBudgetRejection:
        """Acquire a MODEL_LOAD slot for local ECAPA model loading.

        Returns a BudgetToken on success, or an EcapaBudgetRejection on failure.
        """
        if self._frozen:
            return EcapaBudgetRejection.SLOT_UNAVAILABLE

        category = ECAPA_CATEGORY_MAP["model_load"]
        return await self._acquire_slot(category, "ecapa_model_load", timeout_s)

    async def _acquire_slot(
        self,
        category: HeavyTaskCategory,
        name: str,
        timeout_s: float,
    ) -> BudgetToken | EcapaBudgetRejection:
        """Internal: acquire a budget slot and create a tracked token."""
        if self._budget_policy is None:
            logger.warning("No budget policy set — acquiring without budget gate")
            return self._create_ungated_token(category)

        try:
            ctx = self._budget_policy.acquire(category, name, timeout=timeout_s)
            slot = await ctx.__aenter__()
            token = BudgetToken(
                token_id=str(uuid.uuid4()),
                owner_session_id=self._session_id,
                state=BudgetTokenState.ACQUIRED,
                category=category,
                acquired_at=time.monotonic(),
                last_heartbeat_at=time.monotonic(),
            )
            self._tokens[token.token_id] = token
            self._budget_contexts[token.token_id] = ctx

            if category is HeavyTaskCategory.MODEL_LOAD:
                self._active_model_load_count += 1
                self._check_invariant()

            return token
        except PreconditionNotMetError:
            return EcapaBudgetRejection.PHASE_BLOCKED
        except BudgetAcquisitionError:
            return EcapaBudgetRejection.BUDGET_TIMEOUT

    def _create_ungated_token(self, category: HeavyTaskCategory) -> BudgetToken:
        """Fallback: create a token without budget policy (degraded mode)."""
        token = BudgetToken(
            token_id=str(uuid.uuid4()),
            owner_session_id=self._session_id,
            state=BudgetTokenState.ACQUIRED,
            category=category,
            acquired_at=time.monotonic(),
            last_heartbeat_at=time.monotonic(),
        )
        self._tokens[token.token_id] = token
        if category is HeavyTaskCategory.MODEL_LOAD:
            self._active_model_load_count += 1
        return token
```

Also update `release()` to exit the budget context manager:

```python
    def release(self, token: BudgetToken) -> None:
        # ... existing logic, then after setting RELEASED:
        # Release the budget context if we hold one
        ctx = self._budget_contexts.pop(token.token_id, None)
        if ctx is not None:
            # Schedule async cleanup — context manager __aexit__
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._release_budget_ctx(ctx, token.token_id))
            except RuntimeError:
                pass  # No event loop — best-effort
```

Add the import `from backend.core.startup_budget_policy import (StartupBudgetPolicy, BudgetAcquisitionError, PreconditionNotMetError)` at the top.

**Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: All 25 tests PASS

**Step 6: Commit**

```bash
git add backend/core/ecapa_budget_bridge.py backend/core/startup_config.py tests/unit/core/test_ecapa_budget_bridge.py
git commit -m "feat(disease10): add EcapaBudgetBridge budget slot acquisition with policy integration"
```

---

## Task 4: Bridge Telemetry Integration

**Files:**
- Modify: `backend/core/ecapa_budget_bridge.py`
- Test: `tests/unit/core/test_ecapa_budget_bridge.py`

**Context:** Wire `StartupEventBus` into the bridge so every token lifecycle action emits a canonical event. The bus is injected via `set_event_bus()`. All events use the prefix `ecapa_budget.` and include token metadata.

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_ecapa_budget_bridge.py`:

```python
from backend.core.startup_telemetry import StartupEventBus


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
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py::TestTelemetryEmission -v`
Expected: FAIL — `_event_bus` not set, no emit calls

**Step 3: Write implementation**

Add to `EcapaBudgetBridge._init_internal()`:
```python
    self._event_bus: Optional[StartupEventBus] = None
```

Add method:
```python
    def set_event_bus(self, bus: StartupEventBus) -> None:
        """Inject the telemetry event bus."""
        self._event_bus = bus

    def _emit(self, event_type: str, detail: dict) -> None:
        """Synchronously emit a telemetry event (best-effort)."""
        if self._event_bus is None:
            return
        event = self._event_bus.create_event(
            event_type=event_type,
            detail=detail,
            phase="ecapa_budget",
        )
        # Synchronous append to bus history (async consumers called later)
        self._event_bus._history.append(event)
```

Then add `self._emit(...)` calls to:
- `_acquire_slot()` — emit `acquire_attempt` before try, `acquire_granted` on success, `acquire_denied` on failure
- `transfer_token()` — emit `ecapa_budget.transfer`
- `reuse_token()` — emit `ecapa_budget.reuse`
- `release()` — emit `ecapa_budget.release`
- `cleanup_expired()` — emit `ecapa_budget.expire_cleanup` per expired token
- `_check_invariant()` — emit `ecapa_budget.invariant_violation` on breach

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: All 29 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ecapa_budget_bridge.py tests/unit/core/test_ecapa_budget_bridge.py
git commit -m "feat(disease10): add telemetry emission to EcapaBudgetBridge lifecycle"
```

---

## Task 5: Wire Supervisor Startup Path

**Files:**
- Modify: `unified_supervisor.py:73065-73220` — replace ad-hoc ECAPA verification with bridge-coordinated flow
- Test: `tests/integration/test_ecapa_budget_wiring.py`

**Context:** The current ECAPA verification at `unified_supervisor.py:73065-73219` is a monolithic background task with ad-hoc memory checks, CPU backpressure polling, and cloud-first gating. Replace it with the bridge-coordinated flow:

1. Signal DEFERRED_COMPONENTS phase reached
2. Acquire probe slot (ML_INIT) from bridge
3. Run cloud probe (bounded timeout + retries)
4. On probe failure: release probe slot, acquire model slot (MODEL_LOAD), transfer token to registry
5. On probe success: release probe slot, done

**Important context for the implementer:**
- `unified_supervisor.py` is a 73K+ line file — use line-specific reads and edits
- The ECAPA block is at lines 73065-73219
- `create_safe_task()` is the standard task creation helper used throughout the file
- `self._ecapa_verification_task` is the existing task reference
- The bridge singleton must be created earlier in startup and stored on `self`

**Step 1: Write the failing integration test**

Create `tests/integration/test_ecapa_budget_wiring.py`:

```python
"""Integration tests for ECAPA budget wiring — supervisor startup path."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

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

        # Now second should succeed
        third = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(third, BudgetToken)
        bridge.release(third)


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

        # Next recovery attempt should succeed
        new_token = await bridge.acquire_model_slot(timeout_s=2.0)
        assert isinstance(new_token, BudgetToken)
        bridge.release(new_token)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/integration/test_ecapa_budget_wiring.py -v`
Expected: Some tests may pass if bridge implementation from Tasks 1-4 is complete. Verify all PASS.

**Step 3: Modify unified_supervisor.py**

Read `unified_supervisor.py:73065-73219` (the current ECAPA block). Replace it with the bridge-coordinated flow. The key changes:

1. **Before Phase 4** (around line ~72948): Create bridge instance and store on self:
```python
# Right after Phase 4 intelligence init, before ECAPA block:
from backend.core.ecapa_budget_bridge import EcapaBudgetBridge
self._ecapa_bridge = EcapaBudgetBridge.get_instance()
if hasattr(self, '_startup_budget_policy') and self._startup_budget_policy:
    self._ecapa_bridge.set_budget_policy(self._startup_budget_policy)
if hasattr(self, '_startup_event_bus') and self._startup_event_bus:
    self._ecapa_bridge.set_event_bus(self._startup_event_bus)
```

2. **Replace lines 73065-73219** with bridge-coordinated ECAPA verification:

```python
# v300.0: ECAPA verification via Budget Token Bridge (Disease 10 wiring).
# Replaces ad-hoc memory/CPU checks with budget-gated startup flow.
# Path: cloud probe (ML_INIT slot) -> local load (MODEL_LOAD slot).
if self.config.ecapa_enabled:
    async def _run_ecapa_budget_gated() -> None:
        """ECAPA verification gated by Disease 10 budget system."""
        bridge = self._ecapa_bridge
        _ecapa_bg_timeout = _get_env_float("JARVIS_ECAPA_BG_TIMEOUT", 90.0)

        try:
            # Phase 1: Cloud probe under ML_INIT slot
            probe_result = await bridge.acquire_probe_slot(timeout_s=10.0)
            if isinstance(probe_result, EcapaBudgetRejection):
                self.logger.warning(
                    "[Kernel] ECAPA probe slot denied: %s — skipping to local",
                    probe_result.value,
                )
            else:
                # Run cloud probe with bounded timeout
                cloud_ok = False
                try:
                    cloud_ok = await asyncio.wait_for(
                        self._ecapa_cloud_probe(), timeout=5.0,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    probe_result.probe_failure_reason = str(e)
                finally:
                    bridge.release(probe_result)

                if cloud_ok:
                    self.logger.info("[Kernel] ECAPA cloud probe succeeded — cloud path active")
                    return

            # Phase 2: Local model load under MODEL_LOAD slot
            model_result = await bridge.acquire_model_slot(timeout_s=30.0)
            if isinstance(model_result, EcapaBudgetRejection):
                self.logger.warning(
                    "[Kernel] ECAPA model slot denied: %s — scheduling deferred recovery",
                    model_result.value,
                )
                # Trigger deferred recovery in registry
                try:
                    from backend.voice_unlock.ml_engine_registry import get_ml_registry
                    _ml_reg = await get_ml_registry()
                    _ml_reg._schedule_deferred_ecapa_recovery()
                except Exception:
                    pass
                return

            # Transfer token to registry for model loading
            bridge.transfer_token(model_result)
            try:
                _ecapa_deadline = time.monotonic() + _ecapa_bg_timeout
                ecapa_verify = await self._verify_ecapa_pipeline(
                    skip_db_dependent=False,
                    deadline=_ecapa_deadline,
                    budget_token=model_result,
                )
                if ecapa_verify.get("verification_pipeline_ready"):
                    self.logger.info("[Kernel] ECAPA pipeline verified and ready")
                else:
                    issue_collector.add_warning(
                        "ECAPA pipeline verification incomplete",
                        IssueCategory.INTELLIGENCE,
                    )
            finally:
                bridge.release(model_result)

        except asyncio.CancelledError:
            raise
        except Exception as ev_err:
            self.logger.debug(f"[Kernel] ECAPA budget-gated verification error: {ev_err}")

    if self._ecapa_verification_task is None or self._ecapa_verification_task.done():
        self._ecapa_verification_task = create_safe_task(
            _run_ecapa_budget_gated(),
            name="ecapa-verification",
        )
        self._background_tasks.append(self._ecapa_verification_task)
```

**Step 4: Run integration tests**

Run: `python3 -m pytest tests/integration/test_ecapa_budget_wiring.py -v`
Expected: All 5 tests PASS

**Step 5: Run existing Disease 10 tests to verify no regression**

Run: `python3 -m pytest tests/unit/core/test_startup_budget_policy.py tests/unit/core/test_startup_config.py -v`
Expected: All existing tests PASS

**Step 6: Commit**

```bash
git add unified_supervisor.py tests/integration/test_ecapa_budget_wiring.py backend/core/ecapa_budget_bridge.py
git commit -m "feat(disease10): wire supervisor ECAPA startup path through budget bridge"
```

---

## Task 6: Wire Registry Recovery Path

**Files:**
- Modify: `backend/voice_unlock/ml_engine_registry.py:5686-5784` — replace blind polling with budget-aware recovery
- Test: `tests/integration/test_ecapa_budget_wiring.py`

**Context:** The current `_schedule_deferred_ecapa_recovery()` at line 5686 polls every 30s blindly and pauses during startup. Replace with budget-aware recovery that:

1. Acquires probe slot from bridge (budget-gated)
2. On denial: uses differentiated backoff (not blind 30s)
3. On probe success: cloud path active
4. On probe failure: acquire model slot, load locally under budget
5. On model slot denial: backoff per rejection type

**Important:**
- `ml_engine_registry.py` is ~5800 lines — use targeted edits
- The recovery loop is at lines 5715-5772
- Keep the existing `_attempt_ecapa_recovery()` method (lines 4530-4650) as internal logic
- The bridge import should be lazy (avoid circular imports)

**Step 1: Write failing test**

Append to `tests/integration/test_ecapa_budget_wiring.py`:

```python
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
```

**Step 2: Run tests to verify they fail (or pass if bridge is already implemented)**

Run: `python3 -m pytest tests/integration/test_ecapa_budget_wiring.py::TestBudgetAwareRecovery -v`

**Step 3: Modify ml_engine_registry.py**

Replace the `_schedule_deferred_ecapa_recovery()` method at line 5686 with a budget-aware version:

```python
    def _schedule_deferred_ecapa_recovery(self) -> None:
        """v300.0: Schedule budget-aware ECAPA recovery.

        Uses EcapaBudgetBridge for coordinated slot acquisition instead
        of blind 30s polling. Differentiated backoff per rejection type.
        """
        if self._deferred_ecapa_recovery_task is not None:
            return  # Already scheduled

        self._memory_gate_blocked = True

        async def _budget_aware_recovery_loop() -> None:
            # Lazy import to avoid circular dependency
            from backend.core.ecapa_budget_bridge import (
                EcapaBudgetBridge,
                EcapaBudgetRejection,
                BudgetToken,
            )

            bridge = EcapaBudgetBridge.get_instance()
            max_attempts = int(os.getenv("JARVIS_ECAPA_RECOVERY_MAX_ATTEMPTS", "20"))
            base_delay = float(os.getenv("JARVIS_ECAPA_RECOVERY_BASE_DELAY", "5.0"))
            max_delay = float(os.getenv("JARVIS_ECAPA_RECOVERY_MAX_DELAY", "120.0"))
            attempt = 0

            logger.info(
                "[v300.0] Budget-aware ECAPA recovery scheduled "
                "(max %d attempts, base delay %.1fs)",
                max_attempts, base_delay,
            )

            while attempt < max_attempts:
                if self.is_ready:
                    logger.info("[v300.0] ECAPA already ready — recovery exiting")
                    self._memory_gate_blocked = False
                    return

                attempt += 1

                # Phase 1: Cloud probe under budget
                probe_result = await bridge.acquire_probe_slot(timeout_s=5.0)
                if isinstance(probe_result, EcapaBudgetRejection):
                    delay = self._compute_backoff(
                        probe_result, attempt, base_delay, max_delay,
                    )
                    logger.info(
                        "[v300.0] Recovery #%d probe denied (%s) — "
                        "backoff %.1fs",
                        attempt, probe_result.value, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Probe slot acquired — try cloud
                cloud_ok = False
                try:
                    cloud_ok = await self._attempt_cloud_recovery()
                except Exception as e:
                    probe_result.probe_failure_reason = str(e)
                finally:
                    bridge.release(probe_result)

                if cloud_ok:
                    logger.info(
                        "[v300.0] Recovery #%d cloud succeeded!",
                        attempt,
                    )
                    self._memory_gate_blocked = False
                    return

                # Phase 2: Local load under budget
                model_result = await bridge.acquire_model_slot(timeout_s=30.0)
                if isinstance(model_result, EcapaBudgetRejection):
                    delay = self._compute_backoff(
                        model_result, attempt, base_delay, max_delay,
                    )
                    logger.info(
                        "[v300.0] Recovery #%d model slot denied (%s) — "
                        "backoff %.1fs",
                        attempt, model_result.value, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Model slot acquired — try local load
                try:
                    bridge.heartbeat(model_result)
                    success = await self._attempt_local_load_recovery()
                    if success:
                        logger.info(
                            "[v300.0] Recovery #%d local load succeeded!",
                            attempt,
                        )
                        self._memory_gate_blocked = False
                        return
                finally:
                    bridge.release(model_result)

                # Both failed — backoff
                delay = self._compute_backoff(
                    EcapaBudgetRejection.BUDGET_TIMEOUT,
                    attempt, base_delay, max_delay,
                )
                await asyncio.sleep(delay)

            logger.error(
                "[v300.0] Recovery exhausted %d attempts. "
                "Voice unlock unavailable until restart.",
                max_attempts,
            )
            self._memory_gate_blocked = False

        try:
            loop = asyncio.get_running_loop()
            self._deferred_ecapa_recovery_task = loop.create_task(
                _budget_aware_recovery_loop(),
                name="ecapa-budget-recovery",
            )
        except RuntimeError:
            logger.error(
                "[v300.0] No running event loop for budget recovery."
            )

    @staticmethod
    def _compute_backoff(
        rejection: "EcapaBudgetRejection",
        attempt: int,
        base: float,
        cap: float,
    ) -> float:
        """Differentiated backoff per rejection type."""
        import random
        from backend.core.ecapa_budget_bridge import EcapaBudgetRejection

        if rejection == EcapaBudgetRejection.THRASH_EMERGENCY:
            delay = min(15.0 * (2 ** (attempt - 1)), cap)
            jitter = delay * 0.3 * random.uniform(-1, 1)
        elif rejection == EcapaBudgetRejection.CONTRACT_MISMATCH:
            delay = cap  # Non-retryable — wait max
            jitter = 0.0
        else:
            delay = min(base * (2 ** (attempt - 1)), 60.0)
            jitter = delay * 0.2 * random.uniform(-1, 1)

        return max(1.0, delay + jitter)
```

**Step 4: Run all tests**

Run: `python3 -m pytest tests/integration/test_ecapa_budget_wiring.py tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/voice_unlock/ml_engine_registry.py tests/integration/test_ecapa_budget_wiring.py
git commit -m "feat(disease10): wire MLEngineRegistry deferred recovery through budget bridge"
```

---

## Task 7: Final Integration Tests and Contract Mismatch

**Files:**
- Test: `tests/integration/test_ecapa_budget_wiring.py`

**Context:** Add the remaining integration tests: contract mismatch (non-retryable), full startup sequence with DEFERRED_COMPONENTS gate, and telemetry event completeness check.

**Step 1: Write tests**

Append to `tests/integration/test_ecapa_budget_wiring.py`:

```python
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
```

**Step 2: Run all tests**

Run: `python3 -m pytest tests/integration/test_ecapa_budget_wiring.py tests/unit/core/test_ecapa_budget_bridge.py -v`
Expected: All PASS

**Step 3: Run full Disease 10 test suite for regression check**

Run: `python3 -m pytest tests/unit/core/test_startup_budget_policy.py tests/unit/core/test_startup_config.py tests/unit/core/test_startup_telemetry.py tests/unit/core/test_routing_authority_fsm.py tests/unit/core/test_startup_orchestrator.py -v`
Expected: All existing Disease 10 tests PASS

**Step 4: Commit**

```bash
git add tests/integration/test_ecapa_budget_wiring.py
git commit -m "test(disease10): add integration tests for contract mismatch, backoff, and telemetry completeness"
```

---

## Summary

| Task | What | Tests Added |
|------|------|-------------|
| 1 | Core types: enums, BudgetToken, category mapping | 7 |
| 2 | Token lifecycle: transfer, reuse, heartbeat, release, cleanup | 13 |
| 3 | Budget slot acquisition: probe (ML_INIT) + model (MODEL_LOAD) | 5 |
| 4 | Telemetry integration: canonical events for all actions | 4 |
| 5 | Supervisor startup path wired through bridge | 5 |
| 6 | Registry recovery path wired through bridge | 2 |
| 7 | Contract mismatch, backoff verification, telemetry completeness | 5 |
| **Total** | | **~41 tests** |

**Files created:** 3 (bridge module + 2 test files)
**Files modified:** 3 (unified_supervisor.py, ml_engine_registry.py, startup_config.py)
**Commits:** 7 (one per task)
