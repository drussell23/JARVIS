"""Nothing dies quietly.

Every boot logged this, and it was the whole message::

    [HUD-Gov] GovernedLoopService failed:
    [HUD] Ouroboros governance DEGRADED — partial pipeline

Two defects shared one line: `str(asyncio.TimeoutError())` is the empty
string, and `asyncio.shield` left the real work running with nobody watching
how it ended.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    runtime_health_sensor as R,
)
from backend.core.ouroboros.telemetry import task_harvester as TH
from backend.core.ouroboros.telemetry.task_harvester import (
    TaskHarvester, describe_exception, format_traceback,
)


class _Sensor:
    def __init__(self):
        self.got = []

    async def report(self, finding):
        self.got.append(finding)
        return 1


@pytest.fixture(autouse=True)
def _clean():
    TH.reset_task_harvester()
    R._LIVE_SENSOR[0] = None
    yield
    TH.reset_task_harvester()
    R._LIVE_SENSOR[0] = None


# ── The empty message ───────────────────────────────────────────────────────

def test_a_timeout_no_longer_renders_as_nothing():
    """THE BUG. `%s` prints an exception's MESSAGE, and this class has none —
    so a 30-second timeout printed as silence on every single boot."""
    assert str(asyncio.TimeoutError()) == ""
    assert describe_exception(asyncio.TimeoutError()) == "TimeoutError"


@pytest.mark.parametrize("exc,expect", [
    (asyncio.TimeoutError(), "TimeoutError"),
    (StopIteration(), "StopIteration"),
    (RuntimeError("boom"), "RuntimeError: boom"),
    (KeyError("k"), "KeyError: 'k'"),
])
def test_no_exception_can_describe_itself_as_nothing(exc, expect):
    """Not just TimeoutError. Any class without a message does this, so the
    fix leads with the TYPE rather than special-casing one culprit."""
    assert describe_exception(exc) == expect
    assert describe_exception(exc).strip()


def test_the_full_traceback_is_kept_not_just_the_message():
    try:
        raise ValueError("deep")
    except ValueError as e:
        text = format_traceback(e)
    assert "ValueError: deep" in text and "line" in text


# ── The black hole ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_task_that_dies_unobserved_is_still_reported():
    """`shield` abandons the WAIT, not the WORK. The shielded task carried on
    and its exception went to the loop's default handler, where the traceback
    was lost."""
    h = TaskHarvester()

    async def boom():
        raise RuntimeError("governance exploded")

    t = asyncio.ensure_future(boom())
    h.watch(t, what="GovernedLoopService.start")
    with pytest.raises(RuntimeError):
        await t
    await asyncio.sleep(0)

    assert h.stats()["harvested"] == 1


@pytest.mark.asyncio
async def test_a_task_that_succeeds_reports_nothing():
    h = TaskHarvester()

    async def fine():
        return 42

    t = asyncio.ensure_future(fine())
    h.watch(t, what="ok")
    assert await t == 42
    await asyncio.sleep(0)
    assert h.stats()["harvested"] == 0


@pytest.mark.asyncio
async def test_cancellation_is_a_decision_not_a_failure():
    h = TaskHarvester()
    t = asyncio.ensure_future(asyncio.sleep(5))
    h.watch(t, what="cancelled")
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0)
    assert h.stats()["harvested"] == 0


@pytest.mark.asyncio
async def test_watching_never_breaks_the_thing_it_watches():
    h = TaskHarvester()
    assert h.watch(None, what="nothing") is None
    assert h.watch("not a task", what="junk") == "not a task"


# ── Holding what died before anyone could listen ────────────────────────────

@pytest.mark.asyncio
async def test_a_failure_during_boot_is_held_until_intake_exists():
    """THE EDGE CASE THAT MATTERS.

    The failure worth catching is a governance service dying at startup —
    which happens while the intake layer is still coming up. A harvester that
    merely looked the sensor up would drop the one traceback it was built for.
    """
    h = TaskHarvester()
    TH._HARVESTER = h

    h.record(RuntimeError("died during boot"), what="EarlyService")
    assert h.stats()["pending"] == 1, "it should be held, not dropped"
    assert h.stats()["routed"] == 0

    sensor = _Sensor()
    R.register_runtime_health_sensor(sensor)     # intake finally comes up
    await asyncio.sleep(0.05)

    assert len(sensor.got) == 1
    assert "died during boot" in sensor.got[0].summary
    assert h.stats()["pending"] == 0


@pytest.mark.asyncio
async def test_the_finding_carries_the_traceback_for_o_plus_v():
    """O+V is being asked to FIX this. A summary without a traceback is a
    complaint; with one it is a bug report."""
    h = TaskHarvester()
    TH._HARVESTER = h
    try:
        raise ValueError("the actual cause")
    except ValueError as e:
        h.record(e, what="Thing")

    sensor = _Sensor()
    R.register_runtime_health_sensor(sensor)
    await asyncio.sleep(0.05)

    finding = sensor.got[0]
    assert finding.category == "background_task_failure"
    assert "the actual cause" in finding.details["traceback"]
    assert finding.severity == "high"


@pytest.mark.asyncio
async def test_the_buffer_is_bounded(monkeypatch):
    """A boot that fails a hundred times has one problem, not a hundred."""
    monkeypatch.setenv("JARVIS_TASK_HARVESTER_BUFFER", "5")
    h = TaskHarvester()
    TH._HARVESTER = h
    for i in range(50):
        h.record(RuntimeError(f"fail {i}"), what=f"svc{i}")
    assert h.stats()["pending"] == 5


@pytest.mark.asyncio
async def test_it_routes_straight_through_once_intake_is_up():
    h = TaskHarvester()
    TH._HARVESTER = h
    sensor = _Sensor()
    R.register_runtime_health_sensor(sensor)

    h.record(RuntimeError("later failure"), what="LateService")
    await asyncio.sleep(0.05)

    assert len(sensor.got) == 1
    assert h.stats()["pending"] == 0


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_HARVESTER_ENABLED", "false")
    h = TaskHarvester()
    t = object()
    assert h.watch(t, what="x") is t
    assert h.stats()["watched"] == 0


# ── The sensor's push entry point ───────────────────────────────────────────

def test_report_and_scan_share_one_emitter():
    """Two copies of envelope construction would eventually disagree about
    what a finding looks like, and the push path is the one carrying
    tracebacks."""
    import inspect
    assert "_emit" in inspect.getsource(R.RuntimeHealthSensor.scan_once)
    assert "_emit" in inspect.getsource(R.RuntimeHealthSensor.report)


def test_an_absent_sensor_answers_none_rather_than_raising():
    assert R.get_runtime_health_sensor() is None
