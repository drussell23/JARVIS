"""One presence answer, one speech gate, for every voice line in the process."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.core import operator_presence as op
from backend.core import voice_mute as vm


def test_any_arm_saying_yes_is_presence():
    manager = op.PresenceManager(arms=[("cockpit", lambda: None), ("desktop_session", lambda: True)])
    reading = manager.read()
    assert reading.present and reading.by == "desktop_session"


def test_no_arm_saying_yes_is_absence():
    manager = op.PresenceManager(arms=[("cockpit", lambda: False), ("desktop_session", lambda: None)])
    assert not manager.read().present


def test_an_arm_that_raises_has_no_say():
    def broken():
        raise OSError("unreadable")

    manager = op.PresenceManager(arms=[("desktop_session", broken), ("terminal", lambda: True)])
    assert manager.read().by == "terminal"


def test_every_question_gets_a_fresh_reading():
    """An RDP client that just disconnected is not still "present"."""
    state = {"active": True}
    manager = op.PresenceManager(arms=[("desktop_session", lambda: state["active"])])
    assert manager.read().present
    state["active"] = False
    assert not manager.read().present


def test_the_desktop_arm_can_be_declared_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENCE_DESKTOP_SESSION", "false")
    assert op._desktop_session() is None


@pytest.mark.skipif(sys.platform != "win32", reason="the WTS session arm exists on Windows")
def test_the_windows_session_arm_reads_the_os():
    assert op._windows_session_active() in (True, False)


def test_cockpit_attach_asks_the_same_manager(monkeypatch):
    from backend.core.ouroboros.battle_test import cockpit_attach

    monkeypatch.setattr(op, "operator_present", lambda: True)
    assert cockpit_attach.operator_present() is True
    monkeypatch.setattr(op, "operator_present", lambda: False)
    assert cockpit_attach.operator_present() is False


# --- the gate -----------------------------------------------------------------

def _present(monkeypatch, present: bool):
    monkeypatch.setattr(op, "presence", lambda: op.PresenceReading(present, "test" if present else "", 0.0))


def test_the_gate_lets_a_line_through_when_someone_is_there(monkeypatch):
    monkeypatch.setattr(vm, "voice_muted", lambda: False)
    _present(monkeypatch, True)
    assert op.speech_gate() == ""


def test_the_gate_silences_an_empty_room_except_urgent_lines(monkeypatch):
    monkeypatch.setattr(vm, "voice_muted", lambda: False)
    _present(monkeypatch, False)
    assert op.speech_gate() == "no operator present"
    assert op.speech_gate(urgent=True) == ""


def test_the_mute_outranks_urgency(monkeypatch):
    monkeypatch.setattr(vm, "voice_muted", lambda: True)
    _present(monkeypatch, True)
    assert op.speech_gate(urgent=True) == "muted"


def test_urgency_is_the_top_priority_of_any_voice_stack_or_a_marked_line():
    from enum import Enum

    class Priority(Enum):
        NORMAL = 1
        CRITICAL = 2

    assert vm.is_urgent(Priority.CRITICAL)
    assert vm.is_urgent("CRITICAL")
    assert not vm.is_urgent(Priority.NORMAL)
    assert vm.is_urgent(Priority.NORMAL, {vm.URGENT_KEY: True})
    assert not vm.is_urgent(None, None)


def test_the_legacy_narrator_queue_asks_the_gate(monkeypatch):
    """The queue the legacy narrators speak through never asked: in a
    background boot they were the only voice heard."""
    from backend.core.supervisor import unified_voice_orchestrator as uvo

    monkeypatch.setattr(vm, "voice_muted", lambda: False)
    _present(monkeypatch, False)
    said = []

    async def execute_say(text):
        said.append(text)

    fake = SimpleNamespace(_metrics=SimpleNamespace(messages_skipped=0), _execute_say=execute_say)
    msg = uvo.VoiceMessage(text="jarvis-prime linked.", priority=uvo.VoicePriority.MEDIUM,
                           source=list(uvo.VoiceSource)[0], created_at=datetime.now())
    asyncio.run(uvo.UnifiedVoiceOrchestrator._speak_message(fake, msg))
    assert said == [] and fake._metrics.messages_skipped == 1
