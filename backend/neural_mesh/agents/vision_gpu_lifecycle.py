"""
On-demand GPU VM lifecycle for vision and text inference.

Starts the GPU VM when J-Prime is needed (vision or text), tracks idle
time, and stops it after configurable idle timeout to save costs.

Usage in vision agents:
    from .vision_gpu_lifecycle import ensure_vision_available
    if await ensure_vision_available():
        response = await client.send_vision_request(...)

Usage for text inference:
    from .vision_gpu_lifecycle import ensure_text_available
    if await ensure_text_available():
        response = await client.send_request(...)
"""

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_S = float(os.getenv("JARVIS_GPU_IDLE_TIMEOUT_S", "1800"))  # 30 min
_STARTUP_TIMEOUT_S = float(os.getenv("JARVIS_GPU_STARTUP_TIMEOUT_S", "300"))  # 5 min
_INSTANCE_NAME = os.getenv("JARVIS_GPU_INSTANCE_NAME", "jarvis-prime-gpu")
_INSTANCE_ZONE = os.getenv("JARVIS_GPU_INSTANCE_ZONE", "us-central1-a")
_GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "jarvis-473803")
_VISION_PORT = int(os.getenv("JARVIS_PRIME_VISION_PORT", "8001"))
_TEXT_PORT = int(os.getenv("JARVIS_PRIME_PORT", "8000"))
# Poll interval (seconds) when waiting for model to load after VM start
_MODEL_POLL_INTERVAL_S = float(os.getenv("JARVIS_GPU_MODEL_POLL_S", "10"))
# Maximum polls after VM start before giving up
_MODEL_MAX_POLLS = int(os.getenv("JARVIS_GPU_MODEL_MAX_POLLS", "30"))
# Idle-check loop cadence (seconds)
_IDLE_CHECK_INTERVAL_S = float(os.getenv("JARVIS_GPU_IDLE_CHECK_S", "60"))

_last_vision_use: float = 0.0
_idle_stop_task: Optional[asyncio.Task] = None
_starting_lock: Optional[asyncio.Lock] = None


def _get_starting_lock() -> asyncio.Lock:
    """Return (creating if needed) the module-level asyncio.Lock.

    The lock is created lazily inside the running event loop to avoid
    "attached to a different loop" errors when the module is imported
    at module-load time on Python 3.9/3.10.
    """
    global _starting_lock
    if _starting_lock is None:
        _starting_lock = asyncio.Lock()
    return _starting_lock


async def ensure_vision_available() -> bool:
    """Ensure J-Prime vision server is available, starting GPU VM if needed.

    Returns True if vision is ready, False if it could not be started.
    """
    global _last_vision_use

    # 1. Quick health check (cached, <1 ms if recent)
    try:
        from backend.core.prime_client import get_prime_client

        client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
        healthy = await asyncio.wait_for(client.get_vision_health(), timeout=5.0)
        if healthy:
            _last_vision_use = time.monotonic()
            _schedule_idle_stop()
            return True
    except Exception:
        pass

    # 2. Vision not healthy — try to start the GPU VM
    async with _get_starting_lock():
        # Double-check after acquiring lock (another caller may have started it)
        try:
            from backend.core.prime_client import get_prime_client

            client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
            healthy = await asyncio.wait_for(client.get_vision_health(), timeout=5.0)
            if healthy:
                _last_vision_use = time.monotonic()
                _schedule_idle_stop()
                return True
        except Exception:
            pass

        logger.info("[VisionGPU] J-Prime vision offline — starting GPU VM...")
        return await _start_gpu_vm_and_wait(wait_for_vision=True)


async def ensure_text_available() -> bool:
    """Ensure J-Prime text server is available, starting GPU VM if needed.

    Same as ensure_vision_available() but only waits for the text model
    (port 8000), not the vision model (port 8001). Faster cold start
    since text model loads first.
    """
    global _last_vision_use

    # Quick health check — text model on port 8000
    try:
        from backend.core.prime_client import get_prime_client, PrimeStatus

        client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
        status = await asyncio.wait_for(client._check_health(), timeout=5.0)
        if status == PrimeStatus.AVAILABLE:
            _last_vision_use = time.monotonic()
            _schedule_idle_stop()
            return True
    except Exception:
        pass

    # Text not healthy — start the GPU VM
    async with _get_starting_lock():
        # Double-check after lock
        try:
            from backend.core.prime_client import get_prime_client, PrimeStatus

            client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
            status = await asyncio.wait_for(client._check_health(), timeout=5.0)
            if status == PrimeStatus.AVAILABLE:
                _last_vision_use = time.monotonic()
                _schedule_idle_stop()
                return True
        except Exception:
            pass

        logger.info("[VisionGPU] J-Prime text offline — starting GPU VM...")
        return await _start_gpu_vm_and_wait(wait_for_vision=False)


async def _start_gpu_vm_and_wait(wait_for_vision: bool = True) -> bool:
    """Start the GPU VM and wait for models to load.

    Args:
        wait_for_vision: If True, wait for the vision model (port 8001).
                         If False, only wait for the text model (port 8000).
    """
    global _last_vision_use

    try:
        from backend.core.gcp_vm_manager import get_gcp_vm_manager

        vm_manager = await get_gcp_vm_manager()

        # Override instance config for GPU VM.
        original_name = vm_manager.config.static_instance_name
        original_zone = vm_manager.config.zone

        vm_manager.config.static_instance_name = _INSTANCE_NAME
        vm_manager.config.zone = _INSTANCE_ZONE

        try:
            success, ip_address, message = await asyncio.wait_for(
                vm_manager.ensure_static_vm_ready(),
                timeout=_STARTUP_TIMEOUT_S,
            )
        finally:
            vm_manager.config.static_instance_name = original_name
            vm_manager.config.zone = original_zone

        if success and ip_address:
            logger.info("[VisionGPU] GPU VM started at %s", ip_address)

            from backend.core.prime_client import get_prime_client, PrimeStatus

            client = await get_prime_client()
            await client.update_endpoint(ip_address, _TEXT_PORT)

            label = "vision" if wait_for_vision else "text"
            logger.info("[VisionGPU] Waiting for %s model loading...", label)

            for attempt in range(_MODEL_MAX_POLLS):
                await asyncio.sleep(_MODEL_POLL_INTERVAL_S)
                try:
                    if wait_for_vision:
                        healthy = await asyncio.wait_for(
                            client.get_vision_health(), timeout=5.0
                        )
                    else:
                        status = await asyncio.wait_for(
                            client._check_health(), timeout=5.0
                        )
                        healthy = (status == PrimeStatus.AVAILABLE)
                    if healthy:
                        elapsed = (attempt + 1) * _MODEL_POLL_INTERVAL_S
                        logger.info(
                            "[VisionGPU] %s server ready after %.0fs",
                            label.capitalize(), elapsed,
                        )
                        _last_vision_use = time.monotonic()
                        _schedule_idle_stop()
                        return True
                except Exception:
                    pass

            logger.warning(
                "[VisionGPU] %s server did not become ready "
                "(waited %.0fs across %d polls)",
                label.capitalize(),
                _MODEL_MAX_POLLS * _MODEL_POLL_INTERVAL_S,
                _MODEL_MAX_POLLS,
            )
            return False
        else:
            logger.warning("[VisionGPU] Failed to start GPU VM: %s", message)
            return False

    except ImportError:
        logger.debug("[VisionGPU] GCPVMManager not available — skipping VM start")
        return False
    except Exception as exc:
        logger.warning("[VisionGPU] GPU VM startup failed: %s", exc)
        return False


def record_vision_use() -> None:
    """Update the last-use timestamp and reschedule the idle-stop task.

    Call this after every successful vision request to keep the VM alive
    for the configured idle window.
    """
    global _last_vision_use
    _last_vision_use = time.monotonic()
    _schedule_idle_stop()


def _schedule_idle_stop() -> None:
    """Schedule (or reschedule) auto-stop of GPU VM after idle timeout."""
    global _idle_stop_task

    if _idle_stop_task and not _idle_stop_task.done():
        _idle_stop_task.cancel()

    _idle_stop_task = asyncio.create_task(
        _idle_stop_loop(),
        name="gpu_idle_stop",
    )


async def _idle_stop_loop() -> None:
    """Periodically check idle time and stop GPU VM when threshold is exceeded."""
    global _last_vision_use

    while True:
        await asyncio.sleep(_IDLE_CHECK_INTERVAL_S)

        idle_time = time.monotonic() - _last_vision_use
        if idle_time >= _IDLE_TIMEOUT_S:
            logger.info(
                "[VisionGPU] GPU idle for %.0fs (threshold: %.0fs) — stopping VM",
                idle_time,
                _IDLE_TIMEOUT_S,
            )
            await _stop_gpu_vm()
            break


async def _stop_gpu_vm() -> None:
    """Issue a gcloud stop command for the configured GPU VM instance."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud",
            "compute",
            "instances",
            "stop",
            _INSTANCE_NAME,
            f"--zone={_INSTANCE_ZONE}",
            f"--project={_GCP_PROJECT}",
            "--quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            logger.info("[VisionGPU] GPU VM stopped to save costs.")
        else:
            logger.warning(
                "[VisionGPU] gcloud stop returned non-zero exit code %d",
                proc.returncode,
            )
    except FileNotFoundError:
        logger.warning("[VisionGPU] gcloud CLI not found — cannot stop GPU VM")
    except Exception as exc:
        logger.warning("[VisionGPU] Failed to stop GPU VM: %s", exc)
