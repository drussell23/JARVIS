"""Tests for span recording around existing circuit breakers and operations."""
import asyncio
import unittest
import tempfile
from pathlib import Path


class TestSpanRecorder(unittest.TestCase):
    def _make_recorder(self, tmp_path, buffer_max=256):
        from backend.core.span_recorder import SpanRecorder
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        return SpanRecorder(
            trace_dir=tmp_path,
            envelope_factory=factory,
            buffer_max=buffer_max,
        )

    def test_record_success_span(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                recorder = self._make_recorder(Path(tmp))
                async with recorder.span("health_check", component="prime_client") as span:
                    await asyncio.sleep(0.01)
                assert span["status"] == "success"
                assert span["duration_ms"] > 0
        asyncio.get_event_loop().run_until_complete(_run())

    def test_record_error_span(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                recorder = self._make_recorder(Path(tmp))
                with self.assertRaises(ValueError):
                    async with recorder.span("inference", component="model_serving") as span:
                        raise ValueError("model not loaded")
                recent = recorder.get_recent(10)
                assert len(recent) == 1
                assert recent[0]["status"] == "error"
                assert recent[0]["error_class"] == "ValueError"
        asyncio.get_event_loop().run_until_complete(_run())

    def test_flush_writes_to_spans_stream(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                recorder = self._make_recorder(tmp_path)
                async with recorder.span("test_op", component="test"):
                    pass
                recorder.flush()
                files = list((tmp_path / "spans").glob("*.jsonl"))
                assert len(files) == 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_idempotency_key_spans_never_dropped(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                recorder = self._make_recorder(tmp_path, buffer_max=5)
                # Fill buffer with normal spans
                for i in range(10):
                    async with recorder.span("normal", component="test"):
                        pass
                # Add one with idempotency key
                async with recorder.span("vm_create", component="gcp", idempotency_key="create:vm-1:n1"):
                    pass
                # Flush — idempotency span must survive
                recorder.flush()
                files = list((tmp_path / "spans").glob("*.jsonl"))
                content = files[0].read_text()
                assert "create:vm-1:n1" in content
        asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    unittest.main()
