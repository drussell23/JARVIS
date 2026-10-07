"""The auto-train trigger is judged by what it REFUSES.

An automated trainer that fires whenever a soak ends is worse than none:
the measured corpus after five soaks had 19 multi-response groups whose
reward spread was exactly 0.0, so a run would have burned an hour of GPU
to produce a checkpoint trained on nothing -- and looked successful doing
it. Most of these tests assert a refusal, and each one names the specific
way firing would have been wrong.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.observability import training_trigger as tt


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clear(monkeypatch) -> None:
    for k in list(os.environ):
        if k.startswith(("JARVIS_GRPO_AUTOTRAIN", "TRINITY_GRPO", "JARVIS_TRAINING_HANDOFF")):
            monkeypatch.delenv(k, raising=False)


def _fire(**kw):
    return asyncio.run(tt.maybe_train_after_soak(**kw))


# --------------------------------------------------------------------------
# Gate 1 — the master flag
# --------------------------------------------------------------------------

def test_disabled_by_default(monkeypatch) -> None:
    """§33.1 shadow-first. Absent config must mean OFF, not 'probably fine'."""
    _clear(monkeypatch)
    assert tt.autotrain_enabled() is False
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False and v["reason"] == "disabled"


def test_disabled_short_circuits_before_any_subprocess(monkeypatch) -> None:
    """The cheapest gate must be the first one.

    If the flag were checked after preflight, every soak on every box would
    pay a subprocess to be told 'no'.
    """
    _clear(monkeypatch)
    called = []
    monkeypatch.setattr(tt, "_run", lambda *a, **k: called.append(a))
    v = _fire(stop_reason="wall_clock_cap")
    assert called == [] and v["reason"] == "disabled"


# --------------------------------------------------------------------------
# Gate 2 — termination class
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "crashed", "signal:SIGKILL", "unhandled_exception", "",
])
def test_ungraceful_stop_refuses(monkeypatch, reason) -> None:
    """A killed session's corpus is of unknown completeness.

    The flush that makes it complete runs in the same teardown this hook is
    part of; if the session died before it, training would read a corpus
    missing every in-flight trajectory and never know.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    v = _fire(stop_reason=reason)
    assert v["fired"] is False
    assert v["reason"].startswith("stop_reason_not_graceful")


def test_composed_stop_reason_is_recognised(monkeypatch) -> None:
    """The harness composes them: 'wall_clock_cap+atexit_fallback'.

    An equality check would reject every real graceful shutdown this
    harness produces, so the match is by substring.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    _yield(monkeypatch, met=True)
    v = _fire(stop_reason="wall_clock_cap+atexit_fallback")
    assert "JARVIS_TRAINING_HANDOFF_LAUNCH_CMD" in v["reason"]  # got PAST gate 2, to the request


def test_graceful_set_is_configurable(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setenv(tt._ENV_GRACEFUL, "my_custom_stop")
    _yield(monkeypatch, met=True)
    assert "JARVIS_TRAINING_HANDOFF_LAUNCH_CMD" in _fire(stop_reason="my_custom_stop")["reason"]
    assert _fire(stop_reason="wall_clock_cap")["reason"].startswith(
        "stop_reason_not_graceful")


# --------------------------------------------------------------------------
# The request -- teardown ASKS for a cycle; it never runs one
# --------------------------------------------------------------------------

def _yield(monkeypatch, *, met, unlearned=0, threshold=15, error=None):
    from backend.core.ouroboros.governance.observability import training_handoff as th

    async def y(model=None):
        d = {"met": met, "unlearned": unlearned, "threshold": threshold}
        if error:
            d["error"] = error
        return d
    monkeypatch.setattr(th, "training_yield", y)


def test_below_the_batch_threshold_never_requests_a_cycle(monkeypatch) -> None:
    """Hours of inference uptime are not spent on a micro-batch: a clean end
    with too little NEW landed evidence returns at once."""
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    from backend.core.ouroboros.governance.observability import training_handoff as th
    calls = []
    monkeypatch.setattr(th, "request_cycle", lambda **kw: calls.append(kw) or {"requested": True})
    _yield(monkeypatch, met=False, unlearned=4)
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False and v["reason"] == "below_training_batch:4<15" and calls == []


def test_an_unanswerable_yield_is_not_met(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    from backend.core.ouroboros.governance.observability import training_handoff as th
    monkeypatch.setattr(th, "request_cycle", lambda **kw: {"requested": True})
    _yield(monkeypatch, met=False, error="J-Prime unreachable")
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False and "J-Prime unreachable" in v["reason"]


def test_qualifying_stop_requests_a_detached_cycle(monkeypatch) -> None:
    """The cycle is hours long; it must never run inside the organism's
    teardown (2026-10-07: the stretched shutdown was SIGKILLed by the
    independent out-of-process watchdog)."""
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    from backend.core.ouroboros.governance.observability import training_handoff as th
    calls = []
    monkeypatch.setattr(th, "request_cycle", lambda **kw: calls.append(kw) or {"requested": True})
    _yield(monkeypatch, met=True, unlearned=18)
    v = _fire(stop_reason="wall_clock_cap", session_id="bt-1")
    assert v["fired"] is True and v["reason"] == "requested"
    # The requester names itself: it is still alive while the cycle starts.
    assert calls == [{"trigger": "session_end:bt-1", "requester_pid": os.getpid(), "release_within_s": None}]


def test_refused_request_is_reported_not_raised(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    from backend.core.ouroboros.governance.observability import training_handoff as th
    monkeypatch.setattr(th, "request_cycle",
                        lambda **kw: {"requested": False, "reason": "a cycle is in progress (TRAINING)"})
    _yield(monkeypatch, met=True)
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False and "in progress" in v["reason"]


def test_request_crash_is_contained(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    from backend.core.ouroboros.governance.observability import training_handoff as th

    def boom(**kw):
        raise OSError("powershell.exe vanished")
    monkeypatch.setattr(th, "request_cycle", boom)
    _yield(monkeypatch, met=True)
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False and v["reason"] == "request_failed:OSError"


# --------------------------------------------------------------------------
# Orphan safety — the part that costs the NEXT soak if it is wrong
# --------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_timeout_reaps_the_whole_process_group() -> None:
    """A trainer forks workers and holds CUDA contexts.

    Killing only the direct child leaves those resident, and the next soak
    then fails to load a model for reasons that have nothing to do with it.
    The child here spawns a grandchild that would outlive a naive kill.
    """
    marker = Path(os.environ.get("TMPDIR", "/tmp")) / f"orphan_{os.getpid()}.txt"
    marker.unlink(missing_ok=True)
    script = (
        "import os,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',\"import time;open(r'{marker}','w').write('alive');time.sleep(30)\"]);"
        "time.sleep(30)"
    )
    rc, out = asyncio.run(tt._run([sys.executable, "-c", script], timeout_s=2.0))
    assert rc == 124 and "reaped" in out
    time.sleep(1.0)
    # the grandchild must not still be running
    if marker.exists():
        # it started; confirm nothing in that group survived
        import subprocess
        ps = subprocess.run(["ps", "-eo", "cmd"], capture_output=True, text=True)
        assert str(marker) not in ps.stdout, "grandchild survived the group kill"
    marker.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_run_returns_output_and_code_on_normal_exit() -> None:
    rc, out = asyncio.run(tt._run(
        [sys.executable, "-c", "print('hello'); raise SystemExit(3)"],
        timeout_s=30.0))
    assert rc == 3 and "hello" in out


def test_discovery_returns_none_rather_than_guessing(monkeypatch) -> None:
    """A missing repo must be a clean refusal, never a wrong path."""
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_REACTOR_ROOT, "/nonexistent/reactor")
    assert tt._reactor_root() is None
    monkeypatch.setenv(tt._ENV_TRAIN_PY, "/nonexistent/python")
    assert tt._reactor_python() is None


# ---------------------------------------------------------------------------
# _run(log_path=...): a trainer's output is watchable live and survives a kill
# ---------------------------------------------------------------------------

def test_log_path_streams_while_the_child_runs(tmp_path) -> None:
    log = tmp_path / "train.log"
    child = ("import sys,time,pathlib;print('step 1',flush=True);"
             f"p=pathlib.Path({str(tmp_path / 'go')!r})\n"
             "while not p.exists(): time.sleep(0.05)\nprint('step 2')")

    async def scenario():
        task = asyncio.create_task(tt._run([sys.executable, "-c", child], timeout_s=30, log_path=log))
        for _ in range(200):
            if log.exists() and "step 1" in log.read_text():
                break
            await asyncio.sleep(0.05)
        live = log.read_text()
        (tmp_path / "go").touch()
        return live, await task

    live, (rc, out) = asyncio.run(scenario())
    assert "step 1" in live and "step 2" not in live
    assert rc == 0 and "step 1" in out and "step 2" in out
    assert "step 2" in log.read_text()


def test_log_path_keeps_the_output_a_timeout_used_to_discard(tmp_path) -> None:
    log = tmp_path / "train.log"
    child = "import time;print('step 7 loss=0.1',flush=True);time.sleep(60)"
    rc, out = asyncio.run(tt._run([sys.executable, "-c", child], timeout_s=1.5, log_path=log))
    assert rc == 124 and out.startswith("timeout after")
    assert "step 7 loss=0.1" in out and "step 7 loss=0.1" in log.read_text()


def test_without_log_path_output_is_returned_as_before(tmp_path) -> None:
    rc, out = asyncio.run(tt._run([sys.executable, "-c", "print('hi')"], timeout_s=30))
    assert rc == 0 and out.strip() == "hi"
