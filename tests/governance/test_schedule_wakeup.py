"""Slice 3 tests — WakeupController (ScheduleWakeup parity)."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Mapping

import pytest

from backend.core.ouroboros.governance.schedule_wakeup import (
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_FIRED,
    STATE_PENDING,
    WAKEUP_CONTROLLER_SCHEMA_VERSION,
    WakeupCapacityError,
    WakeupController,
    WakeupDelayError,
    WakeupError,
    WakeupOutcome,
    WakeupRequest,
    get_default_wakeup_controller,
    reset_default_wakeup_controller,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_wakeup_controller()
    yield
    reset_default_wakeup_controller()


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert WAKEUP_CONTROLLER_SCHEMA_VERSION == "schedule_wakeup.v1"


# ===========================================================================
# Schedule happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_schedule_and_fire_via_timer():
    fired: List[Dict[str, Any]] = []

    async def _handler(req: WakeupRequest, payload: Mapping[str, Any]) -> str:
        fired.append({
            "wakeup_id": req.wakeup_id, "payload": dict(payload),
        })
        return "handler-result"

    ctl = WakeupController(
        handler_resolver=lambda name: _handler if name == "tick" else None,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(
        handler_name="tick", delay_seconds=0.05,
        reason="test wake", payload={"k": "v"},
    )
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FIRED
    assert outcome.ok is True
    assert outcome.handler_result == "handler-result"
    assert len(fired) == 1
    assert fired[0]["payload"] == {"k": "v"}


@pytest.mark.asyncio
async def test_fire_now_triggers_immediate_fire():
    fired: List[str] = []

    async def _handler(req, payload):
        fired.append(req.wakeup_id)
        return None

    ctl = WakeupController(
        handler_resolver=lambda _name: _handler,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    fut = ctl.schedule(handler_name="h", delay_seconds=50.0, reason="long")
    pending_ids = ctl.pending_ids()
    outcome = await ctl.fire_now(pending_ids[0])
    assert outcome is not None
    assert outcome.state == STATE_FIRED
    via_future = await asyncio.wait_for(fut, timeout=1.0)
    assert via_future.state == STATE_FIRED


# ===========================================================================
# Cancellation
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_before_fire():
    async def _handler(req, payload):
        return "should not be called"

    ctl = WakeupController(
        handler_resolver=lambda _n: _handler,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    fut = ctl.schedule(handler_name="h", delay_seconds=10.0)
    pid = ctl.pending_ids()[0]
    outcome = ctl.cancel(pid, reason="operator cancelled")
    assert outcome is not None
    assert outcome.state == STATE_CANCELLED
    assert outcome.ok is False
    via_future = await asyncio.wait_for(fut, timeout=1.0)
    assert via_future.state == STATE_CANCELLED


def test_cancel_unknown_returns_none():
    ctl = WakeupController(min_delay_s=0.0, max_delay_s=10.0)
    assert ctl.cancel("wk-nope") is None


# ===========================================================================
# Terminal stickiness
# ===========================================================================


@pytest.mark.asyncio
async def test_double_cancel_is_noop():
    async def _handler(req, payload):
        return None

    ctl = WakeupController(
        handler_resolver=lambda _n: _handler,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    ctl.schedule(handler_name="h", delay_seconds=5.0)
    pid = ctl.pending_ids()[0]
    ctl.cancel(pid)
    # Second cancel returns None (already terminal)
    assert ctl.cancel(pid) is None


# ===========================================================================
# Delay bounds
# ===========================================================================


def test_delay_below_min_rejected():
    ctl = WakeupController(min_delay_s=60.0, max_delay_s=3600.0)
    with pytest.raises(WakeupDelayError):
        ctl.schedule(handler_name="h", delay_seconds=30.0)


def test_delay_above_max_rejected():
    ctl = WakeupController(min_delay_s=60.0, max_delay_s=3600.0)
    with pytest.raises(WakeupDelayError):
        ctl.schedule(handler_name="h", delay_seconds=7200.0)


def test_delay_at_exact_bounds_accepted():
    ctl = WakeupController(min_delay_s=60.0, max_delay_s=3600.0)
    ctl.schedule(handler_name="h", delay_seconds=60.0)
    ctl.schedule(handler_name="h", delay_seconds=3600.0)


def test_min_greater_than_max_rejected():
    with pytest.raises(WakeupError):
        WakeupController(min_delay_s=100.0, max_delay_s=50.0)


# ===========================================================================
# Capacity cap
# ===========================================================================


def test_capacity_enforced():
    ctl = WakeupController(
        min_delay_s=0.0, max_delay_s=3600.0, max_pending=2,
    )
    ctl.schedule(handler_name="h", delay_seconds=100.0)
    ctl.schedule(handler_name="h", delay_seconds=100.0)
    with pytest.raises(WakeupCapacityError):
        ctl.schedule(handler_name="h", delay_seconds=100.0)


# ===========================================================================
# Fail-closed on missing / raising handlers
# ===========================================================================


@pytest.mark.asyncio
async def test_no_resolver_fires_with_failed_state():
    ctl = WakeupController(min_delay_s=0.0, max_delay_s=10.0)
    fut = ctl.schedule(handler_name="h", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FAILED
    assert outcome.ok is False
    assert "no_handler_resolver" in (outcome.error or "")


@pytest.mark.asyncio
async def test_resolver_returns_none_fires_with_failed_state():
    ctl = WakeupController(
        handler_resolver=lambda _n: None,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(handler_name="missing", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FAILED
    assert "no_handler" in (outcome.error or "")


@pytest.mark.asyncio
async def test_resolver_raising_fires_with_failed_state():
    def _bad_resolver(name):
        raise RuntimeError("resolver boom")

    ctl = WakeupController(
        handler_resolver=_bad_resolver,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(handler_name="x", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FAILED
    assert "resolver_raise" in (outcome.error or "")


@pytest.mark.asyncio
async def test_handler_raising_fires_with_failed_state():
    async def _boom(req, payload):
        raise ValueError("handler boom")

    ctl = WakeupController(
        handler_resolver=lambda _n: _boom,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(handler_name="x", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FAILED
    assert outcome.ok is False
    assert "handler_raise:ValueError" in (outcome.error or "")
    assert "handler boom" in (outcome.error or "")


# ===========================================================================
# Sync handlers supported
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_handler_also_supported():
    def _sync(req, payload):
        return "sync result"

    ctl = WakeupController(
        handler_resolver=lambda _n: _sync,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(handler_name="x", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FIRED
    assert outcome.handler_result == "sync result"


# ===========================================================================
# Introspection
# ===========================================================================


@pytest.mark.asyncio
async def test_pending_count_and_ids():
    ctl = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    ctl.schedule(handler_name="a", delay_seconds=50.0)
    ctl.schedule(handler_name="b", delay_seconds=50.0)
    assert ctl.pending_count() == 2
    assert len(ctl.pending_ids()) == 2


@pytest.mark.asyncio
async def test_snapshot_projects_bounded_fields():
    ctl = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    ctl.schedule(
        handler_name="h", delay_seconds=60.0, reason="test",
        payload={"secret_key": "sk-confidential", "user": "alice"},
    )
    snap = ctl.snapshot_all()[0]
    assert snap["handler_name"] == "h"
    assert snap["state"] == STATE_PENDING
    # payload keys exposed; values NOT
    assert "user" in snap["payload_keys"]
    assert "sk-confidential" not in str(snap)


@pytest.mark.asyncio
async def test_history_records_resolved_outcomes():
    ctl = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    fut = ctl.schedule(handler_name="h", delay_seconds=0.05)
    await asyncio.wait_for(fut, timeout=3.0)
    h = ctl.history()
    assert len(h) == 1
    assert h[0]["state"] == STATE_FIRED


# ===========================================================================
# Listener hooks (Slice 4 precondition)
# ===========================================================================


@pytest.mark.asyncio
async def test_on_change_emits_scheduled_and_fired():
    events: List[str] = []

    async def _handler(req, payload):
        return None

    ctl = WakeupController(
        handler_resolver=lambda _n: _handler,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    ctl.on_change(lambda e: events.append(e["event_type"]))
    fut = ctl.schedule(handler_name="h", delay_seconds=0.05)
    await asyncio.wait_for(fut, timeout=3.0)
    assert "wakeup_scheduled" in events
    assert "wakeup_fired" in events


@pytest.mark.asyncio
async def test_on_change_emits_cancelled_on_cancel():
    events: List[str] = []
    ctl = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=100.0,
    )
    ctl.on_change(lambda e: events.append(e["event_type"]))
    ctl.schedule(handler_name="h", delay_seconds=50.0)
    ctl.cancel(ctl.pending_ids()[0])
    assert "wakeup_cancelled" in events


@pytest.mark.asyncio
async def test_listener_exception_does_not_break_controller():
    def _bad(_e: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    ctl = WakeupController(
        handler_resolver=lambda _n: lambda r, p: None,
        min_delay_s=0.0, max_delay_s=10.0,
    )
    ctl.on_change(_bad)
    fut = ctl.schedule(handler_name="h", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FIRED


# ===========================================================================
# Singleton + JobRegistry resolver wiring
# ===========================================================================


@pytest.mark.asyncio
async def test_default_controller_wires_job_registry_resolver(monkeypatch):
    """Default controller auto-wires to :func:`JobRegistry.get_handler`."""
    from backend.core.ouroboros.governance.schedule_job import (
        HandlerSource,
        get_default_job_registry,
        reset_default_job_registry,
    )
    reset_default_job_registry()
    fired: List[str] = []

    async def _op_handler(req, payload):
        fired.append(req.wakeup_id)
        return "ok"

    reg = get_default_job_registry()
    reg.register_handler(
        "opbeat", _op_handler, source=HandlerSource.OPERATOR,
    )
    # Set delays very permissive so this test is instant
    monkeypatch.setenv("JARVIS_WAKEUP_MIN_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_WAKEUP_MAX_DELAY_S", "10")
    reset_default_wakeup_controller()
    ctl = get_default_wakeup_controller()
    fut = ctl.schedule(handler_name="opbeat", delay_seconds=0.05)
    outcome = await asyncio.wait_for(fut, timeout=3.0)
    assert outcome.state == STATE_FIRED
    assert fired == [outcome.wakeup_id]
    reset_default_job_registry()


# ===========================================================================
# Env-var bounds read from environment
# ===========================================================================


def test_env_bound_min_delay(monkeypatch):
    monkeypatch.setenv("JARVIS_WAKEUP_MIN_DELAY_S", "5")
    monkeypatch.setenv("JARVIS_WAKEUP_MAX_DELAY_S", "50")
    from backend.core.ouroboros.governance.schedule_wakeup import (
        _default_min_delay_s, _default_max_delay_s,
    )
    assert _default_min_delay_s() == 5.0
    assert _default_max_delay_s() == 50.0


def test_env_bound_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_WAKEUP_MIN_DELAY_S", "not-a-number")
    from backend.core.ouroboros.governance.schedule_wakeup import (
        _default_min_delay_s,
    )
    assert _default_min_delay_s() == 60.0


# ===========================================================================
# Empty handler name rejected
# ===========================================================================


def test_empty_handler_name_rejected():
    ctl = WakeupController(min_delay_s=0.0, max_delay_s=10.0)
    with pytest.raises(WakeupError):
        ctl.schedule(handler_name="", delay_seconds=5.0)


def test_whitespace_handler_name_rejected():
    ctl = WakeupController(min_delay_s=0.0, max_delay_s=10.0)
    with pytest.raises(WakeupError):
        ctl.schedule(handler_name="   ", delay_seconds=5.0)
