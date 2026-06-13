"""Slice 237 — op-weight-scaled convergence (the "seventh layer" fix).

Layer-7 (surfaced by the Slice 235/236 soak): heavy multi-file GOAL ops blow the
generation deadline with `tool_loop_deadline_exceeded` BEFORE emitting a patch.
Root cause (investigated, not guessed): the Slice 85 cumulative-convergence axis
(`_should_force_convergence`) uses a STATIC threshold (default 14 read-only
calls). On heavy ops each round burns 25-45s of the deadline (large multi-file
context → high TTFT + generation + tool-exec), so the loop completes only 1-3
rounds and the wall-clock deadline fires before 14 explore calls accrue — the
convergence trigger never reaches its threshold. This is the Slice 233 convergence
problem at scale: the machinery is correct, it just engages too late on heavy ops.

Fix (reuse, don't rebuild): scale the EXISTING cumulative threshold DOWN for heavy
ops so `_should_force_convergence` engages EARLIER — using the op-weight signal the
gate already computes (`_max_target_line_count`), floored so heavy ops still get
enough localization. Light / unknown ops keep the full base threshold
(byte-identical). NOT a deadline bump, NOT a parallel convergence system.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import tool_executor as te


class TestScaleConvergenceThreshold:
    def test_light_op_keeps_base_threshold(self):
        # at or below the heavy boundary → exploration unaffected (byte-identical)
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=400, heavy_lines=800, min_calls=4,
        )
        assert out == 14

    def test_at_boundary_keeps_base(self):
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=800, heavy_lines=800, min_calls=4,
        )
        assert out == 14

    def test_unknown_line_count_keeps_base(self):
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=None, heavy_lines=800, min_calls=4,
        )
        assert out == 14

    def test_heavy_op_scales_down(self):
        # the 3246-line semantic_index.py case: ratio ~4 → ~3 → floored at 4
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=3246, heavy_lines=800, min_calls=4,
        )
        assert out == 4
        assert out < 14  # convergence forced EARLIER than the static threshold

    def test_moderate_op_scales_proportionally(self):
        # 1600 lines = 2x boundary → base/2 = 7
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=1600, heavy_lines=800, min_calls=4,
        )
        assert out == 7

    def test_very_heavy_floored_at_min_calls(self):
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=100000, heavy_lines=800, min_calls=4,
        )
        assert out == 4  # floored — even the heaviest op gets min localization

    def test_never_exceeds_base(self):
        # scaling only ever reduces; a heavy op can't get MORE budget than base
        for n in (900, 1600, 3246, 50000):
            out = te.scale_convergence_threshold(
                base_threshold=14, target_line_count=n, heavy_lines=800, min_calls=4,
            )
            assert out <= 14

    def test_disabled_axis_base_zero_respected(self):
        # base 0 = operator opted out of the cumulative axis → stays 0 (never fires)
        out = te.scale_convergence_threshold(
            base_threshold=0, target_line_count=5000, heavy_lines=800, min_calls=4,
        )
        assert out == 0

    def test_fail_soft_returns_base_on_bad_input(self):
        out = te.scale_convergence_threshold(
            base_threshold=14, target_line_count="oops", heavy_lines=800, min_calls=4,
        )
        assert out == 14

    def test_defaults_pulled_from_env_when_omitted(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TOOL_LOOP_CONVERGENCE_HEAVY_LINES", raising=False)
        monkeypatch.delenv("JARVIS_TOOL_LOOP_CONVERGENCE_MIN_CALLS", raising=False)
        # heavy op with defaults still scales below base
        out = te.scale_convergence_threshold(base_threshold=14, target_line_count=4000)
        assert isinstance(out, int) and 0 < out < 14


class TestConvergenceWeightEnvKnobs:
    def test_heavy_lines_default_and_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TOOL_LOOP_CONVERGENCE_HEAVY_LINES", raising=False)
        d = te._convergence_heavy_lines()
        assert isinstance(d, int) and d > 0
        monkeypatch.setenv("JARVIS_TOOL_LOOP_CONVERGENCE_HEAVY_LINES", "1200")
        assert te._convergence_heavy_lines() == 1200
        monkeypatch.setenv("JARVIS_TOOL_LOOP_CONVERGENCE_HEAVY_LINES", "-5")
        assert te._convergence_heavy_lines() == d  # invalid → default

    def test_min_calls_default_and_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TOOL_LOOP_CONVERGENCE_MIN_CALLS", raising=False)
        d = te._convergence_min_calls()
        assert isinstance(d, int) and d > 0
        monkeypatch.setenv("JARVIS_TOOL_LOOP_CONVERGENCE_MIN_CALLS", "6")
        assert te._convergence_min_calls() == 6


class TestComposedWithExistingTrigger:
    """The scaled threshold makes the EXISTING _should_force_convergence engage
    earlier on heavy ops — the whole point. Reuses the Slice 85 trigger verbatim."""

    def test_heavy_op_converges_at_low_call_count(self):
        thr = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=3246, heavy_lines=800, min_calls=4,
        )
        # a heavy op that has made 4 read-only calls now forces convergence,
        # where the static threshold of 14 would have kept wandering
        assert te._should_force_convergence(cumulative_explore_calls=4, threshold=thr) is True
        assert te._should_force_convergence(cumulative_explore_calls=4, threshold=14) is False

    def test_light_op_keeps_full_exploration(self):
        thr = te.scale_convergence_threshold(
            base_threshold=14, target_line_count=200, heavy_lines=800, min_calls=4,
        )
        # light op: 4 calls must NOT force convergence (full budget preserved)
        assert te._should_force_convergence(cumulative_explore_calls=4, threshold=thr) is False


class TestRunThreadsOpWeight:
    """Wiring pins (mirrors test_slice85 style): the loop scales its threshold by
    op weight, and the provider caller passes the existing op-weight signal."""

    def test_run_accepts_op_weight_lines_param(self):
        sig = inspect.signature(te.ToolLoopCoordinator.run)
        assert "op_weight_lines" in sig.parameters

    def test_run_loop_scales_threshold_by_op_weight(self):
        src = inspect.getsource(te.ToolLoopCoordinator.run)
        assert "scale_convergence_threshold(" in src, "loop must scale the threshold by op weight"
        assert "op_weight_lines" in src
        # still composes the existing cumulative trigger (not a replacement)
        assert "_should_force_convergence(" in src

    def test_provider_passes_op_weight_to_tool_loop(self):
        from backend.core.ouroboros.governance import providers as pv
        src = inspect.getsource(pv)
        assert "op_weight_lines=" in src, "provider must thread op weight into the tool loop"
        # reuses the EXISTING op-weight signal the gate computes (no parallel calc)
        assert "_max_target_line_count(" in src

    def test_doubleword_provider_passes_op_weight(self):
        # The DW provider is THE path heavy GOAL ops take when DW is sovereign —
        # it runs its own tool loop, so it must thread op weight too (the original
        # providers-only pin missed this; the failing ops live here).
        from backend.core.ouroboros.governance import doubleword_provider as dw
        src = inspect.getsource(dw)
        assert "op_weight_lines=" in src, "DW provider must thread op weight into its tool loop"
        assert "_max_target_line_count" in src, "DW must reuse the gate's op-weight signal"

    def test_all_tool_loop_run_sites_thread_op_weight(self):
        # Every _tool_loop.run( call site must pass op_weight_lines — a site that
        # forgets it silently reverts to the static threshold (the bug class that
        # let the DW site slip the first pin).
        from backend.core.ouroboros.governance import providers as pv
        from backend.core.ouroboros.governance import doubleword_provider as dw
        for mod in (pv, dw):
            src = inspect.getsource(mod)
            run_calls = src.count("_tool_loop.run(")
            weight_args = src.count("op_weight_lines=")
            assert weight_args >= run_calls, (
                f"{mod.__name__}: {run_calls} _tool_loop.run( site(s) but only "
                f"{weight_args} op_weight_lines= — a site is unwired"
            )
