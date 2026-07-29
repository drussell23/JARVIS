"""A mute honoured by only some speech paths is not a mute.

The first attempt guarded `unified_voice_orchestrator.safe_say` alone, on
the strength of its own docstring calling itself "the canonical safe
speech function for the entire JARVIS process".

It is not. The operator set the flag and still heard her. A docstring
claiming canonicity is a claim, not a fact.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from backend.core.voice_mute import voice_muted

#: Every function through which audio can leave this process. Derived by
#: hunting the phrase the operator actually heard, not by trusting any
#: module's self-description.
SPEECH_ENTRY_POINTS = {
    "backend/core/voice_orchestrator.py": ("speak", "announce"),
    "backend/core/shared_voice_client.py": ("announce",),
    "backend/core/cross_repo_voice_client.py": ("announce",),
    "backend/core/trinity_voice_coordinator.py": ("speak",),
    "backend/core/supervisor/unified_voice_orchestrator.py": ("safe_say",),
}


class TestEveryPathIsGuarded:
    @pytest.mark.parametrize("rel", sorted(SPEECH_ENTRY_POINTS))
    def test_the_mute_is_checked(self, rel):
        """Structural, per file: a new speech path that forgets the check
        is the exact bug this replaces."""
        src = pathlib.Path(rel).read_text()
        names = SPEECH_ENTRY_POINTS[rel]
        found = 0
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in names:
                continue
            body = ast.get_source_segment(src, node) or ""
            assert "voice_muted" in body, f"{rel}:{node.lineno} {node.name}"
            found += 1
        assert found, f"{rel}: no entry point found — did it move?"

    def test_the_answer_lives_in_a_LEAF_module(self):
        """`voice_mute` must import nothing from the codebase, or some
        speech path will hit a cycle and skip the check."""
        src = pathlib.Path("backend/core/voice_mute.py").read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                assert not mod.startswith("backend"), mod


class TestTheSwitch:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_obvious_spellings_work(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_VOICE_MUTED", val)
        assert voice_muted() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_off_is_off(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_VOICE_MUTED", val)
        assert voice_muted() is False

    def test_read_fresh_every_call(self, monkeypatch):
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        assert voice_muted() is False
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert voice_muted() is True

    def test_it_fails_OPEN(self, monkeypatch):
        """A transient env fault must not become permanent silence nobody
        can explain."""
        import backend.core.voice_mute as vm
        monkeypatch.setattr(
            vm.os.environ, "get",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("env down")))
        assert vm.voice_muted() is False


class TestItActuallySilences:
    @pytest.mark.asyncio
    async def test_safe_say_is_silent(self, monkeypatch):
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert await safe_say("hello", source="test") is False

    @pytest.mark.asyncio
    async def test_even_the_emergency_carve_out_is_silent(self, monkeypatch):
        """`skip_gate` speaks whether or not the room is ready. An operator
        who asked for silence IS the room."""
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert await safe_say("urgent", skip_gate=True, source="test") is False
