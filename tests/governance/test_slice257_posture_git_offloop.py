"""Slice 257 — posture ``commit_ratios`` git must run OFF the event loop.

Root cause (bt-2026-06-16-052242, near-fatal 108.9s loop wedge):
``SignalCollector.commit_ratios_async`` resolved HEAD + the 100-commit
``git log`` via ``asyncio.create_subprocess_exec``. That helper performs the
fork/exec of the child **on the event-loop thread**. From the large,
multi-threaded organism process — especially concurrent with the Oracle
process pool forking 16 workers during the cold index — that fork blocked the
loop synchronously for 33–108s across three sessions (``git log`` itself is
0.14s, so the wedge is the fork, not the query). Because the block is
synchronous and yield-less, the 30s collector ``wait_for`` could not cancel it
and ``ControlPlaneStarvation`` stayed silent; the heartbeat froze and the
120s ``ExternalWatchdog`` SIGKILLed the session at ``in_flight``.

Fix: run the git work in the dedicated ``fs_signal_executor`` thread (the same
pool the other fs-backed signals already use). A slow fork then blocks a
worker thread, never the loop — the loop keeps beating the heartbeat. Caching
+ ratio semantics are preserved.

Pins:
  §1  commit_ratios_async still returns correct Conventional-Commit ratios
  §2  HEAD-cache short-circuit still skips the git-log when HEAD is unchanged
  §3  the git work executes on a NON-event-loop thread (proves off-loop)
  §4  commit_ratios_async no longer calls create_subprocess_exec on the loop
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.posture_observer import SignalCollector


def test_commit_ratios_async_returns_correct_ratios(monkeypatch) -> None:
    sc = SignalCollector(Path("."))
    monkeypatch.setattr(sc, "_git_head", lambda: "headsha1")
    monkeypatch.setattr(
        sc, "_git_subjects",
        lambda n: ["feat: a", "fix: b", "refactor: c", "test: d", "docs: e", "chore: f"],
    )
    ratios = asyncio.run(sc.commit_ratios_async())
    # 6 subjects: 1 feat, 1 fix, 1 refactor, (1 test + 1 docs)=2 test_docs
    assert ratios["feat"] == pytest.approx(1 / 6)
    assert ratios["fix"] == pytest.approx(1 / 6)
    assert ratios["refactor"] == pytest.approx(1 / 6)
    assert ratios["test_docs"] == pytest.approx(2 / 6)


def test_commit_ratios_head_cache_short_circuits_gitlog(monkeypatch) -> None:
    sc = SignalCollector(Path("."))
    monkeypatch.setattr(sc, "_git_head", lambda: "stablehead")
    calls = {"subjects": 0}

    def _subjects(n):
        calls["subjects"] += 1
        return ["feat: x"]

    monkeypatch.setattr(sc, "_git_subjects", _subjects)
    first = asyncio.run(sc.commit_ratios_async())
    second = asyncio.run(sc.commit_ratios_async())
    assert first == second
    # HEAD unchanged → the expensive git-log runs exactly once.
    assert calls["subjects"] == 1


def test_git_work_runs_off_the_event_loop(monkeypatch) -> None:
    loop_thread_ident = {"v": None}
    seen = {"head": None, "subjects": None}

    def _head():
        seen["head"] = threading.get_ident()
        return "h"

    def _subjects(n):
        seen["subjects"] = threading.get_ident()
        return ["feat: x"]

    async def _run():
        loop_thread_ident["v"] = threading.get_ident()
        sc = SignalCollector(Path("."))
        monkeypatch.setattr(sc, "_git_head", _head)
        monkeypatch.setattr(sc, "_git_subjects", _subjects)
        await sc.commit_ratios_async()

    asyncio.run(_run())
    # The git calls MUST have executed on a different (worker) thread, never
    # the event-loop thread — that is what keeps a slow fork off the loop.
    assert seen["head"] is not None and seen["head"] != loop_thread_ident["v"]
    assert seen["subjects"] is not None and seen["subjects"] != loop_thread_ident["v"]


def test_commit_ratios_async_does_not_fork_on_loop() -> None:
    # Structural guard: the coroutine must not CALL create_subprocess_exec
    # (the loop-thread fork that caused the wedge) nor the on-loop async git
    # helpers; it must offload via an executor. The docstring may still
    # reference the historical mechanism, so match the call forms only.
    src = inspect.getsource(SignalCollector.commit_ratios_async)
    assert "create_subprocess_exec(" not in src
    assert "_git_head_async(" not in src
    assert "_git_subjects_async(" not in src
    assert "run_in_executor(" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
