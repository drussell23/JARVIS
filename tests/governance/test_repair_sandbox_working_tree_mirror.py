"""Slice 9 — RepairSandbox working-tree mirror.

The worktree strategy materializes HEAD; battle-test chaos (and any real
uncommitted state) lives in the WORKING TREE — the baseline TestWatcher
actually observed. The mirror overlays the dirty delta so both sandbox
strategies share the working-tree baseline."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox


@pytest.fixture
def dirty_repo(tmp_path):
    """A tiny git repo with one committed file, one uncommitted
    modification, one untracked file, and one deleted-in-worktree file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "committed.py").write_text("x = 1\n")
    (repo / "doomed.py").write_text("y = 2\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    (repo / "committed.py").write_text("x = 999  # dirty\n")   # modified
    (repo / "untracked.py").write_text("z = 3\n")              # untracked
    (repo / "doomed.py").unlink()                              # deleted
    return repo


def test_mirror_on_baseline_is_working_tree(dirty_repo, monkeypatch):
    monkeypatch.delenv("JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED", raising=False)

    async def _run():
        async with RepairSandbox(dirty_repo, 30.0) as sb:
            root = sb.sandbox_root
            assert (root / "committed.py").read_text() == "x = 999  # dirty\n"
            assert (root / "untracked.py").read_text() == "z = 3\n"
            assert not (root / "doomed.py").exists()
    asyncio.run(_run())


def test_mirror_off_restores_head_baseline(dirty_repo, monkeypatch):
    async def _run():
        async with RepairSandbox(dirty_repo, 30.0, mirror_working_tree=False) as sb:
            root = sb.sandbox_root
            assert (root / "committed.py").read_text() == "x = 1\n"
            assert (root / "doomed.py").exists()
            assert not (root / "untracked.py").exists()
    asyncio.run(_run())


def test_env_kill_switch(dirty_repo, monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED", "false")

    async def _run():
        async with RepairSandbox(dirty_repo, 30.0) as sb:
            assert (sb.sandbox_root / "committed.py").read_text() == "x = 1\n"
    asyncio.run(_run())
