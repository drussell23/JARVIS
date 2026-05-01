"""Priority #3 Slice 5 — Counterfactual Replay graduation regression suite.

End-to-end pin tests proving that the 4-slice pipeline is structurally
sound post-graduation. Slices 1-4 each have their own focused regression
suites; this graduation suite is the cross-slice composition test:

  * The 4 master/sub-flag defaults are TRUE (Slice 5 graduation flip).
  * The 4 AST pins are registered + green in shipped_code_invariants.
  * The 6 FlagRegistry seeds are present.
  * End-to-end pipeline: synthetic ledger → engine → verdict → record →
    aggregator → ComparisonReport → SSE events fired.
  * The 2 SSE event vocabularies are registered in
    ide_observability_stream._VALID_EVENT_TYPES.
  * The Phase C MonotonicTighteningVerdict.PASSED canonical token
    appears on every output (verdict.detail + report.tightening +
    StampedVerdict.tightening).

Test classes:
  * TestGraduationFlagDefaults — all 4 flags default-true post-flip
  * TestGraduationASTInvariants — 4 new pins registered + green
  * TestGraduationFlagRegistrySeeds — 6 new FlagSpecs in SEED_SPECS
  * TestGraduationStreamVocabulary — 2 new event types registered
  * TestGraduationEndToEndPipeline — full Slice 1→4 round-trip
  * TestGraduationStampingCrossStack — PASSED token appears in every
    output surface
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
    counterfactual_replay_enabled,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_engine import (
    replay_engine_enabled,
    run_counterfactual_replay,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
    BaselineQuality,
    ComparisonOutcome,
    ComparisonReport,
    StampedVerdict,
    comparator_enabled,
    compare_replay_history,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
    RecordOutcome,
    compare_recent_history,
    read_replay_history,
    record_replay_verdict,
    replay_history_path,
    replay_observer_enabled,
    reset_for_tests,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED,
    EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE,
    _VALID_EVENT_TYPES,
    get_default_broker,
    reset_default_broker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record_dict(
    *,
    record_id: str,
    op_id: str,
    phase: str,
    kind: str,
    ordinal: int,
    output: dict,
    parents: tuple = (),
    wall_ts: float = 1000.0,
    session_id: str = "bt-grad",
) -> dict:
    row = {
        "record_id": record_id,
        "session_id": session_id,
        "op_id": op_id,
        "phase": phase,
        "kind": kind,
        "ordinal": ordinal,
        "inputs_hash": f"hash_{record_id}",
        "output_repr": json.dumps(output, sort_keys=True),
        "monotonic_ts": 10.0,
        "wall_ts": wall_ts,
        "schema_version": "decision_record.1",
    }
    if parents:
        row["parent_record_ids"] = list(parents)
    return row


@pytest.fixture
def synthetic_session(tmp_path):
    """Build a synthetic recorded session: ROUTE → GATE → APPLY →
    VERIFY chain plus a 'success' summary. Returns the bundle of
    paths the engine reads from."""
    sid = "bt-grad-fix"
    ledger_dir = tmp_path / "ledgers" / sid
    ledger_path = ledger_dir / "decisions.jsonl"
    summary_root = tmp_path / "sessions"
    summary_path = summary_root / sid / "summary.json"

    ledger_dir.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    records = [
        _make_record_dict(
            record_id="r1", op_id="op-1", phase="ROUTE",
            kind="route_assignment", ordinal=0,
            output={"route": "STANDARD"},
            wall_ts=1000.0, session_id=sid,
        ),
        _make_record_dict(
            record_id="r2", op_id="op-1", phase="GATE",
            kind="gate_decision", ordinal=0,
            output={"verdict": "auto_apply"},
            parents=("r1",), wall_ts=1001.0, session_id=sid,
        ),
        _make_record_dict(
            record_id="r3", op_id="op-1", phase="APPLY",
            kind="apply_outcome", ordinal=0,
            output={"applied": True},
            parents=("r2",), wall_ts=1002.0, session_id=sid,
        ),
        _make_record_dict(
            record_id="r4", op_id="op-1", phase="VERIFY",
            kind="test_run", ordinal=0,
            output={"passed": 10, "total": 10},
            parents=("r3",), wall_ts=1003.0, session_id=sid,
        ),
    ]
    with ledger_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")

    summary_path.write_text(json.dumps({
        "session_id": sid, "stop_reason": "complete",
        "duration_s": 100.0,
        "stats": {"attempted": 1, "completed": 1, "failed": 0,
                  "cancelled": 0, "queued": 0},
        "cost_total": 0.05,
        "cost_breakdown": {"claude": 0.05},
        "branch_stats": {"commits": 1, "files_changed": 1,
                         "insertions": 10, "deletions": 2},
        "convergence_state": "complete",
        "ops_digest": {
            "last_apply_mode": "single",
            "last_apply_files": 1,
            "last_apply_op_id": "op-1",
            "last_verify_tests_passed": 10,
            "last_verify_tests_total": 10,
            "last_commit_hash": "abc123def456",
        },
    }))

    return sid, ledger_path, summary_root


@pytest.fixture(autouse=True)
def _graduation_isolated(monkeypatch, tmp_path):
    """Each test gets fresh state. Default flags are now ON
    post-graduation, but we still set explicit env to be deterministic
    and route the history dir to tmp_path so tests don't pollute each
    other or the project root."""
    monkeypatch.setenv("JARVIS_REPLAY_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "10")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    reset_default_broker()
    reset_for_tests()
    yield
    reset_default_broker()
    reset_for_tests()


# ---------------------------------------------------------------------------
# TestGraduationFlagDefaults — all 4 flags default-true post-flip
# ---------------------------------------------------------------------------


class TestGraduationFlagDefaults:

    def test_master_flag_default_true(self, monkeypatch):
        """Slice 5 graduation: JARVIS_COUNTERFACTUAL_REPLAY_ENABLED
        defaults to true (was false in Slices 1-4)."""
        monkeypatch.delenv(
            "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", raising=False,
        )
        assert counterfactual_replay_enabled() is True

    def test_engine_sub_flag_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_ENGINE_ENABLED", raising=False)
        assert replay_engine_enabled() is True

    def test_comparator_sub_flag_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_REPLAY_COMPARATOR_ENABLED", raising=False,
        )
        assert comparator_enabled() is True

    def test_observer_sub_flag_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_REPLAY_OBSERVER_ENABLED", raising=False,
        )
        assert replay_observer_enabled() is True

    def test_explicit_false_still_disables(self, monkeypatch):
        """Hot-revert path remains intact."""
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")
        assert counterfactual_replay_enabled() is False
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", "false")
        assert replay_engine_enabled() is False
        monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", "false")
        assert comparator_enabled() is False
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_ENABLED", "false")
        assert replay_observer_enabled() is False


# ---------------------------------------------------------------------------
# TestGraduationASTInvariants — 4 new pins registered + green
# ---------------------------------------------------------------------------


class TestGraduationASTInvariants:

    def test_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            list_shipped_code_invariants,
        )
        names = {inv.invariant_name for inv in list_shipped_code_invariants()}
        for required in (
            "counterfactual_replay_pure_stdlib",
            "counterfactual_replay_engine_cost_contract",
            "counterfactual_replay_comparator_authority",
            "counterfactual_replay_observer_uses_flock",
        ):
            assert required in names, (
                f"Slice 5 graduation pin {required!r} not registered"
            )

    def test_pins_validate_clean(self):
        """The 4 new pins MUST validate clean against the actual
        shipped modules. Catches any drift between the validator
        logic and the module code."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            list_shipped_code_invariants,
            validate_invariant,
        )
        targets = {
            "counterfactual_replay_pure_stdlib",
            "counterfactual_replay_engine_cost_contract",
            "counterfactual_replay_comparator_authority",
            "counterfactual_replay_observer_uses_flock",
        }
        for inv in list_shipped_code_invariants():
            if inv.invariant_name not in targets:
                continue
            violations = validate_invariant(inv)
            assert violations == (), (
                f"{inv.invariant_name} produced violations: "
                f"{violations}"
            )


# ---------------------------------------------------------------------------
# TestGraduationFlagRegistrySeeds — 6 new FlagSpecs in SEED_SPECS
# ---------------------------------------------------------------------------


class TestGraduationFlagRegistrySeeds:

    def test_six_seeds_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (
            SEED_SPECS,
        )
        names = {s.name for s in SEED_SPECS}
        for required in (
            "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED",
            "JARVIS_REPLAY_ENGINE_ENABLED",
            "JARVIS_REPLAY_COMPARATOR_ENABLED",
            "JARVIS_REPLAY_OBSERVER_ENABLED",
            "JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT",
            "JARVIS_REPLAY_HISTORY_MAX_RECORDS",
        ):
            assert required in names, (
                f"FlagRegistry seed missing: {required}"
            )

    def test_master_flag_seed_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (
            SEED_SPECS,
        )
        for s in SEED_SPECS:
            if s.name == "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED":
                assert s.default is True
                return
        raise AssertionError("master flag seed not found")

    def test_seeds_attribute_to_priority_3(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (
            SEED_SPECS,
        )
        replay_flag_names = {
            "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED",
            "JARVIS_REPLAY_ENGINE_ENABLED",
            "JARVIS_REPLAY_COMPARATOR_ENABLED",
            "JARVIS_REPLAY_OBSERVER_ENABLED",
            "JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT",
            "JARVIS_REPLAY_HISTORY_MAX_RECORDS",
        }
        for s in SEED_SPECS:
            if s.name in replay_flag_names:
                assert "Priority #3" in s.since, (
                    f"{s.name}: expected 'Priority #3' in since field, "
                    f"got {s.since!r}"
                )


# ---------------------------------------------------------------------------
# TestGraduationStreamVocabulary — 2 new event types registered
# ---------------------------------------------------------------------------


class TestGraduationStreamVocabulary:

    def test_events_in_valid_set(self):
        assert (
            EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE
            in _VALID_EVENT_TYPES
        )
        assert (
            EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED
            in _VALID_EVENT_TYPES
        )

    def test_event_strings_canonical(self):
        assert (
            EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE
            == "counterfactual_replay_complete"
        )
        assert (
            EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED
            == "counterfactual_baseline_updated"
        )


# ---------------------------------------------------------------------------
# TestGraduationEndToEndPipeline — full Slice 1→4 round-trip
# ---------------------------------------------------------------------------


class TestGraduationEndToEndPipeline:

    def test_full_pipeline_synthetic_session(self, synthetic_session):
        """The "money shot": synthetic recorded session → engine
        produces ReplayVerdict → observer records it → aggregator
        re-reads + produces ComparisonReport with ESTABLISHED
        outcome. End-to-end with NO env-flag overrides — proves
        the graduated default-true configuration is operational."""
        sid, ledger_path, summary_root = synthetic_session

        # 1. Engine produces a verdict from the recorded session.
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "approval_required"},
        )

        async def _run_engine():
            return await run_counterfactual_replay(
                target,
                ledger_path=ledger_path,
                summary_root=summary_root,
            )
        verdict = asyncio.run(_run_engine())

        assert verdict.outcome is ReplayOutcome.SUCCESS
        # Original succeeded, counterfactual would be gated → orig
        # was BETTER than the counterfactual would have been.
        assert verdict.verdict is BranchVerdict.DIVERGED_BETTER
        assert verdict.is_prevention_evidence() is True

        # 2. Observer records the verdict.
        broker = get_default_broker()
        pre_count = broker.published_count
        record_result = record_replay_verdict(
            verdict, cluster_kind="repeated_failure_cluster",
        )
        assert record_result is RecordOutcome.OK
        # Per-verdict SSE event fired.
        assert broker.published_count == pre_count + 1

        # 3. Read history back.
        history = read_replay_history()
        assert len(history) == 1
        assert isinstance(history[0], StampedVerdict)
        assert history[0].cluster_kind == "repeated_failure_cluster"
        assert history[0].tightening == "passed"

        # 4. Aggregate via the comparator (live default-true flags).
        report = compare_recent_history()
        assert isinstance(report, ComparisonReport)
        assert report.outcome is ComparisonOutcome.ESTABLISHED
        assert report.stats.recurrence_reduction_pct == pytest.approx(100.0)
        assert report.stats.prevention_count == 1
        assert report.tightening == "passed"

    def test_pipeline_handles_diverged_worse(self, synthetic_session):
        """A counterfactual that BEATS the original (DIVERGED_WORSE)
        should propagate cleanly through the same pipeline. Proves
        the comparator distinguishes prevention evidence from
        regression evidence."""
        sid, ledger_path, summary_root = synthetic_session

        # Construct a synthetic verdict where counterfactual
        # outperforms original (orig failed, cf succeeded).
        orig_failed = BranchSnapshot(
            branch_id="orig", terminal_phase="GATE",
            terminal_success=False, apply_outcome="none",
            verify_passed=0, verify_total=0,
            postmortem_records=("test_failure",),
        )
        cf_won = BranchSnapshot(
            branch_id="cf", terminal_phase="COMPLETE",
            terminal_success=True, apply_outcome="single",
            verify_passed=10, verify_total=10,
        )
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        diverged_worse = ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS,
            target=target,
            original_branch=orig_failed,
            counterfactual_branch=cf_won,
            verdict=BranchVerdict.DIVERGED_WORSE,
        )

        record_result = record_replay_verdict(diverged_worse)
        assert record_result is RecordOutcome.OK

        report = compare_recent_history()
        # 1 verdict, all regression → 100% regression rate → DEGRADED
        assert report.outcome is ComparisonOutcome.DEGRADED

    def test_hot_revert_master_flag_disables_full_pipeline(
        self, synthetic_session, monkeypatch,
    ):
        """Operator hot-revert path: setting master to false →
        every public surface returns DISABLED in lockstep. Proves
        the rollback knob actually works post-graduation."""
        sid, ledger_path, summary_root = synthetic_session
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")

        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )

        async def _run_engine():
            return await run_counterfactual_replay(
                target, ledger_path=ledger_path, summary_root=summary_root,
            )
        verdict = asyncio.run(_run_engine())
        assert verdict.outcome is ReplayOutcome.DISABLED

        record_result = record_replay_verdict(verdict)
        assert record_result is RecordOutcome.DISABLED

        report = compare_replay_history([verdict])
        assert report.outcome is ComparisonOutcome.DISABLED


# ---------------------------------------------------------------------------
# TestGraduationStampingCrossStack — PASSED stamp on every output
# ---------------------------------------------------------------------------


class TestGraduationStampingCrossStack:

    def test_engine_verdict_carries_passed_stamp(self, synthetic_session):
        """Slice 2's engine stamps PASSED in detail string."""
        sid, ledger_path, summary_root = synthetic_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )

        async def _run():
            return await run_counterfactual_replay(
                target, ledger_path=ledger_path, summary_root=summary_root,
            )
        verdict = asyncio.run(_run())
        assert "monotonic_tightening=passed" in (verdict.detail or "")

    def test_stamped_verdict_carries_passed(self):
        """Slice 3's StampedVerdict.tightening is always 'passed'."""
        from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
            stamp_verdict,
        )
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS,
            target=target,
            verdict=BranchVerdict.DIVERGED_BETTER,
        )
        sv = stamp_verdict(v)
        assert sv.tightening == "passed"

    def test_comparison_report_carries_passed(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS, target=target,
            verdict=BranchVerdict.DIVERGED_BETTER,
            original_branch=BranchSnapshot(
                branch_id="o", terminal_phase="C",
                terminal_success=True, apply_outcome="single",
                verify_passed=10, verify_total=10,
            ),
            counterfactual_branch=BranchSnapshot(
                branch_id="c", terminal_phase="G",
                terminal_success=False, apply_outcome="gated",
            ),
        )
        report = compare_replay_history([v])
        assert report.tightening == "passed"

    def test_observer_history_records_carry_passed(self, synthetic_session):
        sid, ledger_path, summary_root = synthetic_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "approval_required"},
        )

        async def _run():
            return await run_counterfactual_replay(
                target, ledger_path=ledger_path, summary_root=summary_root,
            )
        verdict = asyncio.run(_run())
        record_replay_verdict(verdict)

        history = read_replay_history()
        for sv in history:
            assert sv.tightening == "passed"
