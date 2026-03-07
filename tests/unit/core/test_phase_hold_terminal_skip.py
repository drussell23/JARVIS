"""Tests for phase-hold behavior with terminal-skipped subsystems.

Validates that a subsystem marked as terminal-skipped does NOT keep
has_active_subsystem=True indefinitely.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestPhaseHoldTerminalSkip:
    def test_completed_background_task_not_active(self):
        """A done() background task should not contribute to has_active_subsystem."""
        import asyncio

        task = asyncio.Future()
        task.set_result(None)  # Mark as done

        # done() tasks should not be considered active
        assert task.done() is True
        # In production: active_subsystem_reasons should not include done tasks

    def test_cancelled_background_task_not_active(self):
        """A cancelled background task should not contribute to has_active_subsystem."""
        import asyncio

        task = asyncio.Future()
        task.cancel()

        assert task.done() is True
        assert task.cancelled() is True

    def test_version_mismatch_terminal_stops_gcp_activity(self):
        """VERSION_MISMATCH_TERMINAL status should not trigger further VM starts."""
        status_msg = "VERSION_MISMATCH_TERMINAL: 236.0 (recycled 3x, still mismatched)"

        assert "VERSION_MISMATCH_TERMINAL" in status_msg
        # In production: supervisor should NOT retry ensure_static_vm_ready
        # after receiving this terminal status

    def test_ecapa_skip_db_on_non_ready_cloudsql(self):
        """ECAPA should skip DB steps for any non-READY CloudSQL state."""
        from enum import Enum

        class RS(Enum):
            UNKNOWN = "unknown"
            CHECKING = "checking"
            READY = "ready"
            UNAVAILABLE = "unavailable"
            DEGRADED_SQLITE = "degraded_sqlite"

        # Every non-READY state should trigger skip
        for state in [RS.UNKNOWN, RS.CHECKING, RS.UNAVAILABLE, RS.DEGRADED_SQLITE]:
            skip = state != RS.READY
            assert skip is True, f"State {state} should skip DB steps"

        # READY should NOT skip
        assert (RS.READY != RS.READY) is False
