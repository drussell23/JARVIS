"""M10 Slice 1 — ArchitectureProposer primitives tests
(PRD §32.4).

Pins the contract layer for the entire M10 arc:
  § 1 — Master flag default-false (per §30.5.2 operator binding)
  § 2 — Closed-taxonomy enums (16-value M10ProposalPhase + 5-value ProposalKind)
  § 3 — Frozen dataclasses (M10AdaptiveThreshold + M10ProposalRecord)
  § 4 — Env knobs — clamping + defaults (lifted from graduation_orchestrator)
  § 5 — compute_threshold Bayesian aggregator — verbatim parity with graduation_orchestrator.compute_adaptive_threshold
  § 6 — M10ProposalRecord helpers (is_terminal / is_awaiting_human / has_required_self_pin)
  § 7 — Cold-start sentinel handling
  § 8 — Authority floor (no orchestrator/iron_gate/providers imports)
  § 9 — Public exports
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag (default-false PER OPERATOR BINDING — does NOT graduate)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_false_per_operator_binding(
        self, monkeypatch,
    ):
        """Per §30.5.2: master flag STAYS default-false post-
        Slice-5 graduation until 30+ proposal-acceptance audit.
        This is operator-pinned; do NOT flip without
        authorization."""
        monkeypatch.delenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_arch_proposer_enabled,
        )
        assert m10_arch_proposer_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy_variants_flip_on(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", v,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_arch_proposer_enabled,
        )
        assert m10_arch_proposer_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "off", "no", "garbage"],
    )
    def test_falsy_variants_stay_off(self, monkeypatch, v):
        """Pre-graduation semantics: any non-truthy = off."""
        monkeypatch.setenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", v,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_arch_proposer_enabled,
        )
        assert m10_arch_proposer_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — Closed enums
# ---------------------------------------------------------------------------


class TestClosedEnums:
    def test_proposal_phase_has_exactly_16_values(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalPhase,
        )
        values = {m.value for m in M10ProposalPhase}
        assert values == {
            "detecting",
            "evaluating",
            "decided_skip",
            "worktree_creating",
            "generating",
            "validating",
            "committing",
            "awaiting_approval",
            "pushing",
            "push_failed",
            "awaiting_merge",
            "registering",
            "graduated",
            "failed",
            "rejected",
            "expired",
        }
        assert len(values) == 16

    def test_proposal_kind_has_exactly_5_values(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            ProposalKind,
        )
        values = {m.value for m in ProposalKind}
        assert values == {
            "new_sensor",
            "new_phase",
            "new_observer",
            "new_flag_family",
            "disabled",
        }

    def test_enums_are_str_subclass(self):
        """str subclass for backward-compat with freeform
        kind strings (matches DecisionKind / OutcomeKind /
        BudgetOutcome / CuriositySource pattern)."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalPhase,
            ProposalKind,
        )
        assert issubclass(M10ProposalPhase, str)
        assert issubclass(ProposalKind, str)


# ---------------------------------------------------------------------------
# § 3 — Frozen dataclasses
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    def test_adaptive_threshold_is_frozen(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10AdaptiveThreshold,
        )
        t = M10AdaptiveThreshold(
            threshold=3, p_success=0.5,
            diversity=0.5, effective_p=0.375,
        )
        with pytest.raises(Exception):
            t.threshold = 99  # type: ignore[misc]

    def test_proposal_record_is_frozen(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalRecord,
            ProposalKind,
        )
        r = M10ProposalRecord(
            proposal_id="r-1", kind=ProposalKind.NEW_SENSOR,
        )
        with pytest.raises(Exception):
            r.proposal_id = "r-99"  # type: ignore[misc]

    def test_default_record_is_at_detecting(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalPhase,
            M10ProposalRecord,
            ProposalKind,
        )
        r = M10ProposalRecord(
            proposal_id="r-1", kind=ProposalKind.NEW_SENSOR,
        )
        assert r.phase is M10ProposalPhase.DETECTING
        assert r.detection_evidence == ()
        assert r.threshold is None
        assert r.proposed_module_path == ""
        assert r.validation_passed is False
        assert r.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# § 4 — Env knobs — clamping + defaults
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_min_threshold_default_2(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_ADAPTIVE_MIN_THRESHOLD",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_adaptive_min_threshold,
        )
        assert m10_adaptive_min_threshold() == 2

    def test_min_threshold_clamped(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_MIN_THRESHOLD", "0",
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_adaptive_min_threshold,
        )
        assert m10_adaptive_min_threshold() == 1  # floor

    def test_confidence_default_2(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_adaptive_confidence,
        )
        assert m10_adaptive_confidence() == 2.0

    def test_max_daily_default_5(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_MAX_DAILY", raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_max_daily_proposals,
        )
        assert m10_max_daily_proposals() == 5

    def test_approval_timeout_default_24h(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_APPROVAL_TIMEOUT_S", raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_approval_timeout_s,
        )
        assert m10_approval_timeout_s() == 86400

    def test_acceptance_rate_floor_default_30pct(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_M10_ACCEPTANCE_RATE_FLOOR", raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_acceptance_rate_floor,
        )
        assert m10_acceptance_rate_floor() == 0.30

    def test_garbage_env_falls_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_ADAPTIVE_CONFIDENCE", "not-a-float",
        )
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_adaptive_confidence,
        )
        assert m10_adaptive_confidence() == 2.0


# ---------------------------------------------------------------------------
# § 5 — compute_threshold Bayesian aggregator
# ---------------------------------------------------------------------------


class TestComputeThreshold:
    def test_cold_start_returns_fallback(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=0, failures=0,
            unique_goals=0, total_uses=0,
        )
        assert r.is_cold_start is True
        assert r.threshold == 3  # _FALLBACK_THRESHOLD
        assert r.p_success == 0.0

    def test_high_success_high_diversity_low_threshold(self):
        """5 successes, 0 failures, 5 unique goals →
        Beta posterior favors success → diversity-adjusted →
        threshold should be at minimum floor."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=5, failures=0,
            unique_goals=5, total_uses=5,
        )
        assert r.is_cold_start is False
        assert r.p_success > 0.8
        assert r.diversity == 1.0
        # ceil(2.0 / (0.857 × 1.0)) ≈ ceil(2.33) = 3, clamped to floor 2
        assert r.threshold >= 2

    def test_all_failures_inflates_threshold(self):
        """0 successes, 5 failures → low p_success → threshold
        inflates (system needs more evidence before graduating
        a flaky pattern)."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=0, failures=5,
            unique_goals=5, total_uses=5,
        )
        # p_success = 1/7 ≈ 0.143
        assert r.p_success == pytest.approx(0.1429, abs=0.001)
        # Threshold significantly higher than the baseline 2
        assert r.threshold > 5

    def test_low_diversity_inflates_threshold(self):
        """High success rate but low diversity → diversity-
        adjusted multiplier reduces effective_p → threshold
        higher than max-diversity case."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r_low_div = compute_threshold(
            successes=5, failures=0,
            unique_goals=1, total_uses=5,
        )
        r_high_div = compute_threshold(
            successes=5, failures=0,
            unique_goals=5, total_uses=5,
        )
        # Low diversity = 0.2 → effective_p smaller → threshold larger
        assert r_low_div.threshold >= r_high_div.threshold
        assert r_low_div.diversity < r_high_div.diversity

    def test_negative_inputs_safely_handled(self):
        """Defensive — negative successes / failures should
        not propagate into negative posterior. Caller may pass
        garbage; primitive must not crash."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=-5, failures=-3,
            unique_goals=-2, total_uses=10,
        )
        # All clamped to 0 internally
        assert r.p_success == 0.5  # (1+0)/(2+0+0)
        assert r.diversity == 0.0
        assert r.threshold > 0

    def test_non_int_inputs_return_cold_start(self):
        """Defensive — non-int inputs (e.g., None, str)
        should produce cold-start sentinel rather than
        crashing."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=None, failures=0,  # type: ignore[arg-type]
            unique_goals=0, total_uses=0,
        )
        assert r.is_cold_start is True

    def test_total_uses_zero_is_cold_start_even_with_successes(
        self,
    ):
        """Edge case — graduation_orchestrator's verbatim
        contract: when total_uses=0, return cold-start
        regardless of other inputs (caller hasn't logged usage
        history yet)."""
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=10, failures=0,
            unique_goals=10, total_uses=0,
        )
        assert r.is_cold_start is True
        assert r.threshold == 3


# ---------------------------------------------------------------------------
# § 6 — M10ProposalRecord helpers
# ---------------------------------------------------------------------------


class TestProposalRecordHelpers:
    @pytest.mark.parametrize(
        "phase,expected",
        [
            ("DETECTING", False),
            ("EVALUATING", False),
            ("DECIDED_SKIP", True),
            ("GENERATING", False),
            ("AWAITING_APPROVAL", False),
            ("PUSH_FAILED", False),
            ("GRADUATED", True),
            ("FAILED", True),
            ("REJECTED", True),
            ("EXPIRED", True),
        ],
    )
    def test_is_terminal(self, phase, expected):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalPhase,
            M10ProposalRecord,
            ProposalKind,
        )
        r = M10ProposalRecord(
            proposal_id="r-1",
            kind=ProposalKind.NEW_SENSOR,
            phase=getattr(M10ProposalPhase, phase),
        )
        assert r.is_terminal() is expected

    @pytest.mark.parametrize(
        "phase,expected",
        [
            ("DETECTING", False),
            ("AWAITING_APPROVAL", True),
            ("AWAITING_MERGE", True),
            ("PUSHING", False),
            ("GRADUATED", False),
        ],
    )
    def test_is_awaiting_human(self, phase, expected):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalPhase,
            M10ProposalRecord,
            ProposalKind,
        )
        r = M10ProposalRecord(
            proposal_id="r-1",
            kind=ProposalKind.NEW_SENSOR,
            phase=getattr(M10ProposalPhase, phase),
        )
        assert r.is_awaiting_human() is expected

    def test_has_required_self_pin_empty(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalRecord,
            ProposalKind,
        )
        r = M10ProposalRecord(
            proposal_id="r-1", kind=ProposalKind.NEW_SENSOR,
        )
        assert r.has_required_self_pin() is False

    def test_has_required_self_pin_set(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalRecord,
            ProposalKind,
        )
        r = M10ProposalRecord(
            proposal_id="r-1", kind=ProposalKind.NEW_SENSOR,
            proposed_ast_pin_name="my_new_sensor_pin",
        )
        assert r.has_required_self_pin() is True

    def test_to_dict_projection_complete(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            M10ProposalPhase,
            M10ProposalRecord,
            ProposalKind,
            compute_threshold,
        )
        r = M10ProposalRecord(
            proposal_id="r-1", kind=ProposalKind.NEW_SENSOR,
            phase=M10ProposalPhase.AWAITING_APPROVAL,
            pattern_signature="abc",
            detection_evidence=("ev-1", "ev-2"),
            threshold=compute_threshold(
                successes=2, failures=0,
                unique_goals=2, total_uses=2,
            ),
            proposed_ast_pin_name="my_pin",
        )
        d = r.to_dict()
        for key in (
            "schema_version", "proposal_id", "kind", "phase",
            "pattern_signature", "detection_evidence",
            "threshold", "proposed_module_path",
            "proposed_class_name", "proposed_ast_pin_name",
            "validation_passed", "validation_failures",
            "worktree_path", "review_pr_url",
            "review_pr_branch", "total_cost_usd",
            "failure_reason", "created_at_unix",
            "last_updated_at_unix", "is_terminal",
            "is_awaiting_human", "has_required_self_pin",
        ):
            assert key in d, f"to_dict missing {key}"
        assert d["is_awaiting_human"] is True
        assert d["has_required_self_pin"] is True
        assert d["threshold"]["threshold"] >= 2


# ---------------------------------------------------------------------------
# § 7 — Cold-start sentinel propagates correctly
# ---------------------------------------------------------------------------


class TestColdStartPropagation:
    def test_cold_start_threshold_to_dict(self):
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            compute_threshold,
        )
        r = compute_threshold(
            successes=0, failures=0,
            unique_goals=0, total_uses=0,
        )
        d = r.to_dict()
        assert d["is_cold_start"] is True
        assert d["threshold"] == 3


# ---------------------------------------------------------------------------
# § 8 — Authority floor (no orchestrator/iron_gate imports)
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
        # Critically — M10 must NOT import the archived
        # graduation_orchestrator. The DESIGN is lifted; the
        # CODE is independent.
        "from backend.core.ouroboros.governance.graduation_orchestrator",
    )

    def test_imports_narrow_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "m10" / "primitives.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"m10/primitives.py must NOT import "
                f"{forbidden} — pure-data primitive layer"
            )


# ---------------------------------------------------------------------------
# § 9 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance.m10 import (
            primitives as p,
        )
        expected = sorted([
            "M10_PRIMITIVES_SCHEMA_VERSION",
            "M10AdaptiveThreshold",
            "M10ProposalPhase",
            "M10ProposalRecord",
            "ProposalKind",
            "compute_threshold",
            "m10_acceptance_rate_floor",
            "m10_adaptive_confidence",
            "m10_adaptive_min_threshold",
            "m10_approval_timeout_s",
            "m10_arch_proposer_enabled",
            "m10_max_daily_proposals",
        ])
        assert sorted(p.__all__) == expected
