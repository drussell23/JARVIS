"""Slice 4 tests — ScheduleRunner + /schedule /wakeup REPL."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Mapping

import pytest

from backend.core.ouroboros.governance.schedule_expression import (
    ScheduleExpression,
)
from backend.core.ouroboros.governance.schedule_job import (
    HandlerSource,
    JobRegistry,
    get_default_job_registry,
    reset_default_job_registry,
)
from backend.core.ouroboros.governance.schedule_runner import (
    SCHEDULE_RUNNER_SCHEMA_VERSION,
    ScheduleRunner,
    dispatch_schedule_command,
    schedule_runner_enabled,
)
from backend.core.ouroboros.governance.schedule_wakeup import (
    WakeupController,
    reset_default_wakeup_controller,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith(("JARVIS_SCHEDULE_", "JARVIS_WAKEUP_")):
            monkeypatch.delenv(key, raising=False)
    reset_default_job_registry()
    reset_default_wakeup_controller()
    yield
    reset_default_job_registry()
    reset_default_wakeup_controller()


# ===========================================================================
# Env + schema
# ===========================================================================


def test_schema_version_stable():
    assert SCHEDULE_RUNNER_SCHEMA_VERSION == "schedule_runner.v1"


def test_runner_enabled_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_SCHEDULE_RUNNER_ENABLED", raising=False)
    assert schedule_runner_enabled() is False


def test_runner_enabled_explicit_true(monkeypatch):
    monkeypatch.setenv("JARVIS_SCHEDULE_RUNNER_ENABLED", "true")
    assert schedule_runner_enabled() is True


# ===========================================================================
# ScheduleRunner.tick fires due jobs
# ===========================================================================


@pytest.mark.asyncio
async def test_tick_fires_due_job():
    reg = JobRegistry()
    fired: List[str] = []

    async def _handler(job, payload):
        fired.append(job.job_id)

    reg.register_handler("tick", _handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    # Schedule a job with next_run_ts in the past so tick fires it
    now = time.time()
    job = reg.add_job(
        handler_name="tick", expression=expr, now_ts=now - 7200,
    )
    runner = ScheduleRunner(registry=reg, tick_interval_s=60.0)
    fired_list = await runner.tick(now=now)
    assert len(fired_list) == 1
    assert fired[0] == job.job_id
    # run_count advanced
    assert reg.get_job(job.job_id).run_count == 1


@pytest.mark.asyncio
async def test_tick_skips_future_job():
    reg = JobRegistry()

    async def _handler(job, payload):
        pass

    reg.register_handler("tick", _handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@daily")
    now = time.time()
    reg.add_job(handler_name="tick", expression=expr, now_ts=now)
    runner = ScheduleRunner(registry=reg)
    fired = await runner.tick(now=now)
    assert fired == []


@pytest.mark.asyncio
async def test_tick_skips_disabled_job():
    reg = JobRegistry()

    async def _handler(job, payload):
        pass

    reg.register_handler("tick", _handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(
        handler_name="tick", expression=expr, now_ts=time.time() - 7200,
    )
    reg.disable_job(job.job_id)
    runner = ScheduleRunner(registry=reg)
    fired = await runner.tick(now=time.time())
    assert fired == []


@pytest.mark.asyncio
async def test_tick_continues_after_handler_raises():
    reg = JobRegistry()
    fired: List[str] = []

    async def _good(job, payload):
        fired.append(job.job_id)

    async def _bad(job, payload):
        raise RuntimeError("boom")

    reg.register_handler("good", _good, source=HandlerSource.OPERATOR)
    reg.register_handler("bad", _bad, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    past = time.time() - 7200
    j_bad = reg.add_job(
        handler_name="bad", expression=expr, now_ts=past,
    )
    j_good = reg.add_job(
        handler_name="good", expression=expr, now_ts=past,
    )
    runner = ScheduleRunner(registry=reg)
    await runner.tick(now=time.time())
    # Both fired; good ran successfully
    assert j_good.job_id in fired
    assert runner.stats()["errors_total"] == 1
    assert runner.stats()["fires_total"] == 1


@pytest.mark.asyncio
async def test_tick_handles_missing_handler():
    reg = JobRegistry()

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(
        handler_name="tick", expression=expr, now_ts=time.time() - 7200,
    )
    # Unregister the handler after the job was added
    reg.unregister_handler("tick")
    runner = ScheduleRunner(registry=reg)
    fired = await runner.tick(now=time.time())
    # Job was considered due and attempted, but handler missing
    assert len(fired) == 1
    assert runner.stats()["errors_total"] == 1


@pytest.mark.asyncio
async def test_tick_advances_next_run():
    reg = JobRegistry()

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    past = time.time() - 7200
    job = reg.add_job(handler_name="tick", expression=expr, now_ts=past)
    original_next = job.next_run_ts
    runner = ScheduleRunner(registry=reg)
    await runner.tick(now=time.time())
    updated = reg.get_job(job.job_id)
    assert updated.next_run_ts is not None
    assert updated.next_run_ts > original_next


# ===========================================================================
# Runner lifecycle (start/stop)
# ===========================================================================


@pytest.mark.asyncio
async def test_start_stop_idempotent():
    runner = ScheduleRunner(tick_interval_s=0.05)
    await runner.start()
    assert runner.is_running
    await runner.start()  # second start is no-op
    assert runner.is_running
    await runner.stop()
    assert not runner.is_running
    await runner.stop()  # second stop is no-op


@pytest.mark.asyncio
async def test_runner_fires_through_full_loop():
    reg = JobRegistry()
    fired: List[str] = []

    async def _handler(job, payload):
        fired.append(job.job_id)

    reg.register_handler("tick", _handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(
        handler_name="tick", expression=expr, now_ts=time.time() - 7200,
    )
    runner = ScheduleRunner(registry=reg, tick_interval_s=0.05)
    await runner.start()
    # Wait a few tick cycles for the first fire to land
    for _ in range(50):
        if fired:
            break
        await asyncio.sleep(0.02)
    await runner.stop()
    assert job.job_id in fired


# ===========================================================================
# Runner stats
# ===========================================================================


def test_stats_projection_shape():
    runner = ScheduleRunner()
    s = runner.stats()
    assert s["schema_version"] == "schedule_runner.v1"
    assert "is_running" in s
    assert "fires_total" in s
    assert "errors_total" in s


# ===========================================================================
# REPL: /schedule commands
# ===========================================================================


def test_repl_unknown_command_falls_through():
    result = dispatch_schedule_command("/plan mode on")
    assert result.matched is False


def test_repl_schedule_list_empty():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)
    result = dispatch_schedule_command(
        "/schedule", registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert "no scheduled jobs" in result.text.lower()


def test_repl_schedule_handlers_list():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler(
        "tick", _h, source=HandlerSource.OPERATOR,
        description="test handler",
    )
    result = dispatch_schedule_command(
        "/schedule handlers", registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert "tick" in result.text
    assert "test handler" in result.text


def test_repl_schedule_add_round_trip():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    result = dispatch_schedule_command(
        '/schedule add tick "@hourly" heartbeat',
        registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert len(reg.list_jobs()) == 1
    job = reg.list_jobs()[0]
    assert job.handler_name == "tick"
    assert job.expression.canonical_cron == "0 * * * *"


def test_repl_schedule_add_every_monday():
    """The gap-writeup phrase must work through the REPL."""
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("check", _h, source=HandlerSource.OPERATOR)
    result = dispatch_schedule_command(
        '/schedule add check "every monday at 9am" weekly-checkin',
        registry=reg, wakeup=wak,
    )
    assert result.ok is True
    job = reg.list_jobs()[0]
    assert job.expression.canonical_cron == "0 9 * * 1"


def test_repl_schedule_add_unknown_handler():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)
    result = dispatch_schedule_command(
        '/schedule add unregistered "@hourly"',
        registry=reg, wakeup=wak,
    )
    assert result.ok is False


def test_repl_schedule_add_bad_expression():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    result = dispatch_schedule_command(
        '/schedule add tick "this is not valid"',
        registry=reg, wakeup=wak,
    )
    assert result.ok is False


def test_repl_schedule_show_by_id():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    job = reg.add_job(
        handler_name="tick",
        expression=ScheduleExpression.from_phrase("@hourly"),
    )
    result = dispatch_schedule_command(
        f"/schedule show {job.job_id}",
        registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert job.job_id in result.text


def test_repl_schedule_remove():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    job = reg.add_job(
        handler_name="tick",
        expression=ScheduleExpression.from_phrase("@hourly"),
    )
    result = dispatch_schedule_command(
        f"/schedule remove {job.job_id}",
        registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert reg.get_job(job.job_id) is None


def test_repl_schedule_enable_disable():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _h(job, payload):
        pass

    reg.register_handler("tick", _h, source=HandlerSource.OPERATOR)
    job = reg.add_job(
        handler_name="tick",
        expression=ScheduleExpression.from_phrase("@hourly"),
    )
    r1 = dispatch_schedule_command(
        f"/schedule disable {job.job_id}",
        registry=reg, wakeup=wak,
    )
    assert r1.ok
    assert reg.get_job(job.job_id).enabled is False
    r2 = dispatch_schedule_command(
        f"/schedule enable {job.job_id}",
        registry=reg, wakeup=wak,
    )
    assert r2.ok
    assert reg.get_job(job.job_id).enabled is True


def test_repl_schedule_help():
    result = dispatch_schedule_command("/schedule help")
    assert result.ok is True
    assert "/schedule" in result.text
    assert "/wakeup" in result.text


# ===========================================================================
# REPL: /wakeup commands
# ===========================================================================


def test_repl_wakeup_list_empty():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)
    result = dispatch_schedule_command(
        "/wakeup list", registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert "no pending wakeups" in result.text.lower()


def test_repl_wakeup_schedule():
    reg = JobRegistry()
    wak = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    result = dispatch_schedule_command(
        '/wakeup alert 30 "look at test output"',
        registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert "wk-" in result.text
    assert wak.pending_count() == 1


def test_repl_wakeup_cancel():
    reg = JobRegistry()
    wak = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    dispatch_schedule_command(
        "/wakeup alert 30", registry=reg, wakeup=wak,
    )
    wid = wak.pending_ids()[0]
    result = dispatch_schedule_command(
        f"/wakeup cancel {wid}", registry=reg, wakeup=wak,
    )
    assert result.ok is True
    assert wak.pending_count() == 0


def test_repl_wakeup_cancel_unknown():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)
    result = dispatch_schedule_command(
        "/wakeup cancel wk-does-not-exist",
        registry=reg, wakeup=wak,
    )
    assert result.ok is False


def test_repl_wakeup_bad_delay():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)
    result = dispatch_schedule_command(
        "/wakeup h not-a-number",
        registry=reg, wakeup=wak,
    )
    assert result.ok is False


def test_repl_wakeup_out_of_range_delay():
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=60.0, max_delay_s=300.0)
    result = dispatch_schedule_command(
        "/wakeup h 7200",  # > max
        registry=reg, wakeup=wak,
    )
    assert result.ok is False
