"""Priority #4 Slice 5 — Speculative Branch Tree graduation suite.

End-to-end pin tests proving the 4-slice pipeline is structurally
sound post-graduation. Cross-slice composition test:

  * 4 master/sub-flag defaults are TRUE (Slice 5 graduation flip)
  * 4 AST pins registered + green in shipped_code_invariants
  * 6 FlagRegistry seeds present
  * End-to-end pipeline: synthetic tree run → record → aggregator →
    SSE events fired
  * 2 SSE event vocabularies registered in
    ide_observability_stream._VALID_EVENT_TYPES
  * Phase C MonotonicTighteningVerdict.PASSED canonical token on
    every output

Test classes:
  * TestGraduationFlagDefaults — all 4 flags default-true
  * TestGraduationASTInvariants — 4 new pins registered + green
  * TestGraduationFlagRegistrySeeds — 6 new FlagSpecs
  * TestGraduationStreamVocabulary — 2 new event types
  * TestGraduationEndToEndPipeline — full Slice 1→4 round-trip
  * TestGraduationStampingCrossStack — PASSED token in every output
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchOutcome,
    BranchResult,
    BranchTreeTarget,
    EvidenceKind,
    TreeVerdict,
    TreeVerdictResult,
    sbt_enabled,
)
from backend.core.ouroboros.governance.verification.speculative_branch_runner import (
    READONLY_TOOL_ALLOWLIST,
    run_speculative_tree,
    sbt_runner_enabled,
)
from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (
    EffectivenessOutcome,
    SBTComparisonReport,
    StampedTreeVerdict,
    comparator_enabled,
    compare_tree_history,
    stamp_tree_verdict,
)
from backend.core.ouroboros.governance.verification.speculative_branch_observer import (
    RecordOutcome,
    compare_recent_tree_history,
    read_tree_history,
    record_tree_verdict,
    reset_for_tests,
    sbt_observer_enabled,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_SBT_BASELINE_UPDATED,
    EVENT_TYPE_SBT_TREE_COMPLETE,
    _VALID_EVENT_TYPES,
    get_default_broker,
    reset_default_broker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _AgreeingProber:
    """All branches return identical evidence → CONVERGED outcome."""
    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        return (
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash="agreed",
                confidence=0.9,
                source_tool="read_file",
            ),
        )


class _DisagreeingProber:
    """Each branch returns distinct evidence → DIVERGED outcome."""
    def __init__(self):
        self.call_count = 0

    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        self.call_count += 1
        return (
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash=f"distinct_{self.call_count}",
                confidence=0.9,
                source_tool="read_file",
            ),
        )


@pytest.fixture(autouse=True)
def _graduation_isolated(monkeypatch, tmp_path):
    """Each test gets fresh state. Default flags now ON post-graduation."""
    monkeypatch.setenv("JARVIS_SBT_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_SBT_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_SBT_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_SBT_BASELINE_HIGH_N", "10")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    reset_default_broker()
    reset_for_tests()
    yield
    reset_default_broker()
    reset_for_tests()


# ---------------------------------------------------------------------------
# TestGraduationFlagDefaults
# ---------------------------------------------------------------------------


class TestGraduationFlagDefaults:

    def test_master_flag_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_ENABLED", raising=False)
        assert sbt_enabled() is True

    def test_runner_sub_flag_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_RUNNER_ENABLED", raising=False)
        assert sbt_runner_enabled() is True

    def test_comparator_sub_flag_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_COMPARATOR_ENABLED", raising=False)
        assert comparator_enabled() is True

    def test_observer_sub_flag_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_OBSERVER_ENABLED", raising=False)
        assert sbt_observer_enabled() is True

    def test_explicit_false_still_disables(self, monkeypatch):
        """Hot-revert path remains intact."""
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        assert sbt_enabled() is False
        monkeypatch.setenv("JARVIS_SBT_RUNNER_ENABLED", "false")
        assert sbt_runner_enabled() is False
        monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", "false")
        assert comparator_enabled() is False
        monkeypatch.setenv("JARVIS_SBT_OBSERVER_ENABLED", "false")
        assert sbt_observer_enabled() is False


# ---------------------------------------------------------------------------
# TestGraduationASTInvariants
# ---------------------------------------------------------------------------


class TestGraduationASTInvariants:

    def test_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            list_shipped_code_invariants,
        )
        names = {inv.invariant_name for inv in list_shipped_code_invariants()}
        for required in (
            "speculative_branch_pure_stdlib",
            "speculative_branch_runner_cost_contract",
            "speculative_branch_comparator_authority",
            "speculative_branch_observer_uses_flock",
        ):
            assert required in names, (
                f"Slice 5 graduation pin {required!r} not registered"
            )

    def test_pins_validate_clean(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            list_shipped_code_invariants,
            validate_invariant,
        )
        targets = {
            "speculative_branch_pure_stdlib",
            "speculative_branch_runner_cost_contract",
            "speculative_branch_comparator_authority",
            "speculative_branch_observer_uses_flock",
        }
        for inv in list_shipped_code_invariants():
            if inv.invariant_name not in targets:
                continue
            violations = validate_invariant(inv)
            assert violations == (), (
                f"{inv.invariant_name} produced violations: "
                f"{violations}"
            )

    def test_invariant_count_at_least_41(self):
        """Priority #4 Slice 5 brings total invariants to 41
        (37 + 4 SBT pins)."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            list_shipped_code_invariants,
        )
        assert len(list_shipped_code_invariants()) >= 41


# ---------------------------------------------------------------------------
# TestGraduationFlagRegistrySeeds
# ---------------------------------------------------------------------------


class TestGraduationFlagRegistrySeeds:

    def test_six_seeds_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (
            SEED_SPECS,
        )
        names = {s.name for s in SEED_SPECS}
        for required in (
            "JARVIS_SBT_ENABLED",
            "JARVIS_SBT_RUNNER_ENABLED",
            "JARVIS_SBT_COMPARATOR_ENABLED",
            "JARVIS_SBT_OBSERVER_ENABLED",
            "JARVIS_SBT_RESOLUTION_THRESHOLD_PCT",
            "JARVIS_SBT_HISTORY_MAX_RECORDS",
        ):
            assert required in names, (
                f"FlagRegistry seed missing: {required}"
            )

    def test_master_flag_seed_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (
            SEED_SPECS,
        )
        for s in SEED_SPECS:
            if s.name == "JARVIS_SBT_ENABLED":
                assert s.default is True
                return
        raise AssertionError("master flag seed not found")

    def test_seeds_attribute_to_priority_4(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (
            SEED_SPECS,
        )
        sbt_flag_names = {
            "JARVIS_SBT_ENABLED",
            "JARVIS_SBT_RUNNER_ENABLED",
            "JARVIS_SBT_COMPARATOR_ENABLED",
            "JARVIS_SBT_OBSERVER_ENABLED",
            "JARVIS_SBT_RESOLUTION_THRESHOLD_PCT",
            "JARVIS_SBT_HISTORY_MAX_RECORDS",
        }
        for s in SEED_SPECS:
            if s.name in sbt_flag_names:
                assert "Priority #4" in s.since, (
                    f"{s.name}: expected 'Priority #4' in since "
                    f"field, got {s.since!r}"
                )


# ---------------------------------------------------------------------------
# TestGraduationStreamVocabulary
# ---------------------------------------------------------------------------


class TestGraduationStreamVocabulary:

    def test_events_in_valid_set(self):
        assert EVENT_TYPE_SBT_TREE_COMPLETE in _VALID_EVENT_TYPES
        assert EVENT_TYPE_SBT_BASELINE_UPDATED in _VALID_EVENT_TYPES

    def test_event_strings_canonical(self):
        assert EVENT_TYPE_SBT_TREE_COMPLETE == "sbt_tree_complete"
        assert EVENT_TYPE_SBT_BASELINE_UPDATED == "sbt_baseline_updated"


# ---------------------------------------------------------------------------
# TestGraduationEndToEndPipeline
# ---------------------------------------------------------------------------


class TestGraduationEndToEndPipeline:

    def test_full_pipeline_agreeing_prober(self):
        """The "money shot": agreeing prober → CONVERGED tree →
        record → aggregator → ESTABLISHED outcome. End-to-end with
        NO env-flag overrides — proves the graduated default-true
        configuration is operational."""
        target = BranchTreeTarget(
            decision_id="grad-test", ambiguity_kind="x",
            max_depth=1, max_breadth=2, max_wall_seconds=10.0,
        )

        # 1. Runner produces a CONVERGED verdict.
        async def _run():
            return await run_speculative_tree(
                target, prober=_AgreeingProber(),
            )
        result = asyncio.run(_run())
        assert result.outcome is TreeVerdict.CONVERGED
        assert result.is_actionable() is True

        # 2. Observer records the verdict.
        broker = get_default_broker()
        pre_count = broker.published_count
        record_result = record_tree_verdict(
            result, cluster_kind="agreeing_branches",
        )
        assert record_result is RecordOutcome.OK
        # Per-tree SSE event fired.
        assert broker.published_count == pre_count + 1

        # 3. Read history back.
        history = read_tree_history()
        assert len(history) == 1
        assert isinstance(history[0], StampedTreeVerdict)
        assert history[0].cluster_kind == "agreeing_branches"
        assert history[0].tightening == "passed"

        # 4. Aggregate via the comparator (live default-true flags).
        report = compare_recent_tree_history()
        assert isinstance(report, SBTComparisonReport)
        assert report.outcome is EffectivenessOutcome.ESTABLISHED
        assert report.stats.ambiguity_resolution_rate == pytest.approx(100.0)
        assert report.stats.converged_count == 1
        assert report.tightening == "passed"

    def test_pipeline_handles_disagreeing_prober(self):
        """A disagreeing prober produces DIVERGED — comparator sees
        escalation signal."""
        target = BranchTreeTarget(
            decision_id="grad-test", ambiguity_kind="x",
            max_depth=1, max_breadth=2, max_wall_seconds=10.0,
        )

        async def _run():
            return await run_speculative_tree(
                target, prober=_DisagreeingProber(),
            )
        result = asyncio.run(_run())
        assert result.outcome is TreeVerdict.DIVERGED

        record_result = record_tree_verdict(result)
        assert record_result is RecordOutcome.OK

        report = compare_recent_tree_history()
        # 1 verdict, 100% diverged → 100% escalation rate but
        # 0% resolution rate → INSUFFICIENT_DATA (below threshold)
        assert report.stats.diverged_count == 1
        assert report.stats.escalation_rate == pytest.approx(100.0)
        assert report.stats.ambiguity_resolution_rate == pytest.approx(0.0)

    def test_hot_revert_master_flag_disables_full_pipeline(
        self, monkeypatch,
    ):
        """Operator hot-revert: master=false → all surfaces DISABLED
        in lockstep."""
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        target = BranchTreeTarget(
            decision_id="grad-test", ambiguity_kind="x",
        )

        async def _run():
            return await run_speculative_tree(
                target, prober=_AgreeingProber(),
            )
        result = asyncio.run(_run())
        assert result.outcome is TreeVerdict.FAILED

        record_result = record_tree_verdict(result)
        assert record_result is RecordOutcome.DISABLED

        report = compare_tree_history([result])
        assert report.outcome is EffectivenessOutcome.DISABLED


# ---------------------------------------------------------------------------
# TestGraduationStampingCrossStack
# ---------------------------------------------------------------------------


class TestGraduationStampingCrossStack:

    def test_stamped_verdict_carries_passed(self):
        target = BranchTreeTarget(
            decision_id="x", ambiguity_kind="y",
        )
        v = TreeVerdictResult(
            outcome=TreeVerdict.CONVERGED, target=target,
            branches=(
                BranchResult(
                    branch_id="b1", outcome=BranchOutcome.SUCCESS,
                    fingerprint="fp",
                ),
            ),
            winning_branch_idx=0, winning_fingerprint="fp",
            aggregate_confidence=0.9,
        )
        sv = stamp_tree_verdict(v)
        assert sv.tightening == "passed"

    def test_comparison_report_carries_passed(self):
        target = BranchTreeTarget(decision_id="x", ambiguity_kind="y")
        v = TreeVerdictResult(
            outcome=TreeVerdict.CONVERGED, target=target,
            branches=(
                BranchResult(
                    branch_id="b1", outcome=BranchOutcome.SUCCESS,
                    evidence=(
                        BranchEvidence(
                            kind=EvidenceKind.FILE_READ,
                            content_hash="x", confidence=0.9,
                        ),
                    ),
                    fingerprint="fp",
                ),
            ),
            winning_branch_idx=0, winning_fingerprint="fp",
            aggregate_confidence=0.9,
        )
        report = compare_tree_history([v])
        assert report.tightening == "passed"

    def test_observer_history_records_carry_passed(self):
        target = BranchTreeTarget(
            decision_id="grad-stamp-test", ambiguity_kind="x",
            max_depth=1, max_breadth=2, max_wall_seconds=10.0,
        )

        async def _run():
            return await run_speculative_tree(
                target, prober=_AgreeingProber(),
            )
        result = asyncio.run(_run())
        record_tree_verdict(result)

        history = read_tree_history()
        for sv in history:
            assert sv.tightening == "passed"

    def test_readonly_tool_allowlist_present(self):
        """Move 5's READONLY_TOOL_ALLOWLIST is reused by SBT runner
        (verified by AST pin) — operators see the canonical 9-tool
        frozenset in the SBT runner's public API."""
        for required in (
            "read_file", "search_code", "get_callers",
            "glob_files", "list_dir", "list_symbols",
            "git_blame", "git_log", "git_diff",
        ):
            assert required in READONLY_TOOL_ALLOWLIST
        # Mutation tools must NOT appear
        for forbidden in (
            "edit_file", "write_file", "delete_file",
            "run_tests", "bash",
        ):
            assert forbidden not in READONLY_TOOL_ALLOWLIST
