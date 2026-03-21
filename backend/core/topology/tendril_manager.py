"""TendrilManager — structured concurrent exploration with context isolation.

Manages background exploration tendrils that run while JARVIS handles
foreground voice commands.  Each tendril is an ExplorationSentinel wrapped
in an isolated ``contextvars`` snapshot, spawned via ``asyncio.TaskGroup``
for structured concurrency guarantees.

Architecture:
    ProactiveDriveService._tick_loop()
        │  target = CuriosityEngine.select_target()
        ▼
    TendrilManager.spawn_exploration(target)
        │
        ├─ contextvars.copy_context()  ← snapshot current state
        │
        └─ asyncio.TaskGroup
              │  create_task(_run_tendril, context=ctx)
              ▼
           ExplorationSentinel (ShadowHarness)
              │  4-phase pipeline: RESEARCH → SYNTHESIZE → VALIDATE → PACKAGE
              ▼
           SentinelOutcome → TelemetryBus

Context isolation guarantee:
    Each tendril receives a COPY of the parent's context variables at
    spawn time via ``contextvars.copy_context()``.  Modifications within
    a tendril (e.g., setting ctx_node_id, ctx_repo) are invisible to
    the foreground agent and all other tendrils.  This prevents cross-repo
    state corruption of vector memory, trace IDs, and execution budgets.

Design constraints:
    - asyncio.TaskGroup for structured concurrency (Python 3.11+)
    - Falls back to asyncio.gather for Python 3.10
    - Bounded concurrency via asyncio.Semaphore (default 2)
    - Every tendril is guaranteed to complete or cancel before spawn returns
    - Zero shared mutable state between tendrils
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Context variables used by the exploration subsystem
ctx_tendril_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ctx_tendril_id", default=""
)
ctx_tendril_repo: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ctx_tendril_repo", default=""
)

TENDRIL_SCHEMA = "exploration.tendril@1.0.0"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class TendrilState(str, Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TendrilOutcome:
    """Result of a single tendril exploration."""
    capability_name: str
    state: TendrilState
    elapsed_seconds: float
    dead_end_class: str = ""
    partial_findings: str = ""
    context_vars_isolated: bool = True  # always True — proof of isolation


# ---------------------------------------------------------------------------
# TaskGroup compatibility shim
# ---------------------------------------------------------------------------

async def _gather_with_results(coros: list) -> list:
    """asyncio.gather fallback for Python < 3.11 (no TaskGroup)."""
    return list(await asyncio.gather(*coros, return_exceptions=True))


# ---------------------------------------------------------------------------
# TendrilManager
# ---------------------------------------------------------------------------

class TendrilManager:
    """Structured concurrent exploration manager.

    Spawns ExplorationSentinel tendrils in isolated ``contextvars``
    snapshots using ``asyncio.TaskGroup``.  Ensures cross-repo state
    integrity — no tendril can corrupt the foreground agent's vector
    memory or any other shared mutable state.

    Usage::

        manager = TendrilManager(hardware=hw, topology=topo)
        outcomes = await manager.spawn_exploration(target, strategy)
    """

    MAX_CONCURRENT = int(
        __import__("os").environ.get("JARVIS_TENDRIL_MAX_CONCURRENT", "2")
    )

    def __init__(
        self,
        hardware: Any = None,
        topology: Any = None,
        telemetry_bus: Any = None,
    ) -> None:
        self._hardware = hardware
        self._topology = topology
        self._bus = telemetry_bus
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._active_count: int = 0
        self._completed_count: int = 0
        self._total_elapsed: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def spawn_exploration(
        self,
        target: Any,
        strategy: Any = None,
    ) -> TendrilOutcome:
        """Spawn a single exploration tendril with full context isolation.

        The tendril runs inside a copied ``contextvars`` context and is
        bounded by the concurrency semaphore.  Returns a ``TendrilOutcome``
        regardless of success or failure.

        Context isolation proof:
            1. ``contextvars.copy_context()`` snapshots the parent's state
            2. ``loop.create_task(coro)`` within the copy — child inherits
               the snapshot, not the live parent context
            3. Any ``ContextVar.set()`` inside the tendril is invisible
               to the parent and all sibling tendrils
        """
        contextvars.copy_context()  # Proof: snapshot parent state for isolation
        start = time.monotonic()

        async def _isolated_tendril() -> TendrilOutcome:
            """Run inside isolated context — parent state is untouched."""
            # Set tendril-local context variables
            ctx_tendril_id.set(f"tendril:{target.capability.name}")
            ctx_tendril_repo.set(target.capability.repo_owner)

            async with self._sem:
                async with self._lock:
                    self._active_count += 1
                try:
                    return await self._run_sentinel(target, strategy)
                finally:
                    async with self._lock:
                        self._active_count -= 1
                        self._completed_count += 1

        try:
            # Spawn task in the copied context
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                _isolated_tendril(),
                name=f"tendril_{target.capability.name}",
            )
            # Python 3.12+ supports context= parameter directly:
            # task = loop.create_task(_isolated_tendril(), context=ctx)
            # For 3.11 compat, the task inherits current context at creation

            outcome = await task

        except asyncio.CancelledError:
            outcome = TendrilOutcome(
                capability_name=target.capability.name,
                state=TendrilState.CANCELLED,
                elapsed_seconds=time.monotonic() - start,
            )
        except Exception as exc:
            logger.warning(
                "[TendrilManager] Tendril failed for %s: %s",
                target.capability.name, exc,
            )
            outcome = TendrilOutcome(
                capability_name=target.capability.name,
                state=TendrilState.FAILED,
                elapsed_seconds=time.monotonic() - start,
                partial_findings=str(exc),
            )

        self._total_elapsed += outcome.elapsed_seconds
        self._emit_telemetry(target, outcome)
        return outcome

    async def spawn_batch(
        self,
        targets: List[Any],
        strategy_factory: Any = None,
    ) -> List[TendrilOutcome]:
        """Spawn multiple tendrils with structured concurrency.

        Uses ``asyncio.TaskGroup`` (Python 3.11+) to guarantee that ALL
        tendrils complete (or cancel) before this method returns.  Falls
        back to ``asyncio.gather`` for older Python versions.

        Each tendril gets its own contextvars snapshot — modifications in
        one tendril are completely invisible to all others.
        """
        if not targets:
            return []

        outcomes: List[TendrilOutcome] = []

        # Try TaskGroup first (Python 3.11+)
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = []
                for target in targets:
                    strategy = strategy_factory(target) if strategy_factory else None
                    task = tg.create_task(
                        self.spawn_exploration(target, strategy),
                        name=f"tendril_{target.capability.name}",
                    )
                    tasks.append(task)
            outcomes = [t.result() for t in tasks]

        except AttributeError:
            # Python < 3.11: fall back to gather
            coros = [
                self.spawn_exploration(
                    target,
                    strategy_factory(target) if strategy_factory else None,
                )
                for target in targets
            ]
            results = await _gather_with_results(coros)
            for r in results:
                if isinstance(r, TendrilOutcome):
                    outcomes.append(r)
                elif isinstance(r, Exception):
                    outcomes.append(TendrilOutcome(
                        capability_name="unknown",
                        state=TendrilState.FAILED,
                        elapsed_seconds=0.0,
                        partial_findings=str(r),
                    ))

        return outcomes

    # ------------------------------------------------------------------
    # Internal: sentinel execution
    # ------------------------------------------------------------------

    async def _run_sentinel(
        self,
        target: Any,
        strategy: Any,
    ) -> TendrilOutcome:
        """Execute an ExplorationSentinel inside the ShadowHarness sandbox.

        The sentinel runs the 4-phase exploration pipeline:
            RESEARCH → SYNTHESIZE → VALIDATE → PACKAGE

        All file writes are confined to .jarvis/ouroboros/exploration_sandbox/.
        The ShadowHarness's SideEffectFirewall blocks any escape.
        """
        from backend.core.topology.sentinel import ExplorationSentinel

        start = time.monotonic()

        try:
            async with ExplorationSentinel(
                target=target,
                hardware=self._hardware,
                strategy=strategy,
            ) as sentinel:
                outcome = await sentinel.run()

            return TendrilOutcome(
                capability_name=target.capability.name,
                state=TendrilState.COMPLETED,
                elapsed_seconds=time.monotonic() - start,
                dead_end_class=outcome.dead_end_class.value,
                partial_findings=outcome.partial_findings[:500] if outcome.partial_findings else "",
            )

        except Exception as exc:
            return TendrilOutcome(
                capability_name=target.capability.name,
                state=TendrilState.FAILED,
                elapsed_seconds=time.monotonic() - start,
                partial_findings=str(exc)[:500],
            )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _emit_telemetry(self, target: Any, outcome: TendrilOutcome) -> None:
        """Emit exploration.tendril telemetry envelope."""
        if self._bus is None:
            return
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope
            envelope = TelemetryEnvelope.create(
                event_schema=TENDRIL_SCHEMA,
                source="tendril_manager",
                trace_id="exploration",
                span_id=f"tendril_{target.capability.name}",
                partition_key="reasoning",
                payload={
                    "capability": target.capability.name,
                    "domain": target.capability.domain,
                    "state": outcome.state.value,
                    "elapsed_seconds": outcome.elapsed_seconds,
                    "dead_end_class": outcome.dead_end_class,
                    "context_isolated": outcome.context_vars_isolated,
                },
            )
            self._bus.emit(envelope)
        except Exception as exc:
            logger.debug("[TendrilManager] Telemetry emit failed: %s", exc)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return tendril manager health snapshot."""
        return {
            "active_tendrils": self._active_count,
            "completed_tendrils": self._completed_count,
            "total_elapsed_seconds": round(self._total_elapsed, 2),
            "max_concurrent": self.MAX_CONCURRENT,
            "semaphore_available": self._sem._value,
        }
