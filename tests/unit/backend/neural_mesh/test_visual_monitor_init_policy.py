import sys
import types

import pytest

from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent


def _make_agent_stub() -> VisualMonitorAgent:
    # Policy helpers do not require full constructor dependencies.
    return VisualMonitorAgent.__new__(VisualMonitorAgent)


def test_resolve_parallel_init_policy_applies_startup_floor(monkeypatch):
    agent = _make_agent_stub()

    monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "false")
    monkeypatch.setenv("JARVIS_VISUAL_HEAVY_INIT_STARTUP_FLOOR_SECONDS", "25.0")
    monkeypatch.setenv("JARVIS_VISUAL_INIT_STARTUP_TIMEOUT_MULTIPLIER", "1.75")
    monkeypatch.delenv("JARVIS_VISUAL_HEAVY_INIT_PARALLELISM", raising=False)

    base = {
        "ferrari_engine": 15.0,
        "watcher_manager": 5.0,
        "detector": 15.0,
        "computer_use": 5.0,
        "agentic_runner": 15.0,
        "spatial_agent": 15.0,
    }
    policy = agent._resolve_parallel_init_policy(base)

    assert policy["in_startup"] is True
    assert policy["heavy_parallelism"] == 1
    assert policy["timeouts"]["ferrari_engine"] == pytest.approx(26.25, rel=0.001)
    assert policy["timeouts"]["detector"] == pytest.approx(26.25, rel=0.001)
    assert policy["timeouts"]["watcher_manager"] == 5.0


def test_resolve_parallel_init_policy_respects_runtime_parallelism_override(monkeypatch):
    agent = _make_agent_stub()

    monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "true")
    monkeypatch.delenv("JARVIS_STARTUP_TIMESTAMP", raising=False)
    monkeypatch.setenv("JARVIS_GLOBAL_STARTUP_DURATION", "180.0")
    monkeypatch.setattr("backend.neural_mesh.agents.visual_monitor_agent.time.time", lambda: 1000.0)
    fake_psutil = types.SimpleNamespace(
        Process=lambda: types.SimpleNamespace(create_time=lambda: 0.0)
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setenv("JARVIS_VISUAL_HEAVY_INIT_PARALLELISM", "3")
    monkeypatch.setenv("JARVIS_VISUAL_HEAVY_INIT_RUNTIME_FLOOR_SECONDS", "12.0")
    monkeypatch.setenv("JARVIS_VISUAL_INIT_EMERGENCY_TIMEOUT_MULTIPLIER", "2.0")
    monkeypatch.setattr(agent, "_get_memory_thrash_state", lambda: "emergency")

    base = {
        "ferrari_engine": 15.0,
        "watcher_manager": 5.0,
        "detector": 15.0,
        "computer_use": 5.0,
        "agentic_runner": 15.0,
        "spatial_agent": 15.0,
    }
    policy = agent._resolve_parallel_init_policy(base)

    assert policy["in_startup"] is False
    assert policy["thrash_state"] == "emergency"
    assert policy["heavy_parallelism"] == 3
    assert policy["timeouts"]["ferrari_engine"] == pytest.approx(30.0, rel=0.001)
    assert policy["timeouts"]["detector"] == pytest.approx(30.0, rel=0.001)


def test_startup_timestamp_overrides_premature_startup_complete(monkeypatch):
    agent = _make_agent_stub()

    monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "true")
    monkeypatch.setenv("JARVIS_STARTUP_TIMESTAMP", "100.0")
    monkeypatch.setenv("JARVIS_GLOBAL_STARTUP_DURATION", "180.0")
    monkeypatch.setattr("backend.neural_mesh.agents.visual_monitor_agent.time.time", lambda: 200.0)

    assert agent._is_global_startup_phase() is True


def test_recent_process_uptime_keeps_startup_phase_without_timestamp(monkeypatch):
    agent = _make_agent_stub()

    monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "true")
    monkeypatch.delenv("JARVIS_STARTUP_TIMESTAMP", raising=False)
    monkeypatch.setenv("JARVIS_GLOBAL_STARTUP_DURATION", "180.0")
    monkeypatch.setattr("backend.neural_mesh.agents.visual_monitor_agent.time.time", lambda: 150.0)

    fake_psutil = types.SimpleNamespace(
        Process=lambda: types.SimpleNamespace(create_time=lambda: 100.0)
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert agent._is_global_startup_phase() is True
