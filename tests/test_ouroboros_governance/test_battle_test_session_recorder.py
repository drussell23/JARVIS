"""Tests for the Ouroboros Battle Test SessionRecorder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder


class TestRecordOperation:
    """test_record_operation: record one operation, verify stats."""

    def test_record_operation(self):
        recorder = SessionRecorder(session_id="bt-test-001")
        recorder.record_operation(
            op_id="op-1",
            status="completed",
            sensor="TestFailureSensor",
            technique="module_mutation",
            composite_score=0.75,
            elapsed_s=12.3,
        )
        stats = recorder.stats
        assert stats["attempted"] == 1
        assert stats["completed"] == 1
        assert stats["failed"] == 0
        assert stats["cancelled"] == 0
        assert stats["queued"] == 0


class TestMultipleStatuses:
    """test_multiple_statuses: record completed/failed/cancelled/queued, verify all counts."""

    def test_multiple_statuses(self):
        recorder = SessionRecorder(session_id="bt-test-002")

        recorder.record_operation("op-1", "completed", "SensorA", "tech1", 0.5, 10.0)
        recorder.record_operation("op-2", "completed", "SensorB", "tech2", 0.6, 11.0)
        recorder.record_operation("op-3", "failed", "SensorA", "tech1", 0.9, 5.0)
        recorder.record_operation("op-4", "cancelled", "SensorC", "tech3", 0.8, 2.0)
        recorder.record_operation("op-5", "queued", "SensorB", "tech2", 0.7, 8.0)

        stats = recorder.stats
        assert stats["attempted"] == 5
        assert stats["completed"] == 2
        assert stats["failed"] == 1
        assert stats["cancelled"] == 1
        assert stats["queued"] == 1


class TestSensorCounts:
    """test_sensor_counts: record 3 ops from 2 sensors, verify top_sensors ordering."""

    def test_sensor_counts(self):
        recorder = SessionRecorder(session_id="bt-test-003")

        recorder.record_operation("op-1", "completed", "OpportunityMinerSensor", "tech1", 0.5, 5.0)
        recorder.record_operation("op-2", "completed", "OpportunityMinerSensor", "tech2", 0.6, 6.0)
        recorder.record_operation("op-3", "completed", "TestFailureSensor", "tech1", 0.7, 4.0)

        top = recorder.top_sensors(n=5)
        assert isinstance(top, list)
        assert len(top) == 2

        # Should be sorted descending by count
        names = [name for name, count in top]
        counts = [count for name, count in top]

        assert names[0] == "OpportunityMinerSensor"
        assert counts[0] == 2
        assert names[1] == "TestFailureSensor"
        assert counts[1] == 1


class TestTopTechniques:
    """test_top_techniques: verify top_techniques ordering."""

    def test_top_techniques(self):
        recorder = SessionRecorder(session_id="bt-test-004")

        recorder.record_operation("op-1", "completed", "SensorA", "module_mutation", 0.5, 5.0)
        recorder.record_operation("op-2", "completed", "SensorA", "module_mutation", 0.6, 6.0)
        recorder.record_operation("op-3", "completed", "SensorA", "module_mutation", 0.7, 7.0)
        recorder.record_operation("op-4", "completed", "SensorB", "metrics_feedback", 0.4, 4.0)
        recorder.record_operation("op-5", "failed", "SensorB", "metrics_feedback", 0.9, 2.0)

        top = recorder.top_techniques(n=5)
        names = [name for name, count in top]
        counts = [count for name, count in top]

        assert names[0] == "module_mutation"
        assert counts[0] == 3
        assert names[1] == "metrics_feedback"
        assert counts[1] == 2


class TestSaveSummary:
    """test_save_summary: save to tmp_path, verify summary.json exists and contains correct data."""

    def test_save_summary(self, tmp_path):
        recorder = SessionRecorder(session_id="bt-test-save")

        recorder.record_operation("op-1", "completed", "SensorA", "tech1", 0.5, 10.0)
        recorder.record_operation("op-2", "failed", "SensorB", "tech2", 0.9, 5.0)
        recorder.record_operation("op-3", "queued", "SensorA", "tech1", 0.7, 8.0)

        summary_path = recorder.save_summary(
            output_dir=tmp_path,
            stop_reason="cost_cap",
            duration_s=600.0,
            cost_total=0.48,
            cost_breakdown={"anthropic": 0.48},
            branch_stats={"branch": "ouroboros/battle-test-2026-04-06", "commits": 1, "files": 5},
            convergence_state="IMPROVING",
            convergence_slope=-0.014,
            convergence_r2=0.73,
        )

        assert summary_path.exists()
        assert summary_path.name == "summary.json"

        data = json.loads(summary_path.read_text())
        assert data["session_id"] == "bt-test-save"
        assert data["stop_reason"] == "cost_cap"
        assert data["stats"]["attempted"] == 3
        assert data["stats"]["completed"] == 1
        assert data["stats"]["failed"] == 1
        assert data["stats"]["queued"] == 1
        assert data["cost_total"] == pytest.approx(0.48)
        assert data["convergence_state"] == "IMPROVING"
        assert data["convergence_slope"] == pytest.approx(-0.014)
        assert data["convergence_r2"] == pytest.approx(0.73)

    def test_review_queue_written_when_queued_ops_exist(self, tmp_path):
        recorder = SessionRecorder(session_id="bt-test-queue")

        recorder.record_operation("op-1", "queued", "SensorA", "tech1", 0.7, 8.0)
        recorder.record_operation("op-2", "queued", "SensorB", "tech2", 0.6, 9.0)

        recorder.save_summary(
            output_dir=tmp_path,
            stop_reason="sigint",
            duration_s=120.0,
            cost_total=0.10,
            cost_breakdown={"anthropic": 0.10},
            branch_stats={},
            convergence_state="INSUFFICIENT_DATA",
            convergence_slope=0.0,
            convergence_r2=0.0,
        )

        review_path = tmp_path / "review_queue.jsonl"
        assert review_path.exists()

        lines = review_path.read_text().strip().splitlines()
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["op_id"] == "op-1"
        assert first["status"] == "queued"

    def test_no_review_queue_when_no_queued_ops(self, tmp_path):
        recorder = SessionRecorder(session_id="bt-test-no-queue")

        recorder.record_operation("op-1", "completed", "SensorA", "tech1", 0.5, 10.0)

        recorder.save_summary(
            output_dir=tmp_path,
            stop_reason="idle",
            duration_s=300.0,
            cost_total=0.05,
            cost_breakdown={"anthropic": 0.05},
            branch_stats={},
            convergence_state="PLATEAUED",
            convergence_slope=0.001,
            convergence_r2=0.1,
        )

        review_path = tmp_path / "review_queue.jsonl"
        assert not review_path.exists()


class TestFormatTerminalSummary:
    """test_format_terminal_summary: verify output contains expected sections."""

    def _make_recorder(self) -> SessionRecorder:
        recorder = SessionRecorder(session_id="bt-2026-04-06-143022")
        recorder.record_operation("op-1", "completed", "OpportunityMinerSensor", "module_mutation", 0.5, 10.0)
        recorder.record_operation("op-2", "completed", "TestFailureSensor", "metrics_feedback", 0.6, 15.0)
        recorder.record_operation("op-3", "failed", "OpportunityMinerSensor", "module_mutation", 0.9, 5.0)
        recorder.record_operation("op-4", "queued", "DocStalenessSensor", "tech3", 0.7, 8.0)
        return recorder

    def test_contains_session_complete(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="Cost cap reached ($0.50)",
            duration_s=2843.0,
            cost_total=0.48,
            cost_breakdown={"anthropic": 0.48},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 2, "files": 10},
            convergence_state="IMPROVING",
            convergence_slope=-0.0142,
            convergence_r2=0.73,
        )
        assert "SESSION COMPLETE" in output

    def test_contains_session_id(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="idle",
            duration_s=600.0,
            cost_total=0.20,
            cost_breakdown={"anthropic": 0.20},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 2, "files": 5},
            convergence_state="IMPROVING",
            convergence_slope=-0.01,
            convergence_r2=0.65,
        )
        assert "bt-2026-04-06-143022" in output

    def test_contains_convergence_state(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="sigint",
            duration_s=300.0,
            cost_total=0.15,
            cost_breakdown={"anthropic": 0.15},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 1, "files": 3},
            convergence_state="LOGARITHMIC",
            convergence_slope=-0.02,
            convergence_r2=0.85,
        )
        assert "LOGARITHMIC" in output

    def test_contains_operations_section(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="cost_cap",
            duration_s=1200.0,
            cost_total=0.50,
            cost_breakdown={"anthropic": 0.50},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 3, "files": 12},
            convergence_state="IMPROVING",
            convergence_slope=-0.01,
            convergence_r2=0.70,
        )
        assert "OPERATIONS" in output
        assert "Attempted" in output
        assert "Completed" in output

    def test_contains_cost_section(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="cost_cap",
            duration_s=900.0,
            cost_total=0.48,
            cost_breakdown={"anthropic": 0.41, "openai": 0.07},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 2, "files": 8},
            convergence_state="PLATEAUED",
            convergence_slope=0.001,
            convergence_r2=0.10,
        )
        assert "COST" in output
        assert "0.48" in output

    def test_contains_top_sensors(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="idle",
            duration_s=600.0,
            cost_total=0.20,
            cost_breakdown={"anthropic": 0.20},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 2, "files": 5},
            convergence_state="IMPROVING",
            convergence_slope=-0.01,
            convergence_r2=0.65,
        )
        assert "TOP SENSORS" in output
        assert "OpportunityMinerSensor" in output

    def test_contains_branch_section(self):
        recorder = self._make_recorder()
        output = recorder.format_terminal_summary(
            stop_reason="idle",
            duration_s=600.0,
            cost_total=0.20,
            cost_breakdown={"anthropic": 0.20},
            branch_name="ouroboros/battle-test-2026-04-06-143022",
            branch_stats={"commits": 2, "files": 5},
            convergence_state="IMPROVING",
            convergence_slope=-0.01,
            convergence_r2=0.65,
        )
        assert "BRANCH" in output
        assert "ouroboros/battle-test-2026-04-06-143022" in output

    def test_duration_formatting(self):
        recorder = SessionRecorder(session_id="bt-test-duration")
        output = recorder.format_terminal_summary(
            stop_reason="idle",
            duration_s=2843.0,  # 47m 23s
            cost_total=0.0,
            cost_breakdown={},
            branch_name="ouroboros/battle-test-2026",
            branch_stats={},
            convergence_state="INSUFFICIENT_DATA",
            convergence_slope=0.0,
            convergence_r2=0.0,
        )
        # 2843s = 47m 23s
        assert "47m" in output
        assert "23s" in output
