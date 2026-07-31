"""The import graph could not see a package re-export.

`ProgressBoard` calls a flag DARK when nothing in production imports the
module it lives in. Relative imports were skipped outright — "not resolvable
without a package walk" — and that was a systematic false negative, not a
rounding error.

The chain it broke is the one this codebase uses everywhere:

    consumer:            from ...intake.sensors import BacklogSensor
    sensors/__init__.py: from .backlog_sensor import BacklogSensor

The consumer names a CLASS, and a class name never matches a module name. The
`__init__.py` re-export is the ONLY edge to the module the flag lives in — and
it is relative, so it was skipped. Every module reached that way reported DARK
while being imported on every boot.

546 dark -> 328. 78 dark-and-enabled -> 42.
"""
from __future__ import annotations

import ast

from backend.core.ouroboros.battle_test.progress_board import (
    _imported_modules, _module_name, _resolve_relative,
)


class TestResolveRelative:
    def test_one_dot_is_the_containing_package(self):
        assert _resolve_relative("a.b.c", 1, "x") == "a.b.x"

    def test_two_dots_is_the_parent(self):
        assert _resolve_relative("a.b.c", 2, "x") == "a.x"

    def test_a_bare_from_dot_import(self):
        assert _resolve_relative("a.b.c", 1, "") == "a.b"

    def test_a_package_init_is_already_named_for_its_package(self):
        """THE off-by-one. `_module_name` strips `__init__`, so a package's
        own `__init__.py` is already named for the package: `level=1` means
        ITSELF, not its parent. Stripping a segment walks one level too far
        and resolves to a module that does not exist."""
        pkg = _module_name("backend/core/ouroboros/governance/intake/"
                           "sensors/__init__.py")
        assert pkg.endswith("intake.sensors")
        assert _resolve_relative(pkg, 1, "backlog_sensor", True) == (
            pkg + ".backlog_sensor")

    def test_a_plain_module_is_unaffected_by_the_package_rule(self):
        assert _resolve_relative("a.b.c", 1, "x", False) == "a.b.x"

    def test_over_deep_is_refused_not_wrapped(self):
        assert _resolve_relative("a", 5, "x") == ""

    def test_garbage_never_raises(self):
        for args in (("", 1, "x"), (None, 1, "x"), ("a.b", "z", "x")):
            _resolve_relative(*args)  # type: ignore[arg-type]


class TestImportedModules:
    def test_absolute_imports_still_both_shapes(self):
        tree = ast.parse("import a.b.c\nfrom d.e import f\n")
        got = _imported_modules(tree, "z")
        assert "a.b.c" in got and "d.e" in got and "d.e.f" in got

    def test_a_relative_import_is_resolved_not_skipped(self):
        tree = ast.parse("from .backlog_sensor import BacklogSensor\n")
        got = _imported_modules(tree, "pkg.sensors", is_package=True)
        assert "pkg.sensors.backlog_sensor" in got

    def test_the_re_export_edge_carries_the_symbol_too(self):
        """`from .m import C` reaches module `pkg.m`; recording
        `pkg.m.C` as well costs nothing and matches the absolute path's
        existing behaviour."""
        tree = ast.parse("from .m import C\n")
        got = _imported_modules(tree, "pkg", is_package=True)
        assert "pkg.m" in got and "pkg.m.C" in got

    def test_a_module_inside_a_package_resolves_against_its_parent(self):
        tree = ast.parse("from .sibling import Thing\n")
        got = _imported_modules(tree, "pkg.mod", is_package=False)
        assert "pkg.sibling" in got


class TestAgainstTheRealTree:
    def test_the_sensors_are_no_longer_dark(self):
        """The regression that motivated this. `BacklogSensor` and
        `OpportunityMinerSensor` are constructed at boot by
        `intake_layer_service`, and their flags read DARK — a board that
        reports a booting sensor as dead teaches operators to ignore it."""
        from backend.core.ouroboros.battle_test.progress_board import (
            ProgressBoard,
        )
        rows = ProgressBoard().read().rows
        dark_on = {r.flag for r in rows if r.state == "dark" and r.enabled}
        for flag in ("JARVIS_BACKLOG_FS_EVENTS_ENABLED",
                     "JARVIS_MINER_BACKPRESSURE_ENABLED",
                     "JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED"):
            assert flag not in dark_on, f"{flag} still reads dark"

    def test_the_board_still_finds_genuinely_dark_flags(self):
        """The fix must not launder every flag to LIVE. If nothing is dark
        the signal is gone, which is the same as having no board."""
        from backend.core.ouroboros.battle_test.progress_board import (
            ProgressBoard,
        )
        rows = ProgressBoard().read().rows
        assert any(r.state == "dark" for r in rows)
        assert any(r.state == "live" for r in rows)


class TestScanRootsIncludeScripts:
    """`scripts/` is production.

    `scripts/ouroboros_battle_test.py` is THE entry point that boots the
    six-layer stack — it is how this system actually runs — and 361 backend
    modules are reachable from `scripts/` and nowhere else. Scanning only
    `backend` reported every one of them DARK while they were imported on
    every session.

    Found via `aegis/preflight.py`: flagged dark-and-enabled, imported at
    `scripts/ouroboros_battle_test.py:1997`.
    """

    def test_scripts_is_a_default_root(self):
        from backend.core.ouroboros.battle_test.progress_board import scan_roots

        assert "scripts" in scan_roots()
        assert "backend" in scan_roots()

    def test_the_env_override_still_wins(self, monkeypatch):
        """A knob, not a constant: what counts as production differs between
        this repo and a consumer of it."""
        from backend.core.ouroboros.battle_test.progress_board import scan_roots

        monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", "lib, pkg")
        assert scan_roots() == ("lib", "pkg")

    def test_a_module_imported_only_from_scripts_is_not_dark(self):
        """THE regression. `preflight` is imported by the battle-test entry
        point and by nothing under `backend/`."""
        from backend.core.ouroboros.battle_test.progress_board import (
            ProgressBoard,
        )
        rows = ProgressBoard().read().rows
        dark_on = {r.flag for r in rows if r.state == "dark" and r.enabled}
        assert "JARVIS_AEGIS_DEP_VALIDATION_ENABLED" not in dark_on

    def test_the_board_still_discriminates(self):
        """Widening the roots must not launder everything to LIVE. A board
        with nothing dark has no signal, which is the same as no board."""
        from backend.core.ouroboros.battle_test.progress_board import (
            ProgressBoard,
        )
        rows = ProgressBoard().read().rows
        assert any(r.state == "dark" for r in rows)
        assert any(r.state == "live" for r in rows)
