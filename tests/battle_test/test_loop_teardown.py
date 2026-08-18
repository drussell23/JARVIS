"""Teardown cancels what it is about to close.

`bt-2026-08-18-021438`, at `post_asyncio_teardown`, produced three symptoms
with one cause::

    RuntimeError: aclose(): asynchronous generator is already running
        <async_generator object StreamEventBroker.stream_iter>
        <async_generator object ExecutionGraphProgressTracker._drain_subscriber>
    UnknownError: Task was destroyed but it is pending!

`asyncio.run` does four things when it tears a loop down; the harness script
owns its loop by hand and did the last three, skipping *cancel every remaining
task and let it finish*. So tasks were still parked inside those generators
when `shutdown_asyncgens()` called `aclose()` on them. The generators were
never at fault -- both handle `CancelledError` and unsubscribe in `finally`.

The first test here is the production defect itself, driven through the REAL
broker: it fails without the phase and passes with it.
"""
from __future__ import annotations

import asyncio
import contextlib
import io

import pytest

from backend.core.ouroboros.battle_test import loop_teardown as lt


# ---------------------------------------------------------------------------
# The production defect
# ---------------------------------------------------------------------------


def _drive(with_phase: bool) -> int:
    """Park a consumer inside the real `stream_iter`, tear down, count aclose
    failures. Runs on its OWN loop because it is the loop teardown under test."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    seen = []
    loop.set_exception_handler(
        lambda l, ctx: seen.append(str(ctx.get("message", "")))
    )
    try:
        broker = get_default_broker()
        sub = broker.subscribe()

        async def consume():
            async for _ in broker.stream_iter(sub, heartbeat_s=0):
                pass

        async def start():
            asyncio.ensure_future(consume())
            await asyncio.sleep(0.05)      # park it on queue.get()

        loop.run_until_complete(start())
        if with_phase:
            loop.run_until_complete(lt.cancel_remaining_tasks())
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:  # noqa: BLE001
                seen.append(repr(exc))
            seen.extend(buf.getvalue().splitlines())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    return len([s for s in seen
                if "aclose" in s or "asynchronous generator" in s])


def test_the_defect_reproduces_without_the_phase():
    """Guards the guard: if this stops failing, the test below proves nothing."""
    assert _drive(with_phase=False) >= 1


def test_the_phase_closes_the_generator_cleanly():
    assert _drive(with_phase=True) == 0


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tasks_is_a_clean_no_op():
    report = await lt.cancel_remaining_tasks()
    assert report.cancelled == 0
    assert report.clean
    assert "no outstanding tasks" in report.render()


@pytest.mark.asyncio
async def test_never_cancels_the_calling_task():
    """The phase runs INSIDE a task; cancelling itself would abort teardown."""
    report = await lt.cancel_remaining_tasks()
    assert not asyncio.current_task().cancelled()
    assert report.clean


@pytest.mark.asyncio
async def test_a_parked_task_is_cancelled_and_awaited():
    async def parked():
        await asyncio.Event().wait()      # never resolves on its own

    t = asyncio.ensure_future(parked())
    await asyncio.sleep(0)
    report = await lt.cancel_remaining_tasks()
    assert report.cancelled >= 1
    assert report.finished >= 1
    assert t.cancelled()


@pytest.mark.asyncio
async def test_a_task_that_swallows_cancellation_is_named_not_awaited_forever():
    """The reason this is bounded and the stdlib's version is not.

    `asyncio.runners._cancel_all_tasks` gathers with no timeout, so one task
    like this hangs teardown forever -- and this process already paid for that
    once with a 1h50m executor wedge."""
    started = asyncio.Event()
    # Stubborn, but RELEASABLE. The first draft ignored cancellation
    # unconditionally, which made it genuinely immortal -- it survived into the
    # next test's loop and hung the suite. A test that models "ignores
    # cancellation" must still be killable by the test that made it, or the
    # suite inherits the exact pathology it is describing.
    release = asyncio.Event()

    async def stubborn():
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue                   # deliberately ignores it

    t = asyncio.ensure_future(stubborn())
    await started.wait()
    report = await lt.cancel_remaining_tasks(deadline_s=0.15)
    assert report.survivors, "a task that outlived the budget must be NAMED"
    assert any("stubborn" in s for s in report.survivors)
    assert "SURVIVED" in report.render()
    assert not report.clean

    release.set()
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(t, timeout=2.0)


@pytest.mark.asyncio
async def test_cleanup_spawned_during_cancellation_is_also_cancelled():
    """A cancelled task's `finally` may legitimately spawn cleanup work.

    One pass would leave exactly the tasks that clean up after themselves --
    which is why the sweep repeats."""
    spawned = {}

    async def child():
        await asyncio.Event().wait()

    async def parent():
        try:
            await asyncio.Event().wait()
        finally:
            spawned["t"] = asyncio.ensure_future(child())

    asyncio.ensure_future(parent())
    await asyncio.sleep(0)
    report = await lt.cancel_remaining_tasks()
    await asyncio.sleep(0)
    assert report.sweeps >= 2, "the second sweep is what catches the child"
    assert spawned["t"].cancelled() or spawned["t"].done()
    assert report.clean


@pytest.mark.asyncio
async def test_a_real_error_during_wind_down_is_surfaced_not_swallowed():
    """Teardown must not be the place bugs go to disappear."""
    # The error must happen DURING wind-down, not before it. A task that
    # merely raises soon is cancelled at its next suspension point and never
    # reaches the raise -- so the failure this guards is a `finally` that
    # blows up while unwinding, which replaces the CancelledError and is
    # exactly the case a teardown could quietly eat.
    async def boom():
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("wind-down failure")

    asyncio.ensure_future(boom())
    await asyncio.sleep(0)
    report = await lt.cancel_remaining_tasks()
    assert any("wind-down failure" in e for e in report.errors)
    assert not report.clean


@pytest.mark.asyncio
async def test_master_flag_off_is_byte_identical_legacy(monkeypatch):
    monkeypatch.setenv(lt.ENABLED_ENV, "0")
    async def parked():
        await asyncio.Event().wait()
    t = asyncio.ensure_future(parked())
    await asyncio.sleep(0)
    report = await lt.cancel_remaining_tasks()
    assert report.skipped
    assert report.cancelled == 0
    assert not t.cancelled(), "OFF must not cancel anything"
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t


@pytest.mark.asyncio
async def test_excluded_tasks_are_left_running():
    async def keeper():
        await asyncio.Event().wait()
    t = asyncio.ensure_future(keeper())
    await asyncio.sleep(0)
    report = await lt.cancel_remaining_tasks(exclude={t})
    assert not t.cancelled()
    assert report.cancelled == 0
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t


class TestConfiguration:
    def test_knobs_read_the_environment(self, monkeypatch):
        monkeypatch.setenv(lt.DEADLINE_ENV, "12.5")
        monkeypatch.setenv(lt.MAX_SWEEPS_ENV, "7")
        assert lt.cancel_deadline_s() == 12.5
        assert lt.max_sweeps() == 7

    def test_malformed_knobs_fall_back_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv(lt.DEADLINE_ENV, "banana")
        monkeypatch.setenv(lt.MAX_SWEEPS_ENV, "none")
        assert lt.cancel_deadline_s() == 5.0
        assert lt.max_sweeps() == 3

    def test_sweeps_can_never_be_zero(self, monkeypatch):
        """Zero sweeps would silently disable the phase while it reported
        itself enabled."""
        monkeypatch.setenv(lt.MAX_SWEEPS_ENV, "0")
        assert lt.max_sweeps() >= 1

    def test_negative_deadline_is_clamped(self, monkeypatch):
        monkeypatch.setenv(lt.DEADLINE_ENV, "-3")
        assert lt.cancel_deadline_s() == 0.0


@pytest.mark.asyncio
async def test_never_raises_on_a_hostile_task_object(monkeypatch):
    class _Hostile:
        def done(self): raise RuntimeError("boom")
        def cancel(self): raise RuntimeError("boom")
        def get_name(self): raise RuntimeError("boom")

    monkeypatch.setattr(lt.asyncio, "all_tasks", lambda *a, **k: {_Hostile()})
    report = await lt.cancel_remaining_tasks()
    assert isinstance(report, lt.TeardownReport)
