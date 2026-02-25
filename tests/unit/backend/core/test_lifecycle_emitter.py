"""Tests for lifecycle event emission."""
import json
import time
import unittest
from pathlib import Path
import tempfile


class TestLifecycleEmitter(unittest.TestCase):
    def _make_emitter(self, tmp_path, auto_flush_interval=0):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        return LifecycleEmitter(
            trace_dir=tmp_path,
            envelope_factory=factory,
            auto_flush_interval=auto_flush_interval,
        )

    def test_emit_phase_enter(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.phase_enter("preflight")
            events = emitter.get_recent(10)
            assert len(events) == 1
            assert events[0]["event_type"] == "phase_enter"
            assert events[0]["phase"] == "preflight"

    def test_emit_phase_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.phase_enter("preflight")
            emitter.phase_exit("preflight", success=True)
            events = emitter.get_recent(10)
            assert len(events) == 2
            assert events[1]["event_type"] == "phase_exit"
            assert events[1]["to_state"] == "success"

    def test_emit_phase_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.phase_enter("backend")
            emitter.phase_fail("backend", error="TimeoutError", evidence={"elapsed_s": 300})
            events = emitter.get_recent(10)
            assert events[1]["event_type"] == "phase_fail"
            assert events[1]["evidence"]["elapsed_s"] == 300

    def test_emit_boot_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            events = emitter.get_recent(10)
            assert events[0]["event_type"] == "boot_start"

    def test_events_persisted_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            emitter = self._make_emitter(tmp_path)
            emitter.boot_start()
            emitter.phase_enter("clean_slate")
            emitter.flush()
            files = list((tmp_path / "lifecycle").glob("*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text().strip().split("\n")
            assert len(lines) == 2
            for line in lines:
                record = json.loads(line)
                assert "envelope" in record
                assert record["envelope"]["repo"] == "jarvis"

    def test_causality_chain_maintained(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            emitter.phase_enter("clean_slate")
            events = emitter.get_recent(10)
            boot_event = events[0]
            phase_event = events[1]
            # Phase enter should be caused by boot start
            assert phase_event["envelope"]["caused_by_event_id"] == boot_event["envelope"]["event_id"]

    def test_boot_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            emitter.boot_complete()
            events = emitter.get_recent(10)
            assert events[1]["event_type"] == "boot_complete"

    def test_shutdown_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            emitter.shutdown_start(reason="user_requested")
            events = emitter.get_recent(10)
            assert events[1]["event_type"] == "shutdown_start"
            assert events[1]["reason"] == "user_requested"

    def test_recovery_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            emitter.recovery_start("cloud_sql", "connection_lost")
            emitter.recovery_complete("cloud_sql", "reconnected")
            events = emitter.get_recent(10)
            assert events[1]["event_type"] == "recovery_start"
            assert events[1]["component"] == "cloud_sql"
            assert events[2]["event_type"] == "recovery_complete"
            assert events[2]["component"] == "cloud_sql"

    def test_recovery_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            emitter.recovery_fail("prime_client", "max_retries_exceeded")
            events = emitter.get_recent(10)
            assert events[1]["event_type"] == "recovery_fail"
            assert events[1]["error"] == "max_retries_exceeded"

    def test_recovery_start_with_explicit_cause(self):
        """recovery_start can link to a specific failure event."""
        with tempfile.TemporaryDirectory() as tmp:
            emitter = self._make_emitter(Path(tmp))
            emitter.boot_start()
            fail_event = emitter.phase_fail("backend", error="timeout")
            recovery = emitter.recovery_start(
                "backend", "auto_recovery",
                caused_by_event_id=fail_event["envelope"]["event_id"],
            )
            assert recovery["envelope"]["caused_by_event_id"] == fail_event["envelope"]["event_id"]

    def test_flush_only_writes_pending_events(self):
        """flush() should not duplicate events on repeated calls."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            emitter = self._make_emitter(tmp_path)
            emitter.boot_start()
            emitter.flush()
            emitter.flush()  # Second flush should write nothing
            files = list((tmp_path / "lifecycle").glob("*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text().strip().split("\n")
            assert len(lines) == 1  # Only one event, not duplicated

    def test_close_cancels_timer_and_flushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            emitter = self._make_emitter(tmp_path, auto_flush_interval=10)
            emitter.boot_start()
            emitter.close()
            # After close, events should be flushed
            files = list((tmp_path / "lifecycle").glob("*.jsonl"))
            assert len(files) == 1

    def test_phase_transitions_auto_flush(self):
        """phase_enter and phase_exit trigger auto-flush."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            emitter = self._make_emitter(tmp_path)
            emitter.boot_start()
            emitter.phase_enter("preflight")
            # phase_enter triggers flush, so boot_start + phase_enter should be on disk
            files = list((tmp_path / "lifecycle").glob("*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text().strip().split("\n")
            assert len(lines) == 2


if __name__ == "__main__":
    unittest.main()
