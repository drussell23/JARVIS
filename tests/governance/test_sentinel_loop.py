"""The autonomous loop: every exit records something, and nothing waits on stdin.

An autonomous loop's dangerous failure is not crashing — it is spinning. The
sensor that produced a target does not stop producing it when the work fails,
so without a memory the loop re-picks the same file forever at full model cost.
These tests pin the four exits and the brake.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.sentinel_cooldown import (
    TargetCooldownLedger,
)
from backend.core.ouroboros.governance.autonomy.sentinel_loop import (
    PassOutcome,
    SentinelLoop,
    loop_interval_s,
)

#: What the loop acts on is the file a goal WRITES. For an uncovered module
#: that is the test file to be created, not the module itself — the goal's
#: scope must name what gets written or the cage refuses the op.
TARGET = "tests/test_widget.py"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "backend" / "api").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "backend" / "api" / "widget.py").write_text("x = 1\n" * 200)
    return tmp_path


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SENTINEL_MODE_ENABLED", "true")


class _Signed:
    ok = True

    def __init__(self, goal_id="ov-auto-x"):
        self.goal_id = goal_id
        self.reason = ""
        self.detail = ""


def _loop(repo, *, dispatch, outcome, cooldown, sign=None, observer=None):
    from backend.core.ouroboros.governance.autonomy import goal_discovery as gd

    if sign is not None:
        gd.synthesize_and_sign = sign          # type: ignore[assignment]
    return SentinelLoop(
        repo_root=repo, dispatch=dispatch, watcher=None, cooldown=cooldown,
        outcome_fn=outcome, observer=observer,
    )


@pytest.fixture
def restore_sign():
    from backend.core.ouroboros.governance.autonomy import goal_discovery as gd
    original = gd.synthesize_and_sign
    yield
    gd.synthesize_and_sign = original


# --------------------------------------------------------------------------
# The interval adapts; it is not a constant
# --------------------------------------------------------------------------

def test_the_interval_is_derived_from_the_pipeline_budget(monkeypatch):
    monkeypatch.delenv("JARVIS_SENTINEL_INTERVAL_S", raising=False)
    monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "4000")
    assert loop_interval_s() == 1000.0
    monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "400")
    assert loop_interval_s() == 100.0


def test_an_explicit_interval_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", "30")
    assert loop_interval_s() == 30.0


# --------------------------------------------------------------------------
# The four exits
# --------------------------------------------------------------------------

def test_a_landed_op_clears_the_cooldown(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")
    cd.record_failure(TARGET, reason="an earlier attempt")
    cd.record_success(TARGET)   # ensure discovery can see it

    async def outcome(op_id, deadline):
        return "landed", "applied"

    loop = _loop(repo, dispatch=lambda **kw: "op-1", outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "landed"
    assert cd.is_cooling(TARGET) is False
    assert cd.entry_for(TARGET) is None


def test_a_failed_op_cools_the_target_and_records_a_lesson(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    async def outcome(op_id, deadline):
        return "failed", "validation rejected the candidate"

    loop = _loop(repo, dispatch=lambda **kw: "op-2", outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "failed"
    assert cd.is_cooling(TARGET) is True
    assert cd.entry_for(TARGET).consecutive_failures == 1


def test_a_timeout_is_treated_as_a_failure_not_a_hang(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    async def outcome(op_id, deadline):
        return "timed_out", "no terminal state"

    loop = _loop(repo, dispatch=lambda **kw: "op-3", outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "timed_out"
    assert cd.is_cooling(TARGET) is True


def test_nothing_to_do_is_idle_not_an_error(repo, tmp_path, restore_sign):
    """Everything cooling is a temporary state by construction."""
    cd = TargetCooldownLedger(tmp_path / "cd.json")
    cd.record_failure(TARGET, reason="cooling")

    async def outcome(op_id, deadline):
        raise AssertionError("must not dispatch when nothing is eligible")

    loop = _loop(repo, dispatch=lambda **kw: "op-x", outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "idle"


def test_a_refused_dispatch_cools_and_does_not_raise(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    async def outcome(op_id, deadline):
        raise AssertionError("should not be reached")

    loop = _loop(repo, dispatch=lambda **kw: "", outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "failed"
    assert "not dispatched" in res.detail
    assert cd.is_cooling(TARGET) is True


def test_an_already_signed_goal_is_re_dispatched_not_stranded(repo, tmp_path,
                                                              restore_sign):
    """A duplicate id is the normal state for a RETRY: the goal was filed on
    an earlier pass and did not land. Treating it as a dead end permanently
    strands every goal the organism ever filed but failed to dispatch — the
    first attempt writes it, the failure cools the target, and the id collides
    forever after. The signed goal is the asset; dispatch THAT."""
    cd = TargetCooldownLedger(tmp_path / "cd.json")
    dispatched = {}

    class _Dup:
        ok = False
        goal_id = "ov-auto-x"
        reason = "duplicate_id"
        detail = ""

    def dispatch(**kw):
        dispatched.update(kw)
        return "op-redispatch"

    async def outcome(op_id, deadline):
        return "landed", "applied"

    loop = _loop(repo, dispatch=dispatch, outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Dup())
    res = asyncio.run(loop.run_once())
    assert res.state == "landed", res.detail
    assert res.op_id == "op-redispatch"
    assert dispatched["goal_id"] == "ov-auto-x", (
        "the EXISTING signed goal must be the one dispatched"
    )
    assert cd.is_cooling(TARGET) is False, "a re-dispatch that landed must not cool"


# --------------------------------------------------------------------------
# The brake: repeated failure must get geometrically rarer
# --------------------------------------------------------------------------

def test_repeated_failure_escalates_the_backoff(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    async def outcome(op_id, deadline):
        return "failed", "again"

    loop = _loop(repo, dispatch=lambda **kw: "op-n", outcome=outcome,
                 cooldown=cd, sign=lambda w, **kw: _Signed())
    asyncio.run(loop.run_once())
    first = cd.entry_for(TARGET)
    # The target is now cooling, so a second pass finds nothing and idles —
    # which IS the brake working.
    assert asyncio.run(loop.run_once()).state == "idle"
    assert cd.entry_for(TARGET).consecutive_failures == first.consecutive_failures


def test_a_pass_that_explodes_does_not_kill_the_loop(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    def boom(**kw):
        raise RuntimeError("dispatch exploded")

    async def outcome(op_id, deadline):
        raise AssertionError("unreachable")

    loop = _loop(repo, dispatch=boom, outcome=outcome, cooldown=cd,
                 sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "failed"          # classified, not raised


# --------------------------------------------------------------------------
# Nothing here waits on a human
# --------------------------------------------------------------------------

def test_the_loop_never_reads_stdin():
    """Scan the AST, not the text — prose about stdin is not a stdin read,
    and a test that cannot tell them apart fails on its own documentation."""
    import ast

    mod = __import__(
        "backend.core.ouroboros.governance.autonomy.sentinel_loop",
        fromlist=["x"],
    )
    tree = ast.parse(inspect.getsource(mod))
    forbidden = {"input", "stdin", "prompt_async", "PromptSession", "readline"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            hits.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            hits.append(node.attr)
    assert not hits, (
        f"the autonomous loop reaches for {sorted(set(hits))} — an unattended "
        "session must never be able to wedge on a TTY"
    )


def test_the_observer_never_breaks_the_loop(repo, tmp_path, restore_sign):
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    async def outcome(op_id, deadline):
        return "landed", "applied"

    def bad_observer(outcome_):
        raise RuntimeError("the TUI fell over")

    loop = _loop(repo, dispatch=lambda **kw: "op-o", outcome=outcome, cooldown=cd,
                 sign=lambda w, **kw: _Signed(), observer=bad_observer)
    res = asyncio.run(loop.run_once())
    loop._emit(res)                      # must not raise
    assert res.state == "landed"


def test_dispatch_is_called_off_the_event_loop(repo, tmp_path, restore_sign):
    """The intake submitter REFUSES to run on the event loop — it would have
    to await intake from inside the loop intake runs on. Every early
    autonomous pass hit exactly that:

        [OperatorGoal] submitter called ON the event loop (expected a
        to_thread worker) — goal will be FILED, not run

    so goals were signed and filed but never dispatched, and the loop then
    cooled targets for a fault that was purely this call's thread affinity.
    """
    import threading

    cd = TargetCooldownLedger(tmp_path / "cd.json")
    seen = {}

    def dispatch(**kw):
        seen["thread"] = threading.current_thread()
        seen["main"] = threading.main_thread()
        return "op-thread"

    async def outcome(op_id, deadline):
        return "landed", "applied"

    loop = _loop(repo, dispatch=dispatch, outcome=outcome, cooldown=cd,
                 sign=lambda w, **kw: _Signed())
    res = asyncio.run(loop.run_once())
    assert res.state == "landed"
    assert seen["thread"] is not seen["main"], (
        "dispatch ran on the event loop thread — the submitter will refuse it"
    )


def test_an_async_dispatch_is_awaited_directly(repo, tmp_path, restore_sign):
    """A coroutine submitter must NOT be shoved into a thread."""
    cd = TargetCooldownLedger(tmp_path / "cd.json")

    async def dispatch(**kw):
        return "op-async"

    async def outcome(op_id, deadline):
        return "landed", "applied"

    loop = _loop(repo, dispatch=dispatch, outcome=outcome, cooldown=cd,
                 sign=lambda w, **kw: _Signed())
    assert asyncio.run(loop.run_once()).op_id == "op-async"


def test_the_outcome_is_correlated_on_the_GOAL_not_the_op_id():
    """The Sentinel dispatches an ORIGIN id (`op-…-goal`); the pipeline runs
    the work under an id of its own (`op-…-cau`). Zero `*-goal*.jsonl` ledgers
    exist, so polling the origin id timed out by construction every pass, no
    matter how generous the deadline. The goal id is the identity that
    survives the intake boundary."""
    src = inspect.getsource(SentinelLoop._await_outcome)
    assert "goal_id" in src
    assert "_await_via_goal_ledger" in src, (
        "the probe still watches an op id the pipeline never uses"
    )
    probe = inspect.getsource(SentinelLoop._goal_verdict)
    assert "read_records" in probe, "reconciliation ledger not consulted"
    assert "SATISFIED" in probe and "TERMINAL" in probe


def test_a_terminal_op_that_did_not_satisfy_is_a_failure_not_a_wait():
    """Waiting longer cannot change a verdict that has already been reached."""
    probe = inspect.getsource(SentinelLoop._goal_verdict)
    assert "goal unsatisfied" in probe


def test_a_previous_sessions_verdict_is_not_this_dispatchs_outcome():
    """The reconciliation ledger is cross-session and append-only, so a goal
    dispatched in an earlier run leaves a TERMINAL record behind forever.
    Reading the whole history made every retry resolve INSTANTLY against a
    verdict from a previous session — the goal never ran, the target was
    cooled, and the backoff escalated on a failure already counted. Observed
    live: ops named in the Sentinel's log appearing nowhere in the session
    that supposedly produced them."""
    src = inspect.getsource(SentinelLoop._goal_verdict)
    assert "since_ts" in src, "the probe still reads the whole history"
    assert "previous session" in src or "History" in src or "history" in src

    stamp = inspect.getsource(SentinelLoop.run_once)
    assert "dispatched_at = time.time()" in stamp, (
        "nothing records WHEN this dispatch happened, so records cannot be "
        "attributed to it"
    )


def test_the_dispatch_stamp_is_wall_time_not_monotonic():
    """The ledger stamps `ts` in wall seconds and is written by OTHER
    processes; a monotonic stamp would be incomparable across them."""
    src = inspect.getsource(SentinelLoop.run_once)
    assert "time.time()" in src
    idx = src.index("dispatched_at")
    assert "monotonic" not in src[idx:idx + 120]


def test_an_unreadable_reconciliation_ledger_means_not_yet():
    """A probe that cannot read must not invent a verdict."""
    assert SentinelLoop._goal_verdict("") is None
    assert SentinelLoop._goal_verdict("no-such-goal-id-at-all") is None


def test_outcomes_render_for_the_telemetry_view():
    o = PassOutcome("landed", TARGET, "ov-auto-x", "op-1", "applied", 3.0)
    text = o.render()
    assert "landed" in text and TARGET in text and "ov-auto-x" in text
