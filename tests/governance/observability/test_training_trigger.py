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
        if k.startswith(("JARVIS_GRPO_AUTOTRAIN", "TRINITY_GRPO")):
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
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: None)
    v = _fire(stop_reason="wall_clock_cap+atexit_fallback")
    assert v["reason"] == "preflight_command_unresolved"  # got PAST gate 2


def test_graceful_set_is_configurable(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setenv(tt._ENV_GRACEFUL, "my_custom_stop")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: None)
    assert _fire(stop_reason="my_custom_stop")["reason"] == "preflight_command_unresolved"
    assert _fire(stop_reason="wall_clock_cap")["reason"].startswith(
        "stop_reason_not_graceful")


# --------------------------------------------------------------------------
# Gate 3 — the corpus
# --------------------------------------------------------------------------

def _fake_run(rc: int, out: str):
    async def _r(cmd, *, timeout_s, cwd=None, env=None):
        return rc, out
    return _r


def test_refusal_is_not_an_error(monkeypatch) -> None:
    """Exit 2 means 'I looked and there is nothing to learn from'.

    It must be distinguishable from a fault, or an operator cannot tell a
    healthy corpus-gate refusal from a broken preflight -- and would go
    hunting a bug that is not there.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_run", _fake_run(
        2, json.dumps({"trainable_groups": 0, "flat_groups": 19})))
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False
    assert v["reason"] == "corpus_not_trainable"
    assert v["preflight"]["flat_groups"] == 19   # the report survives


def test_preflight_error_is_distinct_from_refusal(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_run", _fake_run(1, "boom"))
    v = _fire(stop_reason="wall_clock_cap")
    assert v["reason"] == "preflight_error:rc=1"


def test_unparseable_preflight_output_still_refuses_cleanly(monkeypatch) -> None:
    """rc is the answer; the JSON is a bonus. Garbage must not raise."""
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_run", _fake_run(2, "not json at all"))
    v = _fire(stop_reason="wall_clock_cap")
    assert v["reason"] == "corpus_not_trainable"
    assert "raw" in v["preflight"]


# --------------------------------------------------------------------------
# Gate 4 — the device
# --------------------------------------------------------------------------

def test_busy_card_refuses_rather_than_ooms(monkeypatch) -> None:
    """ollama holds ~21.8 GiB for 1800s after a soak.

    Launching into that measures, and fails on, whatever is left -- and the
    failure reads as 'the model does not fit' rather than 'something else
    was resident'.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setenv(tt._ENV_EVICT_WAIT, "0")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_run", _fake_run(0, "{}"))

    async def _busy():
        return 1200
    monkeypatch.setattr(tt, "_gpu_free_mib", _busy)
    monkeypatch.setattr(tt, "_evict_local_model", lambda: asyncio.sleep(0))

    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False
    assert v["reason"].startswith("gpu_busy:")
    assert v["gpu_free_mib"] == 1200


def test_no_gpu_is_not_a_refusal(monkeypatch) -> None:
    """A CPU-bound or remote trainer is legitimate.

    'I cannot measure the card' must not be read as 'the card is busy', or
    the trigger can never fire on a host without nvidia-smi.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_train_cmd", lambda: ["echo", "trained"])
    monkeypatch.setattr(tt, "_run", _fake_run(0, "{}"))

    async def _none():
        return None
    monkeypatch.setattr(tt, "_gpu_free_mib", _none)
    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is True


def test_free_threshold_is_configurable(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setenv(tt._ENV_FREE_MIB, "1000")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_train_cmd", lambda: ["echo", "ok"])
    monkeypatch.setattr(tt, "_run", _fake_run(0, "{}"))

    async def _some():
        return 1200
    monkeypatch.setattr(tt, "_gpu_free_mib", _some)
    assert _fire(stop_reason="wall_clock_cap")["fired"] is True


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


# --------------------------------------------------------------------------
# Resilience — the hook must never be why a session cannot shut down
# --------------------------------------------------------------------------

def test_train_launch_failure_is_contained(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["echo"])
    monkeypatch.setattr(tt, "_train_cmd", lambda: ["definitely-not-a-binary"])

    async def _ok(cmd, *, timeout_s, cwd=None, env=None):
        if cmd[0] == "echo":
            return 0, "{}"
        raise FileNotFoundError("no such binary")
    monkeypatch.setattr(tt, "_run", _ok)

    async def _free():
        return 99000
    monkeypatch.setattr(tt, "_gpu_free_mib", _free)

    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is False
    assert v["reason"].startswith("train_launch_failed:FileNotFoundError")


def test_nonzero_training_exit_is_reported_not_raised(monkeypatch) -> None:
    """A failed training run is telemetry, not an exception.

    It must not propagate into the harness teardown it is called from.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_MASTER, "true")
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["preflight"])
    monkeypatch.setattr(tt, "_train_cmd", lambda: ["train"])

    # The preflight must PASS and only the training must fail; a single
    # fake returning rc=1 for both never reaches the training call at all.
    async def _by_stage(cmd, *, timeout_s, cwd=None, env=None):
        return (0, "{}") if cmd[0] == "preflight" else (1, "CUDA out of memory")
    monkeypatch.setattr(tt, "_run", _by_stage)

    async def _free():
        return 99000
    monkeypatch.setattr(tt, "_gpu_free_mib", _free)

    v = _fire(stop_reason="wall_clock_cap")
    assert v["fired"] is True and v["reason"] == "train_rc=1"
    assert "CUDA out of memory" in v["tail"]


def test_discovery_returns_none_rather_than_guessing(monkeypatch) -> None:
    """A missing repo must be a clean refusal, never a wrong path."""
    _clear(monkeypatch)
    monkeypatch.setenv(tt._ENV_REACTOR_ROOT, "/nonexistent/reactor")
    assert tt._reactor_root() is None
    monkeypatch.setenv(tt._ENV_TRAIN_PY, "/nonexistent/python")
    assert tt._reactor_python() is None
