"""One answer to "be quiet".

There were thirteen voice flags — `JARVIS_KAREN_*`, `JARVIS_*_NARRATOR_*`,
`JARVIS_APPROVAL_NARRATION_*` — and no single switch. Silencing the
organism meant knowing which subsystem was talking, which is the one thing
an operator who wants silence does not want to think about.
"""
from __future__ import annotations

import pytest

from backend.core.supervisor.unified_voice_orchestrator import (
    _voice_muted,
    safe_say,
)


@pytest.fixture(autouse=True)
def _unmuted(monkeypatch):
    monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
    yield


class TestOneSwitch:
    @pytest.mark.asyncio
    async def test_mute_silences_ordinary_speech(self, monkeypatch):
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert await safe_say("hello", source="test") is False

    @pytest.mark.asyncio
    async def test_it_outranks_the_EMERGENCY_carve_out(self, monkeypatch):
        """`skip_gate` exists for messages urgent enough to speak whether
        or not the room is ready. But an operator who explicitly asked for
        silence IS the room — and a mute that still speaks is not a mute."""
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert await safe_say("urgent", skip_gate=True, source="test") is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_the_obvious_spellings_all_work(self, monkeypatch, val):
        """Someone reaching for silence should not have to guess the
        truthy spelling."""
        monkeypatch.setenv("JARVIS_VOICE_MUTED", val)
        assert _voice_muted() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_off_really_is_off(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_VOICE_MUTED", val)
        assert _voice_muted() is False

    def test_it_is_read_FRESH_every_call(self, monkeypatch):
        """`/voice mute` should take effect on the next sentence, not the
        next restart — an operator reaching for silence wants it now."""
        assert _voice_muted() is False
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert _voice_muted() is True
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "0")
        assert _voice_muted() is False

    def test_it_sits_at_the_ONE_chokepoint(self):
        """Thirteen narrators call `safe_say`; muting there is the only
        place the question has a complete answer."""
        import inspect

        from backend.core.supervisor import unified_voice_orchestrator as u
        src = inspect.getsource(u.safe_say)
        assert "_voice_muted()" in src
        # Before the gate AND before skip_gate's carve-out.
        assert src.index("_voice_muted()") < src.index("if not skip_gate")

    def test_a_broken_env_read_never_silences_by_accident(self, monkeypatch):
        """Fail OPEN: an unreadable flag must not mute the organism, or a
        transient fault becomes permanent silence nobody can explain."""
        import backend.core.supervisor.unified_voice_orchestrator as u
        monkeypatch.setattr(
            u.os.environ, "get",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("env down")))
        assert u._voice_muted() is False
