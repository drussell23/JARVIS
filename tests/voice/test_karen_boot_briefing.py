# tests/voice/test_karen_boot_briefing.py
"""Boot briefing: live-state composition, DW primary, 4s breaker that
provably falls back to the LOCAL live-state line without stalling (spec
§3.3, Mandate 4). No canned strings: every output embeds real vectors."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.comms.karen_boot_briefing import (
    BootBriefing, BriefingVectors, compose_local, gather_vectors,
)


V = BriefingVectors(last_session="apply=AUTO/3 verify=4/4 commit=9f3c21ab",
                    queued_ops=2, posture="EXPLORE", pending_approvals=1)


class SlowSynth:
    """Synthesizer that never completes inside the deadline."""
    def __init__(self):
        self.cancelled = False
    async def synthesize(self, view):
        try:
            await asyncio.sleep(30)
            yield "never"
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class GoodSynth:
    async def synthesize(self, view):
        yield "Right then -- awake."
        yield "Two ops queued overnight."


def test_compose_local_embeds_live_vectors():
    line = compose_local(V)
    assert "2" in line and "EXPLORE".lower() in line.lower()
    assert "1" in line                       # pending approval surfaced


def test_compose_local_empty_state_is_factual_not_boilerplate():
    line = compose_local(BriefingVectors())
    assert "first session" in line.lower()


@pytest.mark.asyncio
async def test_breaker_falls_back_fast_without_stalling():
    """MATHEMATICAL PROOF (Mandate 4): wall-clock bound. timeout=0.2s; a
    30s-hanging LLM must yield the local fallback in < 1.0s."""
    spoken = []
    synth = SlowSynth()
    b = BootBriefing(synthesizer=synth, speak_sink=spoken.append,
                     timeout_s=0.2, vectors_override=V)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    line = await b.run()
    elapsed = loop.time() - t0
    assert elapsed < 1.0                     # CLI init NOT stalled
    assert "2" in line                       # fallback is the LIVE-state line
    assert spoken and spoken[-1] == line
    await asyncio.sleep(0.05)
    assert synth.cancelled is True           # hung task actually cancelled


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_not_swallowed():
    """SHUTDOWN SAFETY: asyncio.wait_for raises TimeoutError (not
    CancelledError) on its OWN deadline, so run() must NOT catch
    CancelledError at the breaker site. A genuine outer cancellation (boot
    shutdown while awaiting DW) MUST propagate out of run() — never be eaten
    by the local-fallback path. Task 8 runs run() inside a cancellable boot."""
    spoken = []
    synth = SlowSynth()
    # Long timeout so the breaker CANNOT trip first — only outer cancel acts.
    b = BootBriefing(synthesizer=synth, speak_sink=spoken.append,
                     timeout_s=5.0, vectors_override=V)
    task = asyncio.ensure_future(b.run())
    await asyncio.sleep(0.1)                  # let it reach the DW await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task                           # propagates, NOT swallowed
    assert spoken == []                      # nothing delivered on shutdown


@pytest.mark.asyncio
async def test_primary_path_speaks_sentences():
    spoken = []
    b = BootBriefing(synthesizer=GoodSynth(), speak_sink=spoken.append,
                     timeout_s=2.0, vectors_override=V)
    line = await b.run()
    assert spoken == ["Right then -- awake.", "Two ops queued overnight."]
    assert "awake" in line.lower()


@pytest.mark.asyncio
async def test_no_synthesizer_goes_straight_to_local():
    spoken = []
    b = BootBriefing(synthesizer=None, speak_sink=spoken.append,
                     vectors_override=V)
    line = await b.run()
    assert "2" in line and spoken == [line]


@pytest.mark.asyncio
async def test_voice_off_text_sink_only():
    texts = []
    b = BootBriefing(synthesizer=None, speak_sink=None, text_sink=texts.append,
                     vectors_override=V)
    line = await b.run()
    assert texts == [line]


@pytest.mark.asyncio
async def test_run_never_raises(monkeypatch):
    class ExplodingSynth:
        async def synthesize(self, view):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover
    b = BootBriefing(synthesizer=ExplodingSynth(),
                     speak_sink=lambda s: (_ for _ in ()).throw(RuntimeError("sink!")),
                     vectors_override=V)
    line = await b.run()                     # absolutely must not raise
    assert isinstance(line, str)


@pytest.mark.asyncio
async def test_gather_vectors_is_fault_isolated(monkeypatch):
    def bad_probe():
        raise RuntimeError("intake exploded")
    v = await gather_vectors(intake_probe=bad_probe, approvals_probe=None)
    assert v.queued_ops is None              # absent, not raised
