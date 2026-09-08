"""Resolved test paths run in the tree that runs them.

A candidate-tree validate builds its runner on the sandbox copy but keys
test DISCOVERY to the base tree (``map_root``: the import map is the
base's), so every resolved path pointed into the session worktree; ``run()``
refused them as outside its root and the validate passed with ZERO tests
(``test_total: 0``, 2026-09-08). Paths discovered under a discovery root are
rebased onto the run tree when the same file exists there.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.test_runner import TestRunner, _ast_import_cache


def _tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    # named so neither the name convention nor the suffix search finds it:
    # only the AST import map (keyed to the BASE tree) does.
    (root / "tests" / "test_arith.py").write_text(
        "from pkg.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_paths_discovered_in_the_base_tree_run_in_the_sandbox(tmp_path):
    base = tmp_path / "repo" / ".worktrees" / "session"
    _tree(base)
    sandbox = tmp_path / "sandbox"
    shutil.copytree(base, sandbox)
    _ast_import_cache.clear()
    runner = TestRunner(repo_root=sandbox, map_root=base)
    resolved = await runner.resolve_affected_tests((sandbox / "pkg" / "calc.py",))
    assert resolved, "the AST import strategy must find tests/test_arith.py"
    for p in resolved:
        assert str(p).startswith(str(sandbox.resolve())), p
    assert (sandbox / "tests" / "test_arith.py").resolve() in {Path(p).resolve() for p in resolved}


@pytest.mark.asyncio
async def test_a_path_missing_from_the_run_tree_is_left_alone(tmp_path):
    base = tmp_path / "repo" / ".worktrees" / "session"
    _tree(base)
    sandbox = tmp_path / "sandbox"
    shutil.copytree(base, sandbox)
    (sandbox / "tests" / "test_arith.py").unlink()
    _ast_import_cache.clear()
    runner = TestRunner(repo_root=sandbox, map_root=base)
    resolved = await runner.resolve_affected_tests((sandbox / "pkg" / "calc.py",))
    assert any(str(p).startswith(str(base.resolve())) for p in resolved), resolved


def test_rebase_is_identity_when_discovery_and_run_trees_coincide(tmp_path):
    _tree(tmp_path / "one")
    runner = TestRunner(repo_root=tmp_path / "one")
    paths = [tmp_path / "one" / "tests" / "test_arith.py"]
    assert runner._rebase_to_run_tree(paths, tmp_path / "one") == paths
