"""Fleet subagents must reach the cockpit, not just debug.log.

The fleet ran: eighty `[ExploreAgent]` events in one session, agents visible
in StallAttributor stacks, the sentinel dispatching through it. And
`exploration_fleet.py` contained no `emit_heartbeat`, no `emit_decision`, no
`comm` reference at all -- so an operator watching `ov --sentinel` saw an
idle cockpit while eight agents worked.

The binding is structural rather than a call per state change, because the
sixth instance of this bug would otherwise be written the same week it was
fixed. A fleet declares which coroutines are agent runs; the mixin wraps
them at class creation.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.exploration_fleet import ExplorationFleet
from backend.core.ouroboros.governance.fleet_telemetry import (
    FleetTelemetryMixin,
    attach_transport,
    detach_transport,
    emit_fleet_frame,
    telemetry_enabled,
    transport_bound,
)


class _RecordingComm:
    def __init__(self):
        self.frames = []

    async def emit_heartbeat(self, *, op_id, phase, progress_pct, **extra):
        self.frames.append(
            {"op_id": op_id, "phase": phase, "pct": progress_pct, **extra}
        )


class _ExplodingComm:
    async def emit_heartbeat(self, **_kwargs):
        raise RuntimeError("transport down")


@pytest.fixture(autouse=True)
def _clean_transport():
    detach_transport()
    yield
    detach_transport()


class _Fleet(FleetTelemetryMixin):
    TELEMETRY_METHODS = ("_run_agent",)
    TELEMETRY_PHASE = "EXPLORE"

    def __init__(self):
        self.ran = []

    async def _run_agent(self, agent, goal):
        self.ran.append((agent, goal))
        return "done"


class _Agent:
    def __init__(self, agent_id="a1", repo="jarvis"):
        self.agent_id = agent_id
        self.repo = repo


# ---------------------------------------------------------------------------
# The defect, pinned
# ---------------------------------------------------------------------------


def test_the_real_fleet_inherits_telemetry():
    """The class that was invisible is the class that must be covered."""
    assert issubclass(ExplorationFleet, FleetTelemetryMixin)
    assert ExplorationFleet.TELEMETRY_METHODS == ("_run_agent",)


def test_the_real_fleets_agent_runner_is_wrapped():
    assert getattr(ExplorationFleet._run_agent, "__fleet_telemetry__", False)


@pytest.mark.asyncio
async def test_agent_run_emits_start_and_finish():
    comm = _RecordingComm()
    attach_transport(comm)
    fleet = _Fleet()

    assert await fleet._run_agent(_Agent(), "find the thing") == "done"

    states = [f["state"] for f in comm.frames]
    assert states == ["started", "finished"]
    assert comm.frames[0]["phase"] == "EXPLORE"
    assert comm.frames[-1]["pct"] == 100.0


@pytest.mark.asyncio
async def test_frames_identify_the_agent():
    """A cockpit row that cannot say WHICH agent is a progress bar, not
    telemetry."""
    comm = _RecordingComm()
    attach_transport(comm)
    await _Fleet()._run_agent(_Agent(agent_id="scout-3", repo="reactor"), "goal text")

    first = comm.frames[0]
    assert first["agent"] == "scout-3"
    assert first["repo"] == "reactor"
    assert first["fleet"] == "_Fleet"


@pytest.mark.asyncio
async def test_failure_is_reported_then_reraised():
    """A crashed agent is the most important thing to render, and swallowing
    the exception to emit it would be worse than silence."""
    comm = _RecordingComm()
    attach_transport(comm)

    class _Boom(FleetTelemetryMixin):
        TELEMETRY_METHODS = ("_run_agent",)

        async def _run_agent(self, agent, goal):
            raise ValueError("agent died")

    with pytest.raises(ValueError, match="agent died"):
        await _Boom()._run_agent(_Agent(), "g")

    assert [f["state"] for f in comm.frames] == ["started", "failed"]
    assert "ValueError" in comm.frames[-1]["error"]


@pytest.mark.asyncio
async def test_elapsed_is_reported():
    comm = _RecordingComm()
    attach_transport(comm)
    await _Fleet()._run_agent(_Agent(), "g")
    assert comm.frames[-1]["elapsed_s"] >= 0.0


# ---------------------------------------------------------------------------
# The cockpit must never break the fleet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exploding_transport_does_not_break_the_agent():
    attach_transport(_ExplodingComm())
    fleet = _Fleet()
    assert await fleet._run_agent(_Agent(), "g") == "done"
    assert fleet.ran, "the agent did not run"


@pytest.mark.asyncio
async def test_no_transport_still_runs_the_agent():
    fleet = _Fleet()
    assert await fleet._run_agent(_Agent(), "g") == "done"


@pytest.mark.asyncio
async def test_disabled_emits_nothing_but_still_runs(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_TELEMETRY_ENABLED", "false")
    comm = _RecordingComm()
    attach_transport(comm)
    assert await _Fleet()._run_agent(_Agent(), "g") == "done"
    assert comm.frames == []
    assert telemetry_enabled() is False


@pytest.mark.asyncio
async def test_concurrent_agents_all_emit():
    """The fleet runs agents under gather; frames must not be lost."""
    comm = _RecordingComm()
    attach_transport(comm)
    fleet = _Fleet()
    await asyncio.gather(*[
        fleet._run_agent(_Agent(agent_id=f"a{i}"), "g") for i in range(8)
    ])
    assert len([f for f in comm.frames if f["state"] == "started"]) == 8
    assert len([f for f in comm.frames if f["state"] == "finished"]) == 8


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def test_transport_must_be_able_to_emit():
    """A binding that accepted anything would report success and drop every
    frame -- the failure this module exists to end."""
    assert attach_transport(None) is False
    assert attach_transport(object()) is False
    assert transport_bound() is False


def test_attach_reports_success():
    assert attach_transport(_RecordingComm()) is True
    assert transport_bound() is True


@pytest.mark.asyncio
async def test_emit_reports_whether_it_reached_a_transport():
    """"Emitted" and "silently dropped" must be distinguishable."""
    assert await emit_fleet_frame(op_id="o", phase="P", progress_pct=1.0) is False
    attach_transport(_RecordingComm())
    assert await emit_fleet_frame(op_id="o", phase="P", progress_pct=1.0) is True


@pytest.mark.asyncio
async def test_op_id_can_be_bound_for_attribution():
    comm = _RecordingComm()
    attach_transport(comm)
    fleet = _Fleet()
    fleet.bind_telemetry_op("op-1234")
    await fleet._run_agent(_Agent(), "g")
    assert comm.frames[0]["op_id"] == "op-1234"


def test_missing_declared_method_warns_not_crashes(caplog):
    """A wiring mistake must be loud at import, not quiet at runtime."""
    class _Bad(FleetTelemetryMixin):
        TELEMETRY_METHODS = ("does_not_exist",)

    assert any("does not exist" in r.message for r in caplog.records)


def test_sync_declared_method_is_not_wrapped(caplog):
    class _Sync(FleetTelemetryMixin):
        TELEMETRY_METHODS = ("run",)

        def run(self):
            return 1

    assert _Sync().run() == 1
    assert any("not a coroutine" in r.message for r in caplog.records)


def test_double_wrapping_is_prevented():
    class _Once(FleetTelemetryMixin):
        TELEMETRY_METHODS = ("_run_agent",)

        async def _run_agent(self, agent, goal):
            return 1

    class _Twice(_Once):
        TELEMETRY_METHODS = ("_run_agent",)

    assert getattr(_Twice._run_agent, "__fleet_telemetry__", False)
    assert _Twice._run_agent is _Once._run_agent
