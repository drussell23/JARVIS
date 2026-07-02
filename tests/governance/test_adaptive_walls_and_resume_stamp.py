"""Time-Dilated Hydration (resume pipeline re-stamp) + arm-time adaptive walls.

(1) A resumed op must never inherit a spent window-1 pipeline_deadline (the
L2 repair path reads it DIRECTLY, no phase-deadline floor): the intake seam
stamps a FRESH envelope at rebirth, derived from the operator's
JARVIS_PIPELINE_TIMEOUT_S.

(2) The driver's soak-child wall margin was a static +600s. It is now DERIVED
at arm time (Slice-47-safe: computed BEFORE launch, immutable after) from the
profiler's own cold-round physics: rounds x seed x heavy-mult x ctx-factor.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router as uir


class TestResumePipelineRestamp:
    def test_fsm_resume_gets_fresh_pipeline_deadline(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "600")
        stamped = {}

        class _Ctx:
            def with_pipeline_deadline(self, dl):
                stamped["dl"] = dl
                return self

        out = uir._stamp_resume_pipeline_deadline(_Ctx(), "fsm_resume")
        assert out is not None
        remaining = (stamped["dl"] - datetime.now(tz=timezone.utc)).total_seconds()
        assert 590.0 <= remaining <= 610.0

    def test_non_resume_source_untouched(self):
        ctx = SimpleNamespace()          # no with_pipeline_deadline -- must not be called
        assert uir._stamp_resume_pipeline_deadline(ctx, "test_failure") is ctx

    def test_failsoft_on_broken_ctx(self):
        class _Broken:
            def with_pipeline_deadline(self, dl):
                raise RuntimeError("frozen")

        b = _Broken()
        assert uir._stamp_resume_pipeline_deadline(b, "fsm_resume") is b


def _load_driver():
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "isomorphic_a1_local.py"
    spec = importlib.util.spec_from_file_location("_iso_walls_test", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_iso_walls_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestArmTimeAdaptiveWall:
    def test_cycle_derived_from_round_physics(self, monkeypatch):
        mod = _load_driver()
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "4.0")
        monkeypatch.setenv("JARVIS_LOCAL_SEED_CTX_BASELINE", "8192")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_TIMEOUT_SEED_MS", "30000")
        # 5 rounds x 30s x 4 x (16384/8192=2) = 1200s
        assert mod._expected_agentic_cycle_s() == pytest.approx(1200.0, rel=0.05)

    def test_more_rounds_more_wall(self, monkeypatch):
        mod = _load_driver()
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        five = mod._expected_agentic_cycle_s()
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "10")
        ten = mod._expected_agentic_cycle_s()
        assert ten > five

    def test_never_below_legacy_margin(self, monkeypatch):
        mod = _load_driver()
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "1")
        monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "1.0")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "1024")
        assert mod._expected_agentic_cycle_s() >= 600.0
