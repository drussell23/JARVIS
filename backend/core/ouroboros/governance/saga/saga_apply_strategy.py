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
import os
import re
import subprocess
import time
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

try:
    from backend.core.ouroboros.governance.autonomy.saga_messages import (
        SagaMessage,
        SagaMessageType,
        MessagePriority,
    )
    _BUS_IMPORTS_OK = True
except ImportError:
    _BUS_IMPORTS_OK = False

logger = logging.getLogger("Ouroboros.SagaApply")


def _safe_branch_name(op_id: str, repo: str) -> str:
    """Sanitize op_id + repo into a valid git ref name."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", op_id)[:64]
    safe_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)[:32]
    return f"ouroboros/saga-{safe_id}/{safe_repo}"


_BRANCH_ISOLATION_ENABLED = os.getenv(
    "JARVIS_SAGA_BRANCH_ISOLATION", "false"
).lower() in ("1", "true", "yes")


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
        branch_isolation: Optional[bool] = None,
        keep_failed_saga_branches: bool = True,
        message_bus: Any = None,
    ) -> None:
        self._repo_roots: Dict[str, Path] = {k: Path(v) for k, v in repo_roots.items()}
        self._ledger = ledger
        self._branch_isolation = (
            branch_isolation if branch_isolation is not None else _BRANCH_ISOLATION_ENABLED
        )
        self._keep_failed_branches = keep_failed_saga_branches
        self._bus = message_bus

        # B+ branch state (populated during execute)
        self._saga_branches: Dict[str, str] = {}
        self._original_branches: Dict[str, str] = {}
        self._original_shas: Dict[str, str] = {}
        self._base_shas: Dict[str, str] = {}
        self._lock_manager: Optional[Any] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch]
    ) -> SagaApplyResult:
        """Execute the full saga. Dispatches to B+ or legacy path based on feature flag."""
        apply_order = self._resolve_apply_order(ctx)
        saga_id = ctx.saga_id or ctx.op_id
        repo_statuses: Dict[str, RepoSagaStatus] = {
            rss.repo: rss for rss in (ctx.saga_state or ())
        }

        if not self._branch_isolation:
            return await self._execute_legacy(ctx, patch_map, apply_order, saga_id, repo_statuses)

        from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager
        if self._lock_manager is None:
            self._lock_manager = RepoLockManager()

        await self._lock_manager.acquire(apply_order, self._repo_roots)
        try:
            return await self._execute_bplus(ctx, patch_map, apply_order, saga_id, repo_statuses)
        except BaseException:
            await self._bplus_compensate_all(apply_order, saga_id, ctx.op_id, "exception_during_execute", repo_statuses)
            raise
        finally:
            await self._lock_manager.release(apply_order)

    async def _execute_legacy(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch],
        apply_order: List[str], saga_id: str, repo_statuses: Dict[str, RepoSagaStatus],
    ) -> SagaApplyResult:
        """Legacy direct-to-HEAD apply path (branch_isolation=False)."""
        # Phase A: Pre-flight drift check
        abort = await self._phase_a_preflight(ctx, apply_order, saga_id)
        if abort is not None:
            return abort

        self._bus_emit("saga_created", saga_id, ctx.op_id)

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
                self._bus_emit("saga_advanced", saga_id, ctx.op_id, repo=repo)
                repo_statuses[repo] = RepoSagaStatus(
                    repo=repo,
                    status=SagaStepStatus.APPLIED,
                )
            except Exception as exc:
                failed_repo = repo
                failure_reason = "apply_write_error"
                failure_error = f"{type(exc).__name__}: {exc}"
                logger.error("[Saga] Apply failed for %s: %s", repo, exc)
                self._bus_emit("saga_failed", saga_id, ctx.op_id, repo=repo, reason_code=str(exc))
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
            self._bus_emit("saga_rolled_back", saga_id, ctx.op_id, reason_code=failure_reason)
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

    async def _execute_bplus(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch],
        apply_order: List[str], saga_id: str, repo_statuses: Dict[str, RepoSagaStatus],
    ) -> SagaApplyResult:
        """B+ branch-isolated execution path."""
        for repo in apply_order:
            await self._assert_clean_worktree(repo)
            branch_name, sha = await self._capture_original_ref(repo)
            self._original_branches[repo] = branch_name
            self._original_shas[repo] = sha
            self._base_shas[repo] = sha
            saga_branch = await self._create_ephemeral_branch(repo, ctx.op_id)
            self._saga_branches[repo] = saga_branch

        await self._emit_sub_event("prepare", saga_id, ctx.op_id)
        self._bus_emit("saga_created", saga_id, ctx.op_id)

        applied_repos: List[str] = []
        step_index = 0
        failed_repo: Optional[str] = None
        failure_reason = ""
        failure_error = ""

        for repo in apply_order:
            patch = patch_map.get(repo, RepoPatch(repo=repo, files=()))
            if patch.is_empty():
                logger.info("[Saga-B+] %s SKIPPED (empty patch)", repo)
                repo_statuses[repo] = RepoSagaStatus(repo=repo, status=SagaStepStatus.SKIPPED)
                step_index += 1
                continue
            logger.info("[Saga-B+] Applying %s (step %d)", repo, step_index)
            try:
                await self._apply_patch_bplus(repo, patch, ctx, saga_id)
                applied_repos.append(repo)
                step_index += 1
                await self._emit_sub_event("apply_repo", saga_id, ctx.op_id, repo=repo)
                self._bus_emit("saga_advanced", saga_id, ctx.op_id, repo=repo)
                repo_statuses[repo] = RepoSagaStatus(repo=repo, status=SagaStepStatus.APPLIED)
            except Exception as exc:
                failed_repo = repo
                failure_reason = "apply_write_error"
                failure_error = f"{type(exc).__name__}: {exc}"
                logger.error("[Saga-B+] Apply failed for %s: %s", repo, exc)
                self._bus_emit("saga_failed", saga_id, ctx.op_id, repo=repo, reason_code=str(exc))
                repo_statuses[repo] = RepoSagaStatus(
                    repo=repo, status=SagaStepStatus.FAILED,
                    last_error=failure_error, reason_code=failure_reason,
                )
                break

        if failed_repo is not None:
            await self._bplus_compensate_all(
                apply_order, saga_id, ctx.op_id, failure_reason, repo_statuses,
            )
            self._bus_emit("saga_rolled_back", saga_id, ctx.op_id, reason_code=failure_reason)
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_ROLLED_BACK,
                saga_id=saga_id, saga_step_index=step_index,
                error=failure_error, reason_code=failure_reason,
                saga_state=tuple(repo_statuses.values()),
            )

        await self._emit_sub_event("pre_verify", saga_id, ctx.op_id)
        return SagaApplyResult(
            terminal_state=SagaTerminalState.SAGA_APPLY_COMPLETED,
            saga_id=saga_id, saga_step_index=step_index, error=None,
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

    def _bus_emit(self, msg_type: str, saga_id: str, op_id: str, **kwargs: Any) -> None:
        """Emit a saga lifecycle event to the message bus (fire-and-forget)."""
        if self._bus is None or not _BUS_IMPORTS_OK:
            return
        try:
            self._bus.send(SagaMessage(
                message_type=SagaMessageType(msg_type),
                saga_id=saga_id,
                source_repo=kwargs.pop("repo", "*"),
                correlation_id=saga_id,
                priority=(
                    MessagePriority.HIGH
                    if "fail" in msg_type or "partial" in msg_type or "moved" in msg_type
                    else MessagePriority.NORMAL
                ),
                payload={
                    "schema_version": "1.0",
                    "op_id": op_id,
                    "saga_id": saga_id,
                    "reason_code": kwargs.get("reason_code", ""),
                    **kwargs,
                },
            ))
        except Exception:
            logger.debug("[Saga] bus emit failed for %s (non-fatal)", msg_type)

    # ------------------------------------------------------------------
    # B+ Branch Lifecycle Helpers
    # ------------------------------------------------------------------

    async def _assert_clean_worktree(self, repo: str) -> None:
        """Raise RuntimeError if the working tree has staged/unstaged changes."""
        rc = await self._git_rc(repo, ["diff", "--quiet"])
        staged_rc = await self._git_rc(repo, ["diff", "--cached", "--quiet"])
        if rc != 0 or staged_rc != 0:
            raise RuntimeError(f"dirty_worktree:{repo}")

    async def _capture_original_ref(self, repo: str) -> Tuple[str, str]:
        """Return (branch_name_or_HEAD, sha) for current checkout."""
        repo_root = self._repo_roots[repo]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "symbolic-ref", "--short", "HEAD"],
                cwd=str(repo_root), capture_output=True, text=True, check=True,
            )
            branch = result.stdout.strip()
        except subprocess.CalledProcessError:
            branch = "HEAD"  # detached
        sha = await self._get_head_hash(repo)
        return branch, sha

    async def _create_ephemeral_branch(self, repo: str, op_id: str) -> str:
        """Create and checkout an ephemeral branch from the base SHA."""
        branch = _safe_branch_name(op_id, repo)
        base = self._base_shas[repo]
        await self._git(repo, ["checkout", "-b", branch, base])
        return branch

    async def _check_promote_safe(self, repo: str) -> None:
        """Raise if target branch has advanced since base_sha."""
        original_branch = self._original_branches[repo]
        if original_branch == "HEAD":
            raise RuntimeError(f"TARGET_MOVED:{repo}:detached_head_cannot_promote")
        base = self._base_shas[repo]
        repo_root = self._repo_roots[repo]
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", original_branch],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
        )
        current_target = result.stdout.strip()
        if current_target != base:
            raise RuntimeError(
                f"TARGET_MOVED:{repo}:expected={base[:12]},actual={current_target[:12]}"
            )
        # Ancestry check
        rc_result = await asyncio.to_thread(
            subprocess.run,
            ["git", "merge-base", "--is-ancestor", base, "HEAD"],
            cwd=str(repo_root), capture_output=True,
        )
        if rc_result.returncode != 0:
            raise RuntimeError(f"TARGET_MOVED:{repo}:ancestry_check_failed")

    async def _promote_ephemeral_branch(self, repo: str) -> str:
        """FF-only merge ephemeral branch into original branch. Returns promoted SHA."""
        await self._check_promote_safe(repo)
        original_branch = self._original_branches[repo]
        saga_branch = self._saga_branches[repo]
        repo_root = self._repo_roots[repo]

        # Get current SHA on saga branch
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
        )
        saga_sha = result.stdout.strip()

        # Checkout original branch and ff-only merge
        await self._git(repo, ["checkout", original_branch])
        await self._git(repo, ["merge", "--ff-only", saga_branch])

        # Delete ephemeral branch
        await self._git(repo, ["branch", "-d", saga_branch])
        return saga_sha

    async def _cleanup_ephemeral_branch(self, repo: str) -> None:
        """Restore original branch, optionally delete ephemeral branch."""
        original_branch = self._original_branches.get(repo, "main")
        saga_branch = self._saga_branches.get(repo)
        repo_root = self._repo_roots[repo]

        # Switch back to original branch
        try:
            await self._git(repo, ["checkout", original_branch])
        except Exception:
            # If checkout fails (e.g., conflicts), force checkout
            original_sha = self._original_shas.get(repo, "")
            if original_sha:
                await self._git(repo, ["checkout", "--force", original_sha])

        if saga_branch and not self._keep_failed_branches:
            try:
                await self._git(repo, ["branch", "-D", saga_branch])
            except Exception:
                logger.debug("[Saga-B+] Could not delete branch %s", saga_branch)

    async def _bplus_compensate_all(
        self, apply_order: List[str], saga_id: str, op_id: str,
        reason: str, repo_statuses: Dict[str, RepoSagaStatus],
    ) -> None:
        """Compensate all repos by cleaning up ephemeral branches."""
        for repo in reversed(apply_order):
            if repo in self._saga_branches:
                try:
                    await self._cleanup_ephemeral_branch(repo)
                    await self._emit_sub_event(
                        "compensate_repo", saga_id, op_id, repo=repo, reason=reason
                    )
                except Exception as exc:
                    logger.error("[Saga-B+] Cleanup failed for %s: %s", repo, exc)

    async def _apply_patch_bplus(
        self, repo: str, patch: RepoPatch, ctx: OperationContext, saga_id: str,
    ) -> None:
        """Write files, git add, git commit on ephemeral branch."""
        repo_root = self._repo_roots[repo]
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
                    continue
            written.append(pf.path)
        if not written:
            return
        await self._git(repo, ["add", "--"] + written)
        rc = await self._git_rc(repo, ["diff", "--cached", "--quiet"])
        if rc == 0:
            logger.info("[Saga-B+] %s: no diff after apply, skipping commit", repo)
            await self._emit_sub_event("skipped_no_diff", saga_id, ctx.op_id, repo=repo)
            return
        # Build commit signature showing which subsystems contributed
        _provider = getattr(ctx, "provider_used", "unknown")
        _has_tool_records = bool(getattr(ctx, "tool_execution_records", ()))
        _venom = "Venom" if _has_tool_records else ""
        _consciousness = ""
        try:
            _gls = getattr(self, "_gls", None) or getattr(self._stack, "governed_loop_service", None) if hasattr(self, "_stack") else None
            if _gls and getattr(_gls, "_consciousness_bridge", None):
                _consciousness = "Consciousness"
        except Exception:
            pass
        _subsystems = " + ".join(filter(None, ["Ouroboros", _venom, _consciousness]))

        commit_msg = (
            f"[ouroboros] {ctx.description[:72]}\n\n"
            f"op_id: {ctx.op_id}\n"
            f"saga_id: {saga_id}\n"
            f"repo: {repo}\n"
            f"base_sha: {self._base_shas.get(repo, '')}\n"
            f"provider: {_provider}\n"
            f"phase: apply\n"
            f"schema_version: {ctx.schema_version}\n"
            f"\n"
            f"Generated-By: {_subsystems}\n"
            f"Signed-off-by: JARVIS Ouroboros <ouroboros@jarvis.local>\n"
        )
        env = {
            "GIT_AUTHOR_NAME": "JARVIS Ouroboros",
            "GIT_AUTHOR_EMAIL": "ouroboros@jarvis.local",
            "GIT_COMMITTER_NAME": "JARVIS Ouroboros",
            "GIT_COMMITTER_EMAIL": "ouroboros@jarvis.local",
        }
        await self._git(repo, ["commit", "--no-verify", "-m", commit_msg], env=env)

    async def promote_all(
        self, apply_order: List[str], saga_id: str, op_id: str,
    ) -> Tuple[SagaTerminalState, Dict[str, str]]:
        """Promote all ephemeral branches via ff-only merge.
        Returns (terminal_state, {repo: promoted_sha}).
        """
        if not self._branch_isolation:
            return SagaTerminalState.SAGA_SUCCEEDED, {}
        promoted: Dict[str, str] = {}
        for idx, repo in enumerate(apply_order):
            if repo not in self._saga_branches:
                continue
            try:
                sha = await self._promote_ephemeral_branch(repo)
                promoted[repo] = sha
                await self._emit_sub_event(
                    "promote_repo", saga_id, op_id,
                    repo=repo, promoted_sha=sha, promote_order_index=idx,
                )
                self._bus_emit("saga_advanced", saga_id, op_id, repo=repo, promoted_sha=sha)
            except Exception as exc:
                logger.error("[Saga-B+] Promote failed for %s: %s", repo, exc)
                await self._emit_sub_event(
                    "partial_promote", saga_id, op_id,
                    repo=repo, reason=str(exc), boundary_repo=repo,
                )
                self._bus_emit("saga_partial_promote", saga_id, op_id, repo=repo, reason_code=str(exc))
                # Emit differentiated event for specific failure types
                exc_str = str(exc)
                if "TARGET_MOVED" in exc_str:
                    self._bus_emit(
                        "target_moved", saga_id, op_id,
                        repo=repo, reason_code=exc_str,
                    )
                elif "ANCESTRY" in exc_str:
                    self._bus_emit(
                        "ancestry_violation", saga_id, op_id,
                        repo=repo, reason_code=exc_str,
                    )
                return SagaTerminalState.SAGA_PARTIAL_PROMOTE, promoted
        self._bus_emit("saga_completed", saga_id, op_id)
        return SagaTerminalState.SAGA_SUCCEEDED, promoted

    async def _git(self, repo: str, args: List[str], env: Optional[Dict[str, str]] = None) -> str:
        """Run a git command in the repo. Returns stdout. Raises on failure."""
        repo_root = self._repo_roots[repo]
        full_env = None
        if env:
            full_env = {**os.environ, **env}
        result = await asyncio.to_thread(
            subprocess.run,
            ["git"] + args,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            env=full_env,
        )
        return result.stdout.strip()

    async def _git_rc(self, repo: str, args: List[str]) -> int:
        """Run a git command and return its return code (no exception on failure)."""
        repo_root = self._repo_roots[repo]
        result = await asyncio.to_thread(
            subprocess.run,
            ["git"] + args,
            cwd=str(repo_root),
            capture_output=True,
        )
        return result.returncode
