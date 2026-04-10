"""Tests for LiveWorkSensor.

Covers the three signal layers (git dirty, recent mtime, IDE lock
files), the cache, and the env-gate. All tests use real subprocess
``git`` so the sensor's production path is exercised.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from backend.core.ouroboros.governance.live_work_sensor import (
    LiveWorkSensor,
    is_enabled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    """Initialise an empty git repo with a single baseline commit."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "baseline.txt"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=repo,
        check=True,
        env=env,
    )


def _make_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    (tmp_path / "backend").mkdir()
    file_path = tmp_path / "backend" / "main.py"
    file_path.write_text("print('hello')\n", encoding="utf-8")
    # Make the file old enough that the mtime window won't fire.
    old_ts = time.time() - 3600
    os.utime(file_path, (old_ts, old_ts))
    # Commit it so git status is clean.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(
        ["git", "add", "backend/main.py"], cwd=tmp_path, check=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add main"],
        cwd=tmp_path,
        check=True,
        env=env,
    )
    # Reset mtime after commit (git touches it).
    os.utime(file_path, (old_ts, old_ts))
    return tmp_path


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------


def test_is_enabled_default_true() -> None:
    assert is_enabled() is True


def test_disabled_via_env_short_circuits(tmp_path: Path, monkeypatch) -> None:
    import importlib
    import backend.core.ouroboros.governance.live_work_sensor as lws

    monkeypatch.setenv("JARVIS_LIVE_WORK_SENSOR_ENABLED", "false")
    importlib.reload(lws)
    try:
        repo = _make_repo(tmp_path)
        # Dirty the file so we know a "real" signal exists.
        (repo / "backend" / "main.py").write_text("modified\n", encoding="utf-8")
        sensor = lws.LiveWorkSensor(repo)
        active, reason = sensor.is_human_active("backend/main.py")
        assert active is False
        assert reason is None
        assert sensor.get_active_files() == set()
    finally:
        monkeypatch.delenv("JARVIS_LIVE_WORK_SENSOR_ENABLED", raising=False)
        importlib.reload(lws)


# ---------------------------------------------------------------------------
# Git dirty signal
# ---------------------------------------------------------------------------


def test_clean_file_is_not_active(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is False
    assert reason is None


def test_unstaged_change_marks_file_active(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "backend" / "main.py").write_text("print('modified')\n", encoding="utf-8")
    # Move mtime backwards so only the git signal can fire.
    old_ts = time.time() - 3600
    os.utime(repo / "backend" / "main.py", (old_ts, old_ts))
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is True
    assert reason is not None
    assert "git status" in reason


def test_staged_change_marks_file_active(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "backend" / "main.py").write_text("print('staged')\n", encoding="utf-8")
    subprocess.run(["git", "add", "backend/main.py"], cwd=repo, check=True)
    old_ts = time.time() - 3600
    os.utime(repo / "backend" / "main.py", (old_ts, old_ts))
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is True
    assert "git status" in (reason or "")


def test_untracked_new_file_marks_active(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    new_file = repo / "backend" / "new_mod.py"
    new_file.write_text("x = 1\n", encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(new_file, (old_ts, old_ts))
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/new_mod.py")
    assert active is True
    assert "git status" in (reason or "")


def test_get_active_files_lists_git_dirty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "backend" / "main.py").write_text("print('a')\n", encoding="utf-8")
    (repo / "baseline.txt").write_text("b\n", encoding="utf-8")
    sensor = LiveWorkSensor(repo)
    active_files = sensor.get_active_files()
    assert "backend/main.py" in active_files
    assert "baseline.txt" in active_files


# ---------------------------------------------------------------------------
# Recent mtime signal
# ---------------------------------------------------------------------------


def test_recent_mtime_without_git_change(tmp_path: Path) -> None:
    """File touched seconds ago but matching its committed content —
    git is clean, so ONLY the mtime signal should fire."""
    repo = _make_repo(tmp_path)
    path = repo / "backend" / "main.py"
    # Touch mtime to "now" without changing content.
    now = time.time()
    os.utime(path, (now, now))
    sensor = LiveWorkSensor(repo, active_window_s=180)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is True
    assert "mtime" in (reason or "")


def test_old_mtime_outside_window(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = repo / "backend" / "main.py"
    very_old = time.time() - 10_000
    os.utime(path, (very_old, very_old))
    sensor = LiveWorkSensor(repo, active_window_s=180)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is False
    assert reason is None


def test_active_window_configurable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = repo / "backend" / "main.py"
    five_min_ago = time.time() - 300
    os.utime(path, (five_min_ago, five_min_ago))
    tight = LiveWorkSensor(repo, active_window_s=60)
    wide = LiveWorkSensor(repo, active_window_s=600)
    assert tight.is_human_active("backend/main.py")[0] is False
    assert wide.is_human_active("backend/main.py")[0] is True


# ---------------------------------------------------------------------------
# IDE lock files
# ---------------------------------------------------------------------------


def test_vim_swap_file_detected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # vim swap lives next to the target, named .main.py.swp
    (repo / "backend" / ".main.py.swp").write_bytes(b"vim-swap")
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is True
    assert "ide-lock" in (reason or "")


def test_emacs_lock_file_detected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "backend" / ".#main.py").write_text("user@host.1234", encoding="utf-8")
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is True
    assert "ide-lock" in (reason or "")


def test_backup_file_detected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "backend" / "main.py~").write_text("backup", encoding="utf-8")
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/main.py")
    assert active is True
    assert "ide-lock" in (reason or "")


def test_unrelated_swap_file_does_not_trigger(tmp_path: Path) -> None:
    """A swap file for a DIFFERENT file in the same dir must not mark
    our target as active."""
    repo = _make_repo(tmp_path)
    (repo / "backend" / ".other.py.swp").write_bytes(b"vim-swap")
    sensor = LiveWorkSensor(repo)
    active, _reason = sensor.is_human_active("backend/main.py")
    assert active is False


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_git_cache_hits_within_ttl(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "backend" / "main.py").write_text("print('x')\n", encoding="utf-8")
    sensor = LiveWorkSensor(repo, git_cache_ttl_s=5.0)
    # First call populates cache.
    sensor._git_dirty_set()
    first_at = sensor._git_cache_at
    # Second call within TTL reuses it.
    sensor._git_dirty_set()
    assert sensor._git_cache_at == first_at


def test_invalidate_cache_forces_refresh(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sensor = LiveWorkSensor(repo, git_cache_ttl_s=60.0)
    sensor._git_dirty_set()
    sensor.invalidate_cache()
    assert sensor._git_cache is None
    assert sensor._git_cache_at == 0.0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_empty_rel_path_is_inactive(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sensor = LiveWorkSensor(repo)
    assert sensor.is_human_active("") == (False, None)


def test_non_git_repo_degrades_gracefully(tmp_path: Path) -> None:
    """A directory with no git metadata must not crash the sensor."""
    (tmp_path / "file.txt").write_text("hi\n", encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(tmp_path / "file.txt", (old_ts, old_ts))
    sensor = LiveWorkSensor(tmp_path)
    active, reason = sensor.is_human_active("file.txt")
    assert active is False
    assert reason is None


def test_missing_file_does_not_crash(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sensor = LiveWorkSensor(repo)
    active, reason = sensor.is_human_active("backend/does_not_exist.py")
    assert active is False
    assert reason is None
