"""Regression spine for §40 Wave 1 #16 — Compositional Curiosity.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`CuriosityVerdict` taxonomy
* Closed 4-value :class:`NoveltyLevel` taxonomy
* Composes FlagRegistry inventory
* Composes Wave 1 #15 second_order_doll_metric (maturity)
* Composes Wave 2 #5 governance_boundary_gate (cage flag)
* Composes cross_process_jsonl (§33.4 persistence)
* Import-graph extraction via ast.parse on substrate files
* Novelty score formula (maturity_product / (1 + co_occurrence))
* Pair verdict transitions (STALE / MUNDANE / NOVEL / FRONTIER)
* Top-level verdict (NO_CANDIDATES / EMERGING / ACTIONABLE /
  DISABLED)
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds + master default-FALSE
* SSE event symbol present + in _VALID_EVENT_TYPES
* End-to-end smoke test (real FlagRegistry inventory)
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    compositional_curiosity as cc,
)
from backend.core.ouroboros.governance.compositional_curiosity import (
    COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION,
    CompositionPair,
    CompositionalCuriosityReport,
    CuriosityVerdict,
    NoveltyLevel,
    _ENV_FRONTIER_THRESHOLD,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_FILES_PER_CATEGORY,
    _ENV_MAX_PAIRS,
    _ENV_NOVEL_THRESHOLD,
    _ENV_PERSIST,
    _build_category_import_graph,
    _index_by_category,
    _maturity_for_category,
    _novelty_level_for,
    _parse_imports_for_file,
    format_curiosity_panel,
    frontier_threshold,
    identify_curious_pairs,
    ledger_path,
    master_enabled,
    max_files_per_category,
    max_pairs,
    novel_threshold,
    novelty_glyph,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    verdict_glyph,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCategory:
    value: str = "safety"


@dataclass
class _FakeFlagSpec:
    name: str = "JARVIS_X"
    category: Any = field(default_factory=_FakeCategory)
    source_file: str = ""


@dataclass
class _FakeStage:
    value: str = "graduated"


@dataclass
class _FakeAxis:
    category: str = "safety"
    stage: Any = field(default_factory=_FakeStage)


@dataclass
class _FakeSnapshot:
    master_enabled: bool = True
    axes: Tuple[Any, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_NOVEL_THRESHOLD,
        _ENV_FRONTIER_THRESHOLD,
        _ENV_MAX_PAIRS,
        _ENV_MAX_FILES_PER_CATEGORY,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "curiosity.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_schema_version():
    assert (
        COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION
        == "compositional_curiosity.1"
    )


def test_master_default_false():
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_novel_threshold_default():
    assert novel_threshold() == 0.20


def test_frontier_threshold_default():
    assert frontier_threshold() == 0.50


def test_frontier_auto_clamped_above_novel(monkeypatch):
    monkeypatch.setenv(_ENV_NOVEL_THRESHOLD, "0.80")
    monkeypatch.setenv(_ENV_FRONTIER_THRESHOLD, "0.20")
    # frontier < novel → auto-clamped UP
    assert frontier_threshold() == 0.80


def test_max_pairs_default():
    assert max_pairs() == 10


def test_max_files_per_category_default():
    assert max_files_per_category() == 15


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/compositional_curiosity_ledger.jsonl"


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_closed():
    assert {v.value for v in CuriosityVerdict} == {
        "no_candidates", "emerging", "actionable", "disabled",
    }


def test_novelty_taxonomy_closed():
    assert {n.value for n in NoveltyLevel} == {
        "stale", "mundane", "novel", "frontier",
    }


@pytest.mark.parametrize("v", list(CuriosityVerdict))
def test_verdict_glyph_known(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("n", list(NoveltyLevel))
def test_novelty_glyph_known(n):
    assert novelty_glyph(n) != "?"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_index_by_category_groups_correctly():
    specs = [
        _FakeFlagSpec(
            category=_FakeCategory("safety"),
            source_file="a.py",
        ),
        _FakeFlagSpec(
            category=_FakeCategory("safety"),
            source_file="b.py",
        ),
        _FakeFlagSpec(
            category=_FakeCategory("tuning"),
            source_file="c.py",
        ),
    ]
    grouped = _index_by_category(specs)
    assert set(grouped.keys()) == {"safety", "tuning"}
    assert set(grouped["safety"]) == {"a.py", "b.py"}
    assert grouped["tuning"] == ["c.py"]


def test_index_skips_empty_fields():
    specs = [
        _FakeFlagSpec(
            category=_FakeCategory(""),
            source_file="a.py",
        ),
        _FakeFlagSpec(
            category=_FakeCategory("safety"),
            source_file="",
        ),
    ]
    grouped = _index_by_category(specs)
    assert grouped == {}


def test_index_dedupes_per_category():
    specs = [
        _FakeFlagSpec(
            category=_FakeCategory("safety"),
            source_file="a.py",
        ),
        _FakeFlagSpec(
            category=_FakeCategory("safety"),
            source_file="a.py",  # duplicate
        ),
    ]
    grouped = _index_by_category(specs)
    assert grouped["safety"] == ["a.py"]


def test_index_clamps_per_category(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_FILES_PER_CATEGORY, "2")
    specs = [
        _FakeFlagSpec(
            category=_FakeCategory("safety"),
            source_file=f"file_{i}.py",
        )
        for i in range(5)
    ]
    grouped = _index_by_category(specs)
    assert len(grouped["safety"]) == 2


def test_parse_imports_missing_file_returns_empty(tmp_path):
    result = _parse_imports_for_file(
        "nonexistent.py", repo_root=tmp_path,
    )
    assert result == frozenset()


def test_parse_imports_recovers_from_syntax_error(tmp_path):
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("def broken(\n", encoding="utf-8")
    result = _parse_imports_for_file(
        "bad.py", repo_root=tmp_path,
    )
    assert result == frozenset()


def test_parse_imports_extracts_from_and_plain(tmp_path):
    src = tmp_path / "sample.py"
    src.write_text(
        "from foo.bar import baz\n"
        "import quux\n"
        "from .relative import thing\n",
        encoding="utf-8",
    )
    result = _parse_imports_for_file(
        "sample.py", repo_root=tmp_path,
    )
    assert "foo.bar" in result
    assert "quux" in result


def test_novelty_level_stale_on_high_co_occurrence():
    assert (
        _novelty_level_for(0.9, 2, 0.20, 0.50)
        is NoveltyLevel.STALE
    )


def test_novelty_level_frontier_high_score():
    assert (
        _novelty_level_for(0.9, 0, 0.20, 0.50)
        is NoveltyLevel.FRONTIER
    )


def test_novelty_level_novel_mid_score():
    assert (
        _novelty_level_for(0.30, 0, 0.20, 0.50)
        is NoveltyLevel.NOVEL
    )


def test_novelty_level_mundane_low_score():
    assert (
        _novelty_level_for(0.05, 0, 0.20, 0.50)
        is NoveltyLevel.MUNDANE
    )


def test_maturity_unknown_category():
    snap = _FakeSnapshot(axes=())
    assert _maturity_for_category(snap, "unknown") == 0.0


def test_maturity_graduated():
    snap = _FakeSnapshot(axes=(
        _FakeAxis(
            category="safety",
            stage=_FakeStage("graduated"),
        ),
    ))
    # GRADUATED stage_weight = 1.0
    assert _maturity_for_category(snap, "safety") == 1.0


def test_maturity_untouched():
    snap = _FakeSnapshot(axes=(
        _FakeAxis(
            category="safety",
            stage=_FakeStage("untouched"),
        ),
    ))
    # UNTOUCHED stage_weight = 0.0
    assert _maturity_for_category(snap, "safety") == 0.0


# ---------------------------------------------------------------------------
# Import graph (uses real ast.parse on tmp files)
# ---------------------------------------------------------------------------


def test_import_graph_cross_category(tmp_path):
    # safety/a.py imports from tuning/b → builds edge safety→tuning
    (tmp_path / "a.py").write_text(
        "from b import x\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "x = 1\n",
        encoding="utf-8",
    )
    grouped = {
        "safety": ["a.py"],
        "tuning": ["b.py"],
    }
    graph = _build_category_import_graph(
        grouped, repo_root=tmp_path,
    )
    assert "tuning" in graph.get("safety", frozenset())


def test_import_graph_no_cross_imports(tmp_path):
    (tmp_path / "a.py").write_text(
        "import os\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "import sys\n",
        encoding="utf-8",
    )
    grouped = {
        "safety": ["a.py"],
        "tuning": ["b.py"],
    }
    graph = _build_category_import_graph(
        grouped, repo_root=tmp_path,
    )
    assert "tuning" not in graph.get("safety", frozenset())
    assert "safety" not in graph.get("tuning", frozenset())


# ---------------------------------------------------------------------------
# identify_curious_pairs — verdict matrix
# ---------------------------------------------------------------------------


def _specs_for(categories: List[str]) -> List[_FakeFlagSpec]:
    """Build minimal specs covering N categories."""
    out: List[_FakeFlagSpec] = []
    for i, cat in enumerate(categories):
        out.append(_FakeFlagSpec(
            name=f"JARVIS_{cat.upper()}_{i}",
            category=_FakeCategory(cat),
            source_file=f"backend/{cat}_module.py",
        ))
    return out


def test_master_off_returns_disabled():
    report = identify_curious_pairs()
    assert report.master_enabled is False
    assert report.verdict is CuriosityVerdict.DISABLED


def test_empty_inventory_no_candidates(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = identify_curious_pairs(
        flag_specs=[],
        doll_snapshot=_FakeSnapshot(axes=()),
    )
    assert report.verdict is CuriosityVerdict.NO_CANDIDATES


def test_all_immature_returns_no_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # 2 categories, both UNTOUCHED → maturity product = 0
    specs = _specs_for(["safety", "tuning"])
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("untouched")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("untouched")),
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    assert report.verdict is CuriosityVerdict.NO_CANDIDATES


def test_mature_uncomposed_yields_frontier(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # Both categories GRADUATED, no cross-imports → FRONTIER
    specs = _specs_for(["safety", "tuning"])
    # Create the source files (empty, no imports)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "safety_module.py").write_text(
        "x = 1\n", encoding="utf-8",
    )
    (tmp_path / "backend" / "tuning_module.py").write_text(
        "y = 2\n", encoding="utf-8",
    )
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("graduated")),
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    assert report.verdict is CuriosityVerdict.ACTIONABLE
    assert len(report.candidate_pairs) == 1
    assert (
        report.candidate_pairs[0].novelty_level
        is NoveltyLevel.FRONTIER
    )


def test_mature_composed_is_stale_filtered(monkeypatch, tmp_path):
    """When a→b AND b→a, pair is STALE and excluded."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    specs = _specs_for(["safety", "tuning"])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    # safety imports from tuning_module
    (tmp_path / "backend" / "safety_module.py").write_text(
        "from tuning_module import y\n",
        encoding="utf-8",
    )
    # tuning imports from safety_module
    (tmp_path / "backend" / "tuning_module.py").write_text(
        "from safety_module import x\n",
        encoding="utf-8",
    )
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("graduated")),
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    # STALE pairs are filtered out
    assert all(
        p.novelty_level is not NoveltyLevel.STALE
        for p in report.candidate_pairs
    )
    # If no other pairs surface, verdict should be NO_CANDIDATES
    assert report.verdict is CuriosityVerdict.NO_CANDIDATES


def test_applied_uncomposed_yields_novel(monkeypatch, tmp_path):
    """Both APPLIED (weight 0.7) → product 0.49 ≥ 0.20 NOVEL,
    < 0.50 FRONTIER → NOVEL."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    specs = _specs_for(["safety", "tuning"])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "safety_module.py").write_text(
        "x = 1\n", encoding="utf-8",
    )
    (tmp_path / "backend" / "tuning_module.py").write_text(
        "y = 2\n", encoding="utf-8",
    )
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("applied")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("applied")),
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    assert report.verdict is CuriosityVerdict.EMERGING
    assert (
        report.candidate_pairs[0].novelty_level
        is NoveltyLevel.NOVEL
    )


def test_max_pairs_cap(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MAX_PAIRS, "2")
    # 4 categories → C(4,2)=6 pairs
    specs = _specs_for([
        "safety", "tuning", "capacity", "observability",
    ])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    for cat in ("safety", "tuning", "capacity", "observability"):
        (tmp_path / "backend" / f"{cat}_module.py").write_text(
            "x = 1\n", encoding="utf-8",
        )
    snap = _FakeSnapshot(axes=tuple(
        _FakeAxis(category=cat, stage=_FakeStage("graduated"))
        for cat in ("safety", "tuning", "capacity", "observability")
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    assert len(report.candidate_pairs) <= 2
    assert report.pairs_examined == 6


def test_pairs_sorted_by_score_desc(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    specs = _specs_for([
        "safety", "tuning", "capacity",
    ])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    for cat in ("safety", "tuning", "capacity"):
        (tmp_path / "backend" / f"{cat}_module.py").write_text(
            "x = 1\n", encoding="utf-8",
        )
    # Different stages → different scores
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="capacity",
                  stage=_FakeStage("proposed")),
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    # safety×tuning (1.0×1.0=1.0) should rank above
    # safety×capacity (1.0×0.3=0.3)
    scores = [p.novelty_score for p in report.candidate_pairs]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# §33.4 persistence
# ---------------------------------------------------------------------------


def test_persist_writes_when_candidates_exist(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    specs = _specs_for(["safety", "tuning"])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "safety_module.py").write_text(
        "x = 1\n", encoding="utf-8",
    )
    (tmp_path / "backend" / "tuning_module.py").write_text(
        "y = 2\n", encoding="utf-8",
    )
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("graduated")),
    ))
    identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    p = ledger_path()
    assert p.exists()


def test_no_persist_when_empty(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    identify_curious_pairs(
        flag_specs=[],
        doll_snapshot=_FakeSnapshot(axes=()),
    )
    assert not ledger_path().exists()


def test_persist_disabled_no_write(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    specs = _specs_for(["safety", "tuning"])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    for cat in ("safety", "tuning"):
        (tmp_path / "backend" / f"{cat}_module.py").write_text(
            "x = 1\n", encoding="utf-8",
        )
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("graduated")),
    ))
    identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    assert not ledger_path().exists()


# ---------------------------------------------------------------------------
# format_curiosity_panel
# ---------------------------------------------------------------------------


def test_format_panel_master_off():
    out = format_curiosity_panel()
    assert "disabled" in out


def test_format_panel_master_on_no_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = format_curiosity_panel()
    assert "no report" in out.lower()


def test_format_panel_with_report(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    specs = _specs_for(["safety", "tuning"])
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    for cat in ("safety", "tuning"):
        (tmp_path / "backend" / f"{cat}_module.py").write_text(
            "x = 1\n", encoding="utf-8",
        )
    snap = _FakeSnapshot(axes=(
        _FakeAxis(category="safety",
                  stage=_FakeStage("graduated")),
        _FakeAxis(category="tuning",
                  stage=_FakeStage("graduated")),
    ))
    report = identify_curious_pairs(
        flag_specs=specs,
        doll_snapshot=snap,
        repo_root=tmp_path,
    )
    out = format_curiosity_panel(report)
    assert "Compositional Curiosity" in out
    assert "safety" in out
    assert "tuning" in out


# ---------------------------------------------------------------------------
# to_dict shape
# ---------------------------------------------------------------------------


def test_pair_to_dict():
    p = CompositionPair(
        category_a="safety",
        category_b="tuning",
        maturity_a=1.0,
        maturity_b=1.0,
        co_occurrence=0,
        novelty_score=1.0,
        novelty_level=NoveltyLevel.FRONTIER,
        sample_files_a=("a.py",),
        sample_files_b=("b.py",),
        boundary_crossed=False,
    )
    d = p.to_dict()
    assert d["novelty_level"] == "frontier"
    assert d["schema_version"] == COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION


def test_report_to_dict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = identify_curious_pairs(
        flag_specs=[],
        doll_snapshot=_FakeSnapshot(axes=()),
    )
    d = report.to_dict()
    assert d["verdict"] == "no_candidates"
    assert d["schema_version"] == COMPOSITIONAL_CURIOSITY_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "compositional_curiosity.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "novelty_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical_pass(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_verdict_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class CuriosityVerdict(str, enum.Enum):\n"
        "    NO_CANDIDATES = 'no_candidates'\n"
        "    EMERGING = 'emerging'\n"
        "    ACTIONABLE = 'actionable'\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_novelty_extra():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "novelty_taxonomy_closed" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class NoveltyLevel(str, enum.Enum):\n"
        "    STALE = 'stale'\n"
        "    MUNDANE = 'mundane'\n"
        "    NOVEL = 'novel'\n"
        "    FRONTIER = 'frontier'\n"
        "    EXTRA = 'extra'\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_authority_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance."
        "curiosity_scheduler import x\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_master_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return _flag('X', default=True)\n"
    )
    tree = ast.parse(bad_src)
    assert pin.validate(tree, bad_src)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(ast.parse("# x\n"), "# x\n")


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_registry_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 6
    names = {spec.name for spec in reg.registered}
    expected = {
        _ENV_MASTER,
        _ENV_PERSIST,
        _ENV_NOVEL_THRESHOLD,
        _ENV_FRONTIER_THRESHOLD,
        _ENV_MAX_PAIRS,
        _ENV_MAX_FILES_PER_CATEGORY,
    }
    assert expected.issubset(names)


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(s for s in reg.registered if s.name == _ENV_MASTER)
    assert master.default is False


# ---------------------------------------------------------------------------
# End-to-end with REAL FlagRegistry inventory
# ---------------------------------------------------------------------------


def test_end_to_end_real_inventory(monkeypatch):
    """Smoke test — runs against the real FlagRegistry inventory
    + real doll snapshot to confirm the substrate doesn't raise
    when invoked with all canonical surfaces wired."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    # Doll snapshot defaults to None (master flag off → returns
    # None) — substrate falls through cleanly.
    report = identify_curious_pairs()
    assert isinstance(report, CompositionalCuriosityReport)
    # Should never raise regardless of inventory state.


# ---------------------------------------------------------------------------
# SSE bind
# ---------------------------------------------------------------------------


def test_sse_event_symbol_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert hasattr(
        ios, "EVENT_TYPE_COMPOSITIONAL_CURIOSITY_EVALUATED",
    )
    assert (
        ios.EVENT_TYPE_COMPOSITIONAL_CURIOSITY_EVALUATED
        == "compositional_curiosity_evaluated"
    )


def test_sse_event_in_valid_set():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        "compositional_curiosity_evaluated"
        in ios._VALID_EVENT_TYPES
    )
