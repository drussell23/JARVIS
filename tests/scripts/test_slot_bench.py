"""slot_bench: the admission window, the ledger fold, and resume.

The live parts (worktrees, signing, the soak) are exercised by a smoke run;
these pin the arithmetic that decides what each arm is given and when an arm
is finished, which must be identical for every slot count.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts/soak/slot_bench.py"


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("slot_bench_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BENCH_ROOT", tmp_path)
    return mod


def _state(ids, window=2):
    return {
        "run": "r", "window": window, "task_ids": list(ids), "arm_seconds": 100,
        "current": 0,
        "arms": [{"index": 0, "slots": 3, "status": "running", "active_s": 0.0,
                  "sessions": [], "restarts": 0,
                  "goals": {g: {"state": "queued", "interrupted": 0} for g in ids}}],
    }


def _rec(event, goal, op="", sha=""):
    return SimpleNamespace(event=event, goal_id=goal, op_id=op, commit_sha=sha)


def test_window_is_the_first_unfinished_tasks_in_list_order(sb):
    st = _state(["a", "b", "c", "d"])
    arm = st["arms"][0]
    assert sb.window_ids(st, arm) == ["a", "b"]
    arm["goals"]["a"]["state"] = "terminal"
    arm["goals"]["c"]["state"] = "satisfied"
    assert sb.window_ids(st, arm) == ["b", "d"]


def test_ledger_fold_dispatch_then_terminal_or_satisfied(sb):
    st = _state(["a", "b"])
    arm = st["arms"][0]
    assert sb.apply_ledger(st, arm, [_rec("dispatched", "a", "op1")], 1.0)
    assert arm["goals"]["a"]["state"] == "dispatched"
    # a terminal for some OTHER op of the same goal is not this attempt's end
    assert not sb.apply_ledger(st, arm, [_rec("terminal", "a", "op0")], 2.0)
    assert sb.apply_ledger(st, arm, [_rec("terminal", "a", "op1")], 3.0)
    assert arm["goals"]["a"]["state"] == "terminal"
    sb.apply_ledger(st, arm, [_rec("satisfied", "b", "op2", "abc")], 4.0)
    assert arm["goals"]["b"] == {**arm["goals"]["b"], "state": "satisfied", "sha": "abc"}
    # a finished task is never reopened by a later re-dispatch
    sb.apply_ledger(st, arm, [_rec("dispatched", "a", "op9")], 5.0)
    assert arm["goals"]["a"]["state"] == "terminal"


def test_resumed_attempt_ignores_the_dead_op(sb):
    st = _state(["a"])
    arm = st["arms"][0]
    arm["goals"]["a"].update(state="queued", dead_ops=["op1"])
    assert not sb.apply_ledger(st, arm, [_rec("dispatched", "a", "op1")], 1.0)
    assert sb.apply_ledger(st, arm, [_rec("dispatched", "a", "op2")], 2.0)


def test_a_task_never_dispatched_stalls_by_the_same_rule_everywhere(sb, monkeypatch):
    monkeypatch.setattr(sb, "STALL_S", 10.0)
    st = _state(["a", "b"])
    arm = st["arms"][0]
    assert not sb.mark_stalls(arm, ["a", "b"], 100.0)
    arm["goals"]["b"]["state"] = "dispatched"
    assert sb.mark_stalls(arm, ["a", "b"], 111.0)
    assert arm["goals"]["a"]["state"] == "stalled"
    assert arm["goals"]["b"]["state"] == "dispatched"


def test_checkpoint_is_atomic_and_readable(sb, tmp_path):
    st = _state(["a"])
    (tmp_path / "r").mkdir()
    sb.save_state(st)
    assert sb.load_state("r") == json.loads(json.dumps(st))
    assert not (tmp_path / "r" / "state.json.tmp").exists()


def test_summary_counts_and_rate(sb):
    st = _state(["a", "b", "c"])
    arm = st["arms"][0]
    arm["active_s"] = 1800.0
    arm["goals"]["a"]["state"] = "satisfied"
    arm["goals"]["b"]["state"] = "terminal"
    arm["goals"]["c"]["interrupted"] = 1
    s = sb.summarize_arm(st, arm)
    assert (s["landed"], s["failed"], s["open"], s["interrupted"]) == (1, 1, 1, 1)
    assert s["landed_per_h"] == 2.0
