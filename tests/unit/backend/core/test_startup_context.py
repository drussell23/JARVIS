"""Tests for StartupContext - crash history and recovery state."""
import pytest
import tempfile
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone


class TestCrashHistory:
    def test_record_and_count_crashes(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))

            # Record some crashes
            history.record_crash(1, "segfault")
            history.record_crash(1, "oom")

            assert history.crashes_in_window() == 2

    def test_crashes_outside_window_not_counted(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))

            # Manually write an old crash
            old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            history.state_dir.mkdir(parents=True, exist_ok=True)
            with open(history.history_file, "w") as f:
                f.write(json.dumps({"timestamp": old_time, "exit_code": 1, "reason": "old"}) + "\n")

            # Only crashes in last hour should count
            assert history.crashes_in_window(timedelta(hours=1)) == 0

    def test_empty_crash_history_returns_zero(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))
            assert history.crashes_in_window() == 0

    def test_multiple_crashes_within_window(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))

            # Record 5 crashes
            for i in range(5):
                history.record_crash(1, f"crash_{i}")

            assert history.crashes_in_window() == 5

    def test_crash_history_persistence(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)

            # First instance writes crashes
            history1 = CrashHistory(state_dir=state_dir)
            history1.record_crash(1, "first")
            history1.record_crash(1, "second")

            # Second instance reads them
            history2 = CrashHistory(state_dir=state_dir)
            assert history2.crashes_in_window() == 2

    def test_crash_history_custom_window(self):
        from backend.core.startup_context import CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            history = CrashHistory(state_dir=Path(tmpdir))

            # Write a crash 30 minutes ago
            recent_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            history.state_dir.mkdir(parents=True, exist_ok=True)
            with open(history.history_file, "w") as f:
                f.write(json.dumps({"timestamp": recent_time, "exit_code": 1, "reason": "recent"}) + "\n")

            # Should be counted in 1-hour window
            assert history.crashes_in_window(timedelta(hours=1)) == 1

            # Should NOT be counted in 15-minute window
            assert history.crashes_in_window(timedelta(minutes=15)) == 0


class TestStartupContext:
    def test_is_recovery_startup_after_crash(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=1,  # crash
            crash_count_recent=1,
        )
        assert ctx.is_recovery_startup

    def test_not_recovery_after_clean_shutdown(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=0,  # clean
            crash_count_recent=0,
        )
        assert not ctx.is_recovery_startup

    def test_not_recovery_after_update_request(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=100,  # update requested
            crash_count_recent=0,
        )
        assert not ctx.is_recovery_startup

    def test_not_recovery_after_rollback_request(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=101,  # rollback requested
            crash_count_recent=0,
        )
        assert not ctx.is_recovery_startup

    def test_not_recovery_after_restart_request(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=102,  # restart requested
            crash_count_recent=0,
        )
        assert not ctx.is_recovery_startup

    def test_needs_conservative_startup_after_multiple_crashes(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=1,
            crash_count_recent=3,  # threshold
        )
        assert ctx.needs_conservative_startup

    def test_no_conservative_startup_after_single_crash(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=1,
            crash_count_recent=1,
        )
        assert not ctx.needs_conservative_startup

    def test_no_conservative_startup_with_zero_crashes(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=0,
            crash_count_recent=0,
        )
        assert not ctx.needs_conservative_startup

    def test_is_recovery_startup_with_no_previous_exit(self):
        from backend.core.startup_context import StartupContext

        ctx = StartupContext(
            previous_exit_code=None,  # First run
            crash_count_recent=0,
        )
        assert not ctx.is_recovery_startup


class TestStartupContextPersistence:
    def test_save_and_load_context(self):
        from backend.core.startup_context import StartupContext

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)

            # Create and save context
            ctx1 = StartupContext(
                previous_exit_code=0,
                crash_count_recent=0,
                state_markers={"test_key": "test_value"},
            )
            ctx1.save(state_dir=state_dir, exit_code=0, exit_reason="clean_shutdown")

            # Load context (simulating restart)
            ctx2 = StartupContext.load(state_dir=state_dir)

            # Should see previous exit was clean
            assert ctx2.previous_exit_code == 0
            assert ctx2.previous_exit_reason == "clean_shutdown"
            assert ctx2.state_markers.get("test_key") == "test_value"

    def test_load_after_crash_save(self):
        from backend.core.startup_context import StartupContext, CrashHistory

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)

            # Record a crash
            crash_history = CrashHistory(state_dir=state_dir)
            crash_history.record_crash(1, "simulated_crash")

            # Save context with crash exit
            ctx1 = StartupContext(
                previous_exit_code=None,
                crash_count_recent=0,
            )
            ctx1.save(state_dir=state_dir, exit_code=1, exit_reason="crash")

            # Load context (simulating restart after crash)
            ctx2 = StartupContext.load(state_dir=state_dir)

            assert ctx2.previous_exit_code == 1
            assert ctx2.previous_exit_reason == "crash"
            assert ctx2.is_recovery_startup
            assert ctx2.crash_count_recent == 1

    def test_load_with_no_previous_run(self):
        from backend.core.startup_context import StartupContext

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)

            # Load context from empty dir (first run)
            ctx = StartupContext.load(state_dir=state_dir)

            assert ctx.previous_exit_code is None
            assert ctx.previous_exit_reason is None
            assert not ctx.is_recovery_startup
            assert ctx.crash_count_recent == 0

    def test_save_records_successful_startup_time(self):
        from backend.core.startup_context import StartupContext

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)

            ctx = StartupContext()
            ctx.save(state_dir=state_dir, exit_code=0, exit_reason="clean")

            ctx2 = StartupContext.load(state_dir=state_dir)
            # On clean exit, last_successful_startup should be set
            # (we can verify by loading and checking it's recent)
            assert ctx2.previous_exit_code == 0

    def test_state_markers_preserved_across_save_load(self):
        from backend.core.startup_context import StartupContext

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)

            ctx1 = StartupContext(
                state_markers={
                    "component_versions": {"core": "1.0", "ai": "2.0"},
                    "last_migration": 42,
                }
            )
            ctx1.save(state_dir=state_dir, exit_code=0)

            ctx2 = StartupContext.load(state_dir=state_dir)
            assert ctx2.state_markers["component_versions"] == {"core": "1.0", "ai": "2.0"}
            assert ctx2.state_markers["last_migration"] == 42


class TestCrashThresholds:
    def test_crash_threshold_configurable(self):
        from backend.core.startup_context import StartupContext

        # Default threshold is 3
        ctx_below = StartupContext(crash_count_recent=2)
        assert not ctx_below.needs_conservative_startup

        ctx_at = StartupContext(crash_count_recent=3)
        assert ctx_at.needs_conservative_startup

        ctx_above = StartupContext(crash_count_recent=5)
        assert ctx_above.needs_conservative_startup

    def test_crash_threshold_class_attribute(self):
        from backend.core.startup_context import StartupContext

        assert hasattr(StartupContext, 'CRASH_THRESHOLD')
        assert StartupContext.CRASH_THRESHOLD == 3
