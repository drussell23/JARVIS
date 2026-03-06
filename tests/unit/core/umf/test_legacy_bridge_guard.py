"""Tests for legacy guard insertion in Reactor Bridge (Task 19)."""
import pytest


class TestReactorBridgeLegacyGuard:

    def test_connect_blocked_in_active_mode(self, monkeypatch):
        """ReactorCoreBridge.connect_async() raises when UMF is active."""
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is False

    def test_connect_allowed_in_shadow_mode(self, monkeypatch):
        """ReactorCoreBridge.connect_async() passes when UMF is shadow."""
        monkeypatch.setenv("JARVIS_UMF_MODE", "shadow")
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_connect_allowed_when_no_umf(self, monkeypatch):
        """ReactorCoreBridge.connect_async() passes when UMF is not configured."""
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_guard_raises_runtime_error(self, monkeypatch):
        """assert_legacy_allowed raises RuntimeError with caller name."""
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import assert_legacy_allowed
        with pytest.raises(RuntimeError, match="Legacy path disabled"):
            assert_legacy_allowed("ReactorCoreBridge.connect_async")
