"""Tests for trace lifecycle hooks."""
import tempfile
import unittest
from pathlib import Path


class TestTraceHooks(unittest.TestCase):
    def setUp(self):
        from backend.core.trace_bootstrap import _reset
        _reset()

    def tearDown(self):
        from backend.core.trace_bootstrap import _reset
        _reset()

    def test_on_phase_enter_emits_lifecycle_event(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_phase_enter

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            emitter = get_lifecycle_emitter()
            emitter.boot_start()

            on_phase_enter("resources", 35)

            recent = emitter.get_recent(5)
            phase_events = [e for e in recent if e["event_type"] == "phase_enter"]
            assert len(phase_events) == 1
            assert phase_events[0]["phase"] == "resources"

    def test_on_phase_exit_emits_lifecycle_event(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_phase_exit

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            emitter = get_lifecycle_emitter()
            emitter.boot_start()

            on_phase_exit("resources", 52, success=True)

            recent = emitter.get_recent(5)
            exit_events = [e for e in recent if e["event_type"] == "phase_exit"]
            assert len(exit_events) == 1
            assert exit_events[0]["phase"] == "resources"
            assert exit_events[0]["to_state"] == "success"

    def test_hooks_noop_when_uninitialized(self):
        from backend.core.trace_hooks import on_phase_enter, on_phase_exit, on_boot_start
        # Should not raise even when bootstrap not initialized
        on_boot_start()
        on_phase_enter("test", 0)
        on_phase_exit("test", 100, success=True)

    def test_on_boot_start_emits_boot_event(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            on_boot_start()
            emitter = get_lifecycle_emitter()
            recent = emitter.get_recent(5)
            boot_events = [e for e in recent if e["event_type"] == "boot_start"]
            assert len(boot_events) == 1

    def test_on_boot_complete_emits_event(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start, on_boot_complete

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            on_boot_start()
            on_boot_complete()
            emitter = get_lifecycle_emitter()
            recent = emitter.get_recent(5)
            types = [e["event_type"] for e in recent]
            assert "boot_start" in types
            assert "boot_complete" in types

    def test_on_phase_fail_emits_failure(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start, on_phase_fail

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            on_boot_start()
            on_phase_fail("trinity", "timeout after 300s")
            emitter = get_lifecycle_emitter()
            recent = emitter.get_recent(5)
            fail_events = [e for e in recent if e["event_type"] == "phase_fail"]
            assert len(fail_events) == 1
            assert fail_events[0]["error"] == "timeout after 300s"

    def test_on_boot_failed_emits_event(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start, on_boot_failed

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            on_boot_start()
            on_boot_failed(error="resources_failed", phase="resources", duration_s=12.5)
            emitter = get_lifecycle_emitter()
            recent = emitter.get_recent(5)
            fail_events = [e for e in recent if e["event_type"] == "boot_failed"]
            assert len(fail_events) == 1
            assert fail_events[0]["error"] == "resources_failed"
            assert fail_events[0]["phase"] == "resources"
            assert fail_events[0]["duration_s"] == 12.5

    def test_on_boot_failed_noop_when_uninitialized(self):
        from backend.core.trace_hooks import on_boot_failed
        # Should not raise even when bootstrap not initialized
        on_boot_failed(error="test_error", phase="test")

    def test_on_phase_exit_failure_path(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start, on_phase_exit

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            on_boot_start()
            on_phase_exit("preflight", progress=15, success=False, duration_s=5.2)
            emitter = get_lifecycle_emitter()
            recent = emitter.get_recent(5)
            exit_events = [e for e in recent if e["event_type"] == "phase_exit"]
            assert len(exit_events) == 1
            assert exit_events[0]["to_state"] == "failure"
            assert exit_events[0]["duration_s"] == 5.2

    def test_on_recovery_complete_emits_event(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start, on_recovery_start, on_recovery_complete

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            on_boot_start()
            on_recovery_start("backend", "stall detected")
            on_recovery_complete("backend", "restart_success")
            emitter = get_lifecycle_emitter()
            recent = emitter.get_recent(10)
            recovery_events = [e for e in recent if e["event_type"] == "recovery_complete"]
            assert len(recovery_events) == 1
            assert recovery_events[0]["component"] == "backend"
            assert recovery_events[0]["outcome"] == "restart_success"


if __name__ == "__main__":
    unittest.main()
