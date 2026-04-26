"""P3.5 — Realtime progress visibility tracker.

Per OUROBOROS_VENOM_PRD.md §9 Phase 3 P3.5:

  > Problem: PLAN-EXPLOIT 3-stream takes 2-5 min with no progress UI.
  >          Operator sees silence.
  > Solution: periodic HEARTBEAT events from each stream surface as a
  >           single coalesced status line.

This module is the **consumer side** — pure in-memory tracker that
ingests per-stream HEARTBEAT events and renders one coalesced status
line per op. The **producer side** lives in
``plan_exploit.py::_generate_unit``, which spawns a 5-second periodic
emitter task per stream.

Status-line example (matches PRD spec)::

    [op-019dc42c-38d7] PLAN-EXPLOIT 3-stream: stream-1 generating (4s),
    stream-2 generating (4s), stream-3 generating (4s)
    (78s elapsed, ~120s ETA)

Authority invariants (PRD §12.2 / Manifesto §1 Boundary):
  * Pure in-memory data — no file I/O, no subprocess, no env mutation.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Bounded state — per-op stream maps capped to ``MAX_STREAMS_PER_OP``;
    process-wide op count capped to ``MAX_OPS_TRACKED`` with FIFO
    eviction so a long session can't accumulate state forever.
  * Best-effort — malformed events are dropped silently. Never raises.

P3.5 ships always-on (no env knob) per PRD spec — visibility is a
strict improvement and the only state added is bounded in-memory dicts.
The only behavioural delta a renderer caller sees is ``render_coalesced``
returning a non-empty string when ≥1 stream has reported.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional


# Per-op stream cap. PLAN-EXPLOIT typically runs 3 streams; cap at 16
# so future fan-out increases don't break the tracker.
MAX_STREAMS_PER_OP: int = 16

# Process-wide op cap. FIFO eviction at this size keeps memory bounded
# even in long-running sessions where many ops produce heartbeats but
# are never explicitly closed.
MAX_OPS_TRACKED: int = 64

# Default heartbeat cadence per PRD spec — used by ETA math + by the
# producer side for sleep interval. Pinned so tests can assert.
DEFAULT_HEARTBEAT_INTERVAL_S: float = 5.0


@dataclass(frozen=True)
class StreamTick:
    """One in-flight stream's most-recent HEARTBEAT payload."""

    stream_id: str
    unit_id: str
    activity_summary: str
    elapsed_seconds: float
    last_seen_unix: float

    @property
    def display_label(self) -> str:
        """Short form for the coalesced status line."""
        # Prefer stream_id if it's already short; else fall back to unit_id.
        return self.stream_id or self.unit_id or "stream-?"


@dataclass
class _OpStreamState:
    """Per-op aggregation. Mutable container — single-writer per scan
    via the tracker's lock."""

    op_id: str
    streams: Dict[str, StreamTick] = field(default_factory=dict)
    op_started_unix: float = 0.0


class RealtimeProgressTracker:
    """Process-wide tracker. Thread-safe via a single coarse lock — all
    public methods take it because each call mutates ≤2 dicts.

    Designed to be fed by SerpentFlow's HEARTBEAT handler:
      * On HEARTBEAT carrying ``stream`` (from plan_exploit), call
        ``record_tick``.
      * On every status-line render tick, call ``render_coalesced(op_id)``.
      * On op terminal (DECISION / POSTMORTEM), call ``forget_op``.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._ops: Dict[str, _OpStreamState] = {}

    # ---- public API ----

    def record_tick(
        self,
        op_id: str,
        stream_id: str,
        unit_id: str = "",
        activity_summary: str = "",
        elapsed_seconds: Optional[float] = None,
        now_unix: Optional[float] = None,
    ) -> None:
        """Ingest one HEARTBEAT tick. Best-effort — malformed inputs
        (empty op_id / empty stream_id) are silently dropped.

        ``now_unix`` is injectable for deterministic tests; defaults to
        ``time.time()``.
        """
        if not op_id or not stream_id:
            return
        ts = now_unix if now_unix is not None else time.time()
        try:
            elapsed = float(elapsed_seconds) if elapsed_seconds is not None else 0.0
        except (TypeError, ValueError):
            elapsed = 0.0
        elapsed = max(0.0, elapsed)
        tick = StreamTick(
            stream_id=str(stream_id),
            unit_id=str(unit_id or ""),
            activity_summary=str(activity_summary or "")[:120],
            elapsed_seconds=elapsed,
            last_seen_unix=float(ts),
        )
        with self._lock:
            state = self._ops.get(op_id)
            if state is None:
                # Cap the global op count BEFORE adding (FIFO evict).
                if len(self._ops) >= MAX_OPS_TRACKED:
                    try:
                        oldest = next(iter(self._ops))
                        self._ops.pop(oldest, None)
                    except StopIteration:
                        pass
                state = _OpStreamState(op_id=op_id, op_started_unix=ts)
                self._ops[op_id] = state
            # Per-op stream cap — drop ticks beyond cap to avoid runaway state.
            if (
                stream_id not in state.streams
                and len(state.streams) >= MAX_STREAMS_PER_OP
            ):
                return
            state.streams[stream_id] = tick

    def get_state(self, op_id: str) -> Optional[_OpStreamState]:
        """Return the per-op aggregation snapshot. ``None`` when unknown."""
        with self._lock:
            return self._ops.get(op_id)

    def forget_op(self, op_id: str) -> None:
        """Drop all state for an op (typically called on DECISION /
        POSTMORTEM terminal). Idempotent."""
        with self._lock:
            self._ops.pop(op_id, None)

    def known_op_ids(self) -> List[str]:
        with self._lock:
            return list(self._ops.keys())

    def render_coalesced(
        self,
        op_id: str,
        now_unix: Optional[float] = None,
    ) -> str:
        """Render a single-line coalesced status string for ``op_id``.

        Returns ``""`` when the op has no recorded ticks (caller should
        fall through to whatever default rendering it had pre-Slice).

        Format mirrors PRD §9 P3.5 example::

            [<op-id>] PLAN-EXPLOIT N-stream: stream-1 ..., stream-2 ...
            (Xs elapsed, ~Ys ETA)
        """
        with self._lock:
            state = self._ops.get(op_id)
            if state is None or not state.streams:
                return ""
            now = now_unix if now_unix is not None else time.time()
            elapsed_overall = max(
                0.0, now - (state.op_started_unix or now)
            )
            # Sort streams by stream_id for stable output.
            sorted_streams = sorted(
                state.streams.values(),
                key=lambda t: t.stream_id,
            )

        # Build per-stream summary outside the lock.
        n = len(sorted_streams)
        per_stream = []
        max_elapsed = 0.0
        for tick in sorted_streams:
            label = tick.display_label
            summary = tick.activity_summary or "generating"
            per_stream.append(
                f"{label} {summary} ({int(tick.elapsed_seconds)}s)"
            )
            max_elapsed = max(max_elapsed, tick.elapsed_seconds)

        eta_str = self._eta_string(elapsed_overall, max_elapsed)
        op_short = op_id[:20] if op_id else "?"
        return (
            f"[{op_short}] PLAN-EXPLOIT {n}-stream: "
            f"{', '.join(per_stream)} "
            f"({int(elapsed_overall)}s elapsed, {eta_str})"
        )

    # ---- internals ----

    @staticmethod
    def _eta_string(elapsed_overall: float, max_per_stream: float) -> str:
        """Conservative ETA estimate.

        Strategy: PLAN-EXPLOIT typically takes 2-5 min per PRD; if the
        slowest stream has been running ``X`` seconds, ETA is roughly
        the typical 180s benchmark unless we've already passed it (then
        we project ~1.5x the longest stream as remaining wall-clock).
        Returns a human-readable ``"~Ns ETA"`` string. ``"~ETA n/a"``
        when no streams have reported (defensive)."""
        if max_per_stream <= 0:
            return "~ETA n/a"
        # Anchor: 180s typical PLAN-EXPLOIT wall (per PRD §9 P3.5
        # "2-5 min"). If we're under it, project to anchor; else
        # extrapolate +50% of slowest stream.
        anchor = 180.0
        if max_per_stream < anchor:
            remaining = max(1.0, anchor - elapsed_overall)
        else:
            remaining = max(1.0, max_per_stream * 0.5)
        return f"~{int(remaining)}s ETA"


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors P0 / P1 / P3 patterns)
# ---------------------------------------------------------------------------


_default_tracker: Optional[RealtimeProgressTracker] = None
_default_lock = Lock()


def get_default_tracker() -> RealtimeProgressTracker:
    """Return the process-wide tracker. Lazy-construct on first call.

    Unlike the engine-style accessors (P1 / P3), this one has no master
    flag — P3.5 ships always-on per PRD spec. The only behaviour delta
    is in-memory state + a non-empty render output."""
    global _default_tracker
    with _default_lock:
        if _default_tracker is None:
            _default_tracker = RealtimeProgressTracker()
    return _default_tracker


def reset_default_tracker() -> None:
    """Reset the singleton — for tests."""
    global _default_tracker
    with _default_lock:
        _default_tracker = None


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "MAX_OPS_TRACKED",
    "MAX_STREAMS_PER_OP",
    "RealtimeProgressTracker",
    "StreamTick",
    "get_default_tracker",
    "reset_default_tracker",
]
