"""Reading a diff riskiest-first.

`blast_gutter` already measured every changed file's reach and drew it. What
nothing did was use that number to decide reading ORDER — the tree rendered
in whatever sequence the diff produced, so a one-line change to a leaf could
sit above a rewrite forty modules import. An operator reviewing under a
five-second NOTIFY_APPLY countdown reads from the top.

The failure pinned hardest is the one that would be actively dangerous: an
UNRESOLVED reach reports `count == 0`, and zero-as-a-number means "reaches
nothing" — the most reassuring answer there is, attached to the file we know
least about.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import blast_bands as BB
from backend.core.ouroboros.ui.blast_gutter import Reach


def _r(path, count, resolved=True, provenance="measured"):
    return Reach(path=path, count=count, provenance=provenance,
                 resolved=resolved)


class TestBanding:
    def test_a_hub_is_wide(self):
        assert BB.band_of(_r("hub.py", 42)) is BB.Band.WIDE

    def test_a_handful_is_moderate(self):
        assert BB.band_of(_r("mid.py", 5)) is BB.Band.MODERATE

    def test_a_leaf_is_contained(self):
        assert BB.band_of(_r("leaf.py", 0)) is BB.Band.CONTAINED

    def test_an_unresolved_reach_is_UNKNOWN_not_contained(self):
        """THE dangerous case. `peek` is read-only and returns count 0 for a
        file the advisor has not measured. Reading that as "reaches nothing"
        marks the least-known file as the safest."""
        assert BB.band_of(_r("mystery.py", 0, resolved=False)) is BB.Band.UNKNOWN

    def test_resolved_is_consulted_before_count(self):
        """Even a nonzero count means nothing if the reach is unresolved."""
        assert BB.band_of(_r("x.py", 99, resolved=False)) is BB.Band.UNKNOWN

    def test_none_and_garbage_are_unknown(self):
        for bad in (None, object(), "nope"):
            assert BB.band_of(bad) is BB.Band.UNKNOWN


class TestOrder:
    ROWS = [
        _r("leaf.py", 0),
        _r("hub.py", 42),
        _r("mid.py", 5),
        _r("mystery.py", 0, resolved=False),
        _r("also_mid.py", 5),
    ]

    def test_unknown_sorts_first(self):
        """Not last. A file whose blast radius could not be established is
        not safe, it is unmeasured — the same rule the advisor's repair
        settled when it made `?` never render as `0`."""
        assert BB.review_order(self.ROWS)[0].path == "mystery.py"

    def test_then_widest_first(self):
        order = [r.path for r in BB.review_order(self.ROWS)]
        assert order[:2] == ["mystery.py", "hub.py"]
        assert order[-1] == "leaf.py"

    def test_ties_break_on_path_so_the_order_is_stable(self):
        """A tree that reshuffles equal-risk files between frames is one an
        operator cannot keep their place in."""
        order = [r.path for r in BB.review_order(self.ROWS)]
        assert order.index("also_mid.py") < order.index("mid.py")
        assert BB.review_order(self.ROWS) == BB.review_order(self.ROWS)

    def test_order_paths_treats_an_unknown_path_as_unresolved(self):
        """A path with no matching reach sorts first for the same reason an
        unmeasured file does — that is exactly what it is."""
        paths = ["leaf.py", "ghost.py", "hub.py"]
        got = BB.order_paths(paths, [_r("leaf.py", 0), _r("hub.py", 42)])
        assert got[0] == "ghost.py"

    def test_the_kill_switch_restores_diff_order(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BLAST_BANDS_ENABLED", "0")
        assert BB.review_order(self.ROWS) == self.ROWS
        paths = ["b.py", "a.py"]
        assert BB.order_paths(paths, []) == paths

    def test_empty_input_is_survivable(self):
        assert BB.review_order([]) == []
        assert BB.order_paths([], []) == []


class TestThresholds:
    def test_they_are_env_tunable(self, monkeypatch):
        """"Wide" is a property of the REPOSITORY: eleven dependents is
        unremarkable in a hub package and alarming in a leaf."""
        monkeypatch.setenv("JARVIS_BLAST_WIDE_AT", "4")
        monkeypatch.setenv("JARVIS_BLAST_MODERATE_AT", "2")
        assert BB.thresholds() == (4, 2)
        assert BB.band_of(_r("x", 4)) is BB.Band.WIDE

    def test_inverted_thresholds_are_repaired_not_obeyed(self, monkeypatch):
        """Swapped values would make every file wide and none moderate,
        silently."""
        monkeypatch.setenv("JARVIS_BLAST_WIDE_AT", "3")
        monkeypatch.setenv("JARVIS_BLAST_MODERATE_AT", "9")
        wide, moderate = BB.thresholds()
        assert wide >= moderate

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BLAST_WIDE_AT", "not-a-number")
        assert BB.thresholds()[0] > 0


class TestSummary:
    def test_it_names_the_risky_bands_first(self):
        rows = [_r("a", 42), _r("b", 0, resolved=False), _r("c", 0)]
        assert BB.summarise_bands(rows) == "1 unknown · 1 wide · 1 contained"

    def test_an_all_contained_diff_says_nothing(self):
        """The absence of the warning IS the signal — the restraint the rest
        of this cockpit already keeps."""
        assert BB.summarise_bands([_r("a", 0), _r("b", 1)]) == ""

    def test_empty_is_empty(self):
        assert BB.summarise_bands([]) == ""


class TestWiredIntoTheTree:
    def test_the_preview_orders_by_reach(self):
        import ast
        import inspect
        import textwrap

        from backend.core.ouroboros.battle_test import diff_preview

        src = textwrap.dedent(inspect.getsource(
            diff_preview.DiffPreviewRenderer._build_file_tree))
        tree = ast.parse(src)
        assert any(
            isinstance(n, ast.Attribute) and n.attr == "order_paths"
            for n in ast.walk(tree)
        ), "the file tree still renders in diff order"

    def test_it_does_not_promise_partial_apply(self):
        """Apply is per-OPERATION. Splitting it by band would need
        orchestrator support, rollback for a half-applied candidate, and a
        VERIFY that knows which half ran. Offering the UI for it would be an
        affordance the engine cannot honour."""
        import inspect

        src = inspect.getsource(BB)
        assert "does NOT" in src and "partial apply" in src
        for forbidden in ("def accept_band", "def reject_band",
                          "def apply_band"):
            assert forbidden not in src
