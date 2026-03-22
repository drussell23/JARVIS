"""Background pre-warming for sequential boot pipeline.

Fires safe, non-mutating probes at T=0 before _startup_impl() runs.
Sequential phases check the result cache — if fresh and OK, skip
redundant work. If PENDING/FAILED/stale, run normally.

Invariants (from spec):
  - No mutation of authoritative boot config (env vars, ports, readiness)
  - No progress/dashboard updates
  - Same outcomes if pre-warmer is disabled or crashes
  - Single owner per async task (release_task for handoff)

See: docs/superpowers/specs/2026-03-22-sequential-boot-prewarming-design.md
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Lazy-imported at module level so tests can patch the name on this module.
# The actual import is deferred until the function runs to avoid heavy
# startup cost when gcp_enabled is False.
try:
    from backend.core.gcp_vm_manager import get_gcp_vm_manager  # noqa: F401
except Exception:  # pragma: no cover — module may not exist in all envs
    get_gcp_vm_manager = None  # type: ignore[assignment]


class PreWarmStatus(Enum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PreWarmResult:
    status: PreWarmStatus
    value: Any = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=lambda: time.monotonic() - 10_000_000.0)
    # Default is 10M seconds in the past so age_s is always enormous when unset.

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.timestamp


class StartupPreWarmer:
    """Advisory background pre-warmer for the sequential boot pipeline.

    Thread safety: _results is written by thread-pool workers and read
    by the async event loop. In CPython, single-key dict assignment is
    atomic under the GIL. If porting to a non-GIL runtime, add a
    threading.Lock. If iterating _results, snapshot keys first.
    """

    def __init__(self, config: Any, log: Optional[logging.Logger] = None):
        self._config = config
        self._log = log or logger
        self._executor: Optional[ThreadPoolExecutor] = None
        self._results: Dict[str, PreWarmResult] = {}
        self._futures: Dict[str, Future] = {}
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self._released_tasks: set[str] = set()
        self._started = False
        self._disabled = os.environ.get(
            "JARVIS_PREWARM_DISABLED", ""
        ).lower() in ("true", "1", "yes")

    def start(self) -> None:
        """Fire all background pre-warm tasks. Non-blocking.
        No-op if disabled. Registers PENDING for each task before submission.

        MUST be called from within a running event loop (the same loop that
        runs _startup_impl), so that asyncio.create_task() in _submit_async
        attaches tasks to that loop."""
        if self._disabled:
            self._log.info("[PreWarm] Disabled via JARVIS_PREWARM_DISABLED")
            return
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="prewarm"
        )
        self._started = True
        self._log.info("[PreWarm] Starting background pre-warm tasks")

        # Task 1: Docker daemon probe
        try:
            self._start_docker_probe()
        except Exception as e:
            self._log.warning("[PreWarm] Failed to start docker_probe: %s", e)

        # Task 2: GCP credential validation
        if self._config and getattr(self._config, 'gcp_enabled', False):
            try:
                self._start_gcp_creds()
            except Exception as e:
                self._log.warning("[PreWarm] Failed to start gcp_creds: %s", e)

        # Task 3: GCP VM proactive start
        if self._config and getattr(self._config, 'gcp_enabled', False):
            try:
                self._start_gcp_vm()
            except Exception as e:
                self._log.warning("[PreWarm] Failed to start gcp_vm_start: %s", e)

        # Task 4: Native library preload
        try:
            self._start_native_preload()
        except Exception as e:
            self._log.warning("[PreWarm] Failed to start native_libs: %s", e)

        # Task 5: GGUF model file scan
        try:
            self._start_gguf_scan()
        except Exception as e:
            self._log.warning("[PreWarm] Failed to start gguf_scan: %s", e)

        self._log.info(
            "[PreWarm] Submitted %d thread tasks, %d async tasks",
            len(self._futures), len(self._async_tasks),
        )

    def get_result(self, name: str, max_age_s: float = 30.0) -> Optional[PreWarmResult]:
        """Get a pre-warm result if OK and fresh. Returns None otherwise."""
        r = self._results.get(name)
        if r is None:
            return None
        if r.status != PreWarmStatus.OK:
            return None
        if r.age_s > max_age_s:
            return None
        return r

    def get_status(self, name: str) -> PreWarmStatus:
        """Get current status without age filtering. SKIPPED if unregistered."""
        r = self._results.get(name)
        return r.status if r else PreWarmStatus.SKIPPED

    def release_task(self, name: str) -> Optional[asyncio.Task]:
        """Release ownership of an async task to the caller.
        After release, shutdown() will NOT cancel this task.
        Returns None if never registered or already released."""
        if name in self._released_tasks or name not in self._async_tasks:
            return None
        self._released_tasks.add(name)
        return self._async_tasks[name]

    def shutdown(self, timeout: float = 5.0) -> None:
        """Bounded shutdown: cancel unreleased async tasks, stop executor."""
        if not self._started:
            return
        self._started = False

        # 1. Cancel un-released async tasks.
        # Note: task.cancelled() becomes True only after the event loop
        # processes the cancellation (one await point).  Callers that need
        # to confirm cancellation should await asyncio.sleep(0) after this
        # method returns (only relevant inside a running event loop).
        for name, task in list(self._async_tasks.items()):
            if name not in self._released_tasks and not task.done():
                task.cancel()
                self._log.info("[PreWarm] Cancelled async task: %s", name)

        # 2. Shutdown thread executor (Python 3.9+)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

            # 3. Bounded wait for thread futures
            pending = [f for f in self._futures.values() if not f.done()]
            if pending:
                done, not_done = futures_wait(pending, timeout=timeout)
                if not_done:
                    self._log.warning(
                        "[PreWarm] %d thread tasks still running after %.1fs shutdown",
                        len(not_done), timeout,
                    )
            self._executor = None

        self._log.info("[PreWarm] Shutdown complete")

    def _register_pending(self, name: str) -> None:
        """Register a task as PENDING before submitting work."""
        self._results[name] = PreWarmResult(
            status=PreWarmStatus.PENDING,
            timestamp=time.monotonic(),
        )

    def _submit_thread(self, name: str, fn: Callable) -> None:
        """Submit a thread-pool task with exception wrapping."""
        self._register_pending(name)

        def wrapper():
            try:
                value = fn()
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.OK, value=value,
                    timestamp=time.monotonic(),
                )
                self._log.info("[PreWarm] %s: OK", name)
            except Exception as exc:
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.FAILED, error=str(exc)[:200],
                    timestamp=time.monotonic(),
                )
                self._log.warning("[PreWarm] %s: FAILED", name, exc_info=True)

        self._futures[name] = self._executor.submit(wrapper)

    def _submit_async(self, name: str, coro_fn: Callable) -> None:
        """Submit an async task. MUST be called from a running event loop."""
        self._register_pending(name)

        async def wrapper():
            try:
                value = await coro_fn()
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.OK, value=value,
                    timestamp=time.monotonic(),
                )
                self._log.info("[PreWarm] %s: OK", name)
            except asyncio.CancelledError:
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.FAILED, error="cancelled",
                    timestamp=time.monotonic(),
                )
                raise
            except Exception as exc:
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.FAILED, error=str(exc)[:200],
                    timestamp=time.monotonic(),
                )
                self._log.warning("[PreWarm] %s: FAILED", name, exc_info=True)

        try:
            self._async_tasks[name] = asyncio.create_task(
                wrapper(), name=f"prewarm_{name}"
            )
        except RuntimeError as exc:
            # No running event loop — mark as failed, not stuck at PENDING
            self._results[name] = PreWarmResult(
                status=PreWarmStatus.FAILED, error=f"no event loop: {exc}"[:200],
                timestamp=time.monotonic(),
            )
            self._log.warning("[PreWarm] %s: FAILED (no event loop): %s", name, exc)

    # ------------------------------------------------------------------ #
    # Pre-warm task implementations                                         #
    # ------------------------------------------------------------------ #

    def _start_docker_probe(self) -> None:
        """Task #1: Probe Docker daemon via socket ping."""
        import socket as _module_socket

        def probe():
            import os as _os
            docker_host = _os.environ.get("DOCKER_HOST", "")
            if docker_host.startswith("unix://"):
                sock_path = docker_host[len("unix://"):]
            elif _os.path.exists("/var/run/docker.sock"):
                sock_path = "/var/run/docker.sock"
            else:
                sock_path = _os.path.expanduser("~/.docker/run/docker.sock")
            with _module_socket.socket(
                _module_socket.AF_UNIX, _module_socket.SOCK_STREAM
            ) as sock:
                sock.settimeout(15.0)
                sock.connect(sock_path)
                sock.sendall(b"GET /_ping HTTP/1.1\r\nHost: localhost\r\n\r\n")
                resp = sock.recv(256)
                return b"200" in resp

        self._submit_thread("docker_probe", probe)

    def _start_gcp_creds(self) -> None:
        """Task #2: Validate GCP credentials and create API client."""
        def validate():
            from google.cloud import compute_v1
            client = compute_v1.InstancesClient()
            return client
        self._submit_thread("gcp_creds", validate)

    def _start_gcp_vm(self) -> None:
        """Task #3: Proactive GCP VM start (idempotent).
        Does NOT write env vars, dashboard, routing. Only caches result."""
        async def start_vm():
            manager = await get_gcp_vm_manager()
            if not manager.is_static_vm_mode:
                self._log.info("[PreWarm] gcp_vm_start: not in static mode — skipping")
                return (False, None, "not_static_mode")
            success, ip, status = await manager.ensure_static_vm_ready()
            return (success, ip, status)
        self._submit_async("gcp_vm_start", start_vm)

    def _start_native_preload(self, modules: list | None = None) -> None:
        """Task #4: Import heavy native libraries in background thread."""
        if modules is None:
            modules = ["numpy", "scipy", "sounddevice", "soundfile", "webrtcvad", "PIL"]

        def preload():
            imported = []
            for mod in modules:
                try:
                    __import__(mod)
                    imported.append(mod)
                except ImportError:
                    pass
            return imported

        self._submit_thread("native_libs", preload)

    def _start_gguf_scan(self, models_dir: str | None = None) -> None:
        """Task #5: Scan for GGUF model files on disk."""
        if models_dir is None:
            models_dir = os.environ.get(
                "PRIME_MODELS_DIR", os.path.expanduser("~/.jarvis/models")
            )

        def scan():
            import pathlib
            p = pathlib.Path(models_dir)
            if not p.is_dir():
                return []
            return [
                (str(f), f.stat().st_size, f.stat().st_mtime)
                for f in p.glob("*.gguf")
            ]

        self._submit_thread("gguf_scan", scan)
