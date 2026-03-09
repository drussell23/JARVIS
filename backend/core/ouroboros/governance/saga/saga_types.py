"""Saga type definitions: patch model, terminal states, apply results."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


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
    """
    repo: str
    files: Tuple[PatchedFile, ...]
    new_content: Tuple[Tuple[str, bytes], ...] = ()

    def is_empty(self) -> bool:
        return len(self.files) == 0


class SagaTerminalState(str, Enum):
    """Terminal states of a saga execution."""
    SAGA_APPLY_COMPLETED = "saga_apply_completed"   # all applies done; enter VERIFY
    SAGA_ROLLED_BACK = "saga_rolled_back"            # compensation succeeded
    SAGA_STUCK = "saga_stuck"                        # compensation failed; human required
    SAGA_SUCCEEDED = "saga_succeeded"                # VERIFY passed; op complete
    SAGA_VERIFY_FAILED = "saga_verify_failed"        # VERIFY failed; triggers compensation
    SAGA_ABORTED = "saga_aborted"                    # pre-flight drift check failed


@dataclass
class SagaApplyResult:
    """Result returned by SagaApplyStrategy.execute()."""
    terminal_state: SagaTerminalState
    saga_id: str
    saga_step_index: int
    error: Optional[str]
    reason_code: str = ""
