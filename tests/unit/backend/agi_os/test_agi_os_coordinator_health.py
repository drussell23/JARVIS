from __future__ import annotations

from types import SimpleNamespace

from backend.agi_os.agi_os_coordinator import AGIOSCoordinator, AGIOSState, ComponentStatus


def test_determine_health_state_ignores_legacy_phase_component_entries():
    coordinator = AGIOSCoordinator()

    coordinator._component_status = {
        "voice": ComponentStatus(name="voice", available=True),
        "approval": ComponentStatus(name="approval", available=True),
        "events": ComponentStatus(name="events", available=True),
        "orchestrator": ComponentStatus(name="orchestrator", available=True),
        # Legacy/stale startup phase marker should not impact runtime health.
        "phase_components_connected": ComponentStatus(
            name="phase_components_connected",
            available=False,
            healthy=False,
            error="timeout (10.0s)",
        ),
    }

    assert coordinator._determine_health_state() == AGIOSState.ONLINE


async def test_start_resets_status_maps_and_tracks_phase_results(monkeypatch):
    coordinator = AGIOSCoordinator()
    coordinator._component_status["stale_component"] = ComponentStatus(
        name="stale_component",
        available=False,
        healthy=False,
        error="stale",
    )
    coordinator._phase_status["phase_old"] = ComponentStatus(
        name="phase_old",
        available=False,
        healthy=False,
        error="stale",
    )

    async def _init_components() -> None:
        coordinator._component_status["voice"] = ComponentStatus(name="voice", available=True)
        coordinator._component_status["approval"] = ComponentStatus(name="approval", available=True)
        coordinator._component_status["events"] = ComponentStatus(name="events", available=True)
        coordinator._component_status["orchestrator"] = ComponentStatus(
            name="orchestrator", available=True
        )

    async def _noop() -> None:
        return None

    monkeypatch.setattr(coordinator, "_init_agi_os_components", _init_components)
    monkeypatch.setattr(coordinator, "_init_intelligence_systems", _noop)
    monkeypatch.setattr(coordinator, "_init_neural_mesh", _noop)
    monkeypatch.setattr(coordinator, "_init_hybrid_orchestrator", _noop)
    monkeypatch.setattr(coordinator, "_init_screen_analyzer", _noop)
    monkeypatch.setattr(coordinator, "_connect_components", _noop)

    await coordinator.start()

    assert "stale_component" not in coordinator._component_status
    assert "phase_old" not in coordinator._phase_status
    assert "phase_components_connected" in coordinator._phase_status
    assert coordinator._phase_status["phase_components_connected"].available is True
    assert coordinator.state == AGIOSState.ONLINE


def test_get_status_includes_phases_separately():
    coordinator = AGIOSCoordinator()
    coordinator._component_status["voice"] = ComponentStatus(name="voice", available=True)
    coordinator._phase_status["phase_components_connected"] = ComponentStatus(
        name="phase_components_connected",
        available=False,
        healthy=False,
        error="timeout",
    )

    status = coordinator.get_status()
    assert "phases" in status
    assert "phase_components_connected" in status["phases"]
    assert "phase_components_connected" not in status["components"]


async def test_start_adjusts_components_phase_timeout_for_sequential_mode(monkeypatch):
    coordinator = AGIOSCoordinator()

    async def _noop() -> None:
        return None

    monkeypatch.setenv("JARVIS_AGI_OS_SEQUENTIAL_COMPONENTS_TIMEOUT_MULTIPLIER", "2.0")
    monkeypatch.setattr(coordinator, "_init_agi_os_components", _noop)
    monkeypatch.setattr(coordinator, "_init_intelligence_systems", _noop)
    monkeypatch.setattr(coordinator, "_init_neural_mesh", _noop)
    monkeypatch.setattr(coordinator, "_init_hybrid_orchestrator", _noop)
    monkeypatch.setattr(coordinator, "_init_screen_analyzer", _noop)
    monkeypatch.setattr(coordinator, "_connect_components", _noop)

    await coordinator.start(memory_mode="sequential")

    assert coordinator._phase_budgets["agi_os_components"] >= 70.0

    await coordinator.stop()


async def test_components_phase_defers_optional_when_sequential_budget_tight(monkeypatch):
    coordinator = AGIOSCoordinator()
    coordinator._memory_mode = "sequential"
    coordinator._phase_budgets = {"agi_os_components": 20.0}
    coordinator._config["enable_voice"] = False
    coordinator._config["enable_autonomous_actions"] = False

    async def _ok_async(*args, **kwargs):
        return object()

    monkeypatch.setattr(
        "backend.agi_os.agi_os_coordinator.get_approval_manager",
        _ok_async,
    )
    monkeypatch.setattr(
        "backend.agi_os.agi_os_coordinator.get_event_stream",
        _ok_async,
    )

    import psutil

    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=8 * 1024 * 1024 * 1024),
    )

    await coordinator._init_agi_os_components()

    for name in (
        "notification_monitor",
        "system_event_monitor",
        "ghost_hands",
        "ghost_display",
    ):
        status = coordinator._component_status[name]
        assert status.available is False
        assert status.error == "Deferred: startup sequential budget"


def test_determine_health_state_treats_deferred_components_as_non_blocking():
    coordinator = AGIOSCoordinator()
    coordinator._component_status = {
        "voice": ComponentStatus(name="voice", available=True),
        "approval": ComponentStatus(name="approval", available=True),
        "events": ComponentStatus(name="events", available=True),
        "orchestrator": ComponentStatus(name="orchestrator", available=True),
        "neural_mesh": ComponentStatus(
            name="neural_mesh",
            available=False,
            healthy=False,
            error="Deferred: low memory",
        ),
    }

    assert coordinator._determine_health_state() == AGIOSState.ONLINE


async def test_recover_single_component_reinitializes_neural_mesh(monkeypatch):
    coordinator = AGIOSCoordinator()
    coordinator._component_status["neural_mesh"] = ComponentStatus(
        name="neural_mesh",
        available=False,
        error="Deferred: low memory",
    )

    class _UnhealthyMesh:
        async def health_check(self):
            return {"status": "unhealthy"}

    coordinator._neural_mesh = _UnhealthyMesh()

    async def _fake_init_neural_mesh() -> None:
        coordinator._neural_mesh = object()
        coordinator._component_status["neural_mesh"] = ComponentStatus(
            name="neural_mesh",
            available=True,
            healthy=True,
        )

    monkeypatch.setattr(coordinator, "_init_neural_mesh", _fake_init_neural_mesh)

    recovered = await coordinator._recover_single_component("neural_mesh")
    assert recovered is coordinator._neural_mesh
    assert coordinator._component_status["neural_mesh"].available is True
