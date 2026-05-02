"""Priority #5 Slice 4 — CIGW observer regression suite.

History store + SSE event publisher + async periodic observer over
the JSONL ring buffer.

Test classes:
  * TestObserverEnabledFlag — sub-flag asymmetric env semantics
  * TestEnvKnobs — int + float knob clamping
  * TestRecordOutcomeSchema — closed-taxonomy enum
  * TestRecordGradientReport — full record matrix
  * TestReadGradientHistory — bounded read + corrupt-line tolerance
  * TestRingBufferRotation — rotation discipline
  * TestCompareRecentGradientHistory — convenience wrapper
  * TestAggregateSignature — stable bucketed dedup
  * TestSSEEventPublication — broker integration
  * TestCIGWObserverLifecycle — async start/stop/idempotent
  * TestObserverIntervalResolution — adaptive cadence + failure backoff
  * TestObserverDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification.gradient_watcher import (
    GradientBreach,
    GradientOutcome,
    GradientReading,
    GradientReport,
    GradientSeverity,
    MeasurementKind,
)
from backend.core.ouroboros.governance.verification.gradient_comparator import (
    CIGWComparisonReport,
    CIGWEffectivenessOutcome,
    StampedGradientReport,
)
from backend.core.ouroboros.governance.verification import (
    gradient_observer as obs_mod,
)
from backend.core.ouroboros.governance.verification.gradient_observer import (
    CIGWObserver,
    RecordOutcome,
    _aggregate_signature,
    _parse_stamped_line,
    _publish_baseline_updated_event,
    _publish_report_recorded_event,
    cigw_history_max_records,
    cigw_history_path,
    cigw_observer_drift_multiplier,
    cigw_observer_enabled,
    cigw_observer_failure_backoff_ceiling_s,
    cigw_observer_interval_default_s,
    cigw_observer_liveness_pulse_passes,
    compare_recent_gradient_history,
    read_gradient_history,
    record_gradient_report,
    reset_for_tests,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CIGW_BASELINE_UPDATED,
    EVENT_TYPE_CIGW_REPORT_RECORDED,
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


def _report(
    outcome: GradientOutcome = GradientOutcome.STABLE,
    severity: GradientSeverity = GradientSeverity.NONE,
    n_breaches: int = 0,
) -> GradientReport:
    readings = (
        GradientReading(
            target_id="f.py",
            measurement_kind=MeasurementKind.LINE_COUNT,
            baseline_mean=100.0, current_value=100.0,
            delta_abs=0.0, delta_pct=0.0,
            severity=severity,
        ),
    )
    breaches = tuple(
        GradientBreach(
            reading=GradientReading(
                target_id=f"b{i}.py",
                measurement_kind=MeasurementKind.LINE_COUNT,
                baseline_mean=100.0, current_value=200.0,
                delta_abs=100.0, delta_pct=100.0,
                severity=GradientSeverity.CRITICAL,
            ),
            detail=f"breach_{i}",
        )
        for i in range(n_breaches)
    )
    return GradientReport(
        outcome=outcome,
        readings=readings,
        breaches=breaches,
        total_samples=1,
    )


@pytest.fixture(autouse=True)
def _isolated_observer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CIGW_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CIGW_OBSERVER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CIGW_COMPARATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CIGW_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_CIGW_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_CIGW_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_CIGW_BASELINE_HIGH_N", "10")
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
        monkeypatch.delenv("JARVIS_CIGW_OBSERVER_ENABLED", raising=False)
        assert cigw_observer_enabled() is True

    def test_empty_treated_as_unset(self, monkeypatch):
        """Empty = unset = graduated default-true."""
        monkeypatch.setenv("JARVIS_CIGW_OBSERVER_ENABLED", "")
        assert cigw_observer_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_CIGW_OBSERVER_ENABLED", v)
        assert cigw_observer_enabled() is True


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_max_records_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_HISTORY_MAX_RECORDS", raising=False)
        assert cigw_history_max_records() == 1000

    def test_max_records_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_HISTORY_MAX_RECORDS", "1")
        assert cigw_history_max_records() == 10
        monkeypatch.setenv("JARVIS_CIGW_HISTORY_MAX_RECORDS", "999999999")
        assert cigw_history_max_records() == 100_000

    def test_interval_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_OBSERVER_INTERVAL_S", raising=False)
        assert cigw_observer_interval_default_s() == pytest.approx(600.0)

    def test_drift_multiplier_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CIGW_OBSERVER_DRIFT_MULTIPLIER", raising=False,
        )
        assert cigw_observer_drift_multiplier() == pytest.approx(0.5)

    def test_failure_backoff_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CIGW_OBSERVER_FAILURE_BACKOFF_CEILING_S",
            raising=False,
        )
        assert (
            cigw_observer_failure_backoff_ceiling_s()
            == pytest.approx(1800.0)
        )

    def test_liveness_pulse_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CIGW_OBSERVER_LIVENESS_PULSE_PASSES", raising=False,
        )
        assert cigw_observer_liveness_pulse_passes() == 12

    def test_liveness_pulse_clamped(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CIGW_OBSERVER_LIVENESS_PULSE_PASSES", "0",
        )
        assert cigw_observer_liveness_pulse_passes() == 1


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
# TestRecordGradientReport
# ---------------------------------------------------------------------------


class TestRecordGradientReport:

    def test_master_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", "false")
        assert record_gradient_report(_report()) is RecordOutcome.DISABLED

    def test_sub_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_OBSERVER_ENABLED", "false")
        assert record_gradient_report(_report()) is RecordOutcome.DISABLED

    def test_enabled_override_false(self):
        result = record_gradient_report(
            _report(), enabled_override=False,
        )
        assert result is RecordOutcome.DISABLED

    def test_garbage_rejected(self):
        result = record_gradient_report("not a report")  # type: ignore
        assert result is RecordOutcome.REJECTED

    def test_none_rejected(self):
        result = record_gradient_report(None)  # type: ignore
        assert result is RecordOutcome.REJECTED

    def test_valid_record_lands_on_disk(self):
        result = record_gradient_report(_report())
        assert result is RecordOutcome.OK
        assert cigw_history_path().exists()

    def test_stream_disabled_returns_ok_no_stream(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        reset_default_broker()
        result = record_gradient_report(_report())
        assert result is RecordOutcome.OK_NO_STREAM

    def test_cluster_kind_persisted(self):
        record_gradient_report(
            _report(), cluster_kind="my_cluster",
        )
        history = read_gradient_history()
        assert len(history) == 1
        assert history[0].cluster_kind == "my_cluster"


# ---------------------------------------------------------------------------
# TestReadGradientHistory
# ---------------------------------------------------------------------------


class TestReadGradientHistory:

    def test_missing_file_empty(self):
        assert read_gradient_history() == ()

    def test_records_round_trip(self):
        for _ in range(3):
            record_gradient_report(_report())
        history = read_gradient_history()
        assert len(history) == 3
        assert all(isinstance(sv, StampedGradientReport) for sv in history)
        assert all(sv.tightening == "passed" for sv in history)

    def test_limit_parameter(self):
        for _ in range(5):
            record_gradient_report(_report())
        assert len(read_gradient_history(limit=2)) == 2
        assert len(read_gradient_history(limit=10)) == 5

    def test_limit_zero_returns_empty(self):
        for _ in range(3):
            record_gradient_report(_report())
        assert read_gradient_history(limit=0) == ()

    def test_corrupt_line_tolerance(self):
        record_gradient_report(_report())
        path = cigw_history_path()
        with path.open("a") as f:
            f.write("{not valid json\n")
            f.write("\n")
            f.write('"raw string"\n')
        record_gradient_report(_report())
        history = read_gradient_history()
        assert len(history) == 2

    def test_breach_round_trip(self):
        breached = _report(
            outcome=GradientOutcome.BREACHED,
            severity=GradientSeverity.CRITICAL,
            n_breaches=2,
        )
        record_gradient_report(breached)
        history = read_gradient_history()
        assert len(history) == 1
        assert history[0].report.outcome is GradientOutcome.BREACHED
        assert len(history[0].report.breaches) == 2

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
        monkeypatch.setenv("JARVIS_CIGW_HISTORY_MAX_RECORDS", "10")
        for _ in range(15):
            record_gradient_report(_report())
        history = read_gradient_history()
        assert len(history) == 10

    def test_rotation_preserves_tail(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_HISTORY_MAX_RECORDS", "10")
        outcomes_written = []
        for i in range(12):
            outcome = (
                GradientOutcome.STABLE
                if i % 2 == 0 else GradientOutcome.DRIFTING
            )
            r = _report(
                outcome=outcome,
                severity=GradientSeverity.LOW if outcome is GradientOutcome.DRIFTING else GradientSeverity.NONE,
            )
            outcomes_written.append(outcome)
            record_gradient_report(r)
        history = read_gradient_history()
        assert len(history) == 10
        retained = [sv.report.outcome for sv in history]
        assert retained == outcomes_written[-10:]


# ---------------------------------------------------------------------------
# TestCompareRecentGradientHistory
# ---------------------------------------------------------------------------


class TestCompareRecentGradientHistory:

    def test_empty_returns_insufficient(self):
        report = compare_recent_gradient_history()
        assert report.outcome is CIGWEffectivenessOutcome.INSUFFICIENT_DATA

    def test_with_records_aggregates(self):
        for _ in range(8):
            record_gradient_report(_report())
        report = compare_recent_gradient_history()
        assert report.outcome is CIGWEffectivenessOutcome.HEALTHY
        assert report.stats.stable_rate == pytest.approx(100.0)

    def test_breach_record_aggregates_to_degraded(self):
        for _ in range(8):
            record_gradient_report(_report())
        record_gradient_report(_report(
            outcome=GradientOutcome.BREACHED,
            severity=GradientSeverity.CRITICAL, n_breaches=1,
        ))
        report = compare_recent_gradient_history()
        assert report.outcome is CIGWEffectivenessOutcome.DEGRADED


# ---------------------------------------------------------------------------
# TestAggregateSignature
# ---------------------------------------------------------------------------


class TestAggregateSignature:

    def test_stable_for_same_report(self):
        for _ in range(5):
            record_gradient_report(_report())
        report = compare_recent_gradient_history()
        s1 = _aggregate_signature(report)
        s2 = _aggregate_signature(report)
        assert s1 == s2
        assert len(s1) == 16

    def test_different_outcomes_different_signature(self):
        for _ in range(5):
            record_gradient_report(_report())
        healthy_sig = _aggregate_signature(
            compare_recent_gradient_history(),
        )

        reset_for_tests()
        for _ in range(5):
            record_gradient_report(_report(
                outcome=GradientOutcome.BREACHED,
                severity=GradientSeverity.CRITICAL, n_breaches=1,
            ))
        degraded_sig = _aggregate_signature(
            compare_recent_gradient_history(),
        )

        assert healthy_sig != degraded_sig

    def test_garbage_returns_empty(self):
        assert _aggregate_signature("not a report") == ""  # type: ignore
        assert _aggregate_signature(None) == ""  # type: ignore


# ---------------------------------------------------------------------------
# TestSSEEventPublication
# ---------------------------------------------------------------------------


class TestSSEEventPublication:

    def test_record_publishes_event(self):
        broker = get_default_broker()
        pre = broker.published_count
        record_gradient_report(_report())
        assert broker.published_count == pre + 1

    def test_event_payload_shape(self):
        record_gradient_report(
            _report(), cluster_kind="my_cluster",
        )
        broker = get_default_broker()
        history = list(broker._history)  # noqa: SLF001
        assert len(history) >= 1
        evt = history[-1]
        assert evt.event_type == EVENT_TYPE_CIGW_REPORT_RECORDED
        assert evt.payload["outcome"] == "stable"
        assert evt.payload["tightening"] == "passed"
        assert evt.payload["cluster_kind"] == "my_cluster"

    def test_baseline_updated_event_published(self):
        for _ in range(8):
            record_gradient_report(_report())
        report = compare_recent_gradient_history()
        result = _publish_baseline_updated_event(report)
        assert result is True

        broker = get_default_broker()
        history = list(broker._history)  # noqa: SLF001
        baseline_events = [
            e for e in history
            if e.event_type == EVENT_TYPE_CIGW_BASELINE_UPDATED
        ]
        assert len(baseline_events) >= 1
        evt = baseline_events[-1]
        assert evt.payload["outcome"] == "healthy"

    def test_publish_complete_with_garbage_returns_false(self):
        assert _publish_report_recorded_event("not stamped") is False  # type: ignore

    def test_publish_baseline_with_garbage_returns_false(self):
        assert (
            _publish_baseline_updated_event("not a report") is False  # type: ignore
        )

    def test_publish_when_stream_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        reset_default_broker()
        from backend.core.ouroboros.governance.verification.gradient_comparator import (
            stamp_gradient_report,
        )
        sv = stamp_gradient_report(_report())
        assert _publish_report_recorded_event(sv) is False


# ---------------------------------------------------------------------------
# TestCIGWObserverLifecycle
# ---------------------------------------------------------------------------


class TestCIGWObserverLifecycle:

    def test_observer_construction(self):
        obs = CIGWObserver(interval_s=10.0)
        assert obs.is_running is False
        assert obs.pass_index == 0

    def test_async_start_stop(self):
        async def run():
            obs = CIGWObserver(interval_s=10.0)
            await obs.start()
            assert obs.is_running is True
            await obs.stop()
            assert obs.is_running is False
        asyncio.run(run())

    def test_idempotent_start_stop(self):
        async def run():
            obs = CIGWObserver(interval_s=10.0)
            await obs.start()
            await obs.start()
            await obs.stop()
            await obs.stop()
        asyncio.run(run())

    def test_observer_runs_passes(self):
        async def run():
            callbacks = []
            async def cb(report):
                callbacks.append(report.outcome.value)

            obs = CIGWObserver(
                interval_s=0.1, on_baseline_updated=cb,
            )
            await obs.start()
            for _ in range(5):
                record_gradient_report(_report())
            await asyncio.sleep(0.4)
            assert obs.pass_index >= 1
            await obs.stop()
            assert len(callbacks) >= 1
        asyncio.run(run())

    def test_callback_failure_does_not_break_loop(self):
        async def run():
            async def bad_cb(report):
                raise RuntimeError("bad")

            obs = CIGWObserver(
                interval_s=0.1, on_baseline_updated=bad_cb,
            )
            await obs.start()
            for _ in range(3):
                record_gradient_report(_report())
            await asyncio.sleep(0.3)
            assert obs.is_running is True
            await obs.stop()
        asyncio.run(run())


# ---------------------------------------------------------------------------
# TestObserverIntervalResolution
# ---------------------------------------------------------------------------


class TestObserverIntervalResolution:

    def test_default_interval(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_OBSERVER_INTERVAL_S", raising=False)
        obs = CIGWObserver()
        assert obs._compute_next_interval() == pytest.approx(600.0)  # noqa: SLF001

    def test_explicit_interval_overrides(self):
        obs = CIGWObserver(interval_s=42.0)
        assert obs._compute_next_interval() == pytest.approx(42.0)  # noqa: SLF001

    def test_drift_multiplier_applied(self):
        obs = CIGWObserver(interval_s=200.0)
        obs._signature_changed_last_pass = True  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(100.0)  # noqa: SLF001

    def test_drift_floor_at_60s(self):
        obs = CIGWObserver(interval_s=60.0)
        obs._signature_changed_last_pass = True  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(60.0)  # noqa: SLF001

    def test_failure_backoff_linear(self):
        obs = CIGWObserver(interval_s=100.0)
        obs._consecutive_failures = 3  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(300.0)  # noqa: SLF001

    def test_failure_backoff_capped(self):
        obs = CIGWObserver(interval_s=1000.0)
        obs._consecutive_failures = 100  # noqa: SLF001
        assert obs._compute_next_interval() == pytest.approx(1800.0)  # noqa: SLF001


# ---------------------------------------------------------------------------
# TestObserverDefensiveContract
# ---------------------------------------------------------------------------


class TestObserverDefensiveContract:

    def test_record_with_garbage_no_raise(self):
        for inp in [None, "string", 42, [], {}]:
            result = record_gradient_report(inp)  # type: ignore
            assert isinstance(result, RecordOutcome)

    def test_read_with_corrupt_file_no_raise(self):
        path = cigw_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage\n{not valid\nmore garbage\n")
        history = read_gradient_history()
        assert history == ()

    def test_compare_with_no_history_no_raise(self):
        report = compare_recent_gradient_history()
        assert isinstance(report, CIGWComparisonReport)

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
            assert hasattr(obs_mod, name)

    def test_cost_contract_constant_present(self):
        assert hasattr(
            obs_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
        )
        assert obs_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_reuses_tier_1_flock(self):
        src = _module_source()
        assert "flock_append_line" in src
        assert "flock_critical_section" in src
        assert "cross_process_jsonl" in src

    def test_reuses_slice_3_aggregator(self):
        src = _module_source()
        assert "compare_gradient_history" in src
        assert "stamp_gradient_report" in src
        assert "gradient_comparator" in src

    def test_reuses_sse_broker(self):
        src = _module_source()
        assert "ide_observability_stream" in src
        assert "get_default_broker" in src
        assert "EVENT_TYPE_CIGW_REPORT_RECORDED" in src
        assert "EVENT_TYPE_CIGW_BASELINE_UPDATED" in src
