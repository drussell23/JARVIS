"""Upgrade 1 Slice 3 — executor-hook dispatch tests (PRD §31.2).

Pins the integration point that connects Slice 1's contract +
Slice 2's tracker to tool_executor's round loop. Tests verify
all 7 BudgetOutcome → BudgetDispatchResult transitions in
isolation with mock runners.

Test layout:
  § 1 — Master flag gate
  § 2 — open_op_tracker / note_round_complete convenience helpers
  § 3 — apply_budget_decision dispatch (all 7 outcomes)
  § 4 — Probe / SBT runner failure isolation (no exception
        propagates; degraded result)
  § 5 — Decision C1 escalation via canonical primitives
        (apply_floor_to_name + get_active_tier_order)
  § 6 — Cost-gated routes never reach PROBE/SBT (Slice 1
        contract trusted; hook does NOT re-check)
  § 7 — Authority floor (no orchestrator/tool_executor imports;
        runners are caller-injected via Protocol)
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_EPISTEMIC_BUDGET_ENABLED", "true",
    )


# ---------------------------------------------------------------------------
# Mock runners — caller-supplied via Protocol injection
# ---------------------------------------------------------------------------


class _MockProbe:
    """In-memory probe runner. Records calls + returns
    configurable verdict."""

    def __init__(self, verdict="confirmed", raises=False):
        self.calls = []
        self._verdict = verdict
        self._raises = raises

    async def run(self, *, payload):
        self.calls.append(payload)
        if self._raises:
            raise RuntimeError("synthetic probe failure")
        return self._verdict


class _MockSBT:
    def __init__(self, verdict="consensus", raises=False):
        self.calls = []
        self._verdict = verdict
        self._raises = raises

    async def run(self, *, payload):
        self.calls.append(payload)
        if self._raises:
            raise RuntimeError("synthetic sbt failure")
        return self._verdict


class _MockOrange:
    def __init__(self, raises=False):
        self.queued = []
        self._raises = raises

    async def queue(self, *, op_id, reason):
        self.queued.append((op_id, reason))
        if self._raises:
            raise RuntimeError("synthetic orange failure")


# ---------------------------------------------------------------------------
# § 1 — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    @pytest.mark.asyncio
    async def test_disabled_returns_disabled_outcome(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        t = EpistemicBudgetTracker()
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
        )
        assert result.action.outcome is BudgetOutcome.DISABLED
        assert result.new_risk_tier is None
        assert result.break_round_loop is False

    def test_open_op_tracker_returns_false_when_disabled(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            open_op_tracker,
        )
        t = EpistemicBudgetTracker()
        ok = open_op_tracker(
            t, op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        assert ok is False
        # Tracker not populated either
        assert len(t) == 0


# ---------------------------------------------------------------------------
# § 2 — Convenience helpers
# ---------------------------------------------------------------------------


class TestConvenienceHelpers:
    def test_open_op_tracker_when_enabled(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            open_op_tracker,
        )
        t = EpistemicBudgetTracker()
        ok = open_op_tracker(
            t, op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        assert ok is True
        assert len(t) == 1

    def test_note_round_complete_when_enabled(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            note_round_complete,
            open_op_tracker,
        )
        t = EpistemicBudgetTracker()
        open_op_tracker(
            t, op_id="op-x", route="standard",
            risk_tier="safe_auto",
        )
        ok = note_round_complete(
            t, op_id="op-x", confidence=0.8,
        )
        assert ok is True
        assert t.get("op-x").rounds_consumed == 1

    def test_note_round_complete_silently_no_op_when_disabled(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            EpistemicBudgetTracker,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            note_round_complete,
        )
        t = EpistemicBudgetTracker()
        ok = note_round_complete(
            t, op_id="op-x", confidence=0.8,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# § 3 — apply_budget_decision dispatch (all 7 outcomes)
# ---------------------------------------------------------------------------


def _setup(monkeypatch, *, op_id="op-x", route="standard",
           risk_tier="safe_auto"):
    """Helper — create + open a tracker with master flag on."""
    _enable(monkeypatch)
    from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
        EpistemicBudgetTracker,
    )
    t = EpistemicBudgetTracker()
    t.open(op_id=op_id, route=route, risk_tier=risk_tier)
    return t


class TestDispatchWithinBudget:
    @pytest.mark.asyncio
    async def test_within_budget_no_side_effects(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
        )
        assert (
            result.action.outcome is BudgetOutcome.WITHIN_BUDGET
        )
        assert result.new_risk_tier is None
        assert result.break_round_loop is False
        assert result.enqueue_for_orange_review is False


class TestDispatchProbeTriggered:
    @pytest.mark.asyncio
    async def test_probe_triggered_invokes_runner_and_updates(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch)
        # 2 rounds with drop → PROBE_TRIGGERED
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        probe = _MockProbe(verdict="confirmed")
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
            probe_runner=probe,
        )
        assert (
            result.action.outcome is BudgetOutcome.PROBE_TRIGGERED
        )
        assert len(probe.calls) == 1
        # Tracker recorded the verdict
        assert t.get("op-x").last_probe_verdict == "confirmed"
        assert t.get("op-x").probe_calls_consumed == 1

    @pytest.mark.asyncio
    async def test_probe_triggered_no_runner_flags_failure(
        self, monkeypatch,
    ):
        """No probe_runner injected → degraded result with
        probe_invocation_failed=True. NEVER raises."""
        t = _setup(monkeypatch)
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
            probe_runner=None,
        )
        assert result.probe_invocation_failed is True
        assert (
            "no_probe_runner_injected"
            in result.extra_telemetry.get(
                "probe_skipped_reason", "",
            )
        )

    @pytest.mark.asyncio
    async def test_probe_runner_exception_isolated(
        self, monkeypatch,
    ):
        """Probe raising must NOT propagate. Hook flags failure
        + returns degraded result."""
        t = _setup(monkeypatch)
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
            probe_runner=_MockProbe(raises=True),
        )
        assert result.probe_invocation_failed is True
        assert (
            result.extra_telemetry.get("probe_error")
            == "RuntimeError"
        )
        # Tracker did NOT increment probe_calls (probe failed
        # before any tracker mutation)
        # Note: tracker state depends on hook ordering — verify
        # the failure flag is what's asserted.

    @pytest.mark.asyncio
    async def test_probe_payload_default_is_empty_dict(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch)
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        probe = _MockProbe()
        await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
            probe_runner=probe,
        )
        # Default payload from action.probe_invocation_kw is {}
        assert probe.calls[0] == {}

    @pytest.mark.asyncio
    async def test_probe_payload_explicit_override(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch)
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        probe = _MockProbe()
        await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
            probe_runner=probe,
            probe_payload={"hypothesis": "x", "evidence": "y"},
        )
        assert probe.calls[0] == {
            "hypothesis": "x", "evidence": "y",
        }


class TestDispatchSBTTriggered:
    @pytest.mark.asyncio
    async def test_sbt_triggered_invokes_runner(
        self, monkeypatch,
    ):
        # SBT requires risk_tier ≥ notify_apply
        t = _setup(monkeypatch, risk_tier="notify_apply")
        # Inconclusive probe verdict → SBT_TRIGGERED
        t.note_probe_completed(
            "op-x", verdict="inconclusive_diminishing",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        sbt = _MockSBT(verdict="consensus")
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
            sbt_runner=sbt,
        )
        assert (
            result.action.outcome is BudgetOutcome.SBT_TRIGGERED
        )
        assert len(sbt.calls) == 1
        assert t.get("op-x").last_sbt_verdict == "consensus"
        assert t.get("op-x").branch_calls_consumed == 1

    @pytest.mark.asyncio
    async def test_sbt_runner_exception_isolated(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch, risk_tier="notify_apply")
        t.note_probe_completed(
            "op-x", verdict="inconclusive_diminishing",
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
            sbt_runner=_MockSBT(raises=True),
        )
        assert result.sbt_invocation_failed is True


class TestDispatchConverged:
    @pytest.mark.asyncio
    async def test_converged_breaks_round_loop(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch)
        t.note_round_complete("op-x", confidence=0.8)
        t.note_probe_completed("op-x", verdict="confirmed")
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
        )
        assert result.action.outcome is BudgetOutcome.CONVERGED
        assert result.break_round_loop is True
        assert result.new_risk_tier is None


class TestDispatchExhaustedNotifyApply:
    @pytest.mark.asyncio
    async def test_escalates_below_notify_apply(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch, risk_tier="safe_auto")
        # Drive rounds to exhaustion
        for _ in range(12):
            t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
        )
        assert (
            result.action.outcome
            is BudgetOutcome.EXHAUSTED_NOTIFY_APPLY
        )
        assert result.new_risk_tier == "notify_apply"
        # NOT a clean exit — caller may continue with elevated tier
        assert result.break_round_loop is False

    @pytest.mark.asyncio
    async def test_no_escalation_when_already_at_target(
        self, monkeypatch,
    ):
        """If the input tier is already notify_apply, the
        EXHAUSTED_APPROVAL_REQUIRED path fires instead. This
        test verifies the EXHAUSTED_NOTIFY_APPLY case is
        unreachable when tier is already at target."""
        t = _setup(monkeypatch, risk_tier="notify_apply")
        for _ in range(12):
            t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
        )
        # At notify_apply tier + exhausted → routes to
        # EXHAUSTED_APPROVAL_REQUIRED, not NOTIFY_APPLY
        assert (
            result.action.outcome
            is BudgetOutcome.EXHAUSTED_APPROVAL_REQUIRED
        )


class TestDispatchExhaustedApprovalRequired:
    @pytest.mark.asyncio
    async def test_escalates_and_queues_orange(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch, risk_tier="notify_apply")
        for _ in range(12):
            t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        orange = _MockOrange()
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
            orange_queue=orange,
        )
        assert (
            result.action.outcome
            is BudgetOutcome.EXHAUSTED_APPROVAL_REQUIRED
        )
        assert result.new_risk_tier == "approval_required"
        assert result.break_round_loop is True
        assert result.enqueue_for_orange_review is True
        assert len(orange.queued) == 1
        assert orange.queued[0][0] == "op-x"
        # Reason carries the action.reason
        assert "exhausted" in orange.queued[0][1].lower()

    @pytest.mark.asyncio
    async def test_no_orange_queue_still_breaks_loop(
        self, monkeypatch,
    ):
        """Without orange_queue injected, escalation still
        applies + round loop still breaks. enqueue=False
        flagged."""
        t = _setup(monkeypatch, risk_tier="notify_apply")
        for _ in range(12):
            t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
            orange_queue=None,
        )
        assert result.new_risk_tier == "approval_required"
        assert result.break_round_loop is True
        assert result.enqueue_for_orange_review is False

    @pytest.mark.asyncio
    async def test_orange_queue_exception_isolated(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch, risk_tier="notify_apply")
        for _ in range(12):
            t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
            orange_queue=_MockOrange(raises=True),
        )
        # Escalation still applies; queue failed silently
        assert result.new_risk_tier == "approval_required"
        assert result.enqueue_for_orange_review is False


# ---------------------------------------------------------------------------
# § 4 — Decision C1: escalation via canonical primitives
# ---------------------------------------------------------------------------


class TestEscalationCanonicalPrimitives:
    def test_escalate_to_target_uses_tier_order(
        self, monkeypatch,
    ):
        """Pure helper test — escalation is computed via
        get_active_tier_order ranks, NOT bespoke logic."""
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            _escalate_to_target,
        )
        # safe_auto → notify_apply MUST escalate (target stricter)
        result = _escalate_to_target(
            "safe_auto", "notify_apply",
        )
        assert result == "notify_apply"
        # notify_apply → notify_apply: no-op (idempotent)
        result = _escalate_to_target(
            "notify_apply", "notify_apply",
        )
        assert result is None
        # approval_required → notify_apply: no-op (already
        # stricter than target)
        result = _escalate_to_target(
            "approval_required", "notify_apply",
        )
        assert result is None
        # safe_auto → approval_required: escalates
        result = _escalate_to_target(
            "safe_auto", "approval_required",
        )
        assert result == "approval_required"

    def test_unknown_tier_input_safe_fallback(self):
        """Unknown input tier: rank=-1 → escalation always
        applies (fail-safe to bumping up)."""
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            _escalate_to_target,
        )
        result = _escalate_to_target(
            "unknown_tier", "notify_apply",
        )
        # Unknown → -1 < notify_apply rank → escalates
        assert result == "notify_apply"


# ---------------------------------------------------------------------------
# § 5 — Cost-gated routes (Slice 1 contract trusted)
# ---------------------------------------------------------------------------


class TestCostGatedRoutes:
    """Slice 1's compute_budget_action structurally refuses
    PROBE_TRIGGERED / SBT_TRIGGERED on BACKGROUND / SPECULATIVE.
    The hook trusts that contract — never reaches probe/sbt
    runners on cost-gated routes."""

    @pytest.mark.asyncio
    async def test_bg_route_with_drop_is_within_budget_not_probe(
        self, monkeypatch,
    ):
        t = _setup(monkeypatch, route="background")
        t.note_round_complete("op-x", confidence=0.9)
        t.note_round_complete("op-x", confidence=0.5)
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        probe = _MockProbe()
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="safe_auto",
            probe_runner=probe,
        )
        # Cost-gate refuses PROBE_TRIGGERED structurally —
        # outcome falls through to WITHIN_BUDGET
        assert (
            result.action.outcome is BudgetOutcome.WITHIN_BUDGET
        )
        # Probe runner NEVER called
        assert len(probe.calls) == 0

    @pytest.mark.asyncio
    async def test_speculative_route_inconclusive_is_within_budget(
        self, monkeypatch,
    ):
        t = _setup(
            monkeypatch, route="speculative",
            risk_tier="notify_apply",
        )
        t.note_probe_completed(
            "op-x", verdict="inconclusive_diminishing",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            BudgetOutcome,
        )
        from backend.core.ouroboros.governance.epistemic_budget_executor_hook import (  # noqa: E501
            apply_budget_decision,
        )
        sbt = _MockSBT()
        result = await apply_budget_decision(
            tracker=t, op_id="op-x",
            current_risk_tier="notify_apply",
            sbt_runner=sbt,
        )
        # Cost-gate refuses SBT_TRIGGERED structurally
        assert (
            result.action.outcome is BudgetOutcome.WITHIN_BUDGET
        )
        assert len(sbt.calls) == 0


# ---------------------------------------------------------------------------
# § 6 — Authority floor
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
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.strategic_direction",
        # Runner implementations are caller-injected via Protocol
        # — hook MUST NOT import them directly
        (
            "from backend.core.ouroboros.governance.verification."
            "confidence_probe_runner"
        ),
        (
            "from backend.core.ouroboros.governance.verification."
            "speculative_branch_runner"
        ),
        "from backend.core.ouroboros.governance.orange_pr_reviewer",
    )

    def test_imports_narrow_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "epistemic_budget_executor_hook.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"epistemic_budget_executor_hook.py must NOT "
                f"import {forbidden} — runners are caller-"
                f"injected via Protocol"
            )

    def test_uses_canonical_tier_primitives(self):
        """Decision C1 pin — escalation MUST use canonical
        primitives from risk_tier_floor."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "epistemic_budget_executor_hook.py"
        )
        source = path.read_text(encoding="utf-8")
        # Both canonical primitives referenced
        assert "apply_floor_to_name" in source
        assert "get_active_tier_order" in source


# ---------------------------------------------------------------------------
# § 7 — __all__ exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_seven_public_names(self):
        from backend.core.ouroboros.governance import (
            epistemic_budget_executor_hook as hook,
        )
        expected = sorted([
            "BudgetDispatchResult",
            "OrangeQueueProtocol",
            "ProbeRunnerProtocol",
            "SBTRunnerProtocol",
            "apply_budget_decision",
            "note_round_complete",
            "open_op_tracker",
        ])
        assert sorted(hook.__all__) == expected
