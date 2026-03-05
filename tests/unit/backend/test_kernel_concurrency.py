#!/usr/bin/env python3
"""
Concurrency safety tests for unified_supervisor.py kernel primitives.

Run: python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v
"""
import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestLazyAsyncLock:
    """Tests for LazyAsyncLock thread-safe initialization."""

    def test_single_lock_instance_under_concurrent_access(self):
        """Two coroutines racing _ensure_lock() must get the same Lock object."""
        from unified_supervisor import LazyAsyncLock

        lazy = LazyAsyncLock()
        results = []
        barrier = threading.Barrier(2)

        def grab_lock():
            # Python 3.9 requires an event loop to exist when creating
            # asyncio.Lock(). Set one per thread so _ensure_lock() can work.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                barrier.wait()
                lock = lazy._ensure_lock()
                results.append(id(lock))
            finally:
                loop.close()

        t1 = threading.Thread(target=grab_lock)
        t2 = threading.Thread(target=grab_lock)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert results[0] == results[1], (
            f"Two threads got different Lock objects: {results[0]} != {results[1]}"
        )

    @pytest.mark.asyncio
    async def test_mutual_exclusion_holds(self):
        """Two tasks using async with must actually serialize."""
        from unified_supervisor import LazyAsyncLock

        lazy = LazyAsyncLock()
        in_critical = 0
        max_concurrent = 0

        async def worker():
            nonlocal in_critical, max_concurrent
            async with lazy:
                in_critical += 1
                max_concurrent = max(max_concurrent, in_critical)
                await asyncio.sleep(0.01)
                in_critical -= 1

        await asyncio.gather(*[worker() for _ in range(10)])
        assert max_concurrent == 1, f"Critical section violated: max_concurrent={max_concurrent}"
