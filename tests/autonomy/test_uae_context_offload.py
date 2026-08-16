"""The screen-context sampler stops eating the event loop.

`StallSampler` dumped the main thread while the loop was provably wedged.
Across 24 captures this module held it in 8 — the single largest share::

    subprocess.run -> communicate -> selectors.select
    uae_context_manager.py:394 in _get_cursor_position
    uae_context_manager.py:319 in _capture_screen_state
    uae_context_manager.py:282 in _update_context
    asyncio/events.py:84 in _run              <- ON the loop

Every one of those call sites was ALREADY `async def`. That is the whole
lesson: a coroutine that calls `subprocess.run` blocks the loop for exactly
as long as the child runs — here an `osascript` that launches a Python
interpreter to import Quartz, and a `screencapture` with a 5s timeout.

These tests assert the PROPERTY (the loop keeps breathing) rather than the
refactor, so a future change that reintroduces blocking fails here even if it
keeps the helper's name.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.autonomy import uae_context_manager as uae


async def _loop_responsiveness(during, *, ticks=40, gap=0.01):
    """Run *during* while sampling how late the loop's own ticks are.

    Returns the worst delay between scheduled 10ms ticks. On a healthy loop
    this stays near `gap`; a blocked loop shows the full block duration.
    """
    worst = 0.0

    async def _ticker():
        nonlocal worst
        for _ in range(ticks):
            t0 = time.monotonic()
            await asyncio.sleep(gap)
            worst = max(worst, time.monotonic() - t0)

    ticker = asyncio.create_task(_ticker())
    await during
    ticker.cancel()
    try:
        await ticker
    except asyncio.CancelledError:
        pass
    return worst


class TestBlockingWorkLeavesTheLoop:
    async def test_a_slow_blocking_call_does_not_stall_the_loop(self):
        """THE regression, in miniature: 300ms of blocking work must not
        cost the loop 300ms of responsiveness."""
        def _blocks():
            time.sleep(0.3)
            return "done"

        worst = await _loop_responsiveness(uae._off_loop(_blocks))
        assert worst < 0.15, f"loop was blocked for {worst:.3f}s"

    async def test_the_result_still_comes_back(self):
        assert await uae._off_loop(lambda: 21 * 2) == 42

    async def test_an_exception_becomes_None_not_a_crash(self, caplog):
        """A screen-context probe must never take down the loop it was moved
        off of — and must not fail silently either."""
        def _boom():
            raise RuntimeError("osascript exploded")

        with caplog.at_level("DEBUG"):
            assert await uae._off_loop(_boom) is None
        assert any("failed" in r.message for r in caplog.records)

    async def test_cancellation_is_propagated_not_swallowed(self):
        """Swallowing CancelledError is how a task becomes unkillable."""
        started = asyncio.Event()

        def _slow():
            started.set()
            time.sleep(2)

        task = asyncio.create_task(uae._off_loop(_slow))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_it_degrades_to_inline_rather_than_failing(self, monkeypatch):
        """Fail-open: a missing offload helper must not break context
        capture. Better a brief stall than a blind organism."""
        import builtins
        real = builtins.__import__

        def _no_offload(name, *a, **k):
            if "async_offload" in name:
                raise ImportError("simulated")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_offload)
        assert await uae._off_loop(lambda: "still works") == "still works"


class TestTheScreenshotBodyIsOneUnitOfWork:
    """Offloading only the capture would leave the file read, the md5 and the
    base64 — megabytes of CPU over a full-screen PNG — on the loop."""

    def test_the_blocking_half_is_synchronous_and_self_contained(self):
        fn = uae.UAEContextManager._capture_screenshot_blocking
        assert not asyncio.iscoroutinefunction(fn), (
            "it must be a plain callable so it can be handed to a worker")

    def test_the_temp_file_is_removed_even_when_capture_fails(
            self, monkeypatch, tmp_path):
        """A failed capture must not leak PNGs into /tmp for the life of the
        daemon — the `finally` is the point of the rewrite."""
        seen = {}

        class _TmpFile:
            name = str(tmp_path / "shot.png")

            def __enter__(self):
                open(self.name, "wb").close()
                seen["path"] = self.name
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(uae.tempfile if hasattr(uae, "tempfile") else
                            __import__("tempfile"), "NamedTemporaryFile",
                            lambda **k: _TmpFile())

        def _explode(*a, **k):
            raise RuntimeError("screencapture missing")

        monkeypatch.setattr(__import__("subprocess"), "run", _explode)
        with pytest.raises(RuntimeError):
            uae.UAEContextManager._capture_screenshot_blocking()
        import os
        assert not os.path.exists(seen["path"]), "temp PNG leaked"


class TestTheSameTrapInTheYabaiHealthMonitor:
    """Found by the same sweep: `_check_service_running` shells out to
    `yabai -m query` with a 3s timeout and was called directly from the
    periodic monitor. `ensure_running_async` already offloaded its own probe;
    this path did not — which is why only the periodic one showed up in the
    dumps."""

    async def test_a_blocking_probe_does_not_stall_the_loop(self):
        from backend.vision.yabai_space_detector import _yabai_off_loop

        def _blocks():
            time.sleep(0.3)
            return True

        worst = await _loop_responsiveness(_yabai_off_loop(_blocks))
        assert worst < 0.15, f"loop was blocked for {worst:.3f}s"

    async def test_a_failed_probe_is_None_not_False(self):
        """A probe that could not RUN is not evidence the service STOPPED —
        returning False there would trigger spurious auto-recovery."""
        from backend.vision.yabai_space_detector import _yabai_off_loop

        def _boom():
            raise RuntimeError("yabai missing")

        assert await _yabai_off_loop(_boom) is None
