"""SBT-Probe Escalation Runner Slice 2 — async wrapper + executor wire-up tests.

Covers:
  * Wrapper returns None when probe is conclusive (CONVERGED /
    DIVERGED) — no escalation, caller falls through
  * Wrapper returns None when master flag off — backward-compat
  * Wrapper returns None when budget exhausted
  * Wrapper escalates on EXHAUSTED + budget OK + enabled=True
  * SBT CONVERGED → ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
    with feedback containing tree fingerprint
  * SBT DIVERGED → ConfidenceCollapseAction.ESCALATE_TO_OPERATOR
  * SBT INCONCLUSIVE / TRUNCATED / FAILED → INCONCLUSIVE
  * Default null prober → SBT INCONCLUSIVE → wrapper INCONCLUSIVE
  * Asyncio cancellation propagates per asyncio convention
  * Defense-in-depth secondary timeout on wait_for
  * Defensive degradation: garbage probe_verdict / runner raise /
    target ctor failure → safe fallback
  * Authority allowlist (no orchestrator-tier imports)
  * Executor integration: backward-compat (master off → unchanged);
    escalation overrides on EXHAUSTED when fires; falls through
    to existing INCONCLUSIVE when escalation returns None
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (
    ConvergenceVerdict,
    ProbeOutcome,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
    ConfidenceCollapseAction,
    ConfidenceCollapseVerdict,
)
from backend.core.ouroboros.governance.verification.sbt_escalation_runner import (
    DEFAULT_AMBIGUITY_KIND,
    escalate_via_sbt,
)
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    EvidenceKind,
    BranchTreeTarget,
    BranchOutcome,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _convergence_verdict(
    outcome: ProbeOutcome,
    *,
    detail: str = "test",
    canonical: str = "",
) -> ConvergenceVerdict:
    return ConvergenceVerdict(
        outcome=outcome,
        agreement_count=0,
        distinct_count=1,
        total_answers=2,
        canonical_answer=canonical or None,
        canonical_fingerprint=None,
        detail=detail,
    )


class _ConvergingProber:
    """Test prober that returns identical evidence across all
    branches → SBT CONVERGED. Uses a single fixed content_hash so
    every branch produces the same fingerprint."""

    def probe_branch(
        self, *, target, branch_id, depth,
        prior_evidence: Tuple[BranchEvidence, ...] = (),
    ) -> Tuple[BranchEvidence, ...]:
        return (
            BranchEvidence(
                kind=EvidenceKind.SYMBOL_LOOKUP,
                content_hash="a" * 64,  # identical across branches
                confidence=0.9,
                source_tool="list_symbols",
                snippet="canonical answer A",
            ),
        )


class _DivergingProber:
    """Test prober that returns DIFFERENT evidence per branch
    (distinct content_hash per branch_id) → SBT DIVERGED."""

    def probe_branch(
        self, *, target, branch_id, depth,
        prior_evidence: Tuple[BranchEvidence, ...] = (),
    ) -> Tuple[BranchEvidence, ...]:
        # Distinct hash per branch via the branch_id.
        import hashlib
        h = hashlib.sha256(
            str(branch_id).encode("utf-8"),
        ).hexdigest()
        return (
            BranchEvidence(
                kind=EvidenceKind.SYMBOL_LOOKUP,
                content_hash=h,
                confidence=0.9,
                source_tool="list_symbols",
                snippet=f"distinct answer {branch_id}",
            ),
        )


class _RaisingProber:
    """Test prober that raises — runner should catch and convert
    to BranchOutcome.FAILED, tree returns INCONCLUSIVE."""

    def probe_branch(self, **kwargs):
        raise RuntimeError("forced failure")


# ---------------------------------------------------------------------------
# Wrapper short-circuit paths (returns None)
# ---------------------------------------------------------------------------


class TestWrapperShortCircuits:
    @pytest.mark.asyncio
    async def test_converged_probe_returns_none(self):
        """CONVERGED probe doesn't need escalation."""
        v = _convergence_verdict(ProbeOutcome.CONVERGED)
        result = await escalate_via_sbt(v, enabled=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_diverged_probe_returns_none(self):
        v = _convergence_verdict(ProbeOutcome.DIVERGED)
        result = await escalate_via_sbt(v, enabled=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_probe_returns_none(self):
        v = _convergence_verdict(ProbeOutcome.DISABLED)
        result = await escalate_via_sbt(v, enabled=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_failed_probe_returns_none(self):
        v = _convergence_verdict(ProbeOutcome.FAILED)
        result = await escalate_via_sbt(v, enabled=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_master_flag_off_returns_none(self):
        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        result = await escalate_via_sbt(v, enabled=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_none(self):
        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        result = await escalate_via_sbt(
            v, enabled=True,
            cost_so_far_usd=10.0, max_cost_usd=0.10,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_convergence_verdict_input_returns_none(self):
        result = await escalate_via_sbt(
            "not-a-verdict",  # type: ignore[arg-type]
            enabled=True,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Wrapper escalation paths (returns ConfidenceCollapseVerdict)
# ---------------------------------------------------------------------------


class TestWrapperEscalation:
    @pytest.mark.asyncio
    async def test_converging_prober_yields_retry_with_feedback(self):
        v = _convergence_verdict(
            ProbeOutcome.EXHAUSTED, detail="probe ran out of budget",
        )
        result = await escalate_via_sbt(
            v, enabled=True,
            op_id="op-conv", target_descriptor="x.py:1",
            prober=_ConvergingProber(),
        )
        assert result is not None
        assert result.action is ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert "Speculative branch tree converged" in result.feedback_text
        assert result.convergence_state == "sbt_escalation_converged"
        assert result.confidence_posterior > 0.5

    @pytest.mark.asyncio
    async def test_diverging_prober_yields_escalate_to_operator(self):
        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        result = await escalate_via_sbt(
            v, enabled=True,
            op_id="op-div", target_descriptor="y.py:2",
            prober=_DivergingProber(),
        )
        assert result is not None
        assert result.action is ConfidenceCollapseAction.ESCALATE_TO_OPERATOR
        assert result.convergence_state == "sbt_escalation_diverged"
        assert result.confidence_posterior < 0.5

    @pytest.mark.asyncio
    async def test_raising_prober_yields_inconclusive_via_runner_safety(
        self,
    ):
        """Prober raises → runner catches → branches PARTIAL/FAILED →
        tree INCONCLUSIVE → wrapper INCONCLUSIVE. Defensive contract."""
        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        result = await escalate_via_sbt(
            v, enabled=True,
            op_id="op-raise",
            prober=_RaisingProber(),
        )
        assert result is not None
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE
        assert result.thinking_budget_reduction_factor < 1.0

    @pytest.mark.asyncio
    async def test_default_null_prober_yields_inconclusive(self):
        """No prober supplied → SBT NullProber → empty evidence →
        all branches PARTIAL → tree INCONCLUSIVE → wrapper INCONCLUSIVE.
        Safe degraded path; same outcome as caller's existing
        INCONCLUSIVE branch."""
        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        result = await escalate_via_sbt(v, enabled=True)
        assert result is not None
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveDegradation:
    @pytest.mark.asyncio
    async def test_async_cancellation_propagates(self):
        """Caller-initiated cancellation propagates per asyncio
        convention. Caller catches."""
        # Slow prober via asyncio.sleep wrapper.
        class _SlowProber:
            def probe_branch(self, **kwargs):
                # Sync prober — runner wraps via to_thread; cancellation
                # propagates at the to_thread boundary. To simulate
                # cleanly, just take a long time.
                import time
                time.sleep(2.0)
                return ()

        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        task = asyncio.create_task(
            escalate_via_sbt(
                v, enabled=True,
                prober=_SlowProber(),
                max_time_s=10.0,
            ),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_internal_target_construction_failure_returns_inconclusive(
        self, monkeypatch,
    ):
        """Force BranchTreeTarget construction to raise — wrapper
        must return INCONCLUSIVE rather than propagate."""
        from backend.core.ouroboros.governance.verification import (
            sbt_escalation_runner as runner_mod,
        )

        def _raise_ctor(*args, **kwargs):
            raise RuntimeError("forced ctor failure")

        monkeypatch.setattr(
            runner_mod, "BranchTreeTarget", _raise_ctor,
        )
        v = _convergence_verdict(ProbeOutcome.EXHAUSTED)
        result = await escalate_via_sbt(v, enabled=True)
        assert result is not None
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE
        assert "target_construction" in result.convergence_state


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_ambiguity_kind_stable(self):
        assert DEFAULT_AMBIGUITY_KIND == "probe_exhausted"


# ---------------------------------------------------------------------------
# Executor integration
# ---------------------------------------------------------------------------


class TestExecutorIntegration:
    @pytest.mark.asyncio
    async def test_executor_master_off_unchanged(self, monkeypatch):
        """When SBT escalation master is OFF (default), executor's
        EXHAUSTED branch must produce the same INCONCLUSIVE verdict
        as before. Backward-compat invariant."""
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "false")
        from backend.core.ouroboros.governance.verification.confidence_probe_generator import (
            AmbiguityContext,
        )
        from backend.core.ouroboros.governance.verification.probe_environment_executor import (
            execute_probe_environment,
        )

        # Patch run_probe_loop to return EXHAUSTED.
        async def _fake_loop(*args, **kwargs):
            return _convergence_verdict(
                ProbeOutcome.EXHAUSTED,
                detail="forced exhausted for test",
            )

        with patch(
            "backend.core.ouroboros.governance.verification."
            "probe_environment_executor.run_probe_loop",
            _fake_loop,
        ):
            ctx = AmbiguityContext(claim="test ambiguity")
            result = await execute_probe_environment(
                monitor=object(),
                ambiguity_context=ctx,
                op_id="op-back-compat",
            )
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE
        assert result.convergence_state == "probe_exhausted"

    @pytest.mark.asyncio
    async def test_executor_escalation_overrides_inconclusive(
        self, monkeypatch,
    ):
        """When SBT escalation master is ON AND wrapper produces a
        non-None verdict (via converging prober), executor returns
        the escalation verdict (not its own INCONCLUSIVE)."""
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.confidence_probe_generator import (
            AmbiguityContext,
        )
        from backend.core.ouroboros.governance.verification.probe_environment_executor import (
            execute_probe_environment,
        )

        async def _fake_loop(*args, **kwargs):
            return _convergence_verdict(
                ProbeOutcome.EXHAUSTED, detail="forced exhausted",
            )

        # Patch escalate_via_sbt to return a fake CONVERGED-style
        # verdict (we're testing the executor's response to a
        # non-None escalation result, not the runner's own
        # behavior).
        async def _fake_escalate(*args, **kwargs):
            return ConfidenceCollapseVerdict(
                action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
                confidence_posterior=0.85,
                convergence_state="sbt_escalation_converged",
                observation_summary="fake escalation",
                cost_usd=0.0,
                feedback_text="fake feedback",
            )

        with patch(
            "backend.core.ouroboros.governance.verification."
            "probe_environment_executor.run_probe_loop",
            _fake_loop,
        ), patch(
            "backend.core.ouroboros.governance.verification."
            "sbt_escalation_runner.escalate_via_sbt",
            _fake_escalate,
        ):
            ctx = AmbiguityContext(claim="test")
            result = await execute_probe_environment(
                monitor=object(),
                ambiguity_context=ctx,
                op_id="op-overridden",
            )
        # Escalation verdict won.
        assert result.action is ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert result.convergence_state == "sbt_escalation_converged"

    @pytest.mark.asyncio
    async def test_executor_falls_through_when_escalation_returns_none(
        self, monkeypatch,
    ):
        """When wrapper returns None (e.g., budget exhausted),
        executor falls through to its existing INCONCLUSIVE
        branch."""
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.confidence_probe_generator import (
            AmbiguityContext,
        )
        from backend.core.ouroboros.governance.verification.probe_environment_executor import (
            execute_probe_environment,
        )

        async def _fake_loop(*args, **kwargs):
            return _convergence_verdict(
                ProbeOutcome.EXHAUSTED, detail="forced",
            )

        async def _fake_escalate_none(*args, **kwargs):
            return None

        with patch(
            "backend.core.ouroboros.governance.verification."
            "probe_environment_executor.run_probe_loop",
            _fake_loop,
        ), patch(
            "backend.core.ouroboros.governance.verification."
            "sbt_escalation_runner.escalate_via_sbt",
            _fake_escalate_none,
        ):
            ctx = AmbiguityContext(claim="test")
            result = await execute_probe_environment(
                monitor=object(),
                ambiguity_context=ctx,
                op_id="op-falls-through",
            )
        # Fell through to existing INCONCLUSIVE.
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE
        assert result.convergence_state == "probe_exhausted"

    @pytest.mark.asyncio
    async def test_executor_escalation_failure_falls_through_safely(
        self, monkeypatch,
    ):
        """If the SBT escalation wrapper itself raises (defensive
        contract violation), executor must STILL fall through to
        its existing INCONCLUSIVE branch — never propagate."""
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.confidence_probe_generator import (
            AmbiguityContext,
        )
        from backend.core.ouroboros.governance.verification.probe_environment_executor import (
            execute_probe_environment,
        )

        async def _fake_loop(*args, **kwargs):
            return _convergence_verdict(
                ProbeOutcome.EXHAUSTED, detail="forced",
            )

        async def _fake_escalate_raise(*args, **kwargs):
            raise RuntimeError("forced wrapper crash")

        with patch(
            "backend.core.ouroboros.governance.verification."
            "probe_environment_executor.run_probe_loop",
            _fake_loop,
        ), patch(
            "backend.core.ouroboros.governance.verification."
            "sbt_escalation_runner.escalate_via_sbt",
            _fake_escalate_raise,
        ):
            ctx = AmbiguityContext(claim="test")
            result = await execute_probe_environment(
                monitor=object(),
                ambiguity_context=ctx,
                op_id="op-defensive",
            )
        # Executor's catch turned it into the existing INCONCLUSIVE.
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _runner_source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "sbt_escalation_runner.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        allowed = {
            "backend.core.ouroboros.governance.verification.confidence_probe_bridge",
            "backend.core.ouroboros.governance.verification.hypothesis_consumers",
            "backend.core.ouroboros.governance.verification.sbt_escalation_bridge",
            "backend.core.ouroboros.governance.verification.speculative_branch",
            "backend.core.ouroboros.governance.verification.speculative_branch_runner",
        }
        tree = ast.parse(self._runner_source())
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or (
                    "governance" in module and module
                ):
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    if module not in allowed:
                        raise AssertionError(
                            f"Slice 2 imported module outside "
                            f"allowlist: {module!r} at line {lineno}"
                        )

    def test_no_orchestrator_tier_imports(self):
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        tree = ast.parse(self._runner_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for ban in banned_substrings:
                    if ban in module:
                        raise AssertionError(
                            f"Slice 2 imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r}"
                        )

    def test_no_exec_eval_compile_calls(self):
        tree = ast.parse(self._runner_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 2 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )
