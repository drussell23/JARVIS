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
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from .candidate_generator import FailbackStateMachine
    from .dw_latency_tracker import DwLatencyTracker
    from .op_context import OperationContext


UNQUARANTINE_GENERATOR_ENV = "JARVIS_UNQUARANTINE_GENERATOR"
UNQUARANTINE_PROVIDERS_ENV = "JARVIS_UNQUARANTINE_PROVIDERS"
UNQUARANTINE_ORCHESTRATOR_ENV = "JARVIS_UNQUARANTINE_ORCHESTRATOR"
JPRIME_PRIMACY_ENV = "JARVIS_JPRIME_PRIMACY"


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


def unquarantine_orchestrator_enabled() -> bool:
    """Return True when ``JARVIS_UNQUARANTINE_ORCHESTRATOR`` is truthy.

    Default false. Flipping to true routes the governed ``Orchestrator``
    through a process-lifetime :class:`OrchestratorState` singleton so
    the 11 reload-hostile fields inventoried in Phase 1 Step 3C — the
    oracle update lock, cost governor, forward-progress / productivity
    detectors, session lessons, RSI trackers, and the
    :class:`ModuleHotReloader` fs-subscription — survive
    ``importlib.reload(orchestrator)``.

    Independent of ``JARVIS_UNQUARANTINE_GENERATOR`` and
    ``JARVIS_UNQUARANTINE_PROVIDERS``. Operators opt in to each hoist
    phase separately during rollout. When off, the orchestrator mints a
    fresh :class:`OrchestratorState` per ``__init__`` call, preserving
    legacy per-instance semantics bit-for-bit.
    """
    return _env_truthy(UNQUARANTINE_ORCHESTRATOR_ENV)


def jprime_primacy_enabled() -> bool:
    """Return True when ``JARVIS_JPRIME_PRIMACY`` is truthy.

    Default false. Flipping to true opts into Phase 3 Scope α: the
    :class:`CandidateGenerator` dispatches BACKGROUND and SPECULATIVE
    routes to :class:`PrimeProvider` first (guarded by the hoisted
    ``jprime_sem=1`` in :class:`JPrimeState`) and only falls through to
    DoubleWord when the semaphore is saturated or J-Prime fails.
    Claude is still skipped entirely on those routes — primacy only
    reorders the Tier-0/self-hosted priority, it does not re-enable the
    fallback that BACKGROUND/SPECULATIVE deliberately drop.

    IMMEDIATE, STANDARD, and COMPLEX routes are untouched regardless of
    this flag: J-Prime's 8–12s CPU latency would regress the Apr 11
    first-token fix, so primacy deliberately stays on the cost-sensitive
    routes where latency is relaxed.

    Independent of the three un-quarantine flags — Scope α only needs
    the hoisted ``jprime_sem`` / stickiness placeholder in
    :data:`_jprime_state` to stay reload-safe, not the full orchestrator
    hoist.
    """
    return _env_truthy(JPRIME_PRIMACY_ENV)


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


# ---------------------------------------------------------------------------
# Phase 1 Step 3C — Orchestrator state hoist + §4 GovernanceStack bind contract
# ---------------------------------------------------------------------------
#
# The governed ``Orchestrator`` is the last big mutation carrier left inside a
# hot-reloadable module. Its ``__init__`` plants 11 reload-hostile roots — an
# ``asyncio.Lock`` the oracle writer contends on, live
# ``CostGovernor``/``ForwardProgressDetector``/``ProductivityDetector``
# feedback trackers, a 20-cap ``session_lessons`` slice-rebind buffer, four
# lesson-convergence counter ints, four RSI tracker singletons, and — most
# load-bearing of all — a ``ModuleHotReloader`` whose ``fs.changed.modified``
# subscription would silently die on ``importlib.reload(orchestrator)`` and
# take the entire G2G3 event-driven reload loop with it.
#
# Seven additional fields arrive by *setter* rather than constructor:
# ``reasoning_bridge``, ``infra_applicator``, ``reasoning_narrator``,
# ``dialogue_store``, ``pre_action_narrator``, ``exploration_fleet``, and
# ``critique_engine``. Each is stamped on once at harness boot via
# ``set_*()`` and never rebound afterwards — but if the orchestrator class
# reloads, the new instance starts with all seven at ``None`` and nobody
# calls the setters a second time. Hoisting them here is exactly the §4
# "don't let harness attach rot" fix: the :class:`OrchestratorState`
# instance outlives the class, so the new orchestrator rebinds into the
# already-populated state without re-running the harness wiring.
#
# Same binding discipline as 3A/3B:
# - Container-stable fields (``oracle_update_lock``, ``cost_governor``,
#   ``forward_progress``, ``productivity_detector``, ``counters``) can be
#   bound once to instance aliases because their identity never changes.
# - Slice-rebindable fields (``session_lessons``) and fields that start
#   ``None`` and are later assigned (``rsi_*``, ``hot_reloader``, the seven
#   attached refs) MUST be accessed via ``@property``/``@setter`` pairs so a
#   plain ``self._attr = X`` on the post-reload instance doesn't plant a
#   shadowing instance attribute that silently drifts from the singleton.
#
# § 4 GovernanceStack bind contract
# ---------------------------------
# The ``GovernanceStack`` is constructed once by ``create_governance_stack``
# and assigned to ``GovernedLoopService._stack``; long-lived workers
# (``BackgroundAgentPool``, the main ``run_op`` path) capture references to
# the current ``stack.orchestrator``. When ``importlib.reload(orchestrator)``
# swaps the class out from under them, those captured refs become stale and
# keep dispatching into the old instance's ``__init__``-allocated primitives.
#
# The fix is a module-level indirection — ``_bound_orchestrator`` under a
# dedicated ``_bind_lock`` (separate from ``_lock`` so orchestrator rebind
# can never deadlock against state-singleton init). ``bind_orchestrator()``
# atomically swaps the process-wide reference; ``get_bound_orchestrator()``
# returns the current value. The ``GovernanceStack`` class exposes the same
# contract via a ``bind_orchestrator()`` method and an ``orchestrator_ref``
# property that reads the live bind. Call sites stop capturing
# ``self._orchestrator`` and start reading ``stack.orchestrator_ref`` on
# every dispatch, so the second the rebind happens the whole system flips
# over to the new instance with no race window.
#
# The lock is an ``RLock`` because nothing currently recurses into bind,
# but tests that monkey-patch the orchestrator inside an already-held bind
# context would otherwise deadlock — ``RLock`` is cheap insurance.


@runtime_checkable
class OrchestratorRole(Protocol):
    """Minimal protocol for the §4 bind contract.

    Kept deliberately narrow: only the hot-path methods call sites reach
    through ``stack.orchestrator_ref``. If a new dispatch path emerges,
    add it here first so the type checker flags any stale capture that
    wasn't migrated to the rebind-safe route.

    The concrete governed ``Orchestrator`` in ``orchestrator.py`` is
    structurally compatible — no ABC registration required — and so are
    any test doubles that implement ``run()``. Using a ``Protocol`` here
    (instead of importing ``Orchestrator`` directly) sidesteps the
    circular edge that would otherwise form:
    ``orchestrator -> _governance_state -> orchestrator``.
    """

    async def run(self, ctx: "OperationContext") -> "OperationContext": ...


@dataclass
class OrchestratorCounters:
    """Monotonic counters for the governed orchestrator.

    Same rationale as :class:`GeneratorCounters` and
    :class:`ClaudeProviderCounters` — wrapping the ints in a dataclass
    gives reference-stable attribute mutation. ``ops_before_lesson``,
    ``ops_after_lesson``, and their ``_success`` siblings drive the
    Session Intelligence *convergence* metric that detects poisoned
    lessons and auto-clears the buffer; rebinding them on every reload
    would silently reset the detector and let a bad lesson keep burning
    money forever.
    """

    ops_before_lesson: int = 0
    ops_before_lesson_success: int = 0
    ops_after_lesson: int = 0
    ops_after_lesson_success: int = 0


@dataclass
class OrchestratorState:
    """Persistent state container for the governed ``Orchestrator``.

    Every field in this dataclass must survive
    ``importlib.reload(orchestrator)``. Adding a new mutable field to
    ``Orchestrator`` that cannot tolerate being reset on reload? Put it
    here and route the consumer through ``self._state``.

    The ``Optional[...]`` fields are *not* optional in principle — they
    are only defaulted to ``None`` because the tracker imports are
    guarded by try/except in the legacy construction path (some
    deployments don't ship the RSI modules). :meth:`fresh` performs the
    same imports lazily to preserve bit-for-bit behavior.
    """

    # Container-stable roots — bind-once aliases in the consumer.
    oracle_update_lock: asyncio.Lock
    cost_governor: Any  # CostGovernor
    forward_progress: Any  # ForwardProgressDetector
    productivity_detector: Any  # ProductivityDetector
    counters: OrchestratorCounters = field(default_factory=OrchestratorCounters)

    # Slice-rebindable — property/setter in the consumer. The list
    # identity changes on every ``xs = xs[-CAP:]`` truncation, so
    # aliasing it once would leave the post-rebind consumer pointing at
    # the stale container.
    session_lessons: List[Any] = field(default_factory=list)

    # Lazy-init singletons — start None, become real inside ``fresh()``
    # when the optional modules import cleanly. Property/setter in the
    # consumer so post-reload try/except re-assignment doesn't shadow.
    rsi_score_function: Optional[Any] = None
    rsi_score_history: Optional[Any] = None
    rsi_convergence_tracker: Optional[Any] = None
    rsi_transition_tracker: Optional[Any] = None

    # The most load-bearing reload-hostile field in the whole subsystem.
    # Its ``fs.changed.modified`` subscription is what makes G2G3 event-
    # driven reload work — lose this reference on reload and the
    # subscription handle is garbage-collected while the callback table
    # still points at the old closure. Property/setter in the consumer.
    hot_reloader: Optional[Any] = None

    # § 4 attached refs — stamped on by the harness at boot via the
    # seven ``set_*()`` methods. Every one needs a property/setter in
    # the consumer so ``self._reasoning_bridge = bridge`` flows through
    # to the state singleton instead of planting an instance attribute
    # that shadows the class descriptor on the next reload.
    reasoning_bridge: Optional[Any] = None
    infra_applicator: Optional[Any] = None
    reasoning_narrator: Optional[Any] = None
    dialogue_store: Optional[Any] = None
    pre_action_narrator: Optional[Any] = None
    exploration_fleet: Optional[Any] = None
    critique_engine: Optional[Any] = None

    @classmethod
    def fresh(
        cls,
        *,
        project_root: Optional[Path] = None,
    ) -> "OrchestratorState":
        """Mint a new :class:`OrchestratorState` with fresh primitives.

        Used by two paths:

        1. The legacy per-instance path inside ``Orchestrator.__init__``
           when ``JARVIS_UNQUARANTINE_ORCHESTRATOR`` is false — preserves
           the pre-hoist behavior bit-for-bit so operators can roll
           forward without observing any change.
        2. Tests that want isolated state without touching the
           singleton — ``reset_for_tests()`` clears the singleton and
           then each test constructs its own via ``fresh()``.

        Every import is deferred because the orchestrator, cost
        governor, and tracker modules all transitively import this file
        at module-load time — reaching back to them at class-definition
        time would break the circular edge at startup.

        The RSI trackers and hot reloader are wrapped in try/except
        blocks mirroring the legacy init so missing optional modules
        don't crash the orchestrator construction. Failures are silent
        on purpose: this method is called in the hot path and the
        legacy code logged debug-level, nothing that would surface in
        production telemetry.
        """
        # Deferred imports — see class-docstring note on circular edges.
        from backend.core.ouroboros.governance.cost_governor import (
            CostGovernor,
            CostGovernorConfig,
        )
        from backend.core.ouroboros.governance.forward_progress import (
            ForwardProgressConfig,
            ForwardProgressDetector,
        )
        from backend.core.ouroboros.governance.productivity_detector import (
            ProductivityDetector,
            ProductivityDetectorConfig,
        )

        state = cls(
            oracle_update_lock=asyncio.Lock(),
            cost_governor=CostGovernor(CostGovernorConfig()),
            forward_progress=ForwardProgressDetector(ForwardProgressConfig()),
            productivity_detector=ProductivityDetector(ProductivityDetectorConfig()),
        )

        # RSI Convergence Framework — optional modules, lazy init.
        # Matches the legacy try/except shape in Orchestrator.__init__.
        try:
            from backend.core.ouroboros.governance.composite_score import (
                CompositeScoreFunction,
                ScoreHistory,
            )

            state.rsi_score_function = CompositeScoreFunction()
            _rsi_dir = Path(
                os.environ.get(
                    "JARVIS_SELF_EVOLUTION_DIR",
                    str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
                )
            )
            state.rsi_score_history = ScoreHistory(persistence_dir=_rsi_dir)
        except Exception:
            pass

        try:
            from backend.core.ouroboros.governance.convergence_tracker import (
                ConvergenceTracker,
            )

            state.rsi_convergence_tracker = ConvergenceTracker()
        except Exception:
            pass

        try:
            from backend.core.ouroboros.governance.transition_tracker import (
                TransitionProbabilityTracker,
            )

            state.rsi_transition_tracker = TransitionProbabilityTracker()
        except Exception:
            pass

        # Module hot-reloader (Manifesto §6 RSI loop closer).
        # ``project_root`` is required by ``ModuleHotReloader`` — when
        # the consumer didn't pass one (e.g. unit tests minting a bare
        # state), the reloader is simply skipped.
        if (
            project_root is not None
            and os.environ.get("JARVIS_HOT_RELOAD_ENABLED", "true").lower() != "false"
        ):
            try:
                from backend.core.ouroboros.governance.module_hot_reloader import (
                    ModuleHotReloader,
                )

                state.hot_reloader = ModuleHotReloader(project_root=project_root)
            except Exception:
                pass

        return state


# ---------------------------------------------------------------------------
# Phase 3 Scope α — J-Prime primacy state hoist
# ---------------------------------------------------------------------------
#
# Scope α routes BACKGROUND and SPECULATIVE operations to J-Prime first when
# ``JARVIS_JPRIME_PRIMACY=true``. Two reload-hostile roots are required from
# day one — per Derek's middle-path directive, both MUST live here and never
# on a hot ``CandidateGenerator`` instance:
#
# 1. ``jprime_sem``: an ``asyncio.Semaphore(1)`` enforcing a single client-
#    side concurrent request to J-Prime. The GCP-hosted J-Prime endpoint
#    exposes a 50-slot server-side queue, but that queue is swap-transient
#    (evicted on any VM swap) and must NOT be treated as a concurrency
#    ceiling by callers. The client-side sem is the real limit. A saturated
#    sem falls through to DoubleWord instead of queuing — the whole point of
#    primacy is "try cheaply, fall through quickly", not to serialize the
#    entire background workload behind a single in-flight J-Prime call.
#
# 2. ``model_stickiness``: a placeholder dict for future per-route / per-
#    intent model pinning metrics. Scope α does not populate this field —
#    intent→model hints are Scope β and hardware-workload-driven tri-tier
#    reshaping is Scope γ. Hoisting the container now means β/γ can land
#    without any further hoist work (same binding discipline as 3A/3B).
#
# Counters track the three branches the primacy path can take so operators
# can read a single stats dict and immediately see whether J-Prime is
# actually eating traffic or whether the sem is saturating and dumping
# everything to DW.


@dataclass
class JPrimeCounters:
    """Monotonic counters for the Scope α primacy path.

    Same rationale as :class:`GeneratorCounters` — wrap the ints in a
    dataclass so the consumer can bind a single reference and mutate via
    attribute access without planting a shadowing instance attribute.
    """

    jprime_hits: int = 0
    jprime_sem_overflows: int = 0
    jprime_failures: int = 0
    fallthrough_to_dw: int = 0


@dataclass
class JPrimeState:
    """Persistent state container for Scope α J-Prime primacy.

    Every field here must survive ``importlib.reload`` of both
    ``candidate_generator`` and ``providers``. The sem is the load-
    bearing piece — losing it on reload would silently drop the client-
    side concurrency limit and let a burst of background ops hit
    J-Prime in parallel, which would immediately hit the 50-slot swap-
    transient queue and start serializing opaquely at the server edge.
    """

    # Client-side concurrency ceiling. ``Semaphore(1)`` is the Scope α
    # contract: only one in-flight J-Prime request at a time. Overflow
    # falls through to DW. The value is NOT configurable at the
    # dataclass level — if a future scope wants more parallelism, it
    # gets a new scope and its own flag.
    jprime_sem: asyncio.Semaphore
    # Placeholder for Scope β/γ model-stickiness telemetry. Shape is
    # deliberately open — when Scope β lands, the key space is expected
    # to be ``{intent: (model_id, last_used_ts, hit_count)}`` but that is
    # not enforced here so β can refine the shape without a state-class
    # migration. Scope α neither reads nor writes this dict.
    model_stickiness: Dict[str, Any] = field(default_factory=dict)
    counters: JPrimeCounters = field(default_factory=JPrimeCounters)

    @classmethod
    def fresh(cls) -> "JPrimeState":
        """Mint a new :class:`JPrimeState` with a fresh ``Semaphore(1)``.

        Called by:

        1. :func:`get_jprime_state` on the first singleton lookup.
        2. Tests that want isolated state without touching the singleton
           (after :func:`reset_for_tests` nulls the module-level slot).

        The semaphore is created here (not in the dataclass ``__init__``)
        because ``asyncio.Semaphore()`` needs to be called inside a
        running event loop in some asyncio versions to pick up the
        correct loop. Deferring to ``fresh()`` keeps module import
        synchronous while still giving the singleton a ready-to-acquire
        sem on first use.
        """
        return cls(jprime_sem=asyncio.Semaphore(1))


_lock = threading.Lock()
_generator_state: Optional[GeneratorState] = None
_claude_provider_state: Optional[ClaudeProviderState] = None
_prime_provider_state: Optional[PrimeProviderState] = None
_doubleword_provider_state: Optional[DoubleWordProviderState] = None
_orchestrator_state: Optional[OrchestratorState] = None
_jprime_state: Optional[JPrimeState] = None

# § 4 bind contract — separate lock so rebind can never deadlock against
# state-singleton init under ``_lock``. ``RLock`` guards against future
# re-entrancy: a test that monkey-patches the orchestrator inside an
# already-held bind context would deadlock with a plain ``Lock``.
_bind_lock = threading.RLock()
_bound_orchestrator: Optional[OrchestratorRole] = None


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


def get_orchestrator_state(
    *,
    project_root: Optional[Path] = None,
) -> OrchestratorState:
    """Return the process-wide :class:`OrchestratorState` singleton.

    First call wins — subsequent calls return the existing instance
    regardless of their ``project_root`` argument, so the oracle update
    lock, cost governor, forward-progress detector, session lessons
    buffer, convergence counters, RSI trackers, hot reloader
    subscription, and all seven attached refs stay stable across
    ``importlib.reload(orchestrator)``.

    The ``project_root`` kwarg is honored only on the *first* call and
    is required to arm the :class:`ModuleHotReloader`. Callers that
    need a different root (tests, alternate worktrees) must either
    disable the singleton path
    (``JARVIS_UNQUARANTINE_ORCHESTRATOR=false``) or call
    :func:`reset_for_tests` first.
    """
    global _orchestrator_state
    with _lock:
        if _orchestrator_state is None:
            _orchestrator_state = OrchestratorState.fresh(project_root=project_root)
        return _orchestrator_state


def bind_orchestrator(orch: Optional[OrchestratorRole]) -> None:
    """Atomically swap the process-wide orchestrator reference.

    This is the § 4 GovernanceStack bind contract entry point. Call
    sites that would otherwise capture ``self._orchestrator`` read
    through :func:`get_bound_orchestrator` (or the
    :attr:`GovernanceStack.orchestrator_ref` property that wraps it)
    and so flip over to the new instance the instant
    ``importlib.reload(orchestrator)`` completes.

    Passing ``None`` clears the bind — used at
    :meth:`GovernedLoopService._detach_from_stack` time so a shut-down
    loop doesn't leak a dead orchestrator into a reborn one.

    Held under :data:`_bind_lock` (a separate ``RLock`` from
    :data:`_lock`, so state-singleton init and orchestrator rebind
    can't deadlock against each other).
    """
    global _bound_orchestrator
    with _bind_lock:
        _bound_orchestrator = orch


def get_bound_orchestrator() -> Optional[OrchestratorRole]:
    """Return the currently bound orchestrator, or ``None`` if unset.

    Hot-path readers should prefer
    :attr:`GovernanceStack.orchestrator_ref`, which wraps this and
    falls back to the legacy ``stack.orchestrator`` dataclass slot
    when the bind contract has not been engaged.
    """
    with _bind_lock:
        return _bound_orchestrator


def get_jprime_state() -> JPrimeState:
    """Return the process-wide :class:`JPrimeState` singleton.

    First call wins — subsequent calls return the existing instance so
    the Semaphore(1) concurrency ceiling and the Scope β/γ stickiness
    dict stay stable across ``importlib.reload(candidate_generator)``
    and ``importlib.reload(providers)``. If the sem were re-minted per
    ``CandidateGenerator.__init__``, a module hot-reload during live
    traffic would silently reset the concurrency limit and let a burst
    of background ops hit J-Prime in parallel.

    No arguments: Scope α does not expose any tunables on the state
    container. Concurrency stays at 1 and the stickiness dict starts
    empty. Future scopes that add per-intent policy (β) or dynamic
    concurrency (γ) should extend :class:`JPrimeState` with their own
    fields and default factories — the first-call-wins getter does not
    need to change.
    """
    global _jprime_state
    with _lock:
        if _jprime_state is None:
            _jprime_state = JPrimeState.fresh()
        return _jprime_state


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
    global _orchestrator_state
    global _jprime_state
    global _bound_orchestrator
    with _lock:
        _generator_state = None
        _claude_provider_state = None
        _prime_provider_state = None
        _doubleword_provider_state = None
        _orchestrator_state = None
        _jprime_state = None
    # Orchestrator bind lives under its own RLock — acquire separately
    # to preserve the "no cross-lock ordering" invariant between
    # ``_lock`` (state-singleton init) and ``_bind_lock`` (rebind).
    with _bind_lock:
        _bound_orchestrator = None
