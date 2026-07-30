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

import ast
import pathlib
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


class TestLiveScene:
    """The scene that finally makes the cockpit watchable.

    Verified end-to-end under a forked pty: boots, enters the alternate screen,
    streams the script, exits on `q`, and emits rmcup so the operator's
    scrollback survives.
    """

    def test_live_needs_a_real_terminal_and_says_so(self):
        # Under pytest stdin is not a tty. Degrading silently would make a
        # working demo indistinguishable from a broken one — the operator sees
        # nothing either way.
        console = _Recorder()
        assert run_demo(console, ["live"]) == 64
        assert "interactive terminal" in console.text

    def test_live_is_NOT_in_the_default_all_scenes_run(self):
        # It takes over the screen and waits for a keypress. A bare `ov demo`
        # must stay a thing you can pipe.
        from backend.core.ouroboros.cli.ov_demo import _SCENES
        assert "live" in _SCENES
        console = _Recorder()
        run_demo(console, [])
        assert "interactive terminal" not in console.text

    @pytest.mark.parametrize("arg,expect", [
        ("--speed=4", 4.0), ("--speed=0.5", 0.5), ("--speed=999", 20.0),
        ("--speed=0", 0.1), ("--speed=junk", 1.0),
    ])
    def test_speed_is_bounded(self, arg, expect):
        from backend.core.ouroboros.cli.ov_demo import live_speed
        assert live_speed([arg]) == expect

    def test_script_timings_are_monotonic(self):
        # The rhythm IS the demonstration. An out-of-order entry would make the
        # driver sleep against a target already past and dump lines at once.
        from backend.core.ouroboros.cli.ov_demo import _LIVE_SCRIPT
        times = [t for t, _ in _LIVE_SCRIPT]
        assert times == sorted(times)
        assert len(_LIVE_SCRIPT) > 10

    def test_exit_bindings_include_an_undisableable_escape(self):
        # `q` is filtered on an empty buffer so typing a word with a q still
        # works. Ctrl-C and Ctrl-D are NOT filtered: an escape hatch that a
        # text field can disable is not an escape hatch.
        from backend.core.ouroboros.cli.ov_demo import _live_exit_bindings
        keys = set()
        for binding in _live_exit_bindings().bindings:
            for key in binding.keys:
                keys.add(str(getattr(key, "value", key)))
        assert "c-c" in keys
        assert "c-d" in keys

    def test_toolbar_uses_the_canonical_pulse(self):
        """The demo must not format a second pulse.

        It used to compose the line itself from `ui.theme.ouroboros_frame` —
        canonical glyph, bespoke sentence. It now hands a payload to
        `attach_heartbeat.format_heartbeat_line`, the formatter every
        attached cockpit renders, which owns that frame. Stronger than the
        old property: there is no second formatter to drift, not merely a
        shared glyph.
        """
        import ast
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/cli/ov_demo.py").read_text(encoding="utf-8")
        assert "format_heartbeat_line" in src
        modules = {n.module or "" for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.ImportFrom)}
        assert any("attach_heartbeat" in m for m in modules)

    def test_toolbar_renders_without_an_app(self):
        from backend.core.ouroboros.cli.ov_demo import _live_toolbar
        render = _live_toolbar(lambda: 0.0, lambda: 7.5)
        text = render()
        assert "q to quit" in text and "tokens" in text


class TestEpistemicsScene:
    """The three surfaces that say how well the organism knows a thing."""

    def test_it_is_a_derived_scene(self):
        from backend.core.ouroboros.cli.ov_demo import demo_scenes
        assert "epistemics" in demo_scenes()

    def test_it_NEVER_writes_the_advisors_live_cache(self):
        """The load-bearing invariant.

        That cache is read by live GATE decisions, so a display surface
        writing to it — even briefly, even with cleanup — could let a
        demonstration change what the organism decides. A demo may lie to
        itself; it may never lie to the gate.
        """
        from rich.console import Console
        from backend.core.ouroboros.cli.ov_demo import scene_epistemics
        from backend.core.ouroboros.governance import operation_advisor as oa
        oa._BLAST_RADIUS_CACHE_SHARED.clear()
        oa._BLAST_PROVENANCE_SHARED.clear()
        scene_epistemics(Console(force_terminal=True, width=80))
        assert oa._BLAST_RADIUS_CACHE_SHARED == {}
        assert oa._BLAST_PROVENANCE_SHARED == {}

    def test_the_source_never_names_the_advisor_cache(self):
        """Structural backstop: the runtime check above also passes for a
        scene that writes and then cleans up perfectly, and the hazard is
        the WINDOW, not the residue."""
        import backend.core.ouroboros.cli.ov_demo as mod
        src = pathlib.Path(mod.__file__).read_text()
        assert "_BLAST_RADIUS_CACHE_SHARED" not in src
        assert "_BLAST_PROVENANCE_SHARED" not in src

    def test_the_resolver_override_is_always_released(self):
        """Left installed, every later gutter in the process would show the
        demo's numbers."""
        from rich.console import Console
        from backend.core.ouroboros.cli.ov_demo import scene_epistemics
        from backend.core.ouroboros.ui import blast_gutter as bg
        scene_epistemics(Console(force_terminal=True, width=80))
        assert bg._RESOLVER is None

    def test_it_renders_the_REAL_diff_tree(self):
        """Not a parallel drawing that agrees with itself while the gate's
        own preview is broken."""
        import backend.core.ouroboros.cli.ov_demo as mod
        src = pathlib.Path(mod.__file__).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "scene_epistemics")
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert "DiffPreviewRenderer()._build_file_tree" in called
        assert "legend" in called and "roster" in called

    def test_a_broken_surface_degrades_instead_of_crashing(self, monkeypatch):
        from rich.console import Console
        from backend.core.ouroboros.cli import ov_demo as mod
        from backend.core.ouroboros.ui import blast_gutter as bg
        monkeypatch.setattr(bg, "set_resolver", lambda *_a: (
            _ for _ in ()).throw(RuntimeError("boom")))
        assert mod.scene_epistemics(Console(force_terminal=True)) == 0


class TestProvenanceBeats:
    def test_the_marks_come_from_the_REAL_annotator(self):
        """A demo that stamped the glyph itself would keep showing a mark
        the cockpit no longer produces."""
        import backend.core.ouroboros.cli.ov_demo as mod
        src = pathlib.Path(mod.__file__).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_compose")
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert {"annotate", "claiming"} <= called

    def test_every_marked_rung_appears_in_the_transcript(self):
        from backend.core.ouroboros.cli.ov_demo import transcript_lines
        rendered = " ".join(transcript_lines())
        for glyph in ("‹model›", "‹synthetic›",
                      "‹unverified›", "‹stated›"):
            assert glyph in rendered, glyph

    def test_observed_beats_stay_CLEAN(self):
        """Scarcity: a demo that marked every line would be showing a
        transcript the cockpit does not produce."""
        from backend.core.ouroboros.cli.ov_demo import _compose
        assert "‹" not in _compose("det", ("Read 847 lines",))


class TestOneStoryTwoProjections:
    """There used to be two hand-authored beat lists telling the same op in
    two presentations, and they drifted: provenance beats added to the
    transcript never reached the live cockpit."""

    def test_the_transcript_is_DERIVED_from_the_live_beats(self):
        import backend.core.ouroboros.cli.ov_demo as mod
        src = pathlib.Path(mod.__file__).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "transcript_lines")
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert "compose_live_script" in called

    def test_there_is_no_second_beat_list(self):
        import backend.core.ouroboros.cli.ov_demo as mod
        src = pathlib.Path(mod.__file__).read_text()
        assigns = [t.id for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.AnnAssign) and isinstance(n.target,
                                                                  ast.Name)
                   for t in (n.target,)
                   if t.id.endswith("BEATS") or t.id == "_SCRIPT"]
        assert assigns == ["_LIVE_BEATS"], assigns

    def test_callable_beats_are_dropped_not_CALLED(self):
        """`_agent_beats` emits closures that mutate the real AgentRoster.
        A static transcript has no panel to mutate; calling them would leave
        a real roster populated by a scene that never showed it."""
        from backend.core.ouroboros.cli.ov_demo import transcript_lines
        assert all(isinstance(x, str) for x in transcript_lines())

    def test_the_agora_renders_exactly_once(self):
        """The scene used to call the thread renderer itself AND inherit it
        from the projection."""
        from backend.core.ouroboros.cli.ov_demo import transcript_lines
        joined = "\n".join(transcript_lines())
        assert joined.count("@cassandra") == 1

    def test_the_reach_gutter_reaches_BOTH_scenes(self):
        from backend.core.ouroboros.cli.ov_demo import (
            compose_live_script, transcript_lines,
        )
        live = " ".join(l for _a, l in compose_live_script()
                        if isinstance(l, str))
        for token in ("≥12", "unmeasured"):
            assert token in live, token
            assert token in " ".join(transcript_lines()), token
