"""Telling the HUD when JARVIS is speaking — so it can shut its own microphone.

`UnifiedSpeechStateManager` has tracked this correctly for a long time: seven
sources, a length-scaled echo cooldown, a text-similarity check for an echo that
slipped the gate, an `AudioBus` mic gate, and a 60s watchdog. Its
`register_websocket_broadcaster` had **zero callers**, so none of it ever
reached the process that owns the microphone. The HUD instead read
`/tmp/jarvis_speaking`, a file with no liveness, describing one of four
speakers, stat'd in the recognition callback — one layer above the audio it was
supposed to suppress.

The mandated scenario is `test_a_start_mutes_and_a_stop_unmutes`: the manager
speaks, the HUD gets a frame that says so, and the frame carries a deadline.

THE TESTS TO KEEP
-------------------
`test_every_frame_carries_a_bounded_deadline`. A mute is a claim on the
operator's microphone. If the process is killed between start and stop, the
deadline is the only thing that gives it back — the property the lockfile could
never have, because `finally:` does not run on SIGKILL.

`test_a_stop_is_never_throttled_away`. The manager dropped any broadcast inside
50ms of the previous one, regardless of what it said. A short utterance starts
and stops well inside that window, so `speech_ended` was discarded while
`speech_started` had gone out — muted, with nothing left to unmute.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Tuple

import pytest

from backend.hud import speech_bridge as sb


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JARVIS_HUD_SPEECH_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HUD_SPEECH_GRACE_MS", "400")
    monkeypatch.setenv("JARVIS_HUD_SPEECH_MAX_CLAIM_MS", "60000")
    sb.reset()
    yield
    sb.reset()


class _Publisher:
    """Stands in for `ipc_server.publish`, recording every frame."""

    def __init__(self, reached: int = 1) -> None:
        self.frames: List[Tuple[str, Dict[str, Any]]] = []
        self.reached = reached

    def __call__(self, event: str, data: Dict[str, Any]) -> int:
        self.frames.append((event, dict(data)))
        return self.reached

    @property
    def last(self) -> Dict[str, Any]:
        return self.frames[-1][1]


def _message(event: str, *, speaking: bool, text: str = "",
             cooldown_until: float = 0.0,
             started_at: float = 0.0) -> Dict[str, Any]:
    """The exact shape `UnifiedSpeechStateManager._broadcast_state_change` emits."""
    return {
        "type": "speech_state_change",
        "event": event,
        "state": {
            "is_speaking": speaking,
            "speech_started_at": started_at or (time.time() * 1000),
            "speech_ended_at": None if speaking else time.time() * 1000,
            "current_text": text,
            "current_source": "system",
            "cooldown_until": cooldown_until,
            "in_cooldown": cooldown_until > time.time() * 1000,
            "cooldown_remaining_ms": 0,
            "recent_texts_count": 1,
        },
        "timestamp": time.time() * 1000,
    }


class TestTheMandatedScenario:
    def test_a_start_mutes_and_a_stop_unmutes(self):
        pub = _Publisher()
        bridge = sb.HUDSpeechBroadcaster(publish=pub)

        bridge(_message("speech_started", speaking=True, text="On it."))
        bridge(_message("speech_ended", speaking=False))

        assert len(pub.frames) == 2
        assert all(e == sb.SPEECH_STATE_EVENT for e, _ in pub.frames)
        assert pub.frames[0][1]["speaking"] is True
        assert pub.frames[1][1]["speaking"] is False


class TestTheDeadline:
    def test_every_frame_carries_a_bounded_deadline(self):
        """The only thing that returns the mic if this process is SIGKILLed."""
        pub = _Publisher()
        bridge = sb.HUDSpeechBroadcaster(publish=pub)

        bridge(_message("speech_started", speaking=True, text="hello there"))

        frame = pub.last
        now = frame["now_ms"]
        assert frame["deadline_ms"] > now
        assert frame["deadline_ms"] <= now + sb.max_claim_ms()

    def test_a_longer_utterance_gets_a_longer_deadline(self):
        pub = _Publisher()
        bridge = sb.HUDSpeechBroadcaster(publish=pub)

        bridge(_message("speech_started", speaking=True, text="hi"))
        short = pub.last["deadline_ms"] - pub.last["now_ms"]
        bridge(_message("speech_started", speaking=True, text="x" * 600))
        long = pub.last["deadline_ms"] - pub.last["now_ms"]

        assert long > short

    def test_the_ceiling_is_absolute(self, monkeypatch):
        """No arithmetic may escape the cap — an unbounded mute is deafness."""
        monkeypatch.setenv("JARVIS_HUD_SPEECH_MAX_CLAIM_MS", "5000")
        pub = _Publisher()
        bridge = sb.HUDSpeechBroadcaster(publish=pub)

        bridge(_message("speech_started", speaking=True, text="x" * 100000))

        frame = pub.last
        assert frame["deadline_ms"] - frame["now_ms"] <= 5000 + 1

    def test_a_stop_honours_the_cooldown_rather_than_unmuting_instantly(self):
        """The room is still ringing; the manager already models that."""
        pub = _Publisher()
        bridge = sb.HUDSpeechBroadcaster(publish=pub)
        cooldown = time.time() * 1000 + 1500

        bridge(_message("speech_ended", speaking=False, cooldown_until=cooldown))

        frame = pub.last
        assert frame["speaking"] is False
        assert frame["deadline_ms"] > frame["now_ms"], (
            "unmuted instantly, so the tail of JARVIS's own sentence can be "
            "transcribed as a command")

    def test_an_elapsed_cooldown_unmutes_now(self):
        pub = _Publisher()
        bridge = sb.HUDSpeechBroadcaster(publish=pub)
        stale = time.time() * 1000 - 10_000

        bridge(_message("speech_ended", speaking=False, cooldown_until=stale))

        frame = pub.last
        assert frame["deadline_ms"] <= frame["now_ms"] + 1


class TestFrameBuilding:
    def test_it_ignores_messages_that_are_not_speech_state(self):
        """An unrelated broadcaster message must never mute a microphone."""
        assert sb.build_frame({"type": "something_else", "state": {}}) is None
        assert sb.build_frame({}) is None
        assert sb.build_frame(None) is None          # type: ignore[arg-type]
        assert sb.build_frame({"type": "speech_state_change"}) is None

    def test_a_malformed_state_does_not_raise(self):
        assert sb.build_frame(
            {"type": "speech_state_change", "state": "not-a-dict"}) is None

    def test_the_frame_is_flat_and_absolute(self):
        """The HUD re-derives nothing: two clocks agreeing on a rule is how
        they eventually disagree."""
        frame = sb.build_frame(
            _message("speech_started", speaking=True, text="hi"))
        assert set(frame) >= {"speaking", "deadline_ms", "now_ms", "source"}
        assert isinstance(frame["speaking"], bool)
        assert isinstance(frame["deadline_ms"], float)

    def test_the_estimate_is_an_over_estimate(self):
        """Under-estimating unmutes mid-sentence and feeds the loop."""
        # ~12 chars/sec, plus a floor. 120 chars must exceed 10s of speech.
        assert sb.estimate_speech_ms("x" * 120) >= 10_000
        assert sb.estimate_speech_ms("") >= 1000
        assert sb.estimate_speech_ms("x" * 10**7) <= sb.max_claim_ms()


class TestFailingSafe:
    def test_no_hud_connected_is_not_an_error(self):
        """No HUD means no microphone of ours is listening."""
        pub = _Publisher(reached=0)
        bridge = sb.HUDSpeechBroadcaster(publish=pub)

        bridge(_message("speech_started", speaking=True, text="hi"))

        assert bridge.stats()["unreached"] == 1
        assert bridge.stats()["sent"] == 0

    def test_a_raising_publisher_never_escapes(self):
        def _boom(event, data):
            raise RuntimeError("socket exploded")

        bridge = sb.HUDSpeechBroadcaster(publish=_boom)
        bridge(_message("speech_started", speaking=True, text="hi"))  # no raise

    def test_disabled_publishes_nothing(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HUD_SPEECH_BRIDGE_ENABLED", "0")
        pub = _Publisher()
        sb.HUDSpeechBroadcaster(publish=pub)(
            _message("speech_started", speaking=True, text="hi"))
        assert pub.frames == []

    def test_knobs_are_clamped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HUD_SPEECH_MAX_CLAIM_MS", "not-a-number")
        assert sb.max_claim_ms() == 60000.0
        monkeypatch.setenv("JARVIS_HUD_SPEECH_GRACE_MS", "999999")
        assert sb.speaking_grace_ms() == 5000.0


class TestAgainstTheRealManager:
    """Drives the actual `UnifiedSpeechStateManager`, not a stand-in."""

    async def test_install_subscribes_to_the_authority(self, monkeypatch):
        from backend.core.unified_speech_state import (
            SpeechSource, get_speech_state_manager,
        )

        pub = _Publisher()
        monkeypatch.setattr(sb, "_BROADCASTER", None)
        manager = await get_speech_state_manager()
        before = len(manager._websocket_broadcasters)

        assert await sb.install() is True
        sb.get_broadcaster()._publish = pub

        assert len(manager._websocket_broadcasters) == before + 1

        await manager.start_speaking("Opening Safari.",
                                     source=SpeechSource.SYSTEM)
        assert pub.frames, "the authority spoke and the HUD was not told"
        assert pub.last["speaking"] is True

        await manager.stop_speaking()
        manager._websocket_broadcasters.remove(sb.get_broadcaster())

    async def test_a_stop_is_never_throttled_away(self):
        """A short utterance starts and stops inside the 50ms throttle window.

        The throttle used to drop it outright, leaving a consumer muted with
        nothing left to unmute it. Only REPEATS may be throttled; a transition
        is the entire message.
        """
        from backend.core.unified_speech_state import (
            SpeechSource, get_speech_state_manager,
        )

        seen: List[str] = []
        manager = await get_speech_state_manager()
        manager.register_websocket_broadcaster(
            lambda m: seen.append(m.get("event", "")))
        try:
            await manager.start_speaking("Done.", source=SpeechSource.SYSTEM)
            await manager.stop_speaking()          # well inside 50ms
            assert "speech_started" in seen
            assert "speech_ended" in seen, (
                "the terminal event was throttled away — the mic would stay "
                "muted forever")
        finally:
            manager._websocket_broadcasters.clear()
