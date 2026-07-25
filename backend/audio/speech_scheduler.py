"""One pair of speakers, two agents — a turnstile at the hardware boundary.

The collision
-------------
"Hey JARVIS, ask Karen to verify the deployment" summons two agents. Both
resolve a voice, both synthesize, and both hand a file to ``afplay`` — which
is a separate OS process precisely so it is GIL-free, and therefore has no
idea another one is already playing. The operator hears two voices at once.

Serialising with a lock is not enough
-------------------------------------
A lock grants in arrival order, and arrival order is a race: the SECONDARY's
work is usually shorter, so it frequently finishes synthesis first and would
speak before the agent that was actually addressed. Order has to follow the
ROLE assigned by arbitration, not the accident of which synthesis returned
first. Hence a priority queue with a single conductor.

The acoustic tail
-----------------
``afplay`` exits when the last sample is handed to CoreAudio, not when it has
left the speakers. Granting the next utterance at that instant clips the first
word of the second agent onto the last of the first. So the conductor waits
for the device to go idle AND then for a fixed tail (default 300ms) before the
next grant. The tail is also what keeps the barge-in detector from hearing the
first agent's decay as the second agent's onset.

What this wraps
---------------
Nothing is re-implemented. The synchronous ``macos_voice`` path already takes
``playback_gate_sync`` around its ``afplay`` subprocess; that gate now marks
the hardware BUSY, and the conductor grants only when it is idle. So a
scheduled utterance yields to an unscheduled one automatically, rather than
the two fighting — which is the property that makes this safe to introduce
under a system with several playback sites.

Non-blocking by construction
----------------------------
The conductor never blocks the event loop: it polls the busy counter with
``asyncio.sleep`` (the same discipline ``playback_gate._await_playback_drain``
already uses) rather than acquiring a threading lock from the loop thread.
The synchronous side blocks its own worker thread, which is that thread's job.

Bounded everywhere
------------------
A wedged synthesis must not leave the assistant permanently mute, so every
wait has a deadline and every deadline fails OPEN: the conductor logs loudly
and moves on. A silent assistant is a worse failure than an overlapped word.
"""
from __future__ import annotations

import asyncio
import enum
import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def scheduler_enabled() -> bool:
    """Master switch. OFF restores the pre-queue behaviour exactly — every
    caller speaks immediately, which is the only honest way to compare."""
    return os.getenv(
        "JARVIS_TTS_SCHEDULER_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def acoustic_tail_s() -> float:
    """Silence held after the device goes idle, before the next grant.

    300ms is a room's decay, not a guess at process teardown: ``afplay``
    exiting means CoreAudio has the samples, and the speakers are still
    moving after that."""
    return _env_float("JARVIS_TTS_ACOUSTIC_TAIL_S", 0.300)


def hardware_idle_timeout_s() -> float:
    """Ceiling on waiting for the speakers to fall silent. Fails OPEN."""
    return _env_float("JARVIS_TTS_HARDWARE_IDLE_TIMEOUT_S", 30.0, 1.0)


def ticket_timeout_s() -> float:
    """Ceiling on ONE utterance holding the turnstile. Fails OPEN."""
    return _env_float("JARVIS_TTS_TICKET_TIMEOUT_S", 60.0, 1.0)


def queue_maxsize() -> int:
    try:
        return max(2, int(os.getenv("JARVIS_TTS_QUEUE_MAXSIZE", "16")))
    except (TypeError, ValueError):
        return 16


# ---------------------------------------------------------------------------
# The hardware busy signal — shared with playback_gate
# ---------------------------------------------------------------------------

_HW_LOCK = threading.Lock()
_HW_BUSY = 0


def mark_hardware_busy() -> None:
    """Sound is leaving the speakers. Called from the playback gate, which is
    already the one place every playback path passes through — so this
    observes the REAL device rather than the scheduler's opinion of it."""
    global _HW_BUSY
    with _HW_LOCK:
        _HW_BUSY += 1


def mark_hardware_idle() -> None:
    global _HW_BUSY
    with _HW_LOCK:
        _HW_BUSY = max(0, _HW_BUSY - 1)


def hardware_busy() -> bool:
    with _HW_LOCK:
        return _HW_BUSY > 0


def reset_hardware_state() -> None:
    """Test seam, and a teardown safety valve: a leaked busy count would make
    the conductor wait out its full timeout on every utterance."""
    global _HW_BUSY
    with _HW_LOCK:
        _HW_BUSY = 0


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


class SpeechRole(enum.IntEnum):
    """Lower speaks first. Integer-valued so the priority queue orders on it
    directly rather than through a comparison table that could disagree."""

    PRIMARY = 0        # the agent the operator addressed
    SECONDARY = 1      # an agent the primary delegated to
    BACKGROUND = 2     # announcements, telemetry — never ahead of a person


_SEQ = itertools.count()


@dataclass(order=True)
class SpeechTicket:
    """One utterance's claim on the speakers.

    Ordered by (role, seq): role first so arbitration decides who speaks, seq
    second so two tickets of the same role keep arrival order and cannot
    starve one another."""

    role: SpeechRole
    seq: int
    agent: str = field(compare=False, default="")
    text: str = field(compare=False, default="")
    granted: asyncio.Event = field(compare=False, default_factory=asyncio.Event)
    done: asyncio.Event = field(compare=False, default_factory=asyncio.Event)
    cancelled: bool = field(compare=False, default=False)
    enqueued_at: float = field(compare=False, default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# The conductor
# ---------------------------------------------------------------------------


class SpeechScheduler:
    """Grants the speakers to exactly one utterance at a time.

    Bound lazily to whichever loop first uses it, and rebound if that loop is
    replaced — a scheduler holding a queue from a dead loop would silently
    swallow every utterance, which in this system means the assistant simply
    stops talking with no error anywhere."""

    def __init__(self) -> None:
        self._queue: Optional[asyncio.PriorityQueue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._conductor: Optional[asyncio.Task] = None
        self._lock = threading.Lock()
        self.granted_order: list = []          # observability + tests

    # -- lifecycle ------------------------------------------------------

    def _ensure(self) -> asyncio.PriorityQueue:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._queue is None or self._loop is not loop:
                self._queue = asyncio.PriorityQueue(maxsize=queue_maxsize())
                self._loop = loop
                self._conductor = None
            if self._conductor is None or self._conductor.done():
                self._conductor = loop.create_task(
                    self._run(), name="tts-conductor",
                )
            return self._queue

    async def aclose(self) -> None:
        """Stop conducting. Queued tickets are released, not stranded — a
        pending ticket whose event never fires is a caller awaiting forever."""
        with self._lock:
            task, queue = self._conductor, self._queue
            self._conductor = None
        if queue is not None:
            while not queue.empty():
                try:
                    ticket = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                ticket.cancelled = True
                ticket.granted.set()
        if task is not None and not task.done():
            task.cancel()
            # asyncio.wait, NOT `await task`.
            #
            # Awaiting a task you just cancelled re-raises its CancelledError
            # in the CALLER, and swallowing it there leaves the caller's own
            # task flagged `cancelling` — after which the loop's shutdown
            # gather waits on it forever. Observed exactly that: the test body
            # ran to completion, aclose returned, and teardown hung with
            # `<Task cancelling name='Task-1'>` as the only live task.
            #
            # asyncio.wait reports completion without delivering the child's
            # exception, which is what a shutdown wants: the conductor holds
            # no state that must be unwound, so its outcome is not news.
            await asyncio.wait({task}, timeout=2.0)

    # -- the loop -------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                queue = self._queue
                if queue is None:
                    return
                ticket = await queue.get()
                if ticket.cancelled:
                    ticket.granted.set()
                    continue

                # Wait for the speakers BEFORE committing to this ticket.
                await self._await_hardware_idle()

                # Re-arbitrate at the moment of grant. A ticket that has been
                # taken out of the priority queue can no longer be outranked —
                # so holding one across the wait for the hardware defeats the
                # priority queue during exactly the window that matters. A
                # SECONDARY that arrived at an idle turnstile would then speak
                # ahead of the PRIMARY that was addressed a moment later,
                # which is the collision this module exists to prevent.
                ticket = self._take_best(queue, ticket)
                if ticket.cancelled:
                    ticket.granted.set()
                    continue

                self.granted_order.append((ticket.role, ticket.agent))
                ticket.granted.set()
                try:
                    await asyncio.wait_for(
                        ticket.done.wait(), timeout=ticket_timeout_s(),
                    )
                except asyncio.TimeoutError:
                    # Fail OPEN. Holding the turnstile for a caller that will
                    # never signal would mute every later utterance — strictly
                    # worse than the overlap this exists to prevent.
                    logger.warning(
                        "[SpeechQueue] %s held the speakers for %.0fs without "
                        "finishing — releasing the turnstile",
                        ticket.agent or "?", ticket_timeout_s(),
                    )

                # The tail runs AFTER the utterance, inside the turnstile, so
                # the next agent cannot start inside the previous one's decay.
                await self._await_hardware_idle()
                tail = acoustic_tail_s()
                if tail > 0:
                    await asyncio.sleep(tail)
            except asyncio.CancelledError:
                raise
            except (RuntimeError, AttributeError, TypeError, ValueError):
                # A defect in one utterance must not silence the conductor.
                logger.warning("[SpeechQueue] conductor step failed", exc_info=True)
                await asyncio.sleep(0.05)

    def _take_best(
        self, queue: asyncio.PriorityQueue, held: SpeechTicket,
    ) -> SpeechTicket:
        """The highest-priority ticket among *held* and everything queued.

        Drains and re-inserts rather than peeking at the heap: the queue is
        bounded (16 by default) and this runs once per grant, so the cost is
        trivial, while reaching into ``_queue`` would couple the conductor to
        an implementation detail of asyncio."""
        best = held
        rest = []
        while not queue.empty():
            try:
                other = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if other.cancelled:
                other.granted.set()
                continue
            if other < best:
                rest.append(best)
                best = other
            else:
                rest.append(other)
        for item in rest:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:      # cannot happen: same count back
                item.cancelled = True
                item.granted.set()
        return best

    async def _await_hardware_idle(self) -> None:
        """Poll the busy counter. NEVER blocks the loop, always bounded."""
        deadline = time.monotonic() + hardware_idle_timeout_s()
        while hardware_busy():
            if time.monotonic() >= deadline:
                logger.warning(
                    "[SpeechQueue] speakers still busy after %.0fs — granting "
                    "anyway rather than going mute",
                    hardware_idle_timeout_s(),
                )
                return
            await asyncio.sleep(0.02)

    # -- the one call the speech paths make ------------------------------

    async def speak(
        self,
        play: Callable[[], Awaitable[None]],
        *,
        agent: str = "",
        role: SpeechRole = SpeechRole.PRIMARY,
        text: str = "",
    ) -> bool:
        """Run *play* with exclusive use of the speakers.

        Returns False when the turn was cancelled before it was granted —
        barge-in, or shutdown. The caller's own cancel_event still governs
        what happens once it IS granted; this decides only WHEN.

        With the scheduler disabled, *play* runs immediately, so the switch
        is a true bypass rather than a different code path."""
        if not scheduler_enabled():
            await play()
            return True

        ticket = SpeechTicket(role=role, seq=next(_SEQ), agent=agent, text=text)
        queue = self._ensure()
        try:
            queue.put_nowait(ticket)
        except asyncio.QueueFull:
            # Shed the LOWEST-priority waiter rather than the new arrival:
            # dropping what just arrived would discard a PRIMARY because
            # background chatter got there first.
            logger.warning("[SpeechQueue] full — shedding the lowest-priority waiter")
            shed = None
            try:
                drained = []
                while not queue.empty():
                    drained.append(queue.get_nowait())
                drained.append(ticket)
                drained.sort()
                shed = drained.pop()
                for item in drained:
                    queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            if shed is not None:
                shed.cancelled = True
                shed.granted.set()
                if shed is ticket:
                    return False

        await ticket.granted.wait()
        if ticket.cancelled:
            return False
        try:
            await play()
            return True
        finally:
            ticket.done.set()

    def cancel_pending(self, agent: str = "") -> int:
        """Drop QUEUED utterances — barge-in. Never touches the one currently
        playing: that is cancelled through its own cancel_event, and reaching
        into it from here would leave the turnstile held by a dead ticket."""
        queue = self._queue
        if queue is None:
            return 0
        keep, dropped = [], 0
        while not queue.empty():
            try:
                ticket = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if agent and ticket.agent != agent:
                keep.append(ticket)
                continue
            ticket.cancelled = True
            ticket.granted.set()
            dropped += 1
        for ticket in keep:
            try:
                queue.put_nowait(ticket)
            except asyncio.QueueFull:
                ticket.cancelled = True
                ticket.granted.set()
        return dropped

    def pending(self) -> int:
        queue = self._queue
        return 0 if queue is None else queue.qsize()


_SCHEDULER: Optional[SpeechScheduler] = None
_SCHEDULER_LOCK = threading.Lock()


def get_scheduler() -> SpeechScheduler:
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = SpeechScheduler()
        return _SCHEDULER


def reset_scheduler() -> None:
    """Test seam — drops the process-wide conductor."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        _SCHEDULER = None
    reset_hardware_state()


__all__ = [
    "SpeechRole",
    "SpeechScheduler",
    "SpeechTicket",
    "acoustic_tail_s",
    "get_scheduler",
    "hardware_busy",
    "mark_hardware_busy",
    "mark_hardware_idle",
    "reset_hardware_state",
    "reset_scheduler",
    "scheduler_enabled",
]
