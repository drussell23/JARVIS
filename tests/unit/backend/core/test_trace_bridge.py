"""Tests for tracing.py <-> CorrelationContext bridge.

Validates that ``unified_trace`` correctly activates both tracing systems,
restores context on exit (normal and exception), and nests properly.
"""
from __future__ import annotations

import unittest

from backend.core.resilience.correlation_context import (
    CorrelationContext,
    get_current_context,
    set_current_context,
    _current_context,
)
from backend.core.tracing import get_tracer, Tracer, _current_span


class TestSyncToTracer(unittest.TestCase):
    """Tests for sync_to_tracer()."""

    def setUp(self):
        set_current_context(None)
        # Reset the tracer's current span
        _current_span.set(None)

    def tearDown(self):
        set_current_context(None)
        _current_span.set(None)

    def test_sync_with_active_root_span(self):
        from backend.core.trace_bridge import sync_to_tracer

        ctx = CorrelationContext.create(
            operation="test-sync-root",
            source_component="bridge_test",
        )
        span = sync_to_tracer(ctx)
        # The tracer is available in this codebase, so we expect a Span back
        self.assertIsNotNone(span)
        self.assertEqual(span.context.operation_name, "test-sync-root")
        # Clean up the span (it was not entered as context manager)
        span._finish()

    def test_sync_with_current_span(self):
        from backend.core.trace_bridge import sync_to_tracer

        ctx = CorrelationContext.create(
            operation="root-op",
            source_component="bridge_test",
        )
        # Create a child span that becomes current_span
        child = ctx.start_span("child-op")
        span = sync_to_tracer(ctx)
        self.assertIsNotNone(span)
        # Should use current_span.operation (child-op), not root_span
        self.assertEqual(span.context.operation_name, "child-op")
        span._finish()

    def test_sync_with_no_operation(self):
        from backend.core.trace_bridge import sync_to_tracer

        # Context with no operation -> no root_span, no current_span
        ctx = CorrelationContext.create(source_component="bridge_test")
        result = sync_to_tracer(ctx)
        self.assertIsNone(result)

    def test_sync_with_none_context(self):
        from backend.core.trace_bridge import sync_to_tracer

        # Passing a random object with no span attributes
        result = sync_to_tracer(object())
        self.assertIsNone(result)


class TestSyncFromTracer(unittest.TestCase):
    """Tests for sync_from_tracer()."""

    def setUp(self):
        set_current_context(None)
        _current_span.set(None)

    def tearDown(self):
        set_current_context(None)
        _current_span.set(None)

    def test_sync_when_no_active_span(self):
        from backend.core.trace_bridge import sync_from_tracer

        result = sync_from_tracer()
        self.assertIsNone(result)

    def test_sync_with_active_tracer_span(self):
        from backend.core.trace_bridge import sync_from_tracer

        tracer = get_tracer()
        span = tracer.start_span("tracer-operation")
        span.__enter__()
        try:
            result = sync_from_tracer()
            self.assertIsNotNone(result)
            self.assertIsInstance(result, CorrelationContext)
            self.assertEqual(result.source_component, "tracer_bridge")
            # The root span operation should mirror the tracer's span name
            self.assertIsNotNone(result.root_span)
            self.assertEqual(result.root_span.operation, "tracer-operation")
        finally:
            span.__exit__(None, None, None)


class TestUnifiedTrace(unittest.TestCase):
    """Tests for the unified_trace() context manager."""

    def setUp(self):
        set_current_context(None)
        _current_span.set(None)

    def tearDown(self):
        set_current_context(None)
        _current_span.set(None)

    def test_basic_context_creation(self):
        """unified_trace should create a CorrelationContext and make it current."""
        from backend.core.trace_bridge import unified_trace

        with unified_trace("test-basic", component="bridge") as ctx:
            self.assertIsNotNone(ctx)
            self.assertIsInstance(ctx, CorrelationContext)
            self.assertEqual(ctx.source_component, "bridge")
            # Should be the current context
            current = get_current_context()
            self.assertIs(current, ctx)

    def test_tracer_span_active_inside(self):
        """unified_trace should also create a tracer span."""
        from backend.core.trace_bridge import unified_trace

        with unified_trace("test-tracer-span", component="bridge"):
            tracer = get_tracer()
            span = tracer.get_current_span()
            self.assertIsNotNone(span)
            self.assertEqual(span.context.operation_name, "test-tracer-span")

    def test_restores_context_on_exit(self):
        """After exiting unified_trace, the previous context should be restored."""
        from backend.core.trace_bridge import unified_trace

        self.assertIsNone(get_current_context())

        with unified_trace("scoped", component="test"):
            self.assertIsNotNone(get_current_context())

        self.assertIsNone(get_current_context())

    def test_restores_tracer_span_on_exit(self):
        """After exiting unified_trace, the tracer's current span should be restored."""
        from backend.core.trace_bridge import unified_trace

        self.assertIsNone(_current_span.get())

        with unified_trace("scoped", component="test"):
            self.assertIsNotNone(_current_span.get())

        self.assertIsNone(_current_span.get())

    def test_handles_exception(self):
        """unified_trace should restore context even when an exception occurs."""
        from backend.core.trace_bridge import unified_trace

        with self.assertRaises(ValueError):
            with unified_trace("error-test", component="test") as ctx:
                raise ValueError("test error")

        # Context should be restored
        self.assertIsNone(get_current_context())
        # Tracer span should be restored
        self.assertIsNone(_current_span.get())

    def test_exception_marks_span_error(self):
        """When an exception occurs, the correlation span should be marked as error."""
        from backend.core.trace_bridge import unified_trace

        captured_ctx = None
        with self.assertRaises(RuntimeError):
            with unified_trace("fail-op", component="test") as ctx:
                captured_ctx = ctx
                raise RuntimeError("boom")

        self.assertIsNotNone(captured_ctx)
        self.assertIsNotNone(captured_ctx.root_span)
        self.assertEqual(captured_ctx.root_span.status, "error")
        self.assertEqual(captured_ctx.root_span.error_message, "boom")

    def test_success_marks_span_ok(self):
        """On normal exit, the correlation span should be marked success."""
        from backend.core.trace_bridge import unified_trace

        captured_ctx = None
        with unified_trace("ok-op", component="test") as ctx:
            captured_ctx = ctx

        self.assertIsNotNone(captured_ctx)
        self.assertIsNotNone(captured_ctx.root_span)
        self.assertEqual(captured_ctx.root_span.status, "success")
        self.assertIsNotNone(captured_ctx.root_span.end_time)

    def test_nests_correctly(self):
        """Nested unified_trace calls should create parent-child relationships."""
        from backend.core.trace_bridge import unified_trace

        with unified_trace("outer", component="parent") as outer_ctx:
            outer_id = outer_ctx.correlation_id

            with unified_trace("inner", component="child") as inner_ctx:
                self.assertIsNotNone(inner_ctx)
                # Inner should have outer as parent
                self.assertEqual(inner_ctx.parent_id, outer_id)
                # Current context should be inner
                self.assertIs(get_current_context(), inner_ctx)

            # After inner exits, outer should be restored
            current = get_current_context()
            self.assertIs(current, outer_ctx)
            self.assertEqual(current.correlation_id, outer_id)

        # After both exit, context should be None
        self.assertIsNone(get_current_context())

    def test_nested_tracer_spans(self):
        """Nested unified_trace calls should create nested tracer spans."""
        from backend.core.trace_bridge import unified_trace

        with unified_trace("outer-span", component="parent"):
            outer_span = _current_span.get()
            self.assertIsNotNone(outer_span)
            outer_trace_id = outer_span.context.trace_id

            with unified_trace("inner-span", component="child"):
                inner_span = _current_span.get()
                self.assertIsNotNone(inner_span)
                # Should share the same trace_id (child of outer)
                self.assertEqual(inner_span.context.trace_id, outer_trace_id)
                # Parent span ID should be outer's span ID
                self.assertEqual(
                    inner_span.context.parent_span_id,
                    outer_span.context.span_id,
                )

            # After inner exits, outer span should be restored
            self.assertIs(_current_span.get(), outer_span)

    def test_preserves_existing_context(self):
        """unified_trace should correctly restore a pre-existing non-None context."""
        from backend.core.trace_bridge import unified_trace

        pre_existing = CorrelationContext.create(
            operation="pre-existing",
            source_component="setup",
        )
        set_current_context(pre_existing)

        with unified_trace("scoped", component="test") as ctx:
            # Should be a child of pre_existing
            self.assertEqual(ctx.parent_id, pre_existing.correlation_id)

        # Should restore the pre-existing context
        restored = get_current_context()
        self.assertIs(restored, pre_existing)

        # Clean up
        set_current_context(None)

    def test_component_is_optional(self):
        """unified_trace with empty component should still work."""
        from backend.core.trace_bridge import unified_trace

        with unified_trace("no-component") as ctx:
            self.assertIsNotNone(ctx)
            # Empty string component => stored as None
            self.assertIsNone(ctx.source_component)


if __name__ == "__main__":
    unittest.main()
