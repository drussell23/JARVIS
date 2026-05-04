"""Unified async event loop — single chronological event stream.

Architecture borrowed from Claude Code's ``queryLoop()`` async generator
pattern: multiple event sources are raced into one ordered stream so a
consumer can dispatch on event order (e.g., "Esc keypress arrived
between REASONING_TOKEN N and N+1 → cancel mid-stream"). The loop is
**purely additive** — existing producer→backend paths continue to
function unchanged. The unified loop subscribes to those paths as an
observer; it does not replace them.

Three design pillars:

  1. **Single chronological ordering** — every event from every source
     gets a ``monotonic_ts`` stamp at arrival; the loop yields envelopes
     in that order. Mid-token interrupt becomes deterministic: the
     consumer sees the keystroke arrive between tokens, not racing
     against them.
  2. **Pure observation** — sources are wrapped (RenderConductor as a
     :class:`UnifiedLoopBackend`, KeyBus via wildcard subscription).
     Existing fan-out continues unchanged. Removing the loop has zero
     effect on producer-side correctness.
  3. **Backend-agnostic event log** — when
     ``JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED=true``, every
     yielded envelope is also written to a JSONL file. Replay tools,
     time-travel debuggers, and headless audit consumers all read
     the same log without instrumenting individual producers.

Architectural pillars (each load-bearing):

  * **Closed-taxonomy UnifiedKind** ({RENDER, KEY, TOOL_RESULT, CANCEL})
    AST-pinned. Adding a kind requires coordinated registry update.
  * **Bounded per-source queues** with documented drop-oldest policy
    when the consumer falls behind. Drops are counted; queue depth
    is observable.
  * **Source-exception isolation** — one source raising doesn't kill
    the loop. Per-source failures logged + skipped; loop continues
    yielding from healthy sources.
  * **Idempotent stop** — ``stop()`` sets a flag; the next
    ``__aiter__`` iteration sees it + raises ``StopAsyncIteration``
    cleanly. Pending events drained before stop.
  * **No authority imports** — substrate stays descriptive only.
    Cancellation wiring is via the conductor's existing CANCEL_*
    surfaces (KeyAction.CANCEL_CURRENT_OP); the loop just observes.

Authority invariants (AST-pinned):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token / conversation_bridge.
  * :class:`UnifiedKind` member set is the documented closed set.
  * :class:`UnifiedEvent` field set closed.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_UNIFIED_EVENT_LOOP_ENABLED`` — master gate. Default
    false (additive observer surface — opt-in).
  * ``JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED`` — JSONL recorder.
    Default false. When on, requires
    ``JARVIS_UNIFIED_EVENT_LOG_PATH`` to be set.
  * ``JARVIS_UNIFIED_EVENT_LOOP_QUEUE_MAX`` — per-source queue size
    cap. Default 256. Drop-oldest when exceeded.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


UNIFIED_EVENT_LOOP_SCHEMA_VERSION: str = "unified_event_loop.1"


_FLAG_UNIFIED_EVENT_LOOP_ENABLED = "JARVIS_UNIFIED_EVENT_LOOP_ENABLED"
_FLAG_UNIFIED_EVENT_LOOP_RECORDING = (
    "JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED"
)
_FLAG_UNIFIED_EVENT_LOG_PATH = "JARVIS_UNIFIED_EVENT_LOG_PATH"
_FLAG_UNIFIED_EVENT_LOOP_QUEUE_MAX = "JARVIS_UNIFIED_EVENT_LOOP_QUEUE_MAX"


# ---------------------------------------------------------------------------
# Flag accessors
# ---------------------------------------------------------------------------


def _get_registry() -> Any:
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def is_enabled() -> bool:
    """Master gate. Default ``false`` — the loop is an additive
    observer surface. Operators opt-in for replay, time-travel
    debugging, or mid-token-interrupt determinism. When off,
    :meth:`UnifiedEventLoop.start` is a no-op; sources that publish
    via the conductor's existing fan-out continue unaffected."""
    reg = _get_registry()
    if reg is None:
        return False
    return reg.get_bool(_FLAG_UNIFIED_EVENT_LOOP_ENABLED, default=False)


def recording_enabled() -> bool:
    """JSONL recorder gate. Default false. When on, every yielded
    envelope also written to the file at
    ``JARVIS_UNIFIED_EVENT_LOG_PATH``. Independent of the master
    flag — operators may want recording without consuming the
    yielded stream programmatically (e.g., harness records for
    later replay)."""
    reg = _get_registry()
    if reg is None:
        return False
    return reg.get_bool(_FLAG_UNIFIED_EVENT_LOOP_RECORDING, default=False)


def log_path() -> Optional[str]:
    reg = _get_registry()
    if reg is None:
        return None
    raw = reg.get_str(_FLAG_UNIFIED_EVENT_LOG_PATH, default="").strip()
    return raw or None


def queue_max() -> int:
    reg = _get_registry()
    if reg is None:
        return 256
    return max(8, reg.get_int(
        _FLAG_UNIFIED_EVENT_LOOP_QUEUE_MAX, default=256, minimum=8,
    ))


# ---------------------------------------------------------------------------
# Closed taxonomies — AST-pinned
# ---------------------------------------------------------------------------


class UnifiedKind(str, enum.Enum):
    """Closed taxonomy of unified-stream event kinds. Each maps to a
    specific source family — the consumer dispatches on this value
    to extract the right typed payload."""

    RENDER = "RENDER"             # RenderEvent from RenderConductor
    KEY = "KEY"                   # KeyEvent from KeyBus
    TOOL_RESULT = "TOOL_RESULT"   # reserved — Slice 7+ tool fan-out
    CANCEL = "CANCEL"             # cancellation signal
    STOP = "STOP"                 # loop-shutdown sentinel


# ---------------------------------------------------------------------------
# UnifiedEvent — frozen typed envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnifiedEvent:
    """One envelope yielded by :class:`UnifiedEventLoop`. Frozen +
    hashable so consumers can buffer / replay without defensive copies.

    The payload is duck-typed (``Any``) because each source family has
    its own typed payload class (RenderEvent, KeyEvent, future
    ToolResult). Consumers dispatch on ``kind`` first, then unpack
    ``payload`` by the well-known type for that kind:

      * RENDER → :class:`RenderEvent` from render_conductor
      * KEY → :class:`KeyEvent` from key_input
      * TOOL_RESULT → reserved
      * CANCEL → string reason
      * STOP → empty
    """

    kind: UnifiedKind
    payload: Any
    source_label: str
    monotonic_ts: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe projection for the recorder. Payload is shallow-
        serialized via ``to_dict`` if available, else ``str(payload)``."""
        payload_dict: Any
        try:
            payload_dict = (
                self.payload.to_dict() if hasattr(self.payload, "to_dict")
                else str(self.payload)
            )
        except Exception:  # noqa: BLE001 — defensive
            payload_dict = repr(self.payload)
        return {
            "schema_version": UNIFIED_EVENT_LOOP_SCHEMA_VERSION,
            "kind": self.kind.value,
            "source_label": self.source_label,
            "monotonic_ts": self.monotonic_ts,
            "payload": payload_dict,
        }


# ---------------------------------------------------------------------------
# Per-source queue — bounded, drop-oldest, telemetry
# ---------------------------------------------------------------------------


class _SourceQueue:
    """Bounded async-friendly queue with drop-oldest policy.

    Sources push via :meth:`put_threadsafe` (sync, non-blocking) or
    :meth:`put_nowait` (async-context, non-blocking). The loop's
    consumer awaits :meth:`get`. When full, the oldest entry is
    evicted and ``dropped_count`` increments — surfaces the backpressure
    to operators rather than silently blocking the producer."""

    def __init__(self, max_size: int) -> None:
        self._max = max(1, int(max_size))
        self._buf: Deque[UnifiedEvent] = deque(maxlen=self._max)
        self._lock = threading.Lock()
        self._not_empty = asyncio.Event()
        self._dropped: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the asyncio loop that owns ``_not_empty`` so threadsafe
        callers can wake it. Must be called once from the consumer task."""
        self._loop = loop

    def put_nowait(self, event: UnifiedEvent) -> None:
        """Async-context push. Drops oldest on overflow."""
        with self._lock:
            was_full = len(self._buf) == self._max
            self._buf.append(event)
            if was_full:
                self._dropped += 1
        self._not_empty.set()

    def put_threadsafe(self, event: UnifiedEvent) -> None:
        """Cross-thread push. Same drop-oldest semantics; the
        ``_not_empty`` event wake is dispatched into the bound loop."""
        with self._lock:
            was_full = len(self._buf) == self._max
            self._buf.append(event)
            if was_full:
                self._dropped += 1
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._not_empty.set)
            except RuntimeError:
                # Loop closed between check + call — drop the wake;
                # the next async drain will see the queued event.
                pass

    async def get(self) -> Optional[UnifiedEvent]:
        """Await the next event. Returns ``None`` when the queue is
        empty AND the wake event was set spuriously (drain race)."""
        await self._not_empty.wait()
        with self._lock:
            if not self._buf:
                self._not_empty.clear()
                return None
            event = self._buf.popleft()
            if not self._buf:
                self._not_empty.clear()
        return event

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._buf)


# ---------------------------------------------------------------------------
# UnifiedEventLoop — the async generator
# ---------------------------------------------------------------------------


class UnifiedEventLoop:
    """Multi-source race that yields :class:`UnifiedEvent` envelopes
    in monotonic-ts arrival order.

    Lifecycle:

      * ``await loop.start()`` — checks master flag, binds asyncio
        loop, starts JSONL recorder if enabled.
      * ``async for event in loop.iter():`` — consumer drains the
        unified stream. Yields STOP envelope when shutting down.
      * ``await loop.stop()`` — sets stop flag, drains pending
        events, closes recorder. Idempotent.

    Sources attach via :meth:`attach_source` with a name. The loop
    owns one :class:`_SourceQueue` per source; sources push events
    via the queue (sync or async). The consumer task reads from all
    queues via ``asyncio.wait(FIRST_COMPLETED)`` race.

    No-op when the master flag is off — :meth:`start` returns False
    + :meth:`iter` yields nothing + STOP. Existing producer→backend
    paths continue unaffected.
    """

    def __init__(self, *, queue_max_override: Optional[int] = None) -> None:
        self._queues: Dict[str, _SourceQueue] = {}
        self._lock = threading.Lock()
        self._stop_event: Optional[asyncio.Event] = None
        self._started: bool = False
        self._recorder: Optional[Any] = None
        self._queue_max: int = (
            queue_max_override if queue_max_override is not None
            else queue_max()
        )

    # -- public API ------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    def attach_source(self, name: str) -> _SourceQueue:
        """Register a new source by ``name`` and return its queue.
        Sources push events into the returned queue via
        :meth:`_SourceQueue.put_nowait` (async context) or
        :meth:`put_threadsafe` (cross-thread). Idempotent — repeat
        attach with the same name returns the existing queue.

        Binds the running asyncio loop to the queue if one is
        available — required so cross-thread/cross-context puts can
        wake the consumer's ``_not_empty.wait()`` via
        ``call_soon_threadsafe``. If no loop is running yet (attach
        before start), the queue's ``_loop`` stays ``None`` until
        :meth:`start` runs the late-bind pass."""
        if not isinstance(name, str) or not name.strip():
            return _SourceQueue(self._queue_max)  # detached, never read
        with self._lock:
            existing = self._queues.get(name)
            if existing is not None:
                return existing
            q = _SourceQueue(self._queue_max)
            self._queues[name] = q
        # Bind running loop now (outside the registry lock) — sources
        # that attach after start() get their loop reference at
        # construction time, eliminating the wake-up gap.
        try:
            running_loop = asyncio.get_running_loop()
            q.bind_loop(running_loop)
        except RuntimeError:
            # No running loop — start() will bind on its own pass.
            pass
        return q

    def detach_source(self, name: str) -> bool:
        with self._lock:
            return self._queues.pop(name, None) is not None

    def sources(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._queues.keys())

    def telemetry(self) -> Dict[str, Any]:
        """Per-source depth + drop counts. Called by /render or
        observability surfaces."""
        with self._lock:
            return {
                name: {
                    "depth": q.depth,
                    "dropped": q.dropped_count,
                }
                for name, q in self._queues.items()
            }

    async def start(self) -> bool:
        """Begin operation. Returns ``True`` when the loop is live;
        ``False`` when the master flag is off OR no asyncio loop is
        running. Idempotent — second start is a no-op returning the
        prior state."""
        if self._started:
            return True
        if not is_enabled():
            return False
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._stop_event = asyncio.Event()
        # Bind every queue's wake-target to the running loop.
        with self._lock:
            for q in self._queues.values():
                q.bind_loop(running_loop)
        # Open recorder if enabled.
        if recording_enabled():
            self._recorder = _open_recorder()
        self._started = True
        return True

    async def stop(self) -> None:
        """Idempotent shutdown. Sets the stop flag; the next
        :meth:`iter` iteration yields a STOP envelope then raises
        ``StopAsyncIteration``. Closes the recorder if open."""
        if self._stop_event is not None:
            self._stop_event.set()
        self._started = False
        if self._recorder is not None:
            try:
                self._recorder.close()
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[UnifiedEventLoop] recorder close failed",
                    exc_info=True,
                )
            self._recorder = None

    async def iter(self):
        """Async generator yielding envelopes in arrival order.

        Implements the multi-source race:
          1. Snapshot the queue list under the lock
          2. Build per-queue ``get`` futures
          3. ``asyncio.wait(FIRST_COMPLETED)`` to await the first
             arriving event
          4. Yield it; cancel the losing futures (they'll be re-
             awaited next iteration)
          5. Loop until stop flag set or no sources

        Source exceptions are caught at the per-future boundary —
        a source raising doesn't terminate the loop. The exception
        is logged + that source's future is dropped from the next
        iteration's race set."""
        if not self._started:
            return
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            with self._lock:
                queues = dict(self._queues)
            if not queues:
                # No sources attached — yield STOP + exit.
                yield UnifiedEvent(
                    kind=UnifiedKind.STOP, payload="no_sources",
                    source_label="loop",
                )
                break
            futures = {
                asyncio.create_task(q.get()): name
                for name, q in queues.items()
            }
            stop_future = (
                asyncio.create_task(self._stop_event.wait())
                if self._stop_event is not None else None
            )
            wait_set = set(futures.keys())
            if stop_future is not None:
                wait_set.add(stop_future)
            try:
                done, pending = await asyncio.wait(
                    wait_set, return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[UnifiedEventLoop] race wait failed", exc_info=True,
                )
                break
            # Cancel all losing futures (they'll be re-issued next iter).
            for fut in pending:
                fut.cancel()
            # If stop fired, yield any winning + drain ALL remaining
            # queued events before STOP. Drain order is per-source
            # FIFO; cross-source ordering is "winners first, then
            # remaining queue contents in attach order" — preserves
            # arrival-order within each source while not blocking the
            # shutdown path.
            if stop_future is not None and stop_future in done:
                for fut in done:
                    if fut is stop_future:
                        continue
                    try:
                        ev = fut.result()
                    except Exception:  # noqa: BLE001 — defensive
                        ev = None
                    if ev is not None:
                        self._record(ev)
                        yield ev
                # Drain remaining per-source contents synchronously.
                with self._lock:
                    drain_queues = list(self._queues.values())
                for q in drain_queues:
                    while True:
                        # Non-blocking peek: if depth > 0, await a
                        # bounded get; else move to next source.
                        if q.depth == 0:
                            break
                        try:
                            ev = await asyncio.wait_for(
                                q.get(), timeout=0.05,
                            )
                        except asyncio.TimeoutError:
                            break
                        except Exception:  # noqa: BLE001 — defensive
                            break
                        if ev is not None:
                            self._record(ev)
                            yield ev
                yield UnifiedEvent(
                    kind=UnifiedKind.STOP, payload="stop_requested",
                    source_label="loop",
                )
                break
            # Yield winning events.
            for fut in done:
                name = futures.get(fut, "?")
                try:
                    ev = fut.result()
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[UnifiedEventLoop] source %s raised", name,
                        exc_info=True,
                    )
                    continue
                if ev is None:
                    continue  # spurious wake; re-iterate
                self._record(ev)
                yield ev

    # -- internals -------------------------------------------------------

    def _record(self, event: UnifiedEvent) -> None:
        """Best-effort JSONL append. Recorder absence + write errors
        both swallowed — the loop's main yield path is unaffected."""
        if self._recorder is None:
            return
        try:
            self._recorder.write(json.dumps(event.to_dict()) + "\n")
            self._recorder.flush()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[UnifiedEventLoop] recorder write failed", exc_info=True,
            )


def _open_recorder() -> Optional[Any]:
    """Open the JSONL recorder file. Returns ``None`` when the path
    isn't set OR when the open fails. NEVER raises."""
    path = log_path()
    if not path:
        return None
    try:
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return open(path, "a", encoding="utf-8")
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[UnifiedEventLoop] recorder open failed for %s", path,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Source adapters — additive observers over RenderConductor + KeyBus
# ---------------------------------------------------------------------------


class UnifiedLoopBackend:
    """RenderBackend adapter that forwards every RenderEvent into a
    :class:`UnifiedEventLoop` source queue. Pure observer — wraps
    every render event as a UnifiedEvent and pushes; doesn't render
    anything itself.

    Attach via ``conductor.add_backend(UnifiedLoopBackend(loop))``.
    The conductor's existing backends continue to receive events
    unchanged.
    """

    name: str = "unified_loop_render"

    _HANDLED_KINDS: frozenset = frozenset()  # handled via passthrough
    _NO_OP_KINDS: frozenset = frozenset()    # all kinds forwarded

    def __init__(self, loop: UnifiedEventLoop, *, label: str = "render") -> None:
        self._loop = loop
        self._queue = loop.attach_source(label)
        self._label = label

    def notify(self, event: Any) -> None:
        """Wrap + push. Sync (per RenderBackend contract). NEVER raises."""
        try:
            self._queue.put_threadsafe(UnifiedEvent(
                kind=UnifiedKind.RENDER,
                payload=event,
                source_label=self._label,
            ))
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[UnifiedLoopBackend] forward failed", exc_info=True,
            )

    def flush(self) -> None:
        return

    def shutdown(self) -> None:
        return


class UnifiedLoopKeySubscriber:
    """KeyBus subscriber that forwards every KeyEvent into a
    :class:`UnifiedEventLoop` source queue.

    Subscribes to all KeyName values via the bus's wildcard-by-list
    pattern. Holds the subscription token for the loop's lifetime.
    """

    def __init__(
        self, loop: UnifiedEventLoop, *, label: str = "key",
    ) -> None:
        self._loop = loop
        self._queue = loop.attach_source(label)
        self._label = label
        self._sub: Optional[Any] = None

    def attach(self) -> bool:
        """Subscribe to KeyBus. Returns True on success. NEVER raises.
        When the InputController/KeyBus isn't reachable (Slice 4 not
        wired), returns False — the loop just doesn't see KEY events."""
        try:
            from backend.core.ouroboros.governance import key_input as _ki
            ctrl = _ki.get_input_controller()
            if ctrl is None:
                return False
            bus = ctrl.bus
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[UnifiedLoopKeySubscriber] key_input unavailable",
                exc_info=True,
            )
            return False
        try:
            from backend.core.ouroboros.governance.key_input import KeyName
            self._sub = bus.subscribe(list(KeyName), self._handler)
            return True
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[UnifiedLoopKeySubscriber] subscribe failed", exc_info=True,
            )
            return False

    def detach(self) -> None:
        if self._sub is not None:
            try:
                self._sub.unsubscribe()
            except Exception:  # noqa: BLE001 — defensive
                pass
            self._sub = None

    def _handler(self, event: Any) -> None:
        try:
            self._queue.put_threadsafe(UnifiedEvent(
                kind=UnifiedKind.KEY,
                payload=event,
                source_label=self._label,
            ))
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[UnifiedLoopKeySubscriber] forward failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Singleton triplet — mirrors RenderConductor / InputController pattern
# ---------------------------------------------------------------------------


_DEFAULT_LOOP: Optional[UnifiedEventLoop] = None
_DEFAULT_LOCK = threading.Lock()


def get_unified_event_loop() -> Optional[UnifiedEventLoop]:
    with _DEFAULT_LOCK:
        return _DEFAULT_LOOP


def register_unified_event_loop(
    loop: Optional[UnifiedEventLoop],
) -> None:
    global _DEFAULT_LOOP
    with _DEFAULT_LOCK:
        _DEFAULT_LOOP = loop


def reset_unified_event_loop() -> None:
    register_unified_event_loop(None)


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    all_postures_relevant = {
        "EXPLORE": Relevance.RELEVANT,
        "CONSOLIDATE": Relevance.RELEVANT,
        "HARDEN": Relevance.RELEVANT,
        "MAINTAIN": Relevance.RELEVANT,
    }
    specs = [
        FlagSpec(
            name=_FLAG_UNIFIED_EVENT_LOOP_ENABLED,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master gate for the UnifiedEventLoop substrate "
                "(Wave 4 #1, U1). Default false — additive observer "
                "surface, opt-in. When true, the loop subscribes to "
                "RenderConductor + KeyBus and yields a single "
                "chronological event stream for replay / debugging / "
                "mid-token-interrupt determinism."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/unified_event_loop.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_UNIFIED_EVENT_LOOP_RECORDING,
            type=FlagType.BOOL,
            default=False,
            description=(
                "JSONL recorder gate. Default false. When true + "
                "JARVIS_UNIFIED_EVENT_LOG_PATH set, every yielded "
                "envelope is appended to the path as a JSONL line. "
                "Recorder failures swallowed (logged DEBUG)."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/unified_event_loop.py"
            ),
            example="false",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_UNIFIED_EVENT_LOG_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Filesystem path for the JSONL recorder. Empty (or "
                "unset) → recorder is no-op even when "
                "JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED=true. "
                "Append mode; directory created if missing."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/unified_event_loop.py"
            ),
            example=".jarvis/unified_events.jsonl",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_UNIFIED_EVENT_LOOP_QUEUE_MAX,
            type=FlagType.INT,
            default=256,
            description=(
                "Per-source bounded queue size. Default 256. When a "
                "queue fills (consumer falling behind), the oldest "
                "entry is dropped + dropped_count increments. "
                "telemetry() surfaces depth + drop counts. Min 8 "
                "clamp."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/unified_event_loop.py"
            ),
            example="256",
            since="v1.0",
        ),
    ]
    registry.bulk_register(specs, override=True)
    return len(specs)


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered
# ---------------------------------------------------------------------------


_FORBIDDEN_RICH_PREFIX: tuple = ("rich",)
_FORBIDDEN_AUTHORITY_MODULES: tuple = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.risk_tier",
    "backend.core.ouroboros.governance.risk_tier_floor",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.gate",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
    "backend.core.ouroboros.governance.cancel_token",
    "backend.core.ouroboros.governance.conversation_bridge",
)


_EXPECTED_UNIFIED_KIND = frozenset({
    "RENDER", "KEY", "TOOL_RESULT", "CANCEL", "STOP",
})
_EXPECTED_UNIFIED_EVENT_FIELDS = frozenset({
    "kind", "payload", "source_label", "monotonic_ts",
})


def _imported_modules(tree: Any) -> List:
    import ast
    out: List = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                out.append((node.lineno, mod))
    return out


def _enum_member_names(tree: Any, class_name: str) -> List[str]:
    import ast
    out: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.isupper():
                        out.append(tgt.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(
                stmt.target, ast.Name,
            ):
                if stmt.target.id.isupper():
                    out.append(stmt.target.id)
    return out


def _validate_no_rich_import(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        for forbidden in _FORBIDDEN_RICH_PREFIX:
            if mod == forbidden or mod.startswith(forbidden + "."):
                violations.append(
                    f"line {lineno}: forbidden rich import: {mod!r}"
                )
    return tuple(violations)


def _validate_no_authority_imports(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_unified_kind_closed(
    tree: Any, source: str,
) -> tuple:
    del source
    found = set(_enum_member_names(tree, "UnifiedKind"))
    if found != set(_EXPECTED_UNIFIED_KIND):
        return (
            f"UnifiedKind members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_UNIFIED_KIND)}",
        )
    return ()


def _validate_unified_event_closed(
    tree: Any, source: str,
) -> tuple:
    del source
    import ast
    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UnifiedEvent":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name,
                ):
                    found.add(stmt.target.id)
    if not found:
        return ("UnifiedEvent class not found",)
    if found != _EXPECTED_UNIFIED_EVENT_FIELDS:
        return (
            f"UnifiedEvent fields {sorted(found)} != expected "
            f"{sorted(_EXPECTED_UNIFIED_EVENT_FIELDS)}",
        )
    return ()


def _validate_discovery_symbols_present(
    tree: Any, source: str,
) -> tuple:
    del source
    import ast
    needed = {"register_flags", "register_shipped_invariants"}
    found: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in needed:
                found.add(node.name)
    missing = needed - found
    if missing:
        return (f"missing discovery symbols: {sorted(missing)}",)
    return ()


_TARGET_FILE = (
    "backend/core/ouroboros/governance/unified_event_loop.py"
)


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="unified_event_loop_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "unified_event_loop.py MUST NOT import rich.* — the "
                "loop is a substrate primitive; rendering belongs to "
                "downstream consumers of the yielded stream."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="unified_event_loop_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "unified_event_loop.py MUST NOT import any authority "
                "module (orchestrator / policy / iron_gate / etc.) "
                "OR conversation_bridge / cancel_token. The loop is "
                "purely descriptive; cancellation wiring is via the "
                "existing KeyAction.CANCEL_CURRENT_OP surface, not a "
                "direct cancel_token import."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="unified_event_loop_kind_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "UnifiedKind enum members must exactly match the "
                "documented 5-value closed set. Adding a kind requires "
                "coordinated downstream consumer + AST pin update."
            ),
            validate=_validate_unified_kind_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="unified_event_loop_event_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "UnifiedEvent field set MUST be exactly {kind, payload, "
                "source_label, monotonic_ts}. Adding/removing without "
                "coordinated to_dict + closed-taxonomy pin update is "
                "structural drift."
            ),
            validate=_validate_unified_event_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="unified_event_loop_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must be "
                "module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "UNIFIED_EVENT_LOOP_SCHEMA_VERSION",
    "UnifiedEvent",
    "UnifiedEventLoop",
    "UnifiedKind",
    "UnifiedLoopBackend",
    "UnifiedLoopKeySubscriber",
    "get_unified_event_loop",
    "is_enabled",
    "log_path",
    "queue_max",
    "recording_enabled",
    "register_flags",
    "register_shipped_invariants",
    "register_unified_event_loop",
    "reset_unified_event_loop",
]
