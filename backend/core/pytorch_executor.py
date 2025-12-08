"""
PyTorch Single-Thread Executor for Apple Silicon Stability

This module provides a singleton executor that serializes ALL PyTorch operations
to a single dedicated thread. This prevents segfaults caused by:
1. Concurrent PyTorch model access from multiple threads
2. OpenMP/MKL threading conflicts with macOS Grand Central Dispatch
3. MPS (Metal) initialization race conditions

Usage:
    from core.pytorch_executor import get_pytorch_executor, run_in_pytorch_thread

    # Option 1: Get executor and use directly
    executor = get_pytorch_executor()
    result = await loop.run_in_executor(executor, my_pytorch_function)

    # Option 2: Use helper function
    result = await run_in_pytorch_thread(my_pytorch_function, arg1, arg2)

IMPORTANT: ALL PyTorch operations (model loading, inference, tensor operations)
MUST use this executor to prevent segfaults on Apple Silicon.
"""

import asyncio
import logging
import os
import sys
import platform
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar
from functools import partial

logger = logging.getLogger(__name__)

# ============================================================================
# Environment Setup (MUST happen before any torch import anywhere)
# ============================================================================
_IS_APPLE_SILICON = platform.machine() == 'arm64' and sys.platform == 'darwin'

if _IS_APPLE_SILICON:
    # Force single-threaded mode for all numeric libraries
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    os.environ.setdefault('MKL_NUM_THREADS', '1')
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    os.environ.setdefault('VECLIB_MAXIMUM_THREADS', '1')
    os.environ.setdefault('NUMEXPR_NUM_THREADS', '1')
    os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK', '1')
    os.environ.setdefault('PYTORCH_MPS_HIGH_WATERMARK_RATIO', '0.0')

# ============================================================================
# Singleton Executor
# ============================================================================

_pytorch_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_thread_id: Optional[int] = None

T = TypeVar('T')


def _init_pytorch_thread():
    """
    Initialize the PyTorch thread with proper settings.
    Called once when the thread starts its first task.
    """
    global _executor_thread_id
    _executor_thread_id = threading.current_thread().ident

    try:
        import torch

        # Force single-threaded PyTorch
        torch.set_num_threads(1)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass  # Already set

        logger.info(
            f"🔧 PyTorch executor thread initialized: "
            f"thread_id={_executor_thread_id}, "
            f"num_threads={torch.get_num_threads()}, "
            f"device=cpu (forced for stability)"
        )
    except ImportError:
        logger.warning("PyTorch not available - executor will run without torch settings")


def _pytorch_thread_initializer():
    """Thread initializer that sets up PyTorch settings once."""
    _init_pytorch_thread()


def get_pytorch_executor() -> ThreadPoolExecutor:
    """
    Get the singleton PyTorch executor.

    This executor has exactly ONE worker thread, ensuring all PyTorch
    operations are serialized and never run concurrently.

    Returns:
        ThreadPoolExecutor with max_workers=1
    """
    global _pytorch_executor

    if _pytorch_executor is None:
        with _executor_lock:
            # Double-check after acquiring lock
            if _pytorch_executor is None:
                logger.info("🚀 Creating singleton PyTorch executor (1 worker thread)")
                _pytorch_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="pytorch_worker",
                    initializer=_pytorch_thread_initializer
                )

    return _pytorch_executor


async def run_in_pytorch_thread(
    func: Callable[..., T],
    *args,
    **kwargs
) -> T:
    """
    Run a function in the dedicated PyTorch thread.

    This is the recommended way to run PyTorch operations from async code.
    It ensures the operation runs in the singleton executor thread, preventing
    concurrent access and segfaults.

    Args:
        func: The function to run (should contain PyTorch operations)
        *args: Positional arguments to pass to func
        **kwargs: Keyword arguments to pass to func

    Returns:
        The result of func(*args, **kwargs)

    Example:
        async def extract_embedding(audio_data):
            def _extract_sync(data):
                # PyTorch operations here
                with torch.no_grad():
                    return model.encode(data)

            return await run_in_pytorch_thread(_extract_sync, audio_data)
    """
    loop = asyncio.get_running_loop()
    executor = get_pytorch_executor()

    # Wrap function with args/kwargs
    if args or kwargs:
        func_with_args = partial(func, *args, **kwargs)
    else:
        func_with_args = func

    return await loop.run_in_executor(executor, func_with_args)


def run_in_pytorch_thread_sync(
    func: Callable[..., T],
    *args,
    timeout: Optional[float] = None,
    **kwargs
) -> T:
    """
    Synchronous version - run a function in the PyTorch thread and wait.

    Use this when you're not in an async context but need to run PyTorch
    operations safely.

    Args:
        func: The function to run
        *args: Positional arguments
        timeout: Optional timeout in seconds
        **kwargs: Keyword arguments

    Returns:
        The result of func(*args, **kwargs)
    """
    executor = get_pytorch_executor()

    if args or kwargs:
        func_with_args = partial(func, *args, **kwargs)
    else:
        func_with_args = func

    future = executor.submit(func_with_args)
    return future.result(timeout=timeout)


def is_pytorch_thread() -> bool:
    """Check if the current thread is the PyTorch executor thread."""
    return threading.current_thread().ident == _executor_thread_id


def shutdown_pytorch_executor(wait: bool = True):
    """
    Shutdown the PyTorch executor gracefully.

    Call this during application shutdown to clean up resources.

    Args:
        wait: If True, wait for pending tasks to complete
    """
    global _pytorch_executor

    with _executor_lock:
        if _pytorch_executor is not None:
            logger.info("🛑 Shutting down PyTorch executor...")
            _pytorch_executor.shutdown(wait=wait)
            _pytorch_executor = None
            logger.info("✅ PyTorch executor shut down")


# ============================================================================
# Decorator for PyTorch functions
# ============================================================================

def pytorch_thread(func: Callable[..., T]) -> Callable[..., T]:
    """
    Decorator to ensure a sync function runs in the PyTorch thread.

    Example:
        @pytorch_thread
        def load_model():
            return torch.load("model.pt")

        # When called, automatically runs in PyTorch thread
        model = load_model()  # Blocks until complete
    """
    def wrapper(*args, **kwargs):
        if is_pytorch_thread():
            # Already in PyTorch thread, run directly
            return func(*args, **kwargs)
        else:
            # Run in PyTorch thread
            return run_in_pytorch_thread_sync(func, *args, **kwargs)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def async_pytorch_thread(func: Callable[..., T]) -> Callable[..., T]:
    """
    Decorator for async functions that need PyTorch operations.

    The decorated function should be a regular (sync) function containing
    PyTorch operations. The decorator makes it awaitable and ensures it
    runs in the PyTorch thread.

    Example:
        @async_pytorch_thread
        def extract_embedding(audio_tensor):
            with torch.no_grad():
                return model.encode_batch(audio_tensor)

        # Usage in async code:
        embedding = await extract_embedding(tensor)
    """
    async def wrapper(*args, **kwargs):
        return await run_in_pytorch_thread(func, *args, **kwargs)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper
