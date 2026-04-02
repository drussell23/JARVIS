#!/usr/bin/env python3
"""
Ouroboros HUD E2E Pipeline Test
================================

Proves the full governance pipeline is wired in HUD mode:
CU failures -> CUExecutionSensor -> IntakeLayerService router -> GovernedLoopService

Run:
    python3 -m pytest tests/integration/test_hud_e2e_pipeline.py -v
"""
import asyncio
import sys
from pathlib import Path

import pytest

# Ensure repo root is on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def fresh_cu_singleton():
    """Reset CUExecutionSensor singleton before/after test."""
    from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
        CUExecutionSensor,
    )
    CUExecutionSensor._instance = None
    yield
    CUExecutionSensor._instance = None


def _make_failure_record():
    """Build a CU failure record with a deterministic signature."""
    from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
        CUExecutionRecord,
    )
    return CUExecutionRecord(
        goal="send message to Alice",
        success=False,
        steps_completed=2,
        steps_total=5,
        elapsed_s=3.0,
        error="target not found",
        is_messaging=True,
        contact="Alice",
        app="messages",
    )


@pytest.mark.asyncio
async def test_hud_e2e_cu_graduation_through_pipeline(tmp_path, fresh_cu_singleton):
    """Full E2E: HUD governance boot -> CU failures -> sensor graduation -> router.ingest().

    This proves the entire spinal cord is connected in HUD mode:
    1. start_hud_governance() boots GovernanceStack + GLS + IntakeLayerService
    2. IntakeLayerService wires CUExecutionSensor to the router (Sub-project A)
    3. 3 CU failures trigger graduation
    4. Envelope reaches the router (not dropped at 'No router wired')
    """
    from backend.core.ouroboros.governance.hud_governance_boot import (
        start_hud_governance,
        stop_hud_governance,
    )
    from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
        CUExecutionSensor,
    )

    # Create a minimal Python file so the governance stack has something to work with
    target = tmp_path / "backend" / "vision"
    target.mkdir(parents=True)
    (target / "cu_task_planner.py").write_text(
        "def plan_goal(goal, frame):\n    return []\n"
    )

    # Boot full governance in HUD mode
    ctx = await start_hud_governance(project_root=tmp_path)

    try:
        # Verify governance booted (at least partially)
        assert ctx.stack is not None, "GovernanceStack should be created"

        # Check if GLS started (may be degraded without real J-Prime)
        if ctx.gls is None:
            pytest.skip("GLS failed to start (expected in test env without full stack)")

        # Get the CUExecutionSensor singleton — should now have router from IntakeLayerService
        sensor = CUExecutionSensor()

        if sensor._router is None:
            # IntakeLayerService may not have started
            if ctx.intake is None:
                pytest.skip("IntakeLayerService failed to start (expected in minimal test env)")
            pytest.fail("CUExecutionSensor has no router despite IntakeLayerService running")

        # Spy on router.ingest
        original_ingest = sensor._router.ingest
        ingest_calls = []

        async def spy_ingest(envelope):
            ingest_calls.append(envelope)
            return await original_ingest(envelope)

        sensor._router.ingest = spy_ingest

        # Feed 3 CU failures to cross graduation threshold
        for _ in range(3):
            await sensor.record(_make_failure_record())

        # Verify envelope was emitted and reached the router
        assert sensor._total_envelopes_emitted >= 1, (
            "Sensor did not emit any envelopes after 3 failures — graduation broken"
        )
        assert len(ingest_calls) >= 1, (
            "Router.ingest was never called — envelope dropped between sensor and router"
        )

        # Verify envelope metadata
        envelope = ingest_calls[0]
        assert envelope.source == "cu_execution"
        assert envelope.repo == "jarvis"

        print(f"\n{'=' * 60}")
        print("  HUD E2E PIPELINE TEST — PASS")
        print(f"{'=' * 60}")
        print(f"  GovernanceStack: {'ACTIVE' if ctx.stack else 'NONE'}")
        print(f"  GovernedLoopService: {ctx.gls.state.name if ctx.gls else 'NONE'}")
        print(f"  IntakeLayerService: {ctx.intake.state.name if ctx.intake else 'NONE'}")
        print(f"  CUExecutionSensor router: {'WIRED' if sensor._router else 'NONE'}")
        print(f"  Envelopes emitted: {sensor._total_envelopes_emitted}")
        print(f"  Router.ingest calls: {len(ingest_calls)}")
        print(f"  Envelope source: {envelope.source}")
        print(f"{'=' * 60}\n")

    finally:
        await stop_hud_governance(ctx)
