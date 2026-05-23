"""Sidecar Profiler — Slice 12V Phase 2.

bt-2026-05-23-192636 (Slice 12U validation soak) closed the
LoopDeadman wedge but surfaced 121 ``ControlPlaneStarvation``
events with peaks of 88s / 52s / 52s — none fatal individually, but
**unattributable** because the existing snapshot path inside
:class:`ControlPlaneWatchdog` runs as an asyncio task. When the
main loop is wedged, that task itself is suspended. Snapshots fire
only *after* the wedge releases — capturing the watchdog observing
itself in the post-recovery frame instead of the actual blocker.

Per operator binding 2026-05-23 (Slice 12V Phase 2): build a
**Sidecar Monitor** as a dedicated daemon thread, out-of-band from
the asyncio loop. The thread polls
:func:`sys._current_frames` at high frequency, tracks MainThread's
current frame, and when the same frame is seen for N consecutive
polls (= MainThread is stuck on that frame), **synchronously dumps
the IN-PROGRESS frame** via the standard logger so operators see
the actual wedge culprit while it's blocking — not the post-event
recovery frame.

This is the **only** primitive that can profile an asyncio wedge
from outside the loop. Composes:

* ``threading.Thread(daemon=True)`` for OS-level scheduling
  independent of any asyncio task — same lifecycle pattern as
  :class:`loop_deadman.LoopDeadman` (Task #103) and
  :class:`shutdown_watchdog.BoundedShutdownWatchdog`.
* :func:`sys._current_frames` — pure-stdlib, signal-safe, returns
  ``{thread_id: frame}`` for every Python thread including ones
  that are GIL-blocked (the frame is the SAME object the GIL
  holder is executing; reading the frame from a different thread
  is safe even when the GIL isn't briefly released between
  Python opcodes — frames are immutable enough for ``extract_stack``).
* The standard :mod:`logging` module — lands in ``debug.log`` via
  the harness's file handler regardless of stderr plumbing
  (mirrors Slice 12T tombstone Part 1's logger sink).

Architecture is byte-identical to the LoopDeadman pattern:

1. Daemon thread (``sidecar-profiler``).
2. Polls every ``poll_interval_s`` (default 1.0s).
3. Tracks MainThread's last-seen frame hash + age.
4. When the same frame stays for ``stuck_threshold_s`` consecutive
   polls (default 5.0s), emits a ``[SidecarProfiler.STUCK_FRAME]``
   CRITICAL log line with the full stack — once per stuck-window.
5. Re-arms after the frame changes (or after ``stuck_log_interval_s``
   has passed since the last emission — bounds the log spam if a
   single wedge lasts minutes).

Master switch ``JARVIS_SIDECAR_PROFILER_ENABLED`` (BOOL/SAFETY,
default TRUE). Knobs:

* ``JARVIS_SIDECAR_POLL_INTERVAL_S`` (FLOAT, default 1.0) — how
  often the daemon polls. Lower = more sensitive; higher = less
  thread overhead.
* ``JARVIS_SIDECAR_STUCK_THRESHOLD_S`` (FLOAT, default 5.0) —
  consecutive seconds on the same frame before declaring "stuck".
  Set above the ``ControlPlaneStarvation`` 500ms warning threshold
  so the sidecar fires on serious wedges, not normal pauses.
* ``JARVIS_SIDECAR_STUCK_LOG_INTERVAL_S`` (FLOAT, default 30.0) —
  minimum seconds between emissions for the SAME stuck frame.
  Bounds log spam on long wedges.

Public API mirrors :class:`loop_deadman.LoopDeadman` for
operator-pattern consistency: ``start()`` / ``stop()`` /
``get_default_sidecar()``.

This module NEVER raises into any caller's context.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from typing import Optional


logger = logging.getLogger("Ouroboros.SidecarProfiler")


# ============================================================================
# Env-knob resolvers
# ============================================================================


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def sidecar_enabled() -> bool:
    """``JARVIS_SIDECAR_PROFILER_ENABLED`` — default TRUE."""
    raw = os.environ.get(
        "JARVIS_SIDECAR_PROFILER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return max(low, min(high, v))
    except (TypeError, ValueError):
        return default


def sidecar_poll_interval_s() -> float:
    """``JARVIS_SIDECAR_POLL_INTERVAL_S`` — default 1.0s.
    Floored at 0.1s, ceilinged at 30s."""
    return _env_float(
        "JARVIS_SIDECAR_POLL_INTERVAL_S", 1.0, low=0.1, high=30.0,
    )


def sidecar_stuck_threshold_s() -> float:
    """``JARVIS_SIDECAR_STUCK_THRESHOLD_S`` — default 5.0s.
    Floored at 1.0s, ceilinged at 300s."""
    return _env_float(
        "JARVIS_SIDECAR_STUCK_THRESHOLD_S", 5.0, low=1.0, high=300.0,
    )


def sidecar_stuck_log_interval_s() -> float:
    """``JARVIS_SIDECAR_STUCK_LOG_INTERVAL_S`` — default 30.0s.
    Floored at 1.0s, ceilinged at 3600s. Bounds log spam on
    sustained wedges (one log per N seconds for the same frame)."""
    return _env_float(
        "JARVIS_SIDECAR_STUCK_LOG_INTERVAL_S", 30.0, low=1.0, high=3600.0,
    )


# ============================================================================
# SidecarProfiler
# ============================================================================


def _frame_signature(frame) -> str:
    """Compact stable identity for a frame — file:lineno:funcname
    of the innermost frame. Two consecutive polls with the same
    signature mean the MainThread hasn't progressed past that
    line. Cheaper than hashing the full stack (which can be deep)
    and equally diagnostic for stuck-frame detection."""
    try:
        code = frame.f_code
        return f"{code.co_filename}:{frame.f_lineno}:{code.co_name}"
    except Exception:  # noqa: BLE001
        return "<unknown_frame>"


# ── Slice 12W Phase 2 — Idle-frame exclusion registry ──
#
# bt-2026-05-23-201956 showed 7/8 STUCK_FRAME emissions on
# ``selectors.py:566:select`` — that's the asyncio event loop's
# ``kqueue.control()`` / ``epoll.poll()`` syscall in its NORMAL idle
# position when no scheduled work is ready. False positive: the loop
# isn't wedged, it's just waiting for I/O.
#
# Slice 12W tunes the sidecar: a closed registry of known-harmless
# wait-state frames. When MainThread's signature matches any entry
# (by filename basename + function name suffix), the sidecar treats
# it as legitimate idle and DOES NOT emit STUCK_FRAME. The registry
# is data-driven (not regex-matched) so adding new exclusions is one
# tuple — single source of truth.
#
# Real wedges (compute, sync I/O, lock acquire, third-party
# deadlocks) stay outside this registry and continue to fire
# STUCK_FRAME emissions. The earlier soak's lone true-positive
# (``threading.py:1590 in _shutdown``) is intentionally NOT in
# the exclusion list — that's the exact wedge class we want
# attribution for.
#
# Each entry is ``(filename_basename, function_name)``. Match is
# AND on both: filename endswith basename AND function name equals
# the second tuple element. This is more precise than substring
# matching (avoids accidentally excluding a user-defined function
# named ``select`` in some unrelated module).
_IDLE_FRAME_EXCLUSIONS: tuple = (
    # asyncio event loop's selector idle states. The loop blocks
    # on the OS's I/O multiplexer (kqueue on macOS, epoll on Linux,
    # select() fallback) when no scheduled task is ready. This is
    # the normal "loop is alive but no work" position.
    ("selectors.py", "select"),
    # asyncio.base_events.run_forever / run_until_complete /
    # _run_once — these are the loop's outer drivers. If MainThread
    # is observed inside run_forever() without descending further,
    # the loop is idle (no current task running).
    ("base_events.py", "run_forever"),
    ("base_events.py", "run_until_complete"),
    ("base_events.py", "_run_once"),
    # asyncio.events._run — the handle dispatcher; observed
    # transiently between tasks. Excluded because a single
    # measurement here means "between tasks," not "stuck on a
    # specific task."
    ("events.py", "_run"),
    # Python's threading.Event.wait + Condition.wait — the
    # canonical "blocked on a synchronization primitive without
    # work to do" patterns. These show up on daemon threads that
    # poll for work; MainThread should rarely hit them, but if
    # it does, it's legitimately idle (waiting on a signal).
    ("threading.py", "wait"),
)


def is_idle_frame(filename: str, function_name: str) -> bool:
    """Return True iff the (filename, function) pair matches any
    entry in :data:`_IDLE_FRAME_EXCLUSIONS` (legitimate idle state,
    NOT a wedge). Pure function; testable in isolation.

    Match is endswith-on-filename + equals-on-function:

      * ``selectors.py`` matches anything ending in ``selectors.py``
        (Python's stdlib path varies by install; basename match
        avoids hardcoding ``/lib/python3.11/selectors.py``).
      * Function name must match exactly — substring would be too
        loose (a user-defined ``select`` should NOT be excluded).

    Returns False for any unrecognized frame so REAL wedges
    continue to fire STUCK_FRAME emissions.
    """
    try:
        for excl_file, excl_func in _IDLE_FRAME_EXCLUSIONS:
            if (
                filename.endswith(excl_file)
                and function_name == excl_func
            ):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


class SidecarProfiler:
    """Daemon-thread monitor that catches MainThread wedges in the
    act — NEVER raises into the asyncio loop's context."""

    __slots__ = (
        "_poll_interval_s", "_stuck_threshold_s",
        "_stuck_log_interval_s",
        "_thread", "_stop_event",
        "_last_signature", "_last_signature_seen_at",
        "_last_emitted_signature", "_last_emitted_at",
        "_main_thread_id",
        "_emission_count",
    )

    def __init__(
        self,
        *,
        poll_interval_s: Optional[float] = None,
        stuck_threshold_s: Optional[float] = None,
        stuck_log_interval_s: Optional[float] = None,
    ) -> None:
        self._poll_interval_s: float = (
            poll_interval_s if poll_interval_s is not None
            else sidecar_poll_interval_s()
        )
        self._stuck_threshold_s: float = (
            stuck_threshold_s if stuck_threshold_s is not None
            else sidecar_stuck_threshold_s()
        )
        self._stuck_log_interval_s: float = (
            stuck_log_interval_s if stuck_log_interval_s is not None
            else sidecar_stuck_log_interval_s()
        )
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        # Captured at start() — the MainThread is identified by
        # its OS thread id at the moment the sidecar is armed.
        # In the JARVIS harness this is the thread running the
        # asyncio loop (``run_until_complete``).
        self._main_thread_id: Optional[int] = None
        self._last_signature: Optional[str] = None
        self._last_signature_seen_at: float = 0.0
        self._last_emitted_signature: Optional[str] = None
        self._last_emitted_at: float = 0.0
        self._emission_count: int = 0

    # ---- introspection ----

    @property
    def poll_interval_s(self) -> float:
        return self._poll_interval_s

    @property
    def stuck_threshold_s(self) -> float:
        return self._stuck_threshold_s

    @property
    def running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    @property
    def emission_count(self) -> int:
        """Total ``[SidecarProfiler.STUCK_FRAME]`` lines emitted
        since arm. Test surface; never raises."""
        return self._emission_count

    # ---- lifecycle ----

    def start(self) -> bool:
        """Arm the sidecar. Returns True on successful start, False
        when disabled or already running. NEVER raises."""
        if not sidecar_enabled():
            logger.info(
                "[SidecarProfiler] disabled via "
                "JARVIS_SIDECAR_PROFILER_ENABLED",
            )
            return False
        if self.running:
            return False
        # Capture the MainThread id at arm time. We assume the
        # arming thread is the asyncio-loop thread (consistent with
        # LoopDeadman's pattern — both armed from harness.run()
        # which executes on the loop thread).
        self._main_thread_id = threading.get_ident()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sidecar-profiler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[SidecarProfiler] armed: poll=%.1fs stuck=%.1fs "
            "log_throttle=%.1fs main_tid=%d — captures MainThread "
            "stack via sys._current_frames() while wedged",
            self._poll_interval_s, self._stuck_threshold_s,
            self._stuck_log_interval_s, self._main_thread_id,
        )
        return True

    def stop(self) -> None:
        """Signal the daemon to exit. NEVER raises. Daemon thread
        dies with the interpreter; ``stop()`` is for tests."""
        self._stop_event.set()

    # ---- the daemon thread loop ----

    def _run(self) -> None:
        """Polls MainThread's frame. On consecutive-same-frame
        detection, emits a structured logger line with the full
        stack — IN-PROGRESS attribution. NEVER raises out of this
        loop; daemon thread death would lose the signal."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — never crash the daemon
                try:
                    logger.debug(
                        "[SidecarProfiler] poll exception (handled)",
                        exc_info=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
            # Interruptible sleep — stop() can wake us early.
            self._stop_event.wait(timeout=self._poll_interval_s)

    def _poll_once(self) -> None:
        """One iteration: read MainThread frame, compare to last,
        emit if stuck past threshold."""
        if self._main_thread_id is None:
            return
        frames = sys._current_frames()
        frame = frames.get(self._main_thread_id)
        if frame is None:
            # MainThread is gone (shutdown in progress?) — nothing
            # to profile. Reset state so a new MainThread (if any)
            # gets a fresh observation window.
            self._last_signature = None
            return

        now = time.monotonic()
        signature = _frame_signature(frame)

        if signature != self._last_signature:
            # MainThread progressed — reset window.
            self._last_signature = signature
            self._last_signature_seen_at = now
            return

        # Same frame as last poll. Has it been stuck past the
        # threshold?
        age = now - self._last_signature_seen_at
        if age < self._stuck_threshold_s:
            return

        # ── Slice 12W Phase 2 — Idle-frame exclusion ──
        # If the stuck frame is in the legitimate-idle registry
        # (selectors.py:select, asyncio loop drivers, threading
        # Event/Condition.wait), suppress the emission. The
        # asyncio event loop blocking in kqueue.control() is
        # NORMAL idle state — not a wedge. Real wedges
        # (compute, sync I/O, lock contention, third-party
        # deadlocks) never match the exclusion list and
        # continue to fire.
        try:
            code = frame.f_code
            if is_idle_frame(code.co_filename, code.co_name):
                # Reset the throttle so a future REAL wedge on
                # a different frame is emitted fresh.
                return
        except Exception:  # noqa: BLE001
            # Frame introspection failed — fall through to emit
            # (better a false positive than missing a wedge).
            pass

        # Log-throttle: don't re-emit the same frame within the
        # throttle window. Different frame (or fresh window after
        # progress) resets the throttle.
        if (
            self._last_emitted_signature == signature
            and (now - self._last_emitted_at)
            < self._stuck_log_interval_s
        ):
            return

        # Emit the in-progress dump.
        try:
            stack = traceback.extract_stack(frame)
            stack_text = "".join(traceback.format_list(stack))
            logger.critical(
                "[SidecarProfiler.STUCK_FRAME] "
                "main_tid=%d stuck_for_s=%.1f "
                "frame=%s\nstack (in-progress, captured "
                "out-of-band from sidecar daemon):\n%s",
                self._main_thread_id, age, signature, stack_text,
            )
            self._last_emitted_signature = signature
            self._last_emitted_at = now
            self._emission_count += 1
        except Exception:  # noqa: BLE001 — defensive
            try:
                logger.debug(
                    "[SidecarProfiler] emission failed (handled)",
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001
                pass


# ============================================================================
# Process-singleton accessor
# ============================================================================


_default_sidecar: Optional[SidecarProfiler] = None
_default_lock: threading.Lock = threading.Lock()


def get_default_sidecar() -> SidecarProfiler:
    """Process-singleton accessor. NEVER raises."""
    global _default_sidecar
    with _default_lock:
        if _default_sidecar is None:
            _default_sidecar = SidecarProfiler()
        return _default_sidecar


def reset_default_sidecar() -> None:
    """For tests."""
    global _default_sidecar
    with _default_lock:
        _default_sidecar = None


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "SidecarProfiler",
    "_IDLE_FRAME_EXCLUSIONS",
    "get_default_sidecar",
    "is_idle_frame",
    "reset_default_sidecar",
    "sidecar_enabled",
    "sidecar_poll_interval_s",
    "sidecar_stuck_threshold_s",
    "sidecar_stuck_log_interval_s",
]
