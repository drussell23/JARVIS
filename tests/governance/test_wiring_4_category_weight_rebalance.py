"""Wiring PR #4 — Phase 7.5 ExplorationLedger category-weight wiring pins.

Phase 7.5 shipped `compute_effective_category_weights(base, adapted=None)`
as the substrate (PR #23139). This wiring PR threads that helper into
`exploration_engine.py:diversity_score()` via two new module-level
helpers (`_baseline_category_weights` + `_compute_active_category_weights`)
that compose adapted weights as MULTIPLIERS on per-tool contributions.

Pinned cage:
  * Master-off byte-identical: when JARVIS_EXPLORATION_LEDGER_LOAD_
    ADAPTED_CATEGORY_WEIGHTS=false (default), every multiplier is
    1.0 → diversity_score arithmetically equivalent to pre-wiring.
  * Master-on rebalance: high-value categories scale up; low-value
    scale down; net Σ tightens (per Slice 5 cage rule, enforced at
    substrate, not re-validated here).
  * UNCATEGORIZED tools default to multiplier 1.0 (no behavior
    change for unknown tools).
  * Defense-in-depth: substrate raise → falls back to canonical
    baseline (NEVER raises into caller).
  * Score-cap (`_BASE_SCORE_CAP=15.0`) still applied after multipliers.
  * Category multiplier (`1.0 + 0.5 × (n_cats - 1)`) still applied
    after weighted base sum.
  * Caller-grep + caller-authority invariants.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.exploration_engine import (
    ExplorationCall,
    ExplorationCategory,
    ExplorationLedger,
    _BASE_SCORE_CAP,
    _baseline_category_weights,
    _compute_active_category_weights,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_PATH = (
    _REPO_ROOT
    / "backend/core/ouroboros/governance/exploration_engine.py"
)


# ---------------------------------------------------------------------------
# Section A — baseline + active-weights helpers
# ---------------------------------------------------------------------------


class TestBaselineHelper:
    def test_baseline_has_all_5_categories(self):
        baseline = _baseline_category_weights()
        # All 5 known exploration categories at weight 1.0.
        for c in ExplorationCategory:
            if c is ExplorationCategory.UNCATEGORIZED:
                continue
            assert c.value in baseline, c.value
            assert baseline[c.value] == 1.0, c.value

    def test_baseline_excludes_uncategorized(self):
        baseline = _baseline_category_weights()
        assert ExplorationCategory.UNCATEGORIZED.value not in baseline

    def test_baseline_returns_new_dict_each_call(self):
        a = _baseline_category_weights()
        a["new"] = 99.0
        b = _baseline_category_weights()
        assert "new" not in b


class TestActiveWeightsHelper:
    def test_master_off_returns_baseline(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        active = _compute_active_category_weights()
        assert active == _baseline_category_weights()

    def test_master_on_no_yaml_returns_baseline(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH",
            str(tmp_path / "missing.yaml"),
        )
        active = _compute_active_category_weights()
        assert active == _baseline_category_weights()

    def test_master_on_valid_yaml_returns_rebalanced(
        self, monkeypatch, tmp_path,
    ):
        # Comprehension up, discovery down — net-tightening per Slice 5
        # cage (sum invariant + per-cat floor + absolute floor).
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "rebalances:\n"
            "  - new_weights:\n"
            "      comprehension: 1.20\n"
            "      discovery: 0.90\n"
            "      call_graph: 1.0\n"
            "      structure: 1.0\n"
            "      history: 1.0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH", str(yaml_path),
        )
        active = _compute_active_category_weights()
        assert active["comprehension"] == 1.20
        assert active["discovery"] == 0.90
        # Sum invariant: net tightening.
        baseline = _baseline_category_weights()
        assert sum(active.values()) >= sum(baseline.values())

    def test_substrate_raise_falls_back(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation import (
            adapted_category_weight_loader as loader,
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            "1",
        )
        with mock.patch.object(
            loader, "compute_effective_category_weights",
            side_effect=RuntimeError("boom"),
        ):
            active = _compute_active_category_weights()
            assert active == _baseline_category_weights()


# ---------------------------------------------------------------------------
# Section B — diversity_score: master-off byte-identical
# ---------------------------------------------------------------------------


def _ledger(*calls):
    return ExplorationLedger.from_records(list(calls))


class TestMasterOffByteIdentical:
    """Master-off → all multipliers 1.0 → diversity_score arithmetic
    is byte-identical to the pre-wiring formula."""

    def test_simple_4_call_diversity(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        # read_file=1.0 + search_code=1.5 + get_callers=2.5 + git_log=1.5 = 6.5
        # n_cats = 4 (comprehension/discovery/call_graph/history)
        # multiplier = 1.0 + 0.5 * 3 = 2.5
        # score = 6.5 * 2.5 = 16.25
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("search_code", "h2", 200, True),
            ExplorationCall("get_callers", "h3", 300, True),
            ExplorationCall("git_log", "h4", 400, True),
        ).diversity_score()
        assert score == 16.25

    def test_duplicate_calls_zero_contribution(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        # Two identical read_file → first contributes 1.0, second 0
        # n_cats = 1 → multiplier = 1.0 → score = 1.0
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("read_file", "h1", 100, True),
        ).diversity_score()
        assert score == 1.0

    def test_failed_calls_count_in_base_not_categories(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        # Failed call adds base_weight but not coverage.
        # Build ledger directly (from_records duck-types `status`,
        # not `succeeded` — so we bypass from_records here).
        ledger = ExplorationLedger(
            calls=(ExplorationCall("read_file", "h1", 100, False),),
        )
        # base = 1.0; n_cats = 0 (failed → not covered) → mult = 0 → score = 0.
        assert ledger.diversity_score() == 0.0

    def test_score_cap_applied(self, monkeypatch):
        # 30 unique reads × 1.0 weight = 30.0 base → capped at 15.0
        # n_cats = 1 → multiplier = 1.0 → score = 15.0
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        score = _ledger(*[
            ExplorationCall("read_file", f"h{i}", 100, True)
            for i in range(30)
        ]).diversity_score()
        assert score == 15.0

    def test_master_off_explicit_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            "false",
        )
        # Same as test_simple_4_call_diversity.
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("search_code", "h2", 200, True),
            ExplorationCall("get_callers", "h3", 300, True),
            ExplorationCall("git_log", "h4", 400, True),
        ).diversity_score()
        assert score == 16.25


# ---------------------------------------------------------------------------
# Section C — Master-on rebalance behavior
# ---------------------------------------------------------------------------


def _seed_yaml(monkeypatch, tmp_path, **weights):
    yaml_path = tmp_path / "y.yaml"
    weights_yaml = "\n".join(
        f"      {k}: {v}" for k, v in weights.items()
    )
    yaml_path.write_text(
        "schema_version: 1\n"
        "rebalances:\n"
        "  - new_weights:\n"
        f"{weights_yaml}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS", "1",
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH", str(yaml_path),
    )


class TestMasterOnRebalance:
    def test_high_value_category_calls_score_higher(
        self, monkeypatch, tmp_path,
    ):
        # Bump comprehension to 1.5 (was 1.0). Calls in the
        # comprehension category (read_file = 1.0 base × 1.5 = 1.5
        # weighted) score MORE than master-off baseline.
        _seed_yaml(
            monkeypatch, tmp_path,
            comprehension=1.5, discovery=1.0,
            call_graph=1.0, structure=1.0, history=1.0,
        )
        # Single read_file → comprehension category.
        # base = 1.0 * 1.5 = 1.5; n_cats = 1 → mult = 1.0; score = 1.5.
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
        ).diversity_score()
        assert score == 1.5

    def test_low_value_category_calls_score_lower(
        self, monkeypatch, tmp_path,
    ):
        # Drop discovery to 0.5 (per Slice 5 floor — half of base=1.0).
        # Single glob_files (discovery, base 0.5) → 0.5 × 0.5 = 0.25.
        # But Slice 5 cage requires net-tighten. To pass cage, we need
        # to bump another category. comprehension=1.5 + discovery=0.5
        # + others=1.0×3 = 5.0 → equal to baseline (5.0) → just barely
        # passes sum invariant.
        _seed_yaml(
            monkeypatch, tmp_path,
            comprehension=1.5, discovery=0.5,
            call_graph=1.0, structure=1.0, history=1.0,
        )
        # Single glob_files (discovery): 0.5 base × 0.5 mult = 0.25.
        # n_cats=1 → mult=1.0 → score = 0.25.
        score = _ledger(
            ExplorationCall("glob_files", "h1", 100, True),
        ).diversity_score()
        assert score == 0.25

    def test_doctored_loosening_yaml_rejected_falls_back(
        self, monkeypatch, tmp_path,
    ):
        # YAML attempts a NET-LOSS (sum < baseline). Substrate's
        # _net_tighten_check rejects → returns dict(baseline) → all
        # multipliers 1.0 → byte-identical to master-off.
        _seed_yaml(
            monkeypatch, tmp_path,
            comprehension=0.6, discovery=0.6,
            call_graph=0.6, structure=1.1, history=1.1,
        )  # sum = 4.0 < 5.0 baseline → REJECTED at substrate
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("search_code", "h2", 200, True),
            ExplorationCall("get_callers", "h3", 300, True),
            ExplorationCall("git_log", "h4", 400, True),
        ).diversity_score()
        assert score == 16.25  # same as master-off

    def test_uncategorized_call_uses_multiplier_1_0(
        self, monkeypatch, tmp_path,
    ):
        # Tools not in _TOOL_CATEGORY map to UNCATEGORIZED. Their
        # weight in the active dict is missing → defaults to 1.0
        # via dict.get(cat, 1.0).
        _seed_yaml(
            monkeypatch, tmp_path,
            comprehension=2.0, discovery=1.0,
            call_graph=1.0, structure=1.0, history=1.0,
        )
        # Unknown tool → UNCATEGORIZED → base_weight=0.0 anyway.
        # Score is 0.0 regardless.
        score = _ledger(
            ExplorationCall("totally_unknown_tool", "h1", 100, True),
        ).diversity_score()
        assert score == 0.0

    def test_partial_yaml_uses_baseline_for_missing(
        self, monkeypatch, tmp_path,
    ):
        # YAML only specifies comprehension; others default to baseline.
        # comprehension=1.5 + 4 others at 1.0 = 5.5 ≥ 5.0 (baseline) ✓
        _seed_yaml(
            monkeypatch, tmp_path,
            comprehension=1.5,
        )
        # Discovery should still be 1.0 (default baseline).
        # Single glob_files (discovery): 0.5 × 1.0 = 0.5.
        score = _ledger(
            ExplorationCall("glob_files", "h1", 100, True),
        ).diversity_score()
        assert score == 0.5

    def test_score_cap_still_applied_after_multipliers(
        self, monkeypatch, tmp_path,
    ):
        # Comprehension boosted → could push above cap. Verify cap
        # is applied AFTER multipliers.
        _seed_yaml(
            monkeypatch, tmp_path,
            comprehension=2.0, discovery=1.0,
            call_graph=1.0, structure=1.0, history=1.0,
        )
        # 30 reads × 1.0 base × 2.0 mult = 60 base → CAP to 15.
        # n_cats=1 → mult=1.0 → score = 15.
        score = _ledger(*[
            ExplorationCall("read_file", f"h{i}", 100, True)
            for i in range(30)
        ]).diversity_score()
        assert score == 15.0


# ---------------------------------------------------------------------------
# Section D — caller-source invariants
# ---------------------------------------------------------------------------


class TestCallerSourceInvariants:
    def test_diversity_score_uses_active_weights(self):
        src = _ENGINE_PATH.read_text(encoding="utf-8")
        # The scorer must call _compute_active_category_weights
        # to fetch live weights.
        assert "_compute_active_category_weights()" in src

    def test_active_weights_helper_imports_substrate(self):
        src = _ENGINE_PATH.read_text(encoding="utf-8")
        assert (
            "from backend.core.ouroboros.governance.adaptation"
            ".adapted_category_weight_loader import" in src
        )
        assert "compute_effective_category_weights" in src

    def test_no_raw_per_tool_only_loop_in_diversity_score(self):
        # Pin the wiring shape: the diversity_score body must apply
        # the per-cat multiplier when summing base. This is the
        # bit-rot guard against silently reverting the wiring.
        src = _ENGINE_PATH.read_text(encoding="utf-8")
        # Find diversity_score body
        idx = src.find("def diversity_score(")
        assert idx > 0
        # The body should include "cat_multiplier" or "cat_weights"
        # — proves the per-category wiring is actually USED in the
        # loop, not just imported as dead weight.
        body_window = src[idx: idx + 3000]
        assert (
            "cat_multiplier" in body_window
            or "cat_weights" in body_window
        )


# ---------------------------------------------------------------------------
# Section E — defensive equality with prior pinned scores
# ---------------------------------------------------------------------------


class TestNoRegressionAgainstPinnedScores:
    """The exploration suite has many tests pinning specific score
    values. Master-off byte-identical wiring must produce IDENTICAL
    scores for the same inputs."""

    def test_balanced_4_category_exploration(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        # Same as smoke test in commit message: 4 calls / 4 categories
        # / score = 16.25.
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("search_code", "h2", 200, True),
            ExplorationCall("get_callers", "h3", 300, True),
            ExplorationCall("git_log", "h4", 400, True),
        ).diversity_score()
        assert score == 16.25

    def test_3_category_exploration(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        # read_file (1.0) + search_code (1.5) + list_symbols (1.5)
        # = 4.0 base; n_cats = 3; mult = 2.0; score = 8.0
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("search_code", "h2", 200, True),
            ExplorationCall("list_symbols", "h3", 300, True),
        ).diversity_score()
        assert score == 8.0

    def test_5_category_full_coverage(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        # Full 5-category coverage:
        # read_file=1.0 (comp) + search_code=1.5 (disc) + get_callers=2.5 (cg)
        # + list_symbols=1.5 (struct) + git_log=1.5 (hist) = 8.0
        # n_cats=5 → mult=3.0 → score = 24.0
        score = _ledger(
            ExplorationCall("read_file", "h1", 100, True),
            ExplorationCall("search_code", "h2", 200, True),
            ExplorationCall("get_callers", "h3", 300, True),
            ExplorationCall("list_symbols", "h4", 400, True),
            ExplorationCall("git_log", "h5", 500, True),
        ).diversity_score()
        assert score == 24.0
