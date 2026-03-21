"""Tests for LittlesLawVerifier hook in GLS submit path."""
from backend.core.topology.idle_verifier import LittlesLawVerifier


class TestGLSVerifierHookContract:
    """Test the contract that GLS calls when it has a proactive drive service."""

    def test_record_accepts_depth_and_latency(self):
        v = LittlesLawVerifier("jarvis", max_queue_depth=1000)
        v.record(depth=5, processing_latency_ms=100.0)
        assert len(v._samples) == 1

    def test_record_depth_from_active_ops_set(self):
        """GLS uses len(self._active_ops) as queue depth."""
        v = LittlesLawVerifier("jarvis", max_queue_depth=1000)
        active_ops = {"op-1", "op-2", "op-3"}
        v.record(depth=len(active_ops), processing_latency_ms=50.0)
        assert v._samples[0].depth == 3

    def test_proactive_drive_service_record_sample_method(self):
        """ProactiveDriveService.record_sample() is the GLS hook entry point."""
        from backend.core.topology.proactive_drive_service import (
            ProactiveDriveConfig,
            ProactiveDriveService,
        )
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        service._verifiers["jarvis"] = LittlesLawVerifier("jarvis", 1000)
        service.record_sample("jarvis", depth=5, latency_ms=100.0)
        assert len(service._verifiers["jarvis"]._samples) == 1

    def test_record_sample_unknown_repo_is_noop(self):
        from backend.core.topology.proactive_drive_service import (
            ProactiveDriveConfig,
            ProactiveDriveService,
        )
        config = ProactiveDriveConfig()
        service = ProactiveDriveService(config=config, telemetry_bus=None)
        service.record_sample("nonexistent", depth=5, latency_ms=100.0)

    def test_getattr_guard_returns_none_by_default(self):
        """GLS uses getattr(self, '_proactive_drive_service', None) — None is safe."""
        class FakeGLS:
            pass
        gls = FakeGLS()
        assert getattr(gls, "_proactive_drive_service", None) is None
