"""Tests for GCP VM trace metadata injection."""
import unittest


class TestTraceVm(unittest.TestCase):
    def setUp(self):
        from backend.core.resilience.correlation_context import set_current_context
        set_current_context(None)

    def tearDown(self):
        from backend.core.resilience.correlation_context import set_current_context
        set_current_context(None)

    def test_returns_empty_without_context(self):
        from backend.core.trace_vm import get_trace_metadata_items
        items = get_trace_metadata_items()
        assert items == []

    def test_returns_metadata_with_context(self):
        from backend.core.trace_vm import get_trace_metadata_items
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="vm-create",
            source_component="gcp_vm_manager",
        )
        set_current_context(ctx)
        items = get_trace_metadata_items()
        keys = [item["key"] for item in items]
        assert "jarvis-correlation-id" in keys
        assert "jarvis-source-repo" in keys
        assert "jarvis-source-component" in keys

    def test_includes_envelope_trace_id(self):
        from backend.core.trace_vm import get_trace_metadata_items
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="vm-create", source_component="gcp",
        )
        set_current_context(ctx)
        items = get_trace_metadata_items()
        item_dict = {i["key"]: i["value"] for i in items}
        if ctx.envelope is not None:
            assert "jarvis-trace-id" in item_dict
            assert "jarvis-parent-span-id" in item_dict

    def test_env_var_dict_generation(self):
        from backend.core.trace_vm import get_trace_env_vars
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context,
        )
        ctx = CorrelationContext.create(
            operation="vm-create", source_component="gcp",
        )
        set_current_context(ctx)
        env_vars = get_trace_env_vars()
        assert "JARVIS_PARENT_CORRELATION_ID" in env_vars
        assert env_vars["JARVIS_PARENT_CORRELATION_ID"] == ctx.correlation_id

    def test_env_vars_empty_without_context(self):
        from backend.core.trace_vm import get_trace_env_vars
        env_vars = get_trace_env_vars()
        assert env_vars == {}


if __name__ == "__main__":
    unittest.main()
