"""SagaApplyStrategy — multi-repo topological apply with preimage compensation.

Selected by GovernedOrchestrator when ctx.cross_repo is True.
Single-repo path in ChangeEngine is untouched.

Execution phases:
  A — Pre-flight drift check (HEAD anchor verification for all repos)
  B — Staged topological apply (preimage capture already in patch → write → git add)
  C — Compensating rollback in reverse apply order on failure
  D — Terminal state determination
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    RepoSagaStatus,
    SagaStepStatus,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    RepoPatch,
    SagaApplyResult,
    SagaTerminalState,
)

try:
    import backend.core.ouroboros.governance.ledger as _ledger_mod
    _LEDGER_IMPORTS_OK = True
except ImportError:
    _ledger_mod = None  # type: ignore[assignment]
    _LEDGER_IMPORTS_OK = False

logger = logging.getLogger("Ouroboros.SagaApply")


class SagaApplyStrategy:
    """Executes multi-repo applies in topological order with preimage compensation.

    Parameters
    ----------
    repo_roots:
        Mapping of repo name → absolute Path to repo root on disk.
    ledger:
        OperationLedger for sub-event persistence (best-effort).
    """

    def __init__(
        self,
        repo_roots: Dict[str, Any],
        ledger: Any,
    ) -> None:
        self._repo_roots: Dict[str, Path] = {k: Path(v) for k, v in repo_roots.items()}
        self._ledger = ledger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch]
    ) -> SagaApplyResult:
        """Execute the full saga: Phase A → B → C/D."""
        apply_order = self._resolve_apply_order(ctx)
        saga_id = ctx.saga_id or ctx.op_id

        # Initialise per-repo status tracking from any existing saga_state for idempotent resume
        repo_statuses: Dict[str, RepoSagaStatus] = {
            rss.repo: rss for rss in (ctx.saga_state or ())
        }

        # Phase A: Pre-flight drift check
        abort = await self._phase_a_preflight(ctx, apply_order, saga_id)
        if abort is not None:
            return abort

        # Phase B: Staged topological apply
        applied_repos: List[str] = []
        step_index = 0
        failed_repo: Optional[str] = None
        failure_reason = ""
        failure_error = ""

        for repo in apply_order:
            patch = patch_map.get(repo, RepoPatch(repo=repo, files=()))

            if patch.is_empty():
                logger.info("[Saga] %s SKIPPED (empty patch)", repo)
                repo_statuses[repo] = RepoSagaStatus(
                    repo=repo,
                    status=SagaStepStatus.SKIPPED,
                )
                step_index += 1
                continue

            # Re-verify HEAD immediately before writing (TOCTOU guard)
            snapshots = dict(ctx.repo_snapshots)
            expected = snapshots.get(repo, "")
            if expected:
                current = await self._get_head_hash(repo)
                if current != expected:
                    failed_repo = repo
                    failure_reason = "drift_detected_mid_apply"
                    failure_error = f"{repo} HEAD moved during apply"
                    repo_statuses[repo] = RepoSagaStatus(
                        repo=repo,
                        status=SagaStepStatus.FAILED,
                        last_error=failure_error,
                        reason_code=failure_reason,
                    )
                    break

            logger.info("[Saga] Applying %s (step %d)", repo, step_index)

            try:
                await self._apply_patch(repo, patch)
                applied_repos.append(repo)
                step_index += 1
                logger.info("[Saga] %s APPLIED", repo)
                # Issue 1 fix: emit apply_repo AFTER successful write+git-add
                await self._emit_sub_event("apply_repo", saga_id, ctx.op_id, repo=repo)
                repo_statuses[repo] = RepoSagaStatus(
                    repo=repo,
                    status=SagaStepStatus.APPLIED,
                )
            except Exception as exc:
                failed_repo = repo
                failure_reason = "apply_write_error"
                failure_error = f"{type(exc).__name__}: {exc}"
                logger.error("[Saga] Apply failed for %s: %s", repo, exc)
                repo_statuses[repo] = RepoSagaStatus(
                    repo=repo,
                    status=SagaStepStatus.FAILED,
                    last_error=failure_error,
                    reason_code=failure_reason,
                )
                break

        if failed_repo is None:
            await self._emit_sub_event("pre_verify", saga_id, ctx.op_id)
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_APPLY_COMPLETED,
                saga_id=saga_id,
                saga_step_index=step_index,
                error=None,
                saga_state=tuple(repo_statuses.values()),
            )

        # Phase C: Compensating rollback in reverse order
        all_compensated, repo_statuses = await self._phase_c_compensate(
            applied_repos, patch_map, saga_id, ctx.op_id, failure_reason, repo_statuses
        )

        if all_compensated:
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_ROLLED_BACK,
                saga_id=saga_id,
                saga_step_index=step_index,
                error=failure_error,
                reason_code=failure_reason,
                saga_state=tuple(repo_statuses.values()),
            )

        # Issue 3 fix: emit saga.stuck before returning SAGA_STUCK terminal state
        await self._emit_sub_event(
            "stuck", saga_id, ctx.op_id, reason="compensation_failed"
        )
        return SagaApplyResult(
            terminal_state=SagaTerminalState.SAGA_STUCK,
            saga_id=saga_id,
            saga_step_index=step_index,
            error=failure_error,
            reason_code="compensation_failed",
            saga_state=tuple(repo_statuses.values()),
        )

    async def compensate_after_verify_failure(
        self,
        saga_result: "SagaApplyResult",
        patch_map: Dict[str, "RepoPatch"],
        op_id: str,
        reason_code: str,
    ) -> bool:
        """Public compensation path for verify-phase failures.

        Derives applied_repos and repo_statuses from saga_result so the caller
        doesn't need to know the strategy's internal representation.
        Returns True if all compensations succeeded, False if any failed.
        """
        if not saga_result.saga_state:
            logger.error(
                "[Saga] compensate_after_verify_failure: saga_state is empty for saga_id=%s; "
                "cannot determine applied repos. Treating as compensation failure.",
                saga_result.saga_id,
            )
            return False
        applied_repos = [
            rss.repo for rss in saga_result.saga_state
            if rss.status == SagaStepStatus.APPLIED
        ]
        repo_statuses = {rss.repo: rss for rss in saga_result.saga_state}

        all_ok, _ = await self._phase_c_compensate(
            applied_repos=applied_repos,
            patch_map=patch_map,
            saga_id=saga_result.saga_id,
            op_id=op_id,
            failure_reason=reason_code,
            repo_statuses=repo_statuses,
        )
        return all_ok

    # ------------------------------------------------------------------
    # Phase A
    # ------------------------------------------------------------------

    async def _phase_a_preflight(
        self, ctx: OperationContext, apply_order: List[str], saga_id: str
    ) -> Optional[SagaApplyResult]:
        """Verify HEAD anchors for all repos before touching any file."""
        # Issue 4 fix: acquire repo leases in deterministic sorted order before drift check
        self._acquire_repo_leases(list(apply_order))

        await self._emit_sub_event("prepare", saga_id, ctx.op_id)
        snapshots = dict(ctx.repo_snapshots)
        for repo in apply_order:
            expected = snapshots.get(repo, "")
            if not expected:
                continue
            current = await self._get_head_hash(repo)
            if current != expected:
                logger.warning(
                    "[Saga] Drift detected for %s: expected %s, got %s",
                    repo, expected, current,
                )
                return SagaApplyResult(
                    terminal_state=SagaTerminalState.SAGA_ABORTED,
                    saga_id=saga_id,
                    saga_step_index=0,
                    error=f"{repo} HEAD drifted from snapshot",
                    reason_code="drift_detected",
                )
        return None

    # ------------------------------------------------------------------
    # Phase B helpers
    # ------------------------------------------------------------------

    async def _apply_patch(self, repo: str, patch: RepoPatch) -> None:
        """Write all files in patch to disk, then stage with git add."""
        repo_root = self._repo_roots[repo]
        if not repo_root.exists():
            raise FileNotFoundError(f"Repo root does not exist: {repo_root}")
        content_map = dict(patch.new_content)
        written: List[str] = []

        for pf in patch.files:
            full_path = repo_root / pf.path
            new_bytes = content_map.get(pf.path, b"")

            if pf.op == FileOp.CREATE:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(new_bytes)
            elif pf.op == FileOp.MODIFY:
                full_path.write_bytes(new_bytes)
            elif pf.op == FileOp.DELETE:
                if full_path.exists():
                    full_path.unlink()
                else:
                    # File doesn't exist; nothing to restore. Don't add to written list.
                    continue
            written.append(pf.path)

        # Stage all written/deleted files for transactional safety
        if written:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "add", "--"] + written,
                    cwd=str(repo_root),
                    check=True,
                    capture_output=True,
                )
            except Exception as exc:
                logger.warning("[Saga] git add failed for repo %s: %s", repo, exc)
                raise  # triggers Phase B failure → compensation

    # ------------------------------------------------------------------
    # Phase C
    # ------------------------------------------------------------------

    async def _phase_c_compensate(
        self,
        applied_repos: List[str],
        patch_map: Dict[str, RepoPatch],
        saga_id: str,
        op_id: str,
        failure_reason: str,
        repo_statuses: Dict[str, RepoSagaStatus],
    ) -> Tuple[bool, Dict[str, RepoSagaStatus]]:
        """Compensate all applied repos in reverse order.

        Returns (all_succeeded, updated_repo_statuses).
        """
        all_ok = True
        for repo in reversed(applied_repos):
            patch = patch_map[repo]
            await self._emit_sub_event(
                "compensate_repo", saga_id, op_id, repo=repo, reason=failure_reason
            )
            try:
                await self._compensate_patch(repo, patch)
                logger.info("[Saga] Compensated %s", repo)
                repo_statuses[repo] = dataclasses.replace(
                    repo_statuses.get(repo, RepoSagaStatus(repo=repo, status=SagaStepStatus.APPLIED)),
                    status=SagaStepStatus.COMPENSATED,
                    compensation_attempted=True,
                )
            except Exception as exc:
                logger.error("[Saga] Compensation FAILED for %s: %s", repo, exc)
                repo_statuses[repo] = dataclasses.replace(
                    repo_statuses.get(repo, RepoSagaStatus(repo=repo, status=SagaStepStatus.APPLIED)),
                    status=SagaStepStatus.COMPENSATION_FAILED,
                    last_error=str(exc),
                    compensation_attempted=True,
                )
                all_ok = False
        return all_ok, repo_statuses

    async def _compensate_patch(self, repo: str, patch: RepoPatch) -> None:
        """Restore all files in patch to their preimage state."""
        repo_root = self._repo_roots[repo]
        to_unstage: List[str] = []

        for pf in patch.files:
            full_path = repo_root / pf.path
            if pf.op == FileOp.CREATE:
                if full_path.exists():
                    full_path.unlink()
            elif pf.op in (FileOp.MODIFY, FileOp.DELETE):
                # preimage is guaranteed non-None for MODIFY/DELETE by PatchedFile.__post_init__
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(pf.preimage)  # type: ignore[arg-type]
            to_unstage.append(pf.path)

        if to_unstage:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "restore", "--staged", "--"] + to_unstage,
                    cwd=str(repo_root),
                    check=False,
                    capture_output=True,
                )
            except Exception as exc:
                logger.debug("[Saga] git restore --staged failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _acquire_repo_leases(self, repos: List[str]) -> None:
        """Acquire repo leases in deterministic sorted order to prevent cross-saga deadlocks.

        TODO: Integrate with DistributedLockManager when available.
        Currently a no-op stub that establishes the correct call site and acquisition order.
        """
        sorted_repos = sorted(repos)
        logger.debug("[SagaApplyStrategy] Lease acquisition order: %s", sorted_repos)

    async def _get_head_hash(self, repo: str) -> str:
        """Return the current HEAD commit hash for a repo. Returns '' on error."""
        repo_root = self._repo_roots.get(repo)
        if repo_root is None:
            return ""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _resolve_apply_order(self, ctx: OperationContext) -> List[str]:
        """Return apply order: use ctx.apply_plan if set; else topological sort."""
        if ctx.apply_plan:
            return list(ctx.apply_plan)
        return self._topological_sort(
            repo_scope=ctx.repo_scope,
            edges=ctx.dependency_edges,
        )

    def _topological_sort(
        self,
        repo_scope: Tuple[str, ...],
        edges: Tuple[Tuple[str, str], ...],
    ) -> List[str]:
        """Kahn's algorithm topological sort. Stable: alphabetical within same depth."""
        graph: Dict[str, List[str]] = collections.defaultdict(list)
        in_degree: Dict[str, int] = {r: 0 for r in repo_scope}
        for dependent, dependency in edges:
            # edge (dependent, dependency) means dependency must be applied first
            # graph arc: dependency -> dependent
            graph[dependency].append(dependent)
            in_degree[dependent] = in_degree.get(dependent, 0) + 1

        queue = collections.deque(sorted(r for r in repo_scope if in_degree.get(r, 0) == 0))
        result: List[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        if len(result) != len(repo_scope):
            cycle_nodes = [r for r in repo_scope if r not in result]
            raise RuntimeError(
                f"Topological sort incomplete — cycle detected among repos: {cycle_nodes}"
            )
        return result

    async def _emit_sub_event(
        self, event: str, saga_id: str, op_id: str, **kwargs: Any
    ) -> None:
        """Emit a saga sub-event to the ledger (best-effort; failures are logged)."""
        if not _LEDGER_IMPORTS_OK or _ledger_mod is None or self._ledger is None:
            logger.debug("[Saga] ledger unavailable; skipping sub-event emit (%s)", event)
            return
        try:
            entry = _ledger_mod.LedgerEntry(
                op_id=op_id,
                state=_ledger_mod.OperationState.APPLYING,
                data={"saga_event": event, "saga_id": saga_id, **kwargs},
            )
            await self._ledger.append(entry)
        except Exception as exc:
            logger.debug("[Saga] sub-event emit failed (%s): %s", event, exc)
