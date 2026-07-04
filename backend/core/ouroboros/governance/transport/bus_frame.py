from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from backend.core.ouroboros.governance.ide_observability_stream import StreamEvent

FRAME_HELLO = "hello"
FRAME_EVENT = "event"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ACK = "ack"

_VALID_KINDS = (FRAME_HELLO, FRAME_EVENT, FRAME_HEARTBEAT, FRAME_ACK)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def qualified_id(source_id: str, event_id: str) -> str:
    """Cross-host dedup key: source-scoped so two brokers' native
    monotonic ids never collide."""
    return f"{source_id}:{event_id}"


@dataclass(frozen=True)
class BusFrame:
    kind: str
    source_id: str
    seq: int = -1
    last_event_id: Optional[str] = None
    event: Optional[Dict[str, Any]] = None
    ts: str = ""

    def encode(self) -> bytes:
        return json.dumps({
            "kind": self.kind,
            "source_id": self.source_id,
            "seq": self.seq,
            "last_event_id": self.last_event_id,
            "event": self.event,
            "ts": self.ts or _iso_now(),
        }, ensure_ascii=True).encode("utf-8")

    @classmethod
    def decode(cls, raw: Union[str, bytes]) -> Optional["BusFrame"]:
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        kind = obj.get("kind")
        if not isinstance(kind, str) or kind not in _VALID_KINDS:
            return None
        source_id = obj.get("source_id")
        if not isinstance(source_id, str):
            return None
        event = obj.get("event")
        if event is not None and not isinstance(event, dict):
            return None
        seq = obj.get("seq", -1)
        if not isinstance(seq, int):
            seq = -1
        return cls(
            kind=kind,
            source_id=source_id,
            seq=seq,
            last_event_id=obj.get("last_event_id"),
            event=event,
            ts=obj.get("ts", "") if isinstance(obj.get("ts"), str) else "",
        )


def event_frame(ev: StreamEvent, source_id: str) -> BusFrame:
    try:
        seq = int(ev.event_id, 16)
    except (ValueError, TypeError):
        seq = -1
    return BusFrame(
        kind=FRAME_EVENT,
        source_id=source_id,
        seq=seq,
        event=ev.to_dict(),
        ts=_iso_now(),
    )


def hello_frame(source_id: str, last_event_id: Optional[str]) -> BusFrame:
    return BusFrame(kind=FRAME_HELLO, source_id=source_id, last_event_id=last_event_id, ts=_iso_now())


def heartbeat_frame(source_id: str) -> BusFrame:
    return BusFrame(kind=FRAME_HEARTBEAT, source_id=source_id, ts=_iso_now())


def ack_frame(source_id: str, last_event_id: str) -> BusFrame:
    return BusFrame(kind=FRAME_ACK, source_id=source_id, last_event_id=last_event_id, ts=_iso_now())
