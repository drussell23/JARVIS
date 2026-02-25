"""Tests for DecisionLog persistence and envelope integration."""
import json
import tempfile
import time
import unittest
from pathlib import Path


class TestDecisionRecordEnvelope(unittest.TestCase):
    def test_record_accepts_envelope(self):
        from backend.core.decision_log import DecisionLog
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        env = factory.create_root(component="gcp_vm_manager", operation="terminate_vm")
        log = DecisionLog(max_entries=100)
        rec = log.record(
            decision_type="vm_termination",
            reason="cost exceeded",
            inputs={"cost": 5.0},
            outcome="terminated",
            component="gcp_vm_manager",
            envelope=env,
        )
        assert rec.envelope is not None
        assert rec.envelope.trace_id == env.trace_id

    def test_record_works_without_envelope(self):
        """Backward compat: envelope is optional."""
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=100)
        rec = log.record(
            decision_type="vm_termination",
            reason="cost exceeded",
            inputs={"cost": 5.0},
            outcome="terminated",
        )
        assert rec.envelope is None

    def test_to_dict_includes_envelope(self):
        from backend.core.decision_log import DecisionLog
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        env = factory.create_root(component="test", operation="test")
        log = DecisionLog(max_entries=100)
        rec = log.record(
            decision_type="test", reason="test",
            inputs={}, outcome="test", envelope=env,
        )
        d = rec.to_dict()
        assert "envelope" in d
        assert d["envelope"]["trace_id"] == env.trace_id


class TestDecisionLogFlusher(unittest.TestCase):
    def test_flush_writes_jsonl(self):
        from backend.core.decision_log import DecisionLog
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = DecisionLog(max_entries=100)
            log.record(decision_type="test", reason="r", inputs={}, outcome="o")
            log.record(decision_type="test", reason="r2", inputs={}, outcome="o2")
            flushed = log.flush_to_jsonl(tmp_path / "decisions")
            assert flushed == 2
            files = list((tmp_path / "decisions").glob("*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text().strip().split("\n")
            assert len(lines) == 2

    def test_flush_is_incremental(self):
        from backend.core.decision_log import DecisionLog
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log = DecisionLog(max_entries=100)
            log.record(decision_type="test", reason="r1", inputs={}, outcome="o1")
            log.flush_to_jsonl(tmp_path / "decisions")
            log.record(decision_type="test", reason="r2", inputs={}, outcome="o2")
            log.flush_to_jsonl(tmp_path / "decisions")
            files = list((tmp_path / "decisions").glob("*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text().strip().split("\n")
            assert len(lines) == 2  # Both records, not duplicated

    def test_flush_does_not_lose_records_on_error(self):
        """If flush fails, records stay in memory for retry."""
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=100)
        log.record(decision_type="test", reason="r1", inputs={}, outcome="o1")
        # Flush to invalid path (should not raise, should return 0)
        flushed = log.flush_to_jsonl("/dev/null/nonexistent/path")
        assert flushed == 0
        # Records still in memory
        assert log.size == 1


if __name__ == "__main__":
    unittest.main()
