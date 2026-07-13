"""Slice 11 Task 6 — observability-honesty riders (RED first).

(a) LiveWork quiet path emits ONE positive DEBUG line — Run-21's audit had
    to prove the gate ran by the ABSENCE of its fail-open line (inferential,
    not positive evidence).
(b) Scoped-verify timeout gets an honest label — the sentinel counters
    (failures=1, total=0) rendered as '1/0 tests failing' and masqueraded
    as a denominator bug across Runs #20/#21.
(c) The four Slice-11 promotion knobs are FlagRegistry-registered.
(d) The A1 driver opts into promotion and its stale 'writes land in
    repo_root' comment (false since Slice 56 whenever ledger sovereignty is
    armed via the persistent record) is corrected.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
ORCH = _REPO / "backend/core/ouroboros/governance/orchestrator.py"
RUNNER = (
    _REPO / "backend/core/ouroboros/governance/phase_runners/slice4b_runner.py"
)
DRIVER = _REPO / "scripts/isomorphic_a1_local.py"


class TestLiveWorkQuietDebug:
    def test_quiet_return_emits_positive_evidence(self):
        src = ORCH.read_text()
        i = src.index("async def _live_work_apply_gate")
        j = src.index("\n    async def ", i + 10)
        gate = src[i:j]
        assert "[LiveWork] quiet" in gate, (
            "the quiet return must log positive DEBUG evidence — audits "
            "currently must infer 'gate ran' from the ABSENCE of its "
            "fail-open line"
        )


class TestHonestTimeoutLabel:
    def test_both_verify_blocks_label_timeouts_honestly(self):
        for path in (ORCH, RUNNER):
            src = path.read_text()
            i = src.index("_verify_test_passed = True")
            j = src.index("Phase 8b: Auto-commit", i)
            block = src[i:j]
            assert "_verify_timed_out" in block, (
                f"{path.name}: the timeout except must set a flag instead "
                "of leaving only the 1/0 sentinel counters"
            )
            assert "timed out after" in block, (
                f"{path.name}: a timed-out scoped verify must say so — "
                "not 'scoped verify: 1/0 tests failing'"
            )


class TestPromotionFlagsRegistered:
    def test_all_four_knobs_in_registry(self):
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded, reset_default_registry,
        )
        reset_default_registry()
        reg = ensure_seeded()
        for name in (
            "JARVIS_WORKSPACE_PROMOTION_ENABLED",
            "JARVIS_PROMOTION_LIVE_WORK_CONSULT",
            "JARVIS_PROMOTION_MAX_COMMITS",
            "JARVIS_PROMOTION_REQUIRE_CLEAN_TARGETS",
        ):
            spec = reg.get_spec(name)
            assert spec is not None, f"{name} missing from FlagRegistry seed"
            assert spec.source_file, name
        reset_default_registry()


class TestDriverOptIn:
    def test_driver_arms_promotion(self):
        src = DRIVER.read_text()
        assert 'env["JARVIS_WORKSPACE_PROMOTION_ENABLED"] = "true"' in src, (
            "the A1 driver IS the ignition harness — it must opt into "
            "workspace promotion so verified repairs land on the tree "
            "TestWatcher polls"
        )

    def test_stale_repo_root_writes_comment_corrected(self):
        src = DRIVER.read_text()
        assert "autonomous writes land in repo_root" not in src, (
            "false since Slice 56 whenever ledger sovereignty is armed via "
            "the signed persistent record: ChangeEngine honors "
            "JARVIS_AUTO_COMMIT_WORKSPACE regardless of the file-isolation "
            "flag — the Run-21 composition bug"
        )
