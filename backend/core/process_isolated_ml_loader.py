"""
Process-Isolated ML Loader
===========================

CRITICAL FIX for the root cause of unkillable startup hangs.

The Problem:
- SpeechBrain/PyTorch model loading is SYNCHRONOUS
- When called inside async functions, it blocks the event loop
- asyncio.wait_for() timeouts DON'T WORK because the event loop is blocked
- The process enters "uninterruptible sleep" (D state) and can't be killed
- Only a system restart can clear it

The Solution:
- Run ALL ML model loading in SEPARATE PROCESSES (not threads!)
- Multiprocessing allows true process termination via SIGKILL
- Parent process monitors with timeouts and kills if stuck
- Graceful fallback when models fail to load

Usage:
    from core.process_isolated_ml_loader import (
        load_model_isolated,
        load_speechbrain_model,
        cleanup_stuck_ml_processes
    )

    # Load SpeechBrain model with 30s timeout (truly killable)
    model = await load_speechbrain_model(
        model_name="speechbrain/spkrec-ecapa-voxceleb",
        timeout=30.0
    )

    # Or generic model loading
    result = await load_model_isolated(
        loader_func=my_model_loader,
        timeout=30.0,
        operation_name="Custom Model"
    )

Author: JARVIS AI System
Version: 1.0.0
"""

import asyncio
import logging
import multiprocessing
import os
import pickle
import signal
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import psutil

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class MLLoaderConfig:
    """Configuration for process-isolated ML loading."""
    default_timeout: float = 60.0  # Default timeout for model loading
    max_retries: int = 2  # Max retries on timeout
    retry_delay: float = 1.0  # Delay between retries
    cleanup_on_start: bool = True  # Clean stuck processes on initialization
    max_memory_mb: int = 4096  # Max memory per worker process
    worker_startup_timeout: float = 10.0  # Timeout for worker process to start
    graceful_shutdown_timeout: float = 5.0  # Time to wait for graceful shutdown
    force_kill_delay: float = 2.0  # Delay before SIGKILL after SIGTERM

    # Process identification
    process_marker: str = "JARVIS_ML_LOADER"

    @classmethod
    def from_environment(cls) -> 'MLLoaderConfig':
        """Load configuration from environment variables."""
        return cls(
            default_timeout=float(os.getenv('ML_LOADER_TIMEOUT', '60.0')),
            max_retries=int(os.getenv('ML_LOADER_MAX_RETRIES', '2')),
            cleanup_on_start=os.getenv('ML_LOADER_CLEANUP_ON_START', 'true').lower() == 'true',
            max_memory_mb=int(os.getenv('ML_LOADER_MAX_MEMORY_MB', '4096')),
        )


# Global configuration
_config: Optional[MLLoaderConfig] = None


def get_ml_loader_config() -> MLLoaderConfig:
    """Get current ML loader configuration."""
    global _config
    if _config is None:
        _config = MLLoaderConfig.from_environment()
    return _config


# =============================================================================
# Exceptions
# =============================================================================

class MLLoadTimeout(Exception):
    """Raised when ML model loading times out."""
    def __init__(self, operation: str, timeout: float, message: str = ""):
        self.operation = operation
        self.timeout = timeout
        self.timestamp = datetime.now()
        super().__init__(message or f"ML operation '{operation}' timed out after {timeout}s")


class MLLoadError(Exception):
    """Raised when ML model loading fails."""
    def __init__(self, operation: str, error: str, message: str = ""):
        self.operation = operation
        self.error = error
        self.timestamp = datetime.now()
        super().__init__(message or f"ML operation '{operation}' failed: {error}")


# =============================================================================
# Process Cleanup Utilities
# =============================================================================

def find_stuck_ml_processes(marker: str = "JARVIS_ML_LOADER") -> List[Dict[str, Any]]:
    """
    Find ML loader processes that appear stuck.

    Returns list of process info dicts with pid, age, status, etc.
    """
    stuck_processes = []

    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status', 'create_time']):
            try:
                info = proc.info
                cmdline = ' '.join(info.get('cmdline', []) or [])

                # Check if this is an ML loader process
                if marker in cmdline or 'speechbrain' in cmdline.lower() or 'torch' in cmdline.lower():
                    age = time.time() - (info.get('create_time', time.time()))
                    status = info.get('status', '')

                    # Consider stuck if:
                    # - In uninterruptible sleep (disk-sleep) state
                    # - Or running for too long with low CPU
                    is_stuck = False
                    stuck_reason = ""

                    if status in ['disk-sleep', 'stopped', 'zombie']:
                        is_stuck = True
                        stuck_reason = f"Process in {status} state"
                    elif age > 120:  # 2 minutes old
                        try:
                            cpu = proc.cpu_percent(interval=0.5)
                            if cpu < 1.0:  # Essentially idle
                                is_stuck = True
                                stuck_reason = f"Idle for {age:.0f}s (CPU: {cpu:.1f}%)"
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    if is_stuck or age > 300:  # Always flag if > 5 minutes old
                        stuck_processes.append({
                            'pid': info['pid'],
                            'name': info.get('name', 'unknown'),
                            'status': status,
                            'age_seconds': age,
                            'cmdline': cmdline[:100],
                            'stuck_reason': stuck_reason or f"Age: {age:.0f}s",
                        })

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    except Exception as e:
        logger.warning(f"Error finding stuck ML processes: {e}")

    return stuck_processes


def kill_process_tree(pid: int, timeout: float = 5.0) -> bool:
    """
    Kill a process and all its children.

    Uses SIGTERM first, then SIGKILL if needed.
    Returns True if process was killed successfully.
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

        # Kill children first
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass

        # Terminate parent
        parent.terminate()

        # Wait for graceful termination
        gone, alive = psutil.wait_procs([parent] + children, timeout=timeout)

        # Force kill any survivors
        for proc in alive:
            try:
                logger.warning(f"Force killing stuck process PID {proc.pid}")
                proc.kill()
            except psutil.NoSuchProcess:
                pass

        # Final wait
        psutil.wait_procs(alive, timeout=2.0)

        return True

    except psutil.NoSuchProcess:
        return True  # Already dead
    except Exception as e:
        logger.error(f"Error killing process tree {pid}: {e}")
        return False


async def cleanup_stuck_ml_processes(
    marker: str = "JARVIS_ML_LOADER",
    max_age_seconds: float = 300.0
) -> int:
    """
    Clean up any stuck ML loader processes.

    Should be called at startup to clear any zombie processes from
    previous crashed sessions.

    Returns number of processes cleaned up.
    """
    stuck = find_stuck_ml_processes(marker)
    cleaned = 0

    for proc_info in stuck:
        if proc_info['age_seconds'] > max_age_seconds or 'disk-sleep' in str(proc_info.get('status', '')):
            pid = proc_info['pid']
            logger.warning(
                f"Cleaning up stuck ML process: PID {pid} "
                f"(age: {proc_info['age_seconds']:.0f}s, reason: {proc_info['stuck_reason']})"
            )

            if await asyncio.to_thread(kill_process_tree, pid):
                cleaned += 1
                logger.info(f"Successfully cleaned up PID {pid}")
            else:
                logger.error(f"Failed to clean up PID {pid} - may require manual intervention")

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} stuck ML processes")

    return cleaned


# =============================================================================
# Worker Process Functions (Run in Subprocess)
# =============================================================================

def _worker_load_speechbrain_model(
    model_name: str,
    save_dir: str,
    device: str,
    result_file: str
) -> None:
    """
    Worker function that runs in a separate process to load SpeechBrain model.

    Writes result to a file to avoid pickling the model.
    """
    # Set process title for identification
    try:
        import setproctitle
        setproctitle.setproctitle(f"JARVIS_ML_LOADER: {model_name}")
    except ImportError:
        pass

    result = {
        'success': False,
        'error': None,
        'model_info': None,
        'load_time_ms': 0,
    }

    start_time = time.perf_counter()

    try:
        # Limit torch threads to prevent CPU overload
        import torch
        torch.set_num_threads(2)

        # Import SpeechBrain
        from speechbrain.pretrained import EncoderClassifier

        # Load the model
        logger.info(f"[Worker] Loading SpeechBrain model: {model_name}")

        model = EncoderClassifier.from_hparams(
            source=model_name,
            savedir=save_dir,
            run_opts={"device": device},
        )

        # Get model info (can't pickle the model itself)
        result['success'] = True
        result['model_info'] = {
            'name': model_name,
            'device': device,
            'save_dir': save_dir,
            'loaded': True,
        }
        result['load_time_ms'] = (time.perf_counter() - start_time) * 1000

        logger.info(f"[Worker] Model loaded successfully in {result['load_time_ms']:.0f}ms")

    except Exception as e:
        result['error'] = f"{type(e).__name__}: {str(e)}"
        result['traceback'] = traceback.format_exc()
        logger.error(f"[Worker] Model loading failed: {result['error']}")

    # Write result to file
    try:
        with open(result_file, 'wb') as f:
            pickle.dump(result, f)
    except Exception as e:
        logger.error(f"[Worker] Failed to write result: {e}")


def _worker_generic_loader(
    loader_func_pickled: bytes,
    args: tuple,
    kwargs: dict,
    result_file: str
) -> None:
    """
    Worker function that runs a generic loader function in a separate process.
    """
    result = {
        'success': False,
        'error': None,
        'result': None,
        'load_time_ms': 0,
    }

    start_time = time.perf_counter()

    try:
        # Unpickle the loader function
        loader_func = pickle.loads(loader_func_pickled)

        # Execute the loader
        load_result = loader_func(*args, **kwargs)

        result['success'] = True
        result['result'] = load_result
        result['load_time_ms'] = (time.perf_counter() - start_time) * 1000

    except Exception as e:
        result['error'] = f"{type(e).__name__}: {str(e)}"
        result['traceback'] = traceback.format_exc()

    # Write result to file
    try:
        with open(result_file, 'wb') as f:
            pickle.dump(result, f)
    except Exception as e:
        logger.error(f"[Worker] Failed to write result: {e}")


# =============================================================================
# Main Async Interface
# =============================================================================

class ProcessIsolatedMLLoader:
    """
    Loads ML models in isolated processes with true timeout/kill capability.

    This solves the fundamental problem where asyncio timeouts don't work
    when synchronous PyTorch/SpeechBrain code blocks the event loop.
    """

    def __init__(self, config: Optional[MLLoaderConfig] = None):
        self.config = config or get_ml_loader_config()
        self._initialized = False
        self._active_processes: Dict[int, multiprocessing.Process] = {}
        self._stats = {
            'total_loads': 0,
            'successful_loads': 0,
            'timeout_loads': 0,
            'error_loads': 0,
            'processes_killed': 0,
            'total_load_time_ms': 0.0,
        }

    async def initialize(self) -> None:
        """Initialize the loader, cleaning up any stuck processes."""
        if self._initialized:
            return

        if self.config.cleanup_on_start:
            cleaned = await cleanup_stuck_ml_processes(self.config.process_marker)
            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} stuck ML processes from previous session")

        self._initialized = True
        logger.info("Process-isolated ML loader initialized")

    async def load_speechbrain_model(
        self,
        model_name: str,
        save_dir: Optional[str] = None,
        device: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Load a SpeechBrain model in an isolated process.

        Args:
            model_name: HuggingFace model name (e.g., "speechbrain/spkrec-ecapa-voxceleb")
            save_dir: Directory to save model files
            device: Device to load model on ("cpu", "cuda", "mps")
            timeout: Timeout in seconds (default from config)

        Returns:
            Dict with success status and model info

        Raises:
            MLLoadTimeout: If loading times out
            MLLoadError: If loading fails
        """
        await self.initialize()

        timeout = timeout or self.config.default_timeout
        save_dir = save_dir or str(Path.home() / ".jarvis" / "models" / "speechbrain")
        device = device or self._get_optimal_device()

        # Create temp file for result (avoids pickling large model objects)
        result_file = tempfile.mktemp(suffix='.pkl', prefix='jarvis_ml_')

        self._stats['total_loads'] += 1
        start_time = time.perf_counter()

        try:
            logger.info(f"Loading SpeechBrain model in isolated process: {model_name}")

            # Create and start worker process
            process = multiprocessing.Process(
                target=_worker_load_speechbrain_model,
                args=(model_name, save_dir, device, result_file),
                name=f"{self.config.process_marker}_{model_name.replace('/', '_')}"
            )
            process.start()
            self._active_processes[process.pid] = process

            # Wait for completion with timeout
            result = await self._wait_for_process(
                process,
                result_file,
                timeout,
                f"SpeechBrain:{model_name}"
            )

            if result['success']:
                self._stats['successful_loads'] += 1
                self._stats['total_load_time_ms'] += result.get('load_time_ms', 0)
                logger.info(f"SpeechBrain model loaded: {model_name} ({result.get('load_time_ms', 0):.0f}ms)")
            else:
                self._stats['error_loads'] += 1
                raise MLLoadError("SpeechBrain", result.get('error', 'Unknown error'))

            return result

        except asyncio.TimeoutError:
            self._stats['timeout_loads'] += 1
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.error(f"SpeechBrain model load TIMEOUT after {elapsed:.0f}ms: {model_name}")
            raise MLLoadTimeout("SpeechBrain", timeout)

        finally:
            # Cleanup
            if os.path.exists(result_file):
                try:
                    os.remove(result_file)
                except:
                    pass

    async def load_generic(
        self,
        loader_func: Callable[..., T],
        *args,
        timeout: Optional[float] = None,
        operation_name: str = "GenericML",
        **kwargs
    ) -> T:
        """
        Run any ML loading function in an isolated process.

        Args:
            loader_func: Function to execute (must be picklable)
            *args: Arguments for the function
            timeout: Timeout in seconds
            operation_name: Name for logging
            **kwargs: Keyword arguments for the function

        Returns:
            The result of loader_func
        """
        await self.initialize()

        timeout = timeout or self.config.default_timeout
        result_file = tempfile.mktemp(suffix='.pkl', prefix='jarvis_ml_')

        self._stats['total_loads'] += 1
        start_time = time.perf_counter()

        try:
            logger.info(f"Loading {operation_name} in isolated process")

            # Pickle the loader function
            loader_pickled = pickle.dumps(loader_func)

            # Create and start worker process
            process = multiprocessing.Process(
                target=_worker_generic_loader,
                args=(loader_pickled, args, kwargs, result_file),
                name=f"{self.config.process_marker}_{operation_name}"
            )
            process.start()
            self._active_processes[process.pid] = process

            # Wait for completion with timeout
            result = await self._wait_for_process(
                process,
                result_file,
                timeout,
                operation_name
            )

            if result['success']:
                self._stats['successful_loads'] += 1
                self._stats['total_load_time_ms'] += result.get('load_time_ms', 0)
                return result['result']
            else:
                self._stats['error_loads'] += 1
                raise MLLoadError(operation_name, result.get('error', 'Unknown error'))

        except asyncio.TimeoutError:
            self._stats['timeout_loads'] += 1
            raise MLLoadTimeout(operation_name, timeout)

        finally:
            if os.path.exists(result_file):
                try:
                    os.remove(result_file)
                except:
                    pass

    async def _wait_for_process(
        self,
        process: multiprocessing.Process,
        result_file: str,
        timeout: float,
        operation_name: str
    ) -> Dict[str, Any]:
        """Wait for a worker process to complete with timeout."""

        start_time = time.perf_counter()
        check_interval = 0.1  # Check every 100ms

        while True:
            elapsed = time.perf_counter() - start_time

            # Check timeout
            if elapsed > timeout:
                await self._kill_process(process, operation_name)
                raise asyncio.TimeoutError()

            # Check if process completed
            if not process.is_alive():
                break

            # Yield to event loop
            await asyncio.sleep(check_interval)

        # Clean up tracking
        self._active_processes.pop(process.pid, None)

        # Read result from file
        if os.path.exists(result_file):
            try:
                with open(result_file, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                return {
                    'success': False,
                    'error': f"Failed to read result: {e}",
                }
        else:
            return {
                'success': False,
                'error': "Worker process did not produce result",
            }

    async def _kill_process(
        self,
        process: multiprocessing.Process,
        operation_name: str
    ) -> None:
        """Kill a worker process that has timed out."""

        pid = process.pid
        logger.warning(f"Killing stuck ML process: {operation_name} (PID {pid})")

        try:
            # Try graceful termination first
            process.terminate()

            # Wait briefly for graceful shutdown
            await asyncio.sleep(self.config.graceful_shutdown_timeout)

            if process.is_alive():
                # Force kill
                logger.warning(f"Force killing PID {pid}")
                process.kill()
                await asyncio.sleep(0.5)

            # Clean up zombie
            process.join(timeout=1.0)

            self._stats['processes_killed'] += 1
            self._active_processes.pop(pid, None)

        except Exception as e:
            logger.error(f"Error killing process {pid}: {e}")

    def _get_optimal_device(self) -> str:
        """Determine optimal device for ML models."""
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    def get_stats(self) -> Dict[str, Any]:
        """Get loader statistics."""
        return {
            **self._stats,
            'success_rate': (
                self._stats['successful_loads'] / max(1, self._stats['total_loads'])
            ),
            'avg_load_time_ms': (
                self._stats['total_load_time_ms'] / max(1, self._stats['successful_loads'])
            ),
            'active_processes': len(self._active_processes),
        }

    async def shutdown(self) -> None:
        """Shutdown the loader, killing any active processes."""
        logger.info("Shutting down process-isolated ML loader...")

        for pid, process in list(self._active_processes.items()):
            await self._kill_process(process, f"shutdown:{pid}")

        self._active_processes.clear()
        logger.info("ML loader shutdown complete")


# =============================================================================
# Singleton Instance
# =============================================================================

_loader_instance: Optional[ProcessIsolatedMLLoader] = None


def get_ml_loader() -> ProcessIsolatedMLLoader:
    """Get the global ML loader instance."""
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = ProcessIsolatedMLLoader()
    return _loader_instance


async def load_model_isolated(
    loader_func: Callable[..., T],
    *args,
    timeout: float = 60.0,
    operation_name: str = "MLModel",
    **kwargs
) -> T:
    """
    Convenience function to load any model in an isolated process.

    Usage:
        model = await load_model_isolated(
            my_loader_function,
            model_name="my-model",
            timeout=30.0,
            operation_name="MyModel"
        )
    """
    loader = get_ml_loader()
    return await loader.load_generic(
        loader_func, *args,
        timeout=timeout,
        operation_name=operation_name,
        **kwargs
    )


async def load_speechbrain_model(
    model_name: str,
    save_dir: Optional[str] = None,
    device: Optional[str] = None,
    timeout: float = 60.0
) -> Dict[str, Any]:
    """
    Convenience function to load a SpeechBrain model in an isolated process.

    Usage:
        result = await load_speechbrain_model(
            "speechbrain/spkrec-ecapa-voxceleb",
            timeout=30.0
        )
    """
    loader = get_ml_loader()
    return await loader.load_speechbrain_model(
        model_name=model_name,
        save_dir=save_dir,
        device=device,
        timeout=timeout
    )


# =============================================================================
# Async-Safe Model Loading Wrappers
# =============================================================================

async def load_model_async(
    sync_loader: Callable[[], T],
    timeout: float = 30.0,
    operation_name: str = "Model",
    use_thread: bool = True,
    use_process: bool = False
) -> Optional[T]:
    """
    Universal wrapper to load ANY model asynchronously with timeout.

    This is the recommended way to load ML models in async code.

    Args:
        sync_loader: Synchronous function that loads and returns the model
        timeout: Maximum time to wait
        operation_name: Name for logging
        use_thread: Use thread pool (faster, but can't kill if stuck)
        use_process: Use process pool (slower, but truly killable)

    Returns:
        The loaded model, or None if loading failed/timed out

    Example:
        model = await load_model_async(
            lambda: SomeModel.from_pretrained("model-name"),
            timeout=30.0,
            operation_name="SomeModel"
        )
    """
    logger.info(f"Loading {operation_name} asynchronously (timeout: {timeout}s)...")
    start_time = time.perf_counter()

    try:
        if use_process:
            # Use process isolation for truly killable loading
            result = await load_model_isolated(
                sync_loader,
                timeout=timeout,
                operation_name=operation_name
            )
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(f"{operation_name} loaded in process isolation ({elapsed:.0f}ms)")
            return result
        else:
            # Use thread pool - faster but can't kill if truly stuck
            result = await asyncio.wait_for(
                asyncio.to_thread(sync_loader),
                timeout=timeout
            )
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(f"{operation_name} loaded in thread pool ({elapsed:.0f}ms)")
            return result

    except asyncio.TimeoutError:
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.error(f"{operation_name} TIMEOUT after {elapsed:.0f}ms")
        return None

    except Exception as e:
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.error(f"{operation_name} FAILED after {elapsed:.0f}ms: {e}")
        return None


# =============================================================================
# Startup Cleanup
# =============================================================================

async def cleanup_before_startup(port: int = 8010) -> Dict[str, Any]:
    """
    Clean up any stuck processes before starting the backend.

    Should be called at the very beginning of main() before any async code.

    Returns:
        Dict with cleanup results
    """
    results = {
        'ml_processes_cleaned': 0,
        'port_freed': False,
        'zombies_cleaned': 0,
    }

    logger.info("Performing pre-startup cleanup...")

    # 1. Clean up stuck ML processes
    results['ml_processes_cleaned'] = await cleanup_stuck_ml_processes()

    # 2. Check if port is blocked by a stuck process
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            result = s.connect_ex(('localhost', port))
            if result == 0:
                # Port is in use, try to find and kill the blocker
                logger.warning(f"Port {port} is in use, checking for stuck process...")

                for conn in psutil.net_connections(kind='inet'):
                    if conn.laddr.port == port and conn.status == 'LISTEN':
                        pid = conn.pid
                        if pid:
                            try:
                                proc = psutil.Process(pid)
                                status = proc.status()

                                if status in ['disk-sleep', 'zombie', 'stopped']:
                                    logger.warning(f"Found stuck process on port {port}: PID {pid} ({status})")
                                    if await asyncio.to_thread(kill_process_tree, pid):
                                        results['port_freed'] = True
                                        logger.info(f"Freed port {port} by killing stuck process {pid}")
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
    except Exception as e:
        logger.debug(f"Port check error: {e}")

    # 3. Clean up any zombie processes
    try:
        for proc in psutil.process_iter(['pid', 'status', 'cmdline']):
            try:
                if proc.status() == 'zombie':
                    cmdline = ' '.join(proc.cmdline() or [])
                    if 'jarvis' in cmdline.lower() or 'python' in cmdline.lower():
                        proc.wait(timeout=0.1)  # Reap zombie
                        results['zombies_cleaned'] += 1
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
    except Exception as e:
        logger.debug(f"Zombie cleanup error: {e}")

    total_cleaned = (
        results['ml_processes_cleaned'] +
        results['zombies_cleaned'] +
        (1 if results['port_freed'] else 0)
    )

    if total_cleaned > 0:
        logger.info(f"Pre-startup cleanup complete: {total_cleaned} items cleaned")
    else:
        logger.info("Pre-startup cleanup complete: nothing to clean")

    return results


# =============================================================================
# Module Entry Point (for testing)
# =============================================================================

if __name__ == "__main__":
    async def test():
        print("Testing Process-Isolated ML Loader...")

        # Clean up first
        cleaned = await cleanup_stuck_ml_processes()
        print(f"Cleaned {cleaned} stuck processes")

        # Test loading a simple function
        loader = get_ml_loader()

        def slow_function():
            import time
            time.sleep(2)
            return "done"

        try:
            result = await loader.load_generic(
                slow_function,
                timeout=5.0,
                operation_name="SlowTest"
            )
            print(f"Result: {result}")
        except Exception as e:
            print(f"Error: {e}")

        print(f"Stats: {loader.get_stats()}")
        await loader.shutdown()

    asyncio.run(test())
