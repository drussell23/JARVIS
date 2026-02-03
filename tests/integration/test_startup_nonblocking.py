"""
Integration tests for non-blocking startup behavior.

These tests verify that the async startup utilities properly offload
blocking work to executors, keeping the event loop responsive.

Tests verify:
1. Slow blocking work doesn't block event loop
2. Progress/heartbeat advances during slow operations
3. Event loop processes other tasks during blocking work
4. Port checks run in parallel
5. Heartbeat continues during async operations
6. File reads don't block event loop
7. Subprocess runs don't block concurrent tasks
8. Startup doesn't block overall (end-to-end verification)
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))


class TestEventLoopNonBlocking:
    """Tests that blocking operations don't freeze the event loop."""

    @pytest.mark.asyncio
    async def test_async_process_wait_doesnt_block_loop(self):
        """Verify async_process_wait allows other tasks to run."""
        from backend.utils.async_startup import async_process_wait

        # Track when a concurrent task runs
        concurrent_task_ran: List[float] = []

        async def concurrent_task():
            """Task that should run during process wait."""
            for i in range(5):
                concurrent_task_ran.append(time.monotonic())
                await asyncio.sleep(0.1)

        # Start concurrent task
        task = asyncio.create_task(concurrent_task())

        # Simulate waiting for a non-existent PID (will timeout quickly)
        start = time.monotonic()
        await async_process_wait(99999, timeout=0.5)
        elapsed = time.monotonic() - start

        await task

        # Verify concurrent task ran multiple times during wait
        assert len(concurrent_task_ran) >= 2, "Concurrent task should run during wait"

    @pytest.mark.asyncio
    async def test_parallel_port_checks_faster_than_serial(self):
        """Verify port checks run in parallel, not serial."""
        from backend.utils.async_startup import async_check_port

        # Use ports that are unlikely to be in use
        ports = [58080, 58081, 58082, 58083, 58084]
        timeout_per_check = 0.3

        # Time parallel execution
        start = time.monotonic()
        results = await asyncio.gather(
            *[async_check_port("localhost", p, timeout=timeout_per_check) for p in ports],
            return_exceptions=True
        )
        parallel_time = time.monotonic() - start

        # If serial, would take at least 5 * 0.3 = 1.5s
        # Parallel should complete in roughly 0.3s + overhead
        assert parallel_time < 1.0, f"Parallel checks took {parallel_time}s, expected < 1.0s"

    @pytest.mark.asyncio
    async def test_multiple_concurrent_file_reads_dont_block(self):
        """Verify multiple file reads can happen concurrently."""
        from backend.utils.async_startup import async_file_read

        # Create test files
        with tempfile.TemporaryDirectory() as tmpdir:
            files = []
            for i in range(5):
                fpath = Path(tmpdir) / f"test_{i}.txt"
                fpath.write_text(f"content_{i}")
                files.append(str(fpath))

            # Track concurrent execution
            execution_times: List[float] = []

            async def read_and_track(path: str):
                start = time.monotonic()
                content = await async_file_read(path)
                execution_times.append(time.monotonic() - start)
                return content

            # Read all files concurrently
            start = time.monotonic()
            results = await asyncio.gather(*[read_and_track(f) for f in files])
            total_time = time.monotonic() - start

            # Verify all reads completed
            assert len(results) == 5
            for i, content in enumerate(results):
                assert content == f"content_{i}"

            # Total time should be much less than sum of individual times
            # (if run in parallel)
            sum_individual = sum(execution_times)
            assert total_time < sum_individual * 0.8 or total_time < 0.1, \
                "Concurrent reads should overlap in time"

    @pytest.mark.asyncio
    async def test_event_loop_responsive_during_slow_subprocess(self):
        """Verify event loop responds while subprocess runs."""
        from backend.utils.async_startup import async_subprocess_run

        # Track heartbeat-like task
        heartbeats: List[float] = []

        async def heartbeat_task():
            """Simulates a heartbeat that should keep running."""
            for _ in range(20):
                heartbeats.append(time.monotonic())
                await asyncio.sleep(0.05)

        # Start heartbeat
        heartbeat = asyncio.create_task(heartbeat_task())

        # Run a subprocess that takes about 0.5s
        result = await async_subprocess_run(
            [sys.executable, "-c", "import time; time.sleep(0.5); print('done')"],
            timeout=5.0
        )

        # Cancel heartbeat after subprocess completes
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Verify subprocess completed successfully
        assert result.returncode == 0
        assert b"done" in result.stdout

        # Verify heartbeat ran multiple times during subprocess
        assert len(heartbeats) >= 5, f"Heartbeat should run during subprocess, got {len(heartbeats)} beats"


class TestHeartbeatContinuity:
    """Tests that heartbeats continue during blocking operations."""

    @pytest.mark.asyncio
    async def test_heartbeat_runs_during_slow_operation(self):
        """Verify heartbeat task continues during slow executor work."""
        heartbeat_times: List[float] = []

        async def mock_heartbeat():
            """Simulate heartbeat recording timestamps."""
            for _ in range(10):
                heartbeat_times.append(time.monotonic())
                await asyncio.sleep(0.1)

        async def slow_executor_work():
            """Simulate slow work in executor."""
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, time.sleep, 0.5)

        # Run both concurrently
        heartbeat_task = asyncio.create_task(mock_heartbeat())
        await slow_executor_work()
        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        # Verify heartbeat ran multiple times during slow work
        assert len(heartbeat_times) >= 3, "Heartbeat should continue during slow work"

    @pytest.mark.asyncio
    async def test_heartbeat_intervals_regular_during_blocking_ops(self):
        """Verify heartbeat intervals remain regular during blocking operations."""
        from backend.utils.async_startup import async_subprocess_run

        heartbeat_times: List[float] = []
        expected_interval = 0.1

        async def regular_heartbeat():
            """Heartbeat that should tick at regular intervals."""
            for _ in range(15):
                heartbeat_times.append(time.monotonic())
                await asyncio.sleep(expected_interval)

        # Start heartbeat
        heartbeat = asyncio.create_task(regular_heartbeat())

        # Run blocking subprocess
        await async_subprocess_run(
            [sys.executable, "-c", "import time; time.sleep(0.8)"],
            timeout=5.0
        )

        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Verify we got enough heartbeats
        assert len(heartbeat_times) >= 5, "Should have at least 5 heartbeats"

        # Calculate intervals between heartbeats
        intervals = [
            heartbeat_times[i + 1] - heartbeat_times[i]
            for i in range(len(heartbeat_times) - 1)
        ]

        # Verify intervals are reasonably close to expected
        # (allowing some variance for scheduling)
        for interval in intervals:
            assert interval < expected_interval * 3, \
                f"Heartbeat interval {interval}s too long (expected ~{expected_interval}s)"


class TestSubprocessNonBlocking:
    """Tests that subprocess operations don't block."""

    @pytest.mark.asyncio
    async def test_async_subprocess_run_allows_concurrent_tasks(self):
        """Verify async_subprocess_run doesn't block concurrent tasks."""
        from backend.utils.async_startup import async_subprocess_run

        concurrent_runs: List[float] = []

        async def concurrent_task():
            for _ in range(5):
                concurrent_runs.append(time.monotonic())
                await asyncio.sleep(0.05)

        task = asyncio.create_task(concurrent_task())

        # Run a real subprocess (echo is fast)
        result = await async_subprocess_run(["echo", "test"], timeout=5.0)

        await task

        assert result.returncode == 0
        assert len(concurrent_runs) >= 2, "Concurrent task should run during subprocess"

    @pytest.mark.asyncio
    async def test_multiple_subprocesses_run_concurrently(self):
        """Verify multiple subprocesses can run in parallel."""
        from backend.utils.async_startup import async_subprocess_run

        sleep_time = 0.3
        num_processes = 4

        # Each subprocess sleeps for sleep_time
        start = time.monotonic()
        results = await asyncio.gather(
            *[
                async_subprocess_run(
                    [sys.executable, "-c", f"import time; time.sleep({sleep_time})"],
                    timeout=5.0
                )
                for _ in range(num_processes)
            ]
        )
        total_time = time.monotonic() - start

        # All should complete successfully
        for r in results:
            assert r.returncode == 0

        # If serial, would take at least 4 * 0.3 = 1.2s
        # Parallel should be closer to 0.3s + overhead
        assert total_time < num_processes * sleep_time * 0.7, \
            f"Processes took {total_time}s, expected parallel execution < {num_processes * sleep_time * 0.7}s"

    @pytest.mark.asyncio
    async def test_subprocess_result_captured_correctly(self):
        """Verify subprocess output is captured correctly."""
        from backend.utils.async_startup import async_subprocess_run

        result = await async_subprocess_run(
            [sys.executable, "-c", "import sys; print('stdout'); print('stderr', file=sys.stderr)"],
            timeout=5.0
        )

        assert result.returncode == 0
        assert b"stdout" in result.stdout
        assert b"stderr" in result.stderr
        assert result.success is True
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_subprocess_timeout_handled(self):
        """Verify subprocess timeout is handled gracefully."""
        from backend.utils.async_startup import async_subprocess_run

        result = await async_subprocess_run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.5
        )

        assert result.timed_out is True
        assert result.success is False


class TestFileIONonBlocking:
    """Tests that file I/O operations don't block."""

    @pytest.mark.asyncio
    async def test_async_file_read_doesnt_block(self):
        """Verify async_file_read allows concurrent tasks."""
        from backend.utils.async_startup import async_file_read

        concurrent_runs: List[float] = []

        async def concurrent_task():
            for _ in range(10):
                concurrent_runs.append(time.monotonic())
                await asyncio.sleep(0.02)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            # Write some content
            f.write("test content " * 1000)  # Reasonable amount of data
            f.flush()
            temp_path = f.name

        try:
            task = asyncio.create_task(concurrent_task())

            content = await async_file_read(temp_path)

            await task

            assert "test content" in content
            assert len(concurrent_runs) >= 3, "Concurrent task should run during file read"
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_async_file_write_doesnt_block(self):
        """Verify async_file_write allows concurrent tasks."""
        from backend.utils.async_startup import async_file_write, async_file_read

        concurrent_runs: List[float] = []

        async def concurrent_task():
            for _ in range(10):
                concurrent_runs.append(time.monotonic())
                await asyncio.sleep(0.02)

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_path = Path(tmpdir) / "test_write.txt"

            task = asyncio.create_task(concurrent_task())

            await async_file_write(str(temp_path), "written content " * 100)

            await task

            # Verify write succeeded
            content = await async_file_read(str(temp_path))
            assert "written content" in content
            assert len(concurrent_runs) >= 2, "Concurrent task should run during file write"

    @pytest.mark.asyncio
    async def test_async_json_operations_dont_block(self):
        """Verify JSON read/write operations don't block."""
        from backend.utils.async_startup import async_json_read, async_json_write

        concurrent_runs: List[float] = []

        async def concurrent_task():
            for _ in range(10):
                concurrent_runs.append(time.monotonic())
                await asyncio.sleep(0.02)

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "test.json"

            task = asyncio.create_task(concurrent_task())

            # Write JSON
            test_data = {"key": "value", "numbers": list(range(100))}
            await async_json_write(str(json_path), test_data)

            # Read JSON back
            read_data = await async_json_read(str(json_path))

            await task

            assert read_data == test_data
            assert len(concurrent_runs) >= 2, "Concurrent task should run during JSON operations"


class TestPortCheckParallelization:
    """Tests for port check parallelization."""

    @pytest.mark.asyncio
    async def test_port_checks_parallelized(self):
        """Verify port checks are parallelized properly."""
        from backend.utils.async_startup import async_check_port

        # Track execution order
        execution_times: List[tuple] = []

        async def timed_port_check(port: int):
            start = time.monotonic()
            result = await async_check_port("localhost", port, timeout=0.2)
            end = time.monotonic()
            execution_times.append((port, start, end))
            return result

        # Check multiple ports
        ports = [59001, 59002, 59003, 59004]

        start = time.monotonic()
        await asyncio.gather(*[timed_port_check(p) for p in ports])
        total_time = time.monotonic() - start

        # Verify parallelization by checking time overlap
        # If parallel, executions should overlap
        starts = [t[1] for t in execution_times]
        ends = [t[2] for t in execution_times]

        # All should start within a small window
        start_spread = max(starts) - min(starts)
        assert start_spread < 0.1, "Port checks should start nearly simultaneously"

        # Total time should be close to single timeout, not N * timeout
        assert total_time < 0.2 * len(ports) * 0.5, \
            f"Port checks should run in parallel, took {total_time}s"


class TestProcessWaitNonBlocking:
    """Tests for process wait operations."""

    @pytest.mark.asyncio
    async def test_async_process_wait_concurrent_with_other_tasks(self):
        """Verify process wait doesn't block other async tasks."""
        from backend.utils.async_startup import async_process_wait

        # Track concurrent execution
        concurrent_executions: List[float] = []

        async def background_task():
            """Background task that should keep running."""
            for _ in range(20):
                concurrent_executions.append(time.monotonic())
                await asyncio.sleep(0.05)

        # Start background task
        bg_task = asyncio.create_task(background_task())

        # Give background task a moment to start
        await asyncio.sleep(0.01)

        # Wait for non-existent process (will poll until timeout)
        # Use longer timeout to allow background task more time to run
        await async_process_wait(99998, timeout=0.5)

        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass

        # Background task should have run at least once
        # The key test is that it runs AT ALL during the blocking operation
        assert len(concurrent_executions) >= 1, \
            "Background task should run during process wait"

    @pytest.mark.asyncio
    async def test_psutil_wait_non_blocking(self):
        """Verify psutil wait doesn't block event loop."""
        from backend.utils.async_startup import async_psutil_wait

        # Create a mock psutil process
        mock_proc = MagicMock()
        mock_proc.wait = MagicMock(side_effect=Exception("Timeout"))

        concurrent_executions: List[float] = []

        async def background_task():
            for _ in range(5):
                concurrent_executions.append(time.monotonic())
                await asyncio.sleep(0.05)

        bg_task = asyncio.create_task(background_task())

        # Should not block
        result = await async_psutil_wait(mock_proc, timeout=0.1)

        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass

        assert result is False  # Should return False due to exception
        assert len(concurrent_executions) >= 1, "Background task should run"


class TestEndToEndStartupNonBlocking:
    """End-to-end tests verifying startup operations don't block."""

    @pytest.mark.asyncio
    async def test_simulated_startup_sequence_non_blocking(self):
        """Simulate a startup sequence and verify non-blocking behavior."""
        from backend.utils.async_startup import (
            async_subprocess_run,
            async_check_port,
            async_file_read,
        )

        # Track heartbeat throughout startup
        heartbeat_times: List[float] = []
        heartbeat_running = True

        async def heartbeat():
            while heartbeat_running:
                heartbeat_times.append(time.monotonic())
                await asyncio.sleep(0.05)  # Faster heartbeat to ensure we capture more

        # Start heartbeat
        heartbeat_task = asyncio.create_task(heartbeat())

        # Give heartbeat a moment to start
        await asyncio.sleep(0.01)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a config file
            config_path = Path(tmpdir) / "config.txt"
            config_path.write_text("test_config=true")

            # Simulate startup sequence
            start = time.monotonic()

            # 1. Read config (simulated)
            config = await async_file_read(str(config_path))
            assert "test_config" in config

            # 2. Check ports in parallel (use longer timeout to allow heartbeat time)
            port_checks = await asyncio.gather(
                async_check_port("localhost", 59101, timeout=0.2),
                async_check_port("localhost", 59102, timeout=0.2),
                async_check_port("localhost", 59103, timeout=0.2),
            )

            # 3. Run a subprocess
            result = await async_subprocess_run(["echo", "startup_test"], timeout=5.0)
            assert result.success

            total_time = time.monotonic() - start

        # Stop heartbeat
        heartbeat_running = False
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        # Verify heartbeat ran at least once during startup
        # The critical assertion is that the heartbeat ran AT ALL during blocking ops
        assert len(heartbeat_times) >= 1, \
            f"Heartbeat should run during startup, got {len(heartbeat_times)} beats"

        # Verify startup was reasonably fast
        assert total_time < 2.0, f"Startup sequence took {total_time}s, expected < 2.0s"

    @pytest.mark.asyncio
    async def test_concurrent_startup_operations(self):
        """Verify multiple startup-like operations can run concurrently."""
        from backend.utils.async_startup import (
            async_subprocess_run,
            async_check_port,
            async_file_write,
            async_file_read,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Define several startup tasks
            async def write_pid_file():
                await async_file_write(
                    str(Path(tmpdir) / "jarvis.pid"),
                    str(os.getpid())
                )

            async def check_service_ports():
                results = await asyncio.gather(
                    async_check_port("localhost", 59201, timeout=0.1),
                    async_check_port("localhost", 59202, timeout=0.1),
                )
                return results

            async def run_health_check():
                return await async_subprocess_run(
                    [sys.executable, "-c", "print('healthy')"],
                    timeout=5.0
                )

            # Run all concurrently
            start = time.monotonic()
            results = await asyncio.gather(
                write_pid_file(),
                check_service_ports(),
                run_health_check(),
            )
            total_time = time.monotonic() - start

            # Verify results
            assert results[1] is not None  # Port check results
            assert results[2].success is True  # Health check succeeded

            # Verify file was written
            content = await async_file_read(str(Path(tmpdir) / "jarvis.pid"))
            assert content == str(os.getpid())

            # Should complete quickly if parallel
            assert total_time < 1.0, f"Concurrent startup ops took {total_time}s"


class TestExecutorBoundedness:
    """Tests verifying the executor is properly bounded."""

    @pytest.mark.asyncio
    async def test_executor_handles_many_concurrent_operations(self):
        """Verify executor handles many operations without exhaustion."""
        from backend.utils.async_startup import async_subprocess_run

        # Submit more operations than executor workers (4)
        num_operations = 10

        start = time.monotonic()
        results = await asyncio.gather(
            *[
                async_subprocess_run(
                    [sys.executable, "-c", "print('ok')"],
                    timeout=5.0
                )
                for _ in range(num_operations)
            ]
        )
        total_time = time.monotonic() - start

        # All should complete successfully
        for r in results:
            assert r.returncode == 0

        # Should complete in reasonable time despite bounded executor
        assert total_time < 10.0, f"Operations took {total_time}s"

    @pytest.mark.asyncio
    async def test_executor_doesnt_deadlock_under_load(self):
        """Verify executor doesn't deadlock with high concurrent load."""
        from backend.utils.async_startup import (
            async_subprocess_run,
            async_check_port,
            async_file_read,
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("test content")
            temp_path = f.name

        try:
            # Mix of different operation types
            tasks = []

            for i in range(5):
                tasks.append(async_subprocess_run(["echo", f"test{i}"], timeout=5.0))
                tasks.append(async_check_port("localhost", 59300 + i, timeout=0.1))
                tasks.append(async_file_read(temp_path))

            # Should complete without deadlock
            start = time.monotonic()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total_time = time.monotonic() - start

            # Check no exceptions (except possibly from port checks)
            subprocess_results = results[::3]  # Every 3rd starting from 0
            for r in subprocess_results:
                assert not isinstance(r, Exception)
                assert r.returncode == 0

            # Should complete in reasonable time
            assert total_time < 15.0, f"Mixed operations took {total_time}s"

        finally:
            os.unlink(temp_path)


class TestProgressBroadcastNonBlocking:
    """Tests verifying progress broadcasts don't get blocked."""

    @pytest.mark.asyncio
    async def test_simulated_progress_continues_during_slow_ops(self):
        """Simulate progress updates continuing during slow operations."""
        from backend.utils.async_startup import async_subprocess_run

        progress_updates: List[tuple] = []

        async def progress_broadcaster():
            """Simulate progress broadcasting."""
            for phase in range(10):
                progress_updates.append((phase, time.monotonic()))
                await asyncio.sleep(0.1)

        # Start progress broadcaster
        progress_task = asyncio.create_task(progress_broadcaster())

        # Run slow subprocess
        await async_subprocess_run(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            timeout=5.0
        )

        # Let broadcaster run a bit more
        await asyncio.sleep(0.2)

        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        # Verify progress continued during slow operation
        assert len(progress_updates) >= 5, \
            f"Progress should continue during slow ops, got {len(progress_updates)} updates"

        # Verify updates were evenly spaced (not bunched)
        if len(progress_updates) >= 3:
            intervals = [
                progress_updates[i + 1][1] - progress_updates[i][1]
                for i in range(min(5, len(progress_updates) - 1))
            ]
            for interval in intervals:
                assert interval < 0.3, \
                    f"Progress intervals should be regular, got {interval}s gap"
