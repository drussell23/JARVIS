"""Control-plane starvation watchdog — Slice 11 Phase 11A.

Empirical context: bt-2026-05-22-011927 (Slice 7g acceptance soak).
After Slice 10 isolated ChromaDB, the asyncio event loop STILL
starved — sample showed the main thread blocked in
``builtin_compile → gc_collect_main`` for many seconds at a time.
The session debug.log froze for 6+ minutes because the loop
couldn't tick to emit anything.

This module is the EARLY-WARNING SIGNAL for that kind of
starvation: an independent watchdog task that schedules a
short ``asyncio.sleep`` repeatedly and measures the actual elapsed
wall-clock time vs the requested sleep. When the delta exceeds a
threshold, the loop is starved — log it loudly with
``[ControlPlaneStarvation]`` so operators see the wedge in real
time instead of after the fact.

## Discipline (Phase 11A — instrumentation only)

  * **Pure telemetry** — never modifies any other coroutine's
    behavior. Just measures and logs.
  * **Bounded resource** — single asyncio task, ~50 byte payload
    per tick. Negligible.
  * **NEVER raises** — every measurement is wrapped; instrumentation
    failure preserves system behavior.
  * **Configurable** — env knobs control cadence + threshold so
    operators can dial sensitivity per environment.
  * **Records ring** — bounded in-memory history for forensic
    inspection alongside ``ast_compile_telemetry`` records.

## API

  * ``ControlPlaneWatchdog`` — class. ``.start()`` / ``.stop()``.
  * ``get_default_watchdog()`` — process-singleton accessor (for
    harness boot wiring + observability endpoint).
  * ``recent_lag_records()`` — snapshot for forensics.

## Slice 11B handoff

When Slice 11A surfaces a caller that consistently triggers
``[ControlPlaneStarvation] lag_ms=X`` events, Slice 11B's
canonical helper (``ast_compile_helper`` / ``code_analysis_worker``)
will route that caller off the main control plane. The watchdog
provides the empirical proof that the refactor actually
eliminated the starvation."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple


logger = logging.getLogger("Ouroboros.ControlPlaneWatchdog")


# ============================================================================
# Env knobs
# ============================================================================


_MASTER_FLAG_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_ENABLED"
_INTERVAL_S_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_INTERVAL_S"
_THRESHOLD_MS_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_THRESHOLD_MS"
_RING_CAP_ENV: str = "JARVIS_CONTROL_PLANE_WATCHDOG_RING_CAP"

# Slice 12K — starvation attribution snapshot knobs. The snapshot
# threshold defaults higher than the warn threshold so we only
# capture frames for REALLY bad lag events (the warn threshold
# fires regularly under any moderate load, but snapshots are
# costly to log and noisy to read). Rate limit prevents one bad
# session from spamming logs with redundant snapshots.
_SNAPSHOT_FLAG_ENV: str = "JARVIS_CONTROL_PLANE_SNAPSHOT_ENABLED"
_SNAPSHOT_THRESHOLD_MS_ENV: str = "JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS"
_SNAPSHOT_RATE_LIMIT_S_ENV: str = "JARVIS_CONTROL_PLANE_SNAPSHOT_RATE_LIMIT_S"
_SNAPSHOT_MAX_THREADS_ENV: str = "JARVIS_CONTROL_PLANE_SNAPSHOT_MAX_THREADS"
_SNAPSHOT_MAX_FRAMES_ENV: str = "JARVIS_CONTROL_PLANE_SNAPSHOT_MAX_FRAMES"
_SNAPSHOT_RING_CAP_ENV: str = "JARVIS_CONTROL_PLANE_SNAPSHOT_RING_CAP"

_DEFAULT_INTERVAL_S: float = 0.1   # 100ms — high-resolution
_MIN_INTERVAL_S: float = 0.01
_MAX_INTERVAL_S: float = 5.0
_DEFAULT_THRESHOLD_MS: float = 500.0  # half a second of lag is alarming
_MIN_THRESHOLD_MS: float = 10.0
_MAX_THRESHOLD_MS: float = 60_000.0
_DEFAULT_RING_CAP: int = 256

# Slice 12K defaults
_DEFAULT_SNAPSHOT_THRESHOLD_MS: float = 2000.0  # 2s of lag = snapshot
_DEFAULT_SNAPSHOT_RATE_LIMIT_S: float = 30.0    # max 1 snapshot per 30s
_DEFAULT_SNAPSHOT_MAX_THREADS: int = 20
_DEFAULT_SNAPSHOT_MAX_FRAMES: int = 12          # per-thread frame cap
_DEFAULT_SNAPSHOT_RING_CAP: int = 32


def watchdog_enabled() -> bool:
    """Master gate. Default TRUE for Phase 11A. Explicit
    ``"false"`` opts out. NEVER raises."""
    try:
        return os.environ.get(_MASTER_FLAG_ENV, "").strip().lower() not in (
            "0", "false", "no", "off",
        )
    except Exception:  # noqa: BLE001
        return True


def _resolve_interval_s() -> float:
    try:
        raw = os.environ.get(_INTERVAL_S_ENV, "").strip()
        if not raw:
            return _DEFAULT_INTERVAL_S
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, min(_MAX_INTERVAL_S, v))


def _resolve_threshold_ms() -> float:
    try:
        raw = os.environ.get(_THRESHOLD_MS_ENV, "").strip()
        if not raw:
            return _DEFAULT_THRESHOLD_MS
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD_MS
    return max(_MIN_THRESHOLD_MS, min(_MAX_THRESHOLD_MS, v))


def _resolve_ring_cap() -> int:
    try:
        raw = os.environ.get(_RING_CAP_ENV, "").strip()
        if not raw:
            return _DEFAULT_RING_CAP
        return max(16, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_RING_CAP


# Slice 12K — snapshot env resolvers (all NEVER-raise, fall back
# to defaults on parse failure so a typo in env can't break boot)

def snapshot_enabled() -> bool:
    """Slice 12K — master gate for starvation attribution
    snapshots. Default TRUE (pure observability, never modifies
    behavior). Explicit ``"false"`` opts out. NEVER raises."""
    try:
        return os.environ.get(_SNAPSHOT_FLAG_ENV, "").strip().lower() not in (
            "0", "false", "no", "off",
        )
    except Exception:  # noqa: BLE001
        return True


def _resolve_snapshot_threshold_ms() -> float:
    try:
        raw = os.environ.get(_SNAPSHOT_THRESHOLD_MS_ENV, "").strip()
        if not raw:
            return _DEFAULT_SNAPSHOT_THRESHOLD_MS
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SNAPSHOT_THRESHOLD_MS
    # Snapshot threshold should not be lower than 100ms — below
    # that any normal scheduler hiccup would trigger a stack walk.
    return max(100.0, min(_MAX_THRESHOLD_MS, v))


def _resolve_snapshot_rate_limit_s() -> float:
    try:
        raw = os.environ.get(_SNAPSHOT_RATE_LIMIT_S_ENV, "").strip()
        if not raw:
            return _DEFAULT_SNAPSHOT_RATE_LIMIT_S
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SNAPSHOT_RATE_LIMIT_S
    # 0 = no rate limit; otherwise minimum 1s to avoid spam
    if v <= 0:
        return 0.0
    return max(1.0, v)


def _resolve_snapshot_max_threads() -> int:
    try:
        raw = os.environ.get(_SNAPSHOT_MAX_THREADS_ENV, "").strip()
        if not raw:
            return _DEFAULT_SNAPSHOT_MAX_THREADS
        return max(1, min(200, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_SNAPSHOT_MAX_THREADS


def _resolve_snapshot_max_frames() -> int:
    try:
        raw = os.environ.get(_SNAPSHOT_MAX_FRAMES_ENV, "").strip()
        if not raw:
            return _DEFAULT_SNAPSHOT_MAX_FRAMES
        return max(1, min(50, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_SNAPSHOT_MAX_FRAMES


def _resolve_snapshot_ring_cap() -> int:
    try:
        raw = os.environ.get(_SNAPSHOT_RING_CAP_ENV, "").strip()
        if not raw:
            return _DEFAULT_SNAPSHOT_RING_CAP
        return max(4, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SNAPSHOT_RING_CAP


# ============================================================================
# Lag record
# ============================================================================


@dataclass(frozen=True)
class LagRecord:
    """One starvation observation."""

    lag_ms: float          # observed - requested
    requested_ms: float    # the configured interval
    observed_ms: float     # actual wall-clock elapsed
    ts_monotonic: float    # event time
    thread_name: str       # main thread (asyncio loop) at instrumentation point


# ============================================================================
# Slice 12K — starvation attribution snapshot
# ============================================================================


@dataclass(frozen=True)
class ThreadFrameSnapshot:
    """One thread's frame stack at the moment of starvation
    capture. ``frames`` is a tuple of pre-formatted
    ``"<file>:<line> in <func>"`` strings, ordered from innermost
    (most recent call) to outermost. Bounded at the watchdog's
    ``max_frames`` setting per thread."""

    thread_id: int
    thread_name: str
    frames: Tuple[str, ...]


@dataclass(frozen=True)
class StarvationSnapshot:
    """One bounded capture of asyncio-loop starvation. Carries:

      * The observed lag (in ms) that triggered the capture.
      * The threshold (in ms) that fired the snapshot — operators
        can correlate against the env-tuned value.
      * Monotonic + wall-clock timestamps for cross-correlation
        against other telemetry surfaces.
      * Per-thread frame snapshots (bounded) for stack attribution.
      * Asyncio task names for any tasks currently in-flight on the
        loop — helps narrow which coroutine was running when the
        loop got starved.

    Snapshots are LOAD-BEARING for Slice 12K attribution — they
    must surface enough detail to identify the wedge culprit
    without further investigation. They are also bounded so one
    bad session can't OOM the ring."""

    lag_ms: float
    threshold_ms: float
    ts_monotonic: float
    ts_wall: float
    thread_snapshots: Tuple[ThreadFrameSnapshot, ...]
    asyncio_task_names: Tuple[str, ...]
    truncated_threads: int  # threads dropped due to max_threads cap


# ============================================================================
# Watchdog
# ============================================================================


class ControlPlaneWatchdog:
    """Independent asyncio task that detects event-loop lag.

    Lifecycle:
      * ``start()`` — sync; schedules the watchdog task. NEVER
        raises. Returns False when master flag is off OR there's
        no running loop.
      * ``stop()`` — async; cancels the watchdog cleanly with a
        bounded ``wait_for``. NEVER raises.

    The watchdog loop is shaped like::

        while running:
            t0 = time.monotonic()
            await asyncio.sleep(interval_s)
            elapsed = time.monotonic() - t0
            lag = elapsed - interval_s
            if lag * 1000.0 >= threshold_ms:
                logger.warning("[ControlPlaneStarvation] lag_ms=%.1f ...", ...)

    The ``await asyncio.sleep`` is the canonical asyncio probe —
    when the loop is healthy the elapsed ≈ interval. When the loop
    is starved (some coroutine doing sync CPU work blocking the
    GIL), ``sleep`` returns LATE because the loop wasn't ticking
    to honor the timer."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        threshold_ms: Optional[float] = None,
        snapshot_threshold_ms: Optional[float] = None,
        snapshot_rate_limit_s: Optional[float] = None,
        snapshot_max_threads: Optional[int] = None,
        snapshot_max_frames: Optional[int] = None,
    ) -> None:
        self._interval_s = (
            interval_s if interval_s is not None
            else _resolve_interval_s()
        )
        self._threshold_ms = (
            threshold_ms if threshold_ms is not None
            else _resolve_threshold_ms()
        )
        self._ring: Deque[LagRecord] = deque(maxlen=_resolve_ring_cap())
        self._ring_lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._lag_event_count: int = 0  # number of lag-threshold breaches

        # Slice 12K — starvation attribution snapshots
        self._snapshot_threshold_ms = (
            snapshot_threshold_ms if snapshot_threshold_ms is not None
            else _resolve_snapshot_threshold_ms()
        )
        self._snapshot_rate_limit_s = (
            snapshot_rate_limit_s if snapshot_rate_limit_s is not None
            else _resolve_snapshot_rate_limit_s()
        )
        self._snapshot_max_threads = (
            snapshot_max_threads if snapshot_max_threads is not None
            else _resolve_snapshot_max_threads()
        )
        self._snapshot_max_frames = (
            snapshot_max_frames if snapshot_max_frames is not None
            else _resolve_snapshot_max_frames()
        )
        self._snapshot_ring: Deque[StarvationSnapshot] = deque(
            maxlen=_resolve_snapshot_ring_cap(),
        )
        self._snapshot_ring_lock = threading.Lock()
        self._snapshot_count: int = 0
        self._snapshot_suppressed_count: int = 0
        # Monotonic timestamp of last snapshot — gates rate-limiting
        self._last_snapshot_at: float = 0.0

    # ---- introspection ----

    @property
    def interval_s(self) -> float:
        return self._interval_s

    @property
    def threshold_ms(self) -> float:
        return self._threshold_ms

    @property
    def lag_event_count(self) -> int:
        return self._lag_event_count

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def recent_lag_records(
        self, limit: Optional[int] = None,
    ) -> List[LagRecord]:
        with self._ring_lock:
            items = list(self._ring)
        if limit is not None:
            items = items[-int(limit):]
        return items

    # ---- Slice 12K — snapshot introspection ----

    @property
    def snapshot_threshold_ms(self) -> float:
        return self._snapshot_threshold_ms

    @property
    def snapshot_rate_limit_s(self) -> float:
        return self._snapshot_rate_limit_s

    @property
    def snapshot_count(self) -> int:
        """Total snapshots emitted (captured + logged + ringed)."""
        return self._snapshot_count

    @property
    def snapshot_suppressed_count(self) -> int:
        """Snapshots SUPPRESSED by the rate-limit window. Operator
        signal that lag events are repeating fast enough to be
        clipped — raise the rate-limit env knob to see more."""
        return self._snapshot_suppressed_count

    def recent_snapshots(
        self, limit: Optional[int] = None,
    ) -> List[StarvationSnapshot]:
        """Snapshot ring snapshot (no pun intended). Bounded by the
        snapshot ring cap. NEVER raises."""
        with self._snapshot_ring_lock:
            items = list(self._snapshot_ring)
        if limit is not None:
            items = items[-int(limit):]
        return items

    # ---- lifecycle ----

    def start(self) -> bool:
        """Spawn the watchdog task. No-op when master flag is
        FALSE or no running loop. Returns True when the task was
        spawned."""
        if not watchdog_enabled():
            logger.debug(
                "[ControlPlaneWatchdog] master flag FALSE — "
                "watchdog not started"
            )
            return False
        if self.running:
            return False
        try:
            self._task = asyncio.create_task(
                self._run(),
                name="control_plane_watchdog",
            )
            logger.info(
                "[ControlPlaneWatchdog] started interval=%.3fs "
                "threshold_ms=%.0f",
                self._interval_s, self._threshold_ms,
            )
            return True
        except RuntimeError:
            logger.debug(
                "[ControlPlaneWatchdog] start: no running loop"
            )
            return False

    async def stop(self) -> None:
        """Cancel cleanly. NEVER raises. Bounded by a short
        wait_for."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 — never raise
            logger.debug(
                "[ControlPlaneWatchdog] stop: exception ignored",
            )
        finally:
            self._task = None
            logger.info(
                "[ControlPlaneWatchdog] stopped lag_events=%d",
                self._lag_event_count,
            )

    # ---- the watchdog loop ----

    async def _run(self) -> None:
        """Sleeps for ``interval_s``, measures actual elapsed,
        logs when lag exceeds threshold. Independent of any other
        coroutine."""
        while True:
            try:
                t0 = time.monotonic()
                await asyncio.sleep(self._interval_s)
                elapsed = time.monotonic() - t0
                lag_s = elapsed - self._interval_s
                lag_ms = lag_s * 1000.0
                observed_ms = elapsed * 1000.0
                requested_ms = self._interval_s * 1000.0
                try:
                    with self._ring_lock:
                        self._ring.append(LagRecord(
                            lag_ms=lag_ms,
                            requested_ms=requested_ms,
                            observed_ms=observed_ms,
                            ts_monotonic=t0,
                            thread_name=threading.current_thread().name,
                        ))
                except Exception:  # noqa: BLE001
                    pass
                if lag_ms >= self._threshold_ms:
                    self._lag_event_count += 1
                    logger.warning(
                        "[ControlPlaneStarvation] lag_ms=%.1f "
                        "(requested=%.1f observed=%.1f) "
                        "threshold=%.1f event_n=%d — main asyncio "
                        "loop is starved",
                        lag_ms, requested_ms, observed_ms,
                        self._threshold_ms, self._lag_event_count,
                    )
                    # Slice 12K — starvation attribution snapshot.
                    # Capture is wrapped in a never-raise envelope:
                    # one bad cycle MUST NOT crash the watchdog,
                    # because the watchdog is itself the early-
                    # warning signal for the bigger system wedge.
                    if lag_ms >= self._snapshot_threshold_ms \
                            and snapshot_enabled():
                        try:
                            self._maybe_capture_snapshot(
                                lag_ms=lag_ms,
                                ts_monotonic=t0,
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "[ControlPlaneWatchdog] snapshot "
                                "capture exception ignored",
                                exc_info=True,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never tear down
                # Log + continue. A single bad cycle must not
                # crash the watchdog.
                logger.debug(
                    "[ControlPlaneWatchdog] cycle exception ignored",
                    exc_info=True,
                )
                # Yield briefly so we don't tight-loop on a
                # persistent error.
                try:
                    await asyncio.sleep(self._interval_s)
                except asyncio.CancelledError:
                    raise


    # ---- Slice 12K — snapshot capture machinery ----

    def _maybe_capture_snapshot(
        self,
        *,
        lag_ms: float,
        ts_monotonic: float,
    ) -> Optional[StarvationSnapshot]:
        """Rate-limited snapshot capture. Returns the snapshot
        when emitted, or None when suppressed by the rate-limit
        window. Wrapped by the caller in a never-raise envelope;
        this method itself only swallows its own helper exceptions
        and returns None on failure rather than reraise.

        Rate-limit semantics: ``snapshot_rate_limit_s <= 0`` opts
        out (no rate limit; useful for synthetic tests). Otherwise
        snapshots within the window are dropped + counted in
        ``snapshot_suppressed_count``.
        """
        now = ts_monotonic
        if self._snapshot_rate_limit_s > 0.0:
            elapsed_since_last = now - self._last_snapshot_at
            if (self._last_snapshot_at > 0.0
                    and elapsed_since_last < self._snapshot_rate_limit_s):
                self._snapshot_suppressed_count += 1
                logger.debug(
                    "[ControlPlaneSnapshot] suppressed lag_ms=%.1f "
                    "(rate_limit=%.1fs, elapsed=%.1fs)",
                    lag_ms, self._snapshot_rate_limit_s,
                    elapsed_since_last,
                )
                return None
        # Capture frames + task names. All wrapped — never raise.
        try:
            thread_snaps, truncated = _capture_thread_frames(
                max_threads=self._snapshot_max_threads,
                max_frames=self._snapshot_max_frames,
            )
        except Exception:  # noqa: BLE001
            thread_snaps, truncated = (), 0
        try:
            task_names = _capture_asyncio_task_names(
                max_tasks=self._snapshot_max_threads,
            )
        except Exception:  # noqa: BLE001
            task_names = ()
        snapshot = StarvationSnapshot(
            lag_ms=lag_ms,
            threshold_ms=self._snapshot_threshold_ms,
            ts_monotonic=now,
            ts_wall=time.time(),
            thread_snapshots=thread_snaps,
            asyncio_task_names=task_names,
            truncated_threads=truncated,
        )
        try:
            with self._snapshot_ring_lock:
                self._snapshot_ring.append(snapshot)
        except Exception:  # noqa: BLE001
            pass
        self._snapshot_count += 1
        self._last_snapshot_at = now
        try:
            self._log_snapshot(snapshot)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ControlPlaneSnapshot] log emission exception ignored",
                exc_info=True,
            )
        return snapshot

    def _log_snapshot(self, snapshot: StarvationSnapshot) -> None:
        """Emit the snapshot at WARNING level in a compact,
        operator-readable format. The format intentionally
        prefixes every block with a stable tag
        (``[ControlPlaneSnapshot]``) so grep-based attribution
        in debug.log is one regex away. NEVER raises (caller
        wraps)."""
        header = (
            f"[ControlPlaneSnapshot] lag_ms={snapshot.lag_ms:.1f} "
            f"threshold_ms={snapshot.threshold_ms:.1f} "
            f"event_n={self._snapshot_count} "
            f"suppressed_n={self._snapshot_suppressed_count} "
            f"threads={len(snapshot.thread_snapshots)} "
            f"truncated_threads={snapshot.truncated_threads} "
            f"asyncio_tasks={len(snapshot.asyncio_task_names)}"
        )
        # Single multi-line warning rather than N separate log
        # records — avoids interleaving with concurrent log calls
        # from other threads.
        lines: List[str] = [header]
        for ts in snapshot.thread_snapshots:
            lines.append(
                f"  thread[{ts.thread_id}] {ts.thread_name}:"
            )
            for f in ts.frames:
                lines.append(f"    {f}")
        if snapshot.asyncio_task_names:
            lines.append("  asyncio_tasks:")
            for n in snapshot.asyncio_task_names:
                lines.append(f"    {n}")
        logger.warning("\n".join(lines))


# ============================================================================
# Slice 12K — frame + task capture helpers (module-level, testable)
# ============================================================================


def _capture_thread_frames(
    *, max_threads: int, max_frames: int,
) -> Tuple[Tuple[ThreadFrameSnapshot, ...], int]:
    """Capture per-thread frame chains via ``sys._current_frames``.

    Returns a tuple of ``(snapshots, truncated_threads_count)`` so
    the caller can surface how many threads were dropped due to
    the cap. NEVER raises — on any exception, returns ``((), 0)``.

    The frame walk is bounded at ``max_frames`` per thread
    (innermost-first) so a deep recursion can't blow the log.
    Filenames are kept as-is (absolute paths); the operator's grep
    line-number link uses them.

    Slice 12L Part A — priority ordering. The bt-2026-05-23-002712
    soak proved Slice 12K worked but had a load-bearing gap:
    ``threads=20 truncated_threads=38`` clipped MainThread out of
    every snapshot, leaving the wedge culprit unidentifiable from
    the snapshot alone. The fix is to STABLE-SORT items by
    priority before applying the cap, so that:

      1. MainThread (the asyncio event-loop thread) ALWAYS first
      2. Then other Python-threading threads (preserves dict
         insertion order — stable sort)
      3. Then raw C threads with no Python-threading wrapper

    The max_threads cap is still honored. Within the cap,
    MainThread cannot be truncated away regardless of the cap
    value, the thread count, or the underlying dict ordering of
    ``sys._current_frames()``.
    """
    try:
        current = sys._current_frames()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return (), 0
    # Build a thread-id → name map. ``threading.enumerate()`` only
    # sees Python-threading threads, not raw C threads, so we
    # fall back to a synthetic name for unknown ids.
    name_by_id: dict = {}
    try:
        for t in threading.enumerate():
            ident = t.ident
            if ident is not None:
                name_by_id[ident] = t.name
    except Exception:  # noqa: BLE001
        pass
    # Slice 12L Part A — resolve MainThread's ident so the priority
    # sort can rank it first. ``threading.main_thread()`` is the
    # canonical accessor; wrap defensively for completeness.
    main_thread_id: Optional[int] = None
    try:
        main_thread_id = threading.main_thread().ident
    except Exception:  # noqa: BLE001
        main_thread_id = None

    def _priority(item: tuple) -> int:
        """0 = MainThread (always first); 1 = named Python-threading
        thread; 2 = raw C thread / unknown ident."""
        tid = item[0]
        if main_thread_id is not None and tid == main_thread_id:
            return 0
        if tid in name_by_id:
            return 1
        return 2

    snapshots: List[ThreadFrameSnapshot] = []
    items = list(current.items())
    # Stable sort by priority — preserves the original dict order
    # within each priority class so non-MainThread capture order
    # is unchanged.
    items.sort(key=_priority)
    truncated = max(0, len(items) - int(max_threads))
    for tid, frame in items[: int(max_threads)]:
        name = name_by_id.get(tid, f"<thread-{tid}>")
        frames: List[str] = []
        f = frame
        depth = 0
        try:
            while f is not None and depth < int(max_frames):
                co = f.f_code
                frames.append(
                    f"{co.co_filename}:{f.f_lineno} in {co.co_name}"
                )
                f = f.f_back
                depth += 1
        except Exception:  # noqa: BLE001
            pass
        snapshots.append(ThreadFrameSnapshot(
            thread_id=tid,
            thread_name=name,
            frames=tuple(frames),
        ))
    return tuple(snapshots), truncated


def _capture_asyncio_task_names(
    *, max_tasks: int,
) -> Tuple[str, ...]:
    """Capture names of asyncio tasks currently in-flight on the
    running loop. Returns an empty tuple when there is no running
    loop or any exception occurs. NEVER raises.

    Names are truncated at ``max_tasks`` items so a runaway
    coroutine explosion can't blow the log. Done tasks are
    excluded (operators want to see what's STILL running, not
    completed history)."""
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        # No running loop in this thread — fine, return empty.
        return ()
    except Exception:  # noqa: BLE001
        return ()
    names: List[str] = []
    try:
        for t in list(tasks)[: int(max_tasks)]:
            try:
                if t.done():
                    continue
                names.append(t.get_name())
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return tuple(names)
    return tuple(names)


# ============================================================================
# Process-singleton accessor
# ============================================================================


_default_watchdog: Optional[ControlPlaneWatchdog] = None
_default_lock = threading.Lock()


def get_default_watchdog() -> ControlPlaneWatchdog:
    """Process-singleton accessor. NEVER raises."""
    global _default_watchdog
    with _default_lock:
        if _default_watchdog is None:
            _default_watchdog = ControlPlaneWatchdog()
        return _default_watchdog


def reset_default_watchdog() -> None:
    """For tests."""
    global _default_watchdog
    with _default_lock:
        _default_watchdog = None


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "ControlPlaneWatchdog",
    "LagRecord",
    "watchdog_enabled",
    "get_default_watchdog",
    "reset_default_watchdog",
    # Slice 12K — starvation attribution surface
    "StarvationSnapshot",
    "ThreadFrameSnapshot",
    "snapshot_enabled",
]
