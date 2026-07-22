"""In-Memory Git Object Surgery & Quiescence Fast-Forward.

The mandated invariant under test: divergence resolution happens
ENTIRELY in git's object database — the human's working tree bytes,
uncommitted WIP, real index, and HEAD are never written by the landing
stages. No ``git stash`` exists in the mechanism.

Mandated integration case: a DIRTY operator working tree plus an
in-memory 3-way merge conflict. Assert (1) ``merge-tree`` executes
without modifying disk bytes, (2) the pending ref receives the
semantically reconciled commit, and (3) the dirty working tree remains
100% byte-identical throughout the cycle. Plus: clean-merge landing,
resolver-declined refusal, quiescence fast-forward semantics, and the
promoter integration (``target_dirty`` → PENDING outcome, not failure).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.pending_ref_lander import (
    land_pending_ref,
    pending_ref_name,
    try_quiescence_fastforward,
)
from backend.core.ouroboros.governance.workspace_promoter import (
    run_workspace_promotion,
)
from backend.core.ouroboros.governance.worktree_manager import WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _tree_fingerprint(root: Path) -> dict:
    """Byte-exact fingerprint of every file in the WORKING TREE
    (excluding .git): path -> sha256(content)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        out[str(p.relative_to(root))] = hashlib.sha256(
            p.read_bytes(),
        ).hexdigest()
    return out


@pytest.fixture
def diverged_dirty_operator(tmp_path: Path):
    """Operator repo: committed DIVERGENCE (conflicting change) + a
    DIRTY uncommitted WIP file + an untracked scratch file, plus a
    linked worktree carrying the verified repair commit."""
    operator = tmp_path / "operator"
    operator.mkdir()
    _git(operator, "init", "-q")
    (operator / "module.py").write_text("X = 1\nY = 1\n")
    _git(operator, "add", "module.py")
    _git(operator, "commit", "-q", "-m", "seed")

    wt = tmp_path / "wt"
    _git(operator, "worktree", "add", "-q", "-b", "ouroboros/auto/t", str(wt))
    (wt / "module.py").write_text("X = 2\nY = 1\n")  # the AI repair
    _git(wt, "add", "module.py")
    _git(wt, "commit", "-q", "-m", "fix: repair X")
    committed = _git(wt, "rev-parse", "HEAD")

    # Operator diverges on the SAME line (guaranteed 3-way conflict)...
    (operator / "module.py").write_text("X = 999\nY = 1\n")
    _git(operator, "add", "module.py")
    _git(operator, "commit", "-q", "-m", "operator diverged")
    # ...and carries live uncommitted WIP + untracked scratch.
    (operator / "module.py").write_text("X = 999\nY = 1\nWIP = True\n")
    (operator / "scratch.txt").write_text("human scratchpad\n")

    return operator, wt, committed


async def _accepting_resolver(rel, base, ours, theirs):
    # Deterministic semantic weave: keep operator's X, adopt nothing
    # blindly — a recognizable merged artifact.
    return "X = 999\nY = 1\nREPAIRED = True\n"


# ---------------------------------------------------------------------------
# 1. THE mandated case — conflict + dirty tree + byte-identity
# ---------------------------------------------------------------------------


async def test_conflicted_landing_never_touches_dirty_tree(
    diverged_dirty_operator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator, wt, committed = diverged_dirty_operator
    mgr = WorktreeManager(repo_root=operator)

    before = _tree_fingerprint(operator)
    head_before = _git(operator, "rev-parse", "HEAD")
    status_before = _git(operator, "status", "--porcelain")

    outcome = await land_pending_ref(
        mgr, operator, committed, "op-surgery-1",
        resolver=_accepting_resolver,
    )

    # (2) The pending ref received the reconciled commit.
    assert outcome.landed is True
    assert outcome.state == "landed_resolved"
    assert outcome.conflicted_paths == ("module.py",)
    ref = pending_ref_name("op-surgery-1")
    landed_sha = _git(operator, "rev-parse", ref)
    assert landed_sha == outcome.commit
    merged_content = _git(operator, "show", f"{ref}:module.py")
    assert "REPAIRED = True" in merged_content
    assert "<<<<<<<" not in merged_content
    # Honest merge history: both parents present.
    parents = _git(operator, "rev-list", "--parents", "-n", "1", ref).split()
    assert head_before in parents and committed in parents

    # (1)+(3) The dirty working tree is 100% byte-identical: WIP line,
    # untracked scratch, every file — and HEAD/index untouched.
    assert _tree_fingerprint(operator) == before
    assert _git(operator, "rev-parse", "HEAD") == head_before
    assert _git(operator, "status", "--porcelain") == status_before
    assert (operator / "module.py").read_text().endswith("WIP = True\n")
    assert (operator / "scratch.txt").read_text() == "human scratchpad\n"


async def test_resolver_declined_leaves_no_ref_and_no_bytes(
    diverged_dirty_operator,
) -> None:
    operator, wt, committed = diverged_dirty_operator
    mgr = WorktreeManager(repo_root=operator)
    before = _tree_fingerprint(operator)

    async def _declining(rel, base, ours, theirs):
        return None

    outcome = await land_pending_ref(
        mgr, operator, committed, "op-decline-1", resolver=_declining,
    )
    assert outcome.landed is False
    assert outcome.state == "conflict_unresolved"
    with pytest.raises(subprocess.CalledProcessError):
        _git(operator, "rev-parse", pending_ref_name("op-decline-1"))
    assert _tree_fingerprint(operator) == before


async def test_clean_inmemory_merge_lands_without_resolver(
    tmp_path: Path,
) -> None:
    """Non-overlapping divergence: merge-tree resolves cleanly in
    memory; the pending ref lands with zero resolver involvement and
    zero tree contact."""
    operator = tmp_path / "op"
    operator.mkdir()
    _git(operator, "init", "-q")
    (operator / "a.py").write_text("A = 1\n")
    (operator / "b.py").write_text("B = 1\n")
    _git(operator, "add", ".")
    _git(operator, "commit", "-q", "-m", "seed")
    wt = tmp_path / "wt"
    _git(operator, "worktree", "add", "-q", "-b", "ouroboros/auto/c", str(wt))
    (wt / "a.py").write_text("A = 2\n")
    _git(wt, "add", "a.py")
    _git(wt, "commit", "-q", "-m", "fix a")
    committed = _git(wt, "rev-parse", "HEAD")
    (operator / "b.py").write_text("B = 2\n")
    _git(operator, "add", "b.py")
    _git(operator, "commit", "-q", "-m", "operator changes b")
    before = _tree_fingerprint(operator)

    mgr = WorktreeManager(repo_root=operator)
    outcome = await land_pending_ref(mgr, operator, committed, "op-clean-1")
    assert outcome.landed is True and outcome.state == "landed_clean"
    ref = pending_ref_name("op-clean-1")
    assert _git(operator, "show", f"{ref}:a.py") == "A = 2"
    assert _git(operator, "show", f"{ref}:b.py") == "B = 2"
    assert _tree_fingerprint(operator) == before


# ---------------------------------------------------------------------------
# 2. Quiescence fast-forward
# ---------------------------------------------------------------------------


async def test_quiescence_ff_lands_clean_pending_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = tmp_path / "op"
    operator.mkdir()
    _git(operator, "init", "-q")
    (operator / "a.py").write_text("A = 1\n")
    _git(operator, "add", "a.py")
    _git(operator, "commit", "-q", "-m", "seed")
    wt = tmp_path / "wt"
    _git(operator, "worktree", "add", "-q", "-b", "ouroboros/auto/f", str(wt))
    (wt / "a.py").write_text("A = 2\n")
    _git(wt, "add", "a.py")
    _git(wt, "commit", "-q", "-m", "fix a")
    committed = _git(wt, "rev-parse", "HEAD")

    mgr = WorktreeManager(repo_root=operator)
    outcome = await land_pending_ref(mgr, operator, committed, "op-ff-1")
    assert outcome.landed is True

    # Quiescent by construction for the test: 0s idle window.
    monkeypatch.setenv("JARVIS_QUIESCENCE_FF_IDLE_S", "0")
    landed = await try_quiescence_fastforward(mgr, operator)

    assert landed == [pending_ref_name("op-ff-1")]
    assert (operator / "a.py").read_text() == "A = 2\n"
    # Ref consumed after landing; HEAD reflog carries the audit trail.
    with pytest.raises(subprocess.CalledProcessError):
        _git(operator, "rev-parse", pending_ref_name("op-ff-1"))


async def test_quiescence_ff_declines_diverged_ref(
    diverged_dirty_operator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending ref that is NOT a fast-forward of HEAD stays parked —
    git's own ancestry proof gates the landing."""
    operator, wt, committed = diverged_dirty_operator
    mgr = WorktreeManager(repo_root=operator)
    outcome = await land_pending_ref(
        mgr, operator, committed, "op-ffd-1",
        resolver=_accepting_resolver,
    )
    assert outcome.landed is True
    before = _tree_fingerprint(operator)
    monkeypatch.setenv("JARVIS_QUIESCENCE_FF_IDLE_S", "0")
    # The merge commit HAS HEAD as ancestor (parent 1) so it IS
    # ff-able — but the touched path module.py is DIRTY in the tree:
    # the clean-tree-at-paths guard must decline.
    landed = await try_quiescence_fastforward(mgr, operator)
    assert landed == []
    assert _tree_fingerprint(operator) == before


# ---------------------------------------------------------------------------
# 3. Promoter integration — target_dirty becomes PENDING, not failure
# ---------------------------------------------------------------------------


async def test_promoter_parks_pending_on_dirty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = tmp_path / "op"
    operator.mkdir()
    _git(operator, "init", "-q")
    (operator / "m.py").write_text("M = 1\n")
    _git(operator, "add", "m.py")
    _git(operator, "commit", "-q", "-m", "seed")
    wt = tmp_path / "wt"
    _git(operator, "worktree", "add", "-q", "-b", "ouroboros/auto/p", str(wt))
    (wt / "m.py").write_text("M = 2\n")
    _git(wt, "add", "m.py")
    _git(wt, "commit", "-q", "-m", "fix m")
    committed = _git(wt, "rev-parse", "HEAD")
    # Dirty the TARGET path in the operator tree (uncommitted WIP).
    (operator / "m.py").write_text("M = 1\nWIP = 1\n")
    before = _tree_fingerprint(operator)

    monkeypatch.setenv("JARVIS_WORKSPACE_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PROMOTION_LIVE_WORK_CONSULT", "false")
    monkeypatch.setenv("JARVIS_QUIESCENCE_FF_ENABLED", "false")

    orch = SimpleNamespace(
        _config=SimpleNamespace(project_root=wt, execution_root=wt),
        _stack=SimpleNamespace(comm=None),
    )
    ctx = SimpleNamespace(
        op_id="op-park-1", target_files=(), generate_file_hashes=None,
    )
    outcome = await run_workspace_promotion(orch, ctx, committed, None)

    assert outcome.attempted is True
    assert outcome.promoted is False
    assert getattr(outcome, "pending", False) is True, (
        f"expected PENDING outcome, got {outcome.state}: {outcome.detail}"
    )
    assert outcome.state in ("landed_clean", "landed_resolved")
    # The WIP is byte-identical; the change waits on the pending ref.
    assert _tree_fingerprint(operator) == before
    ref = pending_ref_name("op-park-1")
    assert _git(operator, "show", f"{ref}:m.py") == "M = 2"


def test_no_stash_anywhere_in_the_mechanism() -> None:
    """The stash hazard class is structurally absent: the lander and
    the promoter contain zero ``git stash`` invocations."""
    import inspect
    from backend.core.ouroboros.governance import (
        pending_ref_lander, workspace_promoter,
    )
    for mod in (pending_ref_lander, workspace_promoter):
        src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
        assert '"stash"' not in src and "'stash'" not in src, (
            f"{mod.__name__} invokes git stash — forbidden by mandate"
        )
