"""Tests that CorrelationContext uses TraceEnvelope as backing store."""
import unittest


class TestCorrelationContextEnvelopeIntegration(unittest.TestCase):
    def test_create_attaches_envelope(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        ctx = CorrelationContext.create(operation="test_op", source_component="test")
        assert ctx.envelope is not None
        assert ctx.envelope.operation == "test_op"
        assert ctx.envelope.component == "test"

    def test_child_context_inherits_trace_id(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        parent = CorrelationContext.create(operation="parent_op")
        child = CorrelationContext.create(operation="child_op", parent=parent)
        assert child.envelope.trace_id == parent.envelope.trace_id
        assert child.envelope.parent_span_id == parent.envelope.span_id

    def test_to_headers_includes_envelope(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        ctx = CorrelationContext.create(operation="test")
        headers = ctx.to_headers()
        assert "X-Trace-ID" in headers
        assert "X-Trace-Span-ID" in headers
        assert "X-Trace-Sequence" in headers
        # Backward compat: old correlation headers still present
        assert "X-Correlation-ID" in headers

    def test_from_headers_restores_envelope(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        original = CorrelationContext.create(operation="test")
        headers = original.to_headers()
        restored = CorrelationContext.from_headers(headers)
        assert restored is not None
        assert restored.envelope.trace_id == original.envelope.trace_id

    def test_inject_extract_round_trip(self):
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context, inject_correlation, extract_correlation,
        )
        ctx = CorrelationContext.create(operation="test")
        set_current_context(ctx)
        try:
            data = {}
            data = inject_correlation(data)
            # Envelope is embedded inside _correlation via to_dict()
            assert "_correlation" in data
            assert "_trace_envelope" in data["_correlation"]
            extracted = extract_correlation(data)
            assert extracted is not None
            assert extracted.envelope.trace_id == ctx.envelope.trace_id
        finally:
            set_current_context(None)

    def test_backward_compat_without_envelope_headers(self):
        """Existing callers that only send X-Correlation-ID still work."""
        from backend.core.resilience.correlation_context import CorrelationContext
        old_headers = {"X-Correlation-ID": "old-style-123", "X-Source-Repo": "jarvis"}
        ctx = CorrelationContext.from_headers(old_headers)
        assert ctx is not None
        assert ctx.correlation_id == "old-style-123"
        # Envelope should be auto-generated for backward compat
        assert ctx.envelope is not None

    def test_existing_correlation_api_unchanged(self):
        """Verify the existing API still works exactly as before."""
        from backend.core.resilience.correlation_context import CorrelationContext
        ctx = CorrelationContext.create(operation="test", source_component="comp")
        # Old fields still work
        assert ctx.correlation_id is not None
        assert ctx.source_repo == "jarvis"
        assert ctx.source_component == "comp"
        # Old serialization still works
        d = ctx.to_dict()
        assert "correlation_id" in d
        restored = CorrelationContext.from_dict(d)
        assert restored.correlation_id == ctx.correlation_id


if __name__ == "__main__":
    unittest.main()
