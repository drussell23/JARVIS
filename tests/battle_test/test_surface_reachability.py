"""The per-surface audit — and the cycle that made its first version useless.

Five modules in a row shipped complete and invisible on the surface the
operator uses. The progress board called every one of them LIVE, correctly:
each WAS imported. What nothing could see was *which surface* imported it.

The load-bearing test here is the barrier walk. A plain transitive closure
answers "every surface reaches everything" — true, because the surfaces
import each other, and useless, because the asymmetry it exists to find
collapses to zero. An instrument that reports a clean bill of health for the
exact defect that motivated it is worse than no instrument.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import surface_reachability as sr


def _fake(tmp_path, files: dict):
    """Write a tiny package and index it the way the audit does."""
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


class TestTheBarrierWalk:
    def test_a_cycle_does_NOT_make_everything_reachable(self, tmp_path,
                                                        monkeypatch):
        """THE regression, found by running the first version.

        `serpent_flow` imports `bipartite_layout`; `ov` imports both. Closure
        from any one swallows the other two and everything they touch.
        """
        _fake(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/daemon.py": "from pkg import cockpit\nfrom pkg import only_daemon\n",
            "pkg/cockpit.py": "from pkg import daemon\nfrom pkg import only_cockpit\n",
            "pkg/only_daemon.py": "",
            "pkg/only_cockpit.py": "",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(
            roots=("pkg",),
            entries=(("daemon", "pkg.daemon"), ("cockpit", "pkg.cockpit")),
        )
        by = {m.short: m.reached_by for m in reading.modules}
        assert by["only_daemon"] == frozenset({"daemon"}), (
            "the cycle leaked: closure crossed into the other surface"
        )
        assert by["only_cockpit"] == frozenset({"cockpit"})

    def test_a_barrier_is_REACHED_but_not_traversed(self, tmp_path,
                                                    monkeypatch):
        """Knowing the cockpit imports `ov` is true. Inheriting everything
        `ov` imports is what destroyed the signal."""
        _fake(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import deep\n",
            "pkg/deep.py": "",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(
            roots=("pkg",), entries=(("a", "pkg.a"), ("b", "pkg.b")))
        by = {m.short: m.reached_by for m in reading.modules}
        assert by["deep"] == frozenset({"b"}), (
            "`a` reached `deep` THROUGH the `b` surface — the cut failed"
        )

    def test_a_self_import_cycle_terminates(self, tmp_path, monkeypatch):
        """The graph is cyclic even after the cuts; a recursive walk would
        blow the stack on the first loop."""
        _fake(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import a\nfrom pkg import c\n",
            "pkg/c.py": "from pkg import b\n",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(roots=("pkg",), entries=(("a", "pkg.a"),))
        # `pkg` is the package `__init__`, correctly indexed as a module.
        assert {m.short for m in reading.modules} == {"b", "c", "pkg"}


class TestItSeesLazyImports:
    def test_a_function_level_import_is_a_real_edge(self, tmp_path,
                                                    monkeypatch):
        """This codebase imports inside functions constantly. Counting only
        module-level imports would report most of the cockpit unreachable."""
        _fake(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/a.py": "def go():\n    from pkg import lazy\n    return lazy\n",
            "pkg/lazy.py": "",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(roots=("pkg",), entries=(("a", "pkg.a"),))
        assert reading.modules[0].reached_by == frozenset({"a"})


class TestHonestClassification:
    def test_a_boot_path_is_not_an_orphan(self, tmp_path, monkeypatch):
        """`harness` reaches every surface and nothing reaches it. Calling
        that an orphan buries the real ones under the program's entry
        points."""
        _fake(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/surface.py": "",
            "pkg/harness.py": "from pkg import surface\n",
            "pkg/truly_dead.py": "",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(
            roots=("pkg",), entries=(("s", "pkg.surface"),))
        orphans = {m.short for m in reading.orphans()}
        assert "truly_dead" in orphans
        assert "harness" not in orphans

    def test_an_entry_outside_the_roots_is_said_OUT_LOUD(self, tmp_path,
                                                         monkeypatch):
        """Silently contributing an empty set would mark every module
        unreachable from that surface and read as a catastrophic finding."""
        _fake(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": ""})
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(
            roots=("pkg",), entries=(("ghost", "pkg.does_not_exist"),))
        assert reading.unresolved_entries
        assert any("entry outside" in ln for ln in sr.render(reading))

    def test_the_report_says_asymmetry_is_a_SIGNAL(self, tmp_path,
                                                   monkeypatch):
        """`transcript_hatches` is attach-only and LIVE on the cockpit, via a
        KeyBindings object handed across. An import graph cannot see that, so
        the output must not read as a defect count."""
        _fake(tmp_path, {
            "pkg/__init__.py": "", "pkg/a.py": "from pkg import x\n",
            "pkg/b.py": "", "pkg/x.py": "",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        text = "\n".join(sr.render(sr.audit(
            roots=("pkg",), entries=(("a", "pkg.a"), ("b", "pkg.b")))))
        assert "SIGNAL" in text and "not a verdict" in text


class TestItIsSafeToRun:
    def test_it_never_imports_what_it_measures(self):
        """The modules worth auditing are exactly the ones that might explode
        on import. Pure AST over source text, no execution."""
        import ast
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/surface_reachability.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", "") or getattr(
                    node.func, "attr", "")
                assert name not in ("import_module", "__import__", "exec",
                                    "eval"), f"dynamic import/exec: {name}"

    def test_an_unparseable_file_has_no_edges_and_does_not_raise(
        self, tmp_path, monkeypatch,
    ):
        _fake(tmp_path, {
            "pkg/__init__.py": "", "pkg/a.py": "from pkg import broken\n",
            "pkg/broken.py": "def (((( this is not python\n",
        })
        monkeypatch.setattr(sr, "_repo_root", lambda: tmp_path)
        reading = sr.audit(roots=("pkg",), entries=(("a", "pkg.a"),))
        assert {m.short for m in reading.modules} == {"broken", "pkg"}

    @pytest.mark.parametrize("call", [
        lambda: sr.audit(roots=("nonexistent",)),
        lambda: sr.render(sr.SurfaceReading()),
        lambda: sr.audit(entries=()),
    ])
    def test_junk_degrades(self, call):
        call()

    def test_the_master_flag_silences_it(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SURFACE_AUDIT_ENABLED", "0")
        assert sr.audit().modules == []

    def test_surfaces_are_configuration_not_code(self, monkeypatch):
        """A fourth surface must not require an edit. This table grew once
        already."""
        monkeypatch.setenv("JARVIS_SURFACE_AUDIT_SURFACES", "x=pkg.x,y=pkg.y")
        assert sr.surfaces() == (("x", "pkg.x"), ("y", "pkg.y"))


class TestAgainstTheRealTree:
    def test_it_finds_the_asymmetry_that_motivated_it(self):
        """The instrument must still distinguish one surface from another.

        SUPERSEDED 2026-08-01: was
        ``by.get("rewind_menu") == frozenset({"attach"})``.

        `rewind_menu` WAS attach-only, and that was the shape this module was
        built to find. It is now reached by all three — the daemon gained a
        diff overlay (`diff_overlay` → `overlay_arbiter` → `rewind_menu`) and
        the cockpit followed. The capability spread; the test did not, and it
        had been failing on that basis before anything in this branch touched
        it.

        Pinning a single module name made the test a hostage to the codebase
        improving. What the module actually promises is that a surface's
        reach is DISTINGUISHABLE from its neighbours' — so that is what is
        asserted, over whatever the tree currently contains. Today: attach
        reaches 27 modules alone, cockpit 9, daemon 8.

        A specific example is still checked, but derived rather than
        transcribed: if it ever becomes empty the message says which surface
        stopped being distinguishable, which is the fact worth having.
        """
        reading = sr.audit()
        solo: dict = {}
        for module in reading.modules:
            if len(module.reached_by) == 1:
                solo.setdefault(next(iter(module.reached_by)), []).append(
                    module.short)

        for label in reading.surface_labels:
            assert solo.get(label), (
                f"no module is reached by {label!r} alone — either the "
                f"surfaces have converged or `_reach`'s barriers stopped "
                f"cutting, and the second would make every asymmetry "
                f"finding vanish silently"
            )

    def test_the_original_case_is_recorded_even_though_it_healed(self):
        """`rewind_menu` reached by everything is a RESULT, not a regression.

        Kept as a named check so the healing is visible: a future change that
        made it attach-only again would be a capability the other surfaces
        lost, and that deserves to be noticed rather than silently matching an
        old assertion.
        """
        reading = sr.audit()
        by = {m.short: m.reached_by for m in reading.modules}
        assert by.get("rewind_menu") == frozenset(
            {"attach", "cockpit", "daemon"}), (
            "rewind_menu's reach changed — if it narrowed, a surface lost the "
            "rewind overlay; if it widened, this expectation needs updating "
            "with the reason"
        )

    def test_the_three_surfaces_all_resolve(self):
        reading = sr.audit()
        assert reading.unresolved_entries == ()
        assert set(reading.surface_labels) == {"daemon", "attach", "cockpit"}
