# tests/voice/duplex/test_protocols.py
from __future__ import annotations

from backend.core.ouroboros.governance.comms.duplex.protocols import (
    ArbiterConfig, Priority, SpeechRequest, VoiceState,
)


def test_priority_ordering_user_barge_in_is_highest():
    assert Priority.USER_BARGE_IN > Priority.USER_RESPONSE
    assert Priority.USER_RESPONSE > Priority.PROACTIVE_CRITICAL
    assert Priority.PROACTIVE_CRITICAL > Priority.PROACTIVE_INFO


def test_speech_request_defaults_and_frozen():
    r = SpeechRequest(text="hi", priority=Priority.PROACTIVE_INFO)
    assert r.coalesce_key == "" and r.op_id == ""
    try:
        r.text = "x"  # type: ignore[misc]
        assert False, "should be frozen"
    except AttributeError:
        pass


def test_voice_state_values():
    assert VoiceState.LISTENING.value == "listening"
    assert {s for s in VoiceState} >= {
        VoiceState.LISTENING, VoiceState.USER_SPEAKING,
        VoiceState.KAREN_SPEAKING, VoiceState.THINKING,
    }


def test_config_from_env_defaults_false(monkeypatch):
    for k in ("JARVIS_KAREN_VOICE_ENABLED", "JARVIS_KAREN_BARGE_IN_ENABLED",
              "JARVIS_KAREN_PROACTIVE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    cfg = ArbiterConfig.from_env()
    assert cfg.enabled is False
    assert cfg.barge_in_enabled is False
    assert cfg.proactive_enabled is False
    assert cfg.queue_max_per_priority == 8
