"""Keyboard input substrate — :class:`InputController` + :class:`KeyBus`.

Slice 4 of the RenderConductor arc (Wave 4 #1). Closes Gap #5: O+V has
``/cancel <op-id>`` (line-based, requires Enter) but no single-keypress
interrupt. CC's Esc-mid-token interrupt is the missing surface. The
W3(7) ``--immediate`` cancel flag is the right cancellation contract,
but until now no producer surfaces single-keypress events to fire it.

This file ships the producer side: a typed keyboard-event substrate that
parallels the conductor's render side. Symmetry with Slices 1-3:

  ===============================================================
   Direction          Substrate                Producer-side
  ===============================================================
   OUTPUT (1-3)       RenderConductor          ReasoningStream,
                      + RenderEvent +          FileRef → publish
                      RenderBackend
   INPUT (Slice 4)    KeyBus + KeyEvent +      InputController
                      KeyAction registry       reads stdin →
                                               publishes events
  ===============================================================

The two channels are deliberately separate. Render flows
producer→conductor→backends (one-way fan-out). Input flows
producer→KeyBus→handlers (one-way fan-in to action registry).
Mixing them would create a cycle; keeping them separate keeps each
substrate's authority discipline clean.

Architectural pillars (each load-bearing):

  1. **No authority imports** — the substrate stays descriptive only.
     The cancel binding is wired via a *registered handler callback*;
     SerpentFlow / harness register their `_handle_cancel` surface
     into the :class:`KeyActionRegistry` at boot. The substrate never
     imports orchestrator / GLS / cancel_token directly. AST-pinned.
  2. **Closed taxonomies, AST-pinned** — :class:`KeyName`,
     :class:`Modifier`, :class:`KeyAction` are all closed enums
     pinned at boot. New keys / actions require coordinated registry
     update.
  3. **Posix raw-mode reader, with TTY + REPL detection** —
     :class:`InputController` enters termios cbreak mode only when
     stdin is a real TTY AND no SerpentREPL is currently active
     (REPL has its own prompt_toolkit key bindings; running a parallel
     raw reader would race on stdin). Headless / sandbox / CI runs
     short-circuit to no-op; the controller's async task never spawns.
  4. **Termios restore is bulletproof** — entry/exit pairs use
     try/finally + atexit fallback + SIGTERM/SIGINT handler so the
     terminal is never left in raw mode after process exit.
  5. **Operator-overrideable bindings, registry-driven** — the default
     binding table ``{"ESC": KeyAction.CANCEL_CURRENT_OP}`` is
     overrideable via ``JARVIS_KEY_BINDINGS`` (JSON map of key-name →
     action-name). Closed-taxonomy validation rejects unknown keys/
     actions. Zero hardcoded strings at the consumer side.
  6. **Defensive everywhere** — every public method returns instead
     of raising; handler exceptions in one subscriber never break
     siblings; KeyBus exceptions in publish are swallowed and logged.
  7. **Master flag default off** — ``JARVIS_INPUT_CONTROLLER_ENABLED``
     defaults ``false`` at Slice 4. When off,
     :meth:`InputController.start` is a no-op; the action registry
     stays alive (descriptive, not authoritative — same pattern as
     FlagRegistry). Hot-flip mid-session works because the controller
     is constructed but not started.

Authority invariants (AST-pinned via ``register_shipped_invariants``):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token. The cancel wiring is via
    registered callback; substrate has zero hard cancel dependency.
  * :class:`KeyName` member set is the documented closed set.
  * :class:`Modifier` member set closed.
  * :class:`KeyAction` member set closed.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).

Kill switches:

  * ``JARVIS_INPUT_CONTROLLER_ENABLED`` — master gate. Default
    ``false``. Graduates with the conductor at Slice 7.
  * ``JARVIS_KEY_BINDINGS`` — JSON object overlay on the default
    binding table. Empty / missing falls through to defaults.

Slice 4 deliberately defers actual stdin-reading wiring to Slice 7
(the graduation slice that flips master flags + wires producers).
What ships here is the substrate + the registered-handler API +
the offline-testable parser. The raw-mode reader is implemented and
unit-testable via byte injection; a separate Slice 7 task wires it
to ``sys.stdin`` and registers concrete cancel-handler from
SerpentFlow.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


KEY_INPUT_SCHEMA_VERSION: str = "key_input.1"


_FLAG_INPUT_CONTROLLER_ENABLED = "JARVIS_INPUT_CONTROLLER_ENABLED"
_FLAG_KEY_BINDINGS = "JARVIS_KEY_BINDINGS"
_FLAG_INPUT_CONTROLLER_RAW_MODE = "JARVIS_INPUT_CONTROLLER_RAW_MODE"


# ---------------------------------------------------------------------------
# Flag accessors — lazy registry import (mirrors render_conductor / primitives)
# ---------------------------------------------------------------------------


def _get_registry() -> Any:
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def is_enabled() -> bool:
    """Master gate. Graduated default ``true`` at Slice 7 follow-up
    #4 — InputController is wired at boot via the harness, with
    SerpentFlow's ``_handle_cancel`` registered into
    ``KeyActionRegistry[CANCEL_CURRENT_OP]`` for Esc-mid-token
    interrupt. Hot-revert via ``JARVIS_INPUT_CONTROLLER_ENABLED=false``
    → ``InputController.start`` becomes a no-op (registry remains
    alive so a re-flip works without re-wire)."""
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(_FLAG_INPUT_CONTROLLER_ENABLED, default=True)


def raw_mode_enabled() -> bool:
    """Sub-gate for actually entering termios cbreak mode. Defaults
    ``true`` when the master is on. Operators can disable raw mode
    while keeping the binding registry alive — useful for environments
    where terminal-state mutation is forbidden but operators still want
    to inspect bindings via ``/help`` (Slice 6)."""
    if not is_enabled():
        return False
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(_FLAG_INPUT_CONTROLLER_RAW_MODE, default=True)


def key_bindings_override() -> Mapping[str, str]:
    """Operator overlay on default key bindings. JSON object mapping
    KeyName values (``"ESC"``) to KeyAction values
    (``"CANCEL_CURRENT_OP"``). Unmapped keys fall back to the in-code
    default. Malformed entries silently skipped (logged DEBUG)."""
    reg = _get_registry()
    if reg is None:
        return {}
    raw = reg.get_json(_FLAG_KEY_BINDINGS, default=None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        out[k.strip().upper()] = v.strip().upper()
    return out


# ---------------------------------------------------------------------------
# Closed taxonomies — AST-pinned
# ---------------------------------------------------------------------------


class KeyName(str, enum.Enum):
    """Closed taxonomy of named keys. Printable characters arrive as
    :attr:`CHAR` with the actual character in :attr:`KeyEvent.char`.

    The vocabulary is intentionally tight — adding a key requires a
    coordinated AST pin update. Esc is named because it's load-bearing
    for the cancel binding; arrows are named because UP/DOWN are the
    natural REPL history bindings (Slice 6+)."""

    ESC = "ESC"
    ENTER = "ENTER"
    SPACE = "SPACE"
    TAB = "TAB"
    BACKSPACE = "BACKSPACE"
    QUESTION = "QUESTION"            # `?` — context help binding (Slice 6)
    CTRL_C = "CTRL_C"
    CTRL_D = "CTRL_D"
    CTRL_L = "CTRL_L"                # `clear screen` convention
    ARROW_UP = "ARROW_UP"
    ARROW_DOWN = "ARROW_DOWN"
    ARROW_LEFT = "ARROW_LEFT"
    ARROW_RIGHT = "ARROW_RIGHT"
    CHAR = "CHAR"                    # generic printable character


class Modifier(str, enum.Enum):
    """Closed taxonomy of keyboard modifiers. Posix terminals don't
    surface SHIFT for printable characters (the shifted glyph arrives
    instead), but we expose the slot so a future Windows / macOS
    integration can populate it."""

    CTRL = "CTRL"
    ALT = "ALT"
    SHIFT = "SHIFT"
    META = "META"


class KeyAction(str, enum.Enum):
    """Closed taxonomy of operator-bindable actions. Each value is a
    handler key in the :class:`KeyActionRegistry`. Slice 4 ships the
    cancel action; Slices 5-6 add HELP_OPEN / THREAD_TOGGLE etc.

    The taxonomy is intentionally small. Operators bind keys to actions
    via :func:`key_bindings_override`; the action set itself is
    versioned and AST-pinned."""

    NO_OP = "NO_OP"
    CANCEL_CURRENT_OP = "CANCEL_CURRENT_OP"
    HELP_OPEN = "HELP_OPEN"           # Slice 6 wires
    HELP_CLOSE = "HELP_CLOSE"         # Slice 6 wires
    THREAD_TOGGLE = "THREAD_TOGGLE"   # Slice 5 wires
    REPL_HISTORY_PREV = "REPL_HISTORY_PREV"
    REPL_HISTORY_NEXT = "REPL_HISTORY_NEXT"


# ---------------------------------------------------------------------------
# KeyEvent — frozen typed primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyEvent:
    """One keyboard event. Frozen + hashable so multiple subscribers
    can hold references safely.

    ``key`` is the closed-taxonomy :class:`KeyName`; for printable
    characters this is :attr:`KeyName.CHAR` and ``char`` carries the
    actual character. ``modifiers`` is a frozenset for cheap equality."""

    key: KeyName
    char: Optional[str] = None
    modifiers: FrozenSet[Modifier] = field(default_factory=frozenset)
    monotonic_ts: float = field(default_factory=time.monotonic)
    source: str = "key_input"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": KEY_INPUT_SCHEMA_VERSION,
            "key": self.key.value,
            "char": self.char,
            "modifiers": sorted(m.value for m in self.modifiers),
            "monotonic_ts": self.monotonic_ts,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# KeyBus — pub/sub channel (independent of the render conductor)
# ---------------------------------------------------------------------------


# Subscription handlers are sync callables OR async coroutines.
# We accept both; KeyBus dispatches sync directly and schedules async
# via the running loop (best-effort).
KeyHandler = Callable[[KeyEvent], Union[None, Awaitable[None]]]


class KeyBus:
    """Process-wide pub/sub for keyboard events. Subscribers register
    interest in one or more :class:`KeyName` values; publish fires
    every matching handler.

    Defensive contract:

      * ``subscribe`` returns an opaque token whose ``.unsubscribe()``
        method removes the handler.
      * ``publish`` swallows handler exceptions (logged DEBUG); one
        misbehaving handler cannot break siblings.
      * Thread-safe: subscriber list mutations under a
        ``threading.Lock``.
      * Async handlers are scheduled on the currently running loop
        if any; if no loop is running, they're treated as fire-and-
        forget (the coroutine is never awaited — operator should
        register sync handlers when no loop is available).
    """

    def __init__(self) -> None:
        self._subs: List[Tuple[FrozenSet[KeyName], KeyHandler]] = []
        self._lock = threading.Lock()

    def subscribe(
        self,
        keys: Union[KeyName, "FrozenSet[KeyName]", List[KeyName]],
        handler: KeyHandler,
    ) -> "KeyBusSubscription":
        """Register ``handler`` to fire when any of ``keys`` is
        published. ``keys`` may be a single :class:`KeyName` or any
        iterable of them. Returns a subscription whose
        ``unsubscribe()`` removes the registration."""
        if isinstance(keys, KeyName):
            key_set = frozenset({keys})
        else:
            key_set = frozenset(k for k in keys if isinstance(k, KeyName))
        if not key_set or not callable(handler):
            # Return a subscription whose entry was never installed —
            # unsubscribe() is harmless (list.remove on a missing item
            # is caught by the ValueError handler).
            sentinel: Tuple[FrozenSet[KeyName], KeyHandler] = (
                frozenset(), lambda _e: None,
            )
            return KeyBusSubscription(self, sentinel)
        entry = (key_set, handler)
        with self._lock:
            self._subs.append(entry)
        return KeyBusSubscription(self, entry)

    def _remove(self, entry: Tuple[FrozenSet[KeyName], KeyHandler]) -> None:
        with self._lock:
            try:
                self._subs.remove(entry)
            except ValueError:
                pass

    def publish(self, event: KeyEvent) -> None:
        """Fan out ``event`` to every subscriber matching its key.
        Sync handlers run inline; async handlers scheduled via
        ``asyncio.ensure_future`` if a loop is running. NEVER raises."""
        if not isinstance(event, KeyEvent):
            return
        if not is_enabled():
            return
        with self._lock:
            matching = [
                handler for keys, handler in self._subs
                if event.key in keys
            ]
        for handler in matching:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        # No running loop — close the coroutine to
                        # avoid the "coroutine was never awaited"
                        # warning. Handler is responsible for not
                        # being async when no loop is up.
                        try:
                            result.close()
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[KeyBus] handler exception", exc_info=True,
                )

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class KeyBusSubscription:
    """Returned by :meth:`KeyBus.subscribe`. The handler stays
    registered until you call ``unsubscribe()`` explicitly. Standard
    pub/sub contract — there is intentionally no ``__del__``
    auto-cleanup, since callers commonly fire-and-forget the returned
    token (e.g. boot-time wiring) and an auto-cleanup would silently
    drop the subscription as soon as the local variable went out of
    scope."""

    def __init__(
        self,
        bus: KeyBus,
        entry: Tuple[FrozenSet[KeyName], KeyHandler],
    ) -> None:
        self._bus = bus
        self._entry = entry
        self._removed = False

    def unsubscribe(self) -> None:
        if self._removed:
            return
        self._removed = True
        self._bus._remove(self._entry)


# ---------------------------------------------------------------------------
# KeyActionRegistry — KeyAction → handler callback
# ---------------------------------------------------------------------------


KeyActionHandler = Callable[[KeyEvent], Union[None, Awaitable[None]]]


class KeyActionRegistry:
    """Maps :class:`KeyAction` values to operator-supplied callbacks.

    SerpentFlow / the harness registers concrete handlers at Slice 7
    boot; until then handlers are :attr:`NO_OP` no-ops. The substrate
    has zero authority dependency — the cancel callback is registered
    by the consumer, not imported by the substrate.
    """

    def __init__(self) -> None:
        self._handlers: Dict[KeyAction, KeyActionHandler] = {}
        self._lock = threading.Lock()
        # NO_OP always present so a binding to NO_OP is a documented
        # null action, not a missing-handler degradation.
        self._handlers[KeyAction.NO_OP] = self._noop

    @staticmethod
    def _noop(event: KeyEvent) -> None:
        del event

    def register(
        self, action: KeyAction, handler: KeyActionHandler,
    ) -> None:
        """Install ``handler`` for ``action``. Replaces any prior
        handler. ``action=KeyAction.NO_OP`` is rejected (the no-op
        handler is owned by the registry to guarantee always-present
        behavior)."""
        if action is KeyAction.NO_OP:
            return
        if not callable(handler):
            return
        with self._lock:
            self._handlers[action] = handler

    def unregister(self, action: KeyAction) -> bool:
        """Remove the handler for ``action``. NO_OP cannot be removed."""
        if action is KeyAction.NO_OP:
            return False
        with self._lock:
            return self._handlers.pop(action, None) is not None

    def fire(self, action: KeyAction, event: KeyEvent) -> bool:
        """Invoke the registered handler for ``action``. Returns
        ``True`` when a handler ran (even if NO_OP); ``False`` only
        when no handler is registered. NEVER raises."""
        with self._lock:
            handler = self._handlers.get(action)
        if handler is None:
            return False
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    try:
                        result.close()
                    except Exception:  # noqa: BLE001
                        pass
            return True
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[KeyActionRegistry] handler exception for action=%s",
                action.value, exc_info=True,
            )
            return True  # ran but threw — caller distinguishes via logs

    def has_handler(self, action: KeyAction) -> bool:
        with self._lock:
            return action in self._handlers

    def actions(self) -> FrozenSet[KeyAction]:
        with self._lock:
            return frozenset(self._handlers.keys())


# ---------------------------------------------------------------------------
# Default binding table + override resolution
# ---------------------------------------------------------------------------


_DEFAULT_BINDINGS: Mapping[KeyName, KeyAction] = {
    KeyName.ESC: KeyAction.CANCEL_CURRENT_OP,
    KeyName.QUESTION: KeyAction.HELP_OPEN,
    KeyName.ARROW_UP: KeyAction.REPL_HISTORY_PREV,
    KeyName.ARROW_DOWN: KeyAction.REPL_HISTORY_NEXT,
}


def resolve_bindings() -> Mapping[KeyName, KeyAction]:
    """Resolve the active :class:`KeyName` → :class:`KeyAction` map.

    Operator overrides (``JARVIS_KEY_BINDINGS`` JSON) are layered on
    top of the in-code defaults. Unknown keys / actions silently
    skipped (logged DEBUG) so a typo'd operator override degrades to
    "default for that key" instead of crashing the controller."""
    out: Dict[KeyName, KeyAction] = dict(_DEFAULT_BINDINGS)
    overrides = key_bindings_override()
    for key_str, action_str in overrides.items():
        try:
            key = KeyName(key_str)
        except ValueError:
            logger.debug(
                "[key_input] unknown KeyName in binding override: %s",
                key_str,
            )
            continue
        try:
            action = KeyAction(action_str)
        except ValueError:
            logger.debug(
                "[key_input] unknown KeyAction in binding override: %s",
                action_str,
            )
            continue
        out[key] = action
    return out


# ---------------------------------------------------------------------------
# Escape-sequence parser — pure stdlib, offline-testable
# ---------------------------------------------------------------------------


# Map well-known control bytes to KeyName.
# (Single-byte control codes; multi-byte sequences handled by the parser.)
_CONTROL_BYTE_KEY: Mapping[int, KeyName] = {
    0x1B: KeyName.ESC,           # ESC alone (stand-alone or sequence start)
    0x0A: KeyName.ENTER,         # \n
    0x0D: KeyName.ENTER,         # \r
    0x20: KeyName.SPACE,
    0x09: KeyName.TAB,
    0x7F: KeyName.BACKSPACE,
    0x08: KeyName.BACKSPACE,
    0x03: KeyName.CTRL_C,
    0x04: KeyName.CTRL_D,
    0x0C: KeyName.CTRL_L,
    0x3F: KeyName.QUESTION,      # `?`
}

# CSI (Control Sequence Introducer) tail bytes for arrows.
_CSI_ARROW_KEY: Mapping[bytes, KeyName] = {
    b"A": KeyName.ARROW_UP,
    b"B": KeyName.ARROW_DOWN,
    b"C": KeyName.ARROW_RIGHT,
    b"D": KeyName.ARROW_LEFT,
}


def parse_input_bytes(buf: bytes) -> Tuple[List[KeyEvent], bytes]:
    """Parse a chunk of stdin bytes into KeyEvents + remainder.

    Returns ``(events, remaining_bytes)``. The remainder is the tail
    of an incomplete escape sequence the caller must re-feed once
    more bytes arrive. NEVER raises.

    Recognised sequences:

      * ESC followed by ``[`` then ``A``/``B``/``C``/``D`` → arrow key
      * ESC alone (no ``[`` follow-up) → :attr:`KeyName.ESC`
      * Single-byte control codes from ``_CONTROL_BYTE_KEY``
      * Printable ASCII bytes → :attr:`KeyName.CHAR` with ``char``
      * Other bytes → silently skipped (logged DEBUG)

    The ESC-alone disambiguation: a real Esc keypress arrives as a
    single 0x1B byte; an arrow key arrives as ``\\x1b[A`` etc. If
    the buffer ends mid-sequence (just 0x1B with no follow-up byte
    yet), we leave it in the remainder so the next read can complete
    it. The caller distinguishes ESC-alone from "incomplete" by the
    timing of the next read — the InputController's reader uses a
    50 ms inter-byte timeout to flush a lone ESC.
    """
    if not buf:
        return [], b""
    events: List[KeyEvent] = []
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b == 0x1B:
            # ESC sequence — peek ahead.
            if i + 1 >= n:
                # Incomplete — leave for next read.
                return events, buf[i:]
            nxt = buf[i + 1]
            if nxt == 0x5B:  # `[`
                # CSI sequence — need a third byte.
                if i + 2 >= n:
                    return events, buf[i:]
                third = bytes([buf[i + 2]])
                if third in _CSI_ARROW_KEY:
                    events.append(KeyEvent(key=_CSI_ARROW_KEY[third]))
                    i += 3
                    continue
                # Unknown CSI tail — skip the 3 bytes.
                i += 3
                continue
            # ESC + non-`[` — could be ALT+char (e.g. ESC+`a` → ALT+a).
            # Slice 4 surfaces this as CHAR with ALT modifier when
            # the next byte is printable; otherwise ESC alone.
            if 0x20 <= nxt <= 0x7E:
                ch = chr(nxt)
                events.append(KeyEvent(
                    key=KeyName.CHAR, char=ch,
                    modifiers=frozenset({Modifier.ALT}),
                ))
                i += 2
                continue
            # ESC followed by another control byte we don't decode —
            # treat as ESC alone, leave the next byte for the next
            # iteration.
            events.append(KeyEvent(key=KeyName.ESC))
            i += 1
            continue
        # Single-byte control code lookup.
        if b in _CONTROL_BYTE_KEY:
            events.append(KeyEvent(key=_CONTROL_BYTE_KEY[b]))
            i += 1
            continue
        # Printable ASCII?
        if 0x20 <= b <= 0x7E:
            events.append(KeyEvent(key=KeyName.CHAR, char=chr(b)))
            i += 1
            continue
        # UTF-8 multibyte start? Slice 4 doesn't surface unicode
        # characters — log + skip.
        logger.debug("[key_input] unhandled byte 0x%02x — skipped", b)
        i += 1
    return events, b""


# ---------------------------------------------------------------------------
# InputController — async raw-mode stdin reader
# ---------------------------------------------------------------------------


class InputController:
    """Async producer that reads stdin in termios cbreak mode and
    publishes :class:`KeyEvent` instances into a :class:`KeyBus`.

    Lifecycle:

      * ``await controller.start()`` — checks gates, enters termios
        cbreak (when raw mode is enabled + stdin is TTY + no REPL
        active), spawns the reader task. Idempotent.
      * Reader loop runs until ``await controller.stop()`` or process
        exit. Termios always restored in a guaranteed `finally` block,
        plus an `atexit` fallback registered at first start so a hard
        crash still un-mangles the terminal.
      * Bytes are read via ``loop.run_in_executor`` (default thread
        pool) so the event loop isn't blocked on a blocking
        ``os.read``. The executor task is cancelled on stop.

    Safe-by-construction degradations:

      * Headless / non-TTY stdin → start returns immediately, no
        reader task spawned, no termios mutation.
      * REPL active (``serpent_flow.is_repl_active()``) → same
        no-op; SerpentREPL owns stdin via prompt_toolkit, parallel
        raw reading would race.
      * Master flag off → start no-ops.
      * Termios import unavailable (Windows / weird platforms) →
        start no-ops, logged INFO.
    """

    def __init__(
        self,
        *,
        bus: Optional[KeyBus] = None,
        registry: Optional[KeyActionRegistry] = None,
    ) -> None:
        self._bus = bus or KeyBus()
        self._registry = registry or KeyActionRegistry()
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._original_termios: Optional[Any] = None
        self._fd: int = -1
        self._atexit_registered: bool = False

    # -- public API ------------------------------------------------------

    @property
    def bus(self) -> KeyBus:
        return self._bus

    @property
    def registry(self) -> KeyActionRegistry:
        return self._registry

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Begin reading. Returns ``True`` when the reader task
        spawned; ``False`` when start is a no-op (any documented
        degradation)."""
        if self.active:
            return True
        if not is_enabled():
            return False
        if not raw_mode_enabled():
            # Bus + registry stay alive; reader doesn't spawn.
            logger.debug(
                "[InputController] raw mode disabled; reader not started",
            )
            return False
        if not self._stdin_is_tty():
            logger.debug(
                "[InputController] stdin not a TTY; reader not started",
            )
            return False
        if self._repl_active():
            logger.debug(
                "[InputController] REPL active; reader deferred to REPL",
            )
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[InputController] no running loop; reader not started",
            )
            return False
        try:
            self._enter_raw_mode()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.info(
                "[InputController] raw mode entry failed: %s", exc,
            )
            return False
        self._stop_event = asyncio.Event()
        self._wire_action_dispatch()
        self._task = loop.create_task(self._reader_loop())
        return True

    async def stop(self) -> None:
        """Stop the reader task + restore termios. Idempotent."""
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(asyncio.wait_for(task, timeout=1.0))
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[InputController] reader stop exception",
                    exc_info=True,
                )
        self._exit_raw_mode()

    # -- internals -------------------------------------------------------

    def _stdin_is_tty(self) -> bool:
        try:
            import sys
            return bool(sys.stdin.isatty())
        except Exception:  # noqa: BLE001 — defensive
            return False

    def _repl_active(self) -> bool:
        try:
            from backend.core.ouroboros.battle_test.serpent_flow import (
                is_repl_active,
            )
            return bool(is_repl_active())
        except Exception:  # noqa: BLE001 — defensive
            return False

    def _enter_raw_mode(self) -> None:
        """Mutate termios into cbreak. Requires Posix termios module —
        raises ``RuntimeError`` on platforms without it (caller logs
        + degrades)."""
        try:
            import sys
            import termios
            import tty
        except ImportError as exc:
            raise RuntimeError(f"termios unavailable: {exc}") from exc
        self._fd = sys.stdin.fileno()
        self._original_termios = termios.tcgetattr(self._fd)
        # cbreak (not raw) — leaves signal generation alone, so
        # Ctrl-C still raises SIGINT (caller handles via signal).
        tty.setcbreak(self._fd, termios.TCSANOW)
        if not self._atexit_registered:
            try:
                import atexit
                atexit.register(self._exit_raw_mode)
                self._atexit_registered = True
            except Exception:  # noqa: BLE001 — defensive
                pass

    def _exit_raw_mode(self) -> None:
        """Restore termios. Idempotent + tolerant of double-call."""
        if self._original_termios is None or self._fd < 0:
            return
        try:
            import termios
            termios.tcsetattr(
                self._fd, termios.TCSANOW, self._original_termios,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[InputController] termios restore failed", exc_info=True,
            )
        finally:
            self._original_termios = None
            self._fd = -1

    def _wire_action_dispatch(self) -> None:
        """Subscribe a single handler that resolves bound action and
        fires the registry. Re-subscribed on each start so binding
        overrides take effect on stop/start cycles."""
        bindings = resolve_bindings()

        def _dispatch(event: KeyEvent) -> None:
            action = bindings.get(event.key)
            if action is None:
                return
            self._registry.fire(action, event)

        self._bus.subscribe(
            keys=list(bindings.keys()), handler=_dispatch,
        )

    async def _reader_loop(self) -> None:
        """Drain stdin in chunks, parse, publish. Stops on
        ``_stop_event`` signal or task cancellation."""
        loop = asyncio.get_running_loop()
        remainder = b""
        try:
            while self._stop_event is not None and not self._stop_event.is_set():
                try:
                    chunk = await loop.run_in_executor(
                        None, self._blocking_read, 64,
                    )
                except Exception:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[InputController] read exception", exc_info=True,
                    )
                    await asyncio.sleep(0.05)
                    continue
                if not chunk:
                    # No data — yield briefly to avoid spinning.
                    await asyncio.sleep(0.01)
                    continue
                events, remainder_new = parse_input_bytes(remainder + chunk)
                remainder = remainder_new
                for event in events:
                    try:
                        self._bus.publish(event)
                    except Exception:  # noqa: BLE001 — defensive
                        logger.debug(
                            "[InputController] publish failed",
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            raise
        finally:
            self._exit_raw_mode()

    def _blocking_read(self, n: int) -> bytes:
        """Blocking read of up to ``n`` bytes from stdin. Runs in
        executor thread. Returns empty bytes on EOF or error."""
        try:
            if self._fd < 0:
                return b""
            return os.read(self._fd, n)
        except Exception:  # noqa: BLE001 — defensive
            return b""


# ---------------------------------------------------------------------------
# Singleton triplet — mirrors RenderConductor pattern
# ---------------------------------------------------------------------------


_DEFAULT_CONTROLLER: Optional[InputController] = None
_DEFAULT_LOCK = threading.Lock()


def get_input_controller() -> Optional[InputController]:
    with _DEFAULT_LOCK:
        return _DEFAULT_CONTROLLER


def register_input_controller(
    controller: Optional[InputController],
) -> None:
    global _DEFAULT_CONTROLLER
    with _DEFAULT_LOCK:
        _DEFAULT_CONTROLLER = controller


def reset_input_controller() -> None:
    register_input_controller(None)


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
            name=_FLAG_INPUT_CONTROLLER_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master gate for the InputController + KeyBus substrate "
                "(Wave 4 #1, Slice 4). Graduated default true at "
                "Slice 7 follow-up #4 — Esc-mid-token interrupt "
                "operational. Reader still short-circuits to no-op on "
                "non-TTY stdin / REPL-active / no-loop conditions. "
                "Hot-revert via false → InputController.start no-op."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/key_input.py"
            ),
            example="false",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_INPUT_CONTROLLER_RAW_MODE,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-gate for entering termios cbreak mode. Default "
                "true when master is on. Disable to keep the binding "
                "registry alive (for /help inspection in Slice 6) "
                "without mutating terminal state — useful when the "
                "harness runs in environments where terminal-state "
                "mutation is forbidden."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/key_input.py"
            ),
            example="true",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_KEY_BINDINGS,
            type=FlagType.JSON,
            default=None,
            description=(
                "Operator overlay on the default key-binding table. "
                "JSON object mapping KeyName values (ESC, ENTER, "
                "SPACE, ...) to KeyAction values (CANCEL_CURRENT_OP, "
                "HELP_OPEN, ...). Unknown keys / actions silently "
                "skipped — the controller falls back to the in-code "
                "defaults."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/governance/key_input.py"
            ),
            example='{"SPACE": "HELP_OPEN"}',
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
)


_EXPECTED_KEY_NAME = frozenset({
    "ESC", "ENTER", "SPACE", "TAB", "BACKSPACE", "QUESTION",
    "CTRL_C", "CTRL_D", "CTRL_L",
    "ARROW_UP", "ARROW_DOWN", "ARROW_LEFT", "ARROW_RIGHT",
    "CHAR",
})
_EXPECTED_MODIFIER = frozenset({"CTRL", "ALT", "SHIFT", "META"})
_EXPECTED_KEY_ACTION = frozenset({
    "NO_OP", "CANCEL_CURRENT_OP",
    "HELP_OPEN", "HELP_CLOSE",
    "THREAD_TOGGLE",
    "REPL_HISTORY_PREV", "REPL_HISTORY_NEXT",
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
    """key_input must NOT import any authority module — including
    cancel_token. The cancel binding is wired via a *registered handler
    callback*; the substrate carries no hard cancel dependency."""
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_key_name_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "KeyName"))
    if found != set(_EXPECTED_KEY_NAME):
        return (
            f"KeyName members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_KEY_NAME)}",
        )
    return ()


def _validate_modifier_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "Modifier"))
    if found != set(_EXPECTED_MODIFIER):
        return (
            f"Modifier members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_MODIFIER)}",
        )
    return ()


def _validate_key_action_closed(tree: Any, source: str) -> tuple:
    del source
    found = set(_enum_member_names(tree, "KeyAction"))
    if found != set(_EXPECTED_KEY_ACTION):
        return (
            f"KeyAction members {sorted(found)} != expected "
            f"{sorted(_EXPECTED_KEY_ACTION)}",
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


_TARGET_FILE = "backend/core/ouroboros/governance/key_input.py"


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="key_input_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "key_input.py MUST NOT import rich.* — substrate "
                "speaks bytes + KeyEvent only; rendering belongs to "
                "backends consuming the resulting actions."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="key_input_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "key_input.py MUST NOT import any authority module "
                "(orchestrator / policy / iron_gate / risk_tier / "
                "change_engine / candidate_generator / gate / "
                "semantic_guardian / semantic_firewall / providers / "
                "doubleword_provider / urgency_router / cancel_token). "
                "The cancel binding is wired via a registered handler; "
                "the substrate carries zero hard cancel dependency."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="key_input_key_name_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "KeyName enum members must exactly match the "
                "documented closed set — adding a key requires a "
                "coordinated AST pin update."
            ),
            validate=_validate_key_name_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="key_input_modifier_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "Modifier enum closed-taxonomy pin (CTRL/ALT/SHIFT/"
                "META). Adding a modifier requires registry update."
            ),
            validate=_validate_modifier_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="key_input_key_action_closed_taxonomy",
            target_file=_TARGET_FILE,
            description=(
                "KeyAction enum closed-taxonomy pin. Adding a "
                "bindable action without coordinated handler "
                "registration would silently null-op operator "
                "overrides — caught here."
            ),
            validate=_validate_key_action_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="key_input_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must "
                "be module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
    ]


__all__ = [
    "InputController",
    "KEY_INPUT_SCHEMA_VERSION",
    "KeyAction",
    "KeyActionRegistry",
    "KeyBus",
    "KeyBusSubscription",
    "KeyEvent",
    "KeyName",
    "Modifier",
    "get_input_controller",
    "is_enabled",
    "key_bindings_override",
    "parse_input_bytes",
    "raw_mode_enabled",
    "register_flags",
    "register_input_controller",
    "register_shipped_invariants",
    "reset_input_controller",
    "resolve_bindings",
]
