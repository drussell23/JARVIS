from __future__ import annotations

# backend/core/ouroboros/governance/autonomous_pr_pipeline.py
"""
Autonomous PR Gate Pipeline (Iron Triad, Task 13b)
==================================================
Capstone of the Iron Triad: make ONE autonomous-PR op assemble the full
branch-bound token chain by running Gate (1) (container exec) + Gate (2)
(blast radius) inside an ISOLATED git worktree, then handing the SAME chain
to Gate (3) + the enforced ``create_review_pr``.

Closes the final-review finding that the three gates sat on mutually-exclusive
risk-tier branches: previously a candidate that reached the Orange (APPROVAL_
REQUIRED) PR path carried no sandbox/blast tokens (those were minted only on
the auto-apply tiers), so ``create_review_pr``'s enforcement was inert.

Invariants
----------
* The worktree is a VALIDATION sandbox. The REAL repo tree is NEVER touched --
  the candidate is written into, and tests run inside, the worktree only.
* The worktree branch (``ouroboros/a1-validate/<op_id>``) is uniform across
  Gate (1), Gate (2), and Gate (3) so the branch-bound chain verifies and the
  ``expected_branch_context`` assertion in ``create_review_pr`` passes.
* Cleanup ALWAYS runs (``finally``) and is best-effort: a cleanup failure is
  logged but never masks the real gate result or error.
* Fail-closed: a gate rejection raises ``PRGatePipelineError`` so the wiring
  routes POSTMORTEM (no PR) -- never a silent PR.
* Async-first: no blocking on the loop (subprocess via asyncio; file writes via
  ``run_in_executor``). Python 3.9 compatible.

Injectable seams keep the orchestration unit-testable with NO real Docker/git;
the ``_default_*`` helpers wire the real production components.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.Orchestrator")


class PRGatePipelineError(RuntimeError):
    """A gate rejected the candidate in the isolated worktree -> no PR."""


@dataclass(frozen=True)
class PRGateResult:
    sandbox_token: object
    blast_token: object
    branch_context: str


def pipeline_enabled() -> bool:
    """The unification runs only when the enforcer is armed (same master flag
    as the PR token gate, ``JARVIS_A1_TOKEN_ENFORCER_ENABLED``)."""
    return os.environ.get(
        "JARVIS_A1_TOKEN_ENFORCER_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Default seam helpers (real production wiring)
# ---------------------------------------------------------------------------


def _default_worktree_factory(
    repo_root: str,
) -> Callable[[str], Awaitable[Path]]:
    """Return an async fn that creates an isolated worktree for ``branch``."""

    async def _factory(branch: str) -> Path:
        from .worktree_manager import WorktreeManager

        mgr = WorktreeManager(Path(repo_root))
        return await mgr.create(branch)

    return _factory


def _default_worktree_cleanup(
    repo_root: str,
) -> Callable[[Path], Awaitable[None]]:
    """Return an async fn that removes a worktree (git deregister + rmtree)."""

    async def _cleanup(wt: Path) -> None:
        from .worktree_manager import WorktreeManager

        mgr = WorktreeManager(Path(repo_root))
        await mgr.cleanup(wt)

    return _cleanup


async def _default_apply_candidate(
    wt: Path, files: Sequence[Tuple[str, str]]
) -> None:
    """Write each ``(relpath, content)`` into ``wt/relpath`` so Gate (2)'s
    tests see the candidate. Runs off-loop via the default executor.
    """

    def _write() -> None:
        for relpath, content in files:
            dest = Path(wt) / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write)


async def _run_git(args: Sequence[str], *, cwd: Path) -> Tuple[int, str, str]:
    """Run ``git <args>`` with cwd=<wt>. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else 1
    return rc, out.decode(errors="replace"), err.decode(errors="replace")


async def _default_tree_sha(wt: Path) -> str:
    """Deterministic content tree-SHA of the worktree, including the applied
    (uncommitted) candidate files.

    Mirrors ``workspace_checkpoint.working_tree_content_sha`` scoped to
    ``cwd=wt``: ``git stash create`` snapshots the working tree into a dangling
    commit without mutating it, and ``<commit>^{tree}`` resolves its tree
    object. When the working tree is clean (stash create prints nothing) we
    fall back to the committed ``HEAD^{tree}`` so the value stays deterministic.
    """
    rc, out, _ = await _run_git(["stash", "create"], cwd=wt)
    snapshot = out.strip()
    if rc == 0 and snapshot:
        rc2, tree_out, _ = await _run_git(
            ["rev-parse", f"{snapshot}^{{tree}}"], cwd=wt
        )
        if rc2 == 0 and tree_out.strip():
            return tree_out.strip()
    rc3, head_tree, _ = await _run_git(
        ["rev-parse", "HEAD^{tree}"], cwd=wt
    )
    return head_tree.strip()


async def _default_test_run(tests: Set[str], wt: Path) -> dict:
    """Run the reverse-dep test closure inside the worktree."""
    from .test_runner import TestRunner

    runner = TestRunner(repo_root=Path(wt))
    test_files = tuple(Path(wt) / t for t in tests)
    result = await runner.run(test_files=test_files, sandbox_dir=None)
    return {
        "failed": list(result.failed_tests),
        "total": result.total,
    }


async def _reset_worktree(wt: Path) -> None:
    """Best-effort reset of the ephemeral worktree to its pre-op state."""
    try:
        await _run_git(["checkout", "--", "."], cwd=wt)
    except Exception as exc:  # noqa: BLE001 -- best-effort rollback
        logger.debug("[A1-PR] worktree reset best-effort failed: %s", exc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def run_pr_gate_pipeline(
    *,
    op_id: str,
    candidate_files: Sequence[Tuple[str, str]],  # (relpath, full_content)
    repo_root: str,
    chain,  # DAGProofChain (the SAME instance create_review_pr will verify)
    worktree_factory: Optional[Callable[[str], Awaitable[Path]]] = None,
    worktree_cleanup: Optional[Callable[[Path], Awaitable[None]]] = None,
    sandbox_gate: Optional[Callable[..., Awaitable]] = None,
    blast_gate: Optional[Callable[..., Awaitable]] = None,
    apply_candidate: Optional[
        Callable[[Path, Sequence[Tuple[str, str]]], Awaitable[None]]
    ] = None,
    graph_resolver: Optional[Callable[..., Awaitable]] = None,
    test_run_fn: Optional[Callable[[set, Path], Awaitable[dict]]] = None,
    tree_sha_fn: Optional[Callable[[Path], Awaitable[str]]] = None,
) -> PRGateResult:
    """Run Gate (1) + Gate (2) in an isolated worktree, branch-bound.

    Raises ``PRGatePipelineError`` if either gate rejects the candidate.
    ALWAYS cleans up the worktree (``finally``). The real repo tree is NEVER
    touched.
    """
    branch = f"ouroboros/a1-validate/{op_id}"
    _factory = worktree_factory or _default_worktree_factory(repo_root)
    _cleanup = worktree_cleanup or _default_worktree_cleanup(repo_root)
    wt = await _factory(branch)
    try:
        # Gate 1 -- container-exec the candidate in the worktree, branch-bound.
        from .pre_apply_exec_lock import (
            acquire_sandbox_execution_token,
            SandboxLockFailed,
            RequiresCloudExecution,
        )

        _sbx = sandbox_gate or acquire_sandbox_execution_token
        try:
            sandbox_token = await _sbx(
                op_id=op_id,
                candidate_files=list(candidate_files),
                repo_root=str(wt),
                chain=chain,
                branch_context=branch,
            )
        except (SandboxLockFailed, RequiresCloudExecution) as exc:
            raise PRGatePipelineError(f"gate1: {exc}") from exc

        # Capture pre-apply tree SHA BEFORE writing any candidate files so
        # Gate 2's rollback assertion compares HEAD (pre-op) against HEAD (restored).
        _tree_sha = tree_sha_fn or _default_tree_sha
        _scope = [p for p, _ in candidate_files]
        pre_sha = await _tree_sha(wt)

        # Apply the candidate into the worktree so Gate 2 tests see it.
        await (apply_candidate or _default_apply_candidate)(
            wt, list(candidate_files)
        )

        # Gate 2 -- reverse-dep tests in the worktree, branch-bound.
        from .blast_radius_verify import (
            acquire_blast_radius_token,
            BlastRadiusBreach,
            BlastRadiusGraphFailure,
        )
        from .reverse_dep_resolver import resolve_reverse_dependency_tests

        _resolver = graph_resolver or resolve_reverse_dependency_tests
        _runner = test_run_fn or _default_test_run

        async def _graph_fn(files):
            return await _resolver(files, repo_root=str(wt), oracle=None)

        async def _test_fn(tests):
            return await _runner(set(tests), wt)

        async def _cur_sha():
            return await _tree_sha(wt)

        async def _rollback(_sha):
            # Worktree is ephemeral; reset it to the pre-op state (best-effort).
            await _reset_worktree(wt)

        _blast = blast_gate or acquire_blast_radius_token
        try:
            blast_token = await _blast(
                op_id=op_id,
                scope_files=_scope,
                pre_op_tree_sha=pre_sha,
                chain=chain,
                prev_token=sandbox_token,
                graph_fn=_graph_fn,
                test_fn=_test_fn,
                current_tree_sha_fn=_cur_sha,
                rollback_fn=_rollback,
                dlq_fn=None,
                branch_context=branch,
            )
        except (BlastRadiusBreach, BlastRadiusGraphFailure) as exc:
            raise PRGatePipelineError(f"gate2: {exc}") from exc

        return PRGateResult(
            sandbox_token=sandbox_token,
            blast_token=blast_token,
            branch_context=branch,
        )
    finally:
        try:
            await _cleanup(wt)
        except Exception as exc:  # noqa: BLE001 -- cleanup is best-effort
            logger.warning(
                "[A1-PR] op=%s worktree cleanup failed: %s", op_id, exc
            )
