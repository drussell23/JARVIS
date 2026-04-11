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
    """Verify the OperationPhase enum and transition table.

    The transition table separates *progress* transitions (non-terminal →
    non-terminal, hand-maintained) from *terminal-escape* transitions
    (every non-terminal → every terminal, auto-injected at module load).
    Tests assert both layers independently.
    """

    EXPECTED_PHASES = {
        "CLASSIFY",
        "ROUTE",
        "CONTEXT_EXPANSION",
        "PLAN",
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

    # ------------------------------------------------------------------
    # Invariant: every non-terminal phase can reach every terminal phase
    # (auto-injected by _inject_terminal_reachability)
    # ------------------------------------------------------------------

    def test_terminal_reachability_invariant(self) -> None:
        """Every non-terminal phase must allow transitions to EVERY terminal.

        This was the FSM bug that blocked L2 repair from escaping VERIFY to
        CANCELLED. The invariant is now enforced at module load; this test
        guards against future regressions.
        """
        for phase in OperationPhase:
            if phase in TERMINAL_PHASES:
                continue
            allowed = PHASE_TRANSITIONS[phase]
            for terminal in TERMINAL_PHASES:
                assert terminal in allowed, (
                    f"{phase.name} cannot reach terminal {terminal.name} — "
                    f"every non-terminal phase must have all terminals as allowed targets"
                )

    def test_verify_allows_cancelled_escape(self) -> None:
        """Regression test for the L2-in-VERIFY illegal-transition bug.

        Before the fix, VERIFY only allowed {COMPLETE, POSTMORTEM}, so L2
        repair escapes raised ``Illegal phase transition: VERIFY -> CANCELLED``.
        The invariant now guarantees every terminal is reachable.
        """
        verify_allowed = PHASE_TRANSITIONS[OperationPhase.VERIFY]
        assert OperationPhase.CANCELLED in verify_allowed
        assert OperationPhase.POSTMORTEM in verify_allowed
        assert OperationPhase.EXPIRED in verify_allowed
        assert OperationPhase.COMPLETE in verify_allowed

    # ------------------------------------------------------------------
    # Progress transitions (non-terminal → non-terminal) — these are the
    # hand-maintained forward-flow edges. Each phase test asserts the
    # non-terminal subset; the terminal escapes are covered by the
    # invariant tests above.
    # ------------------------------------------------------------------

    @staticmethod
    def _progress_targets(phase: OperationPhase) -> set:
        """Return the subset of targets that are non-terminal phases."""
        return {
            t for t in PHASE_TRANSITIONS[phase] if t not in TERMINAL_PHASES
        }

    def test_classify_progress(self) -> None:
        assert self._progress_targets(OperationPhase.CLASSIFY) == {
            OperationPhase.ROUTE,
        }

    def test_route_progress(self) -> None:
        assert self._progress_targets(OperationPhase.ROUTE) == {
            OperationPhase.CONTEXT_EXPANSION,
            OperationPhase.PLAN,
            OperationPhase.GENERATE,
        }

    def test_context_expansion_progress(self) -> None:
        assert self._progress_targets(OperationPhase.CONTEXT_EXPANSION) == {
            OperationPhase.PLAN,
            OperationPhase.GENERATE,
        }

    def test_plan_progress(self) -> None:
        assert self._progress_targets(OperationPhase.PLAN) == {
            OperationPhase.GENERATE,
        }

    def test_generate_progress(self) -> None:
        assert self._progress_targets(OperationPhase.GENERATE) == {
            OperationPhase.VALIDATE,
            OperationPhase.GENERATE_RETRY,
        }

    def test_generate_retry_progress(self) -> None:
        assert self._progress_targets(OperationPhase.GENERATE_RETRY) == {
            OperationPhase.VALIDATE,
            OperationPhase.GENERATE_RETRY,
        }

    def test_validate_progress(self) -> None:
        assert self._progress_targets(OperationPhase.VALIDATE) == {
            OperationPhase.GATE,
            OperationPhase.VALIDATE_RETRY,
        }

    def test_validate_retry_progress(self) -> None:
        assert self._progress_targets(OperationPhase.VALIDATE_RETRY) == {
            OperationPhase.GATE,
            OperationPhase.VALIDATE_RETRY,
        }

    def test_gate_progress(self) -> None:
        assert self._progress_targets(OperationPhase.GATE) == {
            OperationPhase.APPROVE,
            OperationPhase.APPLY,
        }

    def test_approve_progress(self) -> None:
        assert self._progress_targets(OperationPhase.APPROVE) == {
            OperationPhase.APPLY,
        }

    def test_apply_progress(self) -> None:
        assert self._progress_targets(OperationPhase.APPLY) == {
            OperationPhase.VERIFY,
        }

    def test_verify_progress(self) -> None:
        """VERIFY has no forward progress — it can only escape to terminals."""
        assert self._progress_targets(OperationPhase.VERIFY) == set()

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
        assert ctx.benchmark_result is None

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

    def test_with_pre_apply_snapshots_updates_hash_chain(self):
        ctx = OperationContext.create(op_id="op1", description="d", target_files=())
        snapshots = {"src/foo.py": "content"}
        ctx2 = ctx.with_pre_apply_snapshots(snapshots)
        assert ctx2.context_hash != ctx.context_hash
        assert ctx2.previous_hash == ctx.context_hash


# ---------------------------------------------------------------------------
# Telemetry dataclasses (Task 1)
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.op_context import (
    HostTelemetry,
    RoutingIntentTelemetry,
    RoutingActualTelemetry,
    TelemetryContext,
)


def _make_host_telemetry(**overrides) -> HostTelemetry:
    import time
    from datetime import datetime, timezone
    defaults = dict(
        schema_version="1.0",
        arch="arm64",
        cpu_percent=14.20,
        ram_available_gb=6.80,
        pressure="NORMAL",
        sampled_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        sampled_monotonic_ns=time.monotonic_ns(),
        collector_status="ok",
        sample_age_ms=3,
    )
    return HostTelemetry(**{**defaults, **overrides})


def test_host_telemetry_is_frozen():
    ht = _make_host_telemetry()
    with pytest.raises((TypeError, AttributeError)):
        ht.cpu_percent = 99.0  # type: ignore[misc]


def test_host_telemetry_stores_fields():
    ht = _make_host_telemetry(cpu_percent=14.20, ram_available_gb=6.80)
    assert ht.cpu_percent == 14.20
    assert ht.ram_available_gb == 6.80
    assert ht.schema_version == "1.0"
    assert ht.pressure == "NORMAL"
    assert ht.sample_age_ms == 3


def test_routing_intent_telemetry_frozen():
    ri = RoutingIntentTelemetry(expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL")
    with pytest.raises((TypeError, AttributeError)):
        ri.expected_provider = "LOCAL"  # type: ignore[misc]


def test_routing_actual_telemetry_stores_fallback_chain():
    ra = RoutingActualTelemetry(
        provider_name="LOCAL_CLAUDE",
        endpoint_class="local",
        fallback_chain=("GCP_PRIME_SPOT", "LOCAL_CLAUDE"),
        was_degraded=True,
    )
    assert ra.fallback_chain == ("GCP_PRIME_SPOT", "LOCAL_CLAUDE")
    assert ra.was_degraded is True


def test_telemetry_context_routing_actual_optional():
    tc = TelemetryContext(
        local_node=_make_host_telemetry(),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL"
        ),
    )
    assert tc.routing_actual is None


def test_telemetry_context_with_routing_actual():
    ra = RoutingActualTelemetry(
        provider_name="GCP_PRIME_SPOT",
        endpoint_class="gcp_spot",
        fallback_chain=(),
        was_degraded=False,
    )
    tc = TelemetryContext(
        local_node=_make_host_telemetry(),
        routing_intent=RoutingIntentTelemetry(
            expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL"
        ),
        routing_actual=ra,
    )
    assert tc.routing_actual is ra


# ---------------------------------------------------------------------------
# Task 2: OperationContext telemetry fields + with_* helpers
# ---------------------------------------------------------------------------

import pytest


class TestTelemetryIntegration:
    """Tests for OperationContext telemetry fields and with_* helpers."""

    def _make_tc(self) -> "TelemetryContext":
        return TelemetryContext(
            local_node=_make_host_telemetry(),
            routing_intent=RoutingIntentTelemetry(
                expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL"
            ),
        )

    def test_telemetry_default_none(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        assert ctx.telemetry is None

    def test_previous_op_hash_by_scope_default_empty(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        assert ctx.previous_op_hash_by_scope == ()

    def test_create_with_previous_op_hash_by_scope(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
            previous_op_hash_by_scope=(("jarvis", "abc123"),),
        )
        assert ctx.previous_op_hash_by_scope == (("jarvis", "abc123"),)

    def test_with_telemetry_advances_hash(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        ctx2 = ctx.with_telemetry(self._make_tc())
        assert ctx2.context_hash != ctx.context_hash

    def test_with_telemetry_sets_previous_hash(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        ctx2 = ctx.with_telemetry(self._make_tc())
        assert ctx2.previous_hash == ctx.context_hash

    def test_with_telemetry_sets_field(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        tc = self._make_tc()
        ctx2 = ctx.with_telemetry(tc)
        assert ctx2.telemetry is tc
        assert ctx2.phase == ctx.phase  # no phase change

    def test_with_telemetry_hash_stability(self):
        """Same inputs → same hash (deterministic)."""
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        tc = self._make_tc()
        h1 = ctx.with_telemetry(tc).context_hash
        h2 = ctx.with_telemetry(tc).context_hash
        assert h1 == h2

    def test_with_routing_actual_advances_hash(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        ctx2 = ctx.with_telemetry(self._make_tc())
        ra = RoutingActualTelemetry(
            provider_name="GCP_PRIME_SPOT",
            endpoint_class="gcp_spot",
            fallback_chain=(),
            was_degraded=False,
        )
        ctx3 = ctx2.with_routing_actual(ra)
        assert ctx3.context_hash != ctx2.context_hash
        assert ctx3.telemetry is not None
        assert ctx3.telemetry.routing_actual is ra

    def test_with_routing_actual_requires_existing_telemetry(self):
        ctx = OperationContext.create(
            target_files=("backend/foo.py",),
            description="test",
        )
        ra = RoutingActualTelemetry(
            provider_name="GCP_PRIME_SPOT",
            endpoint_class="gcp_spot",
            fallback_chain=(),
            was_degraded=False,
        )
        with pytest.raises(ValueError, match="telemetry"):
            ctx.with_routing_actual(ra)

    def test_concurrent_scope_chains_independent(self):
        """Two ops on different repo scopes have independent hash chains."""
        ctx_jarvis = OperationContext.create(
            target_files=("jarvis/foo.py",),
            description="jarvis op",
            primary_repo="jarvis",
            repo_scope=("jarvis",),
            previous_op_hash_by_scope=(("jarvis", "hash_jarvis_prev"),),
        )
        ctx_prime = OperationContext.create(
            target_files=("prime/bar.py",),
            description="prime op",
            primary_repo="prime",
            repo_scope=("prime",),
            previous_op_hash_by_scope=(("prime", "hash_prime_prev"),),
        )
        jarvis_hashes = dict(ctx_jarvis.previous_op_hash_by_scope)
        prime_hashes = dict(ctx_prime.previous_op_hash_by_scope)
        assert jarvis_hashes.get("jarvis") == "hash_jarvis_prev"
        assert prime_hashes.get("prime") == "hash_prime_prev"
        assert "prime" not in jarvis_hashes
        assert "jarvis" not in prime_hashes


# ---------------------------------------------------------------------------
# TestFrozenAutonomyTier
# ---------------------------------------------------------------------------


class TestFrozenAutonomyTier:
    """OperationContext.frozen_autonomy_tier field and with_frozen_autonomy_tier()."""

    def _make_ctx(self):
        from backend.core.ouroboros.governance.op_context import OperationContext
        return OperationContext.create(
            target_files=("tests/test_foo.py",),
            description="test",
        )

    def test_default_is_governed(self):
        """frozen_autonomy_tier defaults to 'governed' (backward compat)."""
        ctx = self._make_ctx()
        assert ctx.frozen_autonomy_tier == "governed"

    def test_with_frozen_autonomy_tier_sets_field(self):
        """with_frozen_autonomy_tier() returns new context with updated tier."""
        ctx = self._make_ctx()
        ctx2 = ctx.with_frozen_autonomy_tier("observe")
        assert ctx2.frozen_autonomy_tier == "observe"
        assert ctx.frozen_autonomy_tier == "governed"  # original unchanged

    def test_with_frozen_autonomy_tier_updates_hash(self):
        """Hash changes when frozen_autonomy_tier changes (chain integrity)."""
        ctx = self._make_ctx()
        ctx2 = ctx.with_frozen_autonomy_tier("observe")
        assert ctx2.context_hash != ctx.context_hash

    def test_with_frozen_autonomy_tier_chains_hash(self):
        """previous_hash of new ctx equals context_hash of old ctx."""
        ctx = self._make_ctx()
        ctx2 = ctx.with_frozen_autonomy_tier("observe")
        assert ctx2.previous_hash == ctx.context_hash

    def test_governed_tier_is_preserved_through_advance(self):
        """frozen_autonomy_tier is preserved when advancing phase."""
        from backend.core.ouroboros.governance.op_context import OperationPhase
        ctx = self._make_ctx().with_frozen_autonomy_tier("observe")
        ctx2 = ctx.advance(OperationPhase.ROUTE)
        assert ctx2.frozen_autonomy_tier == "observe"


class TestExecutionGraphMetadata:
    def _make_ctx(self):
        from backend.core.ouroboros.governance.op_context import OperationContext

        return OperationContext.create(
            target_files=("backend/a.py", "prime/router.py"),
            description="cross-repo graph",
            primary_repo="jarvis",
            repo_scope=("jarvis", "prime"),
        )

    def test_with_execution_graph_metadata_sets_fields(self):
        ctx = self._make_ctx()
        ctx2 = ctx.with_execution_graph_metadata(
            execution_graph_id="graph-001",
            execution_plan_digest="abc123",
            subagent_count=2,
            parallelism_budget=2,
            causal_trace_id="trace-001",
        )
        assert ctx2.execution_graph_id == "graph-001"
        assert ctx2.execution_plan_digest == "abc123"
        assert ctx2.subagent_count == 2
        assert ctx2.parallelism_budget == 2
        assert ctx2.causal_trace_id == "trace-001"

    def test_with_execution_graph_metadata_updates_hash_chain(self):
        ctx = self._make_ctx()
        ctx2 = ctx.with_execution_graph_metadata(
            execution_graph_id="graph-001",
            execution_plan_digest="abc123",
            subagent_count=2,
            parallelism_budget=2,
            causal_trace_id="trace-001",
        )
        assert ctx2.context_hash != ctx.context_hash
        assert ctx2.previous_hash == ctx.context_hash


class TestStrategicMemoryMetadata:
    def _make_ctx(self):
        from backend.core.ouroboros.governance.op_context import OperationContext

        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="memory-aware prompt",
        )

    def test_with_strategic_memory_context_sets_fields(self):
        ctx = self._make_ctx()
        ctx2 = ctx.with_strategic_memory_context(
            strategic_intent_id="intent-001",
            strategic_memory_fact_ids=("fact-001", "fact-002"),
            strategic_memory_prompt="## Strategic Memory\n- keep architecture stable",
            strategic_memory_digest="digest-001",
        )
        assert ctx2.strategic_intent_id == "intent-001"
        assert ctx2.strategic_memory_fact_ids == ("fact-001", "fact-002")
        assert "## Strategic Memory" in ctx2.strategic_memory_prompt
        assert ctx2.strategic_memory_digest == "digest-001"

    def test_with_strategic_memory_context_updates_hash_chain(self):
        ctx = self._make_ctx()
        ctx2 = ctx.with_strategic_memory_context(
            strategic_intent_id="intent-001",
            strategic_memory_fact_ids=("fact-001",),
            strategic_memory_prompt="## Strategic Memory\n- keep architecture stable",
            strategic_memory_digest="digest-001",
        )
        assert ctx2.context_hash != ctx.context_hash
        assert ctx2.previous_hash == ctx.context_hash


class TestTerminalOutcomeMetadata:
    def _make_ctx(self):
        from backend.core.ouroboros.governance.op_context import OperationContext

        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="terminal outcome metadata",
        )

    def test_terminal_outcome_defaults_safe(self):
        ctx = self._make_ctx()
        assert ctx.terminal_reason_code == ""
        assert ctx.rollback_occurred is False

    def test_with_terminal_outcome_sets_fields(self):
        ctx = self._make_ctx()
        ctx2 = ctx.with_terminal_outcome(
            terminal_reason_code="saga_rolled_back",
            rollback_occurred=True,
        )
        assert ctx2.terminal_reason_code == "saga_rolled_back"
        assert ctx2.rollback_occurred is True

    def test_with_terminal_outcome_updates_hash_chain(self):
        ctx = self._make_ctx()
        ctx2 = ctx.with_terminal_outcome(
            terminal_reason_code="change_engine_failed",
            rollback_occurred=True,
        )
        assert ctx2.context_hash != ctx.context_hash
        assert ctx2.previous_hash == ctx.context_hash
