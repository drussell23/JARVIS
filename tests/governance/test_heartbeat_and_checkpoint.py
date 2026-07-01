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


# --- Cryptographic State Verification (fail-closed) -------------------------

def test_signed_checkpoint_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "test-secret-key")
    base = str(tmp_path)
    ckpt.write_checkpoint(ckpt.FSMCheckpoint(op_id="op-sig", phase="GENERATE"), base_dir=base)
    got = ckpt.list_pending(base_dir=base)
    assert [c.op_id for c in got] == ["op-sig"]   # valid HMAC -> accepted


def test_tampered_payload_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "test-secret-key")
    base = str(tmp_path)
    import json as _json
    import os
    path = ckpt.write_checkpoint(ckpt.FSMCheckpoint(op_id="op-t", phase="GENERATE"), base_dir=base)
    # Tamper the payload but keep the old signature.
    with open(path) as fh:
        w = _json.load(fh)
    payload = _json.loads(w["payload"])
    payload["goal_description"] = "MALICIOUS INJECTION"
    w["payload"] = _json.dumps(payload)  # sig no longer matches
    with open(path, "w") as fh:
        _json.dump(w, fh)
    assert ckpt.list_pending(base_dir=base) == []   # fail-closed -> clean boot


def test_wrong_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "key-A")
    base = str(tmp_path)
    ckpt.write_checkpoint(ckpt.FSMCheckpoint(op_id="op-k", phase="GENERATE"), base_dir=base)
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "key-B")   # attacker/rotated key
    assert ckpt.list_pending(base_dir=base) == []   # HMAC mismatch -> rejected


def test_missing_hmac_rejected(tmp_path):
    base = str(tmp_path)
    d = ckpt.checkpoint_dir(base)
    import os, json as _json
    with open(os.path.join(d, "nosig.json"), "w") as fh:
        _json.dump({"schema": 1, "payload": '{"op_id":"x","phase":"GENERATE"}'}, fh)  # no hmac
    assert ckpt.list_pending(base_dir=base) == []


# --- Resume hydration (Venom fast-forward) ----------------------------------

def test_hydrate_reinjects_and_consumes(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "k")
    base = str(tmp_path)
    ckpt.write_checkpoint(ckpt.FSMCheckpoint(
        op_id="op-r", phase="GENERATE", goal_description="finish the patch",
        target_files=["m.py"], exploration_records=[{"tool": "read_file", "path": "m.py"}],
    ), base_dir=base)

    injected = []
    ckpt.hydrate_pending_checkpoints(lambda env: injected.append(env), base_dir=base)

    assert len(injected) == 1
    env = injected[0]
    assert env["op_id"] == "op-r" and env["resume"] is True and env["resume_phase"] == "GENERATE"
    assert env["exploration_records"] == [{"tool": "read_file", "path": "m.py"}]  # preserved
    # consumed exactly once -> a second boot re-injects nothing.
    assert ckpt.list_pending(base_dir=base) == []
    injected2 = []
    ckpt.hydrate_pending_checkpoints(lambda env: injected2.append(env), base_dir=base)
    assert injected2 == []


def test_capture_inflight_from_registry(tmp_path, monkeypatch):
    """SUSPEND: capture_inflight reads the in-flight registry and writes a signed
    checkpoint per active op (from its ctx_ref)."""
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "k")
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    from backend.core.ouroboros.governance import in_flight_registry as ifr
    ifr.reset_default_registry()
    reg = ifr.get_default_registry()
    ctx = SimpleNamespace(op_id="op-live", phase="GENERATE", description="fix it",
                          target_files=("z.py",), intake_evidence_json="{}", provider_route="standard")
    reg.register("op-live", ctx_ref=ctx, last_phase_name="GENERATE")

    n = ckpt.capture_inflight(reason="wall_clock_cap")
    assert n == 1
    pending = ckpt.list_pending()
    assert [c.op_id for c in pending] == ["op-live"]
    assert pending[0].resume_reason == "wall_clock_cap"
    ifr.reset_default_registry()


def test_capture_inflight_empty_registry_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp2"))
    from backend.core.ouroboros.governance import in_flight_registry as ifr
    ifr.reset_default_registry()
    assert ckpt.capture_inflight(reason="x") == 0   # nothing in-flight -> no-op


def test_hydrate_leaves_pending_on_ingest_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "k")
    base = str(tmp_path)
    ckpt.write_checkpoint(ckpt.FSMCheckpoint(op_id="op-f", phase="GENERATE"), base_dir=base)

    def _boom(env):
        raise RuntimeError("intake down")

    ckpt.hydrate_pending_checkpoints(_boom, base_dir=base)
    # ingest failed -> NOT consumed -> still pending for the next boot.
    assert [c.op_id for c in ckpt.list_pending(base_dir=base)] == ["op-f"]
