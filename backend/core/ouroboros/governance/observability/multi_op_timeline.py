"""Phase 8.3 — Synchronized multi-op timeline aggregator.

Per `OUROBOROS_VENOM_PRD.md` §3.6.4:

  > Extend SerpentFlow with `--multi-op` mode interleaving N op
  > streams by timestamp.

This module ships the timeline AGGREGATOR primitive — a
deterministic merge-sort over per-op event streams that produces
a unified timeline. SerpentFlow's `--multi-op` mode is the
operator-facing surface (deferred — out of scope for this PR);
the aggregator + tests are the substrate.

## Why deterministic merge

Operators investigating "what happened across these 3 parallel L3
fan-out units?" need a single chronological view. Concatenating
per-op debug.log files and `sort -k1` works for naïve cases but
breaks when timestamps tie or when streams are sourced from
different ledgers (decision-trace + intake + heartbeat).

This aggregator:
  * Accepts N streams of typed events (each must have ts_epoch)
  * Merges in O(N log K) where K = stream count via heapq
  * Tie-breaks by stream_id (alpha) for determinism
  * Bounds output count at MAX_TIMELINE_EVENTS (defends against
    callers feeding millions of events)

## Default-off

`JARVIS_MULTI_OP_TIMELINE_ENABLED` (default false). Read-side
operations always work even with master off (the master flag
gates production of these timelines, not their consumption).
"""
from __future__ import annotations

import heapq
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard cap on output timeline length — defends against callers
# feeding multi-million-event streams.
MAX_TIMELINE_EVENTS: int = 50_000

# Per-event payload truncation cap (defends against giant evidence
# blobs bloating the merged timeline).
MAX_EVENT_PAYLOAD_CHARS: int = 1_000


def is_timeline_enabled() -> bool:
    """Master flag — ``JARVIS_MULTI_OP_TIMELINE_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_MULTI_OP_TIMELINE_ENABLED", "",
    ).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TimelineEvent:
    """One event in a merged timeline. Frozen — deterministic
    ordering by (ts_epoch, stream_id, seq).
    """

    ts_epoch: float
    stream_id: str
    event_type: str
    payload: Dict[str, Any]
    seq: int = 0  # stable tie-break for events with identical ts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts_epoch": self.ts_epoch,
            "stream_id": self.stream_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "seq": self.seq,
        }


def _truncate_payload(
    p: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Truncate string-valued fields in a payload dict to defend
    against bloat. Non-string fields pass through unchanged."""
    if not isinstance(p, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in p.items():
        if isinstance(v, str) and len(v) > MAX_EVENT_PAYLOAD_CHARS:
            out[k] = v[: MAX_EVENT_PAYLOAD_CHARS - 14] + "...(truncated)"
        else:
            out[k] = v
    return out


def merge_streams(
    streams: Dict[str, Sequence[TimelineEvent]],
    *,
    max_events: Optional[int] = None,
) -> List[TimelineEvent]:
    """Merge N labeled event streams into one chronological timeline.

    Args:
        streams: ``{stream_id: [TimelineEvent, ...]}``. Each stream
            MUST be pre-sorted by ts_epoch (this aggregator does not
            sort within streams — only across).
        max_events: cap on output (defaults to MAX_TIMELINE_EVENTS).

    Returns: list of TimelineEvent in (ts_epoch ASC, stream_id ASC,
    seq ASC) order.

    Determinism: tie-break by stream_id (alpha) then seq (ASC) so
    operators see a stable view across replays.

    Bounded: at most ``max_events`` events returned (defaults to
    MAX_TIMELINE_EVENTS = 50,000).
    """
    if not streams:
        return []
    cap = max_events if max_events is not None else MAX_TIMELINE_EVENTS
    # Min-heap over (ts_epoch, stream_id, seq, stream_iter_index)
    heap: List[Tuple[float, str, int, int, int]] = []
    iters: List[Tuple[str, List[TimelineEvent]]] = [
        (sid, list(events)) for sid, events in streams.items()
    ]
    # Stable iter_index by alpha-sorting stream_ids.
    iters.sort(key=lambda x: x[0])
    for iter_idx, (sid, events) in enumerate(iters):
        if events:
            ev = events[0]
            heapq.heappush(
                heap, (ev.ts_epoch, ev.stream_id, ev.seq, iter_idx, 0),
            )
    out: List[TimelineEvent] = []
    while heap and len(out) < cap:
        ts, sid, seq, iter_idx, pos = heapq.heappop(heap)
        events = iters[iter_idx][1]
        out.append(events[pos])
        next_pos = pos + 1
        if next_pos < len(events):
            ev = events[next_pos]
            heapq.heappush(
                heap,
                (ev.ts_epoch, ev.stream_id, ev.seq, iter_idx, next_pos),
            )
    return out


def render_text_timeline(
    timeline: Sequence[TimelineEvent],
    *,
    max_lines: int = 200,
) -> str:
    """Render the merged timeline as plain text for SerpentFlow's
    `--multi-op` view. One line per event:

        <ts> [stream_id] event_type :: payload_summary
    """
    parts: List[str] = []
    for i, ev in enumerate(timeline):
        if i >= max_lines:
            parts.append(
                f"... (timeline truncated at {max_lines} lines; "
                f"total={len(timeline)})"
            )
            break
        # ISO-format the ts for readability.
        from datetime import datetime, timezone
        ts_iso = datetime.fromtimestamp(
            ev.ts_epoch, tz=timezone.utc,
        ).strftime("%H:%M:%S.%f")[:-3]
        # Truncate payload summary.
        payload_summary = ", ".join(
            f"{k}={v}" for k, v in list(ev.payload.items())[:3]
        )
        if len(payload_summary) > 80:
            payload_summary = payload_summary[:77] + "..."
        parts.append(
            f"{ts_iso} [{ev.stream_id:<12}] {ev.event_type:<24} "
            f":: {payload_summary}"
        )
    return "\n".join(parts)


__all__ = [
    "MAX_EVENT_PAYLOAD_CHARS",
    "MAX_TIMELINE_EVENTS",
    "TimelineEvent",
    "is_timeline_enabled",
    "merge_streams",
    "render_text_timeline",
]
