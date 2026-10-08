"""Held-out benchmark: tasks no training row can contain, scored by hidden tests.

Pins the parts that decide what the benchmark CLAIMS: which commits can be
tasks (never O+V's), what a score means (partial credit against the code
left alone, never the training reward), that a task set cannot be altered
unnoticed, and that two runs are compared task by task on the same set.
The live parts -- snapshots, O+V's test runner, J-Prime -- are exercised by
building and running a set, not here.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.observability import heldout_benchmark as hb


def _log(*commits):
    out = []
    for sha, parents, opid, files in commits:
        out.append(f"@@{sha}|{parents}|2026-09-01T10:00:00-07:00|{opid}")
        out.extend(f"{s}\t{p}" for s, p in files)
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Which commits can be tasks
# ---------------------------------------------------------------------------

def test_a_human_single_source_change_with_tests_is_a_candidate():
    (c,) = hb.parse_log(_log(("a" * 40, "p1", "", [("M", "backend/x/mod.py"), ("A", "tests/x/test_mod.py")])))
    assert c.target == "backend/x/mod.py" and c.tests == ("tests/x/test_mod.py",) and c.parent == "p1"


@pytest.mark.parametrize("why, commit", [
    ("O+V made it: it may be in the training corpus",
     ("b" * 40, "p1", "op-123", [("M", "backend/x/mod.py"), ("A", "tests/x/test_mod.py")])),
    ("a merge has no single parent to snapshot",
     ("c" * 40, "p1 p2", "", [("M", "backend/x/mod.py"), ("A", "tests/x/test_mod.py")])),
    ("two source files: the task is not one change",
     ("d" * 40, "p1", "", [("M", "backend/a.py"), ("M", "backend/b.py"), ("A", "tests/test_a.py")])),
    ("a CREATED source file has no prior code to change",
     ("e" * 40, "p1", "", [("A", "backend/new.py"), ("A", "tests/test_new.py")])),
    ("no tests: nothing can judge it",
     ("f" * 40, "p1", "", [("M", "backend/x/mod.py"), ("M", "docs/x.md")])),
    ("a rename is not a plain edit",
     ("g" * 40, "p1", "", [("R100", "backend/old.py\tbackend/new.py"), ("A", "tests/test_x.py")])),
    ("a deleted test file is not a hidden test",
     ("h" * 40, "p1", "", [("M", "backend/x/mod.py"), ("D", "tests/x/test_mod.py")])),
])
def test_what_is_not_a_task(why, commit):
    assert hb.parse_log(_log(commit)) == [], why


def test_selection_is_deterministic_and_content_independent():
    cands = hb.parse_log(_log(*[(f"{i:040x}", "p", "", [("M", "backend/m.py"), ("A", "tests/test_m.py")])
                                for i in range(20)]))
    order = [c.commit for c in sorted(cands, key=hb.selection_key)]
    assert order == [c.commit for c in sorted(reversed(cands), key=hb.selection_key)]
    assert order != [c.commit for c in cands]          # not history order: spread across it


# ---------------------------------------------------------------------------
# What a score means
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("share, before, expected", [
    (1.0, 0.25, 1.0),          # all hidden tests pass
    (0.25, 0.25, 0.0),         # no better than leaving the code alone
    (0.625, 0.25, 0.5),        # closed half the gap
    (0.0, 0.25, 0.0),          # broke what already passed: clipped, not negative
    (0.5, 1.0, 0.0),           # no room: never a division by zero
])
def test_gain_is_the_share_of_the_gap_closed(share, before, expected):
    assert hb.gain(share, before) == pytest.approx(expected)


def test_a_test_run_that_collected_nothing_scores_zero():
    assert hb.TestScore(total=0, failed=0).share == 0.0
    assert hb.TestScore(total=8, failed=2).share == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# A frozen task set
# ---------------------------------------------------------------------------

def _task(i, before=0.25):
    return hb.Task(task_id=f"t{i}", commit=f"{i:040x}", parent="p", committed_at="2026-09-01",
                   target="backend/m.py", description=f"change {i}", prompt=f"prompt {i}",
                   contract="diff_capable", tests={"tests/test_m.py": f"def test_{i}(): pass\n"},
                   before=before, hidden_total=4)


def _write_set(path, tasks):
    header = {"schema": hb.TASKSET_SCHEMA, "fingerprint": hb.fingerprint(tasks), "tasks": len(tasks)}
    path.write_text("\n".join([json.dumps({"header": header})] +
                              [json.dumps(hb.asdict(t)) for t in tasks]) + "\n")


def test_a_task_set_round_trips(tmp_path):
    f = tmp_path / "set.jsonl"
    _write_set(f, [_task(1), _task(2)])
    header, tasks = hb.load_taskset(f)
    assert [t.task_id for t in tasks] == ["t1", "t2"] and header["tasks"] == 2


@pytest.mark.parametrize("field, value", [("prompt", "a different prompt"), ("before", 0.0),
                                          ("tests", {"tests/test_m.py": "def test_x(): assert 1\n"})])
def test_an_altered_task_set_is_refused(tmp_path, field, value):
    f = tmp_path / "set.jsonl"
    tasks = [_task(1), _task(2)]
    _write_set(f, tasks)
    lines = f.read_text().splitlines()
    row = json.loads(lines[1])
    row[field] = value
    lines[1] = json.dumps(row)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="altered"):
        hb.load_taskset(f)


def test_the_fingerprint_ignores_order_but_not_content():
    a, b = _task(1), _task(2)
    assert hb.fingerprint([a, b]) == hb.fingerprint([b, a])
    assert hb.fingerprint([a, b]) != hb.fingerprint([a, _task(3)])


# ---------------------------------------------------------------------------
# Paired comparison
# ---------------------------------------------------------------------------

def _run(fp, gains, rewards=None):
    rewards = rewards or [None] * len(gains)
    return {"fingerprint": fp, "results": [{"task_id": f"t{i}", "gain": g, "reward": r}
                                           for i, (g, r) in enumerate(zip(gains, rewards))]}


def test_runs_of_different_task_sets_are_never_compared():
    assert hb.compare(_run("x", [1.0]), _run("y", [0.0]))["verdict"] == "incomparable"


def test_a_consistent_improvement_is_better():
    out = hb.compare(_run("x", [1.0] * 10), _run("x", [0.0] * 10), alpha=0.05)
    assert out["verdict"] == "better" and out["wins"] == 10 and out["p_better"] == pytest.approx(1 / 1024)


def test_a_consistent_regression_is_worse():
    assert hb.compare(_run("x", [0.0] * 10), _run("x", [0.5] * 10), alpha=0.05)["verdict"] == "worse"


def test_ties_carry_no_evidence():
    out = hb.compare(_run("x", [0.5] * 9 + [1.0]), _run("x", [0.5] * 10), alpha=0.05)
    assert out["ties"] == 9 and out["wins"] == 1
    assert out["verdict"] == "no_detectable_difference"           # one win is not evidence


def test_a_rising_reward_without_rising_capability_is_flagged():
    out = hb.compare(_run("x", [0.5] * 6, rewards=[0.9] * 6), _run("x", [0.5] * 6, rewards=[0.6] * 6))
    assert out["mean_reward_delta"] == pytest.approx(0.3)
    assert out["reward_without_capability"] is True


def test_summary_counts_solved_and_mean():
    rs = [hb.TaskResult(task_id="a", gain=1.0, share=1.0, accepted=True, reward=0.8),
          hb.TaskResult(task_id="b", gain=0.0, share=0.2, accepted=False)]
    s = hb.summarize(rs)
    assert s == {"tasks": 2, "mean_gain": 0.5, "solved": 1, "accepted": 1, "mean_reward": 0.8}
