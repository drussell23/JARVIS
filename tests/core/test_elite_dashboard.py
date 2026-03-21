"""Tests for EliteDashboard."""
import time
import pytest

from backend.core.supervisor.elite_dashboard import (
    EliteDashboard,
    DashboardState,
    BootPhase,
    BootTracker,
    RepoHealth,
    TickerEvent,
    _render_health_matrix,
    _render_event_ticker,
    _render_boot_tree,
    _render_stats_bar,
    _build_layout,
    _process_envelope,
)


class TestDashboardState:
    def test_default_repos(self):
        state = DashboardState()
        assert "jarvis" in state.repos
        assert "prime" in state.repos
        assert "reactor" in state.repos

    def test_default_counters(self):
        state = DashboardState()
        assert state.total_envelopes == 0
        assert state.total_faults == 0
        assert state.boot_complete is False


class TestBootPhase:
    def test_default_pending(self):
        p = BootPhase(name="test")
        assert p.status == "pending"
        assert p.started_at == 0.0


class TestBootTracker:
    def test_begin_end_phase(self):
        state = DashboardState()
        bt = BootTracker(state, None)
        bt.begin_phase("Preflight")
        assert len(state.boot_phases) == 1
        assert state.boot_phases[0].status == "running"
        bt.end_phase("Preflight", success=True)
        assert state.boot_phases[0].status == "done"

    def test_failed_phase(self):
        state = DashboardState()
        bt = BootTracker(state, None)
        bt.begin_phase("Backend")
        bt.end_phase("Backend", success=False, detail="timeout")
        assert state.boot_phases[0].status == "failed"
        assert state.boot_phases[0].detail == "timeout"

    def test_skip_phase(self):
        state = DashboardState()
        bt = BootTracker(state, None)
        bt.skip_phase("Reactor", reason="not found")
        assert state.boot_phases[0].status == "skipped"
        assert state.boot_phases[0].detail == "not found"

    def test_mark_boot_complete(self):
        state = DashboardState()
        bt = BootTracker(state, None)
        bt.mark_boot_complete()
        assert state.boot_complete is True
        assert state.boot_elapsed_s > 0


class TestRenderFunctions:
    def _make_state(self):
        state = DashboardState()
        state.boot_phases.append(BootPhase(name="Preflight", status="done", started_at=0, finished_at=1.2))
        state.boot_phases.append(BootPhase(name="Backend", status="running", started_at=1.2))
        state.boot_phases.append(BootPhase(name="Trinity", status="pending"))
        state.events.append(TickerEvent(
            timestamp=time.time(), category="lifecycle",
            message="Test event", severity="info",
        ))
        state.repos["prime"].status = "READY"
        state.repos["prime"].last_heartbeat = time.monotonic()
        state.repos["prime"].latency_ms = 12.5
        return state

    def test_render_health_matrix(self):
        state = self._make_state()
        result = _render_health_matrix(state)
        assert result is not None

    def test_render_event_ticker(self):
        state = self._make_state()
        result = _render_event_ticker(state)
        assert result is not None

    def test_render_boot_tree(self):
        state = self._make_state()
        result = _render_boot_tree(state)
        assert result is not None

    def test_render_boot_tree_complete(self):
        state = self._make_state()
        state.boot_complete = True
        state.boot_elapsed_s = 22.5
        result = _render_boot_tree(state)
        assert result is not None

    def test_render_stats_bar(self):
        state = self._make_state()
        result = _render_stats_bar(state)
        assert result is not None

    def test_build_layout(self):
        state = self._make_state()
        result = _build_layout(state)
        assert result is not None

    def test_render_empty_state(self):
        state = DashboardState()
        result = _build_layout(state)
        assert result is not None


class TestProcessEnvelope:
    def _make_envelope(self, schema, payload, source="test"):
        class E:
            pass
        e = E()
        e.event_schema = schema
        e.payload = payload
        e.source = source
        return e

    def test_lifecycle_transition(self):
        state = DashboardState()
        env = self._make_envelope(
            "lifecycle.transition@1.0.0",
            {"from_state": "PROBING", "to_state": "READY"},
            source="jprime_lifecycle_controller",
        )
        _process_envelope(state, env)
        assert state.total_envelopes == 1
        assert state.repos["prime"].status == "READY"
        assert len(state.events) == 1
        assert state.events[0].category == "lifecycle"

    def test_fault_raised(self):
        state = DashboardState()
        env = self._make_envelope(
            "fault.raised@1.0.0",
            {"fault_class": "connection_refused"},
        )
        _process_envelope(state, env)
        assert state.total_faults == 1
        assert len(state.events) == 1
        assert state.events[0].severity == "error"

    def test_fault_resolved(self):
        state = DashboardState()
        env = self._make_envelope(
            "fault.resolved@1.0.0",
            {"fault_class": "connection_refused"},
        )
        _process_envelope(state, env)
        assert state.total_recoveries == 1

    def test_scheduler_graph_state(self):
        state = DashboardState()
        env = self._make_envelope(
            "scheduler.graph_state@1.0.0",
            {"total_agents": 15, "initialized": 15},
        )
        _process_envelope(state, env)
        assert state.total_agents == 15
        assert state.initialized_agents == 15
        assert state.repos["jarvis"].status == "ONLINE"

    def test_reasoning_decision(self):
        state = DashboardState()
        env = self._make_envelope(
            "reasoning.decision@1.0.0",
            {"command": "search YouTube for NBA"},
        )
        _process_envelope(state, env)
        assert len(state.events) == 1
        assert state.events[0].category == "reasoning"

    def test_proactive_drive_eligible(self):
        state = DashboardState()
        env = self._make_envelope(
            "reasoning.proactive_drive@1.0.0",
            {"state": "ELIGIBLE"},
        )
        _process_envelope(state, env)
        assert state.proactive_explorations == 1

    def test_unknown_schema_increments_count_only(self):
        state = DashboardState()
        env = self._make_envelope("unknown.schema@1.0.0", {})
        _process_envelope(state, env)
        assert state.total_envelopes == 1
        assert len(state.events) == 0


class TestEliteDashboard:
    def test_disabled_no_thread(self):
        d = EliteDashboard(enabled=False)
        assert d._thread is None
        assert d.health()["enabled"] is False

    def test_update_repo_status(self):
        d = EliteDashboard(enabled=False)
        d.update_repo_status("prime", "READY", latency_ms=10.0)
        assert d._state.repos["prime"].status == "READY"
        assert d._state.repos["prime"].latency_ms == 10.0

    def test_add_event(self):
        d = EliteDashboard(enabled=False)
        d.add_event("test", "hello world", severity="warn")
        assert len(d._state.events) == 1
        assert d._state.events[0].message == "hello world"

    def test_health_snapshot(self):
        d = EliteDashboard(enabled=False)
        h = d.health()
        assert "enabled" in h
        assert "running" in h
        assert "total_envelopes" in h
        assert "boot_complete" in h
