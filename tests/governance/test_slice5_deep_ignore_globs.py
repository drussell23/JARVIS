# tests/governance/test_slice5_deep_ignore_globs.py
"""Slice 5 T1 — deep-glob ignore semantics (Run #15 autopsy L2 fuel, F3)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.resilience.file_watch_guard import (
    FileEvent,
    FileEventType,
    FileWatchConfig,
    FileWatchGuard,
)


def _guard(tmp_path: Path, ignore: list) -> FileWatchGuard:
    cfg = FileWatchConfig(patterns=["*.py", "*.json"], ignore_patterns=ignore)
    return FileWatchGuard(watch_dir=tmp_path, on_event=lambda e: None, config=cfg)


def _ev(p: Path) -> FileEvent:
    return FileEvent(event_type=FileEventType.MODIFIED, path=p, checksum="x")


class TestDeepGlobIgnore:
    def test_slash_pattern_drops_nested_worktree_file(self, tmp_path):
        g = _guard(tmp_path, ["*/.worktrees/*"])
        victim = tmp_path / ".worktrees" / "unit-abc" / "tests" / "test_x.py"
        assert g._should_process(_ev(victim)) is False

    def test_slash_pattern_drops_deeply_nested(self, tmp_path):
        g = _guard(tmp_path, ["*/__pycache__/*"])
        victim = tmp_path / "a" / "b" / "__pycache__" / "c" / "d.py"
        assert g._should_process(_ev(victim)) is False

    def test_source_file_still_processed(self, tmp_path):
        g = _guard(tmp_path, ["*/.worktrees/*", "*/__pycache__/*"])
        keeper = tmp_path / "backend" / "core" / "leaf_predicates.py"
        assert g._should_process(_ev(keeper)) is True

    def test_basename_patterns_unchanged(self, tmp_path):
        g = _guard(tmp_path, ["*.tmp"])
        assert g._should_process(_ev(tmp_path / "x" / "y.tmp")) is False
        assert g._should_process(_ev(tmp_path / "x" / "y.py")) is True


class TestBridgeEnvGlobs:
    def test_default_globs_include_worktrees_deep(self, monkeypatch):
        monkeypatch.delenv("JARVIS_FS_BRIDGE_IGNORE_GLOBS", raising=False)
        from backend.core.ouroboros.governance.intake import fs_event_bridge as m
        globs = m._ignore_globs_from_env()
        assert "*/.worktrees/*" in globs and "*/__pycache__/*" in globs

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FS_BRIDGE_IGNORE_GLOBS", "*/foo/*, */bar/*")
        from backend.core.ouroboros.governance.intake import fs_event_bridge as m
        assert m._ignore_globs_from_env() == ["*/foo/*", "*/bar/*"]

    def test_empty_env_means_legacy_only(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FS_BRIDGE_IGNORE_GLOBS", "")
        from backend.core.ouroboros.governance.intake import fs_event_bridge as m
        assert m._ignore_globs_from_env() == []
