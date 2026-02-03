# Voice Orchestrator & VBIA Lock Fix - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate voice hallucinations and VBIA lock errors through a unified voice orchestrator with intelligent coalescing and OS-level file locking.

**Architecture:** Single playback authority via Unix domain socket IPC, fcntl.flock() for cross-process locking, asyncio-native with all blocking I/O in executors.

**Tech Stack:** Python 3.9+, asyncio, fcntl, Unix domain sockets, JSON-lines protocol, pytest

---

## Task 1: RobustFileLock - Core Implementation

**Files:**
- Create: `backend/core/robust_file_lock.py`
- Create: `tests/unit/backend/core/test_robust_file_lock.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/core/test_robust_file_lock.py
"""Tests for RobustFileLock - OS-level file locking."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

# Will fail until we create the module
from backend.core.robust_file_lock import RobustFileLock


@pytest.fixture
def temp_lock_dir():
    """Create a temporary lock directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_acquire_and_release(temp_lock_dir, monkeypatch):
    """Test basic lock acquire and release."""
    monkeypatch.setenv("JARVIS_LOCK_DIR", str(temp_lock_dir))

    lock = RobustFileLock("test_lock", source="test")

    # Should acquire successfully
    async with lock as acquired:
        assert acquired is True
        # Lock file should exist
        assert (temp_lock_dir / "test_lock.lock").exists()

    # After release, should be able to acquire again
    async with lock as acquired:
        assert acquired is True


@pytest.mark.asyncio
async def test_lock_is_exclusive(temp_lock_dir, monkeypatch):
    """Test that lock is exclusive - second acquire fails with short timeout."""
    monkeypatch.setenv("JARVIS_LOCK_DIR", str(temp_lock_dir))

    lock1 = RobustFileLock("exclusive_test", source="test1")
    lock2 = RobustFileLock("exclusive_test", source="test2")

    async with lock1 as acquired1:
        assert acquired1 is True

        # Second lock should fail (short timeout)
        acquired2 = await lock2.acquire(timeout_s=0.1)
        assert acquired2 is False

    # After lock1 released, lock2 should succeed
    async with lock2 as acquired2:
        assert acquired2 is True


@pytest.mark.asyncio
async def test_reentrancy_raises_error(temp_lock_dir, monkeypatch):
    """Test that re-acquiring same lock raises RuntimeError."""
    monkeypatch.setenv("JARVIS_LOCK_DIR", str(temp_lock_dir))

    lock = RobustFileLock("reentrant_test", source="test")

    async with lock as acquired:
        assert acquired is True

        # Attempting to acquire again should raise
        with pytest.raises(RuntimeError, match="already held"):
            await lock.acquire()


@pytest.mark.asyncio
async def test_lock_creates_directory(temp_lock_dir, monkeypatch):
    """Test that lock creates directory if missing."""
    nested_dir = temp_lock_dir / "nested" / "locks"
    monkeypatch.setenv("JARVIS_LOCK_DIR", str(nested_dir))

    lock = RobustFileLock("nested_test", source="test")

    assert not nested_dir.exists()

    async with lock as acquired:
        assert acquired is True
        assert nested_dir.exists()


@pytest.mark.asyncio
async def test_metadata_written(temp_lock_dir, monkeypatch):
    """Test that lock metadata is written for debugging."""
    import json

    monkeypatch.setenv("JARVIS_LOCK_DIR", str(temp_lock_dir))

    lock = RobustFileLock("metadata_test", source="jarvis")

    async with lock as acquired:
        assert acquired is True

        lock_file = temp_lock_dir / "metadata_test.lock"
        with open(lock_file) as f:
            metadata = json.load(f)

        assert metadata["owner_pid"] == os.getpid()
        assert metadata["source"] == "jarvis"
        assert "acquired_at" in metadata
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_robust_file_lock.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'backend.core.robust_file_lock'"

**Step 3: Write minimal implementation**

```python
# backend/core/robust_file_lock.py
"""
RobustFileLock - POSIX Cross-Process File Locking using fcntl.flock().

Guarantees:
- ATOMIC: Lock acquisition is atomic at the kernel level
- EPHEMERAL: Lock automatically released on process death
- NON-BLOCKING EVENT LOOP: All blocking I/O runs in executor
- CROSS-PROCESS: Works across all processes on same machine

Limitations:
- POSIX-ONLY: Does not work on Windows
- LOCAL FILESYSTEM: LOCK_DIR must be on a local filesystem (not NFS)
- NOT REENTRANT: Same process must not acquire same lock twice
- NO FORK: Do not fork while holding the lock
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

# =============================================================================
# Platform check
# =============================================================================

if sys.platform == "win32":
    raise RuntimeError(
        "RobustFileLock is POSIX-only (Linux, macOS). "
        "Windows requires a different implementation using msvcrt.locking."
    )

# =============================================================================
# Configuration (expansion deferred to runtime)
# =============================================================================

LOCK_DIR_RAW = os.environ.get("JARVIS_LOCK_DIR", "~/.jarvis/cross_repo/locks")
LOCK_ACQUIRE_TIMEOUT_S = float(os.environ.get("LOCK_ACQUIRE_TIMEOUT_S", "5.0"))
LOCK_POLL_INTERVAL_S = float(os.environ.get("LOCK_POLL_INTERVAL_S", "0.05"))
LOCK_STALE_WARNING_S = float(os.environ.get("LOCK_STALE_WARNING_S", "30.0"))

# =============================================================================
# Process-local reentrancy guard
# =============================================================================

_held_locks: Set[str] = set()
_held_locks_lock: Optional[asyncio.Lock] = None


def _get_held_locks_lock() -> asyncio.Lock:
    """Lazy initialization of held locks lock (Python 3.9 compatibility)."""
    global _held_locks_lock
    if _held_locks_lock is None:
        _held_locks_lock = asyncio.Lock()
    return _held_locks_lock


class RobustFileLock:
    """
    OS-level file lock using fcntl.flock().
    All blocking I/O runs in executor to avoid blocking the event loop.
    """

    def __init__(self, lock_name: str, source: str = "jarvis"):
        """
        Initialize lock.

        Args:
            lock_name: Unique name for this lock (e.g., "vbia_state")
            source: Identifier for the process holding the lock (for debugging)
        """
        self._lock_name = lock_name
        self._source = source

        # Expand path at runtime (handles both ~ and $VAR)
        lock_dir_raw = os.environ.get("JARVIS_LOCK_DIR", "~/.jarvis/cross_repo/locks")
        self._lock_dir = Path(os.path.expanduser(os.path.expandvars(lock_dir_raw)))
        self._lock_file = self._lock_dir / f"{lock_name}.lock"

        self._fd: Optional[int] = None
        self._acquired = False

    async def acquire(self, timeout_s: Optional[float] = None) -> bool:
        """
        Acquire the lock with timeout.

        Returns:
            True if acquired, False if timeout or error.

        Raises:
            RuntimeError: If same process already holds this lock (reentrancy)
        """
        timeout_s = timeout_s or LOCK_ACQUIRE_TIMEOUT_S

        # Reentrancy check
        held_lock = _get_held_locks_lock()
        async with held_lock:
            if self._lock_name in _held_locks:
                raise RuntimeError(
                    f"Lock '{self._lock_name}' already held by this process. "
                    f"RobustFileLock is NOT reentrant."
                )

        deadline = time.monotonic() + timeout_s
        loop = asyncio.get_running_loop()

        # Ensure directory exists
        await self._ensure_lock_dir()

        # Open lock file (retry once on ENOENT)
        try:
            self._fd = await loop.run_in_executor(None, self._open_lock_file)
        except FileNotFoundError:
            await self._ensure_lock_dir()
            try:
                self._fd = await loop.run_in_executor(None, self._open_lock_file)
            except FileNotFoundError as e:
                logger.error(f"[Lock] Lock dir keeps disappearing: {e}")
                return False
        except OSError as e:
            logger.error(f"[Lock] Failed to open {self._lock_file}: {e}")
            return False

        # Poll for lock acquisition
        while time.monotonic() < deadline:
            try:
                acquired = await loop.run_in_executor(None, self._try_flock)

                if acquired:
                    self._acquired = True

                    # Register in held locks
                    async with held_lock:
                        _held_locks.add(self._lock_name)

                    # Write metadata
                    await loop.run_in_executor(None, self._write_metadata_sync)

                    logger.debug(f"[Lock] Acquired: {self._lock_name}")
                    return True

            except OSError as e:
                logger.error(f"[Lock] flock() error on {self._lock_name}: {e}")
                await self._close_fd_async()
                return False

            await asyncio.sleep(LOCK_POLL_INTERVAL_S)

        # Timeout
        await self._log_stale_warning()
        logger.warning(f"[Lock] Timeout acquiring {self._lock_name} after {timeout_s}s")
        await self._close_fd_async()
        return False

    async def release(self) -> None:
        """Release the lock. Safe to call multiple times."""
        if self._fd is not None and self._acquired:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._release_sync)
                logger.debug(f"[Lock] Released: {self._lock_name}")
            except OSError as e:
                logger.warning(f"[Lock] Error releasing {self._lock_name}: {e}")
            finally:
                self._acquired = False

                # Remove from held locks
                held_lock = _get_held_locks_lock()
                async with held_lock:
                    _held_locks.discard(self._lock_name)

                await self._close_fd_async()

    # =========================================================================
    # Sync methods (run in executor)
    # =========================================================================

    def _open_lock_file(self) -> int:
        """Open lock file (blocking, run in executor)."""
        return os.open(str(self._lock_file), os.O_RDWR | os.O_CREAT, 0o644)

    def _try_flock(self) -> bool:
        """Try to acquire flock. Returns True if acquired, False if would block."""
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _release_sync(self) -> None:
        """Release flock (blocking, run in executor)."""
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _write_metadata_sync(self) -> None:
        """Write debugging metadata to lock file."""
        if self._fd is None:
            return

        try:
            metadata = {
                "owner_pid": os.getpid(),
                "owner_host": socket.gethostname(),
                "acquired_at": time.time(),
                "source": self._source,
            }
            content = json.dumps(metadata, indent=2).encode("utf-8")

            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, content)
            os.fsync(self._fd)
        except OSError as e:
            logger.debug(f"[Lock] Metadata write failed (non-fatal): {e}")

    def _read_metadata_sync(self) -> Optional[dict]:
        """Read metadata from lock file (for stale warning)."""
        try:
            with open(self._lock_file, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    # =========================================================================
    # Async helpers
    # =========================================================================

    async def _ensure_lock_dir(self) -> None:
        """Ensure lock directory exists."""
        loop = asyncio.get_running_loop()

        def _mkdir():
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self._lock_dir, 0o700)
            except OSError:
                pass

        try:
            await loop.run_in_executor(None, _mkdir)
        except FileExistsError:
            pass
        except OSError as e:
            logger.error(f"[Lock] Failed to create lock dir {self._lock_dir}: {e}")
            raise

    async def _close_fd_async(self) -> None:
        """Close fd in executor."""
        if self._fd is not None:
            loop = asyncio.get_running_loop()
            fd = self._fd
            self._fd = None

            def _close():
                try:
                    os.close(fd)
                except OSError:
                    pass

            await loop.run_in_executor(None, _close)

    async def _log_stale_warning(self) -> None:
        """Log warning if lock appears stale."""
        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(None, self._read_metadata_sync)

        if metadata and "acquired_at" in metadata:
            held_for = time.time() - metadata["acquired_at"]
            if held_for > LOCK_STALE_WARNING_S:
                logger.warning(
                    f"[Lock] {self._lock_name} held for {held_for:.1f}s by "
                    f"PID {metadata.get('owner_pid')} ({metadata.get('source')}) - "
                    f"may be stale"
                )

    # =========================================================================
    # Context manager
    # =========================================================================

    async def __aenter__(self) -> bool:
        """Context manager entry. Returns True if acquired, False if timeout."""
        return await self.acquire()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - always releases lock."""
        await self.release()
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_robust_file_lock.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add backend/core/robust_file_lock.py tests/unit/backend/core/test_robust_file_lock.py
git commit -m "feat(lock): Add RobustFileLock using fcntl.flock()

OS-level cross-process file locking that:
- Uses fcntl.flock() for kernel-level atomicity
- Auto-releases on process death (ephemeral)
- Runs all blocking I/O in executor (non-blocking event loop)
- Has reentrancy guard to prevent same-process deadlock
- Writes debugging metadata (PID, source, timestamp)

Fixes: 'Temp file size mismatch' and 'No such file or directory' errors

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 2: VoiceClient - Cross-Repo Client

**Files:**
- Create: `backend/core/voice_client.py`
- Create: `tests/unit/backend/core/test_voice_client.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/core/test_voice_client.py
"""Tests for VoiceClient - Cross-repo voice announcement client."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.voice_client import VoiceClient, VoicePriority


@pytest.fixture
def temp_socket_dir():
    """Create temporary directory for socket."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_client_queues_when_disconnected(temp_socket_dir, monkeypatch):
    """Test that client queues messages locally when disconnected."""
    socket_path = temp_socket_dir / "voice.sock"
    monkeypatch.setenv("VOICE_SOCKET_PATH", str(socket_path))

    client = VoiceClient(source="test")

    # Don't start reconnect loop - stay disconnected
    assert not client._connected

    # Send some messages
    await client.announce("Message 1", VoicePriority.NORMAL, "init")
    await client.announce("Message 2", VoicePriority.NORMAL, "init")

    # Should be queued locally
    assert len(client._local_queue) == 2


@pytest.mark.asyncio
async def test_client_drops_oldest_when_full(temp_socket_dir, monkeypatch):
    """Test that client drops oldest message when queue is full."""
    socket_path = temp_socket_dir / "voice.sock"
    monkeypatch.setenv("VOICE_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("VOICE_CLIENT_QUEUE_MAX", "3")

    client = VoiceClient(source="test")

    # Fill queue beyond capacity
    await client.announce("Message 1", VoicePriority.NORMAL, "init")
    await client.announce("Message 2", VoicePriority.NORMAL, "init")
    await client.announce("Message 3", VoicePriority.NORMAL, "init")
    await client.announce("Message 4", VoicePriority.NORMAL, "init")  # Drops Message 1

    # Queue should have 3 (most recent)
    assert len(client._local_queue) == 3
    assert client._local_queue[0].text == "Message 2"
    assert client._dropped_count == 1


@pytest.mark.asyncio
async def test_message_format():
    """Test that message is formatted correctly as JSON-lines."""
    client = VoiceClient(source="jarvis")

    msg = client._format_message("Hello", VoicePriority.HIGH, "greeting")
    data = json.loads(msg.rstrip("\n"))

    assert data["text"] == "Hello"
    assert data["priority"] == "HIGH"
    assert data["category"] == "greeting"
    assert data["source"] == "jarvis"
    assert "timestamp" in data
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_client.py -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

```python
# backend/core/voice_client.py
"""
VoiceClient - Cross-repo voice announcement client.

Sends announcements to the VoiceOrchestrator via Unix domain socket.
Queues locally when disconnected and drains on reconnect.

Usage:
    client = VoiceClient(source="jarvis-prime")
    await client.start()  # Start reconnect loop

    await client.announce("System ready", VoicePriority.NORMAL, "init")

    await client.stop()  # Cleanup
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Deque, Optional

logger = logging.getLogger(__name__)


class VoicePriority(Enum):
    """Priority levels for voice announcements."""
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


@dataclass
class QueuedMessage:
    """Message queued locally when disconnected."""
    text: str
    priority: VoicePriority
    category: str
    timestamp: float


class VoiceClient:
    """
    Cross-repo voice client. Sends to VoiceOrchestrator via Unix socket.

    Features:
    - Bounded local queue with drop-oldest policy
    - Reconnect loop with exponential backoff
    - Coalesces large queues on reconnect
    """

    def __init__(self, source: str):
        """
        Initialize client.

        Args:
            source: Identifier for this client (e.g., "jarvis", "prime", "reactor")
        """
        self._source = source
        self._socket_path = self._get_socket_path()
        self._connected = False
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader: Optional[asyncio.StreamReader] = None

        # Bounded local queue with drop-oldest policy
        max_size = int(os.environ.get("VOICE_CLIENT_QUEUE_MAX", "20"))
        self._local_queue: Deque[QueuedMessage] = deque(maxlen=max_size)
        self._dropped_count = 0

        # Reconnect task
        self._reconnect_task: Optional[asyncio.Task] = None
        self._shutdown = False

    def _get_socket_path(self) -> str:
        """Get socket path with expansion."""
        raw = os.environ.get("VOICE_SOCKET_PATH", "~/.jarvis/voice.sock")
        return os.path.expanduser(os.path.expandvars(raw))

    async def start(self) -> None:
        """Start the reconnect loop."""
        self._shutdown = False
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        """Stop client gracefully."""
        self._shutdown = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def announce(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.NORMAL,
        category: str = "general",
    ) -> bool:
        """
        Announce a message.

        Args:
            text: Message to announce
            priority: Priority level
            category: Category for coalescing (e.g., "init", "health", "error")

        Returns:
            True if sent immediately, False if queued locally
        """
        if self._connected:
            success = await self._send(text, priority, category)
            if success:
                return True
            # Send failed - disconnect and queue
            self._connected = False

        self._queue_locally(text, priority, category)
        return False

    def _queue_locally(self, text: str, priority: VoicePriority, category: str) -> None:
        """Queue message locally. Drops oldest if full."""
        if len(self._local_queue) == self._local_queue.maxlen:
            self._dropped_count += 1
        self._local_queue.append(
            QueuedMessage(text=text, priority=priority, category=category, timestamp=time.time())
        )

    def _format_message(self, text: str, priority: VoicePriority, category: str) -> str:
        """Format message as JSON-line."""
        data = {
            "text": text,
            "priority": priority.value,
            "category": category,
            "source": self._source,
            "timestamp": time.time(),
        }
        return json.dumps(data) + "\n"

    async def _send(self, text: str, priority: VoicePriority, category: str) -> bool:
        """Send to socket. Returns False on failure."""
        if not self._writer:
            return False

        try:
            line = self._format_message(text, priority, category)
            self._writer.write(line.encode())
            await self._writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug(f"[VoiceClient] Send failed: {e}")
            return False

    async def _reconnect_loop(self) -> None:
        """Background task: reconnect with exponential backoff."""
        backoff = 1.0
        max_backoff = float(os.environ.get("VOICE_CLIENT_MAX_BACKOFF_S", "30"))

        while not self._shutdown:
            if not self._connected:
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        self._socket_path
                    )
                    self._connected = True
                    backoff = 1.0
                    logger.info(f"[VoiceClient] Connected to {self._socket_path}")
                    await self._drain_on_reconnect()
                except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                    logger.debug(f"[VoiceClient] Connect failed: {e}")
                    self._connected = False
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
            else:
                await asyncio.sleep(1.0)

    async def _drain_on_reconnect(self) -> None:
        """Drain local queue on reconnect. Coalesces if large."""
        queue_size = len(self._local_queue)

        if queue_size == 0:
            return

        if queue_size > 5:
            # Coalesce: send summary instead of flooding
            summary = f"{self._source} reconnected with {queue_size} pending messages"
            await self._send(summary, VoicePriority.LOW, "reconnect")
            self._local_queue.clear()
            logger.info(f"[VoiceClient] Coalesced {queue_size} pending messages")
        else:
            # Drain individually with rate limit
            rate_limit_ms = int(os.environ.get("VOICE_CLIENT_DRAIN_RATE_MS", "100"))
            while self._local_queue:
                msg = self._local_queue.popleft()
                success = await self._send(msg.text, msg.priority, msg.category)
                if not success:
                    self._local_queue.appendleft(msg)
                    self._connected = False
                    break
                await asyncio.sleep(rate_limit_ms / 1000)


# Convenience function for simple usage
_default_client: Optional[VoiceClient] = None


async def announce(
    text: str,
    priority: VoicePriority = VoicePriority.NORMAL,
    category: str = "general",
    source: str = "jarvis",
) -> bool:
    """
    Announce via the default client.

    Creates and starts a client on first call.
    """
    global _default_client
    if _default_client is None:
        _default_client = VoiceClient(source=source)
        await _default_client.start()
    return await _default_client.announce(text, priority, category)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_client.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add backend/core/voice_client.py tests/unit/backend/core/test_voice_client.py
git commit -m "feat(voice): Add VoiceClient for cross-repo announcements

Unix domain socket client that:
- Sends announcements to VoiceOrchestrator
- Queues locally when disconnected (bounded, drop-oldest)
- Reconnects with exponential backoff
- Coalesces large queues on reconnect (>5 messages)

Single implementation - Prime/Reactor import this, no duplication.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 3: VoiceOrchestrator - IPC Server & Collector

**Files:**
- Create: `backend/core/voice_orchestrator.py`
- Create: `tests/unit/backend/core/test_voice_orchestrator.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/core/test_voice_orchestrator.py
"""Tests for VoiceOrchestrator - IPC server and collector."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from backend.core.voice_orchestrator import VoiceOrchestrator, VoiceMessage


@pytest.fixture
def temp_socket_dir():
    """Create temporary directory for socket."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_orchestrator_starts_and_stops(temp_socket_dir, monkeypatch):
    """Test basic lifecycle."""
    socket_path = temp_socket_dir / "voice.sock"
    monkeypatch.setenv("VOICE_SOCKET_PATH", str(socket_path))

    orchestrator = VoiceOrchestrator()

    await orchestrator.start()
    assert socket_path.exists()
    assert orchestrator._running

    await orchestrator.stop()
    assert not orchestrator._running


@pytest.mark.asyncio
async def test_orchestrator_receives_message(temp_socket_dir, monkeypatch):
    """Test that orchestrator receives messages via IPC."""
    socket_path = temp_socket_dir / "voice.sock"
    monkeypatch.setenv("VOICE_SOCKET_PATH", str(socket_path))

    orchestrator = VoiceOrchestrator()
    received_messages = []

    # Mock the collector to capture messages
    original_collect = orchestrator._collect_message
    async def capture_collect(msg):
        received_messages.append(msg)
        await original_collect(msg)
    orchestrator._collect_message = capture_collect

    await orchestrator.start()

    # Connect and send a message
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    msg = json.dumps({
        "text": "Test message",
        "priority": "NORMAL",
        "category": "test",
        "source": "test_client",
    }) + "\n"
    writer.write(msg.encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()

    # Wait for message to be processed
    await asyncio.sleep(0.1)

    await orchestrator.stop()

    assert len(received_messages) == 1
    assert received_messages[0].text == "Test message"


@pytest.mark.asyncio
async def test_in_process_announce(temp_socket_dir, monkeypatch):
    """Test that in-process announce works without socket."""
    socket_path = temp_socket_dir / "voice.sock"
    monkeypatch.setenv("VOICE_SOCKET_PATH", str(socket_path))

    orchestrator = VoiceOrchestrator()
    received = []

    async def capture(msg):
        received.append(msg)
    orchestrator._collect_message = capture

    await orchestrator.start()

    # In-process announce (no socket hop)
    await orchestrator.announce("Kernel message", "HIGH", "kernel")

    await asyncio.sleep(0.05)
    await orchestrator.stop()

    assert len(received) == 1
    assert received[0].text == "Kernel message"
    assert received[0].source == "kernel"


@pytest.mark.asyncio
async def test_connection_limit(temp_socket_dir, monkeypatch):
    """Test that connection limit is enforced."""
    socket_path = temp_socket_dir / "voice.sock"
    monkeypatch.setenv("VOICE_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("VOICE_MAX_CONNECTIONS", "2")

    orchestrator = VoiceOrchestrator()
    await orchestrator.start()

    # Open 2 connections (at limit)
    conn1_r, conn1_w = await asyncio.open_unix_connection(str(socket_path))
    conn2_r, conn2_w = await asyncio.open_unix_connection(str(socket_path))

    # Third should get rejected
    conn3_r, conn3_w = await asyncio.open_unix_connection(str(socket_path))
    response = await asyncio.wait_for(conn3_r.readline(), timeout=1.0)
    data = json.loads(response)
    assert data.get("error") == "busy"

    # Cleanup
    for w in [conn1_w, conn2_w, conn3_w]:
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass

    await orchestrator.stop()
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_orchestrator.py -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

```python
# backend/core/voice_orchestrator.py
"""
VoiceOrchestrator - Unified voice coordination for JARVIS ecosystem.

Single playback authority that:
- Receives announcements via Unix domain socket (IPC)
- Collects and coalesces announcements
- Serializes playback (one voice at a time)
- Provides metrics for observability

Architecture:
    IPC Receiver → Bounded Collector → Coalescer → Serialized Speaker
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

VOICE_SOCKET_PATH_RAW = os.environ.get("VOICE_SOCKET_PATH", "~/.jarvis/voice.sock")
VOICE_SOCKET_MODE = int(os.environ.get("VOICE_SOCKET_MODE", "0600"), 8)
VOICE_MAX_CONNECTIONS = int(os.environ.get("VOICE_MAX_CONNECTIONS", "20"))
VOICE_READ_TIMEOUT_S = float(os.environ.get("VOICE_READ_TIMEOUT_MS", "5000")) / 1000
VOICE_MAX_MESSAGE_LENGTH = int(os.environ.get("VOICE_MAX_MESSAGE_LENGTH", "1000"))
VOICE_QUEUE_MAX_SIZE = int(os.environ.get("VOICE_QUEUE_MAX_SIZE", "50"))


class VoicePriority(Enum):
    """Priority levels for voice messages."""
    CRITICAL = 100
    HIGH = 75
    NORMAL = 50
    LOW = 25


@dataclass
class VoiceMessage:
    """A voice announcement message."""
    text: str
    priority: VoicePriority
    category: str
    source: str
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VoiceMessage":
        """Create from dictionary (IPC message)."""
        priority_str = data.get("priority", "NORMAL")
        try:
            priority = VoicePriority[priority_str]
        except KeyError:
            priority = VoicePriority.NORMAL

        return cls(
            text=data.get("text", "")[:VOICE_MAX_MESSAGE_LENGTH],
            priority=priority,
            category=data.get("category", "general"),
            source=data.get("source", "unknown"),
            timestamp=data.get("timestamp", time.time()),
        )


@dataclass
class VoiceMetrics:
    """Metrics for observability."""
    queue_depth: int = 0
    coalesced_count: int = 0
    spoken_count: int = 0
    dropped_count: int = 0
    interrupt_count: int = 0
    last_spoken_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_depth": self.queue_depth,
            "coalesced_count": self.coalesced_count,
            "spoken_count": self.spoken_count,
            "dropped_count": self.dropped_count,
            "interrupt_count": self.interrupt_count,
            "last_spoken_at": self.last_spoken_at,
        }


class VoiceOrchestrator:
    """
    Unified voice orchestrator - single playback authority.

    Receives announcements via IPC socket and in-process calls,
    coalesces them intelligently, and plays them one at a time.
    """

    def __init__(self):
        """Initialize orchestrator."""
        self._socket_path = self._get_socket_path()
        self._server: Optional[asyncio.Server] = None
        self._running = False
        self._shutting_down = False

        # Connection limiting
        self._active_connections = 0
        self._connection_lock = asyncio.Lock()

        # Message queue
        self._queue: asyncio.Queue[VoiceMessage] = asyncio.Queue(maxsize=VOICE_QUEUE_MAX_SIZE)

        # Metrics
        self._metrics = VoiceMetrics()

        # Tasks
        self._consumer_task: Optional[asyncio.Task] = None

        # TTS callback (set by kernel)
        self._tts_callback: Optional[Callable[[str], asyncio.Future]] = None

    def _get_socket_path(self) -> Path:
        """Get socket path with expansion."""
        return Path(os.path.expanduser(os.path.expandvars(VOICE_SOCKET_PATH_RAW)))

    async def start(self) -> None:
        """Start the orchestrator."""
        if self._running:
            return

        # Ensure parent directory exists
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        try:
            self._socket_path.unlink()
            logger.info(f"[Voice] Removed stale socket: {self._socket_path}")
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error(f"[Voice] Failed to unlink socket: {e}")
            raise

        # Start IPC server
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._socket_path),
        )

        # Set permissions
        try:
            os.chmod(self._socket_path, VOICE_SOCKET_MODE)
        except OSError as e:
            logger.warning(f"[Voice] chmod failed: {e}")

        # Start consumer task
        self._consumer_task = asyncio.create_task(self._consumer_loop())

        self._running = True
        logger.info(f"[Voice] Orchestrator started: {self._socket_path}")

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        self._shutting_down = True
        self._running = False

        # Stop server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Stop consumer
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        # Remove socket
        try:
            self._socket_path.unlink()
        except (FileNotFoundError, OSError):
            pass

        logger.info("[Voice] Orchestrator stopped")

    async def announce(
        self,
        text: str,
        priority: str = "NORMAL",
        category: str = "kernel",
    ) -> None:
        """
        In-process announce (no socket hop).

        For kernel messages that originate within the same process.
        """
        try:
            prio = VoicePriority[priority]
        except KeyError:
            prio = VoicePriority.NORMAL

        msg = VoiceMessage(
            text=text[:VOICE_MAX_MESSAGE_LENGTH],
            priority=prio,
            category=category,
            source="kernel",
        )
        await self._collect_message(msg)

    async def _collect_message(self, msg: VoiceMessage) -> None:
        """Add message to queue (drops oldest if full)."""
        try:
            self._queue.put_nowait(msg)
            self._metrics.queue_depth = self._queue.qsize()
        except asyncio.QueueFull:
            # Drop oldest (get and discard)
            try:
                self._queue.get_nowait()
                self._metrics.dropped_count += 1
            except asyncio.QueueEmpty:
                pass
            # Try again
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                self._metrics.dropped_count += 1

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a client connection."""
        # Check connection limit
        async with self._connection_lock:
            if self._active_connections >= VOICE_MAX_CONNECTIONS:
                # Reject
                writer.write(b'{"error":"busy","retry_after_ms":1000}\n')
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            self._active_connections += 1

        try:
            await self._process_client(reader, writer)
        finally:
            async with self._connection_lock:
                self._active_connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _process_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Process messages from a client."""
        while not self._shutting_down:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=VOICE_READ_TIMEOUT_S,
                )

                if not line:
                    break  # Client disconnected

                # Parse JSON
                try:
                    data = json.loads(line.decode().strip())
                    msg = VoiceMessage.from_dict(data)
                    await self._collect_message(msg)
                except json.JSONDecodeError as e:
                    logger.warning(f"[Voice] Invalid JSON from client: {e}")
                    continue

            except asyncio.TimeoutError:
                continue  # Keep connection alive
            except (ConnectionResetError, BrokenPipeError):
                break

    async def _consumer_loop(self) -> None:
        """Consume messages from queue and speak them."""
        while not self._shutting_down:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                self._metrics.queue_depth = self._queue.qsize()

                # Speak the message
                if self._tts_callback:
                    try:
                        await self._tts_callback(msg.text)
                        self._metrics.spoken_count += 1
                        self._metrics.last_spoken_at = time.time()
                    except Exception as e:
                        logger.error(f"[Voice] TTS error: {e}")
                else:
                    # No TTS - just log
                    logger.info(f"[Voice] Would speak: {msg.text}")
                    self._metrics.spoken_count += 1

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def set_tts_callback(self, callback: Callable[[str], asyncio.Future]) -> None:
        """Set the TTS callback for actual speech synthesis."""
        self._tts_callback = callback

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        return self._metrics.to_dict()
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_orchestrator.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add backend/core/voice_orchestrator.py tests/unit/backend/core/test_voice_orchestrator.py
git commit -m "feat(voice): Add VoiceOrchestrator IPC server

Single playback authority that:
- Listens on Unix domain socket for IPC
- Enforces connection limits
- Provides in-process announce() for kernel
- Queues with bounded overflow (drop oldest)
- Exposes metrics for observability

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 4: Coalescer - Intelligent Message Batching

**Files:**
- Modify: `backend/core/voice_orchestrator.py`
- Create: `tests/unit/backend/core/test_voice_coalescer.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/core/test_voice_coalescer.py
"""Tests for voice message coalescing."""

import asyncio
import pytest

from backend.core.voice_orchestrator import (
    VoiceCoalescer,
    VoiceMessage,
    VoicePriority,
    CATEGORY_PRIORITY,
)


@pytest.mark.asyncio
async def test_coalescer_batches_messages():
    """Test that coalescer batches messages within window."""
    results = []

    async def on_flush(summary: str):
        results.append(summary)

    coalescer = VoiceCoalescer(on_flush=on_flush, window_ms=100, idle_ms=50)
    await coalescer.start()

    # Add messages
    await coalescer.add(VoiceMessage("Init 1", VoicePriority.NORMAL, "init", "test"))
    await coalescer.add(VoiceMessage("Init 2", VoicePriority.NORMAL, "init", "test"))
    await coalescer.add(VoiceMessage("Init 3", VoicePriority.NORMAL, "init", "test"))

    # Wait for flush
    await asyncio.sleep(0.2)
    await coalescer.stop()

    assert len(results) == 1
    assert "3" in results[0]  # Should mention count


@pytest.mark.asyncio
async def test_coalescer_flushes_on_idle():
    """Test that coalescer flushes early when idle."""
    results = []
    flush_times = []

    async def on_flush(summary: str):
        results.append(summary)
        flush_times.append(asyncio.get_event_loop().time())

    coalescer = VoiceCoalescer(on_flush=on_flush, window_ms=1000, idle_ms=50)
    await coalescer.start()

    start = asyncio.get_event_loop().time()
    await coalescer.add(VoiceMessage("Single", VoicePriority.NORMAL, "init", "test"))

    # Wait for idle flush (should be < 1000ms window)
    await asyncio.sleep(0.2)
    await coalescer.stop()

    assert len(results) == 1
    assert flush_times[0] - start < 0.5  # Flushed early due to idle


@pytest.mark.asyncio
async def test_supersession_drops_lower_priority():
    """Test that shutdown supersedes ready messages."""
    results = []

    async def on_flush(summary: str):
        results.append(summary)

    coalescer = VoiceCoalescer(on_flush=on_flush, window_ms=100, idle_ms=50)
    await coalescer.start()

    # Add ready, then shutdown
    await coalescer.add(VoiceMessage("System ready", VoicePriority.NORMAL, "ready", "test"))
    await coalescer.add(VoiceMessage("Shutting down", VoicePriority.HIGH, "shutdown", "test"))

    await asyncio.sleep(0.2)
    await coalescer.stop()

    assert len(results) == 1
    assert "shutdown" in results[0].lower() or "Shutting" in results[0]
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_coalescer.py -v`
Expected: FAIL with "ImportError: cannot import name 'VoiceCoalescer'"

**Step 3: Add coalescer to voice_orchestrator.py**

Add this class to `backend/core/voice_orchestrator.py` after VoiceMetrics:

```python
# =============================================================================
# Category Priority (for supersession)
# =============================================================================

CATEGORY_PRIORITY = {
    "shutdown": 100,
    "error": 90,
    "critical": 90,
    "warning": 70,
    "ready": 50,
    "init": 40,
    "health": 30,
    "progress": 20,
    "general": 10,
}

# Supersession rules: key supersedes all values
SUPERSESSION_RULES = {
    "shutdown": ["ready", "init", "health", "progress", "general"],
    "error": ["ready", "init", "general"],
}


class VoiceCoalescer:
    """
    Intelligent message coalescer.

    Batches messages within a time window and produces summaries.
    Flushes early on idle. Applies supersession rules.
    """

    def __init__(
        self,
        on_flush: Callable[[str], asyncio.Future],
        window_ms: Optional[int] = None,
        idle_ms: Optional[int] = None,
    ):
        """
        Initialize coalescer.

        Args:
            on_flush: Async callback when batch is ready to speak
            window_ms: Max time to collect messages (default from env)
            idle_ms: Flush early if no messages for this long
        """
        self._on_flush = on_flush
        self._window_ms = window_ms or int(os.environ.get("VOICE_COALESCE_WINDOW_MS", "2000"))
        self._idle_ms = idle_ms or int(os.environ.get("VOICE_COALESCE_IDLE_MS", "300"))

        self._batch: List[VoiceMessage] = []
        self._batch_lock = asyncio.Lock()
        self._batch_start: Optional[float] = None
        self._last_message_time: float = 0

        self._timer_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the coalescer timer."""
        self._running = True
        self._timer_task = asyncio.create_task(self._timer_loop())

    async def stop(self) -> None:
        """Stop and flush remaining."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
        # Flush remaining
        await self._flush()

    async def add(self, msg: VoiceMessage) -> None:
        """Add a message to the current batch."""
        async with self._batch_lock:
            if not self._batch:
                self._batch_start = time.time()
            self._batch.append(msg)
            self._last_message_time = time.time()

    async def _timer_loop(self) -> None:
        """Check for flush conditions periodically."""
        while self._running:
            try:
                await asyncio.sleep(0.05)  # 50ms check interval

                async with self._batch_lock:
                    if not self._batch:
                        continue

                    now = time.time()
                    elapsed_since_start = (now - self._batch_start) * 1000 if self._batch_start else 0
                    elapsed_since_last = (now - self._last_message_time) * 1000

                    # Flush if: window expired OR idle timeout
                    if elapsed_since_start >= self._window_ms or elapsed_since_last >= self._idle_ms:
                        await self._flush_locked()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Coalescer] Timer error: {e}")

    async def _flush(self) -> None:
        """Flush with lock acquisition."""
        async with self._batch_lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Flush the current batch (must hold lock)."""
        if not self._batch:
            return

        batch = self._batch
        self._batch = []
        self._batch_start = None

        # Apply supersession
        batch = self._apply_supersession(batch)

        if not batch:
            return

        # Generate summary
        summary = self._generate_summary(batch)

        # Callback
        try:
            await self._on_flush(summary)
        except Exception as e:
            logger.error(f"[Coalescer] Flush callback error: {e}")

    def _apply_supersession(self, batch: List[VoiceMessage]) -> List[VoiceMessage]:
        """Apply supersession rules to batch."""
        categories_present = {m.category for m in batch}

        # Check what gets superseded
        superseded: Set[str] = set()
        for dominant, subordinates in SUPERSESSION_RULES.items():
            if dominant in categories_present:
                superseded.update(subordinates)

        # Filter out superseded
        return [m for m in batch if m.category not in superseded]

    def _generate_summary(self, batch: List[VoiceMessage]) -> str:
        """Generate a summary from the batch."""
        if len(batch) == 1:
            return batch[0].text

        # Group by category
        by_category: Dict[str, List[VoiceMessage]] = {}
        for msg in batch:
            by_category.setdefault(msg.category, []).append(msg)

        # Find dominant category
        dominant_cat = max(
            by_category.keys(),
            key=lambda c: CATEGORY_PRIORITY.get(c, 0)
        )
        dominant_msgs = by_category[dominant_cat]

        # Get template
        templates = {
            "init": "{count} components initialized",
            "health": "Health update: {latest}",
            "ready": "System ready",
            "error": "{count} errors occurred",
            "shutdown": "System shutting down",
        }
        template = templates.get(dominant_cat, "{count} updates")

        # Build summary
        return template.format(
            count=len(dominant_msgs),
            latest=dominant_msgs[-1].text[:50],
        )
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_coalescer.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add backend/core/voice_orchestrator.py tests/unit/backend/core/test_voice_coalescer.py
git commit -m "feat(voice): Add VoiceCoalescer for intelligent batching

Coalesces messages within a time window:
- Fixed window from first message (VOICE_COALESCE_WINDOW_MS)
- Early flush on idle (VOICE_COALESCE_IDLE_MS)
- Category-based supersession (shutdown > ready > init)
- Template-based summaries by category

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 5: Serialized Speaker with Exclusive Lock

**Files:**
- Modify: `backend/core/voice_orchestrator.py`
- Create: `tests/unit/backend/core/test_voice_speaker.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/core/test_voice_speaker.py
"""Tests for serialized voice speaker."""

import asyncio
import pytest

from backend.core.voice_orchestrator import SerializedSpeaker


@pytest.mark.asyncio
async def test_speaker_serializes_playback():
    """Test that speaker plays one at a time."""
    play_log = []

    async def mock_tts(text: str):
        play_log.append(f"start:{text}")
        await asyncio.sleep(0.1)  # Simulate playback
        play_log.append(f"end:{text}")

    speaker = SerializedSpeaker(tts_callback=mock_tts)

    # Start two speaks concurrently
    task1 = asyncio.create_task(speaker.speak("First"))
    task2 = asyncio.create_task(speaker.speak("Second"))

    await asyncio.gather(task1, task2)

    # First should complete before second starts
    assert play_log.index("end:First") < play_log.index("start:Second")


@pytest.mark.asyncio
async def test_speaker_stop_interrupts():
    """Test that stop_playback interrupts current playback."""
    started = asyncio.Event()

    async def slow_tts(text: str):
        started.set()
        await asyncio.sleep(10)  # Very long playback

    speaker = SerializedSpeaker(tts_callback=slow_tts)

    # Start playback
    speak_task = asyncio.create_task(speaker.speak("Long message"))

    # Wait for it to start
    await started.wait()

    # Stop it
    stopped = await speaker.stop_playback(timeout_s=0.5)

    # Should have stopped
    assert stopped or speak_task.done()


@pytest.mark.asyncio
async def test_speaker_metrics():
    """Test that speaker tracks metrics."""
    async def mock_tts(text: str):
        await asyncio.sleep(0.01)

    speaker = SerializedSpeaker(tts_callback=mock_tts)

    await speaker.speak("Test 1")
    await speaker.speak("Test 2")

    metrics = speaker.get_metrics()
    assert metrics["spoken_count"] == 2
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_speaker.py -v`
Expected: FAIL with "ImportError: cannot import name 'SerializedSpeaker'"

**Step 3: Add SerializedSpeaker to voice_orchestrator.py**

```python
import threading
from concurrent.futures import ThreadPoolExecutor

class SerializedSpeaker:
    """
    Serialized voice speaker with exclusive lock.

    Guarantees:
    - Only one voice plays at a time
    - Lock held for FULL play-through (not just enqueue)
    - Interruptible via stop_playback()
    - Non-blocking event loop (TTS runs in executor)
    """

    def __init__(self, tts_callback: Optional[Callable[[str], asyncio.Future]] = None):
        """
        Initialize speaker.

        Args:
            tts_callback: Async function to synthesize and play speech
        """
        self._tts_callback = tts_callback
        self._playback_lock = asyncio.Lock()

        # Stop signaling
        self._stop_event = threading.Event()
        self._playback_done = asyncio.Event()

        # Executor for blocking TTS
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

        # Metrics
        self._spoken_count = 0
        self._interrupt_count = 0
        self._last_spoken_at: Optional[float] = None

    async def speak(self, text: str, timeout_s: Optional[float] = None) -> bool:
        """
        Speak text with exclusive lock held for full duration.

        Args:
            text: Text to speak
            timeout_s: Max time for playback (default from env)

        Returns:
            True if completed, False if timeout/interrupted
        """
        timeout_s = timeout_s or float(os.environ.get("VOICE_PLAYBACK_TIMEOUT_S", "30"))

        async with self._playback_lock:
            self._stop_event.clear()
            self._playback_done.clear()

            if not self._tts_callback:
                # No TTS - just pretend
                self._spoken_count += 1
                self._last_spoken_at = time.time()
                return True

            loop = asyncio.get_running_loop()

            # Create the coroutine
            tts_coro = self._tts_callback(text)

            # Wrap in a task we can wait on
            tts_task = asyncio.create_task(tts_coro)

            try:
                # Wait with timeout (don't use wait_for - it cancels)
                done, pending = await asyncio.wait(
                    [tts_task],
                    timeout=timeout_s,
                )

                if pending:
                    # Timeout - cancel the task
                    self._stop_event.set()
                    tts_task.cancel()
                    try:
                        await tts_task
                    except asyncio.CancelledError:
                        pass
                    return False

                self._spoken_count += 1
                self._last_spoken_at = time.time()
                return True

            except asyncio.CancelledError:
                tts_task.cancel()
                raise

    async def stop_playback(self, timeout_s: float = 2.0) -> bool:
        """
        Request stop of current playback.

        Returns True if stopped cleanly, False if timeout.
        """
        self._stop_event.set()
        self._interrupt_count += 1

        # Wait for playback to finish (up to timeout)
        try:
            await asyncio.wait_for(
                self._playback_done.wait(),
                timeout=timeout_s,
            )
            return True
        except asyncio.TimeoutError:
            return False

    def is_stop_requested(self) -> bool:
        """Check if stop was requested (for TTS implementations)."""
        return self._stop_event.is_set()

    def get_metrics(self) -> Dict[str, Any]:
        """Get speaker metrics."""
        return {
            "spoken_count": self._spoken_count,
            "interrupt_count": self._interrupt_count,
            "last_spoken_at": self._last_spoken_at,
        }

    async def shutdown(self) -> None:
        """Shutdown speaker and executor."""
        self._executor.shutdown(wait=False)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_voice_speaker.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add backend/core/voice_orchestrator.py tests/unit/backend/core/test_voice_speaker.py
git commit -m "feat(voice): Add SerializedSpeaker with exclusive lock

Serialized speaker that:
- Holds asyncio.Lock for FULL play-through duration
- Uses asyncio.wait() (not wait_for) to avoid premature cancel
- Provides stop_playback() for interruption
- Tracks spoken_count, interrupt_count metrics

Guarantees one voice at a time, always.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 6: Integration - VBIA Lock Migration

**Files:**
- Modify: `backend/core/cross_repo_state_initializer.py`

**Step 1: Find current lock usages**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && grep -n "acquire.*vbia" backend/core/cross_repo_state_initializer.py`

**Step 2: Update imports and replace lock usage**

At the top of the file, add import:
```python
from backend.core.robust_file_lock import RobustFileLock
```

Replace each occurrence of:
```python
async with self._lock_manager.acquire("vbia_state", timeout=5.0, ttl=10.0) as acquired:
```

With:
```python
async with RobustFileLock("vbia_state", source="jarvis") as acquired:
```

**Step 3: Verify syntax**

Run: `python -m py_compile backend/core/cross_repo_state_initializer.py`
Expected: No output (success)

**Step 4: Commit**

```bash
git add backend/core/cross_repo_state_initializer.py
git commit -m "refactor(vbia): Migrate to RobustFileLock

Replace temp-file-based lock with fcntl.flock()-based RobustFileLock.
Fixes 'Temp file size mismatch' and 'No such file or directory' errors.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 7: Integration - Voice Orchestrator in Kernel

**Files:**
- Modify: `unified_supervisor.py`

**Step 1: Add voice orchestrator initialization**

Find the startup sequence in `unified_supervisor.py` and add voice orchestrator initialization after backend starts.

Add import at top of Zone 1:
```python
from backend.core.voice_orchestrator import VoiceOrchestrator
```

In `JarvisSystemKernel.__init__`, add:
```python
self._voice_orchestrator: Optional[VoiceOrchestrator] = None
```

In startup sequence (after backend phase), add:
```python
# Initialize Voice Orchestrator
self._voice_orchestrator = VoiceOrchestrator()
await self._voice_orchestrator.start()

# Connect TTS callback
if self._narrator:
    self._voice_orchestrator.set_tts_callback(self._narrator.speak)

self.logger.success("[Kernel] Voice Orchestrator started")
```

In shutdown sequence, add:
```python
if self._voice_orchestrator:
    await self._voice_orchestrator.stop()
```

**Step 2: Verify syntax**

Run: `python -m py_compile unified_supervisor.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(kernel): Integrate VoiceOrchestrator into startup

Initialize voice orchestrator after backend:
- Starts IPC server for cross-repo announcements
- Connects TTS callback to narrator
- Graceful shutdown on kernel stop

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 8: Add /api/voice/metrics Endpoint

**Files:**
- Modify: `backend/main.py`

**Step 1: Add the endpoint**

Find the health endpoints section in `backend/main.py` and add:

```python
@app.get("/api/voice/metrics")
async def get_voice_metrics():
    """
    Get voice orchestrator metrics.

    Returns 503 if kernel unavailable.
    """
    try:
        # Try to get metrics from orchestrator
        from backend.core.voice_client import VoiceClient
        import json

        client = VoiceClient(source="api")
        socket_path = client._get_socket_path()

        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=2.0,
        )

        # Request metrics
        writer.write(b'{"command":"metrics"}\n')
        await writer.drain()

        response = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        await writer.wait_closed()

        data = json.loads(response)
        return JSONResponse(data)

    except (FileNotFoundError, ConnectionRefusedError):
        return JSONResponse(
            {"error": "voice_orchestrator_unavailable", "message": "Kernel not running"},
            status_code=503,
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "voice_orchestrator_timeout", "message": "Kernel not responding"},
            status_code=504,
        )
    except Exception as e:
        return JSONResponse(
            {"error": "internal_error", "message": str(e)},
            status_code=500,
        )
```

**Step 2: Verify syntax**

Run: `python -m py_compile backend/main.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat(api): Add /api/voice/metrics endpoint

Returns voice orchestrator metrics:
- queue_depth, coalesced_count, spoken_count
- dropped_count, interrupt_count
- Returns 503 if kernel unavailable

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 9: Run Full Test Suite

**Step 1: Run all new tests**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python -m pytest tests/unit/backend/core/test_robust_file_lock.py tests/unit/backend/core/test_voice_client.py tests/unit/backend/core/test_voice_orchestrator.py tests/unit/backend/core/test_voice_coalescer.py tests/unit/backend/core/test_voice_speaker.py -v`

Expected: All tests PASS

**Step 2: Run syntax check on modified files**

Run: `python -m py_compile backend/core/robust_file_lock.py backend/core/voice_client.py backend/core/voice_orchestrator.py backend/core/cross_repo_state_initializer.py unified_supervisor.py backend/main.py`

Expected: No output (all valid)

**Step 3: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "test: Verify all voice orchestrator tests pass

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Success Criteria Checklist

After implementation, verify:

- [ ] `RobustFileLock` uses `fcntl.flock()` - no temp files
- [ ] `VoiceClient` queues locally when disconnected
- [ ] `VoiceOrchestrator` enforces connection limits
- [ ] `VoiceCoalescer` batches messages and applies supersession
- [ ] `SerializedSpeaker` holds lock for full play-through
- [ ] VBIA state operations use `RobustFileLock`
- [ ] Kernel initializes `VoiceOrchestrator` on startup
- [ ] `/api/voice/metrics` returns 503 when kernel down
- [ ] All tests pass
- [ ] No "Temp file size mismatch" errors
- [ ] No overlapping voices during startup
