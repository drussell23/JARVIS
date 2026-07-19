"""Semantic Interruption Awareness + Crest Geometry Polish spine.

Operator authorization 2026-07-18:

  * A barge-in / flush must not merely halt audio — the prompt plane
    must structurally learn the narration was CUT OFF (marker with
    approximate truncation) so the next GENERATE never hallucinates a
    fully-delivered payload. Channel: ConversationBridge (the existing
    sanitized dialogue→CONTEXT_EXPANSION lane; GapSignalBus was
    evaluated and rejected — it is the typed CapabilityGapEvent intake
    bus, and routing markers through it would forge gap signals).
  * Crest raster gains mathematical density clips: detached
    sub-threshold components pruned, apex whisker rows shed; the
    ceremony skips animation entirely when the frame exceeds the
    viewport (the stale-Live duplication artifact).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.comms.duplex.arbiter import (
    VoiceDuplexArbiter,
)
from backend.core.ouroboros.governance.comms.duplex.protocols import (
    ArbiterConfig,
    Priority,
    SpeechRequest,
    VoiceState,
)

_REPO = Path(__file__).resolve().parents[2]

_CFG = ArbiterConfig(
    enabled=True, barge_in_enabled=True, proactive_enabled=True,
)


class _SlowPlayback:
    def __init__(self) -> None:
        self.preempts = 0

    def preempt(self) -> None:
        self.preempts += 1

    async def play(self, _text: str) -> None:
        await asyncio.sleep(3600)


async def _speaking_arbiter(text: str = "the full narration payload " * 4):
    arb = VoiceDuplexArbiter(_SlowPlayback(), config=_CFG)
    run = asyncio.get_running_loop().create_task(arb.run())
    arb.submit(SpeechRequest(text, Priority.PROACTIVE_INFO))
    for _ in range(100):
        if arb.state == VoiceState.KAREN_SPEAKING:
            break
        await asyncio.sleep(0.01)
    assert arb.state == VoiceState.KAREN_SPEAKING
    return arb, run


async def _teardown(arb, run):
    await arb.stop()
    run.cancel()
    try:
        await run
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# (1) The arbiter interruption seams
# ---------------------------------------------------------------------------


class TestArbiterInterruption:
    async def test_flush_reports_truncation_and_halts_buffer(self):
        arb, run = await _speaking_arbiter()
        reports: list = []
        arb.on_interruption = lambda t, d, c: reports.append((t, d, c))
        try:
            await asyncio.sleep(0.25)
            arb.flush()
            assert arb._playback.preempts == 1        # buffer halted
            assert len(reports) == 1
            text, delivered, cause = reports[0]
            assert cause == "operator_flush"
            assert 0 <= delivered < len(text)          # truncated, not full
        finally:
            await _teardown(arb, run)

    async def test_vad_barge_in_reports_with_distinct_cause(self):
        arb, run = await _speaking_arbiter()
        reports: list = []
        arb.on_interruption = lambda t, d, c: reports.append(c)
        try:
            await arb.on_user_speech_start()
            assert reports == ["vad_barge_in"]
        finally:
            await _teardown(arb, run)

    async def test_truncation_estimate_scales_with_rate(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TTS_CHARS_PER_S", "60")
        arb, run = await _speaking_arbiter("x" * 400)
        reports: list = []
        arb.on_interruption = lambda t, d, c: reports.append(d)
        try:
            await asyncio.sleep(0.5)
            arb.flush()
            delivered = reports[0]
            # ~0.5s at 60 chars/s ≈ 30 chars (generous CI bounds).
            assert 5 <= delivered <= 120
        finally:
            await _teardown(arb, run)

    async def test_no_report_when_idle_and_reporter_faults_contained(self):
        arb = VoiceDuplexArbiter(_SlowPlayback(), config=_CFG)
        calls: list = []
        arb.on_interruption = lambda *a: calls.append(a)
        arb.flush()                        # idle — nothing was speaking
        assert calls == []
        arb._active_text = "mid-flight"
        arb._state = VoiceState.KAREN_SPEAKING
        arb.on_interruption = lambda *a: (_ for _ in ()).throw(RuntimeError())
        arb.flush()                        # hostile reporter: must not raise


# ---------------------------------------------------------------------------
# (2) MANDATE 4 VERBATIM — flush + marker lands before the next GENERATE
# ---------------------------------------------------------------------------


class TestBargeMarkerReachesPromptPlane:
    async def test_tts_interruption_appends_marker_to_context_channel(
        self, monkeypatch,
    ):
        """Mock a TTS interruption: audio buffer flushed AND the
        barge-in semantic marker is present in the composed
        CONTEXT_EXPANSION prompt (the prompt history the next GENERATE
        consumes)."""
        monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
        from backend.core.ouroboros.governance import conversation_bridge as cb
        cb.reset_default_bridge()
        try:
            arb, run = await _speaking_arbiter(
                "Deploying the fix now; three files will change and the "
                "regression suite will re-run automatically afterwards.",
            )
            arb.on_interruption = cb.record_barge_in   # production wiring
            try:
                await asyncio.sleep(0.2)
                arb.flush()
                assert arb._playback.preempts == 1     # (a) buffer flushed
            finally:
                await _teardown(arb, run)
            prompt = cb.get_default_bridge().format_for_prompt()
            assert prompt is not None
            assert "OPERATOR_BARGE_IN_DETECTED" in prompt      # (b) marker
            assert "Do not assume the operator heard" in prompt
            assert "Delivery interruptions" in prompt
        finally:
            cb.reset_default_bridge()

    async def test_marker_disabled_bridge_is_silent_noop(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "false")
        from backend.core.ouroboros.governance import conversation_bridge as cb
        assert cb.record_barge_in("some text", 3, "flush") is False

    def test_marker_carries_truncation_math(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
        from backend.core.ouroboros.governance import conversation_bridge as cb
        cb.reset_default_bridge()
        try:
            assert cb.record_barge_in("a" * 100, 25, "vad_barge_in") is True
            prompt = cb.get_default_bridge().format_for_prompt()
            assert "~25%" in prompt and "25/100 chars" in prompt
            assert "cause=vad_barge_in" in prompt
        finally:
            cb.reset_default_bridge()

    def test_bootstrap_wires_reporter_pin(self):
        src = (_REPO / "backend/audio/audio_pipeline_bootstrap.py").read_text()
        assert src.count("on_interruption") >= 2   # lease + pre-mount paths
        assert "record_barge_in" in src


# ---------------------------------------------------------------------------
# (3) Crest Geometry Polish
# ---------------------------------------------------------------------------


class TestCrestGeometryPolish:
    def test_apex_whisker_rows_are_shed(self, monkeypatch):
        monkeypatch.delenv("JARVIS_OV_CREST_MIN_ROW_PX", raising=False)
        from backend.core.ouroboros.ui import crest
        crest._generate_pixels_cached.cache_clear()
        pf = crest.generate_crest_pixels(89, 60)
        rows: dict = {}
        for (_x, y) in pf.pixels:
            rows[y] = rows.get(y, 0) + 1
        top = min(rows)
        # The topmost surviving raster row is DENSE — the 1-pixel tail
        # whisker that read as floating debris is mathematically shed.
        assert rows[top] >= crest._min_apex_row_px()

    def test_detached_subthreshold_component_clipped(self):
        from backend.core.ouroboros.ui import crest
        body = {(x, y): ((200, 80, 40), 0.0)
                for x in range(10) for y in range(10)}
        debris = {(40, 40): ((200, 80, 40), 0.0), (41, 40): ((200, 80, 40), 0.0)}
        pruned = crest._prune_sparse_geometry({**body, **debris})
        assert (40, 40) not in pruned and (41, 40) not in pruned
        assert (5, 5) in pruned                      # body intact

    def test_largest_component_always_survives(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OV_CREST_MIN_COMPONENT_PX", "9999")
        from backend.core.ouroboros.ui import crest
        tiny = {(x, 5): ((10, 10, 10), 0.0) for x in range(5)}
        pruned = crest._prune_sparse_geometry(dict(tiny))
        assert len(pruned) == 5                      # body never self-deletes

    def test_renderer_trims_emptied_leading_rows(self):
        from backend.core.ouroboros.ui import crest
        crest._generate_pixels_cached.cache_clear()
        pf = crest.generate_crest_pixels(89, 60)
        plain = crest.pixels_to_text(pf).plain
        assert plain.split("\n")[0].strip() != ""    # no dead space on top

    def test_ceremony_viewport_guard_pin(self):
        src = (_REPO / "backend/core/ouroboros/ui/awakening.py").read_text()
        body = src[src.index("async def _run_animated"):][:2400]
        assert "_rows_needed" in body
        # Undersized viewport → static emblem path, never a broken Live.
        assert "self._print_cooled_header()" in body
