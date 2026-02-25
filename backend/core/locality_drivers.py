"""Locality Drivers for the JARVIS Control Plane.

Each driver wraps existing infrastructure behind the LocalityDriver protocol,
enabling the LifecycleEngine to manage components uniformly regardless of
whether they run in-process, as subprocesses, or on remote VMs.

- InProcessDriver: Components initialized within the supervisor process
- SubprocessDriver: Components managed via ProcessOrchestrator
- RemoteDriver: Components running on GCP VMs via GCPVMManager
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger("jarvis.locality_drivers")

__all__ = ["InProcessDriver", "SubprocessDriver", "RemoteDriver"]


class InProcessDriver:
    """Driver for components running inside the supervisor process.

    Wraps arbitrary async callables (init/shutdown functions) behind the
    LocalityDriver protocol. Each component is registered with its own
    start and stop coroutine.
    """

    def __init__(self) -> None:
        self._starters: Dict[str, Callable[..., Coroutine[Any, Any, bool]]] = {}
        self._stoppers: Dict[str, Callable[..., Coroutine[Any, Any, bool]]] = {}
        self._health_checkers: Dict[str, Callable[..., Coroutine[Any, Any, dict]]] = {}
        self._running: Dict[str, bool] = {}

    def register(
        self,
        component_name: str,
        start_fn: Callable[..., Coroutine[Any, Any, bool]],
        stop_fn: Callable[..., Coroutine[Any, Any, bool]],
        health_fn: Optional[Callable[..., Coroutine[Any, Any, dict]]] = None,
    ) -> None:
        """Register start/stop/health coroutines for a component."""
        self._starters[component_name] = start_fn
        self._stoppers[component_name] = stop_fn
        if health_fn is not None:
            self._health_checkers[component_name] = health_fn

    async def start(self, component_name: str, config: dict | None = None) -> bool:
        """Start an in-process component by calling its registered starter."""
        fn = self._starters.get(component_name)
        if fn is None:
            logger.warning("[InProcessDriver] No starter registered for %s", component_name)
            return False
        try:
            result = await fn()
            self._running[component_name] = bool(result)
            return bool(result)
        except Exception as exc:
            logger.error("[InProcessDriver] Start failed for %s: %s", component_name, exc)
            self._running[component_name] = False
            return False

    async def stop(self, component_name: str) -> bool:
        """Stop an in-process component by calling its registered stopper."""
        fn = self._stoppers.get(component_name)
        if fn is None:
            logger.warning("[InProcessDriver] No stopper registered for %s", component_name)
            return False
        try:
            result = await fn()
            self._running[component_name] = False
            return bool(result)
        except Exception as exc:
            logger.error("[InProcessDriver] Stop failed for %s: %s", component_name, exc)
            return False

    async def health_check(self, component_name: str) -> dict:
        """Check health of an in-process component."""
        fn = self._health_checkers.get(component_name)
        if fn is not None:
            try:
                return await fn()
            except Exception as exc:
                return {"healthy": False, "error": str(exc)}
        # Default: report based on running state
        return {"healthy": self._running.get(component_name, False)}

    async def send_drain(self, component_name: str, timeout_s: float = 30.0) -> bool:
        """In-process components drain by stopping gracefully."""
        return await self.stop(component_name)


class SubprocessDriver:
    """Driver for components managed as subprocesses.

    Wraps process management behind the LocalityDriver protocol.
    Delegates actual process operations to a provided spawn/kill interface.
    """

    def __init__(self) -> None:
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._spawn_fns: Dict[str, Callable[..., Coroutine[Any, Any, Optional[asyncio.subprocess.Process]]]] = {}
        self._health_urls: Dict[str, str] = {}

    def register(
        self,
        component_name: str,
        spawn_fn: Callable[..., Coroutine[Any, Any, Optional[asyncio.subprocess.Process]]],
        health_url: str = "",
    ) -> None:
        """Register a spawn function and optional health URL for a component."""
        self._spawn_fns[component_name] = spawn_fn
        if health_url:
            self._health_urls[component_name] = health_url

    async def start(self, component_name: str, config: dict | None = None) -> bool:
        """Start a subprocess component."""
        fn = self._spawn_fns.get(component_name)
        if fn is None:
            logger.warning("[SubprocessDriver] No spawn_fn registered for %s", component_name)
            return False
        try:
            proc = await fn()
            if proc is not None:
                self._processes[component_name] = proc
                return True
            return False
        except Exception as exc:
            logger.error("[SubprocessDriver] Spawn failed for %s: %s", component_name, exc)
            return False

    async def stop(self, component_name: str) -> bool:
        """Stop a subprocess component by terminating its process."""
        proc = self._processes.pop(component_name, None)
        if proc is None:
            return True  # Already stopped
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            return True
        except Exception as exc:
            logger.error("[SubprocessDriver] Stop failed for %s: %s", component_name, exc)
            return False

    async def health_check(self, component_name: str) -> dict:
        """Check health of subprocess via process state and optional HTTP endpoint."""
        proc = self._processes.get(component_name)
        if proc is None or proc.returncode is not None:
            return {"healthy": False, "error": "process_not_running"}

        health_url = self._health_urls.get(component_name)
        if health_url:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {"healthy": True, **data}
                        return {"healthy": False, "status_code": resp.status}
            except Exception as exc:
                return {"healthy": False, "error": str(exc)}

        return {"healthy": True, "pid": proc.pid}

    async def send_drain(self, component_name: str, timeout_s: float = 30.0) -> bool:
        """Send drain signal to subprocess via HTTP if health URL registered."""
        health_url = self._health_urls.get(component_name)
        if not health_url:
            return await self.stop(component_name)

        # Derive drain URL from health URL
        base = health_url.rsplit("/", 1)[0]
        drain_url = f"{base}/lifecycle/drain"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    drain_url,
                    json={"timeout_s": timeout_s},
                    timeout=aiohttp.ClientTimeout(total=timeout_s + 5),
                ) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("[SubprocessDriver] Drain failed for %s: %s", component_name, exc)
            return False


class RemoteDriver:
    """Driver for components running on remote VMs (GCP).

    Wraps VM lifecycle operations behind the LocalityDriver protocol.
    Delegates to provided async callables for VM provisioning and management.
    """

    def __init__(self) -> None:
        self._start_fns: Dict[str, Callable[..., Coroutine[Any, Any, bool]]] = {}
        self._stop_fns: Dict[str, Callable[..., Coroutine[Any, Any, bool]]] = {}
        self._health_urls: Dict[str, str] = {}
        self._endpoints: Dict[str, str] = {}

    def register(
        self,
        component_name: str,
        start_fn: Callable[..., Coroutine[Any, Any, bool]],
        stop_fn: Callable[..., Coroutine[Any, Any, bool]],
        health_url: str = "",
    ) -> None:
        """Register VM lifecycle functions for a component."""
        self._start_fns[component_name] = start_fn
        self._stop_fns[component_name] = stop_fn
        if health_url:
            self._health_urls[component_name] = health_url

    def set_endpoint(self, component_name: str, url: str) -> None:
        """Update the endpoint URL for a remote component (hot-swap support)."""
        self._endpoints[component_name] = url
        logger.info("[RemoteDriver] Endpoint updated for %s: %s", component_name, url)

    async def start(self, component_name: str, config: dict | None = None) -> bool:
        """Start a remote component (provision VM)."""
        fn = self._start_fns.get(component_name)
        if fn is None:
            logger.warning("[RemoteDriver] No start_fn registered for %s", component_name)
            return False
        try:
            return await fn()
        except Exception as exc:
            logger.error("[RemoteDriver] Start failed for %s: %s", component_name, exc)
            return False

    async def stop(self, component_name: str) -> bool:
        """Stop a remote component (deprovision VM)."""
        fn = self._stop_fns.get(component_name)
        if fn is None:
            logger.warning("[RemoteDriver] No stop_fn registered for %s", component_name)
            return False
        try:
            result = await fn()
            self._endpoints.pop(component_name, None)
            return result
        except Exception as exc:
            logger.error("[RemoteDriver] Stop failed for %s: %s", component_name, exc)
            return False

    async def health_check(self, component_name: str) -> dict:
        """Check health of remote component via HTTP."""
        url = self._health_urls.get(component_name) or self._endpoints.get(component_name)
        if not url:
            return {"healthy": False, "error": "no_endpoint"}
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                health_endpoint = url if "/health" in url else f"{url}/health"
                async with session.get(
                    health_endpoint,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {"healthy": True, **data}
                    return {"healthy": False, "status_code": resp.status}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    async def send_drain(self, component_name: str, timeout_s: float = 30.0) -> bool:
        """Send drain signal to remote component via HTTP."""
        url = self._endpoints.get(component_name)
        if not url:
            return False
        drain_url = f"{url}/lifecycle/drain"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    drain_url,
                    json={"timeout_s": timeout_s},
                    timeout=aiohttp.ClientTimeout(total=timeout_s + 5),
                ) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("[RemoteDriver] Drain failed for %s: %s", component_name, exc)
            return False
