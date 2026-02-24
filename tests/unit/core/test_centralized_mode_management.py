"""Tests for v270.1 centralized startup mode management functions.

Validates that _mode_severity, _compute_ideal_mode, _apply_mode_degradation,
_reevaluate_mode_at_boundary, and _check_spawn_admission all behave correctly
and maintain the same semantics as the 6 duplicate inline implementations
they replaced.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset mode-related env vars before each test."""
    for key in (
        "JARVIS_STARTUP_MEMORY_MODE",
        "JARVIS_STARTUP_EFFECTIVE_MODE",
        "JARVIS_STARTUP_DESIRED_MODE",
        "JARVIS_STARTUP_COMPLETE",
        "JARVIS_OOMBRIDGE_AVAILABLE",
        "JARVIS_MEASURED_AVAILABLE_GB",
        "JARVIS_CRITICAL_THRESHOLD_GB",
        "JARVIS_CLOUD_THRESHOLD_GB",
        "JARVIS_OPTIMIZE_THRESHOLD_GB",
        "JARVIS_PLANNED_ML_GB",
        "JARVIS_CAPABILITY_DOCKER",
        "JARVIS_CAPABILITY_LOCAL_STORAGE",
        "JARVIS_BACKEND_MINIMAL",
    ):
        monkeypatch.delenv(key, raising=False)


def _import_functions():
    """Import the module-level functions under test."""
    from unified_supervisor import (
        _mode_severity,
        _compute_ideal_mode,
        _apply_mode_degradation,
        _reevaluate_mode_at_boundary,
        _check_spawn_admission,
        _MODE_SEVERITY,
    )
    return (
        _mode_severity,
        _compute_ideal_mode,
        _apply_mode_degradation,
        _reevaluate_mode_at_boundary,
        _check_spawn_admission,
        _MODE_SEVERITY,
    )


class TestModeSeverity:
    """Test _mode_severity returns correct severity levels."""

    def test_severity_ordering(self):
        sev, *_ = _import_functions()
        assert sev("local_full") == 0
        assert sev("local_optimized") == 1
        assert sev("sequential") == 2
        assert sev("cloud_first") == 3
        assert sev("cloud_only") == 4
        assert sev("minimal") == 5

    def test_monotonic_ordering(self):
        sev, *_ = _import_functions()
        modes = ["local_full", "local_optimized", "sequential",
                 "cloud_first", "cloud_only", "minimal"]
        severities = [sev(m) for m in modes]
        assert severities == sorted(severities)

    def test_normalization(self):
        sev, *_ = _import_functions()
        assert sev("  Local_Full  ") == 0
        assert sev("CLOUD_FIRST") == 3
        assert sev(None) == 0
        assert sev("") == 0

    def test_unknown_mode_defaults_to_zero(self):
        sev, *_ = _import_functions()
        assert sev("nonexistent") == 0


class TestComputeIdealMode:
    """Test _compute_ideal_mode threshold logic."""

    def test_abundant_memory_returns_local_full(self, monkeypatch):
        _, compute, *_ = _import_functions()
        # 12GB available, predicted = 12 - 4.6 = 7.4GB > 4.0
        assert compute(12.0) == "local_full"

    def test_moderate_memory_returns_local_optimized(self, monkeypatch):
        _, compute, *_ = _import_functions()
        # 8GB available, predicted = 8 - 4.6 = 3.4GB < 4.0 but >= 2.0
        assert compute(8.0) == "local_optimized"

    def test_low_memory_returns_cloud_first(self, monkeypatch):
        _, compute, *_ = _import_functions()
        # 5GB available (< 6.0 cloud threshold)
        assert compute(5.0) == "cloud_first"

    def test_critical_memory_returns_cloud_only(self, monkeypatch):
        _, compute, *_ = _import_functions()
        # 1.5GB available (< 2.0 critical threshold)
        assert compute(1.5) == "cloud_only"

    def test_oombridge_unavailable_clamps_cloud_to_sequential(self, monkeypatch):
        _, compute, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_OOMBRIDGE_AVAILABLE", "0")
        # 5GB → would be cloud_first, but OOMBridge unavailable → sequential
        assert compute(5.0) == "sequential"
        # 1.5GB → would be cloud_only, but OOMBridge unavailable → sequential
        assert compute(1.5) == "sequential"

    def test_custom_thresholds(self, monkeypatch):
        _, compute, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_CRITICAL_THRESHOLD_GB", "3.0")
        monkeypatch.setenv("JARVIS_CLOUD_THRESHOLD_GB", "8.0")
        # 2.5GB < 3.0 critical → cloud_only
        assert compute(2.5) == "cloud_only"


class TestApplyModeDegradation:
    """Test _apply_mode_degradation monotonic behavior."""

    def test_degrades_from_lower_to_higher_severity(self, monkeypatch):
        _, _, apply_deg, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        result = apply_deg("cloud_first", "test_reason")
        assert result == "cloud_first"
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "cloud_first"

    def test_refuses_recovery_during_startup(self, monkeypatch):
        _, _, apply_deg, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "cloud_first")
        # Try to "upgrade" from cloud_first(3) to local_full(0) — should be refused
        result = apply_deg("local_full", "test_reason")
        assert result == "cloud_first"
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "cloud_first"

    def test_same_mode_is_noop(self, monkeypatch):
        _, _, apply_deg, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "sequential")
        result = apply_deg("sequential", "test_reason")
        assert result == "sequential"

    def test_multi_step_degradation(self, monkeypatch):
        _, _, apply_deg, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        apply_deg("local_optimized", "step1")
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "local_optimized"
        apply_deg("cloud_first", "step2")
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "cloud_first"
        # Can't go back
        apply_deg("sequential", "step3_backwards")
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "cloud_first"


class TestReevaluateModeAtBoundary:
    """Test _reevaluate_mode_at_boundary phase-boundary reevaluation."""

    def test_returns_current_mode_when_psutil_unavailable(self, monkeypatch):
        _, _, _, reeval, *_ = _import_functions()
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "sequential")
        # Mock _read_available_memory_gb to return None
        import unified_supervisor
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: None)
        mode, avail = reeval("test_phase")
        assert mode == "sequential"
        assert avail == 0.0

    def test_degrades_when_memory_drops(self, monkeypatch):
        _, _, _, reeval, *_ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        # Simulate 1.5GB available → should degrade to cloud_only
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 1.5)
        mode, avail = reeval("test_phase")
        assert mode == "cloud_only"
        assert avail == 1.5
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "cloud_only"
        assert os.environ["JARVIS_STARTUP_EFFECTIVE_MODE"] == "cloud_only"

    def test_no_change_when_memory_adequate(self, monkeypatch):
        _, _, _, reeval, *_ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        # 12GB → local_full, no change
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 12.0)
        mode, avail = reeval("test_phase")
        assert mode == "local_full"
        assert avail == 12.0
        assert os.environ["JARVIS_STARTUP_EFFECTIVE_MODE"] == "local_full"

    def test_monotonic_during_startup(self, monkeypatch):
        _, _, _, reeval, *_ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "cloud_first")
        # 12GB available would suggest local_full, but can't recover during startup
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 12.0)
        mode, avail = reeval("test_phase")
        assert mode == "cloud_first"  # Not upgraded to local_full

    def test_recovery_after_startup_complete(self, monkeypatch):
        _, _, _, reeval, *_ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "cloud_first")
        monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "true")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 12.0)
        mode, avail = reeval("test_phase")
        assert mode == "local_full"  # CAN recover after startup

    def test_sets_measured_available_gb(self, monkeypatch):
        _, _, _, reeval, *_ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 7.42)
        reeval("test_phase")
        assert os.environ["JARVIS_MEASURED_AVAILABLE_GB"] == "7.42"


class TestCheckSpawnAdmission:
    """Test _check_spawn_admission pre-spawn memory gate."""

    def test_admitted_with_sufficient_memory(self, monkeypatch):
        *_, check, _ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 8.0)
        admitted, reason = check("backend", min_gb=1.5)
        assert admitted is True

    def test_rejected_in_minimal_mode(self, monkeypatch):
        *_, check, _ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "minimal")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 8.0)
        admitted, reason = check("backend", min_gb=1.5)
        assert admitted is False
        assert "minimal" in reason

    def test_rejected_when_below_floor(self, monkeypatch):
        *_, check, _ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 1.0)
        admitted, reason = check("backend", min_gb=1.5)
        assert admitted is False
        assert "1.0GB" in reason

    def test_admitted_when_memory_unknown(self, monkeypatch):
        *_, check, _ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: None)
        admitted, reason = check("backend", min_gb=1.5)
        assert admitted is True  # Can't measure → fail open

    def test_rejection_degrades_mode_env_var(self, monkeypatch):
        """Verify side-effect: rejection triggers _apply_mode_degradation."""
        *_, check, _ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 1.0)
        check("backend", min_gb=1.5)
        # Mode should have been degraded (1.0GB < 2.0 critical → cloud_only)
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] != "local_full"

    def test_rejection_uses_compute_ideal_mode_for_target(self, monkeypatch):
        """Below critical threshold should degrade to cloud_only, not cloud_first."""
        *_, check, _ = _import_functions()
        import unified_supervisor
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setattr(unified_supervisor, "_read_available_memory_gb", lambda: 1.0)
        check("backend", min_gb=1.5)
        # 1.0GB < 2.0 critical → ideal is cloud_only
        assert os.environ["JARVIS_STARTUP_MEMORY_MODE"] == "cloud_only"


class TestComputeIdealModeEdgeCases:
    """Edge case tests for _compute_ideal_mode."""

    def test_zero_available_memory(self):
        _, compute, *_ = _import_functions()
        assert compute(0.0) == "cloud_only"

    def test_negative_available_memory(self):
        _, compute, *_ = _import_functions()
        # Should still return cloud_only (most degraded local recommendation)
        assert compute(-1.0) == "cloud_only"


class TestModeConstantConsistency:
    """Verify _MODE_SEVERITY matches the old inline dicts exactly."""

    def test_all_six_modes_present(self):
        *_, severity_dict = _import_functions()
        expected_modes = {"local_full", "local_optimized", "sequential",
                         "cloud_first", "cloud_only", "minimal"}
        assert set(severity_dict.keys()) == expected_modes

    def test_exact_values(self):
        *_, severity_dict = _import_functions()
        assert severity_dict == {
            "local_full": 0, "local_optimized": 1, "sequential": 2,
            "cloud_first": 3, "cloud_only": 4, "minimal": 5,
        }
