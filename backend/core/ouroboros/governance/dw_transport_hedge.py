"""Slice 188 Phase 1+2 — the proactive transport-hedge (concurrent supremacy).

The paradigm flip: we do NOT react to or predict DW's RT ruptures — we STRUCTURALLY neutralize
them by racing the fast path (RT stream) against the stable path (batch) concurrently and taking
the winner. A rupture on the fast path simply means batch wins; the op NEVER sees the failure.

Phase 1 — ``hedged_race``: fire both, ``asyncio.wait(FIRST_COMPLETED)``, take the first SUCCESS,
aggressively ``cancel()`` the loser (no orphaned tasks / wasted credits). A fast-path rupture is
swallowed so the stable path can still win.

Phase 2 — ``should_skip_race_for_storm``: the cortex is demoted from safety-net to ECONOMIC
governor. Before racing, consult the forecast; if a platform-wide STORM is imminent, racing the
RT path is pure waste (it will rupture) — so bypass it and go batch-only, saving the credits.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional


def transport_hedge_enabled() -> bool:
    """Master for the proactive hedge. Default **FALSE** (§33.1 — new default-path behavior that
    can double-spend; opt-in per deployment). NEVER raises."""
    return os.environ.get("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _storm_threshold() -> float:
    try:
        raw = os.environ.get("JARVIS_DW_STORM_SKIP_THRESHOLD", "").strip()
        v = float(raw) if raw else 0.8
        return min(1.0, max(0.0, v))
    except Exception:  # noqa: BLE001
        return 0.8


def should_skip_race_for_storm(storm_probability: float) -> bool:
    """Phase 2 — the cortex as cost-optimizer. If a platform-wide rupture STORM is forecast above
    threshold, don't waste a hedge racing the RT path (it will rupture) — go batch-only. NEVER
    raises."""
    try:
        return float(storm_probability) >= _storm_threshold()
    except Exception:  # noqa: BLE001
        return False


async def hedged_race(
    fast: Callable[[], Awaitable[Any]],
    stable: Callable[[], Awaitable[Any]],
    *,
    is_rupture: Callable[[BaseException], bool] = lambda e: True,
) -> Any:
    """Race ``fast`` (RT) against ``stable`` (batch). Return the FIRST successful result; cancel
    the loser aggressively. A ``fast`` failure that ``is_rupture`` returns True for is swallowed so
    ``stable`` can still win. If BOTH fail, the last exception is raised. Cancellation is awaited
    so no orphaned tasks survive."""
    loop = asyncio.get_event_loop()
    t_fast = loop.create_task(fast())
    t_stable = loop.create_task(stable())
    pending = {t_fast, t_stable}
    last_exc: Optional[BaseException] = None

    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    result = t.result()
                except asyncio.CancelledError:
                    continue
                except BaseException as exc:  # noqa: BLE001
                    last_exc = exc
                    # a fast-path rupture is non-fatal — let the stable path keep racing
                    if t is t_fast and is_rupture(exc):
                        continue
                    # a non-rupture fast error or a stable error: if the other is still
                    # pending, keep waiting; else propagate below
                    continue
                else:
                    # FIRST SUCCESS — cancel the loser aggressively + await its unwind
                    for other in pending:
                        other.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    return result
        # both finished without a success
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("hedged_race: both transports resolved without a result")
    finally:
        for t in (t_fast, t_stable):
            if not t.done():
                t.cancel()
