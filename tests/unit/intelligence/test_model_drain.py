"""Tests for in-flight task drain during model unload."""
import asyncio
import concurrent.futures
import threading
import time
import pytest


class TestModelDrain:
    def test_drain_completes_running_task(self):
        """Drain should wait for running task to complete."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        completed = []

        def slow_inference():
            time.sleep(0.1)
            completed.append(True)
            return "result"

        executor.submit(slow_inference)
        time.sleep(0.01)  # Let task start
        executor.shutdown(wait=True)
        assert len(completed) == 1

    def test_cancel_futures_cancels_pending(self):
        """cancel_futures=True should cancel pending (not running) tasks."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def task(n):
            time.sleep(0.1)

        f1 = executor.submit(task, 1)
        f2 = executor.submit(task, 2)  # Pending
        time.sleep(0.01)
        executor.shutdown(wait=False, cancel_futures=True)
        time.sleep(0.2)
        assert f2.cancelled() or f2.done()

    @pytest.mark.asyncio
    async def test_drain_timeout_produces_abandoned(self):
        """If drain exceeds deadline, disposition should be 'abandoned'."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        stop_event = threading.Event()

        def long_task():
            # Block until signalled -- allows clean teardown after test
            stop_event.wait(timeout=100)

        executor.submit(long_task)
        time.sleep(0.01)

        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: executor.shutdown(wait=True)
                ),
                timeout=0.5,
            )
            disposition = "completed"
        except asyncio.TimeoutError:
            disposition = "abandoned"

        assert disposition == "abandoned"
        # Unblock the long task so the thread can exit cleanly
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)

    def test_executor_recreated_after_shutdown(self):
        """After shutdown, a new executor should be functional."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        executor.shutdown(wait=True)

        # Recreate
        new_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-inference"
        )
        future = new_executor.submit(lambda: 42)
        assert future.result(timeout=5) == 42
        new_executor.shutdown(wait=True)
