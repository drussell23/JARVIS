"""backend/core/ouroboros/governance/autonomy/file_operations.py

Structured file operation request types for L1 multi-file autonomous ops.

Task M2: Extract FileCreationRequest + MultiFileRequest from deprecated engine.py
into proper immutable types with validation and safety checking.

Design:
    - FileOpType: enum of supported file operation kinds.
    - FileOperationRequest: frozen dataclass for a single file operation.
    - MultiFileRequest: frozen dataclass for an atomic batch of operations.
    - FileOperationValidator: validates requests against safety rules
      (protected paths, duplicates, conflicts).

All request types are frozen (immutable) so they can be safely passed between
layers without risk of mutation.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Tuple


# ---------------------------------------------------------------------------
# FileOpType
# ---------------------------------------------------------------------------


class FileOpType(str, enum.Enum):
    """Type of file operation."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


# ---------------------------------------------------------------------------
# FileOperationRequest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileOperationRequest:
    """A single file operation request.

    Immutable so it can be safely passed between layers.
    """

    op_type: FileOpType
    file_path: str
    content: Optional[str] = None  # Required for CREATE/MODIFY
    new_path: Optional[str] = None  # Required for RENAME
    reason: str = ""  # Why this operation is needed

    def validate(self) -> List[str]:
        """Return list of validation errors (empty = valid).

        Checks:
        - file_path must not be empty
        - CREATE/MODIFY must have non-None content
        - CREATE content must not be empty string
        - RENAME must have new_path
        """
        errors: List[str] = []

        if not self.file_path:
            errors.append("file_path must not be empty")

        if self.op_type in (FileOpType.CREATE, FileOpType.MODIFY):
            if self.content is None:
                errors.append(
                    f"{self.op_type.value.upper()} operation requires content"
                )
            elif self.op_type == FileOpType.CREATE and self.content == "":
                errors.append(
                    "CREATE operation content must not be empty"
                )

        if self.op_type == FileOpType.RENAME and not self.new_path:
            errors.append("RENAME operation requires new_path")

        return errors


# ---------------------------------------------------------------------------
# MultiFileRequest
# ---------------------------------------------------------------------------


def _make_request_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class MultiFileRequest:
    """A batch of file operations to be applied atomically.

    All operations in a batch either succeed together or are rolled back.
    """

    request_id: str = field(default_factory=_make_request_id)
    operations: Tuple[FileOperationRequest, ...] = ()  # Tuple for immutability
    op_id: Optional[str] = None  # Link to governance operation
    brain_id: Optional[str] = None  # Which brain generated this
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """Validate all operations. Returns aggregated errors."""
        errors: List[str] = []
        for i, op in enumerate(self.operations):
            op_errors = op.validate()
            for e in op_errors:
                errors.append(f"operation[{i}] ({op.file_path!r}): {e}")
        return errors

    @property
    def file_count(self) -> int:
        """Number of operations in the batch."""
        return len(self.operations)

    @property
    def creates(self) -> Tuple[FileOperationRequest, ...]:
        """Return only CREATE operations."""
        return tuple(op for op in self.operations if op.op_type == FileOpType.CREATE)

    @property
    def modifies(self) -> Tuple[FileOperationRequest, ...]:
        """Return only MODIFY operations."""
        return tuple(op for op in self.operations if op.op_type == FileOpType.MODIFY)

    @property
    def deletes(self) -> Tuple[FileOperationRequest, ...]:
        """Return only DELETE operations."""
        return tuple(op for op in self.operations if op.op_type == FileOpType.DELETE)

    @property
    def renames(self) -> Tuple[FileOperationRequest, ...]:
        """Return only RENAME operations."""
        return tuple(op for op in self.operations if op.op_type == FileOpType.RENAME)

    @property
    def affected_paths(self) -> FrozenSet[str]:
        """All file paths affected by this request.

        Includes both source and destination paths for RENAME operations.
        """
        paths: set[str] = set()
        for op in self.operations:
            if op.file_path:
                paths.add(op.file_path)
            if op.new_path:
                paths.add(op.new_path)
        return frozenset(paths)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/telemetry (excludes file content for safety)."""
        return {
            "request_id": self.request_id,
            "op_id": self.op_id,
            "brain_id": self.brain_id,
            "file_count": self.file_count,
            "operations": [
                {
                    "op_type": op.op_type.value,
                    "file_path": op.file_path,
                    **({"new_path": op.new_path} if op.new_path else {}),
                    "reason": op.reason,
                }
                for op in self.operations
            ],
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# FileOperationValidator
# ---------------------------------------------------------------------------


class FileOperationValidator:
    """Validates file operation requests against safety rules.

    Used by L1 before executing any file operations.
    """

    # Paths that should never be modified autonomously
    PROTECTED_PATTERNS: ClassVar[Tuple[str, ...]] = (
        ".env",
        ".git/",
        "credentials",
        "secret",
        ".ssh/",
        "node_modules/",
        "__pycache__/",
    )

    def __init__(self, additional_protected: Optional[List[str]] = None) -> None:
        self._protected: List[str] = list(self.PROTECTED_PATTERNS)
        if additional_protected:
            self._protected.extend(additional_protected)

    def validate(self, request: MultiFileRequest) -> List[str]:
        """Validate a multi-file request. Returns list of errors.

        Checks:
        1. Request-level validation (from request.validate())
        2. No operations on protected paths
        3. No duplicate file paths in same batch
        4. No conflicting operations (CREATE + DELETE same file)
        """
        errors: List[str] = []

        # 1. Delegate to request's own validation
        errors.extend(request.validate())

        # 2. Protected path check (source and destination for renames)
        for i, op in enumerate(request.operations):
            paths_to_check = [op.file_path]
            if op.new_path:
                paths_to_check.append(op.new_path)
            for path in paths_to_check:
                if self.is_protected(path):
                    # Find which pattern matched for clear error messages
                    for pattern in self._protected:
                        if self._path_matches_pattern(path, pattern):
                            errors.append(
                                f"operation[{i}]: path {path!r} matches "
                                f"protected pattern {pattern!r}"
                            )
                            break

        # 3. Duplicate path detection
        seen_paths: Dict[str, int] = {}
        for i, op in enumerate(request.operations):
            if op.file_path in seen_paths:
                errors.append(
                    f"operation[{i}]: duplicate path {op.file_path!r} "
                    f"(also in operation[{seen_paths[op.file_path]}])"
                )
            else:
                seen_paths[op.file_path] = i

        # 4. Conflicting operations (e.g., CREATE + DELETE on same file)
        ops_by_path: Dict[str, List[FileOpType]] = {}
        for op in request.operations:
            ops_by_path.setdefault(op.file_path, []).append(op.op_type)

        _CONFLICT_PAIRS = frozenset({
            (FileOpType.CREATE, FileOpType.DELETE),
            (FileOpType.DELETE, FileOpType.CREATE),
            (FileOpType.CREATE, FileOpType.CREATE),
            (FileOpType.MODIFY, FileOpType.DELETE),
            (FileOpType.DELETE, FileOpType.MODIFY),
        })

        for path, op_types in ops_by_path.items():
            if len(op_types) > 1:
                for j in range(len(op_types)):
                    for k in range(j + 1, len(op_types)):
                        pair = (op_types[j], op_types[k])
                        if pair in _CONFLICT_PAIRS:
                            errors.append(
                                f"conflicting operations on {path!r}: "
                                f"{op_types[j].value} + {op_types[k].value}"
                            )

        return errors

    def is_protected(self, file_path: str) -> bool:
        """Check if a file path matches any protected pattern."""
        return any(
            self._path_matches_pattern(file_path, pattern)
            for pattern in self._protected
        )

    @staticmethod
    def _path_matches_pattern(file_path: str, pattern: str) -> bool:
        """Check if a file path matches a single protected pattern.

        Matching rules:
        - If pattern ends with '/', it matches any path containing that prefix
          as a path segment (e.g., '.git/' matches '.git/config').
        - Otherwise, the pattern is matched as a substring anywhere in the path
          (e.g., 'secret' matches 'my_secret_config.yaml').
        """
        if pattern.endswith("/"):
            # Directory pattern: match path segments
            # '.git/' should match '.git/config' and 'foo/.git/bar'
            return pattern in file_path or file_path.startswith(pattern)
        else:
            # Substring pattern: match anywhere in the path
            return pattern in file_path
