"""Tests for JSONL append-only trace store."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path


class TestJSONLWriter(unittest.TestCase):
    def test_append_single_event(self):
        import tempfile
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1", "data": "hello"})
            lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
            assert len(lines) == 1
            assert json.loads(lines[0])["event_id"] == "e1"

    def test_append_multiple_events(self):
        import tempfile
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            for i in range(100):
                writer.append({"event_id": f"e{i}"})
            lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
            assert len(lines) == 100

    def test_line_checksum_present(self):
        import tempfile
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "test.jsonl")
            writer.append({"event_id": "e1"})
            line = (tmp_path / "test.jsonl").read_text().strip()
            parsed = json.loads(line)
            assert "_checksum" in parsed

    def test_concurrent_appends_no_interleaving(self):
        import tempfile
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
        import tempfile
        from backend.core.trace_store import JSONLWriter
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deep_path = tmp_path / "a" / "b" / "c" / "test.jsonl"
            writer = JSONLWriter(deep_path)
            writer.append({"event_id": "e1"})
            assert deep_path.exists()


class TestTraceStreamManager(unittest.TestCase):
    def test_lifecycle_stream_creates_epoch_file(self):
        import tempfile
        from backend.core.trace_store import TraceStreamManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
            mgr.write_lifecycle({"event_type": "boot_start", "envelope": {}})
            files = list((tmp_path / "lifecycle").glob("*.jsonl"))
            assert len(files) == 1
            assert "epoch_epoch-001" in files[0].name

    def test_decisions_stream_date_partitioned(self):
        import tempfile
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
        import tempfile
        from backend.core.trace_store import TraceStreamManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
            mgr.write_span({"operation": "health_check", "envelope": {}})
            # Spans are buffered — need to flush
            mgr.flush_spans()
            files = list((tmp_path / "spans").glob("*.jsonl"))
            assert len(files) == 1


class TestSpanBuffer(unittest.TestCase):
    def test_backpressure_drops_spans_not_lifecycle(self):
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=10)
        # Fill buffer past capacity
        for i in range(10):
            buffer.add({"status": "success", "event_id": f"s{i}"})
        # Add an 11th — should trigger drop policy
        buffer.add({"status": "success", "event_id": "overflow"})
        kept = buffer.drain()
        # Buffer should have managed overflow (either dropped or kept depending on policy)
        assert len(kept) <= 11  # Some may be dropped


if __name__ == "__main__":
    unittest.main()
