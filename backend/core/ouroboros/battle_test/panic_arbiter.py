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
import sys
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
    reconstructed: Optional[dict] = None,
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
        if reconstructed and reconstructed.get("traceback") and (
                not tb_text.strip() or reconstructed.get("synthetic")):
            # Reconstructed frames APPEND rather than replace: the real
            # traceback, when there is one, is still the better story.
            marker = "  ── reconstructed from the dying task ──\n"
            tb_text = (tb_text + "\n" + marker
                       + reconstructed["traceback"])[-4000:]
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


#: Locals carried per frame. A frame can hold a whole request body; the
#: operator needs the SHAPE of the failure, not a memory dump.
_MAX_LOCALS = 6
_MAX_LOCAL_CHARS = 120


def _redact(text: str) -> str:
    """Mask credential shapes with the firewall's own patterns. NEVER raises.

    Reconstructed locals are arbitrary runtime values — a token, a header,
    a connection string. Broadcasting them to every attached cockpit
    unredacted would make the crash reporter the worst leak in the system.
    Same authority `live_tool_stream` uses; a private copy would rot.
    """
    try:
        from backend.core.ouroboros.governance.semantic_firewall import (
            _CREDENTIAL_SHAPE_PATTERNS,
        )
        for pattern in _CREDENTIAL_SHAPE_PATTERNS:
            text = pattern.sub("[redacted]", text)
        return text
    except Exception:  # noqa: BLE001
        return ""          # fail CLOSED — no locals beats leaked ones


#: Frames captured at task creation. Four is the caller, its caller, and
#: enough context to name the subsystem — deeper costs more and says less.
_SPAWN_DEPTH = 4

#: Attribute stamped on every Task. Underscored and namespaced so it
#: cannot collide with anything asyncio or a library puts there.
SPAWN_ATTR = "_ov_spawn_provenance"


def _capture_spawn(depth: int = _SPAWN_DEPTH) -> tuple:
    """Raw (file, line, func) tuples for the creating stack. NEVER raises.

    A frame WALK, not `traceback.extract_stack`. The mandate suggested the
    latter; measured, it costs 4-38us per task because it reads source
    lines from disk through linecache, and this runs on EVERY task the
    daemon creates. The walk is 0.66us and captures the same three facts.

    Formatting is deferred to panic time — a session that never crashes
    pays only the walk, and a session that does can afford to read a file.
    That is the same reasoning that kept `loop.set_debug(True)` out: never
    make every task slower to serve the one that dies.
    """
    try:
        out = []
        frame = sys._getframe(2)          # skip this fn + the factory
        while frame is not None and len(out) < max(1, depth):
            code = frame.f_code
            name = code.co_filename
            # asyncio's own plumbing is never the interesting caller.
            if "/asyncio/" not in name:
                out.append((name, frame.f_lineno, code.co_name))
            frame = frame.f_back
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


def format_spawn(provenance: object) -> str:
    """Render captured tuples. Called ONLY when something died."""
    try:
        rows = [f"{f}:{ln} in {fn}" for f, ln, fn in (provenance or ())]
        return " <- ".join(rows[:3])
    except Exception:  # noqa: BLE001
        return ""


def install_task_factory(loop: Any = None) -> bool:
    """Stamp every task with where it was created. NEVER raises.

    `asyncio` ties `source_traceback` to global debug mode, which slows
    every await to serve a logging side effect. This gets the same fact —
    who spawned the task that died — for a frame walk at creation.

    CHAINS any existing factory rather than replacing it: a factory is a
    single global slot, and clobbering one installed by another subsystem
    would silently break whatever it was doing.
    """
    try:
        if not panic_arbiter_enabled():
            return False
        import asyncio as _a
        target = loop or _a.get_event_loop()
        previous = target.get_task_factory()
        if getattr(previous, "_ov_provenance_factory", False):
            return True                    # idempotent

        def _factory(loop_, coro, **kwargs):
            # Delegate construction, never reimplement it: `Task` gains
            # keyword-only params across versions (`name`, `context`,
            # `eager_start`) and a hand-rolled call would drop them.
            if previous is not None:
                task = previous(loop_, coro, **kwargs)
            else:
                task = _a.Task(coro, loop=loop_, **kwargs)
            try:
                setattr(task, SPAWN_ATTR, _capture_spawn())
            except Exception:  # noqa: BLE001
                pass               # a Task that refuses the stamp still runs
            return task

        _factory._ov_provenance_factory = True      # type: ignore[attr-defined]
        target.set_task_factory(_factory)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[PanicArbiter] task factory unavailable", exc_info=True)
        return False


def reconstruct_context(context: dict, exc: Optional[BaseException]) -> dict:
    """Rebuild what a stripped exception lost. NEVER raises.

    `Exception("")` raised in a detached task arrives with an empty
    message and, once the frame is gone, often nothing else either — so
    the overlay had a type and no story. Static analysis cannot find the
    producer of a runtime-empty payload; the payload has to describe
    itself.

    Measured rather than assumed: asyncio populates ``future`` for a dead
    Task (not ``task``, which the obvious reading expects), and
    ``Task.get_stack()`` still yields the frame AFTER the task is done.
    That frame is the whole prize — file, line, function and locals at the
    moment of failure.

    Sources, most-specific first:
      1. the exception's own ``__traceback__``
      2. ``Task.get_stack()`` frames
      3. the coroutine's ``cr_code`` (file + first line) when even the
         frame is gone
      4. ``source_traceback`` — where the task was CREATED, which asyncio
         only records in debug mode but which is the single most useful
         line when it exists
      5. the handle's repr, as a last identifier

    Built with `traceback.format_list` / `StackSummary`, never a
    hand-rolled formatter.
    """
    out: Dict[str, Any] = {"synthetic": False}
    try:
        if exc is not None and getattr(exc, "__traceback__", None) is not None:
            frames = traceback.extract_tb(exc.__traceback__)
            if frames:
                out["traceback"] = "".join(traceback.format_list(frames))
        task = context.get("task") or context.get("future")

        # (2) live frames off the dying task
        if not out.get("traceback") and task is not None:
            try:
                stack = task.get_stack() or []
            except Exception:  # noqa: BLE001
                stack = []
            if stack:
                summary = traceback.StackSummary.extract(
                    ((f, f.f_lineno) for f in stack), capture_locals=False)
                out["traceback"] = "".join(traceback.format_list(summary))
                out["synthetic"] = True
                frame = stack[-1]
                out["where"] = (f"{frame.f_code.co_filename}:"
                                f"{frame.f_lineno} in {frame.f_code.co_name}")
                locals_ = []
                for name, val in list(frame.f_locals.items())[:_MAX_LOCALS]:
                    if name.startswith("__"):
                        continue
                    try:
                        shown = _redact(repr(val))[:_MAX_LOCAL_CHARS]
                    except Exception:  # noqa: BLE001
                        shown = "<unrepresentable>"
                    locals_.append(f"{name}={shown}")
                if locals_:
                    out["locals"] = locals_

        # (3) the coroutine's code object, when the frame is gone
        if not out.get("traceback") and task is not None:
            coro = getattr(task, "get_coro", lambda: None)()
            code = getattr(coro, "cr_code", None) or getattr(
                coro, "gi_code", None)
            if code is not None:
                out["where"] = (f"{code.co_filename}:{code.co_firstlineno} "
                                f"in {code.co_name}")
                out["synthetic"] = True

        # (4) where the task was SPAWNED (asyncio debug mode only)
        # (4b) our own stamp, when asyncio's debug-only record is absent —
        # which it always is, because we refuse to pay for global debug.
        if task is not None and not out.get("spawned_at"):
            stamped = format_spawn(getattr(task, SPAWN_ATTR, ()))
            if stamped:
                out["spawned_at"] = stamped
                out["synthetic"] = True

        src = context.get("source_traceback")
        if src:
            try:
                out["spawned_at"] = "".join(
                    traceback.format_list(src[-3:])).strip()
                out["synthetic"] = True
            except Exception:  # noqa: BLE001
                pass

        # (5) last identifier
        if not out.get("where") and context.get("handle") is not None:
            out["where"] = repr(context["handle"])[:160]
            out["synthetic"] = True
        if task is not None and not out.get("origin"):
            name = getattr(task, "get_name", lambda: "")()
            if name:
                out["origin"] = f"task:{name}"
    except Exception:  # noqa: BLE001
        logger.debug("[PanicArbiter] reconstruction degraded", exc_info=True)
    return out


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
        exc_obj = exc if isinstance(exc, BaseException) else None
        # A payload is "empty" when the exception carries no words AND no
        # frames. That is the shape that reached the operator as `?:` —
        # a type with no story — and it is a RUNTIME condition, so it is
        # answered at runtime.
        thin = not str(exc_obj or "").strip() or (
            exc_obj is not None
            and getattr(exc_obj, "__traceback__", None) is None)
        rebuilt = reconstruct_context(ctx, exc_obj) if thin else {}
        detail = message
        if rebuilt.get("where"):
            detail = f"{detail} | at {rebuilt['where']}"
        if rebuilt.get("spawned_at"):
            detail = f"{detail} | spawned {rebuilt['spawned_at']}"
        if rebuilt.get("locals"):
            detail = f"{detail} | locals: {', '.join(rebuilt['locals'])}"
        report(exc_obj,
               message=f"{detail}{(' | ' + extras) if extras else ''}",
               origin=rebuilt.get("origin") or "loop_handler",
               reconstructed=rebuilt or None)
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
        exc_type = str(payload.get("exc_type") or "").strip()
        message = str(payload.get("message") or "").strip()
        origin = str(payload.get("origin") or "").strip()
        tb_text = str(payload.get("traceback") or "").strip()
        # A payload with NOTHING in it is not a panic worth an overlay —
        # it is a bug in whoever built it, and "?:" / "origin: ?" told the
        # operator nothing while covering their screen. Refuse to raise
        # the overlay rather than raise an empty one.
        if not (exc_type or message or tb_text):
            return []
        rows = [
            "☠  FATAL — a background task died",
            f"   {exc_type or 'unknown error'}"
            + (f": {message[:200]}" if message else ""),
        ]
        if origin:
            rows.append(f"   origin: {origin}")
        rows.append("")
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
    "SPAWN_ATTR",
    "arbitrate",
    "format_spawn",
    "install_task_factory",
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
