"""A background death the operator can see, within milliseconds of it happening.

Three times in one arc a missing symbol was swallowed by a broad ``except``
and the system carried on describing a world that had stopped existing.
The failure mode is not the ``except`` blocks — hunting those is endless
and each one is individually defensible. It is that **a detached task can
die and nothing tells anyone**.

Two detectors, because one is not enough
========================================
The obvious design is ``loop.set_exception_handler()``. It is necessary
and, alone, insufficient — and the gap is not theoretical:

    async def boom(): raise RuntimeError("silent death")
    t = asyncio.create_task(boom())
    await asyncio.sleep(0.05)        # handler has NOT fired
    keep = t                         # any registry holding a reference
    await asyncio.sleep(0.05)        # still has NOT fired
    del t, keep; gc.collect()        # ... only now does it fire

asyncio surfaces a task's unretrieved exception when the Task object is
**garbage collected**. Anything that keeps a reference — a task registry,
``self._tasks``, a list comprehension that outlives the await — defers
that indefinitely. A long-lived daemon holding its own tasks is exactly
the case where the loop handler never fires at all.

So:

  1. :func:`spawn_supervised` — a ``create_task`` that attaches a
     done-callback. Fires the instant the task completes with an
     exception, regardless of who holds a reference. This is the one that
     catches the case we actually have.
  2. :func:`arbitrate` — the loop-level handler, as a BACKSTOP for
     everything the first cannot see: callback errors, transport faults,
     tasks spawned by libraries, and GC-time leaks from code that never
     adopted (1).

Neither replaces the other, and a panic reported by both is reported once
(deduplicated by signature).

Not every exception is a panic
==============================
``serpent_flow`` already carries ``_EXPECTED_BACKGROUND_EXC_PATTERNS`` for
known-benign shutdown noise, and ``CancelledError`` is control flow rather
than failure. Both are honoured — an alarm that cries wolf during every
clean shutdown trains the operator to ignore the one that matters. What is
NOT honoured is silence: anything not provably benign is a panic.

Broadcast, not just logged
==========================
A traceback in a log file is invisible to someone watching a cockpit. The
panic is serialized onto the SAME UDS telemetry lane every other live
state uses, and the client raises the overlay that the ``/`` palette's
``FloatContainer`` architecture already provides. One Z-index story, one
envelope, one lane.

NEVER raises. An arbiter that can throw is a second silent death.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.PanicArbiter")

PANIC_SCHEMA_VERSION = "fatal_panic.v1"

#: The frame kind. Matches the existing UDS envelope convention
#: (``{"type": ..., ...}`` downstream frames) rather than inventing one.
PANIC_KIND = "fatal_panic"

MASTER_FLAG_ENV_VAR = "JARVIS_PANIC_ARBITER_ENABLED"

#: Distinct panics retained. A cascade (one fault triggering ten) must not
#: become ten overlays, and an operator only ever acts on the first.
_MAX_RETAINED = 8

_LOCK = threading.Lock()
_PANICS: List["Panic"] = []
_SEEN: Dict[str, float] = {}
_SINKS: List[Callable[[dict], None]] = []


def panic_arbiter_enabled() -> bool:
    """Default ON. Off, a background death is invisible again."""
    try:
        return os.environ.get(
            MASTER_FLAG_ENV_VAR, "1",
        ).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


@dataclass(frozen=True)
class Panic:
    """One unhandled background failure."""

    kind: str
    message: str
    exc_type: str
    traceback_text: str
    origin: str
    signature: str
    at_unix: float

    def as_payload(self) -> dict:
        return {
            "type": PANIC_KIND,
            "kind": PANIC_KIND,
            "schema_version": PANIC_SCHEMA_VERSION,
            "message": self.message,
            "exc_type": self.exc_type,
            "traceback": self.traceback_text,
            "origin": self.origin,
            "signature": self.signature,
            "at_unix": self.at_unix,
        }


def _is_benign(exc: BaseException, message: str) -> bool:
    """Known-benign background noise? NEVER raises.

    Deliberately narrow. Cancellation is control flow, and the shutdown
    patterns `serpent_flow` already curates are real noise — but anything
    not provably benign is a panic. The failure this module exists to end
    is silence, so the default answer is "report it".
    """
    try:
        import asyncio
        if isinstance(exc, asyncio.CancelledError):
            return True
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return True
        import sys
        if sys.is_finalizing() or sys.meta_path is None:
            # Interpreter teardown: logging itself dies here. Reported
            # panics during finalization are noise from a dying process.
            return True
        try:
            from backend.core.ouroboros.battle_test.serpent_flow import (
                _EXPECTED_BACKGROUND_EXC_PATTERNS,
            )
        except Exception:  # noqa: BLE001
            _EXPECTED_BACKGROUND_EXC_PATTERNS = ()   # type: ignore[assignment]
        blob = f"{message} {exc}"
        return any(p in blob for p in _EXPECTED_BACKGROUND_EXC_PATTERNS or ())
    except Exception:  # noqa: BLE001
        return False


def _signature(exc_type: str, tb_text: str) -> str:
    """Identity of a panic, for dedup. Pure.

    The exception type plus the LAST frame — a cascade of ten failures
    from one root produces one overlay, and a task that dies every second
    does not produce sixty.
    """
    try:
        last = ""
        for line in reversed((tb_text or "").splitlines()):
            if line.strip().startswith("File "):
                last = line.strip()
                break
        return f"{exc_type}|{last}"[:200]
    except Exception:  # noqa: BLE001
        return str(exc_type)[:200]


def register_sink(sink: Callable[[dict], None]) -> None:
    """Add a panic consumer (the UDS broadcaster, a test spy). NEVER raises."""
    try:
        with _LOCK:
            if sink not in _SINKS:
                _SINKS.append(sink)
    except Exception:  # noqa: BLE001
        pass


def reset_for_tests() -> None:
    with _LOCK:
        _PANICS.clear()
        _SEEN.clear()
        _SINKS.clear()


def recent_panics() -> List[Panic]:
    with _LOCK:
        return list(_PANICS)


def report(
    exc: Optional[BaseException], *, message: str = "", origin: str = "",
    clock: Optional[Callable[[], float]] = None,
) -> Optional[Panic]:
    """Record and BROADCAST one background failure. NEVER raises.

    Returns the Panic, or None when suppressed (benign, duplicate, or the
    master flag is off).
    """
    try:
        if not panic_arbiter_enabled():
            return None
        now = (clock or time.time)()
        if exc is None:
            exc_type, tb_text = "UnknownError", ""
        else:
            if _is_benign(exc, message):
                return None
            exc_type = type(exc).__name__
            tb_text = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))[-4000:]
        sig = _signature(exc_type, tb_text)
        with _LOCK:
            if sig in _SEEN:
                return None                # a cascade is still one panic
            _SEEN[sig] = now
            panic = Panic(
                kind=PANIC_KIND,
                message=str(message or (str(exc) if exc else "unknown"))[:400],
                exc_type=exc_type, traceback_text=tb_text,
                origin=str(origin or "unknown")[:80],
                signature=sig, at_unix=float(now),
            )
            _PANICS.append(panic)
            del _PANICS[:-_MAX_RETAINED]
            sinks = list(_SINKS)
        # Logged FIRST, so a dead bridge cannot cost us the record.
        logger.error("[PANIC] %s: %s (origin=%s)\n%s",
                     exc_type, panic.message, panic.origin, tb_text)
        payload = panic.as_payload()
        for sink in sinks:
            try:
                sink(payload)
            except Exception:  # noqa: BLE001
                logger.debug("[PanicArbiter] sink failed", exc_info=True)
        return panic
    except Exception:  # noqa: BLE001
        # An arbiter that raises is a second silent death.
        try:
            logger.debug("[PanicArbiter] report degraded", exc_info=True)
        except Exception:  # noqa: BLE001
            pass
        return None


def arbitrate(loop: Any, context: dict) -> None:
    """``loop.set_exception_handler`` target — the BACKSTOP detector.

    Catches what :func:`spawn_supervised` cannot see: callback errors,
    transport faults, library-spawned tasks, and GC-time leaks from code
    that never adopted supervision. NEVER raises.
    """
    try:
        ctx = context if isinstance(context, dict) else {}
        message = str(ctx.get("message") or "unhandled exception in event loop")
        exc = ctx.get("exception")
        extras = " | ".join(
            f"{k}={ctx[k]!r}" for k in sorted(ctx)
            if k not in ("message", "exception")
        )
        report(exc if isinstance(exc, BaseException) else None,
               message=f"{message}{(' | ' + extras) if extras else ''}",
               origin="loop_handler")
    except Exception:  # noqa: BLE001
        pass


def install(loop: Any = None) -> bool:
    """Install the backstop on ``loop``. Idempotent. NEVER raises."""
    try:
        import asyncio
        target = loop or asyncio.get_event_loop()
        target.set_exception_handler(arbitrate)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[PanicArbiter] install degraded", exc_info=True)
        return False


def supervise(task: Any, *, origin: str = "") -> Any:
    """Attach the IMMEDIATE detector to an existing task. NEVER raises.

    A done-callback fires the moment the task completes with an
    exception — no GC required, no reference-lifetime dependency. This is
    the detector that catches a long-lived daemon's own tasks, which the
    loop handler never sees because the daemon holds them.
    """
    try:
        def _on_done(t: Any) -> None:
            try:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    # The exception's OWN text first: "detached task
                    # died: operator_input_queue.drain" tells the operator
                    # where and not what, and what is the actionable half.
                    report(exc, message=f"{exc} (detached task: "
                                        f"{origin or 'unknown'})",
                           origin=origin or "detached_task")
            except Exception:  # noqa: BLE001
                pass
        task.add_done_callback(_on_done)
    except Exception:  # noqa: BLE001
        pass
    return task


def spawn_supervised(coro: Any, *, origin: str = "") -> Any:
    """``create_task`` that cannot die silently. NEVER raises the spawn.

    The drop-in for every ``loop.create_task(...)`` whose failure would
    otherwise be invisible.
    """
    import asyncio
    task = asyncio.ensure_future(coro)
    return supervise(task, origin=origin)


# ---------------------------------------------------------------------------
# Rendering — shared by the overlay and any plain-text surface
# ---------------------------------------------------------------------------


def render_panic(payload: Optional[dict], *, width: Optional[int] = None,
                 max_frames: int = 12) -> List[str]:
    """The operator-facing crash block. Pure. NEVER raises.

    The LAST frames are kept, not the first: the root of a traceback is
    usually framework scaffolding and the tail is where the organism
    actually died.
    """
    try:
        if not isinstance(payload, dict):
            return []
        cols = int(width) if width and int(width) > 0 else 80
        rows = [
            "☠  FATAL — a background task died",
            f"   {payload.get('exc_type', '?')}: "
            f"{str(payload.get('message') or '')[:200]}",
            f"   origin: {payload.get('origin', '?')}",
            "",
        ]
        tb = str(payload.get("traceback") or "").splitlines()
        if tb:
            kept = tb[-max_frames:]
            if len(tb) > max_frames:
                rows.append(f"   … {len(tb) - max_frames} earlier frames")
            rows.extend(f"   {ln}" for ln in kept)
        rows.append("")
        rows.append("   the organism may be degraded — /status  ·  esc dismisses")
        return [r[:cols] for r in rows]
    except Exception:  # noqa: BLE001
        return ["☠  FATAL — a background task died (render degraded)"]


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "PANIC_KIND",
    "PANIC_SCHEMA_VERSION",
    "Panic",
    "arbitrate",
    "install",
    "panic_arbiter_enabled",
    "recent_panics",
    "register_sink",
    "render_panic",
    "report",
    "reset_for_tests",
    "spawn_supervised",
    "supervise",
]
