"""Tests for UMF wiring into unified_supervisor (Task 13)."""
import os
import pytest


class TestCreateUmfEngine:

    def test_returns_none_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JARVIS_UMF_MODE", raising=False)
        from unified_supervisor import create_umf_engine
        result = create_umf_engine(dedup_db_path=tmp_path / "dedup.db")
        assert result is None

    def test_returns_engine_when_shadow(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_UMF_MODE", "shadow")
        from unified_supervisor import create_umf_engine
        engine = create_umf_engine(dedup_db_path=tmp_path / "dedup.db")
        assert engine is not None

    def test_returns_engine_when_active(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_UMF_MODE", "active")
        from unified_supervisor import create_umf_engine
        engine = create_umf_engine(dedup_db_path=tmp_path / "dedup.db")
        assert engine is not None
