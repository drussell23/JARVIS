"""
Transactional Change Engine
============================

Implements the 8-phase change pipeline from the design doc::

    PLAN -> SANDBOX -> VALIDATE -> GATE -> APPLY -> LEDGER -> PUBLISH -> VERIFY

Each phase is idempotent and recorded in the operation ledger.  Rollback
artifacts are captured BEFORE any production write, so rollback is a
pre-tested operation (not "git revert and pray").

Key guarantees:
- Ledger entry exists for every state transition
- Event published ONLY after ledger commit succeeds (outbox pattern)
- Rollback hash matches pre-change snapshot hash exactly
- Production files untouched until APPLY phase (after all gates pass)
"""

from __future__ import annotations

import ast
import enum
import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from backend.core.ouroboros.governance.break_glass import BreakGlassManager
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
    ChangeType,
    OperationProfile,
    RiskClassification,
    RiskEngine,
    RiskTier,
)
from backend.core.ouroboros.governance.tool_hook_registry import (
    HookDecision,
    ToolCallHookRegistry,
)

logger = logging.getLogger("Ouroboros.ChangeEngine")


# ---------------------------------------------------------------------------
# Anti-Venom (Task 6) — the universal mutation chokepoint
# ---------------------------------------------------------------------------


class PhantomWriteException(RuntimeError):
    """The Absolute I/O Verification Gate physically re-read the just-written file
    and the bytes on disk do NOT match what APPLY claims to have written. FAIL-
    CLOSED: APPLY must never transition to written=True on a phantom (empty/stale)
    disk -- this raises and crashes the op instead of asserting a false success.
    Gated by ``JARVIS_CHANGE_ENGINE_IO_VERIFY`` (default ON)."""


class BlockedPathError(Exception):
    """A write target failed containment / protected-path / immutable-governance.

    Raised exclusively by :meth:`ChangeEngine._pre_write_gate`. The gate is
    fail-closed: ANY internal error (canonicalization failure, import failure,
    guardian crash) raises this rather than allowing the write. The outer
    ``execute`` try/except records the op FAILED and the bytes never reach disk.
    """


# Hardcoded — NO env off-switch. The immovability IS the security property
# (mirrors the Immutable-Orange protocol). These are the governance files that
# enforce safety; a compromised or hallucinating model must never be able to
# clobber the very machinery that gates it. Self-protecting: ``change_engine``
# and ``sandbox_exec`` are themselves listed. Each entry is the repo-relative
# POSIX substring of a governance module — matched against the repo-relative
# path of the (canonicalized) write target.
_IMMUTABLE_GOVERNANCE_SENTINELS: frozenset = frozenset({
    "backend/core/ouroboros/governance/change_engine",       # the enforcer
    "backend/core/ouroboros/governance/sandbox_exec",        # the isolation enforcer
    "backend/core/ouroboros/governance/semantic_guardian",
    "backend/core/ouroboros/governance/tool_executor",
    "backend/core/ouroboros/governance/risk_engine",
    "backend/core/ouroboros/governance/risk_tier_floor",
    "backend/core/ouroboros/governance/semantic_firewall",
    "backend/core/ouroboros/governance/scoped_tool_access",
    "backend/core/ouroboros/governance/orchestrator",
    "backend/core/ouroboros/governance/governed_loop_service",
    "backend/core/ouroboros/governance/intake/unified_intake_router",
})


# ---------------------------------------------------------------------------
# Anti-Venom module-level helpers (reused by ChangeEngine + SagaApplyStrategy)
# ---------------------------------------------------------------------------


def _sentinel_matches_path(path: str, sentinel: str) -> bool:
    """Return True when *sentinel* appears in *path* at a path-component boundary.

    The character immediately following the sentinel match must be ``'.'``,
    ``'/'``, or end-of-string.  This prevents ``risk_engine`` from matching
    ``risk_engine_helpers.py`` (char after = ``'_'``) while still blocking
    ``risk_engine.py`` (char after = ``'.'``) and ``risk_engine/sub.py``
    (char after = ``'/'``).  Security-safe: when in doubt, always block
    ``governance/`` — a non-matching suffix never defeats the sentinel for
    the real governance files.
    """
    idx = path.find(sentinel)
    while idx != -1:
        after = idx + len(sentinel)
        if after >= len(path) or path[after] in (".", "/"):
            return True
        idx = path.find(sentinel, idx + 1)
    return False


def assert_write_path_allowed(target: Path, write_root: Path) -> None:
    """PATH-only pre-write safety check. FAIL-CLOSED.

    Shared chokepoint used by :meth:`ChangeEngine._pre_write_gate` (steps
    1-4) and by ``SagaApplyStrategy`` write sites so that cross-repo saga
    writes receive the same containment + immutable-governance +
    protected-path gates as governed single-repo changes.

    Enforces, in order:

      1. **Canonicalize** — ``realpath(abspath)`` defeats ``../`` traversal
         and symlink redirection.
      2. **Containment** — ``target`` must live under ``write_root``.
      3. **Immutable governance** (FIRST authority; no env off-switch) —
         boundary-anchored sentinel match so ``risk_engine_helpers.py`` is
         NOT blocked while ``risk_engine.py`` IS.
      4. **Protected-path** — reuses Venom's ``_is_protected_path`` registry
         (``.git``, secrets, ``.env``, governance core, …).

    Does **not** run the SemanticGuardian (content-aware; stays in
    :meth:`ChangeEngine._pre_write_gate`).

    Raises
    ------
    BlockedPathError
        On any violation OR any internal error (fail-closed: every failure
        mode raises this rather than allowing the write).
    """
    try:
        real = os.path.realpath(os.path.abspath(str(target)))
        root = os.path.realpath(os.path.abspath(str(write_root)))

        # 1. Containment
        if not (real == root or real.startswith(root + os.sep)):
            raise BlockedPathError(
                f"path escapes write_root: {real} not under {root}"
            )

        rel_norm = os.path.relpath(real, root).replace(os.sep, "/")
        real_posix = real.replace(os.sep, "/")

        # 2. Immutable governance (FIRST authority; no env off-switch).
        #    Boundary-anchored: char after sentinel match must be '.', '/',
        #    or end-of-string — prevents suffix false-positives while still
        #    blocking all real governance files.  Both rel_norm and
        #    real_posix are checked for robustness in deep/worktree roots.
        for sentinel in _IMMUTABLE_GOVERNANCE_SENTINELS:
            if (
                _sentinel_matches_path(rel_norm, sentinel)
                or _sentinel_matches_path(real_posix, sentinel)
            ):
                raise BlockedPathError(
                    f"immutable governance write blocked: {rel_norm}"
                )

        # 3. Protected-path (fail closed on import failure)
        try:
            from backend.core.ouroboros.governance.tool_executor import (
                _is_protected_path,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            raise BlockedPathError(
                f"protected-path check unavailable (import failed): {exc}"
            )
        prot = _is_protected_path(rel_norm)
        if prot:
            raise BlockedPathError(
                f"protected path write blocked: {rel_norm} ({prot})"
            )
    except BlockedPathError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BlockedPathError(
            f"assert_write_path_allowed internal error — fail-closed: "
            f"{type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# Ouroboros code signature (Manifesto §7 — Absolute Observability)
# ---------------------------------------------------------------------------

_SIGNATURE_ENABLED = os.environ.get("OUROBOROS_CODE_SIGNATURE", "1").lower() in (
    "1", "true", "yes",
)


def _inject_ouroboros_signature(
    content: str,
    op_id: str,
    goal: str,
    target_path: str,
) -> str:
    """Inject an Ouroboros attribution comment into the changed content.

    Adds a comment block near the top of the file (after any existing
    module docstring / shebang / encoding declarations) so the user
    can see in their IDE diff exactly what Ouroboros changed and why.

    The signature is deterministic (Manifesto §7 Tier 0) — no model
    call, just structured metadata.
    """
    if not _SIGNATURE_ENABLED:
        return content

    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    goal_short = goal[:120].replace("\n", " ")

    # Determine comment style from file extension
    ext = Path(target_path).suffix.lower()
    if ext in (".py", ".pyi", ".sh", ".yaml", ".yml", ".toml"):
        sig = (
            f"# [Ouroboros] Modified by Ouroboros (op={op_id[:12]}) at {ts}\n"
            f"# Reason: {goal_short}\n"
        )
    elif ext in (".js", ".ts", ".jsx", ".tsx", ".swift", ".java", ".c", ".cpp", ".go", ".rs"):
        sig = (
            f"// [Ouroboros] Modified by Ouroboros (op={op_id[:12]}) at {ts}\n"
            f"// Reason: {goal_short}\n"
        )
    else:
        # Unknown extension — use hash-style comment
        sig = (
            f"# [Ouroboros] Modified by Ouroboros (op={op_id[:12]}) at {ts}\n"
            f"# Reason: {goal_short}\n"
        )

    # Insert after shebang / encoding / module docstring preamble.
    # Find the first non-preamble line to insert before.
    lines = content.split("\n")
    insert_at = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip shebang
        if i == 0 and stripped.startswith("#!"):
            insert_at = i + 1
            continue
        # Skip encoding declaration
        if i <= 1 and stripped.startswith("# -*- coding"):
            insert_at = i + 1
            continue
        # Skip existing Ouroboros signatures (don't stack them)
        if stripped.startswith("# [Ouroboros]") or stripped.startswith("// [Ouroboros]"):
            insert_at = i + 1
            continue
        # Skip blank lines at the very top
        if i <= insert_at and not stripped:
            insert_at = i + 1
            continue
        break

    lines.insert(insert_at, sig)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChangePhase(enum.Enum):
    """The 8 phases of the transactional change pipeline."""

    PLAN = "PLAN"
    SANDBOX = "SANDBOX"
    VALIDATE = "VALIDATE"
    GATE = "GATE"
    APPLY = "APPLY"
    LEDGER = "LEDGER"
    PUBLISH = "PUBLISH"
    VERIFY = "VERIFY"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


# PRD §3.6.2 vector #8 closure (Wave 3 hygiene Item 6, 2026-05-05):
# canonical schema-version constant for the RollbackArtifact contract.
# Bump on field add/remove/rename so cross-runner readers can branch
# at deserialization time. See `meta.versioned_artifact` §33.5 pattern.
ROLLBACK_ARTIFACT_SCHEMA_VERSION: str = "rollback_artifact.1"


@dataclass
class RollbackArtifact:
    """Pre-captured snapshot for deterministic rollback.

    Captures the exact content and hash of a file BEFORE modification,
    so rollback restores to a known-good state.

    Two capture modes:

    1. **existed=True** (default): the file was present at capture time.
       ``original_content`` and ``snapshot_hash`` contain the pre-write
       state, and ``apply()`` rolls back by writing that content back
       and verifying the hash matches.
    2. **existed=False**: the file did not exist at capture time (new-
       file creation path). ``original_content`` is empty and
       ``snapshot_hash`` is the sentinel ``"absent"`` for ledger
       clarity. ``apply()`` rolls back by ``unlink()``-ing the created
       file. No post-unlink hash check — there's nothing to hash.

    Session bt-2026-04-15-091555 (Session K, 2026-04-15) diagnosed the
    new-file case: ``capture()`` unconditionally called ``read_text()``
    and raised ``FileNotFoundError: [Errno 2]`` on the first
    autonomous multi-file generation attempt to reach APPLY phase,
    aborting the entire 4-file batch at progress=70% even though every
    upstream gate (ledger, L2, GATE, NOTIFY_APPLY) had already passed.

    **Versioned artifact contract** (§33.5): ``schema_version`` carries
    the canonical version string so future cross-runner consumers
    (audit ledgers, replay determinism harness) can detect drift.
    Currently in-process only; the field is a forward-compat
    investment.
    """

    original_content: str
    snapshot_hash: str
    existed: bool = True
    schema_version: str = ROLLBACK_ARTIFACT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Project to dict for JSON serialization. Symmetric to
        :meth:`from_dict` — round-trips preserve all fields."""
        return {
            "schema_version": self.schema_version,
            "original_content": self.original_content,
            "snapshot_hash": self.snapshot_hash,
            "existed": self.existed,
        }

    @classmethod
    def from_dict(
        cls, raw: dict,
    ) -> "Optional[RollbackArtifact]":
        """Defensive parse — returns ``None`` on missing /
        malformed fields. Caller verifies schema_version via
        :func:`meta.versioned_artifact.verify_artifact_schema`
        before invoking. NEVER raises."""
        try:
            if not isinstance(raw, dict):
                return None
            return cls(
                original_content=str(
                    raw.get("original_content", ""),
                ),
                snapshot_hash=str(raw.get("snapshot_hash", "")),
                existed=bool(raw.get("existed", True)),
                schema_version=str(
                    raw.get(
                        "schema_version",
                        ROLLBACK_ARTIFACT_SCHEMA_VERSION,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None

    @classmethod
    def capture(cls, file_path: Path) -> "RollbackArtifact":
        """Capture a rollback artifact from the current file state.

        For a **new file** (not yet on disk), returns an "absent"
        artifact whose rollback action is to ``unlink()`` the created
        file rather than restore content. The default ``existed=True``
        preserves the pre-patch behavior for all existing callers that
        construct ``RollbackArtifact`` directly.
        """
        if not file_path.exists():
            return cls(
                original_content="",
                snapshot_hash="absent",
                existed=False,
            )
        content = file_path.read_text(encoding="utf-8")
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        return cls(
            original_content=content,
            snapshot_hash=content_hash,
            existed=True,
        )

    def apply(self, file_path: Path) -> None:
        """Restore the file to the captured snapshot state.

        For ``existed=False`` artifacts (new-file rollback), this
        ``unlink()``-s the created file. There is no post-unlink hash
        check — a deleted file has no content to verify, and a
        missing file is the exact post-state the rollback is trying
        to restore. ``FileNotFoundError`` is swallowed as a no-op:
        the file is already gone, which is the desired end state.

        For ``existed=True`` artifacts, writes back the captured
        content and verifies the hash matches, raising ``RuntimeError``
        on any discrepancy.
        """
        if not self.existed:
            try:
                file_path.unlink()
            except FileNotFoundError:
                pass  # Already absent — desired end state, not an error
            return
        file_path.write_text(self.original_content, encoding="utf-8")
        # Verify the restoration
        restored = file_path.read_text(encoding="utf-8")
        restored_hash = hashlib.sha256(restored.encode()).hexdigest()
        if restored_hash != self.snapshot_hash:
            raise RuntimeError(
                f"Rollback verification failed: expected hash "
                f"{self.snapshot_hash}, got {restored_hash}"
            )


@dataclass
class ChangeRequest:
    """A request to apply a code change through the transactional pipeline.

    Parameters
    ----------
    goal:
        Natural-language description of the change.
    target_file:
        Absolute path to the file to modify.
    proposed_content:
        The new content to write to the file.
    profile:
        Operation risk profile for classification.
    verify_fn:
        Optional async callable that returns True if post-apply verification
        passes.  Defaults to AST parse check.
    break_glass_op_id:
        If set, use this op_id to look up a break-glass token.
    """

    goal: str
    target_file: Path
    proposed_content: str
    profile: OperationProfile
    verify_fn: Optional[Any] = None
    break_glass_op_id: Optional[str] = None
    op_id: Optional[str] = None
    # Slice 64 — per-request APPLY write root. When set (e.g. a swe_bench_pro
    # op's prepared per-problem worktree, carried in the envelope repo_root),
    # it takes precedence over JARVIS_AUTO_COMMIT_WORKSPACE so the patch lands
    # in the correct tree (the cloned benchmark repo), not the JARVIS auto-
    # commit workspace. None = byte-identical legacy resolution.
    write_root: Optional[Path] = None


@dataclass
class ChangeResult:
    """Result of a change engine execution.

    Parameters
    ----------
    op_id:
        The unique operation identifier.
    success:
        Whether the change was successfully applied and verified.
    phase_reached:
        The last phase the pipeline reached.
    risk_tier:
        The risk classification assigned.
    rolled_back:
        Whether the change was rolled back after a verification failure.
    error:
        Error message if the pipeline failed.
    """

    op_id: str
    success: bool
    phase_reached: ChangePhase
    risk_tier: Optional[RiskTier] = None
    rolled_back: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# ChangeEngine
# ---------------------------------------------------------------------------


class ChangeEngine:
    """8-phase transactional change pipeline with rollback guarantees.

    Parameters
    ----------
    project_root:
        Root directory of the project.
    ledger:
        Operation ledger for state tracking.
    comm:
        Communication protocol for lifecycle messages.
    lock_manager:
        Governance lock manager for hierarchy enforcement.
    break_glass:
        Break-glass manager for BLOCKED operation promotion.
    risk_engine:
        Risk classifier (defaults to standard RiskEngine).
    tool_hook_registry:
        Optional ToolCallHookRegistry whose pre-hooks run before every file
        write and whose post-hooks run after.  Pass ``None`` to disable hook
        interception (default).
    """

    def __init__(
        self,
        project_root: Path,
        ledger: OperationLedger,
        comm: Optional[CommProtocol] = None,
        lock_manager: Optional[GovernanceLockManager] = None,
        break_glass: Optional[BreakGlassManager] = None,
        risk_engine: Optional[RiskEngine] = None,
        tool_hook_registry: Optional[Any] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._ledger = ledger
        self._comm = comm or CommProtocol(transports=[LogTransport()])
        self._lock_manager = lock_manager or GovernanceLockManager()
        self._break_glass = break_glass or BreakGlassManager()
        self._risk_engine = risk_engine or RiskEngine()
        self._tool_hook_registry: Optional[ToolCallHookRegistry] = tool_hook_registry

    # ------------------------------------------------------------------
    # Slice 56 — cross-worktree write alignment (Option A)
    # ------------------------------------------------------------------

    def _effective_write_root(self, request_write_root: Optional[Path] = None) -> Path:
        """Resolve the root that APPLY writes into.

        Precedence (Slice 64):
          1. ``request_write_root`` — a per-op root (e.g. a swe_bench_pro
             prepared per-problem worktree from the envelope repo_root). Wins
             so the patch lands in the correct cloned-repo tree, not the JARVIS
             auto-commit workspace (the bt-2026-06-02-081453 mis-route).
          2. ``JARVIS_AUTO_COMMIT_WORKSPACE`` (harness ledger-sovereignty owned
             worktree) — writes land in the SAME tree AutoCommitter commits from
             (coherent by construction); the operator's main tree is untouched.
          3. ``self._project_root`` (byte-identical legacy path).
        """
        if request_write_root is not None:
            return Path(request_write_root)
        override = os.environ.get("JARVIS_AUTO_COMMIT_WORKSPACE")
        if override:
            return Path(override)
        return self._project_root

    def _redirect_target(
        self, target: Path, request_write_root: Optional[Path] = None,
    ) -> Path:
        """Rebase a write target onto :meth:`_effective_write_root`.

        No-op when the effective root equals ``project_root`` (no per-request
        root and no env override). Absolute paths are rebased by their path
        relative to ``project_root``; relative paths are joined to the write
        root. A target NOT under ``project_root`` is left unchanged (defensive —
        never silently cross-write elsewhere). NEVER raises — falls back to the
        original target on any resolution error.
        """
        root = self._effective_write_root(request_write_root)
        try:
            if root.resolve() == self._project_root.resolve():
                return target
            if target.is_absolute():
                rel = target.resolve().relative_to(self._project_root.resolve())
            else:
                rel = target
            return root / rel
        except (ValueError, OSError):
            # Not under project_root (or unresolvable) — leave as-is.
            return target

    # ------------------------------------------------------------------
    # Anti-Venom (Task 6) — universal pre-write gate
    # ------------------------------------------------------------------

    def _pre_write_gate(
        self, target: Path, content: str, request: ChangeRequest,
    ) -> None:
        """The last line of defence before bytes hit disk. FAIL-CLOSED.

        Runs immediately before every ``target.write_text`` in APPLY. Because
        the single-file path AND the multi-file coordinated path
        (orchestrator ``_apply_multi_file_candidate``) both funnel per-file
        through :meth:`execute`, this is the universal mutation chokepoint.

        Steps 1-4 (path-only) are delegated to the module-level
        :func:`assert_write_path_allowed` so that the same checks can be
        reused by the cross-repo :class:`SagaApplyStrategy` write sites
        without duplication.

        Checks, in order (first violation wins):
          1-4. Path checks — see :func:`assert_write_path_allowed`:
               canonicalize → containment → immutable-governance (boundary-
               anchored sentinel, no env switch) → protected-path.
          5.   SemanticGuardian — reject on any ``severity == "hard"`` finding.

        Raises
        ------
        BlockedPathError
            On any violation OR any internal error. The gate is impossible to
            bypass via an internal exception — every failure mode fails closed.
        """
        try:
            write_root = self._effective_write_root(getattr(request, "write_root", None))

            # Steps 1-4: path-only checks (shared with SagaApplyStrategy)
            assert_write_path_allowed(target, write_root)

            # Step 5: guardian — needs content; runs after path checks pass.
            real = os.path.realpath(os.path.abspath(str(target)))
            root = os.path.realpath(os.path.abspath(str(write_root)))
            rel_norm = os.path.relpath(real, root).replace(os.sep, "/")

            # Baseline the guardian against REALITY: the on-disk pre-image is
            # the correct MODIFY baseline so the delta is (on-disk → candidate),
            # not (∅ → candidate). An empty baseline turns every MODIFY into a
            # synthetic creation and makes delta-gated patterns fire on
            # PRE-EXISTING legitimate code (e.g. a file that already imports
            # subprocess), BlockedPathError'ing legit self-development edits.
            try:
                if target.exists():
                    old_content = target.read_text(errors="replace")
                else:
                    old_content = ""  # genuinely new file
            except Exception:  # noqa: BLE001 — read failure ⇒ fall back to ""
                # Rare; re-introduces the create-baseline for this one file
                # (acceptable). Do NOT crash the gate.
                old_content = ""

            try:
                from backend.core.ouroboros.governance.semantic_guardian import (
                    SemanticGuardian,
                )
                findings = SemanticGuardian().inspect(
                    file_path=rel_norm, old_content=old_content, new_content=content,
                )
            except Exception as exc:  # noqa: BLE001 — fail closed
                raise BlockedPathError(
                    f"guardian raised on {rel_norm} — fail-closed: {exc}"
                )
            if any(getattr(f, "severity", "") == "hard" for f in findings):
                raise BlockedPathError(
                    f"guardian hard finding on {rel_norm}"
                )
        except BlockedPathError:
            raise
        except Exception as exc:  # noqa: BLE001 — ANY internal error fails closed
            raise BlockedPathError(
                f"pre-write gate internal error — fail-closed: "
                f"{type(exc).__name__}: {exc}"
            )

    async def execute(self, request: ChangeRequest) -> ChangeResult:
        """Execute the 8-phase transactional change pipeline.

        Parameters
        ----------
        request:
            The change request describing what to modify.

        Returns
        -------
        ChangeResult
            Result with success status, phase reached, and optional error.
        """
        op_id = request.op_id or generate_operation_id(repo_origin="jarvis")

        try:
            # Phase 1: PLAN -- classify risk, record in ledger
            classification = self._risk_engine.classify(request.profile)
            risk_tier = classification.tier

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.PLANNED,
                    data={
                        "goal": request.goal,
                        "target_file": str(request.target_file),
                        "risk_tier": risk_tier.name,
                        "reason_code": classification.reason_code,
                    },
                )
            )

            await self._comm.emit_intent(
                op_id=op_id,
                goal=request.goal,
                target_files=[str(request.target_file)],
                risk_tier=risk_tier.name,
                blast_radius=request.profile.blast_radius,
            )

            # Phase 2: SANDBOX -- validate in isolation
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="sandbox", progress_pct=20.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.SANDBOXING,
                    data={"phase": "sandbox"},
                )
            )

            # Phase 3: VALIDATE -- AST parse in temp dir
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="validate", progress_pct=40.0
            )
            _RUNNABLE_EXTS = {".py", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"}
            if Path(request.target_file).suffix in _RUNNABLE_EXTS:
                valid = await self._validate_in_sandbox(request.proposed_content)
            else:
                valid = True  # non-code files skip AST syntax validation
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.VALIDATING,
                    data={"syntax_valid": valid},
                )
            )

            if not valid:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="validation_failed",
                    reason_code="syntax_error",
                )
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.FAILED,
                        data={"reason": "syntax_error"},
                    )
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.VALIDATE,
                    risk_tier=risk_tier,
                )

            # Phase 4: GATE -- check risk tier and break-glass
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="gate", progress_pct=50.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.GATING,
                    data={"risk_tier": risk_tier.name},
                )
            )

            # Check break-glass for BLOCKED operations
            if risk_tier == RiskTier.BLOCKED:
                promoted = self._break_glass.get_promoted_tier(op_id)
                if promoted is None and request.break_glass_op_id:
                    promoted = self._break_glass.get_promoted_tier(
                        request.break_glass_op_id
                    )
                if promoted is not None:
                    risk_tier = RiskTier.APPROVAL_REQUIRED
                    logger.info(
                        "Break-glass promoted %s from BLOCKED to APPROVAL_REQUIRED",
                        op_id,
                    )

            if risk_tier == RiskTier.BLOCKED:
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
                return ChangeResult(
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
                    diff_summary=f"Change to {request.target_file}",
                )
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.GATING,
                        data={
                            "waiting_approval": True,
                            "reason": classification.reason_code,
                        },
                    )
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.GATE,
                    risk_tier=RiskTier.APPROVAL_REQUIRED,
                )

            # Phase 5: APPLY -- capture rollback artifact, write to production
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="apply", progress_pct=70.0
            )

            # Slice 56 — redirect the write into the owned auto-commit worktree
            # when active, so the patch lands in the SAME tree AutoCommitter
            # commits from (and never the operator's main tree). No-op when
            # JARVIS_AUTO_COMMIT_WORKSPACE is unset. Rollback snapshot + lock +
            # signature all follow the redirected target (Phase 2 coherence).
            # Slice 64 — a per-request write_root (e.g. a swe_bench_pro prepared
            # worktree) takes precedence over the auto-commit workspace so the
            # patch lands in the correct cloned-repo tree.
            target = self._redirect_target(
                Path(request.target_file), request.write_root,
            )
            rollback = RollbackArtifact.capture(target)

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLYING,
                    data={
                        "rollback_hash": rollback.snapshot_hash,
                        "target_file": str(target),
                    },
                )
            )

            # Pre-hook check: allow registries to block the write
            if self._tool_hook_registry is not None:
                hook_decision = await self._tool_hook_registry.run_pre(
                    "edit",
                    {
                        "file": str(target),
                        "op_id": op_id,
                        "goal": request.goal,
                    },
                )
                if hook_decision == HookDecision.BLOCK:
                    logger.warning(
                        "Pre-hook BLOCKED file write for op=%s target=%s",
                        op_id,
                        target,
                    )
                    await self._ledger.append(
                        LedgerEntry(
                            op_id=op_id,
                            state=OperationState.FAILED,
                            data={"reason": "pre_hook_blocked", "target_file": str(target)},
                        )
                    )
                    raise RuntimeError(
                        f"Tool hook blocked file write for {target} (op={op_id})"
                    )

            # Inject Ouroboros signature so the user knows who changed this file
            signed_content = _inject_ouroboros_signature(
                content=request.proposed_content,
                op_id=op_id,
                goal=request.goal,
                target_path=str(target),
            )

            # Anti-Venom (Task 6) — UNIVERSAL MUTATION CHOKEPOINT.
            # Runs immediately before the only governed disk write. Fail-closed:
            # any violation OR internal error raises BlockedPathError, caught by
            # the outer try/except → op recorded FAILED, bytes never reach disk.
            # Covers the multi-file path too: _apply_multi_file_candidate calls
            # execute() per file, so every per-file write funnels through here.
            self._pre_write_gate(target, signed_content, request)

            # Acquire file lock for the write
            async with self._lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource=str(target),
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as handle:
                # 2PC Phase 1 (PREPARE): stage the write. NO ledger entry / NO
                # decision is emitted yet -- success is recorded only AFTER VERIFY
                # passes (the phantom-success fix).
                target.write_text(signed_content, encoding="utf-8")

            # Emit diff heartbeat so SerpentFlow can show colored inline diffs
            # as the file is being assimilated (Manifesto §7: Absolute Observability).
            try:
                import difflib as _difflib
                _diff_lines = list(_difflib.unified_diff(
                    rollback.original_content.splitlines(keepends=True),
                    signed_content.splitlines(keepends=True),
                    fromfile=f"a/{target}",
                    tofile=f"b/{target}",
                    n=3,
                ))
                if _diff_lines:
                    _diff_text = "".join(_diff_lines)
                    # Cap at 5000 chars to avoid flooding the transport
                    if len(_diff_text) > 5000:
                        _diff_text = _diff_text[:5000] + "\n... truncated"
                    await self._comm.emit_heartbeat(
                        op_id=op_id, phase="APPLY", progress_pct=75.0,
                        target_file=str(target),
                        diff_text=_diff_text,
                    )
            except Exception:
                pass  # Diff display is non-critical

            # Post-hook notification (fire-and-forget; errors swallowed by registry)
            if self._tool_hook_registry is not None:
                try:
                    await self._tool_hook_registry.run_post(
                        "edit",
                        {
                            "file": str(target),
                            "op_id": op_id,
                            "goal": request.goal,
                        },
                        result="applied",
                    )
                except Exception:
                    logger.exception(
                        "Post-hook error for op=%s target=%s (ignored)", op_id, target
                    )

            # 2PC Phase 2 (VERIFY) -- post-apply verification. APPLIED is NOT yet
            # recorded; a verify failure below rolls back with NO phantom success.
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="verify", progress_pct=95.0
            )

            verify_passed = True
            if request.verify_fn is not None:
                verify_passed = await request.verify_fn()
            elif Path(request.target_file).suffix in {".py", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"}:
                # Default: AST parse check on the applied file (code files only)
                verify_passed = await self._validate_in_sandbox(
                    target.read_text(encoding="utf-8")
                )

            if not verify_passed:
                # Automatic rollback — with failure handler for rollback itself
                logger.warning(
                    "Post-apply verification failed for %s -- rolling back",
                    op_id,
                )
                _rollback_succeeded = False
                try:
                    rollback.apply(target)
                    _rollback_succeeded = True
                except BaseException as rb_exc:
                    # EMERGENCY: Rollback failed — file may be in corrupted intermediate state.
                    # Log CRITICAL, record in ledger, emit emergency postmortem.
                    # Do NOT re-raise — always return a structured ChangeResult.
                    logger.critical(
                        "ROLLBACK FAILED for %s: %s — file may be corrupted. "
                        "Manual intervention required.",
                        op_id, rb_exc, exc_info=True,
                    )
                    await self._ledger.append(
                        LedgerEntry(
                            op_id=op_id,
                            state=OperationState.ROLLED_BACK,
                            data={
                                "reason": "rollback_apply_failed",
                                "error": str(rb_exc),
                                "emergency": True,
                            },
                        )
                    )
                    await self._comm.emit_postmortem(
                        op_id=op_id,
                        root_cause=f"rollback_failed:{type(rb_exc).__name__}:{rb_exc}",
                        failed_phase="ROLLBACK",
                        next_safe_action="manual_intervention_required",
                    )
                    # Emit fault to TelemetryBus if available
                    try:
                        from backend.core.telemetry_contract import TelemetryEnvelope, get_telemetry_bus
                        bus = get_telemetry_bus()
                        bus.emit(TelemetryEnvelope.create(
                            event_schema="fault.raised@1.0.0",
                            source="change_engine",
                            trace_id=op_id,
                            span_id="rollback_failure",
                            partition_key="fault",
                            payload={
                                "fault_class": "rollback_failed",
                                "component": "change_engine",
                                "message": f"Rollback failed for {op_id}: {rb_exc}",
                                "recovery_policy": "manual_intervention",
                                "terminal": True,
                            },
                            severity="critical",
                        ))
                    except Exception:
                        pass  # telemetry is best-effort

                    return ChangeResult(
                        op_id=op_id,
                        success=False,
                        phase_reached=ChangePhase.VERIFY,
                        risk_tier=risk_tier,
                        rolled_back=False,  # rollback FAILED — not rolled back
                    )

                # Rollback succeeded — normal flow
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.ROLLED_BACK,
                        data={"reason": "verify_failed"},
                    )
                )
                await self._comm.emit_postmortem(
                    op_id=op_id,
                    root_cause="post_apply_verification_failed",
                    failed_phase="VERIFY",
                    next_safe_action="review_proposed_change",
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.VERIFY,
                    risk_tier=risk_tier,
                    rolled_back=True,
                )

            # ── 2PC Phase 3 (COMMIT) — VERIFY passed ──────────────────────────
            # APPLIED is recorded ONLY here, after a clean VERIFY. Cryptographic
            # Terminal Gate first: SHA-256 the in-memory expected bytes against the
            # physical file. A hash mismatch while about to claim APPLIED is a
            # Phantom Write -> raise (fail-closed); never commit a false success.
            if (os.environ.get("JARVIS_CHANGE_ENGINE_IO_VERIFY", "true")
                    or "true").strip().lower() not in {"0", "false", "no", "off"}:
                import hashlib as _hl

                _expected_sha = _hl.sha256(signed_content.encode("utf-8")).hexdigest()
                try:
                    _disk_bytes = target.read_text(encoding="utf-8")
                except Exception as _rex:  # noqa: BLE001 — unreadable == phantom
                    raise PhantomWriteException(
                        "phantom write at %s: APPLIED but file unreadable off disk "
                        "(%r)" % (target, _rex)
                    ) from _rex
                _actual_sha = _hl.sha256(_disk_bytes.encode("utf-8")).hexdigest()
                if _actual_sha != _expected_sha:
                    raise PhantomWriteException(
                        "phantom write at %s: about to claim APPLIED but SHA-256 "
                        "mismatch (expected=%s on-disk=%s)"
                        % (target, _expected_sha[:12], _actual_sha[:12])
                    )

            await self._comm.emit_heartbeat(
                op_id=op_id, phase="ledger", progress_pct=85.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLIED,
                    data={
                        "target_file": str(target),
                        "rollback_hash": rollback.snapshot_hash,
                    },
                )
            )
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="safe_auto_passed",
                diff_summary=f"Applied change to {target}",
            )

            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause="none",
                failed_phase=None,
                next_safe_action="none",
            )

            return ChangeResult(
                op_id=op_id,
                success=True,
                phase_reached=ChangePhase.VERIFY,
                risk_tier=risk_tier,
            )

        except Exception as exc:
            logger.error("Change engine error for %s: %s", op_id, exc)
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=str(exc),
                failed_phase="unknown",
                next_safe_action="investigate_error",
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.FAILED,
                    data={"error": str(exc)},
                )
            )
            return ChangeResult(
                op_id=op_id,
                success=False,
                phase_reached=ChangePhase.PLAN,
                risk_tier=None,
                error=str(exc),
            )

    async def _validate_in_sandbox(self, code: str) -> bool:
        """Validate code by AST-parsing in a temporary directory."""
        try:
            with tempfile.TemporaryDirectory(
                prefix="ouroboros_validate_"
            ) as sandbox_dir:
                sandbox_path = Path(sandbox_dir) / "validate.py"
                sandbox_path.write_text(code, encoding="utf-8")
                source = sandbox_path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(sandbox_path))
            return True
        except SyntaxError:
            return False
