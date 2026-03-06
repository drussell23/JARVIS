"""Tests for UMF Heartbeat Projection wiring into unified_supervisor (Task 14)."""
import pytest


class TestCreateHeartbeatProjection:

    def test_returns_none_when_umf_disabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        from unified_supervisor import create_heartbeat_projection
        result = create_heartbeat_projection()
        assert result is None

    def test_returns_projection_when_shadow(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "shadow")
        from unified_supervisor import create_heartbeat_projection
        projection = create_heartbeat_projection()
        assert projection is not None

    def test_returns_projection_when_active(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        from unified_supervisor import create_heartbeat_projection
        projection = create_heartbeat_projection()
        assert projection is not None

    def test_projection_is_correct_type(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        from unified_supervisor import create_heartbeat_projection
        from backend.core.umf.heartbeat_projection import HeartbeatProjection
        projection = create_heartbeat_projection()
        assert isinstance(projection, HeartbeatProjection)

    def test_custom_stale_timeout(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        from unified_supervisor import create_heartbeat_projection
        projection = create_heartbeat_projection(stale_timeout_s=60.0)
        assert projection._stale_timeout_s == 60.0
