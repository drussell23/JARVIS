from __future__ import annotations
import asyncio
import subprocess

import pytest
from backend.core.ouroboros.governance.workspace_checkpoint import WorkspaceCheckpointManager


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_working_tree_content_sha_is_deterministic(tmp_path):
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    # make a working-tree change so stash create is non-empty
    (tmp_path / "a.py").write_text("x = 2\n")
    mgr = WorkspaceCheckpointManager(tmp_path)
    sha1 = await mgr.working_tree_content_sha()
    sha2 = await mgr.working_tree_content_sha()
    assert sha1 and sha1 == sha2  # SAME content -> SAME tree sha (the bug was: differ)


@pytest.mark.asyncio
async def test_clean_tree_resolves_to_head_tree(tmp_path):
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    mgr = WorkspaceCheckpointManager(tmp_path)
    # clean tree: git stash create is empty -> falls back to HEAD^{tree}
    clean_sha = await mgr.working_tree_content_sha()
    head_tree = await mgr.tree_sha_for_ref("")  # "" -> HEAD inside the helper
    assert clean_sha and clean_sha == head_tree


@pytest.mark.asyncio
async def test_content_sha_changes_with_content(tmp_path):
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    mgr = WorkspaceCheckpointManager(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    sha_a = await mgr.working_tree_content_sha()
    (tmp_path / "a.py").write_text("x = 3\n")
    sha_b = await mgr.working_tree_content_sha()
    assert sha_a != sha_b  # different content -> different tree sha
