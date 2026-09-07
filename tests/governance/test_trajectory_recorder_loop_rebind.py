"""TrajectoryRecorder must survive an event-loop change (Tier-1 flake fix).

A persistent recorder singleton outlives an event loop under pytest-asyncio's
per-test loops (and a daemon restart onto a fresh loop). Its ``asyncio.Queue``
is bound to the loop it was created in; reusing it across loops raised
``RuntimeError: <...> is bound to a different event loop`` inside ``_drain_loop``
— the exact intermittent failure that rolled back every autonomous VALIDATE.
``_ensure_writer`` now rebinds its primitives when the running loop changes.
"""
from __future__ import annotations

import asyncio

from backend.core.ouroboros.governance.observability import (
    trajectory_recorder as TR,
)


def _ensure_then_cleanup(rec):
    """Run ``_ensure_writer`` in a fresh loop, then CANCEL the tasks it started
    so nothing lingers past ``loop.close()``. Returns (loop, ok, queue)."""
    loop = asyncio.new_event_loop()

    async def _call():
        return rec._ensure_writer()

    ok = loop.run_until_complete(_call())
    queue = rec._queue
    # Cancel the writer/watchdog this loop started so the loop can close clean.
    tasks = [t for t in (rec._writer, rec._watchdog) if t is not None and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    loop.close()
    return loop, ok, queue


def test_ensure_writer_rebinds_queue_across_loops():
    rec = TR.TrajectoryRecorder()

    loop_a, ok_a, queue_a = _ensure_then_cleanup(rec)
    assert ok_a is True
    assert queue_a is not None
    assert rec._bound_loop is loop_a  # bound to loop A

    # On a DIFFERENT loop, _ensure_writer must REBIND (pre-fix it kept queue_a,
    # and _drain_loop then crossed loops).
    loop_b, ok_b, queue_b = _ensure_then_cleanup(rec)
    assert ok_b is True
    assert rec._bound_loop is loop_b
    assert queue_b is not queue_a  # fresh queue on loop B


def test_ensure_writer_reuses_queue_within_same_loop():
    rec = TR.TrajectoryRecorder()
    loop = asyncio.new_event_loop()

    async def _twice():
        assert rec._ensure_writer() is True
        q1 = rec._queue
        assert rec._ensure_writer() is True  # same loop -> no rebind
        return q1, rec._queue

    q1, q2 = loop.run_until_complete(_twice())
    assert q1 is q2  # stable within one loop
    tasks = [t for t in (rec._writer, rec._watchdog) if t is not None and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    loop.close()


def test_ensure_writer_without_running_loop_is_false():
    rec = TR.TrajectoryRecorder()
    assert rec._ensure_writer() is False  # no running loop -> False, never raises
