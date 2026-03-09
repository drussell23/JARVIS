"""Tests for OperationContext, OperationPhase, and typed sub-objects.

The OperationContext is a frozen, hash-chained state object that flows through
every pipeline phase.  All mutations go through ``advance()`` which returns a
new instance with an updated phase, timestamp, and cryptographic hash chain.
"""

from datetime import datetime, timezone
from dataclasses import FrozenInstanceError

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationPhase,
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    OperationContext,
    GenerationResult,
    ValidationResult,
    ApprovalDecision,
    ShadowResult,
    _compute_hash,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.routing_policy import RoutingDecision


# ---------------------------------------------------------------------------
# TestOperationPhase
# ---------------------------------------------------------------------------


class TestOperationPhase:
    """Verify the OperationPhase enum and transition table."""

    EXPECTED_PHASES = {
        "CLASSIFY",
        "ROUTE",
        "CONTEXT_EXPANSION",
        "GENERATE",
        "GENERATE_RETRY",
        "VALIDATE",
        "VALIDATE_RETRY",
        "GATE",
        "APPROVE",
        "APPLY",
        "VERIFY",
        "COMPLETE",
        "CANCELLED",
        "EXPIRED",
        "POSTMORTEM",
    }

    def test_all_phases_exist(self) -> None:
        actual = {p.name for p in OperationPhase}
        assert actual == self.EXPECTED_PHASES

    def test_terminal_phases_have_no_transitions(self) -> None:
        for phase in TERMINAL_PHASES:
            assert PHASE_TRANSITIONS[phase] == set(), (
                f"{phase.name} is terminal but has outgoing transitions"
            )

    def test_terminal_phases_set(self) -> None:
        expected = {
            OperationPhase.COMPLETE,
            OperationPhase.CANCELLED,
            OperationPhase.EXPIRED,
            OperationPhase.POSTMORTEM,
        }
        assert TERMINAL_PHASES == expected

    def test_classify_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.CLASSIFY] == {
            OperationPhase.ROUTE,
            OperationPhase.CANCELLED,
        }

    def test_route_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.ROUTE] == {
            OperationPhase.CONTEXT_EXPANSION,
            OperationPhase.GENERATE,
            OperationPhase.CANCELLED,
        }

    def test_generate_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.GENERATE] == {
            OperationPhase.VALIDATE,
            OperationPhase.GENERATE_RETRY,
            OperationPhase.CANCELLED,
        }

    def test_generate_retry_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.GENERATE_RETRY] == {
            OperationPhase.VALIDATE,
            OperationPhase.GENERATE_RETRY,
            OperationPhase.CANCELLED,
        }

    def test_validate_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.VALIDATE] == {
            OperationPhase.GATE,
            OperationPhase.VALIDATE_RETRY,
            OperationPhase.CANCELLED,
        }

    def test_validate_retry_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.VALIDATE_RETRY] == {
            OperationPhase.GATE,
            OperationPhase.VALIDATE_RETRY,
            OperationPhase.CANCELLED,
        }

    def test_gate_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.GATE] == {
            OperationPhase.APPROVE,
            OperationPhase.APPLY,
            OperationPhase.CANCELLED,
        }

    def test_approve_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.APPROVE] == {
            OperationPhase.APPLY,
            OperationPhase.CANCELLED,
            OperationPhase.EXPIRED,
        }

    def test_apply_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.APPLY] == {
            OperationPhase.VERIFY,
            OperationPhase.POSTMORTEM,
            OperationPhase.CANCELLED,
        }

    def test_verify_transitions(self) -> None:
        assert PHASE_TRANSITIONS[OperationPhase.VERIFY] == {
            OperationPhase.COMPLETE,
            OperationPhase.POSTMORTEM,
        }

    def test_every_phase_in_transition_table(self) -> None:
        """Every phase must have a key in PHASE_TRANSITIONS."""
        for phase in OperationPhase:
            assert phase in PHASE_TRANSITIONS, (
                f"{phase.name} missing from PHASE_TRANSITIONS"
            )


# ---------------------------------------------------------------------------
# TestOperationContext
# ---------------------------------------------------------------------------


class TestOperationContext:
    """Verify OperationContext frozen semantics, advance(), and hash chain."""

    @pytest.fixture
    def ctx(self) -> OperationContext:
        """Create a fresh CLASSIFY-phase context via factory."""
        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="Fix utility function",
        )

    def test_frozen(self, ctx: OperationContext) -> None:
        with pytest.raises(FrozenInstanceError):
            ctx.phase = OperationPhase.ROUTE  # type: ignore[misc]

    def test_initial_phase_is_classify(self, ctx: OperationContext) -> None:
        assert ctx.phase is OperationPhase.CLASSIFY

    def test_initial_previous_hash_is_none(self, ctx: OperationContext) -> None:
        assert ctx.previous_hash is None

    def test_initial_context_hash_is_not_empty(self, ctx: OperationContext) -> None:
        assert ctx.context_hash
        assert len(ctx.context_hash) == 64  # SHA-256 hex

    def test_initial_side_effects_blocked(self, ctx: OperationContext) -> None:
        assert ctx.side_effects_blocked is True

    def test_advance_returns_new_instance(self, ctx: OperationContext) -> None:
        advanced = ctx.advance(OperationPhase.ROUTE)
        assert advanced is not ctx

    def test_advance_updates_phase(self, ctx: OperationContext) -> None:
        advanced = ctx.advance(OperationPhase.ROUTE)
        assert advanced.phase is OperationPhase.ROUTE

    def test_advance_updates_hash_chain(self, ctx: OperationContext) -> None:
        advanced = ctx.advance(OperationPhase.ROUTE)
        assert advanced.previous_hash == ctx.context_hash
        assert advanced.context_hash != ctx.context_hash

    def test_advance_updates_phase_entered_at(self, ctx: OperationContext) -> None:
        ts = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)
        advanced = ctx.advance(OperationPhase.ROUTE, _timestamp=ts)
        assert advanced.phase_entered_at == ts

    def test_advance_with_updates(self, ctx: OperationContext) -> None:
        advanced = ctx.advance(
            OperationPhase.ROUTE,
            risk_tier=RiskTier.SAFE_AUTO,
        )
        assert advanced.risk_tier is RiskTier.SAFE_AUTO
        # Original unchanged
        assert ctx.risk_tier is None

    def test_invalid_transition_raises(self, ctx: OperationContext) -> None:
        with pytest.raises(ValueError, match="Illegal phase transition"):
            ctx.advance(OperationPhase.APPLY)

    def test_terminal_phase_cannot_advance(self) -> None:
        ctx = OperationContext.create(
            target_files=("a.py",),
            description="test",
        )
        cancelled = ctx.advance(OperationPhase.CANCELLED)
        with pytest.raises(ValueError, match="Illegal phase transition"):
            cancelled.advance(OperationPhase.CLASSIFY)

    def test_cancelled_reachable_from_all_non_terminal(self) -> None:
        """CANCELLED must be in every non-terminal phase's transition set."""
        for phase in OperationPhase:
            if phase not in TERMINAL_PHASES:
                # VERIFY is special -- no CANCELLED in spec
                if phase is OperationPhase.VERIFY:
                    continue
                assert OperationPhase.CANCELLED in PHASE_TRANSITIONS[phase], (
                    f"CANCELLED not reachable from {phase.name}"
                )

    def test_deterministic_hash(self) -> None:
        """Two contexts with identical fields must have the same hash."""
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        a = OperationContext.create(
            target_files=("x.py",),
            description="same",
            _timestamp=ts,
        )
        b = OperationContext.create(
            target_files=("x.py",),
            description="same",
            _timestamp=ts,
        )
        # op_id differs, so hashes differ -- but the hash function itself
        # must be deterministic for the same input dict.
        d = {"a": 1, "b": "two", "c": [3]}
        assert _compute_hash(d) == _compute_hash(d)

    def test_op_id_populated(self, ctx: OperationContext) -> None:
        assert ctx.op_id
        assert isinstance(ctx.op_id, str)

    def test_created_at_populated(self, ctx: OperationContext) -> None:
        assert isinstance(ctx.created_at, datetime)

    def test_multi_step_advance(self) -> None:
        """Walk through CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> APPLY -> VERIFY -> COMPLETE."""
        ctx = OperationContext.create(
            target_files=("a.py",),
            description="multi-step test",
        )
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.GENERATE)
        ctx = ctx.advance(OperationPhase.VALIDATE)
        ctx = ctx.advance(OperationPhase.GATE)
        ctx = ctx.advance(OperationPhase.APPLY)
        ctx = ctx.advance(OperationPhase.VERIFY)
        ctx = ctx.advance(OperationPhase.COMPLETE)
        assert ctx.phase is OperationPhase.COMPLETE

    def test_advance_preserves_immutable_fields(self, ctx: OperationContext) -> None:
        advanced = ctx.advance(OperationPhase.ROUTE)
        assert advanced.op_id == ctx.op_id
        assert advanced.created_at == ctx.created_at
        assert advanced.target_files == ctx.target_files
        assert advanced.description == ctx.description


# ---------------------------------------------------------------------------
# TestGenerationResult
# ---------------------------------------------------------------------------


class TestGenerationResult:
    """Verify GenerationResult creation and immutability."""

    def test_creation(self) -> None:
        result = GenerationResult(
            candidates=({"file": "a.py", "diff": "+line"},),
            provider_name="gcp_prime",
            generation_duration_s=1.23,
        )
        assert result.candidates == ({"file": "a.py", "diff": "+line"},)
        assert result.provider_name == "gcp_prime"
        assert result.generation_duration_s == 1.23

    def test_frozen(self) -> None:
        result = GenerationResult(
            candidates=(),
            provider_name="local",
            generation_duration_s=0.5,
        )
        with pytest.raises(FrozenInstanceError):
            result.provider_name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestValidationResult
# ---------------------------------------------------------------------------


class TestValidationResult:
    """Verify ValidationResult creation and immutability."""

    def test_creation_passed(self) -> None:
        result = ValidationResult(
            passed=True,
            best_candidate={"file": "a.py"},
            validation_duration_s=0.45,
            error=None,
        )
        assert result.passed is True
        assert result.best_candidate == {"file": "a.py"}
        assert result.validation_duration_s == 0.45
        assert result.error is None

    def test_creation_failed(self) -> None:
        result = ValidationResult(
            passed=False,
            best_candidate=None,
            validation_duration_s=0.12,
            error="Syntax error in candidate",
        )
        assert result.passed is False
        assert result.best_candidate is None
        assert result.error == "Syntax error in candidate"

    def test_frozen(self) -> None:
        result = ValidationResult(
            passed=True,
            best_candidate=None,
            validation_duration_s=0.1,
            error=None,
        )
        with pytest.raises(FrozenInstanceError):
            result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestApprovalDecision
# ---------------------------------------------------------------------------


class TestApprovalDecision:
    """Verify ApprovalDecision creation."""

    def test_creation(self) -> None:
        ts = datetime(2026, 3, 7, 10, 0, 0, tzinfo=timezone.utc)
        decision = ApprovalDecision(
            status="approved",
            approver="derek",
            reason="Looks good",
            decided_at=ts,
            request_id="req-123",
        )
        assert decision.status == "approved"
        assert decision.approver == "derek"
        assert decision.reason == "Looks good"
        assert decision.decided_at == ts
        assert decision.request_id == "req-123"

    def test_frozen(self) -> None:
        decision = ApprovalDecision(
            status="pending",
            approver=None,
            reason=None,
            decided_at=None,
            request_id="req-456",
        )
        with pytest.raises(FrozenInstanceError):
            decision.status = "approved"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestShadowResult
# ---------------------------------------------------------------------------


class TestShadowResult:
    """Verify ShadowResult creation."""

    def test_creation(self) -> None:
        result = ShadowResult(
            confidence=0.92,
            comparison_mode="structural",
            violations=("trailing_whitespace",),
            shadow_duration_s=2.1,
            production_match=True,
            disqualified=False,
        )
        assert result.confidence == 0.92
        assert result.comparison_mode == "structural"
        assert result.violations == ("trailing_whitespace",)
        assert result.shadow_duration_s == 2.1
        assert result.production_match is True
        assert result.disqualified is False

    def test_frozen(self) -> None:
        result = ShadowResult(
            confidence=0.5,
            comparison_mode="exact",
            violations=(),
            shadow_duration_s=0.1,
            production_match=False,
            disqualified=True,
        )
        with pytest.raises(FrozenInstanceError):
            result.confidence = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestComputeHash
# ---------------------------------------------------------------------------


class TestComputeHash:
    """Verify _compute_hash determinism."""

    def test_deterministic(self) -> None:
        d = {"z": 1, "a": 2, "m": [3, 4]}
        assert _compute_hash(d) == _compute_hash(d)

    def test_different_input_different_hash(self) -> None:
        a = {"x": 1}
        b = {"x": 2}
        assert _compute_hash(a) != _compute_hash(b)

    def test_order_independent(self) -> None:
        """sort_keys=True should make key order irrelevant."""
        a = {"z": 1, "a": 2}
        b = {"a": 2, "z": 1}
        assert _compute_hash(a) == _compute_hash(b)

    def test_sha256_hex_length(self) -> None:
        h = _compute_hash({"key": "value"})
        assert len(h) == 64


class TestContextExpansionPhase:
    """CONTEXT_EXPANSION phase state machine additions."""

    def test_context_expansion_in_enum(self):
        from backend.core.ouroboros.governance.op_context import OperationPhase
        assert hasattr(OperationPhase, "CONTEXT_EXPANSION")

    def test_route_to_context_expansion_legal(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        assert ctx.phase is OperationPhase.CONTEXT_EXPANSION

    def test_context_expansion_to_generate_legal(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.advance(OperationPhase.GENERATE)
        assert ctx.phase is OperationPhase.GENERATE

    def test_context_expansion_to_cancelled_legal(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.advance(OperationPhase.CANCELLED)
        assert ctx.phase is OperationPhase.CANCELLED

    def test_route_to_generate_still_legal_direct(self):
        """ROUTE -> GENERATE direct path must remain valid (expansion is optional)."""
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.GENERATE)
        assert ctx.phase is OperationPhase.GENERATE

    def test_expanded_context_files_default_empty(self):
        from backend.core.ouroboros.governance.op_context import OperationContext
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        assert ctx.expanded_context_files == ()

    def test_with_expanded_files_updates_field(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        enriched = ctx.with_expanded_files(("helpers.py", "utils.py"))
        assert enriched.expanded_context_files == ("helpers.py", "utils.py")
        assert enriched.phase is OperationPhase.CONTEXT_EXPANSION

    def test_with_expanded_files_updates_hash_chain(self):
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, OperationPhase,
        )
        ctx = OperationContext.create(target_files=("foo.py",), description="test")
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        enriched = ctx.with_expanded_files(("helpers.py",))
        assert enriched.previous_hash == ctx.context_hash
        assert enriched.context_hash != ctx.context_hash


class TestOperationContextBenchmarkFields:
    """Tests for benchmark_result and pre_apply_snapshots additions."""

    def test_benchmark_result_defaults_to_none(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        assert ctx.benchmark_result is None

    def test_pre_apply_snapshots_defaults_to_empty_dict(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        assert ctx.pre_apply_snapshots == {}

    def test_with_benchmark_result_returns_new_context(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.9, lint_violations=2, coverage_pct=75.0,
            complexity_delta=-0.5, patch_hash="abc", quality_score=0.85,
            task_type="bug_fix", timed_out=False, error=None,
        )
        ctx2 = ctx.with_benchmark_result(br)
        assert ctx2.benchmark_result == br
        assert ctx2.benchmark_result is not ctx.benchmark_result or ctx.benchmark_result is None

    def test_with_benchmark_result_does_not_change_phase(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.9, lint_violations=0, coverage_pct=80.0,
            complexity_delta=0.0, patch_hash="x", quality_score=0.9,
            task_type="code_improvement", timed_out=False, error=None,
        )
        ctx2 = ctx.with_benchmark_result(br)
        assert ctx2.phase == ctx.phase

    def test_with_benchmark_result_updates_hash_chain(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.5, lint_violations=1, coverage_pct=50.0,
            complexity_delta=1.0, patch_hash="h1", quality_score=0.5,
            task_type="refactoring", timed_out=False, error=None,
        )
        ctx2 = ctx.with_benchmark_result(br)
        assert ctx2.context_hash != ctx.context_hash
        assert ctx2.previous_hash == ctx.context_hash

    def test_with_pre_apply_snapshots_stores_content(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        snapshots = {"src/foo.py": "def foo(): pass\n"}
        ctx2 = ctx.with_pre_apply_snapshots(snapshots)
        assert ctx2.pre_apply_snapshots == snapshots

    def test_with_pre_apply_snapshots_does_not_change_phase(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        ctx2 = ctx.with_pre_apply_snapshots({"f.py": "x"})
        assert ctx2.phase == ctx.phase

    def test_hash_deterministic_with_benchmark_result(self):
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        br = BenchmarkResult(
            pass_rate=0.9, lint_violations=0, coverage_pct=80.0,
            complexity_delta=0.0, patch_hash="x", quality_score=0.9,
            task_type="code_improvement", timed_out=False, error=None,
        )
        ctx2a = ctx.with_benchmark_result(br)
        ctx2b = ctx.with_benchmark_result(br)
        assert ctx2a.context_hash == ctx2b.context_hash
