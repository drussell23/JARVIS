# tests/unit/backend/core/test_distributed_lock_manager.py
"""Tests for DistributedLockManager — file-based distributed locking.

Covers acquisition, release, concurrency, keepalive, cleanup, and context
manager behaviour.  All tests use the ``dlm`` fixture from
``tests/unit/conftest.py`` (file-backed, 2 s TTL, 1 s timeout) unless a
custom configuration is needed.

asyncio_mode = auto in pytest.ini — no ``@pytest.mark.asyncio`` required.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import List

import pytest

from backend.core.distributed_lock_manager import (
    DistributedLockManager,
    LockBackend,
    LockConfig,
    LockMetadata,
)


# =========================================================================
# Helpers
# =========================================================================

def _lock_file_path(dlm: DistributedLockManager, name: str) -> Path:
    """Return the expected lock file path for *name*."""
    return dlm.config.lock_dir / f"{name}{dlm.config.lock_extension}"


def _write_fake_lock(
    lock_dir: Path,
    name: str,
    *,
    owner: str = "fake-99999-0.0",
    pid: int = 99999,
    ttl: float = 10.0,
    acquired_offset: float = 0.0,
    extension: str = ".dlm.lock",
    fencing_token: int = 0,
) -> Path:
    """Write a synthetic lock file and return its path.

    ``acquired_offset`` is relative to *now* — negative means acquired in
    the past (and therefore possibly expired if ``ttl`` is short enough).
    """
    now = time.time()
    acquired_at = now + acquired_offset
    metadata = {
        "acquired_at": acquired_at,
        "expires_at": acquired_at + ttl,
        "owner": owner,
        "token": "deadbeef-dead-beef-dead-beefdeadbeef",
        "lock_name": name,
        "process_start_time": 0.0,
        "process_name": "fake",
        "process_cmdline": "fake",
        "machine_id": "test-machine",
        "backend": "file",
        "fencing_token": fencing_token,
        "repo_source": "jarvis",
        "extensions": 0,
    }
    lock_file = lock_dir / f"{name}{extension}"
    lock_file.write_text(json.dumps(metadata, indent=2))
    return lock_file


# =========================================================================
# TestDistributedLockManager
# =========================================================================

class TestDistributedLockManager:
    """Core acquisition, release, and concurrency tests."""

    async def test_acquire_release_basic(self, dlm: DistributedLockManager):
        """Acquire lock, verify file exists, release, verify file gone."""
        lock_path = _lock_file_path(dlm, "basic")

        async with dlm.acquire("basic") as acquired:
            assert acquired is True
            assert lock_path.exists(), "Lock file should exist while held"

        # After context exit the lock must be released.
        assert not lock_path.exists(), "Lock file should be removed after release"

    async def test_concurrent_acquire_fails(self, dlm: DistributedLockManager):
        """Two tasks try to acquire the same lock — exactly one wins."""
        results: List[bool] = []

        async def _grab():
            async with dlm.acquire("contended", timeout=0.5) as acq:
                results.append(acq)
                if acq:
                    await asyncio.sleep(0.3)

        await asyncio.gather(_grab(), _grab())

        assert results.count(True) == 1, (
            f"Exactly one task should win, got {results}"
        )
        assert results.count(False) == 1

    async def test_acquire_timeout_when_held(self, dlm: DistributedLockManager):
        """Second acquire times out when the lock is already held."""
        async with dlm.acquire("held", timeout=0.5, ttl=5.0) as first:
            assert first is True

            async with dlm.acquire("held", timeout=0.5) as second:
                assert second is False, "Should time out while lock is held"

    async def test_stale_lock_recovery(self, dlm: DistributedLockManager):
        """A lock file owned by a dead PID is cleaned up on acquire."""
        # PID 99999 is almost certainly dead (and the owner string embeds it).
        _write_fake_lock(
            dlm.config.lock_dir,
            "stale",
            owner="fake-99999-0.0",
            pid=99999,
            ttl=600.0,  # Not expired — only dead-PID detection matters.
        )

        async with dlm.acquire("stale", timeout=2.0) as acquired:
            assert acquired is True, "Should recover stale lock from dead PID"

    async def test_ttl_expiry_cleanup(self, dlm: DistributedLockManager):
        """Acquire with short TTL, wait, then verify cleanup removes it."""
        # Write a lock that expired 10 seconds ago.
        _write_fake_lock(
            dlm.config.lock_dir,
            "expired",
            owner=f"jarvis-{os.getpid()}-{time.time():.1f}",
            ttl=1.0,
            acquired_offset=-15.0,
        )

        lock_path = _lock_file_path(dlm, "expired")
        assert lock_path.exists()

        # _cleanup_stale_locks should remove the expired file.
        await dlm._cleanup_stale_locks()

        assert not lock_path.exists(), "Expired lock should be cleaned up"

    async def test_lock_file_contains_valid_json(self, dlm: DistributedLockManager):
        """Lock file is valid JSON with required fields."""
        async with dlm.acquire("jsoncheck") as acquired:
            assert acquired
            lock_path = _lock_file_path(dlm, "jsoncheck")
            data = json.loads(lock_path.read_text())

            for key in ("owner", "acquired_at", "expires_at", "token",
                        "lock_name", "process_start_time"):
                assert key in data, f"Missing field: {key}"

            assert isinstance(data["acquired_at"], float)
            assert isinstance(data["expires_at"], float)
            assert data["expires_at"] > data["acquired_at"]

    async def test_fencing_token_monotonic(self, dlm: DistributedLockManager):
        """Fencing tokens must be strictly monotonically increasing."""
        tokens: List[int] = []

        for _ in range(4):
            async with dlm.acquire_unified(
                "fencing", timeout=2.0, ttl=2.0, enable_keepalive=False
            ) as (acquired, metadata):
                assert acquired and metadata is not None
                tokens.append(metadata.fencing_token)

        # Each token must be greater than the previous.
        for i in range(1, len(tokens)):
            assert tokens[i] > tokens[i - 1], (
                f"Fencing tokens not monotonic: {tokens}"
            )

    async def test_owner_id_format(self, dlm: DistributedLockManager):
        """Owner ID matches ``{repo_source}-{pid}-{start_time}`` pattern."""
        pattern = re.compile(
            r"^[a-zA-Z0-9_-]+-\d+-\d+\.\d+$"
        )
        assert pattern.match(dlm._owner_id), (
            f"Owner ID {dlm._owner_id!r} does not match expected format"
        )

    async def test_release_allows_new_acquisition(
        self, dlm: DistributedLockManager
    ):
        """After release, a fresh acquire on the same name succeeds."""
        async with dlm.acquire("reacquire") as first:
            assert first is True

        async with dlm.acquire("reacquire") as second:
            assert second is True

    async def test_five_concurrent_acquires_one_wins(
        self, dlm: DistributedLockManager
    ):
        """Five concurrent tasks — exactly one acquires the lock."""
        results: List[bool] = []
        barrier = asyncio.Event()

        async def _contender():
            barrier.set()
            async with dlm.acquire("five_way", timeout=0.8) as acq:
                results.append(acq)
                if acq:
                    await asyncio.sleep(0.5)

        await asyncio.gather(*[_contender() for _ in range(5)])
        assert results.count(True) == 1, (
            f"Exactly 1 of 5 should win, got {results}"
        )

    async def test_acquire_creates_lock_dir_if_missing(self, tmp_path: Path):
        """If lock_dir does not exist, acquire creates it."""
        missing_dir = tmp_path / "nonexistent" / "locks"
        assert not missing_dir.exists()

        config = LockConfig(
            lock_dir=missing_dir,
            default_ttl_seconds=2.0,
            default_timeout_seconds=1.0,
            cleanup_interval_seconds=60.0,
            backend=LockBackend.FILE,
        )
        custom_dlm = DistributedLockManager(config=config)
        try:
            async with custom_dlm.acquire("autodir") as acquired:
                assert acquired is True
                assert missing_dir.exists(), "Lock dir should be created"
        finally:
            await custom_dlm.shutdown()

    async def test_different_lock_names_independent(
        self, dlm: DistributedLockManager
    ):
        """Locks with different names do not interfere with each other."""
        async with dlm.acquire("alpha", timeout=1.0) as acq_a:
            assert acq_a is True

            async with dlm.acquire("beta", timeout=1.0) as acq_b:
                assert acq_b is True, (
                    "Lock 'beta' should succeed while 'alpha' is held"
                )


# =========================================================================
# TestDLMKeepalive
# =========================================================================

class TestDLMKeepalive:
    """Tests for the background keepalive (TTL refresh) mechanism."""

    async def test_keepalive_refreshes_ttl(self, tmp_path: Path):
        """While a lock is held, the keepalive extends expires_at."""
        lock_dir = tmp_path / "ka_locks"
        lock_dir.mkdir()

        config = LockConfig(
            lock_dir=lock_dir,
            default_ttl_seconds=2.0,
            default_timeout_seconds=2.0,
            cleanup_interval_seconds=60.0,
            backend=LockBackend.FILE,
            keepalive_enabled=True,
            keepalive_interval_seconds=0.5,
        )
        ka_dlm = DistributedLockManager(config=config)

        try:
            async with ka_dlm.acquire("ka_test", ttl=2.0) as acquired:
                assert acquired
                lock_path = lock_dir / "ka_test.dlm.lock"

                # Read initial expiry.
                initial_data = json.loads(lock_path.read_text())
                initial_expiry = initial_data["expires_at"]

                # Wait long enough for at least one keepalive cycle.
                await asyncio.sleep(1.0)

                refreshed_data = json.loads(lock_path.read_text())
                refreshed_expiry = refreshed_data["expires_at"]

                assert refreshed_expiry > initial_expiry, (
                    "Keepalive should have extended expires_at"
                )
        finally:
            await ka_dlm.shutdown()

    async def test_keepalive_stops_on_release(self, tmp_path: Path):
        """After the context manager exits, no keepalive task remains."""
        lock_dir = tmp_path / "ka_stop_locks"
        lock_dir.mkdir()

        config = LockConfig(
            lock_dir=lock_dir,
            default_ttl_seconds=2.0,
            default_timeout_seconds=2.0,
            cleanup_interval_seconds=60.0,
            backend=LockBackend.FILE,
            keepalive_enabled=True,
            keepalive_interval_seconds=0.3,
        )
        ka_dlm = DistributedLockManager(config=config)

        try:
            async with ka_dlm.acquire("ka_stop") as acquired:
                assert acquired
                # Keepalive task should be running.
                assert "ka_stop" in ka_dlm._keepalive_tasks

            # After exit, the keepalive task must be gone.
            assert "ka_stop" not in ka_dlm._keepalive_tasks, (
                "Keepalive task should be removed after context exit"
            )
        finally:
            await ka_dlm.shutdown()

    async def test_keepalive_stopping_flag(self, tmp_path: Path):
        """The _keepalive_stopping flag is set during release and cleared after."""
        lock_dir = tmp_path / "ka_flag_locks"
        lock_dir.mkdir()

        config = LockConfig(
            lock_dir=lock_dir,
            default_ttl_seconds=2.0,
            default_timeout_seconds=2.0,
            cleanup_interval_seconds=60.0,
            backend=LockBackend.FILE,
            keepalive_enabled=True,
            keepalive_interval_seconds=0.3,
        )
        ka_dlm = DistributedLockManager(config=config)

        stopping_was_set = False

        # Monkey-patch _release_lock to observe the stopping flag mid-release.
        original_release = ka_dlm._release_lock

        async def _spying_release(lock_file, token):
            nonlocal stopping_was_set
            # At this point acquire_unified's finally block has already set
            # the stopping flag.
            if "ka_flag" in ka_dlm._keepalive_stopping:
                stopping_was_set = True
            return await original_release(lock_file, token)

        ka_dlm._release_lock = _spying_release

        try:
            async with ka_dlm.acquire("ka_flag") as acquired:
                assert acquired

            # The flag must have been set during release...
            assert stopping_was_set, (
                "_keepalive_stopping should be set during release"
            )
            # ...and cleaned up afterward.
            assert "ka_flag" not in ka_dlm._keepalive_stopping, (
                "_keepalive_stopping should be cleared after release"
            )
        finally:
            await ka_dlm.shutdown()


# =========================================================================
# TestDLMCleanup
# =========================================================================

class TestDLMCleanup:
    """Tests for stale-lock and orphaned temp-file cleanup."""

    async def test_stale_lock_files_removed(self, dlm: DistributedLockManager):
        """Expired lock files are removed by _cleanup_stale_locks."""
        # Create two expired locks (acquired 20 s ago, 1 s TTL = long expired).
        for name in ("stale_a", "stale_b"):
            _write_fake_lock(
                dlm.config.lock_dir,
                name,
                owner=f"jarvis-{os.getpid()}-{time.time():.1f}",
                ttl=1.0,
                acquired_offset=-20.0,
            )

        await dlm._cleanup_stale_locks()

        for name in ("stale_a", "stale_b"):
            assert not _lock_file_path(dlm, name).exists(), (
                f"Stale lock {name} should have been cleaned"
            )

    async def test_orphaned_tmp_files_cleaned(
        self, dlm: DistributedLockManager
    ):
        """Orphaned .tmp files are removed by _cleanup_orphaned_temp_files."""
        # Create fake tmp files that look old (dead PID, old mtime).
        for i in range(3):
            tmp_file = dlm.config.lock_dir / f"test.dlm.lock.tmp.99999.{i}.abcd1234"
            tmp_file.write_text("junk")
            # Set mtime to 120 s ago so the > 60 s age check triggers.
            old_time = time.time() - 120
            os.utime(tmp_file, (old_time, old_time))

        cleaned = await dlm._cleanup_orphaned_temp_files()
        assert cleaned >= 3, f"Expected >= 3 cleaned, got {cleaned}"

        remaining = list(dlm.config.lock_dir.glob("*.tmp.*"))
        assert len(remaining) == 0, f"Orphaned tmp files remain: {remaining}"

    async def test_cleanup_max_files_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """With DLM_CLEANUP_MAX_FILES=5, only the first 5 are processed."""
        monkeypatch.setenv("DLM_CLEANUP_MAX_FILES", "5")

        lock_dir = tmp_path / "cap_locks"
        lock_dir.mkdir()

        # Re-import to pick up the env var — but the constant is read at
        # module load time, so we patch it directly on the module.
        import backend.core.distributed_lock_manager as dlm_mod
        original_cap = dlm_mod.DLM_CLEANUP_MAX_FILES
        monkeypatch.setattr(dlm_mod, "DLM_CLEANUP_MAX_FILES", 5)

        config = LockConfig(
            lock_dir=lock_dir,
            default_ttl_seconds=2.0,
            default_timeout_seconds=1.0,
            cleanup_interval_seconds=60.0,
            backend=LockBackend.FILE,
        )
        cap_dlm = DistributedLockManager(config=config)

        # Create 10 old tmp files.
        for i in range(10):
            tmp = lock_dir / f"test.dlm.lock.tmp.99999.{i}.abcd1234"
            tmp.write_text("junk")
            old_time = time.time() - 120
            os.utime(tmp, (old_time, old_time))

        cleaned = await cap_dlm._cleanup_orphaned_temp_files()
        assert cleaned <= 5, (
            f"Cleanup cap should limit to 5, but cleaned {cleaned}"
        )


# =========================================================================
# TestDLMContextManager
# =========================================================================

class TestDLMContextManager:
    """Tests for the async context manager protocol of acquire()."""

    async def test_context_manager_yields_true(
        self, dlm: DistributedLockManager
    ):
        """Context manager yields True on successful acquisition."""
        async with dlm.acquire("ctx_true") as acquired:
            assert acquired is True

    async def test_context_exit_releases_lock(
        self, dlm: DistributedLockManager
    ):
        """Lock file is removed after the ``async with`` block exits."""
        lock_path = _lock_file_path(dlm, "ctx_exit")

        async with dlm.acquire("ctx_exit") as acquired:
            assert acquired
            assert lock_path.exists()

        assert not lock_path.exists(), "Lock should be released on context exit"

    async def test_failed_acquire_yields_false(
        self, dlm: DistributedLockManager
    ):
        """When the lock is held, a second context manager yields False."""
        async with dlm.acquire("ctx_fail", ttl=5.0) as first:
            assert first is True

            async with dlm.acquire("ctx_fail", timeout=0.5) as second:
                assert second is False

    async def test_nested_different_names(
        self, dlm: DistributedLockManager
    ):
        """Nested acquisitions of different lock names both succeed."""
        async with dlm.acquire("outer") as outer:
            assert outer is True

            async with dlm.acquire("inner") as inner:
                assert inner is True

            # Inner should be released, outer still held.
            assert _lock_file_path(dlm, "outer").exists()
            assert not _lock_file_path(dlm, "inner").exists()

        assert not _lock_file_path(dlm, "outer").exists()
