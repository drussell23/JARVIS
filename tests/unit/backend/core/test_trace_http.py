"""Tests for HTTP trace header injection."""
import unittest


class TestTraceHttp(unittest.TestCase):
    def setUp(self):
        from backend.core.resilience.correlation_context import set_current_context
        set_current_context(None)

    def tearDown(self):
        from backend.core.resilience.correlation_context import set_current_context
        set_current_context(None)

    def test_returns_empty_without_context(self):
        from backend.core.trace_http import get_trace_headers
        headers = get_trace_headers()
        assert headers == {}

    def test_returns_correlation_headers_with_context(self):
        from backend.core.trace_http import get_trace_headers
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="test-request",
            source_component="prime_client",
        )
        set_current_context(ctx)
        headers = get_trace_headers()
        assert "X-Correlation-ID" in headers
        assert headers["X-Source-Repo"] == "jarvis"
        assert headers["X-Source-Component"] == "prime_client"

    def test_includes_envelope_headers_when_available(self):
        from backend.core.trace_http import get_trace_headers
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="test-op", source_component="test",
        )
        set_current_context(ctx)
        headers = get_trace_headers()
        # Envelope headers are added by CorrelationContext.to_headers()
        # if TraceEnvelope is available
        if ctx.envelope is not None:
            assert "X-Trace-ID" in headers
            assert "X-Trace-Span-ID" in headers

    def test_merge_with_existing_headers(self):
        from backend.core.trace_http import merge_trace_headers
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="test-merge", source_component="test",
        )
        set_current_context(ctx)
        existing = {"Content-Type": "application/json", "User-Agent": "test"}
        merged = merge_trace_headers(existing)
        assert merged["Content-Type"] == "application/json"
        assert "X-Correlation-ID" in merged

    def test_merge_does_not_overwrite_existing(self):
        from backend.core.trace_http import merge_trace_headers
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="test-overwrite", source_component="test",
        )
        set_current_context(ctx)
        existing = {"X-Correlation-ID": "my-custom-id"}
        merged = merge_trace_headers(existing)
        assert merged["X-Correlation-ID"] == "my-custom-id"  # NOT overwritten

    def test_extract_from_response_headers(self):
        from backend.core.trace_http import extract_trace_from_response
        from backend.core.resilience.correlation_context import CorrelationContext
        ctx = CorrelationContext.create(
            operation="outgoing", source_component="test",
        )
        # Simulate response headers from a server that echoes correlation
        response_headers = ctx.to_headers()
        extracted = extract_trace_from_response(response_headers)
        assert extracted is not None
        assert extracted.correlation_id == ctx.correlation_id

    def test_extract_returns_none_without_headers(self):
        from backend.core.trace_http import extract_trace_from_response
        result = extract_trace_from_response({})
        assert result is None


if __name__ == "__main__":
    unittest.main()
