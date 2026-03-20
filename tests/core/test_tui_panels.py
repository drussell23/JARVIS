"""Tests for TUI dashboard panel data layers."""
import time
import pytest
from backend.core.telemetry_contract import TelemetryEnvelope
from backend.core.tui.pipeline_panel import PipelineData, CommandTrace
from backend.core.tui.agents_panel import AgentsData, AgentEntry
from backend.core.tui.system_panel import SystemData
from backend.core.tui.faults_panel import FaultsData, FaultEntry


def _make_envelope(schema, payload, trace_id="t1", source="test"):
    return TelemetryEnvelope.create(
        event_schema=schema, source=source, trace_id=trace_id,
        span_id="s1", partition_key=schema.split(".")[0], payload=payload,
    )


class TestPipelineData:
    def test_new_command_creates_trace(self):
        data = PipelineData()
        env = _make_envelope("reasoning.decision@1.0.0", {
            "command": "start my day", "is_proactive": True, "confidence": 0.92,
            "signals": ["workflow_trigger"], "phase": "full_enable",
            "expanded_intents": ["check email", "check calendar"],
            "mind_requests": 2, "delegations": 2, "total_ms": 2300.0, "success_rate": 1.0,
        }, trace_id="abc-123")
        data.update(env)
        assert len(data.commands) == 1
        assert data.commands[0].trace_id == "abc-123"
        assert data.commands[0].command == "start my day"
        assert data.commands[0].is_proactive is True

    def test_bounded_at_50(self):
        data = PipelineData(max_commands=50)
        for i in range(60):
            data.update(_make_envelope("reasoning.decision@1.0.0", {
                "command": f"cmd {i}", "is_proactive": False, "confidence": 0.1,
                "signals": [], "phase": "shadow", "expanded_intents": [],
                "mind_requests": 0, "delegations": 0, "total_ms": 100, "success_rate": 0,
            }, trace_id=f"t-{i}"))
        assert len(data.commands) == 50

    def test_passthrough_command(self):
        data = PipelineData()
        data.update(_make_envelope("reasoning.decision@1.0.0", {
            "command": "what time", "is_proactive": False, "confidence": 0.1,
            "signals": [], "phase": "full_enable", "expanded_intents": [],
            "mind_requests": 0, "delegations": 0, "total_ms": 50, "success_rate": 0,
        }))
        assert data.commands[0].is_proactive is False
        assert data.commands[0].expanded_intents == []

    def test_command_count(self):
        data = PipelineData()
        data.update(_make_envelope("reasoning.decision@1.0.0", {
            "command": "a", "is_proactive": False, "confidence": 0,
            "signals": [], "phase": "", "expanded_intents": [],
            "mind_requests": 0, "delegations": 0, "total_ms": 0, "success_rate": 0,
        }))
        assert data.total_commands == 1

    def test_ignores_non_decision_events(self):
        data = PipelineData()
        data.update(_make_envelope("lifecycle.transition@1.0.0", {"to_state": "READY"}))
        assert len(data.commands) == 0


class TestAgentsData:
    def test_graph_state_populates(self):
        data = AgentsData()
        data.update(_make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 3, "initialized": 3, "failed": 0,
            "agent_names": ["coordinator_agent", "predictive_planner", "memory_agent"],
        }))
        assert len(data.agents) == 3
        assert "coordinator_agent" in data.agents

    def test_unit_state_updates(self):
        data = AgentsData()
        data.update(_make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 1, "initialized": 1, "failed": 0,
            "agent_names": ["coordinator_agent"],
        }))
        data.update(_make_envelope("scheduler.unit_state@1.0.0", {
            "agent_name": "coordinator_agent", "state": "busy", "tasks_completed": 5,
        }))
        assert data.agents["coordinator_agent"].state == "busy"

    def test_counts(self):
        data = AgentsData()
        data.update(_make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 15, "initialized": 13, "failed": 2,
            "agent_names": ["a"] * 13,
        }))
        assert data.total_agents == 15
        assert data.initialized == 13

    def test_ignores_non_scheduler_events(self):
        data = AgentsData()
        data.update(_make_envelope("reasoning.decision@1.0.0", {"command": "test"}))
        assert len(data.agents) == 0


class TestSystemData:
    def test_lifecycle_transition(self):
        data = SystemData()
        data.update(_make_envelope("lifecycle.transition@1.0.0", {
            "from_state": "PROBING", "to_state": "READY", "trigger": "health",
            "reason_code": "ready", "attempt": 0, "restarts_in_window": 0,
            "elapsed_in_prev_state_ms": 5000,
        }, source="jprime_lifecycle_controller"))
        assert data.lifecycle_state == "READY"

    def test_gate_activation(self):
        data = SystemData()
        data.update(_make_envelope("reasoning.activation@1.0.0", {
            "from_state": "READY", "to_state": "ACTIVE", "trigger": "dwell",
            "cause_code": "ARMED", "critical_deps": {"jprime": "HEALTHY"},
            "gate_sequence": 3, "dwell_ms": 5000, "in_flight_preempted": 0,
            "degraded_overrides": {},
        }))
        assert data.gate_state == "ACTIVE"

    def test_transitions_bounded(self):
        data = SystemData()
        for i in range(25):
            data.update(_make_envelope("lifecycle.transition@1.0.0", {
                "from_state": "A", "to_state": "B", "trigger": "t",
                "reason_code": "r", "attempt": 0, "restarts_in_window": 0,
                "elapsed_in_prev_state_ms": 0,
            }))
        assert len(data.recent_transitions) <= 20


class TestFaultsData:
    def test_fault_raised(self):
        data = FaultsData()
        data.update(_make_envelope("fault.raised@1.0.0", {
            "fault_class": "connection_refused", "component": "jprime",
            "message": "unreachable", "recovery_policy": "auto_restart", "terminal": False,
        }))
        assert len(data.active_faults) == 1
        assert data.active_faults[0].fault_class == "connection_refused"

    def test_fault_resolved(self):
        data = FaultsData()
        raise_env = _make_envelope("fault.raised@1.0.0", {
            "fault_class": "timeout", "component": "agent",
            "message": "timeout", "recovery_policy": "retry", "terminal": False,
        })
        data.update(raise_env)
        assert len(data.active_faults) == 1
        data.update(_make_envelope("fault.resolved@1.0.0", {
            "fault_id": raise_env.event_id, "resolution": "recovered", "duration_ms": 12000,
        }))
        assert len(data.active_faults) == 0
        assert len(data.resolved_faults) == 1

    def test_resolved_bounded(self):
        data = FaultsData()
        for i in range(25):
            env = _make_envelope("fault.raised@1.0.0", {
                "fault_class": "t", "component": "t", "message": "t",
                "recovery_policy": "none", "terminal": False,
            }, trace_id=f"f-{i}")
            data.update(env)
            data.update(_make_envelope("fault.resolved@1.0.0", {
                "fault_id": env.event_id, "resolution": "ok", "duration_ms": 100,
            }, trace_id=f"f-{i}"))
        assert len(data.resolved_faults) <= 20


# ---------------------------------------------------------------------------
# BusConsumer + StatusBarData tests
# ---------------------------------------------------------------------------
from backend.core.tui.bus_consumer import TelemetryBusConsumer, StatusBarData


class TestBusConsumer:
    def test_routes_reasoning_to_pipeline(self):
        pipeline, agents, system, faults, status = PipelineData(), AgentsData(), SystemData(), FaultsData(), StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)
        consumer.handle_sync(_make_envelope("reasoning.decision@1.0.0", {
            "command": "test", "is_proactive": False, "confidence": 0.1,
            "signals": [], "phase": "shadow", "expanded_intents": [],
            "mind_requests": 0, "delegations": 0, "total_ms": 50, "success_rate": 0,
        }))
        assert len(pipeline.commands) == 1

    def test_routes_lifecycle_to_system(self):
        pipeline, agents, system, faults, status = PipelineData(), AgentsData(), SystemData(), FaultsData(), StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)
        consumer.handle_sync(_make_envelope("lifecycle.transition@1.0.0", {
            "from_state": "UNKNOWN", "to_state": "READY", "trigger": "t",
            "reason_code": "r", "attempt": 0, "restarts_in_window": 0,
            "elapsed_in_prev_state_ms": 0,
        }))
        assert system.lifecycle_state == "READY"

    def test_routes_scheduler_to_agents(self):
        pipeline, agents, system, faults, status = PipelineData(), AgentsData(), SystemData(), FaultsData(), StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)
        consumer.handle_sync(_make_envelope("scheduler.graph_state@1.0.0", {
            "total_agents": 5, "initialized": 5, "failed": 0, "agent_names": ["a","b","c","d","e"],
        }))
        assert agents.total_agents == 5

    def test_routes_fault_to_faults(self):
        pipeline, agents, system, faults, status = PipelineData(), AgentsData(), SystemData(), FaultsData(), StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)
        consumer.handle_sync(_make_envelope("fault.raised@1.0.0", {
            "fault_class": "test", "component": "test", "message": "test",
            "recovery_policy": "none", "terminal": False,
        }))
        assert len(faults.active_faults) == 1

    def test_gate_activation_routed_to_system(self):
        pipeline, agents, system, faults, status = PipelineData(), AgentsData(), SystemData(), FaultsData(), StatusBarData()
        consumer = TelemetryBusConsumer(pipeline, agents, system, faults, status)
        consumer.handle_sync(_make_envelope("reasoning.activation@1.0.0", {
            "from_state": "READY", "to_state": "ACTIVE", "trigger": "dwell",
            "cause_code": "ARMED", "critical_deps": {}, "gate_sequence": 1,
            "dwell_ms": 5000, "in_flight_preempted": 0, "degraded_overrides": {},
        }))
        assert system.gate_state == "ACTIVE"


class TestStatusBarData:
    def test_lifecycle_updates(self):
        s = StatusBarData()
        s.update(_make_envelope("lifecycle.transition@1.0.0", {"to_state": "READY"}))
        assert s.lifecycle_state == "READY"

    def test_gate_updates(self):
        s = StatusBarData()
        s.update(_make_envelope("reasoning.activation@1.0.0", {"to_state": "ACTIVE"}))
        assert s.gate_state == "ACTIVE"

    def test_command_count(self):
        s = StatusBarData()
        s.update(_make_envelope("reasoning.decision@1.0.0", {"command": "test"}))
        assert s.command_count == 1

    def test_fault_count(self):
        s = StatusBarData()
        s.update(_make_envelope("fault.raised@1.0.0", {}))
        assert s.fault_count == 1
        s.update(_make_envelope("fault.resolved@1.0.0", {}))
        assert s.fault_count == 0

    def test_to_string(self):
        s = StatusBarData()
        text = s.to_string()
        assert "J-Prime:" in text
        assert "Gate:" in text
        assert "Agents:" in text

    def test_bus_emitted_counter(self):
        s = StatusBarData()
        s.update(_make_envelope("lifecycle.transition@1.0.0", {}))
        s.update(_make_envelope("reasoning.decision@1.0.0", {}))
        assert s.bus_emitted == 2


# ---------------------------------------------------------------------------
# JarvisDashboard app tests
# ---------------------------------------------------------------------------


class TestDashboardImport:
    def test_app_importable(self):
        from backend.core.tui.app import JarvisDashboard, start_dashboard
        assert JarvisDashboard is not None
        assert callable(start_dashboard)

    def test_app_creates(self):
        from backend.core.tui.app import JarvisDashboard
        app = JarvisDashboard()
        assert app is not None
        assert app.pipeline_data is not None
        assert app.agents_data is not None
        assert app.system_data is not None
        assert app.faults_data is not None

    def test_start_dashboard_no_tty(self):
        from backend.core.tui.app import start_dashboard
        from unittest.mock import patch
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            result = start_dashboard()
        assert result is None


class TestStartDashboard:
    def test_no_tty_returns_none(self):
        from backend.core.tui.app import start_dashboard
        from unittest.mock import patch
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            result = start_dashboard()
        assert result is None
