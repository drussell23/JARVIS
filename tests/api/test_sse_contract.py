"""JARVISKit SSE serialization contract (Phase 10) — byte-exact tests."""
from __future__ import annotations

import json

from backend.api import sse_contract as sc


def test_render_frame_has_no_space_after_event_prefix():
    """The Swift parser does dropFirst(6) after 'event:' WITHOUT trimming,
    so the type line MUST be 'event:daemon' (no space) or the switch drops
    it."""
    frame = sc.render_jarviskit_frame(7, "daemon", {"command_id": "x"})
    assert "event:daemon\n" in frame            # NO space
    assert "event: daemon" not in frame         # the wrong (spec) form
    assert frame.endswith("\n\n")               # SSE block terminator
    assert frame.startswith("id:7\n")


def test_render_frame_data_is_flat_json():
    frame = sc.render_jarviskit_frame(1, "daemon",
                                      {"command_id": "c", "narration_text": "hi"})
    data_line = [l for l in frame.split("\n") if l.startswith("data:")][0]
    payload = json.loads(data_line[len("data:"):])
    assert payload["command_id"] == "c"


def test_daemon_payload_has_all_four_required_keys():
    """Swift DaemonEvent: command_id/narration_text/narration_priority/
    source_brain are all non-optional — all four MUST be present."""
    p = sc.daemon_payload(command_id="op-1", narration_text="did a thing")
    for k in ("command_id", "narration_text", "narration_priority",
              "source_brain"):
        assert k in p and isinstance(p[k], str)


def test_eventstream_envelope_unwrapped_to_daemon_frame():
    """A local EventStream frame {seq,ch,ts,d:{type:ov_activity,...}} must
    become a flat JARVISKit daemon frame decodable as DaemonEvent."""
    envelope = {"v": 1, "seq": 42, "ch": "governance", "ts": 1.0,
                "d": {"type": "ov_activity", "op_id": "op-9",
                      "event": "op_completed", "detail": {"success": True}}}
    raw = f"id: 42\ndata: {json.dumps(envelope)}\n\n"
    out = sc.eventstream_frame_to_jarviskit(raw)
    assert out is not None
    assert "event:daemon\n" in out              # mapped ov_activity → daemon
    assert out.startswith("id:42\n")
    # The data decodes as the Swift DaemonEvent contract (all 4 keys).
    data_line = [l for l in out.split("\n") if l.startswith("data:")][0]
    payload = json.loads(data_line[len("data:"):])
    for k in ("command_id", "narration_text", "narration_priority",
              "source_brain"):
        assert k in payload
    assert payload["command_id"] == "op-9"      # from op_id


def test_keepalive_and_untyped_frames_pass_through_as_none():
    assert sc.eventstream_frame_to_jarviskit(": keepalive\n\n") is None
    # A frame whose inner payload has no 'type' isn't a governance frame.
    env = {"seq": 1, "d": {"foo": "bar"}}
    assert sc.eventstream_frame_to_jarviskit(f"id: 1\ndata: {json.dumps(env)}\n\n") is None


def test_never_raises_on_garbage():
    assert sc.eventstream_frame_to_jarviskit("not a frame") is None
    assert sc.eventstream_frame_to_jarviskit("") is None
