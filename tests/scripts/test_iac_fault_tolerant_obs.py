# -*- coding: utf-8 -*-
"""Fault-tolerant observability + artifact-rescue tests for
scripts/sovereign_iac_hypervisor.py (the Omni-Soak #2 fix).

THE BUG: the detached surgery ran the FULL pipeline on the node (deps -> inject
-> soak -> audit -> verdict) but the local poll streamed NOTHING past the deps
line -- the byte-offset `tail -c +N` did not pull progress, the wall reaped a
COMPLETING surgery as 'hung', and the node was burned with the verdict never
pulled. The fix decouples liveness from the log stream:

  COMPONENT 1: a node-side anti-starvation HEARTBEAT (nice -20 / ionice realtime)
    ticks soak_state.json.last_active even during a long-quiet step (deps).
    Liveness is judged by last_active ADVANCING, NOT by log output.
  COMPONENT 2: a line-safe SIZE-AWARE delta sync replacing the byte-offset tail:
    stat the remote size, pull [last_synced_size, current_size], commit only up to
    the LAST complete newline, buffer the trailing partial + prepend next, resume
    from last_synced_size on a drop (zero missed lines, no half line).
  COMPONENT 3: a MANDATORY artifact-rescue (scp + sha256 verify) BEFORE any
    teardown, with a dead-SSH OUT-OF-BAND fallback (serial console + disk
    snapshot) -> NEVER burn a node before its data is local.
  COMPONENT 4: a dual-boundary phase-adaptive wall: extend on an advancing
    heartbeat, CAP at MAX_PHASE_CEILING -> a ticking-but-stuck zombie is reaped.

Master gate JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED default-OFF -> the CURRENT
byte-offset/dumb-wall behavior is byte-identical. NO real GCP/SSH: the script's
single `_run` boundary is monkeypatched.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sovereign_iac_hypervisor_ftobs", _SCRIPT
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
    monkeypatch.setenv("JARVIS_IAC_POLL_BASE_S", "0")
    monkeypatch.setenv("JARVIS_IAC_POLL_CAP_S", "0")
    monkeypatch.setenv("JARVIS_IAC_POLL_JITTER_S", "0")
    return tmp_path


@pytest.fixture()
def args(iac):
    a = iac.build_parser().parse_args([])
    a.poll_base_s = 0.0
    a.poll_cap_s = 0.0
    a.poll_jitter_s = 0.0
    a.probe_timeout_s = 5.0
    a.liveness_deadline_s = 100.0
    a.max_wall_seconds = 0.0
    a.surgery_timeout_s = 999999
    a.detached_surgery = True
    # FT-obs ON for the fault-tolerant tests (individual tests override for OFF).
    a.fault_tolerant_obs = True
    a.heartbeat_interval_s = 10.0
    a.heartbeat_stale_s = 100.0
    a.global_ceiling_s = 0.0
    a.rescue_retries = 2
    a.rescue_timeout_s = 5.0
    return a


# --------------------------------------------------------------------------- #
# COMPONENT 1: anti-starvation heartbeat -- liveness via last_active, nice/ionice.
# --------------------------------------------------------------------------- #
def test_heartbeat_block_carries_nice_and_ionice(iac, args):
    """The heartbeat launch command runs at ELEVATED OS priority: `nice -n -20`
    + ionice realtime (-c1 -n0), with a best-effort (-c2 -n0) runtime fallback."""
    block = iac._heartbeat_block(args)
    assert "nice -n -20" in block
    assert "ionice -c1 -n0" in block  # realtime IO class, preferred
    assert "ionice -c2 -n0" in block  # fallback if -c1 denied
    assert "setsid" in block          # detached from the surgery TTY
    assert "last_active" in block     # ticks last_active
    assert "step_seq" in block        # + an advancing step_seq
    assert "_HB_PID" in block         # exports its PID for the trap


def test_surgery_body_arms_heartbeat_before_deps_when_ft_on(iac, args):
    """The heartbeat is launched BEFORE the deps phase (so a long-quiet pip
    install keeps last_active advancing), and killed in the EXIT trap."""
    body = iac._remote_surgery_body_script(args)
    hb_idx = body.find("anti-starvation heartbeat")
    deps_idx = body.find("_write_state deps running")
    assert hb_idx != -1 and deps_idx != -1
    assert hb_idx < deps_idx                       # heartbeat armed BEFORE deps
    assert "last_active" in body                   # phase writer also stamps it
    assert 'kill "$_HB_PID"' in body               # trap kills the heartbeat


def test_surgery_body_no_heartbeat_when_ft_off(iac, args):
    """OFF -> the body is byte-identical to legacy (no heartbeat, no last_active)."""
    args.fault_tolerant_obs = False
    body = iac._remote_surgery_body_script(args)
    assert "anti-starvation heartbeat" not in body
    assert "last_active" not in body
    assert "_HB_PID" not in body


def test_heartbeat_advancing_keeps_liveness_during_quiet_deps_no_false_reap(
    iac, args, monkeypatch
):
    """THE run-#2 REGRESSION: during a long-quiet deps phase surgery.out emits
    NOTHING, but the heartbeat keeps ticking last_active -> the poll reports ALIVE
    and does NOT falsely reap. The wall EXTENDS while the heartbeat advances."""
    # deps phase, soft allowance tiny so we are PAST it -> the only thing keeping
    # the node alive is the advancing heartbeat (not log output).
    monkeypatch.setenv("JARVIS_IAC_PHASE_ALLOWANCE_DEPS", "0")     # past allowance
    monkeypatch.setenv("JARVIS_IAC_PHASE_CEILING_DEPS", "100000")  # huge ceiling
    args.heartbeat_stale_s = 100000.0  # heartbeat never judged frozen

    # Scripted timeline: many deps ticks with an ADVANCING last_active + a quiet
    # (empty) surgery.out, then terminal done.
    state_ticks = []
    for i in range(6):
        ts = 1000 + i  # last_active advances every tick
        state_ticks.append(
            (0, '{"phase":"deps","status":"running","rc":null,"ts":%d,'
                '"verdict":"running","last_active":%d,"step_seq":%d}' % (ts, ts, i))
        )
    state_ticks.append(
        (0, '{"phase":"done","status":"done","rc":0,"ts":2000,'
            '"verdict":"PASS","last_active":2000,"step_seq":99}')
    )

    probe = _FtProbe(state_seq=state_ticks, size_seq=[(0, "0")] * 20)
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    blob = "".join(captured)
    assert rc == 0
    assert verdict == "PASS"
    # NO false reap -- never tripped a wall / liveness deadline during quiet deps.
    assert "reaping" not in blob.lower() or "EXTENDING" in blob
    assert "EXTENDING" in blob          # the wall EXTENDED on the advancing heartbeat
    assert "terminal state" in blob     # reached the real terminal verdict


# --------------------------------------------------------------------------- #
# COMPONENT 2: line-safe size-aware delta-sync (resume on drop, no half-lines).
# --------------------------------------------------------------------------- #
def test_delta_split_line_safe(iac):
    """_split_line_safe commits only up to the LAST newline; the trailing partial
    is buffered (never a half line / mid-utf-8)."""
    committable, partial = iac._split_line_safe(b"line1\nline2\nhalf")
    assert committable == b"line1\nline2\n"
    assert partial == b"half"
    # no newline yet -> NOTHING committable, whole buffer held.
    committable2, partial2 = iac._split_line_safe(b"no newline yet")
    assert committable2 == b""
    assert partial2 == b"no newline yet"


def test_delta_sync_buffers_partial_then_prepends_next(iac, args, monkeypatch):
    """A delta ending mid-line buffers the partial; the next sync prepends it and
    the full line is emitted EXACTLY once -- never a half line."""
    seen = []
    state = iac._DeltaSyncState()

    async def _drive():
        # tick 1: remote size=10, the 10 bytes are "abc\ndef" + "gh" (no newline).
        delivered = {"stat": [(0, "10")], "delta": [(0, "abc\ndefgh")]}

        def _fake(cmd, *, timeout_s=120.0):
            blob = " ".join(cmd)
            if "stat -c" in blob:
                return delivered["stat"].pop(0)
            return delivered["delta"].pop(0)

        monkeypatch.setattr(iac, "_run", _fake)
        ok = await iac._delta_sync_once(args, "n", state, seen.append)
        assert ok
        # "abc\n" committed; "defgh" buffered (no trailing newline yet).
        assert "".join(seen) == "abc\n"
        assert state.partial == b"defgh"

        # tick 2: size grows to 16, new bytes "ij\nkl" -> combined "defghij\nkl".
        delivered2 = {"stat": [(0, "16")], "delta": [(0, "ij\nkl")]}

        def _fake2(cmd, *, timeout_s=120.0):
            blob = " ".join(cmd)
            if "stat -c" in blob:
                return delivered2["stat"].pop(0)
            return delivered2["delta"].pop(0)

        monkeypatch.setattr(iac, "_run", _fake2)
        ok2 = await iac._delta_sync_once(args, "n", state, seen.append)
        assert ok2
        # "defghij\n" now complete + emitted; "kl" buffered.
        assert "".join(seen) == "abc\ndefghij\n"
        assert state.partial == b"kl"
        # ASSERT NO HALF LINE was ever emitted: every emitted chunk ends on \n.
        for chunk in seen:
            assert chunk.endswith("\n")

    asyncio.run(_drive())


def test_delta_sync_resumes_from_last_size_across_drop_zero_missed_lines(
    iac, args, monkeypatch
):
    """A drop mid-pull leaves last_synced_size + the partial buffer UNCHANGED;
    the reconnect resumes from exactly last_synced_size -> zero missed lines."""
    seen = []
    state = iac._DeltaSyncState()
    full = "L1\nL2\nL3\nL4\n"  # 12 bytes

    async def _drive():
        # tick 1: size=6 -> pull "L1\nL2\n", commit both.
        def _t1(cmd, *, timeout_s=120.0):
            blob = " ".join(cmd)
            if "stat -c" in blob:
                return 0, "6"
            return 0, full[0:6]

        monkeypatch.setattr(iac, "_run", _t1)
        await iac._delta_sync_once(args, "n", state, seen.append)
        assert state.last_synced_size == 6
        assert "".join(seen) == "L1\nL2\n"

        # tick 2: DROP -- stat broken pipe. cursor unchanged, NOTHING emitted.
        def _drop(cmd, *, timeout_s=120.0):
            return 255, "Broken pipe"

        monkeypatch.setattr(iac, "_run", _drop)
        ok = await iac._delta_sync_once(args, "n", state, seen.append)
        assert ok is False                 # counts against liveness
        assert state.last_synced_size == 6  # UNCHANGED -> resume here
        assert "".join(seen) == "L1\nL2\n"  # no dup, no half line

        # tick 3: RECONNECT -- size=12, resume from 6 -> pull ONLY "L3\nL4\n".
        def _t3(cmd, *, timeout_s=120.0):
            blob = " ".join(cmd)
            if "stat -c" in blob:
                return 0, "12"
            # the delta range cmd must ask for bytes AFTER offset 6.
            assert "tail -c +7" in blob  # 1-indexed: bytes after byte 6
            return 0, full[6:12]

        monkeypatch.setattr(iac, "_run", _t3)
        await iac._delta_sync_once(args, "n", state, seen.append)
        assert state.last_synced_size == 12

    asyncio.run(_drive())
    # every line seen EXACTLY once, in order -> zero loss, zero dup.
    assert "".join(seen) == full


def test_delta_range_cmd_pulls_bounded_range(iac, args):
    cmd = iac._delta_range_cmd(args, "n", 100, 50)
    blob = " ".join(cmd)
    assert "tail -c +101" in blob   # 1-indexed start (bytes after 100)
    assert "head -c 50" in blob     # bounded LENGTH (not the open tail)


# --------------------------------------------------------------------------- #
# COMPONENT 3: artifact-rescue + sha256 BEFORE delete; dead-SSH -> OOB serial/snap.
# --------------------------------------------------------------------------- #
def test_rescue_scp_pulls_and_sha256_verifies_before_delete(
    iac, args, monkeypatch, tmp_path
):
    """On a teardown the rescue scp-pulls + sha256-verifies the black-box
    artifacts BEFORE the delete is issued. ASSERT ORDER: rescue+verify THEN delete."""
    events = []

    def _fake_run(cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        if "scp" in blob and "--recurse" in blob:
            events.append("rescue_pull")
            # simulate the artifact landing locally (so sha256 can verify it).
            # the local dest dir is the LAST scp arg.
            dest = cmd[-1]
            name = Path(cmd[-2].split(":", 1)[1]).name
            p = Path(dest) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("rescued artifact content")
            return 0, ""
        if "instances delete" in blob:
            events.append("delete")
            return 0, ""
        if "describe" in blob:
            return 1, "gone"
        return 0, ""

    monkeypatch.setattr(iac, "_run", _fake_run)
    manifest = iac.rescue_artifacts_before_teardown(args, "node-x", "verdict=FAILED")
    iac.burn_node(args, "node-x")

    # rescue happened, sha256 verified (verified True), and EVERY rescue precedes
    # the delete.
    assert "rescue_pull" in events
    assert "delete" in events
    assert manifest["verified"] is True
    last_rescue = max(i for i, e in enumerate(events) if e == "rescue_pull")
    first_delete = min(i for i, e in enumerate(events) if e == "delete")
    assert last_rescue < first_delete   # ORDER: rescue+verify THEN delete
    # the manifest carries a real sha256 for a pulled file artifact.
    digests = [v for v in manifest["artifacts"].values() if v and v != "dir:present"]
    assert any(len(str(d)) == 64 for d in digests)  # sha256 hex digest length


def test_rescue_dead_ssh_falls_back_to_oob_serial_and_snapshot_before_delete(
    iac, args, monkeypatch
):
    """If scp fails after max retries (sshd crashed / NIC dropped) the ultimate
    fallback BEFORE deletion is OUT-OF-BAND gcloud: get-serial-port-output +
    disks snapshot. ASSERT the OOB calls precede the delete."""
    events = []

    def _fake_run(cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        if "scp" in blob and "--recurse" in blob:
            events.append("scp_fail")
            return 255, "Broken pipe (dead sshd)"   # ALL pulls fail
        if "get-serial-port-output" in blob:
            events.append("oob_serial")
            return 0, "=== serial console dump ===\nboot ok\npanic\n"
        if "disks snapshot" in blob:
            events.append("oob_snapshot")
            return 0, ""
        if "instances delete" in blob:
            events.append("delete")
            return 0, ""
        return 0, ""

    monkeypatch.setattr(iac, "_run", _fake_run)
    manifest = iac.rescue_artifacts_before_teardown(args, "node-y", "wall_hit")
    iac.burn_node(args, "node-y")

    assert "oob_serial" in events       # serial console captured out-of-band
    assert "oob_snapshot" in events     # disk snapshot captured out-of-band
    assert "delete" in events
    # the OOB capture PRECEDES the delete (never burn-blind).
    first_delete = events.index("delete")
    assert events.index("oob_serial") < first_delete
    assert events.index("oob_snapshot") < first_delete
    assert manifest["verified"] is False           # pulls failed -> not verified
    assert manifest["oob"].get("serial")           # serial captured to a file
    assert str(manifest["oob"].get("snapshot")).startswith("rescue-")


def test_rescue_is_noop_when_ft_off_byte_identical(iac, args, monkeypatch):
    """OFF -> rescue is a no-op (legacy autopsy-only behavior); _run never called
    for a scp pull / OOB capture."""
    args.fault_tolerant_obs = False

    def _no_run(cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        assert "scp" not in blob and "serial" not in blob and "snapshot" not in blob, (
            "OFF rescue must NOT pull / OOB-capture"
        )
        return 0, ""

    monkeypatch.setattr(iac, "_run", _no_run)
    manifest = iac.rescue_artifacts_before_teardown(args, "node-z", "verdict=FAILED")
    assert manifest["artifacts"] == {}
    assert manifest["verified"] is False


def test_rescue_never_raises_fail_soft(iac, args, monkeypatch):
    """A rescue error NEVER crashes the harness (fail-soft) -- it still returns a
    manifest the caller can proceed past to the burn."""
    def _boom(cmd, *, timeout_s=120.0):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(iac, "_run", _boom)
    manifest = iac.rescue_artifacts_before_teardown(args, "node-b", "exc")
    assert isinstance(manifest, dict)   # returned, did not raise


# --------------------------------------------------------------------------- #
# COMPONENT 4: dual-boundary phase-adaptive wall (extend then MAX_PHASE_CEILING).
# --------------------------------------------------------------------------- #
def test_wall_extends_on_advancing_heartbeat_in_fanout(iac, args, monkeypatch):
    """An advancing heartbeat in a heavy phase past the soft allowance EXTENDS the
    wall (the node is NOT reaped as long as the heartbeat advances)."""
    monkeypatch.setenv("JARVIS_IAC_PHASE_ALLOWANCE_INJECT", "0")     # past allowance
    monkeypatch.setenv("JARVIS_IAC_PHASE_CEILING_INJECT", "100000")  # huge ceiling
    args.heartbeat_stale_s = 100000.0

    ticks = []
    for i in range(5):
        ts = 5000 + i  # heartbeat advances
        ticks.append(
            (0, '{"phase":"inject","status":"running","rc":null,"ts":%d,'
                '"verdict":"running","last_active":%d,"step_seq":%d}' % (ts, ts, i))
        )
    ticks.append(
        (0, '{"phase":"done","status":"done","rc":0,"ts":6000,'
            '"verdict":"PASS","last_active":6000,"step_seq":50}')
    )
    probe = _FtProbe(state_seq=ticks, size_seq=[(0, "0")] * 20)
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    blob = "".join(captured)
    assert "EXTENDING" in blob          # extended on the advancing heartbeat
    assert verdict == "PASS"


def test_wall_reaps_zombie_at_max_phase_ceiling_even_if_heartbeat_ticks(
    iac, args, monkeypatch
):
    """CONSTRAINT 4 (trust but BOUND): a `while True` that STILL ticks the
    heartbeat CANNOT extend infinitely -- it is REAPED at MAX_PHASE_CEILING."""
    monkeypatch.setenv("JARVIS_IAC_PHASE_ALLOWANCE_INJECT", "0")
    # ceiling 0 -> elapsed (>0 after the first tick) immediately exceeds it -> reap
    # even though the heartbeat keeps advancing (the zombie can't extend forever).
    monkeypatch.setenv("JARVIS_IAC_PHASE_CEILING_INJECT", "0")
    monkeypatch.setenv("JARVIS_IAC_GLOBAL_CEILING_SECONDS", "100000")  # not this one
    args.global_ceiling_s = 100000.0
    args.heartbeat_stale_s = 100000.0  # heartbeat NEVER frozen -> only ceiling reaps

    # the heartbeat keeps advancing FOREVER (a stuck-but-ticking swarm).
    ticks = []
    for i in range(40):
        ts = 7000 + i
        ticks.append(
            (0, '{"phase":"inject","status":"running","rc":null,"ts":%d,'
                '"verdict":"running","last_active":%d,"step_seq":%d}' % (ts, ts, i))
        )
    probe = _FtProbe(state_seq=ticks, size_seq=[(0, "0")] * 60)
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    blob = "".join(captured)
    assert rc == 124                            # ceiling reap code
    assert "MAX_PHASE_CEILING" in blob          # reaped at the absolute ceiling
    assert "no infinite extend" in blob


def test_frozen_heartbeat_past_allowance_reaps(iac, args, monkeypatch):
    """A heartbeat that STOPS advancing past the soft allowance is a true hang
    (not a quiet step) -> reaped on the staleness boundary."""
    monkeypatch.setenv("JARVIS_IAC_PHASE_ALLOWANCE_INJECT", "0")     # past allowance
    monkeypatch.setenv("JARVIS_IAC_PHASE_CEILING_INJECT", "100000")  # ceiling not hit
    args.heartbeat_stale_s = -1.0  # any non-advance immediately judged FROZEN

    # last_active NEVER changes (frozen heartbeat) across many ticks.
    frozen = (0, '{"phase":"inject","status":"running","rc":null,"ts":8000,'
                 '"verdict":"running","last_active":8000,"step_seq":1}')
    probe = _FtProbe(state_seq=[frozen] * 30, size_seq=[(0, "0")] * 40)
    rc, captured, verdict = _run_loop(iac, args, "n", probe, monkeypatch)
    blob = "".join(captured)
    assert rc == 125                       # liveness reap code
    assert "heartbeat FROZEN" in blob


def test_phase_allowance_and_ceiling_env_tunable(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_PHASE_ALLOWANCE_DEPS", "111")
    monkeypatch.setenv("JARVIS_IAC_PHASE_CEILING_SOAK", "222")
    assert iac._phase_allowance("deps") == 111.0
    assert iac._phase_ceiling("soak") == 222.0
    # unknown phase -> falls back to the generic defaults (no hardcoded crash).
    assert iac._phase_allowance("mystery") > 0
    assert iac._phase_ceiling("mystery") > 0


# --------------------------------------------------------------------------- #
# OFF flag -> byte-identical legacy behavior (no delta-sync, dumb wall).
# --------------------------------------------------------------------------- #
def test_off_flag_uses_byte_offset_tail_not_delta_sync(iac, args, monkeypatch):
    """OFF -> the poll loop uses the legacy byte-offset tail (`tail -c +N`), NOT
    delta-sync (no `stat -c %s` / `head -c`)."""
    args.fault_tolerant_obs = False
    seen_cmds = []

    state_seq = [
        (0, '{"phase":"deps","status":"running","rc":null,"ts":1,"verdict":"running"}'),
        (0, '{"phase":"done","status":"done","rc":0,"ts":2,"verdict":"PASS"}'),
    ]
    si = {"i": 0}

    def _fake_run(cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        seen_cmds.append(blob)
        if "tail -c +" in blob and "head -c" not in blob:
            return 0, ""                 # legacy byte-offset tail
        if "soak_in_progress.lock" in blob or "kill -0" in blob:
            return 0, "ALIVE 1"
        if "soak_state.json" in blob or "cat " in blob:
            out = state_seq[min(si["i"], len(state_seq) - 1)]
            si["i"] += 1
            return out
        return 0, ""

    monkeypatch.setattr(iac, "_run", _fake_run)
    rc, captured, verdict = iac.run_remote_surgery_detached(args, "n")
    assert verdict == "PASS"
    joined = " ".join(seen_cmds)
    assert "tail -c +" in joined        # legacy byte-offset tail used
    assert "stat -c %s" not in joined   # delta-sync NOT used
    assert "head -c" not in joined
    # the legacy engage banner, NOT the fault-tolerant one.
    assert any("detached poll/reconnect loop engaged" in c for c in captured)
    assert not any("fault-tolerant poll" in c for c in captured)


def test_ft_default_off(iac):
    """Master gate defaults OFF (byte-identical) when neither arg nor env set."""
    a = iac.build_parser().parse_args([])
    assert a.fault_tolerant_obs is False
    assert iac._fault_tolerant_obs_enabled(a) is False


def test_ft_env_arms_it(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    a = iac.build_parser().parse_args([])
    assert a.fault_tolerant_obs is True


# --------------------------------------------------------------------------- #
# Stateful fake `_run` for the FT poll loop (dispatches by command shape).
# --------------------------------------------------------------------------- #
class _FtProbe:
    """Dispatches by command shape: stat (delta-sync size), delta-range pull,
    state read, lock probe. Replays scripted timelines. Each entry is (rc, out)."""

    def __init__(self, *, state_seq, size_seq=None, delta_seq=None, lock_seq=None):
        self.state_seq = list(state_seq)
        self.size_seq = list(size_seq or [])
        self.delta_seq = list(delta_seq or [])
        self.lock_seq = list(lock_seq or [])
        self.calls = []

    def _pop(self, seq, default):
        return seq.pop(0) if seq else default

    def __call__(self, cmd, *, timeout_s=120.0):
        blob = " ".join(cmd)
        self.calls.append(blob)
        if "stat -c %s" in blob:
            return self._pop(self.size_seq, (0, "0"))
        if "head -c" in blob or "tail -c +" in blob:
            return self._pop(self.delta_seq, (0, ""))
        if "soak_in_progress.lock" in blob or "kill -0" in blob:
            return self._pop(self.lock_seq, (0, "ALIVE 1234"))
        if "soak_state.json" in blob or "cat " in blob:
            return self._pop(self.state_seq, (0, ""))
        return 0, "launched\n"


def _run_loop(iac, args, node, probe, monkeypatch):
    monkeypatch.setattr(iac, "_run", probe)
    return iac.run_remote_surgery_detached(args, node)
