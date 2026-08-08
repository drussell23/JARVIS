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
        "tests/x.py", "backend/tests/y.py", "backend/conftest.py",
        "conftest.py",          # the ROOT conftest, which "/conftest.py" missed
        "test_actual_voice.py",  # root-level: pytest.ini names `test_*.py`
    ])
    def test_test_paths_are_recognised(self, rel):
        assert _is_test_path(rel)

    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/orchestrator.py",
        # These four match a test NAME and are production. `_is_test_path`
        # used to call all of them tests, which made every module reachable
        # only through them read DARK — including the two flags the
        # 2026-08-08 reachability audit wrongly listed as unwired.
        "scripts/ouroboros_battle_test.py",
        "backend/core/ouroboros/governance/test_runner.py",
        "backend/core/ouroboros/governance/intent/test_watcher.py",
        "backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py",
    ])
    def test_production_paths_are_not(self, rel):
        assert not _is_test_path(rel)

    def test_the_rule_comes_from_the_project_not_from_a_convention(self):
        """`backend/test_z.py` USED to assert True here.

        It is not collected by this repository's pytest — `testpaths` is
        `tests test_*.py`, so a `test_*.py` nested under `backend/` is never
        run — and the convention it encoded is the one that misclassified the
        four production modules above. The rule now reads the project's own
        configuration, so it moves when `pytest.ini` moves.
        """
        from backend.core.ouroboros.battle_test.progress_board import (
            test_collection_config,
        )
        testpaths, python_files = test_collection_config()
        assert "tests" in testpaths
        assert "test_*.py" in python_files
        assert not _is_test_path("backend/test_z.py")


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


class TestOtherCheckoutsAreNotThisCodebase:
    """A `.git` inside a directory means git considers it a checkout of its own.

    `.worktrees` was the instance — 7,382 files, 26% of the walk — but the fix
    is the PROPERTY, not the name, because naming it would have left submodules
    and vendored clones counted as production importers of the real modules.
    """

    def test_a_worktree_copy_does_not_launder_a_dark_module(
        self, tmp_path, monkeypatch,
    ):
        # THE defect. A stale copy importing a module main has orphaned would
        # report it LIVE — the exact laundering `_EXCLUDE_DIRS` was written for.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        # A LINKED WORKTREE: `.git` is a FILE holding a gitdir pointer, which
        # is why a directory-only check would have missed every one of them.
        _write(tmp_path, ".worktrees/old/.git", "gitdir: /elsewhere/.git\n")
        _write(tmp_path, ".worktrees/old/backend/caller.py",
               "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == DARK
        assert rows["JARVIS_FEATURE_ENABLED"].importers == 0

    def test_a_submodule_is_pruned_by_the_same_rule(self, tmp_path, monkeypatch):
        # Not a worktree, not named `.worktrees`, caught anyway — which is the
        # whole reason the rule is structural.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        (tmp_path / "vendor" / "dep" / ".git").mkdir(parents=True)  # dir form
        _write(tmp_path, "vendor/dep/caller.py", "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == DARK

    def test_a_dangling_git_symlink_still_counts_as_a_checkout(
        self, tmp_path, monkeypatch,
    ):
        # This repo's own `.git` has been a symlink under the iCloud `.nosync`
        # layout. `exists()` returns False on a broken one, so the symlink
        # check is what keeps a moved checkout from silently rejoining the scan.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        (tmp_path / "linked").mkdir()
        (tmp_path / "linked" / ".git").symlink_to(tmp_path / "nowhere")
        _write(tmp_path, "linked/caller.py", "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == DARK

    def test_the_repo_root_is_never_pruned_by_its_own_git(
        self, tmp_path, monkeypatch,
    ):
        # The root carries `.git` too. Pruning on it would scan nothing at all
        # and report an empty board as a clean one.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        (tmp_path / ".git").mkdir()
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        _write(tmp_path, "backend/caller.py", "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == LIVE

    def test_pruning_is_a_knob_not_a_law(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_PRUNE_NESTED", "0")
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        _write(tmp_path, ".worktrees/old/.git", "gitdir: /elsewhere/.git\n")
        _write(tmp_path, ".worktrees/old/backend/caller.py",
               "from backend import feature\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_FEATURE_ENABLED"].state == LIVE


class TestOverlappingRootsAreWalkedOnce:
    """`scan_roots()` returns `(".", "backend", "scripts")` and "." contains both.

    The old comment claimed the overlap was free because module names were
    deduplicated. `flags` was; `counts` was not — so every import in an
    overlapping file was counted twice and `importers` was close to double.
    """

    def test_one_importer_is_counted_once_across_overlapping_roots(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".,backend")
        _write(tmp_path, "backend/feature.py",
               'import os\nos.environ.get("JARVIS_FEATURE_ENABLED", "1")\n')
        _write(tmp_path, "backend/caller.py", "from backend import feature\n")
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_FEATURE_ENABLED"]
        assert row.state == LIVE
        assert row.importers == 1, (
            "one caller, walked through two overlapping roots, is still one caller"
        )

    def test_scanned_count_is_files_not_visits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".,backend")
        _write(tmp_path, "backend/a.py",
               'import os\nos.environ.get("JARVIS_A_ENABLED", "1")\n')
        _write(tmp_path, "backend/b.py", "x = 1\n")
        assert _board(tmp_path).read().scanned_files == 2


class TestRegistryMountingConventions:
    """A registry mounts what it RECOGNISES, and recognition is a convention.

    `observability_route_registry` walks its provider packages for a
    module-level callable named exactly `register_routes` and mounts it on the
    HTTP app at boot. Eleven modules in this repo define one and are imported
    by nothing, so they read DARK while serving routes on every session — the
    sixth time the board was right about its own graph and the graph was
    missing an edge.
    """

    def test_register_routes_is_recognised_as_a_mount(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/bus_observability.py",
               'import os\n'
               'os.environ.get("JARVIS_BUS_OBS_ENABLED", "1")\n'
               'def register_routes(app, **kw):\n    return None\n')
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_BUS_OBS_ENABLED"]
        assert row.state == DYNAMIC_LIVE
        assert "register_routes" in row.reason

    def test_a_register_routes_METHOD_is_not_a_module_level_mount(
        self, tmp_path, monkeypatch,
    ):
        # The registry's own exclusion comment says why: a class-based router
        # exposes `register_routes` as a METHOD, and the module-level lookup
        # hits the class object instead. `_semantic_marker` walks module level
        # only, so the two agree without either knowing about the other.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/classy.py",
               'import os\n'
               'os.environ.get("JARVIS_CLASSY_ENABLED", "1")\n'
               'class Router:\n'
               '    def register_routes(self, app):\n        return None\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_CLASSY_ENABLED"].state == DARK


class TestStringReferencesAnnotateButNeverPromote:
    """Being NAMED by a registry is not being MOUNTED by one.

    This mechanism first shipped promoting a referenced module to
    DYNAMIC_LIVE, and it was wrong in the most instructive way available: the
    only two production sites in this repo that spell module names as dotted
    strings are `repl_dispatch_registry`'s provider PACKAGES and
    `observability_route_registry._SUBSTRATE_EXCLUSIONS` — and the second is a
    list of modules the registry refuses to mount. Eight flags were promoted
    on the strength of an exclusion list before anyone read it.

    Telling a mount list from an exclusion list statically is dataflow
    analysis, which is guessing with extra steps. So the detection stays and
    the conclusion goes: the reference annotates a dark row with the file to
    open, and decides nothing.
    """

    def test_a_reference_annotates_the_dark_row(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py",
               'ROUTERS = ("backend.router",)\n')
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_ROUTER_ENABLED"]
        assert row.state == DARK, "a name in a list is not a mount"
        assert "registry.py" in row.reason, (
            "the annotation must name the file to open"
        )

    def test_a_non_boolean_knob_gets_the_annotation_too(self, tmp_path,
                                                        monkeypatch):
        # Both dark exits, for the reason `_row_for`'s own comment already
        # gives about ENTRY: a check only one of two exits consults is not a
        # check. `ide_policy_router` carries three non-boolean knobs and one
        # switch, and the knobs took the other branch.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_CORS_ORIGINS", "*")\n')
        _write(tmp_path, "backend/registry.py", 'R = ("backend.router",)\n')
        row = {r.flag: r for r in
               _board(tmp_path).read().rows}["JARVIS_ROUTER_CORS_ORIGINS"]
        assert row.state == DARK
        assert "registry.py" in row.reason

    def test_an_exclusion_list_does_not_promote(self, tmp_path, monkeypatch):
        # THE regression. Verbatim the shape of
        # `observability_route_registry._SUBSTRATE_EXCLUSIONS`: a tuple of
        # dotted names listing exactly the modules that must NOT be mounted.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py",
               "_SUBSTRATE_EXCLUSIONS = (\n"
               '    "backend.router",   # never mount this one\n'
               ")\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_a_docstring_mention_is_NOT_an_edge(self, tmp_path, monkeypatch):
        # The single most-repeated defect in this repo's audit tooling. A
        # docstring is an `ast.Constant` like any other string, so a module
        # DOCUMENTING its collaborator would vouch for it as loudly as a
        # registry that actually mounts it.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/prose.py",
               '"""The write surface lives in backend.router, see also."""\n'
               "x = 1\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_a_filename_in_prose_is_NOT_an_edge(self, tmp_path, monkeypatch):
        # `"router.py"` passes an identifier-shaped dotted test — both segments
        # are valid identifiers — and this codebase writes exactly that shape
        # in the loader docstrings that first misled the audit.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/mentions.py",
               'NOTE = "backend.router.py"\nOTHER = "router.py"\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_a_string_that_resolves_to_nothing_is_not_an_edge(
        self, tmp_path, monkeypatch,
    ):
        # The real gate. Generous shape-matching is only safe because a string
        # becomes an edge solely by resolving to a module the walk found.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py",
               'ROUTERS = ("backend.does_not_exist", "logging.handlers")\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_a_module_naming_itself_proves_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\n'
               'os.environ.get("JARVIS_ROUTER_ENABLED", "1")\n'
               'SELF = "backend.router"\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_a_package_string_does_NOT_vouch_for_its_members(
        self, tmp_path, monkeypatch,
    ):
        # THE laundering risk. `repl_dispatch_registry` lists PACKAGES and
        # discovers `*_repl` members by naming convention. If a package string
        # vouched for everything underneath, `meta_governor` — same package,
        # no caller anywhere — would be laundered into DYNAMIC_LIVE.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/pkg/__init__.py", "")
        _write(tmp_path, "backend/pkg/orphan.py",
               'import os\nos.environ.get("JARVIS_ORPHAN_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py", 'PACKAGES = ("backend.pkg",)\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ORPHAN_ENABLED"].state == DARK

    def test_a_package_string_composes_with_the_naming_convention(
        self, tmp_path, monkeypatch,
    ):
        # ...and the member that DOES carry the convention is still found, by
        # the mechanism that already existed. The two compose precisely because
        # the string edge refuses to generalise.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/pkg/__init__.py", "")
        _write(tmp_path, "backend/pkg/thing_repl.py",
               'import os\n'
               'os.environ.get("JARVIS_THING_ENABLED", "1")\n'
               'def dispatch_thing_command(line):\n    return None\n')
        _write(tmp_path, "backend/registry.py", 'PACKAGES = ("backend.pkg",)\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_THING_ENABLED"].state == DYNAMIC_LIVE

    def test_a_test_file_reference_does_NOT_make_it_live(
        self, tmp_path, monkeypatch,
    ):
        # Same rule imports already obey: a module exercised only by tests is
        # inert in production, whichever mechanism reaches it.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/tests/test_router.py",
               'TARGET = "backend.router"\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_a_real_importer_still_beats_a_string_reference(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py", 'R = ("backend.router",)\n')
        _write(tmp_path, "backend/caller.py", "from backend import router\n")
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == LIVE

    def test_the_relative_spelling_resolves_too(self, tmp_path, monkeypatch):
        # `backend/` is on sys.path at runtime, so 597 files in this repo spell
        # their siblings `core.x` rather than `backend.core.x`, and a registry
        # may legitimately do the same. `_row_for` already counts importers
        # under both spellings; a string edge that understood only one would
        # report half of them dark.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".,backend")
        _write(tmp_path, "backend/core/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py", 'R = ("core.router",)\n')
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_ROUTER_ENABLED"]
        assert "registry.py" in row.reason

    def test_a_bare_single_segment_name_is_NOT_an_edge(self, tmp_path,
                                                       monkeypatch):
        # The boundary of the relative spelling, and it has to be drawn here.
        # A top-level module's relative name is one bare word — `"router"` —
        # which is indistinguishable from a mode name, a dict key, or a log
        # tag. Accepting it would let any string in the tree vouch for a
        # module that happens to share its name. Requiring a dot is what keeps
        # the generous shape-match safe, so the ambiguous case stays DARK and
        # honest rather than laundered.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".,backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py",
               'R = ("router",)\nMODE = "router"\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_an_fstring_is_left_unresolved_rather_than_guessed(
        self, tmp_path, monkeypatch,
    ):
        # A dynamically-built name is not knowable statically. Inventing one
        # would make this instrument the thing it was built to catch.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py",
               'pkg = "backend"\nR = (f"{pkg}.router",)\n')
        rows = {r.flag: r for r in _board(tmp_path).read().rows}
        assert rows["JARVIS_ROUTER_ENABLED"].state == DARK

    def test_annotation_is_a_knob_not_a_law(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_STRING_REFS", "0")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py", 'R = ("backend.router",)\n')
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_ROUTER_ENABLED"]
        assert row.state == DARK
        assert "registry.py" not in row.reason

    @pytest.mark.parametrize("src,why", [
        ('"""Docs mention backend.router in prose."""\nx = 1\n',
         "a docstring is an ast.Constant like any other string"),
        ('NOTE = "backend.router.py"\n',
         "a .py suffix is a filename in prose, not a module path"),
        ('pkg = "backend"\nR = (f"{pkg}.router",)\n',
         "an f-string value is not knowable statically"),
        ('R = ("backend.does_not_exist",)\n',
         "a string that resolves to no module is not evidence"),
    ])
    def test_non_references_leave_the_reason_clean(self, tmp_path, monkeypatch,
                                                   src, why):
        # A false annotation is cheaper than a false promotion and still costs
        # the operator a file to open for nothing, so the filters are pinned
        # on the reason text too, not just on the state.
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/other.py", src)
        row = {r.flag: r for r in _board(tmp_path).read().rows}["JARVIS_ROUTER_ENABLED"]
        assert row.state == DARK
        assert "other.py" not in row.reason, why

    @pytest.mark.asyncio
    async def test_async_read_annotates_identically(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "backend")
        _write(tmp_path, "backend/router.py",
               'import os\nos.environ.get("JARVIS_ROUTER_ENABLED", "1")\n')
        _write(tmp_path, "backend/registry.py", 'R = ("backend.router",)\n')
        board = _board(tmp_path)
        rows = {r.flag: r for r in (await board.read_async()).rows}
        row = rows["JARVIS_ROUTER_ENABLED"]
        assert row.state == DARK and "registry.py" in row.reason
