"""The board must be right about itself before it is trusted about anything.

Its first real validation was self-referential and it passed: asked about
`JARVIS_PROGRESS_BOARD_ENABLED`, it answered `dark` — correct, because at that
moment nothing imported it. A status view that cannot see its own inertness
would not have seen anyone else's.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.progress_board import (
    DARK, DYNAMIC_LIVE, ENTRY, LIVE, OFF, ProgressBoard, _coerce_bool, _flag_literals,
    _is_test_path, _module_name, board_enabled, render_board,
)
import ast


def _board(tmp_path: Path) -> ProgressBoard:
    return ProgressBoard(repo_root=tmp_path)


def _write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")


class TestDarkDetection:
    def test_enabled_but_unimported_is_dark(self, tmp_path, monkeypatch):
        # The state the board exists to name: on, present, imported by nothing.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nX = os.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == DARK
        assert rows["JARVIS_FEATURE_ENABLED"].importers == 0

    def test_a_production_importer_makes_it_live(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nX = os.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        _write(tmp_path, "backend/caller.py",
               "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == LIVE

    def test_test_only_importers_do_NOT_make_it_live(self, tmp_path,
                                                     monkeypatch):
        # The load-bearing rule. A module exercised only by tests is inert in
        # production, and letting a test importer launder it into LIVE would
        # hide exactly the class of bug this board was built for.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nX = os.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        _write(tmp_path, "backend/tests/test_feature.py",
               "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == DARK

    def test_self_import_is_not_a_caller(self, tmp_path, monkeypatch):
        # Without this every module looks live, which is the same as having
        # no signal at all.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nfrom backend import feature\n'
               'X = os.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == DARK

    def test_disabled_flag_is_off_not_dark(self, tmp_path, monkeypatch):
        # OFF is a deliberate choice; DARK is an accident. Merging them would
        # bury the accidents under the choices.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nX = os.environ.get("JARVIS_FEATURE_ENABLED", "0")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == OFF

    def test_env_override_beats_the_source_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        monkeypatch.setenv("JARVIS_FEATURE_ENABLED", "0")
        _write(tmp_path, "backend/feature.py",
               'import os\nX = os.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == OFF


class TestDiscovery:
    @pytest.mark.parametrize("src,expected", [
        ('os.environ.get("JARVIS_A", "1")', ("JARVIS_A", "1")),
        ('os.getenv("JARVIS_B", "0")', ("JARVIS_B", "0")),
        ('os.environ["JARVIS_C"]', ("JARVIS_C", None)),
    ])
    def test_all_three_env_shapes_are_found(self, src, expected):
        # A discoverer that understood only one shape would report features
        # using the others as non-existent — worse than reporting nothing,
        # because it looks like a complete answer.
        found = set(_flag_literals(ast.parse(src), "JARVIS_"))
        assert expected in found

    def test_non_jarvis_env_reads_are_ignored(self):
        found = list(_flag_literals(ast.parse('os.environ.get("PATH")'),
                                    "JARVIS_"))
        assert found == []

    def test_prefix_is_configurable_not_hardcoded(self):
        found = set(_flag_literals(ast.parse('os.environ.get("ACME_X", "1")'),
                                   "ACME_"))
        assert ("ACME_X", "1") in found


class TestCoercion:
    @pytest.mark.parametrize("raw,expect", [
        ("1", True), ("true", True), ("ON", True),
        ("0", False), ("false", False), ("off", False),
        (True, True), (False, False),
    ])
    def test_boolean_shaped_defaults(self, raw, expect):
        assert _coerce_bool(raw) is expect

    @pytest.mark.parametrize("raw", ["5", "notify_apply", "", None, 3.5])
    def test_values_are_not_switches(self, raw):
        # Guessing at these would drop tuning knobs into a column that means
        # "enabled but inert", inflating the one number an operator scans.
        assert _coerce_bool(raw) is None


class TestRobustness:
    def test_read_never_raises_on_a_broken_tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/broken.py", "def (((\n")
        _write(tmp_path, "backend/ok.py",
               'import os\nX = os.environ.get("JARVIS_OK_ENABLED", "1")\n')
        reading = _board(tmp_path).read()
        # The syntax error is skipped, not fatal — a status view must never be
        # the thing that breaks the cockpit it reports on.
        assert any(r.flag == "JARVIS_OK_ENABLED" for r in reading.rows)

    def test_master_switch_off_yields_an_empty_honest_reading(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ENABLED", "0")
        assert board_enabled() is False
        reading = _board(tmp_path).read()
        assert reading.rows == []
        assert reading.degraded == "disabled"

    def test_vendored_dirs_are_excluded(self, tmp_path, monkeypatch):
        # A venv under the scan root took the walk from ~900 files to 20,121
        # and 119s, and every vendored module counted as a production importer.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/venv/lib/site-packages/x.py",
               'import os\nos.environ.get("JARVIS_VENDORED_ENABLED", "1")\n')
        flags = {r.flag for r in _board(tmp_path).read().rows}
        assert "JARVIS_VENDORED_ENABLED" not in flags

    def test_render_is_total(self, tmp_path):
        assert render_board(_board(tmp_path).read())

    @pytest.mark.asyncio
    async def test_async_read_matches_sync(self, tmp_path, monkeypatch):
        # The scan is thousands of ast.parse calls; on the event loop that is a
        # multi-second freeze, and this is meant to be callable from the live
        # cockpit.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        board = _board(tmp_path)
        assert {r.flag for r in (await board.read_async()).rows} == {
            r.flag for r in board.read().rows
        }


class TestHelpers:
    def test_module_name_from_path(self):
        assert _module_name("backend/core/x.py") == "backend.core.x"
        assert _module_name("backend/core/__init__.py") == "backend.core"

    @pytest.mark.parametrize("rel", [
        "tests/x.py", "backend/tests/y.py", "backend/test_z.py",
        "backend/conftest.py",
    ])
    def test_test_paths_are_recognised(self, rel):
        assert _is_test_path(rel)

    def test_production_paths_are_not(self):
        assert not _is_test_path("backend/core/ouroboros/orchestrator.py")


class TestEntryPoints:
    """Reachability by EXECUTION, which an import graph structurally cannot see.

    Found by sampling the board's own output: `commit_authority_cli` was
    reported dark in the same session the operator ran it by hand.
    """

    def test_main_guard_is_entry_not_dark(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/cli.py",
               'import os\n'
               'X = os.environ.get("JARVIS_CLI_ENABLED", "1")\n'
               'if __name__ == "__main__":\n    pass\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_CLI_ENABLED"].state == ENTRY

    def test_entry_wins_on_the_non_boolean_path_too(self, tmp_path,
                                                    monkeypatch):
        # The precedence bug: a non-boolean default returned from an earlier
        # branch that never consulted entry-point status, so every CLI knob
        # with a value default stayed dark. A state check only one of two
        # exits consults is not a state check.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/cli.py",
               'import os\n'
               'X = os.environ.get("JARVIS_CLI_TIMEOUT", "30")\n'
               'if __name__ == "__main__":\n    pass\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_CLI_TIMEOUT"].state == ENTRY

    def test_an_imported_entry_point_is_live_not_entry(self, tmp_path,
                                                       monkeypatch):
        # ENTRY is the fallback for "nothing imports it". A real importer is
        # stronger evidence and must win.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/cli.py",
               'import os\n'
               'X = os.environ.get("JARVIS_CLI_ENABLED", "1")\n'
               'if __name__ == "__main__":\n    pass\n')
        _write(tmp_path, "backend/caller.py", "from backend import cli\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_CLI_ENABLED"].state == LIVE

    def test_plain_module_without_a_guard_stays_dark(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/plain.py",
               'import os\nos.environ.get("JARVIS_PLAIN_ENABLED", "1")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_PLAIN_ENABLED"].state == DARK


class TestSemanticShadowGraph:
    """Static AST cannot evaluate `inspect.getmembers` — but it does not need to.

    A runtime registry only finds what it can RECOGNISE, and what it recognises
    is a convention written into the source. Detect the convention and you have
    the edge the import graph is missing, with no import and no execution.

    The markers here were MEASURED. `repl_dispatch_registry` has no `@verb`
    decorator: it walks for modules named `*_repl` carrying module-level
    `dispatch_<verb>_command` callables. A detector built on a plausible-looking
    decorator would have matched nothing and reported a confident zero.
    """

    def test_registry_convention_beats_zero_importers(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/thing_repl.py",
               'import os\n'
               'X = os.environ.get("JARVIS_THING_ENABLED", "1")\n'
               'def dispatch_thing_command(line):\n    return None\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        row = rows["JARVIS_THING_ENABLED"]
        assert row.state == DYNAMIC_LIVE
        assert "dispatch_thing_command" in row.reason

    def test_module_naming_convention_alone_is_enough(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/lonely_repl.py",
               'import os\nos.environ.get("JARVIS_LONELY_ENABLED", "1")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_LONELY_ENABLED"].state == DYNAMIC_LIVE

    def test_decorator_marker_is_configurable_not_hardcoded(self, tmp_path,
                                                            monkeypatch):
        # A second registry with a different convention must be a config
        # change, not a code change.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_DECORATORS", "verb")
        _write(tmp_path, "backend/plugin.py",
               'import os\n'
               'X = os.environ.get("JARVIS_PLUGIN_ENABLED", "1")\n'
               '@verb\ndef handler():\n    return None\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        row = rows["JARVIS_PLUGIN_ENABLED"]
        assert row.state == DYNAMIC_LIVE
        assert "@verb" in row.reason

    def test_base_class_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_BASES", "BaseSensor")
        _write(tmp_path, "backend/sensor.py",
               'import os\n'
               'X = os.environ.get("JARVIS_SENSOR_ENABLED", "1")\n'
               'class Thing(BaseSensor):\n    pass\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_SENSOR_ENABLED"].state == DYNAMIC_LIVE

    def test_nested_function_does_NOT_count(self, tmp_path, monkeypatch):
        # A registry walks MODULE-level members. A conventionally-named inner
        # function is unreachable by `getmembers`, and treating it as a marker
        # would launder dark modules into live ones — the exact failure the
        # vendored-venv exclusion already had to fix once.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/inner.py",
               'import os\n'
               'X = os.environ.get("JARVIS_INNER_ENABLED", "1")\n'
               'def outer():\n'
               '    def dispatch_inner_command(line):\n        return None\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_INNER_ENABLED"].state == DARK

    def test_a_real_importer_still_wins(self, tmp_path, monkeypatch):
        # DYNAMIC_LIVE is the fallback for "nothing imports it". Direct
        # evidence must beat an inference.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/thing_repl.py",
               'import os\n'
               'X = os.environ.get("JARVIS_THING_ENABLED", "1")\n'
               'def dispatch_thing_command(line):\n    return None\n')
        _write(tmp_path, "backend/caller.py", "from backend import thing_repl\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_THING_ENABLED"].state == LIVE

    @pytest.mark.asyncio
    async def test_async_read_classifies_shadow_modules_identically(
        self, tmp_path, monkeypatch,
    ):
        # The mandated async assertion: a file with NO inbound imports but a
        # registry-recognisable marker is DYNAMIC_LIVE off the event loop too.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/async_repl.py",
               'import os\n'
               'X = os.environ.get("JARVIS_ASYNC_VERB_ENABLED", "1")\n'
               'def dispatch_async_command(line):\n    return None\n')
        board = _board(tmp_path)
        rows = {r.flag: r for r in (await board.read_async()).rows}
        assert rows["JARVIS_ASYNC_VERB_ENABLED"].state == DYNAMIC_LIVE


class TestVerbPrimingIsNotACount:
    """`ov demo board` said `verbs primed 0` while `ov demo transcript` said 62
    IN THE SAME RUN. Only one of them had asked the registry to prime, and
    `list_verbs()` returns an empty tuple both when discovery has not run and
    when it ran and found nothing.

    A count cannot express "nobody asked yet".
    """

    def test_board_reports_unprimed_distinctly_from_empty(self, tmp_path,
                                                          monkeypatch):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as reg,
        )
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "nonexistent")
        reg.reset_registry_for_tests()
        reading = _board(tmp_path).read()
        assert reading.verbs == ()
        assert reading.verbs_primed is False

    def test_reading_the_board_does_NOT_prime(self, tmp_path, monkeypatch):
        # The load-bearing property. A read-only status view must not trigger
        # the import walk priming performs — looking at the board would then
        # change what the process has loaded, and the board would be reporting
        # on a system its own observation had altered.
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as reg,
        )
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "nonexistent")
        reg.reset_registry_for_tests()

        called = {"n": 0}
        real = reg.prime_registry

        def _spy(*a, **k):  # noqa: ANN002, ANN003
            called["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(reg, "prime_registry", _spy)
        _board(tmp_path).read()
        assert called["n"] == 0, "the board primed the registry as a side effect"

    def test_primed_state_is_reported_when_it_is_true(self, tmp_path,
                                                      monkeypatch):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as reg,
        )
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "nonexistent")
        reg.reset_registry_for_tests()
        reg.prime_registry()
        assert _board(tmp_path).read().verbs_primed is True

    def test_to_dict_carries_the_distinction(self, tmp_path, monkeypatch):
        # A JSON consumer must not have to re-derive it from a zero.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "nonexistent")
        assert "verbs_primed" in _board(tmp_path).read().to_dict()


class TestRegistryPrimedAccessor:
    def test_public_accessor_exists_rather_than_a_reach_around(self):
        # Callers needed this and the only alternative was reading the private
        # `_REGISTRY_PRIMED` — the same reach-around the risk-tier ladder has an
        # authority invariant against.
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as reg,
        )
        assert "registry_primed" in reg.__all__

    def test_accessor_is_side_effect_free(self, monkeypatch):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as reg,
        )
        reg.reset_registry_for_tests()
        assert reg.registry_primed() is False
        assert reg.registry_primed() is False   # still false — it did not prime
        reg.prime_registry()
        assert reg.registry_primed() is True


class TestRenderRespectsTheTerminal:
    """`width: int = 78` was a GUESS baked into a signature.

    Rows padded to 44 columns plus an unclipped reason wrapped and broke the
    layout the moment they met a real terminal — invisible in every unit test,
    obvious in one second of `ov demo board`.
    """

    def _reading(self, tmp_path, monkeypatch, n=30):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        for i in range(n):
            _write(tmp_path, f"backend/pkg{i}/mod_with_a_very_long_name.py",
                   "import os\n"
                   f'os.environ.get("JARVIS_A_VERY_LONG_FLAG_NAME_{i}", "1")\n')
        return _board(tmp_path).read()

    @pytest.mark.parametrize("cols", [40, 60, 80, 120, 200])
    def test_no_line_ever_exceeds_the_width(self, tmp_path, monkeypatch, cols):
        reading = self._reading(tmp_path, monkeypatch)
        for line in render_board(reading, width=cols):
            assert len(line) <= cols, f"{cols}c overflow: {line!r}"

    def test_width_is_asked_at_render_time_not_hardcoded(self):
        from backend.core.ouroboros.battle_test.progress_board import (
            terminal_width,
        )
        assert terminal_width() >= 40

    def test_a_truncated_name_ends_in_an_ellipsis(self, tmp_path, monkeypatch):
        # A blind `line[:cols]` cut flag names mid-word, which reads as a
        # DIFFERENT flag — worse than an ellipsis, because it is plausible.
        reading = self._reading(tmp_path, monkeypatch)
        body = [l for l in render_board(reading, width=48) if "◌" in l]
        assert body
        assert any("…" in l for l in body)


class TestSwitchesAndKnobsAreDifferentFindings:
    def test_boolean_default_is_a_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/f.py",
               'import os\nos.environ.get("JARVIS_F_ENABLED", "1")\n')
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_F_ENABLED"]
        assert row.kind == "switch"

    def test_value_default_is_a_knob(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/f.py",
               'import os\nos.environ.get("JARVIS_F_TIMEOUT", "30")\n')
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_F_TIMEOUT"]
        assert row.kind == "knob"

    def test_switches_sort_before_knobs(self, tmp_path, monkeypatch):
        # Alphabetical-by-flag put twelve `JARVIS_A*` thresholds first and
        # nothing else was ever seen. The sort was hiding the signal it existed
        # to surface.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/aaa_knob.py",
               'import os\nos.environ.get("JARVIS_AAA_TIMEOUT", "30")\n')
        _write(tmp_path, "backend/zzz_switch.py",
               'import os\nos.environ.get("JARVIS_ZZZ_ENABLED", "1")\n')
        actionable = _board(tmp_path).read().actionable
        kinds = [r.kind for r in actionable]
        assert kinds.index("switch") < kinds.index("knob")

    def test_one_module_of_knobs_is_ONE_finding(self, tmp_path, monkeypatch):
        # Twelve dials on one unimported module is one thing to fix. Listing it
        # twelve times is how a real finding gets buried under its own repeats.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        body = "import os\n" + "".join(
            f'os.environ.get("JARVIS_TUNE_{i}_MS", "{i + 2}")\n' for i in range(12))
        _write(tmp_path, "backend/dials.py", body)
        reading = _board(tmp_path).read()
        assert len(reading.actionable) == 12
        assert len(reading.actionable_modules) == 1

    def test_a_module_with_a_switch_outranks_a_module_of_knobs(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/aaa_dials.py", "import os\n" + "".join(
            f'os.environ.get("JARVIS_DIAL_{i}_MS", "{i + 2}")\n' for i in range(9)))
        _write(tmp_path, "backend/zzz_feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        first = _board(tmp_path).read().actionable_modules[0]
        assert "zzz_feature" in first[0]
