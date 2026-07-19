"""Ambient OS Phase 1+2 spine — dual personas + conditional wake briefing.

Operator authorization 2026-07-19. Mandate-4 verbatim case: a mocked
``NSWorkspaceDidWakeNotification`` with CoreAudio state volume=100% +
no headphones must ABORT the TTS generation and emit ONLY a TUI
payload (the Coffee-Shop Protocol — no public acoustic leaks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.comms.duplex import ambient
from backend.core.ouroboros.governance.comms.duplex.ambient import (
    PERSONA_DANIEL,
    PERSONA_KAREN,
    SystemWakeObserver,
    classify_persona,
    speech_permitted,
)

_REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# (1) Phase 1 — deterministic persona routing
# ---------------------------------------------------------------------------


class TestPersonaRouting:
    def test_semantic_classes_route_deterministically(self):
        assert classify_persona("system") == PERSONA_DANIEL
        assert classify_persona("briefing") == PERSONA_DANIEL
        assert classify_persona("wake") == PERSONA_DANIEL
        assert classify_persona("engineering") == PERSONA_KAREN
        assert classify_persona("codebase") == PERSONA_KAREN
        assert classify_persona("test") == PERSONA_KAREN

    def test_unknown_class_defaults_to_karen(self):
        assert classify_persona("quantum") == PERSONA_KAREN
        assert classify_persona("") == PERSONA_KAREN
        assert classify_persona(None) == PERSONA_KAREN

    def test_voice_names_env_tunable_never_hardcoded(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PERSONA_VOICE_DANIEL", "Oliver")
        monkeypatch.setenv("JARVIS_PERSONA_VOICE_KAREN", "Samantha")
        assert ambient.persona_voice(PERSONA_DANIEL) == "Oliver"
        assert ambient.persona_voice(PERSONA_KAREN) == "Samantha"

    def test_speech_request_carries_persona_lane(self):
        from backend.core.ouroboros.governance.comms.duplex.protocols import (
            Priority,
            SpeechRequest,
        )
        req = SpeechRequest("hello", Priority.PROACTIVE_INFO)
        assert req.persona == "karen"          # engineering default
        req2 = SpeechRequest(
            "status", Priority.PROACTIVE_INFO, persona="daniel",
        )
        assert req2.persona == "daniel"

    async def test_arbiter_forwards_persona_to_playback(self):
        from backend.core.ouroboros.governance.comms.duplex.arbiter import (
            VoiceDuplexArbiter,
        )
        from backend.core.ouroboros.governance.comms.duplex.protocols import (
            ArbiterConfig,
            Priority,
            SpeechRequest,
        )

        seen: list = []

        class _Playback:
            is_active = False

            def preempt(self) -> None:
                pass

            async def play(self, text: str, *, persona: str = "karen") -> None:
                seen.append((text, persona))

        arb = VoiceDuplexArbiter(
            _Playback(),
            config=ArbiterConfig(enabled=True, proactive_enabled=True),
        )
        run = asyncio.get_running_loop().create_task(arb.run())
        try:
            arb.submit(SpeechRequest(
                "system status", Priority.PROACTIVE_INFO, persona="daniel",
            ))
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.01)
            assert seen == [("system status", "daniel")]
        finally:
            await arb.stop()
            run.cancel()
            try:
                await run
            except (asyncio.CancelledError, Exception):
                pass

    def test_submit_speech_routes_by_semantic_class_pin(self):
        src = (
            _REPO
            / "backend/core/ouroboros/governance/comms/duplex/karen_duplex_factory.py"
        ).read_text()
        assert "classify_persona(semantic_class)" in src


# ---------------------------------------------------------------------------
# (2) Coffee-Shop Protocol
# ---------------------------------------------------------------------------


class TestCoffeeShopProtocol:
    def test_loud_speakers_no_private_output_blocks(self, monkeypatch):
        monkeypatch.delenv("JARVIS_AMBIENT_VOLUME_THRESHOLD", raising=False)
        assert speech_permitted(
            {"volume": 1.0, "external_output": False},
        ) is False
        assert speech_permitted(
            {"volume": 0.5, "external_output": False},
        ) is False

    def test_quiet_speakers_or_headphones_permit(self):
        assert speech_permitted(
            {"volume": 0.2, "external_output": False},
        ) is True
        assert speech_permitted(
            {"volume": 1.0, "external_output": True},
        ) is True                              # private output = safe

    def test_unreadable_topology_fails_closed_silent(self):
        assert speech_permitted({}) is False   # defaults: vol 1.0, no ext
        assert speech_permitted({"volume": "??"}) is False


# ---------------------------------------------------------------------------
# (3) MANDATE 4 VERBATIM — mocked wake, 100% volume, no headphones
# ---------------------------------------------------------------------------


def _observer(delta, topology, spoken: list, tui: list) -> SystemWakeObserver:
    async def _speak(text: str, persona: str) -> bool:
        spoken.append((text, persona))
        return True

    return SystemWakeObserver(
        delta_provider=lambda: delta,
        topology_probe=lambda: topology,
        speaker=_speak,
        silent_sink=tui.append,
    )


class TestWakeBriefing:
    async def test_wake_at_full_volume_no_headphones_aborts_tts(self):
        """The verbatim case: TTS generation aborted; ONLY the TUI
        payload is emitted."""
        spoken: list = []
        tui: list = []
        obs = _observer(
            [{"text": "Karen landed the routing fix."}],
            {"volume": 1.0, "external_output": False},
            spoken, tui,
        )
        verdict = await obs.handle_system_wake()
        assert verdict == "suppressed_tui"
        assert spoken == []                    # no acoustic leak
        assert len(tui) == 1 and "Karen landed" in tui[0]
        assert obs.stats["suppressed_tui"] == 1

    async def test_zero_delta_aborts_entirely_silent(self):
        spoken: list = []
        tui: list = []
        obs = _observer([], {"volume": 0.1, "external_output": True},
                        spoken, tui)
        assert await obs.handle_system_wake() == "no_delta"
        assert spoken == [] and tui == []      # knows when to shut up

    async def test_safe_topology_speaks_as_daniel(self):
        spoken: list = []
        tui: list = []
        obs = _observer(
            [{"text": "Two ops verified while you were away."}],
            {"volume": 0.15, "external_output": False},
            spoken, tui,
        )
        assert await obs.handle_system_wake() == "spoken"
        assert len(spoken) == 1
        text, persona = spoken[0]
        assert persona == PERSONA_DANIEL       # system plane voice
        assert text.startswith("Welcome back.")
        assert len(tui) == 1                   # TUI mirror always lands

    async def test_massive_backlog_compresses_before_tts(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BRIEFING_MAX_CHARS", "120")
        spoken: list = []
        tui: list = []
        delta = [
            {"text": f"task {i} resolved with a long narrative line"}
            for i in range(40)                 # laptop closed for days
        ]
        obs = _observer(
            delta, {"volume": 0.1, "external_output": True}, spoken, tui,
        )
        assert await obs.handle_system_wake() == "spoken"
        assert obs.stats["compressed"] == 1
        text, _ = spoken[0]
        # High-level abstraction, never a multi-minute monologue.
        assert len(text) < 400

    async def test_hostile_collaborators_never_raise(self):
        def _boom():
            raise RuntimeError("delta source died")

        obs = SystemWakeObserver(
            delta_provider=_boom,
            topology_probe=lambda: {},
            speaker=None,
            silent_sink=None,
        )
        assert await obs.handle_system_wake() == "degraded"

    def test_bootstrap_mounts_observer_gated_pin(self):
        src = (_REPO / "backend/audio/audio_pipeline_bootstrap.py").read_text()
        assert "JARVIS_AMBIENT_WAKE_ENABLED" in src
        assert "SystemWakeObserver" in src
