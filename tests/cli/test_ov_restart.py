"""One command to reload the organism, without hunting a pid.

The operator's actual pain: `ps`, read a pid, `kill <pid>`, `ov`. Four steps
and a number to transcribe, performed most often when a daemon is stale and
least often when there is patience for it.

Their proposed fix was "Ctrl+C should kill it". That would break the property
the whole design rests on — the organism is PROACTIVE, runs sensors and
autonomous operations, and detaching is meant to leave it working. Killing on
Ctrl+C ends a long soak every time someone glances at it. So the persistence
stays and this is the escape hatch: an explicit verb, surfaced exactly where
an operator discovers they need it.
"""
from __future__ import annotations

import contextlib
import io
import os
import signal
from pathlib import Path
from typing import Any, List

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _ov():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli import ov
    return ov


# --------------------------------------------------------------------------
# 1. routing
# --------------------------------------------------------------------------

def test_restart_is_a_first_class_verb() -> None:
    ov = _ov()
    assert ov.resolve(["restart"]).action == "restart"
    assert "restart" in ov._VERBS


def test_the_other_verbs_are_unchanged() -> None:
    ov = _ov()
    for argv, action in ((["status"], "status"), ([], "cockpit"),
                         (["doctor"], "doctor"), (["attach"], "attach")):
        assert ov.resolve(argv).action == action


def test_restart_reuses_the_cockpit_ignition_path() -> None:
    """DRY, and it matters: a second ignition path would drift from `ov`'s in
    how it waits for the socket, which is where this codebase has been bitten
    before."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    branch = src.split('if inv.action == "restart":')[1][:700]
    assert 'Invocation("cockpit"' in branch, (
        "restart does not fall through to the shared boot path"
    )


# --------------------------------------------------------------------------
# 2. stopping is graceful first
# --------------------------------------------------------------------------

def test_it_asks_politely_before_it_insists(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM lets the harness write its summary, flush telemetry and
    release its lock — which is what makes the NEXT boot clean."""
    ov = _ov()
    signals: List[Any] = []
    monkeypatch.setattr(ov, "_live_incumbent", lambda: 4242)
    monkeypatch.setattr(ov.os, "kill",
                        lambda pid, sig: signals.append(sig) or
                        (_ for _ in ()).throw(ProcessLookupError()))
    said: List[str] = []
    assert ov._restart_daemon(said.append) == 0
    assert signals and signals[0] == signal.SIGTERM


def test_it_waits_for_the_process_to_actually_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Igniting while the old one still holds the single-flight lock is how
    two organisms race for one socket."""
    ov = _ov()
    monkeypatch.setenv("JARVIS_OV_RESTART_GRACE_S", "2")
    monkeypatch.setattr(ov, "_live_incumbent", lambda: 4242)
    probes = {"n": 0}

    def _kill(pid: int, sig: int) -> None:
        if sig == 0:                       # liveness probe
            probes["n"] += 1
            if probes["n"] < 3:
                return                     # still alive
            raise ProcessLookupError()

    monkeypatch.setattr(ov.os, "kill", _kill)
    said: List[str] = []
    assert ov._restart_daemon(said.append) == 0
    assert probes["n"] >= 2, "it did not poll for the process to exit"
    assert any("stopped" in s for s in said)


def test_a_daemon_that_will_not_leave_is_escalated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ov = _ov()
    monkeypatch.setenv("JARVIS_OV_RESTART_GRACE_S", "1")
    monkeypatch.setattr(ov, "_live_incumbent", lambda: 4242)
    sent: List[int] = []
    monkeypatch.setattr(ov.os, "kill", lambda pid, sig: sent.append(sig))
    said: List[str] = []
    ov._restart_daemon(said.append)
    assert signal.SIGKILL in sent
    assert any("SIGKILL" in s for s in said), "escalation was silent"


def test_nothing_running_still_ignites(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ov restart` on a cold machine must boot, not error."""
    ov = _ov()
    monkeypatch.setattr(ov, "_live_incumbent", lambda: None)
    said: List[str] = []
    assert ov._restart_daemon(said.append) == 0
    assert any("igniting" in s for s in said)


def test_someone_elses_daemon_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PermissionError means the pid is not ours. Escalating to SIGKILL
    would be no more permitted and reporting success would be a lie."""
    ov = _ov()
    monkeypatch.setattr(ov, "_live_incumbent", lambda: 1)

    def _denied(pid: int, sig: int) -> None:
        raise PermissionError()

    monkeypatch.setattr(ov.os, "kill", _denied)
    said: List[str] = []
    assert ov._restart_daemon(said.append) == 1
    assert any("not permitted" in s for s in said)


def test_the_grace_period_is_tunable_but_never_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero would SIGKILL instantly, cutting the summary write that makes the
    next boot clean."""
    ov = _ov()
    monkeypatch.setenv("JARVIS_OV_RESTART_GRACE_S", "0")
    assert ov._restart_grace_s() >= 1.0
    monkeypatch.setenv("JARVIS_OV_RESTART_GRACE_S", "junk")
    assert ov._restart_grace_s() == 15.0


# --------------------------------------------------------------------------
# 3. Ctrl+C still detaches — the persistence is the feature
# --------------------------------------------------------------------------

def test_ctrl_c_does_not_kill_the_organism() -> None:
    """The operator asked for this and it is the wrong mechanism: a proactive
    organism that dies whenever someone glances at it cannot run a soak."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "Ctrl+C detaches (the organism " in src
    assert "keeps running)" in src
    restart_src = src.split("def _restart_daemon")[1][:1200]
    assert "NOT bound to Ctrl+C" in restart_src, (
        "the reason this is a verb rather than a signal handler is undocumented"
    )


# --------------------------------------------------------------------------
# 4. it is discoverable at the moment it is needed
# --------------------------------------------------------------------------

def test_the_staleness_banner_names_the_command() -> None:
    """A banner that says 'restart to load current code' tells an operator
    what to WANT. This tells them what to TYPE."""
    import json
    import tempfile
    import time

    from backend.core.ouroboros.battle_test.daemon_provenance import (
        staleness_line,
    )
    stamp = Path(tempfile.mkdtemp()) / "p.json"
    stamp.write_text(json.dumps({
        "pid": 1, "booted_at": time.time() - 7200, "commit": "0" * 40,
    }))
    assert "ov restart" in staleness_line(stamp)


def test_the_attach_hint_teaches_it_without_a_stale_daemon() -> None:
    """Otherwise it is only ever learned in the moment of frustration."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "`ov restart` reloads it" in src
