"""Slice 10 — LiveWorkSensor recency-composed dirty signal + wait horizon.

Run #20 (session bt-iso-1783924404): a chaos-dirtied file is dirty BY
CONSTRUCTION and stays dirty until the repair lands — which the timeless
git-dirty signal itself forbade (deadlock class). Signal 1 now composes
with mtime recency: dirty AND recently-modified → active (enriched
reason); dirty + stale mtime falls through to signals 2/3 (both quiet
for a stale file) → idle. `seconds_until_quiet` derives an exact wait
horizon from the SAME signal evaluation so the orchestrator can defer
instead of terminal-failing."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.live_work_sensor import LiveWorkSensor


_WINDOW_S = 100.0


@pytest.fixture
def repo(tmp_path):
    """A tiny real git repo with one committed file we can dirty at will.

    The committed file's mtime is aged far outside any window so only
    the signals each test constructs can fire."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "mod.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    _age(repo / "mod.py", 3600)
    return repo


def _age(path: Path, seconds: float) -> None:
    """Push a file's mtime `seconds` into the past — never sleep."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def _sensor(repo: Path) -> LiveWorkSensor:
    return LiveWorkSensor(repo, active_window_s=_WINDOW_S)


# ---------------------------------------------------------------------------
# Signal 1 — dirty composes with recency
# ---------------------------------------------------------------------------


async def test_dirty_with_stale_mtime_is_idle(repo):
    """THE Run #20 deadlock class: dirty-by-construction with an old
    mtime is NOT a human mid-edit — the file must read idle."""
    (repo / "mod.py").write_text("x = 2  # chaos\n")
    _age(repo / "mod.py", 3600)
    active, reason = await _sensor(repo).is_human_active("mod.py")
    assert active is False
    assert reason is None


async def test_dirty_with_recent_mtime_is_active_with_enriched_reason(repo):
    (repo / "mod.py").write_text("x = 2  # editing now\n")
    _age(repo / "mod.py", 10)
    active, reason = await _sensor(repo).is_human_active("mod.py")
    assert active is True
    assert reason is not None
    assert reason.startswith("git status: mod.py has uncommitted changes")
    assert "(modified 1" in reason and "s ago)" in reason  # ~10s, int()


async def test_clean_recent_mtime_still_active_signal2(repo):
    """Signal 2 untouched: a clean file touched seconds ago is active
    via the bare-mtime signal (reason names mtime, not git)."""
    _age(repo / "mod.py", 5)
    active, reason = await _sensor(repo).is_human_active("mod.py")
    assert active is True
    assert "mtime" in (reason or "")
    assert "git status" not in (reason or "")


async def test_dirty_deleted_file_fails_safe_active(repo):
    """Stat-fails-while-dirty edge: a dirty file whose mtime cannot be
    read (deleted in the working tree) must fail SAFE — active, with an
    unpredictable (infinite) horizon. The no-stomp guarantee outranks
    progress."""
    (repo / "mod.py").unlink()
    sensor = _sensor(repo)
    active, reason = await sensor.is_human_active("mod.py")
    assert active is True
    assert "git status" in (reason or "")
    assert await sensor.seconds_until_quiet("mod.py") == float("inf")


# ---------------------------------------------------------------------------
# Signal 3 — IDE lock unchanged, horizon is unpredictable
# ---------------------------------------------------------------------------


async def test_ide_lock_active_and_horizon_inf(repo):
    (repo / ".mod.py.swp").write_bytes(b"vim-swap")
    sensor = _sensor(repo)
    active, reason = await sensor.is_human_active("mod.py")
    assert active is True
    assert "ide-lock" in (reason or "")
    assert await sensor.seconds_until_quiet("mod.py") == float("inf")


# ---------------------------------------------------------------------------
# Kill switch — legacy timeless-dirty behavior
# ---------------------------------------------------------------------------


async def test_kill_switch_false_restores_timeless_dirty(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_LIVE_WORK_DIRTY_REQUIRES_RECENCY", "false")
    (repo / "mod.py").write_text("x = 2  # chaos\n")
    _age(repo / "mod.py", 3600)
    sensor = _sensor(repo)
    active, reason = await sensor.is_human_active("mod.py")
    assert active is True
    assert reason == "git status: mod.py has uncommitted changes"
    # Legacy dirty has no predictable expiry (dirty until the human commits).
    assert await sensor.seconds_until_quiet("mod.py") == float("inf")


# ---------------------------------------------------------------------------
# seconds_until_quiet — horizon derivation
# ---------------------------------------------------------------------------


async def test_seconds_until_quiet_idle_is_zero(repo):
    sensor = _sensor(repo)
    assert await sensor.seconds_until_quiet("mod.py") == 0.0
    assert await sensor.seconds_until_quiet("") == 0.0


async def test_seconds_until_quiet_recent_dirty_is_window_minus_age(repo):
    (repo / "mod.py").write_text("x = 2  # editing now\n")
    _age(repo / "mod.py", 60)
    horizon = await _sensor(repo).seconds_until_quiet("mod.py")
    # window(100) - age(60) ≈ 40 — tolerance for test wall-clock.
    assert horizon == pytest.approx(_WINDOW_S - 60, abs=2.0)
    assert 0.0 < horizon <= _WINDOW_S - 60


async def test_seconds_until_quiet_recent_clean_mtime_is_window_minus_age(repo):
    _age(repo / "mod.py", 80)
    horizon = await _sensor(repo).seconds_until_quiet("mod.py")
    assert horizon == pytest.approx(_WINDOW_S - 80, abs=2.0)


async def test_horizon_agrees_with_is_human_active(repo):
    """The two public methods project the SAME signal evaluation —
    active ⟺ horizon > 0, idle ⟺ horizon == 0."""
    sensor = _sensor(repo)
    (repo / "mod.py").write_text("x = 2\n")
    _age(repo / "mod.py", 30)
    active, _ = await sensor.is_human_active("mod.py")
    assert active is True
    assert await sensor.seconds_until_quiet("mod.py") > 0.0
    sensor.invalidate_cache()
    _age(repo / "mod.py", 3600)
    active, _ = await sensor.is_human_active("mod.py")
    assert active is False
    assert await sensor.seconds_until_quiet("mod.py") == 0.0
