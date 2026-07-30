"""What the organism is writing RIGHT NOW, readable in the producing process.

The gap this closes
-------------------
`capability_handoff` measured `stream_rows` UNSET on the daemon cockpit, and
`cockpit_mount` recorded why in its own comment rather than papering over it:

    there is NO process-global in-flight text to read.
    `live_tool_stream.make_tool_observer` creates a stream per tool CALL and
    nothing publishes a current frame, so a provider here would have to
    invent one.

So the daemon composed every in-flight frame, shipped it across the bridge,
and could not draw it at its own terminal. Attached clients saw the sentence
being written; the process writing it did not.

Why not tap the publish seam
----------------------------
The obvious single chokepoint is `publish_telemetry_global`, which every
frame already crosses. It is the wrong one, and specifically wrong for this
consumer: it returns early when `attached_cockpits() <= 0`. A daemon with
nobody attached publishes nothing — which is exactly the case where the
daemon's OWN cockpit is the surface being looked at. Tapping there would
have produced a strip that works only while a second cockpit is watching.

So the registry is fed where frames are COMPOSED, before any transport
decision: `stream_renderer` for model prose, `live_tool_stream` for command
tails. Two sites, because there are genuinely two producers — but one sink,
and both hand it the payload they were already building, so the sink cannot
drift from what crosses the wire.

Why a map rather than a single slot
-----------------------------------
"The" in-flight text is not a single thing. L3 dispatches subagents in
parallel and the Venom loop can have several tools open at once, so at any
instant there may be four sentences being written. A single slot would
flicker between them at whatever rate they happen to emit — the surface
would be busy and unreadable, and worse, it would look like one stream
behaving erratically rather than four behaving normally.

The map is keyed by stream identity and the reader takes the most recently
updated LIVE entry: "what is happening right now" answered literally. Bounded
and TTL'd, because a producer that dies mid-command never sends `done`, and a
strip that hangs on a sentence nobody is writing is the specific lie every
other heartbeat-fed surface in this cockpit is careful not to tell.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.InflightRegistry")

INFLIGHT_REGISTRY_SCHEMA_VERSION: str = "inflight_registry.1"

__all__ = [
    "INFLIGHT_REGISTRY_SCHEMA_VERSION",
    "InflightEntry",
    "current_inflight",
    "inflight_registry_enabled",
    "inflight_ttl_s",
    "live_inflight",
    "note_inflight_frame",
    "reset_inflight_for_tests",
]

#: Slots kept before the oldest is evicted. A ceiling, not a target: the
#: reader draws ONE, and this only has to be wide enough that a burst of
#: parallel subagents cannot push the live entry out before it is read.
_MAX_SLOTS = 32


def inflight_registry_enabled() -> bool:
    """``JARVIS_INFLIGHT_REGISTRY_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_INFLIGHT_REGISTRY_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def inflight_ttl_s() -> float:
    """How long a frame stays current without an update. NEVER raises.

    Not a display preference — a correctness bound. A producer killed
    mid-command never sends ``done``, and without this the strip would show
    that command's last output for the rest of the session as though it were
    still running.

    Clamped rather than trusted: too small and a genuinely slow command
    (a 90-second test run emitting nothing) flickers out between frames; too
    large and a dead one lingers. The default is generous relative to
    `live_tool_stream`'s own emit interval so a quiet-but-alive command
    survives, and short enough that a dead one clears within a breath.
    """
    try:
        return max(1.0, min(120.0, float(
            os.environ.get("JARVIS_INFLIGHT_TTL_S", "") or 8.0,
        )))
    except (TypeError, ValueError):
        return 8.0


class InflightEntry:
    """One producer's current sentence, and what shape it is.

    ``is_tool`` is carried rather than re-derived because the two producers
    need different wrapping — command output keeps its line structure, model
    prose does not — and the frame's own ``kind`` is the only place that
    distinction is known for certain.
    """

    __slots__ = ("key", "text", "is_tool", "at", "op_id")

    def __init__(self, key: str, text: str, is_tool: bool, at: float,
                 op_id: str = "") -> None:
        self.key = key
        self.text = text
        self.is_tool = is_tool
        self.at = at
        self.op_id = op_id

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.monotonic()) - self.at)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return (f"InflightEntry(key={self.key!r}, is_tool={self.is_tool}, "
                f"chars={len(self.text)})")


_LOCK = threading.Lock()
_SLOTS: "Dict[str, InflightEntry]" = {}


def _key_for(payload: Any) -> str:
    """Stream identity: the op AND the tool.

    Not the op alone. The Venom loop can hold several tools open inside one
    operation, and keying on `op_id` would make them overwrite each other —
    which reads as a single stream flickering rather than as the several that
    are actually running.
    """
    try:
        op = str(payload.get("op_id") or "")
        tool = str(payload.get("tool") or payload.get("kind") or "")
        label = str(payload.get("label") or "")
        return f"{op}::{tool}::{label}"
    except Exception:  # noqa: BLE001
        return "::"


def note_inflight_frame(payload: Any) -> bool:
    """Record one composed frame. NEVER raises; True when it was kept.

    Takes the payload the producer was ALREADY building rather than a
    hand-assembled copy, which is what stops this from becoming a second
    opinion about what is in flight.

    ``done`` retires the slot instead of storing an empty one: by then the
    text has landed in the deck, and an empty slot that is merely "not live"
    still has to be reasoned about by every reader.
    """
    if not inflight_registry_enabled():
        return False
    try:
        if not isinstance(payload, dict):
            return False
        kind = str(payload.get("kind") or "")
        if kind not in ("stream_inflight", "tool_stream"):
            return False
        key = _key_for(payload)
        now = time.monotonic()
        with _LOCK:
            if payload.get("done"):
                _SLOTS.pop(key, None)
                return True
            text = compose_inflight_text(payload)
            if not text:
                _SLOTS.pop(key, None)
                return False
            _SLOTS[key] = InflightEntry(
                key=key, text=text, is_tool=(kind == "tool_stream"),
                at=now, op_id=str(payload.get("op_id") or ""),
            )
            # Evict by AGE, not insertion order: the oldest slot is the one
            # least likely to still be alive, and a dict's insertion order
            # would evict a long-running command that simply started first.
            if len(_SLOTS) > _MAX_SLOTS:
                oldest = min(_SLOTS.values(), key=lambda e: e.at)
                _SLOTS.pop(oldest.key, None)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[Inflight] note degraded", exc_info=True)
        return False


def compose_inflight_text(payload: Any) -> str:
    """The frame's text, headered exactly as every surface headers it.

    ONE composition. The attach client built ``$ bash · 11s`` inline while
    handling the frame; a second copy here would be a second opinion about
    what an in-flight tool tail looks like, which is the precise defect the
    roster and the status line each already paid for once.

    The header is what makes a long command read as WORKING rather than
    stalled — which is the entire complaint a black box produces.
    """
    try:
        if not isinstance(payload, dict):
            return ""
        body = str(payload.get("text") or "")
        if str(payload.get("kind") or "") != "tool_stream":
            return body
        tool = str(payload.get("tool") or "tool")
        try:
            head = f"$ {tool} · {float(payload.get('elapsed_s') or 0.0):.0f}s"
        except (TypeError, ValueError):
            head = f"$ {tool}"
        return f"{head}\n{body}" if body else head
    except Exception:  # noqa: BLE001
        return ""


def live_inflight(now: Optional[float] = None) -> List[InflightEntry]:
    """Every entry still within its TTL, newest first. NEVER raises.

    Expiry is evaluated on READ rather than swept on a timer: there is no
    thread to own a sweep here, and a registry that needs one has invented a
    lifecycle for what is otherwise pure state. Stale entries are dropped as
    they are noticed, so the map cannot accumulate them either.
    """
    try:
        moment = now if now is not None else time.monotonic()
        ttl = inflight_ttl_s()
        with _LOCK:
            dead = [k for k, e in _SLOTS.items() if e.age_s(moment) > ttl]
            for k in dead:
                _SLOTS.pop(k, None)
            entries = list(_SLOTS.values())
        return sorted(entries, key=lambda e: e.at, reverse=True)
    except Exception:  # noqa: BLE001
        return []


def current_inflight(now: Optional[float] = None) -> Optional[InflightEntry]:
    """The one sentence to draw, or None. NEVER raises.

    Most-recently-updated wins. With four subagents writing at once that is
    the honest answer to "what is happening right now", and it is stable in
    practice because a producer mid-burst keeps refreshing its own slot.
    """
    entries = live_inflight(now)
    return entries[0] if entries else None


def reset_inflight_for_tests() -> None:
    with _LOCK:
        _SLOTS.clear()
