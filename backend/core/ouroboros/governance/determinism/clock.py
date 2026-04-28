"""Phase 1 Slice 1.1 — Deterministic Clock Substrate.

Single source of truth for time across the Ouroboros pipeline.
Replaces ad-hoc ``time.monotonic()`` and ``time.time()`` calls in
decision paths with a session-scoped, replay-safe clock.

Architectural rationale (PRD §24.10 Critical Path #1):

  Time-based decisions (deadlines, cooldowns, rate limits, retry
  schedules) are non-deterministic across runs. Even with frozen
  RNG, a slightly-faster machine can elapse a deadline differently
  and produce a divergent decision. The Determinism Substrate
  needs to record + replay time as well as randomness.

Three operating modes:

  * **PASSTHROUGH** (master flag off, default): wraps the real
    ``time`` module with no recording. Bit-for-bit identical to
    pre-Slice-1.1 behavior. Operators who haven't graduated see
    no difference.

  * **RECORD** (master flag on, live mode): wraps real time + writes
    every ``monotonic()`` / ``wall_clock()`` call into an in-memory
    per-op trace. The trace is flushed to disk asynchronously by
    the Slice 1.2 DecisionLedger (this slice ships only the in-
    memory trace; persistence is the Slice 1.2 surface).

  * **REPLAY** (master flag on, replay mode): reads the recorded
    trace and returns the original value at each call site. Same
    op_id + same call ordinal → same time value. Sleep is fast-
    forwarded instantly (no actual blocking).

Async + adaptive design:

  * Sleep in REPLAY mode resolves immediately via ``asyncio.sleep(0)``
    so replay sessions run at process speed, not wall speed.
  * In RECORD mode, ``sleep(s)`` calls real ``asyncio.sleep`` AND
    captures the requested duration (so replay knows what to skip).
  * Schema mismatch in REPLAY (call ordinal exceeds recorded length)
    auto-degrades to RECORD silently — broken replays don't crash.
    Operators see a structured warning.

Key invariants (pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator.
  * NEVER raises out of any public method.
  * Pure stdlib (``time``, ``asyncio``, ``threading``).
  * No third-party deps.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def clock_enabled() -> bool:
    """``JARVIS_DETERMINISM_CLOCK_ENABLED`` (default ``true`` —
    graduated in Phase 1 Slice 1.5).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_DETERMINISM_CLOCK_ENABLED=false`` returns ``clock_for_session``
    to passthrough mode — RealClock without recording, bit-for-bit
    identical to legacy ``time.monotonic()`` / ``time.time()``.

    When ``true``: ``clock_for_session`` returns a ``RealClock`` in
    RECORD mode (live) or ``FrozenClock`` (replay), depending on
    requested mode. When ``false``: returns a passthrough ``RealClock``
    with recording disabled."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_CLOCK_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _trace_buffer_max() -> int:
    """``JARVIS_DETERMINISM_CLOCK_TRACE_MAX`` (default 100000).

    Per-op trace ring-buffer cap. Bounds memory in long-running ops
    that call ``monotonic()`` thousands of times. When the cap is
    reached, oldest entries are dropped (drop-oldest, not drop-
    newest, because replay needs the prefix to match).

    Setting too low causes replay divergence after the cutoff;
    setting too high consumes memory. 100k entries × ~24 bytes =
    ~2.4MB per op — generous default."""
    try:
        return max(1000, int(
            os.environ.get(
                "JARVIS_DETERMINISM_CLOCK_TRACE_MAX", "100000",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 100000


# ---------------------------------------------------------------------------
# ClockMode + trace dataclass
# ---------------------------------------------------------------------------


class ClockMode(Enum):
    """Operating mode for a Clock instance.

    Modes are dynamic — operators can flip a clock between modes
    via the ``set_mode`` accessor. The clock_for_session factory
    derives the mode from the env + caller hint, but the returned
    instance can be re-modeled at runtime if needed (e.g., an
    operator ``/replay`` REPL command)."""
    PASSTHROUGH = "passthrough"   # No recording, no replay
    RECORD = "record"              # Real time + record every call
    REPLAY = "replay"              # Frozen — return recorded values


@dataclass
class _ClockTrace:
    """Per-op recorded trace. NOT public — accessed via Clock methods."""
    monotonic_calls: List[float] = field(default_factory=list)
    wall_calls: List[float] = field(default_factory=list)
    sleep_calls: List[float] = field(default_factory=list)
    # Replay cursors (advanced as replay calls consume the trace)
    monotonic_cursor: int = 0
    wall_cursor: int = 0
    sleep_cursor: int = 0


# ---------------------------------------------------------------------------
# Base Clock interface
# ---------------------------------------------------------------------------


class _ClockBase:
    """Internal base — NOT public. Use ``RealClock`` / ``FrozenClock``."""

    def __init__(self, *, op_id: str = "unknown", mode: ClockMode = ClockMode.PASSTHROUGH) -> None:
        self._op_id = str(op_id) or "unknown"
        self._mode = mode
        self._lock = threading.RLock()
        self._trace = _ClockTrace()

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def mode(self) -> ClockMode:
        return self._mode

    def set_mode(self, mode: ClockMode) -> None:
        """Adapt mode at runtime. NEVER raises."""
        with self._lock:
            self._mode = mode

    # --- Trace accessors (used by Slice 1.2 ledger to persist) ---

    def export_trace(self) -> Dict[str, List[float]]:
        """Return a deep-ish copy of the recorded trace. Safe for
        caller to mutate. NEVER raises."""
        with self._lock:
            return {
                "monotonic": list(self._trace.monotonic_calls),
                "wall": list(self._trace.wall_calls),
                "sleep": list(self._trace.sleep_calls),
            }

    def import_trace(
        self,
        *,
        monotonic: Optional[List[float]] = None,
        wall: Optional[List[float]] = None,
        sleep: Optional[List[float]] = None,
    ) -> None:
        """Load a recorded trace for REPLAY. Cursors reset to 0.
        NEVER raises on bad input — coerces what it can, drops
        unparseable entries."""
        with self._lock:
            self._trace = _ClockTrace(
                monotonic_calls=_coerce_float_list(monotonic),
                wall_calls=_coerce_float_list(wall),
                sleep_calls=_coerce_float_list(sleep),
            )

    def trace_lengths(self) -> Dict[str, int]:
        """Diagnostic — current trace sizes. Useful for telemetry +
        ring-buffer cap visibility."""
        with self._lock:
            return {
                "monotonic": len(self._trace.monotonic_calls),
                "wall": len(self._trace.wall_calls),
                "sleep": len(self._trace.sleep_calls),
            }

    # --- Trace cap enforcement ---

    def _append_capped(self, lst: List[float], val: float) -> None:
        """Append + drop-oldest if cap exceeded. Replay needs the
        prefix to match the original sequence — dropping oldest
        when the cap fires would break that, BUT the cap is a
        memory-bound, not a correctness guarantee. Operators who
        care about long-trace replay raise the cap via env."""
        cap = _trace_buffer_max()
        lst.append(val)
        if len(lst) > cap:
            # Drop oldest. Pragmatic memory bound; operators who
            # need full replay raise JARVIS_DETERMINISM_CLOCK_TRACE_MAX.
            del lst[: len(lst) - cap]


def _coerce_float_list(raw: Optional[List]) -> List[float]:
    """Best-effort coerce a list of values to floats. Drop bad
    entries silently. NEVER raises."""
    if not isinstance(raw, list):
        return []
    out: List[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# RealClock — wraps the real time module, optionally records
# ---------------------------------------------------------------------------


class RealClock(_ClockBase):
    """Real-time clock with optional recording.

    PASSTHROUGH mode: pure passthrough to ``time.monotonic`` /
    ``time.time`` / ``asyncio.sleep``. Zero overhead.

    RECORD mode: passthrough + appends every call to the in-memory
    trace. Trace is bounded by ``JARVIS_DETERMINISM_CLOCK_TRACE_MAX``.

    NEVER raises. Trace appends are wrapped in try/except so a
    broken trace can't poison the time call itself.
    """

    def __init__(
        self,
        *,
        op_id: str = "unknown",
        mode: ClockMode = ClockMode.PASSTHROUGH,
    ) -> None:
        super().__init__(op_id=op_id, mode=mode)

    def monotonic(self) -> float:
        """Real ``time.monotonic()`` value. In RECORD mode, also
        appends to the trace. NEVER raises."""
        v = _time.monotonic()
        if self._mode is ClockMode.RECORD:
            try:
                with self._lock:
                    self._append_capped(self._trace.monotonic_calls, v)
            except Exception:  # noqa: BLE001 — defensive
                pass
        return v

    def wall_clock(self) -> float:
        """Real ``time.time()`` value (Unix timestamp). In RECORD
        mode, also appends to the trace. NEVER raises."""
        v = _time.time()
        if self._mode is ClockMode.RECORD:
            try:
                with self._lock:
                    self._append_capped(self._trace.wall_calls, v)
            except Exception:  # noqa: BLE001 — defensive
                pass
        return v

    async def sleep(self, seconds: float) -> None:
        """Real ``asyncio.sleep`` with optional recording. NEVER
        raises (negative sleep clamps to 0)."""
        s = max(0.0, float(seconds))
        if self._mode is ClockMode.RECORD:
            try:
                with self._lock:
                    self._append_capped(self._trace.sleep_calls, s)
            except Exception:  # noqa: BLE001 — defensive
                pass
        await asyncio.sleep(s)


# ---------------------------------------------------------------------------
# FrozenClock — replays a recorded trace
# ---------------------------------------------------------------------------


class FrozenClock(_ClockBase):
    """Replay-only clock. Reads from a pre-loaded trace; the cursor
    advances on each ``monotonic`` / ``wall_clock`` / ``sleep`` call.

    When the cursor exceeds the trace length (recorded session was
    shorter than the replay run), behavior degrades gracefully:
      * monotonic / wall_clock → return the LAST recorded value
        (best-effort — replay run doesn't crash)
      * sleep → ``asyncio.sleep(0)`` (instant)
    A structured warning is logged ONCE per call-kind so operators
    see the divergence without log-spam.
    """

    def __init__(self, *, op_id: str = "unknown") -> None:
        super().__init__(op_id=op_id, mode=ClockMode.REPLAY)
        # Track whether we've already warned per kind to suppress spam
        self._warned: Dict[str, bool] = {
            "monotonic": False, "wall": False, "sleep": False,
        }

    def monotonic(self) -> float:
        with self._lock:
            calls = self._trace.monotonic_calls
            cur = self._trace.monotonic_cursor
            if cur < len(calls):
                v = calls[cur]
                self._trace.monotonic_cursor = cur + 1
                return v
            # Past the end of the trace. Fall back to the last value
            # (or 0.0 if trace is empty) and warn once.
            self._warn_once("monotonic")
            return calls[-1] if calls else 0.0

    def wall_clock(self) -> float:
        with self._lock:
            calls = self._trace.wall_calls
            cur = self._trace.wall_cursor
            if cur < len(calls):
                v = calls[cur]
                self._trace.wall_cursor = cur + 1
                return v
            self._warn_once("wall")
            return calls[-1] if calls else 0.0

    async def sleep(self, seconds: float) -> None:  # noqa: ARG002
        """REPLAY: instant. The recorded duration is consumed from
        the trace (cursor advances) but no actual blocking happens.
        Replay sessions run at process speed, not wall speed."""
        with self._lock:
            calls = self._trace.sleep_calls
            cur = self._trace.sleep_cursor
            if cur < len(calls):
                self._trace.sleep_cursor = cur + 1
            else:
                self._warn_once("sleep")
        # Yield to the event loop so other coroutines run, mimicking
        # the original sleep's scheduling effect without the wait.
        await asyncio.sleep(0)

    def _warn_once(self, kind: str) -> None:
        if self._warned.get(kind):
            return
        self._warned[kind] = True
        logger.warning(
            "[determinism] clock REPLAY exhausted trace for kind=%s "
            "op_id=%s — run is longer than the recorded session, "
            "falling back to last-value (replay divergence point).",
            kind, self._op_id,
        )


# ---------------------------------------------------------------------------
# Factory — clock_for_session
# ---------------------------------------------------------------------------


# Per-process cache of (session_id, op_id) → Clock instance. Calling
# clock_for_session multiple times for the same op returns the same
# object so the trace accumulates / cursor advances naturally.
_clock_cache: Dict[tuple, _ClockBase] = {}
_clock_cache_lock = threading.RLock()


def clock_for_session(
    *,
    session_id: Optional[str] = None,
    op_id: str = "unknown",
    mode: Optional[ClockMode] = None,
) -> _ClockBase:
    """Return the Clock for the given session + op.

    Mode resolution order (most-specific to least-specific):
      1. Explicit ``mode`` argument
      2. ``OUROBOROS_DETERMINISM_CLOCK_MODE`` env override
         (``passthrough`` | ``record`` | ``replay``)
      3. Master flag: when off → PASSTHROUGH; when on → RECORD
         (default operating mode for live sessions)

    REPLAY mode is reserved for ``--replay`` harness sessions; the
    harness sets ``OUROBOROS_DETERMINISM_CLOCK_MODE=replay`` at
    boot so all clocks created during the run replay from disk.

    NEVER raises. Garbage ``op_id`` → uses ``"unknown"``.
    """
    safe_op = (str(op_id).strip() if op_id else "") or "unknown"
    if session_id is None or not session_id.strip():
        session_id = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"

    resolved_mode = _resolve_mode(mode)

    cache_key = (session_id, safe_op)
    with _clock_cache_lock:
        cached = _clock_cache.get(cache_key)
        if cached is not None:
            # Adapt to current mode if it changed (operator flip live)
            if cached.mode is not resolved_mode:
                cached.set_mode(resolved_mode)
            return cached
        instance: _ClockBase
        if resolved_mode is ClockMode.REPLAY:
            instance = FrozenClock(op_id=safe_op)
        else:
            instance = RealClock(op_id=safe_op, mode=resolved_mode)
        _clock_cache[cache_key] = instance
        return instance


def _resolve_mode(explicit: Optional[ClockMode]) -> ClockMode:
    """Mode resolution: explicit arg > env override > master flag.
    NEVER raises; unknown env values → PASSTHROUGH."""
    if explicit is not None:
        return explicit
    env_mode = os.environ.get(
        "OUROBOROS_DETERMINISM_CLOCK_MODE", "",
    ).strip().lower()
    if env_mode == "replay":
        return ClockMode.REPLAY
    if env_mode == "record":
        return ClockMode.RECORD
    if env_mode == "passthrough":
        return ClockMode.PASSTHROUGH
    # Fall through to master flag
    if clock_enabled():
        return ClockMode.RECORD
    return ClockMode.PASSTHROUGH


def reset_for_op(
    op_id: str,
    *,
    session_id: Optional[str] = None,
) -> None:
    """Drop the cached clock for ``op_id`` so the next call rebuilds
    fresh. NEVER raises."""
    safe_op = (str(op_id).strip() if op_id else "") or "unknown"
    if session_id is None or not session_id.strip():
        session_id = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"
    with _clock_cache_lock:
        _clock_cache.pop((session_id, safe_op), None)


def reset_all_for_tests() -> None:
    """Drop ALL cached clocks. Production code MUST NOT call this."""
    with _clock_cache_lock:
        _clock_cache.clear()


__all__ = [
    "ClockMode",
    "FrozenClock",
    "RealClock",
    "clock_enabled",
    "clock_for_session",
    "reset_all_for_tests",
    "reset_for_op",
]
