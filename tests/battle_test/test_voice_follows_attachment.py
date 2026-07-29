"""Karen speaks to a room, and stops when the room empties.

`ov` detaches: closing the terminal leaves the organism running, which is
intended and good. But speech kept coming out of the machine's speakers after
the operator had gone home — audio addressed to nobody, with no way to stop
it short of killing the daemon.

Text already behaved correctly: `publish_markup` returns early when no
cockpit is attached. Speech had no equivalent.

The first attempt at this was to flip the narrator's default to OFF. That
treats the symptom and costs Karen the times she IS useful; the operator
diagnosed the real cause — the voice outliving the terminal — and this is
that fix.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test import cockpit_attach as ca


class _Bridge:
    def __init__(self, n: int) -> None:
        self._clients = {f"c{i}": object() for i in range(n)}


@pytest.fixture(autouse=True)
def _detached(monkeypatch):
    """No bridge and no terminal — the state a daemonised `ov` is in."""
    ca.set_active_bridge(None)
    monkeypatch.setattr(
        "backend.core.ouroboros.battle_test.presentation_restraint"
        ".real_stdout_isatty", lambda: False,
    )
    yield
    ca.set_active_bridge(None)


class TestPresence:
    def test_a_detached_daemon_has_no_operator(self):
        assert ca.attached_cockpits() == 0
        assert ca.operator_present() is False

    def test_an_attached_cockpit_is_an_operator(self):
        ca.set_active_bridge(_Bridge(1))
        assert ca.operator_present() is True

    def test_several_cockpits_are_counted(self):
        ca.set_active_bridge(_Bridge(3))
        assert ca.attached_cockpits() == 3

    def test_detaching_empties_the_room(self):
        ca.set_active_bridge(_Bridge(1))
        assert ca.operator_present()
        ca.set_active_bridge(None)
        assert ca.operator_present() is False

    def test_a_FOREGROUND_run_counts_even_with_no_bridge(self, monkeypatch):
        """The operator is looking straight at the daemon's own terminal.
        Requiring an attached cockpit would silence Karen for someone in the
        room with her."""
        monkeypatch.setattr(
            "backend.core.ouroboros.battle_test.presentation_restraint"
            ".real_stdout_isatty", lambda: True,
        )
        assert ca.operator_present() is True

    def test_an_unreadable_bridge_falls_back_to_the_TERMINAL(self, monkeypatch):
        """The honest degradation: if the bridge cannot be consulted, a
        detached daemon has no terminal and falls silent while a foreground
        run keeps its voice — the right answer in both cases, reached
        without guessing."""
        class _Broken:
            @property
            def _clients(self):
                raise RuntimeError("bridge down")
        ca.set_active_bridge(_Broken())
        assert ca.operator_present() is False
        monkeypatch.setattr(
            "backend.core.ouroboros.battle_test.presentation_restraint"
            ".real_stdout_isatty", lambda: True,
        )
        assert ca.operator_present() is True


class TestTheSpeechPrimitiveHonoursIt:
    def _say(self, **kw):
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        return asyncio.get_event_loop().run_until_complete(
            safe_say("the vision floor raises", source="test", **kw))

    def test_an_empty_room_gets_no_speech(self):
        assert self._say() is False

    def test_urgent_speech_still_gets_through(self):
        """`skip_gate` already existed for exactly this class of message —
        something urgent enough to say whether or not the room is ready. An
        emergency must not be silenced by an absent audience."""
        import inspect
        from backend.core.supervisor import unified_voice_orchestrator as uvo
        src = inspect.getsource(uvo.safe_say)
        gate = src[src.index("IS ANYBODY THERE"):src.index("1. Dedup")]
        assert "if not skip_gate:" in gate

    def test_the_gate_is_at_the_ONE_primitive(self):
        """Thirteen modules call `safe_say`. Gating each would be thirteen
        chances to miss one, and the fourteenth would ship ungated."""
        import inspect
        from backend.core.supervisor import unified_voice_orchestrator as uvo
        assert "operator_present" in inspect.getsource(uvo.safe_say)

    def test_presence_is_a_courtesy_not_a_hard_gate(self):
        """If the check itself fails, speech proceeds. A broken import must
        not mute the assistant."""
        import inspect
        from backend.core.supervisor import unified_voice_orchestrator as uvo
        src = inspect.getsource(uvo.safe_say)
        gate = src[src.index("IS ANYBODY THERE"):src.index("1. Dedup")]
        assert "except Exception" in gate and "pass" in gate


class TestTheHarnessPublishesTheBridge:
    def test_it_registers_on_mount_and_clears_on_teardown(self):
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text(encoding="utf-8")
        assert "set_active_bridge(bridge)" in src
        assert "set_active_bridge(None)" in src


class TestNeverRaises:
    @pytest.mark.parametrize("junk", [None, object(), 42, "bridge"])
    def test_junk_bridges_degrade(self, junk):
        ca.set_active_bridge(junk)
        assert isinstance(ca.attached_cockpits(), int)
        assert isinstance(ca.operator_present(), bool)
