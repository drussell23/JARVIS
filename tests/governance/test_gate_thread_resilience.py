"""The gate must shed cleanly — no orphaned reader, no unretrieved exception.

The approval deadline (9d8ba44097) means the gate now fires while a local
prompt is still alive, which is precisely the path that leaked: `cancel()` was
called and never awaited, so the prompt task was still attached to stdin when
the gate returned, and `patch_stdout` was unwound underneath it. One orphaned
task per shed op, at exactly the moment the deadline is designed to fire.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.battle_test import serpent_flow


class _Flow:
    """Hosts the SHIPPING method, so the test cannot pass against a copy."""

    def __init__(self):
        self._race = serpent_flow.SerpentFlow._race_gate_answer.__get__(self, _Flow)
        self._gate_answered_via_cockpit = False
        self._gate_timed_out = False


def test_a_cancelled_local_prompt_is_awaited_not_merely_signalled():
    """`cancel()` is a REQUEST. Returning before the task settles is what
    leaves a reader on stdin after the gate has gone."""
    src = inspect.getsource(serpent_flow.SerpentFlow._race_gate_answer)
    tail = src[src.index("finally:"):]
    assert "local_task.cancel()" in tail
    assert "await asyncio.wait({local_task}" in tail, (
        "the cancelled prompt task is never awaited — it can outlive the gate"
    )


def test_the_task_exception_is_retrieved():
    """Without this every cancelled prompt logs 'exception was never
    retrieved' at GC — noise that hides real faults."""
    tail = inspect.getsource(serpent_flow.SerpentFlow._race_gate_answer)
    assert "local_task.exception()" in tail


def test_stdout_is_restored_only_after_the_reader_has_settled():
    """Order is load-bearing: unwinding patch_stdout under a live reader is
    how a TUI writes into a torn-down patcher."""
    src = inspect.getsource(serpent_flow.SerpentFlow._race_gate_answer)
    tail = src[src.index("finally:"):]
    assert tail.index("asyncio.wait({local_task}") < tail.index("ctx.__exit__"), (
        "patch_stdout is exited before the prompt task settles"
    )


def test_an_unkillable_prompt_does_not_convert_a_shed_into_a_hang():
    """Bounded on purpose. A task that refuses to die must be abandoned, or
    the teardown reinstates the exact wedge the deadline exists to prevent."""
    src = inspect.getsource(serpent_flow.SerpentFlow._race_gate_answer)
    tail = src[src.index("finally:"):]
    assert "timeout=2.0" in tail
    assert "abandoning" in tail.lower()


def test_the_gate_leaves_no_pending_tasks_behind(monkeypatch):
    """The behavioural proof: run the real gate to its deadline and assert the
    loop is clean afterwards."""
    monkeypatch.setenv("JARVIS_APPROVAL_DEADLINE_S", "5")

    async def _run():
        before = {t for t in asyncio.all_tasks()}
        flow = _Flow()
        never = asyncio.get_running_loop().create_future()
        decision = await asyncio.wait_for(flow._race(never), timeout=30)
        # Give any orphan a chance to surface rather than asserting instantly.
        await asyncio.sleep(0.2)
        leaked = {
            t for t in asyncio.all_tasks()
            if t not in before and not t.done() and t is not asyncio.current_task()
        }
        return decision, leaked

    decision, leaked = asyncio.run(_run())
    assert decision is not None and decision.choice.name == "REJECT"
    assert not leaked, f"gate leaked {len(leaked)} pending task(s): {leaked}"


def test_the_gate_holds_no_database_handle_across_the_wait():
    """A gate that waits for a human while holding a connection turns an
    absent operator into a lock nobody can clear."""
    src = inspect.getsource(serpent_flow.SerpentFlow._race_gate_answer)
    for forbidden in ("sqlite3", "connect(", "cursor(", "BEGIN", "commit()"):
        assert forbidden not in src, (
            f"the gate references {forbidden!r} — it must hold no DB handle "
            "while waiting on a human"
        )


def test_the_timeout_ledger_write_does_not_block_the_shed():
    """Recording is scheduled on the running loop, never awaited inline: a
    slow disk must not extend the very wait that just expired."""
    from backend.core.ouroboros.governance import inline_approval

    src = inspect.getsource(inline_approval.record_approval_timeout)
    assert "loop.create_task(coro)" in src
