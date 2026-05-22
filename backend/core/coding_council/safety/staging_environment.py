"""
v77.0: Staging Environment - Gap #16
=====================================

Hot-swap staging environment for safe code evolution:
- Isolated staging directory
- Git worktree for safe testing
- Atomic swap on validation success
- Automatic cleanup on failure
- Import path isolation

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class StagingStatus(Enum):
    """Status of staging environment."""
    CREATED = "created"
    READY = "ready"
    TESTING = "testing"
    VALIDATED = "validated"
    FAILED = "failed"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


@dataclass
class StagingResult:
    """Result of staging operation."""
    success: bool
    status: StagingStatus
    staging_path: Optional[Path] = None
    message: str = ""
    files_staged: List[str] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status.value,
            "staging_path": str(self.staging_path) if self.staging_path else None,
            "message": self.message,
            "files_staged": self.files_staged,
            "validation_errors": self.validation_errors,
        }


class StagingEnvironment:
    """
    Isolated staging environment for safe code changes.

    Features:
    - Git worktree-based isolation
    - Import path sandboxing
    - Atomic commit/rollback
    - Cleanup on failure
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.staging_base = Path.home() / ".jarvis" / "staging"
        self._active_stages: Dict[str, Path] = {}
        self._cleanup_lock = asyncio.Lock()

    async def create_staging(
        self,
        task_id: str,
        files: List[str],
        use_worktree: bool = True
    ) -> StagingResult:
        """
        Create a staging environment for files.

        Args:
            task_id: Unique task identifier
            files: List of files to stage
            use_worktree: Use git worktree if available

        Returns:
            StagingResult with staging path
        """
        staging_id = self._generate_staging_id(task_id)
        staging_path = self.staging_base / staging_id

        try:
            # Ensure base directory exists
            self.staging_base.mkdir(parents=True, exist_ok=True)

            # Create staging directory
            if staging_path.exists():
                shutil.rmtree(staging_path)
            staging_path.mkdir(parents=True)

            # Try git worktree first (cleanest isolation)
            if use_worktree and await self._has_git():
                success = await self._create_worktree(staging_path)
                if success:
                    self._active_stages[task_id] = staging_path
                    return StagingResult(
                        success=True,
                        status=StagingStatus.CREATED,
                        staging_path=staging_path,
                        message="Created git worktree staging",
                        files_staged=files,
                    )

            # Fallback: copy files
            await self._copy_files_to_staging(files, staging_path)

            self._active_stages[task_id] = staging_path

            return StagingResult(
                success=True,
                status=StagingStatus.CREATED,
                staging_path=staging_path,
                message="Created file-copy staging",
                files_staged=files,
            )

        except Exception as e:
            logger.error(f"[Staging] Failed to create staging: {e}")
            if staging_path.exists():
                shutil.rmtree(staging_path, ignore_errors=True)
            return StagingResult(
                success=False,
                status=StagingStatus.FAILED,
                message=str(e),
            )

    async def validate_staging(
        self,
        task_id: str,
        run_tests: bool = False
    ) -> StagingResult:
        """
        Validate staged changes.

        Args:
            task_id: Task identifier
            run_tests: Run unit tests in staging

        Returns:
            StagingResult with validation status
        """
        staging_path = self._active_stages.get(task_id)
        if not staging_path or not staging_path.exists():
            return StagingResult(
                success=False,
                status=StagingStatus.FAILED,
                message=f"No staging found for task {task_id}",
            )

        validation_errors = []

        try:
            # Import validation
            from .ast_validator import ASTValidator

            validator = ASTValidator(staging_path)

            # Get all Python files in staging
            py_files = list(staging_path.rglob("*.py"))
            py_files = [f for f in py_files if "__pycache__" not in str(f)]

            for py_file in py_files:
                result = await validator.validate_file(py_file)
                if not result.valid:
                    for error in result.errors:
                        validation_errors.append(f"{py_file}: {error.message}")

            # Run tests if requested
            if run_tests and not validation_errors:
                test_result = await self._run_staged_tests(staging_path)
                if not test_result["passed"]:
                    validation_errors.append(f"Tests failed: {test_result.get('message', '')}")

            if validation_errors:
                return StagingResult(
                    success=False,
                    status=StagingStatus.FAILED,
                    staging_path=staging_path,
                    message="Validation failed",
                    validation_errors=validation_errors,
                )

            return StagingResult(
                success=True,
                status=StagingStatus.VALIDATED,
                staging_path=staging_path,
                message="Validation passed",
            )

        except Exception as e:
            return StagingResult(
                success=False,
                status=StagingStatus.FAILED,
                staging_path=staging_path,
                message=f"Validation error: {e}",
                validation_errors=[str(e)],
            )

    async def commit_staging(
        self,
        task_id: str,
        files: Optional[List[str]] = None
    ) -> StagingResult:
        """
        Commit staged changes to main repository.

        This is an ATOMIC operation - either all files are
        committed or none are.

        Args:
            task_id: Task identifier
            files: Specific files to commit (None = all)

        Returns:
            StagingResult
        """
        staging_path = self._active_stages.get(task_id)
        if not staging_path or not staging_path.exists():
            return StagingResult(
                success=False,
                status=StagingStatus.FAILED,
                message=f"No staging found for task {task_id}",
            )

        try:
            # If using worktree, just merge back
            if await self._is_worktree(staging_path):
                success = await self._merge_worktree(staging_path)
                if success:
                    await self._cleanup_staging(task_id)
                    return StagingResult(
                        success=True,
                        status=StagingStatus.COMMITTED,
                        message="Merged worktree changes",
                    )

            # File-copy staging: atomic copy back
            committed_files = []

            # Determine which files to commit
            if files:
                files_to_commit = files
            else:
                # Get all modified files
                files_to_commit = await self._get_staged_files(staging_path)

            # Create backup of original files
            backup_path = staging_path.parent / f"{staging_path.name}_backup"
            backup_path.mkdir(parents=True, exist_ok=True)

            try:
                # Backup originals
                for rel_path in files_to_commit:
                    original = self.repo_root / rel_path
                    if original.exists():
                        backup = backup_path / rel_path
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(original, backup)

                # Copy staged files to repo
                for rel_path in files_to_commit:
                    staged = staging_path / rel_path
                    if staged.exists():
                        target = self.repo_root / rel_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(staged, target)
                        committed_files.append(rel_path)

                # Success - cleanup backup
                shutil.rmtree(backup_path, ignore_errors=True)

            except Exception as e:
                # Restore from backup
                logger.error(f"[Staging] Commit failed, restoring: {e}")
                for rel_path in files_to_commit:
                    backup = backup_path / rel_path
                    if backup.exists():
                        target = self.repo_root / rel_path
                        shutil.copy2(backup, target)
                shutil.rmtree(backup_path, ignore_errors=True)
                raise

            await self._cleanup_staging(task_id)

            return StagingResult(
                success=True,
                status=StagingStatus.COMMITTED,
                message="Committed staged changes",
                files_staged=committed_files,
            )

        except Exception as e:
            logger.error(f"[Staging] Commit failed: {e}")
            return StagingResult(
                success=False,
                status=StagingStatus.FAILED,
                staging_path=staging_path,
                message=f"Commit failed: {e}",
            )

    async def rollback_staging(self, task_id: str) -> StagingResult:
        """
        Rollback and cleanup staging.

        Args:
            task_id: Task identifier

        Returns:
            StagingResult
        """
        staging_path = self._active_stages.get(task_id)
        if not staging_path:
            return StagingResult(
                success=True,
                status=StagingStatus.ROLLED_BACK,
                message="No staging to rollback",
            )

        try:
            await self._cleanup_staging(task_id)
            return StagingResult(
                success=True,
                status=StagingStatus.ROLLED_BACK,
                message="Staging rolled back",
            )
        except Exception as e:
            return StagingResult(
                success=False,
                status=StagingStatus.FAILED,
                message=f"Rollback failed: {e}",
            )

    @asynccontextmanager
    async def staging_context(
        self,
        task_id: str,
        files: List[str],
        auto_commit: bool = False
    ) -> AsyncIterator[Path]:
        """
        Context manager for staging environment.

        Usage:
            async with staging.staging_context(task_id, files) as staging_path:
                # Make changes in staging_path
                # Validate...
            # Auto-cleanup on exit unless auto_commit=True and validation passed
        """
        result = await self.create_staging(task_id, files)

        if not result.success:
            raise RuntimeError(f"Failed to create staging: {result.message}")

        try:
            yield result.staging_path

            if auto_commit:
                validation = await self.validate_staging(task_id)
                if validation.success:
                    await self.commit_staging(task_id)
                else:
                    await self.rollback_staging(task_id)
        except Exception:
            await self.rollback_staging(task_id)
            raise
        finally:
            # Ensure cleanup
            if task_id in self._active_stages:
                await self.rollback_staging(task_id)

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _generate_staging_id(self, task_id: str) -> str:
        """Generate unique staging directory name."""
        import time
        content = f"{task_id}:{time.time()}"
        return f"stage_{hashlib.md5(content.encode()).hexdigest()[:12]}"

    async def _has_git(self) -> bool:
        """Check if git is available."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _create_worktree(self, staging_path: Path) -> bool:
        """Create git worktree for staging."""
        try:
            # Get current branch
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            branch = stdout.decode().strip()

            # Create worktree
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", "-b", f"staging-{staging_path.name}",
                str(staging_path), branch,
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            return proc.returncode == 0

        except Exception as e:
            logger.warning(f"[Staging] Worktree creation failed: {e}")
            return False

    async def _is_worktree(self, staging_path: Path) -> bool:
        """Check if staging path is a git worktree."""
        git_file = staging_path / ".git"
        return git_file.is_file()  # Worktrees have .git as file, not directory

    async def _merge_worktree(self, staging_path: Path) -> bool:
        """Merge worktree changes back to main."""
        try:
            # Commit changes in worktree
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "-A",
                cwd=str(staging_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", "Staging changes",
                cwd=str(staging_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            # Get staging branch name
            branch_name = f"staging-{staging_path.name}"

            # Merge into main branch
            proc = await asyncio.create_subprocess_exec(
                "git", "merge", branch_name, "--no-ff", "-m", "Merge staging changes",
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            # Remove worktree
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", str(staging_path),
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            # Delete staging branch
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "-d", branch_name,
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            return True

        except Exception as e:
            logger.error(f"[Staging] Worktree merge failed: {e}")
            return False

    async def _copy_files_to_staging(
        self,
        files: List[str],
        staging_path: Path
    ) -> None:
        """Copy files to staging directory."""
        for rel_path in files:
            source = self.repo_root / rel_path
            target = staging_path / rel_path

            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    async def _get_staged_files(self, staging_path: Path) -> List[str]:
        """Get list of files in staging relative to staging root."""
        files = []
        for f in staging_path.rglob("*"):
            if f.is_file() and "__pycache__" not in str(f) and ".git" not in str(f):
                files.append(str(f.relative_to(staging_path)))
        return files

    async def _run_staged_tests(self, staging_path: Path) -> Dict[str, Any]:
        """Run tests in staging environment.

        Slice 9 — routes through canonical helper (stdin=DEVNULL +
        process-group isolation + provenance + bounded timeout)."""
        from backend.core.ouroboros.governance.test_subprocess_helper import (  # noqa: E501
            KillReason,
            run_pytest_subprocess,
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(staging_path)
        result = await run_pytest_subprocess(
            [sys.executable, "-m", "pytest", "-x", "--tb=short", "-q"],
            cwd=str(staging_path),
            timeout_s=120.0,
            caller="coding_council.staging_environment._run_staged_tests",
            env=env,
        )
        if result.timed_out:
            return {"passed": False, "message": "Tests timed out"}
        if result.kill_reason == KillReason.SPAWN_ERROR:
            return {
                "passed": False,
                "message": str(result.spawn_error_class),
            }
        return {
            "passed": result.returncode == 0,
            "output": result.stdout,
            "error": "",  # helper merges stderr into stdout
        }

    async def _cleanup_staging(self, task_id: str) -> None:
        """Clean up staging environment."""
        async with self._cleanup_lock:
            staging_path = self._active_stages.pop(task_id, None)
            if staging_path and staging_path.exists():
                # If worktree, remove properly
                if await self._is_worktree(staging_path):
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "git", "worktree", "remove", "--force", str(staging_path),
                            cwd=str(self.repo_root),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        await proc.communicate()
                    except Exception:
                        pass

                # Fallback: rmtree
                if staging_path.exists():
                    shutil.rmtree(staging_path, ignore_errors=True)
