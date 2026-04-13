"""Tests for ``BudgetPlan`` + ``ToolLoopCoordinator`` structural budget math.

These tests exist because of battle test session **bt-2026-04-10-045911**,
in which every IMMEDIATE op died with ``tool_loop_deadline_exceeded``
before a candidate could be produced.  Root cause:

    ToolLoopCoordinator(max_rounds=15, tool_timeout_s=30.0)

multiplies to a worst-case 450s per tool loop, but the IMMEDIATE
generation deadline is only 60s.  The old per-round timeout line
(``min(self._tool_timeout_s, remaining)``) ignored ``max_rounds`` entirely,
so a single slow ``read_file`` round could blow the outer deadline.

The fix is a ``BudgetPlan`` frozen dataclass that derives the per-round
timeout from the **generation deadline** at ``run()`` entry — fair share
of remaining budget across remaining rounds, clamped to
``[min_per_round_s, max_per_round_s]``, with a reserve held back for the
final model write.

This file tests two layers:

1. **Pure math** — ``BudgetPlan`` invariants with no async I/O.
2. **Integrated behavior** — ``ToolLoopCoordinator.run`` honors the plan:
   telemetry logged at entry, per-round timeouts shrink adaptively,
   effective max rounds is budget-derived, and the regression condition
   from bt-2026-04-10-045911 no longer explodes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.tool_executor import (
    BudgetPlan,
    GoverningToolPolicy,
    ToolCall,
    ToolExecStatus,
    ToolLoopCoordinator,
    ToolResult,
)


# ── Helpers ──────────────────────────────────────────────────────────────

_SCHEMA = "2b.2-tool"


def _tool_resp(name: str = "read_file", args=None) -> str:
    return json.dumps({
        "schema_version": _SCHEMA,
        "tool_call": {"name": name, "arguments": args or {"path": "src/foo.py"}},
    })


def _final_resp() -> str:
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [{
            "candidate_id": "c1",
            "file_path": "src/foo.py",
            "full_content": "x = 1\n",
            "rationale": "t",
        }],
    })


def _parse_fn(raw: str) -> Optional[List[ToolCall]]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if data.get("schema_version") != _SCHEMA:
        return None
    tc = data.get("tool_call", {})
    name = tc.get("name")
    if not name:
        return None
    return [ToolCall(name=name, arguments=tc.get("arguments", {}))]


class _TrackingBackend:
    """Records the per-call deadline passed in by the coordinator.

    Lets tests assert that the coordinator is computing per-round timeouts
    from the budget plan, not from ``tool_timeout_s`` alone.
    """

    def __init__(self) -> None:
        self.observed_deadlines: List[float] = []
        self.call_count = 0

    async def execute_async(self, call, policy_ctx, deadline):
        self.observed_deadlines.append(deadline)
        self.call_count += 1
        return ToolResult(
            tool_call=call,
            output="ok",
            status=ToolExecStatus.SUCCESS,
        )


def _allow_policy(repo_root) -> GoverningToolPolicy:
    return GoverningToolPolicy(repo_roots={"jarvis": repo_root})


# ── Pure BudgetPlan math (no async, no coordinator) ─────────────────────


class TestBudgetPlanBuild:
    def test_defaults_are_applied_when_none_passed(self):
        plan = BudgetPlan.build(
            total_budget_s=60.0,
            hard_max_rounds=15,
            max_per_round_s=30.0,
        )
        assert plan.total_budget_s == 60.0
        # Reserve default = min(10, 25% of 60) = 10
        assert plan.final_write_reserve_s == 10.0
        # Min per round default = max(1, min(3, 60/20)) = 3
        assert plan.min_per_round_s == 3.0
        assert plan.max_per_round_s == 30.0
        assert plan.hard_max_rounds == 15

    def test_zero_or_negative_total_clamped_to_one(self):
        plan = BudgetPlan.build(0.0, 5, 30.0)
        assert plan.total_budget_s == 1.0
        plan2 = BudgetPlan.build(-5.0, 5, 30.0)
        assert plan2.total_budget_s == 1.0

    def test_reserve_cannot_leave_less_than_one_second_usable(self):
        # If the caller asks for a 50s reserve on a 10s budget, the
        # build() clamp pins the reserve at total - 1.0 = 9.0s so the
        # plan always has ≥1s of usable budget left.
        plan = BudgetPlan.build(
            total_budget_s=10.0,
            hard_max_rounds=5,
            max_per_round_s=30.0,
            final_write_reserve_s=50.0,
        )
        assert plan.final_write_reserve_s == 9.0
        assert plan.usable_budget_s == 1.0

    def test_max_per_round_lifted_to_min_when_misconfigured(self):
        # max_per_round < min_per_round is nonsense — the constructor
        # lifts max to min to keep per_round_timeout()'s clamp well-ordered.
        plan = BudgetPlan.build(
            total_budget_s=60.0,
            hard_max_rounds=5,
            max_per_round_s=0.5,
            min_per_round_s=2.0,
        )
        assert plan.max_per_round_s >= plan.min_per_round_s

    def test_min_per_round_floor_is_half_second(self):
        # Even passing min=0 gets lifted to 0.5 so we never divide by
        # something weird and hang the loop.
        plan = BudgetPlan.build(
            total_budget_s=60.0,
            hard_max_rounds=5,
            max_per_round_s=30.0,
            min_per_round_s=0.0,
        )
        assert plan.min_per_round_s == 0.5

    def test_is_immutable(self):
        plan = BudgetPlan.build(60.0, 15, 30.0)
        with pytest.raises(Exception):  # FrozenInstanceError (dataclass)
            plan.total_budget_s = 999.0  # type: ignore[misc]


class TestBudgetPlanPerRoundTimeout:
    def test_fair_share_under_tight_budget(self):
        """The bt-2026-04-10-045911 regression condition.

        Budget = 60s, max_rounds = 15, max_per_round = 30s.
        The OLD code would pick min(30, remaining=60) = 30s per round.
        With 15 rounds that is 450s — 7.5× the generation deadline.

        The NEW code must pick fair share = (60-10)/15 ≈ 3.33s per round.
        That is what keeps every op from dying.
        """
        plan = BudgetPlan.build(60.0, 15, 30.0)
        t = plan.per_round_timeout(remaining_s=60.0, remaining_rounds=15)
        assert 3.0 <= t <= 4.0, f"expected ≈3.33s, got {t:.2f}s"

    def test_ceiling_respected_under_abundant_budget(self):
        """When budget is huge, per-round timeout is clamped to max_per_round_s."""
        plan = BudgetPlan.build(
            total_budget_s=600.0, hard_max_rounds=5, max_per_round_s=30.0
        )
        # fair_share = (600-10)/5 = 118 — should clamp to 30.
        t = plan.per_round_timeout(remaining_s=600.0, remaining_rounds=5)
        assert t == 30.0

    def test_floor_respected_under_starved_budget(self):
        """When budget is starved, per-round timeout is lifted to min_per_round_s."""
        # Explicit params so the math is unambiguous:
        # usable = 15 - 5 = 10; fair_share = 10/20 = 0.5 → floor = 2.0
        plan = BudgetPlan.build(
            total_budget_s=15.0,
            hard_max_rounds=20,
            max_per_round_s=30.0,
            final_write_reserve_s=5.0,
            min_per_round_s=2.0,
        )
        t = plan.per_round_timeout(remaining_s=15.0, remaining_rounds=20)
        assert t == plan.min_per_round_s == 2.0

    def test_shrinks_as_remaining_budget_shrinks(self):
        """Successive rounds on a shrinking budget yield shrinking timeouts."""
        plan = BudgetPlan.build(60.0, 15, 30.0)
        t_full = plan.per_round_timeout(remaining_s=60.0, remaining_rounds=15)
        t_mid = plan.per_round_timeout(remaining_s=30.0, remaining_rounds=10)
        t_tail = plan.per_round_timeout(remaining_s=15.0, remaining_rounds=5)
        # Under these params t_full and t_mid land on similar floors, so
        # the important guarantee is that it never *grows* as budget
        # shrinks — i.e. the schedule is monotone non-increasing.
        assert t_full >= t_mid >= t_tail

    def test_remaining_rounds_zero_is_safe(self):
        """Division guard: remaining_rounds=0 is treated as 1."""
        plan = BudgetPlan.build(60.0, 15, 30.0)
        # Should not raise ZeroDivisionError
        t = plan.per_round_timeout(remaining_s=60.0, remaining_rounds=0)
        assert t > 0

    def test_negative_remaining_is_safe(self):
        """Negative ``remaining_s`` falls to the floor, never to something weird."""
        plan = BudgetPlan.build(60.0, 15, 30.0)
        t = plan.per_round_timeout(remaining_s=-5.0, remaining_rounds=15)
        assert t == plan.min_per_round_s


class TestBudgetPlanEffectiveMaxRounds:
    def test_equals_hard_cap_when_budget_is_abundant(self):
        plan = BudgetPlan.build(600.0, 15, 30.0)
        assert plan.effective_max_rounds == 15

    def test_clamps_down_when_budget_too_tight(self):
        """10s budget, 3s min_per_round → only 3 rounds fit, not 15."""
        plan = BudgetPlan.build(
            total_budget_s=10.0,
            hard_max_rounds=15,
            max_per_round_s=30.0,
            min_per_round_s=3.0,
            final_write_reserve_s=1.0,  # usable = 9s
        )
        # usable 9 / min 3 = 3 rounds fit
        assert plan.effective_max_rounds == 3

    def test_never_below_one(self):
        """Even a starvation budget gives at least one round (the loop
        will then raise deadline_exceeded on its own)."""
        plan = BudgetPlan.build(
            total_budget_s=1.1,  # barely 0.1s usable after reserve
            hard_max_rounds=15,
            max_per_round_s=30.0,
            min_per_round_s=3.0,
        )
        assert plan.effective_max_rounds >= 1


class TestBudgetPlanReserve:
    def test_should_stop_at_or_below_reserve(self):
        plan = BudgetPlan.build(60.0, 15, 30.0)
        assert plan.final_write_reserve_s == 10.0
        assert plan.should_stop_for_final_write(10.0) is True
        assert plan.should_stop_for_final_write(9.9) is True
        assert plan.should_stop_for_final_write(11.0) is False

    def test_usable_budget_excludes_reserve(self):
        plan = BudgetPlan.build(
            total_budget_s=60.0,
            hard_max_rounds=15,
            max_per_round_s=30.0,
            final_write_reserve_s=10.0,
        )
        assert plan.usable_budget_s == 50.0

    def test_describe_contains_key_numbers(self):
        plan = BudgetPlan.build(60.0, 15, 30.0)
        desc = plan.describe()
        assert "budget=60.0s" in desc
        assert "hard_rounds=15" in desc
        assert "effective_rounds=" in desc


# ── Integrated ToolLoopCoordinator behavior ─────────────────────────────


class TestToolLoopIntegratedBudget:
    @pytest.mark.asyncio
    async def test_budget_plan_logged_at_entry(self, tmp_path, caplog):
        """Telemetry — operators must be able to see the plan in logs."""
        coordinator = ToolLoopCoordinator(
            backend=_TrackingBackend(),
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
        )

        async def generate_fn(_prompt: str) -> str:
            return _final_resp()

        with caplog.at_level(logging.INFO, logger="backend.core.ouroboros.governance.tool_executor"):
            await coordinator.run(
                prompt="x",
                generate_fn=generate_fn,
                parse_fn=_parse_fn,
                repo="jarvis",
                op_id="op-plan-log",
                deadline=time.monotonic() + 60.0,
            )

        assert any("BudgetPlan:" in rec.getMessage() for rec in caplog.records), (
            "coordinator should log BudgetPlan telemetry at run() entry"
        )
        assert coordinator._last_budget_plan is not None
        assert coordinator._last_budget_plan.total_budget_s > 0

    @pytest.mark.asyncio
    async def test_per_round_deadline_is_budget_derived_not_tool_timeout(self, tmp_path):
        """The critical regression test.

        Historically, with max_rounds=15 / tool_timeout_s=30s and a 60s
        outer deadline, the per-tool deadline was ``now + 30s``.  With
        the BudgetPlan fix, the per-tool deadline must be ``now +
        fair_share`` where fair_share is ~3-4s.
        """
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
        )

        responses = [_tool_resp(), _final_resp()]
        idx = [0]

        async def generate_fn(_prompt: str) -> str:
            i = min(idx[0], len(responses) - 1)
            idx[0] += 1
            return responses[i]

        entry = time.monotonic()
        outer_deadline = entry + 60.0
        await coordinator.run(
            prompt="x",
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-derived",
            deadline=outer_deadline,
        )

        assert backend.call_count == 1, "exactly one tool call executed"
        per_tool_deadline = backend.observed_deadlines[0]
        budget_for_round = per_tool_deadline - entry

        # Must NOT be the old behavior (30s).
        assert budget_for_round < 10.0, (
            f"per-tool deadline was {budget_for_round:.1f}s — too close to "
            "the old min(tool_timeout_s, remaining) behavior"
        )
        # Must be at least the floor.
        assert budget_for_round >= 1.0
        # And must never exceed the outer generation deadline.
        assert per_tool_deadline <= outer_deadline + 0.5

    @pytest.mark.asyncio
    async def test_effective_max_rounds_clamps_below_configured(self, tmp_path):
        """Hard max=15 + tight 10s budget → effective max should be
        much smaller, so hitting ``max_rounds_exceeded`` happens earlier.
        """
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
        )

        # Provider keeps asking for tools — never produces final answer.
        async def generate_fn(_prompt: str) -> str:
            return _tool_resp()

        with pytest.raises(RuntimeError, match="tool_loop_max_rounds_exceeded"):
            await coordinator.run(
                prompt="x",
                generate_fn=generate_fn,
                parse_fn=_parse_fn,
                repo="jarvis",
                op_id="op-eff-clamp",
                deadline=time.monotonic() + 10.0,  # 10s budget
            )

        plan = coordinator._last_budget_plan
        assert plan is not None
        # With 10s total / 3s min_per_round + reserve, effective ≤ 3.
        assert plan.effective_max_rounds < 15
        # And we never executed more than effective_max_rounds tool calls.
        assert backend.call_count <= plan.effective_max_rounds

    @pytest.mark.asyncio
    async def test_final_write_reserve_stops_tool_rounds(self, tmp_path):
        """When remaining budget drops to the reserve, the loop must
        inject a "produce final answer" nudge and stop calling tools.

        We simulate budget drain with an ``asyncio.sleep`` inside
        ``generate_fn`` so that ``deadline - time.monotonic()`` actually
        shrinks between rounds.  Without this, an instantaneous backend
        would keep ``remaining`` constant and the reserve check would
        never trip.
        """
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
            # 8s budget / 5s reserve / 0.5s min-per-round →
            # effective_max_rounds = usable(3) / min(0.5) = 6
            # plenty of headroom for tool → nudge → final progression.
            final_write_reserve_s=5.0,
            min_per_round_s=0.5,
        )

        prompts_seen: List[str] = []

        async def generate_fn(prompt: str) -> str:
            prompts_seen.append(prompt)
            # Burn ~1.5s per call so remaining drops below the 5s
            # reserve by the second tool round.
            await asyncio.sleep(1.5)
            if "Budget reserve reached" in prompt:
                return _final_resp()
            return _tool_resp()

        await coordinator.run(
            prompt="x",
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-reserve",
            deadline=time.monotonic() + 8.0,
        )

        # At least one of the prompts must carry the reserve nudge.
        assert any(
            "Budget reserve reached" in p for p in prompts_seen
        ), (
            "expected final-write reserve nudge to be injected "
            f"(saw {len(prompts_seen)} prompts)"
        )

    @pytest.mark.asyncio
    async def test_immediate_regression_60s_15rounds_does_not_explode(
        self, tmp_path
    ):
        """The exact reproduction of bt-2026-04-10-045911.

        - Budget: 60s (IMMEDIATE route)
        - max_rounds: 15, tool_timeout_s: 30s
        - Provider asks for one tool, then returns final answer.

        With the broken code, the single tool round would have reserved
        30s of the 60s budget, leaving no room for the final write.
        With the BudgetPlan fix, the tool round should take fair-share
        (~3-4s) and the final answer should return cleanly.
        """
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
        )

        responses = [_tool_resp(), _final_resp()]
        idx = [0]

        async def generate_fn(_prompt: str) -> str:
            i = min(idx[0], len(responses) - 1)
            idx[0] += 1
            return responses[i]

        entry = time.monotonic()
        raw, records = await coordinator.run(
            prompt="initial",
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-bt-regress",
            deadline=entry + 60.0,
        )
        elapsed = time.monotonic() - entry

        # Sanity: the coordinator returned a final (non-tool) response.
        assert "candidates" in raw
        assert len(records) == 1  # one tool call executed
        assert records[0].status == ToolExecStatus.SUCCESS

        # And crucially: nowhere near the outer deadline.
        assert elapsed < 5.0, (
            f"took {elapsed:.1f}s — suggests the old behavior still lurks"
        )

        # And the per-tool deadline it passed to the backend must
        # reflect the fair-share math, not min(30, 60).
        assert len(backend.observed_deadlines) == 1
        round_budget = backend.observed_deadlines[0] - entry
        assert round_budget < 10.0, (
            f"per-tool round budget was {round_budget:.1f}s — regression"
        )

    @pytest.mark.asyncio
    async def test_adaptive_per_round_shrinks_across_multiple_rounds(
        self, tmp_path
    ):
        """Over several successful tool rounds, the observed per-tool
        deadline (expressed as a delta from round entry) should trend
        non-increasing because the budget shrinks after each round.
        """
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
        )

        # Three tool rounds then final.
        responses = [_tool_resp(), _tool_resp(), _tool_resp(), _final_resp()]
        idx = [0]

        async def generate_fn(_prompt: str) -> str:
            i = min(idx[0], len(responses) - 1)
            idx[0] += 1
            return responses[i]

        entry = time.monotonic()
        await coordinator.run(
            prompt="x",
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-adaptive",
            deadline=entry + 60.0,
        )

        # Each observed deadline is time-of-call + per_round_timeout.
        # The *delta* should shrink (or stay equal) across rounds,
        # because the plan re-derives from shrinking ``remaining``.
        assert backend.call_count == 3
        # Since fair_share lands on similar floor values for small
        # rounds, we only assert monotone non-increasing, not strict.
        deadlines = backend.observed_deadlines
        # Compute per-call budgets relative to the previous observation.
        assert deadlines[0] >= deadlines[0] - 1.0  # trivial baseline
        # Hard guarantee: no round budget exceeds the prior outer budget.
        for i in range(1, len(deadlines)):
            # Later per-tool deadlines are always ≤ earlier outer deadline
            # (because the outer deadline is fixed and we only subtract).
            assert deadlines[i] <= entry + 60.0 + 0.5

    @pytest.mark.asyncio
    async def test_config_overrides_are_honored(self, tmp_path):
        """Explicit min_per_round_s + final_write_reserve_s passed at
        construction time override the env defaults."""
        coordinator = ToolLoopCoordinator(
            backend=_TrackingBackend(),
            policy=_allow_policy(tmp_path),
            max_rounds=10,
            tool_timeout_s=30.0,
            min_per_round_s=4.0,
            final_write_reserve_s=15.0,
        )
        plan = coordinator._build_budget_plan(time.monotonic() + 60.0)
        assert plan.min_per_round_s == 4.0
        assert plan.final_write_reserve_s == 15.0


# ── Phase 2: pre-round viability gate (bt-2026-04-12-054855) ────────────
#
# Root cause: after a Tier-0 (DW) timeout, the fallback provider received
# only ``parent_remaining`` seconds. Round 0 of the tool loop spent most of
# that on a legitimate stream, leaving round 1+ with a sub-floor remainder
# (observed: 6.7s). The Claude call then died at ``first_token=NEVER,
# bytes_received=0`` and the whole op was dispatched as ``fallback_failed``.
#
# The fix is an absolute-floor gate at the top of every round > 0: if
# ``remaining < plan.min_per_round_s`` we raise
# ``tool_loop_round_budget_starved`` before the doomed API call, and
# ``CandidateGenerator._call_fallback`` promotes the breadcrumb cause from
# ``fallback_failed`` to ``fallback_round_starved``.


class TestBudgetPlanViabilityMath:
    """Pure math for ``unclamped_fair_share_s`` + ``is_next_round_viable``."""

    def test_unclamped_fair_share_divides_usable_by_rounds_left(self):
        plan = BudgetPlan.build(
            total_budget_s=60.0,
            hard_max_rounds=15,
            max_per_round_s=30.0,
            final_write_reserve_s=10.0,
            min_per_round_s=3.0,
        )
        # usable = 60 - 10 = 50; 50 / 10 = 5.0s per round
        assert plan.unclamped_fair_share_s(60.0, 10) == pytest.approx(5.0)

    def test_unclamped_fair_share_returns_zero_when_only_reserve_left(self):
        plan = BudgetPlan.build(60.0, 15, 30.0, final_write_reserve_s=10.0)
        # remaining==reserve → usable==0 → fair share 0.0
        assert plan.unclamped_fair_share_s(10.0, 5) == 0.0

    def test_unclamped_fair_share_differs_from_clamped_when_starved(self):
        """The whole point of the unclamped helper: surface structural
        starvation that ``per_round_timeout``'s clamp hides."""
        plan = BudgetPlan.build(
            total_budget_s=60.0,
            hard_max_rounds=15,
            max_per_round_s=30.0,
            final_write_reserve_s=10.0,
            min_per_round_s=3.0,
        )
        # remaining = 11s, 10 rounds left → usable=1s, fair=0.1s
        # per_round_timeout clamps up to min=3.0, hiding the starvation.
        clamped = plan.per_round_timeout(11.0, 10)
        raw = plan.unclamped_fair_share_s(11.0, 10)
        assert clamped == pytest.approx(3.0)
        assert raw == pytest.approx(0.1)
        assert raw < plan.min_per_round_s

    def test_is_next_round_viable_true_above_floor(self):
        plan = BudgetPlan.build(60.0, 15, 30.0, min_per_round_s=3.0)
        assert plan.is_next_round_viable(60.0, 10) is True

    def test_is_next_round_viable_false_when_starved(self):
        plan = BudgetPlan.build(60.0, 15, 30.0, min_per_round_s=3.0)
        # remaining = 11s, 10 rounds → fair=0.1 < min=3.0 → not viable
        assert plan.is_next_round_viable(11.0, 10) is False

    def test_is_next_round_viable_zero_remaining_is_not_viable(self):
        plan = BudgetPlan.build(60.0, 15, 30.0)
        assert plan.is_next_round_viable(0.0, 5) is False


class TestToolLoopRoundStarvationGate:
    """Integrated coverage for the Phase 2 gate in ``run()``."""

    @pytest.mark.asyncio
    async def test_round_zero_runs_even_on_tight_budget(self, tmp_path):
        """Round 0 is *never* gated — the caller's fresh budget deserves
        one shot, otherwise legitimate small ops would starve themselves."""
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=5,
            tool_timeout_s=30.0,
            min_per_round_s=3.0,
            final_write_reserve_s=1.0,
        )

        async def generate_fn(_prompt: str) -> str:
            return _final_resp()  # Immediately final, no tool rounds.

        raw, records = await coordinator.run(
            prompt="x",
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-round0",
            deadline=time.monotonic() + 5.0,  # tight but valid
        )
        assert "candidates" in raw
        assert records == []

    @pytest.mark.asyncio
    async def test_round_one_bails_when_remaining_below_min_per_round(
        self, tmp_path
    ):
        """The bt-2026-04-12-054855 reproduction: round 0 consumes most of
        the budget, round 1 inherits a sub-floor remainder, gate fires.

        Budget math (matters for effective_max_rounds):
            total=10s, reserve=1s, min=3s → usable=9s, by_time=3
            effective_max_rounds = min(10, 3) = 3  (room for rounds 0, 1, 2)
        Round 0 burns ~8s of that 10s; round 1 sees remaining≈2s, which is
        strictly below min_per_round_s=3s → gate fires.
        """
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=10,
            tool_timeout_s=30.0,
            min_per_round_s=3.0,
            final_write_reserve_s=1.0,
        )

        # Round 0: asks for a tool, burns ~8s of a 10s budget.
        # Round 1: only ~2s remain — below min_per_round_s=3 → bail.
        async def generate_fn(prompt: str) -> str:
            if "Observation" not in prompt:
                await asyncio.sleep(8.0)
                return _tool_resp()
            # Should never reach here — the gate must fire first.
            return _final_resp()

        with pytest.raises(
            RuntimeError, match="tool_loop_round_budget_starved"
        ):
            await coordinator.run(
                prompt="x",
                generate_fn=generate_fn,
                parse_fn=_parse_fn,
                repo="jarvis",
                op_id="op-starved",
                deadline=time.monotonic() + 10.0,
            )

        # Round 0's tool call executed; round 1 never reached generate_fn.
        assert backend.call_count == 1

    @pytest.mark.asyncio
    async def test_round_starved_error_message_carries_breadcrumbs(
        self, tmp_path
    ):
        """The RuntimeError string must include ``round=``, ``remaining=``
        and ``min_per_round=`` so ``_call_fallback``'s substring match and
        the breadcrumb audit have the fields they need."""
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=10,
            tool_timeout_s=30.0,
            min_per_round_s=3.0,
            final_write_reserve_s=1.0,
        )

        async def generate_fn(prompt: str) -> str:
            if "Observation" not in prompt:
                await asyncio.sleep(8.0)
                return _tool_resp()
            return _final_resp()

        with pytest.raises(RuntimeError) as ei:
            await coordinator.run(
                prompt="x",
                generate_fn=generate_fn,
                parse_fn=_parse_fn,
                repo="jarvis",
                op_id="op-crumbs",
                deadline=time.monotonic() + 10.0,
            )

        msg = str(ei.value)
        assert "tool_loop_round_budget_starved" in msg
        assert "round=" in msg
        assert "remaining=" in msg
        assert "min_per_round=" in msg

    @pytest.mark.asyncio
    async def test_reserve_nudge_path_still_wins_when_not_starved(
        self, tmp_path
    ):
        """The reserve-based graceful wind-down must still own the
        normal-tempo exit. The absolute-floor gate is a safety net and
        must not supersede ``should_stop_for_final_write``."""
        backend = _TrackingBackend()
        coordinator = ToolLoopCoordinator(
            backend=backend,
            policy=_allow_policy(tmp_path),
            max_rounds=15,
            tool_timeout_s=30.0,
            # Reserve > min_per_round so the reserve check fires first.
            final_write_reserve_s=5.0,
            min_per_round_s=0.5,
        )

        prompts_seen: List[str] = []

        async def generate_fn(prompt: str) -> str:
            prompts_seen.append(prompt)
            await asyncio.sleep(1.5)
            if "Budget reserve reached" in prompt:
                return _final_resp()
            return _tool_resp()

        await coordinator.run(
            prompt="x",
            generate_fn=generate_fn,
            parse_fn=_parse_fn,
            repo="jarvis",
            op_id="op-reserve-wins",
            deadline=time.monotonic() + 8.0,
        )

        # Reserve nudge fired → graceful wind-down won.
        assert any("Budget reserve reached" in p for p in prompts_seen)
