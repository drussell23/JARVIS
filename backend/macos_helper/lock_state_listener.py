"""Screen lock/unlock as an OS-delivered event, not a 1 Hz question.

WHAT THIS REPLACES
------------------
`system_event_monitor._system_state_monitoring_loop` asked the OS "is the
screen locked?" every second, forever. Measured on a live supervisor: 571
checks in 9.8 minutes, each emitting two INFO lines -- 1,142 of the window's
1,783 log lines, 64% of everything the process said. The answer changed zero
times.

Worse than the noise: the check ran SYNCHRONOUSLY on the event loop, and its
fallback path shells out to `osascript`. That is how a lock probe becomes a
multi-second stall, and `screen_lock_detector` frames are exactly what the
one surviving main-thread StallSampler dump contains.

WHY ctypes AND NOT pyobjc / DistributedNotificationCenter
----------------------------------------------------------
`NSDistributedNotificationCenter` is the textbook answer and it is the wrong
one HERE, for a reason already paid for in this codebase. From
`async_pipeline._fast_check_screen_locked`:

    v270.1: Replaced `from Quartz import CGSessionCopyCurrentDictionary`
    with ctypes-based check. Importing Quartz triggers AppKit._metadata
    (15K+ ObjC bridge lines) -- SIGSEGV risk when CoreAudio IO thread active.

This process runs CoreAudio. Pulling the ObjC bridge back in to avoid a
stall would trade a hitch for a crash. `DistributedNotificationCenter` also
needs a live CFRunLoop pumping on its thread, which is a second, larger
commitment on top of the first.

`notify(3)` from libSystem has neither problem: it is a C API reachable by
ctypes, it hands back a plain FILE DESCRIPTOR, and a descriptor is something
a thread can block on with `select` and an event loop can be told about with
`call_soon_threadsafe`. Measured round-trip on this machine: 0.2ms.

EVENT-PRIMARY, NEVER EVENT-ONLY
--------------------------------
`notify_register_file_descriptor` returns success for a name whether or not
anything ever posts it -- the name space is open. So registration proves the
TRANSPORT works; it cannot prove macOS POSTS these keys on this OS version.
That is not a reason to avoid the mechanism, it is a reason not to bet the
correctness of lock detection on it. The consumer keeps a slow polling
backstop, exactly the event-primary-with-a-floor shape the intake sensors
already use for `fs.changed.*`. If the notification fires, the backstop never
matters; if it never fires, behaviour degrades to the old cadence rather than
to blindness.

THREAD DISCIPLINE
-----------------
One daemon thread, blocked in `select()`, touching no loop state. The only
thing it does on wake is `loop.call_soon_threadsafe` -- the single supported
door into a running loop from another thread. Shutdown goes through a
self-pipe, not a select timeout: a timeout would reintroduce polling to stop
a poller.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import select
import threading
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.LockStateListener")

LOCK_LISTENER_SCHEMA_VERSION: str = "lock_state_listener.1"

#: Darwin notification names that accompany a console/lock transition.
#: `screenIsLocked`/`screenIsUnlocked` are the documented pair;
#: `sessionDidMoveOffConsole`/`sessionDidMoveToConsole` are what loginwindow
#: posts for fast-user-switching and, on several OS versions, for lock. All
#: four are registered because the cost of an extra descriptor is one int and
#: the cost of guessing wrong is a missed transition.
DEFAULT_KEYS: Tuple[str, ...] = (
    "com.apple.screenIsLocked",
    "com.apple.screenIsUnlocked",
    "com.apple.sessionDidMoveOffConsole",
    "com.apple.sessionDidMoveToConsole",
)

__all__ = [
    "DEFAULT_KEYS",
    "LOCK_LISTENER_SCHEMA_VERSION",
    "LockStateListener",
    "listener_enabled",
    "notify_keys",
]


def listener_enabled() -> bool:
    """``JARVIS_LOCK_EVENT_LISTENER_ENABLED`` (default true)."""
    raw = (os.environ.get("JARVIS_LOCK_EVENT_LISTENER_ENABLED", "1") or "").strip()
    return raw.lower() not in ("0", "false", "no", "off")


def notify_keys() -> Tuple[str, ...]:
    """The Darwin names to watch. ``JARVIS_LOCK_NOTIFY_KEYS`` replaces the
    default set entirely (comma-separated) -- an operator on an OS version
    that posts something else must not have to edit code. NEVER raises."""
    try:
        raw = (os.environ.get("JARVIS_LOCK_NOTIFY_KEYS") or "").strip()
        if not raw:
            return DEFAULT_KEYS
        keys = tuple(k.strip() for k in raw.split(",") if k.strip())
        return keys or DEFAULT_KEYS
    except Exception:  # noqa: BLE001
        return DEFAULT_KEYS


class LockStateListener:
    """Wakes a callback when macOS says the console/lock state moved.

    The callback is invoked ON THE EVENT LOOP via `call_soon_threadsafe` and
    is given no arguments: this class reports that SOMETHING changed, never
    what the new state is. Reading the state stays with the existing
    detector, which already knows how -- this only removes the need to ask
    on a timer.
    """

    def __init__(
        self,
        on_change: Callable[[], None],
        loop: "object" = None,
        keys: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._on_change = on_change
        self._loop = loop
        self._keys: Tuple[str, ...] = keys if keys is not None else notify_keys()
        self._libc: Optional[ctypes.CDLL] = None
        self._fds: Dict[int, str] = {}          # fd -> key name
        self._tokens: List[int] = []
        self._thread: Optional[threading.Thread] = None
        self._stop_r: int = -1
        self._stop_w: int = -1
        self._running = False
        self._wakeups = 0

    # -- introspection -----------------------------------------------------

    @property
    def available(self) -> bool:
        """True once at least one descriptor is registered and the thread is
        alive. A consumer reads this to decide how hard it still has to
        poll -- it must never be inferred from `start()` not raising."""
        return bool(self._running and self._fds)

    @property
    def wakeups(self) -> int:
        """Transitions observed. A listener that is `available` but has zero
        wakeups after a real lock is the signal that these keys are not
        posted on this OS version -- the thing registration cannot tell us."""
        return self._wakeups

    def stats(self) -> Dict[str, object]:
        """Bounded projection. NEVER raises."""
        return {
            "schema_version": LOCK_LISTENER_SCHEMA_VERSION,
            "available": self.available,
            "wakeups": self._wakeups,
            "keys": list(self._fds.values()),
            "requested_keys": list(self._keys),
        }

    # -- lifecycle ---------------------------------------------------------

    def _load(self) -> bool:
        """Bind the three libSystem symbols. NEVER raises."""
        try:
            path = ctypes.util.find_library("System")
            if not path:
                return False
            libc = ctypes.CDLL(path, use_errno=True)
            libc.notify_register_file_descriptor.restype = ctypes.c_uint32
            libc.notify_register_file_descriptor.argtypes = [
                ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
                ctypes.c_int, ctypes.POINTER(ctypes.c_int),
            ]
            libc.notify_cancel.restype = ctypes.c_uint32
            libc.notify_cancel.argtypes = [ctypes.c_int]
            self._libc = libc
            return True
        except Exception:  # noqa: BLE001 — not macOS, or no libSystem
            logger.debug("[LockListener] libSystem unavailable", exc_info=True)
            return False

    def start(self) -> bool:
        """Register and spawn the waiter. Returns whether it is live.

        NEVER raises and never blocks: every failure mode here means the
        caller keeps polling, which is exactly what it did before.
        """
        if self._running:
            return self.available
        if not listener_enabled():
            logger.debug("[LockListener] disabled by env")
            return False
        if not self._load():
            return False

        # Bind the loop we were started FROM when the caller did not name
        # one. An async caller that forgets would otherwise get its callback
        # invoked on the listener thread -- correct-looking in a test, a
        # data race in production.
        if self._loop is None:
            try:
                import asyncio
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None       # genuinely synchronous caller

        for key in self._keys:
            try:
                fd = ctypes.c_int(0)
                token = ctypes.c_int(0)
                status = self._libc.notify_register_file_descriptor(  # type: ignore[union-attr]
                    key.encode(), ctypes.byref(fd), 0, ctypes.byref(token))
                if status == 0 and fd.value >= 0:
                    self._fds[fd.value] = key
                    self._tokens.append(token.value)
                else:
                    logger.debug("[LockListener] %s -> status=%s", key, status)
            except Exception:  # noqa: BLE001
                logger.debug("[LockListener] register failed: %s", key,
                             exc_info=True)

        if not self._fds:
            logger.debug("[LockListener] no descriptors; caller keeps polling")
            return False

        try:
            self._stop_r, self._stop_w = os.pipe()
        except OSError:
            self._release()
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._wait_forever, name="jarvis-lock-listener", daemon=True)
        self._thread.start()
        logger.info(
            "[LockListener] event-driven lock detection live on %d key(s): %s "
            "— the 1Hz probe is now a backstop, not the mechanism",
            len(self._fds), ", ".join(self._fds.values()))
        return True

    def stop(self) -> None:
        """Wake the thread through the self-pipe and release. NEVER raises."""
        self._running = False
        try:
            if self._stop_w >= 0:
                os.write(self._stop_w, b"x")
        except OSError:
            pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._release()

    def _release(self) -> None:
        """Close descriptors and cancel tokens. NEVER raises."""
        for token in self._tokens:
            try:
                self._libc.notify_cancel(token)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        self._tokens.clear()
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        for fd in (self._stop_r, self._stop_w):
            try:
                if fd >= 0:
                    os.close(fd)
            except OSError:
                pass
        self._stop_r = self._stop_w = -1

    # -- the thread --------------------------------------------------------

    def _wait_forever(self) -> None:
        """Block on the descriptors. Touches no loop state. NEVER raises."""
        while self._running:
            watch = list(self._fds) + ([self._stop_r] if self._stop_r >= 0 else [])
            if not watch:
                return
            try:
                readable, _, _ = select.select(watch, [], [])
            except (OSError, ValueError):
                return              # descriptors closed under us: stopping
            if not self._running or self._stop_r in readable:
                return
            fired = False
            for fd in readable:
                try:
                    # notify(3) writes a 4-byte token per post; draining it is
                    # what re-arms the descriptor for the next transition.
                    os.read(fd, 4)
                    fired = True
                except OSError:
                    return
            if fired:
                self._wakeups += 1
                self._dispatch()

    def _dispatch(self) -> None:
        """Hand off to the loop. The ONLY thread-to-loop door. NEVER raises.

        A first version returned silently when no loop was bound, which meant
        a listener constructed without one counted the wake and dropped the
        callback -- `wakeups` incremented while nothing downstream ever ran.
        A dispatcher that swallows the event it was built to deliver is worse
        than either honest option, so both are taken explicitly: with a loop,
        `call_soon_threadsafe`; without one, a direct call, because the only
        callers that omit a loop are synchronous ones whose callback is
        already thread-safe (a `threading.Event.set`).
        """
        loop = self._loop
        try:
            if loop is not None and not loop.is_closed():  # type: ignore[attr-defined]
                loop.call_soon_threadsafe(self._on_change)  # type: ignore[attr-defined]
            else:
                self._on_change()
        except RuntimeError:
            pass                    # loop shutting down — a missed wake is
            #                         covered by the consumer's backstop
        except Exception:  # noqa: BLE001
            logger.debug("[LockListener] dispatch failed", exc_info=True)
