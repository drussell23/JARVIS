"""Sample the main thread WHILE it is blocked, from outside the loop.

The blindspot this closes
-------------------------

``ControlPlaneWatchdog`` measures lag correctly -- 5,559 events, p50 2,050ms,
p90 4,721ms, max 43.7s -- and structurally cannot say what caused it. It runs
*on* the loop, so it only regains control once the loop ticks again, by which
time the blocker has returned. Verified across every snapshot in a session:
the MainThread frames are always the watchdog itself. Capturing
``asyncio.current_task()`` fails for the same reason; the task that blocked
has already completed.

``LoopDeadman`` already solves the catastrophic case from outside the loop --
daemon thread, monotonic heartbeat age, ``sys._current_frames()``,
``os._exit(75)`` -- but it is armed at a 300s wedge ceiling with a 5s
heartbeat, and it is lethal by design. Nothing covers the band where this
system actually lives: stalls of two to forty seconds that resolve on their
own.

So this is the same proven pattern at a different threshold, sub-lethal. An
OS thread owes nothing to the loop's scheduler, so it can read the main
thread's frames at the one moment they are worth reading: while it is still
stuck in them.

Why a second tick source
------------------------

``LoopDeadman``'s heartbeat fires every 5s, so its age oscillates 0-5s in
perfect health and cannot resolve a 2s stall. The watchdog already ticks at
100ms for its own measurement; this module adds a sink for that existing tick
rather than a third timer. One ``time.monotonic()`` store per tick, no lock
on the write path.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional

logger = logging.getLogger("Ouroboros.StallAttribution")

_DEFAULT_POLL_S = 0.05
_DEFAULT_MIN_THRESHOLD_MS = 500.0
_DEFAULT_SATURATION_WINDOW_S = 5.0
_DEFAULT_SATURATION_MAX = 100
_DEFAULT_MAX_FRAMES = 40
_DEFAULT_COOLDOWN_S = 2.0


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def attribution_enabled() -> bool:
    """Default ON. Pure observability -- it samples and logs, never acts."""
    raw = (os.environ.get("JARVIS_STALL_ATTRIBUTION_ENABLED", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def stall_threshold_ms() -> float:
    """Derived from the watchdog's own definition of "starved".

    That number is already this system's answer to the question, and a
    second opinion about it would be a second thing to keep in agreement.
    An explicit override exists for operators narrowing a hunt.
    """
    override = _env_float("JARVIS_STALL_ATTRIBUTION_THRESHOLD_MS", 0.0)
    if override > 0:
        return max(_DEFAULT_MIN_THRESHOLD_MS, override)
    try:
        from backend.core.ouroboros.governance.control_plane_watchdog import (  # noqa: PLC0415
            _resolve_threshold_ms,
        )
        return max(_DEFAULT_MIN_THRESHOLD_MS, float(_resolve_threshold_ms()))
    except Exception:  # noqa: BLE001
        return _DEFAULT_MIN_THRESHOLD_MS


@dataclass
class StallRecord:
    """One sample of the main thread taken while it was blocked."""

    at_wall: float
    stalled_ms: float
    frames: List[str] = field(default_factory=list)

    @property
    def culprit(self) -> str:
        """The innermost frame -- where the thread actually is."""
        return self.frames[-1] if self.frames else "<no frames>"

    def render(self) -> str:
        return f"stalled_ms={self.stalled_ms:.0f} at {self.culprit}"


class DiagnosticSaturationFault(Exception):
    """Sampling produced more records than the host should absorb."""


class StallAttributor:
    """An OS thread that watches the loop's tick and samples when it stops.

    Sub-lethal on purpose. ``LoopDeadman`` owns the decision to end the
    process; a stall that resolves is a bug report, not a fatality, and a
    sampler that could exit would make every slow AST parse a crash.
    """

    def __init__(
        self,
        *,
        poll_s: Optional[float] = None,
        threshold_ms: Optional[float] = None,
        max_frames: Optional[int] = None,
        ring_cap: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._poll_s = poll_s if poll_s is not None else _env_float(
            "JARVIS_STALL_ATTRIBUTION_POLL_S", _DEFAULT_POLL_S,
        )
        self._threshold_ms = (
            threshold_ms if threshold_ms is not None else stall_threshold_ms()
        )
        self._max_frames = max_frames if max_frames is not None else _env_int(
            "JARVIS_STALL_ATTRIBUTION_MAX_FRAMES", _DEFAULT_MAX_FRAMES,
        )
        self._cooldown_s = _env_float(
            "JARVIS_STALL_ATTRIBUTION_COOLDOWN_S", _DEFAULT_COOLDOWN_S,
        )
        self._sat_window_s = _env_float(
            "JARVIS_STALL_ATTRIBUTION_SATURATION_WINDOW_S",
            _DEFAULT_SATURATION_WINDOW_S,
        )
        self._sat_max = _env_int(
            "JARVIS_STALL_ATTRIBUTION_SATURATION_MAX", _DEFAULT_SATURATION_MAX,
        )
        self._clock = clock

        self._last_tick = clock()
        self._main_ident = threading.main_thread().ident
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._records: Deque[StallRecord] = deque(maxlen=ring_cap)
        self._emissions: Deque[float] = deque()
        self._last_emit_at = 0.0
        self._saturated = False
        self._sample_count = 0

    # -- the loop's side -------------------------------------------------

    def note_tick(self) -> None:
        """Called from the asyncio loop to prove it is still turning.

        Deliberately lock-free: a single float store is atomic under the
        GIL, and taking a lock on the hot path would make the instrument
        part of the problem it measures.
        """
        self._last_tick = self._clock()

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def saturated(self) -> bool:
        return self._saturated

    def start(self) -> bool:
        """Arm the sampler. Idempotent. NEVER raises."""
        if not attribution_enabled() or self.running:
            return False
        try:
            self._stop.clear()
            self._last_tick = self._clock()
            self._thread = threading.Thread(
                target=self._run, name="StallAttributor", daemon=True,
            )
            self._thread.start()
            logger.info(
                "[StallAttribution] armed: threshold=%.0fms poll=%.0fms "
                "saturation=%d/%.0fs — samples the main thread WHILE blocked",
                self._threshold_ms, self._poll_s * 1000.0,
                self._sat_max, self._sat_window_s,
            )
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[StallAttribution] arm failed", exc_info=True)
            return False

    def stop(self) -> None:
        """Disarm. NEVER raises."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.5, self._poll_s * 4))
        self._thread = None

    def records(self) -> List[StallRecord]:
        with self._lock:
            return list(self._records)

    # -- the daemon thread ----------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                age_ms = (self._clock() - self._last_tick) * 1000.0
                if age_ms >= self._threshold_ms:
                    self._sample(age_ms)
            except Exception:  # noqa: BLE001 — the instrument never dies
                logger.debug("[StallAttribution] cycle ignored", exc_info=True)
            self._stop.wait(self._poll_s)

    def _saturation_tripped(self, now: float) -> bool:
        """A runaway cascade must not take the host's disk with it.

        A degraded loop can stall continuously, and a sampler that logged
        every poll would emit thousands of records a minute. The breaker
        latches: once tripped the sampler stays quiet until restarted,
        because a sampler that recovers on its own re-enters the cascade
        it just escaped.
        """
        self._emissions.append(now)
        cutoff = now - self._sat_window_s
        while self._emissions and self._emissions[0] < cutoff:
            self._emissions.popleft()
        if len(self._emissions) > self._sat_max:
            self._saturated = True
            logger.error(
                "[DiagnosticSaturationFault] %d stall samples in %.0fs "
                "exceeds %d — disarming attribution to protect host I/O. "
                "The loop is degraded continuously; the records already "
                "captured are the evidence, more would only cost disk.",
                len(self._emissions), self._sat_window_s, self._sat_max,
            )
            self._stop.set()
            return True
        return False

    def _sample(self, age_ms: float) -> None:
        now = self._clock()
        if self._saturated:
            return
        if self._cooldown_s > 0 and (now - self._last_emit_at) < self._cooldown_s:
            return
        frames = self._capture_main_frames()
        if not frames:
            return
        self._last_emit_at = now
        self._sample_count += 1
        record = StallRecord(
            at_wall=time.time(), stalled_ms=age_ms, frames=frames,
        )
        with self._lock:
            self._records.append(record)
        if self._saturation_tripped(now):
            return
        logger.warning(
            "[StallAttributionFault] main thread blocked %.0fms — innermost "
            "frame: %s\n  stack (innermost last):\n%s",
            age_ms, record.culprit,
            "\n".join(f"    {f}" for f in frames),
        )

    def _capture_main_frames(self) -> List[str]:
        """The main thread's stack, read from outside it.

        ``sys._current_frames()`` is the one primitive that can answer this
        during the stall -- the same one ``LoopDeadman`` uses for its
        tombstone, at a threshold two orders of magnitude lower and without
        ending the process.
        """
        try:
            frames = sys._current_frames()  # noqa: SLF001 — the only way in
            frame = frames.get(self._main_ident)
            if frame is None:
                return []
            stack = traceback.extract_stack(frame)
            rendered = [
                f"{f.filename}:{f.lineno} in {f.name}" for f in stack
            ]
            return rendered[-self._max_frames:]
        except Exception:  # noqa: BLE001
            logger.debug("[StallAttribution] frame capture failed", exc_info=True)
            return []


_default: Optional[StallAttributor] = None
_default_lock = threading.Lock()


def get_default_attributor() -> StallAttributor:
    """Process singleton. Built on first use so env is read after boot."""
    global _default  # noqa: PLW0603
    with _default_lock:
        if _default is None:
            _default = StallAttributor()
        return _default


def note_tick() -> None:
    """Module-level tick sink for the watchdog. NEVER raises."""
    try:
        get_default_attributor().note_tick()
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "DiagnosticSaturationFault",
    "StallAttributor",
    "StallRecord",
    "attribution_enabled",
    "get_default_attributor",
    "note_tick",
    "stall_threshold_ms",
]
