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
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .candidate_generator import FailbackStateMachine
    from .dw_latency_tracker import DwLatencyTracker


UNQUARANTINE_GENERATOR_ENV = "JARVIS_UNQUARANTINE_GENERATOR"
UNQUARANTINE_PROVIDERS_ENV = "JARVIS_UNQUARANTINE_PROVIDERS"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in ("true", "1", "yes", "on")


def unquarantine_generator_enabled() -> bool:
    """Return True when ``JARVIS_UNQUARANTINE_GENERATOR`` is truthy.

    Default false. Flipping to true routes ``CandidateGenerator`` state
    through the process-lifetime singleton so the generator can survive
    ``importlib.reload(candidate_generator)``.
    """
    return _env_truthy(UNQUARANTINE_GENERATOR_ENV)


def unquarantine_providers_enabled() -> bool:
    """Return True when ``JARVIS_UNQUARANTINE_PROVIDERS`` is truthy.

    Default false. Flipping to true routes ``ClaudeProvider``,
    ``PrimeProvider``, and ``DoubleWordProvider`` state through
    process-lifetime singletons so providers can survive
    ``importlib.reload(providers)`` and
    ``importlib.reload(doubleword_provider)``.

    Independent of ``JARVIS_UNQUARANTINE_GENERATOR`` — operators opt in
    to each hoist phase separately during rollout.
    """
    return _env_truthy(UNQUARANTINE_PROVIDERS_ENV)


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


# ---------------------------------------------------------------------------
# Phase 1 Step 3B — Provider state hoist
# ---------------------------------------------------------------------------
#
# The three provider classes (``ClaudeProvider``, ``PrimeProvider``,
# ``DoubleWordProvider``) each get their own dedicated state container and
# singleton. Keeping the split — instead of one fat ``ProviderState`` — lets
# tests reset one provider's state without disturbing siblings, and makes
# future divergence (e.g. DW batch handles vs Claude httpx pools) cheap.
#
# Rebound fields on the provider classes (``_client``, ``_session``,
# ``_recycle_events``, ``_cascade_attempts``, counter ints, etc.) are the
# whole reason this hoist exists — ``self._client = None`` on a stale
# instance post-reload would drift away from the live client if we aliased
# instead of indirecting. The consumers route every read/write through a
# ``@property``/``@setter`` pair on the provider class, so there is no
# instance attribute to shadow the descriptor.
#
# Append-only containers (``cache_stats`` dict mutated via subscript,
# ``DoublewordStats`` dataclass mutated via attribute) are alias-safe —
# the container identity stays put, so binding ``self._cache_stats =
# self._state.cache_stats`` once in ``__init__`` is enough.


@dataclass
class ClaudeProviderCounters:
    """Monotonic counters for ClaudeProvider that must cross reloads.

    Same rationale as :class:`GeneratorCounters` — wrapping the ints in a
    dataclass gives us reference-stable attribute mutation.
    ``budget_reset_date`` uses a default factory so each ``fresh()`` picks
    up the current UTC date at call time (not module-import time).
    """

    daily_spend: float = 0.0
    budget_reset_date: date = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).date()
    )
    client_generation: int = 0


@dataclass
class ClaudeProviderState:
    """Persistent state container for ``ClaudeProvider``.

    ``client`` holds the live ``anthropic.AsyncAnthropic`` — an
    ``httpx.AsyncClient`` under the hood with a live connection pool that
    ``importlib.reload()`` of ``providers`` would otherwise drop on the
    floor mid-flight. ``recycle_events`` and ``cascade_attempts`` are ring
    buffers the consumer truncates via slice-rebind (``xs = xs[-CAP:]``),
    so both need setter indirection to the state object.
    """

    client: Optional[Any] = None  # anthropic.AsyncAnthropic
    recycle_events: List[Dict[str, Any]] = field(default_factory=list)
    cascade_attempts: List[Dict[str, Any]] = field(default_factory=list)
    cache_stats: Dict[str, Any] = field(default_factory=dict)
    counters: ClaudeProviderCounters = field(default_factory=ClaudeProviderCounters)

    @classmethod
    def fresh(cls) -> "ClaudeProviderState":
        return cls()


@dataclass
class PrimeProviderState:
    """Persistent state container for ``PrimeProvider``.

    Minimal today — PrimeProvider is nearly stateless; only the injected
    ``PrimeClient`` reference needs to outlive an ``importlib.reload()``.
    When future work adds cost tracking or recycle logic to Prime, fields
    belong here, not back on the provider instance.
    """

    client: Any = None  # PrimeClient

    @classmethod
    def fresh(cls) -> "PrimeProviderState":
        return cls()


@dataclass
class DoubleWordProviderCounters:
    """Monotonic counters for DoubleWordProvider that must cross reloads.

    ``last_error_status`` and ``last_chunk_at`` look like transient fields
    but they are read across retry boundaries — rebinding to zero on
    reload would drop diagnostic context that drives the retry cascade.
    """

    daily_spend: float = 0.0
    budget_reset_date: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%d", time.gmtime())
    )
    last_error_status: int = 0
    last_chunk_at: float = 0.0


@dataclass
class DoubleWordProviderState:
    """Persistent state container for ``DoubleWordProvider``.

    ``session`` is an ``aiohttp.ClientSession`` with a live TCP connector
    — exactly the kind of object you must not recreate casually, because
    connector teardown is async and reload doesn't wait. ``stats`` is a
    ``DoublewordStats`` dataclass mutated in place across requests and
    carried across reloads so operators can still see cumulative totals
    after a code hot-swap.
    """

    session: Optional[Any] = None  # aiohttp.ClientSession
    stats: Optional[Any] = None  # DoublewordStats (deferred, avoids cycle)
    counters: DoubleWordProviderCounters = field(
        default_factory=DoubleWordProviderCounters
    )

    @classmethod
    def fresh(cls) -> "DoubleWordProviderState":
        """Mint a fresh state. Defers the ``DoublewordStats`` import to
        sidestep the circular edge between ``_governance_state`` and
        ``doubleword_provider`` (consumer imports us at module import
        time; we can only reach back at call time).
        """
        from .doubleword_provider import DoublewordStats

        state = cls()
        state.stats = DoublewordStats()
        return state


_lock = threading.Lock()
_generator_state: Optional[GeneratorState] = None
_claude_provider_state: Optional[ClaudeProviderState] = None
_prime_provider_state: Optional[PrimeProviderState] = None
_doubleword_provider_state: Optional[DoubleWordProviderState] = None


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


def get_claude_provider_state() -> ClaudeProviderState:
    """Return the process-wide ``ClaudeProviderState`` singleton.

    First call wins. Live anthropic client, cascade ring buffers, and
    daily spend all survive ``importlib.reload(providers)``.
    """
    global _claude_provider_state
    with _lock:
        if _claude_provider_state is None:
            _claude_provider_state = ClaudeProviderState.fresh()
        return _claude_provider_state


def get_prime_provider_state() -> PrimeProviderState:
    """Return the process-wide ``PrimeProviderState`` singleton.

    First call wins. The injected ``PrimeClient`` survives module
    reloads so in-flight Prime sessions are not reset.
    """
    global _prime_provider_state
    with _lock:
        if _prime_provider_state is None:
            _prime_provider_state = PrimeProviderState.fresh()
        return _prime_provider_state


def get_doubleword_provider_state() -> DoubleWordProviderState:
    """Return the process-wide ``DoubleWordProviderState`` singleton.

    First call wins. aiohttp session, cumulative stats, and spend
    tracking all survive ``importlib.reload(doubleword_provider)``.
    """
    global _doubleword_provider_state
    with _lock:
        if _doubleword_provider_state is None:
            _doubleword_provider_state = DoubleWordProviderState.fresh()
        return _doubleword_provider_state


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
    Phase 1 lands ``OrchestratorState``, extend this to clear it too.
    Leaving a singleton un-reset here causes semaphores, FSM state, or
    client pools to bleed across unrelated test cases.

    Raises ``RuntimeError`` when called outside test mode so a
    production caller cannot accidentally wipe live state.
    """
    if not _is_test_mode():
        raise RuntimeError(
            "_governance_state.reset_for_tests() called outside test mode. "
            "Set JARVIS_TEST_MODE=true or run under pytest."
        )
    global _generator_state
    global _claude_provider_state
    global _prime_provider_state
    global _doubleword_provider_state
    with _lock:
        _generator_state = None
        _claude_provider_state = None
        _prime_provider_state = None
        _doubleword_provider_state = None
