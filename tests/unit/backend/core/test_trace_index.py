"""Tests for SQLite trace index (rebuildable cache)."""
import tempfile
import time
import unittest
from pathlib import Path


class TestTraceIndex(unittest.TestCase):
    def test_index_event(self):
        from backend.core.trace_store import TraceIndex

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            idx = TraceIndex(tmp_path / "index" / "trace_index.sqlite")
            idx.index_event(
                trace_id="t1",
                event_id="e1",
                stream="lifecycle",
                file_path="lifecycle/20260224.jsonl",
                byte_offset=0,
                ts_wall_utc=time.time(),
                operation="phase_enter",
                status="success",
            )
            results = idx.query_by_trace("t1")
            assert len(results) == 1
            assert results[0]["event_id"] == "e1"
            idx.close()

    def test_query_by_time_range(self):
        from backend.core.trace_store import TraceIndex

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            idx = TraceIndex(tmp_path / "index" / "trace_index.sqlite")
            now = time.time()
            idx.index_event(
                "t1", "e1", "lifecycle", "f.jsonl", 0, now - 100, "op1", "success"
            )
            idx.index_event(
                "t1", "e2", "lifecycle", "f.jsonl", 50, now - 50, "op2", "success"
            )
            idx.index_event(
                "t1", "e3", "lifecycle", "f.jsonl", 100, now, "op3", "success"
            )
            results = idx.query_by_time(since=now - 75, until=now - 25)
            assert len(results) == 1
            assert results[0]["event_id"] == "e2"
            idx.close()


class TestCausalityIndex(unittest.TestCase):
    def test_add_and_query_edge(self):
        from backend.core.trace_store import CausalityIndex

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            idx = CausalityIndex(tmp_path / "index" / "causality_edges.sqlite")
            idx.add_edge(
                event_id="e2",
                caused_by_event_id="e1",
                parent_span_id="s1",
                trace_id="t1",
                operation="recovery",
                ts_wall_utc=time.time(),
            )
            children = idx.get_children("e1")
            assert len(children) == 1
            assert children[0]["event_id"] == "e2"
            idx.close()

    def test_detect_cycle(self):
        from backend.core.trace_store import CausalityIndex

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            idx = CausalityIndex(tmp_path / "index" / "causality_edges.sqlite")
            idx.add_edge("e1", None, None, "t1", "boot", time.time())
            idx.add_edge("e2", "e1", "s1", "t1", "phase", time.time())
            idx.add_edge("e3", "e2", "s2", "t1", "recovery", time.time())
            cycles = idx.detect_cycles()
            assert len(cycles) == 0
            idx.close()

    def test_detect_self_cycle(self):
        from backend.core.trace_store import CausalityIndex

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            idx = CausalityIndex(tmp_path / "index" / "causality_edges.sqlite")
            idx.add_edge("e1", "e1", None, "t1", "broken", time.time())
            cycles = idx.detect_cycles()
            assert len(cycles) > 0
            idx.close()

    def test_rebuild_from_jsonl(self):
        from backend.core.trace_store import JSONLWriter, TraceIndex

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = JSONLWriter(tmp_path / "lifecycle" / "test.jsonl")
            writer.append(
                {
                    "envelope": {
                        "trace_id": "t1",
                        "event_id": "e1",
                        "ts_wall_utc": time.time(),
                    },
                    "event_type": "boot_start",
                }
            )
            writer.append(
                {
                    "envelope": {
                        "trace_id": "t1",
                        "event_id": "e2",
                        "ts_wall_utc": time.time(),
                    },
                    "event_type": "phase_enter",
                }
            )
            idx = TraceIndex(tmp_path / "index" / "trace_index.sqlite")
            rebuilt = idx.rebuild_from_directory(
                tmp_path / "lifecycle", stream="lifecycle"
            )
            assert rebuilt == 2
            results = idx.query_by_trace("t1")
            assert len(results) == 2
            idx.close()


if __name__ == "__main__":
    unittest.main()
