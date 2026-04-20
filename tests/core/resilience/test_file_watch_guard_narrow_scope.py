"""FileWatchGuard narrow-scope scheduling spine.

Pins the fix for the 2026-04-20 PollingObserver-at-scale failure. The
TodoScanner graduation arc surfaced that on this macOS host
``PollingObserver`` delivers zero events when scheduled against the
repo root — 56K ``.py`` files (mostly under ``venv/``, ``.venv/``,
``backend/venv/``) exceed its O(N) per-tick snapshot budget. The fix
is to narrow the scheduled paths so the snapshot skips venv noise.

The tests here cover:
  * ``_resolve_excluded_dirs``: config default, env override, env clear.
  * ``_resolve_watch_paths``: skip excluded depth-1, descend into
    depth-2 when a nested excluded dir is present, missing-root safety,
    file-at-root ignore.
  * End-to-end: real FileWatchGuard + real tmpdir + real PollingObserver
    + file creation ⇒ event delivery.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.resilience.file_watch_guard import (
    FileEvent,
    FileWatchConfig,
    FileWatchGuard,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mkrepo(tmp_path: Path, layout: dict) -> Path:
    """Build a throwaway repo layout under tmp_path.

    ``layout`` maps directory-name → None (create empty dir) or nested dict.
    """
    for name, contents in layout.items():
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, dict):
            _mkrepo(d, contents)
    return tmp_path


def _guard(watch_dir: Path, config: FileWatchConfig = None) -> FileWatchGuard:
    """Construct a guard without starting it — just enough for the
    path-resolution helpers to run against a real directory tree."""
    config = config or FileWatchConfig(patterns=["*.py"])
    return FileWatchGuard(
        watch_dir=watch_dir,
        on_event=lambda ev: None,
        config=config,
    )


# ---------------------------------------------------------------------------
# _resolve_excluded_dirs
# ---------------------------------------------------------------------------

def test_resolve_excluded_dirs_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    """No env override → returns the config's frozenset."""
    monkeypatch.delenv("JARVIS_FILE_WATCH_EXCLUDE_DIRS", raising=False)
    guard = _guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    assert "venv" in excluded
    assert ".venv" in excluded
    assert "node_modules" in excluded
    assert ".git" in excluded


def test_resolve_excluded_dirs_env_override(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """Env override replaces the default set entirely."""
    monkeypatch.setenv(
        "JARVIS_FILE_WATCH_EXCLUDE_DIRS",
        "venv, custom_noise , foo",
    )
    guard = _guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    assert excluded == frozenset({"venv", "custom_noise", "foo"})
    # Config default should NOT be merged — env wins.
    assert ".git" not in excluded


def test_resolve_excluded_dirs_empty_env_falls_back(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """Blank/whitespace env value falls back to config default."""
    monkeypatch.setenv("JARVIS_FILE_WATCH_EXCLUDE_DIRS", "   ")
    guard = _guard(tmp_path)
    excluded = guard._resolve_excluded_dirs()
    # Default still present
    assert "venv" in excluded


# ---------------------------------------------------------------------------
# _resolve_watch_paths
# ---------------------------------------------------------------------------

def test_resolve_watch_paths_skips_excluded_depth1(tmp_path: Path) -> None:
    """Top-level venv/.venv are dropped; normal dirs kept."""
    _mkrepo(tmp_path, {"backend": {}, "tests": {}, "venv": {}, ".venv": {}})
    guard = _guard(tmp_path)
    paths = guard._resolve_watch_paths(guard._resolve_excluded_dirs())
    names = {p.name for p, _r in paths}
    assert names == {"backend", "tests"}


def test_resolve_watch_paths_descends_on_nested_excluded(tmp_path: Path) -> None:
    """backend/venv present → backend/ replaced by its non-venv children."""
    _mkrepo(tmp_path, {
        "backend": {
            "core": {"sub": {}},
            "vision": {},
            "venv": {"bin": {}},  # nested venv — must be skipped
        },
        "tests": {},
    })
    guard = _guard(tmp_path)
    paths = guard._resolve_watch_paths(guard._resolve_excluded_dirs())
    name_to_recursive = {
        str(p.relative_to(tmp_path)): r for p, r in paths
    }
    # backend IS scheduled but non-recursively (so file-level events at
    # backend/ depth still fire without dragging backend/venv into the
    # snapshot); backend/core + backend/vision scheduled recursively.
    assert name_to_recursive.get("backend") is False, (
        "backend must be scheduled non-recursively when nested venv present"
    )
    assert name_to_recursive.get("backend/core") is True
    assert name_to_recursive.get("backend/vision") is True
    assert "backend/venv" not in name_to_recursive
    assert name_to_recursive.get("tests") is True


def test_resolve_watch_paths_ignores_files_at_root(tmp_path: Path) -> None:
    """Files (not dirs) at repo root are not scheduled."""
    _mkrepo(tmp_path, {"backend": {}})
    (tmp_path / "README.md").write_text("top-level file")
    (tmp_path / "setup.py").write_text("")
    guard = _guard(tmp_path)
    paths = guard._resolve_watch_paths(guard._resolve_excluded_dirs())
    names = {p.name for p, _r in paths}
    assert names == {"backend"}


def test_resolve_watch_paths_missing_root_returns_empty(tmp_path: Path) -> None:
    """Missing watch_dir → empty list (no crash)."""
    missing = tmp_path / "does_not_exist"
    guard = _guard(missing)
    paths = guard._resolve_watch_paths(guard._resolve_excluded_dirs())
    assert paths == []


def test_resolve_watch_paths_custom_env_narrows_further(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """Operator-supplied env value can narrow scope beyond defaults."""
    _mkrepo(tmp_path, {
        "backend": {}, "tests": {}, "docs": {}, "scripts": {},
    })
    monkeypatch.setenv(
        "JARVIS_FILE_WATCH_EXCLUDE_DIRS",
        "docs,scripts",
    )
    guard = _guard(tmp_path)
    paths = guard._resolve_watch_paths(guard._resolve_excluded_dirs())
    names = {p.name for p, _r in paths}
    assert names == {"backend", "tests"}


# ---------------------------------------------------------------------------
# End-to-end: real PollingObserver + real file event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_narrow_schedule_delivers_events_end_to_end(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """Full stack: FileWatchGuard watches a tmpdir with a venv sibling,
    creates a .py file under an allowed subtree, asserts the event is
    delivered to the ``on_event`` handler.

    This is the regression test for the OS→bus severance bug: before
    the narrow-scope fix, PollingObserver snapshot would miss events in
    a repo with venvs. After the fix, events fire reliably.
    """
    # Force polling on any OS so we exercise the broken path that we fixed.
    monkeypatch.setenv("JARVIS_FILE_WATCH_BACKEND", "polling")

    _mkrepo(tmp_path, {
        "src": {},
        "tests": {},
        "venv": {"bin": {}, "lib": {}},  # noise — must not be snapshotted
    })

    received: List[FileEvent] = []

    async def on_event(ev: FileEvent) -> None:
        received.append(ev)

    cfg = FileWatchConfig(
        patterns=["*.py"],
        debounce_seconds=0.2,
        verify_checksum=False,
        min_stable_seconds=0.0,
    )
    guard = FileWatchGuard(watch_dir=tmp_path, on_event=on_event, config=cfg)
    assert await guard.start() is True
    try:
        # Confirm the narrow-scope logic actually ran.
        scheduled = getattr(guard, "_scheduled_paths", [])
        scheduled_names = {p.name for p, _r in scheduled}
        assert "venv" not in scheduled_names, (
            f"venv must not be scheduled; got {scheduled_names}"
        )
        assert "src" in scheduled_names or "tests" in scheduled_names

        # Settle so the observer's first snapshot completes.
        await asyncio.sleep(1.5)

        target = tmp_path / "src" / "widget.py"
        target.write_text("# created by narrow-scope regression test\n")

        # Poll up to 8s for delivery
        for _ in range(40):
            await asyncio.sleep(0.2)
            if any(ev.path.name == "widget.py" for ev in received):
                break

        names = {ev.path.name for ev in received}
        assert "widget.py" in names, (
            f"expected widget.py event; received: {names}"
        )
    finally:
        await guard.stop()


@pytest.mark.asyncio
async def test_scheduled_paths_persisted_for_restart_replay(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The health-check loop restarts the observer on crash; to make that
    transparent, ``_scheduled_paths`` must be populated so the restart
    replays the narrow schedule rather than the broken full-root one."""
    monkeypatch.setenv("JARVIS_FILE_WATCH_BACKEND", "polling")
    _mkrepo(tmp_path, {"src": {}, "venv": {}})

    cfg = FileWatchConfig(patterns=["*.py"])
    guard = FileWatchGuard(
        watch_dir=tmp_path, on_event=lambda ev: None, config=cfg,
    )
    await guard.start()
    try:
        assert hasattr(guard, "_scheduled_paths")
        names = {p.name for p, _r in guard._scheduled_paths}
        assert "src" in names
        assert "venv" not in names
    finally:
        await guard.stop()
