"""TerminationHookRegistry — Slice 2 sync registry + auto-discovery.

Per-process registry of termination-time hooks. Mirrors
:class:`LifecycleHookRegistry`'s exact shape (threading.RLock,
priority-ordered insertion-sort, listener pattern, module-owned
discovery, singleton accessor) — different vocabulary, same
discipline.

Architectural reuse — three existing surfaces compose with ZERO
duplication:

  * Slice 1 :mod:`termination_hook` substrate — closed taxonomies
    (:class:`TerminationCause` / :class:`TerminationPhase` /
    :class:`HookOutcome`), frozen dataclasses
    (:class:`TerminationHookContext` /
    :class:`HookExecutionRecord` /
    :class:`TerminationDispatchResult`), and the total
    :func:`dispatch_phase_sync` decision function. The registry
    just supplies the hooks list to the dispatcher.
  * :class:`LifecycleHookRegistry` — same threading.RLock + listener
    pattern + bounded snapshot surface + insertion-sorted priority
    + capacity check. Mirrored 1:1 with vocabulary swapped.
  * Module-owned discovery contract (Priority #6 closure) —
    :func:`discover_module_provided_hooks` walks
    :data:`_TERMINATION_HOOK_PROVIDER_PACKAGES` and calls
    ``register_termination_hooks(registry)`` on any module
    exposing it. Modules own their hooks co-located with the
    consuming code; no edits to this module required.

## Strict directive compliance (Slice 2 surface)

* **Sync-first preserved** — :meth:`dispatch` calls
  :func:`dispatch_phase_sync` (Slice 1) directly. The registry
  itself touches NO asyncio. AST-pinned in tests.
* **Deterministic budgets** — :meth:`dispatch` reads the
  per-phase budget from the registry's env-knob accessors
  (``JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S`` /
  ``JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S``) so Slice 3's
  call sites just supply the phase + cause; the registry handles
  budget shaping uniformly.
* **NEVER raises** — :meth:`dispatch` and all snapshot/inspection
  surfaces are defensive. :meth:`register` is the SINGLE
  documented exception path (operator-misconfig surfaces at
  boot, not at hook-fire-time).
* **No hardcoding** — discovery package list curated once;
  per-phase budgets env-tunable; capacity env-tunable.

## Authority invariants (AST-pinned in Slice 4)

* MAY import: :mod:`termination_hook` (Slice 1 substrate). Module-
  owned ``register_flags`` / ``register_shipped_invariants``
  exempt from import allowlist (registration-contract exemption).
* MUST NOT import: ``asyncio`` (sync-first contract — same as
  Slice 1) / ``yaml_writer`` / ``orchestrator`` / ``iron_gate`` /
  ``risk_tier`` / ``change_engine`` / ``candidate_generator`` /
  ``gate`` / ``policy``.
* No ``exec`` / ``eval`` / ``compile``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import (
    Any, Callable, Dict, List, Optional, Protocol, Tuple,
    runtime_checkable,
)

from backend.core.ouroboros.battle_test.termination_hook import (
    DEFAULT_HARD_EXIT_PHASE_BUDGET_S,
    DEFAULT_PER_HOOK_TIMEOUT_S,
    DEFAULT_PHASE_BUDGET_S,
    TERMINATION_HOOK_SCHEMA_VERSION,
    TerminationCause,
    TerminationDispatchResult,
    TerminationHook,
    TerminationHookContext,
    TerminationPhase,
    dispatch_phase_sync,
)

logger = logging.getLogger(__name__)


TERMINATION_HOOK_REGISTRY_SCHEMA_VERSION: str = (
    "termination_hook_registry.1"
)


# ---------------------------------------------------------------------------
# Env knobs — every tunable parameter reads from environment with
# documented defaults + clamps. No hardcoding. Slice 4 graduation
# registers these in FlagRegistry.
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def master_enabled() -> bool:
    """Master kill switch — ``JARVIS_TERMINATION_HOOKS_ENABLED``
    (default TRUE post Slice 4 graduation).

    Operator emergency escape hatch: when off, :meth:`dispatch`
    returns a clean empty result without invoking any hook.
    Hot-revertable — re-read on every dispatch call. Atomic at
    the dispatch boundary, so a flip mid-session takes effect
    immediately for the next termination event.

    Default-true rationale: termination hooks are SAFETY
    infrastructure (the partial-summary writer is the entire
    reason this arc exists). Default-false would mean operator
    must explicitly opt in to having shutdown summaries land —
    inverting the safety bias. Operators who need to disable
    can flip ``=false`` for instant rollback.

    Asymmetric env semantics: empty/whitespace = unset =
    graduated default true; explicit ``0`` / ``false`` / ``no`` /
    ``off`` evaluates false; explicit truthy values evaluate
    true."""
    raw = os.environ.get(
        "JARVIS_TERMINATION_HOOKS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-02
    return raw in ("1", "true", "yes", "on")


def max_hooks_per_phase() -> int:
    """``JARVIS_TERMINATION_HOOK_MAX_PER_PHASE`` — capacity per
    phase. Default 16, clamped [1, 256]. Defends against
    misconfigured plugins registering thousands of hooks (matches
    LifecycleHookRegistry's discipline)."""
    return _env_int_clamped(
        "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE",
        16, floor=1, ceiling=256,
    )


def per_hook_timeout_s() -> float:
    """``JARVIS_TERMINATION_HOOK_TIMEOUT_S`` — default per-hook
    wall-clock cap (seconds). Default 5.0, clamped [0.1, 30.0].
    Hooks register their own timeout; this is the fallback when
    they don't supply one."""
    return _env_float_clamped(
        "JARVIS_TERMINATION_HOOK_TIMEOUT_S",
        DEFAULT_PER_HOOK_TIMEOUT_S, floor=0.1, ceiling=30.0,
    )


def phase_budget_s(phase: TerminationPhase) -> float:
    """Per-phase wall-clock budget. PRE_HARD_EXIT gets a tighter
    budget than the regular phases because the
    BoundedShutdownWatchdog deadline is measured in single-digit
    seconds at that point. Read fresh on every call so operator
    flips hot-revert without restart.

    Env knobs:
      * ``JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S`` — applies to
        :attr:`PRE_SHUTDOWN_EVENT_SET` and
        :attr:`POST_ASYNC_CLEANUP`. Default 10.0, clamped
        [1.0, 60.0].
      * ``JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S`` — applies
        to :attr:`PRE_HARD_EXIT`. Default 2.0, clamped
        [0.5, 10.0]. Tighter floor + ceiling reflects the
        single-digit-seconds nature of the watchdog deadline."""
    if phase is TerminationPhase.PRE_HARD_EXIT:
        return _env_float_clamped(
            "JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S",
            DEFAULT_HARD_EXIT_PHASE_BUDGET_S,
            floor=0.5, ceiling=10.0,
        )
    return _env_float_clamped(
        "JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S",
        DEFAULT_PHASE_BUDGET_S, floor=1.0, ceiling=60.0,
    )


# ---------------------------------------------------------------------------
# TerminationHookCallable Protocol — what the registry stores
# ---------------------------------------------------------------------------


@runtime_checkable
class TerminationHookCallable(Protocol):
    """Sync callable invoked synchronously on a daemon thread by
    the Slice 1 dispatcher. The hook receives a frozen
    :class:`TerminationHookContext` and returns nothing.

    Hooks MUST be sync (the dispatcher uses threading, never
    asyncio). Hooks MUST NOT mutate the context (it's frozen by
    construction; mutation attempts raise ``FrozenInstanceError``
    at the first attribute write).

    Hooks SHOULD complete within their registered timeout. The
    dispatcher orphans threads that exceed the timeout — by
    contract, the process is shutting down anyway, so a leaked
    daemon thread is acceptable.

    Hooks that raise are caught by the dispatcher and converted
    to a :class:`HookExecutionRecord` with ``outcome=FAILED``.
    """

    def __call__(self, context: TerminationHookContext) -> None:
        ...  # Protocol


# ---------------------------------------------------------------------------
# Frozen registration record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminationHookRegistration:
    """One registered hook entry. Frozen for safe propagation
    across snapshot / projection surfaces.

    ``priority`` controls execution order (lower = earlier).
    Equal-priority registrations execute in registration order
    (the insertion-sort below is stable).

    ``timeout_s`` is the per-hook wall-clock cap; clamped at
    registration to [0.1, 30.0] to stay safely under any
    reasonable phase budget.

    ``enabled_check`` is an optional per-hook gate (for hooks
    that want feature-flag visibility independent of the master
    switch). Returning False at dispatch time means the hook
    is silently skipped — NOT recorded as SKIPPED in the
    dispatch result. SKIPPED is reserved for budget exhaustion."""

    name: str
    phase: TerminationPhase
    callable: TerminationHookCallable
    priority: int = 100
    timeout_s: float = DEFAULT_PER_HOOK_TIMEOUT_S
    enabled_check: Optional[Callable[[], bool]] = None
    registered_ts: float = 0.0
    schema_version: str = TERMINATION_HOOK_REGISTRY_SCHEMA_VERSION

    def is_enabled(self) -> bool:
        """Per-hook enabled gate. NEVER raises — a misbehaving
        check is treated as disabled (defensive: the registry
        cannot be the cause of a missed shutdown hook because
        the hook's OWN check raised)."""
        if self.enabled_check is None:
            return True
        try:
            return bool(self.enabled_check())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[TerminationHookRegistry] enabled_check %s "
                "raised: %s — treating as disabled",
                self.name, exc,
            )
            return False

    def to_projection(self) -> Dict[str, Any]:
        """Bounded read-only projection for snapshot / SSE.
        Excludes the callable + enabled_check (not serializable;
        operators see them by name in audit)."""
        return {
            "name": self.name,
            "phase": self.phase.value,
            "priority": self.priority,
            "timeout_s": self.timeout_s,
            "registered_ts": self.registered_ts,
            "has_enabled_check": self.enabled_check is not None,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Exceptions — registration-time only (NEVER raised by dispatch)
# ---------------------------------------------------------------------------


class TerminationHookRegistryError(Exception):
    """Base for registry registration errors."""


class DuplicateHookNameError(TerminationHookRegistryError):
    """Operator misconfig: same name registered twice. Explicitly
    raised so misconfig surfaces at boot, not at fire-time
    (when surfacing is too late — the process is already
    shutting down)."""


class HookCapacityExceededError(TerminationHookRegistryError):
    """A single phase has more hooks than max_hooks_per_phase
    permits. Explicitly raised; defends against misconfigured
    plugins registering thousands of hooks."""


class InvalidHookError(TerminationHookRegistryError):
    """The supplied hook fails type / shape validation
    (non-callable, non-phase, empty name, etc.)."""


# ---------------------------------------------------------------------------
# TerminationHookRegistry
# ---------------------------------------------------------------------------


class TerminationHookRegistry:
    """Per-process registry of termination-time hooks.

    Thread-safe via ``threading.RLock``. Capacity-limited per
    phase (env-knob, default 16). Listener pattern for Slice 4
    SSE bridge. NEVER raises out of dispatch / snapshot /
    for_phase / unregister; raises explicitly on
    :meth:`register` validation failures."""

    def __init__(
        self,
        *,
        max_per_phase: Optional[int] = None,
    ) -> None:
        try:
            cap = (
                max_per_phase if max_per_phase is not None
                else max_hooks_per_phase()
            )
            self._max_per_phase = max(1, min(256, int(cap)))
        except (TypeError, ValueError):
            self._max_per_phase = max_hooks_per_phase()
        self._by_phase: Dict[
            TerminationPhase, List[TerminationHookRegistration],
        ] = {}
        self._by_name: Dict[
            str, TerminationHookRegistration,
        ] = {}
        self._lock = threading.RLock()
        self._listeners: List[
            Callable[[Dict[str, Any]], None]
        ] = []

    # --- Introspection -----------------------------------------------------

    @property
    def max_per_phase(self) -> int:
        return self._max_per_phase

    def total_count(self) -> int:
        with self._lock:
            return len(self._by_name)

    def count_for_phase(self, phase: TerminationPhase) -> int:
        with self._lock:
            return len(self._by_phase.get(phase, []))

    def for_phase(
        self, phase: TerminationPhase,
    ) -> Tuple[TerminationHookRegistration, ...]:
        """Priority-ordered tuple of registrations for one phase.
        Sort happens at registration time (insertion-sorted) so
        this lookup is O(N) tuple copy. NEVER raises."""
        try:
            if not isinstance(phase, TerminationPhase):
                return ()
            with self._lock:
                bucket = self._by_phase.get(phase, [])
                return tuple(bucket)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[TerminationHookRegistry] for_phase %r "
                "degraded: %s", phase, exc,
            )
            return ()

    def snapshot(
        self, name: str,
    ) -> Optional[Dict[str, Any]]:
        """One bounded read-only projection by name. None if
        unknown. NEVER raises."""
        try:
            with self._lock:
                reg = self._by_name.get(str(name or ""))
                return (
                    reg.to_projection() if reg is not None
                    else None
                )
        except Exception:  # noqa: BLE001 — defensive
            return None

    def snapshot_all(self) -> Tuple[Dict[str, Any], ...]:
        """All registrations as bounded projections, in stable
        alphabetical order by name. NEVER raises."""
        try:
            with self._lock:
                return tuple(
                    self._by_name[k].to_projection()
                    for k in sorted(self._by_name.keys())
                )
        except Exception:  # noqa: BLE001 — defensive
            return ()

    def names(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._by_name.keys()))

    # --- Listeners ---------------------------------------------------------

    def on_transition(
        self,
        listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Subscribe a listener for register / unregister / dispatch
        events. Returns an unsubscribe handle. Listener payload
        shape::

            {
              "event_type": (
                "termination_hook_registered" |
                "termination_hook_unregistered" |
                "termination_hook_dispatched"
              ),
              "projection": <to_projection() dict>
                  for register/unregister, OR
              "dispatch_result": <to_dict() dict>
                  for termination_hook_dispatched,
            }

        Mirrors LifecycleHookRegistry's listener contract.
        Listener exceptions are swallowed (mirror's ``_fire``
        discipline so a broken SSE bridge can't block
        registrations)."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire_registration(
        self,
        event_type: str,
        registration: TerminationHookRegistration,
    ) -> None:
        payload = {
            "event_type": event_type,
            "projection": registration.to_projection(),
        }
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[TerminationHookRegistry] listener "
                    "exception: %s", exc,
                )

    def _fire_dispatch(
        self,
        result: TerminationDispatchResult,
    ) -> None:
        """Fire dispatch listeners. Separate from registration
        listeners so SSE bridges can subscribe to one or both
        channels."""
        payload = {
            "event_type": "termination_hook_dispatched",
            "dispatch_result": result.to_dict(),
        }
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[TerminationHookRegistry] dispatch "
                    "listener exception: %s", exc,
                )

    # --- Mutation ----------------------------------------------------------

    def register(
        self,
        phase: TerminationPhase,
        hook: TerminationHookCallable,
        *,
        name: str,
        priority: int = 100,
        timeout_s: Optional[float] = None,
        enabled_check: Optional[Callable[[], bool]] = None,
    ) -> TerminationHookRegistration:
        """Register one hook. Raises explicitly on validation
        failure so operator misconfig surfaces at boot.

        Raises:
          :class:`InvalidHookError` — non-callable hook, non-phase,
            empty name.
          :class:`DuplicateHookNameError` — name already registered.
          :class:`HookCapacityExceededError` — phase already at
            max_per_phase capacity."""
        # Validate phase.
        if not isinstance(phase, TerminationPhase):
            raise InvalidHookError(
                f"phase must be a TerminationPhase — got "
                f"{type(phase).__name__}"
            )
        # Validate hook callable.
        if not callable(hook):
            raise InvalidHookError(
                f"hook must be callable — got "
                f"{type(hook).__name__}"
            )
        # Validate name.
        clean_name = str(name or "").strip()
        if not clean_name:
            raise InvalidHookError(
                "hook name must be a non-empty string"
            )
        if len(clean_name) > 128:
            clean_name = clean_name[:128]
        # Coerce priority + timeout defensively.
        try:
            clean_priority = int(priority)
        except (TypeError, ValueError):
            clean_priority = 100
        try:
            clean_timeout = (
                float(timeout_s) if timeout_s is not None
                else per_hook_timeout_s()
            )
            clean_timeout = max(0.1, min(30.0, clean_timeout))
        except (TypeError, ValueError):
            clean_timeout = per_hook_timeout_s()

        registration = TerminationHookRegistration(
            name=clean_name,
            phase=phase,
            callable=hook,
            priority=clean_priority,
            timeout_s=clean_timeout,
            enabled_check=enabled_check,
            registered_ts=time.monotonic(),
        )

        with self._lock:
            # Duplicate name check.
            if clean_name in self._by_name:
                raise DuplicateHookNameError(
                    f"hook name {clean_name!r} already "
                    f"registered"
                )
            # Capacity check.
            bucket = self._by_phase.setdefault(phase, [])
            if len(bucket) >= self._max_per_phase:
                raise HookCapacityExceededError(
                    f"phase {phase.value!r} at capacity "
                    f"({self._max_per_phase} hooks)"
                )
            # Insertion-sort by priority so for_phase() is
            # O(N) copy. Stable on equal priority — preserves
            # registration order.
            inserted = False
            for i, existing in enumerate(bucket):
                if clean_priority < existing.priority:
                    bucket.insert(i, registration)
                    inserted = True
                    break
            if not inserted:
                bucket.append(registration)
            self._by_name[clean_name] = registration

        logger.info(
            "[TerminationHookRegistry] registered hook=%s "
            "phase=%s priority=%d timeout_s=%.1f",
            clean_name, phase.value, clean_priority,
            clean_timeout,
        )
        self._fire_registration(
            "termination_hook_registered", registration,
        )
        return registration

    def unregister(self, name: str) -> bool:
        """Remove a hook by name. Returns True if removed, False
        if unknown. NEVER raises."""
        try:
            clean_name = str(name or "").strip()
            if not clean_name:
                return False
            with self._lock:
                reg = self._by_name.pop(clean_name, None)
                if reg is None:
                    return False
                bucket = self._by_phase.get(reg.phase, [])
                if reg in bucket:
                    bucket.remove(reg)
            logger.info(
                "[TerminationHookRegistry] unregistered "
                "hook=%s phase=%s",
                clean_name, reg.phase.value,
            )
            self._fire_registration(
                "termination_hook_unregistered", reg,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[TerminationHookRegistry] unregister %r "
                "raised: %s", name, exc,
            )
            return False

    # --- Dispatch ----------------------------------------------------------

    def dispatch(
        self,
        *,
        phase: TerminationPhase,
        cause: TerminationCause,
        session_dir: str,
        started_at: float,
        stop_reason: str = "",
        per_hook_timeout_override_s: Optional[float] = None,
        phase_budget_override_s: Optional[float] = None,
    ) -> TerminationDispatchResult:
        """Synchronously dispatch every registered hook for the
        phase, bounded by the per-hook timeout AND per-phase
        budget.

        Builds the :class:`TerminationHookContext`, filters
        registrations to enabled ones (per-hook ``enabled_check``
        gate fires here), then delegates to
        :func:`dispatch_phase_sync` (Slice 1). Disabled hooks are
        SILENTLY OMITTED from the dispatch — they don't appear in
        the result records. SKIPPED is reserved for budget
        exhaustion; absence-due-to-disabled is a different
        signal (the operator turned the hook off; observability
        for that is the registration listener channel).

        Per-hook timeouts: each registration carries its own
        timeout from registration time. The dispatcher honors
        per-registration timeouts via the per-hook clamp inside
        ``dispatch_phase_sync`` — but that takes a single
        ``per_hook_timeout_s`` argument. We pass
        ``per_hook_timeout_override_s`` (caller override) OR the
        max of the registered timeouts as the dispatcher's
        per-hook cap, then rely on the per-phase budget to clamp
        each hook's effective timeout.

        Strict-sync: this method does NOT touch asyncio. The
        Slice 1 dispatcher uses threading exclusively.
        Verifiable by AST pin (no asyncio import in this file).

        NEVER raises."""
        # Slice 4 master flag — re-read on every call so flips
        # take effect immediately. Default true (graduated).
        # When off, return a structurally valid empty result so
        # callers can branch on records == () without changing
        # their error-handling shape.
        if not master_enabled():
            logger.debug(
                "[TerminationHookRegistry] master flag off — "
                "skipping dispatch phase=%s cause=%s",
                phase, cause,
            )
            return TerminationDispatchResult(
                phase=phase,
                cause=cause,
                records=(),
                total_duration_ms=0.0,
                budget_exhausted=False,
            )
        try:
            ctx = TerminationHookContext(
                cause=cause,
                phase=phase,
                session_dir=str(session_dir or ""),
                started_at=float(started_at or 0.0),
                stop_reason=str(stop_reason or ""),
            )
            with self._lock:
                bucket = list(self._by_phase.get(phase, []))
            # Filter to enabled hooks; preserve priority order.
            active = [r for r in bucket if r.is_enabled()]
            hooks: List[Tuple[str, TerminationHook]] = [
                (r.name, r.callable) for r in active
            ]
            # Resolve effective per-hook timeout: caller override
            # > max(registered timeouts) > env default. Since
            # dispatch_phase_sync takes ONE per-hook value (then
            # clamps to remaining budget per hook), using the
            # max of registered values keeps the smaller-timeout
            # hooks under their own caps via the budget clamp,
            # while still bounding the longer ones.
            if per_hook_timeout_override_s is not None:
                eff_per_hook = float(per_hook_timeout_override_s)
            elif active:
                eff_per_hook = max(r.timeout_s for r in active)
            else:
                eff_per_hook = per_hook_timeout_s()
            # Resolve effective phase budget.
            if phase_budget_override_s is not None:
                eff_budget = float(phase_budget_override_s)
            else:
                eff_budget = phase_budget_s(phase)
            result = dispatch_phase_sync(
                phase=phase,
                cause=cause,
                hooks=hooks,
                context=ctx,
                per_hook_timeout_s=eff_per_hook,
                phase_budget_s=eff_budget,
            )
            # Fire dispatch listeners (best-effort).
            self._fire_dispatch(result)
            return result
        except Exception as exc:  # noqa: BLE001 — defensive
            # The dispatcher itself is documented to never raise;
            # this is paranoia for the registry's own bookkeeping
            # path. Return a structurally valid result with zero
            # records so callers can branch on
            # ``result.records == ()``.
            logger.warning(
                "[TerminationHookRegistry] dispatch %r "
                "degraded: %s", phase, exc,
            )
            return TerminationDispatchResult(
                phase=phase,
                cause=cause,
                records=(),
                total_duration_ms=0.0,
                budget_exhausted=False,
            )

    # --- Test helper -------------------------------------------------------

    def reset(self) -> None:
        """Test helper — drops all registrations + listeners.
        NEVER called from production. NEVER raises."""
        with self._lock:
            self._by_phase.clear()
            self._by_name.clear()
            self._listeners.clear()


# ---------------------------------------------------------------------------
# Singleton accessor — production wire-up entry point
# ---------------------------------------------------------------------------


_default_registry: Optional[TerminationHookRegistry] = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> TerminationHookRegistry:
    """Process-global :class:`TerminationHookRegistry` instance.
    Lazy-constructed on first call. Used by Slice 3 harness wire-
    up so callers don't have to thread their own registry through
    the call chain."""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = TerminationHookRegistry()
        return _default_registry


def reset_default_registry_for_tests() -> None:
    """Test helper — drops the singleton so the next
    get_default_registry call constructs a fresh instance.
    NEVER called from production."""
    global _default_registry
    with _default_registry_lock:
        _default_registry = None


# ---------------------------------------------------------------------------
# Module-owned-hooks discovery loop (mirrors Priority #6 contract)
# ---------------------------------------------------------------------------


#: Curated package list — modules in these packages may expose
#: ``register_termination_hooks(registry)`` and have their hooks
#: discovered automatically at boot. Add a package here to opt
#: in to discovery; modules in non-listed packages register
#: explicitly via :meth:`TerminationHookRegistry.register`.
#:
#: ``battle_test`` is the natural home for session-lifecycle
#: hooks (the partial-summary writer, the cost-tracker
#: snapshotter, the L3 worktree-orphan reaper, etc.).
#:
#: ``governance`` is included so future hooks for
#: AdaptationLedger/SemanticIndex/PostureStore final-flush can
#: register co-located with the consuming code.
_TERMINATION_HOOK_PROVIDER_PACKAGES: Tuple[str, ...] = (
    "backend.core.ouroboros.battle_test",
    "backend.core.ouroboros.governance",
)


def discover_module_provided_hooks(
    registry: TerminationHookRegistry,
) -> int:
    """Walk every package in
    :data:`_TERMINATION_HOOK_PROVIDER_PACKAGES` for direct
    submodules exposing
    ``register_termination_hooks(registry) -> int``.

    Each matching module receives the registry and registers its
    own hooks via ``registry.register(...)``. New modules expose
    a ``register_termination_hooks`` callable co-located with
    the consuming code — no edits to this module required.

    NEVER raises. Per-module failures logged + skipped — the
    discovery loop must not break boot when one module's
    registration shape is wrong.

    Idempotency: if a module's ``register_termination_hooks``
    is itself idempotent (most aren't — first call registers,
    second raises ``DuplicateHookNameError``), the discovery
    loop swallows the second failure. Operators should call
    once at harness boot."""
    discovered = 0
    try:
        from importlib import import_module
        import pkgutil
        for pkg_name in _TERMINATION_HOOK_PROVIDER_PACKAGES:
            try:
                pkg_mod = import_module(pkg_name)
                pkg_path = getattr(pkg_mod, "__path__", None)
                if not pkg_path:
                    continue
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[TerminationHookRegistry] provider package "
                    "%s unavailable: %s", pkg_name, exc,
                )
                continue
            for _, name, _ispkg in pkgutil.iter_modules(pkg_path):
                full_name = f"{pkg_name}.{name}"
                # Skip self to avoid recursion.
                if full_name == __name__:
                    continue
                try:
                    mod = import_module(full_name)
                    fn = getattr(
                        mod, "register_termination_hooks", None,
                    )
                    if not callable(fn):
                        continue
                    count = fn(registry)
                    if isinstance(count, int) and count > 0:
                        discovered += count
                        logger.debug(
                            "[TerminationHookRegistry] %s "
                            "registered %d hook(s)",
                            full_name, count,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[TerminationHookRegistry] discover "
                        "skipped %s: %s",
                        full_name, exc,
                    )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[TerminationHookRegistry] "
            "discover_module_provided_hooks exc: %s", exc,
        )
    return discovered


def discover_and_register_default() -> int:
    """Convenience wrapper — calls
    :func:`discover_module_provided_hooks` against the singleton
    registry. Boot wire-up entry point. Returns count discovered.
    NEVER raises (per discovery loop's defensive contract).

    Mirrors :func:`lifecycle_hook_registry.discover_and_register_default`
    exactly."""
    try:
        return discover_module_provided_hooks(
            get_default_registry(),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[TerminationHookRegistry] "
            "discover_and_register_default degraded: %s", exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Slice 4 graduation — module-owned shipped_code_invariants + FlagRegistry
# seeds. Discovered automatically via the
# _INVARIANT_PROVIDER_PACKAGES + _FLAG_PROVIDER_PACKAGES contracts
# (Slice 4 extends both lists to include `battle_test`).
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned shipped-code invariants. Returns the list so
    the centralized seed loader can register them at boot. NEVER
    raises (returns ``[]`` on import failure — graduation soak
    path is fail-open per the established convention).

    Seven invariants pin the termination-hook arc's
    correctness-critical surfaces:

      1. ``termination_cause_vocabulary`` — the 8-value
         :class:`TerminationCause` taxonomy is frozen against
         silent expansion (Slice 1 vocabulary).
      2. ``termination_phase_vocabulary`` — the 3-value
         :class:`TerminationPhase` taxonomy frozen.
      3. ``hook_outcome_vocabulary`` — the 4-value
         :class:`HookOutcome` taxonomy frozen.
      4. ``harness_wall_clock_dispatch_present`` — the harness
         ``_monitor_wall_clock`` body MUST contain the
         ``dispatch(`` call. THIS IS THE BUG-FIX REGRESSION PIN
         — if a future refactor accidentally removes the
         dispatch call, the wall-cap path silently regresses to
         pre-Slice-3 behavior (no summary.json on disk after
         os._exit(75)).
      5. ``harness_signal_handler_dispatch_present`` — the
         ``_handle_shutdown_signal`` body MUST contain the
         dispatch call. Pristine-equivalency regression pin.
      6. ``default_adapter_writer_present`` — the adapter module
         MUST expose ``partial_summary_writer_hook`` (the only
         hook the registry installs by default).
      7. ``default_adapter_no_asyncio`` — the adapter module
         MUST NOT import asyncio. Sync-first contract pin
         (mirrors the AST-test pin from Slice 2; promoted to
         shipped invariant so it's enforced in production
         graduation soaks too).
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    def _validate_enum_vocabulary(
        tree, source, *, class_name: str, required: set,
    ) -> tuple:
        violations = []
        seen = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == class_name
            ):
                for stmt in node.body:
                    if isinstance(stmt, _ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, _ast.Name):
                                seen.add(target.id)
        missing = required - seen
        if missing:
            violations.append(
                f"{class_name} lost values: {sorted(missing)} — "
                "the closed taxonomy is frozen by Slice 4 "
                "graduation"
            )
        unexpected = seen - required - {"_generate_next_value_"}
        if unexpected:
            violations.append(
                f"{class_name} gained unpinned values: "
                f"{sorted(unexpected)} — update the AST pin AND "
                "the test suite when widening the vocabulary"
            )
        return tuple(violations)

    def _validate_termination_cause_vocab(tree, source) -> tuple:
        return _validate_enum_vocabulary(
            tree, source,
            class_name="TerminationCause",
            required={
                "WALL_CLOCK_CAP", "SIGTERM", "SIGINT", "SIGHUP",
                "IDLE_TIMEOUT", "BUDGET_EXCEEDED",
                "NORMAL_EXIT", "UNKNOWN",
            },
        )

    def _validate_termination_phase_vocab(tree, source) -> tuple:
        return _validate_enum_vocabulary(
            tree, source,
            class_name="TerminationPhase",
            required={
                "PRE_SHUTDOWN_EVENT_SET",
                "POST_ASYNC_CLEANUP",
                "PRE_HARD_EXIT",
            },
        )

    def _validate_hook_outcome_vocab(tree, source) -> tuple:
        return _validate_enum_vocabulary(
            tree, source,
            class_name="HookOutcome",
            required={"OK", "FAILED", "TIMED_OUT", "SKIPPED"},
        )

    def _validate_function_contains_dispatch(
        tree, source, *, function_name: str,
    ) -> tuple:
        """Locate ``function_name`` as a method body and assert
        its source contains ``.dispatch(`` — the registry's
        dispatch surface invocation. Catches a future refactor
        that removes the bug-fix wiring."""
        violations = []
        for node in _ast.walk(tree):
            if isinstance(
                node,
                (_ast.FunctionDef, _ast.AsyncFunctionDef),
            ) and node.name == function_name:
                # Render the function body as text + check for
                # the dispatch invocation pattern.
                try:
                    body_src = _ast.unparse(node)
                except AttributeError:
                    # ast.unparse is Python 3.9+; older
                    # interpreters get the original source slice.
                    body_src = source
                if ".dispatch(" not in body_src:
                    violations.append(
                        f"{function_name} body MUST contain "
                        ".dispatch( call — Slice 3 wired the "
                        "termination-hook registry here; "
                        "removal regresses the wall-cap "
                        "summary.json bug fix"
                    )
                return tuple(violations)
        violations.append(
            f"{function_name} not found in module — refactor "
            "renamed/removed the function the bug fix lives in"
        )
        return tuple(violations)

    def _validate_wall_clock_dispatch(tree, source) -> tuple:
        return _validate_function_contains_dispatch(
            tree, source, function_name="_monitor_wall_clock",
        )

    def _validate_signal_handler_dispatch(tree, source) -> tuple:
        return _validate_function_contains_dispatch(
            tree, source, function_name="_handle_shutdown_signal",
        )

    def _validate_adapter_hook_present(tree, source) -> tuple:
        violations = []
        if "partial_summary_writer_hook" not in source:
            violations.append(
                "default-adapter module dropped "
                "partial_summary_writer_hook — the entire "
                "Slice 3 migration target is missing"
            )
        if "PARTIAL_SUMMARY_WRITER_HOOK_NAME" not in source:
            violations.append(
                "default-adapter module dropped "
                "PARTIAL_SUMMARY_WRITER_HOOK_NAME constant — "
                "Slice 4 GET-route consumer can't pin the name"
            )
        return tuple(violations)

    def _validate_adapter_no_asyncio(tree, source) -> tuple:
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and node.module:
                if "asyncio" in node.module.split("."):
                    violations.append(
                        f"forbidden asyncio import in adapter: "
                        f"{node.module} — sync-first contract "
                        "REQUIRES no asyncio entanglement"
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if "asyncio" in alias.name.split("."):
                        violations.append(
                            f"forbidden asyncio import in "
                            f"adapter: {alias.name}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="termination_cause_vocabulary",
            target_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook.py"
            ),
            description=(
                "TerminationCause's 8-value closed taxonomy is "
                "frozen. Adding a 9th value silently breaks the "
                "_CAUSE_TO_SESSION_OUTCOME mapping in the "
                "default adapter (which has explicit coverage "
                "for all 8); update both atomically."
            ),
            validate=_validate_termination_cause_vocab,
        ),
        ShippedCodeInvariant(
            invariant_name="termination_phase_vocabulary",
            target_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook.py"
            ),
            description=(
                "TerminationPhase's 3-value closed taxonomy is "
                "frozen. Adding a 4th value requires extending "
                "the harness wire-up + dispatch budgets."
            ),
            validate=_validate_termination_phase_vocab,
        ),
        ShippedCodeInvariant(
            invariant_name="hook_outcome_vocabulary",
            target_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook.py"
            ),
            description=(
                "HookOutcome's 4-value closed taxonomy is "
                "frozen. SKIPPED is reserved for budget "
                "exhaustion; disabled-via-check is silent "
                "omission. Slice 3 §H byte-equivalency "
                "depends on this."
            ),
            validate=_validate_hook_outcome_vocab,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "harness_wall_clock_dispatch_present"
            ),
            target_file=(
                "backend/core/ouroboros/battle_test/harness.py"
            ),
            description=(
                "THE BUG-FIX REGRESSION PIN. _monitor_wall_clock "
                "MUST contain a .dispatch( call to the "
                "termination-hook registry. Slice 3 added this "
                "to fix the bt-2026-05-02-203805 reproduction "
                "(no summary.json after wall-cap → os._exit(75)). "
                "If a refactor removes this, the wall-cap path "
                "silently regresses."
            ),
            validate=_validate_wall_clock_dispatch,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "harness_signal_handler_dispatch_present"
            ),
            target_file=(
                "backend/core/ouroboros/battle_test/harness.py"
            ),
            description=(
                "Pristine-equivalency regression pin. "
                "_handle_shutdown_signal MUST contain a "
                ".dispatch( call to the termination-hook "
                "registry. Slice 3 migrated the direct "
                "_atexit_fallback_write call here; removal "
                "would un-migrate one half of the symmetric "
                "shutdown discipline."
            ),
            validate=_validate_signal_handler_dispatch,
        ),
        ShippedCodeInvariant(
            invariant_name="default_adapter_hook_present",
            target_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_default_adapters.py"
            ),
            description=(
                "Default-adapter module MUST expose "
                "partial_summary_writer_hook + "
                "PARTIAL_SUMMARY_WRITER_HOOK_NAME constant. "
                "These are the entire Slice 3 migration target; "
                "removal eliminates the partial-summary write "
                "from every termination path."
            ),
            validate=_validate_adapter_hook_present,
        ),
        ShippedCodeInvariant(
            invariant_name="default_adapter_no_asyncio",
            target_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_default_adapters.py"
            ),
            description=(
                "Sync-first contract: default-adapter module "
                "MUST NOT import asyncio. The adapter runs "
                "from contexts where the asyncio loop may be "
                "wedged (signal handlers, wall-clock watchdog "
                "tasks); any asyncio entanglement breaks the "
                "PRE_SHUTDOWN_EVENT_SET phase's threading-only "
                "execution guarantee."
            ),
            validate=_validate_adapter_no_asyncio,
        ),
    ]


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Mirrors the
    discovery contract used by Q4 P#2 closure-loop modules and
    Move 6 Quorum — the seed loader walks
    ``_FLAG_PROVIDER_PACKAGES`` (Slice 4 extends to include
    ``battle_test``) and invokes once at boot. Adding a new flag
    requires zero edits to the seed file. Returns count of
    FlagSpecs registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_TERMINATION_HOOKS_ENABLED",
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master kill switch for the TerminationHookRegistry. "
                "Default TRUE post Slice 4 graduation — termination "
                "hooks are SAFETY infrastructure (the partial-"
                "summary writer is the entire reason this arc "
                "exists). Operators flip ``=false`` for instant "
                "rollback when a misbehaving hook is interfering "
                "with shutdown. Re-read on every dispatch — flips "
                "take effect immediately for the next termination "
                "event without restart."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_registry.py"
            ),
            example="true",
            since=(
                "TerminationHookRegistry Slice 4 (2026-05-02)"
            ),
        ),
        FlagSpec(
            name="JARVIS_TERMINATION_HOOK_TIMEOUT_S",
            type=FlagType.FLOAT,
            default=DEFAULT_PER_HOOK_TIMEOUT_S,
            description=(
                "Default per-hook wall-clock timeout in seconds. "
                "Default 5.0, clamped [0.1, 30.0]. Hooks may "
                "register their own timeout via "
                "``timeout_s=`` kwarg; this is the fallback. "
                "Effective per-hook timeout at dispatch is "
                "min(this, remaining phase budget) — a single "
                "hook cannot push past the phase budget."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_registry.py"
            ),
            example="5.0",
            since=(
                "TerminationHookRegistry Slice 4 (2026-05-02)"
            ),
        ),
        FlagSpec(
            name="JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S",
            type=FlagType.FLOAT,
            default=DEFAULT_PHASE_BUDGET_S,
            description=(
                "Per-phase wall-clock budget for "
                "PRE_SHUTDOWN_EVENT_SET and POST_ASYNC_CLEANUP "
                "phases. Default 10.0, clamped [1.0, 60.0]. "
                "Once exhausted, remaining hooks recorded as "
                "SKIPPED (never started). Stays under the "
                "BoundedShutdownWatchdog's 30s grace by default."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_registry.py"
            ),
            example="10.0",
            since=(
                "TerminationHookRegistry Slice 4 (2026-05-02)"
            ),
        ),
        FlagSpec(
            name="JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S",
            type=FlagType.FLOAT,
            default=DEFAULT_HARD_EXIT_PHASE_BUDGET_S,
            description=(
                "Per-phase budget for the PRE_HARD_EXIT phase "
                "(invoked by BoundedShutdownWatchdog "
                "immediately before os._exit). Default 2.0, "
                "clamped [0.5, 10.0]. Tighter than the regular "
                "phase budget because the watchdog deadline is "
                "in single-digit seconds at this point. Use for "
                "last-mile forensic emission, NOT full state "
                "flush (that belongs in PRE_SHUTDOWN_EVENT_SET)."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_registry.py"
            ),
            example="2.0",
            since=(
                "TerminationHookRegistry Slice 4 (2026-05-02)"
            ),
        ),
        FlagSpec(
            name="JARVIS_TERMINATION_HOOK_MAX_PER_PHASE",
            type=FlagType.INT,
            default=16,
            description=(
                "Capacity per phase. Default 16, clamped "
                "[1, 256]. Defends against misconfigured "
                "plugins registering thousands of hooks (matches "
                "LifecycleHookRegistry's discipline). Capacity "
                "exceeded raises HookCapacityExceededError at "
                "register-time — surfaces operator misconfig at "
                "boot, not at fire-time."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/battle_test/"
                "termination_hook_registry.py"
            ),
            example="16",
            since=(
                "TerminationHookRegistry Slice 4 (2026-05-02)"
            ),
        ),
    ]
    try:
        registry.bulk_register(specs, override=True)
    except Exception:  # noqa: BLE001 — defensive
        return 0
    return len(specs)


__all__ = [
    "DuplicateHookNameError",
    "HookCapacityExceededError",
    "InvalidHookError",
    "TERMINATION_HOOK_REGISTRY_SCHEMA_VERSION",
    "TerminationHookCallable",
    "TerminationHookRegistration",
    "TerminationHookRegistry",
    "TerminationHookRegistryError",
    "discover_and_register_default",
    "discover_module_provided_hooks",
    "get_default_registry",
    "master_enabled",
    "max_hooks_per_phase",
    "per_hook_timeout_s",
    "phase_budget_s",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_registry_for_tests",
]
