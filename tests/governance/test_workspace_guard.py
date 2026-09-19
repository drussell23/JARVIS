"""A worker in the wrong checkout must not write a ledger.

This host carries two checkouts of the same repo by design — the WSL tree where
soaks run, and the Windows tree VS Code renders and `ov`'s editable install
executes. A worker that resolves paths from its own cwd writes into whichever
it is standing in, and `.jarvis/goal_reconciliation_ledger.jsonl` in one is a
different file from the same name in the other. A hash-chained, MAC'd,
append-only record split across two of them is not recoverable from either.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import workspace_guard as WG


@pytest.fixture()
def trees(tmp_path: Path, monkeypatch):
    """Two checkouts, like the real host."""
    a, b = tmp_path / "wsl", tmp_path / "windows"
    for t in (a, b):
        (t / ".git").mkdir(parents=True)
        (t / "backend").mkdir()
    monkeypatch.setattr(WG, "authoritative_workspace", lambda *_a, **_k: a.resolve())
    return a, b


def test_the_authoritative_tree_is_allowed(trees):
    a, _ = trees
    v = WG.workspace_verdict(cwd=a)
    assert v.ok is True
    assert v.should_shed is False


def test_a_subdirectory_of_it_is_allowed(trees):
    a, _ = trees
    assert WG.workspace_verdict(cwd=a / "backend").ok is True


def test_THE_OTHER_CHECKOUT_IS_REFUSED(trees):
    """The regression: same repo, same filenames, different tree."""
    _, b = trees
    v = WG.workspace_verdict(cwd=b)
    assert v.ok is False
    assert v.should_shed is True
    assert "WorkspaceBoundaryViolation" in v.reason
    assert str(b) in v.reason


def test_it_names_BOTH_trees_so_the_fault_is_actionable(trees):
    a, b = trees
    v = WG.workspace_verdict(cwd=b)
    assert v.authoritative == str(a.resolve())
    assert v.actual == str(b.resolve())


def test_an_unknowable_root_does_NOT_gate(tmp_path, monkeypatch):
    """A guard that refuses what it cannot adjudicate stops everything rather
    than the wrong thing."""
    monkeypatch.setattr(WG, "authoritative_workspace", lambda *_a, **_k: None)
    assert WG.workspace_verdict(cwd=tmp_path).ok is True


def test_it_can_be_disabled(trees, monkeypatch):
    _, b = trees
    monkeypatch.setenv("JARVIS_WORKSPACE_GUARD_ENABLED", "false")
    assert WG.workspace_verdict(cwd=b).ok is True


def test_it_is_ON_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_WORKSPACE_GUARD_ENABLED", raising=False)
    assert WG.guard_enabled() is True


def test_a_broken_guard_never_blocks_work(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise RuntimeError("resolver on fire")

    monkeypatch.setattr(WG, "authoritative_workspace", _boom)
    assert WG.workspace_verdict(cwd=tmp_path).ok is True


def test_it_MINTS_NO_SECOND_TRUTH():
    """`effective_execution_root` is "THE canonical execution-root seam" and
    says duplicating it is "a review-rejectable offense (Run-21 root cause was
    exactly such a split-truth)". This guard asks it; it does not re-declare
    the answer under a new environment variable."""
    import inspect

    src = inspect.getsource(WG)
    assert "effective_execution_root" in src
    # Scoped to the CODE, not the module docstring — which names the rejected
    # variable precisely in order to record why it does not exist.
    body = src.split('"""', 2)[-1]
    assert "JARVIS_AUTHORITATIVE_WORKSPACE" not in body


def test_it_compares_by_resolved_path_not_inode():
    """The two trees live on different filesystems (ext4 vs DrvFS), so inode
    identity is meaningless across them."""
    import inspect

    src = inspect.getsource(WG._same_tree)
    assert "st_ino" not in src and "samefile" not in src
