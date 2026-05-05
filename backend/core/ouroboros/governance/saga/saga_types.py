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


# PRD §3.6.2 vector #8 closure (Wave 3 hygiene Item 6, 2026-05-05):
# canonical schema-version constants for the saga-audit artifact
# contracts. Bump on field add/remove/rename. See
# `meta.versioned_artifact` §33.5 pattern.
SAGA_LEDGER_ARTIFACT_SCHEMA_VERSION: str = "saga_ledger_artifact.1"
WORK_UNIT_LEDGER_ARTIFACT_SCHEMA_VERSION: str = (
    "work_unit_ledger_artifact.1"
)


@dataclass(frozen=True)
class SagaLedgerArtifact:
    """Frozen artifact emitted with every saga ledger entry for audit trail.

    **Versioned artifact contract** (§33.5): ``schema_version``
    carries the canonical version string. Currently dormant
    (zero importers); the field + symmetric ``to_dict`` /
    ``from_dict`` are in place so a future audit-consumer arc
    inherits the discipline structurally — readers verify
    schema via
    :func:`meta.versioned_artifact.verify_artifact_schema`."""

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
    schema_version: str = SAGA_LEDGER_ARTIFACT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Project to dict for JSON serialization."""
        return {
            "schema_version": self.schema_version,
            "saga_id": self.saga_id,
            "op_id": self.op_id,
            "event": self.event,
            "repo": self.repo,
            "original_ref": self.original_ref,
            "original_sha": self.original_sha,
            "base_sha": self.base_sha,
            "saga_branch": self.saga_branch,
            "promoted_sha": self.promoted_sha,
            "promote_order_index": self.promote_order_index,
            "rollback_reason": self.rollback_reason,
            "partial_promote_boundary_repo": (
                self.partial_promote_boundary_repo
            ),
            "kept_forensics_branches": self.kept_forensics_branches,
            "skipped_no_diff": self.skipped_no_diff,
            "timestamp_ns": self.timestamp_ns,
        }

    @classmethod
    def from_dict(
        cls, raw: dict,
    ) -> "Optional[SagaLedgerArtifact]":
        """Defensive parse — returns ``None`` on malformed
        fields. NEVER raises."""
        try:
            if not isinstance(raw, dict):
                return None
            return cls(
                saga_id=str(raw.get("saga_id", "")),
                op_id=str(raw.get("op_id", "")),
                event=str(raw.get("event", "")),
                repo=str(raw.get("repo", "")),
                original_ref=str(raw.get("original_ref", "")),
                original_sha=str(raw.get("original_sha", "")),
                base_sha=str(raw.get("base_sha", "")),
                saga_branch=str(raw.get("saga_branch", "")),
                promoted_sha=str(raw.get("promoted_sha", "")),
                promote_order_index=int(
                    raw.get("promote_order_index", -1),
                ),
                rollback_reason=str(
                    raw.get("rollback_reason", ""),
                ),
                partial_promote_boundary_repo=str(
                    raw.get("partial_promote_boundary_repo", ""),
                ),
                kept_forensics_branches=bool(
                    raw.get("kept_forensics_branches", False),
                ),
                skipped_no_diff=bool(
                    raw.get("skipped_no_diff", False),
                ),
                timestamp_ns=int(raw.get("timestamp_ns", 0)),
                schema_version=str(
                    raw.get(
                        "schema_version",
                        SAGA_LEDGER_ARTIFACT_SCHEMA_VERSION,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class WorkUnitLedgerArtifact:
    """Audit artifact emitted for L3 execution-graph work-unit transitions.

    **Versioned artifact contract** (§33.5) — same discipline as
    :class:`SagaLedgerArtifact`."""

    graph_id: str
    unit_id: str
    repo: str
    state: str
    barrier_id: str
    causal_trace_id: str
    timestamp_ns: int
    schema_version: str = (
        WORK_UNIT_LEDGER_ARTIFACT_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "unit_id": self.unit_id,
            "repo": self.repo,
            "state": self.state,
            "barrier_id": self.barrier_id,
            "causal_trace_id": self.causal_trace_id,
            "timestamp_ns": self.timestamp_ns,
        }

    @classmethod
    def from_dict(
        cls, raw: dict,
    ) -> "Optional[WorkUnitLedgerArtifact]":
        try:
            if not isinstance(raw, dict):
                return None
            return cls(
                graph_id=str(raw.get("graph_id", "")),
                unit_id=str(raw.get("unit_id", "")),
                repo=str(raw.get("repo", "")),
                state=str(raw.get("state", "")),
                barrier_id=str(raw.get("barrier_id", "")),
                causal_trace_id=str(
                    raw.get("causal_trace_id", ""),
                ),
                timestamp_ns=int(raw.get("timestamp_ns", 0)),
                schema_version=str(
                    raw.get(
                        "schema_version",
                        WORK_UNIT_LEDGER_ARTIFACT_SCHEMA_VERSION,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None
