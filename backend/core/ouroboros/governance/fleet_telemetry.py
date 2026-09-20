"""Make fleet subagents visible on the cockpit, without touching their code.

The defect
----------

The exploration fleet runs. It logged 80 ``[ExploreAgent]`` events in a
single session, its agents showed up in ``StallAttributor`` stacks, and the
sentinel dispatched work through it. And ``exploration_fleet.py`` contains
no ``emit_heartbeat``, no ``emit_decision``, no reference to ``comm`` at all
-- so every one of those events went to ``debug.log`` and nowhere else. The
operator watching ``ov --sentinel`` saw an idle cockpit while eight agents
worked.

That is a transport gap, not a behavioural one, and it is the fifth time
autonomous work in this tree has run correctly and invisibly.

Why a mixin and not emit calls
------------------------------

Adding ``await comm.emit_heartbeat(...)`` at each of a fleet's state changes
puts the transport contract inside every fleet, where it has to be
re-remembered by whoever writes the next one. The sixth instance of this bug
would then be written the same week it was fixed.

So the binding is structural. A fleet declares WHICH of its coroutines are
agent runs (``TELEMETRY_METHODS``) and the mixin wraps them at class-creation
time via ``__init_subclass__``. Start, finish and failure frames are emitted
around the existing method; the method itself is unchanged and does not know
it is observed. A future fleet inherits the mixin and renders for free.

Attachment, not ambience
------------------------

The transport is attached explicitly at the one site where every fleet is
born (``governed_loop_service``), rather than resolved from a ContextVar
that nobody sets. A telemetry channel with no producer bound is exactly the
armed-and-silent capability this module exists to remove -- so when no
transport is attached, :func:`transport_bound` says so out loud and the
mixin degrades to the logging it already had.
"""
from __future__ import annotations

import functools
import inspect
import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.FleetTelemetry")

_transport: Any = None
_transport_label: str = ""


def telemetry_enabled() -> bool:
    """Default ON. Pure observability -- it emits, it never decides."""
    raw = (os.environ.get("JARVIS_FLEET_TELEMETRY_ENABLED", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def attach_transport(comm: Any, *, label: str = "") -> bool:
    """Bind the cockpit transport every fleet will emit through.

    Called once, where fleets are constructed. Idempotent and never raises:
    a telemetry binding that could fail startup would be a worse defect than
    the invisibility it fixes.
    """
    global _transport, _transport_label  # noqa: PLW0603
    if comm is None or not hasattr(comm, "emit_heartbeat"):
        logger.debug("[FleetTelemetry] refused a transport with no emit_heartbeat")
        return False
    _transport = comm
    _transport_label = label or type(comm).__name__
    logger.info(
        "[FleetTelemetry] transport attached (%s) — fleet subagents will "
        "render on the cockpit", _transport_label,
    )
    return True


def detach_transport() -> None:
    global _transport, _transport_label  # noqa: PLW0603
    _transport = None
    _transport_label = ""


def transport_bound() -> bool:
    return _transport is not None


async def emit_fleet_frame(
    *,
    op_id: str,
    phase: str,
    progress_pct: float,
    **extra: Any,
) -> bool:
    """One cockpit frame. NEVER raises, never blocks a fleet.

    Returns whether it reached a transport, so a caller can tell "emitted"
    from "silently dropped" -- the distinction this whole module exists to
    restore.
    """
    if not telemetry_enabled():
        return False
    comm = _transport
    if comm is None:
        return False
    try:
        await comm.emit_heartbeat(
            op_id=op_id, phase=phase, progress_pct=progress_pct, **extra,
        )
        return True
    except Exception:  # noqa: BLE001 — the cockpit never breaks the fleet
        logger.debug("[FleetTelemetry] frame dropped", exc_info=True)
        return False


def _describe(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort label for the agent this call represents.

    Duck-typed rather than tied to ``FleetAgent``: the mixin must work for a
    fleet whose agent type does not exist yet, which is the point of it
    being a mixin. NEVER raises.
    """
    out: Dict[str, Any] = {}
    try:
        for value in list(args) + list(kwargs.values()):
            for attr, key in (
                ("agent_id", "agent"), ("name", "agent"),
                ("repo", "repo"), ("scope", "scope"),
            ):
                got = getattr(value, attr, None)
                if isinstance(got, str) and got and key not in out:
                    out[key] = got[:80]
            if isinstance(value, str) and "goal" not in out and len(value) > 8:
                out["goal"] = value[:120]
    except Exception:  # noqa: BLE001
        return out
    return out


class FleetTelemetryMixin:
    """Emits cockpit frames around a fleet's agent runs.

    Subclasses name the coroutines that constitute an agent run in
    ``TELEMETRY_METHODS``; the mixin wraps them when the class is created.
    A subclass that declares a method it does not have is a wiring mistake
    and says so at import, rather than going quiet at runtime -- the
    failure mode that produced this module.
    """

    TELEMETRY_METHODS: Tuple[str, ...] = ()
    TELEMETRY_PHASE: str = "EXPLORE"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in getattr(cls, "TELEMETRY_METHODS", ()) or ():
            target = getattr(cls, name, None)
            if target is None:
                logger.warning(
                    "[FleetTelemetry] %s declares TELEMETRY_METHODS entry "
                    "%r which does not exist — that agent run will be "
                    "invisible on the cockpit",
                    cls.__name__, name,
                )
                continue
            if getattr(target, "__fleet_telemetry__", False):
                continue
            if not inspect.iscoroutinefunction(target):
                logger.warning(
                    "[FleetTelemetry] %s.%s is not a coroutine — not wrapped",
                    cls.__name__, name,
                )
                continue
            setattr(cls, name, cls._wrap_agent_run(target))

    @staticmethod
    def _wrap_agent_run(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def _wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            op_id = str(getattr(self, "telemetry_op_id", "") or "fleet")
            phase = str(getattr(self, "TELEMETRY_PHASE", "EXPLORE"))
            detail = _describe(args, kwargs)
            t0 = time.monotonic()
            await emit_fleet_frame(
                op_id=op_id, phase=phase, progress_pct=0.0,
                fleet=type(self).__name__, state="started", **detail,
            )
            try:
                result = await fn(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — observe, then re-raise
                await emit_fleet_frame(
                    op_id=op_id, phase=phase, progress_pct=100.0,
                    fleet=type(self).__name__, state="failed",
                    error=f"{type(exc).__name__}: {exc}"[:200],
                    elapsed_s=round(time.monotonic() - t0, 3), **detail,
                )
                raise
            await emit_fleet_frame(
                op_id=op_id, phase=phase, progress_pct=100.0,
                fleet=type(self).__name__, state="finished",
                elapsed_s=round(time.monotonic() - t0, 3), **detail,
            )
            return result

        _wrapped.__fleet_telemetry__ = True  # type: ignore[attr-defined]
        return _wrapped

    def bind_telemetry_op(self, op_id: str) -> None:
        """Attribute this fleet's frames to an operation. NEVER raises."""
        try:
            self.telemetry_op_id = str(op_id or "fleet")
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "FleetTelemetryMixin",
    "attach_transport",
    "detach_transport",
    "emit_fleet_frame",
    "telemetry_enabled",
    "transport_bound",
]
