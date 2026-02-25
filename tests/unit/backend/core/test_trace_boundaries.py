"""Tests for trace boundary registration."""
import unittest


class TestTraceBoundaries(unittest.TestCase):
    def test_register_boundary(self):
        from backend.core.trace_boundaries import BoundaryRegistry
        registry = BoundaryRegistry()
        registry.register("prime_client.execute_request", "http", "critical")
        boundaries = registry.list_boundaries()
        names = [b["name"] for b in boundaries]
        assert "prime_client.execute_request" in names

    def test_register_multiple_boundaries(self):
        from backend.core.trace_boundaries import BoundaryRegistry
        registry = BoundaryRegistry()
        registry.register("prime_client.execute_request", "http", "critical")
        registry.register("gcp_vm_manager.create_vm", "subprocess", "critical")
        registry.register("decision_log.record", "internal", "standard")
        boundaries = registry.list_boundaries()
        assert len(boundaries) == 3

    def test_compliance_integration(self):
        from backend.core.trace_boundaries import BoundaryRegistry
        from backend.core.trace_enforcement import ComplianceTracker
        registry = BoundaryRegistry()
        registry.register("a", "http", "critical")
        registry.register("b", "internal", "standard")

        tracker = ComplianceTracker()
        registry.populate_tracker(tracker)

        score = tracker.get_score()
        assert score["total_boundaries"] == 2
        assert score["critical_total"] == 1
        assert score["instrumented"] == 0

    def test_mark_instrumented(self):
        from backend.core.trace_boundaries import BoundaryRegistry
        from backend.core.trace_enforcement import ComplianceTracker
        registry = BoundaryRegistry()
        registry.register("a", "http", "critical")
        registry.register("b", "internal", "standard")

        tracker = ComplianceTracker()
        registry.populate_tracker(tracker)
        tracker.mark_instrumented("a")

        score = tracker.get_score()
        assert score["instrumented"] == 1
        assert score["critical_instrumented"] == 1

    def test_default_registry_has_known_boundaries(self):
        from backend.core.trace_boundaries import get_default_registry
        registry = get_default_registry()
        boundaries = registry.list_boundaries()
        assert len(boundaries) > 0
        names = [b["name"] for b in boundaries]
        assert "prime_client.execute_request" in names
        assert "gcp_vm_manager.create_vm" in names

    def test_default_registry_singleton(self):
        from backend.core.trace_boundaries import get_default_registry
        r1 = get_default_registry()
        r2 = get_default_registry()
        assert r1 is r2

    def test_boundary_type_preserved(self):
        from backend.core.trace_boundaries import BoundaryRegistry
        registry = BoundaryRegistry()
        registry.register("test.func", "http", "critical")
        boundaries = registry.list_boundaries()
        assert boundaries[0]["boundary_type"] == "http"
        assert boundaries[0]["classification"] == "critical"


if __name__ == "__main__":
    unittest.main()
