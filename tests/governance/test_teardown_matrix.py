"""GracefulTeardownMatrix — DW background-loop cancellation at shutdown
(post-summary 5-min hang fix, 2026-06-20)."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import dw_discovery_runner as DWR


async def _never():
    # A loop that would hang forever if not cancelled.
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise


async def test_shutdown_cancels_pending_loops(monkeypatch):
    t1 = asyncio.ensure_future(_never())
    t2 = asyncio.ensure_future(_never())
    monkeypatch.setattr(DWR, "_REFRESH_TASK", t1, raising=False)
    monkeypatch.setattr(DWR, "_HEAVY_PROBE_TASK", t2, raising=False)
    n = await DWR.shutdown_background_loops(timeout_s=2.0)
    assert n == 2
    assert t1.cancelled() and t2.cancelled()
    # Module refs cleared so a second call is a clean no-op.
    assert DWR._REFRESH_TASK is None
    assert DWR._HEAVY_PROBE_TASK is None


async def test_shutdown_noop_when_no_loops(monkeypatch):
    monkeypatch.setattr(DWR, "_REFRESH_TASK", None, raising=False)
    monkeypatch.setattr(DWR, "_HEAVY_PROBE_TASK", None, raising=False)
    assert await DWR.shutdown_background_loops() == 0


async def test_shutdown_skips_already_done(monkeypatch):
    done = asyncio.ensure_future(asyncio.sleep(0))
    await done
    monkeypatch.setattr(DWR, "_REFRESH_TASK", done, raising=False)
    monkeypatch.setattr(DWR, "_HEAVY_PROBE_TASK", None, raising=False)
    assert await DWR.shutdown_background_loops() == 0


async def test_shutdown_bounded_when_task_slow_to_cancel(monkeypatch):
    # A task whose cancellation cleanup is SLOW must not wedge teardown past the
    # bound — the function returns within ~timeout_s, not the cleanup duration.
    # (Killable, so pytest's loop teardown doesn't hang.)
    async def _slow_to_cancel():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(1.5)  # slow cleanup, but eventually yields
            raise
    t = asyncio.ensure_future(_slow_to_cancel())
    await asyncio.sleep(0)  # let it start
    monkeypatch.setattr(DWR, "_REFRESH_TASK", t, raising=False)
    monkeypatch.setattr(DWR, "_HEAVY_PROBE_TASK", None, raising=False)
    import time
    s = time.monotonic()
    n = await asyncio.wait_for(
        DWR.shutdown_background_loops(timeout_s=0.3), timeout=3.0,
    )
    elapsed = time.monotonic() - s
    assert n == 1
    assert elapsed < 1.2  # returned on the bound, NOT after the 1.5s cleanup


async def test_never_raises_on_garbage(monkeypatch):
    class _Bad:
        def done(self):
            raise RuntimeError("boom")
    monkeypatch.setattr(DWR, "_REFRESH_TASK", _Bad(), raising=False)
    monkeypatch.setattr(DWR, "_HEAVY_PROBE_TASK", None, raising=False)
    # Swallows the error; returns a count (0 here) without raising.
    assert await DWR.shutdown_background_loops() == 0
