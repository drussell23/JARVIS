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


class TestARunningDaemonCanBeSilenced:
    """`export` cannot reach a process that is already running.

    The operator exported the flag and still heard her. That was not a bug
    in the flag — it is what an environment IS: `export` affects processes
    started AFTERWARDS in that shell, and a running process's environment
    was fixed at its launch. The daemon (up for 23 hours, headless,
    started from a different shell) never saw it. Correct behaviour,
    useless outcome.
    """

    def test_a_sentinel_FILE_silences_without_a_restart(self, tmp_path,
                                                        monkeypatch):
        import backend.core.voice_mute as vm
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        target = tmp_path / ".jarvis" / "voice_muted"
        monkeypatch.setattr(vm, "sentinel_paths", lambda: (str(target),))
        assert vm.voice_muted() is False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        # No restart, no re-import, no IPC.
        assert vm.voice_muted() is True

    def test_either_scope_silences(self, tmp_path, monkeypatch):
        """Repo-local for a soak, home for "I typed it once". An operator
        asking twice for quiet should not have to learn which one this
        daemon happens to read."""
        import backend.core.voice_mute as vm
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setattr(vm, "sentinel_paths", lambda: (str(a), str(b)))
        for which in (a, b):
            which.write_text("x")
            assert vm.voice_muted() is True
            which.unlink()
            assert vm.voice_muted() is False

    def test_unmute_clears_EVERY_sentinel(self, tmp_path, monkeypatch):
        """Not the first found: a half-cleared mute that still silences is
        exactly as confusing as a mute that does not."""
        import backend.core.voice_mute as vm
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_text("x"); b.write_text("x")
        monkeypatch.setattr(vm, "sentinel_paths", lambda: (str(a), str(b)))
        assert vm.unmute() == 2
        assert vm.voice_muted() is False

    def test_the_env_var_still_works(self, monkeypatch):
        """Both, not either: env for a fresh launch, sentinel for a live
        one."""
        import backend.core.voice_mute as vm
        monkeypatch.setattr(vm, "sentinel_paths", lambda: ())
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        assert vm.voice_muted() is True

    def test_an_unreadable_sentinel_path_never_silences(self, monkeypatch):
        import backend.core.voice_mute as vm
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        monkeypatch.setattr(vm, "sentinel_paths", lambda: (None, 42))
        assert vm.voice_muted() is False
