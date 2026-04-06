"""End-to-end integration tests for the Ouroboros Battle Test Runner.

Verifies that all battle test components wire together correctly:
  - BattleTestHarness full lifecycle (boot -> wait -> shutdown -> report)
  - CostTracker budget gating
  - NotebookGenerator from a real SessionRecorder summary
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.battle_test.harness import BattleTestHarness, HarnessConfig
from backend.core.ouroboros.battle_test.cost_tracker import CostTracker
from backend.core.ouroboros.battle_test.session_recorder import SessionRecorder
from backend.core.ouroboros.battle_test.notebook_generator import NotebookGenerator


# ---------------------------------------------------------------------------
# Test 1: Full battle test lifecycle
# ---------------------------------------------------------------------------


class TestFullBattleTestLifecycle:
    """test_full_battle_test_lifecycle: verifies all components wire together end-to-end."""

    @pytest.mark.asyncio
    async def test_full_battle_test_lifecycle(self, tmp_path):
        """Boot harness with mocked components, let idle watchdog fire, verify summary.json."""
        session_dir = tmp_path / "session"

        cfg = HarnessConfig(
            repo_path=tmp_path,
            cost_cap_usd=0.50,
            idle_timeout_s=1.0,
            session_dir=session_dir,
            notebook_output_dir=tmp_path / "notebooks",
        )
        harness = BattleTestHarness(cfg)

        # Mock all boot methods so no real Ouroboros components are imported
        harness.boot_oracle = AsyncMock()
        harness.boot_governance_stack = AsyncMock()
        harness.boot_governed_loop_service = AsyncMock()
        harness.boot_jarvis_tiers = AsyncMock()
        harness.create_branch = AsyncMock(return_value="ouroboros/test-branch")
        harness.boot_intake = AsyncMock()
        harness.boot_graduation = AsyncMock()

        # Record a fake operation to the recorder BEFORE running
        harness._session_recorder.record_operation(
            op_id="pre-run-op-1",
            status="completed",
            sensor="TestSensor",
            technique="test_technique",
            composite_score=0.65,
            elapsed_s=5.0,
        )

        # Let idle watchdog fire naturally (idle_timeout_s=1.0, no poke)
        await harness.run()

        # Verify the harness stopped due to idle timeout
        assert harness._stop_reason == "idle_timeout"

        # Verify the session directory has summary.json
        summary_path = session_dir / "summary.json"
        assert summary_path.exists(), "summary.json must be written to session_dir"

        # Verify summary.json has the correct session_id and operations count
        data = json.loads(summary_path.read_text())
        assert data["session_id"] == harness.session_id
        assert data["stats"]["attempted"] == 1
        assert data["stats"]["completed"] == 1

        # Verify all boot methods were called
        harness.boot_oracle.assert_awaited_once()
        harness.boot_governance_stack.assert_awaited_once()
        harness.boot_governed_loop_service.assert_awaited_once()
        harness.boot_jarvis_tiers.assert_awaited_once()
        harness.create_branch.assert_awaited_once()
        harness.boot_intake.assert_awaited_once()
        harness.boot_graduation.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2: CostTracker budget gating
# ---------------------------------------------------------------------------


class TestCostTrackerGatesBudget:
    """test_cost_tracker_gates_budget: verifies budget enforcement and persistence."""

    def test_cost_tracker_gates_budget(self, tmp_path):
        """Record costs across budget boundary, verify event fires, save and reload."""
        persist_path = tmp_path / "cost_tracker.json"
        tracker = CostTracker(budget_usd=0.10, persist_path=persist_path)

        # Record $0.05 — should NOT be exhausted
        tracker.record(provider="anthropic", cost_usd=0.05)
        assert not tracker.exhausted, "Tracker should not be exhausted at $0.05 of $0.10"
        assert not tracker.budget_event.is_set(), "budget_event should not be set yet"

        # Record another $0.06 — total $0.11, crosses the $0.10 budget
        tracker.record(provider="anthropic", cost_usd=0.06)
        assert tracker.exhausted, "Tracker should be exhausted after $0.11 of $0.10"
        assert tracker.budget_event.is_set(), "budget_event must be set when budget is exhausted"

        # Save state to disk
        tracker.save()
        assert persist_path.exists(), "Persist path must be written by save()"

        # Reload from disk and verify state is preserved
        reloaded = CostTracker(budget_usd=0.10, persist_path=persist_path)
        assert pytest.approx(reloaded.total_spent, abs=1e-6) == tracker.total_spent
        assert reloaded.exhausted, "Reloaded tracker must also be exhausted"
        assert reloaded.budget_event.is_set(), "Reloaded tracker must have budget_event set"
        assert reloaded.breakdown.get("anthropic", 0.0) == pytest.approx(0.11, abs=1e-6)


# ---------------------------------------------------------------------------
# Test 3: NotebookGenerator from a real SessionRecorder summary
# ---------------------------------------------------------------------------


class TestNotebookGeneratorFromSession:
    """test_notebook_generator_from_session: verifies notebook or markdown is generated."""

    def test_notebook_generator_from_session(self, tmp_path):
        """Record 5 operations via SessionRecorder, save summary, generate notebook."""
        session_dir = tmp_path / "session"
        session_dir.mkdir(parents=True)
        output_dir = tmp_path / "notebooks"

        # Create SessionRecorder and record 5 operations
        recorder = SessionRecorder(session_id="bt-integration-test-001")
        operations = [
            ("op-1", "completed", "TestFailureSensor", "module_mutation", 0.80, 10.0),
            ("op-2", "completed", "OpportunityMinerSensor", "metrics_feedback", 0.75, 8.5),
            ("op-3", "failed", "TestFailureSensor", "module_mutation", 0.92, 4.2),
            ("op-4", "cancelled", "OpportunityMinerSensor", "syntax_repair", 0.85, 3.1),
            ("op-5", "completed", "TestFailureSensor", "module_mutation", 0.70, 9.8),
        ]
        for op_id, status, sensor, technique, score, elapsed in operations:
            recorder.record_operation(
                op_id=op_id,
                status=status,
                sensor=sensor,
                technique=technique,
                composite_score=score,
                elapsed_s=elapsed,
            )

        # Save summary with realistic data
        summary_path = recorder.save_summary(
            output_dir=session_dir,
            stop_reason="idle_timeout",
            duration_s=120.5,
            cost_total=0.08,
            cost_breakdown={"anthropic": 0.08},
            branch_stats={
                "commits": 3,
                "files_changed": 5,
                "insertions": 42,
                "deletions": 7,
            },
            convergence_state="IMPROVING",
            convergence_slope=-0.012,
            convergence_r2=0.68,
        )

        assert summary_path.exists(), "summary.json must be written by save_summary()"

        # Verify the summary has correct data
        data = json.loads(summary_path.read_text())
        assert data["session_id"] == "bt-integration-test-001"
        assert data["stats"]["attempted"] == 5
        assert data["stats"]["completed"] == 3
        assert data["stats"]["failed"] == 1
        assert data["stats"]["cancelled"] == 1

        # Create NotebookGenerator from the summary path
        generator = NotebookGenerator(summary_path=summary_path)

        # Call generate() and verify output file exists
        output_path = generator.generate(output_dir=output_dir)
        assert output_path.exists(), "Generated output file must exist"

        # Verify it's either a notebook or markdown report
        assert output_path.suffix in (".ipynb", ".md"), (
            f"Expected .ipynb or .md output, got: {output_path.suffix}"
        )
