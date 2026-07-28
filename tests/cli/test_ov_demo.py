"""`ov demo` — the surface that lets the cockpit be watched without paying.

The value is entirely in calling the REAL renderers. A demo with its own draw
path agrees with itself while the cockpit is broken, which is the defect shape
this codebase keeps hitting. So these tests pin the wiring and the routing;
they deliberately do NOT assert on rendered text, because a test that pins the
demo's output would have to be updated whenever a renderer legitimately
changes, and people update such tests by copying the new output — which
silently blesses regressions.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.cli.ov import resolve
from backend.core.ouroboros.cli.ov_demo import (
    DEMO_HELP, demo_scenes, run_demo,
)


class _Recorder:
    """Console double. Records rather than draws."""

    def __init__(self) -> None:
        self.lines: list = []

    def print(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.lines.append(str(args[0]) if args else "")

    def rule(self, text: str = "") -> None:
        self.lines.append(f"--{text}--")

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class TestRouting:
    def test_ov_resolves_demo_as_a_verb(self):
        inv = resolve(["demo"])
        assert inv.action == "demo"

    def test_scene_is_forwarded_verbatim(self):
        # Mirrors `doctor`, which also forwards its rest untouched — the demo
        # owns its own argument grammar rather than teaching `resolve` about it.
        inv = resolve(["demo", "transcript", "--limit=3"])
        assert inv.action == "demo"
        assert inv.delegate_argv == ["transcript", "--limit=3"]

    def test_bare_ov_is_still_the_cockpit(self):
        # A new verb must not shadow the default path.
        assert resolve([]).action == "cockpit"


class TestSceneDispatch:
    def test_help_is_free_and_boots_nothing(self):
        console = _Recorder()
        assert run_demo(console, ["--help"]) == 0
        assert "ov demo" in console.text

    def test_scenes_are_derived_not_listed(self):
        console = _Recorder()
        assert run_demo(console, ["scenes"]) == 0
        for name in demo_scenes():
            assert name in console.text

    def test_unknown_scene_refuses_with_ex_usage(self):
        # Refuse, never silently ignore — `ov doctor`'s discipline for unknown
        # flags. A demo that quietly runs the wrong scene teaches the operator
        # that the argument does not matter.
        console = _Recorder()
        assert run_demo(console, ["nonsense"]) == 64

    def test_near_miss_gets_a_suggestion(self):
        console = _Recorder()
        run_demo(console, ["tra"])
        assert "transcript" in console.text

    def test_transcript_scene_runs_clean(self):
        console = _Recorder()
        assert run_demo(console, ["transcript"]) == 0
        assert console.lines


class TestRealRenderersAreUsed:
    def test_transcript_calls_the_agora_renderer(self, monkeypatch):
        """The load-bearing property: it must not draw its own version.

        Asserted by BREAKING the real renderer and proving the demo notices.
        Checking that some text appeared would pass just as well against a
        hardcoded string, which is precisely the failure being guarded.
        """
        import backend.core.ouroboros.battle_test.moltbook_inline as mb

        called = {"n": 0}

        def _spy(posts, **kwargs):  # noqa: ANN001, ANN003
            called["n"] += 1
            return ["SENTINEL"]

        monkeypatch.setattr(mb, "render_thread", _spy)
        console = _Recorder()
        run_demo(console, ["transcript"])
        assert called["n"] == 1
        assert "SENTINEL" in console.text

    def test_a_broken_renderer_degrades_instead_of_crashing(self, monkeypatch):
        import backend.core.ouroboros.battle_test.moltbook_inline as mb

        def _boom(*_a, **_k):  # noqa: ANN002, ANN003
            raise RuntimeError("renderer down")

        monkeypatch.setattr(mb, "render_thread", _boom)
        console = _Recorder()
        # A demo must never be the thing that breaks — but it must SAY so,
        # not silently draw a shorter deck that looks fine.
        assert run_demo(console, ["transcript"]) == 0
        assert "agora unavailable" in console.text


class TestBoardScene:
    def test_board_scene_runs_and_reports(self, tmp_path, monkeypatch):
        # Scoped to an empty root so the test does not walk the whole tree.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "nonexistent")
        console = _Recorder()
        assert run_demo(console, ["board"]) == 0
        assert "live" in console.text

    def test_board_degrades_when_the_board_is_unavailable(self, monkeypatch):
        import backend.core.ouroboros.cli.ov_demo as demo

        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def _fail(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if "progress_board" in name:
                raise ImportError("board gone")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fail)
        console = _Recorder()
        assert demo.scene_board(console) == 1
        assert "unavailable" in console.text


class TestNoModelCalls:
    def test_demo_module_never_reaches_a_provider(self):
        """The cost guarantee, enforced structurally rather than promised.

        Every scene is synthetic. A future scene that reaches for a provider
        to make the deck 'more realistic' would reintroduce exactly the cost
        this exists to avoid, and would do it invisibly.
        """
        import ast
        import pathlib

        src = pathlib.Path(
            "backend/core/ouroboros/cli/ov_demo.py",
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        banned = ("providers", "candidate_generator", "doubleword",
                  "anthropic", "openai", "requests", "httpx", "urllib")
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(b in name.lower() for b in banned), (
                    f"ov_demo must not import {name!r} — the whole point is "
                    f"that watching the cockpit costs nothing"
                )
