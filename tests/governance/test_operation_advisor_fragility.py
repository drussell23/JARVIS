"""Tests for the Proactive Advisory Plane extensions to OperationAdvisor.

Covers the three additions (git-volatility axis, memory-headroom axis, Phase-3 safety plan):
1. compute_git_volatility — churn hot-spot scoring + normalization + fail-soft.
2. memory_headroom_factor — MemoryPressureGate level → risk factor + fail-soft.
3. build_safety_plan — prerequisite de-risk steps for CAUTION/BLOCK.
4. Advisory.render_safety_plan — prompt clause.
5. axis flag gates (default-ON + kill-switch).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.operation_advisor import (
    Advisory,
    AdvisoryDecision,
    _advisor_git_volatility_enabled,
    _advisor_memory_axis_enabled,
    _advisor_safety_plan_enabled,
    build_safety_plan,
    compute_git_volatility,
    memory_headroom_factor,
)


# --------------------------------------------------------------------------- git volatility
class TestGitVolatility:
    def test_hotspot_scores_high(self) -> None:
        score, hot = compute_git_volatility(
            ("a.py", "b.py"), ".", runner=lambda f: 40 if f == "a.py" else 1)
        assert score == 1.0          # 40 commits >> hotspot threshold (20) → clamped 1.0
        assert "a.py(40)" in hot

    def test_low_churn_low_score(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_ADVISOR_CHURN_HOTSPOT_COMMITS", "20")
        score, hot = compute_git_volatility(("a.py",), ".", runner=lambda f: 5)
        assert 0.0 < score < 0.5 and hot == []

    def test_no_churn_zero(self) -> None:
        score, hot = compute_git_volatility(("a.py",), ".", runner=lambda f: 0)
        assert score == 0.0 and hot == []

    def test_runner_failure_failsoft(self) -> None:
        def _boom(f):
            raise RuntimeError("git missing")
        score, hot = compute_git_volatility(("a.py",), ".", runner=_boom)
        assert score == 0.0 and hot == []


# --------------------------------------------------------------------------- memory axis
class TestMemoryHeadroom:
    @pytest.mark.parametrize("level,factor", [("ok", 0.0), ("warn", 0.3), ("high", 0.6), ("critical", 0.9)])
    def test_levels(self, level: str, factor: float) -> None:
        class _G:
            def pressure(self):  # noqa: ANN201
                return type("L", (), {"value": level})()
        f, lv = memory_headroom_factor(gate=_G())
        assert f == factor and lv == level

    def test_failsoft(self) -> None:
        class _Boom:
            def pressure(self):  # noqa: ANN201
                raise RuntimeError("no gate")
        assert memory_headroom_factor(gate=_Boom()) == (0.0, "ok")


# --------------------------------------------------------------------------- safety plan
class TestSafetyPlan:
    def test_zero_coverage_adds_characterization_test(self) -> None:
        plan = build_safety_plan(AdvisoryDecision.CAUTION, 5, 0.0, ("x.py",), [])
        assert any("characterization test" in s for s in plan)

    def test_high_blast_adds_forensic_branch(self) -> None:
        plan = build_safety_plan(AdvisoryDecision.CAUTION, 25, 0.8, ("x.py",), [])
        assert any("forensic branch" in s for s in plan)

    def test_hotspot_adds_suite_step(self) -> None:
        plan = build_safety_plan(AdvisoryDecision.CAUTION, 3, 0.8, ("x.py",), ["x.py(40)"])
        assert any("hot-spot" in s for s in plan)

    def test_block_always_has_a_step(self) -> None:
        plan = build_safety_plan(AdvisoryDecision.BLOCK, 1, 1.0, ("x.py",), [])
        assert len(plan) >= 1

    def test_render_clause(self) -> None:
        adv = Advisory(decision=AdvisoryDecision.BLOCK, reasons=[], blast_radius=25,
                       test_coverage=0.0, chronic_entropy=0.0, risk_score=0.9,
                       safety_plan=["step one", "step two"])
        out = adv.render_safety_plan()
        assert "PREREQUISITE SAFETY PLAN" in out and "step one" in out and "step two" in out

    def test_render_empty_when_no_plan(self) -> None:
        adv = Advisory(decision=AdvisoryDecision.RECOMMEND, reasons=[], blast_radius=0,
                       test_coverage=1.0, chronic_entropy=0.0, risk_score=0.0)
        assert adv.render_safety_plan() == ""


# --------------------------------------------------------------------------- flag gates
class TestAxisFlags:
    def test_defaults_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("JARVIS_ADVISOR_GIT_VOLATILITY_ENABLED", "JARVIS_ADVISOR_MEMORY_AXIS_ENABLED",
                  "JARVIS_ADVISOR_SAFETY_PLAN_ENABLED"):
            monkeypatch.delenv(v, raising=False)
        assert _advisor_git_volatility_enabled() is True
        assert _advisor_memory_axis_enabled() is True
        assert _advisor_safety_plan_enabled() is True

    def test_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("JARVIS_ADVISOR_GIT_VOLATILITY_ENABLED", "JARVIS_ADVISOR_MEMORY_AXIS_ENABLED",
                  "JARVIS_ADVISOR_SAFETY_PLAN_ENABLED"):
            monkeypatch.setenv(v, "false")
        assert _advisor_git_volatility_enabled() is False
        assert _advisor_memory_axis_enabled() is False
        assert _advisor_safety_plan_enabled() is False
