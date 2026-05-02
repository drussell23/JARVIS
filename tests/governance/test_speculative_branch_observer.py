"""Priority #4 Slice 4 — SBT observer regression suite.

History store + SSE event publisher + async periodic observer over
the JSONL ring buffer.

Test classes:
  * TestObserverEnabledFlag — sub-flag asymmetric env semantics
  * TestEnvKnobs — int + float knob clamping
  * TestRecordOutcomeSchema — closed-taxonomy enum
  * TestRecordTreeVerdict — full record matrix
  * TestReadTreeHistory — bounded read + corrupt-line tolerance
  * TestRingBufferRotation — rotation discipline at max_records
  * TestCompareRecentTreeHistory — convenience wrapper integration
  * TestAggregateSignature — stable bucketed dedup
  * TestSSEEventPublication — broker integration
  * TestSBTObserverLifecycle — async start/stop/idempotent
  * TestObserverIntervalResolution — adaptive cadence + failure backoff
  * TestObserverDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import asyncio
import json
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
)
from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (
    EffectivenessOutcome,
    SBTComparisonReport,
    StampedTreeVerdict,
)
from backend.core.ouroboros.governance.verification import (
    speculative_branch_observer as obs_mod,
)
from backend.core.ouroboros.governance.verification.speculative_branch_observer import (
    RecordOutcome,
    SBTObserver,
    _aggregate_signature,
    _parse_stamped_line,
    _publish_baseline_updated_event,
    _publish_tree_complete_event,
    compare_recent_tree_history,
    read_tree_history,
    record_tree_verdict,
    reset_for_tests,
    sbt_history_max_records,
    sbt_history_path,
    sbt_observer_drift_multiplier,
    sbt_observer_enabled,
    sbt_observer_failure_backoff_ceiling_s,
    sbt_observer_interval_default_s,
    sbt_observer_liveness_pulse_passes,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_SBT_BASELINE_UPDATED,
    EVENT_TYPE_SBT_TREE_COMPLETE,
    get_default_broker,
    reset_default_broker,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TOKENS = (
    "e" + "val(",
    "e" + "xec(",
    "comp" + "ile(",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _verdict(
    outcome: TreeVerdict = TreeVerdict.CONVERGED,
    sid: str = "bt-fix",
) -> TreeVerdictResult:
    target = BranchTreeTarget(
        decision_id=sid, ambiguity_kind="x",
    )
    return TreeVerdictResult(
        outcome=outcome, target=target,
        branches=(
            BranchResult(
                branch_id="b1", outcome=BranchOutcome.SUCCESS,
                evidence=(
                    BranchEvidence(
                        kind=EvidenceKind.FILE_READ,
                        content_hash="x", confidence=0.9,
                    ),
                ),
                fingerprint="fp1",
            ),
        ),
        winning_branch_idx=0 if outcome is TreeVerdict.CONVERGED else None,
        winning_fingerprint="fp1" if outcome is TreeVerdict.CONVERGED else "",
        aggregate_confidence=0.9,
    )


@pytest.fixture(autouse=True)
def _isolated_observer(monkeypatch, tmp_path):
    """Each test gets a fresh history dir + flag bundle."""
    monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SBT_OBSERVER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", "true")
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
# TestObserverEnabledFlag
# ---------------------------------------------------------------------------


class TestObserverEnabledFlag:

    def test_default_true_post_graduation(self, monkeypatch):
        """Slice 5 graduation flipped observer sub-gate to True
        (2026-05-02)."""
        monkeypatch.delenv("JARVIS_SBT_OBSERVER_ENABLED", raising=False)
        assert sbt_observer_enabled() is True

    def test_empty_treated_as_unset(self, monkeypatch):
        """Empty = unset = graduated default-true."""
        monkeypatch.setenv("JARVIS_SBT_OBSERVER_ENABLED", "")
        assert sbt_observer_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_OBSERVER_ENABLED", v)
        assert sbt_observer_enabled() is True


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_max_records_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_HISTORY_MAX_RECORDS", raising=False)
        assert sbt_history_max_records() == 1000

    def test_max_records_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_HISTORY_MAX_RECORDS", "1")
        assert sbt_history_max_records() == 10
        monkeypatch.setenv("JARVIS_SBT_HISTORY_MAX_RECORDS", "999999999")
        assert sbt_history_max_records() == 100_000

    def test_interval_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_OBSERVER_INTERVAL_S", raising=False)
        assert sbt_observer_interval_default_s() == pytest.approx(600.0)

    def test_interval_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_OBSERVER_INTERVAL_S", "0.1")
        assert sbt_observer_interval_default_s() == pytest.approx(60.0)

    def test_drift_multiplier_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_OBSERVER_DRIFT_MULTIPLIER", raising=False,
        )
        assert sbt_observer_drift_multiplier() == pytest.approx(0.5)

    def test_failure_backoff_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_OBSERVER_FAILURE_BACKOFF_CEILING_S",
            raising=False,
        )
        assert (
            sbt_observer_failure_backoff_ceiling_s()
            == pytest.approx(1800.0)
        )

    def test_liveness_pulse_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_OBSERVER_LIVENESS_PULSE_PASSES", raising=False,
        )
        assert sbt_observer_liveness_pulse_passes() == 12

    def test_liveness_pulse_clamped(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_OBSERVER_LIVENESS_PULSE_PASSES", "0",
        )
        assert sbt_observer_liveness_pulse_passes() == 1


# ---------------------------------------------------------------------------
# TestRecordOutcomeSchema
# ---------------------------------------------------------------------------


class TestRecordOutcomeSchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in RecordOutcome} == {
            "ok", "ok_no_stream", "disabled", "rejected",
            "persist_error",
        }


# ---------------------------------------------------------------------------
# TestRecordTreeVerdict
# ---------------------------------------------------------------------------


class TestRecordTreeVerdict:

    def test_master_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        assert record_tree_verdict(_verdict()) is RecordOutcome.DISABLED

    def test_sub_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_OBSERVER_ENABLED", "false")
        assert record_tree_verdict(_verdict()) is RecordOutcome.DISABLED

    def test_enabled_override_false(self):
        result = record_tree_verdict(
            _verdict(), enabled_override=False,
        )
        assert result is RecordOutcome.DISABLED

    def test_garbage_rejected(self):
        result = record_tree_verdict("not a verdict")  # type: ignore
        assert result is RecordOutcome.REJECTED

    def test_none_rejected(self):
        result = record_tree_verdict(None)  # type: ignore
        assert result is RecordOutcome.REJECTED

    def test_valid_record_lands_on_disk(self):
        result = record_tree_verdict(_verdict())
        assert result is RecordOutcome.OK
        assert sbt_history_path().exists()

    def test_stream_disabled_returns_ok_no_stream(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        reset_default_broker()
        result = record_tree_verdict(_verdict())
        assert result is RecordOutcome.OK_NO_STREAM

    def test_cluster_kind_persisted(self):
        record_tree_verdict(
            _verdict(), cluster_kind="my_cluster",
        )
        history = read_tree_history()
        assert len(history) == 1
        assert history[0].cluster_kind == "my_cluster"


# ---------------------------------------------------------------------------
# TestReadTreeHistory
# ---------------------------------------------------------------------------


class TestReadTreeHistory:

    def test_missing_file_empty(self):
        assert read_tree_history() == ()

    def test_records_round_trip(self):
        for _ in range(3):
            record_tree_verdict(_verdict())
        history = read_tree_history()
        assert len(history) == 3
        assert all(isinstance(sv, StampedTreeVerdict) for sv in history)
        assert all(sv.tightening == "passed" for sv in history)

    def test_limit_parameter(self):
        for _ in range(5):
            record_tree_verdict(_verdict())
        assert len(read_tree_history(limit=2)) == 2
        assert len(read_tree_history(limit=10)) == 5

    def test_limit_zero_returns_empty(self):
        for _ in range(3):
            record_tree_verdict(_verdict())
        assert read_tree_history(limit=0) == ()

    def test_corrupt_line_tolerance(self):
        record_tree_verdict(_verdict())
        path = sbt_history_path()
        with path.open("a") as f:
            f.write("{not valid json\n")
            f.write("\n")
            f.write('"raw string"\n')
        record_tree_verdict(_verdict())
        history = read_tree_history()
        assert len(history) == 2

    def test_limit_capped_at_max_records(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_HISTORY_MAX_RECORDS", "10")
        for _ in range(5):
            record_tree_verdict(_verdict())
        assert len(read_tree_history(limit=99)) == 5

    def test_parse_stamped_line_handles_garbage(self):
        assert _parse_stamped_line("") is None
        assert _parse_stamped_line("not json") is None
        assert _parse_stamped_line('"raw string"') is None
        assert _parse_stamped_line("{}") is None


# ---------------------------------------------------------------------------
# TestRingBufferRotation
# ---------------------------------------------------------------------------


class TestRingBufferRotation:

    def test_rotation_at_max(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_HISTORY_MAX_RECORDS", "10")
        for _ in range(15):
            record_tree_verdict(_verdict())
        history = read_tree_history()
        assert len(history) == 10

    def test_rotation_preserves_tail(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_HISTORY_MAX_RECORDS", "10")
        verdicts_written = []
        for i in range(12):
            outcome = (
                TreeVerdict.CONVERGED
                if i % 2 == 0 else TreeVerdict.DIVERGED
            )
            v = _verdict(outcome=outcome)
            verdicts_written.append(outcome)
            record_tree_verdict(v)
        history = read_tree_history()
        assert len(history) == 10
        retained = [sv.verdict.outcome for sv in history]
        assert retained == verdicts_written[-10:]


# ---------------------------------------------------------------------------
# TestCompareRecentTreeHistory
# ---------------------------------------------------------------------------


class TestCompareRecentTreeHistory:

    def test_empty_returns_insufficient(self):
        report = compare_recent_tree_history()
        assert report.outcome is EffectivenessOutcome.INSUFFICIENT_DATA

    def test_with_records_aggregates(self):
        for _ in range(8):
            record_tree_verdict(_verdict())
        report = compare_recent_tree_history()
        assert report.outcome is EffectivenessOutcome.ESTABLISHED
        assert report.stats.ambiguity_resolution_rate == pytest.approx(
            100.0,
        )

    def test_limit_passed_through(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_BASELINE_LOW_N", "10")
        for _ in range(15):
            record_tree_verdict(_verdict())
        report = compare_recent_tree_history(limit=2)
        assert report.outcome is EffectivenessOutcome.INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# TestAggregateSignature
# ---------------------------------------------------------------------------


class TestAggregateSignature:

    def test_stable_for_same_report(self):
        for _ in range(5):
            record_tree_verdict(_verdict())
        report = compare_recent_tree_history()
        s1 = _aggregate_signature(report)
        s2 = _aggregate_signature(report)
        assert s1 == s2
        assert len(s1) == 16

    def test_different_outcomes_different_signature(self):
        for _ in range(5):
            record_tree_verdict(_verdict())
        established_sig = _aggregate_signature(
            compare_recent_tree_history(),
        )

        reset_for_tests()
        for _ in range(5):
            record_tree_verdict(
                _verdict(outcome=TreeVerdict.TRUNCATED),
            )
        ineffective_sig = _aggregate_signature(
            compare_recent_tree_history(),
        )

        assert established_sig != ineffective_sig

    def test_garbage_returns_empty(self):
        assert _aggregate_signature("not a report") == ""  # type: ignore
        assert _aggregate_signature(None) == ""  # type: ignore


# ---------------------------------------------------------------------------
# TestSSEEventPublication
# ---------------------------------------------------------------------------


class TestSSEEventPublication:

    def test_record_publishes_complete_event(self):
        broker = get_default_broker()
        pre = broker.published_count
        record_tree_verdict(_verdict())
        assert broker.published_count == pre + 1

    def test_complete_event_payload_shape(self):
        record_tree_verdict(
            _verdict(), cluster_kind="my_cluster",
        )
        broker = get_default_broker()
        history = list(broker._history)  # noqa: SLF001
        assert len(history) >= 1
        evt = history[-1]
        assert evt.event_type == EVENT_TYPE_SBT_TREE_COMPLETE
        assert evt.payload["outcome"] == "converged"
        assert evt.payload["is_actionable"] is True
        assert evt.payload["tightening"] == "passed"
        assert evt.payload["cluster_kind"] == "my_cluster"

    def test_baseline_updated_event_published(self):
        for _ in range(8):
            record_tree_verdict(_verdict())
        report = compare_recent_tree_history()
        result = _publish_baseline_updated_event(report)
        assert result is True

        broker = get_default_broker()
        history = list(broker._history)  # noqa: SLF001
        baseline_events = [
            e for e in history
            if e.event_type == EVENT_TYPE_SBT_BASELINE_UPDATED
        ]
        assert len(baseline_events) >= 1
        evt = baseline_events[-1]
        assert evt.payload["outcome"] == "established"
        assert evt.payload["ambiguity_resolution_rate"] == pytest.approx(
            100.0,
        )

    def test_publish_complete_with_garbage_returns_false(self):
        assert _publish_tree_complete_event("not stamped") is False  # type: ignore

    def test_publish_baseline_with_garbage_returns_false(self):
        assert (
            _publish_baseline_updated_event("not a report") is False  # type: ignore
        )

    def test_publish_when_stream_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        reset_default_broker()
        from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (
            stamp_tree_verdict,
        )
        sv = stamp_tree_verdict(_verdict())
        assert _publish_tree_complete_event(sv) is False


# ---------------------------------------------------------------------------
# TestSBTObserverLifecycle
# ---------------------------------------------------------------------------


class TestSBTObserverLifecycle:

    def test_observer_construction(self):
        obs = SBTObserver(interval_s=10.0)
        assert obs.is_running is False
        assert obs.pass_index == 0

    def test_async_start_stop(self):
        async def run():
            obs = SBTObserver(interval_s=10.0)
            await obs.start()
            assert obs.is_running is True
            await obs.stop()
            assert obs.is_running is False

        asyncio.run(run())

    def test_idempotent_start_stop(self):
        async def run():
            obs = SBTObserver(interval_s=10.0)
            await obs.start()
            await obs.start()
            await obs.stop()
            await obs.stop()

        asyncio.run(run())

    def test_observer_runs_passes(self):
        async def run():
            callbacks = []

            async def on_baseline(report):
                callbacks.append(report.outcome.value)

            obs = SBTObserver(
                interval_s=0.1, on_baseline_updated=on_baseline,
            )
            await obs.start()
            for _ in range(5):
                record_tree_verdict(_verdict())
            await asyncio.sleep(0.4)
            assert obs.pass_index >= 1
            await obs.stop()
            assert len(callbacks) >= 1

        asyncio.run(run())

    def test_callback_failure_does_not_break_loop(self):
        async def run():
            async def bad_callback(report):
                raise RuntimeError("bad")

            obs = SBTObserver(
                interval_s=0.1, on_baseline_updated=bad_callback,
            )
            await obs.start()
            for _ in range(3):
                record_tree_verdict(_verdict())
            await asyncio.sleep(0.3)
            assert obs.is_running is True
            await obs.stop()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# TestObserverIntervalResolution
# ---------------------------------------------------------------------------


class TestObserverIntervalResolution:

    def test_default_interval(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_OBSERVER_INTERVAL_S", raising=False)
        obs = SBTObserver()
        assert obs._compute_next_interval() == pytest.approx(600.0)  # noqa: SLF001

    def test_explicit_interval_overrides(self):
        obs = SBTObserver(interval_s=42.0)
        assert obs._compute_next_interval() == pytest.approx(42.0)  # noqa: SLF001

    def test_drift_multiplier_applied(self):
        obs = SBTObserver(interval_s=200.0)
        obs._signature_changed_last_pass = True  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(100.0)  # noqa: SLF001

    def test_drift_floor_at_60s(self):
        obs = SBTObserver(interval_s=60.0)
        obs._signature_changed_last_pass = True  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(60.0)  # noqa: SLF001

    def test_failure_backoff_linear(self):
        obs = SBTObserver(interval_s=100.0)
        obs._consecutive_failures = 3  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(300.0)  # noqa: SLF001

    def test_failure_backoff_capped(self):
        obs = SBTObserver(interval_s=1000.0)
        obs._consecutive_failures = 100  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(1800.0)  # noqa: SLF001


# ---------------------------------------------------------------------------
# TestObserverDefensiveContract
# ---------------------------------------------------------------------------


class TestObserverDefensiveContract:

    def test_record_with_garbage_no_raise(self):
        for inp in [None, "string", 42, [], {}]:
            result = record_tree_verdict(inp)  # type: ignore
            assert isinstance(result, RecordOutcome)

    def test_read_with_corrupt_file_no_raise(self):
        path = sbt_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage\n{not valid\nmore garbage\n")
        history = read_tree_history()
        assert history == ()

    def test_compare_with_no_history_no_raise(self):
        report = compare_recent_tree_history()
        assert isinstance(report, SBTComparisonReport)

    def test_signature_with_garbage_no_raise(self):
        assert _aggregate_signature(None) == ""  # type: ignore
        assert _aggregate_signature(object()) == ""  # type: ignore

    def test_reset_for_tests_idempotent(self):
        reset_for_tests()
        reset_for_tests()


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants
# ---------------------------------------------------------------------------


_OBS_PATH = Path(obs_mod.__file__)


def _module_source() -> str:
    return _OBS_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


_BANNED_IMPORT_SUBSTRINGS = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


class TestCostContractAuthorityInvariants:

    def test_no_banned_imports(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in _BANNED_IMPORT_SUBSTRINGS:
                        assert banned not in alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for banned in _BANNED_IMPORT_SUBSTRINGS:
                    assert banned not in module

    def test_no_eval_family_calls(self):
        src = _module_source()
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in ("exec", "eval", "compile")
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_async_limited_to_observer_lifecycle(self):
        """The only async functions should be the SBTObserver
        lifecycle methods. Catches accidental async leakage."""
        tree = _module_ast()
        allowed_async = {
            "start", "stop", "_loop", "_run_one_pass",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                assert node.name in allowed_async, (
                    f"unexpected async function: {node.name}"
                )

    def test_public_api_exported(self):
        for name in obs_mod.__all__:
            assert hasattr(obs_mod, name), (
                f"obs_mod.__all__ contains '{name}' which is not "
                f"a module attribute"
            )

    def test_cost_contract_constant_present(self):
        assert hasattr(
            obs_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
        )
        assert obs_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_reuses_tier_1_flock(self):
        """Positive invariant — proves zero duplication of
        cross-process flock primitives."""
        src = _module_source()
        assert "flock_append_line" in src
        assert "flock_critical_section" in src
        assert "cross_process_jsonl" in src

    def test_reuses_slice_3_aggregator(self):
        """Slice 4 must reuse Slice 3's compare_tree_history +
        stamp_tree_verdict (no duplication)."""
        src = _module_source()
        assert "compare_tree_history" in src
        assert "stamp_tree_verdict" in src
        assert "speculative_branch_comparator" in src

    def test_reuses_sse_broker(self):
        """Slice 4 must reuse the existing SSE broker (Gap #6)."""
        src = _module_source()
        assert "ide_observability_stream" in src
        assert "get_default_broker" in src
        assert "EVENT_TYPE_SBT_TREE_COMPLETE" in src
        assert "EVENT_TYPE_SBT_BASELINE_UPDATED" in src
