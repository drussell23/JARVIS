"""Thirty-nine orphans, one of them real.

`surface_reachability` measured reachability from three RENDERING surfaces —
`serpent_flow`, `ov`, `bipartite_layout` — and reported 39 modules unreached.
That is a fair question badly matched to the conclusion drawn from it, because
the package has at least six entry points and the audit knew about three.

Three blind spots, found by checking each orphan rather than believing the
count:

  1. **Entry points.** `pyproject.toml` declares `ov`, `jarvis` and `trinity`
     as console scripts; two of the three were not surfaces. Six modules,
     including the whole `trinity_*` cluster.
  2. **`scripts/`.** `scripts/ouroboros_battle_test.py` imports `harness`,
     which boots the six-layer stack. The direction is what hid it: the
     harness BOOTS the rendering surfaces, and a surface never imports what
     started it — so an entire entry point's subtree (termination hooks,
     session recording, watchdogs, preflight, telemetry) read as unreachable.
  3. **Relative imports.** The board's extractor returns the bare tail for
     ``from .wake_sequence import X``, which matches no indexed module, so the
     edge dangles. `wake_sequence` was called orphaned while `awakening.py`
     imports it on line 45.

Plus package ``__init__`` modules, which no import names directly.

**37 of 39 were the instrument. One is real** — `port_binder`, which has tests
and no production caller, and whose intended consumer
(``unified_supervisor._detect_best_port``) has since been deleted.

The correction that mattered most is NOT more roots. Feeding entry points into
`surfaces()` makes the count WORSE: `_reach` treats other surfaces as
barriers, so each added entry becomes another wall and every surface's reach
shrinks — 39 became 23 by making live modules look deader. Two questions were
riding one number:

    ASYMMETRY   does THIS surface reach it on its own?   needs barriers
    ORPHANHOOD  does ANY entry point reach it at all?    barriers are wrong

They are computed separately now, and both are asserted below.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import surface_reachability as sr


@pytest.fixture(scope="module")
def reading():
    return sr.audit()


class TestBothQuestionsSurvive:
    def test_orphans_collapsed_to_the_real_residual(self, reading):
        orphans = {m.module.rsplit(".", 1)[-1] for m in reading.orphans()}
        assert len(orphans) <= 3, (
            f"the blind spots are back: {sorted(orphans)}")

    def test_the_asymmetry_signal_is_NOT_collapsed(self, reading):
        """The load-bearing guard.

        Everything here could be 'fixed' by widening reach until nothing is
        ever unreached — which would also destroy the per-surface asymmetry
        the module exists to measure. That number must stay in the same
        neighbourhood it was before (80), not fall toward zero.
        """
        assert len(reading.asymmetric()) > 50, (
            "asymmetry collapsed — reach was widened until the instrument "
            "stopped distinguishing surfaces, which is the failure mode "
            "`_reach`'s barriers exist to prevent")

    def test_the_surface_table_is_still_the_three_renderers(self, reading):
        """Entry points must NOT be added to `surfaces()`. Doing so turns each
        into a barrier and shrinks every surface's reach — measured, that took
        39 orphans to 23 in the wrong direction."""
        assert reading.surface_labels == ("daemon", "attach", "cockpit")

    def test_orphanhood_is_computed_without_barriers(self, reading):
        """`entry_reachable` is a plain closure. If it were barriered it would
        be a second copy of the asymmetry walk and would inherit its blind
        spot for exactly the modules this fixes."""
        assert len(reading.entry_reachable) > len(reading.modules) // 2


class TestEntryPointDiscovery:
    def test_console_scripts_come_from_pyproject_not_a_list(self):
        """Declared data, single source of truth. A script added tomorrow
        becomes a root with no edit to this module."""
        mods = sr._pyproject_entry_modules(sr._repo_root())
        assert "backend.core.ouroboros.cli.ov" in mods
        assert "backend.core.ouroboros.cli.jarvis_thin" in mods
        assert "backend.core.ouroboros.cli.trinity_launcher" in mods

    def test_a_missing_pyproject_is_not_an_error(self, tmp_path):
        assert sr._pyproject_entry_modules(tmp_path) == []

    def test_a_malformed_pyproject_never_raises(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project.scripts\nov = broken", encoding="utf-8")
        assert isinstance(sr._pyproject_entry_modules(tmp_path), list)

    def test_main_guarded_modules_are_entries(self):
        """A module that can be RUN can be entered, and nothing in-tree needs
        to import it for that to be true."""
        index = sr._index(sr.audit_roots())
        mains = sr._main_guarded(index)
        assert "backend.core.ouroboros.cli.ov" in mains

    def test_the_harness_is_reached_through_scripts(self):
        """THE case that dominated the count. Direction is everything: the
        harness boots the surfaces, so no surface imports it."""
        index = sr._index(sr.audit_roots())
        reached = sr._script_reached(sr._repo_root(), index)
        assert "backend.core.ouroboros.battle_test.harness" in reached

    def test_derived_entries_are_labelled_by_provenance(self):
        index = sr._index(sr.audit_roots())
        labels = {label for label, _ in
                  sr.derived_entries(sr._repo_root(), index)}
        assert labels <= {"script", "runnable", "soak"}
        assert labels, "no entry point discovered at all"

    def test_discovery_never_raises_on_a_bare_tree(self, tmp_path):
        assert sr.derived_entries(tmp_path, {}) == ()


class TestRelativeImportsResolve:
    def test_the_extractor_now_resolves_a_relative_import(self, tmp_path):
        """`from .wake_sequence import X` must yield the ABSOLUTE target.

        The board's extractor returns the bare tail, which matches no indexed
        module — so the edge dangles and the target reads as unreachable while
        being imported on line 45 of its neighbour.
        """
        path = tmp_path / "awakening.py"
        path.write_text("from .wake_sequence import R\n", encoding="utf-8")
        edges = sr._edges(path, "backend.core.ouroboros.ui.awakening")
        assert "backend.core.ouroboros.ui.wake_sequence" in edges

    def test_a_two_dot_relative_climbs(self, tmp_path):
        path = tmp_path / "m.py"
        path.write_text("from ..sibling import R\n", encoding="utf-8")
        edges = sr._edges(path, "a.b.c.m")
        assert "a.b.sibling" in edges

    def test_absolute_imports_still_resolve(self, tmp_path):
        path = tmp_path / "m.py"
        path.write_text("from a.b import R\n", encoding="utf-8")
        assert "a.b" in sr._edges(path, "a.b.c.m")

    def test_wake_sequence_is_no_longer_orphaned(self, reading):
        orphans = {m.module for m in reading.orphans()}
        assert "backend.core.ouroboros.ui.wake_sequence" not in orphans

    def test_an_unparseable_file_has_no_edges(self, tmp_path):
        path = tmp_path / "bad.py"
        path.write_text("def f(:\n", encoding="utf-8")
        assert sr._edges(path, "m") == set()


class TestPackages:
    def test_a_package_is_reachable_if_a_submodule_is(self, reading):
        """Nothing imports `backend.core.ouroboros.cli` by name — importing
        `...cli.ov` runs its `__init__` implicitly — so a package is
        structurally invisible to an import-edge walk."""
        orphans = {m.module for m in reading.orphans()}
        assert "backend.core.ouroboros.cli" not in orphans
        assert "backend.core.ouroboros.cli" in reading.entry_reachable


class TestTheOneRealFinding:
    def test_port_binder_is_still_reported(self, reading):
        """The whole point of the exercise. 37 false findings were hiding one
        true one, and an auditor that cries wolf 37 times gets muted before
        the 38th.

        `port_binder` has tests and NO production caller, and the code it was
        authorised to replace — `unified_supervisor._detect_best_port` — has
        since been deleted. Reported, not deleted: superseded is not the same
        as dead, and that call is the operator's.
        """
        orphans = {m.module for m in reading.orphans()}
        assert "backend.core.ouroboros.cli.port_binder" in orphans

    def test_the_count_cannot_reach_zero_by_going_blind(self, reading):
        """A zero must mean "nothing is orphaned", never "the walk stopped
        walking" — the failure this arc already shipped once, when a recursion
        bug in the handoff auditor took its count to zero and read as a clean
        bill of health."""
        assert reading.scanned > 100, "the index stopped indexing"
        assert reading.modules, "no modules measured at all"
        assert not reading.unresolved_entries, (
            f"an entry module fell outside the scanned roots: "
            f"{reading.unresolved_entries}")

    def test_a_synthetic_dead_module_would_still_be_caught(self, tmp_path):
        """Proof by construction that the widened reach did not blind it."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "entry.py").write_text("from pkg.used import x\n", encoding="utf-8")
        (pkg / "used.py").write_text("x = 1\n", encoding="utf-8")
        (pkg / "dead.py").write_text("y = 2\n", encoding="utf-8")
        index = {
            "pkg.entry": pkg / "entry.py",
            "pkg.used": pkg / "used.py",
            "pkg.dead": pkg / "dead.py",
        }
        reached = sr._reach("pkg.entry", index, barriers=frozenset())
        assert "pkg.used" in reached
        assert "pkg.dead" not in reached
