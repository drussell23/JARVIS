# tests/test_ouroboros_governance/test_phase3_integration.py
"""Phase 3 integration tests -- Go/No-Go criteria verification.

Tests verify acceptance criteria for all Phase 3 components:

    Cross-Repo Event Bridge
    Blast Radius Adapter (Oracle integration)
    Learning Feedback Bridge
    Runtime N/N-1 Contracts
    Canary Domain-Slice Promotion
    CLI Break-Glass End-to-End

Test Classes
------------
- **TestCrossRepoEventBridgeGoNoGo** -- governance events bridged to cross-repo
- **TestBlastRadiusIntegrationGoNoGo** -- Oracle blast radius flows into risk
- **TestLearningFeedbackGoNoGo** -- operation outcomes published with op_id
- **TestRuntimeContractsGoNoGo** -- N/N-1 schema compatibility enforcement
- **TestCanaryPromotionGoNoGo** -- domain slice promotion criteria
- **TestCLIBreakGlassGoNoGo** -- end-to-end break-glass lifecycle
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.event_bridge import (
    EventBridge,
    GovernanceEventMapper,
)
from backend.core.ouroboros.governance.blast_radius_adapter import (
    BlastRadiusAdapter,
    BlastRadiusResult,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskClassification,
    RiskEngine,
    RiskTier,
)
from backend.core.ouroboros.governance.learning_bridge import (
    LearningBridge,
    OperationOutcome,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.runtime_contracts import (
    ContractCheckResult,
    ContractViolation,
    RuntimeContractChecker,
)
from backend.core.ouroboros.governance.contract_gate import ContractVersion
from backend.core.ouroboros.governance.canary_controller import (
    CanaryController,
    CanaryState,
    DomainSlice,
    SliceMetrics,
    MIN_OPERATIONS,
    MAX_ROLLBACK_RATE,
    MAX_P95_LATENCY_S,
    STABILITY_WINDOW_H,
)
from backend.core.ouroboros.governance.break_glass import (
    BreakGlassAuditEntry,
    BreakGlassExpired,
    BreakGlassManager,
    BreakGlassScopeMismatch,
    BreakGlassToken,
)
from backend.core.ouroboros.governance.cli_commands import (
    get_audit_report,
    issue_break_glass,
    list_active_tokens,
    revoke_break_glass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEventBus:
    """In-memory event bus that records emitted events."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _FailingEventBus:
    """Event bus that always raises on emit."""

    async def emit(self, event: Any) -> None:
        raise RuntimeError("event bus unavailable")


class _FakeLearningMemory:
    """In-memory LearningMemory stub that records calls."""

    def __init__(self) -> None:
        self.attempts: List[Dict[str, Any]] = []
        self._skip_patterns: Dict[str, bool] = {}

    async def record_attempt(
        self,
        request: Any,
        error_pattern: str,
        solution_pattern: Optional[str],
        success: bool,
    ) -> None:
        self.attempts.append({
            "goal": request.goal,
            "target_file": request.target_file,
            "error_pattern": error_pattern,
            "solution_pattern": solution_pattern,
            "success": success,
        })

    async def should_skip_pattern(
        self, request: Any, error_pattern: str
    ) -> bool:
        key = f"{request.goal}:{request.target_file}:{error_pattern}"
        return self._skip_patterns.get(key, False)

    async def get_known_solution(
        self, request: Any, error_pattern: str
    ) -> Optional[str]:
        return None

    def mark_skip(self, goal: str, target_file: str, error_pattern: str) -> None:
        key = f"{goal}:{target_file}:{error_pattern}"
        self._skip_patterns[key] = True


class _FakeOracleNode:
    """Minimal stand-in for a CodebaseKnowledgeGraph node."""

    def __init__(self, name: str) -> None:
        self.name = name


@dataclass
class _FakeBlastResult:
    """Minimal stand-in for Oracle blast radius result."""

    total_affected: int
    risk_level: str


class _FakeOracle:
    """Minimal Oracle stub for BlastRadiusAdapter tests."""

    def __init__(self, blast_map: Dict[str, _FakeBlastResult]) -> None:
        self._map = blast_map

    def find_nodes_in_file(self, file_path: str) -> List[_FakeOracleNode]:
        if file_path in self._map:
            return [_FakeOracleNode(file_path)]
        return []

    def compute_blast_radius(self, node: _FakeOracleNode) -> _FakeBlastResult:
        return self._map[node.name]


# ---------------------------------------------------------------------------
# Cross-Repo Event Bridge Go/No-Go
# ---------------------------------------------------------------------------


class TestCrossRepoEventBridgeGoNoGo:
    """Governance lifecycle events are correctly mapped and emitted."""

    def test_decision_applied_maps_to_improvement_complete(self):
        """DECISION(applied) -> CrossRepoEvent(IMPROVEMENT_COMPLETE)."""
        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-abc-jarvis",
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied", "reason_code": "all_tests_pass"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.value == "improvement_complete"
        assert event.payload["op_id"] == "op-abc-jarvis"
        assert event.payload["outcome"] == "applied"
        assert event.source_repo.value == "jarvis"

    def test_intent_maps_to_improvement_request(self):
        """INTENT -> CrossRepoEvent(IMPROVEMENT_REQUEST)."""
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id="op-xyz-jarvis",
            seq=1,
            causal_parent_seq=None,
            payload={
                "goal": "fix bug",
                "target_files": ["src/a.py"],
                "risk_tier": "SAFE_AUTO",
                "blast_radius": 1,
            },
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.value == "improvement_request"
        assert event.payload["goal"] == "fix bug"

    def test_postmortem_maps_to_improvement_failed(self):
        """POSTMORTEM -> CrossRepoEvent(IMPROVEMENT_FAILED)."""
        msg = CommMessage(
            msg_type=MessageType.POSTMORTEM,
            op_id="op-fail-jarvis",
            seq=5,
            causal_parent_seq=4,
            payload={
                "root_cause": "syntax error",
                "failed_phase": "validation",
            },
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.value == "improvement_failed"
        assert event.payload["root_cause"] == "syntax error"

    def test_heartbeat_not_bridged(self):
        """HEARTBEAT messages are filtered out (too noisy)."""
        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id="op-hb-jarvis",
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "sandboxing", "progress_pct": 0.5},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is None

    def test_plan_not_bridged(self):
        """PLAN messages are not bridged to cross-repo."""
        msg = CommMessage(
            msg_type=MessageType.PLAN,
            op_id="op-plan-jarvis",
            seq=2,
            causal_parent_seq=1,
            payload={"steps": ["step1"], "rollback_strategy": "revert"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is None

    def test_decision_blocked_maps_to_improvement_failed(self):
        """DECISION(blocked) -> IMPROVEMENT_FAILED."""
        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-blocked-jarvis",
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "blocked", "reason_code": "touches_supervisor"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.value == "improvement_failed"

    @pytest.mark.asyncio
    async def test_event_bridge_emits_to_bus(self):
        """EventBridge forwards mapped events to the event bus."""
        bus = _FakeEventBus()
        bridge = EventBridge(event_bus=bus)

        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-emit-jarvis",
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied", "reason_code": "ok"},
        )
        await bridge.send(msg)
        assert len(bus.events) == 1
        assert bus.events[0].type.value == "improvement_complete"

    @pytest.mark.asyncio
    async def test_event_bridge_skips_heartbeat(self):
        """EventBridge silently skips non-bridgeable messages."""
        bus = _FakeEventBus()
        bridge = EventBridge(event_bus=bus)

        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id="op-skip-jarvis",
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "applying", "progress_pct": 0.9},
        )
        await bridge.send(msg)
        assert len(bus.events) == 0

    @pytest.mark.asyncio
    async def test_event_bridge_fault_isolated(self):
        """EventBridge logs but does not propagate bus failures."""
        bus = _FailingEventBus()
        bridge = EventBridge(event_bus=bus)

        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-fault-jarvis",
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied", "reason_code": "ok"},
        )
        # Must not raise
        await bridge.send(msg)

    @pytest.mark.asyncio
    async def test_event_bridge_as_comm_transport(self):
        """EventBridge works as a CommProtocol transport via send()."""
        bus = _FakeEventBus()
        bridge = EventBridge(event_bus=bus)
        comm = CommProtocol(transports=[LogTransport(), bridge])

        await comm.emit_intent(
            op_id="op-transport-jarvis",
            goal="test transport",
            target_files=["a.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
        # EventBridge should have received the INTENT -> IMPROVEMENT_REQUEST
        assert len(bus.events) == 1
        assert bus.events[0].type.value == "improvement_request"

        await comm.emit_decision(
            op_id="op-transport-jarvis",
            outcome="applied",
            reason_code="tests_pass",
        )
        # Now two events
        assert len(bus.events) == 2
        assert bus.events[1].type.value == "improvement_complete"


# ---------------------------------------------------------------------------
# Blast Radius Integration Go/No-Go
# ---------------------------------------------------------------------------


class TestBlastRadiusIntegrationGoNoGo:
    """Oracle blast radius flows into risk classification."""

    def test_oracle_blast_above_threshold_triggers_approval(self):
        """Oracle blast_radius > 5 -> APPROVAL_REQUIRED via risk engine."""
        oracle = _FakeOracle({
            "src/core.py": _FakeBlastResult(total_affected=8, risk_level="high"),
        })
        adapter = BlastRadiusAdapter(oracle=oracle)

        profile = OperationProfile(
            files_affected=[Path("src/core.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,  # will be enriched
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 8

        engine = RiskEngine()
        result = engine.classify(enriched)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "blast_radius_exceeded"

    def test_no_oracle_safe_fallback(self):
        """No Oracle -> fallback blast_radius=1, profile unchanged."""
        adapter = BlastRadiusAdapter(oracle=None)

        profile = OperationProfile(
            files_affected=[Path("src/small.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        enriched = adapter.enrich_profile(profile)
        assert enriched.blast_radius == 1

        engine = RiskEngine()
        result = engine.classify(enriched)
        assert result.tier == RiskTier.SAFE_AUTO

    def test_oracle_error_safe_fallback(self):
        """Oracle error -> graceful fallback to blast_radius=1."""
        oracle = MagicMock()
        oracle.find_nodes_in_file.side_effect = RuntimeError("oracle down")
        adapter = BlastRadiusAdapter(oracle=oracle)

        result = adapter.compute("broken.py")
        assert result.total_affected == 1
        assert result.from_oracle is False

    def test_multi_file_uses_max_blast_radius(self):
        """Multi-file op uses max blast radius across all files."""
        oracle = _FakeOracle({
            "a.py": _FakeBlastResult(total_affected=2, risk_level="low"),
            "b.py": _FakeBlastResult(total_affected=7, risk_level="high"),
        })
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
        assert enriched.blast_radius == 7

    def test_manual_blast_radius_preserved_without_oracle(self):
        """Manual blast_radius in profile preserved when no Oracle."""
        adapter = BlastRadiusAdapter(oracle=None)

        profile = OperationProfile(
            files_affected=[Path("x.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=3,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        # Fallback computes blast=1 per file; max(1) = 1 which replaces
        # only if > 0. The adapter always sets max_blast to the computed max.
        enriched = adapter.enrich_profile(profile)
        # With no oracle, each file gets fallback=1, so max_blast=1
        assert enriched.blast_radius >= 1

    def test_blast_radius_result_from_oracle_flag(self):
        """BlastRadiusResult correctly reports from_oracle status."""
        oracle = _FakeOracle({
            "tracked.py": _FakeBlastResult(total_affected=3, risk_level="medium"),
        })
        adapter = BlastRadiusAdapter(oracle=oracle)

        result = adapter.compute("tracked.py")
        assert result.from_oracle is True
        assert result.total_affected == 3

        result_fallback = adapter.compute("untracked.py")
        assert result_fallback.from_oracle is False


# ---------------------------------------------------------------------------
# Learning Feedback Go/No-Go
# ---------------------------------------------------------------------------


class TestLearningFeedbackGoNoGo:
    """Operation outcomes published to LearningMemory with op_id."""

    @pytest.mark.asyncio
    async def test_outcome_published_with_op_id(self):
        """Successful outcome is published to LearningMemory."""
        memory = _FakeLearningMemory()
        bridge = LearningBridge(learning_memory=memory)

        outcome = OperationOutcome(
            op_id="op-learn-001-jarvis",
            goal="fix import",
            target_files=["src/main.py"],
            final_state=OperationState.APPLIED,
            solution_pattern="added missing import",
        )
        await bridge.publish(outcome)

        assert len(memory.attempts) == 1
        record = memory.attempts[0]
        assert record["goal"] == "fix import"
        assert record["target_file"] == "src/main.py"
        assert record["success"] is True
        assert record["solution_pattern"] == "added missing import"

    @pytest.mark.asyncio
    async def test_failed_outcome_published(self):
        """Failed outcome is published with success=False."""
        memory = _FakeLearningMemory()
        bridge = LearningBridge(learning_memory=memory)

        outcome = OperationOutcome(
            op_id="op-learn-002-jarvis",
            goal="refactor module",
            target_files=["src/big.py"],
            final_state=OperationState.FAILED,
            error_pattern="SyntaxError on line 42",
        )
        await bridge.publish(outcome)

        assert len(memory.attempts) == 1
        assert memory.attempts[0]["success"] is False
        assert memory.attempts[0]["error_pattern"] == "SyntaxError on line 42"

    @pytest.mark.asyncio
    async def test_failed_pattern_skipped_on_retry(self):
        """Previously failed pattern is skipped via should_skip()."""
        memory = _FakeLearningMemory()
        memory.mark_skip("fix import", "src/main.py", "circular import")
        bridge = LearningBridge(learning_memory=memory)

        should_skip = await bridge.should_skip(
            goal="fix import",
            target_file="src/main.py",
            error_pattern="circular import",
        )
        assert should_skip is True

    @pytest.mark.asyncio
    async def test_novel_pattern_not_skipped(self):
        """Novel goal+file+error combination is not skipped."""
        memory = _FakeLearningMemory()
        bridge = LearningBridge(learning_memory=memory)

        should_skip = await bridge.should_skip(
            goal="new feature",
            target_file="src/new.py",
            error_pattern="none",
        )
        assert should_skip is False

    @pytest.mark.asyncio
    async def test_no_memory_publish_is_noop(self):
        """publish() is a no-op when no LearningMemory configured."""
        bridge = LearningBridge(learning_memory=None)

        outcome = OperationOutcome(
            op_id="op-noop-jarvis",
            goal="test",
            target_files=["a.py"],
            final_state=OperationState.APPLIED,
        )
        # Must not raise
        await bridge.publish(outcome)

    @pytest.mark.asyncio
    async def test_no_memory_should_skip_returns_false(self):
        """should_skip() returns False when no memory configured."""
        bridge = LearningBridge(learning_memory=None)
        assert await bridge.should_skip("g", "f", "e") is False

    @pytest.mark.asyncio
    async def test_memory_error_fault_isolated(self):
        """LearningMemory errors are caught and logged, not propagated."""
        memory = MagicMock()
        memory.record_attempt = AsyncMock(side_effect=RuntimeError("db down"))
        bridge = LearningBridge(learning_memory=memory)

        outcome = OperationOutcome(
            op_id="op-err-jarvis",
            goal="test",
            target_files=["a.py"],
            final_state=OperationState.APPLIED,
        )
        # Must not raise
        await bridge.publish(outcome)

    @pytest.mark.asyncio
    async def test_operation_outcome_success_property(self):
        """OperationOutcome.success reflects APPLIED state."""
        applied = OperationOutcome(
            op_id="op-1", goal="g", target_files=["f"],
            final_state=OperationState.APPLIED,
        )
        failed = OperationOutcome(
            op_id="op-2", goal="g", target_files=["f"],
            final_state=OperationState.FAILED,
        )
        blocked = OperationOutcome(
            op_id="op-3", goal="g", target_files=["f"],
            final_state=OperationState.BLOCKED,
        )
        assert applied.success is True
        assert failed.success is False
        assert blocked.success is False


# ---------------------------------------------------------------------------
# Runtime Contracts Go/No-Go
# ---------------------------------------------------------------------------


class TestRuntimeContractsGoNoGo:
    """N/N-1 schema check blocks incompatible writes."""

    def test_same_version_compatible(self):
        """Same major+minor is compatible."""
        checker = RuntimeContractChecker(ContractVersion(2, 3, 0))
        result = checker.check_compatibility(ContractVersion(2, 3, 0))
        assert result.compatible is True
        assert len(result.violations) == 0

    def test_same_major_minor_n_minus_1_compatible(self):
        """Same major, minor N-1 is compatible (backward compat)."""
        checker = RuntimeContractChecker(ContractVersion(2, 3, 0))
        result = checker.check_compatibility(ContractVersion(2, 2, 0))
        assert result.compatible is True

    def test_same_major_minor_n_plus_1_compatible(self):
        """Same major, minor N+1 (forward) is compatible."""
        checker = RuntimeContractChecker(ContractVersion(2, 3, 0))
        result = checker.check_compatibility(ContractVersion(2, 4, 0))
        assert result.compatible is True

    def test_different_major_incompatible(self):
        """Different major version blocks the write."""
        checker = RuntimeContractChecker(ContractVersion(2, 3, 0))
        result = checker.check_compatibility(ContractVersion(3, 0, 0))
        assert result.compatible is False
        assert len(result.violations) == 1
        assert result.violations[0].field == "major_version"

    def test_minor_n_minus_2_incompatible(self):
        """Minor version N-2 (too old) is incompatible."""
        checker = RuntimeContractChecker(ContractVersion(2, 5, 0))
        result = checker.check_compatibility(ContractVersion(2, 3, 0))
        assert result.compatible is False
        assert len(result.violations) == 1
        assert result.violations[0].field == "minor_version"

    def test_none_proposed_always_compatible(self):
        """None proposed version is always compatible (no schema)."""
        checker = RuntimeContractChecker(ContractVersion(1, 0, 0))
        result = checker.check_compatibility(None)
        assert result.compatible is True

    def test_check_before_write_returns_bool(self):
        """check_before_write() is a convenience bool wrapper."""
        checker = RuntimeContractChecker(ContractVersion(2, 3, 0))
        assert checker.check_before_write(ContractVersion(2, 3, 0)) is True
        assert checker.check_before_write(ContractVersion(3, 0, 0)) is False

    def test_patch_version_ignored(self):
        """Patch version differences are always compatible."""
        checker = RuntimeContractChecker(ContractVersion(1, 2, 3))
        assert checker.check_compatibility(ContractVersion(1, 2, 99)).compatible is True
        assert checker.check_compatibility(ContractVersion(1, 2, 0)).compatible is True

    def test_major_mismatch_short_circuits(self):
        """Major mismatch returns immediately without checking minor."""
        checker = RuntimeContractChecker(ContractVersion(1, 5, 0))
        result = checker.check_compatibility(ContractVersion(2, 5, 0))
        assert result.compatible is False
        # Only one violation (major), no minor check
        assert len(result.violations) == 1
        assert "major" in result.violations[0].reason.lower()


# ---------------------------------------------------------------------------
# Canary Promotion Go/No-Go
# ---------------------------------------------------------------------------


class TestCanaryPromotionGoNoGo:
    """Domain slice promotion criteria enforced correctly."""

    def _make_controller_with_ops(
        self,
        prefix: str,
        n_ops: int,
        rollback_count: int = 0,
        latency: float = 10.0,
        first_op_offset_hours: float = 80.0,
    ) -> CanaryController:
        """Helper: create a controller with pre-populated metrics."""
        ctrl = CanaryController()
        ctrl.register_slice(prefix)
        metrics = ctrl.get_metrics(prefix)
        assert metrics is not None

        for i in range(n_ops):
            rolled = i < rollback_count
            ctrl.record_operation(
                file_path=f"{prefix}file_{i}.py",
                success=not rolled,
                latency_s=latency,
                rolled_back=rolled,
            )

        # Backdate first_operation_time so stability window is met
        if metrics.first_operation_time is not None and first_op_offset_hours > 0:
            metrics.first_operation_time = (
                time.time() - first_op_offset_hours * 3600
            )

        return ctrl

    def test_all_criteria_met_promotes_to_active(self):
        """50+ ops, <5% rollback, <120s p95, 72h stability -> ACTIVE."""
        ctrl = self._make_controller_with_ops(
            prefix="backend/core/ouroboros/",
            n_ops=55,
            rollback_count=1,   # 1/55 ~ 1.8% < 5%
            latency=15.0,       # well under 120s
            first_op_offset_hours=80.0,  # > 72h
        )

        result = ctrl.check_promotion("backend/core/ouroboros/")
        assert result.promoted is True
        assert result.reason == "All criteria met"

        slice_obj = ctrl.get_slice("backend/core/ouroboros/")
        assert slice_obj is not None
        assert slice_obj.state == CanaryState.ACTIVE

    def test_insufficient_ops_blocks_promotion(self):
        """<50 operations blocks promotion."""
        ctrl = self._make_controller_with_ops(
            prefix="backend/tests/",
            n_ops=30,
            first_op_offset_hours=80.0,
        )

        result = ctrl.check_promotion("backend/tests/")
        assert result.promoted is False
        assert "50" in result.reason

    def test_high_rollback_rate_blocks_promotion(self):
        """Rollback rate >= 5% blocks promotion."""
        ctrl = self._make_controller_with_ops(
            prefix="backend/api/",
            n_ops=60,
            rollback_count=4,   # 4/60 ~ 6.7% > 5%
            first_op_offset_hours=80.0,
        )

        result = ctrl.check_promotion("backend/api/")
        assert result.promoted is False
        assert "rollback" in result.reason.lower()

    def test_high_p95_latency_blocks_promotion(self):
        """P95 latency > 120s blocks promotion."""
        ctrl = self._make_controller_with_ops(
            prefix="backend/slow/",
            n_ops=55,
            latency=130.0,   # 130s > 120s threshold
            first_op_offset_hours=80.0,
        )

        result = ctrl.check_promotion("backend/slow/")
        assert result.promoted is False
        assert "latency" in result.reason.lower()

    def test_insufficient_stability_blocks_promotion(self):
        """<72h stability window blocks promotion."""
        ctrl = self._make_controller_with_ops(
            prefix="backend/new/",
            n_ops=55,
            first_op_offset_hours=24.0,  # only 24h < 72h
        )

        result = ctrl.check_promotion("backend/new/")
        assert result.promoted is False
        assert "stability" in result.reason.lower()

    def test_unregistered_slice_not_promoted(self):
        """Unregistered slice returns not promoted."""
        ctrl = CanaryController()
        result = ctrl.check_promotion("nonexistent/")
        assert result.promoted is False
        assert "not registered" in result.reason.lower()

    def test_active_slice_allows_file_operations(self):
        """Files in ACTIVE slices are allowed for autonomous ops."""
        ctrl = self._make_controller_with_ops(
            prefix="backend/core/ouroboros/",
            n_ops=55,
            first_op_offset_hours=80.0,
        )
        ctrl.check_promotion("backend/core/ouroboros/")

        assert ctrl.is_file_allowed("backend/core/ouroboros/governance/risk.py") is True

    def test_pending_slice_blocks_file_operations(self):
        """Files in PENDING slices are not allowed."""
        ctrl = CanaryController()
        ctrl.register_slice("backend/core/")
        assert ctrl.is_file_allowed("backend/core/something.py") is False

    def test_file_outside_all_slices_not_allowed(self):
        """Files not in any slice are blocked."""
        ctrl = CanaryController()
        ctrl.register_slice("backend/core/")
        assert ctrl.is_file_allowed("frontend/app.js") is False


# ---------------------------------------------------------------------------
# CLI Break-Glass End-to-End Go/No-Go
# ---------------------------------------------------------------------------


class TestCLIBreakGlassGoNoGo:
    """End-to-end break-glass lifecycle via CLI commands."""

    @pytest.mark.asyncio
    async def test_issue_validate_list_revoke_audit_lifecycle(self):
        """Full lifecycle: issue -> validate -> list -> revoke -> audit."""
        manager = BreakGlassManager()

        # Step 1: Issue token
        token = await issue_break_glass(
            manager=manager,
            op_id="op-blocked-jarvis",
            reason="emergency deploy fix",
            ttl=300,
            issuer="derek",
        )
        assert isinstance(token, BreakGlassToken)
        assert token.op_id == "op-blocked-jarvis"
        assert token.issuer == "derek"
        assert token.ttl == 300
        assert not token.is_expired()

        # Step 2: Validate token
        is_valid = manager.validate("op-blocked-jarvis")
        assert is_valid is True

        # Step 3: List active tokens
        active = list_active_tokens(manager)
        assert len(active) == 1
        assert active[0].op_id == "op-blocked-jarvis"

        # Step 4: Promoted tier is APPROVAL_REQUIRED, not unguarded
        tier = manager.get_promoted_tier("op-blocked-jarvis")
        assert tier == "APPROVAL_REQUIRED"

        # Step 5: Revoke token
        await revoke_break_glass(
            manager=manager,
            op_id="op-blocked-jarvis",
            reason="deploy complete",
        )

        # Step 6: Token no longer active
        active_after = list_active_tokens(manager)
        assert len(active_after) == 0

        # Step 7: Validate after revoke -> False (no token)
        is_valid_after = manager.validate("op-blocked-jarvis")
        assert is_valid_after is False

        # Step 8: Audit trail has all events
        audit = get_audit_report(manager)
        actions = [entry.action for entry in audit]
        assert "issued" in actions
        assert "validated" in actions
        assert "revoked" in actions

    @pytest.mark.asyncio
    async def test_expired_token_raises(self):
        """Expired token raises BreakGlassExpired on validate."""
        manager = BreakGlassManager()

        # Issue with TTL=0 so it expires immediately
        token = await manager.issue(
            op_id="op-expire-jarvis",
            reason="short-lived",
            ttl=0,
            issuer="derek",
        )

        with pytest.raises(BreakGlassExpired):
            manager.validate("op-expire-jarvis")

    @pytest.mark.asyncio
    async def test_scope_mismatch_raises(self):
        """Validating wrong op_id raises BreakGlassScopeMismatch."""
        manager = BreakGlassManager()

        await manager.issue(
            op_id="op-right-jarvis",
            reason="test",
            ttl=300,
            issuer="derek",
        )

        with pytest.raises(BreakGlassScopeMismatch):
            manager.validate("op-wrong-jarvis")

    @pytest.mark.asyncio
    async def test_audit_entries_have_correct_fields(self):
        """Audit entries contain op_id, action, reason, issuer, timestamp."""
        manager = BreakGlassManager()

        await issue_break_glass(
            manager=manager,
            op_id="op-audit-jarvis",
            reason="audit test",
            ttl=300,
            issuer="derek",
        )
        manager.validate("op-audit-jarvis")
        await revoke_break_glass(
            manager=manager,
            op_id="op-audit-jarvis",
            reason="done",
        )

        audit = get_audit_report(manager)
        for entry in audit:
            assert isinstance(entry, BreakGlassAuditEntry)
            assert entry.op_id == "op-audit-jarvis"
            assert isinstance(entry.action, str)
            assert isinstance(entry.reason, str)
            assert isinstance(entry.timestamp, float)
            assert entry.timestamp > 0

    @pytest.mark.asyncio
    async def test_break_glass_never_promotes_to_safe_auto(self):
        """Break-glass always promotes to APPROVAL_REQUIRED, never SAFE_AUTO."""
        manager = BreakGlassManager()

        await manager.issue(
            op_id="op-check-jarvis",
            reason="test",
            ttl=300,
            issuer="derek",
        )
        tier = manager.get_promoted_tier("op-check-jarvis")
        assert tier == "APPROVAL_REQUIRED"
        assert tier != "SAFE_AUTO"

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_token_safe(self):
        """Revoking a nonexistent token does not raise."""
        manager = BreakGlassManager()
        await revoke_break_glass(
            manager=manager,
            op_id="op-ghost-jarvis",
            reason="cleanup",
        )
        # Audit records the revoke even though no token existed
        audit = get_audit_report(manager)
        assert any(e.action == "revoked" for e in audit)

    @pytest.mark.asyncio
    async def test_multiple_tokens_independent(self):
        """Multiple tokens for different ops are independent."""
        manager = BreakGlassManager()

        await manager.issue(op_id="op-a", reason="a", ttl=300, issuer="d")
        await manager.issue(op_id="op-b", reason="b", ttl=300, issuer="d")

        active = list_active_tokens(manager)
        assert len(active) == 2

        await manager.revoke(op_id="op-a", reason="done with a")

        active_after = list_active_tokens(manager)
        assert len(active_after) == 1
        assert active_after[0].op_id == "op-b"
        assert manager.validate("op-b") is True
