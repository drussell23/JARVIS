"""Every soak ends with a recorded reason, including the deaths Python never sees.

bt-2026-09-23-005910 was left ``in_flight`` / ``stop_reason: unknown``: the
WSL VM stopped under it when the interactive session holding its wsl.exe
closed, and no in-process hook runs after that. SIGKILL and the OOM killer
are the same shape inside a live VM.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import terminal_supervisor as TS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

_REPO = Path(__file__).resolve().parents[2]

_FAKE_DAEMON = textwrap.dedent("""
    import json, os, signal, sys, time
    from pathlib import Path
    root, mode = Path(sys.argv[1]), sys.argv[2]
    d = root / "bt-fake"
    d.mkdir(parents=True)
    def summary(**kw):
        base = {"session_id": d.name, "session_outcome": "in_flight",
                "stop_reason": "unknown", "started_at": time.time()}
        base.update(kw)
        (d / "summary.json").write_text(json.dumps(base))
    summary()
    (d / "heartbeat.tick").write_text(str(time.time()))
    if mode == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "clean":
        summary(session_outcome="complete", stop_reason="idle_timeout")
        sys.exit(0)
    if mode == "graceful":
        def on_term(signum, frame):
            summary(session_outcome="incomplete_kill", stop_reason="sigterm")
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
        print("ready", flush=True)
        time.sleep(60)
""")


def _daemon(tmp_path: Path, mode: str) -> list:
    script = tmp_path / "fake_daemon.py"
    script.write_text(_FAKE_DAEMON)
    return [sys.executable, str(script), str(tmp_path / "sessions"), mode]


def _summary(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "sessions" / "bt-fake" / "summary.json").read_text())


def _terminal(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "sessions" / "bt-fake" / "terminal.json").read_text())


def test_a_sigkilled_daemon_is_recorded_as_killed(tmp_path):
    (tmp_path / "sessions").mkdir()
    rc = TS.supervise(_daemon(tmp_path, "sigkill"), sessions_root=tmp_path / "sessions")
    assert rc == 128 + signal.SIGKILL
    s = _summary(tmp_path)
    assert s["session_outcome"] == "incomplete_kill"
    assert s["stop_reason"] in ("killed:SIGKILL", "killed:oom")
    assert s["reconciled_by"] == "terminal_supervisor"
    t = _terminal(tmp_path)
    assert t["signal"] == "SIGKILL" and t["summary_stamped"] is True


def test_a_recorded_ending_is_never_overwritten(tmp_path):
    (tmp_path / "sessions").mkdir()
    rc = TS.supervise(_daemon(tmp_path, "clean"), sessions_root=tmp_path / "sessions")
    assert rc == 0
    s = _summary(tmp_path)
    assert (s["session_outcome"], s["stop_reason"]) == ("complete", "idle_timeout")
    assert "reconciled_by" not in s
    t = _terminal(tmp_path)
    assert t["stop_reason"] == "exit:0" and t["summary_stamped"] is False


def test_sigterm_to_the_supervisor_reaches_the_daemons_own_handler(tmp_path):
    (tmp_path / "sessions").mkdir()
    env = dict(os.environ, PYTHONPATH=str(_REPO))
    sup = subprocess.Popen(
        [sys.executable, "-m", "backend.core.ouroboros.battle_test.terminal_supervisor",
         "--sessions-root", str(tmp_path / "sessions"), "--", *_daemon(tmp_path, "graceful")],
        cwd=str(_REPO), env=env, stdout=subprocess.PIPE, text=True,
    )
    assert sup.stdout.readline().strip() == "ready"
    sup.send_signal(signal.SIGTERM)
    assert sup.wait(timeout=30) == 0
    s = _summary(tmp_path)
    assert (s["session_outcome"], s["stop_reason"]) == ("incomplete_kill", "sigterm")
    assert "reconciled_by" not in s
    assert _terminal(tmp_path)["stop_reason"] == "exit:0"


# ---------------------------------------------------------------------------
# The VM stopped under it: nothing ran, so the next boot must say why
# ---------------------------------------------------------------------------

def _orphan(root: Path, name: str, beat: float, outcome: str = "in_flight") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps(
        {"session_id": name, "session_outcome": outcome, "stop_reason": "unknown"}))
    (d / "heartbeat.tick").write_text(str(beat))
    return d


# bt-2026-09-23-005910, measured: its boot's last journal entry 19:06:00, its
# last heartbeat 19:06:37 (the journal was simply quiet), next boot 09:45.
NOW = 1_790_184_000.0
BEAT = NOW - 52_000
BOOTS = [
    (BEAT - 4_000, BEAT - 37),        # the boot it ran in: ended as it died
    (NOW - 600, NOW),                 # the current boot
]


def test_a_boot_that_ended_with_the_session_is_a_stopped_vm(tmp_path):
    _orphan(tmp_path, "bt-2026-09-23-005910", beat=BEAT)
    got = TS.reconcile_orphans(tmp_path, now=NOW, btime=NOW - 600, stale_s=240, boots=BOOTS)
    assert got == [("bt-2026-09-23-005910", "host_vm_stopped")]
    s = json.loads((tmp_path / "bt-2026-09-23-005910" / "summary.json").read_text())
    assert s["session_outcome"] == "incomplete_kill"
    assert s["terminal_evidence"]["boot_last_entry"] == BEAT - 37


def test_an_older_boot_that_ran_on_is_a_vanished_process_not_a_vm_stop(tmp_path):
    """Being older than the current boot proves nothing by itself."""
    _orphan(tmp_path, "bt-a", beat=BEAT - 3_000)   # its boot ran ~50 min longer
    assert TS.reconcile_orphans(tmp_path, now=NOW, btime=NOW - 600, stale_s=240, boots=BOOTS) == [
        ("bt-a", "process_vanished")]


def test_a_boot_the_journal_forgot_is_said_to_be_unknown(tmp_path):
    _orphan(tmp_path, "bt-old", beat=BEAT - 900_000)
    assert TS.reconcile_orphans(tmp_path, now=NOW, btime=NOW - 600, stale_s=240, boots=BOOTS) == [
        ("bt-old", "vanished_cause_unknown")]


def test_a_silent_heartbeat_within_this_boot_is_an_uncatchable_kill(tmp_path):
    _orphan(tmp_path, "bt-a", beat=NOW - 1_000)
    assert TS.reconcile_orphans(tmp_path, now=NOW, btime=NOW - 90_000, stale_s=240, boots=BOOTS) == [
        ("bt-a", "process_vanished")]


def test_a_live_heartbeat_and_a_recorded_ending_are_left_alone(tmp_path):
    _orphan(tmp_path, "bt-live", beat=NOW - 30)
    _orphan(tmp_path, "bt-done", beat=BEAT, outcome="complete")
    assert TS.reconcile_orphans(tmp_path, now=NOW, btime=NOW - 600, stale_s=240, boots=BOOTS) == []
    for name in ("bt-live", "bt-done"):
        assert "reconciled_by" not in json.loads((tmp_path / name / "summary.json").read_text())


def test_dry_run_writes_nothing(tmp_path):
    _orphan(tmp_path, "bt-x", beat=BEAT)
    assert TS.reconcile_orphans(tmp_path, now=NOW, btime=NOW - 600, stale_s=240, boots=BOOTS,
                                dry_run=True)
    assert json.loads((tmp_path / "bt-x" / "summary.json").read_text())["session_outcome"] == "in_flight"


def test_stale_window_derives_from_the_harness_watchdog(monkeypatch):
    monkeypatch.delenv("JARVIS_TERMINAL_SUPERVISOR_STALE_S", raising=False)
    monkeypatch.setenv("JARVIS_EXTERNAL_WATCHDOG_STALE_S", "150")
    assert TS.stale_after_s() == 300.0
    monkeypatch.setenv("JARVIS_TERMINAL_SUPERVISOR_STALE_S", "45")
    assert TS.stale_after_s() == 45.0
