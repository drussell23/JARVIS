"""LocalDaemonGovernor (Phase 3.4) -- autonomous, gated, ownership-safe lifecycle
for the local Ollama daemon.

Flag ON  -> JIT: if the daemon is down, start it (brew services) and verify
            /api/tags health before the local tier is routed to.
Flag OFF / loop shutdown -> flush resident weights (keep_alive:0) and, ONLY IF
            the governor started the daemon, stop it (free the ~2GB baseline).

SAFETY: gated by JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED (default OFF) and
ownership-tracked -- a daemon the operator started manually is flushed but NEVER
killed. Reuses the local tier's keep_alive:0 flush + /api/tags health (no dup).
Best-effort + fail-soft throughout: a governor failure never breaks the loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import time
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def daemon_governor_enabled() -> bool:
    return os.environ.get("JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED", "").strip().lower() in _TRUE


def _default_runner(cmd: List[str]) -> int:
    """Run a host command, return its exit code. Best-effort (never raises)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        return int(proc.returncode)
    except Exception:
        logger.debug("[DaemonGovernor] runner failed: %s", cmd, exc_info=True)
        return 1


class LocalDaemonGovernor:
    """Owns the local Ollama daemon lifecycle under the master gate."""

    def __init__(
        self,
        *,
        health_probe: Optional[Callable[[], Awaitable[bool]]] = None,
        flush: Optional[Callable[[], Awaitable[None]]] = None,
        runner: Optional[Callable[[List[str]], int]] = None,
        service_name: str = "ollama",
        start_timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._health_probe = health_probe or self._default_health_probe
        self._flush = flush or self._default_flush
        self._runner = runner or _default_runner
        self._service = service_name
        self._start_timeout_s = start_timeout_s
        self._poll_interval_s = poll_interval_s
        self._owned = False  # True only if WE started the daemon

    def owns_daemon(self) -> bool:
        return self._owned

    # -- default reuse of the local tier primitives ---------------------
    async def _default_health_probe(self) -> bool:
        try:
            from backend.core.ouroboros.governance.local_inference_director import (
                build_local_prime_client,
            )
            client = build_local_prime_client()
            if client is None:
                return False
            try:
                status = await client._check_health()
                return getattr(status, "name", "") == "AVAILABLE"
            finally:
                await client.aclose()
        except Exception:
            return False

    async def _default_flush(self) -> None:
        try:
            from backend.core.ouroboros.governance.local_inference_director import (
                build_local_prime_client,
                LocalConfig,
                LocalInferenceDirector,
            )
            client = build_local_prime_client()
            if client is None:
                return
            director = LocalInferenceDirector(LocalConfig.from_env(), client=client)
            try:
                await director._evict_model()  # keep_alive:0 flush (reuse)
            finally:
                await director.stop()
        except Exception:
            logger.debug("[DaemonGovernor] flush failed", exc_info=True)

    # -- lifecycle ------------------------------------------------------
    async def start_if_enabled(self) -> bool:
        """JIT: ensure the daemon is up when the local tier is enabled. Returns True
        iff the daemon is healthy afterwards. Only marks ownership if WE started it."""
        if not daemon_governor_enabled():
            return False
        try:
            from backend.core.ouroboros.governance.local_inference_director import (
                local_prime_enabled,
            )
            if not local_prime_enabled():
                return False
        except Exception:
            return False

        if await self._health_probe():
            self._owned = False  # already running -> not ours
            return True

        # daemon down -> start it (host mutation; gated above)
        if platform.system() != "Darwin":
            logger.info("[DaemonGovernor] non-macOS host -> skipping brew start")
            return False
        logger.info("[DaemonGovernor] JIT start: brew services start %s", self._service)
        self._runner(["brew", "services", "start", self._service])

        deadline = time.monotonic() + self._start_timeout_s
        while time.monotonic() < deadline:
            if await self._health_probe():
                self._owned = True  # we booted it -> we own it
                logger.info("[DaemonGovernor] local daemon healthy (owned)")
                return True
            await asyncio.sleep(self._poll_interval_s)
        logger.warning(
            "[DaemonGovernor] daemon did not become healthy within %.1fs",
            self._start_timeout_s,
        )
        return False

    async def stop_if_idle(self) -> None:
        """Flush resident weights; stop the daemon ONLY if the governor started it.
        Fires when the flag is OFF or the loop is shutting down. Fail-soft."""
        if not daemon_governor_enabled():
            return
        # always flush (frees unified-memory weights even for an operator-started daemon)
        try:
            await self._flush()
        except Exception:
            logger.debug("[DaemonGovernor] flush during stop failed", exc_info=True)
        # only stop a daemon WE started (never kill the operator's process)
        if self._owned and platform.system() == "Darwin":
            logger.info(
                "[DaemonGovernor] stopping governor-owned daemon: brew services stop %s",
                self._service,
            )
            self._runner(["brew", "services", "stop", self._service])
            self._owned = False
