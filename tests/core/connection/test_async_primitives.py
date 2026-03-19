"""
Tests for Event-Loop-Aware Async Primitives.
"""

import pytest
import asyncio
import threading
from backend.core.connection.async_primitives import EventLoopAwareLock


@pytest.mark.asyncio
async def test_async_context_manager():
    """Async context manager should work correctly."""
    lock = EventLoopAwareLock()
    acquired = False

    async with lock:
        acquired = True
        assert lock.locked()

    assert acquired


@pytest.mark.asyncio
async def test_sync_context_manager():
    """Sync context manager should work correctly."""
    lock = EventLoopAwareLock()

    with lock:
        assert lock.locked()

    # Released after context
    assert not lock.locked()


@pytest.mark.asyncio
async def test_async_acquire_release():
    """Explicit acquire/release should work."""
    lock = EventLoopAwareLock()

    acquired = await lock.acquire()
    assert acquired
    assert lock.locked()

    lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_concurrent_async_access():
    """Concurrent async access should be serialized."""
    lock = EventLoopAwareLock()
    results = []

    async def worker(task_id: int):
        async with lock:
            results.append(f"start_{task_id}")
            await asyncio.sleep(0.01)
            results.append(f"end_{task_id}")

    # Run concurrently
    await asyncio.gather(*[worker(i) for i in range(3)])

    # Results should show serialized access
    # Each start should be followed by its corresponding end
    assert len(results) == 6
    for i in range(3):
        start_idx = results.index(f"start_{i}")
        end_idx = results.index(f"end_{i}")
        assert end_idx > start_idx


@pytest.mark.asyncio
async def test_lock_works_across_different_loops():
    """Lock should work correctly when accessed from different event loops."""
    lock = EventLoopAwareLock()
    results = []

    async def hold_lock(loop_id: int):
        async with lock:
            results.append(f"acquired_{loop_id}")
            await asyncio.sleep(0.01)
            results.append(f"released_{loop_id}")

    # Run in main loop
    await hold_lock(1)

    # Run in different loop via thread
    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(hold_lock(2))
        finally:
            loop.close()

    thread = threading.Thread(target=run_in_thread)
    thread.start()
    thread.join()

    # Both should have acquired and released
    assert 'acquired_1' in results
    assert 'released_1' in results
    assert 'acquired_2' in results
    assert 'released_2' in results


@pytest.mark.asyncio
async def test_reentrant_thread_lock():
    """Thread lock should be reentrant."""
    lock = EventLoopAwareLock()

    with lock:
        with lock:  # Should not deadlock
            pass


@pytest.mark.asyncio
async def test_exception_releases_lock():
    """Lock should be released even if exception occurs."""
    lock = EventLoopAwareLock()

    with pytest.raises(ValueError):
        async with lock:
            raise ValueError("test")

    # Lock should be released
    assert not lock.locked()
