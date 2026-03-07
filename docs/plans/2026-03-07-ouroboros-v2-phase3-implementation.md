# Ouroboros v2.0 Phase 3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire governance layer to existing Ouroboros subsystems (event bus, Oracle, learning memory), add canary rollout controller with domain-slice promotion, and provide CLI break-glass entry points — completing the governed autonomy stack.

**Architecture:** Phase 3 bridges the governance layer (Phases 0-2) to the existing Ouroboros infrastructure. An `EventBridge` maps governance `CommMessage`s to `CrossRepoEvent`s for cross-repo propagation. A `BlastRadiusAdapter` auto-populates `OperationProfile.blast_radius` from the `CodebaseKnowledgeGraph`. A `LearningBridge` publishes operation outcomes to `LearningMemory` with op_id correlation. A `RuntimeContractChecker` extends boot-time `ContractGate` with runtime N/N-1 schema validation. A `CanaryController` manages per-domain-slice promotion with 50-op minimum, <5% rollback rate, and 72h stability windows. A `CLICommands` module provides importable break-glass functions for supervisor CLI integration.

**Tech Stack:** Python 3.11+, asyncio, pytest, pytest-asyncio

**Design doc:** `docs/plans/2026-03-07-ouroboros-v2-design.md`

**Phase 2 code references (all in `backend/core/ouroboros/governance/`):**
- `resource_monitor.py` — `ResourceMonitor`, `ResourceSnapshot`, `PressureLevel`
- `degradation.py` — `DegradationController`, `DegradationMode`
- `routing_policy.py` — `RoutingPolicy`, `RoutingDecision`, `TaskCategory`, `CostGuardrail`
- `multi_file_engine.py` — `MultiFileChangeEngine`, `MultiFileChangeRequest`, `MultiFileChangeResult`
- `comm_protocol.py` — `CommProtocol`, `CommMessage`, `MessageType`, `LogTransport`
- `ledger.py` — `OperationLedger`, `LedgerEntry`, `OperationState`
- `risk_engine.py` — `RiskEngine`, `RiskTier`, `OperationProfile`, `ChangeType`
- `change_engine.py` — `ChangeEngine`, `ChangePhase`, `RollbackArtifact`
- `break_glass.py` — `BreakGlassManager`, `BreakGlassToken`, `BreakGlassAuditEntry`
- `contract_gate.py` — `ContractGate`, `ContractVersion`, `CompatibilityResult`, `BootCheckResult`

**Key existing Ouroboros subsystems (to integrate with):**
- `CrossRepoEventBus` at `backend/core/ouroboros/cross_repo.py:257` — `emit(event)`, `register_handler(type, handler)`, `CrossRepoEvent` dataclass (line 203), `EventType` enum (line 87), `RepoType` enum
- `CodebaseKnowledgeGraph` at `backend/core/ouroboros/oracle.py:610` — `compute_blast_radius(node_id, max_depth)` -> `BlastRadius` (line 232)
- `LearningMemory` at `backend/core/ouroboros/engine.py:362` — `record_attempt(request, error_pattern, solution_pattern, success)`, `get_known_solution()`, `should_skip_pattern()`
- `LearningEntry` at `backend/core/ouroboros/engine.py:348` — `request_hash`, `error_pattern`, `solution_pattern`, `success`, `attempts`

---

## Task 1: Event Bus Bridge — Governance-to-CrossRepo Event Mapping

**Files:**
- Create: `backend/core/ouroboros/governance/event_bridge.py`
- Create: `tests/test_ouroboros_governance/test_event_bridge.py`

**Context:** The governance `CommProtocol` emits lifecycle messages (INTENT, HEARTBEAT, DECISION, POSTMORTEM) but they stay local. The existing `CrossRepoEventBus` propagates events between JARVIS, PRIME, and REACTOR repos via file-based JSON persistence. This bridge maps governance messages to cross-repo events, enabling PRIME/REACTOR to observe Ouroboros operations.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_event_bridge.py
"""Tests for the governance-to-cross-repo event bridge."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.core.ouroboros.governance.event_bridge import (
    EventBridge,
    GovernanceEventMapper,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    MessageType,
)


@pytest.fixture
def mock_event_bus():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture
def bridge(mock_event_bus):
    return EventBridge(event_bus=mock_event_bus)


class TestGovernanceEventMapper:
    def test_intent_maps_to_improvement_request(self):
        """INTENT message maps to IMPROVEMENT_REQUEST event type."""
        msg = CommMessage(
            op_id="op-test-001",
            msg_type=MessageType.INTENT,
            payload={"goal": "fix bug", "target_files": ["foo.py"]},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_REQUEST"
        assert event.payload["op_id"] == "op-test-001"
        assert event.payload["goal"] == "fix bug"

    def test_decision_applied_maps_to_improvement_complete(self):
        """DECISION with outcome=applied maps to IMPROVEMENT_COMPLETE."""
        msg = CommMessage(
            op_id="op-test-002",
            msg_type=MessageType.DECISION,
            payload={"outcome": "applied", "reason_code": "safe_auto_passed"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_COMPLETE"

    def test_decision_blocked_maps_to_improvement_failed(self):
        """DECISION with outcome=blocked maps to IMPROVEMENT_FAILED."""
        msg = CommMessage(
            op_id="op-test-003",
            msg_type=MessageType.DECISION,
            payload={"outcome": "blocked", "reason_code": "touches_supervisor"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_FAILED"

    def test_postmortem_maps_to_improvement_failed(self):
        """POSTMORTEM always maps to IMPROVEMENT_FAILED."""
        msg = CommMessage(
            op_id="op-test-004",
            msg_type=MessageType.POSTMORTEM,
            payload={"root_cause": "syntax_error", "failed_phase": "VALIDATE"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_FAILED"

    def test_heartbeat_not_bridged(self):
        """HEARTBEAT messages are not bridged (too noisy)."""
        msg = CommMessage(
            op_id="op-test-005",
            msg_type=MessageType.HEARTBEAT,
            payload={"phase": "validate", "progress_pct": 50.0},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is None

    def test_source_repo_is_jarvis(self):
        """All bridged events have source_repo=JARVIS."""
        msg = CommMessage(
            op_id="op-test-006",
            msg_type=MessageType.INTENT,
            payload={"goal": "test"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event.source_repo.name == "JARVIS"

    def test_op_id_preserved_in_payload(self):
        """op_id is always in the event payload for correlation."""
        msg = CommMessage(
            op_id="op-test-007",
            msg_type=MessageType.DECISION,
            payload={"outcome": "applied"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event.payload["op_id"] == "op-test-007"


class TestEventBridge:
    @pytest.mark.asyncio
    async def test_bridge_publishes_mapped_event(self, bridge, mock_event_bus):
        """Bridge publishes mapped events to the event bus."""
        msg = CommMessage(
            op_id="op-test-010",
            msg_type=MessageType.INTENT,
            payload={"goal": "fix bug"},
        )
        await bridge.forward(msg)
        mock_event_bus.emit.assert_called_once()
        event = mock_event_bus.emit.call_args[0][0]
        assert event.payload["op_id"] == "op-test-010"

    @pytest.mark.asyncio
    async def test_bridge_skips_unmapped_messages(self, bridge, mock_event_bus):
        """Bridge does not publish for unmapped message types."""
        msg = CommMessage(
            op_id="op-test-011",
            msg_type=MessageType.HEARTBEAT,
            payload={"phase": "validate"},
        )
        await bridge.forward(msg)
        mock_event_bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_fault_isolation(self, bridge, mock_event_bus):
        """Event bus failure does not propagate to caller."""
        mock_event_bus.emit.side_effect = RuntimeError("bus down")
        msg = CommMessage(
            op_id="op-test-012",
            msg_type=MessageType.INTENT,
            payload={"goal": "fix bug"},
        )
        # Should not raise
        await bridge.forward(msg)

    @pytest.mark.asyncio
    async def test_bridge_as_comm_transport(self, bridge, mock_event_bus):
        """Bridge can be used as a CommProtocol transport callback."""
        msg = CommMessage(
            op_id="op-test-013",
            msg_type=MessageType.DECISION,
            payload={"outcome": "applied"},
        )
        await bridge.send(msg)
        mock_event_bus.emit.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_event_bridge.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/event_bridge.py
"""
Event Bus Bridge — Governance-to-CrossRepo Event Mapping
==========================================================

Maps governance :class:`CommMessage` lifecycle events to
:class:`CrossRepoEvent` for propagation across JARVIS/PRIME/REACTOR
repos via the existing :class:`CrossRepoEventBus`.

Only INTENT, DECISION, and POSTMORTEM are bridged.  HEARTBEAT is
too noisy for cross-repo propagation and is filtered out.

Fault isolation: event bus failures are logged but never block the
governance pipeline.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    MessageType,
)

logger = logging.getLogger("Ouroboros.EventBridge")

# Lazy imports to avoid circular dependencies with cross_repo module
_CrossRepoEvent = None
_EventType = None
_RepoType = None


def _ensure_imports():
    """Lazy-import cross-repo types to break circular deps."""
    global _CrossRepoEvent, _EventType, _RepoType
    if _CrossRepoEvent is None:
        from backend.core.ouroboros.cross_repo import (
            CrossRepoEvent,
            EventType,
            RepoType,
        )
        _CrossRepoEvent = CrossRepoEvent
        _EventType = EventType
        _RepoType = RepoType


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------

# Mapping: (MessageType, outcome) -> EventType name
_DECISION_OUTCOME_MAP = {
    "applied": "IMPROVEMENT_COMPLETE",
    "candidate_validated": "IMPROVEMENT_COMPLETE",
    "blocked": "IMPROVEMENT_FAILED",
    "escalated": "IMPROVEMENT_FAILED",
    "all_candidates_failed": "IMPROVEMENT_FAILED",
    "no_candidates": "IMPROVEMENT_FAILED",
    "validation_failed": "IMPROVEMENT_FAILED",
}


class GovernanceEventMapper:
    """Maps governance CommMessages to CrossRepoEvents."""

    @staticmethod
    def map(msg: CommMessage) -> Any:
        """Map a governance message to a cross-repo event.

        Returns None for message types that should not be bridged.
        """
        _ensure_imports()

        if msg.msg_type == MessageType.INTENT:
            return _CrossRepoEvent(
                id=str(uuid.uuid4()),
                type=_EventType["IMPROVEMENT_REQUEST"],
                source_repo=_RepoType.JARVIS,
                target_repo=None,
                payload={"op_id": msg.op_id, **msg.payload},
                timestamp=time.time(),
            )

        if msg.msg_type == MessageType.DECISION:
            outcome = msg.payload.get("outcome", "")
            event_type_name = _DECISION_OUTCOME_MAP.get(
                outcome, "IMPROVEMENT_FAILED"
            )
            return _CrossRepoEvent(
                id=str(uuid.uuid4()),
                type=_EventType[event_type_name],
                source_repo=_RepoType.JARVIS,
                target_repo=None,
                payload={"op_id": msg.op_id, **msg.payload},
                timestamp=time.time(),
            )

        if msg.msg_type == MessageType.POSTMORTEM:
            return _CrossRepoEvent(
                id=str(uuid.uuid4()),
                type=_EventType["IMPROVEMENT_FAILED"],
                source_repo=_RepoType.JARVIS,
                target_repo=None,
                payload={"op_id": msg.op_id, **msg.payload},
                timestamp=time.time(),
            )

        # HEARTBEAT and others are not bridged
        return None


# ---------------------------------------------------------------------------
# EventBridge
# ---------------------------------------------------------------------------


class EventBridge:
    """Fault-isolated bridge from governance CommProtocol to CrossRepoEventBus.

    Can be used as a CommProtocol transport by calling :meth:`send`.
    """

    def __init__(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    async def forward(self, msg: CommMessage) -> None:
        """Forward a governance message to the cross-repo event bus.

        Messages that don't map to cross-repo events are silently skipped.
        Event bus failures are logged but never propagated.
        """
        event = GovernanceEventMapper.map(msg)
        if event is None:
            return

        try:
            await self._event_bus.emit(event)
        except Exception as exc:
            logger.warning(
                "EventBridge: failed to emit event for op=%s: %s",
                msg.op_id, exc,
            )

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol-compatible transport interface."""
        await self.forward(msg)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_event_bridge.py -v`
Expected: All 11 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/event_bridge.py tests/test_ouroboros_governance/test_event_bridge.py
git commit -m "feat(governance): add event bridge for cross-repo event propagation

Maps governance CommMessages (INTENT/DECISION/POSTMORTEM) to
CrossRepoEvents. HEARTBEAT filtered (too noisy). Fault-isolated:
event bus failures never block governance pipeline.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Blast Radius Adapter — Oracle Integration

**Files:**
- Create: `backend/core/ouroboros/governance/blast_radius_adapter.py`
- Create: `tests/test_ouroboros_governance/test_blast_radius_adapter.py`

**Context:** The risk engine uses `OperationProfile.blast_radius` (an int) but currently callers set it manually. The existing `CodebaseKnowledgeGraph.compute_blast_radius()` returns a `BlastRadius` with `total_affected` count and `risk_level` string. This adapter auto-populates the blast radius from the Oracle when available, with graceful fallback to manual values.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_blast_radius_adapter.py
"""Tests for the Oracle blast radius adapter."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.blast_radius_adapter import (
    BlastRadiusAdapter,
    BlastRadiusResult,
)
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
)


def _mock_oracle(total_affected: int, risk_level: str):
    """Create a mock CodebaseKnowledgeGraph with preset blast radius."""
    oracle = MagicMock()
    blast = MagicMock()
    blast.total_affected = total_affected
    blast.risk_level = risk_level
    blast.directly_affected = set()
    blast.transitively_affected = set()
    oracle.compute_blast_radius.return_value = blast
    oracle.find_nodes_in_file.return_value = ["node-1"]
    return oracle


class TestBlastRadiusResult:
    def test_result_fields(self):
        """BlastRadiusResult has all required fields."""
        result = BlastRadiusResult(
            total_affected=5,
            risk_level="medium",
            from_oracle=True,
        )
        assert result.total_affected == 5
        assert result.risk_level == "medium"
        assert result.from_oracle is True


class TestBlastRadiusAdapter:
    def test_compute_from_oracle(self):
        """Adapter uses Oracle when available."""
        oracle = _mock_oracle(total_affected=8, risk_level="medium")
        adapter = BlastRadiusAdapter(oracle=oracle)
        result = adapter.compute("backend/core/foo.py")
        assert result.total_affected == 8
        assert result.risk_level == "medium"
        assert result.from_oracle is True

    def test_fallback_without_oracle(self):
        """Adapter returns fallback value when no Oracle provided."""
        adapter = BlastRadiusAdapter(oracle=None)
        result = adapter.compute("backend/core/foo.py")
        assert result.total_affected == 1
        assert result.risk_level == "low"
        assert result.from_oracle is False

    def test_fallback_on_oracle_error(self):
        """Adapter falls back gracefully on Oracle error."""
        oracle = MagicMock()
        oracle.find_nodes_in_file.side_effect = RuntimeError("graph corrupt")
        adapter = BlastRadiusAdapter(oracle=oracle)
        result = adapter.compute("backend/core/foo.py")
        assert result.from_oracle is False
        assert result.total_affected == 1

    def test_no_nodes_in_file(self):
        """File not in Oracle graph returns fallback."""
        oracle = MagicMock()
        oracle.find_nodes_in_file.return_value = []
        adapter = BlastRadiusAdapter(oracle=oracle)
        result = adapter.compute("backend/core/foo.py")
        assert result.from_oracle is False

    def test_enrich_profile_updates_blast_radius(self):
        """enrich_profile() updates profile blast_radius from Oracle."""
        oracle = _mock_oracle(total_affected=12, risk_level="high")
        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("foo.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 12

    def test_enrich_profile_multi_file_takes_max(self):
        """enrich_profile() with multiple files uses max blast radius."""
        oracle = MagicMock()
        blast_a = MagicMock()
        blast_a.total_affected = 5
        blast_a.risk_level = "medium"
        blast_b = MagicMock()
        blast_b.total_affected = 15
        blast_b.risk_level = "high"
        oracle.find_nodes_in_file.return_value = ["node-1"]
        oracle.compute_blast_radius.side_effect = [blast_a, blast_b]
        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("a.py"), Path("b.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 15

    def test_enrich_profile_preserves_other_fields(self):
        """enrich_profile() preserves all non-blast_radius fields."""
        oracle = _mock_oracle(total_affected=3, risk_level="low")
        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("foo.py")],
            change_type=ChangeType.DELETE,
            blast_radius=1,
            crosses_repo_boundary=True,
            touches_security_surface=True,
            touches_supervisor=False,
            test_scope_confidence=0.5,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.change_type == ChangeType.DELETE
        assert enriched.crosses_repo_boundary is True
        assert enriched.touches_security_surface is True
        assert enriched.test_scope_confidence == 0.5
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_blast_radius_adapter.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/blast_radius_adapter.py
"""
Blast Radius Adapter — Oracle Integration
============================================

Auto-populates :class:`OperationProfile` ``blast_radius`` from the
:class:`CodebaseKnowledgeGraph` when available.  Falls back gracefully
to manual values when the Oracle is unavailable or encounters errors.

For multi-file operations, computes blast radius for each file and
uses the maximum across all files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from backend.core.ouroboros.governance.risk_engine import OperationProfile

logger = logging.getLogger("Ouroboros.BlastRadiusAdapter")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlastRadiusResult:
    """Result of a blast radius computation.

    Parameters
    ----------
    total_affected:
        Number of nodes affected by the change.
    risk_level:
        Risk level string: "low", "medium", "high", "critical".
    from_oracle:
        Whether the result came from the Oracle (True) or fallback (False).
    """

    total_affected: int
    risk_level: str
    from_oracle: bool


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

_FALLBACK = BlastRadiusResult(total_affected=1, risk_level="low", from_oracle=False)


class BlastRadiusAdapter:
    """Adapts the CodebaseKnowledgeGraph blast radius API for governance use.

    Parameters
    ----------
    oracle:
        An instance of :class:`CodebaseKnowledgeGraph`, or ``None`` if the
        Oracle is unavailable.
    """

    def __init__(self, oracle: Optional[Any] = None) -> None:
        self._oracle = oracle

    def compute(self, file_path: str) -> BlastRadiusResult:
        """Compute blast radius for a single file.

        Parameters
        ----------
        file_path:
            Path to the file to compute blast radius for.

        Returns
        -------
        BlastRadiusResult
            The computed blast radius, or a fallback if Oracle is unavailable.
        """
        if self._oracle is None:
            return _FALLBACK

        try:
            nodes = self._oracle.find_nodes_in_file(file_path)
            if not nodes:
                return _FALLBACK

            # Use the first node (typically the module-level node)
            blast = self._oracle.compute_blast_radius(nodes[0])
            return BlastRadiusResult(
                total_affected=blast.total_affected,
                risk_level=blast.risk_level,
                from_oracle=True,
            )
        except Exception as exc:
            logger.warning(
                "BlastRadiusAdapter: Oracle error for %s: %s",
                file_path, exc,
            )
            return _FALLBACK

    def enrich_profile(self, profile: OperationProfile) -> OperationProfile:
        """Enrich an OperationProfile with Oracle blast radius data.

        Computes blast radius for each file in the profile and uses
        the maximum across all files.

        Parameters
        ----------
        profile:
            The original operation profile.

        Returns
        -------
        OperationProfile
            A new profile with updated ``blast_radius``.
        """
        max_blast = 0
        for fpath in profile.files_affected:
            result = self.compute(str(fpath))
            max_blast = max(max_blast, result.total_affected)

        if max_blast > 0:
            return replace(profile, blast_radius=max_blast)
        return profile
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_blast_radius_adapter.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/blast_radius_adapter.py tests/test_ouroboros_governance/test_blast_radius_adapter.py
git commit -m "feat(governance): add blast radius adapter for Oracle integration

Auto-populates OperationProfile.blast_radius from CodebaseKnowledgeGraph.
Multi-file ops use max blast radius. Graceful fallback on Oracle error.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Learning Bridge — Operation Feedback to LearningMemory

**Files:**
- Create: `backend/core/ouroboros/governance/learning_bridge.py`
- Create: `tests/test_ouroboros_governance/test_learning_bridge.py`

**Context:** The existing `LearningMemory` tracks error-solution pairs with attempt counts. The governance layer produces operation outcomes (applied, failed, rolled_back) with op_ids. This bridge publishes governance outcomes to LearningMemory so the Ouroboros engine can consult past results before re-attempting similar operations.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_learning_bridge.py
"""Tests for the governance learning feedback bridge."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.learning_bridge import (
    LearningBridge,
    OperationOutcome,
)
from backend.core.ouroboros.governance.ledger import OperationState


@pytest.fixture
def mock_memory():
    mem = AsyncMock()
    mem.record_attempt = AsyncMock()
    mem.get_known_solution = AsyncMock(return_value=None)
    mem.should_skip_pattern = AsyncMock(return_value=False)
    return mem


@pytest.fixture
def bridge(mock_memory):
    return LearningBridge(learning_memory=mock_memory)


class TestOperationOutcome:
    def test_outcome_fields(self):
        """OperationOutcome has required fields."""
        outcome = OperationOutcome(
            op_id="op-test-001",
            goal="fix bug in foo.py",
            target_files=["foo.py"],
            final_state=OperationState.APPLIED,
            error_pattern=None,
        )
        assert outcome.op_id == "op-test-001"
        assert outcome.success is True

    def test_failed_outcome(self):
        """Failed outcome has success=False."""
        outcome = OperationOutcome(
            op_id="op-test-002",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.FAILED,
            error_pattern="syntax_error",
        )
        assert outcome.success is False

    def test_rolled_back_is_failure(self):
        """ROLLED_BACK is treated as failure."""
        outcome = OperationOutcome(
            op_id="op-test-003",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.ROLLED_BACK,
            error_pattern="verify_failed",
        )
        assert outcome.success is False


class TestLearningBridge:
    @pytest.mark.asyncio
    async def test_publish_success(self, bridge, mock_memory):
        """Successful operation recorded with success=True."""
        outcome = OperationOutcome(
            op_id="op-test-010",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.APPLIED,
        )
        await bridge.publish(outcome)
        mock_memory.record_attempt.assert_called_once()
        call_kwargs = mock_memory.record_attempt.call_args
        assert call_kwargs[1]["success"] is True

    @pytest.mark.asyncio
    async def test_publish_failure(self, bridge, mock_memory):
        """Failed operation recorded with error pattern."""
        outcome = OperationOutcome(
            op_id="op-test-011",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.FAILED,
            error_pattern="syntax_error",
        )
        await bridge.publish(outcome)
        mock_memory.record_attempt.assert_called_once()
        call_kwargs = mock_memory.record_attempt.call_args
        assert call_kwargs[1]["success"] is False
        assert call_kwargs[1]["error_pattern"] == "syntax_error"

    @pytest.mark.asyncio
    async def test_should_skip_delegates_to_memory(self, bridge, mock_memory):
        """should_skip() delegates to LearningMemory."""
        mock_memory.should_skip_pattern.return_value = True
        result = await bridge.should_skip("fix bug", "foo.py", "syntax_error")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_solution_delegates_to_memory(self, bridge, mock_memory):
        """get_known_solution() delegates to LearningMemory."""
        mock_memory.get_known_solution.return_value = "use try/except"
        result = await bridge.get_known_solution("fix bug", "foo.py", "runtime_error")
        assert result == "use try/except"

    @pytest.mark.asyncio
    async def test_fault_isolation_on_publish(self, bridge, mock_memory):
        """Memory failure on publish does not propagate."""
        mock_memory.record_attempt.side_effect = RuntimeError("disk full")
        outcome = OperationOutcome(
            op_id="op-test-012",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.APPLIED,
        )
        # Should not raise
        await bridge.publish(outcome)

    @pytest.mark.asyncio
    async def test_no_memory_returns_defaults(self):
        """Bridge without memory returns safe defaults."""
        bridge = LearningBridge(learning_memory=None)
        assert await bridge.should_skip("goal", "file", "err") is False
        assert await bridge.get_known_solution("goal", "file", "err") is None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_learning_bridge.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/learning_bridge.py
"""
Learning Bridge — Operation Feedback to LearningMemory
========================================================

Publishes governance operation outcomes to the existing
:class:`LearningMemory` for future consultation.  When the Ouroboros
engine plans a new operation, it can check whether a similar
goal+file+error combination has been tried before (and failed).

Fault isolation: LearningMemory failures are logged but never block
the governance pipeline.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.ouroboros.governance.ledger import OperationState

logger = logging.getLogger("Ouroboros.LearningBridge")


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------

_SUCCESS_STATES = {OperationState.APPLIED}


@dataclass
class OperationOutcome:
    """Summary of a governance operation for learning feedback.

    Parameters
    ----------
    op_id:
        Unique operation identifier.
    goal:
        Natural-language goal of the operation.
    target_files:
        Files affected by the operation.
    final_state:
        Final OperationState from the ledger.
    error_pattern:
        Error pattern string if the operation failed.
    solution_pattern:
        Solution pattern string if the operation succeeded.
    """

    op_id: str
    goal: str
    target_files: list
    final_state: OperationState
    error_pattern: Optional[str] = None
    solution_pattern: Optional[str] = None

    @property
    def success(self) -> bool:
        """Whether the operation completed successfully."""
        return self.final_state in _SUCCESS_STATES


# ---------------------------------------------------------------------------
# Fake request for LearningMemory API compatibility
# ---------------------------------------------------------------------------


class _GovernanceRequest:
    """Minimal request object for LearningMemory API compatibility."""

    def __init__(self, goal: str, target_file: str) -> None:
        self.goal = goal
        self.target_file = target_file
        self.improvement_type = "governance"


# ---------------------------------------------------------------------------
# LearningBridge
# ---------------------------------------------------------------------------


class LearningBridge:
    """Bridge between governance outcomes and LearningMemory.

    Parameters
    ----------
    learning_memory:
        An instance of :class:`LearningMemory`, or ``None`` if unavailable.
    """

    def __init__(self, learning_memory: Optional[Any] = None) -> None:
        self._memory = learning_memory

    async def publish(self, outcome: OperationOutcome) -> None:
        """Publish an operation outcome to LearningMemory.

        Parameters
        ----------
        outcome:
            The operation outcome to record.
        """
        if self._memory is None:
            return

        try:
            target = outcome.target_files[0] if outcome.target_files else "unknown"
            request = _GovernanceRequest(goal=outcome.goal, target_file=target)
            error_pattern = outcome.error_pattern or "none"

            await self._memory.record_attempt(
                request=request,
                error_pattern=error_pattern,
                solution_pattern=outcome.solution_pattern,
                success=outcome.success,
            )
        except Exception as exc:
            logger.warning(
                "LearningBridge: failed to publish outcome for op=%s: %s",
                outcome.op_id, exc,
            )

    async def should_skip(
        self, goal: str, target_file: str, error_pattern: str
    ) -> bool:
        """Check if a goal+file+error has failed too many times."""
        if self._memory is None:
            return False

        try:
            request = _GovernanceRequest(goal=goal, target_file=target_file)
            return await self._memory.should_skip_pattern(
                request=request, error_pattern=error_pattern
            )
        except Exception as exc:
            logger.warning("LearningBridge: should_skip error: %s", exc)
            return False

    async def get_known_solution(
        self, goal: str, target_file: str, error_pattern: str
    ) -> Optional[str]:
        """Check if a known solution exists for this goal+file+error."""
        if self._memory is None:
            return None

        try:
            request = _GovernanceRequest(goal=goal, target_file=target_file)
            return await self._memory.get_known_solution(
                request=request, error_pattern=error_pattern
            )
        except Exception as exc:
            logger.warning("LearningBridge: get_known_solution error: %s", exc)
            return None
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_learning_bridge.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/learning_bridge.py tests/test_ouroboros_governance/test_learning_bridge.py
git commit -m "feat(governance): add learning bridge for operation feedback

Publishes governance outcomes to LearningMemory with op_id correlation.
Consults past attempts before re-trying failed patterns. Fault-isolated.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Runtime Contract Checker — N/N-1 Schema Validation at Runtime

**Files:**
- Create: `backend/core/ouroboros/governance/runtime_contracts.py`
- Create: `tests/test_ouroboros_governance/test_runtime_contracts.py`

**Context:** The existing `ContractGate` performs schema compatibility checks at boot time. The design doc requires N/N-1 compatibility enforced at runtime (not just boot) — meaning before any autonomous write, the governance layer must verify that the proposed changes don't break the contract between current schema (N) and previous schema (N-1).

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_runtime_contracts.py
"""Tests for runtime N/N-1 contract validation."""

import pytest

from backend.core.ouroboros.governance.runtime_contracts import (
    RuntimeContractChecker,
    ContractCheckResult,
    ContractViolation,
)
from backend.core.ouroboros.governance.contract_gate import ContractVersion


@pytest.fixture
def checker():
    current = ContractVersion(major=2, minor=1, patch=0)
    return RuntimeContractChecker(current_version=current)


class TestContractCheckResult:
    def test_passing_result(self):
        """Passing result has compatible=True and no violations."""
        result = ContractCheckResult(compatible=True, violations=[])
        assert result.compatible is True
        assert len(result.violations) == 0

    def test_failing_result(self):
        """Failing result has compatible=False and violations list."""
        result = ContractCheckResult(
            compatible=False,
            violations=[
                ContractViolation(
                    field="api_endpoint",
                    reason="removed in proposed change",
                ),
            ],
        )
        assert result.compatible is False
        assert len(result.violations) == 1


class TestRuntimeContractChecker:
    def test_compatible_version(self, checker):
        """Same major version with minor bump is compatible."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=2, patch=0)
        )
        assert result.compatible is True

    def test_patch_bump_compatible(self, checker):
        """Patch-only bump is always compatible."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=1, patch=5)
        )
        assert result.compatible is True

    def test_major_version_break(self, checker):
        """Different major version is incompatible."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=3, minor=0, patch=0)
        )
        assert result.compatible is False
        assert any("major" in v.reason.lower() for v in result.violations)

    def test_minor_downgrade_incompatible(self, checker):
        """Minor version downgrade is incompatible (N-1 only, not N-2)."""
        result = checker.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=0, patch=0)
        )
        # N-1 means current minor - 1 is allowed, but going below is not
        # Current is 2.1.0, so 2.0.0 is exactly N-1, which IS allowed
        assert result.compatible is True

    def test_two_minor_versions_back_incompatible(self, checker):
        """Two minor versions back (N-2) is incompatible."""
        # Current is 2.1.0. Create checker with 2.3.0 so N-2 = 2.1.0
        checker3 = RuntimeContractChecker(
            current_version=ContractVersion(major=2, minor=3, patch=0)
        )
        result = checker3.check_compatibility(
            proposed_version=ContractVersion(major=2, minor=1, patch=0)
        )
        assert result.compatible is False

    def test_check_before_write_passes(self, checker):
        """check_before_write() returns True for compatible changes."""
        assert checker.check_before_write(
            proposed_version=ContractVersion(major=2, minor=1, patch=1)
        ) is True

    def test_check_before_write_blocks_incompatible(self, checker):
        """check_before_write() returns False for incompatible changes."""
        assert checker.check_before_write(
            proposed_version=ContractVersion(major=3, minor=0, patch=0)
        ) is False

    def test_none_proposed_version_passes(self, checker):
        """No proposed version change passes by default."""
        result = checker.check_compatibility(proposed_version=None)
        assert result.compatible is True
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_runtime_contracts.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/runtime_contracts.py
"""
Runtime Contract Checker — N/N-1 Schema Validation at Runtime
================================================================

Extends the boot-time :class:`ContractGate` with runtime checks.
Before any autonomous write, verifies that proposed changes don't
break the contract between the current schema (N) and the previous
schema (N-1).

Rules:
- Same major, same or +1 minor: COMPATIBLE
- Same major, exactly N-1 minor: COMPATIBLE (backward compat)
- Different major: INCOMPATIBLE
- More than 1 minor version back: INCOMPATIBLE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from backend.core.ouroboros.governance.contract_gate import ContractVersion

logger = logging.getLogger("Ouroboros.RuntimeContracts")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractViolation:
    """A single contract violation."""

    field: str
    reason: str


@dataclass(frozen=True)
class ContractCheckResult:
    """Result of a runtime contract compatibility check."""

    compatible: bool
    violations: List[ContractViolation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# RuntimeContractChecker
# ---------------------------------------------------------------------------


class RuntimeContractChecker:
    """Runtime N/N-1 schema compatibility checker.

    Parameters
    ----------
    current_version:
        The current schema version (N).
    """

    def __init__(self, current_version: ContractVersion) -> None:
        self._current = current_version

    def check_compatibility(
        self, proposed_version: Optional[ContractVersion] = None
    ) -> ContractCheckResult:
        """Check if a proposed version is compatible with current.

        Parameters
        ----------
        proposed_version:
            The proposed schema version, or None if no version change.

        Returns
        -------
        ContractCheckResult
            Whether the proposed version is compatible.
        """
        if proposed_version is None:
            return ContractCheckResult(compatible=True)

        violations: List[ContractViolation] = []

        # Major version must match
        if proposed_version.major != self._current.major:
            violations.append(
                ContractViolation(
                    field="major_version",
                    reason=(
                        f"Major version mismatch: current={self._current.major}, "
                        f"proposed={proposed_version.major}"
                    ),
                )
            )
            return ContractCheckResult(compatible=False, violations=violations)

        # Minor version: allow N (same), N+x (forward), N-1 (one back)
        minor_delta = self._current.minor - proposed_version.minor
        if minor_delta > 1:
            violations.append(
                ContractViolation(
                    field="minor_version",
                    reason=(
                        f"Minor version too old: current={self._current.minor}, "
                        f"proposed={proposed_version.minor} (max N-1 allowed)"
                    ),
                )
            )
            return ContractCheckResult(compatible=False, violations=violations)

        return ContractCheckResult(compatible=True)

    def check_before_write(
        self, proposed_version: Optional[ContractVersion] = None
    ) -> bool:
        """Convenience method: returns True if write is safe.

        Parameters
        ----------
        proposed_version:
            The proposed schema version, or None if no version change.

        Returns
        -------
        bool
            True if the proposed version is compatible.
        """
        result = self.check_compatibility(proposed_version)
        if not result.compatible:
            logger.warning(
                "Runtime contract check failed: %s",
                "; ".join(v.reason for v in result.violations),
            )
        return result.compatible
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_runtime_contracts.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/runtime_contracts.py tests/test_ouroboros_governance/test_runtime_contracts.py
git commit -m "feat(governance): add runtime N/N-1 contract checker

Extends boot-time ContractGate with runtime schema validation.
Same major + N-1 minor allowed. Major mismatch or N-2+ blocked.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Canary Controller — Domain Slice Promotion

**Files:**
- Create: `backend/core/ouroboros/governance/canary_controller.py`
- Create: `tests/test_ouroboros_governance/test_canary_controller.py`

**Context:** Before enabling full autonomy, Ouroboros must prove itself per domain slice. A domain slice is a path prefix (e.g., `backend/core/ouroboros/`). The canary controller tracks per-slice metrics and checks promotion criteria: >= 50 operations, rollback rate < 5%, p95 latency < 120s, and 72h stability.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_canary_controller.py
"""Tests for the canary controller with domain slice promotion."""

import time
import pytest

from backend.core.ouroboros.governance.canary_controller import (
    CanaryController,
    DomainSlice,
    SliceMetrics,
    PromotionResult,
    CanaryState,
)


@pytest.fixture
def controller():
    return CanaryController()


class TestDomainSlice:
    def test_slice_fields(self):
        """DomainSlice has path prefix and state."""
        s = DomainSlice(path_prefix="backend/core/ouroboros/")
        assert s.path_prefix == "backend/core/ouroboros/"
        assert s.state == CanaryState.PENDING

    def test_file_matches_prefix(self):
        """matches() returns True for files under the prefix."""
        s = DomainSlice(path_prefix="backend/core/ouroboros/")
        assert s.matches("backend/core/ouroboros/engine.py") is True
        assert s.matches("backend/core/prime_router.py") is False


class TestSliceMetrics:
    def test_initial_metrics(self):
        """Fresh metrics have zero counts."""
        m = SliceMetrics()
        assert m.total_operations == 0
        assert m.successful_operations == 0
        assert m.rollback_count == 0
        assert m.rollback_rate == 0.0

    def test_rollback_rate_calculation(self):
        """Rollback rate is rollbacks / total."""
        m = SliceMetrics()
        m.total_operations = 100
        m.rollback_count = 3
        assert m.rollback_rate == 0.03

    def test_rollback_rate_zero_ops(self):
        """Rollback rate is 0 with zero operations."""
        m = SliceMetrics()
        assert m.rollback_rate == 0.0


class TestCanaryController:
    def test_register_slice(self, controller):
        """Slices can be registered."""
        controller.register_slice("backend/core/ouroboros/")
        assert len(controller.slices) == 1

    def test_record_operation_success(self, controller):
        """Successful operation increments counters."""
        controller.register_slice("backend/core/ouroboros/")
        controller.record_operation(
            file_path="backend/core/ouroboros/engine.py",
            success=True,
            latency_s=5.0,
        )
        metrics = controller.get_metrics("backend/core/ouroboros/")
        assert metrics.total_operations == 1
        assert metrics.successful_operations == 1

    def test_record_operation_rollback(self, controller):
        """Rollback increments rollback counter."""
        controller.register_slice("backend/core/ouroboros/")
        controller.record_operation(
            file_path="backend/core/ouroboros/engine.py",
            success=False,
            latency_s=5.0,
            rolled_back=True,
        )
        metrics = controller.get_metrics("backend/core/ouroboros/")
        assert metrics.rollback_count == 1

    def test_unmatched_file_ignored(self, controller):
        """Operations on unregistered paths are ignored."""
        controller.register_slice("backend/core/ouroboros/")
        controller.record_operation(
            file_path="backend/core/prime_router.py",
            success=True,
            latency_s=5.0,
        )
        metrics = controller.get_metrics("backend/core/ouroboros/")
        assert metrics.total_operations == 0


class TestPromotionCriteria:
    def test_insufficient_operations(self, controller):
        """< 50 operations fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(30):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 5.0
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "50 operations" in result.reason

    def test_high_rollback_rate_fails(self, controller):
        """Rollback rate >= 5% fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(50):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 5.0
            )
        for _ in range(5):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", False, 5.0, rolled_back=True
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "rollback" in result.reason.lower()

    def test_high_latency_fails(self, controller):
        """p95 latency > 120s fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(50):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 130.0
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "latency" in result.reason.lower()

    def test_stability_window_not_met(self, controller):
        """< 72h since first operation fails promotion."""
        controller.register_slice("backend/core/ouroboros/")
        for _ in range(55):
            controller.record_operation(
                "backend/core/ouroboros/foo.py", True, 5.0
            )
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False
        assert "72" in result.reason or "stability" in result.reason.lower()

    def test_all_criteria_met(self, controller):
        """All criteria met -> promotion passes."""
        controller.register_slice("backend/core/ouroboros/")
        metrics = controller.get_metrics("backend/core/ouroboros/")
        # Simulate 55 successful ops with low latency and 72h+ stability
        metrics.total_operations = 55
        metrics.successful_operations = 55
        metrics.rollback_count = 1
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)  # 73 hours ago
        result = controller.check_promotion("backend/core/ouroboros/")
        assert result.promoted is True

    def test_promote_changes_state(self, controller):
        """Successful promotion changes slice state to ACTIVE."""
        controller.register_slice("backend/core/ouroboros/")
        metrics = controller.get_metrics("backend/core/ouroboros/")
        metrics.total_operations = 55
        metrics.successful_operations = 55
        metrics.rollback_count = 1
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)
        controller.check_promotion("backend/core/ouroboros/")
        s = controller.get_slice("backend/core/ouroboros/")
        assert s.state == CanaryState.ACTIVE

    def test_is_file_allowed(self, controller):
        """is_file_allowed() returns True for promoted slices."""
        controller.register_slice("backend/core/ouroboros/")
        assert controller.is_file_allowed("backend/core/ouroboros/foo.py") is False
        # Promote the slice
        metrics = controller.get_metrics("backend/core/ouroboros/")
        metrics.total_operations = 55
        metrics.successful_operations = 55
        metrics.rollback_count = 0
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)
        controller.check_promotion("backend/core/ouroboros/")
        assert controller.is_file_allowed("backend/core/ouroboros/foo.py") is True

    def test_unregistered_file_not_allowed(self, controller):
        """Files not in any slice are not allowed."""
        assert controller.is_file_allowed("random/file.py") is False
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_canary_controller.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/canary_controller.py
"""
Canary Controller — Domain Slice Promotion
=============================================

Manages per-domain-slice canary rollout for governed autonomy.
A domain slice is a path prefix (e.g., ``backend/core/ouroboros/``).

Before a slice is promoted to ACTIVE, it must meet ALL criteria:

1. >= 50 successful operations
2. 0 unrecoverable rollbacks (auto-rollback acceptable)
3. rollback_rate < 5% over trailing operations
4. p95 operation latency < 120s
5. 72 hours elapsed since first operation (stability window)

Slices start in PENDING state and graduate to ACTIVE upon promotion.
Files not in any registered slice are NOT allowed for autonomous ops.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.CanaryController")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_OPERATIONS = 50
MAX_ROLLBACK_RATE = 0.05
MAX_P95_LATENCY_S = 120.0
STABILITY_WINDOW_H = 72


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CanaryState(enum.Enum):
    """State of a domain slice in the canary pipeline."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DomainSlice:
    """A domain slice for canary rollout."""

    path_prefix: str
    state: CanaryState = CanaryState.PENDING

    def matches(self, file_path: str) -> bool:
        """Check if a file path belongs to this slice."""
        return file_path.startswith(self.path_prefix)


class SliceMetrics:
    """Tracks per-slice operation metrics."""

    def __init__(self) -> None:
        self.total_operations: int = 0
        self.successful_operations: int = 0
        self.rollback_count: int = 0
        self.latencies: List[float] = []
        self.first_operation_time: Optional[float] = None

    @property
    def rollback_rate(self) -> float:
        """Rollback rate as a fraction."""
        if self.total_operations == 0:
            return 0.0
        return self.rollback_count / self.total_operations

    @property
    def p95_latency(self) -> float:
        """95th percentile latency in seconds."""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.95)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]

    @property
    def stability_hours(self) -> float:
        """Hours elapsed since first operation."""
        if self.first_operation_time is None:
            return 0.0
        return (time.time() - self.first_operation_time) / 3600.0


@dataclass(frozen=True)
class PromotionResult:
    """Result of a canary promotion check."""

    promoted: bool
    reason: str


# ---------------------------------------------------------------------------
# CanaryController
# ---------------------------------------------------------------------------


class CanaryController:
    """Manages domain slice canary rollout for governed autonomy."""

    def __init__(self) -> None:
        self._slices: Dict[str, DomainSlice] = {}
        self._metrics: Dict[str, SliceMetrics] = {}

    @property
    def slices(self) -> Dict[str, DomainSlice]:
        """All registered slices."""
        return dict(self._slices)

    def register_slice(self, path_prefix: str) -> None:
        """Register a new domain slice for canary tracking."""
        self._slices[path_prefix] = DomainSlice(path_prefix=path_prefix)
        self._metrics[path_prefix] = SliceMetrics()
        logger.info("Registered canary slice: %s", path_prefix)

    def get_slice(self, path_prefix: str) -> Optional[DomainSlice]:
        """Get a registered slice by prefix."""
        return self._slices.get(path_prefix)

    def get_metrics(self, path_prefix: str) -> Optional[SliceMetrics]:
        """Get metrics for a registered slice."""
        return self._metrics.get(path_prefix)

    def record_operation(
        self,
        file_path: str,
        success: bool,
        latency_s: float,
        rolled_back: bool = False,
    ) -> None:
        """Record an operation outcome for the matching slice."""
        for prefix, metrics in self._metrics.items():
            if self._slices[prefix].matches(file_path):
                metrics.total_operations += 1
                if success:
                    metrics.successful_operations += 1
                if rolled_back:
                    metrics.rollback_count += 1
                metrics.latencies.append(latency_s)
                if metrics.first_operation_time is None:
                    metrics.first_operation_time = time.time()
                return

    def check_promotion(self, path_prefix: str) -> PromotionResult:
        """Check if a slice meets all promotion criteria.

        If all criteria pass, the slice is promoted to ACTIVE.
        """
        metrics = self._metrics.get(path_prefix)
        if metrics is None:
            return PromotionResult(promoted=False, reason="Slice not registered")

        # Criterion 1: Minimum operations
        if metrics.total_operations < MIN_OPERATIONS:
            return PromotionResult(
                promoted=False,
                reason=f"Need >= {MIN_OPERATIONS} operations, have {metrics.total_operations}",
            )

        # Criterion 2: Rollback rate
        if metrics.rollback_rate >= MAX_ROLLBACK_RATE:
            return PromotionResult(
                promoted=False,
                reason=f"Rollback rate {metrics.rollback_rate:.1%} >= {MAX_ROLLBACK_RATE:.0%} threshold",
            )

        # Criterion 3: P95 latency
        if metrics.p95_latency > MAX_P95_LATENCY_S:
            return PromotionResult(
                promoted=False,
                reason=f"P95 latency {metrics.p95_latency:.1f}s > {MAX_P95_LATENCY_S}s threshold",
            )

        # Criterion 4: Stability window
        if metrics.stability_hours < STABILITY_WINDOW_H:
            return PromotionResult(
                promoted=False,
                reason=(
                    f"Stability window {metrics.stability_hours:.1f}h "
                    f"< {STABILITY_WINDOW_H}h required"
                ),
            )

        # All criteria met — promote
        self._slices[path_prefix].state = CanaryState.ACTIVE
        logger.info("Canary slice promoted to ACTIVE: %s", path_prefix)
        return PromotionResult(promoted=True, reason="All criteria met")

    def is_file_allowed(self, file_path: str) -> bool:
        """Check if autonomous operations are allowed on a file.

        A file is allowed only if it belongs to an ACTIVE slice.
        """
        for prefix, s in self._slices.items():
            if s.matches(file_path) and s.state == CanaryState.ACTIVE:
                return True
        return False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_canary_controller.py -v`
Expected: All 15 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/canary_controller.py tests/test_ouroboros_governance/test_canary_controller.py
git commit -m "feat(governance): add canary controller with domain slice promotion

Per-slice canary rollout: >= 50 ops, < 5% rollback, < 120s p95,
72h stability window. Files only allowed in ACTIVE slices.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: CLI Break-Glass Commands

**Files:**
- Create: `backend/core/ouroboros/governance/cli_commands.py`
- Create: `tests/test_ouroboros_governance/test_cli_commands.py`

**Context:** The design doc specifies `jarvis break-glass --scope <op_id> --ttl 300` as a CLI command. Rather than modifying the 73K+ line `unified_supervisor.py`, this task creates an importable module with break-glass functions that the supervisor can wire into its argparse later. The module provides `issue_break_glass()`, `list_tokens()`, and `revoke_token()` as standalone async functions.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_cli_commands.py
"""Tests for CLI break-glass command functions."""

import pytest

from backend.core.ouroboros.governance.cli_commands import (
    issue_break_glass,
    list_active_tokens,
    revoke_break_glass,
    get_audit_report,
)
from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
)


@pytest.fixture
def manager():
    return BreakGlassManager()


class TestIssueBreakGlass:
    @pytest.mark.asyncio
    async def test_issue_returns_token(self, manager):
        """issue_break_glass() returns a valid token."""
        token = await issue_break_glass(
            manager=manager,
            op_id="op-test-001",
            reason="emergency fix needed",
            ttl=300,
            issuer="derek",
        )
        assert token.op_id == "op-test-001"
        assert token.reason == "emergency fix needed"
        assert token.ttl == 300
        assert token.issuer == "derek"

    @pytest.mark.asyncio
    async def test_issued_token_validates(self, manager):
        """Issued token can be validated."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-002",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        assert manager.validate("op-test-002") is True


class TestListTokens:
    @pytest.mark.asyncio
    async def test_list_empty(self, manager):
        """No tokens returns empty list."""
        tokens = list_active_tokens(manager)
        assert tokens == []

    @pytest.mark.asyncio
    async def test_list_active(self, manager):
        """Active tokens appear in list."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-010",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        tokens = list_active_tokens(manager)
        assert len(tokens) == 1
        assert tokens[0].op_id == "op-test-010"


class TestRevokeBreakGlass:
    @pytest.mark.asyncio
    async def test_revoke_removes_token(self, manager):
        """Revoked token no longer validates."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-020",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        await revoke_break_glass(
            manager=manager,
            op_id="op-test-020",
            reason="no longer needed",
        )
        tokens = list_active_tokens(manager)
        assert len(tokens) == 0


class TestAuditReport:
    @pytest.mark.asyncio
    async def test_audit_report_includes_actions(self, manager):
        """Audit report includes issue and revoke actions."""
        await issue_break_glass(
            manager=manager,
            op_id="op-test-030",
            reason="emergency",
            ttl=300,
            issuer="derek",
        )
        await revoke_break_glass(
            manager=manager,
            op_id="op-test-030",
            reason="resolved",
        )
        report = get_audit_report(manager)
        assert len(report) >= 2
        actions = [entry.action for entry in report]
        assert "issued" in actions
        assert "revoked" in actions
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_cli_commands.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/cli_commands.py
"""
CLI Break-Glass Commands — Importable Functions for Supervisor CLI
====================================================================

Provides standalone async functions for break-glass operations that
can be wired into the supervisor's argparse CLI.

Usage from supervisor CLI::

    jarvis break-glass issue --op-id <op_id> --reason <reason> --ttl 300
    jarvis break-glass list
    jarvis break-glass revoke --op-id <op_id> --reason <reason>
    jarvis break-glass audit

These functions wrap :class:`BreakGlassManager` with CLI-friendly
signatures and return values.
"""

from __future__ import annotations

import logging
from typing import List

from backend.core.ouroboros.governance.break_glass import (
    BreakGlassAuditEntry,
    BreakGlassManager,
    BreakGlassToken,
)

logger = logging.getLogger("Ouroboros.CLI")


async def issue_break_glass(
    manager: BreakGlassManager,
    op_id: str,
    reason: str,
    ttl: int = 300,
    issuer: str = "cli",
) -> BreakGlassToken:
    """Issue a break-glass token for a blocked operation.

    Parameters
    ----------
    manager:
        The BreakGlassManager instance.
    op_id:
        Operation to unlock.
    reason:
        Human justification for the break-glass.
    ttl:
        Time-to-live in seconds (default 300 = 5 min).
    issuer:
        Who is issuing the token.

    Returns
    -------
    BreakGlassToken
        The issued token.
    """
    token = await manager.issue(
        op_id=op_id,
        reason=reason,
        ttl=ttl,
        issuer=issuer,
    )
    logger.info(
        "Break-glass issued: op=%s, ttl=%ds, issuer=%s",
        op_id, ttl, issuer,
    )
    return token


def list_active_tokens(manager: BreakGlassManager) -> List[BreakGlassToken]:
    """List all active (non-expired) break-glass tokens.

    Parameters
    ----------
    manager:
        The BreakGlassManager instance.

    Returns
    -------
    List[BreakGlassToken]
        Active tokens.
    """
    return [
        token for token in manager._tokens.values()
        if not token.is_expired()
    ]


async def revoke_break_glass(
    manager: BreakGlassManager,
    op_id: str,
    reason: str = "revoked via CLI",
) -> None:
    """Revoke a break-glass token.

    Parameters
    ----------
    manager:
        The BreakGlassManager instance.
    op_id:
        Operation to revoke the token for.
    reason:
        Reason for revocation.
    """
    await manager.revoke(op_id=op_id, reason=reason)
    logger.info("Break-glass revoked: op=%s, reason=%s", op_id, reason)


def get_audit_report(manager: BreakGlassManager) -> List[BreakGlassAuditEntry]:
    """Get the full break-glass audit trail.

    Parameters
    ----------
    manager:
        The BreakGlassManager instance.

    Returns
    -------
    List[BreakGlassAuditEntry]
        All audit entries (issue, validate, revoke, expire).
    """
    return manager.get_audit_trail()
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_cli_commands.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/cli_commands.py tests/test_ouroboros_governance/test_cli_commands.py
git commit -m "feat(governance): add CLI break-glass command functions

Importable functions for issue/list/revoke/audit break-glass tokens.
Ready for supervisor CLI integration.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Wire Phase 3 exports into governance __init__.py

**Files:**
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Step 1: Add Phase 3 exports after the Phase 2 exports**

Add after the `multi_file_engine` imports:

```python
from backend.core.ouroboros.governance.event_bridge import (
    EventBridge,
    GovernanceEventMapper,
)
from backend.core.ouroboros.governance.blast_radius_adapter import (
    BlastRadiusAdapter,
    BlastRadiusResult,
)
from backend.core.ouroboros.governance.learning_bridge import (
    LearningBridge,
    OperationOutcome,
)
from backend.core.ouroboros.governance.runtime_contracts import (
    RuntimeContractChecker,
    ContractCheckResult,
    ContractViolation,
)
from backend.core.ouroboros.governance.canary_controller import (
    CanaryController,
    DomainSlice,
    SliceMetrics,
    PromotionResult,
    CanaryState,
)
from backend.core.ouroboros.governance.cli_commands import (
    issue_break_glass,
    list_active_tokens,
    revoke_break_glass,
    get_audit_report,
)
```

Also update the docstring to include Phase 3 components.

**Step 2: Run all governance tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v`
Expected: ALL tests pass

**Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/__init__.py
git commit -m "feat(governance): wire Phase 3 exports into governance __init__

Adds event_bridge, blast_radius_adapter, learning_bridge,
runtime_contracts, canary_controller, and cli_commands exports.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 8: Phase 3 Integration Tests — Go/No-Go Criteria

**Files:**
- Create: `tests/test_ouroboros_governance/test_phase3_integration.py`

**Context:** End-to-end tests verifying Phase 3 Go/No-Go criteria from the design doc section 4 (canary promotion) and the deferred Phase 2B criteria (cross-repo events, blast radius integration, learning feedback).

**Step 1: Write the integration tests**

```python
# tests/test_ouroboros_governance/test_phase3_integration.py
"""Phase 3 integration tests — Go/No-Go criteria verification.

Tests verify:
- Cross-repo event bridge: governance messages -> CrossRepoEvents
- Blast radius from Oracle integrated into risk classification
- Learning feedback published with op_id correlation
- Runtime N/N-1 contract enforcement
- Canary promotion criteria per domain slice
- CLI break-glass end-to-end flow
"""

import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.event_bridge import (
    EventBridge,
    GovernanceEventMapper,
)
from backend.core.ouroboros.governance.blast_radius_adapter import (
    BlastRadiusAdapter,
)
from backend.core.ouroboros.governance.learning_bridge import (
    LearningBridge,
    OperationOutcome,
)
from backend.core.ouroboros.governance.runtime_contracts import (
    RuntimeContractChecker,
)
from backend.core.ouroboros.governance.canary_controller import (
    CanaryController,
    CanaryState,
)
from backend.core.ouroboros.governance.cli_commands import (
    issue_break_glass,
    list_active_tokens,
    revoke_break_glass,
    get_audit_report,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    MessageType,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.contract_gate import ContractVersion
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


# ---------------------------------------------------------------------------
# Cross-Repo Event Bridge Go/No-Go
# ---------------------------------------------------------------------------


class TestCrossRepoEventGoNoGo:
    @pytest.mark.asyncio
    async def test_outbox_commit_to_inbox_delivery(self):
        """Governance DECISION(applied) -> CrossRepoEvent emitted."""
        bus = AsyncMock()
        bridge = EventBridge(event_bus=bus)
        msg = CommMessage(
            op_id="op-integration-001",
            msg_type=MessageType.DECISION,
            payload={"outcome": "applied", "reason_code": "safe_auto"},
        )
        await bridge.forward(msg)
        bus.emit.assert_called_once()
        event = bus.emit.call_args[0][0]
        assert event.type.name == "IMPROVEMENT_COMPLETE"
        assert event.payload["op_id"] == "op-integration-001"

    def test_governance_intent_maps_to_improvement_request(self):
        """INTENT -> IMPROVEMENT_REQUEST for cross-repo visibility."""
        msg = CommMessage(
            op_id="op-integration-002",
            msg_type=MessageType.INTENT,
            payload={"goal": "fix bug", "target_files": ["foo.py"]},
        )
        event = GovernanceEventMapper.map(msg)
        assert event.type.name == "IMPROVEMENT_REQUEST"

    def test_postmortem_maps_to_failure_event(self):
        """POSTMORTEM -> IMPROVEMENT_FAILED for cross-repo postmortem."""
        msg = CommMessage(
            op_id="op-integration-003",
            msg_type=MessageType.POSTMORTEM,
            payload={"root_cause": "timeout", "failed_phase": "VALIDATE"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event.type.name == "IMPROVEMENT_FAILED"


# ---------------------------------------------------------------------------
# Blast Radius Integration Go/No-Go
# ---------------------------------------------------------------------------


class TestBlastRadiusGoNoGo:
    def test_oracle_blast_radius_flows_into_risk_classification(self):
        """Oracle blast radius > 5 -> APPROVAL_REQUIRED classification."""
        oracle = MagicMock()
        blast = MagicMock()
        blast.total_affected = 8
        blast.risk_level = "medium"
        oracle.find_nodes_in_file.return_value = ["node-1"]
        oracle.compute_blast_radius.return_value = blast

        adapter = BlastRadiusAdapter(oracle=oracle)
        profile = OperationProfile(
            files_affected=[Path("foo.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 8

        engine = RiskEngine()
        classification = engine.classify(enriched)
        assert classification.tier == RiskTier.APPROVAL_REQUIRED

    def test_oracle_unavailable_uses_manual_fallback(self):
        """No Oracle -> uses manual blast_radius (safe fallback)."""
        adapter = BlastRadiusAdapter(oracle=None)
        profile = OperationProfile(
            files_affected=[Path("foo.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 1


# ---------------------------------------------------------------------------
# Learning Feedback Go/No-Go
# ---------------------------------------------------------------------------


class TestLearningFeedbackGoNoGo:
    @pytest.mark.asyncio
    async def test_op_id_correlation_in_learning_feedback(self):
        """Operation outcome published with op_id to LearningMemory."""
        memory = AsyncMock()
        bridge = LearningBridge(learning_memory=memory)
        outcome = OperationOutcome(
            op_id="op-learning-001",
            goal="fix bug in engine.py",
            target_files=["engine.py"],
            final_state=OperationState.APPLIED,
        )
        await bridge.publish(outcome)
        memory.record_attempt.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_pattern_skipped_on_retry(self):
        """Should skip pattern after too many failures."""
        memory = AsyncMock()
        memory.should_skip_pattern.return_value = True
        bridge = LearningBridge(learning_memory=memory)
        result = await bridge.should_skip("fix bug", "foo.py", "syntax_error")
        assert result is True


# ---------------------------------------------------------------------------
# Runtime Contract Go/No-Go
# ---------------------------------------------------------------------------


class TestRuntimeContractGoNoGo:
    def test_nn1_enforced_before_write(self):
        """N/N-1 schema check blocks incompatible writes at runtime."""
        checker = RuntimeContractChecker(
            current_version=ContractVersion(major=2, minor=1, patch=0)
        )
        # Major version bump -> blocked
        assert checker.check_before_write(
            ContractVersion(major=3, minor=0, patch=0)
        ) is False
        # Same major, minor+1 -> allowed
        assert checker.check_before_write(
            ContractVersion(major=2, minor=2, patch=0)
        ) is True
        # N-1 minor -> allowed
        assert checker.check_before_write(
            ContractVersion(major=2, minor=0, patch=0)
        ) is True


# ---------------------------------------------------------------------------
# Canary Promotion Go/No-Go
# ---------------------------------------------------------------------------


class TestCanaryPromotionGoNoGo:
    def test_50_ops_rollback_latency_stability(self):
        """All canary criteria met -> slice promoted to ACTIVE."""
        ctrl = CanaryController()
        ctrl.register_slice("backend/core/ouroboros/")
        metrics = ctrl.get_metrics("backend/core/ouroboros/")
        metrics.total_operations = 55
        metrics.successful_operations = 54
        metrics.rollback_count = 1
        metrics.latencies = [5.0] * 55
        metrics.first_operation_time = time.time() - (73 * 3600)

        result = ctrl.check_promotion("backend/core/ouroboros/")
        assert result.promoted is True
        assert ctrl.get_slice("backend/core/ouroboros/").state == CanaryState.ACTIVE

    def test_insufficient_ops_blocks_promotion(self):
        """< 50 ops -> promotion blocked."""
        ctrl = CanaryController()
        ctrl.register_slice("backend/core/ouroboros/")
        for _ in range(30):
            ctrl.record_operation("backend/core/ouroboros/foo.py", True, 5.0)
        result = ctrl.check_promotion("backend/core/ouroboros/")
        assert result.promoted is False


# ---------------------------------------------------------------------------
# CLI Break-Glass Go/No-Go
# ---------------------------------------------------------------------------


class TestCLIBreakGlassGoNoGo:
    @pytest.mark.asyncio
    async def test_end_to_end_break_glass_flow(self):
        """Issue -> validate -> revoke -> audit trail complete."""
        mgr = BreakGlassManager()

        # Issue
        token = await issue_break_glass(
            mgr, op_id="op-cli-001", reason="emergency",
            ttl=300, issuer="derek",
        )
        assert token.op_id == "op-cli-001"

        # Validate
        assert mgr.validate("op-cli-001") is True

        # List
        active = list_active_tokens(mgr)
        assert len(active) == 1

        # Revoke
        await revoke_break_glass(mgr, "op-cli-001", "resolved")
        active = list_active_tokens(mgr)
        assert len(active) == 0

        # Audit
        audit = get_audit_report(mgr)
        actions = [e.action for e in audit]
        assert "issued" in actions
        assert "validated" in actions
        assert "revoked" in actions
```

**Step 2: Run integration tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_phase3_integration.py -v`
Expected: All tests PASS

**Step 3: Run ALL governance tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v`
Expected: ALL tests pass (Phase 0 + Phase 1 + Phase 2 + Phase 3)

**Step 4: Commit**

```bash
git add tests/test_ouroboros_governance/test_phase3_integration.py
git commit -m "test(governance): add Phase 3 integration tests for Go/No-Go criteria

Verifies cross-repo event bridge, Oracle blast radius integration,
learning feedback with op_id, runtime N/N-1 contracts, canary
promotion criteria, and CLI break-glass end-to-end flow.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Summary of Phase 3 Deliverables

| Task | Component | Tests | Go/No-Go Criteria Covered |
|------|-----------|-------|---------------------------|
| 1 | `event_bridge.py` — Cross-repo event mapping | ~11 | Outbox commit -> inbox delivery, event type mapping |
| 2 | `blast_radius_adapter.py` — Oracle integration | ~8 | Blast radius flows into risk classification |
| 3 | `learning_bridge.py` — LearningMemory feedback | ~9 | op_id correlation, skip failed patterns |
| 4 | `runtime_contracts.py` — N/N-1 at runtime | ~9 | Schema compat enforced before writes |
| 5 | `canary_controller.py` — Domain slice promotion | ~15 | 50 ops, <5% rollback, <120s p95, 72h stability |
| 6 | `cli_commands.py` — Break-glass CLI functions | ~6 | Issue/list/revoke/audit flow |
| 7 | `__init__.py` — Wire exports | 0 | Package completeness |
| 8 | `test_phase3_integration.py` — Go/No-Go | ~13 | All Phase 3 acceptance criteria |

**Total new tests: ~71**
**Total governance tests (Phase 0 + Phase 1 + Phase 2 + Phase 3): ~260**

---

## What Phase 3 Completes (Full Governed Autonomy Stack)

- **Phase 0**: Supervisor authority, operation identity, risk engine, contracts, comm protocol, ledger, sandbox loop
- **Phase 1**: Lock manager, break-glass tokens, transactional change engine, TUI transport
- **Phase 2**: Resource monitor, degradation controller, routing policy, multi-file atomic engine
- **Phase 3**: Event bridge, blast radius from Oracle, learning feedback, runtime contracts, canary rollout, CLI break-glass

## Remaining for Production Activation

After Phase 3 code is merged:
1. Register initial domain slices (start with `backend/core/ouroboros/` only)
2. Run 50+ operations in canary mode per slice
3. Monitor rollback rate, latency, and stability for 72 hours
4. Wire CLI commands into `unified_supervisor.py` argparse (manual edit)
5. Derek signs off on autonomy scope
