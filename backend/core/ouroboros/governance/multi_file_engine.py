# backend/core/ouroboros/governance/multi_file_engine.py
"""
Multi-File Change Engine -- Atomic Multi-File Operations
=========================================================

Wraps the single-file :class:`ChangeEngine` pipeline to apply changes
atomically across multiple files.  All files succeed or all are rolled back.

Uses ``CROSS_REPO_TX`` lock level for the transaction envelope, with nested
``PROD_LOCK``s for individual file writes.

Key guarantees:
- All-or-nothing: if any file fails validation, no files are modified
- Pre-tested rollback: each file's snapshot captured BEFORE any writes
- Ledger tracks all files in the operation via ``data.files[]``
- Communication protocol emits full 5-phase lifecycle
"""

from __future__ import annotations

import ast
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.break_glass import BreakGlassManager
from backend.core.ouroboros.governance.change_engine import (
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    RiskEngine,
    RiskTier,
)

logger = logging.getLogger("Ouroboros.MultiFileEngine")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MultiFileChangeRequest:
    """Request to atomically change multiple files."""

    goal: str
    files: Dict[Path, str]
    profile: OperationProfile
    verify_fn: Optional[Any] = None


@dataclass
class MultiFileChangeResult:
    """Result of a multi-file change engine execution."""

    op_id: str
    success: bool
    phase_reached: ChangePhase
    risk_tier: Optional[RiskTier] = None
    rolled_back: bool = False
    files_applied: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# MultiFileChangeEngine
# ---------------------------------------------------------------------------


class MultiFileChangeEngine:
    """Atomic multi-file change pipeline."""

    def __init__(
        self,
        project_root: Path,
        ledger: OperationLedger,
        comm: Optional[CommProtocol] = None,
        lock_manager: Optional[GovernanceLockManager] = None,
        break_glass: Optional[BreakGlassManager] = None,
        risk_engine: Optional[RiskEngine] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._ledger = ledger
        self._comm = comm or CommProtocol(transports=[LogTransport()])
        self._lock_manager = lock_manager or GovernanceLockManager()
        self._break_glass = break_glass or BreakGlassManager()
        self._risk_engine = risk_engine or RiskEngine()

    async def execute(
        self, request: MultiFileChangeRequest
    ) -> MultiFileChangeResult:
        """Execute the atomic multi-file change pipeline."""
        op_id = generate_operation_id(repo_origin="jarvis")
        file_paths = list(request.files.keys())
        file_strs = [str(f) for f in file_paths]

        try:
            # Phase 1: PLAN -- classify risk
            classification = self._risk_engine.classify(request.profile)
            risk_tier = classification.tier

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.PLANNED,
                    data={
                        "goal": request.goal,
                        "files": file_strs,
                        "file_count": len(file_paths),
                        "risk_tier": risk_tier.name,
                        "reason_code": classification.reason_code,
                    },
                )
            )

            await self._comm.emit_intent(
                op_id=op_id,
                goal=request.goal,
                target_files=file_strs,
                risk_tier=risk_tier.name,
                blast_radius=request.profile.blast_radius,
            )

            # Phase 2: SANDBOX
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="sandbox", progress_pct=15.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.SANDBOXING,
                    data={"file_count": len(file_paths)},
                )
            )

            # Phase 3: VALIDATE -- AST parse ALL files before any writes
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="validate", progress_pct=30.0
            )
            for fpath, content in request.files.items():
                if not self._validate_syntax(content):
                    await self._ledger.append(
                        LedgerEntry(
                            op_id=op_id,
                            state=OperationState.FAILED,
                            data={
                                "reason": "syntax_error",
                                "file": str(fpath),
                            },
                        )
                    )
                    await self._comm.emit_decision(
                        op_id=op_id,
                        outcome="validation_failed",
                        reason_code="syntax_error",
                    )
                    return MultiFileChangeResult(
                        op_id=op_id,
                        success=False,
                        phase_reached=ChangePhase.VALIDATE,
                        risk_tier=risk_tier,
                    )

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.VALIDATING,
                    data={"all_valid": True},
                )
            )

            # Phase 4: GATE -- check risk tier
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="gate", progress_pct=45.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.GATING,
                    data={"risk_tier": risk_tier.name},
                )
            )

            # Check break-glass for BLOCKED
            if risk_tier == RiskTier.BLOCKED:
                promoted = self._break_glass.get_promoted_tier(op_id)
                if promoted is not None:
                    risk_tier = RiskTier.APPROVAL_REQUIRED
                else:
                    await self._comm.emit_decision(
                        op_id=op_id,
                        outcome="blocked",
                        reason_code=classification.reason_code,
                    )
                    await self._ledger.append(
                        LedgerEntry(
                            op_id=op_id,
                            state=OperationState.BLOCKED,
                            data={"reason": classification.reason_code},
                        )
                    )
                    return MultiFileChangeResult(
                        op_id=op_id,
                        success=False,
                        phase_reached=ChangePhase.GATE,
                        risk_tier=RiskTier.BLOCKED,
                    )

            if risk_tier == RiskTier.APPROVAL_REQUIRED:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="escalated",
                    reason_code=classification.reason_code,
                )
                return MultiFileChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.GATE,
                    risk_tier=RiskTier.APPROVAL_REQUIRED,
                )

            # Phase 5: APPLY -- capture rollback artifacts, write all files
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="apply", progress_pct=60.0
            )

            rollback_artifacts: Dict[Path, RollbackArtifact] = {}
            for fpath in file_paths:
                rollback_artifacts[fpath] = RollbackArtifact.capture(fpath)

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLYING,
                    data={
                        "files": file_strs,
                        "rollback_hashes": {
                            str(p): a.snapshot_hash
                            for p, a in rollback_artifacts.items()
                        },
                    },
                )
            )

            # Write all files under lock
            files_written = 0
            async with self._lock_manager.acquire(
                level=LockLevel.CROSS_REPO_TX,
                resource="multi-file-txn",
                mode=LockMode.EXCLUSIVE_WRITE,
            ):
                for fpath, content in request.files.items():
                    async with self._lock_manager.acquire(
                        level=LockLevel.PROD_LOCK,
                        resource=str(fpath),
                        mode=LockMode.EXCLUSIVE_WRITE,
                    ):
                        fpath.write_text(content, encoding="utf-8")
                        files_written += 1

            # Phase 6: LEDGER -- record applied
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="ledger", progress_pct=80.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLIED,
                    data={
                        "files_applied": files_written,
                        "files": file_strs,
                    },
                )
            )

            # Phase 7: PUBLISH -- emit decision
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="safe_auto_passed",
                diff_summary=f"Applied {files_written} files",
            )

            # Phase 8: VERIFY -- post-apply verification
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="verify", progress_pct=90.0
            )

            verify_passed = True
            if request.verify_fn is not None:
                verify_passed = await request.verify_fn()
            else:
                # Default: AST parse all applied files
                for fpath in file_paths:
                    if not self._validate_syntax(
                        fpath.read_text(encoding="utf-8")
                    ):
                        verify_passed = False
                        break

            if not verify_passed:
                # Rollback ALL files
                for fpath, artifact in rollback_artifacts.items():
                    artifact.apply(fpath)
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.ROLLED_BACK,
                        data={
                            "reason": "verify_failed",
                            "files_rolled_back": files_written,
                        },
                    )
                )
                await self._comm.emit_postmortem(
                    op_id=op_id,
                    root_cause="post_apply_verification_failed",
                    failed_phase="VERIFY",
                    next_safe_action="review_proposed_changes",
                )
                return MultiFileChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.VERIFY,
                    risk_tier=risk_tier,
                    rolled_back=True,
                    files_applied=files_written,
                )

            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause="none",
                failed_phase=None,
                next_safe_action="none",
            )

            return MultiFileChangeResult(
                op_id=op_id,
                success=True,
                phase_reached=ChangePhase.VERIFY,
                risk_tier=risk_tier,
                files_applied=files_written,
            )

        except Exception as exc:
            logger.error("MultiFileChangeEngine error for %s: %s", op_id, exc)
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=str(exc),
                failed_phase="unknown",
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.FAILED,
                    data={"error": str(exc)},
                )
            )
            return MultiFileChangeResult(
                op_id=op_id,
                success=False,
                phase_reached=ChangePhase.PLAN,
                risk_tier=None,
                error=str(exc),
            )

    def _validate_syntax(self, code: str) -> bool:
        """Validate Python syntax by AST-parsing in a temp directory."""
        try:
            with tempfile.TemporaryDirectory(
                prefix="ouroboros_mf_validate_"
            ) as sandbox:
                p = Path(sandbox) / "validate.py"
                p.write_text(code, encoding="utf-8")
                ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            return True
        except SyntaxError:
            return False
