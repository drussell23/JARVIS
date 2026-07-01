"""Tests for the A1 agentic cognitive-loop watchdog detector."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, name + ".py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_wd = _load("a1_agentic_watchdog")


def test_identical_tool_call_loop_trips_at_limit():
    w = _wd.AgenticWatchdog(identical_tool_limit=3)
    line = "[Venom] executing read_file path=backend/x.py"
    assert w.observe(line) is None
    assert w.observe(line) is None
    reason = w.observe(line)  # 3rd identical -> trip
    assert reason and "identical_tool_call" in reason


def test_different_args_do_not_trip():
    w = _wd.AgenticWatchdog(identical_tool_limit=3)
    assert w.observe("tool=read_file path=a.py") is None
    assert w.observe("tool=read_file path=b.py") is None
    assert w.observe("tool=read_file path=c.py") is None  # different args -> no trip


def test_retry_stall_trips_without_new_exploration():
    w = _wd.AgenticWatchdog(retry_stall_limit=3)
    assert w.observe("phase=GENERATE_RETRY op=op-1") is None
    assert w.observe("phase=GENERATE_RETRY op=op-1") is None
    reason = w.observe("phase=GENERATE_RETRY op=op-1")  # 3rd retry, no exploration -> trip
    assert reason and "retry_stall" in reason


def test_exploration_resets_retry_stall():
    w = _wd.AgenticWatchdog(retry_stall_limit=3)
    assert w.observe("phase=GENERATE_RETRY op=op-1") is None
    assert w.observe("phase=GENERATE_RETRY op=op-1") is None
    # A real exploration call clears the stall counter.
    assert w.observe("tool=search_code query=_format_age") is None
    assert w.observe("phase=GENERATE_RETRY op=op-1") is None
    assert w.observe("phase=GENERATE_RETRY op=op-1") is None  # only 2 since explore -> no trip


def test_milestones_streamed():
    w = _wd.AgenticWatchdog()
    w.observe("[A1Trace] emit goal=GOAL-001 source=roadmap")
    w.observe("[CandidateGenerator] Phase 3c DAG re-entry: routed generation to the awakened 32B endpoint=x")
    w.observe("tool=read_file path=backend/x.py")
    blob = " | ".join(w.milestones)
    assert "GOAL emitted" in blob
    assert "Routed GENERATE" in blob
    assert "File Read" in blob


def test_healthy_progression_never_trips():
    w = _wd.AgenticWatchdog()
    for line in [
        "[A1Trace] emit goal=g source=roadmap",
        "tool=read_file path=a.py",
        "tool=search_code query=foo",
        "phase=GENERATE_RETRY op=op-1",
        "tool=read_file path=b.py",   # new exploration resets stall
        "[PhaseRunnerDelegate] APPROVE+APPLY+VERIFY op=op-1",
        "LEDGER_TERMINAL op_id=op-1 state=applied",
    ]:
        assert w.observe(line) is None
