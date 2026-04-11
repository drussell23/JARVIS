"""Unit tests for the sandbox path fallback helper (Phase 1.1)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.sandbox_paths import (
    reset_cache,
    sandbox_fallback,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_cache()
    yield
    reset_cache()


def test_writable_path_returns_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "ops" / "today.log"
    result = sandbox_fallback(target)
    assert result == target


def test_non_writable_path_routes_to_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_root = tmp_path / "fallback"
    monkeypatch.setenv("JARVIS_SANDBOX_FALLBACK_ROOT", str(fallback_root))

    # Pick an unambiguously non-writable location.
    blocked = Path("/System/Library/jarvis_test_blocked/dir/file.log")
    result = sandbox_fallback(blocked)

    assert result != blocked
    assert str(result).startswith(str(fallback_root))
    # Structure is preserved.
    assert result.name == "file.log"


def test_cache_returns_identical_object_and_emits_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fallback_root = tmp_path / "fb"
    monkeypatch.setenv("JARVIS_SANDBOX_FALLBACK_ROOT", str(fallback_root))
    caplog.set_level("WARNING", logger="backend.core.ouroboros.governance.sandbox_paths")

    blocked = Path("/System/Library/jarvis_test_blocked/bus.log")
    r1 = sandbox_fallback(blocked)
    r2 = sandbox_fallback(blocked)
    r3 = sandbox_fallback(blocked)
    assert r1 == r2 == r3

    warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "Redirecting" in rec.getMessage()
    ]
    assert len(warnings) == 1, f"expected 1 warning, got {len(warnings)}"


def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_SANDBOX_FALLBACK_DISABLED", "true")
    reset_cache()  # re-evaluate env
    blocked = Path("/System/Library/jarvis_test_blocked/bus.log")
    result = sandbox_fallback(blocked)
    # When disabled, returns the primary unchanged (no fallback).
    assert result == blocked


def test_relative_to_home_jarvis_mirrors_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_root = tmp_path / "fb"
    monkeypatch.setenv("JARVIS_SANDBOX_FALLBACK_ROOT", str(fallback_root))

    # A path that looks like ~/.jarvis/ops/2026.log but is actually blocked
    with patch(
        "backend.core.ouroboros.governance.sandbox_paths._is_writable",
        return_value=False,
    ):
        primary = Path.home() / ".jarvis" / "ops" / "2026-04-11.log"
        result = sandbox_fallback(primary)
        assert result == fallback_root / "ops" / "2026-04-11.log"


def test_parent_directory_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_root = tmp_path / "fb"
    monkeypatch.setenv("JARVIS_SANDBOX_FALLBACK_ROOT", str(fallback_root))

    with patch(
        "backend.core.ouroboros.governance.sandbox_paths._is_writable",
        return_value=False,
    ):
        primary = Path.home() / ".jarvis" / "deep" / "nested" / "file.log"
        result = sandbox_fallback(primary)
        assert result.parent.exists()
