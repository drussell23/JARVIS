import asyncio
import pytest
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent
from backend.neural_mesh.synthesis.gap_resolution_protocol import (
    GapResolutionProtocol,
    ResolutionMode,
    DasSynthesisState,
)


def _evt(source="primary_fallback", task_type="vision_action", target_app="xcode"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source=source,
    )


def test_resolution_modes_exist():
    assert ResolutionMode.A
    assert ResolutionMode.B
    assert ResolutionMode.C


def test_19_states_defined():
    state_names = {s.name for s in DasSynthesisState}
    required = {
        "GAP_DETECTED", "GAP_COALESCING", "GAP_COALESCED",
        "ROUTE_DECIDED_A", "ROUTE_DECIDED_B", "ROUTE_DECIDED_C",
        "SYNTH_PENDING", "SYNTH_TIMEOUT", "SYNTH_REJECTED",
        "ARTIFACT_WRITTEN", "QUARANTINED_PENDING_REVIEW", "ARTIFACT_VERIFIED",
        "CANARY_ACTIVE", "CANARY_ROLLED_BACK", "AGENT_GRADUATED",
        "REPLAY_AUTHORIZED", "REPLAY_STALE",
        "CLOSED_RESOLVED", "CLOSED_UNRESOLVED",
    }
    assert required == state_names


def test_dream_advisory_always_mode_c():
    protocol = GapResolutionProtocol()
    evt = _evt(source="dream_advisory")
    mode = protocol.classify_mode(evt)
    assert mode == ResolutionMode.C


def test_high_risk_domain_is_mode_a():
    protocol = GapResolutionProtocol()
    evt = _evt(task_type="file_edit", target_app="any")
    mode = protocol.classify_mode(evt)
    assert mode == ResolutionMode.A


def test_screen_observation_is_mode_c():
    protocol = GapResolutionProtocol()
    evt = _evt(task_type="screen_observation", target_app="any")
    mode = protocol.classify_mode(evt)
    assert mode == ResolutionMode.C


@pytest.mark.asyncio
async def test_single_flight_dedup_collapses_burst():
    protocol = GapResolutionProtocol()
    synthesis_calls = []

    async def fake_synthesize(_event, dedupe_key):
        synthesis_calls.append(dedupe_key)
        await asyncio.sleep(0.02)

    protocol._synthesize = fake_synthesize

    evt = _evt()
    tasks = [
        asyncio.create_task(protocol.handle_gap_event(evt))
        for _ in range(5)
    ]
    await asyncio.gather(*tasks)
    assert len(synthesis_calls) == 1
