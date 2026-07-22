"""List-and-Watch boot reconciliation for the TestFailureSensor.

Root cause (soak bt-2026-07-22-061753): the sensor is FS-events-PRIMARY
(Gap #4). A failing test COMMITTED just before boot fires no
``fs.changed`` event during the run, and the existing boot hydration
reconstructs only the UNCOMMITTED working-tree diff (``git diff HEAD``)
— so a clean tree with a red committed test hydrates NOTHING
(``[BootHydration] clean working tree -- nothing to hydrate``).

The fix extends the ground-truth "List" surface to the last
``JARVIS_TESTWATCHER_HYDRATION_COMMIT_DEPTH`` commits
(``git diff HEAD~N HEAD``) — bounded by construction (a commit touches
few files, never the 449-entry pytest-cache flood), reusing the exact
async git-subprocess pattern + downstream ``poll_once`` live
re-confirmation + the existing ``_hydrated_keys`` dedup.

Pins:
1. A committed-just-before-boot .py change appears in
   ``diff_working_tree`` even when the working tree is clean.
2. Depth 0 restores byte-identical legacy (working-tree only).
3. Uncommitted + committed changes union without duplication.
4. Idempotency: a file hydrated on boot is suppressed from a redundant
   live ``fs.changed`` within the dedup TTL (mandate-4 dedup).
5. Off-loop: ``diff_working_tree`` never blocks the running loop.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo_with_committed_red_test(tmp_path: Path) -> Path:
    """A repo whose LAST commit added a (failing) test — clean tree."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "seed.py").write_text("SEED = 1\n")
    _git(tmp_path, "add", "seed.py")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    # The committed-just-before-boot change (the probe shape).
    (tmp_path / "probe.py").write_text("def add(a, b):\n    return a - b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text(
        "from probe import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "add probe defect + test")
    return tmp_path


def _watcher(repo: Path) -> TestWatcher:
    return TestWatcher(repo=str(repo), repo_path=str(repo))


# ---------------------------------------------------------------------------
# 1 + 2 — committed change surfaced; depth 0 restores legacy
# ---------------------------------------------------------------------------


async def test_committed_change_hydrated_from_recent_commit(
    repo_with_committed_red_test: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repo_with_committed_red_test
    # Working tree is CLEAN — the legacy path would find nothing.
    assert _git(repo, "status", "--porcelain") == ""
    monkeypatch.setenv("JARVIS_TESTWATCHER_HYDRATION_COMMIT_DEPTH", "1")

    changed = await _watcher(repo).diff_working_tree()

    assert "probe.py" in changed
    assert "tests/test_probe.py" in changed


async def test_depth_zero_restores_legacy_working_tree_only(
    repo_with_committed_red_test: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TESTWATCHER_HYDRATION_COMMIT_DEPTH", "0")
    changed = await _watcher(repo_with_committed_red_test).diff_working_tree()
    # Clean tree + depth 0 → nothing (byte-identical pre-fix behavior).
    assert changed == []


# ---------------------------------------------------------------------------
# 3 — uncommitted + committed union, no duplication
# ---------------------------------------------------------------------------


async def test_uncommitted_and_committed_union_no_dup(
    repo_with_committed_red_test: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repo_with_committed_red_test
    monkeypatch.setenv("JARVIS_TESTWATCHER_HYDRATION_COMMIT_DEPTH", "1")
    # Dirty an already-committed file AND add a fresh uncommitted one.
    (repo / "probe.py").write_text("def add(a, b):\n    return a - b  # wip\n")
    (repo / "extra.py").write_text("X = 1\n")

    changed = await _watcher(repo).diff_working_tree()

    # probe.py appears once despite being both working-tree-dirty AND in
    # the last commit; the new + committed files are all present.
    assert changed.count("probe.py") == 1
    assert "extra.py" in changed
    assert "tests/test_probe.py" in changed


# ---------------------------------------------------------------------------
# 4 — idempotency: hydrated file dedups a later live fs.changed
# ---------------------------------------------------------------------------


async def test_hydrated_file_dedups_live_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs_mod,
    )
    sensor = tfs_mod.TestFailureSensor.__new__(tfs_mod.TestFailureSensor)
    sensor._hydrated_keys = {}

    # Simulate boot hydration recording the probe test.
    key = "tests/test_probe.py"
    sensor._hydrated_keys[key] = time.monotonic()

    # A genuine fs.changed for the same file, milliseconds later, is
    # suppressed within the dedup TTL — no duplicate repair dispatch.
    assert sensor._is_recently_hydrated(key) is True
    assert sensor._is_recently_hydrated("tests/test_other.py") is False


async def test_dedup_expires_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TESTWATCHER_HYDRATION_DEDUP_TTL_S", "0.01")
    import importlib
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs_mod,
    )
    importlib.reload(tfs_mod)
    sensor = tfs_mod.TestFailureSensor.__new__(tfs_mod.TestFailureSensor)
    sensor._hydrated_keys = {}
    key = "tests/test_probe.py"
    sensor._hydrated_keys[key] = time.monotonic() - 1.0  # older than TTL
    # A genuinely-recurring later edit re-runs (dedup window expired).
    assert sensor._is_recently_hydrated(key) is False
    importlib.reload(tfs_mod)  # restore default TTL for other tests


# ---------------------------------------------------------------------------
# 5 — off-loop: diff never blocks the running event loop
# ---------------------------------------------------------------------------


async def test_diff_does_not_block_running_loop(
    repo_with_committed_red_test: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TESTWATCHER_HYDRATION_COMMIT_DEPTH", "1")
    ticks = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.002)

    hb = asyncio.ensure_future(_heartbeat())
    try:
        changed = await _watcher(repo_with_committed_red_test).diff_working_tree()
    finally:
        stop.set()
        await hb
    assert "probe.py" in changed
    assert ticks >= 3, "git subprocess blocked the event loop"
