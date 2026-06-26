# -*- coding: utf-8 -*-
"""True-daemonized fault-tolerant-obs HEARTBEAT tests for
scripts/sovereign_iac_hypervisor.py (the Omni-Soak v4/v5/v6 1815s-freeze fix).

THE BUG (DETERMINISTIC): the heartbeat froze at EXACTLY 1815s in v4/v5/v6 -- it
stopped ticking soak_state.json.last_active the instant the O+V soak (battle_test)
launched. The OLD launch was a weak `setsid nice -20 ionice bash -c '<loop>' &`,
which the battle_test harness's process-group zombie-reaper (it "auto-reaps any
zombie ouroboros_battle_test.py processes" on startup) swept up WITH the soak --
taking the heartbeat down. The dual-boundary wall then correctly reaped at
~1815s+allowance, but with verdict=UNKNOWN: no soak ever completed.

THE FIX (rendered into _heartbeat_block when FT-obs is ON):
  1. TRUE DOUBLE-FORK DAEMON: setsid (new session, no controlling TTY) -> a second
     fork (a `( ... ) &` subshell that can NEVER reacquire a TTY, in its OWN
     process group) -> dedicated PID file -> the loop. The OUTER setsid bash is
     disowned -> the daemon is reparented to init, NOT in the surgery's process
     group, IMMUNE to `kill -- -<pgid>` / `pkill -g` sweeps (the v4/v5/v6 killer).
  2. THE DEATH RATTLE: the daemon installs `trap _rattle SIGINT SIGTERM EXIT`. The
     rattle ATOMICALLY (temp+mv) writes phase=HEARTBEAT_ASSASSINATED_BY_SIGNAL +
     a fresh ts to soak_state.json BEFORE exiting -> a kill is INSTANTLY
     observable to the local poll (reap-with-cause), NOT a 100-min silent
     stale-out.
  3. The surgery EXIT trap STILL reaps the daemon on a NORMAL surgery exit (kills
     the PID-file pid) -- but the daemon survives the soak-launch reaper DURING
     the soak (different session/pgroup).

Master gate JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED default-OFF -> the LEGACY
setsid snippet is byte-identical. NO real GCP/SSH in the rendered-snippet tests;
the real-process tests fork/killpg a minimal local copy of the daemon snippet and
are guarded to skip cleanly where the sandbox forbids fork/killpg.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sovereign_iac_hypervisor_hbd", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def iac():
    return _load_module()


@pytest.fixture()
def args_on(iac, monkeypatch, tmp_path):
    """FT-obs ON args, with the soak_state + PID paths redirected into tmp."""
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    state = str(tmp_path / "soak_state.json")
    monkeypatch.setenv("JARVIS_IAC_SOAK_STATE_PATH", state)
    monkeypatch.setenv("JARVIS_IAC_HEARTBEAT_PID_PATH", state + ".hb.pid")
    monkeypatch.setenv("JARVIS_IAC_HEARTBEAT_INTERVAL_S", "0.2")
    # The module read the path consts at import time -> reload so the redirected
    # paths take effect in the freshly-loaded module.
    mod = _load_module()
    a = mod.build_parser().parse_args([])
    a.surgery_cmd = "echo soak"
    return mod, a, state


# --------------------------------------------------------------------------- #
# (a) Rendered-snippet asserts: double-fork + PID file + death-rattle trap.
# --------------------------------------------------------------------------- #
def test_rendered_block_has_double_fork(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    a = iac.build_parser().parse_args([])
    block = iac._heartbeat_block(a)
    # setsid (fork #1 + new session) + a backgrounded child (fork #2) + disown so
    # the daemon is NOT in the surgery's process group.
    assert "setsid nice" in block
    assert "disown" in block
    assert "DAEMON" in block  # the daemon banner, not the legacy launch
    # still elevated priority (reused, not duplicated).
    assert "nice -n -20" in block
    assert "ionice -c1 -n0" in block
    assert "ionice -c2 -n0" in block  # runtime fallback


def test_rendered_block_writes_pid_file(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    a = iac.build_parser().parse_args([])
    block = iac._heartbeat_block(a)
    # the daemon (second fork, a `bash -c`) writes its OWN $$ to the dedicated PID
    # file; the launcher reads it back into _HB_PID.
    assert "echo $$ >" in block
    assert ".hb.pid" in block
    assert "_HB_PID=$(cat" in block
    assert "export _HB_PID" in block


def test_rendered_block_has_death_rattle_trap(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    a = iac.build_parser().parse_args([])
    block = iac._heartbeat_block(a)
    # the trap fires on SIGINT/SIGTERM/EXIT and writes the assassinated phase.
    assert "trap _rattle SIGINT SIGTERM EXIT" in block
    assert "HEARTBEAT_ASSASSINATED_BY_SIGNAL" in block
    # the rattle writes atomically (temp + mv), reusing the writer shape.
    assert "mv -f" in block


def test_rattle_phase_is_env_tunable(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IAC_HEARTBEAT_ASSASSINATED_PHASE", "CUSTOM_RATTLE")
    mod = _load_module()
    a = mod.build_parser().parse_args([])
    block = mod._heartbeat_block(a)
    assert "CUSTOM_RATTLE" in block


def test_rendered_block_is_ascii_and_bash_valid(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    a = iac.build_parser().parse_args([])
    block = iac._heartbeat_block(a)
    assert block.isascii()
    r = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_surgery_body_trap_reaps_daemon_via_pid_file(iac, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", "true")
    a = iac.build_parser().parse_args([])
    a.surgery_cmd = "echo x"
    body = iac._remote_surgery_body_script(a)
    # the EXIT trap reaps via the exported pid AND the pid file (belt+suspenders).
    assert 'kill "$_HB_PID"' in body
    assert ".hb.pid" in body
    assert 'kill "$_hbf"' in body
    r = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# (d) OFF flag -> legacy setsid snippet byte-identical.
# --------------------------------------------------------------------------- #
def test_off_flag_is_legacy_setsid_byte_identical(iac, monkeypatch):
    monkeypatch.delenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", raising=False)
    a = iac.build_parser().parse_args([])
    block = iac._heartbeat_block(a)
    # legacy markers present, daemon markers ABSENT.
    assert "setsid nice" in block
    assert "& _HB_PID=$!" in block
    assert "DAEMON" not in block
    assert "trap _rattle" not in block
    assert "HEARTBEAT_ASSASSINATED_BY_SIGNAL" not in block
    assert ".hb.pid" not in block
    assert "disown" not in block


def test_off_flag_byte_identical_to_git_head(iac, monkeypatch):
    """The OFF heartbeat snippet must be byte-identical to the pre-change HEAD
    rendering -- the gated default-OFF byte-identical guarantee."""
    head_src = subprocess.run(
        ["git", "show", "HEAD:scripts/sovereign_iac_hypervisor.py"],
        cwd=str(_SCRIPT.resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    if head_src.returncode != 0:
        pytest.skip("git HEAD unavailable")
    head_path = _SCRIPT.parent / "_hbd_head_snapshot.py"
    head_path.write_text(head_src.stdout)
    try:
        spec = importlib.util.spec_from_file_location("iac_head_hbd", head_path)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
    finally:
        head_path.unlink(missing_ok=True)
    monkeypatch.delenv("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED", raising=False)
    a_old = old.build_parser().parse_args([])
    a_new = iac.build_parser().parse_args([])
    assert old._heartbeat_block(a_old) == iac._heartbeat_block(a_new)


# --------------------------------------------------------------------------- #
# Real-process integration tests. Guarded to skip cleanly where fork/killpg are
# unavailable (some sandboxes). The rendered wrapper uses Linux-only `setsid` /
# `ionice`; on a host that lacks `setsid` (macOS dev) we launch the SAME inner
# daemon PROGRAM (reused via _heartbeat_daemon_inner_program) ourselves through
# os.setsid in a preexec_fn -- replicating the new-session double-fork portably.
# On Linux/CI (setsid present) the same path also holds, so the tests run on both.
# --------------------------------------------------------------------------- #
_FORK_OK = (
    hasattr(os, "fork")
    and hasattr(os, "setsid")
    and hasattr(os, "killpg")
    and hasattr(os, "getpgid")
)


def _read_last_active(path: str):
    import json

    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:
        return None


def _fast_loop(state: str) -> str:
    """A minimal fast-interval writer loop (the SAME shape as the production
    loop_body: phase + last_active + advancing step_seq, atomic temp+mv)."""
    return (
        "_seq=0; while true; do _seq=$((_seq+1)); "
        f"_tmp={state}.hb.$$; "
        "printf '{\"phase\":\"running\",\"status\":\"running\",\"rc\":null,"
        "\"ts\":%s,\"verdict\":\"running\",\"last_active\":%s,\"step_seq\":%s}\\n' "
        "\"$(date +%s)\" \"$(date +%s)\" \"$_seq\" "
        f"> \"$_tmp\" 2>/dev/null && mv -f \"$_tmp\" {state} 2>/dev/null || true; "
        "sleep 0.2; done"
    )


def _spawn_daemon_own_session(mod, state: str):
    """Launch the reused inner daemon program (loop + PID file + death-rattle) in
    its OWN new session via os.setsid -- portable across Linux/macOS. Returns the
    Popen handle (the session leader). The daemon (this process) is thus NOT in the
    test runner's process group: a killpg of the SURGERY pgroup cannot reach it."""
    inner = mod._heartbeat_daemon_inner_program(_fast_loop(state))
    return subprocess.Popen(
        ["bash", "-c", inner],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        preexec_fn=os.setsid,  # NEW SESSION -> own pgroup, no controlling TTY
    )


@pytest.mark.skipif(not _FORK_OK, reason="fork/setsid/killpg unavailable in sandbox")
def test_daemon_survives_process_group_kill_and_keeps_advancing(args_on, tmp_path):
    """THE v4/v5/v6 FIX, PROVEN: a 'surgery' process lives in its OWN process
    group; the heartbeat daemon is launched into a DIFFERENT session/pgroup (the
    double-fork). Fire a process-group kill at the SURGERY's pgid (the battle_test
    reaper's weapon). The daemon SURVIVES and KEEPS advancing last_active. The old
    weak in-pgroup setsid loop would have been swept (the 1815s freeze)."""
    mod, a, state = args_on

    # the SURGERY: a process in its own new session/pgroup, just sleeping (the soak).
    surgery = subprocess.Popen(
        ["bash", "-c", "sleep 30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    surgery_pgid = os.getpgid(surgery.pid)

    # the DAEMON: launched into ITS OWN session/pgroup (the double-fork), distinct
    # from the surgery's pgroup -> a killpg(surgery_pgid) cannot reach it.
    daemon = _spawn_daemon_own_session(mod, state)
    daemon_pgid = os.getpgid(daemon.pid)
    assert daemon_pgid != surgery_pgid, "daemon must NOT share the surgery's pgroup"
    try:
        deadline = time.time() + 10
        first = None
        while time.time() < deadline:
            first = _read_last_active(state)
            if first and first.get("last_active"):
                break
            time.sleep(0.1)
        assert first and first.get("last_active"), "daemon never wrote last_active"
        la1 = int(first["last_active"])
        seq1 = int(first.get("step_seq", 0))

        # FIRE THE PROCESS-GROUP KILL at the surgery's whole pgroup (the exact
        # sweep that took the old in-pgroup setsid loop down at soak-launch).
        os.killpg(surgery_pgid, signal.SIGKILL)
        try:
            surgery.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        # the DAEMON must STILL be alive and STILL advancing across an interval.
        time.sleep(1.0)
        after = _read_last_active(state)
        assert after is not None, "state file vanished after pgroup kill"
        assert after.get("phase") != "HEARTBEAT_ASSASSINATED_BY_SIGNAL"
        assert daemon.poll() is None, "daemon died on the pgroup sweep (the bug!)"
        la2 = int(after["last_active"])
        seq2 = int(after.get("step_seq", 0))
        assert la2 >= la1, "daemon stopped advancing after the pgroup kill"
        assert seq2 > seq1, "daemon alive but step_seq not advancing past the sweep"
    finally:
        for pg in (daemon_pgid, surgery_pgid):
            try:
                os.killpg(pg, signal.SIGKILL)
            except Exception:
                pass


@pytest.mark.skipif(not _FORK_OK, reason="fork/setsid/killpg unavailable in sandbox")
def test_direct_term_writes_death_rattle_before_dying(args_on, tmp_path):
    """(c) THE RATTLE: a direct SIGTERM to the daemon -> it writes
    phase=HEARTBEAT_ASSASSINATED_BY_SIGNAL to soak_state.json BEFORE it dies
    (proven by reading the file after the kill)."""
    mod, a, state = args_on
    daemon = _spawn_daemon_own_session(mod, state)
    daemon_pgid = os.getpgid(daemon.pid)
    pidf = state + ".hb.pid"
    dpid = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                dpid = int(Path(pidf).read_text().strip())
                if dpid:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        assert dpid, "daemon never wrote its PID file"
        time.sleep(0.5)
        pre = _read_last_active(state)
        assert pre and pre.get("phase") != "HEARTBEAT_ASSASSINATED_BY_SIGNAL"

        # DIRECT SIGTERM to the daemon -> the rattle MUST fire before it exits.
        os.kill(dpid, signal.SIGTERM)
        deadline = time.time() + 5
        rattled = None
        while time.time() < deadline:
            cur = _read_last_active(state)
            if cur and cur.get("phase") == "HEARTBEAT_ASSASSINATED_BY_SIGNAL":
                rattled = cur
                break
            time.sleep(0.1)
        assert rattled is not None, (
            "no death rattle: phase=HEARTBEAT_ASSASSINATED_BY_SIGNAL never written"
        )
        assert rattled.get("step_seq") == -1  # the rattle marker
        assert int(rattled.get("last_active", 0)) > 0  # fresh ts stamped
    finally:
        try:
            os.killpg(daemon_pgid, signal.SIGKILL)
        except Exception:
            pass


@pytest.mark.skipif(not _FORK_OK, reason="fork/setsid/killpg unavailable in sandbox")
def test_normal_surgery_exit_reaps_daemon_via_pid_file(args_on, tmp_path):
    """The surgery EXIT-trap cleanup reaps the daemon on a NORMAL exit: spawn the
    daemon, then run the SAME cleanup the surgery trap runs (kill the pid-file pid)
    and assert the daemon is dead -- distinct from the soak-launch reaper that must
    NOT take it (proven by the survives-pgroup-kill test above)."""
    mod, a, state = args_on
    daemon = _spawn_daemon_own_session(mod, state)
    daemon_pgid = os.getpgid(daemon.pid)
    pidf = state + ".hb.pid"
    dpid = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                dpid = int(Path(pidf).read_text().strip())
                if dpid:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        assert dpid, "daemon never wrote its PID file"
        assert os.path.exists(pidf)

        # the surgery EXIT trap's cleanup: read the pid file, kill it, rm the file.
        cleanup = (
            f'_hbf=$(cat {state}.hb.pid 2>/dev/null || true); '
            '[ -n "$_hbf" ] && kill "$_hbf" 2>/dev/null || true; '
            f'rm -f {state}.hb.pid 2>/dev/null || true'
        )
        subprocess.run(["bash", "-c", cleanup], timeout=10)
        # the daemon must EXIT on the normal-exit cleanup kill (the rattle trap
        # fires, then it exits). Since this test process is its parent, wait() to
        # reap it and confirm it actually terminated (a bare os.kill(pid,0) would
        # falsely see the un-reaped zombie as 'alive').
        rc = daemon.wait(timeout=5)
        assert rc is not None  # the daemon terminated on the cleanup kill
        assert not os.path.exists(pidf)  # pid file removed by the cleanup
    finally:
        try:
            os.killpg(daemon_pgid, signal.SIGKILL)
        except Exception:
            pass
