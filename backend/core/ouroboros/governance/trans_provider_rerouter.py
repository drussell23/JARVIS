"""Sub-Agent Trans-Provider Re-Router — isolate worker failure boundaries.

A 4-agent Swarm must NOT die because ONE sub-agent's provider degrades mid-flight.
The root cause of multi-agent fragility is FATE-SHARING across independent worker
tasks: a ``StreamRuptureError`` propagating out of one agent aborts the whole
``asyncio.gather``. This wraps each sub-agent's ``agent_fn`` so that a mid-ReAct
transport collapse re-routes ONLY that agent's payload to the fallback provider
tier — the completed work of sibling agents is preserved.

  * A ``StreamRuptureError`` (or a DEGRADED-class transport fault) from the
    PRIMARY tier trips a re-route to the FALLBACK tier for THAT symbol only.
  * The re-route is STICKY per symbol: once an agent has fallen back, its
    remaining ReAct turns go straight to the fallback (no repeated primary
    burns on a known-degraded lane).
  * A non-transport error propagates unchanged (the ReAct loop's own handling).

DRY: consumes the fallback ``agent_fn`` the caller resolves from the existing
``FailoverLifecycle`` matrix (#70016 DW → J-Prime → Claude) — it does NOT
duplicate failover STATE, only routes onto the already-resolved tier. Per-symbol
isolation means one shared instance safely serves every agent in the swarm.
Never raises except to re-propagate a genuinely non-transport fault.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.stream_rupture import StreamRuptureError

logger = logging.getLogger("Ouroboros.TransProviderReRouter")

# agent_fn(target, feedback) -> Awaitable[str]
AgentFn = Callable[..., Awaitable[str]]
DegradedPredicate = Callable[[BaseException], bool]

_DEGRADED_MARKERS = (
    "stream_rupture", "upstream_error", "degraded", "no_tokens",
    "connection reset", "connection aborted", "gateway timeout",
    "service unavailable", "sock_read", "timeouterror",
)


def _default_degraded(exc: BaseException) -> bool:
    """Is *exc* a transport-degradation class fault (worth re-routing), as opposed
    to a logic error (which must propagate)? Conservative — only known markers."""
    if isinstance(exc, StreamRuptureError):
        return True
    if isinstance(exc, (TimeoutError,)):
        return True
    msg = f"{type(exc).__name__}:{exc}".lower()
    return any(m in msg for m in _DEGRADED_MARKERS)


class TransProviderReRouter:
    """A drop-in ``agent_fn`` that re-routes a single sub-agent onto the fallback
    provider tier on transport collapse — sibling agents untouched."""

    def __init__(
        self,
        primary: AgentFn,
        fallback: AgentFn,
        *,
        degraded_predicate: Optional[DegradedPredicate] = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._degraded = degraded_predicate or _default_degraded
        self._rerouted: Set[str] = set()          # sticky per symbol
        self._reroutes: List[Tuple[str, str]] = []

    @property
    def reroutes(self) -> List[Tuple[str, str]]:
        """Audit trail of ``(symbol, reason)`` re-routes."""
        return list(self._reroutes)

    def was_rerouted(self, symbol: str) -> bool:
        return symbol in self._rerouted

    async def __call__(self, target, feedback: str = "") -> str:
        symbol = getattr(target, "symbol", "") or ""
        # Sticky: a known-degraded symbol skips the primary entirely.
        if symbol in self._rerouted:
            return await self._fallback(target, feedback)
        try:
            return await self._primary(target, feedback)
        except StreamRuptureError as exc:
            self._trip(symbol, f"stream_rupture:{getattr(exc, 'phase', '?')}")
            return await self._fallback(target, feedback)
        except Exception as exc:  # noqa: BLE001
            if self._degraded(exc):
                self._trip(symbol, f"degraded:{type(exc).__name__}")
                return await self._fallback(target, feedback)
            raise  # a genuine logic error — the ReAct loop owns it

    def _trip(self, symbol: str, reason: str) -> None:
        self._rerouted.add(symbol)
        self._reroutes.append((symbol, reason))
        logger.warning(
            "[TransProviderReRouter] sub-agent '%s' transport collapse (%s) — "
            "re-routing to fallback tier; sibling agents UNAFFECTED",
            symbol, reason,
        )


def build_rerouter(
    primary: AgentFn, fallback: AgentFn,
    *, degraded_predicate: Optional[DegradedPredicate] = None,
) -> TransProviderReRouter:
    """Compose a re-router from a primary + a fallback ``agent_fn``. In production
    the caller builds each from a ``ProductionAgentTurnFn`` bound to the primary
    (DW) and the ``FailoverLifecycle``-resolved fallback client respectively."""
    return TransProviderReRouter(primary, fallback, degraded_predicate=degraded_predicate)


__all__ = [
    "AgentFn",
    "TransProviderReRouter",
    "build_rerouter",
]
