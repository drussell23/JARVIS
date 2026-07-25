"""Keep the delegated agent's heavy turn off the addressed agent's lane.

The real constraint
-------------------
A dual summon — "Hey JARVIS, ask Karen to verify the deployment" — puts two
agents in flight at once. The collision is NOT local memory: nothing in the
conversation path runs a local model. Generation already leaves this machine
through the established chain::

    Tier 0  DoubleWord 397B   primary    — tokens
    Tier 1  Claude            fallback   — time (lowest TTFT)
    Tier 2  J-Prime (GCP)     sovereign  — ours, heavier, slower to reach

The collision is LANE CONTENTION. A spoken turn is routed IMMEDIATE by
policy — Claude direct, skip DW — precisely because the operator is waiting
in real time. If the secondary's delegated work is dispatched the same way,
two IMMEDIATE calls compete for the one lane that exists to be fast, the
primary's first token arrives behind the secondary's prefill, and the
operator hears the addressed agent hesitate on behalf of work they did not
ask it to do. Cost doubles at the tier that costs the most, for the half of
the work with no human waiting on it.

So this arbitrates LANES, not machines
--------------------------------------
The primary keeps IMMEDIATE. The secondary is placed on a different tier
according to what it was actually asked to do, and dispatched as a task so
the primary's event loop never waits on it:

    heavy / architectural        -> COMPLEX     (Claude plans, DW executes)
    ordinary delegated work      -> STANDARD    (DW primary, Claude fallback)
    fire-and-forget, no reply    -> BACKGROUND  (DW only, no Claude fallback)
    pinned to this organism      -> SOVEREIGN   (J-Prime on GCP)

``ProviderRoute`` is imported rather than re-declared: the vocabulary belongs
to :mod:`urgency_router`, and a second copy of those five names would drift
from the routing policy it is supposed to mirror.

Why the transport is a stub
---------------------------
What must be correct before a tier is worth pointing at is the DECISION, the
isolation and the fallback — not the socket. Each tier is an injectable
transport; none is wired by default, and an unwired tier degrades to the next
one down, ending at the primary's own path. Remote compute is an
optimisation, and an optimisation whose absence breaks the assistant is a
liability.

⚠️ J-Prime note: spinning the GCP tier COSTS money and
``JARVIS_FAILOVER_LIFECYCLE_ENABLED`` can auto-start the instance. This
module therefore never spins anything — it dispatches to a transport that
already exists, or falls back.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

try:  # the vocabulary lives in the router; never re-declare it
    from backend.core.ouroboros.governance.urgency_router import ProviderRoute
except ImportError:  # pragma: no cover - router always present in-tree
    class ProviderRoute(str, enum.Enum):       # type: ignore[no-redef,misc]
        IMMEDIATE = "immediate"
        STANDARD = "standard"
        COMPLEX = "complex"
        BACKGROUND = "background"
        SPECULATIVE = "speculative"


def dispatcher_enabled() -> bool:
    """Default ON: unlike a remote endpoint, lane separation costs nothing and
    its absence is the bug. OFF puts the secondary back on the primary's lane,
    which is the behaviour this replaces."""
    return os.getenv(
        "JARVIS_DELEGATION_DISPATCH_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def dispatch_timeout_s() -> float:
    """Ceiling on ONE delegated turn. Past this the operator has moved on and
    the answer is no longer worth the tokens it is still spending."""
    return _env_float("JARVIS_DELEGATION_TIMEOUT_S", 120.0, 5.0)


class Tier(str, enum.Enum):
    """Where the work runs. Ordered by how far it is from the operator."""

    DOUBLEWORD = "doubleword"     # Tier 0 — tokens
    CLAUDE = "claude"             # Tier 1 — time
    JPRIME = "jprime"             # Tier 2 — sovereign, GCP golden image
    CALLER = "caller"             # ran on the path the caller supplied


#: Which tier a route lands on FIRST. Mirrors the documented chain: IMMEDIATE
#: skips DW for latency; BACKGROUND refuses Claude for cost; SOVEREIGN work
#: goes to the instance that is ours.
_ROUTE_TIER: Dict[str, Tier] = {
    "immediate": Tier.CLAUDE,
    "standard": Tier.DOUBLEWORD,
    "complex": Tier.CLAUDE,
    "background": Tier.DOUBLEWORD,
    "speculative": Tier.DOUBLEWORD,
}

#: Verbs and nouns that mark work as heavy enough to plan before executing.
#: Structural, not a keyword list masquerading as intelligence: these are the
#: operations that touch many files or take minutes, and the distinction they
#: drive is only WHICH remote tier — never whether the work happens.
_HEAVY_MARKERS = frozenset("""
refactor migrate rewrite redesign architect audit benchmark profile
deploy rollout release upgrade backfill reindex retrain
""".split())

#: Work that must run against THIS organism's state — the screen, the
#: microphone, the working tree. Not a performance judgement: a remote tier
#: literally cannot see these.
_PINNED_MARKERS = frozenset("""
screen screenshot microphone mic camera clipboard desktop window
worktree uncommitted staged local
""".split())


@dataclass(frozen=True)
class Workload:
    """One delegated turn."""

    agent: str
    task: str
    #: Set when the caller already knows — e.g. arbitration saw no reply was
    #: expected. Otherwise inferred from the task.
    route: Optional[ProviderRoute] = None
    #: Absolute. Honoured before any tier preference.
    pinned_to_node: bool = False


@dataclass(frozen=True)
class LaneDecision:
    route: ProviderRoute
    tier: Tier
    reason: str

    @property
    def shares_primary_lane(self) -> bool:
        """True when the secondary would compete with the addressed agent.
        The whole point of arbitration is that this is False."""
        return self.route == ProviderRoute.IMMEDIATE


@dataclass
class DispatchResult:
    """What happened, in enough detail to explain a latency spike later."""

    value: Any = None
    tier: Tier = Tier.CALLER
    route: str = ""
    reason: str = ""
    elapsed_s: float = 0.0
    fell_back: bool = False
    error: str = ""
    decision: Optional[LaneDecision] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def _words(text: str) -> set:
    return {w.strip(".,;:!?").lower() for w in str(text or "").split()}


def decide_lane(workload: Workload) -> LaneDecision:
    """Which lane the SECONDARY gets. Pure, synchronous, sub-millisecond.

    Absolute constraints first, preferences after — pinned work can never be
    shipped off-node however heavy it looks."""
    if workload.pinned_to_node:
        return LaneDecision(ProviderRoute.STANDARD, Tier.JPRIME, "pinned_to_node")

    words = _words(workload.task)
    if words & _PINNED_MARKERS:
        return LaneDecision(
            ProviderRoute.STANDARD, Tier.JPRIME, "task_touches_local_state",
        )

    if workload.route is not None:
        route = workload.route
        reason = "caller_stamped"
    elif words & _HEAVY_MARKERS:
        route, reason = ProviderRoute.COMPLEX, "heavy_task"
    elif not workload.task.strip():
        # Nothing was actually delegated — do not spend a lane on it.
        route, reason = ProviderRoute.BACKGROUND, "empty_task"
    else:
        route, reason = ProviderRoute.STANDARD, "delegated_default"

    if route == ProviderRoute.IMMEDIATE and not workload.pinned_to_node:
        # The addressed agent owns IMMEDIATE. A delegated turn taking it is
        # the contention this module exists to prevent, so it is demoted even
        # when the caller asked for it.
        route, reason = ProviderRoute.STANDARD, "demoted_from_primary_lane"

    return LaneDecision(route, _ROUTE_TIER.get(route.value, Tier.DOUBLEWORD), reason)


class RemoteComputeDispatcher:
    """Runs a delegated workload on a lane the primary is not using.

    Transports are injected per tier so the arbitration can be exercised
    without contacting anything, and so wiring a real tier later is a
    parameter rather than an edit here."""

    def __init__(
        self,
        transports: Optional[Dict[Tier, Callable[[Workload], Awaitable[Any]]]] = None,
    ) -> None:
        self._transports = dict(transports or {})
        self.dispatches: list = []          # observability + tests

    def register(
        self, tier: Tier, transport: Callable[[Workload], Awaitable[Any]],
    ) -> None:
        self._transports[tier] = transport

    async def _run_tier(self, tier: Tier, workload: Workload) -> Any:
        transport = self._transports.get(tier)
        if transport is None:
            raise TierUnavailable(f"{tier.value} has no transport wired")
        return await transport(workload)

    async def dispatch(
        self,
        workload: Workload,
        fallback: Callable[[], Awaitable[Any]],
    ) -> DispatchResult:
        """Run *workload* on its arbitrated lane, else fall back.

        *fallback* is the caller's own path — in practice the same generation
        the primary would have used. Reached only when no remote tier is
        wired or every wired one failed.

        NEVER raises: a delegated turn that failed is a RESULT. Propagating it
        would unwind the conversation loop for work the operator delegated,
        not for work they asked the primary to do."""
        started = time.monotonic()
        decision = decide_lane(workload)
        self.dispatches.append((workload.agent, decision.route.value, decision.tier.value))

        if not dispatcher_enabled():
            value, err = await self._safe(fallback)
            return DispatchResult(
                value=value, tier=Tier.CALLER, route="", reason="dispatch_disabled",
                elapsed_s=time.monotonic() - started, error=err,
            )

        # Preference order: the arbitrated tier, then the rest of the chain in
        # the documented order, then the caller's path. Each step is bounded.
        chain = [decision.tier] + [
            t for t in (Tier.DOUBLEWORD, Tier.CLAUDE, Tier.JPRIME)
            if t is not decision.tier
        ]
        errors = []
        for tier in chain:
            if tier not in self._transports:
                continue
            try:
                value = await asyncio.wait_for(
                    self._run_tier(tier, workload), timeout=dispatch_timeout_s(),
                )
                return DispatchResult(
                    value=value, tier=tier, route=decision.route.value,
                    reason=decision.reason, elapsed_s=time.monotonic() - started,
                    fell_back=tier is not decision.tier, decision=decision,
                )
            except asyncio.CancelledError:
                raise
            except (TierUnavailable, asyncio.TimeoutError, OSError,
                    ConnectionError, ValueError, RuntimeError) as exc:
                errors.append(f"{tier.value}:{exc}")
                logger.info(
                    "[Delegation] %s tier %s unavailable — trying the next: %s",
                    workload.agent or "?", tier.value, exc,
                )

        value, err = await self._safe(fallback)
        return DispatchResult(
            value=value, tier=Tier.CALLER, route=decision.route.value,
            reason=f"{decision.reason}->caller_path",
            elapsed_s=time.monotonic() - started, fell_back=True,
            error=err or "; ".join(errors), decision=decision,
        )

    @staticmethod
    async def _safe(fn: Callable[[], Awaitable[Any]]) -> "tuple[Any, str]":
        """The caller's path is arbitrary code; its failure is reported, never
        propagated into the conversation loop."""
        try:
            return await fn(), ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reported, see docstring
            return None, str(exc)

    def spawn(
        self,
        workload: Workload,
        fallback: Callable[[], Awaitable[Any]],
    ) -> "asyncio.Task[DispatchResult]":
        """Start the delegated turn and return IMMEDIATELY.

        This is the shape the dual summon uses: the primary acknowledges the
        operator while the secondary's turn is already running on another
        lane. Awaiting the task later schedules the secondary's REPLY — never
        its computation."""
        return asyncio.get_running_loop().create_task(
            self.dispatch(workload, fallback),
            name=f"delegated-{workload.agent or 'agent'}",
        )


class TierUnavailable(RuntimeError):
    """A tier could not be used. Typed, so the chain catches THIS and a real
    defect in a wired transport still surfaces."""


_DISPATCHER: Optional[RemoteComputeDispatcher] = None


def get_dispatcher() -> RemoteComputeDispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = RemoteComputeDispatcher()
    return _DISPATCHER


def reset_dispatcher() -> None:
    """Test seam."""
    global _DISPATCHER
    _DISPATCHER = None


__all__ = [
    "DispatchResult",
    "LaneDecision",
    "ProviderRoute",
    "RemoteComputeDispatcher",
    "Tier",
    "TierUnavailable",
    "Workload",
    "decide_lane",
    "dispatcher_enabled",
    "get_dispatcher",
    "reset_dispatcher",
]
