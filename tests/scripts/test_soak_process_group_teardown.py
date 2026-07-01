"""Process-group teardown for the soak organism (zero orphaned worker pools).

The leak: `pkill`/SIGTERM on the organism PARENT orphaned its multiprocessing
worker pool + resource_trackers (PPID->1) -> 48 orphans accumulated -> OOM. The
fix: launch the organism as its OWN process-group/session leader
(start_new_session=True) and tear down the ENTIRE group (SIGTERM -> grace ->
SIGKILL), so the workers die WITH the parent. Driver reaps via finally+atexit+signal
(mirrors the proven _reap_failover_resources pattern).
"""
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, name + ".py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_chaos = _load_script("a1_live_fire_chaos_harness")
_drv = _load_script("isomorphic_a1_local")


class _FakeProc:
    def __init__(self, pid=4242, exits_after_sigterm=False):
        self.pid = pid
        self._exits = exits_after_sigterm
        self._term_seen = False
        self.waits = 0

    def poll(self):
        return 0 if (self._exits and self._term_seen) else None

    def wait(self, timeout=None):
        self.waits += 1
        if self._exits and self._term_seen:
            return 0
        raise _chaos.subprocess.TimeoutExpired(cmd="soak", timeout=timeout)


# ---------------------------------------------------------------------------
# launch -> new session/group leader
# ---------------------------------------------------------------------------

def test_launch_starts_new_session(monkeypatch, tmp_path):
    captured = {}

    def _fake_popen(argv, **kw):
        captured.update(kw)
        return _FakeProc(pid=999)

    monkeypatch.setattr(_chaos.subprocess, "Popen", _fake_popen)
    runner = _chaos.SoakRunner(repo_root=str(tmp_path), wall_seconds=10)
    runner.launch({}, str(tmp_path / "run"))

    assert captured.get("start_new_session") is True  # own process group


# ---------------------------------------------------------------------------
# stop -> group SIGTERM, escalate to group SIGKILL
# ---------------------------------------------------------------------------

def test_stop_group_terms_then_escalates_sigkill(monkeypatch):
    killpg = []
    monkeypatch.setattr(_chaos.os, "killpg", lambda pgid, sig: killpg.append((pgid, sig)))
    monkeypatch.setattr(_chaos.os, "getpgid", lambda pid: pid)
    monkeypatch.setenv("JARVIS_A1_SOAK_STOP_GRACE_S", "0.01")

    runner = _chaos.SoakRunner()
    runner._proc = _FakeProc(pid=4242, exits_after_sigterm=False)  # never yields
    runner.stop()

    assert (4242, signal.SIGTERM) in killpg   # whole group, not just parent
    assert (4242, signal.SIGKILL) in killpg   # escalation when it won't yield


def test_stop_group_term_only_when_it_yields(monkeypatch):
    killpg = []
    monkeypatch.setattr(_chaos.os, "killpg", lambda pgid, sig: killpg.append((pgid, sig)))
    monkeypatch.setattr(_chaos.os, "getpgid", lambda pid: pid)
    monkeypatch.setenv("JARVIS_A1_SOAK_STOP_GRACE_S", "5")

    runner = _chaos.SoakRunner()
    proc = _FakeProc(pid=77, exits_after_sigterm=True)
    runner._proc = proc

    # Mark term-seen so wait() returns 0 (the group yielded to SIGTERM).
    def _killpg(pgid, sig):
        killpg.append((pgid, sig))
        if sig == signal.SIGTERM:
            proc._term_seen = True

    monkeypatch.setattr(_chaos.os, "killpg", _killpg)
    runner.stop()

    assert (77, signal.SIGTERM) in killpg
    assert (77, signal.SIGKILL) not in killpg  # no escalation needed


def test_stop_noop_when_already_dead(monkeypatch):
    killpg = []
    monkeypatch.setattr(_chaos.os, "killpg", lambda pgid, sig: killpg.append((pgid, sig)))

    class _Dead:
        pid = 1

        def poll(self):
            return 0

    runner = _chaos.SoakRunner()
    runner._proc = _Dead()
    runner.stop()

    assert killpg == []


# ---------------------------------------------------------------------------
# driver registry reap (finally + atexit + signal)
# ---------------------------------------------------------------------------

def test_reap_soak_runners_stops_and_clears():
    stopped = []

    class _R:
        def stop(self):
            stopped.append(True)

    _drv._ACTIVE_SOAK_RUNNERS.clear()
    _drv._ACTIVE_SOAK_RUNNERS.append(_R())
    _drv._reap_soak_runners()

    assert stopped == [True]
    assert _drv._ACTIVE_SOAK_RUNNERS == []


def test_reap_soak_runners_never_raises():
    class _Bad:
        def stop(self):
            raise RuntimeError("worker wedged")

    _drv._ACTIVE_SOAK_RUNNERS.clear()
    _drv._ACTIVE_SOAK_RUNNERS.append(_Bad())
    _drv._reap_soak_runners()  # must not raise

    assert _drv._ACTIVE_SOAK_RUNNERS == []


# ---------------------------------------------------------------------------
# Fast-Fail: driver detects the global L4 capacity wall marker
# ---------------------------------------------------------------------------

def test_hardware_capacity_exhausted_detects_marker(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text("...\n[GCPComputeRest] HARDWARE_CAPACITY_EXHAUSTED: L4 stockout ...\n...")
    assert _drv._hardware_capacity_exhausted(str(log)) is True


def test_hardware_capacity_exhausted_absent(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text("[GCPComputeRest] instances.insert ok node=x zone=us-west1-a\n")
    assert _drv._hardware_capacity_exhausted(str(log)) is False


def test_hardware_capacity_exhausted_missing_file_is_false():
    assert _drv._hardware_capacity_exhausted("/no/such/debug.log") is False
    assert _drv._hardware_capacity_exhausted(None) is False
