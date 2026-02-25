"""
JARVIS Split-Brain Guard v1.0
===============================
Prevents split-brain conditions where multiple supervisor instances believe
they hold exclusive locks simultaneously.

Root causes cured:
  - Supervisor singleton and DLM use DIFFERENT lock directories
  - Lock directory resolved ONCE at module load, never revalidated
  - Two processes can resolve to DIFFERENT directories when writeability changes
  - Fencing tokens are per-instance counters, useless across instances
  - Keepalive exhaustion silently releases locks

v272.x: Created as part of Phase 10 — split-brain singleton safety.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache for canonical lock dir (per-process, set once)
# ---------------------------------------------------------------------------
_canonical_lock_dir_cache: Optional[Path] = None
_canonical_lock_dir_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Environment variable names
# ---------------------------------------------------------------------------
ENV_LOCK_DIR = "JARVIS_LOCK_DIR"
ENV_FENCING_TOKEN_FILE = "JARVIS_FENCING_TOKEN_FILE"
ENV_SWEEP_ENABLED = "JARVIS_SPLIT_BRAIN_SWEEP_ENABLED"


# ============================================================================
# Public API — Directory Resolution
# ============================================================================


def canonical_lock_dir() -> Path:
    """Return the single canonical lock directory all lock consumers must use.

    Priority:
      1. ``JARVIS_LOCK_DIR`` environment variable (if set and writable)
      2. ``~/.jarvis/locks``
      3. ``/tmp/jarvis/locks``

    If **none** of the candidates are writable a ``RuntimeError`` is raised.
    We deliberately refuse to silently fall back to a PID-scoped directory
    because that is the root cause of the split-brain disease — two processes
    resolving to different directories and each believing they own the lock.

    The result is cached for the lifetime of the process.
    """
    global _canonical_lock_dir_cache

    # Fast path (no lock required once set — immutable after first write)
    if _canonical_lock_dir_cache is not None:
        return _canonical_lock_dir_cache

    with _canonical_lock_dir_lock:
        # Double-checked locking
        if _canonical_lock_dir_cache is not None:
            return _canonical_lock_dir_cache

        candidates = _build_candidate_dirs()

        for candidate in candidates:
            if validate_lock_dir_writeable(candidate):
                _canonical_lock_dir_cache = candidate
                logger.info(
                    "split-brain-guard: canonical lock dir resolved to %s",
                    candidate,
                )
                return candidate

        # Hard failure — DO NOT fall back to PID-scoped dir.
        tried = ", ".join(str(c) for c in candidates)
        raise RuntimeError(
            f"split-brain-guard: NONE of the candidate lock directories are "
            f"writable. Candidates tried: [{tried}].  Cannot proceed — "
            f"falling back to a PID-scoped directory would cause the exact "
            f"split-brain condition this module exists to prevent."
        )


def canonical_cross_repo_lock_dir() -> Path:
    """Return ``canonical_lock_dir() / "cross_repo"``, creating it if needed."""
    cross = canonical_lock_dir() / "cross_repo"
    cross.mkdir(parents=True, exist_ok=True)
    return cross


def validate_lock_dir_writeable(lock_dir: Path) -> bool:
    """Probe-file check: create temp file, write token, read back, delete.

    Suitable for runtime re-validation (not just module load).
    """
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    probe_name = f".sbg_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    probe = lock_dir / probe_name
    token = uuid.uuid4().hex

    try:
        probe.write_text(token, encoding="utf-8")
        readback = probe.read_text(encoding="utf-8")
        if readback != token:
            return False
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


# ============================================================================
# LockCanary — detects stale/stolen locks via sidecar token files
# ============================================================================


class LockCanary:
    """Write and verify a unique sidecar token alongside each lock file.

    All write operations are atomic (write to temp, then ``os.rename``).
    """

    _SUFFIX = ".canary"

    @staticmethod
    def write(lock_file: Path) -> str:
        """Create ``{lock_file}.canary`` with a unique token.  Returns the token."""
        token = uuid.uuid4().hex
        canary_path = lock_file.with_name(lock_file.name + LockCanary._SUFFIX)
        tmp_path = canary_path.with_suffix(".canary.tmp")

        try:
            tmp_path.write_text(token, encoding="utf-8")
            os.rename(str(tmp_path), str(canary_path))
        except OSError:
            # Best-effort cleanup of temp file on failure
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        logger.debug(
            "split-brain-guard: canary written for %s (token=%s…)",
            lock_file.name,
            token[:8],
        )
        return token

    @staticmethod
    def verify(lock_file: Path, expected_token: str) -> bool:
        """Read the canary file and return ``True`` if it matches *expected_token*."""
        canary_path = lock_file.with_name(lock_file.name + LockCanary._SUFFIX)
        try:
            actual = canary_path.read_text(encoding="utf-8").strip()
            return actual == expected_token
        except OSError:
            return False

    @staticmethod
    def cleanup(lock_file: Path) -> None:
        """Remove the canary file.  Silent on any error."""
        canary_path = lock_file.with_name(lock_file.name + LockCanary._SUFFIX)
        try:
            canary_path.unlink(missing_ok=True)
        except OSError:
            pass


# ============================================================================
# CrossDirectorySweep — detect competing locks across candidate dirs
# ============================================================================


class CrossDirectorySweep:
    """Sweep all candidate lock directories for a competing lock held by
    a *different* live process.
    """

    @staticmethod
    def sweep(lock_name: str, my_pid: int) -> Optional[Dict]:
        """Check all candidate directories for a competing lock file.

        Returns a dict with details of the competing lock if found, else
        ``None`` (clear).
        """
        if not _sweep_enabled():
            return None

        candidates = _build_candidate_dirs()
        for candidate in candidates:
            lock_file = candidate / lock_name
            if not lock_file.exists():
                # Also check DLM-style naming
                dlm_file = candidate / f"{lock_name}.dlm.lock"
                if dlm_file.exists():
                    lock_file = dlm_file
                else:
                    continue

            competing_pid = _read_pid_from_lock(lock_file)
            if competing_pid is None:
                continue
            if competing_pid == my_pid:
                continue
            if not _pid_alive(competing_pid):
                continue

            detail = {
                "lock_name": lock_name,
                "lock_file": str(lock_file),
                "competing_pid": competing_pid,
                "my_pid": my_pid,
                "directory": str(candidate),
                "timestamp": time.time(),
            }
            logger.warning(
                "split-brain-guard: COMPETING LOCK detected! %s",
                json.dumps(detail, indent=2),
            )
            return detail

        return None


# ============================================================================
# PersistentFencingToken — file-based, cross-process, monotonic counter
# ============================================================================


class PersistentFencingToken:
    """Monotonically increasing fencing token backed by a shared file.

    Thread-safe via ``threading.Lock`` (in-process) and ``fcntl.flock``
    (cross-process).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        env_override = os.environ.get(ENV_FENCING_TOKEN_FILE, "").strip()
        if path is not None:
            self._path = path
        elif env_override:
            self._path = Path(env_override).expanduser()
        else:
            self._path = canonical_lock_dir() / ".fencing_counter"

        # Ensure parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._thread_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def next_token(self) -> int:
        """Atomically read-increment-write the counter.  Returns the NEW value.

        Cross-process safety via ``fcntl.flock`` (exclusive).
        In-process safety via ``threading.Lock``.
        """
        with self._thread_lock:
            return self._next_token_locked()

    def current_value(self) -> int:
        """Read the current counter value without incrementing."""
        with self._thread_lock:
            try:
                text = self._path.read_text(encoding="utf-8").strip()
                return int(text)
            except (OSError, ValueError):
                return 0

    # -- private --------------------------------------------------------

    def _next_token_locked(self) -> int:
        """Perform the actual read-increment-write under fcntl.flock."""
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                raw = os.read(fd, 64)
                current = int(raw.decode("utf-8").strip()) if raw else 0
            except (ValueError, UnicodeDecodeError):
                current = 0

            new_value = current + 1
            payload = str(new_value).encode("utf-8")

            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)

            return new_value
        finally:
            # flock is released on close
            os.close(fd)


# ============================================================================
# KeepaliveBreachSignal — file-based breach flag per lock
# ============================================================================


class KeepaliveBreachSignal:
    """Signal and detect keepalive-exhaustion breaches for named locks.

    Each breach is represented by a file ``{lock_dir}/{lock_name}.breach``.
    """

    _SUFFIX = ".breach"

    def __init__(self, lock_dir: Optional[Path] = None) -> None:
        self._lock_dir = lock_dir or canonical_lock_dir()
        self._thread_lock = threading.Lock()

    def signal_breach(self, lock_name: str) -> None:
        """Set the breach flag for *lock_name*."""
        breach_file = self._breach_path(lock_name)
        payload = json.dumps(
            {
                "lock_name": lock_name,
                "pid": os.getpid(),
                "timestamp": time.time(),
                "reason": "keepalive_exhaustion",
            },
            indent=2,
        )
        with self._thread_lock:
            try:
                tmp = breach_file.with_suffix(".breach.tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.rename(str(tmp), str(breach_file))
                logger.warning(
                    "split-brain-guard: keepalive breach signalled for '%s'",
                    lock_name,
                )
            except OSError as exc:
                logger.error(
                    "split-brain-guard: failed to signal breach for '%s': %s",
                    lock_name,
                    exc,
                )

    def check_breach(self, lock_name: str) -> bool:
        """Return ``True`` if a breach file exists for *lock_name*."""
        return self._breach_path(lock_name).exists()

    def clear_breach(self, lock_name: str) -> None:
        """Remove the breach file for *lock_name*.  Silent on error."""
        with self._thread_lock:
            try:
                self._breach_path(lock_name).unlink(missing_ok=True)
                logger.info(
                    "split-brain-guard: breach cleared for '%s'",
                    lock_name,
                )
            except OSError:
                pass

    def read_breach(self, lock_name: str) -> Optional[Dict]:
        """Read and parse the breach file.  Returns dict or ``None``."""
        breach_file = self._breach_path(lock_name)
        try:
            text = breach_file.read_text(encoding="utf-8")
            return json.loads(text)
        except (OSError, json.JSONDecodeError):
            return None

    # -- private --------------------------------------------------------

    def _breach_path(self, lock_name: str) -> Path:
        return self._lock_dir / f"{lock_name}{self._SUFFIX}"


# ============================================================================
# Internal helpers
# ============================================================================


def _build_candidate_dirs() -> List[Path]:
    """Build the ordered list of candidate lock directories."""
    candidates: List[Path] = []

    env_lock = os.environ.get(ENV_LOCK_DIR, "").strip()
    if env_lock:
        candidates.append(Path(env_lock).expanduser())

    candidates.append(Path.home() / ".jarvis" / "locks")
    candidates.append(Path("/tmp/jarvis/locks"))

    # Deduplicate while preserving order (resolve to handle symlinks)
    seen = set()
    deduped: List[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            resolved = c
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(c)

    return deduped


def _sweep_enabled() -> bool:
    """Check if cross-directory sweep is enabled (default: true)."""
    val = os.environ.get(ENV_SWEEP_ENABLED, "true").strip().lower()
    return val in ("true", "1", "yes", "on")


def _read_pid_from_lock(lock_file: Path) -> Optional[int]:
    """Attempt to extract a PID from a lock file.

    Supports two formats:
      1. Plain text — first line is an integer PID.
      2. JSON — has a ``"pid"`` key.
    """
    try:
        raw = lock_file.read_text(encoding="utf-8").strip()
        if not raw:
            return None

        # Try JSON first (DLM-style)
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                pid_val = data.get("pid") or data.get("owner_pid")
                if pid_val is not None:
                    return int(pid_val)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        # Fall back to first-line integer
        first_line = raw.split("\n", 1)[0].strip()
        return int(first_line)
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Return ``True`` if *pid* refers to a running process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it
        return True
    except OSError:
        return False
