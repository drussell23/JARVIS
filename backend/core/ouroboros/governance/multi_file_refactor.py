"""
Multi-File Cross-Refactor Engine -- Atomic Codebase-Wide Renames & Moves
=========================================================================

Coordinates changes across multiple files in a single atomic operation
with dependency ordering, conflict detection, checkpoint-based rollback,
and dry-run support.

Unlike :mod:`multi_file_engine` (which wraps the single-file ChangeEngine
pipeline), this module operates at the *refactoring* level: symbol renames,
file moves, import rewrites.  It does NOT go through the full governance
pipeline — it is a utility used by governance operations that need to
express multi-file refactors cleanly.

Key Guarantees
--------------
- Conflict detection via content hashes before any writes
- Dependency-ordered execution (renamed module written before importers)
- Checkpoint creation before execution; automatic rollback on failure
- Dry-run mode returns the full plan without touching the filesystem

Environment Variables
---------------------
``JARVIS_REFACTOR_CHECKPOINT_DIR``
    Directory for refactor checkpoints (default: ~/.jarvis/ouroboros/refactor_checkpoints).
``JARVIS_REFACTOR_MAX_FILES``
    Maximum number of files a single refactor plan may touch (default: 200).
``JARVIS_REFACTOR_TIMEOUT_S``
    Overall timeout for a refactor execution (default: 120).
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.MultiFileRefactor")

_CHECKPOINT_DIR = Path(
    os.environ.get(
        "JARVIS_REFACTOR_CHECKPOINT_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "refactor_checkpoints"),
    )
)
_MAX_FILES = int(os.environ.get("JARVIS_REFACTOR_MAX_FILES", "200"))
_TIMEOUT_S = float(os.environ.get("JARVIS_REFACTOR_TIMEOUT_S", "120"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    """Describes a single file-level change within a refactor plan.

    Attributes
    ----------
    file_path:
        Relative or absolute path to the file.
    change_type:
        One of ``"modify"``, ``"create"``, ``"delete"``, ``"rename"``.
    old_content_hash:
        SHA-256 hex digest of the file content before the change.
        Used for conflict detection (``None`` for ``"create"``).
    new_content:
        Full proposed new content of the file (for ``"modify"`` / ``"create"``).
    patch:
        Diff-style patch as an alternative to ``new_content``.
        If both are set, ``new_content`` takes precedence.
    rename_to:
        Destination path for ``"rename"`` changes.
    """

    file_path: str
    change_type: str  # "modify" | "create" | "delete" | "rename"
    old_content_hash: Optional[str] = None
    new_content: Optional[str] = None
    patch: Optional[str] = None
    rename_to: Optional[str] = None

    def __post_init__(self) -> None:
        valid_types = ("modify", "create", "delete", "rename")
        if self.change_type not in valid_types:
            raise ValueError(
                f"Invalid change_type {self.change_type!r}; "
                f"must be one of {valid_types}"
            )


@dataclass
class RefactorPlan:
    """An ordered set of file changes with dependency information.

    Attributes
    ----------
    plan_id:
        Unique identifier for this plan.
    goal:
        Human-readable description of the refactor intent.
    file_changes:
        Ordered list of :class:`FileChange` entries.
    dependencies:
        Maps ``file_path`` to a list of file paths that must be changed
        first.  Used to compute execution order.
    created_at:
        Wall-clock timestamp of plan creation.
    """

    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    goal: str = ""
    file_changes: List[FileChange] = field(default_factory=list)
    dependencies: Dict[str, List[str]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class RefactorResult:
    """Outcome of a refactor execution.

    Attributes
    ----------
    plan_id:
        The executed plan's identifier.
    success:
        True if all file changes applied without error.
    files_modified:
        Number of files successfully changed.
    files_failed:
        Number of files that failed to change.
    checkpoint_id:
        Identifier of the pre-execution checkpoint (for rollback).
    error:
        Error message if the refactor failed.
    dry_run:
        True if this was a dry-run (no files actually modified).
    details:
        Per-file status messages.
    """

    plan_id: str
    success: bool
    files_modified: int = 0
    files_failed: int = 0
    checkpoint_id: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False
    details: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_content(content: str) -> str:
    """SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> Optional[str]:
    """SHA-256 hex digest of a file's content.  Returns None if unreadable."""
    try:
        return _hash_content(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _toposort(deps: Dict[str, List[str]], all_nodes: List[str]) -> List[str]:
    """Kahn's algorithm topological sort.

    Returns nodes in dependency order (dependencies first).
    Nodes not in ``deps`` are appended at the end in their original order.
    Raises ``ValueError`` on cycles.
    """
    in_degree: Dict[str, int] = {n: 0 for n in all_nodes}
    adj: Dict[str, List[str]] = {n: [] for n in all_nodes}
    node_set = set(all_nodes)

    for node, predecessors in deps.items():
        if node not in node_set:
            continue
        for pred in predecessors:
            if pred in node_set:
                adj[pred].append(node)
                in_degree[node] = in_degree.get(node, 0) + 1

    queue: List[str] = [n for n in all_nodes if in_degree.get(n, 0) == 0]
    result: List[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for successor in adj.get(node, []):
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if len(result) != len(all_nodes):
        raise ValueError(
            "Dependency cycle detected in refactor plan"
        )
    return result


def _match_glob(path_str: str, pattern: str) -> bool:
    """Match a file path against a glob pattern."""
    return fnmatch.fnmatch(path_str, pattern)


# ---------------------------------------------------------------------------
# Checkpoint management
# ---------------------------------------------------------------------------


class _CheckpointStore:
    """File-based checkpoint storage for rollback support."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def create(self, plan_id: str, files: Dict[str, Optional[str]]) -> str:
        """Snapshot current file contents.

        Parameters
        ----------
        plan_id:
            The plan this checkpoint belongs to.
        files:
            Mapping of file paths to their current content (None if missing).

        Returns the checkpoint ID.
        """
        cp_id = f"cp-{plan_id[:12]}-{int(time.time())}"
        cp_dir = self._base_dir / cp_id
        cp_dir.mkdir(parents=True, exist_ok=True)

        manifest: Dict[str, Any] = {
            "checkpoint_id": cp_id,
            "plan_id": plan_id,
            "created_at": time.time(),
            "files": {},
        }

        for fpath, content in files.items():
            safe_name = hashlib.sha256(fpath.encode()).hexdigest()[:16]
            if content is not None:
                (cp_dir / safe_name).write_text(content, encoding="utf-8")
                manifest["files"][fpath] = {"backup": safe_name, "existed": True}
            else:
                manifest["files"][fpath] = {"backup": None, "existed": False}

        (cp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        logger.info("[Checkpoint] Created %s with %d files", cp_id, len(files))
        return cp_id

    def restore(self, checkpoint_id: str) -> bool:
        """Restore all files from a checkpoint.  Returns True on success."""
        cp_dir = self._base_dir / checkpoint_id
        manifest_path = cp_dir / "manifest.json"
        if not manifest_path.exists():
            logger.error("[Checkpoint] Manifest not found for %s", checkpoint_id)
            return False

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("[Checkpoint] Failed to read manifest: %s", exc)
            return False

        restored = 0
        for fpath, info in manifest.get("files", {}).items():
            target = Path(fpath)
            if info.get("existed"):
                backup_name = info["backup"]
                backup_path = cp_dir / backup_name
                if backup_path.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(backup_path), str(target))
                    restored += 1
            else:
                # File did not exist before — remove it
                if target.exists():
                    target.unlink()
                    restored += 1

        logger.info("[Checkpoint] Restored %d files from %s", restored, checkpoint_id)
        return True

    def cleanup(self, checkpoint_id: str) -> None:
        """Remove a checkpoint from disk."""
        cp_dir = self._base_dir / checkpoint_id
        if cp_dir.exists():
            shutil.rmtree(str(cp_dir), ignore_errors=True)


# ---------------------------------------------------------------------------
# MultiFileRefactorEngine
# ---------------------------------------------------------------------------


class MultiFileRefactorEngine:
    """Coordinates multi-file refactoring with atomic semantics.

    Parameters
    ----------
    project_root:
        Root directory of the project being refactored.
    checkpoint_dir:
        Where to store rollback checkpoints.
    """

    def __init__(
        self,
        project_root: Path,
        checkpoint_dir: Optional[Path] = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._checkpoints = _CheckpointStore(
            checkpoint_dir or _CHECKPOINT_DIR
        )
        self._executed_plans: Dict[str, str] = {}  # plan_id -> checkpoint_id

    # ------------------------------------------------------------------
    # Plan builders
    # ------------------------------------------------------------------

    async def plan_rename(
        self,
        old_name: str,
        new_name: str,
        scope: str = "**/*.py",
    ) -> RefactorPlan:
        """Build a plan to rename a symbol across all matching files.

        Scans all files matching ``scope`` for occurrences of ``old_name``
        and produces :class:`FileChange` entries that replace them with
        ``new_name``.

        The renamed module/file itself (if it exists) is ordered before
        its importers in the dependency graph.
        """
        plan = RefactorPlan(goal=f"Rename '{old_name}' -> '{new_name}' in {scope}")
        matching_files = await self._find_files_containing(old_name, scope)

        if len(matching_files) > _MAX_FILES:
            logger.warning(
                "[Refactor] Rename matches %d files (max %d) — truncating",
                len(matching_files), _MAX_FILES,
            )
            matching_files = matching_files[:_MAX_FILES]

        # Identify the "source" module file (the one being renamed)
        source_candidates = [
            f for f in matching_files
            if Path(f).stem == old_name or Path(f).name == f"{old_name}.py"
        ]

        for fpath in matching_files:
            p = self._project_root / fpath
            try:
                content = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            new_content = content.replace(old_name, new_name)
            if new_content == content:
                continue

            change = FileChange(
                file_path=fpath,
                change_type="modify",
                old_content_hash=_hash_content(content),
                new_content=new_content,
            )
            plan.file_changes.append(change)

            # Importers depend on the source module being renamed first
            if fpath not in source_candidates and source_candidates:
                plan.dependencies[fpath] = list(source_candidates)

        logger.info(
            "[Refactor] plan_rename '%s' -> '%s': %d file(s)",
            old_name, new_name, len(plan.file_changes),
        )
        return plan

    async def plan_move(
        self,
        old_path: str,
        new_path: str,
    ) -> RefactorPlan:
        """Build a plan to move a file and update all its importers.

        Produces:
        1. A ``"rename"`` change for the file itself
        2. ``"modify"`` changes for every file that imports the old module path

        The rename is dependency-ordered before the import updates.
        """
        plan = RefactorPlan(goal=f"Move '{old_path}' -> '{new_path}'")
        abs_old = self._project_root / old_path
        if not abs_old.exists():
            logger.warning("[Refactor] Source file does not exist: %s", old_path)
            return plan

        # Read the source file
        try:
            source_content = abs_old.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return plan

        # Rename change
        rename_change = FileChange(
            file_path=old_path,
            change_type="rename",
            old_content_hash=_hash_content(source_content),
            rename_to=new_path,
        )
        plan.file_changes.append(rename_change)

        # Compute old and new module paths for import rewriting
        old_module = self._path_to_module(old_path)
        new_module = self._path_to_module(new_path)

        if old_module and new_module:
            importers = await self._find_files_containing(old_module, "**/*.py")
            for fpath in importers:
                if fpath == old_path:
                    continue
                p = self._project_root / fpath
                try:
                    content = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                new_content = content.replace(old_module, new_module)
                if new_content == content:
                    continue

                change = FileChange(
                    file_path=fpath,
                    change_type="modify",
                    old_content_hash=_hash_content(content),
                    new_content=new_content,
                )
                plan.file_changes.append(change)
                plan.dependencies[fpath] = [old_path]

        logger.info(
            "[Refactor] plan_move '%s' -> '%s': %d file(s)",
            old_path, new_path, len(plan.file_changes),
        )
        return plan

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        plan: RefactorPlan,
        dry_run: bool = False,
    ) -> RefactorResult:
        """Execute a refactor plan.

        If ``dry_run`` is True, returns what would change without modifying
        any files.  Otherwise, creates a checkpoint, applies changes in
        dependency order, and rolls back if any change fails.
        """
        if not plan.file_changes:
            return RefactorResult(
                plan_id=plan.plan_id,
                success=True,
                dry_run=dry_run,
                details=["No file changes in plan"],
            )

        # Compute execution order
        all_paths = [c.file_path for c in plan.file_changes]
        try:
            ordered_paths = _toposort(plan.dependencies, all_paths)
        except ValueError as exc:
            return RefactorResult(
                plan_id=plan.plan_id,
                success=False,
                error=str(exc),
                dry_run=dry_run,
            )

        # Index changes by path
        changes_by_path: Dict[str, FileChange] = {
            c.file_path: c for c in plan.file_changes
        }

        if dry_run:
            return RefactorResult(
                plan_id=plan.plan_id,
                success=True,
                files_modified=len(plan.file_changes),
                dry_run=True,
                details=[
                    f"[dry-run] {changes_by_path[p].change_type}: {p}"
                    for p in ordered_paths
                ],
            )

        # Conflict detection: verify file hashes haven't changed
        conflicts = self._detect_conflicts(plan)
        if conflicts:
            return RefactorResult(
                plan_id=plan.plan_id,
                success=False,
                error=f"Content conflicts detected in {len(conflicts)} file(s)",
                details=[f"Conflict: {f}" for f in conflicts],
            )

        # Create checkpoint
        snapshot: Dict[str, Optional[str]] = {}
        for p in ordered_paths:
            change = changes_by_path[p]
            abs_path = self._project_root / p
            if abs_path.exists():
                try:
                    snapshot[str(abs_path)] = abs_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    snapshot[str(abs_path)] = None
            else:
                snapshot[str(abs_path)] = None

            # For rename, also snapshot the destination
            if change.change_type == "rename" and change.rename_to:
                dest = self._project_root / change.rename_to
                if dest.exists():
                    try:
                        snapshot[str(dest)] = dest.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        snapshot[str(dest)] = None
                else:
                    snapshot[str(dest)] = None

        checkpoint_id = self._checkpoints.create(plan.plan_id, snapshot)

        # Apply changes in order
        modified = 0
        failed = 0
        details: List[str] = []

        for fpath in ordered_paths:
            change = changes_by_path[fpath]
            try:
                self._apply_change(change)
                modified += 1
                details.append(f"OK: {change.change_type} {fpath}")
            except Exception as exc:
                failed += 1
                details.append(f"FAIL: {change.change_type} {fpath}: {exc}")
                logger.error(
                    "[Refactor] Failed to apply %s on %s: %s",
                    change.change_type, fpath, exc,
                )
                # Rollback everything
                logger.info("[Refactor] Rolling back via checkpoint %s", checkpoint_id)
                self._checkpoints.restore(checkpoint_id)
                return RefactorResult(
                    plan_id=plan.plan_id,
                    success=False,
                    files_modified=modified,
                    files_failed=failed,
                    checkpoint_id=checkpoint_id,
                    error=f"Failed at {fpath}: {exc}",
                    details=details,
                )

        self._executed_plans[plan.plan_id] = checkpoint_id
        logger.info(
            "[Refactor] Plan %s executed: %d modified, %d failed",
            plan.plan_id[:8], modified, failed,
        )
        return RefactorResult(
            plan_id=plan.plan_id,
            success=True,
            files_modified=modified,
            files_failed=failed,
            checkpoint_id=checkpoint_id,
            details=details,
        )

    async def rollback(self, plan_id: str) -> bool:
        """Revert a previously executed plan to its pre-refactor state.

        Returns True if the rollback succeeded.
        """
        checkpoint_id = self._executed_plans.get(plan_id)
        if not checkpoint_id:
            logger.warning("[Refactor] No checkpoint found for plan %s", plan_id)
            return False

        success = self._checkpoints.restore(checkpoint_id)
        if success:
            del self._executed_plans[plan_id]
            logger.info("[Refactor] Rolled back plan %s", plan_id[:8])
        return success

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _detect_conflicts(self, plan: RefactorPlan) -> List[str]:
        """Check that file contents match expected hashes."""
        conflicts: List[str] = []
        for change in plan.file_changes:
            if change.old_content_hash is None:
                continue
            abs_path = self._project_root / change.file_path
            current_hash = _hash_file(abs_path)
            if current_hash is not None and current_hash != change.old_content_hash:
                conflicts.append(change.file_path)
        return conflicts

    def _apply_change(self, change: FileChange) -> None:
        """Apply a single FileChange to the filesystem."""
        abs_path = self._project_root / change.file_path

        if change.change_type == "create":
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            content = change.new_content or ""
            abs_path.write_text(content, encoding="utf-8")

        elif change.change_type == "modify":
            if change.new_content is not None:
                abs_path.write_text(change.new_content, encoding="utf-8")
            elif change.patch is not None:
                # Simple line-based patch application
                self._apply_simple_patch(abs_path, change.patch)
            else:
                raise ValueError(
                    f"FileChange for {change.file_path} has neither "
                    f"new_content nor patch"
                )

        elif change.change_type == "delete":
            if abs_path.exists():
                abs_path.unlink()

        elif change.change_type == "rename":
            if change.rename_to is None:
                raise ValueError(
                    f"Rename change for {change.file_path} missing rename_to"
                )
            dest = self._project_root / change.rename_to
            dest.parent.mkdir(parents=True, exist_ok=True)
            abs_path.rename(dest)

    def _apply_simple_patch(self, file_path: Path, patch: str) -> None:
        """Apply a simple unified-diff-style patch.

        This is a best-effort line-based patcher.  For complex patches,
        callers should use ``new_content`` instead.
        """
        try:
            original = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Cannot read {file_path} for patching: {exc}") from exc

        lines = original.splitlines(keepends=True)
        result_lines: List[str] = []
        patch_lines = patch.splitlines(keepends=True)
        line_idx = 0
        patch_idx = 0

        while patch_idx < len(patch_lines):
            pline = patch_lines[patch_idx]
            if pline.startswith("---") or pline.startswith("+++") or pline.startswith("@@"):
                patch_idx += 1
                continue
            if pline.startswith("-"):
                # Skip this line from original
                line_idx += 1
                patch_idx += 1
            elif pline.startswith("+"):
                result_lines.append(pline[1:])
                patch_idx += 1
            elif pline.startswith(" "):
                if line_idx < len(lines):
                    result_lines.append(lines[line_idx])
                    line_idx += 1
                patch_idx += 1
            else:
                if line_idx < len(lines):
                    result_lines.append(lines[line_idx])
                    line_idx += 1
                patch_idx += 1

        # Append remaining original lines
        while line_idx < len(lines):
            result_lines.append(lines[line_idx])
            line_idx += 1

        file_path.write_text("".join(result_lines), encoding="utf-8")

    async def _find_files_containing(
        self, text: str, scope: str
    ) -> List[str]:
        """Find all files under project_root matching scope that contain text.

        Uses ``asyncio.create_subprocess_exec`` with argv-based invocation
        (no shell) for performance on large codebases.  Falls back to
        pure-Python scan if the subprocess fails.
        """
        try:
            # Argv-based — safe, no shell injection possible
            proc = await asyncio.create_subprocess_exec(
                "grep", "-rl", "--include", scope.replace("**/", ""),
                text, str(self._project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode == 0 and stdout:
                abs_paths = stdout.decode("utf-8", errors="replace").strip().splitlines()
                root_s = str(self._project_root)
                return [
                    os.path.relpath(p, root_s) for p in abs_paths
                    if p.startswith(root_s)
                ]
        except (OSError, asyncio.TimeoutError):
            pass

        # Fallback: pure-Python scan
        return await self._scan_files_python(text, scope)

    async def _scan_files_python(
        self, text: str, scope: str
    ) -> List[str]:
        """Pure-Python fallback file scanner."""
        results: List[str] = []
        glob_pattern = scope if "**" in scope else f"**/{scope}"

        for p in self._project_root.glob(glob_pattern):
            if not p.is_file():
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if text in content:
                results.append(str(p.relative_to(self._project_root)))
            if len(results) >= _MAX_FILES:
                break

        return results

    def _path_to_module(self, file_path: str) -> Optional[str]:
        """Convert a Python file path to a dotted module path.

        ``backend/core/auth.py`` -> ``backend.core.auth``
        Returns None for non-Python files.
        """
        p = Path(file_path)
        if p.suffix != ".py":
            return None
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else None
