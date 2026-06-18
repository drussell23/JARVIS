"""TieredPrimeClient -- composite J-Prime tier (Phase 3.2).

Drop-in PrimeClient placing a HEAVY tier (GCP jarvis-prime via get_prime_client)
ALONGSIDE a LIGHT tier (LocalPrimeClient / Ollama). Prefers heavy; falls to light
on heavy failure/unavailability. Presents the same generate/_check_health/aclose
contract PrimeProvider already consumes (no changes to PrimeProvider).

Task 1 = basic routing. Health hysteresis FSM (Task 2) and speculative hedging
(Task 3) layer on top without changing this contract.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
    """Composite of heavy + light PrimeClient-compatible tiers."""

    def __init__(self, *, heavy: Any, light: Any) -> None:
        self._heavy = heavy
        self._light = light

    @property
    def provider_name(self) -> str:
        return "tiered-jprime"

    async def generate(self, prompt: str, system_prompt: Optional[str] = None,
                       context: Optional[Any] = None, max_tokens: int = 4096,
                       temperature: float = 0.7, model_name: Optional[str] = None,
                       task_profile: Optional[Any] = None, **kwargs: Any) -> Any:
        kw = dict(system_prompt=system_prompt, context=context, max_tokens=max_tokens,
                  temperature=temperature, model_name=model_name,
                  task_profile=task_profile, **kwargs)
        if self._heavy is not None:
            try:
                return await self._heavy.generate(prompt, **kw)
            except Exception as exc:
                logger.info("[Tiered] heavy generate failed (%s) -> light", exc)
                if self._light is None:
                    raise
        if self._light is not None:
            return await self._light.generate(prompt, **kw)
        raise RuntimeError("tiered_prime_client: no tier available")

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
