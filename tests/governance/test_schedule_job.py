"""Slice 2 tests — ScheduledJob + JobRegistry."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Mapping

import pytest

from backend.core.ouroboros.governance.schedule_expression import (
    ScheduleExpression,
)
from backend.core.ouroboros.governance.schedule_job import (
    Handler,
    HandlerAuthorityError,
    HandlerSource,
    JobRegistry,
    JobRegistryError,
    ScheduledJob,
    get_default_job_registry,
    reset_default_job_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_default_job_registry()
    yield
    reset_default_job_registry()


async def _noop_handler(_job, _payload):
    return None


# ===========================================================================
# Handler registration (§1 authority)
# ===========================================================================


def test_register_operator_handler():
    reg = JobRegistry()
    r = reg.register_handler(
        "backup", _noop_handler, source=HandlerSource.OPERATOR,
        description="nightly backup",
    )
    assert r.name == "backup"
    assert r.source == "operator"
    assert reg.has_handler("backup")


def test_register_orchestrator_handler():
    reg = JobRegistry()
    reg.register_handler(
        "tick", _noop_handler, source=HandlerSource.ORCHESTRATOR,
    )
    assert reg.has_handler("tick")


def test_register_model_source_rejected():
    """§1 authority boundary: model cannot register handlers."""
    reg = JobRegistry()

    class FakeSource(str):
        pass

    fake = FakeSource("model")
    with pytest.raises(HandlerAuthorityError):
        reg.register_handler(
            "evil", _noop_handler, source=fake,  # type: ignore[arg-type]
        )


def test_register_empty_name_rejected():
    reg = JobRegistry()
    with pytest.raises(JobRegistryError):
        reg.register_handler("", _noop_handler, source=HandlerSource.OPERATOR)


def test_register_non_callable_rejected():
    reg = JobRegistry()
    with pytest.raises(JobRegistryError):
        reg.register_handler(
            "x", "not a callable",  # type: ignore[arg-type]
            source=HandlerSource.OPERATOR,
        )


def test_register_duplicate_name_rejected():
    reg = JobRegistry()
    reg.register_handler(
        "x", _noop_handler, source=HandlerSource.OPERATOR,
    )
    with pytest.raises(JobRegistryError):
        reg.register_handler(
            "x", _noop_handler, source=HandlerSource.OPERATOR,
        )


def test_unregister_handler_round_trip():
    reg = JobRegistry()
    reg.register_handler("x", _noop_handler, source=HandlerSource.OPERATOR)
    assert reg.unregister_handler("x") is True
    assert reg.unregister_handler("x") is False
    assert not reg.has_handler("x")


def test_handler_cap_enforced():
    reg = JobRegistry(max_handlers=2)
    reg.register_handler("a", _noop_handler, source=HandlerSource.OPERATOR)
    reg.register_handler("b", _noop_handler, source=HandlerSource.OPERATOR)
    with pytest.raises(JobRegistryError):
        reg.register_handler(
            "c", _noop_handler, source=HandlerSource.OPERATOR,
        )


def test_list_handlers_returns_metadata():
    reg = JobRegistry()
    reg.register_handler(
        "a", _noop_handler, source=HandlerSource.OPERATOR,
        description="first",
    )
    reg.register_handler(
        "b", _noop_handler, source=HandlerSource.ORCHESTRATOR,
        description="second",
    )
    metas = reg.list_handlers()
    assert {m.name for m in metas} == {"a", "b"}
    assert {m.description for m in metas} == {"first", "second"}


# ===========================================================================
# Job lifecycle
# ===========================================================================


def test_add_job_happy_path():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(
        handler_name="tick", expression=expr,
        description="heartbeat",
    )
    assert isinstance(job, ScheduledJob)
    assert job.handler_name == "tick"
    assert job.enabled is True
    assert job.run_count == 0
    assert job.next_run_ts is not None
    assert job.created_at_iso


def test_add_job_unknown_handler_rejected():
    reg = JobRegistry()
    expr = ScheduleExpression.from_phrase("@hourly")
    with pytest.raises(JobRegistryError):
        reg.add_job(handler_name="not-registered", expression=expr)


def test_add_job_bad_expression_rejected():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    with pytest.raises(JobRegistryError):
        reg.add_job(
            handler_name="tick",
            expression="not-an-expression",  # type: ignore[arg-type]
        )


def test_add_job_cap_enforced():
    reg = JobRegistry(max_jobs=2)
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    reg.add_job(handler_name="tick", expression=expr)
    reg.add_job(handler_name="tick", expression=expr)
    with pytest.raises(JobRegistryError):
        reg.add_job(handler_name="tick", expression=expr)


def test_add_job_payload_isolated_from_caller():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    original_payload: Dict[str, Any] = {"a": 1}
    job = reg.add_job(
        handler_name="tick", expression=expr, payload=original_payload,
    )
    # Caller mutating their dict must not affect the job's payload.
    original_payload["a"] = 999
    original_payload["b"] = "new"
    assert job.payload.get("a") == 1
    assert "b" not in job.payload


def test_remove_job():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(handler_name="tick", expression=expr)
    assert reg.remove_job(job.job_id) is True
    assert reg.remove_job(job.job_id) is False
    assert reg.get_job(job.job_id) is None


def test_enable_disable_round_trip():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(handler_name="tick", expression=expr)
    disabled = reg.disable_job(job.job_id)
    assert disabled is not None
    assert disabled.enabled is False
    enabled = reg.enable_job(job.job_id)
    assert enabled is not None
    assert enabled.enabled is True


def test_enable_disable_unknown_returns_none():
    reg = JobRegistry()
    assert reg.enable_job("job-nope") is None
    assert reg.disable_job("job-nope") is None


def test_list_jobs_sorted_by_next_run():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr_hourly = ScheduleExpression.from_phrase("@hourly")
    expr_daily = ScheduleExpression.from_phrase("@daily")
    # Add daily first then hourly
    j_daily = reg.add_job(handler_name="tick", expression=expr_daily)
    j_hourly = reg.add_job(handler_name="tick", expression=expr_hourly)
    jobs = reg.list_jobs()
    # hourly should sort ahead of daily (fires sooner)
    assert jobs[0].job_id == j_hourly.job_id
    assert jobs[1].job_id == j_daily.job_id


def test_list_jobs_enabled_only_filter():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    a = reg.add_job(handler_name="tick", expression=expr)
    b = reg.add_job(handler_name="tick", expression=expr)
    reg.disable_job(b.job_id)
    enabled = reg.list_jobs(enabled_only=True)
    assert [j.job_id for j in enabled] == [a.job_id]


# ===========================================================================
# record_fire advances counters
# ===========================================================================


def test_record_fire_advances_run_count():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(handler_name="tick", expression=expr)
    now = time.time()
    updated = reg.record_fire(job.job_id, fired_ts=now)
    assert updated is not None
    assert updated.run_count == 1
    assert updated.last_run_ts == now
    assert updated.next_run_ts is not None
    assert updated.next_run_ts > now


def test_record_fire_disables_when_max_runs_reached():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(handler_name="tick", expression=expr, max_runs=1)
    updated = reg.record_fire(job.job_id, fired_ts=time.time())
    assert updated is not None
    assert updated.run_count == 1
    assert updated.exhausted is True
    assert updated.enabled is False


def test_record_fire_unknown_returns_none():
    reg = JobRegistry()
    assert reg.record_fire("job-nope") is None


# ===========================================================================
# Immutability invariants
# ===========================================================================


def test_scheduled_job_is_frozen():
    expr = ScheduleExpression.from_phrase("@hourly")
    job = ScheduledJob(
        job_id="j1", handler_name="x", expression=expr,
    )
    with pytest.raises(Exception):
        job.enabled = False  # type: ignore[misc]


def test_with_run_recorded_returns_new_instance():
    expr = ScheduleExpression.from_phrase("@hourly")
    job = ScheduledJob(
        job_id="j1", handler_name="x", expression=expr,
    )
    updated = job.with_run_recorded(
        fired_ts=100.0, next_run_ts=200.0,
    )
    assert updated is not job
    assert updated.last_run_ts == 100.0
    assert updated.next_run_ts == 200.0
    assert updated.run_count == job.run_count + 1


def test_exhausted_property():
    expr = ScheduleExpression.from_phrase("@hourly")
    job = ScheduledJob(
        job_id="j1", handler_name="x", expression=expr, max_runs=2,
    )
    assert job.exhausted is False
    one = job.with_run_recorded(fired_ts=1.0, next_run_ts=None)
    assert one.exhausted is False
    two = one.with_run_recorded(fired_ts=2.0, next_run_ts=None)
    assert two.exhausted is True


# ===========================================================================
# Listener hooks
# ===========================================================================


def test_on_change_fires_on_handler_and_job_events():
    reg = JobRegistry()
    events: List[Dict[str, Any]] = []
    reg.on_change(events.append)
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    job = reg.add_job(handler_name="tick", expression=expr)
    reg.disable_job(job.job_id)
    reg.remove_job(job.job_id)
    reg.unregister_handler("tick")
    kinds = [e["event_type"] for e in events]
    assert kinds == [
        "handler_registered",
        "job_added",
        "job_disabled",
        "job_removed",
        "handler_unregistered",
    ]


def test_listener_exception_does_not_break_registry():
    reg = JobRegistry()

    def _bad(_p: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    reg.on_change(_bad)
    # Must not raise
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    assert reg.has_handler("tick")


def test_on_change_unsub_works():
    reg = JobRegistry()
    events: List[Dict[str, Any]] = []
    unsub = reg.on_change(events.append)
    reg.register_handler("a", _noop_handler, source=HandlerSource.OPERATOR)
    unsub()
    reg.register_handler("b", _noop_handler, source=HandlerSource.OPERATOR)
    assert len(events) == 1  # only 'a' event captured


# ===========================================================================
# Projection shape (Slice 4 precondition)
# ===========================================================================


def test_project_job_excludes_payload_values():
    """Payload is user data — project only KEYS for SSE / HTTP.

    The sensitive piece is the VALUE ('sk-hidden-value'), not the key
    name; the projection must carry keys but never values. Raw
    'payload' key must not appear.
    """
    expr = ScheduleExpression.from_phrase("@hourly")
    job = ScheduledJob(
        job_id="j1", handler_name="x", expression=expr,
        payload={"secret": "sk-hidden-value", "path": "backend/x.py"},
    )
    projection = JobRegistry.project_job(job)
    assert projection["payload_keys"] == ["path", "secret"]
    # Raw 'payload' key NOT in the projection
    assert "payload" not in projection
    # Sensitive VALUE does not leak
    assert "sk-hidden-value" not in str(projection)
    assert "backend/x.py" not in str(projection)


def test_project_job_schema_version_present():
    expr = ScheduleExpression.from_phrase("@hourly")
    job = ScheduledJob(
        job_id="j1", handler_name="x", expression=expr,
    )
    projection = JobRegistry.project_job(job)
    assert projection["schema_version"] == "schedule_job.v1"


# ===========================================================================
# Singletons
# ===========================================================================


def test_default_registry_singleton():
    a = get_default_job_registry()
    b = get_default_job_registry()
    assert a is b


def test_reset_default_clears():
    reg = get_default_job_registry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    reset_default_job_registry()
    reg2 = get_default_job_registry()
    assert not reg2.has_handler("tick")


# ===========================================================================
# Thread safety
# ===========================================================================


def test_concurrent_job_add_safe():
    reg = JobRegistry(max_jobs=10000)
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    expr = ScheduleExpression.from_phrase("@hourly")
    threads: List[threading.Thread] = []

    def _writer() -> None:
        for _ in range(20):
            reg.add_job(handler_name="tick", expression=expr)

    for _ in range(4):
        t = threading.Thread(target=_writer)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    # No exceptions + all 80 jobs recorded
    assert len(reg.list_jobs()) == 80


# ===========================================================================
# handler name must resolve
# ===========================================================================


def test_get_handler_returns_callable():
    reg = JobRegistry()
    reg.register_handler("tick", _noop_handler, source=HandlerSource.OPERATOR)
    h = reg.get_handler("tick")
    assert h is _noop_handler


def test_get_handler_returns_none_for_unknown():
    reg = JobRegistry()
    assert reg.get_handler("nope") is None
