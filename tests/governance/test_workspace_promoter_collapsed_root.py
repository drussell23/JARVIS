"""Workspace promotion under the isolation-collapsed config posture.

Root cause (net-landed soak bt-2026-07-22-042548): FileIsolation
(``autonomous_workspace.resolve_loop_project_root``) routes the loop's
``project_root`` INTO the sovereignty worktree at arming, so the
orchestrator config carries worktree == worktree and the OPERATOR tree
vanishes from every config field. ``run_workspace_promotion``'s
``noop_same_root`` early-return therefore fired on EVERY op of every
isolation-armed soak — silently, structurally unreachable promotion:
the soak's op landed both files, verified, auto-committed d27d632f in
the workspace branch… and the operator tree never received it.

The repair derives the TRUE operator root from git topology at read
time (a linked worktree's ``--git-common-dir`` lives under the primary
checkout) — env-free, hardcode-free, inert for genuinely-primary
checkouts. This suite pins:

1. The collapsed posture (config.project_root == config.execution_root
   == a linked worktree) now PROMOTES: the workspace commit lands on
   the operator tree.
2. A genuinely-primary checkout (no linked worktree) still no-ops as
   ``noop_same_root`` — the legacy posture is byte-identical.
3. The derivation helper's contract (linked → operator root; primary →
   its own root; garbage → None).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.workspace_promoter import (
    _derive_operator_root,
    run_workspace_promotion,
)
from backend.core.ouroboros.governance.worktree_manager import WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def operator_and_worktree(tmp_path: Path):
    """A primary checkout + a linked ``ouroboros/auto/*`` worktree with
    one un-promoted workspace commit — the soak posture in miniature."""
    operator = tmp_path / "operator"
    operator.mkdir()
    _git(operator, "init", "-q")
    (operator / "module.py").write_text("X = 1\n")
    _git(operator, "add", "module.py")
    _git(operator, "commit", "-q", "-m", "seed")

    wt = tmp_path / "wt"
    _git(operator, "worktree", "add", "-q", "-b", "ouroboros/auto/test", str(wt))
    (wt / "module.py").write_text("X = 2\n")
    _git(wt, "add", "module.py")
    _git(wt, "commit", "-q", "-m", "fix(test): workspace repair")
    committed = _git(wt, "rev-parse", "HEAD")
    return operator, wt, committed


def _orch(project_root: Path, execution_root: Path) -> SimpleNamespace:
    """Minimal orchestrator surface for run_workspace_promotion — the
    collapsed posture passes the SAME path for both roots."""
    return SimpleNamespace(
        _config=SimpleNamespace(
            project_root=project_root, execution_root=execution_root,
        ),
        _stack=SimpleNamespace(comm=None),
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        op_id="op-collapsed-1", target_files=(), generate_file_hashes=None,
    )


# ---------------------------------------------------------------------------
# 1. Collapsed posture now promotes
# ---------------------------------------------------------------------------


async def test_collapsed_config_promotes_to_derived_operator_root(
    operator_and_worktree, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator, wt, committed = operator_and_worktree
    monkeypatch.setenv("JARVIS_WORKSPACE_PROMOTION_ENABLED", "true")
    # The consult needs a full orchestrator; its budget semantics are
    # covered by its own suite — out of scope for the root-derivation pin.
    monkeypatch.setenv("JARVIS_PROMOTION_LIVE_WORK_CONSULT", "false")

    outcome = await run_workspace_promotion(
        _orch(wt, wt),          # collapsed: project_root == execution_root == worktree
        _ctx(),
        committed,
        None,
    )

    assert outcome.promoted is True, (
        f"promotion refused in collapsed posture: {outcome.state} "
        f"{getattr(outcome, 'detail', '')}"
    )
    # The fix genuinely landed on the OPERATOR tree.
    assert (operator / "module.py").read_text() == "X = 2\n"
    assert "workspace repair" in _git(operator, "log", "-1", "--format=%s")


async def test_primary_checkout_still_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No linked worktree: the commit already lives on the operator
    tree — legacy ``noop_same_root`` byte-identical."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q")
    (primary / "a.py").write_text("A = 1\n")
    _git(primary, "add", "a.py")
    _git(primary, "commit", "-q", "-m", "seed")
    head = _git(primary, "rev-parse", "HEAD")

    monkeypatch.setenv("JARVIS_WORKSPACE_PROMOTION_ENABLED", "true")
    outcome = await run_workspace_promotion(
        _orch(primary, primary), _ctx(), head, None,
    )
    assert outcome.attempted is False
    assert outcome.state == "noop_same_root"


# ---------------------------------------------------------------------------
# 2. Derivation helper contract
# ---------------------------------------------------------------------------


async def test_derive_operator_root_linked_worktree(
    operator_and_worktree,
) -> None:
    operator, wt, _ = operator_and_worktree
    mgr = WorktreeManager(repo_root=wt)
    derived = await _derive_operator_root(mgr, wt)
    assert derived is not None
    assert Path(os.path.realpath(derived)) == Path(
        os.path.realpath(operator)
    )


async def test_derive_operator_root_primary_is_self(tmp_path: Path) -> None:
    primary = tmp_path / "p"
    primary.mkdir()
    _git(primary, "init", "-q")
    (primary / "x").write_text("x")
    _git(primary, "add", "x")
    _git(primary, "commit", "-q", "-m", "s")
    mgr = WorktreeManager(repo_root=primary)
    derived = await _derive_operator_root(mgr, primary)
    # Primary checkout: derivation returns its own root — the caller's
    # equality check converts that into the legacy noop.
    assert derived is not None
    assert Path(os.path.realpath(derived)) == Path(os.path.realpath(primary))


async def test_derive_operator_root_non_repo_is_none(tmp_path: Path) -> None:
    bare = tmp_path / "not_a_repo"
    bare.mkdir()
    mgr = WorktreeManager(repo_root=bare)
    assert await _derive_operator_root(mgr, bare) is None


# ---------------------------------------------------------------------------
# 3. Atomic Promotion — Conflict Shedding on a diverged primary
# ---------------------------------------------------------------------------


async def test_diverged_primary_sheds_conflict_and_stays_intact(
    operator_and_worktree, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mandated diverged-primary case: after the worktree branched,
    the OPERATOR tree advanced with a CONFLICTING change to the same
    lines. Promotion must cleanly abort (Conflict Shedding) via the
    existing typed ``PromotionError('conflict_aborted')`` taxonomy —
    the operator tree stays byte-identical to its diverged state, the
    workspace branch survives intact for human review, and nothing is
    force-merged."""
    operator, wt, committed = operator_and_worktree

    # Diverge the primary with a conflicting edit to the same file/line.
    (operator / "module.py").write_text("X = 999  # operator diverged\n")
    _git(operator, "add", "module.py")
    _git(operator, "commit", "-q", "-m", "operator diverged")
    diverged_head = _git(operator, "rev-parse", "HEAD")
    diverged_content = (operator / "module.py").read_text()

    monkeypatch.setenv("JARVIS_WORKSPACE_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PROMOTION_LIVE_WORK_CONSULT", "false")

    outcome = await run_workspace_promotion(
        _orch(wt, wt),  # collapsed posture — derivation finds the primary
        _ctx(),
        committed,
        None,
    )

    # Clean abort, typed: attempted but NOT promoted, conflict state.
    assert outcome.attempted is True
    assert outcome.promoted is False
    assert outcome.state == "conflict_aborted", (
        f"expected conflict shedding, got {outcome.state!r} "
        f"({getattr(outcome, 'detail', '')})"
    )
    # The primary is byte-identical to its diverged state — no corruption,
    # no forced merge, no half-applied cherry-pick residue.
    assert (operator / "module.py").read_text() == diverged_content
    assert _git(operator, "rev-parse", "HEAD") == diverged_head
    assert _git(operator, "status", "--porcelain") == ""
    # The workspace branch survives intact for human review.
    assert _git(wt, "rev-parse", "HEAD") == committed
    assert (wt / "module.py").read_text() == "X = 2\n"
