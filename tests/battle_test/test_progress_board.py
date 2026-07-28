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
    DARK, LIVE, OFF, ProgressBoard, _coerce_bool, _flag_literals,
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
