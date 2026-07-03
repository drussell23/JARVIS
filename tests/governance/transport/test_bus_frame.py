from __future__ import annotations

from backend.core.ouroboros.governance.ide_observability_stream import StreamEvent
from backend.core.ouroboros.governance.transport import bus_frame as bf


def _ev(seq: int) -> StreamEvent:
    return StreamEvent(
        event_id=format(seq, "012x"),
        event_type="task_started",
        op_id="op-1",
        timestamp="2026-07-03T00:00:00.000Z",
        payload={"k": "v"},
    )


def test_event_frame_roundtrip_preserves_all_fields():
    frame = bf.event_frame(_ev(42), source_id="brain-01")
    raw = frame.encode()
    back = bf.BusFrame.decode(raw)
    assert back is not None
    assert back.kind == bf.FRAME_EVENT
    assert back.source_id == "brain-01"
    assert back.seq == 42  # int(event_id, 16)
    assert back.event["event_id"] == format(42, "012x")
    assert back.event["payload"] == {"k": "v"}


def test_hello_frame_carries_last_event_id():
    frame = bf.hello_frame("mac-01", last_event_id="0000000000ff")
    back = bf.BusFrame.decode(frame.encode())
    assert back.kind == bf.FRAME_HELLO
    assert back.last_event_id == "0000000000ff"


def test_decode_malformed_returns_none_never_raises():
    assert bf.BusFrame.decode(b"not json") is None
    assert bf.BusFrame.decode("{}") is None  # missing required kind
    assert bf.BusFrame.decode(b'{"kind": 123}') is None  # wrong type


def test_qualified_id_is_source_scoped():
    assert bf.qualified_id("mac-01", "0000000000ff") == "mac-01:0000000000ff"
