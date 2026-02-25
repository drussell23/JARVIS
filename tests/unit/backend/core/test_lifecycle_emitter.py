"""Tests for lifecycle event emission."""
import json
import time
import unittest
from pathlib import Path
import tempfile


class TestLifecycleEmitter(unittest.TestCase):
    def _make_emitter(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        return LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)

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


if __name__ == "__main__":
    unittest.main()
