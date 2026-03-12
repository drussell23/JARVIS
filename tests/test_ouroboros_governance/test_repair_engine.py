"""Tests for the L2 Iterative Self-Repair Loop engine.

Covers RepairBudget configuration, RepairEngine FSM, and repair workflows.
"""

from __future__ import annotations

import json
import os
import pytest

from backend.core.ouroboros.governance.repair_engine import RepairBudget


class TestRepairBudget:
    """Tests for RepairBudget dataclass and from_env() configuration."""

    def test_defaults(self):
        """Verify RepairBudget default values."""
        b = RepairBudget()
        assert b.enabled is False
        assert b.max_iterations == 5
        assert b.timebox_s == 120.0
        assert b.min_deadline_remaining_s == 10.0
        assert b.per_iteration_test_timeout_s == 60.0
        assert b.max_diff_lines == 150
        assert b.max_files_changed == 3
        assert b.max_total_validation_runs == 8
        assert b.no_progress_streak_kill == 2
        assert b.max_class_retries == {"syntax": 2, "test": 3, "flake": 2, "env": 1}
        assert b.flake_confirm_reruns == 1

    def test_from_env_defaults(self, monkeypatch):
        """Verify from_env() returns defaults when no env vars are set."""
        for k in (
            "JARVIS_L2_ENABLED",
            "JARVIS_L2_MAX_ITERS",
            "JARVIS_L2_TIMEBOX_S",
            "JARVIS_L2_MIN_DEADLINE_S",
            "JARVIS_L2_ITER_TEST_TIMEOUT_S",
            "JARVIS_L2_MAX_DIFF_LINES",
            "JARVIS_L2_MAX_FILES_CHANGED",
            "JARVIS_L2_MAX_VALIDATION_RUNS",
            "JARVIS_L2_NO_PROGRESS_KILL",
            "JARVIS_L2_CLASS_RETRIES_JSON",
            "JARVIS_L2_FLAKE_RERUNS",
        ):
            monkeypatch.delenv(k, raising=False)
        b = RepairBudget.from_env()
        assert b.enabled is False
        assert b.max_iterations == 5

    def test_from_env_reads_values(self, monkeypatch):
        """Verify from_env() reads and parses environment variables."""
        monkeypatch.setenv("JARVIS_L2_ENABLED", "true")
        monkeypatch.setenv("JARVIS_L2_MAX_ITERS", "3")
        monkeypatch.setenv("JARVIS_L2_TIMEBOX_S", "90.0")
        monkeypatch.setenv(
            "JARVIS_L2_CLASS_RETRIES_JSON",
            '{"syntax":1,"test":2,"flake":1,"env":0}',
        )
        b = RepairBudget.from_env()
        assert b.enabled is True
        assert b.max_iterations == 3
        assert b.timebox_s == 90.0
        assert b.max_class_retries["syntax"] == 1

    def test_frozen(self):
        """Verify RepairBudget is immutable (frozen)."""
        b = RepairBudget()
        with pytest.raises(Exception):
            b.enabled = True  # type: ignore[misc]
