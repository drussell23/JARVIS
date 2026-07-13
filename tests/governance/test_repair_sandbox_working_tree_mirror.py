"""Slice 9 — RepairSandbox working-tree mirror.

The worktree strategy materializes HEAD; battle-test chaos (and any real
uncommitted state) lives in the WORKING TREE — the baseline TestWatcher
actually observed. The mirror overlays the dirty delta so both sandbox
strategies share the working-tree baseline."""
from __future__ import annotations

import asyncio
import logging
import shutil
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
            assert sb.baseline_fidelity == "working_tree"
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
            assert sb.baseline_fidelity == "head"
    asyncio.run(_run())


def test_overlay_cap_trip_falls_back_to_head_baseline(dirty_repo, monkeypatch, caplog):
    """Slice 9 final review (Important): an overlay whose dirty-delta
    exceeds JARVIS_SANDBOX_OVERLAY_MAX_FILES must not silently eat the
    materialization budget — the sandbox still gets created, but its
    baseline degrades to HEAD (the pre-existing fail-soft), and the
    WARNING names the cap-trip reason distinctly from a git fault."""
    monkeypatch.setenv("JARVIS_SANDBOX_OVERLAY_MAX_FILES", "1")
    caplog.set_level(
        logging.WARNING,
        logger="backend.core.ouroboros.governance.repair_sandbox",
    )

    async def _run():
        async with RepairSandbox(dirty_repo, 30.0) as sb:
            root = sb.sandbox_root
            assert root is not None
            # HEAD baseline, NOT the working-tree overlay: committed
            # content survives, the working-tree deletion does not.
            assert (root / "committed.py").read_text() == "x = 1\n"
            assert (root / "doomed.py").exists()
            # Refused BEFORE any copy — nothing landed, baseline is HEAD.
            assert sb.baseline_fidelity == "head"
    asyncio.run(_run())

    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("delta too large" in msg for msg in warnings), warnings
    assert any("overlay_cap_exceeded" in msg for msg in warnings), warnings


def test_mid_copy_fault_yields_partial_fidelity(dirty_repo, monkeypatch, caplog):
    """Slice 9 review Finding 2: a copy-loop fault AFTER files have
    already landed leaves a HEAD+partial-delta chimera — the fidelity
    attribute must say 'partial' and the WARNING must not claim the
    baseline is (pure) HEAD."""
    caplog.set_level(
        logging.WARNING,
        logger="backend.core.ouroboros.governance.repair_sandbox",
    )
    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _flaky_copy2(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk fault (injected)")
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copy2", _flaky_copy2)

    async def _run():
        async with RepairSandbox(dirty_repo, 30.0) as sb:
            assert sb.sandbox_root is not None
            assert sb.baseline_fidelity == "partial"
    asyncio.run(_run())

    assert calls["n"] > 1  # the fault actually fired mid-loop
    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("PARTIAL" in msg for msg in warnings), warnings
    assert not any("baseline is HEAD" in msg for msg in warnings), warnings
