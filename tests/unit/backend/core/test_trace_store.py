"""Tests for JSONL append-only trace store."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path


class TestJSONLWriter(unittest.TestCase):
    def test_append_single_event(self):
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1", "data": "hello"})
            lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
            assert len(lines) == 1
            assert json.loads(lines[0])["event_id"] == "e1"

    def test_append_multiple_events(self):
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            for i in range(100):
                writer.append({"event_id": f"e{i}"})
            lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
            assert len(lines) == 100

    def test_line_checksum_present(self):
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1"})
            line = (tmp_path / "test.jsonl").read_text().strip()
            parsed = json.loads(line)
            assert "_checksum" in parsed

    def test_checksum_verifiable(self):
        """Written checksum can be verified by reading back and recomputing."""
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1", "data": "hello"})
            line = (tmp_path / "test.jsonl").read_text().strip()
            parsed = json.loads(line)
            assert JSONLWriter.verify_checksum(parsed) is True

    def test_checksum_detects_corruption(self):
        """Modifying a field after writing invalidates the checksum."""
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1", "data": "hello"})
            line = (tmp_path / "test.jsonl").read_text().strip()
            parsed = json.loads(line)
            parsed["data"] = "tampered"
            assert JSONLWriter.verify_checksum(parsed) is False

    def test_checksum_strips_preexisting(self):
        """Input record with _checksum field doesn't break checksum computation."""
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1", "_checksum": 99999})
            line = (tmp_path / "test.jsonl").read_text().strip()
            parsed = json.loads(line)
            # Checksum should be valid (the stale _checksum was stripped)
            assert JSONLWriter.verify_checksum(parsed) is True
            # And it should not be the bogus value we passed in
            assert parsed["_checksum"] != 99999

    def test_concurrent_appends_no_interleaving(self):
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            def worker(prefix):
                for i in range(50):
                    writer.append({"event_id": f"{prefix}_{i}", "data": "x" * 200})
            threads = [threading.Thread(target=worker, args=(f"t{t}",)) for t in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
            assert len(lines) == 200
            for line in lines:
                json.loads(line)  # Each line must be valid JSON (no torn writes)

    def test_creates_parent_directories(self):
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deep_path = tmp_path / "a" / "b" / "c" / "test.jsonl"
            writer = JSONLWriter(deep_path)
            writer.append({"event_id": "e1"})
            assert deep_path.exists()


class TestTraceStreamManager(unittest.TestCase):
    def test_lifecycle_stream_creates_epoch_file(self):
        from backend.core.trace_store import TraceStreamManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
            mgr.write_lifecycle({"event_type": "boot_start", "envelope": {}})
            files = list((tmp_path / "lifecycle").glob("*.jsonl"))
            assert len(files) == 1
            assert "epoch_epoch-001" in files[0].name

    def test_decisions_stream_date_partitioned(self):
        from backend.core.trace_store import TraceStreamManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
            mgr.write_decision({"decision_type": "vm_termination", "envelope": {}})
            files = list((tmp_path / "decisions").glob("*.jsonl"))
            assert len(files) == 1
            today = time.strftime("%Y%m%d")
            assert today in files[0].name

    def test_spans_stream_date_partitioned(self):
        from backend.core.trace_store import TraceStreamManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
            mgr.write_span({"operation": "health_check", "envelope": {}})
            # Spans are buffered — need to flush
            mgr.flush_spans()
            files = list((tmp_path / "spans").glob("*.jsonl"))
            assert len(files) == 1

    def test_decision_writer_cached(self):
        """Verify write_decision reuses writer for same date (not creating new each call)."""
        from backend.core.trace_store import TraceStreamManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
            mgr.write_decision({"d": 1})
            mgr.write_decision({"d": 2})
            # Should have exactly one cached writer for today
            assert len(mgr._decision_writers) == 1


class TestSpanBuffer(unittest.TestCase):
    def test_backpressure_drops_success_spans_above_95pct(self):
        """At >95% fill, success spans are dropped."""
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=100)
        # Fill to 96 using errors (bypass backpressure during fill)
        for i in range(96):
            buffer.add({"status": "error", "event_id": f"e{i}"})
        assert buffer.size == 96
        # This success span should be dropped (>95%)
        kept = buffer.add({"status": "success", "event_id": "should_drop"})
        assert kept is False
        assert buffer.drop_count == 1

    def test_backpressure_keeps_errors_above_95pct(self):
        """At >95% fill, error spans are still kept."""
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=100)
        # Fill to 96 using errors (bypass backpressure during fill)
        for i in range(96):
            buffer.add({"status": "error", "event_id": f"e{i}"})
        kept = buffer.add({"status": "error", "event_id": "err_extra"})
        assert kept is True
        assert buffer.size == 97

    def test_backpressure_keeps_idempotency_key_above_95pct(self):
        """At >95% fill, records with idempotency_key are still kept."""
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=100)
        # Fill to 96 using errors (bypass backpressure during fill)
        for i in range(96):
            buffer.add({"status": "error", "event_id": f"e{i}"})
        kept = buffer.add({"status": "success", "idempotency_key": "idem1", "event_id": "k1"})
        assert kept is True

    def test_hard_cap_prevents_unbounded_growth(self):
        """Even error/idempotency records are dropped at 2x max_size."""
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=10)
        # Fill with errors (bypass normal backpressure) to 2x
        for i in range(20):
            buffer.add({"status": "error", "event_id": f"e{i}"})
        assert buffer.size == 20
        # 21st record hits hard cap — even errors dropped
        kept = buffer.add({"status": "error", "event_id": "overflow"})
        assert kept is False
        assert buffer.drop_count >= 1

    def test_sampling_at_80pct_is_deterministic(self):
        """Backpressure sampling uses crc32 (deterministic across processes)."""
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=100)
        for i in range(81):
            buffer.add({"status": "success", "event_id": f"s{i}"})
        # Add 20 more success spans — some should be dropped, some kept
        results = []
        for i in range(20):
            results.append(buffer.add({"status": "success", "event_id": f"sample_{i}"}))
        # With crc32 sampling at 50%, expect roughly half dropped
        dropped = results.count(False)
        kept = results.count(True)
        assert dropped > 0, "Expected some drops at 80% fill"
        assert kept > 0, "Expected some kept at 80% fill (50% sampling)"


if __name__ == "__main__":
    unittest.main()
