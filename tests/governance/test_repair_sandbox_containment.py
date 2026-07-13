"""Slice 9 — RepairSandbox write-path containment.

``apply_patch`` and ``apply_full_content`` receive MODEL-CHOSEN candidate
paths from the L2 repair lane. pathlib ``/`` with an absolute right
operand replaces the sandbox root entirely, and ``..`` segments walk out
of it — so both primitives must clamp the target to the sandbox before
a single byte lands. Violations raise ``BlockedPathError`` (the type the
orchestrator's ``_map_tree_run_exception`` classifies fc='security')."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox
from backend.core.ouroboros.governance.test_runner import BlockedPathError


@pytest.fixture
def tiny_repo(tmp_path):
    """A tiny committed git repo with a nested package file to patch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "pkg" / "sub" / "mod.py").write_text("a = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return repo


def _sandbox(repo):
    # mirror off: containment is orthogonal to the working-tree overlay.
    return RepairSandbox(repo, 30.0, mirror_working_tree=False)


ESCAPES = ("../escape.py", "/tmp/escape.py", "a/../../escape.py")


@pytest.mark.parametrize("evil", ESCAPES)
def test_apply_full_content_rejects_escapes(tiny_repo, tmp_path, evil):
    async def _run():
        async with _sandbox(tiny_repo) as sb:
            with pytest.raises(BlockedPathError):
                await sb.apply_full_content("pwned = True\n", evil)
    asyncio.run(_run())
    # Nothing landed outside the sandbox.
    assert not (tmp_path / "escape.py").exists()
    assert not Path("/tmp/escape.py").exists()


@pytest.mark.parametrize("evil", ESCAPES)
def test_apply_patch_rejects_escapes(tiny_repo, tmp_path, evil):
    async def _run():
        async with _sandbox(tiny_repo) as sb:
            with pytest.raises(BlockedPathError):
                await sb.apply_patch("@@ -0,0 +1 @@\n+pwned = True\n", evil)
    asyncio.run(_run())
    assert not (tmp_path / "escape.py").exists()


def test_symlink_hop_rejected(tiny_repo, tmp_path):
    """A relative path that normalizes INSIDE the sandbox but routes
    through a symlink pointing outside must still be blocked."""
    outside = tmp_path / "outside"
    outside.mkdir()

    async def _run():
        async with _sandbox(tiny_repo) as sb:
            (sb.sandbox_root / "lnk").symlink_to(outside)
            with pytest.raises(BlockedPathError):
                await sb.apply_full_content("pwned = True\n", "lnk/evil.py")
    asyncio.run(_run())
    assert not (outside / "evil.py").exists()


def test_legitimate_nested_paths_still_work(tiny_repo):
    async def _run():
        async with _sandbox(tiny_repo) as sb:
            root = sb.sandbox_root
            # apply_full_content: brand-new nested file.
            await sb.apply_full_content("b = 2\n", "pkg/sub/new_mod.py")
            assert (root / "pkg" / "sub" / "new_mod.py").read_text() == "b = 2\n"
            # apply_patch: existing nested file.
            diff = (
                "--- pkg/sub/mod.py\n"
                "+++ pkg/sub/mod.py\n"
                "@@ -1 +1 @@\n"
                "-a = 1\n"
                "+a = 2\n"
            )
            await sb.apply_patch(diff, "pkg/sub/mod.py")
            assert (root / "pkg" / "sub" / "mod.py").read_text() == "a = 2\n"
    asyncio.run(_run())
