"""SBT-Probe Escalation Bridge Slice 3 — graduation regression spine.

Verifies the full Slices 1-3 stack composes end-to-end after the
master flag flips default-true and dynamic registration discovers
all 3 modules' contributions.

Coverage:
  * Master flag default-true post-graduation
  * Master flag explicit false reverts to disabled
  * All 3 SBT escalation flags discovered automatically by
    FlagRegistry seed loop
  * All 7 SBT escalation AST-pin invariants discovered;
    validate clean against current source
  * End-to-end through executor with production prober adapter:
    EXHAUSTED → escalation triggers → SBT spawns 3 branches via
    rotation → CONVERGED via fake resolver → executor returns
    RETRY_WITH_FEEDBACK with tree fingerprint
  * Backward-compat: master flag explicit-false → executor
    behavior reverts to legacy INCONCLUSIVE
"""
from __future__ import annotations

import asyncio
from typing import Optional, Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.flag_registry import FlagRegistry
from backend.core.ouroboros.governance.flag_registry_seed import (
    seed_default_registry,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_all,
)
from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (
    ConvergenceVerdict,
    ProbeAnswer,
    ProbeOutcome,
    ProbeQuestion,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (
    AmbiguityContext,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
    ConfidenceCollapseAction,
)
from backend.core.ouroboros.governance.verification.probe_environment_executor import (
    execute_probe_environment,
)
from backend.core.ouroboros.governance.verification.sbt_branch_prober_adapter import (
    ReadonlyBranchProberAdapter,
    reset_default_branch_prober_for_tests,
)
from backend.core.ouroboros.governance.verification.sbt_escalation_bridge import (
    sbt_escalation_enabled,
)


# ---------------------------------------------------------------------------
# Master flag flip
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_ESCALATION_ENABLED", raising=False)
        assert sbt_escalation_enabled() is True

    def test_empty_string_is_default_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "")
        assert sbt_escalation_enabled() is True

    def test_whitespace_is_default_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "   ")
        assert sbt_escalation_enabled() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "FALSE"])
    def test_explicit_false_disables(self, monkeypatch, falsy: str):
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", falsy)
        assert sbt_escalation_enabled() is False


# ---------------------------------------------------------------------------
# Dynamic flag discovery
# ---------------------------------------------------------------------------


class TestFlagDiscovery:
    def test_seed_discovers_all_3_sbt_escalation_flags(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        sbt_flags = [
            f for f in registry.list_all()
            if "SBT_ESCALATION" in f.name
        ]
        assert len(sbt_flags) == 3

    def test_master_flag_present_default_true(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec("JARVIS_SBT_ESCALATION_ENABLED")
        assert spec is not None
        assert spec.default is True

    def test_cost_flag_present(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec(
            "JARVIS_SBT_ESCALATION_MAX_COST_USD",
        )
        assert spec is not None
        assert spec.default == 0.10

    def test_time_flag_present(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec("JARVIS_SBT_ESCALATION_MAX_TIME_S")
        assert spec is not None
        assert spec.default == 90.0


# ---------------------------------------------------------------------------
# Dynamic AST-pin discovery + clean validation
# ---------------------------------------------------------------------------


class TestInvariantDiscovery:
    def test_all_7_sbt_invariants_discovered(self):
        invs = list_shipped_code_invariants()
        sbt_invs = [
            i for i in invs
            if "sbt_escalation" in i.invariant_name
            or "sbt_branch_prober" in i.invariant_name
        ]
        assert len(sbt_invs) == 7

    def test_each_module_contributes_invariants(self):
        invs = list_shipped_code_invariants()
        names = {
            i.invariant_name for i in invs
            if "sbt_escalation" in i.invariant_name
            or "sbt_branch_prober" in i.invariant_name
        }
        # Slice 1 bridge: 3 invariants
        assert "sbt_escalation_bridge_pure_stdlib" in names
        assert "sbt_escalation_bridge_taxonomy_5_values" in names
        assert (
            "sbt_escalation_bridge_collapse_mapping_complete" in names
        )
        # Slice 2 runner: 2 invariants
        assert (
            "sbt_escalation_runner_authority_allowlist" in names
        )
        assert "sbt_escalation_runner_optional_return" in names
        # Slice 3 adapter: 2 invariants
        assert (
            "sbt_branch_prober_adapter_authority_allowlist" in names
        )
        assert (
            "sbt_branch_prober_adapter_uses_readonly_allowlist" in names
        )

    def test_all_sbt_invariants_validate_clean(self):
        violations = validate_all()
        sbt_v = [
            v for v in violations
            if "sbt_escalation" in v.invariant_name
            or "sbt_branch_prober" in v.invariant_name
        ]
        assert sbt_v == [], (
            f"SBT invariants drifted: "
            f"{[(v.invariant_name, v.detail) for v in sbt_v]}"
        )


# ---------------------------------------------------------------------------
# End-to-end through executor with production adapter
# ---------------------------------------------------------------------------


class _ConvergingFakeResolver:
    """Fake QuestionResolver that returns the same answer text
    regardless of question — branches converge on the same
    fingerprint."""

    def resolve(
        self, question: ProbeQuestion, *,
        max_tool_rounds: Optional[int] = None,
    ) -> ProbeAnswer:
        return ProbeAnswer(
            question=question.question,
            answer_text="canonical resolved answer",
            evidence_fingerprint="fp",
            tool_rounds_used=1,
        )


class TestEndToEndExecutor:
    @pytest.mark.asyncio
    async def test_escalation_overrides_inconclusive_via_real_adapter(
        self, monkeypatch,
    ):
        """E2E: master flag on (graduated default) + executor's
        EXHAUSTED branch → real ReadonlyBranchProberAdapter wired
        with a converging resolver → SBT CONVERGED → executor
        returns RETRY_WITH_FEEDBACK with tree fingerprint."""
        monkeypatch.delenv(
            "JARVIS_SBT_ESCALATION_ENABLED", raising=False,
        )

        async def _fake_loop(*args, **kwargs):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.EXHAUSTED,
                agreement_count=1,
                distinct_count=2,
                total_answers=3,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="probe ran out of budget",
            )

        # Inject a converging resolver into the singleton adapter.
        reset_default_branch_prober_for_tests()
        from backend.core.ouroboros.governance.verification import (
            sbt_branch_prober_adapter as adapter_mod,
        )
        custom_adapter = ReadonlyBranchProberAdapter(
            resolver=_ConvergingFakeResolver(),
        )
        monkeypatch.setattr(
            adapter_mod, "_default_adapter", custom_adapter,
        )

        with patch(
            "backend.core.ouroboros.governance.verification."
            "probe_environment_executor.run_probe_loop",
            _fake_loop,
        ):
            ctx = AmbiguityContext(
                op_id="op-grad-e2e",
                target_symbol="some_symbol",
                claim="some_claim",
            )
            result = await execute_probe_environment(
                monitor=object(),
                ambiguity_context=ctx,
                op_id="op-grad-e2e",
            )

        # Escalation took effect: action = RETRY_WITH_FEEDBACK
        # (SBT CONVERGED maps to RETRY); state names the converged
        # path; feedback contains the tree fingerprint.
        assert result.action is ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert result.convergence_state == "sbt_escalation_converged"
        assert "Speculative branch tree converged" in result.feedback_text

    @pytest.mark.asyncio
    async def test_master_flag_explicit_false_reverts_to_legacy(
        self, monkeypatch,
    ):
        """Backward-compat hot revert: explicit
        JARVIS_SBT_ESCALATION_ENABLED=false → executor's EXHAUSTED
        branch reverts to legacy INCONCLUSIVE behavior."""
        monkeypatch.setenv("JARVIS_SBT_ESCALATION_ENABLED", "false")

        async def _fake_loop(*args, **kwargs):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.EXHAUSTED,
                agreement_count=1,
                distinct_count=2,
                total_answers=3,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="forced exhausted",
            )

        with patch(
            "backend.core.ouroboros.governance.verification."
            "probe_environment_executor.run_probe_loop",
            _fake_loop,
        ):
            ctx = AmbiguityContext(claim="test")
            result = await execute_probe_environment(
                monitor=object(),
                ambiguity_context=ctx,
                op_id="op-revert",
            )
        # Reverted to legacy.
        assert result.action is ConfidenceCollapseAction.INCONCLUSIVE
        assert result.convergence_state == "probe_exhausted"
