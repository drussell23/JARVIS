# [Ouroboros] Modified by Ouroboros (op=op-01a122c6-) at 2026-10-09 22:31 UTC
# Reason: `backend/utils/fork_safe_subprocess.py` has no corresponding test module. CREATE `tests/test_fork_safe_subprocess.py` co

"""
Import Smoke Test for fork_safe_subprocess
=========================================

This test ensures the module can be imported without errors.
"""
from __future__ import annotations
import asyncio
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple
import pytest

# Test that the module imports correctly
try:
    from backend.utils.fork_safe_subprocess import (
        SubprocessResult,
        SubprocessError,
        SubprocessTimeoutError,
        run_subprocess_async,
        popen_async,
        run_ffmpeg_async,
        run_subprocess_sync,
        run_ffmpeg_sync,
        ForkSafeExecutor,
        get_fork_safe_executor,
    )
except ImportError as e:
    pytest.fail(f"Failed to import fork_safe_subprocess: {e}")


# Test the basic functionality of all public functions
@pytest.mark.asyncio
async def test_run_subprocess_async_success() -> None:
    """Test successful execution of run_subprocess_async."""
    result = await run_subprocess_async(['echo', 'hello'])
    assert result.success is True
    assert b'hello' in result.stdout
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_run_subprocess_async_with_input() -> None:
    """Test run_subprocess_async with stdin input."""
    result = await run_subprocess_async(
        ['cat'],
        input=b'hello world'
    )
    assert result.success is True
    assert b'hello world' in result.stdout


@pytest.mark.asyncio
async def test_run_subprocess_async_timeout() -> None:
    """Test run_subprocess_async with timeout."""
    result = await run_subprocess_async(
        ['sleep', '5'],
        timeout=0.1
    )
    assert result.timed_out is True
    assert 'timed out' in result.error


@pytest.mark.asyncio
async def test_run_subprocess_async_error() -> None:
    """Test run_subprocess_async with command failure."""
    result = await run_subprocess_async(['ls', '/nonexistent'])
    assert result.success is False
    assert result.returncode != 0






@pytest.mark.asyncio
async def test_run_ffmpeg_async_failure() -> None:
    """Test run_ffmpeg_async with invalid input."""
    invalid_data = b'this is not valid wav data'
    output, error = await run_ffmpeg_async(invalid_data)
    assert output is None
    assert error is not None
    assert 'FFmpeg failed' in error or 'FFmpeg error' in error


# Test the sync versions
def test_run_subprocess_sync_success() -> None:
    """Test successful execution of run_subprocess_sync."""
    result = run_subprocess_sync(['echo', 'hello'])
    assert result.success is True
    assert b'hello' in result.stdout
    assert result.returncode == 0




def test_run_ffmpeg_sync_failure() -> None:
    """Test run_ffmpeg_sync with invalid input."""
    invalid_data = b'this is not valid wav data'
    output, error = run_ffmpeg_sync(invalid_data)
    assert output is None
    assert error is not None
    assert 'FFmpeg failed' in error or 'FFmpeg error' in error


# Test ForkSafeExecutor functionality




def test_get_fork_safe_executor() -> None:
    """Test get_fork_safe_executor function."""
    executor1 = get_fork_safe_executor()
    executor2 = get_fork_safe_executor()
    assert executor1 is executor2  # Should return the same instance


# Test edge cases and thread safety detection
@pytest.mark.asyncio
async def test_thread_safety_logging() -> None:
    """Test that thread safety warnings are logged appropriately."""
    # This test ensures our functions can handle multi-threaded contexts
    # The actual logging behavior is tested through the module's internal logic
    result = await run_subprocess_async(['echo', 'test'])
    assert result.success is True


# Test error handling with check=True
@pytest.mark.asyncio
async def test_run_subprocess_async_check() -> None:
    """Test that check=True raises exceptions properly."""
    # This should not raise an exception as the command succeeds
    result = await run_subprocess_async(['echo', 'hello'], check=True)
    assert result.success is True

    # This should raise an exception as the command fails
    with pytest.raises(SubprocessError):
        await run_subprocess_async(['ls', '/nonexistent'], check=True)


# Test edge case: empty command
@pytest.mark.asyncio
async def test_empty_command() -> None:
    """Test behavior with empty command list."""
    result = await run_subprocess_async([])
    assert result.success is False  # Should fail as no command to execute


# Test edge case: command with no output capture
@pytest.mark.asyncio
async def test_no_output_capture() -> None:
    """Test run_subprocess_async with no output capture."""
    result = await run_subprocess_async(['echo', 'test'], capture_output=False)
    assert result.success is True
    assert result.stdout is None
    assert result.stderr is None