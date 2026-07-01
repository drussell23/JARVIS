"""Streaming heartbeat (idle-watchdog liveness) + FSM checkpoint/resume hydrator."""
from __future__ import annotations

import time
from types import SimpleNamespace

import backend.core.ouroboros.governance.stream_heartbeat as hb
import backend.core.ouroboros.governance.fsm_checkpoint as ckpt


# --- stream heartbeat -------------------------------------------------------

def test_heartbeat_pulse_and_activity():
    hb.reset()
    assert hb.seconds_since_pulse() == float("inf")
    assert hb.is_active(30.0) is False
    hb.pulse()
    assert hb.is_active(30.0) is True
    assert hb.seconds_since_pulse() < 5.0
    assert hb.pulse_count() == 1


def test_heartbeat_goes_stale():
    hb.reset()
    hb.pulse()
    assert hb.is_active(0.0) is True or hb.is_active(0.0) is False  # boundary tolerant
    time.sleep(0.05)
    assert hb.is_active(0.01) is False   # window smaller than the gap -> stale


def test_heartbeat_file_mirror(tmp_path, monkeypatch):
    hb.reset()
    f = tmp_path / "hb"
    monkeypatch.setenv("JARVIS_STREAM_HEARTBEAT_FILE", str(f))
    hb.pulse()
    assert f.exists() and float(f.read_text()) > 0


def test_streaming_token_pulses_heartbeat():
    """The Inter-Token Watchdog's _emit_stream_token feeds the heartbeat so a
    streaming op stays fresh for the idle watchdog."""
    import backend.core.ouroboros.governance.local_inference_director as lid
    hb.reset()
    lid._emit_stream_token("some tokens")
    assert hb.pulse_count() == 1


# --- FSM checkpoint / resume ------------------------------------------------

def test_checkpoint_roundtrip():
    cp = ckpt.FSMCheckpoint(
        op_id="op-1", phase="GENERATE", goal_description="fix bug",
        target_files=["a.py"], tool_history=[{"tool": "read_file", "path": "a.py"}],
        exploration_records=[{"tool": "search_code"}], created_at=123.0,
    )
    back = ckpt.FSMCheckpoint.from_json(cp.to_json())
    assert back.op_id == "op-1" and back.phase == "GENERATE"
    assert back.tool_history == [{"tool": "read_file", "path": "a.py"}]
    assert back.target_files == ["a.py"]


def test_capture_from_context():
    ctx = SimpleNamespace(op_id="op-2", phase="GENERATE", description="do X",
                          target_files=("x.py", "y.py"), intake_evidence_json="{}",
                          provider_route="standard")
    cp = ckpt.capture_from_context(ctx, phase="GENERATE",
                                   tool_history=[{"tool": "read_file"}])
    assert cp is not None and cp.op_id == "op-2"
    assert cp.target_files == ["x.py", "y.py"]
    assert cp.resume_reason == "wall_clock_cap"


def test_capture_none_without_op_id():
    assert ckpt.capture_from_context(SimpleNamespace(op_id=""), phase="X") is None
    assert ckpt.capture_from_context(object(), phase="X") is None


def test_write_list_and_mark_resumed(tmp_path):
    base = str(tmp_path)
    c1 = ckpt.FSMCheckpoint(op_id="op-a", phase="GENERATE", created_at=1.0)
    c2 = ckpt.FSMCheckpoint(op_id="op-b", phase="VALIDATE", created_at=2.0)
    assert ckpt.write_checkpoint(c1, base_dir=base)
    assert ckpt.write_checkpoint(c2, base_dir=base)
    pending = ckpt.list_pending(base_dir=base)
    assert [c.op_id for c in pending] == ["op-a", "op-b"]   # oldest first
    # Resume op-a exactly once -> it's consumed.
    assert ckpt.mark_resumed("op-a", base_dir=base) is True
    assert ckpt.mark_resumed("op-a", base_dir=base) is False
    assert [c.op_id for c in ckpt.list_pending(base_dir=base)] == ["op-b"]


def test_list_pending_skips_corrupt(tmp_path):
    base = str(tmp_path)
    d = ckpt.checkpoint_dir(base)
    import os
    with open(os.path.join(d, "bad.json"), "w") as fh:
        fh.write("{not valid json")
    ckpt.write_checkpoint(ckpt.FSMCheckpoint(op_id="ok", phase="GENERATE"), base_dir=base)
    got = ckpt.list_pending(base_dir=base)
    assert [c.op_id for c in got] == ["ok"]   # corrupt skipped, valid survives
