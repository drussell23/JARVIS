# -*- coding: utf-8 -*-
"""Detached-surgery daemon + idempotent poll/reconnect + byte-offset log-tailing
tests for scripts/sovereign_iac_hypervisor.py.

The deterministic run-#15 bug: the surgery rode ONE long-lived streaming SSH
session; during the heavy pip install the IAP tunnel dropped (Broken pipe /
Connection closed / rc=255) and the harness fell over -- even though the NODE was
fine. The fix decouples the surgery from the SSH session:

  1. the launching SSH writes surgery.sh + spawns it DETACHED (setsid/nohup/
     systemd-run) and RETURNS IMMEDIATELY;
  2. the local harness POLLS the node (exp-backoff + jitter, SHORT disposable SSH
     probes) reading soak_state.json + soak_in_progress.lock;
  3. a stateful byte-offset tailer fetches ONLY new bytes of surgery.out via
     `tail -c +offset` (zero loss / zero dup across a drop).

A broken-pipe / 255 probe is SWALLOWED + retried -- NEVER fatal. NO real GCP/SSH:
the script's single `_run` boundary is monkeypatched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sovereign_iac_hypervisor_detached", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def iac():
    return _load_module()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Tiny backoff so the poll-loop tests run fast (still exercises exp-backoff).
    monkeypatch.setenv("JARVIS_IAC_POLL_BASE_S", "0")
    monkeypatch.setenv("JARVIS_IAC_POLL_CAP_S", "0")
    monkeypatch.setenv("JARVIS_IAC_POLL_JITTER_S", "0")
    return tmp_path


@pytest.fixture()
def args(iac):
    a = iac.build_parser().parse_args([])
    # Make probes/backoff deterministic + tiny for tests (no real sleeping).
    a.poll_base_s = 0.0
    a.poll_cap_s = 0.0
    a.poll_jitter_s = 0.0
    a.probe_timeout_s = 5.0
    a.liveness_deadline_s = 100.0
    a.max_wall_seconds = 0.0
    a.detached_surgery = True
    return a


# --------------------------------------------------------------------------- #
# 1. The launch command DETACHES (setsid/nohup/systemd-run) + returns immediately.
# --------------------------------------------------------------------------- #
def test_launch_shell_detaches_with_setsid_nohup_or_systemd_run(iac, args):
    shell = iac._remote_surgery_launch_shell(args)
    # detaches via at least one of the supported mechanisms.
    assert "setsid" in shell and "nohup" in shell
    assert "systemd-run" in shell  # preferred, with setsid/nohup fallback
    # backgrounded (&) + returns immediately (exit 0, never waits on the body).
    assert "&" in shell
    assert shell.rstrip().endswith("exit 0")
    # writes the surgery.sh script (base64-decoded) + chmods it.
    assert "base64 -d" in shell
    assert "surgery.sh" in shell


def test_surgery_body_writes_soak_state_and_lock(iac, args):
    body = iac._remote_surgery_body_script(args)
    # structured JSON state at phase boundaries + atomic temp+mv.
    assert "_write_state deps running" in body
    assert "_write_state done done" in body or "_write_state done" in body
    assert "_write_state failed failed" in body or "_write_state failed" in body
    assert '"phase":' in body and '"status":' in body and '"verdict":' in body
    assert "mv -f" in body  # atomic write
    # lock holds the PID, removed in a trap on ANY exit.
    assert iac._DEFAULT_SOAK_LOCK_PATH in body
    assert "trap _cleanup EXIT" in body
    assert 'echo "$$"' in body  # PID into the lock
    # preserves the synchronous tail.
    assert "SYNCHRONOUS TAIL" in body


def test_launch_uses_non_streaming_boundary_and_returns(iac, args, monkeypatch):
    """The launch goes through the SHORT non-streaming `_run` boundary (NOT a long
    stream) and returns without waiting for the surgery body."""
    calls = []

    def _fake_run(cmd, *, timeout_s=120.0):
        calls.append((cmd, timeout_s))
        return 0, "[iac] surgery launched detached (setsid nohup)\n"

    monkeypatch.setattr(iac, "_run", _fake_run)
    rc, cap = iac.launch_detached_surgery(args, "node-x")
    assert rc == 0
    assert len(calls) == 1  # ONE short SSH, returns immediately
    # the probe timeout (short) bounds the launch, NOT the long surgery timeout.
    assert calls[0][1] == args.probe_timeout_s


# --------------------------------------------------------------------------- #
# 2. The poll loop reads soak_state.json + terminates on status done/failed.
# --------------------------------------------------------------------------- #
class _Probe:
    """Stateful fake `_run`: dispatches by command shape (state read / lock probe /
    tail) and replays a scripted timeline so a single poll loop can be driven to
    terminal. Each entry is (rc, out)."""

    def __init__(self, *, state_seq, lock_seq=None, tail_seq=None):
        self.state_seq = list(state_seq)
        self.lock_seq = list(lock_seq or [])
        self.tail_seq = list(tail_seq or [])
        self.calls = []

    def _pop(self, seq, default):
        return seq.pop(0) if seq else default

    def __call__(self, cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        self.calls.append(blob)
        if "tail -c +" in blob:
            return self._pop(self.tail_seq, (0, ""))
        if "soak_in_progress.lock" in blob or "kill -0" in blob:
            return self._pop(self.lock_seq, (0, "ALIVE 1234"))
        if "soak_state.json" in blob or "cat " in blob:
            return self._pop(self.state_seq, (0, ""))
        # the launch shell.
        return 0, "launched\n"


def _run_loop(iac, args, node, probe, monkeypatch):
    monkeypatch.setattr(iac, "_run", probe)
    return iac.run_remote_surgery_detached(args, node)


def test_poll_terminates_on_state_done_with_verdict(iac, args, monkeypatch):
    probe = _Probe(
        state_seq=[
            (0, '{"phase":"deps","status":"running","rc":null,"ts":1,"verdict":"running"}'),
            (0, '{"phase":"done","status":"done","rc":0,"ts":2,"verdict":"PASS"}'),
        ],
        lock_seq=[(0, "ALIVE 1234")],
    )
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    assert rc == 0
    assert verdict == "PASS"
    assert any("terminal state" in "".join(captured) for _ in [0])


def test_poll_terminates_on_state_failed(iac, args, monkeypatch):
    probe = _Probe(
        state_seq=[
            (0, '{"phase":"failed","status":"failed","rc":7,"ts":3,"verdict":"UNKNOWN"}'),
        ],
        lock_seq=[(0, "ALIVE 1234")],
    )
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    assert rc == 7
    assert any("status=failed" in line for line in captured)


# --------------------------------------------------------------------------- #
# 3. THE CORE REGRESSION: a BrokenPipe/255 probe is SWALLOWED + the loop continues.
#    (simulate the run-#15 drop mid-poll -> no crash + eventual terminal read.)
# --------------------------------------------------------------------------- #
def test_broken_pipe_probe_is_swallowed_loop_continues(iac, args, monkeypatch):
    """A probe that fails with a transport drop (broken pipe / 255) is swallowed;
    the loop keeps polling and eventually reads the terminal state. NO crash."""
    probe = _Probe(
        state_seq=[
            # tick 1: the run-#15 drop during pip install -- rc=255 broken pipe.
            (255, "Connection closed by remote host\nBroken pipe\nrc=255"),
            # tick 2: still installing, transport recovered, surgery running.
            (0, '{"phase":"deps","status":"running","rc":null,"ts":1,"verdict":"running"}'),
            # tick 3: terminal done.
            (0, '{"phase":"done","status":"done","rc":0,"ts":2,"verdict":"PASS"}'),
        ],
        lock_seq=[(255, "Broken pipe"), (0, "ALIVE 1234"), (0, "ALIVE 1234")],
    )
    # must NOT raise -- the whole point of decoupling.
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    assert rc == 0
    assert verdict == "PASS"
    blob = "".join(captured)
    assert "swallowed" in blob  # the drop was explicitly swallowed + retried
    assert "terminal state" in blob


def test_is_transport_drop_classifier(iac):
    assert iac._is_transport_drop(255, "anything")
    assert iac._is_transport_drop(1, "Broken pipe")
    assert iac._is_transport_drop(1, "Connection closed by remote host")
    assert iac._is_transport_drop(1, "timed out")
    assert not iac._is_transport_drop(0, "all good")
    assert not iac._is_transport_drop(1, "VERDICT: PASS")


# --------------------------------------------------------------------------- #
# 4. The byte-offset tailer advances correctly across a simulated drop.
#    feed bytes 0-100, drop, then 100-200 -> the local sink sees 0-200 exactly
#    once (no dup, no loss).
# --------------------------------------------------------------------------- #
def test_byte_offset_tailer_zero_loss_zero_dup_across_drop(iac, args, monkeypatch):
    import asyncio

    seen = []
    sink = seen.append

    # The node's surgery.out is a fixed 200-byte stream.
    full = ("A" * 100) + ("B" * 100)  # bytes 0..99 then 100..199

    def _fake_run(cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        # parse `tail -c +N` -> start byte (1-indexed); return bytes from N-1.
        marker = "tail -c +"
        idx = blob.find(marker)
        start = int(blob[idx + len(marker):].split()[0])
        off0 = start - 1  # 0-indexed offset
        return 0, full[off0:]

    monkeypatch.setattr(iac, "_run", _fake_run)

    async def _drive():
        offset = 0
        # tick 1: fetch 0..(end) but pretend only 100 bytes were available so far.
        # We model the drop by capping the FIRST fetch to 100 bytes via a wrapper.
        nonlocal_seen = []

        # First fetch returns ONLY the first 100 bytes (the rest not written yet).
        def _first(cmd, *, timeout_s=120.0):
            return 0, full[0:100]

        monkeypatch.setattr(iac, "_run", _first)
        offset = await iac._tail_once(args, "n", offset, sink)
        assert offset == 100

        # DROP: a transport failure -> offset UNCHANGED, nothing streamed.
        def _drop(cmd, *, timeout_s=120.0):
            return 255, "Broken pipe"

        monkeypatch.setattr(iac, "_run", _drop)
        offset2 = await iac._tail_once(args, "n", offset, sink)
        assert offset2 == 100  # unchanged on a failed tail

        # RECONNECT: resume at offset=100, fetch 100..199 (only the NEW bytes).
        monkeypatch.setattr(iac, "_run", _fake_run)
        offset3 = await iac._tail_once(args, "n", offset2, sink)
        assert offset3 == 200

    asyncio.run(_drive())

    streamed = "".join(seen)
    # every byte seen EXACTLY once, in order: 100 A's then 100 B's, no dup/loss.
    assert streamed.count("A") == 100
    assert streamed.count("B") == 100
    assert streamed.replace("\n", "") == full


def test_tail_cmd_uses_offset_plus_one(iac, args):
    cmd = iac._tail_cmd(args, "n", 100)
    blob = " ".join(cmd)
    assert "tail -c +101" in blob  # 1-indexed: bytes AFTER offset 100


# --------------------------------------------------------------------------- #
# 5. Exponential backoff grows + caps.
# --------------------------------------------------------------------------- #
def test_backoff_grows_and_caps(iac):
    # base=2 cap=10 jitter=0 -> 2,4,8,10,10 (capped).
    seq = [iac._backoff_delay(i, base=2.0, cap=10.0, jitter=0.0) for i in range(5)]
    assert seq == [2.0, 4.0, 8.0, 10.0, 10.0]
    # monotonic non-decreasing up to the cap.
    assert all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
    # jitter stays within bound.
    j = iac._backoff_delay(0, base=1.0, cap=10.0, jitter=3.0)
    assert 1.0 <= j <= 4.0


# --------------------------------------------------------------------------- #
# 6. Absolute-timeout + liveness-deadline terminate (+ reap mapping).
# --------------------------------------------------------------------------- #
def test_absolute_wall_ceiling_terminates(iac, args, monkeypatch):
    args.max_wall_seconds = 0.0001  # immediately exceeded
    args.surgery_timeout_s = 999999
    probe = _Probe(
        # never reaches a terminal state -> the wall ceiling must stop it.
        state_seq=[(0, '{"phase":"deps","status":"running","rc":null,"ts":1,"verdict":"running"}')] * 50,
        lock_seq=[(0, "ALIVE 1")] * 50,
    )
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    assert rc == 124  # wall-ceiling reap code
    assert any("wall ceiling" in line for line in captured)


def test_liveness_deadline_terminates_on_persistent_drops(iac, args, monkeypatch):
    args.liveness_deadline_s = -1.0  # any failed probe immediately exceeds it
    args.max_wall_seconds = 999999
    probe = _Probe(
        state_seq=[(255, "Broken pipe")] * 50,
        lock_seq=[(255, "Broken pipe")] * 50,
        tail_seq=[(255, "Broken pipe")] * 50,
    )
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    assert rc == 125  # liveness-deadline reap code
    assert any("liveness deadline" in line for line in captured)


def test_lock_gone_with_terminal_state_terminates(iac, args, monkeypatch):
    probe = _Probe(
        state_seq=[
            (0, '{"phase":"done","status":"done","rc":0,"ts":2,"verdict":"PASS"}'),
        ],
        # status==done is read first -> terminal before lock even checked; covered
        # by test_poll_terminates_on_state_done. Here assert the GONE path: state
        # not yet flushed to done but lock gone with a non-running phase.
    )
    # Override: state shows 'audit' (non-running, non-terminal) but lock GONE.
    probe.state_seq = [
        (0, '{"phase":"audit","status":"audit","rc":0,"ts":2,"verdict":"PASS"}'),
    ]
    probe.lock_seq = [(0, "GONE")]
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    assert verdict == "PASS"
    assert any("lock GONE" in line for line in captured)


# --------------------------------------------------------------------------- #
# 7. OFF flag -> legacy single-stream path (byte-identical), no poll loop.
# --------------------------------------------------------------------------- #
def test_off_flag_uses_legacy_stream_path(iac, args, monkeypatch):
    args.detached_surgery = False
    streamed = {"called": False}

    def _fake_stream(cmd, *, label="", log_path=None, timeout_s=3600.0):
        streamed["called"] = True
        assert label == "surgery"
        return 0, ["VERDICT: PASS\n"]

    def _no_run(cmd, *, timeout_s=120.0):
        raise AssertionError("legacy path must NOT call _run (no poll loop)")

    monkeypatch.setattr(iac, "_run_streaming_labeled", _fake_stream)
    monkeypatch.setattr(iac, "_run", _no_run)
    rc, captured, verdict = iac.run_remote_surgery(args, "n")
    assert rc == 0
    assert verdict == "PASS"
    assert streamed["called"]  # rode the legacy streaming boundary


def test_off_flag_legacy_shell_byte_identical_markers(iac, args):
    """The OFF legacy shell preserves the exact surgery body + tail + sentinel."""
    args.detached_surgery = False
    shell = iac._remote_surgery_shell(args)
    assert "tee /tmp/surgery.out" in shell
    assert "SYNCHRONOUS TAIL" in shell
    assert "exit $rc" in shell
    # legacy path does NOT detach.
    assert "setsid" not in shell
    assert "soak_state.json" not in shell


def test_detached_default_on(iac):
    # default-ON when neither arg nor env override.
    a = iac.build_parser().parse_args([])
    assert a.detached_surgery is True
