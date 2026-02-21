from __future__ import annotations


async def test_redundant_inference_path_keeps_system_healthy():
    from backend.core.vbi_health_monitor import (
        ComponentType,
        HealthLevel,
        VBIHealthMonitor,
    )

    monitor = VBIHealthMonitor()

    cloud_run = monitor._component_health[ComponentType.CLOUD_RUN]
    cloud_run.total_operations = 1
    cloud_run.health_level = HealthLevel.DEGRADED

    vbi_engine = monitor._component_health[ComponentType.VBI_ENGINE]
    vbi_engine.total_operations = 1
    vbi_engine.health_level = HealthLevel.HEALTHY

    health = await monitor.get_system_health()

    assert health["capability_health"]["inference_path"] == "healthy"
    assert health["overall_health"] == "healthy"


async def test_redundant_persistence_path_keeps_system_healthy():
    from backend.core.vbi_health_monitor import (
        ComponentType,
        HealthLevel,
        VBIHealthMonitor,
    )

    monitor = VBIHealthMonitor()

    cloudsql = monitor._component_health[ComponentType.CLOUDSQL]
    cloudsql.total_operations = 1
    cloudsql.health_level = HealthLevel.DEGRADED

    sqlite = monitor._component_health[ComponentType.SQLITE]
    sqlite.total_operations = 1
    sqlite.health_level = HealthLevel.HEALTHY

    health = await monitor.get_system_health()

    assert health["capability_health"]["persistence_path"] == "healthy"
    assert health["overall_health"] == "healthy"


async def test_overall_health_hysteresis_requires_sustained_state_change():
    from backend.core.vbi_health_monitor import (
        ComponentType,
        HealthLevel,
        VBIHealthMonitor,
    )

    monitor = VBIHealthMonitor()
    monitor._overall_degrade_streak_required = 2
    monitor._overall_recovery_streak_required = 2
    monitor._overall_sticky_seconds = 0.0

    engine = monitor._component_health[ComponentType.VBI_ENGINE]
    engine.total_operations = 1
    engine.health_level = HealthLevel.HEALTHY
    baseline = await monitor.get_system_health()
    assert baseline["overall_health"] == "healthy"

    engine.health_level = HealthLevel.DEGRADED
    first_degrade = await monitor.get_system_health()
    second_degrade = await monitor.get_system_health()
    assert first_degrade["overall_health"] == "healthy"
    assert second_degrade["overall_health"] == "degraded"

    engine.health_level = HealthLevel.HEALTHY
    first_recovery = await monitor.get_system_health()
    second_recovery = await monitor.get_system_health()
    assert first_recovery["overall_health"] == "degraded"
    assert second_recovery["overall_health"] == "healthy"
