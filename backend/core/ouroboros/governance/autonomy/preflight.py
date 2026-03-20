"""Preflight invariant checks for Autonomy Iteration Mode.

Runs immediately before ``scheduler.submit()`` to ensure the world has not
shifted out from under the planner since the execution graph was generated.

Returns ``None`` if all checks pass, or a non-empty error message string on
the first failing invariant.  Callers should treat a non-None return as a
hard veto and must not submit the graph.

Checks (in order):
    1. Repo HEAD unchanged   (T19) -- git rev-parse HEAD vs context.repo_commit
    2. Trust tier not demoted since planning
    3. Budget still has positive headroom
    4. Blast radius still within policy
    5. Policy hash matches  (T28) -- current_policy_hash vs context.policy_hash
    6. No owned-path conflicts with in-flight graphs
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import FrozenSet, Optional, Tuple

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    BlastRadiusPolicy,
    PlanningContext,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import ExecutionGraph
from backend.core.ouroboros.governance.autonomy.tiers import TIER_ORDER, AutonomyTier

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _read_git_head(repo_root: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (commit_sha, error_msg) for git rev-parse HEAD.

    On any subprocess or I/O failure the commit is None and the error
    message is populated.  The caller decides how to surface the failure.
    Uses create_subprocess_exec (not shell=True) to avoid shell injection.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 -- intentionally broad; git may not exist
        _LOG.warning("preflight: git subprocess error: %s", exc)
        return None, "git subprocess error: %s" % exc

    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace").strip() if stderr else "unknown error"
        _LOG.warning(
            "preflight: git rev-parse HEAD exited %d: %s", proc.returncode, err_text
        )
        return None, "git rev-parse HEAD failed (exit %d): %s" % (proc.returncode, err_text)

    commit = stdout.decode(errors="replace").strip()
    return commit, None


def _collect_owned_paths(graph: ExecutionGraph) -> FrozenSet[str]:
    """Collect the union of all effective_owned_paths across graph work units."""
    paths: set = set()
    for unit in graph.units:
        paths.update(unit.effective_owned_paths)
    return frozenset(paths)


def _tier_index(tier: AutonomyTier) -> int:
    """Return the ordinal index of tier in TIER_ORDER; higher = more trusted."""
    return TIER_ORDER.index(tier)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def preflight_check(
    graph: ExecutionGraph,
    context: PlanningContext,
    current_trust_tier: AutonomyTier,
    budget_remaining_usd: float,
    blast_radius: BlastRadiusPolicy,
    repo_root: Path,
    current_policy_hash: str,
    inflight_owned_paths: FrozenSet[str] = frozenset(),
) -> Optional[str]:
    """Run 6 invariant checks before graph submission.

    Parameters
    ----------
    graph:
        The execution graph about to be submitted.
    context:
        Planning context captured when the graph was generated.
    current_trust_tier:
        The autonomy tier active at submission time.
    budget_remaining_usd:
        Remaining USD budget available for this session.
    blast_radius:
        Blast-radius policy to enforce against the graph's owned paths.
    repo_root:
        Filesystem path of the repository root (used for git rev-parse).
    current_policy_hash:
        SHA-256 hex digest of the current stop/governance policy.
    inflight_owned_paths:
        Union of owned paths across all graphs currently executing.  Used to
        detect write-write conflicts before submission.

    Returns
    -------
    Optional[str]
        None when all checks pass.  A non-empty human-readable error string
        describing the first failing invariant otherwise.
    """
    graph_id = getattr(graph, "graph_id", "<unknown>")

    # ------------------------------------------------------------------
    # Check 1 (T19): Repo HEAD must not have changed since planning.
    # ------------------------------------------------------------------
    current_head, git_err = await _read_git_head(repo_root)
    if git_err is not None:
        return "preflight[%s]: cannot verify repo HEAD -- %s" % (graph_id, git_err)

    if current_head != context.repo_commit:
        return (
            "preflight[%s]: stale snapshot -- repo HEAD moved "
            "from %r to %r since planning"
            % (graph_id, context.repo_commit, current_head)
        )

    # ------------------------------------------------------------------
    # Check 2: Trust tier must not have been demoted since planning.
    # ------------------------------------------------------------------
    planned_tier_idx = _tier_index(context.trust_tier)
    current_tier_idx = _tier_index(current_trust_tier)
    if current_tier_idx < planned_tier_idx:
        return (
            "preflight[%s]: trust tier demoted from %r to %r "
            "since planning -- re-plan required"
            % (graph_id, context.trust_tier.value, current_trust_tier.value)
        )

    # ------------------------------------------------------------------
    # Check 3: Budget must have positive headroom.
    # ------------------------------------------------------------------
    if budget_remaining_usd <= 0.0:
        return (
            "preflight[%s]: budget exhausted (budget_remaining_usd=%.4f)"
            % (graph_id, budget_remaining_usd)
        )

    # ------------------------------------------------------------------
    # Check 4: Blast radius must be within policy limits.
    # ------------------------------------------------------------------
    owned_paths = _collect_owned_paths(graph)
    file_count = len(owned_paths)
    blast_violation = blast_radius.check_file_count(file_count)
    if blast_violation:
        return "preflight[%s]: blast radius violation -- %s" % (graph_id, blast_violation)

    # ------------------------------------------------------------------
    # Check 5 (T28): Policy hash must not have changed since planning.
    # ------------------------------------------------------------------
    if current_policy_hash != context.policy_hash:
        return (
            "preflight[%s]: policy hash mismatch -- "
            "planned with %r but current hash is %r -- re-plan required"
            % (graph_id, context.policy_hash, current_policy_hash)
        )

    # ------------------------------------------------------------------
    # Check 6: No owned-path conflicts with in-flight graphs.
    # ------------------------------------------------------------------
    if inflight_owned_paths:
        conflicts = owned_paths & inflight_owned_paths
        if conflicts:
            sorted_conflicts = sorted(conflicts)
            return (
                "preflight[%s]: path conflict with in-flight graph(s) -- "
                "overlapping paths: %s"
                % (graph_id, sorted_conflicts)
            )

    _LOG.debug(
        "preflight[%s]: all 6 checks passed (commit=%s, tier=%s, budget=%.4f, "
        "files=%d, policy_hash=%s)",
        graph_id,
        current_head,
        current_trust_tier.value,
        budget_remaining_usd,
        file_count,
        current_policy_hash,
    )
    return None
