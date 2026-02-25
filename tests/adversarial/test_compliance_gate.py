"""Tests for CI compliance gate."""
import json
import unittest


class TestComplianceGate(unittest.TestCase):
    def test_fails_when_critical_below_100(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("phase_transition", "critical")
        tracker.register_boundary("health_check", "standard")
        # Don't instrument critical boundary
        tracker.mark_instrumented("health_check")
        score = tracker.get_score()
        assert score["score_critical"] < 100.0
        assert tracker.ci_gate_passes() is False

    def test_passes_when_all_critical_instrumented(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("phase_transition", "critical")
        tracker.register_boundary("health_check", "standard")
        tracker.mark_instrumented("phase_transition")
        tracker.mark_instrumented("health_check")
        assert tracker.ci_gate_passes() is True

    def test_fails_when_overall_below_threshold(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        # 1 critical (instrumented) + 9 standard (not instrumented) = 10% overall
        tracker.register_boundary("critical_one", "critical")
        tracker.mark_instrumented("critical_one")
        for i in range(9):
            tracker.register_boundary(f"standard_{i}", "standard")
        assert tracker.ci_gate_passes() is False  # 10% < 80%

    def test_to_json_includes_gate_status(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("phase_transition", "critical")
        tracker.mark_instrumented("phase_transition")
        result = json.loads(tracker.to_json())
        assert "ci_gate_passes" in result
        assert result["ci_gate_passes"] is True


if __name__ == "__main__":
    unittest.main()
