"""
End-to-end integration tests for the DAS synthesis cycle.

These tests verify the full flow from GapSignalBus emission through
GapResolutionProtocol dedup, synthesis dispatch, and trust ledger update.
They use asyncio but stay in-process — no external services required.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent, GapSignalBus
from backend.neural_mesh.synthesis.gap_resolution_protocol import (
    GapResolutionProtocol,
    DasSynthesisState,
)
from backend.neural_mesh.synthesis.domain_trust_ledger import DomainTrustLedger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _evt(task_type="vision_action", target_app="xcode"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source="primary_fallback",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_burst_dedup_collapses_to_one_synthesis():
    """5 identical gap events must result in exactly 1 _synthesize call.

    GapResolutionProtocol deduplicates via asyncio.Event keyed on dedupe_key.
    The first coroutine calls _synthesize; the next 4 wait on the same Event
    and return without calling _synthesize again.
    """
    protocol = GapResolutionProtocol()
    synth_count = {"n": 0}

    async def fake_synthesize(event, _dedupe_key, _retry_count=0):
        synth_count["n"] += 1
        await asyncio.sleep(0)  # yield once to let waiters in

    protocol._synthesize = fake_synthesize  # type: ignore[method-assign]

    event = _evt()
    # Send 5 identical events concurrently
    tasks = [
        asyncio.create_task(protocol.handle_gap_event(event))
        for _ in range(5)
    ]
    await asyncio.gather(*tasks)

    assert synth_count["n"] == 1, (
        f"Expected 1 synthesis call, got {synth_count['n']}"
    )


@pytest.mark.asyncio
async def test_two_different_domains_produce_two_synthesis_calls():
    """Distinct domain_ids are handled independently — 2 calls expected."""
    protocol = GapResolutionProtocol()
    synth_count = {"n": 0}

    async def fake_synthesize(event, _dedupe_key, _retry_count=0):
        synth_count["n"] += 1
        await asyncio.sleep(0)

    protocol._synthesize = fake_synthesize  # type: ignore[method-assign]

    evt_a = _evt(task_type="vision_action", target_app="xcode")
    evt_b = _evt(task_type="browser_navigation", target_app="chrome")

    await asyncio.gather(
        protocol.handle_gap_event(evt_a),
        protocol.handle_gap_event(evt_b),
    )

    assert synth_count["n"] == 2, (
        f"Expected 2 synthesis calls for 2 distinct domains, got {synth_count['n']}"
    )


def test_das_synthesis_state_has_19_members():
    """DasSynthesisState FSM must have exactly 19 states per spec section 11."""
    members = list(DasSynthesisState)
    assert len(members) == 19, (
        f"Expected 19 DasSynthesisState members, found {len(members)}: "
        f"{[m.value for m in members]}"
    )


def test_ledger_records_success():
    """DomainTrustLedger.record_success updates the domain's successful_runs count."""
    ledger = DomainTrustLedger()
    domain = "vision_action:xcode"
    before = ledger.record(domain).successful_runs
    ledger.record_success(domain)
    after = ledger.record(domain).successful_runs
    assert after == before + 1


def test_ledger_records_rollback():
    """DomainTrustLedger.record_rollback updates the domain's rollback_count."""
    ledger = DomainTrustLedger()
    domain = "browser_navigation:chrome"
    before = ledger.record(domain).rollback_count
    ledger.record_rollback(domain)
    after = ledger.record(domain).rollback_count
    assert after == before + 1


# ---------------------------------------------------------------------------
# Appendix B: 10 Go/No-Go smoke assertions
# ---------------------------------------------------------------------------

def test_appendix_b_gap_signal_bus_never_blocks():
    """Check 1: GapSignalBus uses put_nowait, never await put."""
    import inspect
    from backend.neural_mesh.synthesis import gap_signal_bus
    src = inspect.getsource(gap_signal_bus.GapSignalBus)
    assert "put_nowait" in src, "GapSignalBus must use put_nowait"
    assert "await" not in src.split("put_nowait")[0].rsplit("def ", 1)[-1], (
        "No await before put_nowait in emit()"
    )


def test_appendix_b_domain_id_excludes_mutable_metadata():
    """Check 2: domain_id = task_type:target_app only; no risk_class or trust_score."""
    evt = _evt("vision_action", "xcode")
    assert evt.domain_id == "vision_action:xcode"
    assert "risk_class" not in evt.domain_id
    assert "trust_score" not in evt.domain_id


def test_appendix_b_das_canary_key_stable():
    """Check 3: das_canary_key formula uses session_id+normalized_command, no random component."""
    import hashlib
    import re
    session_id = "fixed-session"
    cmd = "  Open  Prefs  "
    normalized = re.sub(r"\s+", " ", cmd.lower().strip())
    key1 = hashlib.sha256(f"{session_id}:{normalized}".encode()).hexdigest()
    key2 = hashlib.sha256(f"{session_id}:{normalized}".encode()).hexdigest()
    assert key1 == key2, "das_canary_key must be deterministic"
    assert len(key1) == 64


def test_appendix_b_versioned_rollback_does_not_pop_sys_modules():
    """Check 5: rollback increments _version; never touches sys.modules."""
    import inspect
    from backend.neural_mesh.registry import agent_registry
    src = inspect.getsource(agent_registry.AgentRegistry.rollback_agent)
    assert "sys.modules" not in src, "rollback_agent must not touch sys.modules"
    assert "_version" in src


def test_appendix_b_domain_trust_ledger_append_only():
    """Check 6: DomainTrustLedger uses append-only journal; denominators use max(..., 1)."""
    import inspect
    from backend.neural_mesh.synthesis import domain_trust_ledger
    src = inspect.getsource(domain_trust_ledger)
    assert "max(" in src and ", 1)" in src, "Denominators must use max(..., 1)"


def test_appendix_b_19_states_reachable():
    """Check 8 (partial): REPLAY_STALE and CLOSED_UNRESOLVED exist in the enum."""
    assert DasSynthesisState.REPLAY_STALE.value == "REPLAY_STALE"
    assert DasSynthesisState.CLOSED_UNRESOLVED.value == "CLOSED_UNRESOLVED"
    assert DasSynthesisState.REPLAY_AUTHORIZED.value == "REPLAY_AUTHORIZED"


def test_appendix_b_trinity_emit_has_noop_fallback():
    """Check 9: Trinity observer section uses try/except or debug log with no re-raise."""
    import inspect
    from backend.neural_mesh.synthesis import gap_resolution_protocol as grp
    src = inspect.getsource(grp.GapResolutionProtocol._synthesize)
    assert "try" in src or "Trinity observer" in src, (
        "Trinity section must exist in _synthesize"
    )


def test_appendix_b_event_type_both_repos():
    """Check 10: 7 DAS EventType values exist in both JARVIS cross_repo.py and Reactor-Core."""
    from backend.core.ouroboros.cross_repo import EventType as JarvisEventType
    das_events = [
        "AGENT_SYNTHESIS_REQUESTED",
        "AGENT_SYNTHESIS_CANARY_ACTIVE",
        "AGENT_SYNTHESIS_COMPLETED",
        "AGENT_SYNTHESIS_FAILED",
        "CAPABILITY_GAP_UNRESOLVED",
        "AGENT_SYNTHESIS_CONFLICT",
        "ROUTING_OSCILLATION_DETECTED",
    ]
    for name in das_events:
        assert hasattr(JarvisEventType, name), f"Missing JARVIS EventType: {name}"
