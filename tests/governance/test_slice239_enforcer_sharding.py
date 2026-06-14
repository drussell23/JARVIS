"""Slice 239 — Adaptive Test-Sharding & Asynchronous Enforcement (layer 9).

The s235→238 arc closed budget/severance/paths/convergence/output-format/model-
routing/cascade. The remaining heavy-GOAL blocker: the TestCoverageEnforcer
INJECTS "generate tests for N uncovered files" into the PRIMARY op's prompt, so a
heavy multi-file GOAL balloons into a patch + N-file test-gen task under one
deadline → exhaustion (the s237/238 soaks confirmed TestCoverageEnforcer amplifies
the heavy GOAL).

Fix (decouple, don't cap): when budget is tight and there are >2 uncovered files,
the enforcer compiles an ISOLATED test-coverage payload and emits it as a SEPARATE
signal into the EXISTING UnifiedIntakeRouter WAL queue (reusing make_envelope +
router.ingest + the intake→op pipeline — no new sub-agent kernel) so the PRIMARY
patch graduates the Iron Gate cleanly while a later independent background op
fulfils coverage. Adaptive (scales to route / budget / complexity from existing
signals — no hardcoded cap), gated, fail-soft (no router → legacy inline inject).
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import intelligence_hooks as ih
from backend.core.ouroboros.governance.intake.intent_envelope import (
    make_envelope,
    _VALID_SOURCES,
)


class TestDetectUncovered:
    def _enforcer(self, tmp_path):
        return ih.TestCoverageEnforcer(tmp_path)

    def test_detects_uncovered_py_modules(self, tmp_path):
        enf = self._enforcer(tmp_path)
        out = enf.detect_uncovered(("backend/a.py", "backend/b.py"))
        assert set(out) == {"backend/a.py", "backend/b.py"}

    def test_skips_test_and_non_py_and_dunder(self, tmp_path):
        enf = self._enforcer(tmp_path)
        out = enf.detect_uncovered((
            "tests/test_a.py", "backend/b_test.py", "README.md",
            "backend/__init__.py", "conftest.py", "backend/real.py",
        ))
        assert out == ["backend/real.py"]

    def test_covered_file_excluded(self, tmp_path):
        # a module WITH a test file present is not "uncovered"
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_covered.py").write_text("def test_x(): pass\n")
        enf = self._enforcer(tmp_path)
        out = enf.detect_uncovered(("backend/covered.py",))
        assert out == []

    def test_check_and_inject_still_works(self, tmp_path):
        # legacy injection path must still produce an instruction (uses detect_uncovered)
        enf = self._enforcer(tmp_path)
        instr = enf.check_and_inject(("backend/a.py", "backend/b.py"), "desc")
        assert instr and "test coverage" in instr.lower()


class TestShouldDecoupleTestGen:
    """The adaptive decision — INVARIANT 1: tight budget + >2 uncovered → decouple."""

    def test_invariant1_tight_budget_many_uncovered_decouples(self):
        # the headline: standard route, tight budget, 3 (>2) uncovered → decouple
        assert ih.should_decouple_test_gen(
            provider_route="standard", remaining_s=20.0, uncovered_count=3,
            target_file_count=3, complexity="heavy_code", enabled=True,
        ) is True

    def test_ample_budget_keeps_inline(self):
        # plenty of time → inject inline (legacy), even with many uncovered
        assert ih.should_decouple_test_gen(
            provider_route="standard", remaining_s=600.0, uncovered_count=5,
            target_file_count=5, complexity="standard", enabled=True,
        ) is False

    def test_two_or_fewer_uncovered_never_decouples(self):
        # ">2 uncovered" gate — 2 is not enough to warrant a separate op
        assert ih.should_decouple_test_gen(
            provider_route="immediate", remaining_s=1.0, uncovered_count=2,
            target_file_count=2, complexity="heavy_code", enabled=True,
        ) is False

    def test_heavy_complexity_moderate_budget_decouples(self):
        assert ih.should_decouple_test_gen(
            provider_route="complex", remaining_s=100.0, uncovered_count=4,
            target_file_count=4, complexity="heavy_code", enabled=True,
        ) is True

    def test_disabled_never_decouples(self):
        assert ih.should_decouple_test_gen(
            provider_route="standard", remaining_s=1.0, uncovered_count=9,
            target_file_count=9, complexity="complex", enabled=False,
        ) is False

    def test_adaptive_route_scaled_floor(self):
        # immediate route has a tighter floor than complex — same remaining_s,
        # immediate decouples while complex (more headroom) may not
        imm = ih.should_decouple_test_gen(
            provider_route="immediate", remaining_s=50.0, uncovered_count=3,
            target_file_count=3, complexity="standard", enabled=True,
        )
        cplx = ih.should_decouple_test_gen(
            provider_route="complex", remaining_s=300.0, uncovered_count=3,
            target_file_count=3, complexity="standard", enabled=True,
        )
        assert imm is False  # 50s is above the immediate floor (~45s) and not heavy
        assert cplx is False  # 300s ample for complex
        # but a tight immediate budget decouples
        assert ih.should_decouple_test_gen(
            provider_route="immediate", remaining_s=30.0, uncovered_count=3,
            target_file_count=3, complexity="standard", enabled=True,
        ) is True

    def test_min_uncovered_env_tunable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TEST_SHARD_MIN_UNCOVERED", "5")
        # now 3 uncovered is below the (raised) floor → no decouple
        assert ih.should_decouple_test_gen(
            provider_route="standard", remaining_s=10.0, uncovered_count=3,
            target_file_count=3, complexity="heavy_code", enabled=True,
        ) is False

    def test_fail_soft_false_on_bad_input(self):
        assert ih.should_decouple_test_gen(
            provider_route="standard", remaining_s="bad", uncovered_count=3,
            target_file_count=3, complexity="x", enabled=True,
        ) is False


class TestShardingFlag:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TEST_SHARDING_ENABLED", raising=False)
        assert ih.test_sharding_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TEST_SHARDING_ENABLED", "false")
        assert ih.test_sharding_enabled() is False


class TestBuildTestCoverageEnvelope:
    """The isolated payload — reuses make_envelope; routes BACKGROUND; dedup-stable."""

    def test_valid_background_envelope(self):
        env = ih.build_test_coverage_envelope(
            uncovered_files=["backend/a.py", "backend/b.py", "backend/c.py"],
            parent_op_id="op-parent-123", repo="jarvis",
        )
        assert env.source == "test_coverage"
        assert env.urgency == "low"
        assert env.routing_override == "background"
        assert set(env.target_files) == {"backend/a.py", "backend/b.py", "backend/c.py"}
        assert env.requires_human_ack is False

    def test_dedup_signature_deterministic(self):
        # same uncovered set (order-independent) → same dedup_key (re-emit suppressed)
        a = ih.build_test_coverage_envelope(
            uncovered_files=["backend/a.py", "backend/b.py"],
            parent_op_id="op-1", repo="jarvis",
        )
        b = ih.build_test_coverage_envelope(
            uncovered_files=["backend/b.py", "backend/a.py"],
            parent_op_id="op-2", repo="jarvis",
        )
        assert a.dedup_key == b.dedup_key

    def test_different_files_different_dedup(self):
        a = ih.build_test_coverage_envelope(
            uncovered_files=["backend/a.py"], parent_op_id="op-1", repo="jarvis",
        )
        b = ih.build_test_coverage_envelope(
            uncovered_files=["backend/z.py"], parent_op_id="op-1", repo="jarvis",
        )
        assert a.dedup_key != b.dedup_key

    def test_evidence_records_parent_and_signature(self):
        env = ih.build_test_coverage_envelope(
            uncovered_files=["backend/a.py", "backend/b.py", "backend/c.py"],
            parent_op_id="op-parent-xyz", repo="jarvis",
        )
        assert env.evidence.get("signature")
        assert env.evidence.get("parent_op_id") == "op-parent-xyz"


class TestSourceRegistration:
    def test_test_coverage_is_a_valid_source(self):
        assert "test_coverage" in _VALID_SOURCES
        # constructing one must not raise the source-validation error
        env = make_envelope(
            source="test_coverage", description="d", target_files=("a.py",),
            repo="jarvis", confidence=0.9, urgency="low",
            evidence={"signature": "sig"}, requires_human_ack=False,
        )
        assert env.source == "test_coverage"

    def test_test_coverage_has_priority_tier(self):
        from backend.core.ouroboros.governance.intake import unified_intake_router as r
        # must be in the deferred (background) tier OR the priority map — not unmapped
        in_deferred = "test_coverage" in r._PRIORITY_MAP_DEFERRED
        in_map = "test_coverage" in r._PRIORITY_MAP
        assert in_deferred or in_map


class TestOrchestratorWiring:
    """Source pins: the seam decouples via the router when the decision fires, and
    fail-softs to legacy inline injection otherwise / when no router is available."""

    def test_seam_consults_decouple_decision_and_emits(self):
        from backend.core.ouroboros.governance import orchestrator as o
        src = inspect.getsource(o)
        assert "should_decouple_test_gen(" in src, "seam must consult the adaptive decision"
        assert "build_test_coverage_envelope(" in src, "seam must compile the isolated payload"
        assert "ingest(" in src, "decoupled payload must be emitted to the intake router"

    def test_seam_failsoft_keeps_legacy_inject(self):
        # the legacy check_and_inject path must remain as the fail-soft fallback
        from backend.core.ouroboros.governance import orchestrator as o
        src = inspect.getsource(o)
        assert "check_and_inject(" in src

    def test_live_plan_runner_seam_is_wired(self):
        # The phase-runner refactor moved the LIVE test-coverage enforcement into
        # plan_runner.py — the capstone soak proved THIS is the executing seam (the
        # orchestrator copy was dormant). It MUST carry the decouple wiring too, or
        # sharding silently never fires. Regression pin against that exact drift.
        from backend.core.ouroboros.governance.phase_runners import plan_runner as pr
        src = inspect.getsource(pr)
        assert "should_decouple_test_gen(" in src
        assert "build_test_coverage_envelope(" in src
        assert "ingest(" in src
        # the old backtick-count log must be gone (proof the old path was replaced)
        assert "uncovered files (op" not in src
