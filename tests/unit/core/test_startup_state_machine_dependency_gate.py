"""Tests for deterministic dependency gating in StartupStateMachine."""

import pytest


@pytest.mark.asyncio
async def test_start_component_enforce_dependencies_blocks_when_not_ready():
    from backend.core.startup_state_machine import (
        DependencyNotReadyError,
        StartupStateMachine,
    )

    sm = StartupStateMachine()

    # preflight depends on loading_experience, which starts as PENDING
    with pytest.raises(DependencyNotReadyError) as exc_info:
        await sm.start_component("preflight", enforce_dependencies=True)

    assert "loading_experience:pending" in str(exc_info.value)


@pytest.mark.asyncio
async def test_start_component_enforce_dependencies_allows_when_ready():
    from backend.core.startup_state_machine import ComponentStatus, StartupStateMachine

    sm = StartupStateMachine()
    sm.update_component_sync("loading_experience", "ready")

    await sm.start_component("preflight", enforce_dependencies=True)

    assert sm.components["preflight"].status == ComponentStatus.LOADING


@pytest.mark.asyncio
async def test_start_component_allow_skipped_dependency_override():
    from backend.core.startup_state_machine import (
        ComponentStatus,
        DependencyNotReadyError,
        StartupStateMachine,
    )

    sm = StartupStateMachine()
    sm.update_component_sync("loading_experience", "skipped")

    with pytest.raises(DependencyNotReadyError):
        await sm.start_component("preflight", enforce_dependencies=True)

    await sm.start_component(
        "preflight",
        enforce_dependencies=True,
        allow_skipped_dependencies=True,
    )
    assert sm.components["preflight"].status == ComponentStatus.LOADING


@pytest.mark.asyncio
async def test_start_component_default_mode_preserves_legacy_behavior():
    from backend.core.startup_state_machine import ComponentStatus, StartupStateMachine

    sm = StartupStateMachine()

    # Legacy mode (enforce_dependencies=False) should keep old permissive behavior.
    await sm.start_component("preflight")

    assert sm.components["preflight"].status == ComponentStatus.LOADING


def test_get_blocking_dependencies_reports_current_state():
    from backend.core.startup_state_machine import StartupStateMachine

    sm = StartupStateMachine()

    blockers = sm.get_blocking_dependencies("preflight")
    assert blockers == ["loading_experience:pending"]

    sm.update_component_sync("loading_experience", "ready")
    assert sm.get_blocking_dependencies("preflight") == []
