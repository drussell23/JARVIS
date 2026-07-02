"""EWMA-Coupled Dynamic Round Budgets -- no hardcoded 30s (bt-iso-1782973775).

The tool loop's BudgetPlan sized `max_per_round_s` from the static
JARVIS_GOVERNED_TOOL_TIMEOUT_S=30 (DW API economics). On a heavy sovereign
node a single streaming round costs 200-400s, so the plan's viability gate /
tool deadlines / fair-share arithmetic were computed against a fantasy round
cost. The coordinator must be dynamically coupled to the node's calibrated
LatencyProfiler: the provider derives `round_budget_hint_s = adaptive_timeout
x safety_factor` per dispatch and threads it into the plan. GPU speeds up ->
the hint (EWMA) shrinks -> budgets shrink automatically. No profiler / DW
path -> hint None -> byte-identical legacy.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import backend.core.ouroboros.governance.providers as prov
from backend.core.ouroboros.governance.tool_executor import ToolLoopCoordinator


class _Backend:
    async def execute_async(self, call, ctx, deadline):  # pragma: no cover
        raise AssertionError("not exercised")


class _Policy:
    def evaluate(self, call, ctx):  # pragma: no cover
        raise AssertionError("not exercised")

    def repo_root_for(self, repo):  # pragma: no cover
        return None


def _coord(**kw):
    return ToolLoopCoordinator(
        backend=_Backend(), policy=_Policy(), max_rounds=10, tool_timeout_s=30.0, **kw,
    )


class TestPlanHint:
    def test_hint_raises_max_per_round(self):
        plan = _coord()._build_budget_plan(
            time.monotonic() + 900.0, round_budget_hint_s=305.0,
        )
        assert plan.max_per_round_s == 305.0

    def test_no_hint_is_legacy(self):
        plan = _coord()._build_budget_plan(time.monotonic() + 900.0)
        assert plan.max_per_round_s == 30.0

    def test_hint_below_static_keeps_static(self):
        """A FAST node (EWMA below the static ceiling) never LOWERS the
        operator's configured ceiling -- the hint only widens for slow HW."""
        plan = _coord()._build_budget_plan(
            time.monotonic() + 900.0, round_budget_hint_s=5.0,
        )
        assert plan.max_per_round_s == 30.0

    def test_hint_failsoft_on_garbage(self):
        plan = _coord()._build_budget_plan(
            time.monotonic() + 900.0, round_budget_hint_s=float("nan"),
        )
        assert plan.max_per_round_s == 30.0


class TestRunThreadsHint:
    async def test_run_threads_hint_into_plan(self, monkeypatch):
        coord = _coord()
        seen = {}
        _orig = coord._build_budget_plan

        def _spy(deadline, op_weight_lines=None, round_budget_hint_s=None):
            seen["hint"] = round_budget_hint_s
            return _orig(deadline, op_weight_lines,
                         round_budget_hint_s=round_budget_hint_s)

        monkeypatch.setattr(coord, "_build_budget_plan", _spy)

        async def gen(prompt):
            return '{"schema_version": "2b.1", "candidates": []}'

        raw, records = await coord.run(
            prompt="p", generate_fn=gen, parse_fn=lambda raw: None,
            repo="jarvis", op_id="op-hint", deadline=time.monotonic() + 60.0,
            round_budget_hint_s=222.0,
        )
        assert seen["hint"] == 222.0
        assert records == []


class TestProviderHintDerivation:
    def test_local_client_profiler(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TOOL_ROUND_BUDGET_SAFETY", "1.5")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 200_000.0)
        client = SimpleNamespace(profiler=prof)
        hint = prov._profiler_round_hint_s(client, "x" * 400)
        assert hint == pytest.approx(300.0)          # 200s x 1.5

    def test_tiered_composite_unwraps_light(self):
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 100_000.0)
        client = SimpleNamespace(_light=SimpleNamespace(profiler=prof))
        hint = prov._profiler_round_hint_s(client, "hello")
        assert hint == pytest.approx(125.0)          # default safety 1.25

    def test_no_profiler_is_none(self):
        assert prov._profiler_round_hint_s(SimpleNamespace(), "p") is None

    def test_profiler_error_failsoft_none(self):
        def _boom(*, prompt_tokens):
            raise RuntimeError("ceiling")
        client = SimpleNamespace(profiler=SimpleNamespace(adaptive_timeout_ms=_boom))
        assert prov._profiler_round_hint_s(client, "p") is None


def test_generate_impl_call_site_threads_hint():
    """Source pin (repo invariant-pin pattern): the tool-loop call site in
    _generate_impl derives + passes round_budget_hint_s so the plan is
    EWMA-coupled wherever a profiler-bearing client is in the seat."""
    import pathlib
    src = (pathlib.Path(prov.__file__)).read_text()
    anchor = src.find("await self._tool_loop.run(")
    assert anchor != -1
    window = src[anchor:anchor + 1200]
    assert "round_budget_hint_s" in window, (
        "tool-loop call site must thread the profiler-derived round hint"
    )
