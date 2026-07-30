"""A diff answers "what changed". A gate asks "what does this reach".

`OperationAdvisor` computes reach with real care — a count, an epistemic
provenance, a localized lower bound when a global scan cannot finish — and
`render_safety_plan()` writes it into the GENERATION PROMPT. The model is
told. The human approving the change is not.

The constraint that shapes everything here: a cold blast scan is a 39–43
second burn (the failure that produced the whole Targeted Locality Bounding
arc, where a timed-out scan fabricated `blast=50`, cached it, and let it
satisfy a hard BLOCK). So the gutter reads only what is already known, and
says `?` for the rest.
"""
from __future__ import annotations

import ast
import pathlib
import time

import pytest

from backend.core.ouroboros.governance import operation_advisor as oa
from backend.core.ouroboros.ui import blast_gutter as bg


@pytest.fixture(autouse=True)
def _clean_cache():
    oa._BLAST_RADIUS_CACHE_SHARED.clear()
    oa._BLAST_PROVENANCE_SHARED.clear()
    yield
    oa._BLAST_RADIUS_CACHE_SHARED.clear()
    oa._BLAST_PROVENANCE_SHARED.clear()


def _warm(path: str, count: int, provenance: str = "measured") -> None:
    key = (frozenset([path]), str(pathlib.Path.cwd()))
    oa._BLAST_RADIUS_CACHE_SHARED[key] = (time.time(), count)
    oa._BLAST_PROVENANCE_SHARED[key] = provenance


class TestItCanNeverTriggerAScan:
    """The load-bearing property. Everything else is presentation."""

    def test_peek_blast_calls_nothing_that_computes(self):
        """Structural: the read-only lookup must not reach any of the
        advisor's computing entry points. Asserted on CALL NODES, not on a
        substring search — the docstring names `_compute_blast_radius`
        precisely to explain what it is the read-only half OF."""
        src = pathlib.Path(oa.__file__).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "peek_blast")
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        forbidden = {"_compute_blast_radius", "_compute_blast_radius_async",
                     "_oracle_blast_count", "compute_blast_radius",
                     "get_blast_radius", "_cooperative_scan"}
        assert not (called & forbidden), called & forbidden

    def test_peek_blast_never_WRITES_the_cache(self):
        """A display surface that populated the cache would let rendering
        change what a later GATE decides."""
        src = pathlib.Path(oa.__file__).read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "peek_blast")
        writes = [t for node in ast.walk(fn)
                  if isinstance(node, ast.Assign)
                  for t in node.targets if isinstance(t, ast.Subscript)]
        assert not writes

    def test_the_render_path_only_peeks(self):
        src = pathlib.Path(bg.__file__).read_text()
        called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.Call)}
        assert "peek_blast" in called
        assert not (called & {"compute_blast_radius", "get_blast_radius"})

    def test_a_cold_lookup_is_a_MISS_not_a_zero(self):
        assert oa.peek_blast(["never/seen.py"]) is None
        assert bg.peek(["never/seen.py"])[0].resolved is False


class TestResolvedZeroIsNotUnknown:
    """The distinction that costs the most to get wrong."""

    def test_they_render_differently(self):
        _warm("touched.py", 0)
        rows = {r.reach.path: r for r in bg.annotate_set(
            bg.peek(["touched.py", "cold.py"]))}
        assert rows["touched.py"].label == "0"
        assert rows["cold.py"].label == "?"
        assert rows["touched.py"].bar != rows["cold.py"].bar

    def test_an_unresolved_reach_never_gets_a_BAR(self):
        """An empty bar would be indistinguishable from a resolved zero."""
        assert bg.bar(bg.Reach("x"), 10).strip() == "?"

    def test_unresolved_is_styled_as_a_WARNING_not_as_data(self):
        assert bg.style_for(bg.Reach("x")) == "heal"
        assert bg.style_for(bg.Reach("x", 3, "measured", True)) == "dim"


class TestTheScaleIsRelativeToTheSet:
    def test_scale_ignores_unresolved(self):
        """Letting a `?` participate would require inventing a value."""
        _warm("a.py", 30)
        reaches = bg.peek(["a.py", "cold.py"])
        assert bg.scale_of(reaches) == 30

    def test_the_largest_file_fills_the_bar(self):
        _warm("big.py", 40)
        _warm("small.py", 4)
        rows = {r.reach.path: r for r in bg.annotate_set(
            bg.peek(["big.py", "small.py"]))}
        assert rows["big.py"].bar.strip() == "█" * 6
        assert len(rows["small.py"].bar.strip()) < 6

    def test_the_largest_is_flagged_only_when_there_is_a_comparison(self):
        """A lone file is not "the riskiest" — there is nothing it is
        riskier than."""
        _warm("solo.py", 99)
        assert bg.annotate_set(bg.peek(["solo.py"]))[0].role == "dim"
        _warm("other.py", 1)
        rows = {r.reach.path: r for r in bg.annotate_set(
            bg.peek(["solo.py", "other.py"]))}
        assert rows["solo.py"].role == "alert"

    def test_scale_of_an_all_unknown_set_is_zero_not_a_crash(self):
        assert bg.scale_of(bg.peek(["a.py", "b.py"])) == 0


class TestTheLowerBoundSurvivesToTheEye:
    def test_a_localized_scan_renders_as_at_least(self):
        """`advisor_locality` keeps "measured lower bound" distinct all the
        way through the gate; dropping it at the last inch would undo the
        arc that put it there."""
        _warm("loc.py", 12, "localized_lower_bound")
        assert bg.annotate_set(bg.peek(["loc.py"]))[0].label == "≥12"

    def test_a_measured_scan_renders_exactly(self):
        _warm("m.py", 12, "measured")
        assert bg.annotate_set(bg.peek(["m.py"]))[0].label == "12"

    def test_the_advisor_vocabulary_is_not_reinterpreted_here(self):
        """`Reach` keeps the advisor's string verbatim so `provenance`
        stays the single place that vocabulary is interpreted."""
        _warm("v.py", 1, "localized_lower_bound")
        assert bg.peek(["v.py"])[0].provenance == "localized_lower_bound"


class TestTTLAndStaleness:
    def test_a_stale_entry_is_a_MISS(self):
        key = (frozenset(["old.py"]), str(pathlib.Path.cwd()))
        oa._BLAST_RADIUS_CACHE_SHARED[key] = (
            time.time() - (oa._BLAST_RADIUS_CACHE_TTL_S + 5), 99)
        oa._BLAST_PROVENANCE_SHARED[key] = "measured"
        assert oa.peek_blast(["old.py"]) is None

    def test_a_fresh_entry_is_a_hit(self):
        _warm("fresh.py", 7)
        assert oa.peek_blast(["fresh.py"]) == (7, "measured")


class TestTheSummaryReportsWhatIsNOTKnown:
    def test_an_all_unknown_set_says_so(self):
        """A gutter that is mostly `?` IS the finding."""
        assert bg.summary(bg.peek(["a.py", "b.py"])) == "reach unmeasured"

    def test_partial_knowledge_is_counted(self):
        _warm("known.py", 5)
        assert "1 unmeasured" in bg.summary(bg.peek(["known.py", "cold.py"]))

    def test_empty_set_renders_nothing(self):
        assert bg.summary([]) == ""


class TestTheTreeDegradesSafely:
    def test_master_flag_off_leaves_the_tree_untouched(self, monkeypatch):
        from backend.core.ouroboros.battle_test.diff_preview import (
            DiffPreviewRenderer, FileChange,
        )
        _warm("f.py", 9)
        changes = [FileChange(path="f.py", old_content="a\n",
                              new_content="b\n")]
        monkeypatch.setenv(bg.MASTER_FLAG_ENV_VAR, "0")
        off = DiffPreviewRenderer()._build_file_tree(changes)
        monkeypatch.setenv(bg.MASTER_FLAG_ENV_VAR, "1")
        on = DiffPreviewRenderer()._build_file_tree(changes)
        assert str(off.children[0].label) != str(on.children[0].label)
        assert "9" in str(on.children[0].label)

    def test_a_broken_gutter_costs_a_column_not_the_diff(self, monkeypatch):
        from backend.core.ouroboros.battle_test.diff_preview import (
            DiffPreviewRenderer, FileChange,
        )
        monkeypatch.setattr(bg, "peek", lambda *_a, **_k: (
            _ for _ in ()).throw(RuntimeError("boom")))
        tree = DiffPreviewRenderer()._build_file_tree(
            [FileChange(path="f.py", old_content="a\n", new_content="b\n")])
        assert "f.py" in str(tree.children[0].label)


class TestItNeverRaises:
    @pytest.mark.parametrize("bad", [None, 0, object(), b"x", [], {}])
    def test_peek_survives_anything(self, bad):
        assert isinstance(bg.peek([bad]), list)  # type: ignore[list-item]

    @pytest.mark.parametrize("bad", [None, 0, object(), "x"])
    def test_peek_blast_survives_anything(self, bad):
        assert oa.peek_blast(bad) is None  # type: ignore[arg-type]

    def test_bar_survives_a_nonsense_scale(self):
        r = bg.Reach("x", 5, "measured", True)
        for scale in (0, -1, 1):
            assert isinstance(bg.bar(r, scale), str)

    def test_ascii_degradation(self):
        r = bg.Reach("x", 10, "measured", True)
        out = bg.bar(r, 10, ascii_only=True)
        assert out.strip() and all(ord(c) < 128 for c in out)

    def test_as_dict_is_transport_safe(self):
        import json
        _warm("j.py", 3)
        json.dumps(bg.as_dict(bg.peek(["j.py", "cold.py"])))
