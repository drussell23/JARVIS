"""Integration test: full trace flow from boot to HTTP to VM.

Verifies the entire traceability pipeline works end-to-end:
1. TraceBootstrap initializes the subsystem
2. Boot lifecycle events are emitted with causal chains
3. Phase enter/exit events carry causal links
4. Context propagates through create_traced_task()
5. HTTP headers contain trace context
6. VM metadata items contain trace context
7. Compliance tracker reports correct coverage
8. Lifecycle events persist to JSONL on disk
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path


class TestFullTraceFlow(unittest.TestCase):
    def setUp(self):
        from backend.core.trace_bootstrap import _reset
        _reset()
        from backend.core.resilience.correlation_context import set_current_context
        set_current_context(None)

    def tearDown(self):
        from backend.core.trace_bootstrap import shutdown
        shutdown()
        from backend.core.resilience.correlation_context import set_current_context
        set_current_context(None)

    def test_full_boot_lifecycle(self):
        """Boot -> phase_enter -> phase_exit -> boot_complete with causal chain."""
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import (
            on_boot_start, on_phase_enter, on_phase_exit, on_boot_complete,
        )

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="int-boot", runtime_epoch_id="int-epoch")
            on_boot_start()
            on_phase_enter("resources", 35)
            on_phase_exit("resources", 52, success=True)
            on_phase_enter("backend", 52)
            on_phase_exit("backend", 65, success=True)
            on_boot_complete()

            emitter = get_lifecycle_emitter()
            events = emitter.get_recent(20)

            types = [e["event_type"] for e in events]
            assert types == [
                "boot_start",
                "phase_enter", "phase_exit",
                "phase_enter", "phase_exit",
                "boot_complete",
            ], f"Got types: {types}"

            # Verify causal chain: each event's caused_by_event_id
            # should reference the previous event's event_id
            for i in range(1, len(events)):
                prev_id = events[i - 1]["envelope"]["event_id"]
                curr_caused_by = events[i]["envelope"].get("caused_by_event_id")
                assert curr_caused_by == prev_id, (
                    f"Event {i} ({events[i]['event_type']}) caused_by={curr_caused_by} "
                    f"doesn't match prev event_id={prev_id}"
                )

    def test_context_propagation_through_task(self):
        """CorrelationContext propagates through create_traced_task."""
        from backend.core.context_task import create_traced_task
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context, get_current_context,
        )

        results = []

        async def child_task():
            ctx = get_current_context()
            results.append(ctx.correlation_id if ctx else None)

        async def parent():
            ctx = CorrelationContext.create(
                operation="integration-test",
                source_component="test_suite",
            )
            set_current_context(ctx)
            task = create_traced_task(child_task(), name="propagation-test")
            await task
            return ctx.correlation_id

        parent_id = asyncio.run(parent())
        assert results[0] == parent_id

    def test_http_headers_carry_trace(self):
        """HTTP trace headers include correlation and envelope IDs."""
        from backend.core.trace_http import get_trace_headers
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )

        ctx = CorrelationContext.create(
            operation="http-test",
            source_component="prime_client",
        )
        set_current_context(ctx)
        try:
            headers = get_trace_headers()
            assert headers["X-Correlation-ID"] == ctx.correlation_id
            assert headers["X-Source-Repo"] == "jarvis"
            if ctx.envelope:
                assert "X-Trace-ID" in headers
        finally:
            set_current_context(None)

    def test_vm_metadata_carries_trace(self):
        """VM metadata items include trace context."""
        from backend.core.trace_vm import get_trace_metadata_items
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )

        ctx = CorrelationContext.create(
            operation="vm-test",
            source_component="gcp_vm_manager",
        )
        set_current_context(ctx)
        try:
            items = get_trace_metadata_items()
            item_dict = {i["key"]: i["value"] for i in items}
            assert item_dict["jarvis-correlation-id"] == ctx.correlation_id
            if ctx.envelope:
                assert "jarvis-trace-id" in item_dict
        finally:
            set_current_context(None)

    def test_compliance_score(self):
        """Compliance tracker reports correct coverage."""
        from backend.core.trace_boundaries import get_default_registry
        from backend.core.trace_enforcement import ComplianceTracker

        registry = get_default_registry()
        tracker = ComplianceTracker()
        registry.populate_tracker(tracker)

        score = tracker.get_score()
        assert score["total_boundaries"] > 0
        assert score["critical_total"] > 0
        assert score["score_overall"] == 0.0  # Nothing instrumented yet

        # Mark some as instrumented
        tracker.mark_instrumented("prime_client.execute_request")
        tracker.mark_instrumented("prime_client.execute_stream_request")
        score = tracker.get_score()
        assert score["instrumented"] == 2

    def test_lifecycle_events_persisted_to_jsonl(self):
        """Lifecycle events are flushed to JSONL files on disk."""
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        from backend.core.trace_hooks import on_boot_start, on_phase_enter

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            initialize(trace_dir=tmp_path, boot_id="persist-test", runtime_epoch_id="persist-epoch")

            on_boot_start()
            on_phase_enter("resources", 35)

            emitter = get_lifecycle_emitter()
            emitter.flush()

            # Check that lifecycle JSONL files exist
            lifecycle_dir = tmp_path / "lifecycle"
            jsonl_files = list(lifecycle_dir.glob("*.jsonl"))
            assert len(jsonl_files) > 0, f"No JSONL files found in {lifecycle_dir}"

            # Parse and validate
            events = []
            for f in jsonl_files:
                for line in f.read_text().strip().split("\n"):
                    if line:
                        events.append(json.loads(line))

            types = [e["event_type"] for e in events]
            assert "boot_start" in types
            assert "phase_enter" in types

    def test_bridge_with_bootstrap(self):
        """unified_trace works alongside TraceBootstrap."""
        from backend.core.trace_bootstrap import initialize
        from backend.core.trace_bridge import unified_trace
        from backend.core.resilience.correlation_context import get_current_context

        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="bridge-test", runtime_epoch_id="bridge-epoch")

            with unified_trace("test-operation", component="integration"):
                ctx = get_current_context()
                assert ctx is not None
                assert ctx.source_component == "integration"
                # Envelope should be available since bootstrap initialized
                assert ctx.envelope is not None

            # Context restored after exit
            assert get_current_context() is None


if __name__ == "__main__":
    unittest.main()
