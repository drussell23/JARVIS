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
        if base["session_outcome"] != "in_flight":
            base.pop("started_at")   # like the harness: the FINAL schema has none
        (d / "summary.json").write_text(json.dumps(base))
    (d / "wall_deadline.json").write_text(json.dumps({"deadline_wall": time.time() + 600}))
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


# ---------------------------------------------------------------------------
# DiskGuard: a detached soak cannot run a disk out of space
# ---------------------------------------------------------------------------

GB = 1024 ** 3


def _guard(tmp_path, free, *, trips, log_dir=None, protect=()):
    return TS.DiskGuard(
        volumes=[tmp_path], sweep_dirs=[tmp_path / "logs", tmp_path / "sessions"],
        log_dir=log_dir, on_trip=trips.append, protect=lambda: list(protect),
        free_probe=lambda _p: free,
    )


def _aged(path: Path, text: str, days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - days * 86400
    os.utime(path, (t, t))
    return path


def test_plenty_of_space_does_nothing(tmp_path):
    trips = []
    assert _guard(tmp_path, 400 * GB, trips=trips).check_once() is None
    assert trips == []


def test_the_soft_floor_rotates_old_logs_and_spares_the_live_session(tmp_path):
    old = _aged(tmp_path / "logs" / "soak-old.log", "x" * 50_000, days=3)
    live = _aged(tmp_path / "sessions" / "bt-live" / "debug.log", "y" * 50_000, days=3)
    trips = []
    g = _guard(tmp_path, 10 * GB, trips=trips, protect=[tmp_path / "sessions" / "bt-live"])
    assert g.check_once() == "sweep"
    assert not old.exists() and (tmp_path / "logs" / "soak-old.log.gz").exists()
    assert live.exists(), "the live session's log must never be rotated"
    assert trips == [] and g.sweeps[0]["compressed"] == 1


def test_the_log_quota_rotates_even_with_free_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_LOG_QUOTA_GB", str(10_000 / GB))
    _aged(tmp_path / "logs" / "soak-old.log", "z" * 50_000, days=3)
    trips = []
    g = _guard(tmp_path, 400 * GB, trips=trips, log_dir=tmp_path / "logs")
    assert g.check_once() == "sweep"
    assert g.sweeps[0]["why"].startswith("log_quota")


def test_the_hard_floor_trips_once(tmp_path):
    trips = []
    g = _guard(tmp_path, 2 * GB, trips=trips)
    assert g.check_once() == "trip"
    assert g.check_once() is None
    assert len(trips) == 1 and trips[0].startswith("disk_guard:")


def test_a_hard_trip_gracefully_stops_the_daemon_and_is_recorded(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    monkeypatch.setenv("JARVIS_DISK_GUARD_HARD_FREE_GB", str(10 ** 9))  # every disk is "full"
    monkeypatch.setenv("JARVIS_DISK_GUARD_INTERVAL_S", "0.2")
    monkeypatch.setenv("JARVIS_DISK_GUARD_VOLUMES", str(tmp_path))
    rc = TS.supervise(_daemon(tmp_path, "graceful"), sessions_root=tmp_path / "sessions")
    assert rc == 0
    s = _summary(tmp_path)
    assert (s["session_outcome"], s["stop_reason"]) == ("incomplete_kill", "sigterm")
    t = _terminal(tmp_path)
    assert t["stop_reason"].startswith("disk_guard:") and t["disk_guard"]["tripped"]
