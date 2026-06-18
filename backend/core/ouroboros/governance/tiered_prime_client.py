"""TieredPrimeClient -- composite J-Prime tier (Phase 3.2).

Drop-in PrimeClient placing a HEAVY tier (GCP jarvis-prime via get_prime_client)
ALONGSIDE a LIGHT tier (LocalPrimeClient / Ollama). Prefers heavy; falls to light
on heavy failure/unavailability. Presents the same generate/_check_health/aclose
contract PrimeProvider already consumes (no changes to PrimeProvider).

Task 1 = basic routing. Health hysteresis FSM (Task 2) and speculative hedging
(Task 3) layer on top without changing this contract.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _hedge_enabled_default() -> bool:
    return os.environ.get("JARVIS_TIERED_HEDGE_ENABLED", "").strip().lower() in _TRUE


def _hedge_window_default_s() -> float:
    return float(os.environ.get("JARVIS_TIERED_HEDGE_WINDOW_MS", "800")) / 1000.0


def tiered_enabled() -> bool:
    """Master switch for composing the GCP-heavy + local-light tiered client.
    Default OFF -> the heavy tier is wired but NOT activated."""
    return os.environ.get("JARVIS_JPRIME_TIERED_ENABLED", "").strip().lower() in _TRUE


class HeavyState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


def _is_available(status: Any) -> bool:
    return getattr(status, "name", "") == "AVAILABLE"


async def _close_any(client: Any) -> None:
    """Close a tier via aclose() or close(), best-effort (zero hanging FDs)."""
    if client is None:
        return
    for meth in ("aclose", "close"):
        fn = getattr(client, meth, None)
        if fn is not None:
            try:
                await fn()
            except Exception:
                logger.debug("[Tiered] close via %s failed", meth, exc_info=True)
            return


class TieredPrimeClient:
    """Composite of heavy + light PrimeClient-compatible tiers.

    Task 2 adds a health hysteresis FSM for the heavy tier:
    - Consecutive failures >= failure_threshold -> DEGRADED (route to light).
    - While DEGRADED within the cooldown window, heavy is completely bypassed.
    - Once the cooldown elapses, a NON-BLOCKING background recovery probe is
      scheduled (strong ref, cleared in finally). A clean probe re-promotes to
      HEALTHY; a failing probe keeps the state DEGRADED.
    """

    def __init__(
        self,
        *,
        heavy: Any,
        light: Any,
        now_fn: Optional[Callable[[], float]] = None,
        failure_threshold: Optional[int] = None,
        cooldown_s: Optional[float] = None,
        hedge_enabled: Optional[bool] = None,
        hedge_window_s: Optional[float] = None,
    ) -> None:
        self._heavy = heavy
        self._light = light
        self._now = now_fn or time.monotonic

        thr, cool = self._resolve_recovery_params()
        self._failure_threshold: int = failure_threshold if failure_threshold is not None else thr
        self._cooldown_s: float = cooldown_s if cooldown_s is not None else cool

        # FSM state (Task 2)
        self._heavy_state: HeavyState = HeavyState.HEALTHY
        self._consecutive_failures: int = 0
        self._degraded_at: float = 0.0
        self._pending_recovery_probe: Optional[asyncio.Task] = None  # strong ref

        # Hedging (Task 3)
        self._hedge_enabled: bool = hedge_enabled if hedge_enabled is not None else _hedge_enabled_default()
        self._hedge_window_s: float = hedge_window_s if hedge_window_s is not None else _hedge_window_default_s()

    @staticmethod
    def _resolve_recovery_params() -> tuple[int, float]:
        """Pull circuit-breaker params from recovery_policy; fall back to 3/30.0."""
        try:
            from backend.core.recovery_policy import get_recovery_params
            rp = get_recovery_params("prime_router")
            if rp is not None:
                return int(rp.circuit_failure_threshold), float(rp.circuit_recovery_seconds)
        except Exception:
            pass
        return 3, 30.0

    @property
    def provider_name(self) -> str:
        return "tiered-jprime"

    # ------------------------------------------------------------------
    # FSM helpers
    # ------------------------------------------------------------------

    def heavy_state(self) -> str:
        return self._heavy_state.value

    def _record_heavy_success(self) -> None:
        self._consecutive_failures = 0
        self._heavy_state = HeavyState.HEALTHY

    def _record_heavy_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            if self._heavy_state is not HeavyState.DEGRADED:
                self._heavy_state = HeavyState.DEGRADED
                self._degraded_at = self._now()
                logger.info(
                    "[Tiered] heavy DEGRADED after %d consecutive failures (cooldown=%.1fs)",
                    self._consecutive_failures,
                    self._cooldown_s,
                )

    def _cooldown_elapsed(self) -> bool:
        return (self._now() - self._degraded_at) >= self._cooldown_s

    async def _recovery_probe(self) -> None:
        """Non-blocking background probe: re-promote heavy only on a clean health check."""
        try:
            status = await self._heavy._check_health()
            if _is_available(status):
                self._record_heavy_success()
                logger.info("[Tiered] heavy recovery probe OK -> re-promoted HEALTHY")
            else:
                logger.debug("[Tiered] heavy recovery probe returned UNAVAILABLE -> stays DEGRADED")
        except Exception:
            logger.debug("[Tiered] heavy recovery probe raised", exc_info=True)
        finally:
            self._pending_recovery_probe = None

    def _maybe_schedule_recovery_probe(self) -> None:
        """Schedule a background recovery probe if cooldown has elapsed and none is running."""
        if (
            self._heavy_state is HeavyState.DEGRADED
            and self._cooldown_elapsed()
            and self._pending_recovery_probe is None
            and self._heavy is not None
        ):
            # ensure_future keeps a strong ref via self._pending_recovery_probe;
            # the finally block in _recovery_probe clears it when done.
            self._pending_recovery_probe = asyncio.ensure_future(self._recovery_probe())

    # ------------------------------------------------------------------
    # Core generate (Task 1 routing extended by Task 2 FSM)
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        context: Optional[Any] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        model_name: Optional[str] = None,
        task_profile: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        kw = dict(
            system_prompt=system_prompt,
            context=context,
            max_tokens=max_tokens,
            temperature=temperature,
            model_name=model_name,
            task_profile=task_profile,
            **kwargs,
        )
        # Heavy tier only when HEALTHY. When DEGRADED, skip heavy entirely and,
        # once the cooldown has elapsed, schedule a non-blocking recovery probe.
        if self._heavy is not None and self._heavy_state is HeavyState.HEALTHY:
            if self._hedge_enabled and self._light is not None:
                return await self._generate_hedged(prompt, kw)
            try:
                result = await self._heavy.generate(prompt, **kw)
                self._record_heavy_success()
                return result
            except Exception as exc:
                self._record_heavy_failure()
                logger.info(
                    "[Tiered] heavy generate failed (%s) state=%s -> light",
                    exc,
                    self._heavy_state.value,
                )
                if self._light is None:
                    raise
        else:
            # DEGRADED path: maybe kick off a background probe
            self._maybe_schedule_recovery_probe()

        if self._light is not None:
            return await self._light.generate(prompt, **kw)
        raise RuntimeError("tiered_prime_client: no tier available")

    # ------------------------------------------------------------------
    # Hedging helpers (Task 3)
    # ------------------------------------------------------------------

    async def _cancel_and_drain(self, task: "Optional[asyncio.Task]") -> None:
        """Cancel a laggard task and await it so no task/FD/socket leaks."""
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _generate_hedged(self, prompt: str, kw: dict) -> Any:
        """Start heavy; if it doesn't finish within the hedge window, race a local
        hedge. Accept first success; cancel the laggard cleanly. Heavy too-slow or
        failed -> record_heavy_failure (FSM signal); heavy win -> record_success."""
        heavy_task: asyncio.Task = asyncio.ensure_future(self._heavy.generate(prompt, **kw))
        done, _pending = await asyncio.wait({heavy_task}, timeout=self._hedge_window_s)
        if heavy_task in done:
            try:
                result = heavy_task.result()
                self._record_heavy_success()
                return result
            except Exception as exc:
                self._record_heavy_failure()
                logger.info("[Tiered] heavy failed within window (%s) -> light", exc)
                return await self._light.generate(prompt, **kw)
        # window elapsed: race a local hedge
        light_task: asyncio.Task = asyncio.ensure_future(self._light.generate(prompt, **kw))
        done, pending = await asyncio.wait(
            {heavy_task, light_task}, return_when=asyncio.FIRST_COMPLETED)
        # Prefer a heavy success; else take a light success.
        if heavy_task in done and not heavy_task.cancelled() and heavy_task.exception() is None:
            await self._cancel_and_drain(light_task)
            self._record_heavy_success()
            return heavy_task.result()
        if light_task in done and not light_task.cancelled() and light_task.exception() is None:
            await self._cancel_and_drain(heavy_task)
            self._record_heavy_failure()  # heavy too slow / failed -> soft failure
            return light_task.result()
        # neither produced a clean success: drain both (no-op on already-done tasks).
        await self._cancel_and_drain(light_task)
        await self._cancel_and_drain(heavy_task)
        # If the light hedge already finished with an exception (e.g. a deliberate
        # LocalMemoryCritical valve refusal), surface it directly rather than firing
        # a redundant third light call (which would duplicate the evict/refuse at
        # CRITICAL memory). Only fall back to a fresh light attempt when the light
        # hedge never produced a result (was still pending and got cancelled).
        if (light_task.done() and not light_task.cancelled()
                and light_task.exception() is not None):
            raise light_task.exception()  # type: ignore[misc]
        return await self._light.generate(prompt, **kw)

    # ------------------------------------------------------------------
    # Health + lifecycle
    # ------------------------------------------------------------------

    async def _check_health(self) -> Any:
        from backend.core.prime_client import PrimeStatus
        for tier in (self._heavy, self._light):
            if tier is None:
                continue
            try:
                if _is_available(await tier._check_health()):
                    return PrimeStatus.AVAILABLE
            except Exception:
                logger.debug("[Tiered] health probe failed", exc_info=True)
        return PrimeStatus.UNAVAILABLE

    async def aclose(self) -> None:
        await _close_any(self._heavy)
        await _close_any(self._light)


def build_tiered_prime_client(*, heavy: Any, light: Any) -> Any:
    """Factory. Both -> TieredPrimeClient; exactly one -> that one (passthrough);
    neither -> None (byte-identical legacy)."""
    if heavy is not None and light is not None:
        return TieredPrimeClient(heavy=heavy, light=light)
    if heavy is not None:
        return heavy
    if light is not None:
        return light
    return None
