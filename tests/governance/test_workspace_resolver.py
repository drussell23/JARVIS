"""Regression suite for the WorkspaceResolver — the run-#12 path-bug fix.

THE BUG (A1 live soak, run #12): ``test_runner._normalize`` rejected a valid
``tests/...py`` as "outside repo root" because the ``repo_root`` it was handed
(the CWD via a bare ``"."`` default) did NOT agree with the directory the
changed-file path anchored to. These tests prove:

1. :func:`resolve_repo_root` anchors to the real ``.git`` directory by walking
   parents — with NO hardcoded ``/opt/trinity`` / ``/Users`` literals.
2. The exact run-#12 mismatch is gone: a changed file under the fixture's real
   ``.git`` root, fed through ``TestRunner.resolve_affected_tests`` /
   ``_normalize`` with the resolver-derived root, is normalized + scoped to its
   own test (no ``BlockedPathError``, no whole-suite fallback) even when the
   process CWD is somewhere else entirely.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.workspace_resolver import (
    clear_cache,
    resolve_repo_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_resolver_cache_and_env() -> Iterator[None]:
    """Clear the resolver cache + scrub JARVIS_REPO_PATH around each test."""
    saved = os.environ.pop("JARVIS_REPO_PATH", None)
    clear_cache()
    try:
        yield
    finally:
        clear_cache()
        if saved is not None:
            os.environ["JARVIS_REPO_PATH"] = saved


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"], cwd=str(root), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Anchor behavior — no hardcoding
# ---------------------------------------------------------------------------


def test_resolves_to_git_dir_from_nested_start(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "backend" / "core").mkdir(parents=True)
    _git_init(root)
    nested = root / "backend" / "core"

    resolved = resolve_repo_root(start=nested)

    assert resolved == root.resolve()


def test_resolves_git_FILE_anchor_linked_worktree(tmp_path: Path) -> None:
    """A ``.git`` *file* (linked worktree marker) also anchors a root."""
    root = tmp_path / "linked"
    (root / "pkg").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/wt\n")

    resolved = resolve_repo_root(start=root / "pkg")

    assert resolved == root.resolve()


def test_fail_soft_no_git_returns_cwd(tmp_path: Path, monkeypatch) -> None:
    """No ``.git`` anywhere up the tree -> the resolved CWD, never raises."""
    orphan = tmp_path / "no_git_here"
    orphan.mkdir()
    monkeypatch.chdir(orphan)

    resolved = resolve_repo_root(start=orphan)

    assert resolved == orphan.resolve()


def test_no_hardcoded_absolute_paths_in_code() -> None:
    """Structural guard: NO literal node/dev root appears in a CODE line.

    Scans only the executable body (comments + the module docstring narrate
    the run-#12 ``/opt/trinity`` story; that prose is not a hardcoded path).
    The guard is that no STRING LITERAL anchors to an absolute machine path.
    """
    import ast

    # Derive the source path via the real repo root (dogfood, no literal).
    module_file = (
        resolve_repo_root() / "backend" / "core" / "ouroboros" / "governance"
        / "workspace_resolver.py"
    )
    tree = ast.parse(module_file.read_text())

    # Collect the AST ids of every docstring node (module / class / function)
    # so the narrative prose (which legitimately recounts the ``/opt/trinity``
    # run-#12 story) is excluded — only EXECUTABLE string literals are scanned.
    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstring_ids.add(id(body[0].value))

    code_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]
    for lit in code_literals:
        for forbidden in ("/opt/trinity", "/Users/", "/home/"):
            assert forbidden not in lit, (
                f"hardcoded path {forbidden!r} leaked into a string literal"
            )


def test_default_resolution_is_cached_and_clearable() -> None:
    first = resolve_repo_root()
    second = resolve_repo_root()
    assert first == second
    # The live repo we are running in must itself be a .git root.
    assert (first / ".git").exists()
    clear_cache()
    assert resolve_repo_root() == first


# ---------------------------------------------------------------------------
# The run-#12 reproduction — fixed
# ---------------------------------------------------------------------------


def test_run12_outside_repo_root_rejection_is_fixed(
    tmp_path: Path, monkeypatch
) -> None:
    """Reproduce run #12: a valid changed file under the REAL ``.git`` root is
    normalized + scoped to its own test even though the process CWD points
    somewhere else entirely.

    Pre-fix: a bare ``"."`` default anchored ``repo_root`` at the (wrong) CWD,
    so ``_normalize``'s ``relative_to`` raised BlockedPathError and scoping fell
    back to the whole suite. Post-fix: ``resolve_repo_root(start=...)`` anchors
    at the fixture's ``.git`` -> normalized cleanly -> scoped to ``test_foo.py``.
    """
    from backend.core.ouroboros.governance.test_runner import (
        BlockedPathError,
        TestRunner,
        _normalize,
    )

    # Build a real fixture repo: src + its sibling tests/test_foo.py.
    root = tmp_path / "trinity_fixture"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "tests").mkdir(parents=True)
    src = root / "pkg" / "foo.py"
    src.write_text("def add(a, b):\n    return a + b\n")
    (root / "pkg" / "tests" / "test_foo.py").write_text(
        "from pkg.foo import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git_init(root)

    # Simulate the live node: the process CWD is NOT the repo root.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    # The authoritative resolver anchors at the fixture's .git, not the CWD.
    repo_root = resolve_repo_root(start=src)
    assert repo_root == root.resolve()
    assert repo_root != Path.cwd().resolve()

    # _normalize agrees — no BlockedPathError (the run-#12 rejection).
    normalized = _normalize(src, repo_root)
    assert normalized == "pkg/foo.py"

    # And the changed source scopes to its OWN test, not the whole suite.
    runner = TestRunner(repo_root=repo_root)
    resolved = asyncio.run(runner.resolve_affected_tests((src,)))
    resolved_names = {p.name for p in resolved}
    assert "test_foo.py" in resolved_names
    # Must NOT degrade to the repo-level ``tests/`` directory (whole suite).
    assert all(p.name == "test_foo.py" for p in resolved), (
        f"scoping fell back to a directory / whole suite: {resolved}"
    )

    # Sanity: with the WRONG root the file no longer normalizes to its true
    # repo-relative path — proving the resolver is what fixes it, not a no-op.
    # (On a host whose tmp tree falls under a sandbox prefix, ``_normalize``
    # degrades to ``path.name`` rather than raising; either way the result is
    # NOT the correct ``pkg/foo.py`` the right root yields.)
    wrong_root = elsewhere.resolve()
    try:
        wrong_norm = _normalize(src, wrong_root)
        assert wrong_norm != "pkg/foo.py"
    except BlockedPathError:
        pass  # the strict (non-sandbox) rejection path — also correct
