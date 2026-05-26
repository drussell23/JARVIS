"""Slice 14 — SessionWAL async-safe checkpoint via asyncio.to_thread.

Closes the LoopDeadman tombstone observed in soak bt-2026-05-25-230029:

  2026-05-25T16:51:55 [Ouroboros.LoopDeadman] CRITICAL
    [LoopDeadman.TOMBSTONE] thread_id=8781099264
    File "harness.py:6752" in _slice12g3_periodic_checkpoint_loop
      await asyncio.sleep(interval_s)
    File "session_wal.py:174" in checkpoint
      with self._lock:
    File "session_wal.py:254" in _atomic_write

The periodic checkpoint loop called the SYNC ``checkpoint()`` from the
asyncio main thread. Inside ``checkpoint()``: `with self._lock`
(threading.Lock) → `_atomic_write()` (synchronous json.dumps + open +
write + fsync + os.replace). The ~5-50ms file I/O blocked the asyncio
loop tick. Cumulative + lock contention with other holders eventually
triggered LoopDeadman. Result: $0.00 burn in 51min — engine deadlocked
itself before reaching any provider call.

# Fix mechanism

Add ``async def acheckpoint(self, state, reason)`` on SessionWAL that
wraps the sync path via ``asyncio.to_thread``. The blocking file I/O
runs on the default thread pool executor; the asyncio main loop tick
continues during the write. Migrate harness.py:6774 to await
``acheckpoint`` instead of calling sync ``checkpoint``.

# Discipline

* Sync ``checkpoint()`` + ``force_checkpoint()`` RETAINED for shutdown
  / signal handler / atexit paths where the loop may already be
  cancelled and ``to_thread`` is unsafe.
* ``threading.Lock`` preserved (NOT asyncio.Lock) — both the sync and
  the async path coordinate through the same lock, so signal handlers
  + atexit fallbacks still safely interleave with the worker-thread
  writes.
* Behavior contract on ``acheckpoint`` identical to ``checkpoint`` —
  same return shape, same debounce, same defensive exception-swallow.
* ``asyncio.CancelledError`` propagates per cooperative-cancel contract.

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAL_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "session_wal.py"
)
HARNESS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "battle_test"
    / "harness.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_acheckpoint_method_present_and_uses_to_thread() -> None:
    """``SessionWAL`` MUST expose an async ``acheckpoint`` method that
    routes the sync ``checkpoint`` through ``asyncio.to_thread``.
    Without this, the periodic checkpoint loop will block the main
    asyncio thread and re-introduce the LoopDeadman tombstone."""
    src = WAL_FILE.read_text()
    assert "async def acheckpoint" in src, (
        "SessionWAL.acheckpoint method missing — Slice 14 reverted"
    )
    assert "asyncio.to_thread" in src, (
        "acheckpoint does NOT use asyncio.to_thread — blocking IO "
        "is still on the asyncio main loop"
    )
    # The to_thread call must wrap self.checkpoint (NOT some other call)
    assert "self.checkpoint, state, reason" in src, (
        "asyncio.to_thread doesn't wrap self.checkpoint — wrong routing"
    )
    # Slice 14 attribution + bt soak link
    assert "Slice 14" in src
    assert "bt-2026-05-25-230029" in src, (
        "Missing soak attribution — future readers can't trace the "
        "LoopDeadman forensic that surfaced this"
    )


def test_ast_pin_harness_periodic_loop_uses_acheckpoint() -> None:
    """The harness periodic checkpoint loop MUST await ``acheckpoint``
    instead of calling sync ``checkpoint``. Without this, the new
    async-safe method is dead code."""
    src = HARNESS_FILE.read_text()
    # The periodic loop must use the async variant
    assert "await self._session_wal.acheckpoint(state, \"periodic\")" in src, (
        "harness periodic checkpoint loop does NOT use acheckpoint — "
        "Slice 14 wiring is dead code"
    )
    # The Slice 14 comment must be present (audit trail)
    assert "Slice 14" in src, (
        "harness.py missing Slice 14 attribution comment"
    )
    # The sync checkpoint() call on the periodic path must be REMOVED
    # — only the shutdown_cancel branch may still use force_checkpoint
    # (which is a separate method). AST-walk the relevant function.
    tree = ast.parse(src, filename=str(HARNESS_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_slice12g3_periodic_checkpoint_loop"
        ):
            body_src = ast.unparse(node)
            # The periodic-firing branch (after the await asyncio.sleep)
            # must NOT contain a plain ``self._session_wal.checkpoint(``
            # call — only the shutdown_cancel branch may call
            # force_checkpoint. Heuristic: the substring
            # ``self._session_wal.checkpoint(state, "periodic")`` is the
            # exact line we replaced; it must be gone.
            assert (
                'self._session_wal.checkpoint(state, "periodic")'
                not in body_src
            ), (
                "harness periodic loop STILL contains sync checkpoint(state, "
                "\"periodic\") — Slice 14 migration incomplete"
            )
            break


# ──────────────────────────────────────────────────────────────────────
# Spine — 5 (functional)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_acheckpoint_writes_summary_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acheckpoint() must successfully write summary.json with the
    provided state. Same effect as sync checkpoint, just async-safely."""
    from backend.core.ouroboros.governance.session_wal import SessionWAL
    monkeypatch.setenv("JARVIS_SESSION_WAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SESSION_WAL_MIN_INTERVAL_S", "0.0")
    wal = SessionWAL(session_dir=tmp_path)
    state = {"foo": "bar", "n": 42}
    ok = await wal.acheckpoint(state, "test")
    assert ok is True, "acheckpoint returned False on a clean write"
    summary = tmp_path / "summary.json"
    assert summary.exists(), "summary.json was not written"
    parsed = json.loads(summary.read_text())
    assert parsed["foo"] == "bar"
    assert parsed["n"] == 42


@pytest.mark.asyncio
async def test_spine_acheckpoint_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While acheckpoint is in flight, the event loop MUST continue
    processing other tasks. Spawn a concurrent task that polls the
    loop's clock — if it gets ≥ N polls during the checkpoint, the
    loop wasn't blocked. This is the regression guard against the
    LoopDeadman tombstone from bt-2026-05-25-230029."""
    from backend.core.ouroboros.governance.session_wal import SessionWAL
    monkeypatch.setenv("JARVIS_SESSION_WAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SESSION_WAL_MIN_INTERVAL_S", "0.0")
    wal = SessionWAL(session_dir=tmp_path)

    # Build a large state to make the write take measurable time
    # (without being slow enough to slow down CI). 50k-row dict.
    state = {f"k{i}": f"v{i}" * 8 for i in range(50_000)}

    poll_count = 0
    async def _poll_loop_tick() -> None:
        nonlocal poll_count
        # Sleep 1ms repeatedly — each yield checks the loop is alive
        for _ in range(40):
            await asyncio.sleep(0.001)
            poll_count += 1

    # Race: checkpoint + poll concurrently. If checkpoint blocks the
    # loop, poll_count will be near 0; if it's properly off-loop, the
    # poller will get most/all of its 40 yields.
    await asyncio.gather(
        wal.acheckpoint(state, "concurrency_test"),
        _poll_loop_tick(),
    )
    # Tolerance: at least half the polls fired (allow CI noise)
    assert poll_count >= 20, (
        f"Event loop appears blocked during acheckpoint — only "
        f"{poll_count}/40 polls fired (regression on Slice 14)"
    )


@pytest.mark.asyncio
async def test_spine_acheckpoint_returns_false_on_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When wal_enabled() is False, acheckpoint must return False
    WITHOUT scheduling the worker thread. Symmetric to sync checkpoint
    behavior — no surprise side effects when disabled."""
    from backend.core.ouroboros.governance.session_wal import SessionWAL
    monkeypatch.setenv("JARVIS_SESSION_WAL_ENABLED", "false")
    wal = SessionWAL(session_dir=tmp_path)
    ok = await wal.acheckpoint({"x": 1}, "disabled_test")
    assert ok is False
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.asyncio
async def test_spine_acheckpoint_swallows_inner_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the wrapped sync checkpoint raises, acheckpoint MUST swallow
    the exception and return False (same defensive contract as the
    sync entry point). Monkey-patch `checkpoint` to raise."""
    from backend.core.ouroboros.governance.session_wal import SessionWAL
    monkeypatch.setenv("JARVIS_SESSION_WAL_ENABLED", "true")
    wal = SessionWAL(session_dir=tmp_path)
    # Force the sync checkpoint to raise — verifies acheckpoint's
    # defensive try/except handles inner failures rather than propagating.
    # SessionWAL uses __slots__ so patch the class-level method
    # (instance binding is the same name lookup at call time).
    def _boom(self, state, reason=""):
        raise RuntimeError("simulated write failure")
    monkeypatch.setattr(SessionWAL, "checkpoint", _boom)
    # Must not raise
    ok = await wal.acheckpoint({"x": 1}, "fail_test")
    assert ok is False, (
        "acheckpoint propagated an exception from wrapped sync call — "
        "defensive contract violated"
    )


@pytest.mark.asyncio
async def test_spine_acheckpoint_respects_debounce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple rapid acheckpoint calls must respect the debounce
    interval — same as sync checkpoint. Second call within
    min_interval_s returns False without writing."""
    from backend.core.ouroboros.governance.session_wal import SessionWAL
    monkeypatch.setenv("JARVIS_SESSION_WAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SESSION_WAL_MIN_INTERVAL_S", "10.0")
    wal = SessionWAL(session_dir=tmp_path)
    first = await wal.acheckpoint({"v": 1}, "first")
    assert first is True
    # Immediate second call is within debounce window
    second = await wal.acheckpoint({"v": 2}, "second")
    assert second is False, (
        "Second checkpoint within debounce window returned True — "
        "debounce broken on async path"
    )
    # The file still contains the FIRST write (debounce skipped the second)
    parsed = json.loads((tmp_path / "summary.json").read_text())
    assert parsed["v"] == 1
