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

import ast

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


class TestTheInstrumentMeasuresTheGraphItReportsOn:
    """Five of eight orphans were the instrument, again.

    The first correction (39 → 3) fixed the ROOT SET. These fix the INDEX
    and the TRAVERSAL: what counts as a module at all, and how far an edge
    is allowed to run before the walk gives up and calls the target dead.
    """

    def test_a_name_no_import_can_spell_is_not_a_finding(self):
        """`audio_pump 2.py` is unreachable by construction, not by defect.

        Five of eight reported orphans were Finder/iCloud duplicates whose
        module name contains a space. No import statement can name them, so
        reporting them is a tautology dressed as a discovery.
        """
        assert not sr._is_importable("pkg.audio_pump 2")
        assert not sr._is_importable("pkg.foo-bar")
        assert not sr._is_importable("pkg.2fast")
        assert not sr._is_importable("pkg.class")     # a keyword
        assert sr._is_importable("pkg.audio_pump")

    def test_the_index_excludes_them(self):
        index = sr._index(sr.audit_roots())
        assert all(sr._is_importable(m) for m in index)

    def test_an_edge_that_leaves_the_reported_roots_is_still_followed(self):
        """The traversal scope and the reporting scope are different.

        `transcript_timeline` was called an orphan while `why_engine`
        imports it — `why_engine` lives in `governance/`, which was not
        indexed, so the edge dangled and the target read as dead. A walk
        that stops at the reporting boundary INVENTS deaths, which is the
        strictly worse error.
        """
        graph = sr._index((sr.traversal_root(),))
        assert "backend.core.ouroboros.governance.why_engine" in graph
        assert len(graph) > len(sr._index(sr.audit_roots()))

    def test_transcript_timeline_is_not_an_orphan(self, reading):
        orphans = {m.short for m in reading.orphans()}
        assert "transcript_timeline" not in orphans

    def test_a_routed_verb_counts_as_an_entry_point(self):
        """`repl_dispatch_registry` reaches 88 verbs by dynamic import.

        That is an edge no AST walker can see, so without this the whole
        routed cockpit surface — and every subtree under it — reports as
        orphaned. It is also the shape that produced a false "unmounted"
        diagnosis: grep cannot see a dynamic mount.
        """
        graph = sr._index((sr.traversal_root(),))
        mounted = sr._dispatch_mounted(graph, sr._Scan())
        assert any(m.endswith("why_repl") for m in mounted), mounted
        assert len(mounted) > 50, (
            f"only {len(mounted)} routed verb modules detected — the "
            f"registry routes ~88")
        labels = {label for label, _ in sr.derived_entries(
            sr._repo_root(), graph, sr._Scan())}
        assert "verb" in labels

    def test_many_seeds_give_exactly_the_union_of_one_seed_each(self):
        """The orphan question is a union over 228 entries.

        Walking them one at a time re-walks a 1500-node graph 228 times for
        an answer a single multi-source walk gives exactly.
        """
        graph = sr._index((sr.traversal_root(),))
        scan = sr._Scan()
        seeds = [e for _, e in sr.surfaces() if e in graph]
        one_at_a_time = set()
        for s in seeds:
            one_at_a_time |= sr._reach(s, graph, frozenset(), scan)
        assert sr._reach(seeds, graph, frozenset(), scan) == one_at_a_time

    def test_a_seed_is_never_its_own_barrier(self):
        """Or a surface would stop at its own front door."""
        graph = sr._index((sr.traversal_root(),))
        entries = frozenset(e for _, e in sr.surfaces())
        for _, entry in sr.surfaces():
            if entry in graph:
                assert len(sr._reach(entry, graph, entries)) > 1

    def test_a_run_guard_is_the_main_literal_not_any_name_comparison(self):
        """The byte pre-filter and the AST test must agree exactly.

        `__name__` is worthless as a filter — `logging.getLogger(__name__)`
        puts it in 883 of 1522 files. `__main__` is in 33.
        """
        yes = ast.parse('if __name__ == "__main__":\n    pass').body[0]
        flipped = ast.parse('if "__main__" == __name__:\n    pass').body[0]
        no = ast.parse('if __name__ == "__mp_main__":\n    pass').body[0]
        assert sr._is_run_guard(yes.test)
        assert sr._is_run_guard(flipped.test)
        assert not sr._is_run_guard(no.test)
        assert sr._RUN_GUARD_LITERAL.encode() not in b'if __name__ == "__mp_main__"'


class TestTheAuditStaysOffTheLoop:
    """Principle 3 has no exception for diagnostics."""

    async def test_audit_async_does_not_block_the_event_loop(self):
        import asyncio

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        await sr.audit_async()
        hb.cancel()
        assert ticks > 5, (
            "the loop stalled for the length of the audit — a verb that "
            "parses a thousand files must not run on the loop that also "
            "runs the organism")

    def test_the_offload_lives_at_the_one_seam(self):
        """`run_watchdog` remembered to thread it; the REPL path did not."""
        from tests.source_probe import code_of

        from backend.core.ouroboros.governance import reach_repl as rr

        assert "audit_async" in code_of(rr._audit)
        # No caller may offload on its own — two places deciding how to get
        # off the loop is how one of them ends up not doing it. Read as
        # CODE: the comment saying so would satisfy a raw substring match.
        assert "to_thread" not in code_of(rr.run_watchdog)


class TestTheAuditNeverImportsWhatItMeasures:
    """The contract that makes it safe against a module that would explode.

    Also the reason routed-verb detection is STATIC: asking
    `repl_dispatch_registry` would prime it, which imports every verb module.
    """

    def test_routed_verb_detection_parses_rather_than_imports(self):
        from tests.source_probe import code_of

        code = code_of(sr, "_dispatch_mounted")
        assert "import_module" not in code
        assert "prime_registry" not in code
        assert "ast." in code or "scan.tree" in code


class TestAMountedVerbIsProvenByExecution:
    """The pin that would have caught the false-orphan diagnosis.

    `/reach` and `/why` were declared unmounted on the strength of a grep
    that found no caller. They were routed the whole time by
    `repl_dispatch_registry.try_dispatch`, whose mount is dynamic and
    therefore invisible to grep. "No caller" is an EXECUTION result, never a
    search result — so this asserts by dispatching.
    """

    async def test_every_routed_verb_module_answers_its_own_help(self):
        import inspect

        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as rd,
        )

        rd.prime_registry()
        checked = 0
        for verb, fn in sorted(rd._VERB_TO_DISPATCHER.items()):
            module = inspect.getmodule(fn)
            authored = getattr(module, "__verb_help__", None)
            if not isinstance(authored, dict) or verb not in authored:
                continue          # only verbs that CLAIM an operator surface
            outcome = await rd.try_dispatch(f"/{verb} help")
            assert outcome is not None and outcome.matched, (
                f"/{verb} declares operator help and is not routed")
            checked += 1
        assert checked >= 3, f"only {checked} verbs declare authored help"

    async def test_the_registry_is_awaited_by_the_daemon_surface(self):
        """A routed table nobody calls is the same defect one level up."""
        from tests.source_probe import code_of

        from backend.core.ouroboros.battle_test import serpent_flow as sf

        code = code_of(sf)
        assert "try_dispatch" in code, "the dispatch registry has no caller"
