"""One meaning for "the operator submitted a line", whichever surface it came from.

The same action had two concurrency semantics:

    serpent_flow.py:6322   result = self._on_command(line)
                           await result                    <- serialized
    harness.py:4576        loop.create_task(
                               self._handle_repl_command_for(text, session))

Typing at the daemon terminal was ordered and backpressured. Typing the
identical line into an attached cockpit spawned a task per line, with no
ordering and no bound. That is not only a UX difference:

  * ``/pause`` then ``/status`` could report the PRE-pause state, because
    the second handler was free to finish first.
  * a pasted block of N lines became N concurrent handlers, all racing.
  * nothing showed that a submitted line had not been acted on yet, so a
    busy organism was indistinguishable from a dropped keystroke.

Why a queue rather than an await
--------------------------------
Awaiting inline in ``_on_input`` was never an option — that callback runs
on the bridge's read loop, and blocking it would stall every attached
cockpit's input, heartbeat and telemetry behind one slow handler. Which is
precisely why it was made fire-and-forget.

So: enqueue on the read loop (non-blocking, order preserved), and let ONE
consumer await handlers in turn. The local surface's semantics, without
the local surface's blocking.

What this does NOT serialize
-----------------------------
The OPS. ``_handle_repl_command`` schedules work and returns; the op it
starts runs in its own task and outlives the handler. So the queue makes
handlers ordered and cheap, and a long-running op never delays the next
command an operator types. Serializing ops would be a different — and
wrong — change: the organism is concurrent by design.

Bounded, and never silently
---------------------------
A full queue REFUSES with a visible reason rather than dropping. Operator
intent is not telemetry: a silently discarded goal is worse than a
rejected one, because the operator believes it landed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

logger = logging.getLogger("Ouroboros.OperatorInputQueue")

OPERATOR_INPUT_QUEUE_SCHEMA_VERSION = "operator_input_queue.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_OPERATOR_INPUT_QUEUE_ENABLED"

#: Submitted-but-unprocessed lines held before refusing. Generous enough
#: for a pasted block, small enough that a runaway producer cannot hide a
#: thousand pending intents behind a busy organism.
_DEFAULT_MAX_DEPTH = 32


def input_queue_enabled() -> bool:
    """Default ON. Off, attached input is fire-and-forget again — the
    unordered, unbounded behaviour this module exists to end."""
    try:
        return os.environ.get(
            MASTER_FLAG_ENV_VAR, "1",
        ).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


def _max_depth() -> int:
    try:
        return max(1, min(512, int(os.environ.get(
            "JARVIS_OPERATOR_INPUT_QUEUE_DEPTH", _DEFAULT_MAX_DEPTH))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_MAX_DEPTH


@dataclass(frozen=True)
class QueuedInput:
    """One submitted line, waiting its turn."""

    text: str
    session: Optional[str] = None
    submitted_monotonic: float = 0.0

    def preview(self, width: int = 48) -> str:
        flat = " ".join(str(self.text or "").split())
        return flat if len(flat) <= width else flat[: width - 1] + "…"


@dataclass(frozen=True)
class SubmitResult:
    """What happened to a submitted line. Never a bare bool.

    ``accepted=False`` always carries a reason, because the one outcome
    this module must never produce is an operator believing their line
    landed when it did not.
    """

    accepted: bool
    depth: int = 0
    reason: str = ""


class OperatorInputQueue:
    """FIFO for operator lines, drained by one consumer.

    Thread-confined to the event loop it is created on; ``submit`` is
    non-blocking and safe to call from a bridge read callback.
    """

    def __init__(
        self,
        handler: Callable[[str, Optional[str]], Any],
        *,
        max_depth: Optional[int] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._handler = handler
        self._clock = clock or time.monotonic
        self._max_depth = int(max_depth or _max_depth())
        self._pending: List[QueuedInput] = []
        self._waiter: Optional[asyncio.Event] = None
        self._consumer: Optional[Any] = None
        self._running: Optional[QueuedInput] = None
        self._closed = False
        #: Lines refused for want of room. Surfaced, never silent.
        self.refused = 0

    # -- producer side (bridge read loop) ------------------------------

    def submit(self, text: object, session: Optional[str] = None) -> SubmitResult:
        """Enqueue one line. Non-blocking. NEVER raises.

        Order is established HERE, on the read loop, which is the only
        place submission order is knowable. Anything downstream that
        re-derives it from timestamps would be guessing.
        """
        try:
            flat = str(text or "")
            if not flat.strip():
                return SubmitResult(accepted=False, reason="empty")
            if self._closed:
                return SubmitResult(accepted=False, reason="shutting down")
            if len(self._pending) >= self._max_depth:
                self.refused += 1
                # REFUSE, never drop. A discarded goal the operator
                # believes landed is worse than a rejected one.
                return SubmitResult(
                    accepted=False, depth=len(self._pending),
                    reason=f"queue full ({self._max_depth}) — "
                           f"wait or /cancel",
                )
            self._pending.append(QueuedInput(
                text=flat, session=session,
                submitted_monotonic=self._clock(),
            ))
            if self._waiter is not None:
                self._waiter.set()
            return SubmitResult(accepted=True, depth=len(self._pending))
        except Exception:  # noqa: BLE001
            logger.debug("[OperatorInput] submit degraded", exc_info=True)
            return SubmitResult(accepted=False, reason="submit failed")

    # -- consumer side --------------------------------------------------

    def start(self) -> Optional[Any]:
        """Begin draining. Idempotent. Returns the consumer task or None."""
        try:
            if self._consumer is not None and not self._consumer.done():
                return self._consumer
            self._waiter = asyncio.Event()
            self._closed = False
            # SUPERVISED. This object holds a reference to its own
            # consumer, which is exactly the case the loop handler never
            # sees: asyncio only surfaces an unretrieved task exception at
            # garbage collection, and a live reference defers that
            # forever. The done-callback fires immediately instead.
            try:
                from backend.core.ouroboros.battle_test.panic_arbiter import (
                    spawn_supervised,
                )
                self._consumer = spawn_supervised(
                    self._drain(), origin="operator_input_queue.drain")
            except Exception:  # noqa: BLE001
                self._consumer = asyncio.ensure_future(self._drain())
            return self._consumer
        except Exception:  # noqa: BLE001
            logger.debug("[OperatorInput] consumer start degraded",
                         exc_info=True)
            return None

    async def _drain(self) -> None:
        """Await each handler in turn, forever. NEVER exits on error.

        A consumer that dies on one bad handler wedges every later line
        the operator types — the queue would then be strictly worse than
        the unordered path it replaced.
        """
        while True:
            try:
                if not self._pending:
                    if self._closed:
                        return
                    if self._waiter is not None:
                        self._waiter.clear()
                        await self._waiter.wait()
                    else:                      # pragma: no cover
                        await asyncio.sleep(0.05)
                    continue
                item = self._pending.pop(0)
                self._running = item
                try:
                    result = self._handler(item.text, item.session)
                    if asyncio.iscoroutine(result) or isinstance(
                            result, asyncio.Future):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[OperatorInput] handler failed for %r",
                        item.preview(), exc_info=True)
                finally:
                    self._running = None
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.debug("[OperatorInput] drain degraded", exc_info=True)
                await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        """Stop accepting, let the consumer finish, report what was lost."""
        self._closed = True
        try:
            if self._waiter is not None:
                self._waiter.set()
            if self._consumer is not None:
                self._consumer.cancel()
        except Exception:  # noqa: BLE001
            pass

    # -- observation ----------------------------------------------------

    @property
    def depth(self) -> int:
        return len(self._pending)

    def snapshot(self) -> dict:
        """Transport-safe view for the cockpit. NEVER raises.

        Depth alone is not enough: an operator who typed three lines wants
        to see WHICH three are still waiting, or the queue is just another
        opaque delay.
        """
        try:
            return {
                "schema_version": OPERATOR_INPUT_QUEUE_SCHEMA_VERSION,
                "depth": len(self._pending),
                "refused": int(self.refused),
                "running": (self._running.preview()
                            if self._running is not None else ""),
                "pending": [q.preview() for q in self._pending[:5]],
            }
        except Exception:  # noqa: BLE001
            return {"depth": 0, "refused": 0, "running": "", "pending": []}


#: The live queue, for read-only observers (the heartbeat). Mirrors
#: `cockpit_attach._ACTIVE_BRIDGE`: a producer publishes itself rather
#: than every consumer hunting for a harness singleton — the version of
#: this that reached for `harness.get_active_harness()` failed silently,
#: because no such function exists and the import error was swallowed.
_ACTIVE_QUEUE: Optional["OperatorInputQueue"] = None


def set_active_queue(queue: Optional["OperatorInputQueue"]) -> None:
    """Publish (or retire) the live queue. NEVER raises."""
    global _ACTIVE_QUEUE
    _ACTIVE_QUEUE = queue


def active_queue_snapshot() -> dict:
    """The live queue's state, or {} when there is none. NEVER raises."""
    try:
        q = _ACTIVE_QUEUE
        return q.snapshot() if q is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def render_queue(payload: Optional[dict], *, width: Optional[int] = None) -> List[str]:
    """Rows for the queued-input strip, or []. Pure. NEVER raises.

    Renders nothing at depth 0 — a queue that is keeping up should be
    invisible. It earns a row only when the operator is ahead of the
    organism, which is exactly when they need to know.
    """
    try:
        if not isinstance(payload, dict):
            return []
        depth = int(payload.get("depth") or 0)
        refused = int(payload.get("refused") or 0)
        if depth <= 0 and refused <= 0:
            return []
        cols = int(width) if width and int(width) > 0 else 80
        rows: List[str] = []
        if depth > 0:
            head = f"  ⋯ {depth} queued"
            first = [p for p in (payload.get("pending") or ()) if p]
            if first:
                head = f"{head} · next: {first[0]}"
            rows.append(head[:cols])
        if refused > 0:
            rows.append(f"  ⚠ {refused} refused — queue was full"[:cols])
        return rows
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "OPERATOR_INPUT_QUEUE_SCHEMA_VERSION",
    "OperatorInputQueue",
    "QueuedInput",
    "SubmitResult",
    "input_queue_enabled",
    "active_queue_snapshot",
    "render_queue",
    "set_active_queue",
]
