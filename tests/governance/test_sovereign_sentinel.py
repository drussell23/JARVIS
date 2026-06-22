"""Unit tests for the Sovereign Telemetry Sentinel pure logic (no live gcloud)."""
from __future__ import annotations

import argparse
import json
import time

import pytest

import importlib.util
import pathlib
import sys

# Load the script module by path (it lives under scripts/, not a package).
_SPEC = importlib.util.spec_from_file_location(
    "sovereign_sentinel",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "sovereign_sentinel.py",
)
ss = importlib.util.module_from_spec(_SPEC)
# Register BEFORE exec so dataclass field-type resolution (which reads
# sys.modules[cls.__module__]) works under `from __future__ import annotations`.
sys.modules["sovereign_sentinel"] = ss
_SPEC.loader.exec_module(ss)  # type: ignore[union-attr]


def _ns(**over):
    base = dict(node="n", zone="", project="", container="",
               boot_timeout="", stall_timeout="", no_auto_kill=False,
               dry_run_kill=False, autopsy_dir="")
    base.update(over)
    return argparse.Namespace(**base)


def _cfg(**over):
    cfg = ss.SentinelConfig.build(_ns())
    return ss.dataclasses.replace(cfg, **over)


# -- EventMatcher ----------------------------------------------------------- #
@pytest.mark.parametrize("line,kind,success,fatal", [
    ("WARNING [A1Trace] accept op=op-1", "DISPATCH", False, False),
    ("Advisor BLOCKED op=GOAL-001 (zero coverage)", "ADVISOR_BLOCK", False, False),
    ("[Orchestrator] BLOCK decomposed into 3 sub-goals", "DECOMPOSE", False, False),
    ("raised LocalEgressOverweightError attempted=900000", "EGRESS_BLOCK", False, False),
    ("[SOVEREIGN YIELD] op=x lineage=y stalled reduction", "SOVEREIGN_YIELD", False, False),
    ("created Pull request #69661 ouroboros/review/op-1", "CONVERGENCE", True, False),
    ("Traceback (most recent call last):", "FATAL", False, True),
])
def test_classify(line, kind, success, fatal):
    ev = ss.EventMatcher().classify(line)
    assert ev is not None and ev.kind == kind
    assert ev.is_success is success and ev.is_fatal is fatal


def test_classify_noise_returns_none():
    assert ss.EventMatcher().classify("just some normal info line") is None


def test_extra_patterns_env(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_EXTRA_PATTERNS", "CUSTOM=my_special_marker")
    ev = ss.EventMatcher().classify("here is my_special_marker firing")
    assert ev is not None and ev.kind == "CUSTOM" and ev.is_transition is True


# -- boot / stall verdict (pure) -------------------------------------------- #
def test_boot_timeout_fires_with_no_logs():
    s = ss.Sentinel(_cfg(boot_timeout_s=100.0))
    s._started_at = 0.0
    s._first_line_at = None
    assert s.stall_verdict(now=50.0) is None
    assert "boot_timeout" in (s.stall_verdict(now=150.0) or "")


def test_stall_timeout_fires_after_first_line():
    s = ss.Sentinel(_cfg(stall_timeout_s=200.0))
    s._started_at = 0.0
    s._first_line_at = 10.0
    s._last_transition_at = None
    assert s.stall_verdict(now=150.0) is None              # within window of first_line
    assert "stall_timeout" in (s.stall_verdict(now=300.0) or "")


def test_transition_resets_stall():
    s = ss.Sentinel(_cfg(stall_timeout_s=200.0))
    s._started_at = 0.0
    s._first_line_at = 10.0
    ev = ss.EventMatcher().classify("[SOVEREIGN YIELD] self-heal")
    s.note(ev, now=290.0)                                   # a transition at 290
    assert s.stall_verdict(now=300.0) is None               # reset -> within window
    assert "stall_timeout" in (s.stall_verdict(now=500.0) or "")


def test_convergence_suppresses_kill():
    s = ss.Sentinel(_cfg(stall_timeout_s=10.0))
    s._started_at = 0.0
    s._first_line_at = 1.0
    ev = ss.EventMatcher().classify("Pull request #1 ouroboros/review")
    s.note(ev, now=2.0)
    assert s._converged is True
    assert s.stall_verdict(now=99999.0) is None             # converged -> never auto-kill


def test_auto_kill_env_off(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_AUTO_KILL", "false")
    assert ss.SentinelConfig.build(_ns()).auto_kill is False


# -- Autopsy Protocol (the Black Box) --------------------------------------- #
def test_sudo_in_log_stream_command():
    """The log stream MUST use sudo (the ssh user is not in the docker group)."""
    cfg = _cfg()
    argv = ss.LogStream(cfg)._ssh_argv()
    assert any("sudo docker logs" in a for a in argv)


def test_autopsy_extract_writes_blackbox(tmp_path):
    """extract() writes docker_logs + fsm_ledgers + an FSM-stamped manifest."""
    import asyncio

    cfg = _cfg(autopsy_dir=str(tmp_path))
    ex = ss.AutopsyExtractor(cfg)

    async def _fake_ssh(remote_cmd, timeout_s):
        return "LEDGER-OR-LOG-CONTENT" if "docker" in remote_cmd else "x"

    ex._ssh = _fake_ssh  # type: ignore[method-assign]
    out = asyncio.run(ex.extract(
        fsm_state="ADVISOR_BLOCK", reason="stall_timeout (900s)",
        counts={"ADVISOR_BLOCK": 5, "DISPATCH": 9}, idle_s=901.0,
    ))
    assert out is not None and out.exists()
    assert "ADVISOR_BLOCK" in out.name          # FSM-stamped dir
    assert (out / "docker_logs.txt").read_text() == "LEDGER-OR-LOG-CONTENT"
    assert (out / "fsm_ledgers.txt").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["stuck_fsm_state"] == "ADVISOR_BLOCK"
    assert manifest["transition_counts"]["ADVISOR_BLOCK"] == 5
    assert "stall_timeout" in manifest["teardown_reason"]


def test_autopsy_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_AUTOPSY_ENABLED", "false")
    import asyncio
    cfg = ss.SentinelConfig.build(_ns(autopsy_dir=str(tmp_path)))
    out = asyncio.run(ss.AutopsyExtractor(cfg).extract(
        fsm_state="boot", reason="x", counts={}, idle_s=0.0))
    assert out is None


def test_autopsy_runs_BEFORE_kill_and_kill_proceeds_on_autopsy_failure(tmp_path):
    """Order invariant: autopsy extracts BEFORE teardown; and a failing autopsy
    still lets the kill proceed (billing must always stop)."""
    import asyncio

    order = []

    class _FakeAutopsy:
        async def extract(self, **kw):
            order.append("autopsy")
            raise RuntimeError("boom")          # autopsy FAILS

    class _FakeController:
        async def teardown(self, reason):
            order.append("kill")

    s = ss.Sentinel(_cfg(autopsy_dir=str(tmp_path)),
                    controller=_FakeController(), autopsy=_FakeAutopsy())
    s._started_at = 0.0
    s._last_kind = "DECOMPOSE"
    asyncio.run(s._teardown_and_stop("stall_timeout"))
    assert order == ["autopsy", "kill"]         # autopsy first, kill still ran
    assert s.verdict.startswith("killed:")


def test_fsm_state_stamp_tracks_last_transition():
    s = ss.Sentinel(_cfg())
    s.note(ss.EventMatcher().classify("[A1Trace] accept"), now=1.0)
    s.note(ss.EventMatcher().classify("BLOCK decomposed into 2"), now=2.0)
    assert s._last_kind == "DECOMPOSE"
