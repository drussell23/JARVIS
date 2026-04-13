"""Process-lifetime state for Ouroboros governance subsystems.

This module is QUARANTINED from ``ModuleHotReloader`` — its objects back
live ``asyncio`` primitives, finite-state machines, and monotonic
counters that must survive ``importlib.reload()`` of the behavior
modules that consume them.

Design principle
----------------
**State is persistent, Behavior is hot.** The governance behavior
modules (``orchestrator``, ``candidate_generator``, ``providers``) hold
no reload-hostile state of their own — every mutable field that must
cross a reload boundary lives here, in a dataclass that is allocated
once per process and looked up by name on every ``__init__``.

Rollout (Phase 1 of the un-quarantine blueprint)
------------------------------------------------
- **3A (this file):** ``GeneratorState`` — CandidateGenerator's
  semaphores, ``FailbackStateMachine``, background poll tasks, Tier 0
  skip counters, exhaustion event counter, and latency tracker. Opted
  in via ``JARVIS_UNQUARANTINE_GENERATOR=true``; default is the legacy
  per-instance path so nothing moves on battle-test day 0.
- **3B (future):** ``ProviderState`` — Claude/Prime/DW client refs,
  daily spend, client-generation counter, recycle events, cascade
  attempts, prompt-cache stats.
- **3C (future):** ``OrchestratorState`` — cost governor, session
  lessons, forward-progress / productivity detectors, RSI trackers,
  oracle update lock.

How to add a new hoisted state root
-----------------------------------
1. Define a new ``@dataclass`` in this file.
2. Add a ``get_*_state()`` lazy-init function with a module-level
   singleton guarded by ``_lock``.
3. Extend ``reset_for_tests()`` to clear the new singleton so tests do
   not leak primitives across cases.
4. The consumer calls ``get_*_state()`` inside ``__init__`` only when
   its feature flag is enabled; otherwise it mints a fresh state via
   ``*State.fresh(...)`` — same dataclass, fresh primitives.
"""
from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .candidate_generator import FailbackStateMachine
    from .dw_latency_tracker import DwLatencyTracker


UNQUARANTINE_GENERATOR_ENV = "JARVIS_UNQUARANTINE_GENERATOR"


def unquarantine_generator_enabled() -> bool:
    """Return True when ``JARVIS_UNQUARANTINE_GENERATOR`` is truthy.

    Default false. Flipping to true routes ``CandidateGenerator`` state
    through the process-lifetime singleton so the generator can survive
    ``importlib.reload(candidate_generator)``.
    """
    raw = os.environ.get(UNQUARANTINE_GENERATOR_ENV, "false").strip().lower()
    return raw in ("true", "1", "yes", "on")


@dataclass
class GeneratorCounters:
    """Monotonic counters for CandidateGenerator that must cross reloads.

    Kept as a dataclass (not bare ints on ``GeneratorState``) so the
    consumer can bind a single reference — ``self._counters =
    state.counters`` — and mutate via attribute access. Aliasing an
    ``int`` would silently rebind the generator's local copy while
    leaving the singleton at its old value, defeating the hoist.
    """

    exhaustion_events: int = 0
    consecutive_tier0_failures: int = 0
    last_tier0_failure_at: float = 0.0


@dataclass
class GeneratorState:
    """Persistent state container for ``CandidateGenerator``.

    Every field here must survive ``importlib.reload()`` of
    ``candidate_generator``. If you find yourself needing to add a
    mutable field to ``CandidateGenerator`` that cannot tolerate being
    reset on reload, add it here instead and route access through
    ``self._state``.
    """

    primary_sem: asyncio.Semaphore
    fallback_sem: asyncio.Semaphore
    fsm: "FailbackStateMachine"
    latency_tracker: "DwLatencyTracker"
    completed_batches: Dict[str, Any] = field(default_factory=dict)
    background_polls: Dict[str, "asyncio.Task[Any]"] = field(default_factory=dict)
    counters: GeneratorCounters = field(default_factory=GeneratorCounters)

    @classmethod
    def fresh(
        cls,
        *,
        primary_concurrency: int,
        fallback_concurrency: int,
        latency_tracker: Optional["DwLatencyTracker"] = None,
    ) -> "GeneratorState":
        """Mint a new ``GeneratorState`` with fresh primitives.

        Used by two paths:

        1. The legacy per-instance path inside ``CandidateGenerator.__init__``
           when ``JARVIS_UNQUARANTINE_GENERATOR`` is false — preserves the
           pre-hoist behavior bit-for-bit.
        2. Tests that want isolated state without touching the singleton.

        Deferred imports on both dependencies sidestep the circular
        import with ``candidate_generator`` (which imports
        ``GeneratorState``) and keep this module's top level stdlib-only.
        """
        from .candidate_generator import FailbackStateMachine
        from .dw_latency_tracker import get_default_tracker

        return cls(
            primary_sem=asyncio.Semaphore(primary_concurrency),
            fallback_sem=asyncio.Semaphore(fallback_concurrency),
            fsm=FailbackStateMachine(),
            latency_tracker=latency_tracker or get_default_tracker(),
        )


_lock = threading.Lock()
_generator_state: Optional[GeneratorState] = None


def get_generator_state(
    *,
    primary_concurrency: int = 4,
    fallback_concurrency: int = 2,
    latency_tracker: Optional["DwLatencyTracker"] = None,
) -> GeneratorState:
    """Return the process-wide ``GeneratorState`` singleton.

    First call wins — subsequent calls return the existing instance
    regardless of their arguments, so semaphore slots, FSM dwell
    timers, and the exhaustion counter stay stable across
    ``importlib.reload(candidate_generator)``. If the caller needs a
    different concurrency for testing, it must either disable the
    singleton path (``JARVIS_UNQUARANTINE_GENERATOR=false``) or call
    ``reset_for_tests()`` first.
    """
    global _generator_state
    with _lock:
        if _generator_state is None:
            _generator_state = GeneratorState.fresh(
                primary_concurrency=primary_concurrency,
                fallback_concurrency=fallback_concurrency,
                latency_tracker=latency_tracker,
            )
        return _generator_state


def _is_test_mode() -> bool:
    """True under ``JARVIS_TEST_MODE=true`` or an active pytest session.

    ``PYTEST_CURRENT_TEST`` is set by pytest on every test function
    invocation, so this flips on automatically in unit tests without
    requiring the env-var to be plumbed through.
    """
    if os.environ.get("JARVIS_TEST_MODE", "").strip().lower() in ("true", "1"):
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def reset_for_tests() -> None:
    """Test-only: clear every hoisted state root.

    Must stay in lockstep with the set of state singletons above — when
    Phase 1 lands ``OrchestratorState`` and ``ProviderState``, extend
    this to clear them too. Leaving a singleton un-reset here causes
    semaphores, FSM state, or client pools to bleed across unrelated
    test cases.

    Raises ``RuntimeError`` when called outside test mode so a
    production caller cannot accidentally wipe live state.
    """
    if not _is_test_mode():
        raise RuntimeError(
            "_governance_state.reset_for_tests() called outside test mode. "
            "Set JARVIS_TEST_MODE=true or run under pytest."
        )
    global _generator_state
    with _lock:
        _generator_state = None
