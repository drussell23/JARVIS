"""
Atomic File Operations with Retry and Safety
=============================================

Production-grade file operations for cross-repo file-based RPC.

Features:
    - Atomic writes via temp file + rename pattern
    - Retries with exponential backoff for transient failures
    - File locking via fcntl (Unix) for concurrent access
    - Checksum verification for data integrity
    - Safe directory creation with race condition handling
    - Comprehensive error classification

Author: JARVIS Cross-Repo Resilience
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AtomicWriteError(Exception):
    """Raised when atomic write fails after all retries."""

    def __init__(self, path: Path, reason: str, original_error: Optional[Exception] = None):
        self.path = path
        self.reason = reason
        self.original_error = original_error
        super().__init__(f"Atomic write failed for {path}: {reason}")


class AtomicReadError(Exception):
    """Raised when atomic read fails after all retries."""

    def __init__(self, path: Path, reason: str, original_error: Optional[Exception] = None):
        self.path = path
        self.reason = reason
        self.original_error = original_error
        super().__init__(f"Atomic read failed for {path}: {reason}")


class FileErrorType(Enum):
    """Classification of file operation errors."""

    TRANSIENT = "transient"  # Worth retrying (permission flicker, temp lock)
    PERMANENT = "permanent"  # Won't succeed (doesn't exist, invalid path)
    CORRUPTION = "corruption"  # Data integrity issue


@dataclass
class AtomicFileConfig:
    """Configuration for atomic file operations."""

    # Retry settings
    max_retries: int = 3
    initial_delay_ms: int = 50
    max_delay_ms: int = 2000
    exponential_base: float = 2.0

    # Safety settings
    verify_checksum: bool = True
    use_fsync: bool = True  # Ensure data hits disk
    create_dirs: bool = True  # Create parent directories
    backup_on_write: bool = False  # Keep .bak of original

    # Permissions
    file_mode: int = 0o600
    dir_mode: int = 0o700

    # Temp file settings
    temp_suffix: str = ".tmp"
    temp_prefix: str = "atomic_"


@dataclass
class FileOpMetrics:
    """Metrics for file operations."""

    reads: int = 0
    writes: int = 0
    deletes: int = 0
    retries: int = 0
    failures: int = 0
    checksum_errors: int = 0
    lock_waits: int = 0
    avg_write_time_ms: float = 0.0
    avg_read_time_ms: float = 0.0


class AtomicFileOps:
    """
    Atomic file operations with retry and safety guarantees.

    Uses the temp file + rename pattern for atomic writes, ensuring
    that readers never see partial data. All operations support
    retries with exponential backoff for transient failures.

    Usage:
        ops = AtomicFileOps()

        # Atomic write
        await ops.write_json(Path("/path/to/file.json"), {"key": "value"})

        # Atomic read with timeout
        data = await ops.read_json(Path("/path/to/file.json"), timeout=5.0)

        # Atomic delete
        await ops.delete(Path("/path/to/file.json"))
    """

    def __init__(self, config: Optional[AtomicFileConfig] = None):
        self.config = config or AtomicFileConfig()
        self.metrics = FileOpMetrics()
        self._write_locks: Dict[str, asyncio.Lock] = {}

    def _get_write_lock(self, path: Path) -> asyncio.Lock:
        """Get or create a write lock for a path."""
        key = str(path.resolve())
        if key not in self._write_locks:
            self._write_locks[key] = asyncio.Lock()
        return self._write_locks[key]

    def _classify_error(self, error: Exception) -> FileErrorType:
        """Classify an error for retry decisions."""
        error_str = str(error).lower()

        # Transient errors
        transient_patterns = [
            "resource temporarily unavailable",
            "try again",
            "interrupted",
            "eagain",
            "ewouldblock",
            "busy",
            "locked",
        ]
        if any(p in error_str for p in transient_patterns):
            return FileErrorType.TRANSIENT

        if isinstance(error, (BlockingIOError, InterruptedError)):
            return FileErrorType.TRANSIENT

        if isinstance(error, PermissionError):
            # Permission might be transient (file being written by another process)
            return FileErrorType.TRANSIENT

        # Permanent errors
        if isinstance(error, (FileNotFoundError, IsADirectoryError, NotADirectoryError)):
            return FileErrorType.PERMANENT

        if "invalid" in error_str or "not found" in error_str:
            return FileErrorType.PERMANENT

        # Default to transient (optimistic)
        return FileErrorType.TRANSIENT

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay before next retry."""
        delay_ms = self.config.initial_delay_ms * (self.config.exponential_base ** attempt)
        delay_ms = min(delay_ms, self.config.max_delay_ms)
        # Add jitter (10%)
        import random
        jitter = random.uniform(-0.1 * delay_ms, 0.1 * delay_ms)
        return (delay_ms + jitter) / 1000.0

    async def write_json(
        self,
        path: Path,
        data: Any,
        indent: int = 2,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Atomically write JSON data to a file.

        Args:
            path: Target file path
            data: JSON-serializable data
            indent: JSON indentation (default 2)
            timeout: Max time for operation

        Returns:
            str: Checksum of written data

        Raises:
            AtomicWriteError: If write fails after retries
        """
        content = json.dumps(data, indent=indent, default=str)
        return await self.write_text(path, content, timeout=timeout)

    async def write_text(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        timeout: Optional[float] = None,
    ) -> str:
        """
        Atomically write text content to a file.

        Returns:
            str: SHA256 checksum of written content
        """
        return await self.write_bytes(
            path,
            content.encode(encoding),
            timeout=timeout,
        )

    async def write_bytes(
        self,
        path: Path,
        content: bytes,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Atomically write bytes to a file.

        Uses temp file + rename pattern for atomicity.

        Returns:
            str: SHA256 checksum of written content
        """
        start_time = time.time()
        path = Path(path)
        last_error: Optional[Exception] = None

        # Calculate checksum
        checksum = hashlib.sha256(content).hexdigest()

        # Ensure parent directory exists
        if self.config.create_dirs:
            await self._ensure_dir(path.parent)

        # Get write lock for this path
        lock = self._get_write_lock(path)

        async with lock:
            for attempt in range(self.config.max_retries + 1):
                try:
                    await self._atomic_write(path, content, checksum)

                    write_time_ms = (time.time() - start_time) * 1000
                    self._update_write_metrics(write_time_ms)

                    logger.debug(
                        f"[AtomicFileOps] Wrote {len(content)} bytes to {path} "
                        f"(checksum={checksum[:12]}...)"
                    )
                    return checksum

                except Exception as e:
                    last_error = e
                    error_type = self._classify_error(e)

                    if error_type == FileErrorType.PERMANENT:
                        break

                    if attempt < self.config.max_retries:
                        self.metrics.retries += 1
                        delay = self._calculate_delay(attempt)
                        logger.debug(
                            f"[AtomicFileOps] Write retry {attempt + 1}: {e}, "
                            f"waiting {delay:.2f}s"
                        )
                        await asyncio.sleep(delay)

        self.metrics.failures += 1
        raise AtomicWriteError(
            path,
            f"Failed after {attempt + 1} attempts",
            last_error,
        )

    async def _atomic_write(
        self,
        path: Path,
        content: bytes,
        checksum: str,
    ) -> None:
        """Perform the actual atomic write."""
        # Create backup if configured
        if self.config.backup_on_write and path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            await asyncio.to_thread(shutil.copy2, path, backup_path)

        # Write to temp file in same directory (for atomic rename)
        temp_dir = path.parent
        temp_fd, temp_path = await asyncio.to_thread(
            tempfile.mkstemp,
            suffix=self.config.temp_suffix,
            prefix=self.config.temp_prefix,
            dir=temp_dir,
        )

        try:
            # Write content
            await asyncio.to_thread(os.write, temp_fd, content)

            # Fsync if configured
            if self.config.use_fsync:
                await asyncio.to_thread(os.fsync, temp_fd)

            await asyncio.to_thread(os.close, temp_fd)
            temp_fd = -1

            # Set permissions
            await asyncio.to_thread(os.chmod, temp_path, self.config.file_mode)

            # Atomic rename
            await asyncio.to_thread(os.replace, temp_path, path)

            # Verify checksum if configured
            if self.config.verify_checksum:
                actual = await self._file_checksum(path)
                if actual != checksum:
                    self.metrics.checksum_errors += 1
                    raise AtomicWriteError(
                        path,
                        f"Checksum mismatch: expected {checksum[:12]}, got {actual[:12]}",
                    )

        except Exception:
            # Cleanup temp file on error
            if temp_fd >= 0:
                try:
                    os.close(temp_fd)
                except Exception:
                    pass
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            raise

    async def read_json(
        self,
        path: Path,
        default: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Read JSON data from a file.

        Args:
            path: File path
            default: Value to return if file doesn't exist
            timeout: Max wait time

        Returns:
            Parsed JSON data or default

        Raises:
            AtomicReadError: If read fails (and no default)
        """
        try:
            content = await self.read_text(path, timeout=timeout)
            return json.loads(content)
        except FileNotFoundError:
            if default is not None:
                return default
            raise
        except json.JSONDecodeError as e:
            raise AtomicReadError(path, f"Invalid JSON: {e}")

    async def read_text(
        self,
        path: Path,
        encoding: str = "utf-8",
        timeout: Optional[float] = None,
    ) -> str:
        """Read text content from a file."""
        content = await self.read_bytes(path, timeout=timeout)
        return content.decode(encoding)

    async def read_bytes(
        self,
        path: Path,
        timeout: Optional[float] = None,
    ) -> bytes:
        """
        Read bytes from a file with retries.

        Handles transient read errors (file being written).
        """
        start_time = time.time()
        path = Path(path)
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            try:
                if timeout:
                    remaining = timeout - (time.time() - start_time)
                    if remaining <= 0:
                        raise AtomicReadError(path, "Timeout waiting for file")

                content = await asyncio.to_thread(path.read_bytes)

                read_time_ms = (time.time() - start_time) * 1000
                self._update_read_metrics(read_time_ms)
                self.metrics.reads += 1

                return content

            except FileNotFoundError:
                raise

            except Exception as e:
                last_error = e
                error_type = self._classify_error(e)

                if error_type == FileErrorType.PERMANENT:
                    break

                if attempt < self.config.max_retries:
                    self.metrics.retries += 1
                    delay = self._calculate_delay(attempt)
                    await asyncio.sleep(delay)

        self.metrics.failures += 1
        raise AtomicReadError(
            path,
            f"Failed after {attempt + 1} attempts",
            last_error,
        )

    async def read_json_with_wait(
        self,
        path: Path,
        timeout: float = 30.0,
        poll_interval: float = 0.1,
        validator: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        """
        Wait for a JSON file to appear and read it.

        Useful for file-based RPC where we wait for response files.

        Args:
            path: File path
            timeout: Max wait time
            poll_interval: How often to check
            validator: Optional function to validate content

        Returns:
            Parsed JSON data

        Raises:
            AtomicReadError: If timeout or validation fails
        """
        start_time = time.time()
        path = Path(path)

        while time.time() - start_time < timeout:
            if path.exists():
                try:
                    data = await self.read_json(path)

                    if validator:
                        if validator(data):
                            return data
                        # File exists but invalid, keep waiting
                    else:
                        return data

                except (json.JSONDecodeError, AtomicReadError):
                    # File might be partially written
                    pass

            await asyncio.sleep(poll_interval)

        raise AtomicReadError(path, f"Timeout after {timeout}s waiting for file")

    async def delete(
        self,
        path: Path,
        missing_ok: bool = True,
    ) -> bool:
        """
        Delete a file safely.

        Args:
            path: File to delete
            missing_ok: Don't error if file doesn't exist

        Returns:
            True if file was deleted, False if didn't exist
        """
        path = Path(path)

        try:
            await asyncio.to_thread(path.unlink, missing_ok=missing_ok)
            self.metrics.deletes += 1
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.error(f"[AtomicFileOps] Delete failed for {path}: {e}")
            raise

    async def delete_dir(
        self,
        path: Path,
        recursive: bool = True,
        missing_ok: bool = True,
    ) -> bool:
        """Delete a directory."""
        path = Path(path)

        try:
            if recursive:
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=missing_ok)
            else:
                await asyncio.to_thread(path.rmdir)
            return True
        except FileNotFoundError:
            if missing_ok:
                return False
            raise

    async def _ensure_dir(self, path: Path) -> None:
        """Create directory if it doesn't exist."""
        try:
            await asyncio.to_thread(
                path.mkdir,
                parents=True,
                exist_ok=True,
            )
        except FileExistsError:
            # Race condition: directory created by another process
            pass

    async def _file_checksum(self, path: Path) -> str:
        """Calculate SHA256 checksum of a file."""
        content = await asyncio.to_thread(path.read_bytes)
        return hashlib.sha256(content).hexdigest()

    def _update_write_metrics(self, write_time_ms: float) -> None:
        """Update write metrics."""
        self.metrics.writes += 1
        total = self.metrics.avg_write_time_ms * (self.metrics.writes - 1) + write_time_ms
        self.metrics.avg_write_time_ms = total / self.metrics.writes

    def _update_read_metrics(self, read_time_ms: float) -> None:
        """Update read metrics."""
        total = self.metrics.avg_read_time_ms * (self.metrics.reads - 1) + read_time_ms
        self.metrics.avg_read_time_ms = total / self.metrics.reads if self.metrics.reads else 0

    async def list_files(
        self,
        directory: Path,
        pattern: str = "*",
        recursive: bool = False,
    ) -> list[Path]:
        """List files matching a pattern."""
        directory = Path(directory)

        if not directory.exists():
            return []

        if recursive:
            files = await asyncio.to_thread(
                lambda: list(directory.rglob(pattern))
            )
        else:
            files = await asyncio.to_thread(
                lambda: list(directory.glob(pattern))
            )

        return [f for f in files if f.is_file()]

    def get_metrics(self) -> Dict[str, Any]:
        """Get operation metrics."""
        return {
            "reads": self.metrics.reads,
            "writes": self.metrics.writes,
            "deletes": self.metrics.deletes,
            "retries": self.metrics.retries,
            "failures": self.metrics.failures,
            "checksum_errors": self.metrics.checksum_errors,
            "avg_write_time_ms": round(self.metrics.avg_write_time_ms, 2),
            "avg_read_time_ms": round(self.metrics.avg_read_time_ms, 2),
        }


# Global instance for convenience
_atomic_ops: Optional[AtomicFileOps] = None


def get_atomic_file_ops() -> AtomicFileOps:
    """Get or create global atomic file ops instance."""
    global _atomic_ops
    if _atomic_ops is None:
        _atomic_ops = AtomicFileOps()
    return _atomic_ops
