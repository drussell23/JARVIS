"""Acceptance tests for GovernedLoopService startup behavior.

AC1: PrimeProvider retained on failed startup health probe (not dropped)
AC2: FALLBACK_ACTIVE FSM state maps to ServiceState.ACTIVE (not DEGRADED)
AC3: QUEUE_ONLY FSM state still maps to ServiceState.DEGRADED (intentional)
"""
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    ServiceState,
)
from backend.core.ouroboros.governance.candidate_generator import FailbackState


def _make_mock_stack() -> MagicMock:
    """Build a minimal mock GovernanceStack sufficient for start()."""
    stack = MagicMock()
    stack.can_write.return_value = (True, "ok")
    stack._started = True
    stack.canary = MagicMock()
    stack.canary.register_slice = MagicMock()
    stack.ledger = MagicMock()
    stack.ledger.append = AsyncMock(return_value=True)
    return stack


async def test_ac1_prime_provider_retained_on_startup_probe_failure(tmp_path):
    """PrimeProvider is kept even when health_probe() returns False at startup."""
    config = GovernedLoopConfig(project_root=tmp_path)
    mock_prime_client = MagicMock()
    stack = _make_mock_stack()
    svc = GovernedLoopService(stack=stack, prime_client=mock_prime_client, config=config)

    with patch(
        "backend.core.ouroboros.governance.providers.PrimeProvider"
    ) as MockProvider, \
         patch.object(svc, "_reconcile_on_boot", new=AsyncMock()), \
         patch.object(svc, "_register_canary_slices"), \
         patch.object(svc, "_attach_to_stack"):
        mock_provider_instance = MagicMock()
        mock_provider_instance.health_probe = AsyncMock(return_value=False)
        MockProvider.return_value = mock_provider_instance

        await svc.start()

    # Generator must exist — primary provider was retained, not dropped
    assert svc._generator is not None
    await svc.stop()


async def test_ac2_fallback_active_maps_to_active_state(tmp_path):
    """FALLBACK_ACTIVE FSM state → ServiceState.ACTIVE (GCP-first intentional fallback)."""
    config = GovernedLoopConfig(project_root=tmp_path)
    stack = _make_mock_stack()
    svc = GovernedLoopService(stack=stack, prime_client=None, config=config)

    mock_generator = MagicMock()
    mock_fsm = MagicMock()
    mock_fsm.state = FailbackState.FALLBACK_ACTIVE
    mock_generator.fsm = mock_fsm

    with patch.object(svc, "_build_components", new=AsyncMock()), \
         patch.object(svc, "_reconcile_on_boot", new=AsyncMock()), \
         patch.object(svc, "_register_canary_slices"), \
         patch.object(svc, "_attach_to_stack"):
        svc._generator = mock_generator
        svc._orchestrator = MagicMock()
        svc._approval_provider = MagicMock()
        svc._state = ServiceState.STARTING
        await svc.start()

    assert svc.state == ServiceState.ACTIVE, (
        f"Expected ACTIVE for FALLBACK_ACTIVE FSM, got {svc.state}"
    )
    await svc.stop()


async def test_ac3_queue_only_still_maps_to_degraded(tmp_path):
    """QUEUE_ONLY FSM state → ServiceState.DEGRADED (no providers = genuinely degraded)."""
    config = GovernedLoopConfig(project_root=tmp_path)
    stack = _make_mock_stack()
    svc = GovernedLoopService(stack=stack, prime_client=None, config=config)

    mock_generator = MagicMock()
    mock_fsm = MagicMock()
    mock_fsm.state = FailbackState.QUEUE_ONLY
    mock_generator.fsm = mock_fsm

    with patch.object(svc, "_build_components", new=AsyncMock()), \
         patch.object(svc, "_reconcile_on_boot", new=AsyncMock()), \
         patch.object(svc, "_register_canary_slices"), \
         patch.object(svc, "_attach_to_stack"):
        svc._generator = mock_generator
        svc._orchestrator = MagicMock()
        svc._approval_provider = MagicMock()
        svc._state = ServiceState.STARTING
        await svc.start()

    assert svc.state == ServiceState.DEGRADED
    await svc.stop()
