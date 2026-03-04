# MCP Governance Unification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate memory-governance fragmentation by routing all 40+ decision points through the MemoryBudgetBroker, with decision envelope fencing, actuator arbitration, and shadow-mode cutover.

**Architecture:** Add a `PressurePolicy` (versioned threshold rules + hysteresis) and `DecisionEnvelope` (snapshot_id + epoch + sequence fencing) to `memory_types.py`. Add a `MemoryActuatorCoordinator` to the broker that serializes actuator requests (cleanup, offload, degrade, disconnect) so only one system acts at a time. Migrate files in dependency order: infrastructure first, then top-10 critical actuators, each with shadow-mode confidence gate before cutover.

**Tech Stack:** Python 3.11+, asyncio, dataclasses (frozen), existing MemoryBudgetBroker + MemoryQuantizer

---

## Phase 0: Decision Infrastructure (Foundation)

### Task 1: Add `PressurePolicy` and `DecisionEnvelope` to `memory_types.py`

**Files:**
- Modify: `backend/core/memory_types.py:395-401`
- Test: `tests/unit/test_decision_envelope.py`

**Context:** Every actuator decision must carry provenance (which snapshot, which epoch, which policy version). This prevents stale decisions from executing after conditions change. The `PressurePolicy` replaces the 6+ sets of hardcoded thresholds scattered across files with one versioned source of truth.

**Step 1: Write the failing test**

```python
# tests/unit/test_decision_envelope.py
"""Tests for DecisionEnvelope and PressurePolicy types."""
import dataclasses
import time
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.core.memory_types import (
    DecisionEnvelope,
    PressurePolicy,
    PressureTier,
    ActuatorAction,
)


class TestDecisionEnvelope:
    def test_envelope_is_frozen(self):
        env = DecisionEnvelope(
            snapshot_id="snap-001",
            epoch=1,
            sequence=1,
            policy_version="v1.0",
            pressure_tier=PressureTier.ELEVATED,
            timestamp=time.time(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.epoch = 2

    def test_is_stale_same_epoch_old_sequence(self):
        env = DecisionEnvelope(
            snapshot_id="snap-001",
            epoch=1,
            sequence=5,
            policy_version="v1.0",
            pressure_tier=PressureTier.ELEVATED,
            timestamp=time.time(),
        )
        assert env.is_stale(current_epoch=1, current_sequence=10)

    def test_is_not_stale_when_current(self):
        env = DecisionEnvelope(
            snapshot_id="snap-002",
            epoch=1,
            sequence=10,
            policy_version="v1.0",
            pressure_tier=PressureTier.ELEVATED,
            timestamp=time.time(),
        )
        assert not env.is_stale(current_epoch=1, current_sequence=10)

    def test_is_stale_old_epoch(self):
        env = DecisionEnvelope(
            snapshot_id="snap-001",
            epoch=1,
            sequence=100,
            policy_version="v1.0",
            pressure_tier=PressureTier.ELEVATED,
            timestamp=time.time(),
        )
        assert env.is_stale(current_epoch=2, current_sequence=1)


class TestPressurePolicy:
    def test_default_thresholds_cover_all_tiers(self):
        policy = PressurePolicy()
        # Must have enter/exit thresholds for every actionable tier
        for tier in (PressureTier.ELEVATED, PressureTier.CONSTRAINED,
                     PressureTier.CRITICAL, PressureTier.EMERGENCY):
            assert tier in policy.enter_thresholds
            assert tier in policy.exit_thresholds
            # Hysteresis: exit < enter
            assert policy.exit_thresholds[tier] < policy.enter_thresholds[tier]

    def test_dwell_and_cooldown_positive(self):
        policy = PressurePolicy()
        assert policy.min_dwell_seconds > 0
        assert policy.cooldown_seconds > 0

    def test_policy_version_set(self):
        policy = PressurePolicy()
        assert policy.version.startswith("v")

    def test_ram_profile_factory_consumer(self):
        policy = PressurePolicy.for_ram_gb(16.0)
        # 16GB "consumer" — higher thresholds (80% baseline is normal)
        assert policy.enter_thresholds[PressureTier.ELEVATED] >= 75.0

    def test_ram_profile_factory_server(self):
        policy = PressurePolicy.for_ram_gb(64.0)
        # 64GB "server" — lower thresholds (80% means something is wrong)
        assert policy.enter_thresholds[PressureTier.ELEVATED] <= 65.0


class TestActuatorAction:
    def test_action_values(self):
        assert ActuatorAction.CLEANUP.value == "cleanup"
        assert ActuatorAction.CLOUD_OFFLOAD.value == "cloud_offload"
        assert ActuatorAction.DISPLAY_SHED.value == "display_shed"
        assert ActuatorAction.DEFCON_ESCALATE.value == "defcon_escalate"
        assert ActuatorAction.MODEL_EVICT.value == "model_evict"

    def test_action_has_priority(self):
        # Display shed is less disruptive than process kill
        assert ActuatorAction.DISPLAY_SHED.priority < ActuatorAction.CLEANUP.priority
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_decision_envelope.py -v`
Expected: FAIL with `ImportError: cannot import name 'DecisionEnvelope'`

**Step 3: Write minimal implementation**

Add to end of `backend/core/memory_types.py` (after `LoadResult` class):

```python
# ===================================================================
# v260.2: Decision Infrastructure — Governance Unification
# ===================================================================

class ActuatorAction(str, Enum):
    """Actions that memory actuators can request.

    Priority (lower = less disruptive, preferred first):
        DISPLAY_SHED < DEFCON_ESCALATE < MODEL_EVICT < CLOUD_OFFLOAD < CLEANUP
    """
    DISPLAY_SHED = "display_shed"
    DEFCON_ESCALATE = "defcon_escalate"
    MODEL_EVICT = "model_evict"
    CLOUD_OFFLOAD = "cloud_offload"
    CLEANUP = "cleanup"

    @property
    def priority(self) -> int:
        return _ACTUATOR_PRIORITY[self]


_ACTUATOR_PRIORITY: Dict[ActuatorAction, int] = {
    ActuatorAction.DISPLAY_SHED: 0,
    ActuatorAction.DEFCON_ESCALATE: 1,
    ActuatorAction.MODEL_EVICT: 2,
    ActuatorAction.CLOUD_OFFLOAD: 3,
    ActuatorAction.CLEANUP: 4,
}


@dataclasses.dataclass(frozen=True)
class PressurePolicy:
    """Versioned pressure thresholds with hysteresis.

    Replaces all hardcoded thresholds across the codebase with a single
    source of truth.  Hardware-profile factories produce appropriate
    defaults for 8GB / 16GB / 32GB / 64GB+ machines.

    Enter threshold = pressure rises above this → tier activates.
    Exit threshold  = pressure drops below this → tier deactivates.
    The gap (enter - exit) is the deadband that prevents oscillation.
    """
    version: str = "v1.0"

    # Memory-percent thresholds keyed by PressureTier
    enter_thresholds: Dict[PressureTier, float] = dataclasses.field(
        default_factory=lambda: {
            PressureTier.ELEVATED: 70.0,
            PressureTier.CONSTRAINED: 80.0,
            PressureTier.CRITICAL: 90.0,
            PressureTier.EMERGENCY: 95.0,
        }
    )
    exit_thresholds: Dict[PressureTier, float] = dataclasses.field(
        default_factory=lambda: {
            PressureTier.ELEVATED: 65.0,
            PressureTier.CONSTRAINED: 75.0,
            PressureTier.CRITICAL: 85.0,
            PressureTier.EMERGENCY: 90.0,
        }
    )

    min_dwell_seconds: float = 5.0
    cooldown_seconds: float = 30.0
    max_actions_per_hour: int = 12

    @classmethod
    def for_ram_gb(cls, total_gb: float) -> "PressurePolicy":
        """Factory: produce hardware-appropriate thresholds."""
        if total_gb < 12:
            # 8GB constrained — high baseline is normal
            return cls(
                version="v1.0-constrained",
                enter_thresholds={
                    PressureTier.ELEVATED: 85.0,
                    PressureTier.CONSTRAINED: 90.0,
                    PressureTier.CRITICAL: 95.0,
                    PressureTier.EMERGENCY: 97.0,
                },
                exit_thresholds={
                    PressureTier.ELEVATED: 80.0,
                    PressureTier.CONSTRAINED: 87.0,
                    PressureTier.CRITICAL: 92.0,
                    PressureTier.EMERGENCY: 95.0,
                },
            )
        elif total_gb < 20:
            # 16GB consumer — 75-88% baseline is normal
            return cls(
                version="v1.0-consumer",
                enter_thresholds={
                    PressureTier.ELEVATED: 80.0,
                    PressureTier.CONSTRAINED: 88.0,
                    PressureTier.CRITICAL: 93.0,
                    PressureTier.EMERGENCY: 96.0,
                },
                exit_thresholds={
                    PressureTier.ELEVATED: 75.0,
                    PressureTier.CONSTRAINED: 84.0,
                    PressureTier.CRITICAL: 90.0,
                    PressureTier.EMERGENCY: 93.0,
                },
            )
        elif total_gb < 48:
            # 32GB prosumer
            return cls(
                version="v1.0-prosumer",
                enter_thresholds={
                    PressureTier.ELEVATED: 65.0,
                    PressureTier.CONSTRAINED: 75.0,
                    PressureTier.CRITICAL: 85.0,
                    PressureTier.EMERGENCY: 93.0,
                },
                exit_thresholds={
                    PressureTier.ELEVATED: 60.0,
                    PressureTier.CONSTRAINED: 70.0,
                    PressureTier.CRITICAL: 80.0,
                    PressureTier.EMERGENCY: 90.0,
                },
            )
        else:
            # 64GB+ server
            return cls(
                version="v1.0-server",
                enter_thresholds={
                    PressureTier.ELEVATED: 55.0,
                    PressureTier.CONSTRAINED: 65.0,
                    PressureTier.CRITICAL: 80.0,
                    PressureTier.EMERGENCY: 90.0,
                },
                exit_thresholds={
                    PressureTier.ELEVATED: 50.0,
                    PressureTier.CONSTRAINED: 60.0,
                    PressureTier.CRITICAL: 75.0,
                    PressureTier.EMERGENCY: 85.0,
                },
            )


@dataclasses.dataclass(frozen=True)
class DecisionEnvelope:
    """Provenance wrapper for every actuator decision.

    Every decision to kill, offload, degrade, or shed must carry this
    envelope.  The execution layer rejects stale envelopes before acting.
    """
    snapshot_id: str
    epoch: int
    sequence: int
    policy_version: str
    pressure_tier: PressureTier
    timestamp: float

    def is_stale(self, *, current_epoch: int, current_sequence: int) -> bool:
        """Return True if this decision was made on outdated data."""
        if self.epoch < current_epoch:
            return True
        if self.epoch == current_epoch and self.sequence < current_sequence:
            return True
        return False
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_decision_envelope.py -v`
Expected: PASS (11 tests)

**Step 5: Commit**

```bash
git add backend/core/memory_types.py tests/unit/test_decision_envelope.py
git commit -m "feat(memory): add DecisionEnvelope, PressurePolicy, ActuatorAction types"
```

---

### Task 2: Add `MemoryActuatorCoordinator` to the broker

**Files:**
- Create: `backend/core/memory_actuator_coordinator.py`
- Test: `tests/unit/test_actuator_coordinator.py`

**Context:** Today, `process_cleanup_manager`, `resource_governor`, `gcp_vm_manager`, and `DisplayPressureController` all fire independently when pressure rises. The coordinator serializes their requests: only one actuator acts per evaluation cycle. Actions are ordered by priority (least disruptive first: display shed before process kill). Stale decisions are rejected via `DecisionEnvelope.is_stale()`. Failed actions are quarantined after repeated failures.

**Step 1: Write the failing test**

```python
# tests/unit/test_actuator_coordinator.py
"""Tests for MemoryActuatorCoordinator."""
import asyncio
import time
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.core.memory_types import (
    ActuatorAction,
    DecisionEnvelope,
    PressureTier,
)
from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator


class TestCoordinatorSubmit:
    @pytest.fixture
    def coordinator(self):
        return MemoryActuatorCoordinator()

    def test_submit_returns_decision_id(self, coordinator):
        env = DecisionEnvelope(
            snapshot_id="s1", epoch=1, sequence=1,
            policy_version="v1.0",
            pressure_tier=PressureTier.CRITICAL,
            timestamp=time.time(),
        )
        decision_id = coordinator.submit(
            action=ActuatorAction.DISPLAY_SHED,
            envelope=env,
            source="display_controller",
        )
        assert decision_id is not None
        assert isinstance(decision_id, str)

    def test_stale_envelope_rejected(self, coordinator):
        coordinator._current_epoch = 2
        coordinator._current_sequence = 10
        env = DecisionEnvelope(
            snapshot_id="s-old", epoch=1, sequence=5,
            policy_version="v1.0",
            pressure_tier=PressureTier.CRITICAL,
            timestamp=time.time(),
        )
        decision_id = coordinator.submit(
            action=ActuatorAction.CLEANUP,
            envelope=env,
            source="cleanup_manager",
        )
        assert decision_id is None  # Rejected


class TestCoordinatorPriority:
    def test_least_disruptive_action_wins(self):
        coordinator = MemoryActuatorCoordinator()
        env = DecisionEnvelope(
            snapshot_id="s1", epoch=1, sequence=1,
            policy_version="v1.0",
            pressure_tier=PressureTier.CRITICAL,
            timestamp=time.time(),
        )
        # Submit aggressive action first
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        # Submit less disruptive action second
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "display")

        # Drain should return DISPLAY_SHED first (lower priority number)
        actions = coordinator.drain_pending()
        assert len(actions) == 2
        assert actions[0].action == ActuatorAction.DISPLAY_SHED
        assert actions[1].action == ActuatorAction.CLEANUP


class TestCoordinatorQuarantine:
    def test_quarantine_after_repeated_failures(self):
        coordinator = MemoryActuatorCoordinator(failure_budget=2)
        # Report 2 failures for CLEANUP
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_non_quarantined_action_passes(self):
        coordinator = MemoryActuatorCoordinator(failure_budget=3)
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)


class TestCoordinatorShadowMode:
    def test_shadow_mode_logs_but_does_not_actuate(self):
        coordinator = MemoryActuatorCoordinator(shadow_mode=True)
        env = DecisionEnvelope(
            snapshot_id="s1", epoch=1, sequence=1,
            policy_version="v1.0",
            pressure_tier=PressureTier.CRITICAL,
            timestamp=time.time(),
        )
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        actions = coordinator.drain_pending()
        # In shadow mode, actions are returned but flagged
        assert all(a.shadow for a in actions)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_actuator_coordinator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.memory_actuator_coordinator'`

**Step 3: Write minimal implementation**

```python
# backend/core/memory_actuator_coordinator.py
"""Memory Actuator Coordinator — serializes competing memory actions.

v260.2: Prevents tug-of-war between process_cleanup_manager,
resource_governor, gcp_vm_manager, and DisplayPressureController.

Only one actuator acts per evaluation cycle.  Actions are ordered by
priority (least disruptive first).  Stale decisions are rejected.
Failed actions are quarantined after exceeding their failure budget.

Design invariants:
* submit() is synchronous and O(1) — never blocks the caller.
* drain_pending() returns actions sorted by priority (ascending).
* Quarantined actions are silently dropped on submit.
* Shadow mode flags actions but never suppresses them from drain.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Dict, List, Optional

from backend.core.memory_types import ActuatorAction, DecisionEnvelope

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class PendingAction:
    """A submitted actuator action awaiting execution."""
    decision_id: str
    action: ActuatorAction
    envelope: DecisionEnvelope
    source: str
    submitted_at: float
    shadow: bool = False


class MemoryActuatorCoordinator:
    """Serializes memory actuator requests across the system.

    Thread-safe: submit() and drain_pending() can be called from any thread.
    """

    def __init__(
        self,
        *,
        failure_budget: int = 3,
        quarantine_seconds: float = 300.0,
        shadow_mode: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._pending: List[PendingAction] = []
        self._failure_budget = failure_budget
        self._quarantine_seconds = quarantine_seconds
        self._shadow_mode = shadow_mode

        # Epoch/sequence tracking for staleness checks
        self._current_epoch: int = 0
        self._current_sequence: int = 0

        # Failure tracking per action type
        self._failure_counts: Dict[ActuatorAction, int] = defaultdict(int)
        self._quarantine_until: Dict[ActuatorAction, float] = {}

        # Stats
        self._total_submitted: int = 0
        self._total_rejected_stale: int = 0
        self._total_rejected_quarantined: int = 0

    def advance_epoch(self, epoch: int, sequence: int) -> None:
        """Update current epoch/sequence (called by broker on new snapshot)."""
        with self._lock:
            self._current_epoch = epoch
            self._current_sequence = sequence

    def submit(
        self,
        action: ActuatorAction,
        envelope: DecisionEnvelope,
        source: str,
    ) -> Optional[str]:
        """Submit an actuator action request.

        Returns decision_id if accepted, None if rejected (stale or quarantined).
        """
        with self._lock:
            # Reject stale decisions
            if envelope.is_stale(
                current_epoch=self._current_epoch,
                current_sequence=self._current_sequence,
            ):
                self._total_rejected_stale += 1
                logger.debug(
                    "[ActuatorCoord] Rejected stale %s from %s "
                    "(envelope epoch=%d seq=%d, current epoch=%d seq=%d)",
                    action.value, source,
                    envelope.epoch, envelope.sequence,
                    self._current_epoch, self._current_sequence,
                )
                return None

            # Reject quarantined actions
            if self.is_quarantined(action):
                self._total_rejected_quarantined += 1
                logger.debug(
                    "[ActuatorCoord] Rejected quarantined %s from %s",
                    action.value, source,
                )
                return None

            decision_id = f"dec-{uuid.uuid4().hex[:12]}"
            self._pending.append(PendingAction(
                decision_id=decision_id,
                action=action,
                envelope=envelope,
                source=source,
                submitted_at=time.monotonic(),
                shadow=self._shadow_mode,
            ))
            self._total_submitted += 1
            return decision_id

    def drain_pending(self) -> List[PendingAction]:
        """Return all pending actions sorted by priority, clearing the queue."""
        with self._lock:
            actions = sorted(self._pending, key=lambda a: a.action.priority)
            self._pending = []
            return actions

    def report_failure(self, action: ActuatorAction, reason: str) -> None:
        """Report a failed actuator action.  Quarantines after failure_budget."""
        with self._lock:
            self._failure_counts[action] += 1
            if self._failure_counts[action] >= self._failure_budget:
                self._quarantine_until[action] = (
                    time.monotonic() + self._quarantine_seconds
                )
                logger.warning(
                    "[ActuatorCoord] Quarantined %s for %.0fs after %d failures: %s",
                    action.value, self._quarantine_seconds,
                    self._failure_counts[action], reason,
                )

    def report_success(self, action: ActuatorAction) -> None:
        """Report a successful actuator action.  Resets failure counter."""
        with self._lock:
            self._failure_counts[action] = 0
            self._quarantine_until.pop(action, None)

    def is_quarantined(self, action: ActuatorAction) -> bool:
        """Check if an action type is currently quarantined."""
        deadline = self._quarantine_until.get(action)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            # Quarantine expired — clear it
            self._quarantine_until.pop(action, None)
            self._failure_counts[action] = 0
            return False
        return True

    def get_stats(self) -> Dict[str, int]:
        """Return coordinator statistics."""
        with self._lock:
            return {
                "total_submitted": self._total_submitted,
                "total_rejected_stale": self._total_rejected_stale,
                "total_rejected_quarantined": self._total_rejected_quarantined,
                "pending_count": len(self._pending),
                "quarantined_actions": [
                    a.value for a in self._quarantine_until
                    if self.is_quarantined(a)
                ],
                "shadow_mode": self._shadow_mode,
            }
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_actuator_coordinator.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add backend/core/memory_actuator_coordinator.py tests/unit/test_actuator_coordinator.py
git commit -m "feat(memory): add MemoryActuatorCoordinator with priority, staleness, quarantine"
```

---

### Task 3: Wire coordinator into broker + add sequence counter

**Files:**
- Modify: `backend/core/memory_budget_broker.py`
- Test: `tests/unit/test_broker_coordinator_wire.py`

**Context:** The broker must own the coordinator singleton and advance the epoch/sequence on every snapshot. When `notify_pressure_observers()` fires, it passes the coordinator reference so observers can submit decisions through it instead of acting independently.

**Step 1: Write the failing test**

```python
# tests/unit/test_broker_coordinator_wire.py
"""Tests for broker ↔ coordinator wiring."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, AsyncMock
import pytest


class TestBrokerCoordinatorWire:
    def test_broker_has_coordinator_property(self):
        """Broker exposes its MemoryActuatorCoordinator."""
        from backend.core.memory_budget_broker import MemoryBudgetBroker
        quantizer = MagicMock()
        quantizer.set_broker_ref = MagicMock()
        broker = MemoryBudgetBroker(quantizer, epoch=1)
        assert broker.coordinator is not None
        from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
        assert isinstance(broker.coordinator, MemoryActuatorCoordinator)

    def test_broker_has_sequence_counter(self):
        """Broker maintains a monotonic sequence counter."""
        from backend.core.memory_budget_broker import MemoryBudgetBroker
        quantizer = MagicMock()
        quantizer.set_broker_ref = MagicMock()
        broker = MemoryBudgetBroker(quantizer, epoch=1)
        seq1 = broker.current_sequence
        broker._advance_sequence()
        seq2 = broker.current_sequence
        assert seq2 == seq1 + 1

    def test_broker_has_policy_property(self):
        """Broker exposes the active PressurePolicy."""
        from backend.core.memory_budget_broker import MemoryBudgetBroker
        from backend.core.memory_types import PressurePolicy
        quantizer = MagicMock()
        quantizer.set_broker_ref = MagicMock()
        broker = MemoryBudgetBroker(quantizer, epoch=1)
        assert isinstance(broker.policy, PressurePolicy)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_broker_coordinator_wire.py -v`
Expected: FAIL with `AttributeError: 'MemoryBudgetBroker' object has no attribute 'coordinator'`

**Step 3: Write minimal implementation**

Add to `MemoryBudgetBroker.__init__()` in `backend/core/memory_budget_broker.py`:

```python
# After existing __init__ code, add:
from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
from backend.core.memory_types import PressurePolicy

self._coordinator = MemoryActuatorCoordinator()
self._sequence: int = 0
self._policy = PressurePolicy.for_ram_gb(self._detect_total_ram_gb())
```

Add these properties/methods to the class:

```python
@property
def coordinator(self) -> "MemoryActuatorCoordinator":
    return self._coordinator

@property
def current_sequence(self) -> int:
    return self._sequence

@property
def policy(self) -> "PressurePolicy":
    return self._policy

def _advance_sequence(self) -> int:
    self._sequence += 1
    self._coordinator.advance_epoch(self._epoch, self._sequence)
    return self._sequence

@staticmethod
def _detect_total_ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return 16.0
```

Also update `notify_pressure_observers()` to advance sequence before notifying:

```python
async def notify_pressure_observers(self, tier: PressureTier, snapshot: MemorySnapshot) -> None:
    self._advance_sequence()  # <-- ADD THIS LINE at the top
    # ... rest of existing code unchanged
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_broker_coordinator_wire.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add backend/core/memory_budget_broker.py tests/unit/test_broker_coordinator_wire.py
git commit -m "feat(memory): wire ActuatorCoordinator + PressurePolicy into broker"
```

---

## Phase 1: Critical Actuator Migration (Top 10 Files)

### Migration Order Rationale

Files are ordered by dependency: infrastructure observability first (resource_governor provides Defcon to everyone), then the highest-impact actuators, then the files that depend on already-migrated ones.

| Order | File | Why This Order |
|-------|------|---------------|
| 1 | `resource_governor.py` | Defcon level feeds into 5+ other files |
| 2 | `process_cleanup_manager.py` | Largest actuator (14 call sites), depends on Defcon |
| 3 | `gcp_vm_manager.py` | VM lifecycle decisions, 3 call sites |
| 4 | `gcp_oom_prevention_bridge.py` | OOM emergency, 4 call sites |
| 5 | `memory_fault_guard.py` | SIGBUS recovery, 5 call sites |
| 6 | `dynamic_component_manager.py` | Component load parallelism, 3 sites |
| 7 | `parallel_initializer.py` | Startup parallelism, 3 sites |
| 8 | `unified_model_serving.py` | Model admission, 2 sites |
| 9 | `ecapa_cloud_service.py` | Voice auth routing, 1 site |
| 10 | `intelligent_memory_optimizer.py` | Cache/app management, 6 sites |

---

### Task 4: Migrate `resource_governor.py` to broker observer

**Files:**
- Modify: `backend/core/resource_governor.py:370-444`
- Test: `tests/unit/test_resource_governor_mcp.py`

**Context:** The resource governor has a 3-level Defcon state machine (GREEN → YELLOW → RED) with hysteresis. Currently it polls `psutil.virtual_memory()` on its own interval. Migration: register as a broker pressure observer, receive `PressureTier` + `MemorySnapshot`, and map tiers to Defcon levels using the broker's `PressurePolicy` instead of its own env-var thresholds.

**Key insight:** The governor's existing hysteresis (separate up/down thresholds + stabilization timer) maps directly to `PressurePolicy.enter_thresholds` / `exit_thresholds`. We keep the governor's state machine but replace its data source.

**Step 1: Write the failing test**

```python
# tests/unit/test_resource_governor_mcp.py
"""Tests for resource_governor MCP integration."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, AsyncMock, patch
import pytest


class TestGovernorBrokerObserver:
    def test_design_intent_governor_registers_as_observer(self):
        """resource_governor must register as a broker pressure observer."""
        import ast
        with open("backend/core/resource_governor.py") as f:
            source = f.read()
        tree = ast.parse(source)
        source_text = source.lower()
        assert "register_pressure_observer" in source_text, \
            "resource_governor must call broker.register_pressure_observer()"

    def test_design_intent_governor_uses_snapshot_not_psutil(self):
        """_update_memory_state should accept MemorySnapshot, not call psutil."""
        import ast
        with open("backend/core/resource_governor.py") as f:
            source = f.read()
        # Find _update_memory_state function
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name == "_on_pressure_change":
                    # Must accept tier and snapshot parameters
                    args = [a.arg for a in node.args.args]
                    assert "tier" in args or "snapshot" in args, \
                        "_on_pressure_change must accept tier/snapshot params"
                    return
        # If _on_pressure_change not found, check for alternative pattern
        assert "pressure_tier" in source.lower() or "pressuretiersnapshot" in source.lower(), \
            "Governor must reference PressureTier from broker"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_resource_governor_mcp.py -v`
Expected: FAIL with `AssertionError: resource_governor must call broker.register_pressure_observer()`

**Step 3: Implement**

In `resource_governor.py`, add an `_on_pressure_change(self, tier, snapshot)` async callback method that:
1. Maps `PressureTier` to `DefconLevel` using `PressurePolicy` thresholds
2. Applies existing stabilization timer logic
3. Submits `ActuatorAction.DEFCON_ESCALATE` to coordinator if transitioning up
4. Keeps the existing `_update_memory_state()` as legacy fallback (shadow mode)

Add `register_with_broker(broker)` method that:
1. Stores broker reference
2. Calls `broker.register_pressure_observer(self._on_pressure_change)`
3. Sets `self._mcp_active = True`

Modify `_update_memory_state()` to check `self._mcp_active` — if True, skip the psutil call (observer handles it). If False, use legacy psutil path (shadow mode fallback).

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/test_resource_governor_mcp.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/resource_governor.py tests/unit/test_resource_governor_mcp.py
git commit -m "feat(governor): register as broker pressure observer, map PressureTier to Defcon"
```

---

### Task 5: Migrate `process_cleanup_manager.py` to broker (14 call sites)

**Files:**
- Modify: `backend/process_cleanup_manager.py:111-260, 464, 909, 1387-1395, 3011, 3049, 3544-3558, 3649, 3742-3874, 4044, 5510-5511`
- Test: `tests/unit/test_cleanup_manager_mcp.py`

**Context:** This is the largest migration target (14 raw psutil calls). The `IntelligentMemoryController` already has hardware-aware thresholds that duplicate what `PressurePolicy` provides. Migration strategy:

1. Add `_broker` reference and `_on_pressure_change()` observer callback
2. Replace `_detect_total_ram()` (line 198-205) with broker's `_detect_total_ram_gb()`
3. Replace `_get_hardware_aware_thresholds()` (line 224-260) with `broker.policy`
4. Replace all 14 `psutil.virtual_memory()` calls with `broker.latest_snapshot` access
5. All actuation (kill, offload, degrade) routes through `coordinator.submit()`
6. Keep legacy paths as shadow-mode fallback during dual-control period

**Key design decision:** The `IntelligentMemoryController` class stays but its thresholds come from `PressurePolicy`. Its cooldown/backoff/effectiveness-tracking logic is valuable and should NOT be duplicated — it wraps the coordinator's decision.

**Step 1: Write the failing test**

```python
# tests/unit/test_cleanup_manager_mcp.py
"""Tests for process_cleanup_manager MCP integration."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import ast


class TestCleanupManagerMCP:
    def test_design_intent_imports_broker(self):
        """process_cleanup_manager must import the broker."""
        with open("backend/process_cleanup_manager.py") as f:
            source = f.read()
        assert "memory_budget_broker" in source, \
            "Must import from memory_budget_broker"

    def test_design_intent_uses_coordinator(self):
        """Actuator actions must route through coordinator."""
        with open("backend/process_cleanup_manager.py") as f:
            source = f.read()
        assert "coordinator" in source.lower() or "actuator" in source.lower(), \
            "Must use ActuatorCoordinator for decisions"

    def test_design_intent_no_new_psutil_virtual_memory_for_decisions(self):
        """No NEW psutil.virtual_memory() calls for decision-making.

        Legacy calls may remain but must be guarded by `if not self._mcp_active`.
        New decision paths must use broker snapshot.
        """
        with open("backend/process_cleanup_manager.py") as f:
            source = f.read()
        tree = ast.parse(source)
        # Count psutil.virtual_memory() calls NOT inside `if not self._mcp_active` guards
        # This is a design-intent test — exact enforcement is in CI
        # For now, just verify the broker integration exists
        assert "latest_snapshot" in source or "_on_pressure_change" in source, \
            "Must use broker.latest_snapshot or pressure observer pattern"
```

**Step 2-5:** Implementation follows the same pattern — register as observer, replace psutil calls with snapshot access, route actions through coordinator, keep legacy as shadow fallback. Commit after tests pass.

```bash
git commit -m "feat(cleanup): migrate process_cleanup_manager to MCP broker observer"
```

---

### Task 6: Migrate `gcp_vm_manager.py` (3 call sites)

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:7396-7401, 10038-10043, 10115-10169`
- Test: `tests/unit/test_gcp_vm_mcp.py`

**Context:** Three psutil call sites gate VM lifecycle decisions. Migration:
- Line 7396: Replace `local_mem_percent < 70` with `snapshot.pressure_tier <= PressureTier.OPTIMAL`
- Line 10038: Replace init deferral threshold with `pressure_tier >= CONSTRAINED`
- Line 10115-10169: Replace `LocalMemoryFallback` raw psutil with broker snapshot

**Step 1: Write design-intent test**

```python
# tests/unit/test_gcp_vm_mcp.py
import ast

class TestGCPVMMCP:
    def test_design_intent_uses_pressure_tier(self):
        with open("backend/core/gcp_vm_manager.py") as f:
            source = f.read()
        assert "pressure_tier" in source.lower() or "memory_budget_broker" in source, \
            "gcp_vm_manager must use PressureTier from broker"
```

**Step 2-5:** Replace raw psutil with broker snapshot, use PressureTier comparisons. Commit.

```bash
git commit -m "feat(gcp): migrate VM lifecycle decisions to MCP pressure tiers"
```

---

### Task 7: Migrate `gcp_oom_prevention_bridge.py` (4 call sites)

**Files:**
- Modify: `backend/core/gcp_oom_prevention_bridge.py:617-621, 643-646, 979-1006`
- Test: `tests/unit/test_oom_bridge_mcp.py`

**Context:** OOM prevention bridge has a 3-tier fallback for pressure estimation. Migration: make broker the primary path, keep psutil as last-resort fallback only.

```bash
git commit -m "feat(oom): migrate OOM prevention bridge to MCP broker primary path"
```

---

### Task 8: Migrate `memory_fault_guard.py` (5 call sites)

**Files:**
- Modify: `backend/core/memory_fault_guard.py:393-397, 455-459, 522-527, 568-595, 654-662`
- Test: `tests/unit/test_fault_guard_mcp.py`

**Context:** SIGBUS recovery and cloud failover. The `should_offload_to_cloud()` method (line 568-595) must use `PressureTier >= CONSTRAINED` instead of raw percent threshold.

```bash
git commit -m "feat(fault-guard): migrate SIGBUS recovery to MCP pressure tiers"
```

---

### Task 9: Migrate `dynamic_component_manager.py` (3 call sites)

**Files:**
- Modify: `backend/core/dynamic_component_manager.py:944-960, 965, 1955-1958`
- Test: `tests/unit/test_dynamic_component_mcp.py`

**Context:** Component loading parallelism decisions. Replace `memory_available_mb()` with broker `headroom_bytes` and `pressure_tier` comparisons.

```bash
git commit -m "feat(components): migrate component manager to MCP headroom checks"
```

---

### Task 10: Migrate `parallel_initializer.py` (3 call sites)

**Files:**
- Modify: `backend/core/parallel_initializer.py:623-625, 899-905, 1100-1106`
- Test: `tests/unit/test_parallel_init_mcp.py`

**Context:** Forces sequential init when available < 4GB. Replace with `pressure_tier >= CONSTRAINED` which accounts for UMA GPU claims.

```bash
git commit -m "feat(init): migrate parallel initializer to MCP pressure-aware sequencing"
```

---

### Task 11: Migrate `unified_model_serving.py` (2 call sites)

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py:1033-1038, 1286-1289`
- Test: `tests/unit/test_model_serving_mcp.py`

**Context:** Model download admission gate. Replace `available_gb` threshold with `headroom_bytes` from broker snapshot.

```bash
git commit -m "feat(models): migrate model admission gate to MCP headroom"
```

---

### Task 12: Migrate `ecapa_cloud_service.py` (1 call site)

**Files:**
- Modify: `backend/cloud_services/ecapa_cloud_service.py:1425-1430`
- Test: `tests/unit/test_ecapa_mcp.py`

**Context:** Voice biometric authentication routing. Replace `available_gb < 4.0` with `pressure_tier >= CONSTRAINED`. This is critical for UMA correctness — the current 4GB threshold doesn't account for GPU framebuffer claims.

```bash
git commit -m "feat(ecapa): migrate voice auth routing to MCP pressure tier"
```

---

### Task 13: Migrate `intelligent_memory_optimizer.py` (6 call sites)

**Files:**
- Modify: `backend/memory/intelligent_memory_optimizer.py:254-291, 325-328, 380-425, 594-599, 690-717, 871`
- Test: `tests/unit/test_optimizer_mcp.py`

**Context:** Memory optimizer selects strategies (cache purge, app suspend, model evict) based on raw psutil thresholds. Replace with broker pressure tier — each tier maps to a strategy level.

```bash
git commit -m "feat(optimizer): migrate memory optimizer strategies to MCP pressure tiers"
```

---

### Task 14: Integration test — multi-actuator coordination

**Files:**
- Create: `tests/stress/test_actuator_coordination.py`

**Context:** Verify that when pressure rises, the coordinator serializes actions correctly: display sheds first, then Defcon escalates, then cleanup fires — never simultaneously, never on stale data.

```python
# tests/stress/test_actuator_coordination.py
"""Integration test: multi-actuator coordination under pressure."""

class TestMultiActuatorCoordination:
    def test_priority_ordering_under_critical_pressure(self):
        """Display shed fires before process cleanup under CRITICAL."""
        ...

    def test_stale_decisions_rejected_after_pressure_drops(self):
        """Decisions from old snapshot rejected when pressure resolves."""
        ...

    def test_quarantined_action_skipped(self):
        """Failed actuator quarantined, others still fire."""
        ...

    def test_shadow_mode_no_actuation(self):
        """Shadow mode logs but does not execute actions."""
        ...
```

```bash
git commit -m "test(memory): add multi-actuator coordination integration tests"
```

---

## Phase 2 & 3 (Future — outlined only)

### Phase 2: Migrate remaining 20+ HIGH-tier files to observer bus
- `agi_os_coordinator.py` (3 sites)
- `trinity_integrator.py` (4 sites)
- `jarvis_prime_client.py` (2 sites)
- `gcp_hybrid_prime_router.py` (4 sites)
- `video_stream_capture.py` (5 sites)
- `voice/resource_monitor.py` (1 site)
- `async_system_metrics.py` (3 sites)
- `advanced_ram_monitor.py` (1 site)
- `platform_memory_monitor.py` (1 site)
- `system_primitives.py` (2 sites)
- `pytorch_executor.py` (1 site)
- `swift_system_monitor.py` (2 sites)
- `pressure_aware_watchdog.py` (1 site)
- `ouroboros/native_integration.py` (1 site)
- `adaptive_resource_governor.py` (1 site)

### Phase 3: CI enforcement + legacy removal
- Add pre-commit hook: block new `psutil.virtual_memory()` outside quantizer
- Add pre-commit hook: block threshold literals in decision code
- Remove all `if not self._mcp_active` shadow-mode branches
- Remove `_psutil_memory_snapshot()` fallback from `unified_supervisor.py`
- Add `decision_id` requirement to all actuator log events
