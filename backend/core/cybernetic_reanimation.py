"""Cybernetic Reanimation — the asynchronous resilience nervous system (Spec 2).

Wires the surviving resilience organs to the live event stream WITHOUT coupling
to the 102K-line kernel: every collaborator is duck-typed (any bus with
``subscribe``/``emit``, any organ exposing an async ``on_pressure`` handler), so
this module is unit-testable with fakes and never triggers the kernel's
import-time side effects.

Three parts:
  * ``PressureSignalEmitter`` — turns raw, level-based observations into typed,
    EDGE-TRIGGERED signals. A signal fires only on a state TRANSITION (rising:
    became-active; falling: cleared), never on every poll — so a sustained
    pressure does not spam the organs.
  * ``EventActivationDispatcher`` — subscribes to the SupervisorEventBus and
    routes pressure signals to the registered organs entirely asynchronously and
    fail-soft (one slow/broken organ never blocks or breaks the others).
  * Shadow Mode (``JARVIS_RESILIENCE_SHADOW_MODE``, default-TRUE) — the fail-safe.
    A reanimated organ wraps every DANGEROUS action (process kill, load shed) in
    ``shadow_guard``: in shadow mode it logs ``[SHADOW MODE] Would have ...`` and
    yields the trapped sentinel instead of executing. The muscle wakes and
    reasons, but cannot act on the world until the operator clears shadow mode.

Pure/async, env-driven, NEVER raises on the dispatch hot path.
"""
from __future__ import annotations

import inspect
import logging
import os
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("jarvis.cybernetic_reanimation")

_ENV_SHADOW = "JARVIS_RESILIENCE_SHADOW_MODE"

# Slice 252 — the pressure signal currently being dispatched to an organ. The
# dispatcher sets it before invoking a handler so shadow_guard (called deep
# inside the organ, with no signal in scope) can attribute a trap to its
# triggering signal WITHOUT threading the signal through every kernel method.
# Async-safe (ContextVar is per-task).
_current_signal_var: "ContextVar[Optional[Any]]" = ContextVar(
    "jarvis_reanimation_current_signal", default=None,
)


class PressureSignalType(str, Enum):
    """The typed, edge-triggered signals the nervous system carries."""
    RESOURCE_PRESSURE = "resource_pressure"      # CPU/mem/fd headroom crossed a threshold
    ANOMALY_DETECTED = "anomaly_detected"        # behavioural anomaly / health breach
    COMPONENT_DEGRADED = "component_degraded"    # a subsystem reported degraded


class SignalEdge(str, Enum):
    RISING = "rising"    # condition became active
    FALLING = "falling"  # condition cleared


@dataclass(frozen=True)
class PressureSignal:
    """An immutable, edge-triggered pressure signal."""
    type: PressureSignalType
    source: str
    edge: SignalEdge
    severity: str = "warning"          # info | warning | critical
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shadow Mode — the fail-safe
# ---------------------------------------------------------------------------

class _ShadowTrapped:
    """Singleton sentinel returned by shadow_guard when an action is trapped."""
    __slots__ = ()
    def __repr__(self) -> str:  # noqa: D401
        return "<SHADOW_TRAPPED>"


SHADOW_TRAPPED = _ShadowTrapped()


def resilience_shadow_mode_enabled() -> bool:
    """Master fail-safe (default-TRUE). While on, reanimated organs reason but do
    NOT execute world-affecting actions. NEVER raises."""
    try:
        return os.getenv(_ENV_SHADOW, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return True  # fail SAFE — default to shadow on error


# ---------------------------------------------------------------------------
# Slice 253 — Active Shadow Endorsement & HITL Gateway (the return channel)
#
# Passive telemetry (Slice 252) tells the Host what the muscle WOULD have done.
# This is the bidirectional half: when shadow_guard traps a dangerous action it
# STASHES the original callable (a live in-process closure) keyed by a unique
# action_id. The Host can later ENDORSE that one action_id — the closure is
# re-hydrated and executed for a single run, bypassing the shadow block for THAT
# action only, while the global JARVIS_RESILIENCE_SHADOW_MODE shield stays UP.
#
# Bounded + TTL'd: a pending action that is never endorsed expires (a stale kill
# must NOT fire late) and the registry is capacity-capped (oldest evicted). The
# stash is in-process only — closures are not serializable, so an un-endorsed
# action does not survive a process restart (by design: a restart is a clean
# slate, not a queue of pending world-mutations).
# ---------------------------------------------------------------------------

_ENV_PENDING_MAX = "JARVIS_SHADOW_PENDING_MAX"
_ENV_PENDING_TTL_S = "JARVIS_SHADOW_PENDING_TTL_S"
_DEFAULT_PENDING_MAX = 64
_DEFAULT_PENDING_TTL_S = 300.0


def _pending_max() -> int:
    try:
        return max(1, int(os.getenv(_ENV_PENDING_MAX, str(_DEFAULT_PENDING_MAX))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_PENDING_MAX


def _pending_ttl_s() -> float:
    try:
        return max(0.0, float(os.getenv(_ENV_PENDING_TTL_S, str(_DEFAULT_PENDING_TTL_S))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_PENDING_TTL_S


def _current_signal_repr() -> str:
    """Compact repr of the dispatcher-set triggering signal (ContextVar), for
    stashing alongside a pending action. NEVER raises."""
    try:
        sig = _current_signal_var.get()
        if sig is None:
            return ""
        return f"{sig.type.value}:{sig.source}:{sig.edge.value}"
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class PendingShadowAction:
    """A trapped dangerous action awaiting endorsement. Holds the live in-process
    callable so it can be re-hydrated and executed for one run."""
    action_id: str
    organ: str
    action_desc: str
    execute: Callable[[], Any]
    is_coro: bool
    signal_repr: str
    created_monotonic: float


@dataclass(frozen=True)
class EndorsementResult:
    """Terminal outcome of an endorsement attempt. ``status`` is one of:
    executed | declined | not_found | expired | error."""
    status: str
    action_id: str
    organ: str = ""
    intended_action: str = ""
    result: Any = None
    error: str = ""


class PendingShadowActionRegistry:
    """Bounded, TTL-aware store of trapped actions awaiting endorsement. Pure +
    in-process; NEVER raises on the register/pop hot path."""

    def __init__(self) -> None:
        self.entries: "OrderedDict[str, PendingShadowAction]" = OrderedDict()
        self._seq = 0

    def register(
        self,
        *,
        organ: str,
        action_desc: str,
        execute: Callable[[], Any],
        is_coro: bool,
        signal_repr: str = "",
    ) -> str:
        """Stash a trapped action; returns its unique action_id. Evicts the
        oldest entry when the capacity cap is exceeded."""
        self._seq += 1
        action_id = f"shadow-{self._seq:06d}"
        self.entries[action_id] = PendingShadowAction(
            action_id=action_id,
            organ=str(organ),
            action_desc=str(action_desc),
            execute=execute,
            is_coro=bool(is_coro),
            signal_repr=str(signal_repr),
            created_monotonic=time.monotonic(),
        )
        # Bound the registry — drop oldest beyond the cap (FIFO).
        cap = _pending_max()
        while len(self.entries) > cap:
            self.entries.popitem(last=False)
        return action_id

    def pop(self, action_id: str) -> Optional[PendingShadowAction]:
        return self.entries.pop(action_id, None)

    def reset(self) -> None:
        self.entries.clear()
        self._seq = 0


_PENDING = PendingShadowActionRegistry()


def reset_pending_shadow_actions() -> None:
    """Clear all pending trapped actions (test seam + clean-slate on boot)."""
    _PENDING.reset()


def pending_shadow_action_count() -> int:
    return len(_PENDING.entries)


def pending_shadow_action_ids() -> List[str]:
    """The action_ids currently awaiting endorsement (oldest first)."""
    return list(_PENDING.entries.keys())


# ---------------------------------------------------------------------------
# Slice 254 — trap observers: the in-process subscription to SHADOW_ACTION_TRAPPED
#
# A consumer (e.g. the diagnostic swarm) registers a callback that fires — fully
# fail-soft — every time shadow_guard traps an action, receiving the same payload
# shape as the telemetry event. This is the decoupled, kernel-free equivalent of
# subscribing to the event on the SupervisorEventBus: the trap chokepoint is the
# single source of truth, so observers see exactly what the broker sees, in-proc.
# Observers MUST be non-blocking (return immediately, schedule any real work).
# ---------------------------------------------------------------------------

_TRAP_OBSERVERS: "List[Callable[[Dict[str, Any]], Any]]" = []


def register_trap_observer(callback: Callable[[Dict[str, Any]], Any]) -> None:
    """Subscribe a callback to SHADOW_ACTION_TRAPPED. Idempotent per callable."""
    if callback not in _TRAP_OBSERVERS:
        _TRAP_OBSERVERS.append(callback)


def unregister_trap_observer(callback: Callable[[Dict[str, Any]], Any]) -> None:
    try:
        _TRAP_OBSERVERS.remove(callback)
    except ValueError:
        pass


def reset_trap_observers() -> None:
    _TRAP_OBSERVERS.clear()


def _notify_trap_observers(payload: Dict[str, Any]) -> None:
    """Fan a trap out to every observer. NEVER raises; one bad observer never
    blocks the trap path or the others."""
    for cb in list(_TRAP_OBSERVERS):
        try:
            cb(payload)
        except Exception:  # noqa: BLE001 — an observer must never break the trap
            logger.debug("[Reanimation] trap observer raised", exc_info=True)


async def endorse_shadow_action(action_id: str) -> EndorsementResult:
    """Re-hydrate and execute ONE trapped action for the given ``action_id`` —
    bypassing the shadow block for this single run WITHOUT touching the global
    JARVIS_RESILIENCE_SHADOW_MODE shield. One-shot (the entry is popped, so it
    cannot double-fire). Rejects unknown ids and expired (TTL'd) entries — a
    stale kill must never fire late. Fail-soft: an exception in the underlying
    action yields status="error", never propagates. Publishes an audit event."""
    entry = _PENDING.pop(action_id)
    if entry is None:
        return EndorsementResult(status="not_found", action_id=action_id)

    ttl = _pending_ttl_s()
    if ttl > 0 and (time.monotonic() - entry.created_monotonic) > ttl:
        _emit_endorsement(entry, "expired")
        return EndorsementResult(
            status="expired", action_id=action_id, organ=entry.organ,
            intended_action=entry.action_desc,
        )

    try:
        out = entry.execute()
        if entry.is_coro or inspect.isawaitable(out):
            out = await out
        _emit_endorsement(entry, "executed")
        return EndorsementResult(
            status="executed", action_id=action_id, organ=entry.organ,
            intended_action=entry.action_desc, result=out,
        )
    except Exception as exc:  # noqa: BLE001 — endorsement must never crash the Host
        logger.exception(
            "[Reanimation] endorsed action %s (%s) raised", action_id, entry.organ,
        )
        _emit_endorsement(entry, "error")
        return EndorsementResult(
            status="error", action_id=action_id, organ=entry.organ,
            intended_action=entry.action_desc, error=str(exc)[:256],
        )


def _emit_endorsement(entry: PendingShadowAction, outcome: str) -> None:
    """Publish the endorsement audit event. Fail-soft, lazy import, NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_endorse_shadow_action as _publish,
        )
        _publish(
            action_id=entry.action_id,
            organ_name=entry.organ,
            intended_action=entry.action_desc,
            outcome=outcome,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Reanimation] endorsement telemetry emit skipped", exc_info=True)


def endorsement_prompt_for(payload: Dict[str, Any]) -> str:
    """Render the non-blocking HITL prompt the CLI/TUI shows the Host when a
    SHADOW_ACTION_TRAPPED event arrives. Pure (TTY-free, testable)."""
    organ = str(payload.get("organ_name", "") or "?")
    action = str(payload.get("intended_action", "") or "?")
    aid = str(payload.get("action_id", "") or "?")
    return f"[SHADOW] {organ} would {action} (id={aid}). Endorse Execution? [Y/N]"


async def handle_endorsement_choice(action_id: str, choice: str) -> EndorsementResult:
    """Apply the Host's CLI/TUI decision. 'y'/'yes' endorses (re-hydrate+execute);
    anything else declines (the action stays pending until its TTL lapses, so the
    Host can still endorse it later). The IN-PROCESS dispatch target a REPL verb
    or trusted command channel calls — deliberately NOT the read-only
    /observability surface (executing a kill is an authority action)."""
    if str(choice).strip().lower() in ("y", "yes"):
        return await endorse_shadow_action(action_id)
    entry = _PENDING.entries.get(action_id)
    return EndorsementResult(
        status="declined", action_id=action_id,
        organ=entry.organ if entry else "",
        intended_action=entry.action_desc if entry else "",
    )


def emit_shadow_trap(
    organ_name: str,
    intended_action: str,
    triggering_signal: Any = None,
    action_id: str = "",
) -> None:
    """Slice 252 — publish a SHADOW_ACTION_TRAPPED telemetry event to the
    StreamEventBroker when a dangerous action is trapped, giving the Sovereign
    Host real-time structured audit (organ + action + signal) instead of a text
    log. ``triggering_signal`` defaults to the dispatcher-set ContextVar so the
    deep guard call-site needs no signal in scope. Out-of-band + fail-soft + lazy
    import (keeps this module decoupled from the broker). NEVER raises."""
    try:
        sig = triggering_signal if triggering_signal is not None else _current_signal_var.get()
        sig_repr = ""
        if sig is not None:
            try:
                sig_repr = f"{sig.type.value}:{sig.source}:{sig.edge.value}"
            except Exception:  # noqa: BLE001
                sig_repr = str(sig)[:128]
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_shadow_action_trapped as _publish,
        )
        _publish(
            organ_name=str(organ_name),
            intended_action=str(intended_action),
            triggering_signal=sig_repr,
            action_id=str(action_id),
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort, never the gate
        logger.debug("[Reanimation] shadow-trap telemetry emit skipped", exc_info=True)


def shadow_guard(
    action_desc: str,
    execute: Callable[[], Any],
    *,
    organ: str = "",
    log: Optional[logging.Logger] = None,
) -> Any:
    """Gate a DANGEROUS action behind shadow mode. In shadow mode: log
    ``[SHADOW MODE] Would have <action_desc>``, publish a SHADOW_ACTION_TRAPPED
    telemetry event, and return ``SHADOW_TRAPPED`` WITHOUT calling ``execute``.
    Otherwise: return ``execute()``. The single chokepoint every reanimated organ
    routes its kill/shed/restart through."""
    _log = log or logger
    if resilience_shadow_mode_enabled():
        _log.warning("[SHADOW MODE] Would have %s", action_desc)
        # Slice 253 — stash the live callable so the Host can later endorse it.
        sig_repr = _current_signal_repr()
        action_id = _PENDING.register(
            organ=organ, action_desc=action_desc, execute=execute, is_coro=False,
            signal_repr=sig_repr,
        )
        emit_shadow_trap(organ, action_desc, action_id=action_id)
        _notify_trap_observers({
            "organ_name": organ, "intended_action": action_desc,
            "triggering_signal": sig_repr, "action_id": action_id, "op_id": "",
        })
        return SHADOW_TRAPPED
    return execute()


async def shadow_guard_async(
    action_desc: str,
    execute: Callable[[], Awaitable[Any]],
    *,
    organ: str = "",
    log: Optional[logging.Logger] = None,
) -> Any:
    """Async variant of :func:`shadow_guard` for coroutine actions."""
    _log = log or logger
    if resilience_shadow_mode_enabled():
        _log.warning("[SHADOW MODE] Would have %s", action_desc)
        # Slice 253 — stash the live coroutine factory for endorsement.
        sig_repr = _current_signal_repr()
        action_id = _PENDING.register(
            organ=organ, action_desc=action_desc, execute=execute, is_coro=True,
            signal_repr=sig_repr,
        )
        emit_shadow_trap(organ, action_desc, action_id=action_id)
        _notify_trap_observers({
            "organ_name": organ, "intended_action": action_desc,
            "triggering_signal": sig_repr, "action_id": action_id, "op_id": "",
        })
        return SHADOW_TRAPPED
    return await execute()


# ---------------------------------------------------------------------------
# Pure sampler predicate — extracted for unit testing the producer's logic
# ---------------------------------------------------------------------------

def _pressure_active(
    mem: float,
    cpu: float,
    mem_thr: float,
    cpu_thr: float,
) -> bool:
    """Return True when EITHER normalized resource (0.0-1.0) is at-or-above its
    threshold. The pure predicate the live pressure sampler feeds into
    :meth:`PressureSignalEmitter.observe` — extracted so the producer's
    threshold/edge logic is unit-testable without the 102K-line kernel. NEVER
    raises (non-numeric inputs coerce to the safe ``False`` floor)."""
    try:
        return (float(mem) >= float(mem_thr)) or (float(cpu) >= float(cpu_thr))
    except Exception:  # noqa: BLE001 — sampler must never throw into the loop
        return False


# ---------------------------------------------------------------------------
# PressureSignalEmitter — level observations -> edge-triggered signals
# ---------------------------------------------------------------------------

class PressureSignalEmitter:
    """Converts level-based observations into edge-triggered :class:`PressureSignal`
    emissions. ``emit_fn`` is any callable taking a PressureSignal (e.g. the
    SupervisorEventBus.emit bridge). State is tracked per (type, source) so a
    sustained condition fires exactly one RISING edge and, on clearing, one
    FALLING edge."""

    def __init__(self, emit_fn: Callable[[PressureSignal], Any]) -> None:
        self._emit = emit_fn
        self._active: Dict[Tuple[PressureSignalType, str], bool] = {}

    def observe(
        self,
        signal_type: PressureSignalType,
        source: str,
        active: bool,
        *,
        severity: str = "warning",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[PressureSignal]:
        """Record a level observation. Emit + return a signal ONLY on an edge
        (transition); return None when the state is unchanged. NEVER raises into
        the caller (a broken emit_fn is swallowed after the state update)."""
        key = (signal_type, source)
        was = self._active.get(key, False)
        if bool(active) == was:
            return None  # no edge — stay silent
        self._active[key] = bool(active)
        sig = PressureSignal(
            type=signal_type,
            source=source,
            edge=SignalEdge.RISING if active else SignalEdge.FALLING,
            severity=severity,
            detail=dict(detail or {}),
        )
        try:
            self._emit(sig)
        except Exception:  # noqa: BLE001 — emission is best-effort
            logger.debug("[Reanimation] emit_fn raised for %s", sig, exc_info=True)
        return sig

    def reset(self) -> None:
        self._active.clear()


# ---------------------------------------------------------------------------
# EventActivationDispatcher — route signals to reanimated organs (async)
# ---------------------------------------------------------------------------

OrganHandler = Callable[[PressureSignal], Awaitable[Any]]


class EventActivationDispatcher:
    """Routes :class:`PressureSignal` s to registered resilience organs,
    asynchronously and fail-soft. Each organ subscribes to the signal types it
    cares about; an organ that raises or hangs never breaks the others."""

    def __init__(self) -> None:
        self._organs: Dict[PressureSignalType, List[Tuple[str, OrganHandler]]] = {}

    def register_organ(
        self,
        name: str,
        handler: OrganHandler,
        signal_types: List[PressureSignalType],
    ) -> None:
        """Wake an organ: route the given signal types to its async handler."""
        for st in signal_types:
            self._organs.setdefault(st, []).append((name, handler))
        logger.info(
            "[Reanimation] organ %s registered for %s",
            name, [s.value for s in signal_types],
        )

    def organ_count(self) -> int:
        return len({n for hs in self._organs.values() for (n, _) in hs})

    async def dispatch(self, signal: PressureSignal) -> int:
        """Deliver a signal to every organ subscribed to its type. Returns the
        number of organs invoked. Fail-soft: per-organ exceptions are logged +
        swallowed. NEVER raises."""
        handlers = list(self._organs.get(signal.type, ()))
        delivered = 0
        for name, handler in handlers:
            # Slice 252 — expose the triggering signal to shadow_guard (deep in
            # the organ) via the ContextVar, so a trapped action is attributed to
            # the signal that woke it. Reset after each handler (per-organ scope).
            _token = _current_signal_var.set(signal)
            try:
                await handler(signal)
                delivered += 1
            except Exception:  # noqa: BLE001 — one organ never breaks the bus
                logger.exception("[Reanimation] organ %s raised on %s", name, signal.type)
            finally:
                _current_signal_var.reset(_token)
        return delivered

    def attach_to_bus(self, bus: Any, *, extract: Callable[[Any], Optional[PressureSignal]]) -> None:
        """Bridge a SupervisorEventBus to this dispatcher. ``extract`` maps a raw
        bus event to a PressureSignal (or None to ignore). The subscription is
        non-blocking; dispatch is scheduled on the running loop. NEVER raises."""
        import asyncio

        def _on_event(event: Any) -> None:
            try:
                sig = extract(event)
                if sig is None:
                    return
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return  # no loop — bus events only meaningful under the async kernel
                loop.create_task(self.dispatch(sig))
            except Exception:  # noqa: BLE001
                logger.debug("[Reanimation] bus bridge swallowed an event", exc_info=True)

        bus.subscribe(_on_event)
