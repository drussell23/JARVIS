# backend/core/lifecycle_engine.py
"""
JARVIS Lifecycle Engine v1.0
=============================
Unified DAG-driven lifecycle management for all components — in-process,
subprocess, and remote.

Provides:
  - Component state machine with journaled transitions
  - Wave-based parallel execution (Kahn's algorithm)
  - Failure propagation (hard deps skip/drain, soft deps degrade)
  - Reverse-DAG shutdown with drain contracts

Design doc: docs/plans/2026-02-24-cross-repo-control-plane-design.md
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import (
    Awaitable, Callable, Dict, List, Optional, Protocol, Set, Tuple,
    runtime_checkable,
)

from backend.core.orchestration_journal import OrchestrationJournal, StaleEpochError

logger = logging.getLogger("jarvis.lifecycle_engine")


# ── Enums & Constants ───────────────────────────────────────────────

class ComponentLocality(Enum):
    IN_PROCESS = "in_process"
    SUBPROCESS = "subprocess"
    REMOTE = "remote"


VALID_TRANSITIONS: Dict[str, Set[str]] = {
    "REGISTERED":   {"STARTING"},
    "STARTING":     {"HANDSHAKING", "FAILED"},
    "HANDSHAKING":  {"READY", "FAILED"},
    "READY":        {"DEGRADED", "DRAINING", "FAILED", "LOST"},
    "DEGRADED":     {"READY", "DRAINING", "FAILED", "LOST"},
    "DRAINING":     {"STOPPING", "FAILED", "LOST"},
    "STOPPING":     {"STOPPED", "FAILED"},
    "FAILED":       {"STARTING"},
    "LOST":         {"STARTING", "STOPPED"},
    "STOPPED":      {"STARTING"},
}


# ── Exceptions ──────────────────────────────────────────────────────

class InvalidTransitionError(Exception):
    """Raised when a state transition violates the state machine."""
    pass


class CyclicDependencyError(Exception):
    """Raised when the component dependency graph contains a cycle."""
    pass


# ── Data Model ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComponentDeclaration:
    """Static declaration of a component in the lifecycle DAG."""
    name: str
    locality: ComponentLocality
    dependencies: Tuple[str, ...] = ()
    soft_dependencies: Tuple[str, ...] = ()
    is_critical: bool = False
    start_timeout_s: float = 60.0
    handshake_timeout_s: float = 10.0
    drain_timeout_s: float = 30.0
    heartbeat_ttl_s: float = 30.0
    spawn_command: Optional[Tuple[str, ...]] = None
    endpoint: Optional[str] = None
    health_path: str = "/health"
    init_fn: Optional[str] = None


@runtime_checkable
class LocalityDriver(Protocol):
    """How to start/stop/health-check a component by locality."""
    async def start(self, component_name: str, config: Optional[dict] = None) -> bool: ...
    async def stop(self, component_name: str) -> bool: ...
    async def health_check(self, component_name: str) -> dict: ...
    async def send_drain(self, component_name: str, timeout_s: float = 30.0) -> bool: ...


# ── Wave Computation ────────────────────────────────────────────────

def compute_waves(
    components: Tuple[ComponentDeclaration, ...],
) -> List[List[ComponentDeclaration]]:
    """Compute parallel execution waves via Kahn's topological sort.

    Only hard dependencies affect ordering. Soft dependencies are ignored.
    Components in the same wave can start concurrently.
    """
    comp_map: Dict[str, ComponentDeclaration] = {c.name: c for c in components}
    all_names = set(comp_map.keys())

    in_degree: Dict[str, int] = {name: 0 for name in all_names}
    dependents: Dict[str, List[str]] = {name: [] for name in all_names}

    for c in components:
        for dep in c.dependencies:
            if dep in all_names:
                in_degree[c.name] += 1
                dependents[dep].append(c.name)

    waves: List[List[ComponentDeclaration]] = []
    queue = sorted([n for n, d in in_degree.items() if d == 0])

    processed = 0
    while queue:
        wave = [comp_map[name] for name in queue]
        waves.append(wave)
        processed += len(queue)

        next_queue = []
        for name in queue:
            for dep_name in dependents[name]:
                in_degree[dep_name] -= 1
                if in_degree[dep_name] == 0:
                    next_queue.append(dep_name)
        queue = sorted(next_queue)

    if processed < len(all_names):
        remaining = [n for n, d in in_degree.items() if d > 0]
        raise CyclicDependencyError(f"Cycle detected involving: {remaining}")

    return waves


def _make_idempotency_key(
    action: str, target: str, trigger_seq: Optional[int] = None,
) -> str:
    """Generate epoch-independent idempotency key."""
    if trigger_seq is not None:
        return f"{action}:{target}:triggered_by:{trigger_seq}"
    import uuid
    return f"{action}:{target}:{uuid.uuid4().hex}"


# ── Lifecycle Engine ────────────────────────────────────────────────

class LifecycleEngine:
    """Unified lifecycle management for all components.

    Usage:
        engine = LifecycleEngine(journal, SYSTEM_COMPONENTS)
        engine.register_locality_driver(ComponentLocality.IN_PROCESS, driver)
        await engine.start_all()
        await engine.shutdown_all("user_request")
    """

    def __init__(
        self,
        journal: OrchestrationJournal,
        components: Tuple[ComponentDeclaration, ...],
    ):
        self._journal = journal
        self._components = {c.name: c for c in components}
        self._statuses: Dict[str, str] = {c.name: "REGISTERED" for c in components}
        self._drivers: Dict[ComponentLocality, LocalityDriver] = {}
        self._drain_hooks: Dict[str, Callable] = {}
        self._event_callbacks: List[Callable] = []

    # ── Status ──────────────────────────────────────────────────

    def get_status(self, component: str) -> str:
        return self._statuses.get(component, "REGISTERED")

    def get_all_statuses(self) -> Dict[str, str]:
        return dict(self._statuses)

    def get_declaration(self, component: str) -> Optional[ComponentDeclaration]:
        return self._components.get(component)

    # ── Registration ────────────────────────────────────────────

    def register_locality_driver(
        self, locality: ComponentLocality, driver: LocalityDriver,
    ) -> None:
        self._drivers[locality] = driver

    def register_drain_hook(
        self, component: str, hook: Callable[[], Awaitable[None]],
    ) -> None:
        self._drain_hooks[component] = hook

    def on_transition(self, callback: Callable) -> None:
        """Register callback for state transitions."""
        self._event_callbacks.append(callback)

    # ── State Transitions ───────────────────────────────────────

    async def transition_component(
        self,
        component: str,
        new_status: str,
        *,
        reason: str,
        trigger_seq: Optional[int] = None,
    ) -> int:
        """Transition a component with journal + validation."""
        current = self._statuses.get(component, "REGISTERED")

        allowed = VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"{component}: {current} -> {new_status} not valid "
                f"(allowed: {allowed})"
            )

        idemp_key = _make_idempotency_key(
            f"transition_{new_status}", component, trigger_seq,
        )

        seq = self._journal.fenced_write(
            "state_transition", component,
            idempotency_key=idemp_key,
            payload={"from": current, "to": new_status, "reason": reason},
        )

        self._statuses[component] = new_status

        self._journal.update_component_state(
            component, new_status, seq,
        )

        for cb in self._event_callbacks:
            try:
                cb(component, current, new_status, reason)
            except Exception as e:
                logger.warning("[Engine] Transition callback error: %s", e)

        return seq

    # ── Failure Propagation ─────────────────────────────────────

    async def propagate_failure(
        self,
        failed_component: str,
        failure_type: str,
    ) -> None:
        """Propagate failure to dependents."""
        for name, comp in self._components.items():
            current = self._statuses.get(name, "REGISTERED")

            if failed_component in comp.dependencies:
                # Hard dependency
                if failure_type == "failed":
                    if current == "REGISTERED":
                        # REGISTERED -> FAILED not direct; go through STARTING first
                        await self.transition_component(
                            name, "STARTING",
                            reason=f"hard_dep_{failed_component}_cascade",
                        )
                        await self.transition_component(
                            name, "FAILED",
                            reason=f"hard_dep_{failed_component}_{failure_type}",
                        )
                    elif current == "STARTING":
                        await self.transition_component(
                            name, "FAILED",
                            reason=f"hard_dep_{failed_component}_{failure_type}",
                        )
                elif failure_type == "lost":
                    if current in ("READY", "DEGRADED"):
                        await self.transition_component(
                            name, "DRAINING",
                            reason=f"hard_dep_{failed_component}_lost",
                        )

            elif failed_component in comp.soft_dependencies:
                # Soft dependency
                if current == "READY":
                    await self.transition_component(
                        name, "DEGRADED",
                        reason=f"soft_dep_{failed_component}_{failure_type}",
                    )

    # ── Wave Execution ──────────────────────────────────────────

    async def start_all(self) -> bool:
        """Start all components in wave order. Returns True if no critical failure."""
        waves = compute_waves(tuple(self._components.values()))

        for wave_idx, wave in enumerate(waves):
            logger.info("[Engine] Starting wave %d: %s",
                        wave_idx, [c.name for c in wave])

            tasks = []
            for comp in wave:
                # Check hard deps
                deps_ok = all(
                    self._statuses.get(d) == "READY"
                    for d in comp.dependencies
                )
                if not deps_ok:
                    failed_deps = [
                        d for d in comp.dependencies
                        if self._statuses.get(d) != "READY"
                    ]
                    await self.transition_component(
                        comp.name, "STARTING", reason="wave_start",
                    )
                    await self.transition_component(
                        comp.name, "FAILED",
                        reason=f"dependency_not_ready: {failed_deps}",
                    )
                    continue

                tasks.append(self._start_single(comp))

            await asyncio.gather(*tasks, return_exceptions=True)

            # Check for critical failures in this wave
            for comp in wave:
                if comp.is_critical and self._statuses.get(comp.name) == "FAILED":
                    logger.error("[Engine] Critical component %s failed. Aborting.", comp.name)
                    return False

        return True

    async def _start_single(self, comp: ComponentDeclaration) -> None:
        """Start a single component through the lifecycle."""
        try:
            await self.transition_component(comp.name, "STARTING", reason="wave_start")

            driver = self._drivers.get(comp.locality)
            if driver:
                await asyncio.wait_for(
                    driver.start(comp.name),
                    timeout=comp.start_timeout_s,
                )

            # Transition to HANDSHAKING (handshake manager will handle the rest)
            await self.transition_component(
                comp.name, "HANDSHAKING", reason="start_complete",
            )
        except asyncio.TimeoutError:
            await self.transition_component(
                comp.name, "FAILED",
                reason=f"start_timeout_{comp.start_timeout_s}s",
            )
        except Exception as e:
            await self.transition_component(
                comp.name, "FAILED", reason=f"start_error: {e}",
            )

    # ── Shutdown ────────────────────────────────────────────────

    async def shutdown_all(self, reason: str) -> None:
        """Graceful shutdown in reverse dependency order with drain."""
        shutdown_seq = self._journal.fenced_write(
            "shutdown_initiated", "control_plane",
            payload={"reason": reason},
        )

        waves = compute_waves(tuple(self._components.values()))
        reverse_waves = list(reversed(waves))

        for wave_idx, wave in enumerate(reverse_waves):
            active = [
                c for c in wave
                if self._statuses.get(c.name)
                in ("READY", "DEGRADED", "STARTING", "HANDSHAKING")
            ]
            if not active:
                continue

            # Phase 1: DRAINING
            drain_tasks = []
            for comp in active:
                drain_tasks.append(
                    self._drain_single(comp, shutdown_seq)
                )
            await asyncio.gather(*drain_tasks, return_exceptions=True)

            # Phase 2: STOPPING
            stop_tasks = []
            for comp in active:
                stop_tasks.append(
                    self._stop_single(comp, shutdown_seq)
                )
            await asyncio.gather(*stop_tasks, return_exceptions=True)

    async def _drain_single(
        self, comp: ComponentDeclaration, trigger_seq: int,
    ) -> None:
        current = self._statuses.get(comp.name, "REGISTERED")
        if current not in VALID_TRANSITIONS or "DRAINING" not in VALID_TRANSITIONS.get(current, set()):
            return

        await self.transition_component(
            comp.name, "DRAINING",
            reason="shutdown_requested",
            trigger_seq=trigger_seq,
        )

        drain_hook = self._drain_hooks.get(comp.name)
        if drain_hook:
            try:
                await asyncio.wait_for(drain_hook(), timeout=comp.drain_timeout_s)
            except asyncio.TimeoutError:
                logger.warning("[Engine] %s drain timed out", comp.name)
            except Exception as e:
                logger.warning("[Engine] %s drain error: %s", comp.name, e)

    async def _stop_single(
        self, comp: ComponentDeclaration, trigger_seq: int,
    ) -> None:
        current = self._statuses.get(comp.name, "REGISTERED")
        if current not in VALID_TRANSITIONS or "STOPPING" not in VALID_TRANSITIONS.get(current, set()):
            return

        await self.transition_component(
            comp.name, "STOPPING",
            reason="drain_complete",
            trigger_seq=trigger_seq,
        )

        driver = self._drivers.get(comp.locality)
        if driver:
            try:
                await asyncio.wait_for(driver.stop(comp.name), timeout=10.0)
            except Exception as e:
                logger.warning("[Engine] %s stop error: %s", comp.name, e)

        await self.transition_component(
            comp.name, "STOPPED",
            reason="terminated",
            trigger_seq=trigger_seq,
        )

    # ── Recovery ────────────────────────────────────────────────

    async def recover_from_journal(self) -> None:
        """Rebuild state from journal and reconcile with reality."""
        states = self._journal.get_all_component_states()

        for name, state in states.items():
            if name in self._statuses:
                self._statuses[name] = state["status"]
                logger.info(
                    "[Engine] Recovered %s -> %s from journal",
                    name, state["status"],
                )
