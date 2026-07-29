"""`/voice mute` must silence what it says it silenced.

It flipped ONE announcer's in-process flag and replied "Karen muted." It
did not touch the other six speech paths, did not survive a restart, and
could not reach any other process. An operator typed it, was told it
worked, and kept hearing her — a false confirmation, which is worse than
having no verb at all.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.voice_repl import (
    dispatch_voice_command,
)
from backend.core.voice_mute import unmute, voice_muted


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
    unmute()
    yield
    unmute()


class TestTheVerbDoesWhatItClaims:
    def test_mute_actually_mutes_every_path(self):
        assert voice_muted() is False
        res = dispatch_voice_command("/voice mute")
        assert res.ok is True
        assert voice_muted() is True, "the verb reported success and did nothing"

    def test_unmute_clears_it(self):
        dispatch_voice_command("/voice mute")
        assert voice_muted() is True
        res = dispatch_voice_command("/voice unmute")
        assert res.ok is True
        assert voice_muted() is False

    @pytest.mark.parametrize("spelling", ["/voice mute", "/voice off"])
    def test_both_spellings_engage_the_real_mute(self, spelling):
        unmute()
        dispatch_voice_command(spelling)
        assert voice_muted() is True

    def test_it_says_the_mute_is_DURABLE(self):
        """The sentinel is what makes it survive a restart and reach other
        processes; the reply should say so, because that is the property
        the operator is relying on."""
        res = dispatch_voice_command("/voice mute")
        assert "restart" in res.text.lower()

    def test_unmute_reports_what_it_CLEARED(self):
        dispatch_voice_command("/voice mute")
        res = dispatch_voice_command("/voice unmute")
        assert "sentinel" in res.text.lower()


class TestItRefusesToOVERCLAIM:
    def test_a_failed_sentinel_is_REPORTED_not_swallowed(self, monkeypatch):
        """An operator told "muted" who is still hearing her needs to know
        which half failed. Reporting success on a partial mute is the
        original defect, restated."""
        import backend.core.ouroboros.governance.voice_repl as vr
        monkeypatch.setattr(
            "backend.core.voice_mute.mute", lambda *a, **k: "")
        res = vr.dispatch_voice_command("/voice mute")
        assert res.ok is False, "claimed success with no durable mute"
        assert "could NOT" in res.text or "not survive" in res.text

    def test_a_dead_announcer_does_not_stop_the_real_mute(self, monkeypatch):
        """The durable half is the one that matters; a broken announcer
        must not prevent silence."""
        import backend.core.ouroboros.governance.voice_repl as vr
        monkeypatch.setattr(
            vr, "_announcer",
            lambda: (_ for _ in ()).throw(RuntimeError("no announcer")))
        res = vr.dispatch_voice_command("/voice mute")
        assert voice_muted() is True
        assert res.ok is True
