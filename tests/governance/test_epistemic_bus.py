"""Unified Epistemic Memory Bus spine — the split-brain cure.

Mandate 4 verbatim (2026-07-19): a Daniel visual command followed by
a Karen codebase command → Karen inherits the EXACT cached VLM
payload, Quartz capture_screen is called strictly ONCE, and a
simulated TTL expiry flushes the bus.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.duplex import epistemic_bus as eb
from backend.core.ouroboros.governance.comms.duplex.epistemic_bus import (
    EpistemicMemoryBus,
)
from backend.core.ouroboros.governance.comms.duplex.semantic_gaze import (
    VERDICT_ANALYZED,
    VERDICT_CACHED,
    SemanticGaze,
)


@pytest.fixture(autouse=True)
def _fresh_bus():
    eb.reset_default_bus()
    yield
    eb.reset_default_bus()


def _gaze(captures, vlm_calls, hash_value="H1"):
    async def _vlm(frame, cmd):
        vlm_calls.append(cmd)
        return "error: NoneType in orchestrator.py line 42"

    def _capture():
        captures.append(1)
        return b"frame-bytes"

    return SemanticGaze(
        lease_active=lambda: True, thermal_ok=lambda: True,
        capture=_capture, frame_hash=lambda f: hash_value, vlm=_vlm,
    )


class TestCrossPersonaHandoff:
    async def test_daniel_to_karen_single_capture_shared_payload(
        self, monkeypatch,
    ):
        """MANDATE 4 VERBATIM."""
        captures, vlm_calls = [], []
        daniel_gaze = _gaze(captures, vlm_calls)
        # Daniel: "what is this error on my screen?"
        r1 = await daniel_gaze.request("what is this error on my screen")
        assert r1["verdict"] == VERDICT_ANALYZED
        assert len(captures) == 1
        # Karen (a DIFFERENT gaze instance — different persona FSM):
        karen_gaze = _gaze(captures, vlm_calls)
        r2 = await karen_gaze.request("Karen, write a patch for this code")
        assert r2["verdict"] == VERDICT_CACHED
        assert r2["semantic_state"] == r1["semantic_state"]   # EXACT payload
        assert len(captures) == 1                # Quartz called ONCE, total
        assert len(vlm_calls) == 1               # VLM once, total
        bus = eb.get_default_bus()
        assert bus.stats["deposits"] == 1 and bus.stats["inherits"] >= 1

    async def test_ttl_expiry_flushes_and_forces_fresh_capture(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_EPISTEMIC_VISUAL_TTL_S", "60")
        clock = [1000.0]
        bus = EpistemicMemoryBus(clock=lambda: clock[0])
        monkeypatch.setattr(eb, "_DEFAULT", bus)
        captures, vlm_calls = [], []
        g = _gaze(captures, vlm_calls)
        g._bus = bus
        await g.request("look at the screen")
        assert len(captures) == 1
        clock[0] += 61.0                          # simulated TTL expiry
        assert bus.inherit_visual() is None       # flushed at read
        assert bus.stats["ttl_flushes"] == 1
        await g.request("look at the screen")
        assert len(captures) == 2                 # fresh capture forced

    async def test_spatial_invalidation_on_catastrophic_delta(self):
        captures, vlm_calls = [], []
        hashes = iter(["SPACE1", "SPACE2"])
        async def _vlm(frame, cmd):
            vlm_calls.append(cmd); return f"state-{len(vlm_calls)}"
        g = SemanticGaze(
            lease_active=lambda: True, thermal_ok=lambda: True,
            capture=lambda: captures.append(1) or b"f",
            frame_hash=lambda f: next(hashes), vlm=_vlm,
        )
        await g.request("look at my screen now")   # 'now' forces capture
        bus = eb.get_default_bus()
        await g.request("look at my screen now")   # Space swapped → SPACE2
        assert bus.stats["spatial_flushes"] == 1   # old state flushed
        assert len(vlm_calls) == 2                 # re-analyzed fresh

    async def test_explicit_relook_overrides_inheritance(self):
        captures, vlm_calls = [], []
        g = _gaze(captures, vlm_calls)
        await g.request("look at the screen")
        await g.request("look again")              # operator demands fresh
        assert len(captures) == 2

    def test_bridge_and_compactor_integration_pins(self):
        from pathlib import Path
        cb_src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/conversation_bridge.py"
        ).read_text()
        assert "SOURCE_VISUAL" in cb_src
        assert "def record_visual_state" in cb_src
        assert "### Visual context (shared gaze)" in cb_src
        gz_src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/semantic_gaze.py"
        ).read_text()
        assert "get_default_bus" in gz_src
        assert "_cached_state" not in gz_src       # persona-local cache GONE

    async def test_visual_state_reaches_prompt_plane(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
        from backend.core.ouroboros.governance import conversation_bridge as cb
        cb.reset_default_bridge()
        try:
            captures, vlm_calls = [], []
            g = _gaze(captures, vlm_calls)
            await g.request("what is on my screen")
            prompt = cb.get_default_bridge().format_for_prompt()
            assert prompt is not None
            assert "Visual context (shared gaze)" in prompt
            assert "NoneType in orchestrator.py" in prompt
        finally:
            cb.reset_default_bridge()
