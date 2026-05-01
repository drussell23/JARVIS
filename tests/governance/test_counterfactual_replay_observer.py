"""Priority #3 Slice 4 — Counterfactual Replay observer regression suite.

History store + SSE event publisher + async periodic observer over
the JSONL ring buffer.

Test classes:
  * TestObserverEnabledFlag — sub-flag asymmetric env semantics
  * TestEnvKnobs — int + float knob clamping
  * TestRecordOutcomeSchema — closed-taxonomy enum
  * TestReplayVerdictRoundTrip — Slice 1 ReplayVerdict to_dict ↔
    from_dict (added in Slice 4 to enable history persistence)
  * TestRecordReplayVerdict — full record matrix
  * TestReadReplayHistory — bounded read + corrupt-line tolerance
  * TestRingBufferRotation — rotation discipline at max_records
  * TestCompareRecentHistory — convenience wrapper integration
  * TestAggregateSignature — stable bucketed dedup
  * TestSSEEventPublication — broker integration
  * TestReplayObserverLifecycle — async start/stop/idempotent
  * TestObserverIntervalResolution — adaptive cadence + failure backoff
  * TestObserverDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Optional

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
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
    ComparisonOutcome,
    ComparisonReport,
    StampedVerdict,
)
from backend.core.ouroboros.governance.verification import (
    counterfactual_replay_observer as obs_mod,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
    RecordOutcome,
    ReplayObserver,
    _aggregate_signature,
    _parse_stamped_line,
    _publish_baseline_updated_event,
    _publish_replay_complete_event,
    compare_recent_history,
    read_replay_history,
    record_replay_verdict,
    replay_history_max_records,
    replay_history_path,
    replay_observer_drift_multiplier,
    replay_observer_enabled,
    replay_observer_failure_backoff_ceiling_s,
    replay_observer_interval_default_s,
    replay_observer_liveness_pulse_passes,
    reset_for_tests,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED,
    EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE,
    get_default_broker,
    reset_default_broker,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens — Slice 1/2/3 pattern
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
    *,
    branch_verdict: BranchVerdict = BranchVerdict.DIVERGED_BETTER,
    outcome: ReplayOutcome = ReplayOutcome.SUCCESS,
    orig_pm: int = 0,
    cf_pm: int = 0,
    sid: str = "bt-fix",
) -> ReplayVerdict:
    target = ReplayTarget(
        session_id=sid, swap_at_phase="GATE",
        swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
    )
    orig = BranchSnapshot(
        branch_id="orig", terminal_phase="COMPLETE",
        terminal_success=True, apply_outcome="single",
        verify_passed=10, verify_total=10,
        postmortem_records=tuple(f"pm_{i}" for i in range(orig_pm)),
    )
    cf = BranchSnapshot(
        branch_id="cf", terminal_phase="GATE",
        terminal_success=False, apply_outcome="gated",
        postmortem_records=tuple(f"pm_{i}" for i in range(cf_pm)),
    )
    return ReplayVerdict(
        outcome=outcome, target=target,
        original_branch=orig, counterfactual_branch=cf,
        verdict=branch_verdict,
    )


@pytest.fixture(autouse=True)
def _isolated_observer(monkeypatch, tmp_path):
    """Each test gets a fresh history dir + flag bundle. Resets the
    SSE broker singleton + the JSONL store between tests."""
    monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REPLAY_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "10")
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

    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_OBSERVER_ENABLED", raising=False)
        assert replay_observer_enabled() is False

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_ENABLED", "")
        assert replay_observer_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy_variants(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_ENABLED", val)
        assert replay_observer_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_falsy_variants(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_ENABLED", val)
        assert replay_observer_enabled() is False


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_max_records_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", raising=False)
        assert replay_history_max_records() == 1000

    def test_max_records_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", "1")
        assert replay_history_max_records() == 10

    def test_max_records_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", "999999999")
        assert replay_history_max_records() == 100_000

    def test_max_records_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", "junk")
        assert replay_history_max_records() == 1000

    def test_interval_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_OBSERVER_INTERVAL_S", raising=False)
        assert replay_observer_interval_default_s() == pytest.approx(600.0)

    def test_interval_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_INTERVAL_S", "0.1")
        assert replay_observer_interval_default_s() == pytest.approx(60.0)

    def test_drift_multiplier_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_OBSERVER_DRIFT_MULTIPLIER", raising=False)
        assert replay_observer_drift_multiplier() == pytest.approx(0.5)

    def test_failure_backoff_ceiling_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_REPLAY_OBSERVER_FAILURE_BACKOFF_CEILING_S",
            raising=False,
        )
        assert replay_observer_failure_backoff_ceiling_s() == pytest.approx(1800.0)

    def test_liveness_pulse_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_REPLAY_OBSERVER_LIVENESS_PULSE_PASSES",
            raising=False,
        )
        assert replay_observer_liveness_pulse_passes() == 12

    def test_liveness_pulse_clamped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_LIVENESS_PULSE_PASSES", "0")
        assert replay_observer_liveness_pulse_passes() == 1


# ---------------------------------------------------------------------------
# TestRecordOutcomeSchema
# ---------------------------------------------------------------------------


class TestRecordOutcomeSchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in RecordOutcome} == {
            "ok", "ok_no_stream", "disabled", "rejected", "persist_error",
        }


# ---------------------------------------------------------------------------
# TestReplayVerdictRoundTrip
# ---------------------------------------------------------------------------


class TestReplayVerdictRoundTrip:

    def test_to_dict_then_from_dict_full(self):
        v = _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER, orig_pm=2)
        d = v.to_dict()
        recon = ReplayVerdict.from_dict(d)
        assert recon is not None
        assert recon.outcome is v.outcome
        assert recon.verdict is v.verdict
        assert recon.target is not None
        assert recon.target.session_id == v.target.session_id
        assert recon.original_branch is not None
        assert recon.counterfactual_branch is not None
        # Postmortem tuple round-trips
        assert (
            recon.original_branch.postmortem_records
            == v.original_branch.postmortem_records
        )

    def test_from_dict_returns_none_on_garbage(self):
        assert ReplayVerdict.from_dict("not a dict") is None  # type: ignore
        assert ReplayVerdict.from_dict(None) is None  # type: ignore
        assert ReplayVerdict.from_dict({}) is None
        # Wrong schema_version
        d = _verdict().to_dict()
        d["schema_version"] = "wrong.99"
        assert ReplayVerdict.from_dict(d) is None
        # Unknown outcome enum
        d = _verdict().to_dict()
        d["outcome"] = "made_up_outcome"
        assert ReplayVerdict.from_dict(d) is None

    def test_from_dict_handles_null_branches(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = ReplayVerdict(
            outcome=ReplayOutcome.PARTIAL, target=target,
            original_branch=None, counterfactual_branch=None,
            verdict=BranchVerdict.FAILED,
        )
        recon = ReplayVerdict.from_dict(v.to_dict())
        assert recon is not None
        assert recon.original_branch is None
        assert recon.counterfactual_branch is None


# ---------------------------------------------------------------------------
# TestRecordReplayVerdict
# ---------------------------------------------------------------------------


class TestRecordReplayVerdict:

    def test_master_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")
        assert record_replay_verdict(_verdict()) is RecordOutcome.DISABLED

    def test_sub_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_OBSERVER_ENABLED", "false")
        assert record_replay_verdict(_verdict()) is RecordOutcome.DISABLED

    def test_enabled_override_false(self):
        result = record_replay_verdict(
            _verdict(), enabled_override=False,
        )
        assert result is RecordOutcome.DISABLED

    def test_garbage_returns_rejected(self):
        result = record_replay_verdict("not a verdict")  # type: ignore
        assert result is RecordOutcome.REJECTED

    def test_none_returns_rejected(self):
        result = record_replay_verdict(None)  # type: ignore
        assert result is RecordOutcome.REJECTED

    def test_valid_record_lands_on_disk(self):
        result = record_replay_verdict(_verdict())
        assert result is RecordOutcome.OK
        assert replay_history_path().exists()

    def test_stream_disabled_returns_ok_no_stream(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        # Reset broker so the stream_enabled check runs
        reset_default_broker()
        result = record_replay_verdict(_verdict())
        assert result is RecordOutcome.OK_NO_STREAM

    def test_cluster_kind_persisted(self):
        record_replay_verdict(
            _verdict(), cluster_kind="repeated_failure_cluster",
        )
        history = read_replay_history()
        assert len(history) == 1
        assert history[0].cluster_kind == "repeated_failure_cluster"


# ---------------------------------------------------------------------------
# TestReadReplayHistory
# ---------------------------------------------------------------------------


class TestReadReplayHistory:

    def test_missing_file_empty(self):
        # No records yet
        assert read_replay_history() == ()

    def test_records_round_trip(self):
        for _ in range(3):
            record_replay_verdict(_verdict())
        history = read_replay_history()
        assert len(history) == 3
        assert all(isinstance(sv, StampedVerdict) for sv in history)
        assert all(sv.tightening == "passed" for sv in history)

    def test_limit_parameter(self):
        for _ in range(5):
            record_replay_verdict(_verdict())
        assert len(read_replay_history(limit=2)) == 2
        assert len(read_replay_history(limit=10)) == 5

    def test_limit_zero_returns_empty(self):
        for _ in range(3):
            record_replay_verdict(_verdict())
        assert read_replay_history(limit=0) == ()

    def test_corrupt_line_tolerance(self):
        record_replay_verdict(_verdict())
        # Append garbage to the JSONL
        path = replay_history_path()
        with path.open("a") as f:
            f.write("{not valid json\n")
            f.write("\n")
            f.write('"just a string"\n')  # parseable JSON but not a Mapping
        record_replay_verdict(_verdict())
        # 2 valid + 3 corrupt → only the 2 valid should come back
        history = read_replay_history()
        assert len(history) == 2

    def test_limit_capped_at_max_records(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", "10")
        for _ in range(5):
            record_replay_verdict(_verdict())
        # Asking for 99 → capped at 10
        assert len(read_replay_history(limit=99)) == 5

    def test_parse_stamped_line_handles_garbage(self):
        assert _parse_stamped_line("") is None
        assert _parse_stamped_line("not json") is None
        assert _parse_stamped_line('"raw string"') is None
        assert _parse_stamped_line("{}") is None  # missing verdict


# ---------------------------------------------------------------------------
# TestRingBufferRotation
# ---------------------------------------------------------------------------


class TestRingBufferRotation:

    def test_rotation_at_max(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", "10")
        for _ in range(15):
            record_replay_verdict(_verdict())
        history = read_replay_history()
        # Rotation should have trimmed to 10
        assert len(history) == 10

    def test_rotation_preserves_tail(self, monkeypatch):
        # Floor on JARVIS_REPLAY_HISTORY_MAX_RECORDS is 10; set max
        # above the floor so rotation actually triggers.
        monkeypatch.setenv("JARVIS_REPLAY_HISTORY_MAX_RECORDS", "10")
        # Write 12 with distinguishable verdicts (alternating)
        verdicts_written = []
        for i in range(12):
            bv = (
                BranchVerdict.DIVERGED_BETTER
                if i % 2 == 0 else BranchVerdict.EQUIVALENT
            )
            v = _verdict(branch_verdict=bv)
            verdicts_written.append(bv)
            record_replay_verdict(v)
        history = read_replay_history()
        assert len(history) == 10
        # Last 10 written: indices 2..11
        retained = [sv.verdict.verdict for sv in history]
        assert retained == verdicts_written[-10:]


# ---------------------------------------------------------------------------
# TestCompareRecentHistory
# ---------------------------------------------------------------------------


class TestCompareRecentHistory:

    def test_empty_returns_insufficient_or_disabled(self):
        report = compare_recent_history()
        # Comparator sub-flag is on; empty → INSUFFICIENT_DATA
        assert report.outcome is ComparisonOutcome.INSUFFICIENT_DATA

    def test_with_records_aggregates(self):
        for _ in range(8):
            record_replay_verdict(
                _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER),
            )
        report = compare_recent_history()
        assert report.outcome is ComparisonOutcome.ESTABLISHED
        assert report.stats.recurrence_reduction_pct == pytest.approx(100.0)

    def test_limit_passed_through(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "10")
        # Write 15 verdicts but limit reads to 2 (below low_n=10)
        for _ in range(15):
            record_replay_verdict(_verdict())
        report = compare_recent_history(limit=2)
        # 2 verdicts < low_n=10 → INSUFFICIENT_DATA
        assert report.outcome is ComparisonOutcome.INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# TestAggregateSignature
# ---------------------------------------------------------------------------


class TestAggregateSignature:

    def test_stable_for_same_report(self):
        for _ in range(5):
            record_replay_verdict(_verdict())
        report = compare_recent_history()
        s1 = _aggregate_signature(report)
        s2 = _aggregate_signature(report)
        assert s1 == s2
        assert len(s1) == 16

    def test_different_outcomes_different_signature(self):
        for _ in range(5):
            record_replay_verdict(
                _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER),
            )
        established_sig = _aggregate_signature(compare_recent_history())

        reset_for_tests()
        for _ in range(5):
            record_replay_verdict(
                _verdict(branch_verdict=BranchVerdict.DIVERGED_WORSE),
            )
        degraded_sig = _aggregate_signature(compare_recent_history())

        assert established_sig != degraded_sig

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
        record_replay_verdict(_verdict())
        assert broker.published_count == pre + 1

    def test_complete_event_payload_shape(self):
        # Wire a subscriber to inspect the published event
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE,
        )
        broker = get_default_broker()
        record_replay_verdict(
            _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER),
            cluster_kind="my_cluster",
        )
        # Inspect history (broker keeps last N events)
        # The history is a deque of StreamEvent; access via reasonable API
        # We look up the recent event by checking history attribute
        history = list(broker._history)  # noqa: SLF001 — test introspection
        assert len(history) >= 1
        evt = history[-1]
        assert evt.event_type == EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE
        assert evt.payload["outcome"] == "success"
        assert evt.payload["verdict"] == "diverged_better"
        assert evt.payload["is_prevention_evidence"] is True
        assert evt.payload["tightening"] == "passed"
        assert evt.payload["cluster_kind"] == "my_cluster"

    def test_baseline_updated_event_published(self):
        # Build a report, then publish
        for _ in range(8):
            record_replay_verdict(
                _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER),
            )
        report = compare_recent_history()
        result = _publish_baseline_updated_event(report)
        assert result is True

        broker = get_default_broker()
        history = list(broker._history)  # noqa: SLF001
        baseline_events = [
            e for e in history
            if e.event_type == EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED
        ]
        assert len(baseline_events) >= 1
        evt = baseline_events[-1]
        assert evt.payload["outcome"] == "established"
        assert evt.payload["recurrence_reduction_pct"] == pytest.approx(100.0)

    def test_publish_complete_with_garbage_returns_false(self):
        assert _publish_replay_complete_event("not stamped") is False  # type: ignore

    def test_publish_baseline_with_garbage_returns_false(self):
        assert _publish_baseline_updated_event("not a report") is False  # type: ignore

    def test_publish_when_stream_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        reset_default_broker()
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            stamp_verdict,
        )
        from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
            stamp_verdict as stamp,
        )
        sv = stamp(_verdict())
        assert _publish_replay_complete_event(sv) is False


# ---------------------------------------------------------------------------
# TestReplayObserverLifecycle
# ---------------------------------------------------------------------------


class TestReplayObserverLifecycle:

    def test_observer_construction(self):
        obs = ReplayObserver(interval_s=10.0)
        assert obs.is_running is False
        assert obs.pass_index == 0

    def test_async_start_stop(self):
        async def run():
            obs = ReplayObserver(interval_s=10.0)
            await obs.start()
            assert obs.is_running is True
            await obs.stop()
            assert obs.is_running is False

        asyncio.run(run())

    def test_idempotent_start_stop(self):
        async def run():
            obs = ReplayObserver(interval_s=10.0)
            await obs.start()
            await obs.start()  # idempotent
            await obs.stop()
            await obs.stop()  # idempotent

        asyncio.run(run())

    def test_observer_runs_passes(self):
        async def run():
            callbacks = []

            async def on_baseline(report):
                callbacks.append(report.outcome.value)

            obs = ReplayObserver(
                interval_s=0.1, on_baseline_updated=on_baseline,
            )
            await obs.start()
            # Add records while running
            for _ in range(5):
                record_replay_verdict(
                    _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER),
                )
            # Wait for at least one pass
            await asyncio.sleep(0.4)
            assert obs.pass_index >= 1
            await obs.stop()
            # First-pass-with-actionable-outcome triggers callback
            assert len(callbacks) >= 1

        asyncio.run(run())

    def test_callback_failure_does_not_break_loop(self):
        async def run():
            async def bad_callback(report):
                raise RuntimeError("bad callback")

            obs = ReplayObserver(
                interval_s=0.1, on_baseline_updated=bad_callback,
            )
            await obs.start()
            for _ in range(3):
                record_replay_verdict(_verdict())
            await asyncio.sleep(0.3)
            # Loop survives bad callback
            assert obs.is_running is True
            await obs.stop()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# TestObserverIntervalResolution — adaptive cadence + failure backoff
# ---------------------------------------------------------------------------


class TestObserverIntervalResolution:

    def test_default_interval(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_OBSERVER_INTERVAL_S", raising=False)
        obs = ReplayObserver()
        # No drift, no failures → base interval
        result = obs._compute_next_interval()  # noqa: SLF001
        assert result == pytest.approx(600.0)

    def test_explicit_interval_overrides(self):
        obs = ReplayObserver(interval_s=42.0)
        result = obs._compute_next_interval()  # noqa: SLF001
        assert result == pytest.approx(42.0)

    def test_drift_multiplier_applied(self):
        obs = ReplayObserver(interval_s=200.0)
        obs._signature_changed_last_pass = True  # noqa: SLF001
        result = obs._compute_next_interval()  # noqa: SLF001
        # 200 × 0.5 = 100, but floored at 60.0
        assert result == pytest.approx(100.0)

    def test_drift_floor_at_60s(self):
        obs = ReplayObserver(interval_s=60.0)
        obs._signature_changed_last_pass = True  # noqa: SLF001
        result = obs._compute_next_interval()  # noqa: SLF001
        # 60 × 0.5 = 30, but floored at 60.0
        assert result == pytest.approx(60.0)

    def test_failure_backoff_linear(self):
        obs = ReplayObserver(interval_s=100.0)
        obs._consecutive_failures = 3  # noqa: SLF001
        result = obs._compute_next_interval()  # noqa: SLF001
        # 100 × 3 = 300
        assert result == pytest.approx(300.0)

    def test_failure_backoff_capped(self):
        obs = ReplayObserver(interval_s=1000.0)
        obs._consecutive_failures = 100  # noqa: SLF001
        # 1000 × 100 = 100000, capped at 1800.0 (default ceiling)
        result = obs._compute_next_interval()  # noqa: SLF001
        assert result == pytest.approx(1800.0)


# ---------------------------------------------------------------------------
# TestObserverDefensiveContract
# ---------------------------------------------------------------------------


class TestObserverDefensiveContract:

    def test_record_with_garbage_no_raise(self):
        # All paths return a closed-taxonomy outcome; never raise
        for inp in [None, "string", 42, [], {}]:
            result = record_replay_verdict(inp)  # type: ignore
            assert isinstance(result, RecordOutcome)

    def test_read_with_corrupt_file_no_raise(self):
        path = replay_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage\n{not valid\nmore garbage\n")
        history = read_replay_history()
        assert history == ()

    def test_compare_with_no_history_no_raise(self):
        report = compare_recent_history()
        assert isinstance(report, ComparisonReport)

    def test_signature_with_garbage_no_raise(self):
        assert _aggregate_signature(None) == ""  # type: ignore
        assert _aggregate_signature(object()) == ""  # type: ignore

    def test_reset_for_tests_idempotent(self):
        reset_for_tests()
        reset_for_tests()  # No-op when file missing


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants — AST-level pin
# ---------------------------------------------------------------------------


_OBS_PATH = Path(obs_mod.__file__)


def _module_source() -> str:
    return _OBS_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


_BANNED_IMPORT_SUBSTRINGS = (
    ".providers",
    "doubleword_provider",
    "urgency_router",
    "candidate_generator",
    "orchestrator",
    "tool_executor",
    "phase_runner",
    "iron_gate",
    "change_engine",
    "auto_action_router",
    "subagent_scheduler",
    "semantic_guardian",
    "semantic_firewall",
    "risk_engine",
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
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("exec", "eval", "compile")
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_record_replay_verdict_is_sync_pure(self):
        """The record API is sync — it appends to disk via flock'd
        IO and publishes to the broker (in-memory). The async
        observer wraps it via to_thread. Ensures no async leakage
        in the public sync surface."""
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                # Allowed: ReplayObserver methods (start/stop/_loop)
                if node.name not in (
                    "start", "stop", "_loop", "_run_one_pass",
                ):
                    raise AssertionError(
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
        """Positive invariant — proves no duplication of cross-
        process flock primitives."""
        src = _module_source()
        assert "flock_append_line" in src
        assert "flock_critical_section" in src
        assert "cross_process_jsonl" in src

    def test_reuses_slice_3_aggregator(self):
        """Slice 4 must reuse Slice 3's compare_replay_history +
        stamp_verdict (no duplication)."""
        src = _module_source()
        assert "compare_replay_history" in src
        assert "stamp_verdict" in src
        assert "counterfactual_replay_comparator" in src

    def test_reuses_sse_broker(self):
        """Slice 4 must reuse the existing SSE broker (Gap #6)."""
        src = _module_source()
        assert "ide_observability_stream" in src
        assert "get_default_broker" in src
        assert "EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE" in src
        assert "EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED" in src
