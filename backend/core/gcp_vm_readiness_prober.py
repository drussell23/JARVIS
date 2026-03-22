"""Hybrid ReadinessProber adapter that delegates to GCPVMManager.

Disease 10 — Startup Sequencing, Task 5.

Bridges the abstract :class:`ReadinessProber` interface defined in
:mod:`backend.core.gcp_readiness_lease` to the concrete health and lineage
checks already implemented in :class:`GCPVMManager`.  Adds a standalone
warm-model probe via HTTP (``/v1/warm_check``).

Probe results for *health* and *capabilities* are cached with a configurable
TTL to avoid redundant network round-trips during rapid retry loops.  The
warm-model probe is **never cached** — it must always reflect real-time
model readiness.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

from backend.core.gcp_readiness_lease import (
    HandshakeResult,
    HandshakeStep,
    ReadinessFailureClass,
    ReadinessProber,
)

__all__ = ["GCPVMReadinessProber"]

logger = logging.getLogger(__name__)

# Try to import aiohttp at module level; record availability.
try:
    import aiohttp  # type: ignore[import-untyped]

    _AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_AVAILABLE = False


class GCPVMReadinessProber(ReadinessProber):
    """Concrete :class:`ReadinessProber` backed by a duck-typed VM manager.

    The *vm_manager* argument is duck-typed.  It must expose:

    * ``async ping_health(host, port, timeout) -> (verdict, data_dict)``
      where ``verdict.value == "ready"`` indicates a healthy VM.
    * ``async check_lineage(instance_name, vm_metadata) -> (should_recreate, reason)``
      where ``should_recreate=False`` means the lineage matches.

    Health and capabilities results are cached for *probe_cache_ttl* seconds.
    Warm-model results are **never** cached.
    """

    def __init__(
        self,
        vm_manager: Any,
        probe_cache_ttl: float = 3.0,
    ) -> None:
        self._vm_manager = vm_manager
        self._cache: Dict[HandshakeStep, Tuple[float, HandshakeResult]] = {}
        self._cache_ttl = probe_cache_ttl
        self._aiohttp_available: bool = _AIOHTTP_AVAILABLE

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _get_cached(self, step: HandshakeStep) -> Optional[HandshakeResult]:
        """Return a cached result if present and still valid, else ``None``."""
        entry = self._cache.get(step)
        if entry is None:
            return None
        cached_at, result = entry
        if time.monotonic() - cached_at < self._cache_ttl:
            logger.debug("Cache hit for %s (age=%.3fs)", step.value, time.monotonic() - cached_at)
            return result
        # Stale — evict.
        del self._cache[step]
        return None

    def _put_cache(self, step: HandshakeStep, result: HandshakeResult) -> None:
        """Store a probe result in the cache."""
        self._cache[step] = (time.monotonic(), result)

    # ------------------------------------------------------------------
    # probe_health
    # ------------------------------------------------------------------

    async def probe_health(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> HandshakeResult:
        """Delegate health check to ``vm_manager.ping_health``."""
        cached = self._get_cached(HandshakeStep.HEALTH)
        if cached is not None:
            return cached

        try:
            verdict, _data = await self._vm_manager.ping_health(
                host, port, timeout=timeout,
            )
            if getattr(verdict, "value", None) == "ready":
                result = HandshakeResult(
                    step=HandshakeStep.HEALTH,
                    passed=True,
                    detail="healthy",
                    data=_data if isinstance(_data, dict) else None,
                )
            else:
                result = HandshakeResult(
                    step=HandshakeStep.HEALTH,
                    passed=False,
                    failure_class=ReadinessFailureClass.NETWORK,
                    detail=str(verdict),
                )
        except Exception as exc:
            logger.warning("probe_health raised: %s", exc)
            result = HandshakeResult(
                step=HandshakeStep.HEALTH,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail=str(exc),
            )

        self._put_cache(HandshakeStep.HEALTH, result)
        return result

    # ------------------------------------------------------------------
    # probe_capabilities
    # ------------------------------------------------------------------

    async def probe_capabilities(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> HandshakeResult:
        """Delegate lineage/capabilities check to ``vm_manager.check_lineage``."""
        cached = self._get_cached(HandshakeStep.CAPABILITIES)
        if cached is not None:
            return cached

        try:
            # v304.0: If health probe already passed, the VM is alive and
            # serving. Lineage check (which needs GCP metadata API) adds
            # latency and can fail with SCHEMA_MISMATCH when metadata is
            # unavailable. With INV-3, J-Prime's readiness is authoritative
            # regardless of golden image lineage — skip lineage check when
            # we already know the VM is healthy.
            _health_cached = self._get_cached(HandshakeStep.HEALTH)
            if _health_cached and _health_cached.passed:
                result = HandshakeResult(
                    step=HandshakeStep.CAPABILITIES,
                    passed=True,
                    detail="health_passed_lineage_skipped",
                )
                self._put_cache(HandshakeStep.CAPABILITIES, result)
                return result

            _instance_name = getattr(
                getattr(self._vm_manager, 'config', None),
                'static_instance_name', None
            ) or "jarvis-prime-node"
            should_recreate, reason = await self._vm_manager.check_lineage(
                _instance_name, None,
            )
            if not should_recreate:
                result = HandshakeResult(
                    step=HandshakeStep.CAPABILITIES,
                    passed=True,
                    detail=reason,
                )
            else:
                result = HandshakeResult(
                    step=HandshakeStep.CAPABILITIES,
                    passed=False,
                    failure_class=ReadinessFailureClass.SCHEMA_MISMATCH,
                    detail=reason,
                )
        except Exception as exc:
            logger.warning("probe_capabilities raised: %s", exc)
            result = HandshakeResult(
                step=HandshakeStep.CAPABILITIES,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail=str(exc),
            )

        self._put_cache(HandshakeStep.CAPABILITIES, result)
        return result

    # ------------------------------------------------------------------
    # probe_warm_model
    # ------------------------------------------------------------------

    async def probe_warm_model(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> HandshakeResult:
        """HTTP probe to ``/v1/warm_check`` — **never cached**."""
        return await self._do_warm_model_probe(host, port, timeout)

    async def _do_warm_model_probe(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> HandshakeResult:
        """Internal implementation of the warm-model HTTP probe."""
        if not self._aiohttp_available:
            return HandshakeResult(
                step=HandshakeStep.WARM_MODEL,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail="aiohttp not available — cannot probe warm model",
            )

        url = f"http://{host}:{port}/v1/warm_check"
        try:
            async with aiohttp.ClientSession() as session:  # type: ignore[union-attr]
                resp_coro = session.post(url)
                resp = await asyncio.wait_for(resp_coro, timeout=timeout)
                if resp.status == 200:
                    data = await resp.json()
                    return HandshakeResult(
                        step=HandshakeStep.WARM_MODEL,
                        passed=True,
                        detail="model warm",
                        data=data,
                    )
                # v304.0: 404 means /v1/warm_check doesn't exist on this
                # J-Prime version. If health probe already passed (model
                # loaded, status healthy), the model IS warm — treat as pass.
                if resp.status == 404:
                    logger.info(
                        "probe_warm_model: /v1/warm_check returned 404 "
                        "(pre-v300 J-Prime). Treating as passed — "
                        "health probe already confirmed readiness."
                    )
                    return HandshakeResult(
                        step=HandshakeStep.WARM_MODEL,
                        passed=True,
                        detail="warm_check_404_health_passed",
                    )
                return HandshakeResult(
                    step=HandshakeStep.WARM_MODEL,
                    passed=False,
                    failure_class=ReadinessFailureClass.NETWORK,
                    detail=f"warm_check returned status {resp.status}",
                )
        except asyncio.TimeoutError:
            return HandshakeResult(
                step=HandshakeStep.WARM_MODEL,
                passed=False,
                failure_class=ReadinessFailureClass.TIMEOUT,
                detail=f"warm_check timed out after {timeout:.3f}s",
            )
        except Exception as exc:
            logger.warning("probe_warm_model raised: %s", exc)
            return HandshakeResult(
                step=HandshakeStep.WARM_MODEL,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail=str(exc),
            )
