"""Lifecycle Hook Registry — Slice 2 sync registry.

Per-process registry of operator-defined hook callables. Thread-safe.
Capacity-limited per event. Listener pattern for observability
bridges (Slice 4 attaches SSE here). Dynamic discovery via
module-owned ``register_lifecycle_hooks(registry)`` mirroring the
Priority #6 closure module-owned-registration contract.

Architectural reuse — three existing surfaces compose with ZERO
duplication:

  * Slice 1 :class:`HookContext` / :class:`HookResult` /
    :class:`LifecycleEvent` / :class:`HookOutcome` — the closed
    taxonomies. Registry consumes them; executors emit them.
  * :class:`InlinePromptController` (``inline_permission_prompt.py``)
    — same threading.RLock + listener pattern + bounded snapshot
    surface. Registry mirrors the operational discipline (NEVER
    raises out, on_transition returns unsubscribe handle, reset
    for tests).
  * Module-owned discovery contract (Priority #6 closure) —
    ``discover_module_provided_hooks(registry)`` walks
    ``_HOOK_PROVIDER_PACKAGES`` and calls
    ``register_lifecycle_hooks(registry)`` on any module exposing
    it. Modules own their hooks co-located with the consuming
    code; no edits to this module required when new modules add
    hooks.

The registry is a hybrid:
  * Persistent like :class:`FlagRegistry` (registrations live
    until unregistered or process death).
  * Listener-bridged like :class:`InlinePromptController`
    (registrations fire SSE-bridge listeners on add/remove).
  * NOT Future-backed (hooks fire many times per registration —
    the per-call result lives in the executor, not the registry).

Direct-solve principles
-----------------------

* **Asynchronous-ready** — registry operations are sync (called
  at boot or from REPL); Slice 3 executor wraps the callable
  invocation in ``asyncio.to_thread``.
* **Dynamic** — ``max_per_event`` flows from Slice 1's env-knob
  (re-read at registry construction). No hardcoded magic.
* **Adaptive** — duplicate name re-register raises explicitly
  (the one documented exception case — operator misconfig that
  must surface). Capacity exceeded raises explicitly. Garbage
  inputs (non-callable, non-event) raise explicitly. All other
  paths NEVER raise.
* **Intelligent** — registrations are priority-sorted on
  for_event() lookup so the executor sees a stable ordering
  without re-sorting per call.
* **Robust** — listener failures swallowed (matching
  InlinePromptController's ``_fire`` discipline so a broken SSE
  bridge can't block registrations).
* **No hardcoding** — discovery package list curated once;
  hooks live with the consuming code.

Authority invariants (AST-pinned by Slice 5):

* MAY import: ``lifecycle_hook`` (Slice 1 primitive). Module-owned
  ``register_flags`` / ``register_shipped_invariants`` exempt
  from import allowlist (registration-contract exemption).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, List, Optional, Protocol, Tuple,
    runtime_checkable,
)

from backend.core.ouroboros.governance.lifecycle_hook import (
    HookContext,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    default_hook_timeout_s,
    make_hook_result,
    max_hooks_per_event,
)

logger = logging.getLogger(__name__)


LIFECYCLE_HOOK_REGISTRY_SCHEMA_VERSION: str = (
    "lifecycle_hook_registry.1"
)


# ---------------------------------------------------------------------------
# LifecycleHookCallable Protocol — what the registry stores
# ---------------------------------------------------------------------------


@runtime_checkable
class LifecycleHookCallable(Protocol):
    """Operator-defined hook. Sync callable (Slice 3 executor
    wraps in ``asyncio.to_thread`` so the event loop never
    blocks).

    Hooks SHOULD return via :func:`make_hook_result` so Phase C
    tightening stamping stays consistent. Hooks that raise are
    caught by the executor and converted to ``HookResult`` with
    outcome=FAILED.

    Hooks MUST NOT mutate the :class:`HookContext` payload —
    propagation across hooks shares the same context by reference.
    Read-only treatment is enforced by the frozen dataclass shape.
    """

    def __call__(self, context: HookContext) -> HookResult:
        ...  # Protocol


# ---------------------------------------------------------------------------
# Frozen registration record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRegistration:
    """One registered hook entry. Frozen for safe propagation
    across snapshot / projection surfaces. The callable itself is
    bound at registration; ``priority`` controls execution order
    (lower-priority value = earlier execution); ``timeout_s``
    bounds the per-call wall-clock; ``enabled_check`` is an
    optional per-hook gate (for hooks that want feature-flag
    visibility independent of the master switch)."""

    name: str
    event: LifecycleEvent
    callable: LifecycleHookCallable
    priority: int = 100
    timeout_s: float = 5.0
    enabled_check: Optional[Callable[[], bool]] = None
    registered_ts: float = 0.0
    schema_version: str = LIFECYCLE_HOOK_REGISTRY_SCHEMA_VERSION

    def is_enabled(self) -> bool:
        """Returns True iff the hook's per-hook enabled_check
        (if any) returns True. Master flag is separate — checked
        upstream by the executor. NEVER raises."""
        if self.enabled_check is None:
            return True
        try:
            return bool(self.enabled_check())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[LifecycleHookRegistry] enabled_check %s raised: "
                "%s — treating as disabled", self.name, exc,
            )
            return False

    def to_projection(self) -> Dict[str, Any]:
        """Bounded read-only projection for snapshot / SSE.
        Excludes the callable + enabled_check (not serializable;
        operators see them by name in audit)."""
        return {
            "name": self.name,
            "event": self.event.value,
            "priority": self.priority,
            "timeout_s": self.timeout_s,
            "registered_ts": self.registered_ts,
            "has_enabled_check": self.enabled_check is not None,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LifecycleHookRegistryError(Exception):
    """Base for registry errors."""


class DuplicateHookNameError(LifecycleHookRegistryError):
    """Operator misconfig: same name registered twice. Explicitly
    raised so misconfig surfaces at boot, not at hook-fire-time."""


class HookCapacityExceededError(LifecycleHookRegistryError):
    """A single event has more hooks than max_hooks_per_event
    permits. Explicitly raised; defends against misconfigured
    plugins registering thousands of hooks."""


class InvalidHookError(LifecycleHookRegistryError):
    """The supplied hook fails type / shape validation."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class LifecycleHookRegistry:
    """Per-process registry of operator-defined hook callables.

    Thread-safe via ``threading.RLock``. Capacity-limited per event
    (env-knob, default 16). Listener pattern for observability
    bridges (Slice 4 attaches SSE here). NEVER raises out of
    snapshot / for_event / unregister methods; raises explicitly
    on register() validation failures (the one documented
    exception class)."""

    def __init__(
        self,
        *,
        max_per_event: Optional[int] = None,
    ) -> None:
        try:
            cap = (
                max_per_event if max_per_event is not None
                else max_hooks_per_event()
            )
            self._max_per_event = max(1, min(256, int(cap)))
        except (TypeError, ValueError):
            self._max_per_event = max_hooks_per_event()
        self._by_event: Dict[LifecycleEvent, List[HookRegistration]] = {}
        self._by_name: Dict[str, HookRegistration] = {}
        self._lock = threading.RLock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- introspection ---------------------------------------------------

    @property
    def max_per_event(self) -> int:
        return self._max_per_event

    def total_count(self) -> int:
        with self._lock:
            return len(self._by_name)

    def count_for_event(self, event: LifecycleEvent) -> int:
        with self._lock:
            return len(self._by_event.get(event, []))

    def for_event(
        self, event: LifecycleEvent,
    ) -> Tuple[HookRegistration, ...]:
        """Priority-ordered tuple of registrations for one event.
        Sort happens at registration time (insertion-sorted) so
        this lookup is O(N) tuple copy. NEVER raises."""
        try:
            if not isinstance(event, LifecycleEvent):
                return ()
            with self._lock:
                bucket = self._by_event.get(event, [])
                return tuple(bucket)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[LifecycleHookRegistry] for_event %r degraded: %s",
                event, exc,
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
                return reg.to_projection() if reg is not None else None
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

    # --- listeners -------------------------------------------------------

    def on_transition(
        self,
        listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Subscribe a listener for register/unregister events.
        Returns an unsubscribe handle. Listener payload shape:
        ``{"event_type": "hook_registered" | "hook_unregistered",
        "projection": <to_projection() dict>}``.
        Mirrors InlinePromptController's listener contract."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(
        self,
        event_type: str,
        registration: HookRegistration,
    ) -> None:
        """Sync-fire all listeners with the projection. Listener
        exceptions swallowed so a broken SSE bridge can't block
        registrations. Mirrors InlinePromptController._fire."""
        payload = {
            "event_type": event_type,
            "projection": registration.to_projection(),
        }
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[LifecycleHookRegistry] listener exception: %s",
                    exc,
                )

    # --- mutation --------------------------------------------------------

    def register(
        self,
        event: LifecycleEvent,
        hook: LifecycleHookCallable,
        *,
        name: str,
        priority: int = 100,
        timeout_s: Optional[float] = None,
        enabled_check: Optional[Callable[[], bool]] = None,
    ) -> HookRegistration:
        """Register one hook. Raises explicitly on validation
        failure so operator misconfig surfaces at boot.

        Raises:
          :class:`InvalidHookError` — non-callable hook, non-event,
            empty name.
          :class:`DuplicateHookNameError` — name already registered.
          :class:`HookCapacityExceededError` — event already at
            max_per_event capacity.
        """
        # Validate event.
        if not isinstance(event, LifecycleEvent):
            raise InvalidHookError(
                f"event must be a LifecycleEvent — got "
                f"{type(event).__name__}"
            )
        # Validate hook callable.
        if not callable(hook):
            raise InvalidHookError(
                f"hook must be callable — got {type(hook).__name__}"
            )
        # Validate name.
        clean_name = str(name or "").strip()
        if not clean_name:
            raise InvalidHookError(
                "hook name must be a non-empty string"
            )
        if len(clean_name) > 128:
            clean_name = clean_name[:128]
        # Defensive: priority + timeout coercion.
        try:
            clean_priority = int(priority)
        except (TypeError, ValueError):
            clean_priority = 100
        try:
            clean_timeout = (
                float(timeout_s) if timeout_s is not None
                else default_hook_timeout_s()
            )
            clean_timeout = max(0.1, min(60.0, clean_timeout))
        except (TypeError, ValueError):
            clean_timeout = default_hook_timeout_s()

        registration = HookRegistration(
            name=clean_name,
            event=event,
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
                    f"hook name {clean_name!r} already registered"
                )
            # Capacity check.
            bucket = self._by_event.setdefault(event, [])
            if len(bucket) >= self._max_per_event:
                raise HookCapacityExceededError(
                    f"event {event.value!r} at capacity "
                    f"({self._max_per_event} hooks)"
                )
            # Insertion-sort by priority so for_event() is O(N) copy.
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
            "[LifecycleHookRegistry] registered hook=%s event=%s "
            "priority=%d timeout_s=%.1f",
            clean_name, event.value, clean_priority, clean_timeout,
        )
        self._fire("hook_registered", registration)
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
                bucket = self._by_event.get(reg.event, [])
                if reg in bucket:
                    bucket.remove(reg)
            logger.info(
                "[LifecycleHookRegistry] unregistered hook=%s "
                "event=%s", clean_name, reg.event.value,
            )
            self._fire("hook_unregistered", reg)
            return True
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[LifecycleHookRegistry] unregister %r raised: %s",
                name, exc,
            )
            return False

    def reset(self) -> None:
        """Test helper — drops all registrations + listeners.
        NEVER called from production. NEVER raises."""
        with self._lock:
            self._by_event.clear()
            self._by_name.clear()
            self._listeners.clear()


# ---------------------------------------------------------------------------
# Singleton accessor — production wire-up entry point
# ---------------------------------------------------------------------------


_default_registry: Optional[LifecycleHookRegistry] = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> LifecycleHookRegistry:
    """Process-global :class:`LifecycleHookRegistry` instance.
    Lazy-constructed on first call. Used by Slice 3 executor +
    Slice 4 orchestrator wire-up so callers don't have to thread
    their own registry through the call chain."""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = LifecycleHookRegistry()
        return _default_registry


def reset_default_registry_for_tests() -> None:
    """Test helper — drops the singleton so the next get_default_*
    call constructs a fresh instance. NEVER called from production."""
    global _default_registry
    with _default_registry_lock:
        _default_registry = None


# ---------------------------------------------------------------------------
# Module-owned-hooks discovery loop (mirrors Priority #6 contract)
# ---------------------------------------------------------------------------

#: Curated package list — modules in these packages may expose
#: ``register_lifecycle_hooks(registry)`` and have their hooks
#: discovered automatically at boot. Add a package here to opt
#: in to discovery; modules in non-listed packages register
#: explicitly via :meth:`LifecycleHookRegistry.register`.
_HOOK_PROVIDER_PACKAGES: Tuple[str, ...] = (
    "backend.core.ouroboros.governance",
    "backend.core.ouroboros.governance.verification",
)


def discover_module_provided_hooks(
    registry: LifecycleHookRegistry,
) -> int:
    """Walk every package in :data:`_HOOK_PROVIDER_PACKAGES` for
    direct submodules exposing
    ``register_lifecycle_hooks(registry) -> int``.

    Each matching module receives the registry and registers its
    own hooks via ``registry.register(...)``. New modules expose
    a ``register_lifecycle_hooks`` callable co-located with the
    consuming code — no edits to this module required.

    NEVER raises. Per-module failures logged + skipped.

    The contract scales organically: when a new module owns a new
    hook surface, it owns the registration too."""
    discovered = 0
    try:
        from importlib import import_module
        import pkgutil
        for pkg_name in _HOOK_PROVIDER_PACKAGES:
            try:
                pkg_mod = import_module(pkg_name)
                pkg_path = getattr(pkg_mod, "__path__", None)
                if not pkg_path:
                    continue
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[LifecycleHookRegistry] provider package %s "
                    "unavailable: %s", pkg_name, exc,
                )
                continue
            for _, name, _ispkg in pkgutil.iter_modules(pkg_path):
                full_name = f"{pkg_name}.{name}"
                if full_name == __name__:
                    continue
                try:
                    mod = import_module(full_name)
                    fn = getattr(mod, "register_lifecycle_hooks", None)
                    if not callable(fn):
                        continue
                    count = fn(registry)
                    if isinstance(count, int) and count > 0:
                        discovered += count
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[LifecycleHookRegistry] discover skipped "
                        "%s: %s", full_name, exc,
                    )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[LifecycleHookRegistry] discover_module_provided_hooks "
            "exc: %s", exc,
        )
    return discovered


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "DuplicateHookNameError",
    "HookCapacityExceededError",
    "HookRegistration",
    "InvalidHookError",
    "LIFECYCLE_HOOK_REGISTRY_SCHEMA_VERSION",
    "LifecycleHookCallable",
    "LifecycleHookRegistry",
    "LifecycleHookRegistryError",
    "discover_module_provided_hooks",
    "get_default_registry",
    "reset_default_registry_for_tests",
]
