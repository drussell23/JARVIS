"""Saga type definitions: patch model, terminal states, apply results."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.op_context import RepoSagaStatus


class FileOp(str, Enum):
    """File operation type in a RepoPatch."""
    MODIFY = "modify"
    CREATE = "create"
    DELETE = "delete"


@dataclass(frozen=True)
class PatchedFile:
    """A single file operation in a RepoPatch.

    Parameters
    ----------
    path:
        Path relative to the repo root.
    op:
        Operation type (MODIFY, CREATE, DELETE).
    preimage:
        Original file bytes before the change. None for CREATE (file didn't exist).
        Required for MODIFY and DELETE (used for compensation).
    """
    path: str
    op: FileOp
    preimage: Optional[bytes]

    def __post_init__(self) -> None:
        if self.op in (FileOp.MODIFY, FileOp.DELETE) and self.preimage is None:
            raise ValueError(
                f"PatchedFile with op={self.op.value} requires preimage; got None for path={self.path!r}"
            )


@dataclass(frozen=True)
class RepoPatch:
    """All file operations for a single repo in a multi-repo saga.

    Parameters
    ----------
    repo:
        Repository name (must match OperationContext.repo_scope entry).
    files:
        Tuple of PatchedFile describing every file this patch touches.
    new_content:
        Tuple of (path, bytes) pairs to write during apply.
        Stored separately from preimage so the patch is self-contained.
        Every path in `new_content` must correspond to a path in `files`.
    """
    repo: str
    files: Tuple[PatchedFile, ...]
    new_content: Tuple[Tuple[str, bytes], ...] = ()

    def is_empty(self) -> bool:
        """Return True when there are no file operations to apply.

        `new_content` entries without matching `files` entries are invalid
        and treated as empty for apply purposes.
        """
        return len(self.files) == 0


class SagaTerminalState(str, Enum):
    """Terminal states of a saga execution."""
    SAGA_APPLY_COMPLETED = "saga_apply_completed"   # all applies done; enter VERIFY
    SAGA_ROLLED_BACK = "saga_rolled_back"            # compensation succeeded
    SAGA_STUCK = "saga_stuck"                        # compensation failed; human required
    SAGA_SUCCEEDED = "saga_succeeded"                # VERIFY passed; op complete
    SAGA_VERIFY_FAILED = "saga_verify_failed"        # VERIFY failed; triggers compensation
    SAGA_ABORTED = "saga_aborted"                    # pre-flight drift check failed
    SAGA_PARTIAL_PROMOTE = "saga_partial_promote"    # some repos promoted before failure


@dataclass  # intentionally NOT frozen: strategy builds this incrementally
class SagaApplyResult:
    """Result returned by SagaApplyStrategy.execute()."""
    terminal_state: SagaTerminalState
    saga_id: str
    saga_step_index: int
    error: Optional[str]
    reason_code: str = ""
    saga_state: Tuple["RepoSagaStatus", ...] = field(default_factory=tuple)  # updated RepoSagaStatus entries for idempotent resume


@dataclass(frozen=True)
class SagaLedgerArtifact:
    """Frozen artifact emitted with every saga ledger entry for audit trail."""

    saga_id: str
    op_id: str
    event: str                          # "prepare" | "apply_repo" | "promote_repo" | etc.
    repo: str                           # "*" for saga-wide events
    original_ref: str                   # branch name or "HEAD" (detached)
    original_sha: str                   # SHA at saga start
    base_sha: str                       # pinned base SHA for this repo
    saga_branch: str                    # ouroboros/saga-<op_id>/<repo>
    promoted_sha: str                   # SHA after ff-only merge ("" if not promoted)
    promote_order_index: int            # position in promotion sequence (-1 if N/A)
    rollback_reason: str                # "" on success, reason code on failure
    partial_promote_boundary_repo: str  # repo where promotion failed ("" if clean)
    kept_forensics_branches: bool       # True if saga branches retained for debug
    skipped_no_diff: bool               # True if repo had no actual changes
    timestamp_ns: int                   # time.monotonic_ns()
