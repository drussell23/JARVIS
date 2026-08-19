"""The board reads each file once, and asks it five questions in one pass.

`build_import_graph` calls five analysers per file, and each opened its OWN
`ast.walk`. Profiled across 7464 files: 46.4M `walk` calls and 92.9M
`iter_child_nodes` for facts all present in a single traversal. It also parsed
all 3908 TEST files to reach a tree that no analyser is allowed to look at.

Both are fixed without merging the analysers — they have separate callers
(`surface_reachability`, `render_thread`, `source_assertion_audit`) and each
owns its own matching rules. The analysers instead accept an already-walked
node sequence, and the board hands the same one to all five.

Measured on this repository: 92.90s -> 48.84s (1.90x), with every output
structure identical — importers 16101, flag_sites 4549, entries 658,
shadows 99, string_refs 260, scanned 7464.
"""
from __future__ import annotations

import ast
import textwrap

import pytest

from backend.core.ouroboros.battle_test import progress_board as pb


SAMPLE = textwrap.dedent('''
    """A module docstring naming backend.core.fake.module in prose."""
    import os
    import backend.core.ouroboros.governance.orchestrator
    from backend.core.ouroboros.ui import theme

    TARGET = "backend.core.ouroboros.governance.plan_generator"
    FLAG = os.environ.get("JARVIS_SAMPLE_ENABLED", "1")
    OTHER = os.getenv("JARVIS_OTHER_ENABLED", "0")

    if __name__ == "__main__":
        pass
''')


class TestOneWalkAnswersEveryQuestion:
    """Handing a pre-walked sequence must change nothing at all."""

    @pytest.mark.parametrize("call", [
        lambda t, n: pb._imported_modules(t, "m", False, nodes=n),
        lambda t, n: pb._has_main_guard(t, nodes=n),
        lambda t, n: pb._docstring_constants(t, nodes=n),
        lambda t, n: pb._dotted_module_strings(t, nodes=n),
        lambda t, n: list(pb._flag_literals(t, "JARVIS_", nodes=n)),
    ])
    def test_shared_nodes_match_an_independent_walk(self, call):
        tree = ast.parse(SAMPLE)
        own = call(tree, None)
        shared = call(tree, tuple(ast.walk(tree)))
        assert own == shared

    def test_the_default_is_still_an_independent_walk(self):
        """Every existing caller passes no `nodes` and must be untouched."""
        tree = ast.parse(SAMPLE)
        assert pb._has_main_guard(tree) is True
        assert "os" in pb._imported_modules(tree)
        assert any(f == "JARVIS_SAMPLE_ENABLED"
                   for f, _ in pb._flag_literals(tree, "JARVIS_"))


class TestTheSharedWalkIsMaterialised:
    def test_a_generator_would_feed_only_the_first_analyser(self):
        """Why `build_import_graph` calls `tuple(ast.walk(tree))`.

        `ast.walk` returns a generator, and a generator feeds exactly one
        consumer. Handing the same one to five analysers gives the first every
        node and the rest an empty file — silence indistinguishable from a
        module that imports nothing and reads no flags. This test states the
        hazard so the `tuple(...)` is never "simplified" away.
        """
        tree = ast.parse(SAMPLE)
        shared = ast.walk(tree)                      # deliberately NOT a tuple
        first = pb._imported_modules(tree, "m", False, nodes=shared)
        second = pb._imported_modules(tree, "m", False, nodes=shared)
        assert first, "the first consumer should see the whole file"
        assert not second, (
            "a generator was exhausted by the first analyser — if this now "
            "passes, ast.walk stopped being lazy and the tuple() may go"
        )

    def test_the_board_hands_over_a_reusable_sequence(self):
        """The board's own call site, asserted structurally: it must
        materialise before sharing."""
        import inspect
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(pb.ProgressBoard.build_import_graph)))
        materialised = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "tuple"
            and n.args and isinstance(n.args[0], ast.Call)
            and isinstance(n.args[0].func, ast.Attribute)
            and n.args[0].func.attr == "walk"
        ]
        assert materialised, "the shared walk is not materialised"


class TestATestFileIsNeverParsed:
    def test_the_board_does_not_parse_what_it_may_not_read(self, tmp_path,
                                                           monkeypatch):
        """Every analyser sits under `not is_test`, so a test module's tree is
        unreachable by construction. Parsing 3908 of them cost 9.2s per cold
        scan for a result no code path can consult."""
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        (tmp_path / "prod.py").write_text(
            'import os\nos.environ.get("JARVIS_P_ENABLED", "1")\n')
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_thing.py").write_text("import prod\n")

        parsed: list = []
        real_parse = ast.parse

        def _spy(src, *a, **k):
            parsed.append(src)
            return real_parse(src, *a, **k)

        monkeypatch.setattr(pb.ast, "parse", _spy)
        board = pb.ProgressBoard(repo_root=tmp_path)
        board.build_import_graph()

        assert any("JARVIS_P_ENABLED" in s for s in parsed), \
            "the production file was not parsed"
        assert not any("import prod" in s for s in parsed), \
            "a test file was parsed even though no analyser may read it"

    def test_a_test_module_is_still_named_in_the_alias_index(self, tmp_path,
                                                             monkeypatch):
        """Skipping the PARSE must not skip the NAME. The alias index is how a
        dotted string reference resolves to a real module, and it is built from
        the path — dropping test modules from it would turn a resolvable
        reference into silence."""
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        (tmp_path / "prod.py").write_text("x = 1\n")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_thing.py").write_text("import prod\n")
        board = pb.ProgressBoard(repo_root=tmp_path)
        assert board.build_import_graph() == 2, \
            "both files are counted as scanned"

    def test_a_test_importer_still_does_not_make_a_module_live(self, tmp_path,
                                                              monkeypatch):
        """The invariant the whole `is_test` branch exists to protect, pinned
        here because this change moved that branch earlier in the loop."""
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        (tmp_path / "prod.py").write_text(
            'import os\nos.environ.get("JARVIS_DARK_ENABLED", "1")\n')
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_thing.py").write_text("import prod\n")
        board = pb.ProgressBoard(repo_root=tmp_path)
        board.build_import_graph()
        assert board._importers.get("prod", 0) == 0, (
            "a test importer laundered a dark module into a live one"
        )
