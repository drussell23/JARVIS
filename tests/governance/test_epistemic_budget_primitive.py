"""Upgrade 1 Slice 1 — EpistemicBudget primitive tests (PRD §31.2).

Pins the **full contract layer** for the entire 5-slice arc:
  * Master flag default-FALSE (Slice 5 graduation flips to TRUE)
  * EpistemicBudget 14-field shape (every field Slice 2 will
    populate + every field Slice 3 will read MUST be named here)
  * ConfidenceTrajectory nested structure shape
  * ConfidenceSample primitive shape
  * BudgetOutcome 7-value closed enum (every routing branch
    Slice 3 will dispatch on)
  * BudgetAction result shape
  * compute_budget_action decision tree — all 7 outcomes via
    independent test cases
  * Cost-gated routes refuse PROBE / SBT structurally
    (BG / SPECULATIVE)
  * No-duplication contract — probe_call_cap defers to
    JARVIS_HYPOTHESIS_PROBE_MAX_CALLS / MAX_CALLS_PER_PROBE_DEFAULT
  * Ordering pins: exhaustion before triggers; converged exit;
    cost-gate before everything

Authority pins (preview of Slice 5):
  * stdlib + cost_contract_assertion + hypothesis_probe ONLY
  * NO orchestrator / tool_executor / providers / etc.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag (asymmetric env semantics, default-FALSE Slice 1)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        """Slice 1 default is FALSE; Slice 5 graduation flips to
        TRUE. Mirrors Upgrade 3 + M11 pre-graduation pattern."""
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_budget_enabled,
        )
        assert epistemic_budget_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy_variants_flip_on(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", v,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_budget_enabled,
        )
        assert epistemic_budget_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off"],
    )
    def test_falsy_variants(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", v,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_budget_enabled,
        )
        assert epistemic_budget_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — Env-knob clamps (every Upgrade 1 env knob from PRD §31.2.2)
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_max_rounds_default_is_twelve(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_MAX_ROUNDS", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_max_rounds,
        )
        assert epistemic_max_rounds() == 12

    def test_max_rounds_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_EPISTEMIC_MAX_ROUNDS", "0")
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_max_rounds,
        )
        assert epistemic_max_rounds() == 1

    def test_drop_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD",
            raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_confidence_drop_threshold,
        )
        assert epistemic_confidence_drop_threshold() == 0.25

    def test_drop_threshold_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD", "2.0",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_confidence_drop_threshold,
        )
        # Ceiling at 1.0 (drop threshold is bounded to [0, 1])
        assert epistemic_confidence_drop_threshold() == 1.0

    def test_sbt_branch_cap_default_is_three(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_SBT_BRANCH_CAP", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_sbt_branch_cap,
        )
        assert epistemic_sbt_branch_cap() == 3

    def test_tracker_ttl_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_TRACKER_TTL_S", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_tracker_ttl_s,
        )
        assert epistemic_tracker_ttl_s() == 3600


# ---------------------------------------------------------------------------
# § 3 — No-duplication contract: probe cap defers to HypothesisProbe
# ---------------------------------------------------------------------------


class TestProbeCapNoDuplication:
    """Load-bearing: Upgrade 1's probe_call_cap MUST defer to
    HypothesisProbe's existing cap. NEVER duplicate the constant
    or define a parallel env reader. Slice 5 AST pin enforces."""

    def test_probe_cap_default_matches_hypothesis_probe(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", raising=False,
        )
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (  # noqa: E501
            MAX_CALLS_PER_PROBE_DEFAULT,
            get_max_calls_per_probe,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        # Construct via default factory; cap MUST equal hypothesis_probe's
        budget = EpistemicBudget(
            op_id="op-x", route="standard", risk_tier="safe_auto",
        )
        assert budget.probe_call_cap == MAX_CALLS_PER_PROBE_DEFAULT
        assert (
            budget.probe_call_cap == get_max_calls_per_probe()
        )

    def test_probe_cap_honors_hypothesis_probe_env(
        self, monkeypatch,
    ):
        """Setting JARVIS_HYPOTHESIS_PROBE_MAX_CALLS changes
        EpistemicBudget's probe_call_cap default — proves the
        defer-not-duplicate contract."""
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_MAX_CALLS", "8",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        budget = EpistemicBudget(
            op_id="op-x", route="standard", risk_tier="safe_auto",
        )
        assert budget.probe_call_cap == 8


# ---------------------------------------------------------------------------
# § 4 — ConfidenceSample + ConfidenceTrajectory shape
# ---------------------------------------------------------------------------


class TestConfidenceSample:
    def test_frozen(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            ConfidenceSample,
        )
        s = ConfidenceSample(
            confidence=0.8, at_round_index=3, at_unix=1700000000.0,
        )
        with pytest.raises(FrozenInstanceError):
            s.confidence = 0.5  # type: ignore[misc]

    def test_three_required_fields(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            ConfidenceSample,
        )
        s = ConfidenceSample(
            confidence=0.8, at_round_index=3, at_unix=1700000000.0,
        )
        assert s.confidence == 0.8
        assert s.at_round_index == 3
        assert s.at_unix == 1700000000.0


class TestConfidenceTrajectory:
    def test_frozen(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            ConfidenceTrajectory,
        )
        t = ConfidenceTrajectory()
        with pytest.raises(FrozenInstanceError):
            t.latest = 0.5  # type: ignore[misc]

    def test_empty_factory(self):
        """Cold-boot factory returns the canonical zero-state.
        Slice 2's tracker calls this before observing any
        ConfidenceMonitor reading."""
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            ConfidenceTrajectory,
        )
        t = ConfidenceTrajectory.empty()
        assert t.samples == ()
        assert t.latest == 0.0
        assert t.peak == 0.0
        assert t.nadir == 0.0
        assert t.dropped_in_window is False

    def test_required_field_set_includes_dropped_in_window(self):
        """Slice 3's compute_budget_action reads dropped_in_window
        for PROBE_TRIGGERED routing — the field MUST exist on the
        trajectory shape so Slice 2's tracker can populate it."""
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            ConfidenceTrajectory,
        )
        t = ConfidenceTrajectory(
            samples=(),
            latest=0.5,
            peak=0.9,
            nadir=0.4,
            dropped_in_window=True,
        )
        assert t.dropped_in_window is True


# ---------------------------------------------------------------------------
# § 5 — EpistemicBudget 14-field shape (PRD §31.2 contract)
# ---------------------------------------------------------------------------


class TestEpistemicBudgetShape:
    def test_frozen(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        b = EpistemicBudget(
            op_id="op-1", route="standard", risk_tier="safe_auto",
        )
        with pytest.raises(FrozenInstanceError):
            b.rounds_consumed = 5  # type: ignore[misc]

    def test_all_pdr_fields_present(self):
        """Pin EVERY field PRD §31.2 mentions. Tracker (Slice 2)
        + tool_executor (Slice 3) implement against this contract;
        any missing field would force a breaking redesign."""
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        # Required fields per PRD §31.2.2 + scope decisions A1/B1/C1
        required_fields = (
            "op_id", "route", "risk_tier",
            "rounds_consumed", "max_rounds",
            "confidence_trajectory",
            "probe_calls_consumed", "probe_call_cap",
            "branch_calls_consumed", "sbt_branch_cap",
            "confidence_drop_threshold",
            "last_probe_verdict", "last_sbt_verdict",
            "created_at_unix", "last_updated_at_unix",
            "schema_version",
        )
        b = EpistemicBudget(
            op_id="op-1", route="standard", risk_tier="safe_auto",
        )
        for f in required_fields:
            assert hasattr(b, f), (
                f"EpistemicBudget missing required field {f!r}"
            )

    def test_default_values_match_env_knobs(self, monkeypatch):
        """Default factories MUST read the env knobs. Captured at
        op-start so env changes mid-op don't shift the cap."""
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_MAX_ROUNDS", raising=False,
        )
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD",
            raising=False,
        )
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_SBT_BRANCH_CAP", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        b = EpistemicBudget(
            op_id="op-1", route="standard", risk_tier="safe_auto",
        )
        assert b.max_rounds == 12
        assert b.confidence_drop_threshold == 0.25
        assert b.sbt_branch_cap == 3
        assert b.rounds_consumed == 0
        assert b.probe_calls_consumed == 0
        assert b.branch_calls_consumed == 0
        assert b.last_probe_verdict is None
        assert b.last_sbt_verdict is None

    def test_is_route_cost_gated(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        bg = EpistemicBudget(
            op_id="x", route="background", risk_tier="safe_auto",
        )
        spec = EpistemicBudget(
            op_id="x", route="speculative", risk_tier="safe_auto",
        )
        std = EpistemicBudget(
            op_id="x", route="standard", risk_tier="safe_auto",
        )
        assert bg.is_route_cost_gated() is True
        assert spec.is_route_cost_gated() is True
        assert std.is_route_cost_gated() is False

    def test_has_probe_budget_helper(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        empty = EpistemicBudget(
            op_id="x", route="standard", risk_tier="safe_auto",
        )
        assert empty.has_probe_budget() is True
        full = EpistemicBudget(
            op_id="x", route="standard", risk_tier="safe_auto",
            probe_calls_consumed=empty.probe_call_cap,
        )
        assert full.has_probe_budget() is False

    def test_is_at_or_above_notify_apply(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudget,
        )
        for tier in (
            "notify_apply", "approval_required", "blocked",
        ):
            b = EpistemicBudget(
                op_id="x", route="standard", risk_tier=tier,
            )
            assert b.is_at_or_above_notify_apply() is True
        for tier in ("safe_auto", "", "unknown"):
            b = EpistemicBudget(
                op_id="x", route="standard", risk_tier=tier,
            )
            assert b.is_at_or_above_notify_apply() is False


# ---------------------------------------------------------------------------
# § 6 — BudgetOutcome closed enum (7-value contract)
# ---------------------------------------------------------------------------


class TestBudgetOutcome:
    def test_seven_values_per_prd(self):
        """PRD §31.2.2 mandates exactly these 7 values. Slice 3
        branches on the enum; adding/removing requires PRD update."""
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        for name in (
            "WITHIN_BUDGET",
            "CONVERGED",
            "PROBE_TRIGGERED",
            "SBT_TRIGGERED",
            "EXHAUSTED_NOTIFY_APPLY",
            "EXHAUSTED_APPROVAL_REQUIRED",
            "DISABLED",
        ):
            assert hasattr(BudgetOutcome, name), (
                f"BudgetOutcome missing required PRD value {name}"
            )
        assert len(BudgetOutcome) == 7

    def test_values_lowercase_canonical(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        for member in BudgetOutcome:
            assert member.value == member.value.lower()


# ---------------------------------------------------------------------------
# § 7 — BudgetAction result shape
# ---------------------------------------------------------------------------


class TestBudgetAction:
    def test_to_dict_serializable(self):
        """Slice 4 observability + SSE depend on JSON-friendly
        projection."""
        import json
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetAction,
            BudgetOutcome,
        )
        a = BudgetAction(
            outcome=BudgetOutcome.PROBE_TRIGGERED,
            reason="confidence_drop",
            probe_invocation_kw={"k": "v"},
        )
        d = a.to_dict()
        roundtrip = json.loads(json.dumps(d))
        assert roundtrip["outcome"] == "probe_triggered"
        assert roundtrip["probe_invocation_kw"] == {"k": "v"}


# ---------------------------------------------------------------------------
# § 8 — compute_budget_action — every branch + ordering pins
# ---------------------------------------------------------------------------


def _budget(*, route="standard", risk_tier="safe_auto", **kw):
    from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
        EpistemicBudget,
    )
    return EpistemicBudget(
        op_id="op-test", route=route, risk_tier=risk_tier,
        **kw,
    )


def _trajectory(
    *, latest=0.5, peak=0.9, nadir=0.4, dropped=True,
):
    from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
        ConfidenceSample,
        ConfidenceTrajectory,
    )
    return ConfidenceTrajectory(
        samples=(
            ConfidenceSample(
                confidence=peak, at_round_index=1, at_unix=1.0,
            ),
            ConfidenceSample(
                confidence=latest, at_round_index=2, at_unix=2.0,
            ),
        ),
        latest=latest, peak=peak, nadir=nadir,
        dropped_in_window=dropped,
    )


class TestComputeBudgetActionDecisionTree:
    def test_disabled_when_master_off(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget())
        assert result.outcome is BudgetOutcome.DISABLED

    def test_disabled_via_explicit_override(self):
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(
            _budget(), enabled_override=False,
        )
        assert result.outcome is BudgetOutcome.DISABLED

    def test_disabled_on_garbage_input(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(
            "not a budget",  # type: ignore[arg-type]
        )
        assert result.outcome is BudgetOutcome.DISABLED

    def test_within_budget_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget())
        assert result.outcome is BudgetOutcome.WITHIN_BUDGET

    def test_probe_triggered_on_drop(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            confidence_trajectory=_trajectory(),
            rounds_consumed=2,
        ))
        assert result.outcome is BudgetOutcome.PROBE_TRIGGERED
        assert result.probe_invocation_kw == {}

    def test_probe_refused_on_cost_gated_route(
        self, monkeypatch,
    ):
        """**Load-bearing structural pin**: BG / SPEC routes
        cannot return PROBE_TRIGGERED regardless of trajectory."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        for cost_gated_route in ("background", "speculative"):
            result = compute_budget_action(_budget(
                route=cost_gated_route,
                confidence_trajectory=_trajectory(),
                rounds_consumed=2,
            ))
            assert result.outcome is BudgetOutcome.WITHIN_BUDGET, (
                f"Cost-gated route {cost_gated_route} must NOT "
                f"return PROBE_TRIGGERED — got {result.outcome}"
            )

    def test_probe_refused_when_budget_exhausted(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        b = _budget(
            confidence_trajectory=_trajectory(),
            rounds_consumed=2,
            probe_calls_consumed=10,  # exhausted
        )
        result = compute_budget_action(b)
        # No probe budget left → no trigger
        assert result.outcome is BudgetOutcome.WITHIN_BUDGET

    def test_sbt_triggered_on_inconclusive_with_budget(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            risk_tier="notify_apply",
            rounds_consumed=3,
            last_probe_verdict="inconclusive_diminishing",
        ))
        assert result.outcome is BudgetOutcome.SBT_TRIGGERED
        assert result.sbt_invocation_kw == {}

    def test_sbt_refused_below_notify_apply(self, monkeypatch):
        """**Load-bearing structural pin**: SBT only at
        NOTIFY_APPLY+. Cost gate cannot be bypassed via low risk
        tier."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            risk_tier="safe_auto",  # below notify_apply
            rounds_consumed=3,
            last_probe_verdict="inconclusive_diminishing",
        ))
        assert result.outcome is BudgetOutcome.WITHIN_BUDGET

    def test_sbt_refused_on_cost_gated_route(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        for cost_gated_route in ("background", "speculative"):
            result = compute_budget_action(_budget(
                route=cost_gated_route,
                risk_tier="notify_apply",
                rounds_consumed=3,
                last_probe_verdict="inconclusive_diminishing",
            ))
            assert (
                result.outcome is BudgetOutcome.WITHIN_BUDGET
            ), (
                f"Cost-gated route {cost_gated_route} must NOT "
                f"return SBT_TRIGGERED"
            )

    def test_converged_on_confirmed_probe(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            rounds_consumed=3,
            last_probe_verdict="confirmed",
        ))
        assert result.outcome is BudgetOutcome.CONVERGED

    def test_converged_on_refuted_probe(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            rounds_consumed=3,
            last_probe_verdict="refuted",
        ))
        assert result.outcome is BudgetOutcome.CONVERGED

    def test_exhausted_notify_apply_below_tier(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            risk_tier="safe_auto",
            rounds_consumed=12,
            max_rounds=12,
        ))
        assert (
            result.outcome is BudgetOutcome.EXHAUSTED_NOTIFY_APPLY
        )
        assert result.escalation_target_tier == "notify_apply"

    def test_exhausted_approval_required_at_or_above_tier(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            risk_tier="notify_apply",
            rounds_consumed=12,
            max_rounds=12,
        ))
        assert (
            result.outcome
            is BudgetOutcome.EXHAUSTED_APPROVAL_REQUIRED
        )
        assert (
            result.escalation_target_tier == "approval_required"
        )

    def test_exhausted_with_converged_probe_returns_converged(
        self, monkeypatch,
    ):
        """Edge: rounds exhausted exactly when probe converged.
        Treat as CONVERGED (clean exit) rather than phantom
        escalation."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            risk_tier="safe_auto",
            rounds_consumed=12,
            max_rounds=12,
            last_probe_verdict="confirmed",
        ))
        assert result.outcome is BudgetOutcome.CONVERGED


# ---------------------------------------------------------------------------
# § 9 — Decision-tree ordering pins (load-bearing for Slice 3)
# ---------------------------------------------------------------------------


class TestDecisionTreeOrdering:
    """Slice 3's tool_executor branches on outcomes; the ORDER
    of checks in compute_budget_action determines which outcome
    wins when multiple conditions could fire. Ordering pinned
    by these tests."""

    def test_exhaustion_precedes_probe_trigger(
        self, monkeypatch,
    ):
        """Exhausted rounds — even with confidence drop +
        probe budget — must NOT fire PROBE_TRIGGERED. The
        round cap is the outer envelope."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            confidence_trajectory=_trajectory(),
            rounds_consumed=12, max_rounds=12,  # exhausted
        ))
        # Should escalate, NOT probe
        assert result.outcome.value.startswith("exhausted")

    def test_converged_precedes_within_budget(self, monkeypatch):
        """A CONVERGED probe verdict should be reported even when
        no other trigger fires (probe-converged is a stronger
        signal than 'no trigger')."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            rounds_consumed=2,
            last_probe_verdict="confirmed",
        ))
        assert result.outcome is BudgetOutcome.CONVERGED

    def test_probe_trigger_precedes_sbt_trigger(self, monkeypatch):
        """When BOTH a confidence drop AND a prior inconclusive
        probe verdict are present, the NEW drop signal takes
        precedence (we can't run SBT before fresh probe data)."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            compute_budget_action,
        )
        result = compute_budget_action(_budget(
            risk_tier="notify_apply",
            confidence_trajectory=_trajectory(),
            rounds_consumed=3,
            last_probe_verdict="inconclusive_diminishing",
        ))
        # Probe wins — fresh signal beats stale verdict
        assert result.outcome is BudgetOutcome.PROBE_TRIGGERED


# ---------------------------------------------------------------------------
# § 10 — Authority floor (Slice 5 will pin this AST-style)
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    """Slice 1 narrowest floor: stdlib +
    cost_contract_assertion + hypothesis_probe ONLY. Slice 5
    will pin this AST-style; this test pins it bytes-style as
    early-warning."""

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
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.strategic_direction",
    )

    def test_imports_narrow_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "epistemic_budget.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"epistemic_budget.py (Slice 1) must NOT import "
                f"{forbidden} — narrowest authority floor"
            )

    def test_no_duplication_of_probe_max_calls(self):
        """**Load-bearing pin**: Upgrade 1 must NOT define a
        parallel constant for the probe-call cap. The source
        must reference the canonical hypothesis_probe symbols."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "epistemic_budget.py"
        )
        source = path.read_text(encoding="utf-8")
        # Must reference the hypothesis_probe canonical names
        assert "MAX_CALLS_PER_PROBE_DEFAULT" in source
        assert "get_max_calls_per_probe" in source
        # Must NOT define a parallel constant
        # (e.g., _PROBE_CALL_CAP_DEFAULT = 5)
        for bad_name in (
            "_EPISTEMIC_PROBE_CAP_DEFAULT",
            "EPISTEMIC_PROBE_CAP_DEFAULT",
            "_PROBE_CALL_CAP_DEFAULT",
            "JARVIS_EPISTEMIC_PROBE_CALL_CAP",  # would shadow
        ):
            assert bad_name not in source, (
                f"epistemic_budget.py defined parallel "
                f"probe-cap symbol {bad_name!r} — must defer "
                f"to hypothesis_probe instead"
            )

    def test_uses_canonical_cost_gated_routes_symbol(self):
        """Cost-gate refusal must use the COST_GATED_ROUTES
        symbol from cost_contract_assertion, NOT scattered
        string checks."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "epistemic_budget.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "COST_GATED_ROUTES" in source
        # Must import from cost_contract_assertion
        assert (
            "from backend.core.ouroboros.governance"
            ".cost_contract_assertion"
        ) in source


# ---------------------------------------------------------------------------
# § 11 — __all__ exports (locked Slice 1 surface)
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_slices_1_and_2_public_names(self):
        """Slice 1 contract (12) + 5 cost/probe re-exports +
        Slice 2 tracker (3) = 20 public exports."""
        from backend.core.ouroboros.governance import epistemic_budget  # noqa: E501
        expected = sorted([
            # Cost-gate symbols (re-exported for caller
            # convenience)
            "BG_ROUTE",
            "COST_GATED_ROUTES",
            "SPEC_ROUTE",
            # No-duplication probe-cap symbols
            "MAX_CALLS_PER_PROBE_DEFAULT",
            "get_max_calls_per_probe",
            # Slice 1 contract layer
            "BudgetAction",
            "BudgetOutcome",
            "ConfidenceSample",
            "ConfidenceTrajectory",
            "EPISTEMIC_BUDGET_SCHEMA_VERSION",
            "EpistemicBudget",
            "compute_budget_action",
            "epistemic_budget_enabled",
            "epistemic_confidence_drop_threshold",
            "epistemic_max_rounds",
            "epistemic_sbt_branch_cap",
            "epistemic_tracker_ttl_s",
            # Slice 2 tracker
            "EpistemicBudgetTracker",
            "get_default_tracker",
            "reset_default_tracker_for_tests",
        ])
        assert sorted(epistemic_budget.__all__) == expected
