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
        self._released_tasks: set = set()
        self._started = False
        self._disabled = os.environ.get(
            "JARVIS_PREWARM_DISABLED", ""
        ).lower() in ("true", "1", "yes")

    def start(self) -> None:
        """Fire all background pre-warm tasks. Non-blocking.
        No-op if disabled. Registers PENDING for each task before submission."""
        if self._disabled:
            self._log.info("[PreWarm] Disabled via JARVIS_PREWARM_DISABLED")
            return
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="prewarm"
        )
        self._started = True
        self._log.info("[PreWarm] Starting background pre-warm tasks")
        # Task submissions will be added in Task 2

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

        # 1. Cancel un-released async tasks
        cancelled_any = False
        for name, task in list(self._async_tasks.items()):
            if name not in self._released_tasks and not task.done():
                task.cancel()
                cancelled_any = True
                self._log.info("[PreWarm] Cancelled async task: %s", name)

        # Drain one event-loop iteration so tasks transition from "cancelling"
        # to "cancelled" state synchronously.  This matters when shutdown() is
        # called from a synchronous frame inside a running event loop (e.g.
        # pytest-asyncio tests).  _run_once() is a private but stable CPython
        # implementation detail used only as a best-effort drain.
        if cancelled_any:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop._run_once()  # type: ignore[attr-defined]
            except Exception:
                pass

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
                self._log.warning("[PreWarm] %s: FAILED: %s", name, exc)

        self._futures[name] = self._executor.submit(wrapper)

    def _submit_async(self, name: str, coro_fn: Callable) -> None:
        """Submit an async task with exception wrapping."""
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
                self._log.warning("[PreWarm] %s: FAILED: %s", name, exc)

        self._async_tasks[name] = asyncio.create_task(
            wrapper(), name=f"prewarm_{name}"
        )
