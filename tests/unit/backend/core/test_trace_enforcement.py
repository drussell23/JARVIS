"""Tests for boundary enforcement middleware."""
import asyncio
import unittest


class TestEnforcementDecorator(unittest.TestCase):
    def setUp(self):
        from backend.core.trace_enforcement import set_enforcement_mode, EnforcementMode
        # Reset to permissive before each test
        set_enforcement_mode(EnforcementMode.PERMISSIVE)

    def test_strict_rejects_missing_envelope(self):
        async def _run():
            from backend.core.trace_enforcement import enforce_trace, EnforcementMode, set_enforcement_mode
            from backend.core.resilience.correlation_context import set_current_context
            set_enforcement_mode(EnforcementMode.STRICT)
            set_current_context(None)

            @enforce_trace(boundary_type="internal", classification="critical")
            async def my_phase():
                return "ok"

            from backend.core.trace_enforcement import TraceEnforcementError
            with self.assertRaises(TraceEnforcementError):
                await my_phase()
        asyncio.get_event_loop().run_until_complete(_run())

    def test_strict_allows_valid_envelope(self):
        async def _run():
            from backend.core.trace_enforcement import enforce_trace, EnforcementMode, set_enforcement_mode
            from backend.core.resilience.correlation_context import CorrelationContext, set_current_context
            set_enforcement_mode(EnforcementMode.STRICT)

            ctx = CorrelationContext.create(operation="test")
            set_current_context(ctx)
            try:
                @enforce_trace(boundary_type="internal", classification="critical")
                async def my_phase():
                    return "ok"

                result = await my_phase()
                assert result == "ok"
            finally:
                set_current_context(None)
        asyncio.get_event_loop().run_until_complete(_run())

    def test_permissive_allows_missing_with_warning(self):
        async def _run():
            from backend.core.trace_enforcement import (
                enforce_trace, EnforcementMode, set_enforcement_mode, get_violation_count,
            )
            from backend.core.resilience.correlation_context import set_current_context
            set_enforcement_mode(EnforcementMode.PERMISSIVE)
            set_current_context(None)
            initial_count = get_violation_count()

            @enforce_trace(boundary_type="internal", classification="standard")
            async def my_handler():
                return "ok"

            result = await my_handler()
            assert result == "ok"
            assert get_violation_count() > initial_count
        asyncio.get_event_loop().run_until_complete(_run())

    def test_canary_allows_but_alerts(self):
        async def _run():
            from backend.core.trace_enforcement import (
                enforce_trace, EnforcementMode, set_enforcement_mode, get_violation_count,
            )
            from backend.core.resilience.correlation_context import set_current_context
            set_enforcement_mode(EnforcementMode.CANARY)
            set_current_context(None)
            initial_count = get_violation_count()

            @enforce_trace(boundary_type="http", classification="standard")
            async def my_request():
                return "ok"

            result = await my_request()
            assert result == "ok"
            assert get_violation_count() > initial_count
        asyncio.get_event_loop().run_until_complete(_run())


class TestComplianceScore(unittest.TestCase):
    def test_score_calculation(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("startup_phase", classification="critical")
        tracker.register_boundary("health_check", classification="standard")
        tracker.mark_instrumented("startup_phase")
        score = tracker.get_score()
        assert score["critical_instrumented"] == 1
        assert score["critical_total"] == 1
        assert score["score_critical"] == 100.0
        assert score["total_boundaries"] == 2
        assert score["instrumented"] == 1
        assert score["score_overall"] == 50.0


class TestHTTPEnvelopeInjection(unittest.TestCase):
    def test_inject_headers(self):
        from backend.core.trace_enforcement import inject_trace_headers
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        env = factory.create_root(component="test", operation="test")
        headers = {}
        inject_trace_headers(headers, env)
        assert "X-Trace-ID" in headers
        assert headers["X-Trace-ID"] == env.trace_id

    def test_extract_headers(self):
        from backend.core.trace_enforcement import inject_trace_headers, extract_trace_from_headers
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(
            repo="jarvis", boot_id="b1", runtime_epoch_id="e1",
            node_id="n1", producer_version="v1"
        )
        original = factory.create_root(component="test", operation="test")
        headers = {}
        inject_trace_headers(headers, original)
        restored = extract_trace_from_headers(headers)
        assert restored is not None
        assert restored.trace_id == original.trace_id


if __name__ == "__main__":
    unittest.main()
