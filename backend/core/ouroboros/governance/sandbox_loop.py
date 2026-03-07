"""
Sandbox Improvement Loop
========================

Wires together the governance components (risk engine, ledger, comm protocol)
with a code improvement pipeline that runs **entirely** in sandbox isolation.
Production files are NEVER modified.

The loop lifecycle:

1. Generate a unique operation ID.
2. Create an :class:`OperationProfile`, classify risk via :class:`RiskEngine`.
3. Emit INTENT via :class:`CommProtocol`.
4. Append PLANNED to :class:`OperationLedger`.
5. Emit HEARTBEAT (generating phase).
6. Call ``_generate_candidates()`` (virtual — override or mock).
7. Append VALIDATING to ledger.
8. If no candidates: emit DECISION(no_candidates), append FAILED, return.
9. Emit HEARTBEAT (validating phase).
10. Call ``_validate_in_sandbox()`` — AST-parse each candidate in a temp dir.
11. Emit DECISION with outcome.
12. Append final state (APPLIED if best found, FAILED otherwise).
13. Return :class:`SandboxResult`.
14. On exception: emit POSTMORTEM, append FAILED, return error result.

Key invariant: ``target_file`` inside ``project_root`` is NEVER written to.
Only temporary directories are used for validation.
"""

from __future__ import annotations

import ast
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SandboxConfig:
    """Configuration for the sandbox improvement loop.

    Parameters
    ----------
    worktree_base:
        Base directory for git worktrees / temp sandbox dirs.
        Defaults to ``<tempdir>/ouroboros_worktrees``.
    ledger_dir:
        Directory for the operation ledger JSONL files.
        Defaults to ``~/.jarvis/ouroboros/ledger``.
    """

    worktree_base: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "ouroboros_worktrees"
    )
    ledger_dir: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "ledger"
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SandboxResult:
    """Result of a sandbox improvement loop run.

    Parameters
    ----------
    op_id:
        The unique operation identifier for this run.
    success:
        ``True`` if at least one candidate passed sandbox validation.
    candidates_generated:
        Total number of candidates produced by ``_generate_candidates``.
    best_candidate:
        The first candidate that passed AST validation, or ``None``.
    error:
        Error message if the loop failed with an exception.
    """

    op_id: str
    success: bool
    candidates_generated: int = 0
    best_candidate: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# SandboxLoop
# ---------------------------------------------------------------------------


class SandboxLoop:
    """Governance-integrated sandbox improvement loop.

    Orchestrates candidate generation and validation entirely within
    temporary directories, ensuring that production files are never
    modified.

    Parameters
    ----------
    project_root:
        Path to the project root (used for reading source files only).
    config:
        :class:`SandboxConfig` controlling directory paths.
    comm:
        :class:`CommProtocol` for emitting lifecycle messages.
    risk_engine:
        :class:`RiskEngine` for classifying operation risk.
    ledger:
        :class:`OperationLedger` for recording state transitions.
    """

    def __init__(
        self,
        project_root: Path,
        config: Optional[SandboxConfig] = None,
        comm: Optional[CommProtocol] = None,
        risk_engine: Optional[RiskEngine] = None,
        ledger: Optional[OperationLedger] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._config = config or SandboxConfig()
        self._comm = comm or CommProtocol(transports=[LogTransport()])
        self._risk_engine = risk_engine or RiskEngine()

        # Ensure ledger dir exists
        ledger_dir = self._config.ledger_dir
        ledger_dir.mkdir(parents=True, exist_ok=True)
        self._ledger = ledger or OperationLedger(storage_dir=ledger_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        goal: str,
        target_file: str,
        repo_origin: str = "jarvis",
    ) -> SandboxResult:
        """Execute the sandbox improvement loop.

        Parameters
        ----------
        goal:
            Natural-language description of the improvement goal.
        target_file:
            Relative path (from project root) to the file to improve.
        repo_origin:
            Repository origin label for the operation ID.

        Returns
        -------
        SandboxResult
            Result containing the operation ID, success flag, candidate
            count, best candidate (if any), and error (if any).
        """
        # Step 1: Generate operation ID
        op_id = generate_operation_id(repo_origin=repo_origin)

        try:
            # Step 2: Create OperationProfile and classify risk
            profile = OperationProfile(
                files_affected=[Path(target_file)],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.8,
            )
            classification = self._risk_engine.classify(profile)

            # Step 3: Emit INTENT
            await self._comm.emit_intent(
                op_id=op_id,
                goal=goal,
                target_files=[target_file],
                risk_tier=classification.tier.name,
                blast_radius=profile.blast_radius,
            )

            # Step 4: Append PLANNED to ledger
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.PLANNED,
                    data={
                        "goal": goal,
                        "target_file": target_file,
                        "risk_tier": classification.tier.name,
                        "reason_code": classification.reason_code,
                    },
                )
            )

            # Step 5: Emit HEARTBEAT (generating)
            await self._comm.emit_heartbeat(
                op_id=op_id,
                phase="generating",
                progress_pct=25.0,
            )

            # Step 6: Generate candidates
            candidates = await self._generate_candidates(goal, target_file)
            candidates_count = len(candidates)

            # Step 7: Append VALIDATING to ledger
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.VALIDATING,
                    data={
                        "candidates_generated": candidates_count,
                    },
                )
            )

            # Step 8: No candidates -> FAILED
            if not candidates:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="no_candidates",
                    reason_code="generation_empty",
                )
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.FAILED,
                        data={"reason": "no_candidates"},
                    )
                )
                return SandboxResult(
                    op_id=op_id,
                    success=False,
                    candidates_generated=0,
                )

            # Step 9: Emit HEARTBEAT (validating)
            await self._comm.emit_heartbeat(
                op_id=op_id,
                phase="validating",
                progress_pct=50.0,
            )

            # Step 10: Validate in sandbox
            best = await self._validate_in_sandbox(target_file, candidates)

            # Step 11: Emit DECISION
            if best is not None:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="candidate_validated",
                    reason_code="ast_check_passed",
                    diff_summary=best.get("description", ""),
                )
            else:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="all_candidates_failed",
                    reason_code="ast_check_failed",
                )

            # Step 12: Append final state
            if best is not None:
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.APPLIED,
                        data={
                            "best_candidate_description": best.get(
                                "description", ""
                            ),
                        },
                    )
                )
            else:
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.FAILED,
                        data={"reason": "all_candidates_failed_validation"},
                    )
                )

            # Step 13: Return result
            return SandboxResult(
                op_id=op_id,
                success=best is not None,
                candidates_generated=candidates_count,
                best_candidate=best,
            )

        except Exception as exc:
            # Step 14: On exception -> POSTMORTEM + FAILED
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=str(exc),
                failed_phase="sandbox_loop",
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.FAILED,
                    data={"error": str(exc)},
                )
            )
            return SandboxResult(
                op_id=op_id,
                success=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Virtual / overridable methods
    # ------------------------------------------------------------------

    async def _generate_candidates(
        self,
        goal: str,
        target_file: str,
    ) -> List[Dict[str, Any]]:
        """Generate improvement candidates for *target_file*.

        Default implementation returns an empty list.  Override in
        subclasses or mock in tests to provide actual candidates.

        Parameters
        ----------
        goal:
            Natural-language improvement goal.
        target_file:
            Relative path to the target file.

        Returns
        -------
        List[Dict[str, Any]]
            Each dict must contain at least a ``"code"`` key with the
            proposed source code, and optionally a ``"description"``.
        """
        return []

    async def _validate_in_sandbox(
        self,
        target_file: str,
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Validate candidates by AST-parsing in a temporary directory.

        For each candidate, writes the proposed code to a temp file and
        attempts ``ast.parse()``.  Returns the first candidate that
        passes, or ``None`` if all fail.

        **Key invariant:** ``target_file`` in ``project_root`` is never
        written to.  Only temp directories are used.

        Parameters
        ----------
        target_file:
            Relative path to the target file (used for naming only).
        candidates:
            List of candidate dicts, each with a ``"code"`` key.

        Returns
        -------
        Optional[Dict[str, Any]]
            The first passing candidate, or ``None``.
        """
        for candidate in candidates:
            code = candidate.get("code", "")
            try:
                # Validate in a completely isolated temp directory
                with tempfile.TemporaryDirectory(
                    prefix="ouroboros_validate_",
                ) as sandbox_dir:
                    sandbox_path = Path(sandbox_dir) / Path(target_file).name
                    sandbox_path.write_text(code, encoding="utf-8")

                    # Read back and AST-parse to verify syntactic validity
                    source = sandbox_path.read_text(encoding="utf-8")
                    ast.parse(source, filename=str(sandbox_path))

                # If we get here, the candidate is syntactically valid
                return candidate
            except SyntaxError:
                # Candidate has invalid syntax — skip it
                continue

        return None
