"""Tests for UMF legacy guard flag (Task 18)."""
import pytest


class TestLegacyGuard:

    def test_legacy_enabled_by_default_when_no_umf(self, monkeypatch):
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_legacy_disabled_in_active_mode(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is False

    def test_legacy_enabled_in_shadow_mode(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "shadow")
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.setenv("JARVIS_UMF_LEGACY_ENABLED", "true")
        from backend.core.umf.legacy_guard import is_legacy_enabled
        assert is_legacy_enabled() is True

    def test_guard_check_raises_when_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import assert_legacy_allowed
        with pytest.raises(RuntimeError, match="Legacy path disabled"):
            assert_legacy_allowed("test-caller")

    def test_guard_check_passes_when_enabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        monkeypatch.delenv("JARVIS_UMF_LEGACY_ENABLED", raising=False)
        from backend.core.umf.legacy_guard import assert_legacy_allowed
        assert_legacy_allowed("test-caller")  # Should not raise
